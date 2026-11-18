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
# con OpenStreetMap y se elige la agencia del directorio local
# (directorio_limpio.json trae Latitud/Longitud) mas cercana a ese punto.
#
# Direcciones con cruce de calles ("Garcia Moreno y Martinez Mera 469"): se
# ubica cada calle por separado y se toma el punto donde se cruzan.
#
# Geocodificadores (OpenStreetMap): Photon tolera faltas de ortografia
# ("Martines" -> "Martinez") y nombres incompletos ("Garcia Moreno" ->
# "Gabriel Garcia Moreno"); Nominatim queda de respaldo.
#
# Solo se envia la referencia de lugar + ciudad (nunca nombre ni telefono).
#
# Variables de entorno:
#   GEOCODER            "auto" (def.: Photon y luego Nominatim), "photon",
#                       "nominatim" u "off" para desactivar
#   PHOTON_URL          def. https://photon.komoot.io/api/
#   GEOCODER_URL        def. https://nominatim.openstreetmap.org/search
#   GEOCODER_UA         User-Agent (Nominatim lo exige). Def. "ventas-bot/1.0"
#   DIRECTORIO_PATH     def. directorio_limpio.json junto al codigo

import json
import math
import re
import threading
import time
import unicodedata

GEOCODER = os.environ.get("GEOCODER", "auto").strip().lower()
PHOTON_URL = os.environ.get("PHOTON_URL", "https://photon.komoot.io/api/")
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
# Dos calles "se cruzan" si algun tramo de cada una queda a menos de esto.
_MAX_KM_CRUCE = 0.6

_dir_cache = None
_geo_cache = {}
_geo_lock = threading.Lock()
_ultimo_geo = 0.0

# Palabras que delatan una referencia de lugar en un mensaje del cliente.
_PISTAS = ("sector", "barrio", "cdla", "ciudadela", "colegio", "escuela", "parque",
           "av ", "av.", "avenida", "calle", "cerca", "frente", "junto", "mercado",
           "iglesia", "hospital", "redondel", "terminal", "universidad", "centro",
           "norte", "sur", "via ", "km", "parada", "estadio", "gasolinera",
           "supermercado", "mall", "plaza", "coliseo", "urbanizacion", "conjunto",
           "entrada", "redondel", "cancha", "hotel", "banco", "farmacia")


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
    # Solo agencias donde el cliente puede retirar (Entrega_en_oficina = SI).
    retiro = [a for a in ok if _norm(a.get("Entrega_en_oficina")) != "no"]
    return retiro or ok, centro


def _pedir(url, params):
    """GET al geocodificador respetando 1 peticion/segundo. JSON o None."""
    global _ultimo_geo
    with _geo_lock:
        espera = 1.1 - (time.time() - _ultimo_geo)
        if espera > 0:
            time.sleep(espera)
        try:
            r = requests.get(url, params=params, headers={"User-Agent": GEOCODER_UA},
                             timeout=10)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print("Geocoder fallo:", e)
            return None
        finally:
            _ultimo_geo = time.time()


def _photon(consulta, centro, cerca=None):
    lat, lon = centro
    params = {"q": consulta, "limit": 8, "lat": lat, "lon": lon,
              "bbox": f"{lon - 0.25},{lat - 0.25},{lon + 0.25},{lat + 0.25}"}
    if cerca:  # priorizar los tramos cercanos a este punto (otra calle del cruce)
        params.update(lat=cerca[0], lon=cerca[1], zoom=16, location_bias_scale=0.1)
    d = _pedir(PHOTON_URL, params)
    out = []
    for f in (d or {}).get("features") or []:
        c = (f.get("geometry") or {}).get("coordinates") or []
        if len(c) == 2:
            p = f.get("properties") or {}
            out.append((float(c[1]), float(c[0]), p.get("name") or "",
                        p.get("type") == "street"))  # no paradas de bus
    return out


def _nominatim(consulta):
    d = _pedir(GEOCODER_URL, {"q": consulta, "format": "json", "limit": 5,
                              "countrycodes": "ec"})
    return [(float(x["lat"]), float(x["lon"]), x.get("display_name", "").split(",")[0],
             x.get("class") == "highway") for x in d or []]


def _candidatos(consulta, centro):
    """
    Lugares posibles para la consulta dentro de la ciudad:
    [(lat, lon, nombre, es_calle)]. Con cache.
    """
    clave = _norm(consulta) + f"|{centro[0]:.2f},{centro[1]:.2f}"
    if clave in _geo_cache:
        return _geo_cache[clave]
    res = []
    if GEOCODER in ("auto", "photon"):
        res = _photon(consulta, centro)
    if not res and GEOCODER in ("auto", "nominatim"):
        res = _nominatim(consulta)
    res = [x for x in res if _km(centro[0], centro[1], x[0], x[1]) <= _MAX_KM_PUNTO]
    _geo_cache[clave] = res
    return res


def _geocodificar(consulta, centro=None):
    """(lat, lon, nombre_lugar) del mejor candidato, o None."""
    if centro is None:
        return None
    c = _candidatos(consulta, centro)
    return c[0][:3] if c else None


# "Calle Garcia Moreno y Martinez Mera 469" -> ["garcia moreno", "martinez mera"]
_CONECTOR_CRUCE = re.compile(r"\s(?:y|e|esq|esquina|entre|con|interseccion)\s")
_PREFIJO_CALLE = re.compile(r"^(?:calle|calles|av|avenida|avda|pasaje|psje|via|"
                            r"diagonal|direccion|dir|en|la|el|de)\s+")


def _calles_de(ref):
    """Las calles de un cruce, o [] si la referencia no es un cruce."""
    partes = _CONECTOR_CRUCE.split(f" {ref} ")
    if len(partes) < 2:
        return []
    calles = []
    for p in partes[:3]:
        p = re.sub(r"\b(?:n|no|nro|numero|s n|sn|casa|lote|mz|manzana|barrio)\b", " ", p)
        # numeros de casa fuera; los de calles-fecha ("9 de octubre") se quedan
        p = re.sub(r"\b\d+[a-z]?\b(?!\s+de\b)", " ", p)
        p = re.sub(r"\s+", " ", p).strip()
        while True:
            q = _PREFIJO_CALLE.sub("", p)
            if q == p:
                break
            p = q
        if len(p) >= 3 and not p.isdigit():
            calles.append(p)
    return calles if len(calles) >= 2 else []


def _parece_cruce(n):
    """'garcia moreno y martinez mera 4 69': dos nombres de 2+ palabras unidos por y/e."""
    calles = _calles_de(n)
    return bool(calles) and all(len(c.split()) >= 2 for c in calles[:2]) and len(n) <= 90


def _cruce(calles, ciudad, centro):
    """
    Punto donde se cruzan las calles: (lat, lon, "Calle A y Calle B") o None.
    Se busca cada calle y se toma el par de tramos mas cercano entre si.
    """
    listas = []
    for c in calles[:2]:
        cand = [x for x in _candidatos(f"{c}, {ciudad}", centro) if x[3]] \
            or _candidatos(f"{c}, {ciudad}", centro)
        if not cand:
            return None
        listas.append(cand)
    def _par_mas_cercano(xs, ys):
        mejor = None
        for a in xs:
            for b in ys:
                d = _km(a[0], a[1], b[0], b[1])
                if mejor is None or d < mejor[0]:
                    mejor = (d, a, b)
        return mejor

    mejor = _par_mas_cercano(listas[0], listas[1])
    if mejor and mejor[0] > _MAX_KM_CRUCE and GEOCODER in ("auto", "photon"):
        # Avenidas largas: el geocoder solo devuelve algunos tramos. Se busca
        # cada calle otra vez junto a la otra (maximo 2 peticiones mas).
        for i, otra in ((0, 1), (1, 0)):
            calles_i = [x for x in listas[otra] if x[3]][:1] or listas[otra][:1]
            extra = [x for x in _photon(f"{calles[i]}, {ciudad}", centro, cerca=calles_i[0][:2])
                     if x[3] and _km(centro[0], centro[1], x[0], x[1]) <= _MAX_KM_PUNTO]
            listas[i] = listas[i] + extra
            mejor = _par_mas_cercano(listas[0], listas[1])
            if mejor[0] <= _MAX_KM_CRUCE:
                break
    if not mejor or mejor[0] > _MAX_KM_CRUCE:
        return None
    _, a, b = mejor
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, f"{a[2]} y {b[2]}")


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
        if not any(p in f" {n} " or p in n for p in _PISTAS) and not _parece_cruce(n):
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
        calles = _calles_de(ref)
        punto = _cruce(calles, ciudad, centro) if calles else None
        if not punto:
            punto = _geocodificar(f"{ref}, {ciudad}", centro)
        if not punto:
            continue
        lat, lon, nombre = punto
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


def es_retiro(a):
    """True si en la agencia el cliente puede retirar (Entrega_en_oficina != NO)."""
    v = a.get("entrega_en_oficina", a.get("Entrega_en_oficina", ""))
    return _norm(v) != "no"


def de_ciudad(ciudad, solo_retiro=False):
    """
    Agencias del directorio en esa ciudad (formato limpio).
    solo_retiro=True deja fuera las que tienen Entrega_en_oficina = NO
    (no entregan paquetes al cliente: no sirven para retiro).
    """
    c = _norm(ciudad)
    lista = [_formato(a) for a in _directorio() if _norm(a.get("Ciudad")) == c]
    if solo_retiro:
        lista = [a for a in lista if es_retiro(a)]
    return sorted(lista, key=lambda a: (a.get("sector") or "", a.get("nombre") or ""))


# ----------------------------------- agencia nombrada por el cliente en el chat --
#
# Muchos clientes responden solo con el nombre de la zona, que suele ser el
# nombre de la agencia: "Guachi grande" -> AMBATO_HUACHI GRANDE. Se compara
# con una forma "fonetica" para tolerar como escriben en Ecuador
# (gua/hua, v/b, z/s/c, ll/y, h muda) y errores de 1-2 letras.

# Nombres demasiado genericos para tomarlos como eleccion del cliente.
_NOMBRES_GENERICOS = {"principal", "centro", "norte", "sur", "matriz", "oficina",
                      "agencia", "terminal", "mercado", "central"}


def _fonetica(t):
    t = f" {_norm(t)} "
    t = re.sub(r" [gh]?u(?=[aei])", " w", t)       # guachi/huachi/uachi -> wachi
    t = re.sub(r" h", " ", t)                        # h muda inicial
    t = t.replace("ll", "y").replace("v", "b").replace("z", "s")
    t = re.sub(r"c(?=[ei])", "s", t)
    t = re.sub(r"(.)\1", r"\1", t)                   # letras dobles
    return re.sub(r"\s+", " ", t).strip()


def _palabra_parecida(a, b):
    if a == b:
        return True
    if len(a) < 5 or abs(len(a) - len(b)) > 1:
        return False
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def _contiene_nombre(texto_fon, clave_fon):
    """La frase clave (ya fonetica) aparece en el texto, tolerando typos."""
    if f" {clave_fon} " in f" {texto_fon} ":
        return True
    pt, pc = texto_fon.split(), clave_fon.split()
    for i in range(len(pt) - len(pc) + 1):
        if all(_palabra_parecida(pt[i + j], pc[j]) for j in range(len(pc))):
            return True
    return False


def nombrada_en_textos(ciudad, textos):
    """
    Busca, del texto mas reciente al mas antiguo, si el cliente nombro una
    agencia de su ciudad. Devuelve (agencia_formateada, texto) o (None, None).
    Si un texto nombra varias, gana el nombre mas largo (HUACHI GRANDE > HUACHI).
    """
    lista = de_ciudad(ciudad, solo_retiro=True)
    claves = []
    for a in lista:
        clave = _fonetica(nombre_corto(a))
        if len(clave) < 4 or clave in _NOMBRES_GENERICOS:
            continue
        claves.append((clave, a))
    if not claves:
        return None, None
    for t in textos:
        if not t:
            continue
        tf = _fonetica(t)
        hallados = [(c, a) for c, a in claves if _contiene_nombre(tf, c)]
        if hallados:
            return max(hallados, key=lambda x: len(x[0]))[1], t
    return None, None


def nombre_corto(a):
    """'CUENCA_REMIGIO CRESPO PRINCIPAL' -> 'REMIGIO CRESPO PRINCIPAL'."""
    n = a.get("nombre") or ""
    return n.split("_", 1)[1] if "_" in n else n
