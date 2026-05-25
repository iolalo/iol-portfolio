import os
import json
import requests
from datetime import datetime, timezone, timedelta

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

ART = timezone(timedelta(hours=-3))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


def main():
    with open("data/portfolio.json", encoding="utf-8") as f:
        data = json.load(f)

    positions = data["positions"]
    total = data["total_ars"]
    alerts = [p for p in positions if p["recommendation"] in ("COMPRAR", "VENDER", "ALERTA")]
    emoji = {"COMPRAR": "🟢", "VENDER": "🔴", "ALERTA": "⚠️", "MANTENER": "⚪"}

    lines = [
        "📊 *IOL Portfolio — Resumen diario*",
        f"💰 Total: ${total:,.0f} ARS",
        "",
    ]

    if alerts:
        lines.append("*Señales activas:*")
        for a in alerts:
            e = emoji.get(a["recommendation"], "⚪")
            lines.append(f"{e} *{a['symbol']}* — {a['recommendation']}")
            lines.append(f"  Precio: ${a['unit_price']:,.0f} | Día: {a['daily_change_pct']:+.2f}%")
            if a["rsi"] is not None and a["ma20"] is not None:
                lines.append(f"  RSI: {a['rsi']} | MA20: ${a['ma20']:,.0f}")
        lines.append("")
    else:
        lines.append("✅ Sin señales — mercado tranquilo")
        lines.append("")

    lines.append("*Posiciones:*")
    for p in positions:
        gp_sign = "+" if p["gain_pct"] > 0 else ""
        lines.append(
            f"  {p['symbol']}: ${p['unit_price']:,.0f} ({p['daily_change_pct']:+.2f}% hoy, {gp_sign}{p['gain_pct']:.2f}% G/P)"
        )

    try:
        with open("data/trades_log.json", encoding="utf-8") as f:
            raw = json.load(f)
            trades = raw if isinstance(raw, list) else raw.get("trades", [])
        today = datetime.now(ART).strftime("%Y-%m-%d")
        today_trades = [
            t for t in trades
            if t.get("date", "").startswith(today)
            and t.get("status") == "executed"
        ]
        if today_trades:
            lines.append("\n*Operaciones ejecutadas hoy:*")
            for t in today_trades:
                icon = "🟢" if t["side"] == "buy" else "🔴"
                op = "COMPRA" if t["side"] == "buy" else "VENTA"
                status = f"#{t['order_id']}" if t.get("order_id") else "pendiente"
                lines.append(f"  {icon} {op} {t['quantity']}x {t['symbol']} @ ${t['price']:,.0f} ({status})")
    except FileNotFoundError:
        pass

    lines.append(f"\n_Datos al: {data['last_updated']}_")
    send_telegram("\n".join(lines))
    print("Summary sent to Telegram.")


if __name__ == "__main__":
    main()