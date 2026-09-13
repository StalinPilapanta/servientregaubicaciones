"""
Extractor del Directorio de Centros de Solucion (CS) de Servientrega Ecuador.

La pagina del directorio carga un <select id="ciudad"> cuyo onchange llama a
showdep(value), que a su vez hace una peticion AJAX a:

    xgetcds.php?q=<valor_de_la_opcion>

Esa peticion devuelve una tabla HTML con TODAS las agencias de esa ciudad/punto
y con todos los campos (Regional, Provincia, Ciudad, Tipo, Nombre, Sector,
Direccion, Telefono, Supervisor, Entrega en oficina, Hora promedio, Horario L-V,
Horario fin de semana, E-Mail y coordenadas del mapa).

Por eso NO se necesita Selenium: basta con leer las opciones del <select> y
llamar directamente al endpoint por cada una. Esto es mas rapido y confiable.
"""

import re
import csv
import json
import html
from html.parser import HTMLParser
from urllib.parse import quote_plus

import requests

# --- Configuracion ---
BASE = ("https://servientrega-ecuador.appsiscore.com/app/"
        "xMr3hVYKKkVzL0gGjnCFySIRO6RLPLb1qUb1bwop94Zq8Of/"
        "xMr3hVYKKkVzL0gGjnCFySIRO6RLPLb1qUb1bwop94Zq8Of")

URL_DIRECTORIO = f"{BASE}/directorio.php"
URL_ENDPOINT = f"{BASE}/xgetcds.php"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}

# Orden de columnas tal como las devuelve el endpoint xgetcds.php
CAMPOS = [
    "Regional",
    "Provincia",
    "Ciudad",
    "Tipo_CS",
    "Nombre_agencia",
    "Sector",
    "Direccion",
    "Telefono",
    "Supervisor",
    "Entrega_en_oficina",
    "Hora_promedio_entrega",
    "Horario_Lunes_Viernes",
    "Horario_fin_semana",
    "Email",
]

# Todas las claves del registro final (incluye coordenadas del mapa)
CAMPOS_SALIDA = ["Latitud", "Longitud"] + CAMPOS


def decodificar(contenido_bytes):
    """El servidor declara UTF-8 pero el contenido real es ISO-8859-1."""
    for enc in ("utf-8", "latin-1"):
        try:
            return contenido_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return contenido_bytes.decode("latin-1", errors="replace")


class ExtractorOpciones(HTMLParser):
    """Lee las opciones del <select id="ciudad"> de la pagina directorio.php."""

    def __init__(self):
        super().__init__()
        self.en_select = False
        self.en_option = False
        self.value_actual = None
        self.opciones = []  # (value_para_endpoint, texto_visible)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "select" and d.get("id") == "ciudad":
            self.en_select = True
        elif tag == "option" and self.en_select:
            self.en_option = True
            # El value viene con espacios de relleno; los quitamos.
            self.value_actual = (d.get("value") or "").strip()

    def handle_endtag(self, tag):
        if tag == "select":
            self.en_select = False
        elif tag == "option":
            self.en_option = False

    def handle_data(self, data):
        if self.en_option:
            texto = data.strip()
            if self.value_actual and texto and "Seleccione" not in texto:
                self.opciones.append((self.value_actual, texto))


class ExtractorTabla(HTMLParser):
    """
    Parsea la respuesta de xgetcds.php.

    Cada fila (<tr>) corresponde a una agencia. La primera celda contiene un
    enlace a xmapa_cs.php?latitud=..&longitud=.. con las coordenadas, y las
    celdas siguientes contienen los 14 campos en el orden de CAMPOS.
    """

    def __init__(self):
        super().__init__()
        self.filas = []          # lista de dict por agencia
        self.en_fila = False
        self.en_celda = False
        self.celda_texto = []
        self.celda_indice = 0
        self.lat = None
        self.lng = None
        self.celdas = []         # textos de las celdas de datos (sin la del mapa)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.en_fila = True
            self.celdas = []
            self.celda_indice = 0
            self.lat = None
            self.lng = None
        elif tag == "td" and self.en_fila:
            self.en_celda = True
            self.celda_texto = []
        elif tag == "a" and self.en_fila:
            href = dict(attrs).get("href", "")
            m = re.search(r"latitud=([^&]+)&longitud=([^'\"&]+)", href)
            if m:
                self.lat = m.group(1)
                self.lng = m.group(2)

    def handle_endtag(self, tag):
        if tag == "td" and self.en_celda:
            self.en_celda = False
            texto = " ".join("".join(self.celda_texto).split())
            self.celdas.append(texto)
            self.celda_indice += 1
        elif tag == "tr" and self.en_fila:
            self.en_fila = False
            # La primera celda es la del mapa (queda vacia de texto). Las
            # 14 siguientes son los campos. Filtramos filas de encabezado.
            datos = [c for c in self.celdas]
            # La celda del mapa suele quedar vacia; la descartamos si es la 1a.
            if datos and datos[0] == "":
                datos = datos[1:]
            if len(datos) >= len(CAMPOS):
                registro = {"Latitud": self.lat or "", "Longitud": self.lng or ""}
                for i, campo in enumerate(CAMPOS):
                    registro[campo] = datos[i]
                # Evitar filas que no sean de agencia (sin nombre)
                if registro["Nombre_agencia"]:
                    self.filas.append(registro)

    def handle_data(self, data):
        if self.en_celda:
            self.celda_texto.append(data)


def obtener_opciones(session):
    print("Descargando pagina del directorio...")
    r = session.get(URL_DIRECTORIO, headers=HEADERS, timeout=30)
    r.raise_for_status()
    parser = ExtractorOpciones()
    parser.feed(decodificar(r.content))
    print(f"Se encontraron {len(parser.opciones)} entradas en el listado.")
    return parser.opciones


def obtener_agencias_de(session, valor):
    # El value original ya viene con '+' como separador (formato del sitio).
    # requests lo re-codificaria, asi que construimos la URL manualmente.
    url = f"{URL_ENDPOINT}?q={quote_plus(valor.replace('+', ' '))}"
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    parser = ExtractorTabla()
    parser.feed(decodificar(r.content))
    return parser.filas


def main():
    session = requests.Session()
    opciones = obtener_opciones(session)

    agencias = []
    vistos = set()  # evitar duplicados (Nombre + Direccion)

    for i, (valor, texto) in enumerate(opciones, start=1):
        try:
            filas = obtener_agencias_de(session, valor)
            nuevas = 0
            for f in filas:
                clave = (f["Nombre_agencia"], f["Direccion"])
                if clave in vistos:
                    continue
                vistos.add(clave)
                agencias.append(f)
                nuevas += 1
            print(f"[{i}/{len(opciones)}] {texto}: {len(filas)} filas ({nuevas} nuevas)")
        except Exception as e:
            print(f"[{i}/{len(opciones)}] ERROR en '{texto}': {e}")

    print(f"\nTotal de agencias unicas extraidas: {len(agencias)}")
    guardar(agencias)
    print("Proceso finalizado.")


def guardar(agencias):
    # 1. JSON
    with open("directorio_servientrega.json", "w", encoding="utf-8") as f:
        json.dump(agencias, f, ensure_ascii=False, indent=4)

    # 2. CSV
    with open("directorio_servientrega.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS_SALIDA)
        writer.writeheader()
        writer.writerows(agencias)

    # 3. TXT
    with open("directorio_servientrega.txt", "w", encoding="utf-8") as f:
        encabezado = " | ".join(CAMPOS_SALIDA)
        f.write(encabezado + "\n")
        f.write("-" * len(encabezado) + "\n")
        for d in agencias:
            f.write(" | ".join(str(d.get(c, "")) for c in CAMPOS_SALIDA) + "\n")

    # 4. PDF (usa reportlab: Python puro, no requiere binarios externos)
    generar_pdf(agencias)


def generar_pdf(agencias, nombre="directorio_servientrega.pdf"):
    """Genera un PDF apaisado con reportlab (sin depender de wkhtmltopdf)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer)
    except ImportError:
        print("Nota: no se pudo generar el PDF (falta 'reportlab'; "
              "instalalo con: pip3 install reportlab). "
              "Los archivos JSON, CSV y TXT si se guardaron.")
        return

    # Columnas a incluir en el PDF (nombre visible -> clave del registro)
    columnas = [
        ("Provincia", "Provincia"),
        ("Ciudad", "Ciudad"),
        ("Agencia", "Nombre_agencia"),
        ("Sector", "Sector"),
        ("Direccion", "Direccion"),
        ("Telefono", "Telefono"),
        ("Horario L-V", "Horario_Lunes_Viernes"),
        ("Fin de semana", "Horario_fin_semana"),
        ("Email", "Email"),
    ]
    # Anchos relativos (suman al ancho util de la pagina apaisada)
    anchos = [22, 24, 34, 16, 60, 22, 34, 30, 42]

    estilos = getSampleStyleSheet()
    estilo_celda = ParagraphStyle(
        "celda", parent=estilos["Normal"], fontSize=6, leading=7)
    estilo_encab = ParagraphStyle(
        "encab", parent=estilos["Normal"], fontSize=7, leading=8,
        textColor=colors.white, fontName="Helvetica-Bold")

    # Encabezado de la tabla
    fila_encab = [Paragraph(nombre_col, estilo_encab) for nombre_col, _ in columnas]
    datos_tabla = [fila_encab]
    for d in agencias:
        datos_tabla.append([
            Paragraph(html.escape(str(d.get(clave, ""))), estilo_celda)
            for _, clave in columnas
        ])

    ancho_total = sum(anchos)
    ancho_util = landscape(A4)[0] - 20 * mm
    col_widths = [a / ancho_total * ancho_util for a in anchos]

    tabla = Table(datos_tabla, colWidths=col_widths, repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#006039")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f2f2")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    doc = SimpleDocTemplate(
        nombre, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm)
    titulo = Paragraph(
        f"<b>Directorio Servientrega - {len(agencias)} agencias</b>",
        estilos["Title"])
    try:
        doc.build([titulo, Spacer(1, 6 * mm), tabla])
        print(f"PDF generado con exito: {nombre}")
    except Exception as e:
        print(f"Nota: no se pudo generar el PDF ({e}). "
              "Los archivos JSON, CSV y TXT si se guardaron.")


if __name__ == "__main__":
    main()
