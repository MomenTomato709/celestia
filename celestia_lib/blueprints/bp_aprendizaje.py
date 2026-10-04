"""Blueprint de aprendizaje continuo / eval: /aprendizaje/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("aprendizaje", __name__)

    @bp.route("/aprendizaje/estado", methods=["GET"])
    def aprendizaje_estado():
        """Estado del aprendizaje continuo (roadmap AGI #2).

        Devuelve: hardware (RAM/GPU/peft/entrenable), dataset (totales y
        elegibles), último entrenamiento, historial.
        """
        try:
            return jsonify(api.orch.aprendizaje_estado())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/aprendizaje/entrenar", methods=["POST"])
    def aprendizaje_entrenar():
        """Dispara un entrenamiento LoRA inmediato.

        POST {"dry_run": true|false}. En proot Android sin GPU el
        entrenamiento real es inviable — usar dry_run para validar pipeline.
        """
        data = flask_request.get_json(silent=True) or {}
        dry_run = data.get("dry_run")
        try:
            out = api.orch.aprendizaje_entrenar(
                dry_run=None if dry_run is None else bool(dry_run)
            )
            return jsonify(out)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/aprendizaje/adaptadores", methods=["GET"])
    def aprendizaje_adaptadores():
        """Lista de adaptadores LoRA entrenados (todo el historial)."""
        try:
            manager = api.orch.aprendizaje_manager
            return jsonify({"adaptadores": manager.listar()})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/aprendizaje/aplicar", methods=["POST"])
    def aprendizaje_aplicar():
        """Aplica el último adapter exitoso sobre el modelo local cargado."""
        try:
            ok = api.orch.aprendizaje_aplicar_ultimo_adapter()
            return jsonify({"aplicado": bool(ok)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/aprendizaje/eval", methods=["POST"])
    def aprendizaje_eval():
        """Corre el banco de eval gold y persiste el run.

        Body JSON opcional: {nota?, max_prompts?}.
        Devuelve métricas + delta vs run anterior si existe.
        """
        data = flask_request.get_json(silent=True) or {}
        nota = (data.get("nota") or "")[:200]
        try:
            max_prompts = data.get("max_prompts")
            if max_prompts is not None:
                max_prompts = int(max_prompts)
        except (TypeError, ValueError):
            max_prompts = None
        try:
            out = api.orch.evaluar_modelo(nota=nota, max_prompts=max_prompts)
        except Exception as e:
            logger.exception("eval falló")
            return jsonify({"error": str(e)}), 500
        return jsonify(out)

    @bp.route("/aprendizaje/eval/historial", methods=["GET"])
    def aprendizaje_eval_historial():
        """Lista runs históricos del banco de eval (sin detalles por prompt)."""
        try:
            limit = min(int(flask_request.args.get("limit", 20)), 100)
        except (TypeError, ValueError):
            limit = 20
        try:
            hist = api.orch.historial_evaluaciones(limit=limit)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"runs": hist})

    @bp.route("/aprendizaje/eval/<int:run_id>", methods=["GET"])
    def aprendizaje_eval_detalle(run_id: int):
        """Detalle completo de un run (con resultados por prompt)."""
        try:
            out = api.orch.detalle_evaluacion(run_id)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        if out is None:
            return jsonify({"error": "run no encontrado"}), 404
        return jsonify(out)

    @bp.route("/aprendizaje/feedback", methods=["POST"])
    def aprendizaje_feedback():
        """Registra 👍 (1) / 👎 (-1) / neutro (0) sobre una conversación.

        Body JSON: {conv_id, valoracion, comentario?}.
        Alimenta la pipeline DPO para futuros entrenamientos.
        """
        data = flask_request.get_json(silent=True) or {}
        try:
            conv_id = int(data.get("conv_id"))
            valoracion = int(data.get("valoracion"))
        except (TypeError, ValueError):
            return jsonify({"error": "conv_id y valoracion (int) requeridos"}), 400
        if valoracion not in (-1, 0, 1):
            return jsonify({"error": "valoracion debe ser -1/0/1"}), 400
        comentario = (data.get("comentario") or "")[:500]
        try:
            api.orch.registrar_feedback(conv_id, valoracion, comentario)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "conv_id": conv_id, "valoracion": valoracion})

    return bp
