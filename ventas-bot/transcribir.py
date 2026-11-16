"""
Transcripcion de notas de voz de Telegram (Claude no recibe audio).

Usa el primer proveedor configurado:
  GROQ_API_KEY    -> Groq Whisper (whisper-large-v3-turbo), muy barato/rapido
  OPENAI_API_KEY  -> OpenAI (gpt-4o-mini-transcribe)
  TRANSCRIPCION_MODELO  cambia el modelo del proveedor elegido (opcional)
"""

import os

import requests

_PROVEEDORES = (
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/audio/transcriptions",
     "whisper-large-v3-turbo"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1/audio/transcriptions",
     "gpt-4o-mini-transcribe"),
)


def _proveedor():
    for var, url, modelo in _PROVEEDORES:
        clave = os.environ.get(var)
        if clave:
            return clave, url, os.environ.get("TRANSCRIPCION_MODELO", modelo)
    return None


def disponible():
    return _proveedor() is not None


def transcribir(contenido, nombre="audio.ogg", mime="audio/ogg"):
    """Devuelve el texto del audio. Lanza RuntimeError si no se pudo."""
    p = _proveedor()
    if p is None:
        raise RuntimeError("No hay GROQ_API_KEY ni OPENAI_API_KEY para transcribir audios.")
    clave, url, modelo = p
    r = requests.post(url, headers={"Authorization": f"Bearer {clave}"},
                      data={"model": modelo, "language": "es", "response_format": "json"},
                      files={"file": (nombre, contenido, mime)}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Transcripción falló ({r.status_code}): {r.text[:200]}")
    texto = (r.json().get("text") or "").strip()
    if not texto:
        raise RuntimeError("El audio no tenía voz reconocible.")
    return texto
