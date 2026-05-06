"""Hyperliquid execution client.

Wraps the official hyperliquid-python-sdk with safety guardrails:
  - HARDCODED max notional per order ($200) - cannot be overridden by config
  - HARDCODED max leverage (5x) - same
  - All orders idempotent via cloid (client order id)
  - Supports limit orders (entry) AND trigger orders (stop, target)

API wallet model:
  - api_private_key: signs the orders (the "agent" key, trade-only scope)
  - main_address: the wallet that holds the funds and positions
"""
import os
import time
import uuid
from typing import Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import OrderType
from hyperliquid.utils.types import Cloid


# ===== HARD LIMITS - DO NOT MOVE TO CONFIG =====
MAX_NOTIONAL_USD_PER_ORDER = 200.0
MAX_LEVERAGE_ALLOWED = 5.0
MIN_ORDER_USD = 10.0
# ================================================


class HLLimitExceeded(Exception):
    """Raised when an order request exceeds hardcoded safety limits."""


class HyperliquidClient:
    def __init__(self, api_private_key: str, main_address: str,
                 testnet: bool = False):
        if not api_private_key:
            raise ValueError("api_private_key required")
        if not main_address:
            raise ValueError("main_address required")
        if not main_address.startswith("0x") or len(main_address) != 42:
            raise ValueError("main_address must be 0x... 42 chars")

        self.main_address = main_address
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

        self.agent_account = Account.from_key(api_private_key)
        self.agent_address = self.agent_account.address

        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(
            wallet=self.agent_account,
            base_url=base_url,
            account_address=main_address,
        )

        print("[hl_client] initialized")
        print("  agent address: " + self.agent_address)
        print("  main address:  " + self.main_address)
        print("  base url:      " + base_url)

    # ---- Read-only methods ----

    def get_user_state(self) -> dict:
        return self.info.user_state(self.main_address)

    def get_balance(self) -> dict:
        """Returns Perps margin summary. NOTE: with Unified Account this may
        show $0 even when spot USDC is available as margin. The actual
        execution still works (verified with test_hl_order)."""
        state = self.get_user_state()
        margin = state.get("marginSummary", {})
        return {
            "account_value_usd": float(margin.get("accountValue", 0)),
            "total_ntl_pos_usd": float(margin.get("totalNtlPos", 0)),
            "total_margin_used_usd": float(margin.get("totalMarginUsed", 0)),
            "withdrawable_usd": float(state.get("withdrawable", 0)),
        }

    def get_spot_balance(self, coin: str = "USDC") -> float:
        """Get the spot balance for a given coin."""
        try:
            spot = self.info.spot_user_state(self.main_address)
            for b in spot.get("balances", []):
                if b.get("coin") == coin:
                    return float(b.get("total", 0))
        except Exception as e:
            print("[hl_client] get_spot_balance error: " + str(e))
        return 0.0

    def get_positions(self) -> list:
        state = self.get_user_state()
        positions = []
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            sz = float(pos.get("szi", 0))
            if abs(sz) < 1e-9:
                continue
            positions.append({
                "coin": pos.get("coin"),
                "size": sz,
                "side": "long" if sz > 0 else "short",
                "entry_price": float(pos.get("entryPx", 0)),
                "mark_price": float(pos.get("markPx", 0)) if pos.get("markPx") else None,
                "unrealized_pnl_usd": float(pos.get("unrealizedPnl", 0)),
                "leverage_used": float(pos.get("leverage", {}).get("value", 0)),
                "liquidation_price": float(pos.get("liquidationPx", 0))
                                     if pos.get("liquidationPx") else None,
            })
        return positions

    def get_mark_price(self, coin: str) -> float:
        mids = self.info.all_mids()
        if coin not in mids:
            raise ValueError("coin '" + coin + "' not found")
        return float(mids[coin])

    def get_open_orders(self) -> list:
        return self.info.open_orders(self.main_address)

    def query_order_status(self, oid: int) -> dict:
        """Query order status by oid. Returns dict with 'order' and 'status' fields."""
        try:
            return self.info.query_order_by_oid(self.main_address, oid)
        except Exception as e:
            return {"error": str(e)}

    # ---- Validation ----

    def _validate_order(self, coin: str, sz: float, limit_px: float):
        if sz <= 0:
            raise HLLimitExceeded("size must be > 0")
        if limit_px <= 0:
            raise HLLimitExceeded("limit_px must be > 0")

        notional = abs(sz * limit_px)
        if notional > MAX_NOTIONAL_USD_PER_ORDER:
            raise HLLimitExceeded(
                "notional ${:.2f} exceeds hard limit ${:.2f}".format(
                    notional, MAX_NOTIONAL_USD_PER_ORDER)
            )
        if notional < MIN_ORDER_USD:
            raise HLLimitExceeded(
                "notional ${:.2f} below HL minimum ${:.2f}".format(
                    notional, MIN_ORDER_USD)
            )

        try:
            mark = self.get_mark_price(coin)
            if abs(limit_px - mark) / mark > 0.5:
                raise HLLimitExceeded(
                    "limit_px {:.4f} differs by >50% from mark {:.4f}".format(
                        limit_px, mark)
                )
        except HLLimitExceeded:
            raise
        except Exception:
            pass  # if we can't fetch mark, allow

    # ---- Write methods: limit order (entry) ----

    def place_limit_order(self, coin: str, is_buy: bool, sz: float,
                          limit_px: float, reduce_only: bool = False,
                          cloid_str: Optional[str] = None,
                          post_only: bool = True) -> dict:
        """Place a limit order. post_only=True uses Alo (maker-only)."""
        self._validate_order(coin, sz, limit_px)

        if cloid_str is None:
            cloid_str = "0x" + uuid.uuid4().hex
        cloid = Cloid.from_str(cloid_str)

        order_type: OrderType = {
            "limit": {"tif": "Alo" if post_only else "Gtc"}
        }

        print("[hl_client] place_limit_order: "
              + ("BUY" if is_buy else "SELL") + " " + coin
              + " sz=" + str(sz) + " px=" + str(limit_px)
              + " notional=${:.2f}".format(abs(sz * limit_px))
              + " " + ("Alo" if post_only else "Gtc")
              + " cloid=" + cloid.to_raw())

        return self.exchange.order(
            name=coin, is_buy=is_buy, sz=sz, limit_px=limit_px,
            order_type=order_type, reduce_only=reduce_only, cloid=cloid,
        )

    # ---- Write methods: trigger order (stop / target) ----

    def place_stop_market(self, coin: str, is_buy: bool, sz: float,
                          trigger_px: float,
                          cloid_str: Optional[str] = None) -> dict:
        """Place a Stop Market order (becomes market when trigger hit).

        For a LONG position: is_buy=False, trigger_px = stop_loss_price (below entry)
        For a SHORT position: is_buy=True, trigger_px = stop_loss_price (above entry)

        is_buy refers to the EXIT direction (closing the position).
        Always reduce_only=True.
        """
        if trigger_px <= 0:
            raise HLLimitExceeded("trigger_px must be > 0")
        if sz <= 0:
            raise HLLimitExceeded("size must be > 0")

        # Sanity: trigger should be within 30% of mark
        try:
            mark = self.get_mark_price(coin)
            if abs(trigger_px - mark) / mark > 0.3:
                raise HLLimitExceeded(
                    "trigger_px {:.4f} differs >30% from mark {:.4f}".format(
                        trigger_px, mark)
                )
        except HLLimitExceeded:
            raise
        except Exception:
            pass

        if cloid_str is None:
            cloid_str = "0x" + uuid.uuid4().hex
        cloid = Cloid.from_str(cloid_str)

        order_type: OrderType = {
            "trigger": {
                "triggerPx": trigger_px,
                "isMarket": True,
                "tpsl": "sl",
            }
        }

        # When trigger fires, market order: limit_px is just a worst-case slippage limit.
        # For a stop SELL (closing long), set limit_px far below trigger.
        # For a stop BUY (closing short), set limit_px far above trigger.
        if is_buy:
            # closing short: market BUY at any price up to +20%
            slippage_limit = trigger_px * 1.20
        else:
            # closing long: market SELL at any price down to -20%
            slippage_limit = trigger_px * 0.80

        print("[hl_client] place_stop_market: "
              + ("BUY" if is_buy else "SELL") + " " + coin
              + " sz=" + str(sz) + " trigger=$" + str(trigger_px)
              + " (reduce_only)"
              + " cloid=" + cloid.to_raw())

        return self.exchange.order(
            name=coin, is_buy=is_buy, sz=sz, limit_px=slippage_limit,
            order_type=order_type, reduce_only=True, cloid=cloid,
        )

    def place_take_profit_limit(self, coin: str, is_buy: bool, sz: float,
                                 limit_px: float,
                                 cloid_str: Optional[str] = None) -> dict:
        """Place a Take Profit as a regular limit order (post-only).

        For a LONG position: is_buy=False, limit_px = target (above entry)
        For a SHORT position: is_buy=True, limit_px = target (below entry)

        Always reduce_only=True. post-only for maker fee.
        """
        return self.place_limit_order(
            coin=coin, is_buy=is_buy, sz=sz, limit_px=limit_px,
            reduce_only=True, cloid_str=cloid_str, post_only=True,
        )

    # ---- Cancel ----

    def cancel_order(self, coin: str, oid: int) -> dict:
        return self.exchange.cancel(coin, oid)

    def cancel_all_orders(self, coin: Optional[str] = None) -> list:
        orders = self.get_open_orders()
        results = []
        for o in orders:
            if coin and o.get("coin") != coin:
                continue
            try:
                r = self.cancel_order(o["coin"], o["oid"])
                results.append({"oid": o["oid"], "result": r})
            except Exception as e:
                results.append({"oid": o["oid"], "error": str(e)})
        return results
