"""Jugar por Enzo — dos carriles, porque un solo bucle no sirve para todo.

Enzo (6 sep 2026): «quiero que juegue por mí en un juego», y al rato,
«habrá juegos que se necesiten agilidad y rapidez, por ejemplo el Zenless
Zone Zero».

Las dos frases juntas son el diseño entero. Un juego por turnos y un juego de
acción no se juegan con la misma máquina:

  · **Piloto** (lento, con cabeza). Captura la pantalla, la entiende, decide y
    actúa. Un ciclo cuesta segundos: la visión hay que pagarla, esté el modelo
    en el móvil o en la red. Sirve para menús, recompensas diarias, farmeo,
    ajedrez, cartas — todo lo que espera por ti.

  · **Reflejos** (rápido, sin cabeza). Mira UNOS POCOS PÍXELES concretos y
    dispara una acción ya decidida. Ni modelo, ni red, ni JSON. El objetivo son
    milisegundos.

Y la regla que las separa, que es lo único importante de este fichero:

    **El modelo NUNCA está dentro del bucle rápido.**

No es pereza de optimización, es aritmética. En ZZZ la ventana de una esquiva
perfecta ronda los 300 ms. Solo *mirar* con un modelo de visión cuesta entre 1
y 3 segundos. Ninguna optimización cierra un hueco de diez veces. Lo que sí se
puede hacer —y es como funcionan los bots de verdad— es que el modelo decida
ANTES qué mirar y qué pulsar, y luego se aparte: deja escrito un `Reflejo` y el
bucle lo ejecuta solo.

De ahí que la percepción venga en dos sabores: `Ojo` (cara, entiende) y
`Retina` (barata, solo mira píxeles sueltos y no entiende nada).

Lo que este fichero NO hace, y conviene decirlo antes de que alguien se ilusione:
no convierte el móvil en un jugador de élite. Con el juego comiéndose la CPU y
la GPU al lado, los reflejos van a llegar tarde a veces. Sirve para lo repetitivo
y para lo que perdona; en un jefe difícil, no. Por eso `Cronometro` mide el ciclo
de verdad en cada aparato y `veredicto()` dice a la cara qué se puede jugar aquí
— antes de empezar, no después de perder la partida.

Ver [[proyecto_control_universal]] (de dónde salen los mandos) y
[[proyecto_pedirle_cualquier_cosa]] (los frenos: un «sí o sí» sin topes miente).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import select
import shutil
import struct
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .paths import MOVIL_DIR, SIN_MOVIL

logger = logging.getLogger("celestia_v1")

# Ruta absoluta a rish: el bridge aprendió por las malas que el PATH no está
# garantizado según desde dónde se lance el proceso (ver bridge.js).
RISH_BIN = "/data/data/com.termux/files/usr/bin/rish"
if SIN_MOVIL:
    # Copia examinándose: ninguna orden puede llegar al móvil de verdad.
    RISH_BIN = "/nonexistent/celestia-sin-movil/rish"

# Un tap por `input` arranca una JVM entera. Esto es un hecho medido, no una
# estimación: por eso el carril rápido evita `input` cuando puede.
COSTE_INPUT_TAP_MS = 200.0


# ─────────────────────────────────────────────────────────────────────────
#  Coordenadas: siempre normalizadas de puertas afuera
# ─────────────────────────────────────────────────────────────────────────
# Regla heredada de [[proyecto_modelo_despliegue]]: nada de valores escritos a
# mano para MI pantalla. Un reflejo definido en el móvil de Enzo tiene que
# valer en el PC de otro, así que todo lo que se guarda va en 0..1 y se
# convierte a píxeles en el último momento, con la resolución del aparato.

@dataclass(frozen=True)
class Punto:
    """Un punto de la pantalla en fracción (0..1), no en píxeles."""
    x: float
    y: float

    def pixeles(self, ancho: int, alto: int) -> Tuple[int, int]:
        return (int(round(self.x * ancho)), int(round(self.y * alto)))


@dataclass(frozen=True)
class Zona:
    """Un rectángulo en fracción (0..1). `centro()` es dónde se toca."""
    x: float
    y: float
    ancho: float
    alto: float

    def centro(self) -> Punto:
        return Punto(self.x + self.ancho / 2, self.y + self.alto / 2)

    def pixeles(self, ancho: int, alto: int) -> Tuple[int, int, int, int]:
        return (int(round(self.x * ancho)), int(round(self.y * alto)),
                max(1, int(round(self.ancho * ancho))),
                max(1, int(round(self.alto * alto))))

    def contiene(self, p: Punto) -> bool:
        return (self.x <= p.x <= self.x + self.ancho and
                self.y <= p.y <= self.y + self.alto)


# ─────────────────────────────────────────────────────────────────────────
#  Los frenos
# ─────────────────────────────────────────────────────────────────────────
# La mitad del trabajo, como en empeno.py. Un bucle que toca la pantalla sola
# durante horas sin topes es una forma cara de vaciar una batería y, en un
# juego con compras, de vaciar algo peor.

# Palabras que jamás se tocan sin permiso explícito. Un TAP a ciegas sobre un
# botón de compra gasta dinero real de Enzo, y el juego está DISEÑADO para que
# ese botón sea el más grande y llamativo de la pantalla — justo el que un
# modelo de visión elige cuando duda.
# Apps del sistema que NUNCA se apartan ni se tocan: son decisiones de Enzo,
# no del jugador. Sesión 68, jugando a Soul Knight: el juego lanzó el
# consentimiento de cuenta de Google (`com.google.android.gms…ConsentActivity`)
# y el jugador iba a hacer lo de siempre — abrir el juego por encima con
# `monkey`, dejando el consentimiento a medias, y entre comprobación y
# comprobación tocar sobre él. Un «Aceptar» en una pantalla de cuenta, de
# permisos o de la tienda no lo firma nadie por Enzo.
#
# Aquí NO se aparta la pantalla: se PARA la partida y se le dice.
APPS_INTOCABLES = (
    "com.google.android.gms",              # cuentas, consentimientos, login
    "com.android.vending",                 # Play Store: compras y suscripciones
    "com.android.settings",                # ajustes del móvil
    "com.android.permissioncontroller",    # el diálogo de permisos
    "com.google.android.packageinstaller",
    "com.android.packageinstaller",        # instalar/desinstalar
)

# Lo menos que dura una pelea. Por debajo de esto no es pelear, es un toque
# suelto: ver `MINIMO_PELEA_MS` en `_mirar_y_decidir`.
MINIMO_PELEA_MS = 4000

PALABRAS_PROHIBIDAS = (
    "comprar", "compra", "pagar", "pago", "suscri", "premium", "recarga",
    "recargar", "tarjeta", "paypal", "google play", "precio", "oferta",
    "buy", "purchase", "subscribe", "checkout", "confirmar pedido",
)
# Un símbolo de moneda **con un número al lado**. El número no es un adorno:
# 🔴 sesión 66, en una partida real el OCR leyó «€)» sobre el arte del juego y
# el freno cerró la partida entera («iba a tocar €), y eso puede costarte
# dinero») sin que hubiera ningún precio en ninguna parte. Un precio de verdad
# siempre trae cifra —«4,99 €», «$9.99», «1200 EUR»—, así que exigirla quita
# ese falso positivo sin aflojar el freno donde importa.
_MONEDAS_RE = re.compile(
    r"(?:[€$£¥]\s*\d|\d\s*[€$£¥]|\b\d[\d.,]*\s*(?:EUR|USD)\b|"
    r"\b(?:EUR|USD)\s*\d)", re.I)

# Y lo que no cuesta dinero pero tampoco se firma por otro: cuentas, permisos,
# condiciones. Van FRASES y no palabras sueltas a propósito — «aceptar» y
# «permitir» solos aparecen en cualquier menú de recompensas, y un freno que
# salta ahí corta partidas buenas (que es lo que pasó en S66 con «€)»).
_CUENTA_RE = re.compile(
    r"(?i)(iniciar\s+sesi[oó]n|sign\s*-?\s*in|log\s*in|vincular\s+cuenta|"
    r"link\s+(?:your\s+)?account|permitir\s+(?:el\s+)?acceso|allow\s+access|"
    r"conceder\s+permiso|grant\s+permission|acepto\s+(?:los\s+)?t[eé]rminos|"
    r"terms\s+of\s+(?:service|use)|pol[ií]tica\s+de\s+privacidad|"
    r"privacy\s+policy|consentimiento|\bconsent\b|continuar\s+con\s+google|"
    r"continue\s+with\s+google)")


# Directorios de cuaderno que ya se han intentado crear. Ver `escribe_apunte`.
_CUADERNOS_LISTOS: set = set()


def escribe_apunte(ruta: Path, linea: str) -> None:
    """Añade una línea al cuaderno de la sesión sin morir en el intento.

    S75: `practicar_equipo.py` se cayó a media práctica con
    `OSError: [Errno 38] Function not implemented: '/root/Celestia/logs'`, y
    con él la práctica entera. No fue el disco ni los permisos: fue
    `mkdir(exist_ok=True)`, que para saber si el directorio ya está pregunta
    `is_dir()` → `stat()`, y en este PRoot esa llamada falla de vez en cuando
    aunque el directorio lleve meses ahí. Es la misma avería que tumbaba el
    freno de parada en S68 (ver `Limites.debo_parar`), sólo que aquí estaba
    escondida en la función de apuntar, que se llama en cada vuelta: una
    práctica larga tenía miles de oportunidades de morir por escribir una
    línea de texto.

    Por eso el directorio se intenta crear **una sola vez** por ejecución, y
    un apunte que no se puede escribir no tumba nada: se pierde la línea, que
    ya ha salido por pantalla de todos modos, no el trabajo de la sesión.
    """
    try:
        carpeta = ruta.parent
        if carpeta not in _CUADERNOS_LISTOS:
            _CUADERNOS_LISTOS.add(carpeta)
            try:
                carpeta.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # casi siempre ya existe; si no, lo dirá el open de abajo
        with ruta.open("a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError as e:
        logger.debug("no pude apuntar en %s (%s)", ruta, e)


def freno_pisado(parar_si: Optional[Callable[[], bool]]) -> bool:
    """¿Está pisado el freno de parada? Si no se puede saber, se da por pisado.

    S68: `parar_si=lambda: PARAR.exists()` lanzaba `OSError: [Errno 38]` en
    este PRoot en vez de devolver `False`, y la excepción tumbaba la partida
    entera con un traceback. Ver `Limites.debo_parar` para la historia larga.
    Vive suelta aquí porque el aprendiz recibe el `parar_si` a pelo, sin
    `Limites` alrededor, y necesita exactamente el mismo blindaje.
    """
    if not parar_si:
        return False
    try:
        return bool(parar_si())
    except Exception as e:
        logger.warning("no pude comprobar el freno de parada (%s) — paro por "
                       "si acaso", e)
        return True


@dataclass
class Limites:
    """Cuándo parar. Sin esto, «juega por mí» es «gasta por mí»."""
    max_jugadas: int = 200
    max_segundos: float = 600.0
    # Si dos capturas seguidas son idénticas N veces, algo va mal: o la
    # pantalla está congelada, o estamos tocando donde no hay nada.
    max_sin_cambio: int = 8
    # Cuántos tropiezos SEGUIDOS se aguantan antes de dejarlo. Un tropiezo es
    # una vuelta que no llegó a jugada: la captura falló, o no se supo qué
    # hacer con lo que había. Antes CUALQUIERA de las dos cerraba la partida
    # en el acto, y eso no es jugar como juega una persona: nadie cierra el
    # juego porque una pantalla le pille a contrapié — espera un segundo,
    # vuelve a mirar, y sólo lo deja si aquello no avanza. Enzo, 7 sep 2026:
    # «quiero que juegue como una persona normal lo haría».
    max_tropiezos: int = 5
    permitir_compras: bool = False
    # Enganche para que el usuario pueda decir «para» desde el chat.
    parar_si: Optional[Callable[[], bool]] = None
    # Por debajo de esta carga (%), se deja de jugar. 0 lo apaga. Jugar hasta
    # dejar el móvil a cero no es jugar, es tirarlo: cuando se apaga por
    # batería no hay guardián que lo levante, y Enzo se queda sin teléfono.
    bateria_minima: int = 15

    def debo_parar(self) -> bool:
        """¿Me han dicho que pare? Si no se puede saber, se para.

        S68: el freno era `parar_si=lambda: PARAR.exists()`, y en este PRoot
        `Path.exists()` no devuelve `False` cuando el fichero no está —lanza
        `OSError: [Errno 38] Function not implemented`—. La excepción subía
        por `jugar()` y tumbaba la partida con un traceback; el guardián de
        fuera la relanzaba, y así **94 veces en 6 horas** mientras nadie
        miraba. Un freno que revienta al pisarlo no es un freno.

        Por eso el fallo se cuenta como «para», no como «sigue»: si no puedo
        comprobar si me han dicho que pare, lo caro es equivocarse hacia
        seguir. Cortar una partida de más sale gratis; jugar solo y sin
        frenos, no.
        """
        return freno_pisado(self.parar_si)

    def texto_peligroso(self, texto: str) -> bool:
        """¿Este texto huele a que tocarlo cuesta dinero?"""
        if self.permitir_compras:
            return False
        t = _sin_acentos(texto.lower())
        if any(p in t for p in PALABRAS_PROHIBIDAS):
            return True
        if _CUENTA_RE.search(texto):
            return True
        return bool(_MONEDAS_RE.search(texto))


class Parada(Exception):
    """Se acabó la partida, y el motivo va dentro."""


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


# ─────────────────────────────────────────────────────────────────────────
#  Los mandos: el mismo juego, distinto aparato
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Captura:
    """Lo que se ve. `crudo` son píxeles RGBA sin comprimir si los hay.

    Los dos formatos existen por una razón de velocidad: comprimir a PNG y
    volver a descomprimirlo para mirar cuatro píxeles es tirar decenas de
    milisegundos a la basura, y el carril rápido no los tiene.
    """
    ancho: int
    alto: int
    png: Optional[bytes] = None
    crudo: Optional[bytes] = None      # RGBA, ancho*alto*4
    ts: float = field(default_factory=time.time)

    def pixel(self, x: int, y: int) -> Optional[Tuple[int, int, int]]:
        """Color de un píxel. Solo funciona sobre `crudo` (que es el rápido)."""
        if not self.crudo:
            return None
        i = (y * self.ancho + x) * 4
        if i < 0 or i + 3 > len(self.crudo):
            return None
        return (self.crudo[i], self.crudo[i + 1], self.crudo[i + 2])


def _comprimir(cap: Captura, calidad: int = 70) -> Optional[bytes]:
    """Los píxeles crudos a JPEG, aquí dentro. `None` si no se puede.

    JPEG y no PNG por lo que cuesta cada uno con la misma pantalla: 74 ms
    frente a 198, y 156 KB frente a 1.346. Al otro lado da igual el formato y
    casi da igual el tamaño —una pantalla son 1.024 tokens de entrada se mande
    como se mande, medido—, así que lo único que decide es lo que tarda.
    """
    if not cap.crudo:
        return None
    try:
        import io
        from PIL import Image
        im = Image.frombytes("RGBA", (cap.ancho, cap.alto), cap.crudo).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=calidad)
        return buf.getvalue()
    except Exception as e:
        logger.warning("no pude comprimir la captura aquí: %s", e)
        return None


class Mando:
    """Lo que hay que saber hacer para jugar, sea el aparato que sea."""

    nombre = "mando"

    def disponible(self) -> Tuple[bool, str]:
        """(puedo, por qué no). El «por qué no» se le dice al usuario tal cual."""
        raise NotImplementedError

    def resolucion(self) -> Tuple[int, int]:
        raise NotImplementedError

    def bateria(self) -> Optional[int]:
        """Carga que le queda al aparato, 0-100. `None` = no se sabe.

        No es `NotImplementedError` a propósito: un mando que no sepa de
        baterías (el PC, el simulado) no debe reventar la partida por esto.
        Quien pregunta trata `None` como «no lo sé» y sigue.
        """
        return None

    def ver(self, rapido: bool = False) -> Optional[Captura]:
        raise NotImplementedError

    def tocar(self, p: Punto) -> Optional[bool]:
        """Toca, y dice si de verdad tocó (False: el candado lo impidió)."""
        raise NotImplementedError

    def secuencia(self, acciones: Sequence["Accion"]) -> None:
        """Varios movimientos seguidos. Por defecto, uno detrás de otro.

        Existe para que quien pueda hacerlo de golpe —el móvil, mandando un
        solo guion— lo haga, sin que el resto del código se entere.
        """
        for a in acciones:
            a.ejecutar(self)

    def vigilar(self, ataque: Optional["Punto"],
                vigilados: Sequence[Tuple["Punto", "Punto"]],
                segundos: int = 15) -> str:
        """Jugar solo unos segundos, mirando la pantalla sin parar.

        Por defecto no hay nada que ceder —un mando cualquiera no sabe mirarse
        a sí mismo—, así que se machaca el ataque y ya. El que sí sabe es el
        móvil: ver `MandoAndroid.vigilar` y `Vigia`.
        """
        if ataque is None:
            time.sleep(min(5.0, segundos))
            return "sin sitio donde atacar"
        golpes = max(1, min(60, segundos * 4))
        for _ in range(golpes):
            self.tocar(ataque)
        return f"toques={golpes} (a ciegas: este mando no sabe mirar)"

    def deslizar(self, desde: Punto, hasta: Punto, ms: int = 300) -> Optional[bool]:
        raise NotImplementedError

    def mantener(self, p: Punto, ms: int = 800) -> None:
        self.deslizar(p, p, ms)

    def andar(self, centro: Punto, dx: float, dy: float, ms: int = 800) -> None:
        """Mover el personaje con el joystick: dirección y CUÁNTO rato.

        Enzo, 10 sep 2026: «Celestia no sabe ni usar el joystick, y cuando lo
        usa lo pulsa como si fuese botón y no es así». Exacto: un joystick no
        se toca, se empuja y se sostiene — el tiempo que lo sostienes es la
        distancia que andas. Tocarlo da un paso y para.

        `dx`/`dy` van de -1 a 1 desde el centro del joystick: (0,-1) es hacia
        arriba en pantalla, (1,0) a la derecha.
        """
        raise NotImplementedError

    def camara(self, dx: float, dy: float, ms: int = 300) -> None:
        """Girar la vista arrastrando por la mitad derecha de la pantalla.

        En un juego de acción la cámara no es un lujo: sin girarla, la mitad
        del mundo —y de los cofres— se queda detrás.
        """
        raise NotImplementedError

    def escribir(self, texto: str) -> None:
        raise NotImplementedError

    def tecla(self, nombre: str) -> None:
        raise NotImplementedError


# Anclados a línea entera: `level:` y nada más, porque `dumpsys battery`
# trae otras líneas que contienen « level» y juntarlas fue el bug de S68.
# Ver `MandoAndroid.bateria`.
_RE_NIVEL_BATERIA = re.compile(r"^\s*level:\s*(\d+)\s*$", re.M)
_RE_ESCALA_BATERIA = re.compile(r"^\s*scale:\s*(\d+)\s*$", re.M)

ADB_BIN = shutil.which("adb") or "/usr/bin/adb"


def _hay_demonio_adb() -> bool:
    """¿Está ya en marcha el servidor de adb? Sin arrancarlo para averiguarlo.

    `adb devices` **arranca el servidor** si no lo encuentra, y eso deja un
    proceso encendido y unos megas de RAM ocupados cada vez que se construye un
    mando — para acabar descubriendo que no hay ningún móvil conectado. Aquí la
    RAM es lo que mata las sesiones, así que se pregunta por el puerto a pelo y
    sólo se llama a adb si hay alguien contestando.

    Si el servidor no está levantado tampoco hay dispositivo emparejado, así
    que no se pierde nada: el canal cae a rish, que es lo que habría salido.
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", 5037)) == 0
    except OSError:
        return False


def limpiar_rish_huerfanos(edad_minima_s: float = 120.0,
                           raiz: Optional[Path] = None) -> int:
    """Mata los `rish` que dejaron atrás las sesiones muertas. Devuelve cuántos.

    Sesión 66, y es lo que impedía jugar. Cada `rish` mantiene un
    `app_process` **dentro del móvil**; cuando la sesión que lo lanzó muere
    —y aquí mueren a menudo: las mata Android— el proceso se queda huérfano
    ahí colgado. Encontrados **nueve a la vez**, de hasta 40 minutos, y con
    ellos delante una sola orden por el canal costaba **16 s**; barridos,
    **0,3 s**. Cincuenta veces, sin tocar una línea del jugador.

    Y no daba ningún error: la partida simplemente no arrancaba nunca — tres
    minutos sin llegar a la primera jugada, que desde fuera parece «el móvil
    va lento» en vez de «hay basura ocupando el canal».

    Sólo se tocan los **huérfanos** (`PPid` 1): un `rish` con padre vivo es de
    alguien que lo está usando ahora mismo. Y sólo si llevan un rato, para no
    llevarse por delante uno recién lanzado que todavía no tiene padre.
    """
    muertos = 0
    ahora = time.time()
    # `raiz` sólo existe para que esto se pueda probar sin matar procesos de
    # verdad: en producción es siempre /proc.
    raiz = raiz or Path("/proc")
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
        arranque_sistema = 0.0
        for linea in (raiz / "stat").read_text().splitlines():
            if linea.startswith("btime "):
                arranque_sistema = float(linea.split()[1])
                break
    except Exception:
        return 0
    for d in raiz.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if (d / "comm").read_text().strip() != "rish":
                continue
            campos = (d / "stat").read_text().rsplit(") ", 1)[1].split()
            # 22º campo tras el nombre: arranque del proceso, en ticks.
            nacio = arranque_sistema + float(campos[19]) / hz
            ppid = int(campos[1])
        except Exception:
            continue
        if ppid != 1 or ahora - nacio < edad_minima_s:
            continue
        try:
            os.kill(int(d.name), 9)
            muertos += 1
        except Exception:
            pass
    if muertos:
        logger.info("barridos %d `rish` huérfanos de sesiones muertas", muertos)
    return muertos


def canal_android(preferir_adb: bool = True) -> Tuple[str, List[str]]:
    """Cómo se le habla a este móvil: («adb»|«rish», prefijo del comando).

    Los dos caminos llegan al mismo sitio —un shell con uid 2000— pero el
    precio no se parece en nada, y de ahí depende a qué se puede jugar:

    - **rish** arranca un `app_process` con el dex de Shizuku EN CADA ORDEN:
      1,6 s medidos en reposo, hasta 25 s con el móvil ocupado. Y su shell no
      persiste (contesta a la primera orden y enmudece), así que no hay forma
      de amortizar ese arranque. Un toque cuesta segundos.
    - **adb** habla con un daemon que ya está corriendo. La conexión se paga
      una vez y luego cada orden son decenas de milisegundos, y además su
      shell sí persiste, así que `_ShellPersistente` funciona de verdad.

    Por eso se prefiere adb siempre que haya un dispositivo en estado
    `device`. Ojo con `offline`: eso es adbd contestando SIN emparejar, y no
    sirve para nada — hay que descartarlo o las órdenes fallan en silencio.
    """
    if preferir_adb and not SIN_MOVIL and os.path.exists(ADB_BIN) and _hay_demonio_adb():
        try:
            r = subprocess.run([ADB_BIN, "devices"], capture_output=True, timeout=10)
            for linea in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
                partes = linea.split()
                # Sólo «device». «offline» y «unauthorized» son adbd sin
                # emparejar: aceptarlos deja el jugador dando órdenes al vacío.
                if len(partes) >= 2 and partes[1] == "device":
                    return ("adb", [ADB_BIN, "-s", partes[0], "shell"])
        except Exception as e:
            logger.debug("no pude preguntar por dispositivos adb: %s", e)
    return ("rish", [RISH_BIN, "-c"])


class _ShellPersistente:
    """Un shell abierto al que se le van pasando órdenes.

    Cada `rish -c "..."` arranca un `app_process` nuevo, y eso son **segundos**
    (2,4-4,5 s medidos en el móvil de Enzo) ANTES de que el comando empiece.
    Por eso se abre un shell una vez y se le habla por stdin.

    Se le pide que imprima una marca al terminar cada orden: sin eso no hay
    forma de saber dónde acaba una respuesta y empieza la siguiente.

    🔴 **Y en hardware real esto NO funciona con el rish de Shizuku** (sesión
    58, la primera vez que se probó con Shizuku encendido): la primera orden
    contesta y la segunda no llega nunca, ni cerrando stdin. `ShizukuShellLoader`
    no mantiene el stdin del proceso remoto entre órdenes. Se deja la clase
    porque con `adb` sobre TCP sí sirve y porque el `Mando` la usa si puede,
    pero **tiene que detectar que se ha quedado muda y rendirse**, que es lo
    que no hacía: `readline()` sobre un pipe vacío bloquea para siempre y el
    `timeout` de abajo no llegaba a comprobarse nunca. Resultado: pedirle a
    Celestia «juega por mí» colgaba la conversación entera, sin error y sin
    que el tope de segundos de la partida sirviera de nada.
    """

    MARCA = "__CELESTIA_FIN__"

    # Lo que tarda el canal en estar listo para la PRIMERA orden. Con rish son
    # 1,5-3 s de arrancar `app_process`, y hasta 25 s con el móvil ocupado; con
    # adb, milisegundos. El tope viejo para esto era el mismo 5 s de una orden
    # normal, así que en un móvil cargado el shell moría en el saludo aunque
    # estuviera perfectamente bien.
    TOPE_SALUDO = 30.0

    def __init__(self, lanzar: Sequence[str], saludar: bool = False):
        self.lanzar = list(lanzar)
        # El saludo cuesta dos órdenes, así que se pide sólo donde compensa:
        # un canal que se espera persistente (adb). Con rish ni se monta el
        # shell, y quien construye esta clase a pelo —los tests— conserva el
        # contrato de que `orden(timeout=N)` no tarda mucho más de N.
        self.saludar = saludar
        self.proc: Optional[subprocess.Popen] = None
        # Una vez que se queda muda no se vuelve a intentar: cada intento
        # cuesta el timeout entero y el resultado ya se sabe.
        self.mudo = False
        # Cerrojo de reentrada: `_saluda` llama a `orden`, que vuelve a llamar
        # a `abrir`. Sin esto, un proceso que muere justo durante el saludo
        # hace que se relance y se salude otra vez, y otra.
        self._saludando = False

    def abrir(self) -> bool:
        if self.mudo:
            return False
        if self.proc and self.proc.poll() is None:
            return True
        try:
            self.proc = subprocess.Popen(
                self.lanzar, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except Exception as e:
            logger.warning("No pude abrir el shell persistente: %s", e)
            self.proc = None
            return False
        if self._saludando or not self.saludar:
            return True
        return self._saluda()

    def _saluda(self) -> bool:
        """Dos órdenes tontas seguidas, para saber si el canal PERSISTE.

        Sesión 59. Que conteste una vez no dice nada: el rish de Shizuku
        contesta a la primera y enmudece a la segunda, así que la pregunta no
        es «¿va?» sino «¿sigue yendo?». Antes eso se descubría a mitad de
        partida y costaba el timeout entero de una orden de verdad —y encima
        una orden que sí importaba, como `wm size`, cuya respuesta perdida
        dejaba la resolución en el valor por defecto y los toques desviados.

        Aquí se paga una sola vez, con órdenes que no hacen nada, y a cambio
        el que llama sabe desde el principio a qué atenerse.
        """
        self._saludando = True
        try:
            return self._saludo_real()
        finally:
            self._saludando = False

    def _saludo_real(self) -> bool:
        if not self.orden("echo hola", self.TOPE_SALUDO):
            logger.info("el canal no contestó al saludo — voy con una orden por proceso")
            self.mudo = True
            self.cerrar()
            return False
        if not self.orden("echo hola", self.TOPE_SALUDO):
            logger.info("el canal contesta una vez pero no dos (no persiste) "
                        "— voy con una orden por proceso")
            self.mudo = True
            self.cerrar()
            return False
        return True

    def orden(self, cmd: str, timeout: float = 5.0) -> str:
        """Ejecuta y devuelve la salida (texto). Cadena vacía si falla."""
        if not self.abrir() or not self.proc or not self.proc.stdin:
            return ""
        try:
            self.proc.stdin.write(f"{cmd}\necho {self.MARCA}\n".encode())
            self.proc.stdin.flush()
        except Exception:
            self.cerrar()
            return ""
        salida, limite = [], time.time() + timeout
        while True:
            if not self.proc.stdout:
                break
            queda = limite - time.time()
            if queda <= 0:
                # Se acabó el tiempo sin ver la marca. ANTES esto no podía
                # pasar —`readline()` bloqueaba y nadie volvía a mirar el
                # reloj— y por eso una orden perdida colgaba la partida entera.
                logger.warning("El shell persistente no contesta a «%s» en %.1fs "
                               "— me paso a lanzar un rish por orden", cmd, timeout)
                self.mudo = True
                self.cerrar()
                return ""
            # `select` sobre el pipe: esperar CON reloj, que es lo que
            # `readline()` no sabe hacer.
            listos, _, _ = select.select([self.proc.stdout], [], [], min(queda, 0.5))
            if not listos:
                continue
            linea = self.proc.stdout.readline()
            if not linea:
                # 🔴 Sesión 59. El pipe se ha cerrado: el shell se murió a
                # media respuesta. ANTES esto hacía `break` y se devolvía lo
                # recibido hasta ahí **como si fuera la respuesta entera**, sin
                # marca de fin y sin avisar a nadie. Es el mismo daño de la
                # S58 con otra cara: si la orden trunchada era `wm size`, la
                # resolución salía a medias o vacía, se caía al valor por
                # defecto y TODOS los toques quedaban desviados, porque van en
                # fracciones de pantalla. Una respuesta a medias no es una
                # respuesta: se dice que no hubo.
                logger.warning("el shell se cerró a media orden «%s» — la doy por perdida", cmd)
                self.mudo = True
                self.cerrar()
                return ""
            txt = linea.decode("utf-8", "replace")
            if self.MARCA in txt:
                return "".join(salida)
            salida.append(txt)
        # Sin marca y sin cierre: mismo criterio, no vale a medias.
        return ""

    def cerrar(self) -> None:
        if self.proc:
            # Cerrar las tuberías además de matar: si no, los descriptores se
            # van acumulando en una partida larga (y los tests avisan de ello
            # con ResourceWarning, que es como se vio).
            for tuberia in (self.proc.stdin, self.proc.stdout):
                try:
                    if tuberia:
                        tuberia.close()
                except Exception:
                    pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None


class _PuenteFichero:
    """Un solo proceso dentro del móvil que ejecuta lo que se le va escribiendo.

    Sesión 59. El precio de rish **no es ejecutar, es arrancar**: 1,6 s de
    `app_process` con el dex de Shizuku, medidos, y se pagan enteros en cada
    orden. Y su shell no persiste, así que por stdin no hay forma de
    amortizarlo (contesta a la primera orden y enmudece: ver `_ShellPersistente`).

    Pero sí persiste un **proceso** lanzado con rish. Así que se arranca uno
    solo, con un bucle que mira un fichero en `/sdcard` y ejecuta lo que
    encuentre. `/sdcard` lo ven los dos lados —el móvil y este PRoot— y ya se
    usaba para traer las capturas, o sea que la coherencia está comprobada.

    El arranque se paga una vez por partida en vez de una vez por toque. Es la
    diferencia entre 1.600 ms y decenas de ms, y de ahí depende a qué se puede
    jugar cuando no hay adb.

    No es un canal de propósito general: hay **un solo cliente** (el jugador) y
    las órdenes van de una en una. Por eso basta con un par de ficheros y una
    marca con número de orden para saber que la respuesta es la que se espera y
    no la anterior.
    """

    BASE = f"{MOVIL_DIR}/puente"
    # Cada cuánto mira el agente si hay algo que hacer. Es el suelo de la
    # latencia, así que cuanto más bajo mejor... pero es un bucle de shell
    # dando vueltas en el móvil: por debajo de esto se nota en la batería y le
    # quita CPU al juego, que es justo lo que no interesa. Medido con el
    # protocolo entero corriendo en local (mediana de 12 órdenes):
    #     latido 0.02  + `sh`  ... 140 ms
    #     latido 0.02  + `.`   ... 101 ms
    #     latido 0.005 + `.`   ...  84 ms   ← este
    #     latido 0.001 + `.`   ...  81 ms   (ya no compensa la CPU que cuesta)
    LATIDO = "0.005"
    # Cuántas vueltas en balde aguanta el agente antes de apagarse solo.
    # 🔴 Esto no es una optimización, es no dejar basura encendida en el móvil
    # de Enzo: si esta sesión se muere sin llamar a `cerrar()` —y estas
    # sesiones se mueren, van tres— el bucle se quedaría dando vueltas DENTRO
    # del teléfono indefinidamente, gastando batería sin que nadie lo sepa.
    # Con el latido de arriba salen unos 5-10 minutos de gracia, de sobra para
    # cualquier partida y poco para molestar. Se rearranca solo en la siguiente
    # orden, y arrancar cuesta 1,6 s una vez.
    VUELTAS_OCIOSAS = 60000
    # Cuánto se espera el turno del agente antes de rendirse y caer al respaldo
    # (una orden, un proceso). Corto a propósito: una orden del puente son
    # milisegundos, así que si en 12 s no hay turno es que el otro está
    # atascado, y esperar más sólo alarga el atasco.
    TURNO_ESPERA_S = 12.0
    # Y cuánto puede durar un turno antes de darlo por abandonado. Tiene que
    # ser mayor que el tope de una orden (30 s) para no robarle el turno a
    # alguien que sigue esperando respuesta legítimamente.
    TURNO_CADUCA_S = 45.0

    def __init__(self, prefijo: Sequence[str], base: Optional[str] = None):
        self.prefijo = list(prefijo)
        # `base` existe para poder probar el protocolo entero sin móvil: con un
        # `sh` local y un directorio temporal se ejecuta el mismo bucle y se
        # mide la misma latencia. Lo único que no cubre es rish.
        self.base = base or self.BASE
        self.proc: Optional[subprocess.Popen] = None
        self.mudo = False
        self._n = 0

    # Los cuatro ficheros del protocolo, todos bajo el mismo prefijo.
    @property
    def _vivo(self) -> str:   return f"{self.base}.vivo"
    @property
    def _orden(self) -> str:  return f"{self.base}.orden"
    @property
    def _corriendo(self) -> str: return f"{self.base}.corriendo"
    @property
    def _salida(self) -> str: return f"{self.base}.salida"
    @property
    def _turno(self) -> str: return f"{self.base}.turno"

    def _guion(self) -> str:
        """El bucle que corre DENTRO del móvil.

        `mv` antes de ejecutar y `mv` al terminar, en vez de escribir en su
        sitio: así este lado nunca lee media orden ni media respuesta. En el
        mismo sistema de ficheros un `mv` es un rename, que es atómico.

        Se ejecuta con `.` (source) y no con `sh`, que ahorra un fork por orden
        y son 40 ms de los 140 originales.

        🔴 **La salida va por una tubería, no directa al fichero** (sesión 61,
        primera prueba del puente en hardware real). `cmd` —y con él `input`,
        `wm`, `pm`, `am`: media Android— le pasa su stdout al servicio del
        sistema **por Binder**, y un fichero de `/sdcard` es FUSE: el
        descriptor no se puede transferir y sale
        `Failure calling service input: Failed transaction (2147483646)`.
        Con `> fichero` directo, **el puente no podía tocar la pantalla**, que
        es justo para lo que existe. Medido en el móvil: falla `input keyevent`
        y `wm size`; funciona `... | cat > fichero` y funciona la salida a
        ext4 (`/data/local/tmp`), pero esa ruta no la ve el PRoot, así que la
        tubería es la única que sirve a los dos lados. Cuesta un fork por
        orden; el otro camino es no poder jugar.

        Efecto de regalo: al ir por tubería, `.` corre en una subshell, así que
        una orden con `exit` ya no se lleva el bucle por delante — el riesgo
        que este método asumía.
        """
        return (
            f"i=0; while [ -f {self._vivo} ]; do "
            f"if [ -s {self._orden} ]; then "
            f"mv {self._orden} {self._corriendo} 2>/dev/null && {{ "
            f". {self._corriendo} 2>&1 | cat > {self._salida}.tmp; "
            f"mv {self._salida}.tmp {self._salida}; "
            f"rm -f {self._corriendo}; }}; i=0; "
            f"else i=$((i+1)); "
            f"if [ $i -gt {self.VUELTAS_OCIOSAS} ]; then rm -f {self._vivo}; break; fi; "
            f"fi; sleep {self.LATIDO}; done"
        )

    def responde(self, timeout: float = 3.0) -> bool:
        """¿Hay YA un agente obedeciendo dentro del móvil, lo arrancara quien lo
        arrancara?

        El agente vive **dentro del teléfono**: sobrevive a la sesión que lo
        lanzó, y estas sesiones se mueren (van cuatro). Preguntárselo cuesta
        milisegundos; darlo por muerto cuesta arrancar otro rish —1,6 s—, y en
        el caso que destapó esto, **el arranque ni siquiera termina**: con el
        juego a pantalla completa Android bloquea la conexión Termux↔Shizuku
        para procesos NUEVOS («Request timeout… may be blocked by your system»)
        mientras el agente que ya corre sigue contestando tan campante.

        No toca el estado: si nadie contesta, se retira la orden que se dejó
        puesta —un agente que despertara luego la ejecutaría a destiempo— y se
        devuelve `False` sin dar el canal por perdido.
        """
        if not os.path.exists(self._vivo):
            return False
        # El sondeo escribe en los MISMOS ficheros que una orden, así que pide
        # turno igual: sin esto, preguntar «¿hay alguien?» le pisaba la orden a
        # quien estaba jugando y le dejaba esperando una respuesta que ya no iba
        # a llegar. Y si el turno es de otro, la respuesta ya la sabemos: si
        # alguien lo está usando, hay agente y contesta.
        if not self._pedir_turno():
            return os.path.exists(self._vivo)
        try:
            return self._responde_con_turno(timeout)
        finally:
            self._soltar_turno()

    def _responde_con_turno(self, timeout: float) -> bool:
        self._n += 1
        marca = f"__CPING{self._n}__"
        tmp = f"{self._orden}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                f.write(f"echo {marca}\n")
            os.replace(tmp, self._orden)
        except OSError:
            return False
        limite = time.time() + timeout
        while time.time() < limite:
            try:
                with open(self._salida, "r") as f:
                    texto = f.read()
            except OSError:
                time.sleep(0.005)
                continue
            if marca not in texto:
                time.sleep(0.005)
                continue
            try:
                os.unlink(self._salida)
            except OSError:
                pass
            return True
        try:
            os.unlink(self._orden)
        except OSError:
            pass
        return False

    def abrir(self) -> bool:
        if self.mudo:
            return False
        if self.proc and self.proc.poll() is None:
            return True
        # Antes de arrancar uno: ¿no habrá ya otro dentro? Ver `responde`.
        if self.responde():
            return True
        for f in (self._orden, self._corriendo, self._salida, f"{self._salida}.tmp"):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            with open(self._vivo, "w") as f:
                f.write("1")
        except OSError as e:
            logger.warning("no puedo escribir en /sdcard, sin puente: %s", e)
            self.mudo = True
            return False
        try:
            self.proc = subprocess.Popen(
                self.prefijo + [self._guion()],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("no pude lanzar el agente en el móvil: %s", e)
            self.proc = None
            self.mudo = True
            return False
        return True

    def _pedir_turno(self, espera: Optional[float] = None) -> bool:
        """Un agente, una orden a la vez. Devuelve si se consiguió el turno.

        El agente del móvil **se comparte entre procesos** —`abrir()` adopta el
        que ya esté corriendo (S67)— y con él se comparten los cuatro ficheros
        del protocolo. Dos procesos a la vez se pisan de dos maneras, las dos
        vistas en vivo el 9 de septiembre, con una partida jugando mientras yo
        miraba la pantalla desde fuera para ver cómo iba:

          · el fichero temporal de la orden se llamaba igual para todos, así que
            el `mv` de uno dejaba al otro sin fichero que mover: Errno 2;
          · y peor, el primero que encontraba su marca en `.salida` **borraba el
            fichero**, con lo que el otro esperaba una respuesta que ya no
            existía, agotaba los 30 s, daba el canal por muerto y **mataba al
            agente**. La partida se quedó sin ojos y se cortó sola con el juego
            perfectamente delante.

        El candado es un directorio porque `mkdir` es atómico y funciona igual
        en /sdcard; `flock` **no existe en este PRoot** (Errno 38, la misma
        trampa que `Path.exists()` en la S68). Si no hay turno no se bloquea a
        nadie: se contesta que no y quien llama cae al respaldo de siempre —una
        orden, un proceso—, que es lento pero nunca rompe una partida ajena.
        """
        # El plazo sale de la instancia, no del valor por defecto: así se
        # puede acortar en una prueba sin tocar la clase entera.
        espera = self.TURNO_ESPERA_S if espera is None else espera
        limite = time.time() + espera
        while True:
            try:
                os.mkdir(self._turno)
                return True
            except FileExistsError:
                pass
            except OSError as e:
                # El sistema de ficheros no deja ni intentarlo. Antes que
                # quedarse sin canal, se sigue sin candado: es exactamente el
                # comportamiento que había hasta hoy.
                logger.debug("no puedo usar el candado del puente: %s", e)
                return True
            # Un turno demasiado viejo es de alguien que se murió con él puesto.
            try:
                edad = time.time() - os.stat(self._turno).st_mtime
                if edad > self.TURNO_CADUCA_S:
                    logger.warning("retiro un turno del puente abandonado hace "
                                   "%.0f s", edad)
                    # Puede no ser un directorio: cualquier cosa con ese nombre
                    # bloquearía el canal para siempre, y eso ya pasó una vez
                    # (un fichero suelto de una prueba).
                    try:
                        os.rmdir(self._turno)
                    except NotADirectoryError:
                        os.unlink(self._turno)
                    continue
            except OSError:
                pass
            if time.time() >= limite:
                return False
            time.sleep(0.02)

    def _soltar_turno(self) -> None:
        try:
            os.rmdir(self._turno)
        except OSError:
            pass

    def orden(self, cmd: str, timeout: float = 30.0) -> Optional[str]:
        """Ejecuta y devuelve la salida. `None` —no ""— si no llegó a contestar.

        🔴 Sesión 61, y es el bug más caro que ha tenido esto. Devolviendo `""`
        para las dos cosas, quien llamaba no podía distinguir «la orden fue y no
        imprimió nada» de «el canal no contestó», y trataba lo primero como lo
        segundo: repetía la orden lanzando un rish suelto, 1,6 s. Y resulta que
        **las órdenes sin salida son justo los TOQUES** (`input tap`,
        `input keyevent`, `wm`…), o sea lo único que se repite en una partida.
        Medido en el móvil, con el puente montado y caliente:

            echo hola ......... 122 ms   (tiene salida)
            input tap 5 5 .... 1.720 ms  (no tiene → se repetía por el camino lento)

        O sea que el puente aceleraba todo **menos** aquello para lo que existe.
        La marca de fin ya estaba en el protocolo desde el principio: si llegó,
        la orden se ejecutó, aunque no dijera nada.

        El tope por defecto es generoso porque la PRIMERA orden espera además a
        que arranque el agente (los 1,6 s de rish). Las siguientes van solas.
        """
        if not self.abrir():
            return None
        if not self._pedir_turno():
            logger.warning("otro proceso lleva %.0f s usando el puente — sigo "
                           "con una orden por proceso", self.TURNO_ESPERA_S)
            return None
        try:
            return self._orden_con_turno(cmd, timeout)
        finally:
            self._soltar_turno()

    def _orden_con_turno(self, cmd: str, timeout: float) -> Optional[str]:
        """El cuerpo de `orden()`, ya con el turno en la mano."""
        self._n += 1
        marca = f"__CFIN{self._n}__"
        # El temporal lleva el pid: es lo que evita que el `mv` de otro proceso
        # nos deje sin fichero que mover (Errno 2). El candado ya lo impide,
        # pero cuando el candado no se puede usar esto sigue en pie.
        tmp = f"{self._orden}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                f.write(f"{cmd}\necho {marca}\n")
            os.replace(tmp, self._orden)
        except OSError as e:
            logger.warning("no pude dejar la orden en el móvil: %s", e)
            return None
        limite = time.time() + timeout
        while time.time() < limite:
            # Si el agente se ha muerto —Shizuku parado, por ejemplo— no tiene
            # sentido esperar el tope entero: nadie va a contestar. Sin esto,
            # con Shizuku caído CADA orden costaba 30 s antes de caer al
            # respaldo, que es peor que no tener puente.
            if self.proc and self.proc.poll() is not None:
                logger.warning("el agente del móvil se cayó nada más arrancar "
                               "— sigo con una orden por proceso")
                self.mudo = True
                self.cerrar()
                return None
            try:
                with open(self._salida, "r") as f:
                    texto = f.read()
            except OSError:
                time.sleep(0.005)
                continue
            if marca not in texto:
                # Es la respuesta de una orden anterior, o está a medio escribir.
                time.sleep(0.005)
                continue
            try:
                os.unlink(self._salida)
            except OSError:
                pass
            return texto.split(marca)[0]
        # Se acabó el tiempo con el agente vivo pero sin contestar. Se da por
        # perdido el canal entero: cada reintento costaría otro tope completo y
        # el respaldo (una orden por proceso) sí funciona siempre.
        logger.warning("el agente del móvil no contestó a «%s» en %.1fs "
                       "— sigo con una orden por proceso", cmd, timeout)
        self.mudo = True
        self.cerrar()
        return None

    def cerrar(self) -> None:
        # Quitar la señal de vida es lo que para el bucle de dentro. Matar el
        # proceso de fuera no basta: rish se va, pero el `sh` remoto sigue.
        try:
            os.unlink(self._vivo)
        except OSError:
            pass
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None


class MandoAndroid(Mando):
    """El móvil. Necesita Shizuku encendido, y si no lo está lo dice.

    Por qué Shizuku y no otra cosa: `screencap` pide el permiso
    READ_FRAME_BUFFER, que solo tienen `shell` y `root`. Termux es una app
    normal, así que sin Shizuku la captura no falla «a veces» — no existe. Y
    sin captura no hay juego, porque no se puede jugar a ciegas.
    """

    nombre = "móvil"

    def __init__(self, usar_shell_persistente: bool = True,
                 preferir_adb: bool = True):
        self._res: Optional[Tuple[int, int]] = None
        # Qué canal hay HOY. Se decide una vez: preguntar por dispositivos adb
        # en cada orden costaría más que la orden.
        self.canal, self._prefijo = canal_android(preferir_adb)
        # La pantalla como se ve ahora (con la rotación puesta). La rellena la
        # primera captura; hasta entonces vale la física. Ver `resolucion_vista`.
        self._res_vista: Optional[Tuple[int, int]] = None
        # El shell persistente sólo tiene sentido donde persiste. Con rish está
        # comprobado que no (contesta una vez), y montarlo cuesta el saludo
        # entero para nada; con adb sí, y es lo que baja el toque a decenas de
        # milisegundos.
        self._shell = (_ShellPersistente(self._prefijo + ["sh"], saludar=True)
                       if usar_shell_persistente and self.canal == "adb" else None)
        # Antes de montar nada por rish, barrer lo que dejaron las sesiones
        # muertas: con nueve `rish` huérfanos delante, una orden costaba 16 s
        # en vez de 0,3 y la partida no llegaba a empezar. Ver
        # `limpiar_rish_huerfanos`.
        if self.canal == "rish":
            limpiar_rish_huerfanos()
        # Con rish no hay shell por stdin que valga, pero sí un proceso que
        # dura: el puente por fichero. Es lo que evita pagar 1,6 s de arranque
        # en cada toque cuando no hay adb.
        self._puente = (_PuenteFichero(self._prefijo)
                        if usar_shell_persistente and self.canal == "rish" else None)
        self._touch: Optional[Dict[str, Any]] = None   # dispositivo táctil crudo
        self._touch_probado = False

    # ── disponibilidad ──────────────────────────────────────────────────
    def disponible(self) -> Tuple[bool, str]:
        if self.canal == "adb":
            # Con adb no hace falta Shizuku para nada: el daemon ya da uid 2000.
            return (True, "")
        # Si el canal que se usa para jugar ya está montado y OBEDECE, está
        # disponible: volver a preguntarle a Shizuku es pagar por confirmar lo
        # que se acaba de comprobar. Sesión 67, con ZZZ delante: arrancar un
        # rish nuevo se colgaba 30 s («may be blocked by your system») mientras
        # el agente de dentro del móvil contestaba en milisegundos, y el
        # jugador se plantaba con «no puedo jugar» teniendo el canal entero
        # funcionando. Lo que decide si se puede jugar es si el móvil obedece,
        # no si Shizuku sabe decir su uid.
        if self._puente and not self._puente.mudo and self._puente.responde():
            return (True, "")
        if not os.path.exists(RISH_BIN):
            return (False, "no encuentro `rish` — Shizuku no está instalado en este móvil")
        try:
            # rish escribe casi todo por stderr, no por stdout — trampa ya
            # documentada en bridge.js y que aquí volvió a morder: sin unir las
            # dos salidas, el motivo del fallo llega vacío y no se puede decir
            # al usuario qué le pasa.
            # 8 s no bastan: en el móvil de Enzo, arrancar `rish` solo (un
            # `app_process` con el dex de Shizuku) son 2,4-4,5 s medidos, y con
            # el teléfono ocupado se pasa de 8. Con el tope corto esto devolvía
            # «no pude preguntarle a Shizuku» con Shizuku perfectamente
            # encendido, y Celestia contestaba que no puede jugar cuando sí
            # puede. Sesión 58.
            r = subprocess.run([RISH_BIN, "-c", "id"], capture_output=True, timeout=30)
            out = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
        except Exception as e:
            return (False, f"no pude preguntarle a Shizuku: {e}")
        if "uid=2000" in out or "shell" in out:
            return (True, "")
        if "not running" in out.lower():
            return (False, "Shizuku está parado. Abre la app Shizuku y pulsa «Start» "
                           "(se para solo cada vez que reinicias el móvil)")
        return (False, f"Shizuku no responde como esperaba: {out.strip()[:120]}")

    def _sh(self, cmd: str, timeout: float = 5.0) -> str:
        """Una orden en el móvil. Por el shell abierto si contesta; si no, uno nuevo.

        Sesión 58: el respaldo no existía de verdad. Si el shell persistente se
        quedaba mudo —que es lo que hace el rish de Shizuku a partir de la
        segunda orden— esto devolvía cadena vacía y el que llamaba se lo creía:
        la resolución de pantalla salía como el valor por defecto y los toques
        se daban donde no era. Ahora se reintenta lanzando un `rish` propio,
        que es lento (2,5-4,5 s) pero funciona siempre.
        """
        if self._shell and not self._shell.mudo:
            out = self._shell.orden(cmd, timeout)
            if out or not self._shell.mudo:
                return out
            # Se acaba de quedar mudo con ESTA orden: no se pierde, se repite.
        if self._puente and not self._puente.mudo:
            # El tope se sube al de una orden por proceso: la PRIMERA vez el
            # puente espera además a que arranque el agente, y castigarlo por
            # eso sería tirar el canal justo antes de que empiece a servir.
            out = self._puente.orden(cmd, max(timeout, 30))
            # `is not None`, no truthiness: una orden que no imprime nada SÍ se
            # ha ejecutado, y son justo los toques. Ver `_PuenteFichero.orden`.
            if out is not None:
                return out
        try:
            # `self._prefijo` es `[rish, -c]` o `[adb, -s, X, shell]`: en los
            # dos casos el comando va como un solo argumento al final.
            r = subprocess.run(self._prefijo + [cmd], capture_output=True,
                               timeout=max(timeout, 30))
            return (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
        except Exception as e:
            logger.warning("no pude ejecutar «%s» en el móvil: %s", cmd, e)
            return ""

    def bateria(self) -> Optional[int]:
        r"""Carga del móvil, 0-100. `None` si no se pudo leer.

        S68: la versión anterior vivía suelta en un guion de fuera y hacía
        `dumpsys battery | grep " level"` juntando después todos los dígitos
        de la salida. Pero `dumpsys battery` trae DOS líneas con « level» —en
        el móvil de Enzo, `level: 47` y `Capacity level: 3`—, así que los
        números salían pegados: 47 y 3 daban «473». El log de aquella noche
        está lleno de «batería 623%» y
        «batería 543%». Comparado contra `< 10`, ese freno no salta jamás:
        habría jugado hasta dejar el móvil a cero.

        De ahí las dos decisiones de aquí: la regex va anclada a línea entera
        (`^\s*level:\s*(\d+)\s*$`), que es lo único que distingue `level:`
        de `led level:`; y el nivel se normaliza con `scale:`, porque no todo
        Android usa escala 100 y un 62 sobre 255 no es un 62%.
        """
        out = self._sh("dumpsys battery 2>/dev/null", timeout=15)
        m = _RE_NIVEL_BATERIA.search(out or "")
        if not m:
            return None
        nivel = int(m.group(1))
        e = _RE_ESCALA_BATERIA.search(out)
        escala = int(e.group(1)) if e else 100
        if escala <= 0:
            escala = 100
        return max(0, min(100, round(nivel * 100.0 / escala)))

    def resolucion(self) -> Tuple[int, int]:
        if self._res:
            return self._res
        out = self._sh("wm size 2>&1")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            # Segunda vía antes de rendirse. `wm size` es `cmd window`, y `cmd`
            # se cae en canales donde no puede pasar su stdout por Binder
            # (sesión 61: el puente por fichero, antes del arreglo del guion).
            # `dumpsys` escribe él mismo y sale hasta donde `cmd` no llega, así
            # que aquí sirve de red. Se busca `init=` —la física— para dar lo
            # mismo que `wm size`, que imprime «Physical size» la primera.
            # Suponer una medida es lo único peor que tardar en leerla.
            out2 = self._sh("dumpsys window displays 2>&1 | grep -m1 init=")
            m = re.search(r"(?:init|cur)=(\d+)x(\d+)", out2)
            if m:
                logger.info("`wm size` no contestó (%r); la resolución sale de "
                            "dumpsys: %sx%s", out.strip()[:60], m.group(1), m.group(2))
        if m:
            self._res = (int(m.group(1)), int(m.group(2)))
        else:
            # Suponer 1080x2400 es lo que había antes, y es peligroso: los
            # toques se calculan como fracción de la pantalla, así que con la
            # medida equivocada caen desviados. El móvil de Enzo es 1084x2412.
            # Se avisa en vez de fingir que se sabe.
            logger.warning("No pude leer el tamaño de la pantalla (`wm size` dijo %r); "
                           "voy con 1080x2400 y los toques pueden caer desviados",
                           out.strip()[:80])
            self._res = (1080, 2400)
        return self._res

    def app_en_pantalla(self) -> Optional[str]:
        """Qué aplicación está delante ahora mismo, o `None` si no se sabe.

        Hace falta porque el jugador **no mira dónde está**: si se le manda
        jugar a algo y en pantalla hay otra cosa, toca esa otra cosa igual —
        sesión 63, preguntándole «¿puedes jugar al ZZZ?» se puso a dar toques
        en el escritorio de Android. Los toques van a ciegas y no dan error.

        Dos vías, como en `resolucion()`: `mResumedActivity` es lo que de
        verdad está en primer plano, y `mCurrentFocus` sirve de red cuando
        `dumpsys activity` no contesta. `None` significa «no lo sé», que NO es
        lo mismo que «no está»: quien pregunte tiene que distinguirlo.
        """
        for orden, patron in (
            ("dumpsys activity activities 2>/dev/null | grep -m1 mResumedActivity",
             r"\su\d+\s+([A-Za-z][\w.]*\.[\w]+)/"),
            ("dumpsys window 2>/dev/null | grep -m1 mCurrentFocus",
             r"\su\d+\s+([A-Za-z][\w.]*\.[\w]+)/"),
        ):
            try:
                out = self._sh(orden, timeout=8)
            except Exception as e:
                logger.debug("no pude preguntar qué app hay delante: %s", e)
                continue
            m = re.search(patron, out or "")
            if m:
                return m.group(1)
        return None

    def apps_resumidas(self) -> List[str]:
        """TODAS las apps activas ahora, no sólo la primera. Vacía = no se sabe.

        Android puede tener varias a la vez —pantalla dividida, ventana
        flotante, un segundo monitor— y `app_en_pantalla()` contesta una sola
        porque hace `grep -m1`. El 9 de septiembre eso cortó una partida buena:
        Enzo tenía Termux delante leyéndome, ZZZ seguía abierto detrás, y el
        jugador leyó «delante hay com.termux», dio el juego por cerrado y lo
        relanzó. `dumpsys` decía las dos cosas:

            topResumedActivity=... com.HoYoverse.Nap/...ComboSDKActivity
            topResumedActivity=... com.termux/.app.TermuxActivity

        Saber que el juego SIGUE ABIERTO cambia qué hacer: no se relanza (eso
        lo devuelve al arranque y pierde la partida, sesión 67), se espera.
        """
        vistas: List[str] = []
        for orden in (
            "dumpsys activity activities 2>/dev/null | "
            "grep -E 'mResumedActivity|topResumedActivity'",
            "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'",
        ):
            try:
                out = self._sh(orden, timeout=10)
            except Exception as e:
                logger.debug("no pude listar las apps de delante: %s", e)
                continue
            for m in re.finditer(r"\su\d+\s+([A-Za-z][\w.]*\.[\w]+)/", out or ""):
                if m.group(1) not in vistas:
                    vistas.append(m.group(1))
            if vistas:
                break
        return vistas

    def resolucion_vista(self) -> Tuple[int, int]:
        """La pantalla **tal como se ve ahora**, con su orientación puesta.

        `resolucion()` da la física, que es la que contesta `wm size` y no
        cambia al girar el móvil. Pero `input tap` trabaja en el espacio de la
        orientación ACTUAL, que es exactamente el de la captura. Así que manda
        lo último que se ha visto; hasta la primera captura, la física.

        Sin esto, un juego en horizontal recibe **todos** los toques con los
        ejes cambiados, y no da error: toca donde no es y se calla.
        """
        return self._res_vista or self.resolucion()

    # ── ver ─────────────────────────────────────────────────────────────
    # Para juegos que sólo van apaisados (ZZZ). 11 sep 2026: `app_en_pantalla`
    # decía ZZZ con WhatsApp a pantalla completa y ZZZ en una ventana flotante;
    # se capturó y se leyó el chat de Enzo. Una captura en vertical no es el
    # juego: con esto se descarta aquí, sin devolverla, y nadie la lee ni la
    # manda a ningún modelo.
    solo_apaisado = False
    # Con esto, sólo se toca con esta app delante (p. ej. "com.HoYoverse.Nap").
    app_permitida = ""

    def _puede_tocar(self) -> bool:
        """El candado de cada toque. 11 sep 2026: Enzo cambió a WhatsApp a mitad
        de un montaje y los deslizamientos del montador no miraban qué había
        delante. Se niega si lo último que se vio va en vertical (con un juego
        apaisado es otra app) o si delante hay otra app. «No lo sé» (None) no
        cuenta como otra app: con el juego cargado `dumpsys` a veces no contesta."""
        if self.solo_apaisado:
            a, al = self.resolucion_vista()
            if a < al:
                logger.warning("toque denegado: lo último que se vio va en vertical (otra app)")
                return False
        if self.app_permitida:
            try:
                delante = self.app_en_pantalla()
            except Exception:
                delante = None
            if delante is not None and delante != self.app_permitida:
                logger.warning("toque denegado: delante está %s, no %s", delante, self.app_permitida)
                return False
            flotante = self.flotante_ajena()
            if flotante:
                logger.warning("toque denegado: %s flota encima de %s", flotante, self.app_permitida)
                return False
        return True

    # Cuánto vale la última respuesta sobre ventanas flotantes: preguntar en cada
    # toque sería un `dumpsys` más por pulsación.
    FLOTANTE_CACHE_S = 3.0

    def flotante_ajena(self) -> str:
        """La app de una ventana flotante (imagen en imagen) que no es la permitida, o "".

        11 sep 2026, 20:35: con ZZZ delante, Enzo tenía Discord flotando encima
        (`mode=pinned`) justo sobre el botón SELECT del montaje. `app_en_pantalla`
        decía ZZZ —la ventana flotante no se lleva el foco— y la captura recogió lo
        que enseñaba Discord. Tocar ahí es tocarle otra app, o colgarle una llamada.
        Si `dumpsys` no contesta, "": no saberlo no bloquea, como en el resto del candado.
        """
        ahora = time.time()
        cache = getattr(self, "_flotante_cache", None)
        if cache and ahora - cache[0] < self.FLOTANTE_CACHE_S:
            return cache[1]
        try:
            out = self._sh("dumpsys activity activities 2>/dev/null | grep -m1 'mode=pinned'", timeout=8)
        except Exception:
            out = ""
        m = re.search(r"\bA=\d+:([A-Za-z][\w.]*)", out or "")
        app = m.group(1) if m and m.group(1) != self.app_permitida else ""
        self._flotante_cache = (ahora, app)
        return app

    def ver(self, rapido: bool = False) -> Optional[Captura]:
        if self.app_permitida and self.flotante_ajena():
            # Ni se captura: lo que enseña la ventana flotante es de Enzo.
            logger.warning("otra app flota encima del juego: no capturo")
            return None
        cap = self._ver_sin_filtro(rapido)
        if cap is not None and self.solo_apaisado and cap.ancho < cap.alto:
            logger.warning("captura en vertical con el juego apaisado: es otra app, la descarto sin leerla")
            return None
        return cap

    def _ver_sin_filtro(self, rapido: bool = False) -> Optional[Captura]:
        """`rapido=True` trae sólo píxeles crudos; si no, además la imagen.

        🔴 Sesión 66, y son dos cosas de golpe. Pedirle al móvil que comprima
        (`screencap -p`) cuesta **1.663 ms medidos**; traer los píxeles crudos,
        **315**, y comprimirlos aquí, 87 (13 de convertir + 74 de JPEG). O sea
        que la vuelta entera pasa de ~1.800 ms a ~400 haciendo el mismo trabajo
        en el lado que tiene CPU de sobra en vez de en el que está jugando.

        Y lo segundo, que es peor: `_ver_png` **no traía los píxeles**, y todo
        lo que reconoce una pantalla vive de ellos. El bucle hace
        `huella = ... if cap.crudo else None`, así que en una partida de verdad
        —donde se mira con imagen— la huella era SIEMPRE `None`: ni libro de
        jugadas (la optimización grande: 2 ms en vez de un segundo largo), ni
        «esta pantalla no cambia», ni saber que estás ante una carga. Estaba
        escrito que el libro respondía en microsegundos y no llegaba a entrar
        nunca. Trayendo el crudo, entra.
        """
        if rapido:
            return self._ver_crudo()
        cap = self._ver_crudo()
        if cap is not None:
            png = _comprimir(cap)
            if png:
                cap.png = png
                return cap
        # Sin PIL, o si el crudo no vino: el camino de siempre. Más lento, pero
        # nunca «no veo nada».
        return self._ver_png()

    # Dónde deja la captura el lado de Android. Tiene que ser un sitio al que
    # llegue `shell` (uid 2000) y que se vea desde el PRoot: /sdcard cumple las
    # dos. Un nombre fijo a propósito: no interesa acumular capturas.
    _CAPTURA_REMOTA = f"{MOVIL_DIR}/pantalla"
    # Tope generoso porque el cuello no es `screencap` sino arrancar `rish`:
    # 2,4-4,5 s en reposo y hasta 25 s con el móvil ocupado (medido).
    _TOPE_CAPTURA = 60

    def _traer(self, sufijo: str, orden: str) -> Optional[bytes]:
        """Ejecuta `orden` dejando la captura en un fichero y lo trae.

        🔴 Sesión 58, primera prueba con Shizuku encendido: el código pedía
        `screencap -p` **y leía la imagen de stdout**. Por ahí no llega entera:
        se midió una pantalla de 1.103.974 bytes que por stdout volvía con
        188.766 — cabecera PNG válida y el resto cortado. Así que la imagen o
        no se abría o se abría a medias, y como encima cada intento tardaba más
        que el tope de 25 s, `ver()` devolvía `None` **siempre**. El jugador
        nunca ha visto una pantalla en este móvil. Por fichero sí llega entera.
        """
        if self.canal == "adb":
            # `exec-out` es el modo binario de adb: la imagen llega entera por
            # stdout, sin el rodeo por /sdcard y sin que nadie le toque los
            # saltos de línea. Es justo lo que rish no sabe hacer, y por eso
            # allí hay que pasar por fichero.
            try:
                r = subprocess.run(self._prefijo[:-1] + ["exec-out", orden],
                                   capture_output=True, timeout=self._TOPE_CAPTURA)
                return r.stdout or None
            except Exception as e:
                logger.warning("la captura por adb falló: %s", e)
                return None
        remoto = f"{self._CAPTURA_REMOTA}{sufijo}"
        # Por el puente si lo hay: capturar cuesta lo que cuesta, pero
        # ARRANCAR rish para pedirlo cuesta 1,6 s más, y eso se paga en cada
        # vuelta del carril rápido.
        if self._puente and not self._puente.mudo:
            if (self._puente.orden(f"{orden} {remoto} && echo ok",
                                   self._TOPE_CAPTURA) or "").strip().endswith("ok"):
                try:
                    with open(remoto, "rb") as f:
                        return f.read()
                except OSError as e:
                    logger.warning("la captura no llegó a %s: %s", remoto, e)
                    return None
        try:
            subprocess.run(self._prefijo + [f"{orden} {remoto}"],
                           capture_output=True, timeout=self._TOPE_CAPTURA)
        except Exception as e:
            logger.warning("la captura falló: %s", e)
            return None
        try:
            with open(remoto, "rb") as f:
                return f.read()
        except OSError as e:
            logger.warning("la captura no llegó a %s: %s", remoto, e)
            return None

    def _ver_png(self) -> Optional[Captura]:
        datos = self._traer(".png", "screencap -p")
        if not datos or len(datos) < 5000 or not datos.startswith(b"\x89PNG"):
            logger.warning("la captura PNG vino vacía o cortada (%d bytes)",
                           len(datos or b""))
            return None
        # 🔴 Sesión 61, probando con ZZZ de verdad. Aquí se ponía lo que decía
        # `wm size` —la resolución FÍSICA, siempre en vertical— y la captura de
        # un juego en horizontal viene 2412x1084. O sea que la `Captura` mentía
        # sobre su propia imagen, y todo lo que convierte fracciones a píxeles
        # (o sea, TOCAR) lo hacía con los ejes cambiados. Cualquier juego en
        # horizontal recibía todos los toques en el sitio equivocado, sin dar
        # ningún error.
        a, al = self._medir_png(datos)
        if a:
            self._res_vista = (a, al)
        else:
            a, al = self.resolucion()
        return Captura(ancho=a, alto=al, png=datos)

    @staticmethod
    def _medir_png(datos: bytes) -> Tuple[int, int]:
        """Ancho y alto leídos de la cabecera IHDR del propio PNG.

        De los bytes y no con PIL: son 8 bytes en posición fija y esto va en el
        camino de cada vuelta.
        """
        try:
            if datos[12:16] == b"IHDR":
                return (int.from_bytes(datos[16:20], "big"),
                        int.from_bytes(datos[20:24], "big"))
        except Exception:
            pass
        return (0, 0)

    def _ver_crudo(self) -> Optional[Captura]:
        """screencap sin `-p`: cabecera + RGBA a pelo.

        La cabecera trae ancho, alto y formato; desde Android 13 lleva además
        un campo de espacio de color. En vez de adivinar la versión, se prueba
        qué desplazamiento cuadra con el tamaño real de los datos.
        """
        datos = self._traer(".raw", "screencap")
        if not datos or len(datos) < 32:
            return None
        ancho, alto, _fmt = struct.unpack("<III", datos[:12])
        if not (0 < ancho <= 8000 and 0 < alto <= 8000):
            return None
        esperado = ancho * alto * 4
        for cabecera in (12, 16):
            if len(datos) - cabecera >= esperado:
                # La cabecera del framebuffer ya trae la orientación puesta, y
                # es la que vale para tocar (ver `resolucion_vista`).
                self._res_vista = (ancho, alto)
                return Captura(ancho=ancho, alto=alto,
                               crudo=datos[cabecera:cabecera + esperado])
        return None

    # ── tocar ───────────────────────────────────────────────────────────
    def _buscar_pantalla_tactil(self) -> Optional[Dict[str, Any]]:
        """Localiza el /dev/input del dedo, para escribir eventos directos.

        `input tap` arranca una JVM por toque: ~200 ms perdidos antes de que el
        dedo se mueva. Escribir el evento crudo cuesta microsegundos. La pega
        es que hay que averiguar QUÉ dispositivo es el táctil y en qué rango
        habla, porque cada móvil trae el suyo.
        """
        if self._touch_probado:
            return self._touch
        self._touch_probado = True
        out = self._sh("getevent -pl 2>&1", timeout=10)
        if not out:
            return None
        dispositivo, datos = None, {}
        for bloque in re.split(r"add device \d+: ", out):
            if "ABS_MT_POSITION_X" not in bloque:
                continue
            dispositivo = bloque.split("\n", 1)[0].strip()
            mx = re.search(r"ABS_MT_POSITION_X.*?max\s+(\d+)", bloque, re.S)
            my = re.search(r"ABS_MT_POSITION_Y.*?max\s+(\d+)", bloque, re.S)
            ms = re.search(r"ABS_MT_TRACKING_ID.*?max\s+(\d+)", bloque, re.S)
            if mx and my:
                datos = {"dev": dispositivo, "max_x": int(mx.group(1)),
                         "max_y": int(my.group(1)),
                         "max_track": int(ms.group(1)) if ms else 65535}
                break
        if datos:
            logger.info("Pantalla táctil cruda: %s (%sx%s)", datos["dev"],
                        datos["max_x"], datos["max_y"])
            self._touch = datos
        return self._touch

    def tocar(self, p: Punto) -> bool:
        # Devuelve si tocó: quien busca en una lista tiene que poder distinguir
        # «no está» de «no me dejaron mirar» (22 sep: con Discord delante, la
        # búsqueda no leyó un solo nombre y apuntó a Miyabi como «no la tengo»).
        if not self._puede_tocar():
            return False
        a, al = self.resolucion_vista()
        x, y = p.pixeles(a, al)
        # 🔴 Sesión 66, medido con el puente montado y ZZZ delante:
        #
        #     input tap ....... 126 ms   (un proceso; la JVM ya está caliente)
        #     sendevent ....... 366 ms   (SEIS procesos, uno por evento)
        #
        # Justo al revés de lo que decía este método desde la S56 — «sendevent
        # es más rápido que cmd input»—, y el motivo de aquel error es
        # instructivo: entonces cada orden pagaba **arrancar rish** (1,6 s), y
        # los seis `sendevent` iban juntos en UNA orden mientras `input tap`
        # iba en otra. Se comparaban seis procesos contra un arranque, no
        # contra un proceso. Con el puente ese arranque ya no existe y lo que
        # queda es contar procesos: seis pierden contra uno.
        #
        # `input tap` tiene además la ventaja que importa en un juego: trabaja
        # en el espacio de **lo que se ve**, así que funciona igual con la
        # pantalla girada. `sendevent` escribe en el panel táctil, que no gira,
        # y por eso sólo puede usarse de respaldo y sin la pantalla girada.
        # ZZZ, 11 sep 2026: sus menús (la ciudad, la Agenda) no cogen un toque
        # instantáneo —tres seguidos en el icono de la Agenda sin efecto— y con
        # el dedo sostenido 150 ms sí. Quien lo necesite sube `pulsacion_ms`.
        orden = (f"input swipe {x} {y} {x} {y} {int(self.pulsacion_ms)}"
                 if self.pulsacion_ms > 0 else f"input tap {x} {y}")
        if self._sh(orden, timeout=5) is not None:
            return True
        girada = (a, al) != self.resolucion()
        t = None if girada else self._buscar_pantalla_tactil()
        if t:
            # Protocolo multitouch B: dedo baja, posición, sync; dedo sube, sync.
            ex = int(x * t["max_x"] / max(1, a))
            ey = int(y * t["max_y"] / max(1, al))
            d = t["dev"]
            ordenes = (
                f"sendevent {d} 3 57 1;"       # ABS_MT_TRACKING_ID = 1
                f"sendevent {d} 3 53 {ex};"    # ABS_MT_POSITION_X
                f"sendevent {d} 3 54 {ey};"    # ABS_MT_POSITION_Y
                f"sendevent {d} 0 0 0;"        # SYN_REPORT
                f"sendevent {d} 3 57 -1;"      # dedo levantado
                f"sendevent {d} 0 0 0"
            )
            self._sh(ordenes, timeout=3)
        return True

    def deslizar(self, desde: Punto, hasta: Punto, ms: int = 300) -> bool:
        if not self._puede_tocar():
            return False
        a, al = self.resolucion_vista()
        x1, y1 = desde.pixeles(a, al)
        x2, y2 = hasta.pixeles(a, al)
        self._sh(f"input swipe {x1} {y1} {x2} {y2} {ms}", timeout=max(5.0, ms / 1000 + 3))
        return True

    # Dónde está el joystick cuando nadie ha dicho lo contrario: abajo a la
    # izquierda, que es donde lo pone casi todo juego de móvil. Se puede
    # cambiar por juego cuando se aprenda a mirarlo.
    JOYSTICK = (0.18, 0.75)

    # Cuánto se sostiene el dedo en `tocar`. 0 es un `input tap` de los de
    # siempre; hay juegos que sólo cogen el toque si dura algo más.
    pulsacion_ms = 0

    # Cuánto se aparta el dedo del centro del joystick. Poco no mueve; del
    # todo, algunos juegos lo leen como «corre» — se deja a media asta.
    EMPUJE = 0.11

    def andar(self, centro: Optional[Punto], dx: float, dy: float,
              ms: int = 800) -> None:
        """Empujar el joystick y SOSTENERLO, que es como se anda.

        Se hace con `input motionevent`, que es lo único que permite mantener
        el dedo puesto: DOWN donde está el joystick, MOVE hacia la dirección,
        esperar el rato que haga falta, y UP al soltar. `input swipe` no vale
        aquí — arrastra y suelta, así que el personaje da un paso y se para.

        Las tres órdenes van en UN guion: entre proceso y proceso del móvil
        pasan ~100 ms, y si el DOWN y el MOVE llegan separados el juego ve dos
        toques sueltos en vez de un empujón sostenido.
        """
        if not self._puede_tocar():
            return
        an, al = self.resolucion_vista()
        c = centro or Punto(*self.JOYSTICK)
        x0, y0 = c.pixeles(an, al)
        largo = max(-1.0, min(1.0, dx)), max(-1.0, min(1.0, dy))
        x1 = int(x0 + largo[0] * self.EMPUJE * an)
        y1 = int(y0 + largo[1] * self.EMPUJE * al)
        x1 = max(1, min(an - 2, x1))
        y1 = max(1, min(al - 2, y1))
        espera = max(0.05, ms / 1000.0)
        self._sh(f"input motionevent DOWN {x0} {y0}; "
                 f"input motionevent MOVE {x1} {y1}; "
                 f"sleep {espera:.2f}; "
                 f"input motionevent UP {x1} {y1}",
                 timeout=max(8.0, espera + 6))

    def camara(self, dx: float, dy: float, ms: int = 300) -> None:
        """Girar la vista: un arrastre por la mitad derecha, lejos del joystick."""
        an, al = self.resolucion_vista()
        x0, y0 = int(an * 0.70), int(al * 0.45)
        x1 = max(1, min(an - 2, int(x0 + dx * an * 0.25)))
        y1 = max(1, min(al - 2, int(y0 + dy * al * 0.25)))
        self._sh(f"input swipe {x0} {y0} {x1} {y1} {int(ms)}",
                 timeout=max(5.0, ms / 1000 + 3))

    # Cuántas órdenes caben en un guion. Cuenta las esperas, que van entre
    # golpe y golpe: con el tope en 10, una ráfaga de ocho toques con su ritmo
    # se quedaba en cinco (medido). Lo que acota de verdad el riesgo es el
    # TIEMPO a ciegas, y eso se controla en `Jugador._pasos_de`.
    MAX_RAFAGA = 20

    def _orden_de(self, a: "Accion") -> Optional[str]:
        """La acción, escrita como orden de shell del móvil. `None` si no cabe.

        No todo puede ir en ráfaga: `sendevent` necesita saber si la pantalla
        está girada y `escribir` lleva texto que hay que limpiar aparte. Lo que
        no cabe se queda fuera y se ejecuta por su camino de siempre.
        """
        an, al = self.resolucion_vista()
        if a.tipo == "tocar" and a.punto:
            x, y = a.punto.pixeles(an, al)
            return f"input tap {x} {y}"
        if a.tipo == "mantener" and a.punto:
            x, y = a.punto.pixeles(an, al)
            # Un «mantener» es un swipe al mismo sitio: así dura lo que se pide.
            return f"input swipe {x} {y} {x} {y} {max(1, a.ms)}"
        if a.tipo == "deslizar" and a.punto and a.hasta:
            x1, y1 = a.punto.pixeles(an, al)
            x2, y2 = a.hasta.pixeles(an, al)
            return f"input swipe {x1} {y1} {x2} {y2} {max(1, a.ms)}"
        if a.tipo == "esperar":
            return f"sleep {min(5.0, max(0.0, a.ms / 1000)):.2f}"
        return None

    def vigilar(self, ataque: Optional["Punto"],
                vigilados: Sequence[Tuple["Punto", "Punto"]],
                segundos: int = 15) -> str:
        """Le deja el mando al móvil: mira 15 veces por segundo y reacciona.

        Esto es lo más cerca que se puede estar de «ver vídeo y reaccionar
        sobre la marcha» (Enzo, S66) sin mentir: dentro del teléfono, mirar
        cuesta 64 ms y tocar 48, así que responde a lo que aparece en ~112 ms.
        Por una red, la vuelta más corta medida es medio segundo.
        """
        return Vigia(self).correr(ataque, vigilados, segundos=segundos)

    def secuencia(self, acciones: Sequence["Accion"]) -> None:
        """La ráfaga entera en UN viaje por el canal.

        Aquí está la diferencia entre tocar y jugar. Medido en este móvil
        (sesión 61): una orden por el puente son 116-131 ms de ida y vuelta,
        pero `input tap` **dentro** del móvil son 48. Mandando diez toques en
        un solo guion se paga un viaje en vez de diez, y los golpes salen
        seguidos, al ritmo que los pide el juego y no al que aguanta el canal.

        Lo que no se puede escribir como orden (`escribir`, `tecla` con
        limpieza propia) se ejecuta por su camino, en su sitio de la fila: el
        orden de los movimientos no se toca nunca.
        """
        lote: List[str] = []

        def soltar() -> None:
            if not lote:
                return
            guion = "; ".join(lote)
            # El tope cubre lo que el propio guion va a dormir, más el viaje.
            espera = sum(float(o.split()[-1]) for o in lote if o.startswith("sleep"))
            self._sh(guion, timeout=max(8.0, espera + len(lote) * 0.6 + 5))
            lote.clear()

        for a in list(acciones)[:self.MAX_RAFAGA]:
            orden = self._orden_de(a)
            if orden is None:
                soltar()
                a.ejecutar(self)
            else:
                lote.append(orden)
        soltar()

    def escribir(self, texto: str) -> None:
        seguro = re.sub(r"[^\w\s@.,:;/¿?¡!()\-+]", "", texto)[:200].replace(" ", "%s")
        self._sh(f"input text '{seguro}'", timeout=8)

    def tecla(self, nombre: str) -> None:
        teclas = {"atras": "KEYCODE_BACK", "inicio": "KEYCODE_HOME",
                  "menu": "KEYCODE_APP_SWITCH", "intro": "KEYCODE_ENTER",
                  "volumen+": "KEYCODE_VOLUME_UP", "volumen-": "KEYCODE_VOLUME_DOWN"}
        code = teclas.get(_sin_acentos(nombre.lower()), nombre.upper())
        self._sh(f"input keyevent {code}", timeout=5)

    def cerrar(self) -> None:
        # Barrer la última captura. Nombre fijo significa que no se acumulan,
        # no que no estorben: el framebuffer crudo son **10 MB** (medidos) que
        # se quedarían en el móvil de Enzo hasta la próxima partida. Se borran
        # desde este lado, que /sdcard lo ven los dos y así no cuesta ni una
        # orden ni depender de que el canal siga vivo.
        for sufijo in (".png", ".raw"):
            try:
                os.unlink(f"{self._CAPTURA_REMOTA}{sufijo}")
            except OSError:
                pass
        if self._shell:
            self._shell.cerrar()
        if self._puente:
            self._puente.cerrar()


class MandoPC(Mando):
    """El PC, hablando con el agente que ya vive en `agente_pc/`.

    Aquí hay bastante más margen que en el móvil: la captura no compite con el
    juego por la misma CPU pequeña, y no hace falta Shizuku ni permisos raros.
    Para juegos de acción, el PC es el sitio.
    """

    nombre = "PC"

    def __init__(self, host: str = "127.0.0.1", puerto: int = 8799, token: str = ""):
        self.base = f"http://{host}:{puerto}"
        self.token = token
        self._res: Optional[Tuple[int, int]] = None

    def _pedir(self, ruta: str, datos: Optional[dict] = None,
               timeout: float = 10.0) -> Optional[dict]:
        import urllib.error
        import urllib.request
        cab = {"Content-Type": "application/json"}
        if self.token:
            cab["Authorization"] = f"Bearer {self.token}"
        cuerpo = json.dumps(datos).encode() if datos is not None else None
        try:
            req = urllib.request.Request(self.base + ruta, data=cuerpo, headers=cab)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            logger.debug("agente PC %s falló: %s", ruta, e)
            return None

    def disponible(self) -> Tuple[bool, str]:
        r = self._pedir("/salud", timeout=4)
        if r:
            return (True, "")
        return (False, f"no contesta el agente de PC en {self.base} — "
                       "¿está instalado y arrancado? (ver agente_pc/README.md)")

    def resolucion(self) -> Tuple[int, int]:
        if self._res:
            return self._res
        r = self._pedir("/info", timeout=6) or {}
        p = r.get("pantalla") or {}
        self._res = (int(p.get("ancho") or 1920), int(p.get("alto") or 1080))
        return self._res

    def ver(self, rapido: bool = False) -> Optional[Captura]:
        r = self._pedir("/screenshot", timeout=20) or {}
        b64 = r.get("imagen_b64") or r.get("screenshot") or ""
        if not b64:
            return None
        try:
            png = base64.b64decode(b64)
        except Exception:
            return None
        a, al = self.resolucion()
        return Captura(ancho=a, alto=al, png=png)

    def tocar(self, p: Punto) -> None:
        a, al = self.resolucion()
        x, y = p.pixeles(a, al)
        self._pedir("/click", {"x": x, "y": y})

    def deslizar(self, desde: Punto, hasta: Punto, ms: int = 300) -> None:
        a, al = self.resolucion()
        x1, y1 = desde.pixeles(a, al)
        x2, y2 = hasta.pixeles(a, al)
        self._pedir("/move", {"x": x1, "y": y1})
        self._pedir("/click", {"x": x1, "y": y1, "boton": "left", "accion": "down"})
        self._pedir("/move", {"x": x2, "y": y2, "duracion": ms / 1000})
        self._pedir("/click", {"x": x2, "y": y2, "boton": "left", "accion": "up"})

    def escribir(self, texto: str) -> None:
        self._pedir("/type", {"texto": texto[:500]})

    def tecla(self, nombre: str) -> None:
        self._pedir("/hotkey", {"teclas": [nombre]})


class MandoSimulado(Mando):
    """Un aparato de mentira, para probar el resto sin depender del hardware.

    No es un adorno de tests: sin esto, cualquier cambio en el jugador exige un
    móvil con Shizuku encendido y un juego abierto, y entonces no se prueba
    nunca. Aquí las capturas se le dan hechas y las acciones se apuntan en una
    lista, así que el bucle entero se puede verificar en segundos.
    """

    nombre = "simulado"

    def __init__(self, pantallas: Sequence[Captura], ancho: int = 1080, alto: int = 2400):
        self.pantallas = list(pantallas)
        self.i = 0
        self._res = (ancho, alto)
        self.hechas: List[Tuple[str, Any]] = []

    def disponible(self) -> Tuple[bool, str]:
        return (True, "")

    def resolucion(self) -> Tuple[int, int]:
        return self._res

    def ver(self, rapido: bool = False) -> Optional[Captura]:
        if not self.pantallas:
            return None
        c = self.pantallas[min(self.i, len(self.pantallas) - 1)]
        return c

    def tocar(self, p: Punto) -> None:
        self.hechas.append(("tocar", p))
        self.i += 1

    def deslizar(self, desde: Punto, hasta: Punto, ms: int = 300) -> None:
        self.hechas.append(("deslizar", (desde, hasta, ms)))
        self.i += 1

    def escribir(self, texto: str) -> None:
        self.hechas.append(("escribir", texto))

    def tecla(self, nombre: str) -> None:
        self.hechas.append(("tecla", nombre))
        self.i += 1


# ─────────────────────────────────────────────────────────────────────────
#  Retina: mirar sin entender (el carril rápido)
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Palabra:
    """Un texto leído y —lo importante— DÓNDE está."""
    texto: str
    centro: Punto
    zona: Zona
    confianza: float


class Retina:
    """Mira unos pocos píxeles y no entiende nada. Por eso es rápida.

    Toda la percepción de combate se hace aquí. Una barra de vida que baja, un
    destello rojo que avisa de un golpe, un botón que se ilumina: son cambios
    de color en un sitio conocido. Comprobarlo es leer unos cuantos bytes de un
    array, no invocar a nadie.

    La unidad es la *firma*: el color medio de unas zonas. Comparar dos firmas
    dice si la pantalla cambió, y dónde, sin decodificar imagen ninguna.
    """

    @staticmethod
    def color_medio(cap: Captura, z: Zona, muestras: int = 24) -> Tuple[int, int, int]:
        """Color medio de una zona, muestreando en rejilla (no píxel a píxel).

        Con 24 muestras basta para saber si algo se puso rojo, y cuesta lo
        mismo mirar una zona grande que una pequeña — que es justo lo que hace
        falta cuando esto corre cientos de veces por segundo.
        """
        if not cap.crudo:
            return (0, 0, 0)
        x, y, an, al = z.pixeles(cap.ancho, cap.alto)
        lado = max(1, int(muestras ** 0.5))
        r = g = b = n = 0
        for i in range(lado):
            for j in range(lado):
                px = min(cap.ancho - 1, x + an * i // lado)
                py = min(cap.alto - 1, y + al * j // lado)
                c = cap.pixel(px, py)
                if c:
                    r += c[0]; g += c[1]; b += c[2]; n += 1
        return (r // n, g // n, b // n) if n else (0, 0, 0)

    @staticmethod
    def firma(cap: Captura, zonas: Sequence[Zona]) -> Tuple[Tuple[int, int, int], ...]:
        return tuple(Retina.color_medio(cap, z) for z in zonas)

    @staticmethod
    def distancia(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
        """Distancia entre colores, 0..1. Sin pretensiones de ser perceptual."""
        return sum(abs(x - y) for x, y in zip(a, b)) / 765.0

    @staticmethod
    def firmas_parecidas(a: Sequence, b: Sequence, umbral: float = 0.06) -> bool:
        if len(a) != len(b):
            return False
        return all(Retina.distancia(x, y) <= umbral for x, y in zip(a, b))

    @staticmethod
    def pantalla_de_paso(firma: Sequence[Tuple[int, int, int]],
                         umbral: float = 0.05) -> bool:
        """¿Esto es una carga —negro, o un color plano— y no una pantalla?

        No hay nada que decidir en una pantalla así, y sin embargo era donde
        más caro salía pensar. Visto en el móvil de Enzo (S65-66), con ZZZ
        arrancando: **seis vueltas seguidas** preguntándole al modelo qué hacer
        con una pantalla negra, ~13 s cada una, y las seis contestaron lo mismo
        —«esperar»—. Ochenta segundos y seis miradas de la cuota tiradas para
        llegar a lo que se ve sin mirar: que aún no había salido nada.

        Dos condiciones, y las dos hacen falta:

        · Todas las zonas casi del mismo color. Un menú, por vacío que
          parezca, tiene rótulos y bordes: sus zonas no coinciden.
        · Y que ese color sea **casi negro o casi blanco**. Esto es lo que
          separa una carga de un fondo liso cualquiera: una transición de juego
          es un fundido, no un gris. Sin la segunda condición, cualquier
          pantalla de fondo plano —un menú oscuro, un diálogo a pantalla
          completa— se toma por carga y se espera a que «termine» algo que ya
          había terminado.
        """
        if not firma:
            return False
        base = firma[0]
        if not all(Retina.distancia(base, c) <= umbral for c in firma):
            return False
        medio = sum(base) / 3.0
        return medio <= 18 or medio >= 237


# Rejilla estándar para identificar una pantalla de un vistazo: 4x6 zonas que
# cubren todo. Su firma es la «huella» de una pantalla — con esto se reconoce
# un menú ya visto sin volver a leerlo.
ZONAS_HUELLA: Tuple[Zona, ...] = tuple(
    Zona(c / 4, f / 6, 1 / 4, 1 / 6) for f in range(6) for c in range(4)
)


# ─────────────────────────────────────────────────────────────────────────
#  Ojo: mirar entendiendo (el carril lento)
# ─────────────────────────────────────────────────────────────────────────

class Ojo:
    """Convierte una pantalla en texto con coordenadas. Sin modelo, sin red.

    Es la pieza que hace innecesaria la mitad de la visión por IA. Un menú de
    juego es texto sobre botones: si sabes QUÉ pone y DÓNDE, ya puedes jugar.
    Y esto lo da tesseract en local, gratis, sin depender de que el modelo de
    turno acierte — la regla de [[feedback_funcionar_cualquier_modelo]].

    Dos pasadas, y no por capricho. Un umbral global se traga los botones de
    color (el naranja de un «JUGAR» cae del mismo lado que su texto blanco y
    desaparece); uno local rescata esos pero marca los bordes de los botones y
    ensucia el resto. Cada una ve lo que a la otra se le escapa, así que se
    lanzan las dos y se unen los resultados.
    """

    def __init__(self, idioma: str = "spa"):
        self.idioma = idioma
        self.disponible = bool(shutil.which("tesseract"))
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "celestia_ojo"
        self._tmp.mkdir(parents=True, exist_ok=True)

    # ── preparación de la imagen ────────────────────────────────────────
    @staticmethod
    def _otsu(hist: Sequence[int]) -> int:
        """Umbral que separa fondo y texto, calculado del histograma.

        Se calcula en vez de fijarlo a mano porque un número puesto a ojo
        funciona en la pantalla donde se probó y en ninguna otra.
        """
        total = sum(hist)
        suma = sum(i * h for i, h in enumerate(hist))
        sB = wB = 0.0
        mejor_t, mejor_var = 0, 0.0
        for t in range(256):
            wB += hist[t]
            if wB == 0:
                continue
            wF = total - wB
            if wF == 0:
                break
            sB += t * hist[t]
            var = wF * wB * ((sB / wB) - ((suma - sB) / wF)) ** 2
            if var > mejor_var:
                mejor_t, mejor_var = t, var
        return mejor_t

    def _variantes(self, png: bytes, escala: float,
                   zona: Optional[Zona] = None) -> List[Tuple[str, Any]]:
        from PIL import Image, ImageChops, ImageFilter
        import io
        im = Image.open(io.BytesIO(png)).convert("L")
        if zona is not None:
            x, y, an, al = zona.pixeles(im.width, im.height)
            im = im.crop((max(0, x), max(0, y),
                          min(im.width, x + an), min(im.height, y + al)))
        if escala != 1.0:
            im = im.resize((max(1, int(im.width * escala)),
                            max(1, int(im.height * escala))), Image.BILINEAR)
        u = self._otsu(im.histogram())
        global_ = im.point(lambda p, u=u: 255 if p > u else 0)
        radio = max(8, int(min(im.width, im.height) * 0.02))
        media = im.filter(ImageFilter.BoxBlur(radio))
        sub = ImageChops.subtract(im, media, scale=1, offset=128)
        local = sub.point(lambda p: 0 if p > 138 else 255)
        return [("global", global_), ("local", local)]

    # 🔴 Sesión 59, medido sobre una pantalla real de este móvil (1084×2412):
    # tesseract paraleliza con OpenMP y aquí eso le hace daño, no bien. Los 8
    # núcleos son heterogéneos (big.LITTLE) y el reparto acaba esperando
    # siempre al más lento. Con un solo hilo salen LAS MISMAS 178 palabras en
    # una cuarta parte del tiempo:
    #     sin tocar (8 núcleos) ... 35.856 ms
    #     OMP_THREAD_LIMIT=2 ...... 24.231 ms
    #     OMP_THREAD_LIMIT=1 ......  8.839 ms   ← este
    # Y no estorba a las dos pasadas en paralelo de `leer()`: al contrario, con
    # un hilo cada una sí caben de verdad en núcleos distintos.
    _ENTORNO_UN_HILO = {**os.environ, "OMP_THREAD_LIMIT": "1"}

    # El tope viejo era 30 s, que es MENOS de lo que tardaba una pantalla real
    # (29-36 s): saltaba el timeout, `_tesseract` devolvía [] y el jugador se
    # quedaba ciego justo cuando había algo que leer. Con un hilo sobra de
    # largo, pero el tope se deja holgado por si el móvil está ocupado.
    _TOPE_OCR = 90

    def _tesseract(self, ruta: Path, psm: int = 11) -> List[Tuple[str, int, int, int, int, float]]:
        cmd = ["tesseract", str(ruta), "stdout", "-l", self.idioma,
               "--psm", str(psm), "--dpi", "300",
               "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0", "tsv"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self._TOPE_OCR,
                               env=self._ENTORNO_UN_HILO)
        except Exception as e:
            logger.warning("tesseract falló: %s", e)
            return []
        fuera = []
        for linea in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
            p = linea.split("\t")
            if len(p) < 12 or not p[11].strip():
                continue
            try:
                conf = float(p[10])
                if conf < 45:
                    continue
                fuera.append((p[11].strip(), int(p[6]), int(p[7]),
                              int(p[8]), int(p[9]), conf))
            except ValueError:
                continue
        return fuera

    @staticmethod
    def _escala_para(zona: Optional[Zona]) -> float:
        """Cuánto encoger la imagen antes del OCR, según lo que se vaya a leer.

        🔴 Sesión 61. El 0,5 se eligió midiendo la pantalla ENTERA del móvil,
        llena de texto pequeño. Sobre una franja con un botón y poco más **no
        lee nada**: mismo recorte, mismas letras,

            escala 0,50 ... 325 ms — nada
            escala 0,75 ... 372 ms — «CONTINUAR»
            escala 1,00 ... 375 ms — «CONTINUAR»

        Y eso rompe justo el carril nuevo, porque una franja tiene poco texto
        **por definición**. Encoger sale a cuenta cuando hay mucho que leer;
        cuando hay poco, los 50 ms que ahorra valen menos que quedarse ciego.
        Así que el recorte pequeño se lee a tamaño natural — que además cuesta
        poco, porque son pocos píxeles.
        """
        if zona is None:
            return 0.5
        area = max(0.0, zona.ancho) * max(0.0, zona.alto)
        return 1.0 if area <= 0.25 else 0.5

    def leer(self, cap: Captura, escala: Optional[float] = None,
             zona: Optional[Zona] = None) -> List[Palabra]:
        """Todas las palabras de la pantalla, con su sitio en fracciones.

        Con `zona` se lee **sólo ese rectángulo**, y es la palanca grande sobre
        el cuello del ciclo: el OCR tarda en proporción a los píxeles, así que
        mirar un cuarto de pantalla cuesta un cuarto. Medido en el móvil de
        Enzo (sesión 61) sobre la misma captura y con el sistema igual de
        cargado en las tres — importan las proporciones, no los absolutos:
            pantalla entera        ... 24.160 ms — 180 palabras
            mitad de abajo         ...  9.859 ms —  71 palabras
            un cuarto (abajo-izq)  ...  6.442 ms —  47 palabras
        No baja del todo en proporción: arrancar tesseract cuesta lo mismo se
        le dé lo que se le dé, y por debajo de medio pantallazo ese suelo pesa
        más que los píxeles. Aun así, mirar donde hay algo que leer en vez de
        la pantalla entera vale un 2,5x.
        Los sitios que devuelve van **siempre en fracciones de la pantalla
        completa**, no del recorte: quien lee no tiene que saber que hubo
        recorte para poder tocar lo que encontró.

        Las dos pasadas van EN PARALELO, y eso vale la mitad del tiempo del
        ciclo entero. Se puede porque tesseract es un proceso aparte — mientras
        trabaja, Python no está reteniendo nada, así que dos hilos aquí sí
        corren de verdad (y con `_ENTORNO_UN_HILO` cada una ocupa un núcleo
        distinto en vez de pelearse por los ocho).

        La escala por defecto no es 1.0 a propósito. Medido con un hilo sobre
        una pantalla real de este móvil, y ojo que **no es monótono**:
            escala 0.4 ...  6.447 ms — 175 palabras (se deja texto)
            escala 0.5 ...  7.400 ms — 179 palabras   ← este
            escala 0.6 ... 13.417 ms — 178 palabras
            escala 1.0 ...  9.162 ms — 180 palabras
        0.6 sale PEOR que 1.0 porque tesseract reescala por dentro a su tamaño
        preferido y desde 0.6 le toca ampliar. 0.5 es el punto bueno: el más
        rápido de los que no pierden palabras. Con letra diminuta, súbela —
        pero súbela a 1.0, no a 0.6.
        """
        if not self.disponible or not cap.png:
            return []
        if escala is None:
            escala = self._escala_para(zona)
        from concurrent.futures import ThreadPoolExecutor

        variantes = self._variantes(cap.png, escala, zona)
        rutas = []
        for nombre, img in variantes:
            ruta = self._tmp / f"v_{nombre}.png"
            img.save(ruta)
            rutas.append((ruta, img.width, img.height))

        with ThreadPoolExecutor(max_workers=len(rutas)) as pool:
            resultados = list(pool.map(lambda r: self._tesseract(r[0]), rutas))

        vistas: Dict[str, Palabra] = {}
        for (ruta, an, al), hallazgos in zip(rutas, resultados):
            for texto, x, y, w, h, conf in hallazgos:
                if zona is None:
                    z = Zona(x / an, y / al, w / an, h / al)
                else:
                    # Del recorte a la pantalla: sin esto, una palabra leída en
                    # la mitad de abajo se tocaría en la mitad de arriba.
                    z = Zona(zona.x + (x / an) * zona.ancho,
                             zona.y + (y / al) * zona.alto,
                             (w / an) * zona.ancho, (h / al) * zona.alto)
                c = z.centro()
                # La misma palabra vista por las dos pasadas: se queda la que
                # más confianza trae, no la última en llegar.
                clave = f"{_sin_acentos(texto.lower())}@{round(c.x, 2)},{round(c.y, 2)}"
                previa = vistas.get(clave)
                if previa is None or conf > previa.confianza:
                    vistas[clave] = Palabra(texto, c, z, conf)
        return sorted(vistas.values(), key=lambda p: (p.centro.y, p.centro.x))

    @staticmethod
    def donde_dice(palabras: Sequence[Palabra], busco: str) -> Optional[Palabra]:
        """Encuentra un texto en la pantalla. Sin acentos y sin distinguir mayúsculas.

        Devuelve la coincidencia exacta si la hay; si no, la que empieza igual.
        Es a propósito tonto: si hiciera falta algo listo aquí, es que el que
        pide no sabe qué está buscando.
        """
        b = _sin_acentos(busco.lower().strip())
        if not b:
            return None
        exactas = [p for p in palabras if _sin_acentos(p.texto.lower()) == b]
        if exactas:
            return max(exactas, key=lambda p: p.confianza)
        parciales = [p for p in palabras if b in _sin_acentos(p.texto.lower())]
        return max(parciales, key=lambda p: p.confianza) if parciales else None


# ─────────────────────────────────────────────────────────────────────────
#  Libro de jugadas: no preguntar dos veces lo mismo
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Accion:
    """Lo que se hace. Un vocabulario corto a propósito.

    Cuanto más corto el vocabulario, menos formas hay de que el modelo se
    invente una acción que no existe — y menos tokens cuesta pedirla.
    """
    tipo: str                          # tocar|deslizar|mantener|escribir|tecla|esperar|andar|camara
    punto: Optional[Punto] = None
    hasta: Optional[Punto] = None
    ms: int = 300
    texto: str = ""
    porque: str = ""                   # para la bitácora, no para el juego
    del_libro: bool = False            # ya la sabía: ni se piensa ni se re-aprende
    # Los movimientos que van DETRÁS de este, sin volver a mirar. Enzo, 7 sep
    # 2026: «nadie juega cada 2 segundos un movimiento, tiene que ser
    # constante». Y no es cuestión de correr más: con Groq a 7.000 tokens de
    # entrada por minuto y 1.024 por pantalla, el techo son ~6,8 miradas por
    # minuto — una acción cada 9 s mientras haya que mirar antes de cada una.
    # La salida no es mirar más deprisa, es traer varios movimientos de cada
    # mirada y encadenarlos dentro del móvil (48 ms por toque allí).
    pasos: List["Accion"] = field(default_factory=list)
    # Para «pelear»: los puntos que hay que vigilar y dónde tocar si cambian.
    # (qué miro, qué toco). Van aquí y no en `pasos` porque no son movimientos
    # que se den: son la regla que se le deja puesta al móvil.
    vigilar: List[Tuple["Punto", "Punto"]] = field(default_factory=list)

    def cadena(self) -> List["Accion"]:
        """Este movimiento y los que van detrás, en orden."""
        return [self] + list(self.pasos)

    def ejecutar(self, mando: Mando) -> None:
        if self.tipo == "tocar" and self.punto:
            mando.tocar(self.punto)
        elif self.tipo == "deslizar" and self.punto and self.hasta:
            mando.deslizar(self.punto, self.hasta, self.ms)
        elif self.tipo == "mantener" and self.punto:
            mando.mantener(self.punto, self.ms)
        elif self.tipo == "escribir":
            mando.escribir(self.texto)
        elif self.tipo == "tecla":
            mando.tecla(self.texto)
        elif self.tipo == "pelear":
            # El móvil se queda jugando solo unos segundos, mirando su propia
            # pantalla 15 veces por segundo. Ver `Vigia`.
            mando.vigilar(self.punto, self.vigilar,
                          max(1, min(30, int(self.ms / 1000))))
        elif self.tipo == "andar":
            # `punto` aquí no es dónde tocar: es HACIA DÓNDE, de -1 a 1. Se
            # reutiliza el campo para no inventar otro, y por eso la acción se
            # construye con `Accion.hacia()` y no a mano.
            mando.andar(None, (self.punto.x if self.punto else 0.0),
                        (self.punto.y if self.punto else 0.0), self.ms)
        elif self.tipo == "camara":
            mando.camara((self.punto.x if self.punto else 0.0),
                         (self.punto.y if self.punto else 0.0), self.ms)
        elif self.tipo == "esperar":
            time.sleep(min(5.0, self.ms / 1000))

    @staticmethod
    def hacia(tipo: str, dx: float, dy: float, ms: int, porque: str = "") -> "Accion":
        """Una acción de dirección —andar o mover la cámara—, no de sitio.

        Las direcciones viajan en `punto` como (dx, dy) de -1 a 1. Es feo pero
        es honesto: el resto del jugador ya habla en `Punto`, y meter un campo
        nuevo obligaría a tocarlo entero para lo mismo.
        """
        return Accion(tipo=tipo, punto=Punto(dx, dy), ms=ms, porque=porque)

    def resumen(self) -> str:
        if self.tipo == "andar" and self.punto:
            return (f"ando hacia ({self.punto.x:+.1f}, {self.punto.y:+.1f}) "
                    f"durante {self.ms} ms")
        if self.tipo == "camara" and self.punto:
            return (f"giro la cámara ({self.punto.x:+.1f}, {self.punto.y:+.1f})")
        if self.tipo in ("tocar", "mantener") and self.punto:
            return f"{self.tipo} en ({self.punto.x:.2f}, {self.punto.y:.2f})"
        if self.tipo == "deslizar" and self.punto and self.hasta:
            return (f"deslizar ({self.punto.x:.2f},{self.punto.y:.2f}) → "
                    f"({self.hasta.x:.2f},{self.hasta.y:.2f})")
        if self.tipo in ("escribir", "tecla"):
            return f"{self.tipo} «{self.texto}»"
        if self.tipo == "pelear":
            return (f"peleo {self.ms // 1000} s seguidos "
                    f"(vigilando {len(self.vigilar)} sitios)")
        return f"{self.tipo} {self.ms}ms"


class LibroDeJugadas:
    """Lo que ya funcionó en una pantalla, para no volver a pensarlo.

    Esta es LA optimización, muy por encima de elegir un modelo más rápido. En
    un juego las pantallas se repiten sin parar: el mismo menú, el mismo
    diálogo de recompensa, el mismo botón de saltar. La primera vez cuesta
    (leer + preguntar ≈ segundo y pico); a partir de la segunda cuesta lo que
    tarda una firma de píxeles, que es nada.

    La huella de una pantalla son los colores medios de una rejilla 4×6. Dos
    pantallas del mismo menú dan huellas casi idénticas aunque se mueva una
    animación de fondo — de ahí que se comparen con tolerancia y no por
    igualdad exacta.

    Se guarda en disco: lo aprendido en una partida sirve en la siguiente. Y no
    se guarda ni una captura, solo 24 colores por pantalla; no hay forma de
    reconstruir qué había ahí (importa: por aquí pasan chats y datos de Enzo).
    """

    def __init__(self, ruta: Optional[Path] = None, tolerancia: float = 0.06):
        self.ruta = ruta
        self.tolerancia = tolerancia
        self.entradas: List[Dict[str, Any]] = []
        # Sin `exists()`: en este PRoot puede lanzar Errno 38 y tumbar el
        # arranque entero. Se intenta abrir, y «no está» es un error más.
        if ruta:
            self.cargar()

    def cargar(self) -> None:
        try:
            self.entradas = json.loads(self.ruta.read_text("utf-8"))
        except FileNotFoundError:
            self.entradas = []          # aún no hay libro: no es un fallo
        except Exception as e:
            logger.warning("No pude leer el libro de jugadas: %s", e)
            self.entradas = []

    def guardar(self) -> None:
        """Guarda el libro SIN poder dejarlo a medias.

        `write_text` vacía el fichero y luego escribe: si el móvil mata el
        proceso en ese hueco —aquí pasa— el libro queda en JSON roto y al
        arrancar se lee como vacío, o sea, se pierde todo lo aprendido. Con un
        temporal y `os.replace` o está el libro viejo o está el nuevo.
        """
        if not self.ruta:
            return
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass                        # Errno 38 con el directorio ya puesto
        tmp = self.ruta.with_name(self.ruta.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.entradas, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.ruta)
        except Exception as e:
            logger.warning("No pude guardar el libro de jugadas: %s", e)
            try:
                tmp.unlink()
            except OSError:
                pass

    def buscar(self, huella: Sequence, objetivo: str) -> Optional[Accion]:
        for e in self.entradas:
            if e.get("objetivo") != objetivo:
                continue
            if Retina.firmas_parecidas(huella, [tuple(c) for c in e["huella"]],
                                       self.tolerancia):
                a = e["accion"]
                return Accion(
                    tipo=a["tipo"],
                    punto=Punto(*a["punto"]) if a.get("punto") else None,
                    hasta=Punto(*a["hasta"]) if a.get("hasta") else None,
                    ms=a.get("ms", 300), texto=a.get("texto", ""),
                    porque="ya me funcionó aquí antes", del_libro=True,
                    # La ráfaga entra en el libro con la jugada: lo que se
                    # aprendió fue la SERIE de golpes que funcionó, no el
                    # primero. Guardar sólo el primero devolvería la partida al
                    # ritmo de un movimiento por pantalla en cuanto el libro
                    # empezara a acertar, que es justo al revés de lo que toca.
                    pasos=[Accion(tipo=q.get("tipo", "tocar"),
                                  punto=Punto(*q["punto"]) if q.get("punto") else None,
                                  ms=q.get("ms", 150))
                           for q in a.get("pasos", [])],
                )
        return None

    def olvidar(self, huella: Sequence, objetivo: str) -> bool:
        """Quita la jugada de esta pantalla. Devuelve si había alguna.

        🔴 Sesión 66, visto en una partida: el libro sólo sabía **añadir**. Una
        jugada que funcionó una vez se repetía para siempre, y si el juego
        cambiaba —un botón que se mueve, un diálogo que ya no sale— se quedaba
        dándole al mismo sitio hasta que la partida moría por «la pantalla
        lleva rato sin cambiar». Tres veces seguidas «ya me funcionó aquí
        antes» y ni un píxel distinto.

        Aprender incluye desaprender: lo que deja de servir se tira, y la
        próxima vez se vuelve a pensar esa pantalla.
        """
        antes = len(self.entradas)
        self.entradas = [
            e for e in self.entradas
            if not (e.get("objetivo") == objetivo and
                    Retina.firmas_parecidas(huella,
                                            [tuple(c) for c in e["huella"]],
                                            self.tolerancia))]
        if len(self.entradas) != antes:
            self.guardar()
            return True
        return False

    def aprender(self, huella: Sequence, objetivo: str, accion: Accion) -> None:
        """Apunta una jugada que FUNCIONÓ. Las que no, no se guardan.

        El matiz importa: guardar lo que se intentó convertiría el libro en un
        catálogo de errores que se repiten solos. Solo entra lo que cambió la
        pantalla a mejor.
        """
        if self.buscar(huella, objetivo):
            return
        self.entradas.append({
            "objetivo": objetivo,
            "huella": [list(c) for c in huella],
            "accion": {
                "tipo": accion.tipo,
                "punto": [accion.punto.x, accion.punto.y] if accion.punto else None,
                "hasta": [accion.hasta.x, accion.hasta.y] if accion.hasta else None,
                "ms": accion.ms, "texto": accion.texto,
                "pasos": [{"tipo": q.tipo,
                           "punto": [q.punto.x, q.punto.y] if q.punto else None,
                           "ms": q.ms} for q in accion.pasos],
            },
            "ts": time.time(),
        })
        self.guardar()


# ─────────────────────────────────────────────────────────────────────────
#  Reflejos: el carril rápido
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Reflejo:
    """Si esta zona se pone así, haz esto. Ni una palabra más.

    Un reflejo no razona ni consulta: compara un color y dispara. Eso es lo que
    permite responder en milisegundos, y también lo que lo hace tonto — no
    entiende el juego, solo reacciona. La cabeza (el modelo, o Enzo) es quien
    decide qué reflejos tiene sentido poner; el bucle solo los ejecuta.
    """
    nombre: str
    zona: Zona
    color: Tuple[int, int, int]
    accion: Accion
    tolerancia: float = 0.10
    enfriamiento_ms: int = 400          # no repetir como un loco el mismo reflejo
    _ultimo: float = field(default=0.0, repr=False)

    def procede(self, cap: Captura, ahora: float) -> bool:
        if (ahora - self._ultimo) * 1000 < self.enfriamiento_ms:
            return False
        actual = Retina.color_medio(cap, self.zona, muestras=9)
        return Retina.distancia(actual, self.color) <= self.tolerancia

    def disparar(self, mando: Mando, ahora: float) -> None:
        self._ultimo = ahora
        self.accion.ejecutar(mando)


# ─────────────────────────────────────────────────────────────────────────
#  Cronómetro: decir la verdad sobre a qué se puede jugar AQUÍ
# ─────────────────────────────────────────────────────────────────────────

class Cronometro:
    """Mide cada eslabón y luego dice, sin adornos, qué da de sí este aparato.

    Existe por lo que pasó con `empeno.py`: al darle manos, empezó a sonar
    igual de segura acertando menos. Un jugador que promete y llega tarde es
    lo mismo. Así que el ritmo no se estima: se mide en el móvil de quien sea,
    con el juego abierto, y el número manda.
    """

    def __init__(self) -> None:
        self.marcas: Dict[str, List[float]] = {}

    def medir(self, etiqueta: str):
        crono = self

        class _Ctx:
            def __enter__(self):
                self.t0 = time.perf_counter()
                return self

            def __exit__(self, *_):
                ms = (time.perf_counter() - self.t0) * 1000
                crono.marcas.setdefault(etiqueta, []).append(ms)
                return False
        return _Ctx()

    def media(self, etiqueta: str) -> float:
        v = self.marcas.get(etiqueta) or []
        return sum(v) / len(v) if v else 0.0

    def ciclo_ms(self) -> float:
        return sum(self.media(e) for e in self.marcas)

    def veredicto(self) -> str:
        """Qué se puede jugar con este ciclo. En cristiano y sin vender humo."""
        c = self.ciclo_ms()
        if c <= 0:
            return "Todavía no he medido nada."
        detalle = ", ".join(f"{e} {self.media(e):.0f} ms"
                            for e in sorted(self.marcas, key=lambda k: -self.media(k)))
        if c < 120:
            cabe = ("Da para juegos de acción con reacciones exigentes, aunque siempre "
                    "por debajo de un humano concentrado.")
        elif c < 400:
            cabe = ("Da para acción con margen (esquivar telegrafiado, combos por "
                    "rotación) y para todo lo lento. Un jefe difícil, no.")
        elif c < 1500:
            cabe = ("Da para menús, recompensas, farmeo y juegos por turnos. Un juego "
                    "de reflejos se pierde: cuando termino de mirar, ya me han dado.")
        else:
            cabe = ("Solo lo que espera por mí: turnos, menús, tareas repetitivas. "
                    "Nada que exija reaccionar.")
        return f"Ciclo medido: {c:.0f} ms ({detalle}). {cabe}"

    # Lo que tarda un humano en reaccionar a algo que ve venir. Por debajo de
    # esto se esquiva; por encima, se come el golpe. En Zenless Zone Zero la
    # ventana de esquiva perfecta ronda los 300 ms, así que ése es el listón
    # que pidió Enzo y con el que se compara.
    LISTON_ACCION_MS = 300.0

    def veredicto_reflejos(self, etiquetas: Sequence[str] = ("ver", "firma", "actuar")) -> str:
        """El ciclo SIN leer ni preguntar al modelo: mirar, comparar, tocar.

        Es el único que puede jugar a algo de acción, porque el otro carril
        arrastra OCR (segundos) o LLM (cientos de ms más la captura). Se mide
        aparte a propósito: mezclarlos da una media que no le sirve a nadie y
        que encima suena mejor de lo que es.
        """
        c = sum(self.media(e) for e in etiquetas if e in self.marcas)
        if c <= 0:
            return "Del carril rápido todavía no he medido nada."
        detalle = ", ".join(f"{e} {self.media(e):.0f} ms"
                            for e in etiquetas if e in self.marcas)
        veces = c / self.LISTON_ACCION_MS
        if c <= self.LISTON_ACCION_MS:
            cabe = ("Llego a una esquiva telegrafiada tipo Zenless Zone Zero, "
                    "aunque justo: un humano concentrado sigue siendo mejor.")
        elif c <= self.LISTON_ACCION_MS * 3:
            cabe = (f"Voy {veces:.1f} veces por encima de la ventana de esquiva de "
                    "Zenless Zone Zero. Sirve para acción con margen, no para pelear.")
        else:
            cabe = (f"Voy {veces:.0f} veces por encima de la ventana de esquiva de "
                    "Zenless Zone Zero: cuando toco, el golpe ya ha entrado. "
                    "Para eso hace falta otro canal, no otro modelo.")
        return f"Carril rápido (sin leer ni preguntar): {c:.0f} ms ({detalle}). {cabe}"


class BucleReflejos:
    """El carril rápido: mirar, comparar, disparar. Sin modelo y sin red.

    Todo lo que hay dentro de esta vuelta tiene que ser barato o no entra. Nada
    de OCR, nada de HTTP, nada de JSON. Si algo de eso se cuela aquí, el bucle
    deja de ser rápido y el jugador deja de servir para lo que Enzo lo pidió.
    """

    def __init__(self, mando: Mando, reflejos: Sequence[Reflejo],
                 crono: Optional[Cronometro] = None):
        self.mando = mando
        self.reflejos = list(reflejos)
        self.crono = crono or Cronometro()

    def correr(self, limites: Limites) -> Dict[str, Any]:
        t0 = time.time()
        disparos: Dict[str, int] = {}
        vueltas = 0
        motivo = "límite de tiempo"
        while True:
            if time.time() - t0 > limites.max_segundos:
                break
            if limites.debo_parar():
                motivo = "me dijiste que parara"
                break
            with self.crono.medir("ver"):
                cap = self.mando.ver(rapido=True)
            if not cap:
                motivo = "me quedé sin imagen de la pantalla"
                break
            vueltas += 1
            ahora = time.time()
            for r in self.reflejos:
                if r.procede(cap, ahora):
                    with self.crono.medir("actuar"):
                        r.disparar(self.mando, ahora)
                    disparos[r.nombre] = disparos.get(r.nombre, 0) + 1
                    break
            if sum(disparos.values()) >= limites.max_jugadas:
                motivo = "llegué al tope de jugadas"
                break
        return {"vueltas": vueltas, "disparos": disparos, "motivo": motivo,
                "segundos": round(time.time() - t0, 1),
                # Este bucle ES el carril rápido: aquí no entra ni OCR ni
                # modelo. El veredicto general mezclaría medias de cosas que no
                # han corrido y diría un ritmo que no es el de esta vuelta.
                "ritmo": self.crono.veredicto_reflejos()}


class Vigia:
    """Mira la pantalla quince veces por segundo y reacciona sin preguntar.

    🔴 Enzo, 8 sep 2026, y es la crítica que ordena todo este fichero: «no
    quiero que mire un png, tiene que ver vídeo y reaccionar sobre la marcha».
    Tiene razón, y conviene decir por qué mandar vídeo a un modelo **no** lo
    resuelve: la vuelta más rápida medida a un proveedor es 0,5 s y la ventana
    de esquiva de ZZZ ronda los 0,3. Aunque el vídeo llegara entero y gratis,
    la respuesta llegaría tarde siempre. No es un problema de modelo: es la
    velocidad de la luz por un cable de por medio.

    Lo que sí llega a tiempo es **no salir del móvil**. Medido aquí (S66),
    cronometrando **desde fuera** el bucle entero, que es lo único fiable:

        `screencap` a ext4 ...................... 163 ms   ← el 90% del coste
        leer un píxel con `od -j` ...............  13 ms
        dar un toque ............................  48 ms
        → una vuelta mirando dos puntos ......... ~230 ms  (4,3 fotogramas/s)

    ⚠️ Y una advertencia de método: el mismo bucle, cronometrándose **por
    dentro** con `date +%s%N`, decía 64 ms por vuelta — cuatro veces menos de
    lo que tardaba de verdad. El reloj de fuera contaba 5,8 s para veinte
    vueltas que por dentro sumaban 1,3. No se cambió el código por ese número
    porque no se comprobó contra nada; si vuelve a aparecer una cifra buena de
    dentro del móvil, contrastarla con el reloj de aquí antes de creérsela.

    O sea: reaccionar a algo que aparece cuesta **~280 ms**. Eso llega a una
    esquiva de ZZZ (~300 ms) **justo**, sin margen. Es mucho mejor que medio
    segundo largo por la red, y no es «reflejos de jugador humano»: para eso
    haría falta un flujo de vídeo de verdad, y el `screenrecord` de este móvil
    (v1.4) no sabe sacar fotogramas crudos — sólo un mp4.

    Eso es ver en continuo y responder sobre la marcha, sin imágenes, sin red y
    sin modelo. Lo que decide el modelo no es cada golpe: es **qué vigilar** —
    dónde está el botón de atacar, qué puntos de la pantalla avisan— y el
    teléfono se encarga del resto durante los segundos siguientes.

    Y no vigila un color fijo, sino el **cambio** respecto a como estaba al
    empezar. Es lo que permite no saberse el juego de memoria: un botón que se
    enciende, un aviso que aparece o un destello son todos «este trozo de
    pantalla ya no es como era», y eso vale igual en ZZZ que en cualquier otro.
    """

    VIVO = f"{MOVIL_DIR}/vigia.vivo"
    LOG = f"{MOVIL_DIR}/vigia.log"
    # En ext4, no en /sdcard: son 10 MB por fotograma y FUSE los cobra caros.
    CRUDO = "/data/local/tmp/.celestia_vigia.raw"
    # El flujo de vídeo, que es lo que de verdad da fotogramas.
    VIDEO = "/data/local/tmp/.celestia_vigia.frames"
    NOTA_VIDEO = "/data/local/tmp/.celestia_vigia.verbose"

    # Lo ancho que se pide el vídeo. No hace falta más: aquí no se lee texto,
    # se mira si un trozo de pantalla ha cambiado de color. Menos ancho es
    # menos que escribir y menos que leer, y el fichero crece mientras dura.
    ANCHO_VIDEO = 96

    # 🔴 Cada píxel del vídeo son TRES bytes (RGB), no cuatro. Medido el 10 sep
    # 2026: dos fotogramas de 96x43 ocupaban 24.768 bytes, exactamente 2·96·43·3.
    # Con 4 el vigía leía píxeles desplazados desde que usa vídeo, y no daba
    # error: con el EX encendido veía el puño «casi blanco» y el relevo «marrón».
    # Atacar funcionaba porque no mira nada; las reglas, no.
    BYTES_POR_PIXEL_VIDEO = 3

    # Cuántos golpes se dan en cada vuelta. Atacar no necesita mirar, así que
    # no tiene por qué ir al ritmo de la vista: con dos por vuelta salen ~7
    # toques por segundo —el ritmo al que machaca una persona— mientras se mira
    # 4 veces por segundo. Uno por vuelta dejaría el ataque en 4/s, que se nota
    # flojo; cuatro llenarían la cola de toques del sistema.
    GOLPES_POR_VUELTA = 2

    def __init__(self, mando: "MandoAndroid", cabecera: int = 16):
        self.mando = mando
        self.cabecera = cabecera

    # ── Reglas con condición ───────────────────────────────────────────
    # Hasta la S72 una regla del vigía sólo sabía «si este punto CAMBIA, toca
    # ahí». Para jugar un equipo no basta: el EX hay que pulsarlo cuando la
    # estrella está ENCENDIDA —apagarse también es cambiar, y ahí pulsarla no
    # sirve—, y el relevo cuando pierde el amarillo porque enseña «ASSIST».
    # Medido en ZZZ (10 sep 2026) sobre capturas reales, en píxel suelto y en
    # el vídeo de 96 px, que son los dos carriles del vigía:
    #
    #     anillo del EX ...... encendido s 100-205 · gris s 0-3
    #     centro del relevo .. amarillo s 151-243 · con ASSIST s 12-38
    #
    # Una regla es (mirado, toque) —«cambia», lo de siempre— o
    # (mirado, toque, "encendido"|"apagado"[, saturación]).
    SAT_ENCENDIDO = 50
    SAT_APAGADO = 80
    # Vueltas que una regla con condición se queda quieta después de tocar: el
    # botón tarda unos fotogramas en apagarse, y sin esto la misma estrella se
    # pulsaría cuatro veces seguidas por un solo EX.
    ESPERA_TRAS_REGLA = 3
    # 🔴 Vueltas seguidas sin guardia (sin la interfaz de pelea a la vista) tras las
    # que el bucle se para solo. 10 sep 2026, 23:49: ZZZ se reinició a mitad de un
    # tramo y el vigía —que corre DENTRO del móvil y no mira qué app hay delante—
    # siguió 14 s dando golpes a ciegas: 153 toques y 17 «ASSIST» sobre lo que
    # hubiera. Ahora los golpes también pasan por la guardia, y sin ella se sale.
    MAX_FALLOS_GUARDA = 6

    @classmethod
    def _regla(cls, v: Sequence[Any]) -> Tuple[Punto, Punto, str, int]:
        mirado, toque = v[0], v[1]
        cond = str(v[2]) if len(v) > 2 else "cambia"
        if cond not in ("cambia", "encendido", "apagado"):
            cond = "cambia"
        if len(v) > 3:
            sat = int(v[3])
        else:
            sat = cls.SAT_ENCENDIDO if cond == "encendido" else cls.SAT_APAGADO
        return mirado, toque, cond, sat

    @staticmethod
    def _antes_del_bucle(reglas: Sequence[Tuple[Punto, Punto, str, int]],
                         lector: Callable[[Punto], str],
                         guarda: Optional[Tuple[Punto, int]]) -> List[str]:
        """Las referencias —el color de partida de cada «cambia» y de la
        guardia— y los contadores de cada regla, que son lo que dice después
        si el EX o la asistencia llegaron a dispararse."""
        lineas: List[str] = []
        for i, (mirado, _t, cond, _s) in enumerate(reglas, 1):
            if cond == "cambia":
                lineas.append(f"{lector(mirado)}; r{i}=$1; g{i}=$2; b{i}=$3; t{i}=0")
            else:
                lineas.append(f"e{i}=0; t{i}=0")
        if guarda is not None:
            lineas.append(f"{lector(guarda[0])}; gr=$1; gg=$2; gb=$3; fg=0")
        return lineas

    def _reglas_en_shell(self, reglas: Sequence[Tuple[Punto, Punto, str, int]],
                         lector: Callable[[Punto], str], ancho: int, alto: int,
                         umbral: int, guarda: Optional[Tuple[Punto, int]]) -> List[str]:
        """Una vuelta de reglas, en shell.

        La guardia es un punto de la interfaz que no cambia nunca —el botón de
        atacar—: si deja de parecerse a como estaba al empezar, la interfaz no
        está (una cadena, una definitiva, una carga), y un relevo «sin
        amarillo» ahí no es un ASSIST. Se compara contra su PROPIO color de
        partida, que es lo que la hace valer igual en el vídeo reducido que en
        la captura entera, donde el mismo gris sale con otros números.
        """
        lineas: List[str] = []
        if guarda is not None:
            tol = int(guarda[1]) ** 2 * 3
            lineas.append(
                f"  {lector(guarda[0])}; [ -z \"$1\" ] && break; ok=0; "
                f"[ $(( ($1-$gr)*($1-$gr) + ($2-$gg)*($2-$gg) + ($3-$gb)*($3-$gb) )) "
                f"-lt {tol} ] && ok=1; "
                # «-eq 0» y no «-eq 1»: las pruebas cuentan los «[ $ok -eq 1 ]» para
                # asegurar que cada disparo pasa por la guardia, y el contador de
                # fallos no es un disparo.
                f"[ $ok -eq 0 ] && fg=$((fg+1)) || fg=0; "
                f"[ $fg -ge {self.MAX_FALLOS_GUARDA} ] && break")
        elif any(c != "cambia" for _m, _t, c, _s in reglas):
            lineas.append("  ok=1")
        for i, (mirado, toque, cond, sat) in enumerate(reglas, 1):
            tx, ty = toque.pixeles(ancho, alto)
            if cond == "cambia":
                lineas.append(
                    f"  {lector(mirado)}; [ -z \"$1\" ] && break; "
                    f"d=$(( ($1-$r{i})*($1-$r{i}) + ($2-$g{i})*($2-$g{i}) + "
                    f"($3-$b{i})*($3-$b{i}) )); "
                    f"if [ $d -gt {umbral} ]; then input tap {tx} {ty}; "
                    f"n=$((n+1)); t{i}=$((t{i}+1)); r{i}=$1; g{i}=$2; b{i}=$3; fi")
                continue
            signo = "-gt" if cond == "encendido" else "-lt"
            lineas.append(
                f"  if [ $e{i} -gt 0 ]; then e{i}=$((e{i}-1)); else "
                f"{lector(mirado)}; [ -z \"$1\" ] && break; "
                f"mx=$1; [ $2 -gt $mx ] && mx=$2; [ $3 -gt $mx ] && mx=$3; "
                f"mn=$1; [ $2 -lt $mn ] && mn=$2; [ $3 -lt $mn ] && mn=$3; "
                f"if [ $ok -eq 1 ] && [ $((mx-mn)) {signo} {sat} ]; then "
                f"input tap {tx} {ty}; n=$((n+1)); t{i}=$((t{i}+1)); "
                f"e{i}={self.ESPERA_TRAS_REGLA}; fi; fi")
        return lineas

    def area_de_video(self) -> Optional[Tuple[int, int]]:
        """Qué tamaño tiene de verdad el fotograma del flujo. No se calcula.

        `screenrecord` **no da el tamaño que se le pide**: lo ajusta al aspecto
        de la pantalla y lo redondea. Pedidos 64x142 salen 63x142, y pedidos
        160x90 salen **40x90 con las bandas negras quitadas**. Equivocarse aquí
        no da error: desplaza el píxel que se mira, exactamente el mismo daño
        que la cabecera de `screencap` (ver `ReflejoDelegado.medir_cabecera`).
        Así que se lanza un segundo de vídeo y se lee lo que él mismo dice.
        """
        # Medirlo cuesta 3,3 s —hay que lanzar un vídeo de prueba— y no cambia
        # mientras no gire la pantalla: se pregunta una vez por móvil.
        guardado = getattr(self.mando, "_area_video", "sin medir")
        if guardado != "sin medir":
            return guardado
        an, al = self.mando.resolucion_vista()
        alto = max(2, int(round(self.ANCHO_VIDEO * al / max(1, an))))
        salida = self.mando._sh(
            f"rm -f {self.NOTA_VIDEO}; "
            f"timeout 3 screenrecord --verbose --output-format=raw-frames "
            f"--size {self.ANCHO_VIDEO}x{alto} --time-limit 1 {self.VIDEO} "
            f"2>{self.NOTA_VIDEO}; grep -i 'content area' {self.NOTA_VIDEO}",
            timeout=40) or ""
        m = re.search(r"Content area is (\d+)x(\d+)", salida)
        area = (int(m.group(1)), int(m.group(2))) if m else None
        if area is None:
            logger.info("este móvil no da fotogramas de vídeo (%r): "
                        "vigilo con capturas sueltas", salida.strip()[:80])
        try:
            self.mando._area_video = area
        except Exception:
            pass
        return area

    def _guion_video(self, area: Tuple[int, int], ataque: Optional[Punto],
                     vigilados: Sequence[Tuple[Punto, Punto]],
                     umbral: int, max_segundos: int, max_toques: int,
                     guarda: Optional[Tuple[Punto, int]] = None) -> str:
        """El bucle sobre el FLUJO de vídeo, no sobre capturas sueltas.

        `screenrecord` va escribiendo fotogramas al final de un fichero; aquí
        se mira cuánto ha crecido y se lee **el último completo**. Sin tuberías
        ni esperas: si todavía no hay fotograma nuevo, se vuelve a mirar.

        Y la diferencia con capturar de una en una es toda: `screencap` cuesta
        **152 ms** aunque se tire el resultado a /dev/null —o sea que el coste
        es capturar, no guardar—, mientras que aquí el fotograma ya está hecho
        y leerlo son 13 ms. Lo que antes eran 4 vueltas por segundo pasa a ir
        al ritmo al que el juego dibuja.

        ⚠️ El flujo sólo produce cuando la pantalla CAMBIA: con la pantalla
        quieta salen unos pocos bytes en tres segundos. En un juego eso no es
        un problema —cambia siempre— y de hecho es lo que hace que no se gaste
        nada mirando un menú parado.
        """
        an, al = area
        marco = an * al * self.BYTES_POR_PIXEL_VIDEO
        lineas = [f": > {self.VIVO}", f"rm -f {self.VIDEO}", "n=0", "v=0",
                  "ini=$(date +%s)",
                  f"screenrecord --output-format=raw-frames --size {an}x{al} "
                  f"--time-limit {max_segundos} {self.VIDEO} 2>/dev/null &",
                  "grabando=$!",
                  # Sin un primer fotograma no hay referencia que comparar.
                  "espera=0",
                  f"while [ $(stat -c %s {self.VIDEO} 2>/dev/null || echo 0) "
                  f"-lt {marco} ]; do",
                  "  espera=$((espera+1))",
                  "  [ $espera -gt 300 ] && break",
                  "  sleep 0.05",
                  "done"]

        def leer(p: Punto, var: str) -> str:
            px = min(an - 1, max(0, int(p.x * an)))
            py = min(al - 1, max(0, int(p.y * al)))
            desplazamiento = (py * an + px) * self.BYTES_POR_PIXEL_VIDEO
            return (f"set -- $(od -An -tu1 -j $(( ($m-1)*{marco} + "
                    f"{desplazamiento} )) -N 3 {self.VIDEO} 2>/dev/null); "
                    f"{var}")

        lineas.append(f"m=$(( $(stat -c %s {self.VIDEO}) / {marco} ))")
        reglas = [self._regla(r) for r in vigilados]

        def lector(p: Punto) -> str:
            return leer(p, "").rstrip("; ")

        lineas += self._antes_del_bucle(reglas, lector, guarda)
        lineas.append(f"while [ -f {self.VIVO} ]; do")
        lineas.append(f"  m=$(( $(stat -c %s {self.VIDEO} 2>/dev/null || echo 0) "
                      f"/ {marco} ))")
        lineas.append("  [ $m -lt 1 ] && break")
        lineas.append("  v=$((v+1))")
        lineas += self._reglas_en_shell(reglas, lector, *self.mando.resolucion_vista(),
                                        umbral=umbral, guarda=guarda)
        if ataque is not None:
            ax, ay = ataque.pixeles(*self.mando.resolucion_vista())
            golpes = "; ".join([f"input tap {ax} {ay}"] * self.GOLPES_POR_VUELTA)
            if guarda is not None:
                lineas.append(f"  if [ $ok -eq 1 ]; then {golpes}; n=$((n+{self.GOLPES_POR_VUELTA})); fi")
            else:
                for _ in range(self.GOLPES_POR_VUELTA):
                    lineas.append(f"  input tap {ax} {ay}")
                lineas.append(f"  n=$((n+{self.GOLPES_POR_VUELTA}))")
        lineas.append(f"  [ $n -ge {max_toques} ] && break")
        lineas.append(f"  [ $(( $(date +%s) - $ini )) -ge {max_segundos} ] && break")
        lineas.append("done")
        lineas.append("kill $grabando 2>/dev/null")
        # El fichero crece ~1 MB por segundo: no se puede dejar ahí.
        lineas.append(f"rm -f {self.VIVO} {self.VIDEO}")
        cuentas = ",".join(f"$t{i}" for i in range(1, len(reglas) + 1))
        lineas.append(f'echo "toques=$n vueltas=$v reglas={cuentas}" > {self.LOG}')
        return "\n".join(lineas)

    def _lee_pixel(self, x_px: int, y_px: int, ancho: int) -> str:
        """El trozo de shell que saca un píxel del framebuffer, sin moverlo.

        `od -j` salta por sí solo, así que es UN proceso y no dos. Medido en el
        móvil, veinte lecturas seguidas: `dd | od` 19 ms, `od -j -N` **13**.
        Parece poco y no lo es: se paga en cada punto vigilado y en cada
        fotograma.
        """
        salto = self.cabecera + (y_px * ancho + x_px) * 4
        return f"od -An -tu1 -j {salto} -N 3 {self.CRUDO} 2>/dev/null"

    def _guion(self, ancho: int, alto: int, ataque: Optional[Punto],
               vigilados: Sequence[Tuple[Punto, Punto]], ritmo_ms: int,
               umbral: int, max_segundos: int, max_toques: int,
               guarda: Optional[Tuple[Punto, int]] = None) -> str:
        """El bucle que corre DENTRO del móvil. Aquí no entra nadie de fuera.

        Primero toma una referencia de cada punto vigilado —cómo está la
        pantalla cuando empezamos— y luego compara en cada vuelta. La distancia
        va al cuadrado para no sacar raíces en shell.
        """
        lineas = [f": > {self.VIVO}", "n=0", "v=0", "ini=$(date +%s)",
                  f"screencap {self.CRUDO} 2>/dev/null"]
        reglas = [self._regla(r) for r in vigilados]

        def lector(p: Punto) -> str:
            x, y = p.pixeles(ancho, alto)
            return f"set -- $({self._lee_pixel(x, y, ancho)})"

        lineas += self._antes_del_bucle(reglas, lector, guarda)
        lineas.append(f"while [ -f {self.VIVO} ]; do")
        lineas.append(f"  screencap {self.CRUDO} 2>/dev/null || break")
        # Las vueltas se cuentan siempre, se ataque o no: son los fotogramas
        # vistos, y es el número que dice si esto va a 15 por segundo o a 2.
        lineas.append("  v=$((v+1))")
        lineas += self._reglas_en_shell(reglas, lector, ancho, alto,
                                        umbral=umbral, guarda=guarda)
        if ataque is not None:
            ax, ay = ataque.pixeles(ancho, alto)
            golpes = "; ".join([f"input tap {ax} {ay}"] * self.GOLPES_POR_VUELTA)
            if guarda is not None:
                lineas.append(f"  if [ $ok -eq 1 ]; then {golpes}; n=$((n+{self.GOLPES_POR_VUELTA})); fi")
            else:
                for _ in range(self.GOLPES_POR_VUELTA):
                    lineas.append(f"  input tap {ax} {ay}")
                lineas.append(f"  n=$((n+{self.GOLPES_POR_VUELTA}))")
        lineas.append(f"  [ $n -ge {max_toques} ] && break")
        lineas.append(f"  [ $(( $(date +%s) - $ini )) -ge {max_segundos} ] && break")
        if ritmo_ms > 0:
            lineas.append(f"  sleep {ritmo_ms / 1000.0:.2f}")
        lineas.append("done")
        lineas.append(f"rm -f {self.VIVO} {self.CRUDO}")
        cuentas = ",".join(f"$t{i}" for i in range(1, len(reglas) + 1))
        lineas.append(f'echo "toques=$n vueltas=$v reglas={cuentas}" > {self.LOG}')
        # Con saltos de línea, no con «;». Unir con «; » deja `while …; do;`,
        # que es un error de sintaxis del shell — y el síntoma no se parece en
        # nada a la causa: el agente del móvil se queda mudo 38 s y el canal se
        # da por perdido («no contestó, sigo con una orden por proceso»).
        return "\n".join(lineas)

    def correr(self, ataque: Optional[Punto],
               vigilados: Sequence[Tuple[Punto, Punto]] = (),
               segundos: int = 20, ritmo_ms: int = 0, umbral: int = 2500,
               max_toques: int = 400,
               guarda: Optional[Tuple[Punto, int]] = None) -> str:
        """Deja el vigía puesto y espera a que acabe. Devuelve qué hizo.

        Los topes no son adorno: es un bucle tocando la pantalla de Enzo sin
        nadie delante. Para en los segundos, en los toques, o al borrarse su
        `.vivo` —que es como se apaga desde fuera, y como una sesión muerta no
        deja el móvil dándose toques solo.
        """
        vigilados = list(vigilados)[:4]
        # Primero el vídeo, que es lo que da fotogramas de verdad. Si este
        # móvil no lo sirve, se sigue con capturas sueltas: más lento, pero
        # nunca «no puedo mirar».
        area = self.area_de_video() if vigilados else None
        if area:
            guion = self._guion_video(area, ataque, vigilados, umbral,
                                      max(1, segundos), max(1, max_toques),
                                      guarda=guarda)
        else:
            ancho, alto = self.mando.resolucion_vista()
            guion = self._guion(ancho, alto, ataque, vigilados,
                                ritmo_ms, umbral, max(1, segundos),
                                max(1, max_toques), guarda=guarda)
        try:
            os.unlink(self.LOG)
        except OSError:
            pass
        # El tope de aquí va por encima del de allí: el que manda es el del
        # móvil, y este sólo existe para no esperar a un muerto.
        self.mando._sh(guion, timeout=segundos + 30)
        try:
            with open(self.LOG) as f:
                return f.read().strip()
        except OSError:
            return "no sé qué pasó: el vigía no dejó nota"

    def parar(self) -> None:
        """Apaga el vigía desde fuera, sin esperar a sus topes."""
        try:
            os.unlink(self.VIVO)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────
#  Jugador: el carril lento, con cabeza
# ─────────────────────────────────────────────────────────────────────────

INSTRUCCION = """Juegas en el móvil de alguien. Te doy lo que se lee en la pantalla \
con su posición en fracciones (0..1: 0,0 arriba-izquierda; 1,1 abajo-derecha).

Objetivo: {objetivo}

Pantalla:
{pantalla}

{historial}
Responde SOLO con un JSON, sin explicar nada:
{{"accion":"tocar|deslizar|mantener|escribir|tecla|esperar|listo","x":0.5,"y":0.5,\
"x2":0,"y2":0,"ms":300,"texto":"","porque":"3 palabras"}}
- "tocar": pon x,y donde haya que tocar.
- "deslizar": de x,y a x2,y2.
- "listo": el objetivo ya está cumplido.
- Si no ves nada útil, "esperar" con ms.
No toques nada que cueste dinero."""


class ReflejoDelegado:
    """La regla se le manda al móvil y la ejecuta él. Sin canal en el bucle.

    Sesión 61, y es la única forma medida de bajar de 300 ms. Enzo: «pues tiene
    que haber algo para llegar a los 300 ms y menos».

    El carril rápido ya no preguntaba al modelo, pero seguía dando **dos
    vueltas por el canal** en cada ciclo —una para capturar y otra para tocar—
    y cada vuelta son ~120 ms de ficheros en `/sdcard`, que es FUSE. Con eso
    el suelo estaba en 400 ms hiciera lo que hiciera. Medido dentro del móvil,
    en cambio:

        screencap ......... 164 ms
        leer un píxel .....  28 ms   (`dd` + `od`)
        input tap .........  48 ms   (por el canal eran 131)
        ─────────────────────────
        ciclo ............. 240 ms

    O sea que el trabajo de verdad cabe de sobra en el presupuesto; lo que no
    cabía era el ir y venir. Así que aquí no se manda una orden: se manda **la
    regla entera** —«mira este punto; si está de este color, toca aquí»— y el
    móvil la repite solo.

    ⚠️ **Esto deja un bucle tocando la pantalla sin nadie mirando**, así que los
    topes no son adorno: se para al llegar al máximo de toques, al máximo de
    segundos, o en cuanto desaparece su fichero `.vivo` —que es como se apaga
    desde fuera, y lo que hace que una sesión muerta no deje al móvil solo
    dándose toques—. Lo que sabe hacer es un reflejo, no una partida: un color
    y un punto. Lo demás sigue siendo del carril lento.
    """

    VIVO = f"{MOVIL_DIR}/reflejo.vivo"
    LOG = f"{MOVIL_DIR}/reflejo.log"
    # Dentro del móvil, en ext4: /sdcard sería FUSE y son 10 MB por vuelta.
    CRUDO = "/data/local/tmp/.celestia_reflejo.raw"

    def __init__(self, mando: "MandoAndroid", cabecera: int = 16):
        self.mando = mando
        # screencap escribe 12 o 16 bytes de cabecera según la versión de
        # Android. Aquí no se adivina: se mide con una captura real.
        self.cabecera = cabecera

    def medir_cabecera(self) -> int:
        """Cuántos bytes van antes de los píxeles. Se mide, no se supone.

        Son 12 o 16 según la versión de Android, y equivocarse **no da error**:
        desplaza el píxel que se mira unas posiciones y el reflejo dispara
        mirando otra cosa. Como el tamaño de los píxeles se sabe
        (ancho×alto×4), la cabecera es lo que sobra del fichero.
        """
        ancho, alto = self.mando.resolucion()
        remoto = f"{self.mando._CAPTURA_REMOTA}.raw"
        self.mando._sh(f"screencap {remoto}", timeout=30)
        try:
            sobra = os.path.getsize(remoto) - ancho * alto * 4
        except OSError as e:
            logger.warning("no pude medir la cabecera de screencap: %s", e)
            return self.cabecera
        if sobra in (12, 16):
            self.cabecera = sobra
        else:
            logger.warning("la captura no mide lo que debería (sobran %d bytes); "
                           "sigo con cabecera %d", sobra, self.cabecera)
        return self.cabecera

    def _guion(self, x_px: int, y_px: int, ancho: int, color: Tuple[int, int, int],
               tolerancia: int, toque: Tuple[int, int], max_toques: int,
               max_segundos: int) -> str:
        """El bucle que corre DENTRO del móvil.

        `dd` con `bs=1 skip=…` lee sólo los 3 bytes del píxel: da igual que el
        framebuffer pese 10 MB, porque no se mueve de allí. El umbral va al
        cuadrado para no sacar raíces en shell.
        """
        salto = self.cabecera + (y_px * ancho + x_px) * 4
        r, g, b = color
        tx, ty = toque
        return (
            f"n=0; ini=$(date +%s); "
            f": > {self.VIVO}; "
            f"while [ -f {self.VIVO} ]; do "
            f"screencap {self.CRUDO} 2>/dev/null || break; "
            f"set -- $(dd if={self.CRUDO} bs=1 skip={salto} count=3 2>/dev/null "
            f"| od -An -tu1); "
            f'[ -z "$1" ] && break; '
            f"d=$(( ($1-{r})*($1-{r}) + ($2-{g})*($2-{g}) + ($3-{b})*($3-{b}) )); "
            f"if [ $d -le {tolerancia} ]; then "
            f"input tap {tx} {ty}; n=$((n+1)); echo \"toques=$n\" > {self.LOG}; "
            f"[ $n -ge {max_toques} ] && break; fi; "
            f"[ $(( $(date +%s) - $ini )) -ge {max_segundos} ] && break; "
            f"done; "
            f"rm -f {self.VIVO} {self.CRUDO}; echo \"fin toques=$n\" > {self.LOG}"
        )

    def correr(self, zona: Zona, color: Tuple[int, int, int], toque: Punto,
               tolerancia: int = 900, max_toques: int = 20,
               max_segundos: int = 60) -> str:
        """Deja el reflejo puesto y espera a que termine. Devuelve qué hizo."""
        ancho, alto = self.mando.resolucion()
        c = zona.centro()
        guion = self._guion(
            int(c.x * ancho), int(c.y * alto), ancho, color, tolerancia,
            (int(toque.x * ancho), int(toque.y * alto)),
            max(1, max_toques), max(1, max_segundos))
        try:
            os.unlink(self.LOG)
        except OSError:
            pass
        # El tope de aquí va por encima del de allí: el que manda es el del
        # móvil, y este sólo existe para no quedarse esperando a un muerto.
        self.mando._sh(guion, timeout=max_segundos + 30)
        try:
            with open(self.LOG) as f:
                return f.read().strip()
        except OSError:
            return "no sé qué pasó: el reflejo no dejó nota"

    def parar(self) -> None:
        """Apaga el reflejo desde fuera, sin esperar a sus topes."""
        try:
            os.unlink(self.VIVO)
        except OSError:
            pass


class Marcador:
    """Le numera al modelo los sitios tocables, para que sólo tenga que elegir.

    🔴 Sesión 61, el hallazgo que más baja el ciclo. El modelo **ve** la
    pantalla pero **no la mide**: pidiéndole el centro de un botón se equivoca
    hasta 443 px a lo ancho, o sea que el dedo cae fuera. Medido de las tres
    formas, sobre los mismos dos botones:

        pedirle las coordenadas .................... 454 px de error  ❌
        + una rejilla dibujada encima .............. 248 px de error  ❌
        numerarle los recuadros y que elija uno ....   2 px de error  ✅

    Lo que cambia no es el modelo, es la pregunta: elegir entre cuatro
    recuadros es una tarea que sabe hacer; medir en píxeles, no. Y como las
    cajas las hemos puesto nosotros, el punto exacto lo tenemos ya — sin OCR,
    que era el cuello (1.714 ms sobre pantalla real frente a **91 ms** de esto).

    Cómo se encuentran los candidatos: el color de fondo es el más repetido, y
    lo que no es fondo se agrupa en manchas conexas; las del tamaño de un botón
    se quedan. Es tosco a propósito — no hace falta acertar qué es un botón,
    sólo dar unos pocos sitios entre los que el modelo elija bien.

    ⚠️ **Dónde se le ve el límite:** en una UI plana (menús, recompensas,
    inventarios) va bien. Sobre un juego en marcha, con el escenario de fondo,
    «lo que no es fondo» deja de significar nada — puede salir una mancha
    gigante o cientos. Por eso hay topes, y si no se cumplen se sigue por el
    camino del OCR, que no depende de que el fondo sea liso.
    """

    # Con uno solo ya compensa: no es por darle a elegir, es porque la caja la
    # medimos nosotros y el punto sale exacto. Lo que sobra es el otro extremo
    # —la imagen llena de números encima de lo que hay que mirar—, y sobre todo
    # es la señal de que aquí no hay fondo que valga y toca el OCR.
    MIN_CAJAS = 1
    MAX_CAJAS = 20

    @staticmethod
    def precalentar() -> None:
        """Cargar numpy/scipy/PIL ahora, no en la primera jugada.

        Importarlos cuesta **1.245 ms medidos** la primera vez y 0 las
        siguientes. Sin esto, ese segundo y cuarto se lo come la primera
        jugada de cada partida — la que peor sienta que vaya lenta, igual que
        pasaba con el arranque del puente.
        """
        try:
            import numpy            # noqa: F401
            from PIL import Image   # noqa: F401
            from scipy import ndimage  # noqa: F401
        except Exception as e:      # pragma: no cover
            logger.debug("sin numpy/scipy no habrá marcado: %s", e)

    @staticmethod
    def candidatos(png: bytes, div: int = 8) -> List[Zona]:
        """Los sitios que podrían ser tocables, en fracciones de pantalla."""
        try:
            import io
            import numpy as np
            from PIL import Image
            from scipy import ndimage
        except Exception as e:                       # pragma: no cover
            logger.debug("sin numpy/scipy no hay marcado: %s", e)
            return []
        try:
            im = Image.open(io.BytesIO(png)).convert("RGB")
            an_o, al_o = im.width, im.height
            # Se trabaja sobre una miniatura: un botón sigue siendo un botón a
            # 1/8, y el coste baja de segundos a decenas de ms.
            ch = im.resize((max(1, an_o // div), max(1, al_o // div)), Image.BILINEAR)
            a = np.asarray(ch).astype(np.int16)
            h, w, _ = a.shape
            q = (a // 32).reshape(-1, 3)
            uni, cnt = np.unique(q, axis=0, return_counts=True)
            fondo = uni[int(np.argmax(cnt))]
            masc = (np.abs(q - fondo).sum(1).reshape(h, w)) > 1
            etiquetas, cuantas = ndimage.label(masc)
            cajas: List[Zona] = []
            for y, x in ndimage.find_objects(etiquetas):
                ancho, alto = (x.stop - x.start) * div, (y.stop - y.start) * div
                # Ni una letra suelta ni media pantalla: tamaño de botón.
                if not (120 <= ancho <= an_o * 0.9 and 50 <= alto <= al_o * 0.4):
                    continue
                cajas.append(Zona(x.start * div / an_o, y.start * div / al_o,
                                  ancho / an_o, alto / al_o))
                if len(cajas) > Marcador.MAX_CAJAS:
                    # 🔴 Pasarse del tope no se arregla cortando la lista: eso
                    # disfraza de pantalla normal lo que es justo la señal de
                    # que aquí no hay fondo liso (un juego en marcha, con el
                    # escenario detrás). Cortando, el modelo elegiría entre 20
                    # recuadros inventados y el dedo caería en cualquier parte.
                    # Lo encontró un test con ruido puro, que yo daba por hecho
                    # que devolvería cero cajas y devolvía un montón.
                    logger.info("demasiados sitios candidatos: aquí no marco, "
                                "que lo resuelva el OCR")
                    return []
            return cajas
        except Exception as e:
            logger.warning("no pude buscar sitios que tocar: %s", e)
            return []

    @staticmethod
    def marcar(png: bytes, cajas: Sequence[Zona]) -> Optional[bytes]:
        """La misma pantalla con cada candidato recuadrado y numerado."""
        try:
            import io
            from PIL import Image, ImageDraw, ImageFont
            im = Image.open(io.BytesIO(png)).convert("RGB")
            d = ImageDraw.Draw(im)
            try:
                f = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
            except OSError:
                f = None
            for i, z in enumerate(cajas, 1):
                x, y, an, al = z.pixeles(im.width, im.height)
                # Rosa: un color que no suele estar en un juego, para que el
                # modelo no confunda la marca con parte de la pantalla.
                d.rectangle([x, y, x + an, y + al], outline="#ff0066", width=6)
                d.rectangle([x, max(0, y - 56), x + 70, y], fill="#ff0066")
                d.text((x + 16, max(0, y - 52)), str(i), font=f, fill="#ffffff")
            # JPEG y no PNG, por lo mismo que la franja de `Rejilla`: la
            # pantalla marcada se manda en CADA vuelta, y guardarla en PNG
            # costaba 770 ms medidos —más que mirarla— porque los recuadros
            # rosa arruinan la compresión sin pérdida. En JPEG son ~80 ms, y
            # al otro lado los números se leen igual de bien: son de 44 px.
            buf = io.BytesIO(); im.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        except Exception as e:
            logger.warning("no pude numerar la pantalla: %s", e)
            return None


class Rejilla:
    """Numerarle una FRANJA de la pantalla para que diga en qué casilla está.

    Sesión 62, y el diseño lo decidió una medida que tumbó al anterior. La S61
    dejó apuntada una «rejilla numerada en dos pasos» (6x4 sobre la pantalla
    entera → recorte → 4x4) probada **a mano**: la geometría salía a 8 px del
    botón. Pero a mano se probaba lo fácil —si eliges la celda buena, el punto
    cae bien— y no lo difícil: **que el modelo elija la celda**. Preguntado de
    verdad sobre la pantalla real de ZZZ, pide la celda 23 y contesta **18 o
    24**; y desde una celda vecina el segundo paso **no recupera** (eligió la
    14, fuera del botón). No es torpeza suya: el botón cae justo encima de una
    línea de la rejilla, así que la pregunta no tiene una respuesta clara.

    Lo que sí funciona es **quitar la fila de la pregunta**. La altura ya la
    acierta el modelo con 8 px (medido en la S61), así que se recorta la franja
    de esa altura y se numera **a lo ancho**. Elegir entre casillas en fila es
    una pregunta de una sola dimensión y ahí no falla. Medido contra Groq sobre
    la pantalla real, buscando el botón «Reclamar» (que está en x 1770..1954):

        8 casillas ..... elige la 7 → x 1960, 98 px de error, FUERA por 6 px
        12 casillas .... elige la 10 → x 1910, 48 px, DENTRO del botón ✅

    Con 12 sale, con 8 no: la casilla tiene que ser más estrecha que el botón,
    y no es un ajuste fino sino la condición de que esto funcione. Comprobado
    también con un segundo botón de la misma pantalla («Sin reclamar» → casilla
    11, dentro) y con un objetivo que NO estaba en la franja, donde contestó
    **0** en vez de inventarse una casilla — que es lo que permite no tocar a
    ciegas.

    Y de paso sale mucho más barata que mirar la pantalla entera: la franja son
    **61 KB** en JPEG frente a 367 KB de la pantalla completa (2,6 MB en PNG),
    y eso viaja por el WiFi de Enzo en cada jugada.
    """

    # 12 casillas a lo ancho: con 8, la elegida cayó 6 px fuera del botón.
    CASILLAS = 12
    # La franja que se recorta alrededor de la altura que dijo el modelo. Su
    # error medido es de 8 px, pero se le da margen de sobra: recortar
    # estrecho no ahorra casi nada y dejar el botón fuera lo estropea todo.
    ALTO_FRANJA = 0.16
    # Y la franja va en TRES filas, no en una. La `y` la pone el modelo, y en
    # la prueba real se quedó a **1 px** del borde de abajo del botón (dijo 846
    # donde el centro estaba en 818): con una sola fila esa altura no hay quien
    # la corrija. Con tres, elegir casilla también corrige la altura, y cuando
    # ya la tenía bien elige la fila de en medio y no cambia nada — o sea que
    # no puede salir peor. Medido: acierta la casilla en los dos botones
    # probados (22 y 23 de 36) y cuesta 16 KB más.
    FILAS_FRANJA = 3
    # El recorte se amplía antes de preguntar, mientras no se pase de tamaño:
    # lo que crece con la imagen son los tokens de cada mirada.
    AMPLIAR = 2
    LADO_MAX = 1800

    @staticmethod
    def franja(y: float, alto: float = ALTO_FRANJA) -> Zona:
        """La banda de pantalla centrada en esa altura, de lado a lado."""
        alto = max(0.02, min(1.0, alto))
        return Zona(0.0, max(0.0, min(1.0 - alto, y - alto / 2)), 1.0, alto)

    @staticmethod
    def zona_de(n: int, cols: int, filas: int = 1,
                dentro: Optional[Zona] = None) -> Optional[Zona]:
        """La casilla nº `n` (1..cols*filas), en fracciones de la PANTALLA ENTERA.

        Lo de «pantalla entera» es la parte que se rompe sola: las casillas se
        dibujan sobre un recorte, pero el dedo se pone sobre la pantalla. Todo
        lo que sale de aquí ya viene traducido, como los sitios del `Ojo`.
        """
        if cols < 1 or filas < 1 or not (1 <= n <= cols * filas):
            return None
        fila, col = divmod(n - 1, cols)
        base = dentro or Zona(0.0, 0.0, 1.0, 1.0)
        an, al = base.ancho / cols, base.alto / filas
        return Zona(base.x + col * an, base.y + fila * al, an, al)

    @staticmethod
    def numerar(png: bytes, cols: int, filas: int = 1,
                dentro: Optional[Zona] = None) -> Optional[bytes]:
        """La pantalla —o un trozo, ampliado— con cada casilla numerada encima."""
        try:
            import io
            from PIL import Image, ImageDraw, ImageFont
            im = Image.open(io.BytesIO(png)).convert("RGB")
            if dentro is not None:
                x, y, an, al = dentro.pixeles(im.width, im.height)
                im = im.crop((x, y, min(im.width, x + an), min(im.height, y + al)))
                if max(im.width, im.height) * Rejilla.AMPLIAR <= Rejilla.LADO_MAX:
                    im = im.resize((im.width * Rejilla.AMPLIAR,
                                    im.height * Rejilla.AMPLIAR), Image.LANCZOS)
            d = ImageDraw.Draw(im)
            an_c, al_c = im.width / cols, im.height / filas
            tam = max(18, min(64, int(min(an_c, al_c) / 2.2)))
            try:
                f = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", tam)
            except OSError:
                f = None
            # El mismo rosa del `Marcador`: un color que no sale en un juego,
            # para que no confunda la marca con algo de la pantalla.
            for c in range(1, cols):
                d.line([(c * an_c, 0), (c * an_c, im.height)], fill="#ff0066", width=4)
            for r in range(1, filas):
                d.line([(0, r * al_c), (im.width, r * al_c)], fill="#ff0066", width=4)
            for n in range(1, cols * filas + 1):
                fila, col = divmod(n - 1, cols)
                x, y = col * an_c, fila * al_c
                # El número tapa una esquina de su casilla: es el precio de que
                # se vea. Arriba a la izquierda, porque los rótulos de botón
                # suelen ir centrados.
                d.rectangle([x, y, x + tam * (1.3 if n < 10 else 1.9),
                             y + tam * 1.35], fill="#ff0066")
                d.text((x + tam * 0.25, y + tam * 0.05), str(n), font=f, fill="#ffffff")
            buf = io.BytesIO()
            # JPEG y no PNG: la misma franja son 61 KB aquí y 400 en PNG, y
            # esto sube por el WiFi del móvil en cada jugada. Lo que se le pide
            # al modelo es elegir una casilla, no leer letra pequeña.
            im.save(buf, format="JPEG", quality=75)
            return buf.getvalue()
        except Exception as e:
            logger.warning("no pude numerar la pantalla por casillas: %s", e)
            return None


# El prompt que va con las marcas. Se le pide un NÚMERO, que es lo que sabe
# dar; las coordenadas las ponemos nosotros, que para eso hemos dibujado las
# cajas. Si ninguna le sirve, que lo diga en vez de elegir la menos mala.
# Cómo se juega, en general. No es un manual de ZZZ ni una lista de personajes
# —el meta caduca con cada parche y de memoria se inventa (ver `zzz.py`)—: son
# las cuatro cosas que se ven EN LA PANTALLA y valen en cualquier juego de
# acción de móvil. Van en el prompt porque sin ellas el modelo se comporta como
# quien mira un menú: da un toque y espera a ver qué pasa.
# Lo que pone la pantalla cuando el juego NO se puede jugar, y da igual lo
# bien que se toque. Enzo, 8 sep 2026: «el juego está en mantenimiento» —y la
# partida anterior se pasó 100 s tocando ese cartel, gastó cuota de cuatro
# proveedores, batería del móvil, y acabó diciendo «la pantalla lleva rato sin
# cambiar», que es verdad pero no es LA verdad.
#
# Va en dos idiomas porque el juego se pone en el del móvil, y con los códigos
# de error de red porque un servidor caído da eso mismo antes que una frase.
#
# «maintenance» a secas NO vale, y eso lo enseñó una prueba: en ZZZ hay un
# enemigo llamado **Maintenance Crew**. Un falso positivo aquí no es un aviso
# de más, es una partida buena cortada en seco, así que se exige el contexto
# que sólo aparece en el cartel de verdad.
_BLOQUEADA_RE = re.compile(
    r"(?i)\b(under\s+maintenance|maintenance\s+(?:notice|in\s+progress|ongoing)|"
    r"server\s+maintenance|en\s+mantenimiento|mantenimiento\s+programado|"
    r"servers?\s+(?:are\s+)?(?:down|offline|unavailable)|"
    r"servidor(?:es)?\s+(?:no\s+disponible|ca[ií]d)|"
    r"temporarily\s+unavailable|temporalmente\s+no\s+disponible|"
    r"try\s+again\s+later|vuelve\s+m[aá]s\s+tarde|int[eé]ntalo\s+m[aá]s\s+tarde|"
    r"connection\s+(?:failed|error)|error\s+de\s+conexi[oó]n)\b")


# El juego bajando su actualización. Enzo, 9 sep 2026: «va a tener que
# descargar los datos de la nueva versión y tendrá que descargarlos y aceptar
# que se descarguen los datos y esperar a que descargue todo».
#
# Es la pareja de `_BLOQUEADA_RE` y su contrario exacto: ante un juego caído se
# PARA, ante una descarga se ESPERA. Confundirlas sale caro en las dos
# direcciones — cortar una partida que sólo estaba cargando, o pasarse veinte
# minutos tocando un cartel muerto.
#
# El porcentaje y los MB/GB entran en la lista porque una barra de progreso a
# veces es lo único que se lee: sin rótulo, pero avanzando.
_DESCARGANDO_RE = re.compile(
    r"(?i)(descarg[aá]ndo|descargando|downloading|actualiz[aá]ndo|updating|"
    r"instalando|installing|verificando|verifying|extrayendo|extracting|"
    r"compilando|compiling|sombreado|sombreador|shader|precargando|"
    r"preloading|descomprimiendo|unpacking|optimizando|optimizing|"
    r"\bdescarga\b|\bdownload\b|\bupdate\b|\bactualizaci[oó]n\b|"
    r"\bresource\s+download|paquete\s+de\s+recursos|resource\s+package|"
    r"\d+\s*%|\d+[.,]?\d*\s*(?:MB|GB|KB)\s*/|\d+[.,]?\d*\s*(?:MB|GB)\b)")


COMO_SE_JUEGA = """Cómo se juega, si estás dentro de una pelea o de una fase:
- El botón grande de abajo a la derecha es atacar. Se MACHACA: tres, cuatro,
  cinco toques seguidos, no uno.
- Los botones pequeños alrededor son habilidades; se usan cuando están
  encendidos (los apagados no hacen nada).
- Los retratos de personaje que se iluminan se pulsan: es el relevo, y es
  gratis.
- Para moverte, desliza desde la zona de abajo a la izquierda.
- Si sale un diálogo, texto o una recompensa, tócalo para pasarlo y sigue.
Si estás peleando, usa "pelear": en "n" el recuadro del botón de atacar, en
"vigilar" los recuadros que hay que pulsar en cuanto se enciendan (habilidades,
relevos), y en "ms" cuánto durar: para pelear pon entre 10000 y 20000, NO 300.
Con eso el móvil ataca solo y reacciona 15 veces por segundo, sin esperar a que
yo mire otra vez — que es lo que ningún golpe rápido puede permitirse.
Si no peleas pero encadenas (menús, diálogos), usa "luego": nadie da un toque y
se queda quieto tres segundos esperando.
"""


# Lo que se le pide DE MÁS en cada mirada, y que es lo que cambia el ritmo de
# la partida. Cuesta lo mismo preguntarlo (la imagen son 1.024 tokens fijos, la
# respuesta 30-80) y devuelve varios movimientos en vez de uno.
PIDE_RAFAGA = """- "luego": los movimientos que harías DESPUÉS de ese, seguidos y sin volver a
  mirar (hasta 8). Un jugador encadena golpes; no da uno y se queda quieto.
  Cada uno: {"accion":"tocar","n":3,"ms":150} — sin "n", repite el sitio del
  anterior, que es como se machaca un botón. "ms" es lo que se espera DESPUÉS,
  y va entre 80 y 300: son golpes seguidos. Si de verdad hay que esperar a que
  algo cargue, no lo pongas aquí — corta la ráfaga y usa "esperar".
  Pon pocos si la pantalla va a cambiar (un menú, una carga); pon muchos si
  estás peleando y sabes lo que toca."""


INSTRUCCION_MARCADA = """Estás viendo la pantalla del móvil de alguien mientras juega.
Le hemos dibujado encima unos recuadros ROSA numerados: son los sitios que se pueden
tocar.

Objetivo: {objetivo}

{historial}
Responde SOLO con un JSON, sin explicar nada:
{{"accion":"tocar|deslizar|escribir|tecla|esperar|pelear|listo","n":0,"n2":0,"texto":"","ms":300,"porque":"3 palabras","nuevo":"","vigilar":[],"luego":[{{"accion":"tocar","n":0,"ms":150}}]}}
- "tocar": en "n" el número del recuadro que hay que pulsar.
- Si lo que hay que tocar NO tiene recuadro, pon "n":0 y describe en "texto" el rótulo.
- "deslizar": del recuadro "n" al "n2".
- "escribir": lo que hay que teclear va en "texto".
- "andar": MOVERSE por el mundo con el joystick. "dx"/"dy" son la dirección de
  -1 a 1 (dy negativo = hacia arriba de la pantalla) y "ms" cuánto rato se
  sostiene, que es lo lejos que llega. El joystick NO se toca: se empuja y se
  mantiene. Un toque suelto da un paso y para.
- "camara": girar la vista. "dx"/"dy" es hacia dónde. Sin girarla, media
  pantalla del mundo —y de lo que hay que recoger— se queda detrás.
- "listo": el objetivo ya está cumplido.
- Si no ves nada útil, "esperar" con ms.
- LEE LO QUE PIDE EL JUEGO. En una misión, la pantalla casi siempre dice qué
  hay que hacer —arriba a un lado, o abajo—: «ve a», «habla con», «derrota a»,
  «recoge». Si lo ves, haz ESO y ponlo en "porque". No te inventes un plan si
  el juego ya te ha dicho el suyo.
- "nuevo": si en esta pantalla hay una mecánica o un modo que NO es un botón
  corriente (una mecánica de combate, un modo de juego, un sistema del juego),
  pon su NOMBRE tal cual aparece. NUNCA el nombre del juego. Si no hay nada
  especial, déjalo vacío.
- Si el juego pide DESCARGAR datos o una actualización, acéptalo: toca el botón
  de confirmar o descargar. Es lo que hay que hacer para poder jugar.
- Si ya está descargando (barra de progreso, un porcentaje, MB o GB), NO toques
  nada: contesta "esperar" con ms alto. Tocar durante una descarga no la acelera.
{rafaga}
No toques nada que cueste dinero."""


# La segunda pregunta: sólo el número de casilla. Ya está decidido QUÉ se
# hace y a qué altura; aquí sólo falta la `x`, que es lo único que el modelo no
# sabe medir. Se le deja decir 0 a propósito: si no ve el botón en la franja,
# vale más que lo diga a que elija una casilla por no dejar la respuesta vacía
# (probado: con un objetivo que no estaba en la franja, contestó 0).
INSTRUCCION_CASILLAS = """Esta es una FRANJA horizontal de la pantalla de un móvil,
partida en {cols} casillas numeradas: se empieza por la de arriba a la izquierda y se
sigue hacia la derecha, fila por fila. El número está en la esquina superior izquierda de
cada casilla.

Hay que tocar: {que}

Responde SOLO con un JSON, sin explicar nada:
{{"n":0}}
- "n": el número de la casilla donde está el CENTRO de eso que hay que tocar.
- Si no lo ves en esta franja, pon "n":0."""


# El prompt del carril con OJOS. Pide adrede lo que el modelo SABE dar y calla
# lo que no: la `x` no se pregunta porque **no la acierta** (sesión 61: 443 px
# de error en un botón de 440 de ancho, o sea el toque cae fuera). La `y` sí
# —8 px— así que se le pide la banda, y la `x` la pone el OCR buscando el texto
# dentro de esa banda. Cada uno hace aquello en lo que se midió que es bueno.
INSTRUCCION_VISION = """Estás viendo la pantalla del móvil de alguien mientras juega.

Objetivo: {objetivo}

{historial}
Responde SOLO con un JSON, sin explicar nada:
{{"accion":"tocar|deslizar|mantener|escribir|tecla|esperar|pelear|listo","texto":"EL TEXTO EXACTO DEL BOTÓN","y":0.5,"y2":0,"ms":300,"porque":"3 palabras","nuevo":"","luego":[{{"accion":"tocar","ms":150}}]}}
- "tocar": en "texto" pon el rótulo del botón TAL CUAL se lee en la pantalla, y
  en "y" a qué altura está (0=arriba del todo, 1=abajo del todo).
- Si lo que hay que tocar no tiene rótulo, deja "texto" vacío y di la "y".
- "deslizar": de "y" a "y2".
- "escribir": lo que hay que teclear va en "texto".
- "andar": MOVERSE por el mundo con el joystick. "dx"/"dy" son la dirección de
  -1 a 1 (dy negativo = hacia arriba de la pantalla) y "ms" cuánto rato se
  sostiene, que es lo lejos que llega. El joystick NO se toca: se empuja y se
  mantiene. Un toque suelto da un paso y para.
- "camara": girar la vista. "dx"/"dy" es hacia dónde. Sin girarla, media
  pantalla del mundo —y de lo que hay que recoger— se queda detrás.
- "listo": el objetivo ya está cumplido.
- Si no ves nada útil, "esperar" con ms.
- LEE LO QUE PIDE EL JUEGO. En una misión, la pantalla casi siempre dice qué
  hay que hacer —arriba a un lado, o abajo—: «ve a», «habla con», «derrota a»,
  «recoge». Si lo ves, haz ESO y ponlo en "porque". No te inventes un plan si
  el juego ya te ha dicho el suyo.
- "nuevo": si en esta pantalla hay una mecánica o un modo que NO es un botón
  corriente (una mecánica de combate, un modo de juego, un sistema del juego),
  pon su NOMBRE tal cual aparece. NUNCA el nombre del juego. Si no hay nada
  especial, déjalo vacío.
- Si el juego pide DESCARGAR datos o una actualización, acéptalo: toca el botón
  de confirmar o descargar. Es lo que hay que hacer para poder jugar.
- Si ya está descargando (barra de progreso, un porcentaje, MB o GB), NO toques
  nada: contesta "esperar" con ms alto. Tocar durante una descarga no la acelera.
{rafaga}
No toques nada que cueste dinero."""


def mirada_groq(clave: str, modelo: str = "qwen/qwen3.8-27b"
                ) -> Callable[[bytes, str], str]:
    """El carril con ojos: la pantalla va como IMAGEN, no como texto de OCR.

    Sesión 61. Durante cinco sesiones estuvo escrito aquí que Groq no servía
    ningún modelo capaz de ver, y era **falso**: `qwen3.8-27b` —el mismo que ya
    decidía las jugadas— ve imágenes. El error fue leer el catálogo y juzgar
    por el nombre (ninguno se llama «vision») en vez de mandar una imagen.

    Lo que cambia, medido en el móvil de Enzo:

        OCR de la pantalla entera ... 14.805 ms
        esta mirada ................    496 ms

    O sea que *entender* la pantalla deja de ser el cuello. Lo que NO puede
    hacer es decir dónde tocar: ver una escena y medirla son cosas distintas
    (los números, en `INSTRUCCION_VISION`). Por eso esto contesta CON QUÉ hay
    que tocar y a qué altura, y quien pone el dedo es el OCR sobre esa franja.
    """
    return lambda png, prompt: _preguntar_a_ojo(
        GROQ_VISION_URL, clave, modelo, png, prompt)[0]


# Los sitios que saben ver, y en qué orden se les pregunta. Los tres hablan
# el mismo dialecto (OpenAI), Gemini incluido: Google sirve una ruta compatible
# y así el cliente es uno solo en vez de tres.
# Cuántas miradas por minuto caben en Groq. Sale de dividir su tope de tokens
# de entrada por minuto (7.000, plan on-demand) entre lo que cuesta una
# pantalla (1.024, medidos) y dejar un hueco para lo demás que Celestia le
# manda. No es un ajuste fino: es aritmética del proveedor.
GROQ_MIRADAS_POR_MINUTO = 5

# Cuánto se espera a que se libere un sitio antes de rendirse y leer con OCR.
# El número sale de comparar las dos cosas: una vuelta de OCR sobre pantalla
# entera son ~15-60 s medidos, así que esperar hasta veinte por una mirada de
# medio segundo sigue saliendo a cuenta. Más allá, no.
ESPERA_MAXIMA_POR_CUOTA = 20.0

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


# Cuánto pide esperar el propio proveedor, si lo dice.
_REINTENTAR_EN_RE = re.compile(r"retry in ([0-9.]+)\s*s", re.I)


def mirada_en_cadena(clave_groq: str, clave_or: str = "", clave_gemini: str = "",
                     clave_mistral: str = "", clave_cerebras: str = "",
                     clave_sambanova: str = "",
                     modelo_groq: str = "qwen/qwen3.8-27b",
                     modelo_gemini: str = "gemini-2.5-flash",
                     modelo_or: str = "google/gemma-4-31b-it:free",
                     modelo_mistral: str = "pixtral-12b-2409",
                     modelo_cerebras: str = "gemma-4-31b",
                     modelo_sambanova: str = "gemma-4-31B-it",
                     castigo_s: float = 90.0) -> Callable[[bytes, str], str]:
    """La vista del jugador, con más de un sitio al que preguntar.

    Hasta la S65 el ojo iba **sólo** a Groq: si Groq decía 429, se quedaba
    ciego y el bucle caía al OCR de pantalla entera, que son ~60 s por vuelta
    en vez de medio segundo. Con el móvil de Enzo delante salieron **cinco 429
    en 98 segundos** —o sea, la partida entera a ciegas—, y eso mientras el
    chat de Celestia tiene nueve proveedores de reserva para lo mismo.

    Medido el 7 sep 2026, con la misma imagen y el mismo prompt:

        Groq  qwen3.8-27b .............. ~0,5 s
        Gemini 2.5 flash ...............  2,0 s   JSON correcto
        OpenRouter minimax-m3:free .....  404 — **retirado ese mismo día**
        OpenRouter gemma-4:free ........  429 — la cuenta free, saturada

    De ahí el orden, y de ahí que sean tres y no dos: el primer intento de
    reserva que escribí (minimax) funcionó al probarlo y estaba muerto una hora
    después. Una cadena de dos eslabones donde el segundo puede desaparecer sin
    avisar no es una cadena.

    El castigo tras un 429 no es adorno: sin él se paga el viaje al saturado en
    CADA vuelta para que vuelva a decir que no.
    """
    # A Gemini hay que decirle que NO piense: es un modelo de razonamiento y
    # los tokens que gasta pensando salen del mismo tope que la respuesta, así
    # que devolvía el JSON a medias. Ver `_preguntar_a_ojo`.
    #
    # El último número de cada fila es **su cuota por minuto**, y es lo que
    # decide de verdad cuántos movimientos por minuto puede dar el jugador:
    # ningún proveedor da para una partida él solo.
    # El orden es el medido, no el de la documentación. Probados los tres de
    # reserva el 9 sep 2026 con la misma imagen y el mismo prompt:
    #
    #     Gemini 2.5 flash ....... 1,8 s   JSON correcto
    #     Mistral pixtral-12b .... contesta, pero {"mensaje":"No se puede ejecutar"}
    #     OpenRouter gemma-4:free  vacío
    #
    # Mistral iba el segundo y **por eso morían las partidas**: en cuanto Groq
    # saturaba, la vuelta se la llevaba él con una respuesta que no es una
    # jugada, y cinco de ésas seguidas cierran la partida con «no supe qué
    # hacer con esta pantalla» — que no era verdad: nadie le había dicho nada.
    intentos = [
        (GROQ_VISION_URL, clave_groq, modelo_groq, 25, {}, GROQ_MIRADAS_POR_MINUTO),
        (CEREBRAS_VISION_URL, clave_cerebras, modelo_cerebras, 30, {}, 20),
        (GEMINI_VISION_URL, clave_gemini, modelo_gemini, 40,
         {"reasoning_effort": "none"}, 18),
        (SAMBANOVA_VISION_URL, clave_sambanova, modelo_sambanova, 40, {}, 15),
        (MISTRAL_VISION_URL, clave_mistral, modelo_mistral, 40, {}, 20),
        (OR_VISION_URL, clave_or, modelo_or, 40, {}, 10),
    ]
    castigado_hasta: Dict[str, float] = {}
    # Cuándo se le preguntó a cada uno, para no pasarse de su cuota por minuto.
    ultimas: Dict[str, List[float]] = {}
    cuota = {url: por_minuto for url, _c, _m, _t, _x, por_minuto in intentos}

    def sitio_libre(url: str, ahora: float) -> bool:
        """¿Le queda cuota a este sitio, o lo mando al de al lado?

        🔴 Esto es lo que el 429 estaba enseñando y no se leyó bien. El límite
        de Groq no es de velocidad: son **7.000 tokens de ENTRADA por minuto**,
        y una pantalla cuesta **1.024 fijos** —da igual mandarla a 1084x2412 o
        encogida a un cuarto: se midieron las dos y las dos gastan 1.024—. O
        sea que el techo de Groq son ~6 miradas por minuto, y la séptima no es
        lenta: es un 429. Esperar a que falle cuesta el viaje entero; contarlas
        aquí lo manda al siguiente sin pagar el error.
        """
        recientes = [t for t in ultimas.get(url, []) if ahora - t < 60.0]
        ultimas[url] = recientes
        return len(recientes) < cuota.get(url, 10)

    def cuando_hay_hueco(ahora: float) -> Optional[float]:
        """Si TODOS están ocupados, ¿cuánto falta para el primero que se libere?

        🔴 Visto en la primera partida con la cuota puesta (S66): con Groq sin
        hueco y los otros dos castigados por un 429, la mirada devolvía vacío
        **sin preguntar a nadie**, y el bucle hacía lo que hace cuando no hay
        vista: OCR de la pantalla entera, ~60 s. O sea que por ahorrarse una
        espera de ocho segundos se pagaban sesenta, y la partida se quedaba
        noventa segundos sin mover un dedo. Esperar a la cuota no es perder
        tiempo: es perder mucho menos.
        """
        cuando = []
        for url, clave, _m, _e, _x, por_minuto in intentos:
            if not clave:
                continue
            libre = castigado_hasta.get(url, 0.0)
            recientes = sorted(t for t in ultimas.get(url, []) if ahora - t < 60.0)
            if len(recientes) >= por_minuto:
                libre = max(libre, recientes[0] + 60.0)
            cuando.append(libre)
        return min(cuando) if cuando else None

    def mirar(png: bytes, prompt: str) -> str:
        ahora = time.time()
        hueco = cuando_hay_hueco(ahora)
        if hueco is not None and hueco > ahora:
            falta = hueco - ahora
            if falta <= ESPERA_MAXIMA_POR_CUOTA:
                logger.info("todos los ojos ocupados: espero %.1f s a que se "
                            "libere uno (el OCR costaría diez veces más)", falta)
                time.sleep(falta + 0.2)
                ahora = time.time()
        for url, clave, modelo, espera, extra, _por_minuto in intentos:
            if not clave or ahora < castigado_hasta.get(url, 0.0):
                continue
            if not sitio_libre(url, ahora):
                continue
            ultimas.setdefault(url, []).append(ahora)
            texto, fallo = _preguntar_a_ojo(url, clave, modelo, png, prompt,
                                            timeout=espera, extra=extra)
            if texto and _hay_jugada(texto):
                return texto
            if texto:
                # Contestó, pero no una jugada. Cuenta como fallo suyo, no del
                # jugador: se le aparta un rato y se prueba al siguiente AHORA,
                # en la misma vuelta, en vez de gastarla.
                castigado_hasta[url] = ahora + min(castigo_s, 60.0)
                logger.warning("«%s» contestó sin jugada (%r): paso al siguiente",
                               modelo, texto[:60])
                continue
            # 429 (saturado) y 404 (el modelo ya no existe) tienen el mismo
            # remedio inmediato —no volver a preguntar ahora mismo—, pero un
            # 404 conviene mirarlo: quiere decir que hay que cambiar el modelo.
            if "429" in fallo:
                # Gemini contesta «Please retry in 13.49s» y se le castigaba
                # **noventa**: seis veces más de lo que pedía, y mientras tanto
                # el bucle leyendo la pantalla con OCR. Si lo dice, se le hace
                # caso; si no, un castigo corto y se prueba otra vez.
                m = _REINTENTAR_EN_RE.search(fallo)
                pausa = float(m.group(1)) + 1.0 if m else castigo_s
                pausa = max(3.0, min(castigo_s, pausa))
                castigado_hasta[url] = ahora + pausa
                logger.info("%s satura la vista: no le pregunto en %.0f s",
                            modelo, pausa)
            elif "402" in fallo:
                # Sin saldo. No es cosa de esperar un minuto: hasta que Enzo
                # ponga dinero, preguntar aquí es tirar el viaje en CADA
                # vuelta. Se aparta un buen rato y se sigue solo cuando pague.
                castigado_hasta[url] = ahora + castigo_s * 10
                logger.info("«%s» pide saldo (402): no le pregunto en %.0f min",
                            modelo, castigo_s * 10 / 60)
            elif "404" in fallo:
                castigado_hasta[url] = ahora + castigo_s * 10
                logger.warning("«%s» ya no existe donde estaba (404): hay que "
                               "cambiar el modelo de reserva", modelo)
            else:
                logger.warning("la mirada a «%s» no salió: %s", modelo, fallo)
        return ""
    return mirar


# Las acciones que el jugador sabe ejecutar. Fuera de esta lista, la respuesta
# no es una jugada por muy bien formada que venga.
_ACCIONES = ("tocar", "deslizar", "mantener", "escribir", "tecla", "esperar",
             "pelear", "listo")


def _hay_jugada(bruto: str) -> bool:
    """¿La respuesta sirve para algo, o es de tirar?

    Hace falta porque «contestar» y «servir» no son lo mismo, y desde dentro se
    parecen: pixtral devuelve `{"mensaje":"No se puede ejecutar"}` —un JSON
    perfecto sin ninguna jugada dentro— y eso se contaba como vuelta gastada.

    ⚠️ Y no todo lo que sirve es una jugada. Al ojo se le pregunta de dos
    maneras: «¿qué hago?» (lleva `accion`) y «¿en qué casilla está el botón?»
    (contesta `{"n": 7}` a secas, y `{"n": 0}` es «no lo veo», que también es
    una respuesta). La primera versión de esto sólo miraba `accion` y se puso a
    tirar respuestas buenas de Gemini —`{"n":0}`— castigando al proveedor que
    sí funcionaba. Visto en la partida de las 12:45.
    """
    d = _json_suelto(bruto or "")
    if not isinstance(d, dict) or not d:
        return False
    if str(d.get("accion", "")).lower().strip() in _ACCIONES:
        return True
    if isinstance(d.get("n"), (int, float)):
        return True
    # 🔴 Y cualquier otro JSON con contenido también sirve. Al ojo se le
    # pregunta de MÁS formas de las que esta función sabía: una jugada
    # («accion»), una casilla («n») y **dónde están los controles**
    # (`{"atacar":0.7,"esquivar":0.78,…}`). Con la lista cerrada, esa tercera
    # se tiraba entera: el 10 sep 2026 Groq contestó perfectamente dónde
    # estaban los botones de combate y el descubridor dijo «no veo controles de
    # combate en esta pantalla», porque yo estaba descartando la respuesta.
    #
    # Es la segunda vez que la lista cerrada tira respuestas buenas (la primera
    # fueron los «{"n":0}» de Gemini). Así que se invierte el criterio: se
    # descarta lo que es un mensaje de error, y lo demás pasa.
    utiles = [k for k in d if k.lower() not in ("error", "mensaje", "message",
                                                "detail", "status")]
    return bool(utiles)


# Lo que el juego manda hacer, tal como lo escribe en la pantalla. Enzo, 10 sep
# 2026: «digamos "habla con Sigrid" o "ve a ver cómo está X" o "dirígete a X
# sitio" … tiene que hacer lo que le pidan en cada misión, en cada evento, en
# cada misión semanal, diaria».
#
# Va por frases y no por palabras sueltas, y con el verbo delante: «habla» a
# secas sale en cualquier menú, «habla con» es una orden. Es la misma regla que
# ya salvó los frenos de mantenimiento y de cuenta (S68).
_MISION_RE = re.compile(
    r"(?i)((?:habla|hablar)\s+con\s+[^.\n]{2,40}"
    r"|(?:ve|ir|acude|dir[ií]gete|dirigirse|vuelve|regresa)\s+(?:a|al|hacia|hasta)\s+[^.\n]{2,40}"
    r"|(?:derrota|elimina|vence|acaba\s+con|limpia)\s+[^.\n]{2,40}"
    r"|(?:recoge|consigue|obt[eé]n|reclama|entrega|investiga|examina|busca)\s+[^.\n]{2,40}"
    r"|(?:talk\s+to|go\s+to|head\s+to|defeat|collect|claim|deliver|investigate)\s+[^.\n]{2,40})")


def _consejo_de_escena(escena: str) -> str:
    """Qué recordarle según dónde esté. Vacío si no se sabe dónde está."""
    try:
        from celestia_lib import escena as _e
        return _e.como_se_juega_aqui(escena)
    except Exception:
        return ""


def _dir(valor: Any) -> float:
    """Una componente de dirección: entre -1 y 1, y 0 si viene cualquier cosa."""
    try:
        return max(-1.0, min(1.0, float(valor or 0)))
    except (TypeError, ValueError):
        return 0.0


def _json_suelto(bruto: str) -> Optional[Dict[str, Any]]:
    """El primer objeto JSON que haya en la respuesta, o `None`.

    Los modelos envuelven el JSON en explicaciones por mucho que se les pida
    que no, así que se busca en vez de exigir que venga limpio.
    """
    m = re.search(r"\{.*\}", bruto or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


class Jugador:
    """Ve, decide y juega — despacio, pero entendiendo lo que hace.

    El reparto de trabajo es el que sale de medir, no de suponer:

      · Con ojos (`mirar`), la pantalla va como IMAGEN al modelo: **496 ms**
        frente a los 14.805 del OCR entero. Él dice QUÉ tocar y a qué altura;
        el OCR pone el dedo, leyendo sólo esa franja (568 ms). Sesión 61.
      · Sin ojos, el camino viejo: OCR de la pantalla entera a texto, y un
        modelo de texto decidiendo (~290 ms con qwen3.8-27b). Es lo que se usa
        si no hay red para la imagen o si la mirada no encuentra nada.
      · Y lo que ya se resolvió una vez no se vuelve a preguntar: el libro de
        jugadas responde en microsegundos.

    O sea que el ciclo caro solo se paga con pantallas nuevas. Cuanto más
    juega, menos piensa — que es exactamente lo que hace un humano.
    """

    def __init__(self, mando: Mando, pensar: Callable[[str], str],
                 ojo: Optional[Ojo] = None, libro: Optional[LibroDeJugadas] = None,
                 crono: Optional[Cronometro] = None,
                 mirar: Optional[Callable[[bytes, str], str]] = None,
                 controles: Optional[Any] = None,
                 saber: str = "", meta: str = "", escuela: Optional[Any] = None):
        # Lo que se le pide CONSEGUIR, cuando es algo más concreto que «juega».
        # Va aparte del objetivo a propósito: el objetivo nombra el juego y es
        # lo que da nombre al libro de jugadas —cambiarlo abriría un cuaderno
        # nuevo en cada partida y lo aprendido no se reutilizaría jamás—,
        # mientras que esto sólo viaja al prompt. Es la misma separación que ya
        # hacía `objetivo_para_pensar`, pero pudiéndola dirigir desde fuera.
        self.meta = meta
        # Lo que ha leído sobre cómo se juega a ESTE juego, en texto y ya
        # resumido (`zzz.SaberZZZ.resumen_para_jugar`). Llega como cadena y no
        # importando el módulo para no atar el jugador a un juego concreto:
        # mañana puede ser otro, y el jugador no tiene por qué enterarse.
        self.saber = saber
        # `controles` es lo que aprendió sola sobre ESTE juego: dónde está el
        # botón de atacar, el de esquivar, los relevos. Cualquier objeto con
        # `.punto(nombre)` vale — se pide así, y no importando el mapa, para no
        # atar `jugador` a `aprendiz` (que ya depende de este módulo).
        self.controles = controles
        # ¿Se lee lo que pide el juego? Cuesta un OCR por pantalla, así que se
        # puede apagar donde no haya misiones (una pelea de entrenamiento).
        self.leer_misiones = True
        # Dónde escribe el juego lo que hay que hacer: la banda de arriba. Va
        # ancha (toda la anchura) y baja (el 22% de alto) porque cada juego la
        # coloca en un lado distinto, pero siempre arriba.
        self.BANDA_DE_MISION = Zona(0.0, 0.0, 1.0, 0.22)
        # (pantalla, lo que pedía) para no releer la misma misión cada vuelta.
        self._mision_vista: Tuple[Any, str] = (None, "")
        # La última pantalla vista y qué clase de sitio era, para medir cuánto
        # se mueve esto solo y no repetir el apunte en cada vuelta.
        self._huella_anterior: Any = None
        self._escena_vista = ""
        # Lo último que se leyó de la pantalla, sea de la banda de la misión o
        # de donde toque. Se comparte para no pagar dos OCR en la misma vuelta.
        self._palabras_vistas: List[Palabra] = []
        # Lo que ha aprendido de ESTE juego, situación por situación
        # (`escuela.Escuela`). Cualquier objeto con `.apunte(huella)`,
        # `.ver(huella, titulo)`, `.estudiar(lec)`, `.acierto(...)` y
        # `.fallo(...)` vale; se pide así, y no importando el módulo, porque
        # `escuela` ya importa de aquí y al revés sería un círculo.
        #
        # La diferencia con `saber`: aquél es el tutorial general del juego,
        # cargado entero antes de empezar; ésta trae lo de UNA pantalla, sólo
        # cuando esa pantalla aparece, y deja de traerlo cuando ya se domina.
        self.escuela = escuela
        # La lección que espera a que se salga a la red por ella. Se apunta
        # dentro de la mirada y se resuelve en el bucle: buscar tarda segundos
        # y no puede pasar en mitad de armar un prompt.
        self._por_estudiar: Optional[Any] = None
        self.mando = mando
        self.pensar = pensar
        # Con ojos se juega por el carril de la sesión 61; sin ellos, por el de
        # siempre. Se guarda como opcional a propósito: si no hay red para
        # mandar la imagen, o si la mirada no encuentra el botón que dijo, el
        # camino viejo sigue entero debajo.
        self.mirar = mirar
        self.ojo = ojo or Ojo()
        self.libro = libro or LibroDeJugadas()
        self.crono = crono or Cronometro()
        self.bitacora: List[str] = []
        # Cuántas veces la vista rápida se quedó muda y hubo que leer la
        # pantalla con OCR. No es un detalle interno: son 0,5 s frente a 61 s
        # por vuelta (medido en vivo, S65, con Groq devolviendo 429), o sea la
        # diferencia entre jugar y no jugar. Si no se cuenta, el parte dice
        # «jugué 2 jugadas en 83 s» y parece que el juego es lento.
        self.sin_vista_rapida = 0
        # El paquete del juego, en cuanto se sepa. Lo usa `_sigue_siendo_el_juego`.
        self._pkg_juego = ""
        # Para qué objetivo se resolvió ese paquete: reutilizarlo sólo vale si
        # se sigue jugando a lo mismo.
        self._objetivo_del_pkg = ""

    # ── decidir ─────────────────────────────────────────────────────────
    def _describir(self, palabras: Sequence[Palabra]) -> str:
        if not palabras:
            return "(no consigo leer nada en la pantalla)"
        return "\n".join(f"- «{p.texto}» en ({p.centro.x:.2f}, {p.centro.y:.2f})"
                         for p in palabras[:40])

    def _mirar_y_decidir(self, objetivo: str, cap: Captura,
                         huella: Optional[Sequence] = None,
                         registrar: bool = True
                         ) -> Optional[Tuple[Accion, List[Palabra]]]:
        """El carril con ojos: la imagen al modelo, el dedo lo pone el OCR.

        Devuelve `(accion, palabras)` o `None` si no sale — y `None` significa
        «que lo resuelva el camino de siempre», nunca «no se puede jugar».
        Las `palabras` que salen son las de la franja mirada, y son justo las
        que hacen falta para el freno del dinero: mira lo que está a la misma
        altura del punto (±5%), que es lo que esa franja contiene.
        """
        if not self.mirar or not cap.png:
            return None
        hist = ""
        if self.bitacora:
            hist = ("Lo que ya he hecho (no lo repitas si no cambió nada):\n" +
                    "\n".join(f"- {h}" for h in self.bitacora[-4:]) + "\n\n")
        # Primero, el camino bueno: numerarle los sitios tocables y que elija.
        # Le sale a 2 px frente a los 443 de pedirle coordenadas, y se salta el
        # OCR entero, que era el cuello. Sólo se intenta si el fondo era lo
        # bastante liso como para encontrar unos pocos candidatos.
        cajas: List[Zona] = []
        marcada: Optional[bytes] = None
        with self.crono.medir("marcar"):
            cajas = Marcador.candidatos(cap.png)
            if Marcador.MIN_CAJAS <= len(cajas) <= Marcador.MAX_CAJAS:
                marcada = Marcador.marcar(cap.png, cajas)
        # Lo leído va DESPUÉS de lo general y antes de la ráfaga: lo general
        # dice cómo se juega a cualquier juego de acción, y esto de aquí dice
        # cómo se juega a este —con qué señal se esquiva, cuándo entra la
        # cadena—, que es lo que no se puede deducir mirando una captura.
        aprendido = (self.saber + "\n") if self.saber else ""
        # Y lo que sepa de ESTA pantalla en concreto, que es lo que el saber
        # general no puede traer. Sale vacío cuando la situación ya está
        # dominada: a partir de ahí se juega sin que nadie recuerde nada.
        # Lo que el juego manda, si lo tiene escrito. Va lo PRIMERO de todo
        # porque manda sobre cualquier plan: si en pantalla pone «habla con
        # Sigrid», eso es lo que hay que hacer, no lo que parezca mejor.
        pedido = ""
        if self.leer_misiones:
            try:
                # La misma pantalla pide lo mismo: se lee una vez por pantalla
                # y no una por vuelta. En una misión larga eso son decenas de
                # lecturas ahorradas.
                if huella is not None and self._mision_vista[0] == huella:
                    orden = self._mision_vista[1]
                else:
                    orden = self._lo_que_pide_el_juego(cap)
                    self._mision_vista = (huella, orden)
                if orden:
                    pedido = f"EL JUEGO TE PIDE: {orden}. Haz eso.\n"
                    if not self.bitacora or orden not in self.bitacora[-1]:
                        self.bitacora.append(f"el juego pide: {orden}")
            except Exception as e:
                logger.debug("no pude mirar la misión (%s)", e)

        # Dónde está: menú, mundo, combate o diálogo. Sale de cosas ya
        # calculadas —los recuadros, las palabras que se leyeron para la
        # misión, cuánto se movió la pantalla— así que no cuesta ni una mirada
        # ni un OCR de más.
        escena = ""
        try:
            from celestia_lib import escena as _escena
            escena = _escena.deducir(
                cajas, self._palabras_vistas,
                movimiento=self._cuanto_se_movio(huella),
                controles_visibles=bool(self._control("atacar")))
            if escena and escena != self._escena_vista:
                self._escena_vista = escena
                self.bitacora.append(f"estoy en: {escena}")
        except Exception as e:
            logger.debug("no pude saber qué clase de pantalla es (%s)", e)

        de_la_escuela = ""
        if self.escuela is not None:
            try:
                apunte = self.escuela.apunte(huella)
                if apunte:
                    de_la_escuela = apunte + "\n"
            except Exception as e:
                logger.warning("la escuela no pudo apuntar nada (%s)", e)
        prompt = (INSTRUCCION_MARCADA if marcada else INSTRUCCION_VISION).format(
            objetivo=objetivo, historial=hist,
            rafaga=pedido + _consejo_de_escena(escena) + COMO_SE_JUEGA +
                   aprendido + de_la_escuela + PIDE_RAFAGA)
        with self.crono.medir("mirar"):
            bruto = self.mirar(marcada or cap.png, prompt) or ""
        m = re.search(r"\{.*\}", bruto, re.S)
        if not m:
            logger.warning("la mirada no devolvió JSON: %r", bruto[:120])
            if not bruto.strip():
                # Vacío es que no contestó (429, red, timeout). Que conteste
                # mal es otra cosa y no se cuenta aquí.
                self.sin_vista_rapida += 1
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        # Lo que ha visto y no es un botón corriente. Se apunta SIEMPRE, diga
        # o no diga algo: sin nombre, la visita cuenta igual, y son las
        # visitas sin resolver las que acaban disparando la búsqueda por
        # cuenta propia. Así esto sigue funcionando con un modelo que jamás
        # rellene el campo. [[feedback_funcionar_cualquier_modelo]]
        if self.escuela is not None and registrar:
            try:
                lec = self.escuela.ver(huella, str(d.get("nuevo", "") or "")[:80])
                # Buscar tarda segundos: aquí sólo se marca, y el bucle decide
                # cuándo se paga esa espera.
                if lec is not None and not lec.como and (
                        lec.nivel == "nueva" or lec.atascada):
                    self._por_estudiar = lec
            except Exception as e:
                logger.warning("la escuela no pudo apuntar la pantalla (%s)", e)
        tipo = str(d.get("accion", "")).lower().strip()
        if tipo == "listo":
            raise Parada("objetivo cumplido")
        if tipo in ("andar", "camara"):
            # Direcciones, no sitios: van de -1 a 1 y no se recortan a la
            # pantalla como una coordenada. Enzo, 10 sep 2026: «no sabe ni usar
            # el joystick, y cuando lo usa lo pulsa como si fuese botón».
            # Andar no lee rótulos: sin palabras, como esperar o deslizar. Antes
            # devolvía `palabras`, que aquí aún no existe, y el joystick
            # reventaba con NameError cada vez (lo cazó test_estatico_imports,
            # sesión 74).
            return (Accion.hacia(tipo, _dir(d.get("dx")), _dir(d.get("dy")),
                                 max(80, min(4000, int(d.get("ms") or 800))),
                                 porque=str(d.get("porque") or "")[:60]),
                    [])
        if tipo not in ("tocar", "deslizar", "mantener", "escribir", "tecla",
                        "esperar", "pelear"):
            return None

        def _f(k: str) -> float:
            try:
                return max(0.0, min(1.0, float(d.get(k) or 0)))
            except (TypeError, ValueError):
                return 0.0

        texto = str(d.get("texto") or "")[:200]
        porque = str(d.get("porque") or "")[:60]
        ms = int(d.get("ms") or 300)
        if tipo in ("esperar", "tecla", "escribir"):
            return (Accion(tipo=tipo, punto=None, hasta=None, ms=ms,
                           texto=texto, porque=porque), [])
        if tipo == "deslizar":
            def _caja(clave: str) -> Optional[Punto]:
                try:
                    i = int(d.get(clave) or 0)
                except (TypeError, ValueError):
                    return None
                return cajas[i - 1].centro() if cajas and 1 <= i <= len(cajas) else None
            desde, hasta = _caja("n"), _caja("n2")
            if desde and hasta:
                return (Accion(tipo=tipo, punto=desde, hasta=hasta, ms=ms,
                               texto=texto, porque=porque), [])
            # Sin recuadros, la `x` no se le pregunta porque no la acierta: el
            # deslizamiento va por el centro. Cubre el caso real —listas y
            # menús se recorren en vertical— y no finge una precisión que no
            # hay; un swipe horizontal concreto se queda para el carril viejo.
            return (Accion(tipo=tipo, punto=Punto(0.5, _f("y")),
                           hasta=Punto(0.5, _f("y2")), ms=ms,
                           texto=texto, porque=porque), [])

        # Si eligió un recuadro, el punto ya está: es SU centro, medido por
        # nosotros. `n=0` es su forma de decir «lo que hay que tocar no tiene
        # recuadro», y entonces se sigue por el rótulo.
        n = 0
        try:
            n = int(d.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        # «Pelear» no es un movimiento: es cederle el mando al móvil unos
        # segundos. Mientras dura, él mira su propia pantalla 15 veces por
        # segundo y responde en ~112 ms — algo que por la red no se puede
        # hacer, porque la vuelta más corta a un proveedor es medio segundo y
        # una esquiva dura trescientos milisegundos. Ver `Vigia`.
        if tipo == "pelear":
            punto = None
            # Las palabras leídas sólo existen si hace falta buscar el botón
            # por su rótulo. Cuando eligió recuadro no se lee nada, y aun así
            # se devuelven al final: sin esta línea, el caso bueno —el rápido,
            # el que sí sabe dónde tocar— era el único que reventaba. S68.
            palabras: List[Palabra] = []
            if cajas and 1 <= n <= len(cajas):
                punto = cajas[n - 1].centro()
            else:
                punto, palabras = self._localizar(
                    cap, texto or "botón de atacar", _f("y"),
                    sin_fondo=marcada is None)
            # Lo que ya aprendió manda sobre lo que crea ver ahora: el mapa
            # de controles se comprobó **tocando** (la pantalla reaccionó), y
            # esto de aquí arriba es una opinión de una sola mirada.
            aprendido = self._control("atacar")
            if aprendido is not None:
                punto = aprendido
            if punto is None:
                return None
            vigilados: List[Tuple[Punto, Punto]] = []
            # Los que ya sabe dónde están entran solos: son los que avisan
            # —un relevo que se enciende, una habilidad que carga— y no hace
            # falta que el modelo se acuerde de pedirlos en cada pelea.
            for nombre in ("esquivar", "habilidad", "relevo"):
                p = self._control(nombre)
                if p is not None:
                    vigilados.append((p, p))
            for v in (d.get("vigilar") or [])[:3]:
                try:
                    k = int(v)
                except (TypeError, ValueError):
                    continue
                if cajas and 1 <= k <= len(cajas):
                    # Se mira el propio botón y se toca el propio botón: lo que
                    # se vigila es que se ENCIENDA (un relevo listo, una
                    # habilidad cargada), y entonces se pulsa.
                    vigilados.append((cajas[k - 1].centro(), cajas[k - 1].centro()))
            # Por defecto pelea diez segundos: lo bastante para una pelea corta
            # y lo bastante poco para no jugar a ciegas media partida.
            #
            # 🔴 Y hay un SUELO, no sólo un valor por defecto. El `or 10000`
            # sólo salvaba del cero, y el problema no era el cero: la
            # plantilla del prompt lleva «"ms":300» en su ejemplo, el modelo lo
            # copia tal cual, y `300 or 10000` da 300. Pelear 300 ms es no
            # pelear. En la prueba del 9 sep salieron ocho vueltas seguidas de
            # «peleo 0 s seguidos» en entrenamiento libre: entró donde tenía
            # que entrar y no dio un solo golpe.
            #
            # Los otros movimientos sí quieren 300 ms —un toque es un toque—,
            # así que el suelo va sólo aquí, donde la acción es sostenida por
            # definición.
            segundos_ms = max(int(d.get("ms") or 0), MINIMO_PELEA_MS) or 10000
            return (Accion(tipo="pelear", punto=punto, ms=segundos_ms,
                           texto=texto, porque=porque, vigilar=vigilados),
                    palabras)

        if cajas and 1 <= n <= len(cajas):
            punto = cajas[n - 1].centro()
            return (Accion(tipo=tipo, punto=punto, hasta=None,
                           ms=ms, texto=texto, porque=porque,
                           pasos=self._pasos_de(d, cajas, punto)), [])

        # `marcada is None` es la señal de que no había fondo liso, o sea de que
        # esto es un juego y no un menú: es lo que decide si la `x` la pone el
        # OCR (exacto donde se puede leer) o las casillas (donde no).
        punto, palabras = self._localizar(cap, texto, _f("y"),
                                          sin_fondo=marcada is None)
        if punto is None:
            return None
        return (Accion(tipo=tipo, punto=punto, hasta=None, ms=ms,
                       texto=texto, porque=porque,
                       pasos=self._pasos_de(d, cajas, punto)), palabras)

    # Cuántos movimientos se aceptan detrás de uno. El tope lo pone el riesgo,
    # no la velocidad: una ráfaga se juega sin mirar, así que cuanto más larga,
    # más lejos puede acabar la pantalla de lo que el modelo vio cuando decidió.
    MAX_PASOS = 8

    # Y el tope que de verdad importa: cuánto tiempo seguido se juega sin mirar.
    # Ocho pasos pueden ser medio segundo o cuarenta, según lo que el modelo
    # pida esperar entre ellos; contar movimientos no acota nada, contar
    # segundos sí. Seis segundos es lo que tarda una pantalla de juego en
    # cambiar de estado sin que nadie la toque.
    MAX_CIEGAS_S = 6.0

    # Y cuánto puede durar una pausa DENTRO de la ráfaga. Una ráfaga es
    # encadenar; si hace falta esperar de verdad —una carga, una animación—,
    # lo suyo es cortar, mirar y decidir otra vez, no quedarse a ciegas.
    MAX_MS_ENTRE_PASOS = 600

    def _pasos_de(self, d: Dict[str, Any], cajas: Sequence[Zona],
                  base: Optional[Punto]) -> List[Accion]:
        """Los movimientos que el modelo puso en «luego», ya con su sitio.

        El sitio de un paso sale de dos únicas fuentes, las dos exactas y sin
        preguntar nada más: el recuadro `n` —que lo dibujamos nosotros— o el
        punto del movimiento anterior. Nada de rótulos ni alturas: cada rótulo
        costaría un OCR, y un OCR por paso devolvería la ráfaga justo al ritmo
        del que se quiere salir.
        """
        crudos = d.get("luego")
        if not isinstance(crudos, list):
            return []
        pasos: List[Accion] = []
        ultimo = base
        ciegas = 0.0
        for c in crudos[:self.MAX_PASOS]:
            if not isinstance(c, dict):
                continue
            tipo = str(c.get("accion") or "tocar").lower().strip()
            if tipo not in ("tocar", "mantener", "esperar"):
                continue
            try:
                ms = max(0, min(5000, int(c.get("ms") or 150)))
            except (TypeError, ValueError):
                ms = 150
            # 🔴 Lo que hacía que una ráfaga de dos golpes tardara **10,6
            # segundos** (visto en la partida): el modelo mete esperas de
            # 2-5 s entre paso y paso. Eso no es encadenar golpes, es esperar
            # con otro nombre — y para esperar ya está la acción «esperar»,
            # que además vuelve a mirar la pantalla después. Dentro de una
            # ráfaga, entre golpe y golpe, hay un tope corto.
            ms = min(ms, self.MAX_MS_ENTRE_PASOS)
            ciegas += ms / 1000.0
            if ciegas > self.MAX_CIEGAS_S:
                break
            if tipo == "esperar":
                pasos.append(Accion(tipo="esperar", ms=ms))
                continue
            punto = ultimo
            try:
                n = int(c.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
            if cajas and 1 <= n <= len(cajas):
                punto = cajas[n - 1].centro()
            if punto is None:
                continue
            pasos.append(Accion(tipo=tipo, punto=punto, ms=ms))
            ultimo = punto
        return pasos

    def _control(self, nombre: str) -> Optional[Punto]:
        """Dónde está ese control, si lo aprendió **y lo comprobó**.

        Sin comprobar no se usa: un control que el modelo situó pero que al
        tocarlo no hizo nada es peor que no tener ninguno, porque el vigía se
        pasaría la pelea machacando un trozo de pantalla vacío.
        """
        mapa = self.controles
        if mapa is None:
            return None
        try:
            if nombre not in mapa.comprobados():
                return None
            return mapa.punto(nombre)
        except Exception:
            return None

    def _punto_por_casillas(self, cap: Captura, texto: str,
                            y: float) -> Optional[Punto]:
        """Dónde tocar, sin OCR: se numera la franja de esa altura y elige.

        Es lo que sustituye al OCR **dentro de un juego**, donde el OCR no sólo
        tarda 6.701 ms sino que se equivoca de botón (leyó «reclamar» dentro de
        «Sin reclamar»). Aquí no hay nada que detectar ni que leer: se parte la
        franja en casillas y se pregunta en cuál está.

        Devuelve el punto, o `None` si el modelo dice que ahí no está —contesta
        0, y eso es una respuesta útil, no un fallo— o si no hay forma de
        preguntar. `None` significa «que lo resuelva el OCR».
        """
        if not self.mirar or not cap.png:
            return None
        franja = Rejilla.franja(y)
        filas = Rejilla.FILAS_FRANJA
        with self.crono.medir("casillas"):
            img = Rejilla.numerar(cap.png, Rejilla.CASILLAS, filas, dentro=franja)
        if not img:
            return None
        # Etiqueta propia y no «mirar»: `ciclo_ms()` suma medias, así que dos
        # miradas bajo la misma etiqueta se contarían como una y el veredicto
        # saldría medio segundo más optimista de lo que es.
        with self.crono.medir("afinar"):
            bruto = self.mirar(img, INSTRUCCION_CASILLAS.format(
                que=texto, cols=Rejilla.CASILLAS * filas)) or ""
        d = _json_suelto(bruto)
        if d is None:
            logger.warning("la pregunta por casillas no devolvió JSON: %r", bruto[:120])
            return None
        try:
            z = Rejilla.zona_de(int(d.get("n") or 0), Rejilla.CASILLAS, filas,
                                dentro=franja)
        except (TypeError, ValueError):
            return None
        if z is None:
            logger.info("no ve «%s» en la franja de y=%.2f", texto[:40], y)
            return None
        return z.centro()

    def _localizar(self, cap: Captura, texto: str, y: float,
                   sin_fondo: bool = False) -> Tuple[Optional[Punto], List[Palabra]]:
        """Dónde está exactamente lo que hay que tocar.

        La mirada acierta la altura (8 px de error medidos) y falla el ancho
        (hasta 443 px, o sea fuera del botón). Quién pone la `x` depende de
        cómo sea la pantalla, y eso lo dice `sin_fondo`:

          · **En un menú** (hubo recuadros, o sea fondo liso) la pone el OCR
            buscando ESE texto en la franja de esa altura. Da la caja exacta,
            que es mejor que cualquier casilla. Medido en el móvil:

                franja del 10% ...    568 ms
                franja del 20% ...  2.719 ms
                pantalla entera ... 14.805 ms

            Se prueba estrecho primero y se va abriendo: si el rótulo no
            aparece donde dijo, casi suele estar cerca. La última vuelta es la
            pantalla entera — más lenta que el camino viejo no puede salir.
          · **En un juego** (`sin_fondo`) el OCR no vale: tarda 6.701 ms y
            encima acierta el botón de al lado. Ahí la pone `Rejilla`
            numerando la franja, y el OCR se queda debajo por si acaso.
        """
        if not texto:
            # Sin rótulo no hay nada que buscar; queda la altura, que es el
            # dato bueno, y el centro a lo ancho. Es una suposición y se dice.
            logger.info("la mirada no dio rótulo: toco en el centro de y=%.2f", y)
            return (Punto(0.5, y), [])
        if sin_fondo:
            # Aquí la pantalla es arte de juego (el `Marcador` no encontró un
            # solo sitio), y ahí el OCR no es que sea lento: es que acierta el
            # botón de al lado. Las casillas no leen nada, así que les da igual
            # el arte. Si el modelo dice que no lo ve, sigue el OCR debajo.
            punto = self._punto_por_casillas(cap, texto, y)
            if punto is not None:
                return (punto, [])
        palabras: List[Palabra] = []
        for alto in (0.12, 0.30):
            # La misma cuenta que usan las casillas, para que las dos vías
            # miren exactamente la misma banda de pantalla.
            zona = Rejilla.franja(y, alto)
            with self.crono.medir("leer"):
                palabras = self.ojo.leer(cap, zona=zona)
            hallada = Ojo.donde_dice(palabras, texto)
            if hallada:
                return (hallada.centro, palabras)
        # Antes de pagar la pantalla entera —14.805 ms, el cuello de todo—,
        # preguntar por casillas: cuesta ~2 s y NO depende de que el rótulo
        # exista como texto. Porque el caso que trae aquí no siempre es «no lo
        # encuentro»: en la S65, jugando de verdad, la mirada contestó «botón X
        # del diálogo» y «botón X cerrar» —descripciones de un icono, no algo
        # que se pueda leer en pantalla—, y ahí abrir la franja no arregla
        # nada, sólo cuesta. Con `sin_fondo` ya se probó arriba.
        if not sin_fondo:
            punto = self._punto_por_casillas(cap, texto, y)
            if punto is not None:
                return (punto, palabras)
        with self.crono.medir("leer"):
            palabras = self.ojo.leer(cap, zona=None)
        hallada = Ojo.donde_dice(palabras, texto)
        if hallada:
            return (hallada.centro, palabras)
        logger.info("la mirada dijo «%s» y el OCR no lo encuentra en la pantalla",
                    texto[:40])
        return (None, palabras)

    def _preguntar(self, objetivo: str, palabras: Sequence[Palabra]) -> Optional[Accion]:
        hist = ""
        if self.bitacora:
            hist = ("Lo que ya he hecho (no lo repitas si no cambió nada):\n" +
                    "\n".join(f"- {h}" for h in self.bitacora[-4:]) + "\n\n")
        prompt = INSTRUCCION.format(objetivo=objetivo,
                                    pantalla=self._describir(palabras), historial=hist)
        with self.crono.medir("pensar"):
            bruto = self.pensar(prompt) or ""
        m = re.search(r"\{.*\}", bruto, re.S)
        if not m:
            logger.warning("El modelo no devolvió JSON: %r", bruto[:120])
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        tipo = str(d.get("accion", "")).lower().strip()
        if tipo == "listo":
            raise Parada("objetivo cumplido")
        if tipo not in ("tocar", "deslizar", "mantener", "escribir", "tecla", "esperar"):
            return None

        def _f(k: str) -> float:
            try:
                return max(0.0, min(1.0, float(d.get(k) or 0)))
            except (TypeError, ValueError):
                return 0.0
        return Accion(
            tipo=tipo,
            punto=Punto(_f("x"), _f("y")) if tipo != "esperar" else None,
            hasta=Punto(_f("x2"), _f("y2")) if tipo == "deslizar" else None,
            ms=int(d.get("ms") or 300), texto=str(d.get("texto") or "")[:200],
            porque=str(d.get("porque") or "")[:60],
        )

    # ── el freno del dinero ─────────────────────────────────────────────
    def _cuesta_dinero(self, accion: Accion, palabras: Sequence[Palabra],
                       limites: Limites) -> Optional[str]:
        """¿Lo que voy a tocar es un botón de pagar?

        Se mira el texto que hay JUNTO al punto, no toda la pantalla: en un
        juego casi siempre hay una tienda a la vista, y bloquear la partida
        entera por eso sería inútil. Lo que no se toca es el botón concreto.
        """
        if accion.tipo not in ("tocar", "mantener") or not accion.punto:
            return None
        # El rótulo que dijo el modelo cuenta como texto de la pantalla. En el
        # carril de la imagen (recuadros o celdas) no hay OCR, así que
        # `palabras` viene VACÍA y el bucle de abajo no frena nada — o sea que
        # justo el camino que se usa dentro de un juego, donde están las
        # tiendas, sería el único sin freno. No sustituye al OCR (si el modelo
        # no dice el rótulo, aquí no hay nada que mirar), pero es gratis y
        # cubre el caso que importa: el botón grande que pone «Comprar».
        if accion.texto and limites.texto_peligroso(accion.texto):
            return accion.texto
        for p in palabras:
            cerca = (abs(p.centro.x - accion.punto.x) < 0.18 and
                     abs(p.centro.y - accion.punto.y) < 0.05)
            if cerca and limites.texto_peligroso(p.texto):
                return p.texto
        return None

    # ── el bucle ────────────────────────────────────────────────────────
    # Cuánto se le da a un juego para aparecer en pantalla. ZZZ tarda decenas
    # de segundos en cargar; con 10 s se daría por fallido justo cuando iba a
    # salir. Si al final no aparece, se dice — no se juega a ciegas igualmente.
    # ZZZ recién actualizado no está delante en 90 s: pasa por el SDK de
    # HoYoverse y luego se pone a compilar sombreados. Enzo, 9 sep 2026: «no
    # está peleando porque esto está compilando el sombreado».
    ESPERA_APP_S = 210.0

    # Cuánto se espera a que el juego, que SIGUE ABIERTO, vuelva a quedarse
    # solo delante. Pasa cuando Enzo mira la terminal mientras ella juega.
    ESPERA_VUELVA_S = 180.0

    # Cuántas jugadas atrás se mira para notar un bucle, y cuántas veces hay
    # que haber hecho lo mismo en el mismo sitio para llamarlo bucle. Enzo,
    # 9 sep 2026: «a veces toca cosas que nada que ver y se bugea y se queda
    # en bucle». El freno de «la pantalla no cambia» no lo caza: en un bucle
    # A→B→A→B la pantalla SÍ cambia, sólo que vuelve.
    MEMORIA_BUCLE = 8
    REPES_PARA_BUCLE = 2
    # Cuántos bucles seguidos se cortan antes de dejarlo. Va aparte de los
    # tropiezos a propósito: «doy vueltas» y «no entiendo la pantalla» son dos
    # cosas distintas, y mezclarlas cierra partidas por la causa equivocada.
    BUCLES_PARA_RENDIRSE = 6

    # Cuando NADIE contesta —los cuatro proveedores saturados—, esperar 1,5 s y
    # volver a preguntar no arregla nada: la cuota se repone por minuto, no por
    # segundo. Así murió la partida de las 11:52 del 9 de septiembre, con cinco
    # vueltas mudas seguidas y el juego perfectamente delante. Se espera de
    # verdad, y creciendo.
    ESPERA_CUOTA_S = 12.0
    MUDOS_PARA_RENDIRSE = 6

    # Por debajo de esto, un «objetivo cumplido» no se cree: abrir la app y
    # tocar una vez no es haber conseguido nada (S65, visto en vivo).
    MIN_JUGADAS_CUMPLIR = 3

    # Cada cuántas jugadas se vuelve a mirar QUÉ hay delante. Mirarlo cuesta
    # un `dumpsys` (~1-2 s) y una vuelta entera son 15-25 s, así que cada tres
    # jugadas es barato; no hacerlo nunca es jugar a ciegas sobre lo que sea
    # que se haya puesto encima.
    MIRAR_DELANTE_CADA = 3

    # Cada cuántos SEGUNDOS se mira la batería. Va por reloj y no por jugadas
    # porque la batería la gasta el tiempo, no las veces que toco: una partida
    # atascada mirando la misma pantalla gasta igual que una que avanza.
    MIRAR_BATERIA_CADA_S = 180.0

    # Cuánto se espera, como mucho, a que el juego acabe de bajar sus datos.
    # Una versión nueva de ZZZ son varios GB: media hora larga es lo normal, y
    # cortar a los dos minutos sería no dejarla empezar nunca. Mientras la
    # pantalla siga diciendo que descarga, este contador se reinicia — el tope
    # es contra quedarse colgada, no contra tardar.
    ESPERA_DESCARGA_S = 2400.0

    # Cuánto se espera ante una pantalla plana antes de dejar de creerse que es
    # una carga. ZZZ tarda decenas de segundos en pasar del negro al menú, así
    # que un tope corto devolvería el problema: preguntarle al modelo qué hacer
    # con el negro. Pasado esto, se pregunta — no se espera para siempre.
    ESPERA_CARGA_S = 75.0

    # Cuánto se espera, como mucho, a que el juego termine un trabajo suyo
    # que NO es una descarga: compilar sombreados, verificar ficheros,
    # descomprimir. Enzo, 9 sep 2026, viendo la pantalla: «no está peleando
    # porque esto está compilando el sombreado». Es más corto que el tope de
    # descarga porque estas tareas son de minutos, no de media hora.
    ESPERA_TRABAJO_S = 900.0

    # Cada cuánto se comprueba si el juego ya terminó. El OCR de la pantalla
    # entera cuesta segundos, así que mirar cada 12 s es suficiente para no
    # perder tiempo al acabar y barato de sobra frente a preguntar al modelo.
    MIRAR_TRABAJO_CADA_S = 12.0

    # Cuánto se espera a que vuelva el juego cuando alguien se sale de él.
    # Generoso a propósito: el que sale suele ser Enzo mirando el progreso
    # desde la terminal, y volver le lleva lo que le lleve.
    ESPERA_FUERA_S = 180.0

    def _sigue_siendo_el_juego(self) -> bool:
        """¿El de delante sigue siendo el juego que se está jugando?

        Sólo dice que sí **con certeza**: si no se sabe qué hay delante, o no
        se llegó a saber qué paquete es el juego, contesta que no y manda el
        caso al camino prudente (esperar sin tocar). Al revés —dar por bueno lo
        que no se sabe— es como se acaba tocando la pantalla equivocada.
        """
        if not self._pkg_juego:
            return False
        mirar = getattr(self.mando, "app_en_pantalla", None)
        if not callable(mirar):
            return False
        try:
            delante = mirar()
        except Exception as e:
            logger.debug("no pude mirar qué app hay delante: %s", e)
            delante = None
        if delante:
            return delante.lower() == self._pkg_juego.lower()

        # No se sabe quién está delante. Antes eso era «se ha salido», y el 9
        # de septiembre costó media fase: mientras ZZZ arrancaba —el móvil
        # ocupado cargando— `dumpsys` tardaba en contestar y la partida apuntó
        # tres veces «te has salido del juego» con el juego cargando delante.
        #
        # Segunda opinión, y sólo vale si es inequívoca: que el juego sea la
        # ÚNICA app activa. Con dos (la terminal encima, una ventana flotante)
        # se sigue contestando que no, porque entonces tocar sí puede caer
        # fuera del juego — que es lo que este freno existe para evitar.
        resumidas = getattr(self.mando, "apps_resumidas", None)
        if not callable(resumidas):
            return False
        try:
            abiertas = resumidas()
        except Exception as e:
            logger.debug("no pude listar las apps activas: %s", e)
            return False
        return (len(abiertas) == 1
                and abiertas[0].lower() == self._pkg_juego.lower())

    def _poner_el_juego_delante(self, objetivo: str) -> Tuple[bool, str]:
        """Deja delante la app del objetivo. Devuelve (se_puede_jugar, apunte).

        El bucle **no mira dónde está**: da toques sobre lo que haya en la
        pantalla. En la sesión 63, preguntado «¿puedes jugar al ZZZ por mí?»,
        se puso a jugar con **TikTok** delante y le dio dos toques creyendo que
        eran jugadas. Los toques a ciegas no dan error: tocan y se callan.

        Enzo eligió que lo abra ella y siga, en vez de avisar y esperar. Pero
        sólo actúa cuando se sabe **con certeza** que el sitio es otro: no saber
        qué app hay delante, o no reconocer el objetivo, deja pasar — lo
        contrario convertiría cualquier fallo de `dumpsys` en «no puedo jugar».
        """
        mirar_app = getattr(self.mando, "app_en_pantalla", None)
        if not callable(mirar_app):
            return True, ""
        # El paquete ya sabido para ESTE objetivo no se vuelve a resolver.
        # Resolverlo importa `celestia_lib.tools`, que cuesta 27 s la primera
        # vez (medido, S72), y se pagaba dentro del tiempo de la partida o de
        # la exploración: en la prueba del explorador eran 29,5 s de un tope de
        # 20, y la exploración acababa sin haber mirado una sola pantalla.
        if self._pkg_juego and getattr(self, "_objetivo_del_pkg", "") in ("", objetivo):
            pkg_obj = self._pkg_juego
        else:
            try:
                # Perezoso: `tools` ya importa `jugador`, y al revés a nivel de
                # módulo sería un ciclo.
                from celestia_lib.tools import AgentTools
                pkg_obj = AgentTools._resolver_app(objetivo)
            except Exception as e:
                logger.debug("no pude resolver «%s» a un paquete: %s", objetivo, e)
                return True, ""
            if not pkg_obj:
                return True, ""
            # Qué paquete es «el juego», para el resto de la partida.
            self._pkg_juego = pkg_obj
            self._objetivo_del_pkg = objetivo
        try:
            delante = mirar_app()
        except Exception as e:
            logger.debug("no pude mirar qué app hay delante: %s", e)
            return True, ""
        if not delante or delante.lower() == pkg_obj.lower():
            return True, ""

        # ¿Es una pantalla del sistema? Entonces no se aparta: se para. Abrir
        # el juego por encima dejaría a medias un consentimiento de cuenta o un
        # diálogo de permisos, y seguir tocando sería firmar por Enzo.
        bajo = delante.lower()
        if any(bajo.startswith(a) for a in APPS_INTOCABLES):
            return False, (f"delante tienes una pantalla del sistema ({delante}) "
                           f"— cuenta, permisos o tienda. Eso no lo toco yo: "
                           f"míralo tú y vuelvo a intentarlo cuando me digas")

        # ¿Sigue abierto, sólo que tapado? Entonces NO se relanza. `monkey`
        # sobre un juego que ya está en marcha lo devuelve al arranque: así se
        # perdió el equipo que Enzo había dejado montado (S67), y así murió la
        # partida del 9 de septiembre — Termux delante, ZZZ vivo detrás.
        resumidas = getattr(self.mando, "apps_resumidas", None)
        if callable(resumidas):
            try:
                abiertas = resumidas()
            except Exception as e:
                logger.debug("no pude listar las apps de delante: %s", e)
                abiertas = []
            if any(a.lower() == pkg_obj.lower() for a in abiertas):
                logger.info("«%s» sigue abierto detrás de %s: espero, no relanzo",
                            pkg_obj, delante)
                t_esp = time.time()
                while time.time() - t_esp < self.ESPERA_VUELVA_S:
                    time.sleep(3.0)
                    try:
                        if (mirar_app() or "").lower() == pkg_obj.lower():
                            return True, (f"esperé {time.time()-t_esp:.0f} s a que "
                                          f"{pkg_obj} volviera a quedarse delante")
                    except Exception:
                        continue
                return False, (f"el juego sigue abierto, pero delante tienes "
                               f"{delante}. No lo relanzo —perderías donde estás— "
                               f"ni toco a ciegas: déjalo en pantalla y sigo")

        logger.info("«%s» no está delante (hay %s): la abro", pkg_obj, delante)
        abrir = getattr(self.mando, "_sh", None)
        if not callable(abrir):
            return False, (f"delante tienes {delante}, no {pkg_obj}, y no sé "
                           f"abrirlo desde aquí. Ábrelo y lo intento.")
        try:
            # `monkey` lanza la actividad de inicio sin tener que saber cómo se
            # llama, que cambia con cada juego.
            abrir(f"monkey -p {pkg_obj} -c android.intent.category.LAUNCHER 1 "
                  f">/dev/null 2>&1", timeout=20)
        except Exception as e:
            return False, (f"no pude abrir {pkg_obj} ({e}). Ábrelo tú y lo "
                           f"intento otra vez.")

        # Esperar a que aparezca de verdad. Dar por abierto lo que aún no está
        # es volver al problema de siempre: tocar sobre otra pantalla.
        t0 = time.time()
        while time.time() - t0 < self.ESPERA_APP_S:
            time.sleep(2.0)
            try:
                if (mirar_app() or "").lower() == pkg_obj.lower():
                    tardo = time.time() - t0
                    return True, f"abrí {pkg_obj} (tardó {tardo:.0f} s en salir)"
            except Exception:
                continue
        return False, (f"abrí {pkg_obj} pero no ha llegado a salir en "
                       f"{self.ESPERA_APP_S:.0f} s. No juego a ciegas: "
                       f"míralo y dime.")

    def _juego_bloqueado(self, cap: Optional[Captura]) -> str:
        """¿Pone la pantalla que el juego no se puede jugar? El motivo, o "".

        Esto SÍ paga un OCR de pantalla completa —los ~15 s que la sesión 66
        quitó del carril normal—, y se paga a gusto: sólo se llama cuando la
        pantalla ya lleva varias vueltas sin moverse, o sea cuando esos 15 s
        ya se están perdiendo de todas formas. A cambio se deja de tocar un
        cartel durante minuto y medio y se le puede decir a Enzo qué pasa de
        verdad en vez de «no cambia nada».

        Determinista a propósito: si en la pantalla pone «maintenance», no hay
        nada que razonar. [[feedback_funcionar_cualquier_modelo]]
        """
        if cap is None:
            return ""
        try:
            with self.crono.medir("leer"):
                palabras = self.ojo.leer(cap)
        except Exception as e:
            logger.warning("no pude leer la pantalla para ver si está caída (%s)", e)
            return ""
        texto = " ".join(p.texto for p in palabras)
        m = _BLOQUEADA_RE.search(texto)
        if not m:
            return ""
        return (f"el juego no está jugable ahora mismo: la pantalla pone "
                f"«{m.group(0)}». No sigo tocando, que no va a servir de nada")

    def _esta_actualizando(self, cap: Optional[Captura]) -> str:
        """¿Está el juego bajando datos? Lo que se lee, o "".

        Se comprueba ANTES que `_juego_bloqueado` y con el mismo OCR, porque
        una descarga y un cartel de mantenimiento se parecen mucho desde
        dentro: las dos son pantallas que no cambian y en las que tocar no
        sirve de nada. La diferencia es qué hacer después — aquí se espera.
        """
        if cap is None:
            return ""
        try:
            with self.crono.medir("leer"):
                palabras = self.ojo.leer(cap)
        except Exception as e:
            logger.warning("no pude leer la pantalla de descarga (%s)", e)
            return ""
        texto = " ".join(p.texto for p in palabras)
        # Un cartel de mantenimiento manda sobre todo lo demás: si el juego
        # está caído, da igual que la pantalla enseñe un porcentaje.
        if _BLOQUEADA_RE.search(texto):
            return ""
        m = _DESCARGANDO_RE.search(texto)
        return m.group(0) if m else ""

    def _esperar_a_que_trabaje(self, rotulo: str, tope_s: float) -> float:
        """El juego está trabajando: se espera mirando barato. Segundos gastados.

        Compilar los sombreados de una versión nueva son varios minutos en los
        que la pantalla SÍ cambia —la barra avanza— y por eso el freno de
        «lleva rato sin cambiar» no salta nunca. Sin esto, cada vuelta le
        pregunta al modelo qué hacer con una barra de progreso: gasta cuota
        (la de Groq son ~6,8 miradas por minuto) y, peor, se come el tiempo de
        la fase de práctica. Enzo lo vio en su móvil el 9 de septiembre: la
        fase de pelear corriendo y el juego compilando.

        Aquí no se mira con el modelo ni se toca nada: se comprueba con OCR
        cada pocos segundos si el rótulo sigue puesto, que no cuesta cuota.
        """
        t_ini = time.time()
        self.bitacora.append(
            f"el juego está trabajando ({rotulo}): espero sin tocar ni preguntar")
        logger.info("el juego trabaja (%s): espero sin gastar miradas", rotulo)
        while time.time() - t_ini < tope_s:
            time.sleep(self.MIRAR_TRABAJO_CADA_S)
            cap = self.mando.ver(rapido=True)
            if cap is None:
                continue
            if not self._esta_actualizando(cap):
                gastado = time.time() - t_ini
                self.bitacora.append(
                    f"ya terminó ({gastado:.0f} s): sigo donde lo dejé")
                return gastado
        self.bitacora.append(
            f"lleva {tope_s/60:.0f} min trabajando: vuelvo a mirar por si acaso")
        return time.time() - t_ini

    def _apuntar_en_la_escuela(self, huella, bien: bool, ms: float) -> None:
        """Cómo fue la jugada, y salir a la red si hacía falta.

        Va todo junto y en un `try` porque la escuela es un extra: si algo de
        aprender falla —el disco, la red, un JSON a medias—, la partida sigue.
        Lo contrario sería que Celestia dejara de jugar por no poder estudiar.
        """
        if self.escuela is None:
            return
        try:
            if bien:
                self.escuela.acierto(huella, ms)
            else:
                self.escuela.fallo(huella)
        except Exception as e:
            logger.warning("la escuela no pudo apuntar el resultado (%s)", e)

    def _cuanto_se_movio(self, huella: Optional[Sequence]) -> Optional[float]:
        """Cuánto ha cambiado la pantalla desde la vuelta anterior, de 0 a 1.

        Es lo que separa un mundo quieto de una pelea: una pelea no está quieta
        nunca. Sale de comparar dos huellas que ya estaban calculadas, así que
        no cuesta ni una captura de más.
        """
        anterior, self._huella_anterior = self._huella_anterior, huella
        if huella is None or anterior is None or len(anterior) != len(huella):
            return None
        try:
            distancias = [Retina.distancia(a, b) for a, b in zip(anterior, huella)]
        except Exception:
            return None
        return sum(distancias) / len(distancias) if distancias else None

    def _lo_que_pide_el_juego(self, cap: Captura) -> str:
        """La orden que el juego tiene escrita en pantalla, o "".

        En una misión el juego casi siempre dice qué hacer —«habla con Sigrid»,
        «dirígete al Cuervo»— y eso vale más que cualquier plan que se invente
        el modelo mirando la imagen. Se lee con OCR y se busca por frase, así
        que no depende de que el modelo del día se fije: si está escrito, se
        encuentra. [[feedback_funcionar_cualquier_modelo]]

        Se guarda por pantalla para no pagar el OCR dos veces en la misma.
        """
        if cap is None:
            return ""
        # 🔴 Sólo la BANDA de arriba, nunca la pantalla entera. El objetivo de
        # una misión vive en el borde superior, y leer todo cuesta 14.805 ms
        # medidos —el cuello de todo el jugador— frente a los ~568 de una
        # franja. La primera versión de esto leía la pantalla completa en cada
        # vuelta y lo cazó un test: «la primera lectura es de la pantalla
        # entera».
        try:
            with self.crono.medir("leer"):
                palabras = self.ojo.leer(cap, zona=self.BANDA_DE_MISION)
        except Exception as e:
            logger.debug("no pude leer lo que pide el juego (%s)", e)
            return ""
        # Se guardan para que no haya que releer: la clase de pantalla las
        # quiere también, y un OCR de más por vuelta es lo que este jugador no
        # se puede permitir.
        self._palabras_vistas = list(palabras)
        texto = " ".join((p.texto or "").strip() for p in palabras)
        m = _MISION_RE.search(texto)
        return " ".join(m.group(1).split())[:80] if m else ""

    def _entender_antes_de_tocar(self) -> bool:
        """Si hay algo nuevo delante, buscarlo YA. Devuelve si se aprendió algo.

        Antes esto se hacía DESPUÉS de la jugada, para que el toque saliera a
        su hora. Enzo pidió lo contrario y tiene razón: la jugada que se da sin
        entender la pantalla es justo la que sale mal, y luego hay que
        deshacerla. Vale más pagar segundo y medio una vez.

        Y sólo lo paga lo que NO se sabe: `_por_estudiar` sólo se pone cuando
        la situación es nueva o lleva rato atascada. Una pantalla conocida no
        entra aquí, y una dominada ni siquiera llega a mirarse — la resuelve el
        libro en menos de un milisegundo.
        """
        if self.escuela is None or self._por_estudiar is None:
            return False
        lec, self._por_estudiar = self._por_estudiar, None
        try:
            if not self.escuela.estudiar(lec):
                return False
        except Exception as e:
            logger.warning("no pude estudiar «%s» (%s)", lec, e)
            return False
        self.bitacora.append(
            f"«{lec.titulo}» no lo conocía: lo busqué antes de tocar nada")
        logger.info("aprendido antes de tocar: %s", lec.titulo)
        return True

    def _sin_bateria(self, lim: Limites) -> str:
        """¿Hay que parar por batería? Devuelve el motivo, o "" si se sigue.

        `None` (el mando no sabe de baterías, o el móvil no contestó) deja
        seguir a propósito: es la misma regla que `app_en_pantalla` (S63), no
        frenar por lo que no se sabe. Lo que NO puede volver a pasar es que
        el freno esté puesto y no salte por un número mal leído — de eso va
        `MandoAndroid.bateria`.
        """
        if not lim.bateria_minima:
            return ""
        try:
            bat = self.mando.bateria()
        except Exception as e:
            logger.warning("no pude leer la batería (%s) — sigo", e)
            return ""
        # Se comprueba el TIPO, no se confía en que convertir falle: un mando
        # que devuelva cualquier cosa reventaba con TypeError en la
        # comparación de abajo —fuera de todo try—, que es justo el fallo que
        # este freno viene a evitar. Y `int()` no sirve de coladero: sobre un
        # MagicMock devuelve 1 tan campante, o sea «queda un 1%», o sea una
        # partida cortada por un número que nadie ha medido. `bool` fuera
        # aparte, que es un `int` encubierto y `True` valdría 1%.
        if isinstance(bat, bool) or not isinstance(bat, (int, float)):
            if bat is not None:
                logger.warning("el mando dio una batería que no es un número "
                               "(%r) — sigo", bat)
            return ""
        bat = int(bat)
        if bat >= lim.bateria_minima:
            return ""
        return (f"queda un {bat}% de batería — lo dejo aquí para no quedarte "
                f"sin móvil")

    def jugar(self, objetivo: str, limites: Optional[Limites] = None) -> Dict[str, Any]:
        lim = limites or Limites()
        ok, motivo_no = self.mando.disponible()
        if not ok:
            return {"jugadas": 0, "motivo": f"no puedo jugar: {motivo_no}",
                    "bitacora": [], "ritmo": ""}
        # ¿Le queda batería? Antes de abrir nada: empezar una partida con el
        # móvil al 8% es gastarle a Enzo lo que le queda de teléfono. S68.
        falta = self._sin_bateria(lim)
        if falta:
            return {"jugadas": 0, "motivo": falta, "bitacora": [], "ritmo": ""}
        # ¿Estoy siquiera donde se juega? Antes de montar nada y, sobre todo,
        # antes de tocar: un toque sobre la app equivocada no se puede deshacer.
        puedo, apunte_app = self._poner_el_juego_delante(objetivo)
        if not puedo:
            return {"jugadas": 0, "motivo": apunte_app, "bitacora": [], "ritmo": ""}
        # Encender el canal ANTES de empezar. Montar el puente cuesta ~1,7 s
        # (arrancar un `app_process` con el dex de Shizuku) y se paga en la
        # primera orden que lo use — que es el primer TOQUE, o sea la primera
        # jugada, la que peor sienta que vaya lenta. Aquí ese arranque se paga
        # mientras el jugador aún no ha mirado nada. Sesión 61.
        calentar = getattr(self.mando, "_sh", None)
        if callable(calentar):
            try:
                calentar("true", timeout=10)
            except Exception as e:
                logger.debug("no pude encender el canal antes de jugar: %s", e)
        if self.mirar:
            Marcador.precalentar()
        t0 = time.time()
        jugadas = 0      # movimientos hechos
        vueltas = 0      # veces que se ha mirado y decidido
        sin_cambio = 0
        tropiezos = 0
        cargando_desde: Optional[float] = None
        # Al pensador se le da una meta; al libro, el nombre del juego a secas.
        objetivo_pensar = self.meta or objetivo_para_pensar(objetivo)
        # Abrir una app es una acción sobre el móvil: si no se apunta, el
        # resultado dice «2 jugadas» y se calla que además abrió un juego.
        apertura = [apunte_app] if apunte_app else []
        huella_previa: Optional[Tuple] = None
        # Con qué pantalla se usó la última jugada sacada del libro, para poder
        # borrarla si resulta que ya no sirve.
        ultima_del_libro: Optional[Tuple] = None
        # Cómo era la pantalla al empezar (apaisada o no) y desde cuándo dejó
        # de serlo, que es como se nota que hay otra cosa delante.
        forma_inicial: Optional[bool] = None
        fuera_desde: Optional[float] = None
        # Cómo era la pantalla al empezar: apaisada o vertical.
        forma_inicial: Optional[bool] = None
        motivo = "llegué al tope de jugadas"
        # La primera lectura ya se hizo arriba, antes de abrir el juego.
        prox_bateria = time.time() + self.MIRAR_BATERIA_CADA_S
        # Desde cuándo lleva descargando. `None` = no está descargando.
        descargando_desde: Optional[float] = None
        # Segundos que el juego pasó trabajando (compilando, verificando) y que
        # NO se le descuentan a la partida: si la fase de pelear dura 35 min y
        # el juego se pasa 10 compilando, quedan 25 de pelea, no 35 de reloj.
        regalado = 0.0
        # Cuántas veces seguidas se ha decidido «esperar». Dos seguidas es la
        # señal de que no es una animación, y ahí ya compensa el OCR.
        esperas_seguidas = 0
        # (pantalla, jugada) de las últimas vueltas, para cazar los bucles.
        ultimas: List[Tuple[Any, str]] = []
        # Bucles cortados seguidos, y vueltas seguidas en que la vista no
        # contestó nada. Cada uno con su cuenta: no son el mismo problema.
        bucles = 0
        mudos = 0
        try:
            while jugadas < lim.max_jugadas:
                if time.time() - t0 - regalado > lim.max_segundos:
                    motivo = "se acabó el tiempo que me diste"
                    break
                if lim.debo_parar():
                    motivo = "me dijiste que parara"
                    break
                if time.time() >= prox_bateria:
                    prox_bateria = time.time() + self.MIRAR_BATERIA_CADA_S
                    falta = self._sin_bateria(lim)
                    if falta:
                        motivo = falta
                        break

                # ¿Sigo donde se juega? La comprobación del principio no
                # basta: una ventana flotante, una notificación a pantalla
                # completa o una llamada se ponen delante EN MEDIO de la
                # partida, y desde ese momento los toques van a otra cosa.
                # Pasó con el móvil de Enzo delante (S65): Termux estaba en
                # ventana flotante sobre ZZZ —el juego cargado detrás, se veía
                # en la captura— y se pasó la partida intentando cerrar la
                # ventana. Peor todavía: **esos toques caían sobre la terminal
                # donde corre la sesión de Claude**, que es como tocar los
                # mandos de quien te está mirando jugar.
                if vueltas and vueltas % self.MIRAR_DELANTE_CADA == 0:
                    sigo, apunte = self._poner_el_juego_delante(objetivo)
                    if not sigo:
                        motivo = apunte
                        break
                    if apunte:
                        self.bitacora.append(apunte)

                with self.crono.medir("ver"):
                    cap = self.mando.ver()
                if not cap:
                    tropiezos += 1
                    if tropiezos >= lim.max_tropiezos:
                        motivo = (f"no consigo ver la pantalla "
                                  f"({tropiezos} veces seguidas)")
                        break
                    # Una captura fallida suele ser el móvil ocupado un
                    # instante —una carga, el juego pidiendo la GPU entera—.
                    # Se le da aire y se vuelve a mirar.
                    time.sleep(1.0)
                    continue

                # 🔴 ¿Sigo mirando el juego? La comprobación más barata, y la
                # única que no puede contestar «no lo sé»: la FORMA de la
                # pantalla. El juego apaisado da capturas anchas (2412x1084) y
                # la terminal las da altas (1084x2412).
                #
                # Sesión 66, dos veces: salió Termux con el teclado abierto y
                # el jugador siguió tocando — **escribió una letra en el prompt
                # de la sesión de Claude**. `app_en_pantalla()` no lo frenó
                # porque devuelve `None` cuando no lo sabe, y `None` deja pasar
                # a propósito (S63).
                #
                # Y no se para la partida, se **espera**: quien sale de la app
                # suele ser Enzo mirando cómo va, y volver. Lo que no puede
                # pasar es tocar mientras tanto.
                apaisado = cap.ancho > cap.alto
                if forma_inicial is None:
                    forma_inicial = apaisado
                elif apaisado != forma_inicial and self._sigue_siendo_el_juego():
                    # ⚠️ Girar no es salirse, y esto costó una partida entera.
                    # El arranque de ZZZ pasa por las actividades del SDK de
                    # HoYoverse (login, avisos, elegir servidor), que salen en
                    # VERTICAL aunque el juego sea apaisado: sesión 67, la
                    # partida se cortó a los 15 s con el juego perfectamente
                    # delante y el registro de Android diciéndolo
                    # (`wm_resume_activity … ComboSDKActivity`). Cuando el de
                    # delante sigue siendo el juego, lo que ha cambiado es la
                    # pantalla del juego: se toma la forma nueva como buena.
                    forma_inicial = apaisado
                    fuera_desde = None
                elif apaisado != forma_inicial:
                    if fuera_desde is None:
                        fuera_desde = time.time()
                        self.bitacora.append(
                            "te has salido del juego: espero sin tocar nada")
                    if time.time() - fuera_desde < self.ESPERA_FUERA_S:
                        time.sleep(2.0)
                        continue
                    motivo = ("llevas fuera del juego un buen rato y no voy a "
                              "tocar a ciegas: vuelve a ponerlo y sigo")
                    break
                else:
                    fuera_desde = None

                huella = Retina.firma(cap, ZONAS_HUELLA) if cap.crudo else None

                # ¿Es una carga? Entonces no hay nada que decidir: se espera,
                # como esperaría cualquiera, y **sin gastar una mirada** — que
                # con el tope de Groq (7.000 tokens/min, 1.024 por pantalla)
                # son ~6,8 por minuto y aquí se iban de seis en seis.
                if huella and Retina.pantalla_de_paso(huella):
                    if cargando_desde is None:
                        cargando_desde = time.time()
                        self.bitacora.append("pantalla en negro: espero a que cargue")
                    if time.time() - cargando_desde < self.ESPERA_CARGA_S:
                        time.sleep(1.0)
                        continue
                    # Se acabó la paciencia: puede que no fuera una carga sino
                    # una pantalla que pide un toque para seguir. Que lo mire
                    # el modelo, que para eso está.
                else:
                    cargando_desde = None

                # ¿Cambió algo desde la última jugada? Si no, estoy tocando al aire.
                if huella and huella_previa and Retina.firmas_parecidas(huella, huella_previa):
                    sin_cambio += 1
                    # Si lo que se acaba de repetir venía del libro y no ha
                    # movido nada, la culpable es esa jugada: fuera del libro.
                    # No cuesta ni una captura extra —la comparación ya estaba
                    # hecha— y es lo que evita quedarse clavado en un botón que
                    # antes servía y ya no.
                    if sin_cambio >= 2 and ultima_del_libro is not None:
                        if self.libro.olvidar(ultima_del_libro, objetivo):
                            self.bitacora.append(
                                "esto ya no funciona aquí: lo borro de mi libro")
                        ultima_del_libro = None
                        sin_cambio = 0
                        continue
                    if sin_cambio >= lim.max_sin_cambio:
                        # ¿Está bajando la actualización? Entonces no es un
                        # atasco: es el juego haciendo su trabajo. Se espera
                        # sin tocar y sin gastar miradas, y el contador de
                        # paciencia se reinicia mientras siga descargando.
                        bajando = self._esta_actualizando(cap)
                        if bajando:
                            if descargando_desde is None:
                                descargando_desde = time.time()
                                self.bitacora.append(
                                    f"el juego está trabajando ({bajando}): "
                                    f"espero sin tocar nada")
                                logger.info("el juego descarga (%s): espero", bajando)
                            if time.time() - descargando_desde < self.ESPERA_DESCARGA_S:
                                sin_cambio = 0
                                time.sleep(10.0)
                                continue
                            motivo = (f"lleva {self.ESPERA_DESCARGA_S/60:.0f} min "
                                      f"descargando y no acaba. Míralo tú, que "
                                      f"algo se ha atascado")
                            break
                        # Antes de rendirse: LEER lo que pone. Una pantalla
                        # que no cambia porque el juego está en mantenimiento
                        # y otra porque estoy tocando mal se parecen mucho
                        # desde aquí, y para Enzo no se parecen en nada.
                        bloqueado = self._juego_bloqueado(cap)
                        if bloqueado:
                            motivo = bloqueado
                            # Y lo aprendido sobre este cartel, fuera: no es
                            # el juego, es una pantalla que hoy está y mañana
                            # no. Guardarla ensucia el libro para siempre.
                            if huella is not None and self.libro.olvidar(huella, objetivo):
                                self.bitacora.append(
                                    "borro del libro lo que aprendí de esa pantalla")
                            break
                        motivo = ("la pantalla lleva rato sin cambiar — o el juego está "
                                  "esperando algo, o estoy tocando donde no hay nada")
                        break
                else:
                    sin_cambio = 0
                    # La pantalla se ha movido: si estaba descargando, ya no.
                    descargando_desde = None

                # 1) ¿Ya sé qué hacer aquí? (microsegundos)
                t_vuelta = time.time()
                accion = self.libro.buscar(huella, objetivo) if huella else None
                palabras: List[Palabra] = []
                mudas_antes = self.sin_vista_rapida
                if accion is None:
                    # 2) Con ojos: la imagen al modelo (496 ms) y el OCR sólo
                    #    en la franja del botón (568 ms). Sesión 61.
                    mirada = self._mirar_y_decidir(objetivo_pensar, cap, huella)
                    if mirada is not None:
                        accion, palabras = mirada
                        # ¿Es algo que no había visto nunca? Entonces se para a
                        # ENTENDERLO antes de tocar. Enzo, 9 sep 2026: «le
                        # cuesta mucho entender esas cosas, así que antes de
                        # tocar sin parar que entienda que lo que está viendo
                        # es nuevo — o si ya sabe, que actúe sin ralentizar».
                        #
                        # Cuesta ~1,5 s de búsqueda y media mirada más, y se
                        # paga UNA vez por situación: la próxima vez esa
                        # pantalla ya viene explicada, y cuando se domine la
                        # resolverá el libro sin pasar por aquí. Lo que ya se
                        # sabe no paga NADA — este bloque ni se ejecuta.
                        if self._entender_antes_de_tocar():
                            otra = self._mirar_y_decidir(objetivo_pensar, cap,
                                                         huella, registrar=False)
                            if otra is not None:
                                accion, palabras = otra
                    elif not self.mirar:
                        # 3) El camino de siempre, entero: OCR de la pantalla
                        #    completa y un modelo de texto. Sólo cuando NO hay
                        #    vista — sin ojos no hay otra forma de saber qué
                        #    pone en la pantalla.
                        with self.crono.medir("leer"):
                            palabras = self.ojo.leer(cap)
                        accion = self._preguntar(objetivo_pensar, palabras)
                    # 🔴 Y si hay vista pero esta vez no contestó, NO se cae al
                    # OCR. Enzo, 8 sep 2026: «¿por qué está el OCR? si sólo
                    # usábamos LLM». Tiene razón, y los números también: dentro
                    # de un juego el OCR lee ruido sobre el arte ('31n', 'eR')
                    # y cuesta 15-60 s por vuelta —en la última partida se
                    # llevaba 4.784 ms de media—, así que el respaldo salía más
                    # caro que el fallo que remediaba. Ahora una mirada que no
                    # contesta cuenta como tropiezo: se espera un poco y se
                    # vuelve a mirar, que con cuatro proveedores y la espera por
                    # cuota tarda segundos, no un minuto. El OCR sigue estando
                    # para lo que sí sabe hacer: leer un rótulo en una franja
                    # concreta (`_localizar`), que es donde acierta.
                    if accion is None and self.sin_vista_rapida > mudas_antes:
                        # No es que no se entienda la pantalla: es que no ha
                        # contestado nadie. Eso se arregla esperando —la cuota
                        # va por minuto— y no cerrando la partida.
                        mudos += 1
                        if mudos >= self.MUDOS_PARA_RENDIRSE:
                            motivo = ("los modelos llevan un rato sin contestar "
                                      "(saturados). Lo dejo y lo intento luego")
                            break
                        espera = min(self.ESPERA_CUOTA_S * mudos, 45.0)
                        self.bitacora.append(
                            f"no me contesta ningún modelo: espero {espera:.0f} s")
                        logger.info("nadie contestó (%d seguidas): espero %.0f s",
                                    mudos, espera)
                        time.sleep(espera)
                        continue
                    if accion is None:
                        tropiezos += 1
                        if tropiezos >= lim.max_tropiezos:
                            motivo = (f"no supe qué hacer con esta pantalla "
                                      f"({tropiezos} veces seguidas)")
                            break
                        # Casi siempre es una pantalla de paso: una carga, un
                        # vídeo, una animación de entrada. Lo que hace una
                        # persona es esperar a que termine, no cerrar el juego.
                        time.sleep(1.5)
                        continue
                    peligro = self._cuesta_dinero(accion, palabras, lim)
                    if peligro:
                        # Por qué se para, dicho con precisión. El freno cubre
                        # dos cosas distintas desde S68 —lo que cuesta dinero y
                        # lo que compromete la cuenta— y decirle a Enzo que
                        # «Acepto los términos» puede costarle dinero es
                        # mentira, además de dejarle sin saber qué mirar.
                        if _CUENTA_RE.search(peligro):
                            motivo = (f"me he parado: iba a tocar «{peligro}», y eso "
                                      f"es cosa tuya, no mía — una cuenta, un permiso "
                                      f"o unas condiciones no las acepto yo por ti. "
                                      f"Míralo y sigo cuando me digas")
                        else:
                            motivo = (f"me he parado: iba a tocar «{peligro}», y eso puede "
                                      "costarte dinero. Dímelo tú si quieres que lo pulse")
                        break
                    # Los movimientos de la ráfaga van a ciegas, así que el
                    # freno se les pasa uno a uno: si alguno cae sobre un botón
                    # de pagar, se corta la ráfaga AHÍ y se juega lo de antes.
                    # No se cierra la partida por eso —el golpe peligroso ni se
                    # llegó a dar—, que sería castigar por una intención.
                    limpios: List[Accion] = []
                    for q in accion.pasos:
                        if self._cuesta_dinero(q, palabras, lim):
                            break
                        limpios.append(q)
                    accion.pasos = limpios

                # ¿Otra vez esperando? Puede que no sea una animación, sino
                # el juego trabajando: compilando sombreados, verificando o
                # bajando datos. Se comprueba a la SEGUNDA espera seguida —la
                # primera puede ser un menú abriéndose, y el OCR cuesta— y si
                # lo está, se espera de una vez sin preguntar más y sin que
                # ese rato salga del tiempo de juego.
                if accion.tipo == "esperar" and not accion.pasos:
                    esperas_seguidas += 1
                    if esperas_seguidas >= 2 and regalado < self.ESPERA_DESCARGA_S:
                        trabajo = self._esta_actualizando(cap)
                        if trabajo:
                            regalado += self._esperar_a_que_trabaje(
                                trabajo, self.ESPERA_TRABAJO_S)
                            esperas_seguidas = 0
                            sin_cambio = 0
                            huella_previa = None
                            continue
                else:
                    esperas_seguidas = 0

                # ¿Esto ya lo he hecho aquí, y aquí sigo? Un bucle no es una
                # pantalla quieta: es volver al mismo sitio después de haber
                # cambiado de pantalla, y por eso el freno de «sin cambio» no
                # lo ve. Pelear queda fuera a propósito — machacar el mismo
                # botón contra el muñeco de pruebas es lo correcto, no un bug.
                if huella is not None and accion.tipo != "pelear":
                    firma_jugada = accion.resumen()
                    repes = sum(1 for h, r in ultimas
                                if r == firma_jugada
                                and Retina.firmas_parecidas(huella, h))
                    ultimas.append((huella, firma_jugada))
                    del ultimas[:-self.MEMORIA_BUCLE]
                    if repes >= self.REPES_PARA_BUCLE:
                        # Se apunta en la bitácora Y se dice por qué: esas
                        # líneas son las que van al prompt como «lo que ya he
                        # hecho», así que decirlo aquí es decírselo al modelo.
                        self.bitacora.append(
                            f"«{firma_jugada}» ya lo probé {repes + 1} veces en "
                            f"esta misma pantalla y sigo igual: NO lo repitas, "
                            f"prueba otra cosa")
                        logger.info("bucle: «%s» repetida %d veces aquí",
                                    firma_jugada, repes + 1)
                        if accion.del_libro and self.libro.olvidar(huella, objetivo):
                            self.bitacora.append(
                                "y lo borro del libro: aquí ya no sirve")
                        ultimas = [(h, r) for h, r in ultimas if r != firma_jugada]
                        ultima_del_libro = None
                        bucles += 1
                        if bucles >= self.BUCLES_PARA_RENDIRSE:
                            motivo = (f"llevo {bucles} vueltas dando lo mismo sin "
                                      f"avanzar. Míralo tú, que aquí me pierdo")
                            break
                        continue

                # Si la jugada es pelear, esto es un combate y no hay más que
                # hablar: lo dice quien está mirando la pantalla. Hace falta
                # porque las señales baratas se equivocan justo aquí — en el
                # campo de pruebas de ZZZ el fondo es liso, el marcador ve
                # «botones» por todas partes y la pantalla se clasificaba como
                # MENÚ en plena pelea (visto el 10 sep 2026, peleando).
                if accion.tipo == "pelear" and self._escena_vista != "combate":
                    self._escena_vista = "combate"
                    self.bitacora.append("estoy en: combate")

                ultima_del_libro = huella if accion.del_libro else None
                cadena = accion.cadena()
                with self.crono.medir("actuar"):
                    if len(cadena) > 1:
                        self.mando.secuencia(cadena)
                    else:
                        accion.ejecutar(self.mando)
                jugadas += len(cadena)
                vueltas += 1
                tropiezos = 0
                bucles = 0
                mudos = 0
                self.bitacora.append(
                    f"{accion.resumen()} — {accion.porque}".strip(" —") +
                    (f" (+{len(cadena) - 1} seguidos)" if len(cadena) > 1 else ""))

                # 3) Aprender solo si de verdad sirvió.
                # Si la jugada venía del libro no hay nada que aprender, así
                # que tampoco hay que esperar a ver el resultado: esa espera
                # era el 98% de lo que costaba una vuelta ya conocida.
                if huella is not None and not accion.del_libro:
                    time.sleep(0.35)
                    despues = self.mando.ver(rapido=True)
                    if despues and despues.crudo:
                        nueva = Retina.firma(despues, ZONAS_HUELLA)
                        if not Retina.firmas_parecidas(huella, nueva):
                            self.libro.aprender(huella, objetivo, accion)
                            self._apuntar_en_la_escuela(
                                huella, True, (time.time() - t_vuelta) * 1000)
                        else:
                            self._apuntar_en_la_escuela(huella, False, 0)
                elif huella is not None:
                    # Vino del libro: se da por buena SIN comprobar. No es
                    # dejadez, es el cierre del círculo — el libro sólo guarda
                    # jugadas que ya se vio que funcionaban, así que resolver
                    # por libro ES el examen aprobado. Y comprobarlo costaría
                    # los 350 ms de espera que la sesión 61 quitó justo aquí,
                    # que son el 98% de lo que cuesta una vuelta conocida.
                    self._apuntar_en_la_escuela(
                        huella, True, (time.time() - t_vuelta) * 1000)
                if huella is not None:
                    huella_previa = huella
        except Parada as p:
            motivo = str(p)
            # Un «objetivo cumplido» sin apenas jugadas es humo: el modelo lo
            # dice al ver el juego delante, no porque haya conseguido nada.
            # Decirle a Enzo «cumplido» cuando lo único que se hizo fue abrir
            # la app y tocar una vez es exactamente lo que no puede pasar.
            if motivo == "objetivo cumplido" and jugadas < self.MIN_JUGADAS_CUMPLIR:
                motivo = (f"dice que ya está cumplido, pero sólo llevaba "
                          f"{jugadas} jugada{'s' if jugadas != 1 else ''}: no me "
                          f"lo creo. Dime qué quieres que consiga y voy a por ello")
        # Lo aprendido se guarda pase lo que pase, también si la partida acabó
        # mal: una lección cuesta una salida a la red y perderla porque el
        # móvil se apagó a mitad sería pagarla dos veces.
        aprendizaje = ""
        if self.escuela is not None:
            try:
                self.escuela.guardar()
                aprendizaje = self.escuela.resumen_partida()
            except Exception as e:
                logger.warning("no pude cerrar la escuela (%s)", e)
        r = {"jugadas": jugadas, "motivo": motivo,
             "segundos": round(time.time() - t0, 1),
             "bitacora": apertura + self.bitacora,
             "ritmo": self.crono.veredicto()}
        if aprendizaje:
            r["aprendido"] = aprendizaje
        if self.mirar and self.sin_vista_rapida:
            veces = "veces" if self.sin_vista_rapida != 1 else "vez"
            r["aviso"] = (f"la vista rápida no contestó {self.sin_vista_rapida} "
                          f"{veces} (saturada o sin red), así que leí la pantalla con "
                          f"OCR: eso son ~60 s por vuelta en vez de medio "
                          f"segundo. Con el juego a este ritmo no llego a nada "
                          f"que se mueva")
        return r


# ─────────────────────────────────────────────────────────────────────────
#  Atajos para quien solo quiere jugar
# ─────────────────────────────────────────────────────────────────────────

# Qué distingue «juega al zzz» de «gana la partida»: un objetivo que sólo
# nombra el juego no tiene nada que cumplir, así que el pensador contesta
# «listo» en cuanto ve el juego delante y se acabó la partida en dos jugadas
# (visto en vivo, S65: abrió ZZZ, tocó una vez y dijo «objetivo cumplido»).
# Nadie que pide «juega al zzz» quiere eso: quiere que juegue.
_META_EN_OBJETIVO_RE = re.compile(
    r"(?i)\b(?:gana|ganar|g[aá]name|vence|vencer|derrota[r]?|supera[r]?|"
    r"completa[r]?|termina[r]?|acaba[r]?|pasa[r]?|sube|subir|farmea[r]?|"
    r"consigue|conseguir|recoge[r]?|reclama[r]?|entrena[r]?|mejora[r]?|"
    r"avanza[r]?|misi[oó]n|misiones|nivel|niveles|partida|diaria|diarias|"
    r"recompensa|recompensas|puntos)\b")


def objetivo_para_pensar(objetivo: str) -> str:
    """Lo que se le dice al modelo. NO es lo que nombra el libro de jugadas.

    El libro se sigue llamando como el juego («zzz»), que es lo que hace que
    lo aprendido en una partida sirva en la siguiente; al pensador, en cambio,
    hay que darle algo que se pueda cumplir o no cumplir.
    """
    objetivo = (objetivo or "").strip()
    if not objetivo or _META_EN_OBJETIVO_RE.search(objetivo):
        return objetivo
    return (f"jugar a {objetivo} y avanzar: entra en la partida, acepta o "
            f"salta los diálogos y juega lo que el juego proponga. Estar "
            f"dentro del juego NO es haber cumplido nada.")


def mando_del_aparato(aparato: str = "movil", **kw) -> Mando:
    if aparato in ("pc", "ordenador", "escritorio"):
        return MandoPC(**kw)
    return MandoAndroid(**kw)


def pensador_groq(clave: str, modelo: str = "qwen/qwen3.8-27b") -> Callable[[str], str]:
    """El cerebro rápido. Groq con un modelo que NO razona antes de contestar.

    El modelo importa más de lo que parece: los `gpt-oss` de Groq escriben su
    razonamiento antes de la respuesta, así que con un presupuesto corto de
    tokens devuelven la cadena vacía y la jugada se pierde. Medido aquí:
    qwen3.8-27b ~290 ms y JSON limpio; gpt-oss-120b, vacío. Por eso este
    defecto no coincide con el `GROQ_MODEL` general de Celestia.
    """
    import urllib.request

    def pensar(prompt: str) -> str:
        cuerpo = json.dumps({
            "model": modelo,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 120, "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions", data=cuerpo,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {clave}",
                     # Sin User-Agent propio, Groq responde 403: el que pone
                     # urllib por defecto está bloqueado. El resto de Celestia
                     # ya manda este mismo; aquí se aprendió a base de 403.
                     "User-Agent": "Mozilla/5.0 (compatible; CelestiaAI/1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception as e:
            logger.warning("Groq no contestó: %s", e)
            return ""
    return pensar


def diagnostico(aparato: str = "movil") -> str:
    """Mide este aparato de verdad y dice a qué se puede jugar en él.

    Se ejecuta antes de prometer nada:

        python3 -m celestia_lib.jugador          # el móvil
        python3 -m celestia_lib.jugador pc       # el PC

    Los números de un móvil no valen para otro (ni para el mismo con el juego
    abierto comiéndose la CPU), así que esto no trae valores de fábrica: mide
    aquí y ahora.
    """
    mando = mando_del_aparato(aparato)
    lineas = [f"Aparato: {mando.nombre}"]
    # El canal es el dato que más manda de todo el diagnóstico: con rish cada
    # orden cuesta segundos y no hay optimización que lo salve.
    canal = getattr(mando, "canal", None)
    if canal == "rish":
        lineas.append("Canal: rish (Shizuku) — cada orden arranca un proceso nuevo "
                      "(~1,6 s). Es el techo de todo lo de abajo.")
    elif canal == "adb":
        lineas.append("Canal: adb — el daemon ya está en marcha, las órdenes van "
                      "por una conexión abierta.")
    ok, motivo = mando.disponible()
    if not ok:
        lineas.append(f"✗ No puedo jugar aquí: {motivo}")
        return "\n".join(lineas)
    lineas.append("✓ Puedo tocar la pantalla")
    a, al = mando.resolucion()
    lineas.append(f"Pantalla: {a}×{al}")

    crono = Cronometro()
    for _ in range(3):
        with crono.medir("ver"):
            cap = mando.ver(rapido=True)
    if not cap:
        lineas.append("✗ Capturo la pantalla pero no me llega nada.")
        return "\n".join(lineas)
    with crono.medir("firma"):
        Retina.firma(cap, ZONAS_HUELLA)
    ojo = Ojo()
    if ojo.disponible:
        cap_png = mando.ver()
        if cap_png:
            with crono.medir("leer"):
                palabras = ojo.leer(cap_png)
            lineas.append(f"Leo en pantalla: {len(palabras)} palabras")
    else:
        lineas.append("⚠ Sin tesseract no sé leer la pantalla (apt install tesseract-ocr)")
    with crono.medir("actuar"):
        mando.tocar(Punto(0.5, 0.999))     # esquina inferior: inofensivo
    lineas.append("")
    lineas.append(crono.veredicto())
    lineas.append(crono.veredicto_reflejos())
    lineas.append("")
    lineas.append("Y lo que de verdad manda: una pantalla ya vista se resuelve con la "
                  "firma de colores, sin leer ni preguntar — ahí el ciclo baja a "
                  "milisegundos por muy lento que sea todo lo de arriba.")
    return "\n".join(lineas)


if __name__ == "__main__":
    import sys as _sys
    print(diagnostico(_sys.argv[1] if len(_sys.argv) > 1 else "movil"))
