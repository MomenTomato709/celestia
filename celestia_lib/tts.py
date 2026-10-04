"""Abstracción de motores TTS.

Tres providers intercambiables:
  - EdgeTTSProvider: cloud Microsoft (lo que Celestia tenía siempre). Se usa
    cuando no hay otro configurado — el código que lo invoca sigue viviendo
    en api.py para no romper compatibilidad.
  - PiperProvider: ONNX local en Termux ARM. Sin red. ~1.2x tiempo real.
  - XTTSHttpProvider: cliente HTTP para un servidor Coqui XTTS-v2 remoto
    (cuando el usuario tenga PC con GPU). Calidad casi-humana + clonable.

La selección se hace por Config.TTS_BACKEND y variables de entorno
CELESTIA_TTS_BACKEND, CELESTIA_PIPER_VOZ, CELESTIA_XTTS_URL,
CELESTIA_XTTS_VOZ_REF.

Si el provider configurado no está disponible o falla, devolvemos None y el
caller cae al fallback (típicamente edge-tts en api.py).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Protocol

logger = logging.getLogger("celestia_v1")

VOCES_PIPER_DIR = Path(__file__).resolve().parent.parent / "voces_piper"

# Catálogo de voces Piper descargadas. Sólo medium o high — sin low/x_low.
# Filas: (id, idioma, país, género, calidad).
# id = stem del archivo (.onnx) sin extensión. Para usar una voz desde el
# perfil del usuario o vía env CELESTIA_PIPER_VOZ, pon el id completo
# (p.ej. "es_AR-daniela-high"). El default es "es_ES-sharvard-medium".
PIPER_CATALOGO = [
    # Español
    ("es_AR-daniela-high",       "es", "Argentina",  "F", "high"),
    ("es_ES-sharvard-medium",    "es", "España",     "F", "medium"),
    ("es_ES-davefx-medium",      "es", "España",     "M", "medium"),
    ("es_MX-claude-high",        "es", "México",     "F", "high"),
    ("es_MX-ald-medium",         "es", "México",     "M", "medium"),
    # Inglés
    ("en_US-lessac-high",        "en", "EEUU",       "F", "high"),
    ("en_US-ryan-high",          "en", "EEUU",       "M", "high"),
    ("en_GB-cori-high",          "en", "Reino Unido","F", "high"),
    ("en_GB-alan-medium",        "en", "Reino Unido","M", "medium"),
    # Francés
    ("fr_FR-siwis-medium",       "fr", "Francia",    "F", "medium"),
    ("fr_FR-tom-medium",         "fr", "Francia",    "M", "medium"),
    # Italiano (no hay masculino disponible en medium+)
    ("it_IT-paola-medium",       "it", "Italia",     "F", "medium"),
    # Portugués
    ("pt_BR-cadu-medium",        "pt", "Brasil",     "M", "medium"),
    ("pt_BR-faber-medium",       "pt", "Brasil",     "M", "medium"),
    # Alemán (no hay femenino en medium+ — solo masculino HIGH)
    ("de_DE-thorsten-high",      "de", "Alemania",   "M", "high"),
    # Polaco
    ("pl_PL-bass-high",          "pl", "Polonia",    "M", "high"),
    ("pl_PL-gosia-medium",       "pl", "Polonia",    "F", "medium"),
    # Ruso
    ("ru_RU-irina-medium",       "ru", "Rusia",      "F", "medium"),
    ("ru_RU-dmitri-medium",      "ru", "Rusia",      "M", "medium"),
    # Otros idiomas (una voz por idioma)
    ("zh_CN-huayan-medium",      "zh", "China",      "F", "medium"),
    ("ar_JO-kareem-medium",      "ar", "Jordania",   "M", "medium"),
    ("tr_TR-dfki-medium",        "tr", "Turquía",    "?", "medium"),
]


def piper_buscar_voz(idioma: str = "", genero: str = "", pais: str = "") -> Optional[str]:
    """Devuelve el id de la mejor voz Piper que cumple filtros.

    Prefiere `high` sobre `medium`. Filtros vacíos no aplican.
    Ejemplos:
      buscar_voz(idioma="es", genero="F")          → "es_AR-daniela-high"
      buscar_voz(idioma="es", genero="M")          → "es_ES-davefx-medium"
      buscar_voz(idioma="en", genero="M")          → "en_US-ryan-high"
      buscar_voz(idioma="es", genero="F", pais="México") → "es_MX-claude-high"
    """
    candidatos = [
        (vid, lang, p, g, q) for (vid, lang, p, g, q) in PIPER_CATALOGO
        if (not idioma or lang == idioma)
        and (not genero or g == genero)
        and (not pais or p.lower() == pais.lower())
    ]
    if not candidatos:
        return None
    # Ordenar por calidad: high > medium
    candidatos.sort(key=lambda r: 0 if r[4] == "high" else 1)
    return candidatos[0][0]


class TTSProvider(Protocol):
    """Interfaz común: sintetizar texto → ruta a archivo MP3 (o None si falla)."""
    nombre: str

    def disponible(self) -> bool: ...
    def sintetizar(
        self, texto: str, voz_id: str = "", rate: str = "+0%"
    ) -> Optional[str]: ...


def _convertir_wav_a_mp3(wav_path: str) -> Optional[str]:
    """Convierte WAV → MP3 con ffmpeg. Borra el WAV original al éxito."""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg no instalado — entregando WAV en lugar de MP3")
        return wav_path
    mp3_path = wav_path.rsplit(".", 1)[0] + ".mp3"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", wav_path, "-codec:a", "libmp3lame", "-qscale:a", "5",
             mp3_path],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and os.path.exists(mp3_path):
            try:
                os.unlink(wav_path)
            except Exception:
                pass
            return mp3_path
        logger.warning("ffmpeg falló: %s", r.stderr.decode(errors="replace")[:200])
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timeout convirtiendo %s", wav_path)
        return None
    except Exception as e:
        logger.warning("ffmpeg exception: %s", e)
        return None


class PiperProvider:
    """TTS local via Piper (ONNX). Soporta múltiples voces en VOCES_PIPER_DIR.

    Cada voz es un par (`<nombre>.onnx`, `<nombre>.onnx.json`). El `voz_id`
    es el nombre sin extensión (p.ej. "sharvard", "davefx", "claude_mx").
    """
    nombre = "piper"

    def __init__(
        self,
        voces_dir: Optional[Path] = None,
        voz_default: str = "sharvard",
    ):
        self.voces_dir = Path(voces_dir) if voces_dir else VOCES_PIPER_DIR
        self.voz_default = voz_default

    def disponible(self) -> bool:
        return shutil.which("piper") is not None and self.voces_dir.exists() \
            and any(self.voces_dir.glob("*.onnx"))

    def listar_voces(self) -> List[str]:
        if not self.voces_dir.exists():
            return []
        return sorted(p.stem for p in self.voces_dir.glob("*.onnx"))

    def _ruta_modelo(self, voz_id: str) -> Optional[Path]:
        for candidato in (voz_id, self.voz_default):
            if not candidato:
                continue
            modelo = self.voces_dir / f"{candidato}.onnx"
            if modelo.exists():
                return modelo
        return None

    @staticmethod
    def _rate_to_length_scale(rate: str) -> float:
        """Convierte rate edge-tts ("+10%", "-6%") a length_scale piper.

        length_scale invierte velocidad: <1.0 más rápido, >1.0 más lento.
        Para "+10%" (10% más rápido) → length_scale = 1/1.10 ≈ 0.91.
        """
        if not rate:
            return 1.0
        try:
            limpio = rate.strip().replace("%", "").replace("+", "")
            pct = int(limpio)
        except (ValueError, AttributeError):
            return 1.0
        return 1.0 / (1.0 + pct / 100.0)

    def sintetizar(
        self, texto: str, voz_id: str = "", rate: str = "+0%"
    ) -> Optional[str]:
        if not texto or not texto.strip():
            return None
        modelo = self._ruta_modelo(voz_id)
        if modelo is None:
            logger.warning("Piper: voz '%s' no encontrada en %s",
                           voz_id or self.voz_default, self.voces_dir)
            return None
        length_scale = self._rate_to_length_scale(rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False,
                                         prefix="piper_") as wav_f:
            wav_path = wav_f.name
        try:
            cmd = [
                "piper",
                "--model", str(modelo),
                "--output_file", wav_path,
                "--length_scale", str(length_scale),
            ]
            r = subprocess.run(
                cmd,
                input=texto.encode("utf-8"),
                capture_output=True,
                timeout=60,
            )
            if r.returncode != 0:
                logger.warning("Piper rc=%d: %s",
                               r.returncode,
                               r.stderr.decode(errors="replace")[:200])
                _safe_unlink(wav_path)
                return None
            if not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
                _safe_unlink(wav_path)
                return None
            return _convertir_wav_a_mp3(wav_path) or wav_path
        except subprocess.TimeoutExpired:
            logger.warning("Piper timeout (>60s) — texto demasiado largo?")
            _safe_unlink(wav_path)
            return None
        except Exception as e:
            logger.warning("Piper exception: %s", e)
            _safe_unlink(wav_path)
            return None


class XTTSHttpProvider:
    """Cliente HTTP para servidor Coqui XTTS-v2 remoto.

    Espera servidor compatible con XTTS API:
      POST {url}  body JSON: {"text": ..., "language": "es",
                              "speaker_wav": <ruta>?, "speaker": <nombre>?}
      Response: audio/wav o audio/mpeg bytes.

    Servidores compatibles probados:
      - coqui-ai/TTS server (`tts-server`)
      - daswer123/xtts-api-server
      - daswer123/xtts-webui

    Sin URL configurada, `disponible()` devuelve False y el caller cae al
    fallback (Piper o edge-tts) sin romper nada.
    """
    nombre = "xtts_http"

    def __init__(
        self,
        url: str = "",
        voz_ref: str = "",
        language: str = "es",
        timeout: float = 60.0,
    ):
        self.url = url
        self.voz_ref = voz_ref
        self.language = language
        self.timeout = timeout

    def disponible(self) -> bool:
        return bool(self.url)

    def sintetizar(
        self, texto: str, voz_id: str = "", rate: str = "+0%"
    ) -> Optional[str]:
        if not self.disponible() or not texto or not texto.strip():
            return None
        try:
            import requests
        except ImportError:
            logger.warning("XTTS: requests no instalado")
            return None
        payload = {"text": texto, "language": self.language}
        if self.voz_ref:
            payload["speaker_wav"] = self.voz_ref
        if voz_id:
            payload["speaker"] = voz_id
        try:
            r = requests.post(
                self.url, json=payload, timeout=self.timeout,
                headers={"User-Agent": "Celestia/1.5"},
            )
            if r.status_code != 200:
                logger.warning("XTTS server HTTP %d: %s",
                               r.status_code, r.text[:200])
                return None
            if not r.content:
                logger.warning("XTTS server devolvió body vacío")
                return None
            ct = r.headers.get("Content-Type", "").lower()
            ext = ".mp3" if "mpeg" in ct or "mp3" in ct else ".wav"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False,
                                             prefix="xtts_") as f:
                f.write(r.content)
                ruta = f.name
            if ext == ".wav":
                return _convertir_wav_a_mp3(ruta) or ruta
            return ruta
        except requests.exceptions.ConnectionError:
            logger.warning("XTTS server inalcanzable en %s", self.url)
            return None
        except Exception as e:
            logger.warning("XTTS exception: %s", e)
            return None


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def get_tts_provider(config) -> Optional[TTSProvider]:
    """Devuelve el provider activo según Config / env vars.

    Returns:
      - PiperProvider si backend="piper" y está disponible
      - XTTSHttpProvider si backend="xtts" y URL configurada
      - None si backend="edge_tts" o default → caller usa código edge-tts existente
    """
    backend = (getattr(config, "TTS_BACKEND", "") or "edge_tts").lower()
    if backend == "piper":
        voz = getattr(config, "PIPER_VOZ_DEFAULT", "sharvard")
        p = PiperProvider(voz_default=voz)
        if p.disponible():
            return p
        logger.warning("TTS_BACKEND=piper pero no disponible — fallback edge-tts. "
                       "Voces en %s, binario piper en PATH.", VOCES_PIPER_DIR)
        return None
    if backend == "xtts":
        url = getattr(config, "XTTS_URL", "")
        if not url:
            logger.warning("TTS_BACKEND=xtts pero CELESTIA_XTTS_URL vacío — "
                           "fallback edge-tts.")
            return None
        voz_ref = getattr(config, "XTTS_VOZ_REF", "")
        return XTTSHttpProvider(url=url, voz_ref=voz_ref)
    return None
