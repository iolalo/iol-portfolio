import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from iol_account import extract_cash_snapshot
from signal_logic import (
    DEFAULT_RULES,
    get_position_recommendation,
    get_watchlist_recommendation,
    parse_trading_context,
)

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
_LOG_FMT  = "%(asctime)s %(levelname)-8s %(message)s"
_LOG_DATE = "%H:%M:%S"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_LOG_DATE)
log = logging.getLogger(__name__)

def _setup_file_log(root: "Path") -> None:
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    existing = sorted(log_dir.glob("bot_*.log"))
    for old in existing[:-6]:
        old.unlink(missing_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh       = logging.FileHandler(log_dir / f"bot_{ts}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE))
    logging.getLogger().addHandler(fh)
    log.info("Log file: %s", fh.baseFilename)

# ── Constants ─────────────────────────────────────────────────────────────────
IOL_BASE   = "https://api.invertironline.com"
IOL_GW     = "https://gateway-api-internal.invertironline.com"
CLAUDE_EXE = r"C:\Users\Usuario\.local\bin\claude.exe"
ART        = timezone(timedelta(hours=-3))
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent
TRADES_LOG     = ROOT / "data" / "trades_log.json"
PORTFOLIO      = ROOT / "data" / "portfolio.json"
PENDING_ORDERS = ROOT / "data" / "pending_orders.json"
CONTEXT_MD     = SCRIPT_DIR / "trading_context.md"

# FIX P9: Comisiones (~0.6% + IVA) -> reducir presupuesto de compra en 0.7%
COMMISSION_FACTOR = 0.993

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
MAX_ITERATIONS   = int(os.environ.get("MAX_ITERATIONS", "0"))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


TELEGRAM_NOTIFY_STARTUP = _env_flag("TELEGRAM_NOTIFY_STARTUP", False)
TELEGRAM_NOTIFY_DRY_RUN = _env_flag("TELEGRAM_NOTIFY_DRY_RUN", False)

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
                    body_snippet = r.text[:500] if r.text else ""
                    log.error("HTTP %d on POST %s: %s", r.status_code, path, body_snippet)
                    raise requests.exceptions.HTTPError(
                        f"{r.status_code} {r.reason} | {body_snippet}", response=r
                    )
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
# FIX P4: Cache de históricos para no consultar Yahoo a cada iteración
_price_cache = {}

def _get_historical_prices(symbol: str, period_days: int = 60) -> list[float]:
    if not HAS_YFINANCE:
        return []
    if symbol in _price_cache:
        return _price_cache[symbol]
    try:
        ticker = yf.Ticker(symbol + ".BA")
        df = ticker.history(period=f"{period_days}d")
        if df.empty:
            return []
        closes = df["Close"].tolist()
        _price_cache[symbol] = closes
        return closes
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
_SCAN_UNIVERSE: list[tuple[str, str]] = [
    ("GGAL",  "ACCION"), ("BBAR",  "ACCION"), ("BMA",   "ACCION"),
    ("SUPV",  "ACCION"), ("BPAT",  "ACCION"), ("PAMP",  "ACCION"),
    ("CEPU",  "ACCION"), ("TGNO4", "ACCION"), ("TRAN",  "ACCION"),
    ("YPFD",  "ACCION"), ("TXAR",  "ACCION"), ("ALUA",  "ACCION"),
    ("CRES",  "ACCION"), ("COME",  "ACCION"), ("LOMA",  "ACCION"),
    ("TECO2", "ACCION"), ("MIRG",  "ACCION"), ("MORI",  "ACCION"),
    ("AAPL",  "CEDEAR"), ("GOOGL", "CEDEAR"), ("AMZN",  "CEDEAR"),
    ("MSFT",  "CEDEAR"), ("NVDA",  "CEDEAR"), ("KO",    "CEDEAR"),
    ("XOM",   "CEDEAR"), ("META",  "CEDEAR"),
]

def get_merval_tickers() -> list[tuple[str, str]]:
    log.info("Scanner: hardcoded universe (%d instruments).", len(_SCAN_UNIVERSE))
    return _SCAN_UNIVERSE

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

        # FIX P4: Usa caché (ya precargado si se llamó antes)
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
DEFAULTS = dict(DEFAULT_RULES)

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
    return parse_trading_context(CONTEXT_MD)

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

def load_pending_orders() -> list[dict]:
    if not PENDING_ORDERS.exists():
        return []
    try:
        data = json.loads(PENDING_ORDERS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as exc:
        log.error("Malformed pending_orders.json: %s", exc)
        return []

def save_pending_orders(orders: list[dict]) -> None:
    PENDING_ORDERS.parent.mkdir(exist_ok=True)
    PENDING_ORDERS.write_text(
        json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def sanitize_pending_orders(orders: list[dict], *, persist: bool = True) -> list[dict]:
    cutoff = datetime.now(ART) - timedelta(days=2)
    changed = False
    sanitized = []
    for order in orders:
        normalized = dict(order)
        if normalized.get("status") in ("pending", "executing"):
            try:
                ts = datetime.fromisoformat(normalized["timestamp"])
                if ts < cutoff:
                    normalized["status"] = "stale"
                    normalized["result"] = "Marked stale automatically after 2 days without resolution."
                    changed = True
            except Exception:
                normalized["status"] = "stale"
                normalized["result"] = "Marked stale automatically because timestamp is invalid."
                changed = True
        sanitized.append(normalized)

    if changed and persist:
        save_pending_orders(sanitized)
    return sanitized

def today_op_count(trade_log):
    today = datetime.now(ART).strftime("%Y-%m-%d")
    return sum(1 for t in trade_log
               if t.get("date", "").startswith(today)
               and t.get("status") == "executed")

# ── Balance ───────────────────────────────────────────────────────────────────
def get_cash(term="t1") -> float | None:
    try:
        data = iol.get("/api/v2/estadocuenta")
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected response type: {type(data).__name__}")
        snapshot = extract_cash_snapshot(data, term)
        log.info("Saldos disponibles: %s", snapshot["available_by_liquidation"])
        log.info(
            "Cash seleccionado (%s): $%s",
            snapshot["selected_liquidation"],
            f"{snapshot['available_to_trade']:,.2f}",
        )
        return snapshot["available_to_trade"]
    except Exception as exc:
        log.error("Balance fetch failed: %s", exc)
        send_telegram(
            "❌ *Trade Bot — ERROR de saldo*\n"
            f"No se pudo obtener el efectivo disponible:\n"
            f"`{_escape_md(str(exc)[:300])}`\n"
            "_Bot abortado — operar sin conocer el saldo real es peligroso\\._"
        )
    return None

# ── Portfolio helpers ─────────────────────────────────────────────────────────
def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _normalize_position_fields(pos: dict) -> None:
    qty = _safe_float(pos.get("quantity"))
    unit_price = _safe_float(pos.get("unit_price"))
    ppc = _safe_float(pos.get("ppc"), unit_price)
    total_value = qty * unit_price
    invested = qty * ppc
    gain = total_value - invested
    gain_pct = (gain / invested * 100) if invested > 0 else 0.0

    pos["quantity"] = qty
    pos["unit_price"] = unit_price
    pos["ppc"] = ppc
    pos["total_value"] = round(total_value, 2)
    pos["invested_ars"] = round(invested, 2)
    pos["gain_ars"] = round(gain, 2)
    pos["gain_pct"] = round(gain_pct, 2)

def refresh_portfolio_state(portfolio: dict, *, mark_updated: bool = True) -> dict:
    positions = portfolio.setdefault("positions", [])
    for pos in positions:
        _normalize_position_fields(pos)

    portfolio_syms = {
        pos.get("symbol") for pos in positions
        if pos.get("symbol") and _safe_float(pos.get("quantity")) > 0
    }
    watchlist = portfolio.get("watchlist", [])
    portfolio["watchlist"] = [
        item for item in watchlist
        if item.get("symbol") not in portfolio_syms
    ]

    total_ars = round(sum(_safe_float(pos.get("total_value")) for pos in positions), 2)
    invested_ars = round(sum(_safe_float(pos.get("invested_ars")) for pos in positions), 2)
    total_gain = round(total_ars - invested_ars, 2)
    total_gain_pct = round((total_gain / invested_ars * 100), 2) if invested_ars > 0 else 0.0
    pending_orders = sanitize_pending_orders(load_pending_orders())
    pending_count = sum(1 for order in pending_orders if order.get("status") in ("pending", "executing"))

    portfolio["total_ars"] = total_ars
    portfolio["invested_ars"] = invested_ars
    portfolio["total_gain"] = total_gain
    portfolio["total_gain_pct"] = total_gain_pct
    portfolio["total_positions"] = sum(1 for pos in positions if _safe_float(pos.get("quantity")) > 0)
    portfolio["pending_orders_count"] = pending_count
    portfolio["pending_orders"] = pending_orders
    if mark_updated:
        portfolio["last_updated"] = datetime.now(ART).strftime("%Y-%m-%d %H:%M")
    return portfolio

def save_portfolio(portfolio: dict) -> None:
    """Guarda el estado actual del portfolio al disco."""
    try:
        refresh_portfolio_state(portfolio)
        PORTFOLIO.parent.mkdir(exist_ok=True)
        PORTFOLIO.write_text(json.dumps(portfolio, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Portfolio guardado en %s", PORTFOLIO)
    except Exception as exc:
        log.error("Error guardando portfolio.json: %s", exc)

def update_portfolio_position(portfolio: dict, symbol: str, side: str, qty: int, price: float) -> None:
    """Actualiza la posición en el portfolio (compra/venta). Crea, modifica o elimina según corresponda."""
    positions = portfolio.setdefault("positions", [])
    if side == "buy":
        for pos in positions:
            if pos["symbol"] == symbol:
                old_qty = pos.get("quantity", 0)
                old_ppc = pos.get("ppc", price)
                new_qty = old_qty + qty
                new_ppc = ((old_ppc * old_qty) + (price * qty)) / new_qty if new_qty else price
                pos["quantity"] = new_qty
                pos["ppc"] = round(new_ppc, 6)
                log.info("Portfolio: actualizada posición %s: qty=%d, ppc=%.2f", symbol, new_qty, new_ppc)
                return
        positions.append({
            "symbol": symbol,
            "quantity": qty,
            "ppc": price,
            "unit_price": price,
            "rsi": None,
            "ma20": None
        })
        log.info("Portfolio: nueva posición %s: qty=%d, ppc=%.2f", symbol, qty, price)
    else:  # sell
        for pos in positions:
            if pos["symbol"] == symbol:
                old_qty = pos.get("quantity", 0)
                new_qty = max(0, old_qty - qty)
                if new_qty == 0:
                    positions.remove(pos)
                    log.info("Portfolio: posición %s eliminada (vendida toda).", symbol)
                else:
                    pos["quantity"] = new_qty
                    log.info("Portfolio: reducida posición %s: qty=%d", symbol, new_qty)
                return
        log.warning("Intento de venta de %s sin posición en portfolio.", symbol)

def sync_portfolio_from_api(portfolio: dict) -> bool:
    """Sincroniza posiciones contra IOL API al arrancar.

    Detecta compras manuales, corrige cantidades/PPC, elimina posiciones cerradas.
    Preserva RSI/MA20 locales. Guarda si hubo cambios.
    """
    try:
        raw = iol.get("/api/v2/portafolio/argentina")
        activos = raw.get("activos", raw.get("positions", []))
    except Exception as exc:
        log.warning("Portfolio sync falló: %s — usando datos locales", exc)
        return False

    api_positions: dict[str, dict] = {}
    for pos in activos:
        titulo  = pos.get("titulo", pos.get("asset", {}))
        symbol  = titulo.get("simbolo", titulo.get("symbol", ""))
        if not symbol:
            continue
        qty   = int(pos.get("cantidad", pos.get("quantity", 0)) or 0)
        ppc   = float(pos.get("ppc", 0) or 0)
        price = float(pos.get("ultimoPrecio", pos.get("unit_price", 0)) or 0)
        if qty > 0:
            api_positions[symbol] = {"quantity": qty, "ppc": ppc, "unit_price": price}

    local_by_sym: dict[str, dict] = {p["symbol"]: p for p in portfolio.get("positions", [])}
    changed = False

    # Agregar o actualizar desde API
    for symbol, api in api_positions.items():
        if symbol not in local_by_sym:
            log.info("Sync: posición nueva detectada %s qty=%d ppc=%.2f", symbol, api["quantity"], api["ppc"])
            portfolio.setdefault("positions", []).append({
                "symbol":     symbol,
                "quantity":   api["quantity"],
                "ppc":        api["ppc"],
                "unit_price": api["unit_price"],
                "rsi":        None,
                "ma20":       None,
            })
            changed = True
        else:
            local = local_by_sym[symbol]
            qty_diff = local.get("quantity", 0) != api["quantity"]
            ppc_diff = abs(local.get("ppc", 0) - api["ppc"]) > 0.01
            if qty_diff or ppc_diff:
                log.info("Sync: actualizada %s qty=%d→%d ppc=%.2f→%.2f",
                         symbol, local.get("quantity", 0), api["quantity"],
                         local.get("ppc", 0), api["ppc"])
                local["quantity"]   = api["quantity"]
                local["ppc"]        = api["ppc"]
                local["unit_price"] = api["unit_price"]
                changed = True

    # Eliminar posiciones cerradas (no aparecen en API)
    before = len(portfolio.get("positions", []))
    portfolio["positions"] = [
        p for p in portfolio.get("positions", [])
        if p["symbol"] in api_positions
    ]
    if len(portfolio["positions"]) < before:
        log.info("Sync: %d posición(es) eliminada(s) — no aparecen en IOL",
                 before - len(portfolio["positions"]))
        changed = True

    if changed:
        save_portfolio(portfolio)
        log.info("Portfolio sincronizado con IOL API (%d posiciones).", len(portfolio.get("positions", [])))
    else:
        log.info("Portfolio sin cambios vs IOL API.")
    return changed

# ── Order execution ────────────────────────────────────────────────────────────
def _queue_pending_order(symbol: str, side: str, qty: int, limit_price: float, term: str) -> tuple:
    import uuid
    try:
        existing = load_pending_orders()
    except Exception:
        existing = []

    # FIX P3: Limpiar órdenes zombie (>2 días de antigüedad)
    existing = sanitize_pending_orders(existing)

    # Check if previously queued order was filled by the interactive session
    for i, e in enumerate(existing):
        if e["symbol"] == symbol and e["side"] == side and e.get("status") == "done":
            order_id = e.get("order_id", "?")
            existing.pop(i)
            save_pending_orders(existing)
            log.info("Pending order CONSUMED: %s %s order_id=%s", side, symbol, order_id)
            return True, str(order_id), f"MCP ejecutó #{order_id}"

    # Already in queue — don't duplicate
    for e in existing:
        if e["symbol"] == symbol and e["side"] == side and e.get("status") in ("pending", "executing"):
            log.info("Order already queued [%s]: %s %s", e["status"], side, symbol)
            return False, None, f"awaiting MCP: {symbol} {side} ({e['status']})"

    # Enqueue new order
    order = {
        "id":          str(uuid.uuid4())[:8],
        "timestamp":   datetime.now(ART).isoformat(),
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "limit_price": limit_price,
        "term":        term,
        "status":      "pending",
        "order_id":    None,
        "result":      None,
    }
    existing.append(order)
    save_pending_orders(existing)
    log.info("Order QUEUED for MCP session: %s %s %d @ %.2f [#%s]",
             side, symbol, qty, limit_price, order["id"])
    return False, None, f"queued #{order['id']}"


def _place_order_gw(body: dict) -> tuple:
    """Try gateway-api-internal with short timeout (fast fail, 2 paths)."""
    with iol._lock:
        if not iol._token:
            iol._fetch_token()
        headers = {**_HEADERS, "Authorization": f"Bearer {iol._token}"}

    for path in ("/api/v2/operaciones/Validar", "/api/v2/operaciones"):
        try:
            r = requests.post(
                f"{IOL_GW}{path}", headers=headers, json=body, timeout=8
            )
            if r.status_code == 405:
                log.warning("GW %s → 405, trying next", path)
                continue
            if not r.ok:
                log.warning("GW %s → HTTP %d: %s", path, r.status_code, r.text[:200])
                if path == "/api/v2/operaciones/Validar":
                    continue
                return False, None, f"GW HTTP {r.status_code}"
            data = r.json()
            if path == "/api/v2/operaciones/Validar":
                vid = data.get("validacionId") or data.get("id")
                log.info("GW Validate OK: validacionId=%s", vid)
                # FIX P6: Incluir validacionId en el body de confirmación
                confirm_body = {**body, "validacionId": vid}
                r2 = requests.post(
                    f"{IOL_GW}/api/v2/operaciones/{vid}",
                    headers=headers, json=confirm_body, timeout=8
                )
                if r2.ok:
                    oid = str(r2.json().get("id", r2.json().get("numeroOperacion", "?")))
                    log.info("Order placed OK via GW: #%s", oid)
                    return True, oid, f"OK GW #{oid}"
                log.warning("GW place failed: HTTP %d", r2.status_code)
                return False, None, f"GW place HTTP {r2.status_code}"
            else:
                oid = str(data.get("id", data.get("numeroOperacion", "?")))
                log.info("Order placed OK via GW direct: #%s", oid)
                return True, oid, f"OK GW #{oid}"
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            log.warning("GW %s unreachable: %s", path, type(exc).__name__)
            # FIX P2: continuar con el siguiente path en lugar de salir
            continue
        except Exception as exc:
            log.warning("GW %s error: %s", path, exc)
            continue  # try next path
    return False, None, "GW: all paths failed"


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

    # Step 1: validate (v2) — 405 expected from external IPs, skip immediately
    validation_id = None
    try:
        val = iol.post("/api/v2/operaciones/Validar", body)
        validation_id = val.get("validacionId") or val.get("validation_id") or val.get("id")
        log.info("Validate OK: validacionId=%s", validation_id)
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 405:
            log.info("Validate 405 — trying GW then MCP")
            ok, oid, msg = _place_order_gw(body)
            if ok:
                return ok, oid, msg
            log.warning("GW failed (%s) — trying Claude MCP", msg)
            return _queue_pending_order(symbol, side, qty, limit_price, term)
        log.warning("Validate failed (%s) — proceeding with direct POST", exc)
    except Exception as exc:
        log.warning("Validate skipped (%s) — proceeding with direct POST", exc)

    # Step 2: place order via v2
    try:
        endpoint = f"/api/v2/operaciones/{validation_id}" if validation_id else "/api/v2/operaciones"
        resp = iol.post(endpoint, body)
        oid  = str(resp.get("id", resp.get("numeroOperacion", "?")))
        log.info("Order placed OK: #%s via %s", oid, endpoint)
        return True, oid, f"OK #{oid}"
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 405:
            log.warning("HTTP 405 on v2 POST — trying GW then MCP")
            ok, oid, msg = _place_order_gw(body)
            if ok:
                return ok, oid, msg
            log.warning("GW failed (%s) — trying Claude MCP", msg)
            return _queue_pending_order(symbol, side, qty, limit_price, term)
        err_str = str(exc).lower()
        if any(k in err_str for k in ("ddjj", "declaraci", "sworn", "jurada")):
            msg = f"DDJJ requerida para {symbol} — aceptar en app IOL"
            log.warning(msg)
            return False, None, msg
        return False, None, str(exc)
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
        "status":      "dry_run" if DRY_RUN else ("executed" if ok else ("queued" if msg.startswith(("queued #", "awaiting MCP")) else "failed")),
        "order_id":    oid,
        "message":     msg,
    }
    trade_log.append(entry)

    side_label = "COMPRA" if side == "buy" else "VENTA"
    is_queued  = not ok and msg.startswith(("queued #", "awaiting MCP", "MCP ejecutó"))
    icon       = ("🟢" if side == "buy" else "🔴") if ok else ("📋" if is_queued else "❌")

    qty_int = int(qty)
    if DRY_RUN and TELEGRAM_NOTIFY_DRY_RUN:
        send_telegram(
            f"🔵 *\\[SIMULACIÓN\\] {side_label} {_escape_md(symbol)}* — {_escape_md(reason.upper())}\n"
            f"Señal: {qty_int} acc a límite ${_escape_md(f'{limit_price:,.2f}')}\n"
            f"Precio ref: ${_escape_md(f'{price:,.0f}')}\n"
            "_bot en modo DRY RUN — no se ejecutó ninguna orden real_"
        )
    elif is_queued and not msg.startswith("awaiting"):
        send_telegram(
            f"📋 *ORDEN EN COLA: {side_label} {_escape_md(symbol)}* — {_escape_md(reason.upper())}\n"
            f"{qty_int} acc a límite ${_escape_md(f'{limit_price:,.2f}')}\n"
            f"Precio ref: ${_escape_md(f'{price:,.0f}')}\n"
            f"_Esperando ejecución en sesión Claude Code_ \\({_escape_md(msg)}\\)"
        )
    else:
        action = "Compré" if side == "buy" else "Vendí"
        detail = (f"✅ Orden #{_escape_md(str(oid))}" if ok
                  else f"❌ {_escape_md(msg[:200])}")
        send_telegram(
            f"{icon} *{side_label} {_escape_md(symbol)}* — {_escape_md(reason.upper())}\n"
            f"{action} {qty_int} acc a límite ${_escape_md(f'{limit_price:,.2f}')}\n"
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
    _setup_file_log(ROOT)
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
    sync_portfolio_from_api(portfolio)
    term    = str(rules["settlement_term"])
    max_ops = int(rules["max_ops_per_day"])
    slip    = float(rules["limit_slippage_pct"]) / 100

    # FIX P7: Cargar señales ya ejecutadas hoy para evitar recompras tras reinicio
    signals_done: set = set()
    trade_log_initial = load_log()
    today_str = now.strftime("%Y-%m-%d")
    for t in trade_log_initial:
        if t.get("date", "").startswith(today_str) and t.get("status") == "executed":
            side = t.get("side")
            sym  = t.get("symbol")
            reason = t.get("reason", "")
            if side in ("buy", "sell") and sym:
                signals_done.add((side, sym, reason))

    cash_init = get_cash(term)
    if cash_init is None:
        return

    if TELEGRAM_NOTIFY_STARTUP:
        send_telegram(
            f"🤖 *Trade Bot \\[{'SIMULACIÓN' if DRY_RUN else 'REAL'}\\] INICIADO*\n"
            f"⏱️ Intervalo: cada {LOOP_MINUTES} min \\| Límite diario: {max_ops} ops"
            + (f" \\| Iteraciones máx: {MAX_ITERATIONS}" if MAX_ITERATIONS > 0 else "")
            + "\n"
            f"💰 Cash inicial: ${_escape_md(f'{cash_init:,.0f}')} ARS"
        )

    # FIX P4: Precarga de históricos e indicadores para posiciones y watchlist
    all_items = portfolio.get("positions", []) + portfolio.get("watchlist", [])
    for item in all_items:
        sym = item["symbol"]
        live = get_live_price(sym)
        if live and live > 0:
            item["unit_price"] = live
        if HAS_YFINANCE:
            closes = _get_historical_prices(sym, period_days=60)
            if len(closes) >= 21:
                item["rsi"] = compute_rsi(closes, 14)
                item["ma20"] = compute_sma(closes, 20)

    # ── Bucle principal ──────────────────────────────────────────────────────
    iteration_count = 0
    while True:
        now = datetime.now(ART)
        if not byma_open():
            log.info("Mercado cerrado. Saliendo del bucle.")
            break

        if MAX_ITERATIONS > 0 and iteration_count >= MAX_ITERATIONS:
            log.info("Max iterations reached (%d). Saliendo del bucle.", MAX_ITERATIONS)
            break

        iteration_count += 1

        log.info("── Iteración %s ──", now.strftime("%H:%M"))

        cash = get_cash(term)
        if cash is None:
            time.sleep(60 * LOOP_MINUTES)
            continue

        usable     = max(0.0, cash - float(rules["cash_reserve_ars"]))
        # FIX P9: Descontar comisiones estimadas
        buy_budget = usable * float(rules["buy_cash_pct"]) / 100 * COMMISSION_FACTOR

        trade_log = load_log()
        ops_today = today_op_count(trade_log)

        if ops_today >= max_ops and not DRY_RUN:
            log.info("Límite diario alcanzado (%d/%d). Esperando...", ops_today, max_ops)
            time.sleep(60 * LOOP_MINUTES)
            continue

        # Refrescar solo precios en vivo (RSI/MA20 ya están precargados y no cambian intradía)
        for item in all_items:
            sym  = item["symbol"]
            live = get_live_price(sym)
            if live and live > 0:
                item["unit_price"] = live

        # ── 1) Stop-loss / Take-profit ─────────────────────────────────────
        for pos in portfolio.get("positions", []):
            sym   = pos["symbol"]
            price = pos.get("unit_price", 0)
            ppc   = pos.get("ppc", price) or price
            qty   = pos.get("quantity", 0)
            decision, _, reason = get_position_recommendation(
                sym, price, ppc, qty, pos.get("rsi"), pos.get("ma20"), rules, overrides
            )

            if decision == "VENDER" and reason == "stop-loss":
                if ("sell", sym, "stop-loss") not in signals_done:
                    lp = _round_to_tick(price * (1 - slip), "sell")
                    ok, oid, msg = place_order(sym, "sell", qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "sell", "stop-loss", qty, price, lp, ok, oid, msg)
                    if ok:
                        signals_done.add(("sell", sym, "stop-loss"))
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        _, count = _apply_fill(entry, fill, fqty, buy_budget, qty, lp, is_buy=False)
                        # FIX P1: Actualizar portfolio (venta)
                        if fill in ("ejecutada", "parcial") and count:
                            sold_qty = entry.get("quantity", qty)
                            update_portfolio_position(portfolio, sym, "sell", sold_qty, lp)
                continue

            if decision == "VENDER" and reason == "take-profit":
                if ("sell", sym, "take-profit") not in signals_done:
                    sell_qty = max(1, qty // 2)
                    lp = _round_to_tick(price * (1 - slip), "sell")
                    ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "sell", "take-profit", sell_qty, price, lp, ok, oid, msg)
                    if ok:
                        signals_done.add(("sell", sym, "take-profit"))
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        _, count = _apply_fill(entry, fill, fqty, buy_budget, sell_qty, lp, is_buy=False)
                        if fill in ("ejecutada", "parcial") and count:
                            sold_qty = entry.get("quantity", sell_qty)
                            update_portfolio_position(portfolio, sym, "sell", sold_qty, lp)
                continue

        # ── 2) RSI señales en posiciones ───────────────────────────────────
        for pos in portfolio.get("positions", []):
            sym   = pos["symbol"]
            price = pos.get("unit_price", 0)
            rsi   = pos.get("rsi")
            ma20  = pos.get("ma20")
            qty   = pos.get("quantity", 0)
            ppc   = pos.get("ppc", price) or price
            decision, _, reason = get_position_recommendation(
                sym, price, ppc, qty, rsi, ma20, rules, overrides
            )

            if decision == "COMPRAR" and reason == "RSI+MA20" and ("buy", sym, "RSI+MA20") not in signals_done:
                lp      = _round_to_tick(price * (1 + slip), "buy")
                buy_qty = max(1, int(buy_budget // lp))
                if buy_qty * lp <= buy_budget + 1e-6:
                    ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "buy", "RSI+MA20", buy_qty, price, lp, ok, oid, msg)
                    if ok:
                        signals_done.add(("buy", sym, "RSI+MA20"))
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                        if fill in ("ejecutada", "parcial") and count:
                            bought_qty = entry.get("quantity", buy_qty)
                            update_portfolio_position(portfolio, sym, "buy", bought_qty, lp)
                            ops_today += 1

            if decision == "VENDER" and reason == "RSI+MA20" and ("sell", sym, "RSI+MA20") not in signals_done:
                sell_qty = max(1, qty // 2)
                lp = _round_to_tick(price * (1 - slip), "sell")
                ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                entry = log_and_notify(trade_log, sym, "sell", "RSI+MA20", sell_qty, price, lp, ok, oid, msg)
                if ok:
                    signals_done.add(("sell", sym, "RSI+MA20"))
                if ok and not DRY_RUN:
                    fill, fqty = check_order_status(oid)
                    _, count = _apply_fill(entry, fill, fqty, buy_budget, sell_qty, lp, is_buy=False)
                    if fill in ("ejecutada", "parcial") and count:
                        sold_qty = entry.get("quantity", sell_qty)
                        update_portfolio_position(portfolio, sym, "sell", sold_qty, lp)
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
            decision, _, reason = get_watchlist_recommendation(
                sym, price, rsi, ma20, rules, overrides
            )

            if decision == "COMPRAR" and reason == "RSI+MA20 (watchlist)" and ("buy", sym, "RSI+MA20 (watchlist)") not in signals_done:
                lp      = _round_to_tick(price * (1 + slip), "buy")
                buy_qty = max(1, int(buy_budget // lp))
                if buy_qty * lp <= buy_budget + 1e-6:
                    ok, oid, msg = place_order(sym, "buy", buy_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "buy", "RSI+MA20 (watchlist)", buy_qty, price, lp, ok, oid, msg)
                    if ok:
                        signals_done.add(("buy", sym, "RSI+MA20 (watchlist)"))
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                        if fill in ("ejecutada", "parcial") and count:
                            bought_qty = entry.get("quantity", buy_qty)
                            update_portfolio_position(portfolio, sym, "buy", bought_qty, lp)
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
                # FIX P3: Solo marcar señal si fue exitosa o quedó encolada; si falló, permitir reintento
                if ok or (not ok and msg and (msg.startswith("queued #") or msg.startswith("awaiting MCP"))):
                    signals_done.add(("buy", sym, "market_scanner"))
                if ok and not DRY_RUN:
                    fill, fqty = check_order_status(oid)
                    buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                    if fill in ("ejecutada", "parcial") and count:
                        bought_qty = entry.get("quantity", buy_qty)
                        update_portfolio_position(portfolio, sym, "buy", bought_qty, lp)
                        ops_today += 1

        # Diagnostic state log
        for pos in portfolio.get("positions", []):
            sym  = pos["symbol"]
            rsi  = pos.get("rsi")
            ma20 = pos.get("ma20")
            px   = pos.get("unit_price", 0)
            ppc  = pos.get("ppc", px) or px
            qty  = pos.get("quantity", 0)
            ov   = overrides.get(sym, {})
            log.info(
                "STATE %s qty=%d price=%.2f ppc=%.2f rsi=%s ma20=%s "
                "no_sell=%s no_buy=%s cash=%.0f buy_budget=%.0f",
                sym, qty, px, ppc, f"{rsi:.2f}" if rsi else "N/A",
                f"{ma20:.2f}" if ma20 else "N/A",
                ov.get("no_sell", False), ov.get("no_buy", False),
                cash, buy_budget,
            )

        # FIX P1: Guardar portfolio actualizado (con posiciones, precios e indicadores)
        save_portfolio(portfolio)
        save_log(trade_log)

        if MAX_ITERATIONS > 0 and iteration_count >= MAX_ITERATIONS:
            log.info("Max iterations reached (%d). Finalizando sin espera adicional.", MAX_ITERATIONS)
            break

        log.info("Esperando %d min para siguiente iteración...", LOOP_MINUTES)
        time.sleep(60 * LOOP_MINUTES)

    log.info("Bot detenido. Iteraciones ejecutadas: %d", iteration_count)

if __name__ == "__main__":
    main()
