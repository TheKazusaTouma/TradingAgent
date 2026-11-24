import logging
import time
from datetime import datetime
from typing import Annotated

import numpy as np
import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.cache import (
    STATS,
    _Inflight,
    _RangeCache,
    range_cache_key,
)
from tradingagents.dataflows.vendors.yahoo.common import price_text, raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import (
    _assert_range_usable_and_fresh,
    load_ohlcv,
)

logger = logging.getLogger(__name__)

# One process-local cache for date-range downloads. Raw frames are keyed by the
# normalized request identity so a repeated query within the TTL reuses one
# provider load; a failed load is never stored.
_range_cache = _RangeCache()

# Concurrent identical misses share one provider load instead of each issuing
# their own download.
_range_inflight = _Inflight()


def _now() -> float:
    """Monotonic clock for cache freshness (patched by tests with a fake clock)."""
    return time.monotonic()


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    refresh: bool = False,
):

    datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Resolve broker/forex symbols to Yahoo's convention (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)

    # yfinance treats ``end`` as EXCLUSIVE, so it would drop the requested
    # end_date row (and the current day when end_date is today). Request one day
    # past end_date so the requested range is actually inclusive (#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")

    # Reuse a raw download for an identical request within its TTL. ``refresh``
    # bypasses the old entry and a successful load re-populates it.
    key = range_cache_key(canonical, "yahoo", "ohlcv", start_date, end_inclusive)
    now = _now()
    data = None if refresh else _range_cache.get(key, now)
    if data is not None:
        STATS.incr("memory_hits")

    if data is None:
        # Coalesce concurrent identical misses into one provider load. The
        # loader rechecks the cache first, so a request that loses the race but
        # arrives after another already stored the frame is served from it.
        # A forced caller joining a recheck-only flight is pushed back through
        # this same loading path (see _Inflight.run's require_fresh) so it still
        # obtains provider data without a separate refresh namespace.
        def _load() -> tuple[pd.DataFrame, bool]:
            cached = None if refresh else _range_cache.get(key, _now())
            if cached is not None:
                # Served from memory inside the flight (the race was lost): a hit.
                STATS.incr("memory_hits")
                return cached, False
            # One load attempt per shared load, before the provider is invoked,
            # so yf_retry's internal retries are not counted separately.
            STATS.incr("load_attempts")
            try:
                loaded = yf_retry(
                    lambda: yf.Ticker(canonical).history(start=start_date, end=end_inclusive)
                )

                # Empty result means the symbol is unknown/delisted. Raise a typed
                # error instead of returning prose: the routing layer turns it into a
                # single unambiguous "no data" signal so the agent never fabricates a
                # price.
                if loaded is None or loaded.empty:
                    raise_for_empty(symbol, canonical, f"rows between {start_date} and {end_date}")

                # Validate the raw download BEFORE storing it or handing it to the
                # waiting callers: a stale frame, or one with no usable finite close,
                # must never enter the success cache or a shared result.
                _assert_range_usable_and_fresh(loaded, end_date, symbol, canonical)

                # Store only after the load succeeded and validated; cache.store
                # keeps its own copy.
                _range_cache.store(key, loaded, _now())
                return loaded, True
            except BaseException:
                # A shared load failure is counted once (only the leader runs this
                # loader), not once per waiting caller.
                STATS.incr("load_failures")
                raise

        data = _range_inflight.run(key, _load, require_fresh=refresh)
    # On a hit (cache or shared in-flight) this caller holds an independent copy,
    # so ``data`` is safe to format in place below.

    # Remove timezone info from index for cleaner output
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    # Coerce non-finite closes (±inf) to missing so they render as empty rather
    # than "inf" in the CSV. The date index and column layout are untouched, and
    # no OHLCV-only transformation (row dropping, column renames) is applied.
    for col in ("Open", "High", "Low", "Close", "Adj Close"):
        if col in data.columns:
            numeric = pd.to_numeric(data[col], errors="coerce").astype("float64")
            data[col] = numeric.where(np.isfinite(numeric))

    # Defense in depth for the warm (cache-hit) path too: reject a frame with no
    # usable finite close, or one whose latest usable row is stale, before
    # formatting. Fresh loads were already validated before being stored.
    _assert_range_usable_and_fresh(data, end_date, symbol, canonical)

    for col in ("Open", "High", "Low", "Close", "Adj Close"):
        if col in data.columns:
            data[col] = data[col].map(price_text, na_action="ignore")

    csv_string = data.to_csv()

    # Name the resolved symbol when it differs, so the reader sees which
    # instrument was priced.
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"

    return header + csv_string


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:

    best_ind_params = {
        # Moving Averages
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD Related
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # Momentum Indicators
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # Volatility Indicators
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # Volume-Based Indicators
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    end_date = as_of_date
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of_dt - relativedelta(days=look_back_days)

    # Optimized: Get stock data once and calculate indicators for all dates
    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, as_of_date)

        # Generate the date range we need
        current_dt = as_of_dt
        date_values = []

        while current_dt >= before:
            date_str = current_dt.strftime('%Y-%m-%d')

            # Look up the indicator value for this date
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
            else:
                indicator_value = "N/A: Not a trading day (weekend or holiday)"

            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"

    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        logger.warning("Bulk stockstats fetch failed, falling back per-day: %s", e)
        # Fallback to original implementation if bulk method fails
        ind_string = ""
        as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
        while as_of_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, as_of_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{as_of_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            as_of_dt = as_of_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )

    return result_str


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    as_of_date: Annotated[str, "current date for reference"]
) -> dict:
    """
    Optimized bulk calculation of stock stats indicators.
    Fetches data once and calculates indicator for all available dates.
    Returns dict mapping date strings to indicator values.
    """
    from stockstats import wrap

    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    df[indicator]  # This triggers stockstats to calculate the indicator

    result_dict = {}
    for _, row in df.iterrows():
        date_str = row["Date"]
        indicator_value = row[indicator]

        if pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)

    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
) -> str:

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    as_of_date = as_of_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = get_stock_stats(
            symbol,
            indicator,
            as_of_date,
        )
    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        # An empty string renders as "2026-05-08: " in the indicator table, which
        # reads as no value that day rather than a read that failed. Raise so the
        # router can try the next vendor or report the series unavailable.
        raise NoMarketDataError(
            symbol, symbol, f"{indicator} could not be read for {as_of_date}: {e}"
        ) from e

    return str(indicator_value)


def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes from ``start_date`` up to, not including, ``end_date``."""
    canonical = normalize_symbol(symbol)
    history = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_date))
    return history["Close"] if history is not None and "Close" in history else pd.Series(dtype=float)


def get_stock_stats(
    symbol: Annotated[str, "ticker symbol for the company"],
    indicator: Annotated[
        str, "quantitative indicators based off of the stock data for the company"
    ],
    as_of_date: Annotated[
        str, "curr date for retrieving stock price data, YYYY-mm-dd"
    ],
):
    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    as_of_str = pd.to_datetime(as_of_date).strftime("%Y-%m-%d")

    df[indicator]  # trigger stockstats to calculate the indicator
    matching_rows = df[df["Date"].str.startswith(as_of_str)]

    if not matching_rows.empty:
        indicator_value = matching_rows[indicator].values[0]
        return indicator_value
    else:
        return "N/A: Not a trading day (weekend or holiday)"
