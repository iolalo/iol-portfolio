import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PENDING_ORDERS = DATA_DIR / "pending_orders.json"
TRADES_LOG = DATA_DIR / "trades_log.json"
PORTFOLIO = DATA_DIR / "portfolio.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pending_orders() -> list[dict]:
    data = load_json(PENDING_ORDERS, [])
    return data if isinstance(data, list) else []


def save_pending_orders(orders: list[dict]) -> None:
    save_json(PENDING_ORDERS, orders)


def load_trade_log() -> list[dict]:
    data = load_json(TRADES_LOG, [])
    if isinstance(data, dict):
        return data.get("trades", [])
    return data if isinstance(data, list) else []


def save_trade_log(items: list[dict]) -> None:
    save_json(TRADES_LOG, items)


def find_order(orders: list[dict], order_id: str) -> dict | None:
    return next((o for o in orders if str(o.get("id")) == order_id), None)


def print_orders(orders: list[dict], statuses: set[str] | None = None) -> int:
    rows = [
        o for o in orders
        if statuses is None or str(o.get("status", "")).lower() in statuses
    ]
    if not rows:
        print("No hay ordenes para mostrar.")
        return 0

    for o in rows:
        print(
            f"[{o.get('id')}] {o.get('status')} | {o.get('side')} {o.get('symbol')} "
            f"{o.get('qty')} @ {o.get('limit_price')} ({o.get('term')})"
        )
        if o.get("result"):
            print(f"  result: {o['result']}")
        if o.get("last_error"):
            print(f"  last_error: {o['last_error']}")
    return 0


def append_trade_resolution(order: dict, resolution: str, note: str, order_id: str | None) -> None:
    items = load_trade_log()
    items.append(
        {
            "date": now_iso(),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "reason": f"manual_{resolution}",
            "quantity": order.get("qty"),
            "price": order.get("limit_price"),
            "limit_price": order.get("limit_price"),
            "status": "executed" if resolution == "done" else "cancelled",
            "order_id": order_id,
            "message": note,
        }
    )
    save_trade_log(items)


def sync_portfolio_if_possible() -> None:
    if not PORTFOLIO.exists():
        print("portfolio.json no existe; omito sync.")
        return
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import trade_executor as te  # noqa: PLC0415

        te._setup_file_log(ROOT)
        te.iol.authenticate()
        portfolio = json.loads(PORTFOLIO.read_text(encoding="utf-8"))
        te.sync_portfolio_from_api(portfolio)
        te.refresh_portfolio_state(portfolio)
        te.save_portfolio(portfolio)
        print("Portfolio sincronizado contra IOL API.")
    except Exception as exc:
        print(f"No se pudo sincronizar portfolio: {exc}")


def cmd_list(args) -> int:
    statuses = set(s.strip().lower() for s in args.status.split(",")) if args.status else None
    return print_orders(load_pending_orders(), statuses)


def cmd_done(args) -> int:
    orders = load_pending_orders()
    order = find_order(orders, args.id)
    if not order:
        print(f"No encontre la orden {args.id}.")
        return 1

    order["status"] = "done"
    order["order_id"] = args.order_id
    order["result"] = args.note or f"Ejecutada manualmente en IOL #{args.order_id}"
    order["resolved_at"] = now_iso()
    save_pending_orders(orders)
    append_trade_resolution(order, "done", order["result"], args.order_id)
    print(f"Orden {args.id} marcada como done con order_id={args.order_id}.")
    if args.sync:
        sync_portfolio_if_possible()
    return 0


def cmd_reject(args) -> int:
    orders = load_pending_orders()
    order = find_order(orders, args.id)
    if not order:
        print(f"No encontre la orden {args.id}.")
        return 1

    order["status"] = "rejected_manual"
    order["result"] = args.note or "Rechazada manualmente"
    order["resolved_at"] = now_iso()
    save_pending_orders(orders)
    append_trade_resolution(order, "reject", order["result"], None)
    print(f"Orden {args.id} marcada como rechazada.")
    if args.sync:
        sync_portfolio_if_possible()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gestion manual de pending orders del bot IOL.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Lista ordenes pendientes.")
    p_list.add_argument(
        "--status",
        default="pending,approved_pending,executing",
        help="Estados a mostrar separados por coma.",
    )
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="Marca una orden como ejecutada manualmente.")
    p_done.add_argument("--id", required=True, help="ID interno de pending order.")
    p_done.add_argument("--order-id", required=True, help="Numero de orden real de IOL.")
    p_done.add_argument("--note", default="", help="Nota opcional.")
    p_done.add_argument("--sync", action="store_true", help="Sincroniza portfolio.json contra IOL API.")
    p_done.set_defaults(func=cmd_done)

    p_reject = sub.add_parser("reject", help="Marca una orden como rechazada manualmente.")
    p_reject.add_argument("--id", required=True, help="ID interno de pending order.")
    p_reject.add_argument("--note", default="", help="Nota opcional.")
    p_reject.add_argument("--sync", action="store_true", help="Sincroniza portfolio.json contra IOL API.")
    p_reject.set_defaults(func=cmd_reject)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
