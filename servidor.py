"""
Servicio HTTP para conectar el buscador de agencias Servientrega con
Chatea Pro (UChat).

Expone DOS interfaces sobre el mismo buscador:

  1) REST simple (para webhooks / pruebas):
       GET  /salud
       GET  /buscar?texto=<direccion del cliente>
       POST /buscar   body JSON: {"texto": "..."}

  2) MCP (Model Context Protocol, Streamable HTTP / JSON-RPC 2.0):
       POST /mcp
       GET  /mcp   (handshake SSE opcional; aqui respondemos vacio)
     Pensado para conectarse como "AI MCP Server" al agente de IA de
     Chatea Pro. Expone una sola herramienta: buscar_agencia(texto).

Como levantarlo:
    pip3 install flask
    python3 servidor.py        # http://localhost:8000

En Chatea Pro -> AI Agent -> MCP Servers:
    Server URL: https://servientrega.neolabsgroup.io/mcp
    (sin auth, o bearer si lo activas con la variable MCP_BEARER_TOKEN)
"""

import os

from flask import Flask, request, jsonify, Response

from buscar_agencia import cargar_directorio, buscar

app = Flask(__name__)

# Cargamos el directorio UNA sola vez al arrancar (mas rapido por request).
AGENCIAS = cargar_directorio()
print(f"Directorio cargado: {len(AGENCIAS)} agencias.")

# Token opcional para proteger el MCP. Si esta vacio, el MCP queda abierto.
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "").strip()

# Version del protocolo MCP que soportamos.
MCP_PROTOCOL_VERSION = "2024-11-05"


# ─────────────────────────────── REST simple ───────────────────────────────

@app.get("/salud")
def salud():
    return jsonify({"ok": True, "agencias": len(AGENCIAS)})


@app.route("/buscar", methods=["GET", "POST"])
def buscar_endpoint():
    if request.method == "POST":
        datos = request.get_json(silent=True) or {}
        texto = datos.get("texto", "")
    else:
        texto = request.args.get("texto", "")

    texto = (texto or "").strip()
    if not texto:
        return jsonify({
            "ciudad_detectada": None,
            "resultados": [],
            "mensaje": ("Por favor escribeme tu ciudad y sector para asignarte "
                        "la oficina Servientrega mas cercana."),
        })

    resultado = buscar(texto, agencias=AGENCIAS, limite=3)
    return jsonify(resultado)


# ─────────────────────────────── MCP (JSON-RPC) ─────────────────────────────

# Definicion de la unica herramienta que expone este MCP.
TOOL_BUSCAR_AGENCIA = {
    "name": "buscar_agencia",
    "description": (
        "Busca la agencia Servientrega mas cercana a partir de la ciudad y el "
        "sector/barrio o calles que escribe el cliente en Ecuador. Usar SIEMPRE "
        "que el cliente indique su ciudad o zona para el retiro del pedido. "
        "Devuelve el nombre de la agencia, el sector y la direccion exacta para "
        "registrarlos como datos de entrega. No inventes agencias: usa SIEMPRE "
        "esta herramienta."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "texto": {
                "type": "string",
                "description": (
                    "Ciudad y sector/barrio o calles del cliente, tal como lo "
                    "escribio. Ej: 'Huaquillas centro', 'Quito, Quitumbe', "
                    "'Guayaquil sauces 6'."
                ),
            }
        },
        "required": ["texto"],
    },
}


def _mcp_auth_ok():
    """Si hay token configurado, exige Authorization: Bearer <token>."""
    if not MCP_BEARER_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return auth.strip() == f"Bearer {MCP_BEARER_TOKEN}"


def _jsonrpc_result(req_id, result):
    return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_error(req_id, code, message):
    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


def _ejecutar_buscar_agencia(argumentos):
    """Corre el buscador y arma un texto claro para que lo use el agente IA."""
    texto = (argumentos or {}).get("texto", "")
    texto = (texto or "").strip()
    if not texto:
        return {
            "ok": False,
            "mensaje": "Falta la ciudad y el sector del cliente.",
            "agencias": [],
        }

    # limite=5: cuando el cliente da ciudad+sector devolvemos hasta 5 cercanas.
    # Cuando da solo la ciudad, el buscador lista todas (hasta max_ciudad=25).
    resultado = buscar(texto, agencias=AGENCIAS, limite=5)
    agencias = [
        {
            "nombre": r["nombre"],
            "sector": r["sector"],
            "direccion": r["direccion"],
            "ciudad": r["ciudad"],
            "telefono": r["telefono"],
            "horario_lun_vie": r["horario_lun_vie"],
            "entrega_en_oficina": r["entrega_en_oficina"],
        }
        for r in resultado.get("resultados", [])
    ]
    return {
        "ok": bool(agencias),
        "ciudad_detectada": resultado.get("ciudad_detectada"),
        "modo": resultado.get("modo"),
        "total_ciudad": resultado.get("total_ciudad", 0),
        "agencias": agencias,
        "mensaje": resultado.get("mensaje", ""),
    }


def _handle_mcp_rpc(payload):
    """Procesa un unico mensaje JSON-RPC y devuelve una respuesta Flask."""
    req_id = payload.get("id")
    metodo = payload.get("method")
    params = payload.get("params") or {}

    # Notificaciones (sin id): no requieren respuesta con contenido.
    if metodo and metodo.startswith("notifications/"):
        return Response(status=202)

    if metodo == "initialize":
        return _jsonrpc_result(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "servientrega-agencias",
                "version": "1.0.0",
            },
        })

    if metodo == "ping":
        return _jsonrpc_result(req_id, {})

    if metodo == "tools/list":
        return _jsonrpc_result(req_id, {"tools": [TOOL_BUSCAR_AGENCIA]})

    if metodo == "tools/call":
        nombre = params.get("name")
        argumentos = params.get("arguments") or {}
        if nombre != "buscar_agencia":
            return _jsonrpc_error(req_id, -32602,
                                  f"Herramienta desconocida: {nombre}")
        salida = _ejecutar_buscar_agencia(argumentos)
        import json as _json
        return _jsonrpc_result(req_id, {
            "content": [
                {"type": "text", "text": _json.dumps(salida, ensure_ascii=False)}
            ],
            "isError": not salida.get("ok", False),
        })

    return _jsonrpc_error(req_id, -32601, f"Metodo no soportado: {metodo}")


@app.route("/mcp", methods=["GET", "POST"])
def mcp_endpoint():
    # Handshake GET (algunos clientes abren un stream SSE). No mantenemos
    # estado de sesion: respondemos 200 vacio para no bloquear.
    if request.method == "GET":
        return Response("", status=200, mimetype="text/event-stream")

    if not _mcp_auth_ok():
        return _jsonrpc_error(None, -32001, "No autorizado"), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return _jsonrpc_error(None, -32700, "JSON invalido"), 400

    # Soporta batch (lista de mensajes) o un unico mensaje.
    if isinstance(payload, list):
        respuestas = []
        import json as _json
        for mensaje in payload:
            r = _handle_mcp_rpc(mensaje)
            # Solo acumulamos respuestas con cuerpo JSON (ignora 202 de notifs).
            if r.status_code == 200:
                respuestas.append(_json.loads(r.get_data(as_text=True)))
        return jsonify(respuestas) if respuestas else Response(status=202)

    return _handle_mcp_rpc(payload)


if __name__ == "__main__":
    # Solo para desarrollo local. En produccion lo sirve Gunicorn (ver Dockerfile).
    puerto = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=puerto, debug=False)
