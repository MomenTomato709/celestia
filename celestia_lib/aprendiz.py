"""Aprender a jugar mirando jugar a Enzo.

Enzo, 8 sep 2026: «primero tenemos que hacer que Celestia sepa cómo jugar, tiene
que aprender antes de meterse a jugar ella; ¿cómo le podemos enseñar?».

Hasta ahora sólo había dos formas de que supiera algo de un juego, y las dos
son malas para empezar:

  · **Probar tocando.** Es lo que hace el bucle de `jugador.py`: mira, decide,
    toca, y si la pantalla cambió apunta la jugada. Funciona, pero aprende a
    base de equivocarse en el móvil de alguien, y lo caro no es el error: es
    que cada intento cuesta una mirada, y de esas hay ~6 por minuto.
  · **Que se lo cuente yo.** Está prohibido a propósito (ver `zzz.py`): mi
    conocimiento del juego está desfasado y suena seguro igualmente.

Queda la buena, que es como aprende cualquiera: **mirar a alguien que sabe**.
Mientras Enzo juega, aquí se apunta qué había en la pantalla y dónde puso el
dedo. De ahí salen las dos cosas que hoy no tiene:

  1. **El mapa de sus controles** — dónde está atacar, esquivar, el relevo—,
     que es exactamente lo que el `Vigia` necesita para pelear solo y lo único
     que no se puede deducir mirando una captura.
  2. **Un libro de jugadas lleno desde el primer día**, con jugadas que ya se
     sabe que funcionan porque las hizo él.

Y no toca la pantalla en ningún momento: aquí sólo se mira.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from celestia_lib.jugador import (Accion, Captura, LibroDeJugadas, MandoAndroid,
                                  Punto, Retina, ZONAS_HUELLA, Zona,
                                  freno_pisado)
from celestia_lib.paths import ENV_FILE, MEM_DIR, MOVIL_DIR, ROOT

logger = logging.getLogger(__name__)


@dataclass
class Toque:
    """Un dedo que baja. En píxeles del PANEL, que no es lo que se ve."""
    t: float
    x: int
    y: int


# ─────────────────────────────────────────────────────────────────────────
#  El panel táctil no es la pantalla
# ─────────────────────────────────────────────────────────────────────────

class Panel:
    """Traduce dónde tocó un dedo a dónde está eso en lo que se ve.

    🔴 Es el problema de fondo de todo este módulo, y no es un detalle: el
    panel táctil **no gira con la pantalla**. Sus ejes son siempre los físicos
    (aquí 1084x2412), así que con un juego en horizontal —o sea, con ZZZ— un
    toque en la esquina de arriba del panel puede ser la esquina de la
    izquierda de lo que se ve. Cuatro giros posibles y ninguno da error: el
    aprendizaje entero saldría torcido en silencio, que es como salen mal las
    cosas en este aparato.

    Se podría deducir de `mRotation`, y es justo lo que **no** se hace: la
    correspondencia entre el número de rotación y el sentido del giro depende
    del fabricante, y equivocarse cuesta una tarde. Se calibra: dos toques de
    Enzo en dos esquinas que él ve, y ya no hay nada que adivinar.
    """

    def __init__(self, ancho_panel: int, alto_panel: int,
                 ancho_vista: int, alto_vista: int,
                 giro: int = 0, espejo_x: bool = False, espejo_y: bool = False):
        self.ancho_panel = ancho_panel
        self.alto_panel = alto_panel
        self.ancho_vista = ancho_vista
        self.alto_vista = alto_vista
        # `giro` sólo distingue dos casos: los ejes cambiados o no. El resto lo
        # cubren los espejos, y así no hay que acertar ningún convenio.
        self.giro = giro
        self.espejo_x = espejo_x
        self.espejo_y = espejo_y

    # ── traducir ────────────────────────────────────────────────────────
    def a_vista(self, t: Toque) -> Punto:
        """El toque, en fracciones de lo que se ve (0..1)."""
        fx = t.x / max(1, self.ancho_panel)
        fy = t.y / max(1, self.alto_panel)
        if self.giro:
            fx, fy = fy, fx
        if self.espejo_x:
            fx = 1.0 - fx
        if self.espejo_y:
            fy = 1.0 - fy
        return Punto(min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy)))

    # ── guardar y recuperar ─────────────────────────────────────────────
    def como_dict(self) -> Dict[str, Any]:
        return {"ancho_panel": self.ancho_panel, "alto_panel": self.alto_panel,
                "ancho_vista": self.ancho_vista, "alto_vista": self.alto_vista,
                "giro": self.giro, "espejo_x": self.espejo_x,
                "espejo_y": self.espejo_y}

    @classmethod
    def desde_dict(cls, d: Dict[str, Any]) -> "Panel":
        return cls(int(d.get("ancho_panel", 1)), int(d.get("alto_panel", 1)),
                   int(d.get("ancho_vista", 1)), int(d.get("alto_vista", 1)),
                   int(d.get("giro", 0)), bool(d.get("espejo_x")),
                   bool(d.get("espejo_y")))

    def guardar(self, ruta: Path) -> None:
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(json.dumps(self.como_dict(), indent=1))
        except OSError as e:
            logger.warning("no pude guardar la calibración del panel: %s", e)

    @classmethod
    def cargar(cls, ruta: Path) -> Optional["Panel"]:
        try:
            return cls.desde_dict(json.loads(ruta.read_text()))
        except Exception:
            return None

    # ── calibrar ────────────────────────────────────────────────────────
    @classmethod
    def desde_dos_esquinas(cls, arriba_izq: Toque, abajo_der: Toque,
                           ancho_panel: int, alto_panel: int,
                           ancho_vista: int, alto_vista: int) -> "Panel":
        """Con dos toques basta, y no hay que creerse ningún convenio.

        Se le pide a Enzo que toque **la esquina de arriba a la izquierda de lo
        que él ve** y luego la de abajo a la derecha. Si entre esos dos toques
        el que más se movió fue el eje `y` del panel, es que los ejes están
        cambiados; y el sentido de cada uno sale del signo. Ya está: cuatro
        giros y sus espejos resueltos con aritmética de primero.
        """
        dx = abajo_der.x - arriba_izq.x
        dy = abajo_der.y - arriba_izq.y
        cambiados = abs(dy) < abs(dx)
        if cambiados:
            # El eje X del panel recorre la vertical de lo que se ve.
            return cls(ancho_panel, alto_panel, ancho_vista, alto_vista,
                       giro=1, espejo_x=dy < 0, espejo_y=dx < 0)
        return cls(ancho_panel, alto_panel, ancho_vista, alto_vista,
                   giro=0, espejo_x=dx < 0, espejo_y=dy < 0)


# ─────────────────────────────────────────────────────────────────────────
#  Escuchar los dedos
# ─────────────────────────────────────────────────────────────────────────

class EscuchaDeToques:
    """Apunta dónde toca Enzo, sin tocar nada.

    `getevent` saca los eventos del panel tal cual llegan del driver. Se
    escribe a un fichero de /sdcard —que ven los dos lados— y se lee desde
    aquí; leerlo por una tubería se queda bloqueado a la primera.

    ⚠️ Sólo se ven así los toques **de una persona**. Los que da Celestia con
    `input tap` no pasan por el driver (los inyecta más arriba, por
    InputManager), así que no se cuelan en lo aprendido. Es una suerte, porque
    si no aprendería de sí misma; pero también quiere decir que esto no sirve
    para comprobar sus propios toques.
    """

    SALIDA = f"{MOVIL_DIR}/toques.txt"

    # getevent -lt escribe una línea por evento:
    #   [   12345.678901] /dev/input/event2: EV_ABS ABS_MT_POSITION_X 000001f4
    _EVENTO = re.compile(
        r"\[\s*([0-9.]+)\]\s+\S+:\s+(\w+)\s+(\w+)\s+([0-9a-fA-F]+)")

    def __init__(self, mando: MandoAndroid, dispositivo: Optional[str] = None):
        self.mando = mando
        self.dispositivo = dispositivo
        self.leidas = 0

    def _panel(self) -> Optional[Dict[str, Any]]:
        return self.mando._buscar_pantalla_tactil()

    def empezar(self, segundos: int = 120) -> bool:
        """Deja `getevent` grabando. No estorba a quien está jugando."""
        panel = self._panel()
        dev = self.dispositivo or (panel or {}).get("dev")
        if not dev:
            logger.warning("no encuentro el panel táctil: no puedo mirar cómo juegas")
            return False
        self.dispositivo = dev
        self.leidas = 0
        try:
            Path(self.SALIDA).unlink()
        except OSError:
            pass
        # En segundo plano y con tope: si esta sesión muere, `getevent` no se
        # queda para siempre escribiendo en el móvil de Enzo.
        self.mando._sh(
            f"(timeout {int(segundos)} getevent -lt {dev} > {self.SALIDA} 2>&1 &) ",
            timeout=20)
        return True

    def nuevos(self) -> List[Toque]:
        """Los toques aparecidos desde la última vez que se preguntó."""
        try:
            with open(self.SALIDA, "r", errors="replace") as f:
                lineas = f.readlines()
        except OSError:
            return []
        pendientes = lineas[self.leidas:]
        self.leidas = len(lineas)
        return self._toques(pendientes)

    def _toques(self, lineas: Sequence[str]) -> List[Toque]:
        """De eventos sueltos a «aquí bajó un dedo».

        Un toque no es un evento: son varios (posición, identificador, y un
        `SYN_REPORT` que dice «ya está, aplícalo»). Lo que interesa es dónde
        bajó el dedo, así que se acumulan las posiciones y se cierra el toque
        en el primer SYN tras un identificador nuevo — no al levantarlo, que
        en un deslizamiento sería el final del recorrido y no el principio.
        """
        toques: List[Toque] = []
        x = y = None
        t = 0.0
        dedo_abajo = False
        for linea in lineas:
            m = self._EVENTO.search(linea)
            if not m:
                continue
            marca, _tipo, codigo, valor = m.groups()
            try:
                t = float(marca)
            except ValueError:
                pass
            if codigo == "ABS_MT_TRACKING_ID":
                # ffffffff es el dedo levantándose.
                dedo_abajo = valor.lower() not in ("ffffffff", "-1")
                if not dedo_abajo:
                    x = y = None
            elif codigo in ("ABS_MT_POSITION_X", "ABS_X"):
                x = int(valor, 16)
            elif codigo in ("ABS_MT_POSITION_Y", "ABS_Y"):
                y = int(valor, 16)
            elif codigo == "SYN_REPORT" and dedo_abajo and x is not None and y is not None:
                toques.append(Toque(t=t, x=x, y=y))
                # Un dedo apoyado sigue mandando posiciones mientras se
                # arrastra; sólo interesa la primera de cada apoyo.
                dedo_abajo = False
                x = y = None
        return toques

    def parar(self) -> None:
        self.mando._sh("pkill -f 'getevent -lt' 2>/dev/null; true", timeout=15)


# ─────────────────────────────────────────────────────────────────────────
#  Lo que se saca de mirar
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Control:
    """Un sitio que Enzo pulsa una y otra vez. O sea: un botón."""
    punto: Punto
    veces: int
    seguidos: int = 0        # cuántas veces lo machacó (dos toques en <400 ms)

    @property
    def machacado(self) -> bool:
        """Un botón que se pulsa en ráfaga es un botón de acción.

        Es la única señal que distingue «atacar» de «abrir el menú» sin
        entender el juego: nadie pulsa dos veces seguidas «ajustes».
        """
        return self.seguidos >= 2


class MapaDeControles:
    """Dónde tiene Enzo los botones, sacado de dónde pone los dedos.

    No hay que reconocer nada en la imagen: los botones de un juego de acción
    están fijos, así que los sitios que se repiten **son** los controles. El
    más machacado es atacar; eso no es una suposición sobre ZZZ, es lo que
    hace cualquiera en cualquier juego de acción.
    """

    # Dos toques a menos de esto son «el mismo botón». 6% de la pantalla es el
    # tamaño típico de un botón grande de juego móvil.
    JUNTOS = 0.06
    # Y dos toques al mismo sitio en menos de esto son «machacar».
    SEGUIDOS_S = 0.4

    def __init__(self) -> None:
        self.controles: List[Control] = []
        self._ultimo: Optional[Tuple[Punto, float]] = None

    def apunta(self, p: Punto, t: float) -> None:
        for c in self.controles:
            if abs(c.punto.x - p.x) < self.JUNTOS and abs(c.punto.y - p.y) < self.JUNTOS:
                # La posición se afina con la media: así el centro del botón
                # sale de todos los dedos, no del primero.
                n = c.veces
                c.punto = Punto((c.punto.x * n + p.x) / (n + 1),
                                (c.punto.y * n + p.y) / (n + 1))
                c.veces += 1
                if (self._ultimo and self._ultimo[0] is not None
                        and t - self._ultimo[1] < self.SEGUIDOS_S
                        and abs(self._ultimo[0].x - p.x) < self.JUNTOS
                        and abs(self._ultimo[0].y - p.y) < self.JUNTOS):
                    c.seguidos += 1
                break
        else:
            self.controles.append(Control(punto=p, veces=1))
        self._ultimo = (p, t)

    def principales(self, cuantos: int = 5) -> List[Control]:
        return sorted(self.controles, key=lambda c: -c.veces)[:cuantos]

    def boton_de_atacar(self) -> Optional[Control]:
        """El más machacado; y si ninguno lo fue, el más usado de la mitad
        derecha, que es donde están las acciones en un juego apaisado."""
        machacados = [c for c in self.controles if c.machacado]
        if machacados:
            return max(machacados, key=lambda c: c.seguidos)
        derecha = [c for c in self.controles if c.punto.x > 0.5]
        return max(derecha, key=lambda c: c.veces) if derecha else None

    def como_dict(self) -> Dict[str, Any]:
        return {"controles": [{"x": round(c.punto.x, 4), "y": round(c.punto.y, 4),
                               "veces": c.veces, "seguidos": c.seguidos}
                              for c in self.principales(12)]}


# ─────────────────────────────────────────────────────────────────────────
#  Mirar jugar
# ─────────────────────────────────────────────────────────────────────────

class Aprendiz:
    """Mira jugar a Enzo y apunta. No toca la pantalla en ningún momento."""

    # Cada cuánto se mira la pantalla mientras él juega. Capturar cuesta ~320
    # ms, así que esto es casi todo el rato mirando; no hace falta más, porque
    # lo que se quiere aprender es «en esta pantalla se toca aquí», y la
    # pantalla no cambia entre un fotograma y el siguiente.
    CADA_S = 0.4

    def __init__(self, mando: MandoAndroid, libro: LibroDeJugadas,
                 panel: Panel, objetivo: str = "zzz"):
        self.mando = mando
        self.libro = libro
        self.panel = panel
        self.objetivo = objetivo
        self.mapa = MapaDeControles()
        self.aprendidas = 0
        self.vistos = 0

    def mirar(self, segundos: int = 120,
              parar_si: Optional[Any] = None) -> Dict[str, Any]:
        """Mientras Enzo juega, apunta qué había y dónde tocó."""
        escucha = EscuchaDeToques(self.mando)
        if not escucha.empezar(segundos + 10):
            return {"error": "no encuentro el panel táctil: no puedo mirar cómo juegas"}
        t0 = time.time()
        # La pantalla ANTERIOR al toque es la que enseña algo: la de después ya
        # tiene el resultado, y aprenderla sería aprender la consecuencia.
        #
        # Se mira una vez ANTES de entrar al bucle, y no es un detalle: sin
        # esto el primer toque no tiene con qué aprenderse y se pierde. Da la
        # casualidad de que el primero es de los mejores —Enzo empieza a jugar
        # desde una pantalla parada, la más fácil de reconocer luego.
        cap0 = self.mando.ver(rapido=True)
        previa: Optional[Tuple] = (Retina.firma(cap0, ZONAS_HUELLA)
                                   if cap0 and cap0.crudo else None)
        try:
            while time.time() - t0 < segundos:
                # S68: a pelo esto reventaba la sesión entera si el freno
                # fallaba al comprobarse. `freno_pisado` lo cuenta como «para».
                if freno_pisado(parar_si):
                    break
                cap = self.mando.ver(rapido=True)
                huella = (Retina.firma(cap, ZONAS_HUELLA)
                          if cap and cap.crudo else None)
                for t in escucha.nuevos():
                    p = self.panel.a_vista(t)
                    self.vistos += 1
                    self.mapa.apunta(p, t.t)
                    if previa is not None:
                        self._aprende(previa, p)
                if huella is not None:
                    previa = huella
                time.sleep(self.CADA_S)
        finally:
            escucha.parar()
        return {"toques": self.vistos, "aprendidas": self.aprendidas,
                "segundos": round(time.time() - t0, 1),
                "controles": self.mapa.como_dict()["controles"]}

    def _aprende(self, huella: Tuple, p: Punto) -> None:
        """Guarda «en esta pantalla, él tocó aquí» como jugada del libro."""
        if self.libro.buscar(huella, self.objetivo):
            return
        self.libro.aprender(huella, self.objetivo,
                            Accion(tipo="tocar", punto=p,
                                   porque="se lo vi hacer a Enzo"))
        self.aprendidas += 1


def calibrar(mando: MandoAndroid, esperar_s: int = 30,
             ruta: Optional[Path] = None) -> Optional[Panel]:
    """Pide dos toques y deja el panel calibrado. Devuelve `None` si no llegan.

    Se hace una vez por orientación. Es lo que evita que todo lo aprendido
    salga girado sin que nada lo diga.
    """
    escucha = EscuchaDeToques(mando)
    panel_info = mando._buscar_pantalla_tactil() or {}
    if not escucha.empezar(esperar_s + 5):
        return None
    t0 = time.time()
    recogidos: List[Toque] = []
    while time.time() - t0 < esperar_s and len(recogidos) < 2:
        recogidos.extend(escucha.nuevos())
        time.sleep(0.3)
    escucha.parar()
    if len(recogidos) < 2:
        return None
    an_v, al_v = mando.resolucion_vista()
    panel = Panel.desde_dos_esquinas(
        recogidos[0], recogidos[-1],
        int(panel_info.get("max_x") or an_v), int(panel_info.get("max_y") or al_v),
        an_v, al_v)
    if ruta:
        panel.guardar(ruta)
    return panel


# ─────────────────────────────────────────────────────────────────────────
#  Para usarlo a mano
# ─────────────────────────────────────────────────────────────────────────

RUTA_PANEL = MEM_DIR / "jugador" / "panel.json"


def _mando() -> MandoAndroid:
    m = MandoAndroid()
    m._sh("true", timeout=20)          # encender el canal antes de nada
    return m


def calibrar_a_mano(segundos: int = 40) -> str:
    """Pide las dos esquinas y deja el panel calibrado."""
    mando = _mando()
    print("Abre el juego y ponlo como vayas a jugar (si va en horizontal, "
          "en horizontal).")
    print(f"Tienes {segundos} s. Toca DOS sitios, en este orden:")
    print("  1) la esquina de ARRIBA a la IZQUIERDA de lo que ves")
    print("  2) la esquina de ABAJO a la DERECHA")
    panel = calibrar(mando, esperar_s=segundos, ruta=RUTA_PANEL)
    if panel is None:
        return ("No me han llegado los dos toques. ¿Está Shizuku encendido y "
                "has tocado la pantalla?")
    d = panel.como_dict()
    return (f"Calibrado y guardado en {RUTA_PANEL}.\n"
            f"  ejes cambiados: {'sí' if d['giro'] else 'no'} · "
            f"espejo x: {d['espejo_x']} · espejo y: {d['espejo_y']}")


def mirar_jugar(segundos: int = 180, objetivo: str = "zzz") -> str:
    """Mira jugar a Enzo y aprende. No toca la pantalla."""
    mando = _mando()
    panel = Panel.cargar(RUTA_PANEL)
    if panel is None:
        return ("Primero hay que calibrar el panel, o todo lo que aprenda "
                "saldrá girado sin que nada lo diga:\n"
                "    python3 -m celestia_lib.aprendiz calibrar")
    libro = LibroDeJugadas(MEM_DIR / "jugador" / f"{objetivo}.json")
    ap = Aprendiz(mando, libro, panel, objetivo=objetivo)
    print(f"Mirando {segundos} s. Juega normal — no voy a tocar nada.")
    r = ap.mirar(segundos=segundos)
    if "error" in r:
        return r["error"]
    lineas = [f"He visto {r['toques']} toques en {r['segundos']} s y he "
              f"aprendido {r['aprendidas']} jugadas nuevas.",
              "Dónde pones los dedos:"]
    for c in r["controles"][:6]:
        marca = "  ← lo machacas: esto es atacar" if c["seguidos"] >= 2 else ""
        lineas.append(f"  ({c['x']:.2f}, {c['y']:.2f})  {c['veces']} veces{marca}")
    atacar = ap.mapa.boton_de_atacar()
    if atacar:
        lineas.append(f"Botón de atacar: ({atacar.punto.x:.2f}, "
                      f"{atacar.punto.y:.2f}) — es el que le paso al vigía.")
    return "\n".join(lineas)


RUTA_CONTROLES = MEM_DIR / "jugador" / "controles_{juego}.json"


def _jugador_montado(objetivo: str = "zzz"):
    """Un `Jugador` con vista y mando, sin ganas de jugar: para aprender."""
    import os
    from celestia_lib.jugador import (Cronometro, Jugador, LibroDeJugadas, Ojo,
                                      mirada_en_cadena, pensador_groq)
    for linea in open(ENV_FILE):
        if "=" in linea and not linea.strip().startswith("#"):
            k, _, v = linea.strip().partition("=")
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    clave = os.environ.get("GROQ_API_KEY", "")
    mando = _mando()
    mapa = MapaGuardado(Path(str(RUTA_CONTROLES).format(juego=objetivo)))
    j = Jugador(mando, pensador_groq(clave), ojo=Ojo(),
                libro=LibroDeJugadas(
                    MEM_DIR / "jugador" / f"{objetivo}.json"),
                crono=Cronometro(),
                mirar=mirada_en_cadena(clave,
                                       os.environ.get("OPENROUTER_API_KEY", ""),
                                       os.environ.get("GEMINI_API_KEY", ""),
                                       os.environ.get("MISTRAL_API_KEY", ""),
                                       os.environ.get("CEREBRAS_API_KEY", ""),
                                       os.environ.get("SAMBANOVA_API_KEY", "")),
                controles=mapa,
                # Lo que haya leído de cómo se juega. Sin esto, el aprendiz
                # jugaba a ciegas mientras el tutorial estaba guardado en disco.
                saber=_lo_leido(objetivo))
    return j, mapa


def _lo_leido(objetivo: str) -> str:
    """El tutorial guardado de ese juego, si lo hay. Nunca sale a la red."""
    if "zzz" not in objetivo.lower() and "zenless" not in objetivo.lower():
        return ""
    try:
        from celestia_lib.zzz import SaberZZZ
        return SaberZZZ().resumen_para_jugar()
    except Exception as e:
        logger.debug("no pude traer el tutorial: %s", e)
        return ""


def aprender_controles(objetivo: str = "zzz", comprobar: bool = True) -> str:
    """Descubre sola dónde están los controles de lo que tenga delante."""
    j, mapa = _jugador_montado(objetivo)
    r = DescubridorDeControles(j, mapa).descubrir(comprobar=comprobar)
    if "error" in r:
        return r["error"] + "\n" + mapa.resumen()
    lineas = [f"Lo que he encontrado (el juego se mueve solo un "
              f"{r['ruido'] * 100:.1f}%, lo he descontado):"]
    for nombre, d in r["controles"].items():
        marca = "✓ reaccionó al tocarlo" if d["comprobado"] else "✗ no hizo nada"
        lineas.append(f"  {nombre:10} ({d['x']:.2f}, {d['y']:.2f})  {marca}")
    lineas.append("")
    lineas.append(mapa.resumen())
    return "\n".join(lineas)




# ─────────────────────────────────────────────────────────────────────────
#  Aprender sola: descubrir los controles y comprobarlos tocando
# ─────────────────────────────────────────────────────────────────────────

# Enzo, 8 sep 2026: «quiero que aprenda las cosas por su cuenta, todo lo va a
# tener que aprender por su cuenta». O sea que lo de arriba —mirarle jugar— se
# queda como plan B, y el camino es este: que descubra ella dónde están los
# controles y **lo compruebe tocando**, que es lo que separa aprender de
# suponer.
#
# Los nombres son de mecánica de juego de acción, no de ZZZ: se pregunta por lo
# que se VE en la pantalla. Un catálogo de ZZZ escrito de memoria está prohibido
# en este proyecto, y con razón (ver `zzz.py`).
INSTRUCCION_CONTROLES = """Estás viendo la pantalla de un juego de acción en un móvil.

Dime a qué ALTURA está cada control, en fracción de la pantalla (0 = arriba del
todo, 1 = abajo del todo). Si no lo ves, pon 0.

Responde SOLO con un JSON, sin explicar nada:
{"atacar":0,"esquivar":0,"habilidad":0,"relevo":0,"definitiva":0,"interactuar":0,"correr":0,"joystick":0}
- "atacar": el botón grande de golpear, normalmente abajo a la derecha.
- "esquivar": el de apartarse o rodar.
- "habilidad": un botón redondo más pequeño al lado del de atacar.
- "relevo": los retratos de personaje que se pulsan para cambiar.
- "definitiva": el botón especial que solo se enciende cuando está cargado.
- "interactuar": el de hablar, abrir o recoger cuando hay algo delante. Sale
  FUERA del combate, moviéndose por el mundo.
- "correr": el de esprintar mientras se anda por el mundo.
- "joystick": el círculo de moverse, abajo a la IZQUIERDA.
No te inventes ninguno: si no está en la pantalla, 0."""

# Los de pelear y los de andar por el mundo no salen a la vez, y por eso se
# guardan separados: preguntar por los ocho en una pantalla de menú devolvía
# ocho ceros, y en una pelea devolvía «interactuar» donde había una habilidad.
# Enzo, 10 sep 2026: «los distintos tipos de botones que hay, diferenciado del
# combate como de la exploración».
CONTROLES_DE_COMBATE = ("atacar", "esquivar", "habilidad", "relevo", "definitiva")
CONTROLES_DE_MUNDO = ("interactuar", "correr", "joystick")


class MapaGuardado:
    """Los controles que ya descubrió, y si llegó a comprobarlos.

    Se guarda aparte del libro de jugadas porque no dicen lo mismo: el libro
    dice «en ESTA pantalla, esto funciona»; esto dice «en este juego, atacar
    está aquí», y sirve para todas las pantallas de combate.
    """

    def __init__(self, ruta: Path):
        self.ruta = ruta
        self.controles: Dict[str, Dict[str, Any]] = {}
        self.cargar()

    def cargar(self) -> None:
        try:
            self.controles = json.loads(self.ruta.read_text())
        except Exception:
            self.controles = {}

    def guardar(self) -> None:
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            self.ruta.write_text(json.dumps(self.controles, indent=1))
        except OSError as e:
            logger.warning("no pude guardar los controles: %s", e)

    def pon(self, nombre: str, p: Punto, comprobado: bool) -> None:
        self.controles[nombre] = {"x": round(p.x, 4), "y": round(p.y, 4),
                                  "comprobado": comprobado, "ts": time.time()}
        self.guardar()

    def punto(self, nombre: str) -> Optional[Punto]:
        d = self.controles.get(nombre)
        return Punto(float(d["x"]), float(d["y"])) if d else None

    def comprobados(self) -> List[str]:
        return [k for k, v in self.controles.items() if v.get("comprobado")]

    def resumen(self) -> str:
        if not self.controles:
            return "Todavía no sé dónde está ningún control de este juego."
        filas = []
        for nombre, d in self.controles.items():
            marca = "comprobado" if d.get("comprobado") else "sin comprobar"
            filas.append(f"  {nombre:10} ({d['x']:.2f}, {d['y']:.2f})  {marca}")
        return "Controles que he aprendido:\n" + "\n".join(filas)


class DescubridorDeControles:
    """Encuentra los botones sola: los ve, y luego los prueba.

    Las dos mitades cuentan, y la segunda es la que convierte esto en
    aprender. Que el modelo diga dónde está un botón es una opinión; **tocarlo
    y ver que la pantalla reacciona** es un hecho. Sin la comprobación esto
    sería otra vez creerse lo que un modelo dice de un juego, que es
    exactamente lo que salió mal en la sesión 64.

    Y se comprueba sin entender el juego: se mira, se toca, se vuelve a mirar.
    Si el botón hace algo, algo cambia. Un sitio muerto deja la pantalla igual
    — salvo por las animaciones, y por eso se compara contra lo que se mueve
    **sin tocar nada**, que es la línea base.
    """

    # Cuánto tiene que cambiar la pantalla, por encima de lo que ya cambia
    # sola, para creerse que el toque hizo algo.
    DE_MAS = 1.6

    # 🔴 Por debajo de esto, la pantalla está QUIETA, y una pelea no lo está
    # nunca. Sin esta comprobación el primer intento contra ZZZ cantó «¡PELEA!»
    # delante de un menú parado: la mirada dijo ver botones de combate donde no
    # los había, y como nada se movía, cualquier parpadeo al tocar pasaba por
    # «reaccionó». Un menú no se descarta porque el modelo lo llame combate —
    # se descarta porque no se mueve.
    QUIETA = 0.004

    # Y dos controles distintos no pueden estar en el mismo sitio. En ese mismo
    # intento, atacar, esquivar y habilidad salieron los tres en (0.125, 0.92):
    # eso no es haber encontrado tres botones, es haber fallado tres veces con
    # la misma respuesta.
    SEPARADOS = 0.04

    def __init__(self, jugador: Any, mapa: MapaGuardado, objetivo: str = "zzz"):
        # `jugador` es un `Jugador` ya montado: trae la vista, el ojo y el
        # mando. No se construye aquí para no duplicar la cadena de miradas.
        self.jugador = jugador
        self.mapa = mapa
        self.objetivo = objetivo

    # ── 1) verlos ───────────────────────────────────────────────────────
    def proponer(self, cap: Captura) -> Dict[str, float]:
        """Qué controles ve y a qué altura. Sólo alturas: la `x` no la acierta.

        Es el reparto que ya está medido en `jugador.py`: el modelo acierta la
        altura con 8 px de error y falla la horizontal por cientos. Así que
        aquí sólo se le pregunta lo que sabe contestar.
        """
        mirar = getattr(self.jugador, "mirar", None)
        if not mirar or not cap.png:
            return {}
        bruto = mirar(cap.png, INSTRUCCION_CONTROLES) or ""
        m = re.search(r"\{.*\}", bruto, re.S)
        if not m:
            logger.info("la vista no dijo dónde están los controles: %r", bruto[:80])
            return {}
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
        alturas: Dict[str, float] = {}
        for nombre in CONTROLES_DE_COMBATE + CONTROLES_DE_MUNDO:
            try:
                y = float(d.get(nombre) or 0)
            except (TypeError, ValueError):
                continue
            if 0.02 < y < 0.99:
                alturas[nombre] = y
        return alturas

    # ── 2) situarlos ────────────────────────────────────────────────────
    def situar(self, cap: Captura, nombre: str, y: float) -> Optional[Punto]:
        """La `x` la pone la franja numerada, que es lo que sí funciona."""
        punto, _palabras = self.jugador._localizar(
            cap, f"el botón de {nombre}", y, sin_fondo=True)
        return punto

    # ── 3) comprobarlos ─────────────────────────────────────────────────
    @staticmethod
    def _distancia(a: Sequence, b: Sequence) -> float:
        return sum(Retina.distancia(x, y) for x, y in zip(a, b)) / max(1, len(a))

    def _cuanto_se_mueve_sola(self, vueltas: int = 2) -> float:
        """Cuánto cambia la pantalla SIN tocar nada. La línea base.

        Un juego nunca está quieto —animaciones, luces, el pelo del personaje—,
        así que «la pantalla cambió» no prueba nada por sí solo. Esto mide el
        ruido para poder descontarlo.
        """
        cambios = []
        anterior = None
        for _ in range(vueltas + 1):
            cap = self.jugador.mando.ver(rapido=True)
            if not cap or not cap.crudo:
                continue
            firma = Retina.firma(cap, ZONAS_HUELLA)
            if anterior is not None:
                cambios.append(self._distancia(anterior, firma))
            anterior = firma
            time.sleep(0.3)
        return (sum(cambios) / len(cambios)) if cambios else 0.0

    # Cuánto más tiene que moverse lo que hay ALREDEDOR del botón que el
    # resto de la pantalla, para creerse que fue el toque y no el juego.
    LOCAL_DE_MAS = 1.5

    @staticmethod
    def _cerca_y_lejos(antes: Sequence, despues: Sequence,
                       p: Punto) -> Tuple[float, float]:
        """Cuánto cambió junto al dedo, y cuánto en el resto de la pantalla."""
        cerca, lejos = [], []
        for zona, a, b in zip(ZONAS_HUELLA, antes, despues):
            c = zona.centro()
            d = Retina.distancia(a, b)
            if abs(c.x - p.x) <= 0.26 and abs(c.y - p.y) <= 0.20:
                cerca.append(d)
            else:
                lejos.append(d)
        return ((sum(cerca) / len(cerca)) if cerca else 0.0,
                (sum(lejos) / len(lejos)) if lejos else 0.0)

    def comprobar(self, nombre: str, p: Punto, ruido: float) -> bool:
        """Toca el botón y mira si reacciona **eso**, no la pantalla entera.

        🔴 La primera versión miraba si cambiaba la pantalla, y en una pelea
        eso lo cumple cualquier toque: el juego se mueve solo todo el rato. Con
        ZZZ delante dio por bueno un «esquivar» en lo alto de la pantalla,
        donde no hay ningún botón. Comprobar contra el fondo no comprueba nada.

        Lo que sí distingue: un botón que responde se ilumina o se hunde **ahí
        donde está el dedo**, mientras que el ruido del juego mueve toda la
        escena por igual. Así que se comparan las dos cosas — lo que cambia
        junto al punto contra lo que cambia lejos— y sólo cuenta si lo de
        cerca destaca. Un toque al aire da los dos números parecidos.
        """
        antes = self.jugador.mando.ver(rapido=True)
        if not antes or not antes.crudo:
            return False
        firma_antes = Retina.firma(antes, ZONAS_HUELLA)
        self.jugador.mando.tocar(p)
        time.sleep(0.35)
        despues = self.jugador.mando.ver(rapido=True)
        if not despues or not despues.crudo:
            return False
        firma_despues = Retina.firma(despues, ZONAS_HUELLA)
        cerca, lejos = self._cerca_y_lejos(firma_antes, firma_despues, p)
        sirve = (cerca > max(0.01, ruido * self.DE_MAS) and
                 cerca > lejos * self.LOCAL_DE_MAS)
        logger.info("«%s» en (%.2f, %.2f): junto al dedo %.3f, lejos %.3f "
                    "(ruido %.3f) → %s", nombre, p.x, p.y, cerca, lejos, ruido,
                    "sirve" if sirve else "nada")
        return sirve

    # ── todo junto ──────────────────────────────────────────────────────
    def descubrir(self, comprobar: bool = True) -> Dict[str, Any]:
        """Mira la pantalla, propone controles, los sitúa y los prueba."""
        # 🔴 Antes de mirar y, sobre todo, antes de TOCAR: ¿está el juego
        # delante? Sesión 66, visto en una captura: ZZZ se había ido al fondo y
        # esto se puso a analizar **la terminal de Termux con el teclado
        # abierto** — y el siguiente paso era tocarla, que es la terminal donde
        # corre la sesión. Tercera vez que aparece el mismo daño en el proyecto
        # (S63 con TikTok, S65 con Termux flotante, y esto). Los toques a
        # ciegas no dan error: tocan y se callan.
        delante = getattr(self.jugador, "_poner_el_juego_delante", None)
        if callable(delante):
            puedo, apunte = delante(self.objetivo)
            if not puedo:
                return {"error": f"no estoy donde se juega: {apunte}"}
        cap = self.jugador.mando.ver()
        if not cap:
            return {"error": "no consigo ver la pantalla"}
        alturas = self.proponer(cap)
        if not alturas:
            return {"error": "no veo controles de combate en esta pantalla; "
                             "esto hay que hacerlo con una pelea delante"}
        ruido = self._cuanto_se_mueve_sola() if comprobar else 0.0
        # ⚠️ Una pantalla quieta descarta un COMBATE, pero no el mundo: por el
        # mapa se anda con el personaje parado y ahí no se mueve nada hasta que
        # tocas. Así que el freno sólo vale cuando lo que se busca son botones
        # de pelea. Enzo, 10 sep 2026: «cuando el juego tiene mapa donde no hay
        # combate y es más interactuar, moverse por el mapa».
        de_combate = [n for n in alturas if n in CONTROLES_DE_COMBATE]
        de_mundo = [n for n in alturas if n in CONTROLES_DE_MUNDO]
        if comprobar and ruido < self.QUIETA and de_combate and not de_mundo:
            return {"error": "la pantalla no se mueve, así que esto no es una "
                             "pelea aunque lo parezca; no me pongo a tocar botones "
                             "aquí", "ruido": round(ruido, 4)}
        encontrados: Dict[str, Any] = {}
        puestos: List[Punto] = []
        for nombre, y in alturas.items():
            punto = self.situar(cap, nombre, y)
            if punto is None:
                continue
            repetido = any(abs(q.x - punto.x) < self.SEPARADOS and
                           abs(q.y - punto.y) < self.SEPARADOS for q in puestos)
            if repetido:
                logger.info("«%s» sale en el mismo sitio que otro control "
                            "(%.2f, %.2f): no me lo creo", nombre, punto.x, punto.y)
                continue
            puestos.append(punto)
            ok = self.comprobar(nombre, punto, ruido) if comprobar else False
            self.mapa.pon(nombre, punto, ok)
            encontrados[nombre] = {"x": round(punto.x, 3), "y": round(punto.y, 3),
                                   "comprobado": ok}
        if not encontrados:
            return {"error": "los controles que creí ver caían todos en el mismo "
                             "sitio: eso no es haberlos encontrado",
                    "ruido": round(ruido, 4)}
        return {"controles": encontrados, "ruido": round(ruido, 4)}


if __name__ == "__main__":
    import sys as _sys
    _modo = _sys.argv[1] if len(_sys.argv) > 1 else "controles"
    _n = int(_sys.argv[2]) if len(_sys.argv) > 2 and _sys.argv[2].isdigit() else 0
    if _modo.startswith("cal"):
        print(calibrar_a_mano(_n or 40))
    elif _modo.startswith("mir"):
        print(mirar_jugar(_n or 180))
    else:
        print(aprender_controles())
