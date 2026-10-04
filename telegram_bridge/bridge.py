#!/usr/bin/env python3
"""Telegram bridge para Celestia.

Conecta Telegram → Celestia API en localhost:8765. Mismo modelo que
`whatsapp_bridge/bridge.js` pero en Python con python-telegram-bot v21+.

Configurar:
  1. Hablar con @BotFather en Telegram → /newbot → guardar TOKEN
  2. Añadir `TELEGRAM_BOT_TOKEN=<token>` en /root/Celestia/.env
  3. Ejecutar: python3 telegram_bridge/bridge.py
  4. En Telegram, abrir chat con tu bot y mandar /start

Comparativa con bridge.js (WhatsApp):
  - Igual: usa `/mensaje` para procesar texto
  - Telegram permite bot oficial (no requiere QR ni emparejamiento web)
  - Audio/voz: usa /audio igual que WhatsApp
  - Múltiples usuarios: cada chat_id se atiende independientemente
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
import json
from pathlib import Path
from typing import Optional

logger = logging.getLogger("celestia-telegram")
logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CELESTIA_API = os.environ.get("CELESTIA_API_URL", "http://127.0.0.1:8765")


def _leer_env_file(path: str) -> dict:
    """Lee un .env sin python-dotenv (sin deps)."""
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


# Cargar tokens del .env del proyecto si no están en el entorno
_env = _leer_env_file("/root/Celestia/.env")
_env.update(_leer_env_file("/sdcard/Celestia/.env"))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _env.get("TELEGRAM_BOT_TOKEN", "")
CELESTIA_TOKEN = os.environ.get("CELESTIA_API_TOKEN") or _env.get("CELESTIA_API_TOKEN", "")


def _api_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CELESTIA_TOKEN:
        h["X-Celestia-Token"] = CELESTIA_TOKEN
    return h


def llamar_api(endpoint: str, payload: dict, timeout: int = 120) -> dict:
    """POST al endpoint de Celestia. Devuelve JSON o {error}."""
    req = urllib.request.Request(
        CELESTIA_API + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=_api_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        logger.warning("API %s → HTTP %s: %.200s", endpoint, e.code, body)
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        logger.warning("API %s falló: %s", endpoint, e)
        return {"error": str(e)}


async def manejar_texto(update, context):
    """Maneja /start, /help y mensajes de texto normales."""
    texto = update.message.text or ""
    if texto.startswith("/start"):
        await update.message.reply_text(
            "✨ Soy Celestia. Mándame texto o audio y te respondo.\n"
            "Comandos: /help para ver más."
        )
        return
    if texto.startswith("/help"):
        await update.message.reply_text(
            "Lo que puedes hacer:\n"
            "• Texto libre — conversación normal\n"
            "• Audio/voz — transcribo con Whisper y respondo\n"
            "• Imagen — analizo con visión\n"
            "• /estado — health check\n"
        )
        return
    if texto.startswith("/estado"):
        try:
            with urllib.request.urlopen(CELESTIA_API + "/estado", timeout=5) as r:
                data = json.loads(r.read())
            await update.message.reply_text(f"```\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```",
                                              parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Celestia no responde: {e}")
        return

    # Mensaje normal → /mensaje
    # Quién escribe: el servidor decide si le atiende. Este puente atendía a
    # cualquiera que diera con el bot — y al otro lado está la memoria de su
    # dueño y sus herramientas.
    quien = str(update.effective_chat.id)
    res = llamar_api("/mensaje", {"texto": texto, "canal": "telegram",
                                  "remitente": quien})
    if res.get("error"):
        await update.message.reply_text(f"⚠ {res['error']}")
        return
    respuesta_texto = res.get("texto", "")
    if respuesta_texto:
        await update.message.reply_text(respuesta_texto)
    try:
        await enviar_audio(update, res)
    except Exception as e:
        logger.warning("No pude enviar voice: %s", e)


async def enviar_audio(update, res: dict) -> None:
    """Su voz. OGG/Opus es una nota de voz de Telegram; en un PC sin ffmpeg
    llega MP3 (audio_tipo «audio/mpeg») y va como audio normal: como nota de
    voz Telegram no lo reproduciría."""
    audio_b64 = res.get("audio_b64")
    if not audio_b64:
        return
    es_mp3 = "mpeg" in (res.get("audio_tipo") or "") or "mp3" in (res.get("audio_tipo") or "")
    with tempfile.NamedTemporaryFile(suffix=".mp3" if es_mp3 else ".ogg", delete=False) as f:
        f.write(base64.b64decode(audio_b64))
        ruta = f.name
    try:
        with open(ruta, "rb") as audio:
            if es_mp3:
                await update.message.reply_audio(audio=audio, title="Celestia")
            else:
                await update.message.reply_voice(voice=audio)
    finally:
        os.unlink(ruta)


async def manejar_voz(update, context):
    """Descarga audio del usuario, lo manda a /audio, devuelve la respuesta."""
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    try:
        file = await context.bot.get_file(voice.file_id)
        ruta_local = tempfile.mktemp(suffix=".ogg")
        await file.download_to_drive(ruta_local)
        res = llamar_api("/audio", {"ruta": ruta_local, "canal": "telegram",
                                    "remitente": str(update.effective_chat.id)})
        texto = res.get("texto", "")
        if texto:
            await update.message.reply_text(texto)
        await enviar_audio(update, res)
    except Exception as e:
        logger.exception("Error procesando audio: %s", e)
        await update.message.reply_text(f"⚠ No pude procesar el audio: {e}")


async def manejar_foto(update, context):
    """Descarga la imagen y la manda a /mensaje con imagen_b64."""
    photo = (update.message.photo or [None])[-1]
    caption = update.message.caption or "¿Qué hay en esta imagen?"
    if not photo:
        return
    try:
        file = await context.bot.get_file(photo.file_id)
        buf_io = await file.download_as_bytearray()
        img_b64 = base64.b64encode(bytes(buf_io)).decode()
        res = llamar_api("/mensaje", {"texto": caption, "imagen_b64": img_b64,
                                      "remitente": str(update.effective_chat.id),
                                        "canal": "telegram"})
        await update.message.reply_text(res.get("texto", "(sin descripción)"))
    except Exception as e:
        logger.exception("Error procesando foto: %s", e)
        await update.message.reply_text(f"⚠ Error: {e}")


def main():
    if not TG_TOKEN:
        print("✗ Falta TELEGRAM_BOT_TOKEN en .env o entorno. Saliendo.")
        sys.exit(1)
    try:
        from telegram.ext import (
            ApplicationBuilder, MessageHandler, CommandHandler, filters,
        )
    except ImportError:
        print("✗ python-telegram-bot no instalado. Instala: pip install 'python-telegram-bot>=21.0'")
        sys.exit(1)

    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help", "estado"], manejar_texto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, manejar_voz))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_foto))

    logger.info("Telegram bridge listo — esperando mensajes")
    app.run_polling()


if __name__ == "__main__":
    main()
