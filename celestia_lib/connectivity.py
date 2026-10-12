"""Detección de conectividad a internet.

Extraído del monolito en sesión 15 como primera pieza del refactor. Ver REFACTOR_PLAN.md
para el resto de módulos previstos.
"""
import logging
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger("celestia_v1")

_LOCALES = ("127.0.0.1", "localhost", "::1")


def escucha(url: str, espera: float = 0.3) -> bool:
    """¿Hay alguien escuchando en esa dirección de ESTE aparato?

    En Windows, preguntar a un puerto local cerrado tarda 2 s en dar el «no»
    (reintenta la conexión antes de rendirse; con «localhost», 4 s). Cada
    /estado del chat pagaba esos 2 s por mirar si había un servidor de visión
    local que nadie tiene (portátil, 4 oct 2026). Un servidor local que sí está
    acepta en milésimas, así que 0,3 s de paciencia bastan. Las direcciones que
    no son de este aparato no se miran: se devuelve True y que decida la petición.
    """
    try:
        partes = urllib.parse.urlsplit(url)
        host = partes.hostname or ""
        if host not in _LOCALES:
            return True
        puerto = partes.port or (443 if partes.scheme == "https" else 80)
        with socket.create_connection(("127.0.0.1" if host == "localhost" else host, puerto),
                                      timeout=espera):
            return True
    except OSError:
        return False
    except ValueError:
        return True


class ConnectivityManager:
    """Detecta conectividad a internet y gestiona operaciones pendientes offline."""

    _CHECK_INTERVAL = 30.0
    _TEST_URLS = [
        "http://clients3.google.com/generate_204",
        "http://1.1.1.1",
        "http://8.8.8.8",
    ]

    def __init__(self):
        self._online: Optional[bool] = None
        self._last_check: float = 0.0
        self._pending: deque = deque(maxlen=50)

    def is_online(self, force: bool = False) -> bool:
        now = time.time()
        if (not force
                and self._online is not None
                and now - self._last_check < self._CHECK_INTERVAL):
            return self._online
        prev = self._online
        self._online = self._check_connection()
        self._last_check = now
        if prev is False and self._online is True and self._pending:
            logger.info(
                "Conexión recuperada — %d búsqueda(s) pendiente(s) en cola",
                len(self._pending),
            )
        if prev is True and self._online is False:
            logger.warning("Conexión perdida — modo offline activado")
        return self._online

    def is_online_sin_esperar(self) -> bool:
        """Lo último que se supo; si está viejo, se vuelve a mirar en un hilo.

        Para /estado: con la WiFi floja, mirar en el momento son hasta 9 s
        (tres direcciones a 3 s), y el lanzador usa /estado para saber si ya hay
        una Celestia encendida (portátil, 4 oct 2026: /estado llegó a 15,8 s).
        Antes de la primera comprobación se da por buena la conexión.
        """
        if self._online is not None and time.time() - self._last_check < self._CHECK_INTERVAL:
            return self._online
        if not getattr(self, "_mirando", False):
            self._mirando = True

            def mirar():
                try:
                    self.is_online(force=True)
                finally:
                    self._mirando = False
            threading.Thread(target=mirar, name="celestia-conexion", daemon=True).start()
        return True if self._online is None else self._online

    def _check_connection(self) -> bool:
        for url in self._TEST_URLS:
            try:
                urllib.request.urlopen(url, timeout=3)
                return True
            except Exception:
                pass
        return False

    def queue_search(self, query: str) -> None:
        self._pending.append({"query": query, "ts": time.time()})
        logger.info("Búsqueda encolada (sin conexión): %.50s", query)

    def drain_pending(self) -> List[Dict]:
        items = list(self._pending)
        self._pending.clear()
        return items

    def pending_count(self) -> int:
        return len(self._pending)

    def status_str(self) -> str:
        if self._online is None:
            return "desconocida"
        return "en línea" if self._online else "sin conexión"
