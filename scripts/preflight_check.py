import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ART = timezone(timedelta(hours=-3))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PORTFOLIO = DATA_DIR / "portfolio.json"
TRADES_LOG = DATA_DIR / "trades_log.json"
PENDING_ORDERS = DATA_DIR / "pending_orders.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"__error__": f"{path.name}: {exc}"}


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main():
    issues = []
    portfolio = load_json(PORTFOLIO, {})
    trades = load_json(TRADES_LOG, [])
    pending = load_json(PENDING_ORDERS, [])

    if isinstance(portfolio, dict) and "__error__" in portfolio:
        issues.append(portfolio["__error__"])
        portfolio = {}
    if isinstance(trades, dict) and "__error__" in trades:
        issues.append(trades["__error__"])
        trades = []
    if isinstance(pending, dict) and "__error__" in pending:
        issues.append(pending["__error__"])
        pending = []

    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
    watchlist = portfolio.get("watchlist", []) if isinstance(portfolio, dict) else []

    seen = set()
    duplicates = set()
    for pos in positions:
        sym = pos.get("symbol")
        if sym in seen:
            duplicates.add(sym)
        seen.add(sym)
    if duplicates:
        issues.append(f"Duplicate positions: {', '.join(sorted(duplicates))}")

    watchlist_syms = {w.get('symbol') for w in watchlist if w.get("symbol")}
    overlap = sorted(sym for sym in seen if sym in watchlist_syms)
    if overlap:
        issues.append(f"Symbols present in both positions and watchlist: {', '.join(overlap)}")

    total_ars = round(sum(safe_float(pos.get("quantity")) * safe_float(pos.get("unit_price")) for pos in positions), 2)
    invested_ars = round(sum(safe_float(pos.get("quantity")) * safe_float(pos.get("ppc"), safe_float(pos.get("unit_price"))) for pos in positions), 2)
    if positions:
        stored_total = round(safe_float(portfolio.get("total_ars")), 2)
        stored_invested = round(safe_float(portfolio.get("invested_ars", portfolio.get("invested"))), 2)
        if abs(stored_total - total_ars) > 1:
            issues.append(f"Stored total_ars mismatch: file={stored_total} calc={total_ars}")
        if abs(stored_invested - invested_ars) > 1:
            issues.append(f"Stored invested_ars mismatch: file={stored_invested} calc={invested_ars}")

    active_pending = []
    stale_pending = []
    cutoff = datetime.now(ART) - timedelta(days=2)
    for order in pending if isinstance(pending, list) else []:
        status = order.get("status")
        if status in ("pending", "executing"):
            active_pending.append(order)
            ts_raw = order.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts < cutoff:
                    stale_pending.append(order)
            except Exception:
                issues.append(f"Pending order with invalid timestamp: {order.get('id', '?')}")
    if stale_pending:
        issues.append(f"Stale pending orders (>2d): {', '.join(o.get('id', '?') for o in stale_pending)}")

    today = datetime.now(ART).strftime("%Y-%m-%d")
    executed_today = 0
    for trade in trades if isinstance(trades, list) else []:
        if str(trade.get("date", "")).startswith(today) and trade.get("status") == "executed":
            executed_today += 1

    print("=== IOL Preflight ===")
    print(f"Positions: {len(positions)}")
    print(f"Watchlist: {len(watchlist)}")
    print(f"Portfolio total_ars: {total_ars}")
    print(f"Portfolio invested_ars: {invested_ars}")
    print(f"Pending active orders: {len(active_pending)}")
    print(f"Executed trades today: {executed_today}")
    if portfolio.get("last_updated"):
        print(f"Portfolio last_updated: {portfolio.get('last_updated')}")

    if issues:
        print("\nWARNINGS:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("\nNo issues detected.")


if __name__ == "__main__":
    main()
