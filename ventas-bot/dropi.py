"""
Cliente de la API de Dropi (app.dropi.ec) para el bot de ventas.

Autenticacion: login con email + password + codigo MFA (TOTP del
authenticator o OTP por correo). El codigo MFA vence rapido (~30s si es
TOTP), asi que se pasa por variable de entorno en cada ejecucion:

  DROPI_API_GO_URL      base del BFF auth (def. https://api-v2.dropi.ec)
  DROPI_API_CLASSIC_URL base del API clasico (def. https://api.dropi.ec)
  DROPI_API_URL         URL del panel (def. https://app.dropi.ec, solo referer)
  DROPI_EMAIL           correo de la cuenta Dropi
  DROPI_PASSWORD        clave de la cuenta Dropi
  DROPI_MFA_CODE        codigo MFA fresco (solo se usa al hacer login)
  DROPI_WHITE_BRAND_ID  marca del panel (def. 1)
  DROPI_TOKEN_PATH      donde cachear el token (def. temporal del sistema)

Endpoints verificados en vivo (panel app.dropi.ec, cuenta Ecuador):
  POST {API_GO}/bff/auth/core/login        {email,password,otp,...} -> token
  GET  {API_GO}/bff/orders/myorders/v2     ?result_number&start&orderBy...
                                           + filtros: status, textToSearch,
                                           from/untill, filter_by...
  POST {API_GO}/bff/orders/quote           cotizar (payload del panel)
  POST {API_GO}/bff/orders                 crear orden (ver campos obligatorios)
  GET  {API_GO}/bff/wallet/balance         saldo de cartera
  GET  {CLASSIC}/orders/consultarhistoricoguia?shipping_guide=...
                                           movimientos Servientrega de la guia

Auth BFF: headers X-Authorization + Authorization (Bearer) + X-Host (pais).
El API clasico (/api/...) rechaza este token salvo consultarhistoricoguia.
"""

import os
import json
import time
import tempfile

import requests

API_GO = os.environ.get("DROPI_API_GO_URL", "https://api-v2.dropi.ec")
API_CLASSIC = os.environ.get("DROPI_API_CLASSIC_URL", "https://api.dropi.ec")
APP_URL = os.environ.get("DROPI_API_URL", "https://app.dropi.ec")

EMAIL = os.environ.get("DROPI_EMAIL", "")
PASSWORD = os.environ.get("DROPI_PASSWORD", "")
WHITE_BRAND_ID = int(os.environ.get("DROPI_WHITE_BRAND_ID", "1"))
# Codigo de pais de la cuenta (header X-Host que exige el BFF).
COUNTRY_CODE = os.environ.get("DROPI_COUNTRY_CODE", "ec")

TOKEN_PATH = os.environ.get(
    "DROPI_TOKEN_PATH",
    os.path.join(tempfile.gettempdir(), "dropi_token.json"),
)

# Token en memoria (se carga del cache en disco si existe).
_TOKEN = None
_TOKEN_TS = 0
# El token del panel suele durar horas; si el API responde 401 se reintenta
# el login una vez automaticamente.
TOKEN_TTL = int(os.environ.get("DROPI_TOKEN_TTL_SECONDS", "43200"))


def _headers_base():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": APP_URL,
        "Referer": APP_URL + "/",
        "User-Agent": "ventas-bot/1.0",
    }


def _mi_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=15).text.strip()
    except Exception:
        return ""


def _guardar_token(token):
    global _TOKEN, _TOKEN_TS
    _TOKEN, _TOKEN_TS = token, time.time()
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": token, "ts": _TOKEN_TS}, f)
    except OSError as e:
        print("No se pudo cachear el token Dropi:", e)


def _cargar_token():
    global _TOKEN, _TOKEN_TS
    if _TOKEN and (time.time() - _TOKEN_TS) < TOKEN_TTL:
        return _TOKEN
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("token") and (time.time() - d.get("ts", 0)) < TOKEN_TTL:
            _TOKEN, _TOKEN_TS = d["token"], d["ts"]
            return _TOKEN
    except (OSError, ValueError):
        pass
    return None


def _extraer_token(data):
    """Saca el token del login probando las formas que usa el panel."""
    if not isinstance(data, dict):
        return None
    for k in ("token", "access_token", "accessToken", "jwt", "api_token"):
        if data.get(k):
            return str(data[k])
    for k in ("objects", "object", "result", "user"):
        sub = data.get(k)
        if isinstance(sub, dict):
            t = _extraer_token(sub)
            if t:
                return t
    return None


def login(mfa_code=None):
    """
    Hace login en Dropi con email + password + codigo MFA fresco.
    Devuelve el token de sesion (y lo cachea). Lanza RuntimeError si falla.
    """
    code = (mfa_code or os.environ.get("DROPI_MFA_CODE", "")).strip()
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Faltan DROPI_EMAIL / DROPI_PASSWORD")
    if not code:
        raise RuntimeError("Falta el codigo MFA fresco (DROPI_MFA_CODE)")

    payload = {
        "email": EMAIL.lower().strip(),
        "password": PASSWORD,
        "otp": code,
        "white_brand_id": WHITE_BRAND_ID,
        "ipAddress": _mi_ip(),
        "with_cdc": False,
    }
    r = requests.post(API_GO + "/bff/auth/core/login", json=payload,
                      headers=_headers_base(), timeout=30)
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"Login Dropi: respuesta no JSON (HTTP {r.status_code})")

    ok = d.get("is_successful", d.get("isSuccess", False))
    token = _extraer_token(d.get("data"))
    if token:
        # Aceptar aunque el flag venga inconsistente (el panel a veces
        # devuelve mensajes de exito con status 400).
        _guardar_token(token)
        return token
    if not ok or r.status_code >= 400:
        msg = d.get("status_reason") or d.get("message") or f"HTTP {r.status_code}"
        raise RuntimeError(f"Login Dropi fallo: {msg}")

    raise RuntimeError("Login Dropi ok pero sin token en la respuesta")


def _auth_headers():
    token = _cargar_token()
    if not token:
        token = login()
    h = _headers_base()
    # El BFF exige este trio (verificado contra el panel web).
    h["X-Authorization"] = f"Bearer {token}"
    h["Authorization"] = f"Bearer {token}"
    h["X-Host"] = COUNTRY_CODE
    return h


def _get(path, params=None, base=API_GO, reintento=True):
    """GET autenticado; si hay 401 re-hace login una vez y reintenta."""
    r = requests.get(base + path, params=params or {},
                     headers=_auth_headers(), timeout=30)
    if r.status_code == 401 and reintento:
        login()  # pide DROPI_MFA_CODE fresco del entorno
        r = requests.get(base + path, params=params or {},
                         headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, body=None, base=API_GO, reintento=True):
    """POST autenticado; si hay 401 re-hace login una vez y reintenta."""
    r = requests.post(base + path, json=body or {}, headers=_auth_headers(),
                      timeout=30)
    if r.status_code == 401 and reintento:
        login()  # pide DROPI_MFA_CODE fresco del entorno
        r = requests.post(base + path, json=body or {},
                          headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


# --- Reportes ---

def listar_mis_ordenes(page=1, per_page=50, filtros=None):
    """
    Ordenes de la cuenta (GET /bff/orders/myorders/v2).
    Filtros utiles: textToSearch, from, untill (yyyy-mm-dd), status,
    filter_by (GUIA/ORDEN ID/CELULAR) + value_filter_by, filter_date_by.
    Devuelve el JSON tal cual: data.objects (lista) + data.count.
    """
    params = {
        "result_number": per_page,
        "start": page,
        "orderBy": "id",
        "orderDirection": "desc",
    }
    if filtros:
        params.update(filtros)
    return _get("/bff/orders/myorders/v2", params, base=API_GO)


def objetos_ordenes(respuesta):
    """Extrae (lista, total) del formato {data: {objects, count}}."""
    data = (respuesta or {}).get("data") or {}
    return data.get("objects") or [], data.get("count")


def exportar_excel(filtros=None):
    """Reporte Excel del panel (POST /orders/exportexcel)."""
    return _post("/orders/exportexcel", filtros or {}, base=API_CLASSIC)


def buscar_ordenes(texto, per_page=10):
    """Busca ordenes por nombre/ciudad/direccion (textToSearch del panel)."""
    return _get("/bff/orders/myorders/v2", {
        "result_number": per_page,
        "start": 1,
        "textToSearch": texto,
        "orderBy": "id",
        "orderDirection": "desc",
    }, base=API_GO)


def historial_guia(guia):
    """
    Movimientos Servientrega de una guia
    (GET /orders/consultarhistoricoguia?shipping_guide=...).
    Devuelve el JSON con objects.Mov.InformacionMov (lista).
    """
    r = requests.get(API_CLASSIC + "/orders/consultarhistoricoguia",
                     params={"shipping_guide": str(guia).strip()},
                     headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def movimientos_guia(guia):
    """Lista de movimientos (dicts) de una guia, o [] si no existe."""
    d = historial_guia(guia)
    mov = ((d.get("objects") or {}).get("Mov") or {}).get("InformacionMov") or []
    return mov if isinstance(mov, list) else [mov]


def saldo_cartera():
    """Saldo de cartera (GET /bff/wallet/balance) -> dict con amount, etc."""
    d = _get("/bff/wallet/balance", base=API_GO)
    return d.get("data") or {}


# --- Gestion ---

# Campos que el panel exige para crear una orden (verificado en su JS).
CAMPOS_ORDEN_OBLIGATORIOS = (
    "name", "surname", "dir", "country", "state", "city", "phone",
    "user_id", "supplier_id", "type", "rate_type", "products",
    "distributionCompany", "type_service", "warehouses_selected_id",
)


def crear_orden(payload):
    """
    Crea una orden en Dropi (POST /bff/orders). NO probado en vivo todavia:
    valida los campos obligatorios en cliente y envia el payload tal cual
    lo arma el panel. Revisar respuesta antes de usar en produccion.
    """
    faltan = [c for c in CAMPOS_ORDEN_OBLIGATORIOS if not payload.get(c)]
    if faltan:
        raise ValueError(f"Faltan campos obligatorios: {', '.join(faltan)}")
    return _post("/bff/orders", payload, base=API_GO)


def cotizar(payload):
    """Cotiza envio (POST /bff/orders/quote). Payload con el formato del panel
    (origen/bodega con stock + destino + productos)."""
    return _post("/bff/orders/quote", payload, base=API_GO)
