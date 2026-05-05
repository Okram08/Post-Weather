"""Exchange OHLCV + funding rate fetcher.

Supports: binance, bybit, okx, hyperliquid.

Hyperliquid quirks:
  - USDC-margined perps (BTC/USDC:USDC, not USDT)
  - Funding paid hourly (not 8h)
  - OHLCV: API may return fewer bars than requested limit; advance by last bar
    timestamp, and jump by chunk_window only when API returns ZERO bars
    (to skip pre-listing periods)
"""
import time

import ccxt
import pandas as pd


_DATA_PY_VERSION = "v3-hl-pagination-fix-20260505"


TIMEFRAME_MS = {
    "1m": 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def make_exchange(name: str = "bybit"):
    print("[data.py " + _DATA_PY_VERSION + "] make_exchange: " + name)
    if name == "binance":
        return ccxt.binance({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    if name == "bybit":
        return ccxt.bybit({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    if name == "okx":
        return ccxt.okx({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    if name == "hyperliquid":
        return ccxt.hyperliquid({"enableRateLimit": True})
    raise ValueError("Unsupported exchange: " + name)


def fetch_ohlcv_recent(exchange, symbol: str, timeframe: str, n_bars: int = 300) -> pd.DataFrame:
    bars = exchange.fetch_ohlcv(symbol, timeframe, limit=n_bars)
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts")


def fetch_current_funding(exchange, symbol: str) -> float:
    try:
        if exchange.id == "hyperliquid":
            rates = exchange.fetch_funding_rate_history(symbol, limit=1)
            if rates:
                return float(rates[-1].get("fundingRate", 0.0) or 0.0)
            return 0.0
        rate = exchange.fetch_funding_rate(symbol)
        return float(rate.get("fundingRate", 0.0) or 0.0)
    except Exception as e:
        print("  [funding] error for " + symbol + ": " + str(e))
        return 0.0


def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str,
                          since_ms: int, end_ms: int,
                          max_errors: int = 5) -> pd.DataFrame:
    """Fetch OHLCV in chunks. Aborts after `max_errors` consecutive failures.

    Logic:
      - If API returns bars: advance cursor to (last_bar_ts + 1 timeframe).
      - If API returns NO bars: jump forward by skip_window (to skip pre-listing).
      - Stop when cursor passes end_ms or we get 3 empty results in a row past
        the listing date.
    """
    print("  [data.py " + _DATA_PY_VERSION + "] fetch_ohlcv_paginated: "
          + symbol + " " + timeframe
          + " from=" + str(pd.Timestamp(since_ms, unit='ms', tz='UTC'))
          + " to=" + str(pd.Timestamp(end_ms, unit='ms', tz='UTC')))

    all_bars = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"
    tf_ms = TIMEFRAME_MS.get(timeframe, 60 * 60 * 1000)

    # Per-call limit
    chunk_limit = 5000 if is_hl else 1000
    # Skip window when no bars returned (used only for HL to skip pre-listing)
    skip_window_ms = 30 * 24 * 60 * 60 * 1000  # 30 days
    consecutive_empty = 0
    max_consecutive_empty = 6  # allow up to 6 months of empty before giving up

    cursor_ms = since_ms
    iteration = 0
    while cursor_ms < end_ms:
        iteration += 1
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if bars and len(bars) > 0:
                consecutive_empty = 0
                all_bars.extend(bars)
                last_ts = bars[-1][0]
                # Always advance by last_ts + 1 timeframe (never skip valid data)
                new_cursor = last_ts + tf_ms
                if new_cursor <= cursor_ms:
                    # Defensive: if API returned bars before our since, force advance
                    new_cursor = cursor_ms + tf_ms * chunk_limit
                cursor_ms = new_cursor
                if iteration % 5 == 0:
                    print("    iter " + str(iteration) + ": "
                          + str(len(all_bars)) + " bars cumul, cursor="
                          + str(pd.Timestamp(cursor_ms, unit='ms', tz='UTC')))
            else:
                # Empty response
                consecutive_empty += 1
                if not is_hl:
                    # For non-HL exchanges, empty = end of data, stop
                    break
                # For HL: skip forward to find first available data
                if consecutive_empty >= max_consecutive_empty:
                    print("    " + str(consecutive_empty) + " consecutive empty responses, stopping")
                    break
                cursor_ms += skip_window_ms
                print("    empty response (consecutive=" + str(consecutive_empty)
                      + "), skipping +30d to "
                      + str(pd.Timestamp(cursor_ms, unit='ms', tz='UTC')))

            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            errors += 1
            print("  retry ohlcv " + str(errors) + "/" + str(max_errors) + ": " + str(e))
            if errors >= max_errors:
                raise RuntimeError(
                    "Aborted ohlcv fetch after " + str(max_errors) +
                    " consecutive errors. Last error: " + str(e)
                )
            time.sleep(2)

    print("    [done] total bars fetched: " + str(len(all_bars)))

    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]


def fetch_funding_paginated(exchange, symbol: str,
                            since_ms: int, end_ms: int,
                            max_errors: int = 5) -> pd.DataFrame:
    """Fetch funding rate history. Same advance logic as ohlcv."""
    print("  [data.py " + _DATA_PY_VERSION + "] fetch_funding_paginated: " + symbol)

    all_rates = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"
    # Funding interval (1h on HL, 8h on others)
    funding_interval_ms = 60 * 60 * 1000 if is_hl else 8 * 60 * 60 * 1000
    chunk_limit = 500 if is_hl else 1000
    skip_window_ms = 30 * 24 * 60 * 60 * 1000
    consecutive_empty = 0
    max_consecutive_empty = 6

    cursor_ms = since_ms
    while cursor_ms < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if rates and len(rates) > 0:
                consecutive_empty = 0
                all_rates.extend(rates)
                last_ts = rates[-1]["timestamp"]
                new_cursor = last_ts + funding_interval_ms
                if new_cursor <= cursor_ms:
                    new_cursor = cursor_ms + funding_interval_ms * chunk_limit
                cursor_ms = new_cursor
            else:
                consecutive_empty += 1
                if not is_hl:
                    break
                if consecutive_empty >= max_consecutive_empty:
                    break
                cursor_ms += skip_window_ms

            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            errors += 1
            print("  retry funding " + str(errors) + "/" + str(max_errors) + ": " + str(e))
            if errors >= max_errors:
                raise RuntimeError(
                    "Aborted funding fetch after " + str(max_errors) +
                    " consecutive errors. Last error: " + str(e)
                )
            time.sleep(2)

    print("    [done] total funding rates fetched: " + str(len(all_rates)))

    if not all_rates:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
         "funding_rate": r["fundingRate"]}
        for r in all_rates
    ])
    return df.drop_duplicates("ts").set_index("ts").sort_index()
