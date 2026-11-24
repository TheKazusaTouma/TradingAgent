"""Local commands for reviewing settled memory reflections."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import (
    ReviewLedger,
    ReviewLedgerError,
    ReviewSnapshot,
    TradingMemoryLog,
    reflection_fingerprint,
)

memory_app = typer.Typer(
    help="Review settled memory reflections before a run may use them.",
    no_args_is_help=True,
)


def _log_path(log: Path | None) -> Path:
    return Path(log) if log is not None else Path(DEFAULT_CONFIG["memory_log_path"])


def _fail(message: str, code: int = 1):
    console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code=code)


def _find_entry(log: TradingMemoryLog, ticker: str, decision_date: str) -> dict | None:
    for entry in log.load_entries():
        if entry["ticker"].upper() == ticker.upper() and entry["date"] == decision_date:
            return entry
    return None


def _status(entry: dict, snapshot: ReviewSnapshot) -> str:
    if entry["pending"]:
        return "pending"
    return snapshot.status(entry["ticker"], entry["date"], entry.get("reflection", ""))


@memory_app.command("list")
def list_entries(
    log: Annotated[Path | None, typer.Option("--log", help="Memory log to inspect (default: the configured one)")] = None,
):
    """List settled reflections and whether each is approved."""
    path = _log_path(log)
    memory_log = TradingMemoryLog({"memory_log_path": str(path)})
    entries = memory_log.load_entries()
    if not entries:
        console.print("No memory entries.")
        return
    try:
        snapshot = ReviewLedger(path).snapshot()
    except ReviewLedgerError as exc:
        _fail(str(exc))
    statuses = [(entry, _status(entry, snapshot)) for entry in entries]
    for entry, status in statuses:
        console.print(
            f"{entry['ticker']} {entry['date']} {entry['rating']} {status}"
        )


@memory_app.command("approve")
def approve(
    ticker: Annotated[str, typer.Argument(help="Ticker of the settled decision")],
    decision_date: Annotated[str, typer.Argument(help="Decision date, YYYY-MM-DD")],
    log: Annotated[Path | None, typer.Option("--log", help="Memory log to inspect (default: the configured one)")] = None,
):
    """Show a settled reflection and approve exactly that text for later runs."""
    path = _log_path(log)
    memory_log = TradingMemoryLog({"memory_log_path": str(path)})
    entry = _find_entry(memory_log, ticker, decision_date)
    if entry is None:
        _fail(f"No memory entry for {ticker} on {decision_date}.")
    if entry["pending"]:
        _fail(f"The entry for {ticker} on {decision_date} is still pending.")
    if not entry.get("reflection"):
        _fail(f"The entry for {ticker} on {decision_date} has no reflection.")
    console.print(f"Reflection for {entry['ticker']} on {entry['date']}:")
    console.print(entry["reflection"], markup=False, highlight=False)
    try:
        confirmed = typer.confirm(
            f"Approve exactly this reflection for {entry['ticker']} on {entry['date']}?"
        )
    except (typer.Abort, EOFError):
        confirmed = False
    if not confirmed:
        console.print("Not approved; the review ledger is unchanged.")
        return
    try:
        ReviewLedger(path).append(
            "approve",
            entry["ticker"],
            entry["date"],
            reflection_fingerprint(entry["reflection"]),
        )
    except ReviewLedgerError as exc:
        _fail(str(exc))
    console.print(f"[green]Approved {entry['ticker']} on {entry['date']}.[/green]")


@memory_app.command("revoke")
def revoke(
    ticker: Annotated[str, typer.Argument(help="Ticker of the settled decision")],
    decision_date: Annotated[str, typer.Argument(help="Decision date, YYYY-MM-DD")],
    log: Annotated[Path | None, typer.Option("--log", help="Memory log to inspect (default: the configured one)")] = None,
):
    """Withdraw approval for a settled reflection."""
    path = _log_path(log)
    memory_log = TradingMemoryLog({"memory_log_path": str(path)})
    entry = _find_entry(memory_log, ticker, decision_date)
    if entry is None:
        _fail(f"No memory entry for {ticker} on {decision_date}.")
    if entry["pending"] or not entry.get("reflection"):
        _fail(f"The entry for {ticker} on {decision_date} has no settled reflection.")
    try:
        ReviewLedger(path).append(
            "revoke",
            entry["ticker"],
            entry["date"],
            reflection_fingerprint(entry["reflection"]),
        )
    except ReviewLedgerError as exc:
        _fail(str(exc))
    console.print(f"[yellow]Revoked {entry['ticker']} on {entry['date']}.[/yellow]")


@memory_app.command("history")
def history(
    ticker: Annotated[str | None, typer.Argument(help="Only this ticker")] = None,
    decision_date: Annotated[str | None, typer.Argument(help="Only this decision date, YYYY-MM-DD")] = None,
    log: Annotated[Path | None, typer.Option("--log", help="Memory log to inspect (default: the configured one)")] = None,
):
    """Show review events in the order they took effect."""
    try:
        events = ReviewLedger(_log_path(log)).history(ticker, decision_date)
    except ReviewLedgerError as exc:
        _fail(str(exc))
    if not events:
        console.print("No review events.")
        return
    for event in events:
        console.print(
            f"{event.at.isoformat()} {event.action:7} {event.ticker} "
            f"{event.decision_date} {event.reflection_sha256[:12]}"
        )
