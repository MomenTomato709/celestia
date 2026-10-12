# celestia_lib/blueprints/bp_trabajos.py
"""El panel «Trabajando»: lo que el chat lee para enseñar a Celestia trabajando.

GET  /trabajos[?hilo=…&activos=1]   los encargos recientes (sin sucesos)
GET  /trabajos/<id>?desde=N         estado y sucesos nuevos (el chat pregunta cada ~0,7 s)
POST /trabajos/<id>/parar           pedir que pare al acabar el paso en curso
GET  /trabajos/<id>/vista/<ruta>    un archivo de la carpeta del encargo (vista previa)

Mismo permiso que el chat; exentas del límite de peticiones (api.py). El gasto
(`coste`) se cuenta pero no sale de aquí: Enzo, 5 oct 2026, «quita el contador
ese de cuánto gasta». Diseño: docs/superpowers/specs/2026-10-05-panel-trabajando-design.md.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request as flask_request, send_file

from .. import trabajo


def _publico(datos: dict) -> dict:
    datos = dict(datos)
    datos.pop("coste", None)
    if "eventos" in datos:
        datos["eventos"] = [{k: v for k, v in e.items() if k not in ("carpeta", "pid", "arranque")}
                            for e in datos["eventos"]]
    return datos


def crear(api) -> Blueprint:
    bp = Blueprint("trabajos", __name__)

    @bp.route("/trabajos", methods=["GET"])
    def trabajos_recientes():
        hilo = (flask_request.args.get("hilo") or "").strip()[:64] or None
        lista = trabajo.recientes(hilo)
        if flask_request.args.get("activos") in ("1", "true"):
            lista = [t for t in lista if t["estado"] in ("trabajando", "atascado")]
        return jsonify({"trabajos": [_publico(t) for t in lista]})

    @bp.route("/trabajos/<id_>", methods=["GET"])
    def trabajo_leer(id_):
        try:
            desde = max(0, int(flask_request.args.get("desde", 0)))
        except ValueError:
            desde = 0
        datos = trabajo.leer(id_, desde)
        if datos is None:
            return jsonify({"error": "ese encargo no existe"}), 404
        return jsonify(_publico(datos))

    @bp.route("/trabajos/<id_>/parar", methods=["POST"])
    def trabajo_parar(id_):
        if not trabajo.pedir_parada(id_):
            return jsonify({"error": "ese encargo no está trabajando"}), 409
        return jsonify({"parando": id_})

    @bp.route("/trabajos/<id_>/vista/<path:ruta>", methods=["GET"])
    def trabajo_vista(id_, ruta):
        archivo = trabajo.archivo_de_vista(id_, ruta)
        if archivo is None:
            abort(404)
        resp = send_file(archivo)
        # Encerrado: corre sus scripts, pero sin cookies ni acceso a Celestia.
        resp.headers["Content-Security-Policy"] = "sandbox allow-scripts"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return bp
