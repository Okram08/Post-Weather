"""Reconciler - light cycle every 10 min.

Responsabilities:
  1. Acquire scan lock (skip if scanner full is running).
  2. Reconcile open positions:
     - Detect fills, place targets after fill
     - Detect stop/target hits
     - Detect orphan positions (no stop) and re-place or force-close
     - Detect timeouts (48h)
  3. Log every action to actions_log.json in Gist.
  4. Send Telegram notification on EVERY order placed/cancelled/event.
  5. Release lock and exit silently if no events.

Does NOT:
  - Scan pairs for new setups (that's scanner.py's job)
  - Send heartbeats unless events occurred
"""
import sys
import traceback
from datetime import datetime, timezone, timedelta

from src.config import load_config
from src.state import GistState, load_bankroll, save_bankroll
from src.telegram_bot import send_message
from src.executor import (
    make_client_from_env, reconcile_positions,
)


LOCK_KEY = "lock.json"
LOCK_TIMEOUT_MINUTES = 5  # auto-expire stale locks
ACTIONS_LOG_KEY = "actions_log.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now():
    return datetime.now(timezone.utc)


def _try_acquire_lock(gist: GistState, holder: str) -> bool:
    """Try to acquire the lock. Return True if acquired, False if held by other."""
    try:
        existing = gist.read(LOCK_KEY)
    except Exception:
        existing = {}

    if existing and existing.get("locked"):
        # Check if expired
        try:
            locked_at = datetime.fromisoformat(existing["locked_at"])
            age_min = (_now() - locked_at).total_seconds() / 60.0
            if age_min < LOCK_TIMEOUT_MINUTES:
                print("[lock] held by '" + str(existing.get("holder"))
                      + "' since " + str(existing.get("locked_at"))
                      + " ({:.1f} min ago, < {} min timeout) - SKIP".format(
                          age_min, LOCK_TIMEOUT_MINUTES))
                return False
            else:
                print("[lock] STALE (held by '" + str(existing.get("holder"))
                      + "' for {:.1f} min > timeout, force-acquiring".format(age_min))
        except Exception:
            print("[lock] could not parse existing lock, force-acquiring")

    # Acquire
    new_lock = {
        "locked": True,
        "holder": holder,
        "locked_at": _now_iso(),
    }
    try:
        gist.write(LOCK_KEY, new_lock)
        print("[lock] acquired by '" + holder + "'")
        return True
    except Exception as e:
        print("[lock] failed to acquire: " + str(e))
        return False


def _release_lock(gist: GistState):
    try:
        gist.write(LOCK_KEY, {"locked": False, "released_at": _now_iso()})
        print("[lock] released")
    except Exception as e:
        print("[lock] release failed: " + str(e))


def _log_action(gist: GistState, action_type: str, detail: dict):
    """Append a structured entry to actions_log.json for daily reports."""
    try:
        entry = {
            "ts": _now_iso(),
            "type": action_type,
            "detail": detail,
        }
        gist.append_log(ACTIONS_LOG_KEY, entry)
    except Exception as e:
        print("[log] failed to log action: " + str(e))


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
        "stop_recovered": "*STOP RECOVERED (safety net)*",
        "stop_replaced_post_fill": "*STOP REPLACED post-fill*",
        "stop_replaced_naked": "*STOP REPLACED on naked pos*",
        "naked_force_close": "*NAKED FORCE-CLOSE*",
        "no_stop_safety_cancel": "*ENTRY CANCELLED (no stop)*",
    }
    title = titles.get(e_type, "*EVENT: " + e_type + "*")
    lines = [title, "Signal: `" + sig_id + "`"]
    if "fill_price" in ev:
        lines.append("Fill price: $" + str(ev["fill_price"]))
    if "exit_price" in ev:
        lines.append("Exit price: $" + str(ev["exit_price"]))
    if "stop_oid" in ev:
        lines.append("New stop oid: `" + str(ev["stop_oid"]) + "`")
    if "pnl_usd" in ev:
        pnl = ev["pnl_usd"]
        sign = "+" if pnl >= 0 else ""
        lines.append("PnL: " + sign + "${:.2f}".format(pnl))
    if "detail" in ev:
        lines.append("Detail: " + str(ev["detail"])[:200])
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

    # Try to acquire lock - skip if scanner is running
    if not _try_acquire_lock(gist, "reconciler"):
        _log_action(gist, "reconcile_skipped", {"reason": "lock held"})
        print("[reconciler] skipped (lock held by other process)")
        return 0

    try:
        # Load bankroll
        try:
            bankroll = load_bankroll(gist, {
                "initial_capital_usd": cfg.bankroll.initial_capital_usd
            })
        except Exception as e:
            print("[fatal] load bankroll: " + str(e))
            _log_action(gist, "reconcile_error", {"error": str(e)[:200]})
            return 1

        # Convert bankroll to mutable dict
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

        n_positions = len(bankroll_dict.get("open_positions", []))
        if n_positions == 0:
            print("[reconciler] no open positions, nothing to do")
            _log_action(gist, "reconcile_run", {
                "n_positions": 0, "n_events": 0, "skipped": True,
            })
            return 0

        # Init HL
        hl_client = make_client_from_env()
        if hl_client is None:
            print("[reconciler] HL client unavailable, skipping")
            _log_action(gist, "reconcile_error", {
                "error": "HL client unavailable",
            })
            return 1

        # Reconcile
        print("[reconciler] reconciling " + str(n_positions) + " positions...")
        try:
            events = reconcile_positions(hl_client, bankroll_dict)
        except Exception as e:
            print("[reconciler] reconcile crashed: " + str(e))
            traceback.print_exc()
            _log_action(gist, "reconcile_error", {"error": str(e)[:200]})
            return 1

        # Sync bankroll back if dataclass
        if is_dataclass:
            bankroll.equity_usd = bankroll_dict["equity_usd"]
            bankroll.peak_equity_usd = bankroll_dict["peak_equity_usd"]
            bankroll.daily_pnl_usd = bankroll_dict["daily_pnl_usd"]
            bankroll.halted = bankroll_dict.get("halted", False)
            bankroll.halt_reason = bankroll_dict.get("halt_reason", "")
            bankroll.open_positions = bankroll_dict["open_positions"]

        # Log this reconcile run
        _log_action(gist, "reconcile_run", {
            "n_positions_before": n_positions,
            "n_positions_after": len(bankroll_dict.get("open_positions", [])),
            "n_events": len(events),
        })

        # Telegram notifs for events
        if events and cfg.telegram.enabled:
            for ev in events:
                # Log each event individually
                _log_action(gist, "event", ev)
                try:
                    msg = _format_event_message(ev)
                    send_message(cfg.telegram.bot_token,
                                 cfg.telegram.chat_id, msg)
                except Exception as e:
                    print("[reconciler] telegram event send failed: "
                          + str(e))

            # Compact summary at the end (only if events)
            try:
                summary_lines = [
                    "*Reconcile " + _now().strftime("%H:%M UTC") + "*",
                    str(len(events)) + " event(s) processed",
                    "Equity: ${:.2f}".format(bankroll_dict["equity_usd"]),
                    "Open: " + str(len(bankroll_dict.get("open_positions", []))),
                ]
                send_message(cfg.telegram.bot_token,
                             cfg.telegram.chat_id,
                             "\n".join(summary_lines))
            except Exception:
                pass

        # Persist state
        try:
            save_bankroll(gist, bankroll)
        except Exception as e:
            print("[fatal] persist failed: " + str(e))
            _log_action(gist, "reconcile_error", {
                "error": "persist: " + str(e)[:200],
            })
            return 1

        print("[reconciler] done. " + str(len(events)) + " events")
        return 0
    finally:
        _release_lock(gist)


if __name__ == "__main__":
    sys.exit(run())
