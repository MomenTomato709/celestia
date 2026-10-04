#!/usr/bin/env python3
"""Copia a android/ lo que la app necesita del repositorio, antes de `briefcase`.

Briefcase sólo empaqueta lo que cuelga de android/, y Celestia vive en la raíz.
Copiar (y no enlazar) es a propósito: lo que entra en la APK es exactamente
esto, sin `__pycache__` ni nada que no esté en git.

    python3 android/preparar.py && cd android && briefcase build android
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ANDROID = Path(__file__).resolve().parent
RAIZ = ANDROID.parent
SRC = ANDROID / "src"


def _en_git(ruta: Path) -> list[Path]:
    """Los ficheros de `ruta` que están en git (nada de datos ni cachés)."""
    salida = subprocess.run(["git", "ls-files", "-z", str(ruta.relative_to(RAIZ))],
                            cwd=RAIZ, check=True, capture_output=True).stdout
    return [RAIZ / p for p in salida.decode().split("\0") if p]


def copiar_codigo() -> None:
    destino = SRC / "celestia_lib"
    if destino.exists():
        shutil.rmtree(destino)
    for f in _en_git(RAIZ / "celestia_lib"):
        d = destino / f.relative_to(RAIZ / "celestia_lib")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, d)
    # celestia.py es un script, no un paquete: va dentro de la app como módulo.
    shutil.copy2(RAIZ / "celestia.py", SRC / "celestia_movil" / "nucleo.py")
    imagenes = SRC / "imagenes"
    imagenes.mkdir(exist_ok=True)
    for f in _en_git(RAIZ / "imagenes"):
        shutil.copy2(f, imagenes / f.name)
    shutil.copy2(RAIZ / "LICENSE", ANDROID / "LICENSE")


def iconos() -> None:
    """Los tamaños que pide Briefcase para Android, sacados del logo."""
    from PIL import Image, ImageDraw
    logo = Image.open(RAIZ / "imagenes" / "celestia_logo_transparente.png").convert("RGBA")
    carpeta = ANDROID / "icono"
    carpeta.mkdir(exist_ok=True)
    fondo = (11, 13, 23, 255)                       # el #0B0D17 del chat
    for lado in (48, 72, 96, 144, 192, 320, 480, 640, 960, 1280):
        cuadrado = Image.new("RGBA", (lado, lado), fondo)
        cuadrado.alpha_composite(logo.resize((lado, lado), Image.LANCZOS))
        cuadrado.save(carpeta / f"celestia-square-{lado}.png")
        mascara = Image.new("L", (lado, lado), 0)
        ImageDraw.Draw(mascara).ellipse((0, 0, lado - 1, lado - 1), fill=255)
        redondo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
        redondo.paste(cuadrado, (0, 0), mascara)
        redondo.save(carpeta / f"celestia-round-{lado}.png")
    # Iconos adaptativos (Android 8+): el dibujo en el 66 % central.
    for lado in (108, 162, 216, 324, 432):
        primer = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
        interior = int(lado * 0.66)
        primer.alpha_composite(logo.resize((interior, interior), Image.LANCZOS),
                               ((lado - interior) // 2, (lado - interior) // 2))
        primer.save(carpeta / f"celestia-adaptive-{lado}.png")


if __name__ == "__main__":
    copiar_codigo()
    iconos()
    print("✓ android/ listo para briefcase")
