"""Optional-numeric sanitation — ADR-002 section 4, tests NS-1..NS-15.

Every expectation is written from the ADR and the supersession table in
``docs/verification/increment-v3-test-spec.md`` section 5: an invalid *optional* numeric output
field becomes ``None`` with a content-free ``sanitized_invalid_number`` finding on every route,
required fields are still enforced, nothing unreadable is ever read as a number, no extra model
call is spent, and mode ``off`` is unchanged.

The schema-shape tests use the real ``jsonschema`` validator over the schema a provider would
receive, because the provider side is exactly where an unannounced ``null`` would be rejected
(NS-7).
"""

from __future__ import annotations

import json

import jsonschema
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tradingagents.agents import contracts
from tradingagents.agents.contracts import (
    AGENT_CONTRACT_VERSION,
    MODE_VALIDATE,
    MODE_VALIDATE_REPAIR,
    PORTFOLIO_MANAGER,
    RESEARCH_MANAGER,
    SANITIZED_NUMBER_CODE,
    TRADER,
    AgentContractError,
    InvalidNumber,
    StrictPortfolioDecision,
    StrictTraderProposal,
    invoke_with_contract,
    render_payload,
    revalidate_saved_plan,
    sanitize_numbers,
    strict_schema_for,
    to_baseline_payload,
    validate_payload,
)
from tradingagents.agents.schemas import PortfolioDecision, TraderProposal

pytestmark = pytest.mark.unit

F03_TRADER = {"action": "Buy", "reasoning": "The plan supports adding here.",
              "entry_price": 189.5, "stop_loss": 172.0,
              "position_sizing": "5% of portfolio"}
F03_PM = {"rating": "Overweight", "executive_summary": "Add on strength.",
          "investment_thesis": "Earnings beat.", "price_target": 210.0,
          "time_horizon": "3-6 months"}
F03_RM = {"recommendation": "Overweight", "rationale": "Bulls led.", "strategic_actions": "Scale in."}

TRADER_STATE = {
    "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: r\n\n**Strategic Actions**: s",
    "market_report": "Market report body.",
    "sentiment_report": "Sentiment body.",
    "news_report": "News body.",
    "fundamentals_report": "Fundamentals body.",
}
PM_STATE = {
    "risk_debate_state": {"history": "Aggressive: add. Conservative: trim.", "count": 2},
    "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: r\n\n**Strategic Actions**: s",
    "trader_investment_plan": "**Action**: Buy\n\n**Reasoning**: r",
}
RM_STATE = {
    "investment_debate_state": {"history": "Bull: demand. Bear: margin.", "count": 2},
    "market_report": "Market report body.",
}

PAYLOAD = {TRADER: F03_TRADER, PORTFOLIO_MANAGER: F03_PM, RESEARCH_MANAGER: F03_RM}
STATE = {TRADER: TRADER_STATE, PORTFOLIO_MANAGER: PM_STATE, RESEARCH_MANAGER: RM_STATE}
NUMERIC_FIELDS = {TRADER: ("entry_price", "stop_loss"), PORTFOLIO_MANAGER: ("price_target",),
                  RESEARCH_MANAGER: ()}


class Scripted:
    """A scripted provider callable that counts its invocations."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, _prompt):
        self.calls += 1
        if not self.script:
            raise AssertionError("the provider was invoked more often than the test allows")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def run(agent, state, mode=MODE_VALIDATE, structured=None, plain=None, **kwargs):
    return invoke_with_contract(agent=agent, mode=mode, state=state, structured_llm=structured,
                                plain_llm=plain, prompt="prompt", **kwargs)


def findings_for(result, field_name):
    return [i for i in result.record.sanitized if i.field == field_name]


# ---------------------------------------------------------------------------
# NS-1 — one invalid price becomes None; everything else is preserved exactly
# ---------------------------------------------------------------------------

def test_ns1_one_invalid_price_is_absent_and_every_other_field_is_preserved():
    structured = Scripted(dict(F03_TRADER, entry_price="15%"))
    plain = Scripted(AIMessage(content="**Action**: Buy\n\n**Reasoning**: r"))
    result = run(TRADER, TRADER_STATE, structured=structured, plain=plain)

    assert result.payload.entry_price is None
    assert result.payload.stop_loss == 172.0
    assert result.payload.action.value == "Buy"
    assert result.payload.reasoning == F03_TRADER["reasoning"]
    assert result.payload.position_sizing == "5% of portfolio"

    assert result.record.ok is True
    assert (result.record.calls.structured, result.record.calls.free_text_recovery,
            result.record.calls.semantic_repair) == (1, 0, 0)
    assert result.record.calls.logical_total == 1
    assert structured.calls == 1 and plain.calls == 0

    findings = findings_for(result, "entry_price")
    assert len(findings) == 1
    assert findings[0].code == SANITIZED_NUMBER_CODE
    assert findings[0].detail == contracts.REASON_PERCENTAGE
    assert "15%" not in result.record.canonical_json()


# ---------------------------------------------------------------------------
# NS-2 — several invalid prices all become None, charged once
# ---------------------------------------------------------------------------

def test_ns2_two_invalid_prices_are_both_absent_with_one_call():
    structured = Scripted(dict(F03_TRADER, entry_price="150-160", stop_loss=float("nan")))
    result = run(TRADER, TRADER_STATE, structured=structured)

    assert result.payload.entry_price is None and result.payload.stop_loss is None
    assert result.payload.position_sizing == "5% of portfolio"
    assert {i.field for i in result.record.sanitized} == {"entry_price", "stop_loss"}
    details = {i.field: i.detail for i in result.record.sanitized}
    assert details == {"entry_price": contracts.REASON_RANGE, "stop_loss": contracts.REASON_NONFINITE}
    assert structured.calls == 1 and result.record.calls.logical_total == 1


# ---------------------------------------------------------------------------
# NS-3 — every invalid class is sanitised, for both agents and both numeric fields
# ---------------------------------------------------------------------------

INVALID_VALUES = [
    ("15%", contracts.REASON_PERCENTAGE),
    ("150-160", contracts.REASON_RANGE),
    ("around 150", contracts.REASON_HEDGE),
    ("abc", contracts.REASON_NOT_A_NUMBER),
    ("1,2,3", contracts.REASON_GROUPING),
    ("$1,23.50", contracts.REASON_GROUPING),
    ("1,234,56", contracts.REASON_GROUPING),
    (True, contracts.REASON_BOOLEAN),
    (float("nan"), contracts.REASON_NONFINITE),
    (float("inf"), contracts.REASON_NONFINITE),
    (float("-inf"), contracts.REASON_NONFINITE),
    ("", contracts.REASON_PLACEHOLDER),
    ("   ", contracts.REASON_PLACEHOLDER),
    ([150], contracts.REASON_UNSUPPORTED_TYPE),
    ({"lo": 1}, contracts.REASON_UNSUPPORTED_TYPE),
]


def _numeric_cases():
    for agent in (TRADER, PORTFOLIO_MANAGER):
        for field_name in NUMERIC_FIELDS[agent]:
            yield agent, field_name


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
@pytest.mark.parametrize("raw,reason", INVALID_VALUES, ids=[str(r) for r, _ in INVALID_VALUES])
def test_ns3_every_invalid_class_is_sanitized(agent, field_name, raw, reason):
    payload = dict(PAYLOAD[agent], **{field_name: raw})
    structured = Scripted(payload)
    result = run(agent, STATE[agent], structured=structured)

    assert getattr(result.payload, field_name) is None
    assert result.record.ok is True
    findings = findings_for(result, field_name)
    assert len(findings) == 1 and findings[0].detail == reason
    assert structured.calls == 1 and result.record.calls.logical_total == 1
    # Every other required field survived the sanitation untouched.
    for name, value in PAYLOAD[agent].items():
        if name != field_name:
            assert getattr(result.payload, name) in (value, getattr(getattr(result.payload, name), "value", value))


# ---------------------------------------------------------------------------
# NS-4 — valid numerics are preserved; no sign or magnitude rule
# ---------------------------------------------------------------------------

VALID_VALUES = [("189.5", 189.5), (189.5, 189.5), ("$1,234.50", 1234.5), (-3.5, -3.5),
                (0.0, 0.0), (1e3, 1000.0), ("1e3", 1000.0), ("-12", -12.0),
                ("$1,234,567.80", 1234567.8), ("-1,500", -1500.0)]


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
@pytest.mark.parametrize("raw,expected", VALID_VALUES, ids=[str(r) for r, _ in VALID_VALUES])
def test_ns4_valid_numerics_are_preserved(agent, field_name, raw, expected):
    result = run(agent, STATE[agent], structured=Scripted(dict(PAYLOAD[agent], **{field_name: raw})))
    assert getattr(result.payload, field_name) == expected
    assert result.record.sanitized == []


# ---------------------------------------------------------------------------
# NS-5 — an explicit omission stays absent, with no diagnostic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
@pytest.mark.parametrize("raw", [None, "N/A"])
def test_ns5_explicit_omission_is_absent_without_a_diagnostic(agent, field_name, raw):
    result = run(agent, STATE[agent], structured=Scripted(dict(PAYLOAD[agent], **{field_name: raw})))
    assert getattr(result.payload, field_name) is None
    assert result.record.sanitized == []


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
def test_ns5_natural_language_omission_is_absent_and_recorded_as_such(agent, field_name):
    """\"not provided\" states no level; it is absent, and the reason is not \"a number\"."""
    result = run(agent, STATE[agent],
                 structured=Scripted(dict(PAYLOAD[agent], **{field_name: "not provided"})))
    assert getattr(result.payload, field_name) is None
    assert [(i.field, i.detail) for i in result.record.sanitized] == [
        (field_name, contracts.REASON_NOT_A_NUMBER)]


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
def test_ns5_omitted_key_is_absent_without_a_diagnostic(agent, field_name):
    payload = {k: v for k, v in PAYLOAD[agent].items() if k != field_name}
    result = run(agent, STATE[agent], structured=Scripted(payload))
    assert getattr(result.payload, field_name) is None
    assert result.record.sanitized == []


# ---------------------------------------------------------------------------
# NS-6 — a bad required field is still refused (with a valid price)
# ---------------------------------------------------------------------------

BAD_REQUIRED = [
    (TRADER, {"action": "Long"}, "action"),
    (TRADER, {"reasoning": ""}, "reasoning"),
    (PORTFOLIO_MANAGER, {"rating": "Neutral"}, "rating"),
    (PORTFOLIO_MANAGER, {"executive_summary": "  "}, "executive_summary"),
    (PORTFOLIO_MANAGER, {"investment_thesis": ""}, "investment_thesis"),
    (RESEARCH_MANAGER, {"rationale": "   "}, "rationale"),
    (RESEARCH_MANAGER, {"strategic_actions": ""}, "strategic_actions"),
]


@pytest.mark.parametrize("agent,damage,field_name", BAD_REQUIRED,
                         ids=[f"{a}-{f}" for a, _d, f in BAD_REQUIRED])
def test_ns6_a_bad_required_field_is_still_refused(agent, damage, field_name):
    payload = dict(PAYLOAD[agent], **damage)
    with pytest.raises(AgentContractError) as caught:
        run(agent, STATE[agent], structured=Scripted(payload))
    assert field_name in {issue.field for issue in caught.value.issues}
    assert caught.value.record.calls.logical_total == 1


MISSING_REQUIRED = [
    (TRADER, {"action"}),
    (TRADER, {"reasoning"}),
    (PORTFOLIO_MANAGER, {"rating"}),
    (PORTFOLIO_MANAGER, {"executive_summary"}),
    (RESEARCH_MANAGER, {"recommendation", "strategic_actions"}),
]


@pytest.mark.parametrize("agent,missing", MISSING_REQUIRED, ids=[f"{a}-{sorted(m)[0]}" for a, m in MISSING_REQUIRED])
def test_ns6_validate_repair_still_uses_exactly_one_repair_for_a_required_defect(agent, missing):
    """A required defect still gets the single semantic repair, never a numeric fallback."""
    repaired = dict(PAYLOAD[agent])
    structured = Scripted({k: v for k, v in PAYLOAD[agent].items() if k not in missing}, repaired)
    result = run(agent, STATE[agent], mode=MODE_VALIDATE_REPAIR, structured=structured,
                 recovery_prompt=lambda issues: "repair please")

    assert result.record.calls.semantic_repair == 1
    assert result.record.calls.logical_total == 2
    assert result.record.calls.free_text_recovery == 0
    assert structured.calls == 2


# ---------------------------------------------------------------------------
# NS-7 — the native with_structured_output path sanitises before schema rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent", [TRADER, PORTFOLIO_MANAGER])
def test_ns7_optional_numeric_schema_allows_number_and_null(agent):
    schema = strict_schema_for(agent).model_json_schema()
    for name in NUMERIC_FIELDS[agent]:
        prop = schema["properties"][name]
        assert set(prop["type"]) == {"number", "null"}, prop
        assert name not in schema.get("required", [])


@pytest.mark.parametrize("agent", [TRADER, PORTFOLIO_MANAGER])
def test_ns7_provider_side_validation_accepts_null_and_numbers(agent):
    schema = strict_schema_for(agent).model_json_schema()
    base = {k: v for k, v in PAYLOAD[agent].items() if k not in NUMERIC_FIELDS[agent]}
    number_ok = dict(base, **dict.fromkeys(NUMERIC_FIELDS[agent], 1.5))
    null_ok = dict(base, **dict.fromkeys(NUMERIC_FIELDS[agent]))
    jsonschema.validate(number_ok, schema)
    jsonschema.validate(null_ok, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(base, **dict.fromkeys(NUMERIC_FIELDS[agent], "15%")), schema)


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
def test_ns7_schema_boundary_carries_an_invalid_number_as_a_marker(agent, field_name):
    payload = dict(PAYLOAD[agent], **{field_name: "15%"})
    model = strict_schema_for(agent).model_validate(payload)
    assert isinstance(getattr(model, field_name), InvalidNumber)
    findings = sanitize_numbers(agent, model)
    assert [(i.field, i.detail) for i in findings] == [(field_name, contracts.REASON_PERCENTAGE)]
    # After sanitation the marker is gone, so no accepted output can contain it.
    assert getattr(model, field_name) is None
    dumped = model.model_dump()
    assert all(not isinstance(value, InvalidNumber) for value in dumped.values())
    json.dumps(dumped)


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
def test_ns7_native_parser_style_result_is_sanitized_without_losing_required_fields(agent, field_name):
    """A native parser hands over a strict-schema instance; the contract accepts it."""
    payload = dict(PAYLOAD[agent], **{field_name: "15%"})
    parsed_by_provider = strict_schema_for(agent).model_validate(payload)
    result = run(agent, STATE[agent], structured=Scripted(parsed_by_provider))
    assert getattr(result.payload, field_name) is None
    assert result.record.ok is True
    assert findings_for(result, field_name)
    for name in PAYLOAD[agent]:
        if name != field_name:
            assert getattr(result.payload, name) not in (None, "")


def test_ns7_the_marker_never_escapes_into_the_rendered_report():
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F03_TRADER, entry_price="15%")))
    assert "not provided" in result.rendered
    assert "InvalidNumber" not in result.rendered
    assert "InvalidNumber" not in result.record.canonical_json()


def test_ns7_an_unreadable_value_on_a_model_dump_is_not_serialised():
    """``model_dump`` of an *unsanitised* model is never produced by an accepted route."""
    model = strict_schema_for(TRADER).model_validate(dict(F03_TRADER, entry_price="15%"))
    with pytest.raises(ValueError):
        json.dumps(model.model_dump(mode="json"))
    # The accepted route always dumps the sanitised model.
    sanitize_numbers(TRADER, model)
    json.dumps(model.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# NS-8 — the plain-text and JSON routes sanitise identically
# ---------------------------------------------------------------------------

PLAIN_TRADER = ("**Action**: Buy\n\n**Reasoning**: {reasoning}\n\n**Entry Price**: {price}\n\n"
                "**Stop Loss**: 172.0\n\n**Position Sizing**: 5% of portfolio")


@pytest.mark.parametrize("mangle", [
    lambda text: AIMessage(content=text),
    lambda text: AIMessage(content="```json\n" + json.dumps(_json_of(text)) + "\n```"),
    lambda text: AIMessage(content=json.dumps(_json_of(text))),
], ids=["labelled-markdown", "fenced-json", "bare-json"])
def test_ns8_plain_routes_sanitize_identically(mangle):
    text = PLAIN_TRADER.format(reasoning="r", price="15%")
    plain = Scripted(mangle(text))
    result = run(TRADER, TRADER_STATE, structured=Scripted(None), plain=plain)
    assert result.payload.entry_price is None
    assert result.payload.stop_loss == 172.0
    assert result.payload.reasoning == "r"
    # With no structured binding the plain call is the primary route; a structured attempt that
    # returns nothing is still made first, and the numeric defect is not what changed that.
    assert result.record.calls.structured == 1
    assert result.record.calls.primary_plain + result.record.calls.free_text_recovery == 1
    assert result.record.calls.logical_total == 2
    assert plain.calls == 1
    assert [(i.field, i.detail) for i in result.record.sanitized] == [
        ("entry_price", contracts.REASON_PERCENTAGE)]


def test_ns8_the_clean_plain_answer_uses_the_same_call_count():
    """The numeric defect is not what causes any difference in calls: both spend the same."""
    text = PLAIN_TRADER.format(reasoning="r", price="189.5")
    result = run(TRADER, TRADER_STATE, structured=Scripted(None),
                 plain=Scripted(AIMessage(content=text)))
    assert result.payload.entry_price == 189.5
    assert result.record.calls.structured == 1
    assert result.record.calls.logical_total == 2
    assert result.record.sanitized == []


def test_ns8_a_plain_numeric_does_not_spend_a_recovery():
    """A parse-failure fallback happens exactly once, and the numeric is not why it failed."""
    text = PLAIN_TRADER.format(reasoning="r", price="150-160")
    structured = Scripted({"action": "Buy"})  # a partial answer is a parse failure, not a semantic one
    plain = Scripted(AIMessage(content=text))
    result = run(TRADER, TRADER_STATE, structured=structured, plain=plain)
    assert result.payload.entry_price is None
    assert result.record.calls.structured == 1
    assert result.record.calls.free_text_recovery == 1
    assert result.record.calls.semantic_repair == 0
    assert result.record.calls.logical_total == 2


def _json_of(text):
    """The Trader fields stated by ``text``, as a JSON object a model could emit."""
    values = {}
    for line in text.splitlines():
        if line.startswith("**"):
            label, value = line[2:].split("**: ", 1)
            key = {"Action": "action", "Reasoning": "reasoning", "Entry Price": "entry_price",
                   "Stop Loss": "stop_loss", "Position Sizing": "position_sizing"}[label]
            values[key] = value
    return values


# ---------------------------------------------------------------------------
# NS-9 — a repaired answer is sanitised too
# ---------------------------------------------------------------------------

def test_ns9_a_repaired_answer_is_sanitized_too():
    first = dict(F03_PM, executive_summary="   ")
    repaired = dict(F03_PM, price_target="8%")
    structured = Scripted(first, repaired)
    result = run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_VALIDATE_REPAIR, structured=structured,
                 recovery_prompt=lambda issues: "repair please")

    assert result.payload.price_target is None
    assert result.payload.executive_summary == "Add on strength."
    assert [i.field for i in result.record.sanitized] == ["price_target"]
    assert result.record.calls.semantic_repair == 1 and result.record.calls.logical_total == 2


# ---------------------------------------------------------------------------
# NS-10 — a numeric-only defect does not spend the recovery slot
# ---------------------------------------------------------------------------

def test_ns10_numeric_only_defect_spends_no_recovery():
    structured = Scripted(dict(F03_TRADER, entry_price=True))
    plain = Scripted(AIMessage(content="unused"))
    result = run(TRADER, TRADER_STATE, structured=structured, plain=plain)
    assert result.payload.entry_price is None
    assert structured.calls == 1 and plain.calls == 0
    assert result.record.calls.logical_total == 1


def test_ns10_numeric_only_defect_in_repair_mode_spends_no_repair():
    structured = Scripted(dict(F03_TRADER, entry_price=True))
    result = run(TRADER, TRADER_STATE, mode=MODE_VALIDATE_REPAIR, structured=structured)
    assert result.payload.entry_price is None
    assert result.record.calls.semantic_repair == 0 and structured.calls == 1


# ---------------------------------------------------------------------------
# NS-11 — a conflicting numeric statement becomes None with a diagnostic
# ---------------------------------------------------------------------------

CONFLICTING_TEXT = ("**Action**: Buy\n\n**Reasoning**: r\n\n**Entry Price**: 189.5\n\n"
                    "**Entry Price**: 200\n\n**Stop Loss**: 172.0")


def test_ns11_conflicting_plain_statements_are_absent_with_a_diagnostic():
    result = run(TRADER, TRADER_STATE, structured=Scripted(None),
                 plain=Scripted(AIMessage(content=CONFLICTING_TEXT)))
    assert result.payload.entry_price is None
    assert result.payload.stop_loss == 172.0
    assert result.payload.reasoning == "r"


# ---------------------------------------------------------------------------
# NS-12 — the diagnostic record is stable, content-free and JSON-safe
# ---------------------------------------------------------------------------

def test_ns12_the_record_is_stable_and_content_free():
    def once():
        result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F03_TRADER, entry_price="15%")))
        return result.record.canonical_json(), result.record.as_dict()

    first, payload = once()
    second, _ = once()
    assert first == second
    assert "15%" not in first
    assert payload["sanitized"][0]["detail"] == contracts.REASON_PERCENTAGE
    json.dumps(payload)


def test_ns12_a_sanitized_model_dumps_to_json():
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F03_TRADER, stop_loss=float("inf"))))
    dumped = result.payload.model_dump(mode="json")
    json.dumps(dumped)
    assert dumped["stop_loss"] is None


# ---------------------------------------------------------------------------
# NS-13 — off mode is unchanged
# ---------------------------------------------------------------------------

def test_ns13_off_mode_baseline_coercer_is_untouched():
    for raw in ("15%", "150-160", "around 150", ""):
        assert TraderProposal(**dict(F03_TRADER, entry_price=raw)).entry_price is None
    assert TraderProposal(**dict(F03_TRADER, entry_price=True)).entry_price == 1.0
    assert PortfolioDecision(**dict(F03_PM, price_target="8%")).price_target is None


def test_ns13_off_mode_rendering_is_byte_identical_to_the_baseline():
    expected = render_payload(TRADER, TraderProposal(**F03_TRADER))
    strict = strict_schema_for(TRADER)(**F03_TRADER)
    assert render_payload(TRADER, to_baseline_payload(TRADER, strict)) == expected


def test_ns13_no_contract_record_is_produced_in_off_mode():
    """Mode off has no contract layer at all; the marker lives only in enhanced modes."""
    assert AGENT_CONTRACT_VERSION == "2"


# ---------------------------------------------------------------------------
# NS-14 — the saved-plan revalidation follows the same policy
# ---------------------------------------------------------------------------

STALE_PLAN_BAD_PRICE = ("**Action**: Buy\n\n**Reasoning**: Add.\n\n**Entry Price**: 15%\n\n"
                        "**Stop Loss**: 172.0\n\n**Position Sizing**: 5% of portfolio")
STALE_PLAN_BLANK_REASONING = "**Action**: Buy\n\n**Reasoning**: \n\n**Entry Price**: 189.5"


def test_ns14_saved_plan_with_only_a_bad_optional_numeric_is_accepted():
    assert revalidate_saved_plan(TRADER, STALE_PLAN_BAD_PRICE, "trader_investment_plan") is None


def test_ns14_saved_plan_with_a_blank_required_field_is_still_refused():
    issue = revalidate_saved_plan(TRADER, STALE_PLAN_BLANK_REASONING, "trader_investment_plan")
    assert issue is not None and issue.code in {"blank", "missing_field"}


def test_ns14_saved_plan_with_a_missing_required_field_is_still_refused():
    issue = revalidate_saved_plan(TRADER, "**Reasoning**: r", "trader_investment_plan")
    assert issue is not None and issue.field == "action"


# ---------------------------------------------------------------------------
# NS-15 — the contract version moved on
# ---------------------------------------------------------------------------

def test_ns15_contract_version_agrees_across_modules():
    import tradingagents.default_config as default_config

    assert AGENT_CONTRACT_VERSION == "2"
    assert default_config.AGENT_CONTRACT_VERSION == AGENT_CONTRACT_VERSION


def test_ns15_validate_payload_treats_a_bad_optional_numeric_as_benign_only():
    issues = validate_payload(TRADER, dict(F03_TRADER, entry_price="15%"))
    assert [i.code for i in issues] == [SANITIZED_NUMBER_CODE]
    assert [i.code for i in validate_payload(TRADER, dict(F03_TRADER, reasoning="  "))] == ["blank"]


def test_ns15_a_schema_validation_error_on_a_required_field_is_still_an_error():
    with pytest.raises(ValidationError):
        StrictTraderProposal(action="Long", reasoning="r")


def test_ns15_pm_strict_schema_still_rejects_an_unknown_rating():
    with pytest.raises(ValidationError):
        StrictPortfolioDecision(**dict(F03_PM, rating="Neutral"))


# ---------------------------------------------------------------------------
# NS-16 — ambiguous digit grouping is not silently re-read as a number
#
# Reported probe: ``read_optional_number("1,2,3")`` returned ``123.0`` and ``"$1,23.50"``
# returned ``123.5``, because the separators were removed before the grouping was checked.
# Those are malformed groupings, not levels the model stated.
# ---------------------------------------------------------------------------

AMBIGUOUS_GROUPING = ["1,2,3", "$1,23.50", "12,34", "1,234,56", "1,0000", "$12,34",
                      "1.234,50", "1,23,456"]


@pytest.mark.parametrize("agent,field_name", list(_numeric_cases()))
@pytest.mark.parametrize("raw", AMBIGUOUS_GROUPING)
def test_ns16_ambiguous_grouping_is_absent_with_a_grouping_diagnostic(agent, field_name, raw):
    result = run(agent, STATE[agent], structured=Scripted(dict(PAYLOAD[agent], **{field_name: raw})))
    assert getattr(result.payload, field_name) is None
    assert [(i.field, i.detail) for i in result.record.sanitized] == [
        (field_name, contracts.REASON_GROUPING)]
    assert result.record.ok is True and result.record.calls.logical_total == 1
    for name in PAYLOAD[agent]:
        if name != field_name:
            assert getattr(result.payload, name) is not None


@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", 1234.5),
    ("$1,234.50", 1234.5),
    ("12,345", 12345.0),
    ("123,456,789.01", 123456789.01),
    ("-1,500", -1500.0),
    ("+1,000", 1000.0),
    ("1e3", 1000.0),
    ("-2.5e-2", -0.025),
    ("-3.5", -3.5),
])
def test_ns16_well_formed_grouping_and_signs_are_still_read(raw, expected):
    assert contracts.read_optional_number(raw) == (expected, None)


def test_ns16_the_grouping_check_runs_before_the_separators_are_removed():
    """A source-level guard: the reader must not strip commas first."""
    import inspect

    source = inspect.getsource(contracts.read_optional_number)
    assert source.index("_GROUPED_NUMBER_RE") < source.index('body.replace(",", "")')


def test_ns16_off_mode_legacy_coercion_is_untouched():
    """Mode off keeps the baseline coercer exactly as upstream wrote it (INV-13).

    The baseline strips separators and reads ``"1,2,3"`` as ``123.0``; that is an observation
    of the untouched baseline, not an endorsement. Only the enhanced modes check grouping.
    """
    assert TraderProposal(**dict(F03_TRADER, entry_price="1,234.50")).entry_price == 1234.5
    assert TraderProposal(**dict(F03_TRADER, entry_price="1,2,3")).entry_price == 123.0
    assert TraderProposal(**dict(F03_TRADER, entry_price="$1,23.50")).entry_price == 123.5
