import os
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

IOL_BASE   = "https://api.invertironline.com"
IOL_USER   = os.environ["IOL_USERNAME"]
IOL_PASS   = os.environ["IOL_PASSWORD"]
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN    = os.environ.get("DRY_RUN", "true").lower() == "true"

_HEADERS = {
    "Content-Type": "application/json",
    "Accept":       "application/json",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

ART        = timezone(timedelta(hours=-3))
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent
TRADES_LOG = ROOT / "data" / "trades_log.json"
PORTFOLIO  = ROOT / "data" / "portfolio.json"
CONTEXT_MD = SCRIPT_DIR / "trading_context.md"


# ── IOL auth & HTTP ──────────────────────────────────────────────────────────

def get_token():
    r = requests.post(
        f"{IOL_BASE}/token",
        data={"username": IOL_USER, "password": IOL_PASS, "grant_type": "password"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def iol_get(token, path):
    r = requests.get(f"{IOL_BASE}{path}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def iol_post(token, path, body):
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    r = requests.post(f"{IOL_BASE}{path}", headers=headers, json=body, timeout=20)
    if not r.ok:
        print(f"  [HTTP {r.status_code}] POST {path} → {r.text[:800]}")
        r.raise_for_status()
    return r.json()


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"  [WARN] Telegram: {e}")


# ── Config parsing ────────────────────────────────────────────────────────────

DEFAULTS = {
    "rsi_buy":            35.0,
    "rsi_sell":           65.0,
    "stop_loss_pct":       8.0,
    "take_profit_pct":    25.0,
    "buy_cash_pct":       70.0,
    "max_ops_per_day":     2,
    "cash_reserve_ars":  500.0,
    "settlement_term":   "t1",
    "limit_slippage_pct":  0.5,
}


def parse_context():
    rules    = dict(DEFAULTS)
    overrides = {}   # symbol → {no_sell, no_buy, stop_loss_pct, ...}

    if not CONTEXT_MD.exists():
        return rules, overrides

    text = CONTEXT_MD.read_text(encoding="utf-8")

    # YAML front matter
    fm = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k in rules:
                try:
                    rules[k] = type(rules[k])(v)
                except (ValueError, TypeError):
                    rules[k] = v

    # Per-ticker notes: "- SYMBOL: key=val  key2=val2  no_sell=true"
    for line in text.splitlines():
        m = re.match(r"^-\s+([A-Z.]+):\s+(.*)", line.strip())
        if not m:
            continue
        sym, note = m.group(1), m.group(2).lower()
        ov = overrides.setdefault(sym, {})
        if "no_sell=true"  in note or "no vender" in note: ov["no_sell"] = True
        if "no_buy=true"   in note or "no comprar" in note: ov["no_buy"]  = True
        for key, val in re.findall(r"(\w+)=([\d.]+)", note):
            ov[key] = float(val)

    return rules, overrides


# ── Trades log ────────────────────────────────────────────────────────────────

def load_log():
    if TRADES_LOG.exists():
        data = json.loads(TRADES_LOG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("trades", [])
        return data if isinstance(data, list) else []
    return []


def save_log(log):
    TRADES_LOG.parent.mkdir(exist_ok=True)
    TRADES_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def today_op_count(log):
    today = datetime.now(ART).strftime("%Y-%m-%d")
    return sum(1 for t in log
               if t.get("date", "").startswith(today)
               and t.get("status") == "executed")


# ── Balance ──────────────────────────────────────────────────────────────────

def get_cash(token, term="t1"):
    """Return available cash for the given settlement term (t0=inmediato, t1=hrs24)."""
    liquidacion_map = {"t0": "inmediato", "t1": "hrs24", "t2": "hrs48"}
    target_liq = liquidacion_map.get(term, "hrs24")
    try:
        data = iol_get(token, "/api/v2/estadocuenta")
        if not isinstance(data, dict):
            return 0.0
        for cuenta in data.get("cuentas", []):
            moneda = (cuenta.get("moneda") or "").lower()
            if "peso" not in moneda:
                continue
            for s in cuenta.get("saldos", []):
                if s.get("liquidacion") == target_liq:
                    val = float(s.get("disponibleOperar") or 0)
                    print(f"  Cash ({target_liq}): ${val:,.2f}")
                    return val
            # fallback: top-level disponible
            return float(cuenta.get("disponible") or 0)
    except Exception as e:
        print(f"  [WARN] Balance: {e}")
    return 0.0


# ── Order execution ───────────────────────────────────────────────────────────

def place_order(token, symbol, side, qty, limit_price, term):
    """Validate + place a limit order. Returns (ok, order_id, msg)."""
    body = {
        "mercado":   "bCBA",
        "simbolo":   symbol,
        "cantidad":  int(qty),
        "precio":    round(float(limit_price), 2),
        "validez":   "HoyHasta",
        "tipo":      "precioLimite",
        "plazo":     term,
        "operacion": "compra" if side == "buy" else "venta",
    }
    tag = "[DRY RUN] " if DRY_RUN else ""
    print(f"  {tag}Order body: {body}")

    if DRY_RUN:
        return True, "DRY-RUN", f"DRY RUN — {side} {qty}x {symbol} @ {limit_price}"

    # Step 1 — Validate, extract validacionId
    validation_id = None
    try:
        val = iol_post(token, "/api/v2/operaciones/Validar", body)
        print(f"  Validate response: {val}")
        validation_id = (
            val.get("validacionId")
            or val.get("validation_id")
            or val.get("id")
        )
        msgs   = val.get("mensajes", val.get("warnings", []))
        errors = [m for m in msgs if isinstance(m, str) and m]
        if errors:
            return False, None, f"Validation: {errors}"
    except Exception as e:
        print(f"  [WARN] Validate step failed ({e})")
        return False, None, f"Validate error: {e}"

    if not validation_id:
        return False, None, "Validate returned no validacionId"

    # Step 2 — Place using validacionId in URL path
    try:
        resp = iol_post(token, f"/api/v2/operaciones/{validation_id}", body)
        oid  = str(resp.get("id", resp.get("numeroOperacion", resp.get("numero", "?"))))
        return True, oid, f"OK #{oid}"
    except Exception as e:
        return False, None, str(e)


def log_and_notify(log, symbol, side, reason, qty, price, limit_price, ok, oid, msg):
    entry = {
        "date":        datetime.now(ART).isoformat(),
        "symbol":      symbol,
        "side":        side,
        "reason":      reason,
        "quantity":    qty,
        "price":       price,
        "limit_price": limit_price,
        "status":      "dry_run" if DRY_RUN else ("executed" if ok else "failed"),
        "order_id":    oid,
        "message":     msg,
    }
    log.append(entry)

    icons = {"buy": "🟢", "sell": "🔴"}
    e = icons.get(side, "⚪") if ok else "❌"
    side_label = "COMPRA" if side == "buy" else "VENTA"
    send_telegram(
        f"{e} *{side_label} {symbol}* — {reason.upper()}\n"
        f"{'Compré' if side == 'buy' else 'Vendí'} {qty} acc a límite ${limit_price:,.2f}\n"
        f"Precio ref: ${price:,.0f}\n"
        f"{'✅ Orden #' + oid if ok else '❌ ' + msg}"
    )
    return entry


# ── Market hours ──────────────────────────────────────────────────────────────

def byma_open():
    now = datetime.now(ART)
    return now.weekday() < 5 and 11 <= now.hour < 17


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(ART)
    print(f"Time ART: {now.strftime('%Y-%m-%d %H:%M')} (weekday={now.weekday()})")
    if not byma_open():
        print("BYMA closed — skipping.")
        return

    rules, overrides = parse_context()
    print(f"Rules: {rules}")

    if not PORTFOLIO.exists():
        print("portfolio.json missing — run fetch_portfolio.py first.")
        return

    portfolio = json.loads(PORTFOLIO.read_text(encoding="utf-8"))
    log       = load_log()
    ops_today = today_op_count(log)
    max_ops   = int(rules["max_ops_per_day"])

    if ops_today >= max_ops:
        print(f"Daily limit reached ({ops_today}/{max_ops}).")
        return

    token = get_token()
    term  = str(rules["settlement_term"])
    cash  = get_cash(token, term)
    print(f"Cash available: ${cash:,.0f} ARS")

    usable     = max(0.0, cash - float(rules["cash_reserve_ars"]))
    buy_budget = usable * float(rules["buy_cash_pct"]) / 100
    slip       = float(rules["limit_slippage_pct"]) / 100
    executed   = []

    for pos in portfolio.get("positions", []):
        if ops_today + len(executed) >= max_ops:
            break

        sym   = pos["symbol"]
        price = pos["unit_price"]
        ppc   = pos.get("ppc", price) or price
        rsi   = pos.get("rsi")
        ma20  = pos.get("ma20")
        rec   = pos.get("recommendation", "MANTENER")
        qty   = pos.get("quantity", 0)
        ov    = overrides.get(sym, {})

        # ── Stop-loss ──────────────────────────────────────────────────────
        sl_pct = float(ov.get("stop_loss_pct", rules["stop_loss_pct"]))
        if qty > 0 and ppc > 0 and price <= ppc * (1 - sl_pct / 100):
            if ov.get("no_sell"):
                print(f"  {sym}: stop-loss triggered but no_sell override — skip")
                continue
            lp  = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(token, sym, "sell", qty, lp, term)
            entry = log_and_notify(log, sym, "sell", "stop-loss", qty, price, lp, ok, oid, msg)
            if ok: executed.append(entry)
            continue

        # ── Take-profit ────────────────────────────────────────────────────
        tp_pct = float(ov.get("take_profit_pct", rules["take_profit_pct"]))
        if qty > 0 and ppc > 0 and price >= ppc * (1 + tp_pct / 100):
            if ov.get("no_sell"):
                print(f"  {sym}: take-profit triggered but no_sell override — skip")
                continue
            sell_qty = max(1, qty // 2)
            lp  = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(token, sym, "sell", sell_qty, lp, term)
            entry = log_and_notify(log, sym, "sell", "take-profit", sell_qty, price, lp, ok, oid, msg)
            if ok: executed.append(entry)
            continue

        # ── Buy signal ─────────────────────────────────────────────────────
        rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
        buy_ok  = (
            rec == "COMPRAR"
            and rsi is not None and rsi < rsi_buy
            and ma20 is not None and price < ma20
            and buy_budget >= price
            and not ov.get("no_buy")
        )
        if buy_ok:
            buy_qty = max(1, int(buy_budget // price))
            lp      = round(price * (1 + slip), 2)
            ok, oid, msg = place_order(token, sym, "buy", buy_qty, lp, term)
            entry = log_and_notify(log, sym, "buy", "RSI+MA20", buy_qty, price, lp, ok, oid, msg)
            if ok:
                buy_budget -= buy_qty * price
                executed.append(entry)
            continue

        # ── Sell signal ────────────────────────────────────────────────────
        rsi_sell = float(ov.get("rsi_sell", rules["rsi_sell"]))
        sell_ok  = (
            rec == "VENDER"
            and rsi is not None and rsi > rsi_sell
            and ma20 is not None and price > ma20
            and qty > 0
            and not ov.get("no_sell")
        )
        if sell_ok:
            sell_qty = max(1, qty // 2)
            lp       = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(token, sym, "sell", sell_qty, lp, term)
            entry = log_and_notify(log, sym, "sell", "RSI+MA20", sell_qty, price, lp, ok, oid, msg)
            if ok: executed.append(entry)

    # ── Watchlist buy scan ────────────────────────────────────────────────────
    for wpos in portfolio.get("watchlist", []):
        if ops_today + len(executed) >= max_ops:
            break

        sym   = wpos["symbol"]
        price = wpos["unit_price"]
        rsi   = wpos.get("rsi")
        ma20  = wpos.get("ma20")
        rec   = wpos.get("recommendation", "MANTENER")
        ov    = overrides.get(sym, {})

        rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
        buy_ok  = (
            rec == "COMPRAR"
            and rsi is not None and rsi < rsi_buy
            and ma20 is not None and price < ma20
            and buy_budget >= price
            and not ov.get("no_buy")
        )
        if buy_ok:
            buy_qty = max(1, int(buy_budget // price))
            lp      = round(price * (1 + slip), 2)
            ok, oid, msg = place_order(token, sym, "buy", buy_qty, lp, term)
            entry = log_and_notify(log, sym, "buy", "RSI+MA20 (watchlist)", buy_qty, price, lp, ok, oid, msg)
            if ok:
                buy_budget -= buy_qty * price
                executed.append(entry)

    save_log(log)
    print(f"Done — {len(executed)} trade(s) executed today ({ops_today + len(executed)}/{max_ops}).")


if __name__ == "__main__":
    main()
