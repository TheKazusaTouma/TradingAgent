"""Trace ids for one analysis execution attempt.

A trace id names a single execution attempt: every ``TradingAgentsGraph.propagate``
call and every CLI ``run_analysis`` gets its own full UUID4, even when the same
ticker and date are analysed again or a checkpoint is resumed.

The id travels two ways so callbacks can see it however they are reached:

* a :class:`contextvars.ContextVar` — read by callbacks running inside the graph
  (including the parallel analyst subgraphs);
* ``config["metadata"]["trace_id"]`` at the graph invocation, which LangGraph
  copies into callback metadata for nodes that have it.

The ContextVar pattern mirrors ``tradingagents.dataflows.config`` (``run_config``),
which already carries the run's config into tools and subgraphs the same way.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

_trace_id: ContextVar[str | None] = ContextVar("tradingagents_trace_id", default=None)


def new_trace_id() -> str:
    """A fresh trace id for one execution attempt (a full UUID4)."""
    return str(uuid.uuid4())


def current_trace_id() -> str | None:
    """The trace id of the execution in progress, if any."""
    return _trace_id.get()


@contextlib.contextmanager
def trace_scope(trace_id: str) -> Iterator[str]:
    """Serve ``trace_id`` to every :func:`current_trace_id` inside the block."""
    token = _trace_id.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id.reset(token)
