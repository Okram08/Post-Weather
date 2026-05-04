"""Backtest engine for Setup A v2 — runs on GitHub Actions (workflow_dispatch).

Outputs to ./backtest_results/:
  - trades_<pair>.csv per pair
  - trades_all.csv combined portfolio
  - summary.txt with metrics
  - equity_curve.png

Models real frictions:
  - Maker fees on limit fills + target exits
  - Taker fees on stop and time exits
  - Funding payments every 8h while in position
  - Stop-first conservative assumption when both touched in same bar
  - Risk-based sizing with leverage cap
"""
import argparse
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.config import load_config
from src.data import (
    make_exchange, fetch_ohlcv_paginated, fetch_funding_paginated,
)
from src.indicators import resample_to_4h, compute_indicators


TRADE_COLUMNS = [
    "pair", "direction", "setup_time", "fill_time", "exit_time",
    "exit_reason", "limit", "stop", "target", "exit_price",
    "pnl_usd", "fees", "funding", "size_usd",
]


@dataclass
class Trade:
    pair: str
    direction: str
    setup_time: pd.Timestamp
    limit_price: float
    stop_price: float
    target_price: float
    fill_time: Optional[pd.Timestamp] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_usd: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    position_size_usd: float = 0.0


def _attach_funding_4h(ohlc_4h: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    if funding.empty:
        ohlc_4h["funding_rate"] = 0.0
        return ohlc_4h
    ohlc_4h["funding_rate"] = funding["funding_rate"].reindex(
        ohlc_4h.index, method="ffill"
    ).fillna(0.0)
    return ohlc_4h


def _detect_at(row: pd.Series, p: dict) -> Optional[str]:
    if pd.isna(row["atr"]) or pd.isna(row["vwap_24h"]) or pd.isna(row["adx"]):
        return None
    if row["adx"] >= p["adx_max"]:
        return None
    if (row["extension_atr"] < -p["setup_atr_extension"]
            and row["rsi"] < p["rsi_threshold"]
            and row["funding_rate"] < p["funding_threshold"]):
        return "long"
    if (row["extension_atr"] > p["setup_atr_extension"]
            and row["rsi"] > 100 - p["rsi_threshold"]
            and row["funding_rate"] > -p["funding_threshold"]):
        return "short"
    return None


def _simulate_trade(setup_ts, direction: str, row_4h: pd.Series,
                    ohlc_1h: pd.DataFrame, funding: pd.DataFrame,
                    p: dict, fees: dict, sizing: dict, pair: str) -> Trade:
    vwap, atr = row_4h["vwap_24h"], row_4h["atr"]
    if direction == "long":
        limit = vwap - p["limit_extension_atr"] * atr
        stop = limit - p["stop_atr"] * atr
        target = limit * (1 + p["target_pct"])
    else:
        limit = vwap + p["limit_extension_atr"] * atr
        stop = limit + p["stop_atr"] * atr
        target = limit * (1 - p["target_pct"])

    t = Trade(pair=pair, direction=direction, setup_time=setup_ts,
              limit_price=limit, stop_price=stop, target_price=target)

    order_t = setup_ts + pd.Timedelta(hours=4)
    valid_end = order_t + pd.Timedelta(hours=p["limit_validity_hours"])

    fill_window = ohlc_1h.loc[order_t:valid_end]
    fill_idx = None
    for ts, bar in fill_window.iterrows():
        if direction == "long" and bar["low"] <= limit:
            fill_idx = ts
            break
        if direction == "short" and bar["high"] >= limit:
            fill_idx = ts
            break

    if fill_idx is None:
        t.exit_reason = "unfilled"
        return t
    t.fill_time = fill_idx

    max_exit = fill_idx + pd.Timedelta(hours=p["position_max_hours"])
    post = ohlc_1h.loc[fill_idx:max_exit]

    for ts, bar in post.iloc[1:].iterrows():
        if direction == "long":
            if bar["low"] <= stop:
                t.exit_time, t.exit_price, t.exit_reason = ts, stop, "stop"; break
            if bar["high"] >= target:
                t.exit_time, t.exit_price, t.exit_reason = ts, target, "target"; break
        else:
            if bar["high"] >= stop:
                t.exit_time, t.exit_price, t.exit_reason = ts, stop, "stop"; break
            if bar["low"] <= target:
                t.exit_time, t.exit_price, t.exit_reason = ts, target, "target"; break
    if t.exit_time is None:
        t.exit_time = post.index[-1]
        t.exit_price = post["close"].iloc[-1]
        t.exit_reason = "time"

    risk_dist_pct = abs(limit - stop) / limit
    notional = sizing["initial_capital"] * sizing["risk_per_trade"] / max(risk_dist_pct, 1e-6)
    notional = min(notional, sizing["initial_capital"] * sizing["max_leverage"])
    t.position_size_usd = notional

    if direction == "long":
        gross_pct = (t.exit_price - limit) / limit
    else:
        gross_pct = (limit - t.exit_price) / limit

    entry_fee = notional * fees["maker_bps"] / 10_000
    exit_bps = fees["maker_bps"] if t.exit_reason == "target" else fees["taker_bps"]
    exit_fee = notional * exit_bps / 10_000
    t.fees_paid = entry_fee + exit_fee

    funding_pnl = 0.0
    if not funding.empty:
        in_win = funding.loc[t.fill_time:t.exit_time]
        sign = -1 if direction == "long" else 1
        for _, rate in in_win["funding_rate"].items():
            funding_pnl += sign * rate * notional
    t.funding_paid = -funding_pnl

    t.pnl_usd = gross_pct * notional - t.fees_paid + funding_pnl
    return t


def _backtest_pair(pair: str, ohlc_1h: pd.DataFrame, funding: pd.DataFrame,
                   p: dict, fees: dict, sizing: dict) -> List[Trade]:
    ohlc_4h = compute_indicators(resample_to_4h(ohlc_1h))
    ohlc_4h = _attach_funding_4h(ohlc_4h, funding)

    trades = []
    blocked_until = pd.Timestamp("1970-01-01", tz="UTC")
    for ts, row in ohlc_4h.iterrows():
        if ts < blocked_until:
            continue
        d = _detect_at(row, p)
        if d is None:
            continue
        t = _simulate_trade(ts, d, row, ohlc_1h, funding, p, fees, sizing, pair)
        trades.append(t)
        if t.exit_time is not None:
            blocked_until = t.exit_time
        else:
            blocked_until = ts + pd.Timedelta(hours=4 + p["limit_validity_hours"])
    return trades


def _trades_df(trades: List[Trade]) -> pd.DataFrame:
    """Returns a DataFrame with TRADE_COLUMNS, even if trades is empty."""
    if not trades:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    return pd.DataFrame([{
        "pair": t.pair, "direction": t.direction,
        "setup_time": t.setup_time, "fill_time": t.fill_time,
        "exit_time": t.exit_time, "exit_reason": t.exit_reason,
        "limit": t.limit_price, "stop": t.stop_price, "target": t.target_price,
        "exit_price": t.exit_price,
        "pnl_usd": t.pnl_usd,
        "fees": t.fees_paid, "funding": t.funding_paid,
        "size_usd": t.position_size_usd,
    } for t in trades])


def _metrics(df: pd.DataFrame, initial_capital: float) -> dict:
    if df.empty:
        return {"total_signals": 0, "filled": 0, "fill_rate": 0.0}
    filled = df[df["exit_reason"] != "unfilled"].copy()
    out = {
        "total_signals": len(df),
        "filled": len(filled),
        "fill_rate": len(filled) / len(df) if len(df) > 0 else 0.0,
    }
    if filled.empty:
        return out
    filled = filled.sort_values("exit_time").reset_index(drop=True)
    filled["equity"] = initial_capital + filled["pnl_usd"].cumsum()
    wins = filled[filled["pnl_usd"] > 0]
    losses = filled[filled["pnl_usd"] <= 0]
    eq = filled.set_index("exit_time")["equity"].resample("1D").last().ffill()
    daily = eq.pct_change().dropna()
    sharpe = (daily.mean() / daily.std()) * np.sqrt(365) if daily.std() > 0 else 0
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    hold = (filled["exit_time"] - filled["fill_time"]).dt.total_seconds() / 3600
    by_reason = filled["exit_reason"].value_counts(normalize=True).to_dict()
    out.update({
        "win_rate": len(wins) / len(filled),
        "avg_win_usd": float(wins["pnl_usd"].mean()) if not wins.empty else 0.0,
        "avg_loss_usd": float(losses["pnl_usd"].mean()) if not losses.empty else 0.0,
        "expectancy_usd": float(filled["pnl_usd"].mean()),
        "total_pnl_usd": float(filled["pnl_usd"].sum()),
        "total_return_pct": float(filled["pnl_usd"].sum() / initial_capital),
        "sharpe_annualized": float(sharpe),
        "max_drawdown_pct": float(dd),
        "avg_hold_h": float(hold.mean()),
        "median_hold_h": float(hold.median()),
        "fees_total_usd": float(filled["fees"].sum()),
        "funding_pnl_usd": float(-filled["funding"].sum()),
        "exit_target_pct": by_reason.get("target", 0.0),
        "exit_stop_pct": by_reason.get("stop", 0.0),
        "exit_time_pct": by_reason.get("time", 0.0),
    })
    return out


def _plot_equity(df: pd.DataFrame, initial_capital: float, out_path: Path):
    if df.empty:
        return
    filled = df[df["exit_reason"] != "unfilled"].copy().sort_values("exit_time")
    if filled.empty:
        return
    filled["equity"] = initial_capital + filled["pnl_usd"].cumsum()
    plt.figure(figsize=(12, 6))
    plt.plot(filled["exit_time"], filled["equity"])
    plt.axhline(initial_capital, color="grey", linestyle=":", linewidth=0.8)
    plt.title("Setup A v2 — Backtest equity curve")
    plt.xlabel("Date"); plt.ylabel("Equity (USD)")
    plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(out_path, dpi=100); plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--pairs", default=None,
                        help="comma-separated, defaults to config.yaml pairs")
    parser.add_argument("--setup_atr", type=float, default=None)
    parser.add_argument("--limit_atr", type=float, default=None)
    parser.add_argument("--stop_atr", type=float, default=None)
    parser.add_argument("--target_pct", type=float, default=None)
    parser.add_argument("--cache_dir", default="cache")
    parser.add_argument("--output_dir", default="backtest_results")
    args = parser.parse_args()

    cfg = load_config()
    p = dict(cfg.strategy.setup_a)
    if args.setup_atr is not None: p["setup_atr_extension"] = args.setup_atr
    if args.limit_atr is not None: p["limit_extension_atr"] = args.limit_atr
    if args.stop_atr is not None: p["stop_atr"] = args.stop_atr
    if args.target_pct is not None: p["target_pct"] = args.target_pct

    pairs = (args.pairs.split(",") if args.pairs else cfg.strategy.pairs)
    pairs = [s.strip() for s in pairs]

    fees = {
        "maker_bps": cfg.frictions.maker_fee_bps,
        "taker_bps": cfg.frictions.taker_fee_bps,
    }
    sizing = {
        "initial_capital": cfg.bankroll.initial_capital_usd,
        "risk_per_trade": cfg.bankroll.risk_per_trade_pct,
        "max_leverage": cfg.bankroll.max_leverage,
    }

    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir); cache.mkdir(parents=True, exist_ok=True)

    print(f"\nExchange: {cfg.frictions.exchange}")
    print(f"Pairs:    {pairs}")
    print(f"Period:   {args.start} → {args.end}")
    print(f"Params:   {p}\n")

    exchange = make_exchange(cfg.frictions.exchange)
    since_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)

    all_trades = []
    for pair in pairs:
        print(f"=== {pair} ===")
        cache_path = cache / f"{pair.replace('/','_').replace(':','_')}_{args.start}_{args.end}.pkl"
        if cache_path.exists():
            print(f"  loading cache: {cache_path.name}")
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
        else:
            print("  fetching ohlcv 1h...")
            ohlc_1h = fetch_ohlcv_paginated(exchange, pair, "1h", since_ms, end_ms)
            print("  fetching funding...")
            funding = fetch_funding_paginated(exchange, pair, since_ms, end_ms)
            data = {"ohlc_1h": ohlc_1h, "funding": funding}
            with open(cache_path, "wb") as f:
                pickle.dump(data, f)

        n_bars = len(data["ohlc_1h"])
        n_funding = len(data["funding"])
        print(f"  → bars 1h: {n_bars}, funding rates: {n_funding}")

        if n_bars == 0:
            print(f"  ⚠️  no ohlcv data fetched, skipping")
            continue
        if n_funding == 0:
            print(f"  ⚠️  no funding data fetched — Setup A funding condition will never trigger")

        trades = _backtest_pair(pair, data["ohlc_1h"], data["funding"], p, fees, sizing)
        n_signals = len(trades)
        n_filled = sum(1 for t in trades if t.exit_reason != "unfilled")
        print(f"  → signals detected: {n_signals}, filled: {n_filled}")

        df = _trades_df(trades)
        df.to_csv(out / f"trades_{pair.replace('/','_').replace(':','_')}.csv", index=False)
        m = _metrics(df, sizing["initial_capital"])
        if m.get("filled", 0) > 0:
            print(f"  win_rate={m['win_rate']:.1%}, "
                  f"PnL ${m['total_pnl_usd']:.0f} ({m['total_return_pct']:.1%}), "
                  f"Sharpe {m['sharpe_annualized']:.2f}, DD {m['max_drawdown_pct']:.1%}")
        all_trades.extend(trades)
        print()

    portfolio = _trades_df(all_trades)
    if not portfolio.empty:
        portfolio = portfolio.sort_values("setup_time").reset_index(drop=True)
    portfolio.to_csv(out / "trades_all.csv", index=False)
    metrics = _metrics(portfolio, sizing["initial_capital"])

    summary_lines = ["PORTFOLIO METRICS", "=" * 40,
                     f"exchange : {cfg.frictions.exchange}",
                     f"period   : {args.start} → {args.end}",
                     f"pairs    : {', '.join(pairs)}",
                     f"params   : {p}", ""]
    if metrics.get("total_signals", 0) == 0:
        summary_lines.append("⚠️  Aucun signal détecté sur la période.")
        summary_lines.append("Causes probables :")
        summary_lines.append("  - funding rates non fetchés (vérifier nb funding par paire ci-dessus)")
        summary_lines.append("  - paramètres trop stricts (extension/rsi/funding_threshold)")
        summary_lines.append("  - période sans condition de marché matching")
    else:
        for k, v in metrics.items():
            if isinstance(v, float):
                summary_lines.append(f"{k:30s}: {v:.4f}")
            else:
                summary_lines.append(f"{k:30s}: {v}")

    text = "\n".join(summary_lines)
    print("\n" + text)
    (out / "summary.txt").write_text(text)
    _plot_equity(portfolio, sizing["initial_capital"], out / "equity_curve.png")
    print(f"\n→ results in {out}/")


if __name__ == "__main__":
    main()
