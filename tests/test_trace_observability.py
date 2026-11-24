"""Trace ID first version: one trace per attempt, written as its own JSONL file.

These tests drive the real ``TradingAgentsGraph.propagate`` (both ``debug`` paths)
and the real CLI ``run_analysis`` entry, plus the trace primitives directly. A
scripted offline model and the vendor stubs from ``test_graph_end_to_end`` keep
them network-free. Every test asserts the expected events are actually present
before asserting on their content, so an empty trace cannot pass silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _graph
from tradingagents.observability import TraceSession, new_trace_id
from tradingagents.observability.handler import TraceCallbackHandler


@pytest.fixture
def offline(monkeypatch):
    """Network-free vendor stubs (the same ones test_graph_end_to_end uses)."""
    import pandas as pd

    from tradingagents.agents import context
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot

    called: set[str] = set()
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor,
                                lambda *a, _m=method, **k: called.add(_m) or f"{_m} data")
    prices = pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000,
    })
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: called.add("ohlcv") or prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker", lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})())
    context._identity.cache_clear()
    return called


# --- helpers ----------------------------------------------------------------

def _read_events(path) -> list[dict]:
    """Every JSON line of a trace file, parsed."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _trace_file(results_dir, ticker, date):
    """The single trace file a one-run directory holds."""
    trace_dir = Path(results_dir) / ticker / date / "trace"
    assert trace_dir.is_dir(), f"no trace directory under {results_dir}"
    files = list(trace_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one trace file, found {files}"
    return files[0]


def _events(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e["event"] == name]


def _assert_nonempty(events: list[dict]) -> None:
    """Guard every assertion: a trace with no events must fail, not vacuously pass."""
    assert events, "trace file was empty; the run recorded nothing"


# --- real propagate: debug=False (invoke) and debug=True (stream_run) -------

@pytest.mark.unit
@pytest.mark.parametrize("debug", [False, True], ids=["invoke", "stream"])
def test_propagate_writes_its_own_trace_with_run_and_call_events(tmp_path, monkeypatch, offline, debug):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), debug=debug)

    graph.propagate("NVDA", TRADE_DATE)

    path = _trace_file(tmp_path / "results", "NVDA", TRADE_DATE)
    assert path.name == f"{graph.last_trace_id}.jsonl"          # file named by the trace id
    assert len(graph.last_trace_id) == 36                       # a full UUID4
    events = _read_events(path)
    _assert_nonempty(events)

    # Analysis lifecycle.
    assert len(_events(events, "run_start")) == 1
    assert _events(events, "run_end") and not _events(events, "run_fail")
    assert _events(events, "run_start")[0]["run_status"] == "started"

    # Observable model and tool calls, with starts paired to ends by run_id.
    starts = _events(events, "llm_start") + _events(events, "tool_start")
    assert starts, "no model or tool call was traced"
    ended = {e["run_id"] for e in _events(events, "llm_end") + _events(events, "tool_end")}
    errored = {e["run_id"] for e in _events(events, "llm_error") + _events(events, "tool_error")}
    for start in starts:
        assert start["run_id"] in ended | errored, f"start {start} never finished"


@pytest.mark.unit
def test_tool_start_is_not_pre_marked_as_success(tmp_path, monkeypatch, offline):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    graph.propagate("NVDA", TRADE_DATE)

    events = _read_events(_trace_file(tmp_path / "results", "NVDA", TRADE_DATE))
    _assert_nonempty(events)
    tool_starts = _events(events, "tool_start")
    assert tool_starts, "the run made no tool calls"
    assert all(s["run_status"] == "started" for s in tool_starts)
    # The paired end carries the measured duration (monotonic), start does not.
    assert all(s["duration_ms"] is None for s in tool_starts)
    assert any(e["duration_ms"] is not None and e["duration_ms"] >= 0
               for e in _events(events, "tool_end"))


@pytest.mark.unit
def test_the_trace_records_no_prompts_args_responses_or_key(tmp_path, monkeypatch, offline):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    graph.propagate("NVDA", TRADE_DATE)

    text = _trace_file(tmp_path / "results", "NVDA", TRADE_DATE).read_text(encoding="utf-8")
    events = _read_events(_trace_file(tmp_path / "results", "NVDA", TRADE_DATE))
    _assert_nonempty(events)
    # The scripted model's answer text and a tool's argument values must not appear.
    assert "FINAL TRANSACTION PROPOSAL" not in text
    assert "Report." not in text
    for e in events:
        assert "input_str" not in e and "prompt" not in e and "response" not in e and "args" not in e


# --- real propagate: failure ------------------------------------------------

@pytest.mark.unit
def test_a_failed_run_writes_run_fail_and_the_run_returns_the_exception(tmp_path, monkeypatch, offline):
    # fail_at makes the scripted model raise on a later call, mid-run.
    model = ScriptedModel(fail_at=2)
    graph = _graph(tmp_path, monkeypatch, model)

    with pytest.raises(RuntimeError):
        graph.propagate("NVDA", TRADE_DATE)

    events = _read_events(_trace_file(tmp_path / "results", "NVDA", TRADE_DATE))
    _assert_nonempty(events)
    assert _events(events, "run_start")
    fails = _events(events, "run_fail")
    assert fails and fails[0]["run_status"] == "error"
    assert fails[0]["error_type"] == "RuntimeError"        # class name only, not the message
    assert not _events(events, "run_end")


# --- reuse and concurrency -------------------------------------------------

@pytest.mark.unit
def test_a_reused_graph_gives_each_run_a_new_trace_and_no_stale_events(tmp_path, monkeypatch, offline):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())

    graph.propagate("NVDA", TRADE_DATE)
    first_id = graph.last_trace_id
    first_events = _read_events(_trace_file(tmp_path / "results", "NVDA", TRADE_DATE))

    graph.propagate("NVDA", TRADE_DATE)
    second_id = graph.last_trace_id

    assert first_id != second_id                         # a new attempt each time
    first_run_ids = {e["run_id"] for e in first_events if e["run_id"]}
    # Each attempt has its own file; find the new one by id.
    second_path = tmp_path / "results" / "NVDA" / TRADE_DATE / "trace" / f"{second_id}.jsonl"
    first_path = tmp_path / "results" / "NVDA" / TRADE_DATE / "trace" / f"{first_id}.jsonl"
    second_events = _read_events(second_path)
    _assert_nonempty(second_events)
    assert first_path.exists()                           # the earlier trace is kept too
    assert {e["trace_id"] for e in second_events} == {second_id}
    second_run_ids = {e["run_id"] for e in second_events if e["run_id"]}
    assert first_run_ids.isdisjoint(second_run_ids)      # no ids carried over


@pytest.mark.unit
def test_two_concurrent_runs_do_not_mix_their_traces(tmp_path, monkeypatch, offline):
    import threading

    results = []
    errors = []

    def run(tag):
        try:
            # Each thread its own graph/config so the two runs are independent.
            g = _graph(tmp_path / tag, monkeypatch, ScriptedModel())
            g.propagate("NVDA", TRADE_DATE)
            results.append((tag, g.last_trace_id))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 2
    (tag_a, id_a), (tag_b, id_b) = results
    assert id_a != id_b
    for tag, tid in results:
        path = tmp_path / tag / "results" / "NVDA" / TRADE_DATE / "trace" / f"{tid}.jsonl"
        events = _read_events(path)
        _assert_nonempty(events)
        assert {e["trace_id"] for e in events} == {tid}


# --- propagate: setup failures and trace scope ---------------------------

@pytest.mark.unit
def test_propagate_create_run_state_failure_writes_run_start_and_fail(tmp_path, monkeypatch, offline):
    """A create_run_state failure still leaves run_start + run_fail, and re-raises."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())

    def boom(*a, **k):
        raise RuntimeError("identity lookup failed")

    monkeypatch.setattr(graph, "create_run_state", boom)

    with pytest.raises(RuntimeError, match="identity lookup failed"):
        graph.propagate("NVDA", TRADE_DATE)

    path = _trace_file(tmp_path / "results", "NVDA", TRADE_DATE)
    events = _read_events(path)
    _assert_nonempty(events)
    assert len(_events(events, "run_start")) == 1
    fails = _events(events, "run_fail")
    assert fails and fails[0]["error_type"] == "RuntimeError"
    assert not _events(events, "run_end")                      # no fake clean end


@pytest.mark.unit
def test_propagate_checkpoint_init_failure_is_traced(tmp_path, monkeypatch, offline):
    """A begin_checkpoint failure (checkpoint_scope entry) still leaves run_start + run_fail."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), checkpoint_enabled=True)

    def boom(*a, **k):
        raise RuntimeError("checkpoint db unavailable")

    monkeypatch.setattr(graph, "begin_checkpoint", boom)

    with pytest.raises(RuntimeError, match="checkpoint db unavailable"):
        graph.propagate("NVDA", TRADE_DATE)

    events = _read_events(_trace_file(tmp_path / "results", "NVDA", TRADE_DATE))
    _assert_nonempty(events)
    assert _events(events, "run_start")
    assert _events(events, "run_fail") and not _events(events, "run_end")


@pytest.mark.unit
def test_propagate_serves_current_trace_id_and_clears_it(tmp_path, monkeypatch, offline):
    """current_trace_id() is this run's id during the graph, and None afterwards."""
    from tradingagents.observability import current_trace_id

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), debug=True)
    seen = {}
    real_stream = graph.stream_run

    def stream_run(*a, **k):
        seen["id"] = current_trace_id()
        yield from real_stream(*a, **k)

    monkeypatch.setattr(graph, "stream_run", stream_run)

    graph.propagate("NVDA", TRADE_DATE)

    assert seen["id"] == graph.last_trace_id
    assert current_trace_id() is None                          # not leaked


@pytest.mark.unit
def test_trace_context_does_not_leak_after_an_exception(tmp_path, monkeypatch, offline):
    """After a failed propagate, current_trace_id() is cleared."""
    from tradingagents.observability import current_trace_id

    def boom(*a, **k):
        raise RuntimeError("boom")

    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    monkeypatch.setattr(graph, "create_run_state", boom)

    with pytest.raises(RuntimeError):
        graph.propagate("NVDA", TRADE_DATE)

    assert current_trace_id() is None


# --- concurrent writes to one trace file ----------------------------------

@pytest.mark.unit
def test_concurrent_writes_to_one_trace_file_stay_whole_lines(tmp_path):
    """The writer's lock keeps each JSON event to one intact line under threads."""
    import threading

    from tradingagents.observability import TraceWriter

    writer = TraceWriter(tmp_path / "t" / "trace" / "x.jsonl")
    barrier = threading.Barrier(8)

    def emit(n):
        barrier.wait()
        for i in range(50):
            writer.write({"n": n, "i": i, "pad": "x" * (i % 40)})

    threads = [threading.Thread(target=emit, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 50                                 # no lost or merged writes
    for line in lines:
        json.loads(line)                                       # every line is whole JSON
    # No interleaving: every parsed event has its expected integer fields.
    assert all(isinstance(json.loads(line)["n"], int) for line in lines)


@pytest.mark.unit
def test_an_unserialisable_event_is_swallowed_and_warned_about(tmp_path, caplog):
    """A field json cannot encode must not raise; the warning carries no event content."""
    import logging

    from tradingagents.observability import TraceWriter

    writer = TraceWriter(tmp_path / "t" / "trace" / "x.jsonl")
    with caplog.at_level(logging.WARNING):
        writer.write({"secret_field": object(), "note": "sk-live-DEADBEEF"})   # object not JSON-serialisable
        writer.write({"ok": 1})                                                # next write still works

    assert caplog.records != []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the unserialisable event produced no warning"
    # The warning names the type, never the sensitive field/value.
    joined = " ".join(r.getMessage() for r in warnings)
    assert "TypeError" in joined
    assert "sk-live-DEADBEEF" not in joined and "secret_field" not in joined
    # The good event was still written; the bad one was dropped, not half-written.
    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == {"ok": 1}


@pytest.mark.unit
def test_a_session_survives_an_unserialisable_extra_field(tmp_path):
    """Passing a non-serialisable field to a run event does not abort the analysis."""
    session = TraceSession(str(tmp_path), "NVDA", TRADE_DATE)
    with session.run(company_of_interest="NVDA", trade_date=TRADE_DATE, weird=object()):
        pass
    # run_start (with the bad field) is dropped; run_end is still recorded and the
    # session completes without raising.
    events = _read_events(_trace_file(tmp_path, "NVDA", TRADE_DATE))
    _assert_nonempty(events)
    assert _events(events, "run_end")


# --- CLI entry (real run_analysis) ----------------------------------------
class _FakeGraphForTrace:
    """A minimal graph whose streamed run the CLI traces, with config-aware callbacks."""

    def __init__(self, results_dir):
        self.graph = self
        self._results = results_dir
        self.propagator = self
        self._resuming = False
        self.config = {"results_dir": results_dir}

    def create_run_state(self, *a, **k):
        return {"messages": [], "company_of_interest": "NVDA"}

    def get_graph_args(self, callbacks=None):
        return {"config": {"callbacks": list(callbacks or [])}}

    def begin_checkpoint(self, *a, **k):
        return None

    def checkpoint_input(self, state):
        return state

    def clear_checkpoint_on_success(self, *a, **k):
        pass

    def end_checkpoint(self):
        pass

    def record_decision(self, *a, **k):
        pass

    def stream_run(self, graph_input, **kwargs):
        # Fire the run's config callbacks the way LangGraph would, so the trace
        # handler sees a model/tool pair.
        handler_fired = False
        for cb in kwargs.get("config", {}).get("callbacks", []):
            if isinstance(cb, TraceCallbackHandler) and not handler_fired:
                handler_fired = True
                rid = uuid4()
                cb.on_tool_start({"name": "get_stock_data"}, "NVDA", run_id=rid,
                                 parent_run_id=None, metadata={"langgraph_node": "tools"})
                cb.on_tool_end("data", run_id=rid, parent_run_id=None, metadata=None)
        yield [], {"messages": [], "final_trade_decision": "**Rating**: Hold", "final_rating": "Hold"}


class _NullLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _drive_cli(monkeypatch, tmp_path, fake):
    """Run the real ``run_analysis`` against ``fake``; returns (results_dir, buffer, shown)."""
    import cli.main as cli_main
    import cli.run as cli_run
    from cli.display import MessageBuffer
    from cli.models import AnalystType

    results = tmp_path / "results"
    buffer = MessageBuffer()
    shown = []
    fake.config = {"results_dir": str(results)}
    fake._results = str(results)
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: fake)
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", _NullLive)
    monkeypatch.setattr(cli_run, "display_complete_report", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE,
        "asset_type": "stock", "analysts": [AnalystType.MARKET],
    })
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: {
        "data_cache_dir": str(tmp_path / "cache"), "results_dir": str(results),
    })
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")
    monkeypatch.setattr(cli_run.console, "print", lambda *a, **k: shown.append(a))
    return cli_run, results, buffer, shown


@pytest.mark.unit
def test_the_real_cli_run_writes_a_trace_and_shows_the_id(tmp_path, monkeypatch):
    fake = _FakeGraphForTrace(str(tmp_path / "results"))
    cli_run, results, buffer, shown = _drive_cli(monkeypatch, tmp_path, fake)

    cli_run.run_analysis(flags={"save": False, "show": False})

    trace_files = list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))
    assert len(trace_files) == 1
    events = _read_events(trace_files[0])
    _assert_nonempty(events)
    assert _events(events, "run_start") and _events(events, "run_end")
    assert _events(events, "tool_start") and _events(events, "tool_end")   # via config callback
    # The id was shown to the user (System message and/or the completion print).
    shown_text = [str(part) for _, _kind, part in buffer.messages] + [str(a) for a in shown]
    assert any("Trace ID" in text for text in shown_text)


@pytest.mark.unit
def test_the_real_cli_run_serves_current_trace_id_inside_the_run(tmp_path, monkeypatch):
    """Inside the CLI's streamed run, current_trace_id() is this run's id."""
    from tradingagents.observability import current_trace_id

    trace_seen = {}

    fake = _FakeGraphForTrace(str(tmp_path / "results"))

    def bound_stream_run(graph_input, **kwargs):
        # Instance-bound override (assigned to the instance): record the id, yield one state.
        trace_seen["id"] = current_trace_id()
        yield [], {"messages": [], "final_trade_decision": "**Rating**: Hold", "final_rating": "Hold"}

    fake.stream_run = bound_stream_run
    cli_run, results, _buffer, _shown = _drive_cli(monkeypatch, tmp_path, fake)

    cli_run.run_analysis(flags={"save": False, "show": False})

    trace_files = list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))
    assert len(trace_files) == 1
    assert trace_seen["id"] == trace_files[0].stem              # served id == the file name
    assert current_trace_id() is None                           # cleared after the run


@pytest.mark.unit
def test_the_cli_run_fails_cleanly_on_create_run_state_failure(tmp_path, monkeypatch):
    """A create_run_state failure still leaves run_start + run_fail, and re-raises."""

    class _BoomStart(_FakeGraphForTrace):
        def create_run_state(self, *a, **k):
            raise RuntimeError("cannot resolve identity")

    fake = _BoomStart(str(tmp_path / "results"))
    cli_run, results, _buffer, _shown = _drive_cli(monkeypatch, tmp_path, fake)

    with pytest.raises(RuntimeError, match="cannot resolve identity"):
        cli_run.run_analysis(flags={"save": False, "show": False})

    trace_files = list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))
    assert len(trace_files) == 1
    events = _read_events(trace_files[0])
    _assert_nonempty(events)
    assert _events(events, "run_start")
    fails = _events(events, "run_fail")
    assert fails and fails[0]["error_type"] == "RuntimeError"
    assert not _events(events, "run_end")


@pytest.mark.unit
def test_the_cli_run_fails_cleanly_on_checkpoint_init_failure(tmp_path, monkeypatch):
    """A begin_checkpoint failure still leaves run_start + run_fail, and re-raises."""

    class _BoomCheckpoint(_FakeGraphForTrace):
        def begin_checkpoint(self, *a, **k):
            raise RuntimeError("checkpoint db unreadable")

    fake = _BoomCheckpoint(str(tmp_path / "results"))
    cli_run, results, _buffer, _shown = _drive_cli(monkeypatch, tmp_path, fake)

    with pytest.raises(RuntimeError, match="checkpoint db unreadable"):
        cli_run.run_analysis(flags={"save": False, "show": False})

    events = _read_events(list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))[0])
    _assert_nonempty(events)
    assert _events(events, "run_start")
    assert _events(events, "run_fail") and not _events(events, "run_end")


@pytest.mark.unit
def test_the_cli_trace_context_is_cleared_after_a_failure(tmp_path, monkeypatch):
    """The trace context does not leak past a failed run."""
    from tradingagents.observability import current_trace_id

    class _BoomStart(_FakeGraphForTrace):
        def create_run_state(self, *a, **k):
            raise RuntimeError("boom")

    fake = _BoomStart(str(tmp_path / "results"))
    cli_run, _results, _buffer, _shown = _drive_cli(monkeypatch, tmp_path, fake)

    with pytest.raises(RuntimeError):
        cli_run.run_analysis(flags={"save": False, "show": False})

    assert current_trace_id() is None


@pytest.mark.unit
def test_the_cli_run_traces_a_graph_construction_failure(tmp_path, monkeypatch):
    """A failure while building TradingAgentsGraph writes run_start + run_fail, no run_end."""
    import cli.main as cli_main
    import cli.run as cli_run
    from cli.display import MessageBuffer
    from cli.models import AnalystType

    results = tmp_path / "results"

    def boom_graph(*a, **k):
        raise RuntimeError("llm client init failed")

    buffer = MessageBuffer()
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", boom_graph)
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE,
        "asset_type": "stock", "analysts": [AnalystType.MARKET],
    })
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: {
        "data_cache_dir": str(tmp_path / "cache"), "results_dir": str(results),
    })
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")
    from tradingagents.observability import current_trace_id

    with pytest.raises(RuntimeError, match="llm client init failed"):
        cli_run.run_analysis(flags={"save": False, "show": False})

    trace_files = list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))
    assert len(trace_files) == 1
    events = _read_events(trace_files[0])
    _assert_nonempty(events)
    assert len(_events(events, "run_start")) == 1
    fails = _events(events, "run_fail")
    assert fails and fails[0]["error_type"] == "RuntimeError"
    assert not _events(events, "run_end")              # never a fake clean end
    assert current_trace_id() is None                  # scope cleaned up on failure


@pytest.mark.unit
def test_real_cli_to_langgraph_records_model_and_tool_events(tmp_path, monkeypatch, offline):
    """The real CLI -> real TradingAgentsGraph -> LangGraph path records model/tool events.

    Unlike the fake-graph CLI tests, this leaves TradingAgentsGraph and LangGraph in
    place and only scripts the model and the vendors, so it proves the whole chain.
    """
    import copy as _copy

    import cli.main as cli_main
    import cli.run as cli_run
    from cli.display import MessageBuffer
    from cli.models import AnalystType
    from tests.test_graph_end_to_end import _Client
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph

    results = tmp_path / "results"
    # The real graph builds its LLMs through create_tier_client; script them.
    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda config, tier, **k: _Client(ScriptedModel()))
    buffer = MessageBuffer()
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", _NullLive)
    monkeypatch.setattr(cli_run, "display_complete_report", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE,
        "asset_type": "stock", "analysts": [AnalystType.MARKET],
    })

    def _run_config(selections, checkpoint):
        cfg = _copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(results_dir=str(results), data_cache_dir=str(tmp_path / "cache"),
                   memory_log_path=str(tmp_path / "log.md"))
        return cfg

    monkeypatch.setattr(cli_run, "_build_run_config", _run_config)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")

    cli_run.run_analysis(flags={"save": False, "show": False})

    trace_files = list((results / "NVDA" / TRADE_DATE / "trace").glob("*.jsonl"))
    assert len(trace_files) == 1
    events = _read_events(trace_files[0])
    _assert_nonempty(events)

    # The whole chain produced run start/end plus real model and tool events.
    assert _events(events, "run_start") and _events(events, "run_end")
    assert _events(events, "llm_start"), "no model events reached the trace"
    assert _events(events, "tool_start"), "no tool events reached the trace"
    assert {e["trace_id"] for e in events} == {trace_files[0].stem}   # one id throughout

    # Stats and trace coexist without double-recording a logical call: each
    # llm_start/tool_start should appear once per run_id.
    for kind, end in (("llm_start", "llm_end"), ("tool_start", "tool_end")):
        starts = [e["run_id"] for e in _events(events, kind) if e["run_id"]]
        ends = [e["run_id"] for e in _events(events, end) if e["run_id"]]
        assert len(starts) == len(set(starts)), f"duplicate {kind} for one run_id"
        assert len(ends) == len(set(ends)), f"duplicate {end} for one run_id"


# --- write failure is fail-open -------------------------------------------

@pytest.mark.unit
def test_a_trace_that_cannot_be_written_does_not_fail_the_run(tmp_path, caplog):
    """Candidate policy (pending team confirmation): warn and continue."""
    import logging

    # Point the trace at a path under a file (so mkdir fails): a directory that is
    # actually a regular file cannot hold the trace directory.
    blocker = tmp_path / "NVDA"
    blocker.write_text("not a directory")
    session = TraceSession(str(tmp_path), "NVDA", TRADE_DATE)

    with caplog.at_level(logging.WARNING):
        session.start(company_of_interest="NVDA", trade_date=TRADE_DATE)
        session.callback_handler().on_tool_start(
            {"name": "t"}, "x", run_id=uuid4(), parent_run_id=None, metadata={"langgraph_node": "tools"})
        session.end()

    assert session._ended is True                                # the run itself continued
    assert any("trace" in rec.message.lower() for rec in caplog.records)


# --- primitives -----------------------------------------------------------

@pytest.mark.unit
def test_new_trace_id_is_a_full_uuid4():
    a, b = new_trace_id(), new_trace_id()
    assert len(a) == 36 and a != b
    assert a.count("-") == 4


@pytest.mark.unit
def test_start_is_marked_started_and_end_carries_monotonic_duration(tmp_path):
    written = []
    handler = TraceCallbackHandler(written.append, trace_id="tid-1")
    rid = uuid4()
    handler.on_tool_start({"name": "get_news"}, "NVDA", run_id=rid, parent_run_id=None,
                          metadata={"langgraph_node": "tools"})
    handler.on_tool_end("out", run_id=rid, parent_run_id=None, metadata=None)

    start, end = written
    assert start["event"] == "tool_start" and start["run_status"] == "started"
    assert start["tool_name"] == "get_news" and start["node"] == "tools"
    assert end["event"] == "tool_end" and end["run_status"] == "ok"
    assert end["run_id"] == start["run_id"]                      # paired by run_id
    assert end["duration_ms"] is not None and end["duration_ms"] >= 0
    assert end["tool_name"] == "get_news" and end["node"] == "tools"   # recovered from start


@pytest.mark.unit
def test_an_error_event_pairs_by_run_id_and_records_only_the_class(tmp_path):
    written = []
    handler = TraceCallbackHandler(written.append, trace_id="tid-1")
    rid = uuid4()
    handler.on_tool_start({"name": "get_news"}, "NVDA", run_id=rid, parent_run_id=None,
                          metadata={"langgraph_node": "tools"})
    handler.on_tool_error(ValueError("secret sk-123"), run_id=rid, parent_run_id=None, metadata=None)

    err = [e for e in written if e["event"] == "tool_error"][0]
    assert err["run_status"] == "error"
    assert err["error_type"] == "ValueError"
    assert "secret" not in json.dumps(err)                       # no exception message


@pytest.mark.unit
def test_a_missing_end_metadata_still_leaves_duration_for_timing(tmp_path):
    """End events carry metadata=None; duration still comes from the start's monotonic clock."""
    written = []
    handler = TraceCallbackHandler(written.append, trace_id="tid-1")
    rid = uuid4()
    handler.on_chat_model_start({}, [[object()]], run_id=rid, parent_run_id=None,
                               metadata={"langgraph_node": "agent"})
    handler.on_llm_end(object(), run_id=rid, parent_run_id=None, metadata=None)

    end = [e for e in written if e["event"] == "llm_end"][0]
    assert end["node"] == "agent"                                # recovered from the start
    assert end["duration_ms"] is not None
