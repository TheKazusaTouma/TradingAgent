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
        # Guard every entry mutation/clone: concurrent callers can read the same
        # expired entry and both try to ``del`` it, which raised KeyError without
        # this lock. The lock covers only the dict operations, never a provider
        # load, so it does not serialise the downloads themselves.
        self._lock = threading.Lock()

    def _is_fresh(self, stored_at: float, now: float) -> bool:
        return (now - stored_at) <= self.ttl

    def get(self, key: tuple, now: float) -> pd.DataFrame | None:
        """The cached frame for ``key`` as an independent copy, or ``None``.

        A missing, evicted, or expired entry is a miss (and an expired one is
        dropped). A hit renews the entry's recency for LRU eviction.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, frame = entry
            if not self._is_fresh(stored_at, now):
                # Drop under the lock so a concurrent reader cannot del it again.
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return frame.copy()

    def store(self, key: tuple, frame: pd.DataFrame, now: float) -> None:
        """Store a copy of ``frame`` for ``key``, evicting the LRU entry if full."""
        with self._lock:
            self._entries[key] = (now, frame.copy())
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry (used on a process-local reset)."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_STAT_FIELDS = (
    "memory_hits",
    "disk_hits",
    "coalesced_waits",
    "load_attempts",
    "load_failures",
)


class CacheStats:
    """Thread-safe, process-local counters for the Yahoo cache paths.

    Every counter is advanced under one lock so a snapshot is a consistent
    point-in-time view. The counters describe *outcomes*, not provider retries:

    * ``load_attempts`` — a caller that actually began a data-source load (the
      leader of an in-flight flight whose recheck missed). A single shared load
      counts once even when ``yf_retry`` retries the provider internally.
    * ``load_failures`` — a shared load that raised. Counted once per load, not
      once per caller, even when every waiter receives the same error.
    * ``coalesced_waits`` — a follower that joined an in-flight flight and
      waited, whether it ultimately got the shared result or the shared error.
    * ``memory_hits`` / ``disk_hits`` — a request served from the in-memory
      range cache or the on-disk five-year CSV without starting a load.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(_STAT_FIELDS, 0)

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts = dict.fromkeys(_STAT_FIELDS, 0)


# One process-local statistics object shared by both Yahoo cache paths.
STATS = CacheStats()


@dataclass
class _Flight:
    """One provider load other callers can wait on and share.

    The leader (the caller that created the flight) runs the load and records
    its outcome here; followers block on ``done`` and then copy ``result`` or
    re-raise ``error``. ``waiters`` counts followers currently parked on the
    flight, so a test can confirm a second caller actually joined it. ``loaded``
    is True only when the leader actually invoked the data source; a leader that
    resolved from a cache recheck leaves it False.

    ``follow_up`` links a forced refresh to the cache-only flight it abandoned:
    when an ordinary flight resolves from a cache recheck, the forced callers
    that joined it share ONE follow-up flight (created on demand and attached
    here), so a late resumer can still reach that follow-up's result or error
    through the original flight instead of starting a fresh load.
    """

    done: threading.Event = field(default_factory=threading.Event)
    result: pd.DataFrame | None = None
    error: BaseException | None = None
    waiters: int = 0
    loaded: bool = False
    follow_up: _Flight | None = None


class _Inflight:
    """Coalesce concurrent loads for the same key into one provider call.

    :meth:`run` guarantees every caller for a key either performs the one load
    (the first to arrive) or waits for and shares its outcome, then runs its own
    ``postprocess`` on an independent copy. A failed load wakes all waiters with
    the same error and clears the key, so a later call retries from scratch.
    Different keys occupy separate flights and load independently.
    """

    def __init__(self, stats: CacheStats | None = None):
        self._lock = threading.Lock()
        self._flights: dict[tuple, _Flight] = {}
        # Followers recorded here count even if they get the shared result or error.
        self._stats = stats if stats is not None else STATS

    def run(self, key: tuple, loader, postprocess=None, *, timeout: float | None = None,
            require_fresh: bool = False):
        """Run ``loader()`` once per key across concurrent callers.

        ``loader`` returns ``(frame, loaded)``: ``loaded`` is True only when the
        loader actually called the data source (False when it resolved from a
        cache recheck). ``postprocess`` (optional) turns the raw frame into this
        caller's result on a per-caller copy. On the first (leader) call the load
        runs inline; followers wait up to ``timeout``. A leader failure
        propagates to every follower.

        ``require_fresh`` (a forced refresh): if this caller joins a flight whose
        leader resolved *only* from a cache recheck (``loaded`` False), it must
        not accept that cached result. Instead the forced callers that joined that
        flight share ONE follow-up flight, attached to the original flight so a
        late resumer still reaches the same result/error (never a fresh load while
        a sibling follow-up exists). The follow-up runs ``loader`` (which, being
        forced, does a real provider load) with same-key coalescing preserved.
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
            self._stats.incr("coalesced_waits")
            try:
                finished = flight.done.wait(timeout)
            finally:
                with self._lock:
                    flight.waiters -= 1
            if not finished:
                raise TimeoutError(f"timed out waiting for in-flight load {key!r}")
            if flight.error is not None:
                raise flight.error
            # A forced caller must not accept a result the leader served from a
            # cache recheck (loaded=False). Join (or lead) the single follow-up
            # tied to this flight rather than starting an independent load.
            if require_fresh and not flight.loaded:
                return self._join_follow_up(key, flight, loader, postprocess, timeout)
            result = flight.result.copy()
            return postprocess(result) if postprocess else result

        return self._lead(key, flight, loader, postprocess)

    def _lead(self, key: tuple, flight: _Flight, loader, postprocess):
        """Run ``loader`` as ``flight``'s leader (outside the lock)."""
        try:
            result, loaded = loader()
        except BaseException as exc:  # noqa: BLE001 — released to every waiter as-is
            with self._lock:
                flight.error = exc
                # Only remove if this exact flight still owns the key (a follow-up
                # may have replaced it); else leave the newer one in place.
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                flight.done.set()
            raise
        with self._lock:
            flight.result = result.copy()
            flight.loaded = loaded
            if self._flights.get(key) is flight:
                self._flights.pop(key, None)
            flight.done.set()
        return postprocess(result.copy()) if postprocess else result

    def _join_follow_up(self, key, original: _Flight, loader, postprocess, timeout):
        """Join or lead the single follow-up refresh tied to ``original``.

        Created on demand under the lock and linked via ``original.follow_up`` so
        every forced caller that abandoned ``original`` shares one follow-up (its
        result *or* error), and a late resumer reaches it instead of reloading.

        If another same-key provider load already owns the key (e.g. a separate
        forced request started first and is blocked in the provider), reuse *that*
        active flight instead of starting a parallel follow-up load: the follow-up
        is pointed at it and this caller waits on it. That keeps one provider call
        per key and leaves cleanup to the flight's own leader.
        """
        with self._lock:
            follow = original.follow_up
            leader = follow is None
            if leader:
                existing = self._flights.get(key)
                if existing is not None:
                    # A same-key load is already in progress: share it rather than
                    # load in parallel. Link it as this flight's follow-up so late
                    # resumers reach the same outcome, and join as a follower.
                    original.follow_up = existing
                    existing.waiters += 1
                    follow = existing
                    leader = False
                else:
                    follow = _Flight()
                    original.follow_up = follow
                    # Register the follow-up under the key so a brand-new same-key
                    # caller coalesces onto the in-progress refresh instead of
                    # loading in parallel (same-key coalescing preserved).
                    self._flights[key] = follow
            else:
                follow.waiters += 1

        if not leader:
            self._stats.incr("coalesced_waits")
            try:
                finished = follow.done.wait(timeout)
            finally:
                with self._lock:
                    follow.waiters -= 1
            if not finished:
                raise TimeoutError(f"timed out waiting for follow-up load {key!r}")
            if follow.error is not None:
                raise follow.error
            result = follow.result.copy()
            return postprocess(result) if postprocess else result

        return self._lead(key, follow, loader, postprocess)

    def waiter_count(self, key: tuple) -> int:
        """Followers currently waiting on ``key`` (test/debug hook)."""
        with self._lock:
            flight = self._flights.get(key)
            return flight.waiters if flight is not None else 0

    def inflight_keys(self) -> tuple:
        """Keys with a load in progress (test/debug hook)."""
        with self._lock:
            return tuple(self._flights)
