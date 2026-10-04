"""Actualizarse desde la propia app: /actualizacion (ver), /actualizacion/buscar
y /actualizacion/instalar. La lógica vive en `celestia_lib/actualizar.py`.

Instalar sólo se acepta desde el propio aparato: quien entra por la red (otro
móvil con la llave) puede charlar, pero no reinstalar el programa del
ordenador de otro.
"""
from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request as flask_request

from .. import actualizar

DESDE_AQUI = ("127.0.0.1", "::1")


def crear(api) -> Blueprint:
    bp = Blueprint("actualizar", __name__)

    @bp.route("/actualizacion", methods=["GET"])
    def actualizacion():
        """Versión actual, la última publicada y en qué fase va la instalación."""
        return jsonify(actualizar.comprobar())

    @bp.route("/actualizacion/buscar", methods=["POST"])
    def actualizacion_buscar():
        return jsonify(actualizar.comprobar(forzar=True))

    @bp.route("/actualizacion/instalar", methods=["POST"])
    def actualizacion_instalar():
        if (flask_request.remote_addr or "") not in DESDE_AQUI:
            return jsonify({"error": "sólo se actualiza desde el propio aparato"}), 403
        if not actualizar.archivo_de_este_sistema():
            return jsonify({"error": "esta copia de Celestia se actualiza con git"}), 400
        threading.Thread(target=actualizar.instalar, daemon=True,
                         name="actualizar-instalar").start()
        return jsonify({"ok": True})

    return bp
