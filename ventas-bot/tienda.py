"""
Pedidos de la tienda web (proyecto tienda/) -> ficha en Telegram -> Dropi.

Flujo (con aprobacion del vendedor):
  1. revisar(): pide a la tienda los pedidos en estado "nuevo", los marca
     "tomado" (para no repetirlos) y devuelve la ficha de cada uno.
     Al arrancar tambien recupera los "tomado" que quedaron sin crear.
  2. Boton «Crear en Dropi» -> preparar(): ciudad exacta en Dropi, armado
     con stock (mismas alternativas que el bot: promos.opciones) y cotizacion
     de Servientrega contra entrega -> pantalla de confirmacion.
  3. Boton «Sí, crear» -> crear(): ultimo chequeo de duplicados por
     telefono, create_order y aviso a la tienda (estado "creado" + #Dropi).
  4. Boton «Cancelar pedido» -> la tienda lo marca "cancelado".

Botones (callback_data): tc:<id> crear · tk:<id> confirmar · tv:<id> volver ·
tx:<id> cancelar pedido · ty:<id> confirmar cancelacion.

Variables de entorno (sin ellas el modulo queda apagado):
  TIENDA_URL       p. ej. https://tienda.midominio.com
  TIENDA_API_KEY   la misma clave configurada en la tienda
"""

import os
import re
import threading
from urllib.parse import quote

import requests

import dropi_pedido
import ficha
import promos

TIENDA_URL = os.environ.get("TIENDA_URL", "").rstrip("/")
TIENDA_API_KEY = os.environ.get("TIENDA_API_KEY", "")

_PEDIDOS = {}            # id (str) -> pedido de la tienda (+ "preparado", "existente")
_lock = threading.Lock()
_ocupados = set()
_recuperados = False


class TiendaError(Exception):
    pass


def activo():
    return bool(TIENDA_URL and TIENDA_API_KEY)


# ------------------------------------------------------------------- API --

def _api(metodo, ruta, **kw):
    try:
        r = requests.request(metodo, f"{TIENDA_URL}{ruta}", timeout=20,
                             headers={"X-Tienda-Key": TIENDA_API_KEY}, **kw)
    except requests.RequestException as e:
        raise TiendaError(f"No pude conectar con la tienda: {e}") from e
    if not r.ok:
        raise TiendaError(f"La tienda respondió {r.status_code}: {r.text[:200]}")
    return r.json()


def _estado(pid, estado, dropi_id="", nota=""):
    return _api("POST", f"/api/interno/pedidos/{pid}",
                json={"estado": estado, "dropi_id": str(dropi_id or ""), "nota": nota[:300]})


def revisar():
    """Pedidos nuevos (y, la primera vez, los que quedaron 'tomado'). Devuelve lista."""
    global _recuperados
    if not activo():
        return []
    lista = _api("GET", "/api/interno/pedidos", params={"estado": "nuevo"})
    if not _recuperados:
        lista = _api("GET", "/api/interno/pedidos", params={"estado": "tomado"}) + lista
        _recuperados = True
    nuevos = []
    for p in lista:
        pid = str(p["id"])
        if p["estado"] == "nuevo":
            _estado(pid, "tomado")
        p["estado"] = "tomado"
        try:
            p["existente"] = dropi_pedido.pedido_existente(p["celular"])
        except Exception as e:      # sin Dropi conectado igual se muestra la ficha
            p["existente"] = None
            print("Tienda: no pude revisar duplicados:", e)
        with _lock:
            _PEDIDOS[pid] = p
        nuevos.append(p)
    return nuevos


def pedido(pid):
    with _lock:
        return _PEDIDOS.get(str(pid))


def ocupar(pid):
    with _lock:
        if pid in _ocupados:
            return False
        _ocupados.add(pid)
        return True


def liberar(pid):
    with _lock:
        _ocupados.discard(pid)


# ----------------------------------------------------------------- ficha --

def _tel9(t):
    return re.sub(r"\D", "", str(t or ""))[-9:]


def _envio(p):
    if p["entrega"] == "agencia":
        a = p.get("agencia") or {}
        return [f"🏢 Retiro en agencia: {a.get('id', '-')}",
                f"   {a.get('direccion', '')}"]
    return [f"🏠 Domicilio: {p.get('direccion', '')}",
            f"   Ref: {p.get('referencia') or '-'}"]


def texto_ficha(p, extra=""):
    L = [f"🛒 PEDIDO WEB {p['codigo']}", "",
         f"🎁 {p['frascos']} frasco{'s' if p['frascos'] > 1 else ''} de {p['producto_nombre']}"
         + (" + regalo sorpresa" if p.get("regalo") else ""),
         f"💵 ${p['total']:.2f} contra entrega  ·  promo {p['promo']}",
         f"👤 {p['nombre']} {p['apellido']}",
         f"📲 {p['celular']}",
         f"📍 {p['ciudad']} ({p['provincia']})"] + _envio(p)
    if p.get("notas"):
        L.append(f"📝 {p['notas']}")
    if promos.valida(p["promo"]) and promos.aviso_valor(p["promo"], p["total"]):
        L.append(f"⚠️ La tienda mostró ${p['total']:.2f}; en Dropi se sube "
                 f"${promos.total_promo(p['promo']):.2f}.")
    ex = p.get("existente")
    if ex:
        L.append(f"⚠️ Ya tiene un pedido activo en Dropi: #{ex['order_id']} ({ex.get('status')})")
    if p["estado"] == "creado":
        L.append(f"\n✅ Creado en Dropi #{p.get('dropi_id')}")
    elif p["estado"] == "cancelado":
        L.append("\n✖️ Pedido cancelado")
    if extra:
        L += ["", extra]
    return "\n".join(L)


def _boton_wa(p):
    tel = "593" + _tel9(p["celular"])
    txt = (f"Hola {p['nombre']}, te escribimos por tu pedido {p['codigo']} de "
           f"{p['producto_nombre']} (${p['total']:.2f}).")
    return {"text": "💬 WhatsApp", "url": f"https://wa.me/{tel}?text={quote(txt)}"}


def teclado(p):
    pid = str(p["id"])
    if p["estado"] in ("creado", "cancelado"):
        return [[_boton_wa(p)]]
    return [[{"text": "✅ Crear en Dropi", "callback_data": f"tc:{pid}"}],
            [_boton_wa(p), {"text": "✖️ Cancelar pedido", "callback_data": f"tx:{pid}"}]]


def teclado_cancelar(p):
    pid = str(p["id"])
    return [[{"text": "Sí, cancelar pedido", "callback_data": f"ty:{pid}"}],
            [{"text": "↩️ Volver", "callback_data": f"tv:{pid}"}]]


# ------------------------------------------------------------ preparar --

def _datos_dropi(p, clave):
    if p["entrega"] == "agencia":
        nota = f"RETIRO EN AGENCIA: {(p.get('agencia') or {}).get('id', '')}"
    else:
        nota = "ENTREGA A DOMICILIO" + (f" - REF: {p['referencia']}" if p.get("referencia") else "")
    return {
        "name": p["nombre"],
        "surname": p["apellido"],
        "phone": _tel9(p["celular"]),
        "dir": p["dir_dropi"],
        "rate_type": "CON RECAUDO",
        "products": promos.productos_dropi(clave),
        "notes": f"{nota} · Pedido web {p['codigo']}",
        "shop_order_id": p["codigo"],
    }


def preparar(p):
    """Destino + armado con stock + cotizacion. Devuelve (ok, mensaje)."""
    if p["estado"] != "tomado":
        return False, f"El pedido está «{p['estado']}»."
    if not promos.valida(p["promo"]):
        return False, f"La promo {p['promo']} no existe en el bot (revisa promos.py)."
    ag = p.get("agencia") or {}
    try:
        destino = dropi_pedido.resolver_destino(ag.get("ciudad") or p["ciudad"],
                                                ag.get("provincia") or p["provincia"])
    except dropi_pedido.DestinoError as e:
        return False, str(e)
    clave, faltan, stock = ficha._elegir_armado(p["promo"])
    if faltan:
        return False, ("SIN STOCK en Dropi: " + "; ".join(faltan)
                       + ". Créalo a mano en Dropi o espera reposición.")
    datos = _datos_dropi(p, clave)
    cot = dropi_pedido.cotizar(destino, datos["products"], datos["dir"])
    if not cot["ok"]:
        return False, cot["error"] + " Si persiste, créalo a mano en Dropi."
    p["preparado"] = {"clave": clave, "datos": datos, "destino": destino,
                      "cotizacion": cot, "stock": stock}
    return True, ""


def texto_confirmacion(p):
    pr = p["preparado"]
    d, dest, cot = pr["datos"], pr["destino"], pr["cotizacion"]
    L = [f"📝 CONFIRMAR PEDIDO WEB {p['codigo']} EN DROPI", "",
         f"👤 {d['name']} {d['surname']}",
         f"📲 {d['phone']}",
         f"🗺️ Dropi: {dest['city']} — {dest['state']} (ciudad #{dest['city_id']})",
         f"📦 {d['dir']}", "",
         f"🎁 {promos.descripcion(pr['clave'])}", "",
         f"🚚 SERVIENTREGA · contra entrega · envío ${cot['costo']:.2f} {cot['moneda']}",
         f"💵 El cliente paga ${promos.total_promo(pr['clave']):.2f}",
         "", "📦 Stock en Dropi:"] + [f"   {x}" for x in pr["stock"]]
    L += ["", "¿Creo el pedido?"]
    return "\n".join(L)


def teclado_confirmacion(p):
    pid = str(p["id"])
    return [[{"text": "✅ Sí, crear en Dropi", "callback_data": f"tk:{pid}"}],
            [{"text": "↩️ Volver", "callback_data": f"tv:{pid}"}]]


# --------------------------------------------------------------- crear --

def crear(p):
    """create_order + aviso a la tienda. Devuelve {"ok", "order_id", "mensaje"}."""
    pr = p.get("preparado")
    if not pr:
        return {"ok": False, "order_id": None,
                "mensaje": "La confirmación venció: pulsa «Crear en Dropi» de nuevo."}
    dropi_pedido.olvidar_pedidos()
    ex = dropi_pedido.pedido_existente(p["celular"])
    if ex:
        p["existente"] = ex
        p["preparado"] = None
        return {"ok": False, "order_id": None,
                "mensaje": (f"Ya existe en Dropi el pedido #{ex['order_id']} con este teléfono. "
                            "Si es otro pedido distinto, créalo a mano en Dropi.")}
    res = dropi_pedido.crear(pr["datos"], pr["destino"])
    p["preparado"] = None
    if res["ok"]:
        p["estado"], p["dropi_id"] = "creado", res["order_id"]
        try:
            _estado(p["id"], "creado", res["order_id"])
        except TiendaError as e:
            res["mensaje"] = (res.get("mensaje") or "") + f"\n⚠️ No pude avisar a la tienda: {e}"
    return res


def cancelar(p):
    _estado(p["id"], "cancelado", nota="Cancelado desde Telegram")
    p["estado"] = "cancelado"
    p["preparado"] = None
