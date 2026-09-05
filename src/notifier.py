"""
Alert delivery — Telegram if configured, console otherwise.
"""
import os
import requests


def format_alert(pair: dict, score_result: dict, sizing: dict, exits: dict) -> str:
    base = pair.get("baseToken") or {}
    symbol = base.get("symbol", "?")
    mint = base.get("address", "")
    price = float(pair.get("priceUsd") or 0)
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)

    top = sorted(score_result["breakdown"].items(), key=lambda kv: -kv[1][1])[:3]
    drivers = ", ".join(f"{n.replace('_', ' ')} {w}" for n, (_, w) in top)

    tps = " / ".join(
        f"{tp['multiple']}x sell {int(tp['sell_fraction'] * 100)}%"
        for tp in exits["take_profits"]
    )

    return "\n".join([
        f"🎯 {symbol} — score {score_result['score']}/100 (tier {sizing.get('tier', '-')})",
        "",
        f"Price:  ${price:.10f}".rstrip("0"),
        f"Liq:    ${liq:,.0f}   MCap: ${mcap:,.0f}",
        f"Drivers: {drivers}",
        "",
        f"SIZE:   ${sizing['amount_usd']:,.2f} ({sizing['fraction'] * 100:.2f}% of bankroll)",
        f"STOP:   ${exits['stop_loss']:.10f}".rstrip("0"),
        f"TAKE:   {tps}",
        f"TIME STOP: {exits['time_stop_hours']}h if no move",
        "",
        f"Mint: {mint}",
        f"Chart: https://dexscreener.com/solana/{mint}",
        "",
        "⚠️ Paper trade first. Verify before buying.",
    ])


def send(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("\n" + "=" * 55)
        print(message)
        print("=" * 55 + "\n")
        return True

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except requests.Timeout:
        print("[notifier] Telegram timed out — check network or VPN")
        print(message)
        return False

    except requests.RequestException as e:
        print(f"[notifier] Telegram send failed: {e}")
        print(message)
        return False