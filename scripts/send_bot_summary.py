"""
Envía al chat de Telegram un resumen fijo de la lógica del bot.
Uso: set TELEGRAM_TOKEN=... && set TELEGRAM_CHAT_ID=... && python send_bot_summary.py
"""
import os
import requests

TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MSG = """📋 *IOL Trading Bot — Lógica de decisiones*

*🛑 Stop-loss (prioridad 1)*
Si el precio actual cae ≥ 8 % por debajo del PPC (precio promedio de compra), vende TODA la posición con orden límite (precio × 0.995).
Objetivo: cortar pérdidas antes de que se profundicen.

*🎯 Take-profit (prioridad 2)*
Si el precio sube ≥ 25 % sobre el PPC, vende la MITAD de la posición.
Objetivo: realizar ganancia parcial dejando correr el resto.

*🟢 Señal de compra (prioridad 3)*
Condición doble:
• RSI(14) < 35 → activo sobrevendido
• Precio actual < MA20 → todavía por debajo de la media
Compra con el 70 % del efectivo disponible (reservando $500 ARS mínimo).
Orden límite = precio × 1.005 (0.5 % de slippage).

*🔴 Señal de venta (prioridad 4)*
Condición doble:
• RSI(14) > 65 → activo sobrecomprado
• Precio actual > MA20 → extendido sobre la media
Vende TODA la posición con orden límite = precio × 0.995.

*⚙️ Reglas generales*
• Máximo 2 operaciones por día
• Solo opera en horario BYMA: lun–vie 11:00–17:00 ART
• Liquidación T+1
• Aplica a todas las posiciones por igual (sin excepciones manuales)

*📊 Parámetros actuales*
RSI compra: 35 | RSI venta: 65
Stop-loss: 8 % | Take-profit: 25 %
Cash por operación: 70 % | Reserva mínima: $500 ARS"""

base = f"https://api.telegram.org/bot{TG_TOKEN}"

resp = requests.post(f"{base}/sendMessage", json={
    "chat_id":    TG_CHAT_ID,
    "text":       MSG,
    "parse_mode": "Markdown",
})
resp.raise_for_status()
msg_id = resp.json()["result"]["message_id"]
print("Enviado OK:", msg_id)

pin = requests.post(f"{base}/pinChatMessage", json={
    "chat_id":              TG_CHAT_ID,
    "message_id":           msg_id,
    "disable_notification": True,
})
if pin.ok:
    print("Fijado OK")
else:
    print("No se pudo fijar (el bot necesita ser admin del grupo):", pin.text)
