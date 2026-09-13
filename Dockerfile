# Servicio de busqueda de agencias Servientrega para el chat en vivo de Chatea Pro.
FROM python:3.12-slim

# No generar .pyc y salida sin buffer (logs en vivo).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl: necesario para el health check que Coolify ejecuta dentro del contenedor.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias primero (mejor cache de capas).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el codigo y los datos.
COPY buscar_agencia.py servidor.py ./
COPY directorio_limpio.json ./
# Se incluye el crudo como respaldo por si el limpio no estuviera.
COPY directorio_servientrega.json ./

# Puerto interno del contenedor.
EXPOSE 8000

# Health check propio del contenedor (usa curl instalado arriba).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/salud || exit 1

# Gunicorn sirve la app Flask "app" definida en servidor.py.
# 2 workers es suficiente para este servicio liviano.
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "30", "servidor:app"]
