"""
Ficha de venta: une Chatea Pro + OpenAI + servicio de agencias + Hermes.

Flujo por cada venta:
  1. valida los datos del CRM (campos de usuario de Chatea Pro)
  2. lee el chat y le pide a OpenAI la promo y la ubicacion de retiro
  3. consulta el servicio de agencias con esa ubicacion
  4. arma el texto de la ficha y los botones de Telegram
  5. al pulsar "Crear", delega la creacion a Hermes (MCP de Dropi)
"""

import re

import agencias
import chateapro
import hermes
import ia
import promos

# Cache en memoria del analisis por suscriptor (para los botones).
_CACHE = {}

# Datos del CRM que dejan de ser bloqueantes cuando ya hay promo / agencia.
_CUBIERTOS_POR_PROMO = {"Productos escogidos", "Valor de la compra"}
_CUBIERTOS_POR_AGENCIA = {"Provincia", "Ciudad"}

_RANGO_ESTADO = {"unica": 0, "probable": 1, "falta_sector": 2,
                 "sin_ciudad": 3, "error": 4}


def _buscar_agencia(textos):
    """Prueba cada texto de ubicacion y se queda con el mejor resultado."""
    mejor = None
    for t in textos:
        if not t:
            continue
        r = agencias.buscar_agencia(t)
        if mejor is None or _RANGO_ESTADO[r["estado"]] < _RANGO_ESTADO[mejor["estado"]]:
            mejor = r
        if r["estado"] in ("unica", "probable"):
            break
    return mejor or agencias.buscar_agencia("")


def analizar(campos, forzar=False):
    """Devuelve (y cachea) el analisis completo de una venta."""
    ns = campos.get("_user_ns", "")
    if not forzar and ns in _CACHE:
        return _CACHE[ns]

    productos = campos.get(chateapro.CAMPO_PRODUCTOS, "")
    valor = campos.get(chateapro.CAMPO_VALOR, "")

    # --- chat + IA ---
    try:
        chat = chateapro.obtener_chat(ns)
        error_chat = ""
    except Exception as e:  # el chat es un extra: la ficha sale igual
        chat, error_chat = [], str(e)
    analisis_ia = ia.analizar_venta(chat, productos, valor)

    # --- promo: primero reglas sobre el CRM, luego IA ---
    promo = promos.detectar_promo(productos, valor)
    origen = "valor/productos del CRM" if promo else ""
    if not promo and analisis_ia["promo"]:
        promo, origen = analisis_ia["promo"], "IA (chat)"
    aviso_promo = ""
    if promo and analisis_ia["promo"] and analisis_ia["promo"] != promo:
        aviso_promo = (f"La IA entendió otra promo ({analisis_ia['promo']}); "
                       f"se usó {promo} por el CRM.")

    # --- agencia ---
    crm_texto = " ".join(x for x in (campos.get(chateapro.CAMPO_CIUDAD, ""),
                                     campos.get(chateapro.CAMPO_DIRECCION, "")) if x)
    res = _buscar_agencia([analisis_ia["ubicacion_texto"], crm_texto])
    opciones = ([res["sugerida"]] if res["sugerida"] else []) + res["alternativas"]
    elegida = res["sugerida"] if res["estado"] in ("unica", "probable") else None

    a = {
        "ns": ns, "campos": campos, "chat": chat, "error_chat": error_chat,
        "ia": analisis_ia, "promo": promo, "promo_origen": origen,
        "aviso_promo": aviso_promo, "agencia_res": res,
        "opciones": opciones, "elegida": elegida,
    }
    _CACHE[ns] = a
    return a


def faltantes(a):
    """Datos del CRM que faltan y SI bloquean la creacion."""
    faltan = set(chateapro.datos_faltantes(a["campos"]))
    if a["promo"]:
        faltan -= _CUBIERTOS_POR_PROMO
    if a["elegida"]:
        faltan -= _CUBIERTOS_POR_AGENCIA
    return sorted(faltan)


def bloqueos(a):
    """Motivos por los que aun no se puede crear el pedido."""
    motivos = []
    if not a["promo"]:
        motivos.append("No se pudo determinar la promo")
    if not a["elegida"]:
        motivos.append("No detecté agencia, revisa el chat")
    f = faltantes(a)
    if f:
        motivos.append("Faltan datos: " + ", ".join(f))
    ya = hermes.ya_creado(a["ns"])
    if ya:
        motivos.append(f"Ya se creó el pedido #{ya.get('order_id')}")
    return motivos


def elegir_agencia(a, indice):
    if 0 <= indice < len(a["opciones"]):
        a["elegida"] = a["opciones"][indice]
        return True
    return False


# ------------------------------------------------------------------ texto --

def _recorta(s, n=140):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _lineas_cliente(a, n=3):
    cliente = [l[len("Cliente: "):] for l in a["chat"] if l.startswith("Cliente: ")]
    return [_recorta(l) for l in cliente[-n:]]


def texto_ficha(a):
    c = a["campos"]
    motivos = bloqueos(a)
    nombre = c.get(chateapro.CAMPO_NOMBRE) or c.get("_name") or "-"
    ya = hermes.ya_creado(a["ns"])

    if ya:
        estado = f"✅ PEDIDO YA CREADO #{ya.get('order_id')}"
    elif not motivos:
        estado = "✅ LISTA PARA CREAR EN DROPI"
    else:
        estado = "⚠️ REVISAR ANTES DE CREAR"

    L = [f"🔔 NUEVA VENTA — {estado}", ""]
    if a["ia"]["quiere_cancelar"]:
        L += ["🚫 OJO: el cliente parece querer CANCELAR. Revisa el chat.", ""]

    L += [
        f"👤 {nombre}",
        f"📲 {c.get('_phone', '-')}",
        f"🛍️ CRM: {c.get(chateapro.CAMPO_PRODUCTOS, '-')} — ${c.get(chateapro.CAMPO_VALOR, '-')}",
        "",
    ]

    if a["promo"]:
        L.append(f"🎁 {promos.descripcion(a['promo'])}")
        L.append(f"   (detectada por {a['promo_origen']})")
        if a["aviso_promo"]:
            L.append(f"   ⚠️ {a['aviso_promo']}")
    else:
        L.append("🎁 Promo: no detectada")

    res = a["agencia_res"]
    L.append("")
    if a["elegida"]:
        ag = a["elegida"]
        etiqueta = {"unica": "única en la ciudad", "probable": "la más cercana al sector"}.get(
            res["estado"], "elegida por ti")
        L += [f"🏢 Agencia ({etiqueta}):",
              f"   {ag.get('nombre')} — {ag.get('sector', '')}",
              f"   📍 {ag.get('direccion', '-')}",
              f"   🕒 L-V {ag.get('horario_lun_vie', '-')}"]
    else:
        if res["estado"] == "falta_sector":
            L.append(f"🏢 No detecté agencia: {res['ciudad']} tiene {res['total_ciudad']} y el "
                     "cliente no dio sector. Elige una abajo o revisa el chat.")
        else:
            L.append("🏢 No detecté agencia, revisa el chat")
        if res["estado"] == "error":
            L.append(f"   ({res['mensaje']})")

    if a["ia"]["ubicacion_texto"]:
        L.append(f"   Ubicación entendida: “{_recorta(a['ia']['ubicacion_texto'])}”")

    # Evidencia del chat cuando hay dudas.
    if motivos and not ya:
        ult = _lineas_cliente(a)
        if ult:
            L += ["", "💬 Lo último que escribió el cliente:"] + [f"   • {x}" for x in ult]
        elif a["error_chat"]:
            L += ["", f"💬 No pude leer el chat: {_recorta(a['error_chat'], 100)}"]
        else:
            L += ["", "💬 No se encontraron mensajes del cliente en el chat."]

    if a["ia"]["notas"]:
        L += ["", f"📝 IA: {_recorta(a['ia']['notas'], 200)}"]
    if motivos and not ya:
        L += ["", "Pendiente:"] + [f"   ✖ {m}" for m in motivos]
    return "\n".join(L)


def teclado(a):
    """Botones inline de Telegram (lista de filas)."""
    ns = a["ns"]
    filas = []
    ya = hermes.ya_creado(ns)
    if ya:
        return []
    if not bloqueos(a):
        filas.append([{"text": "✅ Crear pedido en Dropi", "callback_data": f"crear:{ns}"}])
    # Agencias alternativas (maximo 3) para cambiar o elegir.
    for i, ag in enumerate(a["opciones"][:3]):
        marca = "● " if a["elegida"] is ag else ""
        filas.append([{"text": f"{marca}🏢 {ag.get('nombre', '?')}"[:60],
                       "callback_data": f"ag:{ns}:{i}"}])
    filas.append([{"text": "🔄 Reanalizar", "callback_data": f"re:{ns}"}])
    return filas


# ------------------------------------------------------------------ crear --

def crear_pedido(a):
    """
    Delega a Hermes la creacion. Devuelve el dict de hermes.crear_pedido,
    o ok=False si hay bloqueos / ya existe / Hermes no esta configurado.
    """
    motivos = bloqueos(a)
    if motivos:
        return {"ok": False, "order_id": None, "guia": None,
                "mensaje": "No se puede crear: " + "; ".join(motivos)}
    if not hermes.disponible():
        return {"ok": False, "order_id": None, "guia": None,
                "mensaje": "HERMES_URL no está configurada en el bot."}

    datos = hermes.armar_datos(a["campos"], a["promo"], a["elegida"])
    res = hermes.crear_pedido(datos)
    if res["ok"]:
        hermes.registrar(a["ns"], res)
    return res
