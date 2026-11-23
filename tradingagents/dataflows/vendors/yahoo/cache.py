"""Bounded in-process cache for Yahoo date-range queries.

Yahoo is asked for the same date range repeatedly within one analysis run (the
price tool, retries, parallel analyst subgraphs). This cache keeps the raw
download for a normalized request identity so a repeated query within its TTL
reuses one provider load instead of issuing another.

Only successful loads are stored: the caller stores a frame *after* the load
succeeds, so a failed load leaves no poisoned entry. Frames go in and come out
as copies, so one caller mutating its result cannot corrupt the cache or the
next caller's data. The cache is process-local; a restart clears it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import pandas as pd

# Plan defaults: a small cache holding the handful of ranges one run touches,
# with a TTL matching the five-year cache's intraday refresh window.
DEFAULT_CAPACITY = 64
DEFAULT_TTL_SECONDS = 900


def range_cache_key(
    symbol: str,
    provider: str,
    data_type: str,
    start: str,
    end_exclusive: str,
    options: tuple = (),
) -> tuple:
    """Identity of a raw date-range request.

    Every field that changes the bytes Yahoo returns is part of the key: the
    normalized symbol, the provider, the data type, the actual download window
    (``end_exclusive`` is the exclusive end Yahoo is queried with), and any
    option tuple that affects the raw response. Two requests that agree on all
    of these reuse one entry; any difference stays isolated.
    """
    return (symbol, provider, data_type, start, end_exclusive, tuple(options))


class _RangeCache:
    """A bounded, TTL'd, LRU cache of raw date-range frames.

    ``capacity`` entries at most; the least-recently-used is evicted first.
    ``ttl`` seconds after a store an entry is stale and refetched. Frames are
    isolated on both sides: the stored value is a copy of what the caller
    stored, and every :meth:`get` returns a fresh copy.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, ttl: float = DEFAULT_TTL_SECONDS):
        self.capacity = capacity
        self.ttl = ttl
        # key -> (stored_at, frame); OrderedDict preserves access order for LRU.
        self._entries: OrderedDict[tuple, tuple[float, pd.DataFrame]] = OrderedDict()

    def _is_fresh(self, stored_at: float, now: float) -> bool:
        return (now - stored_at) <= self.ttl

    def get(self, key: tuple, now: float) -> pd.DataFrame | None:
        """The cached frame for ``key`` as an independent copy, or ``None``.

        A missing, evicted, or expired entry is a miss (and an expired one is
        dropped). A hit renews the entry's recency for LRU eviction.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, frame = entry
        if not self._is_fresh(stored_at, now):
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return frame.copy()

    def store(self, key: tuple, frame: pd.DataFrame, now: float) -> None:
        """Store a copy of ``frame`` for ``key``, evicting the LRU entry if full."""
        self._entries[key] = (now, frame.copy())
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry (used on a process-local reset)."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class _Flight:
    """One provider load other callers can wait on and share.

    The leader (the caller that created the flight) runs the load and records
    its outcome here; followers block on ``done`` and then copy ``result`` or
    re-raise ``error``. ``waiters`` counts followers currently parked on the
    flight, so a test can confirm a second caller actually joined it.
    """

    done: threading.Event = field(default_factory=threading.Event)
    result: pd.DataFrame | None = None
    error: BaseException | None = None
    waiters: int = 0


class _Inflight:
    """Coalesce concurrent loads for the same key into one provider call.

    :meth:`run` guarantees every caller for a key either performs the one load
    (the first to arrive) or waits for and shares its outcome, then runs its own
    ``postprocess`` on an independent copy. A failed load wakes all waiters with
    the same error and clears the key, so a later call retries from scratch.
    Different keys occupy separate flights and load independently.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._flights: dict[tuple, _Flight] = {}

    def run(self, key: tuple, loader, postprocess=None, *, timeout: float | None = None):
        """Run ``loader()`` once per key across concurrent callers.

        ``loader`` returns the raw frame; ``postprocess`` (optional) turns that
        raw frame into this caller's result and is applied to a per-caller copy
        so callers can keep distinct date cutoffs / gap handling. On the first
        (leader) call the load runs inline; followers wait up to ``timeout``.
        A leader failure propagates to every follower.
        """
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._flights[key] = flight
            else:
                flight.waiters += 1

        if not leader:
            # The follower registered itself above; always unregister, whether it
            # wakes normally or times out. A timed-out follower must not leave
            # the flight registered with a stale waiter count — the flight stays
            # in ``_flights`` for the still-running leader and any later caller
            # to join, but this follower is no longer waiting on it.
            try:
                finished = flight.done.wait(timeout)
            finally:
                with self._lock:
                    flight.waiters -= 1
            if not finished:
                raise TimeoutError(f"timed out waiting for in-flight load {key!r}")
            if flight.error is not None:
                raise flight.error
            result = flight.result.copy()
            return postprocess(result) if postprocess else result

        # Leader: load outside the lock so other keys are not blocked.
        try:
            result = loader()
        except BaseException as exc:  # noqa: BLE001 — released to every waiter as-is
            with self._lock:
                flight.error = exc
                self._flights.pop(key, None)
                flight.done.set()
            raise
        with self._lock:
            flight.result = result.copy()
            self._flights.pop(key, None)
            flight.done.set()
        return postprocess(result.copy()) if postprocess else result

    def waiter_count(self, key: tuple) -> int:
        """Followers currently waiting on ``key`` (test/debug hook)."""
        with self._lock:
            flight = self._flights.get(key)
            return flight.waiters if flight is not None else 0

    def inflight_keys(self) -> tuple:
        """Keys with a load in progress (test/debug hook)."""
        with self._lock:
            return tuple(self._flights)
