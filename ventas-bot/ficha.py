"""
Ficha de venta: une Chatea Pro + OpenAI + servicio de agencias + Dropi (MCP).

Flujo por cada venta:
  1. valida los datos del CRM (campos de usuario de Chatea Pro)
  2. lee el chat y le pide a OpenAI la promo y la ubicacion de retiro
  3. consulta el servicio de agencias con esa ubicacion
  4. arma el texto de la ficha y los botones de Telegram
  5. al pulsar "Crear" arma el resumen (ciudad/departamento Dropi + cotizacion
     Servientrega) y, al confirmar, crea el pedido DIRECTO con el MCP de Dropi
     (dropi_pedido.py). Antes revisa que no exista ya un pedido con ese telefono.
"""

import json
import os
import re
import time

import agencias
import chateapro
import dropi_mcp
import dropi_pedido
import hermes
import ia
import promos

# Cache en memoria del analisis por suscriptor (para los botones).
_CACHE = {}

# Datos del CRM que dejan de ser bloqueantes cuando ya hay promo / agencia.
_CUBIERTOS_POR_PROMO = {"Productos escogidos", "Valor de la compra"}
_CUBIERTOS_POR_AGENCIA = {"Provincia", "Ciudad"}

_RANGO_ESTADO = {"unica": 0, "probable": 1, "falta_sector": 2,
                 "sin_ciudad": 3, "error": 4}

# Enviar al cliente el mensaje de "pedido ingresado" al crear el pedido.
NOTIFICAR_CLIENTE = os.environ.get("NOTIFICAR_CLIENTE", "true").lower() == "true"
# Ciudades con hasta este numero de agencias: se le envia la lista al cliente.
MAX_AGENCIAS_LISTA = int(os.environ.get("MAX_AGENCIAS_LISTA", "10"))
RUTA_OFRECIDAS = os.environ.get(
    "AGENCIAS_OFRECIDAS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "agencias_ofrecidas.json"))

MSG_PROMO = (
    "😊 ¡Hola! Le confirmamos que su pedido ya se encuentra correctamente ingresado "
    "y estará en oficinas dentro de 2 días laborables.\n\n"
    "{agencia}"
    "📦 En cuanto tengamos su guía, se la enviaremos para que juntos podamos dar "
    "seguimiento al pedido. También le recordaremos cuando se encuentre disponible "
    "en la agencia de *Servientrega*\n\n"
    "Se le está enviando un producto gratis por la promoción!!\n"
    "¡Muchas gracias por su confianza! Estamos a las órdenes. 😊")
MSG_SIN_PROMO = (
    "😊 ¡Hola! Le confirmamos que su pedido ya se encuentra correctamente ingresado.\n\n"
    "{agencia}"
    "📦 En cuanto tengamos su guía, se la enviaremos para que juntos podamos dar "
    "seguimiento al pedido. También le recordaremos cuando se encuentre disponible "
    "en la agencia de *Servientrega*\n\n"
    "¡Muchas gracias por su confianza! Estamos a las órdenes. 😊")
MSG_AGENCIA = ("📍 Agencia Servientrega donde retira: *{nombre}*\n"
               "{direccion}\n"
               "🕒 Lunes a viernes {horario}{finde}\n\n")

_ETIQUETA_ESTADO = {"crm": "la que ya está en el CRM",
                    "cliente": "elegida por el cliente en el chat",
                    "unica": "única en la ciudad",
                    "probable": "la más cercana al sector",
                    "geo": "la más cercana a la referencia del cliente"}


def _buscar_agencia(textos):
    """Prueba cada texto de ubicacion y se queda con el mejor resultado."""
    mejor = None
    for t in textos:
        if not t:
            continue
        r = agencias.buscar_agencia(t)
        if mejor is None or _RANGO_ESTADO[r["estado"]] < _RANGO_ESTADO[mejor["estado"]]:
            mejor = r
        if r["estado"] in ("unica", "probable"):
            break
    return mejor or agencias.buscar_agencia("")


def analizar(campos, forzar=False):
    """Devuelve (y cachea) el analisis completo de una venta."""
    ns = campos.get("_user_ns", "")
    if not forzar and ns in _CACHE:
        return _CACHE[ns]

    productos = campos.get(chateapro.CAMPO_PRODUCTOS, "")
    valor = campos.get(chateapro.CAMPO_VALOR, "")

    # --- chat + IA ---
    try:
        chat = chateapro.obtener_chat(ns)
        error_chat = ""
    except Exception as e:  # el chat es un extra: la ficha sale igual
        chat, error_chat = [], str(e)
    analisis_ia = ia.analizar_venta(chat, productos, valor)

    # --- promo: primero reglas sobre el CRM, luego IA ---
    cliente_txt = " ".join(_lineas_cliente({"chat": chat}, n=15))
    promo, origen, avisos_promo = promos.detectar(campos, cliente_txt)
    ia_promo = analisis_ia["promo"] if promos.valida(analisis_ia["promo"] or "") else None
    if not promo and ia_promo:
        # La IA solo desempata dentro del mismo producto que dice el CRM.
        fams = promos.familias_en(productos)
        if not fams or promos.familia(ia_promo) in fams:
            promo, origen = ia_promo, "IA (chat)"
    elif promo and ia_promo and ia_promo != promo:
        avisos_promo.append(f"La IA entendió {ia_promo}; se usó {promo} por el CRM.")

    # --- agencia ---
    colonia = campos.get("Colonia", "")
    crm_texto = " ".join(x for x in (campos.get(chateapro.CAMPO_CIUDAD, ""), colonia,
                                     campos.get(chateapro.CAMPO_DIRECCION, "")) if x)
    res = agencias.desde_direccion_dropi(campos.get(chateapro.CAMPO_DIRECCION, ""))
    if not res:
        res = _respuesta_del_cliente(ns, chat)
    if not res:
        res = _buscar_agencia([analisis_ia["ubicacion_texto"], crm_texto])
    geo = None
    if res["estado"] in ("falta_sector", "sin_ciudad", "probable"):
        # El buscador no reconocio el sector, o lo emparejo por una palabra
        # suelta ("colegio"): geocodificar la referencia y usar la mas cercana.
        ciudad = res["ciudad"] or campos.get(chateapro.CAMPO_CIUDAD, "")
        textos = ([analisis_ia["ubicacion_texto"], colonia,
                   campos.get(chateapro.CAMPO_DIRECCION)]
                  + list(reversed(_lineas_cliente({"chat": chat}, n=8, recorte=200))))
        try:
            geo = agencias.cercanas_por_referencia(
                ciudad, textos, campos.get(chateapro.CAMPO_PROVINCIA, ""),
                nombre_cliente=f"{campos.get(chateapro.CAMPO_NOMBRE, '')} {campos.get('_name', '')}")
        except Exception as e:
            print("Error geocodificando agencia:", e)
        if geo and geo["agencias"]:
            alternativas = geo["agencias"][1:]
            previa = res.get("sugerida")
            nombres = {g["nombre"] for g in geo["agencias"]}
            if previa and previa.get("nombre") not in nombres:
                alternativas = alternativas[:1] + [previa]  # la del buscador queda como opcion
            res = dict(res, estado="geo", ciudad=ciudad,
                       sugerida=geo["agencias"][0], alternativas=alternativas)
    opciones = ([res["sugerida"]] if res["sugerida"] else []) + res["alternativas"]
    elegida = (res["sugerida"] if res["estado"] in ("crm", "cliente", "unica", "probable", "geo")
               else None)

    # --- Dropi: ya existe un pedido con este telefono? ---
    existente, aviso_dropi = None, ""
    try:
        existente = dropi_pedido.pedido_existente(campos.get("_phone", ""))
    except dropi_mcp.AuthRequired:
        aviso_dropi = "Dropi no está conectado (/dropi_conectar): no pude revisar duplicados."
    except Exception as e:
        aviso_dropi = f"No pude revisar duplicados en Dropi: {_recorta(str(e), 80)}"

    # --- Dropi: historial del comprador (entrega / devolucion) ---
    perfil, error_perfil = None, ""
    try:
        perfil = dropi_pedido.perfil_cliente(campos.get("_phone", ""))
    except dropi_mcp.AuthRequired:
        error_perfil = "Dropi no conectado: no pude ver el historial del cliente."
    except Exception as e:
        error_perfil = f"No pude ver el historial del cliente: {_recorta(str(e), 80)}"

    a = {
        "ns": ns, "campos": campos, "chat": chat, "error_chat": error_chat,
        "ia": analisis_ia, "promo": promo, "promo_origen": origen,
        "avisos_promo": avisos_promo, "agencia_res": res, "geo": geo,
        "opciones": opciones, "elegida": elegida,
        "dropi_existente": existente, "aviso_dropi": aviso_dropi,
        "perfil": perfil, "error_perfil": error_perfil,
        "preparado": None,
    }
    _CACHE[ns] = a
    return a


# ------------------------------------------- pedir agencia al cliente --

def _leer_ofrecidas():
    try:
        with open(RUTA_OFRECIDAS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar_ofrecida(ns, ciudad, lista):
    d = _leer_ofrecidas()
    ahora = time.time()
    d = {k: v for k, v in d.items() if ahora - v.get("ts", 0) < 7 * 86400}
    d[ns] = {"ciudad": ciudad, "agencias": [x["nombre"] for x in lista], "ts": ahora}
    try:
        with open(RUTA_OFRECIDAS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print("No se pudo guardar agencias_ofrecidas.json:", e)


def _respuesta_del_cliente(ns, chat):
    """
    Si al cliente se le envio la lista de agencias y respondio con un numero
    (o con el nombre de una), devuelve el resultado con esa agencia.
    """
    of = _leer_ofrecidas().get(ns)
    if not of:
        return None
    respuestas = [l[len("Cliente: "):] for l in chat if l.startswith("Cliente: ")][-3:]
    todas = {x["nombre"]: x for x in agencias.de_ciudad(of["ciudad"])}
    for txt in reversed(respuestas):
        t = agencias._norm(txt)
        m = re.fullmatch(r"(?:la |opcion |numero |agencia )?(\d{1,2})", t)
        if m and 1 <= int(m.group(1)) <= len(of["agencias"]):
            nombre = of["agencias"][int(m.group(1)) - 1]
        else:
            nombre = next((n for n in of["agencias"]
                           if len(agencias._norm(agencias.nombre_corto({"nombre": n}))) > 3
                           and agencias._norm(agencias.nombre_corto({"nombre": n})) in t), None)
        if nombre and nombre in todas:
            return agencias._resultado("cliente", of["ciudad"], todas[nombre], [], len(todas),
                                       f"El cliente respondió «{_recorta(txt, 40)}».")
    return None


def ciudad_de(a):
    return (a["agencia_res"].get("ciudad")
            or a["campos"].get(chateapro.CAMPO_CIUDAD, "")).strip()


def agencias_ciudad(a):
    return agencias.de_ciudad(ciudad_de(a)) if ciudad_de(a) else []


def mensaje_pedir_agencia(a):
    """
    Texto para pedirle al cliente la agencia. Si la ciudad tiene pocas, se le
    envia la lista numerada; si tiene muchas, se le pide el sector.
    Devuelve (texto, lista_ofrecida).
    """
    ciudad = ciudad_de(a)
    lista = agencias_ciudad(a)
    if not ciudad or not lista:
        return ("😊 ¡Hola! Para enviar su pedido a la agencia Servientrega más cercana, "
                "¿nos indica su *ciudad* y el *sector o barrio* donde le queda mejor "
                "retirarlo? 🙌"), []
    titulo = ciudad.title()
    if len(lista) <= MAX_AGENCIAS_LISTA:
        L = [f"😊 ¡Hola! Para enviar su pedido necesitamos saber en qué agencia "
             f"Servientrega de *{titulo}* le queda mejor retirarlo:", ""]
        for i, ag in enumerate(lista, 1):
            L.append(f"*{i}.* {agencias.nombre_corto(ag).title()} — {ag.get('direccion', '')}")
        L += ["", "Respóndanos solo con el *número* de la agencia 🙌"]
        return "\n".join(L), lista
    return (f"😊 ¡Hola! En *{titulo}* tenemos {len(lista)} agencias Servientrega. "
            "Para enviar su pedido a la más cercana, ¿nos indica el *sector o barrio* donde "
            "le queda mejor retirarlo? Una referencia (parque, colegio, avenida o centro "
            "comercial) nos ayuda mucho 🙌"), []


def pedir_agencia(a):
    """Envia al cliente el mensaje para elegir agencia. Devuelve (ok, detalle)."""
    texto, lista = mensaje_pedir_agencia(a)
    ok, det = chateapro.enviar_texto(a["ns"], texto)
    if ok and lista:
        _guardar_ofrecida(a["ns"], ciudad_de(a), lista)
    return ok, det


def ver_agencias(a, limite=12):
    """Pone como opciones todas las agencias de la ciudad (para elegir a mano)."""
    lista = agencias_ciudad(a)
    if not lista:
        return 0
    if a["elegida"] and all(x["nombre"] != a["elegida"]["nombre"] for x in lista[:limite]):
        lista = [a["elegida"]] + lista
    a["opciones"] = lista[:limite]
    if a["elegida"]:
        a["elegida"] = next((x for x in a["opciones"]
                             if x["nombre"] == a["elegida"]["nombre"]), a["elegida"])
    return len(agencias_ciudad(a))


def faltantes(a):
    """Datos del CRM que faltan y SI bloquean la creacion."""
    faltan = set(chateapro.datos_faltantes(a["campos"]))
    if a["promo"]:
        faltan -= _CUBIERTOS_POR_PROMO
    if a["elegida"]:
        faltan -= _CUBIERTOS_POR_AGENCIA
    return sorted(faltan)


def bloqueos(a):
    """Motivos por los que aun no se puede crear el pedido."""
    motivos = []
    if not a["promo"]:
        motivos.append("No se pudo determinar la promo")
    if not a["elegida"]:
        motivos.append("No detecté agencia, revisa el chat")
    f = faltantes(a)
    if f:
        motivos.append("Faltan datos: " + ", ".join(f))
    ya = hermes.ya_creado(a["ns"])
    if ya:
        motivos.append(f"Ya se creó el pedido #{ya.get('order_id')}")
    elif a.get("dropi_existente"):
        motivos.append(f"Ya existe en Dropi el pedido #{a['dropi_existente']['order_id']} "
                       "con este teléfono")
    if a["ia"].get("quiere_cancelar"):
        motivos.append("El cliente parece querer cancelar")
    return motivos


def elegir_agencia(a, indice):
    if 0 <= indice < len(a["opciones"]):
        a["elegida"] = a["opciones"][indice]
        a["preparado"] = None
        return True
    return False


# ------------------------------------------------------------------ texto --

def _recorta(s, n=140):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _lineas_cliente(a, n=3, recorte=140):
    cliente = [l[len("Cliente: "):] for l in a["chat"] if l.startswith("Cliente: ")]
    return [_recorta(l, recorte) for l in cliente[-n:]]


def texto_ficha(a):
    c = a["campos"]
    motivos = bloqueos(a)
    nombre = c.get(chateapro.CAMPO_NOMBRE) or c.get("_name") or "-"
    ya = hermes.ya_creado(a["ns"])

    ex = a.get("dropi_existente")
    if ya:
        estado = f"✅ PEDIDO YA CREADO #{ya.get('order_id')}"
    elif ex:
        estado = f"📦 YA EXISTE EN DROPI #{ex['order_id']}"
    elif not motivos:
        estado = "✅ LISTA PARA CREAR EN DROPI"
    else:
        estado = "⚠️ REVISAR ANTES DE CREAR"

    L = [f"🔔 NUEVA VENTA — {estado}", ""]
    if (a.get("perfil") or {}).get("alerta"):
        L += [f"⚠️ CLIENTE CON DEVOLUCIONES: {a['perfil']['devolucion']:.0f}% de sus pedidos "
              "los devolvió. Confirma antes de crear.", ""]
    if a["ia"]["quiere_cancelar"]:
        L += ["🚫 OJO: el cliente parece querer CANCELAR. Revisa el chat.", ""]

    L += [
        f"👤 {nombre}",
        f"📲 {c.get('_phone', '-')}",
        (f"ℹ️ {a['error_perfil']}" if a.get("error_perfil")
         else dropi_pedido.texto_perfil(a.get("perfil"))),
        f"🛍️ CRM: {c.get(chateapro.CAMPO_PRODUCTOS, '-')} — ${c.get(chateapro.CAMPO_VALOR, '-')}",
        "",
    ]

    if a["promo"]:
        L.append(f"🎁 {promos.descripcion(a['promo'])}")
        L.append(f"   (detectada por {a['promo_origen']})")
    else:
        L.append("🎁 Promo: no detectada")
    for av in a["avisos_promo"]:
        L.append(f"   ⚠️ {av}")

    res = a["agencia_res"]
    L.append("")
    if a["elegida"]:
        ag = a["elegida"]
        etiqueta = _ETIQUETA_ESTADO.get(res["estado"], "elegida por ti")
        if res["sugerida"] is not ag:
            etiqueta = "elegida por ti"
        L += [f"🏢 Agencia ({etiqueta}):",
              f"   {ag.get('nombre')} — {ag.get('sector', '')}",
              f"   📍 {ag.get('direccion', '-')}",
              f"   🕒 L-V {ag.get('horario_lun_vie', '-')}"]
        if a.get("geo") and ag.get("distancia_km") is not None:
            L.append(f"   🧭 a {ag['distancia_km']} km de «{a['geo']['punto']}» "
                     f"(cliente: “{a['geo']['referencia']}”)")
    else:
        if res["estado"] == "falta_sector":
            L.append(f"🏢 No detecté agencia: {res['ciudad']} tiene {res['total_ciudad']} y el "
                     "cliente no dio sector. Elige una abajo o revisa el chat.")
        else:
            L.append("🏢 No detecté agencia, revisa el chat")
        if res["estado"] == "error":
            L.append(f"   ({res['mensaje']})")

    if a["ia"]["ubicacion_texto"]:
        L.append(f"   Ubicación entendida: “{_recorta(a['ia']['ubicacion_texto'])}”")

    # Evidencia del chat cuando hay dudas.
    if motivos and not ya:
        ult = _lineas_cliente(a)
        if ult:
            L += ["", "💬 Lo último que escribió el cliente:"] + [f"   • {x}" for x in ult]
        elif a["error_chat"]:
            L += ["", f"💬 No pude leer el chat: {_recorta(a['error_chat'], 100)}"]
        else:
            L += ["", "💬 No se encontraron mensajes del cliente en el chat."]

    if ex and not ya:
        L += ["", f"📦 Dropi ya tiene el pedido #{ex['order_id']} de este teléfono "
                  f"({ex.get('status')}, ${ex.get('total')}, {ex.get('city')}). "
                  "No se crea otro."]
    if a.get("aviso_dropi"):
        L += ["", f"ℹ️ {a['aviso_dropi']}"]

    if a["ia"]["notas"]:
        L += ["", f"📝 IA: {_recorta(a['ia']['notas'], 200)}"]
    if motivos and not ya:
        L += ["", "Pendiente:"] + [f"   ✖ {m}" for m in motivos]
    return "\n".join(L)


def teclado(a):
    """Botones inline de Telegram (lista de filas)."""
    ns = a["ns"]
    filas = []
    ya = hermes.ya_creado(ns)
    if ya:
        return []
    if a.get("dropi_existente"):
        filas.append([{"text": "🔄 Reanalizar", "callback_data": f"re:{ns}"}])
        return filas
    if not bloqueos(a):
        filas.append([{"text": "✅ Crear pedido en Dropi", "callback_data": f"crear:{ns}"}])
    # Agencias para cambiar o elegir (3 sugeridas, o la ciudad completa).
    for i, ag in enumerate(a["opciones"][:12]):
        marca = "● " if a["elegida"] is ag else ""
        filas.append([{"text": f"{marca}🏢 {ag.get('nombre', '?')}"[:60],
                       "callback_data": f"ag:{ns}:{i}"}])
    n_ciudad = len(agencias_ciudad(a))
    if n_ciudad > len(a["opciones"]):
        filas.append([{"text": f"🗂️ Ver agencias de {ciudad_de(a).title()} ({n_ciudad})"[:60],
                       "callback_data": f"va:{ns}"}])
    if not a["elegida"] or a["agencia_res"]["estado"] not in ("crm", "cliente", "unica"):
        filas.append([{"text": "📲 Pedir agencia/sector al cliente",
                       "callback_data": f"ps:{ns}"}])
    filas.append([{"text": "🔄 Reanalizar (relee CRM y chat)", "callback_data": f"re:{ns}"}])
    return filas


# ------------------------------------------------------------------ crear --

def preparar(a):
    """
    Paso 1 del boton Crear: resuelve ciudad/departamento en Dropi y cotiza
    Servientrega. Guarda el resultado en a["preparado"]. Devuelve (ok, mensaje).
    Lanza dropi_mcp.AuthRequired si hay que conectar Dropi.
    """
    motivos = bloqueos(a)
    if motivos:
        return False, "; ".join(motivos)
    ag = a["elegida"]
    datos = hermes.armar_datos(a["campos"], a["promo"], ag)
    try:
        destino = dropi_pedido.resolver_destino(
            ag.get("ciudad") or a["agencia_res"].get("ciudad"),
            ag.get("provincia") or a["campos"].get(chateapro.CAMPO_PROVINCIA, ""))
    except dropi_pedido.DestinoError as e:
        return False, str(e)
    faltan, stock_lineas = dropi_pedido.revisar_stock(
        promos.armar(a["promo"]), promos.BUSQUEDA_STOCK)
    if faltan:
        return False, ("SIN STOCK en Dropi: " + "; ".join(faltan)
                       + ". Créalo a mano en Dropi (con otro producto) o espera reposición.")
    cot = dropi_pedido.cotizar(destino, datos["products"], datos["dir"])
    if not cot["ok"]:
        return False, cot["error"] + " Si persiste, créalo a mano en Dropi."
    a["preparado"] = {"datos": datos, "destino": destino, "cotizacion": cot,
                      "stock": stock_lineas}
    return True, "Revisa y confirma"


def texto_confirmacion(a):
    p = a["preparado"]
    d, dest, cot = p["datos"], p["destino"], p["cotizacion"]
    total = promos.total_promo(a["promo"])
    L = ["📝 CONFIRMAR PEDIDO EN DROPI", "",
         f"👤 {d['name']} {d['surname']}".rstrip(),
         f"📲 {d['phone']}",
         f"🗺️ Dropi: {dest['city']} — {dest['state']} (ciudad #{dest['city_id']})",
         f"🏢 {a['elegida'].get('nombre')}",
         f"   {d['dir']}", "",
         f"🎁 {promos.descripcion(a['promo'])}",
         "",
         f"🚚 SERVIENTREGA · contra entrega · envío ${cot['costo']:.2f} {cot['moneda']}",
         f"💵 El cliente paga ${total:.2f} {cot['moneda']}",
         "", "📦 Stock en Dropi:"] + [f"   {x}" for x in p.get("stock", [])]
    if NOTIFICAR_CLIENTE:
        L += ["", "📲 Al crear se le enviará al cliente por WhatsApp:",
              "┄┄┄┄┄┄┄┄", mensaje_cliente(a), "┄┄┄┄┄┄┄┄"]
    L += ["", "¿Creo el pedido?"]
    return "\n".join(L)


def mensaje_cliente(a):
    """Mensaje de 'pedido ingresado' (con o sin promo) con los datos de la agencia."""
    ag = a["elegida"] or {}
    finde = str(ag.get("horario_fin_semana") or "").strip()
    if finde.upper() in ("", "-", "NO", "N/A", "CERRADO", "NO ATIENDE"):
        finde = ""
    ciudad = str(ag.get("ciudad") or "").title()
    corto = agencias.nombre_corto(ag).title()
    bloque = MSG_AGENCIA.format(
        nombre=f"{ciudad} – {corto}" if ciudad and corto.upper() != ciudad.upper() else corto or "-",
        direccion=ag.get("direccion", ""),
        horario=ag.get("horario_lun_vie", "-"),
        finde=f" · sábados {finde}" if finde else "")
    plantilla = MSG_PROMO if promos.tiene_regalo(a["promo"]) else MSG_SIN_PROMO
    return plantilla.format(agencia=bloque)


def teclado_confirmacion(a):
    ns = a["ns"]
    return [[{"text": "✅ Sí, crear en Dropi", "callback_data": f"ok:{ns}"}],
            [{"text": "↩️ Volver", "callback_data": f"x:{ns}"}]]


def crear_pedido(a):
    """
    Paso 2: crea el pedido con el MCP de Dropi. Devuelve
    {"ok", "order_id", "guia", "mensaje"}. Lanza AuthRequired si falta login.
    """
    motivos = bloqueos(a)
    if motivos:
        return {"ok": False, "order_id": None, "guia": None,
                "mensaje": "No se puede crear: " + "; ".join(motivos)}
    if not a.get("preparado"):
        return {"ok": False, "order_id": None, "guia": None,
                "mensaje": "Falta la confirmación: pulsa «Crear pedido en Dropi»."}

    # Ultimo chequeo de duplicados justo antes de crear (sin cache).
    dropi_pedido.olvidar_pedidos()
    ex = dropi_pedido.pedido_existente(a["campos"].get("_phone", ""))
    if ex:
        a["dropi_existente"] = ex
        return {"ok": False, "order_id": None, "guia": None,
                "mensaje": f"Ya existe en Dropi el pedido #{ex['order_id']} con este teléfono."}

    p = a["preparado"]
    res = dropi_pedido.crear(p["datos"], p["destino"])
    if res["ok"]:
        hermes.registrar(a["ns"], res)
        res["notificacion"] = None
        if NOTIFICAR_CLIENTE:
            ok, det = chateapro.enviar_texto(a["ns"], mensaje_cliente(a))
            res["notificacion"] = (ok, det)
    return res
