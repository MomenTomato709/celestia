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

from .paths import ES_ANDROID, ROOT

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


# ── Node.js (el puente de WhatsApp) ────────────────────────────────────────
# En el móvil se instala con `pkg install nodejs`. En un PC nadie tiene por qué
# tenerlo: Celestia se baja el oficial de nodejs.org (unos 30 MB), comprueba
# su huella SHA-256 con la lista que publica nodejs.org y lo deja en su
# carpeta. Si el sistema ya tiene Node, se usa ése.
NODE_DIR = ROOT / "complementos" / "node"
NODE_MAYOR = 22                     # LTS; Baileys pide 20 o más


def _plataforma_node() -> Optional[tuple]:
    """(sistema, arquitectura, extensión) como los nombra nodejs.org."""
    import platform
    maquina = platform.machine().lower()
    arq = "arm64" if maquina in ("arm64", "aarch64") else ("x64" if maquina in
                                                            ("amd64", "x86_64") else "")
    if not arq:
        return None
    if sys.platform == "win32":
        return "win", arq, "zip"
    if sys.platform == "darwin":
        return "darwin", arq, "tar.gz"
    if sys.platform.startswith("linux"):
        return "linux", arq, "tar.xz"
    return None


def _carpeta_node() -> Optional[Path]:
    try:
        nombre = (NODE_DIR / "actual.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    carpeta = NODE_DIR / nombre
    return carpeta if carpeta.is_dir() else None


def ruta_node() -> Optional[str]:
    """El `node` del sistema o el que se bajó Celestia. None si no hay."""
    import shutil
    propio = _carpeta_node()
    if propio:
        exe = propio / ("node.exe" if sys.platform == "win32" else "bin/node")
        if exe.exists():
            return str(exe)
    return shutil.which("node")


def ruta_npm() -> Optional[str]:
    import shutil
    propio = _carpeta_node()
    if propio:
        npm = propio / ("npm.cmd" if sys.platform == "win32" else "bin/npm")
        if npm.exists():
            return str(npm)
    return shutil.which("npm")


def entorno_node(base: Optional[dict] = None) -> dict:
    """El entorno con la carpeta de Node delante en el PATH (npm lanza `node`)."""
    env = dict(base if base is not None else os.environ)
    node = ruta_node()
    if node:
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    return env


def _bajar_node() -> tuple:
    """(ok, error). Baja, comprueba y descomprime el Node LTS de este sistema."""
    import hashlib
    import json
    import shutil
    import tarfile
    import urllib.request
    import zipfile
    plat = _plataforma_node()
    if not plat:
        return False, "no hay Node oficial para este sistema"
    sistema, arq, ext = plat
    cabecera = {"User-Agent": "Celestia"}
    with urllib.request.urlopen(urllib.request.Request(
            "https://nodejs.org/dist/index.json", headers=cabecera), timeout=30) as r:
        versiones = json.load(r)
    version = next(v["version"] for v in versiones
                   if v.get("lts") and v["version"].startswith(f"v{NODE_MAYOR}."))
    nombre = f"node-{version}-{sistema}-{arq}"
    archivo = f"{nombre}.{ext}"
    base = f"https://nodejs.org/dist/{version}/"
    with urllib.request.urlopen(urllib.request.Request(
            base + "SHASUMS256.txt", headers=cabecera), timeout=30) as r:
        huellas = dict(reversed(l.split()) for l in r.read().decode().splitlines() if l.strip())
    esperada = huellas.get(archivo)
    if not esperada:
        return False, f"nodejs.org no publica {archivo}"
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    destino = NODE_DIR / archivo
    huella = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(base + archivo, headers=cabecera),
                                timeout=60) as r, open(destino, "wb") as f:
        while bloque := r.read(1 << 20):
            huella.update(bloque)
            f.write(bloque)
    if huella.hexdigest() != esperada:
        destino.unlink(missing_ok=True)
        return False, "la descarga de Node no coincide con su huella oficial"
    shutil.rmtree(NODE_DIR / nombre, ignore_errors=True)
    if ext == "zip":
        with zipfile.ZipFile(destino) as z:
            z.extractall(NODE_DIR)
    else:
        with tarfile.open(destino) as t:
            t.extractall(NODE_DIR, filter="data")
    destino.unlink(missing_ok=True)
    (NODE_DIR / "actual.txt").write_text(nombre, encoding="utf-8")
    return True, ""


def instalar_node(al_terminar: Optional[Callable[[bool], None]] = None) -> str:
    """Como `instalar`, para Node. "listo", "instalando", "fallo" o "no_se_puede"."""
    if ruta_node():
        return "listo"
    if not se_puede():
        return "no_se_puede"
    with _LOCK:
        previo = _ESTADO.get("node") or {}
        if previo.get("estado") == "instalando":
            return "instalando"
        if previo.get("estado") == "fallo" and time.time() - previo.get("desde", 0) < _REINTENTO_SEG:
            return "fallo"
        _ESTADO["node"] = {"estado": "instalando", "desde": time.time(), "error": ""}

    def _trabajo() -> None:
        logger.info("Bajando Node.js…")
        try:
            ok, error = _bajar_node()
        except Exception as e:                          # sin red, nodejs.org caído…
            ok, error = False, str(e)
        with _LOCK:
            _ESTADO["node"] = {"estado": "listo" if ok else "fallo",
                               "desde": time.time(), "error": error}
        if ok:
            logger.info("Node.js listo: %s", ruta_node())
        else:
            logger.warning("No pude bajar Node.js: %s", error)
        if al_terminar:
            try:
                al_terminar(ok)
            except Exception as e:
                logger.debug("Aviso tras bajar Node: %s", e)

    threading.Thread(target=_trabajo, daemon=True, name="instalar-node").start()
    return "instalando"
