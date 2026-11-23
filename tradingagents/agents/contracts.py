"""Input/output contracts for the Research Manager, Trader and Portfolio Manager.

Implements ADR-001 (`docs/architecture/adr/001-agent-io-contracts.md`) sections 2-3 and the
requirements FR-1..FR-12, INV-1..INV-12 in `docs/req/requirements.yaml`.

Scope of this module (increment 1): the pure contract layer only. It provides the typed
failure, the strict payload schemas used by enhanced modes, input checks, cross-field
consistency checks, the stable error record and the accounting record. It does not call a
model and it does not decide graph flow; the typed abort is raised here and handled by the
callers wired in a later increment (ADR section 7, steps 5-7).

Two behaviours are deliberate and load-bearing:

* The strict payload schemas do NOT inherit the permissive ``_coerce_optional_float`` in
  ``tradingagents.agents.schemas`` (FR-3). That coercer turns ``"15%"``, ``"150-160"`` and
  ``"N/A"`` into ``None`` so a weak model's output still validates (#1058, #1288). Enhanced
  modes must instead read such a value as invalid, so it cannot be told apart from a field
  the model legitimately omitted. Mode ``off`` keeps the permissive behaviour untouched.
* Only parse and validation problems are recoverable (ADR section 2.5). Transport and
  programming errors are classified and surfaced; they are never converted into a
  successful fallback.

The strict schemas use no ``field_validator`` that repairs a value. Every field is validated
by Pydantic's own type rules plus ``StrictFloat | None``, so they can be handed to
``with_structured_output`` unchanged (they are ordinary ``BaseModel`` subclasses).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# LangChain's parser exception is the shape a provider raises inside ``invoke``.
# ``langchain_core`` is a hard dependency of this package, so it is imported directly
# instead of being guarded: a guarded import would leave the name bound to ``None``, and a
# provider parse failure would then be classified as a programming error (T-29).
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
    render_pm_decision,
    render_research_plan,
    render_trader_proposal,
)

# Bumping this invalidates saved checkpoints whose payloads predate the current contract.
# It participates in the run signature together with the mode (ADR section 2.10, NFR-7). The
# signature wiring lands with the graph increment; until then this constant is the single
# source of truth the later increment must read.
AGENT_CONTRACT_VERSION = "1"

MODE_OFF = "off"
MODE_VALIDATE = "validate"
MODE_VALIDATE_REPAIR = "validate+repair"
CONTRACT_MODES: tuple[str, ...] = (MODE_OFF, MODE_VALIDATE, MODE_VALIDATE_REPAIR)


def normalize_mode(value: Any) -> str:
    """Return a validated contract mode name, or raise ``ValueError``.

    Kept here so the config key and every caller share one vocabulary; an unknown mode is a
    configuration error, not something to guess around.
    """
    if value is None:
        return MODE_OFF
    text = str(value).strip().lower()
    if text not in CONTRACT_MODES:
        raise ValueError(
            f"agent_contract_mode must be one of {', '.join(CONTRACT_MODES)}, got {value!r}"
        )
    return text


def mode_enabled(mode: Any) -> bool:
    """Whether contract checks run in ``mode`` (FR-1, FR-2)."""
    return normalize_mode(mode) != MODE_OFF


def mode_allows_repair(mode: Any) -> bool:
    """Whether a semantic repair call is permitted (ADR section 2.3)."""
    return normalize_mode(mode) == MODE_VALIDATE_REPAIR


# ---------------------------------------------------------------------------
# Failure marker and typed failure
# ---------------------------------------------------------------------------

# A plan field equal to this is a failure notice, never content (ADR section 3, T-4).
FAILURE_MARKER_PREFIX = "[AGENT CONTRACT VALIDATION FAILED"


def is_failure_marker(value: Any) -> bool:
    """Whether ``value`` is a contract failure notice rather than a usable plan."""
    return isinstance(value, str) and value.strip().startswith(FAILURE_MARKER_PREFIX)


class Stage(StrEnum):
    """Where a contract failed; the record carries it so a reader knows the cause."""

    INPUT = "input"
    STRUCTURED = "structured"
    FREE_TEXT = "free_text"
    REPAIR = "repair"


class FailureKind(StrEnum):
    """Classification of a failure (ADR section 2.5).

    Only ``PARSE`` and ``VALIDATION`` are recoverable. ``TRANSPORT`` and ``PROGRAMMING`` are
    surfaced: they are not hidden behind a fallback, and they never count as a recovery.
    """

    PARSE = "parse"
    VALIDATION = "validation"
    TRANSPORT = "transport"
    PROGRAMMING = "programming"


RECOVERABLE_KINDS = frozenset({FailureKind.PARSE, FailureKind.VALIDATION})


@dataclass(frozen=True)
class ContractIssue:
    """One deterministic field-level finding, with a redacted detail string.

    ``code`` is a stable machine key; ``detail`` never echoes model text beyond a short,
    neutral description, so a log line cannot leak a prompt or a secret (FR-6).
    """

    code: str
    field: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "detail": self.detail}


class AgentContractError(RuntimeError):
    """Typed abort raised when a contract cannot be satisfied.

    Carries the agent, the stage and the ordered issues so the narrow outer handler can
    write a stable failure audit (ADR section 2.6, 2.8) instead of substituting a value.
    """

    def __init__(
        self,
        agent: str,
        stage: Stage | str,
        kind: FailureKind | str,
        issues: list[ContractIssue],
        record: ValidationRecord | None = None,
    ):
        self.agent = agent
        self.stage = Stage(stage)
        self.kind = FailureKind(kind)
        self.issues = list(issues)
        self.record = record
        summary = "; ".join(f"{i.field}:{i.code}" for i in self.issues) or "no issue detail"
        super().__init__(f"{agent} contract failed at {self.stage.value} ({self.kind.value}): {summary}")


# ---------------------------------------------------------------------------
# Numeric parsing (FR-3, T-6, T-7, T-26)
# ---------------------------------------------------------------------------

# The permissive baseline drops a formatted price down to its number
# (``_coerce_optional_float`` in schemas.py). The strict path accepts the same explicit
# formatting, but rejects anything with more than one number or any signed-difference
# wording, so a range or a hedge cannot slip through as a single level.
_CURRENCY_SIGNS = frozenset({chr(36), chr(0x20AC), chr(0xA3), chr(0xA5)})
_FORMATTED_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:,\d{3})*|\d*)(?:\.\d+)?$")
_NULLISH = frozenset({"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"})


def parse_strict_number(value: Any, field_name: str) -> float | None:
    """Strict reading of an *optional* numeric field, or raise ``ValueError``.

    Accepted: a real number that is finite; an explicit numeric string (``"189.5"``,
    ``"$1,234.50"``, ``"-3.5"``); ``None``, the field being omitted.

    Rejected, and deliberately not turned into ``None``: booleans, NaN, infinities,
    percentages, ranges, hedges, empty strings and non-numeric placeholders. A negative
    number is accepted: no domain evidence supports a universal sign rule (FR-3).

    ``field_name`` is used only in the error message, which stays short and does not echo
    model content beyond the value's type (FR-6).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name}: boolean is not a number")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field_name}: nonfinite number")
        return number
    if not isinstance(value, str):
        raise ValueError(f"{field_name}: unsupported type {type(value).__name__}")
    text = value.strip()
    if text.lower() in _NULLISH:
        raise ValueError(f"{field_name}: placeholder or empty value is not a number")
    cleaned = text.replace(",", "")
    if cleaned[:1] in _CURRENCY_SIGNS:
        cleaned = cleaned[1:].strip()
    if not _FORMATTED_NUMBER_RE.match(cleaned) or cleaned in ("", "+", "-", "."):
        raise ValueError(f"{field_name}: not a single unambiguous number")
    number = float(cleaned)
    if not math.isfinite(number):
        raise ValueError(f"{field_name}: nonfinite number")
    return number


# Placeholders a model writes to mean "I am omitting this optional field". They are an
# absence, not a number, and enhanced modes accept them as ``None`` exactly because the
# model stated no level; an empty string, a hedge and a range are different: those are
# inputs that failed to be a number and must be rejected (T-26).
_OMISSION_PLACEHOLDERS = frozenset({"none", "null", "nil", "n/a", "na", "tbd", "unknown", "-"})


def _strict_optional_float(value: Any) -> float | None:
    """Schema-level wrapper around :func:`parse_strict_number`.

    Only explicit omission placeholders become ``None``. Everything else is parsed strictly,
    and a rejected value raises so Pydantic reports a validation error rather than silently
    accepting the payload (FR-3, T-26).
    """
    if isinstance(value, str) and value.strip().lower() in _OMISSION_PLACEHOLDERS:
        return None
    return parse_strict_number(value, "value")


# ---------------------------------------------------------------------------
# Strict payload schemas used by enhanced modes
# ---------------------------------------------------------------------------

_STRICT_CONFIG = ConfigDict(extra="forbid")


class StrictResearchPlan(BaseModel):
    """``ResearchPlan`` with strict numerics and extra-field rejection (FR-2, FR-3).

    Same three fields as ``schemas.ResearchPlan`` so a provider can be given either model;
    the difference is that nothing here repairs a value. A blank narrative is left for the
    contract to report as an issue, so the failure says which field was blank (FR-6).
    """

    model_config = _STRICT_CONFIG

    recommendation: PortfolioRating = Field(
        description="Exactly one of Buy / Overweight / Hold / Underweight / Sell."
    )
    rationale: str = Field(description="Which arguments decided the recommendation.")
    strategic_actions: str = Field(description="Concrete steps for the trader.")


class StrictTraderProposal(BaseModel):
    """``TraderProposal`` with strict numerics (FR-3).

    An optional level may be omitted or written as an explicit placeholder ("N/A"); a
    percentage, a range, a hedge, a boolean or a nonfinite number is a validation error
    rather than a silent ``None``. No sign rule is applied.
    """

    model_config = _STRICT_CONFIG

    action: TraderAction = Field(description="Exactly one of Buy / Hold / Sell.")
    reasoning: str = Field(description="The case for this action, two to four sentences.")
    entry_price: float | None = Field(
        default=None,
        description=(
            "Optional entry price as an absolute number in the instrument's quote currency "
            "(e.g. 189.5), never a percentage or a range."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description="Optional stop-loss as an absolute price, never a percentage.",
    )
    position_sizing: str | None = Field(default=None, description="Optional sizing guidance.")

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _strict_floats(cls, v):
        return _strict_optional_float(v)


class StrictPortfolioDecision(BaseModel):
    """``PortfolioDecision`` with strict numerics (FR-3). Mirrors ``StrictTraderProposal``."""

    model_config = _STRICT_CONFIG

    rating: PortfolioRating = Field(
        description="Exactly one of Buy / Overweight / Hold / Underweight / Sell."
    )
    executive_summary: str = Field(description="The call and how to act on it.")
    investment_thesis: str = Field(description="The evidence that decided it.")
    price_target: float | None = Field(
        default=None, description="Optional target price in the instrument's quote currency."
    )
    time_horizon: str | None = Field(default=None, description="Optional holding period.")

    @field_validator("price_target", mode="before")
    @classmethod
    def _strict_floats(cls, v):
        return _strict_optional_float(v)


# ---------------------------------------------------------------------------
# Stable error record and call accounting (FR-6, FR-10, INV-5, NFR-3)
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    """What this agent actually asked the provider for, and what it is allowed to ask.

    ``sdk_retry_budget`` is the configured ``llm_max_retries`` value as written; when unset
    it reports that the provider's own default applies and is not assumed. Transport retries
    happen inside the provider SDK, so ``observed_http_attempts`` is the string ``"unknown"``
    and no attempt count is invented (FR-10, NFR-3).
    """

    structured: int = 0
    free_text_recovery: int = 0
    semantic_repair: int = 0
    # Not part of the approved six-key record (T-13): a call on the plain route when the
    # provider rejected the structured binding. It is exposed here only so accounting can
    # explain the total honestly, and is excluded from ``logical_total`` of the record.
    primary_plain: int = 0
    sdk_retry_budget: Any = None
    observed_http_attempts: str = "unknown"

    @property
    def logical_total(self) -> int:
        return self.structured + self.free_text_recovery + self.semantic_repair

    def as_dict(self) -> dict[str, Any]:
        """Ordered, timestamp-free mapping: equal inputs give byte-identical records (INV-5).

        Exactly the six keys the test specification requires (T-13). ``primary_plain`` is
        folded into ``structured`` so the sum still equals the calls a provider saw.
        """
        return {
            "structured": self.structured + self.primary_plain,
            "free_text_recovery": self.free_text_recovery,
            "semantic_repair": self.semantic_repair,
            "logical_total": self.logical_total + self.primary_plain,
            "sdk_retry_budget": describe_retry_budget(self.sdk_retry_budget),
            "observed_http_attempts": self.observed_http_attempts,
        }


def describe_retry_budget(value: Any) -> str:
    """The configured SDK retry budget, or a note that no default is assumed (FR-10)."""
    if value is None or value == "":
        return "provider_default_not_assumed"
    return f"configured:{value}"


@dataclass
class ValidationRecord:
    """The stable, ordered record of one agent's contract outcome (FR-6, INV-5)."""

    agent: str
    mode: str
    stage: Stage
    ok: bool
    issues: list[ContractIssue] = field(default_factory=list)
    informational: list[ContractIssue] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    calls: CallRecord = field(default_factory=CallRecord)

    def __post_init__(self) -> None:
        # Normalise here so every reader of ``stage`` sees a real ``Stage`` member, even a
        # record built from a plain string (``Stage("input")``); serialisation below and
        # the summary lines then cannot disagree about the recorded stage.
        self.stage = Stage(self.stage)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "mode": self.mode,
            "stage": self.stage.value,
            "ok": self.ok,
            "failures": [issue.as_dict() for issue in self.issues],
            "informational": [issue.as_dict() for issue in self.informational],
            "skipped_checks": list(self.skipped),
            "calls": self.calls.as_dict(),
            "contract_version": AGENT_CONTRACT_VERSION,
        }

    def canonical_json(self) -> str:
        """Deterministic serialisation used to assert record stability (INV-5, T-14)."""
        return json.dumps(self.as_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Per-agent contracts (ADR section 3)
# ---------------------------------------------------------------------------

RESEARCH_MANAGER = "Research Manager"
TRADER = "Trader"
PORTFOLIO_MANAGER = "Portfolio Manager"

# Analyst reports are optional inputs for all three agents: absent, None or a blank string is
# valid, and no analyst selection is required (INV-6, T-2).
OPTIONAL_ANALYST_REPORTS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
)


def blank_text_issue(name: str, value: Any) -> ContractIssue | None:
    """A required narrative that is not a string, or is blank after stripping (T-5)."""
    if not isinstance(value, str):
        return ContractIssue("not_a_string", name, f"expected str, got {type(value).__name__}")
    if not value.strip():
        return ContractIssue("blank", name, "required text is blank")
    return None


@dataclass(frozen=True)
class PayloadContract:
    """Declarative shape of one agent's output, checked for both schemas and free text."""

    agent: str
    required_enum: str
    required_text: tuple[str, ...]
    optional_numeric: tuple[str, ...]


CONTRACTS: dict[str, PayloadContract] = {
    RESEARCH_MANAGER: PayloadContract(RESEARCH_MANAGER, "recommendation", ("rationale", "strategic_actions"), ()),
    TRADER: PayloadContract(TRADER, "action", ("reasoning",), ("entry_price", "stop_loss")),
    PORTFOLIO_MANAGER: PayloadContract(PORTFOLIO_MANAGER, "rating", ("executive_summary", "investment_thesis"), ("price_target",)),
}


def contract_for(agent: str) -> PayloadContract:
    try:
        return CONTRACTS[agent]
    except KeyError:  # pragma: no cover - guarded by the agent factories
        raise ValueError(f"unknown agent for a contract: {agent!r}") from None


# ---------------------------------------------------------------------------
# Input validation (FR-1, FR-7, INV-3, INV-6)
# ---------------------------------------------------------------------------


def _text_input_issue(name: str, value: Any) -> ContractIssue | None:
    """A plan handed to the next agent: present, a string, nonblank and not a marker."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ContractIssue("missing_required_input", name, "required input is absent or blank")
    issue = blank_text_issue(name, value)
    if issue is not None:
        return ContractIssue("wrong_input_type", name, issue.detail)
    if is_failure_marker(value):
        return ContractIssue("failure_marker_input", name, "a failed plan cannot be consumed")
    return None


def _identity_input_issue(value: Any) -> ContractIssue | None:
    """The instrument identity, which the Trader and Portfolio Manager index directly.

    The nodes read ``state["company_of_interest"]`` without a default, so its absence would
    surface as a ``KeyError`` inside the node. In an enhanced mode it is reported as an input
    issue instead, costing zero model calls (review item C).
    """
    if not isinstance(value, str) or not value.strip():
        return ContractIssue("missing_required_input", "company_of_interest",
                             "the instrument identity is required")
    return None


def _optional_text_issue(name: str, value: Any) -> ContractIssue | None:
    """Context a node reads only to render it: absent, ``None`` or blank is fine (T-3).

    Only a value that could not be interpolated into a prompt is a problem, so an absent
    report never blocks a run while a non-text one is surfaced as an input issue.
    """
    if value is None or isinstance(value, str):
        return None
    return ContractIssue("wrong_input_type", name,
                         f"optional context must be text, got {type(value).__name__}")


def validate_research_manager_input(state: Mapping[str, Any]) -> list[ContractIssue]:
    """What the Research Manager reads: the debate history and its turn count (T-3).

    The market report is optional context (absent, ``None`` or blank is fine), but a value
    of the wrong type cannot be rendered into a prompt.
    """
    issues: list[ContractIssue] = []
    debate = state.get("investment_debate_state")
    if not isinstance(debate, Mapping):
        return [ContractIssue("missing_required_input", "investment_debate_state",
                              "an investment debate state is required")]
    history = debate.get("history")
    count = debate.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        issues.append(ContractIssue("wrong_input_type", "investment_debate_state.count",
                                    "a turn count is required"))
    elif not isinstance(history, str):
        issues.append(ContractIssue("missing_required_input", "investment_debate_state.history",
                                    "a debate history string is required"))
    elif count > 0 and not history.strip():
        issues.append(ContractIssue("blank", "investment_debate_state.history",
                                    "a debate that has spoken must leave a history"))
    issue = _optional_text_issue("market_report", state.get("market_report"))
    if issue is not None:
        issues.append(issue)
    return issues


def validate_trader_input(state: Mapping[str, Any]) -> list[ContractIssue]:
    """What the Trader reads: the research plan, plus the instrument identity (T-2, T-3).

    The four analyst reports are optional context; when present they must be renderable.
    """
    issues: list[ContractIssue] = []
    for name, agent in (("investment_plan", RESEARCH_MANAGER),):
        # The saved research plan is revalidated in full before the Trader consumes it.
        issue = revalidate_saved_plan(agent, state.get(name), name)
        if issue is not None:
            issues.append(issue)
    # Identity and reports are prompt context: absent is tolerated in the contract layer; a
    # value that could not be interpolated is still an input issue.
    issue = _optional_text_issue("company_of_interest", state.get("company_of_interest"))
    if issue is not None:
        issues.append(issue)
    for name in ("market_report", "sentiment_report", "news_report", "fundamentals_report"):
        issue = _optional_text_issue(name, state.get(name))
        if issue is not None:
            issues.append(issue)
    return issues


def validate_portfolio_manager_input(state: Mapping[str, Any]) -> list[ContractIssue]:
    """What the Portfolio Manager reads: the risk history, both plans and its own inputs (T-3).

    ``company_of_interest`` and every analyst report are read for the instrument context and
    the research digest; the reports are optional, the identity is not.
    """
    issues: list[ContractIssue] = []
    risk = state.get("risk_debate_state")
    if not isinstance(risk, Mapping):
        issues.append(ContractIssue("missing_required_input", "risk_debate_state",
                                    "a risk debate state is required"))
    else:
        history = risk.get("history")
        if not isinstance(history, str) or not history.strip():
            issues.append(ContractIssue("missing_required_input", "risk_debate_state.history",
                                        "a risk debate history is required"))
        for name in ("aggressive_history", "conservative_history", "neutral_history",
                     "current_aggressive_response", "current_conservative_response",
                     "current_neutral_response"):
            issue = _optional_text_issue(f"risk_debate_state.{name}", risk.get(name))
            if issue is not None:
                issues.append(issue)
        count = risk.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
            issues.append(ContractIssue("wrong_input_type", "risk_debate_state.count",
                                        "the turn count must be an integer"))
    for name, agent in (("investment_plan", RESEARCH_MANAGER),
                        ("trader_investment_plan", TRADER)):
        # Both saved plans are revalidated in full, not just for an unknown label.
        issue = revalidate_saved_plan(agent, state.get(name), name)
        if issue is not None:
            issues.append(issue)
    # The identity is read by the node, not by this layer; the node checks it itself (C).
    issue = _optional_text_issue("company_of_interest", state.get("company_of_interest"))
    if issue is not None:
        issues.append(issue)
    for name in ("market_report", "sentiment_report", "news_report", "fundamentals_report",
                 "past_context"):
        issue = _optional_text_issue(name, state.get(name))
        if issue is not None:
            issues.append(issue)
    return issues


INPUT_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], list[ContractIssue]]] = {
    RESEARCH_MANAGER: validate_research_manager_input,
    TRADER: validate_trader_input,
    PORTFOLIO_MANAGER: validate_portfolio_manager_input,
}


def validate_input(agent: str, state: Mapping[str, Any], mode: Any = MODE_VALIDATE,
                   calls: CallRecord | None = None) -> None:
    """Raise ``AgentContractError`` before any model call when ``state`` cannot be read.

    The caller must invoke this first so an invalid input costs zero model calls (FR-7,
    INV-3). In mode ``off`` nothing is checked, which keeps baseline behaviour byte-identical
    (INV-7).
    """
    normalized = normalize_mode(mode)
    if normalized == MODE_OFF:
        return
    issues = INPUT_VALIDATORS[agent](state)
    if issues:
        record = ValidationRecord(agent, normalized, Stage.INPUT, False, list(issues), calls=calls or CallRecord())
        raise AgentContractError(agent, Stage.INPUT, FailureKind.VALIDATION, issues, record)


# ---------------------------------------------------------------------------
# Output validation, on schemas and on free text (FR-2, T-5, T-28)
# ---------------------------------------------------------------------------


def _payload_value(payload: Any, name: str) -> Any:
    if isinstance(payload, BaseModel):
        return getattr(payload, name, None)
    if isinstance(payload, Mapping):
        return payload.get(name)
    return getattr(payload, name, None)


def validate_payload(agent: str, payload: Any) -> list[ContractIssue]:
    """Every required field of ``payload``, checked the same way for all routes (FR-2).

    Works on a parsed strict model, a baseline model, or a plain mapping produced by a
    compatibility free-text recovery, so no route can skip the required-field check.
    """
    contract = contract_for(agent)
    issues: list[ContractIssue] = []
    enum_field = contract.required_enum
    raw_enum = _payload_value(payload, enum_field)
    allowed = {rating.value for rating in PortfolioRating} if agent != TRADER else {a.value for a in TraderAction}
    enum_value = getattr(raw_enum, "value", raw_enum)
    if enum_value not in allowed:
        issues.append(ContractIssue("invalid_enum", enum_field, "not one of the allowed values"))
    for name in contract.required_text:
        issue = blank_text_issue(name, _payload_value(payload, name))
        if issue is not None:
            issues.append(issue)
    for name in contract.optional_numeric:
        raw = _payload_value(payload, name)
        if raw is None:
            continue
        try:
            parse_strict_number(raw, name)
        except ValueError as exc:
            issues.append(ContractIssue("invalid_number", name, str(exc)))
    return issues


def render_payload(agent: str, payload: Any) -> str:
    """Render a validated payload with the baseline renderer (FR-11, T-24).

    Reusing one renderer per agent is what keeps markdown byte-identical between modes.
    """
    if agent == RESEARCH_MANAGER:
        return render_research_plan(payload)
    if agent == TRADER:
        return render_trader_proposal(payload)
    return render_pm_decision(payload)


# ---------------------------------------------------------------------------
# Consistency checks: informational, and skipped when context is absent (FR-4, T-8, T-9)
# ---------------------------------------------------------------------------

_RECOMMENDATION_TO_ACTION = {
    PortfolioRating.BUY.value: TraderAction.BUY.value,
    PortfolioRating.OVERWEIGHT.value: TraderAction.BUY.value,
    PortfolioRating.HOLD.value: TraderAction.HOLD.value,
    PortfolioRating.UNDERWEIGHT.value: TraderAction.SELL.value,
    PortfolioRating.SELL.value: TraderAction.SELL.value,
}


def check_trader_against_plan(plan: Any, action: Any) -> tuple[list[ContractIssue], list[str]]:
    """Compare the Trader's action with the plan's recommendation: informational only (T-8).

    A mismatch is recorded, never a failure: the prompt already allows sizing nuance
    (``tradingagents/agents/trader/trader.py:76-78``). When the plan carries no readable
    recommendation the check is ``skipped`` rather than passed (T-9).
    """
    recommendation = extract_plan_recommendation(plan)
    if recommendation is None:
        return [], ["trader.action_vs_recommendation"]
    action_value = getattr(action, "value", action)
    if action_value == _RECOMMENDATION_TO_ACTION.get(recommendation):
        return [], []
    return (
        [ContractIssue(
            "action_disagrees_with_recommendation",
            "action",
            f"recommendation {recommendation} maps to {_RECOMMENDATION_TO_ACTION[recommendation]}, "
            f"trader action is {action_value}",
        )],
        [],
    )


_PLAN_RECOMMENDATION_RE = re.compile(
    r"^[\s*_#\-]*recommendation[\s*_]*[:\-][\s*]*(\w+)", re.IGNORECASE | re.MULTILINE
)


def extract_plan_recommendation(plan: Any) -> str | None:
    """The recommendation a rendered plan states, or ``None`` when it states none.

    A structural read of the baseline render's ``**Recommendation**: X`` line
    (``tradingagents/agents/schemas.py:133``), not a reading of the prose around it.
    """
    if isinstance(plan, BaseModel):
        value = getattr(plan, "recommendation", None)
        return getattr(value, "value", value)
    if not isinstance(plan, str):
        return None
    match = _PLAN_RECOMMENDATION_RE.search(unicodedata.normalize("NFKC", plan))
    if not match:
        return None
    candidate = match.group(1).capitalize()
    return candidate if candidate in _RECOMMENDATION_TO_ACTION else None


# ---------------------------------------------------------------------------
# Failure classification (FR-10, T-29)
# ---------------------------------------------------------------------------

# Exceptions raised by a provider SDK / HTTP stack. They are transport problems: the provider
# SDK's own retry budget may have already run, and nothing here is allowed to turn them into
# an accepted fallback (ADR section 2.5). Matched by class *name* so no provider package is
# imported by this module, which keeps the data/vendor layering intact (NFR-6, SC-02).
_TRANSPORT_ERROR_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError",
    "ServiceUnavailableError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "RemoteProtocolError", "TimeoutException", "TransportError", "ConnectionError",
})
_TRANSPORT_MODULE_MARKERS = ("httpx", "httpcore", "openai", "anthropic", "google.api_core",
                             "requests", "urllib3", "botocore")


class ParseFailure(Exception):
    """A local parse problem: the provider answered, but the answer is not the contract.

    Raised only by this module's own parsing helpers, so it is the *only* ``Exception``
    kind that counts as recoverable alongside a Pydantic ``ValidationError``. A bare
    ``ValueError`` raised inside a scripted or provider callable is therefore never
    mistaken for a parse miss (T-29).
    """

    def __init__(self, issues: list[ContractIssue]):
        self.issues = list(issues)
        super().__init__("; ".join(f"{i.field}:{i.code}" for i in self.issues))


def classify_exception(exc: BaseException) -> FailureKind:
    """Classify a non-success outcome (T-29).

    Three parse failures are recoverable because each means "the model's answer could not be
    read", not "the program is broken": a Pydantic ``ValidationError``, this module's own
    ``ParseFailure``, and LangChain's ``OutputParserException`` (the exception a provider or
    a parser raises when tool-call JSON cannot be parsed). A bare ``ValueError`` or
    ``KeyError`` -- including one raised *inside* a model callable -- is a programming error:
    it is surfaced, never turned into a fallback, and its message is never copied into a
    record (review item F).
    """
    if isinstance(exc, AgentContractError):
        return exc.kind
    if isinstance(exc, (ValidationError, ParseFailure, OutputParserException)):
        return FailureKind.PARSE
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSPORT_ERROR_NAMES:
            return FailureKind.TRANSPORT
        module = getattr(klass, "__module__", "") or ""
        if any(marker in module for marker in _TRANSPORT_MODULE_MARKERS):
            return FailureKind.TRANSPORT
    return FailureKind.PROGRAMMING


def redact_detail(exc: BaseException) -> str:
    """A short, content-free description of an exception, safe for a record or a log.

    Never echoes model text, request bodies or header values: only the class and, for a
    Pydantic error, the number of problems and the field names (FR-6).
    """
    if isinstance(exc, ValidationError):
        fields = "/".join(
            ".".join(str(part) for part in error.get("loc", ())) or "?"
            for error in exc.errors()[:5]
        )
        return f"{type(exc).__name__}: {len(exc.errors())} problem(s) at {fields}"
    return type(exc).__name__



# ---------------------------------------------------------------------------
# Compatibility between the strict and baseline schemas (FR-11, INV-7, T-24)
# ---------------------------------------------------------------------------

# A validated strict payload is rendered by the baseline renderer, which is typed against
# the baseline model. The conversion copies fields under the strict schema's guarantees
# instead of re-validating through the baseline schema: re-validating would run the
# permissive coercer again on values the strict contract already approved (FR-3, INV-7).
_BASELINE_SCHEMAS: dict[str, type[BaseModel]] = {
    RESEARCH_MANAGER: ResearchPlan,
    TRADER: TraderProposal,
    PORTFOLIO_MANAGER: PortfolioDecision,
}
_STRICT_SCHEMAS: dict[str, type[BaseModel]] = {
    RESEARCH_MANAGER: StrictResearchPlan,
    TRADER: StrictTraderProposal,
    PORTFOLIO_MANAGER: StrictPortfolioDecision,
}


def baseline_schema_for(agent: str) -> type[BaseModel]:
    """The schema `off` mode binds today, unchanged (INV-7)."""
    return _BASELINE_SCHEMAS[agent]


def strict_schema_for(agent: str) -> type[BaseModel]:
    """The strict schema enhanced modes bind (a plain BaseModel for the provider)."""
    return _STRICT_SCHEMAS[agent]


def to_baseline_payload(agent: str, payload: BaseModel) -> BaseModel:
    """Copy a validated strict payload into the baseline model for rendering (T-24).

    `model_construct` bypasses validation deliberately, for the reason above. The strict
    and baseline field sets are asserted equal in `tests/test_agent_contracts.py`, so the
    copy cannot silently drop a field.
    """
    baseline = baseline_schema_for(agent)
    values = {name: getattr(payload, name) for name in baseline.model_fields}
    return baseline.model_construct(**values)


# ---------------------------------------------------------------------------
# Bounded invocation with at most one recovery (FR-5, FR-9, INV-4, NFR-2)
# ---------------------------------------------------------------------------


@dataclass
class EnhancedInvocation:
    """Outcome of one enhanced-mode invocation.

    `payload` is the validated strict model; `rendered` is the markdown for storage,
    produced by the baseline renderer so it is byte-identical to `off` output for the same
    content (T-24); `record` is the stable accounting record (T-13, T-14).
    """

    payload: BaseModel
    rendered: str
    record: ValidationRecord


def _issues_from_validation_error(exc: ValidationError) -> list[ContractIssue]:
    """Stable, ordered findings from a Pydantic parse failure, without echoing input.

    The reason comes from the strict parser, not from Pydantic's rendering, so a record
    reads the same for every provider and never quotes the value (FR-6, T-6).
    """
    issues: list[ContractIssue] = []
    for error in exc.errors():
        field_name = ".".join(str(part) for part in error.get("loc", ())) or "payload"
        pydantic_type = error.get("type", "invalid")
        if pydantic_type in {"value_error", "assertion_error"}:
            message = str(error.get("ctx", {}).get("error", "")) or "value did not validate"
            # The strict validator's message already names the field ("entry_price:
            # nonfinite number"); suffix it so the field appears once in the record.
            prefix = f"{field_name}: "
            if message.startswith(prefix):
                message = message[len(prefix):]
            issues.append(ContractIssue("invalid_value", field_name, message))
        else:
            issues.append(ContractIssue(pydantic_type, field_name, "value did not validate"))
    return issues


def _coerce_structured_result(agent: str, result: Any) -> BaseModel:
    """Turn a structured-output result into the strict schema, or raise a parse problem.

    The provider contract for ``with_structured_output(strict_schema)`` is a strict-schema
    instance. An explicit ``None`` is the documented "thinking model answered in text
    instead of calling the tool" case (``structured.py:70-74``), reported as
    ``no_parsed_result`` rather than as a generic error. A mapping is accepted because some
    providers (Gemini response-schema mode) return one; it is validated by the strict
    schema, which is where an invalid numeric is rejected. An instance of a schema that is
    not the bound strict schema is rejected outright: the provider was not asked for it,
    and its fields may already be permissively coerced, so it is **not** treated as
    preserving raw data.
    """
    strict = strict_schema_for(agent)
    if result is None:
        raise ParseFailure([ContractIssue("no_parsed_result", "payload",
                                          "the structured call returned no parsed result")])
    if isinstance(result, BaseModel) and not isinstance(result, strict):
        raise ParseFailure([ContractIssue("mismatched_schema", "payload",
                                          "the structured call returned an unexpected schema")])
    if isinstance(result, BaseModel):
        result = result.model_dump()
    if not isinstance(result, Mapping):
        raise ParseFailure([ContractIssue("unexpected_result_type", "payload",
                                          f"the structured call returned {type(result).__name__}, not a schema instance")])
    try:
        return strict.model_validate(dict(result))
    except ValidationError as exc:
        raise ParseFailure(_issues_from_validation_error(exc)) from None


def _surface_unrecoverable(agent, mode, stage, kind, exc, calls) -> AgentContractError:
    """Build the typed abort for a transport or programming failure (T-29).

    The record carries the classification and a redacted detail, no fallback is taken,
    and the honest retry accounting is preserved.
    """
    issues = [ContractIssue(kind.value, "payload", redact_detail(exc))]
    record = ValidationRecord(agent, mode, stage, False, issues, calls=calls)
    return AgentContractError(agent, stage, kind, issues, record)



def invoke_with_contract(
    *,
    agent: str,
    mode: str,
    state: Mapping[str, Any] | None,
    structured_llm: Any | None,
    plain_llm: Any | None,
    prompt: Any,
    sdk_retry_budget: Any = None,
    recovery_prompt: Callable[[list[ContractIssue]], Any] | None = None,
) -> EnhancedInvocation:
    """Run one agent under an enhanced mode with at most one recovery (FR-5, FR-9).

    Sequence, bounded by construction:

    1. Input contract, before any model call: invalid input costs zero calls (FR-7).
    2. `structured` call. A parse or validation problem is recoverable; a transport or
       programming problem is surfaced immediately and never replaced by a fallback.
    3. One recovery fills the single remaining slot. `validate+repair` may ask for a
       semantic repair (counted as `semantic_repair`); otherwise the compatibility
       free-text route is used and itself validated (`free_text_recovery`).
    4. Full revalidation of whatever is accepted; nothing is accepted unchecked.
    5. The record is built from the calls actually made, with no invented counts.

    Raises `AgentContractError` when no accepted route validates; the caller must write no
    state on that failure (FR-8, INV-2).

    When the provider rejected the structured binding (`structured_llm` is None), the
    plain route is the only route. It is validated and is not counted as a recovery,
    because no structured attempt was spent to recover from.
    """
    from tradingagents.agents.contracts_plain import parse_plain_output

    normalized = normalize_mode(mode)
    if normalized == MODE_OFF:
        raise ValueError("invoke_with_contract is for enhanced modes; off mode keeps the baseline helper")
    if state is None:
        # Callers always pass the state they read from; a missing mapping is a programming
        # error, not a validation pass.
        raise TypeError(f"{agent}: invoke_with_contract requires the agent's state mapping")

    calls = CallRecord(sdk_retry_budget=sdk_retry_budget)

    # Stage 1: input contract.
    validate_input(agent, state, normalized, calls)

    # Stage 2: the structured attempt. Counting is decision-free: an attempted call costs
    # one logical slot whether or not it then parses.
    stage: Stage = Stage.STRUCTURED
    issues: list[ContractIssue] = []
    payload: BaseModel | None = None

    def _call_and_parse(callable_: Any, argument: Any, structured: bool) -> tuple[BaseModel | None, list[ContractIssue], bool]:
        """One provider call, then a local parse, kept strictly apart (T-29).

        Only the provider call is wrapped for classification. The parse below runs outside
        that wrapper, so a ``ValueError`` from the provider propagates as the programming
        error it is, while a parse problem is expressed as returned issues.

        The third element separates the two recoverable kinds (review item B):

        - ``True`` -- the answer could not be *parsed* at all (validation error, no parsed
          result, unparseable text). The answer carries no usable payload, so a repair or a
          compatibility free-text attempt is a different answer to the same question.
        - ``False`` -- the answer parsed and then *violated its contract* (blank narrative,
          action not supported by the plan). The model did answer; asking it again inside
          ``validate`` would be a semantic re-run, which only ``validate+repair`` may do.
        """
        try:
            result = callable_.invoke(argument)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            kind = classify_exception(exc)
            if kind in RECOVERABLE_KINDS:
                local_issues = list(exc.issues) if isinstance(exc, ParseFailure) else (
                    _issues_from_validation_error(exc) if isinstance(exc, ValidationError)
                    else [ContractIssue(kind.value, "payload", redact_detail(exc))]
                )
                return None, local_issues, True
            raise _surface_unrecoverable(agent, normalized, stage, kind, exc, calls) from exc
        if structured:
            try:
                parsed = _coerce_structured_result(agent, result)
            except ParseFailure as exc:
                return None, list(exc.issues), True
        else:
            plain = parse_plain_output(agent, result)
            if not plain.ok:
                return None, list(plain.issues), True
            try:
                parsed = strict_schema_for(agent).model_validate(plain.values)
            except ValidationError as exc:
                return None, _issues_from_validation_error(exc), True
        field_issues = validate_payload(agent, parsed)
        if field_issues:
            return None, field_issues, False
        return parsed, [], False

    parse_failure = True
    if structured_llm is None:
        stage = Stage.FREE_TEXT
    else:
        calls.structured = 1
        payload, issues, parse_failure = _call_and_parse(structured_llm, prompt, structured=True)

    # Stage 3: the single recovery slot. Counters rise only for calls actually made.
    if payload is None:
        # Semantic repair is what ``validate+repair`` exists for, and it applies to a *parsed*
        # payload that violates its contract (blank narrative, unsupported action) exactly as
        # it does to an unparseable one. ``parse_failure`` therefore does NOT gate the repair;
        # it gates only the ``validate`` compatibility fallback below. In ``validate`` a
        # semantic violation is terminal: no repair, no fallback (review B).
        repair_allowed = (
            mode_allows_repair(normalized)
            and structured_llm is not None
            and recovery_prompt is not None
        )
        # Narrow the callback into a local before calling it: mypy cannot see through the
        # combined ``repair_allowed`` boolean, and the guard is still the same condition.
        repair_call = recovery_prompt if repair_allowed else None
        if repair_call is not None:
            stage = Stage.REPAIR
            calls.semantic_repair = 1
            try:
                repair_argument = repair_call(issues)
            except Exception as exc:  # noqa: BLE001 - a broken prompt builder is a bug
                raise _surface_unrecoverable(agent, normalized, Stage.REPAIR, FailureKind.PROGRAMMING, exc, calls) from exc
            payload, repair_issues, _ = _call_and_parse(structured_llm, repair_argument, structured=True)
            if payload is None:
                # A repaired payload is validated in full, so a second bad field also fails.
                issues = repair_issues

        # The plain route is the primary route when no structured binding exists (its call
        # is moved to ``primary_plain``), so it is allowed even though no slot was spent.
        # Otherwise it is the compatibility fallback for a *parse* failure under ``validate``
        # only: in ``validate+repair`` the single slot was already spent on the repair, and a
        # semantic violation is never retried (review B).
        plain_is_only_route = structured_llm is None and calls.primary_plain == 0
        plain_as_fallback = (
            calls.semantic_repair == 0
            and not mode_allows_repair(normalized)
            and parse_failure
        )
        if payload is None and plain_llm is not None and (plain_is_only_route or plain_as_fallback):
            stage = Stage.FREE_TEXT
            plain_failed_before = plain_is_only_route
            calls.free_text_recovery = 1
            payload, plain_issues, _ = _call_and_parse(plain_llm, prompt, structured=False)
            if payload is None:
                issues = plain_issues
            if plain_failed_before:
                # The provider rejected the structured binding: this plain call was the only
                # route, not a recovery from a structured attempt. Counting it as a recovery
                # would claim an event that never happened (FR-10).
                calls.free_text_recovery = 0
                calls.primary_plain = 1
        elif payload is None and plain_llm is None and calls.free_text_recovery == 0 and calls.semantic_repair == 0:
            issues = issues or [ContractIssue("no_accepted_route", "payload",
                                              "no structured or free-text route available")]

        if payload is None:
            record = ValidationRecord(agent, normalized, stage, False, list(issues), calls=calls)
            raise AgentContractError(agent, stage, FailureKind.VALIDATION, list(issues), record)

    # Stage 4: accepted. The trade-off check is informational and skipped when unreadable.
    informational: list[ContractIssue] = []
    skipped: list[str] = []
    if agent == TRADER:
        # ``_call_and_parse`` validates every accepted Trader payload against the strict
        # schema, so this holds; the check states the invariant instead of asserting it away.
        if not isinstance(payload, StrictTraderProposal):
            raise TypeError("a Trader payload must be a validated StrictTraderProposal")
        informational, skipped = check_trader_against_plan(state.get("investment_plan"), payload.action)

    record = ValidationRecord(agent, normalized, stage, True, [], informational, skipped, calls)
    rendered = render_payload(agent, to_baseline_payload(agent, payload))
    return EnhancedInvocation(payload=payload, rendered=rendered, record=record)

# ---------------------------------------------------------------------------
# Node-facing integration surface (FR-1, FR-3, FR-6, FR-8; ADR section 7 steps 5-7)
# ---------------------------------------------------------------------------

# The records of the run in progress, so a report or an audit can show what each agent
# did. Kept per run in a context variable rather than in the graph state, because records
# are diagnostics, not decision inputs, and must not enter the checkpoint signature.
_RUN_RECORDS: ContextVar[dict[str, ValidationRecord] | None] = ContextVar(
    "tradingagents_agent_contract_records", default=None
)
# Failure markers written into state by a failed run's own audit path; a downstream reader
# must never consume one as a plan (FR-8).
FAILURE_MARKERS: dict[str, ContractIssue] = {}


def begin_record_scope() -> Token[dict[str, ValidationRecord] | None]:
    """Start a fresh per-run record scope; returns a token to pass to :func:`end_record_scope`."""
    return _RUN_RECORDS.set({})


def end_record_scope(token: Token[dict[str, ValidationRecord] | None]) -> None:
    _RUN_RECORDS.reset(token)


def current_records() -> dict[str, ValidationRecord]:
    """The validation records recorded so far in this run (empty outside a run)."""
    return dict(_RUN_RECORDS.get() or {})


def contract_state_update(existing: Mapping[str, Any] | None = None) -> dict:
    """The additive ``contract_records`` state key a successful agent node returns (E).

    It merges this run's records into any already saved in state, so a resumed run keeps the
    records of the agents that ran before the interruption while the current scope still
    supplies the live view. Values are plain JSON-compatible dicts, so the key survives a
    SQLite checkpoint round trip. Outside an enhanced run it returns ``{}``, which leaves the
    state shape of mode ``off`` untouched (INV-7).
    """
    merged = dict(existing or {}) if isinstance(existing, Mapping) else {}
    for agent, record in current_records().items():
        merged[agent] = record.as_dict()
    return merged


def _store_record(record: ValidationRecord) -> None:
    records = _RUN_RECORDS.get()
    if records is not None:
        records[record.agent] = record


def active_mode(config: Mapping[str, Any] | None = None) -> str | None:
    """The enhanced mode in force for this run, or ``None`` for ``off`` (FR-1).

    An unknown value raises ``ValueError`` rather than silently running in ``off``.
    """
    from tradingagents.dataflows.config import get_config

    settings = config if config is not None else get_config()
    mode = normalize_mode(settings.get("agent_contract_mode", MODE_OFF))
    return None if mode == MODE_OFF else mode


def bind_strict(llm: Any, agent: str) -> Any | None:
    """Bind the **strict** schema for ``agent`` in an enhanced mode (FR-3).

    Mirrors ``structured.bind_structured``: a provider that rejects structured output
    returns ``None`` and the node then has only the plain, validated route. The strict
    schema is what keeps raw invalid numerics out of the provider's parser (T-26).
    """
    from tradingagents.agents.structured import bind_structured

    return bind_structured(llm, strict_schema_for(agent), agent)


def run_agent_contract(
    *,
    agent: str,
    state: Mapping[str, Any],
    structured_llm: Any | None,
    plain_llm: Any | None,
    prompt: Any,
    repair_prompt: Callable[[list[ContractIssue]], Any] | None = None,
) -> EnhancedInvocation:
    """Run one agent node under the run's enhanced mode (FR-1, FR-5, FR-6).

    The repair prompt is built lazily from the actual issues, so the model receives the
    original context plus every contract problem (FR-5). The record is stored for the run
    whether the invocation succeeds or raises (FR-6).
    """
    from tradingagents.dataflows.config import get_config

    mode = active_mode()
    if mode is None:
        raise ValueError("run_agent_contract is for enhanced modes; off mode keeps the baseline helper")
    config = get_config()
    try:
        invocation = invoke_with_contract(
            agent=agent, mode=mode, state=state, structured_llm=structured_llm,
            plain_llm=plain_llm, prompt=prompt,
            sdk_retry_budget=config.get("llm_max_retries"),
            recovery_prompt=repair_prompt,
        )
    except AgentContractError as exc:
        if exc.record is not None:
            _store_record(exc.record)
        raise
    _store_record(invocation.record)
    return invocation


def failure_marker(agent: str, issues: list[ContractIssue]) -> str:
    """The failure notice a failed run may write where a plan would have gone.

    It is written so a reader sees why, and every consumer rejects it as input
    (``is_failure_marker``, FR-8). It never contains model text or secrets (FR-6).
    """
    codes = ", ".join(sorted({issue.code for issue in issues})) or "validation"
    return (f"{FAILURE_MARKER_PREFIX}: {agent}] No usable output was produced for this step "
            f"({codes}). The run was stopped; this is not a recommendation.")


def contract_summary_lines(records: Mapping[str, ValidationRecord] | None = None) -> list[str]:
    """English, payload-free lines describing each agent's contract outcome (FR-6).

    Printed only for enhanced runs. A caller writes this into its own summary artifact or
    report block; it carries codes and counts, never model text (FR-6).
    """
    records = records if records is not None else current_records()
    lines = []
    for agent in (RESEARCH_MANAGER, TRADER, PORTFOLIO_MANAGER):
        record = records.get(agent)
        if record is None:
            continue
        calls = record.calls.as_dict()
        status = "accepted" if record.ok else "failed"
        detail = ""
        if record.issues:
            detail = " (" + ", ".join(f"{i.field}:{i.code}" for i in record.issues) + ")"
        if record.informational:
            detail += " [informational: " + ", ".join(i.code for i in record.informational) + "]"
        if record.skipped:
            detail += " [skipped: " + ", ".join(record.skipped) + "]"
        lines.append(
            f"- {agent}: {status} at {record.stage.value}{detail}; "
            f"calls structured={calls['structured']}, free_text_recovery={calls['free_text_recovery']}, "
            f"semantic_repair={calls['semantic_repair']}, total={calls['logical_total']}; "
            f"retry budget {calls['sdk_retry_budget']}, observed HTTP attempts "
            f"{calls['observed_http_attempts']}"
        )
    return lines

def build_repair_prompt(original_prompt: Any, issues: list[ContractIssue], requirement: str) -> str:
    """The single repair instruction: the original context plus every contract problem.

    The model sees exactly what it was asked before, so the repair is a correction of its
    own answer rather than a fresh, differently-contextualised question (FR-5). Only issue
    fields, codes and fixed descriptions are included; model text is never echoed (FR-6).
    """
    problems = "\n".join(f"- {issue.field}: {issue.code}" for issue in issues) or "- payload: invalid"
    return (
        f"{original_prompt}\n\n---\n\nYour previous answer did not satisfy the output "
        f"contract for these fields:\n{problems}\n\n"
        f"Answer again with every required section. Keep {requirement}. Change nothing "
        "else about the analysis."
    )
def revalidate_saved_plan(agent: str, value: Any, field: str) -> ContractIssue | None:
    """Revalidate a plan already saved in state, in full, before another agent consumes it.

    Only an **absent optional** value is acceptable here; a required upstream plan is never
    skipped. Everything else is re-parsed against the documented shape of ``agent`` and the
    strict schema, and **every** issue counts: a missing narrative, an invalid numeric or a
    wrong type is a reason to refuse the plan, not only an unknown label or a duplicate key
    (review item D). In mode ``off`` the caller keeps the baseline behaviour and never
    calls this.
    """
    from tradingagents.agents.contracts_plain import parse_plain_output

    if value is None or (isinstance(value, str) and not value.strip()):
        return ContractIssue("missing_required_input", field, "required upstream plan is absent or blank")
    if not isinstance(value, str):
        return ContractIssue("wrong_input_type", field, f"a plan must be text, got {type(value).__name__}")
    if is_failure_marker(value):
        return ContractIssue("failure_marker_input", field, "a failed plan cannot be consumed")
    parsed = parse_plain_output(agent, value)
    if parsed.issues:
        return parsed.issues[0]
    try:
        strict = strict_schema_for(agent).model_validate(parsed.values)
    except ValidationError as exc:
        issues = _issues_from_validation_error(exc)
        if issues:
            return issues[0]
    # The schema constrains types and numerics; the field contract adds the narrative rules
    # (a blank required text is a violation even though it is a valid string).
    field_issues = validate_payload(agent, strict)
    return field_issues[0] if field_issues else None


def validate_risk_debator_input(state: Mapping[str, Any]) -> list[ContractIssue]:
    """What every risk debator reads: the trader plan, the risk state and the reports.

    The plan is revalidated in full (it is the debate's subject); the risk debate state is
    indexed directly, so its presence is required, while the four analyst reports are
    optional context that a debator renders as "not provided" when absent.
    """
    issues: list[ContractIssue] = []
    plan_issue = revalidate_saved_plan(TRADER, state.get("trader_investment_plan"),
                                       "trader_investment_plan")
    if plan_issue is not None:
        issues.append(plan_issue)
    risk = state.get("risk_debate_state")
    if not isinstance(risk, Mapping):
        issues.append(ContractIssue("missing_required_input", "risk_debate_state",
                                    "a risk debate state is required"))
    else:
        for name in ("history", "aggressive_history", "conservative_history", "neutral_history",
                     "current_aggressive_response", "current_conservative_response",
                     "current_neutral_response"):
            issue = _optional_text_issue(f"risk_debate_state.{name}", risk.get(name))
            if issue is not None:
                issues.append(issue)
    for name in ("market_report", "sentiment_report", "news_report", "fundamentals_report"):
        issue = _optional_text_issue(name, state.get(name))
        if issue is not None:
            issues.append(issue)
    return issues


def require_identity(agent: str, state: Mapping[str, Any], mode: Any) -> None:
    """The instrument identity the Trader and Portfolio Manager index directly (review C).

    Those nodes read ``state["company_of_interest"]`` without a default, so in an enhanced
    mode an absent identity is reported as an input issue at INPUT with zero model calls
    instead of surfacing as a ``KeyError``. In mode ``off`` this is never called.
    """
    issue = _identity_input_issue(state.get("company_of_interest"))
    if issue is not None:
        _surface_input_failure(agent, normalize_mode(mode), [issue])


def _surface_input_failure(agent: str, mode: str, issues: list[ContractIssue]) -> None:
    records = current_records()
    record = ValidationRecord(agent, mode, Stage.INPUT, False, list(issues),
                              calls=records[agent].calls if agent in records else CallRecord())
    raise AgentContractError(agent, Stage.INPUT, FailureKind.VALIDATION, list(issues), record)


def guard_trader_plan(state) -> str:
    """The guard every risk debator runs before its own model call (ADR-001 section 2.7).

    It runs **first**, before the debator indexes any state: a plan that failed its contract
    -- or a failure marker written by an aborted run -- must cost zero model calls, and the
    rest of the debator's input is checked at the same moment (review items C and D). In
    mode ``off`` it returns the value untouched (INV-7).
    """
    plan = state.get("trader_investment_plan")
    mode = active_mode()
    if mode is None:
        return plan
    issues = validate_risk_debator_input(state)
    if issues:
        _surface_input_failure(TRADER, mode, issues)
    return plan
