"""
Bot de Telegram de ventas para VitashopEc / Chatea Pro.

Comandos:
  /start    -> mensaje de bienvenida
  /resumen  -> estadisticas de ventas del dia (ventas, facturacion, top producto)
  /ventas   -> lista de pedidos del dia (nombre, ciudad, valor)
  /pendientes -> pedidos del dia cuyo Resumen menciona 'pendiente'
  /pedido <telefono>  -> "Resumen del pedido - Datos de envio" de un cliente

Usa long polling (getUpdates), asi que NO necesita webhook ni dominio publico.
Se controla el acceso con AUTHORIZED_CHAT_IDS (solo tu chat puede usarlo).

Variables de entorno:
  TELEGRAM_BOT_TOKEN     token del bot (@BotFather)
  TELEGRAM_ALLOWED_IDS   ids de chat autorizados, separados por coma
  CHATEAPRO_API_TOKEN    token de la API de Chatea Pro
"""

import os
import time
import html

import requests

import chateapro

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

ALLOWED = {
    x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",")
    if x.strip()
}


def enviar(chat_id, texto):
    requests.post(f"{TG_API}/sendMessage", data={
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
    }, timeout=30)


def autorizado(chat_id):
    # Si no se configuran ids permitidos, se permite a todos (no recomendado).
    if not ALLOWED:
        return True
    return str(chat_id) in ALLOWED


def _parse_arg_fecha(texto):
    """
    Extrae una fecha del comando: 'ayer' o 'dd/mm/aaaa'.
    Si no hay argumento, devuelve None (=> hoy en Ecuador).
    """
    import re
    import datetime as dt
    partes = texto.split(maxsplit=1)
    if len(partes) < 2:
        return None
    arg = partes[1].strip().lower()
    if arg == "ayer":
        return chateapro.hoy_ecuador() - dt.timedelta(days=1)
    if arg in ("hoy", ""):
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", arg)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None
    return None


def manejar_comando(chat_id, texto):
    texto = (texto or "").strip()
    cmd = texto.split()[0].lower() if texto else ""

    if cmd in ("/start", "/ayuda", "/help"):
        enviar(chat_id,
               "👋 *Bot de ventas VitashopEc*\n\n"
               "Comandos disponibles:\n"
               "• /resumen — ventas de hoy\n"
               "• /resumen dd/mm/aaaa — ventas de una fecha\n"
               "• /ventas — lista de pedidos\n"
               "• /ventas dd/mm/aaaa — pedidos por fecha\n"
               "• /pendientes — pedidos pendientes\n"
               "• /pedido <teléfono> — datos de envío de un cliente")
        return

    if cmd == "/resumen":
        fecha = _parse_arg_fecha(texto)
        enviar(chat_id, "⏳ Calculando resumen...")
        try:
            enviar(chat_id, chateapro.resumen_del_dia(fecha))
        except Exception as e:
            enviar(chat_id, f"❌ Error al obtener el resumen: {e}")
        return

    if cmd == "/ventas":
        fecha = _parse_arg_fecha(texto)
        enviar(chat_id, "⏳ Buscando pedidos...")
        try:
            ventas = chateapro.ventas_del_dia(fecha)
            if not ventas:
                enviar(chat_id, "No hay ventas registradas hoy todavía.")
                return
            lineas = [f"🧾 *Pedidos de hoy ({len(ventas)})*\n"]
            for i, v in enumerate(ventas, 1):
                nombre = v.get(chateapro.CAMPO_NOMBRE) or v.get("_name") or "-"
                tel = v.get("_phone", "-")
                lineas.append(
                    f"{i}. {nombre}\n"
                    f"   📲 {tel}\n"
                    f"   📍 {v.get(chateapro.CAMPO_CIUDAD, '-')} — "
                    f"${v.get(chateapro.CAMPO_VALOR, '-')} — "
                    f"{v.get(chateapro.CAMPO_PRODUCTOS, '-')}"
                )
            enviar(chat_id, "\n".join(lineas))
        except Exception as e:
            enviar(chat_id, f"❌ Error al listar ventas: {e}")
        return

    if cmd == "/pedido":
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar(chat_id, "Uso: /pedido <teléfono>  (ej: /pedido 995276111)")
            return
        telefono = partes[1].strip()
        enviar(chat_id, f"⏳ Buscando pedido de {telefono}...")
        try:
            encontrado = None
            for v in chateapro.ventas_del_dia():
                if telefono in str(v.get("_phone", "")):
                    encontrado = v
                    break
            # Si no esta entre las ventas de hoy, buscar en recientes.
            if not encontrado:
                for s in chateapro.listar_suscriptores_recientes():
                    campos = chateapro.obtener_campos(s["user_ns"])
                    if telefono in str(campos.get("_phone", "")):
                        encontrado = campos
                        break
            if encontrado:
                enviar(chat_id, chateapro.resumen_pedido(encontrado))
            else:
                enviar(chat_id, f"No encontré un pedido con el teléfono {telefono}.")
        except Exception as e:
            enviar(chat_id, f"❌ Error al buscar el pedido: {e}")
        return

    if cmd == "/pendientes":
        fecha = _parse_arg_fecha(texto)
        enviar(chat_id, "⏳ Buscando pedidos pendientes...")
        try:
            pendientes = chateapro.pendientes_del_dia(fecha)
            if not pendientes:
                enviar(chat_id,
                       "✅ No hay pedidos pendientes "
                       "(ninguna venta con 'pendiente' en el Resumen).")
                return
            f_str = (fecha or chateapro.hoy_ecuador()).strftime("%d/%m/%Y")
            lineas = [f"⏳ *Pedidos pendientes del {f_str} ({len(pendientes)})*\n"]
            for i, v in enumerate(pendientes, 1):
                nombre = v.get(chateapro.CAMPO_NOMBRE) or v.get("_name") or "-"
                lineas.append(
                    f"{i}. {nombre}\n"
                    f"   📲 {v.get('_phone', '-')}\n"
                    f"   📍 {v.get(chateapro.CAMPO_CIUDAD, '-')} — "
                    f"${v.get(chateapro.CAMPO_VALOR, '-')}"
                )
            enviar(chat_id, "\n".join(lineas))
        except Exception as e:
            enviar(chat_id, f"❌ Error al listar pendientes: {e}")
        return

    enviar(chat_id, "No reconozco ese comando. Usa /ayuda para ver las opciones.")


# --- Notificacion automatica de ventas nuevas ---

# Cada cuantos segundos revisar si hay ventas nuevas.
INTERVALO_CHECK = int(os.environ.get("CHECK_INTERVAL_SECONDS", "120"))
# Activar/desactivar la notificacion automatica.
NOTIFICAR = os.environ.get("NOTIFICAR_VENTAS", "true").lower() == "true"

# Recuerda que ventas ya se notificaron (por user_ns) para no repetir.
_ya_notificadas = set()
# Controla cada cuanto se re-consulta a un suscriptor que NO ha comprado.
# Evita refetchear a todo el listado en cada ciclo (saturacion del API).
_ultima_revision = {}
_ultimo_check = 0
_primera_pasada = True

# Cada cuantos segundos se vuelve a revisar a un suscriptor aun sin compra.
REVISAR_NUEVOS_INTERVALO = int(
    os.environ.get("REVISAR_NUEVOS_INTERVAL_SECONDS", "1800")
)


def revisar_ventas_nuevas():
    """
    Detecta ventas de hoy no notificadas y las envia a los chats permitidos.

    Para no saturar la API:
      - solo hace 1 request para listar suscriptores recientes por ciclo,
      - consulta los campos de un suscriptor que aun no compra como maximo cada
        REVISAR_NUEVOS_INTERVALO segundos (30 min por defecto),
      - los campos se leen de la cache de chateapro.py.
    """
    global _primera_pasada
    try:
        suscriptores = chateapro.listar_suscriptores_recientes()
    except Exception as e:
        print("Error revisando ventas:", e)
        return

    hoy = chateapro.hoy_ecuador()
    ahora = time.time()

    for s in suscriptores:
        ns = s.get("user_ns")
        if not ns or ns in _ya_notificadas:
            continue
        # Aun no ha comprado: no re-consultarlo tan seguido.
        if not _primera_pasada and (ahora - _ultima_revision.get(ns, 0)) < REVISAR_NUEVOS_INTERVALO:
            continue
        _ultima_revision[ns] = ahora
        try:
            campos = chateapro.obtener_campos(ns)  # cacheado
        except Exception as e:
            print(f"Error obteniendo campos de {ns}: {e}")
            continue
        if not chateapro.es_venta_de_fecha(campos, hoy):
            continue
        _ya_notificadas.add(ns)
        # En la PRIMERA pasada solo marcamos (no notificar el historial).
        if _primera_pasada:
            continue
        texto = chateapro.resumen_venta_nueva(campos)
        for chat_id in ALLOWED:
            enviar(chat_id, texto)

    _primera_pasada = False


def main():
    if not TG_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN")
    print("Bot de ventas iniciado. Escuchando mensajes...")
    if NOTIFICAR:
        print(f"Notificacion automatica ACTIVA (cada {INTERVALO_CHECK}s).")

    global _ultimo_check
    offset = None
    while True:
        # 1) Revisar mensajes entrantes (comandos) con timeout corto.
        try:
            resp = requests.get(f"{TG_API}/getUpdates", params={
                "timeout": 10,
                "offset": offset,
            }, timeout=20).json()
        except Exception as e:
            print("Error getUpdates:", e)
            time.sleep(5)
            resp = {}

        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            texto = msg.get("text", "")
            if not autorizado(chat_id):
                enviar(chat_id, "🚫 No autorizado.")
                continue
            manejar_comando(chat_id, texto)

        # 2) Revisar ventas nuevas cada INTERVALO_CHECK segundos.
        if NOTIFICAR and (time.time() - _ultimo_check) >= INTERVALO_CHECK:
            _ultimo_check = time.time()
            revisar_ventas_nuevas()


if __name__ == "__main__":
    main()
