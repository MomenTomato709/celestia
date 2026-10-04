# Telegram bridge

Conecta Telegram → Celestia. Equivalente al `whatsapp_bridge/` pero en Python.

## Setup

1. Crea un bot con @BotFather en Telegram:
   - Comanda `/newbot`
   - Elige nombre + username
   - Guarda el token
2. Añade en `/root/Celestia/.env`:
   ```
   TELEGRAM_BOT_TOKEN=tu-token-aqui
   ```
3. Instala dependencia:
   ```bash
   pip install 'python-telegram-bot>=21.0'
   ```
4. Arranca Celestia (`make run` o widget) y luego el bridge:
   ```bash
   python3 telegram_bridge/bridge.py
   ```
5. En Telegram, busca tu bot y manda `/start`.

## Qué hace

| Input del usuario | Acción |
|---|---|
| Texto normal | POST `/mensaje` → responde texto + voz (si TTS activo) |
| Audio/voice | Descarga, POST `/audio` → respuesta |
| Foto + caption | POST `/mensaje` con `imagen_b64` → análisis de imagen |
| `/start` `/help` | Mensaje guía |
| `/estado` | JSON del health check |

## Multi-tenant

Cada `chat_id` de Telegram puede ser un usuario distinto en el futuro. Hoy
todos comparten el mismo perfil (single-tenant del Celestia local). Para
multi-tenant real:

1. Asociar `chat_id` → perfil propio en `memoria/perfiles/<chat_id>.json`
2. Pasar el `chat_id` a `/mensaje` como header `X-User-Id`
3. `WhatsAppAPI` cargaría el perfil correcto según ese header

Ver `ROADMAP.md` fase 4 (multi-canal + multi-usuario).
