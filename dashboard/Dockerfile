# Dashboard de operacion VitashopEc (Streamlit, solo lectura).
# Contexto de build: raiz del repo (Base Directory = / en Coolify,
# Dockerfile en dashboard/Dockerfile).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY dashboard/requirements.txt ./dashboard-requirements.txt
RUN pip install --no-cache-dir -r dashboard-requirements.txt

COPY ventas-bot/chateapro.py ventas-bot/dropi.py ./shared/
COPY dashboard/app.py ./

EXPOSE 8501

# Streamlit expone /_stcore/health para el health check de Coolify.
CMD ["streamlit", "run", "app.py", "--server.port=8501", \
     "--server.address=0.0.0.0", "--server.headless=true"]
