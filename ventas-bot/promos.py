"""
Promos de VitashopEc y su mapeo a productos de Dropi.

Cada promo define los productos (id de Dropi), el precio de venta unitario y la
cantidad que se envian en la orden. Los precios son fijos por promo, asi que
basta con saber que promo pidio el cliente para armar la orden completa.

Ejemplos reales ya creados en Dropi:
  #7826649  promo 4 drenajes -> 196169 + 193483 + 86248 = $38.00
  #7826125  promo 2x1        -> 186883                  = $27.99
"""

import re

PROMOS = {
    "4_drenajes": {
        "nombre": "Promo 4 drenajes",
        "productos": [
            {"id": 196169, "nombre": "Drenaje Linfatico 30ml X3 Unidades",
             "price": 21.00, "quantity": 1},
            {"id": 193483, "nombre": "Drenaje Linfatico 30 Ml",
             "price": 9.00, "quantity": 1},
            {"id": 86248, "nombre": "Aceite Batana Oil",
             "price": 8.00, "quantity": 1},
        ],
        # Palabras que delatan la promo en 'Productos escogidos' o en el chat.
        "palabras": ("4 drenaje", "cuatro drenaje", "x3", "batana"),
        "total_esperado": 38.00,
    },
    "2x1": {
        "nombre": "Promo 2x1 (publicidad)",
        "productos": [
            {"id": 186883, "nombre": "Promocion Drenaje Linfatico 30 Ml 2x1",
             "price": 27.99, "quantity": 1},
        ],
        "palabras": ("2x1", "2 x 1", "dos por uno", "2 drenaje", "dos drenaje"),
        "total_esperado": 27.99,
    },
}

# Tolerancia al comparar el valor de la compra con el total de cada promo.
_TOLERANCIA = 0.15


def total_promo(clave):
    """Suma precio*cantidad de los productos de la promo."""
    p = PROMOS[clave]
    return round(sum(x["price"] * x["quantity"] for x in p["productos"]), 2)


def _a_float(valor):
    m = re.search(r"\d+(?:[.,]\d+)?", str(valor or ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", "."))
    except ValueError:
        return None


def detectar_promo(productos_escogidos="", valor_compra="", texto_extra=""):
    """
    Intenta identificar la promo SIN IA, con dos senales:
      1. El valor de la compra coincide con el total de una promo (la mas fiable).
      2. Palabras clave en 'Productos escogidos' / texto del chat.

    Devuelve la clave de la promo, o None si no hay certeza (entonces la IA
    desempata o se avisa para revision manual).
    """
    valor = _a_float(valor_compra)
    if valor is not None:
        for clave in PROMOS:
            if abs(valor - total_promo(clave)) <= _TOLERANCIA:
                return clave

    texto = f"{productos_escogidos} {texto_extra}".lower()
    puntos = {clave: 0 for clave in PROMOS}
    for clave, p in PROMOS.items():
        for palabra in p["palabras"]:
            if palabra in texto:
                puntos[clave] += 1
    mejor = max(puntos, key=puntos.get)
    if puntos[mejor] > 0 and list(puntos.values()).count(puntos[mejor]) == 1:
        return mejor
    return None


def productos_dropi(clave):
    """Lista de productos en el formato que espera create_order de Dropi."""
    return [
        {"id": x["id"], "price": x["price"], "quantity": x["quantity"]}
        for x in PROMOS[clave]["productos"]
    ]


def descripcion(clave):
    """Texto legible de la promo para la ficha de Telegram."""
    p = PROMOS[clave]
    lineas = [f"{p['nombre']} — ${total_promo(clave):.2f}"]
    for x in p["productos"]:
        lineas.append(f"  • {x['quantity']}x {x['nombre']} (${x['price']:.2f})")
    return "\n".join(lineas)
