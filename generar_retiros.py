#!/usr/bin/env python3
"""Genera el listado de pedidos 'PARA RETIRO EN AGENCIA SERVIENTREGA'.

Uso:
    python3 generar_retiros.py pedidos.json

pedidos.json es una lista de objetos:
    {"order_id": "...", "name": "...", "phone": "...", "city": "...",
     "address": "...", "guia": "..."}

Salidas (en servientrega/reportes/):
    - whatsapp_retiros_YYYYMMDD.html : botones para WhatsApp Web
    - retiros_YYYYMMDD.json          : snapshot del dia (para detectar nuevos)
    - imprime por stdout el resumen para Telegram/chat
"""
import glob
import html
import json
import os
import sys
from datetime import date
from urllib.parse import quote

HORARIO = "lunes a viernes 09:00–18:00, sábados 09:00–13:00"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reportes")


def limpiar_telefono(p):
    d = "".join(c for c in str(p) if c.isdigit())
    if d.startswith("593"):
        d = d[3:]
    return d.lstrip("0")[-9:]


def bonito(texto):
    t = texto.strip().title()
    for a in (" Y ", " E ", " De ", " Del ", " La ", " El "):
        t = t.replace(a, a.lower())
    return t


def formatear_direccion(addr):
    """Dropi: 'CIUDAD_SECTOR---ZONA---CALLE' -> 'Calle (zona)'."""
    addr = addr.replace("RETIRO EN AGENCIA:", "").strip()
    partes = [p.strip("- ").strip() for p in addr.split("---") if p.strip("- ")]
    if len(partes) >= 3:
        return f"{bonito(partes[2])} ({partes[1].lower()})"
    if len(partes) == 2:
        return f"{bonito(partes[1])}"
    return bonito(addr.split("_", 1)[-1])


def nombre_agencia(addr):
    """Dropi: 'CIUDAD_AGENCIA---ZONA---CALLE' -> 'AGENCIA' (vacío si no existe)."""
    addr = addr.replace("RETIRO EN AGENCIA:", "").strip()
    primero = addr.split("---")[0]
    return primero.split("_", 1)[1].strip() if "_" in primero else ""


def mensaje(p):
    agencia = nombre_agencia(p["address"])
    destino = f"{p['city'].upper()} - {agencia}" if agencia else p["city"].upper()
    return (
        "📦 Servientrega le informa que su pedido ya se encuentra disponible en la agencia de\n"
        f"📍 {destino},\n"
        f"ubicación: {formatear_direccion(p['address'])}.\n"
        f"🔖 Guía: {p['guia']}\n"
        f"🕒 Horario referencial: {HORARIO}\n"
        "¿Contamos con su retiro el día de hoy? 🙌"
    )


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as f:
        pedidos = json.load(f)

    os.makedirs(OUT_DIR, exist_ok=True)
    hoy = date.today().strftime("%Y%m%d")

    # Snapshot anterior (el mas reciente que no sea de hoy) para marcar nuevos
    previos = sorted(g for g in glob.glob(os.path.join(OUT_DIR, "retiros_*.json"))
                     if not g.endswith(f"retiros_{hoy}.json"))
    guias_previas = set()
    if previos:
        with open(previos[-1], encoding="utf-8") as f:
            guias_previas = {x["guia"] for x in json.load(f)}

    filas, lineas = [], []
    for i, p in enumerate(pedidos, 1):
        tel = limpiar_telefono(p["phone"])
        nuevo = bool(guias_previas) and p["guia"] not in guias_previas
        url = f"https://web.whatsapp.com/send?phone=593{tel}&text={quote(mensaje(p))}"
        etiqueta = " 🆕" if nuevo else ""
        filas.append(
            f"<tr><td>{i}</td><td>{html.escape(p['name'])}{etiqueta}</td><td>0{tel}</td>"
            f"<td>{html.escape(p['city'])}</td><td>{p['guia']}</td>"
            f"<td><a class=\"b\" href=\"{url}\" target=\"_blank\" rel=\"noopener\">"
            "Enviar por WhatsApp</a></td></tr>"
        )
        lineas.append(f"{i}. {p['name']} - 0{tel} - {p['city']} - {p['guia']}{etiqueta}")

    pagina = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Retiros en agencia Servientrega</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f4f4f4}}
.b{{background:#1a7f37;color:#fff;padding:6px 12px;border-radius:6px;text-decoration:none;display:inline-block}}
.b:visited{{background:#6b7280}}
</style></head><body>
<h1>Pedidos para retiro en agencia ({len(pedidos)}) - {date.today():%d/%m/%Y}</h1>
<p>Clic en cada botón: se abre WhatsApp Web con el mensaje listo. Revisa y envía.</p>
<table><thead><tr><th>#</th><th>Cliente</th><th>Teléfono</th><th>Ciudad</th><th>Guía</th><th>Acción</th></tr></thead>
<tbody>
{chr(10).join(filas)}
</tbody></table></body></html>"""

    ruta_html = os.path.join(OUT_DIR, f"whatsapp_retiros_{hoy}.html")
    with open(ruta_html, "w", encoding="utf-8") as f:
        f.write(pagina)
    with open(os.path.join(OUT_DIR, f"retiros_{hoy}.json"), "w", encoding="utf-8") as f:
        json.dump(pedidos, f, ensure_ascii=False, indent=2)

    nuevos = sum(1 for p in pedidos if guias_previas and p["guia"] not in guias_previas)
    print(f"📦 Retiros en agencia Servientrega: {len(pedidos)} pedidos"
          + (f" ({nuevos} nuevos 🆕)" if guias_previas else " (sin día anterior para comparar)"))
    print("\n".join(lineas))
    print(f"\nHTML: {ruta_html}")


if __name__ == "__main__":
    main()
