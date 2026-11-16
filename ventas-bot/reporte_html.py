"""
Reportes HTML genericos para Telegram (reemplazan a los CSV).

tabla(titulo, columnas, filas) arma una pagina con buscador, modo oscuro y
vista de tarjetas en el celular. Los telefonos se muestran sin el 0 inicial
(992118584) y con boton de llamada / WhatsApp.

columnas: [(clave, etiqueta, tipo)]  tipo: texto | tel | dinero | dias | estado
"""

import datetime as dt
import html
import os
import tempfile

from reporte_agencia import limpiar_telefono, TZ, WA_BASE

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--tx:#1f2328;--mut:#656d76;--bd:#d8dee4;--ok:#1a7f37;--old:#fff4e5;--bad:#cf222e}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--tx:#e6edf3;--mut:#8d96a0;--bd:#30363d;--ok:#3fb950;--old:#2d2210;--bad:#f85149}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,-apple-system,sans-serif}
h1{font-size:20px;margin:0 0 4px} p.sub{color:var(--mut);margin:0 0 12px}
.kpis{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:8px 12px}
.kpi b{display:block;font-size:18px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mut);font-size:12px}
input{width:100%;max-width:360px;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--card);color:var(--tx);margin-bottom:12px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
th,td{padding:9px 10px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;color:var(--mut);background:var(--bg)}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
a.tel{color:inherit;text-decoration:none;font-variant-numeric:tabular-nums;white-space:nowrap}
a.wa{margin-left:6px;color:var(--ok);text-decoration:none;font-weight:600}
tr.alerta{background:var(--old)} .bad{color:var(--bad);font-weight:600}
@media (max-width:760px){thead{display:none}table,tbody,tr,td{display:block;width:100%}
tr{border-bottom:1px solid var(--bd);padding:6px 0}td{border:0;padding:3px 12px}
td::before{content:attr(data-l);display:block;color:var(--mut);font-size:11px;text-transform:uppercase}}
"""


def _celda(valor, tipo):
    e = html.escape
    v = "" if valor is None else str(valor)
    if tipo == "tel":
        t = limpiar_telefono(v)
        if not t:
            return e(v) or "-"
        return (f'<a class="tel" href="tel:+593{t}">{t}</a>'
                f'<a class="wa" href="{e(WA_BASE)}593{t}" target="_blank" rel="noopener">WA</a>')
    if tipo == "dinero":
        try:
            return f"${float(v):.2f}"
        except ValueError:
            return e(v) or "-"
    if tipo == "estado":
        malo = any(x in v.upper() for x in ("NOVEDAD", "DEVOLU", "CANCEL", "RECHAZ"))
        return f'<span class="bad">{e(v)}</span>' if malo else e(v)
    return e(v) or "-"


def tabla(titulo, columnas, filas, subtitulo="", kpis=None, alerta=None):
    """
    Devuelve el HTML. kpis: [(valor, etiqueta)]. alerta: fn(fila)->bool para
    resaltar la fila (ej. muchos dias sin retirar).
    """
    ahora = dt.datetime.now(TZ)
    e = html.escape
    cab = "".join(f"<th>{e(et)}</th>" for _, et, _ in columnas)
    cuerpo = []
    for f in filas:
        clase = "fila alerta" if alerta and alerta(f) else "fila"
        celdas = "".join(
            f'<td data-l="{e(et)}" class="{"num" if tp in ("dinero", "dias") else ""}">'
            f"{_celda(f.get(k), tp)}</td>" for k, et, tp in columnas)
        cuerpo.append(f'<tr class="{clase}">{celdas}</tr>')
    kp = "".join(f'<div class="kpi"><b>{e(str(v))}</b><span>{e(et)}</span></div>'
                 for v, et in (kpis or []))
    vacio = f'<tr><td colspan="{len(columnas)}">Sin datos.</td></tr>'
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(titulo)}</title><style>{_CSS}</style></head><body>
<h1>{e(titulo)}</h1>
<p class="sub">{e(subtitulo + ' · ' if subtitulo else '')}Generado {ahora:%d/%m/%Y %H:%M}</p>
{f'<div class="kpis">{kp}</div>' if kp else ''}
<input placeholder="Buscar…" oninput="var q=this.value.toLowerCase();document.querySelectorAll('tr.fila').forEach(function(t){{t.style.display=t.textContent.toLowerCase().indexOf(q)>-1?'':'none'}})">
<table><thead><tr>{cab}</tr></thead><tbody>
{chr(10).join(cuerpo) if cuerpo else vacio}
</tbody></table></body></html>"""


def guardar(contenido, prefijo):
    """Escribe el HTML en un archivo temporal y devuelve la ruta."""
    fd, ruta = tempfile.mkstemp(prefix=f"{prefijo}_{dt.datetime.now(TZ):%Y%m%d_%H%M}_",
                                suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(contenido)
    return ruta


# ---------------------------------------------------- reportes del menu --

COLUMNAS_RETIRO = [
    ("guia", "Guía", "texto"), ("nombre", "Nombre", "texto"), ("telefono", "Celular", "tel"),
    ("ciudad", "Ciudad", "texto"), ("provincia", "Provincia", "texto"),
    ("direccion", "Dirección", "texto"), ("productos", "Productos", "texto"),
    ("valor", "Valor", "dinero"), ("estado", "Estado", "estado"),
    ("fecha_en_retiro", "En retiro desde", "texto"),
    ("dias_sin_retirar", "Días sin retirar", "dias"),
]

COLUMNAS_PEDIDOS = [
    ("pedido", "Pedido", "texto"), ("fecha", "Fecha", "texto"), ("nombre", "Nombre", "texto"),
    ("telefono", "Celular", "tel"), ("ciudad", "Ciudad", "texto"),
    ("estado", "Estado", "estado"), ("valor", "Valor", "dinero"),
    ("transportadora", "Transportadora", "texto"), ("guia", "Guía", "texto"),
]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def html_retiros(filas, subtitulo):
    con = [d for d in (_int(x.get("dias_sin_retirar")) for x in filas) if d is not None]
    kpis = [(len(filas), "pedidos en retiro"),
            (round(sum(con) / len(con), 1) if con else 0, "días promedio"),
            (max(con, default=0), "máximo sin retirar")]
    return tabla("📦 Seguimiento retiro Servientrega", COLUMNAS_RETIRO, filas, subtitulo, kpis,
                 alerta=lambda f: (_int(f.get("dias_sin_retirar")) or 0) >= 3)


def html_pedidos(filas, subtitulo, titulo="📦 Pedidos Dropi"):
    total = 0.0
    for f in filas:
        try:
            total += float(f.get("valor") or 0)
        except ValueError:
            pass
    kpis = [(len(filas), "pedidos"), (f"${total:.2f}", "valor total")]
    return tabla(titulo, COLUMNAS_PEDIDOS, filas, subtitulo, kpis,
                 alerta=lambda f: any(x in str(f.get("estado", "")).upper()
                                      for x in ("NOVEDAD", "DEVOLU")))
