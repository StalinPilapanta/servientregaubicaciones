"""
Como se arma el pedido de Dropi a partir de la venta de Chatea Pro.

Aprendido de los pedidos reales (13-16/11/2026) cruzados con el CRM:

  * Cada embudo de Chatea Pro vende UN producto y lo guarda en el campo
    "ID de Dropi" (148700 = Mega Red, 186883 = Drenaje Linfatico 2x1).
  * "Cantidad de productos" son FRASCOS que pidio el cliente.
  * El precio sale de la escalera del embudo (campo Precios_y_ofertas):
      Mega Red: 1 = $27.99 | 2 = $37.99 + regalo | 3 = $52 | 4 = $65 |
                5 = $80 | 6 = $97 | 7+ = cantidad x $15
      Drenaje:  2 frascos = $27.99 | 4 frascos = $37.99 + regalo
  * La promo de $37.99 se sube a Dropi como $38.00 (asi se viene haciendo).
  * Desde la promo de la semana va un REGALO SORPRESA, que se sube como
    Aceite Batana Oil (86248) a $8 (alguna vez Turkesterone 111484).
  * El producto que se sube a Dropi NO siempre es el del CRM:
      - Mega Red 1 frasco  -> Mega Red 3 (14235)            [16/11]
      - Mega Red 2 frascos -> 2x MEGA RED PRIV (148700) $15 + Batana $8
      - Drenaje 2 frascos  -> 1x PROMOCION DRENAJE 2X1 (186883) $27.99
      - Drenaje 4 frascos  -> 2x PROMOCION DRENAJE 2X1 $15 + Batana $8
    Todo eso se ajusta abajo en EMBUDOS sin tocar el resto del bot.

Una "promo" es una clave "<embudo>:<frascos>" (ej. "megared:2"), con
sufijo "+batana" si el cliente pago el aceite aparte (ej. 1 Mega Red +
Batana = $35.99).
"""

import re
import unicodedata

BATANA = {"id": 86248, "nombre": "Aceite Batana Oil"}
TURKESTERONE = {"id": 111484, "nombre": "Turkesterone"}

# Regalo sorpresa que se sube a Dropi y su precio dentro del total.
REGALO = BATANA
PRECIO_REGALO = 8.00
# Precio del aceite cuando el cliente lo compra aparte.
PRECIO_BATANA_EXTRA = 8.00

EMBUDOS = {
    "megared": {
        "nombre": "Mega Red Omega-3 Krill Oil",
        "ids_crm": (148700, 14235),
        "palabras": ("megared", "mega red", "krill", "omega"),
        # Producto de Dropi segun frascos (los que no esten -> "defecto").
        "producto": {
            1: {"id": 14235, "nombre": "Mega Red 3", "frascos_por_unidad": 1},
            "defecto": {"id": 148700, "nombre": "Mega Red Priv", "frascos_por_unidad": 1},
        },
        # frascos -> total al cliente
        "precios": {1: 27.99, 2: 38.00, 3: 52.00, 4: 65.00, 5: 80.00, 6: 97.00},
        "precio_frasco_desde_7": 15.00,
        "regalo_desde": 2,
    },
    "drenaje": {
        "nombre": "Drenaje Linfatico",
        "ids_crm": (186883, 196169, 193483),
        "palabras": ("drenaje", "linfatico", "linfatic"),
        # 186883 es la PROMOCION 2X1: cada unidad trae 2 frascos.
        "producto": {
            "defecto": {"id": 186883, "nombre": "Promocion Drenaje Linfatico 30ml 2x1",
                        "frascos_por_unidad": 2},
        },
        "precios": {2: 27.99, 4: 38.00},
        "precio_frasco_desde_7": None,
        "regalo_desde": 4,
    },
}

# Productos de un solo item que se venden sueltos (sin escalera).
SUELTOS = {
    "aurelys": {"nombre": "Drenaje Linfatico Aurelys 35ml", "id": 204734,
                "precio": 19.99, "palabras": ("aurelys 35", "aurelys")},
    "herloss": {"nombre": "Her Loss", "id": 120461, "precio": 20.98,
                "palabras": ("her loss", "herloss")},
}

# Texto con el que se encuentra cada producto en list_products de Dropi
# (busqueda literal por nombre) para revisar el stock antes de crear.
BUSQUEDA_STOCK = {
    14235: "Mega Red 3",
    148700: "MEGA RED PRIV",
    186883: "PROMOCION DRENAJE",
    86248: "ACEITE BATANA OIL",
    111484: "TURKESTERONE",
    204734: "AURELYS 35ML",
    120461: "Her loss",
}

_TOLERANCIA = 0.15
_NUMEROS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4,
            "cinco": 5, "seis": 6, "siete": 7, "ocho": 8}


def _norm(texto):
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.replace("-", " ")).strip()


def _a_float(valor):
    m = re.search(r"\d+(?:[.,]\d+)?", str(valor or ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", "."))
    except ValueError:
        return None


def _a_int(valor):
    v = _a_float(valor)
    return int(v) if v and v == int(v) else None


# ------------------------------------------------------------ claves --

def _partes(clave):
    """'megared:2+batana' -> ('megared', 2, True)."""
    base, _, extra = str(clave).partition("+")
    emb, _, n = base.partition(":")
    return emb, int(n) if n.isdigit() else 0, extra == "batana"


def _clave(embudo, frascos, batana=False):
    return f"{embudo}:{frascos}" + ("+batana" if batana else "")


def valida(clave):
    if clave in SUELTOS:
        return True
    emb, n, _ = _partes(clave)
    return emb in EMBUDOS and n > 0 and _total_escalera(emb, n) is not None


def _total_escalera(embudo, frascos):
    e = EMBUDOS[embudo]
    if frascos in e["precios"]:
        return e["precios"][frascos]
    if e["precio_frasco_desde_7"] and frascos >= 7:
        return round(frascos * e["precio_frasco_desde_7"], 2)
    return None


def familia(clave):
    if clave in SUELTOS:
        return "drenaje" if clave == "aurelys" else clave
    return _partes(clave)[0]


# ------------------------------------------------------------ armado --

def armar(clave):
    """Lista de items {id, nombre, price, quantity} para Dropi."""
    if clave in SUELTOS:
        s = SUELTOS[clave]
        return [{"id": s["id"], "nombre": s["nombre"], "price": s["precio"], "quantity": 1}]

    emb, frascos, batana = _partes(clave)
    e = EMBUDOS[emb]
    total = _total_escalera(emb, frascos)
    prod = e["producto"].get(frascos) or e["producto"]["defecto"]
    unidades = max(1, frascos // prod["frascos_por_unidad"])

    items = []
    resto = total
    con_regalo = frascos >= e["regalo_desde"]
    if con_regalo:
        resto -= PRECIO_REGALO
    precio_u = round(resto / unidades, 2)
    items.append({"id": prod["id"], "nombre": prod["nombre"],
                  "price": precio_u, "quantity": unidades})
    if con_regalo:
        # El regalo absorbe los centavos del redondeo para que el total cuadre.
        items.append({"id": REGALO["id"], "nombre": REGALO["nombre"] + " (regalo)",
                      "price": round(total - precio_u * unidades, 2), "quantity": 1})
    if batana:
        items.append({"id": BATANA["id"], "nombre": BATANA["nombre"],
                      "price": PRECIO_BATANA_EXTRA, "quantity": 1})
    return items


def tiene_regalo(clave):
    """True si la venta lleva regalo sorpresa (promo de la semana)."""
    if clave in SUELTOS:
        return False
    emb, frascos, _ = _partes(clave)
    return frascos >= EMBUDOS[emb]["regalo_desde"]


def total_promo(clave):
    return round(sum(x["price"] * x["quantity"] for x in armar(clave)), 2)


def productos_dropi(clave):
    return [{"id": x["id"], "price": x["price"], "quantity": x["quantity"]}
            for x in armar(clave)]


def descripcion(clave):
    if clave in SUELTOS:
        titulo = SUELTOS[clave]["nombre"]
    else:
        emb, frascos, batana = _partes(clave)
        titulo = f"{EMBUDOS[emb]['nombre']} — {frascos} frasco{'s' if frascos > 1 else ''}"
        if batana:
            titulo += " + Aceite Batana"
    L = [f"{titulo} — ${total_promo(clave):.2f}"]
    for x in armar(clave):
        L.append(f"  • {x['quantity']}x {x['nombre']} (#{x['id']}, ${x['price']:.2f})")
    return "\n".join(L)


# --------------------------------------------------------- deteccion --

def familias_en(texto):
    t = _norm(texto)
    f = {k for k, e in EMBUDOS.items() if any(p in t for p in e["palabras"])}
    f |= {k for k, s in SUELTOS.items() if any(p in t for p in s["palabras"])}
    if "aurelys" in f and "drenaje" in f and re.search(r"\b[2-9]\s*frasco", t):
        f.discard("aurelys")  # "4 frascos de Drenaje Aurelys" es la promo, no el suelto
    return f


def _cantidad_texto(texto):
    t = _norm(texto)
    m = re.search(r"\b(\d+|" + "|".join(_NUMEROS) + r")\s+"
                  r"(?:frasco|unidad|caja|botella|megared|mega red|drenaje)", t)
    if not m:
        return None
    n = m.group(1)
    return int(n) if n.isdigit() else _NUMEROS.get(n)


def _embudo_por_id(id_dropi):
    i = _a_int(id_dropi)
    for k, e in EMBUDOS.items():
        if i in e["ids_crm"]:
            return k
    for k, s in SUELTOS.items():
        if i == s["id"]:
            return k
    return None


def _embudo_por_valor(valor, frascos=None, solo=None):
    """Embudo y frascos cuyo total coincide con el valor (si es unico)."""
    if valor is None:
        return None
    hits = []
    for k in ([solo] if solo else EMBUDOS):
        for n in list(EMBUDOS[k]["precios"]) + list(range(7, 13)):
            t = _total_escalera(k, n)
            if t is not None and abs(t - valor) <= _TOLERANCIA and (frascos in (None, n)):
                hits.append((k, n))
    return hits[0] if len(set(hits)) == 1 else None


def detectar(campos, texto_chat=""):
    """
    Detecta la promo desde los campos del CRM. Devuelve (clave|None, origen, avisos).

    Prioridad: ID de Dropi -> embudo; Cantidad de productos -> frascos; el
    texto de 'Productos escogidos' y el valor solo confirman o desempatan.
    """
    escogidos = campos.get("Productos escogidos", "")
    valor = _a_float(campos.get("Valor de la compra"))
    avisos = []

    fams_txt = familias_en(escogidos)
    emb = _embudo_por_id(campos.get("ID de Dropi"))
    origen = "ID de Dropi del CRM"
    if emb and fams_txt and emb not in fams_txt:
        avisos.append(f"El ID de Dropi del CRM es de {emb} pero «{escogidos}» dice otra cosa: "
                      "se usó el texto.")
        emb = None
    if not emb and fams_txt:
        sueltos = [k for k in fams_txt if k in SUELTOS]
        por_precio = [k for k in sueltos
                      if valor is not None and abs(SUELTOS[k]["precio"] - valor) <= _TOLERANCIA]
        if por_precio:
            fams_txt = set(por_precio)
        elif len(fams_txt) > 1:
            fams_txt -= set(sueltos)
        emb = sorted(fams_txt)[0] if len(fams_txt) == 1 else None
        origen = "productos del CRM"
    if emb in SUELTOS:
        return emb, origen, avisos

    frascos = _a_int(campos.get("Cantidad de productos")) or _cantidad_texto(escogidos)
    if not emb:
        hit = _embudo_por_valor(valor)
        if hit:
            emb, frascos = hit
            origen = "valor de la compra"
            avisos.append("No hay producto claro en el CRM: se dedujo por el valor.")
    if not emb:
        if escogidos.strip():
            avisos.append(f"«{escogidos}» no corresponde a ningún embudo configurado (promos.py).")
        return None, "", avisos

    if not frascos:
        hit = _embudo_por_valor(valor, solo=emb)
        if hit:
            frascos = hit[1]
    if not frascos:
        avisos.append("No sé cuántos frascos pidió el cliente.")
        return None, "", avisos

    batana = emb == "megared" and "batana" in _norm(escogidos) and frascos < EMBUDOS[emb]["regalo_desde"]
    clave = _clave(emb, frascos, batana)
    if not valida(clave):
        avisos.append(f"{EMBUDOS[emb]['nombre']}: no hay precio configurado para {frascos} frascos.")
        return None, "", avisos

    total = total_promo(clave)
    if valor is not None and abs(valor - total) > 0.02:
        if abs(valor - total) <= _TOLERANCIA:
            avisos.append(f"El CRM dice ${valor:.2f}; en Dropi se sube ${total:.2f} (como siempre).")
        else:
            avisos.append(f"⚠️ El CRM dice ${valor:.2f} pero {frascos} frascos valen ${total:.2f}. "
                          "Revisa antes de crear.")
    return clave, origen, avisos


def detectar_promo(productos_escogidos="", valor_compra="", texto_extra=""):
    """Compatibilidad: deteccion solo con texto + valor."""
    return detectar({"Productos escogidos": productos_escogidos,
                     "Valor de la compra": valor_compra}, texto_extra)[0]


def aviso_valor(clave, valor_compra):
    valor = _a_float(valor_compra)
    if valor is None or abs(valor - total_promo(clave)) < 0.005:
        return ""
    return f"El CRM dice ${valor:.2f} y en Dropi se sube ${total_promo(clave):.2f}."


# ---------------------------------------------------------------- IA --

def claves_validas():
    claves = list(SUELTOS)
    for k, e in EMBUDOS.items():
        claves += [_clave(k, n) for n in e["precios"]]
    return tuple(claves)


def descripcion_para_ia():
    L = []
    for k, e in EMBUDOS.items():
        esc = ", ".join(f"{n} frascos ${p:.2f}" for n, p in e["precios"].items())
        L.append(f'- "{k}:<frascos>": {e["nombre"]} ({esc}). Ej: "{k}:{list(e["precios"])[0]}".')
    for k, s in SUELTOS.items():
        L.append(f'- "{k}": {s["nombre"]} (${s["precio"]:.2f}).')
    return "\n".join(L)
