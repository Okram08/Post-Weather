"""Exchange OHLCV + funding rate fetcher (live + historical).

Supports: binance, bybit, okx, hyperliquid.
- binance/bybit/okx: USDT-margined perpetual swaps (BTC/USDT:USDT)
- hyperliquid: USDC-margined perpetual swaps (BTC/USDC:USDC)
                funding paid hourly (vs 8h elsewhere)

Hyperliquid pagination quirk:
  - OHLCV: max ~5000 candles per call but the API limits the time WINDOW per call,
    not the number of candles returned. So we must paginate by absolute time step,
    not by relying on the last returned candle's timestamp.
  - Funding: 500 max per call, paginate by since/limit.
"""
import time

import ccxt
import pandas as pd


# Map ccxt timeframe strings to milliseconds
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
    if name == "binance":
        return ccxt.binance({
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
    if name == "bybit":
        return ccxt.bybit({
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
    if name == "okx":
        return ccxt.okx({
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
    if name == "hyperliquid":
        return ccxt.hyperliquid({
            "enableRateLimit": True,
        })
    raise ValueError("Unsupported exchange: " + name)


# ---- Live (last N bars / current funding) ----

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


# ---- Historical paginated (for backtests) ----

def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str,
                          since_ms: int, end_ms: int,
                          max_errors: int = 5) -> pd.DataFrame:
    """Fetch OHLCV in chunks. Aborts after `max_errors` consecutive failures.

    Hyperliquid: paginate by explicit time step (since + chunk_window) because
    the API caps the time window per call regardless of `limit` parameter.
    Other exchanges: rely on last returned candle timestamp + 1.
    """
    all_bars = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"

    if is_hl:
        # HL: use 5000-candle chunks but advance by chunk window in milliseconds
        chunk_limit = 5000
        tf_ms = TIMEFRAME_MS.get(timeframe, 60 * 60 * 1000)
        chunk_window_ms = chunk_limit * tf_ms
    else:
        chunk_limit = 1000
        chunk_window_ms = None  # we use last-bar advance for non-HL

    cursor_ms = since_ms
    while cursor_ms < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if is_hl:
                # Always advance by chunk window, even if the API returned fewer bars
                # than requested (because the API may be capping the time window).
                if bars:
                    all_bars.extend(bars)
                    last_ts = bars[-1][0]
                    # Advance to either last_ts + 1ms OR cursor + chunk_window, whichever is later
                    next_cursor = max(last_ts + tf_ms, cursor_ms + chunk_window_ms)
                else:
                    # No bars returned: jump forward by full window to avoid infinite loop
                    next_cursor = cursor_ms + chunk_window_ms
                if next_cursor <= cursor_ms:
                    break
                cursor_ms = next_cursor
            else:
                if not bars:
                    break
                all_bars.extend(bars)
                cursor_ms = bars[-1][0] + 1

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

    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]


def fetch_funding_paginated(exchange, symbol: str,
                            since_ms: int, end_ms: int,
                            max_errors: int = 5) -> pd.DataFrame:
    """Fetch funding rate history in chunks."""
    all_rates = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"

    if is_hl:
        # HL: 500 max per call, funding hourly = 500h window per chunk
        chunk_limit = 500
        chunk_window_ms = chunk_limit * 60 * 60 * 1000  # 500 hours in ms
    else:
        chunk_limit = 1000
        chunk_window_ms = None

    cursor_ms = since_ms
    while cursor_ms < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if is_hl:
                if rates:
                    all_rates.extend(rates)
                    last_ts = rates[-1]["timestamp"]
                    next_cursor = max(last_ts + 60 * 60 * 1000, cursor_ms + chunk_window_ms)
                else:
                    next_cursor = cursor_ms + chunk_window_ms
                if next_cursor <= cursor_ms:
                    break
                cursor_ms = next_cursor
            else:
                if not rates:
                    break
                all_rates.extend(rates)
                new_cursor = rates[-1]["timestamp"] + 1
                if new_cursor <= cursor_ms:
                    break
                cursor_ms = new_cursor

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

    if not all_rates:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
         "funding_rate": r["fundingRate"]}
        for r in all_rates
    ])
    return df.drop_duplicates("ts").set_index("ts").sort_index()
