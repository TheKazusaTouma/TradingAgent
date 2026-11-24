"""Offline demo of the Yahoo cache statistics (Stage 4).

Runs six scenarios with mocked providers and temporary cache directories,
printing, per scenario, the cache-stat deltas and the mock provider call count
(the latter tracked separately so provider loads are never conflated with cache
statistics). Exits non-zero if any scenario's numbers are not what is expected.

Requirements: the project dependencies (see pyproject.toml), in particular pandas.
No network access is used and no personal paths are required.

Run from the repository root (use your interpreter: ``python``, ``.venv/bin/python``,
etc.):

    python scripts/demo_cache_stats.py

Self-contained scenarios (each with expected cache stats and mock provider calls):
  1. ten identical sequential range queries  -> 1 load, 9 memory hits
  2. two overlapping cold requests (range and OHLCV) -> 1 load, 1 wait each
  3. valid disk-cache hit                     -> 1 disk hit, 0 loads
  4. corrupt OHLCV CSV                         -> 1 load, usable replacement
  5. invalid refresh over a valid CSV          -> 1 load, 1 failure, file preserved
  6. shared load failure (range and OHLCV)     -> 1 load, 1 wait, 1 failure
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import cache, common, market, ohlcv

TIMEOUT = 5.0
FIELD_ORDER = ("memory_hits", "disk_hits", "coalesced_waits", "load_attempts", "load_failures")


def frame(closes=(105.0, 106.0, 107.5)):
    idx = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"], name="Date")
    return pd.DataFrame(
        {"Open": [100.0, 101.0, 102.0], "High": [110.0, 111.0, 112.0],
         "Low": [90.0, 91.0, 92.0], "Close": list(closes),
         "Volume": [1000, 2000, 3000], "Stock Splits": 0.0, "Dividends": 0.0},
        index=idx,
    )


def ohlcv_frame():
    idx = pd.DatetimeIndex(["2026-07-16", "2026-07-17"], name="Date")
    return pd.DataFrame({"Open": [100.0, 101.0], "High": [110.0, 111.0],
                         "Low": [90.0, 91.0], "Close": [105.0, 106.0],
                         "Volume": [1000, 2000]}, index=idx)


def valid_csv():
    return pd.DataFrame({"Date": pd.to_datetime(["2026-07-16", "2026-07-17"]),
                         "Open": [100.0, 101.0], "High": [110.0, 111.0],
                         "Low": [90.0, 91.0], "Close": [105.0, 106.0],
                         "Volume": [1000, 2000]})


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def history(self, **kw):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, BaseException):
            raise r
        return r.copy()


class BlockingProvider(Provider):
    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()

    def history(self, **kw):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(TIMEOUT)
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, BaseException):
            raise r
        return r.copy()


def delta(before, after):
    return {k: after[k] - before[k] for k in FIELD_ORDER}


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


def _patch_ticker(module, provider):
    return _Ctx(lambda: setattr(module.yf, "Ticker", lambda s: provider),
                lambda: setattr(module.yf, "Ticker", _ORIG[module]))


class _Ctx:
    def __init__(self, enter, exit):
        self._e, self._x = enter, exit

    def __enter__(self):
        self._e()
        return self

    def __exit__(self, *a):
        self._x()


_ORIG = {market: market.yf.Ticker, ohlcv: ohlcv.yf.Ticker}


def scenario_1():
    cache.STATS.reset()
    market._range_cache.clear()
    p = Provider([frame()])
    with _patch_ticker(market, p):
        outs = [market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06") for _ in range(10)]
    s = cache.STATS.snapshot()
    return {"scenario": "1 ten sequential range queries", "provider_calls": p.calls,
            "stats": s, "identical": len(set(outs)) == 1,
            "expect": {"provider_calls": 1, "memory_hits": 9, "load_attempts": 1}}


def scenario_2_path(which):
    cache.STATS.reset()
    p = BlockingProvider([frame() if which == "range" else ohlcv_frame()])
    tmp = tempfile.mkdtemp()
    mps_backup = {}
    try:
        if which == "range":
            market._range_cache.clear()
            market.yf.Ticker = lambda s: p
            key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")

            def call():
                return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

            def waiter():
                return market._range_inflight.waiter_count(key)

        else:
            mps_backup["cfg"] = ohlcv.get_config
            mps_backup["today"] = ohlcv.pd.Timestamp.today
            ohlcv.get_config = lambda: {"data_cache_dir": tmp}
            ohlcv.pd.Timestamp.today = staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))
            common.vendor_reachable = lambda *a, **k: True
            ohlcv.yf.Ticker = lambda s: p
            key = (os.path.abspath(tmp), "AAPL")

            def call():
                return ohlcv.load_ohlcv("AAPL", "2026-07-17")

            def waiter():
                return ohlcv._ohlcv_inflight.waiter_count(key)

        a, b, res, err = _run_pair(call)
        assert p.entered.wait(TIMEOUT)
        joined = _wait(waiter, key)
        p.release.set()
        a.join(TIMEOUT)
        b.join(TIMEOUT)
    finally:
        for k, v in mps_backup.items():
            if k == "cfg":
                ohlcv.get_config = v
            if k == "today":
                ohlcv.pd.Timestamp.today = v
        if which == "range":
            market.yf.Ticker = _ORIG[market]
        else:
            ohlcv.yf.Ticker = _ORIG[ohlcv]
    s = cache.STATS.snapshot()
    return {"scenario": f"2 overlap cold ({which})", "provider_calls": p.calls,
            "stats": s, "joined_while_blocked": joined, "errors": err,
            "expect": {"provider_calls": 1, "coalesced_waits": 1, "memory_hits": 0, "disk_hits": 0}}


def _wait(pred, key):
    d = time.monotonic() + TIMEOUT
    while time.monotonic() < d:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def scenario_3():
    cache.STATS.reset()
    tmp = tempfile.mkdtemp()
    f = os.path.join(tmp, "AAPL-YFin-data.csv")
    valid_csv().to_csv(f, index=False)
    t = pd.Timestamp("2026-07-18 12:00").to_pydatetime().timestamp()
    os.utime(f, (t, t))
    old_cfg, old_today = ohlcv.get_config, ohlcv.pd.Timestamp.today
    ohlcv.get_config = lambda: {"data_cache_dir": tmp}
    ohlcv.pd.Timestamp.today = staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))
    try:
        ohlcv.yf.Ticker = lambda s: type("P", (), {"history": lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download"))})()
        ohlcv.load_ohlcv("AAPL", "2026-07-17")
    finally:
        ohlcv.yf.Ticker = _ORIG[ohlcv]
        ohlcv.get_config, ohlcv.pd.Timestamp.today = old_cfg, old_today
    s = cache.STATS.snapshot()
    return {"scenario": "3 valid disk hit", "provider_calls": 0, "stats": s,
            "expect": {"disk_hits": 1, "load_attempts": 0}}


def scenario_4():
    cache.STATS.reset()
    tmp = tempfile.mkdtemp()
    f = os.path.join(tmp, "AAPL-YFin-data.csv")
    with open(f, "wb"):
        pass
    old_cfg, old_today = ohlcv.get_config, ohlcv.pd.Timestamp.today
    ohlcv.get_config = lambda: {"data_cache_dir": tmp}
    ohlcv.pd.Timestamp.today = staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))
    common.vendor_reachable = lambda *a, **k: True
    p = Provider([valid_csv().set_index("Date")])
    try:
        ohlcv.yf.Ticker = lambda s: p
        ohlcv.load_ohlcv("AAPL", "2026-07-17")
    finally:
        ohlcv.yf.Ticker = _ORIG[ohlcv]
        ohlcv.get_config, ohlcv.pd.Timestamp.today = old_cfg, old_today
    s = cache.STATS.snapshot()
    return {"scenario": "4 corrupt CSV recovers", "provider_calls": p.calls, "stats": s,
            "disk_size": os.path.getsize(f), "expect": {"load_attempts": 1, "usable_file": True}}


def scenario_5():
    cache.STATS.reset()
    tmp = tempfile.mkdtemp()
    f = os.path.join(tmp, "AAPL-YFin-data.csv")
    valid_csv().to_csv(f, index=False)
    now = pd.Timestamp("2026-07-18 12:00")
    stale = (now - pd.Timedelta(seconds=ohlcv.OHLCV_CACHE_TTL_SECONDS + 60)).to_pydatetime().timestamp()
    os.utime(f, (stale, stale))
    with open(f, "rb") as fh:
        before = fh.read()
    old_cfg, old_today = ohlcv.get_config, ohlcv.pd.Timestamp.today
    ohlcv.get_config = lambda: {"data_cache_dir": tmp}
    ohlcv.pd.Timestamp.today = staticmethod(lambda: now)
    common.vendor_reachable = lambda *a, **k: True
    bad = pd.DataFrame({"Date": pd.to_datetime(["2026-07-18"]), "Open": [102.0],
                        "High": [112.0], "Low": [92.0], "Close": [float("nan")],
                        "Volume": [3000]}).set_index("Date")
    p = Provider([bad])
    raised = None
    try:
        ohlcv.yf.Ticker = lambda s: p
        try:
            ohlcv.load_ohlcv("AAPL", "2026-07-18")
        except NoMarketDataError as e:
            raised = type(e).__name__
    finally:
        ohlcv.yf.Ticker = _ORIG[ohlcv]
        ohlcv.get_config, ohlcv.pd.Timestamp.today = old_cfg, old_today
    with open(f, "rb") as fh:
        after = fh.read()
    s = cache.STATS.snapshot()
    return {"scenario": "5 invalid refresh preserves file", "provider_calls": p.calls, "stats": s,
            "preserved": before == after, "raised": raised,
            "expect": {"load_attempts": 1, "load_failures": 1, "preserved": True}}


def scenario_6_path(which):
    cache.STATS.reset()
    tmp = tempfile.mkdtemp()
    p = BlockingProvider([VendorUnavailableError("boom")])
    backup = {}
    try:
        if which == "range":
            market._range_cache.clear()
            market.yf.Ticker = lambda s: p
            key = market.range_cache_key("AAPL", "yahoo", "ohlcv", "2026-01-02", "2026-01-07")

            def call():
                return market.get_YFin_data_online("AAPL", "2026-01-02", "2026-01-06")

            def waiter():
                return market._range_inflight.waiter_count(key)

        else:
            backup["cfg"], backup["today"] = ohlcv.get_config, ohlcv.pd.Timestamp.today
            ohlcv.get_config = lambda: {"data_cache_dir": tmp}
            ohlcv.pd.Timestamp.today = staticmethod(lambda: pd.Timestamp("2026-07-18 12:00"))
            common.vendor_reachable = lambda *a, **k: True
            ohlcv.yf.Ticker = lambda s: p
            key = (os.path.abspath(tmp), "AAPL")

            def call():
                return ohlcv.load_ohlcv("AAPL", "2026-07-17")

            def waiter():
                return ohlcv._ohlcv_inflight.waiter_count(key)

        a, b, res, err = _run_pair(call)
        assert p.entered.wait(TIMEOUT)
        joined = _wait(waiter, key)
        p.release.set()
        a.join(TIMEOUT)
        b.join(TIMEOUT)
    finally:
        if which == "range":
            market.yf.Ticker = _ORIG[market]
        else:
            ohlcv.yf.Ticker = _ORIG[ohlcv]
            ohlcv.get_config, ohlcv.pd.Timestamp.today = backup["cfg"], backup["today"]
    s = cache.STATS.snapshot()
    return {"scenario": f"6 shared load failure ({which})", "provider_calls": p.calls, "stats": s,
            "errors": err, "joined_while_blocked": joined,
            "expect": {"load_attempts": 1, "coalesced_waits": 1, "load_failures": 1}}


def fmt_lines(rec):
    out = [f"\n=== {rec['scenario']} ==="]
    out.append(f"  mock provider calls : {rec['provider_calls']}")
    out.append("  cache stats         : " + ", ".join(f"{k}={rec['stats'][k]}" for k in FIELD_ORDER))
    for extra in ("identical", "joined_while_blocked", "preserved", "raised", "errors", "disk_size"):
        if extra in rec:
            out.append(f"  {extra:<19} : {rec[extra]}")
    out.append(f"  expected            : {rec['expect']}")
    return out, rec["stats"], rec["provider_calls"]


def main():
    print("Offline Yahoo cache statistics demo (mocked providers, temp dirs)")
    print("Note: 'mock provider calls' is tracked separately from cache stats.\n")
    records = [
        scenario_1(),
        scenario_2_path("range"),
        scenario_2_path("ohlcv"),
        scenario_3(),
        scenario_4(),
        scenario_5(),
        scenario_6_path("range"),
        scenario_6_path("ohlcv"),
    ]
    ok = True
    for rec in records:
        lines, stats, calls = fmt_lines(rec)
        print("\n".join(lines))
        exp = rec["expect"]
        for key, want in exp.items():
            if key == "provider_calls":
                got = calls
            elif key == "usable_file":
                got = rec.get("disk_size", 0) > 0
            elif key == "preserved":
                got = rec.get("preserved")
            else:
                got = stats.get(key)
            if got != want:
                print(f"  !! MISMATCH {key}: got {got!r}, want {want!r}")
                ok = False
    print("\n" + ("ALL SCENARIOS OK" if ok else "SOME SCENARIOS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
