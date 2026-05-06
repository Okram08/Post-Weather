"""Unified scanner with BRACKET ORDERS - runs every 30 min.

KEY CHANGE FROM v4:
  Now uses HL native bracket orders (entry + SL + TP placed atomically).
  - TP is armed INSTANTLY when entry fills - no more 30min delay.
  - Cancelling entry auto-cancels SL/TP (HL handles linkage).
  - Reconcile is now simpler: just detects state transitions, no manual TP placement.

Behavior:
  - ALWAYS reconciles open positions on HL.
  - SCAN MODE: if a new 4h bar has closed since the last scan, do the full
    scan of all 9 pairs and detect Setup A signals.
  - LIGHT MODE: if no new 4h bar, only reconcile (silent unless events).
"""
import sys
import traceback
import uuid
from datetime import datetime, timezone, timedelta

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


LOCK_KEY = "lock.json"
LOCK_TIMEOUT_MINUTES = 8
ACTIONS_LOG_KEY = "actions_log.json"
SCAN_STATE_KEY = "last_scan_4h_bar.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now():
    return datetime.now(timezone.utc)


def _try_acquire_lock(gist: GistState, holder: str) -> bool:
    try:
        existing = gist.read(LOCK_KEY)
    except Exception:
        existing = {}

    if existing and existing.get("locked"):
        try:
            locked_at = datetime.fromisoformat(existing["locked_at"])
            age_min = (_now() - locked_at).total_seconds() / 60.0
            if age_min < LOCK_TIMEOUT_MINUTES:
                print("[lock] held by '" + str(existing.get("holder"))
                      + "' for {:.1f} min - SKIP".format(age_min))
                return False
            else:
                print("[lock] STALE, force-acquiring")
        except Exception:
            print("[lock] could not parse, force-acquiring")

    try:
        gist.write(LOCK_KEY, {
            "locked": True, "holder": holder, "locked_at": _now_iso(),
        })
        print("[lock] acquired by '" + holder + "'")
        return True
    except Exception as e:
        print("[lock] acquire failed: " + str(e))
        return False


def _release_lock(gist: GistState):
    try:
        gist.write(LOCK_KEY, {"locked": False, "released_at": _now_iso()})
        print("[lock] released")
    except Exception as e:
        print("[lock] release failed: " + str(e))


def _log_action(gist: GistState, action_type: str, detail: dict):
    try:
        gist.append_log(ACTIONS_LOG_KEY, {
            "ts": _now_iso(), "type": action_type, "detail": detail,
        })
    except Exception as e:
        print("[log] action log failed: " + str(e))


def _last_4h_bar_close_iso():
    """ISO timestamp of the most recent CLOSED 4h bar boundary."""
    now = _now()
    h = now.hour
    last_close_h = (h // 4) * 4
    last_close = now.replace(hour=last_close_h, minute=0, second=0, microsecond=0)
    if last_close >= now:
        last_close = last_close - timedelta(hours=4)
    return last_close.isoformat()


def _should_run_scan(gist: GistState) -> bool:
    current_4h_close = _last_4h_bar_close_iso()
    try:
        state = gist.read(SCAN_STATE_KEY)
        last_scanned = state.get("last_4h_bar", "") if state else ""
    except Exception:
        last_scanned = ""

    if last_scanned == current_4h_close:
        print("[scan_check] last 4h bar already scanned: " + current_4h_close)
        return False
    print("[scan_check] new 4h bar to scan: " + current_4h_close
          + " (was: " + (last_scanned or "never") + ")")
    return True


def _mark_scan_done(gist: GistState):
    try:
        gist.write(SCAN_STATE_KEY, {
            "last_4h_bar": _last_4h_bar_close_iso(),
            "scanned_at": _now_iso(),
        })
    except Exception as e:
        print("[scan_check] failed to persist: " + str(e))


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


def _format_event_message(ev: dict) -> str:
    e_type = ev.get("event", "?")
    sig_id = ev.get("signal_id", "?")
    titles = {
        "entry_filled": "*POSITION OPENED*",
        "stop_hit": "*STOP HIT*",
        "target_hit": "*TARGET HIT - WIN*",
        "entry_expired": "*ENTRY EXPIRED (8h)*",
        "entry_vanished": "*ENTRY VANISHED*",
        "position_timeout": "*POSITION TIMEOUT (48h)*",
        "stop_recovered": "*STOP RECOVERED*",
        "stop_replaced_post_fill": "*STOP REPLACED post-fill*",
        "naked_force_close": "*NAKED FORCE-CLOSE*",
        "no_stop_safety_cancel": "*ENTRY CANCELLED (safety)*",
    }
    title = titles.get(e_type, "*EVENT: " + e_type + "*")
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


def _format_full_heartbeat(bankroll, inspected, emitted_count, rejected_count,
                            executed_count, reconcile_events, hl_connected):
    now_str = _now().strftime("%Y-%m-%d %H:%M UTC")

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
        "*Scan done* `" + now_str + "`",
        "",
        "*Bankroll:* " + _fmt_usd(equity)
            + " (peak " + _fmt_usd(peak) + ", DD " + _fmt_pct(dd_pct) + ")",
        "*Halted:* " + ("YES - " + str(halt_reason) if halted else "no"),
        "*HL execution:* " + ("LIVE" if hl_connected else "PAPER"),
        "*Open positions:* " + str(len(open_positions)),
        "*Emitted:* " + str(emitted_count) + " | *Rejected:* "
            + str(rejected_count) + " | *Executed:* " + str(executed_count),
    ]

    if reconcile_events:
        lines.append("")
        lines.append("*Position events this scan:*")
        for ev in reconcile_events:
            tag = {
                "entry_filled": "[FILL]", "stop_hit": "[STOP]",
                "target_hit": "[WIN]", "entry_expired": "[CXL]",
                "entry_vanished": "[CXL]", "position_timeout": "[TIME]",
                "stop_recovered": "[FIX]", "naked_force_close": "[FORCE]",
            }.get(ev.get("event", ""), "[?]")
            line = "  " + tag + " " + ev.get("signal_id", "?")
            if "pnl_usd" in ev:
                line += " PnL=" + _fmt_usd(ev["pnl_usd"])
            if "fill_price" in ev:
                line += " @$" + str(ev["fill_price"])
            lines.append(line)

    if open_positions:
        lines.append("")
        lines.append("*Active positions:*")
        for p in open_positions:
            tag = "[" + p.get("status", "?").upper() + "]"
            d = p.get("direction", "?").upper()
            lev = p.get("leverage_used")
            extra = " " + ("{:.2f}x".format(lev) if lev else "")
            lines.append(
                "  " + tag + " " + p.get("pair", "?") + " " + d + extra
                + " size=" + str(p.get("size"))
                + " limit=$" + str(p.get("limit_price"))
                + " stop=$" + str(p.get("stop_price"))
                + " target=$" + str(p.get("target_price"))
            )

    lines.append("")
    lines.append("*Pairs scanned:*")
    for d in inspected:
        tag = {
            "setup-long": "[LONG]", "setup-short": "[SHORT]",
            "no-setup": "[--]", "no-data": "[??]",
            "halted": "[HALT]", "error": "[!!]",
        }.get(d["status"], "[--]")
        lines.append("  " + tag + " `" + d["pair"] + "` " + d.get("detail", ""))

    return "\n".join(lines)


def _format_light_heartbeat(bankroll_state, reconcile_events):
    now_str = _now().strftime("%H:%M UTC")
    open_positions = bankroll_state.get("open_positions", []) or []
    equity = bankroll_state.get("equity_usd", 0.0)
    lines = [
        "*Reconcile* `" + now_str + "`",
        str(len(reconcile_events)) + " event(s) processed",
        "Equity: ${:.2f}".format(equity),
        "Open positions: " + str(len(open_positions)),
    ]
    return "\n".join(lines)


def _do_reconcile(hl_client, bankroll_dict, gist, cfg):
    if not bankroll_dict.get("open_positions"):
        return []
    n = len(bankroll_dict["open_positions"])
    print("[reconcile] " + str(n) + " positions to check")
    try:
        events = reconcile_positions(hl_client, bankroll_dict)
    except Exception as e:
        print("[reconcile] crashed: " + str(e))
        traceback.print_exc()
        _log_action(gist, "reconcile_error", {"error": str(e)[:200]})
        return []
    for ev in events:
        print("[reconcile] event: " + str(ev))
        _log_action(gist, "event", ev)
        if cfg.telegram.enabled:
            try:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                             _format_event_message(ev))
            except Exception:
                pass
    return events


def _do_full_scan(hl_client, bankroll, bankroll_dict, is_dataclass,
                   cfg, gist, reconcile_events):
    inspected = []
    n_emitted = 0
    n_rejected = 0
    n_executed = 0

    if bankroll_dict.get("halted"):
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                         format_halt(bankroll_dict.get("halt_reason", "")))
        for pair in cfg.strategy.pairs:
            inspected.append({"pair": pair, "status": "halted",
                              "detail": "halted"})
        _log_action(gist, "scan_run", {
            "halted": True, "n_pairs": len(cfg.strategy.pairs),
            "n_signals": 0, "n_executed": 0,
            "n_events": len(reconcile_events),
        })
        msg = _format_full_heartbeat(bankroll, inspected, 0, 0, 0,
                                      reconcile_events, hl_client is not None)
        if cfg.telegram.enabled:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
        return inspected, n_emitted, n_rejected, n_executed

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
                inspected.append({"pair": pair,
                                  "status": "setup-" + direction,
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
                inspected.append({
                    "pair": pair, "status": "setup-" + direction,
                    "detail": "REJECTED: " + str(sizing.reason),
                })
                _log_action(gist, "signal_rejected", {
                    "id": sig_id, "pair": pair, "reason": sizing.reason,
                })
                gist.append_log("signals_log.json", {
                    "id": sig_id, "pair": pair, "direction": direction,
                    "status": "rejected", "reason": sizing.reason,
                })
                continue

            msg = format_signal(signal, sizing, bankroll)
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token,
                             cfg.telegram.chat_id, msg)
            n_emitted += 1
            emitted.add(sig_id)
            _log_action(gist, "signal_emitted", {
                "id": sig_id, "pair": pair, "direction": direction,
            })

            if hl_client is None:
                inspected.append({"pair": pair,
                                  "status": "setup-" + direction,
                                  "detail": "EMITTED " + sig_id + " (PAPER)"})
                gist.append_log("signals_log.json", {
                    "id": sig_id, "pair": pair, "direction": direction,
                    "status": "emitted_paper",
                })
                print("[" + pair + "] EMITTED PAPER " + sig_id)
            else:
                exec_result = execute_signal(hl_client, signal, sizing,
                                              bankroll_dict)
                if exec_result["ok"]:
                    n_executed += 1
                    bankroll_dict["open_positions"].append(
                        exec_result["position"])
                    if is_dataclass:
                        bankroll.open_positions = bankroll_dict["open_positions"]
                    pos = exec_result["position"]
                    inspected.append({
                        "pair": pair, "status": "setup-" + direction,
                        "detail": "EXECUTED notional=${:.2f}".format(
                            pos.get("notional_usd", 0)),
                    })
                    _log_action(gist, "order_executed", {
                        "id": sig_id, "pair": pair,
                        "entry_oid": pos["entry_oid"],
                        "stop_oid": pos["stop_oid"],
                        "target_oid": pos.get("target_oid"),
                        "notional": pos.get("notional_usd"),
                    })
                    gist.append_log("signals_log.json", {
                        "id": sig_id, "pair": pair, "direction": direction,
                        "status": "executed",
                        "entry_oid": pos["entry_oid"],
                        "stop_oid": pos["stop_oid"],
                        "target_oid": pos.get("target_oid"),
                        "limit": pos["limit_price"],
                        "stop": pos["stop_price"],
                        "target": pos["target_price"],
                        "size": pos["size"],
                        "notional_usd": pos.get("notional_usd"),
                    })
                    if cfg.telegram.enabled:
                        send_message(
                            cfg.telegram.bot_token,
                            cfg.telegram.chat_id,
                            "*EXECUTED (bracket)* `" + sig_id + "`\n"
                            + "notional=$" + "{:.2f}".format(
                                pos.get("notional_usd", 0))
                            + "\nentry=`" + str(pos["entry_oid"]) + "`"
                            + "\nstop=`" + str(pos["stop_oid"]) + "`"
                            + "\ntarget=`" + str(pos.get("target_oid")) + "`"
                        )
                    print("[" + pair + "] EXECUTED " + sig_id)
                else:
                    inspected.append({
                        "pair": pair, "status": "setup-" + direction,
                        "detail": "EXEC FAILED: "
                                  + exec_result["reason"][:80],
                    })
                    _log_action(gist, "exec_failed", {
                        "id": sig_id, "pair": pair,
                        "reason": exec_result["reason"][:200],
                    })
                    gist.append_log("signals_log.json", {
                        "id": sig_id, "pair": pair, "direction": direction,
                        "status": "exec_failed",
                        "reason": exec_result["reason"],
                    })
                    if cfg.telegram.enabled:
                        send_message(
                            cfg.telegram.bot_token,
                            cfg.telegram.chat_id,
                            "*EXEC FAILED* `" + sig_id + "`\nreason: `"
                            + exec_result["reason"][:200] + "`"
                        )
                    print("[" + pair + "] EXEC FAILED")

        except Exception as e:
            err = type(e).__name__ + ": " + str(e)
            print("[" + pair + "] error: " + err)
            traceback.print_exc()
            inspected.append({"pair": pair, "status": "error",
                              "detail": err[:80]})
            _log_action(gist, "scan_pair_error", {
                "pair": pair, "error": err[:200],
            })
            if cfg.telegram.enabled:
                send_message(cfg.telegram.bot_token, cfg.telegram.chat_id,
                             format_error(pair, err))

    if is_dataclass:
        bankroll.open_positions = bankroll_dict["open_positions"]

    _log_action(gist, "scan_run", {
        "n_pairs": len(cfg.strategy.pairs),
        "n_signals": n_emitted,
        "n_rejected": n_rejected,
        "n_executed": n_executed,
        "n_events": len(reconcile_events),
    })

    try:
        gist.write("emitted_signals.json", {"ids": list(emitted)[-500:]})
    except Exception as e:
        print("[fatal] cannot persist emitted: " + str(e))

    msg = _format_full_heartbeat(bankroll, inspected, n_emitted, n_rejected,
                                  n_executed, reconcile_events,
                                  hl_client is not None)
    if cfg.telegram.enabled:
        send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)

    return inspected, n_emitted, n_rejected, n_executed


def run() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print("[fatal] config load: " + str(e))
        return 1

    if not cfg.gist.pat or not cfg.gist.state_gist_id:
        print("[fatal] GIST_PAT or GIST_STATE_ID missing")
        return 1

    gist = GistState(cfg.gist.pat, cfg.gist.state_gist_id)

    if not _try_acquire_lock(gist, "scanner"):
        _log_action(gist, "scan_skipped", {"reason": "lock held"})
        return 0

    try:
        scan_mode = _should_run_scan(gist)

        try:
            bankroll = load_bankroll(gist, {
                "initial_capital_usd": cfg.bankroll.initial_capital_usd
            })
        except Exception as e:
            print("[fatal] bankroll load: " + str(e))
            _log_action(gist, "scan_error", {"error": str(e)[:200]})
            return 1

        if hasattr(bankroll, "__dict__"):
            bankroll_dict = {
                "equity_usd": getattr(bankroll, "equity_usd",
                                      cfg.bankroll.initial_capital_usd),
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

        bankroll_dict["_max_leverage"] = float(cfg.bankroll.max_leverage)
        bankroll_dict["_max_concurrent_positions"] = int(
            cfg.bankroll.max_concurrent_positions)

        hl_client = make_client_from_env()
        if hl_client:
            print("[run] LIVE EXECUTION mode")
        else:
            print("[run] PAPER mode")

        reconcile_events = []
        if hl_client and bankroll_dict.get("open_positions"):
            reconcile_events = _do_reconcile(hl_client, bankroll_dict,
                                              gist, cfg)

        if is_dataclass:
            bankroll.equity_usd = bankroll_dict["equity_usd"]
            bankroll.peak_equity_usd = bankroll_dict["peak_equity_usd"]
            bankroll.daily_pnl_usd = bankroll_dict["daily_pnl_usd"]
            bankroll.halted = bankroll_dict["halted"]
            bankroll.halt_reason = bankroll_dict["halt_reason"]
            bankroll.open_positions = bankroll_dict["open_positions"]

        if scan_mode:
            print("[run] SCAN MODE (new 4h bar)")
            _log_action(gist, "scan_mode", {"mode": "full"})
            inspected, n_emitted, n_rejected, n_executed = _do_full_scan(
                hl_client, bankroll, bankroll_dict, is_dataclass,
                cfg, gist, reconcile_events)
            _mark_scan_done(gist)
        else:
            print("[run] LIGHT MODE (no new 4h bar, reconcile only)")
            _log_action(gist, "reconcile_only", {
                "n_events": len(reconcile_events),
            })
            if reconcile_events and cfg.telegram.enabled:
                try:
                    msg = _format_light_heartbeat(bankroll_dict,
                                                   reconcile_events)
                    send_message(cfg.telegram.bot_token,
                                 cfg.telegram.chat_id, msg)
                except Exception as e:
                    print("[run] light heartbeat failed: " + str(e))

        if is_dataclass:
            bankroll.equity_usd = bankroll_dict["equity_usd"]
            bankroll.peak_equity_usd = bankroll_dict["peak_equity_usd"]
            bankroll.daily_pnl_usd = bankroll_dict["daily_pnl_usd"]
            bankroll.open_positions = bankroll_dict["open_positions"]

        try:
            save_bankroll(gist, bankroll)
        except Exception as e:
            print("[fatal] persist failed: " + str(e))
            _log_action(gist, "scan_error", {
                "error": "persist: " + str(e)[:200],
            })
            return 1

        print("[run] done. mode=" + ("scan" if scan_mode else "light")
              + ", events=" + str(len(reconcile_events)))
        return 0
    finally:
        _release_lock(gist)


if __name__ == "__main__":
    sys.exit(run())
