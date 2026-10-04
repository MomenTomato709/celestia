"""Bandeja de mensajes proactivos: lo que Celestia dice *sin* que le pregunten.

Un PDF que tarda 30 s en escribirse, un recordatorio que vence, una habilidad
que termina de aprenderse… todo eso nace cuando la petición HTTP del usuario ya
se cerró, así que no hay ninguna respuesta donde meterlo. Hasta ahora se
mandaba a ciegas al puente de WhatsApp (`127.0.0.1:8766`) y, si el usuario
estaba hablando por Termux o por Discord, el mensaje —y el fichero adjunto— se
perdía en el log: Celestia prometía «te lo paso en cuanto esté listo» y no lo
pasaba nunca.

Aquí se guardan esos mensajes hasta que alguien los recoge. Cada uno lleva el
canal al que iba dirigido, así que dos canales abiertos a la vez no se roban el
correo del otro: el cliente de Termux pide los suyos y el puente de Discord los
suyos.

Se persiste en disco a propósito: la API se reinicia a menudo (y a veces se
cae), y un documento que costó medio minuto de generar no debería evaporarse
por eso.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

BANDEJA_FILE = MEM_DIR / "pendientes.json"

# Un mensaje sin recoger tras esto ya no interesa: si el usuario vuelve dos días
# después, que no le caiga encima el PDF de anteayer.
CADUCIDAD_S = 24 * 3600

# Tope de seguridad: si nadie recoge (canal apagado), la bandeja no puede crecer
# sin fin en un móvil.
MAX_PENDIENTES = 50

_lock = threading.Lock()

# Por dónde entró la petición que se está atendiendo ahora mismo. Lo que nazca
# de ella y termine más tarde debe volver por ahí y no por «el canal que esté
# encendido»: con Termux y Discord a la vez, el fichero acababa en el chat
# equivocado (visto en vivo).
CANAL_PETICION: contextvars.ContextVar = contextvars.ContextVar(
    "canal_peticion", default="")


def _ahora() -> float:
    return time.time()


def _leer() -> List[Dict[str, Any]]:
    try:
        datos = json.loads(BANDEJA_FILE.read_text(encoding="utf-8"))
        return datos if isinstance(datos, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _escribir(mensajes: List[Dict[str, Any]]) -> None:
    try:
        BANDEJA_FILE.parent.mkdir(parents=True, exist_ok=True)
        BANDEJA_FILE.write_text(
            json.dumps(mensajes, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("No pude guardar la bandeja de pendientes: %s", e)


def _vigentes(mensajes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    corte = _ahora() - CADUCIDAD_S
    return [m for m in mensajes if float(m.get("ts", 0)) >= corte]


def encolar(texto: str, destino: str, documento_ruta: str = "",
            documento_mime: str = "", imagen_b64: str = "",
            audio_b64: str = "", audio_tipo: str = "") -> Dict[str, Any]:
    """Guarda un mensaje proactivo para el canal `destino`.

    Devuelve el mensaje tal como quedó guardado (con su id y su marca de tiempo).
    """
    mensaje: Dict[str, Any] = {
        "id": f"{int(_ahora() * 1000)}",
        "ts": _ahora(),
        "fecha": datetime.now(timezone.utc).isoformat(),
        "destino": destino or "termux",
        "texto": texto or "",
    }
    if documento_ruta:
        mensaje["documento_ruta"] = documento_ruta
    if documento_mime:
        mensaje["documento_mime"] = documento_mime
    if imagen_b64:
        mensaje["imagen_b64"] = imagen_b64
    if audio_b64:
        mensaje["audio_b64"] = audio_b64
        # Sin el tipo, quien lo recoge tiene que adivinarlo — y el chat web lo
        # adivinaba mal (daba Opus por MP3 y el reproductor salía mudo).
        mensaje["audio_tipo"] = audio_tipo or "audio/ogg"

    with _lock:
        mensajes = _vigentes(_leer())
        mensajes.append(mensaje)
        # Si se desborda, se tiran los más viejos: interesa lo último.
        if len(mensajes) > MAX_PENDIENTES:
            mensajes = mensajes[-MAX_PENDIENTES:]
        _escribir(mensajes)
    logger.info("Bandeja: encolado mensaje para '%s'%s",
                mensaje["destino"], " (con adjunto)" if documento_ruta else "")
    if mensaje["destino"] == "web":
        # Y que se quede: la bandeja se la da a la primera pestaña que pregunte
        # y la olvida (24 sep: Enzo no vio las fotos de la criatura).
        from . import buzon
        buzon.guardar(texto, imagen_b64)
        # Con la app cerrada nadie recoge la bandeja: que el móvil avise.
        try:
            from . import push
            push.avisar(texto or ("Te ha dejado un documento" if documento_ruta
                                  else "Te ha dejado algo"))
        except Exception as e:
            logger.warning("Bandeja: no pude mandar el aviso push: %s", e)
    return mensaje


def recoger(destino: Optional[str] = None) -> List[Dict[str, Any]]:
    """Devuelve los mensajes de ese canal y los BORRA de la bandeja.

    Sin `destino` se lleva todos (útil para depurar o para un canal único).
    """
    with _lock:
        mensajes = _vigentes(_leer())
        if destino:
            mios  = [m for m in mensajes if m.get("destino") == destino]
            otros = [m for m in mensajes if m.get("destino") != destino]
        else:
            mios, otros = mensajes, []
        if mios:
            _escribir(otros)
    return mios


def asomarse(destino: Optional[str] = None) -> List[Dict[str, Any]]:
    """Como `recoger`, pero sin vaciar la bandeja."""
    with _lock:
        mensajes = _vigentes(_leer())
    if destino:
        return [m for m in mensajes if m.get("destino") == destino]
    return mensajes


def vaciar() -> int:
    """Tira todo lo pendiente. Devuelve cuántos mensajes había."""
    with _lock:
        n = len(_leer())
        _escribir([])
    return n
