"""One analysis execution's trace: its id, its file, its callback and run events.

``TraceSession`` is created once per attempt by ``propagate`` and by the CLI. It
writes the trace file under ``results_dir/<TICKER>/<date>/trace/<trace_id>.jsonl``
so two runs of the same ticker and date never share a trace file. Run start/end/
fail events are written here; model and tool events are written by the handler it
hands out, which the caller injects into the graph's ``config.callbacks``.

The end of a run (first-version definition) is when the analysis graph and the
required decision recording are done; it does not include any later choice to
save a report.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tradingagents.dataflows.symbols import safe_ticker_component

from .handler import TraceCallbackHandler
from .trace import new_trace_id, trace_scope
from .writer import TraceWriter, iso_now


class TraceSession:
    """The trace of one execution attempt: id, file, callback handler, run events."""

    def __init__(self, results_dir: str | Path, ticker: str, trade_date: str,
                 trace_id: str | None = None) -> None:
        self.trace_id = trace_id or new_trace_id()
        self.path = (Path(results_dir) / safe_ticker_component(ticker) / trade_date
                     / "trace" / f"{self.trace_id}.jsonl")
        self._writer = TraceWriter(self.path)
        self.handler: TraceCallbackHandler | None = None
        self._ended = False

    def callback_handler(self) -> TraceCallbackHandler:
        """This run's callback handler (created once), to add to config.callbacks."""
        if self.handler is None:
            self.handler = TraceCallbackHandler(self._writer.write, trace_id=self.trace_id)
        return self.handler

    def config_metadata(self) -> dict[str, Any]:
        """Metadata to merge into the graph config so callbacks see the trace id."""
        return {"trace_id": self.trace_id}

    @contextlib.contextmanager
    def run(self, **fields: Any) -> Iterator[TraceSession]:
        """Trace one attempt: emit ``run_start`` now, ``run_end`` on clean exit.

        The block must include everything that can fail before the graph runs
        (``create_run_state``, checkpoint setup), so a failure there still leaves
        a ``run_start`` and a ``run_fail`` behind. Any exception is re-raised
        unchanged after ``run_fail``; ``run_end`` is never written on error. The
        trace id is served to ``current_trace_id()`` for the block and reset on
        exit, so a later run does not inherit it.
        """
        with trace_scope(self.trace_id):
            self.start(**fields)
            try:
                yield self
            except BaseException as exc:
                self.fail(exc)
                raise
            self.end()

    def start(self, **fields: Any) -> None:
        self._run_event("run_start", "started", **fields)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._run_event("run_end", "ok")

    def fail(self, error: BaseException) -> None:
        if self._ended:
            return
        self._ended = True
        self._run_event("run_fail", "error", error_type=type(error).__name__)

    def _run_event(self, event: str, status: str, *, error_type: str | None = None,
                   **fields: Any) -> None:
        self._writer.write({
            "ts": iso_now(),
            "trace_id": self.trace_id,
            "event": event,
            "run_id": None,
            "parent_run_id": None,
            "node": None,
            "tool_name": None,
            "duration_ms": None,
            "run_status": status,
            "error_type": error_type,
            **fields,
        })
