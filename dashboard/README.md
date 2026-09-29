# Dashboard de operación VitashopEc (Streamlit, solo lectura)

## Deploy en Coolify (app separada)

1. Nueva Application desde el repo `servientregaubicaciones`, branch `main`.
2. **Base Directory** = `/` (raíz, el Dockerfile usa contexto del repo).
3. **Dockerfile location** = `dashboard/Dockerfile`. Build Pack: Dockerfile.
4. Puerto: `8501`. Health check: path `/_stcore/health` (o desactívalo;
   la app igual corre).
5. Variables de entorno:
   | Variable | Descripción |
   |---|---|
   | `DASHBOARD_PASSWORD` | Clave del login web (obligatoria) |
   | `CHATEAPRO_API_TOKEN` | Token ChateaPro (sección Resumen) |
   | `DROPI_EMAIL` / `DROPI_PASSWORD` | Cuenta Dropi (ojalá usuario limitado) |
   | `DROPI_MFA_CODE` | Solo al expirar la sesión Dropi (~12h) |
   | `DROPI_COUNTRY_CODE` | Def. `ec` |
   | `DROPI_TOKEN_PATH` | Def. `/data/dropi_token.json` (con volumen) |
   | `DASHBOARD_CACHE_TTL_SECONDS` | Cache de datos (def. `300`) |
6. Volumen persistente en `/data` para no loguear Dropi en cada restart.
7. Primer arranque: pon un `DROPI_MFA_CODE` fresco → login ok → quítalo.
   Cuando la sesión expire verás el aviso y repites el paso.
8. No necesita dominio público si solo es interno; si lo expones, activa
   el auth básico de Coolify o restringe la red + `noindex`.

## Local

```bash
pip3 install -r dashboard/requirements.txt
export DASHBOARD_PASSWORD="..."
export CHATEAPRO_API_TOKEN="..."
export DROPI_EMAIL="..." DROPI_PASSWORD="..."
streamlit run dashboard/app.py
```
