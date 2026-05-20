import os
import json
import requests
from datetime import datetime, timedelta, timezone

IOL_BASE = "https://api.invertironline.com"
IOL_USER = os.environ["IOL_USERNAME"]
IOL_PASS = os.environ["IOL_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

ART = timezone(timedelta(hours=-3))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_json_config(filename):
    path = os.path.join(SCRIPT_DIR, filename)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


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
    resp = requests.get(f"{IOL_BASE}{path}", headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_historical(token, symbol, from_date, to_date):
    """Fetch daily OHLCV from IOL and return list of {date, close} dicts."""
    date_from = from_date.strftime("%Y-%m-%d")
    date_to   = to_date.strftime("%Y-%m-%d")
    path = (
        f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas"
        f"/ajustada/{date_from}/{date_to}/dia"
    )
    try:
        raw = iol_get(token, path)
    except Exception as e:
        print(f"  [WARN] Historical API error for {symbol}: {e}")
        return []

    # Unwrap if the response is a dict with a list inside
    if isinstance(raw, dict):
        for key in ("historico", "data", "series", "items", "values", "candles", "bars"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
        else:
            print(f"  [WARN] Unexpected historical response for {symbol}: keys={list(raw.keys())}")
            return []

    if not isinstance(raw, list):
        return []

    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Close price – try Spanish and English field names
        close = item.get("cierre") or item.get("close") or item.get("ultimo")
        if not close:
            continue
        # Date – ISO string or Unix timestamp
        fecha = item.get("fechaHora") or item.get("fecha") or item.get("date") or item.get("time")
        if isinstance(fecha, str):
            date_str = fecha[:10]
        elif isinstance(fecha, (int, float)):
            date_str = datetime.fromtimestamp(fecha, tz=ART).strftime("%Y-%m-%d")
        else:
            date_str = ""
        result.append({"date": date_str, "close": round(float(close), 2)})

    # Sort by date ascending
    result.sort(key=lambda x: x["date"])
    return result


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
    if daily_pct is not None and daily_pct <= -5:
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
            votes["COMPRAR"] += 1
            signals.append(f"Precio 3%+ bajo MA20 (${ma20:,.0f})")
        elif price > ma20 * 1.03:
            votes["VENDER"] += 1
            signals.append(f"Precio 3%+ sobre MA20 (${ma20:,.0f})")
    if votes["COMPRAR"] > votes["VENDER"]:
        return "COMPRAR", signals
    if votes["VENDER"] > votes["COMPRAR"]:
        return "VENDER", signals
    return "MANTENER", signals


def analyze_ticker(token, symbol, current_price, daily_pct, buy_date, today):
    """Return (rsi, ma20, rec, signals, sparkline, full_history) for any symbol."""
    hist_from = buy_date if buy_date else (today - timedelta(days=90))
    hist = get_historical(token, symbol, hist_from, today)

    closes = [h["close"] for h in hist]
    dates  = [h["date"]  for h in hist]

    # Append today's price if missing
    today_str = today.strftime("%Y-%m-%d")
    if not dates or dates[-1] != today_str:
        closes.append(current_price)
        dates.append(today_str)

    rsi  = calculate_rsi(closes)
    ma20 = calculate_ma(closes)
    recommendation, signals = get_recommendation(rsi, current_price, ma20, daily_pct)

    sparkline    = [{"date": d, "close": c} for d, c in zip(dates[-30:], closes[-30:])]
    full_history = [{"date": d, "close": c} for d, c in zip(dates, closes)]

    return rsi, ma20, recommendation, signals, sparkline, full_history


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")


def main():
    now_art = datetime.now(ART)
    today   = now_art.replace(tzinfo=None)

    buy_dates = load_json_config("buy_dates.json")
    watchlist = load_json_config("watchlist.json")
    if isinstance(watchlist, dict):
        watchlist = list(watchlist.keys())
    if not isinstance(watchlist, list):
        watchlist = []

    print("Authenticating...")
    token = get_token()

    print("Fetching portfolio...")
    raw     = iol_get(token, "/api/v2/portafolio/argentina")
    activos = raw.get("activos", raw.get("positions", []))

    positions        = []
    alerts           = []
    portfolio_syms   = set()

    for pos in activos:
        try:
            titulo      = pos.get("titulo", pos.get("asset", {}))
            symbol      = titulo.get("simbolo", titulo.get("symbol", ""))
            description = titulo.get("descripcion", titulo.get("description", symbol))
            # Currency can be in "moneda" (Spanish API) or "currency" (English wrapper)
            currency    = titulo.get("moneda", titulo.get("currency", "ARS"))
            quantity    = pos.get("cantidad",    pos.get("quantity", 0))
            price       = pos.get("ultimoPrecio", pos.get("unit_price", pos.get("lot_price", 0)))
            daily_pct   = pos.get("variacion",   pos.get("daily_change_pct", 0)) or 0
            ppc         = pos.get("ppc", price)
            gain_pct    = pos.get("gananciaPorcentaje", pos.get("gain_pct", 0)) or 0

            if gain_pct == 0 and ppc and ppc > 0:
                gain_pct = round((price - ppc) / ppc * 100, 2)

            buy_date_str = buy_dates.get(symbol)
            buy_date     = datetime.strptime(buy_date_str, "%Y-%m-%d") if buy_date_str else None

            print(f"  Analyzing {symbol} (precio={price}, ppc={ppc})...")
            rsi, ma20, rec, signals, sparkline, full_history = analyze_ticker(
                token, symbol, price, daily_pct, buy_date, today
            )

            data = {
                "symbol":           symbol,
                "description":      description,
                "quantity":         quantity,
                "unit_price":       price,
                "total_value":      round(quantity * price, 2),
                "currency":         currency,
                "ppc":              ppc,
                "gain_pct":         gain_pct,
                "daily_change_pct": daily_pct,
                "buy_date":         buy_date_str,
                "rsi":              rsi,
                "ma20":             ma20,
                "recommendation":   rec,
                "signals":          signals,
                "sparkline":        sparkline,
                "full_history":     full_history,
            }
            positions.append(data)
            portfolio_syms.add(symbol)
            if rec in ("COMPRAR", "VENDER", "ALERTA"):
                alerts.append(data)

        except Exception as e:
            print(f"  [ERROR] Processing position: {e}")

    # Portfolio totals — no currency filter (all Argentine positions are ARS)
    total_ars  = sum(p["total_value"] for p in positions)
    invested   = sum(p["ppc"] * p["quantity"] for p in positions)
    total_gain = round(total_ars - invested, 2)
    gain_pct_total = round((total_gain / invested * 100) if invested > 0 else 0, 2)

    # Watchlist — analyze extra tickers not already in portfolio
    watchlist_data = []
    for symbol in watchlist:
        if symbol in portfolio_syms:
            continue
        try:
            buy_date_str = buy_dates.get(symbol)
            buy_date     = datetime.strptime(buy_date_str, "%Y-%m-%d") if buy_date_str else None
            hist_from    = buy_date if buy_date else (today - timedelta(days=90))
            hist         = get_historical(token, symbol, hist_from, today)

            if len(hist) < 2:
                print(f"  [SKIP] Insufficient data for {symbol}")
                continue

            price     = hist[-1]["close"]
            prev      = hist[-2]["close"]
            daily_pct = round((price - prev) / prev * 100, 2) if prev else 0

            print(f"  Analyzing watchlist {symbol} (precio={price})...")
            rsi, ma20, rec, signals, sparkline, full_history = analyze_ticker(
                token, symbol, price, daily_pct, buy_date, today
            )

            wdata = {
                "symbol":           symbol,
                "description":      symbol,
                "unit_price":       price,
                "daily_change_pct": daily_pct,
                "buy_date":         buy_date_str,
                "rsi":              rsi,
                "ma20":             ma20,
                "recommendation":   rec,
                "signals":          signals,
                "sparkline":        sparkline,
                "full_history":     full_history,
            }
            watchlist_data.append(wdata)
            if rec in ("COMPRAR", "VENDER", "ALERTA"):
                alerts.append(wdata)

        except Exception as e:
            print(f"  [ERROR] Watchlist {symbol}: {e}")

    output = {
        "last_updated":     now_art.strftime("%Y-%m-%d %H:%M"),
        "total_ars":        round(total_ars, 2),
        "invested":         round(invested, 2),
        "total_gain":       total_gain,
        "total_gain_pct":   gain_pct_total,
        "alert_count":      len(alerts),
        "positions":        positions,
        "watchlist":        watchlist_data,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/portfolio.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("Saved data/portfolio.json")

    # ── Telegram ─────────────────────────────────────────────────────────────
    emoji      = {"COMPRAR": "🟢", "VENDER": "🔴", "ALERTA": "⚠️", "MANTENER": "⚪"}
    gain_emoji = "📈" if total_gain >= 0 else "📉"

    summary = (
        f"📊 *IOL Portfolio — {now_art.strftime('%d/%m/%Y %H:%M')} ART*\n\n"
        f"💰 Total: *${total_ars:,.0f}*\n"
        f"💼 Invertido: ${invested:,.0f}\n"
        f"{gain_emoji} Resultado: ${total_gain:+,.0f} ({gain_pct_total:+.2f}%)\n"
    )

    if alerts:
        port_alerts  = [a for a in alerts if "quantity" in a]
        watch_alerts = [a for a in alerts if "quantity" not in a]
        lines = [summary, "🔔 *Alertas activas:*\n"]
        for a in port_alerts + watch_alerts:
            e = emoji.get(a["recommendation"], "⚪")
            lines.append(f"{e} *{a['symbol']}* — {a['recommendation']}")
            lines.append(
                f"Precio: ${a['unit_price']:,.0f} | "
                f"Día: {a['daily_change_pct']:+.2f}%"
            )
            if a["rsi"] is not None and a["ma20"] is not None:
                lines.append(f"RSI: {a['rsi']} | MA20: ${a['ma20']:,.0f}")
            for s in a["signals"]:
                lines.append(f"  • {s}")
            lines.append("")
        send_telegram("\n".join(lines))
    else:
        send_telegram(summary + "✅ Sin alertas activas")

    print(
        f"Done. {len(alerts)} alerts. "
        f"Total: ${total_ars:,.0f} | Ganancia: ${total_gain:+,.0f} ({gain_pct_total:+.2f}%)"
    )


if __name__ == "__main__":
    main()
