import re
from pathlib import Path


DEFAULT_RULES = {
    "rsi_buy": 40.0,
    "rsi_sell": 65.0,
    "stop_loss_pct": 8.0,
    "take_profit_pct": 25.0,
    "buy_cash_pct": 70.0,
    "max_ops_per_day": 2,
    "cash_reserve_ars": 500.0,
    "settlement_term": "t1",
    "limit_slippage_pct": 0.5,
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


def parse_trading_context(context_path: str | Path):
    path = Path(context_path)
    rules = dict(DEFAULT_RULES)
    overrides = {}

    if not path.exists():
        return rules, overrides

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
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


def _position_signals(symbol: str, price: float, ppc: float, qty: float, rsi, ma20, rules: dict, ov: dict):
    if qty <= 0:
        return "MANTENER", [], None

    no_sell = ov.get("no_sell", False)
    no_buy = ov.get("no_buy", False)

    stop_loss_pct = float(ov.get("stop_loss_pct", rules["stop_loss_pct"]))
    take_profit_pct = float(ov.get("take_profit_pct", rules["take_profit_pct"]))
    rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
    rsi_sell = float(ov.get("rsi_sell", rules["rsi_sell"]))

    if not no_sell and ppc > 0 and price <= ppc * (1 - stop_loss_pct / 100):
        trigger = ppc * (1 - stop_loss_pct / 100)
        return "VENDER", [f"Stop-loss activo: precio <= ${trigger:,.0f}"], "stop-loss"

    if not no_sell and ppc > 0 and price >= ppc * (1 + take_profit_pct / 100):
        trigger = ppc * (1 + take_profit_pct / 100)
        return "VENDER", [f"Take-profit activo: precio >= ${trigger:,.0f}"], "take-profit"

    if not no_sell and rsi is not None and ma20 is not None and rsi > rsi_sell and price > ma20:
        return "VENDER", [f"RSI {rsi:.1f} > {rsi_sell:.0f} y precio sobre MA20 (${ma20:,.0f})"], "RSI+MA20"

    if not no_buy and rsi is not None and ma20 is not None and rsi < rsi_buy and price < ma20:
        return "COMPRAR", [f"RSI {rsi:.1f} < {rsi_buy:.0f} y precio bajo MA20 (${ma20:,.0f})"], "RSI+MA20"

    return "MANTENER", [], None


def get_position_recommendation(symbol: str, price: float, ppc: float, qty: float, rsi, ma20, rules: dict, overrides: dict):
    ov = overrides.get(symbol, {})
    return _position_signals(symbol, price, ppc, qty, rsi, ma20, rules, ov)


def get_watchlist_recommendation(symbol: str, price: float, rsi, ma20, rules: dict, overrides: dict):
    ov = overrides.get(symbol, {})
    if ov.get("no_buy", False):
        return "MANTENER", [], None

    rsi_buy = float(ov.get("rsi_buy", rules["rsi_buy"]))
    if rsi is not None and ma20 is not None and rsi < rsi_buy and price < ma20:
        return "COMPRAR", [f"RSI {rsi:.1f} < {rsi_buy:.0f} y precio bajo MA20 (${ma20:,.0f})"], "RSI+MA20 (watchlist)"

    return "MANTENER", [], None
