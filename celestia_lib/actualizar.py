"""Que Celestia se actualice sola, desde la propia app.

Enzo (3 oct 2026): «quiero que la misma app detecte que hay una actualización
y se instale desde la app, no teniendo que ir a GitHub y descargarla».

Cómo:
- `comprobar()`: pregunta a GitHub por la última versión publicada en el
  repositorio PÚBLICO (el mismo del que descarga la página de descarga) y la
  compara con `version.VERSION`. Sin llave: GitHub da 60 consultas por hora y
  aquí se hace una cada pocas horas.
- `instalar()`: baja el archivo de ESTE sistema, comprueba tamaño y huella
  (SHA-256 que publica GitHub) y lo aplica:
    · Windows: el instalador en silencio con /ABRIR — cierra Celestia,
      instala y la vuelve a abrir (instalador/windows.nsi).
    · Mac y Linux: un guion aparte espera a que Celestia se cierre, copia el
      código nuevo ENCIMA (memoria/, .env y lo aprendido no vienen en el
      paquete, así que no se tocan) y la vuelve a abrir.
    · App de Android: la descarga y el instalador los pone la app
      (`INSTALAR_APK`, en android/src/celestia_movil/app.py); Android pide
      siempre que la persona toque «Instalar», eso no se puede saltar.
- `vigilar()`: mira al arrancar y cada 6 horas, y avisa UNA vez por versión.

La Celestia de Termux (y cualquier copia del repositorio) no se toca: se
actualiza con git. Sólo se actualiza una copia INSTALADA (la marca el
archivo VERSION_INSTALADOR que deja el instalador) o la app de Android.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional

from .paths import ES_ANDROID, MEM_DIR, ROOT
from .version import VERSION

logger = logging.getLogger("celestia_v1")

REPO = os.environ.get("CELESTIA_REPO_PUBLICO", "").strip() or "MomenTomato709/celestia"
URL_ULTIMA = f"https://api.github.com/repos/{REPO}/releases/latest"
CADA_S = 6 * 3600
CACHE_S = 30 * 60
AVISADA = MEM_DIR / "actualizacion_avisada.txt"

# Lo pone la app de Android al arrancar: (url, bytes) → None. Baja la APK con
# el DownloadManager de Android y abre su instalador.
INSTALAR_APK: Optional[Callable[[str, int], None]] = None

# fase: "" | buscando | descargando | instalando | android | android-permiso |
# android-instalar. progreso: 0-100 mientras se descarga (None si no se sabe).
# Enzo (3 oct 2026): «tarda mucho en actualizarse desde la app» — sin
# porcentaje no había forma de saber si avanzaba o estaba atascada.
_estado: Dict[str, object] = {"ts": 0.0, "info": None, "fase": "", "error": "",
                              "progreso": None}
_candado = threading.Lock()


def numeros(version: str) -> tuple:
    """«v2.3.0» → (2, 3, 0). Lo que no sea número cuenta como 0."""
    partes = []
    for trozo in (version or "").strip().lstrip("vV").split("."):
        digitos = "".join(c for c in trozo if c.isdigit())
        partes.append(int(digitos) if digitos else 0)
    while len(partes) < 3:
        partes.append(0)
    return tuple(partes)


def es_mas_nueva(nueva: str, actual: str = VERSION) -> bool:
    return numeros(nueva) > numeros(actual)


def es_app_android() -> bool:
    return os.environ.get("CELESTIA_APP_ANDROID", "").strip() == "1"


def archivo_de_este_sistema() -> Optional[str]:
    """El archivo publicado que le toca a ESTE aparato, o None si esta copia no
    se actualiza sola (la de Termux, o el repositorio de quien la desarrolla)."""
    if es_app_android():
        return "Celestia-Android.apk"
    if ES_ANDROID or not (ROOT / "VERSION_INSTALADOR").is_file():
        return None
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "Celestia-Instalador-Windows.exe"
    if sys.platform == "darwin":
        return f"Celestia-Mac-{'arm64' if arm else 'x64'}.zip"
    if sys.platform.startswith("linux"):
        return f"Celestia-Linux-{'arm64' if arm else 'x64'}.tar.gz"
    return None


def comprobar(forzar: bool = False, abrir=urllib.request.urlopen) -> dict:
    """Lo que se sabe de la última versión. Nunca lanza."""
    archivo = archivo_de_este_sistema()
    base = {"actual": VERSION, "nueva": None, "hay": False, "se_puede": bool(archivo),
            "notas": "", "url": None, "bytes": 0, "sha256": None,
            "fase": _estado["fase"], "error": _estado["error"],
            "progreso": _estado["progreso"]}
    # Un test no sale a internet por esto (el de las rutas llama a todas).
    if abrir is urllib.request.urlopen and os.environ.get("CELESTIA_EN_TESTS"):
        return base
    ahora = time.time()
    if not forzar and _estado["info"] and ahora - float(_estado["ts"]) < CACHE_S:
        return dict(_estado["info"], fase=_estado["fase"], error=_estado["error"],
                    progreso=_estado["progreso"])
    try:
        req = urllib.request.Request(URL_ULTIMA, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "celestia"})
        with abrir(req, timeout=15) as r:
            ultima = json.loads(r.read(2_000_000))
    except Exception as e:
        logger.info("Actualización: no pude preguntar a GitHub (%s)", type(e).__name__)
        return base
    etiqueta = str(ultima.get("tag_name") or "")
    info = dict(base, nueva=etiqueta.lstrip("vV") or None,
                notas=str(ultima.get("body") or "")[:1500])
    if archivo:
        for a in ultima.get("assets") or []:
            if a.get("name") == archivo:
                digest = str(a.get("digest") or "")
                info.update(url=a.get("browser_download_url"), bytes=int(a.get("size") or 0),
                            sha256=digest[7:] if digest.startswith("sha256:") else None)
    info["hay"] = bool(etiqueta and es_mas_nueva(etiqueta) and info["url"])
    _estado.update(ts=ahora, info=info)
    return info


def descargar(info: dict, carpeta: Path, abrir=urllib.request.urlopen) -> Path:
    """Baja el archivo y comprueba tamaño y huella. Lanza si no cuadra."""
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / Path(info["url"]).name
    parcial = destino.with_suffix(destino.suffix + ".part")
    huella = hashlib.sha256()
    total, llevamos = int(info.get("bytes") or 0), 0
    req = urllib.request.Request(info["url"], headers={"User-Agent": "celestia"})
    with abrir(req, timeout=60) as r, open(parcial, "wb") as f:
        while True:
            bloque = r.read(1 << 20)
            if not bloque:
                break
            huella.update(bloque)
            f.write(bloque)
            llevamos += len(bloque)
            if total:
                _estado["progreso"] = min(100, llevamos * 100 // total)
    if info.get("bytes") and parcial.stat().st_size != info["bytes"]:
        parcial.unlink(missing_ok=True)
        raise RuntimeError("la descarga llegó incompleta")
    if info.get("sha256") and huella.hexdigest() != info["sha256"]:
        parcial.unlink(missing_ok=True)
        raise RuntimeError("la descarga no coincide con la publicada")
    parcial.replace(destino)
    return destino


def _guion_unix(nuevo: Path, pids: list, abrir_otra_vez: str) -> str:
    """Espera a que Celestia se cierre, copia lo nuevo encima y la reabre."""
    pids_txt = " ".join(str(p) for p in pids)
    return f"""#!/bin/sh
for p in {pids_txt}; do kill "$p" 2>/dev/null; done
i=0
while [ $i -lt 60 ]; do
  vivos=0
  for p in {pids_txt}; do kill -0 "$p" 2>/dev/null && vivos=1; done
  [ $vivos = 0 ] && break
  sleep 0.5; i=$((i+1))
done
cp -Rp "{nuevo}/." "{ROOT}/" && rm -rf "{nuevo}"
{abrir_otra_vez} >/dev/null 2>&1 &
"""


def _extraer_zip_mac(archivo: Path, destino: Path) -> Path:
    """El .zip del Mac, con los permisos de ejecución (zipfile no los pone)."""
    with zipfile.ZipFile(archivo) as z:
        for info in z.infolist():
            ruta = destino / info.filename
            modo = (info.external_attr >> 16) & 0o777
            if info.is_dir():
                ruta.mkdir(parents=True, exist_ok=True)
                continue
            ruta.parent.mkdir(parents=True, exist_ok=True)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:      # enlace
                ruta.symlink_to(z.read(info).decode())
                continue
            ruta.write_bytes(z.read(info))
            if modo:
                ruta.chmod(modo)
    return destino / "Celestia.app" / "Contents" / "Resources" / "Celestia"


def _aplicar(archivo: Path) -> None:
    """Pone en marcha la instalación de lo descargado. En PC, Celestia se
    cerrará y volverá a abrirse sola."""
    if sys.platform == "win32":
        subprocess.Popen([str(archivo), "/S", "/ABRIR"], close_fds=True,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED | NEW_GROUP
        return
    trabajo = Path(tempfile.mkdtemp(prefix="celestia-nueva-"))
    if sys.platform == "darwin":
        nuevo = _extraer_zip_mac(archivo, trabajo)
    else:
        with tarfile.open(archivo) as tar:
            tar.extractall(trabajo, filter="tar")
        nuevo = trabajo / "Celestia"
    if not (nuevo / "lanzador.py").is_file():
        raise RuntimeError("el paquete descargado no trae Celestia")
    reabrir = f'"{ROOT}/python/bin/python3" "{ROOT}/lanzador.py"'
    guion = trabajo / "aplicar.sh"
    # La de ahora y quien la lanzó (el lanzador, con su ventana).
    guion.write_text(_guion_unix(nuevo, [os.getpid(), os.getppid()], reabrir))
    guion.chmod(0o755)
    subprocess.Popen(["/bin/sh", str(guion)], start_new_session=True, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def instalar() -> None:
    """Todo el proceso, pensado para correr en un hilo. Va dejando la fase en
    `_estado` para que el chat la enseñe."""
    if not _candado.acquire(blocking=False):
        return                                    # ya hay una en marcha
    try:
        _estado.update(fase="buscando", error="", progreso=None)
        info = comprobar(forzar=True)
        if not info["hay"]:
            _estado.update(fase="", error="No hay ninguna versión nueva.")
            return
        if es_app_android():
            if INSTALAR_APK is None:
                raise RuntimeError("esta app no sabe instalarse sola")
            _estado.update(fase="android")
            INSTALAR_APK(info["url"], int(info["bytes"] or 0))
            return
        _estado.update(fase="descargando")
        archivo = descargar(info, Path(tempfile.gettempdir()) / "celestia-actualizacion")
        _estado.update(fase="instalando")
        _aplicar(archivo)
        logger.info("Actualización a %s en marcha: Celestia se reinicia", info["nueva"])
    except Exception as e:
        logger.warning("Actualización: falló (%s: %s)", type(e).__name__, e)
        _estado.update(fase="", error=f"No he podido actualizarme: {e}", progreso=None)
    finally:
        _candado.release()


def avisar_si_toca(avisar: Callable[[str, str], None]) -> bool:
    """Si hay versión nueva y aún no se avisó de ESTA, avisa. True si avisó."""
    info = comprobar(forzar=True)
    if not info["hay"]:
        return False
    try:
        ya = AVISADA.read_text(encoding="utf-8").strip()
    except OSError:
        ya = ""
    if ya == info["nueva"]:
        return False
    avisar(f"Hay una versión nueva de Celestia ({info['nueva']}). "
           "Ábreme y pulsa «Actualizar».", "Celestia: actualización")
    try:
        AVISADA.write_text(info["nueva"], encoding="utf-8")
    except OSError:
        pass
    return True


def poner_fase(fase: str, progreso: Optional[int] = None, error: str = "") -> None:
    """Para la app de Android, que lleva su parte de la instalación."""
    _estado.update(fase=fase, progreso=progreso, error=error)


def vigilar(avisar: Callable[[str, str], None], primera_espera_s: float = 90) -> None:
    """Hilo de fondo: mira al poco de arrancar y cada 6 horas."""
    if not archivo_de_este_sistema() or os.environ.get("CELESTIA_EN_TESTS"):
        return
    # Lo que bajó la actualización anterior (80-140 MB) ya no hace falta.
    shutil.rmtree(Path(tempfile.gettempdir()) / "celestia-actualizacion", ignore_errors=True)

    def _bucle():
        time.sleep(primera_espera_s)
        while True:
            try:
                avisar_si_toca(avisar)
            except Exception as e:
                logger.info("Actualización: no pude mirar (%s)", type(e).__name__)
            time.sleep(CADA_S)

    threading.Thread(target=_bucle, daemon=True, name="actualizar").start()
