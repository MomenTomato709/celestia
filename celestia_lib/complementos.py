"""Piezas que Celestia se instala sola la primera vez que las necesita (en un PC).

En el móvil (Termux) cada pieza se instaló a mano en su día y ahí sigue. El
instalador de PC no las lleva todas: oír sin ninguna clave (faster-whisper)
son unos 80 MB más que mucha gente no usaría nunca. Así que se bajan cuando
hacen falta, con el pip del propio Python de la instalación, y se quedan: las
actualizaciones pisan ficheros, no borran la carpeta de Python.

En la app de Android no se puede (Chaquopy no trae pip): ahí `se_puede()` es
falso y quien llama tiene que ofrecer otro camino (una clave gratis).
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .paths import ES_ANDROID

logger = logging.getLogger("celestia_v1")

# nombre → {"estado": "instalando" | "listo" | "fallo", "desde": t, "error": str}
_ESTADO: Dict[str, Dict] = {}
_LOCK = threading.Lock()
# Un intento fallido no se repite en cada nota de voz: se espera un rato.
_REINTENTO_SEG = 15 * 60


def esta(modulo: str) -> bool:
    """¿Se puede importar ya? (sin importarlo: faster-whisper tarda)."""
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(modulo) is not None
    except (ImportError, ValueError):
        return False


def _python() -> str:
    """El intérprete de esta Celestia, con consola (pythonw no tiene salida)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def se_puede() -> bool:
    """¿Puede esta Celestia instalarse piezas ella sola?"""
    if os.environ.get("CELESTIA_EN_TESTS") or os.environ.get("CELESTIA_SIN_COMPLEMENTOS"):
        return False
    if ES_ANDROID:
        return False
    return esta("pip")


def estado(nombre: str) -> str:
    with _LOCK:
        return (_ESTADO.get(nombre) or {}).get("estado", "")


def _pip(paquetes: Sequence[str], modulo: str, timeout: float) -> tuple:
    """(ok, error) de instalar `paquetes` con el pip de esta Celestia."""
    extra = {}
    if sys.platform == "win32":
        extra["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            [_python(), "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", "--only-binary=:all:", *paquetes],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, **extra)
        ok = r.returncode == 0 and esta(modulo)
        return ok, "" if ok else (r.stderr or r.stdout or "")[-600:]
    except Exception as e:                              # sin red, timeout…
        return False, str(e)


def instalar_ya(nombre: str, paquetes: Sequence[str], modulo: str,
                timeout: float = 300) -> bool:
    """Como `instalar`, pero esperando a que termine (piezas pequeñas: la
    librería de Telegram o de Discord tardan unos segundos)."""
    if esta(modulo):
        return True
    if not se_puede():
        return False
    logger.info("Instalando %s (%s)…", nombre, " ".join(paquetes))
    ok, error = _pip(paquetes, modulo, timeout)
    with _LOCK:
        _ESTADO[nombre] = {"estado": "listo" if ok else "fallo",
                           "desde": time.time(), "error": error}
    if not ok:
        logger.warning("No pude instalar %s: %s", nombre, error.strip()[-300:])
    return ok


def instalar(nombre: str, paquetes: Sequence[str], modulo: str,
             al_terminar: Optional[Callable[[bool], None]] = None) -> str:
    """Empieza a instalar `paquetes` en segundo plano.

    Devuelve "listo" si `modulo` ya se puede importar, "instalando" si está en
    marcha (ahora o de antes), "fallo" si falló hace poco y "no_se_puede" si
    en este aparato no hay cómo.
    """
    if esta(modulo):
        return "listo"
    if not se_puede():
        return "no_se_puede"
    with _LOCK:
        previo = _ESTADO.get(nombre) or {}
        if previo.get("estado") == "instalando":
            return "instalando"
        if previo.get("estado") == "fallo" and time.time() - previo.get("desde", 0) < _REINTENTO_SEG:
            return "fallo"
        _ESTADO[nombre] = {"estado": "instalando", "desde": time.time(), "error": ""}

    def _trabajo() -> None:
        logger.info("Instalando %s (%s)…", nombre, " ".join(paquetes))
        inicio = time.time()
        ok, error = _pip(paquetes, modulo, 1200)
        with _LOCK:
            _ESTADO[nombre] = {"estado": "listo" if ok else "fallo",
                               "desde": time.time(), "error": error}
        if ok:
            logger.info("%s instalado en %.0f s", nombre, time.time() - inicio)
        else:
            logger.warning("No pude instalar %s: %s", nombre, error.strip()[-300:])
        if al_terminar:
            try:
                al_terminar(ok)
            except Exception as e:
                logger.debug("Aviso tras instalar %s: %s", nombre, e)

    threading.Thread(target=_trabajo, daemon=True, name=f"instalar-{nombre}").start()
    return "instalando"
