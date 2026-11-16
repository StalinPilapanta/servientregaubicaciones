"""
Asistente conversacional del bot (Claude + herramientas de Chatea Pro y Dropi).

Le escribes al bot en lenguaje normal ("¿qué pedidos siguen pendientes?",
"busca a Hugo", "¿cuánto stock queda de Mega Red?") y Claude decide qué
consultar. Las consultas son de SOLO LECTURA. Lo que tiene efecto real
(crear un pedido, escribirle a un cliente) nunca lo hace el asistente: deja una
"accion" que bot.py muestra con botones para que tú confirmes.

Variables de entorno:
  ANTHROPIC_API_KEY     clave de la API de Claude (sin ella el asistente se apaga)
  ASISTENTE_MODELO      modelo (def. claude-haiku-5-5, el mas economico)
  ASISTENTE_ESFUERZO    low | medium | high (def. low)
"""

import os
import re
import json
import time
import threading
import datetime as dt

import anthropic

import chateapro
import dropi_mcp
import dropi_pedido
import agencias
import promos

MODELO = os.environ.get("ASISTENTE_MODELO", "claude-haiku-5-5")
ESFUERZO = os.environ.get("ASISTENTE_ESFUERZO", "low")
MAX_VUELTAS = 8            # llamadas a herramientas por pregunta
MAX_MENSAJES = 40          # al pasar esto se empieza una conversacion nueva
INACTIVIDAD = 30 * 60      # 30 min sin hablar -> conversacion nueva

SISTEMA = """Eres el asistente interno del equipo de ventas de VitashopEc (Ecuador).
Hablas con el dueño del negocio por Telegram. Responde en español, corto y directo,
en texto plano (sin Markdown, sin tablas). Usa listas con "•" cuando haya varios items.

Fuentes:
- Chatea Pro: el CRM de WhatsApp. Cada cliente tiene un user_ns, campos (nombre,
  teléfono, ciudad, provincia, dirección, productos escogidos, valor) y su chat.
- Dropi: donde se crean los pedidos y se ve su estado, guía, stock y envío.
- Agencias Servientrega del directorio local.

Reglas:
- Usa las herramientas para cualquier dato; nunca inventes pedidos, montos ni estados.
- Si buscas un cliente y hay varios parecidos, muéstralos y pregunta cuál.
- Tú NO creas pedidos ni envías mensajes. Para crear un pedido usa abrir_ficha
  (el usuario confirma con botones). Para escribirle a un cliente usa
  proponer_mensaje (el usuario lo revisa y pulsa Enviar). Dilo así al responder.
- Montos en dólares con 2 decimales. Fechas en formato dd/mm/aaaa.
- Si una herramienta falla, di qué falló en una línea y sugiere qué hacer."""


# ------------------------------------------------------------ herramientas --

def _tool(nombre, descripcion, props=None, requeridos=()):
    return {"name": nombre, "description": descripcion,
            "input_schema": {"type": "object", "properties": props or {},
                             "required": list(requeridos)}}


TOOLS = [
    _tool("ventas_del_dia",
          "Ventas registradas en Chatea Pro en una fecha (hoy por defecto): nombre, "
          "teléfono, productos, valor, ciudad, user_ns y ID de Dropi si ya tiene pedido.",
          {"fecha": {"type": "string", "description": "dd/mm/aaaa (opcional)"}}),
    _tool("buscar_cliente",
          "Busca clientes de Chatea Pro por teléfono o nombre entre las ventas de hoy y "
          "los chats de las últimas 24h. Devuelve user_ns, nombre, teléfono y ciudad.",
          {"texto": {"type": "string", "description": "teléfono o nombre"}}, ["texto"]),
    _tool("ficha_cliente",
          "Campos guardados en Chatea Pro de un cliente (datos de envío, productos, valor, "
          "ID de Dropi, etc.).",
          {"user_ns": {"type": "string"}}, ["user_ns"]),
    _tool("chat_cliente",
          "Últimos mensajes del chat de WhatsApp de un cliente (Cliente/Bot).",
          {"user_ns": {"type": "string"},
           "cantidad": {"type": "integer", "description": "mensajes (def. 25, máx. 60)"}},
          ["user_ns"]),
    _tool("pedidos_dropi",
          "Pedidos de Dropi, más nuevos primero. Filtra por estado (PENDIENTE, GUIA_GENERADA, "
          "EN REPARTO, ENTREGADO, DEVOLUCION, CANCELADO...) y días hacia atrás.",
          {"estado": {"type": "string"},
           "dias": {"type": "integer", "description": "def. 7"},
           "cantidad": {"type": "integer", "description": "def. 20, máx. 100"},
           "texto": {"type": "string", "description": "filtra por nombre o teléfono"}}),
    _tool("pedido_dropi",
          "Detalle de un pedido de Dropi por número de pedido o número de guía.",
          {"numero": {"type": "string"}}, ["numero"]),
    _tool("stock_producto",
          "Stock actual en Dropi de un producto (busca por nombre, ej. 'mega red').",
          {"producto": {"type": "string"}}, ["producto"]),
    _tool("cotizar_envio",
          "Costo de envío Servientrega (contra entrega) a una ciudad para una promo.",
          {"ciudad": {"type": "string"},
           "provincia": {"type": "string"},
           "promo": {"type": "string",
                     "description": "clave de promo, ej. megared:1, megared:2, drenaje:4"}},
          ["ciudad"]),
    _tool("agencias_ciudad",
          "Agencias Servientrega de una ciudad (nombre, sector, dirección, horarios).",
          {"ciudad": {"type": "string"}}, ["ciudad"]),
    _tool("agencia_cercana",
          "Agencias Servientrega más cercanas a una referencia (barrio, colegio, calle) "
          "dentro de una ciudad.",
          {"ciudad": {"type": "string"}, "referencia": {"type": "string"},
           "provincia": {"type": "string"}}, ["ciudad", "referencia"]),
    _tool("abrir_ficha",
          "Muestra al usuario la ficha de venta de un cliente con los botones para crear "
          "el pedido en Dropi (él confirma). Úsala cuando pida crear o revisar un pedido.",
          {"user_ns": {"type": "string"}}, ["user_ns"]),
    _tool("proponer_mensaje",
          "Prepara un mensaje de WhatsApp para un cliente. NO se envía: el usuario lo ve "
          "con un botón Enviar.",
          {"user_ns": {"type": "string"}, "texto": {"type": "string"}},
          ["user_ns", "texto"]),
]

_CAMPOS_UTILES = (
    chateapro.CAMPO_NOMBRE, chateapro.CAMPO_FECHA, chateapro.CAMPO_PRODUCTOS,
    chateapro.CAMPO_VALOR, chateapro.CAMPO_CIUDAD, chateapro.CAMPO_PROVINCIA,
    chateapro.CAMPO_DIRECCION, "Colonia", "Cantidad de productos", "ID de Dropi",
    "Transportadora", chateapro.CAMPO_ENTREGA, chateapro.CAMPO_RESUMEN,
)


def _dig(t):
    return re.sub(r"\D", "", str(t or ""))


def _norm(t):
    return agencias._norm(t)


def _resumen_cliente(c):
    return {
        "user_ns": c.get("_user_ns"),
        "nombre": c.get(chateapro.CAMPO_NOMBRE) or c.get("_name"),
        "telefono": c.get("_phone"),
        "ciudad": c.get(chateapro.CAMPO_CIUDAD),
        "productos": c.get(chateapro.CAMPO_PRODUCTOS),
        "valor": c.get(chateapro.CAMPO_VALOR),
        "id_dropi": c.get("ID de Dropi") or None,
    }


def _fecha(txt):
    if not txt:
        return None
    return dt.datetime.strptime(txt.strip(), "%d/%m/%Y").date()


def _pedido_corto(p):
    claves = ("order_id", "id", "status", "name", "surname", "phone", "city", "state",
              "total_order", "tracking_code", "shipping_company", "created_at", "dir")
    return {k: p.get(k) for k in claves if p.get(k) not in (None, "")}


def t_ventas_del_dia(fecha=None):
    ventas = chateapro.ventas_del_dia(_fecha(fecha))
    return {"fecha": fecha or chateapro.hoy_ecuador().strftime("%d/%m/%Y"),
            "total": len(ventas),
            "facturacion": round(sum(chateapro._valor_num(v) for v in ventas), 2),
            "ventas": [_resumen_cliente(v) for v in ventas]}


def t_buscar_cliente(texto):
    dig = _dig(texto)[-9:]
    clave = _norm(texto)
    vistos, hallados = set(), []

    def revisar(campos):
        ns = campos.get("_user_ns")
        if ns in vistos:
            return
        vistos.add(ns)
        if len(dig) >= 7:
            ok = dig in _dig(campos.get("_phone"))
        else:
            nombre = _norm(f"{campos.get(chateapro.CAMPO_NOMBRE, '')} {campos.get('_name', '')}")
            ok = bool(clave) and all(p in nombre for p in clave.split())
        if ok:
            hallados.append(_resumen_cliente(campos))

    for v in chateapro.ventas_del_dia():
        revisar(v)
    for s in chateapro.listar_suscriptores_recientes():
        if len(hallados) >= 10:
            break
        revisar(chateapro.obtener_campos(s["user_ns"]))
    return {"encontrados": hallados,
            "nota": "Solo se busca en ventas de hoy y chats de las últimas 24h."}


def t_ficha_cliente(user_ns):
    c = chateapro.obtener_campos(user_ns)
    datos = {k: c.get(k) for k in _CAMPOS_UTILES if c.get(k)}
    datos.update(user_ns=user_ns, nombre_perfil=c.get("_name"), telefono=c.get("_phone"))
    return datos


def t_chat_cliente(user_ns, cantidad=25):
    n = max(1, min(int(cantidad or 25), 60))
    return {"mensajes": chateapro.obtener_chat(user_ns, limite=n)[-n:]}


def t_pedidos_dropi(estado=None, dias=7, cantidad=20, texto=None):
    pedidos = dropi_pedido.listar_pedidos((estado or "").upper() or None,
                                          n=cantidad or 20, dias=dias or 7)
    if texto:
        dig, t = _dig(texto)[-9:], _norm(texto)
        pedidos = [p for p in pedidos
                   if (len(dig) >= 7 and dig in _dig(p.get("phone")))
                   or (t and t in _norm(f"{p.get('name', '')} {p.get('surname', '')}"))]
    return {"total": len(pedidos), "pedidos": [_pedido_corto(p) for p in pedidos]}


def t_pedido_dropi(numero):
    numero = str(numero).strip()
    det = dropi_pedido.buscar_guia(numero)
    if det is None and numero.isdigit():
        det = dropi_mcp.llamar("get_order", {"id": numero})
    if not det:
        return {"error": f"No encontré el pedido o guía {numero} en los últimos 89 días."}
    return det


def t_stock_producto(producto):
    res = dropi_mcp.llamar("list_products", {"search": producto, "get_stock": True,
                                             "page_size": 20})
    return {"productos": [{"id": p.get("id"), "nombre": p.get("name"),
                           "stock": p.get("stock"), "precio": p.get("sale_price")}
                          for p in dropi_pedido._items(res)]}


def t_cotizar_envio(ciudad, provincia="", promo="megared:1"):
    promo = promo or "megared:1"
    if not promos.valida(promo):
        return {"error": f"Promo «{promo}» no existe. Válidas: {promos.claves_validas()}"}
    destino = dropi_pedido.resolver_destino(ciudad, provincia)
    cot = dropi_pedido.cotizar(destino, promos.productos_dropi(promo))
    return {"ciudad": destino["city"], "provincia": destino["state"], "promo": promo,
            "servientrega": cot["costo"], "moneda": cot["moneda"], "error": cot["error"]}


def t_agencias_ciudad(ciudad):
    lista = agencias.de_ciudad(ciudad)
    return {"total": len(lista), "agencias": lista[:25]}


def t_agencia_cercana(ciudad, referencia, provincia=""):
    return {"cercanas": agencias.cercanas_por_referencia(ciudad, [referencia], provincia)}


EJECUTORES = {
    "ventas_del_dia": t_ventas_del_dia,
    "buscar_cliente": t_buscar_cliente,
    "ficha_cliente": t_ficha_cliente,
    "chat_cliente": t_chat_cliente,
    "pedidos_dropi": t_pedidos_dropi,
    "pedido_dropi": t_pedido_dropi,
    "stock_producto": t_stock_producto,
    "cotizar_envio": t_cotizar_envio,
    "agencias_ciudad": t_agencias_ciudad,
    "agencia_cercana": t_agencia_cercana,
}


def _ejecutar(nombre, args, acciones):
    """Corre una herramienta. Devuelve (texto_resultado, es_error)."""
    if nombre == "abrir_ficha":
        acciones.append({"tipo": "ficha", "ns": args["user_ns"]})
        return "Ficha mostrada al usuario con los botones para crear el pedido.", False
    if nombre == "proponer_mensaje":
        acciones.append({"tipo": "mensaje", "ns": args["user_ns"], "texto": args["texto"]})
        return "Borrador mostrado al usuario con el botón Enviar (aún no se envía).", False
    fn = EJECUTORES.get(nombre)
    if fn is None:
        return f"Herramienta desconocida: {nombre}", True
    try:
        res = fn(**args)
    except dropi_mcp.AuthRequired:
        acciones.append({"tipo": "conectar_dropi"})
        return "Dropi pide iniciar sesión; se le mostró el botón al usuario.", True
    except Exception as e:
        return f"Error en {nombre}: {e}", True
    texto = json.dumps(res, ensure_ascii=False, default=str)
    if len(texto) > 15000:
        texto = texto[:15000] + " …(recortado)"
    return texto, False


# --------------------------------------------------------- conversaciones --

_conversaciones = {}   # chat_id -> {"mensajes": [...], "ts": float}
_lock = threading.Lock()
_cliente = None


def disponible():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    global _cliente
    if _cliente is None:
        _cliente = anthropic.Anthropic()
    return _cliente


def olvidar(chat_id):
    with _lock:
        _conversaciones.pop(chat_id, None)


def _historial(chat_id):
    with _lock:
        c = _conversaciones.get(chat_id)
        if (c is None or time.time() - c["ts"] > INACTIVIDAD
                or len(c["mensajes"]) > MAX_MENSAJES):
            # Conversacion nueva en vez de recortar: el historial solo se agrega,
            # nunca se edita (asi los bloques de razonamiento siguen validos).
            c = {"mensajes": []}
            _conversaciones[chat_id] = c
        c["ts"] = time.time()
        return c["mensajes"]


def responder(chat_id, texto):
    """
    Procesa un mensaje del usuario. Devuelve (respuesta, acciones).
    acciones: [{"tipo": "ficha"|"mensaje"|"conectar_dropi", ...}]
    """
    mensajes = _historial(chat_id)
    hoy = chateapro.hoy_ecuador().strftime("%d/%m/%Y")
    mensajes.append({"role": "user", "content": f"[Hoy es {hoy}] {texto}"})
    acciones = []

    for _ in range(MAX_VUELTAS):
        try:
            r = _client().messages.create(
                model=MODELO,
                max_tokens=4096,
                system=SISTEMA,
                tools=TOOLS,
                output_config={"effort": ESFUERZO},
                cache_control={"type": "ephemeral"},
                messages=mensajes,
            )
        except anthropic.AuthenticationError:
            olvidar(chat_id)
            return "❌ La ANTHROPIC_API_KEY no es válida.", acciones
        except anthropic.RateLimitError:
            olvidar(chat_id)
            return "⏳ Claude está saturado, intenta en un minuto.", acciones
        except anthropic.APIStatusError as e:
            olvidar(chat_id)
            return f"❌ Error de Claude ({e.status_code}): {e.message}", acciones
        except anthropic.APIConnectionError:
            olvidar(chat_id)
            return "❌ No pude conectar con Claude. Intenta de nuevo.", acciones

        # Se guarda el contenido completo (incluye bloques de razonamiento).
        mensajes.append({"role": "assistant", "content": r.content})

        if r.stop_reason == "refusal":
            olvidar(chat_id)
            return "No puedo ayudar con eso.", acciones
        if r.stop_reason != "tool_use":
            texto_r = "\n".join(b.text for b in r.content if b.type == "text").strip()
            return texto_r or "Listo.", acciones

        resultados = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            salida, error = _ejecutar(b.name, dict(b.input or {}), acciones)
            resultados.append({"type": "tool_result", "tool_use_id": b.id,
                               "content": salida, "is_error": error})
        mensajes.append({"role": "user", "content": resultados})

    olvidar(chat_id)
    return ("Me tomó demasiados pasos responder eso. Pregúntame algo más concreto.",
            acciones)
