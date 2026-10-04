"""Las pestañas de la app: lo que Celestia ha hecho para la persona.

Proyectos, documentos e imágenes vivían solo en una carpeta del disco: para
verlos había que salir del chat y buscarlos a mano (Enzo, 3 oct 2026: «una app
con su chat y todas las opciones que tenga, con sus pestañas dentro para crear
proyectos y cosas así»). Aquí se listan, se descargan y, en un PC, se abre su
carpeta con el explorador del sistema.

Solo lectura del disco, y siempre dentro de las tres carpetas de salida: el
nombre que llega se reduce a un nombre suelto y se comprueba que el resultado
sigue dentro. Las rutas van protegidas por la misma llave que el resto de la
API (`X-Celestia-Token`), así que una visita de la web pública no las alcanza.
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from flask import Blueprint, jsonify, request as flask_request, send_file

from .. import paths

logger = logging.getLogger("celestia_v1")

# Lo que se muestra de cada carpeta. Más que esto no cabe en una pestaña y
# leerlo todo en un móvil con miles de imágenes tardaría segundos.
MAX_ELEMENTOS = 300
# Un proyecto se descarga como ZIP hecho al vuelo, en memoria: con un tope,
# para que una carpeta con un `node_modules` dentro no se coma la RAM del móvil.
MAX_ZIP_BYTES = 150 * 1024 * 1024
# Dentro de un proyecto, lo que no es del proyecto: cachés y el historial de
# las herramientas que lo construyeron.
NO_VA_EN_EL_ZIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
                   ".aider.tags.cache.v4"}
NO_VA_PREFIJO = (".aider",)


def carpetas() -> Dict[str, Path]:
    """Se leen de `paths` en cada llamada: los tests las cambian."""
    return {"proyectos": paths.PROYECTOS_DIR,
            "documentos": paths.DOCUMENTOS_DIR,
            "imagenes": paths.IMAGENES_DIR}


def _dentro(base: Path, nombre: str) -> Optional[Path]:
    """`base/nombre` si `nombre` es un nombre suelto y el resultado no se sale
    de `base`; None en cualquier otro caso (barras, «..», vacío)."""
    nombre = (nombre or "").strip()
    if not nombre or nombre in (".", "..") or "/" in nombre or "\\" in nombre \
            or "\x00" in nombre:
        return None
    try:
        ruta = (base / nombre).resolve()
        if ruta.parent != base.resolve():
            return None
    except (OSError, ValueError):
        return None
    return ruta


def _cuanto_y_cuando(ruta: Path) -> tuple:
    """(bytes, última modificación, nº de archivos) de un archivo o carpeta.
    En una carpeta se cuenta con tope: es para pintar una fila, no un inventario."""
    if ruta.is_file():
        st = ruta.stat()
        return st.st_size, st.st_mtime, 1
    total, ultimo, n = 0, ruta.stat().st_mtime, 0
    for i, f in enumerate(ruta.rglob("*")):
        if i > 5000:
            break
        if any(p in NO_VA_EN_EL_ZIP or p.startswith(NO_VA_PREFIJO)
               for p in f.relative_to(ruta).parts):
            continue
        try:
            if f.is_file():
                st = f.stat()
                total += st.st_size
                ultimo = max(ultimo, st.st_mtime)
                n += 1
        except OSError:
            continue
    return total, ultimo, n


def listar(tipo: str) -> List[dict]:
    base = carpetas()[tipo]
    try:
        if not base.is_dir():
            return []
        hijos = list(base.iterdir())
    except OSError:
        return []
    filas = []
    for h in hijos:
        if h.name.startswith("."):
            continue
        try:
            tam, ts, n = _cuanto_y_cuando(h)
        except OSError:
            continue
        filas.append({"nombre": h.name, "carpeta": h.is_dir(),
                      "bytes": tam, "ts": ts, "archivos": n})
    filas.sort(key=lambda f: f["ts"], reverse=True)
    return filas[:MAX_ELEMENTOS]


def zip_de(carpeta: Path) -> Optional[io.BytesIO]:
    """La carpeta entera en un ZIP en memoria, o None si pasa del tope."""
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(carpeta.rglob("*")):
            partes = f.relative_to(carpeta).parts
            if any(p in NO_VA_EN_EL_ZIP or p.startswith(NO_VA_PREFIJO) for p in partes):
                continue
            if f.is_symlink() or not f.is_file():
                continue          # un enlace podría apuntar fuera del proyecto
            total += f.stat().st_size
            if total > MAX_ZIP_BYTES:
                return None
            z.write(f, Path(carpeta.name, *partes).as_posix())
    buf.seek(0)
    return buf


def abrir_en_el_sistema(ruta: Path) -> bool:
    """Abre la carpeta con el explorador del sistema (Explorador, Finder…)."""
    try:
        if sys.platform == "win32":
            os.startfile(str(ruta))                      # noqa: S606 (ruta nuestra)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ruta)])
        else:
            subprocess.Popen(["xdg-open", str(ruta)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError) as e:
        logger.info("No pude abrir %s: %s", ruta, e)
        return False


def crear(api) -> Blueprint:
    bp = Blueprint("espacio", __name__)

    def _tipo():
        tipo = (flask_request.args.get("tipo") or
                (flask_request.get_json(silent=True) or {}).get("tipo") or "")
        return tipo if tipo in carpetas() else None

    @bp.route("/espacio/archivos", methods=["GET"])
    def espacio_archivos():
        """Lo que hay en proyectos, documentos o imágenes, lo último primero."""
        tipo = _tipo()
        if not tipo:
            return jsonify({"error": "tipo tiene que ser proyectos, documentos o imagenes"}), 400
        return jsonify({"tipo": tipo, "carpeta": str(carpetas()[tipo]),
                        "elementos": listar(tipo),
                        # En el móvil la carpeta se ve desde la galería; abrirla
                        # desde aquí solo tiene sentido en un ordenador.
                        "se_puede_abrir": not paths.ES_ANDROID})

    @bp.route("/espacio/descargar", methods=["GET"])
    def espacio_descargar():
        """Un archivo tal cual, o un proyecto entero como ZIP."""
        tipo = _tipo()
        if not tipo:
            return jsonify({"error": "tipo no válido"}), 400
        ruta = _dentro(carpetas()[tipo], flask_request.args.get("nombre", ""))
        if ruta is None or not ruta.exists():
            return jsonify({"error": "no existe"}), 404
        if ruta.is_dir():
            datos = zip_de(ruta)
            if datos is None:
                return jsonify({"error": "el proyecto es demasiado grande para "
                                         "descargarlo de una vez"}), 413
            return send_file(datos, mimetype="application/zip", as_attachment=True,
                             download_name=f"{ruta.name}.zip")
        return send_file(ruta, as_attachment=True, download_name=ruta.name)

    @bp.route("/espacio/abrir", methods=["POST"])
    def espacio_abrir():
        """Abre la carpeta (o la de un proyecto) en el explorador del PC.

        Solo desde el propio aparato: quien entra por la red no está delante
        de esa pantalla, y abrir ventanas en el ordenador de otro no es cosa
        suya. Y nunca en Android, donde no hay explorador que abrir.
        """
        if (flask_request.remote_addr or "") not in ("127.0.0.1", "::1"):
            return jsonify({"error": "solo desde este aparato"}), 403
        if paths.ES_ANDROID:
            return jsonify({"error": "en el móvil se abre desde la galería"}), 400
        tipo = _tipo()
        if not tipo:
            return jsonify({"error": "tipo no válido"}), 400
        base = carpetas()[tipo]
        nombre = (flask_request.get_json(silent=True) or {}).get("nombre") or ""
        if nombre:
            ruta = _dentro(base, nombre)
            if ruta is None or not ruta.exists():
                return jsonify({"error": "no existe"}), 404
            if ruta.is_file():
                ruta = ruta.parent
        else:
            ruta = base
            try:
                ruta.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        return jsonify({"ok": abrir_en_el_sistema(ruta)})

    return bp
