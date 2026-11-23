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

from collections import OrderedDict

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
