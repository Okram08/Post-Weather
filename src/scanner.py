"""Live scanner with HL execution.

Flow per scan (every 4h):
  1. Load config + Gist state.
  2. Init HL client (if HL_API_PRIVATE_KEY set).
  3. Reconcile existing open positions on HL (handle fills, stops, timeouts).
  4. If halted -> heartbeat and exit.
  5. For each pair: fetch data, detect setup, gates check, size, execute.
  6. Persist state.
  7. Send heartbeat.

Heartbeat is always sent at the end so the user knows the bot is alive.
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
from src.executor import (
    make_client_from_env, execute_signal, reconcile_positions,
)


def _last_closed_4h_bar(ohlc_4h: pd.DataFrame):
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


def _format_heartbeat(bankroll, inspected, emitted_count, rejected_count,
                      executed_count, reconcile_events,
                      hl_connected: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
        "*HL execution:* " + ("LIVE" if hl_connected else "PAPER (no HL)"),
        "*Open positions:* " + str(len(open_positions)),
        "*Emitted:* " + str(emitted_count) + " | *Rejected:* " + str(rejected_count)
            + " | *Executed:* " + str(executed_count),
    ]

    # Reconcile events (fills, stops, timeouts from previous scans)
    if reconcile_events:
        lines.append("")
        lines.append("*Position events this scan:*")
        for ev in reconcile_events:
            tag = {
                "entry_filled": "[FILL]",
                "stop_hit": "[STOP]",
                "target_hit": "[WIN]",
                "entry_expired": "[CXL]",
                "entry_vanished": "[CXL]",
                "position_timeout": "[TIME]",
            }.get(ev.get("event", ""), "[?]")
            line = "  " + tag + " " + ev.get("signal_id", "?")
            if "pnl_usd" in ev:
                line += " PnL=" + _fmt_usd(ev["pnl_usd"])
            if "fill_price" in ev:
                line += " @$" + str(ev["fill_price"])
            if "exit_price" in ev:
                line += " @$" + str(ev["exit_price"])
            lines.append(line)

    # Active position details
    if open_positions:
        lines.append("")
        lines.append("*Active positions:*")
        for p in open_positions:
            tag = "[" + p.get("status", "?").upper() + "]"
            d = p.get("direction", "?").upper()
            lines.append(
                "  " + tag + " " + p.get("pair", "?") + " " + d
                + " size=" + str(p.get("size"))
                + " limit=$" + str(p.get("limit_price"))
                + " stop=$" + str(p.get("stop_price"))
                + " target=$" + str(p.get("target_price"))
            )

    lines.append("")
    lines.append("*Pairs scanned:*")
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


def _send_heartbeat(cfg, bankroll, inspected, emitted_count, rejected_count,
                     executed_count, reconcile_events, hl_connected):
    if not cfg.telegram.enabled:
        return
    try:
        msg = _format_heartbeat(bankroll, inspected, emitted_count,
                                 rejected_count, executed_count,
                                 reconcile_events, hl_connected)
        send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
    except Exception as e:
        print("[heartbeat] failed: " + str(e))


def run() -> int:
    inspected = []
    n_emitted = 0
    n_rejected = 0
    n_executed = 0
    reconcile_events = []
    hl_connected = False

    try:
        cfg = load_config()
    except Exception as e:
        print("[fatal] config load: " + str(e))
        return 1

    if not cfg.gist.pat or not cfg.gist.state_gist_id:
        print("[fatal] GIST_PAT or GIST_STATE_ID missing")
        return 1

    gist = GistState(cfg.gist.pat, cfg.gist.state_gist_id)

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

    # Convert bankroll to a mutable dict for executor compatibility
    if hasattr(bankroll, "__dict__"):
        # It's a dataclass / object - convert to dict for executor
        # but we still need to save_bankroll(gist, bankroll) which expects original type
        bankroll_dict = {
            "equity_usd": getattr(bankroll, "equity_usd", cfg.bankroll.initial_capital_usd),
            "peak_equity_usd": getattr(bankroll, "peak_equity_usd",
                                       cfg.bankroll.initial_capital_usd),
            "daily_pnl_usd": getattr(bankroll, "daily_pnl_usd", 0.0),
            "halted": getattr(bankroll, "halted", False),
            "halt_reason": getattr(bankroll, "halt_reason", ""),
            "open_positions": getattr(bankroll, "open_positions", []) or [],
        }
        is_dataclass = True
    else:
        bankroll_dict = dict(bankroll)
        bankroll_dict.setdefault("open_positions", [])
        is_dataclass = False

    # ---- Init HL client ----
    hl_client = make_client_from_env()
    hl_connected = hl_client is not None
    if hl_connected:
        print("[scanner] HL client connected, LIVE EXECUTION mode")
    else:
        print("[scanner] HL client NOT configured, PAPER mode (signals only)")

    # ---- Reconcile open positions on HL ----
    if hl_client and bankroll_dict.get("open_positions"):
        print("[scanner] reconciling " + str(len(bankroll_dict["open_positions"]))
              + " open positions...")
        try:
            reconcile_events = reconcile_positions(hl_client, bankroll_dict)
            for ev in reconcile_events:
                print("[reconcile] " + str(ev))
                # Push individual fill/exit events to Telegram
                if cfg.telegram.enabled:
                    try:
                        send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                                     _format_event_message(ev))
                    except Exception:
                        pass
        except Exception as e:
            print("[scanner] reconcile failed: " + str(e))
            traceback.print_exc()

    # ---- Sync bankroll dict back to bankroll object ----
    if is_dataclass:
        bankroll.equity_usd = bankroll_dict["equity_usd"]
        bankroll.peak_equity_usd = bankroll_dict["peak_equity_usd"]
        bankroll.daily_pnl_usd = bankroll_dict["daily_pnl_usd"]
        bankroll.halted = bankroll_dict["halted"]
        bankroll.halt_reason = bankroll_dict["halt_reason"]
        bankroll.open_positions = bankroll_dict["open_positions"]

    # ---- Halted check ----
    if bankroll_dict.get("halted"):
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         format_halt(bankroll_dict.get("halt_reason", "")))
        for pair in cfg.strategy.pairs:
            inspected.append({
                "pair": pair, "status": "halted",
                "detail": "scanner halted",
            })
        save_bankroll(gist, bankroll)
        _send_heartbeat(cfg, bankroll, inspected, 0, 0, 0,
                        reconcile_events, hl_connected)
        print("[halt] " + str(bankroll_dict.get("halt_reason", "")))
        return 0

    # ---- Idempotency set ----
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
                inspected.append({"pair": pair, "status": "no-data",
                                  "detail": "<100 1h bars"})
                continue

            ohlc_4h = compute_indicators(resample_to_4h(ohlc_1h))
            if len(ohlc_4h) < 30:
                inspected.append({"pair": pair, "status": "no-data",
                                  "detail": "<30 4h bars"})
                continue

            funding = fetch_current_funding(exchange, pair)
            ohlc_4h = ohlc_4h.copy()
            ohlc_4h["funding_rate"] = funding

            last_bar = _last_closed_4h_bar(ohlc_4h)
            if last_bar is None:
                inspected.append({"pair": pair, "status": "no-data",
                                  "detail": "no closed 4h bar"})
                continue

            current_price = float(ohlc_1h["close"].iloc[-1])

            signal = detect_setup_a(last_bar, pair, current_price,
                                    cfg.strategy.setup_a)

            ext = float(last_bar.get("extension_atr", float("nan")))
            rsi = float(last_bar.get("rsi", float("nan")))
            adx = float(last_bar.get("adx", float("nan")))
            fund = float(last_bar.get("funding_rate", 0.0))
            ind_detail = "ext={:.2f} rsi={:.1f} adx={:.1f} fund={:.6f}".format(
                ext, rsi, adx, fund)

            if signal is None:
                inspected.append({"pair": pair, "status": "no-setup",
                                  "detail": ind_detail})
                continue

            direction = getattr(signal, "direction", "long")
            sig_id = getattr(signal, "signal_id", "")

            if sig_id in emitted:
                inspected.append({"pair": pair, "status": "setup-" + direction,
                                  "detail": "duplicate " + sig_id})
                continue

            sizing = compute_size(bankroll, signal, bankroll_params)
            if not sizing.accept:
                msg = format_rejected(signal, sizing)
                if cfg.telegram.enabled:
                    send_message(cfg.telegram.bot_token,
                                 cfg.telegram.chat_id, msg)
                print("[" + pair + "] REJECTED " + sig_id
                      + ": " + str(sizing.reason))
                n_rejected += 1
                emitted.add(sig_id)
                inspected.append({"pair": pair, "status": "setup-" + direction,
                                  "detail": "REJECTED: " + str(sizing.reason)})
                gist.append_log("signals_log.json", {
                    "id": sig_id, "pair": pair, "direction": direction,
                    "status": "rejected", "reason": sizing.reason,
                })
                continue

            # Sizing accepted -> emit signal Telegram
            msg = format_signal(signal, sizing, bankroll)
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
            n_emitted += 1
            emitted.add(sig_id)

            # ---- EXECUTE on HL ----
            if hl_client is None:
                # Paper mode
                inspected.append({"pair": pair, "status": "setup-" + direction,
                                  "detail": "EMITTED " + sig_id + " (PAPER)"})
                gist.append_log("signals_log.json", {
                    "id": sig_id, "pair": pair, "direction": direction,
                    "status": "emitted_paper",
                })
                print("[" + pair + "] EMITTED (paper) " + sig_id)
            else:
                # Live execution
                exec_result = execute_signal(hl_client, signal, sizing,
                                              bankroll_dict)
                if exec_result["ok"]:
                    n_executed += 1
                    bankroll_dict["open_positions"].append(exec_result["position"])
                    if is_dataclass:
                        bankroll.open_positions = bankroll_dict["open_positions"]
                    inspected.append({
                        "pair": pair, "status": "setup-" + direction,
                        "detail": "EXECUTED entry_oid="
                                  + str(exec_result["position"]["entry_oid"]),
                    })
                    gist.append_log("signals_log.json", {
                        "id": sig_id, "pair": pair, "direction": direction,
                        "status": "executed",
                        "entry_oid": exec_result["position"]["entry_oid"],
                        "stop_oid": exec_result["position"]["stop_oid"],
                        "limit": exec_result["position"]["limit_price"],
                        "stop": exec_result["position"]["stop_price"],
                        "target": exec_result["position"]["target_price"],
                        "size": exec_result["position"]["size"],
                    })
                    if cfg.telegram.enabled:
                        send_message(
                            cfg.telegram.bot_token, cfg.telegram.chat_id,
                            "[EXECUTED] " + sig_id + "\nentry_oid=`"
                            + str(exec_result["position"]["entry_oid"])
                            + "`\nstop_oid=`"
                            + str(exec_result["position"]["stop_oid"]) + "`"
                        )
                    print("[" + pair + "] EXECUTED " + sig_id)
                else:
                    inspected.append({
                        "pair": pair, "status": "setup-" + direction,
                        "detail": "EXEC FAILED: " + exec_result["reason"][:80],
                    })
                    gist.append_log("signals_log.json", {
                        "id": sig_id, "pair": pair, "direction": direction,
                        "status": "exec_failed",
                        "reason": exec_result["reason"],
                    })
                    if cfg.telegram.enabled:
                        send_message(
                            cfg.telegram.bot_token, cfg.telegram.chat_id,
                            "[EXEC FAILED] " + sig_id + "\nreason: `"
                            + exec_result["reason"][:200] + "`"
                        )
                    print("[" + pair + "] EXEC FAILED: "
                          + exec_result["reason"])

        except Exception as e:
            err = type(e).__name__ + ": " + str(e)
            print("[" + pair + "] error: " + err)
            traceback.print_exc()
            inspected.append({"pair": pair, "status": "error",
                              "detail": err[:80]})
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                             format_error(pair, err))

    # ---- Sync bankroll dict back ----
    if is_dataclass:
        bankroll.equity_usd = bankroll_dict["equity_usd"]
        bankroll.peak_equity_usd = bankroll_dict["peak_equity_usd"]
        bankroll.daily_pnl_usd = bankroll_dict["daily_pnl_usd"]
        bankroll.open_positions = bankroll_dict["open_positions"]

    # ---- Persist ----
    try:
        gist.write("emitted_signals.json", {"ids": list(emitted)[-500:]})
        save_bankroll(gist, bankroll)
    except Exception as e:
        print("[fatal] cannot persist state: " + str(e))
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         "Cannot persist state: `" + str(e) + "`")
        _send_heartbeat(cfg, bankroll, inspected, n_emitted, n_rejected,
                        n_executed, reconcile_events, hl_connected)
        return 1

    _send_heartbeat(cfg, bankroll, inspected, n_emitted, n_rejected,
                    n_executed, reconcile_events, hl_connected)

    print("\nDone. Emitted: " + str(n_emitted)
          + ", Rejected: " + str(n_rejected)
          + ", Executed: " + str(n_executed)
          + ", Reconcile events: " + str(len(reconcile_events)))
    return 0


def _format_event_message(ev: dict) -> str:
    """Format a single reconcile event for Telegram."""
    e_type = ev.get("event", "?")
    sig_id = ev.get("signal_id", "?")

    titles = {
        "entry_filled": "*POSITION OPENED*",
        "stop_hit": "*STOP HIT*",
        "target_hit": "*TARGET HIT - WIN*",
        "entry_expired": "*ENTRY EXPIRED (8h)*",
        "entry_vanished": "*ENTRY VANISHED*",
        "position_timeout": "*POSITION TIMEOUT (48h)*",
    }
    title = titles.get(e_type, "*EVENT*")

    lines = [title, "Signal: `" + sig_id + "`"]
    if "fill_price" in ev:
        lines.append("Fill price: $" + str(ev["fill_price"]))
    if "exit_price" in ev:
        lines.append("Exit price: $" + str(ev["exit_price"]))
    if "pnl_usd" in ev:
        pnl = ev["pnl_usd"]
        sign = "+" if pnl >= 0 else ""
        lines.append("PnL: " + sign + "${:.2f}".format(pnl))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
