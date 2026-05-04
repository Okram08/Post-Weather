"""Technical indicators: ATR, RSI, ADX, VWAP_24h."""
import numpy as np
import pandas as pd


def resample_to_4h(ohlc_1h: pd.DataFrame) -> pd.DataFrame:
    return ohlc_1h.resample("4h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def compute_indicators(ohlc_4h: pd.DataFrame) -> pd.DataFrame:
    df = ohlc_4h.copy()

    # ATR (Wilder, 14)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    # RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    # ADX (14)
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    # VWAP_24h (rolling 6 × 4h)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap_24h"] = (
        (typical * df["volume"]).rolling(6).sum() / df["volume"].rolling(6).sum()
    )
    df["extension_atr"] = (df["close"] - df["vwap_24h"]) / df["atr"]
    return df
