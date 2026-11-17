"""
Alertas diarias de Dropi:

1) Novedades y devoluciones: pedidos en NOVEDAD / DEVOLUCION de los ultimos
   30 dias. Avisa los nuevos (los ya avisados se recuerdan en el estado) y deja
   preparada la plantilla de novedad para enviarla con un boton.

2) Recordatorio de retiro en agencia: a los RECORDATORIO_DIAS dias de ver un
   pedido en "PARA RETIRO EN AGENCIA SERVIENTREGA" se le envia al cliente la
   plantilla aprobada de WhatsApp (funciona aunque pasen mas de 24h). Una sola
   vez por pedido.

Variables de entorno:
  ALERTAS_ESTADO_PATH   archivo de estado (usar volumen; def. alertas_estado.json)
  RECORDATORIO_DIAS     dias en agencia antes de recordar (def. 2)
  RECORDATORIO_MODO     off (def., desactivado) | auto (envia solo) | confirmar (boton)
  PLANTILLA_RETIRO      def. seguimiento_recordatorio_en_oficina_v2_utilidad_new
                        ({{1}} nombre, {{2}} producto, {{3}} agencia, {{4}} ciudad)
  PLANTILLA_NOVEDAD     def. novedad_recordatorio_v2_utilidad ({{1}} producto)
"""

import datetime as dt
import json
import os
import threading

import chateapro
import dropi_mcp
import dropi_pedido
import reporte_agencia
import datos

RUTA_ESTADO = os.environ.get(
    "ALERTAS_ESTADO_PATH",
    datos.ruta("alertas_estado.json"))
RECORDATORIO_DIAS = int(os.environ.get("RECORDATORIO_DIAS", "2"))
RECORDATORIO_MODO = os.environ.get("RECORDATORIO_MODO", "off").lower()  # desactivado por ahora
PLANTILLA_RETIRO = os.environ.get("PLANTILLA_RETIRO",
                                  "seguimiento_recordatorio_en_oficina_v2_utilidad_new")
PLANTILLA_NOVEDAD = os.environ.get("PLANTILLA_NOVEDAD", "novedad_recordatorio_v2_utilidad")

ESTADOS_NOVEDAD = ("NOVEDAD",)
ESTADOS_DEVOLUCION = ("DEVOLUCION", "DEVOLUCION EN TRANSITO")

_lock = threading.Lock()


# ---------------------------------------------------------------- estado --

def _leer():
    try:
        with open(RUTA_ESTADO, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("novedades", {})
    d.setdefault("retiro", {})
    return d


def _guardar(d):
    tmp = RUTA_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RUTA_ESTADO)


def _hoy():
    return chateapro.hoy_ecuador()


def _primer_nombre(nombre):
    n = (nombre or "").strip().split(" ")[0]
    return n.capitalize() if n else "cliente"


def _producto_de(det):
    items = det.get("items") or []
    return ", ".join(f"{i.get('product_name', '')}".strip() for i in items) or "su pedido"


# ----------------------------------------------- novedades y devoluciones --

def _listar(estados, dias=30):
    out = []
    for est in estados:
        out.extend(dropi_pedido.listar_pedidos(est, n=100, dias=dias))
    return out


def _fila(o):
    return {"pedido": o.get("order_id"), "fecha": str(o.get("created_at") or "")[:10],
            "nombre": o.get("customer_name"), "telefono": o.get("customer_phone"),
            "ciudad": o.get("city"), "estado": o.get("status"), "valor": o.get("total"),
            "transportadora": o.get("shipping_company"),
            "guia": o.get("tracking_code") or ""}


def revisar_novedades(solo_nuevas=True):
    """
    Devuelve {"novedades": [filas], "devoluciones": [filas], "nuevas": n}.
    Con solo_nuevas, filtra los pedidos que ya se avisaron antes.
    """
    novedades = [_fila(o) for o in _listar(ESTADOS_NOVEDAD)]
    devoluciones = [_fila(o) for o in _listar(ESTADOS_DEVOLUCION)]
    with _lock:
        est = _leer()
        vistos = est["novedades"]
        hoy = _hoy().isoformat()
        nuevas_n = [f for f in novedades if f"{f['pedido']}:{f['estado']}" not in vistos]
        nuevas_d = [f for f in devoluciones if f"{f['pedido']}:{f['estado']}" not in vistos]
        for f in nuevas_n + nuevas_d:
            vistos[f"{f['pedido']}:{f['estado']}"] = hoy
        _guardar(est)
    if solo_nuevas:
        novedades, devoluciones = nuevas_n, nuevas_d
    return {"novedades": novedades, "devoluciones": devoluciones,
            "nuevas": len(nuevas_n) + len(nuevas_d)}


def enviar_novedad(order_id):
    """Envia la plantilla de novedad al cliente de ese pedido. (ok, detalle)."""
    det = dropi_mcp.llamar("get_order", {"id": str(order_id)})
    cli = det.get("customer") or {}
    return chateapro.enviar_plantilla(cli.get("phone"), PLANTILLA_NOVEDAD, [_producto_de(det)])


# ------------------------------------------------- recordatorio de retiro --

def _params_retiro(p):
    ag = reporte_agencia.info_agencia(p["direccion_dropi"], p["ciudad"])
    agencia = ag["nombre"].split(" - ", 1)[-1].title() if ag.get("nombre") else "Servientrega"
    return [_primer_nombre(p["nombre"]), p["producto"], f"Servientrega {agencia}",
            reporte_agencia._bonito(p["ciudad"])]


def revisar_retiros(enviar=None):
    """
    Revisa los pedidos en agencia y decide a quien recordar.
      enviar=None -> segun RECORDATORIO_MODO; True/False lo fuerza.
    Devuelve {"en_agencia": n, "pendientes": [p], "enviados": [p], "fallidos": [(p, error)]}
    Cada p trae: order_id, nombre, telefono, ciudad, guia, producto, dias.
    """
    if enviar is None:
        enviar = RECORDATORIO_MODO == "auto"
    pedidos = reporte_agencia.obtener_pedidos()
    hoy = _hoy()
    pendientes = []
    with _lock:
        est = _leer()
        reg = est["retiro"]
        actuales = set()
        for p in pedidos:
            oid = p["order_id"]
            actuales.add(oid)
            r = reg.setdefault(oid, {"visto": hoy.isoformat(), "recordado": ""})
            p["dias"] = (hoy - dt.date.fromisoformat(r["visto"])).days
            if not r["recordado"] and p["dias"] >= RECORDATORIO_DIAS:
                pendientes.append(p)
        # Los que ya no estan en agencia (retirados o devueltos) salen del registro.
        for oid in [k for k in reg if k not in actuales]:
            reg.pop(oid)
        _guardar(est)

    enviados, fallidos = [], []
    if enviar:
        enviados, fallidos = enviar_recordatorios(pendientes)
        pendientes = []
    return {"en_agencia": len(pedidos), "pendientes": pendientes,
            "enviados": enviados, "fallidos": fallidos}


def enviar_recordatorios(pedidos):
    enviados, fallidos = [], []
    for p in pedidos:
        ok, det = chateapro.enviar_plantilla(p["telefono"], PLANTILLA_RETIRO, _params_retiro(p))
        if ok:
            enviados.append(p)
            with _lock:
                est = _leer()
                est["retiro"].setdefault(p["order_id"], {"visto": _hoy().isoformat()})
                est["retiro"][p["order_id"]]["recordado"] = _hoy().isoformat()
                _guardar(est)
        else:
            fallidos.append((p, det))
    return enviados, fallidos


def probar_plantilla(telefono):
    """Envia la plantilla de retiro con datos de ejemplo a TU numero."""
    return chateapro.enviar_plantilla(
        telefono, PLANTILLA_RETIRO,
        ["Prueba", "Mega Red", "Servientrega Centro", "Loja"])
