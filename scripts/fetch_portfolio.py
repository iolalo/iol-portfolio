import os
import json
import requests
from datetime import datetime, timedelta

IOL_BASE = "https://api.invertironline.com"
IOL_USER = os.environ["IOL_USERNAME"]
IOL_PASS = os.environ["IOL_PASSWORD"]


def get_token():
    resp = requests.post(
        f"{IOL_BASE}/token",
        data={"username": IOL_USER, "password": IOL_PASS, "grant_type": "password"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def iol_get(token, path):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{IOL_BASE}{path}", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_historical(token, symbol, days=60):
    today = datetime.now()
    date_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    path = f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas/ajustada/{date_from}/{date_to}/dia"
    try:
        return iol_get(token, path)
    except Exception:
        return []


def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def calculate_ma(prices, period=20):
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)


def get_recommendation(rsi, price, ma20, daily_pct):
    if daily_pct <= -5:
        return "ALERTA", [f"Caída brusca {daily_pct:+.2f}% hoy"]

    signals = []
    votes = {"COMPRAR": 0, "VENDER": 0}

    if rsi is not None:
        if rsi < 30:
            votes["COMPRAR"] += 2
            signals.append(f"RSI sobrevendido ({rsi})")
        elif rsi > 70:
            votes["VENDER"] += 2
            signals.append(f"RSI sobrecomprado ({rsi})")

    if ma20 is not None:
        if price < ma20 * 0.97:
            votes["VENDER"] += 1
            signals.append(f"Precio 3%+ bajo MA20 (${ma20:,.0f})")
        elif price > ma20 * 1.03:
            votes["COMPRAR"] += 1
            signals.append(f"Precio 3%+ sobre MA20 (${ma20:,.0f})")

    if votes["COMPRAR"] > votes["VENDER"]:
        return "COMPRAR", signals
    if votes["VENDER"] > votes["COMPRAR"]:
        return "VENDER", signals
    return "MANTENER", signals


def main():
    token = get_token()
    raw = iol_get(token, "/api/v2/portafolio/argentina")
    activos = raw.get("activos", raw.get("positions", []))

    positions = []
    alerts = []

    for pos in activos:
        titulo = pos.get("titulo", pos.get("asset", {}))
        symbol = titulo.get("simbolo", titulo.get("symbol", ""))
        description = titulo.get("descripcion", titulo.get("description", ""))
        currency = titulo.get("moneda", titulo.get("currency", "ARS"))
        quantity = pos.get("cantidad", pos.get("quantity", 0))
        price = pos.get("ultimoPrecio", pos.get("unit_price", 0))
        daily_pct = pos.get("variacion", pos.get("daily_change_pct", 0))
        ppc = pos.get("ppc", price)
        gain_pct = pos.get("gananciaPorcentaje", 0)

        hist = get_historical(token, symbol)
        closes = [h.get("cierre", h.get("close", 0)) for h in hist if h.get("cierre", h.get("close"))]
        closes.append(price)

        rsi = calculate_rsi(closes)
        ma20 = calculate_ma(closes)
        recommendation, signals = get_recommendation(rsi, price, ma20, daily_pct)

        data = {
            "symbol": symbol,
            "description": description,
            "quantity": quantity,
            "unit_price": price,
            "total_value": round(quantity * price, 2),
            "currency": currency,
            "ppc": ppc,
            "gain_pct": gain_pct,
            "daily_change_pct": daily_pct,
            "rsi": rsi,
            "ma20": ma20,
            "recommendation": recommendation,
            "signals": signals,
            "sparkline": closes[-30:],
        }
        positions.append(data)
        if recommendation in ("COMPRAR", "VENDER", "ALERTA"):
            alerts.append(data)

    total_ars = sum(p["total_value"] for p in positions if p["currency"] == "ARS")

    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_ars": total_ars,
        "alert_count": len(alerts),
        "positions": positions,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/portfolio.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(alerts)} alerts. Total ARS: {total_ars:,.0f}")


if __name__ == "__main__":
    main()
