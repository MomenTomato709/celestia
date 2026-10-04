"""Gestor de canales de conversación de Celestia.

Celestia tiene UN solo cerebro (el `Orchestrator` que vive dentro del proceso
`celestia.py --modo whatsapp`, que en realidad es el daemon de la API HTTP) y
varios *canales* por los que el usuario puede hablarle. Cada canal es un puente
(bridge) independiente que traduce su plataforma a llamadas HTTP contra
`/mensaje`, `/audio` o `/captura`.

El objetivo de este módulo es que el usuario pueda decir «hablemos por Telegram»
y que Celestia levante ese puente y apague el que estaba usando, para no gastar
RAM ni batería en canales que nadie está mirando — algo crítico porque todo esto
corre en un móvil Android bajo Termux/PRoot.

Modelo mental
─────────────
    ┌──────────┐   HTTP    ┌───────────────────────────┐
    │ WhatsApp │──────────▶│                           │
    ├──────────┤           │  API Flask  (127.0.0.1)   │
    │ Telegram │──────────▶│  = el ÚNICO proceso caro  │
    ├──────────┤           │    (modelo, FAISS, TTS)   │
    │ Discord  │──────────▶│                           │
    ├──────────┤           │      Orchestrator         │
    │  Termux  │──────────▶│                           │
    └──────────┘           └───────────────────────────┘

`termux` es especial: no es un proceso puente, es un cliente de línea de comandos
(`hablar.py`) que se conecta a la API solo mientras el usuario lo tiene abierto.
Por eso su coste es cero y está SIEMPRE disponible: es la red de seguridad para
volver a hablar con Celestia aunque todos los puentes estén apagados.

Uso
───
    gestor = GestorCanales()
    gestor.estado()                      # qué hay vivo y qué falta para cada canal
    gestor.activar("telegram")           # enciende Telegram y apaga los demás
    gestor.activar("discord", exclusivo=False)   # enciende sin apagar nada
    gestor.parar("whatsapp")

El estado se persiste en `memoria/canales.json` para recordar la preferencia
entre reinicios, pero la verdad sobre qué está vivo se consulta siempre al
sistema operativo (`pgrep`), nunca al fichero: si Android mata Termux, el
fichero mentiría.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .paths import ENV_FILE, ES_ANDROID, ROOT, LOG_DIR, MEM_DIR, no_es_el_env_real

# Windows y Mac no tienen /proc ni pgrep: allí los puentes se buscan con psutil.
_SIN_PROC = not Path("/proc/self").exists()

logger = logging.getLogger("celestia_v1")

ESTADO_FILE = MEM_DIR / "canales.json"

# Canal que nunca se apaga: es un cliente efímero, no un proceso servidor.
CANAL_LOCAL = "termux"


# ─────────────────────────────────────────────────────────────────────────────
# Definición de canales
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Canal:
    """Un canal de conversación.

    `patron_proceso` es una expresión para `pgrep -f` que identifica el proceso
    del puente. Se escribe con corchetes en la primera letra (`bridge[.]js`)
    para que el propio `pgrep` no se encuentre a sí mismo en la lista.
    """
    nombre:         str
    descripcion:    str
    # Cómo arrancarlo. None = no es un proceso (canal local).
    comando:        Optional[List[str]] = None
    cwd:            Optional[Path]      = None
    patron_proceso: Optional[str]       = None
    # Nombre del ejecutable real del puente (`/proc/PID/comm`). Es un filtro de
    # SEGURIDAD, no una optimización: `pgrep -f` encuentra el patrón en la línea
    # de comandos de CUALQUIER proceso, incluido el `proot` que contiene todo el
    # sistema y el shell que lanzó Celestia. Matar aquello tumbaría la máquina
    # entera. Solo se considera puente lo que además ejecuta este binario.
    binario:        Optional[str]       = None
    # Marca de «ya estoy funcionando» en el log del puente. Sirve para no dar
    # por bueno un arranque que en realidad va a morir dos segundos después.
    patron_listo:   Optional[str]       = None
    # Qué hace falta para que funcione: (comprobación, mensaje si falla)
    requisitos:     List[Callable[[], Optional[str]]] = field(default_factory=list)
    aliases:        List[str]           = field(default_factory=list)
    # Librería de Python que necesita el puente y cómo se instala: en un PC,
    # Celestia se la pone sola la primera vez (complementos.instalar_ya).
    modulo:         Optional[str]       = None
    paquetes:       List[str]           = field(default_factory=list)

    @property
    def es_local(self) -> bool:
        return self.comando is None


def _requiere_env(clave: str, ayuda: str = "") -> Callable[[], Optional[str]]:
    """Requisito: una variable presente en el entorno o en el .env del proyecto.

    `ayuda` explica en una frase CÓMO conseguir el valor. Sin ella el usuario
    solo lee «falta DISCORD_BOT_TOKEN», que no le dice qué hacer a continuación.
    """
    def _check() -> Optional[str]:
        if os.environ.get(clave):
            return None
        # El daemon puede haber arrancado sin el .env cargado; miramos el fichero.
        env_file = ENV_FILE
        try:
            for linea in env_file.read_text(encoding="utf-8").splitlines():
                linea = linea.strip()
                if linea.startswith("#") or "=" not in linea:
                    continue
                k, _, v = linea.partition("=")
                if k.strip() == clave and v.strip().strip('"').strip("'"):
                    return None
        except OSError:
            pass
        return f"falta {clave} en {env_file}" + (f" — {ayuda}" if ayuda else "")
    return _check


def _requiere_modulo(modulo: str, paquete: str) -> Callable[[], Optional[str]]:
    """Requisito: un módulo de Python importable."""
    def _check() -> Optional[str]:
        import importlib.util
        if importlib.util.find_spec(modulo) is None:
            # --break-system-packages: este entorno (Ubuntu en PRoot) marca el
            # Python del sistema como «externally managed» (PEP 668) y sin la
            # bandera pip se niega a instalar. Sin ella el consejo no funciona.
            return (f"falta la librería «{modulo}» (instálala con: "
                    f"pip install --break-system-packages {paquete})")
        return None
    return _check


def _requiere_ruta(ruta: Path, pista: str) -> Callable[[], Optional[str]]:
    """Requisito: un fichero o directorio que debe existir."""
    def _check() -> Optional[str]:
        if not ruta.exists():
            return f"falta {ruta} ({pista})"
        return None
    return _check


def _requiere_binario(binario: str, pista: str) -> Callable[[], Optional[str]]:
    """Requisito: un ejecutable en el PATH."""
    def _check() -> Optional[str]:
        if shutil.which(binario) is None:
            return f"falta el ejecutable «{binario}» ({pista})"
        return None
    return _check


def _python_puente() -> str:
    """Con qué Python se lanza un puente.

    En Linux (el móvil), `python3` a secas, como siempre: el filtro de
    seguridad de `_pids` compara con /proc/PID/comm, que dice «python3». En
    Windows y Mac una instalación no tiene `python3` en el PATH: el mismo
    Python que mueve a Celestia (con consola: pythonw no tiene salida)."""
    if sys.platform.startswith("linux"):
        return "python3"
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def _node_puente() -> str:
    """El `node` del sistema o el que se bajó Celestia (en un PC; ver complementos)."""
    from . import complementos
    return complementos.ruta_node() or "node"


def _requiere_node() -> Callable[[], Optional[str]]:
    def _check() -> Optional[str]:
        from . import complementos
        if complementos.ruta_node():
            return None
        if complementos.se_puede():
            return ("me falta Node.js; me lo bajo yo si me dices «quiero hablar por "
                    "WhatsApp»")
        if os.environ.get("CELESTIA_APP_ANDROID") == "1":
            return ("WhatsApp no se puede encender desde la app de Android (necesita "
                    "Node.js): hazlo desde la Celestia del ordenador o de Termux")
        return "falta el ejecutable «node» (instala Node.js: en Termux, pkg install nodejs)"
    return _check


def _aqui() -> str:
    """Cómo se llama el canal local: en el móvil es Termux; en un PC, el chat."""
    return "Termux" if ES_ANDROID else "el chat"


def _catalogo() -> Dict[str, Canal]:
    """Canales conocidos. Añadir uno nuevo es añadir una entrada aquí."""
    wa_dir = ROOT / "whatsapp_bridge"
    return {
        CANAL_LOCAL: Canal(
            nombre=CANAL_LOCAL,
            descripcion="Terminal de Termux (cliente ligero, sin proceso propio)",
            aliases=["cli", "terminal", "consola", "cmd", "aqui", "aquí", "local"],
        ),
        "whatsapp": Canal(
            nombre="whatsapp",
            descripcion="WhatsApp vía Baileys (Node.js)",
            comando=[_node_puente(), "bridge.js"],
            cwd=wa_dir,
            patron_proceso="bridge[.]js",
            binario="node",
            patron_listo=r"WhatsApp vinculado|Celestia está lista|CÓDIGO:",
            requisitos=[
                _requiere_node(),
                _requiere_ruta(wa_dir / "node_modules",
                               "me las instalo yo si me dices «quiero hablar por WhatsApp»"),
            ],
            aliases=["wa", "whats", "guasap", "wasap"],
        ),
        "telegram": Canal(
            nombre="telegram",
            descripcion="Telegram vía bot oficial (python-telegram-bot)",
            comando=[_python_puente(), "telegram_bridge/bridge.py"],
            cwd=ROOT,
            patron_proceso="telegram_bridge/bridge[.]py",
            binario="python3",
            patron_listo=r"Telegram bridge listo",
            requisitos=[
                _requiere_env(
                    "TELEGRAM_BOT_TOKEN",
                    "créalo en Telegram escribiéndole a @BotFather (/newbot) y "
                    "dímelo aquí: «mi token de Telegram es 123456:ABC-DEF…»"),
                _requiere_modulo("telegram", "'python-telegram-bot>=21.0'"),
            ],
            aliases=["tg", "telegran"],
            modulo="telegram",
            paquetes=["python-telegram-bot>=21.0"],
        ),
        "discord": Canal(
            nombre="discord",
            descripcion="Discord vía bot (discord.py)",
            comando=[_python_puente(), "discord_bridge/bridge.py"],
            cwd=ROOT,
            patron_proceso="discord_bridge/bridge[.]py",
            binario="python3",
            patron_listo=r"Discord bridge listo",
            requisitos=[
                _requiere_env(
                    "DISCORD_BOT_TOKEN",
                    "créalo en discord.com/developers/applications → New "
                    "Application → Bot → Reset Token, y dímelo aquí: "
                    "«mi token de Discord es …»"),
                _requiere_modulo("discord", "discord.py"),
            ],
            aliases=["disc", "discor"],
            modulo="discord",
            paquetes=["discord.py"],
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Detección de intención (determinista, sin LLM)
# ─────────────────────────────────────────────────────────────────────────────
# Se resuelve con regex a propósito: cambiar de canal es una operación de
# infraestructura y debe funcionar igual con cualquier modelo detrás, incluso
# con el modelo local pequeño o sin conexión.

def _alt_canales() -> str:
    """Alternativa regex con todos los nombres y alias de canal."""
    nombres: List[str] = []
    for canal in _catalogo().values():
        nombres.append(canal.nombre)
        nombres.extend(canal.aliases)
    # Los más largos primero para que "whatsapp" gane a "wa".
    nombres.sort(key=len, reverse=True)
    return "|".join(re.escape(n) for n in nombres)


def _re_cambiar() -> List[re.Pattern]:
    """Patrones de «quiero hablar por este otro canal».

    Son dos familias con reglas distintas a propósito:

    1. Verbos de conversación («hablemos», «pásate», «vamos») — exigen una
       preposición de destino (por/en/a/con…). NO se acepta «de», porque
       «hablemos de Discord» es conversar *sobre* Discord, no *por* Discord.
    2. Verbos de activación («activa», «enciende», «conecta») — ahí «de» sí
       vale («activa el bridge de Telegram») e incluso puede no haber
       preposición («enciende Telegram»), porque el verbo ya no es ambiguo.
    """
    canales = _alt_canales()
    return [
        re.compile(
            r"(?:h[aá]bla(?:me|mos|rte|r)?|habl[ae]mos|p[aá]s(?:a|ar|ate|arme|arnos|émonos|emos)|"
            r"cambia(?:r|rme|rnos|te|mos)?|vamos|volv(?:amos|er)|vuelve|vete|"
            r"m[uú]da(?:te|rme|rnos)|"
            r"sig(?:ue|amos|uemos)|seguir|contin(?:[uú]a|uemos|uar)|"
            r"escr[ií]be(?:me)?|responde(?:me)?)"
            r"[^.,;\n]{0,40}?"
            r"\b(?:por|en|a|al|con|v[ií]a|usando|desde)\s+"
            r"(?:el\s+|la\s+|mi\s+)?"
            rf"\b({canales})\b",
            re.I,
        ),
        # 2a. Verbos INEQUÍVOCOS de infraestructura: solo se usan para
        #     encender un servicio, nunca para abrir una app en el móvil.
        #     Los de «ponerlo en marcha» (configura, instala, monta) están aquí
        #     porque quien los dice quiere justo eso — que se lo dejen andando—,
        #     y esa frase es la que abre la guía paso a paso (`guias.py`).
        re.compile(
            r"(?:activa(?:r|me)?|reactiva(?:r)?|enciende|encender|"
            r"conecta(?:r)?|arranca(?:r)?|levanta(?:r)?|inicia(?:r)?|"
            r"config[úu]ra(?:me|r)?|configurar|inst[áa]la(?:me|r)?|"
            r"m[óo]nta(?:me|r)?|prepara(?:me|r)?|habilita(?:r)?|"
            r"pon(?:me)?\s+en\s+marcha|d[ée]ja(?:me)?\s+listo)"
            r"[^.,;\n]{0,40}?"
            rf"\b({canales})\b",
            re.I,
        ),
        # 2c. «quiero usar Telegram», «necesito tener WhatsApp». El verbo de en
        #     medio es lo que quita la duda: «quiero WhatsApp» a secas podría
        #     ser «ábreme la app», y esa distinción ya estaba tomada más abajo.
        re.compile(
            r"(?:quiero|quisiera|necesito|me\s+gustar[ií]a|querr[ií]a)\s+"
            r"(?:poder\s+)?(?:usar|tener|poner|probar|configurar|activar|montar)\s+"
            r"(?:el\s+|la\s+|mi\s+|un\s+|una\s+)?"
            rf"\b({canales})\b",
            re.I,
        ),
        # 2b. Verbos AMBIGUOS: «abre WhatsApp» / «pon Telegram» es, en el móvil
        #     del usuario, ABRIR LA APP (herramienta `abrir_app` del agente),
        #     no cambiar de canal. Solo cuentan como canal si el mensaje nombra
        #     explícitamente la infraestructura («abre el puente de Telegram»).
        re.compile(
            r"(?:abre|abrir|[aá]breme|pon(?:me|er)?)"
            r"[^.,;\n]{0,40}?"
            r"\b(?:bridge|puente|canal|conexi[oó]n|conector|bot)\b"
            r"[^.,;\n]{0,40}?"
            rf"\b({canales})\b",
            re.I,
        ),
        # 3. Preferencia sin verbo de movimiento («mejor por WhatsApp»,
        #    «a partir de ahora en Telegram»). Exige preposición de destino
        #    por la misma razón que la familia 1: evitar «hablamos de X».
        re.compile(
            r"(?:mejor|ahora|desde\s+ahora|a\s+partir\s+de\s+ahora|"
            r"de\s+ahora\s+en\s+adelante|me\s+voy|me\s+paso|me\s+cambio)"
            r"[^.,;\n]{0,20}?"
            r"\b(?:por|en|a|al|con|v[ií]a)\s+(?:el\s+|la\s+|mi\s+)?"
            rf"\b({canales})\b",
            re.I,
        ),
        # 4. Preferencia directa, sin preposición: el verbo ya es inequívoco.
        re.compile(
            r"(?:prefiero|me\s+quedo\s+con)\s+"
            r"(?:el\s+|la\s+|hablar\s+por\s+|)(?:canal\s+)?"
            rf"\b({canales})\b",
            re.I,
        ),
    ]


def _re_apagar() -> re.Pattern:
    """«apaga WhatsApp», «desactiva el bridge de Telegram»…"""
    return re.compile(
        r"(?:apaga|apagar|desactiva|desactivar|desconecta|desconectar|"
        r"cierra|cerrar|para|parar|det[ée]n|detener|quita|quitar)"
        r"[^.,;\n]{0,40}?"
        rf"\b({_alt_canales()})\b",
        re.I,
    )


def _re_apagar_todos() -> re.Pattern:
    """«apágalo todo», «apaga todos los bridges», «no quiero ningún puente».

    Va antes que `_re_apagar` porque «apaga todos los canales» también encaja
    con el patrón de un canal concreto si alguien llama «canal» a un canal.
    """
    return re.compile(
        r"(?:apaga|ap[aá]ga\w*|apagar|desactiva\w*|desactivar|desconecta\w*|"
        r"cierra|cerrar|quita\w*|quitar|para|parar|det[ée]n\w*|detener)"
        r"(?:\s+\w+){0,2}?\s+"
        r"(?:todos?|toda?s?)\b"
        r"|"
        r"no\s+quiero\s+(?:ning[uú]n|ninguno|nada\s+de)\s*"
        r"(?:bridge|puente|canal)"
        r"|"
        r"(?:apaga|desactiva|quita|cierra)\w*\s+(?:los\s+|todos\s+los\s+)?"
        r"(?:bridges|puentes|canales)\b",
        re.I,
    )


def _re_no_exclusivo() -> re.Pattern:
    """Marcas de «enciéndelo sin apagar lo otro».

    Por defecto encender un canal apaga los demás (es la razón de ser de este
    módulo: gastar lo mínimo). Estas frases piden lo contrario explícitamente.
    """
    return re.compile(
        r"\btambi[ée]n\b|\badem[áa]s\b|"
        r"sin\s+(?:apagar|cerrar|desactivar|parar|quitar|desconectar)|"
        r"\b(?:los\s+dos|las\s+dos|ambos|ambas|todos\s+a\s+la\s+vez)\b|"
        r"a\s+la\s+vez|al\s+mismo\s+tiempo|"
        r"deja\s+(?:el\s+otro|los\s+otros|lo\s+dem[áa]s)\s+(?:encendido|activo)",
        re.I,
    )


def _re_listar() -> re.Pattern:
    """«¿por dónde puedo hablarte?», «qué canales tienes»…"""
    return re.compile(
        r"(?:qu[eé]|cu[aá]les|cu[aá]nto[s]?)\s+canales|"
        r"canales\s+(?:tienes|hay|disponibles|activos)|"
        r"(?:qu[eé]|cu[aá]l)\s+canal\s+(?:est[aá]|tienes|uso|usas|hay)|"
        r"canal\s+(?:actual|activo)\b|"
        r"por\s+d[oó]nde\s+(?:puedo|podemos|te|me|est[aá]s)\s*\w*|"
        r"d[oó]nde\s+(?:est[aá]s|me)\s+(?:escuchando|escuchas|lees|oyes)|"
        r"(?:lista|list[ae]r|dime|mu[eé]strame|ver)\s+(?:los\s+)?canales|"
        r"estado\s+de\s+(?:los\s+)?(?:canales|bridges|puentes)|"
        r"(?:qu[eé]|cu[aá]les)\s+(?:bridges|puentes)\s+\w+",
        re.I,
    )


def resolver_alias(texto: str) -> Optional[str]:
    """Devuelve el nombre canónico del canal mencionado, o None."""
    t = texto.strip().lower()
    for canal in _catalogo().values():
        if t == canal.nombre or t in canal.aliases:
            return canal.nombre
    return None


# Dónde vive el token de cada canal que necesita uno. Los canales que no
# aparecen aquí (termux, whatsapp) se configuran de otra manera.
TOKEN_POR_CANAL: Dict[str, str] = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord":  "DISCORD_BOT_TOKEN",
}

_RE_MENCION_TOKEN = re.compile(
    r"\b(?:token|clave|api[\s_-]?key|credencial)\b", re.I)

# Formas reales de cada token. Validar la FORMA evita guardar como token una
# palabra cualquiera de la frase («mi token de discord es este de aquí»).
_FORMA_TOKEN: Dict[str, re.Pattern] = {
    # BotFather: «<id numérico>:<secreto>».
    "telegram": re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
    # Discord: tres partes separadas por puntos.
    "discord":  re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"),
}


def detectar_token(texto: str) -> Optional[Tuple[str, str]]:
    """Detecta «mi token de Discord es XXXX» y devuelve (canal, token).

    Se pide que coincidan TRES cosas —la palabra «token», el canal y la forma
    exacta del token— para no confundir con una pregunta («¿qué es un token de
    Discord?») ni guardar basura en el `.env`.
    """
    if not texto or not _RE_MENCION_TOKEN.search(texto):
        return None
    m = re.search(rf"\b({_alt_canales()})\b", texto, re.I)
    if not m:
        return None
    canal = resolver_alias(m.group(1))
    forma = _FORMA_TOKEN.get(canal or "")
    if not forma:
        return None
    encontrado = forma.search(texto)
    return (canal, encontrado.group(0)) if encontrado else None


# Fallos de arranque que ya hemos visto, traducidos a una frase que dice QUÉ
# HACER. El log de un puente caído es un traceback de asyncio o de Node: útil
# para depurar, inservible para el usuario, que solo necesita saber que le falta
# pulsar un interruptor. Se busca tanto el mensaje que escribe nuestro puente
# como la excepción cruda, por si murió antes de poder traducirla él mismo.
_FALLOS_CONOCIDOS: List[Tuple[str, str]] = [
    # Ojo con esta: el puente también ESCRIBE «MESSAGE CONTENT INTENT» en un
    # aviso informativo cuando arranca sin él (que es un arranque válido). Solo
    # cuentan la excepción de discord.py y la frase con la que la traducimos.
    (r"PrivilegedIntentsRequired|falta activar MESSAGE CONTENT INTENT",
     "Discord no me deja leer los mensajes. Entra en "
     "discord.com/developers/applications → tu aplicación → pestaña Bot → "
     "activa «MESSAGE CONTENT INTENT» → Save Changes, y lo enciendo otra vez."),
    (r"LoginFailure|Improper token|rechaza el token|Unauthorized|InvalidToken",
     "El token no vale: o está mal copiado o lo han reseteado. Genera uno "
     "nuevo en el portal del bot y dímelo: «mi token es …»."),
    (r"ModuleNotFoundError: No module named '([\w.]+)'",
     "Falta una librería de Python: {0}."),
    (r"Cannot find module '([^']+)'",
     "Falta una dependencia de Node: {0}. Ejecuta: cd whatsapp_bridge && npm install."),
    (r"EADDRINUSE|address already in use",
     "El puerto ya está ocupado: hay otra copia de ese puente corriendo."),
]


def _cola_log(log_path: Path, limite: int = 8000) -> str:
    """El final del log de un puente. Vacío si aún no existe o no se puede leer.

    Solo el final porque los mensajes de arranque están siempre ahí y un log
    puede haber crecido mucho.

    Sesión 58: eso era lo que decía este comentario, pero el código hacía
    `read_text()[-limite:]` — o sea, cargaba el log **entero** en memoria y
    luego tiraba todo menos los últimos 8 KB. Con los logs de hoy (~50 KB) no
    se nota; con un puente llevando meses sin rotar, sí. Es el mismo patrón
    que costó un pico de 268 MB en `model._get_few_shot`, y en un móvil donde
    Android mata por presión de memoria no conviene dejarlo puesto.
    """
    try:
        tam = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if tam > limite:
                f.seek(tam - limite)
            crudo = f.read()
    except OSError:
        return ""
    return crudo.decode("utf-8", errors="replace")


def _diagnosticar(log_path: Path) -> Optional[str]:
    """Traduce el final del log de un puente caído a una frase accionable."""
    texto = _cola_log(log_path)
    if not texto:
        return None
    for patron, explicacion in _FALLOS_CONOCIDOS:
        m = re.search(patron, texto)
        if m:
            return explicacion.format(*m.groups()) if m.groups() else explicacion
    return None


@dataclass
class Intencion:
    """Resultado del análisis de un mensaje del usuario."""
    accion: str                   # "cambiar" | "apagar" | "apagar_todos" | "listar"
    canal:  Optional[str] = None
    # Solo para "cambiar": False cuando el usuario pide explícitamente que los
    # demás puentes sigan encendidos («enciende Telegram también»).
    exclusivo: bool = True


def detectar_intencion(texto: str) -> Optional[Intencion]:
    """Analiza un mensaje y decide si pide una operación sobre canales.

    Devuelve None cuando el mensaje no habla de canales, que es el caso normal:
    el mensaje sigue entonces su camino habitual hacia el LLM.
    """
    if not texto or not texto.strip():
        return None

    exclusivo = not bool(_re_no_exclusivo().search(texto))

    # «sin apagar WhatsApp» menciona un apagado que NO se pide. Se retira del
    # texto antes de buscar apagados para que no se interprete al revés.
    texto_apagar = re.sub(
        r"\bsin\s+(?:apagar|cerrar|desactivar|parar|quitar|desconectar)\b[^.,;\n]*",
        " ", texto, flags=re.I)

    # El orden importa, de lo más específico a lo más general:
    # "apágalo todo" > "apaga X" > "cambia a X" > "¿qué canales hay?".
    if _re_apagar_todos().search(texto_apagar):
        return Intencion(accion="apagar_todos")

    m = _re_apagar().search(texto_apagar)
    if m:
        canal = resolver_alias(m.group(1))
        if canal:
            return Intencion(accion="apagar", canal=canal)

    for patron in _re_cambiar():
        m = patron.search(texto)
        if m:
            canal = resolver_alias(m.group(1))
            if canal:
                return Intencion(accion="cambiar", canal=canal, exclusivo=exclusivo)

    if _re_listar().search(texto):
        return Intencion(accion="listar")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Gestor
# ─────────────────────────────────────────────────────────────────────────────
class GestorCanales:
    """Enciende, apaga y consulta los puentes de conversación.

    Es seguro instanciarlo varias veces: no guarda estado propio más allá del
    fichero de preferencias, y la verdad sobre los procesos se pregunta al SO.
    """

    def __init__(self, catalogo: Optional[Dict[str, Canal]] = None):
        self.canales = catalogo if catalogo is not None else _catalogo()

    # ── Procesos ─────────────────────────────────────────────────────────────
    @staticmethod
    def _comm(pid: int) -> str:
        """Nombre del ejecutable de un PID («node», «python3», «bash»…)."""
        try:
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _ancestros() -> set:
        """PIDs de los que cuelga este proceso, subiendo hasta init.

        Se usan como lista negra absoluta: matar a un ancestro es suicidarse y,
        en este entorno (Termux + PRoot), tumbar la máquina entera.
        """
        cadena, pid = set(), os.getpid()
        for _ in range(64):                     # tope: nunca colgarse en un ciclo
            cadena.add(pid)
            try:
                # Campo 4 de /proc/PID/stat es el PPID. El nombre del ejecutable
                # (campo 2) puede llevar espacios y paréntesis, así que se corta
                # por el último ')' antes de partir.
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                ppid = int(stat[stat.rfind(")") + 1:].split()[1])
            except (OSError, ValueError, IndexError):
                break
            if ppid <= 1 or ppid in cadena:
                break
            pid = ppid
        return cadena

    def _pids(self, canal: Canal) -> List[int]:
        """PIDs vivos del puente, según el sistema operativo.

        Filtra dos cosas que `pgrep -f` devuelve y que NUNCA se pueden matar:
        procesos que solo mencionan el patrón en su línea de comandos (el `proot`
        contenedor, el shell lanzador) y los ancestros de este propio proceso.
        """
        if not canal.patron_proceso:
            return []
        if _SIN_PROC:
            return self._pids_psutil(canal)
        try:
            out = subprocess.run(
                ["pgrep", "-f", canal.patron_proceso],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("pgrep falló para %s: %s", canal.nombre, e)
            return []

        prohibidos = self._ancestros()
        pids = []
        for p in out.stdout.split():
            if not p.isdigit():
                continue
            pid = int(p)
            if pid in prohibidos:
                continue
            if canal.binario and self._comm(pid) != canal.binario:
                # Es el proot, el bash lanzador o un `tail` del log: no es el puente.
                continue
            pids.append(pid)
        return pids

    @staticmethod
    def _pids_psutil(canal: Canal) -> List[int]:
        """Lo mismo que `_pids` en Windows y Mac. Las rutas de Windows llevan
        «\\»: se comparan como «/» para que el patrón valga en los dos."""
        try:
            import psutil
        except ImportError:
            return []
        patron = re.compile(canal.patron_proceso or "")
        prohibidos = {os.getpid(), os.getppid()}
        # «python3» en el catálogo es cualquier Python (python.exe, python3.12…).
        binario = (canal.binario or "").rstrip("3")
        pids = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                linea = " ".join(p.info["cmdline"] or []).replace("\\", "/")
                nombre = (p.info["name"] or "").lower()
            except Exception:
                continue
            if p.info["pid"] in prohibidos or not patron.search(linea):
                continue
            if binario and not nombre.startswith(binario):
                continue
            pids.append(p.info["pid"])
        return pids

    @staticmethod
    def _matar(pid: int, fuerte: bool) -> None:
        if os.name == "nt":                     # Windows: sin SIGKILL
            try:
                import psutil
                proceso = psutil.Process(pid)
                proceso.kill() if fuerte else proceso.terminate()
            except Exception:
                pass
            return
        import signal
        try:
            os.kill(pid, signal.SIGKILL if fuerte else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def esta_vivo(self, nombre: str) -> bool:
        canal = self.canales.get(nombre)
        if not canal:
            return False
        if canal.es_local:
            return True          # el CLI siempre está disponible
        return bool(self._pids(canal))

    def requisitos_faltantes(self, nombre: str) -> List[str]:
        """Qué le falta a este canal para poder arrancar. Lista vacía = listo."""
        canal = self.canales.get(nombre)
        if not canal:
            return [f"canal desconocido: {nombre}"]
        faltan = []
        for check in canal.requisitos:
            try:
                problema = check()
            except Exception as e:                      # nunca romper por un check
                problema = f"no pude comprobar un requisito: {e}"
            if problema:
                faltan.append(problema)
        return faltan

    # ── Encender / apagar ────────────────────────────────────────────────────
    def activar(self, nombre: str, exclusivo: bool = True) -> Dict[str, Any]:
        """Enciende un canal. Con `exclusivo`, apaga los demás puentes.

        Devuelve un dict con `ok`, `mensaje` y el estado resultante. Nunca lanza:
        los fallos se comunican en `ok=False` + `mensaje` legible, porque esto se
        invoca desde una conversación y el usuario tiene que entender qué pasó.
        """
        canal = self.canales.get(nombre)
        if not canal:
            return {"ok": False, "mensaje": f"No conozco el canal «{nombre}».",
                    "canales": self.estado()}

        apagados: List[str] = []
        if exclusivo:
            for otro in self.canales.values():
                if otro.nombre != nombre and not otro.es_local and self.esta_vivo(otro.nombre):
                    if self.parar(otro.nombre).get("ok"):
                        apagados.append(otro.nombre)

        if canal.es_local:
            # No hay proceso que levantar: el canal local es el cliente CLI.
            self._guardar_preferencia(nombre)
            msg = ("Listo, hablamos por aquí (Termux). Ejecuta: python3 hablar.py"
                   if ES_ANDROID else "Listo, hablamos por aquí, en el chat.")
            if apagados:
                msg += f" — apagué {', '.join(apagados)} para no gastar recursos."
            return {"ok": True, "mensaje": msg, "apagados": apagados,
                    "canales": self.estado()}

        if self.esta_vivo(nombre):
            self._guardar_preferencia(nombre)
            return {"ok": True, "mensaje": f"{nombre} ya estaba encendido.",
                    "apagados": apagados, "canales": self.estado()}

        # En un PC, la librería del puente se instala sola la primera vez (en
        # el móvil se instaló a mano en su día y ahí sigue).
        if canal.modulo and canal.paquetes:
            from . import complementos
            if not complementos.esta(canal.modulo) and complementos.se_puede():
                complementos.instalar_ya(f"canal-{nombre}", canal.paquetes, canal.modulo)

        faltan = self.requisitos_faltantes(nombre)
        if faltan:
            return {"ok": False,
                    "mensaje": f"No puedo encender {nombre}: " + "; ".join(faltan),
                    "requisitos_faltantes": faltan, "canales": self.estado()}

        log_path = LOG_DIR / f"canal_{nombre}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
        if os.name == "nt":
            # Sin ventana negra, y en su propio grupo (lo de start_new_session).
            aparte = {"creationflags": 0x08000000 | 0x00000200}
        else:
            # Grupo propio: sobrevive al cierre de la sesión que lo lanzó
            # y permite matar el árbol entero sin tocar a la API.
            aparte = {"start_new_session": True}
        try:
            with open(log_path, "ab") as log:
                subprocess.Popen(
                    canal.comando or [],
                    cwd=str(canal.cwd or ROOT),
                    stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=self._entorno(),
                    **aparte,
                )
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("No pude arrancar el canal %s: %s", nombre, e)
            return {"ok": False, "mensaje": f"No pude arrancar {nombre}: {e}",
                    "canales": self.estado()}

        # Esperar a un VEREDICTO, no solo a que el proceso exista. Un puente mal
        # configurado (un intent sin activar, un token caducado) sí aparece en la
        # tabla de procesos y se muere unos segundos después: decir «encendido»
        # en ese hueco es mentirle al usuario, que se queda esperando mensajes
        # que nunca van a llegar.
        espera = 15.0 if canal.patron_listo else 5.0
        limite = time.monotonic() + espera
        vivo = listo = False
        fallo: Optional[str] = None
        while time.monotonic() < limite:
            time.sleep(0.5)
            vivo = self.esta_vivo(nombre)
            fallo = _diagnosticar(log_path)
            if fallo:
                break
            if not canal.patron_listo:
                if vivo:
                    break                    # sin marca de éxito: basta con que exista
                continue
            if vivo and re.search(canal.patron_listo, _cola_log(log_path), re.I):
                listo = True
                break
            if not vivo and _cola_log(log_path):
                break                        # llegó a escribir y ya no está: murió al nacer

        self._guardar_preferencia(nombre)
        if fallo or not vivo:
            detalle = fallo or f"Se ha caído nada más arrancar. Log: {log_path.name}"
            return {"ok": False,
                    "mensaje": f"No pude encender {nombre}. {detalle}",
                    "log": str(log_path), "diagnostico": fallo,
                    "canales": self.estado()}

        msg = f"Encendido {nombre}."
        if canal.patron_listo and not listo:
            # Vivo pero sin confirmar: no se afirma más de lo que se sabe.
            msg = (f"Encendido {nombre}, aunque todavía no ha confirmado la "
                   f"conexión. Si no responde, mírame el log {log_path.name}.")
        if apagados:
            msg += f" Apagué {', '.join(apagados)} para no gastar recursos."
        if nombre == "whatsapp":
            msg += " Si no estaba vinculado, tendrás que meter el código en el móvil."
        return {"ok": True, "mensaje": msg, "apagados": apagados,
                "log": str(log_path), "canales": self.estado()}

    def parar(self, nombre: str) -> Dict[str, Any]:
        """Apaga el puente de un canal. El canal local no se puede apagar."""
        canal = self.canales.get(nombre)
        if not canal:
            return {"ok": False, "mensaje": f"No conozco el canal «{nombre}»."}
        if canal.es_local:
            return {"ok": False,
                    "mensaje": f"{_aqui()[0].upper() + _aqui()[1:]} no se apaga: es tu forma "
                               "de llegar a mí siempre."}

        pids = self._pids(canal)
        if not pids:
            return {"ok": True, "mensaje": f"{nombre} ya estaba apagado."}

        for fuerte in (False, True):
            for pid in self._pids(canal):
                self._matar(pid, fuerte)
            time.sleep(0.5 if fuerte else 1.5)
            if not self._pids(canal):
                # Sin ese puente se vuelve a Termux, que es de donde no se sale.
                self._guardar_preferencia(CANAL_LOCAL)
                return {"ok": True, "mensaje": f"Apagado {nombre}.", "pids": pids}

        return {"ok": False, "mensaje": f"No pude apagar {nombre} (PIDs: {pids})."}

    def parar_todos(self) -> Dict[str, Any]:
        """Apaga TODOS los puentes y deja solo el canal local.

        Es el «modo mínimo consumo»: sin puentes vivos el único gasto es la API
        (que es el cerebro y no se toca) y Celestia sigue siendo alcanzable
        desde Termux con `hablar.py`. Por eso deja la preferencia en el canal
        local: refleja la verdad de por dónde se puede hablar después.
        """
        apagados, fallidos = [], []
        for canal in self.canales.values():
            if canal.es_local or not self.esta_vivo(canal.nombre):
                continue
            if self.parar(canal.nombre).get("ok"):
                apagados.append(canal.nombre)
            else:
                fallidos.append(canal.nombre)

        self._guardar_preferencia(CANAL_LOCAL)
        if fallidos:
            return {"ok": False, "apagados": apagados, "fallidos": fallidos,
                    "mensaje": f"Apagué {', '.join(apagados) or 'nada'}, pero no pude con "
                               f"{', '.join(fallidos)}.",
                    "canales": self.estado()}
        if not apagados:
            return {"ok": True, "apagados": [],
                    "mensaje": "Ya no había ningún puente encendido. "
                               f"Seguimos por aquí ({_aqui()}).",
                    "canales": self.estado()}
        return {"ok": True, "apagados": apagados,
                "mensaje": f"Apagado todo ({', '.join(apagados)}). "
                           f"Ahora mismo solo gasto lo imprescindible; "
                           + ("háblame por Termux con `python3 hablar.py`."
                              if ES_ANDROID else "háblame por el chat."),
                "canales": self.estado()}

    def _entorno(self) -> Dict[str, str]:
        """Entorno para los puentes: el actual + el .env del proyecto.

        El daemon puede haber arrancado sin `.env` cargado; sin esto un puente
        hijo no vería su token aunque esté escrito en el fichero.
        """
        env = dict(os.environ)
        try:
            for linea in ENV_FILE.read_text(encoding="utf-8").splitlines():
                linea = linea.strip()
                if linea.startswith("#") or "=" not in linea:
                    continue
                k, _, v = linea.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass
        env.setdefault("CELESTIA_TZ", "Europe/Madrid")
        # Dónde está Celestia: en un PC el puerto puede no ser el 8765 (el
        # lanzador coge el primero libre). La API lo deja dicho al arrancar.
        env.setdefault("CELESTIA_API_URL", "http://127.0.0.1:8765")
        env.setdefault("PYTHONUTF8", "1")
        if not ES_ANDROID:
            # El puente de WhatsApp: en el móvil usa /sdcard/Celestia; en un PC,
            # las carpetas de Celestia. Y el Node que se bajó, delante.
            from . import complementos
            from .paths import RECIBIDOS_DIR
            env.setdefault("CELESTIA_MOVIL_DIR", str(MEM_DIR / "puente_whatsapp"))
            env.setdefault("CELESTIA_RECIBIDOS", str(RECIBIDOS_DIR))
            env.setdefault("CELESTIA_ENV_FILE", str(ENV_FILE))
            env = complementos.entorno_node(env)
        return env

    # ── Estado y preferencia ─────────────────────────────────────────────────
    def estado(self) -> List[Dict[str, Any]]:
        """Foto de todos los canales: vivo, disponible y qué le falta a cada uno."""
        preferido = self.preferencia()
        salida = []
        for canal in self.canales.values():
            faltan = [] if canal.es_local else self.requisitos_faltantes(canal.nombre)
            salida.append({
                "nombre":      canal.nombre,
                "descripcion": canal.descripcion,
                "activo":      self.esta_vivo(canal.nombre),
                "local":       canal.es_local,
                "listo":       not faltan,
                "falta":       faltan,
                "preferido":   canal.nombre == preferido,
            })
        return salida

    def preferencia(self) -> str:
        """Último canal que el usuario eligió (por defecto, el local)."""
        try:
            datos = json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
            canal = datos.get("preferido")
            if canal in self.canales:
                return canal
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return CANAL_LOCAL

    def _guardar_preferencia(self, nombre: str) -> None:
        """Anota por dónde se está hablando ahora.

        Es solo un registro para poder decírselo al usuario: quién está vivo de
        verdad se le pregunta siempre al sistema operativo. Nadie restaura esto
        al arrancar —la máquina siempre empieza en Termux, sin puentes— porque
        un canal encendido es batería y RAM del móvil, y eso solo se gasta
        cuando alguien lo pide.
        """
        try:
            ESTADO_FILE.write_text(json.dumps(
                {"preferido": nombre,
                 "actualizado": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("No pude guardar la preferencia de canal: %s", e)

    # ── Configuración dictada en la conversación ─────────────────────────────
    def guardar_token(self, canal: str, token: str) -> Dict[str, Any]:
        """Escribe el token de un canal en el `.env` y lo deja usable ya.

        El usuario no debería tener que editar ficheros para poder hablar por
        Telegram o Discord: dice el token en el chat y aquí queda guardado.
        El valor NUNCA se escribe en los logs.
        """
        clave = TOKEN_POR_CANAL.get(canal)
        if not clave:
            return {"ok": False, "mensaje": f"{canal} no usa ningún token."}

        env_file = ENV_FILE
        no_es_el_env_real(env_file)
        try:
            lineas = env_file.read_text(encoding="utf-8").splitlines() \
                if env_file.exists() else []
            reemplazada = False
            for i, linea in enumerate(lineas):
                if linea.strip().split("=", 1)[0].strip() == clave:
                    lineas[i] = f"{clave}={token}"
                    reemplazada = True
                    break
            if not reemplazada:
                lineas.append(f"{clave}={token}")
            # Escritura atómica: si algo falla a medias, el .env no queda roto.
            tmp = env_file.with_suffix(".env.tmp")
            tmp.write_text("\n".join(lineas) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, env_file)
        except OSError as e:
            logger.error("No pude guardar el token de %s: %s", canal, e)
            return {"ok": False,
                    "mensaje": f"No pude guardar el token de {canal}: {e}"}

        # Disponible ya en este proceso: sin esto habría que reiniciar el daemon
        # para que el puente heredara el token.
        os.environ[clave] = token
        logger.info("Token de %s guardado en .env (valor no registrado).", canal)

        faltan = self.requisitos_faltantes(canal)
        if faltan:
            return {"ok": True,
                    "mensaje": (f"Token de {canal} guardado. Todavía falta: "
                                + "; ".join(faltan)),
                    "requisitos_faltantes": faltan}
        return {"ok": True,
                "mensaje": (f"Token de {canal} guardado — ya puedo encender ese "
                            f"canal. Dime «hablemos por {canal}» cuando quieras.\n"
                            "(Si me lo has mandado por un chat, borra ese mensaje: "
                            "un token da control total del bot.)")}

    # ── Texto para el usuario ────────────────────────────────────────────────
    def resumen(self) -> str:
        """Estado de los canales en una frase por canal, para responder en chat."""
        lineas = []
        for c in self.estado():
            if c["activo"]:
                marca = "🟢 activo"
            elif c["listo"]:
                marca = "⚪ apagado (listo para encender)"
            else:
                marca = "🔴 sin configurar"
            linea = f"• {c['nombre']} — {marca}"
            if c["falta"]:
                linea += f" · {'; '.join(c['falta'])}"
            lineas.append(linea)
        return "Mis canales ahora mismo:\n" + "\n".join(lineas)
