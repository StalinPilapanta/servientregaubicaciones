# Servicio de agencias Servientrega para Chatea Pro

Servicio HTTP que, a partir de la direccion que el cliente escribe en el chat
en vivo, devuelve la(s) oficina(s) Servientrega mas adecuada(s), con su ciudad,
provincia, direccion, horario y telefono reales.

Sirve para que el bot arme pedidos con el punto de retiro correcto y evite
errores como confundir "Suscal" con "Cuenca".

## Archivos

| Archivo | Para que sirve |
|---|---|
| `servidor.py` | App Flask con los endpoints HTTP. |
| `buscar_agencia.py` | Logica de matching por texto (ciudad, sector, calles). |
| `limpiar_directorio.py` | Genera `directorio_limpio.json` desde el crudo. |
| `directorio_limpio.json` | Directorio ya normalizado (lo usa el servicio). |
| `directorio_servientrega.json` | Directorio crudo (respaldo / fuente). |
| `Dockerfile` | Imagen para desplegar en Coolify. |
| `requirements.txt` | Dependencias Python. |

## Endpoints

- `GET /salud` -> `{"ok": true, "agencias": 738}`
- `GET /buscar?texto=<direccion>` -> JSON con `ciudad_detectada`, `resultados` y `mensaje`.
- `POST /buscar` con body `{"texto": "<direccion>"}` -> igual que el GET.

El campo `mensaje` ya viene listo para responder en el chat.

## Regenerar el directorio limpio

Si vuelves a extraer el directorio (con `listadoAgencias.py`), regenera el limpio:

```bash
python3 limpiar_directorio.py
```

## Despliegue en Coolify

Este servicio se despliega como una aplicacion Docker en Coolify.

1. **Sube el codigo a un repositorio Git** (GitHub/GitLab). Coolify despliega
   desde un repo. La carpeta a publicar es `servientrega/`.

2. En Coolify: **+ New Resource -> Application -> desde tu repositorio Git**.

3. **Build Pack:** elige **Dockerfile**.
   - Si el repo tiene varios proyectos, define el **Base Directory** como
     `/servientrega` para que use este Dockerfile.

4. **Puerto:** Coolify detecta el `EXPOSE 8000`. Si te pide "Ports Exposes",
   pon `8000`.

5. **Dominio:** asigna un dominio o subdominio (Coolify te da HTTPS con Let's
   Encrypt automaticamente). Ej: `https://servientrega.tudominio.com`.

6. **Deploy.** Cuando termine, prueba:
   ```
   https://servientrega.tudominio.com/salud
   ```
   Debe responder `{"ok": true, ...}`.

### Health check (opcional pero recomendado)

En la config de la aplicacion en Coolify, usa como health check path:
```
/salud
```

## Conectar con el flujo del bot en Chatea Pro

En el flujo donde pides la direccion de envio, agrega una accion
**External Request (Peticion externa)**:

- **Metodo:** `GET`
- **URL:**
  ```
  https://servientrega.tudominio.com/buscar?texto={{VARIABLE_CON_LA_DIRECCION}}
  ```
  Reemplaza `{{VARIABLE_CON_LA_DIRECCION}}` por la variable de Chatea Pro que
  guarda lo que escribio el cliente (ej. la respuesta de "¿A que direccion
  enviamos?").
- **Guardar respuesta:** guarda el JSON en una variable, por ejemplo
  `resp_servientrega`.
- **Responder al cliente:** muestra el campo `mensaje`:
  ```
  {{resp_servientrega.mensaje}}
  ```

Tambien puedes usar los campos individuales para armar el pedido, por ejemplo:
`{{resp_servientrega.resultados[0].nombre}}`,
`{{resp_servientrega.resultados[0].ciudad}}`,
`{{resp_servientrega.resultados[0].direccion}}`.

### Sobre las fotos

El servicio trabaja con texto. Cuando el cliente manda una **foto** de la
direccion, el bot debe pedir que la escriba:
"Para asignarte la oficina Servientrega, escribeme por favor tu ciudad y sector".
Luego esa respuesta se envia al endpoint `/buscar`.

## Prueba local rapida

```bash
pip3 install -r requirements.txt
python3 -m gunicorn --bind 0.0.0.0:8000 servidor:app
# en otra terminal:
curl "http://localhost:8000/buscar?texto=Ambato%20sector%20Ficoa"
```
