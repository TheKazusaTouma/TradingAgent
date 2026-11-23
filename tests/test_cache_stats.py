"""Stage 4: process-local Yahoo cache statistics.

Counters describe outcomes, not provider retries:
  * memory_hits   -- served from the in-memory range cache
  * disk_hits     -- served from the on-disk five-year CSV
  * coalesced_waits -- a follower joined an in-flight load (result or error)
  * load_attempts -- a shared load that actually invoked the data source
  * load_failures -- a shared load that raised (once per load, not per caller)

Mock provider call counts are asserted separately from the cache statistics so
the two are never conflated.
"""
from __future__ import annotations

import os
import threading

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import cache, common, market, ohlcv

TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _reset_stats():
    cache.STATS.reset()
    if hasattr(market, "_range_cache"):
        market._range_cache.clear()
    yield
    cache.STATS.reset()


def _wait(pred, deadline=None):
    import time

    deadline = deadline or (time.monotonic() + TIMEOUT)
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _range_frame(closes=(105.0, 106.0, 107.5)):
    idx = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0], "High": [110.0, 111.0, 112.0],
            "Low": [90.0, 91.0, 92.0], "Close": list(closes),
            "Volume": [1000, 2000, 3000], "Stock Splits": 0.0, "Dividends": 0.0,
        },
        index=idx,
    )


def _ohlcv_frame():
    idx = pd.DatetimeIndex(["2026-07-16", "2026-07-17"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [105.0, 106.0], "Volume": [1000, 2000],
        },
        index=idx,
    )


def _valid_csv_frame():
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-16", "2026-07-17"]),
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [105.0, 106.0], "Volume": [1000, 2000],
        }
    )


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def history(self, **kw):
        self.calls += 1
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(resp, BaseException):
            raise resp
        return resp.copy()


class BlockingProvider(Provider):
    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()

    def history(self, **kw):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(TIMEOUT), "leader never released"
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(resp, BaseException):
            raise resp
        return resp.copy()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(market, "_now", lambda: now["t"])
    yield now


@pytest.fixture
def ohlcv_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00")))
    monkeypatch.setattr(common, "vendor_reachable", lambda *a, **k: True)
    return tmp_path, tmp_path / "AAPL-YFin-data.csv"


def _run_pair(fn):
    res, err = [None, None], [None, None]

    def wrap(i):
        try:
            res[i] = fn()
        except BaseException as e:  # noqa: BLE001
            err[i] = e

    a = threading.Thread(target=wrap, args=(0,))
    b = threading.Thread(target=wrap, args=(1,))
    a.start()
    b.start()
    return a, b, res, err


# --------------------------------------------------------------------------- #
# 1. Ten identical sequential range queries from a cold cache
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s1_ten_sequential_range_queries(clock, monkeypatch):
    provider = Provider([_range_frame()])
    monkeypatch.setattr(market.yf, "Ticker", lambda s: provider)

    outs = [market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06") for _ in range(10)]

    assert len(set(outs)) == 1                 # identical outputs
    assert provider.calls == 1                 # one provider call
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["memory_hits"] == 9
    assert s["coalesced_waits"] == 0
    assert s["load_failures"] == 0


# --------------------------------------------------------------------------- #
# 2. Two overlapping same-key cold requests, each path
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s2_overlap_range_path(clock, monkeypatch):
    provider = BlockingProvider([_range_frame()])
    monkeypatch.setattr(market.yf, "Ticker", lambda s: provider)
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")


    a, b, res, err = _run_pair(call)
    assert provider.entered.wait(TIMEOUT)
    # Prove the follower joined the same in-flight load while the leader blocked.
    assert _wait(lambda: market._range_inflight.waiter_count(key) == 1)
    provider.release.set()
    a.join(TIMEOUT)
    b.join(TIMEOUT)
    assert not a.is_alive() and not b.is_alive()

    assert err == [None, None]
    assert res[0] == res[1] and "105.00" in res[0]
    assert provider.calls == 1
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["coalesced_waits"] == 1
    assert s["memory_hits"] == 0


@pytest.mark.unit
def test_s2_overlap_ohlcv_path(ohlcv_env, monkeypatch):
    tmp_path, _ = ohlcv_env
    provider = BlockingProvider([_ohlcv_frame()])
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: provider)
    key = (os.path.abspath(str(tmp_path)), "AAPL")
    def call():
        return ohlcv.load_ohlcv("AAPL", "2026-07-17")


    a, b, res, err = _run_pair(call)
    assert provider.entered.wait(TIMEOUT)
    assert _wait(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1)
    provider.release.set()
    a.join(TIMEOUT)
    b.join(TIMEOUT)
    assert not a.is_alive() and not b.is_alive()

    assert err == [None, None]
    assert res[0].reset_index(drop=True).equals(res[1].reset_index(drop=True))
    assert provider.calls == 1
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["coalesced_waits"] == 1
    assert s["disk_hits"] == 0


# --------------------------------------------------------------------------- #
# 3. A valid disk-cache hit: 1 disk hit, 0 loads
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s3_valid_disk_hit(ohlcv_env, monkeypatch):
    tmp_path, cache_file = ohlcv_env
    _valid_csv_frame().to_csv(cache_file, index=False)
    t = pd.Timestamp("2026-07-18 12:00").to_pydatetime().timestamp()
    os.utime(cache_file, (t, t))

    def _must_not(*a, **k):
        raise AssertionError("valid disk cache must not download")
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: type("P", (), {"history": _must_not})())

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert float(out["Close"].iloc[-1]) == 106.0
    s = cache.STATS.snapshot()
    assert s["disk_hits"] == 1
    assert s["load_attempts"] == 0
    assert s["coalesced_waits"] == 0


# --------------------------------------------------------------------------- #
# 4. A corrupt OHLCV CSV: 1 load and a usable replacement file
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s4_corrupt_csv_recovers(ohlcv_env, monkeypatch):
    tmp_path, cache_file = ohlcv_env
    cache_file.write_bytes(b"")  # zero-byte corrupt cache
    provider = Provider([_valid_csv_frame().set_index("Date")])
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: provider)

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert float(out["Close"].iloc[-1]) == 106.0
    assert provider.calls == 1
    assert cache_file.stat().st_size > 0
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["disk_hits"] == 0


# --------------------------------------------------------------------------- #
# 5. Invalid refresh over a valid CSV: 1 load, 1 failure, file preserved
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s5_invalid_refresh_preserves_file(ohlcv_env, monkeypatch):
    tmp_path, cache_file = ohlcv_env
    _valid_csv_frame().to_csv(cache_file, index=False)
    now = pd.Timestamp("2026-07-18 12:00")
    stale = (now - pd.Timedelta(seconds=ohlcv.OHLCV_CACHE_TTL_SECONDS + 60)).to_pydatetime().timestamp()
    os.utime(cache_file, (stale, stale))
    before = cache_file.read_bytes()

    bad = pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-18"]), "Open": [102.0], "High": [112.0],
        "Low": [92.0], "Close": [float("nan")], "Volume": [3000],
    }).set_index("Date")
    provider = Provider([bad])
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: provider)

    from tradingagents.dataflows.errors import NoMarketDataError
    with pytest.raises(NoMarketDataError):
        ohlcv.load_ohlcv("AAPL", "2026-07-18")

    assert provider.calls == 1
    assert cache_file.read_bytes() == before        # byte-for-byte preserved
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["load_failures"] == 1


# --------------------------------------------------------------------------- #
# 6. A shared load failure: 1 load, 1 wait, 1 failure, both callers error
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_s6_shared_load_failure_range(clock, monkeypatch):
    provider = BlockingProvider([VendorUnavailableError("boom")])
    monkeypatch.setattr(market.yf, "Ticker", lambda s: provider)
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")


    a, b, res, err = _run_pair(call)
    assert provider.entered.wait(TIMEOUT)
    assert _wait(lambda: market._range_inflight.waiter_count(key) == 1)
    provider.release.set()
    a.join(TIMEOUT)
    b.join(TIMEOUT)
    assert not a.is_alive() and not b.is_alive()

    assert all(isinstance(e, VendorUnavailableError) for e in err)   # both callers
    assert provider.calls == 1
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["coalesced_waits"] == 1
    assert s["load_failures"] == 1                                  # once per load
    assert market._range_inflight.inflight_keys() == ()


@pytest.mark.unit
def test_s6_shared_load_failure_ohlcv(ohlcv_env, monkeypatch):
    tmp_path, cache_file = ohlcv_env
    provider = BlockingProvider([VendorUnavailableError("boom")])
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: provider)
    key = (os.path.abspath(str(tmp_path)), "AAPL")
    def call():
        return ohlcv.load_ohlcv("AAPL", "2026-07-17")


    a, b, res, err = _run_pair(call)
    assert provider.entered.wait(TIMEOUT)
    assert _wait(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1)
    provider.release.set()
    a.join(TIMEOUT)
    b.join(TIMEOUT)
    assert not a.is_alive() and not b.is_alive()

    assert all(isinstance(e, VendorUnavailableError) for e in err)
    assert provider.calls == 1
    assert not cache_file.exists()
    s = cache.STATS.snapshot()
    assert s["load_attempts"] == 1
    assert s["coalesced_waits"] == 1
    assert s["load_failures"] == 1
