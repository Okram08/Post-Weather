"""Hyperliquid execution layer.

Bridges scanner -> HL: when scanner detects a Setup A signal AND the bankroll
gates pass, executor.execute_signal() is called. It:

  1. Computes order size from notional (signal.notional_usd)
  2. Places the ENTRY limit order (post-only, maker fee)
  3. Places the STOP MARKET trigger order (reduce_only, taker fee on fill)
  4. The TARGET will be placed only AFTER the entry fills, by reconcile_positions
     (this avoids the case where target fills before entry due to spread).
  5. Records all OIDs in the gist position record so reconcile_positions can
     monitor the lifecycle.

reconcile_positions() runs at every scan and:
  - Cancels stale entry orders (> 8h old, never filled)
  - When entry just filled: places the target order
  - Detects when stop or target was filled: closes the position, updates bankroll PnL
  - Detects time-out at 48h: forces a market close

Pair format: "BTC/USDC:USDC" -> coin "BTC" (HL uses just the asset name).
"""
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.hyperliquid_client import HyperliquidClient, HLLimitExceeded


# ===== HARD LIMITS (bot-level safety on top of HL client limits) =====
EXECUTOR_MAX_NOTIONAL_PER_TRADE = 100.0  # USD per single position
EXECUTOR_MAX_OPEN_POSITIONS = 1
ENTRY_VALIDITY_HOURS = 8
POSITION_MAX_HOURS = 48
# =====================================================================


def pair_to_coin(pair: str) -> str:
    """'BTC/USDC:USDC' -> 'BTC'."""
    return pair.split("/")[0]


def _round_size(coin: str, sz: float) -> float:
    """Round position size to HL-compatible precision per coin.

    HL uses asset-specific szDecimals. For our 3 pairs:
      BTC: 5 decimals, ETH: 4 decimals, SOL: 2 decimals.
    """
    decimals = {"BTC": 5, "ETH": 4, "SOL": 2}.get(coin, 4)
    return round(sz, decimals)


def _round_price(coin: str, px: float) -> float:
    """Round price to HL tick size per coin.

    HL has price tick: 5 significant digits, max 6 decimals for most coins.
    For simplicity we round to a per-coin tick:
      BTC: $1, ETH: $0.1, SOL: $0.01
    """
    tick = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.01}.get(coin, 0.01)
    return round(round(px / tick) * tick, 6)


def make_client_from_env() -> Optional[HyperliquidClient]:
    """Returns None if HL credentials not configured (paper mode).
    Returns a connected HyperliquidClient otherwise."""
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


def execute_signal(client: HyperliquidClient, signal, sizing,
                   bankroll_state: dict) -> dict:
    """Place entry limit + stop trigger on HL.

    Returns a dict with execution status, OIDs, and a position record to
    persist in the gist bankroll state.

    Args:
      client: a connected HyperliquidClient
      signal: a Signal object (from src.setups). Has fields: pair, direction,
              limit_price, stop_price, target_price, signal_id, current_price.
      sizing: a Sizing object (from src.bankroll). Has fields: notional_usd,
              accept, reason.
      bankroll_state: current dict from gist, used to count open positions.

    Returns:
      {
        "ok": bool,
        "reason": str (if not ok),
        "position": {  # to add to bankroll_state["open_positions"]
            "signal_id": str,
            "pair": str,
            "direction": "long" or "short",
            "size": float,
            "entry_oid": int,
            "stop_oid": int,
            "target_oid": int or None,  # None until entry fills
            "limit_price": float,
            "stop_price": float,
            "target_price": float,
            "opened_at": ISO string,
            "filled_at": null,
            "status": "pending_entry",
        }
      }
    """
    coin = pair_to_coin(signal.pair)
    direction = signal.direction
    limit_px = _round_price(coin, signal.limit_price)
    stop_px = _round_price(coin, signal.stop_price)
    target_px = _round_price(coin, signal.target_price)

    # Sanity check the geometry of the order
    if direction == "long":
        # entry is below current_price, stop below entry, target above entry
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

    # Notional check (executor-level, in addition to client-level)
    notional = float(sizing.notional_usd)
    if notional > EXECUTOR_MAX_NOTIONAL_PER_TRADE:
        return {"ok": False,
                "reason": "notional ${:.2f} > executor cap ${:.2f}".format(
                    notional, EXECUTOR_MAX_NOTIONAL_PER_TRADE)}

    # Concurrent position cap
    open_positions = bankroll_state.get("open_positions", []) or []
    active = [p for p in open_positions if p.get("status") not in ("closed",)]
    if len(active) >= EXECUTOR_MAX_OPEN_POSITIONS:
        return {"ok": False,
                "reason": "{} active positions, cap is {}".format(
                    len(active), EXECUTOR_MAX_OPEN_POSITIONS)}

    # Compute size from notional and entry price
    raw_size = notional / limit_px
    size = _round_size(coin, raw_size)
    if size <= 0:
        return {"ok": False,
                "reason": "computed size {} <= 0 from notional ${:.2f} / px ${:.4f}".format(
                    size, notional, limit_px)}

    # Direction flags for HL
    entry_is_buy = (direction == "long")
    exit_is_buy = (direction == "short")  # closing direction = opposite

    # Generate stable cloids derived from signal_id so retry is idempotent
    sig_hash = signal.signal_id.replace("-", "").replace("_", "")[:24].lower()
    sig_hash = "".join(c if c in "0123456789abcdef" else "0" for c in sig_hash)
    sig_hash = sig_hash.ljust(24, "0")
    entry_cloid = "0x" + sig_hash + "0000000e"  # last 8 chars distinguish role
    stop_cloid = "0x" + sig_hash + "0000000s"
    # but cloid must be hex - replace non-hex
    entry_cloid = "0x" + (sig_hash + "00000000")[:32]
    stop_cloid = "0x" + (sig_hash + "11111111")[:32]
    # validate hex
    try:
        int(entry_cloid, 16)
        int(stop_cloid, 16)
    except ValueError:
        # fallback to random
        entry_cloid = "0x" + uuid.uuid4().hex
        stop_cloid = "0x" + uuid.uuid4().hex

    # ---- Place ENTRY (limit, post-only) ----
    print("[executor] placing ENTRY for " + signal.signal_id
          + " " + direction + " " + coin
          + " size=" + str(size) + " limit=$" + str(limit_px))
    try:
        entry_result = client.place_limit_order(
            coin=coin,
            is_buy=entry_is_buy,
            sz=size,
            limit_px=limit_px,
            reduce_only=False,
            cloid_str=entry_cloid,
            post_only=True,
        )
    except HLLimitExceeded as e:
        return {"ok": False, "reason": "entry rejected: " + str(e)}
    except Exception as e:
        return {"ok": False, "reason": "entry exception: " + str(e)[:200]}

    entry_oid = _extract_oid(entry_result)
    if entry_oid is None:
        err = _extract_error(entry_result)
        return {"ok": False,
                "reason": "entry failed: " + (err or str(entry_result)[:300])}

    # ---- Place STOP (trigger market, reduce_only) ----
    # NOTE: at this moment the position isn't open yet (entry is resting).
    # We still place the stop now: HL accepts reduce_only triggers without
    # an open position; the stop will only execute IF a position is open
    # AND the trigger is hit. This avoids the race condition of placing the
    # stop after entry fill.
    print("[executor] placing STOP for " + signal.signal_id
          + " trigger=$" + str(stop_px))
    stop_oid = None
    try:
        stop_result = client.place_stop_market(
            coin=coin,
            is_buy=exit_is_buy,
            sz=size,
            trigger_px=stop_px,
            cloid_str=stop_cloid,
        )
        stop_oid = _extract_oid(stop_result)
        if stop_oid is None:
            print("[executor] WARNING stop did not return oid: "
                  + str(stop_result)[:300])
    except Exception as e:
        # If stop fails, we cancel the entry to keep the system consistent.
        print("[executor] stop failed, cancelling entry. err: " + str(e)[:200])
        try:
            client.cancel_order(coin, entry_oid)
        except Exception as ce:
            print("[executor] entry cancel also failed: " + str(ce)[:200])
        return {"ok": False,
                "reason": "stop placement failed (entry cancelled): " + str(e)[:200]}

    # Build position record
    now_iso = datetime.now(timezone.utc).isoformat()
    position = {
        "signal_id": signal.signal_id,
        "pair": signal.pair,
        "coin": coin,
        "direction": direction,
        "size": size,
        "notional_usd": round(notional, 4),
        "entry_oid": entry_oid,
        "stop_oid": stop_oid,
        "target_oid": None,  # placed after entry fills
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


def reconcile_positions(client: HyperliquidClient,
                        bankroll_state: dict) -> list:
    """Walk through open positions, transition their state, and return events.

    Events returned (list of dicts):
      [{"event": "entry_filled", "signal_id": ..., "fill_price": ...}, ...]
      [{"event": "stop_filled", "signal_id": ..., "fill_price": ..., "pnl_usd": ...}, ...]
      [{"event": "target_filled", ...}, ...]
      [{"event": "entry_expired", ...}]   # cancelled after 8h
      [{"event": "position_timeout", ...}] # market-closed after 48h

    The bankroll_state is mutated in place: positions are updated, closed ones
    moved out of open_positions, equity updated for closed PnL.
    """
    events = []
    open_positions = bankroll_state.get("open_positions", []) or []
    if not open_positions:
        return events

    now = datetime.now(timezone.utc)

    # Get current open orders once
    try:
        open_orders = client.get_open_orders()
        open_oids = {o.get("oid") for o in open_orders}
    except Exception as e:
        print("[reconcile] could not fetch open_orders: " + str(e))
        open_oids = set()

    # Get current positions on HL
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

        # ---- Status: pending_entry ----
        if status == "pending_entry":
            entry_oid = pos.get("entry_oid")
            entry_still_open = entry_oid in open_oids

            if entry_still_open:
                # Check if expired (validity 8h)
                if age_h > ENTRY_VALIDITY_HOURS:
                    print("[reconcile] " + sig_id
                          + " entry expired (age={:.1f}h), cancelling".format(age_h))
                    try:
                        client.cancel_order(coin, entry_oid)
                    except Exception as e:
                        print("[reconcile] cancel entry failed: " + str(e))
                    # Also cancel the stop (no longer needed)
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
                    # don't add to new_open
                    continue
                else:
                    # Still pending and within validity
                    new_open.append(pos)
                    continue
            else:
                # Entry no longer in open orders -> filled or cancelled externally
                # Check HL position to know
                hl_pos = hl_pos_by_coin.get(coin)
                if hl_pos and abs(hl_pos["size"]) >= pos["size"] * 0.95:
                    # Entry filled
                    fill_px = hl_pos.get("entry_price", pos["limit_price"])
                    pos["status"] = "open"
                    pos["filled_at"] = now.isoformat()
                    print("[reconcile] " + sig_id + " ENTRY FILLED at $" + str(fill_px))
                    events.append({
                        "event": "entry_filled",
                        "signal_id": sig_id,
                        "fill_price": fill_px,
                    })

                    # Now place the target
                    try:
                        exit_is_buy = (pos["direction"] == "short")
                        target_cloid = "0x" + uuid.uuid4().hex
                        target_result = client.place_take_profit_limit(
                            coin=coin,
                            is_buy=exit_is_buy,
                            sz=pos["size"],
                            limit_px=pos["target_price"],
                            cloid_str=target_cloid,
                        )
                        pos["target_oid"] = _extract_oid(target_result)
                        print("[reconcile] " + sig_id
                              + " target placed oid=" + str(pos["target_oid"]))
                    except Exception as e:
                        print("[reconcile] target placement failed: " + str(e))
                        # Position stays open without target; will timeout at 48h
                        pos["target_oid"] = None

                    new_open.append(pos)
                    continue
                else:
                    # Entry vanished but no position -> probably cancelled externally
                    print("[reconcile] " + sig_id
                          + " entry oid gone but no position: probably cancelled")
                    pos["status"] = "expired"
                    pos["closed_at"] = now.isoformat()
                    pos["exit_reason"] = "entry_vanished"
                    pos["realized_pnl_usd"] = 0.0
                    events.append({
                        "event": "entry_vanished",
                        "signal_id": sig_id,
                    })
                    continue

        # ---- Status: open ----
        if status == "open":
            hl_pos = hl_pos_by_coin.get(coin)
            stop_oid = pos.get("stop_oid")
            target_oid = pos.get("target_oid")

            stop_still_open = stop_oid in open_oids if stop_oid else False
            target_still_open = target_oid in open_oids if target_oid else False

            if not hl_pos or abs(hl_pos.get("size", 0)) < 1e-9:
                # Position closed -> determine reason
                # If stop oid no longer open and was placed -> stop fired
                # If target oid no longer open and was placed -> target hit
                exit_reason = "unknown"
                if target_oid and not target_still_open:
                    exit_reason = "target_hit"
                elif stop_oid and not stop_still_open:
                    exit_reason = "stop_hit"

                # Compute realized PnL using last known mark and entry
                # Better: query HL trade history for accurate fill price.
                # For now, approximate: target_hit -> use target_price, stop_hit -> stop_price
                if exit_reason == "target_hit":
                    exit_px = pos["target_price"]
                elif exit_reason == "stop_hit":
                    exit_px = pos["stop_price"]
                else:
                    # use last known mark
                    try:
                        exit_px = client.get_mark_price(coin)
                    except Exception:
                        exit_px = pos["limit_price"]

                if pos["direction"] == "long":
                    pnl_per_unit = exit_px - pos["limit_price"]
                else:
                    pnl_per_unit = pos["limit_price"] - exit_px
                pnl_usd = pnl_per_unit * pos["size"]
                # Subtract estimated fees (entry maker + exit maker or taker)
                entry_fee = pos["notional_usd"] * 0.000144  # maker
                if exit_reason == "stop_hit":
                    exit_fee = pos["notional_usd"] * 0.000432  # taker
                else:
                    exit_fee = pos["notional_usd"] * 0.000144  # maker
                pnl_usd -= (entry_fee + exit_fee)

                pos["status"] = "closed"
                pos["closed_at"] = now.isoformat()
                pos["exit_reason"] = exit_reason
                pos["exit_price"] = round(exit_px, 4)
                pos["realized_pnl_usd"] = round(pnl_usd, 4)

                # Cancel any remaining orders
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

                # Update bankroll equity
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

            # Position still open -> check for time-out
            try:
                filled_dt = datetime.fromisoformat(pos["filled_at"])
                hold_h = (now - filled_dt).total_seconds() / 3600.0
            except Exception:
                hold_h = 0.0

            if hold_h > POSITION_MAX_HOURS:
                print("[reconcile] " + sig_id
                      + " hold {:.1f}h > {}h, force-closing".format(
                          hold_h, POSITION_MAX_HOURS))
                # Cancel target and stop
                for oid_field in ["stop_oid", "target_oid"]:
                    other = pos.get(oid_field)
                    if other:
                        try:
                            client.cancel_order(coin, other)
                        except Exception:
                            pass
                # Place a market close
                exit_is_buy = (pos["direction"] == "short")
                try:
                    mark = client.get_mark_price(coin)
                    # market via aggressive limit
                    if exit_is_buy:
                        urgent_px = _round_price(coin, mark * 1.01)
                    else:
                        urgent_px = _round_price(coin, mark * 0.99)
                    close_result = client.place_limit_order(
                        coin=coin, is_buy=exit_is_buy, sz=pos["size"],
                        limit_px=urgent_px, reduce_only=True,
                        cloid_str="0x" + uuid.uuid4().hex,
                        post_only=False,  # taker
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
                    # Keep open, retry next scan
                    new_open.append(pos)
                    continue

            new_open.append(pos)
            continue

        # Other statuses -> drop
        if status not in ("closed", "expired"):
            new_open.append(pos)

    bankroll_state["open_positions"] = new_open
    return events


def _extract_oid(result: dict) -> Optional[int]:
    """Extract oid from HL order placement response."""
    try:
        if not isinstance(result, dict) or result.get("status") != "ok":
            return None
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return None
        first = statuses[0]
        if "resting" in first:
            return int(first["resting"]["oid"])
        if "filled" in first:
            return int(first["filled"]["oid"])
        return None
    except Exception:
        return None


def _extract_error(result: dict) -> Optional[str]:
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if statuses and "error" in statuses[0]:
            return str(statuses[0]["error"])
    except Exception:
        pass
    return None
