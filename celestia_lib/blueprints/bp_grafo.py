"""Blueprint del grafo de conocimiento: /grafo/* (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("grafo", __name__)

    @bp.route("/grafo", methods=["GET"])
    def grafo_resumen():
        """Resumen del grafo de conocimiento: stats + lista de entidades top.

        Query params:
          tipo:   filtra por tipo (persona, lugar, etc.).
          limit:  máx entidades a devolver (default 100, max 500).
        """
        kg = getattr(api.orch, "knowledge", None)
        if kg is None:
            return jsonify({"error": "world model no disponible"}), 503
        tipo = flask_request.args.get("tipo")
        try:
            limit = min(int(flask_request.args.get("limit", 100)), 500)
        except ValueError:
            limit = 100
        return jsonify({
            "stats": kg.stats(),
            "entidades": kg.listar_entidades(tipo=tipo, limit=limit),
        })

    @bp.route("/grafo/entidad/<nombre>", methods=["GET"])
    def grafo_entidad(nombre: str):
        """Detalle de una entidad: atributos + vecinos + historial de estados.

        Query params:
          profundidad: BFS hasta N saltos (default 2, max 4).
          tipo:        desambigua si hay entidades del mismo nombre en distintos tipos.
        """
        kg = getattr(api.orch, "knowledge", None)
        if kg is None:
            return jsonify({"error": "world model no disponible"}), 503
        try:
            profundidad = min(int(flask_request.args.get("profundidad", 2)), 4)
        except ValueError:
            profundidad = 2
        tipo = flask_request.args.get("tipo")
        ent = kg.obtener_entidad(nombre, tipo=tipo)
        if not ent:
            return jsonify({"error": "entidad no encontrada", "nombre": nombre}), 404
        ent_id = int(ent["id"])
        # Estados actuales por propiedad
        cur = kg.conn.cursor()
        cur.execute(
            "SELECT propiedad, valor, ts, confianza FROM kg_estados "
            "WHERE entidad_id=? ORDER BY ts DESC", (ent_id,)
        )
        estados_por_prop: dict = {}
        for prop, valor, ts, conf in cur.fetchall():
            if prop not in estados_por_prop:
                estados_por_prop[prop] = {"valor": valor, "ts": ts, "confianza": conf}
        return jsonify({
            "entidad": ent,
            "estados_actuales": estados_por_prop,
            "vecindad": kg.vecinos(ent_id, profundidad=profundidad),
        })

    return bp
