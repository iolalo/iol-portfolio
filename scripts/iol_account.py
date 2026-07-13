from __future__ import annotations

from typing import Any

LIQUIDATION_MAP = {"t0": "inmediato", "t1": "hrs24", "t2": "hrs48"}
FALLBACK_ORDER = ["inmediato", "hrs24", "hrs48", "hrs72", "masHrs72"]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_cash_snapshot(account_data: dict[str, Any], term: str = "t1") -> dict[str, Any]:
    target_liq = LIQUIDATION_MAP.get(term, "hrs24")

    for cuenta in account_data.get("cuentas", []):
        moneda = (cuenta.get("moneda") or "").lower()
        if "peso" not in moneda:
            continue

        saldos = cuenta.get("saldos", [])
        saldos_by_liq = {
            saldo.get("liquidacion"): saldo
            for saldo in saldos
            if saldo.get("liquidacion")
        }
        available_by_liq = {
            liq: _safe_float(saldo.get("disponibleOperar", saldo.get("disponible")))
            for liq, saldo in saldos_by_liq.items()
        }

        selected_liq = target_liq
        available = available_by_liq.get(target_liq, 0.0)
        for liq in [target_liq] + [x for x in FALLBACK_ORDER if x != target_liq]:
            if liq not in available_by_liq:
                continue
            val = available_by_liq[liq]
            if val > 0 or liq == target_liq:
                selected_liq = liq
                available = val
                break

        return {
            "account_number": cuenta.get("numero"),
            "currency": cuenta.get("moneda"),
            "state": cuenta.get("estado"),
            "selected_liquidation": selected_liq,
            "available_to_trade": round(available, 2),
            "available_total": round(_safe_float(cuenta.get("disponible")), 2),
            "account_total": round(_safe_float(cuenta.get("total")), 2),
            "invested_value": round(_safe_float(cuenta.get("titulosValorizados")), 2),
            "available_by_liquidation": {
                liq: round(val, 2) for liq, val in available_by_liq.items()
            },
        }

    return {
        "account_number": None,
        "currency": None,
        "state": None,
        "selected_liquidation": LIQUIDATION_MAP.get(term, "hrs24"),
        "available_to_trade": 0.0,
        "available_total": 0.0,
        "account_total": 0.0,
        "invested_value": 0.0,
        "available_by_liquidation": {},
    }
