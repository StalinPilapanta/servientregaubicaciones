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
