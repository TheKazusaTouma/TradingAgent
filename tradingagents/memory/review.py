"""Human review of settled memory reflections, recorded in an append-only ledger.

A settled reflection is a model-written lesson. When review is enabled, a run
may only use a lesson a person approved, and only as that person saw it: an
approval binds the reflection's SHA-256, so editing the reflection afterwards
silently withdraws the approval. The ledger stores the decision, never the
reflection text, and an unreadable ledger is an error rather than a reason to
fall back to every lesson.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from tradingagents.dataflows.date_window import to_utc
from tradingagents.dataflows.files import locked

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"approve", "revoke"})
_FIELDS = frozenset({"ticker", "decision_date", "reflection_sha256", "action", "at"})
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class ReviewLedgerError(RuntimeError):
    """The review ledger exists but cannot be trusted."""


def review_ledger_path(memory_log_path) -> Path | None:
    """Where a memory log's review ledger lives, beside the log itself."""
    if not memory_log_path:
        return None
    try:
        return Path(memory_log_path).expanduser().with_suffix(".reviews.jsonl")
    except (OSError, RuntimeError) as exc:
        raise ReviewLedgerError(
            f"the review ledger path for {memory_log_path!r} could not be resolved: {exc}"
        ) from exc


def reflection_fingerprint(reflection: str) -> str:
    """The fingerprint an approval binds: exactly the reflection text displayed."""
    return hashlib.sha256(reflection.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewCutoff:
    """The latest review event a run may take into account."""

    at: datetime
    inclusive: bool

    def includes(self, when: datetime) -> bool:
        return when <= self.at if self.inclusive else when < self.at


def review_cutoff(
    trade_date: str,
    now_utc: datetime | None = None,
    *,
    historical: bool | None = None,
) -> ReviewCutoff:
    """The review clock for a run dated ``trade_date``.

    The run mode is decided once, by the caller, and shared with settlement
    filtering: pass ``historical=True`` or ``False``. Without it, fall back to
    the local calendar, as the rest of the app does. A live run sees events up
    to now, inclusive. A historical run sees only events strictly before
    midnight UTC after its analysis date, so replaying an old analysis cannot
    see a later approval or revocation.
    """
    now = to_utc(now_utc or datetime.now(UTC))
    try:
        day = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"trade_date must be YYYY-MM-DD, got {trade_date!r}") from exc
    if historical is None:
        historical = day < now.date()
    if historical:
        return ReviewCutoff(
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC),
            inclusive=False,
        )
    return ReviewCutoff(now, inclusive=True)


@dataclass(frozen=True)
class ReviewEvent:
    """One line of the review ledger."""

    ticker: str
    decision_date: str
    reflection_sha256: str
    action: str
    at: datetime


def _parse_event(line: str, line_number: int) -> ReviewEvent:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ReviewLedgerError(f"line {line_number} is not valid JSON: {exc.msg}") from exc
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ReviewLedgerError(
            f"line {line_number} must have exactly these fields: {', '.join(sorted(_FIELDS))}"
        )
    values = {key: record[key] for key in _FIELDS}
    if not all(isinstance(value, str) for value in values.values()):
        raise ReviewLedgerError(f"line {line_number} has a non-string field")
    if not values["ticker"]:
        raise ReviewLedgerError(f"line {line_number} has an empty ticker")
    try:
        decision_date = datetime.strptime(values["decision_date"], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ReviewLedgerError(f"line {line_number} has an invalid decision_date") from exc
    if decision_date != values["decision_date"]:
        raise ReviewLedgerError(f"line {line_number} has an invalid decision_date")
    if not _FINGERPRINT.fullmatch(values["reflection_sha256"]):
        raise ReviewLedgerError(f"line {line_number} has an invalid reflection_sha256")
    if values["action"] not in _ACTIONS:
        raise ReviewLedgerError(f"line {line_number} has an invalid action")
    try:
        parsed_at = datetime.fromisoformat(values["at"])
    except ValueError as exc:
        raise ReviewLedgerError(f"line {line_number} has an invalid at timestamp") from exc
    if parsed_at.tzinfo is None:
        raise ReviewLedgerError(f"line {line_number} has a timestamp without a timezone")
    at = to_utc(parsed_at)
    return ReviewEvent(
        ticker=values["ticker"],
        decision_date=decision_date,
        reflection_sha256=values["reflection_sha256"],
        action=values["action"],
        at=at,
    )


class ReviewSnapshot:
    """One replayed view of the ledger, safely queried many times."""

    def __init__(self, effective: dict[tuple[str, str, str], ReviewEvent]):
        self._effective = effective

    def status(self, ticker: str, decision_date: str, reflection: str) -> str:
        """Whether exactly this reflection text is approved in this snapshot."""
        if not reflection:
            return "not approved"
        event = self._effective.get((ticker, decision_date, reflection_fingerprint(reflection)))
        return "approved" if event is not None and event.action == "approve" else "not approved"

    def approved(self) -> dict[tuple[str, str], str]:
        """The fingerprint in force per (ticker, decision_date), latest first."""
        approved = [event for event in self._effective.values() if event.action == "approve"]
        approved.sort(key=lambda event: event.at)
        latest: dict[tuple[str, str], str] = {}
        for event in approved:
            latest[(event.ticker, event.decision_date)] = event.reflection_sha256
        return latest


class ReviewLedger:
    """The append-only approval/revocation history for one memory log."""

    def __init__(self, memory_log_path):
        self._path = review_ledger_path(memory_log_path)

    @property
    def path(self) -> Path | None:
        return self._path

    def _read_locked(self) -> list[ReviewEvent]:
        try:
            if self._path is None or not self._path.exists():
                return []
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReviewLedgerError(f"the review ledger could not be read: {exc}") from exc
        if not text:
            return []
        if not text.endswith("\n"):
            raise ReviewLedgerError("the review ledger does not end with a newline")
        events = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line:
                raise ReviewLedgerError(f"the review ledger {self._path}: line {line_number} is empty")
            try:
                events.append(_parse_event(line, line_number))
            except ReviewLedgerError as exc:
                raise ReviewLedgerError(f"the review ledger {self._path}: {exc}") from exc
        return events

    def events(self) -> list[ReviewEvent]:
        """Every event in write order; a missing ledger is empty, a bad one raises."""
        try:
            if self._path is None or not self._path.exists():
                return []
            with locked(self._path):
                return self._read_locked()
        except OSError as exc:
            raise ReviewLedgerError(
                f"the review ledger {self._path} could not be locked: {exc}"
            ) from exc

    @staticmethod
    def _in_effect(
        events: list[ReviewEvent], cutoff: ReviewCutoff | None = None
    ) -> dict[tuple[str, str, str], ReviewEvent]:
        """The last event per (ticker, decision_date, fingerprint) at ``cutoff``.

        Replay uses one order for every reader: event time, then file order, so
        an approval and a revocation of the same reflection version resolve the
        same way in ``approved``, ``history`` and the CLI's status. Keying by the
        fingerprint makes each reflection version its own history: approving
        R', editing, then restoring R leaves R's own approval in force.
        """
        ordered = sorted(enumerate(events), key=lambda pair: (pair[1].at, pair[0]))
        effective: dict[tuple[str, str, str], ReviewEvent] = {}
        for _, event in ordered:
            if cutoff is not None and not cutoff.includes(event.at):
                continue
            effective[(event.ticker, event.decision_date, event.reflection_sha256)] = event
        return effective

    def snapshot(self, cutoff: ReviewCutoff | None = None) -> ReviewSnapshot:
        """Read and replay the ledger once, for many queries against one view.

        A caller filtering candidates reads the ledger exactly once and asks this
        snapshot about each; a second read could see a different ledger than the
        one the candidates were judged against.
        """
        return ReviewSnapshot(self._in_effect(self.events(), cutoff))

    def approved(self, cutoff: ReviewCutoff | None = None) -> dict[tuple[str, str], str]:
        """The fingerprint in force for each (ticker, decision_date) at ``cutoff``.

        Versions replay independently; among the versions still approved, the
        one that most recently took effect wins, so restoring an old reflection
        brings its own earlier approval back. With no cutoff, every event
        currently on disk counts.
        """
        return self.snapshot(cutoff).approved()

    def status(
        self, ticker: str, decision_date: str, reflection: str,
        cutoff: ReviewCutoff | None = None,
    ) -> str:
        """Whether exactly this reflection text is approved: the CLI's answer."""
        return self.snapshot(cutoff).status(ticker, decision_date, reflection)

    def append(
        self,
        action: str,
        ticker: str,
        decision_date: str,
        fingerprint: str,
        at: datetime | None = None,
    ) -> ReviewEvent:
        """Append an event after proving the existing ledger is readable."""
        if action not in _ACTIONS:
            raise ValueError(f"unknown review action: {action!r}")
        if self._path is None:
            raise ReviewLedgerError("no memory log path is configured, so there is no review ledger")
        when = to_utc(at or datetime.now(UTC))
        record = {
            "ticker": ticker,
            "decision_date": decision_date,
            "reflection_sha256": fingerprint,
            "action": action,
            "at": when.isoformat(),
        }
        line = json.dumps(record)
        event = _parse_event(line, 1)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with locked(self._path):
                self._read_locked()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{line}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise ReviewLedgerError(
                f"the review ledger {self._path} could not be locked or written: {exc}"
            ) from exc
        return event

    def history(self, ticker: str | None = None, decision_date: str | None = None) -> list[ReviewEvent]:
        """Events in time order, optionally for one decision.

        Uses the same (time, file order) rule as replay, so the recorded history
        reads in the order that decided each event's effect.
        """
        ordered = [event for _, event in sorted(
            enumerate(self.events()), key=lambda pair: (pair[1].at, pair[0])
        )]
        if ticker is not None:
            ordered = [event for event in ordered if event.ticker.upper() == ticker.upper()]
        if decision_date is not None:
            ordered = [event for event in ordered if event.decision_date == decision_date]
        return ordered
