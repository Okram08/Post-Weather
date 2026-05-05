"""Scan runner - executes every 4h via GitHub Actions cron.

Workflow:
  1. Load config + bankroll state
  2. For each pair: fetch recent 1h ohlcv, compute 4h indicators
  3. Detect Setup A signals on the latest closed 4h bar
  4. Apply bankroll gates (halted, max positions, daily loss, etc.)
  5. Emit accepted signals to Telegram + log to gist (idempotent via signal_id)
  6. ALWAYS send a heartbeat message to Telegram at end of scan
     (so user knows the bot is alive even when no setup is found)
"""
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from src.config import load_config
from src.data import make_exchange, fetch_ohlcv_recent, fetch_current_funding
from src.indicators import resample_to_4h, compute_indicators
from src.setups import detect_setup_a, build_signal
from src.bankroll import check_gates, size_position
from src.state import load_state, save_state, was_signal_emitted, mark_signal_emitted
from src.telegram_bot import send_telegram


def _fmt_pct(x):
    return "{:+.2%}".format(x) if x is not None else "n/a"


def _fmt_usd(x):
    return "${:.2f}".format(x) if x is not None else "n/a"


def main():
    print("[scanner] start " + datetime.now(timezone.utc).isoformat())

    cfg = load_config()
    p = dict(cfg.strategy.setup_a)
    p.setdefault("target_mode", "fixed_pct")

    pairs = cfg.strategy.pairs
    fees = {"maker_bps": cfg.frictions.maker_fee_bps,
            "taker_bps": cfg.frictions.taker_fee_bps}
    sizing = {"initial_capital": cfg.bankroll.initial_capital_usd,
              "risk_per_trade": cfg.bankroll.risk_per_trade_pct,
              "max_leverage": cfg.bankroll.max_leverage}

    state = load_state()
    bankroll = state.get("bankroll", {})
    equity = bankroll.get("equity_usd", cfg.bankroll.initial_capital_usd)
    peak = bankroll.get("peak_equity_usd", equity)
    halted = bankroll.get("halted", False)
    halt_reason = bankroll.get("halt_reason", "")
    open_positions = bankroll.get("open_positions", [])

    exchange = make_exchange(cfg.frictions.exchange)

    emitted = []
    rejected = []
    inspected = []  # for the heartbeat: state of each pair this scan

    for pair in pairs:
        print("[scanner] " + pair + " ...")
        try:
            ohlc_1h = fetch_ohlcv_recent(exchange, pair, "1h", n_bars=300)
            if ohlc_1h.empty or len(ohlc_1h) < 48:
                print("[" + pair + "] not enough data, skip")
                inspected.append({
                    "pair": pair, "status": "no-data", "detail": "<48 bars"
                })
                continue

            ohlc_4h = compute_indicators(resample_to_4h(ohlc_1h))
            if ohlc_4h.empty or len(ohlc_4h) < 2:
                inspected.append({
                    "pair": pair, "status": "no-data", "detail": "<2 4h bars"
                })
                continue

            # Attach current funding rate to the latest 4h bar
            current_funding = fetch_current_funding(exchange, pair)
            ohlc_4h["funding_rate"] = current_funding

            # Use the LAST CLOSED 4h bar (penultimate, since the last is in-progress)
            last_closed = ohlc_4h.iloc[-2]

            d = detect_setup_a(last_closed, p)
            if d is None:
                # Capture diagnostic for heartbeat: WHY no setup ?
                reasons = []
                if pd.isna(last_closed["adx"]):
                    reasons.append("adx=nan")
                elif last_closed["adx"] >= p["adx_max"]:
                    reasons.append("adx={:.1f}>={}".format(
                        last_closed["adx"], p["adx_max"]))
                ext = last_closed.get("extension_atr", float("nan"))
                rsi = last_closed.get("rsi", float("nan"))
                fund = last_closed.get("funding_rate", 0.0)
                reasons.append("ext={:.2f}".format(ext))
                reasons.append("rsi={:.1f}".format(rsi))
                reasons.append("fund={:.6f}".format(fund))

                inspected.append({
                    "pair": pair, "status": "no-setup",
                    "detail": ", ".join(reasons),
                })
                print("[" + pair + "] no setup (" + ", ".join(reasons) + ")")
                continue

            # We have a setup
            signal = build_signal(pair, d, last_closed, p)

            inspected.append({
                "pair": pair, "status": "setup-" + d,
                "detail": "limit={:.4f}, stop={:.4f}, target={:.4f}".format(
                    signal["limit"], signal["stop"], signal["target"]
                ),
            })

            # Idempotency check
            if was_signal_emitted(state, signal["signal_id"]):
                print("[" + pair + "] signal already emitted: " + signal["signal_id"])
                rejected.append({**signal, "reject_reason": "duplicate"})
                continue

            # Bankroll gates
            gate = check_gates(
                halted=halted, halt_reason=halt_reason,
                open_positions=open_positions,
                max_concurrent=cfg.bankroll.max_concurrent_positions,
                equity_usd=equity, peak_equity_usd=peak,
                max_drawdown_pct=cfg.bankroll.max_drawdown_pct,
                daily_pnl_usd=bankroll.get("daily_pnl_usd", 0.0),
                daily_loss_limit_pct=cfg.bankroll.daily_loss_limit_pct,
            )
            if not gate["allowed"]:
                print("[" + pair + "] gate REJECT: " + gate["reason"])
                rejected.append({**signal, "reject_reason": gate["reason"]})
                continue

            # Sizing
            notional = size_position(
                equity_usd=equity,
                risk_per_trade_pct=cfg.bankroll.risk_per_trade_pct,
                limit_price=signal["limit"], stop_price=signal["stop"],
                max_leverage=cfg.bankroll.max_leverage,
            )
            signal["notional_usd"] = notional

            # Emit
            msg = format_signal_message(signal, equity)
            send_telegram(msg)
            mark_signal_emitted(state, signal["signal_id"])
            emitted.append(signal)
            print("[" + pair + "] EMITTED " + signal["signal_id"])

        except Exception as e:
            print("[" + pair + "] ERROR: " + str(e))
            inspected.append({"pair": pair, "status": "error", "detail": str(e)})

    save_state(state)

    # Heartbeat: always send a status message at end of scan
    heartbeat = format_heartbeat(
        equity=equity, peak=peak, halted=halted, halt_reason=halt_reason,
        open_positions=open_positions, inspected=inspected,
        emitted_count=len(emitted), rejected_count=len(rejected),
    )
    try:
        send_telegram(heartbeat)
    except Exception as e:
        print("[scanner] heartbeat send failed: " + str(e))

    print("Done. Emitted: " + str(len(emitted)) + ", Rejected: " + str(len(rejected)))


def format_signal_message(s: dict, equity: float) -> str:
    arrow = "[LONG]" if s["direction"] == "long" else "[SHORT]"
    lines = [
        "*SETUP A signal* " + arrow,
        "*Pair:* `" + s["pair"] + "`",
        "*Direction:* " + s["direction"].upper(),
        "*Limit:* `{:.4f}`".format(s["limit"]),
        "*Stop:* `{:.4f}`".format(s["stop"]),
        "*Target:* `{:.4f}`".format(s["target"]),
        "*Notional:* " + _fmt_usd(s.get("notional_usd")),
        "*Bankroll:* " + _fmt_usd(equity),
        "*Signal ID:* `" + s["signal_id"] + "`",
        "",
        "_Limit valid 8h, position max 48h_",
    ]
    return "\n".join(lines)


def format_heartbeat(equity, peak, halted, halt_reason, open_positions,
                      inspected, emitted_count, rejected_count) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dd_pct = ((equity - peak) / peak) if peak > 0 else 0.0

    lines = [
        "*Scan done* `" + now + "`",
        "",
        "*Bankroll:* " + _fmt_usd(equity)
            + " (peak " + _fmt_usd(peak) + ", DD " + _fmt_pct(dd_pct) + ")",
        "*Halted:* " + ("YES - " + halt_reason if halted else "no"),
        "*Open positions:* " + str(len(open_positions)),
        "*Emitted:* " + str(emitted_count) + " | *Rejected:* " + str(rejected_count),
        "",
        "*Pairs:*",
    ]
    for d in inspected:
        emoji = {
            "setup-long": "[LONG]",
            "setup-short": "[SHORT]",
            "no-setup": "[--]",
            "no-data": "[??]",
            "error": "[!!]",
        }.get(d["status"], "[--]")
        lines.append("  " + emoji + " `" + d["pair"] + "` " + d["detail"])

    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Final safety net: even on crash, send something to Telegram
        err_msg = "*Scanner CRASHED* `{}`\n\n```\n{}\n```".format(
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            str(e)[:500]
        )
        try:
            send_telegram(err_msg)
        except Exception:
            pass
        raise
