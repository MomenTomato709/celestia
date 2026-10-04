#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copia una versión ya fabricada del repositorio privado al público.

    python3 instalador/publicar_publica.py v2.2.7

La página de descarga (celestia_lib/web/descargar.html) y la actualización de
la app (celestia_lib/actualizar.py) leen del repositorio PÚBLICO, pero GitHub
fabrica y prueba en el privado. Mientras no exista el secreto
CELESTIA_PUBLICAR_TOKEN en el privado, la copia se hace con esto.

Llaves, del .env (nunca se imprimen):
  GITHUB_TOKEN_REPO  lee la versión del privado (alcance repo)
  GITHUB_TOKEN       escribe en el público (alcance public_repo)
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PRIVADO = "MomenTomato709/celestia-app"
PUBLICO = "MomenTomato709/celestia"
TIPOS = {".apk": "application/vnd.android.package-archive"}


def _env(nombre: str) -> str:
    for linea in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
        k, _, v = linea.strip().partition("=")
        if k == nombre:
            return v.strip().strip('"').strip("'")
    raise SystemExit(f"Falta {nombre} en el .env")


def _pedir(url: str, token: str, *, metodo: str = "GET", datos: bytes | None = None,
           tipo: str = "application/json", acepta: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, data=datos, method=metodo, headers={
        "Authorization": f"Bearer {token}", "Accept": acepta,
        "Content-Type": tipo, "User-Agent": "celestia-publicar"})
    return urllib.request.urlopen(req, timeout=1800)


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("v"):
        raise SystemExit(__doc__)
    etiqueta = sys.argv[1]
    privado, publico = _env("GITHUB_TOKEN_REPO"), _env("GITHUB_TOKEN")
    with _pedir(f"https://api.github.com/repos/{PRIVADO}/releases/tags/{etiqueta}", privado) as r:
        version = json.load(r)
    archivos = version["assets"]
    if len(archivos) < 6:
        raise SystemExit(f"{etiqueta} sólo tiene {len(archivos)} archivos (faltan por fabricar)")
    with tempfile.TemporaryDirectory() as tmp:
        bajados = []
        for a in archivos:
            destino = Path(tmp) / a["name"]
            with _pedir(a["url"], privado, acepta="application/octet-stream") as r, \
                    open(destino, "wb") as f:
                while bloque := r.read(1 << 20):
                    f.write(bloque)
            if destino.stat().st_size != a["size"]:
                raise SystemExit(f"{a['name']} llegó incompleto")
            bajados.append(destino)
            print(f"  ↓ {a['name']} ({a['size'] / 1e6:.0f} MB)")
        cuerpo = json.dumps({
            "tag_name": etiqueta, "target_commitish": "main", "name": f"Celestia {etiqueta[1:]}",
            "body": (version.get("body") or "") + "\n\nElige tu sistema en "
                    "https://momentomato709.github.io/celestia/descargar.html",
        }).encode()
        with _pedir(f"https://api.github.com/repos/{PUBLICO}/releases", publico,
                    metodo="POST", datos=cuerpo) as r:
            nueva = json.load(r)
        base = nueva["upload_url"].split("{")[0]
        for f in bajados:
            with _pedir(f"{base}?name={f.name}", publico, metodo="POST", datos=f.read_bytes(),
                        tipo=TIPOS.get(f.suffix, "application/octet-stream")) as r:
                print(f"  ↑ {f.name}: {json.load(r)['state']}")
    print(f"✓ {nueva['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
