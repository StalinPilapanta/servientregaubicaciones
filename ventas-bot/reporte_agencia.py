"""
Reporte "Pedidos en agencia Servientrega" (datos del MCP oficial de Dropi).

  1. list_orders(status="PARA RETIRO EN AGENCIA SERVIENTREGA") ultimos 88 dias
  2. get_order de cada pedido (direccion de la agencia + productos)
  3. Horario de la agencia desde directorio_limpio.json (directorio Servientrega)
  4. HTML ordenado del MAS ANTIGUO al MAS NUEVO, con boton de WhatsApp por cliente

Variables de entorno:
  DIRECTORIO_AGENCIAS_PATH  def. directorio_limpio.json junto al codigo
  WHATSAPP_LINK_BASE        def. https://wa.me/  (usa https://web.whatsapp.com/send?phone=
                            para abrir directo WhatsApp Web)
  HORARIO_AGENCIA_DEFECTO   horario si la agencia no esta en el directorio
"""

import datetime as dt
import html
import json
import os
import re
import tempfile
import unicodedata
from urllib.parse import quote

import dropi_mcp

ESTADO = "PARA RETIRO EN AGENCIA SERVIENTREGA"
DIAS_ATRAS = 88  # list_orders admite maximo 90 dias por consulta
TZ = dt.timezone(dt.timedelta(hours=-5))  # Ecuador

DIRECTORIO_PATH = os.environ.get(
    "DIRECTORIO_AGENCIAS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "directorio_limpio.json"),
)
WA_BASE = os.environ.get("WHATSAPP_LINK_BASE", "https://wa.me/")
HORARIO_DEFECTO = os.environ.get(
    "HORARIO_AGENCIA_DEFECTO", "lunes a viernes 09:00–18:00, sábados 09:00–13:00")

_directorio = None


# --------------------------------------------------------------- utilidades --

def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.upper()).strip()


def _bonito(t):
    t = (t or "").strip().title()
    for a in (" Y ", " E ", " De ", " Del ", " La ", " El ", " Al ", " A "):
        t = t.replace(a, a.lower())
    return t


def limpiar_telefono(p):
    d = re.sub(r"\D", "", str(p or ""))
    if d.startswith("593"):
        d = d[3:]
    return d.lstrip("0")[-9:]


def _directorio_por_nombre():
    global _directorio
    if _directorio is None:
        try:
            with open(DIRECTORIO_PATH, encoding="utf-8") as f:
                datos = json.load(f)
            _directorio = {_norm(a.get("Nombre_agencia")): a for a in datos}
        except (OSError, ValueError) as e:
            print("No se pudo leer el directorio de agencias:", e)
            _directorio = {}
    return _directorio


def _hora(h):
    """'08H30 A 12H30 - 13H30 A 16H30' -> '08:30–12:30 y 13:30–16:30'."""
    h = _norm(h)
    if not h or h in ("NO", "NO ATIENDE", "NO LABORA", "CERRADO", "-", "N/A"):
        return ""
    h = re.sub(r"(\d{1,2})H(\d{2})", r"\1:\2", h)
    h = re.sub(r"\s*A\s*(?=\d)", "–", h)
    return re.sub(r"\s*-\s*|\s+Y\s+", " y ", h)


def partir_direccion(dir_dropi):
    """
    Dropi guarda la direccion de retiro como 'CIUDAD_AGENCIA---SECTOR---CALLE'.
    Devuelve (clave_agencia, nombre_agencia, sector, calle).
    """
    d = (dir_dropi or "").replace("RETIRO EN AGENCIA:", "").strip()
    d = re.sub(r"[—–]", "-", d)  # algunas guias traen guiones largos: 'X—-CENTRO—-...'
    partes = [p.strip("- ").strip() for p in re.split(r"-{2,}", d) if p.strip("- ")]
    primero = partes[0] if partes else ""
    agencia = primero.split("_", 1)[1].strip() if "_" in primero else ""
    sector = partes[1] if len(partes) >= 3 else ""
    calle = partes[-1] if len(partes) >= 2 else primero.split("_", 1)[-1]
    return primero, agencia, sector, calle


def info_agencia(dir_dropi, ciudad):
    """Agencia + direccion + horario (directorio Servientrega si existe)."""
    clave, agencia, sector, calle = partir_direccion(dir_dropi)
    a = _directorio_por_nombre().get(_norm(clave))
    direccion = _bonito(calle) + (f" ({sector.lower()})" if sector else "")
    if a:
        lv = _hora(a.get("Horario_Lunes_Viernes"))
        fs = _hora(a.get("Horario_fin_semana"))
        partes = []
        if lv:
            partes.append(f"lunes a viernes {lv}")
        if fs:
            partes.append(f"fin de semana {fs}")
        elif lv:
            partes.append("fin de semana cerrado")
        horario = "; ".join(partes)
        return {
            "nombre": f"{_norm(ciudad) or a.get('Ciudad', '')} - {agencia or a.get('Sector', '')}".strip(" -"),
            "direccion": direccion or _bonito(a.get("Direccion")),
            "horario": horario or HORARIO_DEFECTO,
            "telefono": a.get("Telefono") or "",
            "en_directorio": True,
        }
    return {
        "nombre": f"{_norm(ciudad)} - {agencia}".strip(" -") if agencia else _norm(ciudad),
        "direccion": direccion,
        "horario": HORARIO_DEFECTO,
        "telefono": "",
        "en_directorio": False,
    }


# ------------------------------------------------------------------- datos --

def _lista(d, *claves):
    for k in claves:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, list):
            return v
    return []


def obtener_pedidos(progreso=None):
    """Pedidos en agencia (con detalle), del mas antiguo al mas nuevo."""
    hoy = dt.datetime.now(TZ).date()
    desde = (hoy - dt.timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + dt.timedelta(days=1)).isoformat()

    ordenes, start = [], 0
    while True:
        r = dropi_mcp.llamar("list_orders", {
            "status": ESTADO, "from": desde, "until": hasta,
            "result_number": 100, "start": start,
        })
        items = _lista(r, "items", "orders", "objects", "data")
        ordenes.extend(items)
        if len(items) < 100:
            break
        start += 100

    pedidos = []
    for i, o in enumerate(ordenes, 1):
        if progreso and i % 10 == 0:
            progreso(i, len(ordenes))
        oid = str(o.get("order_id") or o.get("id") or "")
        try:
            det = dropi_mcp.llamar("get_order", {"id": oid}) if oid else {}
        except dropi_mcp.AuthRequired:
            raise
        except Exception as e:
            print(f"get_order {oid} fallo: {e}")
            det = {}
        cli = det.get("customer") or {}
        productos = ", ".join(
            f"{p.get('product_name', '')}" + (f" x{p.get('qty')}" if p.get("qty") not in (None, "", 1) else "")
            for p in _lista(det, "items", "products")
        )
        pedidos.append({
            "order_id": oid,
            "nombre": cli.get("name") or o.get("customer_name") or "",
            "telefono": limpiar_telefono(cli.get("phone") or o.get("customer_phone")),
            "ciudad": cli.get("city") or o.get("city") or "",
            "direccion_dropi": cli.get("address") or "",
            "guia": str(det.get("tracking_code") or o.get("tracking_code") or ""),
            "producto": productos or "-",
            "total": det.get("total") or o.get("total") or "",
            "creado": str(det.get("created_at") or o.get("created_at") or ""),
        })

    pedidos.sort(key=lambda p: (p["creado"], p["order_id"]))  # mas antiguo primero
    return pedidos


# ---------------------------------------------------------------- mensaje --

def mensaje_whatsapp(p, ag):
    primer_nombre = _bonito((p["nombre"] or "").split(" ")[0])
    saludo = f"Hola {primer_nombre} 👋\n" if primer_nombre else ""
    return (
        f"{saludo}"
        "📦 *Servientrega* le confirma que su producto "
        f"*{p['producto']}* ya se encuentra disponible en la agencia.\n\n"
        f"🏢 Agencia: {ag['nombre']}\n"
        f"📍 Dirección: {ag['direccion']}\n"
        f"🔖 Guía: {p['guia']}\n"
        f"🕒 Horario: {ag['horario']}\n\n"
        "Para retirarlo solo indique su número de guía en la agencia.\n"
        "¿Nos confirma si lo va a retirar el día de hoy? 🙌"
    )


def _dias(creado):
    try:
        f = dt.date.fromisoformat(creado[:10])
        return (dt.datetime.now(TZ).date() - f).days
    except ValueError:
        return None


# ------------------------------------------------------------------- HTML --

def generar_html(pedidos):
    ahora = dt.datetime.now(TZ)
    filas = []
    for i, p in enumerate(pedidos, 1):
        ag = info_agencia(p["direccion_dropi"], p["ciudad"])
        url = f"{WA_BASE}593{p['telefono']}" + ("&" if "?" in WA_BASE else "?") + \
              "text=" + quote(mensaje_whatsapp(p, ag))
        dias = _dias(p["creado"])
        dias_txt = f"{dias} d" if dias is not None else "-"
        alerta = " viejo" if (dias or 0) >= 7 else ""
        e = html.escape
        filas.append(f"""<tr class="fila{alerta}" data-guia="{e(p['guia'])}">
<td class="n">{i}</td>
<td><span class="fecha">{e(p['creado'][:10])}</span><span class="dias">{dias_txt}</span></td>
<td class="cli"><b>{e(p['nombre'])}</b><a class="tel" href="tel:+593{e(p['telefono'])}">{e(p['telefono'])}</a></td>
<td>{e(p['ciudad'])}<small>{e(ag['nombre'])}<br>{e(ag['direccion'])}<br>🕒 {e(ag['horario'])}</small></td>
<td class="guia">{e(p['guia'])}<small>#{e(p['order_id'])}</small></td>
<td>{e(p['producto'])}<small>${e(str(p['total']))}</small></td>
<td><a class="wa" href="{e(url)}" target="_blank" rel="noopener">Enviar WhatsApp</a></td>
</tr>""")

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pedidos en agencia Servientrega</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1f2328;--mut:#656d76;--bd:#d8dee4;--wa:#1a7f37;--wa2:#6e7781;--old:#fff4e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--card:#161b22;--tx:#e6edf3;--mut:#8d96a0;--bd:#30363d;--wa:#238636;--wa2:#484f58;--old:#2d2210}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,-apple-system,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}}
p.sub{{color:var(--mut);margin:0 0 12px}}
input{{width:100%;max-width:360px;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--card);color:var(--tx);margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}}
th,td{{padding:10px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;color:var(--mut);background:var(--bg)}}
small{{display:block;color:var(--mut);font-size:12px;margin-top:2px}}
.fecha,.dias,.tel{{display:block}} .dias{{font-weight:600}}
.tel{{color:inherit;text-decoration:none;font-variant-numeric:tabular-nums}}
.guia{{font-variant-numeric:tabular-nums;white-space:nowrap}}
tr.viejo{{background:var(--old)}}
.wa{{display:inline-block;background:var(--wa);color:#fff;padding:8px 12px;border-radius:8px;text-decoration:none;font-weight:600;white-space:nowrap}}
tr.enviado .wa{{background:var(--wa2)}} tr.enviado .wa::after{{content:" ✓"}}
@media (max-width:760px){{
  thead{{display:none}} table,tbody,tr,td{{display:block;width:100%}}
  tr{{border-bottom:1px solid var(--bd);padding:8px 0}} td{{border:0;padding:4px 12px}}
  td.n{{font-weight:700}} .wa{{width:100%;text-align:center;padding:12px}}
}}
</style></head><body>
<h1>📦 Pedidos en agencia Servientrega ({len(pedidos)})</h1>
<p class="sub">Generado {ahora:%d/%m/%Y %H:%M} · del más antiguo al más nuevo · resaltados: 7+ días desde el pedido</p>
<input id="q" placeholder="Buscar nombre, celular, ciudad o guía…" oninput="filtrar(this.value)">
<table><thead><tr><th>#</th><th>Pedido</th><th>Nombre y apellido / Celular</th><th>Ciudad / Agencia</th><th>Guía</th><th>Producto</th><th>WhatsApp</th></tr></thead>
<tbody>
{chr(10).join(filas) if filas else '<tr><td colspan="7">No hay pedidos en agencia.</td></tr>'}
</tbody></table>
<script>
var K='enviados_agencia';
function leer(){{try{{return JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{return {{}}}}}}
function marcar(){{var s=leer();document.querySelectorAll('tr.fila').forEach(function(t){{if(s[t.dataset.guia])t.classList.add('enviado')}})}}
document.querySelectorAll('a.wa').forEach(function(a){{a.addEventListener('click',function(){{
  var t=a.closest('tr');t.classList.add('enviado');try{{var s=leer();s[t.dataset.guia]=Date.now();localStorage.setItem(K,JSON.stringify(s))}}catch(e){{}}
}})}});
function filtrar(q){{q=q.toLowerCase();document.querySelectorAll('tr.fila').forEach(function(t){{t.style.display=t.textContent.toLowerCase().indexOf(q)>-1?'':'none'}})}}
marcar();
</script>
</body></html>"""


def generar(progreso=None):
    """Devuelve (ruta_html_temporal, pedidos). Lanza dropi_mcp.AuthRequired."""
    pedidos = obtener_pedidos(progreso)
    fd, ruta = tempfile.mkstemp(
        prefix=f"agencia_servientrega_{dt.datetime.now(TZ):%Y%m%d_%H%M}_", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(generar_html(pedidos))
    return ruta, pedidos


def resumen_telegram(pedidos, maximo=25):
    if not pedidos:
        return "✅ No hay pedidos en agencia Servientrega."
    lineas = [f"📦 Pedidos en agencia Servientrega: {len(pedidos)}",
              "(del más antiguo al más nuevo)\n"]
    for i, p in enumerate(pedidos[:maximo], 1):
        d = _dias(p["creado"])
        lineas.append(f"{i}. {p['nombre']} — {p['telefono']} — {p['ciudad']} — "
                      f"guía {p['guia']} — {p['producto']}"
                      + (f" — {d} d" if d is not None else ""))
    if len(pedidos) > maximo:
        lineas.append(f"... y {len(pedidos) - maximo} más (ver HTML).")
    return "\n".join(lineas)
