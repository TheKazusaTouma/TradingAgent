"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

import os
from datetime import datetime
from pathlib import Path

from tradingagents.agents.rating import run_rating


def analyst_names(keys) -> list[str]:
    """The analysts as users select them: the sentiment analyst's key is "social"."""
    return ["sentiment" if key == "social" else key for key in keys or []]


def _header(ticker: str, final_state: dict, settings: dict | None) -> str:
    """The report's title and what produced it: analysis date, version, models, analysts, vendors."""
    lines = [f"# Trading Analysis Report: {ticker}", ""]
    if final_state.get("trade_date"):
        lines.append(f"- Analysis date: {final_state['trade_date']}")
    if final_state.get("final_trade_decision"):
        lines.append(f"- Rating: {run_rating(final_state)}")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        deep_provider = s("deep_think_provider") or s("llm_provider", "?")
        quick_provider = s("quick_think_provider") or s("llm_provider", "?")
        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {s('deep_think_llm', '?')}, quick {s('quick_think_llm', '?')}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {s('deep_think_llm', '?')}, "
                      f"quick {quick_provider} {s('quick_think_llm', '?')}")
        lines.append(f"- TradingAgents {s('version', '?')}: {models}")
        lines.append(f"- Analysts: {', '.join(analyst_names(s('analysts')))}; "
                     f"research debate rounds {s('max_debate_rounds', '?')}, "
                     f"risk debate rounds {s('max_risk_discuss_rounds', '?')}")
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            lines.append("- Data vendors: " + ", ".join(f"{k} {v}" for k, v in vendors.items()))
    if final_state.get("memory_note"):
        lines.append(f"- Memory log: {final_state['memory_note']}")
    return "\n".join(lines) + "\n\n"


# The report's sections in order: (heading, folder, [(agent, file, state path)]).
_SECTIONS = (
    ("I. Analyst Team Reports", "1_analysts", (
        ("Market Analyst", "market.md", ("market_report",)),
        ("Sentiment Analyst", "sentiment.md", ("sentiment_report",)),
        ("News Analyst", "news.md", ("news_report",)),
        ("Fundamentals Analyst", "fundamentals.md", ("fundamentals_report",)),
    )),
    ("II. Research Team Decision", "2_research", (
        ("Bull Researcher", "bull.md", ("investment_debate_state", "bull_history")),
        ("Bear Researcher", "bear.md", ("investment_debate_state", "bear_history")),
        ("Research Manager", "manager.md", ("investment_plan",)),
    )),
    ("III. Trading Team Plan", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Risk Management Team Decision", "4_risk", (
        ("Aggressive Analyst", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Neutral Analyst", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Portfolio Manager Decision", "5_portfolio", (
        ("Portfolio Manager", "decision.md", ("final_trade_decision",)),
    )),
)


def _report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in _SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key)
            if value:
                written.append((agent, filename, value))
        if written:
            parts.append((heading, folder, written))
    return parts


def contract_validation_block(records: dict | None) -> str:
    """The English ``## Contract Validation`` summary of an enhanced run, or ``""``.

    Payload-free by construction: it lists each agent's outcome, stage, issue codes and the
    call accounting, never model text (FR-6). It returns an empty string when there are no
    records, which is every ``off``-mode run, so the baseline report is unchanged.
    """
    if not records:
        return ""
    from tradingagents.agents.contracts import contract_summary_lines

    lines = contract_summary_lines(records)
    if not lines:
        return ""
    return "## Contract Validation\n\n" + "\n".join(lines)


def write_failed_run_audit(root, ticker: str, trade_date, error, records: dict | None = None) -> Path:
    """Write a failed run's audit next to the reports, clearly labelled as a failure.

    Kept separate from ``write_report_tree`` on purpose: a failed run must never appear as
    a report and must never replace an earlier successful one (ADR-001 section 2.9, T-30).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"contract_validation_failed_{safe_component(ticker)}_{trade_date}.md"
    lines = [
        f"# Contract Validation Failure: {ticker} on {trade_date}",
        "",
        "This run stopped before producing a decision. This file is an audit of the",
        "failure, not a report of a result.",
        "",
        f"- Outcome: failed ({_kind(error)})",
        f"- Stage: {_stage(error)}",
        f"- Agent: {_agent(error)}",
    ]
    issues = getattr(error, "issues", None) or []
    if issues:
        lines.append("- Issues: " + ", ".join(f"{i.field}:{i.code}" for i in issues))
    from tradingagents.agents.contracts import contract_summary_lines

    summary = contract_summary_lines(records)
    if summary:
        lines.extend(["", "## Contract Validation", "", *summary])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _kind(error):
    return getattr(getattr(error, "kind", None), "value", "validation")


def _stage(error):
    return getattr(getattr(error, "stage", None), "value", "unknown")


def _agent(error):
    return getattr(error, "agent", "unknown")


def safe_component(ticker: str) -> str:
    """A ticker made safe to use inside a filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(ticker)) or "unknown"


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True, contract_records: dict | None = None) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419).
    ``contract_records`` are an enhanced-mode run's per-agent validation records; when
    present they add a ``## Contract Validation`` summary. With no records -- every
    ``off``-mode run -- the report is byte-for-byte the baseline report (INV-7).
    Reusing a directory replaces the generated report files, removing sections
    absent from this run and disabled HTML. Other files and directories are kept.
    """
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    parts = _report_parts(final_state)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        content = "\n\n".join(f"### {agent}\n{text}" for agent, _filename, text in written)
        sections.append(f"## {heading}\n\n{content}")

    contract_block = contract_validation_block(contract_records)
    if contract_block:
        sections.append(contract_block)

    (save_path / "complete_report.md").write_text(
        _header(ticker, final_state, settings) + "\n\n".join(sections), encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / "complete_report.html").write_text(
            render_report(ticker, final_state, settings, parts), encoding="utf-8"
        )
    else:
        (save_path / "complete_report.html").unlink(missing_ok=True)

    # Reconcile only the filenames owned by this writer; never clear the tree,
    # since callers may keep notes or other artifacts alongside the reports.
    current_files = {
        (folder, filename)
        for _heading, folder, written in parts
        for _agent, filename, _text in written
    }
    for _heading, folder, agents in _SECTIONS:
        for _agent, filename, _path in agents:
            if (folder, filename) not in current_files:
                (save_path / folder / filename).unlink(missing_ok=True)

    # The integrity manifest is written last, after the stale-file reconciliation, and lists
    # exactly the files this save produced -- never the manifest itself and never a file the
    # caller keeps alongside them (ADR-002 section 5). Paths are handed over as absolute so
    # the contract is unambiguous even when ``save_path`` is relative to the working directory.
    from tradingagents.report_integrity import write_manifest

    base = Path(os.path.abspath(save_path))
    written_files = [base / "complete_report.md"]
    if html:
        written_files.append(base / "complete_report.html")
    written_files.extend(
        base / folder / filename
        for _heading, folder, written in parts
        for _agent, filename, _text in written
    )
    write_manifest(base, ticker, written_files)
    return save_path / "complete_report.md"
