"""Telegram message sender — formatted signal output."""
import requests

from src.bankroll import BankrollState, SizingDecision
from src.setups import Signal

TIMEOUT = 10


def send_message(bot_token: str, chat_id: str, text: str,
                 parse_mode: str = "Markdown") -> None:
    if not bot_token or not chat_id:
        print("[telegram] missing creds, skipping send")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=TIMEOUT)
        if not r.ok:
            print(f"[telegram] error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[telegram] exception: {e}")


def format_signal(signal: Signal, sizing: SizingDecision,
                  bankroll: BankrollState) -> str:
    arrow = "🟢" if signal.direction == "long" else "🔴"
    pair = signal.pair.replace("/USDT:USDT", "/USDT")

    # Compute RR for display
    if signal.direction == "long":
        gross_target_pct = (signal.target_price - signal.limit_price) / signal.limit_price
    else:
        gross_target_pct = (signal.limit_price - signal.target_price) / signal.limit_price
    rr = gross_target_pct / signal.risk_distance_pct if signal.risk_distance_pct > 0 else 0

    risk_pct_of_bankroll = (sizing.risk_amount_usd / bankroll.equity_usd * 100
                            if bankroll.equity_usd > 0 else 0)

    return f"""{arrow} *SIGNAL — Setup A v2 ({signal.direction.upper()})*
`{pair}` (Binance Perp)

📊 *Conditions match*
• Extension: `{signal.extension_atr:+.2f} ATR`
• RSI 4h: `{signal.rsi:.1f}`
• ADX 4h: `{signal.adx:.1f}` (range)
• Funding 8h: `{signal.funding_rate * 100:+.4f}%`
• Prix actuel: `${signal.current_price:.4f}`

📋 *ORDRES À PLACER*
🎯 *LIMIT {signal.direction.upper()}*: `${signal.limit_price:.4f}`
🛑 *STOP*: `${signal.stop_price:.4f}`
✅ *TARGET (LIMIT)*: `${signal.target_price:.4f}`

💰 *Sizing (auto, risk-based)*
• Notional: `${sizing.notional_usd:,.0f}`
• Quantité: `{sizing.qty:.6f}`
• Levier impliqué: `{sizing.leverage_implied:.2f}x`
• Risque: `${sizing.risk_amount_usd:.2f}` (`{risk_pct_of_bankroll:.2f}%` bankroll)
• RR théorique: `{rr:.2f}`

⏰ *Validité*
• Annule LIMIT si non rempli après 8h
• Time-stop position 12h après fill

📈 *Bankroll*
• Equity: `${bankroll.equity_usd:,.2f}` (peak `${bankroll.peak_equity_usd:,.2f}`)
• Daily P&L: `${bankroll.daily_pnl_usd:+,.2f}`
• Positions: `{len(bankroll.open_positions)}`

🆔 `{signal.signal_id}`
"""


def format_rejected(signal: Signal, sizing: SizingDecision) -> str:
    arrow = "🟡"
    pair = signal.pair.replace("/USDT:USDT", "/USDT")
    return f"""{arrow} *Signal détecté — REJETÉ par bankroll*
`{pair}` {signal.direction.upper()}
Raison: `{sizing.reason}`
🆔 `{signal.signal_id}`
"""


def format_halt(reason: str) -> str:
    return (
        f"🛑 *BOT HALTED*\n"
        f"Raison: `{reason}`\n\n"
        f"Action requise : reset manuel via workflow `bankroll_reset`."
    )


def format_error(pair: str, error: str) -> str:
    return f"⚠️ *Scanner error*\n`{pair}`: `{error}`"


def format_heartbeat(scanned_pairs: list) -> str:
    return (
        f"ℹ️ Scan OK — aucun signal\n"
        f"Paires: `{', '.join(scanned_pairs)}`"
    )
