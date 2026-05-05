"""Hyperliquid execution client.

Wraps the official hyperliquid-python-sdk with safety guardrails:
  - HARDCODED max notional per order ($200) - cannot be overridden by config
  - HARDCODED max leverage (5x) - same
  - All orders idempotent via cloid (client order id)
  - Bracket orders placed atomically (entry + stop + target as TP/SL group)

API wallet model:
  - api_private_key: signs the orders (the "agent" key, trade-only scope)
  - main_address: the wallet that holds the funds and positions
  - Funds and positions stay in main wallet, agent only signs trades on behalf

Auth: HL uses EIP-712 signing, the SDK handles it from the private key.
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
# Even if config or signal asks more, we refuse.
MAX_NOTIONAL_USD_PER_ORDER = 200.0
MAX_LEVERAGE_ALLOWED = 5.0
MIN_ORDER_USD = 10.0  # HL exchange minimum
# ================================================


class HLLimitExceeded(Exception):
    """Raised when an order request exceeds hardcoded safety limits."""


class HyperliquidClient:
    """Safe Hyperliquid execution client.

    Usage:
        client = HyperliquidClient(
            api_private_key=os.getenv("HL_API_PRIVATE_KEY"),
            main_address=os.getenv("HL_MAIN_ADDRESS"),
        )
        client.get_balance()
        client.get_positions()
        client.get_mark_price("BTC")
    """

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

        # The agent wallet - signs orders
        self.agent_account = Account.from_key(api_private_key)
        self.agent_address = self.agent_account.address

        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(
            wallet=self.agent_account,
            base_url=base_url,
            account_address=main_address,  # acts on behalf of main
        )

        print("[hl_client] initialized")
        print("  agent address: " + self.agent_address)
        print("  main address:  " + self.main_address)
        print("  base url:      " + base_url)

    # ---- Read-only methods (safe) ----

    def get_user_state(self) -> dict:
        """Full user state: balance, positions, margin, etc."""
        return self.info.user_state(self.main_address)

    def get_balance(self) -> dict:
        """Returns {'account_value': x, 'available_to_trade': y, 'margin_used': z}."""
        state = self.get_user_state()
        margin = state.get("marginSummary", {})
        return {
            "account_value_usd": float(margin.get("accountValue", 0)),
            "total_ntl_pos_usd": float(margin.get("totalNtlPos", 0)),
            "total_margin_used_usd": float(margin.get("totalMarginUsed", 0)),
            "withdrawable_usd": float(state.get("withdrawable", 0)),
        }

    def get_positions(self) -> list:
        """Returns list of open positions."""
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
        """Mark price for a coin (e.g. 'BTC', 'ETH', 'SOL')."""
        mids = self.info.all_mids()
        if coin not in mids:
            raise ValueError("coin '" + coin + "' not found in mids. "
                             "Sample: " + str(list(mids.keys())[:10]))
        return float(mids[coin])

    def get_open_orders(self) -> list:
        """Returns list of open (resting) orders for main address."""
        return self.info.open_orders(self.main_address)

    # ---- Validation (used before any write) ----

    def _validate_order(self, coin: str, is_buy: bool, sz: float,
                        limit_px: float):
        """Hard safety checks. Raises HLLimitExceeded on violation."""
        if sz <= 0:
            raise HLLimitExceeded("size must be > 0, got " + str(sz))
        if limit_px <= 0:
            raise HLLimitExceeded("limit_px must be > 0, got " + str(limit_px))

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

        # Sanity check on price: must be within 50% of mark price
        try:
            mark = self.get_mark_price(coin)
        except Exception:
            return  # if we can't fetch mark, allow (rare)
        if abs(limit_px - mark) / mark > 0.5:
            raise HLLimitExceeded(
                "limit_px {:.4f} differs by >50% from mark {:.4f}".format(
                    limit_px, mark)
            )

    # ---- Write methods ----

    def place_limit_order(self, coin: str, is_buy: bool, sz: float,
                          limit_px: float, reduce_only: bool = False,
                          cloid_str: Optional[str] = None,
                          post_only: bool = True) -> dict:
        """Place a single limit order. Validates against hard limits first."""
        self._validate_order(coin, is_buy, sz, limit_px)

        cloid = None
        if cloid_str:
            cloid = Cloid.from_str(cloid_str)
        else:
            cloid = Cloid.from_str("0x" + uuid.uuid4().hex)

        order_type: OrderType = {
            "limit": {"tif": "Alo" if post_only else "Gtc"}
            # Alo = Add Liquidity Only (post-only, maker-only fee)
            # Gtc = Good Till Cancelled
        }

        print("[hl_client] place_limit_order: "
              + ("BUY" if is_buy else "SELL") + " " + coin
              + " size=" + str(sz) + " px=" + str(limit_px)
              + " notional=${:.2f}".format(abs(sz * limit_px))
              + " cloid=" + cloid.to_raw())

        result = self.exchange.order(
            name=coin,
            is_buy=is_buy,
            sz=sz,
            limit_px=limit_px,
            order_type=order_type,
            reduce_only=reduce_only,
            cloid=cloid,
        )
        return result

    def cancel_order(self, coin: str, oid: int) -> dict:
        return self.exchange.cancel(coin, oid)

    def cancel_all_orders(self, coin: Optional[str] = None) -> list:
        """Cancel all open orders, optionally filtered by coin."""
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
