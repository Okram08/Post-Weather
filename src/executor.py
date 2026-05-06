"""Hyperliquid execution layer.

Bridges scanner -> HL: when scanner detects a Setup A signal AND the bankroll
gates pass, executor.execute_signal() is called.

NOTIONAL CAP per trade = MIN of:
  1. equity * MAX_NOTIONAL_PCT_PER_TRADE  (20%)
  2. equity * max_leverage (from config, 1.0 = spot equivalent)
  3. EXECUTOR_HARD_NOTIONAL_CEILING ($500 absolute floor)

SAFETY GUARANTEES:
  - Every entry is paired with a stop. If stop placement fails, entry is cancelled.
  - reconcile_positions() detects orphan positions (filled without stop) at every
    scan and either re-places the stop or force-closes via market.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.hyperliquid_client import HyperliquidClient, HLLimitExceeded


# ===== EXECUTOR LIMITS =====
MAX_NOTIONAL_PCT_PER_TRADE = 0.20
EXECUTOR_HARD_NOTIONAL_CEILING = 500.0
ENTRY_VALIDITY_HOURS = 8
POSITION_MAX_HOURS = 48
HL_MIN_NOTIONAL = 10.0
# ============================


# Per-coin precision tables
SIZE_DECIMALS = {
    "BTC":   5, "ETH":   4, "SOL":   2, "HYPE":  2, "AVAX":  2,
    "LINK":  1, "ARB":   1, "OP":    1, "POL":   1, "MATIC": 1,
}
PRICE_TICK = {
    "BTC":   1.0,    "ETH":   0.1,    "SOL":   0.01,   "HYPE":  0.01,
    "AVAX":  0.01,   "LINK":  0.001,  "ARB":   0.0001, "OP":    0.001,
    "POL":   0.0001, "MATIC": 0.0001,
}
DEFAULT_SIZE_DECIMALS = 2
DEFAULT_PRICE_TICK = 0.01


def pair_to_coin(pair: str) -> str:
    return pair.split("/")[0]


def _round_size(coin: str, sz: float) -> float:
    decimals = SIZE_DECIMALS.get(coin, DEFAULT_SIZE_DECIMALS)
    return round(sz, decimals)


def _round_price(coin: str, px: float) -> float:
    tick = PRICE_TICK.get(coin, DEFAULT_PRICE_TICK)
    return round(round(px / tick) * tick, 8)


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


def _extract_oid(result: dict) -> Optional[int]:
    """Extract oid from any HL order placement response shape.

    Handles all known status types:
      - "resting": limit / trigger / Alo / Gtc still in book
      - "filled": immediately filled (rare for far-OTM limits)
      - "error": rejected (returns None, error in message)
    """
    if not isinstance(result, dict):
        return None
    if result.get("status") != "ok":
        return None
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None
    first = statuses[0]
    if not isinstance(first, dict):
        return None
    # Try every known oid-bearing key
    for key in ("resting", "filled", "trigger"):
        if key in first and isinstance(first[key], dict):
            oid = first[key].get("oid")
            if oid is not None:
                try:
                    return int(oid)
                except (TypeError, ValueError):
                    pass
    return None


def _extract_error(result: dict) -> Optional[str]:
    """Get the error message from an HL response if present."""
    try:
        if not isinstance(result, dict):
            return "non-dict result"
        if result.get("status") != "ok":
            return "status != ok: " + str(result.get("status"))
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if statuses:
            first = statuses[0]
            if isinstance(first, dict) and "error" in first:
                return str(first["error"])
            if isinstance(first, str):
                return first
    except Exception as e:
        return "extraction failed: " + str(e)
    return None


def execute_signal(client: HyperliquidClient, signal, sizing,
                   bankroll_state: dict) -> dict:
    """Place entry limit + stop trigger on HL, ATOMICALLY.

    If the stop placement fails for ANY reason, the entry is cancelled
    immediately. This guarantees no naked positions.
    """
    coin = pair_to_coin(signal.pair)
    direction = signal.direction
    limit_px = _round_price(coin, signal.limit_price)
    stop_px = _round_price(coin, signal.stop_price)
    target_px = _round_price(coin, signal.target_price)

    # Geometry validation
    if direction == "long":
        if not (stop_px < limit_px < target_px):
            return {"ok": False,
                    "reason": "long: must have stop<limit<target, got "
                              + "stop={:.4f} limit={:.4f} target={:.4f}".format(
                                  stop_px, limit_px, target_px)}
    elif direction == "short":
        if not (target_px < limit_px < stop_px):
            return {"ok": False,
                    "reason": "short: must have target<limit<stop, got "
                              + "target={:.4f} limit={:.4f} stop={:.4f}".format(
                                  target_px, limit_px, stop_px)}
    else:
        return {"ok": False, "reason": "unknown direction: " + str(direction)}

    notional = float(sizing.notional_usd)
    equity = float(bankroll_state.get("equity_usd", 0.0))
    max_leverage = float(bankroll_state.get("_max_leverage", 1.0))

    # Notional caps
    pct_cap = equity * MAX_NOTIONAL_PCT_PER_TRADE
    leverage_cap = equity * max_leverage
    effective_cap = min(pct_cap, leverage_cap, EXECUTOR_HARD_NOTIONAL_CEILING)

    if notional > effective_cap:
        if pct_cap == effective_cap:
            cap_reason = "20% per trade cap"
        elif leverage_cap == effective_cap:
            cap_reason = "{}x leverage cap".format(max_leverage)
        else:
            cap_reason = "absolute ceiling"
        print("[executor] notional ${:.2f} clamped to ${:.2f} ({})".format(
            notional, effective_cap, cap_reason))
        notional = effective_cap

    if notional < HL_MIN_NOTIONAL:
        return {"ok": False,
                "reason": "notional ${:.2f} below HL min ${:.2f} after caps".format(
                    notional, HL_MIN_NOTIONAL)}

    leverage_used = notional / equity if equity > 0 else 0
    if leverage_used > max_leverage + 0.01:
        return {"ok": False,
                "reason": "leverage {:.2f}x > max {:.1f}x".format(
                    leverage_used, max_leverage)}

    # Concurrent position cap
    open_positions = bankroll_state.get("open_positions", []) or []
    active = [p for p in open_positions if p.get("status") not in ("closed",)]
    max_concurrent = int(bankroll_state.get("_max_concurrent_positions", 5))
    if len(active) >= max_concurrent:
        return {"ok": False,
                "reason": "{} active positions, cap is {}".format(
                    len(active), max_concurrent)}

    # Compute size
    raw_size = notional / limit_px
    size = _round_size(coin, raw_size)
    if size <= 0:
        return {"ok": False,
                "reason": "computed size {} <= 0 from notional ${:.2f} / px ${:.4f}".format(
                    size, notional, limit_px)}

    actual_notional = size * limit_px
    if actual_notional < HL_MIN_NOTIONAL:
        return {"ok": False,
                "reason": "after rounding, notional ${:.2f} < HL min ${:.2f}".format(
                    actual_notional, HL_MIN_NOTIONAL)}

    # Direction flags
    entry_is_buy = (direction == "long")
    exit_is_buy = (direction == "short")

    # Cloids
    sig_hash = signal.signal_id.replace("-", "").replace("_", "")[:24].lower()
    sig_hash = "".join(c if c in "0123456789abcdef" else "0" for c in sig_hash)
    sig_hash = sig_hash.ljust(24, "0")
    entry_cloid = "0x" + (sig_hash + "00000000")[:32]
    stop_cloid = "0x" + (sig_hash + "11111111")[:32]
    try:
        int(entry_cloid, 16)
        int(stop_cloid, 16)
    except ValueError:
        entry_cloid = "0x" + uuid.uuid4().hex
        stop_cloid = "0x" + uuid.uuid4().hex

    # ===== STEP 1: place ENTRY =====
    print("[executor] placing ENTRY for " + signal.signal_id
          + " " + direction + " " + coin
          + " size=" + str(size) + " limit=$" + str(limit_px)
          + " notional=${:.2f} lev={:.2f}x".format(actual_notional, leverage_used))
    try:
        entry_result = client.place_limit_order(
            coin=coin, is_buy=entry_is_buy, sz=size, limit_px=limit_px,
            reduce_only=False, cloid_str=entry_cloid, post_only=True,
        )
    except HLLimitExceeded as e:
        return {"ok": False, "reason": "entry rejected: " + str(e)}
    except Exception as e:
        return {"ok": False, "reason": "entry exception: " + str(e)[:200]}

    entry_oid = _extract_oid(entry_result)
    if entry_oid is None:
        err = _extract_error(entry_result) or "unknown"
        return {"ok": False,
                "reason": "entry failed: " + err + " | raw: " + json.dumps(entry_result)[:200]}

    # ===== STEP 2: place STOP (CRITICAL) =====
    print("[executor] placing STOP for " + signal.signal_id
          + " trigger=$" + str(stop_px))
    stop_result = None
    stop_exception = None
    stop_oid = None
    try:
        stop_result = client.place_stop_market(
            coin=coin, is_buy=exit_is_buy, sz=size,
            trigger_px=stop_px, cloid_str=stop_cloid,
        )
        stop_oid = _extract_oid(stop_result)
    except Exception as e:
        stop_exception = str(e)[:200]

    # ===== ATOMIC GUARANTEE: if stop did not get an oid, ROLLBACK entry =====
    if stop_oid is None:
        # Build error explanation
        if stop_exception:
            stop_err = "exception: " + stop_exception
        else:
            stop_err = _extract_error(stop_result) or "no oid in response"
            stop_err += " | raw: " + json.dumps(stop_result)[:200]

        print("[executor] CRITICAL: stop has no oid, rolling back entry "
              + str(entry_oid))
        print("[executor] stop response: " + str(stop_result))
        rollback_ok = False
        try:
            cancel_result = client.cancel_order(coin, entry_oid)
            print("[executor] entry cancel result: " + str(cancel_result))
            if isinstance(cancel_result, dict) and cancel_result.get("status") == "ok":
                rollback_ok = True
        except Exception as ce:
            print("[executor] entry cancel exception: " + str(ce))

        return {"ok": False,
                "reason": "stop placement failed (entry " + (
                    "cancelled" if rollback_ok else "CANCEL ALSO FAILED")
                    + "): " + stop_err}

    # ===== Both orders successful =====
    now_iso = datetime.now(timezone.utc).isoformat()
    position = {
        "signal_id": signal.signal_id,
        "pair": signal.pair,
        "coin": coin,
        "direction": direction,
        "size": size,
        "notional_usd": round(actual_notional, 4),
        "leverage_used": round(leverage_used, 4),
        "entry_oid": entry_oid,
        "stop_oid": stop_oid,
        "target_oid": None,
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

    print("[executor] OK " + signal.signal_id + " entry_oid=" + str(entry_oid)
          + " stop_oid=" + str(stop_oid))
    return {"ok": True, "position": position, "reason": ""}


def _try_place_replacement_stop(client, pos):
    """Try to place a stop for a position that lost its stop somehow.

    Returns (ok, oid_or_error_message).
    """
    coin = pos.get("coin", pair_to_coin(pos.get("pair", "")))
    direction = pos.get("direction", "long")
    exit_is_buy = (direction == "short")
    stop_px = _round_price(coin, pos["stop_price"])
    new_cloid = "0x" + uuid.uuid4().hex
    try:
        result = client.place_stop_market(
            coin=coin, is_buy=exit_is_buy, sz=pos["size"],
            trigger_px=stop_px, cloid_str=new_cloid,
        )
        oid = _extract_oid(result)
        if oid is not None:
            return True, oid
        err = _extract_error(result) or ("no oid: " + json.dumps(result)[:200])
        return False, err
    except Exception as e:
        return False, str(e)[:200]


def _force_close_market(client, pos):
    """Last-resort: market-close a naked position.

    Returns (ok, msg).
    """
    coin = pos.get("coin", pair_to_coin(pos.get("pair", "")))
    direction = pos.get("direction", "long")
    exit_is_buy = (direction == "short")
    try:
        mark = client.get_mark_price(coin)
    except Exception as e:
        return False, "could not get mark: " + str(e)
    # Aggressive limit (slippage budget 1%)
    if exit_is_buy:
        urgent_px = _round_price(coin, mark * 1.01)
    else:
        urgent_px = _round_price(coin, mark * 0.99)
    try:
        result = client.place_limit_order(
            coin=coin, is_buy=exit_is_buy, sz=pos["size"],
            limit_px=urgent_px, reduce_only=True,
            cloid_str="0x" + uuid.uuid4().hex,
            post_only=False,  # taker
        )
        if result.get("status") == "ok":
            return True, "force-closed at ~$" + str(mark)
        return False, "close result: " + json.dumps(result)[:200]
    except Exception as e:
        return False, "close exception: " + str(e)[:200]


def reconcile_positions(client: HyperliquidClient,
                        bankroll_state: dict) -> list:
    """Walk through open positions, transition state, return events.

    SAFETY: detects naked positions (filled without stop) and either
    re-places the stop or force-closes via market.
    """
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

        # ===== SAFETY NET 1: pending_entry without stop_oid =====
        # If the entry was placed but stop_oid is None (legacy bugs or partial
        # placement), we MUST place the stop NOW or cancel the entry.
        if status == "pending_entry" and pos.get("stop_oid") is None:
            entry_oid = pos.get("entry_oid")
            entry_still_open = entry_oid in open_oids if entry_oid else False
            hl_pos = hl_pos_by_coin.get(coin)
            entry_filled = hl_pos and abs(hl_pos.get("size", 0)) >= pos["size"] * 0.95

            print("[reconcile] " + sig_id + " has NO stop_oid, attempting recovery")
            ok, info = _try_place_replacement_stop(client, pos)
            if ok:
                pos["stop_oid"] = info
                print("[reconcile] " + sig_id + " stop recovered, oid=" + str(info))
                events.append({
                    "event": "stop_recovered",
                    "signal_id": sig_id,
                    "stop_oid": info,
                })
                # Continue with normal flow below
            else:
                print("[reconcile] " + sig_id + " stop recovery FAILED: " + str(info))
                if entry_filled:
                    # Position is naked! Force close to avoid risk.
                    print("[reconcile] " + sig_id + " is NAKED FILLED, force-closing")
                    closed_ok, close_msg = _force_close_market(client, pos)
                    pos["status"] = "closed"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "naked_force_close"
                    pos["realized_pnl_usd"] = 0.0  # approximate
                    events.append({
                        "event": "naked_force_close",
                        "signal_id": sig_id,
                        "detail": close_msg,
                    })
                    continue
                elif entry_still_open:
                    # Entry not filled yet, just cancel it before it can fill
                    print("[reconcile] " + sig_id + " entry not filled, cancelling for safety")
                    try:
                        client.cancel_order(coin, entry_oid)
                    except Exception as ce:
                        print("[reconcile] cancel entry failed: " + str(ce))
                    pos["status"] = "expired"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "no_stop_safety_cancel"
                    pos["realized_pnl_usd"] = 0.0
                    events.append({
                        "event": "no_stop_safety_cancel",
                        "signal_id": sig_id,
                    })
                    continue
                else:
                    # Entry vanished (cancelled externally?) and no stop
                    pos["status"] = "expired"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "entry_vanished_no_stop"
                    pos["realized_pnl_usd"] = 0.0
                    events.append({
                        "event": "entry_vanished",
                        "signal_id": sig_id,
                    })
                    continue

        # Normal flow: pending_entry
        if status == "pending_entry":
            entry_oid = pos.get("entry_oid")
            entry_still_open = entry_oid in open_oids

            if entry_still_open:
                if age_h > ENTRY_VALIDITY_HOURS:
                    print("[reconcile] " + sig_id
                          + " entry expired (age={:.1f}h), cancelling".format(age_h))
                    try:
                        client.cancel_order(coin, entry_oid)
                    except Exception as e:
                        print("[reconcile] cancel entry failed: " + str(e))
                    if pos.get("stop_oid"):
                        try:
                            client.cancel_order(coin, pos["stop_oid"])
                        except Exception as e:
                            print("[reconcile] cancel stop failed: " + str(e))
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
            else:
                hl_pos = hl_pos_by_coin.get(coin)
                if hl_pos and abs(hl_pos["size"]) >= pos["size"] * 0.95:
                    fill_px = hl_pos.get("entry_price", pos["limit_price"])
                    pos["status"] = "open"
                    pos["filled_at"] = now.isoformat()
                    print("[reconcile] " + sig_id + " ENTRY FILLED at $" + str(fill_px))
                    events.append({
                        "event": "entry_filled",
                        "signal_id": sig_id,
                        "fill_price": fill_px,
                    })

                    # ===== SAFETY NET 2: verify stop is still alive after fill =====
                    stop_oid = pos.get("stop_oid")
                    stop_alive = stop_oid in open_oids if stop_oid else False
                    if not stop_alive:
                        print("[reconcile] " + sig_id
                              + " entry filled but stop NOT in book! re-placing")
                        ok, info = _try_place_replacement_stop(client, pos)
                        if ok:
                            pos["stop_oid"] = info
                            print("[reconcile] " + sig_id + " stop replaced, oid=" + str(info))
                            events.append({
                                "event": "stop_replaced_post_fill",
                                "signal_id": sig_id,
                                "stop_oid": info,
                            })
                        else:
                            print("[reconcile] " + sig_id
                                  + " stop replacement failed: " + str(info)
                                  + " - FORCE CLOSING")
                            closed_ok, close_msg = _force_close_market(client, pos)
                            pos["status"] = "closed"
                            pos["closed_at"] = now.isoformat()
                            pos["exit_reason"] = "naked_post_fill"
                            pos["realized_pnl_usd"] = 0.0
                            events.append({
                                "event": "naked_force_close",
                                "signal_id": sig_id,
                                "detail": close_msg,
                            })
                            continue

                    # Place target
                    try:
                        exit_is_buy = (pos["direction"] == "short")
                        target_cloid = "0x" + uuid.uuid4().hex
                        target_result = client.place_take_profit_limit(
                            coin=coin, is_buy=exit_is_buy, sz=pos["size"],
                            limit_px=pos["target_price"],
                            cloid_str=target_cloid,
                        )
                        pos["target_oid"] = _extract_oid(target_result)
                        print("[reconcile] " + sig_id
                              + " target placed oid=" + str(pos["target_oid"]))
                    except Exception as e:
                        print("[reconcile] target placement failed: " + str(e))
                        pos["target_oid"] = None

                    new_open.append(pos)
                    continue
                else:
                    print("[reconcile] " + sig_id
                          + " entry oid gone but no position: vanished")
                    if pos.get("stop_oid"):
                        try:
                            client.cancel_order(coin, pos["stop_oid"])
                        except Exception:
                            pass
                    pos["status"] = "expired"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "entry_vanished"
                    pos["realized_pnl_usd"] = 0.0
                    events.append({
                        "event": "entry_vanished",
                        "signal_id": sig_id,
                    })
                    continue

        # ===== Status: open =====
        if status == "open":
            hl_pos = hl_pos_by_coin.get(coin)
            stop_oid = pos.get("stop_oid")
            target_oid = pos.get("target_oid")

            stop_still_open = stop_oid in open_oids if stop_oid else False
            target_still_open = target_oid in open_oids if target_oid else False

            # ===== SAFETY NET 3: open position must have a live stop =====
            if hl_pos and abs(hl_pos.get("size", 0)) > 0 and not stop_still_open:
                print("[reconcile] " + sig_id
                      + " is OPEN with NO stop in book! re-placing")
                ok, info = _try_place_replacement_stop(client, pos)
                if ok:
                    pos["stop_oid"] = info
                    print("[reconcile] " + sig_id + " stop replaced, oid=" + str(info))
                    events.append({
                        "event": "stop_replaced_naked",
                        "signal_id": sig_id,
                        "stop_oid": info,
                    })
                else:
                    print("[reconcile] " + sig_id
                          + " stop replacement failed - FORCE CLOSING")
                    closed_ok, close_msg = _force_close_market(client, pos)
                    pos["status"] = "closed"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "naked_open_force_close"
                    # Compute approximate PnL
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
                        bankroll_state.get("equity_usd", 0.0) + pos["realized_pnl_usd"]
                    )
                    bankroll_state["peak_equity_usd"] = max(
                        bankroll_state.get("peak_equity_usd", 0.0),
                        bankroll_state["equity_usd"],
                    )
                    events.append({
                        "event": "naked_force_close",
                        "signal_id": sig_id,
                        "exit_price": exit_px,
                        "pnl_usd": pos["realized_pnl_usd"],
                        "detail": close_msg,
                    })
                    continue

            if not hl_pos or abs(hl_pos.get("size", 0)) < 1e-9:
                # Position closed
                exit_reason = "unknown"
                if target_oid and not target_still_open:
                    exit_reason = "target_hit"
                elif stop_oid and not stop_still_open:
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
                            print("[reconcile] cancel " + oid_field
                                  + " failed: " + str(e))

                events.append({
                    "event": exit_reason,
                    "signal_id": sig_id,
                    "exit_price": exit_px,
                    "pnl_usd": pos["realized_pnl_usd"],
                })

                bankroll_state["equity_usd"] = (
                    bankroll_state.get("equity_usd", 0.0) + pos["realized_pnl_usd"]
                )
                bankroll_state["peak_equity_usd"] = max(
                    bankroll_state.get("peak_equity_usd", 0.0),
                    bankroll_state["equity_usd"],
                )
                bankroll_state["daily_pnl_usd"] = (
                    bankroll_state.get("daily_pnl_usd", 0.0) + pos["realized_pnl_usd"]
                )
                continue

            # Check timeout
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
                        urgent_px = _round_price(coin, mark * 1.01)
                    else:
                        urgent_px = _round_price(coin, mark * 0.99)
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
                        bankroll_state.get("equity_usd", 0.0) + pos["realized_pnl_usd"]
                    )
                    bankroll_state["peak_equity_usd"] = max(
                        bankroll_state.get("peak_equity_usd", 0.0),
                        bankroll_state["equity_usd"],
                    )
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
