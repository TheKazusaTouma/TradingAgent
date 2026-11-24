"""Concurrent same-key Yahoo loads coalesce into one provider call.

Both the date-range (``get_YFin_data_online``) and five-year OHLCV
(``load_ohlcv``) paths share one in-flight load across overlapping callers,
while each caller keeps its own date cutoff and gap handling. Determinism comes
from a leader that holds the provider open until the test *observes* a second
caller waiting on the same flight (``waiter_count``), plus bounded waits and
joins — never a startup race.
"""
from __future__ import annotations

import os
import threading
import time

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import market, ohlcv, snapshot

JOIN_TIMEOUT = 5.0


def _deadline():
    return time.monotonic() + JOIN_TIMEOUT


def _wait_until(predicate, deadline):
    """Poll ``predicate`` with a bounded wait; fail loudly if it never holds."""
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _run_pair(leader_fn, follower_fn):
    """Start two workers, return their (results, errors) after both finish."""
    results, errors = [None, None], [None, None]

    def wrap(idx, fn):
        try:
            results[idx] = fn()
        except BaseException as exc:  # noqa: BLE001 — captured for assertions
            errors[idx] = exc

    t_leader = threading.Thread(target=wrap, args=(0, leader_fn))
    t_follower = threading.Thread(target=wrap, args=(1, follower_fn))
    t_leader.start()
    t_follower.start()
    return t_leader, t_follower, results, errors


def _join_all(*threads):
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT)
    return all(not t.is_alive() for t in threads)


def _frame(rows, *, open0=100.0):
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


class BlockingTicker:
    """A yfinance Ticker whose first history() call blocks until released.

    ``entered`` is set when the leader is inside the provider; ``release`` lets
    it return. A second call also blocks (on a separate event) so a buggy second
    download can never silently complete before the test inspects it.
    """

    def __init__(self, frame, *, entered, release, second_entered=None, second_release=None):
        self.frame = frame
        self.entered = entered
        self.release = release
        self.second_entered = second_entered
        self.second_release = second_release
        self.calls = 0

    def history(self, **kwargs):
        self.calls += 1
        self.entered.set()
        if self.calls == 1:
            assert self.release.wait(JOIN_TIMEOUT), "leader release never set"
        else:
            if self.second_entered is not None:
                self.second_entered.set()
            if self.second_release is not None:
                assert self.second_release.wait(JOIN_TIMEOUT), "second release never set"
        return self.frame.copy()


@pytest.fixture
def fixed_clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(market, "_now", lambda: now["t"])
    market._range_cache.clear()
    yield now
    market._range_cache.clear()


# --------------------------------------------------------------------------- #
# Path 1 — date-range (get_YFin_data_online)
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_c1_same_key_cold_requests_share_one_load(fixed_clock, monkeypatch):
    frame = _frame([("2026-01-02", 105.0), ("2026-01-05", 106.0)])
    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(frame, entered=entered, release=release)
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: ticker)

    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")


    threads = _run_pair(call, call)
    t_leader, t_follower, results, errors = threads

    # Leader is inside the provider; wait until the follower has joined the same
    # in-flight request, then release. This proves overlap at the load, not just
    # a coordinating thread start.
    assert _wait_until(lambda: market._range_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    assert errors == [None, None]
    assert results[0] == results[1]
    assert ticker.calls == 1


@pytest.mark.unit
def test_c2_expired_entry_coalesces_into_one_refresh(fixed_clock, monkeypatch):
    frame = _frame([("2026-01-02", 999.0)])
    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(frame, entered=entered, release=release)
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: ticker)

    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    # Seed an expired entry so both callers must refresh.
    market._range_cache.store(key, _frame([("2026-01-02", 111.0)]), fixed_clock["t"] - 10_000)

    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    t_leader, t_follower, results, errors = _run_pair(call, call)

    assert _wait_until(lambda: market._range_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    assert errors == [None, None]
    assert "999.00" in results[0] and "999.00" in results[1]
    assert ticker.calls == 1


@pytest.mark.unit
def test_c3_different_keys_load_independently(fixed_clock, monkeypatch):
    frames = {
        "AAPL": _frame([("2026-01-02", 105.0)]),
        "MSFT": _frame([("2026-01-02", 405.0)], open0=400.0),
    }
    both_entered = threading.Barrier(2, timeout=JOIN_TIMEOUT)
    calls = []

    class EachKeyTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            calls.append(self.symbol)
            both_entered.wait()  # if loads were serialized, this times out
            return frames[self.symbol].copy()

    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: EachKeyTicker(symbol))
    t_leader, t_follower, results, errors = _run_pair(
        lambda: market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06"),
        lambda: market.get_YFin_data_online("MSFT", "2026-01-02", "2026-01-06"),
    )
    assert _join_all(t_leader, t_follower)
    assert errors == [None, None]
    assert sorted(calls) == ["AAPL", "MSFT"]
    assert "105.00" in results[0] and "405.00" in results[1]


@pytest.mark.unit
def test_c4_concurrent_refresh_shares_one_load_and_bypasses_old(fixed_clock, monkeypatch):
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    market._range_cache.store(key, _frame([("2026-01-02", 105.0)]), fixed_clock["t"])
    frame = _frame([("2026-01-02", 999.0)])
    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(frame, entered=entered, release=release)
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: ticker)

    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06", refresh=True)

    t_leader, t_follower, results, errors = _run_pair(call, call)

    assert _wait_until(lambda: market._range_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    assert errors == [None, None]
    assert ticker.calls == 1  # one shared refresh
    assert "999.00" in results[0] and "999.00" in results[1]
    assert "105.00" not in results[0]  # old entry bypassed


@pytest.mark.unit
def test_c7b_range_failed_shared_load_releases_and_retries(fixed_clock, monkeypatch):
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    good = _frame([("2026-01-02", 105.0)])
    entered, release = threading.Event(), threading.Event()

    class FailThenSucceed:
        calls = 0

        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            FailThenSucceed.calls += 1
            if FailThenSucceed.calls == 1:
                entered.set()
                assert release.wait(JOIN_TIMEOUT)
                raise VendorUnavailableError("boom")
            return good.copy()

    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: FailThenSucceed(symbol))
    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    t_leader, t_follower, results, errors = _run_pair(call, call)

    assert _wait_until(lambda: market._range_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    # Both overlapping callers fail together, one provider call, nothing in flight.
    assert all(isinstance(e, VendorUnavailableError) for e in errors)
    assert FailThenSucceed.calls == 1
    assert market._range_inflight.inflight_keys() == ()

    # A separate retry succeeds and hits the provider a second time.
    out = market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")
    assert "105.00" in out
    assert FailThenSucceed.calls == 2


# --------------------------------------------------------------------------- #
# Path 2 — five-year OHLCV (load_ohlcv)
# --------------------------------------------------------------------------- #

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00")))
    return tmp_path


@pytest.mark.unit
def test_c5_same_key_cold_load_ohlcv_shares_one_download(cache_dir, monkeypatch):
    frame = _frame([("2026-07-16", 100.0), ("2026-07-17", 101.0)])
    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(frame, entered=entered, release=release)
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda symbol: ticker)

    key = (os.path.abspath(str(cache_dir)), "AAPL")
    def call():
        return ohlcv.load_ohlcv("AAPL", "2026-07-17")

    t_leader, t_follower, results, errors = _run_pair(call, call)

    assert _wait_until(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    assert errors == [None, None]
    assert ticker.calls == 1
    pd.testing.assert_frame_equal(results[0].reset_index(drop=True), results[1].reset_index(drop=True))


@pytest.mark.unit
def test_c6_indicators_and_snapshots_share_raw_download_with_own_cutoff_and_gaps(
    cache_dir, monkeypatch
):
    """One raw five-year download serves both; each caller keeps its own
    date cutoff and gap handling.

    Raw rows (the missing-Open row sits on/before both cutoffs):
      2026-07-10  Open=100  Close=105
      2026-07-13  Open=NaN  Close=106   <- missing Open, <= both cutoffs
      2026-07-14  Open=102  Close=107   <- between cutoffs (D_snap < 07-14 <= D_ind)
      2026-07-15  Open=103  Close=108   <- beyond both cutoffs (look-ahead to drop)
    Indicator caller D_ind = 2026-07-14, snapshot caller D_snap = 2026-07-13.
    """
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]),
            "Open": [100.0, float("nan"), 102.0, 103.0],
            "High": [110.0, 111.0, 112.0, 113.0],
            "Low": [90.0, 91.0, 92.0, 93.0],
            "Close": [105.0, 106.0, 107.0, 108.0],
            "Volume": [1000, 2000, 3000, 4000],
        }
    )

    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(raw.set_index("Date"), entered=entered, release=release)
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda symbol: ticker)

    # Capture the frame load_ohlcv hands to stockstats so we can inspect the
    # gap-filled Open on the indicator path. stockstats.wrap is also called
    # internally with Series; record only the first DataFrame that carries a
    # Date column (the raw frame from load_ohlcv) and delegate everything else.
    import stockstats

    captured_wrap = {}
    real_wrap = stockstats.wrap

    def capture_wrap(value, *a, **k):
        if "data" not in captured_wrap and isinstance(value, pd.DataFrame) and "Date" in value.columns:
            captured_wrap["data"] = value.copy()
        return real_wrap(value, *a, **k)

    monkeypatch.setattr(stockstats, "wrap", capture_wrap)

    D_IND = "2026-07-14"
    D_SNAP = "2026-07-13"
    key = (os.path.abspath(str(cache_dir)), "AAPL")

    t_ind, t_snap, results, errors = _run_pair(
        lambda: market._get_stock_stats_bulk("AAPL", "rsi", D_IND),  # dict
        lambda: snapshot._verified_rows("AAPL", D_SNAP),             # DataFrame
    )

    assert _wait_until(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_ind, t_snap)

    assert errors == [None, None]
    assert ticker.calls == 1  # both callers share one provider load

    indicator_dict, snapshot_frame = results[0], results[1]

    # Indicator path (fill_gaps=True): the frame passed to wrap keeps the
    # missing-Open row and fills its Open from a neighbour.
    filled = captured_wrap["data"]
    gap_row = filled[pd.to_datetime(filled["Date"]) == pd.Timestamp("2026-07-13")]
    assert not gap_row.empty
    assert not pd.isna(gap_row["Open"].iloc[0])
    # Indicator cutoff: its dict keys never exceed D_IND.
    assert max(indicator_dict) <= D_IND
    assert "2026-07-14" in indicator_dict
    assert "2026-07-15" not in indicator_dict

    # Snapshot path (fill_gaps=False): the missing Open is retained, the
    # between-cutoffs row is included (<= D_SNAP? no: 07-14 > 07-13, excluded),
    # and the beyond-cutoff row is dropped.
    assert isinstance(snapshot_frame, pd.DataFrame)
    snap = snapshot_frame.copy()
    snap["Date"] = pd.to_datetime(snap["Date"])
    snap_gap = snap[snap["Date"] == pd.Timestamp("2026-07-13")]
    assert not snap_gap.empty
    assert pd.isna(snap_gap["Open"].iloc[0])          # missing Open retained
    assert "2026-07-14" not in {d.strftime("%Y-%m-%d") for d in snap["Date"]}
    assert "2026-07-15" not in {d.strftime("%Y-%m-%d") for d in snap["Date"]}
    assert snap["Date"].max() <= pd.Timestamp(D_SNAP)


@pytest.mark.unit
def test_c8_historical_served_from_disk_while_current_waits_for_fresh(cache_dir, monkeypatch):
    """Per-caller freshness survives coalescing: an eligible historical request
    returns the on-disk data *before* the blocked current-date download finishes,
    and that current-date caller receives the fresh rows after release."""
    # Disk file written today but older than the TTL.
    old = pd.DataFrame(
        {
            "Date": ["2026-07-16", "2026-07-17"],
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [100.0, 101.0], "Volume": [1000, 2000],
        }
    )
    data_file = cache_dir / "AAPL-YFin-data.csv"
    old.to_csv(data_file, index=False)
    now = pd.Timestamp("2026-07-18 12:00")
    stale_ts = (now - pd.Timedelta(seconds=ohlcv.OHLCV_CACHE_TTL_SECONDS + 60)).to_pydatetime().timestamp()
    os.utime(data_file, (stale_ts, stale_ts))

    # Provider returns a frame that includes today with a fresh close (999) and
    # blocks until released.
    fresh = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-16", "2026-07-17", "2026-07-18"]),
            "Open": [100.0, 101.0, 102.0], "High": [110.0, 111.0, 112.0],
            "Low": [90.0, 91.0, 92.0], "Close": [100.0, 101.0, 999.0],
            "Volume": [1000, 2000, 3000],
        }
    ).set_index("Date")
    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(fresh, entered=entered, release=release)
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda symbol: ticker)

    results, errors = {}, {}

    def current_caller():
        try:
            results["current"] = ohlcv.load_ohlcv("AAPL", "2026-07-18")
        except BaseException as exc:  # noqa: BLE001
            errors["current"] = exc

    def historical_caller():
        try:
            results["hist"] = ohlcv.load_ohlcv("AAPL", "2026-07-17")
        except BaseException as exc:  # noqa: BLE001
            errors["hist"] = exc

    # Start the current-date request first and let it block inside the provider.
    t_current = threading.Thread(target=current_caller)
    t_current.start()
    assert entered.wait(JOIN_TIMEOUT), "current-date download never entered provider"

    # Then a historical request: it must be served from disk without waiting.
    t_hist = threading.Thread(target=historical_caller)
    t_hist.start()
    t_hist.join(timeout=JOIN_TIMEOUT)
    assert not t_hist.is_alive(), "historical request should not block on the download"
    assert "hist" in results and "hist" not in errors
    hist = results["hist"]
    assert (hist["Close"] == 101.0).iloc[-1]  # old disk data, not the fresh 999
    assert hist["Date"].max() <= pd.Timestamp("2026-07-17")

    # Release the provider; the current-date caller now receives fresh data.
    release.set()
    t_current.join(timeout=JOIN_TIMEOUT)
    assert not t_current.is_alive()
    assert "current" in results
    cur = results["current"]
    assert (cur["Close"] == 999.0).any()  # fresh close reached the current caller
    assert cur["Date"].max() == pd.Timestamp("2026-07-18")

    assert ticker.calls == 1  # exactly one provider load


@pytest.mark.unit
def test_c7a_ohlcv_failed_shared_load_releases_and_retries(cache_dir, monkeypatch):
    good = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-16", "2026-07-17"]),
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [100.0, 101.0], "Volume": [1000, 2000],
        }
    ).set_index("Date")
    entered, release = threading.Event(), threading.Event()

    class FailThenSucceed:
        calls = 0

        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            FailThenSucceed.calls += 1
            if FailThenSucceed.calls == 1:
                entered.set()
                assert release.wait(JOIN_TIMEOUT)
                raise VendorUnavailableError("boom")
            return good.copy()

    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda symbol: FailThenSucceed(symbol))
    key = (os.path.abspath(str(cache_dir)), "AAPL")
    def call():
        return ohlcv.load_ohlcv("AAPL", "2026-07-17")

    t_leader, t_follower, results, errors = _run_pair(call, call)

    assert _wait_until(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    assert _join_all(t_leader, t_follower)

    assert all(isinstance(e, VendorUnavailableError) for e in errors)
    assert FailThenSucceed.calls == 1
    assert ohlcv._ohlcv_inflight.inflight_keys() == ()
    assert not (cache_dir / "AAPL-YFin-data.csv").exists()  # nothing poisoned

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert not out.empty
    assert FailThenSucceed.calls == 2


# --------------------------------------------------------------------------- #
# Coalescer bookkeeping: a timed-out follower must not corrupt the flight
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_c9_timed_out_follower_keeps_flight_and_later_caller_joins_it(monkeypatch):
    """A follower that times out leaves the still-running flight registered (so a
    later same-key caller joins it with no second load) and does not leak the
    waiter count."""
    from tradingagents.dataflows.vendors.yahoo.cache import _Inflight

    inflight = _Inflight()
    key = ("AAPL", "2026-01-02", "2026-01-07")
    leader_entered = threading.Event()
    leader_release = threading.Event()
    loader_calls = []

    def leader():
        loader_calls.append("leader")
        leader_entered.set()
        assert leader_release.wait(JOIN_TIMEOUT)
        return pd.DataFrame({"Close": [105.0]}), True

    t_leader = threading.Thread(target=lambda: inflight.run(key, leader))
    t_leader.start()
    assert leader_entered.wait(JOIN_TIMEOUT)

    # Follower times out while the leader is still loading.
    with pytest.raises(TimeoutError):
        inflight.run(key, leader, timeout=0.1)

    # The flight is still registered for the running leader, and the leaked
    # waiter is cleaned up.
    assert inflight.inflight_keys() == (key,)
    assert inflight.waiter_count(key) == 0

    # A later same-key caller joins the existing flight instead of loading again.
    late_result = {}

    def second_loader():
        loader_calls.append("second")  # must NOT run
        return pd.DataFrame({"Close": [999.0]}), True

    def late_caller():
        late_result["frame"] = inflight.run(key, second_loader, timeout=JOIN_TIMEOUT)

    t_late = threading.Thread(target=late_caller)
    t_late.start()
    assert _wait_until(lambda: inflight.waiter_count(key) == 1, _deadline())

    leader_release.set()
    assert _join_all(t_leader, t_late)

    # Only the leader ever loaded; the late caller received the leader's frame.
    assert loader_calls == ["leader"]
    assert late_result["frame"]["Close"].iloc[0] == 105.0
    assert inflight.inflight_keys() == ()
    assert inflight.waiter_count(key) == 0


# --------------------------------------------------------------------------- #
# Regression: concurrent reads of the same EXPIRED range entry must not raise
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_c10_concurrent_reads_of_expired_entry_share_one_reload(fixed_clock, monkeypatch):
    """Two concurrent public queries against the same expired entry both succeed
    and share a single provider reload.

    Before the ``_RangeCache`` lock, both callers fetched the entry, saw it
    expired, and each ran ``del self._entries[key]`` — the second ``del`` raised
    KeyError. Here the leader's reload is blocked in the provider while the
    second caller joins the in-flight load; the pre-flight ``get`` on the expired
    entry is exercised by both callers before either reloads.
    """
    key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
    fresh = _frame([("2026-01-02", 205.0), ("2026-01-05", 206.0)])
    from tradingagents.dataflows.vendors.yahoo import cache

    # Seed an EXPIRED entry for the exact key the public call computes.
    market._range_cache.store(key, _frame([("2026-01-02", 111.0)]),
                              fixed_clock["t"] - market._range_cache.ttl - 10)
    before = cache.STATS.snapshot()

    entered, release = threading.Event(), threading.Event()
    ticker = BlockingTicker(fresh, entered=entered, release=release)
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: ticker)

    def call():
        return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

    results, errors = [None, None], [None, None]

    def wrap(i):
        try:
            results[i] = call()
        except BaseException as exc:  # noqa: BLE001
            errors[i] = exc

    t1 = threading.Thread(target=wrap, args=(0,))
    t2 = threading.Thread(target=wrap, args=(1,))
    t1.start()
    assert entered.wait(JOIN_TIMEOUT), "leader never entered provider"
    t2.start()
    # The second caller joins the shared reload while the first is still blocked.
    assert _wait_until(lambda: market._range_inflight.waiter_count(key) == 1, _deadline())
    release.set()
    t1.join(JOIN_TIMEOUT)
    t2.join(JOIN_TIMEOUT)

    # No KeyError (or any error) from either caller; both finished; both got the
    # fresh reload value and an identical result.
    assert errors == [None, None]
    assert not t1.is_alive() and not t2.is_alive()
    assert results[0] == results[1]
    # Both callers see the fresh reload (closes 205/206), never the stale 111.
    assert "2026-01-02,100.00,110.00,90.00,205.00" in results[0]
    assert "2026-01-02,100.00,110.00,90.00,111.00" not in results[0]
    assert ticker.calls == 1  # exactly one provider reload
    # Exactly one cache load attempt for the two callers (the second coalesced).
    after = cache.STATS.snapshot()
    assert after["load_attempts"] - before["load_attempts"] == 1


@pytest.mark.unit
def test_c11_range_cache_lock_serialises_expired_delete(fixed_clock):
    """Direct cache-layer check: many threads reading the same expired entry all
    return ``None`` (a miss) without raising, whether or not they overlap.

    This does not put a barrier inside ``_is_fresh`` (the fix holds the lock
    there); it just hammers the same expired key from many threads and requires
    no exception and a consistent miss, which the unlocked old code violated by
    deleting the entry twice.
    """
    key = ("AAPL", "2026-01-02", "2026-01-07")
    market._range_cache.store(key, _frame([("2026-01-02", 1.0)]),
                              fixed_clock["t"] - market._range_cache.ttl - 10)
    errors = []
    misses = []

    def reader():
        try:
            misses.append(market._range_cache.get(key, fixed_clock["t"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=reader) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)

    assert errors == []
    assert all(m is None for m in misses)
