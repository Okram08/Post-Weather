"""Hyperliquid execution layer with BRACKET ORDERS.

CHANGES 2026-05-11 (debug session):
  - Uses client.round_price() (which queries HL meta() at startup) instead
    of hardcoded PRICE_TICK dict. Fixes ETH "Price must be divisible by
    tick size" errors.
  - _extract_oids_from_bracket: more robust parser that handles all status
    formats and logs raw response when extraction fails.
  - Keeps SIZE_DECIMALS hardcoded (we already have decent values, and HL meta
    doesn't strictly need them since we round sizes in our own logic).

NOTIONAL CAP per trade = MIN of:
  1. equity * MAX_NOTIONAL_PCT_PER_TRADE  (30%)
  2. equity * max_leverage (1.5)
  3. EXECUTOR_HARD_NOTIONAL_CEILING ($500)
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.hyperliquid_client import HyperliquidClient, HLLimitExceeded


# ===== EXECUTOR LIMITS =====
MAX_NOTIONAL_PCT_PER_TRADE = 0.30
EXECUTOR_HARD_NOTIONAL_CEILING = 500.0
ENTRY_VALIDITY_HOURS = 8
POSITION_MAX_HOURS = 48
HL_MIN_NOTIONAL = 10.0
# ============================


SIZE_DECIMALS = {
    "BTC":   5, "ETH":   4, "SOL":   2, "HYPE":  2, "AVAX":  2,
    "LINK":  1, "ARB":   1, "OP":    1, "POL":   1, "MATIC": 1,
}
DEFAULT_SIZE_DECIMALS = 2


def pair_to_coin(pair: str) -> str:
    return pair.split("/")[0]


def _round_size(coin: str, sz: float) -> float:
    decimals = SIZE_DECIMALS.get(coin, DEFAULT_SIZE_DECIMALS)
    return round(sz, decimals)


def make_client_from_env() -> Optional[HyperliquidClient]:
    api_key = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
    main_addr = os.environ.get("HL_MAIN_ADDRESS", "").strip()
    if not api_key or not main_addr:
        return None
    try:
        return HyperliquidClient(
            api_private_key=api_key,
            main_address=main_addr,
            testnet=False,
        )
    except Exception as e:
        print("[executor] failed to init HL client: " + str(e))
        return None


def _extract_oids_from_bracket(result: dict) -> dict:
    """Parse bulk_orders response with 3 statuses (entry, SL, TP).

    Returns {"entry_oid": int|None, "stop_oid": int|None, "target_oid": int|None}.
    Handles all known HL response formats:
      - {"resting": {"oid": N}}     - resting order
      - {"filled": {"oid": N, ...}} - immediate fill
      - {"error": "..."}            - rejected
    """
    out = {"entry_oid": None, "stop_oid": None, "target_oid": None}
    errors = {"entry_err": None, "stop_err": None, "target_err": None}

    if not isinstance(result, dict):
        return out, errors
    if result.get("status") != "ok":
        return out, errors

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if len(statuses) < 3:
        return out, errors

    oid_keys = ["entry_oid", "stop_oid", "target_oid"]
    err_keys = ["entry_err", "stop_err", "target_err"]

    for i, status in enumerate(statuses[:3]):
        if not isinstance(status, dict):
            errors[err_keys[i]] = "non-dict status: " + str(status)[:100]
            continue

        # Check for error first
        if "error" in status:
            errors[err_keys[i]] = str(status["error"])
            continue

        # Try all known success keys
        for k in ("resting", "filled", "trigger", "tpSl", "tpsl"):
            if k in status and isinstance(status[k], dict):
                oid = status[k].get("oid")
                if oid is not None:
                    try:
                        out[oid_keys[i]] = int(oid)
                    except (TypeError, ValueError):
                        pass
                    break

        # If still nothing, log what we saw
        if out[oid_keys[i]] is None:
            errors[err_keys[i]] = "unparsed status: " + json.dumps(status)[:200]

    return out, errors


def execute_signal(client: HyperliquidClient, signal, sizing,
                   bankroll_state: dict) -> dict:
    coin = pair_to_coin(signal.pair)
    direction = signal.direction
    # Use client.round_price (HL meta-based) instead of hardcoded ticks
    limit_px = client.round_price(coin, signal.limit_price)
    stop_px = client.round_price(coin, signal.stop_price)
    target_px = client.round_price(coin, signal.target_price)

    print("[executor] " + signal.signal_id + " " + direction + " " + coin
          + " rounded: entry=$" + str(limit_px)
          + " stop=$" + str(stop_px) + " target=$" + str(target_px))

    if direction == "long":
        if not (stop_px < limit_px < target_px):
            return {"ok": False,
                    "reason": "long: must have stop<limit<target, got "
                              + "stop={:.6f} limit={:.6f} target={:.6f}".format(
                                  stop_px, limit_px, target_px)}
    elif direction == "short":
        if not (target_px < limit_px < stop_px):
            return {"ok": False,
                    "reason": "short: must have target<limit<stop, got "
                              + "target={:.6f} limit={:.6f} stop={:.6f}".format(
                                  target_px, limit_px, stop_px)}
    else:
        return {"ok": False, "reason": "unknown direction: " + str(direction)}

    notional = float(sizing.notional_usd)
    equity = float(bankroll_state.get("equity_usd", 0.0))
    max_leverage = float(bankroll_state.get("_max_leverage", 1.5))

    pct_cap = equity * MAX_NOTIONAL_PCT_PER_TRADE
    leverage_cap = equity * max_leverage
    effective_cap = min(pct_cap, leverage_cap, EXECUTOR_HARD_NOTIONAL_CEILING)

    if notional > effective_cap:
        if pct_cap == effective_cap:
            cap_reason = "{:.0%} per trade cap".format(MAX_NOTIONAL_PCT_PER_TRADE)
        elif leverage_cap == effective_cap:
            cap_reason = "{}x leverage cap".format(max_leverage)
        else:
            cap_reason = "absolute ceiling"
        print("[executor] notional ${:.2f} clamped to ${:.2f} ({})".format(
            notional, effective_cap, cap_reason))
        notional = effective_cap

    if notional < HL_MIN_NOTIONAL:
        return {"ok": False,
                "reason": "notional ${:.2f} below HL min ${:.2f}".format(
                    notional, HL_MIN_NOTIONAL)}

    leverage_used = notional / equity if equity > 0 else 0
    if leverage_used > max_leverage + 0.01:
        return {"ok": False,
                "reason": "leverage {:.2f}x > max {:.1f}x".format(
                    leverage_used, max_leverage)}

    open_positions = bankroll_state.get("open_positions", []) or []
    active = [p for p in open_positions if p.get("status") not in ("closed",)]
    max_concurrent = int(bankroll_state.get("_max_concurrent_positions", 5))
    if len(active) >= max_concurrent:
        return {"ok": False,
                "reason": "{} active positions, cap is {}".format(
                    len(active), max_concurrent)}

    raw_size = notional / limit_px
    size = _round_size(coin, raw_size)
    if size <= 0:
        return {"ok": False,
                "reason": "computed size {} <= 0".format(size)}

    actual_notional = size * limit_px
    if actual_notional < HL_MIN_NOTIONAL:
        return {"ok": False,
                "reason": "after rounding, notional ${:.2f} < HL min".format(
                    actual_notional)}

    is_buy_entry = (direction == "long")

    sig_hash = signal.signal_id.replace("-", "").replace("_", "")[:24].lower()
    sig_hash = "".join(c if c in "0123456789abcdef" else "0" for c in sig_hash)
    sig_hash = sig_hash.ljust(24, "0")

    def _make_cloid(suffix: str) -> str:
        cl = "0x" + (sig_hash + suffix * 8)[:32]
        try:
            int(cl, 16)
            return cl
        except ValueError:
            return "0x" + uuid.uuid4().hex

    entry_cloid = _make_cloid("0")
    stop_cloid = _make_cloid("1")
    target_cloid = _make_cloid("2")

    print("[executor] placing BRACKET for " + signal.signal_id
          + " " + direction + " " + coin
          + " size=" + str(size)
          + " entry=$" + str(limit_px) + " stop=$" + str(stop_px)
          + " target=$" + str(target_px))
    try:
        result = client.place_bracket_order(
            coin=coin,
            is_buy_entry=is_buy_entry,
            sz=size,
            entry_px=limit_px,
            stop_px=stop_px,
            target_px=target_px,
            entry_cloid_str=entry_cloid,
            stop_cloid_str=stop_cloid,
            target_cloid_str=target_cloid,
            post_only_entry=True,
        )
    except HLLimitExceeded as e:
        return {"ok": False, "reason": "bracket rejected: " + str(e)}
    except Exception as e:
        return {"ok": False, "reason": "bracket exception: " + str(e)[:200]}

    oids, errors = _extract_oids_from_bracket(result)

    # Log details
    print("[executor] bracket parsed: oids=" + str(oids)
          + " errors=" + str(errors))

    # If entry oid is None, the bracket failed entirely
    if oids["entry_oid"] is None:
        err_msg = errors["entry_err"] or "no entry oid"
        return {"ok": False,
                "reason": "bracket failed: [entry] " + str(err_msg)
                          + " | raw: " + json.dumps(result)[:300]}

    # Entry succeeded. Check stop and target.
    # If either is missing, log warning but still register position so
    # reconcile can detect and fix.
    if oids["stop_oid"] is None:
        print("[executor] WARNING stop_oid is None. err="
              + str(errors["stop_err"])
              + ". Reconcile will attempt to place a replacement stop.")
    if oids["target_oid"] is None:
        print("[executor] WARNING target_oid is None. err="
              + str(errors["target_err"])
              + ". Reconcile will attempt to place a replacement target.")

    now_iso = datetime.now(timezone.utc).isoformat()
    position = {
        "signal_id": signal.signal_id,
        "pair": signal.pair,
        "coin": coin,
        "direction": direction,
        "size": size,
        "notional_usd": round(actual_notional, 4),
        "leverage_used": round(leverage_used, 4),
        "entry_oid": oids["entry_oid"],
        "stop_oid": oids["stop_oid"],
        "target_oid": oids["target_oid"],
        "limit_price": limit_px,
        "stop_price": stop_px,
        "target_price": target_px,
        "opened_at": now_iso,
        "filled_at": None,
        "closed_at": None,
        "exit_reason": None,
        "realized_pnl_usd": None,
        "status": "pending_entry",
    }

    print("[executor] BRACKET OK " + signal.signal_id
          + " entry=" + str(oids["entry_oid"])
          + " stop=" + str(oids["stop_oid"])
          + " target=" + str(oids["target_oid"]))
    return {"ok": True, "position": position, "reason": ""}


def _try_place_replacement_stop(client, pos):
    coin = pos.get("coin", pair_to_coin(pos.get("pair", "")))
    direction = pos.get("direction", "long")
    exit_is_buy = (direction == "short")
    stop_px = client.round_price(coin, pos["stop_price"])
    new_cloid = "0x" + uuid.uuid4().hex
    try:
        result = client.place_stop_market(
            coin=coin, is_buy=exit_is_buy, sz=pos["size"],
            trigger_px=stop_px, cloid_str=new_cloid,
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            return False, "bad result: " + str(result)[:200]
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return False, "no statuses"
        first = statuses[0]
        for k in ("resting", "filled", "trigger"):
            if k in first and isinstance(first[k], dict):
                oid = first[k].get("oid")
                if oid is not None:
                    return True, int(oid)
        if "error" in first:
            return False, str(first["error"])
        return False, "no oid: " + str(first)[:200]
    except Exception as e:
        return False, str(e)[:200]


def _force_close_market(client, pos):
    coin = pos.get("coin", pair_to_coin(pos.get("pair", "")))
    direction = pos.get("direction", "long")
    exit_is_buy = (direction == "short")
    try:
        mark = client.get_mark_price(coin)
    except Exception as e:
        return False, "could not get mark: " + str(e)
    if exit_is_buy:
        urgent_px = client.round_price(coin, mark * 1.01)
    else:
        urgent_px = client.round_price(coin, mark * 0.99)
    try:
        result = client.place_limit_order(
            coin=coin, is_buy=exit_is_buy, sz=pos["size"],
            limit_px=urgent_px, reduce_only=True,
            cloid_str="0x" + uuid.uuid4().hex,
            post_only=False,
        )
        if result.get("status") == "ok":
            return True, "force-closed at ~$" + str(mark)
        return False, "close result: " + json.dumps(result)[:200]
    except Exception as e:
        return False, "close exception: " + str(e)[:200]


def reconcile_positions(client: HyperliquidClient,
                        bankroll_state: dict) -> list:
    events = []
    open_positions = bankroll_state.get("open_positions", []) or []
    if not open_positions:
        return events

    now = datetime.now(timezone.utc)

    try:
        open_orders = client.get_open_orders()
        open_oids = {o.get("oid") for o in open_orders}
    except Exception as e:
        print("[reconcile] could not fetch open_orders: " + str(e))
        open_oids = set()

    try:
        hl_positions = client.get_positions()
        hl_pos_by_coin = {p["coin"]: p for p in hl_positions}
    except Exception as e:
        print("[reconcile] could not fetch positions: " + str(e))
        hl_pos_by_coin = {}

    new_open = []
    for pos in open_positions:
        coin = pos.get("coin", pair_to_coin(pos.get("pair", "")))
        sig_id = pos.get("signal_id", "?")
        status = pos.get("status", "unknown")

        try:
            opened_dt = datetime.fromisoformat(pos["opened_at"])
            age_h = (now - opened_dt).total_seconds() / 3600.0
        except Exception:
            age_h = 0.0

        if status == "pending_entry":
            entry_oid = pos.get("entry_oid")
            entry_still_open = entry_oid in open_oids if entry_oid else False
            hl_pos = hl_pos_by_coin.get(coin)
            entry_filled = (hl_pos and
                            abs(hl_pos.get("size", 0)) >= pos["size"] * 0.95)

            if entry_filled:
                fill_px = hl_pos.get("entry_price", pos["limit_price"])
                pos["status"] = "open"
                pos["filled_at"] = now.isoformat()
                print("[reconcile] " + sig_id + " ENTRY FILLED at $" + str(fill_px))
                events.append({
                    "event": "entry_filled",
                    "signal_id": sig_id,
                    "fill_price": fill_px,
                })
                stop_alive = pos.get("stop_oid") in open_oids
                target_alive = pos.get("target_oid") in open_oids
                if not stop_alive:
                    print("[reconcile] WARN " + sig_id
                          + " stop missing post-fill, recovering")
                    ok, info = _try_place_replacement_stop(client, pos)
                    if ok:
                        pos["stop_oid"] = info
                        events.append({
                            "event": "stop_replaced_post_fill",
                            "signal_id": sig_id, "stop_oid": info,
                        })
                if not target_alive:
                    print("[reconcile] WARN " + sig_id
                          + " target missing post-fill (rare with bracket)")
                new_open.append(pos)
                continue

            if entry_still_open:
                if age_h > ENTRY_VALIDITY_HOURS:
                    print("[reconcile] " + sig_id
                          + " entry expired age={:.1f}h, cancelling".format(age_h))
                    try:
                        client.cancel_order(coin, entry_oid)
                    except Exception as e:
                        print("[reconcile] cancel entry: " + str(e))
                    for oid_field in ["stop_oid", "target_oid"]:
                        oid = pos.get(oid_field)
                        if oid and oid in open_oids:
                            try:
                                client.cancel_order(coin, oid)
                            except Exception:
                                pass
                    pos["status"] = "expired"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "entry_validity_expired"
                    pos["realized_pnl_usd"] = 0.0
                    events.append({
                        "event": "entry_expired",
                        "signal_id": sig_id,
                        "age_hours": round(age_h, 2),
                    })
                    continue
                else:
                    new_open.append(pos)
                    continue

            print("[reconcile] " + sig_id + " entry vanished (no oid, no pos)")
            for oid_field in ["stop_oid", "target_oid"]:
                oid = pos.get(oid_field)
                if oid and oid in open_oids:
                    try:
                        client.cancel_order(coin, oid)
                    except Exception:
                        pass
            pos["status"] = "expired"
            pos["closed_at"] = now.isoformat()
            pos["exit_reason"] = "entry_vanished"
            pos["realized_pnl_usd"] = 0.0
            events.append({"event": "entry_vanished", "signal_id": sig_id})
            continue

        if status == "open":
            hl_pos = hl_pos_by_coin.get(coin)
            stop_oid = pos.get("stop_oid")
            target_oid = pos.get("target_oid")
            stop_alive = stop_oid in open_oids if stop_oid else False
            target_alive = target_oid in open_oids if target_oid else False

            if hl_pos and abs(hl_pos.get("size", 0)) > 0 and not stop_alive:
                print("[reconcile] " + sig_id
                      + " OPEN with NO stop, replacing")
                ok, info = _try_place_replacement_stop(client, pos)
                if ok:
                    pos["stop_oid"] = info
                    events.append({
                        "event": "stop_replaced_naked",
                        "signal_id": sig_id, "stop_oid": info,
                    })
                else:
                    print("[reconcile] " + sig_id + " stop replace failed, FORCE CLOSE")
                    closed_ok, close_msg = _force_close_market(client, pos)
                    pos["status"] = "closed"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "naked_open_force_close"
                    try:
                        exit_px = client.get_mark_price(coin)
                    except Exception:
                        exit_px = pos["limit_price"]
                    if pos["direction"] == "long":
                        pnl_per_unit = exit_px - pos["limit_price"]
                    else:
                        pnl_per_unit = pos["limit_price"] - exit_px
                    pnl_usd = pnl_per_unit * pos["size"]
                    pnl_usd -= pos["notional_usd"] * (0.000144 + 0.000432)
                    pos["realized_pnl_usd"] = round(pnl_usd, 4)
                    bankroll_state["equity_usd"] = (
                        bankroll_state.get("equity_usd", 0.0)
                        + pos["realized_pnl_usd"])
                    bankroll_state["peak_equity_usd"] = max(
                        bankroll_state.get("peak_equity_usd", 0.0),
                        bankroll_state["equity_usd"])
                    events.append({
                        "event": "naked_force_close",
                        "signal_id": sig_id,
                        "exit_price": exit_px,
                        "pnl_usd": pos["realized_pnl_usd"],
                        "detail": close_msg,
                    })
                    continue

            if not hl_pos or abs(hl_pos.get("size", 0)) < 1e-9:
                exit_reason = "unknown"
                if target_oid and not target_alive:
                    exit_reason = "target_hit"
                elif stop_oid and not stop_alive:
                    exit_reason = "stop_hit"

                if exit_reason == "target_hit":
                    exit_px = pos["target_price"]
                elif exit_reason == "stop_hit":
                    exit_px = pos["stop_price"]
                else:
                    try:
                        exit_px = client.get_mark_price(coin)
                    except Exception:
                        exit_px = pos["limit_price"]

                if pos["direction"] == "long":
                    pnl_per_unit = exit_px - pos["limit_price"]
                else:
                    pnl_per_unit = pos["limit_price"] - exit_px
                pnl_usd = pnl_per_unit * pos["size"]
                entry_fee = pos["notional_usd"] * 0.000144
                if exit_reason == "stop_hit":
                    exit_fee = pos["notional_usd"] * 0.000432
                else:
                    exit_fee = pos["notional_usd"] * 0.000144
                pnl_usd -= (entry_fee + exit_fee)

                pos["status"] = "closed"
                pos["closed_at"] = now.isoformat()
                pos["exit_reason"] = exit_reason
                pos["exit_price"] = round(exit_px, 4)
                pos["realized_pnl_usd"] = round(pnl_usd, 4)

                for oid_field in ["stop_oid", "target_oid"]:
                    other = pos.get(oid_field)
                    if other and other in open_oids:
                        try:
                            client.cancel_order(coin, other)
                            print("[reconcile] cancelled remaining "
                                  + oid_field + "=" + str(other))
                        except Exception as e:
                            print("[reconcile] cancel: " + str(e))

                events.append({
                    "event": exit_reason,
                    "signal_id": sig_id,
                    "exit_price": exit_px,
                    "pnl_usd": pos["realized_pnl_usd"],
                })

                bankroll_state["equity_usd"] = (
                    bankroll_state.get("equity_usd", 0.0)
                    + pos["realized_pnl_usd"])
                bankroll_state["peak_equity_usd"] = max(
                    bankroll_state.get("peak_equity_usd", 0.0),
                    bankroll_state["equity_usd"])
                bankroll_state["daily_pnl_usd"] = (
                    bankroll_state.get("daily_pnl_usd", 0.0)
                    + pos["realized_pnl_usd"])
                continue

            try:
                filled_dt = datetime.fromisoformat(pos["filled_at"])
                hold_h = (now - filled_dt).total_seconds() / 3600.0
            except Exception:
                hold_h = 0.0

            if hold_h > POSITION_MAX_HOURS:
                print("[reconcile] " + sig_id
                      + " hold {:.1f}h > {}h, force-closing".format(
                          hold_h, POSITION_MAX_HOURS))
                for oid_field in ["stop_oid", "target_oid"]:
                    other = pos.get(oid_field)
                    if other:
                        try:
                            client.cancel_order(coin, other)
                        except Exception:
                            pass
                exit_is_buy = (pos["direction"] == "short")
                try:
                    mark = client.get_mark_price(coin)
                    if exit_is_buy:
                        urgent_px = client.round_price(coin, mark * 1.01)
                    else:
                        urgent_px = client.round_price(coin, mark * 0.99)
                    client.place_limit_order(
                        coin=coin, is_buy=exit_is_buy, sz=pos["size"],
                        limit_px=urgent_px, reduce_only=True,
                        cloid_str="0x" + uuid.uuid4().hex,
                        post_only=False,
                    )
                    pos["status"] = "closed"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "timeout_48h"
                    pos["exit_price"] = mark
                    if pos["direction"] == "long":
                        pnl_per_unit = mark - pos["limit_price"]
                    else:
                        pnl_per_unit = pos["limit_price"] - mark
                    pnl_usd = pnl_per_unit * pos["size"]
                    pnl_usd -= pos["notional_usd"] * (0.000144 + 0.000432)
                    pos["realized_pnl_usd"] = round(pnl_usd, 4)
                    events.append({
                        "event": "position_timeout",
                        "signal_id": sig_id,
                        "exit_price": mark,
                        "pnl_usd": pos["realized_pnl_usd"],
                    })
                    bankroll_state["equity_usd"] = (
                        bankroll_state.get("equity_usd", 0.0)
                        + pos["realized_pnl_usd"])
                    bankroll_state["peak_equity_usd"] = max(
                        bankroll_state.get("peak_equity_usd", 0.0),
                        bankroll_state["equity_usd"])
                    continue
                except Exception as e:
                    print("[reconcile] force-close failed: " + str(e))
                    new_open.append(pos)
                    continue

            new_open.append(pos)
            continue

        if status not in ("closed", "expired"):
            new_open.append(pos)

    bankroll_state["open_positions"] = new_open
    return events
