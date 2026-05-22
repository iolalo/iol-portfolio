import os
import json
import requests
from datetime import datetime, timedelta

IOL_BASE = "https://api.invertironline.com"
IOL_USER = os.environ["IOL_USERNAME"]
IOL_PASS = os.environ["IOL_PASSWORD"]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Universe of tradeable assets (BYMA acciones + CEDEARs)
UNIVERSE = [
    # Bancos
    "GGAL", "BBAR", "BMA", "SUPV", "BPAT",
    # Energía
    "PAMP", "CEPU", "TGNO4", "TRAN", "YPFD",
    # Industria / commodities
    "TXAR", "ALUA", "CRES", "COME", "LOMA",
    # Telecom / otros
    "TECO2", "MIRG", "MORI",
    # CEDEARs (top tickers, se negocian en bCBA en ARS)
    "AAPL", "GOOGL", "AMZN", "MSFT", "NVDA", "KO", "XOM", "META",
]

# Risk parameters
MAX_POSITION_PCT  = 0.10   # max 10% del portafolio por operación
MIN_CASH_RESERVE  = 0.10   # reserva mínima 10% en cash
MAX_DAILY_TRADES  = 3      # máximo operaciones por día
COOLDOWN_DAYS     = 5      # días sin operar el mismo símbolo tras una operación

# Sell thresholds (cualquier condición es suficiente)
RSI_OVERBOUGHT    = 72
PRICE_ABOVE_MA    = 1.05   # precio ≥ 5% sobre MA20
STOP_LOSS_DAILY   = -7.0   # caída diaria ≥ 7%
TRAILING_STOP     = -15.0  # pérdida total ≥ 15% desde PPC
TAKE_PROFIT_RSI   = 65     # RSI al tomar ganancias
TAKE_PROFIT_GP    = 30.0   # ganancia ≥ 30% desde PPC

# Buy scoring (necesita score ≥ 2 para comprar)
RSI_OVERSOLD      = 35
PRICE_BELOW_MA    = 0.97   # precio ≥ 3% bajo MA20
MIN_BUY_SCORE     = 2


# ── IOL API helpers ──────────────────────────────────────────────────────────

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


def iol_post(token, path, body):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(f"{IOL_BASE}{path}", headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_historical_iol(token, symbol, days=60):
    today = datetime.now()
    date_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    candidates = [
        f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas/ajustada/{date_from}/{date_to}/Dia",
        f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas/sinAjustar/{date_from}/{date_to}/Dia",
        f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas/ajustada/{date_from}/{date_to}/dia",
    ]
    for path in candidates:
        try:
            data = iol_get(token, path)
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict):
                for key in ("seriesHistoricas", "bars", "data", "valores"):
                    if key in data and data[key]:
                        return data[key]
        except Exception:
            continue
    return None


# CEDEARs en BCBA mapean al ticker US en Yahoo Finance
_CEDEAR_MAP = {
    "AAPL": "AAPL", "GOOGL": "GOOGL", "AMZN": "AMZN", "MSFT": "MSFT",
    "NVDA": "NVDA", "KO": "KO", "XOM": "XOM", "META": "META",
}

def get_historical_yfinance(symbol, days=60):
    try:
        import yfinance as yf
        # Para CEDEARs usar ticker US, para acciones locales agregar .BA
        yf_ticker = _CEDEAR_MAP.get(symbol, f"{symbol}.BA")
        data = yf.download(yf_ticker, period=f"{days}d", interval="1d",
                           progress=False, auto_adjust=True)
        if data.empty:
            return None
        col = data["Close"]
        if hasattr(col, "squeeze"):
            col = col.squeeze()
        closes = [float(x) for x in col.dropna().values]
        return [{"cierre": c} for c in closes]
    except Exception as e:
        print(f"  Warning: yfinance {symbol} falló — {e}")
        return None


def get_historical(token, symbol, days=60):
    data = get_historical_iol(token, symbol, days)
    if data:
        return data
    data = get_historical_yfinance(symbol, days)
    if data:
        return data
    print(f"  Warning: histórico {symbol} — IOL y yfinance fallaron")
    return []


# ── Technical indicators ─────────────────────────────────────────────────────

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


# ── Signal logic ─────────────────────────────────────────────────────────────

def check_sell(rsi, price, ma20, daily_pct, gain_pct):
    reasons = []
    if daily_pct is not None and daily_pct <= STOP_LOSS_DAILY:
        reasons.append(f"Stop loss diario ({daily_pct:+.2f}%)")
    if gain_pct is not None and gain_pct <= TRAILING_STOP:
        reasons.append(f"Trailing stop ({gain_pct:+.2f}% desde PPC)")
    if (rsi is not None and rsi >= RSI_OVERBOUGHT
            and ma20 is not None and price >= ma20 * PRICE_ABOVE_MA):
        reasons.append(f"Sobrecomprado: RSI={rsi}, precio {((price/ma20)-1)*100:.1f}% sobre MA20")
    if (gain_pct is not None and gain_pct >= TAKE_PROFIT_GP
            and rsi is not None and rsi >= TAKE_PROFIT_RSI):
        reasons.append(f"Toma de ganancias: G/P={gain_pct:+.2f}%, RSI={rsi}")
    return len(reasons) > 0, reasons


def score_buy(rsi, price, ma20, daily_pct):
    score = 0
    reasons = []
    if rsi is not None and rsi < RSI_OVERSOLD:
        score += 2
        reasons.append(f"RSI sobrevendido ({rsi})")
    if ma20 is not None and price < ma20 * PRICE_BELOW_MA:
        score += 1
        reasons.append(f"Precio {((price/ma20)-1)*100:.1f}% bajo MA20 (${ma20:,.0f})")
    if daily_pct is not None and -3 < daily_pct < -0.5:
        score += 1
        reasons.append(f"Corrección ({daily_pct:+.2f}%) — potencial rebote")
    return score, reasons


# ── Trade log ────────────────────────────────────────────────────────────────

def load_trades_log():
    try:
        with open("data/trades_log.json", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"trades": data}
            return data
    except FileNotFoundError:
        return {"trades": []}


def save_trades_log(log):
    os.makedirs("data", exist_ok=True)
    with open("data/trades_log.json", "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_cooldown_symbols(log):
    cutoff = (datetime.now() - timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
    return {t["symbol"] for t in log["trades"] if t["date"] >= cutoff}


def count_today_trades(log):
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for t in log["trades"] if t["date"] == today and not t.get("dry_run"))


# ── Order execution ──────────────────────────────────────────────────────────

def place_order(token, symbol, action, quantity, price, reason, log):
    today = datetime.now().strftime("%Y-%m-%d")
    entry = {
        "date": today,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": round(price, 2),
        "total": round(quantity * price, 2),
        "reason": reason,
        "order_id": None,
        "error": None,
        "dry_run": DRY_RUN,
    }
    tag = "[DRY RUN] " if DRY_RUN else ""
    print(f"{tag}{action.upper()} {quantity}x {symbol} @ ${price:,.0f} — {reason}")

    if not DRY_RUN:
        try:
            limit = price * 1.01 if action == "compra" else price * 0.99
            body = {
                "mercado":   "bCBA",
                "simbolo":   symbol,
                "cantidad":  quantity,
                "precio":    round(limit, 2),
                "validez":   "HoyHasta",
                "tipo":      "precioLimite",
                "plazo":     "t1",
                "operacion": action,
                "monto":     None,
            }
            # Step 1: validate → get validacionId
            val = iol_post(token, "/api/v2/operaciones/Validar", body)
            validation_id = (
                val.get("validacionId") or val.get("validation_id") or val.get("id")
            )
            if not validation_id:
                raise ValueError(f"Validate returned no validacionId: {val}")
            # Step 2: execute with validacionId in path
            result = iol_post(token, f"/api/v2/operaciones/{validation_id}", body)
            entry["order_id"] = str(result.get("id", result.get("numeroOperacion", "?")))
            print(f"  → orden #{entry['order_id']} enviada")
        except Exception as e:
            entry["error"] = str(e)
            print(f"  → ERROR: {e}")

    log["trades"].append(entry)
    return entry


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"Trade bot [{mode}] — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    token = get_token()

    # Current portfolio
    raw = iol_get(token, "/api/v2/portafolio/argentina")
    activos = raw.get("activos", raw.get("positions", []))

    # Cash balance
    cash_ars = 0
    try:
        balance = iol_get(token, "/api/v2/estadocuenta")
        # Real API response: {"arg_ars": {"available_for_withdrawal": N, "available": {"t0": N, "t1": N}}}
        arg_ars = balance.get("arg_ars", {})
        if arg_ars:
            withdrawal = arg_ars.get("available_for_withdrawal")
            if withdrawal is not None:
                cash_ars = float(withdrawal)
            else:
                avail = arg_ars.get("available", {})
                cash_ars = float(avail.get("t0") or avail.get("t1") or 0)
    except Exception as e:
        print(f"Warning: no pudo obtener cash balance — {e}")

    # Build position map
    positions = []
    current_symbols = set()
    portfolio_value = cash_ars

    for pos in activos:
        titulo = pos.get("titulo", pos.get("asset", {}))
        symbol  = titulo.get("simbolo", titulo.get("symbol", ""))
        qty     = pos.get("cantidad", pos.get("quantity", 0))
        price   = pos.get("ultimoPrecio", pos.get("unit_price", 0))
        daily   = pos.get("variacion", pos.get("daily_change_pct", 0))
        ppc     = pos.get("ppc", 0)
        gain    = pos.get("gananciaPorcentaje", 0)
        if ppc and ppc > 0 and price > 0:
            gain = round((price - ppc) / ppc * 100, 2)
        value   = qty * price
        portfolio_value += value
        current_symbols.add(symbol)
        positions.append({
            "symbol": symbol,
            "quantity": qty,
            "price": price,
            "value": value,
            "daily_pct": daily,
            "gain_pct": gain,
        })

    max_per_trade  = portfolio_value * MAX_POSITION_PCT
    min_cash_hold  = portfolio_value * MIN_CASH_RESERVE

    print(f"Portfolio: ${portfolio_value:,.0f} ARS | Cash: ${cash_ars:,.0f} | Max/trade: ${max_per_trade:,.0f}")

    log            = load_trades_log()
    cooldown_syms  = get_cooldown_symbols(log)
    trades_today   = count_today_trades(log)

    sell_queue    = []
    buy_candidates = []

    # ── Analyze current positions for sells ──────────────────────────────────
    for pos in positions:
        sym = pos["symbol"]
        if sym in cooldown_syms:
            print(f"  {sym}: cooldown activo — skip")
            continue
        hist   = get_historical(token, sym)
        closes = [h.get("cierre", h.get("close", 0)) for h in hist if h.get("cierre", h.get("close"))]
        closes.append(pos["price"])
        rsi  = calculate_rsi(closes)
        ma20 = calculate_ma(closes)
        should_sell, reasons = check_sell(rsi, pos["price"], ma20, pos["daily_pct"], pos["gain_pct"])
        label = f"VENDER: {reasons[0]}" if should_sell else "mantener"
        print(f"  {sym}: precio=${pos['price']:,.0f} RSI={rsi} MA20={ma20} → {label}")
        if should_sell:
            sell_queue.append({**pos, "rsi": rsi, "ma20": ma20, "reasons": reasons})

    # ── Scan universe for buy opportunities ──────────────────────────────────
    to_scan = [s for s in UNIVERSE if s not in current_symbols and s not in cooldown_syms]
    for sym in to_scan:
        try:
            hist = get_historical(token, sym)
            if not hist:
                continue
            closes = [h.get("cierre", h.get("close", 0)) for h in hist if h.get("cierre", h.get("close"))]
            if len(closes) < 15:
                continue
            current_price = closes[-1]
            daily_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
            rsi  = calculate_rsi(closes)
            ma20 = calculate_ma(closes)
            score, reasons = score_buy(rsi, current_price, ma20, daily_pct)
            print(f"  {sym}: ${current_price:,.0f} RSI={rsi} score={score}")
            if score >= MIN_BUY_SCORE:
                buy_candidates.append({
                    "symbol": sym,
                    "price": current_price,
                    "rsi": rsi,
                    "ma20": ma20,
                    "score": score,
                    "reasons": reasons,
                })
        except Exception as e:
            print(f"  {sym}: error — {e}")

    buy_candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── Execute sells ────────────────────────────────────────────────────────
    sell_proceeds = 0
    for pos in sell_queue:
        if trades_today >= MAX_DAILY_TRADES:
            print(f"Límite diario de operaciones ({MAX_DAILY_TRADES}) alcanzado")
            break
        reason = " | ".join(pos["reasons"])
        entry = place_order(token, pos["symbol"], "venta", pos["quantity"], pos["price"], reason, log)
        if not entry.get("error"):
            sell_proceeds += pos["value"]
            trades_today += 1

    # ── Execute buys ─────────────────────────────────────────────────────────
    available = (cash_ars + sell_proceeds) - min_cash_hold
    for candidate in buy_candidates[:3]:
        if trades_today >= MAX_DAILY_TRADES:
            break
        if available <= 0:
            print("Sin cash disponible para compras")
            break
        trade_amount = min(max_per_trade, available)
        quantity = int(trade_amount / candidate["price"])
        if quantity < 1:
            print(f"  {candidate['symbol']}: cash insuficiente para 1 acción (${candidate['price']:,.0f})")
            continue
        reason = " | ".join(candidate["reasons"])
        entry = place_order(token, candidate["symbol"], "compra", quantity, candidate["price"], reason, log)
        if not entry.get("error"):
            available -= quantity * candidate["price"]
            trades_today += 1

    save_trades_log(log)

    today_str = datetime.now().strftime("%Y-%m-%d")
    n = sum(1 for t in log["trades"] if t["date"] == today_str)
    print(f"Fin. {n} operaciones hoy. DRY_RUN={DRY_RUN}")


if __name__ == "__main__":
    main()
