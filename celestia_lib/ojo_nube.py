"""Mandar una imagen a un modelo que ve, en la nube (OpenAI-compatible).

Vivía dentro de `jugador.py`, y «ver fotos» con la clave de Gemini (api.py,
_analizar_imagen) la usaba de ahí. El jugador no va en la versión pública
(Enzo, 4 oct 2026: «que lo tengamos pero que nadie lo pueda utilizar, sólo
yo»), así que lo que no es del juego tiene su sitio aparte: sin él, la
versión pública se habría quedado sin ver fotos. `jugador.py` lo importa de
aquí y sigue igual.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional, Tuple

GROQ_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_VISION_URL = ("https://generativelanguage.googleapis.com/v1beta/openai/"
                     "chat/completions")
OR_VISION_URL = "https://openrouter.ai/api/v1/chat/completions"
# Mistral, comprobado como se debe comprobar (S66): se le manda una imagen con
# un número de cuatro cifras que **sólo está ahí** y lo acierta, en 576 ms.
# `mistral-large` no ve; `pixtral-12b` sí. El nombre del modelo importa.
MISTRAL_VISION_URL = "https://api.mistral.ai/v1/chat/completions"
# Dos sitios más donde mirar, los dos con cuenta ya hecha de Enzo y los dos de
# pago por uso (9 sep 2026, contestan 402 hasta que tengan saldo). Cerebras es
# el más rápido que hay —menos de un segundo hasta la primera palabra— y sirve
# **el mismo `qwen-3.8-27b` que ya usa el jugador**, pero de sus tres modelos
# sólo `gemma-4-31b` acepta imágenes; por eso el modelo de aquí no coincide con
# el de Groq. SambaNova va detrás porque su soporte de imagen no está
# comprobado: la cuenta contestó 402 antes de mirar el contenido.
CEREBRAS_VISION_URL = "https://api.cerebras.ai/v1/chat/completions"
SAMBANOVA_VISION_URL = "https://api.sambanova.ai/v1/chat/completions"


def _preguntar_a_ojo(url: str, clave: str, modelo: str, png: bytes,
                     prompt: str, timeout: int = 25,
                     max_tokens: int = 400,
                     extra: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Manda la imagen a un endpoint OpenAI-compatible.

    Devuelve `(respuesta, fallo)`: el fallo va aparte porque quien llama
    necesita distinguir un 429 —el proveedor está saturado y no vale la pena
    volver enseguida— de un error cualquiera.

    🔴 **Los 150 tokens de tope eran el JSON cortado de la S65-66.** En el log
    de la partida de Enzo aparecía catorce veces `la mirada no devolvió JSON:
    '{"accion":"tocar'`, y cada una de esas tiraba la vuelta al OCR de pantalla
    entera: ~60 s en vez de medio segundo. La causa no era el formato ni el
    prompt: era **Gemini**, la reserva, que piensa antes de contestar y se
    gastaba los 150 tokens pensando. Medido con la misma imagen:

        gemini-2.5-flash, 150 tok ....... `finish_reason=length`, JSON a medias
        + reasoning_effort=none, 400 .... 1.408 ms, JSON entero

    O sea que salió más barato y más rápido a la vez. Y ahora el tope tiene que
    ser holgado por otra razón: la respuesta ya no trae un movimiento, trae la
    ráfaga entera.
    """
    import urllib.request
    if not clave:
        return "", "sin clave"
    # El tipo sale de los bytes, no del nombre del parámetro: las franjas
    # numeradas viajan en JPEG (61 KB frente a 400 del mismo recorte en
    # PNG) y anunciarlas como PNG es pedir que el otro lado adivine.
    mime = "image/jpeg" if png[:3] == b"\xff\xd8\xff" else "image/png"
    b64 = base64.b64encode(png).decode()
    cuerpo = json.dumps({
        "model": modelo,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
        "max_tokens": max_tokens, "temperature": 0,
        **(extra or {}),
    }).encode()
    req = urllib.request.Request(
        url, data=cuerpo,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {clave}",
                 # Sin User-Agent propio, Groq contesta 403 (ver pensador_groq).
                 "User-Agent": "Mozilla/5.0 (compatible; CelestiaAI/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return (d["choices"][0]["message"].get("content") or ""), ""
    except Exception as e:
        # El cuerpo del error, no sólo el código. Los proveedores dicen ahí
        # **cuánto** hay que esperar («Please retry in 13.49s»), y con esa
        # cifra el castigo deja de ser un número inventado.
        cuerpo_err = ""
        try:
            cuerpo_err = e.read().decode("utf-8", "replace")[:400]   # type: ignore[attr-defined]
        except Exception:
            pass
        return "", f"{e} {cuerpo_err}".strip()
