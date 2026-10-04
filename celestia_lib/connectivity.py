"""Detección de conectividad a internet.

Extraído del monolito en sesión 15 como primera pieza del refactor. Ver REFACTOR_PLAN.md
para el resto de módulos previstos.
"""
import logging
import time
import urllib.request
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger("celestia_v1")


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
