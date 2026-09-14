# Bot de Ventas Telegram — VitashopEc

Bot de Telegram que reporta las ventas de Chatea Pro (VitashopEc). Lee los datos
desde los campos de usuario de los suscriptores (el modulo de ordenes de shop
esta vacio en esta cuenta; las ventas viven en el chat en vivo).

## Comandos

- `/start` — menu de ayuda
- `/resumen` — estadisticas del dia (ventas, facturacion, producto mas vendido)
- `/ventas` — lista de pedidos de hoy
- `/pedido <telefono>` — "Resumen del pedido - Datos de envio" de un cliente

## Como funciona

Usa **long polling** (getUpdates), asi que NO necesita webhook ni dominio
publico ni puerto. Solo control de acceso por chat id.

## Variables de entorno

| Variable | Descripcion |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot (@BotFather) |
| `TELEGRAM_ALLOWED_IDS` | Chat ids autorizados, separados por coma (ej: `629254023`) |
| `CHATEAPRO_API_TOKEN` | Token de la API de Chatea Pro |

## Despliegue en Coolify

1. Nueva Application desde el repo, **Base Directory** = `/ventas-bot`.
2. Build Pack: **Dockerfile**.
3. NO necesita dominio ni puerto (usa polling).
4. Configura las 3 variables de entorno de arriba.
5. Deploy.

## Prueba local

```bash
pip3 install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_ALLOWED_IDS="629254023"
export CHATEAPRO_API_TOKEN="..."
python3 bot.py
```
