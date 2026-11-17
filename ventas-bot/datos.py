"""
Carpeta de datos persistentes del bot (tokens, registros, estado).

En Coolify monta UN volumen (ej. en /data) y pon DATA_DIR=/data: todos los
archivos de estado se guardan ahi y sobreviven a cada despliegue. Sin DATA_DIR
se guardan junto al codigo (se pierden al redesplegar).

Cada archivo se puede seguir moviendo por separado con su variable *_PATH.
"""

import os

_CODIGO = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "").strip()


def ruta(nombre):
    """Ruta del archivo de estado 'nombre' dentro de DATA_DIR (o junto al codigo)."""
    base = DATA_DIR or _CODIGO
    if DATA_DIR:
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, nombre)
