"""
Limpiador / normalizador del directorio Servientrega.

Toma  directorio_servientrega.json  (crudo, con datos sucios) y genera
      directorio_limpio.json         (listo para produccion).

Que corrige:
  - Coordenadas URL-encoded:  "-2%2C896446" -> -2.896446 ,
    "%E2%88%922.056006" (signo menos unicode) -> -2.056006
  - Separador de miles mal puesto: "-2.109.375" -> -2.109375
  - Comas decimales: "-2,896446" -> -2.896446
  - Latitud/Longitud invertidas (cuando la lat cae fuera del rango de Ecuador
    pero la lng si es una latitud valida) -> se intercambian.
  - Coordenadas 0/0, vacias, "-" o imposibles -> lat/lng quedan en null.
  - Espacios sobrantes en todos los campos de texto.

Ecuador continental + Galapagos, rango aproximado:
    Latitud   entre  +1.7  y  -5.1
    Longitud  entre -75.0  y -92.1  (Galapagos llega a ~ -92)

El objetivo NO es perder agencias: si las coordenadas no se pueden salvar, se
dejan en null pero la agencia se conserva (el buscador por texto no usa GPS).
"""

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

ENTRADA = Path(__file__).with_name("directorio_servientrega.json")
SALIDA = Path(__file__).with_name("directorio_limpio.json")

# Rangos validos para Ecuador (con margen para Galapagos).
LAT_MIN, LAT_MAX = -5.2, 1.8
LNG_MIN, LNG_MAX = -92.5, -74.9


def limpiar_texto(valor):
    if valor is None:
        return ""
    return " ".join(str(valor).split()).strip()


def a_numero(valor):
    """
    Convierte un valor de coordenada (string sucio) a float, o None si no se
    puede interpretar de forma confiable.
    """
    if valor is None:
        return None

    s = str(valor).strip()
    if s in ("", "-", "0"):
        return None

    # 1. Decodificar URL-encoding (%2C = coma, %E2%88%92 = signo menos unicode)
    s = unquote(s)

    # 2. Normalizar el signo menos unicode (U+2212) a '-'
    s = s.replace("\u2212", "-")
    s = "".join(
        "-" if unicodedata.category(c) == "Pd" else c
        for c in s
    )

    # 3. Coma decimal -> punto (ej "-2,896446")
    #    Ojo: si hay varias comas seria raro; nos quedamos con la primera.
    if "," in s and "." not in s:
        s = s.replace(",", ".", 1).replace(",", "")

    # 4. Quitar cualquier caracter que no sea digito, punto o signo.
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None

    # 5. Separador de miles mal puesto: mas de un punto -> el 1o es decimal.
    #    "-2.109.375" -> "-2.109375"
    if s.count(".") > 1:
        primero = s.index(".")
        s = s[: primero + 1] + s[primero + 1:].replace(".", "")

    try:
        return float(s)
    except ValueError:
        return None


def en_rango_lat(v):
    return v is not None and LAT_MIN <= v <= LAT_MAX


def en_rango_lng(v):
    return v is not None and LNG_MIN <= v <= LNG_MAX


def arreglar_coordenadas(lat_raw, lng_raw):
    """
    Devuelve (lat, lng, nota) ya validadas. Si no se pueden salvar -> (None, None, nota).
    """
    lat = a_numero(lat_raw)
    lng = a_numero(lng_raw)

    # Caso normal: ambas en rango.
    if en_rango_lat(lat) and en_rango_lng(lng):
        return lat, lng, "ok"

    # Invertidas: la 'lat' parece longitud y la 'lng' parece latitud.
    if en_rango_lat(lng) and en_rango_lng(lat):
        return lng, lat, "invertidas"

    # A veces la longitud viene sin el punto: "7989950" -> -79.8995 no es trivial
    # y arriesgado de adivinar, asi que la descartamos.
    return None, None, "descartada"


def main():
    with open(ENTRADA, encoding="utf-8") as f:
        agencias = json.load(f)

    limpias = []
    stats = {"ok": 0, "invertidas": 0, "descartada": 0}

    for a in agencias:
        lat, lng, nota = arreglar_coordenadas(
            a.get("Latitud"), a.get("Longitud")
        )
        stats[nota] += 1

        registro = {k: limpiar_texto(v) for k, v in a.items()
                    if k not in ("Latitud", "Longitud")}
        registro = {
            "Latitud": lat,
            "Longitud": lng,
            **registro,
        }
        limpias.append(registro)

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(limpias, f, ensure_ascii=False, indent=2)

    print(f"Agencias procesadas : {len(agencias)}")
    print(f"Coordenadas OK      : {stats['ok']}")
    print(f"Coordenadas arregladas (invertidas): {stats['invertidas']}")
    print(f"Coordenadas descartadas (null)     : {stats['descartada']}")
    print(f"Archivo generado    : {SALIDA.name}")


if __name__ == "__main__":
    main()
