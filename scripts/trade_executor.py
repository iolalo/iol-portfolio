import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Dependencias opcionales ───────────────────────────────────────────────────
try:
    import holidays as _holidays_lib
    _HOLIDAYS_AR = _holidays_lib.country_holidays("AR")
    HAS_HOLIDAYS = True
except ImportError:
    HAS_HOLIDAYS = False
    print("⚠️  Librería 'holidays' no instalada – usando lista estática de feriados.")

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    print("⚠️  Librería 'yfinance' no instalada – los indicadores RSI/MA20 se tomarán del portfolio.json.")

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

# ── Feriados estáticos (fallback) ─────────────────────────────────────────────
_HOLIDAYS_FALLBACK = {
    "2025-01-01", "2025-03-03", "2025-03-04", "2025-04-02", "2025-04-17",
    "2025-04-18", "2025-05-01", "2025-05-25", "2025-06-16", "2025-06-20",
    "2025-07-09", "2025-08-17", "2025-10-12", "2025-11-20", "2025-12-08",
    "2025-12-25",
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-04-02", "2026-04-03",
    "2026-05-01", "2026-05-25", "2026-06-15", "2026-06-20", "2026-07-09",
    "2026-08-17", "2026-10-12", "2026-11-20", "2026-12-08", "2026-12-25",
}

# ── Mapeo de estados de órdenes IOL ───────────────────────────────────────────
_IOL_STATE_MAP = {
    "ejecutada":                "ejecutada",
    "ejecutado":                "ejecutada",
    "operada":                  "ejecutada",
    "parcialmente ejecutada":   "parcial",
    "parcial":                  "parcial",
    "activa":                   "pendiente",
    "pendiente":                "pendiente",
    "en proceso":               "pendiente",
    "cancelada":                "cancelada",
    "cancelado":                "cancelada",
    "anulada":                  "cancelada",
    "rechazada":                "cancelada",
    "expirada":                 "cancelada",
    "vencida":                  "cancelada",
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
SCAN_BUDGET_PCT  = float(os.environ.get("SCAN_BUDGET_PCT", "30"))
SCAN_ASSET_TYPES = os.environ.get("SCAN_ASSET_TYPES", "ACCION,CEDEAR").upper().split(",")
LOOP_MINUTES     = int(os.environ.get("LOOP_MINUTES", "5"))

# ── Telegram helpers (MarkdownV2) ─────────────────────────────────────────────
_MD_V2_SPECIAL = [
    "_", "*", "[", "]", "(", ")", "~", "`", ">",
    "#", "+", "-", "=", "|", "{", "}", ".", "!"
]

def _escape_md(text: str) -> str:
    for ch in ["\\"] + _MD_V2_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text

def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
            },
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

# ── Tick size ─────────────────────────────────────────────────────────────────
def _round_to_tick(price: float, side: str) -> float:
    if price < 1:          tick = 0.001
    elif price < 10:       tick = 0.01
    elif price < 100:      tick = 0.05
    elif price < 1_000:    tick = 0.50
    elif price < 10_000:   tick = 5.0
    elif price < 100_000:  tick = 25.0
    else:                  tick = 50.0

    if side == "buy":
        result = round(math.ceil(price / tick) * tick, 6)
    else:
        result = round(math.floor(price / tick) * tick, 6)

    return max(tick, result)

# ── Live price ─────────────────────────────────────────────────────────────────
def get_live_price(symbol: str, retries: int = 2) -> float | None:
    for attempt in range(retries + 1):
        try:
            data = iol.get(f"/api/v2/bCBA/Titulos/{symbol}/Cotizacion")
            price = (
                data.get("ultimoPrecio")
                or data.get("ultimo")
                or data.get("ultimoCierre")
                or data.get("last")
            )
            if price and float(price) > 0:
                return float(price)
            return None
        except Exception as exc:
            if attempt < retries:
                wait = 2 * (attempt + 1)
                log.warning("Live price %s (attempt %d/%d): %s — retry in %ds",
                            symbol, attempt + 1, retries + 1, exc, wait)
                time.sleep(wait)
            else:
                log.warning("Live price fetch failed for %s: %s", symbol, exc)
    return None

# ── Order status ───────────────────────────────────────────────────────────────
def check_order_status(oid: str, wait_secs: int = 5) -> tuple[str, int | None]:
    if not oid or oid in ("?", "DRY-RUN"):
        return "unknown", None

    time.sleep(wait_secs)
    try:
        resp = iol.get(f"/api/v2/operaciones/{oid}")
        raw  = (resp.get("estado") or resp.get("status") or resp.get("Estado") or "").lower().strip()
        status = _IOL_STATE_MAP.get(raw)
        if status is None:
            if "ejecut" in raw:
                status = "ejecutada"
            elif "parcial" in raw:
                status = "parcial"
            elif any(k in raw for k in ("cancel", "anul", "rechaz", "venc", "expir")):
                status = "cancelada"
            else:
                status = "unknown"
        log.info("Order #%s status: '%s' → %s", oid, raw, status)

        filled_qty = None
        for key in ("cantidadEjecutada", "cantidadOperada", "operado", "filledQty"):
            val = resp.get(key)
            if val is not None:
                try:
                    filled_qty = int(float(val))
                except (ValueError, TypeError):
                    pass
                break
        return status, filled_qty
    except Exception as exc:
        log.warning("Order status check failed for #%s: %s — assuming pending", oid, exc)
        return "unknown", None

# ── Indicadores técnicos ──────────────────────────────────────────────────────
def _get_historical_prices(symbol: str, period_days: int = 60) -> list[float]:
    if not HAS_YFINANCE:
        return []
    try:
        ticker = yf.Ticker(symbol + ".BA")
        df = ticker.history(period=f"{period_days}d")
        if df.empty:
            return []
        return df["Close"].tolist()
    except Exception as e:
        log.warning("Error descargando histórico para %s: %s", symbol, e)
        return []

def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))

def compute_sma(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

# ── Market scanner ────────────────────────────────────────────────────────────
def get_merval_tickers() -> list[tuple[str, str]]:
    try:
        data = iol.get("/api/v2/bCBA/Titulos")
        tickers = []
        for item in data.get("titulos", []):
            simbolo = item.get("simbolo") or item.get("symbol")
            tipo = (item.get("tipo") or item.get("type") or "").upper()
            if simbolo:
                tickers.append((simbolo, tipo))
        log.info("Panel bCBA: %d instrumentos obtenidos.", len(tickers))
        return tickers
    except Exception as exc:
        log.error("No se pudo obtener la lista de instrumentos: %s", exc)
        return []

def scan_market(rules: dict, overrides: dict, portfolio_syms: set, budget: float, signals_done: set) -> list[dict]:
    if budget <= 0 or not HAS_YFINANCE:
        return []

    all_tickers = get_merval_tickers()
    if not all_tickers:
        return []

    allowed_types = set(SCAN_ASSET_TYPES)
    excluded = portfolio_syms.copy()
    excluded.update(sym for sym, ov in overrides.items() if ov.get("no_buy"))

    candidates = [
        sym for sym, tipo in all_tickers
        if tipo in allowed_types and sym not in excluded
    ]

    log.info("Scanner: %d tickers después de filtrar tipo=%s.", len(candidates), SCAN_ASSET_TYPES)

    rsi_buy = float(rules["rsi_buy"])
    slip    = float(rules["limit_slippage_pct"]) / 100

    opportunities = []
    for i, sym in enumerate(candidates):
        if i > 0:
            time.sleep(0.15)

        if ("buy", sym, "market_scanner") in signals_done:
            continue

        price = get_live_price(sym)
        if not price or price <= 0:
            continue

        lp = _round_to_tick(price * (1 + slip), "buy")
        if lp > budget:
            continue

        closes = _get_historical_prices(sym, period_days=60)
        if len(closes) < 21:
            continue

        rsi = compute_rsi(closes, 14)
        ma20 = compute_sma(closes, 20)
        if rsi is None or ma20 is None:
            continue

        if rsi < rsi_buy and price < ma20:
            discount  = (ma20 - price) / ma20
            rsi_score = (rsi_buy - rsi) / rsi_buy
            score     = discount * 0.6 + rsi_score * 0.4
            opportunities.append({
                "symbol": sym,
                "price":  price,
                "rsi":    round(rsi, 2),
                "ma20":   round(ma20, 2),
                "score":  round(score, 4),
            })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    log.info("Scanner: %d oportunidades nuevas.", len(opportunities))
    return opportunities

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
def get_cash(term="t1") -> float | None:
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
                    log.info("Cash (%s): $%s", target_liq, f"{val:,.2f}")
                    return val
            return float(cuenta.get("disponible") or 0)
    except Exception as exc:
        log.error("Balance fetch failed: %s", exc)
        send_telegram(
            "❌ *Trade Bot — ERROR de saldo*\n"
            f"No se pudo obtener el efectivo disponible:\n"
            f"`{_escape_md(str(exc)[:300])}`\n"
            "_Bot abortado — operar sin conocer el saldo real es peligroso\\._"
        )
    return None

# ── Order execution ────────────────────────────────────────────────────────────
def place_order(symbol, side, qty, limit_price, term):
    body = {
        "mercado":   "bCBA",
        "simbolo":   symbol,
        "cantidad":  int(qty),
        "precio":    round(float(limit_price), 6),
        "validez":   "HoyHasta",
        "tipo":      "precioLimite",
        "plazo":     term,
        "operacion": "compra" if side == "buy" else "venta",
    }
    log.info("%sOrder body: %s", "[DRY RUN] " if DRY_RUN else "", body)

    if DRY_RUN:
        return True, "DRY-RUN", f"DRY RUN — {side} {qty}x {symbol} @ {limit_price}"

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
            f"🔵 *\\[SIMULACIÓN\\] {side_label} {_escape_md(symbol)}* — {_escape_md(reason.upper())}\n"
            f"Señal: {qty} acc a límite ${_escape_md(f'{limit_price:,.2f}')}\n"
            f"Precio ref: ${_escape_md(f'{price:,.0f}')}\n"
            "_bot en modo DRY RUN — no se ejecutó ninguna orden real_"
        )
    else:
        action = "Compré" if side == "buy" else "Vendí"
        detail = (f"✅ Orden #{_escape_md(str(oid))}" if ok
                  else f"❌ {_escape_md(msg[:200])}")
        send_telegram(
            f"{icon} *{side_label} {_escape_md(symbol)}* — {_escape_md(reason.upper())}\n"
            f"{action} {qty} acc a límite ${_escape_md(f'{limit_price:,.2f}')}\n"
            f"Precio ref: ${_escape_md(f'{price:,.0f}')}\n"
            f"{detail}"
        )

    log.info("%s %s %s qty=%d lp=%.2f ok=%s %s",
             side_label, symbol, reason, qty, limit_price, ok, msg)
    return entry

def _apply_fill(entry, fill_status, filled_qty, buy_budget, qty, lp, is_buy):
    entry["fill_status"] = fill_status

    if fill_status == "cancelada":
        entry["status"] = "cancelled"
        log.info("Order #%s cancelled – no se descuenta ni cuenta para el límite.", entry.get("order_id"))
        return buy_budget, False

    if fill_status == "parcial":
        if filled_qty is not None:
            entry["quantity"] = filled_qty
            entry["message"] += f" (parcial: {filled_qty}/{qty})"
            if is_buy:
                real_cost = filled_qty * lp
                return buy_budget - real_cost, True
            else:
                return buy_budget, True
        else:
            log.warning("Orden #%s parcial sin cantidad ejecutada – no se descuenta presupuesto.",
                        entry.get("order_id"))
            entry["message"] += " (parcial, cantidad desconocida)"
            return buy_budget, True

    if is_buy:
        buy_budget -= qty * lp
    return buy_budget, True

# ── Market hours ───────────────────────────────────────────────────────────────
def byma_open():
    now     = datetime.now(ART)
    today_d = now.date()
    today_s = today_d.strftime("%Y-%m-%d")

    if HAS_HOLIDAYS:
        if today_d in _HOLIDAYS_AR:
            log.info("Feriado (dinámico) (%s) — BYMA cerrado.", today_s)
            return False
    else:
        if today_s in _HOLIDAYS_FALLBACK:
            log.info("Feriado (estático) (%s) — BYMA cerrado.", today_s)
            return False

    return now.weekday() < 5 and 11 <= now.hour < 17

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(ART)
    log.info("Time ART: %s (weekday=%d) | DRY_RUN=%s | holidays_lib=%s | yfinance=%s",
             now.strftime("%Y-%m-%d %H:%M"), now.weekday(), DRY_RUN, HAS_HOLIDAYS, HAS_YFINANCE)

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

    iol.authenticate()
    term    = str(rules["settlement_term"])
    max_ops = int(rules["max_ops_per_day"])
    slip    = float(rules["limit_slippage_pct"]) / 100

    # Señales ejecutadas en esta sesión — evita duplicados entre iteraciones
    signals_done: set = set()

    # Notificación de inicio (una sola vez)
    cash_init = get_cash(term)
    if cash_init is None:
        return

    send_telegram(
        f"🤖 *Trade Bot \\[{'SIMULACIÓN' if DRY_RUN else 'REAL'}\\] INICIADO*\n"
        f"⏱️ Intervalo: cada {LOOP_MINUTES} min \\| Límite diario: {max_ops} ops\n"
        f"💰 Cash inicial: ${_escape_md(f'{cash_init:,.0f}')} ARS"
    )

    # ── Bucle principal ──────────────────────────────────────────────────────
    while True:
        now = datetime.now(ART)
        if not byma_open():
            log.info("Mercado cerrado. Saliendo del bucle.")
            break

        log.info("── Iteración %s ──", now.strftime("%H:%M"))

        cash = get_cash(term)
        if cash is None:
            time.sleep(60 * LOOP_MINUTES)
            continue

        usable     = max(0.0, cash - float(rules["cash_reserve_ars"]))
        buy_budget = usable * float(rules["buy_cash_pct"]) / 100

        trade_log = load_log()
        ops_today = today_op_count(trade_log)

        if ops_today >= max_ops and not DRY_RUN:
            log.info("Límite diario alcanzado (%d/%d). Esperando...", ops_today, max_ops)
            time.sleep(60 * LOOP_MINUTES)
            continue

        # Refrescar precios e indicadores
        all_items = portfolio.get("positions", []) + portfolio.get("watchlist", [])
        for item in all_items:
            sym  = item["symbol"]
            live = get_live_price(sym)
            if live and live > 0:
                item["unit_price"] = live
            if HAS_YFINANCE:
                closes = _get_historical_prices(sym, period_days=60)
                if len(closes) >= 21:
                    rsi_val = compute_rsi(closes, 14)
                    ma_val  = compute_sma(closes, 20)
                    if rsi_val is not None:
                        item["rsi"] = rsi_val
                    if ma_val is not None:
                        item["ma20"] = ma_val

        # ── 1) Stop-loss / Take-profit ─────────────────────────────────────
        for pos in portfolio.get("positions", []):
            sym   = pos["symbol"]
            price = pos.get("unit_price", 0)
            ppc   = pos.get("ppc", price) or price
            qty   = pos.get("quantity", 0)
            ov    = overrides.get(sym, {})

            sl_pct = float(ov.get("stop_loss_pct", rules["stop_loss_pct"]))
            if qty > 0 and ppc > 0 and price <= ppc * (1 - sl_pct / 100):
                if ("sell", sym, "stop-loss") not in signals_done and not ov.get("no_sell"):
                    lp = _round_to_tick(price * (1 - slip), "sell")
                    ok, oid, msg = place_order(sym, "sell", qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "sell", "stop-loss", qty, price, lp, ok, oid, msg)
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        _, count = _apply_fill(entry, fill, fqty, buy_budget, qty, lp, is_buy=False)
                        if count:
                            signals_done.add(("sell", sym, "stop-loss"))
                continue

            tp_pct = float(ov.get("take_profit_pct", rules["take_profit_pct"]))
            if qty > 0 and ppc > 0 and price >= ppc * (1 + tp_pct / 100):
                if ("sell", sym, "take-profit") not in signals_done and not ov.get("no_sell"):
                    sell_qty = max(1, qty // 2)
                    lp = _round_to_tick(price * (1 - slip), "sell")
                    ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "sell", "take-profit", sell_qty, price, lp, ok, oid, msg)
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        _, count = _apply_fill(entry, fill, fqty, buy_budget, sell_qty, lp, is_buy=False)
                        if count:
                            signals_done.add(("sell", sym, "take-profit"))
                continue

        # ── 2) RSI señales en posiciones ───────────────────────────────────
        for pos in portfolio.get("positions", []):
            sym   = pos["symbol"]
            price = pos.get("unit_price", 0)
            rsi   = pos.get("rsi")
            ma20  = pos.get("ma20")
            qty   = pos.get("quantity", 0)
            ov    = overrides.get(sym, {})

            rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
            if (rsi is not None and ma20 is not None
                    and rsi < rsi_buy and price < ma20
                    and not ov.get("no_buy")
                    and ("buy", sym, "RSI+MA20") not in signals_done):
                lp      = _round_to_tick(price * (1 + slip), "buy")
                buy_qty = max(1, int(buy_budget // lp))
                if buy_qty * lp <= buy_budget + 1e-6:
                    ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "buy", "RSI+MA20", buy_qty, price, lp, ok, oid, msg)
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                        if count:
                            signals_done.add(("buy", sym, "RSI+MA20"))
                            ops_today += 1

            rsi_sell = float(ov.get("rsi_sell", rules["rsi_sell"]))
            if (rsi is not None and ma20 is not None
                    and rsi > rsi_sell and price > ma20
                    and qty > 0
                    and not ov.get("no_sell")
                    and ("sell", sym, "RSI+MA20") not in signals_done):
                sell_qty = max(1, qty // 2)
                lp = _round_to_tick(price * (1 - slip), "sell")
                ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                entry = log_and_notify(trade_log, sym, "sell", "RSI+MA20", sell_qty, price, lp, ok, oid, msg)
                if ok and not DRY_RUN:
                    fill, fqty = check_order_status(oid)
                    _, count = _apply_fill(entry, fill, fqty, buy_budget, sell_qty, lp, is_buy=False)
                    if count:
                        signals_done.add(("sell", sym, "RSI+MA20"))
                        ops_today += 1

        # ── 3) Watchlist ───────────────────────────────────────────────────
        portfolio_syms = {
            p["symbol"] for p in portfolio.get("positions", [])
            if p.get("quantity", 0) > 0
        }
        for wpos in portfolio.get("watchlist", []):
            sym = wpos["symbol"]
            if sym in portfolio_syms:
                continue
            price = wpos.get("unit_price", 0)
            rsi   = wpos.get("rsi")
            ma20  = wpos.get("ma20")
            ov    = overrides.get(sym, {})

            rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
            if (rsi is not None and ma20 is not None
                    and rsi < rsi_buy and price < ma20
                    and not ov.get("no_buy")
                    and ("buy", sym, "RSI+MA20 (watchlist)") not in signals_done):
                lp      = _round_to_tick(price * (1 + slip), "buy")
                buy_qty = max(1, int(buy_budget // lp))
                if buy_qty * lp <= buy_budget + 1e-6:
                    ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "buy", "RSI+MA20 (watchlist)", buy_qty, price, lp, ok, oid, msg)
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                        if count:
                            signals_done.add(("buy", sym, "RSI+MA20 (watchlist)"))
                            ops_today += 1

        # ── 4) Market scanner ──────────────────────────────────────────────
        scan_budget = buy_budget * (SCAN_BUDGET_PCT / 100)
        if scan_budget > 0 and not DRY_RUN:
            opportunities = scan_market(rules, overrides, portfolio_syms, scan_budget, signals_done)
            for opp in opportunities:
                if ops_today >= max_ops or buy_budget <= 0:
                    break
                sym     = opp["symbol"]
                price   = opp["price"]
                lp      = _round_to_tick(price * (1 + slip), "buy")
                buy_qty = max(1, int(buy_budget // lp))
                if buy_qty * lp > buy_budget + 1e-6:
                    continue
                if ("buy", sym, "market_scanner") in signals_done:
                    continue
                ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
                entry = log_and_notify(trade_log, sym, "buy", "market_scanner", buy_qty, price, lp, ok, oid, msg)
                if ok and not DRY_RUN:
                    fill, fqty = check_order_status(oid)
                    buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                    if count:
                        signals_done.add(("buy", sym, "market_scanner"))
                        ops_today += 1

        save_log(trade_log)
        log.info("Esperando %d min para siguiente iteración...", LOOP_MINUTES)
        time.sleep(60 * LOOP_MINUTES)

    log.info("Bot detenido.")

if __name__ == "__main__":
    main()
