"""
Bot de Telegram de ventas para VitashopEc / Chatea Pro.

Comandos:
  /start    -> mensaje de bienvenida
  /resumen  -> estadisticas de ventas del dia (ventas, facturacion, top producto)
  /ventas   -> lista de pedidos del dia (nombre, ciudad, valor)
  /pendientes -> pedidos del dia cuyo Resumen menciona 'pendiente'
  /retiro   -> pedidos disponibles para retiro en oficina Servientrega
  /seguimiento [ayer|fecha] -> CSV de pedidos para retiro (dias sin retirar y guia)
  /reporte_diario -> reporte completo del dia (resumen + ventas + retiros)
  /pedido <telefono>  -> "Resumen del pedido - Datos de envio" de un cliente
  /campos <telefono>  -> muestra todos los campos guardados del cliente
  /ficha <telefono>   -> ficha de la venta (promo + agencia detectada) con botones
  /crear <telefono>   -> crea el pedido en Dropi (via Hermes) si la ficha esta completa

Cada venta nueva llega a Telegram como ficha con botones:
  [Crear pedido en Dropi] [agencias alternativas] [Reanalizar]

Usa long polling (getUpdates), asi que NO necesita webhook ni dominio publico.
Se controla el acceso con AUTHORIZED_CHAT_IDS (solo tu chat puede usarlo).

Variables de entorno:
  TELEGRAM_BOT_TOKEN     token del bot (@BotFather)
  TELEGRAM_ALLOWED_IDS   ids de chat autorizados, separados por coma
  CHATEAPRO_API_TOKEN    token de la API de Chatea Pro
  HORA_REPORTE_DIARIO    hora del reporte diario (formato HH:MM, hora Ecuador, def. 22:00)
  SCAN_DELAY_SECONDS     pausa (seg) entre peticiones en barridos masivos (def. 2.0)
  RETIROS_DB_PATH        ruta del archivo retiros.json (caché de fecha de retiro)
  RETIROS_HISTORICO_PATH ruta del historico de guias en retiro (no perder data)
  OPENAI_API_KEY         clave de OpenAI (promo + ubicacion desde el chat)
  OPENAI_MODEL           modelo (def. gpt-4o-mini)
  AGENCIAS_API_URL       servicio de agencias (def. https://servientrega.neolabsgroup.io)
  AGENCIAS_TOKEN         token opcional del servicio de agencias
  HERMES_URL             endpoint de Hermes que crea el pedido en Dropi (MCP)
  HERMES_TOKEN           token opcional de Hermes
  HERMES_TIMEOUT         segundos de espera a Hermes (def. 120)
  PEDIDOS_CREADOS_PATH   registro anti-duplicados de pedidos creados
"""

import os
import re
import csv
import json
import time
import html
import threading
import tempfile
import datetime as dt

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

import chateapro
import ficha
import dropi_mcp
import reporte_agencia

try:
    import dropi
    _DROPI_OK = bool(dropi.EMAIL and dropi.PASSWORD)
except Exception:
    dropi = None
    _DROPI_OK = False

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

ALLOWED = {
    x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",")
    if x.strip()
}


def enviar(chat_id, texto):
    r = requests.post(f"{TG_API}/sendMessage", data={
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
    }, timeout=30)
    # Si Telegram rechaza el texto (p.ej. Markdown mal formado con '[' sueltos),
    # reenviar SIN parse_mode para garantizar que el mensaje siempre llegue.
    if not r.ok:
        try:
            ok = r.json().get("ok", False)
        except Exception:
            ok = False
        if not ok:
            requests.post(f"{TG_API}/sendMessage", data={
                "chat_id": chat_id,
                "text": texto,
            }, timeout=30)


def enviar_documento(chat_id, ruta, caption):
    with open(ruta, "rb") as f:
        requests.post(f"{TG_API}/sendDocument", data={
            "chat_id": chat_id,
            "caption": caption,
        }, files={"document": f}, timeout=60)


def _enviar_csv(chat_id, filas, caption):
    """Arma un CSV temporal a partir de filas (dicts) y lo envia por Telegram."""
    fd, ruta = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
            w.writeheader()
            w.writerows(filas)
        enviar_documento(chat_id, ruta, caption)
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass


# --- Ficha de venta con botones (promo + agencia + crear en Dropi) ---

# Clientes cuyo pedido se esta creando ahora (evita doble clic / doble envio).
_creando = set()
_creando_lock = threading.Lock()


def enviar_plano(chat_id, texto, botones=None):
    """Envia texto SIN parse_mode (las fichas llevan guiones bajos). Devuelve message_id."""
    data = {"chat_id": chat_id, "text": texto}
    if botones:
        data["reply_markup"] = json.dumps({"inline_keyboard": botones})
    try:
        r = requests.post(f"{TG_API}/sendMessage", data=data, timeout=30)
        return (r.json().get("result") or {}).get("message_id")
    except Exception as e:
        print("Error enviando mensaje:", e)
        return None


def editar_plano(chat_id, message_id, texto, botones=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": texto,
            "reply_markup": json.dumps({"inline_keyboard": botones or []})}
    try:
        requests.post(f"{TG_API}/editMessageText", data=data, timeout=30)
    except Exception as e:
        print("Error editando mensaje:", e)


def responder_callback(callback_id, texto="", alerta=False):
    try:
        requests.post(f"{TG_API}/answerCallbackQuery", data={
            "callback_query_id": callback_id, "text": texto[:190],
            "show_alert": "true" if alerta else "false",
        }, timeout=15)
    except Exception as e:
        print("Error answerCallbackQuery:", e)


def enviar_ficha(chat_id, a):
    return enviar_plano(chat_id, ficha.texto_ficha(a), ficha.teclado(a))


def _nombre_de(a):
    c = a["campos"]
    return c.get(chateapro.CAMPO_NOMBRE) or c.get("_name") or "cliente"


def _campos_por_telefono(telefono):
    """Busca los campos de un cliente por telefono (ventas de hoy y recientes)."""
    digitos = re.sub(r"\D", "", telefono)[-9:]
    if len(digitos) < 7:
        return None
    for v in chateapro.ventas_del_dia():
        if digitos in re.sub(r"\D", "", str(v.get("_phone", ""))):
            return v
    for s in chateapro.listar_suscriptores_recientes():
        campos = chateapro.obtener_campos(s["user_ns"])
        if digitos in re.sub(r"\D", "", str(campos.get("_phone", ""))):
            return campos
    return None


def _ejecutar_creacion(chat_id, message_id, a):
    """Corre en un hilo: delega a Hermes y reporta el resultado en Telegram."""
    ns = a["ns"]
    try:
        res = ficha.crear_pedido(a)
    except Exception as e:  # nunca dejar el ns bloqueado
        res = {"ok": False, "order_id": None, "guia": None, "mensaje": str(e)}
    finally:
        with _creando_lock:
            _creando.discard(ns)

    if res["ok"]:
        txt = f"✅ Pedido creado en Dropi: #{res['order_id']}\n👤 {_nombre_de(a)}"
        if res.get("guia"):
            txt += f"\n🔖 Guía: {res['guia']}"
    else:
        txt = (f"❌ No se creó el pedido de {_nombre_de(a)}.\n{res['mensaje']}\n"
               "Antes de reintentar revisa en Dropi que no se haya creado.")
    enviar_plano(chat_id, txt)
    if message_id:
        editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))


def _iniciar_creacion(chat_id, message_id, a):
    """Valida y lanza la creacion en un hilo. Devuelve (ok, mensaje)."""
    motivos = ficha.bloqueos(a)
    if motivos:
        return False, "; ".join(motivos)
    with _creando_lock:
        if a["ns"] in _creando:
            return False, "Ya se está creando este pedido."
        _creando.add(a["ns"])
    threading.Thread(target=_ejecutar_creacion, args=(chat_id, message_id, a),
                     daemon=True).start()
    return True, "Creando pedido en Dropi..."


def _ficha_de(ns, forzar=False):
    """Analisis cacheado, o lo reconstruye si el bot se reinicio."""
    if forzar:
        chateapro._CACHE.pop(ns, None)  # releer campos frescos del CRM
        ficha._CACHE.pop(ns, None)
    a = ficha._CACHE.get(ns)
    if a is None:
        a = ficha.analizar(chateapro.obtener_campos(ns), forzar=True)
    return a


def manejar_callback(cb):
    """Atiende los botones de la ficha: crear:<ns>, ag:<ns>:<i>, re:<ns>."""
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not autorizado(chat_id):
        responder_callback(cb_id, "No autorizado", True)
        return

    partes = (cb.get("data") or "").split(":")
    accion = partes[0]
    ns = partes[1] if len(partes) > 1 else ""
    if not ns:
        responder_callback(cb_id, "Botón inválido", True)
        return

    try:
        if accion == "re":
            a = _ficha_de(ns, forzar=True)
            responder_callback(cb_id, "Ficha actualizada")
            editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
        elif accion == "ag" and len(partes) == 3:
            a = _ficha_de(ns)
            if ficha.elegir_agencia(a, int(partes[2])):
                responder_callback(cb_id, "Agencia elegida")
                editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
            else:
                responder_callback(cb_id, "Esa opción ya no existe, pulsa Reanalizar", True)
        elif accion == "crear":
            a = _ficha_de(ns)
            ok, texto = _iniciar_creacion(chat_id, message_id, a)
            responder_callback(cb_id, texto, not ok)
            if ok and message_id:
                # Quita los botones mientras se crea (evita doble clic).
                editar_plano(chat_id, message_id,
                             ficha.texto_ficha(a) + "\n\n⏳ Creando pedido en Dropi...")
        else:
            responder_callback(cb_id, "Acción desconocida", True)
    except Exception as e:
        print("Error en callback:", e)
        responder_callback(cb_id, f"Error: {e}", True)


def _notificar_venta(campos):
    """Manda la ficha completa a Telegram; si algo falla, el aviso simple de siempre."""
    try:
        a = ficha.analizar(campos)
        for chat_id in ALLOWED:
            enviar_ficha(chat_id, a)
    except Exception as e:
        print("Error armando la ficha, se envia el aviso simple:", e)
        texto = chateapro.resumen_venta_nueva(campos)
        for chat_id in ALLOWED:
            enviar(chat_id, texto)


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
               "• /retiro — pedidos disponibles para retiro en Servientrega\n"
               "• /en_agencia — reporte HTML de pedidos en agencia Servientrega "
               "(Dropi MCP) con botón de WhatsApp\n"
               "• /dropi_conectar — iniciar sesión en Dropi (MCP)\n"
               "• /seguimiento ayer, fecha o 'todos' — CSV de retiros (días sin retirar)\n"
               "• /reporte_diario — reporte completo del día (resumen + ventas + retiros)\n"
               "• /pedido <teléfono> — datos de envío de un cliente\n"
               "• /campos <teléfono> — ver todos los campos de un cliente\n"
               "• /guia <número> — movimientos Servientrega (Dropi directo)\n"
               "• /dropi_pedidos [estado] — pedidos directo de Dropi\n"
               "• /dropi_saldo — saldo de cartera Dropi\n"
               "• /ficha <teléfono> — ficha de la venta (promo + agencia) con botones\n"
               "• /crear <teléfono> — crea el pedido en Dropi (vía Hermes) si está completo")
        return

    if cmd == "/en_agencia":
        enviar_plano(chat_id, "⏳ Consultando en Dropi los pedidos en agencia Servientrega...")
        threading.Thread(target=_reporte_en_agencia, args=(chat_id,), daemon=True).start()
        return

    if cmd == "/dropi_conectar":
        _pedir_conexion_dropi(chat_id, "🔐 Conectar Dropi (MCP).")
        return

    if cmd == "/dropi_desconectar":
        dropi_mcp.desconectar()
        enviar_plano(chat_id, "Dropi (MCP) desconectado. Usa /dropi_conectar para volver a entrar.")
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

    if cmd in ("/ficha", "/crear"):
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar(chat_id, f"Uso: {cmd} <teléfono>  (ej: {cmd} 995276111)")
            return
        telefono = partes[1].strip()
        enviar(chat_id, f"⏳ Analizando la venta de {telefono}...")
        try:
            campos = _campos_por_telefono(telefono)
            if not campos:
                enviar(chat_id, f"No encontré una venta con el teléfono {telefono}.")
                return
            a = ficha.analizar(campos, forzar=True)
            if cmd == "/ficha":
                enviar_ficha(chat_id, a)
                return
            # /crear: si algo bloquea, muestra la ficha para que veas por que.
            ok, mensaje = _iniciar_creacion(chat_id, None, a)
            if ok:
                enviar_plano(chat_id, f"⏳ Creando pedido de {_nombre_de(a)} en Dropi...")
            else:
                enviar_plano(chat_id, f"⚠️ No se creó: {mensaje}")
                enviar_ficha(chat_id, a)
        except Exception as e:
            enviar(chat_id, f"❌ Error: {e}")
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

    if cmd == "/retiro":
        enviar(chat_id, "⏳ Buscando pedidos disponibles para retiro...")
        try:
            retiros = chateapro.listar_para_retiro()
            if not retiros:
                enviar(chat_id,
                       "No hay pedidos disponibles para retiro en Servientrega "
                       "en este momento.")
                return
            lineas = [f"📦 *Disponibles para retiro ({len(retiros)})*\n"]
            for i, v in enumerate(retiros, 1):
                nombre = v.get(chateapro.CAMPO_NOMBRE) or v.get("_name") or "-"
                lineas.append(
                    f"{i}. {nombre}\n"
                    f"   📲 {v.get('_phone', '-')}\n"
                    f"   📍 {v.get(chateapro.CAMPO_CIUDAD, '-')} — "
                    f"${v.get(chateapro.CAMPO_VALOR, '-')}"
                )
            enviar(chat_id, "\n".join(lineas))
        except Exception as e:
            enviar(chat_id, f"❌ Error al listar retiros: {e}")
        return

    if cmd == "/campos":
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar(chat_id, "Uso: /campos <teléfono>  (ej: /campos 995276111)")
            return
        telefono = partes[1].strip()
        try:
            encontrado = None
            for s in chateapro.listar_suscriptores_recientes():
                campos = chateapro.obtener_campos(s["user_ns"])
                if telefono in str(campos.get("_phone", "")):
                    encontrado = campos
                    break
            if not encontrado:
                enviar(chat_id, f"No encontré un suscriptor con el teléfono {telefono}.")
                return
            lineas = [f"📎 *Campos de {telefono}:*\n"]
            for k, v in encontrado.items():
                if v:
                    lineas.append(f"• {k}: {v}")
            enviar(chat_id, "\n".join(lineas) if len(lineas) > 1
                   else "El suscriptor no tiene campos con valor.")
        except Exception as e:
            enviar(chat_id, f"❌ Error: {e}")
        return

    if cmd == "/seguimiento":
        partes = texto.split(maxsplit=1)
        arg = partes[1].strip().lower() if len(partes) > 1 else ""
        if arg in ("todos", "todas", "todo"):
            # Barrido completo: todas las guias en retiro, sin importar fecha.
            fecha = chateapro.hoy_ecuador() - dt.timedelta(days=3650)
            f_s = "todas las fechas"
            scope = "todos los suscriptores"
        else:
            fecha = _parse_arg_fecha(texto)
            f_s = (fecha or chateapro.hoy_ecuador()).strftime("%d/%m/%Y")
            scope = ("últimas 24h" if (fecha is None or fecha == chateapro.hoy_ecuador())
                     else "todos los suscriptores")
        enviar(chat_id,
               f"⏳ Generando seguimiento de retiros ({f_s}, {scope})... "
               "puede tardar unos segundos")
        try:
            filas = chateapro.seguimiento_retiro(fecha)
            if not filas:
                enviar(chat_id,
                       "No hay pedidos con estado PARA RETIRO EN AGENCIA "
                       "SERVIENTREGA.")
                return

            _enviar_csv(chat_id, filas,
                        "📦 *Seguimiento retiro Servientrega*\n"
                        f"Fecha de consulta: {f_s} ({scope})\n"
                        f"Pedidos para retiro: {len(filas)}\n"
                        "Columnas: guía, nombre, teléfono, ciudad, provincia, "
                        "dirección, productos, valor, bodega, tasa, estado, "
                        "fecha en retiro, días sin retirar, datos_dropi "
                        "(JSON completo de Dropi)\n"
                        "Archivo: seguimiento_retiro.csv")

            con_fecha = [x for x in filas if x["dias_sin_retirar"] != ""]
            prom_dias = 0
            if con_fecha:
                prom_dias = round(
                    sum(int(x["dias_sin_retirar"]) for x in con_fecha) / len(con_fecha), 1)
            max_dias = max((int(x["dias_sin_retirar"]) for x in con_fecha), default=0)

            top = min(len(filas), 10)
            lineas = [f"⏳ *Los {top} con más días sin retirar:*\n"
                      f"Promedio: {prom_dias} días | Máximo: {max_dias}\n"]
            for v in filas[:top]:
                lineas.append(
                    f"• {v['nombre']} — 📲 {v['telefono']} — "
                    f"🕐 {v['dias_sin_retirar'] or '?'} días "
                    f"({v['fecha_en_retiro']})"
                )
            enviar(chat_id, "\n".join(lineas))
        except Exception as e:
            enviar(chat_id, f"❌ Error al generar el seguimiento: {e}")
        return

    if cmd == "/reporte_diario":
        enviar(chat_id, "⏳ Generando reporte diario...")
        try:
            texto, filas = generar_reporte_diario()
            enviar(chat_id, texto)
            if filas:
                _enviar_csv(
                    chat_id, filas,
                    f"📦 *Seguimiento retiro Servientrega*\n"
                    f"Fecha: {chateapro.hoy_ecuador().strftime('%d/%m/%Y')} "
                    f"(últimas 24h)\n"
                    f"Pedidos para retiro: {len(filas)}\n"
                    "Columnas: guía, nombre, teléfono, ciudad, provincia, "
                    "dirección, productos, valor, bodega, tasa, estado, "
                    "fecha en retiro, días sin retirar, datos_dropi "
                    "(JSON completo de Dropi)\n"
                    "Archivo: seguimiento_retiro.csv",
                )
        except Exception as e:
            enviar(chat_id, f"❌ Error al generar el reporte diario: {e}")
        return

    if cmd == "/guia":
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar(chat_id, "Uso: /guia <número>  (ej: /guia 189832507)")
            return
        if not _DROPI_OK:
            enviar(chat_id, "Dropi directo no configurado (faltan DROPI_EMAIL / DROPI_PASSWORD).")
            return
        numero = partes[1].strip()
        enviar(chat_id, f"⏳ Consultando guía {numero} en Dropi...")
        try:
            movs = dropi.movimientos_guia(numero)
            if not movs:
                enviar(chat_id, f"No existe la guía {numero} en Dropi.")
                return
            lineas = [f"📦 *Guía {numero}* ({len(movs)} movimientos)\n"]
            for m in movs[-10:]:
                lineas.append(
                    f"• {m.get('NomMov', '-')} — {m.get('FecMov', '')}\n"
                    f"  {m.get('OriMov', '')} → {m.get('DesMov', '')}"
                )
            enviar(chat_id, "\n".join(lineas))
        except Exception as e:
            enviar(chat_id, f"❌ Error al consultar la guía: {e}")
        return

    if cmd == "/dropi_pedidos":
        if not _DROPI_OK:
            enviar(chat_id, "Dropi directo no configurado (faltan DROPI_EMAIL / DROPI_PASSWORD).")
            return
        partes = texto.split(maxsplit=2)
        estado = partes[1].upper() if len(partes) > 1 else None
        try:
            n = int(partes[2]) if len(partes) > 2 else 15
        except ValueError:
            n = 15
        filtros = {"status": estado} if estado else None
        enviar(chat_id, "⏳ Consultando pedidos en Dropi...")
        try:
            items, total = dropi.objetos_ordenes(
                dropi.listar_mis_ordenes(page=1, per_page=min(n, 50),
                                         filtros=filtros))
            if not items:
                enviar(chat_id, "Sin pedidos para ese filtro en Dropi.")
                return
            titulo = f"📦 *Pedidos Dropi{f' ({estado})' if estado else ''}*\n"
            lineas = [titulo]
            for o in items:
                nombre = f"{o.get('name', '')} {o.get('surname', '')}".strip() or "-"
                lineas.append(
                    f"• #{o.get('id')} — {nombre}\n"
                    f"  📲 {o.get('phone', '-')} — 📍 {o.get('city', '-')}\n"
                    f"  {o.get('status', '-')} — ${o.get('total_order', '-')} — "
                    f"guía {o.get('shipping_guide') or '-'}"
                )
            enviar(chat_id, "\n".join(lineas))
            filas = [{
                "id": o.get("id"), "nombre": f"{o.get('name', '')} {o.get('surname', '')}".strip(),
                "telefono": o.get("phone"), "ciudad": o.get("city"),
                "estado": o.get("status"), "valor": o.get("total_order"),
                "guia": o.get("shipping_guide") or "",
            } for o in items]
            _enviar_csv(chat_id, filas, f"📦 Pedidos Dropi{f' ({estado})' if estado else ''} (total: {total})")
        except Exception as e:
            enviar(chat_id, f"❌ Error al listar pedidos Dropi: {e}")
        return

    if cmd == "/dropi_saldo":
        if not _DROPI_OK:
            enviar(chat_id, "Dropi directo no configurado (faltan DROPI_EMAIL / DROPI_PASSWORD).")
            return
        try:
            s = dropi.saldo_cartera()
            enviar(chat_id,
                   f"💰 *Cartera Dropi:* {s.get('amount', '-')} "
                   f"{s.get('currency_code', '')}")
        except Exception as e:
            enviar(chat_id, f"❌ Error al consultar saldo: {e}")
        return

    enviar(chat_id, "No reconozco ese comando. Usa /ayuda para ver las opciones.")


# --- Reporte "pedidos en agencia" via MCP de Dropi (con login OAuth) ---

# Chats que pidieron el reporte y esperan a que se conecte Dropi.
_esperando_dropi = set()
_reporte_lock = threading.Lock()


def _pedir_conexion_dropi(chat_id, motivo):
    """Envia el boton para iniciar sesion en Dropi (OAuth del MCP)."""
    try:
        url = dropi_mcp.url_autorizacion()
    except Exception as e:
        enviar_plano(chat_id, f"❌ No se pudo generar el link de Dropi: {e}")
        return
    local = urlparse(dropi_mcp.REDIRECT_URI).hostname in ("localhost", "127.0.0.1")
    texto = (f"{motivo}\n\n1) Toca «Iniciar sesión en Dropi» y entra con tu cuenta.\n"
             "2) Autoriza el acceso.\n")
    if local:
        texto += ("3) Si al final el navegador muestra «no se puede acceder al sitio» "
                  "(localhost), copia la URL COMPLETA de la barra de direcciones y "
                  "pégala aquí en el chat.\n")
    texto += "\nEl link vence en 15 minutos."
    enviar_plano(chat_id, texto, [[{"text": "🔐 Iniciar sesión en Dropi", "url": url}]])


def _reporte_en_agencia(chat_id):
    if not _reporte_lock.acquire(blocking=False):
        enviar_plano(chat_id, "⏳ Ya se está generando un reporte, espera un momento.")
        return
    ruta = None
    try:
        ruta, pedidos = reporte_agencia.generar(
            progreso=lambda i, n: print(f"/en_agencia: {i}/{n} pedidos"))
        enviar_plano(chat_id, reporte_agencia.resumen_telegram(pedidos))
        enviar_documento(chat_id, ruta,
                         f"📦 Pedidos en agencia Servientrega: {len(pedidos)}\n"
                         "Abre el HTML y toca «Enviar WhatsApp» en cada cliente.")
    except dropi_mcp.AuthRequired:
        _esperando_dropi.add(chat_id)
        _pedir_conexion_dropi(
            chat_id, "🔐 Dropi pide iniciar sesión para generar el reporte.\n"
                     "Al conectar, el reporte se genera solo.")
    except Exception as e:
        enviar_plano(chat_id, f"❌ Error generando el reporte en agencia: {e}")
    finally:
        _reporte_lock.release()
        if ruta:
            try:
                os.remove(ruta)
            except OSError:
                pass


def _dropi_conectado_ok(chat_ids):
    """Tras el login: avisa y genera los reportes que estaban esperando."""
    pendientes = set(_esperando_dropi)
    _esperando_dropi.clear()
    for cid in set(chat_ids) | pendientes:
        enviar_plano(cid, "✅ Dropi conectado (MCP).")
    for cid in pendientes:
        enviar_plano(cid, "⏳ Generando el reporte de pedidos en agencia...")
        threading.Thread(target=_reporte_en_agencia, args=(cid,), daemon=True).start()


def _procesar_url_dropi(chat_id, texto):
    try:
        dropi_mcp.completar_desde_url(texto)
    except Exception as e:
        enviar_plano(chat_id, f"❌ No se pudo conectar Dropi: {e}\nUsa /dropi_conectar para intentar de nuevo.")
        return
    _dropi_conectado_ok([chat_id])


class _OAuthCallback(BaseHTTPRequestHandler):
    """Recibe el redirect de Dropi cuando el navegador llega al bot."""

    def do_GET(self):
        ruta = urlparse(self.path).path
        if ruta != urlparse(dropi_mcp.REDIRECT_URI).path:
            self.send_response(404)
            self.end_headers()
            return
        try:
            dropi_mcp.completar_desde_url("http://x" + self.path)
            ok, msg = True, "✅ Dropi conectado. Ya puedes volver a Telegram."
        except Exception as e:
            ok, msg = False, f"❌ No se pudo conectar: {html.escape(str(e))}"
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<body style='font:18px system-ui;padding:24px'>{msg}</body>".encode())
        if ok:
            _dropi_conectado_ok(ALLOWED)

    def log_message(self, *args):
        pass


def _iniciar_servidor_oauth():
    """Servidor HTTP para el redirect OAuth (opcional; si no, se pega la URL)."""
    if os.environ.get("OAUTH_CALLBACK_SERVER", "true").lower() != "true":
        return
    puerto = int(os.environ.get("OAUTH_CALLBACK_PORT")
                 or urlparse(dropi_mcp.REDIRECT_URI).port or 19876)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", puerto), _OAuthCallback)
    except OSError as e:
        print(f"Servidor OAuth no iniciado (puerto {puerto}): {e}. "
              "Se usara el modo 'pegar URL' en Telegram.")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Callback OAuth de Dropi escuchando en :{puerto}")


MENU_COMANDOS = [
    ("en_agencia", "Pedidos en agencia Servientrega (HTML + WhatsApp)"),
    ("resumen", "Ventas de hoy"),
    ("ventas", "Lista de pedidos de hoy"),
    ("pendientes", "Pedidos pendientes"),
    ("retiro", "Disponibles para retiro (Chatea Pro)"),
    ("seguimiento", "CSV de retiros (ayer, fecha o todos)"),
    ("reporte_diario", "Reporte completo del día"),
    ("pedido", "Datos de envío: /pedido <teléfono>"),
    ("ficha", "Ficha de la venta: /ficha <teléfono>"),
    ("guia", "Movimientos de una guía"),
    ("dropi_pedidos", "Pedidos directo de Dropi"),
    ("dropi_saldo", "Saldo de cartera Dropi"),
    ("dropi_conectar", "Iniciar sesión en Dropi (MCP)"),
    ("ayuda", "Ver todos los comandos"),
]


def _registrar_menu():
    """Publica los comandos en el boton 'Menú' de Telegram."""
    try:
        requests.post(f"{TG_API}/setMyCommands", json={
            "commands": [{"command": c, "description": d} for c, d in MENU_COMANDOS],
        }, timeout=15)
    except Exception as e:
        print("No se pudo registrar el menu de comandos:", e)


# --- Notificacion automatica de ventas nuevas ---

# Cada cuantos segundos revisar si hay ventas nuevas.
INTERVALO_CHECK = int(os.environ.get("CHECK_INTERVAL_SECONDS", "120"))
# Activar/desactivar la notificacion automatica.
NOTIFICAR = os.environ.get("NOTIFICAR_VENTAS", "true").lower() == "true"

# --- Reporte diario automatico (final del dia, hora de Ecuador) ---
REPORTE_DIARIO = os.environ.get("REPORTE_DIARIO", "true").lower() == "true"
HORA_REPORTE = os.environ.get("HORA_REPORTE_DIARIO", "22:00")
_ultimo_reporte_dia = None


def generar_reporte_diario():
    """
    Arma el reporte diario completo (resumen + ventas + seguimiento de retiro).
    Devuelve (texto, filas_seguimiento): el texto listo para /sendMessage y las
    filas del seguimiento (para adjuntar como CSV si hay datos).
    """
    hoy = chateapro.hoy_ecuador()
    f_s = hoy.strftime("%d/%m/%Y")
    partes = [f"📋 *REPORTE DIARIO — {f_s}*\n"]

    try:
        partes.append("*📊 RESUMEN*\n" + chateapro.resumen_del_dia(hoy))
    except Exception as e:
        partes.append(f"*📊 RESUMEN*\n❌ Error: {e}")

    try:
        ventas = chateapro.ventas_del_dia(hoy)
        if ventas:
            lineas = [f"*🧾 VENTAS DEL DÍA ({len(ventas)})*\n"]
            for i, v in enumerate(ventas[:25], 1):
                nombre = v.get(chateapro.CAMPO_NOMBRE) or v.get("_name") or "-"
                lineas.append(
                    f"{i}. {nombre} — 📲 {v.get('_phone', '-')} — "
                    f"📍 {v.get(chateapro.CAMPO_CIUDAD, '-')} — "
                    f"${v.get(chateapro.CAMPO_VALOR, '-')}"
                )
            if len(ventas) > 25:
                lineas.append(f"... y {len(ventas) - 25} ventas más.")
            partes.append("\n".join(lineas))
        else:
            partes.append("*🧾 VENTAS DEL DÍA*\nSin ventas registradas.")
    except Exception as e:
        partes.append(f"*🧾 VENTAS DEL DÍA*\n❌ Error: {e}")

    filas = []
    seg = "*📦 SEGUIMIENTO DE RETIRO (últimas 24h)*\n"
    try:
        filas = chateapro.seguimiento_retiro(hoy)
        if not filas:
            seg += "Sin pedidos disponibles para retiro."
        else:
            con_fecha = [x for x in filas if x["dias_sin_retirar"] != ""]
            prom_dias = 0
            max_dias = 0
            if con_fecha:
                dias = [int(x["dias_sin_retirar"]) for x in con_fecha]
                prom_dias = round(sum(dias) / len(dias), 1)
                max_dias = max(dias)
            seg += (f"Pedidos para retiro: {len(filas)}\n"
                    f"Días promedio: {prom_dias} | Máximo sin retirar: {max_dias}\n")
            lineas = [f"Pendientes más antiguos (top 5):"]
            for v in filas[:5]:
                lineas.append(
                    f"  • {v['nombre']} — {v['dias_sin_retirar'] or '?'} días "
                    f"({v['fecha_en_retiro']})"
                )
            seg += "\n".join(lineas)
    except Exception as e:
        seg += f"❌ Error: {e}"
    partes.append(seg)

    return "\n\n".join(partes), filas


def _enviar_reporte_diario():
    texto, filas = generar_reporte_diario()
    for chat_id in ALLOWED:
        try:
            enviar(chat_id, texto)
            if filas:
                _enviar_csv(chat_id, filas,
                            f"📦 *Seguimiento retiro Servientrega*\n"
                            f"Fecha: {chateapro.hoy_ecuador().strftime('%d/%m/%Y')} "
                            f"(últimas 24h)\n"
                            f"Pedidos para retiro: {len(filas)}\n"
                            "Columnas: guía, nombre, teléfono, ciudad, provincia, "
                            "dirección, productos, valor, bodega, tasa, estado, "
                            "fecha en retiro, días sin retirar, datos_dropi "
                            "(JSON completo de Dropi)\n"
                            "Archivo: seguimiento_retiro.csv")
        except Exception as e:
            print(f"Error enviando reporte diario a {chat_id}: {e}")


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
        # Registra la fecha de deteccion de guias en estado de retiro, para
        # calcular dias sin retirar de forma confiable en /seguimiento.
        chateapro.registrar_retiros(campos)
        if not chateapro.es_venta_de_fecha(campos, hoy):
            continue
        _ya_notificadas.add(ns)
        # En la PRIMERA pasada solo marcamos (no notificar el historial).
        if _primera_pasada:
            continue
        _notificar_venta(campos)

    _primera_pasada = False


def main():
    if not TG_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN")
    print("Bot de ventas iniciado. Escuchando mensajes...")
    _registrar_menu()
    _iniciar_servidor_oauth()
    if NOTIFICAR:
        print(f"Notificacion automatica ACTIVA (cada {INTERVALO_CHECK}s).")
    if REPORTE_DIARIO:
        print(f"Reporte diario ACTIVO (envio a las {HORA_REPORTE} hora de Ecuador).")

    global _ultimo_check, _ultimo_reporte_dia
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
            if u.get("callback_query"):
                manejar_callback(u["callback_query"])
                continue
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            texto = msg.get("text", "")
            if not autorizado(chat_id):
                enviar(chat_id, "🚫 No autorizado.")
                continue
            if dropi_mcp.es_url_callback(texto):
                _procesar_url_dropi(chat_id, texto)
                continue
            manejar_comando(chat_id, texto)

        # 2) Revisar ventas nuevas cada INTERVALO_CHECK segundos.
        if NOTIFICAR and (time.time() - _ultimo_check) >= INTERVALO_CHECK:
            _ultimo_check = time.time()
            revisar_ventas_nuevas()

        # 3) Reporte diario automatico al final del dia (hora de Ecuador).
        if REPORTE_DIARIO and ALLOWED:
            ahora = dt.datetime.now(chateapro.TZ_ECUADOR)
            hoy_s = ahora.strftime("%Y-%m-%d")
            if ahora.strftime("%H:%M") >= HORA_REPORTE and _ultimo_reporte_dia != hoy_s:
                _ultimo_reporte_dia = hoy_s
                print("Enviando reporte diario...")
                try:
                    _enviar_reporte_diario()
                except Exception as e:
                    print("Error generando reporte diario:", e)


if __name__ == "__main__":
    main()
