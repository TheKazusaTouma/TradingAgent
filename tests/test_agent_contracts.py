"""Core contract and recovery tests for the approved agent I/O increment.

Covers the assertions of ``docs/verification/agent-contract-test-spec.md`` v1.0 that are
testable against the pure contract layer and the invocation helper, without the graph,
CLI, reporting or node wiring (those arrive in the next increment).

Implemented here: T-2, T-3, T-4, T-5, T-6, T-7, T-8, T-9, T-10, T-11, T-12, T-13, T-14,
T-24, T-25, T-26, T-27, T-28, T-29.

NOT implemented here, because each needs the integration increment (nodes, graph, CLI,
reporting, checkpoint): T-1 (whole-suite regression), T-15, T-16, T-17, T-18, T-19, T-20,
T-21, T-22, T-23, T-30. No test below is marked as covering them.

Assertions are written from the requirements and the spec's expected outcomes, not from the
implementation's internals: fixtures are scripted callables with counters, and no test
asserts on a private helper's shape.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import tradingagents.default_config as default_config
from tradingagents.agents import contracts
from tradingagents.agents.contracts import (
    AGENT_CONTRACT_VERSION,
    MODE_OFF,
    MODE_VALIDATE,
    MODE_VALIDATE_REPAIR,
    PORTFOLIO_MANAGER,
    RESEARCH_MANAGER,
    TRADER,
    AgentContractError,
    ContractIssue,
    StrictPortfolioDecision,
    StrictResearchPlan,
    StrictTraderProposal,
    baseline_schema_for,
    invoke_with_contract,
    parse_strict_number,
    render_payload,
    validate_payload,
)
from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    TraderProposal,
    render_pm_decision,
    render_research_plan,
    render_trader_proposal,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures (F-01 family) and scripted callables with counters
# ---------------------------------------------------------------------------

F01_RM = {"recommendation": "Overweight", "rationale": "Bulls led on cash flow.", "strategic_actions": "Scale in."}
F01_TRADER = {"action": "Buy", "reasoning": "The plan supports adding here.", "entry_price": 189.5,
              "stop_loss": 172.0, "position_sizing": "5% of portfolio"}
F01_PM = {"rating": "Overweight", "executive_summary": "Add on strength.", "investment_thesis": "Earnings beat.",
          "price_target": 210.0, "time_horizon": "3-6 months"}

RM_STATE = {
    "investment_debate_state": {"history": "Bull: demand. Bear: margin.", "count": 2},
    "market_report": "Market report body.",
}
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


class Scripted:
    """A scripted callable that records how many times it ran.

    Each entry is either a payload (returned) or an exception instance (raised), so a
    test can state exactly what the provider did on the first and second call.
    """

    def __init__(self, *script: Any):
        self.script = list(script)
        self.calls = 0

    def invoke(self, _prompt: Any):
        self.calls += 1
        step = self.script.pop(0) if self.script else self.script_default()
        if isinstance(step, BaseException):
            raise step
        return step

    @staticmethod
    def script_default():
        raise AssertionError("scripted callable invoked more times than the test script allows")



def plain_message(agent, values):
    """A real provider answer for the plain route: baseline markdown inside an AIMessage.

    The plain route parses text, so fixtures supply text -- rendered by the baseline
    renderer, exactly the shape the prompts ask a model to write -- not a dict.
    """
    return AIMessage(content=render_payload(agent, baseline_schema_for(agent)(**values)))


def raw_message(text):
    return AIMessage(content=text)


def raw_for(agent, values):
    """Hand-written labelled sections for a payload, so one field can be broken on purpose."""
    spec = {
        RESEARCH_MANAGER: (("Recommendation", "recommendation"), ("Rationale", "rationale"),
                           ("Strategic Actions", "strategic_actions")),
        TRADER: (("Action", "action"), ("Reasoning", "reasoning"), ("Entry Price", "entry_price"),
                 ("Stop Loss", "stop_loss"), ("Position Sizing", "position_sizing")),
        PORTFOLIO_MANAGER: (("Rating", "rating"), ("Executive Summary", "executive_summary"),
                            ("Investment Thesis", "investment_thesis"), ("Price Target", "price_target"),
                            ("Time Horizon", "time_horizon")),
    }[agent]
    lines = []
    for label, name in spec:
        value = values.get(name)
        if value is None:
            continue
        lines.extend([f"**{label}**: {value}", ""])
    return "\n".join(lines).strip()

def run(agent, state, mode=MODE_VALIDATE, structured=None, plain=None, **kwargs):
    return invoke_with_contract(
        agent=agent, mode=mode, state=state, structured_llm=structured, plain_llm=plain,
        prompt="prompt", **kwargs,
    )


VALID = {
    RESEARCH_MANAGER: (F01_RM, StrictResearchPlan),
    TRADER: (F01_TRADER, StrictTraderProposal),
    PORTFOLIO_MANAGER: (F01_PM, StrictPortfolioDecision),
}
STATES = {RESEARCH_MANAGER: RM_STATE, TRADER: TRADER_STATE, PORTFOLIO_MANAGER: PM_STATE}


# ---------------------------------------------------------------------------
# T-2 — optional analyst reports are valid when absent, None or blank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent", [RESEARCH_MANAGER, TRADER, PORTFOLIO_MANAGER])
@pytest.mark.parametrize("variant", ["omitted", "none", "empty", "spaces"])
def test_t2_optional_analyst_reports_never_fail(agent, variant):
    payload, _schema = VALID[agent]
    state = dict(STATES[agent])
    for key in contracts.OPTIONAL_ANALYST_REPORTS:
        if variant == "omitted":
            state.pop(key, None)
        elif variant == "none":
            state[key] = None
        elif variant == "empty":
            state[key] = ""
        else:
            state[key] = "   "

    structured = Scripted(payload)
    result = run(agent, state, structured=structured)

    assert structured.calls == 1
    assert result.record.ok is True
    assert result.record.calls.structured == 1
    reported = {issue.field for issue in result.record.issues + result.record.informational}
    assert not (reported & set(contracts.OPTIONAL_ANALYST_REPORTS))


# ---------------------------------------------------------------------------
# T-3 / T-4 — invalid input costs zero model calls; markers are not content
# ---------------------------------------------------------------------------

def _invalid_input_cases():
    marker = f"{contracts.FAILURE_MARKER_PREFIX}: Portfolio Manager]"
    blank_plan = dict(TRADER_STATE, investment_plan="   ")
    int_plan = dict(TRADER_STATE, investment_plan=42)
    no_plan = dict(TRADER_STATE)
    no_plan.pop("investment_plan")
    market_none_no_plan = dict(no_plan, market_report=None)
    no_risk_history = dict(PM_STATE, risk_debate_state={"count": 2})
    return [
        (RESEARCH_MANAGER, {k: v for k, v in RM_STATE.items() if k != "investment_debate_state"}, "investment_debate_state"),
        (TRADER, market_none_no_plan, "investment_plan"),
        (TRADER, blank_plan, "investment_plan"),
        (TRADER, int_plan, "investment_plan"),
        (PORTFOLIO_MANAGER, no_risk_history, "risk_debate_state.history"),
        (PORTFOLIO_MANAGER, dict(PM_STATE, trader_investment_plan=marker), "trader_investment_plan"),
    ]


@pytest.mark.parametrize("agent,state,failing_field", _invalid_input_cases())
def test_t3_invalid_input_costs_zero_calls(agent, state, failing_field):
    structured = Scripted(*[VALID[agent][0]] * 3)
    plain = Scripted(*[VALID[agent][0]] * 3)

    with pytest.raises(AgentContractError) as caught:
        run(agent, state, structured=structured, plain=plain)

    assert structured.calls == 0
    assert plain.calls == 0
    error = caught.value
    assert error.stage == contracts.Stage.INPUT
    assert failing_field in {issue.field for issue in error.issues}
    # Case (b): a None market report is never the cause of the failure.
    for issue in error.issues:
        assert issue.field not in contracts.OPTIONAL_ANALYST_REPORTS


def test_t4_failure_marker_is_not_input():
    marker = f"{contracts.FAILURE_MARKER_PREFIX}: Trader]"
    structured = Scripted(F01_PM)
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, dict(PM_STATE, trader_investment_plan=marker), structured=structured)
    assert caught.value.stage == contracts.Stage.INPUT
    assert structured.calls == 0


# ---------------------------------------------------------------------------
# T-5 — required narratives non-blank, no length rule
# ---------------------------------------------------------------------------

_NARRATIVE_CASES = [
    (RESEARCH_MANAGER, "rationale"), (RESEARCH_MANAGER, "strategic_actions"),
    (TRADER, "reasoning"),
    (PORTFOLIO_MANAGER, "executive_summary"), (PORTFOLIO_MANAGER, "investment_thesis"),
]


@pytest.mark.parametrize("agent,field", _NARRATIVE_CASES)
@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_t5_blank_narrative_fails(agent, field, bad):
    payload = dict(VALID[agent][0], **{field: bad})
    structured = Scripted(payload)
    with pytest.raises(AgentContractError) as caught:
        run(agent, STATES[agent], structured=structured)
    assert field in {issue.field for issue in caught.value.issues}
    assert caught.value.record.calls.semantic_repair == 0


@pytest.mark.parametrize("agent,field", _NARRATIVE_CASES)
@pytest.mark.parametrize("short", ["ok", "x"])
def test_t5_short_narrative_is_accepted(agent, field, short):
    payload = dict(VALID[agent][0], **{field: short})
    result = run(agent, STATES[agent], structured=Scripted(payload))
    assert getattr(result.payload, field) == short


# ---------------------------------------------------------------------------
# T-6 — nonfinite numbers are rejected, not converted to None
# ---------------------------------------------------------------------------

def _numeric_cases():
    return [
        (TRADER, "entry_price"), (TRADER, "stop_loss"), (PORTFOLIO_MANAGER, "price_target"),
    ]


@pytest.mark.parametrize("agent,field", _numeric_cases())
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_t6_nonfinite_is_sanitized_to_absent(agent, field, bad):
    """ADR-002 section 4 supersedes the old abort: a nonfinite level becomes absent.

    T-6 as originally approved required ``AgentContractError`` here. The student's latest
    authorisation changes that policy for *optional* numeric output fields only; the field is
    now ``None``, the answer is accepted, and one content-free diagnostic names the field.
    """
    payload = dict(VALID[agent][0], **{field: bad})
    result = run(agent, STATES[agent], structured=Scripted(payload))
    assert getattr(result.payload, field) is None
    findings = [i for i in result.record.sanitized if i.field == field]
    assert findings, f"{field} was not reported"
    assert findings[0].code == contracts.SANITIZED_NUMBER_CODE
    assert findings[0].detail == "nonfinite"
    assert result.record.ok is True
    assert result.record.calls.logical_total == 1


@pytest.mark.parametrize("agent,field", _numeric_cases())
def test_t6_omitted_and_none_remain_valid(agent, field):
    payload = dict(VALID[agent][0], **{field: None})
    result = run(agent, STATES[agent], structured=Scripted(payload))
    assert getattr(result.payload, field) is None


# ---------------------------------------------------------------------------
# T-7 — legal numeric formatting; negatives accepted; no sign rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("189.5", 189.5), ("$1,234.50", 1234.5), (189.5, 189.5), (-3.5, -3.5)])
def test_t7_legal_numeric_formatting(raw, expected):
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F01_TRADER, entry_price=raw)))
    assert result.payload.entry_price == expected


def test_t7_no_positivity_rule_on_prices():
    assert parse_strict_number(-100.0, "price") == -100.0


# ---------------------------------------------------------------------------
# T-8 / T-9 — disagreement is informational; unreadable context is skipped
# ---------------------------------------------------------------------------

def test_t8_action_disagreement_is_informational():
    # A complete saved plan (review D revalidates it in full) whose rating disagrees.
    state = dict(TRADER_STATE, investment_plan="**Recommendation**: Sell\n\n**Rationale**: margins.\n\n"
                                               "**Strategic Actions**: exit.")
    result = run(TRADER, state, structured=Scripted(dict(F01_TRADER, action="Buy")))

    assert result.payload.action.value == "Buy"
    assert result.record.ok is True
    assert result.record.issues == []
    info = [i for i in result.record.informational if i.code == "action_disagrees_with_recommendation"]
    assert info and info[0].field == "action"
    assert "Sell" in info[0].detail and "Buy" in info[0].detail


def test_t9_check_skipped_when_context_absent():
    """With no readable recommendation the trade-off check is skipped, not failed.

    Review item D now refuses a *saved plan* that lacks its required recommendation, so the
    skip branch is exercised at the check itself, and the input stage is shown to refuse the
    same plan separately.
    """
    informational, skipped = contracts.check_trader_against_plan(
        "**Rationale**: carry on.\n\n**Strategic Actions**: hold.", "Buy")
    assert skipped == ["trader.action_vs_recommendation"]
    assert informational == []

    with pytest.raises(AgentContractError) as caught:
        run(TRADER, dict(TRADER_STATE, investment_plan="**Rationale**: carry on."),
            structured=Scripted(F01_TRADER))
    assert caught.value.record.stage == contracts.Stage.INPUT
    assert caught.value.record.calls.logical_total == 0


# ---------------------------------------------------------------------------
# T-10 / T-11 — one semantic repair then full revalidation; two failures exhaust
# ---------------------------------------------------------------------------

def test_t10_repair_uses_the_repaired_payload():
    state = PM_STATE
    first = dict(F01_PM, executive_summary="  ")
    repaired = dict(F01_PM, executive_summary="Add on strength.", price_target=215.0)
    structured = Scripted(first, repaired)

    result = run(PORTFOLIO_MANAGER, state, mode=MODE_VALIDATE_REPAIR,
                 structured=structured, recovery_prompt=lambda issues: "repair please")

    assert result.payload.price_target == 215.0
    assert result.record.calls.semantic_repair == 1
    assert result.record.calls.structured == 1


def test_t10_repaired_payload_is_validated_in_full():
    first = dict(F01_PM, executive_summary="  ")
    still_bad = dict(F01_PM, executive_summary="Add.", investment_thesis="\t")
    structured = Scripted(first, still_bad)

    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_VALIDATE_REPAIR,
            structured=structured, recovery_prompt=lambda issues: "repair please")

    assert "investment_thesis" in {issue.field for issue in caught.value.issues}


def test_t11_two_failures_exhaust_the_budget():
    structured = Scripted(dict(F01_PM, executive_summary=""), dict(F01_PM, executive_summary=""))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_VALIDATE_REPAIR,
            structured=structured, recovery_prompt=lambda issues: "repair please")
    calls = caught.value.record.calls
    assert calls.logical_total == 2
    assert calls.semantic_repair == 1
    assert calls.structured == 1
    assert caught.value.record.ok is False


# ---------------------------------------------------------------------------
# T-12 / T-27 — no third call; fallback counted and validated
# ---------------------------------------------------------------------------

def test_t12_no_third_call_after_invalid_fallback():
    """A parse miss falls back to free text; an invalid fallback ends the run."""
    structured = Scripted(None)
    plain = Scripted(plain_message(PORTFOLIO_MANAGER, dict(F01_PM, executive_summary="")))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=structured, plain=plain)
    calls = caught.value.record.calls
    assert calls.structured == 1
    assert calls.free_text_recovery == 1
    assert calls.semantic_repair == 0
    assert calls.logical_total == 2
    assert structured.calls == 1 and plain.calls == 1


def test_t12_repair_and_fallback_never_both_run():
    """validate+repair spends its one slot on the repair; no fallback follows it."""
    structured = Scripted(None, dict(F01_PM, executive_summary="  "))
    plain = Scripted(plain_message(PORTFOLIO_MANAGER, F01_PM))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_VALIDATE_REPAIR,
            structured=structured, plain=plain, recovery_prompt=lambda issues: "repair please")
    calls = caught.value.record.calls
    assert calls.structured == 1 and calls.semantic_repair == 1
    assert calls.free_text_recovery == 0
    assert calls.logical_total == 2
    assert structured.calls == 2 and plain.calls == 0


def test_t27_valid_fallback_is_used_only_after_validation():
    structured = Scripted(None)
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=structured, plain=Scripted(plain_message(PORTFOLIO_MANAGER, F01_PM)))
    calls = result.record.calls
    assert calls.structured == 1 and calls.free_text_recovery == 1
    assert calls.semantic_repair == 0 and calls.logical_total == 2
    assert result.record.stage == contracts.Stage.FREE_TEXT


def test_t27_no_further_call_when_fallback_invalid():
    structured = Scripted(None)
    plain = Scripted(raw_message("**Rating**: Buy-ish\n\n**Executive Summary**: s\n\n**Investment Thesis**: t"))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=structured, plain=plain)
    assert structured.calls == 1 and plain.calls == 1
    assert caught.value.record.calls.logical_total == 2
    assert "rating" in {issue.field for issue in caught.value.issues}


# ---------------------------------------------------------------------------
# T-13 — accounting record completeness and honesty
# ---------------------------------------------------------------------------

def test_t13_record_keys_and_retry_accounting():
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(F01_PM), sdk_retry_budget=None)
    calls = result.record.calls.as_dict()
    assert list(calls) == [
        "structured", "free_text_recovery", "semantic_repair", "logical_total",
        "sdk_retry_budget", "observed_http_attempts",
    ]
    assert calls["logical_total"] == calls["structured"] + calls["free_text_recovery"] + calls["semantic_repair"]
    assert calls["observed_http_attempts"] == "unknown"
    assert calls["sdk_retry_budget"] == "provider_default_not_assumed"


def test_t13_configured_retry_budget_is_reported():
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(F01_PM), sdk_retry_budget=3)
    assert result.record.calls.as_dict()["sdk_retry_budget"] == "configured:3"


def test_t13_config_module_agrees_with_contract_module():
    """default_config duplicates the mode/version strings to stay import-free."""
    assert default_config.AGENT_CONTRACT_VERSION == AGENT_CONTRACT_VERSION
    assert set(default_config.AGENT_CONTRACT_MODES) == set(contracts.CONTRACT_MODES)
    assert default_config.DEFAULT_CONFIG["agent_contract_mode"] == MODE_OFF
    assert default_config.DEFAULT_CONFIG["agent_contract_version"] == AGENT_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# T-14 — records are deterministic
# ---------------------------------------------------------------------------

def test_t14_records_are_byte_identical():
    bad_state = dict(PM_STATE, trader_investment_plan="  ")
    records = []
    for _ in range(2):
        with pytest.raises(AgentContractError) as caught:
            run(PORTFOLIO_MANAGER, bad_state, structured=Scripted(F01_PM))
        records.append(caught.value.record.canonical_json())

    assert records[0] == records[1]
    parsed = json.loads(records[0])
    assert list(parsed) == ["agent", "mode", "stage", "ok", "failures", "informational",
                            "sanitized", "skipped_checks", "calls", "contract_version"]
    assert "timestamp" not in records[0] and "duration" not in records[0]


# ---------------------------------------------------------------------------
# T-24 — valid payload rendering is unchanged
# ---------------------------------------------------------------------------

def _construct(values: dict[str, Any]) -> dict[str, Any]:
    """Enum values for model construction, which does not coerce them."""
    agent = RESEARCH_MANAGER if "recommendation" in values else (TRADER if "action" in values else PORTFOLIO_MANAGER)
    out = dict(values)
    if agent == RESEARCH_MANAGER:
        out["recommendation"] = contracts.PortfolioRating(values["recommendation"])
    elif agent == TRADER:
        out["action"] = contracts.TraderAction(values["action"])
    else:
        out["rating"] = contracts.PortfolioRating(values["rating"])
    return out


_BASELINE_RENDERS = {
    RESEARCH_MANAGER: render_research_plan(ResearchPlan(**_construct(F01_RM))),
    TRADER: render_trader_proposal(TraderProposal(**_construct(F01_TRADER))),
    PORTFOLIO_MANAGER: render_pm_decision(PortfolioDecision(**_construct(F01_PM))),
}


def test_t24_rendering_matches_baseline_renderer():
    for agent in (RESEARCH_MANAGER, TRADER, PORTFOLIO_MANAGER):
        result = run(agent, STATES[agent], structured=Scripted(VALID[agent][0]))
        assert result.rendered == _BASELINE_RENDERS[agent]
        assert result.payload is not None


def test_t24_strict_and_baseline_field_sets_match():
    for agent in (RESEARCH_MANAGER, TRADER, PORTFOLIO_MANAGER):
        strict = contracts.strict_schema_for(agent)
        baseline = contracts.baseline_schema_for(agent)
        assert set(strict.model_fields) == set(baseline.model_fields)


# ---------------------------------------------------------------------------
# T-25 — validate never semantically repairs
# ---------------------------------------------------------------------------

def test_t25_validate_does_not_repair():
    structured = Scripted(dict(F01_PM, executive_summary=""), F01_PM)
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_VALIDATE, structured=structured,
            recovery_prompt=lambda issues: "repair please")
    assert caught.value.record.calls.semantic_repair == 0
    assert structured.calls == 1  # only the structured attempt; the repair was never asked


# ---------------------------------------------------------------------------
# Review B regression: a parsed semantic violation is repairable in validate+repair
# ---------------------------------------------------------------------------

def test_semantic_violation_validate_rejects_after_one_call():
    """``validate`` stops on a parsed blank narrative: no repair, no fallback, one call."""
    structured = Scripted(dict(F01_RM, rationale="   "), F01_RM)
    plain = Scripted(plain_message(RESEARCH_MANAGER, F01_RM))
    with pytest.raises(AgentContractError) as caught:
        run(RESEARCH_MANAGER, RM_STATE, mode=MODE_VALIDATE, structured=structured,
            plain=plain, recovery_prompt=lambda issues: "repair please")
    calls = caught.value.record.calls
    assert (calls.structured, calls.semantic_repair, calls.free_text_recovery) == (1, 0, 0)
    assert calls.logical_total == 1
    assert structured.calls == 1 and plain.calls == 0
    assert caught.value.record.stage == contracts.Stage.STRUCTURED
    assert "rationale" in {issue.field for issue in caught.value.issues}


def test_semantic_violation_validate_repair_succeeds_after_two_calls():
    """A blank parsed rationale is repaired: the repair gate is not gated by ``parse_failure``."""
    structured = Scripted(dict(F01_RM, rationale="   "), F01_RM)
    plain = Scripted(plain_message(RESEARCH_MANAGER, F01_RM))
    prompts = []

    def repair(issues):
        prompts.append([i.code for i in issues])
        return "repair please"

    result = run(RESEARCH_MANAGER, RM_STATE, mode=MODE_VALIDATE_REPAIR, structured=structured,
                 plain=plain, recovery_prompt=repair)
    calls = result.record.calls
    assert (calls.structured, calls.semantic_repair, calls.free_text_recovery) == (1, 1, 0)
    assert calls.logical_total == 2
    assert structured.calls == 2 and plain.calls == 0
    assert result.record.stage == contracts.Stage.REPAIR
    assert prompts == [["blank"]]
    assert result.payload.rationale == "Bulls led on cash flow."


def test_semantic_violation_validate_repair_second_invalid_fails_after_two_calls():
    """A repair that is still semantically invalid ends the run at two calls."""
    structured = Scripted(dict(F01_RM, rationale="   "), dict(F01_RM, strategic_actions="\t"))
    plain = Scripted(plain_message(RESEARCH_MANAGER, F01_RM))
    with pytest.raises(AgentContractError) as caught:
        run(RESEARCH_MANAGER, RM_STATE, mode=MODE_VALIDATE_REPAIR, structured=structured,
            plain=plain, recovery_prompt=lambda issues: "repair please")
    calls = caught.value.record.calls
    assert (calls.structured, calls.semantic_repair, calls.free_text_recovery) == (1, 1, 0)
    assert calls.logical_total == 2
    assert structured.calls == 2 and plain.calls == 0
    assert "strategic_actions" in {issue.field for issue in caught.value.issues}


# ---------------------------------------------------------------------------
# T-26 — invalid numerics are sanitised in validate; off mode stays compatible
#
# ADR-002 section 4 supersedes the old strict-reject policy for *optional* numeric output
# fields: the field becomes absent with a diagnostic, the answer is accepted, and no extra
# model call is made. The off-mode half of every test below is unchanged.
# ---------------------------------------------------------------------------

_INVALID_NUMERICS = ["15%", "150-160", True, "around 150", ""]


@pytest.mark.parametrize("raw", _INVALID_NUMERICS)
def test_t26_strict_path_sanitizes_raw_invalid_numbers(raw):
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F01_TRADER, entry_price=raw)))
    assert result.payload.entry_price is None
    findings = [i for i in result.record.sanitized if i.field == "entry_price"]
    assert findings, "entry_price was not named"
    assert findings[0].code == contracts.SANITIZED_NUMBER_CODE
    assert result.record.calls.structured == 1 and result.record.calls.logical_total == 1
    # The record names the field and the reason, never the model's text (FR-6).
    assert result.record.canonical_json().find("15%") == -1
    assert result.record.canonical_json().find("150-160") == -1
    assert result.record.canonical_json().find("around 150") == -1


@pytest.mark.parametrize("raw", ["15%", "150-160", "around 150", ""])
def test_t26_off_mode_baseline_coercer_unchanged(raw):
    """Mode off is byte-compatible: the baseline schema still nulls these values."""
    proposal = TraderProposal(**dict(F01_TRADER, entry_price=raw))
    assert proposal.entry_price is None


def test_t26_off_mode_boolean_behaviour_is_unchanged():
    """Baseline Pydantic reads ``True`` as 1.0; validate refuses to read it as a level.

    Recorded as an observation of the untouched baseline, not as a contract guarantee. In
    an enhanced mode ``True`` is sanitised to absent (never 1.0) with a diagnostic
    (ADR-002 section 4).
    """
    assert TraderProposal(**dict(F01_TRADER, entry_price=True)).entry_price == 1.0
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F01_TRADER, entry_price=True)))
    assert result.payload.entry_price is None
    assert [i.field for i in result.record.sanitized] == ["entry_price"]


def test_t26_boolean_is_not_accepted_as_one():
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F01_TRADER, entry_price=True)))
    assert result.payload.entry_price is None
    assert result.record.sanitized[0].detail == contracts.REASON_BOOLEAN


def test_t26_none_is_legal_absence_in_both_modes():
    result = run(TRADER, TRADER_STATE, structured=Scripted(dict(F01_TRADER, entry_price=None)))
    assert result.payload.entry_price is None
    assert result.record.sanitized == []
    assert TraderProposal(**dict(F01_TRADER, entry_price=None)).entry_price is None


# ---------------------------------------------------------------------------
# T-28 — all required fields, on all agents and both output routes
# ---------------------------------------------------------------------------

_INVALID_FIELDS = [
    (RESEARCH_MANAGER, "recommendation", "StrongBuy"),
    (RESEARCH_MANAGER, "rationale", "   "),
    (RESEARCH_MANAGER, "strategic_actions", ""),
    (TRADER, "action", "Long"),
    (TRADER, "reasoning", "\t"),
    (PORTFOLIO_MANAGER, "rating", "Neutral"),
    (PORTFOLIO_MANAGER, "executive_summary", " "),
    (PORTFOLIO_MANAGER, "investment_thesis", ""),
]


@pytest.mark.parametrize("agent,field,bad", _INVALID_FIELDS)
def test_t28_required_field_structured_route(agent, field, bad):
    payload = dict(VALID[agent][0], **{field: bad})
    with pytest.raises(AgentContractError) as caught:
        run(agent, STATES[agent], structured=Scripted(payload))
    assert field in {issue.field for issue in caught.value.issues}
    assert caught.value.stage == contracts.Stage.STRUCTURED


@pytest.mark.parametrize("agent,field,bad", _INVALID_FIELDS)
def test_t28_required_field_free_text_route(agent, field, bad):
    payload = dict(VALID[agent][0], **{field: bad})
    structured = Scripted(None)
    with pytest.raises(AgentContractError) as caught:
        run(agent, STATES[agent], structured=structured,
            plain=Scripted(raw_message(raw_for(agent, payload))))
    assert field in {issue.field for issue in caught.value.issues}
    assert caught.value.stage == contracts.Stage.FREE_TEXT


def test_t28_optional_absence_never_fails():
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted({"rating": "Hold", "executive_summary": "s",
                                                                    "investment_thesis": "t"}))
    assert result.payload.price_target is None and result.payload.time_horizon is None
    assert validate_payload(PORTFOLIO_MANAGER, result.payload) == []


# ---------------------------------------------------------------------------
# Provider rejected the structured binding: one plain route, still validated
# ---------------------------------------------------------------------------

def test_rejected_structured_binding_uses_one_validated_plain_call():
    plain = Scripted(plain_message(PORTFOLIO_MANAGER, F01_PM))
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=None, plain=plain)
    assert plain.calls == 1
    calls = result.record.calls.as_dict()
    # One provider call, not counted as a recovery (there was nothing to recover from).
    assert calls["structured"] == 1 and calls["free_text_recovery"] == 0
    assert calls["logical_total"] == 1
    assert result.rendered == _BASELINE_RENDERS[PORTFOLIO_MANAGER]


def test_rejected_structured_binding_is_still_validated():
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=None,
            plain=Scripted(raw_message("**Rating**: Neutral\n\n**Executive Summary**: s\n\n**Investment Thesis**: t")))
    assert "rating" in {issue.field for issue in caught.value.issues}
    assert caught.value.record.calls.as_dict()["logical_total"] == 1


@pytest.mark.parametrize("mode", ["off", "OFF", " off ", "validate", "VALIDATE", "validate+repair", " Validate+Repair "])
def test_mode_names_are_case_and_space_insensitive(mode):
    assert contracts.normalize_mode(mode) in contracts.CONTRACT_MODES


@pytest.mark.parametrize("bad", ["on", "strict", "repair", "validate+", ""])
def test_unknown_modes_are_configuration_errors(bad):
    with pytest.raises(ValueError):
        contracts.normalize_mode(bad)


def test_off_mode_refuses_the_enhanced_helper():
    with pytest.raises(ValueError):
        run(PORTFOLIO_MANAGER, PM_STATE, mode=MODE_OFF, structured=Scripted(F01_PM))


# ---------------------------------------------------------------------------
# T-29 — transport and programming errors are classified, not repaired
# ---------------------------------------------------------------------------

class _FakeTransportError(Exception):
    """Stands in for a provider SDK transport error without importing a vendor package."""


_FakeTransportError.__name__ = "APIConnectionError"


def test_t29_transport_error_not_replaced_by_fallback():
    for mode in (MODE_VALIDATE, MODE_VALIDATE_REPAIR):
        structured = Scripted(_FakeTransportError("connection reset"))
        plain = Scripted(F01_PM)
        with pytest.raises(AgentContractError) as caught:
            run(PORTFOLIO_MANAGER, PM_STATE, mode=mode, structured=structured, plain=plain,
                recovery_prompt=lambda issues: "repair please", sdk_retry_budget=2)
        error = caught.value
        assert error.kind == contracts.FailureKind.TRANSPORT
        assert error.record.calls.semantic_repair == 0
        assert plain.calls == 0
        calls = error.record.calls.as_dict()
        assert calls["observed_http_attempts"] == "unknown"
        assert calls["sdk_retry_budget"] == "configured:2"


def test_t29_programming_error_is_classified():
    structured = Scripted(KeyError("missing"))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=structured, plain=Scripted(F01_PM))
    assert caught.value.kind == contracts.FailureKind.PROGRAMMING
    assert caught.value.record.calls.logical_total == 1


def test_t29_a_value_error_from_the_model_callable_is_programming():
    """A ValueError raised BY the model callable is not a parse miss (review item 2)."""
    structured = Scripted(ValueError("provider glue bug"))
    plain = Scripted(plain_message(PORTFOLIO_MANAGER, F01_PM))
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=structured, plain=plain)
    assert caught.value.kind == contracts.FailureKind.PROGRAMMING
    assert plain.calls == 0
    # The arbitrary message is not copied into the record.
    assert "provider glue bug" not in caught.value.record.canonical_json()


def test_t29_none_result_is_a_named_parse_miss_not_an_error_message():
    """A no-parsed-result answer is ``no_parsed_result``, not a broad ValueError excuse."""
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(None),
                 plain=Scripted(plain_message(PORTFOLIO_MANAGER, F01_PM)))
    assert result.record.calls.free_text_recovery == 1


def test_t29_mismatched_schema_at_bind_boundary_is_rejected():
    """A baseline-coerced model is not accepted as it stands (review item 3)."""
    coerced = TraderProposal(action="Buy", reasoning="r", entry_price="15%")  # baseline nulls it
    structured = Scripted(coerced)
    with pytest.raises(AgentContractError) as caught:
        run(TRADER, TRADER_STATE, structured=structured)
    assert "mismatched_schema" in {issue.code for issue in caught.value.issues}


def test_t29_real_aimessage_json_and_fenced_json_are_parsed():
    body = '{"rating": "Underweight", "executive_summary": "Trim.", "investment_thesis": "Margins.", "price_target": "$1,234.50"}'
    for text in (body, "```json\n" + body + "\n```"):
        result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(None), plain=Scripted(raw_message(text)))
        assert result.payload.rating.value == "Underweight"
        assert result.payload.price_target == 1234.5


def test_t29_duplicate_conflicting_field_is_rejected():
    text = "**Rating**: Buy\n\n**Executive Summary**: s\n\n**Investment Thesis**: t\n\n**Rating**: Sell"
    with pytest.raises(AgentContractError) as caught:
        run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(None), plain=Scripted(raw_message(text)))
    assert "duplicate_field" in {issue.code for issue in caught.value.issues}


class _RawMessage:
    """A provider result with an arbitrary ``content``, for content shapes AIMessage forbids."""

    def __init__(self, content):
        self.content = content


def test_t29_malformed_and_nontext_content_fails_closed():
    for content in ("", {"weird": 1}, [{"type": "tool_use", "name": "search"}], None):
        with pytest.raises(AgentContractError) as caught:
            run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(None),
                plain=Scripted(_RawMessage(content)))
        assert caught.value.stage == contracts.Stage.FREE_TEXT


def test_t29_multiline_narrative_is_preserved():
    text = ("**Rating**: Hold\n\n**Executive Summary**: First line.\n\nSecond paragraph stays.\n\n"
            "**Investment Thesis**: Thesis line one.\n\n- bullet kept\n- second bullet")
    result = run(PORTFOLIO_MANAGER, PM_STATE, structured=Scripted(None), plain=Scripted(raw_message(text)))
    assert "Second paragraph stays." in result.payload.executive_summary
    assert "- second bullet" in result.payload.investment_thesis


def test_t29_plain_numeric_label_is_sanitized_before_coercion():
    """ADR-002 section 4: a plain ``15%`` becomes absent, not a rejection of the answer."""
    text = "**Action**: Buy\n\n**Reasoning**: r\n\n**Entry Price**: 15%"
    result = run(TRADER, TRADER_STATE, structured=Scripted(None), plain=Scripted(raw_message(text)))
    assert result.payload.entry_price is None
    assert result.payload.reasoning == "r"
    # The report no longer shows the level, and the record still names it with the reason,
    # even when no field-level finding is attached (the plain parser already dropped it).
    assert "**Entry Price**: not provided" in result.rendered
    findings = [i for i in result.record.sanitized if i.field == "entry_price"]
    assert findings and findings[0].code == contracts.SANITIZED_NUMBER_CODE
    assert result.record.calls.free_text_recovery == 1
    assert result.record.calls.semantic_repair == 0


def test_t29_invalid_number_contract_error_is_classified():
    strict = StrictTraderProposal(action="Buy", reasoning="ok", entry_price=1.0)
    issue = ContractIssue("invalid_number", "entry_price", "nonfinite")
    error = contracts.AgentContractError(TRADER, contracts.Stage.STRUCTURED,
                                         contracts.FailureKind.VALIDATION, [issue])
    assert contracts.classify_exception(error) == contracts.FailureKind.VALIDATION
    assert strict.entry_price == 1.0
