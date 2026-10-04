#!/usr/bin/env python3
"""Discord bridge para Celestia.

Conecta Discord → Celestia API en localhost:8765. Mismo contrato que
`telegram_bridge/bridge.py` y `whatsapp_bridge/bridge.js`: este proceso no sabe
nada del modelo ni de la memoria, solo traduce mensajes de Discord a llamadas
HTTP contra `/mensaje` y `/audio`.

Configurar (una sola vez):
  1. https://discord.com/developers/applications → New Application
  2. Pestaña «Bot» → Add Bot → Reset Token → copiar el token
  3. En esa misma pestaña, activar el interruptor MESSAGE CONTENT INTENT
     (sin él, el bot recibe los mensajes vacíos y no puede leerte)
  4. Añadir a /root/Celestia/.env:
         DISCORD_BOT_TOKEN=tu-token-aqui
     Opcional, para dar acceso a MÁS gente además de a ti:
         DISCORD_USUARIOS=123456789012345678,987654321098765432
     Si no la pones, Celestia solo responde al dueño de la aplicación
     (tú): nunca contesta a un desconocido del servidor.
  5. Instalar la librería:  pip install discord.py
  6. Invitar el bot a tu servidor: pestaña «OAuth2 → URL Generator»,
     scopes `bot`, permisos «Send Messages» + «Read Message History»,
     y abrir la URL generada. También funciona por mensaje directo al bot.

Arranque: lo normal es no arrancarlo a mano, sino decírselo a Celestia
(«hablemos por Discord») o usar `python3 hablar.py --canal discord`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

logger = logging.getLogger("celestia-discord")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CELESTIA_API = os.environ.get("CELESTIA_API_URL", "http://127.0.0.1:8765")

# Discord corta los mensajes a 2000 caracteres; troceamos por debajo del límite.
LIMITE_DISCORD = 1900


def _leer_env_file(path: str) -> dict:
    """Lee un .env sin dependencias externas."""
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:      # no existe o no se puede leer (en GitHub, /root)
        pass
    return out


# El .env de al lado del código, no una ruta escrita a mano: cada Celestia vive
# en el aparato de su dueño y no todas están en /root/Celestia. Se conservan las
# dos de siempre por detrás, para las instalaciones que ya existen.
RUTAS_ENV = [str(Path(__file__).resolve().parent.parent / ".env"),
             "/root/Celestia/.env", "/sdcard/Celestia/.env"]


def _cargar_env() -> dict:
    fuera = {}
    for ruta in RUTAS_ENV:
        fuera.update(_leer_env_file(ruta))
    return fuera


_env = _cargar_env()
DISCORD_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN") or _env.get("DISCORD_BOT_TOKEN", "")
CELESTIA_TOKEN = os.environ.get("CELESTIA_API_TOKEN") or _env.get("CELESTIA_API_TOKEN", "")
# Lista blanca de IDs de usuario de Discord. Si se deja vacía NO se abre el bot
# a todo el mundo: en `on_ready` se rellena con el dueño de la aplicación.
# Dos fuentes para lo mismo: la de siempre (DISCORD_USUARIOS) y la común a
# todos los canales (CELESTIA_PERMITIDOS_DISCORD), que es donde apunta Celestia
# cuando te empareja hablando. Sin unirlas, ella te daría por autorizado y este
# puente te seguiría rechazando antes de que el mensaje llegara a ella.
def _lista(clave: str) -> set:
    crudo = os.environ.get(clave) or _env.get(clave, "")
    return {u.strip() for u in crudo.split(",") if u.strip()}


USUARIOS_OK    = _lista("DISCORD_USUARIOS") | _lista("CELESTIA_PERMITIDOS_DISCORD")

# Quién puede hablar con Celestia por Discord, ya resuelto en tiempo de ejecución.
# Empieza siendo la lista blanca del .env y, si esa está vacía, `on_ready` le
# añade al dueño del bot. Un conjunto VACÍO significa «no responder a nadie».
#
# Cerrado por defecto a propósito: un bot de Discord está en servidores con
# gente, y al otro lado de este puente está el asistente personal del usuario
# —su memoria, sus recordatorios, su móvil—. Abrirlo a cualquiera que sepa
# mencionarlo sería regalar esa puerta.
AUTORIZADOS: set = set(USUARIOS_OK)


def puede_hablar(user_id) -> bool:
    """¿Este ID de Discord tiene permiso para hablar con Celestia?

    Si no lo conoce, relee el `.env` antes de decir que no: Celestia apunta ahí
    a quien empareja mientras te guía, y este proceso ya estaba arrancado con la
    lista de antes. Sin esta relectura habría que reiniciar el puente a mano
    justo después de que ella dijera «ya te tengo apuntado».
    """
    ident = str(user_id)
    if ident in AUTORIZADOS:
        return True
    _env.update(_cargar_env())
    nuevos = _lista("DISCORD_USUARIOS") | _lista("CELESTIA_PERMITIDOS_DISCORD")
    if nuevos - AUTORIZADOS:
        AUTORIZADOS.update(nuevos)
    return ident in AUTORIZADOS


# Banderas de la aplicación que indican que MESSAGE CONTENT está concedido,
# en su versión completa o en la limitada de los bots pequeños.
_FLAG_MESSAGE_CONTENT = (1 << 18) | (1 << 19)


def intent_de_contenido_concedido(token: str) -> bool:
    """¿Tiene la aplicación activado MESSAGE CONTENT INTENT en el portal?

    Se pregunta por REST antes de conectar en vez de intentarlo y ver si la
    pasarela nos echa: pedir un intent no concedido no da un error limpio, sino
    una sesión que se invalida una y otra vez, y eso desde fuera parece que
    Celestia está colgada.

    Ante la duda (red caída, respuesta rara) se responde True: es mejor
    intentarlo con todo y fallar con el mensaje explicativo que arrancar
    capado sin necesidad.
    """
    req = urllib.request.Request(
        "https://discord.com/api/v10/applications/@me",
        # Sin un User-Agent con la forma que Discord espera, Cloudflare
        # responde 403 y nos quedamos sin saber qué intents hay concedidos.
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "DiscordBot (https://celestia.local, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            flags = json.loads(r.read().decode("utf-8")).get("flags", 0)
        return bool(flags & _FLAG_MESSAGE_CONTENT)
    except Exception as e:
        logger.warning("No pude consultar los intents de la aplicación (%s); "
                       "sigo como si estuvieran concedidos.", e)
        return True


def _api_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CELESTIA_TOKEN:
        h["X-Celestia-Token"] = CELESTIA_TOKEN
    return h


def llamar_api(endpoint: str, payload: dict | None = None,
               timeout: int = 180, metodo: str = "POST") -> dict:
    """Llama a Celestia. Devuelve JSON o {error}."""
    req = urllib.request.Request(
        CELESTIA_API + endpoint,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=_api_headers(),
        method=metodo,
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


def trocear(texto: str, limite: int = LIMITE_DISCORD) -> list[str]:
    """Parte un texto largo en trozos que Discord acepte, cortando por líneas."""
    if len(texto) <= limite:
        return [texto]
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        # Una línea sola más larga que el límite: se parte a lo bruto.
        while len(linea) > limite:
            if actual:
                trozos.append(actual)
                actual = ""
            trozos.append(linea[:limite])
            linea = linea[limite:]
        if len(actual) + len(linea) + 1 > limite:
            trozos.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        trozos.append(actual)
    return trozos


# Cada cuánto se pregunta por lo que Celestia haya dejado pendiente.
POLL_BANDEJA_S = 4

# Último sitio donde hubo conversación: ahí se entrega lo que llegue tarde (un
# PDF que tardó medio minuto). Si nunca se habló, se cae al DM del dueño.
_ultimo_canal = {"canal": None}


def adjuntos_de(res: dict) -> list:
    """Ficheros que acompañan a una respuesta: (ruta, nombre, borrar_después).

    Celestia devuelve lo que produce de tres maneras distintas y las tres se
    entregan igual por Discord:
      · `audio_b64`      — su voz, si el perfil la tiene activada.
      · `imagen_b64`     — una imagen que acaba de generar o capturar.
      · `documento_ruta` — un PDF/DOCX ya escrito en el disco del móvil.

    Los dos primeros se materializan en un temporal (se borran tras enviarlos);
    el documento NO se borra: es un fichero del usuario que vive en su móvil.
    """
    adjuntos = []
    # En un PC sin ffmpeg la voz llega en MP3 (audio_tipo «audio/mpeg»).
    voz = ".mp3" if "mpeg" in (res.get("audio_tipo") or "") else ".ogg"
    for clave, nombre, sufijo in (("audio_b64", f"celestia{voz}", voz),
                                  ("imagen_b64", "celestia.jpg", ".jpg")):
        dato = res.get(clave)
        if not dato:
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as f:
                f.write(base64.b64decode(dato))
                adjuntos.append((f.name, nombre, True))
        except Exception as e:
            logger.warning("No pude preparar %s: %s", clave, e)

    doc = res.get("documento_ruta")
    if doc and os.path.exists(doc):
        adjuntos.append((doc, os.path.basename(doc), False))
    return adjuntos


def main() -> None:
    if not DISCORD_TOKEN:
        print("✗ Falta DISCORD_BOT_TOKEN en .env o entorno. Saliendo.")
        sys.exit(1)
    try:
        import discord
    except ImportError:
        print("✗ discord.py no instalado. Instala: pip install discord.py")
        sys.exit(1)

    intents = discord.Intents.default()
    # Sin este intent los mensajes de un servidor llegan con `content` vacío.
    # Hay que activarlo también en el portal, no basta con pedirlo aquí — y
    # pedirlo sin tenerlo concedido impide conectar del todo.
    intents.message_content = intent_de_contenido_concedido(DISCORD_TOKEN)
    if not intents.message_content:
        # No es un problema para lo que hace este puente: Discord envía el
        # contenido igualmente en los mensajes directos y en aquellos donde
        # mencionan al bot, que son exactamente los dos casos que atendemos.
        logger.warning(
            "MESSAGE CONTENT INTENT no está concedido: funcionaré por mensajes "
            "directos y menciones (que es como funciono de todas formas). Si "
            "quieres que lea también los mensajes sueltos de un servidor, "
            "actívalo en discord.com/developers/applications → tu aplicación → "
            "Bot → MESSAGE CONTENT INTENT → Save Changes.")
    cliente = discord.Client(intents=intents)

    @cliente.event
    async def on_ready():
        logger.info("Discord bridge listo — conectado como %s", cliente.user)
        if USUARIOS_OK:
            logger.info("Autorizados por DISCORD_USUARIOS: %d usuario(s).",
                        len(USUARIOS_OK))
            return
        # Sin lista blanca: el único autorizado es quien creó la aplicación en
        # el portal de Discord, que es por definición el dueño de Celestia.
        try:
            app = await cliente.application_info()
            equipo = getattr(app, "team", None)
            if equipo is not None:
                AUTORIZADOS.update(str(m.id) for m in equipo.members)
            elif app.owner is not None:
                AUTORIZADOS.add(str(app.owner.id))
        except Exception as e:                    # red caída, permisos, API…
            logger.error("No pude averiguar el dueño del bot (%s). Por "
                         "seguridad no responderé a nadie: añade tu ID a "
                         "DISCORD_USUARIOS en el .env.", e)
            return
        if AUTORIZADOS:
            logger.info("Sin DISCORD_USUARIOS: solo responderé al dueño del "
                        "bot (%s).", ", ".join(sorted(AUTORIZADOS)))
        else:
            logger.error("No hay ningún usuario autorizado — no responderé a "
                         "nadie. Añade tu ID a DISCORD_USUARIOS en el .env.")

    @cliente.event
    async def on_message(message):
        # No responderse a sí misma: sería un bucle infinito.
        if message.author == cliente.user or message.author.bot:
            return
        # Solo habla con quien está autorizado (ver AUTORIZADOS): la lista del
        # .env o, si no hay, el dueño del bot. Nunca «cualquiera».
        if not puede_hablar(message.author.id):
            logger.info("Ignorado mensaje de %s (id %s): no autorizado. Para "
                        "darle acceso, añade ese id a DISCORD_USUARIOS.",
                        message.author, message.author.id)
            return

        # En un servidor solo contesta si la mencionan; en mensaje directo
        # siempre. Así no interrumpe conversaciones ajenas.
        es_dm = isinstance(message.channel, discord.DMChannel)
        mencionada = cliente.user in message.mentions
        if not es_dm and not mencionada:
            return

        _ultimo_canal["canal"] = message.channel
        texto = message.content or ""
        if mencionada:
            texto = texto.replace(f"<@{cliente.user.id}>", "").strip()

        # Audio adjunto → /audio (Whisper). Imagen → /mensaje con imagen_b64.
        adjunto_audio = None
        adjunto_imagen = None
        for a in message.attachments:
            tipo = (a.content_type or "").lower()
            if tipo.startswith("audio/") and adjunto_audio is None:
                adjunto_audio = a
            elif tipo.startswith("image/") and adjunto_imagen is None:
                adjunto_imagen = a

        async with message.channel.typing():
            if adjunto_audio is not None:
                try:
                    datos = await adjunto_audio.read()
                    sufijo = os.path.splitext(adjunto_audio.filename)[1] or ".ogg"
                    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as f:
                        f.write(datos)
                        ruta = f.name
                    res = llamar_api("/audio", {"ruta": ruta,
                                                "canal": "discord", "remitente": str(message.author.id)})
                    try:
                        os.unlink(ruta)
                    except OSError:
                        pass
                except Exception as e:
                    logger.exception("Error con el audio")
                    await message.reply(f"⚠ No pude procesar el audio: {e}")
                    return
            elif adjunto_imagen is not None:
                try:
                    datos = await adjunto_imagen.read()
                    res = llamar_api("/mensaje", {
                        "texto": texto or "¿Qué hay en esta imagen?",
                        "imagen_b64": base64.b64encode(datos).decode(),
                        "canal": "discord", "remitente": str(message.author.id),
                    })
                except Exception as e:
                    logger.exception("Error con la imagen")
                    await message.reply(f"⚠ No pude procesar la imagen: {e}")
                    return
            else:
                if not texto:
                    return
                res = llamar_api("/mensaje", {"texto": texto,
                                               "canal": "discord", "remitente": str(message.author.id)})

        if res.get("error"):
            await message.reply(f"⚠ {res['error']}")
            return

        adjuntos = adjuntos_de(res)
        respuesta = (res.get("texto") or "").strip()
        # Solo se rellena si no hay NADA que entregar: una imagen sin pie de
        # foto es una respuesta perfectamente válida.
        if not respuesta and not adjuntos:
            respuesta = "(sin respuesta)"
        for trozo in trocear(respuesta):
            await message.reply(trozo)

        for ruta, nombre, temporal in adjuntos:
            try:
                await message.channel.send(file=discord.File(ruta, nombre))
            except Exception as e:
                # Que falle un adjunto no puede tumbar la respuesta ni callarse:
                # el usuario ha pedido algo y tiene que saber que existe.
                logger.warning("No pude enviar «%s»: %s", nombre, e)
                aviso = f"⚠ Generé «{nombre}» pero no pude enviártelo por aquí"
                if not temporal:
                    aviso += f" — lo tienes en {ruta}"
                await message.reply(aviso + ".")
            finally:
                if temporal:
                    try:
                        os.unlink(ruta)
                    except OSError:
                        pass

    async def _donde_entregar():
        """Dónde dejar algo que llega sin que nadie lo haya pedido ahora mismo.

        Lo natural es el sitio de la última conversación; si aún no ha habido
        ninguna, el mensaje directo del dueño.
        """
        if _ultimo_canal["canal"] is not None:
            return _ultimo_canal["canal"]
        for uid in sorted(AUTORIZADOS):
            try:
                usuario = cliente.get_user(int(uid)) or await cliente.fetch_user(int(uid))
                return usuario.dm_channel or await usuario.create_dm()
            except Exception as e:
                logger.debug("No pude abrir el DM con %s: %s", uid, e)
        return None

    async def entregar(canal, res: dict) -> None:
        """Manda por Discord un texto y sus ficheros."""
        adjuntos = adjuntos_de(res)
        texto = (res.get("texto") or "").strip()
        for trozo in trocear(texto):
            if trozo:
                await canal.send(trozo)
        for ruta, nombre, temporal in adjuntos:
            try:
                await canal.send(file=discord.File(ruta, nombre))
            except Exception as e:
                logger.warning("No pude enviar «%s»: %s", nombre, e)
                aviso = f"⚠ Generé «{nombre}» pero no pude enviártelo por aquí"
                if not temporal:
                    aviso += f" — lo tienes en {ruta}"
                await canal.send(aviso + ".")
            finally:
                if temporal:
                    try:
                        os.unlink(ruta)
                    except OSError:
                        pass

    async def vigilar_bandeja():
        """Trae lo que Celestia terminó DESPUÉS de haber contestado.

        Un PDF tarda 20-40 s: la respuesta («te lo paso en cuanto esté listo»)
        ya se envió y el fichero no cabe en ella. Celestia lo deja en la bandeja
        y este bucle lo trae. Sin esto la promesa no se cumplía nunca.
        """
        await cliente.wait_until_ready()
        while not cliente.is_closed():
            await asyncio.sleep(POLL_BANDEJA_S)
            try:
                canal = await _donde_entregar()
                if canal is None:
                    # Sin sitio donde entregar no se recoge nada: recoger vacía
                    # la bandeja, y lo que se saque de ahí ya no vuelve.
                    continue
                res = await asyncio.to_thread(
                    llamar_api, "/pendientes?para=discord", None, 20, "GET")
                for mensaje in res.get("mensajes") or []:
                    await entregar(canal, mensaje)
            except Exception as e:
                logger.debug("Bandeja: %s", e)   # red o API caída: se reintenta

    @cliente.event
    async def on_connect():
        # on_ready se dispara otra vez en cada reconexión; la tarea se lanza
        # una sola vez o habría un vigía por cada corte de red del móvil.
        if not getattr(cliente, "_vigia_bandeja", None):
            cliente._vigia_bandeja = asyncio.create_task(vigilar_bandeja())

    # Los dos fallos de configuración típicos se traducen a una frase que dice
    # qué hacer. Sin esto el usuario recibe 30 líneas de traceback de asyncio
    # para algo que se arregla con un interruptor en una web.
    try:
        cliente.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.PrivilegedIntentsRequired:
        logger.error(
            "Discord no me deja LEER los mensajes: falta activar MESSAGE "
            "CONTENT INTENT. Ve a discord.com/developers/applications → tu "
            "aplicación → pestaña Bot → activa «MESSAGE CONTENT INTENT» → "
            "Save Changes, y vuelve a encender el canal.")
        sys.exit(2)
    except discord.errors.LoginFailure:
        logger.error(
            "Discord rechaza el token (DISCORD_BOT_TOKEN). Genera uno nuevo en "
            "discord.com/developers/applications → tu aplicación → Bot → Reset "
            "Token y dímelo: «mi token de Discord es …».")
        sys.exit(3)


if __name__ == "__main__":
    main()
