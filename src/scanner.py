"""Live scanner - runs on cron (every 4h) via GitHub Actions.

Flow:
  1. Load config + Gist state (bankroll + idempotency set).
  2. If halted -> notify and exit (with heartbeat).
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
       f. Always record pair status for the heartbeat.
  4. Persist state to Gist.
  5. Send a heartbeat Telegram message with full scan summary.
"""
import sys
import traceback
from datetime import datetime, timezone

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


def _last_closed_4h_bar(ohlc_4h: pd.DataFrame):
    """Return the last bar that has fully closed (now > bar_start + 4h)."""
    now = pd.Timestamp.now(tz="UTC")
    closed = ohlc_4h[ohlc_4h.index + pd.Timedelta(hours=4) <= now]
    if closed.empty:
        return None
    return closed.iloc[-1]


def _fmt_usd(x):
    try:
        return "${:.2f}".format(float(x))
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pct(x):
    try:
        return "{:+.2%}".format(float(x))
    except (TypeError, ValueError):
        return "n/a"


def _format_heartbeat(bankroll, inspected, emitted_count, rejected_count) -> str:
    """Build the always-sent end-of-scan summary message."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Try to extract bankroll fields - support both dict and object
    def get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    equity = get(bankroll, "equity_usd", None)
    peak = get(bankroll, "peak_equity_usd", None)
    halted = get(bankroll, "halted", False)
    halt_reason = get(bankroll, "halt_reason", "")
    open_positions = get(bankroll, "open_positions", []) or []

    dd_pct = None
    if equity is not None and peak and peak > 0:
        dd_pct = (float(equity) - float(peak)) / float(peak)

    lines = [
        "*Scan done* `" + now + "`",
        "",
        "*Bankroll:* " + _fmt_usd(equity)
            + " (peak " + _fmt_usd(peak) + ", DD " + _fmt_pct(dd_pct) + ")",
        "*Halted:* " + ("YES - " + str(halt_reason) if halted else "no"),
        "*Open positions:* " + str(len(open_positions)),
        "*Emitted:* " + str(emitted_count) + " | *Rejected:* " + str(rejected_count),
        "",
        "*Pairs:*",
    ]
    for d in inspected:
        tag = {
            "setup-long": "[LONG]",
            "setup-short": "[SHORT]",
            "no-setup": "[--]",
            "no-data": "[??]",
            "halted": "[HALT]",
            "error": "[!!]",
        }.get(d["status"], "[--]")
        lines.append("  " + tag + " `" + d["pair"] + "` " + d.get("detail", ""))

    return "\n".join(lines)


def _send_heartbeat(cfg, bankroll, inspected, emitted_count, rejected_count):
    if not cfg.telegram.enabled:
        return
    try:
        msg = _format_heartbeat(bankroll, inspected, emitted_count, rejected_count)
        send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
    except Exception as e:
        print("[heartbeat] failed: " + str(e))


def run() -> int:
    inspected = []
    n_emitted = 0
    n_rejected = 0

    try:
        cfg = load_config()
    except Exception as e:
        print("[fatal] config load: " + str(e))
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
        print("[fatal] cannot load bankroll: " + str(e))
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         "Cannot load bankroll: `" + str(e) + "`")
        return 1

    # Halted: still send a heartbeat so user knows
    if getattr(bankroll, "halted", False):
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         format_halt(bankroll.halt_reason))
        for pair in cfg.strategy.pairs:
            inspected.append({
                "pair": pair, "status": "halted",
                "detail": "scanner halted",
            })
        _send_heartbeat(cfg, bankroll, inspected, 0, 0)
        print("[halt] " + str(bankroll.halt_reason))
        return 0

    # Idempotency set
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

    for pair in cfg.strategy.pairs:
        try:
            ohlc_1h = fetch_ohlcv_recent(exchange, pair, "1h", n_bars=300)
            if ohlc_1h.empty or len(ohlc_1h) < 100:
                print("[" + pair + "] insufficient data")
                inspected.append({
                    "pair": pair, "status": "no-data",
                    "detail": "<100 1h bars",
                })
                continue

            ohlc_4h = compute_indicators(resample_to_4h(ohlc_1h))
            if len(ohlc_4h) < 30:
                print("[" + pair + "] not enough 4h history yet")
                inspected.append({
                    "pair": pair, "status": "no-data",
                    "detail": "<30 4h bars",
                })
                continue

            funding = fetch_current_funding(exchange, pair)
            ohlc_4h = ohlc_4h.copy()
            ohlc_4h["funding_rate"] = funding

            last_bar = _last_closed_4h_bar(ohlc_4h)
            if last_bar is None:
                print("[" + pair + "] no closed bar yet")
                inspected.append({
                    "pair": pair, "status": "no-data",
                    "detail": "no closed 4h bar",
                })
                continue

            current_price = float(ohlc_1h["close"].iloc[-1])

            signal = detect_setup_a(last_bar, pair, current_price,
                                    cfg.strategy.setup_a)

            # Build a diagnostic detail string for the heartbeat regardless
            ext = float(last_bar.get("extension_atr", float("nan")))
            rsi = float(last_bar.get("rsi", float("nan")))
            adx = float(last_bar.get("adx", float("nan")))
            fund = float(last_bar.get("funding_rate", 0.0))
            ind_detail = "ext={:.2f} rsi={:.1f} adx={:.1f} fund={:.6f}".format(
                ext, rsi, adx, fund)

            if signal is None:
                print("[" + pair + "] no setup")
                inspected.append({
                    "pair": pair, "status": "no-setup",
                    "detail": ind_detail,
                })
                continue

            # We have a signal - get its direction for the heartbeat tag
            direction = getattr(signal, "direction", "long")
            sig_id = getattr(signal, "signal_id", "")

            if sig_id in emitted:
                print("[" + pair + "] duplicate " + sig_id + ", skip")
                inspected.append({
                    "pair": pair, "status": "setup-" + direction,
                    "detail": "duplicate " + sig_id,
                })
                continue

            sizing = compute_size(bankroll, signal, bankroll_params)
            if sizing.accept:
                msg = format_signal(signal, sizing, bankroll)
                if cfg.telegram.enabled:
                    send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
                print("[" + pair + "] EMITTED " + sig_id + " (" + direction + ")")
                n_emitted += 1
                emitted.add(sig_id)
                inspected.append({
                    "pair": pair, "status": "setup-" + direction,
                    "detail": "EMITTED " + sig_id,
                })
                gist.append_log("signals_log.json", {
                    "id": sig_id,
                    "pair": pair,
                    "direction": direction,
                    "limit": getattr(signal, "limit_price", None),
                    "stop": getattr(signal, "stop_price", None),
                    "target": getattr(signal, "target_price", None),
                    "notional_usd": sizing.notional_usd,
                    "leverage": sizing.leverage_implied,
                    "risk_usd": sizing.risk_amount_usd,
                    "current_price": getattr(signal, "current_price", None),
                    "rsi": getattr(signal, "rsi", None),
                    "adx": getattr(signal, "adx", None),
                    "funding_rate": getattr(signal, "funding_rate", None),
                    "extension_atr": getattr(signal, "extension_atr", None),
                    "status": "emitted",
                })
            else:
                msg = format_rejected(signal, sizing)
                if cfg.telegram.enabled:
                    send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
                print("[" + pair + "] REJECTED " + sig_id + ": " + str(sizing.reason))
                n_rejected += 1
                emitted.add(sig_id)
                inspected.append({
                    "pair": pair, "status": "setup-" + direction,
                    "detail": "REJECTED: " + str(sizing.reason),
                })
                gist.append_log("signals_log.json", {
                    "id": sig_id,
                    "pair": pair,
                    "direction": direction,
                    "status": "rejected",
                    "reason": sizing.reason,
                })

        except Exception as e:
            err = type(e).__name__ + ": " + str(e)
            print("[" + pair + "] error: " + err)
            traceback.print_exc()
            inspected.append({
                "pair": pair, "status": "error", "detail": err[:80],
            })
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                             format_error(pair, err))

    # Persist
    try:
        gist.write("emitted_signals.json", {"ids": list(emitted)[-500:]})
        save_bankroll(gist, bankroll)
    except Exception as e:
        print("[fatal] cannot persist state: " + str(e))
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         "Cannot persist state: `" + str(e) + "`")
        # Still send heartbeat before exiting
        _send_heartbeat(cfg, bankroll, inspected, n_emitted, n_rejected)
        return 1

    # Always send heartbeat at the end
    _send_heartbeat(cfg, bankroll, inspected, n_emitted, n_rejected)

    print("\nDone. Emitted: " + str(n_emitted) + ", Rejected: " + str(n_rejected))
    return 0


if __name__ == "__main__":
    sys.exit(run())
