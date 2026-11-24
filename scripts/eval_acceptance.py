"""Offline acceptance evaluation for the Yahoo market-data cache (Stages 1-3).

Runs the SAME scenarios on whatever revision is importable (via PYTHONPATH) and
prints a JSON report. Capabilities are detected at runtime so baseline, Stage 1,
Stage 2 and Stage 3 all run through one script:

  * has_range_cache   -> market._range_cache exists (Stage 1+)
  * has_coalescing    -> market._range_inflight / ohlcv._ohlcv_inflight exist (Stage 2+)
  * has_recovery      -> ohlcv._read_cached_csv / _has_usable_bar exist (Stage 3)

The script verifies the imported revision by reporting the location of the
tradingagents package it actually loaded, and the caller passes the expected
worktree path so a mismatch is visible.

Requirements: the project dependencies (see pyproject.toml), in particular pandas.
No network access and no personal paths are required.

Usage (from the repository root; pass the label, the expected commit short SHA,
and the path your run imports the package from):

    PYTHONPATH=<checkout> python scripts/eval_acceptance.py <label> <expected_rev> <expected_pkg_fragment>

Compare revisions by running once per isolated checkout (e.g. a git worktree):

    PYTHONPATH=/path/to/checkout python /path/to/checkout/scripts/eval_acceptance.py stage3 9e431ea /path/to/checkout

Reference commits used by the comparisons:
    307d15e  baseline (no in-memory cache; every range query hits the provider)
    003082f  Stage 1 (in-memory range cache)
    97bb88e  Stage 2 (in-flight request coalescing)
    9e431ea  Stage 3 (OHLCV corrupt/empty/invalid recovery)

Scenarios:
  A. ten identical sequential date-range queries
  B. two overlapping cold requests, date-range path (overlap proven)
  C. two overlapping cold requests, five-year OHLCV path (overlap proven)
  D. zero-byte cached CSV -> recover  (S3: refetch; earlier: raise EmptyDataError)
  E. parseable CSV, Close column, no usable date/Close pair -> recover
  F. invalid refresh over a valid CSV -> preserve original byte-for-byte
  G. failed/invalid load followed by a valid retry
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

import pandas as pd

import tradingagents  # to prove which revision is imported
from tradingagents.dataflows.vendors.yahoo import common, market, ohlcv

TIMEOUT = 5.0


def pkg_location():
    return os.path.dirname(os.path.abspath(tradingagents.__file__))


def capabilities():
    return {
        "has_range_cache": hasattr(market, "_range_cache"),
        "has_coalescing": hasattr(market, "_range_inflight") and hasattr(ohlcv, "_ohlcv_inflight"),
        "has_recovery": hasattr(ohlcv, "_read_cached_csv") and hasattr(ohlcv, "_has_usable_bar"),
    }


# ---- Fixed fixtures -------------------------------------------------------- #
def range_frame(closes=(105.0, 106.0, 107.5)):
    idx = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [110.0, 111.0, 112.0],
            "Low": [90.0, 91.0, 92.0],
            "Close": list(closes),
            "Volume": [1000, 2000, 3000],
            "Stock Splits": 0.0,
            "Dividends": 0.0,
        },
        index=idx,
    )


def ohlcv_frame():
    idx = pd.DatetimeIndex(["2026-07-16", "2026-07-17"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [105.0, 106.0], "Volume": [1000, 2000],
        },
        index=idx,
    )


def valid_dated_frame():
    """A valid OHLCV frame with Date as a COLUMN (as a cached CSV stores it)."""
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-16", "2026-07-17"]),
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [105.0, 106.0], "Volume": [1000, 2000],
        }
    )


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Provider:
    """Records history() calls; returns queued responses (frames or exceptions)."""

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
    """First call blocks until released; records that the 2nd caller arrived."""

    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.second_entered = threading.Event()

    def history(self, **kw):
        self.calls += 1
        n = self.calls
        if n == 1:
            self.entered.set()
            assert self.release.wait(TIMEOUT), "leader never released"
        else:
            self.second_entered.set()
            self.release.wait(TIMEOUT)
        resp = self.responses[min(n - 1, len(self.responses) - 1)]
        if isinstance(resp, BaseException):
            raise resp
        return resp.copy()


def _run_pair(fn):
    res, err = [None, None], [None, None]

    def wrap(i):
        try:
            res[i] = fn()
        except BaseException as e:  # noqa: BLE001
            err[i] = type(e).__name__

    a = threading.Thread(target=wrap, args=(0,))
    b = threading.Thread(target=wrap, args=(1,))
    a.start()
    b.start()
    return a, b, res, err


def _wait(pred):
    d = time.monotonic() + TIMEOUT
    while time.monotonic() < d:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _join(*ts):
    for t in ts:
        t.join(TIMEOUT)
    return all(not t.is_alive() for t in ts)


# ---- A: ten identical sequential range queries ---------------------------- #
def scenario_a(caps):
    if caps["has_range_cache"]:
        market._range_cache.clear()
    provider = Provider([range_frame()])
    with mock.patch.object(market.yf, "Ticker", lambda s: provider):
        outs = [market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06") for _ in range(10)]
    return {
        "provider_calls": provider.calls,
        "all_outputs_identical": len(set(outs)) == 1,
        "output_sha256": sha(outs[0].encode()),
    }


# ---- B: two overlapping cold requests, date-range ------------------------- #
def scenario_b(caps):
    if caps["has_range_cache"]:
        market._range_cache.clear()
    provider = BlockingProvider([range_frame()])
    with mock.patch.object(market.yf, "Ticker", lambda s: provider):
        def call():
            return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

        a, b, res, err = _run_pair(call)
        assert provider.entered.wait(TIMEOUT)
        if caps["has_coalescing"]:
            key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")
            overlapped = _wait(lambda: market._range_inflight.waiter_count(key) == 1)
            mode = "follower joined same in-flight flight"
        else:
            overlapped = _wait(provider.second_entered.is_set)
            mode = "second provider call entered while first loading"
        provider.release.set()
        finished = _join(a, b)
    return {
        "provider_calls": provider.calls,
        "overlapped_while_loading": overlapped,
        "overlap_mode": mode,
        "both_finished": finished,
        "errors": err,
        "outputs_identical": res[0] == res[1],
    }


# ---- C: two overlapping cold requests, OHLCV ------------------------------ #
def scenario_c(caps, tmpdir):
    provider = BlockingProvider([ohlcv_frame()])
    patches = [
        mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": tmpdir}),
        mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))),
        mock.patch.object(common, "vendor_reachable", lambda *a, **k: True),
        mock.patch.object(ohlcv.yf, "Ticker", lambda s: provider),
    ]
    for p in patches:
        p.start()
    try:
        def call():
            return ohlcv.load_ohlcv("AAPL", "2026-07-17")

        a, b, res, err = _run_pair(call)
        assert provider.entered.wait(TIMEOUT)
        if caps["has_coalescing"]:
            key = (os.path.abspath(tmpdir), "AAPL")
            overlapped = _wait(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1)
            mode = "follower joined same in-flight flight"
        else:
            overlapped = _wait(provider.second_entered.is_set)
            mode = "second provider call entered while first loading"
        provider.release.set()
        finished = _join(a, b)
    finally:
        for p in reversed(patches):
            p.stop()
    same = res[0] is not None and res[1] is not None and \
        res[0].reset_index(drop=True).equals(res[1].reset_index(drop=True))
    return {
        "provider_calls": provider.calls,
        "overlapped_while_loading": overlapped,
        "overlap_mode": mode,
        "both_finished": finished,
        "errors": err,
        "outputs_identical": same,
    }


# ---- D: zero-byte cached CSV -> recover ----------------------------------- #
def scenario_d(caps, tmpdir):
    cache_file = os.path.join(tmpdir, "AAPL-YFin-data.csv")
    open(cache_file, "wb").close()
    provider = Provider([valid_dated_frame().set_index("Date")])
    with mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": tmpdir}), \
         mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))), \
         mock.patch.object(common, "vendor_reachable", lambda *a, **k: True), \
         mock.patch.object(ohlcv.yf, "Ticker", lambda s: provider):
        try:
            out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
            result = {"outcome": "returned", "rows": int(len(out))}
        except BaseException as e:  # noqa: BLE001
            result = {"outcome": "raised", "error": type(e).__name__}
    result["provider_calls"] = provider.calls
    result["disk_size"] = os.path.getsize(cache_file)
    result["disk_usable"] = result["disk_size"] > 0
    return result


# ---- E: parseable CSV, Close column, no usable date/Close pair ------------ #
def scenario_e(caps, tmpdir):
    cache_file = os.path.join(tmpdir, "AAPL-YFin-data.csv")
    # Fresh, parseable, has Close, but valid date and valid close on different rows.
    frame = pd.DataFrame({
        "Date": ["2026-07-16", ""], "Open": [100.0, 101.0], "High": [110.0, 111.0],
        "Low": [90.0, 91.0], "Close": [float("nan"), 101.0], "Volume": [1000, 2000],
    })
    frame.to_csv(cache_file, index=False)
    now = pd.Timestamp("2026-07-18 12:00")
    t = now.to_pydatetime().timestamp()
    os.utime(cache_file, (t, t))
    provider = Provider([valid_dated_frame().set_index("Date")])
    with mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": tmpdir}), \
         mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: now)), \
         mock.patch.object(common, "vendor_reachable", lambda *a, **k: True), \
         mock.patch.object(ohlcv.yf, "Ticker", lambda s: provider):
        try:
            out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
            result = {"outcome": "returned", "last_close": float(out["Close"].iloc[-1])}
        except BaseException as e:  # noqa: BLE001
            result = {"outcome": "raised", "error": type(e).__name__}
    result["provider_calls"] = provider.calls
    # Was a valid replacement written?
    try:
        disk = pd.read_csv(cache_file)
        result["disk_last_close"] = float(disk["Close"].iloc[-1]) if "Close" in disk.columns else None
    except BaseException:
        result["disk_last_close"] = "unreadable"
    return result


# ---- F: invalid refresh over a valid CSV -> preserve original ------------- #
def scenario_f(caps, tmpdir):
    cache_file = os.path.join(tmpdir, "AAPL-YFin-data.csv")
    valid_dated_frame().to_csv(cache_file, index=False)
    now = pd.Timestamp("2026-07-18 12:00")
    stale = (now - pd.Timedelta(seconds=ohlcv.OHLCV_CACHE_TTL_SECONDS + 60)).to_pydatetime().timestamp()
    os.utime(cache_file, (stale, stale))
    with open(cache_file, "rb") as fh:
        before = fh.read()

    bad = pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-18"]), "Open": [102.0], "High": [112.0],
        "Low": [92.0], "Close": [float("nan")], "Volume": [3000],
    }).set_index("Date")
    provider = Provider([bad])
    with mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": tmpdir}), \
         mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: now)), \
         mock.patch.object(common, "vendor_reachable", lambda *a, **k: True), \
         mock.patch.object(ohlcv.yf, "Ticker", lambda s: provider):
        try:
            ohlcv.load_ohlcv("AAPL", "2026-07-18")
            result = {"outcome": "returned"}
        except BaseException as e:  # noqa: BLE001
            result = {"outcome": "raised", "error": type(e).__name__}
    result["provider_calls"] = provider.calls
    with open(cache_file, "rb") as fh:
        after = fh.read()
    result["valid_csv_preserved_bytewise"] = (before == after)
    result["disk_close"] = float(pd.read_csv(cache_file)["Close"].iloc[-1])
    return result


# ---- G: failed/invalid load then valid retry ------------------------------ #
def scenario_g(caps, tmpdir):
    cache_file = os.path.join(tmpdir, "AAPL-YFin-data.csv")
    from tradingagents.dataflows.errors import VendorUnavailableError
    bad = pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-17"]), "Open": [100.0], "High": [110.0],
        "Low": [90.0], "Close": [float("nan")], "Volume": [1000],
    }).set_index("Date")
    # First: provider exception. Second: invalid data. Third: valid. (Pick by stage.)
    provider = Provider([VendorUnavailableError("boom"), bad, valid_dated_frame().set_index("Date")])
    with mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": tmpdir}), \
         mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))), \
         mock.patch.object(common, "vendor_reachable", lambda *a, **k: True), \
         mock.patch.object(ohlcv.yf, "Ticker", lambda s: provider):
        steps = []
        for _ in range(3):
            try:
                ohlcv.load_ohlcv("AAPL", "2026-07-17")
                steps.append({"outcome": "returned", "calls": provider.calls,
                              "disk_exists": os.path.exists(cache_file)})
            except BaseException as e:  # noqa: BLE001
                steps.append({"outcome": "raised", "error": type(e).__name__,
                              "calls": provider.calls, "disk_exists": os.path.exists(cache_file)})
    return {"steps": steps}


def main():
    label = sys.argv[1]
    expected_rev = sys.argv[2]
    expected_frag = sys.argv[3]
    caps = capabilities()
    loc = pkg_location()

    report = {
        "label": label,
        "expected_rev": expected_rev,
        "imported_pkg": loc,
        "imported_from_expected_worktree": expected_frag in loc,
        "capabilities": caps,
    }
    report["A_ten_sequential_range"] = scenario_a(caps)
    report["B_overlap_range"] = scenario_b(caps)
    with tempfile.TemporaryDirectory() as d:
        report["C_overlap_ohlcv"] = scenario_c(caps, d)
    with tempfile.TemporaryDirectory() as d:
        report["D_zero_byte_recover"] = scenario_d(caps, d)
    with tempfile.TemporaryDirectory() as d:
        report["E_contentless_recover"] = scenario_e(caps, d)
    with tempfile.TemporaryDirectory() as d:
        report["F_invalid_refresh_preserve"] = scenario_f(caps, d)
    with tempfile.TemporaryDirectory() as d:
        report["G_fail_then_retry"] = scenario_g(caps, d)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
