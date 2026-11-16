"""
Cliente del servicio de agencias Servientrega (servidor.py del repo servientrega).

Llama a  GET {AGENCIAS_API_URL}/buscar?texto=<ubicacion del cliente>
y traduce la respuesta del buscador a un estado simple para la ficha:

  estado         significado
  -------------  ---------------------------------------------------------
  "unica"        la ciudad tiene una sola agencia -> asignada con certeza
  "probable"     ciudad + sector: hay una agencia sugerida y alternativas
  "falta_sector" se detecto la ciudad pero hay varias agencias, falta sector
  "sin_ciudad"   no se detecto ciudad (revisar el chat)
  "error"        el servicio no respondio

Variables de entorno:
  AGENCIAS_API_URL   base del servicio (def. https://servientrega.neolabsgroup.io)
  AGENCIAS_TOKEN     token opcional (se envia como Bearer si existe)
"""

import os

import requests

BASE_URL = os.environ.get(
    "AGENCIAS_API_URL", "https://servientrega.neolabsgroup.io"
).rstrip("/")
TOKEN = os.environ.get("AGENCIAS_TOKEN", "").strip()


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def buscar_agencia(texto, limite=3):
    """
    Consulta el servicio con el texto de ubicacion del cliente.

    Devuelve un dict:
      estado, ciudad, sugerida (dict o None), alternativas (lista),
      total_ciudad, mensaje
    """
    texto = (texto or "").strip()
    if not texto:
        return _resultado("sin_ciudad", mensaje="No hay texto de ubicacion.")

    try:
        r = requests.get(f"{BASE_URL}/buscar", params={"texto": texto},
                         headers=_headers(), timeout=15)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        return _resultado("error", mensaje=f"Servicio de agencias no disponible: {e}")

    ciudad = d.get("ciudad_detectada")
    modo = d.get("modo")
    resultados = d.get("resultados") or []
    total = d.get("total_ciudad", 0)

    if not ciudad or not resultados:
        return _resultado("sin_ciudad", ciudad=ciudad, total=total,
                          mensaje=d.get("mensaje", ""))

    if modo == "unica":
        return _resultado("unica", ciudad, resultados[0], [], total,
                          d.get("mensaje", ""))

    if modo == "ciudad_sector":
        return _resultado("probable", ciudad, resultados[0],
                          resultados[1:limite], total, d.get("mensaje", ""))

    # "solo_ciudad": hay varias agencias y el cliente no dio sector.
    return _resultado("falta_sector", ciudad, None, resultados[:limite], total,
                      d.get("mensaje", ""))


def _resultado(estado, ciudad=None, sugerida=None, alternativas=None,
               total=0, mensaje=""):
    return {
        "estado": estado,
        "ciudad": ciudad,
        "sugerida": sugerida,
        "alternativas": alternativas or [],
        "total_ciudad": total,
        "mensaje": mensaje,
    }


def formato_agencia(a):
    """Una agencia en una linea para Telegram."""
    return f"{a.get('nombre', '-')} — {a.get('direccion', '-')}"


def direccion_dropi(a):
    """
    Direccion de retiro en el formato que Dropi/Servientrega usa en sus guias:
      CIUDAD_AGENCIA---SECTOR---DIRECCION
    Sirve como campo 'dir' al crear la orden cuando el envio es a agencia.
    """
    return f"{a.get('nombre', '')}---{a.get('sector', '')}---{a.get('direccion', '')}"


# ------------------------------------------------- agencia mas cercana (geo) --
#
# Cuando el buscador no reconoce el sector ("la salle", "colegio X", "parque Y")
# y la ciudad tiene varias agencias, se geocodifica la referencia del cliente
# con OpenStreetMap (Nominatim) y se elige la agencia del directorio local
# (directorio_limpio.json trae Latitud/Longitud) mas cercana a ese punto.
#
# Solo se envia la referencia de lugar + ciudad (nunca nombre ni telefono).
#
# Variables de entorno:
#   GEOCODER            "nominatim" (def.) u "off" para desactivar
#   GEOCODER_URL        def. https://nominatim.openstreetmap.org/search
#   GEOCODER_UA         User-Agent (Nominatim lo exige). Def. "ventas-bot/1.0"
#   DIRECTORIO_PATH     def. directorio_limpio.json junto al codigo

import json
import math
import re
import threading
import time
import unicodedata

GEOCODER = os.environ.get("GEOCODER", "nominatim").strip().lower()
GEOCODER_URL = os.environ.get(
    "GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
GEOCODER_UA = os.environ.get("GEOCODER_UA", "ventas-bot/1.0")
DIRECTORIO_PATH = os.environ.get(
    "DIRECTORIO_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "directorio_limpio.json"),
)

# Agencias cuyo punto queda a mas de esto del centro de su ciudad se ignoran
# (hay coordenadas erroneas en el directorio, ej. longitud -87).
_MAX_KM_CENTRO = 25
# Un punto geocodificado debe caer a menos de esto del centro de la ciudad.
_MAX_KM_PUNTO = 20

_dir_cache = None
_geo_cache = {}
_geo_lock = threading.Lock()
_ultimo_geo = 0.0

# Palabras que delatan una referencia de lugar en un mensaje del cliente.
_PISTAS = ("sector", "barrio", "cdla", "ciudadela", "colegio", "escuela", "parque",
           "av ", "av.", "avenida", "calle", "cerca", "frente", "junto", "mercado",
           "iglesia", "hospital", "redondel", "terminal", "universidad", "centro",
           "norte", "sur", "via ", "km")


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def _km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _directorio():
    global _dir_cache
    if _dir_cache is None:
        try:
            with open(DIRECTORIO_PATH, "r", encoding="utf-8") as f:
                _dir_cache = json.load(f)
        except (OSError, ValueError) as e:
            print("No se pudo leer el directorio de agencias:", e)
            _dir_cache = []
    return _dir_cache


def _formato(a):
    return {
        "nombre": a.get("Nombre_agencia", ""),
        "provincia": a.get("Provincia", ""),
        "ciudad": a.get("Ciudad", ""),
        "sector": a.get("Sector", ""),
        "direccion": a.get("Direccion", ""),
        "telefono": a.get("Telefono", ""),
        "entrega_en_oficina": a.get("Entrega_en_oficina", ""),
        "horario_lun_vie": a.get("Horario_Lunes_Viernes", ""),
        "horario_fin_semana": a.get("Horario_fin_semana", ""),
    }


def _agencias_ciudad(ciudad):
    """Agencias de la ciudad con coordenadas validas y el centro de la ciudad."""
    c = _norm(ciudad)
    lista = [a for a in _directorio() if _norm(a.get("Ciudad")) == c
             and isinstance(a.get("Latitud"), (int, float))
             and isinstance(a.get("Longitud"), (int, float))]
    if not lista:
        return [], None
    lats = sorted(a["Latitud"] for a in lista)
    lons = sorted(a["Longitud"] for a in lista)
    centro = (lats[len(lats) // 2], lons[len(lons) // 2])  # mediana: robusta
    ok = [a for a in lista
          if _km(centro[0], centro[1], a["Latitud"], a["Longitud"]) <= _MAX_KM_CENTRO]
    return ok, centro


def _geocodificar(consulta):
    """(lat, lon, nombre_lugar) o None. Respeta 1 peticion/segundo."""
    global _ultimo_geo
    clave = _norm(consulta)
    if clave in _geo_cache:
        return _geo_cache[clave]
    with _geo_lock:
        espera = 1.1 - (time.time() - _ultimo_geo)
        if espera > 0:
            time.sleep(espera)
        try:
            r = requests.get(GEOCODER_URL, params={
                "q": consulta, "format": "json", "limit": 1, "countrycodes": "ec",
            }, headers={"User-Agent": GEOCODER_UA}, timeout=10)
            r.raise_for_status()
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            print("Geocoder fallo:", e)
            return None
        finally:
            _ultimo_geo = time.time()
    res = None
    if d:
        res = (float(d[0]["lat"]), float(d[0]["lon"]),
               d[0].get("display_name", "").split(",")[0])
    _geo_cache[clave] = res
    return res


def referencias_de_lugar(textos, ciudad, provincia="", maximo=3, excluir=""):
    """
    Saca de los textos (IA, CRM, mensajes del cliente) las referencias de lugar
    sin la ciudad/provincia, listas para geocodificar. Mas recientes primero.
    """
    quitar = {w for w in _norm(f"{ciudad} {provincia}").split() if len(w) > 2}
    # Privacidad: palabras del nombre del cliente nunca salen al geocodificador.
    nombre = {w for w in _norm(excluir).split() if len(w) > 1}
    quitar |= {"sector", "barrio", "ecuador", "ciudad", "provincia", "por", "favor", "retiro", "retirar",
               "agencia", "servientrega", "ok", "si", "gracias"}
    vistos, refs = set(), []
    for t in textos:
        if not t:
            continue
        crudo = str(t)
        if len(crudo) > 160:
            continue
        n = _norm(crudo)
        if not any(p in f" {n} " or p in n for p in _PISTAS):
            continue
        if nombre and len(nombre & set(n.split())) >= 2:
            # El mensaje trae el nombre: solo se usa la parte desde la pista de lugar.
            m = re.search(r"\b(" + "|".join(re.escape(p.strip()) for p in _PISTAS) + r")", n)
            n = n[m.start():] if m else ""
        palabras = [w for w in n.split() if w not in quitar and w not in nombre]
        while palabras and palabras[0] in ("en", "el", "la", "de", "del", "y", "a"):
            palabras.pop(0)
        if not palabras:
            continue
        ref = " ".join(palabras)
        if ref in vistos or len(ref) < 3:
            continue
        vistos.add(ref)
        refs.append(ref)
        if len(refs) >= maximo:
            break
    return refs


def cercanas_por_referencia(ciudad, textos, provincia="", limite=3, nombre_cliente=""):
    """
    Geocodifica las referencias del cliente y devuelve las agencias mas
    cercanas, o None si no se pudo. Resultado:
      {"punto": "Colegio Hermano Miguel La Salle", "referencia": "colegio la salle",
       "agencias": [ {..agencia.., "distancia_km": 0.6}, ... ]}
    """
    if GEOCODER == "off" or not ciudad:
        return None
    agencias_c, centro = _agencias_ciudad(ciudad)
    if not agencias_c:
        return None
    for ref in referencias_de_lugar(textos, ciudad, provincia, excluir=nombre_cliente):
        punto = _geocodificar(f"{ref}, {ciudad}, Ecuador")
        if not punto:
            continue
        lat, lon, nombre = punto
        if _km(centro[0], centro[1], lat, lon) > _MAX_KM_PUNTO:
            continue  # el geocoder encontro otro lugar homonimo
        orden = sorted(agencias_c,
                       key=lambda a: _km(lat, lon, a["Latitud"], a["Longitud"]))
        out = []
        for a in orden[:limite]:
            f = _formato(a)
            f["distancia_km"] = round(_km(lat, lon, a["Latitud"], a["Longitud"]), 1)
            out.append(f)
        return {"punto": nombre, "referencia": ref, "agencias": out}
    return None


def desde_direccion_dropi(direccion):
    """
    Si la Direccion del CRM ya viene en formato Dropi
    (CIUDAD_AGENCIA---SECTOR---DIRECCION, la pone logistica a mano) devuelve
    el resultado con esa agencia del directorio; si no, None.
    """
    d = str(direccion or "")
    if "---" not in d:
        return None
    nombre = _norm(d.split("---")[0])
    for a in _directorio():
        if _norm(a.get("Nombre_agencia")) == nombre:
            return _resultado("crm", a.get("Ciudad"), _formato(a), [], 0,
                              "Agencia tomada de la dirección del CRM.")
    return None


def de_ciudad(ciudad):
    """Todas las agencias del directorio en esa ciudad (formato limpio)."""
    c = _norm(ciudad)
    lista = [_formato(a) for a in _directorio() if _norm(a.get("Ciudad")) == c]
    return sorted(lista, key=lambda a: (a.get("sector") or "", a.get("nombre") or ""))


def nombre_corto(a):
    """'CUENCA_REMIGIO CRESPO PRINCIPAL' -> 'REMIGIO CRESPO PRINCIPAL'."""
    n = a.get("nombre") or ""
    return n.split("_", 1)[1] if "_" in n else n
