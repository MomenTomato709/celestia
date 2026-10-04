"""La Celestia de PC: seguir despierta, encenderse con el ordenador y apagarse.

GET/POST /escritorio — los dos ajustes (celestia_lib/escritorio.py).
POST /apagar         — apagarla del todo (con la ventana cerrada no hay otra
                       forma que el administrador de tareas).

Sólo desde el propio aparato: quien entra por la red con la llave puede
charlar, pero no apagar el ordenador de otro ni tocar su arranque.
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from flask import Blueprint, jsonify, request as flask_request

from .. import escritorio

logger = logging.getLogger("celestia_v1")
DESDE_AQUI = ("127.0.0.1", "::1")


def _apagarse() -> None:
    """Como un Ctrl+C: el servidor se cierra por su camino normal. Si en diez
    segundos sigue vivo, se corta."""
    logger.info("Apagado pedido desde Ajustes")
    try:
        signal.raise_signal(signal.SIGINT)
    except Exception as e:
        logger.warning("No pude apagarme con SIGINT (%s): corto", e)
        os._exit(0)
    threading.Timer(10, lambda: os._exit(0)).start()


def crear(api) -> Blueprint:
    bp = Blueprint("escritorio", __name__)

    def _desde_fuera():
        return (flask_request.remote_addr or "") not in DESDE_AQUI

    @bp.route("/escritorio", methods=["GET", "POST"])
    def ajustes_escritorio():
        if not escritorio.es_escritorio():
            return jsonify({"es_escritorio": False})
        if flask_request.method == "POST":
            if _desde_fuera():
                return jsonify({"error": "sólo desde el propio aparato"}), 403
            datos = flask_request.get_json(force=True, silent=True) or {}
            cambios = {k: bool(datos[k]) for k in ("segundo_plano", "al_iniciar") if k in datos}
            return jsonify({"es_escritorio": True, **escritorio.guardar(**cambios)})
        return jsonify({"es_escritorio": True, **escritorio.leer()})

    @bp.route("/apagar", methods=["POST"])
    def apagar():
        if not escritorio.es_escritorio():
            return jsonify({"error": "aquí no se apaga así"}), 400
        if _desde_fuera():
            return jsonify({"error": "sólo desde el propio aparato"}), 403
        threading.Timer(0.5, _apagarse).start()     # primero se contesta
        return jsonify({"ok": True})

    return bp
