import json
import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

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
MOBILE_APPROVALS = ROOT / "data" / "mobile_approvals.json"
RECOMMENDATIONS = ROOT / "data" / "recommendations.json"
RECOMMENDATIONS_MD = ROOT / "data" / "recommendations.md"
RECOMMENDATIONS_STATE = ROOT / "data" / "recommendations_state.json"
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
IOL_WEB_NODE     = os.environ.get("IOL_WEB_NODE", "node").strip() or "node"
IOL_WEB_SCRIPT   = os.environ.get("IOL_WEB_SCRIPT", str(SCRIPT_DIR / "iol_web_order.js")).strip()
IOL_WEB_TIMEOUT  = int(os.environ.get("IOL_WEB_TIMEOUT_SECS", "180"))
DISABLE_TELEGRAM = os.environ.get("DISABLE_TELEGRAM", "false").strip().lower() in ("1", "true", "yes", "on")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


TELEGRAM_NOTIFY_STARTUP = _env_flag("TELEGRAM_NOTIFY_STARTUP", False)
TELEGRAM_NOTIFY_DRY_RUN = _env_flag("TELEGRAM_NOTIFY_DRY_RUN", False)
N8N_QUEUE_WEBHOOK_URL   = os.environ.get("N8N_QUEUE_WEBHOOK_URL", "").strip()
APPROVAL_BRIDGE_SECRET  = os.environ.get("APPROVAL_BRIDGE_SECRET", "").strip()
APPROVAL_BRIDGE_PORT    = int(os.environ.get("APPROVAL_BRIDGE_PORT", "8765"))
IOL_WEB_EXECUTOR_ENABLED = _env_flag("IOL_WEB_EXECUTOR_ENABLED", False)
REQUIRE_APPROVAL_CHANNEL = _env_flag("REQUIRE_APPROVAL_CHANNEL", True)

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
    if DISABLE_TELEGRAM:
        return
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


def _normalize_api_position(symbol: str, qty: int, ppc: float, price: float) -> tuple[int, float, float]:
    """Normaliza posiciones de IOL cuando hay corporate actions que dejan PPC viejo.

    Caso conocido:
    - MIRG 2026-08-04: IOL expone cantidad post-split pero PPC pre-split.
      Si no se corrige, el portfolio local muestra una pérdida falsa ~90% y el
      bot evalúa señales con base contaminada.
    """
    if (
        symbol == "MIRG"
        and qty >= 100
        and ppc > 10_000
        and 0 < price < 5_000
    ):
        adjusted_ppc = round(ppc / 10, 4)
        log.info(
            "Sync normalize: %s qty=%d ppc=%.2f -> %.4f por desajuste post-split",
            symbol, qty, ppc, adjusted_ppc,
        )
        return qty, adjusted_ppc, price
    return qty, ppc, price


def _is_pending_mcp_message(msg: str | None) -> bool:
    return bool(msg) and msg.startswith(("queued #", "awaiting MCP", "MCP ejecut"))


def _is_terminal_infra_failure(msg: str | None) -> bool:
    return bool(msg) and msg.startswith("Manual review unavailable:")


def _should_mark_signal_done(ok: bool, msg: str | None) -> bool:
    return ok or _is_pending_mcp_message(msg) or _is_terminal_infra_failure(msg)


def load_mobile_approvals() -> list[dict]:
    if not MOBILE_APPROVALS.exists():
        return []
    try:
        data = json.loads(MOBILE_APPROVALS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("Malformed mobile approvals file: %s", exc)
        return []


def save_mobile_approvals(items: list[dict]) -> None:
    MOBILE_APPROVALS.parent.mkdir(exist_ok=True)
    MOBILE_APPROVALS.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_trade_log_entry(entry: dict) -> None:
    trade_log = load_log()
    trade_log.append(entry)
    save_log(trade_log)


def _store_mobile_decision(payload: dict) -> None:
    items = load_mobile_approvals()
    payload = dict(payload)
    payload["received_at"] = datetime.now(ART).isoformat()
    items.append(payload)
    save_mobile_approvals(items)


def start_approval_bridge() -> None:
    if not APPROVAL_BRIDGE_SECRET:
        log.info("Approval bridge disabled: APPROVAL_BRIDGE_SECRET not set.")
        return

    class ApprovalHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("Approval bridge: " + fmt, *args)

        def _respond(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._respond(200, {"ok": True, "service": "approval-bridge"})
                return
            self._respond(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            if self.path != "/decision":
                self._respond(404, {"ok": False, "error": "not_found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._respond(400, {"ok": False, "error": "invalid_json"})
                return

            if payload.get("secret") != APPROVAL_BRIDGE_SECRET:
                self._respond(403, {"ok": False, "error": "forbidden"})
                return

            order_id = str(payload.get("id", "")).strip()
            decision = str(payload.get("decision", "")).strip().lower()
            if not order_id or decision not in ("approve", "reject"):
                self._respond(400, {"ok": False, "error": "invalid_payload"})
                return

            _store_mobile_decision({
                "id": order_id,
                "decision": decision,
                "source": payload.get("source", "n8n"),
                "message": payload.get("message", ""),
            })
            self._respond(200, {"ok": True, "id": order_id, "decision": decision})

    def _serve():
        server = ThreadingHTTPServer(("0.0.0.0", APPROVAL_BRIDGE_PORT), ApprovalHandler)
        log.info("Approval bridge listening on 0.0.0.0:%d", APPROVAL_BRIDGE_PORT)
        server.serve_forever()

    thread = threading.Thread(target=_serve, name="approval-bridge", daemon=True)
    thread.start()


def notify_n8n_pending_order(order: dict, reason: str) -> None:
    if not N8N_QUEUE_WEBHOOK_URL:
        return
    try:
        payload = {
            "id": order.get("id"),
            "timestamp": order.get("timestamp"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "limit_price": order.get("limit_price"),
            "term": order.get("term"),
            "reason": reason,
            "chat_id": TG_CHAT_ID,
        }
        r = requests.post(N8N_QUEUE_WEBHOOK_URL, json=payload, timeout=10)
        if not r.ok:
            log.warning("n8n webhook error %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("n8n webhook failed: %s", exc)


def _n8n_webhook_reachable(timeout_secs: float = 2.0) -> bool:
    if not N8N_QUEUE_WEBHOOK_URL:
        return False
    try:
        parsed = urlparse(N8N_QUEUE_WEBHOOK_URL)
        if not parsed.hostname:
            return False
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        with socket.create_connection((parsed.hostname, port), timeout=timeout_secs):
            return True
    except OSError:
        return False


def _manual_review_channel_available() -> tuple[bool, str]:
    if IOL_WEB_EXECUTOR_ENABLED:
        return True, "web_executor_enabled"
    if _n8n_webhook_reachable():
        return True, "n8n_webhook_reachable"
    if not N8N_QUEUE_WEBHOOK_URL:
        return False, "n8n_webhook_missing"
    return False, "n8n_webhook_unreachable"


def apply_mobile_decisions() -> None:
    items = load_mobile_approvals()
    if not items:
        return

    remaining = []
    orders = sanitize_pending_orders(load_pending_orders(), persist=False)
    changed = False

    for item in items:
        order_id = str(item.get("id", "")).strip()
        decision = str(item.get("decision", "")).strip().lower()
        matched = next((o for o in orders if o.get("id") == order_id), None)
        if not matched or decision not in ("approve", "reject"):
            remaining.append(item)
            continue
        current_status = str(matched.get("status", "")).strip().lower()
        if current_status not in ("pending", "approved_pending"):
            log.info(
                "Ignoring mobile decision %s for order %s with status=%s",
                decision, order_id, current_status or "?",
            )
            continue

        if decision == "reject":
            matched["status"] = "rejected_mobile"
            matched["result"] = "Rechazada desde Telegram"
            matched["resolved_at"] = datetime.now(ART).isoformat()
            changed = True
            log.info("Pending order %s rechazada desde mobile.", order_id)
            send_telegram(
                f"🚫 *ORDEN RECHAZADA* \\#{_escape_md(order_id)}\n"
                f"{_escape_md(matched.get('symbol','?'))} · {_escape_md(matched.get('side','?').upper())}\n"
                "_Rechazada desde Telegram_"
            )
            continue

        matched["mobile_approved_at"] = item.get("received_at")
        matched["status"] = "executing"
        matched["result"] = "Aprobada desde Telegram. Reintentando ejecución local..."
        matched["attempts"] = int(matched.get("attempts", 0) or 0) + 1
        matched["last_attempt_at"] = datetime.now(ART).isoformat()
        changed = True
        log.info("Pending order %s aprobada desde mobile.", order_id)
        send_telegram(
            f"✅ *ORDEN APROBADA* \\#{_escape_md(order_id)}\n"
            f"{_escape_md(matched.get('symbol','?'))} · {_escape_md(matched.get('side','?').upper())}\n"
            "_Aprobada desde Telegram\\. Reintentando ejecución local_"
        )

        ok, oid, msg = place_order(
            matched.get("symbol"),
            matched.get("side"),
            matched.get("qty"),
            matched.get("limit_price"),
            matched.get("term"),
            queue_on_fail=False,
        )
        matched["last_error"] = None if ok else msg
        if ok:
            matched["status"] = "done"
            matched["order_id"] = oid
            matched["result"] = msg
            matched["resolved_at"] = datetime.now(ART).isoformat()
            log.info("Pending order %s ejecutada tras aprobación mobile: #%s", order_id, oid)
            _append_trade_log_entry({
                "date": datetime.now(ART).isoformat(),
                "symbol": matched.get("symbol"),
                "side": matched.get("side"),
                "reason": "mobile_approval_retry",
                "quantity": matched.get("qty"),
                "price": matched.get("limit_price"),
                "limit_price": matched.get("limit_price"),
                "status": "executed",
                "order_id": oid,
                "message": msg,
            })
            send_telegram(
                f"✅ *ORDEN EJECUTADA* \\#{_escape_md(order_id)}\n"
                f"{_escape_md(matched.get('symbol','?'))} · {_escape_md(matched.get('side','?').upper())}\n"
                f"Orden IOL: \\#{_escape_md(str(oid))}"
            )
            continue

        matched["status"] = "approved_pending"
        matched["result"] = f"Aprobada desde Telegram, pero sigue pendiente: {msg}"
        log.warning("Pending order %s sigue pendiente tras aprobación mobile: %s", order_id, msg)
        send_telegram(
            f"⚠️ *ORDEN APROBADA PERO PENDIENTE* \\#{_escape_md(order_id)}\n"
            f"{_escape_md(matched.get('symbol','?'))} · {_escape_md(matched.get('side','?').upper())}\n"
            f"{_escape_md(str(msg)[:180])}"
        )

    if changed:
        save_pending_orders(orders)
    save_mobile_approvals(remaining)


def retry_approved_pending_orders() -> None:
    orders = sanitize_pending_orders(load_pending_orders(), persist=False)
    changed = False

    for order in orders:
        if str(order.get("status", "")).strip().lower() != "approved_pending":
            continue

        order_id = str(order.get("id", "?"))
        symbol = order.get("symbol")
        side = order.get("side")
        qty = order.get("qty")
        limit_price = order.get("limit_price")
        term = order.get("term")

        order["status"] = "executing"
        order["result"] = "Reintentando ejecución automática de orden aprobada..."
        order["attempts"] = int(order.get("attempts", 0) or 0) + 1
        order["last_attempt_at"] = datetime.now(ART).isoformat()
        changed = True
        save_pending_orders(orders)

        log.info(
            "Retrying approved pending order %s: %s %s qty=%s lp=%s",
            order_id, side, symbol, qty, limit_price,
        )

        ok, oid, msg = place_order(
            symbol,
            side,
            qty,
            limit_price,
            term,
            queue_on_fail=False,
        )
        order["last_error"] = None if ok else msg

        if ok:
            order["status"] = "done"
            order["order_id"] = oid
            order["result"] = msg
            order["resolved_at"] = datetime.now(ART).isoformat()
            log.info("Approved pending order %s executed automatically: #%s", order_id, oid)
            _append_trade_log_entry({
                "date": datetime.now(ART).isoformat(),
                "symbol": symbol,
                "side": side,
                "reason": "approved_pending_retry",
                "quantity": qty,
                "price": limit_price,
                "limit_price": limit_price,
                "status": "executed",
                "order_id": oid,
                "message": msg,
            })
            send_telegram(
                f"✅ *ORDEN EJECUTADA* \\#{_escape_md(order_id)}\n"
                f"{_escape_md(str(symbol or '?'))} · {_escape_md(str(side or '?').upper())}\n"
                "_Reintento automático de orden aprobada_\n"
                f"Orden IOL: \\#{_escape_md(str(oid))}"
            )
        else:
            order["status"] = "approved_pending"
            order["result"] = f"Sigue pendiente tras reintento automático: {msg}"
            log.warning("Approved pending order %s still pending after auto retry: %s", order_id, msg)

    if changed:
        save_pending_orders(orders)

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
        if normalized.get("status") in ("pending", "executing", "approved_pending"):
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
    pending_count = sum(
        1 for order in pending_orders
        if order.get("status") in ("pending", "executing", "approved_pending")
    )

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


def save_recommendations(payload: dict) -> None:
    try:
        RECOMMENDATIONS.parent.mkdir(exist_ok=True)
        RECOMMENDATIONS.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Recommendations saved to %s", RECOMMENDATIONS)
    except Exception as exc:
        log.error("Error guardando recommendations.json: %s", exc)


def save_recommendations_markdown(payload: dict) -> None:
    try:
        lines = [
            f"# Advisor Plan - {payload.get('generated_at', '')}",
            "",
            f"- Cash disponible: ${payload.get('cash_available_ars', 0):,.2f}",
            f"- Presupuesto sugerido de compra: ${payload.get('buy_budget_ars', 0):,.2f}",
            f"- Presupuesto restante: ${payload.get('remaining_buy_budget_ars', 0):,.2f}",
            "",
            "## Resumen",
        ]
        for item in payload.get("summary", {}).get("top_actions", []):
            lines.append(f"- {item}")

        sells = payload.get("sell_recommendations", [])
        buys = payload.get("buy_recommendations", [])
        holds = payload.get("hold_positions", [])

        lines.extend(["", "## Vender"])
        if sells:
            for item in sells:
                lines.append(
                    f"- {item['symbol']}: vender {item['suggested_quantity']} acc "
                    f"({item['position_pct']:.0f}% de la posicion, prioridad {item['priority']}) "
                    f"a limite ${item['limit_price']:,.2f} por {item['reason']}"
                )
        else:
            lines.append("- Sin ventas sugeridas.")

        lines.extend(["", "## Comprar"])
        if buys:
            for item in buys:
                lines.append(
                    f"- {item['symbol']}: invertir ${item['suggested_amount_ars']:,.2f} "
                    f"({item['suggested_quantity']} acc a limite ${item['limit_price']:,.2f}, "
                    f"prioridad {item['priority']}) [{item['source']}]"
                )
        else:
            lines.append("- Sin compras sugeridas.")

        lines.extend(["", "## Mantener"])
        if holds:
            for item in holds:
                lines.append(
                    f"- {item['symbol']}: mantener. Precio ${item['current_price']:,.2f} | PPC ${item['ppc']:,.2f}"
                )
        else:
            lines.append("- Sin posiciones para mantener.")

        RECOMMENDATIONS_MD.parent.mkdir(exist_ok=True)
        RECOMMENDATIONS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Recommendations markdown saved to %s", RECOMMENDATIONS_MD)
    except Exception as exc:
        log.error("Error guardando recommendations.md: %s", exc)


def _round_sell_quantity(qty_int: int, pct: float) -> int:
    if qty_int <= 0:
        return 0
    raw_qty = max(1, math.ceil(qty_int * pct))
    if qty_int <= 10:
        return min(qty_int, raw_qty)
    rounded = int(math.ceil(raw_qty / 5.0) * 5)
    return min(qty_int, max(1, rounded))


def _sell_qty_for_reason(
    qty: float,
    reason: str | None,
    gain_pct: float | None = None,
    position_weight_pct: float = 0.0,
    position_value_ars: float = 0.0,
    price: float = 0.0,
) -> int:
    qty_int = max(0, int(qty))
    if qty_int <= 0:
        return 0

    gain = 0.0 if gain_pct is None else float(gain_pct)
    exposure = float(position_weight_pct)
    value = float(position_value_ars)

    # Proxy simple de liquidez por tamaño/precio de la posición.
    liquidity_penalty = 0.0
    if price > 0 and price < 100 and value < 30_000:
        liquidity_penalty = 0.05

    if reason == "stop-loss":
        pct = 0.60
        if gain <= -12:
            pct += 0.15
        if gain <= -20:
            pct += 0.10
        if exposure >= 20:
            pct += 0.10
        if exposure >= 35:
            pct += 0.05
        pct = min(1.0, pct)
        return _round_sell_quantity(qty_int, max(0.50, pct - liquidity_penalty))

    if reason == "take-profit":
        pct = 0.20
        if gain >= 12:
            pct += 0.10
        if gain >= 20:
            pct += 0.10
        if exposure >= 20:
            pct += 0.10
        if exposure >= 35:
            pct += 0.10
        pct = min(0.60, pct)
        return _round_sell_quantity(qty_int, max(0.15, pct - liquidity_penalty))

    if reason == "RSI+MA20":
        pct = 0.25
        if gain > 0:
            pct += 0.05
        if gain >= 10:
            pct += 0.10
        if exposure >= 20:
            pct += 0.10
        if exposure >= 35:
            pct += 0.10
        if gain <= -5:
            pct -= 0.10
        pct = max(0.15, min(0.50, pct))
        return _round_sell_quantity(qty_int, max(0.10, pct - liquidity_penalty))

    return qty_int


def _sell_priority(reason: str | None) -> str:
    return {
        "stop-loss": "alta",
        "take-profit": "media",
        "RSI+MA20": "media",
    }.get(reason, "baja")


def _buy_priority(score: float) -> str:
    if score >= 1.12:
        return "alta"
    if score >= 0.95:
        return "media"
    return "baja"


def _candidate_buy_score(source: str, item: dict) -> float:
    base = {
        "position_add": 1.0,
        "watchlist": 0.9,
        "scanner": 0.8,
    }.get(source, 0.5)
    rsi = _safe_float(item.get("rsi"))
    ma20 = _safe_float(item.get("ma20"))
    price = _safe_float(item.get("price", item.get("unit_price")))
    if ma20 > 0 and price > 0 and price < ma20:
        base += min(0.25, (ma20 - price) / ma20)
    if rsi > 0:
        base += min(0.2, max(0.0, (40.0 - rsi) / 100.0))
    return round(base, 4)


def _position_weight(total_ars: float, position_value: float) -> float:
    if total_ars <= 0:
        return 0.0
    return round((position_value / total_ars) * 100, 2)


def _buy_penalty_for_concentration(symbol: str, position_value: float, total_ars: float) -> tuple[float, str | None]:
    weight = _position_weight(total_ars, position_value)
    if weight >= 45:
        return 0.22, f"concentracion actual alta ({weight:.1f}% del portfolio)"
    if weight >= 35:
        return 0.12, f"concentracion actual elevada ({weight:.1f}% del portfolio)"
    if weight >= 25:
        return 0.05, f"concentracion moderada ({weight:.1f}% del portfolio)"
    return 0.0, None


def _allocation_weights(count: int) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [1.0]
    if count == 2:
        return [0.6, 0.4]
    return [0.5, 0.3, 0.2][:count]


def _load_recommendation_state() -> dict:
    if not RECOMMENDATIONS_STATE.exists():
        return {}
    try:
        data = json.loads(RECOMMENDATIONS_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_recommendation_state(state: dict) -> None:
    try:
        RECOMMENDATIONS_STATE.parent.mkdir(exist_ok=True)
        RECOMMENDATIONS_STATE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("No se pudo guardar recommendations_state.json: %s", exc)


def _advisor_signature(payload: dict) -> str:
    normalized = {
        "sell_recommendations": payload.get("sell_recommendations", []),
        "buy_recommendations": payload.get("buy_recommendations", []),
        "summary": payload.get("summary", {}),
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def notify_advisor_plan(payload: dict) -> None:
    if DISABLE_TELEGRAM or not TG_TOKEN or not TG_CHAT_ID:
        return

    signature = _advisor_signature(payload)
    state = _load_recommendation_state()
    if state.get("last_signature") == signature:
        log.info("Advisor Telegram unchanged; skipping notification.")
        return

    sells = payload.get("sell_recommendations", [])
    buys = payload.get("buy_recommendations", [])
    generated_at = payload.get("generated_at", "")

    lines = [
        "🧠 *Sugerencias IOL*",
        f"_{_escape_md(generated_at)}_",
        f"💰 Cash: ${_escape_md(f'{payload.get('cash_available_ars', 0):,.2f}')}",
        f"🛒 Presupuesto compra: ${_escape_md(f'{payload.get('buy_budget_ars', 0):,.2f}')}",
    ]

    if sells:
        lines.append("")
        lines.append("*Vender*")
        for item in sells[:5]:
            lines.append(
                f"• {_escape_md(item['symbol'])}: {int(item['suggested_quantity'])} acc "
                f"\\({_escape_md(item['priority'])}, {_escape_md(item['reason'])}, "
                f"{_escape_md(f'{item['position_pct']:.0f}')}% pos\\)"
            )

    if buys:
        lines.append("")
        lines.append("*Comprar*")
        for item in buys[:5]:
            lines.append(
                f"• {_escape_md(item['symbol'])}: {int(item['suggested_quantity'])} acc "
                f"por ${_escape_md(f'{item['suggested_amount_ars']:,.0f}')} "
                f"\\({_escape_md(item['priority'])}\\)"
            )

    if not sells and not buys:
        lines.append("")
        lines.append("_Sin cambios sugeridos por ahora\\._")

    send_telegram("\n".join(lines))
    _save_recommendation_state({
        "last_signature": signature,
        "last_sent_at": generated_at,
    })
    log.info("Advisor Telegram notification sent.")


def build_advisor_plan(
    portfolio: dict,
    rules: dict,
    overrides: dict,
    cash: float,
    buy_budget: float,
    slip: float,
    portfolio_syms: set[str],
    scanner_opportunities: list[dict] | None = None,
) -> dict:
    generated_at = datetime.now(ART).strftime("%Y-%m-%d %H:%M:%S")
    sales: list[dict] = []
    buys: list[dict] = []
    hold: list[dict] = []
    total_ars = _safe_float(portfolio.get("total_ars"))
    position_values = {
        pos.get("symbol"): _safe_float(pos.get("total_value"))
        for pos in portfolio.get("positions", [])
        if pos.get("symbol")
    }

    for pos in portfolio.get("positions", []):
        sym = pos.get("symbol")
        if not sym:
            continue
        qty = _safe_float(pos.get("quantity"))
        price = _safe_float(pos.get("unit_price"))
        ppc = _safe_float(pos.get("ppc"), price)
        gain_pct = pos.get("gain_pct")
        position_value = position_values.get(sym, 0.0)
        position_weight = _position_weight(total_ars, position_value)
        rsi = pos.get("rsi")
        ma20 = pos.get("ma20")
        decision, signals, reason = get_position_recommendation(
            sym, price, ppc, qty, rsi, ma20, rules, overrides
        )
        if decision == "VENDER":
            sell_qty = _sell_qty_for_reason(
                qty,
                reason,
                gain_pct=gain_pct,
                position_weight_pct=position_weight,
                position_value_ars=position_value,
                price=price,
            )
            lp = _round_to_tick(price * (1 - slip), "sell") if price > 0 else 0.0
            sales.append({
                "symbol": sym,
                "priority": _sell_priority(reason),
                "reason": reason,
                "signals": signals,
                "quantity": sell_qty,
                "suggested_quantity": sell_qty,
                "position_quantity": int(qty),
                "position_pct": round((sell_qty / qty) * 100, 2) if qty > 0 else 0.0,
                "portfolio_weight_pct": position_weight,
                "current_price": round(price, 4),
                "limit_price": lp,
                "ppc": round(ppc, 4),
                "gain_pct": None if gain_pct is None else round(float(gain_pct), 2),
                "estimated_value_ars": round(sell_qty * lp, 2),
            })
        elif decision == "COMPRAR":
            lp = _round_to_tick(price * (1 + slip), "buy") if price > 0 else 0.0
            raw_score = _candidate_buy_score("position_add", pos)
            penalty, penalty_note = _buy_penalty_for_concentration(
                sym, position_values.get(sym, 0.0), total_ars
            )
            final_score = round(max(0.0, raw_score - penalty), 4)
            enriched_signals = list(signals)
            if penalty_note:
                enriched_signals.append(f"Penalizacion por {penalty_note}")
            buys.append({
                "symbol": sym,
                "source": "position_add",
                "reason": reason,
                "signals": enriched_signals,
                "current_price": round(price, 4),
                "limit_price": lp,
                "rsi": None if rsi is None else round(float(rsi), 2),
                "ma20": None if ma20 is None else round(float(ma20), 2),
                "score": final_score,
            })
        else:
            hold.append({
                "symbol": sym,
                "action": "hold",
                "current_price": round(price, 4),
                "ppc": round(ppc, 4),
            })

    for item in portfolio.get("watchlist", []):
        sym = item.get("symbol")
        if not sym or sym in portfolio_syms:
            continue
        price = _safe_float(item.get("unit_price"))
        rsi = item.get("rsi")
        ma20 = item.get("ma20")
        decision, signals, reason = get_watchlist_recommendation(
            sym, price, rsi, ma20, rules, overrides
        )
        if decision != "COMPRAR":
            continue
        lp = _round_to_tick(price * (1 + slip), "buy") if price > 0 else 0.0
        buys.append({
            "symbol": sym,
            "source": "watchlist",
            "reason": reason,
            "signals": signals,
            "current_price": round(price, 4),
            "limit_price": lp,
            "rsi": None if rsi is None else round(float(rsi), 2),
            "ma20": None if ma20 is None else round(float(ma20), 2),
            "score": _candidate_buy_score("watchlist", item),
        })

    if scanner_opportunities:
        for opp in scanner_opportunities[:3]:
            sym = opp.get("symbol")
            if not sym:
                continue
            lp = _round_to_tick(_safe_float(opp.get("price")) * (1 + slip), "buy")
            buys.append({
                "symbol": sym,
                "source": "scanner",
                "reason": "market_scanner",
                "signals": [
                    f"Score scanner {opp.get('score')}",
                    f"RSI {opp.get('rsi')} | MA20 ${opp.get('ma20')}",
                ],
                "current_price": round(_safe_float(opp.get("price")), 4),
                "limit_price": lp,
                "rsi": opp.get("rsi"),
                "ma20": opp.get("ma20"),
                "score": max(_candidate_buy_score("scanner", opp), _safe_float(opp.get("score"))),
            })

    buys.sort(key=lambda item: (-_safe_float(item.get("score")), item.get("symbol", "")))
    sales.sort(
        key=lambda item: (
            {"stop-loss": 0, "take-profit": 1, "RSI+MA20": 2}.get(item.get("reason"), 9),
            item.get("symbol", ""),
        )
    )

    available_budget = round(max(0.0, buy_budget), 2)
    top_buys = buys[:3]
    weights = _allocation_weights(len(top_buys))
    allocated_buys: list[dict] = []
    remaining_budget = available_budget
    for idx, item in enumerate(top_buys):
        lp = _safe_float(item.get("limit_price"))
        if lp <= 0:
            continue
        target_amount = round(available_budget * weights[idx], 2)
        if idx == len(top_buys) - 1:
            target_amount = round(remaining_budget, 2)
        qty = int(target_amount // lp)
        if qty <= 0:
            continue
        amount = round(qty * lp, 2)
        remaining_budget = round(max(0.0, remaining_budget - amount), 2)
        allocated = dict(item)
        allocated["suggested_amount_ars"] = amount
        allocated["suggested_quantity"] = qty
        allocated["priority"] = _buy_priority(_safe_float(allocated.get("score")))
        allocated_buys.append(allocated)

    summary_lines = []
    if sales:
        summary_lines.append(
            "Vender: " + ", ".join(
                f"{item['symbol']} ({item['quantity']} acc, {item['reason']})"
                for item in sales[:3]
            )
        )
    if allocated_buys:
        summary_lines.append(
            "Comprar: " + ", ".join(
                f"{item['symbol']} (${item['suggested_amount_ars']:,.0f}, {item['suggested_quantity']} acc)"
                for item in allocated_buys
            )
        )
    if not summary_lines:
        summary_lines.append("Sin cambios sugeridos; mantener cartera actual.")

    return {
        "generated_at": generated_at,
        "mode": "advisor",
        "cash_available_ars": round(cash, 2),
        "buy_budget_ars": available_budget,
        "remaining_buy_budget_ars": remaining_budget,
        "sell_recommendations": sales,
        "buy_recommendations": allocated_buys,
        "hold_positions": hold,
        "summary": {
            "sell_count": len(sales),
            "buy_count": len(allocated_buys),
            "top_actions": summary_lines,
        },
    }

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
        qty, ppc, price = _normalize_api_position(symbol, qty, ppc, price)
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

    if not REQUIRE_APPROVAL_CHANNEL:
        msg = "Autonomous mode: pending approval queue disabled"
        log.warning("Skipping queue for %s %s - %s", side, symbol, msg)
        return False, None, msg

    if REQUIRE_APPROVAL_CHANNEL:
        channel_ok, channel_reason = _manual_review_channel_available()
        if not channel_ok:
            msg = f"Manual review unavailable: {channel_reason}"
            log.warning("Skipping queue for %s %s — %s", side, symbol, msg)
            return False, None, msg

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
        if e["symbol"] == symbol and e["side"] == side and e.get("status") in ("pending", "executing", "approved_pending"):
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
    notify_n8n_pending_order(order, "queued_for_manual_review")
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


def _place_order_direct_v2(body: dict, validation_id: str | None = None) -> tuple:
    endpoints = []
    if validation_id:
        endpoints.append(f"/api/v2/operaciones/{validation_id}")
    endpoints.append("/api/v2/operaciones")

    last_http_error = None
    for endpoint in endpoints:
        try:
            resp = iol.post(endpoint, body)
            oid = str(resp.get("id", resp.get("numeroOperacion", "?")))
            log.info("Order placed OK: #%s via %s", oid, endpoint)
            return True, oid, f"OK #{oid}"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body_snippet = exc.response.text[:200] if exc.response is not None and exc.response.text else str(exc)
            log.warning("Direct POST failed on %s: HTTP %d %s", endpoint, status, body_snippet)
            last_http_error = exc
            if status == 405:
                continue
            err_str = str(exc).lower()
            if any(k in err_str for k in ("ddjj", "declaraci", "sworn", "jurada")):
                msg = f"DDJJ requerida para {body.get('simbolo')} — aceptar en app IOL"
                log.warning(msg)
                return False, None, msg
        except Exception as exc:
            log.warning("Direct POST error on %s: %s", endpoint, exc)
            return False, None, str(exc)

    if last_http_error is not None:
        return False, None, str(last_http_error)
    return False, None, "Direct v2: no viable endpoint"


def _place_order_web(symbol, side, qty, limit_price, term) -> tuple:
    if not IOL_WEB_EXECUTOR_ENABLED:
        return False, None, "WEB executor disabled"

    script_path = Path(IOL_WEB_SCRIPT)
    if not script_path.exists():
        return False, None, f"WEB script missing: {script_path}"

    payload = {
        "symbol": symbol,
        "side": side,
        "qty": int(qty),
        "limit_price": round(float(limit_price), 6),
        "term": term,
    }
    try:
        result = subprocess.run(
            [IOL_WEB_NODE, str(script_path), json.dumps(payload, ensure_ascii=False)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=IOL_WEB_TIMEOUT,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return False, None, f"WEB timeout after {IOL_WEB_TIMEOUT}s"
    except Exception as exc:
        return False, None, f"WEB launcher error: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stderr:
        log.warning("WEB executor stderr: %s", stderr[:500])

    parsed = None
    if stdout:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if parsed:
        status = str(parsed.get("status", "")).strip().lower()
        order_id = parsed.get("order_id")
        message = str(parsed.get("message") or parsed.get("error") or stdout[:300]).strip()
        if status in ("executed", "confirmed"):
            return True, str(order_id or "WEB"), message
        if status == "prepared":
            return False, None, f"WEB prepared: {message}"
        return False, None, f"WEB failed: {message}"

    if result.returncode == 0:
        return False, None, stdout[:300] or "WEB executor finished without structured output"
    return False, None, stderr[:300] or stdout[:300] or f"WEB executor rc={result.returncode}"


def place_order(symbol, side, qty, limit_price, term, *, queue_on_fail: bool = True):
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
            log.info("Validate 405 — trying direct POST v2 without validation")
            ok, oid, msg = _place_order_direct_v2(body)
            if ok:
                return ok, oid, msg
            log.warning("Direct POST failed (%s) — trying GW then MCP", msg)
            ok, oid, msg = _place_order_gw(body)
            if ok:
                return ok, oid, msg
            log.warning("GW failed (%s) — trying WEB then Claude MCP", msg)
            ok, oid, web_msg = _place_order_web(symbol, side, qty, limit_price, term)
            if ok:
                return ok, oid, web_msg
            if web_msg and web_msg != "WEB executor disabled":
                log.warning("WEB fallback failed (%s) — trying Claude MCP", web_msg)
            if queue_on_fail:
                return _queue_pending_order(symbol, side, qty, limit_price, term)
            return False, None, f"Pendiente tras reintento local: {msg}"
        log.warning("Validate failed (%s) — proceeding with direct POST", exc)
    except Exception as exc:
        log.warning("Validate skipped (%s) — proceeding with direct POST", exc)

    # Step 2: place order via v2
    ok, oid, msg = _place_order_direct_v2(body, validation_id)
    if ok:
        return ok, oid, msg
    if "DDJJ requerida" in str(msg):
        return False, None, msg
    try:
        raise requests.exceptions.HTTPError(msg)
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 405 or "405" in str(msg):
            log.warning("HTTP 405 on v2 POST — trying GW then MCP")
            ok, oid, msg = _place_order_gw(body)
            if ok:
                return ok, oid, msg
            log.warning("GW failed (%s) — trying WEB then Claude MCP", msg)
            ok, oid, web_msg = _place_order_web(symbol, side, qty, limit_price, term)
            if ok:
                return ok, oid, web_msg
            if web_msg and web_msg != "WEB executor disabled":
                log.warning("WEB fallback failed (%s) — trying Claude MCP", web_msg)
            if queue_on_fail:
                return _queue_pending_order(symbol, side, qty, limit_price, term)
            return False, None, f"Pendiente tras reintento local: {msg}"
        err_str = str(msg).lower()
        if any(k in err_str for k in ("ddjj", "declaraci", "sworn", "jurada")):
            msg = f"DDJJ requerida para {symbol} — aceptar en app IOL"
            log.warning(msg)
            return False, None, msg
        return False, None, msg
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
        "status":      "dry_run" if DRY_RUN else ("executed" if ok else ("queued" if _is_pending_mcp_message(msg) else "failed")),
        "order_id":    oid,
        "message":     msg,
    }
    trade_log.append(entry)

    side_label = "COMPRA" if side == "buy" else "VENTA"
    is_queued  = not ok and _is_pending_mcp_message(msg)
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
    elif is_queued:
        pass
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
    start_approval_bridge()
    now = datetime.now(ART)
    log.info("Time ART: %s (weekday=%d) | DRY_RUN=%s | holidays_lib=%s | yfinance=%s",
             now.strftime("%Y-%m-%d %H:%M"), now.weekday(), DRY_RUN, HAS_HOLIDAYS, HAS_YFINANCE)

    if not byma_open():
        log.info("BYMA closed — skipping.")
        return

    rules, overrides = parse_context()
    log.info("Rules: %s", rules)
    channel_ok, channel_reason = _manual_review_channel_available()
    log.info(
        "Manual review channel: %s (%s) | REQUIRE_APPROVAL_CHANNEL=%s",
        "available" if channel_ok else "unavailable",
        channel_reason,
        REQUIRE_APPROVAL_CHANNEL,
    )

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
        if not t.get("date", "").startswith(today_str):
            continue
        side = t.get("side")
        sym  = t.get("symbol")
        reason = t.get("reason", "")
        if side not in ("buy", "sell") or not sym:
            continue
        if t.get("status") in ("executed", "queued") or _is_terminal_infra_failure(t.get("message")):
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
        apply_mobile_decisions()
        retry_approved_pending_orders()

        cash = get_cash(term)
        if cash is None:
            time.sleep(60 * LOOP_MINUTES)
            continue

        usable     = max(0.0, cash - float(rules["cash_reserve_ars"]))
        # FIX P9: Descontar comisiones estimadas
        buy_budget = usable * float(rules["buy_cash_pct"]) / 100 * COMMISSION_FACTOR

        trade_log = load_log()
        ops_today = today_op_count(trade_log)
        advisor_scanner_opportunities: list[dict] = []

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
                    if _should_mark_signal_done(ok, msg):
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
                    sell_qty = qty
                    lp = _round_to_tick(price * (1 - slip), "sell")
                    ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                    entry = log_and_notify(trade_log, sym, "sell", "take-profit", sell_qty, price, lp, ok, oid, msg)
                    if _should_mark_signal_done(ok, msg):
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
                    if _should_mark_signal_done(ok, msg):
                        signals_done.add(("buy", sym, "RSI+MA20"))
                    if ok and not DRY_RUN:
                        fill, fqty = check_order_status(oid)
                        buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                        if fill in ("ejecutada", "parcial") and count:
                            bought_qty = entry.get("quantity", buy_qty)
                            update_portfolio_position(portfolio, sym, "buy", bought_qty, lp)
                            ops_today += 1

            if decision == "VENDER" and reason == "RSI+MA20" and ("sell", sym, "RSI+MA20") not in signals_done:
                sell_qty = qty
                lp = _round_to_tick(price * (1 - slip), "sell")
                ok, oid, msg = place_order(sym, "sell", sell_qty, lp, term)
                entry = log_and_notify(trade_log, sym, "sell", "RSI+MA20", sell_qty, price, lp, ok, oid, msg)
                if _should_mark_signal_done(ok, msg):
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
                    if _should_mark_signal_done(ok, msg):
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
            advisor_scanner_opportunities = scan_market(rules, overrides, portfolio_syms, scan_budget, signals_done)
            for opp in advisor_scanner_opportunities:
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
                if _should_mark_signal_done(ok, msg):
                    signals_done.add(("buy", sym, "market_scanner"))
                if ok and not DRY_RUN:
                    fill, fqty = check_order_status(oid)
                    buy_budget, count = _apply_fill(entry, fill, fqty, buy_budget, buy_qty, lp, is_buy=True)
                    if fill in ("ejecutada", "parcial") and count:
                        bought_qty = entry.get("quantity", buy_qty)
                        update_portfolio_position(portfolio, sym, "buy", bought_qty, lp)
                        ops_today += 1

        if scan_budget > 0 and not advisor_scanner_opportunities:
            advisor_scanner_opportunities = scan_market(
                rules, overrides, portfolio_syms, scan_budget, set()
            )

        advisor_plan = build_advisor_plan(
            portfolio,
            rules,
            overrides,
            cash,
            buy_budget,
            slip,
            portfolio_syms,
            advisor_scanner_opportunities,
        )
        portfolio["advisor_recommendations"] = {
            "generated_at": advisor_plan["generated_at"],
            "sell_count": advisor_plan["summary"]["sell_count"],
            "buy_count": advisor_plan["summary"]["buy_count"],
            "top_actions": advisor_plan["summary"]["top_actions"],
        }
        save_recommendations(advisor_plan)
        save_recommendations_markdown(advisor_plan)
        notify_advisor_plan(advisor_plan)

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
        log.info("ADVISOR %s", " | ".join(advisor_plan["summary"]["top_actions"]))

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
