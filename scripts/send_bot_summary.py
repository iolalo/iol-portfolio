"""
Envia al chat de Telegram un resumen fijo de la logica del bot.
Uso: python send_bot_summary.py
"""
import os
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent.parent


def _load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_local_env()

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DISABLE_TELEGRAM = os.environ.get("DISABLE_TELEGRAM", "").strip().lower() in ("1", "true", "yes", "on")

MSG = """📋 *IOL Trading Bot - Logica de decisiones*

*Stop-loss (prioridad 1)*
Si el precio actual cae >= 8 % por debajo del PPC, vende toda la posicion con orden limite (precio x 0.995).
Objetivo: cortar perdidas antes de que se profundicen.

*Take-profit (prioridad 2)*
Si el precio sube >= 25 % sobre el PPC, vende la mitad de la posicion.
Objetivo: realizar ganancia parcial dejando correr el resto.

*Senal de compra (prioridad 3)*
Condicion doble:
- RSI(14) < 35
- Precio actual < MA20
Compra con el 70 % del efectivo disponible, reservando $500 ARS minimo.
Orden limite = precio x 1.005.

*Senal de venta (prioridad 4)*
Condicion doble:
- RSI(14) > 65
- Precio actual > MA20
Vende toda la posicion con orden limite = precio x 0.995.

*Reglas generales*
- Maximo 2 operaciones por dia
- Solo opera en horario BYMA: lunes a viernes, 11:00 a 17:00 ART
- Liquidacion T+1
- Aplica a todas las posiciones por igual

*Parametros actuales*
RSI compra: 35 | RSI venta: 65
Stop-loss: 8 % | Take-profit: 25 %
Cash por operacion: 70 % | Reserva minima: $500 ARS"""


def main() -> None:
    if DISABLE_TELEGRAM:
        print("Telegram disabled via DISABLE_TELEGRAM=true. Bot summary not sent.")
        return
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")

    base = f"https://api.telegram.org/bot{TG_TOKEN}"

    resp = requests.post(
        f"{base}/sendMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "text": MSG,
            "parse_mode": "Markdown",
        },
        timeout=10,
    )
    resp.raise_for_status()
    msg_id = resp.json()["result"]["message_id"]
    print("Enviado OK:", msg_id)

    pin = requests.post(
        f"{base}/pinChatMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "message_id": msg_id,
            "disable_notification": True,
        },
        timeout=10,
    )
    if pin.ok:
        print("Fijado OK")
    else:
        print("No se pudo fijar:", pin.text)


if __name__ == "__main__":
    main()
