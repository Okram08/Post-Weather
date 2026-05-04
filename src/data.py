"""Exchange OHLCV + funding rate fetcher (live + historical)."""
import time

import ccxt
import pandas as pd


def make_exchange(name: str = "binance"):
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
    raise ValueError(f"Unsupported exchange: {name}")


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
        rate = exchange.fetch_funding_rate(symbol)
        return float(rate.get("fundingRate", 0.0) or 0.0)
    except Exception as e:
        print(f"  [funding] error for {symbol}: {e}")
        return 0.0


# ---- Historical paginated (for backtests) ----

def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str,
                          since_ms: int, end_ms: int) -> pd.DataFrame:
    all_bars = []
    while since_ms < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
            if not bars:
                break
            all_bars.extend(bars)
            since_ms = bars[-1][0] + 1
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"  retry ohlcv: {e}")
            time.sleep(2)
    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]


def fetch_funding_paginated(exchange, symbol: str,
                            since_ms: int, end_ms: int) -> pd.DataFrame:
    all_rates = []
    while since_ms < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=1000)
            if not rates:
                break
            all_rates.extend(rates)
            since_ms = rates[-1]["timestamp"] + 1
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"  retry funding: {e}")
            time.sleep(2)
    if not all_rates:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
         "funding_rate": r["fundingRate"]}
        for r in all_rates
    ])
    return df.drop_duplicates("ts").set_index("ts").sort_index()
