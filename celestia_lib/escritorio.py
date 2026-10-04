"""La Celestia de PC despierta como la del móvil: con la ventana cerrada y al encender.

En el móvil estaba siempre encendida (el guardián la revivía) y por eso podía
avisar de un recordatorio a cualquier hora. En un PC, cerrar la ventana la
apagaba, y el aviso de las nueve no llegaba si a las nueve no había ventana.
Dos ajustes, los dos a elección de la persona (Ajustes → Celestia):

- `segundo_plano`: cerrar la ventana no la apaga (lanzador.py lo lee).
- `al_iniciar`: se enciende sola, sin ventana, al entrar en el ordenador
  (Windows: «Ejecutar» del registro del usuario; Mac: un LaunchAgent; Linux:
  ~/.config/autostart). Volver a abrir Celestia sólo abre la ventana.

Apagarla del todo: Ajustes → «Apagar Celestia» (POST /apagar).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict

from .paths import ES_ANDROID, MEM_DIR, ROOT

logger = logging.getLogger("celestia_v1")

AJUSTES = MEM_DIR / "escritorio.json"
_DEFECTO = {"segundo_plano": False, "al_iniciar": False}
NOMBRE = "Celestia"
ETIQUETA_MAC = "io.github.momentomato709.celestia"


def es_escritorio() -> bool:
    """¿Es una Celestia instalada en un PC (y no el móvil ni una copia de git)?"""
    if os.environ.get("CELESTIA_ESCRITORIO") == "1":
        return True
    return not ES_ANDROID and (ROOT / "VERSION_INSTALADOR").exists()


def leer() -> Dict[str, bool]:
    try:
        datos = json.loads(AJUSTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    return {k: bool(datos.get(k, v)) for k, v in _DEFECTO.items()}


def guardar(**cambios: bool) -> Dict[str, bool]:
    ajustes = leer()
    for k, v in cambios.items():
        if k in _DEFECTO:
            ajustes[k] = bool(v)
    if "al_iniciar" in cambios:
        ajustes["al_iniciar"] = inicio_con_el_sistema(ajustes["al_iniciar"])
        # Encenderse al iniciar sin quedarse despierta no tendría sentido.
        if ajustes["al_iniciar"]:
            ajustes["segundo_plano"] = True
    AJUSTES.parent.mkdir(parents=True, exist_ok=True)
    AJUSTES.write_text(json.dumps(ajustes), encoding="utf-8")
    return ajustes


def _python_sin_consola() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" \
            and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def comando() -> list:
    return [_python_sin_consola(), str(ROOT / "lanzador.py"), "--segundo-plano"]


def _plist_mac() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{ETIQUETA_MAC}.plist"


def _desktop_linux() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "celestia.desktop"


def inicio_con_el_sistema(activo: bool) -> bool:
    """Apunta (o borra) a Celestia en el arranque del sistema. Devuelve cómo
    queda de verdad: False si no se pudo."""
    if os.environ.get("CELESTIA_EN_TESTS"):
        return activo
    try:
        if sys.platform == "win32":
            import winreg
            clave = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                   r"Software\Microsoft\Windows\CurrentVersion\Run",
                                   0, winreg.KEY_SET_VALUE)
            with clave:
                if activo:
                    valor = " ".join(p if p.startswith("--") else f'"{p}"' for p in comando())
                    winreg.SetValueEx(clave, NOMBRE, 0, winreg.REG_SZ, valor)
                else:
                    try:
                        winreg.DeleteValue(clave, NOMBRE)
                    except FileNotFoundError:
                        pass
        elif sys.platform == "darwin":
            plist = _plist_mac()
            if activo:
                import plistlib
                plist.parent.mkdir(parents=True, exist_ok=True)
                with open(plist, "wb") as f:
                    plistlib.dump({"Label": ETIQUETA_MAC, "ProgramArguments": comando(),
                                   "WorkingDirectory": str(ROOT), "RunAtLoad": True}, f)
            else:
                plist.unlink(missing_ok=True)
        else:
            entrada = _desktop_linux()
            if activo:
                entrada.parent.mkdir(parents=True, exist_ok=True)
                exec_ = " ".join(f'"{p}"' for p in comando())
                entrada.write_text(
                    "[Desktop Entry]\nType=Application\nName=Celestia\n"
                    f"Comment=Tu IA personal, despierta para avisarte\nExec={exec_}\n"
                    "Terminal=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
            else:
                entrada.unlink(missing_ok=True)
        return activo
    except Exception as e:
        logger.warning("No pude %s el arranque con el sistema: %s",
                       "poner" if activo else "quitar", e)
        return False
