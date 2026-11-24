"""Append trace events as JSON lines to this run's own file.

One file per execution attempt, so two runs of the same ticker and date never
share a trace file (they still share ``message_tool.log`` and the state log; this
isolation covers the trace file only).

Concurrency: within one run, LangGraph runs the analysts in parallel threads and
their model/tool callbacks all write through the same :class:`TraceWriter`. A
per-writer :class:`threading.Lock` serialises those writes so two events never
interleave into a torn line. This covers threads in one process; it does not add
cross-process locking (each attempt has its own file, so separate processes do
not contend on the same path).

Writes are best-effort: a trace that cannot be written must never fail the
analysis. The candidate policy (pending team confirmation) is to warn and keep
going.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def iso_now() -> str:
    """The current instant as an ISO-8601 string in UTC, for event timestamps."""
    return datetime.now(UTC).isoformat()


class TraceWriter:
    """Appends trace events to ``path`` as JSON lines; failures warn, never raise.

    A per-instance lock serialises writes from the run's parallel threads so each
    event lands as one whole line.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        """Write one event line. Any failure (serialise or write) is logged and swallowed.

        Serialisation runs inside the guard too: an event with a field ``json``
        cannot encode (e.g. a non-serialisable object) must not raise into the
        analysis. The warning names only the path and the exception's type, never
        the event's content, so nothing sensitive is logged.
        """
        with self._lock:
            try:
                line = json.dumps(event, ensure_ascii=False)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception as exc:  # trace must never break the run
                # Class name only: str(exc) (and the event) could carry sensitive data.
                logger.warning(
                    "Could not record trace event in %s: %s", self.path, type(exc).__name__
                )
