import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

IOL_BASE   = "https://api.invertironline.com"
ART        = timezone(timedelta(hours=-3))
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent
TRADES_LOG = ROOT / "data" / "trades_log.json"
PORTFOLIO  = ROOT / "data" / "portfolio.json"
CONTEXT_MD = SCRIPT_DIR / "trading_context.md"

_HEADERS = {
    "Content-Type": "application/json",
    "Accept":       "application/json",
    "User-Agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Argentina public holidays — update annually
ARGENTINA_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-03-03", "2025-03-04", "2025-04-02", "2025-04-17",
    "2025-04-18", "2025-05-01", "2025-05-25", "2025-06-16", "2025-06-20",
    "2025-07-09", "2025-08-17", "2025-10-12", "2025-11-20", "2025-12-08",
    "2025-12-25",
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-04-02", "2026-04-03",
    "2026-05-01", "2026-05-25", "2026-06-15", "2026-06-20", "2026-07-09",
    "2026-08-17", "2026-10-12", "2026-11-20", "2026-12-08", "2026-12-25",
}

# ── Environment validation ────────────────────────────────────────────────────

def _require_env(*names):
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)

_require_env("IOL_USERNAME", "IOL_PASSWORD")

IOL_USER   = os.environ["IOL_USERNAME"]
IOL_PASS   = os.environ["IOL_PASSWORD"]
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN    = os.environ.get("DRY_RUN", "true").lower() == "true"

# ── Telegram ──────────────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in dynamic content."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            log.warning("Telegram error %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)

# ── HTTP session ──────────────────────────────────────────────────────────────

def _build_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class _IOLSession:
    """Thread-safe IOL client with persistent session and auto-refresh on 401."""

    def __init__(self):
        self._session = _build_session()
        self._token   = None
        self._lock    = threading.Lock()

    def _fetch_token(self):
        for attempt in range(3):
            try:
                r = self._session.post(
                    f"{IOL_BASE}/token",
                    data={"username": IOL_USER, "password": IOL_PASS,
                          "grant_type": "password"},
                    timeout=30,
                )
                r.raise_for_status()
                self._token = r.json()["access_token"]
                log.info("Authenticated OK")
                return
            except Exception as exc:
                if attempt < 2:
                    wait = 5 * (2 ** attempt)
                    log.warning("Auth attempt %d/3: %s — retry in %ds", attempt + 1, exc, wait)
                    time.sleep(wait)
                else:
                    raise

    def authenticate(self):
        with self._lock:
            self._fetch_token()

    def get(self, path):
        with self._lock:
            if not self._token:
                self._fetch_token()
            headers = {"Authorization": f"Bearer {self._token}"}

        for attempt in range(3):
            try:
                r = self._session.get(f"{IOL_BASE}{path}", headers=headers, timeout=45)
                if r.status_code == 401:
                    log.warning("401 on GET %s — refreshing token", path)
                    with self._lock:
                        self._fetch_token()
                        headers = {"Authorization": f"Bearer {self._token}"}
                    r = self._session.get(f"{IOL_BASE}{path}", headers=headers, timeout=45)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                if attempt < 2:
                    wait = 5 * (2 ** attempt)
                    log.warning("Timeout GET %s (%d/3) — retry in %ds", path, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    raise
            except requests.exceptions.RequestException:
                raise

    def post(self, path, body):
        with self._lock:
            if not self._token:
                self._fetch_token()
            headers = {**_HEADERS, "Authorization": f"Bearer {self._token}"}

        for attempt in range(3):
            try:
                r = self._session.post(
                    f"{IOL_BASE}{path}", headers=headers, json=body, timeout=45
                )
                if r.status_code == 401:
                    log.warning("401 on POST %s — refreshing token", path)
                    with self._lock:
                        self._fetch_token()
                        headers = {**_HEADERS, "Authorization": f"Bearer {self._token}"}
                    r = self._session.post(
                        f"{IOL_BASE}{path}", headers=headers, json=body, timeout=45
                    )
                if not r.ok:
                    log.error("HTTP %d on POST %s: %s", r.status_code, path, r.text[:800])
                    r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                if attempt < 2:
                    wait = 5 * (2 ** attempt)
                    log.warning("Timeout POST %s (%d/3) — retry in %ds", path, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    raise
            except requests.exceptions.RequestException:
                raise


iol = _IOLSession()

# ── Config parsing ─────────────────────────────────────────────────────────────

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


def _coerce(value: str, target):
    """Robustly cast value to the same type as target. Falls back to str."""
    if isinstance(target, bool):
        return value.lower() in ("true", "1", "yes")
    for cast in (type(target), int, float):
        try:
            return cast(value)
        except (ValueError, TypeError):
            continue
    return value


def parse_context():
    rules     = dict(DEFAULTS)
    overrides = {}

    if not CONTEXT_MD.exists():
        return rules, overrides

    try:
        text = CONTEXT_MD.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read trading_context.md: %s", exc)
        return rules, overrides

    fm = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k in rules:
                rules[k] = _coerce(v, rules[k])

    for line in text.splitlines():
        m = re.match(r"^-\s+([A-Z.]+):\s+(.*)", line.strip())
        if not m:
            continue
        sym, note = m.group(1), m.group(2).lower()
        ov = overrides.setdefault(sym, {})
        if "no_sell=true" in note or "no vender" in note:
            ov["no_sell"] = True
        if "no_buy=true" in note or "no comprar" in note:
            ov["no_buy"] = True
        for key, val in re.findall(r"(\w+)=([\d.]+)", note):
            ov[key] = float(val)

    return rules, overrides

# ── Trades log ─────────────────────────────────────────────────────────────────

def load_log():
    if not TRADES_LOG.exists():
        return []
    try:
        data = json.loads(TRADES_LOG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("trades", [])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as exc:
        log.error("Malformed trades_log.json: %s — starting fresh", exc)
        return []


def save_log(trade_log):
    TRADES_LOG.parent.mkdir(exist_ok=True)
    TRADES_LOG.write_text(
        json.dumps(trade_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def today_op_count(trade_log):
    today = datetime.now(ART).strftime("%Y-%m-%d")
    return sum(1 for t in trade_log
               if t.get("date", "").startswith(today)
               and t.get("status") == "executed")

# ── Balance ───────────────────────────────────────────────────────────────────

def get_cash(term="t1"):
    """Returns ARS cash available, or None on error (caller must abort)."""
    liquidacion_map = {"t0": "inmediato", "t1": "hrs24", "t2": "hrs48"}
    target_liq = liquidacion_map.get(term, "hrs24")
    try:
        data = iol.get("/api/v2/estadocuenta")
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected response type: {type(data).__name__}")
        for cuenta in data.get("cuentas", []):
            moneda = (cuenta.get("moneda") or "").lower()
            if "peso" not in moneda:
                continue
            for s in cuenta.get("saldos", []):
                if s.get("liquidacion") == target_liq:
                    val = float(s.get("disponibleOperar") or 0)
                    log.info("Cash (%s): $%,.2f", target_liq, val)
                    return val
            return float(cuenta.get("disponible") or 0)
    except Exception as exc:
        log.error("Balance fetch failed: %s", exc)
        send_telegram(
            f"❌ *Trade Bot — ERROR de saldo*\n"
            f"No se pudo obtener el efectivo disponible:\n"
            f"`{_escape_md(str(exc)[:300])}`\n"
            f"_Bot abortado — operando sin conocer el saldo real es peligroso._"
        )
    return None

# ── Order execution ────────────────────────────────────────────────────────────

def place_order(symbol, side, qty, limit_price, term):
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
    log.info("%sOrder body: %s", "[DRY RUN] " if DRY_RUN else "", body)

    if DRY_RUN:
        return True, "DRY-RUN", f"DRY RUN — {side} {qty}x {symbol} @ {limit_price}"

    # Step 1 — Validar, extraer validacionId
    validation_id = None
    try:
        val = iol.post("/api/v2/operaciones/Validar", body)
        log.info("Validate response: %s", val)
        validation_id = (
            val.get("validacionId")
            or val.get("validation_id")
            or val.get("id")
        )
        msgs   = val.get("mensajes", val.get("warnings", []))
        errors = [m for m in msgs if isinstance(m, str) and m]
        if errors:
            return False, None, f"Validation: {errors}"
    except Exception as exc:
        log.warning("Validate step failed: %s", exc)
        return False, None, f"Validate error: {exc}"

    if not validation_id:
        return False, None, "Validate returned no validacionId"

    # Step 2 — Ejecutar con validacionId en el path
    try:
        resp = iol.post(f"/api/v2/operaciones/{validation_id}", body)
        oid  = str(resp.get("id", resp.get("numeroOperacion", resp.get("numero", "?"))))
        return True, oid, f"OK #{oid}"
    except Exception as exc:
        return False, None, str(exc)


def log_and_notify(trade_log, symbol, side, reason, qty, price, limit_price, ok, oid, msg):
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
    trade_log.append(entry)

    side_label = "COMPRA" if side == "buy" else "VENTA"
    icon       = ("🟢" if side == "buy" else "🔴") if ok else "❌"

    if DRY_RUN:
        send_telegram(
            f"🔵 *[SIMULACIÓN] {side_label} {symbol}* — {reason.upper()}\n"
            f"Señal: {qty} acc a límite ${limit_price:,.2f}\n"
            f"Precio ref: ${price:,.0f}\n"
            f"_(bot en modo DRY RUN — no se ejecutó ninguna orden real)_"
        )
    else:
        action = "Compré" if side == "buy" else "Vendí"
        detail = (f"✅ Orden #{_escape_md(str(oid))}" if ok
                  else f"❌ {_escape_md(msg[:200])}")
        send_telegram(
            f"{icon} *{side_label} {symbol}* — {reason.upper()}\n"
            f"{action} {qty} acc a límite ${limit_price:,.2f}\n"
            f"Precio ref: ${price:,.0f}\n"
            f"{detail}"
        )

    log.info("%s %s %s qty=%d lp=%.2f ok=%s %s",
             side_label, symbol, reason, qty, limit_price, ok, msg)
    return entry

# ── Market hours ───────────────────────────────────────────────────────────────

def byma_open():
    now     = datetime.now(ART)
    today_s = now.strftime("%Y-%m-%d")
    if today_s in ARGENTINA_HOLIDAYS:
        log.info("Public holiday (%s) — BYMA closed.", today_s)
        return False
    return now.weekday() < 5 and 11 <= now.hour < 17

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(ART)
    log.info("Time ART: %s (weekday=%d) | DRY_RUN=%s",
             now.strftime("%Y-%m-%d %H:%M"), now.weekday(), DRY_RUN)

    if not byma_open():
        log.info("BYMA closed — skipping.")
        return

    rules, overrides = parse_context()
    log.info("Rules: %s", rules)

    if not PORTFOLIO.exists():
        log.error("portfolio.json missing — run fetch_portfolio.py first.")
        return
    try:
        portfolio = json.loads(PORTFOLIO.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("Malformed portfolio.json: %s — aborting.", exc)
        return

    # Warn if portfolio data is stale
    last_updated = portfolio.get("last_updated", "")
    try:
        lu_date = datetime.strptime(last_updated[:10], "%Y-%m-%d").date()
        if lu_date < now.date():
            log.warning("portfolio.json is stale (last updated: %s)", last_updated)
            send_telegram(
                f"⚠️ *Trade Bot*: portfolio.json desactualizado "
                f"(último update: {last_updated}). "
                f"Señales pueden ser incorrectas."
            )
    except Exception:
        log.warning("Could not parse last_updated field: %s", last_updated)

    trade_log = load_log()
    ops_today = today_op_count(trade_log)
    max_ops   = int(rules["max_ops_per_day"])

    if not DRY_RUN and ops_today >= max_ops:
        log.info("Daily limit reached (%d/%d).", ops_today, max_ops)
        return

    iol.authenticate()
    term = str(rules["settlement_term"])
    cash = get_cash(term)
    if cash is None:
        return  # error already logged and Telegram-alerted

    # Symbols already held — used to prevent duplicate watchlist buys
    portfolio_syms = {
        p["symbol"] for p in portfolio.get("positions", [])
        if p.get("quantity", 0) > 0
    }

    usable     = max(0.0, cash - float(rules["cash_reserve_ars"]))
    slip       = float(rules["limit_slippage_pct"]) / 100
    buy_budget = usable * float(rules["buy_cash_pct"]) / 100

    # Only real (non-DRY_RUN) executions count toward daily limit
    executed: list = []

    # Health-check: startup notification
    mode_tag = "[SIMULACIÓN] " if DRY_RUN else ""
    send_telegram(
        f"🤖 *Trade Bot {mode_tag}— {now.strftime('%H:%M')} ART*\n"
        f"💰 Cash: ${cash:,.0f} ARS | Posiciones: {len(portfolio.get('positions', []))}"
    )

    # ── Portfolio: stop-loss / take-profit / RSI signals ─────────────────────
    for pos in portfolio.get("positions", []):
        if not DRY_RUN and ops_today + len(executed) >= max_ops:
            break

        sym   = pos["symbol"]
        price = pos["unit_price"]
        ppc   = pos.get("ppc", price) or price
        rsi   = pos.get("rsi")
        ma20  = pos.get("ma20")
        rec   = pos.get("recommendation", "MANTENER")
        qty   = pos.get("quantity", 0)
        ov    = overrides.get(sym, {})

        # Stop-loss — sell full position
        sl_pct = float(ov.get("stop_loss_pct", rules["stop_loss_pct"]))
        if qty > 0 and ppc > 0 and price <= ppc * (1 - sl_pct / 100):
            if ov.get("no_sell"):
                log.info("%s: stop-loss triggered but no_sell override — skip", sym)
                continue
            lp  = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(sym, "sell", qty, lp, term)
            entry = log_and_notify(trade_log, sym, "sell", "stop-loss", qty, price, lp, ok, oid, msg)
            if ok and not DRY_RUN:
                executed.append(entry)
            continue

        # Take-profit — sell half position
        tp_pct = float(ov.get("take_profit_pct", rules["take_profit_pct"]))
        if qty > 0 and ppc > 0 and price >= ppc * (1 + tp_pct / 100):
            if ov.get("no_sell"):
                log.info("%s: take-profit triggered but no_sell override — skip", sym)
                continue
            sell_qty = max(1, qty // 2)
            lp  = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
            entry = log_and_notify(trade_log, sym, "sell", "take-profit", sell_qty, price, lp, ok, oid, msg)
            if ok and not DRY_RUN:
                executed.append(entry)
            continue

        # RSI buy — averaging down on existing position
        rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
        if (rec == "COMPRAR"
                and rsi is not None and rsi < rsi_buy
                and ma20 is not None and price < ma20
                and not ov.get("no_buy")):
            lp      = round(price * (1 + slip), 2)  # limit price first
            buy_qty = max(1, int(buy_budget // lp))  # qty based on limit price (conservative)
            if buy_qty * lp > buy_budget:
                log.info("%s: insufficient budget (need $%.0f, have $%.0f)", sym, buy_qty * lp, buy_budget)
                continue
            ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
            entry = log_and_notify(trade_log, sym, "buy", "RSI+MA20", buy_qty, price, lp, ok, oid, msg)
            if ok:
                buy_budget -= buy_qty * lp  # deduct at limit price in both modes
            if ok and not DRY_RUN:
                executed.append(entry)
            continue

        # RSI sell — sell half position
        rsi_sell = float(ov.get("rsi_sell", rules["rsi_sell"]))
        if (rec == "VENDER"
                and rsi is not None and rsi > rsi_sell
                and ma20 is not None and price > ma20
                and qty > 0
                and not ov.get("no_sell")):
            sell_qty = max(1, qty // 2)
            lp       = round(price * (1 - slip), 2)
            ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
            entry = log_and_notify(trade_log, sym, "sell", "RSI+MA20", sell_qty, price, lp, ok, oid, msg)
            if ok and not DRY_RUN:
                executed.append(entry)

    # ── Watchlist: open new positions ─────────────────────────────────────────
    for wpos in portfolio.get("watchlist", []):
        if not DRY_RUN and ops_today + len(executed) >= max_ops:
            break

        sym = wpos["symbol"]
        if sym in portfolio_syms:
            log.info("%s: already held — skip watchlist buy", sym)
            continue

        price = wpos["unit_price"]
        rsi   = wpos.get("rsi")
        ma20  = wpos.get("ma20")
        rec   = wpos.get("recommendation", "MANTENER")
        ov    = overrides.get(sym, {})

        rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
        if (rec == "COMPRAR"
                and rsi is not None and rsi < rsi_buy
                and ma20 is not None and price < ma20
                and not ov.get("no_buy")):
            lp      = round(price * (1 + slip), 2)
            buy_qty = max(1, int(buy_budget // lp))
            if buy_qty * lp > buy_budget:
                log.info("%s: insufficient budget (need $%.0f, have $%.0f)", sym, buy_qty * lp, buy_budget)
                continue
            ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
            entry = log_and_notify(
                trade_log, sym, "buy", "RSI+MA20 (watchlist)", buy_qty, price, lp, ok, oid, msg
            )
            if ok:
                buy_budget -= buy_qty * lp
            if ok and not DRY_RUN:
                executed.append(entry)

    save_log(trade_log)
    n_real = len(executed)
    log.info("Done — %d trade(s) executed today (%d/%d).",
             n_real, ops_today + n_real, max_ops)


if __name__ == "__main__":
    main()
