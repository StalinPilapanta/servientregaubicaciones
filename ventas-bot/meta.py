"""
Meta Ads (Facebook) con "Conectar con Facebook" desde Telegram.

Flujo (igual que el login de Dropi):
  1. /meta_conectar -> boton con el link de Facebook Login (permiso ads_read).
  2. Inicias sesion con Facebook y aceptas los permisos.
  3. Facebook vuelve a META_REDIRECT_URI con ?code=...; el bot lo canjea por un
     token de larga duracion (~60 dias) y guarda tus cuentas publicitarias.
     Si el redirect es localhost y el navegador no abre, pegas la URL en el chat.
  4. /meta consulta gasto, conversaciones y costo por campaña.
  Cuando el token esta por vencer, el bot te vuelve a mandar el boton.

Requiere UNA app de Meta (developers.facebook.com) creada una sola vez:
  META_APP_ID, META_APP_SECRET   de la app
  META_REDIRECT_URI     def. http://localhost:19876/meta/oauth/callback
                        (agregarla en "URI de redireccionamiento de OAuth validos")
  META_LOGIN_CONFIG_ID  opcional: id de configuracion de "Facebook Login for Business"
  META_AD_ACCOUNT_ID    opcional: act_XXXX a usar si tienes varias cuentas
  META_TOKEN_PATH       def. meta_token.json junto al codigo (usar volumen)
  META_GRAPH_VERSION    def. v24.0
"""

import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import datos

APP_ID = os.environ.get("META_APP_ID", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")
REDIRECT_URI = os.environ.get("META_REDIRECT_URI",
                              "http://localhost:19876/meta/oauth/callback")
CONFIG_ID = os.environ.get("META_LOGIN_CONFIG_ID", "")
CUENTA_FIJA = os.environ.get("META_AD_ACCOUNT_ID", "")
VERSION = os.environ.get("META_GRAPH_VERSION", "v24.0")
TOKEN_PATH = os.environ.get(
    "META_TOKEN_PATH",
    datos.ruta("meta_token.json"))
GRAPH = f"https://graph.facebook.com/{VERSION}"
ACCION_CONVERSACION = "onsite_conversion.messaging_conversation_started_7d"

_pendientes = {}   # state -> creado (ts)
_lock = threading.Lock()


class MetaError(Exception):
    pass


class AuthRequired(MetaError):
    """No hay token de Facebook o vencio: hay que volver a conectar."""


def configurada():
    return bool(APP_ID and APP_SECRET)


# ------------------------------------------------------------------ token --

def _leer():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar(d):
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TOKEN_PATH)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def estado():
    """{conectado, usuario, cuenta, cuentas, dias_restantes} (sin exponer el token)."""
    d = _leer()
    if not d.get("token"):
        return {"conectado": False}
    restante = None
    if d.get("expira"):
        restante = int((d["expira"] - time.time()) / 86400 + 0.5)
    return {"conectado": restante is None or restante >= 0, "usuario": d.get("usuario"),
            "cuenta": d.get("cuenta"), "cuentas": d.get("cuentas", []),
            "dias_restantes": restante}


def desconectar():
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass


# ------------------------------------------------------------------ login --

def url_login():
    """Link de "Conectar con Facebook" (vence en 15 min)."""
    if not configurada():
        raise MetaError("Falta META_APP_ID / META_APP_SECRET (la app de Meta).")
    state = secrets.token_urlsafe(16)
    with _lock:
        ahora = time.time()
        for k in [k for k, t in _pendientes.items() if ahora - t > 900]:
            _pendientes.pop(k, None)
        _pendientes[state] = ahora
    params = {"client_id": APP_ID, "redirect_uri": REDIRECT_URI, "state": state,
              "response_type": "code"}
    if CONFIG_ID:
        params["config_id"] = CONFIG_ID       # Facebook Login for Business
    else:
        params["scope"] = "ads_read"
    return f"https://www.facebook.com/{VERSION}/dialog/oauth?" + urlencode(params)


def es_url_callback(texto):
    """True si el texto es la URL de retorno de Facebook (pegada en el chat)."""
    t = (texto or "").strip()
    if not t.startswith(("http://", "https://")):
        return False
    u = urlparse(t)
    st = (parse_qs(u.query).get("state") or [""])[0]
    return u.path == urlparse(REDIRECT_URI).path or st in _pendientes


def es_ruta_callback(path):
    return urlparse(path).path == urlparse(REDIRECT_URI).path


def _graph(path, params):
    r = requests.get(f"{GRAPH}/{path.lstrip('/')}", params=params, timeout=40)
    try:
        d = r.json()
    except ValueError:
        raise MetaError(f"Meta respondió {r.status_code}: {r.text[:200]}")
    if "error" in d:
        err = d["error"]
        if err.get("code") == 190 or (err.get("type") == "OAuthException"
                                      and r.status_code == 401):
            raise AuthRequired(err.get("message", "Token de Facebook vencido."))
        raise MetaError(f"Meta: {err.get('message')} (código {err.get('code')})")
    return d


def completar_desde_url(url):
    q = parse_qs(urlparse(url.strip()).query)
    if q.get("error"):
        raise MetaError("Facebook rechazó el acceso: "
                        + (q.get("error_description") or q["error"])[0])
    state = (q.get("state") or [""])[0]
    with _lock:
        if _pendientes.pop(state, None) is None:
            raise MetaError("El link de conexión venció o ya se usó. Pide uno nuevo.")
    code = (q.get("code") or [""])[0]
    if not code:
        raise MetaError("La URL no trae el código de Facebook.")

    corto = _graph("oauth/access_token", {
        "client_id": APP_ID, "client_secret": APP_SECRET,
        "redirect_uri": REDIRECT_URI, "code": code})
    largo = _graph("oauth/access_token", {
        "grant_type": "fb_exchange_token", "client_id": APP_ID,
        "client_secret": APP_SECRET, "fb_exchange_token": corto["access_token"]})
    token = largo["access_token"]
    expira = time.time() + int(largo.get("expires_in") or 60 * 86400)

    yo = _graph("me", {"fields": "name", "access_token": token})
    cuentas = _graph("me/adaccounts", {
        "fields": "name,account_id,account_status,currency", "limit": 50,
        "access_token": token}).get("data", [])
    cuentas = [{"id": c["id"], "nombre": c.get("name"), "moneda": c.get("currency"),
                "activa": c.get("account_status") == 1} for c in cuentas]
    elegida = next((c["id"] for c in cuentas if c["id"] == CUENTA_FIJA), None) \
        or next((c["id"] for c in cuentas if c["activa"]), None) \
        or (cuentas[0]["id"] if cuentas else None)
    _guardar({"token": token, "expira": expira, "usuario": yo.get("name"),
              "cuentas": cuentas, "cuenta": elegida, "conectado_el": time.time()})
    return estado()


def elegir_cuenta(cuenta_id):
    d = _leer()
    if not any(c["id"] == cuenta_id for c in d.get("cuentas", [])):
        raise MetaError("Esa cuenta no está entre las conectadas.")
    d["cuenta"] = cuenta_id
    _guardar(d)


# --------------------------------------------------------------- consultas --

def _token_y_cuenta():
    d = _leer()
    if not d.get("token"):
        raise AuthRequired("Meta no está conectado.")
    if d.get("expira") and d["expira"] < time.time():
        raise AuthRequired("El acceso a Facebook venció (dura ~60 días).")
    if not d.get("cuenta"):
        raise MetaError("Tu usuario de Facebook no tiene cuentas publicitarias visibles.")
    return d["token"], d["cuenta"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def campanas(desde, hasta):
    """
    Rendimiento por campaña entre dos fechas (date). Devuelve lista de dicts:
      campana, gasto, impresiones, clics, conversaciones, costo_conversacion
    """
    token, cuenta = _token_y_cuenta()
    params = {
        "level": "campaign", "limit": 100, "access_token": token,
        "fields": "campaign_name,spend,impressions,clicks,actions",
        "time_range": json.dumps({"since": desde.isoformat(), "until": hasta.isoformat()}),
    }
    filas, ruta = [], f"{cuenta}/insights"
    while True:
        d = _graph(ruta, params)
        for x in d.get("data", []):
            conv = sum(_num(a.get("value")) for a in x.get("actions") or []
                       if a.get("action_type") == ACCION_CONVERSACION)
            gasto = _num(x.get("spend"))
            filas.append({
                "campana": x.get("campaign_name"), "gasto": round(gasto, 2),
                "impresiones": int(_num(x.get("impressions"))),
                "clics": int(_num(x.get("clicks"))), "conversaciones": int(conv),
                "costo_conversacion": round(gasto / conv, 2) if conv else None,
            })
        siguiente = (d.get("paging") or {}).get("next")
        if not siguiente:
            break
        # El link "next" ya trae todos los parametros (incluido el token).
        u = urlparse(siguiente)
        ruta = u.path.split(f"/{VERSION}/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
    filas.sort(key=lambda f: -f["gasto"])
    return filas


def resumen(desde, hasta, etiqueta):
    filas = campanas(desde, hasta)
    gasto = sum(f["gasto"] for f in filas)
    conv = sum(f["conversaciones"] for f in filas)
    L = [f"📣 Meta Ads · {etiqueta}",
         f"💸 Gasto: ${gasto:.2f} · 💬 Conversaciones: {conv}"
         + (f" · ${gasto / conv:.2f} c/u" if conv else ""), ""]
    for f in filas:
        if not f["gasto"] and not f["conversaciones"]:
            continue
        cpc = f" · ${f['costo_conversacion']:.2f} c/u" if f["costo_conversacion"] else ""
        L.append(f"• {f['campana']}\n  ${f['gasto']:.2f} · {f['conversaciones']} conv{cpc} · "
                 f"{f['clics']} clics · {f['impresiones']:,} impr.".replace(",", "."))
    if len(L) == 3:
        L.append("Sin gasto en ese periodo.")
    return "\n".join(L)
