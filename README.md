# Servientrega Ubicaciones

Servicio HTTP que, a partir de la direccion que un cliente escribe en el chat
en vivo de Chatea Pro, devuelve la(s) oficina(s) Servientrega mas adecuada(s):
ciudad, provincia, direccion, horario y telefono reales.

Sirve para que el bot arme pedidos con el punto de retiro correcto y evite
confusiones (por ejemplo, distinguir "Suscal" de "Cuenca").

## Caracteristicas

- Busqueda por **texto libre** (no requiere GPS).
- Detecta la **ciudad** y afina por **sector** y **calles**.
- **Apodos/siglas**: GYE -> Guayaquil, UIO -> Quito, etc.
- Tolera **errores de escritura** (kuenca -> Cuenca, anbato -> Ambato).
- Ignora mayusculas/minusculas y tildes.
- Prioriza oficinas que **entregan en oficina**.

## Endpoints

- `GET /salud`
- `GET /buscar?texto=<direccion>`
- `POST /buscar`  body: `{"texto": "<direccion>"}`

La respuesta incluye `ciudad_detectada`, `resultados` y un `mensaje` listo para
responder en el chat.

## Uso local

```bash
pip3 install -r requirements.txt
python3 -m gunicorn --bind 0.0.0.0:8000 servidor:app
curl "http://localhost:8000/buscar?texto=Ambato%20sector%20Ficoa"
```

## Despliegue

Ver **README_DESPLIEGUE.md** para el paso a paso en Coolify y como conectarlo
al flujo del bot en Chatea Pro.

## Estructura

| Archivo | Descripcion |
|---|---|
| `servidor.py` | App Flask (endpoints HTTP). |
| `buscar_agencia.py` | Logica de matching por texto. |
| `limpiar_directorio.py` | Normaliza el directorio crudo. |
| `directorio_limpio.json` | Directorio normalizado (lo usa el servicio). |
| `directorio_servientrega.json` | Directorio crudo / fuente. |
| `listadoAgencias.py` | Extractor original del directorio Servientrega. |
| `Dockerfile` | Imagen para desplegar. |
