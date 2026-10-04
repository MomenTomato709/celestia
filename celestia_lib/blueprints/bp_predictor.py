"""Blueprint del predictor de acciones: /predictor/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("predictor", __name__)

    @bp.route("/predictor/registrar_accion", methods=["POST"])
    def predictor_registrar():
        """Registra una acción del usuario para alimentar el predictor Markov."""
        data = flask_request.get_json(silent=True) or {}
        accion = (data.get("accion") or "").strip()
        if not accion:
            return jsonify({"error": "accion requerida"}), 400
        try:
            aid = api.orch.registrar_accion_usuario(
                accion=accion,
                contexto=(data.get("contexto") or "")[:200],
                accion_previa=(data.get("accion_previa") or "")[:100],
            )
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"id": aid, "accion": accion})

    @bp.route("/predictor/siguiente", methods=["GET"])
    def predictor_siguiente():
        """Top-K acciones probables en el contexto dado (o el actual por defecto)."""
        bucket = flask_request.args.get("bucket")
        dia = flask_request.args.get("dia_semana")
        previa = flask_request.args.get("accion_previa", "")
        try:
            top_k = min(int(flask_request.args.get("top_k", 3)), 20)
        except (TypeError, ValueError):
            top_k = 3
        modo = flask_request.args.get("modo", "relajado")
        if modo not in ("estricto", "relajado", "global"):
            return jsonify({"error": "modo debe ser estricto|relajado|global"}), 400
        try:
            out = api.orch.predecir_siguiente_accion(
                bucket=bucket, dia_semana=dia,
                accion_previa=previa, top_k=top_k, modo_contexto=modo,
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify(out)

    return bp
