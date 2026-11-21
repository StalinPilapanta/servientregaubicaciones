"""
Creacion de pedidos en Dropi DIRECTO con el MCP oficial (dropi_mcp.py).

Reemplaza a Hermes: el bot ya tiene sesion OAuth con https://mcp.dropi.co/mcp,
asi que hace el mismo flujo que recomienda el MCP:

  1. resolver_destino(): search_cities + list_departments -> ciudad y
     departamento EXACTOS de Dropi para la ciudad de la agencia.
  2. cotizar(): quote_shipping con contra entrega -> confirma cobertura y
     costo de Servientrega.
  3. (el vendedor confirma en Telegram el resumen)
  4. crear(): create_order con Servientrega fijo y la direccion de la agencia
     en el formato CIUDAD_AGENCIA---SECTOR---DIRECCION.

Anti-duplicados: pedido_existente() busca en Dropi (ultimos dias) un pedido
activo con el mismo telefono antes de mostrar el boton y antes de crear.

Variables de entorno:
  DROPI_CARRIER_NOMBRE   def. SERVIENTREGA
  DROPI_DIAS_DUPLICADO   dias hacia atras para buscar duplicados (def. 3)
"""

import os
import re
import threading
import time
import unicodedata
from datetime import timedelta

import chateapro
import dropi_mcp

CARRIER_NOMBRE = os.environ.get("DROPI_CARRIER_NOMBRE", "SERVIENTREGA").upper()
DIAS_DUPLICADO = int(os.environ.get("DROPI_DIAS_DUPLICADO", "3"))

_ESTADOS_INACTIVOS = {"CANCELADO", "RECHAZADO", "DEVOLUCION", "ANULADO"}

_cache = {}
_lock = threading.Lock()


class DestinoError(Exception):
    """No se pudo resolver la ciudad/departamento en Dropi con certeza."""


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t or "").upper())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", t)).strip()


def _items(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("items", "data", "results", "objects", "carriers"):
            if isinstance(res.get(k), list):
                return res[k]
    return []


def _cacheado(clave, ttl, fn):
    with _lock:
        v = _cache.get(clave)
        if v and time.time() - v[0] < ttl:
            return v[1]
    valor = fn()
    with _lock:
        _cache[clave] = (time.time(), valor)
    return valor


# ------------------------------------------------------------ catalogos --

def departamentos():
    """{id: {"name":..., "code":...}} (cache 24h)."""
    def cargar():
        return {int(d["id"]): {"name": d.get("name", ""),
                               "code": str(d.get("department_code") or "")}
                for d in _items(dropi_mcp.llamar("list_departments"))}
    return _cacheado("departamentos", 86400, cargar)


def carrier_id():
    """id de Servientrega en la cuenta (cache 24h). Def. 2."""
    def cargar():
        for c in _items(dropi_mcp.llamar("list_carriers")):
            if _norm(c.get("name")) == CARRIER_NOMBRE:
                return int(c["id"])
        return None
    try:
        return _cacheado("carrier", 86400, cargar) or 2
    except dropi_mcp.MCPError:
        return 2


# --------------------------------------------------------------- destino --

def resolver_destino(ciudad, provincia):
    """
    Ciudad y departamento exactos de Dropi. Devuelve dict:
      city_id, city, department_id, state, dane_code, zip_code
    Lanza DestinoError si no hay coincidencia unica.
    """
    ciudad_n, prov_n = _norm(ciudad), _norm(provincia)
    if not ciudad_n:
        raise DestinoError("La agencia no tiene ciudad.")

    cands = _cacheado(f"ciudad:{ciudad_n}", 86400,
                      lambda: _items(dropi_mcp.llamar("search_cities", {"name": ciudad})))
    deps = departamentos()

    def dep_de(c):
        return deps.get(int(c.get("department_id") or 0), {})

    exactas = [c for c in cands if _norm(c.get("name")) == ciudad_n]
    if prov_n:
        en_prov = [c for c in exactas if _norm(dep_de(c).get("name")) == prov_n]
        if en_prov:
            exactas = en_prov
    if len(exactas) != 1:
        nombres = ", ".join(f"{c.get('name')} ({dep_de(c).get('name', '?')})"
                            for c in cands[:6]) or "ninguna"
        raise DestinoError(
            f"No pude ubicar «{ciudad}» ({provincia or 'sin provincia'}) en Dropi con "
            f"certeza. Coincidencias: {nombres}.")

    c = exactas[0]
    return {
        "city_id": int(c["id"]),
        "city": c.get("name"),
        "department_id": int(c.get("department_id") or 0),
        "state": dep_de(c).get("name") or provincia,
        # Solo para mostrar en errores (create_order NO acepta el codigo).
        "state_code": dep_de(c).get("code") or "",
        "dane_code": str(c.get("cod_dane") or ""),
        "zip_code": str(c.get("zip_code") or "") or None,
    }


# --------------------------------------------------------------- cotizar --

def cotizar(destino, productos, direccion=""):
    """
    Cotiza con contra entrega. Devuelve dict:
      ok, costo (Servientrega), moneda, error, otras [(carrier, costo, error)]
    """
    ciudad = {"id": destino["city_id"], "zip_code": destino["zip_code"]}
    if destino.get("dane_code"):
        ciudad["dane_code"] = destino["dane_code"]
    args = {
        "cash_on_delivery": True,
        "destination_city": ciudad,
        "products": [{"id": p["id"], "type": "SIMPLE", "quantity": p["quantity"],
                      "price": p["price"]} for p in productos],
    }
    if direccion:
        args["direction"] = direccion
    res = dropi_mcp.llamar("quote_shipping", args)
    moneda = res.get("currency", "USD") if isinstance(res, dict) else "USD"
    otras, serv = [], None
    for o in _items(res):
        nombre = _norm(o.get("carrier") or o.get("name"))
        otras.append((nombre, o.get("shipping_cost"), o.get("error")))
        if nombre == CARRIER_NOMBRE:
            serv = o
    if serv is None:
        return {"ok": False, "costo": None, "moneda": moneda, "otras": otras,
                "error": f"{CARRIER_NOMBRE} no aparece en la cotización."}
    if serv.get("shipping_cost") in (None, ""):
        return {"ok": False, "costo": None, "moneda": moneda, "otras": otras,
                "error": f"{CARRIER_NOMBRE} no cotiza: {serv.get('error') or 'sin motivo'}"}
    return {"ok": True, "costo": float(serv["shipping_cost"]), "moneda": moneda,
            "otras": otras, "error": ""}


# ------------------------------------------------------------ duplicados --

def _tel9(t):
    return re.sub(r"\D", "", str(t or ""))[-9:]


def pedido_existente(telefono, dias=None):
    """
    Pedido ACTIVO en Dropi con ese telefono en los ultimos dias, o None.
    Devuelve {"order_id", "status", "total", "created_at", "city"}.
    """
    tel = _tel9(telefono)
    if len(tel) < 9:
        return None
    dias = DIAS_DUPLICADO if dias is None else dias

    def cargar():
        hoy = chateapro.hoy_ecuador()
        desde = (hoy - timedelta(days=dias)).strftime("%Y-%m-%d")
        hasta = (hoy + timedelta(days=1)).strftime("%Y-%m-%d")
        todos = []
        for pagina in range(5):
            lote = _items(dropi_mcp.llamar("list_orders", {
                "from": desde, "until": hasta,
                "result_number": 100, "start": pagina * 100}))
            todos += lote
            if len(lote) < 100:
                break
        return todos

    # Cache corta: varias fichas seguidas reusan la misma lista.
    pedidos = _cacheado(f"pedidos:{dias}", 90, cargar)
    for p in pedidos:
        if _tel9(p.get("customer_phone")) == tel and \
                _norm(p.get("status")) not in _ESTADOS_INACTIVOS:
            return {"order_id": str(p.get("order_id")), "status": p.get("status"),
                    "total": p.get("total"), "created_at": p.get("created_at"),
                    "city": p.get("city")}
    return None


def olvidar_pedidos():
    with _lock:
        for k in [k for k in _cache if k.startswith("pedidos:")]:
            _cache.pop(k, None)


# ----------------------------------------------------------------- crear --

def crear(datos, destino):
    """
    create_order con Servientrega. datos = hermes.armar_datos(...).
    Devuelve {"ok", "order_id", "guia", "total", "mensaje"}. No reintenta.
    Lanza dropi_mcp.AuthRequired si hay que reconectar Dropi.
    """
    args = {
        "name": datos["name"],
        "surname": datos["surname"] or ".",
        "phone": datos["phone"],
        "dir": datos["dir"],
        "state": destino["state"],  # nombre: Dropi rechaza el codigo (1024) en create_order
        "city": destino["city"],
        "rate_type": "CON RECAUDO",
        "products": datos["products"],
        "distribution_company_id": carrier_id(),
        "notes": datos.get("notes", ""),
    }
    if datos.get("shop_order_id"):
        args["shop_order_id"] = str(datos["shop_order_id"])
    try:
        res = dropi_mcp.llamar("create_order", args)
    except dropi_mcp.AuthRequired:
        raise
    except Exception as e:
        # Puede haberse creado igual: verificar antes de dar por fallido.
        olvidar_pedidos()
        try:
            ya = pedido_existente(datos["phone"], dias=1)
        except Exception:
            ya = None
        if ya:
            return {"ok": True, "order_id": ya["order_id"], "guia": None,
                    "total": ya.get("total"),
                    "mensaje": f"Hubo un error pero el pedido sí quedó creado ({e})."}
        return {"ok": False, "order_id": None, "guia": None, "total": None,
                "mensaje": explicar_error(str(e), datos, destino)}

    olvidar_pedidos()
    oid = None
    if isinstance(res, dict):
        oid = res.get("order_id") or res.get("id") or (res.get("order") or {}).get("id")
    if not oid:
        m = re.search(r"\b(\d{6,9})\b", str(res))
        oid = m.group(1) if m else None
    if not oid:
        return {"ok": False, "order_id": None, "guia": None, "total": None,
                "mensaje": f"Respuesta de Dropi sin número de pedido: {str(res)[:300]}"}
    return {"ok": True, "order_id": str(oid), "guia": None,
            "total": res.get("total") if isinstance(res, dict) else None,
            "mensaje": str(res.get("status", "")) if isinstance(res, dict) else ""}


def arreglar_texto(s):
    """'estÃ¡' -> 'está', 'CAÃ\\x83Â\\x91AR' -> 'CAÑAR' (UTF-8 leido como latin-1, 1 o 2 veces)."""
    def uno(m):  # cada tramo roto por separado: puede venir roto 1 o 2 veces
        t = m.group()
        for _ in range(3):
            try:
                nuevo = t.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                break
            if nuevo == t:
                break
            t = nuevo
        return t
    return re.sub(r"[À-ÿ][\u0080-ÿ]+", uno, str(s or ""))


# (palabras en el error de Dropi, que fallo, que hacer)
_TIPOS_ERROR = [
    (("departamento", "department", "state"), "DEPARTAMENTO",
     "Revisa en Dropi que ese departamento exista y esté habilitado; si no, crea el pedido a mano."),
    (("ciudad", "city", "municipio", "destino", "cobertura", "coverage"), "CIUDAD / COBERTURA",
     "Revisa la ciudad de la agencia o que Servientrega cubra ese destino."),
    (("stock", "inventario", "agotado", "producto", "product", "variation", "variacion"), "PRODUCTO",
     "Revisa stock y que los productos sigan activos en Dropi; pulsa Reanalizar."),
    (("telefono", "phone", "celular"), "TELÉFONO",
     "Corrige el teléfono del cliente en el CRM y pulsa Reanalizar."),
    (("direccion", "address", "dir "), "DIRECCIÓN",
     "Revisa la dirección / agencia del pedido."),
    (("saldo", "wallet", "balance", "cartera"), "SALDO DE DROPI",
     "Recarga saldo en Dropi y vuelve a intentar."),
    (("transportadora", "carrier", "distribution"), "TRANSPORTADORA",
     "Revisa que Servientrega esté disponible para esa ruta en Dropi."),
]


def explicar_error(texto, datos=None, destino=None):
    """Mensaje claro de por que Dropi no creo el pedido (que fallo + que hacer)."""
    detalle = arreglar_texto(texto)
    detalle = re.sub(r"^(MCP error:\s*)?(core rejected the request:\s*)?", "", detalle,
                     flags=re.I).strip()
    bajo = _norm(detalle).lower() + " "
    destino, datos = destino or {}, datos or {}
    for palabras, que, accion in _TIPOS_ERROR:
        if any(p in bajo for p in palabras):
            break
    else:
        que, accion = "OTRO", "Revisa el detalle en Dropi."
    dato = {
        "DEPARTAMENTO": f"«{destino.get('state', '?')}» (código {destino.get('state_code') or '-'})",
        "CIUDAD / COBERTURA": f"«{destino.get('city', '?')}» — {destino.get('state', '?')}",
        "PRODUCTO": ", ".join(f"#{p['id']} ×{p['quantity']}" for p in datos.get("products") or []),
        "TELÉFONO": datos.get("phone", ""),
        "DIRECCIÓN": datos.get("dir", ""),
    }.get(que, "")
    L = [f"📍 Falló: {que}" + (f" {dato}" if dato else ""),
         f"💬 Dropi dice: {detalle[:300]}",
         f"👉 {accion}"]
    return "\n".join(L)


# ------------------------------------------------------- consultas (bot) --

def listar_pedidos(estado=None, n=15, dias=30):
    """Pedidos recientes de Dropi (MCP), mas nuevos primero. Max 100."""
    hoy = chateapro.hoy_ecuador()
    args = {"from": (hoy - timedelta(days=dias)).strftime("%Y-%m-%d"),
            "until": (hoy + timedelta(days=1)).strftime("%Y-%m-%d"),
            "result_number": max(1, min(int(n), 100)), "start": 0}
    if estado:
        args["status"] = estado
    return _items(dropi_mcp.llamar("list_orders", args))


def buscar_guia(numero, dias=89):
    """Pedido (detalle de get_order) cuyo tracking_code es esa guia, o None."""
    numero = re.sub(r"\s", "", str(numero))
    hoy = chateapro.hoy_ecuador()
    lista = _items(dropi_mcp.llamar("list_orders", {
        "from": (hoy - timedelta(days=dias)).strftime("%Y-%m-%d"),
        "until": (hoy + timedelta(days=1)).strftime("%Y-%m-%d"),
        "text_to_search": numero, "result_number": 20}))
    for p in lista:
        if str(p.get("tracking_code") or "").strip() == numero or \
                str(p.get("order_id")) == numero:
            det = dropi_mcp.llamar("get_order", {"id": str(p["order_id"])})
            det.setdefault("tracking_code", p.get("tracking_code"))
            det.setdefault("shipping_company", p.get("shipping_company"))
            return det
    return None


# ----------------------------------------------------------------- stock --

# Proveedor del que sale casi todo: de aqui se ofrecen los regalos de reemplazo.
PROVEEDOR_PRINCIPAL = int(os.environ.get("DROPI_PROVEEDOR_PRINCIPAL", "15819"))
# Stock minimo para ofrecer un producto como regalo de reemplazo.
STOCK_MIN_REGALO = int(os.environ.get("STOCK_MIN_REGALO", "5"))
# Costo minimo: deja fuera "ENVIO PRIORITARIO", "GARANTIA", "PRODUCTO GRATIS" ($0-0.10).
COSTO_MIN_REGALO = float(os.environ.get("COSTO_MIN_REGALO", "1"))


def _info(p):
    return {"id": int(p["id"]), "nombre": p.get("name") or f"#{p['id']}",
            "stock": int(p.get("stock") or 0), "proveedor": p.get("supplier_id"),
            "costo": float(p.get("sale_price") or 0)}


def info_producto(producto_id, busqueda):
    """
    {id, nombre, stock, proveedor, costo} del producto en Dropi, o None si no
    aparece. busqueda: texto, o (texto, supplier_id) cuando hay muchos
    productos con ese nombre ("CANDELA") y el buscado no sale en la 1a pagina.
    """
    texto, proveedor = busqueda if isinstance(busqueda, tuple) else (busqueda, None)

    def cargar():
        params = {"search": texto, "get_stock": True, "page_size": 50}
        if proveedor:
            params["supplier_id"] = proveedor
        for p in _items(dropi_mcp.llamar("list_products", params)):
            if int(p.get("id") or 0) == int(producto_id):
                return _info(p)
        return None
    return _cacheado(f"prod:{producto_id}", 60, cargar)


def stock(producto_id, busqueda):
    """Stock del producto en Dropi (int) o None si no se pudo encontrar."""
    i = info_producto(producto_id, busqueda)
    return i["stock"] if i else None


def _busqueda_de(p, busquedas):
    """Texto de busqueda del producto: la tabla, o su nombre sin '(regalo)'."""
    b = busquedas.get(p["id"])
    if b:
        return b
    nombre = re.sub(r"\s*\(regalo\)\s*$", "", str(p.get("nombre") or "")).strip()
    return nombre if nombre and not nombre.startswith("#") else None


def revisar_productos(productos, busquedas):
    """
    productos: [{id, quantity, nombre?}]. Devuelve dict:
      faltantes   -> textos que BLOQUEAN (sin stock, o varios proveedores)
      lineas      -> "#148700 MEGA RED PRIV ×2 · stock 1377 ✓ · prov 15819"
      proveedores -> proveedores encontrados
      info        -> {id: info_producto}
    """
    faltantes, lineas, info = [], [], {}
    for p in productos:
        busq = _busqueda_de(p, busquedas)
        i = info_producto(p["id"], busq) if busq else None
        nombre = (i or {}).get("nombre") or p.get("nombre") or f"#{p['id']}"
        regalo = " 🎁" if str(p.get("nombre") or "").endswith("(regalo)") else ""
        base = f"#{p['id']} {nombre}{regalo} ×{p['quantity']}"
        if i is None:
            lineas.append(f"{base} · no pude verificar ⚠️")
            continue
        info[p["id"]] = i
        prov = f"prov {i['proveedor']}"
        if i["stock"] < p["quantity"]:
            faltantes.append(f"{nombre} (#{p['id']}): hay {i['stock']}, se necesitan {p['quantity']}")
            lineas.append(f"{base} · stock {i['stock']} ❌ · {prov}")
        else:
            lineas.append(f"{base} · stock {i['stock']} ✓ · {prov}")
    proveedores = sorted({i["proveedor"] for i in info.values() if i["proveedor"]})
    if len(proveedores) > 1:
        faltantes.append(f"El pedido mezcla {len(proveedores)} proveedores "
                         f"({', '.join(map(str, proveedores))}): debe ir con uno solo.")
    return {"faltantes": faltantes, "lineas": lineas, "proveedores": proveedores, "info": info}


def revisar_stock(productos, busquedas):
    """Compatibilidad: (faltantes, lineas) de revisar_productos."""
    r = revisar_productos(productos, busquedas)
    return r["faltantes"], r["lineas"]


def regalos_baratos(proveedor=None, excluir=(), n=3, cantidad=1):
    """
    Los n productos mas economicos del proveedor con stock (para elegir otro
    regalo cuando no hay el principal). Lista de info_producto.
    """
    proveedor = proveedor or PROVEEDOR_PRINCIPAL

    def cargar():
        res = dropi_mcp.llamar("list_products", {
            "supplier_id": proveedor, "get_stock": True, "page_size": 60,
            "order_by": "sale_price", "order_type": "asc"})
        return [_info(p) for p in _items(res)]
    todos = _cacheado(f"baratos:{proveedor}", 600, cargar)
    excluir = {int(x) for x in excluir}
    ok = [i for i in sorted(todos, key=lambda i: i["costo"])
          if i["costo"] >= COSTO_MIN_REGALO and i["id"] not in excluir
          and i["stock"] >= max(cantidad, STOCK_MIN_REGALO)]
    return ok[:n]


# ---------------------------------------------------------- riesgo cliente --

RIESGO_DEVOLUCION_MIN = int(os.environ.get("RIESGO_DEVOLUCION_MIN", "30"))


def perfil_cliente(telefono):
    """
    Huella del comprador en Dropi (get_customer_fingerprint_profile):
      {tipo, riesgo, entrega, devolucion, sin_datos, alerta} o None si no hay huella.
    Las etiquetas se muestran tal cual; la alerta se decide solo por los numeros.
    entrega + devolucion no suman 100 (lo que falta son pedidos en curso).
    """
    tel = _tel9(telefono)
    if len(tel) < 7:
        return None

    def cargar():
        try:
            res = dropi_mcp.llamar("get_customer_fingerprint_profile", {"phone": tel})
        except dropi_mcp.AuthRequired:
            raise
        except Exception as e:
            if "no se encontr" in str(e).lower():
                return None
            raise
        if not isinstance(res, dict) or "risk_label" not in res:
            return None  # "No se encontró huella digital para ese teléfono"
        entrega = float(res.get("delivery_probability") or 0)
        devol = float(res.get("return_probability") or 0)
        sin_datos = entrega == 0 and devol == 0
        return {"tipo": res.get("buyer_type") or "", "riesgo": res.get("risk_label") or "",
                "entrega": entrega, "devolucion": devol, "sin_datos": sin_datos,
                "alerta": (not sin_datos) and devol >= RIESGO_DEVOLUCION_MIN}
    return _cacheado(f"perfil:{tel}", 3600, cargar)


def texto_perfil(p):
    if p is None:
        return "🛡️ Dropi: sin huella para este teléfono (no se encontró)."
    if p["sin_datos"]:
        return f"🛡️ Dropi: {p['tipo']} · {p['riesgo']} · aún sin pedidos finalizados."
    icono = "⚠️" if p["alerta"] else "🛡️"
    return (f"{icono} Dropi: {p['tipo']} · {p['riesgo']} · entrega {p['entrega']:.0f}% · "
            f"devolución {p['devolucion']:.0f}%")
