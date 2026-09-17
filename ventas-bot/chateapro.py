"""
Cliente de la API de Chatea Pro para el bot de ventas.

Lee los datos de ventas desde los CAMPOS DE USUARIO de los suscriptores
(no desde el modulo de ordenes, que esta vacio en esta cuenta).

Un cliente cuenta como "venta" cuando su campo [WhatsApp IA] Compra realizada
es "true" (o tiene Fecha de compra con valor).
"""

import os
import re
import time
import datetime as dt

import requests

BASE_URL = os.environ.get("CHATEAPRO_API_URL", "https://chateapro.app/api")
API_TOKEN = os.environ.get("CHATEAPRO_API_TOKEN", "")

# Zona horaria de Ecuador (UTC-5). El servidor (Coolify) suele estar en UTC,
# lo que hacia que "hoy" apuntara al dia equivocado.
TZ_ECUADOR = dt.timezone(dt.timedelta(hours=-5))

# Cache en memoria de los campos de cada suscriptor: evita disparar una
# peticion /subscriber/get-info por cada suscriptor en cada ciclo de revision.
# El TTL es configurable (por defecto 5 min).
_CACHE = {}
CACHE_TTL = int(os.environ.get("CHATEAPRO_CACHE_TTL_SECONDS", "300"))


def hoy_ecuador():
    """Fecha actual en Ecuador, sin importar la zona del servidor."""
    return dt.datetime.now(TZ_ECUADOR).date()

# Campos de usuario que nos interesan para el resumen del pedido.
CAMPO_NOMBRE = "Nombre completo"
CAMPO_FECHA = "Fecha de compra"
CAMPO_VALOR = "Valor de la compra"
CAMPO_PRODUCTOS = "Productos escogidos"
CAMPO_CIUDAD = "Ciudad"
CAMPO_PROVINCIA = "Departamento/ Provincia"
CAMPO_DIRECCION = "Dirección"
CAMPO_ENTREGA = "% de Entrega"
CAMPO_RESUMEN = "Resumen"
CAMPO_COMPRA_OK = "[WhatsApp IA] Compra realizada"


def _headers():
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }


def _get(path, params=None):
    r = requests.get(BASE_URL + path, headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def listar_suscriptores_recientes(limit=100):
    """
    Devuelve suscriptores que interactuaron en las ultimas 24h.
    (La API pagina de a 100 como maximo.)
    """
    data = _get("/subscribers", {
        "is_interacted_in_last_24h": "yes",
        "limit": min(limit, 100),
        "page": 1,
    })
    return data.get("data", [])


def listar_todos_suscriptores(max_paginas=10):
    """
    Devuelve TODOS los suscriptores (paginando de a 100).
    Se usa para consultar ventas de fechas pasadas (mas alla de 24h).
    max_paginas limita el recorrido para no tardar demasiado.
    """
    todos = []
    for page in range(1, max_paginas + 1):
        data = _get("/subscribers", {"limit": 100, "page": page})
        items = data.get("data", [])
        todos.extend(items)
        meta = data.get("meta") or {}
        if page >= (meta.get("last_page") or 1):
            break
    return todos


def obtener_campos(user_ns):
    """Devuelve un dict {nombre_campo: valor} de un suscriptor (con cache)."""
    ahora = time.time()
    cacheado = _CACHE.get(user_ns)
    if cacheado and (ahora - cacheado["ts"]) < CACHE_TTL:
        return cacheado["campos"]

    data = _get("/subscriber/get-info", {"user_ns": user_ns}).get("data", {})
    campos = {}
    for f in data.get("user_fields") or []:
        campos[f.get("name", "")] = str(f.get("value") or "")
    # Incluimos algunos datos base del perfil.
    campos["_name"] = data.get("name") or ""
    campos["_phone"] = data.get("phone") or ""
    campos["_user_ns"] = user_ns

    _CACHE[user_ns] = {"campos": campos, "ts": ahora}
    return campos


def _parse_fecha_compra(valor):
    """
    Convierte 'Fecha de compra' (ej '13/09/2026 10:16 pm') a date, o None.
    """
    if not valor:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", valor)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _parse_fecha_compra_dt(valor):
    """
    Convierte 'Fecha de compra' (ej '13/09/2026 10:16 pm') a datetime completo,
    o None si no se puede. Se usa para ordenar las ventas de la mas antigua a la
    mas nueva.
    """
    fecha = _parse_fecha_compra(valor)
    if not fecha:
        return None
    hm = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", valor, re.IGNORECASE)
    hora = minuto = 0
    if hm:
        hora = int(hm.group(1))
        minuto = int(hm.group(2))
        sufijo = (hm.group(3) or "").lower()
        if sufijo == "pm" and hora < 12:
            hora += 12
        elif sufijo == "am" and hora == 12:
            hora = 0
    try:
        return dt.datetime(fecha.year, fecha.month, fecha.day, hora, minuto)
    except ValueError:
        return None


def es_venta(campos):
    """True si el suscriptor concreto una compra."""
    if str(campos.get(CAMPO_COMPRA_OK, "")).strip().lower() == "true":
        return True
    return bool(str(campos.get(CAMPO_FECHA, "")).strip())


def es_venta_de_fecha(campos, fecha=None):
    """True si el suscriptor concreto una compra que cae en 'fecha' (hoy por defecto)."""
    if fecha is None:
        fecha = hoy_ecuador()
    if not es_venta(campos):
        return False
    return _parse_fecha_compra(campos.get(CAMPO_FECHA, "")) == fecha


def _valor_num(campos):
    v = str(campos.get(CAMPO_VALOR, "0")).replace(",", ".")
    m = re.search(r"[\d.]+", v)
    try:
        return float(m.group()) if m else 0.0
    except ValueError:
        return 0.0


def ventas_del_dia(fecha=None):
    """
    Devuelve la lista de ventas (dicts de campos) cuya Fecha de compra es 'fecha'
    (por defecto hoy). Recorre los suscriptores recientes. Los campos de cada
    suscriptor se leen desde la cache para no golpear la API.
    """
    if fecha is None:
        fecha = hoy_ecuador()

    # Para "hoy" basta con los recientes (rapido). Para fechas pasadas,
    # hay que recorrer todos los suscriptores (mas lento pero completo).
    if fecha == hoy_ecuador():
        suscriptores = listar_suscriptores_recientes()
    else:
        suscriptores = listar_todos_suscriptores()

    ventas = []
    for s in suscriptores:
        campos = obtener_campos(s["user_ns"])
        if es_venta_de_fecha(campos, fecha):
            ventas.append(campos)
    # Ordenar de la venta mas antigua a la mas nueva (por fecha+ hora de compra).
    ventas.sort(key=lambda v: _parse_fecha_compra_dt(v.get(CAMPO_FECHA, "")) or dt.datetime.min)
    return ventas


def resumen_pedido(campos):
    """Arma el texto 'Resumen del pedido - Datos de envio' de un cliente."""
    nombre = campos.get(CAMPO_NOMBRE) or campos.get("_name") or "-"
    return (
        "📝 *Resumen del pedido - Datos de envío*\n\n"
        "Estos son los datos recopilados para el envío del pedido:\n\n"
        f"👤 *Nombre completo:* {nombre}\n"
        f"📲 *Número de teléfono:* {campos.get('_phone', '-')}\n"
        f"📍 *Dirección:* {campos.get(CAMPO_DIRECCION, '-')}\n"
        f"🏘️ *Ciudad:* {campos.get(CAMPO_CIUDAD, '-')}\n"
        f"🗺️ *Departamento o provincia:* {campos.get(CAMPO_PROVINCIA, '-')}\n"
        f"🛍️ *Productos escogidos:* {campos.get(CAMPO_PRODUCTOS, '-')}\n"
        f"💵 *Valor de la compra:* ${campos.get(CAMPO_VALOR, '-')}\n"
        f"📌 *Prob. recibir:* {campos.get(CAMPO_ENTREGA, '-')}\n"
        f"📄 *Resumen:* {campos.get(CAMPO_RESUMEN, '-')}"
    )


def resumen_venta_nueva(campos):
    """Texto corto y detallado para la notificacion de una venta nueva."""
    nombre = campos.get(CAMPO_NOMBRE) or campos.get("_name") or "-"
    return (
        "🔔 *NUEVA VENTA*\n\n"
        f"👤 *{nombre}*\n"
        f"📲 {campos.get('_phone', '-')}\n"
        f"🛍️ {campos.get(CAMPO_PRODUCTOS, '-')}\n"
        f"💵 ${campos.get(CAMPO_VALOR, '-')}\n"
        f"📍 {campos.get(CAMPO_CIUDAD, '-')}\n"
        f"📌 Prob. recibir: {campos.get(CAMPO_ENTREGA, '-')}"
    )


def pendientes_del_dia(fecha=None):
    """
    Ventas del dia cuyo campo 'Resumen' menciona la palabra 'pendiente'.
    Reutiliza ventas_del_dia() (cache y orden), asi que no genera peticiones
    extra a la API.
    """
    if fecha is None:
        fecha = hoy_ecuador()
    return [
        v for v in ventas_del_dia(fecha)
        if "pendiente" in str(v.get(CAMPO_RESUMEN, "")).lower()
    ]


def resumen_del_dia(fecha=None):
    """Arma el texto de estadisticas del dia (ventas, facturacion, top producto)."""
    if fecha is None:
        fecha = hoy_ecuador()
    ventas = ventas_del_dia(fecha)

    total_ventas = len(ventas)
    facturacion = sum(_valor_num(v) for v in ventas)

    # Producto mas vendido (por cantidad estimada del texto "N Producto")
    conteo = {}
    for v in ventas:
        prod = v.get(CAMPO_PRODUCTOS, "").strip()
        if not prod:
            continue
        m = re.match(r"\s*(\d+)\s+(.*)", prod)
        if m:
            cant, nombre = int(m.group(1)), m.group(2).strip()
        else:
            cant, nombre = 1, prod
        conteo[nombre] = conteo.get(nombre, 0) + cant

    if conteo:
        top = max(conteo.items(), key=lambda x: x[1])
        top_txt = f"{top[0]} — {top[1]} ventas"
    else:
        top_txt = "-"

    f_str = fecha.strftime("%d/%m/%Y")
    return (
        "🎉 *Resumen de ventas del día*\n\n"
        f"📅 *Fecha:* {f_str}\n"
        f"🧾 *Ventas del día:* {total_ventas}\n"
        f"💵 *Facturación del día:* ${facturacion:.2f}\n"
        f"🥇 *Producto más vendido:* {top_txt}"
    )
