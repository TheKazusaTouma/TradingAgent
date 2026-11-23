"""The Yahoo date-range memory cache: one provider load per request identity.

A repeated range query within the TTL is served from an in-process cache keyed
by the normalized request identity, and the cache holds independent copies so a
caller can never corrupt it or another caller's frame. The five-year OHLCV cache
(tested elsewhere) is unchanged; this file covers only ``get_YFin_data_online``
and the small ``cache._RangeCache`` it uses.
"""
from __future__ import annotations

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
