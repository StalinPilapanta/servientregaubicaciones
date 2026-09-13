"""
Servicio HTTP para conectar el buscador de agencias Servientrega con el
chat en vivo de Chatea Pro (via External Request / webhook).

Endpoints:
  GET  /salud
        -> {"ok": true}  (para probar que el servicio esta arriba)

  GET  /buscar?texto=<direccion del cliente>
  POST /buscar   body JSON: {"texto": "<direccion del cliente>"}
        -> {
             "ciudad_detectada": "...",
             "resultados": [ { nombre, direccion, sector, telefono, ... } ],
             "mensaje": "texto listo para responder en el chat"
           }

Como levantarlo:
    pip3 install flask
    python3 servidor.py
    # queda en http://localhost:8000

En Chatea Pro (flujo del bot), agrega una accion "External Request":
    Metodo: GET
    URL:    https://TU-DOMINIO/buscar?texto={{last_user_input}}
    Guarda la respuesta y muestra el campo "mensaje" al cliente.

Nota: para que Chatea Pro (en la nube) llegue a este servicio debe estar
expuesto en internet (deploy en un VPS, o un tunel tipo ngrok/cloudflared
mientras pruebas).
"""

from flask import Flask, request, jsonify

from buscar_agencia import cargar_directorio, buscar

app = Flask(__name__)

# Cargamos el directorio UNA sola vez al arrancar (mas rapido por request).
AGENCIAS = cargar_directorio()
print(f"Directorio cargado: {len(AGENCIAS)} agencias.")


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


if __name__ == "__main__":
    # Solo para desarrollo local. En produccion lo sirve Gunicorn (ver Dockerfile).
    import os
    puerto = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=puerto, debug=False)
