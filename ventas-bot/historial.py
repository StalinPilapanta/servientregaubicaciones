"""
Aprende como arma ESTA cuenta sus pedidos de Dropi, desde su propio historial
en Chatea Pro. Asi una cuenta nueva no necesita escribir combos en promos.py.

Cada venta que ya paso a Dropi guarda en el suscriptor:
  * lo vendido:  "ID de Dropi" (embudo), "Cantidad de productos", "Valor de la compra"
  * lo subido:   "[Logistica] JSON products para Fletes" -> [{id, quantity, price}]
  * los nombres: "[Confirmaciones] JSON productos dropi" -> [{product_id, product{name}}]

Con eso se arma la tabla (embudo, frascos, valor) -> armados vistos, del mas
frecuente al menos. Ej. VitashopEc: (148700, 2, $38) -> 2x 148700 $15 + 1x 86248 $8.

La tabla se guarda en DATA_DIR/historial_promos.json, se refresca cada
HISTORIAL_REFRESCO_HORAS barriendo los suscriptores, y cada pedido que crea
el bot se suma al instante (registrar).
"""

import json
import os
import threading
import time
from collections import Counter

import datos

RUTA = os.environ.get("HISTORIAL_PROMOS_PATH") or datos.ruta("historial_promos.json")
REFRESCO_HORAS = float(os.environ.get("HISTORIAL_REFRESCO_HORAS", "6"))
MAX_PAGINAS = int(os.environ.get("HISTORIAL_MAX_PAGINAS", "10"))
# Veces que se debe haber visto un armado para usarlo solo.
MIN_VISTOS = int(os.environ.get("HISTORIAL_MIN_VISTOS", "2"))
TOLERANCIA = 0.15  # $37.99 y $38.00 son la misma promo

CAMPO_EMBUDO = "ID de Dropi"
CAMPO_CANTIDAD = "Cantidad de productos"
CAMPO_VALOR = "Valor de la compra"
CAMPO_ARMADO = "[Logistica] JSON products para Fletes"
CAMPO_NOMBRES = "[Confirmaciones] JSON productos dropi"

_lock = threading.Lock()
_estado = {"cargado": False, "ts": 0, "ventas": {}, "nombres": {}}


# ------------------------------------------------------------ lectura --

def _num(valor):
    try:
        return float(str(valor).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _json(valor):
    if isinstance(valor, (list, dict)):
        return valor
    try:
        return json.loads(valor or "")
    except (TypeError, ValueError):
        return None


def _campos_de(sub):
    """Suscriptor de la API ({user_fields: [...]}) o dict de campos -> dict."""
    if "user_fields" in sub:
        return {f.get("name", ""): f.get("value") for f in sub.get("user_fields") or []}
    return sub


def extraer(sub):
    """(venta, nombres) de un suscriptor, o (None, {}) si no tiene pedido armado."""
    c = _campos_de(sub)
    emb = _num(c.get(CAMPO_EMBUDO))
    frascos = _num(c.get(CAMPO_CANTIDAD))
    valor = _num(c.get(CAMPO_VALOR))
    armado = _json(c.get(CAMPO_ARMADO))
    if not (emb and frascos and valor and isinstance(armado, list) and armado):
        return None, {}
    try:
        items = sorted([int(x["id"]), int(float(x["quantity"])), round(float(x["price"]), 2)]
                       for x in armado)
    except (KeyError, TypeError, ValueError):
        return None, {}
    total = sum(q * p for _, q, p in items)
    if abs(total - valor) > max(TOLERANCIA, valor * 0.02):
        return None, {}  # lo subido no corresponde al valor del CRM: no se aprende
    nombres = {}
    for x in _json(c.get(CAMPO_NOMBRES)) or []:
        try:
            nombres[str(int(x["product_id"]))] = x["product"]["name"]
        except (KeyError, TypeError, ValueError):
            pass
    venta = {"emb": int(emb), "frascos": int(frascos), "valor": round(valor, 2), "items": items}
    return venta, nombres


# ------------------------------------------------------- persistencia --

def _cargar():
    if _estado["cargado"]:
        return
    try:
        with open(RUTA, encoding="utf-8") as f:
            d = json.load(f)
        _estado.update(ts=d.get("ts", 0), ventas=d.get("ventas", {}), nombres=d.get("nombres", {}))
    except (OSError, ValueError):
        pass
    _estado["cargado"] = True


def _guardar():
    tmp = RUTA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": _estado["ts"], "ventas": _estado["ventas"],
                   "nombres": _estado["nombres"]}, f, ensure_ascii=False)
    os.replace(tmp, RUTA)


def aprender_de(suscriptores):
    """Suma al historial las ventas con pedido armado. Devuelve cuantas aprendio."""
    n = 0
    with _lock:
        _cargar()
        for s in suscriptores:
            venta, nombres = extraer(s)
            ns = (s.get("user_ns") or s.get("_user_ns") or "") if isinstance(s, dict) else ""
            if not venta or not ns:
                continue
            _estado["ventas"][ns] = venta
            _estado["nombres"].update(nombres)
            n += 1
    return n


def refrescar(forzar=False):
    """Barre los suscriptores de Chatea Pro y actualiza la tabla (si toca)."""
    with _lock:
        _cargar()
        if not forzar and time.time() - _estado["ts"] < REFRESCO_HORAS * 3600:
            return None
    import chateapro  # tarde: el modulo se puede probar sin API
    subs = chateapro.listar_todos_suscriptores(max_paginas=MAX_PAGINAS)
    n = aprender_de(subs)
    with _lock:
        _estado["ts"] = time.time()
        _guardar()
    print(f"historial: {n} ventas con pedido aprendidas de {len(subs)} suscriptores")
    return n


def registrar(user_ns, campos, items):
    """Suma al instante un pedido que acaba de crear el bot."""
    c = dict(campos)
    c[CAMPO_ARMADO] = [{"id": x["id"], "quantity": x["quantity"], "price": x["price"]} for x in items]
    c[CAMPO_NOMBRES] = [{"product_id": x["id"],
                         "product": {"name": x["nombre"].replace(" (regalo)", "")}}
                        for x in items if x.get("nombre") and not x["nombre"].startswith("#")]
    c["_user_ns"] = user_ns
    if aprender_de([c]):
        with _lock:
            _guardar()


# ------------------------------------------------------------ consulta --

def nombre(id_producto):
    with _lock:
        _cargar()
        return _estado["nombres"].get(str(id_producto))


def armados(emb, frascos, valor):
    """
    [(items, vistos), ...] de esta combinacion, del mas visto al menos.
    items = [[id, cantidad, precio], ...]. Valor con tolerancia de $0.15.
    """
    with _lock:
        _cargar()
        ventas = list(_estado["ventas"].values())
    cuenta = Counter(
        json.dumps(v["items"]) for v in ventas
        if v["emb"] == emb and v["frascos"] == frascos and abs(v["valor"] - valor) <= TOLERANCIA)
    return [(json.loads(k), n) for k, n in cuenta.most_common()]


def resumen():
    """Texto con la tabla aprendida (para Telegram / revision)."""
    with _lock:
        _cargar()
        ventas = list(_estado["ventas"].values())
        nombres = dict(_estado["nombres"])
    combos = Counter((v["emb"], v["frascos"], v["valor"], json.dumps(v["items"])) for v in ventas)
    L = [f"📚 Promos aprendidas ({len(ventas)} ventas con pedido)"]
    for (emb, frascos, valor, items), n in sorted(combos.items(), key=lambda x: (x[0][0], x[0][1], -x[1])):
        txt = " + ".join(f"{q}x {nombres.get(str(i), '#' + str(i))} ${p:g}" for i, q, p in json.loads(items))
        L.append(f"• {nombres.get(str(emb), emb)} · {frascos} fr · ${valor:g} → {txt}  ({n}x)")
    return "\n".join(L)
