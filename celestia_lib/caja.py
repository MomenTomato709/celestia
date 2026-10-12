"""La caja cerrada: ejecutar las pruebas de un proyecto que ha escrito Celestia.

Enzo, 11 oct 2026: «así nos aseguramos de que siempre funcione el código que ella
escriba». Hasta ahora sólo se leía el código (piezas.comprobar); un programador que
nunca ejecuta lo que escribe sólo puede adivinar si funciona. En el examen de nivel
(cuentas-viaje) salieron programas que compilaban y no arrancaban (un NameError, un
dict donde se esperaba otra cosa) y sus propias pruebas lo habrían dicho en un
segundo.

`probar(carpeta)` copia el proyecto a una carpeta nueva, arranca el mismo Python con
la jaula de caja_jaula.py (sin red, sin disco fuera de la caja, sin las claves ni
otros programas), un tope de tiempo y, en Linux/Android, de CPU y memoria, y ejecuta
sus pruebas (pytest si lo hay; si no, unittest). Devuelve cuántas fallan, la salida
y en qué ficheros del proyecto están los fallos. No toca la carpeta original.

`CELESTIA_SIN_CAJA=1` la apaga. En la app de Android (Chaquopy) no hay un Python que
lanzar y no se usa.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .paths import SIN_VENTANA, entorno_de_scripts, python_de_scripts

TIEMPO_MAX_S = 90
CPU_MAX_S = 60
MEMORIA_MAX = 1536 * 1024 * 1024
FICHERO_MAX = 50 * 1024 * 1024
MAX_SALIDA = 6000
_NO_COPIAR = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv", "venv",
                                    "*.zip", ".pytest_cache")


@dataclass
class Resultado:
    estado: str                    # «ok», «fallan» o «no_se_pudo»
    ejecutadas: int = 0
    fallidas: int = 0
    salida: str = ""
    culpables: List[str] = field(default_factory=list)
    motivo: str = ""


def activa() -> bool:
    if os.environ.get("CELESTIA_SIN_CAJA", "").strip() in ("1", "si", "sí", "true"):
        return False
    return Path(python_de_scripts()).exists()


def _limites() -> None:                                   # en el hijo, antes de arrancar
    try:
        import resource
        for nombre, tope in (("RLIMIT_CPU", CPU_MAX_S), ("RLIMIT_AS", MEMORIA_MAX),
                             ("RLIMIT_FSIZE", FICHERO_MAX)):
            limite = getattr(resource, nombre, None)
            if limite is not None:
                try:
                    resource.setrlimit(limite, (tope, tope))
                except (OSError, ValueError):
                    pass                                  # algunos PRoot no dejan
    except ImportError:
        pass


def _orden(caja: Path, usa_pytest: bool) -> List[str]:
    python = python_de_scripts()
    if usa_pytest:
        return [python, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
                "--rootdir", str(caja), "-c", str(caja / "_jaula" / "pytest.ini"), str(caja)]
    # unittest no entra en una carpeta de pruebas sin __init__.py: se le dice dónde.
    carpetas = sorted({p.parent for p in caja.rglob("test*.py") if "_jaula" not in p.parts})
    inicio = carpetas[0] if len(carpetas) == 1 else caja
    return [python, "-m", "unittest", "discover", "-s", str(inicio), "-t", str(caja),
            "-p", "test*.py"]


def _contar(salida: str, usa_pytest: bool):
    """(ejecutadas, fallidas) o None si no llegó a ejecutar pruebas."""
    if usa_pytest:
        final = "\n".join(salida.strip().splitlines()[-3:])
        n = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?)", final)}
        if not n:
            return None
        fallidas = n.get("failed", 0) + n.get("error", 0) + n.get("errors", 0)
        return n.get("passed", 0) + fallidas, fallidas
    m = re.search(r"^Ran (\d+) tests?", salida, re.M)
    if not m:
        return None
    fallo = re.search(r"^FAILED \(([^)]*)\)", salida, re.M)
    fallidas = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)",
                                              fallo.group(1))) if fallo else 0
    return int(m.group(1)), fallidas


def _culpables(salida: str, caja: Path) -> List[str]:
    """Los ficheros del proyecto que salen en los fallos, primero los que no son pruebas."""
    from .piezas import es_prueba
    raiz = str(caja.resolve())
    vistos: dict = {}
    for ruta in re.findall(r'File "([^"]+)", line \d+|^([^\s:][^:\n]*\.py):\d+:', salida, re.M):
        ruta = next(r for r in ruta if r)
        p = Path(ruta)
        try:
            rel = (p if p.is_absolute() else caja / p).resolve().relative_to(raiz).as_posix()
        except (ValueError, OSError):
            continue
        if rel.startswith("_jaula/"):
            continue
        vistos[rel] = vistos.get(rel, 0) + 1
    return sorted(vistos, key=lambda r: (es_prueba(r), -vistos[r]))


def probar(carpeta: Path, tiempo_max: int = TIEMPO_MAX_S) -> Resultado:
    """Ejecuta las pruebas del proyecto de `carpeta` dentro de la caja."""
    if not activa():
        return Resultado("no_se_pudo", motivo="la caja está apagada o no hay Python")
    carpeta = Path(carpeta)
    if not any(carpeta.rglob("test*.py")) and not any(carpeta.rglob("*_test.py")):
        return Resultado("no_se_pudo", motivo="el proyecto no tiene pruebas de Python")
    caja = Path(tempfile.mkdtemp(prefix="celestia_caja_")).resolve()
    try:
        # symlinks=True: un enlace a algo de fuera se copia como enlace (y la jaula no
        # deja leer a través de él), no como una copia de lo de fuera.
        shutil.copytree(carpeta, caja, dirs_exist_ok=True, ignore=_NO_COPIAR, symlinks=True)
        jaula = caja / "_jaula"
        jaula.mkdir()
        (caja / "_tmp").mkdir()
        shutil.copyfile(Path(__file__).with_name("caja_jaula.py"), jaula / "sitecustomize.py")
        (jaula / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (jaula / "puertos").write_text("", encoding="utf-8")
        tmp = str(caja / "_tmp")
        entorno = entorno_de_scripts({
            # Nada de lo de Celestia: ni sus claves, ni su carpeta, ni la casa de Enzo.
            # El mismo Python delante: en Termux no está en /usr/bin (en Windows,
            # entorno_de_scripts pone el suyo).
            "PATH": os.pathsep.join([str(Path(python_de_scripts()).parent),
                                     "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": str(caja), "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONPATH": str(jaula), "CAJA_RAIZ": str(caja),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        })
        if os.name == "nt":
            entorno.update({"TEMP": tmp, "TMP": tmp, "USERPROFILE": str(caja)})
        usa_pytest = _tiene_pytest(entorno, caja)
        orden = _orden(caja, usa_pytest)
        extra = (dict(start_new_session=True, preexec_fn=_limites) if os.name == "posix"
                 else dict(creationflags=SIN_VENTANA.get("creationflags", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)))
        proc = subprocess.Popen(orden, cwd=str(caja), env=entorno, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **extra)
        try:
            bruto, _ = proc.communicate(timeout=tiempo_max)
        except subprocess.TimeoutExpired:
            _matar(proc)
            proc.communicate()
            return Resultado("no_se_pudo", motivo=f"las pruebas no acabaron en {tiempo_max} s "
                                                  "(¿un bucle sin fin o algo que espera?)")
        finally:
            _matar(proc)          # lo que haya dejado vivo (un servidor de una prueba…)
        salida = bruto.decode("utf-8", "replace").replace(str(caja) + os.sep, "")
        cuenta = _contar(salida, usa_pytest)
        if cuenta is None or cuenta[0] == 0:
            return Resultado("no_se_pudo", salida=salida[-MAX_SALIDA:],
                             motivo="no llegó a ejecutar ninguna prueba")
        ejecutadas, fallidas = cuenta
        # Un paquete que aquí no está (flask, requests…) no es un fallo del código: si
        # se «arreglase», se quitaría lo que el proyecto necesita. Se dice y no se toca.
        propios = {p.stem for p in caja.rglob("*.py")} | {p.name for p in caja.rglob("*")
                                                          if p.is_dir()}
        faltan = sorted({m.split(".")[0] for m in re.findall(
            r"ModuleNotFoundError: No module named '([\w.]+)'", salida)} - propios)
        if faltan:
            return Resultado("no_se_pudo", ejecutadas, fallidas, salida[-MAX_SALIDA:],
                             motivo=f"usa paquetes que aquí no están instalados "
                                    f"({', '.join(faltan)}); no instalo nada")
        if not fallidas and proc.returncode != 0:
            return Resultado("no_se_pudo", ejecutadas, 0, salida[-MAX_SALIDA:],
                             motivo=f"salió con código {proc.returncode} sin ningún fallo contado")
        return Resultado("ok" if not fallidas else "fallan", ejecutadas, fallidas,
                         salida[-MAX_SALIDA:], _culpables(salida, caja))
    except OSError as e:
        return Resultado("no_se_pudo", motivo=f"no se pudo montar la caja: {e}")
    finally:
        shutil.rmtree(caja, ignore_errors=True)


def _tiene_pytest(entorno: dict, caja: Path) -> bool:
    try:
        r = subprocess.run([python_de_scripts(), "-c", "import pytest"], cwd=str(caja),
                           env=entorno, capture_output=True, timeout=30, **SIN_VENTANA)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _matar(proc: subprocess.Popen) -> None:
    """Al hijo y a todo lo que haya lanzado."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, **SIN_VENTANA)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
