"""A LangChain callback that records start/end/error events for one trace.

It pairs a call's start with its end or error by LangChain's ``run_id``: end and
error callbacks arrive with ``metadata=None`` (verified), so the node and tool
name and the monotonic start time are remembered from the matching start event,
keyed by ``run_id``. Durations use ``time.monotonic`` (immune to wall-clock
changes); the event timestamp is a separate UTC wall-clock value.

It records no prompt, tool argument, model response, API key or exception text:
only the exception's type name, and tool/LLM names already public in the graph.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from .trace import current_trace_id
from .writer import iso_now

logger = logging.getLogger(__name__)


class TraceCallbackHandler(BaseCallbackHandler):
    """Write this run's model and tool start/end/error events to a trace file."""

    def __init__(self, write: Callable[[dict[str, Any]], None], trace_id: str | None = None) -> None:
        super().__init__()
        self._write = write
        self._trace_id = trace_id
        # run_id -> (monotonic start, node, tool_name) for pairing end/error events.
        self._open: dict[str, tuple[float, str | None, str | None]] = {}

    # --- helpers ---------------------------------------------------------

    def _trace(self) -> str | None:
        return self._trace_id or current_trace_id()

    def _emit(self, event: str, *, run_id: UUID | str | None, parent_run_id: UUID | str | None,
              node: str | None = None, tool_name: str | None = None,
              duration_ms: float | None = None, status: str = "ok",
              error_type: str | None = None) -> None:
        self._write({
            "ts": iso_now(),
            "trace_id": self._trace(),
            "event": event,
            "run_id": _s(run_id),
            "parent_run_id": _s(parent_run_id),
            "node": node,
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "run_status": status,
            "error_type": error_type,
        })

    @staticmethod
    def _node(metadata: dict | None) -> str | None:
        # Metadata is only present on start events; end/error pass None.
        return (metadata or {}).get("langgraph_node")

    # --- LLM -------------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self._start("llm_start", kwargs, tool_name=None)

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self._start("llm_start", kwargs, tool_name=None)

    def on_llm_end(self, response, **kwargs) -> None:
        self._finish("llm_end", kwargs, "ok", None)

    def on_llm_error(self, error, **kwargs) -> None:
        self._finish("llm_error", kwargs, "error", type(error).__name__)

    # --- tools -----------------------------------------------------------

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        name = (serialized or {}).get("name")
        self._start("tool_start", kwargs, tool_name=name)

    def on_tool_end(self, output, **kwargs) -> None:
        self._finish("tool_end", kwargs, "ok", None)

    def on_tool_error(self, error, **kwargs) -> None:
        self._finish("tool_error", kwargs, "error", type(error).__name__)

    # --- shared start/finish --------------------------------------------

    def _start(self, event: str, kwargs: dict, *, tool_name: str | None) -> None:
        run_id = kwargs.get("run_id")
        node = self._node(kwargs.get("metadata"))
        if run_id is not None:
            self._open[str(run_id)] = (time.monotonic(), node, tool_name)
        # A start is not yet a success: status stays "started".
        self._emit(event, run_id=run_id, parent_run_id=kwargs.get("parent_run_id"),
                   node=node, tool_name=tool_name, status="started")

    def _finish(self, event: str, kwargs: dict, status: str, error_type: str | None) -> None:
        run_id = kwargs.get("run_id")
        started = self._open.pop(str(run_id), None) if run_id is not None else None
        duration_ms = round((time.monotonic() - started[0]) * 1000, 3) if started else None
        node = started[1] if started else None
        tool_name = started[2] if started else None
        self._emit(event, run_id=run_id, parent_run_id=kwargs.get("parent_run_id"),
                   node=node, tool_name=tool_name, duration_ms=duration_ms,
                   status=status, error_type=error_type)


def _s(value: UUID | str | None) -> str | None:
    return None if value is None else str(value)
