"""Trabajar sobre un proyecto que ya existe: el que Enzo pasa como ZIP.

Examen de nivel, 11 oct 2026 (arreglar-citas): se le pasó el programa de citas de una
peluquería con dos fallos y una orden nueva que añadir, y contestó «descomprime el zip
y me dices dónde queda, o pégame aquí el contenido». Entendía el encargo, pero no sabía
abrir un proyecto, cambiarlo y devolverlo, que es casi todo el trabajo de programar.

Aquí, sin red ni modelo:
  `adjunto(texto, buzon)` reconoce un ZIP de código adjunto al mensaje (chat o WhatsApp)
      y separa lo que se pide;
  `extraer(zip, destino)` lo abre sin salirse de la carpeta y sin bombas;
  `leer(carpeta)` da sus ficheros de texto;
  `resumen(antes, despues)` dice qué cambió.
El trabajo con el modelo (planear, cambiar, probar en la caja) está en api.py.
"""
from __future__ import annotations

import difflib
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

# El chat (web/chat.html) y WhatsApp (bp_chat.anexar_archivo_recibido) adjuntan así.
_RE_CHAT = re.compile(
    r"Te he pasado el archivo «([^»\n]+?\.zip)» y lo tienes en (.+?), pero no es texto que "
    r"puedas leer directamente\. Dime qué hago con él\.", re.I)
_RE_WHATSAPP = re.compile(r"\[ARCHIVO RECIBIDO:\s*([^\]\n]+?\.zip)\s+—\s+\d+\s+bytes\]", re.I)

EXT_CODIGO = {".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css",
              ".scss", ".lua", ".luau", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".c",
              ".cpp", ".h", ".cs", ".swift", ".sh", ".sql", ".vue"}
EXT_TEXTO = EXT_CODIGO | {".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".ini", ".cfg",
                          ".csv", ".xml", ".env.example", ".gitignore", ".rst"}
_BASURA = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
           ".mypy_cache", ".idea", ".vscode", "__MACOSX", ".DS_Store"}
MAX_FICHEROS = 400
MAX_TOTAL = 20 * 1024 * 1024          # descomprimido: un ZIP pequeño puede ser una bomba
MAX_TEXTO = 300 * 1024                # un fichero de texto más grande no se le enseña

# «arréglalo», «añade…», «cambia…»: lo que convierte el adjunto en un encargo.
_PIDE_CAMBIO_RE = re.compile(
    r"\b(?:arregl|correg|corrij|a[ñn]ad|agreg|cambi|modific|mejor|implement|quit|elimin|"
    r"refactoriz|optimiz|actualiz|traduc|repar|soluciona|haz|hazle|pon|ponle|mete|crea)\w*",
    re.I)


class ProyectoMalo(ValueError):
    """El ZIP no se puede abrir como proyecto; el mensaje es para Enzo."""


def adjunto(texto: str, buzon: Path) -> Optional[Tuple[Path, str]]:
    """(ruta del ZIP, lo que pide) si el mensaje trae un ZIP de código del buzón."""
    texto = texto or ""
    m = _RE_CHAT.search(texto)
    if m:
        ruta = Path(m.group(2).strip())
    else:
        m = _RE_WHATSAPP.search(texto)
        if not m:
            return None
        ruta = Path(buzon) / PurePosixPath(m.group(1).replace("\\", "/")).name
    try:
        ruta = ruta.resolve()
        buzon = Path(buzon).resolve()
    except OSError:
        return None
    # Sólo lo que está en el buzón: la ruta va en el texto y cualquiera la puede escribir.
    if buzon not in ruta.parents or not ruta.is_file() or not es_de_codigo(ruta):
        return None
    peticion = (texto[:m.start()] + texto[m.end():]).strip()
    return ruta, peticion


def pide_un_cambio(peticion: str) -> bool:
    return bool(_PIDE_CAMBIO_RE.search(peticion or ""))


def es_de_codigo(ruta: Path) -> bool:
    try:
        with zipfile.ZipFile(ruta) as z:
            return any(PurePosixPath(n).suffix.lower() in EXT_CODIGO for n in z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def _miembros(z: zipfile.ZipFile) -> List[Tuple[zipfile.ZipInfo, PurePosixPath]]:
    sale = []
    for info in z.infolist():
        if info.is_dir():
            continue
        nombre = info.filename.replace("\\", "/")
        p = PurePosixPath(nombre)
        if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", nombre):
            raise ProyectoMalo(f"el ZIP trae una ruta que se sale de su carpeta: {nombre}")
        if any(parte in _BASURA for parte in p.parts) or p.suffix == ".pyc":
            continue
        # Un enlace simbólico dentro del ZIP podría apuntar fuera: no se extrae.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            continue
        sale.append((info, p))
    return sale


def extraer(zip_path: Path, destino: Path) -> Tuple[str, List[str]]:
    """Abre el ZIP en `destino`. Si todo va dentro de una carpeta («citas/…»), se quita
    esa carpeta: el proyecto queda en la raíz, como lo tiene Enzo. Devuelve (nombre del
    proyecto, ficheros extraídos)."""
    destino = Path(destino)
    try:
        with zipfile.ZipFile(zip_path) as z:
            miembros = _miembros(z)
            if not miembros:
                raise ProyectoMalo("el ZIP está vacío")
            if len(miembros) > MAX_FICHEROS:
                raise ProyectoMalo(f"el ZIP trae {len(miembros)} ficheros (como mucho "
                                   f"{MAX_FICHEROS})")
            if sum(i.file_size for i, _ in miembros) > MAX_TOTAL:
                raise ProyectoMalo("el ZIP descomprimido pasa de 20 MB")
            primeras = {p.parts[0] for _, p in miembros}
            envoltorio = (next(iter(primeras)) if len(primeras) == 1
                          and all(len(p.parts) > 1 for _, p in miembros) else "")
            nombre = envoltorio or Path(zip_path).stem.split("_", 1)[-1]
            hechos = []
            raiz = destino.resolve()
            for info, p in miembros:
                rel = PurePosixPath(*p.parts[1:]) if envoltorio else p
                final = (raiz / rel.as_posix()).resolve()
                if raiz not in final.parents:
                    raise ProyectoMalo(f"el ZIP trae una ruta que se sale de su carpeta: {p}")
                final.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as origen:
                    datos = origen.read(MAX_TOTAL + 1)
                if len(datos) > MAX_TOTAL:
                    raise ProyectoMalo("un fichero del ZIP pasa de 20 MB")
                final.write_bytes(datos)
                hechos.append(rel.as_posix())
            return re.sub(r"[^\w.-]", "_", nombre) or "proyecto", sorted(hechos)
    except zipfile.BadZipFile:
        raise ProyectoMalo("no es un ZIP que se pueda abrir") from None


def es_texto(ruta: str) -> bool:
    p = PurePosixPath(ruta)
    return p.suffix.lower() in EXT_TEXTO or p.name in (".gitignore", "Makefile", "Dockerfile")


def leer(carpeta: Path) -> Dict[str, str]:
    """Los ficheros de texto del proyecto (ruta relativa → contenido)."""
    carpeta = Path(carpeta)
    sale = {}
    for f in sorted(carpeta.rglob("*")):
        rel = f.relative_to(carpeta).as_posix()
        if not f.is_file() or not es_texto(rel) or f.stat().st_size > MAX_TEXTO:
            continue
        try:
            sale[rel] = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return sale


def arbol(ficheros: Dict[str, str]) -> str:
    return "\n".join(f"- {r} ({t.count(chr(10)) + 1} líneas)" for r, t in sorted(ficheros.items()))


def resumen(antes: Dict[str, str], despues: Dict[str, str]) -> List[str]:
    """«citas/horario.py: +3 −1», «tests/test_mover.py: nuevo (40 líneas)»…"""
    lineas = []
    for ruta in sorted(despues):
        nuevo = despues[ruta]
        if ruta not in antes:
            lineas.append(f"{ruta}: nuevo ({nuevo.count(chr(10)) + 1} líneas)")
            continue
        if antes[ruta] == nuevo:
            continue
        mas = menos = 0
        for d in difflib.unified_diff(antes[ruta].splitlines(), nuevo.splitlines(), lineterm="",
                                      n=0):
            if d.startswith("+") and not d.startswith("+++"):
                mas += 1
            elif d.startswith("-") and not d.startswith("---"):
                menos += 1
        lineas.append(f"{ruta}: +{mas} −{menos}")
    return lineas


def describir(zip_path: Path) -> str:
    """Lo que se contesta cuando llega el ZIP sin decir qué hacer con él."""
    nombre = Path(zip_path).name.split("_", 1)[-1]
    try:
        with zipfile.ZipFile(zip_path) as z:
            rutas = [p.as_posix() for _, p in _miembros(z)]
    except (zipfile.BadZipFile, ProyectoMalo, OSError) as e:
        return f"Tengo tu ZIP «{nombre}», pero no lo puedo abrir: {e}."
    codigo = [r for r in rutas if PurePosixPath(r).suffix.lower() in EXT_CODIGO]
    return (f"Tengo tu proyecto «{nombre}»: {len(rutas)} archivos, {len(codigo)} de código "
            f"({', '.join(codigo[:6])}{'…' if len(codigo) > 6 else ''}). ¿Qué quieres que "
            "haga con él? Puedo arreglar un fallo, añadir algo o revisarlo, y te lo devuelvo "
            "cambiado y con sus pruebas pasadas.")
