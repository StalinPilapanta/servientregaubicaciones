"""
Cliente de la API de Chatea Pro para el bot de ventas.

Lee los datos de ventas desde los CAMPOS DE USUARIO de los suscriptores
(no desde el modulo de ordenes, que esta vacio en esta cuenta).

Un cliente cuenta como "venta" cuando su campo [WhatsApp IA] Compra realizada
es "true" (o tiene Fecha de compra con valor).
"""

import os
import re
import json
import time
import datetime as dt

import requests

BASE_URL = os.environ.get("CHATEAPRO_API_URL", "https://chateapro.app/api")
API_TOKEN = os.environ.get("CHATEAPRO_API_TOKEN", "")

# Zona horaria de Ecuador (UTC-5). El servidor (Coolify) suele estar en UTC,
# lo que hacia que "hoy" apuntara al dia equivocado.
TZ_ECUADOR = dt.timezone(dt.timedelta(hours=-5))

# Demora (segundos) entre peticiones en los barridos masivos de suscriptores
# (/seguimiento y reporte diario) para no saturar el API de Chatea Pro.
SCAN_DELAY_SEC = float(os.environ.get("SCAN_DELAY_SECONDS", "2.0"))

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


def _throttle():
    """Pequeña pausa entre peticiones en barridos masivos (evita saturar)."""
    if SCAN_DELAY_SEC > 0:
        time.sleep(SCAN_DELAY_SEC)


def _get(path, params=None, reintentos=3):
    """GET con reintento automatico ante rate limit (429) con espera creciente."""
    espera = 5
    for intento in range(reintentos + 1):
        r = requests.get(BASE_URL + path, headers=_headers(), params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and intento < reintentos:
            retry_after = r.headers.get("Retry-After")
            try:
                sleep_s = max(float(retry_after), espera)
            except (TypeError, ValueError):
                sleep_s = espera
            print(f"Rate limit (429) en {path}; esperando {sleep_s}s...")
            time.sleep(sleep_s)
            espera *= 2
            continue
        r.raise_for_status()
    r.close()
    raise requests.HTTPError(f"{path}: excedio el numero de reintentos")


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
        _throttle()
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


# Frases que indican que el pedido quedo disponible para retiro en una oficina
# de Servientrega (el estado viene de Dropi).
_FRASES_RETIRO = (
    "para retiro en agencia servientrega",
    "para retiro en agencia",
    "para retiro en oficina",
    "disponible para retiro",
    "retiro en la oficina",
)

# Prefijo del campo de Chatea Pro donde Dropi guarda la orden (ej:
# "[Dropi] Datos de la orden f300687v1701544"). El valor es un JSON con los
# datos de la orden (guia real en 'shipping_guide').
PREFIJO_DROPI = "[Dropi] Datos de la orden"

# Archivo donde el bot guarda la fecha en que vio por primera vez cada guia en
# estado "para retiro". Es la referencia para calcular dias sin retirar, porque
# Dropi no expone la fecha exacta de ingreso a la oficina.
RUTA_RETIROS = os.environ.get(
    "RETIROS_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "retiros.json"),
)

# Historico de guias en retiro: guarda la fila completa de cada guia detectada.
# Asi, aunque el suscriptor desaparezca de Chatea Pro, el reporte no pierde el
# pedido ni los dias sin retirar.
RUTA_HISTORICO = os.environ.get(
    "RETIROS_HISTORICO_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "retiros_historico.json"),
)

# Caché en memoria de la base de guias->fecha de retiro.
_RETIROS = None
_HISTORICO = None


def _leer_retiros():
    try:
        with open(RUTA_RETIROS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar_retiros():
    if _RETIROS is None:
        return
    try:
        with open(RUTA_RETIROS, "w", encoding="utf-8") as f:
            json.dump(_RETIROS, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("No se pudo guardar retiros.json:", e)


def _retiros():
    global _RETIROS
    if _RETIROS is None:
        _RETIROS = _leer_retiros()
    return _RETIROS


def _leer_historico():
    try:
        with open(RUTA_HISTORICO, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _historico():
    global _HISTORICO
    if _HISTORICO is None:
        _HISTORICO = _leer_historico()
    return _HISTORICO


def _guardar_historico():
    if _HISTORICO is None:
        return
    try:
        with open(RUTA_HISTORICO, "w", encoding="utf-8") as f:
            json.dump(_HISTORICO, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("No se pudo guardar retiros_historico.json:", e)


def _parse_dropi(valor):
    """Convierte el valor del campo Dropi (JSON) a dict, o None si no es JSON."""
    try:
        d = json.loads(valor)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _status_dropi(valor):
    """Devuelve el estatus dentro del JSON de Dropi (en minusculas), o ''."""
    d = _parse_dropi(valor)
    if not d:
        return ""
    return str(d.get("status") or "").strip().lower()


def es_estado_retiro(valor):
    """True si el valor del campo Dropi indica retiro en oficina Servientrega."""
    status = _status_dropi(valor)
    if status:
        return status in _FRASES_RETIRO
    return any(frase in str(valor or "").lower() for frase in _FRASES_RETIRO)


def _parse_fecha_guardada(s):
    """Convierte 'dd/mm/yyyy' (fechas guardadas en retiros.json) a date, o None."""
    try:
        return dt.datetime.strptime(str(s), "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


def _guia_campo(nombre_campo, valor):
    """Guia real de la orden: 'shipping_guide' del JSON, o sufijo del nombre."""
    d = _parse_dropi(valor)
    if d and d.get("shipping_guide"):
        return str(d["shipping_guide"]).strip()
    return str(nombre_campo)[len(PREFIJO_DROPI):].strip() or str(nombre_campo)


def registrar_retiros(campos):
    """Anota HOY como fecha de 'primera vista' de cada guia recien detectada
    en estado de retiro (si aun no estaba). Persiste en retiros.json."""
    datos = _retiros()
    cambio = False
    hoy_s = hoy_ecuador().strftime("%d/%m/%Y")
    for nombre, valor in campos_dropi(campos):
        if not es_estado_retiro(valor):
            continue
        guia = _guia_campo(nombre, valor)
        if guia and guia not in datos:
            datos[guia] = hoy_s
            cambio = True
    if cambio:
        _guardar_retiros()


def campos_dropi(campos):
    """Itera (nombre, valor) de los campos '[Dropi] Datos de la orden ...'."""
    for k, v in campos.items():
        nombre = str(k).strip()
        if nombre.lower().startswith(PREFIJO_DROPI.lower()):
            yield nombre, str(v or "")


def campo_retiro(campos):
    """Devuelve (nombre_campo, valor) del primer campo Dropi en retiro, o None."""
    for nombre, valor in campos_dropi(campos):
        if es_estado_retiro(valor):
            return nombre, valor
    return None


def es_para_retiro(campos):
    """True si algun campo Dropi del suscriptor indica retiro en oficina."""
    return campo_retiro(campos) is not None


def listar_para_retiro():
    """
    Suscriptores recientes con estado de retiro en una oficina Servientrega.
    Solo 1 request de lista + get-info (con cache): no satura la API.
    """
    encontrados = []
    for s in listar_suscriptores_recientes():
        campos = obtener_campos(s["user_ns"])
        if es_para_retiro(campos):
            encontrados.append(campos)
    return encontrados


def _parse_iso_fecha(valor):
    """Convierte una fecha ISO (ej '2026-09-16T20:00:38Z') a date en Ecuador."""
    s = str(valor or "").strip()
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t.astimezone(TZ_ECUADOR).date()
    except ValueError:
        return None


def _fecha_en_texto(valor):
    """Extrae la primera fecha dd/mm/aaaa de un texto, o None."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(valor or ""))
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _productos_dropi(d):
    """Arma el texto de productos desde 'orderdetails' (ej '2x BIOKIDS MORINGA')."""
    partes = []
    for od in d.get("orderdetails") or []:
        nombre = (od.get("product") or {}).get("name", "")
        if not nombre:
            continue
        try:
            cant = float(od.get("quantity") or 0)
            partes.append(f"{cant:g}x {nombre}")
        except (TypeError, ValueError):
            partes.append(nombre)
    return ", ".join(partes)


def _fila_retiro(nombre_campo, valor, campos):
    """Arma un dict/fila a partir del campo Dropi (JSON) + campos del suscriptor."""
    d = _parse_dropi(valor)
    guia = _guia_campo(nombre_campo, valor)
    estado = valor
    fecha = None

    if d:
        estado = str(d.get("status") or estado)
        fecha = _parse_iso_fecha(d.get("created_at"))
        nombre = ((str(d.get("name") or "") + " " + str(d.get("surname") or "")).strip()
                  or campos.get(CAMPO_NOMBRE) or campos.get("_name", ""))
        telefono = str(d.get("phone") or campos.get("_phone", ""))
        ciudad = str(d.get("city") or campos.get(CAMPO_CIUDAD, ""))
        provincia = str(d.get("state") or campos.get(CAMPO_PROVINCIA, ""))
        direccion = str(d.get("dir") or campos.get(CAMPO_DIRECCION, ""))
        productos = _productos_dropi(d) or campos.get(CAMPO_PRODUCTOS, "")
        valor_fila = str(d.get("total_order") or campos.get(CAMPO_VALOR, ""))
        bodega = str((d.get("warehouse") or {}).get("name", ""))
        tasa = str(d.get("rate_type") or "")
    else:
        nombre = campos.get(CAMPO_NOMBRE) or campos.get("_name", "")
        telefono = campos.get("_phone", "")
        ciudad = campos.get(CAMPO_CIUDAD, "")
        provincia = campos.get(CAMPO_PROVINCIA, "")
        direccion = campos.get(CAMPO_DIRECCION, "")
        productos = campos.get(CAMPO_PRODUCTOS, "")
        valor_fila = campos.get(CAMPO_VALOR, "")
        bodega = ""
        tasa = ""
        fecha = _fecha_en_texto(valor) or _parse_fecha_compra(campos.get(CAMPO_FECHA, ""))

    # Si el bot ya detecto esta guia en retiro, usar esa fecha (mas cercana a
    # cuando quedo disponible). Si no, se queda con created_at como respaldo.
    fecha_detectada = _parse_fecha_guardada(_retiros().get(guia, ""))
    if fecha_detectada:
        fecha = fecha_detectada

    dias = (hoy_ecuador() - fecha).days if fecha else None

    # JSON completo de la orden tal como lo guarda Dropi (informacion cruda).
    if d:
        datos_dropi = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    else:
        datos_dropi = valor

    return {
        "guia": guia,
        "nombre": nombre,
        "telefono": telefono,
        "ciudad": ciudad,
        "provincia": provincia,
        "direccion": direccion,
        "productos": productos,
        "valor": valor_fila,
        "bodega": bodega,
        "tasa_envio": tasa,
        "estado": estado,
        "fecha_en_retiro": fecha.strftime("%d/%m/%Y") if fecha else "",
        "dias_sin_retirar": dias if dias is not None else "",
        "datos_dropi": datos_dropi,
    }


def _actualizar_historico(guia, fila):
    """Guarda/actualiza la fila de una guia en el historico local."""
    hist = _historico()
    actual = hist.get(guia)
    if actual:
        # Conserva la fecha original en que se detecto el retiro y si ya se retiro.
        fila = {**fila,
                "fecha_en_retiro": actual.get("fecha_en_retiro") or fila["fecha_en_retiro"],
                "retirado_el": actual.get("retirado_el", "")}
    else:
        # Primera deteccion: la fecha en retiro queda fija (el dia detectado).
        detectado = fila["fecha_en_retiro"] or hoy_ecuador().strftime("%d/%m/%Y")
        fila = {**fila, "fecha_en_retiro": detectado, "retirado_el": ""}
    hist[guia] = fila


def _marcar_retirados(barrido_completo, vistas_ahora):
    """En barrridos completos, marca como retiradas las guias que ya no aparecen."""
    if not barrido_completo:
        return
    hoy_s = hoy_ecuador().strftime("%d/%m/%Y")
    cambio = False
    for guia, rec in _historico().items():
        if rec.get("retirado_el") == "" and guia not in vistas_ahora:
            rec["retirado_el"] = hoy_s
            cambio = True
    if cambio:
        _guardar_historico()


def _recomputar_dias(rec):
    """Recalcula dias sin retirar desde la fecha en retiro (seguimiento diario)."""
    fecha = _parse_fecha_guardada(rec.get("fecha_en_retiro", ""))
    rec["dias_sin_retirar"] = (hoy_ecuador() - fecha).days if fecha else ""
    return rec


def seguimiento_retiro(fecha=None):
    """
    Reporte de seguimiento: una fila por cada guia con estatus 'PARA RETIRO EN
    AGENCIA SERVIENTREGA', combinando lo detectado ahora en Chatea Pro con el
    historico local (para no perder pedidos si el suscriptor desaparece).

    Igual que ventas_del_dia():
      - fecha None u hoy  -> suscriptores recientes (ultimas 24h, rapido).
      - fecha pasada/'todos' -> recorre TODOS los suscriptores (completo, lento)
        y marca como retiradas las guias que ya no estan en retiro.
    """
    if fecha is None:
        fecha = hoy_ecuador()
    barrido_completo = fecha != hoy_ecuador()
    if barrido_completo:
        suscriptores = listar_todos_suscriptores()
    else:
        suscriptores = listar_suscriptores_recientes()

    vistas_ahora = set()
    for s in suscriptores:
        campos = obtener_campos(s["user_ns"])
        registrar_retiros(campos)
        for nombre_campo, valor in campos_dropi(campos):
            if not es_estado_retiro(valor):
                continue
            guia = _guia_campo(nombre_campo, valor)
            _actualizar_historico(guia, _fila_retiro(nombre_campo, valor, campos))
            vistas_ahora.add(guia)
        _throttle()

    _marcar_retirados(barrido_completo, vistas_ahora)

    # Reporte = guias activas (aun no retiradas) desde el historico.
    filas = [ _recomputar_dias(rec)
              for rec in _historico().values()
              if rec.get("retirado_el") == "" ]
    # Ordena por mas dias sin retirar primero (los sin fecha al final).
    filas.sort(key=lambda r: (r["dias_sin_retirar"] == "", -(r["dias_sin_retirar"] or 0)))
    return filas


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
