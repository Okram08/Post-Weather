"""Exchange OHLCV + funding rate fetcher.

Supports: binance, bybit, okx, hyperliquid.

v4: detailed timestamp diagnostic per fetch call to understand HL behavior.
"""
import time

import ccxt
import pandas as pd


_DATA_PY_VERSION = "v4-hl-timestamp-diag-20260505"


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


def _ts_str(ms):
    return str(pd.Timestamp(ms, unit='ms', tz='UTC'))


def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str,
                          since_ms: int, end_ms: int,
                          max_errors: int = 5) -> pd.DataFrame:
    """Fetch OHLCV in chunks, with detailed per-call timestamp logging."""
    print("  [data.py " + _DATA_PY_VERSION + "] fetch_ohlcv_paginated: "
          + symbol + " " + timeframe
          + " requested from=" + _ts_str(since_ms) + " to=" + _ts_str(end_ms))

    all_bars = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"
    tf_ms = TIMEFRAME_MS.get(timeframe, 60 * 60 * 1000)

    chunk_limit = 5000 if is_hl else 1000
    skip_window_ms = 30 * 24 * 60 * 60 * 1000
    consecutive_empty = 0
    max_consecutive_empty = 6

    cursor_ms = since_ms
    iteration = 0
    while cursor_ms < end_ms:
        iteration += 1
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if bars and len(bars) > 0:
                consecutive_empty = 0
                # DIAGNOSTIC: log first and last bar timestamp returned
                first_ts = bars[0][0]
                last_ts = bars[-1][0]
                print("    iter " + str(iteration)
                      + " cursor_req=" + _ts_str(cursor_ms)
                      + " got " + str(len(bars)) + " bars"
                      + " range=[" + _ts_str(first_ts) + " ... " + _ts_str(last_ts) + "]")

                # If the API ignored our `since` and returned bars from far in the future
                # (e.g. when since is before listing), we must not loop forever.
                if first_ts > end_ms:
                    print("    -> all returned bars are AFTER end_ms; stopping")
                    break

                all_bars.extend(bars)
                new_cursor = last_ts + tf_ms
                if new_cursor <= cursor_ms:
                    new_cursor = cursor_ms + tf_ms * chunk_limit
                cursor_ms = new_cursor
            else:
                consecutive_empty += 1
                print("    iter " + str(iteration) + " cursor_req=" + _ts_str(cursor_ms)
                      + " EMPTY (consecutive=" + str(consecutive_empty) + ")")
                if not is_hl:
                    break
                if consecutive_empty >= max_consecutive_empty:
                    break
                cursor_ms += skip_window_ms

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

    print("    [done] total bars fetched (raw): " + str(len(all_bars)))

    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()

    print("    [done] after dedup: " + str(len(df)) + " bars,"
          + " range=[" + str(df.index.min()) + " ... " + str(df.index.max()) + "]")

    df_clipped = df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    print("    [done] after clip <= end_ms: " + str(len(df_clipped)) + " bars")

    return df_clipped


def fetch_funding_paginated(exchange, symbol: str,
                            since_ms: int, end_ms: int,
                            max_errors: int = 5) -> pd.DataFrame:
    print("  [data.py " + _DATA_PY_VERSION + "] fetch_funding_paginated: " + symbol)

    all_rates = []
    errors = 0
    is_hl = exchange.id == "hyperliquid"
    funding_interval_ms = 60 * 60 * 1000 if is_hl else 8 * 60 * 60 * 1000
    chunk_limit = 500 if is_hl else 1000
    skip_window_ms = 30 * 24 * 60 * 60 * 1000
    consecutive_empty = 0
    max_consecutive_empty = 6

    cursor_ms = since_ms
    iteration = 0
    while cursor_ms < end_ms:
        iteration += 1
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if rates and len(rates) > 0:
                consecutive_empty = 0
                first_ts = rates[0]["timestamp"]
                last_ts = rates[-1]["timestamp"]
                if iteration <= 3 or iteration % 10 == 0:
                    print("    funding iter " + str(iteration)
                          + " cursor_req=" + _ts_str(cursor_ms)
                          + " got " + str(len(rates)) + " rates"
                          + " range=[" + _ts_str(first_ts) + " ... " + _ts_str(last_ts) + "]")

                if first_ts > end_ms:
                    break

                all_rates.extend(rates)
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
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    print("    [done] funding after dedup: " + str(len(df))
          + " range=[" + str(df.index.min()) + " ... " + str(df.index.max()) + "]")
    return df
