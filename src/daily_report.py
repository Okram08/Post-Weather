"""Daily report - runs at 15:00 UTC.

Reads actions_log.json from Gist and generates a 24h activity summary:
  - Scan / reconcile run counts
  - Errors and lock skips
  - Trading activity (signals, orders, fills, exits)
  - PnL summary
  - Active positions
"""
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

from src.config import load_config
from src.state import GistState, load_bankroll
from src.telegram_bot import send_message


ACTIONS_LOG_KEY = "actions_log.json"
SIGNALS_LOG_KEY = "signals_log.json"
REPORT_HOURS = 24


def _now():
    return datetime.now(timezone.utc)


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _filter_recent(entries, since_dt):
    out = []
    for e in entries or []:
        ts = e.get("ts") or e.get("opened_at") or e.get("closed_at")
        if not ts:
            continue
        dt = _parse_iso(ts)
        if dt and dt >= since_dt:
            out.append(e)
    return out


def _count_by_type(entries, key="type"):
    return Counter(e.get(key, "?") for e in entries)


def _count_event_subtypes(events):
    """Count detailed event subtypes from events log."""
    return Counter(
        e.get("detail", {}).get("event", "?")
        for e in events if e.get("type") == "event"
    )


def _format_report(actions, signals_log, bankroll, since_dt) -> str:
    now = _now()
    period_str = (since_dt.strftime("%d %b %H:%M") + " -> "
                  + now.strftime("%d %b %H:%M") + " UTC")

    # Run counts
    type_counts = _count_by_type(actions)
    n_scan = type_counts.get("scan_run", 0)
    n_reconcile = type_counts.get("reconcile_run", 0)
    n_skipped = type_counts.get("reconcile_skipped", 0)
    n_errors = type_counts.get("reconcile_error", 0) + type_counts.get("scan_error", 0)

    # Event subtypes
    event_counts = _count_event_subtypes(actions)

    # Signal/orders activity
    n_signals_emitted = type_counts.get("signal_emitted", 0)
    if n_signals_emitted == 0:
        # Fallback: count from signals_log
        recent_signals = _filter_recent(signals_log, since_dt)
        n_signals_emitted = sum(1 for s in recent_signals
                                if s.get("status") in ("emitted_paper", "executed"))

    n_executed = type_counts.get("order_executed", 0)
    if n_executed == 0:
        recent_signals = _filter_recent(signals_log, since_dt)
        n_executed = sum(1 for s in recent_signals if s.get("status") == "executed")

    n_fills = event_counts.get("entry_filled", 0)
    n_targets_placed = event_counts.get("target_placed", 0)
    n_target_hits = event_counts.get("target_hit", 0)
    n_stop_hits = event_counts.get("stop_hit", 0)
    n_expired = (event_counts.get("entry_expired", 0)
                 + event_counts.get("entry_vanished", 0))
    n_timeouts = event_counts.get("position_timeout", 0)
    n_force_close = event_counts.get("naked_force_close", 0)
    n_stop_recovery = (event_counts.get("stop_recovered", 0)
                       + event_counts.get("stop_replaced_post_fill", 0)
                       + event_counts.get("stop_replaced_naked", 0))

    # Realized PnL on closed events
    pnl_total = 0.0
    closed_events = []
    for a in actions:
        if a.get("type") == "event":
            ev = a.get("detail", {})
            if "pnl_usd" in ev and ev.get("event") in (
                    "target_hit", "stop_hit", "position_timeout",
                    "naked_force_close"):
                pnl_total += _safe_float(ev["pnl_usd"])
                closed_events.append({
                    "ts": a.get("ts"),
                    "event": ev.get("event"),
                    "sig_id": ev.get("signal_id", "?"),
                    "pnl": _safe_float(ev["pnl_usd"]),
                })

    # Bankroll info
    equity = _safe_float(getattr(bankroll, "equity_usd", 0.0)
                         if hasattr(bankroll, "__dict__")
                         else bankroll.get("equity_usd", 0.0))
    peak = _safe_float(getattr(bankroll, "peak_equity_usd", 0.0)
                       if hasattr(bankroll, "__dict__")
                       else bankroll.get("peak_equity_usd", 0.0))
    halted = (getattr(bankroll, "halted", False)
              if hasattr(bankroll, "__dict__")
              else bankroll.get("halted", False))
    open_positions = (getattr(bankroll, "open_positions", []) or []
                      if hasattr(bankroll, "__dict__")
                      else bankroll.get("open_positions", []) or [])

    dd = (equity - peak) / peak if peak > 0 else 0.0

    # Build the report
    lines = [
        "*DAILY REPORT* 24h",
        "`" + period_str + "`",
        "",
        "*== Activity ==*",
        "Scan full runs   : " + str(n_scan),
        "Reconcile runs   : " + str(n_reconcile),
        "Lock skips       : " + str(n_skipped),
        "Errors           : " + str(n_errors),
        "",
        "*== Trading ==*",
        "Signals emitted  : " + str(n_signals_emitted),
        "Orders executed  : " + str(n_executed),
        "Entries filled   : " + str(n_fills),
        "Target hits      : " + str(n_target_hits),
        "Stop hits        : " + str(n_stop_hits),
        "Entries expired  : " + str(n_expired),
        "Pos timeouts     : " + str(n_timeouts),
    ]

    if n_stop_recovery > 0 or n_force_close > 0:
        lines.append("")
        lines.append("*== Safety actions ==*")
        if n_stop_recovery > 0:
            lines.append("Stop recoveries  : " + str(n_stop_recovery))
        if n_force_close > 0:
            lines.append("Naked closures   : " + str(n_force_close))

    sign = "+" if pnl_total >= 0 else ""
    lines.append("")
    lines.append("*== PnL ==*")
    lines.append("Realized 24h     : " + sign + "${:.2f}".format(pnl_total))
    lines.append("Equity now       : ${:.2f}".format(equity))
    lines.append("Peak             : ${:.2f}".format(peak))
    lines.append("Drawdown         : {:+.2%}".format(dd))
    if halted:
        lines.append("HALTED           : YES")

    if closed_events:
        lines.append("")
        lines.append("*== Closed trades ==*")
        for ce in closed_events[-10:]:
            tag = {
                "target_hit": "[WIN]",
                "stop_hit": "[LOSS]",
                "position_timeout": "[TIME]",
                "naked_force_close": "[!FORCE!]",
            }.get(ce["event"], "[?]")
            sign = "+" if ce["pnl"] >= 0 else ""
            lines.append(tag + " " + ce["sig_id"][:30]
                         + " " + sign + "${:.2f}".format(ce["pnl"]))

    if open_positions:
        lines.append("")
        lines.append("*== Active positions ==*")
        for p in open_positions:
            tag = "[" + p.get("status", "?").upper()[:10] + "]"
            lines.append(
                tag + " " + p.get("pair", "?")
                + " " + p.get("direction", "?").upper()
                + " size=" + str(p.get("size"))
                + " limit=$" + str(p.get("limit_price"))
                + " stop=$" + str(p.get("stop_price"))
            )

    return "\n".join(lines)


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

    since_dt = _now() - timedelta(hours=REPORT_HOURS)
    print("[report] generating for period since " + since_dt.isoformat())

    # Read actions log
    try:
        raw_log = gist.read(ACTIONS_LOG_KEY)
        if isinstance(raw_log, list):
            all_actions = raw_log
        elif isinstance(raw_log, dict) and "entries" in raw_log:
            all_actions = raw_log["entries"]
        else:
            all_actions = []
    except Exception as e:
        print("[report] could not read actions log: " + str(e))
        all_actions = []

    # Filter to last 24h
    actions = _filter_recent(all_actions, since_dt)
    print("[report] " + str(len(actions)) + " actions in last 24h "
          + "(" + str(len(all_actions)) + " total)")

    # Read signals log (fallback for signal counts)
    try:
        raw_signals = gist.read(SIGNALS_LOG_KEY)
        if isinstance(raw_signals, list):
            signals_log = raw_signals
        elif isinstance(raw_signals, dict) and "entries" in raw_signals:
            signals_log = raw_signals["entries"]
        else:
            signals_log = []
    except Exception:
        signals_log = []

    # Read bankroll
    try:
        bankroll = load_bankroll(gist, {
            "initial_capital_usd": cfg.bankroll.initial_capital_usd
        })
    except Exception as e:
        print("[report] could not load bankroll: " + str(e))
        return 1

    # Build report
    msg = _format_report(actions, signals_log, bankroll, since_dt)
    print("[report] generated, length=" + str(len(msg)))
    print(msg)

    # Send
    if cfg.telegram.enabled:
        try:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
            print("[report] sent to Telegram")
        except Exception as e:
            print("[report] telegram send failed: " + str(e))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
