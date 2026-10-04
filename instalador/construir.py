#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fabrica el instalador de Celestia para un sistema, desde cualquier sistema.

    python3 instalador/construir.py windows-x64     # → dist/Celestia-Instalador-Windows.exe
    python3 instalador/construir.py linux-x64       # → dist/Celestia-Linux-x64.tar.gz
    python3 instalador/construir.py macos-arm64     # → dist/Celestia-Mac-arm64.zip

Cómo: un Python portátil del sistema de destino (python-build-standalone, el
mismo que usa `uv`), las librerías de `dependencias/escritorio.txt` bajadas ya
compiladas para ese sistema (`pip --platform`), el código tal como está en el
último commit (`git archive HEAD`, así nunca se cuela algo a medias) y el
lanzador. Nada se compila: por eso se puede fabricar el de Windows desde el
móvil.

Python de verdad y no un ejecutable congelado (PyInstaller): Celestia aprende
habilidades escribiendo programas en Python y ejecutándolos aparte, y para eso
necesita un intérprete.

Lo que el instalador NUNCA lleva: `.env`, `memoria/`, `logs/` (las claves y
las conversaciones de quien lo fabrica). `git archive` ya los deja fuera por
`.gitignore`, y aquí se comprueba otra vez antes de empaquetar.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIST = RAIZ / "dist"                 # sólo los instaladores terminados
# Se trabaja fuera del repositorio: en el móvil, /root/Celestia es almacenamiento
# compartido de Android, que no guarda ni permisos de ejecución ni enlaces
# simbólicos, y el Python del paquete de Linux salía sin poder ejecutarse.
TRABAJO = Path.home() / ".cache" / "celestia-construir"
CACHE = TRABAJO / "descargas"
PY_VERSION = "3.12"

# destino → (triple de python-build-standalone, plataformas de pip)
DESTINOS = {
    "windows-x64": ("x86_64-pc-windows-msvc", ["win_amd64"]),
    "linux-x64":   ("x86_64-unknown-linux-gnu", ["manylinux_2_28_x86_64", "manylinux2014_x86_64",
                                                 "manylinux_2_17_x86_64"]),
    "linux-arm64": ("aarch64-unknown-linux-gnu", ["manylinux_2_28_aarch64", "manylinux2014_aarch64",
                                                  "manylinux_2_17_aarch64"]),
    "macos-arm64": ("aarch64-apple-darwin", ["macosx_11_0_arm64", "macosx_12_0_arm64",
                                             "macosx_13_0_arm64", "macosx_14_0_arm64"]),
    "macos-x64":   ("x86_64-apple-darwin", ["macosx_10_13_x86_64", "macosx_10_15_x86_64",
                                            "macosx_11_0_x86_64", "macosx_12_0_x86_64"]),
}

# Python puro que sólo se publica como código fuente: `pip --platform` no
# acepta código fuente, así que se convierte en rueda aquí (sirve para
# cualquier sistema) y se instala sin dependencias; las suyas, que sí son
# compiladas, van en la lista normal.
SOLO_FUENTE = ("pywebpush", "http-ece")
DEPS_DE_SOLO_FUENTE = ("aiohttp", "py-vapid", "six")

# La ventana propia de Celestia (lanzador.py → pywebview, 3 oct 2026). Va con
# `--no-deps` y la lista entera escrita aquí, por dos motivos: pip resuelve los
# marcadores `sys_platform == "win32"` con el sistema que FABRICA (un Ubuntu en
# GitHub), así que no pediría pythonnet ni pyobjc; y `proxy_tools` solo existe
# como código fuente, y con `--only-binary` la instalación entera fallaría.
# En Linux no va: haría falta GTK o Qt del sistema, y sin ellos el lanzador
# abre el chat en el navegador como siempre.
VENTANA = {
    "windows": ("pywebview", "bottle", "typing_extensions", "pythonnet", "clr_loader"),
    "macos": ("pywebview", "bottle", "typing_extensions", "pyobjc-core",
              "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz",
              "pyobjc-framework-WebKit", "pyobjc-framework-Security",
              "pyobjc-framework-UniformTypeIdentifiers"),
}
VENTANA_SOLO_FUENTE = ("proxy_tools",)

# Del repositorio, lo que no le sirve a quien la instala.
FUERA = ("tests", "examen", "roadmap", "site", "typings", "docs", "dist", "instalador", ".github", "android", "contexto_claude",
         "conversacion_completa.txt", "REFACTOR_PLAN.md", "agente_movil",
         "docker-compose.yml", "Dockerfile", "Makefile", "CLAUDE.md", "CONTRIBUTING.md",
         "ARCHITECTURE.md", "ROADMAP.md",
         # Los guardianes y lanzadores de Termux: en un PC manda lanzador.py.
         "arrancar.sh", "guardian.sh", "reanimador.sh", "resucitador.sh", "entrar.sh",
         "local.sh", "celestia.sh", "hablar.sh", "diagnostico_widget.sh",
         "iniciar_whatsapp.sh", "cambiar_keys.sh")
# Nunca, pase lo que pase con el .gitignore.
PROHIBIDOS = (".env", "memoria", "logs", "recibidos", "voces_piper", "complementos")


def _python_portatil(triple: str) -> Path:
    """Descarga (una vez) el Python portátil de ese sistema."""
    CACHE.mkdir(parents=True, exist_ok=True)
    api = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    cabeceras = {"Accept": "application/vnd.github+json"}
    # Sin identificarse, la API de GitHub da 60 consultas por hora y por IP: en
    # GitHub Actions, con cinco fabricaciones a la vez, se acababan (403).
    if os.environ.get("GITHUB_TOKEN"):
        cabeceras["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(api, headers=cabeceras), timeout=30) as r:
        version = json.load(r)
    nombre = next(
        a["name"] for a in version["assets"]
        if a["name"].startswith(f"cpython-{PY_VERSION}.") and triple in a["name"]
        and a["name"].endswith("install_only_stripped.tar.gz"))
    destino = CACHE / nombre
    if not destino.exists():
        url = next(a["browser_download_url"] for a in version["assets"] if a["name"] == nombre)
        print(f"  ↓ {nombre}")
        with urllib.request.urlopen(url, timeout=300) as r, open(destino.with_suffix(".part"), "wb") as f:
            shutil.copyfileobj(r, f)
        destino.with_suffix(".part").rename(destino)
    return destino


def _site_packages(python_dir: Path, destino: str) -> Path:
    if destino.startswith("windows"):
        return python_dir / "Lib" / "site-packages"
    return python_dir / "lib" / f"python{PY_VERSION}" / "site-packages"


def _codigo(carpeta: Path, commit: str = "HEAD") -> None:
    """El último commit (u otro), sin lo que no hace falta."""
    datos = subprocess.run(["git", "archive", "--format=tar", commit], cwd=RAIZ,
                           check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(datos)) as tar:
        miembros = [m for m in tar.getmembers()
                    if m.name.split("/")[0] not in FUERA + PROHIBIDOS]
        tar.extractall(carpeta, members=miembros, filter="data")
    corto = subprocess.run(["git", "rev-parse", "--short", commit], cwd=RAIZ,
                           check=True, capture_output=True, text=True).stdout.strip()
    (carpeta / "VERSION_INSTALADOR").write_text(corto + "\n")


# Sin esto no arranca o sale a medias. El logo faltó en los primeros paquetes:
# `imagenes/` estaba entero en el .gitignore y `git archive` no lo llevaba.
IMPRESCINDIBLES = ("celestia.py", "lanzador.py", "celestia_lib/web/chat.html",
                   "imagenes/celestia_logo.png", "imagenes/celestia_logo_transparente.png")


def _comprobar_completo(carpeta: Path) -> None:
    faltan = [f for f in IMPRESCINDIBLES if not (carpeta / f).is_file()]
    if faltan:
        raise SystemExit(f"✗ al paquete le falta: {', '.join(faltan)} (¿está en git?)")


def _comprobar_limpio(carpeta: Path) -> None:
    for prohibido in PROHIBIDOS:
        if (carpeta / prohibido).exists():
            raise SystemExit(f"✗ {prohibido} se iba a colar en el instalador")
    # Ninguna clave del .env de quien lo fabrica, ni siquiera dentro de un fichero.
    env = RAIZ / ".env"
    claves = []
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            k, _, v = linea.partition("=")
            v = v.strip().strip('"').strip("'")
            if len(v) >= 20 and any(t in k for t in ("KEY", "TOKEN", "SECRET", "LLAVE")):
                claves.append(v.encode())
    for f in carpeta.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".txt", ".json", ".md", ".html", ".js", ".sh", ".env"):
            datos = f.read_bytes()
            for c in claves:
                if c in datos:
                    raise SystemExit(f"✗ una clave del .env aparece en {f.relative_to(carpeta)}")


def _icono(carpeta: Path, formato: str) -> Path | None:
    try:
        from PIL import Image
    except ImportError:
        print("  ! sin Pillow: el instalador irá sin icono propio")
        return None
    origen = RAIZ / "imagenes" / "celestia_logo.png"
    salida = carpeta / f"celestia.{formato}"
    img = Image.open(origen).convert("RGBA")
    if formato == "ico":
        img.save(salida, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    else:
        img.resize((512, 512)).save(salida)
    return salida


def preparar(destino: str, commit: str = "HEAD") -> Path:
    triple, plataformas = DESTINOS[destino]
    trabajo = TRABAJO / destino
    if trabajo.exists():
        shutil.rmtree(trabajo)
    app = trabajo / "Celestia"
    app.mkdir(parents=True)

    print(f"▸ Python {PY_VERSION} portátil para {destino}")
    with tarfile.open(_python_portatil(triple)) as tar:
        tar.extractall(app, filter="data")           # crea app/python/

    print("▸ Librerías (ya compiladas para ese sistema)")
    sp = str(_site_packages(app / "python", destino))
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    lista = [l.split("#")[0].strip() for l in
             (RAIZ / "dependencias" / "escritorio.txt").read_text().splitlines()]
    lista = [l for l in lista if l]
    compiladas = [l for l in lista if not l.lower().startswith(SOLO_FUENTE)]
    fuente = [l for l in lista if l.lower().startswith(SOLO_FUENTE)]
    plat = sum((["--platform", p] for p in plataformas), [])
    subprocess.run(
        pip + ["install", "--quiet", "--target", sp, "--python-version", PY_VERSION,
               "--implementation", "cp", "--only-binary=:all:", *plat,
               *compiladas, *DEPS_DE_SOLO_FUENTE],
        check=True)
    ventana = VENTANA.get(destino.split("-")[0], ())
    if ventana:
        print("▸ La ventana propia (pywebview)")
        subprocess.run(
            pip + ["install", "--quiet", "--no-deps", "--target", sp,
                   "--python-version", PY_VERSION, "--implementation", "cp",
                   "--only-binary=:all:", *plat, *ventana],
            check=True)
        fuente = fuente + list(VENTANA_SOLO_FUENTE)
    if fuente:
        ruedas = trabajo / "ruedas"
        subprocess.run(pip + ["wheel", "--quiet", "--no-deps", "-w", str(ruedas),
                              *fuente, "http-ece"], check=True)
        subprocess.run(pip + ["install", "--quiet", "--no-deps", "--target", sp,
                              *map(str, ruedas.glob("*.whl"))], check=True)
        shutil.rmtree(ruedas)

    print("▸ Código del último commit")
    _codigo(app, commit)
    _comprobar_completo(app)
    _comprobar_limpio(app)
    return trabajo


def empaquetar_windows(trabajo: Path) -> Path:
    if not shutil.which("makensis"):
        raise SystemExit("✗ falta NSIS (apt install nsis)")
    icono = _icono(trabajo / "Celestia", "ico")
    salida = DIST / "Celestia-Instalador-Windows.exe"
    version = (trabajo / "Celestia" / "VERSION_INSTALADOR").read_text().strip()
    subprocess.run(
        ["makensis", "-V2", f"-DORIGEN={trabajo / 'Celestia'}", f"-DSALIDA={salida}",
         f"-DVERSION={version}", f"-DICONO={icono or ''}",
         str(Path(__file__).resolve().parent / "windows.nsi")],
        check=True)
    return salida


def empaquetar_linux(trabajo: Path, destino: str) -> Path:
    app = trabajo / "Celestia"
    _icono(app, "png")
    abrir = app / "Celestia.sh"
    abrir.write_text(
        "#!/bin/sh\n"
        "# Doble clic (o ./Celestia.sh): enciende Celestia y abre el chat.\n"
        'cd "$(dirname "$0")" && exec ./python/bin/python3 lanzador.py "$@"\n')
    abrir.chmod(0o755)
    (app / "instalar-acceso-directo.sh").write_text(
        "#!/bin/sh\n"
        "# Pone Celestia en el menú de aplicaciones.\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'mkdir -p "$HOME/.local/share/applications"\n'
        'cat > "$HOME/.local/share/applications/celestia.desktop" <<EOF\n'
        "[Desktop Entry]\nType=Application\nName=Celestia\n"
        "Comment=Tu IA personal\nExec=$DIR/Celestia.sh\nIcon=$DIR/celestia.png\n"
        # La ventana de Celestia (navegador en modo app, --class=Celestia) se
        # agrupa con este icono en la barra en vez de con el del navegador.
        "Terminal=false\nCategories=Utility;\nStartupWMClass=Celestia\nEOF\n"
        'echo "Listo: Celestia ya está en el menú de aplicaciones."\n')
    (app / "instalar-acceso-directo.sh").chmod(0o755)
    salida = DIST / f"Celestia-{destino.replace('linux', 'Linux')}.tar.gz"
    with tarfile.open(salida, "w:gz") as tar:
        tar.add(app, arcname="Celestia")
    return salida


def empaquetar_mac(trabajo: Path, destino: str) -> Path:
    """Una .app que, al abrirse, se copia a ~/Library/Application Support.

    Una app descargada sin firmar la abre macOS desde una carpeta de sólo
    lectura (App Translocation): Celestia no podría guardar ni su memoria.
    Copiada a la carpeta del usuario, sí; y al actualizar sólo se pisa el
    código, nunca `memoria/` ni el `.env`.
    """
    app_dir = trabajo / "Celestia.app" / "Contents"
    (app_dir / "MacOS").mkdir(parents=True)
    recursos = app_dir / "Resources"
    recursos.mkdir()
    shutil.move(str(trabajo / "Celestia"), recursos / "Celestia")
    icono = _icono(recursos, "icns")
    version = (recursos / "Celestia" / "VERSION_INSTALADOR").read_text().strip()
    (app_dir / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Celestia</string>
  <key>CFBundleDisplayName</key><string>Celestia</string>
  <key>CFBundleIdentifier</key><string>io.github.momentomato709.celestia</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>CFBundleExecutable</key><string>Celestia</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  {"<key>CFBundleIconFile</key><string>celestia.icns</string>" if icono else ""}
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Para mandarle notas de voz a Celestia.</string>
</dict></plist>
""")
    arranque = app_dir / "MacOS" / "Celestia"
    arranque.write_text(
        "#!/bin/sh\n"
        'ORIGEN="$(cd "$(dirname "$0")/../Resources/Celestia" && pwd)"\n'
        'DEST="$HOME/Library/Application Support/Celestia"\n'
        'PAQUETE="$(cat "$ORIGEN/VERSION_INSTALADOR")"\n'
        "# Se copia cuando cambia ESTE paquete, no cuando el código de la carpeta es\n"
        "# distinto: la app se actualiza sola en su carpeta (celestia_lib/actualizar.py)\n"
        "# y comparar con VERSION_INSTALADOR volvía a poner la versión vieja encima.\n"
        'if [ "$(cat "$DEST/.version_del_paquete" 2>/dev/null)" != "$PAQUETE" ]; then\n'
        '  mkdir -p "$DEST"\n'
        "  # Encima, sin --delete: lo que no viene en el paquete (memoria/, logs/,\n"
        "  # .env, recibidos/, las habilidades que aprendió) no se toca. Con --delete\n"
        "  # se perdían los archivos recibidos (lo cazó Codex, 3 oct 2026).\n"
        '  rsync -a "$ORIGEN/" "$DEST/"\n'
        '  printf "%s" "$PAQUETE" > "$DEST/.version_del_paquete"\n'
        "fi\n"
        'cd "$DEST" && exec ./python/bin/python3 lanzador.py\n')
    arranque.chmod(0o755)
    salida = DIST / f"Celestia-Mac-{destino.split('-')[1]}.zip"
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((trabajo / "Celestia.app").rglob("*")):
            info = zipfile.ZipInfo(str(f.relative_to(trabajo)))
            modo = f.lstat().st_mode
            info.external_attr = (modo & 0xFFFF) << 16
            if f.is_symlink():
                z.writestr(info, str(f.readlink()))
            elif f.is_file():
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, f.read_bytes())
    return salida


def main() -> int:
    DIST.mkdir(exist_ok=True)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("destino", choices=sorted(DESTINOS))
    ap.add_argument("--commit", default="HEAD",
                    help="qué versión empaquetar (para probar sin commitear: "
                         "`git add -A && git stash create`)")
    args = ap.parse_args()
    trabajo = preparar(args.destino, args.commit)
    if args.destino.startswith("windows"):
        salida = empaquetar_windows(trabajo)
    elif args.destino.startswith("linux"):
        salida = empaquetar_linux(trabajo, args.destino)
    else:
        salida = empaquetar_mac(trabajo, args.destino)
    print(f"✓ {salida.relative_to(RAIZ)} ({salida.stat().st_size / 1e6:.0f} MB)")
    shutil.rmtree(TRABAJO / args.destino, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
