"""The whole graph, end to end, with scripted models and no network.

Every tool-using analyst calls each of its tools once through the vendor router,
the debates and managers run, and the decision is parsed and logged. This pins
the wiring: a restructure that drops a node, a tool or an edge fails here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from tradingagents.agents import context, schemas
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.graph.checkpointer import checkpoint_step
from tradingagents.memory import TradingMemoryLog

TRADE_DATE = "2026-01-09"

# Enough for every free-text reader: the PM's labelled rating and the trader's
# closing proposal line.
TEXT = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"

STRUCTURED = {
    schemas.ResearchPlan: schemas.ResearchPlan(
        recommendation=schemas.PortfolioRating.OVERWEIGHT, rationale="r", strategic_actions="a"),
    schemas.TraderProposal: schemas.TraderProposal(action=schemas.TraderAction.BUY, reasoning="r"),
    # The thesis quotes another party's rating; the decision is still the PM's own.
    schemas.PortfolioDecision: schemas.PortfolioDecision(
        rating=schemas.PortfolioRating.OVERWEIGHT, executive_summary="s",
        investment_thesis="Street consensus rating: Buy (28 of 35 analysts)."),
    schemas.SentimentReport: schemas.SentimentReport(
        overall_band=schemas.SentimentBand.NEUTRAL, overall_score=5.0, confidence="low", narrative="n"),
}

ARGS = {"symbol": "NVDA", "ticker": "NVDA", "curr_date": TRADE_DATE, "start_date": "2026-01-02",
        "end_date": TRADE_DATE, "indicator": "rsi", "topic": "Fed rate cut", "freq": "quarterly"}


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, then answers with TEXT."""

    structured: bool = False
    tools: tuple = ()
    calls: list = Field(default_factory=list)   # shared across bound copies
    threads: set = Field(default_factory=set)   # threads that served a tool-bound call
    fail_at: int | None = None                  # raise on this call, once

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        if not self.structured:
            raise NotImplementedError
        return RunnableLambda(lambda _: self._count() or STRUCTURED[schema])

    def _count(self) -> None:
        self.calls.append(1)
        if len(self.calls) == self.fail_at:
            raise RuntimeError("provider unavailable")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._count()
        if self.tools:
            import threading
            import time
            self.threads.add(threading.current_thread().name)
            time.sleep(0.05)   # long enough for concurrent analysts to overlap
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = [{"name": t.name, "id": f"call_{i}",
                      "args": {k: v for k, v in ARGS.items()
                               if k in t.tool_call_schema.model_json_schema()["properties"]}}
                     for i, t in enumerate(self.tools)]
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content=TEXT)
        return ChatResult(generations=[ChatGeneration(message=message)])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Every vendor answers offline; returns the set of router methods called."""
    called: set[str] = set()
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor,
                                lambda *a, _m=method, **k: called.add(_m) or f"{_m} data")
    prices = pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000,
    })
    monkeypatch.setattr(snapshot, "load_ohlcv",
                        lambda *a, **k: called.add("ohlcv") or prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker", lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})())
    context._identity.cache_clear()
    return called


def _graph(tmp_path, monkeypatch, model, debug=False, **config):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"), **config)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    return trading_graph.TradingAgentsGraph(config=cfg, debug=debug)


REVIEW_REFLECTION = "REFLECTION_MARKER_WAIT_FOR_PULLBACK"
REVIEW_TICKER = "NVDA"
REVIEW_DECISION_DATE = "2026-01-02"
REVIEW_RESOLUTION_DATE = "2026-01-08"


def _review_ledger_path(log_path: Path) -> Path:
    return Path(log_path).with_suffix(".reviews.jsonl")


def _reflection_fingerprint(reflection: str) -> str:
    return hashlib.sha256(reflection.encode("utf-8")).hexdigest()


def _write_settled_memory(log_path: Path, reflection: str = REVIEW_REFLECTION) -> None:
    log_path.write_text(
        f"[{REVIEW_DECISION_DATE} | {REVIEW_TICKER} | Buy | +5.0% | +2.0% | 5d | "
        f"resolved:{REVIEW_RESOLUTION_DATE}]\n\n"
        f"DECISION:\nRating: Buy\n\nBuy {REVIEW_TICKER}.\n\n"
        f"REFLECTION:\n{reflection}"
        f"{TradingMemoryLog._SEPARATOR}",
        encoding="utf-8",
    )


def _ledger_line(action: str, at: str, fingerprint: str) -> str:
    return json.dumps({
        "ticker": REVIEW_TICKER,
        "decision_date": REVIEW_DECISION_DATE,
        "reflection_sha256": fingerprint,
        "action": action,
        "at": at,
    })


def _write_ledger(log_path: Path, lines: list[str]) -> None:
    _review_ledger_path(log_path).write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


def _append_ledger(log_path: Path, line: str) -> None:
    with _review_ledger_path(log_path).open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")


def _pm_prompts(model) -> list[str]:
    return [prompt for step, prompt, *_ in model.seen if step == "Portfolio Manager"]


@pytest.mark.unit
@pytest.mark.parametrize("structured", [False, True], ids=["free-text", "structured"])
def test_a_full_run_reaches_a_logged_decision(tmp_path, monkeypatch, offline, structured):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(structured=structured))

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == state["final_rating"] == "Overweight"
    for key in ("market_report", "sentiment_report", "news_report", "fundamentals_report",
                "investment_plan", "trader_investment_plan", "final_trade_decision"):
        assert state[key].strip(), key
    tool_methods = {"get_stock_data", "get_indicators", "get_news", "get_global_news",
                    "get_macro_indicators", "get_prediction_markets", "get_fundamentals",
                    "get_balance_sheet", "get_cashflow", "get_income_statement",
                    "get_insider_transactions", "ohlcv"}
    assert offline == tool_methods
    assert [e["rating"] for e in graph.memory_log.load_entries()] == ["Overweight"]


@pytest.mark.unit
def test_an_interrupted_run_resumes_from_its_checkpoint(tmp_path, monkeypatch, offline):
    model = ScriptedModel(fail_at=12)            # past the analysts, before the decision
    graph = _graph(tmp_path, monkeypatch, model, checkpoint_enabled=True)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        graph.propagate("NVDA", TRADE_DATE)
    calls_before = len(model.calls)

    _, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    resumed_calls = len(model.calls) - calls_before
    full_run = ScriptedModel()
    _graph(tmp_path / "fresh", monkeypatch, full_run).propagate("NVDA", TRADE_DATE)
    # The resumed run makes only the calls the interrupted one had not completed.
    assert resumed_calls == len(full_run.calls) - (model.fail_at - 1)


@pytest.mark.unit
def test_a_graph_reused_across_runs_keeps_no_run_state(tmp_path, monkeypatch, offline):
    """A backtest reuses one graph over its whole grid; holding every run's full
    state would grow without bound."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    for trade_date in ("2026-01-08", TRADE_DATE):
        graph.propagate("NVDA", trade_date)

    held = [v for v in vars(graph).values() if isinstance(v, dict) and TRADE_DATE in v]
    assert held == []
    assert len(list(tmp_path.glob("results/NVDA/TradingAgentsStrategy_logs/*.json"))) == 2


@pytest.mark.unit
def test_the_analysts_run_at_the_same_time(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    graph = _graph(tmp_path, monkeypatch, model)

    assert not [n for n in graph.graph.get_graph().nodes if n.startswith("Msg Clear")]
    graph.propagate("NVDA", TRADE_DATE)

    assert len(model.threads) > 1


@pytest.mark.unit
def test_a_debug_run_prints_the_analysts_work_and_reaches_the_same_decision(tmp_path, monkeypatch, offline, capsys):
    """Debug mode streams the analysts' own graphs, so their tool calls still print."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), debug=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    assert state["market_report"].strip() and state["fundamentals_report"].strip()
    printed = capsys.readouterr().out
    assert "get_stock_data" in printed and "get_balance_sheet" in printed


@pytest.mark.unit
def test_each_report_streams_as_soon_as_its_analyst_files_it(tmp_path, monkeypatch, offline):
    """The main state takes the analysts' reports only when the slowest one is
    done; the CLI shows each report, and stops each clock, as it lands."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    reports = ("market_report", "sentiment_report", "news_report", "fundamentals_report")

    first = next(state for _, state in graph.stream_run(graph.create_run_state("NVDA", TRADE_DATE),
                                                        **graph.propagator.get_graph_args())
                 if state and any(state.get(k) for k in reports))

    assert sum(bool(first.get(k)) for k in reports) == 1


@pytest.mark.unit
def test_a_streamed_run_reads_its_own_graph_config(tmp_path, monkeypatch, offline):
    """The CLI streams the run; a process-wide config set elsewhere must not reach
    its tools, and the graph's config must not reach the caller between steps."""
    from tradingagents.dataflows.config import get_config, set_config

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), output_language="French")
    set_config({"output_language": "German"})
    seen = []
    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "yfinance",
                        lambda *a, **k: seen.append(get_config()["output_language"]) or "prices")

    between = [get_config()["output_language"]
               for _ in graph.stream_run(graph.create_run_state("NVDA", TRADE_DATE), **graph.propagator.get_graph_args())]

    assert seen and set(seen) == {"French"}
    assert set(between) == {"German"}


class LoopingModel(ScriptedModel):
    """Calls a tool on every turn it is offered one (#1420)."""

    tool_turns: list = Field(default_factory=list)    # shared across bound copies
    last_turns: list = Field(default_factory=list)    # histories of the turns offered no tools

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if not self.tools:
            self.last_turns.append(messages)
            return super()._generate(messages, stop, run_manager, **kwargs)
        self.tool_turns.append(1)
        tool = self.tools[0]
        call = {"name": tool.name, "id": f"call_{len(self.tool_turns)}",
                "args": {k: v for k, v in ARGS.items()
                         if k in tool.tool_call_schema.model_json_schema()["properties"]}}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


@pytest.mark.unit
def test_an_analyst_that_keeps_calling_tools_writes_its_report_at_the_limit(tmp_path, monkeypatch, offline):
    """A model that does not stop calling tools must not end the run (#1420)."""
    model = LoopingModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, max_tool_rounds=3)

    final_state, rating = graph.propagate("NVDA", TRADE_DATE)

    assert len(model.tool_turns) == 3 * 3   # three tool-using analysts, three rounds each
    for key in ("market_report", "news_report", "fundamentals_report"):
        assert final_state[key] == TEXT
    assert rating == "Overweight"


@pytest.mark.unit
def test_the_last_turn_is_offered_no_tools_and_reads_its_tool_results_as_text(tmp_path, monkeypatch, offline):
    """Offered tools, a model may call one whatever it is told; a history holding
    tool calls may be refused by a provider when no tools are bound."""
    model = LoopingModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, max_tool_rounds=2)

    graph.propagate("NVDA", TRADE_DATE)

    wrap_ups = [h for h in model.last_turns if isinstance(h[-1], HumanMessage) and "tool round" in h[-1].content]
    assert len(wrap_ups) == 3
    for history in wrap_ups:
        assert not any(isinstance(m, ToolMessage) or getattr(m, "tool_calls", None) for m in history)
        assert any("returned]" in str(m.content) for m in history)
        assert "tool rounds are spent" in history[0].content   # the system prompt lists no tools


@pytest.mark.unit
def test_a_tool_limit_the_recursion_limit_cannot_hold_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="max_tool_rounds"):
        _graph(tmp_path, monkeypatch, ScriptedModel(), max_tool_rounds=60, max_recur_limit=100)


class TimedModel(ScriptedModel):
    """ScriptedModel that records each call's graph step, prompt and timing."""

    seen: list = Field(default_factory=list)    # shared across bound copies

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        step = (run_manager.metadata or {}).get("langgraph_node") if run_manager else None
        started = time.monotonic()
        if step in {"Market Analyst", "agent"} or step == "Memory Log":
            time.sleep(0.2)                     # long enough to see what overlaps
        result = super()._generate(messages, stop, run_manager, **kwargs)
        prompt = "\n".join(str(m.content) for m in messages)
        self.seen.append((step, prompt, started, time.monotonic()))
        return result


RESEARCH_PLAN_MARKER = "RESEARCH_PLAN_MARKER_ENTER_ON_PULLBACK"
TRADER_PROPOSAL_MARKER = "TRADER_PROPOSAL_MARKER_SCALE_IN_THIRDS"
RISK_DEBATE_MARKER = "RISK_DEBATE_MARKER_POSITION_SIZE"


class _MaterialMarkerModel(TimedModel):
    """Gives each current-run material its own marker, so a test can prove it
    reached the Portfolio Manager independently of the memory lesson."""

    @staticmethod
    def _answer(content: str) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = super()._generate(messages, stop, run_manager, **kwargs)
        step = (run_manager.metadata or {}).get("langgraph_node") if run_manager else None
        if step == "Research Manager":
            return self._answer(RESEARCH_PLAN_MARKER)
        if step == "Trader":
            return self._answer(TRADER_PROPOSAL_MARKER)
        if step in {"Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"}:
            return self._answer(f"{RISK_DEBATE_MARKER} from {step}")
        if step == "Portfolio Manager":
            return self._answer("**Rating**: Overweight\n\nUse the stronger risk case.")
        return result


@pytest.mark.unit
def test_past_decisions_settle_alongside_the_analysts(tmp_path, monkeypatch, offline):
    from tradingagents.memory import settlement

    model = TimedModel()
    graph = _graph(tmp_path, monkeypatch, model)
    graph.memory_log.store_decision("NVDA", "2025-12-31", "Rating: Buy\n\nBuy NVDA.")
    closes = pd.Series([100.0 + i for i in range(10)], index=pd.bdate_range("2026-01-02", periods=10))
    monkeypatch.setattr(settlement, "get_closes", lambda *a, **k: closes)

    graph.propagate("NVDA", TRADE_DATE)

    reflections = [c for c in model.seen if c[0] == "Memory Log"]
    assert len(reflections) == 1                # settled once, inside the run
    analyst_turns = [c for c in model.seen if c[0] == "agent"]
    assert reflections[0][2] < max(c[3] for c in analyst_turns), "settled before the analysts, not alongside"
    settled = next(e for e in graph.memory_log.load_entries() if e["date"] == "2025-12-31")
    assert not settled["pending"]
    lessons = graph.memory_log.get_past_context("NVDA", as_of=TRADE_DATE)
    assert lessons
    pm_prompt = next(c[1] for c in model.seen if c[0] == "Portfolio Manager")
    assert lessons in pm_prompt


class _FailOnceAtPMModel(_MaterialMarkerModel):
    """Captures the Portfolio Manager prompt, then interrupts the first run."""

    failed_at_pm: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = super()._generate(messages, stop, run_manager, **kwargs)
        step = (run_manager.metadata or {}).get("langgraph_node") if run_manager else None
        if step == "Portfolio Manager" and not self.failed_at_pm:
            self.failed_at_pm = True
            raise RuntimeError("interrupt after Portfolio Manager input")
        return result


@pytest.mark.unit
def test_review_enabled_without_approval_hides_reflection_but_keeps_current_materials(
    tmp_path, monkeypatch, offline
):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    model = _MaterialMarkerModel()
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    pm_prompt = _pm_prompts(model)[-1]
    assert REVIEW_REFLECTION not in pm_prompt
    assert state["investment_plan"] == RESEARCH_PLAN_MARKER
    assert state["trader_investment_plan"] == TRADER_PROPOSAL_MARKER
    assert RISK_DEBATE_MARKER in state["risk_debate_state"]["history"]
    assert RESEARCH_PLAN_MARKER in pm_prompt
    assert TRADER_PROPOSAL_MARKER in pm_prompt
    assert RISK_DEBATE_MARKER in pm_prompt


@pytest.mark.unit
def test_review_disabled_keeps_the_existing_automatic_memory_path(
    tmp_path, monkeypatch, offline
):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    model = _MaterialMarkerModel()
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=False)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    pm_prompt = _pm_prompts(model)[-1]
    assert REVIEW_REFLECTION in pm_prompt
    assert state["investment_plan"] == RESEARCH_PLAN_MARKER
    assert state["trader_investment_plan"] == TRADER_PROPOSAL_MARKER
    assert RISK_DEBATE_MARKER in state["risk_debate_state"]["history"]
    assert RESEARCH_PLAN_MARKER in pm_prompt
    assert TRADER_PROPOSAL_MARKER in pm_prompt
    assert RISK_DEBATE_MARKER in pm_prompt


@pytest.mark.unit
def test_a_corrupt_review_ledger_fails_closed_and_reports_the_reason(
    tmp_path, monkeypatch, offline
):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    fingerprint = _reflection_fingerprint(REVIEW_REFLECTION)
    approve = _ledger_line("approve", "2026-01-09T10:00:00+00:00", fingerprint)
    damaged_revoke = (
        '{"ticker":"NVDA","decision_date":"2026-01-02",'
        f'"reflection_sha256":"{fingerprint}","action":"revoke"'
    )
    _write_ledger(log_path, [approve, damaged_revoke])

    model = _MaterialMarkerModel()
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    pm_prompt = _pm_prompts(model)[-1]
    assert REVIEW_REFLECTION not in pm_prompt
    note = state.get("memory_note", "").lower()
    assert "review ledger" in note
    assert "could not be read" in note
    assert state["investment_plan"] == RESEARCH_PLAN_MARKER
    assert state["trader_investment_plan"] == TRADER_PROPOSAL_MARKER
    assert RISK_DEBATE_MARKER in state["risk_debate_state"]["history"]
    assert RESEARCH_PLAN_MARKER in pm_prompt
    assert TRADER_PROPOSAL_MARKER in pm_prompt
    assert RISK_DEBATE_MARKER in pm_prompt


@pytest.mark.unit
@pytest.mark.parametrize("local_today, utc_now, expect_reflection, label", [
    # Live run: the local calendar is still the analysis date, but UTC has
    # crossed into the next day. A revocation after UTC midnight happened after
    # the historical cutoff but before this run, so it must apply.
    ("2026-01-09", datetime(2026, 1, 10, 0, 30, tzinfo=UTC), False, "live"),
    # Historical replay: the local calendar has moved past the analysis date, so
    # the same revocation is after the analysis date's midnight cutoff and must
    # not affect the replay.
    ("2026-01-12", datetime(2026, 1, 12, 12, 0, tzinfo=UTC), True, "historical"),
])
def test_the_review_cutoff_follows_the_run_mode_when_local_and_utc_dates_differ(
    tmp_path, monkeypatch, offline, local_today, utc_now, expect_reflection, label
):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    fingerprint = _reflection_fingerprint(REVIEW_REFLECTION)
    _write_ledger(log_path, [
        _ledger_line("approve", f"{REVIEW_DECISION_DATE}T10:00:00+00:00", fingerprint),
        _ledger_line("revoke", "2026-01-10T00:30:00+00:00", fingerprint),
    ])
    from tradingagents.dataflows import date_window

    monkeypatch.setattr(date_window, "get_current_date", lambda: local_today)
    monkeypatch.setattr(trading_graph, "get_current_date", lambda: local_today)
    monkeypatch.setattr(trading_graph, "_utc_now", lambda: utc_now)
    model = _MaterialMarkerModel()
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=True)

    state, signal = graph.propagate("NVDA", "2026-01-09")

    assert signal == "Overweight", label
    pm_prompt = _pm_prompts(model)[-1]
    if expect_reflection:
        assert REVIEW_REFLECTION in pm_prompt, label
    else:
        assert REVIEW_REFLECTION not in pm_prompt, label
    assert RESEARCH_PLAN_MARKER in pm_prompt
    assert TRADER_PROPOSAL_MARKER in pm_prompt
    assert RISK_DEBATE_MARKER in pm_prompt


@pytest.mark.unit
def test_one_pm_read_decides_the_run_mode_once(tmp_path, monkeypatch, offline):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    fingerprint = _reflection_fingerprint(REVIEW_REFLECTION)
    _write_ledger(log_path, [_ledger_line("approve", "2026-01-09T10:00:00+00:00", fingerprint)])
    model = _MaterialMarkerModel()

    calls = []
    real = trading_graph.is_historical
    monkeypatch.setattr(
        trading_graph, "is_historical",
        lambda value: calls.append(value) or real(value),
    )
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    assert REVIEW_REFLECTION in _pm_prompts(model)[-1]
    # as_of and the review cutoff are derived from one decision, not two.
    assert calls == [TRADE_DATE]


@pytest.mark.unit
def test_resume_after_revoke_excludes_the_reflection_and_keeps_current_materials(
    tmp_path, monkeypatch, offline
):
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    fingerprint = _reflection_fingerprint(REVIEW_REFLECTION)
    _write_ledger(log_path, [_ledger_line("approve", "2026-01-09T10:00:00+00:00", fingerprint)])

    model = _FailOnceAtPMModel()
    graph = _graph(
        tmp_path, monkeypatch, model,
        memory_review_enabled=True, checkpoint_enabled=True,
    )

    with pytest.raises(RuntimeError, match="interrupt after Portfolio Manager input"):
        graph.propagate("NVDA", TRADE_DATE)

    first_prompts = _pm_prompts(model)
    assert len(first_prompts) == 1
    assert REVIEW_REFLECTION in first_prompts[0]

    signature = graph._run_signature("stock")
    assert checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE, signature) is not None

    _append_ledger(log_path, _ledger_line("revoke", "2026-01-09T12:00:00+00:00", fingerprint))
    calls_before_resume = len(model.seen)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    assert len(model.seen) - calls_before_resume == 1
    assert model.seen[-1][0] == "Portfolio Manager"
    pm_prompts = _pm_prompts(model)
    assert len(pm_prompts) == 2
    assert REVIEW_REFLECTION in pm_prompts[0]
    assert REVIEW_REFLECTION not in pm_prompts[1]
    assert state["investment_plan"] == RESEARCH_PLAN_MARKER
    assert state["trader_investment_plan"] == TRADER_PROPOSAL_MARKER
    assert RISK_DEBATE_MARKER in state["risk_debate_state"]["history"]
    assert RESEARCH_PLAN_MARKER in pm_prompts[1]
    assert TRADER_PROPOSAL_MARKER in pm_prompts[1]
    assert RISK_DEBATE_MARKER in pm_prompts[1]
    assert checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE, signature) is None


@pytest.mark.unit
def test_the_reports_show_the_same_memory_the_portfolio_manager_received(
    tmp_path, monkeypatch, offline
):
    """The reports read the PM's past_context, so they cannot disagree with the
    model input, and building them never re-reads the review ledger."""
    log_path = tmp_path / "log.md"
    _write_settled_memory(log_path)
    fingerprint = _reflection_fingerprint(REVIEW_REFLECTION)
    _write_ledger(log_path, [_ledger_line("approve", "2026-01-09T10:00:00+00:00", fingerprint)])
    model = _MaterialMarkerModel()
    graph = _graph(tmp_path, monkeypatch, model, memory_review_enabled=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == "Overweight"
    pm_prompt = _pm_prompts(model)[-1]
    assert REVIEW_REFLECTION in pm_prompt
    assert state["past_context"]
    assert REVIEW_REFLECTION in state["past_context"]

    # A ledger read after the run would be a second source of truth; make the
    # actual read entry point fail to prove the report uses the PM's
    # written-back context instead.
    monkeypatch.setattr(
        graph.review_ledger, "events",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("report re-read the ledger")),
    )
    from tradingagents.reporting import write_report_tree

    report_dir = tmp_path / "report"
    write_report_tree(state, "NVDA", report_dir)
    markdown = (report_dir / "complete_report.md").read_text()
    html = (report_dir / "complete_report.html").read_text()
    assert "Memory" in markdown and "REFLECTION_MARKER_WAIT_FOR_PULLBACK" in markdown
    assert "REFLECTION_MARKER_WAIT_FOR_PULLBACK" in html


@pytest.mark.unit
def test_a_settlement_failure_is_noted_and_the_run_goes_on(tmp_path, monkeypatch, offline):
    """The analysts have spent their tokens by the time the join waits on settlement."""
    from tradingagents.reporting import write_report_tree

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(structured=True))

    def locked():
        raise OSError("memory log is locked")

    monkeypatch.setattr(graph, "settle_all_pending", locked)
    final_state, rating = graph.propagate("NVDA", TRADE_DATE)

    assert rating == "Overweight"
    assert "could not be settled" in final_state["memory_note"]
    report = write_report_tree(final_state, "NVDA", tmp_path / "report").read_text()
    assert "Memory log: Past decisions could not be settled" in report


@pytest.mark.unit
def test_the_run_settles_other_tickers_due_decisions_too(tmp_path, monkeypatch, offline):
    """A ticker a scheduler stopped analysing still gets its decisions settled (#1445)."""
    from tradingagents.memory import settlement

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(structured=True))
    graph.memory_log.store_decision("MSFT", "2026-01-02", "Rating: Buy\n\nBuy MSFT.")
    closes = pd.Series([100.0 + i for i in range(10)], index=pd.bdate_range("2026-01-02", periods=10))
    monkeypatch.setattr(settlement, "get_closes", lambda *a, **k: closes)

    graph.propagate("NVDA", TRADE_DATE)

    msft = next(e for e in graph.memory_log.load_entries() if e["ticker"] == "MSFT")
    assert not msft["pending"]


@pytest.mark.unit
def test_the_memory_step_reads_lessons_as_of_the_trade_date_and_settles_once(tmp_path, monkeypatch, offline):
    """A past-dated run reads only lessons known by its date (#1251); a resumed run
    that re-runs the step settles nothing twice."""
    from tradingagents.memory import settlement

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(structured=True))
    graph.memory_log.store_decision("NVDA", "2025-12-31", "Rating: Buy\n\nBuy NVDA.")
    closes = pd.Series([100.0 + i for i in range(10)], index=pd.bdate_range("2026-01-02", periods=10))
    monkeypatch.setattr(settlement, "get_closes", lambda *a, **k: closes)
    asked = []
    real = graph.memory_log.get_past_context
    monkeypatch.setattr(graph.memory_log, "get_past_context",
                        lambda ticker, as_of=None, **k: asked.append(as_of) or real(ticker, as_of=as_of, **k))
    state = {"company_of_interest": "NVDA", "trade_date": "2026-01-20"}

    graph._memory_step(state)
    log_after_first = (tmp_path / "log.md").read_text()
    graph._memory_step(state)

    assert asked == ["2026-01-20", "2026-01-20"]
    assert (tmp_path / "log.md").read_text() == log_after_first
