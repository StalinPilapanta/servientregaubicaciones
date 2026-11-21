"""
Como se arma el pedido de Dropi a partir de la venta de Chatea Pro.

Aprendido de los pedidos reales (13-16/11/2026) cruzados con el CRM:

  * Cada embudo de Chatea Pro vende UN producto y lo guarda en el campo
    "ID de Dropi" (148700 = Mega Red, 186883 = Drenaje Linfatico 2x1).
  * "Cantidad de productos" son FRASCOS que pidio el cliente.
  * Mega Red (20/11/2026): 1 frasco = $27.99 | 2 frascos + regalo = $37.99
    o $38 (se cobra ese valor) | mas frascos: 3 $52 | 4 $65 | 5 $80 | 6 $97 |
    7+ $15 c/u. Cualquier otra cosa del CRM -> 1 frasco $27.99.
  * Drenaje: 2 frascos = $27.99 | 4 frascos = $37.99 + regalo (se sube $38).
  * El REGALO SORPRESA se sube como Te Chino x30 (115343, REGALO_PRINCIPAL).
  * El producto que se sube a Dropi NO siempre es el del CRM:
      - Mega Red 1 frasco  -> Mega Red 3 (14235); sin stock 148700 o 19257
      - Mega Red 2 frascos -> 2x MEGA RED PRIV (148700) $15 + regalo $8 ($7.99)
      - Drenaje 2 frascos  -> 1x PROMOCION DRENAJE 2X1 (186883) $27.99
      - Drenaje 4 frascos  -> 2x PROMOCION DRENAJE 2X1 $15 + Batana $8
    Todo eso se ajusta abajo en EMBUDOS sin tocar el resto del bot.

Una "promo" es una clave "<embudo>:<frascos>" (ej. "megared:2"). No hay
promo "1 Mega Red + Batana": con 1 frasco solo va el producto.
"""

import copy
import os
import re
import unicodedata

BATANA = {"id": 86248, "nombre": "Aceite Batana Oil"}
TURKESTERONE = {"id": 111484, "nombre": "Turkesterone"}

# Regalo sorpresa que se sube a Dropi y su precio dentro del total.
REGALO = BATANA
PRECIO_REGALO = 8.00

# --------------------------------------------------- tablas comunes --
# Van al inicio para que cualquier seccion de abajo las pueda usar.

# Texto con el que se encuentra cada producto en list_products de Dropi
# (busqueda literal por nombre) para revisar el stock antes de crear.
BUSQUEDA_STOCK = {
    14235: "Mega Red 3",
    148700: "MEGA RED PRIV",
    19257: ("MEGA RED", 15819),  # alternativa de Mega Red 1 frasco
    186883: ("PROMOCION DRENAJE", 15819),
    115343: ("CHINO", 15819),  # TE CHINO X30, regalo principal
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

# Regalo sorpresa que se sube en toda promo que lleve regalo (ver es_regalo):
# Te Chino x30, proveedor 15819. Si no hay stock, la ficha ofrece otros.
REGALO_PRINCIPAL = {"id": int(os.environ.get("REGALO_PRINCIPAL_ID", "115343")),
                    "nombre": os.environ.get("REGALO_PRINCIPAL_NOMBRE", "TÉ CHINO X30")}
# Productos que en Dropi SON un regalo ("PRODUCTO GRATIS" 61592, agotado):
# siempre se cambian por REGALO_PRINCIPAL, venga de donde venga el armado
# (historial aprendido incluido) y sin importar la cantidad.
REGALOS_EXPLICITOS = {61592}
# Productos que, dentro de una promo con regalo, son el regalo (no se pagan).
REGALOS_CONOCIDOS = {BATANA["id"], TURKESTERONE["id"], 175317, REGALO_PRINCIPAL["id"]}  # + Candela

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
        # Promos: 1 frasco $27.99 | 2 frascos + regalo $37.99 ($38). Si el
        # cliente quiere mas: 3 $52 | 4 $65 | 5 $80 | 6 $97 | 7+ = $15 c/u.
        # 1 y 2 frascos: productos en EMBUDOS["megared"]["alternativas"];
        # 3+ : MEGA RED PRIV (148700) x frascos + regalo.
        "precios": {1: 27.99, 2: 38.00, 3: 52.00, 4: 65.00, 5: 80.00, 6: 97.00},
        "precio_frasco_desde_7": 15.00,
        "regalo_desde": 2,
        # $37.99 (lo que anuncia el embudo) tambien es la promo de 2 + regalo;
        # se cobra lo que le dijeron al cliente (ver ajustar_al_valor).
        "precios_aceptados": {2: (37.99,)},
        # Cualquier otra cosa del CRM (otro valor, otra cantidad) -> 1 frasco.
        "si_no_cuadra": 1,
        # No usa historial ni reglas de Chatea Pro: solo estas 2 promos.
        "solo_codigo": True,
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

# Mega Red 1 frasco: Mega Red 3 (14235, proveedor 18132); si no hay stock,
# MEGA RED PRIV (148700) o MEGA RED (19257), ambos del proveedor 15819.
EMBUDOS["megared"]["alternativas"] = {
    1: [
        [_it(14235, "Mega Red 3", 27.99)],
        [_it(148700, "MEGA RED PRIV", 27.99)],
        [_it(19257, "MEGA RED", 27.99)],
    ],
    # 2 frascos + regalo = $38 / $37.99 (el regalo se sube como Te Chino, ver con_regalo).
    2: [
        [_it(148700, "MEGA RED PRIV", 15.00, 2), _it(*_BATANA_REGALO, 8.00)],
        [_it(19257, "MEGA RED", 15.00, 2), _it(*_BATANA_REGALO, 8.00)],
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
    if es_auto(clave):
        return [clave]
    if es_hist(clave):
        base, p = clave.partition("#")[0], _partes_hist(clave)
        n = len(_armados_hist(*p[:3])) if p else 0
        propias = [f"{base}#{i}" for i in range(n)] if n > 1 else [base]
        return propias + _respaldo_codigo(p)
    base = _sin_opcion(clave)[0]
    if base in SUELTOS:
        return [base]
    emb, frascos, _ = _partes(base)
    n = len(EMBUDOS.get(emb, {}).get("alternativas", {}).get(frascos, []))
    return [f"{base}#{i}" for i in range(n)] if n > 1 else [base]


def _partes(clave):
    """'megared:2' -> ('megared', 2, extra); extra=True si trae "+batana" (ya no valida)."""
    base, _, extra = _sin_opcion(clave)[0].partition("+")
    emb, _, n = base.partition(":")
    return emb, int(n) if n.isdigit() else 0, extra == "batana"


def _clave(embudo, frascos):
    return f"{embudo}:{frascos}"


def valida(clave):
    if es_auto(clave):
        return _armar_auto(clave) is not None
    if es_hist(clave):
        return _armar_hist(clave) is not None
    if _sin_opcion(clave)[0] in SUELTOS:
        return True
    emb, n, extra = _partes(clave)
    if extra or "+" in _sin_opcion(clave)[0]:
        return False  # "+batana" y similares ya no son promos
    return emb in EMBUDOS and n > 0 and _total_escalera(emb, n) is not None


def _total_escalera(embudo, frascos):
    e = EMBUDOS[embudo]
    if frascos in e["precios"]:
        return e["precios"][frascos]
    if e["precio_frasco_desde_7"] and frascos >= 7:
        return round(frascos * e["precio_frasco_desde_7"], 2)
    return None


def familia(clave):
    if es_auto(clave) or es_hist(clave):
        emb_id = (_partes_auto(clave) if es_auto(clave) else _partes_hist(clave))[0]
        return _embudo_por_id(emb_id) or f"auto{emb_id}"
    clave = _sin_opcion(clave)[0]
    if clave in SUELTOS:
        return "drenaje" if clave == "aurelys" else clave
    return _partes(clave)[0]


# ------------------------------------------------------------ armado --

def armar(clave):
    """Lista de items {id, nombre, price, quantity} para Dropi."""
    if es_auto(clave):
        items = _armar_auto(clave)
        if items is None:
            raise ValueError(f"Promo {clave} no se puede armar (revisa [Promos Dropi] Reglas).")
        return items
    if es_hist(clave):
        items = _armar_hist(clave)
        if items is None:
            raise ValueError(f"Promo {clave} ya no esta en el historial de la cuenta.")
        return items
    clave, opcion = _sin_opcion(clave)
    if clave in SUELTOS:
        s = SUELTOS[clave]
        return [{"id": s["id"], "nombre": s["nombre"], "price": s["precio"], "quantity": 1}]

    emb, frascos, batana = _partes(clave)
    e = EMBUDOS[emb]
    if frascos in e.get("combos", {}):
        return copy.deepcopy(e["combos"][frascos])
    alternativas = e.get("alternativas", {}).get(frascos)
    if alternativas:
        return copy.deepcopy(alternativas[min(opcion, len(alternativas) - 1)])
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
    return items


def _frascos_y_regalo_desde(clave):
    """(frascos de la venta, desde cuantos frascos esa promo lleva regalo | None)."""
    if es_auto(clave):
        emb, frascos, _ = _partes_auto(clave)
        r = _regla(emb)
        return frascos, (r["regalo_desde"] if r and r["regalo"] else None)
    if es_hist(clave):
        emb, frascos, _, _ = _partes_hist(clave)
        fam = _embudo_por_id(emb)
        return frascos, (EMBUDOS[fam].get("regalo_desde") if fam in EMBUDOS else 2)
    base = _sin_opcion(clave)[0]
    if base in SUELTOS:
        return 0, None
    emb, frascos, _ = _partes(base)
    return frascos, EMBUDOS.get(emb, {}).get("regalo_desde")


def lleva_regalo(clave):
    """
    UNICA regla del regalo: solo si los frascos llegan al minimo de la promo
    (Mega Red desde 2, Drenaje desde 4...). Con 1 frasco nunca hay regalo, ni
    aunque el pedido traiga otro producto.
    """
    frascos, desde = _frascos_y_regalo_desde(clave)
    return bool(desde) and frascos >= desde


def tiene_regalo(clave):
    """True si la venta lleva regalo sorpresa (promo de la semana o producto gratis)."""
    return any(es_regalo(x, clave) for x in armar(clave))


def total_promo(clave):
    return round(sum(x["price"] * x["quantity"] for x in armar(clave)), 2)


def productos_dropi(clave):
    return [{"id": x["id"], "price": x["price"], "quantity": x["quantity"]}
            for x in armar(clave)]


def descripcion(clave):
    if es_auto(clave):
        emb_id, frascos, _ = _partes_auto(clave)
        r = _regla(emb_id)
        titulo = f"{r['nombre'] if r else emb_id} — {frascos} frasco{'s' if frascos > 1 else ''}"
    elif es_hist(clave):
        emb_id, frascos, _, _ = _partes_hist(clave)
        nombre = (EMBUDOS.get(_embudo_por_id(emb_id) or "", {}).get("nombre")
                  or _nombre_producto(emb_id))
        titulo = f"{nombre} — {frascos} frasco{'s' if frascos > 1 else ''}"
    elif _sin_opcion(clave)[0] in SUELTOS:
        titulo = SUELTOS[_sin_opcion(clave)[0]]["nombre"]
    else:
        emb, frascos, _ = _partes(clave)
        titulo = f"{EMBUDOS[emb]['nombre']} — {frascos} frasco{'s' if frascos > 1 else ''}"
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

    clave = _clave(emb, frascos)
    if not valida(clave):
        avisos.append(f"{EMBUDOS[emb]['nombre']}: no hay precio configurado para {frascos} frascos.")
        return None, "", avisos
    if not cuadra_valor(clave, valor):
        return None, "", avisos  # el valor no es el de ninguna promo: se revisa a mano
    return clave, origen, avisos


def cuadra_valor(clave, valor):
    """True si el valor del CRM es el precio de esa promo (±$0.15) o no hay valor."""
    valor = _a_float(valor)
    if valor is None or abs(valor - total_promo(clave)) <= _TOLERANCIA:
        return True
    if es_auto(clave) or es_hist(clave):
        return False
    emb, frascos, _ = _partes(clave)
    return _precio_aceptado(emb, frascos, valor)


def _precio_aceptado(emb, frascos, valor):
    """True si 'valor' es uno de los precios aceptados de esa promo (ej. 2 Mega Red $37.99)."""
    vals = (EMBUDOS.get(emb, {}).get("precios_aceptados") or {}).get(frascos) or ()
    return valor is not None and any(abs(valor - v) <= 0.02 for v in vals)


def ajustar_al_valor(items, clave, valor):
    """
    Si el CRM trae un precio aceptado (2 Mega Red a $37.99), el pedido se cobra
    ese valor: la diferencia sale del precio del regalo. Si no, items igual.
    """
    valor = _a_float(valor)
    if not items or es_auto(clave) or es_hist(clave):
        return items
    emb, frascos, _ = _partes(clave)
    if not _precio_aceptado(emb, frascos, valor):
        return items
    regalos = [x for x in items if es_regalo(x, clave)]
    if not regalos:
        return items
    diff = round(valor - sum(x["price"] * x["quantity"] for x in items), 2)
    regalos[0]["price"] = round(regalos[0]["price"] + diff / regalos[0]["quantity"], 2)
    return items


def detectar_promo(productos_escogidos="", valor_compra="", texto_extra=""):
    """Compatibilidad: deteccion solo con texto + valor."""
    return detectar({"Productos escogidos": productos_escogidos,
                     "Valor de la compra": valor_compra}, texto_extra)[0]


def aviso_valor(clave, valor_compra):
    valor = _a_float(valor_compra)
    if valor is None or abs(valor - total_promo(clave)) < 0.005:
        return ""
    return f"El CRM dice ${valor:.2f} y en Dropi se sube ${total_promo(clave):.2f}."


# ------------------------------------------------- promo automatica --
#
# Para que una cuenta nueva no tenga que escribir combos aqui, la promo se
# arma con los campos de Chatea Pro:
#
#   ID de Dropi x Cantidad de productos, total = Valor de la compra
#   + regalo (upsells.1 del embudo "[Producto Ventas Wp] N")
#
# Solo aplica a los embudos que tienen entrada en el campo del bot
# "[Promos Dropi] Reglas" (JSON). Ejemplo (VitashopEc):
#   {"148700": {"producto_por_frascos": {"1": {"id": 14235, "nombre": "Mega Red 3"}},
#               "regalo_desde": 2},
#    "186883": {"frascos_por_unidad": 2, "regalo_desde": 4}}
# Claves (todas opcionales):
#   frascos_por_unidad   frascos que trae 1 unidad de Dropi (2x1 = 2). Def. 1
#   producto_por_frascos {frascos: id o {id, nombre}} producto distinto segun cantidad
#   regalo_desde         frascos desde los que va el regalo. Def. 2
#   regalo               {id, nombre, precio} en vez de upsells.1; null = sin regalo
#   max_frascos          mas frascos que esto -> aviso "revisa el armado"
# Clave de la promo: "auto:<id embudo>:<frascos>:<valor en centavos>".

_CONFIG = {"valor": None}
_VACIO = {"embudos": {}, "reglas": {}}


def usar_config(cfg):
    """Fija la configuracion (pruebas / simulacion) en vez de leer Chatea Pro."""
    _CONFIG["valor"] = cfg
    _registrar_busquedas(cfg)


def _config():
    if _CONFIG["valor"] is not None:
        return _CONFIG["valor"]
    try:
        import chateapro  # tarde: promos se usa tambien sin API
        cfg = chateapro.config_promos()
    except Exception as e:  # sin config -> solo reglas del codigo
        print(f"promos: no pude leer la config de Chatea Pro: {e}")
        return _VACIO
    _registrar_busquedas(cfg)
    return cfg


def _producto_ref(valor, defecto):
    if isinstance(valor, dict) and str(valor.get("id", "")).isdigit():
        return int(valor["id"]), valor.get("nombre") or f"#{valor['id']}"
    if str(valor or "").isdigit():
        return int(valor), f"#{valor}"
    return defecto


def _regla(emb_id, cfg=None):
    """Regla completa del embudo o None si no esta activado en Chatea Pro."""
    cfg = cfg or _config()
    if emb_id not in cfg["reglas"] or emb_id not in cfg["embudos"]:
        return None
    e, r = cfg["embudos"][emb_id], cfg["reglas"][emb_id]
    regalo = r["regalo"] if "regalo" in r else e.get("regalo")
    if isinstance(regalo, dict) and str(regalo.get("id", "")).isdigit():
        regalo = {"id": int(regalo["id"]), "nombre": regalo.get("nombre") or f"#{regalo['id']}",
                  "precio": float(regalo.get("precio") or 0)}
    else:
        regalo = None
    return {
        "nombre": e["nombre"],
        "precio": e.get("precio"),
        "frascos_por_unidad": max(1, int(r.get("frascos_por_unidad") or 1)),
        "producto_por_frascos": {int(k): v for k, v in (r.get("producto_por_frascos") or {}).items()
                                 if str(k).isdigit()},
        "regalo": regalo,
        "regalo_desde": int(r.get("regalo_desde") or 2),
        "max_frascos": int(r["max_frascos"]) if str(r.get("max_frascos", "")).isdigit() else None,
    }


def _registrar_busquedas(cfg):
    """Nombres de los productos de Chatea Pro para revisar stock en Dropi."""
    for emb_id, e in cfg.get("embudos", {}).items():
        BUSQUEDA_STOCK.setdefault(emb_id, e["nombre"])
        if e.get("regalo"):
            BUSQUEDA_STOCK.setdefault(e["regalo"]["id"], e["regalo"]["nombre"])
    for r in cfg.get("reglas", {}).values():
        for v in (r.get("producto_por_frascos") or {}).values():
            pid, nombre = _producto_ref(v, (None, None))
            if pid and not nombre.startswith("#"):
                BUSQUEDA_STOCK.setdefault(pid, nombre)
        reg = r.get("regalo")
        if isinstance(reg, dict) and str(reg.get("id", "")).isdigit() and reg.get("nombre"):
            BUSQUEDA_STOCK.setdefault(int(reg["id"]), reg["nombre"])


def es_auto(clave):
    return str(clave or "").startswith("auto:")


def _partes_auto(clave):
    """'auto:148700:2:3800' -> (148700, 2, 38.0) o None."""
    p = str(clave).split(":")
    if len(p) != 4 or not all(x.isdigit() for x in p[1:]):
        return None
    return int(p[1]), int(p[2]), int(p[3]) / 100


def _armar_auto(clave, cfg=None):
    """Items para Dropi o None si la clave no se puede armar."""
    partes = _partes_auto(clave)
    if not partes:
        return None
    emb_id, frascos, valor = partes
    r = _regla(emb_id, cfg)
    if not r or frascos <= 0 or valor <= 0 or frascos % r["frascos_por_unidad"]:
        return None
    unidades = frascos // r["frascos_por_unidad"]
    pid, nombre = _producto_ref(r["producto_por_frascos"].get(frascos), (emb_id, r["nombre"]))
    regalo = r["regalo"] if r["regalo"] and frascos >= r["regalo_desde"] else None
    resto = round(valor - (regalo["precio"] if regalo else 0), 2)
    if resto <= 0:
        return None
    precio_u = round(resto / unidades, 2)
    items = [{"id": pid, "nombre": nombre, "price": precio_u, "quantity": unidades}]
    if regalo:
        # El regalo absorbe los centavos del redondeo para que el total cuadre.
        items.append({"id": regalo["id"], "nombre": regalo["nombre"] + " (regalo)",
                      "price": round(valor - precio_u * unidades, 2), "quantity": 1})
    return items


def detectar_auto(campos):
    """
    Promo automatica desde el CRM. (clave|None, origen, avisos).
    clave None y sin avisos = el embudo no esta activado en Chatea Pro.
    """
    emb_id = _a_int(campos.get("ID de Dropi"))
    r = _regla(emb_id) if emb_id else None
    if not r:
        return None, "", []
    avisos = []
    frascos = _a_int(campos.get("Cantidad de productos"))
    valor = _a_float(campos.get("Valor de la compra"))
    if not frascos:
        return None, "", [f"{r['nombre']}: la cantidad de productos está vacía en el CRM."]
    if not valor:
        return None, "", [f"{r['nombre']}: el valor de la compra está vacío en el CRM."]
    if r["precio"] and valor > r["precio"] * frascos * 1.5 + 1:
        return None, "", [f"⚠️ {r['nombre']}: ${valor:g} por {frascos} frasco(s) no tiene sentido "
                          "(¿falta el punto decimal?). Revisa el valor en el CRM."]
    if frascos % r["frascos_por_unidad"]:
        return None, "", [f"{r['nombre']}: {frascos} frascos no es múltiplo de "
                          f"{r['frascos_por_unidad']} (cada unidad trae {r['frascos_por_unidad']})."]
    clave = f"auto:{emb_id}:{frascos}:{int(round(valor * 100))}"
    if not _armar_auto(clave):
        return None, "", [f"{r['nombre']}: ${valor:.2f} no alcanza para el regalo; revisa el valor."]
    if r["max_frascos"] and frascos > r["max_frascos"]:
        avisos.append(f"Pedido grande ({frascos} frascos): revisa el armado antes de crear.")
    fams = familias_en(campos.get("Productos escogidos", ""))
    propia = _embudo_por_id(emb_id)
    if fams and propia and propia not in fams:
        avisos.append(f"⚠️ «{campos.get('Productos escogidos')}» no parece {r['nombre']}: revisa el chat.")
    return clave, "Chatea Pro (ID de Dropi + cantidad + valor)", avisos


# ------------------------------------------ promo aprendida (historial) --
#
# historial.py aprende de los pedidos que la cuenta ya subio a Dropi. Clave:
# "hist:<id embudo>:<frascos>:<valor en centavos>[#n]"; #n = n-esimo armado
# mas visto (las alternativas se prueban en orden hasta encontrar stock).



def es_hist(clave):
    return str(clave or "").startswith("hist:")


def _partes_hist(clave):
    """'hist:148700:2:3800#1' -> (148700, 2, 38.0, 1) o None."""
    base, _, op = str(clave).partition("#")
    p = _partes_auto("auto:" + base[len("hist:"):]) if es_hist(base) else None
    return (*p, int(op) if op.isdigit() else 0) if p else None


def _armados_hist(emb, frascos, valor):
    import historial
    # Sin regalo para esa cantidad (ej. 1 Mega Red), un armado que trae
    # Batana/Candela/etc. no es una promo valida: no se aprende.
    sin_regalo = not lleva_regalo(f"hist:{emb}:{frascos}:0")
    return [(items, n) for items, n in historial.armados(emb, frascos, valor)
            if n >= historial.MIN_VISTOS
            and not (sin_regalo and any(i in REGALOS_CONOCIDOS for i, _, _ in items))]


def _nombre_producto(pid):
    import historial
    n = historial.nombre(pid)
    if n:
        BUSQUEDA_STOCK.setdefault(pid, n)  # para revisar stock en Dropi
        return n
    if pid in CATALOGO:
        return CATALOGO[pid]["nombre"]
    b = BUSQUEDA_STOCK.get(pid)
    return b if isinstance(b, str) else f"#{pid}"


def _respaldo_codigo(partes):
    """
    Alternativas de EMBUDOS para la misma venta (si lo aprendido no tiene
    stock): ej. hist de Mega Red 1 frasco -> megared:1#0..#2 (14235, 148700, 19257).
    """
    if not partes:
        return []
    emb, frascos, valor = partes[:3]
    fam = _embudo_por_id(emb)
    clave = f"{fam}:{frascos}"
    if fam not in EMBUDOS or not valida(clave) or abs(total_promo(clave) - valor) > _TOLERANCIA:
        return []
    return opciones(clave)


def _armar_hist(clave):
    partes = _partes_hist(clave)
    if not partes:
        return None
    emb, frascos, valor, op = partes
    arm = _armados_hist(emb, frascos, valor)
    if not arm:
        return None
    items, _ = arm[min(op, len(arm) - 1)]
    items = sorted(items, key=lambda x: (x[0] in REGALOS_CONOCIDOS, x[0] != emb))  # producto, luego regalo
    con_regalo_ok = lleva_regalo(clave)
    out = []
    for i, q, p in items:
        nombre = _nombre_producto(i)
        regalo = (i in REGALOS_CONOCIDOS and con_regalo_ok) or \
            es_regalo_explicito({"id": i, "nombre": nombre})
        out.append({"id": i, "nombre": nombre + (" (regalo)" if regalo else ""),
                    "price": p, "quantity": q})
    return out


def detectar_hist(campos):
    """Promo con lo que esta cuenta ya hizo en Dropi. (clave|None, origen, avisos)."""
    emb = _a_int(campos.get("ID de Dropi"))
    frascos = _a_int(campos.get("Cantidad de productos"))
    valor = _a_float(campos.get("Valor de la compra"))
    if not (emb and frascos and valor):
        return None, "", []
    arm = _armados_hist(emb, frascos, valor)
    if not arm:
        return None, "", []
    clave = f"hist:{emb}:{frascos}:{int(round(valor * 100))}"
    avisos = []
    if len(arm) > 1:
        avisos.append(f"Esta cuenta lo armó de {len(arm)} formas; se usa la más frecuente "
                      "que tenga stock.")
    fams = familias_en(campos.get("Productos escogidos", ""))
    propia = _embudo_por_id(emb)
    if fams and propia and propia not in fams:
        avisos.append(f"⚠️ «{campos.get('Productos escogidos')}» no parece el producto del "
                      "embudo: revisa el chat.")
    return clave, f"historial de la cuenta (visto {arm[0][1]}x)", avisos


def detectar_promo_venta(campos, texto_chat=""):
    """
    Lo que usa la ficha, en este orden:
      1. lo aprendido del historial de la cuenta (historial.py)
      2. la regla automatica si el embudo tiene [Promos Dropi] Reglas
      3. las reglas de este archivo (detectar)
    """
    # Embudos con promos fijas (Mega Red): solo sus promos; si el CRM dice
    # otra cosa, se arma la de por defecto (1 frasco $27.99), sin avisos.
    fam = _embudo_por_id(campos.get("ID de Dropi"))
    if fam in EMBUDOS and EMBUDOS[fam].get("solo_codigo"):
        valor = _a_float(campos.get("Valor de la compra"))
        frascos = _a_int(campos.get("Cantidad de productos"))
        for n in (EMBUDOS[fam].get("precios_aceptados") or {}):
            if _precio_aceptado(fam, n, valor) and frascos in (None, n):
                return _clave(fam, n), "ID de Dropi del CRM", []
        clave, origen, avisos = detectar(campos, texto_chat)
        if clave:
            return clave, origen, avisos
        defecto = EMBUDOS[fam].get("si_no_cuadra")
        if defecto:
            return _clave(fam, defecto), f"{EMBUDOS[fam]['nombre']}: 1 frasco por defecto", []
        return clave, origen, avisos
    try:
        clave, origen, avisos = detectar_hist(campos)
    except Exception as e:  # el historial es un extra: nunca bloquea la ficha
        print(f"promos: historial no disponible: {e}")
        clave, origen, avisos = None, "", []
    if clave:
        return clave, origen, avisos
    clave, origen, avisos = detectar_auto(campos)
    if clave or avisos:
        # Embudo activado: si los datos no sirven, se revisa a mano (sin
        # caer a las reglas del codigo, que pueden ser de otra cuenta).
        return clave, origen, avisos
    return detectar(campos, texto_chat)


# ----------------------------------------------------------- regalo --
#
# El regalo sorpresa de cualquier armado (codigo, regla o historial) se sube
# como REGALO_PRINCIPAL (Te Chino x30, proveedor 15819), al mismo precio y
# cantidad que traia el armado: asi el total no cambia y el pedido queda de un
# solo proveedor. Si no hay stock, la ficha ofrece los 3 productos mas
# economicos del proveedor (dropi_pedido.regalos_baratos) para elegir.



def es_regalo(item, clave=""):
    """True si el item es el regalo de la promo (no el producto que se paga)."""
    if es_regalo_explicito(item):
        return True  # "PRODUCTO GRATIS": siempre es regalo
    if clave and not lleva_regalo(clave):
        return False  # esa cantidad no lleva regalo: todo se paga
    if str(item.get("nombre", "")).endswith("(regalo)"):
        return True
    return item["id"] in REGALOS_CONOCIDOS


def es_regalo_explicito(item):
    """El producto mismo es un regalo en Dropi: 61592 o un nombre con 'GRATIS'."""
    nombre = _norm(str(item.get("nombre", "")).replace("(regalo)", ""))
    return item["id"] in REGALOS_EXPLICITOS or bool(re.search(r"\bgratis\b", nombre))


def con_regalo(items, regalo, clave=""):
    """
    Copia de items con el regalo cambiado por regalo={id, nombre}, y en orden:
    primero lo que pidio el cliente y el obsequio AL FINAL (Dropi muestra el
    primer producto como el principal del envio).
    """
    out = []
    for x in items:
        x = dict(x)
        if regalo and es_regalo(x, clave):
            x["id"], x["nombre"] = int(regalo["id"]), f"{regalo['nombre']} (regalo)"
        out.append(x)
    # Producto principal (el de mas valor) primero; el obsequio siempre ultimo.
    return sorted(out, key=lambda x: (es_regalo(x, clave), -x["price"] * x["quantity"]))


def texto_items(items):
    """Lineas '• 2x MEGA RED PRIV (#148700, $15.00)' de una lista de items."""
    return "\n".join(f"  • {x['quantity']}x {x['nombre']} (#{x['id']}, ${x['price']:.2f})"
                     for x in items)


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
