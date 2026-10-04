#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publica una versión de Celestia en el repositorio PÚBLICO (MomenTomato709/celestia).

Por qué: el repositorio privado (celestia-app) se quedó sin minutos de GitHub
Actions, y en uno público son gratis. Enzo eligió «copia limpia» (3 oct 2026):
sólo lo que hace falta para fabricar y usar Celestia, sin la historia de git
ni lo personal (la memoria de Claude, la hoja de ruta, los tests con casos de
chats reales…). Va a la rama `codigo`; `main` sigue siendo la web.

    python instalador/copia_publica.py v2.4.0            # prepara y revisa (no sube nada)
    python instalador/copia_publica.py v2.4.0 --subir    # sube la rama y la etiqueta
    python instalador/copia_publica.py v2.4.0 --subir --sin-etiqueta   # sólo probar en GitHub

Al subir la etiqueta, GitHub fabrica allí los instaladores y la APK, los prueba
y los publica en la versión (instaladores.yml, android.yml). Necesita
GITHUB_TOKEN_REPO en el .env (permisos repo + workflow); el token nunca va en
la línea de órdenes.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
TRABAJO = Path.home() / ".cache" / "celestia-publicar"
REPO = "MomenTomato709/celestia"
RAMA = "codigo"

# Lo que va. Todo lo demás (contexto_claude, roadmap, docs, tests, examen,
# scripts del juego, .claude, CLAUDE.md…) se queda en el privado.
INCLUIR = ("celestia.py", "lanzador.py", "LICENSE", "README.md", ".gitignore", ".env.example",
           "celestia_lib/", "imagenes/", "instalador/", "android/", "dependencias/",
           "telegram_bridge/", "discord_bridge/", "whatsapp_bridge/",
           ".github/workflows/instaladores.yml", ".github/workflows/android.yml",
           ".github/workflows/ruedas_android.yml")

# Si aparece algo de esto, no se sube: se mira a mano primero. Los lugares y el
# trabajo de Enzo NO se escriben aquí (este fichero es público): están en
# contexto_claude/marcas_personales.txt, que se queda en el privado.
PERSONAL = {
    "correo": r"[\w.+-]+@(?:gmail|hotmail|outlook|yahoo|icloud)\.\w+",
    # Teléfonos de España de verdad; los de ejemplo (600111222…) se dejan.
    "teléfono": r"(?<![\d.])(?:\+?34[ -]?)?[67]\d{2}[ -]?\d{3}[ -]?\d{3}(?![\d.])",
}
_PRIVADAS = RAIZ / "contexto_claude" / "marcas_personales.txt"
if _PRIVADAS.exists():
    for _linea in _PRIVADAS.read_text(encoding="utf-8").splitlines():
        if "=" in _linea and not _linea.lstrip().startswith("#"):
            _nombre, _forma = (p.strip() for p in _linea.split("=", 1))
            PERSONAL[_nombre] = _forma
TELEFONOS_DE_EJEMPLO = re.compile(r"(?:34)?6000|(?:34)?600[ -]?(?:111|333|11)|(?:34)?612345678")


def _git(*args: str, cwd: Path = RAIZ, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout


def _entra(nombre: str) -> bool:
    return any(nombre == i or (i.endswith("/") and nombre.startswith(i)) for i in INCLUIR)


def preparar(commit: str) -> Path:
    """La copia limpia del `commit`, en TRABAJO/copia."""
    copia = TRABAJO / "copia"
    if copia.exists():
        shutil.rmtree(copia)
    copia.mkdir(parents=True)
    datos = subprocess.run(["git", "archive", "--format=tar", commit], cwd=RAIZ,
                           check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(datos)) as tar:
        tar.extractall(copia, members=[m for m in tar.getmembers() if _entra(m.name)],
                       filter="data")
    return copia


def _claves_del_env() -> list[bytes]:
    env = RAIZ / ".env"
    claves = []
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            k, _, v = linea.partition("=")
            v = v.strip().strip('"').strip("'")
            if len(v) >= 16 and any(t in k for t in ("KEY", "TOKEN", "SECRET", "LLAVE")):
                claves.append(v.encode())
    return claves


def revisar(copia: Path) -> list[str]:
    """Lo que no debería salir. Vacío si está limpia."""
    problemas = []
    claves = _claves_del_env()
    for f in sorted(copia.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(copia).as_posix()
        if rel.split("/")[0] in (".env", "memoria", "logs", "recibidos") or rel.endswith(".p12"):
            problemas.append(f"{rel}: no debería estar")
            continue
        datos = f.read_bytes()
        if any(c in datos for c in claves):
            problemas.append(f"{rel}: lleva una clave del .env")
        texto = datos.decode("utf-8", "ignore")
        for nombre, forma in PERSONAL.items():
            for m in re.finditer(forma, texto, re.IGNORECASE):
                if nombre == "teléfono" and TELEFONOS_DE_EJEMPLO.match(re.sub(r"\D", "", m.group(0))):
                    continue
                linea = texto.count("\n", 0, m.start()) + 1
                problemas.append(f"{rel}:{linea}: {nombre}")
    return problemas


def subir(copia: Path, version: str, etiquetar: bool = True) -> None:
    token = os.environ.get("GITHUB_TOKEN_REPO", "")
    if not token:
        for linea in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
            if linea.startswith("GITHUB_TOKEN_REPO="):
                token = linea.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        raise SystemExit("✗ falta GITHUB_TOKEN_REPO en el .env")
    cabecera = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # El token va por el entorno, no por argumentos (que cualquiera ve con ps).
    env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
               GIT_CONFIG_VALUE_0=f"Authorization: Basic {cabecera}", GIT_TERMINAL_PROMPT="0")
    repo = TRABAJO / "repo"
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git("init", "-q", cwd=repo)
        _git("remote", "add", "origin", f"https://github.com/{REPO}.git", cwd=repo)
    hay_rama = bool(_git("ls-remote", "--heads", "origin", RAMA, cwd=repo, env=env).strip())
    if hay_rama:
        _git("fetch", "-q", "--depth", "1", "origin", RAMA, cwd=repo, env=env)
        _git("checkout", "-q", "-B", RAMA, "FETCH_HEAD", cwd=repo)
    else:
        _git("checkout", "-q", "--orphan", RAMA, cwd=repo)
    # El árbol entero se sustituye por la copia: lo que se borró en el privado
    # también desaparece aquí.
    for f in repo.iterdir():
        if f.name != ".git":
            shutil.rmtree(f) if f.is_dir() else f.unlink()
    shutil.copytree(copia, repo, dirs_exist_ok=True)
    nombre = _git("config", "user.name").strip() or "Celestia"
    correo = _git("config", "user.email").strip()
    autor = ["-c", f"user.name={nombre}", "-c", f"user.email={correo}"]
    _git("add", "-A", cwd=repo)
    origen = _git("rev-parse", "--short", "HEAD").strip()
    mensaje = f"Celestia {version}" + ("" if etiquetar else f" (prueba, {origen})")
    _git(*autor, "commit", "-q", "--allow-empty", "-m", mensaje, cwd=repo)
    _git("push", "-q", "origin", f"{RAMA}:{RAMA}", cwd=repo, env=env)
    if etiquetar:
        _git(*autor, "tag", "-f", "-a", version, "-m", f"Celestia {version}", cwd=repo)
        _git("push", "-q", "-f", "origin", f"refs/tags/{version}", cwd=repo, env=env)
        print(f"✓ subida {version} a {REPO}: GitHub fabrica, prueba y publica")
    else:
        print(f"✓ subida a {REPO} (rama {RAMA}, sin etiqueta): GitHub fabrica y prueba, sin publicar")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version", help="la etiqueta, p. ej. v2.4.0")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--subir", action="store_true", help="subirla de verdad")
    ap.add_argument("--sin-etiqueta", action="store_true",
                    help="sólo la rama: GitHub fabrica y prueba, pero no publica versión")
    args = ap.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.version):
        raise SystemExit("✗ la versión va como v2.4.0")
    copia = preparar(args.commit)
    problemas = revisar(copia)
    n = sum(1 for f in copia.rglob("*") if f.is_file())
    print(f"▸ copia limpia: {n} ficheros en {copia}")
    if problemas:
        print("✗ no se sube; revisa esto primero:")
        for p in problemas[:50]:
            print("   ", p)
        return 1
    print("✓ sin claves ni datos personales")
    if args.subir:
        subir(copia, args.version, etiquetar=not args.sin_etiqueta)
    else:
        print("  (para subirla: --subir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
