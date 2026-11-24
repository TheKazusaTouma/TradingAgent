"""The local review commands: what they show, bind, withdraw and refuse."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import cli.main as m
from tradingagents.memory import (
    ReviewLedger,
    TradingMemoryLog,
    reflection_fingerprint,
    review_ledger_path,
)

KEY = ("NVDA", "2026-01-02")
REFLECTION = "WAIT_FOR_PULLBACK_BEFORE_ADDING"


@pytest.fixture
def runner():
    return CliRunner()


def _settled_log(tmp_path):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    log.store_decision("NVDA", "2026-01-02", "Rating: Buy\n\nBuy NVDA.")
    log.update_with_outcome(
        "NVDA", "2026-01-02", 0.05, 0.02, 5, REFLECTION,
        resolution_date="2026-01-08",
    )
    return log


def _invoke(runner, tmp_path, *args, input=None):
    return runner.invoke(
        m.app, ["memory", *args, "--log", str(tmp_path / "memory.md")], input=input
    )


@pytest.mark.unit
def test_list_reads_the_ledger_once_for_all_entries(runner, tmp_path, monkeypatch):
    log = _settled_log(tmp_path)
    log.store_decision("AAPL", "2026-01-03", "Rating: Hold\n\nHold AAPL.")
    log.update_with_outcome(
        "AAPL", "2026-01-03", 0.01, 0.0, 5, "AAPL_LESSON", resolution_date="2026-01-08",
    )
    ledger = ReviewLedger(tmp_path / "memory.md")
    ledger.append("approve", *KEY, reflection_fingerprint(REFLECTION))
    ledger.append("approve", "AAPL", "2026-01-03", reflection_fingerprint("AAPL_LESSON"))

    reads = []
    real = ReviewLedger.events
    monkeypatch.setattr(ReviewLedger, "events", lambda self: reads.append(1) or real(self))

    result = _invoke(runner, tmp_path, "list")

    assert result.exit_code == 0, result.output
    assert "NVDA 2026-01-02 Buy approved" in result.output
    assert "AAPL 2026-01-03 Hold approved" in result.output
    assert len(reads) == 1


@pytest.mark.unit
def test_approve_shows_the_reflection_then_binds_it_only_after_confirmation(runner, tmp_path):
    _settled_log(tmp_path)

    result = _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n")

    assert result.exit_code == 0, result.output
    assert REFLECTION in result.output
    assert ReviewLedger(tmp_path / "memory.md").approved() == {
        KEY: reflection_fingerprint(REFLECTION)
    }


@pytest.mark.unit
@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_approve_leaves_the_ledger_unchanged_without_confirmation(runner, tmp_path, answer):
    _settled_log(tmp_path)

    result = _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input=answer)

    assert result.exit_code == 0, result.output
    assert REFLECTION in result.output
    assert not review_ledger_path(tmp_path / "memory.md").exists()
    assert ReviewLedger(tmp_path / "memory.md").approved() == {}


@pytest.mark.unit
def test_list_says_whether_each_settled_reflection_is_approved(runner, tmp_path):
    log = _settled_log(tmp_path)

    before = _invoke(runner, tmp_path, "list")
    assert before.exit_code == 0, before.output
    assert "NVDA 2026-01-02 Buy not approved" in before.output

    assert _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n").exit_code == 0
    after = _invoke(runner, tmp_path, "list")
    assert after.exit_code == 0, after.output
    assert "NVDA 2026-01-02 Buy approved" in after.output
    assert "NVDA 2026-01-02 Buy not approved" not in after.output

    log.store_decision("AAPL", "2026-01-03", "Rating: Hold\n\nHold AAPL.")
    pending = _invoke(runner, tmp_path, "list")
    assert pending.exit_code == 0, pending.output
    assert "AAPL 2026-01-03 Hold pending" in pending.output


@pytest.mark.unit
def test_list_tracks_the_reflection_version_not_just_the_decision(runner, tmp_path):
    _settled_log(tmp_path)
    rewritten = "REWRITTEN_AFTER_APPROVAL"
    assert _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n").exit_code == 0

    text = (tmp_path / "memory.md").read_text(encoding="utf-8")
    (tmp_path / "memory.md").write_text(text.replace(REFLECTION, rewritten), encoding="utf-8")
    assert _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n").exit_code == 0
    listed = _invoke(runner, tmp_path, "list")
    assert "NVDA 2026-01-02 Buy approved" in listed.output

    # Restoring R brings R's own earlier approval back, even though R' was the
    # version most recently approved.
    (tmp_path / "memory.md").write_text(text, encoding="utf-8")
    restored = _invoke(runner, tmp_path, "list")
    assert "NVDA 2026-01-02 Buy approved" in restored.output

    # A revoke of R survives a round trip through R'.
    assert _invoke(runner, tmp_path, "revoke", "NVDA", "2026-01-02").exit_code == 0
    (tmp_path / "memory.md").write_text(text.replace(REFLECTION, rewritten), encoding="utf-8")
    (tmp_path / "memory.md").write_text(text, encoding="utf-8")
    assert "NVDA 2026-01-02 Buy not approved" in _invoke(runner, tmp_path, "list").output


@pytest.mark.unit
def test_revoke_withdraws_the_approval_and_history_shows_both_events(runner, tmp_path):
    _settled_log(tmp_path)
    assert _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n").exit_code == 0

    revoked = _invoke(runner, tmp_path, "revoke", "NVDA", "2026-01-02")
    assert revoked.exit_code == 0, revoked.output
    assert ReviewLedger(tmp_path / "memory.md").approved() == {}

    history = _invoke(runner, tmp_path, "history", "NVDA", "2026-01-02")
    assert history.exit_code == 0, history.output
    assert "approve" in history.output and "revoke" in history.output


@pytest.mark.unit
def test_approve_and_revoke_refuse_a_pending_entry(runner, tmp_path):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    log.store_decision("NVDA", "2026-01-02", "Rating: Buy\n\nBuy NVDA.")

    approved = _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n")
    revoked = _invoke(runner, tmp_path, "revoke", "NVDA", "2026-01-02")

    assert approved.exit_code == 1 and "pending" in approved.output.lower()
    assert revoked.exit_code == 1 and "settled reflection" in revoked.output.lower()
    assert ReviewLedger(tmp_path / "memory.md").approved() == {}


@pytest.mark.unit
def test_a_corrupt_ledger_is_reported_and_never_overwritten(runner, tmp_path):
    _settled_log(tmp_path)
    path = review_ledger_path(tmp_path / "memory.md")
    path.write_text("not json\n", encoding="utf-8")

    listed = _invoke(runner, tmp_path, "list")
    approved = _invoke(runner, tmp_path, "approve", "NVDA", "2026-01-02", input="y\n")
    revoked = _invoke(runner, tmp_path, "revoke", "NVDA", "2026-01-02")
    history = _invoke(runner, tmp_path, "history")

    for result in (listed, approved, revoked, history):
        assert result.exit_code == 1
        assert "not valid JSON" in " ".join(result.output.split())
    assert path.read_text(encoding="utf-8") == "not json\n"
