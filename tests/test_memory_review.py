"""Review-ledger semantics: time cutoffs, replay order, and content binding."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradingagents.memory import (
    ReviewCutoff,
    ReviewLedger,
    ReviewLedgerError,
    TradingMemoryLog,
    reflection_fingerprint,
    review_cutoff,
    review_ledger_path,
)

KEY = ("NVDA", "2026-01-02")
REFLECTION = "WAIT_FOR_PULLBACK_BEFORE_ADDING"


def _log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})


def _settled(log, reflection=REFLECTION):
    log.store_decision("NVDA", "2026-01-02", "Rating: Buy\n\nBuy NVDA.")
    log.update_with_outcome(
        "NVDA", "2026-01-02", 0.05, 0.02, 5, reflection,
        resolution_date="2026-01-08",
    )


def _live(at=datetime(2026, 2, 1, tzinfo=UTC)):
    return ReviewCutoff(at, inclusive=True)


def _context(log, cutoff, ledger):
    return log.get_past_context("NVDA", review_ledger=ledger, cutoff=cutoff)


@pytest.mark.unit
def test_a_live_cutoff_includes_the_instant_it_is_taken():
    now = datetime(2026, 1, 9, 15, 30, tzinfo=UTC)
    cutoff = review_cutoff("2026-01-09", now)

    assert cutoff == ReviewCutoff(now, inclusive=True)
    assert cutoff.includes(now)
    assert not cutoff.includes(datetime(2026, 1, 9, 15, 30, 0, 1, tzinfo=UTC))


@pytest.mark.unit
def test_a_historical_cutoff_is_midnight_after_the_analysis_date_and_exclusive():
    cutoff = review_cutoff("2026-01-09", datetime(2026, 2, 1, tzinfo=UTC))

    assert cutoff == ReviewCutoff(datetime(2026, 1, 10, tzinfo=UTC), inclusive=False)
    assert cutoff.includes(datetime(2026, 1, 9, 23, 59, 59, 999999, tzinfo=UTC))
    assert not cutoff.includes(datetime(2026, 1, 10, tzinfo=UTC))


@pytest.mark.unit
def test_review_cutoff_rejects_an_unreadable_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        review_cutoff("January", datetime(2026, 2, 1, tzinfo=UTC))


@pytest.mark.unit
def test_events_replay_in_time_order_not_file_order(tmp_path):
    ledger = ReviewLedger(tmp_path / "memory.md")
    fingerprint = reflection_fingerprint(REFLECTION)

    ledger.append("approve", *KEY, fingerprint, at=datetime(2026, 1, 9, 14, tzinfo=UTC))
    ledger.append("revoke", *KEY, fingerprint, at=datetime(2026, 1, 9, 12, tzinfo=UTC))
    ledger.append("approve", *KEY, fingerprint, at=datetime(2026, 1, 9, 10, tzinfo=UTC))

    assert [event.action for event in ledger.history()] == ["approve", "revoke", "approve"]
    assert ledger.approved() == {KEY: fingerprint}
    assert ledger.approved(ReviewCutoff(datetime(2026, 1, 9, 11, tzinfo=UTC), True)) == {
        KEY: fingerprint
    }
    assert ledger.approved(ReviewCutoff(datetime(2026, 1, 9, 13, tzinfo=UTC), True)) == {}


@pytest.mark.unit
def test_an_approval_binds_the_reflection_text_that_was_shown(tmp_path):
    log = _log(tmp_path)
    _settled(log)
    ledger = ReviewLedger(log._log_path)
    fingerprint = reflection_fingerprint(REFLECTION)
    ledger.append("approve", *KEY, fingerprint, at=datetime(2026, 1, 9, 10, tzinfo=UTC))

    assert REFLECTION in _context(log, _live(), ledger)

    text = log._log_path.read_text(encoding="utf-8")
    log._log_path.write_text(text.replace(REFLECTION, "CHANGED_AFTER_APPROVAL"), encoding="utf-8")

    assert _context(log, _live(), ledger) == ""

    ledger.append(
        "approve", *KEY, reflection_fingerprint("CHANGED_AFTER_APPROVAL"),
        at=datetime(2026, 1, 9, 11, tzinfo=UTC),
    )
    assert "CHANGED_AFTER_APPROVAL" in _context(log, _live(), ledger)


@pytest.mark.unit
def test_an_approval_binds_versions_and_history_replays_each_version(tmp_path):
    """R and R' are separate keys: approving R', editing to R', then back must
    leave R's own approval intact, and a revoke of R survives the round trip."""
    log = _log(tmp_path)
    _settled(log)
    ledger = ReviewLedger(log._log_path)
    original = reflection_fingerprint(REFLECTION)
    rewritten = reflection_fingerprint("REWRITTEN_AFTER_APPROVAL")
    ledger.append("approve", *KEY, original, at=datetime(2026, 1, 9, 10, tzinfo=UTC))
    ledger.append("approve", *KEY, rewritten, at=datetime(2026, 1, 9, 11, tzinfo=UTC))

    # Both versions are approved; the question is which one the current text is.
    at_eleven = ReviewCutoff(datetime(2026, 1, 9, 11, tzinfo=UTC), True)
    assert ledger.status(*KEY, REFLECTION, at_eleven) == "approved"
    assert ledger.status(*KEY, "REWRITTEN_AFTER_APPROVAL", at_eleven) == "approved"
    assert ledger.status(*KEY, REFLECTION, _live()) == "approved"
    assert ledger.status(*KEY, "REWRITTEN_AFTER_APPROVAL", _live()) == "approved"

    text = log._log_path.read_text(encoding="utf-8")
    log._log_path.write_text(text.replace(REFLECTION, "REWRITTEN_AFTER_APPROVAL"), encoding="utf-8")
    assert "REWRITTEN_AFTER_APPROVAL" in _context(log, _live(), ledger)

    log._log_path.write_text(text, encoding="utf-8")
    assert REFLECTION in _context(log, _live(), ledger)

    ledger.append("revoke", *KEY, original, at=datetime(2026, 1, 9, 12, tzinfo=UTC))
    text = log._log_path.read_text(encoding="utf-8")
    log._log_path.write_text(text.replace(REFLECTION, "REWRITTEN_AFTER_APPROVAL"), encoding="utf-8")
    log._log_path.write_text(text, encoding="utf-8")

    assert _context(log, _live(), ledger) == ""


@pytest.mark.unit
def test_history_and_approval_use_the_same_version_order(tmp_path):
    ledger = ReviewLedger(tmp_path / "memory.md")
    a, b = reflection_fingerprint("A"), reflection_fingerprint("B")
    ledger.append("approve", *KEY, b, at=datetime(2026, 1, 9, 12, tzinfo=UTC))
    ledger.append("approve", *KEY, a, at=datetime(2026, 1, 9, 10, tzinfo=UTC))
    ledger.append("revoke", *KEY, b, at=datetime(2026, 1, 9, 11, tzinfo=UTC))

    # Time order is A approve, then B revoke, then B approve: the later approval
    # of B wins even though its revoke was written after A's approval.
    assert [event.reflection_sha256 for event in ledger.history()] == [a, b, b]
    assert ledger.approved() == {KEY: b}


@pytest.mark.unit
def test_a_revoke_withdraws_the_lesson_for_runs_after_it(tmp_path):
    log = _log(tmp_path)
    _settled(log)
    ledger = ReviewLedger(log._log_path)
    fingerprint = reflection_fingerprint(REFLECTION)
    ledger.append("approve", *KEY, fingerprint, at=datetime(2026, 1, 9, 10, tzinfo=UTC))
    ledger.append("revoke", *KEY, fingerprint, at=datetime(2026, 1, 9, 12, tzinfo=UTC))

    assert REFLECTION not in _context(log, _live(), ledger)
    before_revoke = ReviewCutoff(datetime(2026, 1, 9, 11, tzinfo=UTC), True)
    assert REFLECTION in _context(log, before_revoke, ledger)


@pytest.mark.unit
def test_selecting_two_approved_candidates_reads_the_ledger_once(tmp_path, monkeypatch):
    log = _log(tmp_path)
    log.store_decision("NVDA", "2026-01-02", "Rating: Buy\n\nBuy NVDA.")
    log.update_with_outcome(
        "NVDA", "2026-01-02", 0.05, 0.02, 5, "NVDA_LESSON", resolution_date="2026-01-08",
    )
    log.store_decision("AAPL", "2026-01-03", "Rating: Hold\n\nHold AAPL.")
    log.update_with_outcome(
        "AAPL", "2026-01-03", 0.01, 0.0, 5, "AAPL_LESSON", resolution_date="2026-01-08",
    )
    ledger = ReviewLedger(log._log_path)
    ledger.append("approve", "NVDA", "2026-01-02", reflection_fingerprint("NVDA_LESSON"),
                  at=datetime(2026, 1, 9, 10, tzinfo=UTC))
    ledger.append("approve", "AAPL", "2026-01-03", reflection_fingerprint("AAPL_LESSON"),
                  at=datetime(2026, 1, 9, 11, tzinfo=UTC))

    reads = []
    real = ledger.events
    monkeypatch.setattr(ledger, "events", lambda: reads.append(1) or real())

    context = _context(log, _live(), ledger)

    assert "NVDA_LESSON" in context and "AAPL_LESSON" in context
    assert len(reads) == 1


@pytest.mark.unit
def test_a_missing_ledger_is_empty_but_a_missing_parent_is_a_ledger_error(tmp_path):
    # A normal absence of the ledger is no events.
    assert ReviewLedger(tmp_path / "memory.md").events() == []

    # A path whose parent cannot be created is an I/O error, not an absence.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    ledger = ReviewLedger(blocker / "memory.md")

    with pytest.raises(ReviewLedgerError):
        ledger.append("approve", *KEY, "0" * 64)


@pytest.mark.unit
def test_a_bad_json_ledger_reports_the_line_and_reason(tmp_path):
    log = _log(tmp_path)
    _settled(log)
    path = review_ledger_path(log._log_path)
    path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(ReviewLedgerError, match="line 1 is not valid JSON"):
        ReviewLedger(log._log_path).events()


@pytest.mark.unit
def test_an_absent_ledger_approves_nothing(tmp_path):
    log = _log(tmp_path)
    _settled(log)
    ledger = ReviewLedger(log._log_path)

    assert not review_ledger_path(log._log_path).exists()
    assert ledger.approved() == {}
    assert _context(log, _live(), ledger) == ""


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    '{"ticker":"NVDA"}\n',
    '{"ticker":"NVDA","decision_date":"2026-01-02","reflection_sha256":"'
    + "0" * 64 + '","action":"approve","at":"2026-01-09T10:00:00"}\n',
    '{"ticker":"NVDA","decision_date":"2026-01-02","reflection_sha256":"'
    + "0" * 64 + '","action":"approve","at":"2026-01-09T10:00:00+00:00"}',
])
def test_a_corrupt_or_unterminated_ledger_raises_and_injects_nothing(tmp_path, text):
    log = _log(tmp_path)
    _settled(log)
    path = review_ledger_path(log._log_path)
    path.write_text(text, encoding="utf-8")
    ledger = ReviewLedger(log._log_path)

    with pytest.raises(ReviewLedgerError):
        ledger.approved()
    with pytest.raises(ReviewLedgerError):
        _context(log, _live(), ledger)


@pytest.mark.unit
@pytest.mark.parametrize("method", ["events", "approved", "history"])
def test_an_unusable_lock_path_is_a_ledger_error(tmp_path, method):
    path = review_ledger_path(tmp_path / "memory.md")
    path.write_text("", encoding="utf-8")
    Path(f"{path}.lock").mkdir()   # opening the lock raises IsADirectoryError
    ledger = ReviewLedger(tmp_path / "memory.md")

    with pytest.raises(ReviewLedgerError):
        getattr(ledger, method)()


@pytest.mark.unit
def test_an_append_turns_an_unusable_lock_path_into_a_ledger_error(tmp_path):
    path = review_ledger_path(tmp_path / "memory.md")
    path.write_text("", encoding="utf-8")
    Path(f"{path}.lock").mkdir()
    ledger = ReviewLedger(tmp_path / "memory.md")

    with pytest.raises(ReviewLedgerError):
        ledger.append("approve", *KEY, "0" * 64)
    assert path.read_text(encoding="utf-8") == ""


@pytest.mark.unit
def test_an_append_refuses_to_write_over_a_corrupt_ledger(tmp_path):
    path = review_ledger_path(tmp_path / "memory.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n", encoding="utf-8")
    ledger = ReviewLedger(tmp_path / "memory.md")

    with pytest.raises(ReviewLedgerError):
        ledger.append("approve", *KEY, "0" * 64)

    assert path.read_text(encoding="utf-8") == "not json\n"
