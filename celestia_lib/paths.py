"""Rutas raíz del proyecto Celestia.

Extraído del monolito en sesión 15. ROOT se calcula relativo a la ubicación de
celestia.py (un nivel arriba del paquete celestia_lib/).
"""
import os
from pathlib import Path

# El paquete vive en /root/Celestia/celestia_lib/; subimos un nivel para el ROOT
ROOT = Path(__file__).resolve().parent.parent

# Lo que es de la persona (memoria, logs, habilidades aprendidas, archivos
# recibidos y el .env con sus claves) vive junto al programa… salvo que
# CELESTIA_DATOS diga otra cosa. La app de Android lo necesita: allí el código
# va en una carpeta que se reemplaza en cada actualización.
DATOS = Path(os.environ.get("CELESTIA_DATOS", "").strip() or ROOT).expanduser()
ENV_FILE            = DATOS / ".env"
MEM_DIR             = DATOS / "memoria"
LOG_DIR             = DATOS / "logs"
SKILLS_DIR          = DATOS / "skills"
RECIBIDOS_DIR       = DATOS / "recibidos"


def no_es_el_env_real(ruta) -> None:
    """Frena a un test que va a escribir en el .env de verdad.

    3 oct 2026: los tests de canales redirigían el .env cambiando `ROOT`;
    cuando el código pasó a usar `ENV_FILE`, escribieron en el real y
    pisaron el token de Discord de Enzo (se recuperó del proceso vivo).
    """
    if os.environ.get("CELESTIA_EN_TESTS") and \
            Path(ruta).resolve() == ENV_FILE.resolve():
        raise RuntimeError(f"un test iba a escribir el .env de verdad ({ruta})")
DOMOTICA_SKILLS_DIR = SKILLS_DIR / "domotica"

# Salidas al mundo real, desviables para examinar una copia de Celestia sin que
# escriba a nadie ni toque el móvil (examen/examinar.py). En producción no se
# fija ninguna de las dos y todo va como siempre.
PUENTE_URL = (os.environ.get("CELESTIA_PUENTE", "").strip()
              or "http://127.0.0.1:8766").rstrip("/")
SIN_MOVIL = os.environ.get("CELESTIA_SIN_MOVIL", "").strip() in ("1", "true", "sí", "si")

# Los ficheros con los que Celestia trabaja con el móvil por Shizuku (el puente,
# capturas, el reflejo del ZZZ…). Ruta tal como la ve Android, porque los lee y
# escribe el lado de rish. Antes iban sueltos en la raíz de /sdcard como
# `.celestia_*`; Enzo no quiere nada de Celestia fuera de su carpeta (3 oct 2026).
MOVIL_DIR = "/sdcard/Celestia/.movil"


def _es_android() -> bool:
    """¿Corre dentro de un Android? También vale el Linux de PRoot que va
    dentro de Termux: ve el `/system` del móvil y Android le deja su entorno.
    `CELESTIA_ANDROID=0/1` lo fuerza (para probar cómo se ve en un PC)."""
    forzado = os.environ.get("CELESTIA_ANDROID", "").strip()
    if forzado in ("0", "1"):
        return forzado == "1"
    if os.environ.get("ANDROID_ROOT"):
        return True
    try:
        return Path("/system/build.prop").is_file()
    except OSError:                       # PRoot puede dar Errno 38
        return False


# Sin Android no hay apps que abrir, ni linterna, ni ZZZ que jugar: esas
# herramientas lo dicen en vez de fingir que lo han hecho (`AgentTools.execute`).
ES_ANDROID = _es_android()

if ES_ANDROID and not SIN_MOVIL:
    try:                                   # rish no crea carpetas: que exista ya
        os.makedirs(MOVIL_DIR, exist_ok=True)
    except OSError:                        # PRoot a veces da Errno 38: se reintenta al usarla
        pass



def _carpeta_de_salida() -> Path:
    """Dónde deja lo que crea para la persona: documentos, imágenes, proyectos.

    En Android, `/sdcard/Celestia`, que se ve desde la galería y el gestor de
    archivos (lo de siempre). En un PC eso no existe: `Documentos/Celestia`, o
    `~/Celestia` si no hay carpeta de documentos. `CELESTIA_SALIDA` manda.
    """
    elegida = os.environ.get("CELESTIA_SALIDA", "").strip()
    if elegida:
        return Path(elegida).expanduser()
    # En PRoot hasta `exists()` puede lanzar (Errno 38): se trata como «no».
    for candidata in (Path("/sdcard"), Path.home() / "Documents",
                      Path.home() / "Documentos"):
        try:
            if candidata.is_dir():
                return candidata / "Celestia"
        except OSError:
            continue
    # Sin que coincida con la carpeta del propio programa (instalado en ~/Celestia).
    casa = Path.home() / "Celestia"
    return DATOS / "mis_archivos" if casa.resolve() == ROOT else casa


# No se crea al importar: en Android es almacenamiento compartido, y cada
# función que guarda algo ya crea su subcarpeta.
SALIDA_DIR = _carpeta_de_salida()
DOCUMENTOS_DIR = SALIDA_DIR / "documentos"
IMAGENES_DIR = SALIDA_DIR / "imagenes"
PROYECTOS_DIR = SALIDA_DIR / "proyectos"

# Crear al importar para que existan antes de cualquier uso
for _d in (MEM_DIR, LOG_DIR, SKILLS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
