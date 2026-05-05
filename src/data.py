"""Exchange OHLCV + funding rate fetcher (live + historical).

Supports: binance, bybit, okx, hyperliquid.
- binance/bybit/okx: USDT-margined perpetual swaps (BTC/USDT:USDT)
- hyperliquid: USDC-margined perpetual swaps (BTC/USDC:USDC)
                funding paid hourly (vs 8h elsewhere)
"""
import time

import ccxt
import pandas as pd


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
            # Hyperliquid: take the most recent funding rate entry
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
    """Fetch OHLCV in chunks. Aborts after `max_errors` consecutive failures."""
    all_bars = []
    errors = 0
    # Hyperliquid: limit=5000 max, others: 1000 is safe
    chunk_limit = 5000 if exchange.id == "hyperliquid" else 1000
    while since_ms < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=chunk_limit)
            errors = 0
            if not bars:
                break
            all_bars.extend(bars)
            since_ms = bars[-1][0] + 1
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
    # Hyperliquid: 500 max per call (per HL API spec)
    chunk_limit = 500 if exchange.id == "hyperliquid" else 1000
    while since_ms < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=chunk_limit)
            errors = 0
            if not rates:
                break
            all_rates.extend(rates)
            new_since = rates[-1]["timestamp"] + 1
            if new_since <= since_ms:
                # Defensive: if API returns same timestamp, force advance to avoid infinite loop
                break
            since_ms = new_since
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
