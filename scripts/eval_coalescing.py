"""Offline before/after: Stage 1 (no coalescing) vs Stage 2 (coalescing).

For each Yahoo path (date-range, five-year OHLCV) this makes TWO overlapping
same-key requests from a cold cache and measures how many provider loads occur.

Overlap is *proven*, not assumed: the mock provider's first call blocks on an
``entered`` event until the second caller has demonstrably arrived (either a
second provider call for Stage 1, or a waiter on the same in-flight flight for
Stage 2). A hard timeout guarantees termination.

This script is revision-agnostic: it inspects the ``tradingagents`` package that
is importable and runs against whichever checkout ``PYTHONPATH`` points at, so it
can compare a baseline checkout with a later one. It prints a JSON summary;
provider call counts and per-caller outputs are compared across versions.

Requirements: the project dependencies (see pyproject.toml), in particular pandas.
No network access and no personal paths are required.

Run from the repository root, passing a ref label just for the printed report:

    PYTHONPATH=. python scripts/eval_coalescing.py <ref>

To compare two revisions, run it once per isolated checkout (e.g. a git worktree
of each commit), with PYTHONPATH set to that checkout:

    PYTHONPATH=/path/to/checkout python /path/to/checkout/scripts/eval_coalescing.py 97bb88e

Reference commits used by the Stage 1/Stage 2 comparison:
    003082f  Stage 1 (in-memory range cache, no coalescing)
    97bb88e  Stage 2 (adds request coalescing)
"""
import json
import sys
import threading
import time
from unittest import mock

import pandas as pd

from tradingagents.dataflows.vendors.yahoo import market, ohlcv

TIMEOUT = 5.0


# ---- Fixed mock data (identical for every run) ---------------------------- #
def range_frame():
    idx = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [110.0, 111.0, 112.0],
            "Low": [90.0, 91.0, 92.0],
            "Close": [105.0, 106.0, 107.5],
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
            "Open": [100.0, 101.0],
            "High": [110.0, 111.0],
            "Low": [90.0, 91.0],
            "Close": [105.0, 106.0],
            "Volume": [1000, 2000],
        },
        index=idx,
    )


class Run:
    """Shared bookkeeping for one path's overlap experiment."""

    def __init__(self, frame):
        self.frame = frame
        self.calls = 0
        self.entered = threading.Event()       # leader inside provider
        self.release = threading.Event()       # let leader return
        self.second_entered = threading.Event()  # 2nd provider call (Stage 1)
        self._lock = threading.Lock()

    def history(self, **kwargs):
        with self._lock:
            self.calls += 1
            n = self.calls
        if n == 1:
            self.entered.set()
            assert self.release.wait(TIMEOUT), "leader never released"
        else:
            self.second_entered.set()
            # A second load (Stage 1) also waits briefly so overlap is real.
            self.release.wait(TIMEOUT)
        return self.frame.copy()


def run_two_threads(fn):
    results, errors = [None, None], [None, None]

    def wrap(i):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001
            errors[i] = repr(exc)

    t1 = threading.Thread(target=wrap, args=(0,))
    t2 = threading.Thread(target=wrap, args=(1,))
    t1.start()
    t2.start()
    return t1, t2, results, errors


def _wait(predicate):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---- Path A: date-range (get_YFin_data_online) ---------------------------- #
def eval_range(has_inflight: bool) -> dict:
    if hasattr(market, "_range_cache"):
        market._range_cache.clear()
    run = Run(range_frame())
    monkeypatch = mock.patch.object(market.yf, "Ticker", lambda s: run)

    with monkeypatch:
        def call():
            return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

        key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")

        t1, t2, results, errors = run_two_threads(call)
        assert run.entered.wait(TIMEOUT), "leader never entered provider"

        # Prove overlap while the first load is in progress.
        if has_inflight:
            overlapped = _wait(lambda: market._range_inflight.waiter_count(key) == 1)
            overlap_mode = "follower joined same in-flight flight"
        else:
            overlapped = _wait(run.second_entered.is_set)
            overlap_mode = "second provider call entered while first still loading"

        run.release.set()
        t1.join(TIMEOUT)
        t2.join(TIMEOUT)
        done = not t1.is_alive() and not t2.is_alive()

    return {
        "path": "date-range",
        "commit_ref": sys.argv[1] if len(sys.argv) > 1 else "?",
        "provider_calls": run.calls,
        "overlapped_while_loading": overlapped,
        "overlap_mode": overlap_mode,
        "both_finished": done,
        "errors": errors,
        "callers_identical": results[0] == results[1],
        "sample_output_ok": bool(results[0]) and "105.00" in results[0] and "107.50" in results[0],
    }


# ---- Path B: five-year OHLCV (load_ohlcv) --------------------------------- #
def eval_ohlcv(has_inflight: bool, cache_dir: str) -> dict:
    import os

    run = Run(ohlcv_frame())
    mocks = [
        mock.patch.object(ohlcv, "get_config", lambda: {"data_cache_dir": cache_dir}),
        mock.patch.object(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))),
        mock.patch.object(ohlcv.yf, "Ticker", lambda s: run),
    ]
    for m in mocks:
        m.start()
    try:
        def call():
            return ohlcv.load_ohlcv("AAPL", "2026-07-17")

        key = (os.path.abspath(cache_dir), "AAPL")

        t1, t2, results, errors = run_two_threads(call)
        assert run.entered.wait(TIMEOUT), "leader never entered provider"

        if has_inflight:
            overlapped = _wait(lambda: ohlcv._ohlcv_inflight.waiter_count(key) == 1)
            overlap_mode = "follower joined same in-flight flight"
        else:
            overlapped = _wait(run.second_entered.is_set)
            overlap_mode = "second provider call entered while first still loading"

        run.release.set()
        t1.join(TIMEOUT)
        t2.join(TIMEOUT)
        done = not t1.is_alive() and not t2.is_alive()
    finally:
        for m in reversed(mocks):
            m.stop()

    same = False
    if results[0] is not None and results[1] is not None:
        same = results[0].reset_index(drop=True).equals(results[1].reset_index(drop=True))
    sample_ok = (
        results[0] is not None
        and not results[0].empty
        and float(results[0]["Close"].iloc[-1]) == 106.0
    )
    return {
        "path": "ohlcv",
        "commit_ref": sys.argv[1] if len(sys.argv) > 1 else "?",
        "provider_calls": run.calls,
        "overlapped_while_loading": overlapped,
        "overlap_mode": overlap_mode,
        "both_finished": done,
        "errors": errors,
        "callers_identical": same,
        "sample_output_ok": sample_ok,
    }


def main():
    import os
    import tempfile

    import tradingagents

    has_inflight = hasattr(market, "_range_inflight") and hasattr(ohlcv, "_ohlcv_inflight")
    out = {
        "stage": "stage2-97bb88e" if has_inflight else "stage1-003082f",
        "coalescing_present": has_inflight,
        # The package actually imported, so a run against the wrong checkout is visible.
        "imported_pkg": os.path.dirname(os.path.abspath(tradingagents.__file__)),
        "yahoo_market_module": os.path.abspath(market.__file__),
        "date_range": eval_range(has_inflight),
    }
    with tempfile.TemporaryDirectory() as d:
        out["ohlcv"] = eval_ohlcv(has_inflight, d)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
