"""
Dashboard de operacion VitashopEc (Streamlit).

Fuentes: ChateaPro (ventas del chat) + Dropi directo (ordenes, guias, cartera).
Solo lectura: no crea ni modifica nada.

Despliegue (Coolify, app separada):
  Base Directory = /  (usa dashboard/Dockerfile con contexto del repo)
  Puerto 8501, health check path /_stcore/health
  Vars: CHATEAPRO_API_TOKEN, DROPI_EMAIL, DROPI_PASSWORD,
        DROPI_MFA_CODE (solo al expirar sesion), DASHBOARD_PASSWORD,
        DROPI_TOKEN_PATH=/data/dropi_token.json (volumen persistente)

Local:
  cd servientrega && pip install -r dashboard/requirements.txt
  CHATEAPRO_API_TOKEN=... DROPI_EMAIL=... DASHBOARD_PASSWORD=... \
    streamlit run dashboard/app.py
"""

import os
import sys
import datetime as dt

import streamlit as st

BASE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(BASE, "shared"),  # Docker: modulos en ./shared
              os.path.join(BASE, "..", "ventas-bot")):  # local: repo
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)
import chateapro  # noqa: E402
import dropi  # noqa: E402

st.set_page_config(page_title="Operación VitashopEc", layout="wide")

TTL = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "300"))


# --- Seguridad: login gate + PII ---

def _login_gate():
    pwd = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pwd:
        st.error("Falta DASHBOARD_PASSWORD en el entorno.")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("Operación VitashopEc")
    clave = st.text_input("Clave", type="password")
    if st.button("Entrar"):
        if clave == pwd:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Clave incorrecta.")
    st.stop()


def mask_phone(tel):
    """Muestra solo los ultimos 3 digitos salvo que se active ver todo."""
    t = str(tel or "")
    if st.session_state.get("show_pii") or len(t) <= 3:
        return t
    return "***" + t[-3:]


_login_gate()
st.sidebar.title("Operación VitashopEc")
st.sidebar.checkbox("Mostrar datos completos (PII)", key="show_pii",
                    value=False)
seccion = st.sidebar.radio("Sección",
                           ["Resumen", "Embudo", "Retiros", "Novedades",
                            "Cartera"])
if st.sidebar.button("Actualizar datos"):
    st.cache_data.clear()


# --- Capa de datos (cacheada, tolerante a fallos) ---

@st.cache_data(ttl=TTL)
def _ventas_hoy():
    try:
        return chateapro.ventas_del_dia(), None
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=TTL)
def _ordenes_recientes(n=200):
    try:
        items, _ = dropi.objetos_ordenes(
            dropi.listar_mis_ordenes(page=1, per_page=n))
        return items, None
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=TTL)
def _saldo():
    try:
        return dropi.saldo_cartera(), None
    except Exception as e:
        return {}, str(e)


def _dias_desde(fecha_iso):
    try:
        f = dt.datetime.fromisoformat(str(fecha_iso).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - f).days
    except (ValueError, TypeError):
        return None


ventas, err_ventas = _ventas_hoy()
ordenes, err_dropi = _ordenes_recientes()
saldo, err_saldo = _saldo()

if err_ventas:
    st.warning(f"ChateaPro no disponible: {err_ventas}")
if err_dropi:
    st.warning(f"Dropi no disponible (¿sesion MFA expirada?): {err_dropi}")


# --- Secciones ---

if seccion == "Resumen":
    st.header(f"Resumen — {chateapro.hoy_ecuador().strftime('%d/%m/%Y')}")
    fact = sum(float(str(v.get(chateapro.CAMPO_VALOR, '0'))
                     .replace(',', '.').split()[0]
                     .replace('$', '') or 0)
               for v in ventas)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ventas hoy", len(ventas))
    c2.metric("Facturación", f"${fact:,.2f}")
    c3.metric("Ticket promedio", f"${fact / len(ventas):,.2f}" if ventas else "-")
    c4.metric("Órdenes Dropi (muestra)", len(ordenes))
    if ventas:
        por_ciudad = {}
        for v in ventas:
            c = (v.get(chateapro.CAMPO_CIUDAD) or "-").strip() or "-"
            por_ciudad[c] = por_ciudad.get(c, 0) + 1
        st.subheader("Ventas por ciudad")
        st.bar_chart(por_ciudad)
        st.subheader("Últimas ventas")
        st.dataframe([{
            "nombre": v.get(chateapro.CAMPO_NOMBRE) or v.get("_name"),
            "teléfono": mask_phone(v.get("_phone")),
            "ciudad": v.get(chateapro.CAMPO_CIUDAD),
            "valor": v.get(chateapro.CAMPO_VALOR),
            "productos": v.get(chateapro.CAMPO_PRODUCTOS),
        } for v in ventas[:50]], use_container_width=True)

elif seccion == "Embudo":
    st.header("Embudo operativo")
    por_estado = {}
    for o in ordenes:
        e = str(o.get("status") or "-").strip() or "-"
        por_estado[e] = por_estado.get(e, 0) + 1
    c1, c2 = st.columns(2)
    c1.metric("Ventas (chat hoy)", len(ventas))
    c2.metric("Órdenes Dropi (muestra 200)", len(ordenes))
    st.subheader("Órdenes por estado")
    if por_estado:
        st.bar_chart(por_estado)
        st.dataframe([{"estado": k, "n": v}
                      for k, v in sorted(por_estado.items(),
                                         key=lambda x: -x[1])],
                     use_container_width=True)

elif seccion == "Retiros":
    st.header("Retiros Servientrega")
    ret = [o for o in ordenes
           if "retiro" in str(o.get("status") or "").lower()]
    st.metric("En retiro (muestra)", len(ret))
    if ret:
        filas = []
        for o in ret:
            dias = _dias_desde(o.get("created_at"))
            nombre = f"{o.get('name', '')} {o.get('surname', '')}".strip()
            filas.append({
                "guía": o.get("shipping_guide") or "-",
                "cliente": nombre if st.session_state.get("show_pii") else (
                    (nombre[:1] + "***") if nombre else "-"),
                "teléfono": mask_phone(o.get("phone")),
                "ciudad": o.get("city"),
                "días": dias if dias is not None else "-",
            })
        filas.sort(key=lambda r: (r["días"] == "-",
                                  -(r["días"] if r["días"] != "-" else 0)))

        def _color(dias):
            if dias == "-" or dias is None:
                return ""
            if dias >= 7:
                return "background-color: #ffcccc"
            if dias >= 3:
                return "background-color: #fff2cc"
            return "background-color: #d9ead3"

        import pandas as pd
        df = pd.DataFrame(filas)
        st.dataframe(df.style.map(_color, subset=["días"]),
                     use_container_width=True)
        st.download_button("Descargar CSV", df.to_csv(index=False),
                           "retiros.csv", "text/csv")

elif seccion == "Novedades":
    st.header("Novedades")
    nov = [o for o in ordenes if str(o.get("novedad_servientrega") or "").strip()]
    st.metric("Con novedad Servientrega", len(nov))
    if nov:
        st.dataframe([{
            "orden": o.get("id"),
            "guía": o.get("shipping_guide") or "-",
            "cliente": f"{o.get('name', '')} {o.get('surname', '')}".strip(),
            "teléfono": mask_phone(o.get("phone")),
            "novedad": str(o.get("novedad_servientrega"))[:200],
            "estado": o.get("status"),
        } for o in nov], use_container_width=True)

elif seccion == "Cartera":
    st.header("Cartera Dropi")
    if err_saldo:
        st.error(f"No se pudo leer el saldo: {err_saldo}")
    else:
        st.metric("Saldo", f"{saldo.get('amount', '-')} "
                           f"{saldo.get('currency_code', '')}")

st.sidebar.caption("Solo lectura · cache %ds · Dropi sesion 12h (MFA al expirar)"
                   % TTL)
