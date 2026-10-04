"""Embedder semántico con `sentence-transformers` + fallback Jaccard + cache disco.

Extraído del monolito en sesión 15. Detecta sus propias deps; si
`sentence-transformers` o `numpy` no están instalados, opera en modo lexical.

Cache: vectores se persisten por hash(texto) en `memoria/embed_cache.npz`.
Al arrancar carga el cache; al hacer encode, escribe en RAM + disco asíncrono.
Esto evita recomputar embeddings de queries repetidas o de la hidratación
de `short_mem` al arrancar.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("celestia_v1")

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]

SentenceTransformer = None  # type: ignore[assignment,misc]
_HAS_ST: Optional[bool] = None


def _ensure_st_loaded() -> bool:
    """Carga perezosa de sentence-transformers (su import tarda ~45s).

    Llamado solo cuando se instancia un Embedder real. Si la dep no está
    instalada o el import falla, queda en modo lexical (Jaccard) sin colgar
    el resto del sistema ni los tests que no usan embeddings reales.
    """
    global SentenceTransformer, _HAS_ST
    if _HAS_ST is not None:
        return _HAS_ST
    try:
        from sentence_transformers import SentenceTransformer as _ST
        SentenceTransformer = _ST  # type: ignore[assignment,misc]
        _HAS_ST = True
    except ImportError:
        _HAS_ST = False
    return _HAS_ST


class Embedder:
    """Wrapper sobre SentenceTransformer con fallback Jaccard + cache disco.

    Atributos
    ---------
    model : SentenceTransformer | None
        Modelo cargado, o None si las deps no están instaladas.
    dim : int
        Dimensión del vector. 384 por defecto (MiniLM-L6) si no se pudo cargar.

    Cache
    -----
    Vectores se cachean en memoria por `md5(texto)[:16]`. Al instanciar se
    carga el cache disco si existe. Cada N vectores nuevos se persiste
    (write-behind asíncrono). Stats expuestas en `cache_hits`/`cache_misses`.
    """

    # Cuántos vectores acumulamos en RAM antes de persistir a disco
    _CACHE_FLUSH_EVERY = 50

    def __init__(self, model_name: str, cache_dir: str, device: str,
                  cache_disco_path: Optional[str] = None):
        self.model: Optional["SentenceTransformer"] = None
        self.dim = 384
        self._cache: Dict[str, "np.ndarray"] = {}
        self._cache_pendiente_flush = 0
        self._cache_lock = threading.Lock()
        self._cache_disco_path = Path(cache_disco_path) if cache_disco_path else None
        self.cache_hits = 0
        self.cache_misses = 0

        if not _ensure_st_loaded():
            logger.info("sentence-transformers no disponible — similitud Jaccard")
            return
        try:
            self.model = SentenceTransformer(model_name, cache_folder=cache_dir, device=device)
            try:
                self.dim = self.model.get_embedding_dimension()
            except AttributeError:
                self.dim = self.model.get_sentence_embedding_dimension()
            logger.info("Embedder semántico cargado: dim=%d en %s", self.dim, device)
        except Exception as e:
            logger.warning("Embedder no disponible: %s", e)

        # Cargar cache disco si existe
        if self._cache_disco_path and _HAS_NUMPY and self._cache_disco_path.exists():
            try:
                data = np.load(self._cache_disco_path, allow_pickle=False)
                for k in data.files:
                    self._cache[k] = data[k]
                logger.info("Cache de embeddings cargado: %d vectores desde %s",
                              len(self._cache), self._cache_disco_path.name)
            except Exception as e:
                logger.warning("No pude cargar cache de embeddings: %s", e)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

    def encode(self, text: str):
        """Vector float32 del texto. Cachea en memoria + disco. None si sin modelo."""
        if self.model is None or not _HAS_NUMPY or not text:
            return None
        h = self._hash(text)
        with self._cache_lock:
            v = self._cache.get(h)
        if v is not None:
            self.cache_hits += 1
            return v
        self.cache_misses += 1
        try:
            v = self.model.encode(text, convert_to_numpy=True).astype("float32")
        except Exception:
            return None
        with self._cache_lock:
            self._cache[h] = v
            self._cache_pendiente_flush += 1
            necesita_flush = self._cache_pendiente_flush >= self._CACHE_FLUSH_EVERY
        if necesita_flush and self._cache_disco_path:
            # Write-behind asíncrono — no bloquear el caller
            threading.Thread(target=self._flush_cache, daemon=True,
                              name="embed-cache-flush").start()
        return v

    def _flush_cache(self) -> None:
        """Persiste el cache a disco en `.npz`. Idempotente y thread-safe."""
        if not self._cache_disco_path or not _HAS_NUMPY:
            return
        try:
            with self._cache_lock:
                snapshot = dict(self._cache)
                self._cache_pendiente_flush = 0
            if not snapshot:
                return
            self._cache_disco_path.parent.mkdir(parents=True, exist_ok=True)
            # OJO: np.savez_compressed añade `.npz` automáticamente si la ruta
            # no termina en `.npz`. Si pasamos `foo.npz.tmp` escribe a
            # `foo.npz.tmp.npz` y luego tmp.replace falla con Errno 2.
            tmp_base = self._cache_disco_path.with_suffix(".tmp")
            np.savez_compressed(tmp_base, **snapshot)
            tmp_real = tmp_base.with_suffix(tmp_base.suffix + ".npz")
            # numpy puede crear tmp_real o tmp_base según versión; cubrimos ambos
            candidato = tmp_real if tmp_real.exists() else tmp_base
            if not candidato.exists():
                logger.debug("Flush cache: numpy no creó el archivo temporal")
                return
            candidato.replace(self._cache_disco_path)
            logger.debug("Cache embeddings persistido: %d vectores", len(snapshot))
        except Exception as e:
            logger.warning("Flush cache embeddings falló: %s", e)

    def hit_rate(self) -> float:
        """% de cache hits sobre el total de encodes (0-100)."""
        total = self.cache_hits + self.cache_misses
        if total == 0:
            return 0.0
        return 100.0 * self.cache_hits / total

    def similarity(self, a: str, b: str) -> float:
        """Similitud cosena en [0,1]. Fallback Jaccard lexical si no hay modelo."""
        va = self.encode(a)
        vb = self.encode(b)
        if va is not None and vb is not None:
            try:
                num = float(np.dot(va, vb))
                denom = float(np.linalg.norm(va) * np.linalg.norm(vb)) + 1e-9
                return max(0.0, min(1.0, num / denom))
            except Exception:
                pass
        return self._jaccard(a, b)

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        """Similitud Jaccard de tokens (fallback sin embeddings)."""
        if not a or not b:
            return 0.0
        sa = set(a.lower().split())
        sb = set(b.lower().split())
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)
