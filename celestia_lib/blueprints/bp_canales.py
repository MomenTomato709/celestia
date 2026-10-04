"""Blueprint de canales de conversación: /canales/*.

Expone el `GestorCanales` por HTTP para que el usuario pueda encender y apagar
los puentes (WhatsApp, Telegram, Discord) desde cualquier sitio: desde el propio
chat, desde el cliente de Termux o desde un script.

La regla de negocio importante vive en el gestor, no aquí: encender un canal
apaga los demás salvo que se pida lo contrario, porque esto corre en un móvil y
cada puente encendido gasta RAM y batería.
"""
import logging
from flask import Blueprint, request as flask_request, jsonify

from .. import bandeja
from ..canales import GestorCanales

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    bp = Blueprint("canales", __name__)

    def _gestor() -> GestorCanales:
        # Se reutiliza el del api si existe (lo crea WhatsAppAPI.__init__), y si
        # no se instancia al vuelo: el gestor no guarda estado en memoria.
        return getattr(api, "canales", None) or GestorCanales()

    @bp.route("/canales", methods=["GET"])
    def canales_listar():
        """Estado de todos los canales: cuál está vivo, cuál está listo y qué falta."""
        g = _gestor()
        return jsonify({
            "canales":   g.estado(),
            "preferido": g.preferencia(),
            "resumen":   g.resumen(),
        })

    @bp.route("/canales/activar", methods=["POST"])
    def canales_activar():
        """Enciende un canal.

        Body JSON: {canal: str, exclusivo?: bool}.
        Con `exclusivo` (por defecto) apaga el resto de puentes.
        """
        data = flask_request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "body debe ser objeto JSON"}), 400
        nombre = (data.get("canal") or "").strip().lower()
        if not nombre:
            return jsonify({"error": "canal requerido"}), 400
        exclusivo = data.get("exclusivo", True)
        if not isinstance(exclusivo, bool):
            return jsonify({"error": "exclusivo debe ser booleano"}), 400

        g = _gestor()
        # Aceptamos alias ("wa", "tg") igual que en el chat.
        from ..canales import resolver_alias
        canonico = resolver_alias(nombre) or nombre
        resultado = g.activar(canonico, exclusivo=exclusivo)
        return jsonify(resultado), (200 if resultado.get("ok") else 409)

    @bp.route("/canales/parar", methods=["POST"])
    def canales_parar():
        """Apaga un puente. Body JSON: {canal: str}.

        `canal: "todos"` apaga todos los puentes de golpe y deja solo Termux
        (modo mínimo consumo).
        """
        data = flask_request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "body debe ser objeto JSON"}), 400
        nombre = (data.get("canal") or "").strip().lower()
        if not nombre:
            return jsonify({"error": "canal requerido"}), 400

        from ..canales import resolver_alias
        g = _gestor()
        if nombre in ("todos", "todo", "all", "*"):
            resultado = g.parar_todos()
            return jsonify(resultado), (200 if resultado.get("ok") else 409)
        canonico = resolver_alias(nombre) or nombre
        resultado = g.parar(canonico)
        return jsonify(resultado), (200 if resultado.get("ok") else 409)

    @bp.route("/pendientes", methods=["GET"])
    def pendientes_recoger():
        """El correo que Celestia dejó para este canal mientras nadie escuchaba.

        Lo que se genera en segundo plano (un PDF de 30 s, un recordatorio, una
        habilidad aprendida) no cabe en ninguna respuesta HTTP porque nace
        cuando la petición ya se cerró. Queda aquí y lo recoge el canal al que
        iba: el cliente de Termux los suyos, el puente de Discord los suyos.

        Parámetros:
          para=<canal>   solo los de ese canal (sin él, TODOS: red de seguridad
                         para que nada quede huérfano si el canal cambió).
          mirar=1        no vacía la bandeja (útil para depurar).

        Recoger BORRA lo recogido: quien lo pide se hace responsable de
        entregarlo.
        """
        para  = (flask_request.args.get("para") or "").strip().lower() or None
        mirar = flask_request.args.get("mirar") in ("1", "true", "si", "sí")
        mensajes = bandeja.asomarse(para) if mirar else bandeja.recoger(para)
        return jsonify({"mensajes": mensajes, "total": len(mensajes)})

    return bp
