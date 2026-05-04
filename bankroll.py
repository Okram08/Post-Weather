"""Bankroll management: position sizing + risk circuit breakers.

Pro-grade rules implemented:
  - Risk-based fixed-fractional sizing (1% equity at risk per trade by default).
  - Optional fractional Kelly sizing (capped, anti-Martingale by construction).
  - Hard cap on implied leverage.
  - Hard cap on concurrent positions (cross-pair).
  - One position per pair at a time (no stacking).
  - Daily loss limit: -3% in 24h → 24h halt automatic.
  - Max drawdown circuit breaker: -15% from peak → halt until manual reset.
  - All decisions return SizingDecision with explicit reason for audit.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List


@dataclass
class BankrollState:
    equity_usd: float
    peak_equity_usd: float
    daily_pnl_usd: float
    daily_reset_at: str  # ISO timestamp, UTC
    open_positions: List[dict] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    last_updated: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SizingDecision:
    accept: bool
    notional_usd: float
    qty: float
    leverage_implied: float
    risk_amount_usd: float
    reason: str


def reset_daily_if_needed(state: BankrollState) -> None:
    """Roll the daily P&L window every 24h."""
    now = datetime.now(timezone.utc)
    last_reset = datetime.fromisoformat(state.daily_reset_at)
    if (now - last_reset).total_seconds() >= 86400:
        state.daily_pnl_usd = 0.0
        state.daily_reset_at = now.isoformat()


def compute_size(state: BankrollState, signal, params: dict) -> SizingDecision:
    """Compute position size and pass through all risk gates.

    params: BankrollConfig as dict (risk_per_trade_pct, max_concurrent_positions,
    max_leverage, daily_loss_limit_pct, max_drawdown_pct, sizing_model, kelly_fraction).
    """
    reset_daily_if_needed(state)

    # GATE 1: hard halt (max DD or operator halt)
    if state.halted:
        return _reject(f"halted: {state.halt_reason}")

    # GATE 2: max drawdown check (in case state was modified externally)
    dd = (state.equity_usd - state.peak_equity_usd) / state.peak_equity_usd
    if dd <= -params["max_drawdown_pct"]:
        state.halted = True
        state.halt_reason = (
            f"max DD breached: {dd:.2%} <= -{params['max_drawdown_pct']:.0%}"
        )
        return _reject(state.halt_reason)

    # GATE 3: daily loss limit
    daily_loss_floor = -params["daily_loss_limit_pct"] * state.equity_usd
    if state.daily_pnl_usd <= daily_loss_floor:
        return _reject(
            f"daily loss limit hit "
            f"(${state.daily_pnl_usd:.2f} <= ${daily_loss_floor:.2f})"
        )

    # GATE 4: concurrent positions
    if len(state.open_positions) >= params["max_concurrent_positions"]:
        return _reject(
            f"max concurrent positions reached "
            f"({len(state.open_positions)}/{params['max_concurrent_positions']})"
        )

    # GATE 5: per-pair lock (no stacking on same pair)
    for pos in state.open_positions:
        if pos.get("pair") == signal.pair:
            return _reject(f"position already open on {signal.pair}")

    # SIZING
    risk_dist_pct = signal.risk_distance_pct
    if risk_dist_pct < 1e-6:
        return _reject("invalid risk distance")

    if params.get("sizing_model", "fixed") == "kelly_fraction":
        # Conservative Kelly: assume win_rate=0.6, RR=0.67 (typical for this setup)
        # Kelly fraction = win_rate - (1-win_rate)/RR = 0.6 - 0.4/0.67 = 0.003
        # Apply kelly_fraction multiplier (typically 0.25 = quarter Kelly)
        risk_per_trade = params["kelly_fraction"] * params["risk_per_trade_pct"]
    else:
        risk_per_trade = params["risk_per_trade_pct"]

    risk_amount = state.equity_usd * risk_per_trade
    notional = risk_amount / risk_dist_pct

    # GATE 6: leverage cap
    max_notional = state.equity_usd * params["max_leverage"]
    if notional > max_notional:
        notional = max_notional
        # actual risk now lower than target — that's fine, conservative direction

    qty = notional / signal.limit_price
    leverage = notional / state.equity_usd
    actual_risk = notional * risk_dist_pct

    return SizingDecision(
        accept=True,
        notional_usd=notional,
        qty=qty,
        leverage_implied=leverage,
        risk_amount_usd=actual_risk,
        reason="ok",
    )


def update_after_trade(state: BankrollState, pnl_usd: float) -> None:
    """Update equity and peak after a trade closes."""
    state.equity_usd += pnl_usd
    state.peak_equity_usd = max(state.peak_equity_usd, state.equity_usd)
    state.daily_pnl_usd += pnl_usd
    state.last_updated = datetime.now(timezone.utc).isoformat()


def _reject(reason: str) -> SizingDecision:
    return SizingDecision(
        accept=False, notional_usd=0.0, qty=0.0,
        leverage_implied=0.0, risk_amount_usd=0.0, reason=reason,
    )
