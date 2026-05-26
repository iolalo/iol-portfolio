import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

IOL_BASE     = "https://api.invertironline.com"
ART          = timezone(timedelta(hours=-3))
SCRIPT_DIR   = Path(__file__).parent
ROOT         = SCRIPT_DIR.parent
CACHE_DIR    = ROOT / "data" / "cache"
SIGNALS_FILE = ROOT / "data" / "last_signals.json"

# ── Environment validation ────────────────────────────────────────────────────

def _require_env(*names):
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)

_require_env("IOL_USERNAME", "IOL_PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")

IOL_USER   = os.environ["IOL_USERNAME"]
IOL_PASS   = os.environ["IOL_PASSWORD"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

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


iol = _IOLSession()

# ── Config ────────────────────────────────────────────────────────────────────

def load_json_config(filename):
    path = SCRIPT_DIR / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("Malformed JSON in %s: %s — using empty config", filename, exc)
        return {}

# ── Historical cache ──────────────────────────────────────────────────────────

def _cache_path(symbol, date_str):
    return CACHE_DIR / f"{symbol}_{date_str}.json"

def _load_cache(symbol, date_str):
    p = _cache_path(symbol, date_str)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def _save_cache(symbol, date_str, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(symbol, date_str).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )

# ── Historical data ───────────────────────────────────────────────────────────

def _yf_historical(symbol, from_date, to_date):
    if not HAS_YF:
        return []
    try:
        df = yf.download(
            symbol + ".BA",
            start=from_date.strftime("%Y-%m-%d"),
            end=(to_date + timedelta(days=1)).strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return []
        close = df["Close"].squeeze()
        return sorted(
            [{"date": str(d.date()), "close": round(float(v), 2)}
             for d, v in zip(close.index, close.values)
             if v == v],
            key=lambda x: x["date"],
        )
    except Exception as exc:
        log.warning("yfinance fallback failed for %s: %s", symbol, exc)
        return []


def get_historical(symbol, from_date, to_date):
    today_str = to_date.strftime("%Y-%m-%d")
    cached = _load_cache(symbol, today_str)
    if cached is not None:
        log.debug("Cache hit: %s", symbol)
        return cached

    path = (f"/api/v2/bCBA/Titulos/{symbol}/SeriesHistoricas"
            f"/ajustada/{from_date.strftime('%Y-%m-%d')}/{today_str}/dia")
    try:
        raw = iol.get(path)
    except Exception as exc:
        log.warning("Historical API error for %s: %s — trying yfinance", symbol, exc)
        result = _yf_historical(symbol, from_date, to_date)
        if result:
            _save_cache(symbol, today_str, result)
        return result

    if isinstance(raw, dict):
        for key in ("historico", "data", "series", "items", "values", "candles", "bars"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
        else:
            log.warning("Unexpected historical response for %s — trying yfinance", symbol)
            result = _yf_historical(symbol, from_date, to_date)
            if result:
                _save_cache(symbol, today_str, result)
            return result

    if not isinstance(raw, list) or not raw:
        result = _yf_historical(symbol, from_date, to_date)
        if result:
            _save_cache(symbol, today_str, result)
        return result

    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        close = item.get("cierre") or item.get("close") or item.get("ultimo")
        if not close:
            continue
        fecha = (item.get("fechaHora") or item.get("fecha")
                 or item.get("date") or item.get("time"))
        if isinstance(fecha, str):
            date_str = fecha[:10]
        elif isinstance(fecha, (int, float)):
            date_str = datetime.fromtimestamp(fecha, tz=ART).strftime("%Y-%m-%d")
        else:
            date_str = ""
        result.append({"date": date_str, "close": round(float(close), 2)})

    result.sort(key=lambda x: x["date"])
    if not result:
        result = _yf_historical(symbol, from_date, to_date)
    if result:
        _save_cache(symbol, today_str, result)
    return result

# ── Indicators ────────────────────────────────────────────────────────────────

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0.0:
        return 100.0 if avg_g > 0 else 50.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def calculate_ma(prices, period=20):
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)


def get_recommendation(rsi, price, ma20, daily_pct):
    if daily_pct is not None and daily_pct <= -5:
        return "ALERTA", [f"Caída brusca {daily_pct:+.2f}% hoy"]
    signals = []
    votes   = {"COMPRAR": 0, "VENDER": 0}
    if rsi is not None:
        if rsi < 35:
            votes["COMPRAR"] += 2
            signals.append(f"RSI sobrevendido ({rsi})")
        elif rsi > 65:
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


def analyze_ticker(symbol, current_price, daily_pct, buy_date, today):
    hist_from = buy_date if buy_date else (today - timedelta(days=90))
    hist = get_historical(symbol, hist_from, today)

    closes = [h["close"] for h in hist]
    dates  = [h["date"]  for h in hist]

    today_str = today.strftime("%Y-%m-%d")
    if dates and dates[-1] == today_str:
        # Always override today's close with the live price to keep signals aligned
        closes[-1] = current_price
    else:
        closes.append(current_price)
        dates.append(today_str)

    rsi  = calculate_rsi(closes)
    ma20 = calculate_ma(closes)
    recommendation, signals = get_recommendation(rsi, current_price, ma20, daily_pct)

    sparkline    = [{"date": d, "close": c} for d, c in zip(dates[-30:], closes[-30:])]
    full_history = [{"date": d, "close": c} for d, c in zip(dates, closes)]

    return rsi, ma20, recommendation, signals, sparkline, full_history

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    MAX = 4000
    if len(text) <= MAX:
        chunks = [text]
    else:
        chunks = []
        current = ""
        for block in text.split("\n\n"):
            if len(current) + len(block) + 2 > MAX:
                if current:
                    chunks.append(current.strip())
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            chunks.append(current.strip())

    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "Markdown"},
                timeout=10,
            )
            if not r.ok:
                log.warning("Telegram error %d: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

# ── Signal deduplication ──────────────────────────────────────────────────────

def load_last_signals():
    if SIGNALS_FILE.exists():
        try:
            return json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_last_signals(signals):
    SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNALS_FILE.write_text(
        json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── Parallel workers ──────────────────────────────────────────────────────────

def _analyze_position(pos, buy_dates, today):
    try:
        titulo      = pos.get("titulo", pos.get("asset", {}))
        symbol      = titulo.get("simbolo", titulo.get("symbol", ""))
        description = titulo.get("descripcion", titulo.get("description", symbol))
        currency    = titulo.get("moneda", titulo.get("currency", "ARS"))
        quantity    = pos.get("cantidad",    pos.get("quantity", 0))
        price       = pos.get("ultimoPrecio", pos.get("unit_price", pos.get("lot_price", 0)))

        raw_var   = pos.get("variacion", pos.get("daily_change_pct"))
        daily_pct = float(raw_var) if raw_var is not None else 0.0

        ppc      = pos.get("ppc", price) or price
        gain_pct = pos.get("gananciaPorcentaje", pos.get("gain_pct")) or 0.0
        if gain_pct == 0 and ppc and ppc > 0:
            gain_pct = round((price - ppc) / ppc * 100, 2)

        buy_date_str = buy_dates.get(symbol)
        buy_date     = datetime.strptime(buy_date_str, "%Y-%m-%d") if buy_date_str else None

        log.info("  Analyzing %s (price=%.2f, ppc=%.2f)...", symbol, price, ppc)
        rsi, ma20, rec, signals, sparkline, full_history = analyze_ticker(
            symbol, price, daily_pct, buy_date, today
        )

        return {
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
    except Exception as exc:
        log.error("Error analyzing position: %s", exc)
        return None


def _analyze_watchlist_item(symbol, buy_dates, today):
    try:
        buy_date_str = buy_dates.get(symbol)
        buy_date     = datetime.strptime(buy_date_str, "%Y-%m-%d") if buy_date_str else None
        hist_from    = buy_date if buy_date else (today - timedelta(days=90))
        hist         = get_historical(symbol, hist_from, today)

        if len(hist) < 2:
            log.info("  [SKIP] Insufficient data for %s", symbol)
            return None

        price     = hist[-1]["close"]
        prev      = hist[-2]["close"]
        daily_pct = round((price - prev) / prev * 100, 2) if prev else 0.0

        log.info("  Analyzing watchlist %s (price=%.2f)...", symbol, price)
        rsi, ma20, rec, signals, sparkline, full_history = analyze_ticker(
            symbol, price, daily_pct, buy_date, today
        )

        return {
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
    except Exception as exc:
        log.error("Error analyzing watchlist %s: %s", symbol, exc)
        return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_art = datetime.now(ART)
    today   = now_art.replace(tzinfo=None)

    buy_dates = load_json_config("buy_dates.json")
    watchlist = load_json_config("watchlist.json")
    if isinstance(watchlist, dict):
        watchlist = list(watchlist.keys())
    if not isinstance(watchlist, list):
        watchlist = []

    log.info("Authenticating...")
    iol.authenticate()

    log.info("Fetching portfolio...")
    raw     = iol.get("/api/v2/portafolio/argentina")
    activos = raw.get("activos", raw.get("positions", []))

    last_signals    = load_last_signals()
    current_signals = {}

    # ── Portfolio positions (parallel) ────────────────────────────────────────
    positions: list = []
    portfolio_syms: set = set()

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_analyze_position, pos, buy_dates, today): pos
            for pos in activos
        }
        for fut in as_completed(futures):
            data = fut.result()
            if data:
                positions.append(data)
                portfolio_syms.add(data["symbol"])

    positions.sort(key=lambda p: p["symbol"])

    # ── Totals per currency ───────────────────────────────────────────────────
    ars_pos = [p for p in positions if "usd" not in (p["currency"] or "").lower()]
    usd_pos = [p for p in positions if "usd"     in (p["currency"] or "").lower()]

    total_ars      = sum(p["total_value"] for p in ars_pos)
    invested_ars   = sum(p["ppc"] * p["quantity"] for p in ars_pos)
    total_gain     = round(total_ars - invested_ars, 2)
    gain_pct_total = round((total_gain / invested_ars * 100) if invested_ars > 0 else 0.0, 2)

    total_usd    = sum(p["total_value"] for p in usd_pos)
    invested_usd = sum(p["ppc"] * p["quantity"] for p in usd_pos)

    # ── Watchlist (parallel) ──────────────────────────────────────────────────
    watchlist_data: list = []
    watchlist_syms = [s for s in watchlist if s not in portfolio_syms]

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_analyze_watchlist_item, sym, buy_dates, today): sym
            for sym in watchlist_syms
        }
        for fut in as_completed(futures):
            data = fut.result()
            if data:
                watchlist_data.append(data)

    watchlist_data.sort(key=lambda w: w["symbol"])

    # ── Alerts — only notify on signal changes ────────────────────────────────
    alerts: list = []
    for item in positions + watchlist_data:
        sym = item["symbol"]
        rec = item["recommendation"]
        current_signals[sym] = rec
        if rec in ("COMPRAR", "VENDER", "ALERTA") and last_signals.get(sym) != rec:
            alerts.append(item)
        elif rec in ("COMPRAR", "VENDER", "ALERTA"):
            log.info("  [SKIP] %s: signal %s unchanged", sym, rec)

    save_last_signals(current_signals)

    # ── Save JSON ─────────────────────────────────────────────────────────────
    output = {
        "last_updated":   now_art.strftime("%Y-%m-%d %H:%M"),
        "total_ars":      round(total_ars, 2),
        "invested_ars":   round(invested_ars, 2),
        "total_gain":     total_gain,
        "total_gain_pct": gain_pct_total,
        "total_usd":      round(total_usd, 2),
        "invested_usd":   round(invested_usd, 2),
        "alert_count":    len(alerts),
        "positions":      positions,
        "watchlist":      watchlist_data,
    }

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "portfolio.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Saved data/portfolio.json")

    # ── Telegram ──────────────────────────────────────────────────────────────
    emoji      = {"COMPRAR": "🟢", "VENDER": "🔴", "ALERTA": "⚠️", "MANTENER": "⚪"}
    gain_emoji = "📈" if total_gain >= 0 else "📉"

    summary = (
        f"📊 *IOL Portfolio — {now_art.strftime('%d/%m/%Y %H:%M')} ART*\n\n"
        f"💰 Total ARS: *${total_ars:,.0f}*\n"
        f"💼 Invertido: ${invested_ars:,.0f}\n"
        f"{gain_emoji} Resultado: ${total_gain:+,.0f} ({gain_pct_total:+.2f}%)\n"
    )
    if usd_pos:
        usd_gain = round(total_usd - invested_usd, 2)
        summary += f"🌎 USD: ${total_usd:,.2f} (inv. ${invested_usd:,.2f} | G/P ${usd_gain:+,.2f})\n"

    if alerts:
        port_alerts  = [a for a in alerts if "quantity" in a]
        watch_alerts = [a for a in alerts if "quantity" not in a]
        lines = [summary, "🔔 *Alertas nuevas:*\n"]
        for a in port_alerts + watch_alerts:
            e = emoji.get(a["recommendation"], "⚪")
            lines.append(f"{e} *{a['symbol']}* — {a['recommendation']}")
            lines.append(f"Precio: ${a['unit_price']:,.0f} | Día: {a['daily_change_pct']:+.2f}%")
            if a["rsi"] is not None and a["ma20"] is not None:
                lines.append(f"RSI: {a['rsi']} | MA20: ${a['ma20']:,.0f}")
            for s in a["signals"]:
                lines.append(f"  • {s}")
            lines.append("")
        send_telegram("\n".join(lines))
    else:
        send_telegram(summary + "✅ Sin alertas nuevas")

    log.info(
        "Done. %d new alert(s). ARS: $%s | Ganancia: $%+.0f (%+.2f%%)",
        len(alerts), f"{total_ars:,.0f}", total_gain, gain_pct_total,
    )


if __name__ == "__main__":
    main()
