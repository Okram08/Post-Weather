"""Read-only test of Hyperliquid connection.

v2: dumps full user_state JSON to debug unified account balance issue.
"""
import json
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
        return
    try:
        # Telegram has a 4096 char limit per message, so truncate
        if len(text) > 3900:
            text = text[:3900] + "\n... (truncated)"
        r = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
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
        msg = "Missing HL secrets"
        print(msg)
        send_telegram(msg)
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("[test_hl] start " + now)

    try:
        client = HyperliquidClient(
            api_private_key=api_key,
            main_address=main_addr,
        )

        # ===== Dump full user_state =====
        print("\n========== FULL user_state ==========")
        state = client.get_user_state()
        print(json.dumps(state, indent=2, default=str))

        # ===== Dump spot_user_state =====
        print("\n========== FULL spot_user_state ==========")
        try:
            spot_state = client.info.spot_user_state(client.main_address)
            print(json.dumps(spot_state, indent=2, default=str))
        except Exception as e:
            print("spot_user_state error: " + str(e))

        # ===== Try perp meta =====
        print("\n========== meta (perp dex) ==========")
        try:
            meta = client.info.meta()
            # Just universe length, not the whole list
            print("Number of perp markets: " + str(len(meta.get("universe", []))))
        except Exception as e:
            print("meta error: " + str(e))

        # ===== Mark prices =====
        print("\n========== Mark prices ==========")
        for coin in ["BTC", "ETH", "SOL"]:
            try:
                px = client.get_mark_price(coin)
                print("  " + coin + ": $" + str(px))
            except Exception as e:
                print("  " + coin + ": ERROR " + str(e))

        # ===== Build Telegram report =====
        margin = state.get("marginSummary", {})
        cross_margin = state.get("crossMarginSummary", {})

        # Try every possible field for the balance
        report_lines = [
            "*HL state dump* `" + now + "`",
            "",
            "*marginSummary:*",
            "  accountValue: $" + str(margin.get("accountValue", "?")),
            "  totalNtlPos: $" + str(margin.get("totalNtlPos", "?")),
            "  totalRawUsd: $" + str(margin.get("totalRawUsd", "?")),
            "  totalMarginUsed: $" + str(margin.get("totalMarginUsed", "?")),
            "",
            "*crossMarginSummary:*",
            "  accountValue: $" + str(cross_margin.get("accountValue", "?")),
            "  totalRawUsd: $" + str(cross_margin.get("totalRawUsd", "?")),
            "",
            "*Top-level fields:*",
            "  withdrawable: $" + str(state.get("withdrawable", "?")),
            "  time: " + str(state.get("time", "?")),
            "  assetPositions count: " + str(len(state.get("assetPositions", []))),
        ]

        # Spot balances
        try:
            spot_state = client.info.spot_user_state(client.main_address)
            spot_balances = spot_state.get("balances", [])
            report_lines.append("")
            report_lines.append("*Spot balances:* " + str(len(spot_balances)) + " entries")
            for b in spot_balances[:10]:
                report_lines.append(
                    "  " + str(b.get("coin", "?")) + ": "
                    + str(b.get("total", "?")) + " (hold: "
                    + str(b.get("hold", "0")) + ")"
                )
        except Exception as e:
            report_lines.append("  spot_user_state ERROR: " + str(e)[:100])

        # Mark prices
        report_lines.append("")
        report_lines.append("*Mark prices:*")
        for coin in ["BTC", "ETH", "SOL"]:
            try:
                px = client.get_mark_price(coin)
                report_lines.append("  " + coin + ": ${:,.2f}".format(px))
            except Exception as e:
                report_lines.append("  " + coin + ": ERROR")

        report_lines.append("")
        report_lines.append("_Full JSON in workflow logs_")

        msg = "\n".join(report_lines)
        print("\n========== Telegram message ==========")
        print(msg)
        send_telegram(msg)
        sys.exit(0)

    except Exception as e:
        tb = traceback.format_exc()
        err_msg = "*HL test FAILED*\n```\n" + str(e)[:500] + "\n```"
        print(err_msg)
        print(tb)
        send_telegram(err_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
