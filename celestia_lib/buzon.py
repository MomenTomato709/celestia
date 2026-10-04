"""El buzón: lo que Celestia manda POR SU CUENTA al chat web, guardado.

24 sep 2026: Celestia modeló su primera criatura y mandó cada versión con foto
al chat, pero Enzo no vio nada. La bandeja (bandeja.py) entrega cada mensaje a
la primera pestaña que pregunta y lo olvida: lo recogió una pestaña abierta de
fondo y al recargar ya no estaba. Lo que Celestia dice sin que le pregunten
tiene que quedarse, como un chat más.

Va en su propia tabla y no en `conversations`: esos mensajes no son una charla
con Enzo, y mezclarlos ahí los metería en su memoria y en sus ejemplos. En la
lista de chats sale como uno fijo, «📬 Celestia te escribe». Las fotos se
guardan como ficheros al lado de la base de datos, no dentro.
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("celestia_v1")

HILO = "buzon"
TITULO = "📬 Celestia te escribe"
MAX_MENSAJES = 500


def _en_pruebas() -> bool:
    return os.environ.get("CELESTIA_EN_TESTS", "").strip() == "1" and not os.environ.get("CELESTIA_DB")


def _db_path() -> Path:
    from .config import Config
    return Path(Config.DB_PATH)


def _carpeta() -> Path:
    return _db_path().parent / "buzon"


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS buzon ("
                "id TEXT PRIMARY KEY, ts REAL NOT NULL, texto TEXT NOT NULL, imagen TEXT)")
    return con


def guardar(texto: str, imagen_b64: str = "") -> None:
    """Guarda un mensaje de Celestia. Nunca lanza: guardar no puede impedir entregar."""
    if _en_pruebas():
        return
    try:
        ts = time.time()
        ident = f"{int(ts * 1000)}"
        imagen = ""
        if imagen_b64:
            carpeta = _carpeta()
            carpeta.mkdir(parents=True, exist_ok=True)
            fichero = carpeta / f"{ident}.jpg"
            fichero.write_bytes(base64.b64decode(imagen_b64))
            imagen = fichero.name
        con = _conectar()
        with con:
            con.execute("INSERT OR REPLACE INTO buzon (id, ts, texto, imagen) VALUES (?, ?, ?, ?)",
                        (ident, ts, texto or "", imagen))
            # Un buzón que no crece sin fin en un móvil.
            viejos = con.execute("SELECT id, imagen FROM buzon ORDER BY ts DESC LIMIT -1 OFFSET ?",
                                 (MAX_MENSAJES,)).fetchall()
            for vid, vimg in viejos:
                con.execute("DELETE FROM buzon WHERE id=?", (vid,))
                if vimg:
                    (_carpeta() / vimg).unlink(missing_ok=True)
        con.close()
    except Exception as e:
        logger.warning("Buzón: no pude guardar el mensaje (%s)", type(e).__name__)


def resumen() -> Dict[str, Any] | None:
    """Para la lista de chats: None si el buzón está vacío."""
    try:
        con = _conectar()
        n, ultimo, primero = con.execute("SELECT COUNT(*), MAX(ts), MIN(ts) FROM buzon").fetchone()
        con.close()
    except Exception:
        return None
    if not n:
        return None
    return {"id": HILO, "titulo": TITULO, "mensajes": n, "ultimo_ts": ultimo, "creado_ts": primero}


def mensajes(limite: int = 400) -> List[Dict[str, Any]]:
    """Los mensajes para pintar el chat, con la foto dentro si la hay."""
    try:
        con = _conectar()
        filas = con.execute("SELECT ts, texto, imagen FROM buzon ORDER BY ts ASC LIMIT ?",
                            (limite,)).fetchall()
        con.close()
    except Exception:
        return []
    salida = []
    for ts, texto, imagen in filas:
        m: Dict[str, Any] = {"quien": "ella", "texto": texto, "ts": ts}
        if imagen:
            fichero = _carpeta() / imagen
            if fichero.exists():
                m["imagen_b64"] = base64.b64encode(fichero.read_bytes()).decode()
        salida.append(m)
    return salida


def ultimos_textos(n: int = 3) -> List[str]:
    """Lo último que dejó en el buzón, del más viejo al más nuevo (sin fotos)."""
    try:
        con = _conectar()
        filas = con.execute("SELECT texto FROM buzon ORDER BY ts DESC LIMIT ?",
                            (n,)).fetchall()
        con.close()
    except Exception:
        return []
    return [t for (t,) in reversed(filas) if t]


def vaciar() -> None:
    try:
        con = _conectar()
        with con:
            imagenes = [r[0] for r in con.execute("SELECT imagen FROM buzon WHERE imagen != ''")]
            con.execute("DELETE FROM buzon")
        con.close()
        for img in imagenes:
            (_carpeta() / img).unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Buzón: no pude vaciarlo (%s)", type(e).__name__)
