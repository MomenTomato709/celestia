"""Primer arranque: que una Celestia recién instalada pueda pensar sin editar ficheros.

Una instalación nueva no trae ninguna clave de IA, y el proveedor sin clave
(Pollinations) no sirve de base: su API antigua está en retirada y con un
mensaje de tamaño real devuelve 402 o 500 (comprobado el 3 oct 2026). Así que
la persona tiene que conseguir una clave gratis — y esto lo deja en un minuto:

- `MENSAJE_SIN_CEREBRO`: los pasos para sacarla, sin jerga.
- `detectar_clave()`: reconoce una clave pegada en el chat por su FORMA, sin
  que haga falta decir «mi clave es…».
- `validar()`: la prueba contra el proveedor con una llamada gratuita (listar
  modelos) antes de guardarla, para no dejar una clave mal copiada.
- `guardar()`: al `.env` y al proceso vivo, sin reiniciar.

Como el token de un canal (`canales.detectar_token`), el mensaje con la clave
se contesta antes del orquestador: no llega ni al historial, ni a la memoria,
ni a ningún proveedor, ni a los logs.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional, Tuple

from .config import fijar_en_env
from .paths import ENV_FILE

logger = logging.getLogger("celestia_v1")

# (variable del .env, nombre para la persona, forma de la clave,
#  URL que la comprueba gratis, cómo va la clave en esa URL)
_PROVEEDORES = (
    ("GROQ_API_KEY", "Groq", re.compile(r"\bgsk_[A-Za-z0-9]{40,64}\b"),
     "https://api.groq.com/openai/v1/models", "bearer"),
    # Las de Google de antes empiezan por «AIza»; las nuevas (2026), por «AQ.».
    ("GEMINI_API_KEY", "Gemini",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b|(?<![\w.])AQ\.[0-9A-Za-z_-]{40,70}"),
     "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", "cabecera_google"),
    ("OPENROUTER_API_KEY", "OpenRouter", re.compile(r"\bsk-or-v1-[0-9a-f]{64}\b"),
     "https://openrouter.ai/api/v1/key", "bearer"),
    ("CEREBRAS_API_KEY", "Cerebras", re.compile(r"\bcsk-[0-9a-z]{40,64}\b"),
     "https://api.cerebras.ai/v1/models", "bearer"),
    # De pago (Enzo, 3 oct 2026: «le estoy pegando la de deepseek y no va» —
    # no estaba en la lista y el mensaje se trataba como charla). «sk-» y 32
    # hexadecimales; no choca con OpenRouter («sk-or-v1-…»: la «o» no es hex).
    # Listar modelos no cuesta saldo.
    ("DEEPSEEK_API_KEY", "DeepSeek", re.compile(r"\bsk-[0-9a-f]{32}\b"),
     "https://api.deepseek.com/models", "bearer"),
)

# Lo que ya hay que tener para que Celestia piense por la cadena remota. Es la
# misma lista que mira `ModelWrapper._hay_clave_remota`, más la de Groq.
CLAVES_DE_IA = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY",
                "GITHUB_MODELS_TOKEN", "OPENROUTER_API_KEY", "SAMBANOVA_API_KEY",
                "MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "NVIDIA_API_KEY")

MENSAJE_SIN_CEREBRO = (
    "Para poder pensar necesito una clave de IA. Es gratis y se saca en un minuto:\n\n"
    "1. Entra en aistudio.google.com/apikey con tu cuenta de Google.\n"
    "2. Pulsa «Create API key» y copia la clave.\n"
    "3. Pégamela aquí, tal cual, en un mensaje.\n\n"
    "Si prefieres que vaya más rápida, también vale una de Groq: "
    "console.groq.com/keys (empieza por «gsk_»).\n"
    "La clave se queda guardada sólo en este aparato."
)


def hay_cerebro(config) -> bool:
    """¿Hay al menos una clave con la que pensar?"""
    return any(getattr(config, c, "") for c in CLAVES_DE_IA)


def detectar_clave(texto: str) -> Optional[Tuple[str, str, str]]:
    """Devuelve (variable, nombre, clave) si el texto lleva una clave de IA."""
    if not texto or len(texto) > 2000:
        return None
    for var, nombre, forma, _url, _modo in _PROVEEDORES:
        m = forma.search(texto)
        if m:
            return var, nombre, m.group(0)
    return None


def validar(var: str, clave: str, timeout: float = 15) -> Optional[bool]:
    """True si el proveedor la acepta, False si la rechaza, None si no se pudo
    comprobar (sin internet, proveedor caído): ahí se guarda igual, porque
    pedir que la vuelva a pegar por un fallo de red sería peor."""
    for v, _nombre, _forma, url, modo in _PROVEEDORES:
        if v != var:
            continue
        cabeceras = {"User-Agent": "Celestia"}
        if modo == "bearer":
            cabeceras["Authorization"] = f"Bearer {clave}"
        else:
            cabeceras["x-goog-api-key"] = clave
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cabeceras),
                                        timeout=timeout) as r:
                json.loads(r.read(200_000) or b"{}")
                return True
        except urllib.error.HTTPError as e:
            # 400/401/403: clave mala. Gemini contesta 400 «API_KEY_INVALID».
            if e.code in (400, 401, 403):
                return False
            logger.warning("No pude comprobar la clave de %s: HTTP %s", var, e.code)
            return None
        except Exception as e:
            logger.warning("No pude comprobar la clave de %s: %s", var, type(e).__name__)
            return None
    return None


def guardar(config, var: str, clave: str) -> bool:
    """Al `.env` (sólo legible por el dueño) y al proceso vivo."""
    if not fijar_en_env(var, clave):
        return False
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    os.environ[var] = clave
    setattr(config, var, clave)
    logger.info("Clave de %s guardada en .env (valor no registrado).", var)
    return True


def atender_clave(config, texto: str, desde_fuera: bool = False) -> Optional[str]:
    """Si `texto` trae una clave de IA, la comprueba y la guarda.

    Devuelve lo que hay que contestar, o None si el mensaje no lleva clave.

    `desde_fuera`: llega por WhatsApp, Telegram o Discord. Ahí escribe quien
    esté en la lista de permitidos, no sólo el dueño, y cambiar la clave es
    decidir con qué cuota piensa Celestia (lo cazó Codex, 3 oct 2026). Se
    contesta igual aquí —la clave no sigue hacia el modelo ni el historial—,
    pero no se guarda.
    """
    hallada = detectar_clave(texto)
    if hallada is None:
        return None
    var, nombre, clave = hallada
    if desde_fuera:
        logger.warning("Clave de %s recibida por un canal externo: no se guarda.", var)
        return (f"Eso parece una clave de {nombre}. Por seguridad sólo las acepto "
                "desde el chat de este aparato, así que no la he guardado. "
                "Borra este mensaje: con ella se puede gastar la cuota de quien la sacó.")
    ok = validar(var, clave)
    if ok is False:
        return (f"Esa clave de {nombre} no la acepta {nombre}: puede que se haya "
                "copiado a medias. Cópiala otra vez entera y pégamela.")
    if not guardar(config, var, clave):
        return (f"La clave de {nombre} es buena, pero no he podido guardarla en "
                "el disco. ¿Queda espacio libre en el aparato?")
    if ok is None:
        return (f"Guardada la clave de {nombre}. No he podido comprobarla ahora "
                "(¿hay internet?), pero la usaré en cuanto se pueda. "
                "Si me lo has mandado desde otro chat, borra ese mensaje.")
    try:
        from .sentidos import tras_clave
        gana = tras_clave(var)
    except Exception as e:                  # que un fallo aquí no pierda la clave
        logger.debug("tras_clave: %s", e)
        gana = ""
    return (f"¡Listo! Clave de {nombre} guardada. {gana}\n".replace(" \n", "\n") +
            "(Si me la has mandado desde otro chat, borra ese mensaje: "
            "con ella se puede gastar tu cuota.)")
