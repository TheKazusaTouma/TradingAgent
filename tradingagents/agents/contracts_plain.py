"""Finite, explicit parser for a model's plain-text answer under an enhanced mode.

The compatibility free-text route (ADR-001 section 2.3) exists because some providers
cannot bind a structured schema. Their answer is an ``AIMessage`` whose ``content`` is
text, so the contract layer needs one parser that turns that text into the strict payload
*or* fails. It never invents a field:

1. The message content is unwrapped (string, or a list of text blocks).
2. A JSON object -- bare, fenced (```json) or as ``` fence -- is tried first, because it
   states fields exactly.
3. Otherwise the documented labelled Markdown sections are read, which is the same shape
   the baseline renderers write (``**Recommendation**: X``, ``**Rationale**: ...``).

Ambiguity is a failure, not a guess: a duplicated required label, a conflicting repeated
value, or a required label that is absent, all produce a stable issue code and no payload.
An optional numeric label that cannot be read as one unambiguous number is **not** a
failure: the field is absent from the payload and one finding records the neutral reason
(``contracts.SANITIZED_NUMBER_CODE``, ADR-002 section 4). A numeric stated twice with
different values is reported the same way, with reason ``conflicting_json``: picking one
would be a guess.

This module deliberately has no dependency on the graph, the CLI or a vendor SDK.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tradingagents.agents.contracts import (
    PORTFOLIO_MANAGER,
    REASON_CONFLICTING,
    REASON_OMITTED,
    RESEARCH_MANAGER,
    SANITIZED_NUMBER_CODE,
    TRADER,
    ContractIssue,
    InvalidNumber,
    read_optional_number,
)
from tradingagents.agents.schemas import PortfolioRating, TraderAction

# Labels each agent's documented output uses, keyed by the schema field name. The keys are
# fixed by the prompts in the agent modules and by ``schemas.py``'s renderers, so this is a
# structural read of the documented format, not a reading of arbitrary prose.
_LABELS: dict[str, dict[str, tuple[str, ...]]] = {
    RESEARCH_MANAGER: {
        "recommendation": ("recommendation",),
        "rationale": ("rationale",),
        "strategic_actions": ("strategic actions", "strategic action"),
    },
    TRADER: {
        "action": ("action",),
        "reasoning": ("reasoning",),
        "entry_price": ("entry price",),
        "stop_loss": ("stop loss", "stop-loss"),
        "position_sizing": ("position sizing",),
    },
    PORTFOLIO_MANAGER: {
        "rating": ("rating",),
        "executive_summary": ("executive summary",),
        "investment_thesis": ("investment thesis",),
        "price_target": ("price target",),
        "time_horizon": ("time horizon",),
    },
}
_TEXT_FIELDS = {
    RESEARCH_MANAGER: ("rationale", "strategic_actions"),
    TRADER: ("reasoning", "position_sizing"),
    PORTFOLIO_MANAGER: ("executive_summary", "investment_thesis", "time_horizon"),
}
# Required narrative fields: nonblank when present, and their absence is a failure.
_REQUIRED_TEXT = {
    RESEARCH_MANAGER: ("rationale", "strategic_actions"),
    TRADER: ("reasoning",),
    PORTFOLIO_MANAGER: ("executive_summary", "investment_thesis"),
}
_NUMERIC_FIELDS = {
    RESEARCH_MANAGER: (),
    TRADER: ("entry_price", "stop_loss"),
    PORTFOLIO_MANAGER: ("price_target",),
}
_ENUM_VALUES = {
    RESEARCH_MANAGER: {value.value for value in PortfolioRating},
    PORTFOLIO_MANAGER: {value.value for value in PortfolioRating},
    TRADER: {value.value for value in TraderAction},
}
# "not provided" is what the baseline renderer writes for an absent optional level
# (``schemas.py:208``, ``:285-287``). Reading it back as an omission keeps a rendered and
# re-read payload equal to the original; it is never read as a number.
_ABSENT_LEVELS = frozenset({"not provided", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"})

_LEVEL3 = re.compile(r"^###\s+(.+?)\s*$")
_LABEL_LINE = re.compile(
    r"^(?:[-*+]\s+)?(?:\*\*|__)?\s*([A-Za-z][A-Za-z /-]*?)\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$"
)
_BULLET_LABEL = re.compile(r"^[-*+]\s+(?:\*\*|__)?\s*([A-Za-z][A-Za-z /-]*?)\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$")


@dataclass
class ParsedPlain:
    """The fields a plain answer stated, plus any finding that makes it unusable.

    ``sanitized`` holds one finding per optional numeric the answer stated but that could
    not be read as a single number. That field is absent in ``values`` (ADR-002 section 4),
    so it never becomes a failure and never spends another model call.
    """

    values: dict[str, Any] = field(default_factory=dict)
    issues: list[ContractIssue] = field(default_factory=list)
    sanitized: list[ContractIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def unwrap_content(message: Any) -> tuple[str | None, list[ContractIssue]]:
    """The text of a provider result, or an issue explaining why there is none.

    ``AIMessage(content=...)`` is the shape ``llm.invoke`` returns. A list content is the
    provider's text-block form; only ``text`` blocks are joined, and any non-text block
    (a tool call, an image) means the answer is not a complete plan, which is reported
    rather than ignored.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content, []
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, dict) and block.get("type", "text") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
                continue
            return None, [ContractIssue("non_text_content", "payload",
                                        "content includes a non-text block, so no complete plan is present")]
        # Blocks are separate paragraphs of one answer; joining them without a separator
        # would fuse two labelled lines into one unparseable line.
        return "\n".join(parts), []
    if content is None:
        return None, [ContractIssue("empty_content", "payload", "the provider returned no content")]
    return None, [ContractIssue("unsupported_content", "payload",
                                f"content type {type(content).__name__} is not text")]


def _strip_fence(text: str) -> str:
    """The body of a ``` or ```json fence, when the whole answer is one fence."""
    stripped = text.strip()
    match = re.fullmatch(r"```[A-Za-z0-9_-]*\s*\n(.*?)\n?```", stripped, re.DOTALL)
    return match.group(1) if match else stripped


def _json_object(text: str) -> dict[str, Any] | None:
    """The top-level JSON object in ``text``, or ``None`` when the text is not one."""
    candidate = _strip_fence(text)
    for attempt in (candidate, _first_braced_object(candidate)):
        if not attempt:
            continue
        try:
            parsed = json.loads(attempt)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)\n?[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove a fenced code block's banner lines so the content inside is read as the answer.

    A model that answers with ``json`` in a fence still stated the fields; the fence is
    formatting, not content. A fence that holds a whole answer becomes plain text, and one
    that holds a JSON object is then found by :func:`_json_object`.
    """
    stripped = text.strip()
    match = _FENCE.fullmatch(stripped)
    return match.group(1).strip() if match else stripped


def _first_braced_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else None


def _normalise_label(label: str) -> str:
    return re.sub(r"[\s_*:\-]+", " ", label).strip().lower()


def _sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown answer into ``(label, body)`` pairs, one per labelled section.

    A labelled line opens a section; everything after it, up to the next labelled line or a
    heading, is that section's body, so a multiline narrative is preserved verbatim. A
    ``###`` heading inside a body is kept as content of that body: only a heading whose text
    is a field label starts a new section.
    """
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = _LEVEL3.match(line.strip())
        if heading:
            label = _normalise_label(heading.group(1))
            if label:
                sections.append((label, []))
                continue
        match = _BULLET_LABEL.match(line.strip()) or _LABEL_LINE.match(line.strip())
        if match:
            label = _normalise_label(match.group(1))
            if label:
                sections.append((label, [match.group(2)]))
                continue
        if sections:
            sections[-1][1].append(line)
    return [(label, "\n".join(body).strip()) for label, body in sections]


def _enum_value(raw: str, allowed: set[str]) -> tuple[str | None, str | None]:
    text = raw.strip()
    # Try the whole statement first (``Hold (no action)`` is still a Hold), then its first
    # token (``Buy, adding 5%``). Neither is a guess: only an exact, case-insensitive match
    # against an allowed label succeeds, and anything else is reported unchanged.
    for candidate in (text, text.split()[0] if text.split() else ""):
        cleaned = candidate.strip().strip("*_`").strip().rstrip(".,;:()")
        for value in allowed:
            if cleaned.lower() == value.lower():
                return value, None
    return None, "not one of the allowed values"


def parse_plain_output(agent: str, message: Any) -> ParsedPlain:
    """Parse an unknown provider result into strict-shape fields, or report why not.

    Returns a :class:`ParsedPlain`; the caller validates the resulting payload against the
    agent's contract exactly as it validates a structured one.
    """
    text, issues = unwrap_content(message)
    if text is None:
        return ParsedPlain(issues=issues)
    text = _strip_code_fences(text)
    labels = _LABELS[agent]

    payload = _json_object(text)
    if payload is not None:
        return _from_mapping(agent, payload)

    sections = _sections(text)
    found: dict[str, list[str]] = {}
    for label, body in sections:
        for field_name, aliases in labels.items():
            if label in aliases:
                found.setdefault(field_name, []).append(body)
                break

    values: dict[str, Any] = {}
    sanitized: list[ContractIssue] = []
    for field_name, bodies in found.items():
        distinct = {body.strip() for body in bodies}
        if len(distinct) > 1 and field_name in _NUMERIC_FIELDS[agent]:
            # A level stated twice with different values cannot be resolved without picking
            # one, so the field is absent and the reason is recorded (ADR-002 section 4).
            sanitized.append(ContractIssue(SANITIZED_NUMBER_CODE, field_name, REASON_CONFLICTING))
            continue
        if len(distinct) > 1:
            issues.append(ContractIssue("duplicate_field", field_name,
                                        "the answer states this field more than once with different values"))
            continue
        body = distinct.pop()
        if field_name in _ENUM_VALUES[agent]:
            value, reason = _enum_value(body, _ENUM_VALUES[agent])
            if reason:
                issues.append(ContractIssue("invalid_enum", field_name, reason))
            else:
                values[field_name] = value
        elif field_name in _NUMERIC_FIELDS[agent]:
            number, reason = read_optional_number(body)
            if reason is None:
                values[field_name] = number
            elif reason != REASON_OMITTED:
                values[field_name] = InvalidNumber(body, reason)
                sanitized.append(ContractIssue(SANITIZED_NUMBER_CODE, field_name, reason))
        else:
            values[field_name] = body

    required = _required_fields(agent)
    for field_name in required:
        if field_name not in values and field_name not in found:
            issues.append(ContractIssue("missing_field", field_name,
                                        "the answer does not state this required field"))
    return ParsedPlain(values=values, issues=issues, sanitized=sanitized)


def _required_fields(agent: str) -> tuple[str, ...]:
    """The fields the agent's contract requires. Optional fields may be absent (INV-6)."""
    return (_enum_field(agent),) + _REQUIRED_TEXT[agent]


def _enum_field(agent: str) -> str:
    if agent == TRADER:
        return "action"
    if agent == PORTFOLIO_MANAGER:
        return "rating"
    return "recommendation"


def _from_mapping(agent: str, payload: dict[str, Any]) -> ParsedPlain:
    """A JSON object: keep only the agent's known fields, and read numerics strictly."""
    known = set(_TEXT_FIELDS[agent]) | set(_NUMERIC_FIELDS[agent]) | {_enum_field(agent)}
    values: dict[str, Any] = {}
    issues: list[ContractIssue] = []
    sanitized: list[ContractIssue] = []
    for name in known:
        if name not in payload:
            continue
        raw = payload[name]
        if name in _NUMERIC_FIELDS[agent]:
            number, reason = read_optional_number(raw)
            if reason is None:
                values[name] = number
            elif reason != REASON_OMITTED:
                values[name] = InvalidNumber(raw, reason)
                sanitized.append(ContractIssue(SANITIZED_NUMBER_CODE, name, reason))
        elif name == _enum_field(agent):
            if not isinstance(raw, str):
                issues.append(ContractIssue("invalid_enum", name, "not one of the allowed values"))
                continue
            value, reason = _enum_value(raw, _ENUM_VALUES[agent])
            if reason:
                issues.append(ContractIssue("invalid_enum", name, reason))
            else:
                values[name] = value
        else:
            values[name] = raw
    return ParsedPlain(values=values, issues=issues, sanitized=sanitized)
