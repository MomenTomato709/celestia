"""Blueprint de razonamiento Datalog: /razonar/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
El import relativo de datalog sube un nivel (`..datalog`) por estar en el subpaquete.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("razonar", __name__)

    @bp.route("/razonar/deducir", methods=["POST"])
    def razonar_deducir():
        """Deduce relaciones implícitas vía Datalog.

        Body JSON: {entidad: str, relaciones?: list[str]}.
        Devuelve lista de tuplas (sujeto, relacion, objeto, deducida).
        Útil para responder «¿quién es mi abuelo?» — pasa entidad="usuario"
        y relaciones=["abuelo_de", "tio_de", "primo_de"].
        """
        kg = getattr(api.orch, "knowledge", None)
        if kg is None:
            return jsonify({"error": "world model no disponible"}), 503
        data = flask_request.get_json(silent=True) or {}
        entidad = (data.get("entidad") or "").strip()
        if not entidad:
            return jsonify({"error": "entidad requerida"}), 400
        relaciones = data.get("relaciones")
        if relaciones is not None and not isinstance(relaciones, list):
            return jsonify({"error": "relaciones debe ser lista"}), 400
        try:
            deducidas = kg.relaciones_deducidas(entidad, relaciones=relaciones)
        except Exception as e:
            logger.exception("razonar/deducir falló")
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "entidad": entidad,
            "relaciones": deducidas,
            "n": len(deducidas),
        })

    @bp.route("/razonar/reglas", methods=["GET"])
    def razonar_reglas():
        """Lista las reglas Datalog activas en el motor de deducción."""
        try:
            from ..datalog import DatalogEngine, reglas_familia_es
            rs = reglas_familia_es()
            return jsonify({
                "reglas": [str(r) for r in rs],
                "n": len(rs),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return bp
