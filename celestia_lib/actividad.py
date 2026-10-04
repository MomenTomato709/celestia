#!/usr/bin/env python3
"""Qué está haciendo Celestia ahora mismo, para que el chat pueda enseñarlo.

Un spinner que pone «pensando…» durante veinte segundos miente: casi todo ese
rato Celestia está buscando en internet, leyendo una página o regenerando la
respuesta porque se ha pillado a sí misma citando un dato viejo. Aquí se apunta
la fase real —la marca el propio orquestador según pasa por ella— y el chat la
lee por `GET /actividad`, así que el logo cambia de estado porque el estado ha
cambiado de verdad, no por un temporizador.

Es a propósito un tablón en memoria y no una tabla: son datos de un segundo de
vida, y si el proceso se reinicia lo que estuviera en curso ya no existe.

Uso:
    from . import actividad
    with actividad.fase("buscando", "el tiempo en Madrid"):
        ...
    actividad.marcar("escribiendo")
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
from typing import Dict, List, Optional

# Las fases que se pueden anunciar. Coinciden con los estados del logo en
# `marca.ESTADOS`: si se añade una aquí, hay que darle su color y su velocidad
# allí o el chat la pintará como «reposo».
FASES = ("reposo", "escuchando", "pensando", "recordando", "buscando",
         "leyendo", "actuando", "escribiendo", "error")

# Cuánto se guarda del recorrido de un turno. Es para que el chat pueda enseñar
# «ha buscado y luego ha leído dos páginas», no un histórico.
MAX_HITOS = 12

# Si nadie toca el estado en este tiempo, se considera que el turno murió (una
# excepción que se saltó el finally, el proceso ocupado en otra cosa) y se
# vuelve a reposo solo. Sin esto, un fallo raro dejaba el chat girando para
# siempre.
CADUCIDAD_S = 180.0

def _limpiar(detalle: str) -> str:
    """Deja el detalle en una línea legible.

    Lo que llega puede traer el anexo de una herramienta pegado detrás
    («…Madrid?\n\n[RESULTADO]\nTiempo en Madri»). En el chat es una etiqueta
    de una línea: los saltos partirían el spinner del terminal y dejarían
    restos colgando al repintarse.
    """
    primera = (detalle or "").strip().split("\n", 1)[0]
    return re.sub(r"\s{2,}", " ", primera).strip()[:120]


_lock = threading.Lock()
_estado: Dict[str, object] = {
    "fase": "reposo",
    "detalle": "",
    "desde": time.time(),
    "turno": 0,
}
_hitos: List[Dict[str, object]] = []


def marcar(fase: str, detalle: str = "") -> None:
    """Anuncia en qué anda. `detalle` es texto corto para enseñar tal cual."""
    if fase not in FASES:
        fase = "pensando"
    detalle = _limpiar(detalle)
    ahora = time.time()
    with _lock:
        anterior = str(_estado["fase"])
        if anterior == fase and str(_estado["detalle"]) == detalle:
            return                       # nada nuevo: no ensuciar los hitos
        _estado["fase"] = fase
        _estado["detalle"] = detalle
        _estado["desde"] = ahora
        if fase != "reposo":
            _hitos.append({"fase": fase, "detalle": detalle, "t": ahora})
            del _hitos[:-MAX_HITOS]


def empezar_turno(detalle: str = "") -> None:
    """Un mensaje nuevo: se limpia el recorrido del anterior."""
    detalle = _limpiar(detalle)
    ahora = time.time()
    with _lock:
        _estado["turno"] = int(_estado["turno"]) + 1
        _estado["fase"] = "pensando"
        _estado["detalle"] = detalle
        _estado["desde"] = ahora
        _hitos.clear()
        _hitos.append({"fase": "pensando", "detalle": detalle, "t": ahora})


def reposo() -> None:
    """Ha terminado. El logo vuelve a su respiración lenta."""
    with _lock:
        _estado["fase"] = "reposo"
        _estado["detalle"] = ""
        _estado["desde"] = time.time()


def instantanea() -> Dict[str, object]:
    """Lo que hay ahora, listo para `jsonify`."""
    ahora = time.time()
    with _lock:
        fase = str(_estado["fase"])
        desde = float(_estado["desde"])
        # Red de seguridad: una fase eterna es una fase perdida.
        if fase != "reposo" and ahora - desde > CADUCIDAD_S:
            fase = "reposo"
            _estado["fase"] = "reposo"
            _estado["detalle"] = ""
            _estado["desde"] = ahora
            desde = ahora
        return {
            "fase": fase,
            "detalle": str(_estado["detalle"]),
            "segundos": round(ahora - desde, 2),
            "turno": int(_estado["turno"]),
            "hitos": [dict(h) for h in _hitos],
        }


@contextlib.contextmanager
def fase(nombre: str, detalle: str = "", volver_a: Optional[str] = None):
    """Marca una fase mientras dure el bloque y luego vuelve a la anterior.

    Se restaura la fase de antes (no reposo) porque estas cosas se anidan: al
    salir de «leyendo» seguimos «buscando», y dejar el logo en calma a mitad de
    turno se ve como si hubiera terminado.
    """
    with _lock:
        previa = str(_estado["fase"])
        previo_detalle = str(_estado["detalle"])
    marcar(nombre, detalle)
    try:
        yield
    finally:
        destino = volver_a or previa
        marcar(destino, "" if destino != previa else previo_detalle)
