"""Las «manos» de Celestia en un ordenador: abrir y cerrar programas, el volumen.

En el móvil, «abre Spotify» lo hacía por Shizuku. En un PC contestaba «eso sólo
funciona en un móvil Android» (tools.py, _SOLO_ANDROID), y era verdad: no había
nada. Esto es lo mismo para Windows, Mac y Linux, sin librerías nuevas:

- Abrir: por el nombre con el que el programa aparece en el sistema (los
  accesos del menú Inicio en Windows, /Applications en el Mac, los .desktop en
  Linux), tolerando mayúsculas, tildes y nombres a medias («abre el word»).
- Cerrar: pidiéndoselo al programa como lo haría la persona (en Windows,
  `taskkill` SIN /F; en el Mac, «quit»): si hay algo sin guardar, el programa
  pregunta. Nunca se mata a la fuerza.
- Volumen: las teclas multimedia en Windows, osascript en el Mac, pactl/amixer
  en Linux.

Determinista a propósito: lo que se abre lo decide el nombre, no un modelo.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("celestia_v1")

_SIN_VENTANA = {"creationflags": 0x08000000} if os.name == "nt" else {}

# Los de siempre en Windows, que no salen como acceso en el menú Inicio.
_WINDOWS_PROPIOS = {
    "bloc de notas": "notepad", "notepad": "notepad", "notas": "notepad",
    "calculadora": "calc", "paint": "mspaint",
    "explorador": "explorer", "explorador de archivos": "explorer", "archivos": "explorer",
    "configuracion": "ms-settings:", "ajustes": "ms-settings:",
    "administrador de tareas": "taskmgr", "terminal": "wt", "simbolo del sistema": "cmd",
    "recortes": "snippingtool", "recortes de pantalla": "snippingtool",
}
_ALIAS = {"navegador": ["chrome", "edge", "firefox", "brave", "opera"],
          "word": ["microsoft word", "word"], "excel": ["microsoft excel", "excel"],
          "powerpoint": ["microsoft powerpoint", "powerpoint"]}


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(el|la|los|las|programa|app|aplicacion|de|del)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ── Qué hay instalado ───────────────────────────────────────────────────────
_TIENDA: Dict[str, object] = {"ts": 0.0, "apps": {}}


def _apps_de_la_tienda() -> Dict[str, str]:
    """nombre normalizado → AppID de las apps de Windows (Get-StartApps).
    Tarda un par de segundos: se recuerda diez minutos."""
    import json
    import time
    if time.time() - float(_TIENDA["ts"]) < 600:
        return dict(_TIENDA["apps"])                       # type: ignore[arg-type]
    apps: Dict[str, str] = {}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             # Sin esto la salida va en la página de códigos de la consola y
             # las tildes llegaban rotas («administraci n de equipos»).
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, **_SIN_VENTANA)
        datos = json.loads(r.stdout or "[]")
        for app in datos if isinstance(datos, list) else [datos]:
            nombre = _norm(str(app.get("Name") or ""))
            if nombre and app.get("AppID"):
                apps.setdefault(nombre, str(app["AppID"]))
    except Exception as e:
        logger.info("Sin la lista de apps de Windows: %s", e)
    _TIENDA.update(ts=time.time(), apps=apps)
    return apps


def _instalados() -> Dict[str, str]:
    """nombre normalizado → cómo abrirlo (ruta de acceso, .app, id de .desktop)."""
    encontrados: Dict[str, str] = {}
    if sys.platform == "win32":
        bases = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                 Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
        for base in bases:
            if base.is_dir():
                for lnk in base.rglob("*.lnk"):
                    nombre = _norm(lnk.stem)
                    if nombre and "desinstal" not in nombre and "uninstall" not in nombre:
                        encontrados.setdefault(nombre, str(lnk))
        # Las de la Microsoft Store (Spotify, WhatsApp, la Calculadora…) no
        # tienen acceso .lnk: se abren por su AppID (shell:AppsFolder).
        for nombre, app_id in _apps_de_la_tienda().items():
            encontrados.setdefault(nombre, f"shell:AppsFolder\\{app_id}")
    elif sys.platform == "darwin":
        for base in (Path("/Applications"), Path("/System/Applications"),
                     Path.home() / "Applications"):
            if base.is_dir():
                for app in list(base.glob("*.app")) + list(base.glob("*/*.app")):
                    encontrados.setdefault(_norm(app.stem), str(app))
    else:
        bases = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
                 Path.home() / ".local/share/applications",
                 Path("/var/lib/flatpak/exports/share/applications")]
        for base in bases:
            if not base.is_dir():
                continue
            for desktop in base.glob("*.desktop"):
                try:
                    texto = desktop.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if re.search(r"^NoDisplay=true", texto, re.M):
                    continue
                m = re.search(r"^Name=(.+)$", texto, re.M)
                if m:
                    encontrados.setdefault(_norm(m.group(1)), str(desktop))
    return encontrados


def buscar(nombre: str) -> Optional[Tuple[str, str]]:
    """(nombre bonito, cómo abrirlo) del programa que mejor encaja, o None."""
    pedido = _norm(nombre)
    if not pedido:
        return None
    propios = {_norm(k): v for k, v in _WINDOWS_PROPIOS.items()}
    if sys.platform == "win32" and pedido in propios:
        return nombre.strip(), propios[pedido]
    instalados = _instalados()
    candidatos = _ALIAS.get(pedido, [pedido])
    for c in candidatos:                                 # exacto
        if c in instalados:
            return c, instalados[c]
    for c in candidatos:                                 # empieza o contiene
        for nombre_inst in sorted(instalados, key=len):
            if nombre_inst.startswith(c) or f" {c}" in f" {nombre_inst}":
                return nombre_inst, instalados[nombre_inst]
    parecidos = difflib.get_close_matches(pedido, list(instalados), n=1, cutoff=0.8)
    if parecidos:
        return parecidos[0], instalados[parecidos[0]]
    return None


def abrir(nombre: str) -> str:
    hallado = buscar(nombre)
    if not hallado:
        return (f"No encuentro «{nombre}» entre los programas de este ordenador. "
                "Dime el nombre tal como sale en el menú de programas.")
    bonito, como = hallado
    try:
        if sys.platform == "win32":
            os.startfile(como)                                     # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", como], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            lanzador = shutil.which("gtk-launch") or shutil.which("gio")
            id_ = Path(como).name
            if lanzador and lanzador.endswith("gtk-launch"):
                orden = [lanzador, id_]
            elif lanzador:
                orden = [lanzador, "launch", como]
            else:
                texto = Path(como).read_text(encoding="utf-8", errors="ignore")
                exec_ = re.search(r"^Exec=(.+)$", texto, re.M)
                orden = re.sub(r"%[a-zA-Z]", "", exec_.group(1)).split() if exec_ else []
            if not orden:
                return f"No sé cómo abrir «{bonito}» en este sistema."
            subprocess.Popen(orden, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        logger.warning("No pude abrir %s: %s", como, e)
        return f"No he podido abrir «{bonito}»: {e}"
    return f"✅ Abriendo {bonito}."


# Cómo se llama el proceso de los de Windows que no coinciden con su nombre.
_PROCESO_PROPIO = {"calc": ["CalculatorApp", "calc"], "notepad": ["Notepad"],
                   "mspaint": ["mspaint"], "taskmgr": ["Taskmgr"], "explorer": [],
                   "snippingtool": ["SnippingTool"]}


def _destino_lnk(ruta: str) -> str:
    """El ejecutable al que apunta un acceso directo de Windows."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             f"(New-Object -ComObject WScript.Shell).CreateShortcut('{ruta}').TargetPath"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, **_SIN_VENTANA)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _nombres_de_proceso(nombre: str) -> List[str]:
    """Con qué nombres de ejecutable puede estar en marcha lo que se pidió."""
    nombres = [nombre]
    hallado = buscar(nombre) if sys.platform == "win32" else None
    if hallado:
        como = hallado[1]
        if como.lower().endswith(".lnk"):
            destino = _destino_lnk(como)
            if destino:
                nombres.append(Path(destino).stem)
        elif como.startswith("shell:AppsFolder\\"):
            app_id = como.split("\\", 1)[1]
            nombres += [app_id.split("!")[-1], app_id.split("_")[0].split(".")[-1]]
        else:
            nombres += _PROCESO_PROPIO.get(como, [como])
    return [n for n in nombres if n]


def _procesos_de(nombre: str) -> List[str]:
    """Nombres de ejecutable en marcha que encajan con el programa pedido."""
    try:
        import psutil
    except ImportError:
        return []
    pedidos = [_norm(n).replace(" ", "") for n in _nombres_de_proceso(nombre)]
    exactos, parecidos = set(), set()
    for p in psutil.process_iter(["name"]):
        exe = (p.info.get("name") or "")
        base = _norm(Path(exe).stem).replace(" ", "")
        if not base:
            continue
        if base in pedidos:
            exactos.add(exe)
        elif any(len(pe) >= 4 and pe in base for pe in pedidos):
            parecidos.add(exe)
    # «Discord» es Discord.exe, no también DiscordSystemHelper.exe.
    return sorted(exactos or parecidos)


def cerrar(nombre: str) -> str:
    if sys.platform == "darwin":
        hallado = buscar(nombre)
        app = Path(hallado[1]).stem if hallado else nombre
        r = subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'],
                           capture_output=True, text=True, timeout=20)
        return f"✅ Le he pedido a {app} que se cierre." if r.returncode == 0 else \
            f"No he podido cerrar «{nombre}»: {r.stderr.strip()[:120]}"
    exes = _procesos_de(nombre)
    if not exes:
        return f"No veo «{nombre}» abierto ahora mismo."
    for exe in exes:
        if sys.platform == "win32":
            # Sin /F: el programa recibe «ciérrate» y, si hay algo sin guardar, pregunta.
            subprocess.run(["taskkill", "/IM", exe], capture_output=True, timeout=20, **_SIN_VENTANA)
        else:
            subprocess.run(["pkill", "-TERM", "-x", exe], capture_output=True, timeout=20)
    # Comprobar, no suponer: las apps de la Tienda de Windows (la Calculadora,
    # Spotify…) no hacen caso al «ciérrate» normal (medido el 4 oct 2026).
    import time
    for _ in range(6):
        time.sleep(0.5)
        if not _procesos_de(nombre):
            return f"✅ He cerrado {nombre}."
    return (f"Le he pedido a {nombre} que se cierre, pero sigue abierto: o te está "
            "preguntando si guardas algo, o es de las que no hacen caso a esa petición "
            "(las de la Tienda de Windows). Ciérralo desde su ventana; a la fuerza no "
            "lo cierro, por si tienes algo sin guardar.")


def volumen(accion: str) -> str:
    a = (accion or "").lower().strip()
    sube = a in ("up", "subir", "sube", "más", "mas")
    baja = a in ("down", "bajar", "baja", "menos")
    mute = a in ("mute", "silenciar", "silencio", "silenciado")
    nivel = re.match(r"(\d{1,3})%?$", a)
    try:
        if sys.platform == "win32":
            import ctypes
            if sube:
                codigo, veces = 0xAF, 5                  # VK_VOLUME_UP (≈2 % cada una)
            elif baja:
                codigo, veces = 0xAE, 5                  # VK_VOLUME_DOWN
            elif mute:
                codigo, veces = 0xAD, 1                  # VK_VOLUME_MUTE
            elif nivel:
                # Sin librerías no se puede fijar un % exacto: a cero y subir.
                for _ in range(50):
                    ctypes.windll.user32.keybd_event(0xAE, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xAE, 0, 2, 0)
                codigo, veces = 0xAF, max(0, min(100, int(nivel.group(1)))) // 2
            else:
                return f"No entiendo «{accion}» para el volumen."
            for _ in range(veces):
                ctypes.windll.user32.keybd_event(codigo, 0, 0, 0)
                ctypes.windll.user32.keybd_event(codigo, 0, 2, 0)
        elif sys.platform == "darwin":
            if mute:
                guion = "set volume with output muted"
            elif nivel:
                guion = f"set volume output volume {max(0, min(100, int(nivel.group(1))))}"
            elif sube or baja:
                signo = "+" if sube else "-"
                guion = (f"set volume output volume ((output volume of (get volume settings))"
                         f" {signo} 10)")
            else:
                return f"No entiendo «{accion}» para el volumen."
            subprocess.run(["osascript", "-e", guion], capture_output=True, timeout=10)
        else:
            pactl = shutil.which("pactl")
            if pactl:
                arg = ("toggle" if mute else f"{int(nivel.group(1))}%" if nivel
                       else "+10%" if sube else "-10%" if baja else None)
                if arg is None:
                    return f"No entiendo «{accion}» para el volumen."
                orden = ([pactl, "set-sink-mute", "@DEFAULT_SINK@", arg] if mute else
                         [pactl, "set-sink-volume", "@DEFAULT_SINK@", arg])
                subprocess.run(orden, capture_output=True, timeout=10)
            elif shutil.which("amixer"):
                arg = ("toggle" if mute else f"{int(nivel.group(1))}%" if nivel
                       else "10%+" if sube else "10%-" if baja else None)
                if arg is None:
                    return f"No entiendo «{accion}» para el volumen."
                subprocess.run(["amixer", "-q", "set", "Master", arg], capture_output=True, timeout=10)
            else:
                return "No encuentro cómo cambiar el volumen en este Linux (ni pactl ni amixer)."
    except Exception as e:
        return f"No he podido cambiar el volumen: {e}"
    if mute:
        return "🔇 Silencio (o vuelvo a poner el sonido, si ya estaba en silencio)."
    if nivel:
        return f"🔊 Volumen al {max(0, min(100, int(nivel.group(1))))} %."
    return "🔊 Subo el volumen." if sube else "🔉 Bajo el volumen."


def captura() -> Optional[bytes]:
    """La pantalla entera en PNG (Windows y Mac; Linux con X). None si no se puede."""
    try:
        import io
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=sys.platform == "win32")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        logger.info("Sin captura con PIL: %s", e)
        return None
