# Bot de Ventas Telegram — VitashopEc

Bot de Telegram que reporta las ventas de Chatea Pro (VitashopEc). Lee los datos
desde los campos de usuario de los suscriptores (el modulo de ordenes de shop
esta vacio en esta cuenta; las ventas viven en el chat en vivo).

Ademas integra **Dropi**: cada pedido aparece en Chatea Pro como un campo
`[Dropi] Datos de la orden <referencia>` cuyo valor es el JSON completo de la
orden de Dropi. El bot detecta las guias con estatus **"PARA RETIRO EN AGENCIA
SERVIENTREGA"** y lleva su seguimiento (dias sin retirar).

## Comandos

- `/start` — menu de ayuda
- `/resumen` — estadisticas del dia (ventas, facturacion, producto mas vendido)
- `/ventas` — lista de pedidos de hoy
- `/pedido <telefono>` — "Resumen del pedido - Datos de envio" de un cliente
- `/pendientes` — pedidos de hoy que aun no tienen datos de envio
- `/retiro` — guias de hoy entrantes para retiro en agencia Servientrega
- `/campos` — lista los campos de usuario disponibles (nombres de campos)
- `/seguimiento [ayer|dd/mm/aaaa|todos]` — CSV de las guias para retiro:
  - sin argumento → suscriptores recientes (ultimas 24h, rapido)
  - fecha pasada o `todos` → recorre TODOS los suscriptores (completo, lento)
- `/reporte_diario` — envia el CSV de seguimiento del dia (tambien se envia
  automatico a las 22:00 hora Ecuador)

## Como funciona

Usa **long polling** (getUpdates), asi que NO necesita webhook ni dominio
publico ni puerto. Solo control de acceso por chat id.

El bot guarda dos archivos locales:

- `retiros.json` — fecha en que el bot detecto por primera vez cada guia en
  retiro (fija el "dia 0" del seguimiento).
- `retiros_historico.json` — fila completa (incluido el JSON de Dropi) de cada
  guia detectada. Asi, aunque el suscriptor desaparezca de Chatea Pro, el
  reporte no pierde el pedido ni los dias sin retirar. Cuando la guia deja de
  aparecer en un barrido completo se marca `retirado_el`.

## Variables de entorno

| Variable | Descripcion |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot (@BotFather) |
| `TELEGRAM_ALLOWED_IDS` | Chat ids autorizados, separados por coma (ej: `629254023`) |
| `CHATEAPRO_API_TOKEN` | Token de la API de Chatea Pro |
| `CHATEAPRO_CACHE_TTL_SECONDS` | TTL del cache de campos (default `300`). Evita saturar el API |
| `CHECK_INTERVAL_SECONDS` | Intervalo del ciclo de revision (default `120`) |
| `REVISAR_NUEVOS_INTERVAL_SECONDS` | Cada cuanto re-consultar nuevos (default `1800`) |
| `SCAN_DELAY_SECONDS` | Pausa entre peticiones en escaneos grandes (default `2.0`) |
| `RETIROS_DB_PATH` | Ruta de `retiros.json` (default: junto al codigo) |
| `RETIROS_HISTORICO_PATH` | Ruta de `retiros_historico.json` (default: junto al codigo) |

## Despliegue en Coolify

1. Nueva Application desde el repo, **Base Directory** = `/ventas-bot`.
2. Build Pack: **Dockerfile**.
3. NO necesita dominio ni puerto (usa polling).
4. Configura las variables de entorno de arriba.
5. Mountea un **volumen persistente** para `retiros.json` y
   `retiros_historico.json` (o apunta `RETIROS_DB_PATH` /
   `RETIROS_HISTORICO_PATH` a una ruta con volumen). Sin volumen se resetean en
   cada deploy.
6. Deploy.

## Prueba local

```bash
pip3 install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_ALLOWED_IDS="629254023"
export CHATEAPRO_API_TOKEN="..."
python3 bot.py
```