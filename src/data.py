"""Exchange OHLCV + funding rate fetcher.

Supports: binance, bybit, okx (via ccxt), hyperliquid (via direct REST API).

ccxt's Hyperliquid OHLCV implementation IGNORES the `since` parameter and
returns only the last ~5000 bars. We bypass ccxt for HL OHLCV and use the
official /info endpoint with candleSnapshot which respects startTime/endTime.

Funding history works correctly via ccxt for HL, so we keep that.
"""
import time

import ccxt
import pandas as pd
import requests


_DATA_PY_VERSION = "v5-hl-direct-api-20260505"


HL_API_URL = "https://api.hyperliquid.xyz/info"


# Map ccxt symbol "BTC/USDC:USDC" to HL coin name "BTC"
def _hl_coin(symbol: str) -> str:
    return symbol.split("/")[0]


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
    if exchange.id == "hyperliquid":
        # Use direct API for consistency with paginated version
        end_ms = int(time.time() * 1000)
        tf_ms = _tf_to_ms(timeframe)
        start_ms = end_ms - n_bars * tf_ms
        return _hl_fetch_ohlcv_direct(symbol, timeframe, start_ms, end_ms)
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


def _tf_to_ms(timeframe: str) -> int:
    mapping = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    return mapping.get(timeframe, 3_600_000)


def _ts_str(ms):
    return str(pd.Timestamp(ms, unit='ms', tz='UTC'))


def _hl_fetch_ohlcv_direct(symbol: str, timeframe: str,
                            start_ms: int, end_ms: int) -> pd.DataFrame:
    """Direct Hyperliquid /info candleSnapshot API.

    HL caps each call to 5000 candles; we paginate by sliding the start time.
    """
    coin = _hl_coin(symbol)
    tf_ms = _tf_to_ms(timeframe)
    chunk_candles = 5000
    chunk_window_ms = chunk_candles * tf_ms

    all_bars = []
    cursor = start_ms
    iteration = 0
    consecutive_empty = 0

    while cursor < end_ms:
        iteration += 1
        req_end = min(cursor + chunk_window_ms, end_ms)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": timeframe,
                "startTime": cursor,
                "endTime": req_end,
            },
        }
        try:
            resp = requests.post(HL_API_URL, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("  [HL ohlcv] error iter " + str(iteration) + ": " + str(e))
            time.sleep(2)
            continue

        if not data:
            consecutive_empty += 1
            if iteration <= 3 or iteration % 5 == 0:
                print("    HL iter " + str(iteration)
                      + " req=[" + _ts_str(cursor) + " ... " + _ts_str(req_end) + "]"
                      + " EMPTY (consecutive=" + str(consecutive_empty) + ")")
            if consecutive_empty >= 6:
                print("    -> 6 consecutive empty, stopping")
                break
            cursor = req_end
            continue

        consecutive_empty = 0
        for c in data:
            # HL candle format: {"t": startTime, "T": endTime, "s": coin, "i": interval,
            #                    "o": open, "h": high, "l": low, "c": close, "v": volume, "n": trades}
            all_bars.append([
                int(c["t"]),
                float(c["o"]),
                float(c["h"]),
                float(c["l"]),
                float(c["c"]),
                float(c["v"]),
            ])

        last_ts = int(data[-1]["t"])
        if iteration <= 3 or iteration % 5 == 0:
            print("    HL iter " + str(iteration)
                  + " req=[" + _ts_str(cursor) + " ... " + _ts_str(req_end) + "]"
                  + " got " + str(len(data)) + " bars,"
                  + " last=" + _ts_str(last_ts))

        new_cursor = last_ts + tf_ms
        if new_cursor <= cursor:
            new_cursor = req_end
        cursor = new_cursor
        time.sleep(0.1)

    print("    [done] HL direct: " + str(len(all_bars)) + " bars total")

    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    df_clipped = df[(df.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))
                    & (df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC"))]
    print("    [done] HL direct after dedup+clip: " + str(len(df_clipped)) + " bars,"
          + " range=[" + str(df_clipped.index.min()) + " ... " + str(df_clipped.index.max()) + "]")
    return df_clipped


def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str,
                          since_ms: int, end_ms: int,
                          max_errors: int = 5) -> pd.DataFrame:
    """Fetch OHLCV in chunks. For HL: uses direct REST API. Other: ccxt."""
    print("  [data.py " + _DATA_PY_VERSION + "] fetch_ohlcv_paginated: "
          + symbol + " " + timeframe
          + " from=" + _ts_str(since_ms) + " to=" + _ts_str(end_ms))

    if exchange.id == "hyperliquid":
        return _hl_fetch_ohlcv_direct(symbol, timeframe, since_ms, end_ms)

    # ccxt path for binance/bybit/okx
    all_bars = []
    errors = 0
    cursor_ms = since_ms
    while cursor_ms < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=1000)
            errors = 0
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
                    " errors. Last: " + str(e)
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
    """Fetch funding rate history. ccxt works fine for HL funding so we keep it."""
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
    while cursor_ms < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=cursor_ms, limit=chunk_limit)
            errors = 0

            if rates and len(rates) > 0:
                consecutive_empty = 0
                last_ts = rates[-1]["timestamp"]
                if rates[0]["timestamp"] > end_ms:
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
                    " errors. Last: " + str(e)
                )
            time.sleep(2)

    print("    [done] funding: " + str(len(all_rates)) + " rates")

    if not all_rates:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
         "funding_rate": r["fundingRate"]}
        for r in all_rates
    ])
    return df.drop_duplicates("ts").set_index("ts").sort_index()
