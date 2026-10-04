"""Quién puede hablar con Celestia por los canales de fuera.

EL AGUJERO QUE ESTO TAPA (4 sep 2026): los puentes de WhatsApp, Telegram y
Discord atendían a **cualquiera**. El de Telegram lo decía en su cabecera sin
inmutarse («múltiples usuarios: cada chat_id se atiende independientemente»), y
el de WhatsApp solo descartaba los mensajes propios (`fromMe`). Quien diera con
el bot —o le escribiera al número— hablaba con la Celestia de su dueño: con su
memoria («¿qué sabes de mí?» suelta nombre, trabajo y planes), su historial y
sus herramientas (leer ficheros, ejecutar comandos de la lista, mandar SMS).

Por qué vive AQUÍ y no en cada puente: son tres, en dos lenguajes, y el criterio
tiene que ser uno. Dos sitios decidiendo lo mismo se separan solas — ya pasó en
la S49 con los hechos y en la S53 con las quejas. Los puentes solo dicen QUIÉN
escribe; quien decide es el servidor.

Los canales de dentro (el chat web, Termux, `hablar.py`) no pasan por aquí: esos
ya los guarda el token de la API, y quien está en el móvil está dentro.

Configuración, en el `.env`:

    CELESTIA_PERMITIDOS_WHATSAPP=34600111222,34600333444
    CELESTIA_PERMITIDOS_TELEGRAM=123456789
    CELESTIA_PERMITIDOS_DISCORD=987654321

**Sin lista, el canal está CERRADO.** Lo eligió Enzo (4 sep 2026) sobre la
alternativa de que el primero que escribiera se quedara como dueño: esa ventana
es real —probándolo, un número inventado se quedó de dueño de WhatsApp— y quien
llegue antes que tú se queda con el canal. Para que cerrar no signifique «no
funciona y no sé por qué», el primer intento de cada desconocido deja en el log
la línea exacta que hay que pegar en el `.env`.

WhatsApp trae su lista puesta sin tocar nada: el número de Enzo ya está en
`whatsapp_bridge/numero.txt`, que es de donde se lee si no hay variable.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger("celestia_v1")

# Por dónde puede escribir alguien que no seas tú. El resto de canales entran
# por la API, que ya pide llave.
CANALES_EXTERNOS = frozenset({"whatsapp", "telegram", "discord", "sms"})

_lock = threading.Lock()


def es_externo(canal: str) -> bool:
    return (canal or "").strip().lower() in CANALES_EXTERNOS


def _clave(canal: str) -> str:
    return f"CELESTIA_PERMITIDOS_{canal.strip().upper()}"


def normalizar(canal: str, remitente) -> str:
    """El identificador, en la forma en que se compara.

    WhatsApp manda `34600111222@s.whatsapp.net` (y `@g.us` para los grupos);
    Telegram y Discord, números. Se compara siempre lo mismo para que apuntar el
    número a mano en el `.env` funcione tal y como uno lo escribiría.
    """
    ident = str(remitente or "").strip()
    if not ident:
        return ""
    canal = (canal or "").lower()
    if canal in ("whatsapp", "sms"):
        # Un grupo no es una persona: se queda con su identificador entero, que
        # es lo que hay que poner en la lista si se quiere permitir el grupo.
        if "@g.us" in ident:
            return ident.lower()
        ident = ident.split("@", 1)[0]
        return re.sub(r"\D", "", ident)
    return re.sub(r"[^\w-]", "", ident)[:64]


# El número del dueño ya vive aquí desde antes (es a quien Celestia le escribe),
# así que WhatsApp no necesita configuración nueva para quedar en «solo yo».
#
# La ruta se saca del propio módulo, no escrita a mano: cada Celestia vive en el
# aparato de su dueño y no todas están en /root/Celestia (el contenedor, otra
# instalación, otro móvil). Con la ruta fija, en cualquier otra el fichero no
# existiría y WhatsApp se quedaría cerrado sin que nadie supiera por qué.
_NUMERO_DEL_DUENO = str(Path(__file__).resolve().parent.parent
                        / "whatsapp_bridge" / "numero.txt")


def permitidos(canal: str) -> set[str]:
    crudo = os.environ.get(_clave(canal), "")
    lista = {n for n in (normalizar(canal, p) for p in crudo.split(",")) if n}
    if lista:
        return lista
    if (canal or "").lower() == "whatsapp":
        try:
            with open(_NUMERO_DEL_DUENO, encoding="utf-8") as f:
                suyo = normalizar("whatsapp", f.read())
            if suyo:
                return {suyo}
        except OSError:
            pass
    return set()


# ── La ventana de emparejamiento ────────────────────────────────────────────
# «Cerrado por defecto» no puede significar «y apáñate para abrirlo»: nadie sabe
# de memoria su chat id de Telegram. La salida es la de cualquier aparato que se
# empareja: una ventana que ABRE EL DUEÑO a propósito, desde un canal donde ya
# está identificado, y que se cierra sola —en cuanto entra alguien o pasa el
# rato—. No es el «el primero que escriba se queda» que se descartó: aquello
# estaba abierto siempre y sin que nadie lo pidiera.
_ventanas: dict[str, float] = {}
VENTANA_SEGUNDOS = 300


def abrir_emparejamiento(canal: str, segundos: int = VENTANA_SEGUNDOS) -> float:
    """Deja que el próximo que escriba por `canal` se quede como dueño."""
    canal = (canal or "").strip().lower()
    with _lock:
        _ventanas[canal] = time.time() + max(30, segundos)
        return _ventanas[canal]


def emparejamiento_abierto(canal: str) -> bool:
    canal = (canal or "").strip().lower()
    hasta = _ventanas.get(canal, 0)
    return bool(hasta) and time.time() < hasta


def cerrar_emparejamiento(canal: str) -> None:
    with _lock:
        _ventanas.pop((canal or "").strip().lower(), None)


def _apuntar(canal: str, ident: str) -> bool:
    """Añade a `ident` a la lista del canal, en el `.env` y en el entorno."""
    from celestia_lib.config import fijar_en_env      # perezoso: evita el círculo

    clave = _clave(canal)
    ya = permitidos(canal)
    ya.add(ident)
    valor = ",".join(sorted(ya))
    try:
        fijar_en_env(clave, valor,
                     comentario=f"Quién puede hablarle a Celestia por {canal}")
        ok = True
    except Exception as e:                            # el .env puede no dejarse
        logger.warning("No pude apuntar a %s en el .env: %s", ident, e)
        ok = False
    os.environ[clave] = valor      # en memoria vale igual, aunque falle el fichero
    return ok


# Para no repetir el mismo aviso en cada mensaje de un pesado.
_ya_avisado: set[tuple[str, str]] = set()


def _avisar_una_vez(canal: str, ident: str, hay_lista: bool) -> None:
    if (canal, ident) in _ya_avisado:
        return
    _ya_avisado.add((canal, ident))
    if len(_ya_avisado) > 200:                 # que no crezca sin fin
        _ya_avisado.clear()
    porque = "no está en la lista" if hay_lista else "no hay nadie autorizado todavía"
    logger.warning(
        "%s: no atiendo a %s (%s). Si eres tú, pega esto en el .env y reinicia:"
        "  %s=%s", canal, ident, porque, _clave(canal), ident)


def puede_hablar(canal: str, remitente) -> tuple[bool, str]:
    """¿Atiendo a este mensaje? Devuelve (sí/no, motivo).

    El motivo se registra: un booleano a secas no deja ver por qué se rechazó
    algo, que es justo lo que hace falta cuando alguien dice «no me contesta».
    """
    canal = (canal or "").strip().lower()
    if not es_externo(canal):
        return True, "canal de dentro"

    ident = normalizar(canal, remitente)
    if not ident:
        # Fallar cerrado: un puente que no dice quién escribe es un puente
        # viejo, y de los viejos venía justo este agujero.
        return False, "el puente no dice quién escribe (¿sin actualizar?)"

    with _lock:
        lista = permitidos(canal)
        if ident in lista:
            return True, "en la lista"
    if emparejamiento_abierto(canal):
        cerrar_emparejamiento(canal)                  # una y no más: la abrió él
        _apuntar(canal, ident)
        logger.warning("%s: emparejado con %s (ventana abierta por el dueño)",
                       canal, ident)
        return True, "emparejado durante la ventana que abriste"
    # Cerrado: ni lista, ni emparejamiento. El aviso lleva la línea que hay que
    # pegar en el .env, para que «cerrado» no acabe siendo «lo abro entero».
    _avisar_una_vez(canal, ident, bool(lista))
    if not lista:
        return False, f"canal {canal} cerrado: nadie autorizado todavía"
    return False, f"{ident} no está en la lista de {canal}"


# Lo que se le contesta a quien no está en la lista. Fijo y corto: no pasa por
# el modelo (ni gasta cuota ni le da un sitio donde intentar convencerlo) y no
# confirma qué es esto ni de quién es.
NO_ERES_TU = "No estoy disponible en este chat."
