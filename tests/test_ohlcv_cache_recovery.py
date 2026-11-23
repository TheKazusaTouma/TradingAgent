"""Stage 3: the five-year OHLCV CSV cache recovers from bad content.

A cached CSV can be unreadable (zero-byte, malformed) or contentless (a Close
column but no row carrying both a date and a close). A download can be equally
unusable. Neither may crash the call, be served, or overwrite a good file.

Rules verified here:
  * a bad cached file is treated as a miss and refetched (no crash);
  * a download is validated for storage *before* writing — it needs at least one
    row with both a parseable date and a finite close, judged independently of
    the caller's as_of_date;
  * an invalid download leaves an existing valid CSV byte-for-byte intact.

All offline: the provider is mocked; the clock is fixed; the cache dir is a
temp path.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import common, ohlcv

NOW = pd.Timestamp("2026-07-18 12:00")
FRESH = ohlcv.OHLCV_CACHE_TTL_SECONDS + 60


def _valid(index_dates=("2026-07-16", "2026-07-17"), closes=(100.0, 101.0)):
    """A valid OHLCV frame with Date as a *column* (as the cache file stores it)."""
    dates = pd.to_datetime(list(index_dates))
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [100.0 + i for i in range(len(dates))],
            "High": [110.0 + i for i in range(len(dates))],
            "Low": [90.0 + i for i in range(len(dates))],
            "Close": list(closes),
            "Volume": [1000 * (i + 1) for i in range(len(dates))],
        }
    )


class Provider:
    """Records each history() call; returns the next queued response."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def history(self, **kwargs):
        self.calls += 1
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(resp, BaseException):
            raise resp
        return resp.copy()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Patch config, clock, and reachability; yield (cache_dir, cache_file)."""
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    # Yahoo answers (reachable): "no usable rows" must mean no data, not an outage.
    monkeypatch.setattr(common, "vendor_reachable", lambda *a, **k: True)
    return tmp_path, tmp_path / "AAPL-YFin-data.csv"


def _use(monkeypatch, provider):
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda symbol: provider)
    return provider


def _age_seconds(path, seconds):
    ts = (NOW - pd.Timedelta(seconds=seconds)).to_pydatetime().timestamp()
    os.utime(path, (ts, ts))


# --------------------------------------------------------------------------- #
# G1 — unreadable cached CSV (zero-byte / malformed) recovers by refetching
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g1a_zero_byte_cache_is_a_miss_and_refetched(env, monkeypatch):
    cache_dir, cache_file = env
    cache_file.write_bytes(b"")  # zero-byte, as a prior failed write leaves it
    provider = _use(monkeypatch, Provider([_valid()]))

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")

    assert provider.calls == 1
    assert not out.empty and out["Close"].iloc[-1] == 101.0
    # Disk now holds a usable file.
    assert cache_file.stat().st_size > 0
    assert "Close" in pd.read_csv(cache_file).columns


@pytest.mark.unit
def test_g1b_malformed_cache_is_a_miss_and_refetched(env, monkeypatch):
    cache_dir, cache_file = env
    # An unclosed quote reliably raises ParserError under read_csv(on_bad_lines=skip).
    cache_file.write_text('Date,Open,Close,Volume\n"2026-07-17,1,11,1\n')
    with pytest.raises(pd.errors.ParserError):
        pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")  # fixture sanity
    provider = _use(monkeypatch, Provider([_valid()]))

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")

    assert provider.calls == 1
    assert not out.empty and out["Close"].iloc[-1] == 101.0
    assert cache_file.stat().st_size > 0


# --------------------------------------------------------------------------- #
# G2 — a fresh, parseable CSV with a Close column but no usable date/close pair
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g2_contentless_cache_is_a_miss_and_replaced(env, monkeypatch):
    cache_dir, cache_file = env
    # Fresh (same day), parseable, has a Close column, but no row carries both a
    # date and a close: valid date and valid close are on different rows.
    frame = pd.DataFrame(
        {
            "Date": ["2026-07-16", ""],           # 2nd date unparseable
            "Open": [100.0, 101.0],
            "High": [110.0, 111.0],
            "Low": [90.0, 91.0],
            "Close": [float("nan"), 101.0],        # 1st close missing
            "Volume": [1000, 2000],
        }
    )
    cache_file.write_text(frame.to_csv(index=False))
    _age_seconds(cache_file, 0)  # fresh today
    provider = _use(monkeypatch, Provider([_valid()]))

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")

    assert provider.calls == 1  # contentless cache refetched, not served
    assert not out.empty and out["Close"].iloc[-1] == 101.0
    replaced = pd.read_csv(cache_file)
    assert float(replaced["Close"].iloc[-1]) == 101.0  # valid replacement written


# --------------------------------------------------------------------------- #
# G3 — invalid download is rejected before writing; valid CSV preserved
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g3_invalid_download_does_not_overwrite_a_valid_csv(env, monkeypatch):
    cache_dir, cache_file = env
    good = _valid()
    good.to_csv(cache_file, index=False)
    _age_seconds(cache_file, FRESH)  # current-day request will refetch (TTL expired)
    before = cache_file.read_bytes()

    # Download has valid dates but no close anywhere -> unusable content.
    bad = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-18"]),
            "Open": [102.0], "High": [112.0], "Low": [92.0],
            "Close": [float("nan")], "Volume": [3000],
        }
    ).set_index("Date")
    provider = _use(monkeypatch, Provider([bad]))

    with pytest.raises(NoMarketDataError, match="usable price rows"):
        ohlcv.load_ohlcv("AAPL", "2026-07-18")

    assert provider.calls == 1
    # The critical assertion: the existing valid file is byte-for-byte intact.
    assert cache_file.read_bytes() == before


# --------------------------------------------------------------------------- #
# G4 — download with no usable date/close pair anywhere (cold cache)
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g4_split_date_and_close_rows_is_no_data_and_not_written(env, monkeypatch):
    cache_dir, cache_file = env
    # Valid date on one row, valid close on another — no single usable bar.
    split = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-17", pd.NaT]),
            "Open": [100.0, 101.0], "High": [110.0, 111.0], "Low": [90.0, 91.0],
            "Close": [float("nan"), 101.0], "Volume": [1000, 2000],
        }
    ).set_index("Date")
    provider = _use(monkeypatch, Provider([split]))

    with pytest.raises(NoMarketDataError, match="usable price rows"):
        ohlcv.load_ohlcv("AAPL", "2026-07-17")

    assert provider.calls == 1
    assert not cache_file.exists()  # nothing written for an unusable download


@pytest.mark.unit
def test_g4b_all_nat_dates_is_no_data_and_not_written(env, monkeypatch):
    cache_dir, cache_file = env
    no_dates = pd.DataFrame(
        {
            "Date": [pd.NaT, pd.NaT],
            "Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
            "Close": [10.0, 11.0], "Volume": [1, 2],  # closes valid, dates are not
        }
    ).set_index("Date")
    provider = _use(monkeypatch, Provider([no_dates]))

    with pytest.raises(NoMarketDataError, match="usable price rows"):
        ohlcv.load_ohlcv("AAPL", "2026-07-17")

    assert provider.calls == 1
    assert not cache_file.exists()


# --------------------------------------------------------------------------- #
# G5 — failed (invalid) load then a valid retry: one call per request
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g5_invalid_load_then_valid_retry(env, monkeypatch):
    cache_dir, cache_file = env
    bad = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-17"]),
            "Open": [100.0], "High": [110.0], "Low": [90.0],
            "Close": [float("nan")], "Volume": [1000],
        }
    ).set_index("Date")
    provider = _use(monkeypatch, Provider([bad, _valid()]))

    # First request: invalid download -> error, nothing cached.
    with pytest.raises(NoMarketDataError, match="usable price rows"):
        ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert provider.calls == 1
    assert not cache_file.exists()

    # Second request: valid download -> served and cached.
    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert provider.calls == 2
    assert not out.empty and out["Close"].iloc[-1] == 101.0
    assert cache_file.exists()


# --------------------------------------------------------------------------- #
# Regression guards — valid disk hits and freshness rules still hold
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_g6_fresh_valid_cache_is_served_without_download(env, monkeypatch):
    cache_dir, cache_file = env
    _valid().to_csv(cache_file, index=False)
    _age_seconds(cache_file, 0)  # written moments ago, today

    def _must_not_download(*a, **k):
        raise AssertionError("fresh valid cache must be served, not refetched")

    _use(monkeypatch, type("P", (), {"history": _must_not_download})())

    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert out["Close"].iloc[-1] == 101.0


@pytest.mark.unit
def test_g7_historical_request_uses_todays_cache_past_ttl(env, monkeypatch):
    cache_dir, cache_file = env
    _valid().to_csv(cache_file, index=False)
    _age_seconds(cache_file, FRESH)  # older than TTL, but still written today

    def _must_not_download(*a, **k):
        raise AssertionError("historical request must reuse today's cache")

    _use(monkeypatch, type("P", (), {"history": _must_not_download})())

    # as_of before today: the same-day file is fresh regardless of TTL.
    out = ohlcv.load_ohlcv("AAPL", "2026-07-17")
    assert out["Close"].iloc[-1] == 101.0
