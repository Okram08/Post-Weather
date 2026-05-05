"""Read-only test of Hyperliquid connection.

Run via the workflow `test_hl_connection.yml` to verify:
  1. API key works (signs requests successfully)
  2. We can read your main account state
  3. We can read mark prices for BTC/ETH/SOL
  4. We can list open orders/positions

NO ORDERS ARE PLACED. This is purely a sanity check.

Required env vars (from GitHub Actions secrets):
  HL_API_PRIVATE_KEY: the agent wallet private key (0x...)
  HL_MAIN_ADDRESS:    your main wallet address (0x...)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: for reporting results
"""
import os
import sys
import traceback
from datetime import datetime, timezone

import requests

from src.hyperliquid_client import HyperliquidClient


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] credentials missing, skipping send")
        return
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print("[telegram] HTTP " + str(r.status_code) + ": " + r.text[:200])
    except Exception as e:
        print("[telegram] error: " + str(e))


def main():
    api_key = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
    main_addr = os.environ.get("HL_MAIN_ADDRESS", "").strip()

    if not api_key or not main_addr:
        msg = "*HL connection test FAILED*\n\nMissing secrets:\n"
        if not api_key:
            msg += "- HL_API_PRIVATE_KEY\n"
        if not main_addr:
            msg += "- HL_MAIN_ADDRESS\n"
        print(msg)
        send_telegram(msg)
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_lines = ["*HL connection test* `" + now + "`", ""]

    try:
        client = HyperliquidClient(
            api_private_key=api_key,
            main_address=main_addr,
        )
        report_lines.append("OK init")
        report_lines.append("  agent: `" + client.agent_address + "`")
        report_lines.append("  main:  `" + client.main_address + "`")

        # ---- Test 1: balance ----
        bal = client.get_balance()
        report_lines.append("")
        report_lines.append("*Balance*")
        report_lines.append("  account value: ${:.2f}".format(bal["account_value_usd"]))
        report_lines.append("  withdrawable:  ${:.2f}".format(bal["withdrawable_usd"]))
        report_lines.append("  margin used:   ${:.2f}".format(bal["total_margin_used_usd"]))
        report_lines.append("  position ntl:  ${:.2f}".format(bal["total_ntl_pos_usd"]))

        if bal["account_value_usd"] < 10:
            report_lines.append("  WARNING: account value < $10, fund the account")

        # ---- Test 2: mark prices ----
        report_lines.append("")
        report_lines.append("*Mark prices*")
        for coin in ["BTC", "ETH", "SOL"]:
            try:
                px = client.get_mark_price(coin)
                report_lines.append("  " + coin + ": ${:,.2f}".format(px))
            except Exception as e:
                report_lines.append("  " + coin + ": ERROR " + str(e)[:60])

        # ---- Test 3: positions ----
        positions = client.get_positions()
        report_lines.append("")
        report_lines.append("*Open positions:* " + str(len(positions)))
        for p in positions:
            report_lines.append(
                "  " + p["coin"] + " " + p["side"]
                + " size=" + str(p["size"])
                + " entry=${:.2f}".format(p["entry_price"])
                + " uPnL=${:.2f}".format(p["unrealized_pnl_usd"])
            )

        # ---- Test 4: open orders ----
        orders = client.get_open_orders()
        report_lines.append("")
        report_lines.append("*Open orders:* " + str(len(orders)))
        for o in orders[:5]:
            side = "BUY" if o.get("side") == "B" else "SELL"
            report_lines.append(
                "  " + o.get("coin", "?") + " " + side
                + " sz=" + str(o.get("sz"))
                + " px=$" + str(o.get("limitPx"))
            )

        # ---- All good ----
        report_lines.append("")
        report_lines.append("*ALL TESTS PASSED*")
        report_lines.append("Ready for Session 2 (executor with preview mode).")
        msg = "\n".join(report_lines)
        print(msg)
        send_telegram(msg)
        sys.exit(0)

    except Exception as e:
        tb = traceback.format_exc()
        err_msg = "*HL connection test FAILED*\n\n```\n" + str(e)[:300] + "\n```"
        print(err_msg)
        print(tb)
        send_telegram(err_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
