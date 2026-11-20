# Bot de Ventas Telegram — VitashopEc

Bot de Telegram que reporta las ventas de Chatea Pro (VitashopEc). Lee los datos
desde los campos de usuario de los suscriptores (el modulo de ordenes de shop
esta vacio en esta cuenta; las ventas viven en el chat en vivo).

Ademas integra **Dropi**: cada pedido aparece en Chatea Pro como un campo
`[Dropi] Datos de la orden <referencia>` cuyo valor es el JSON completo de la
orden de Dropi. El bot detecta las guias con estatus **"PARA RETIRO EN AGENCIA
SERVIENTREGA"** y lleva su seguimiento (dias sin retirar).

## Asistente (texto libre y audios)

Escríbele al bot sin "/" o mándale una nota de voz: «¿qué pedidos siguen
pendientes en Dropi?», «busca a Hugo», «¿cuánto stock queda de Mega Red?»,
«¿cuánto cuesta el envío a Macas?», «¿qué agencias hay en Babahoyo?».
Lo responde Claude (`asistente.py`, modelo `claude-haiku-5-5`) consultando
Chatea Pro, Dropi y el directorio de agencias. Recuerda la conversación 30 min;
`/nuevo` la reinicia.

Solo consulta. Si le pides crear un pedido muestra la ficha con sus botones; si
le pides escribirle a un cliente muestra el borrador con «Enviar por WhatsApp» /
«Descartar». Nada sale sin que pulses el botón.

Las notas de voz se transcriben con Groq o OpenAI (`transcribir.py`), porque
Claude no recibe audio, y el texto pasa al asistente.

## Riesgo del cliente, novedades y recordatorios

- **Riesgo del cliente:** cada ficha muestra el historial del comprador en Dropi
  (`get_customer_fingerprint_profile`): tipo, etiqueta, % de entrega y % de
  devolución. Si devolvió `RIESGO_DEVOLUCION_MIN`% o más (def. 30) sale
  «⚠️ CLIENTE CON DEVOLUCIONES» arriba. No bloquea: solo avisa.
- **Novedades y devoluciones (diario a `HORA_ALERTAS`, def. 09:00, y `/novedades`):**
  pedidos en NOVEDAD / DEVOLUCION de los últimos 30 días. El aviso diario solo
  trae los nuevos. Cada novedad llega con el botón «📲 Enviar plantilla de
  novedad» (`novedad_recordatorio_v2_utilidad`) y se adjunta un HTML.
- **Recordatorio de retiro (DESACTIVADO por defecto, `RECORDATORIO_MODO=off`):** al pedido que lleva
  `RECORDATORIO_DIAS` días (def. 2) en «PARA RETIRO EN AGENCIA SERVIENTREGA» se le
  envía la plantilla `seguimiento_recordatorio_en_oficina_v2_utilidad_new`
  (nombre, producto, agencia, ciudad). Una sola vez por pedido. Los días se
  cuentan desde que el bot ve el pedido en agencia, así que tras el primer
  deploy los primeros recordatorios salen 2 días después.
  `RECORDATORIO_MODO`: `off` (def.), `auto` (envía solo) o `confirmar` (botón).
  `/recordatorios` siempre pide confirmación.
- `/probar_plantilla <tu celular>` envía la plantilla de retiro con datos de
  ejemplo a tu número, para comprobar el formato antes de que salga a clientes.

## Meta Ads: «Conectar con Facebook»

`/meta_conectar` manda un botón «🔵 Conectar con Facebook». Inicias sesión,
aceptas el permiso `ads_read` (ver rendimiento de anuncios) y listo: el bot
guarda un acceso de ~60 días y tus cuentas publicitarias. 7 días antes de que
venza, la alerta diaria te vuelve a mandar el botón. Luego:

- `/meta [hoy|ayer|7d|mes|mes_pasado|dd/mm/aaaa|dd/mm/aaaa-dd/mm/aaaa]` — gasto,
  conversaciones de WhatsApp iniciadas y costo por conversación, por campaña.
- El asistente también responde «¿cuánto llevo gastado en Drenaje esta semana?».

**Configuración (una sola vez):** Facebook exige una app propia para el botón.

1. developers.facebook.com → **Mis apps → Crear app** → caso de uso
   «Otro» → tipo **Empresa**, vinculada a tu portafolio comercial.
2. Agrega el producto **Inicio de sesión con Facebook** (o «para empresas») y
   en *Configuración* pon en **URI de redireccionamiento de OAuth válidos** el
   valor de `META_REDIRECT_URI`.
3. Agrega el producto **API de Marketing**.
4. En *Configuración → Básica* copia **Identificador de la app** →
   `META_APP_ID` y **Clave secreta** → `META_APP_SECRET`.
5. La app puede quedarse en modo **Desarrollo**: tú eres administrador y solo
   lees tu propia cuenta publicitaria, así que no necesita revisión de Meta.
6. Si la app es «Facebook Login for Business», crea una *Configuración* con el
   permiso `ads_read` y pon su id en `META_LOGIN_CONFIG_ID`.

Redirect: si el bot tiene dominio con HTTPS en Coolify, usa
`https://tu-dominio/meta/oauth/callback` y expón el puerto 19876; el login
termina solo. Sin dominio, deja el valor por defecto (`http://localhost:19876/...`):
al final el navegador dirá «no se puede acceder al sitio»; copias esa URL y la
pegas en el chat de Telegram (igual que con Dropi).

## Comandos (menú)

- `/start` — menu de ayuda
- `/nuevo` — nueva conversación con el asistente
- `/novedades` — novedades y devoluciones de Dropi (30 días) + HTML
- `/meta [periodo]` — Meta Ads por campaña · `/meta_conectar` / `/meta_desconectar`
- `/resumen` — estadisticas del dia (ventas, facturacion, producto mas vendido)
- `/ventas` — lista de pedidos de hoy
- `/crear_pedido <teléfono>` — ficha de la venta (promo, agencia, riesgo) con el botón para crear el pedido en Dropi
- `/en_agencia` — reporte HTML de pedidos en agencia Servientrega (MCP) con botón de WhatsApp
- `/reporte_diario` — reporte completo del día (también automático a las 22:00)
- `/guia <número>` — pedido de Dropi con esa guía: estado, cliente, agencia, productos (MCP)
- `/dropi_pedidos [ESTADO] [n]` — pedidos de Dropi de los últimos 30 días + HTML (MCP)
- `/dropi_conectar` / `/dropi_desconectar` — login OAuth del MCP de Dropi

Siguen funcionando aunque ya no están en el menú: `/crear`, `/pedido`,
`/campos`, `/pendientes`, `/retiro`, `/seguimiento`, `/dropi_saldo`
(este último usa la API web con MFA, ver abajo).

## Como funciona

Usa **long polling** (getUpdates), asi que NO necesita webhook ni dominio
publico ni puerto. Solo control de acceso por chat id.

El bot guarda dos archivos locales:

- `retiros.json` — fecha en que el bot detecto por primera vez cada guia en
  retiro (fija el "dia 0" del seguimiento).
- `retiros_historico.json` — fila completa (incluido el JSON de Dropi) de cada
  guia detectada. Asi, aunque el suscriptor desaparezca de Chatea Pro, el
  reporte no pierde el pedido ni los dias sin retirar. Cuando la guia deja de
  aparecer en un barrido completo se marca `retirado_el`.

## Variables de entorno

| Variable | Descripcion |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot (@BotFather) |
| `TELEGRAM_ALLOWED_IDS` | Chat ids autorizados, separados por coma (ej: `629254023`) |
| `CHATEAPRO_API_TOKEN` | Token de la API de Chatea Pro |
| `CHATEAPRO_CACHE_TTL_SECONDS` | TTL del cache de campos (default `300`). Evita saturar el API |
| `CHECK_INTERVAL_SECONDS` | Intervalo del ciclo de revision (default `120`) |
| `TIENDA_URL` | URL de la tienda web (proyecto `tienda/`). Sin ella no se leen pedidos web |
| `TIENDA_API_KEY` | Misma clave `TIENDA_API_KEY` configurada en la tienda |
| `TIENDA_CHECK_SECONDS` | Cada cuanto revisar pedidos web nuevos (default `60`) |
| `REVISAR_NUEVOS_INTERVAL_SECONDS` | Cada cuanto re-consultar nuevos (default `1800`) |
| `SCAN_DELAY_SECONDS` | Pausa entre peticiones en escaneos grandes (default `2.0`) |
| `RETIROS_DB_PATH` | Ruta de `retiros.json` (default: junto al codigo) |
| `RETIROS_HISTORICO_PATH` | Ruta de `retiros_historico.json` (default: junto al codigo) |
| `DROPI_EMAIL` / `DROPI_PASSWORD` | Cuenta Dropi para conexion directa (reportes/gestion) |
| `DROPI_MFA_CODE` | Codigo MFA fresco, solo al hacer login (vence en ~30s) |
| `DROPI_COUNTRY_CODE` | Pais de la cuenta (def. `ec`, header X-Host) |
| `ANTHROPIC_API_KEY` | Clave de Claude para el asistente (sin ella el asistente se apaga) |
| `ASISTENTE_MODELO` | Modelo del asistente (def. `claude-haiku-5-5`) |
| `ASISTENTE_ESFUERZO` | `low` (def.), `medium` o `high` |
| `GROQ_API_KEY` | Transcripción de audios con Groq Whisper (se usa primero) |
| `OPENAI_API_KEY` | Transcripción de audios con OpenAI si no hay Groq (y análisis IA de la ficha) |
| `TRANSCRIPCION_MODELO` | Cambia el modelo de transcripción (opcional) |
| `RIESGO_DEVOLUCION_MIN` | % de devolución desde el que la ficha alerta (def. 30) |
| `ALERTAS_DIARIAS` | `true` (def.) / `false`: novedades + recordatorios diarios |
| `HORA_ALERTAS` | Hora de las alertas diarias, Ecuador (def. `09:00`) |
| `RECORDATORIO_DIAS` | Días en agencia antes de recordar (def. 2) |
| `RECORDATORIO_MODO` | `off` (def., desactivado), `auto` o `confirmar` |
| `PLANTILLA_RETIRO` / `PLANTILLA_NOVEDAD` | Nombres de las plantillas aprobadas |
| `WA_TEMPLATE_NAMESPACE` | Namespace de plantillas de WhatsApp (ya viene el de la cuenta) |
| `ALERTAS_ESTADO_PATH` | Estado de alertas/recordatorios (usar volumen) |
| `VENTAS_NOTIFICADAS_PATH` | Ventas ya avisadas en Telegram; evita perder o repetir avisos al redesplegar (usar volumen) |
| `META_APP_ID` / `META_APP_SECRET` | App de Meta para «Conectar con Facebook» |
| `META_REDIRECT_URI` | Def. `http://localhost:19876/meta/oauth/callback` |
| `META_LOGIN_CONFIG_ID` | Solo si usas «Facebook Login for Business» |
| `META_AD_ACCOUNT_ID` | Opcional: `act_…` a usar si tienes varias cuentas |
| `META_TOKEN_PATH` | Acceso guardado de Facebook (usar volumen) |
| `META_GRAPH_VERSION` | Versión de la Graph API (def. `v24.0`) |

## Dropi directo (`dropi.py`)

Ademas de leer ChateaPro, el bot puede hablar directo con `app.dropi.ec`:

- Login: `POST api-v2.dropi.ec/bff/auth/core/login` (email + password + MFA).
  El token dura ~12h y se cachea: las llamadas siguientes no piden MFA.
- Lee: ordenes (`/bff/orders/myorders/v2` con filtros), historial de guias
  (Servientrega), saldo (`/bff/wallet/balance`).
- Escribe (implementado, sin probar en vivo aun): `crear_orden()` y
  `cotizar()` contra `/bff/orders` y `/bff/orders/quote`.

Los tokens de `Mis Integraciones` (ej. DROPILYZER) NO sirven para esto:
el API responde `Token not issued to this api`. Para operar sin MFA
pedir a Dropi un token server-to-server.

## Reporte en agencia (MCP de Dropi + OAuth desde Telegram)

`dropi_mcp.py` habla con `https://mcp.dropi.co/mcp` (OAuth 2.1: registro dinámico
+ PKCE + refresh token). Si no hay sesión, el bot manda un botón
**🔐 Iniciar sesión en Dropi**. Tras autorizar, Dropi redirige a
`DROPI_MCP_REDIRECT_URI`:

- Si el bot recibe esa URL (servidor de callback interno), se conecta solo y
  genera el reporte que estaba pendiente.
- Si no (redirect a `localhost` y abriste el link en el celular o el bot está
  en un VPS), el navegador muestra error: **copia la URL completa y pégala en
  el chat** y el bot termina la conexión.

El token se renueva solo (refresh) y se guarda en `dropi_mcp_token.json`.
`reporte_agencia.py` cruza la dirección Dropi (`CIUDAD_AGENCIA---SECTOR---CALLE`)
con `directorio_limpio.json` para poner el horario real de cada agencia.

| Variable | Descripción |
|---|---|
| `DROPI_MCP_REDIRECT_URI` | Def. `http://localhost:19876/mcp/oauth/callback`. Con dominio en Coolify: `https://<dominio>/mcp/oauth/callback` |
| `OAUTH_CALLBACK_PORT` | Puerto del callback (def. el de la redirect URI o 19876) |
| `OAUTH_CALLBACK_SERVER` | `false` para no levantar el servidor (solo modo pegar URL) |
| `DROPI_MCP_TOKEN_PATH` | Ruta del token (def. junto al código; usar volumen) |
| `WHATSAPP_LINK_BASE` | Def. `https://wa.me/`; `https://web.whatsapp.com/send?phone=` para WhatsApp Web directo |
| `HORARIO_AGENCIA_DEFECTO` | Horario si la agencia no está en el directorio |

## Crear pedidos en Dropi desde la ficha (MCP)

Cada venta nueva llega como ficha. El pedido se crea **directo con el MCP de
Dropi** (misma sesión OAuth de `/dropi_conectar`), sin Hermes:

1. **Productos** (`promos.py`): el embudo sale del campo `ID de Dropi` del CRM
   (148700 Mega Red, 186883 Drenaje) y los frascos de `Cantidad de productos`.
   El total sale de la escalera de precios del embudo y desde la promo de la
   semana se agrega el regalo sorpresa (Aceite Batana $8). Qué producto de
   Dropi se sube por cantidad se configura en `EMBUDOS`. Ej:
   Mega Red: 1 → Mega Red 3 (14235; sin stock 148700/19257) $27.99 · 2 → 2×
   148700 $15 + regalo = $37.99 o $38 (se cobra ese valor) · más
   frascos: 3 $52 · 4 $65 · 5 $80 · 6 $97 · 7+ $15 c/u (148700 + regalo);
   cualquier otra cosa del CRM se arma como 1 × $27.99 ·
   Drenaje 2 → 186883 $27.99 · Drenaje 4 → 2× 186883 $15 + Batana $8.

   **Stock, proveedor y regalo en la ficha.** Cada ficha muestra los
   productos que se subirán a Dropi con `#id · stock · proveedor`. El regalo
   sorpresa de cualquier promo se sube como **Té Chino x30 (115343)**
   (`REGALO_PRINCIPAL_ID`) al mismo precio, así el total no cambia. Si no hay
   stock, la ficha lista los 3 productos más económicos del proveedor
   (`DROPI_PROVEEDOR_PRINCIPAL`, def. 15819; costo ≥ `COSTO_MIN_REGALO` $1,
   stock ≥ `STOCK_MIN_REGALO` 5) como botones para elegir. Un pedido con
   productos de más de un proveedor no se puede crear.

   **Orden en que se decide la promo:** (1) lo aprendido del historial de la
   cuenta, (2) la regla automática, (3) `EMBUDOS` de este archivo. Si nada
   encaja, la ficha lo pide revisar. Siempre se confirma con el botón.

   **Promos aprendidas del historial (`historial.py`, sin configurar nada).**
   Cada venta que ya pasó a Dropi deja en Chatea Pro lo vendido (`ID de
   Dropi`, `Cantidad de productos`, `Valor de la compra`) y lo subido
   (`[Logistica] JSON products para Fletes`). El bot arma con eso la tabla
   (embudo, frascos, valor ±$0.15) → armados vistos y la usa cuando una
   combinación se vio al menos `HISTORIAL_MIN_VISTOS` veces (def. 2). Si hay
   varios armados, prueba del más visto al menos hasta encontrar stock. Se
   re-aprende cada `HISTORIAL_REFRESCO_HORAS` (def. 6, hasta
   `HISTORIAL_MAX_PAGINAS`×100 suscriptores) y cada pedido creado por el bot
   se suma al instante. Se guarda en `DATA_DIR/historial_promos.json`.
   `/promos_aprendidas [actualizar]` muestra la tabla en Telegram.

   **Promo automática desde Chatea Pro (sin tocar código).** Un embudo
   (`[Producto Ventas Wp] N`) que tenga entrada en el campo del bot
   `[Promos Dropi] Reglas` se arma solo: `ID de Dropi` × `Cantidad de
   productos`, total = `Valor de la compra`, + regalo (`upsells.1` del
   embudo, o `regalo` en la regla). Ejemplo del campo:

   ```json
   {"148700": {"producto_por_frascos": {"1": {"id": 14235, "nombre": "Mega Red 3"}},
               "regalo": {"id": 86248, "nombre": "Aceite Batana Oil", "precio": 8},
               "regalo_desde": 2, "max_frascos": 6},
    "186883": {"frascos_por_unidad": 2,
               "regalo": {"id": 86248, "nombre": "Aceite Batana Oil", "precio": 8},
               "regalo_desde": 4, "max_frascos": 6}}
   ```

   Si la cantidad está vacía, el valor no tiene sentido (ej. `2799` sin punto)
   o el pedido supera `max_frascos`, la ficha lo avisa para revisarlo. Los
   embudos sin regla siguen con `EMBUDOS` de `promos.py`. La config se lee
   cada 10 min (`CONFIG_PROMOS_TTL_SECONDS`). Si no se puede editar el Chatea
   Pro de la cuenta, el mismo JSON va en `DATA_DIR/reglas_promos.json`
   (`PROMOS_REGLAS_PATH`); el archivo gana sobre el campo del bot.
2. **Agencia**: el buscador de agencias; si la ciudad tiene varias y no
   reconoce el sector, se geocodifica la referencia del cliente ("colegio La
   Salle") con OpenStreetMap y se sugiere la agencia más cercana (con km).
   Se puede cambiar con los botones.
3. **Duplicados**: si Dropi ya tiene un pedido activo con ese teléfono
   (últimos `DROPI_DIAS_DUPLICADO` días) la ficha lo muestra y no deja crear.
4. **Crear pedido en Dropi** → resuelve ciudad y departamento exactos de Dropi
   (`search_cities` + `list_departments`), cotiza Servientrega
   (`quote_shipping`) y muestra el resumen. **Sí, crear en Dropi** →
   `create_order` con Servientrega y la dirección `AGENCIA---SECTOR---DIRECCIÓN`.

5. **Stock**: antes de mostrar el resumen se revisa el stock de cada producto
   en Dropi (`list_products`). Si falta, no deja crear y avisa para crearlo a mano.
6. **Mensaje al cliente**: al crear, se le envía por WhatsApp (Chatea Pro
   `send-text`) la confirmación con la agencia; plantilla con regalo si es promo.
   El texto se ve en el resumen antes de confirmar. Si WhatsApp lo rechaza
   (más de 24h sin que el cliente escriba) el bot avisa para enviarlo a mano.
7. **Sin agencia**: botón «Pedir agencia/sector al cliente». Si la ciudad tiene
   pocas agencias (`MAX_AGENCIAS_LISTA`) se le manda la lista numerada; si tiene
   muchas, se le pide sector o referencia. Cuando responde, «Reanalizar» toma su
   número. «Ver agencias de X» muestra la ciudad completa para elegir a mano.

«Reanalizar» vuelve a leer el CRM y el chat (por ejemplo después de que el
cliente responde o de corregir un dato en Chatea Pro) y rehace la ficha.

| Variable | Descripción |
|---|---|
| `NOTIFICAR_CLIENTE` | `true` (def.) envía la confirmación al cliente al crear |
| `MAX_AGENCIAS_LISTA` | Hasta cuántas agencias se le listan al cliente (def. 10) |
| `AGENCIAS_OFRECIDAS_PATH` | Registro de listas enviadas a clientes (usar volumen) |
| `GEOCODER` | `auto` (def.: Photon y de respaldo Nominatim), `photon`, `nominatim` u `off`. Los cruces de calles ("García Moreno y Martínez Mera") se ubican buscando cada calle y tomando donde se cruzan; solo se sugieren agencias con entrega en oficina |
| `PHOTON_URL` | def. `https://photon.komoot.io/api/` |
| `GEOCODER_UA` | User-Agent para el geocodificador (def. `ventas-bot/1.0`) |
| `DROPI_DIAS_DUPLICADO` | Días revisados para no duplicar (def. 3) |
| `PEDIDOS_CREADOS_PATH` | Registro local de pedidos creados (usar volumen) |

## Despliegue en Coolify

1. Nueva Application desde el repo, **Base Directory** = `/ventas-bot`.
2. Build Pack: **Dockerfile**.
3. NO necesita dominio ni puerto (usa polling).
4. Configura las variables de entorno de arriba.
5. **Persistencia (un solo volumen):** en la app → *Persistent Storage* →
   *Add* → *Volume Mount*, nombre `ventas-bot-data`, Destination Path `/data`.
   Luego agrega la variable `DATA_DIR=/data`. Ahí quedan todos los archivos de
   estado: login de Dropi (`dropi_mcp_token.json`), acceso a Facebook
   (`meta_token.json`), ventas avisadas, retiros, alertas, agencias ofrecidas y
   pedidos creados. Sin volumen se borran en cada deploy (hay que reconectar
   Dropi y se pierden los avisos). Cada archivo se puede mover aparte con su
   variable `*_PATH`.
6. Deploy.

## Prueba local

```bash
pip3 install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_ALLOWED_IDS="629254023"
export CHATEAPRO_API_TOKEN="..."
python3 bot.py
```