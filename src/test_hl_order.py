"""Test order placement on Hyperliquid (write test).

Places a tiny BUY limit order on BTC at -30% from mark (will NOT fill),
verifies it appears in open orders, then cancels it.

This validates that:
  1. The agent wallet is properly authorized to trade
  2. The unified account spot balance can serve as margin for perps
  3. Order/cancel API works end-to-end

Cost: ~$0. The order is too far below market to ever fill.
"""
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

import requests

from src.hyperliquid_client import HyperliquidClient


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        if len(text) > 3900:
            text = text[:3900] + "\n... (truncated)"
        requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print("[telegram] error: " + str(e))


def main():
    api_key = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
    main_addr = os.environ.get("HL_MAIN_ADDRESS", "").strip()

    if not api_key or not main_addr:
        send_telegram("Missing HL secrets")
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("[test_hl_order] start " + now)
    report = ["*HL ORDER TEST* `" + now + "`", ""]

    try:
        client = HyperliquidClient(
            api_private_key=api_key,
            main_address=main_addr,
        )

        # ---- Get BTC mark price ----
        mark = client.get_mark_price("BTC")
        print("BTC mark: $" + str(mark))
        report.append("BTC mark: ${:,.2f}".format(mark))

        # ---- Build a far-from-market BUY limit ----
        # 30% below mark, so it will not fill
        limit_px = round(mark * 0.70, 0)  # whole dollars for BTC

        # Size such that notional ~ $11 (just above $10 HL min)
        sz = round(11.0 / limit_px, 5)  # 5 decimals for BTC perp

        notional = sz * limit_px
        print("Test order: BUY " + str(sz) + " BTC @ $" + str(limit_px)
              + " (notional ${:.2f})".format(notional))
        report.append("Test order: BUY " + str(sz) + " BTC @ ${:,.2f}".format(limit_px))
        report.append("Notional: ${:.2f}".format(notional))
        report.append("")

        # ---- Place the order ----
        cloid_str = "0x" + uuid.uuid4().hex
        try:
            result = client.place_limit_order(
                coin="BTC",
                is_buy=True,
                sz=sz,
                limit_px=limit_px,
                reduce_only=False,
                cloid_str=cloid_str,
                post_only=False,  # Use Gtc so it can rest in the book
            )
            print("Order placement result:")
            print(json.dumps(result, indent=2, default=str))

            # Parse status
            status = result.get("status", "unknown")
            response = result.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])

            report.append("*Placement status:* " + str(status))

            if status == "ok" and statuses:
                first = statuses[0]
                if "resting" in first:
                    oid = first["resting"]["oid"]
                    report.append("OK ORDER PLACED, oid=" + str(oid))
                    print("Order placed, oid=" + str(oid))

                    # Wait briefly for it to appear
                    time.sleep(2)

                    # Verify in open orders
                    open_orders = client.get_open_orders()
                    found = [o for o in open_orders if o.get("oid") == oid]
                    if found:
                        report.append("OK Order visible in open_orders")
                        print("Order found in open_orders")
                    else:
                        report.append(
                            "WARNING Order placed but not visible in open_orders ("
                            + str(len(open_orders)) + " orders total)"
                        )

                    # Cancel
                    try:
                        cancel_result = client.cancel_order("BTC", oid)
                        print("Cancel result:")
                        print(json.dumps(cancel_result, indent=2, default=str))
                        cancel_status = cancel_result.get("status", "unknown")
                        report.append("*Cancel status:* " + str(cancel_status))
                        if cancel_status == "ok":
                            report.append("OK ORDER CANCELLED CLEAN")
                        else:
                            report.append("WARNING Cancel returned: " + str(cancel_result)[:200])
                    except Exception as ce:
                        report.append("ERROR cancel failed: " + str(ce)[:200])

                elif "filled" in first:
                    # Should not happen with our far-OTM price
                    report.append("UNEXPECTED Order filled immediately?!")
                    report.append("filled: " + json.dumps(first["filled"])[:200])
                elif "error" in first:
                    err = first["error"]
                    report.append("ERROR Order rejected: " + str(err)[:300])
                    print("Order rejected: " + str(err))
                else:
                    report.append("Unexpected status: " + str(first)[:300])
            else:
                # status != ok -> typically a margin error or auth error
                report.append("ERROR Placement failed")
                report.append("Full response: " + json.dumps(result)[:300])
                print("Placement failed:")
                print(json.dumps(result, indent=2))

        except Exception as oe:
            report.append("ERROR Exception placing order:")
            report.append("`" + str(oe)[:300] + "`")
            print("Exception during placement:")
            traceback.print_exc()

        report.append("")
        report.append("_Check workflow logs for full SDK response._")

        msg = "\n".join(report)
        print("\n========== Telegram message ==========")
        print(msg)
        send_telegram(msg)
        sys.exit(0)

    except Exception as e:
        tb = traceback.format_exc()
        err_msg = "*HL order test CRASHED*\n```\n" + str(e)[:400] + "\n```"
        print(err_msg)
        print(tb)
        send_telegram(err_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
