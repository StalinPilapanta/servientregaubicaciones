# Bot de Telegram de ventas para VitashopEc / Chatea Pro.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chateapro.py bot.py ./

# El bot usa long polling (getUpdates); no expone puertos ni necesita dominio.
CMD ["python", "bot.py"]
