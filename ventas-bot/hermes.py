"""
Cliente para delegar a Hermes (agente en el VPS con MCP de Dropi) la creacion
del pedido.

El bot ya dejo los datos limpios (promo, productos, precios, agencia). Hermes
solo ejecuta `create_order` con el MCP de Dropi y devuelve el resultado.

Como no se conoce aun la API de Hermes, el contrato es configurable:

  HERMES_URL      endpoint HTTP al que se hace POST con el JSON (obligatorio)
  HERMES_TOKEN    token opcional (se envia como 'Authorization: Bearer ...')
  HERMES_TIMEOUT  segundos de espera (def. 120; el agente puede tardar)

Request  (POST JSON):
  {"accion": "crear_pedido_dropi", "instruccion": "<texto>", "datos": {...}}

Respuesta esperada (JSON), cualquiera de estas formas:
  {"ok": true, "order_id": "7826649", "guia": "...", "total": 38, "mensaje": "..."}
  {"ok": false, "error": "..."}
Si Hermes responde texto libre, se muestra tal cual y se considera ok solo si
se puede extraer un numero de pedido.

Anti-duplicados: cada pedido creado se registra en pedidos_creados.json por
user_ns. Un segundo intento sobre el mismo cliente se bloquea.
"""

import json
import os
import re

import requests

import agencias
import chateapro
import promos
import datos

URL = os.environ.get("HERMES_URL", "").strip()
TOKEN = os.environ.get("HERMES_TOKEN", "").strip()
TIMEOUT = int(os.environ.get("HERMES_TIMEOUT", "120"))

RUTA_CREADOS = os.environ.get(
    "PEDIDOS_CREADOS_PATH",
    datos.ruta("pedidos_creados.json"),
)

INSTRUCCION = (
    "Crea UN solo pedido en Dropi usando la herramienta create_order del MCP de Dropi "
    "con EXACTAMENTE los datos del campo 'datos'. Pasos: 1) resuelve la ciudad con "
    "search_cities (usa el id y department que correspondan a 'city'/'state'); "
    "2) cotiza con quote_shipping solo para confirmar cobertura de Servientrega; "
    "3) llama create_order con rate_type, productos y precios tal cual vienen "
    "(NO cambies precios ni cantidades); en 'dir' va la direccion de la agencia de "
    "retiro. Si la ciudad no se puede resolver con certeza, NO crees el pedido y "
    "responde ok=false con el motivo. Si hay un error de red al crear, NO reintentes: "
    "verifica primero con list_orders si el pedido ya existe (por telefono o nombre). "
    "Responde SOLO un JSON: "
    '{"ok": true|false, "order_id": "...", "guia": "...", "total": 0, "mensaje": "...", "error": "..."}'
)


# ---------------------------------------------------------------- registro --

def _leer_creados():
    try:
        with open(RUTA_CREADOS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ya_creado(user_ns):
    """Devuelve el registro del pedido ya creado para ese cliente, o None."""
    return _leer_creados().get(user_ns)


def registrar(user_ns, resultado):
    datos = _leer_creados()
    datos[user_ns] = {
        "order_id": resultado.get("order_id"),
        "guia": resultado.get("guia"),
        "fecha": chateapro.hoy_ecuador().strftime("%d/%m/%Y"),
    }
    try:
        with open(RUTA_CREADOS, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("No se pudo guardar pedidos_creados.json:", e)


# ------------------------------------------------------------------ datos --

def armar_datos(campos, promo_clave, agencia, items=None):
    """
    Arma el payload limpio para Hermes/create_order a partir de los campos de
    Chatea Pro, la promo elegida y la agencia asignada (dict de agencias.py).
    items: productos ya armados (ej. con el regalo cambiado); si no, los de la promo.
    """
    if items:
        productos = [{"id": x["id"], "price": x["price"], "quantity": x["quantity"]} for x in items]
        total = round(sum(x["price"] * x["quantity"] for x in items), 2)
    else:
        productos = promos.productos_dropi(promo_clave)
        total = promos.total_promo(promo_clave)
    nombre, apellido = chateapro.separar_nombre(
        campos.get(chateapro.CAMPO_NOMBRE) or campos.get("_name")
    )
    telefono = re.sub(r"\D", "", str(campos.get("_phone", "")))
    if telefono.startswith("593"):
        telefono = telefono[3:]
    telefono = telefono.lstrip("0")

    return {
        "name": nombre,
        "surname": apellido,
        "phone": telefono,
        "state": agencia.get("provincia") or campos.get(chateapro.CAMPO_PROVINCIA, ""),
        "city": agencia.get("ciudad") or campos.get(chateapro.CAMPO_CIUDAD, ""),
        "dir": agencias.direccion_dropi(agencia),
        "rate_type": "CON RECAUDO",
        "products": productos,
        "notes": f"RETIRO EN AGENCIA: {agencia.get('nombre', '')}",
        "total_esperado": total,
        "shop_order_id": campos.get("_user_ns", ""),
    }


# ----------------------------------------------------------------- llamada --

def disponible():
    return bool(URL)


def crear_pedido(datos):
    """
    Envia los datos a Hermes y devuelve:
      {"ok": bool, "order_id": str|None, "guia": str|None, "mensaje": str}
    Nunca lanza excepcion: los errores se devuelven con ok=False.
    """
    if not URL:
        return _fallo("HERMES_URL no configurada.")

    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    try:
        r = requests.post(
            URL,
            headers=headers,
            json={"accion": "crear_pedido_dropi", "instruccion": INSTRUCCION,
                  "datos": datos},
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        return _fallo("Hermes tardo demasiado. NO reintentes: revisa en Dropi "
                      "si el pedido se creo antes de volver a intentar.")
    except requests.RequestException as e:
        return _fallo(f"No se pudo contactar a Hermes: {e}")

    if r.status_code >= 400:
        return _fallo(f"Hermes respondio HTTP {r.status_code}: {r.text[:200]}")

    return _interpretar(r)


def _interpretar(r):
    try:
        d = r.json()
    except ValueError:
        d = None

    if isinstance(d, dict):
        # Algunos agentes envuelven la respuesta: {"response": "<json o texto>"}
        for k in ("response", "output", "result", "message"):
            if isinstance(d.get(k), str) and "ok" not in d:
                interno = _json_en_texto(d[k])
                if interno is not None:
                    d = interno
                    break
        if "ok" in d:
            return {
                "ok": bool(d.get("ok")),
                "order_id": str(d["order_id"]) if d.get("order_id") else None,
                "guia": d.get("guia") or None,
                "mensaje": str(d.get("mensaje") or d.get("error") or "")[:400],
            }
        texto = json.dumps(d, ensure_ascii=False)
    else:
        texto = r.text

    # Texto libre: solo es exito si aparece un numero de pedido claro.
    m = re.search(r"(?:pedido|orden|order)\D{0,12}(\d{6,9})", texto, re.IGNORECASE)
    if m:
        return {"ok": True, "order_id": m.group(1), "guia": None,
                "mensaje": texto[:400]}
    return _fallo(f"Respuesta de Hermes no reconocida: {texto[:300]}")


def _json_en_texto(texto):
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group())
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def _fallo(mensaje):
    return {"ok": False, "order_id": None, "guia": None, "mensaje": mensaje}
