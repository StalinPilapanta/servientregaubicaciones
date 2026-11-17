"""
Bot de Telegram de ventas para VitashopEc / Chatea Pro.

Comandos:
  /start    -> mensaje de bienvenida
  /resumen  -> estadisticas de ventas del dia (ventas, facturacion, top producto)
  /ventas   -> lista de pedidos del dia (nombre, ciudad, valor)
  /pendientes -> pedidos del dia cuyo Resumen menciona 'pendiente'
  /retiro   -> pedidos disponibles para retiro en oficina Servientrega
  /seguimiento [ayer|fecha] -> HTML de pedidos para retiro (dias sin retirar y guia)
  /reporte_diario -> reporte completo del dia (resumen + ventas + retiros)
  /pedido <telefono>  -> "Resumen del pedido - Datos de envio" de un cliente
  /campos <telefono>  -> muestra todos los campos guardados del cliente
  /crear_pedido <telefono> -> ficha de la venta (promo + agencia detectada) con
                              botones para crear el pedido en Dropi
  /crear <telefono>   -> resumen + confirmar para crear el pedido en Dropi (MCP)

Cada venta nueva llega a Telegram como ficha con botones:
  [Crear pedido en Dropi] [agencias alternativas] [Reanalizar]
"Crear" muestra el resumen (ciudad/departamento Dropi + envio Servientrega) y
solo crea el pedido al pulsar "Si, crear en Dropi".

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
  ANTHROPIC_API_KEY      asistente conversacional (texto libre y audios), ver asistente.py
  GROQ_API_KEY           transcripcion de notas de voz (o OPENAI_API_KEY), ver transcribir.py
  HORA_ALERTAS           novedades + recordatorio de retiro diarios (def. 09:00), ver alertas.py
  AGENCIAS_API_URL       servicio de agencias (def. https://servientrega.neolabsgroup.io)
  AGENCIAS_TOKEN         token opcional del servicio de agencias
  GEOCODER               "nominatim" (def.) u "off": agencia mas cercana a la referencia
  DROPI_DIAS_DUPLICADO   dias que se revisan en Dropi para no duplicar (def. 3)
  (los pedidos se crean con el MCP de Dropi: ver dropi_mcp.py / dropi_pedido.py)
  PEDIDOS_CREADOS_PATH   registro anti-duplicados de pedidos creados
"""

import os
import re
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
import dropi_pedido
import reporte_agencia
import reporte_html
import alertas
import meta
import asistente
import transcribir
import datos

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


def _enviar_html(chat_id, contenido, prefijo, caption):
    """Guarda el HTML en un temporal, lo envia por Telegram y lo borra."""
    ruta = reporte_html.guardar(contenido, prefijo)
    try:
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


def _ocupado(ns):
    with _creando_lock:
        return ns in _creando


def _correr_en_hilo(chat_id, message_id, a, fn):
    """Corre fn(chat_id, message_id, a) en un hilo con candado por suscriptor."""
    with _creando_lock:
        if a["ns"] in _creando:
            return False
        _creando.add(a["ns"])

    def envoltura():
        try:
            fn(chat_id, message_id, a)
        except dropi_mcp.AuthRequired:
            _pedir_conexion_dropi(chat_id, "🔐 Dropi pide iniciar sesión para crear el pedido.\n"
                                           "Al conectar, vuelve a pulsar «Crear pedido en Dropi».")
            if message_id:
                editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
        except Exception as e:
            print("Error creando pedido:", e)
            enviar_plano(chat_id, f"❌ Error con Dropi: {e}\n"
                                  "Antes de reintentar revisa en Dropi que no se haya creado.")
            if message_id:
                editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
        finally:
            with _creando_lock:
                _creando.discard(a["ns"])

    threading.Thread(target=envoltura, daemon=True).start()
    return True


def _preparar(chat_id, message_id, a):
    """Paso 1: ciudad/departamento Dropi + cotizacion -> pide confirmacion."""
    ok, msg = ficha.preparar(a)
    if not ok:
        texto = ficha.texto_ficha(a) + f"\n\n❌ No se puede crear: {msg}"
        if message_id:
            editar_plano(chat_id, message_id, texto, ficha.teclado(a))
        else:
            enviar_plano(chat_id, texto, ficha.teclado(a))
        return
    if message_id:
        editar_plano(chat_id, message_id, ficha.texto_confirmacion(a),
                     ficha.teclado_confirmacion(a))
    else:
        enviar_plano(chat_id, ficha.texto_confirmacion(a), ficha.teclado_confirmacion(a))


def _crear(chat_id, message_id, a):
    """Paso 2: create_order con el MCP de Dropi y reporte del resultado."""
    res = ficha.crear_pedido(a)
    a["preparado"] = None
    if res["ok"]:
        txt = f"✅ Pedido creado en Dropi: #{res['order_id']}\n👤 {_nombre_de(a)}"
        if res.get("mensaje"):
            txt += f"\n{res['mensaje']}"
        noti = res.get("notificacion")
        if noti and noti[0]:
            txt += "\n📲 Confirmación enviada al cliente por WhatsApp."
        elif noti:
            txt += (f"\n⚠️ No se pudo enviar la confirmación al cliente ({noti[1]}). "
                    "Envíasela a mano desde Chatea Pro.")
    else:
        txt = (f"❌ No se creó el pedido de {_nombre_de(a)}.\n{res['mensaje']}\n"
               "Antes de reintentar revisa en Dropi que no se haya creado.")
    enviar_plano(chat_id, txt)
    if message_id:
        editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))


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
    """Botones: crear, ok, x, ag:<i>, re, ps (pedir agencia), va (ver agencias)."""
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
        if accion in ("wa", "wx"):
            _callback_borrador(cb_id, chat_id, message_id, accion, ns)
            return
        if accion == "mc":
            try:
                meta.elegir_cuenta(ns)
                responder_callback(cb_id, "Cuenta elegida")
                editar_plano(chat_id, message_id, f"✅ Cuenta publicitaria activa: {ns}")
            except meta.MetaError as e:
                responder_callback(cb_id, str(e), True)
            return
        if accion == "nv":
            responder_callback(cb_id, "Enviando plantilla...")
            ok, det = alertas.enviar_novedad(ns)
            enviar_plano(chat_id, ("✅ Plantilla de novedad enviada" if ok
                                   else f"❌ No se envió ({det})") + f" · pedido #{ns}")
            return
        if accion == "rr":
            with _retiros_lock:
                lista = list(_retiros_pendientes)
                _retiros_pendientes.clear()
            if not lista:
                responder_callback(cb_id, "No hay recordatorios pendientes.", True)
                return
            responder_callback(cb_id, f"Enviando {len(lista)}...")
            editar_plano(chat_id, message_id, f"⏳ Enviando {len(lista)} recordatorios...")
            env, fal = alertas.enviar_recordatorios(lista)
            enviar_plano(chat_id, _texto_recordatorios(env, fal, []))
            return
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
            motivos = ficha.bloqueos(a)
            if motivos:
                responder_callback(cb_id, "; ".join(motivos), True)
                return
            if _ocupado(ns):
                responder_callback(cb_id, "Ya se está procesando este pedido.", True)
                return
            responder_callback(cb_id, "Preparando el pedido...")
            # Primero el aviso y DESPUES el hilo: si no, el aviso puede tapar el resultado.
            if message_id:
                editar_plano(chat_id, message_id, ficha.texto_ficha(a)
                             + "\n\n⏳ Ubicando ciudad en Dropi y cotizando Servientrega...")
            _correr_en_hilo(chat_id, message_id, a, _preparar)
        elif accion == "ok":
            a = _ficha_de(ns)
            if not a.get("preparado"):
                responder_callback(cb_id, "La confirmación venció, pulsa Crear de nuevo.", True)
                editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
                return
            if _ocupado(ns):
                responder_callback(cb_id, "Ya se está creando este pedido.", True)
                return
            responder_callback(cb_id, "Creando pedido en Dropi...")
            if message_id:
                # Quita los botones mientras se crea (evita doble clic).
                editar_plano(chat_id, message_id,
                             ficha.texto_confirmacion(a) + "\n\n⏳ Creando pedido en Dropi...")
            _correr_en_hilo(chat_id, message_id, a, _crear)
        elif accion == "va":
            a = _ficha_de(ns)
            n = ficha.ver_agencias(a)
            responder_callback(cb_id, f"{n} agencias en la ciudad" if n else "Sin agencias")
            editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
        elif accion == "ps":
            a = _ficha_de(ns)
            ok, det = ficha.pedir_agencia(a)
            if ok:
                responder_callback(cb_id, "Mensaje enviado al cliente")
                texto, _ = ficha.mensaje_pedir_agencia(a)
                enviar_plano(chat_id, f"📲 Le pedí la agencia a {_nombre_de(a)}:\n\n{texto}\n\n"
                                      "Cuando responda, toca «Reanalizar» en la ficha.")
            else:
                responder_callback(cb_id, "No se pudo enviar", True)
                enviar_plano(chat_id, f"⚠️ No se pudo escribir a {_nombre_de(a)}: {det}")
        elif accion == "x":
            a = _ficha_de(ns)
            a["preparado"] = None
            responder_callback(cb_id, "Cancelado")
            editar_plano(chat_id, message_id, ficha.texto_ficha(a), ficha.teclado(a))
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


_COMANDOS = (
    "/start", "/ayuda", "/help", "/nuevo", "/en_agencia", "/dropi_conectar",
    "/dropi_desconectar", "/resumen", "/ventas", "/pedido", "/crear_pedido", "/crear",
    "/pendientes", "/retiro", "/campos", "/seguimiento", "/reporte_diario", "/guia",
    "/dropi_pedidos", "/dropi_saldo", "/novedades", "/recordatorios", "/probar_plantilla",
    "/meta", "/meta_conectar", "/meta_desconectar",
)
# "/dropipedidos" -> "/dropi_pedidos" (copias del menu viejo sin guion bajo).
_SIN_GUION = {c.replace("_", ""): c for c in _COMANDOS}


def _normalizar_cmd(cmd):
    cmd = cmd.split("@", 1)[0]  # /resumen@VitashopBot -> /resumen
    return cmd if cmd in _COMANDOS else _SIN_GUION.get(cmd.replace("_", ""), cmd)


def manejar_comando(chat_id, texto):
    texto = (texto or "").strip()
    cmd = _normalizar_cmd(texto.split()[0].lower()) if texto else ""

    if cmd == "/meta_conectar":
        _pedir_conexion_meta(chat_id, "🔵 Conectar Meta Ads (Facebook).")
        return

    if cmd == "/meta_desconectar":
        meta.desconectar()
        enviar_plano(chat_id, "Meta desconectado. Usa /meta_conectar para volver a conectar.")
        return

    if cmd == "/meta":
        try:
            desde, hasta, etiqueta = _periodo_meta(texto.split(maxsplit=1)[1:] or [""])
        except ValueError:
            enviar_plano(chat_id, "Uso: /meta [hoy | ayer | 7d | mes | mes_pasado | "
                                  "dd/mm/aaaa | dd/mm/aaaa-dd/mm/aaaa]")
            return
        enviar_plano(chat_id, f"⏳ Consultando Meta Ads ({etiqueta})...")
        threading.Thread(target=_meta_resumen, args=(chat_id, desde, hasta, etiqueta),
                         daemon=True).start()
        return

    if cmd == "/novedades":
        enviar_plano(chat_id, "⏳ Revisando novedades y devoluciones en Dropi...")
        threading.Thread(target=_alerta_novedades, args=([chat_id], False),
                         daemon=True).start()
        return

    if cmd == "/recordatorios":
        enviar_plano(chat_id, "⏳ Revisando pedidos en agencia para recordar...")
        threading.Thread(target=_alerta_retiros, args=([chat_id], False),
                         daemon=True).start()
        return

    if cmd == "/probar_plantilla":
        partes = texto.split()
        if len(partes) < 2:
            enviar_plano(chat_id, "Uso: /probar_plantilla <tu celular>\n"
                                  "Envía la plantilla de recordatorio de retiro con datos de "
                                  "ejemplo a ESE número, para comprobar que llega bien.")
            return
        ok, det = alertas.probar_plantilla(partes[1])
        enviar_plano(chat_id, ("✅ " if ok else "❌ ") + f"{det} → {partes[1]} "
                              f"({alertas.PLANTILLA_RETIRO})")
        return

    if cmd == "/nuevo":
        asistente.olvidar(chat_id)
        enviar_plano(chat_id, "🆕 Listo, empecemos de nuevo. ¿Qué necesitas?")
        return

    if cmd in ("/start", "/ayuda", "/help"):
        # Sin Markdown: los guiones bajos de /en_agencia o /dropi_pedidos se
        # tomaban como cursiva y Telegram mostraba /enagencia, /dropipedidos.
        enviar_plano(chat_id,
               "👋 Bot de ventas VitashopEc\n\n"
               "💬 Puedes escribirme o mandarme un audio con lo que necesites, por ejemplo:\n"
               "«¿qué pedidos siguen pendientes en Dropi?», «busca a Hugo Montenegro», "
               "«¿cuánto stock queda de Mega Red?», «¿qué agencias hay en Babahoyo?».\n"
               "/nuevo — empezar una conversación nueva con el asistente\n\n"
               "Comandos disponibles:\n"
               "• /resumen — ventas de hoy\n"
               "• /resumen dd/mm/aaaa — ventas de una fecha\n"
               "• /ventas — lista de pedidos\n"
               "• /ventas dd/mm/aaaa — pedidos por fecha\n"
               "• /crear_pedido <teléfono> — ficha de la venta (promo + agencia) con botón para crear en Dropi\n"
               "• /en_agencia — reporte HTML de pedidos en agencia Servientrega "
               "(Dropi MCP) con botón de WhatsApp\n"
               "• /dropi_conectar — iniciar sesión en Dropi (MCP)\n"
               "• /reporte_diario — reporte completo del día (resumen + ventas + retiros)\n"
               "• /guia <número> — estado de una guía en Dropi\n"
               "• /dropi_pedidos [estado] [n] — pedidos de Dropi (últimos 30 días) + HTML\n"
               "• /novedades — pedidos con novedad o devolución (y plantilla al cliente)\n"
               "• /meta [hoy|ayer|7d|mes] — gasto y conversaciones por campaña en Meta Ads\n"
               "• /meta_conectar — conectar con Facebook (Meta Ads)")
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

    if cmd in ("/crear_pedido", "/crear"):
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar_plano(chat_id, f"Uso: {cmd} <teléfono>  (ej: {cmd} 995276111)")
            return
        telefono = partes[1].strip()
        enviar(chat_id, f"⏳ Analizando la venta de {telefono}...")
        try:
            campos = _campos_por_telefono(telefono)
            if not campos:
                enviar(chat_id, f"No encontré una venta con el teléfono {telefono}.")
                return
            a = ficha.analizar(campos, forzar=True)
            if cmd == "/crear_pedido":
                enviar_ficha(chat_id, a)
                return
            # /crear: si algo bloquea, muestra la ficha para que veas por que;
            # si no, muestra el resumen con el boton de confirmar.
            motivos = ficha.bloqueos(a)
            if motivos:
                enviar_plano(chat_id, "⚠️ No se puede crear: " + "; ".join(motivos))
                enviar_ficha(chat_id, a)
            elif not _correr_en_hilo(chat_id, None, a, _preparar):
                enviar_plano(chat_id, "Ya se está procesando este pedido.")
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

            _enviar_html(chat_id,
                         reporte_html.html_retiros(filas, f"Consulta: {f_s} ({scope})"),
                         "seguimiento_retiro",
                         f"📦 Seguimiento retiro Servientrega · {f_s}\n"
                         f"Pedidos para retiro: {len(filas)}")

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
                _enviar_html(chat_id, reporte_html.html_retiros(filas, "Últimas 24h"),
                             "seguimiento_retiro",
                             f"📦 Seguimiento retiro Servientrega · "
                             f"{chateapro.hoy_ecuador().strftime('%d/%m/%Y')}\n"
                             f"Pedidos para retiro: {len(filas)}")
        except Exception as e:
            enviar(chat_id, f"❌ Error al generar el reporte diario: {e}")
        return

    if cmd == "/guia":
        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar(chat_id, "Uso: /guia <número>  (ej: /guia 1800151524)")
            return
        enviar_plano(chat_id, f"⏳ Buscando la guía {partes[1].strip()} en Dropi...")
        threading.Thread(target=_guia_mcp, args=(chat_id, partes[1].strip()),
                         daemon=True).start()
        return

    if cmd == "/dropi_pedidos":
        partes = texto.split()[1:]
        n = 15
        if partes and partes[-1].isdigit():
            n = int(partes.pop())
        estado = " ".join(partes).upper() or None
        enviar_plano(chat_id, "⏳ Consultando pedidos en Dropi...")
        threading.Thread(target=_dropi_pedidos_mcp, args=(chat_id, estado, n),
                         daemon=True).start()
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


def _guia_mcp(chat_id, numero):
    """/guia: pedido de Dropi con esa guia (MCP)."""
    try:
        p = dropi_pedido.buscar_guia(numero)
    except dropi_mcp.AuthRequired:
        _pedir_conexion_dropi(chat_id, "🔐 Dropi pide iniciar sesión para consultar la guía.")
        return
    except Exception as e:
        enviar_plano(chat_id, f"❌ Error consultando la guía: {e}")
        return
    if not p:
        enviar_plano(chat_id, f"No encontré la guía {numero} en Dropi (últimos 90 días).")
        return
    cli = p.get("customer") or {}
    L = [f"📦 Guía {p.get('tracking_code') or numero} — pedido #{p.get('order_id')}",
         f"🚚 {p.get('shipping_company') or '-'} · {p.get('status', '-')}",
         f"👤 {cli.get('name', '-')} · 📲 {cli.get('phone', '-')}",
         f"📍 {cli.get('city', '-')}: {cli.get('address', '-')}", ""]
    for it in p.get("items") or []:
        L.append(f"• {it.get('qty')}x {it.get('product_name')} (${it.get('unit_price')})")
    L += [f"💵 Total ${p.get('total')} {p.get('currency', '')}",
          f"🗓️ Creado {str(p.get('created_at', ''))[:16].replace('T', ' ')} · "
          f"actualizado {str(p.get('updated_at', ''))[:16].replace('T', ' ')}"]
    enviar_plano(chat_id, "\n".join(L))


def _dropi_pedidos_mcp(chat_id, estado, n):
    """/dropi_pedidos [ESTADO] [n]: pedidos de los ultimos 30 dias (MCP) + HTML."""
    try:
        items = dropi_pedido.listar_pedidos(estado, n)
    except dropi_mcp.AuthRequired:
        _pedir_conexion_dropi(chat_id, "🔐 Dropi pide iniciar sesión para listar pedidos.")
        return
    except Exception as e:
        enviar_plano(chat_id, f"❌ Error al listar pedidos Dropi: {e}")
        return
    filtro = f" ({estado})" if estado else ""
    if not items:
        enviar_plano(chat_id, f"Sin pedidos{filtro} en Dropi en los últimos 30 días.\n"
                              "Estados válidos: PENDIENTE, GUIA_GENERADA, ENTREGADO, "
                              "PARA RETIRO EN AGENCIA SERVIENTREGA, NOVEDAD, CANCELADO…")
        return
    L = [f"📦 Pedidos Dropi{filtro}: {len(items)}", ""]
    for o in items[:30]:
        L.append(f"• #{o.get('order_id')} {o.get('customer_name', '-')} — "
                 f"{o.get('city', '-')}\n  {o.get('status', '-')} · ${o.get('total')} · "
                 f"{o.get('shipping_company', '-')} · guía {o.get('tracking_code') or '-'}")
    enviar_plano(chat_id, "\n".join(L)[:4000])
    filas = [{"pedido": o.get("order_id"), "fecha": o.get("created_at"),
              "nombre": o.get("customer_name"), "telefono": o.get("customer_phone"),
              "ciudad": o.get("city"), "estado": o.get("status"), "valor": o.get("total"),
              "transportadora": o.get("shipping_company"),
              "guia": o.get("tracking_code") or ""} for o in items]
    _enviar_html(chat_id, reporte_html.html_pedidos(filas, f"Últimos 30 días{filtro}"),
                 "pedidos_dropi", f"📦 Pedidos Dropi{filtro}: {len(filas)}")


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
        if meta.es_ruta_callback(self.path):
            self._meta()
            return
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

    def _meta(self):
        try:
            est = meta.completar_desde_url("http://x" + self.path)
            ok, msg = True, "✅ Facebook conectado. Ya puedes volver a Telegram."
        except Exception as e:
            ok, est, msg = False, None, f"❌ No se pudo conectar: {html.escape(str(e))}"
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<body style='font:18px system-ui;padding:24px'>{msg}</body>".encode())
        if ok:
            for c in ALLOWED:
                _meta_conectado_ok(c, est)

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


# --- Meta Ads (Conectar con Facebook) ---

def _pedir_conexion_meta(chat_id, motivo):
    try:
        url = meta.url_login()
    except meta.MetaError as e:
        enviar_plano(chat_id, f"❌ {e}\nRevisa la configuración de la app de Meta (README).")
        return
    texto = (f"{motivo}\n\n1) Toca «Conectar con Facebook» y entra con tu cuenta.\n"
             "2) Acepta el permiso para ver el rendimiento de tus anuncios.\n")
    if urlparse(meta.REDIRECT_URI).hostname in ("localhost", "127.0.0.1"):
        texto += ("3) Si al final el navegador muestra «no se puede acceder al sitio», "
                  "copia la URL COMPLETA de la barra de direcciones y pégala aquí.\n")
    texto += "\nEl link vence en 15 minutos. El acceso dura unos 60 días."
    enviar_plano(chat_id, texto, [[{"text": "🔵 Conectar con Facebook", "url": url}]])


def _meta_conectado_ok(chat_id, est):
    cuentas = est.get("cuentas") or []
    L = [f"✅ Facebook conectado: {est.get('usuario') or '-'}"
         + (f" · vence en {est['dias_restantes']} días" if est.get("dias_restantes") else "")]
    if not cuentas:
        L.append("⚠️ Tu usuario no tiene cuentas publicitarias visibles.")
        enviar_plano(chat_id, "\n".join(L))
        return
    L.append(f"📣 Cuenta activa: {est.get('cuenta')}")
    botones = None
    if len(cuentas) > 1:
        L.append("Tienes varias cuentas; elige otra si no es esa:")
        botones = [[{"text": f"{c['nombre']} ({c['id']})", "callback_data": f"mc:{c['id']}"}]
                   for c in cuentas[:8]]
    L.append("Prueba: /meta mes")
    enviar_plano(chat_id, "\n".join(L), botones)


def _procesar_url_meta(chat_id, texto):
    try:
        est = meta.completar_desde_url(texto)
    except Exception as e:
        enviar_plano(chat_id, f"❌ No se pudo conectar Facebook: {e}\n"
                              "Usa /meta_conectar para intentar de nuevo.")
        return
    _meta_conectado_ok(chat_id, est)


def _periodo_meta(args):
    """Devuelve (desde, hasta, etiqueta). Lanza ValueError si no se entiende."""
    a = (args[0] if args else "").strip().lower()
    hoy = chateapro.hoy_ecuador()
    if a in ("", "hoy"):
        return hoy, hoy, f"hoy {hoy:%d/%m/%Y}"
    if a == "ayer":
        d = hoy - dt.timedelta(days=1)
        return d, d, f"ayer {d:%d/%m/%Y}"
    if a in ("7d", "semana"):
        return hoy - dt.timedelta(days=6), hoy, "últimos 7 días"
    if a == "mes":
        return hoy.replace(day=1), hoy, f"{hoy:%m/%Y} (hasta hoy)"
    if a in ("mes_pasado", "mespasado"):
        fin = hoy.replace(day=1) - dt.timedelta(days=1)
        return fin.replace(day=1), fin, f"{fin:%m/%Y}"
    partes = [x.strip() for x in a.split("-")]
    fechas = [dt.datetime.strptime(x, "%d/%m/%Y").date() for x in partes if x]
    if len(fechas) == 1:
        return fechas[0], fechas[0], f"{fechas[0]:%d/%m/%Y}"
    if len(fechas) == 2:
        return fechas[0], fechas[1], f"{fechas[0]:%d/%m/%Y} al {fechas[1]:%d/%m/%Y}"
    raise ValueError(a)


def _meta_resumen(chat_id, desde, hasta, etiqueta):
    try:
        enviar_plano(chat_id, meta.resumen(desde, hasta, etiqueta)[:4000])
    except meta.AuthRequired as e:
        _pedir_conexion_meta(chat_id, f"🔵 {e} Conecta de nuevo con Facebook.")
    except Exception as e:
        enviar_plano(chat_id, f"❌ Error consultando Meta Ads: {e}")


def _aviso_vencimiento_meta():
    est = meta.estado()
    if not est.get("conectado") and not meta.configurada():
        return
    dias = est.get("dias_restantes")
    if est.get("usuario") and dias is not None and dias <= 7:
        for c in ALLOWED:
            _pedir_conexion_meta(c, f"⏰ El acceso a Facebook vence en {max(dias, 0)} días. "
                                    "Vuelve a conectar para que /meta siga funcionando.")


MENU_COMANDOS = [
    ("en_agencia", "Pedidos en agencia Servientrega (HTML + WhatsApp)"),
    ("resumen", "Ventas de hoy"),
    ("ventas", "Lista de pedidos de hoy"),
    ("crear_pedido", "Ficha y crear pedido: /crear_pedido <teléfono>"),
    ("reporte_diario", "Reporte completo del día"),
    ("guia", "Estado de una guía: /guia <número>"),
    ("dropi_pedidos", "Pedidos de Dropi (MCP) + HTML"),
    ("dropi_conectar", "Iniciar sesión en Dropi (MCP)"),
    ("novedades", "Novedades y devoluciones de Dropi"),
    ("meta", "Meta Ads: gasto y conversaciones"),
    ("meta_conectar", "Conectar con Facebook (Meta Ads)"),
    ("nuevo", "Nueva conversación con el asistente"),
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
ALERTAS_DIARIAS = os.environ.get("ALERTAS_DIARIAS", "true").lower() == "true"
HORA_ALERTAS = os.environ.get("HORA_ALERTAS", "09:00")
_ultima_alerta_dia = None


def generar_reporte_diario():
    """
    Arma el reporte diario completo (resumen + ventas + seguimiento de retiro).
    Devuelve (texto, filas_seguimiento): el texto listo para /sendMessage y las
    filas del seguimiento (para adjuntar como HTML si hay datos).
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
                _enviar_html(chat_id, reporte_html.html_retiros(filas, "Últimas 24h"),
                             "seguimiento_retiro",
                             f"📦 Seguimiento retiro Servientrega · "
                             f"{chateapro.hoy_ecuador().strftime('%d/%m/%Y')}\n"
                             f"Pedidos para retiro: {len(filas)}")
        except Exception as e:
            print(f"Error enviando reporte diario a {chat_id}: {e}")


# Ventas ya notificadas: {user_ns: "Fecha de compra"}. Se guarda en disco para
# que un reinicio/despliegue no "trague" las ventas que llegaron mientras tanto.
NOTIFICADAS_PATH = os.environ.get(
    "VENTAS_NOTIFICADAS_PATH",
    datos.ruta("ventas_notificadas.json"))
_ya_notificadas = {}
# Ultima revision de cada suscriptor: {user_ns: (ts, last_interaction)}.
_ultima_revision = {}
_ultimo_check = 0
_primera_pasada = True

# Cada cuantos segundos se vuelve a revisar a un suscriptor aun sin compra,
# aunque no haya escrito nada nuevo (si escribio, se revisa en el siguiente ciclo).
REVISAR_NUEVOS_INTERVALO = int(
    os.environ.get("REVISAR_NUEVOS_INTERVAL_SECONDS", "1800")
)


def _cargar_notificadas():
    """Devuelve (dict, existia_el_archivo)."""
    try:
        with open(NOTIFICADAS_PATH, encoding="utf-8") as f:
            return json.load(f), True
    except (OSError, ValueError):
        return {}, False


def _guardar_notificadas():
    # Solo se conservan las de los ultimos dias para que el archivo no crezca.
    limite = chateapro.hoy_ecuador() - dt.timedelta(days=7)
    for ns in [k for k, v in _ya_notificadas.items()
               if (chateapro._parse_fecha_compra(v) or limite) < limite]:
        _ya_notificadas.pop(ns, None)
    try:
        tmp = NOTIFICADAS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_ya_notificadas, f, ensure_ascii=False)
        os.replace(tmp, NOTIFICADAS_PATH)
    except OSError as e:
        print("No se pudo guardar ventas_notificadas.json:", e)


def revisar_ventas_nuevas():
    """
    Detecta ventas de hoy no notificadas y las envia a los chats permitidos.

    Para no saturar la API:
      - 1 request por ciclo para listar los suscriptores de las ultimas 24h,
      - solo se vuelven a leer los campos de un suscriptor si escribio algo
        nuevo (cambio su last_interaction) o cada REVISAR_NUEVOS_INTERVALO.

    Tras un reinicio se notifican las ventas de hoy que no estaban en
    ventas_notificadas.json. Solo la primera vez que el bot corre (sin archivo)
    se marcan en silencio para no mandar todo el historial del dia.
    """
    global _primera_pasada, _ya_notificadas
    try:
        suscriptores = chateapro.listar_suscriptores_recientes()
    except Exception as e:
        print("Error revisando ventas:", e)
        return

    silencioso = False
    if _primera_pasada:
        _ya_notificadas, existia = _cargar_notificadas()
        silencioso = not existia

    hoy = chateapro.hoy_ecuador()
    ahora = time.time()
    cambios = False

    for s in suscriptores:
        ns = s.get("user_ns")
        if not ns:
            continue
        interaccion = s.get("last_interaction") or ""
        ts, vista = _ultima_revision.get(ns, (0, None))
        escribio = vista is not None and interaccion != vista
        if not _primera_pasada and not escribio and (ahora - ts) < REVISAR_NUEVOS_INTERVALO:
            continue
        _ultima_revision[ns] = (ahora, interaccion)
        try:
            campos = chateapro.obtener_campos(ns, fresco=escribio)
        except Exception as e:
            print(f"Error obteniendo campos de {ns}: {e}")
            continue
        # Registra la fecha de deteccion de guias en estado de retiro, para
        # calcular dias sin retirar de forma confiable en /seguimiento.
        chateapro.registrar_retiros(campos)
        if not chateapro.es_venta_de_fecha(campos, hoy):
            continue
        fecha = campos.get(chateapro.CAMPO_FECHA, "")
        if _ya_notificadas.get(ns) == fecha:
            continue
        _ya_notificadas[ns] = fecha
        cambios = True
        if silencioso:
            continue
        print(f"Venta nueva: {ns} ({fecha})")
        _notificar_venta(campos)

    if cambios or (_primera_pasada and silencioso):
        _guardar_notificadas()
    _primera_pasada = False


# --- Asistente conversacional (texto libre y notas de voz) ---

# Borradores de WhatsApp propuestos por el asistente: id -> {ns, texto, ts}
_borradores = {}
_borradores_lock = threading.Lock()
_un_turno = {}  # chat_id -> Lock: una pregunta a la vez por chat


def _escribiendo(chat_id):
    try:
        requests.post(f"{TG_API}/sendChatAction",
                      data={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass


def _guardar_borrador(ns, texto):
    bid = f"{int(time.time() * 1000) % 10**10:x}"
    with _borradores_lock:
        ahora = time.time()
        for k in [k for k, v in _borradores.items() if ahora - v["ts"] > 86400]:
            _borradores.pop(k, None)
        _borradores[bid] = {"ns": ns, "texto": texto, "ts": ahora}
    return bid


def _atender_asistente(chat_id, texto):
    if not asistente.disponible():
        enviar_plano(chat_id, "🤖 El asistente no está activo (falta ANTHROPIC_API_KEY).\n"
                              "Mientras tanto usa los comandos: /ayuda")
        return
    candado = _un_turno.setdefault(chat_id, threading.Lock())
    with candado:
        _escribiendo(chat_id)
        try:
            respuesta, acciones = asistente.responder(chat_id, texto)
        except Exception as e:
            print("Error en asistente:", e)
            asistente.olvidar(chat_id)
            enviar_plano(chat_id, f"❌ El asistente falló: {e}")
            return
        enviar_plano(chat_id, respuesta[:4000])
        for acc in acciones:
            try:
                _accion_asistente(chat_id, acc)
            except Exception as e:
                enviar_plano(chat_id, f"❌ No pude mostrar la acción: {e}")


def _accion_asistente(chat_id, acc):
    if acc["tipo"] == "ficha":
        enviar_ficha(chat_id, _ficha_de(acc["ns"], forzar=True))
    elif acc["tipo"] == "mensaje":
        campos = chateapro.obtener_campos(acc["ns"])
        nombre = campos.get(chateapro.CAMPO_NOMBRE) or campos.get("_name") or acc["ns"]
        bid = _guardar_borrador(acc["ns"], acc["texto"])
        enviar_plano(chat_id,
                     f"✉️ Borrador para {nombre} ({campos.get('_phone', '-')}):\n\n"
                     f"{acc['texto']}",
                     [[{"text": "📲 Enviar por WhatsApp", "callback_data": f"wa:{bid}"},
                       {"text": "✖️ Descartar", "callback_data": f"wx:{bid}"}]])
    elif acc["tipo"] == "conectar_meta":
        _pedir_conexion_meta(chat_id, "🔵 Para responderte necesito acceso a Meta Ads.")
    elif acc["tipo"] == "conectar_dropi":
        _pedir_conexion_dropi(chat_id, "🔐 Dropi pide iniciar sesión para responderte.")


def _atender_audio(chat_id, msg):
    if not transcribir.disponible():
        enviar_plano(chat_id, "🎙️ Para entender audios falta GROQ_API_KEY u OPENAI_API_KEY.")
        return
    media = msg.get("voice") or msg.get("audio")
    _escribiendo(chat_id)
    try:
        info = requests.get(f"{TG_API}/getFile", params={"file_id": media["file_id"]},
                            timeout=30).json()["result"]
        url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}"
        contenido = requests.get(url, timeout=60).content
        texto = transcribir.transcribir(contenido, info["file_path"].rsplit("/", 1)[-1],
                                        media.get("mime_type") or "audio/ogg")
    except Exception as e:
        enviar_plano(chat_id, f"❌ No pude entender el audio: {e}")
        return
    enviar_plano(chat_id, f"🎙️ Entendí: «{texto}»")
    _atender_asistente(chat_id, texto)


def _callback_borrador(cb_id, chat_id, message_id, accion, bid):
    with _borradores_lock:
        b = _borradores.pop(bid, None)
    if b is None:
        responder_callback(cb_id, "Ese borrador ya no existe.", True)
        return
    if accion == "wx":
        responder_callback(cb_id, "Descartado")
        editar_plano(chat_id, message_id, f"✖️ Borrador descartado:\n\n{b['texto']}")
        return
    responder_callback(cb_id, "Enviando...")
    ok, det = chateapro.enviar_texto(b["ns"], b["texto"])
    if ok:
        editar_plano(chat_id, message_id, f"✅ Enviado por WhatsApp:\n\n{b['texto']}")
    else:
        editar_plano(chat_id, message_id,
                     f"❌ No se envió ({det}). Envíalo a mano desde Chatea Pro:\n\n{b['texto']}")


# --- Alertas de Dropi: novedades/devoluciones y recordatorio de retiro ---

_retiros_pendientes = []
_retiros_lock = threading.Lock()


def _alerta_novedades(chats, solo_nuevas=True):
    try:
        r = alertas.revisar_novedades(solo_nuevas)
    except dropi_mcp.AuthRequired:
        for c in chats:
            _pedir_conexion_dropi(c, "🔐 Dropi pide iniciar sesión para revisar novedades.")
        return
    except Exception as e:
        for c in chats:
            enviar_plano(c, f"❌ Error revisando novedades: {e}")
        return
    nov, dev = r["novedades"], r["devoluciones"]
    que = " nuevas" if solo_nuevas else ""
    if not nov and not dev:
        if not solo_nuevas:
            for c in chats:
                enviar_plano(c, "✅ Sin novedades ni devoluciones en los últimos 30 días.")
        return
    L = [f"🚨 Novedades{que}: {len(nov)} · Devoluciones{que}: {len(dev)}", ""]
    for f in dev[:15]:
        L.append(f"↩️ #{f['pedido']} {f['nombre']} — {f['ciudad']} — ${f['valor']} — "
                 f"{f['estado']} ({f['transportadora']})")
    for c in chats:
        enviar_plano(c, "\n".join(L)[:4000])
        for f in nov[:10]:
            enviar_plano(c, f"⚠️ NOVEDAD #{f['pedido']} · {f['nombre']} · "
                            f"{reporte_agencia.limpiar_telefono(f['telefono'])}\n"
                            f"📍 {f['ciudad']} · {f['transportadora']} · guía {f['guia'] or '-'} · "
                            f"${f['valor']}",
                         [[{"text": "📲 Enviar plantilla de novedad",
                            "callback_data": f"nv:{f['pedido']}"}]])
        filas = nov + dev
        _enviar_html(c, reporte_html.html_pedidos(filas, "Últimos 30 días",
                                                  "🚨 Novedades y devoluciones"),
                     "novedades", f"🚨 Novedades y devoluciones: {len(filas)}")


def _texto_recordatorios(enviados, fallidos, pendientes, en_agencia=None):
    L = []
    if en_agencia is not None:
        L.append(f"🏢 Pedidos en agencia: {en_agencia} · recordatorio a los "
                 f"{alertas.RECORDATORIO_DIAS} días")
    if enviados:
        L.append(f"✅ Recordatorio enviado a {len(enviados)}:")
        L += [f"   • {p['nombre']} — {p['ciudad']} — guía {p['guia']}" for p in enviados[:25]]
    for p, err in fallidos:
        L.append(f"❌ {p['nombre']} ({p['telefono']}): {err}")
    if pendientes:
        L.append(f"⏳ Listos para recordar: {len(pendientes)}")
        L += [f"   • {p['nombre']} — {p['ciudad']} — {p['dias']} días" for p in pendientes[:25]]
    if not (enviados or fallidos or pendientes):
        L.append("Nadie para recordar hoy.")
    return "\n".join(L)


def _alerta_retiros(chats, automatico=True):
    if alertas.RECORDATORIO_MODO == "off":
        if not automatico:
            for c in chats:
                enviar_plano(c, "Recordatorios desactivados (RECORDATORIO_MODO=off).")
        return
    try:
        # Manual (/recordatorios) siempre pide confirmacion; el diario sigue el modo.
        r = alertas.revisar_retiros(enviar=None if automatico else False)
    except dropi_mcp.AuthRequired:
        for c in chats:
            _pedir_conexion_dropi(c, "🔐 Dropi pide iniciar sesión para los recordatorios.")
        return
    except Exception as e:
        for c in chats:
            enviar_plano(c, f"❌ Error en recordatorios de retiro: {e}")
        return
    with _retiros_lock:
        _retiros_pendientes[:] = r["pendientes"]
    texto = _texto_recordatorios(r["enviados"], r["fallidos"], r["pendientes"], r["en_agencia"])
    botones = ([[{"text": f"📲 Enviar {len(r['pendientes'])} recordatorios",
                  "callback_data": "rr:todos"}]] if r["pendientes"] else None)
    for c in chats:
        enviar_plano(c, texto[:4000], botones)


def _alertas_diarias():
    chats = list(ALLOWED)
    _alerta_novedades(chats, solo_nuevas=True)
    _alerta_retiros(chats, automatico=True)
    try:
        _aviso_vencimiento_meta()
    except Exception as e:
        print("Error revisando el acceso a Meta:", e)


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

    global _ultimo_check, _ultimo_reporte_dia, _ultima_alerta_dia
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
            if meta.es_url_callback(texto):
                threading.Thread(target=_procesar_url_meta, args=(chat_id, texto),
                                 daemon=True).start()
                continue
            if dropi_mcp.es_url_callback(texto):
                _procesar_url_dropi(chat_id, texto)
                continue
            if msg.get("voice") or msg.get("audio"):
                threading.Thread(target=_atender_audio, args=(chat_id, msg),
                                 daemon=True).start()
                continue
            if texto and not texto.startswith("/"):
                threading.Thread(target=_atender_asistente, args=(chat_id, texto),
                                 daemon=True).start()
                continue
            manejar_comando(chat_id, texto)

        # 2) Revisar ventas nuevas cada INTERVALO_CHECK segundos.
        if NOTIFICAR and (time.time() - _ultimo_check) >= INTERVALO_CHECK:
            _ultimo_check = time.time()
            revisar_ventas_nuevas()

        # 3) Alertas de Dropi (novedades + recordatorio de retiro), una vez al dia.
        if ALERTAS_DIARIAS and ALLOWED:
            ahora = dt.datetime.now(chateapro.TZ_ECUADOR)
            hoy_s = ahora.strftime("%Y-%m-%d")
            if ahora.strftime("%H:%M") >= HORA_ALERTAS and _ultima_alerta_dia != hoy_s:
                _ultima_alerta_dia = hoy_s
                threading.Thread(target=_alertas_diarias, daemon=True).start()

        # 4) Reporte diario automatico al final del dia (hora de Ecuador).
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
