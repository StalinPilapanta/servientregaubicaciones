"""
Buscador de agencias Servientrega por TEXTO.

Pensado para el chat en vivo de Chatea Pro: el cliente escribe su direccion en
texto libre (ciudad, sector, calles) y esta logica devuelve las agencias
Servientrega mas adecuadas, priorizando las que entregan en oficina.

Estrategia de matching (de mayor a menor peso):
  1. Ciudad   -> es lo mas confiable y reduce el universo de ~1000 a unas pocas.
  2. Sector   -> norte/sur/centro/nombre de barrio.
  3. Palabras de la Direccion y del Nombre de la agencia (calles, referencias).

No usa coordenadas: los clientes casi nunca comparten GPS. Aun asi, el JSON
tiene coordenadas sucias; aqui NO se usan, se trabaja solo con texto.

Uso rapido por linea de comandos:
    python3 buscar_agencia.py "estoy en Ambato, sector Ficoa"
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

# Usa el directorio limpio si existe; si no, cae al crudo.
_LIMPIO = Path(__file__).with_name("directorio_limpio.json")
_CRUDO = Path(__file__).with_name("directorio_servientrega.json")
DIRECTORIO = _LIMPIO if _LIMPIO.exists() else _CRUDO

# Palabras que no aportan al matching (se ignoran al comparar).
STOPWORDS = {
    "de", "la", "el", "los", "las", "y", "en", "por", "del", "al", "un", "una",
    "mi", "me", "estoy", "vivo", "queda", "esta", "sector", "barrio", "calle",
    "avenida", "av", "cdla", "ciudadela", "urb", "urbanizacion", "frente",
    "cerca", "junto", "diagonal", "a", "para", "casa", "direccion", "ref",
    "referencia", "entre", "e", "con",
    # Palabras conversacionales frecuentes que se parecen a nombres de ciudad
    # (quiero~quero, buenas~buena fe) y causarian falsos positivos.
    "hola", "buenas", "buenos", "dias", "tardes", "noches", "gracias",
    "quiero", "quisiera", "necesito", "enviar", "envio", "pedido", "favor",
    "frascos", "frasco", "producto", "productos", "unidad", "unidades",
}

# Apodos / alias -> ciudad real (en el directorio).
# La clave se compara ya normalizada (minusculas, sin tildes).
ALIAS_CIUDAD = {
    # Siglas de aeropuerto / uso comun
    "gye": "guayaquil",
    "uio": "quito",
    "sd": "santo domingo",
    "sto domingo": "santo domingo",
    "stodomingo": "santo domingo",
    "the": "tena",
    "mnt": "manta",
    "pvo": "portoviejo",
    "ptv": "portoviejo",
    "atf": "ambato",
    # Formas cortas / coloquiales
    "guayas": "guayaquil",
    "capital": "quito",
    "riobamba chimborazo": "riobamba",
    "sto dgo": "santo domingo",
    "la libertad santa elena": "la libertad",
    # Errores frecuentes tipicos
    "guayaqui": "guayaquil",
    "guyaquil": "guayaquil",
    "guayakil": "guayaquil",
    "kito": "quito",
    "cuemca": "cuenca",
    "kuenca": "cuenca",
    "anbato": "ambato",
    "manavi": "manta",
    "machalla": "machala",
    "loha": "loja",
    "ryobamba": "riobamba",
}


def normalizar(texto):
    """Minusculas, sin tildes, sin signos. Para comparar de forma robusta."""
    if not texto:
        return ""
    texto = str(texto).lower()
    # Quitar tildes / diacriticos
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    # Dejar solo letras, numeros y espacios
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def tokens(texto):
    """Palabras utiles (sin stopwords, sin palabras de 1 letra)."""
    return {
        p for p in normalizar(texto).split()
        if p not in STOPWORDS and len(p) > 1
    }


def cargar_directorio(ruta=DIRECTORIO):
    with open(ruta, encoding="utf-8") as f:
        agencias = json.load(f)
    # Pre-normalizamos los campos que vamos a comparar para no repetir trabajo.
    for a in agencias:
        a["_ciudad_norm"] = normalizar(a.get("Ciudad", ""))
        a["_sector_norm"] = normalizar(a.get("Sector", ""))
        a["_texto_norm"] = normalizar(
            f'{a.get("Nombre_agencia", "")} {a.get("Direccion", "")} '
            f'{a.get("Sector", "")}'
        )
        a["_tokens"] = tokens(
            f'{a.get("Nombre_agencia", "")} {a.get("Direccion", "")}'
        )
    return agencias


def ciudades_disponibles(agencias):
    return sorted({a["_ciudad_norm"] for a in agencias if a["_ciudad_norm"]})


def detectar_ciudad(texto_norm, ciudades):
    """
    Busca en el texto del cliente el nombre de una ciudad del directorio.

    Orden de intentos (de mas confiable a menos):
      1. Alias/apodos (gye, uio, kuenca, guayaqui...) como frase o palabra.
      2. Coincidencia exacta del nombre de la ciudad como frase.
      3. Fuzzy: palabra del cliente muy parecida a una ciudad (typos).

    Devuelve la ciudad normalizada, o None si no hay confianza suficiente.
    """
    conjunto_ciudades = set(ciudades)

    # --- 1. Alias / apodos ---
    # Los alias compuestos ("sto domingo") se buscan como frase; los simples
    # ("gye") como palabra completa.
    for alias, ciudad_real in ALIAS_CIUDAD.items():
        if ciudad_real not in conjunto_ciudades:
            continue
        if " " in alias:
            if alias in texto_norm:
                return ciudad_real
        elif re.search(rf"\b{re.escape(alias)}\b", texto_norm):
            return ciudad_real

    # --- 2. Coincidencia exacta como frase ---
    encontradas = [
        ciudad for ciudad in ciudades
        if re.search(rf"\b{re.escape(ciudad)}\b", texto_norm)
    ]
    if encontradas:
        # Preferimos el match mas largo (ej. "santa elena" sobre "santa").
        return max(encontradas, key=len)

    # --- 2b. Coincidencia por nombre base (sin sufijo entre parentesis) ---
    # Muchas ciudades vienen como "salinas (santa elena)" -> normalizado
    # "salinas santa elena". Si el cliente escribe solo "salinas", la
    # reconocemos por su primera palabra distintiva.
    for ciudad in ciudades:
        base = ciudad.split(" ")[0]
        if len(base) >= 4 and re.search(rf"\b{re.escape(base)}\b", texto_norm):
            return ciudad

    # --- 3. Fuzzy matching (tolera errores de escritura) ---
    return _fuzzy_ciudad(texto_norm, ciudades)


def _fuzzy_ciudad(texto_norm, ciudades, umbral=0.85):
    """
    Compara cada palabra 'larga' del mensaje contra los nombres de ciudad
    usando similitud de cadenas. Devuelve la mejor ciudad si supera el umbral.

    umbral 0.85 + diferencia de longitud <= 1 -> tolera 1-2 letras cambiadas
    (kuenca->cuenca, anbato->ambato, guayaqui->guayaquil) SIN confundir
    ciudades distintas de tamano parecido (salinas != quero, balzar != balsas
    se resuelve antes por coincidencia exacta).
    """
    from difflib import SequenceMatcher

    # Solo ciudades de una palabra para el fuzzy simple (las compuestas ya se
    # intentaron como frase exacta arriba). Comparamos contra su nombre base.
    bases = {c.split(" ")[0]: c for c in ciudades}

    # Ignoramos stopwords (hola, quiero, buenas...) para no confundirlas con
    # ciudades cortas de nombre parecido.
    palabras = [
        p for p in texto_norm.split()
        if len(p) >= 4 and p not in STOPWORDS
    ]

    mejor_ciudad = None
    mejor_ratio = umbral
    for palabra in palabras:
        for base, ciudad in bases.items():
            # Descartamos rapido si la diferencia de longitud es notable:
            # los typos reales cambian pocas letras, no la longitud.
            if abs(len(palabra) - len(base)) > 1:
                continue
            ratio = SequenceMatcher(None, palabra, base).ratio()
            if ratio > mejor_ratio:
                mejor_ratio = ratio
                mejor_ciudad = ciudad

    return mejor_ciudad


def puntuar(agencia, texto_norm, toks_cliente):
    """
    Devuelve un puntaje de que tan bien encaja la agencia con el mensaje.
    Mientras mas alto, mejor.
    """
    score = 0

    # 1. Coincidencia de sector (norte, sur, ficoa, solanda, etc.)
    sector = agencia["_sector_norm"]
    if sector and sector in texto_norm:
        score += 25

    # 2. Palabras compartidas entre el mensaje y (nombre + direccion) agencia.
    comunes = toks_cliente & agencia["_tokens"]
    score += 10 * len(comunes)

    # 3. Bonus si alguna palabra del cliente aparece como frase en el texto.
    for t in toks_cliente:
        if len(t) > 3 and t in agencia["_texto_norm"]:
            score += 3

    # 4. Preferir agencias que entregan en oficina.
    if str(agencia.get("Entrega_en_oficina", "")).strip().upper() == "SI":
        score += 5

    return score


def formato_agencia(a):
    """Version limpia para devolver / mostrar (sin campos internos)."""
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
        "email": a.get("Email", ""),
    }


def buscar(texto_cliente, agencias=None, limite=3):
    """
    Punto de entrada principal.

    Devuelve un dict con:
      - ciudad_detectada
      - resultados: lista de agencias (mejores primero)
      - mensaje: texto listo para responder en el chat
    """
    if agencias is None:
        agencias = cargar_directorio()

    texto_norm = normalizar(texto_cliente)
    toks_cliente = tokens(texto_cliente)
    ciudades = ciudades_disponibles(agencias)

    ciudad = detectar_ciudad(texto_norm, ciudades)

    if ciudad:
        candidatas = [a for a in agencias if a["_ciudad_norm"] == ciudad]
    else:
        # Sin ciudad clara: buscamos en todo el directorio, pero solo tendra
        # sentido si el texto trae calles/sectores muy especificos.
        candidatas = agencias

    # Umbral minimo de puntaje para considerar una agencia "relevante".
    # Sin ciudad exigimos mas evidencia (calles/sector), para no adivinar.
    if ciudad:
        umbral = 21  # basta con estar en la ciudad (bonus 20) + algo mas
        bonus_ciudad = 20
    else:
        umbral = 20  # requiere coincidencias reales de sector/calles
        bonus_ciudad = 0

    puntuadas = []
    for a in candidatas:
        s = puntuar(a, texto_norm, toks_cliente) + bonus_ciudad
        if s >= umbral:
            puntuadas.append((s, a))

    puntuadas.sort(key=lambda x: x[0], reverse=True)
    mejores = [formato_agencia(a) for _, a in puntuadas[:limite]]

    return {
        "ciudad_detectada": ciudad,
        "total_candidatas": len(candidatas) if ciudad else 0,
        "resultados": mejores,
        "mensaje": _armar_mensaje(ciudad, mejores),
    }


def _armar_mensaje(ciudad, resultados):
    if not resultados:
        if ciudad:
            return (f"Encontre tu ciudad ({ciudad.title()}) pero necesito el "
                    f"sector o las calles para ubicar la oficina mas cercana.")
        return ("No pude identificar la ciudad. Por favor escribeme tu ciudad "
                "y el sector (ej: 'Ambato, sector Ficoa').")

    lineas = ["Estas son las oficinas Servientrega mas cercanas:\n"]
    for i, r in enumerate(resultados, 1):
        oficina = "entrega en oficina" if str(
            r["entrega_en_oficina"]).upper() == "SI" else "NO entrega en oficina"
        lineas.append(
            f"{i}. {r['nombre']}\n"
            f"   Direccion: {r['direccion']}\n"
            f"   Sector: {r['sector']} | Tel: {r['telefono']}\n"
            f"   Horario L-V: {r['horario_lun_vie']} ({oficina})"
        )
    return "\n".join(lineas)


if __name__ == "__main__":
    consulta = " ".join(sys.argv[1:]) or "estoy en Ambato, sector Ficoa"
    print(f"Consulta: {consulta}\n")
    salida = buscar(consulta)
    print(f"Ciudad detectada: {salida['ciudad_detectada']}")
    print(f"Candidatas en la ciudad: {salida['total_candidatas']}\n")
    print(salida["mensaje"])
