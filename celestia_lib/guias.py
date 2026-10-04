"""Poner en marcha algo hablando: Celestia hace lo que puede y pide lo demás.

Enzo (4 sep 2026): «me gustaría que nadie tenga que saber cómo, solo dar la
idea y que Celestia la haga realidad — tipo "quiero que hablemos por WhatsApp":
que haga los pasos que ella puede sin necesidad de nadie, y en cuanto necesite
que el usuario haga algo, que se lo pida».

Cómo funciona: cada procedimiento es una lista de pasos, y cada paso sabe **tres
cosas** — si ya está hecho, cómo hacerlo (si puede ella) y qué pedir (si no).
Avanzar es recorrerlos: los que puede, los hace y sigue sin decir nada; en el
primero que necesita a una persona, para y pide UNA cosa, con las palabras
exactas. Cuando esa persona la hace, se retoma por donde iba.

Dos decisiones que hacen que esto no sea un adorno:

1. **Comprobar, no suponer.** Un paso no se da por hecho porque el comando
   saliera bien, sino porque se vuelve a mirar el mundo (¿existe la carpeta?,
   ¿vive el proceso?, ¿lo dice el log?). Si no, «configurado» acaba
   significando «se ejecutó algo» y el usuario se queda con un canal muerto.
2. **Nada de LLM aquí.** Es infraestructura y tiene que funcionar igual con el
   modelo pequeño, con otro proveedor o sin conexión — la misma razón por la
   que cambiar de canal se resuelve con regex (ver `canales.py`).

El estado vive en disco: media configuración sobrevive a que Celestia se
reinicie, que es justo lo que pasa mientras se instala algo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("celestia_v1")

RAIZ = Path(__file__).resolve().parent.parent
ESTADO = RAIZ / "logs" / "guia_en_curso.json"
CADUCA = 3600            # una configuración a medias no dura para siempre


@dataclass
class Paso:
    """Un paso: si está hecho, cómo hacerlo y qué pedir cuando toca a la persona."""
    id: str
    titulo: str                                   # qué es, en una línea
    comprobar: Callable[[], bool]                 # ¿ya está?
    hacer: Optional[Callable[[], str]] = None     # lo hace Celestia; devuelve nota
    pedir: str = ""                               # lo que tiene que hacer la persona
    y_luego: str = ""                             # cómo avisar de que ya está
    tarda: bool = False                           # avisar de que va para largo


@dataclass
class Guia:
    canal: str
    titulo: str
    pasos: List[Paso] = field(default_factory=list)


@dataclass
class Avance:
    """Lo que ha pasado al avanzar, y qué toca ahora."""
    terminada: bool
    mensaje: str
    hechos: List[str] = field(default_factory=list)
    esperando: str = ""          # id del paso que necesita a la persona


# ── Estado en disco ─────────────────────────────────────────────────────────

def _leer_estado() -> dict:
    try:
        d = json.loads(ESTADO.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) > CADUCA:
            return {}
        return d
    except Exception:
        return {}


def _guardar_estado(canal: str, esperando: str = "") -> None:
    try:
        ESTADO.parent.mkdir(parents=True, exist_ok=True)
        ESTADO.write_text(json.dumps(
            {"canal": canal, "esperando": esperando, "ts": time.time()},
            ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("No pude guardar la guía en curso: %s", e)


def en_curso() -> Optional[str]:
    """Qué canal se está configurando ahora mismo, si es que hay alguno."""
    return _leer_estado().get("canal") or None


def cancelar() -> None:
    try:
        ESTADO.unlink()
    except OSError:
        pass


# ── Herramientas de los pasos ───────────────────────────────────────────────

_procesos: Dict[str, subprocess.Popen] = {}


def _lanzar(paso_id: str, orden: List[str], cwd: Path, env: Optional[dict] = None) -> str:
    """Arranca algo largo (npm, pip) sin bloquear la conversación.

    Bloquear tres minutos esperando a `npm install` deja a quien pregunta
    mirando una pantalla parada; se lanza y el siguiente paso lo comprueba.
    """
    vivo = _procesos.get(paso_id)
    if vivo and vivo.poll() is None:
        return "sigue en marcha"
    log = RAIZ / "logs" / f"guia_{paso_id}.log"
    extra = {"creationflags": 0x08000000} if os.name == "nt" else {}   # sin ventana negra
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as f:
            _procesos[paso_id] = subprocess.Popen(
                orden, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, **extra)
        return "lo he puesto en marcha"
    except Exception as e:
        return f"no pude lanzarlo: {e}"


# ── Lo que en un PC Celestia se pone sola (complementos.py) ─────────────────
# En el móvil (Termux) Node y las librerías se instalaban a mano o con el
# `python3`/`npm` del sistema; en un PC no hay ni lo uno ni lo otro, y en la
# app de Android no se puede lanzar nada.

def _es_app() -> bool:
    return os.environ.get("CELESTIA_APP_ANDROID") == "1"


def _paso_node() -> Paso:
    from . import complementos
    if complementos.se_puede():
        def bajar() -> str:
            estado = complementos.instalar_node()
            return {"listo": "", "instalando": "me lo estoy bajando (unos 30 MB)",
                    "fallo": "no he podido bajarlo, ¿hay internet?"}.get(estado, estado)
        return Paso("node", "Node.js", comprobar=lambda: complementos.ruta_node() is not None,
                    hacer=bajar, tarda=True)
    if _es_app():
        return Paso("node", "Node.js", comprobar=lambda: False,
                    pedir="Desde la app de Android no puedo encender WhatsApp: el puente "
                          "necesita Node.js y aquí dentro no se puede. Hazlo desde la "
                          "Celestia del ordenador (o la de Termux) y me escribes igual.")
    return Paso("node", "Node.js",
                comprobar=lambda: complementos.ruta_node() is not None,
                pedir="Me falta Node.js y este es el único paso que no puedo dar yo: "
                      "el gestor de paquetes de Termux no me deja instalar nada desde "
                      "aquí dentro. En una ventana de Termux (fuera del contenedor): "
                      "`pkg install nodejs`.",
                y_luego="Cuando lo tengas, dime «ya está» y sigo.")


def _instalar_npm(wa: Path) -> str:
    from . import complementos
    npm = complementos.ruta_npm()
    if not npm:
        return "no encuentro npm"
    return _lanzar("npm", [npm, "install", "--no-audit", "--no-fund"], wa,
                   env=complementos.entorno_node())


def _instalar_modulo(nombre: str, paquete: str, modulo: str) -> str:
    """La librería de un puente: con el pip de esta Celestia (PC) o con el
    python3 del sistema (Termux)."""
    from . import complementos
    if complementos.se_puede():
        estado = complementos.instalar(f"canal-{nombre}", [paquete], modulo)
        return {"listo": "", "instalando": "la estoy instalando",
                "fallo": "no he podido instalarla, ¿hay internet?"}.get(estado, estado)
    return _lanzar(f"pip_{nombre}", ["python3", "-m", "pip", "install", "--quiet", paquete], RAIZ)


def _hay_modulo(nombre: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(nombre) is not None
    except (ImportError, ValueError):
        return False


def _ultimo_log(patron: str) -> Optional[Path]:
    logs = sorted((RAIZ / "logs").glob(patron), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    return logs[0] if logs else None


def _codigo_whatsapp() -> Optional[str]:
    """El código de vinculación que el puente deja en su log."""
    log = _ultimo_log("canal_whatsapp*.log") or _ultimo_log("*whatsapp*.log")
    if not log:
        return None
    # Solo el final: el código lo escribe el puente al arrancar y se repite en
    # cada intento, así que lo último es lo que vale. Leer el log entero para
    # tirar todo menos 8 KB es el patrón que costó 268 MB de pico en
    # `model._get_few_shot` (sesión 58); aquí no llega a tanto, pero tampoco
    # hay motivo para dejarlo puesto.
    from .canales import _cola_log
    texto = _cola_log(log)
    if not texto:
        return None
    m = None
    for m in re.finditer(r"CÓDIGO(?:\s+CELESTIA)?:\s*([A-Z0-9-]{6,12})", texto):
        pass                                  # el último es el que vale
    return m.group(1).strip() if m else None


def _numero_guardado() -> str:
    try:
        return re.sub(r"\D", "", (RAIZ / "whatsapp_bridge" / "numero.txt")
                      .read_text(encoding="utf-8"))
    except OSError:
        return ""


def guardar_numero(numero: str) -> bool:
    """El número al que Celestia se vincula y desde el que le hablas."""
    limpio = re.sub(r"\D", "", str(numero or ""))
    if not 8 <= len(limpio) <= 15:            # ni un dígito suelto ni un disparate
        return False
    try:
        (RAIZ / "whatsapp_bridge" / "numero.txt").write_text(limpio + "\n",
                                                             encoding="utf-8")
        return True
    except OSError as e:
        logger.warning("No pude guardar el número: %s", e)
        return False


def _whatsapp_vinculado() -> bool:
    # Las credenciales en disco son la prueba de que la vinculación se hizo:
    # el log se rota y «lo puse en marcha» no significa que quedara vinculado.
    return (RAIZ / "whatsapp_bridge" / "auth" / "creds.json").is_file()


# ── Las guías ───────────────────────────────────────────────────────────────

def _guia_whatsapp(gestor) -> Guia:
    wa = RAIZ / "whatsapp_bridge"

    def pedir_codigo() -> str:
        codigo = _codigo_whatsapp()
        if not codigo:
            return ("Estoy pidiéndole el código a WhatsApp; dame unos segundos y "
                    "dime «ya» para que te lo dé.")
        return (f"Coge tu móvil y abre WhatsApp → Ajustes → **Dispositivos "
                f"vinculados** → *Vincular un dispositivo* → *Vincular con el "
                f"número de teléfono*. Ahí mete este código:\n\n    {codigo}\n\n"
                "Tienes un par de minutos antes de que caduque.")

    return Guia("whatsapp", "hablar por WhatsApp", [
        _paso_node(),
        Paso("dependencias", "las librerías del puente",
             # npm escribe este índice al TERMINAR: con la carpeta a medias
             # (instalación en curso o cortada) el puente no arrancaría.
             comprobar=lambda: (wa / "node_modules" / ".package-lock.json").is_file(),
             hacer=lambda: _instalar_npm(wa),
             tarda=True),
        Paso("numero", "tu número",
             comprobar=lambda: bool(_numero_guardado()),
             pedir="Dime tu número de WhatsApp con el prefijo del país, así: "
                   "«mi número es 34600111222»."),
        Paso("puente", "el puente en marcha",
             comprobar=lambda: gestor.esta_vivo("whatsapp"),
             hacer=lambda: gestor.activar("whatsapp").get("mensaje", "")),
        Paso("vinculado", "vincular con tu WhatsApp",
             comprobar=_whatsapp_vinculado,
             pedir=pedir_codigo,
             y_luego="En cuanto lo metas, quedamos vinculados y te escribo por ahí."),
    ])


def _guia_telegram(gestor) -> Guia:
    from . import acceso
    return Guia("telegram", "hablar por Telegram", [
        Paso("token", "el token del bot",
             comprobar=lambda: bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
             pedir="Abre Telegram y escríbele a **@BotFather**. Mándale `/newbot` y "
                   "contesta a sus dos preguntas (un nombre y un usuario que acabe "
                   "en «bot»). Te devolverá un token largo: pégamelo aquí tal cual, "
                   "con un «mi token de Telegram es …» delante.",
             y_luego="En cuanto me lo pases, sigo yo con lo demás."),
        Paso("modulo", "la librería de Telegram",
             comprobar=lambda: _hay_modulo("telegram"),
             hacer=lambda: _instalar_modulo("telegram", "python-telegram-bot>=21.0", "telegram"),
             tarda=True),
        Paso("puente", "el puente en marcha",
             comprobar=lambda: gestor.esta_vivo("telegram"),
             hacer=lambda: gestor.activar("telegram").get("mensaje", "")),
        Paso("emparejado", "que sepa que ese eres tú",
             comprobar=lambda: bool(acceso.permitidos("telegram")),
             hacer=lambda: (acceso.abrir_emparejamiento("telegram"),
                            "te espero cinco minutos")[1],
             pedir="Ya está en marcha. Escríbele algo a tu bot desde Telegram —un "
                   "«hola» vale—. Con ese mensaje sé que eres tú y te apunto; "
                   "después de eso, nadie más podrá hablarme por ahí.",
             y_luego="Te contesto por Telegram en cuanto lo hagas."),
    ])


def _guia_discord(gestor) -> Guia:
    from . import acceso
    return Guia("discord", "hablar por Discord", [
        Paso("token", "el token del bot",
             comprobar=lambda: bool(os.environ.get("DISCORD_BOT_TOKEN", "").strip()),
             pedir="Entra en discord.com/developers/applications → *New "
                   "Application* → pestaña **Bot** → *Reset Token* y copia el que "
                   "te dé. Pégamelo aquí: «mi token de Discord es …». Y ya que "
                   "estás, en esa misma pantalla activa **MESSAGE CONTENT "
                   "INTENT**, o el bot no podrá leer lo que le escribas."),
        Paso("modulo", "la librería de Discord",
             comprobar=lambda: _hay_modulo("discord"),
             hacer=lambda: _instalar_modulo("discord", "discord.py", "discord"),
             tarda=True),
        Paso("puente", "el puente en marcha",
             comprobar=lambda: gestor.esta_vivo("discord"),
             hacer=lambda: gestor.activar("discord").get("mensaje", "")),
        Paso("emparejado", "que sepa que ese eres tú",
             comprobar=lambda: bool(acceso.permitidos("discord")),
             hacer=lambda: (acceso.abrir_emparejamiento("discord"),
                            "te espero cinco minutos")[1],
             pedir="Invita al bot a tu servidor (o escríbele por privado) y mándale "
                   "un mensaje. Con eso te apunto y dejo el canal cerrado a los demás.",
             y_luego="Te contesto por Discord en cuanto lo hagas."),
    ])


_GUIAS = {"whatsapp": _guia_whatsapp, "telegram": _guia_telegram,
          "discord": _guia_discord}


def hay_guia(canal: str) -> bool:
    return (canal or "").strip().lower() in _GUIAS


# ── Avanzar ─────────────────────────────────────────────────────────────────

def _texto(valor) -> str:
    """`pedir` puede ser un texto fijo o algo que se calcula al momento (el
    código de WhatsApp cambia en cada intento)."""
    try:
        return valor() if callable(valor) else (valor or "")
    except Exception as e:
        logger.warning("No pude preparar la instrucción: %s", e)
        return ""


def _cumplido(paso: Paso) -> bool:
    try:
        return bool(paso.comprobar())
    except Exception as e:                      # un check roto no tumba la guía
        logger.warning("Fallo comprobando «%s»: %s", paso.id, e)
        return False


def avanzar(canal: str, gestor=None) -> Avance:
    """Hace todo lo que puede y para en lo primero que necesita a la persona."""
    canal = (canal or "").strip().lower()
    constructor = _GUIAS.get(canal)
    if not constructor:
        return Avance(False, f"No sé poner en marcha «{canal}» todavía.")
    if gestor is None:
        from .canales import GestorCanales
        gestor = GestorCanales()

    guia = constructor(gestor)
    hechos: List[str] = []

    for paso in guia.pasos:
        if _cumplido(paso):
            continue

        if paso.hacer:
            try:
                nota = paso.hacer() or ""
            except Exception as e:
                nota = f"no salió: {e}"
                logger.warning("Fallo haciendo «%s»: %s", paso.id, e)
            hechos.append(f"{paso.titulo} ({nota})" if nota else paso.titulo)
            if _cumplido(paso):
                continue
            if paso.tarda and not paso.pedir:
                _guardar_estado(canal, paso.id)
                return Avance(False, _mensaje(guia, hechos, esperando_a_que_acabe=paso),
                              hechos, paso.id)

        instruccion = _texto(paso.pedir)
        if instruccion:
            _guardar_estado(canal, paso.id)
            return Avance(False, _mensaje(guia, hechos, paso=paso,
                                          instruccion=instruccion), hechos, paso.id)

        # Ni sé hacerlo ni sé qué pedir: decirlo, en vez de dar por hecho que va.
        _guardar_estado(canal, paso.id)
        return Avance(False,
                      f"Me he quedado atascada en «{paso.titulo}» y no sé seguir "
                      f"sola. Si me dices qué ves, lo miramos.", hechos, paso.id)

    cancelar()
    return Avance(True, f"Listo: ya podemos {guia.titulo}.", hechos)


def _mensaje(guia: Guia, hechos: List[str], paso: Optional[Paso] = None,
             instruccion: str = "", esperando_a_que_acabe: Optional[Paso] = None) -> str:
    """Lo que se le dice a la persona: qué he hecho y qué necesito de ti.

    Con las dos partes separadas a propósito: mezclar «he hecho A, B y C» con
    «ahora tú haz D» en un párrafo hace que D se pierda, y D es lo único que
    hay que hacer.
    """
    partes = []
    if hechos:
        partes.append("Ya está hecho: " + "; ".join(hechos) + ".")
    if esperando_a_que_acabe is not None:
        partes.append(f"Estoy con «{esperando_a_que_acabe.titulo}», que tarda un "
                      "poco. Dime «ya» en un minuto y sigo.")
        return "\n\n".join(partes)
    if instruccion:
        partes.append("Ahora te toca a ti:\n" + instruccion)
        if paso is not None and paso.y_luego:
            partes.append(paso.y_luego)
        else:
            partes.append("Cuando lo tengas, dime «ya está» y sigo yo.")
    return "\n\n".join(partes)


# ── Retomar: «ya está», «hecho», «sigue» ────────────────────────────────────
# Determinista, como todo lo de infraestructura: tiene que funcionar igual con
# el modelo pequeño o sin conexión.
_SEGUIR_RE = re.compile(
    r"^\s*(?:ya\s*(?:est[áa]|lo\s*hice|lo\s*tengo)?|hecho|listo|vale\s*ya|"
    r"sigue|contin[úu]a|adelante|dale|ok(?:ey)?\s*ya)\s*[.!]?\s*$", re.I)


def pide_seguir(texto: str) -> bool:
    return bool(_SEGUIR_RE.match(texto or ""))


# «mi número es 34600111222», «mi numero: +34 600 11 12 22»
_NUMERO_RE = re.compile(
    r"\bmi\s+(?:n[úu]mero|tel[ée]fono|m[óo]vil)\b[^0-9+]{0,12}(\+?[\d\s().-]{8,20})",
    re.I)


def detectar_numero(texto: str) -> Optional[str]:
    """El número que alguien dicta en la conversación, si lo dicta.

    Exige el «mi número es» delante a propósito: sin eso, cualquier cifra larga
    de una charla (un precio, un año, un DNI) acabaría guardada como teléfono.
    """
    m = _NUMERO_RE.search(texto or "")
    if not m:
        return None
    limpio = re.sub(r"\D", "", m.group(1))
    return limpio if 8 <= len(limpio) <= 15 else None
