"""Túnel rápido de Cloudflare hacia la puerta pública de Celestia.

La web vive en GitHub Pages con un enlace fijo, pero la dirección del túnel
cambia cada vez que se abre. Así que aquí se abre el túnel y se publica su
dirección en el repositorio, para que la web sepa dónde encontrarla.

Sólo biblioteca estándar.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# La dirección que da la API de Cloudflare (https://api.trycloudflare.com) NO
# vale: esa es la del panel, no la del túnel. Sólo aceptamos el subdominio
# aleatorio que cloudflared imprime al arrancar. El lookahead negativo descarta
# justamente el subdominio literal "api".
_PATRON_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")

_API_GITHUB = "https://api.github.com/repos/{usuario}/{repo}/contents/{ruta}"


def publicador_github(
    usuario: str,
    repo: str,
    token: str,
    ruta: str = "direccion.json",
    abrir: Callable = urllib.request.urlopen,
) -> Callable[[str], bool]:
    """Devuelve `publicar(url)` que escribe la dirección en el repo de GitHub.

    Nunca lanza: devuelve True si el contenido quedó publicado (o ya estaba),
    False si algo falló. El token no aparece jamás en logs ni en mensajes.
    """

    api = _API_GITHUB.format(usuario=usuario, repo=repo, ruta=ruta)

    def _cabeceras() -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "celestia-tunel",
            "Content-Type": "application/json",
        }

    def _sha_actual() -> Optional[str]:
        peticion = urllib.request.Request(api, headers=_cabeceras(), method="GET")
        try:
            with abrir(peticion, timeout=15) as respuesta:
                datos = json.loads(respuesta.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        sha = datos.get("sha")
        return sha if isinstance(sha, str) else None

    def _contenido_actual() -> Optional[dict]:
        peticion = urllib.request.Request(api, headers=_cabeceras(), method="GET")
        try:
            with abrir(peticion, timeout=15) as respuesta:
                datos = json.loads(respuesta.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        contenido = datos.get("content")
        if not isinstance(contenido, str):
            return None
        try:
            crudo = base64.b64decode(contenido).decode("utf-8")
            return json.loads(crudo)
        except (ValueError, UnicodeDecodeError):
            return None

    def publicar(url: str) -> bool:
        try:
            actual = _contenido_actual()
            if isinstance(actual, dict) and actual.get("url") == url:
                return True

            sha = _sha_actual()
            cuerpo = {
                "url": url,
                "actualizado": datetime.now(timezone.utc).isoformat(),
            }
            carga = {
                "message": "Actualizar dirección del túnel de Celestia",
                "content": base64.b64encode(
                    json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
                ).decode("ascii"),
            }
            if sha:
                carga["sha"] = sha

            peticion = urllib.request.Request(
                api,
                data=json.dumps(carga).encode("utf-8"),
                headers=_cabeceras(),
                method="PUT",
            )
            with abrir(peticion, timeout=15) as respuesta:
                respuesta.read()
            return True
        except Exception as error:  # noqa: BLE001 - nunca debe lanzar
            # Ojo: el mensaje del error puede traer la URL con el token dentro.
            # Por eso no se registra el error tal cual, sólo su tipo.
            log.warning("No se pudo publicar la dirección del túnel (%s)", type(error).__name__)
            return False

    return publicar


class Tunel:
    """Un túnel de Cloudflare hacia `puerto_local`, publicado en GitHub."""

    def __init__(
        self,
        puerto_local: int,
        publicar: Callable[[str], bool],
        binario: Optional[str] = None,
        lanzar: Callable = subprocess.Popen,
        espera_s: float = 60.0,
    ) -> None:
        self.puerto_local = puerto_local
        self.publicar = publicar
        self.binario = binario or shutil.which("cloudflared") or str(Path.home() / ".local" / "bin" / "cloudflared")
        self.lanzar = lanzar
        self.espera_s = espera_s
        self.url: Optional[str] = None
        self._proceso = None
        self._cerrado = False
        self._hilo_vigia: Optional[threading.Thread] = None

    def _comando(self) -> list:
        return [
            self.binario,
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://127.0.0.1:{self.puerto_local}",
        ]

    def abrir(self) -> Optional[str]:
        """Arranca el túnel y devuelve su dirección, o None si no aparece."""
        self._cerrado = False
        self._proceso = self.lanzar(
            self._comando(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        encontrada: list = []
        listo = threading.Event()

        def _leer() -> None:
            salida = self._proceso.stdout
            if salida is None:
                listo.set()
                return
            for linea in salida:
                if not encontrada:
                    coincidencia = _PATRON_URL.search(linea)
                    if coincidencia:
                        encontrada.append(coincidencia.group(0))
                        listo.set()
                # Sigue vaciando la tubería para que no se llene.

        hilo_lectura = threading.Thread(target=_leer, daemon=True)
        hilo_lectura.start()

        plazo = time.monotonic() + self.espera_s
        while time.monotonic() < plazo:
            if listo.wait(timeout=0.1):
                break
            if self._proceso.poll() is not None and not encontrada:
                break

        if not encontrada:
            self._matar()
            return None

        self.url = encontrada[0]
        self.publicar(self.url)
        return self.url

    def vivo(self) -> bool:
        return self._proceso is not None and self._proceso.poll() is None

    def _matar(self) -> None:
        proceso = self._proceso
        if proceso is None:
            return
        if proceso.poll() is None:
            proceso.terminate()
            try:
                proceso.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proceso.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._proceso = None

    def cerrar(self) -> None:
        self._cerrado = True
        self._matar()
        self.url = None
        self.publicar("")

    def vigilar(self, cada_s: float = 30.0) -> None:
        """Hilo daemon que reabre el túnel si el proceso muere."""

        def _bucle() -> None:
            while not self._cerrado:
                time.sleep(cada_s)
                if self._cerrado:
                    return
                if not self.vivo():
                    self.abrir()

        self._hilo_vigia = threading.Thread(target=_bucle, daemon=True)
        self._hilo_vigia.start()
