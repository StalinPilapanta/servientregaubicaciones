"""
Cliente minimo del MCP oficial de Dropi (https://mcp.dropi.co/mcp) con OAuth.

El MCP usa OAuth 2.1 estandar (verificado en sus /.well-known):
  - registro dinamico de cliente  POST https://mcp.dropi.co/oauth/register
  - autorizacion (login del user) GET  https://mcp.dropi.co/login/authorize
  - token / refresh               POST https://mcp.dropi.co/oauth/token
  (se descubren solos por /.well-known; antes eran oauth.dropi.co e integrations.dropi.co)
  - cliente publico + PKCE (S256), sin client_secret

Flujo desde Telegram:
  1. Una llamada al MCP necesita token y no hay (o el refresh fallo) ->
     se lanza AuthRequired.
  2. El bot llama url_autorizacion() y envia el link como boton.
  3. El usuario inicia sesion en Dropi. Dropi redirige a REDIRECT_URI con
     ?code=...&state=...
       - Si el bot escucha en esa URL (servidor de callback), se completa solo.
       - Si no (bot en un VPS sin dominio, o abriste el link en el celular),
         el navegador muestra error: el usuario copia la URL completa y la
         pega en el chat; el bot la procesa con completar_desde_url().
  4. Tokens (access + refresh) se guardan en disco; el refresh renueva solo.

Variables de entorno:
  DROPI_MCP_URL            def. https://mcp.dropi.co/mcp
  DROPI_MCP_REDIRECT_URI   def. http://localhost:19876/mcp/oauth/callback
                           (si el bot tiene dominio publico: https://<dominio>/mcp/oauth/callback)
  DROPI_MCP_TOKEN_PATH     def. dropi_mcp_token.json junto al codigo (montar volumen)
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse, parse_qs

import requests
import datos

MCP_URL = os.environ.get("DROPI_MCP_URL", "https://mcp.dropi.co/mcp")
REDIRECT_URI = os.environ.get(
    "DROPI_MCP_REDIRECT_URI", "http://localhost:19876/mcp/oauth/callback")
TOKEN_PATH = os.environ.get(
    "DROPI_MCP_TOKEN_PATH",
    datos.ruta("dropi_mcp_token.json"),
)
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "ventas-bot-telegram"

_lock = threading.RLock()
_meta = None            # metadata del servidor de autorizacion (cache)
_pendientes = {}        # state -> {"verifier": ..., "ts": ...}
_session_id = None      # Mcp-Session-Id de la sesion MCP actual (solo si ya esta LISTA)
_rpc_id = 0
# Abrir sesion = initialize + notifications/initialized. Si otro hilo llama
# tools/call entre ambos, Dropi responde "invalid during session
# initialization": la sesion solo se publica en _session_id ya inicializada.
_init_lock = threading.Lock()


class AuthRequired(Exception):
    """No hay token valido: hay que iniciar sesion en Dropi."""


class MCPError(Exception):
    pass


# ------------------------------------------------------------ persistencia --

def _leer():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar(d):
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, TOKEN_PATH)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def conectado():
    """True si hay token guardado (puede estar vencido pero tener refresh)."""
    d = _leer()
    return bool(d.get("access_token") or d.get("refresh_token"))


def desconectar():
    global _session_id
    with _lock:
        d = _leer()
        for k in ("access_token", "refresh_token", "expires_at"):
            d.pop(k, None)
        _guardar(d)
        _session_id = None


# ------------------------------------------------------------------ OAuth --

def _well_known_resource():
    u = urlparse(MCP_URL)
    return f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path}"


def _metadata(fresca=False):
    """Metadata del servidor de autorizacion. Se re-descubre cada hora (o si
    fresca=True): Dropi ya movio su OAuth una vez (oauth.dropi.co -> mcp.dropi.co)."""
    global _meta
    if _meta and not fresca and time.time() - _meta.get("_ts", 0) < 3600:
        return _meta
    r = requests.get(_well_known_resource(), timeout=15)
    r.raise_for_status()
    prm = r.json()
    servidor = prm["authorization_servers"][0].rstrip("/")
    r = requests.get(servidor + "/.well-known/oauth-authorization-server", timeout=15)
    r.raise_for_status()
    _meta = dict(r.json(), _ts=time.time(),
                 _scope=" ".join(prm.get("scopes_supported") or ["mcp"]))
    return _meta


def _cliente():
    """client_id registrado (registro dinamico, una vez por redirect_uri)."""
    d = _leer()
    meta = _metadata()
    if (d.get("client_id") and d.get("redirect_uri") == REDIRECT_URI
            and d.get("registro") == meta["registration_endpoint"]):
        return d["client_id"]
    r = requests.post(meta["registration_endpoint"], json={
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": meta["_scope"],
    }, timeout=20)
    if r.status_code >= 400:
        raise MCPError(f"Registro OAuth en Dropi fallo (HTTP {r.status_code}): {r.text[:200]}")
    client_id = r.json()["client_id"]
    d.update({"client_id": client_id, "redirect_uri": REDIRECT_URI,
              "registro": meta["registration_endpoint"]})
    _guardar(d)
    return client_id


def url_autorizacion():
    """Genera el link de login de Dropi (PKCE + state). Valido ~15 min."""
    with _lock:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        ahora = time.time()
        for s in [s for s, v in _pendientes.items() if ahora - v["ts"] > 900]:
            _pendientes.pop(s, None)
        _pendientes[state] = {"verifier": verifier, "ts": ahora}
        meta = _metadata(fresca=True)
        params = {
            "response_type": "code",
            "client_id": _cliente(),
            "redirect_uri": REDIRECT_URI,
            "scope": meta["_scope"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP_URL,
        }
        return meta["authorization_endpoint"] + "?" + urlencode(params)


def es_url_callback(texto):
    """True si el texto parece la URL de retorno de Dropi (con code y state)."""
    t = (texto or "").strip()
    return t.startswith(("http://", "https://")) and "code=" in t and "state=" in t


def completar_desde_url(url):
    """Procesa la URL de retorno (pegada en el chat o recibida por el servidor)."""
    q = parse_qs(urlparse(url.strip()).query)
    if q.get("error"):
        raise MCPError(f"Dropi rechazo el acceso: {q['error'][0]}")
    return completar((q.get("code") or [""])[0], (q.get("state") or [""])[0])


def completar(code, state):
    """Canjea el code por tokens. Devuelve True si quedo conectado."""
    with _lock:
        pend = _pendientes.pop(state, None)
        if not pend:
            raise MCPError("El link de conexion vencio o ya se uso. Pide uno nuevo.")
        r = requests.post(_metadata()["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": _cliente(),
            "code_verifier": pend["verifier"],
            "resource": MCP_URL,
        }, timeout=20)
        if r.status_code >= 400:
            raise MCPError(f"No se pudo obtener el token (HTTP {r.status_code}): {r.text[:200]}")
        _guardar_tokens(r.json())
        return True


def _guardar_tokens(tok):
    global _session_id
    d = _leer()
    d["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        d["refresh_token"] = tok["refresh_token"]
    d["expires_at"] = time.time() + int(tok.get("expires_in") or 3600) - 60
    _guardar(d)
    _session_id = None  # nueva sesion MCP con el token nuevo


def _refrescar():
    with _lock:
        d = _leer()
        if not d.get("refresh_token"):
            desconectar()
            raise AuthRequired("Sin refresh token")
        r = requests.post(_metadata()["token_endpoint"], data={
            "grant_type": "refresh_token",
            "refresh_token": d["refresh_token"],
            "client_id": d.get("client_id"),
            "resource": MCP_URL,
        }, timeout=20)
        if r.status_code >= 400:
            desconectar()
            raise AuthRequired(f"Refresh rechazado (HTTP {r.status_code})")
        _guardar_tokens(r.json())


def _token():
    with _lock:
        d = _leer()
        if not d.get("access_token") and not d.get("refresh_token"):
            raise AuthRequired("Dropi no esta conectado")
        if not d.get("access_token") or time.time() >= d.get("expires_at", 0):
            _refrescar()
            d = _leer()
        return d["access_token"]


# -------------------------------------------------------------- MCP (HTTP) --

def _post_rpc(payload, token, sid):
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if sid:
        h["Mcp-Session-Id"] = sid
    return requests.post(MCP_URL, json=payload, headers=h, timeout=60)


def _respuesta(r, rpc_id):
    """Extrae el mensaje JSON-RPC con ese id (respuesta JSON o SSE)."""
    if "text/event-stream" in r.headers.get("Content-Type", ""):
        for linea in r.text.splitlines():
            if linea.startswith("data:"):
                try:
                    m = json.loads(linea[5:].strip())
                except ValueError:
                    continue
                if m.get("id") == rpc_id:
                    return m
        raise MCPError("Respuesta SSE del MCP sin resultado")
    return r.json()


def _rpc(metodo, params=None, notificacion=False, sid=None, _reintento=True):
    """
    Una llamada JSON-RPC con la sesion 'sid'. Devuelve el result; en
    "initialize" devuelve (result, session_id).
    """
    global _rpc_id
    with _lock:
        _rpc_id += 1
        rid = _rpc_id
    payload = {"jsonrpc": "2.0", "method": metodo}
    if params is not None:
        payload["params"] = params
    if not notificacion:
        payload["id"] = rid

    r = _post_rpc(payload, _token(), sid)
    if r.status_code == 401:
        if not _reintento:
            desconectar()
            raise AuthRequired("Token rechazado por el MCP")
        _refrescar()  # lanza AuthRequired si no se puede
        if metodo not in ("initialize", "notifications/initialized"):
            sid = _sesion(vieja=sid)
        return _rpc(metodo, params, notificacion, sid, _reintento=False)
    if r.status_code == 404 and sid and _reintento and \
            metodo not in ("initialize", "notifications/initialized"):  # _sesion ya tiene el candado
        sid = _sesion(vieja=sid)  # la sesion expiro en el servidor
        return _rpc(metodo, params, notificacion, sid, _reintento=False)
    if r.status_code >= 400:
        raise MCPError(f"MCP HTTP {r.status_code}: {r.text[:200]}")
    if notificacion:
        return None
    m = _respuesta(r, rid)
    if m.get("error"):
        raise MCPError(f"MCP error: {m['error'].get('message')}")
    if metodo == "initialize":
        return m.get("result") or {}, r.headers.get("Mcp-Session-Id")
    return m.get("result") or {}


def _sesion(vieja=None):
    """
    Id de una sesion MCP ya inicializada. Si 'vieja' es la sesion actual
    (expiro o el servidor la rechazo) abre otra. Un solo hilo la abre a la vez.
    """
    global _session_id
    with _init_lock:
        if _session_id and _session_id != vieja:
            return _session_id
        _session_id = None
        _, sid = _rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        })
        _rpc("notifications/initialized", notificacion=True, sid=sid)
        _session_id = sid  # recien ahora la ven los demas hilos
        return sid


def llamar(herramienta, argumentos=None):
    """
    Llama una herramienta del MCP de Dropi y devuelve el resultado como
    dict de Python (structuredContent, JSON o texto TOON parseado).
    Lanza AuthRequired si hay que (re)conectar Dropi.
    """
    sid = _sesion()
    params = {"name": herramienta, "arguments": argumentos or {}}
    try:
        res = _rpc("tools/call", params, sid=sid)
    except MCPError as e:
        if "initializ" not in str(e).lower():
            raise
        res = _rpc("tools/call", params, sid=_sesion(vieja=sid))  # sesion a medio abrir: otra
    if res.get("isError"):
        raise MCPError(_texto(res) or f"Error en {herramienta}")
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    texto = _texto(res)
    try:
        return json.loads(texto)
    except ValueError:
        return parse_toon(texto)


def _texto(res):
    return "\n".join(c.get("text", "") for c in res.get("content") or []
                     if c.get("type") == "text")


# ------------------------------------------------------------- TOON parser --
# El MCP de Dropi responde en TOON (formato compacto tipo YAML + tablas):
#   order_id: "7944449"
#   customer:
#     name: Edgar
#   items[1]{product_id,product_name,qty}:
#     14235,Mega Red 3,1

def _valor(s):
    s = s.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_csv(linea):
    """Separa por comas respetando comillas dobles (con escapes \\")."""
    campos, actual, en_comillas, i = [], [], False, 0
    while i < len(linea):
        c = linea[i]
        if en_comillas and c == "\\" and i + 1 < len(linea):
            actual.append(linea[i:i + 2])
            i += 2
            continue
        if c == '"':
            en_comillas = not en_comillas
        if c == "," and not en_comillas:
            campos.append("".join(actual))
            actual = []
        else:
            actual.append(c)
        i += 1
    campos.append("".join(actual))
    return [_valor(x) for x in campos]


def _indent(linea):
    return len(linea) - len(linea.lstrip(" "))


def parse_toon(texto):
    lineas = [l for l in (texto or "").splitlines() if l.strip()]
    obj, _ = _parse_bloque(lineas, 0, 0)
    return obj


def _parse_bloque(lineas, i, nivel):
    obj = {}
    while i < len(lineas):
        l = lineas[i]
        ind = _indent(l)
        if ind < nivel:
            break
        s = l.strip()
        # Tabla: key[N]{a,b,c}:
        m = re.match(r"^([^\[:]+)\[(\d+)\]\{([^}]*)\}:\s*$", s)
        if m:
            clave, campos = m.group(1).strip(), [c.strip() for c in m.group(3).split(",")]
            filas, i = [], i + 1
            while i < len(lineas) and _indent(lineas[i]) > ind:
                filas.append(dict(zip(campos, _split_csv(lineas[i].strip()))))
                i += 1
            obj[clave] = filas
            continue
        # Lista: key[N]: a,b   o   key[N]:  seguido de "- item"
        m = re.match(r"^([^\[:]+)\[(\d+)\]:\s*(.*)$", s)
        if m:
            clave = m.group(1).strip()
            if m.group(3):
                obj[clave] = _split_csv(m.group(3))
                i += 1
                continue
            items, i = [], i + 1
            while i < len(lineas) and _indent(lineas[i]) > ind:
                item = lineas[i].strip()
                resto = item[2:] if item.startswith("- ") else item
                # "- clave: valor" seguido de mas claves -> lista de objetos
                if item.startswith("- ") and re.match(r'^"?[\w ]+"?(\[\d+\])?(\{[^}]*\})?:', resto):
                    base = _indent(lineas[i]) + 2
                    sub = [" " * base + resto]
                    i += 1
                    while i < len(lineas) and _indent(lineas[i]) >= base:
                        sub.append(lineas[i])
                        i += 1
                    items.append(_parse_bloque(sub, 0, base)[0])
                    continue
                items.append(_valor(resto))
                i += 1
            obj[clave] = items
            continue
        if ":" not in s:
            i += 1
            continue
        clave, _, resto = s.partition(":")
        clave = clave.strip().strip('"')
        if resto.strip() == "":
            if i + 1 < len(lineas) and _indent(lineas[i + 1]) > ind:
                sub, i = _parse_bloque(lineas, i + 1, _indent(lineas[i + 1]))
                obj[clave] = sub
                continue
            obj[clave] = ""
            i += 1
            continue
        obj[clave] = _valor(resto)
        i += 1
    return obj, i
