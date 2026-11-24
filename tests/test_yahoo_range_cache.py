"""The Yahoo date-range memory cache: one provider load per request identity.

A repeated range query within the TTL is served from an in-process cache keyed
by the normalized request identity, and the cache holds independent copies so a
caller can never corrupt it or another caller's frame. The five-year OHLCV cache
(tested elsewhere) is unchanged; this file covers only ``get_YFin_data_online``
and the small ``cache._RangeCache`` it uses.
"""
from __future__ import annotations

import os
import threading
import time

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import market
from tradingagents.dataflows.vendors.yahoo.cache import _RangeCache, range_cache_key

# A fixed clock; tests advance it around the TTL to exercise expiry.
FAKE_NOW = 1_000_000.0
TTL = market._range_cache.ttl

# Captured from the unmodified baseline (commit 307d15e) with the fixed frame
# built by ``_frame`` for AAPL 2026-01-02..2026-01-06. Caching must not change
# a single byte of this output.
EXPECTED_CACHE_OFF = (
    "# Stock data for AAPL from 2026-01-02 to 2026-01-06\n"
    "# Total records: 3\n\n"
    "Date,Open,High,Low,Close,Volume,Stock Splits,Dividends\n"
    "2026-01-02,100.00,110.00,90.00,105.00,1000,0.0,0.0\n"
    "2026-01-05,101.00,111.00,91.00,106.00,2000,0.0,0.0\n"
    "2026-01-06,102.00,112.00,92.00,107.50,3000,0.0,0.0\n"
)
# The hand-written header uses LF; pandas writes CSV with the platform newline.
_header, _csv = EXPECTED_CACHE_OFF.split("\n\n", 1)
EXPECTED_CACHE_OFF = _header + "\n\n" + _csv.replace("\n", os.linesep)


def _frame(rows, *, open0=100.0, close0=105.0):
    """A yfinance-shaped frame (DatetimeIndex) with the given ``(date, close)`` rows."""
    idx = pd.DatetimeIndex([d for d, _ in rows], name="Date")
    n = len(rows)
    return pd.DataFrame(
        {
            "Open": [open0 + i for i in range(n)],
            "High": [110.0 + i for i in range(n)],
            "Low": [90.0 + i for i in range(n)],
            "Close": [c for _, c in rows],
            "Volume": [1000 * (i + 1) for i in range(n)],
            "Stock Splits": 0.0,
            "Dividends": 0.0,
        },
        index=idx,
    )


A_WINDOW = ("2026-01-02", "2026-01-06")


class FakeProvider:
    """Records each history() call and returns preset data.

    ``responses`` is a list of frames (or exceptions) consumed in order; the
    last one repeats. This lets a test return different data on a later load.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self._served = 0

    def history(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses[min(self._served, len(self.responses) - 1)]
        self._served += 1
        if isinstance(resp, Exception):
            raise resp
        return resp.copy()

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def clock(monkeypatch):
    """Patch market._now and reset the shared cache before/after each test."""
    now = {"t": FAKE_NOW}
    monkeypatch.setattr(market, "_now", lambda: now["t"])
    market._range_cache.clear()
    yield now
    market._range_cache.clear()


def _use(monkeypatch, provider):
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: provider)
    return provider


# --------------------------------------------------------------------------- #
# CACHE-01: key isolation
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_t1_distinct_symbols_are_isolated(clock, monkeypatch):
    frames = {
        "AAPL": _frame([("2026-01-02", 105.0), ("2026-01-05", 106.0)]),
        "MSFT": _frame([("2026-01-02", 405.0), ("2026-01-05", 406.0)], open0=400.0),
    }
    calls = []

    class Router:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kw):
            calls.append(self.symbol)
            return frames[self.symbol].copy()

    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: Router(symbol))

    out_a = market.get_YFin_data_online("AAPL", *A_WINDOW)
    out_b = market.get_YFin_data_online("MSFT", *A_WINDOW)

    assert "105.00" in out_a and "405.00" not in out_a
    assert "405.00" in out_b and "105.00" not in out_b
    assert len(calls) == 2


@pytest.mark.unit
def test_t2_distinct_windows_are_isolated(clock, monkeypatch):
    jan = _frame([("2026-01-02", 105.0), ("2026-01-05", 106.0)])
    feb = _frame([("2026-02-02", 205.0), ("2026-02-03", 206.0)])

    class Router:
        def history(self, start, end, **kw):
            calls.append((start, end))
            # Serve January rows for a January window, February rows otherwise.
            return (jan if start == "2026-01-02" else feb).copy()

    calls = []
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: Router())

    out_a = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    out_b = market.get_YFin_data_online("AAPL", "2026-02-02", "2026-02-03")

    assert "2026-01-05" in out_a and "2026-02-02" not in out_a
    assert "2026-02-02" in out_b and "2026-01-05" not in out_b
    assert len(calls) == 2


@pytest.mark.unit
def test_t3_key_equality_and_inequality():
    base = range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    same = range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    assert base == same

    # Each structurally distinct field changes the identity.
    assert base != range_cache_key("MSFT", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    assert base != range_cache_key("AAPL", "alpha_vantage", "ohlcv", "2026-01-02", "2026-01-07")
    assert base != range_cache_key("AAPL", "yahoo", "splits", "2026-01-02", "2026-01-07")
    assert base != range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-03", "2026-01-07")
    assert base != range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-08")
    assert base != range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07", ("adj",))


# --------------------------------------------------------------------------- #
# CACHE-02: hits, TTL, refresh, LRU, isolation
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_t4_repeated_hits_within_ttl_make_one_load(clock, monkeypatch):
    provider = _use(monkeypatch, FakeProvider([_frame([("2026-01-02", 105.0)])]))

    outputs = [market.get_YFin_data_online("AAPL", *A_WINDOW) for _ in range(10)]

    assert provider.count == 1
    assert len(set(outputs)) == 1  # every call renders the same


@pytest.mark.unit
def test_t5_expiry_refetches_and_returns_new_values(clock, monkeypatch):
    first = _frame([("2026-01-02", 105.0)])
    second = _frame([("2026-01-02", 999.0)])
    provider = _use(monkeypatch, FakeProvider([first, second]))

    out1 = market.get_YFin_data_online("AAPL", *A_WINDOW)
    clock["t"] += TTL + 1
    out2 = market.get_YFin_data_online("AAPL", *A_WINDOW)

    assert provider.count == 2
    assert "105.00" in out1
    assert "999.00" in out2  # the caller receives the refreshed value


@pytest.mark.unit
def test_t6_explicit_refresh_bypasses_and_repopulates(clock, monkeypatch):
    first = _frame([("2026-01-02", 105.0)])
    second = _frame([("2026-01-02", 999.0)])
    provider = _use(monkeypatch, FakeProvider([first, second]))

    out1 = market.get_YFin_data_online("AAPL", *A_WINDOW)  # cold: load #1
    out2 = market.get_YFin_data_online("AAPL", *A_WINDOW, refresh=True)  # load #2
    assert provider.count == 2
    assert "105.00" in out1
    assert "999.00" in out2

    # A further non-refresh call within the TTL is served from the refreshed entry.
    out3 = market.get_YFin_data_online("AAPL", *A_WINDOW)
    assert provider.count == 2
    assert "999.00" in out3


@pytest.mark.unit
def test_t7_lru_eviction_order(clock, monkeypatch):
    original = market._range_cache
    market._range_cache = _RangeCache(capacity=2)
    try:
        provider = _use(monkeypatch, FakeProvider([_frame([("2026-01-02", 105.0)])]))

        def load(symbol):
            return market.get_YFin_data_online(symbol, "2026-01-02", "2026-01-06")

        load("A")                      # provider #1
        load("B")                      # provider #2
        load("A")                      # hit (A now most-recently-used)
        load("C")                      # evicts LRU = B; provider #3

        # Right after C is inserted: A (recently used) and C remain, B evicted.
        remaining = {key[0] for key in market._range_cache._entries}
        assert remaining == {"A", "C"}

        load("A")                      # hit (A survived)
        load("B")                      # miss (B was evicted); provider #4

        assert provider.count == 4
    finally:
        market._range_cache = original


@pytest.mark.unit
def test_t8_dataframe_isolation_at_cache_layer():
    cache = _RangeCache(capacity=4)
    key = range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    original = _frame([("2026-01-02", 105.0), ("2026-01-05", 106.0)])
    snapshot = original.copy()

    cache.store(key, original, FAKE_NOW)

    # Mutating the original after storing must not affect the cached copy.
    original.loc[:, "Close"] = -1.0
    original.drop(columns=["Volume"], inplace=True)

    got1 = cache.get(key, FAKE_NOW)
    assert got1 is not None
    pd.testing.assert_frame_equal(got1, snapshot)

    # Mutating a returned frame must not affect later reads.
    got1.loc[:, "Close"] = 12345.0
    got1.drop(columns=["Open"], inplace=True)

    got2 = cache.get(key, FAKE_NOW)
    assert got2 is not None
    pd.testing.assert_frame_equal(got2, snapshot)


# --------------------------------------------------------------------------- #
# CACHE-04 (scope: failed load is not cached): retry after failure
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_t9_failed_load_is_not_cached_and_retry_refetches(clock, monkeypatch):
    ok = _frame([("2026-01-02", 105.0)])
    provider = _use(monkeypatch, FakeProvider([VendorUnavailableError("boom"), ok]))

    with pytest.raises(VendorUnavailableError):
        market.get_YFin_data_online("AAPL", *A_WINDOW)

    out = market.get_YFin_data_online("AAPL", *A_WINDOW)

    assert provider.count == 2  # failure did not populate the cache
    assert "105.00" in out


@pytest.mark.unit
def test_t10_output_matches_baseline_cold_and_warm(clock, monkeypatch):
    frame = _frame(
        [("2026-01-02", 105.0), ("2026-01-05", 106.0), ("2026-01-06", 107.5)],
    )
    provider = _use(monkeypatch, FakeProvider([frame]))

    cold = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    warm = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    assert cold == EXPECTED_CACHE_OFF
    assert warm == EXPECTED_CACHE_OFF
    assert provider.count == 1


# --------------------------------------------------------------------------- #
# Regression: range downloads validated before storing / publishing
# --------------------------------------------------------------------------- #

def _stale_frame():
    """A frame whose only usable row is far older than the requested window."""
    idx = pd.DatetimeIndex(["2020-01-02"], name="Date")
    return pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [10.0], "Volume": [1]},
        index=idx,
    )


@pytest.mark.unit
def test_r1_stale_not_cached_then_identical_request_refetches(clock, monkeypatch):
    """A stale first response raises and is NOT cached; an identical request within
    the TTL calls the provider again and returns the fresh data (2 calls)."""
    from tradingagents.dataflows.errors import NoMarketDataError

    fresh = _frame([("2026-01-02", 205.0), ("2026-01-05", 206.0), ("2026-01-06", 207.0)])
    provider = _use(monkeypatch, FakeProvider([_stale_frame(), fresh]))
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")

    with pytest.raises(NoMarketDataError):
        market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    # The stale frame must not have entered the success cache.
    assert market._range_cache.get(key, clock["t"]) is None

    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert provider.count == 2  # refetched; not served from a cached stale entry
    assert "205.00" in out


@pytest.mark.unit
def test_r2_recent_inf_tail_with_old_finite_row_is_rejected(clock, monkeypatch):
    """A recent row with a ±inf close does not count as the latest quote: an older
    finite row behind it is judged stale, so the response is rejected and a later
    identical request refetches and recovers."""
    from tradingagents.dataflows.errors import NoMarketDataError

    # Old finite row (2020) + a recent row (2026-01-06) whose close is inf.
    bad = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
         "Close": [100.0, float("inf")], "Volume": [1, 2]},
        index=pd.DatetimeIndex(["2020-01-02", "2026-01-06"], name="Date"),
    )
    fresh = _frame([("2026-01-02", 205.0), ("2026-01-05", 206.0), ("2026-01-06", 207.0)])
    provider = _use(monkeypatch, FakeProvider([bad, fresh]))

    with pytest.raises(NoMarketDataError):
        market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert provider.count == 2
    assert "205.00" in out
    assert "inf" not in out.lower()  # the recovered output carries no infinite close


# --------------------------------------------------------------------------- #
# Regression: refresh never accepts an ordinary request's cache-recheck result
# --------------------------------------------------------------------------- #

class BlockingProvider:
    """history() blocks the first call until released; records that it entered."""

    def __init__(self, frame):
        self.frame = frame
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def history(self, **kwargs):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5), "provider never released"
        return self.frame.copy()


@pytest.mark.unit
def test_r3_refresh_bypasses_ordinary_recheck_hit(clock, monkeypatch):
    """A forced caller that overlaps an ordinary request whose flight resolves
    only from the cache recheck must still receive freshly fetched data.

    Deterministic setup: the ordinary request's pre-flight get misses, then its
    in-flight recheck returns a cached OLD frame (no provider call) while a
    refresh=True caller overlaps. The refresh must not accept the cached result;
    it goes through the same-key loading path and fetches (close 999).
    """
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    old = _frame([("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)])
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    calls = []

    class Prov:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            calls.append(1)
            return fresh.copy()

    real_get = market._range_cache.get
    tls = threading.local()
    recheck_gate = threading.Event()

    def controlled_get(k, now):
        if k != key:
            return real_get(k, now)
        phase = getattr(tls, "phase", 0)
        tls.phase = phase + 1
        if phase == 0:
            return None  # ordinary pre-flight miss
        if phase == 1:
            recheck_gate.wait(5)  # hold the ordinary in-flight recheck
            return old.copy()     # recheck hit: cached OLD frame, no provider call
        return real_get(k, now)

    monkeypatch.setattr(market._range_cache, "get", controlled_get)
    monkeypatch.setattr(market.yf, "Ticker", Prov)

    res = {}

    def ordinary():
        tls.phase = 0
        res["ord"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    def forced():
        res["for"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)

    t1 = threading.Thread(target=ordinary)
    t1.start()
    time.sleep(0.1)  # let the ordinary request reach its recheck
    t2 = threading.Thread(target=forced)
    t2.start()
    time.sleep(0.1)  # let the refresh overlap the ordinary flight
    recheck_gate.set()
    t1.join(5)
    t2.join(5)
    monkeypatch.setattr(market._range_cache, "get", real_get)

    # The forced caller got freshly fetched data, never the cached 100.
    assert "999.00" in res["for"]
    assert "2026-01-02,100.00,110.00,90.00,100.00" not in res["for"]
    assert len(calls) >= 1  # the refresh triggered a real provider load


@pytest.mark.unit
def test_r4_refresh_joins_active_ordinary_provider_load(clock, monkeypatch):
    """A forced caller that joins an ordinary request's ACTIVE provider load shares
    that one load (same-key coalescing preserved): exactly one provider call, both
    get the fresh result."""
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    provider = BlockingProvider(fresh)
    monkeypatch.setattr(market.yf, "Ticker", lambda s: provider)
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    res = {}

    res_err = {}

    def call(tag, refresh):
        try:
            res[tag] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=refresh)
        except BaseException as exc:  # noqa: BLE001
            res_err[tag] = exc

    t_ord = threading.Thread(target=call, args=("ord", False))
    t_ord.start()
    assert provider.entered.wait(5)
    t_for = threading.Thread(target=call, args=("for", True))
    t_for.start()
    # The forced caller joins the same in-flight load.
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) == 1)
    provider.release.set()
    t_ord.join(5)
    t_for.join(5)

    assert res_err == {}
    assert provider.calls == 1  # one shared provider load
    assert "999.00" in res["ord"] and "999.00" in res["for"]


@pytest.mark.unit
def test_r5_two_overlapping_refreshes_share_one_load(clock, monkeypatch):
    """Two overlapping forced refreshes coalesce onto one provider load."""
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    provider = BlockingProvider(fresh)
    monkeypatch.setattr(market.yf, "Ticker", lambda s: provider)
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    res, err = {}, {}

    def call(tag):
        try:
            res[tag] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err[tag] = exc

    t1 = threading.Thread(target=call, args=("a",))
    t1.start()
    assert provider.entered.wait(5)
    t2 = threading.Thread(target=call, args=("b",))
    t2.start()
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) == 1)
    provider.release.set()
    t1.join(5)
    t2.join(5)

    assert err == {}
    assert provider.calls == 1  # one shared forced load
    assert "999.00" in res["a"] and "999.00" in res["b"]


def _wait_until_lambda(pred):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


# --------------------------------------------------------------------------- #
# Regression: ±inf closing prices in the date-range path
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_r6_inf_only_range_raises_and_retry_recovers(clock, monkeypatch):
    """An inf-only (±inf) response raises NoMarketDataError (nothing cached); a
    later identical request retries and returns fresh data."""
    from tradingagents.dataflows.errors import NoMarketDataError

    inf_frame = _frame([("2026-01-02", float("inf")), ("2026-01-05", float("-inf")),
                        ("2026-01-06", float("inf"))])
    fresh = _frame([("2026-01-02", 205.0), ("2026-01-05", 206.0), ("2026-01-06", 207.0)])
    provider = _use(monkeypatch, FakeProvider([inf_frame, fresh]))
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")

    with pytest.raises(NoMarketDataError):
        market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert market._range_cache.get(key, clock["t"]) is None  # nothing cached

    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert provider.count == 2
    assert "205.00" in out and "inf" not in out.lower()


@pytest.mark.unit
def test_r7_mixed_inf_range_no_inf_cold_or_warm(clock, monkeypatch):
    """Mixed data keeps valid rows usable and never emits ±inf, cold or warm."""

    mixed = _frame([("2026-01-02", 100.0), ("2026-01-05", float("inf")),
                    ("2026-01-06", 102.0)])
    provider = _use(monkeypatch, FakeProvider([mixed]))

    cold = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    warm = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    assert provider.count == 1  # warm served from cache
    for out in (cold, warm):
        assert "inf" not in out.lower()  # no inf / -inf in CSV
        assert "100.00" in out and "102.00" in out  # valid finite rows usable


@pytest.mark.unit
def test_r8_normal_range_output_byte_identical(clock, monkeypatch):
    """Normal finite data keeps byte-for-byte CSV output (regression guard)."""
    provider = _use(monkeypatch, FakeProvider([_frame(
        [("2026-01-02", 105.0), ("2026-01-05", 106.0), ("2026-01-06", 107.5)])]))
    cold = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    warm = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert cold == EXPECTED_CACHE_OFF
    assert warm == EXPECTED_CACHE_OFF
    assert provider.count == 1


# --------------------------------------------------------------------------- #
# Regression: combined refresh — two forced callers over an ordinary recheck
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_r9_combined_refresh_two_forced_over_ordinary_recheck_one_load(clock, monkeypatch):
    """Combined case: an ordinary request resolves from the cache recheck while
    TWO refresh=True callers have joined its flight; one forced caller's re-entry
    is held (bounded event) until the other has finished its provider reload.

    Asserts the ordinary caller got the cached value, both forced callers got the
    SAME fresh value, all finished, and the provider was called exactly once. A
    separate forced request after completion still triggers a new load.

    All synchronization is bounded events; every wait happens outside the cache
    and flight locks (the provider and the re-entry gate wait before returning
    into run(), never while a lock is held).
    """
    res, err, calls = _run_combined_refresh(monkeypatch, clock, reload_result="fresh")

    assert err == {}
    assert "2026-01-02,100.00,110.00,90.00,100.00" in res["ord"]  # ordinary: cached
    assert "999.00" in res["A"] and "999.00" in res["B"]           # both: same fresh
    assert res["A"] == res["B"]
    assert len(calls) == 1                                          # one provider load

    # A separate forced request after completion starts a NEW load.
    before = len(calls)
    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
    assert len(calls) == before + 1
    assert "999.00" in out


@pytest.mark.unit
def test_r10_combined_refresh_shares_follow_up_error(clock, monkeypatch):
    """When the shared follow-up refresh fails, BOTH forced callers receive the
    same provider error (the follow-up's error is reachable through the original
    flight), and there was exactly one provider call for the follow-up."""
    from tradingagents.dataflows.errors import VendorUnavailableError

    res, err, calls = _run_combined_refresh(monkeypatch, clock, reload_result="error")

    # The ordinary caller still got its cached value; both forced callers failed
    # with the same error type from the single shared follow-up load.
    assert "ord" not in err and "2026-01-02,100.00,110.00,90.00,100.00" in res["ord"]
    assert isinstance(err.get("A"), VendorUnavailableError)
    assert isinstance(err.get("B"), VendorUnavailableError)
    assert type(err["A"]) is type(err["B"])
    assert len(calls) == 1  # one shared follow-up provider call


def _run_combined_refresh(monkeypatch, clock, *, reload_result):
    """Drive the ordinary-recheck + two-forced-refresh interaction deterministically.

    Returns (res, err, calls). ``reload_result`` is "fresh" (999 frame) or "error"
    (the follow-up provider call raises VendorUnavailableError). B's re-entry is
    held until A's reload completes, using bounded events only.
    """
    from tradingagents.dataflows.errors import VendorUnavailableError

    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    old = _frame([("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)])
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    calls = []
    reload_entered = threading.Event()   # a real (follow-up) provider load began
    reload_release = threading.Event()   # let it return (fresh or error)

    class Prov:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            calls.append(1)
            reload_entered.set()
            assert reload_release.wait(5), "provider reload never released"
            if reload_result == "error":
                raise VendorUnavailableError("boom")
            return fresh.copy()

    # Ordinary request's get: pre-flight miss, then in-flight recheck hit (cached
    # OLD frame, no provider call). Gated so both forced callers join first.
    real_get = market._range_cache.get
    tls = threading.local()
    recheck_gate = threading.Event()
    ordinary_at_recheck = threading.Event()  # set once the ordinary hits its recheck

    def controlled_get(k, now):
        if k != key or threading.current_thread().name.startswith("forced"):
            return real_get(k, now)
        phase = getattr(tls, "phase", 0)
        tls.phase = phase + 1
        if phase == 0:
            return None  # ordinary pre-flight miss
        if phase == 1:
            ordinary_at_recheck.set()
            assert recheck_gate.wait(5), "recheck gate never released"
            return old.copy()  # recheck hit -> loaded=False
        return real_get(k, now)

    monkeypatch.setattr(market._range_cache, "get", controlled_get)
    monkeypatch.setattr(market.yf, "Ticker", Prov)

    # Hold forced caller B's RE-ENTRY until A's reload has completed and A has
    # returned (its flight settled), so B must resume late. The re-entry is the
    # nested run() call (call depth >= 2 on the same thread), so track per-thread
    # depth. Bounded event; the wait is outside the cache/flight lock.
    real_run = market._range_inflight.run
    a_done = threading.Event()

    def wrapping_run(*args, **kwargs):
        depth = getattr(tls, "run_depth", 0)
        tls.run_depth = depth + 1
        try:
            if threading.current_thread().name == "forcedB" and depth >= 1:
                # B's re-entry (nested): wait until A finished and popped its flight.
                assert a_done.wait(5), "forced A never finished"
            return real_run(*args, **kwargs)
        finally:
            tls.run_depth = depth

    monkeypatch.setattr(market._range_inflight, "run", wrapping_run)

    res, err = {}, {}

    def ordinary():
        tls.phase = 0
        try:
            res["ord"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
        except BaseException as exc:  # noqa: BLE001
            err["ord"] = exc

    def forced_a():
        try:
            res["A"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err["A"] = exc
        a_done.set()

    def forced_b():
        try:
            res["B"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err["B"] = exc

    t_ord = threading.Thread(target=ordinary, name="ord")
    t_a = threading.Thread(target=forced_a, name="forcedA")
    t_b = threading.Thread(target=forced_b, name="forcedB")

    t_ord.start()
    # Ordinary reaches its gated recheck (pre-flight miss already recorded).
    assert ordinary_at_recheck.wait(5)
    t_a.start()
    t_b.start()
    # Both forced callers joined the ordinary (recheck-only) flight.
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) == 2)
    recheck_gate.set()          # ordinary resolves from the cache recheck (loaded=False)
    assert reload_entered.wait(5)  # a follow-up provider reload has started

    t_ord.join(5)               # ordinary returns its cached value
    reload_release.set()        # let the follow-up reload finish (fresh or error)
    t_a.join(6)                 # A completes; a_done set -> B may resume
    t_b.join(6)

    monkeypatch.setattr(market._range_cache, "get", real_get)
    monkeypatch.setattr(market._range_inflight, "run", real_run)

    assert not (t_ord.is_alive() or t_a.is_alive() or t_b.is_alive())
    return res, err, calls


@pytest.mark.unit
def test_r11_follow_up_reuses_active_same_key_load(clock, monkeypatch):
    """Occupied-key ordering: an ordinary flight resolves from cache; the original
    forced waiter is held before follow-up selection while a NEW same-key forced
    request starts and blocks in the provider. When the original waiter resumes
    (provider call still active) it must REUSE that active flight, not start a
    second load.

    Asserts both forced callers get the same fresh data, exactly one provider
    call, the key is freed afterward, and a separate later refresh loads anew.
    Bounded events only; all waits are outside cache/flight locks.
    """
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    old = _frame([("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)])
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    calls = []
    provider_entered = threading.Event()
    provider_release = threading.Event()

    class Prov:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            calls.append(1)
            provider_entered.set()
            assert provider_release.wait(5), "provider never released"
            return fresh.copy()

    # Ordinary request's get: pre-flight miss, then gated in-flight recheck hit.
    real_get = market._range_cache.get
    tls = threading.local()
    recheck_gate = threading.Event()
    ordinary_at_recheck = threading.Event()

    def controlled_get(k, now):
        if k != key or threading.current_thread().name.startswith("forced"):
            return real_get(k, now)
        phase = getattr(tls, "phase", 0)
        tls.phase = phase + 1
        if phase == 0:
            return None
        if phase == 1:
            ordinary_at_recheck.set()
            assert recheck_gate.wait(5)
            return old.copy()  # recheck hit -> loaded=False
        return real_get(k, now)

    monkeypatch.setattr(market._range_cache, "get", controlled_get)
    monkeypatch.setattr(market.yf, "Ticker", Prov)

    # Hold forced A at follow-up selection (start of _follow_until_fresh, before
    # it picks a flight) until C's provider call is active. This proves the
    # occupied-key reuse path: A resumes while C's provider load is in progress.
    real_follow = market._range_inflight._follow_until_fresh
    c_active = threading.Event()

    def gated_follow(k, joined, loader, postprocess, timeout, **kwargs):
        if threading.current_thread().name == "forcedA":
            assert c_active.wait(5), "new forced request never reached the provider"
        return real_follow(k, joined, loader, postprocess, timeout, **kwargs)

    monkeypatch.setattr(market._range_inflight, "_follow_until_fresh", gated_follow)

    res, err = {}, {}

    def ordinary():
        tls.phase = 0
        try:
            res["ord"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
        except BaseException as exc:  # noqa: BLE001
            err["ord"] = exc

    def forced_a():
        try:
            res["A"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err["A"] = exc

    def forced_c():
        try:
            res["C"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err["C"] = exc

    t_ord = threading.Thread(target=ordinary, name="ord")
    t_ord.start()
    assert ordinary_at_recheck.wait(5)
    # A joins the ordinary (recheck-only) flight first.
    t_a = threading.Thread(target=forced_a, name="forcedA")
    t_a.start()
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) == 1)
    recheck_gate.set()  # ordinary resolves from the cache recheck
    # Let O1 fully complete (and pop the key) before C starts, so C leads a fresh
    # provider load rather than joining O1's already-resolving flight.
    t_ord.join(5)
    assert not t_ord.is_alive(), "ordinary request did not finish"

    # C: a separate same-key forced request starts and blocks in the provider.
    t_c = threading.Thread(target=forced_c, name="forcedC")
    t_c.start()
    assert provider_entered.wait(5), "new forced request never entered provider"
    c_active.set()  # let A resume at follow-up selection, provider still active
    # A must have joined C's active flight before we release the provider, else A
    # could arrive after C pops the key and start a second load (test race).
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) >= 1), "A never joined C"
    provider_release.set()
    t_ord.join(6)
    t_a.join(6)
    t_c.join(6)

    monkeypatch.setattr(market._range_cache, "get", real_get)
    monkeypatch.setattr(market._range_inflight, "_follow_until_fresh", real_follow)

    assert err == {}
    assert not (t_ord.is_alive() or t_a.is_alive() or t_c.is_alive())
    assert "2026-01-02,100.00,110.00,90.00,100.00" in res["ord"]  # ordinary: cached
    assert "999.00" in res["A"] and "999.00" in res["C"]           # both: same fresh
    assert res["A"] == res["C"]
    assert len(calls) == 1                                          # ONE provider load

    # Cleanup left the key available for a separate later refresh -> a new load.
    assert key not in market._range_inflight.inflight_keys()
    before = len(calls)
    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
    assert len(calls) == before + 1
    assert "999.00" in out


@pytest.mark.unit
@pytest.mark.parametrize("provider_outcome", ["fresh", "error"])
def test_r12_forced_follower_over_ordinary_recheck_gets_provider_result(clock, monkeypatch, provider_outcome):
    """A forced caller that first joins a completed cache-only flight O1, and then
    finds ANOTHER ordinary cache-recheck flight O2 owning the key, must continue
    past O2 to provider data (or the shared provider error) -- never return O2's
    cached frame.

    Deterministic ordering, all bounded events:
      1. O1 (ordinary) runs and resolves from a cache recheck (loaded=False).
      2. Forced A joins O1.
      3. Before A traverses, O2 (another ordinary) owns the key (in-flight leader)
         and A is confirmed joined to the key's flight before O2 is released.
      4. O2 is released to return loaded=False (cached 100) while A traverses.

    Pre-fix: A reuses O2 and returns cached 100 with 0 provider calls.
    Fixed:   A continues to a real provider load -> provider_outcome.
    """
    from tradingagents.dataflows.errors import VendorUnavailableError

    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    old = _frame([("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)])
    fresh = _frame([("2026-01-02", 999.0), ("2026-01-05", 1000.0), ("2026-01-06", 1001.0)])
    calls = []

    class Prov:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            calls.append(1)
            if provider_outcome == "error":
                raise VendorUnavailableError("boom")
            return fresh.copy()

    # O1's ordinary get: pre-flight miss, then gated recheck hit (cached 100).
    real_get = market._range_cache.get
    tls = threading.local()
    o1_gate = threading.Event()
    o1_at_recheck = threading.Event()

    def controlled_get(k, now):
        if k != key or threading.current_thread().name != "ord1":
            return real_get(k, now)
        phase = getattr(tls, "phase", 0)
        tls.phase = phase + 1
        if phase == 0:
            return None
        if phase == 1:
            o1_at_recheck.set()
            assert o1_gate.wait(5)
            return old.copy()  # recorded (never stored) cache-recheck frame (loaded=False)
        return real_get(k, now)

    monkeypatch.setattr(market._range_cache, "get", controlled_get)
    monkeypatch.setattr(market.yf, "Ticker", Prov)

    # Gate forced A at the start of its follow traversal until O2 owns the key, so
    # A cannot lead an early load before O2 becomes the key owner. After the gate
    # opens, A proceeds to join O2; we confirm that before releasing O2.
    a_go = threading.Event()
    orig_seam = {}
    for attr in ("_follow_until_fresh", "_join_follow_up"):
        if hasattr(market._range_inflight, attr):
            orig_seam[attr] = getattr(market._range_inflight, attr)

    def make_gate(real):
        def gated(*a, **k):
            if threading.current_thread().name == "forcedA":
                assert a_go.wait(5), "forced A gate never opened"
            return real(*a, **k)
        return gated

    for attr, real in orig_seam.items():
        monkeypatch.setattr(market._range_inflight, attr, make_gate(real))

    res, err = {}, {}

    def ordinary_o1():
        tls.phase = 0
        try:
            res["o1"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
        except BaseException as exc:  # noqa: BLE001
            err["o1"] = exc

    def forced_a():
        try:
            res["A"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        except BaseException as exc:  # noqa: BLE001
            err["A"] = exc

    # 1-2: start O1, then A joins O1's (recheck-only) flight while O1 is gated.
    t1 = threading.Thread(target=ordinary_o1, name="ord1")
    t1.start()
    assert o1_at_recheck.wait(5), "O1 never reached its recheck"
    t_a = threading.Thread(target=forced_a, name="forcedA")
    t_a.start()
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) >= 1), "A never joined O1"
    o1_gate.set()  # O1 resolves from the cache recheck (loaded=False); A abandons to traverse
    t1.join(5)

    # 3: O2 (another ordinary) starts and owns the key; A traverses and joins O2
    # as a waiter. O2 is held at its recheck so we can confirm A joined it BEFORE
    # O2 returns loaded=False.
    o2_gate = threading.Event()
    o2_phase = threading.local()

    def controlled_get_o2(k, now):
        if k != key or threading.current_thread().name != "ord2":
            return real_get(k, now)
        phase = getattr(o2_phase, "n", 0)
        o2_phase.n = phase + 1
        if phase == 0:
            return None
        if phase == 1:
            assert o2_gate.wait(5)
            return old.copy()
        return real_get(k, now)

    monkeypatch.setattr(market._range_cache, "get", controlled_get_o2)

    def ordinary_o2():
        try:
            res["o2"] = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
        except BaseException as exc:  # noqa: BLE001
            err["o2"] = exc

    t2 = threading.Thread(target=ordinary_o2, name="ord2")
    t2.start()
    # O2 owns the key (it is the in-flight leader). Open A's gate so it proceeds
    # to join O2, then confirm A has joined before releasing O2 to loaded=False.
    assert _wait_until_lambda(lambda: key in market._range_inflight.inflight_keys())
    a_go.set()
    assert _wait_until_lambda(lambda: market._range_inflight.waiter_count(key) >= 1), "A never joined O2"
    o2_gate.set()  # now release O2 to return loaded=False (cached 100) while A follows

    t_a.join(6)
    t2.join(6)

    for attr, real in orig_seam.items():
        monkeypatch.setattr(market._range_inflight, attr, real)
    monkeypatch.setattr(market._range_cache, "get", real_get)

    assert not (t_a.is_alive() or t2.is_alive())
    if provider_outcome == "fresh":
        assert err == {}
        # The forced caller gets provider data (999), never cached 100.
        assert "999.00" in res["A"]
        assert "2026-01-02,100.00,110.00,90.00,100.00" not in res["A"]
        assert len(calls) == 1  # exactly one provider load for A's traversal
        # A separate later refresh starts a new load.
        before = len(calls)
        out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)
        assert len(calls) == before + 1
        assert "999.00" in out
    else:
        # Provider-error variant: the forced caller receives that error, not cached.
        assert isinstance(err.get("A"), VendorUnavailableError)
        assert "A" not in res
        assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Regression: _Inflight coalesced_waits accounting (paired wait per join)
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_w1_forced_join_active_load_counts_one_wait():
    """A forced follower joining an ACTIVE provider load increments coalesced_waits
    exactly once, and the joined flight's waiter count returns to zero."""
    import threading as _t

    from tradingagents.dataflows.vendors.yahoo.cache import CacheStats, _Flight, _Inflight

    def frame(c):
        return pd.DataFrame({"Close": [c]}, index=pd.DatetimeIndex(["2026-01-02"], name="Date"))

    stats = CacheStats()
    inflight = _Inflight(stats=stats)
    key = ("K",)
    active = _Flight()
    inflight._flights[key] = active
    release = _t.Event()

    def leader():
        assert release.wait(5)
        with inflight._lock:
            active.result = frame(999.0)
            active.loaded = True
            inflight._flights.pop(key, None)
            active.done.set()

    t_leader = _t.Thread(target=leader)
    t_leader.start()
    out = {}

    def forced():
        out["v"] = inflight.run(key, lambda: (frame(999.0), True),
                                require_fresh=True, timeout=5)

    t_forced = _t.Thread(target=forced)
    t_forced.start()
    # The forced caller has joined the active flight (one waiter) before release.
    assert _wait_until_lambda(lambda: inflight.waiter_count(key) == 1)
    release.set()
    t_leader.join(6)
    t_forced.join(6)

    assert not t_forced.is_alive()
    assert float(out["v"]["Close"].iloc[0]) == 999.0
    assert stats.snapshot()["coalesced_waits"] == 1  # exactly one wait for one join
    assert active.waiters == 0                        # paired: decremented back to 0


@pytest.mark.unit
def test_w2_late_caller_reusing_completed_follow_up_adds_no_wait():
    """A late caller reusing an already-completed follow-up receives its result with
    no new coalesced wait counted, and the completed flight's waiter count stays 0."""
    from tradingagents.dataflows.vendors.yahoo.cache import CacheStats, _Flight, _Inflight

    def frame(c):
        return pd.DataFrame({"Close": [c]}, index=pd.DatetimeIndex(["2026-01-02"], name="Date"))

    stats = CacheStats()
    inflight = _Inflight(stats=stats)
    key = ("K",)
    # The flight the caller first joins: a completed cache-only flight.
    original = _Flight()
    original.result = frame(100.0)
    original.loaded = False
    original.done.set()
    inflight._flights[key] = original
    # Its follow-up already completed with provider data.
    follow = _Flight()
    follow.result = frame(999.0)
    follow.loaded = True
    follow.done.set()
    original.follow_up = follow

    out = {}
    t = threading.Thread(target=lambda: out.__setitem__(
        "v", inflight.run(key, lambda: (frame(999.0), True), require_fresh=True, timeout=5)))
    t.start()
    t.join(6)

    assert not t.is_alive()
    assert float(out["v"]["Close"].iloc[0]) == 999.0      # reused follow-up result
    assert stats.snapshot()["coalesced_waits"] == 1        # only the initial join
    assert follow.waiters == 0                             # completed flight stays at 0
