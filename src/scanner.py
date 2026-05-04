"""Live scanner — runs on cron (every 4h) via GitHub Actions.

Flow:
  1. Load config + Gist state (bankroll + idempotency set).
  2. If halted → notify and exit.
  3. For each pair:
       a. Fetch last ~300 bars (1h) + current funding.
       b. Resample to 4h, compute indicators.
       c. Use the LAST FULLY CLOSED 4h bar as input to setup detection.
       d. Detect setup A v2 (long/short).
       e. If signal:
            - Skip if already emitted (idempotency).
            - Compute sizing through all bankroll gates.
            - Push Telegram (signal or rejected).
            - Append to audit log.
  4. Persist state to Gist.
"""
import os
import sys
import traceback

import pandas as pd

from src.config import load_config
from src.data import (
    make_exchange, fetch_ohlcv_recent, fetch_current_funding,
)
from src.indicators import resample_to_4h, compute_indicators
from src.setups import detect_setup_a
from src.bankroll import compute_size
from src.state import GistState, load_bankroll, save_bankroll
from src.telegram_bot import (
    send_message, format_signal, format_rejected,
    format_halt, format_error,
)


def _last_closed_4h_bar(ohlc_4h: pd.DataFrame) -> pd.Series:
    """Return the last bar that has fully closed (now > bar_start + 4h)."""
    now = pd.Timestamp.now(tz="UTC")
    closed = ohlc_4h[ohlc_4h.index + pd.Timedelta(hours=4) <= now]
    if closed.empty:
        return None
    return closed.iloc[-1]


def run() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(f"[fatal] config load: {e}")
        return 1

    if not cfg.gist.pat or not cfg.gist.state_gist_id:
        print("[fatal] GIST_PAT or GIST_STATE_ID missing")
        return 1

    gist = GistState(cfg.gist.pat, cfg.gist.state_gist_id)

    # Bankroll
    try:
        bankroll = load_bankroll(gist, {
            "initial_capital_usd": cfg.bankroll.initial_capital_usd
        })
    except Exception as e:
        print(f"[fatal] cannot load bankroll: {e}")
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         f"⚠️ Cannot load bankroll: `{e}`")
        return 1

    if bankroll.halted:
        msg = format_halt(bankroll.halt_reason)
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
        print(f"[halt] {bankroll.halt_reason}")
        return 0

    # Idempotency set (last 500 emitted signal IDs)
    emitted = set(gist.read("emitted_signals.json").get("ids", []))

    exchange = make_exchange(cfg.frictions.exchange)
    bankroll_params = {
        "risk_per_trade_pct": cfg.bankroll.risk_per_trade_pct,
        "max_concurrent_positions": cfg.bankroll.max_concurrent_positions,
        "max_leverage": cfg.bankroll.max_leverage,
        "daily_loss_limit_pct": cfg.bankroll.daily_loss_limit_pct,
        "max_drawdown_pct": cfg.bankroll.max_drawdown_pct,
        "sizing_model": cfg.bankroll.sizing_model,
        "kelly_fraction": cfg.bankroll.kelly_fraction,
    }

    n_emitted = 0
    n_rejected = 0

    for pair in cfg.strategy.pairs:
        try:
            ohlc_1h = fetch_ohlcv_recent(exchange, pair, "1h", n_bars=300)
            if ohlc_1h.empty or len(ohlc_1h) < 100:
                print(f"[{pair}] insufficient data")
                continue

            ohlc_4h = compute_indicators(resample_to_4h(ohlc_1h))
            if len(ohlc_4h) < 30:
                print(f"[{pair}] not enough 4h history yet")
                continue

            funding = fetch_current_funding(exchange, pair)
            ohlc_4h = ohlc_4h.copy()
            ohlc_4h["funding_rate"] = funding

            last_bar = _last_closed_4h_bar(ohlc_4h)
            if last_bar is None:
                print(f"[{pair}] no closed bar yet")
                continue

            current_price = float(ohlc_1h["close"].iloc[-1])

            signal = detect_setup_a(last_bar, pair, current_price,
                                    cfg.strategy.setup_a)
            if signal is None:
                print(f"[{pair}] no setup")
                continue

            if signal.signal_id in emitted:
                print(f"[{pair}] duplicate {signal.signal_id}, skip")
                continue

            sizing = compute_size(bankroll, signal, bankroll_params)

            if sizing.accept:
                msg = format_signal(signal, sizing, bankroll)
                if cfg.telegram.enabled:
                    send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
                print(f"[{pair}] EMITTED {signal.signal_id} ({signal.direction})")
                n_emitted += 1
                emitted.add(signal.signal_id)
                gist.append_log("signals_log.json", {
                    "id": signal.signal_id,
                    "pair": pair,
                    "direction": signal.direction,
                    "limit": signal.limit_price,
                    "stop": signal.stop_price,
                    "target": signal.target_price,
                    "notional_usd": sizing.notional_usd,
                    "leverage": sizing.leverage_implied,
                    "risk_usd": sizing.risk_amount_usd,
                    "current_price": signal.current_price,
                    "rsi": signal.rsi,
                    "adx": signal.adx,
                    "funding_rate": signal.funding_rate,
                    "extension_atr": signal.extension_atr,
                    "status": "emitted",
                })
            else:
                msg = format_rejected(signal, sizing)
                if cfg.telegram.enabled:
                    send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
                print(f"[{pair}] REJECTED {signal.signal_id}: {sizing.reason}")
                n_rejected += 1
                # Add to emitted set to avoid spamming the same rejection
                emitted.add(signal.signal_id)
                gist.append_log("signals_log.json", {
                    "id": signal.signal_id,
                    "pair": pair,
                    "direction": signal.direction,
                    "status": "rejected",
                    "reason": sizing.reason,
                })

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[{pair}] error: {err}")
            traceback.print_exc()
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                             format_error(pair, err))

    # Persist
    try:
        gist.write("emitted_signals.json", {"ids": list(emitted)[-500:]})
        save_bankroll(gist, bankroll)
    except Exception as e:
        print(f"[fatal] cannot persist state: {e}")
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         f"⚠️ Cannot persist state: `{e}`")
        return 1

    print(f"\nDone. Emitted: {n_emitted}, Rejected: {n_rejected}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
