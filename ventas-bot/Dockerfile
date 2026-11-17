# Bot de Telegram de ventas para VitashopEc / Chatea Pro.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY datos.py chateapro.py bot.py dropi.py dropi_mcp.py dropi_pedido.py reporte_agencia.py ficha.py ia.py promos.py agencias.py hermes.py asistente.py transcribir.py alertas.py reporte_html.py meta.py directorio_limpio.json ./

# El bot usa long polling (getUpdates); no expone puertos ni necesita dominio.
CMD ["python", "bot.py"]
