#!/usr/bin/env python3
"""Monta una rueda de Android con lo de la rueda de PC y el .so compilado para Android.

    python3 montar_rueda.py plantilla.whl libprimp.so cp312-cp312-android_24_arm64_v8a ruedas/

De la plantilla (la rueda de Linux de PyPI) se queda todo lo que es Python
(el __init__, los tipos, METADATA); el binario de PC se cambia por el de
Android, con el nombre que busca el Python de Chaquopy (`.cpython-312.so`, como
los de su lib-dynload). WHEEL y RECORD se escriben de nuevo.
"""
from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path


def _hash(datos: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(datos).digest()).rstrip(b"=").decode()


def montar(plantilla: Path, so: Path, etiqueta: str, destino: Path) -> Path:
    with zipfile.ZipFile(plantilla) as z:
        entradas = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
    info = next(n for n in entradas if n.endswith(".dist-info/WHEEL"))
    dist_info = info.rsplit("/", 1)[0]
    nombre, version = dist_info[:-len(".dist-info")].rsplit("-", 1)
    binarios = [n for n in entradas if n.endswith(".so") or n.endswith(".pyd")]
    if len(binarios) != 1:
        raise SystemExit(f"✗ esperaba un binario en la plantilla y hay {binarios}")
    paquete, fichero = binarios[0].rsplit("/", 1)
    modulo = fichero.split(".", 1)[0]

    nuevas = {n: d for n, d in entradas.items()
              if n not in binarios and not n.endswith(("/RECORD", "/WHEEL")) and "/sboms/" not in n}
    nuevas[f"{paquete}/{modulo}.cpython-312.so"] = so.read_bytes()
    nuevas[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: celestia (android/montar_rueda.py)\n"
        f"Root-Is-Purelib: false\nTag: {etiqueta}\n").encode()
    record = "".join(f"{n},{_hash(d)},{len(d)}\n" for n, d in sorted(nuevas.items()))
    record += f"{dist_info}/RECORD,,\n"
    nuevas[f"{dist_info}/RECORD"] = record.encode()

    destino.mkdir(parents=True, exist_ok=True)
    salida = destino / f"{nombre}-{version}-{etiqueta}.whl"
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in nuevas.items():
            z.writestr(n, d)
    print(f"✓ {salida.name} ({salida.stat().st_size // 1024} KB)")
    return salida


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    montar(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
