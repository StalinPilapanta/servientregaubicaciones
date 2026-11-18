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

import copy
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
        "ids_crm": (186883, 196169, 193483, 202300),
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

# --- Combos fijos ---
# Un embudo con "combos" sube EXACTAMENTE esos items (producto, cantidad y
# precio) en vez de repartir el total. Asi cargan NAD Pro Max las otras
# cuentas (50 pedidos c/u, 09-18/11/2026):
#   1 frasco  = 1x NAD PRO MAX $24.99                               (19 pedidos)
#   2 frascos = 2x NAD PRO MAX $15 + 1x CANDELA $4.99 = $34.99      (10 pedidos)
#               (otra cuenta: 2x $12 + Batana $10.99 = $34.99)
#   3 frascos = 3x NAD PRO MAX $12 + 1x Batana $8.99 = $44.99       (1 pedido)
# En el CRM aparece como 149084 o 18186 ("Nad + Promax"); se sube 149084.

def _it(id_, nombre, precio, cantidad=1):
    return {"id": id_, "nombre": nombre, "price": precio, "quantity": cantidad}


NAD = (149084, "NAD PRO MAX")
CANDELA = (175317, "CANDELA")

EMBUDOS["nad"] = {
    "nombre": "NAD Pro Max",
    "ids_crm": (149084, 18186),
    # "nad" solo no: tambien esta dentro de "nada".
    "palabras": ("nad pro", "nad +", "nad+", "promax", "pro max"),
    "combos": {
        1: [_it(*NAD, 24.99)],
        2: [_it(*NAD, 15.00, 2), _it(*CANDELA, 4.99)],
        3: [_it(*NAD, 12.00, 3), _it(BATANA["id"], BATANA["nombre"], 8.99)],
    },
    "precio_frasco_desde_7": None,
    "regalo_desde": 2,
}

# BioKids Moringa (gomitas para ninos). Precios desde 18/11/2026:
# 1 frasco $27.99 · 2 frascos $37.99 + regalo (2x $14.50 + Batana $8.99).
# El regalo sube como Batana, igual que NAD de 3 frascos.
BIOKIDS = (118567, "BIOKIDS MORINGA")
EMBUDOS["biokids"] = {
    "nombre": "BioKids Moringa",
    "ids_crm": (118567, 107177),
    "palabras": ("biokids", "bio kids", "gomitas con moringa"),
    "combos": {
        1: [_it(*BIOKIDS, 27.99)],
        2: [_it(*BIOKIDS, 14.50, 2), _it(BATANA["id"], BATANA["nombre"], 8.99)],
    },
    "precio_frasco_desde_7": None,
    "regalo_desde": 2,
}
# --- Alternativas (si un producto se agota o deja de verse en Dropi) ---
# Al crear el pedido se usa la PRIMERA opcion con stock; si no, la siguiente.
# Drenaje 4 frascos = $38 (pedido 8020687, 18/11/2026: X3 $21 + X1 $9 + Batana $8).
DRENAJE_2X1 = (186883, "Promocion Drenaje Linfatico 30ml 2x1")   # 2 frascos
DRENAJE_X3 = (196169, "Drenaje Linfatico 30ml x3 unidades")      # 3 frascos
DRENAJE_X1 = (193483, "Drenaje Linfatico 30ml")                  # 1 frasco
_BATANA_REGALO = (BATANA["id"], BATANA["nombre"] + " (regalo)")

EMBUDOS["drenaje"]["alternativas"] = {
    2: [
        [_it(*DRENAJE_2X1, 27.99)],
        [_it(*DRENAJE_X1, 14.00, 2)],                                     # $28.00
    ],
    4: [
        [_it(*DRENAJE_X3, 21.00), _it(*DRENAJE_X1, 9.00), _it(*_BATANA_REGALO, 8.00)],
        [_it(*DRENAJE_2X1, 15.00, 2), _it(*_BATANA_REGALO, 8.00)],
        [_it(*DRENAJE_X1, 7.50, 4), _it(*_BATANA_REGALO, 8.00)],
    ],
}

for _e in EMBUDOS.values():
    if "combos" in _e:  # el total de cada combo es su precio
        _e["precios"] = {n: round(sum(x["price"] * x["quantity"] for x in c), 2)
                         for n, c in _e["combos"].items()}

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
    186883: ("PROMOCION DRENAJE", 15819),
    86248: "ACEITE BATANA OIL",
    111484: "TURKESTERONE",
    204734: "AURELYS 35ML",
    120461: "Her loss",
    196169: ("DRENAJE LINFATICO 30ML X3", 15819),
    193483: ("DRENAJE LINFATICO 30 ML", 15819),
    202300: "Drenaje Linfatico x2",
    # NAD y su Candela son del proveedor 15819; con el proveedor la busqueda
    # no se pierde entre los otros "NAD"/"CANDELA" del catalogo.
    149084: ("NAD PRO MAX", 15819),
    175317: ("CANDELA", 15819),  # regalo de NAD 2 frascos
    146736: "VIGORMAX",
    107177: "BIOKIDS MORINGA X2",
    118567: "BIOKIDS MORINGA",
    61427: "MAGNESIUM CITRATE KIDS",
}

# Catalogo de productos de Dropi que se venden en las cuentas, con su id.
# Sacado de los pedidos reales (20/10 - 18/11/2026) de los conectores de
# Dropi de cada cuenta. "cuentas": donde aparece vendido. Los precios son
# los vistos en los pedidos (dependen de la promo/combinacion).
#   vitashop = conector 5380b46f
#   cuenta_a = conector 2478aa1f  (Mona o Import360: confirmar)
#   cuenta_b = conector 5449aec9  (Mona o Import360: confirmar)
CATALOGO = {
    148700: {"nombre": "MEGA RED PRIV", "cuentas": ("vitashop", "cuenta_a", "cuenta_b"),
             "precios_vistos": "1 x $27.99; 2 x $13-$16.95 en combos"},
    14235: {"nombre": "Mega Red 3", "cuentas": ("vitashop", "cuenta_b"),
            "precios_vistos": "1 x $27.99"},
    186883: {"nombre": "PROMOCION DRENAJE LINFATICO 30 ML 2X1",
             "cuentas": ("vitashop", "cuenta_a", "cuenta_b"),
             "precios_vistos": "$11.99-$16.99 en combo con X3; 2 x $13"},
    196169: {"nombre": "DRENAJE LINFATICO 30ML X3 UNIDADES", "cuentas": ("cuenta_a", "cuenta_b"),
             "precios_vistos": "1 x $21 (combo con 2X1); 2 x $18.99-$24"},
    193483: {"nombre": "DRENAJE LINFATICO 30 ML", "cuentas": ("cuenta_b",),
             "precios_vistos": "1 x $9.99 (combo)"},
    202300: {"nombre": "Drenaje Linfatico x2", "cuentas": ("cuenta_a",),
             "precios_vistos": "1 x $27.99"},
    149084: {"nombre": "NAD PRO MAX", "cuentas": ("cuenta_a", "cuenta_b"),
             "precios_vistos": "1 x $24.99 o $27.99; 2 x $15-$17.49; 3 x $12"},
    175317: {"nombre": "CANDELA", "cuentas": ("cuenta_a",),
             "precios_vistos": "1 x $4.99 (complemento de NAD)"},
    146736: {"nombre": "VIGORMAX", "cuentas": ("cuenta_a",),
             "precios_vistos": "1 x $3.09 (complemento de Mega Red)"},
    107177: {"nombre": "BIOKIDS MORINGA X2", "cuentas": ("cuenta_b",),
             "precios_vistos": "1 x $24; 2 x $22"},
    118567: {"nombre": "BIOKIDS MORINGA", "cuentas": ("cuenta_b",),
             "precios_vistos": "1 x $13"},
    61427: {"nombre": "MAGNESIUM CITRATE KIDS", "cuentas": ("cuenta_b",),
            "precios_vistos": "1 x $13"},
    86248: {"nombre": "ACEITE BATANA OIL", "cuentas": ("vitashop", "cuenta_a", "cuenta_b"),
            "precios_vistos": "regalo/complemento $2.25-$11.99"},
    111484: {"nombre": "TURKESTERONE", "cuentas": ("vitashop", "cuenta_b"),
             "precios_vistos": "regalo/complemento $11.99"},
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

def _sin_opcion(clave):
    """'drenaje:4#1' -> ('drenaje:4', 1). La opcion elige una alternativa."""
    base, _, op = str(clave).partition("#")
    return base, int(op) if op.isdigit() else 0


def opciones(clave):
    """Claves de todas las alternativas de armado, en orden de preferencia."""
    base = _sin_opcion(clave)[0]
    if base in SUELTOS:
        return [base]
    emb, frascos, _ = _partes(base)
    n = len(EMBUDOS.get(emb, {}).get("alternativas", {}).get(frascos, []))
    return [f"{base}#{i}" for i in range(n)] if n > 1 else [base]


def _partes(clave):
    """'megared:2+batana' -> ('megared', 2, True)."""
    base, _, extra = _sin_opcion(clave)[0].partition("+")
    emb, _, n = base.partition(":")
    return emb, int(n) if n.isdigit() else 0, extra == "batana"


def _clave(embudo, frascos, batana=False):
    return f"{embudo}:{frascos}" + ("+batana" if batana else "")


def valida(clave):
    if _sin_opcion(clave)[0] in SUELTOS:
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
    clave = _sin_opcion(clave)[0]
    if clave in SUELTOS:
        return "drenaje" if clave == "aurelys" else clave
    return _partes(clave)[0]


# ------------------------------------------------------------ armado --

def armar(clave):
    """Lista de items {id, nombre, price, quantity} para Dropi."""
    clave, opcion = _sin_opcion(clave)
    if clave in SUELTOS:
        s = SUELTOS[clave]
        return [{"id": s["id"], "nombre": s["nombre"], "price": s["precio"], "quantity": 1}]

    emb, frascos, batana = _partes(clave)
    e = EMBUDOS[emb]
    if frascos in e.get("combos", {}):
        items = copy.deepcopy(e["combos"][frascos])
        if batana:
            items.append({"id": BATANA["id"], "nombre": BATANA["nombre"],
                          "price": PRECIO_BATANA_EXTRA, "quantity": 1})
        return items
    alternativas = e.get("alternativas", {}).get(frascos)
    if alternativas:
        items = copy.deepcopy(alternativas[min(opcion, len(alternativas) - 1)])
        if batana:
            items.append({"id": BATANA["id"], "nombre": BATANA["nombre"],
                          "price": PRECIO_BATANA_EXTRA, "quantity": 1})
        return items
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
    if _sin_opcion(clave)[0] in SUELTOS:
        return False
    emb, frascos, _ = _partes(clave)
    return frascos >= EMBUDOS[emb]["regalo_desde"]


def total_promo(clave):
    return round(sum(x["price"] * x["quantity"] for x in armar(clave)), 2)


def productos_dropi(clave):
    return [{"id": x["id"], "price": x["price"], "quantity": x["quantity"]}
            for x in armar(clave)]


def descripcion(clave):
    if _sin_opcion(clave)[0] in SUELTOS:
        titulo = SUELTOS[_sin_opcion(clave)[0]]["nombre"]
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
