"""Blueprint del planificador multiobjetivo / PDDL: /plan/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
`_plan_a_dict` es un helper de módulo de api.py; se importa dentro de `crear`
(api ya está cargado cuando se registran los blueprints → sin import circular).
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    from ..api import _plan_a_dict
    bp = Blueprint("plan", __name__)

    @bp.route("/plan", methods=["GET", "POST"])
    def planificador():
        """Planificador multiobjetivo.

        POST con {situacion, goal_ids?}: genera plan, devuelve detalle.
        GET sin args: lista últimos planes (con query estado para filtrar).
        """
        planner = getattr(api.orch, "planner", None)
        if planner is None:
            return jsonify({"error": "planner no disponible"}), 503
        if flask_request.method == "POST":
            data = flask_request.get_json(silent=True) or {}
            situacion = (data.get("situacion") or "").strip()
            if not situacion:
                return jsonify({"error": "situacion requerida"}), 400
            goal_ids = data.get("goal_ids")
            if goal_ids is not None and not isinstance(goal_ids, list):
                return jsonify({"error": "goal_ids debe ser lista"}), 400
            try:
                plan = api.orch.planificar(situacion, goal_ids=goal_ids)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
            if plan is None:
                return jsonify({"error": "no pude generar plan"}), 502
            return jsonify(_plan_a_dict(plan))
        # GET: lista
        estado = flask_request.args.get("estado")
        try:
            limit = min(int(flask_request.args.get("limit", 20)), 100)
        except ValueError:
            limit = 20
        planes = planner.listar_planes(estado=estado, limit=limit)
        return jsonify({
            "planes": [_plan_a_dict(p, incluir_pasos=False) for p in planes],
        })

    @bp.route("/plan/<int:plan_id>", methods=["GET"])
    def planificador_detalle(plan_id: int):
        planner = getattr(api.orch, "planner", None)
        if planner is None:
            return jsonify({"error": "planner no disponible"}), 503
        plan = planner.obtener_plan(plan_id)
        if plan is None:
            return jsonify({"error": "plan no encontrado"}), 404
        return jsonify(_plan_a_dict(plan))

    @bp.route("/plan/pddl", methods=["POST"])
    def planificador_pddl():
        """Genera un plan vía PDDL clásico (LLM traduce, A* resuelve).

        Body JSON: {situacion, goal_ids?}.
        Devuelve plan detallado o 502 si no se pudo planificar.
        """
        planner = getattr(api.orch, "planner", None)
        if planner is None:
            return jsonify({"error": "planner no disponible"}), 503
        data = flask_request.get_json(silent=True) or {}
        situacion = (data.get("situacion") or "").strip()
        if not situacion:
            return jsonify({"error": "situacion requerida"}), 400
        goal_ids = data.get("goal_ids")
        if goal_ids is not None and not isinstance(goal_ids, list):
            return jsonify({"error": "goal_ids debe ser lista"}), 400
        try:
            plan = api.orch.planificar_pddl(situacion, goal_ids=goal_ids)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        if plan is None:
            return jsonify({
                "error": "no pude generar plan PDDL (traducción o búsqueda falló)",
            }), 502
        return jsonify(_plan_a_dict(plan))

    @bp.route("/plan/<int:plan_id>/ejecutar", methods=["POST"])
    def planificador_ejecutar(plan_id: int):
        """Ejecuta un plan persistido usando AgentTools como tool_runner real.

        Body JSON opcional: {max_replans?: int}.
        Devuelve el plan actualizado con estado de cada paso.
        """
        planner = getattr(api.orch, "planner", None)
        if planner is None:
            return jsonify({"error": "planner no disponible"}), 503
        data = flask_request.get_json(silent=True) or {}
        try:
            max_replans = int(data.get("max_replans", 1))
        except (TypeError, ValueError):
            max_replans = 1
        max_replans = max(0, min(max_replans, 3))
        try:
            plan = api.orch.ejecutar_plan(plan_id, max_replans=max_replans)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            logger.exception("Error ejecutando plan %s", plan_id)
            return jsonify({"error": str(e)}), 500
        return jsonify(_plan_a_dict(plan))

    @bp.route("/plan/goal", methods=["GET", "POST"])
    def planificador_goals():
        """GET lista goals activos. POST crea uno con {descripcion, prioridad?, criterio_exito?}."""
        planner = getattr(api.orch, "planner", None)
        if planner is None:
            return jsonify({"error": "planner no disponible"}), 503
        if flask_request.method == "POST":
            data = flask_request.get_json(silent=True) or {}
            desc = (data.get("descripcion") or "").strip()
            if not desc:
                return jsonify({"error": "descripcion requerida"}), 400
            try:
                g = planner.crear_goal(
                    desc,
                    prioridad=int(data.get("prioridad", 5)),
                    criterio_exito=(data.get("criterio_exito") or "").strip(),
                )
            except (ValueError, TypeError) as e:
                return jsonify({"error": str(e)}), 400
            return jsonify({
                "id": g.id, "descripcion": g.descripcion, "prioridad": g.prioridad,
                "criterio_exito": g.criterio_exito, "estado": g.estado,
            })
        estado = flask_request.args.get("estado", "activo") or None
        goals = planner.listar_goals(estado=estado)
        return jsonify({
            "goals": [
                {"id": g.id, "descripcion": g.descripcion, "prioridad": g.prioridad,
                 "criterio_exito": g.criterio_exito, "estado": g.estado}
                for g in goals
            ],
        })

    return bp
