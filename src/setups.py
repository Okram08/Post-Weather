"""Setup A v2 detection — symmetric long/short on funding × extension × RSI."""
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    pair: str
    direction: str           # 'long' or 'short'
    timestamp: pd.Timestamp  # bar timestamp (start of 4h bar)
    current_price: float

    # Bar context (for audit)
    vwap_24h: float
    atr: float
    rsi: float
    adx: float
    funding_rate: float
    extension_atr: float

    # Computed orders
    limit_price: float
    stop_price: float
    target_price: float

    @property
    def signal_id(self) -> str:
        clean = self.pair.replace("/", "").replace(":", "")
        return f"{clean}_{self.direction}_{int(self.timestamp.timestamp())}"

    @property
    def risk_distance_pct(self) -> float:
        return abs(self.limit_price - self.stop_price) / self.limit_price


def detect_setup_a(row: pd.Series, pair: str,
                   current_price: float, params: dict) -> Optional[Signal]:
    """Detect Setup A v2 on a 4h bar (must be a fully closed bar).

    params: dict with keys
      setup_atr_extension, rsi_threshold, adx_max, funding_threshold,
      limit_extension_atr, stop_atr, target_pct
    """
    if pd.isna(row.get("atr")) or pd.isna(row.get("vwap_24h")) or pd.isna(row.get("adx")):
        return None
    if row["adx"] >= params["adx_max"]:
        return None

    direction = None
    if (row["extension_atr"] < -params["setup_atr_extension"]
            and row["rsi"] < params["rsi_threshold"]
            and row["funding_rate"] < params["funding_threshold"]):
        direction = "long"
    elif (row["extension_atr"] > params["setup_atr_extension"]
          and row["rsi"] > 100 - params["rsi_threshold"]
          and row["funding_rate"] > -params["funding_threshold"]):
        direction = "short"

    if direction is None:
        return None

    vwap, atr = row["vwap_24h"], row["atr"]
    if direction == "long":
        limit = vwap - params["limit_extension_atr"] * atr
        stop = limit - params["stop_atr"] * atr
        target = limit * (1 + params["target_pct"])
    else:
        limit = vwap + params["limit_extension_atr"] * atr
        stop = limit + params["stop_atr"] * atr
        target = limit * (1 - params["target_pct"])

    return Signal(
        pair=pair, direction=direction, timestamp=row.name,
        current_price=current_price,
        vwap_24h=float(vwap), atr=float(atr),
        rsi=float(row["rsi"]), adx=float(row["adx"]),
        funding_rate=float(row["funding_rate"]),
        extension_atr=float(row["extension_atr"]),
        limit_price=float(limit), stop_price=float(stop), target_price=float(target),
    )
