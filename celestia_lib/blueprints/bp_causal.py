"""Blueprint de razonamiento causal: /causal/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("causal", __name__)

    @bp.route("/causal/observar", methods=["POST"])
    def causal_observar():
        """Registra observación causa→efecto con actualización bayesiana.

        Body: {causa, efecto, ocurrio?=true, delay_seg?, duracion_seg?, fuente?}.
        """
        data = flask_request.get_json(silent=True) or {}
        causa = (data.get("causa") or "").strip()
        efecto = (data.get("efecto") or "").strip()
        if not causa or not efecto:
            return jsonify({"error": "causa y efecto requeridos"}), 400
        try:
            d = api.orch.observar_causal(
                causa=causa, efecto=efecto,
                ocurrio=bool(data.get("ocurrio", True)),
                delay_seg=data.get("delay_seg"),
                duracion_seg=data.get("duracion_seg"),
                fuente=(data.get("fuente") or "user")[:32],
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify(d)

    @bp.route("/causal/simular", methods=["POST"])
    def causal_simular():
        """Simulación Monte Carlo de efectos futuros.

        Body: {causa, horizonte_seg?, umbral_prob?=0.2, formato?='estructurado'|'texto'}.
        """
        data = flask_request.get_json(silent=True) or {}
        causa = (data.get("causa") or "").strip()
        if not causa:
            return jsonify({"error": "causa requerida"}), 400
        try:
            horizonte = data.get("horizonte_seg")
            horizonte = float(horizonte) if horizonte is not None else None
            umbral = float(data.get("umbral_prob", 0.2))
        except (TypeError, ValueError):
            return jsonify({"error": "horizonte_seg/umbral_prob inválidos"}), 400
        formato = data.get("formato") or "estructurado"
        if formato not in ("estructurado", "texto"):
            return jsonify({"error": "formato debe ser estructurado o texto"}), 400
        try:
            res = api.orch.simular_que_pasa_si(
                causa=causa, horizonte_seg=horizonte,
                umbral_prob=umbral, formato=formato,
            )
        except Exception as e:
            logger.exception("causal/simular falló")
            return jsonify({"error": str(e)}), 500
        if formato == "texto":
            return jsonify({"causa": causa, "respuesta": res})
        return jsonify({"causa": causa, "efectos": res, "n": len(res)})

    @bp.route("/causal/links", methods=["GET"])
    def causal_links():
        """Lista enlaces causales aprendidos, filtrables por causa/efecto."""
        causa = flask_request.args.get("causa")
        efecto = flask_request.args.get("efecto")
        try:
            min_conf = float(flask_request.args.get("min_confianza", 0.0))
            limit = min(int(flask_request.args.get("limit", 200)), 1000)
        except (TypeError, ValueError):
            return jsonify({"error": "min_confianza/limit inválidos"}), 400
        try:
            links = api.orch.causal_listar(
                causa=causa, efecto=efecto,
                min_confianza=min_conf, limit=limit,
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"links": links, "n": len(links)})

    return bp
