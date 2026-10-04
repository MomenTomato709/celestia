"""Zona horaria del usuario — central para todo el proyecto.

Sesión 30: el server Termux a veces corre en UTC mientras el usuario está en
Europe/Madrid. Bug AU del 28-may: "Recuerdame en 2 minutos" mostraba "22:46"
en vez de "00:46" porque datetime.fromtimestamp() usaba TZ del sistema (UTC).

Solución: usar siempre `TZ_USUARIO` para formatear hora al usuario y para
interpretar referencias horarias suyas ("a las 18:00").

Para cambiar la zona: export CELESTIA_TZ=America/Argentina/Buenos_Aires
(o lo que sea) antes de arrancar Celestia.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _zona_nombre = os.environ.get("CELESTIA_TZ", "Europe/Madrid")
    try:
        TZ_USUARIO: Optional[ZoneInfo] = ZoneInfo(_zona_nombre)
    except Exception:
        TZ_USUARIO = ZoneInfo("Europe/Madrid")
except ImportError:
    TZ_USUARIO = None  # type: ignore[assignment]


def ahora_usuario() -> datetime:
    """Devuelve datetime aware en la TZ del usuario.

    Si zoneinfo no está disponible, devuelve naive (datetime.now()).
    """
    if TZ_USUARIO is None:
        return datetime.now()
    return datetime.now(TZ_USUARIO)


def from_ts_usuario(ts: float) -> datetime:
    """Convierte un timestamp UNIX a datetime aware en TZ del usuario.

    Equivalente a `datetime.fromtimestamp(ts, tz=TZ_USUARIO)` pero
    tolerante a la falta de zoneinfo.
    """
    if TZ_USUARIO is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, tz=TZ_USUARIO)
