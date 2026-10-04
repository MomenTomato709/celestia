"""Oír: pasar una nota de voz a texto con lo que haya en este aparato.

Hasta el 4 oct 2026 había un solo camino: Whisper dentro del aparato
(faster-whisper). En el móvil de Enzo estaba instalado; en el PC y en la app
no, y Celestia se quedaba sorda — peor: «[faster-whisper no instalado]»
llegaba al modelo como si lo hubieras dicho tú. Ahora, por orden:

1. Groq (`whisper-large-v3-turbo`): gratis con su clave, ~1 s, y oye mucho
   mejor que el Whisper pequeño que cabe en un móvil.
2. Whisper en el aparato, si está (el móvil de siempre, o el PC después de
   instalárselo sola: ver `complementos`).
3. Gemini, que también entiende audio, con su clave gratis.

Si no hay ninguno, en un PC se instala el Whisper local en segundo plano y se
dice; en la app de Android, donde no se puede, se pide una clave de Groq.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import complementos
from .paths import ES_ANDROID

logger = logging.getLogger("celestia_v1")

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODELO = os.environ.get("CELESTIA_GROQ_STT", "").strip() or "whisper-large-v3-turbo"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
GEMINI_MODELO = "gemini-2.5-flash"
# Groq gratis acepta 25 MB por fichero; Gemini, 20 MB por petición entera.
TOPE_GROQ = 24 * 1024 * 1024
TOPE_GEMINI = 14 * 1024 * 1024          # en base64 crece un tercio

# Lo que se instala para oír sin ninguna clave (sólo en un PC). PyAV 19 quitó
# `metadata_errors`, que faster-whisper 1.2 todavía usa: con él, cada nota de
# voz fallaba con un TypeError (medido el 4 oct 2026; con 18.1, oye bien).
PAQUETES_LOCAL = ("faster-whisper>=1.0", "av<19")

_MIME = {"webm": "audio/webm", "ogg": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
         "wav": "audio/wav", "flac": "audio/flac"}
_EXTENSION = {"oga": "ogg", "opus": "ogg", "mpeg": "mp3", "mpga": "mp3", "mp4": "m4a",
              "aac": "m4a", "3gp": "m4a"}

# Lo que Whisper «oye» en un audio en silencio: viene de los subtítulos con
# los que se entrenó. Si es TODO lo que ha oído, no se ha oído nada.
_ALUCINACIONES = re.compile(
    r"^\W*(?:subt[ií]tulos?\s+(?:realizados?\s+)?por\s+la\s+comunidad\s+de\s+amara\.org|"
    r"gracias\s+por\s+ver(?:\s+el\s+v[ií]deo)?|thanks?\s+for\s+watching|"
    r"suscr[ií]bete(?:\s+al\s+canal)?|\.+)\W*$", re.IGNORECASE)


def _formato(ruta: str) -> str:
    """El formato de verdad, mirando los primeros bytes: la extensión miente
    (las llamadas llegan como «.audio») y Groq decide por ella."""
    try:
        with open(ruta, "rb") as f:
            cabeza = f.read(16)
    except OSError:
        cabeza = b""
    if cabeza.startswith(b"OggS"):
        return "ogg"
    if cabeza.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if cabeza.startswith(b"RIFF") and cabeza[8:12] == b"WAVE":
        return "wav"
    if cabeza.startswith(b"fLaC"):
        return "flac"
    if cabeza[4:8] == b"ftyp":
        return "m4a"
    if cabeza.startswith(b"ID3") or cabeza[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    ext = Path(ruta).suffix.lower().lstrip(".")
    ext = _EXTENSION.get(ext, ext)
    return ext if ext in _MIME else "webm"


def _limpiar(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto or "").strip()
    return "" if _ALUCINACIONES.match(texto) else texto


# ── 1. Groq ────────────────────────────────────────────────────────────────
def _groq(ruta: str, idioma: Optional[str], clave: str) -> str:
    import requests
    datos = Path(ruta).read_bytes()
    if len(datos) > TOPE_GROQ:
        raise ValueError(f"audio de {len(datos) // 1_000_000} MB: demasiado para Groq")
    fmt = _formato(ruta)
    campos = {"model": GROQ_MODELO, "response_format": "json", "temperature": "0"}
    if idioma:
        campos["language"] = idioma
    r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {clave}"},
                      files={"file": (f"voz.{fmt}", datos, _MIME[fmt])},
                      data=campos, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("text") or ""


# ── 2. En el aparato ───────────────────────────────────────────────────────
_LOCAL: dict = {"modelo": None}
_LOCAL_LOCK = threading.Lock()


def _nombre_modelo_local() -> str:
    # En el móvil, el pequeño (lo que da la CPU); en un PC, «base», que oye
    # bastante mejor el español y sigue tardando un par de segundos.
    return (os.environ.get("CELESTIA_WHISPER_MODELO", "").strip()
            or ("tiny" if ES_ANDROID else "base"))


def _modelo_local():
    """El Whisper del aparato, cargado una vez. None si no está instalado.
    La primera vez se baja el modelo (tiny 75 MB, base 145 MB)."""
    if _LOCAL["modelo"] is not None:
        return _LOCAL["modelo"]
    if not complementos.esta("faster_whisper"):
        return None
    with _LOCAL_LOCK:
        if _LOCAL["modelo"] is None:
            # Windows sin modo desarrollador no deja crear enlaces: la caché
            # de Hugging Face funciona igual, pero llenaba el log de avisos.
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from faster_whisper import WhisperModel
            nombre = _nombre_modelo_local()
            logger.info("Cargando Whisper %s…", nombre)
            _LOCAL["modelo"] = WhisperModel(nombre, device="cpu", compute_type="int8")
    return _LOCAL["modelo"]


def _local(ruta: str, idioma: Optional[str]) -> Optional[str]:
    modelo = _modelo_local()
    if modelo is None:
        return None
    # El pequeño se equivoca de idioma con frases cortas: en el móvil, español
    # salvo que se haya pedido otro (lo de siempre).
    if not idioma and _nombre_modelo_local() == "tiny":
        idioma = "es"
    segmentos, _info = modelo.transcribe(ruta, language=idioma, beam_size=1)
    return " ".join(s.text.strip() for s in segmentos)


# ── 3. Gemini ──────────────────────────────────────────────────────────────
def _gemini(ruta: str, idioma: Optional[str], clave: str) -> str:
    import requests
    datos = Path(ruta).read_bytes()
    if len(datos) > TOPE_GEMINI:
        raise ValueError(f"audio de {len(datos) // 1_000_000} MB: demasiado para Gemini")
    orden = ("Transcribe literalmente lo que se dice en este audio, en el idioma en "
             "que se habla. Devuelve SOLO la transcripción, sin comillas, sin notas "
             "y sin describir sonidos. Si no se entiende ninguna palabra, no devuelvas nada.")
    cuerpo = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": _MIME[_formato(ruta)],
                             "data": base64.b64encode(datos).decode()}},
            {"text": orden}]}],
        # Sin pensar: transcribir no lo necesita y el pensamiento gasta del tope.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 4096,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    r = requests.post(GEMINI_URL.format(modelo=GEMINI_MODELO),
                      headers={"x-goog-api-key": clave}, json=cuerpo, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    partes = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in partes)


# ── El orden ───────────────────────────────────────────────────────────────
def _clave(var: str) -> str:
    return os.environ.get(var, "").strip()


def disponible() -> bool:
    """¿Puede oír ahora mismo, sin instalar nada?"""
    return bool(_clave("GROQ_API_KEY") or _clave("GEMINI_API_KEY")
                or complementos.esta("faster_whisper"))


def _preparar_local(ok: bool) -> None:
    """Tras instalar faster-whisper: bajar ya el modelo, para que la próxima
    nota de voz no espere a la descarga, y avisar."""
    if not ok:
        return
    try:
        _modelo_local()
    except Exception as e:
        logger.warning("Whisper instalado, pero el modelo no carga: %s", e)
        return
    try:
        from . import push
        push.avisar("Ya puedo oír tus notas de voz.", titulo="Celestia")
    except Exception as e:
        logger.debug("Aviso de oído listo: %s", e)


def transcribir(ruta: str, idioma: Optional[str] = None,
                solo_local: bool = False) -> Tuple[str, str]:
    """(texto, motivo). Con texto, `motivo` dice quién lo ha oído. Sin texto:
    «vacio» (se oyó, pero no se dijo nada), «instalando» (el oído se está
    bajando), «fallo» (no se pudo bajar) o «sin_oido» (no hay con qué).

    `solo_local`: para la palabra de activación, que manda un trozo cada dos
    segundos: eso por la nube agotaría la cuota en una mañana."""
    pasos: List[Tuple[str, Callable[[], Optional[str]]]] = []
    if not solo_local and _clave("GROQ_API_KEY"):
        pasos.append(("groq", lambda: _groq(ruta, idioma, _clave("GROQ_API_KEY"))))
    pasos.append(("local", lambda: _local(ruta, idioma)))
    if not solo_local and _clave("GEMINI_API_KEY"):
        pasos.append(("gemini", lambda: _gemini(ruta, idioma, _clave("GEMINI_API_KEY"))))

    alguien_oyo = False
    for nombre, paso in pasos:
        try:
            texto = paso()
        except Exception as e:
            logger.info("Oído %s falló: %s", nombre, str(e)[:200])
            continue
        if texto is None:                    # ese camino no existe aquí
            continue
        alguien_oyo = True
        texto = _limpiar(texto)
        if texto:
            return texto, nombre
    if alguien_oyo:
        return "", "vacio"
    if solo_local:
        return "", "sin_oido"
    estado = complementos.instalar("oido", PAQUETES_LOCAL, "faster_whisper",
                                   al_terminar=_preparar_local)
    if estado == "listo":                    # instalado, pero no ha cargado
        return "", "fallo"
    return "", estado if estado in ("instalando", "fallo") else "sin_oido"


_PIDE_GROQ = ("pégame una clave gratis de Groq (se saca en un minuto en "
              "console.groq.com/keys) y te oiré al momento")


def explicar(motivo: str) -> str:
    """Qué contestar cuando no se ha podido oír."""
    if motivo == "instalando":
        return ("Estoy aprendiendo a oír: me estoy bajando el oído (unos 300 MB) y "
                "en unos minutos podré escuchar tus notas de voz. Si no quieres "
                f"esperar, {_PIDE_GROQ}.")
    if motivo == "fallo":
        return ("No he podido instalarme el oído (¿hay internet?). Lo vuelvo a "
                f"intentar dentro de un rato; si no quieres esperar, {_PIDE_GROQ}.")
    if motivo == "sin_oido":
        return (f"Para oír notas de voz necesito una clave: {_PIDE_GROQ}. Mientras "
                "tanto puedes dictarme con el micrófono del teclado.")
    return "No pude entender el audio."


def estado() -> dict:
    """Para la sección «Sentidos» de Ajustes."""
    if _clave("GROQ_API_KEY"):
        return {"ok": True, "como": "Groq"}
    if complementos.esta("faster_whisper"):
        return {"ok": True, "como": "en este aparato"}
    if _clave("GEMINI_API_KEY"):
        return {"ok": True, "como": "Gemini"}
    instalando = complementos.estado("oido") == "instalando"
    return {"ok": False, "instalando": instalando,
            "se_instala_solo": complementos.se_puede()}
