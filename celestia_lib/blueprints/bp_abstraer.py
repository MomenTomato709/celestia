"""Blueprint de abstracción / síntesis de programas: /abstraer/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("abstraer", __name__)

    @bp.route("/abstraer/inducir", methods=["POST"])
    def abstraer_inducir():
        """Inducción de regla a partir de ejemplos (roadmap AGI #5).

        POST {"ejemplos": [{"input": ..., "output": ...}], "usar_llm": true,
              "persistir": true}
        Devuelve: {resultado: {descripcion, codigo, accuracy_train,
                   accuracy_holdout, aceptada, fallos}, patron_id}
        """
        data = flask_request.get_json(silent=True) or {}
        ejemplos = data.get("ejemplos") or []
        if not isinstance(ejemplos, list) or not ejemplos:
            return jsonify({"error": "ejemplos requeridos (lista no vacía)"}), 400
        try:
            return jsonify(api.orch.inducir_regla(
                ejemplos,
                usar_llm=bool(data.get("usar_llm", True)),
                persistir=bool(data.get("persistir", True)),
            ))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/abstraer/aplicar", methods=["POST"])
    def abstraer_aplicar():
        """Aplica un patrón abstracto guardado a un input nuevo.

        POST {"patron_id": <int>, "input": <any>}
        """
        data = flask_request.get_json(silent=True) or {}
        pid = data.get("patron_id")
        if pid is None:
            return jsonify({"error": "patron_id requerido"}), 400
        try:
            return jsonify(api.orch.aplicar_patron_abstracto(
                int(pid), data.get("input")
            ))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/abstraer/patrones", methods=["GET"])
    def abstraer_patrones():
        """Lista de patrones abstractos persistidos."""
        try:
            limit = min(int(flask_request.args.get("limit", 50)), 200)
        except ValueError:
            limit = 50
        q = (flask_request.args.get("q") or "").strip()
        try:
            store = api.orch.abstraccion_store
            patrones = store.buscar(q, limit=limit) if q else store.listar(limit=limit)
            return jsonify({
                "patrones": [p.to_dict() for p in patrones],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/abstraer/sintetizar", methods=["POST"])
    def abstraer_sintetizar():
        """Program synthesis: dado N ejemplos input→output, busca programa del DSL.

        Body: {ejemplos: [{input, output}], max_tamano?=4}.
        Devuelve programa, tamaño, primitivas usadas, accuracies, tiempo.
        """
        data = flask_request.get_json(silent=True) or {}
        ejemplos = data.get("ejemplos") or []
        if not isinstance(ejemplos, list) or not ejemplos:
            return jsonify({"error": "ejemplos requeridos (lista no vacía)"}), 400
        try:
            max_t = data.get("max_tamano")
            if max_t is not None:
                max_t = int(max_t)
        except (TypeError, ValueError):
            return jsonify({"error": "max_tamano debe ser int"}), 400
        try:
            out = api.orch.sintetizar_programa(ejemplos, max_tamano=max_t)
        except Exception as e:
            logger.exception("sintetizar falló")
            return jsonify({"error": str(e)}), 500
        return jsonify(out)

    return bp
