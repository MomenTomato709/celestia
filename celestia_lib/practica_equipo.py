"""Practicar un equipo en una pelea: su vuelta, mirando la interfaz.

Enzo, 10 sep 2026: «voy a poner un equipo mío y que aprenda cómo funciona», y
«tú la vas a ir viendo cómo aprende, y si no lo hace bien vas y lo arreglas».

Aquí se junta lo de ese día, cada pieza en lo suyo:

  · `equipo_zzz` — quién va, su papel y el orden de la vuelta. Criterio.
  · `hud_zzz`    — quién está dentro, si hay EX, si sale ASSIST. Colores.
  · `Vigia`      — el tramo de cada agente, DENTRO del móvil: machaca, pulsa el
                   EX cuando se enciende y el relevo cuando sale ASSIST.
  · este fichero — encadenar los tramos, comprobar que cada relevo entró de
                   verdad, medir la vuelta y ajustar los tiempos.

## Cómo aprende

Cada vuelta se mide —con los puntos del reto si se pueden leer; si no, con las
acciones que dispararon: EX, asistencias, definitivas, cadenas y relevos que
entraron— y el tiempo en el campo de UN agente se mueve dos segundos. Si la
vuelta siguiente rinde igual o más, se queda; si rinde menos, se deshace. Es
probar y quedarse con lo que funciona, con cada decisión escrita en el diario.

## Qué se practica (S73)

Enzo, 11 sep 2026: «que se aprenda cada equipo, cada variable, cada combo, cada
combinación, los tiempos, los esquives, cada pasiva». Con las fichas de
`habilidades_zzz`, cada vuelta practica UNA cosa, por turno:

  · esquiva   — cancelar la cadena básica con la esquiva a distintas alturas: la
                que coincide con un golpe sale perfecta y se contraataca (así se
                practican también los golpes que no avisan con destello);
  · combos    — todos los combos del agente que está dentro, uno tras otro, y lo
                que sale de cada uno se apunta (`CombosAprendidos`);
  · rotacion  — sólo los mejores combos de cada uno, y aquí —y sólo aquí— se
                prueban los tiempos del relevo, para comparar vueltas iguales.

Las esperas desde el destello se aprenden en todas: los destellos salen solos.

⚠️ Dos cosas que no se pueden hacer a ciegas, y por eso aquí se mira:

  · Un ASSIST mete al siguiente agente A MITAD de tramo. Después de cada tramo
    se reconoce quién está dentro y la vuelta sigue desde ahí.
  · Un relevo pulsado durante una animación no entra. Se comprueba en el
    retrato; si dos seguidos no entran, se para en vez de machacar a ciegas.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from celestia_lib import hud_zzz as hud
from celestia_lib.equipo_zzz import Rotacion
from celestia_lib.habilidades_zzz import Combo, pasos_validos
from celestia_lib.jugador import Punto, escribe_apunte, freno_pisado
from celestia_lib.reflejo_zzz import Ajustes, EsperasAprendidas, leer_nota
from celestia_lib.paths import LOG_DIR, MEM_DIR

logger = logging.getLogger("celestia_v1")

ATACAR = Punto(*hud.PUNTO_ATACAR)
RELEVO = Punto(*hud.PUNTO_RELEVO)
EX = Punto(*hud.PUNTO_EX)
DEFINITIVA = Punto(*hud.PUNTO_DEFINITIVA)
# Las reglas miran el ANILLO —el centro del icono es oscuro encendido o
# apagado— y tocan el centro. 27 px a la derecha, medido: s 100-205 / 0-3.
EX_ANILLO = Punto(hud.PUNTO_EX[0] + 27 / 2412, hud.PUNTO_EX[1])
DEFINITIVA_ANILLO = Punto(hud.PUNTO_DEFINITIVA[0] + 27 / 2412, hud.PUNTO_DEFINITIVA[1])
GUARDA = (ATACAR, 18)

# 🔴 22 sep: con tope en 16 s el aprendizaje llevó los turnos a 16/13/14 s
# (Yanagi/Astra/Miyabi) empujado por el juez de la barra, que con un enemigo
# pequeño la pierde de vista el 65 % del rato. Enzo releva cada 2-3 s, el
# gameplay de este mismo equipo deja 5 s al principal y 1 s a cada apoyo, y la
# guía de Astra dice «especial, asistencia rápida y fuera». Un turno de más de
# 8 s no es una rotación: es jugar con una sola.
SEG_MIN = 1.0
SEG_MAX = 8.0
PASO = 1.0
# En las vueltas de esquivas y de combos se PRACTICA: un apoyo con turno de 2 s
# no tendría sitio ni para un combo. Ahí el tramo dura al menos esto.
SEG_PRACTICA = 6.0
# Un turno así de corto es entrar, soltar la EX y salir (ver combos_del_tramo).
TURNO_DE_ENTRAR_Y_SALIR = 2.0

# 🔴 Una vuelta sola NO sirve para decidir. Primera práctica real (10 sep 2026),
# misma configuración en las cuatro: 285, 266, 705 y 629 puntos por minuto,
# según cómo atacaran los enemigos. Con una vuelta por lado se deshizo «Lucia
# 5 s» por 629 frente a 705, que es ruido. Así que cada configuración se mide
# varias vueltas y sólo se decide si la diferencia pasa de un margen.
MIN_MUESTRAS = 2
MARGEN = 0.10

# Cada cuánto se despierta la pantalla durante la práctica. El móvil la apaga a
# los 30 minutos sin toques REALES —los inyectados no cuentan (S67)— y con la
# pantalla apagada el vigía se queda ciego («INVALID_LAYER_STACK») y los toques
# caen al aire. Tras la primera práctica el móvil estaba dormido y bloqueado.
LATIDO_S = 600.0
# Con estos toques en una vuelta, un marcador que no se mueve está parado, no es que no se pegue.
TOQUES_PARA_MARCADOR_PARADO = 20

# S73: cada vuelta practica una cosa, por turno (ver el principio del fichero).
FOCOS = ("esquiva", "combos", "rotacion")
# La vuelta de esquivas: la esquiva a distintas alturas de la cadena básica. «d a»
# es la carrerilla; las demás cancelan la cadena tras el 1.º, 2.º o 3.er golpe.
COMBOS_DE_ESQUIVA = (("carrerilla", "d a"), ("esquiva tras el golpe 1", "a d a"),
                     ("esquiva tras el golpe 2", "a a d a"), ("esquiva tras el golpe 3", "a a a d a"))
# Cuántos combos de cada agente entran en una vuelta de rotación.
COMBOS_EN_ROTACION = 3
# Lo que dura el turno de un agente cuando se juega a ganar: sus combos
# más largos rondan los 6 s, así que con 24 s caben tres o cuatro enteros.
TRAMO_A_POR_TODAS_S = 24.0     # tope: nadie se queda más de esto en el campo
MIN_TRAMO_S = 2.5              # menos que esto no cabe ni la jugada más corta
MARGEN_RELEVO_S = 0.8          # lo que tarda el relevo en entrar
RITMO_A_POR_TODAS = 140


# El marcador de estilo se para en 3000 («MAXIMUM»): desde ahí no puede subir,
# así que ese rato no dice nada ni a favor ni en contra.
TOPE_MARCADOR = 3000
# Parte de la vuelta que tiene que quedar medida con el marcador para fiarse de él.
MARCADOR_CUBRE_MIN = 0.4
# Una lectura más vieja que esto ya no sirve de referencia: en ese rato el
# marcador ha podido caer y volver a subir sin que se viera.
MARCADOR_VIEJO_S = 20.0


def subida_del_marcador(tramos: Sequence["Medida"], previo: Optional[int],
                        desde_previo: float = 0.0) -> Tuple[float, float]:
    """(puntos que subió el marcador, segundos en que se pudo medir).

    22 sep 2026. El marcador de arriba a la izquierda es la nota que pone el
    propio juego: sube con cada golpe, más con anomalías, asistencias y
    cadenas, y baja si te dan o te paras. No es un contador —en el vídeo de
    Enzo pasa de 1648 a 287 y a 2124 en 9 s—, así que lo que se mide es cuánto
    SUBE: Enzo lo lleva de 81 a 3000 en 92 s (~1900 por minuto) y Celestia, el
    mismo día, de 86 a 957 en 57 s. Es el juez que siempre está a la vista: la
    barra del enemigo, con uno pequeño, se ve un 35 % del rato.

    Cada tramo deja su lectura al acabar. Entre dos lecturas buenas lo que
    subió cuenta, lo que bajó es cero (se perdió nota) y el tiempo cuenta en los
    dos casos. Una lectura que falta no rompe nada: se mide desde la última
    buena (también si era de la vuelta anterior: `desde_previo` es lo que
    llevaba sin leerse), salvo que sea de hace más de `MARCADOR_VIEJO_S`.
    Desde el tope (3000) no se puede subir, así que ese rato no se mide.
    """
    subida = medido = 0.0
    antes = previo
    desde = float(desde_previo)      # segundos desde la última lectura buena
    for t in tramos:
        desde += _duracion_real(t)
        if t.marcador is None:
            continue
        if antes is not None and antes < TOPE_MARCADOR and desde <= MARCADOR_VIEJO_S:
            subida += max(0, t.marcador - antes)
            medido += desde
        antes, desde = t.marcador, 0.0
    return subida, medido


def _duracion_real(t: "Medida") -> float:
    """Lo que pasa entre la lectura de un tramo y la del siguiente."""
    return float(t.segundos) + float(t.arranque_s or 0) + float(t.relevo_s or 0)


def ultima_lectura(tramos: Sequence["Medida"], previo: Optional[int],
                   desde_previo: float = 0.0) -> Tuple[Optional[int], float]:
    """(última lectura buena, segundos desde ella) al acabar estos tramos.

    Codex, 22 sep: si la lectura del último tramo fallaba, la vuelta siguiente
    empezaba sin referencia y su primer tramo no se medía; con pocas lecturas
    eso bajaba la vuelta del 40 % y el juez saltaba de «estilo» a «daño», y dos
    vueltas medidas con cosas distintas no se pueden comparar.
    """
    antes, desde = previo, float(desde_previo)
    for t in tramos:
        desde += _duracion_real(t)
        if t.marcador is not None:
            antes, desde = t.marcador, 0.0
    return antes, desde


# Medio segundo sin hacer nada ya es estar plantado delante del enemigo: el
# mismo listón que usa el reflejo (HUECO_LARGO_MS en reflejo_zzz.c).
HUECO_LARGO_MS = 500
# Lo que ocupa un toque suelto (TOQUE_MS en reflejo_zzz.c).
TOQUE_MS = 35
# Lo que cuesta un giro de cámara buscando (GIRO_MS en buscar_enemigo_zzz).
GIRO_MS = 150

# Lo que imprime el reflejo, siempre con su milisegundo delante.
_EVENTO = re.compile(r"^(\w+) (\d+)(.*)$", re.M)


def _es_relevo(tipo: str, resto: str) -> bool:
    return tipo == "toque" and resto.startswith("relevo")


def medidas_por_turno(salida: str, plan: Sequence[Tuple[str, str]]) -> List["Medida"]:
    """Una Medida por turno jugado, repartiendo lo que imprimió el reflejo.

    22 sep 2026. Con la rotación dentro del binario (`--turnos`) ya no hay un
    tramo por llamada: en una sola salida vienen todos los turnos seguidos. Como
    cada línea lleva su milisegundo y cada cambio imprime «turno ms i», se puede
    repartir por agente TODO lo que se imprime —toques, defensas, cadenas y la
    vida del enemigo— sin contadores nuevos en C.

    `plan` es [(agente, pasos)] en el orden del relevo; el turno i es plan[i].
    """
    if not plan:
        return []
    eventos: List[Tuple[int, str, str]] = []
    cambios: List[Tuple[int, int]] = []
    for tipo, ms, resto in _EVENTO.findall(salida or ""):
        if tipo == "turno":
            cambios.append((int(ms), int(resto.strip() or 0)))
        else:
            eventos.append((int(ms), tipo, resto.strip()))
    if not cambios:
        return []
    fin_todo = max([ms for ms, _t, _r in eventos] + [cambios[-1][0]])
    fuera: List[Medida] = []
    for k, (ini, idx) in enumerate(cambios):
        # El último turno llega hasta el final, con su última línea dentro.
        fin = cambios[k + 1][0] if k + 1 < len(cambios) else fin_todo + 1
        agente, pasos = plan[idx % len(plan)]
        # El relevo del cambio de turno se imprime con el MISMO milisegundo que
        # la línea «turno» siguiente (Codex, 22 sep): es del que SALE, no del que
        # entra. Sin esto, el turno saliente nunca constaba como relevado y el
        # toque se le apuntaba al agente equivocado.
        dentro = [(ms, tipo, resto) for ms, tipo, resto in eventos
                  if (ini < ms < fin) or (ms == ini and not _es_relevo(tipo, resto))
                  or (ms == fin and _es_relevo(tipo, resto) and k + 1 < len(cambios))]
        vida = [(ms, float(v), int(n)) for ms, tipo, resto in dentro if tipo == "enemigo"
                for v, n in re.findall(r"vida=([-\d.]+) vistos=(\d+)", resto)]
        try:
            from celestia_lib.vida_enemigo_zzz import vida_quitada
            quitado, muertes = vida_quitada([(ms, v, n) for ms, v, n in vida if v >= 0])
        except Exception as e:                      # pragma: no cover - juez roto
            logger.warning("práctica: sin juez de vida por turnos (%s)", e)
            quitado, muertes = 0.0, 0
        def cuantos(tipo: str, que: str = "") -> int:
            return sum(1 for _ms, t, r in dentro if t == tipo and (not que or r.startswith(que)))
        # Con la rotación dentro, estos tres los medía el binario POR LLAMADA y
        # salían a cero en cada turno: se calculan aquí del propio rastro.
        # Cada acción ocupa un rato: un toque, TOQUE_MS; un sostenido, lo suyo.
        ocupa: List[Tuple[int, int]] = []
        for ms, t, r in dentro:
            if t == "toque":
                ocupa.append((ms, ms + TOQUE_MS))
            elif t == "mantener":
                dura = int(re.findall(r"(\d+)$", r)[0]) if re.findall(r"(\d+)$", r) else 0
                ocupa.append((ms, ms + dura))
        tocando_ms = sum(b - a for a, b in ocupa)
        # Parado: los huecos de más de medio segundo sin hacer nada (como en C).
        parado_ms, ultimo = 0, ini
        for a0, b0 in sorted(ocupa) + [(fin, fin)]:
            if a0 - ultimo > HUECO_LARGO_MS:
                parado_ms += a0 - ultimo
            ultimo = max(ultimo, b0)
        buscando_ms = sum(int(x) for _ms, t, r in dentro if t == "andar"
                          for x in re.findall(r"ms=(\d+)", r))
        buscando_ms += GIRO_MS * cuantos("camara")
        # Una «especial» es EX si la jugada del agente la pide; si no, es su especial normal.
        especiales = cuantos("toque", "especial") + cuantos("mantener", "especial")
        m = Medida(agente, round((fin - ini) / 1000.0, 2),
                   toques=cuantos("toque", "atacar") + cuantos("mantener", "atacar") + especiales,
                   ex=especiales if "E" in (pasos or "").split() else 0,
                   especiales=especiales,
                   # Un relevo pegado al final del turno es el cambio; los otros
                   # son asistencias (el reflejo releva en el destello dorado).
                   assist=sum(1 for ms, t, r in dentro
                              if t == "toque" and r.startswith("relevo") and ms < fin - 150),
                   definitiva=cuantos("toque", "definitiva"),
                   avisos=cuantos("aviso"), cadenas=cuantos("cadena"),
                   perfectas=cuantos("perfecta"), fallidas=cuantos("fallida"),
                   saltadas=cuantos("defensa_saltada"),
                   combos=cuantos("combo_hecho"), cortados=cuantos("combo_cortado"),
                   barras=round(float(quitado), 3), muertes=int(muertes),
                   visto=round(min(1.0, sum(n for _ms, _v, n in vida)
                                   / max(1.0, 29 * (fin - ini) / 1000.0)), 3),
                   tocando_s=round(tocando_ms / 1000.0, 1),
                   parado_s=round(parado_ms / 1000.0, 1),
                   buscando_s=round(buscando_ms / 1000.0, 1),
                   foco="rotacion")
        m.relevo = any(t == "toque" and r.startswith("relevo") and ms >= fin - 150
                       for ms, t, r in dentro)
        m.entra = plan[(idx + 1) % len(plan)][0] if m.relevo else ""
        fuera.append(m)
    return fuera


def leer_nota_del_vigia(salida: str, nombres: Sequence[str]) -> Dict[str, int]:
    """«toques=180 vueltas=90 reglas=3,1» → {"toques": 180, nombres[0]: 3, …}."""
    fuera: Dict[str, int] = {"toques": 0}
    m = re.search(r"toques=(\d+)", salida or "")
    if m:
        fuera["toques"] = int(m.group(1))
    r = re.search(r"reglas=([\d,]*)", salida or "")
    cuentas = [int(x) for x in r.group(1).split(",") if x.isdigit()] if r else []
    for nombre, n in zip(nombres, cuentas):
        fuera[nombre] = fuera.get(nombre, 0) + n
    return fuera


def duracion_ms(pasos: str, ritmo: int) -> int:
    """Lo que tarda un combo en jugarse, paso a paso, como lo juega el binario."""
    total = 0
    for t in (pasos or "").split():
        if t.startswith("."):
            total += int(t[1:])
        elif ":" in t:
            total += int(t.split(":")[1]) + 80
        else:
            total += ritmo
    return total + ritmo


CRUDO = LOG_DIR / "practica_crudo.log"
# Un tramo son unas 300 líneas; con 200 tramos el fichero ronda los 6 MB. Pasado
# eso se empieza de cero: sirve para mirar lo de ayer, no para guardar la historia.
CRUDO_MAX_MB = 8


def guarda_el_crudo(salida: str, agente: str, ritmo: int, ruta: Optional[Path] = None) -> None:
    """La línea de resumen del reflejo, tal cual la escribió, por tramo.

    Falta hacía: el 19 sep aparecieron 27.958 «cortes» de un combo y no se pudo
    saber de dónde salían porque la práctica no guardaba nada de lo que dice el
    binario — sólo lo ya digerido. Sin el crudo no se puede contar cuántas
    líneas `combo_cortado` hubo de verdad ni de qué tramo salieron.

    Se guarda el resumen (una línea), no los miles de sucesos: es lo que hace
    falta para cazar un contador y cabe en cualquier sitio.
    """
    linea = next((l for l in (salida or "").splitlines() if l.startswith("resumen ")), "")
    if not linea:
        return
    ruta = ruta or CRUDO                  # se mira al llamar: los tests lo cambian
    try:
        if ruta.exists() and ruta.stat().st_size > CRUDO_MAX_MB * 1048576:
            ruta.write_text("", encoding="utf-8")
    except OSError:
        pass                              # en este PRoot `stat` falla a ratos; se sigue
    sellos = f"{datetime.datetime.now().isoformat(timespec='seconds')} {agente} ritmo={ritmo} "
    escribe_apunte(ruta, sellos + linea)


def rinde_el_recurso(texto: str, vida: Sequence[Tuple[int, float, int]],
                     ventana_ms: int = 4000, ritmo_base: float = 0.0) -> Dict[str, Tuple[int, float]]:
    """Cuánto daño sale de cada definitiva y de cada EX: (veces, barras por uso).

    Enzo, 19 sep 2026: «a veces es mejor esperar para luego hacer más daño, es
    lo que pide el kit de algunos personajes». Por eso el juez del recurso no
    puede ser «¿la usaste?» —eso empuja a soltarla en cuanto brilla— sino
    cuánto rinde cada vez que se usa. Si esperar al aturdimiento da el doble,
    sale solo en este número y nadie tiene que decidirlo a mano.

    Se mira la vida del enemigo justo antes del toque y `ventana_ms` después:
    el daño de una definitiva no es instantáneo, tarda su animación.

    Con `ritmo_base` (barras por segundo que ese agente hace normalmente) cada
    uso se clasifica además en **bien / mal / no lo sé**, que es el juez que
    pidió Enzo: «quiero otro de cuando se usa bien y cuando no». Un gasto está
    bien usado si saca MÁS que lo que habría salido sin gastarlo —el daño
    normal de ese rato—, y eso no es una opinión ni un umbral inventado. Y las
    tres respuestas son tres: un uso que no se pudo medir (sin barra a la vista)
    no es un uso malo, se queda sin juzgar.
    """
    fuera: Dict[str, Tuple[int, float]] = {}
    if not vida:
        return fuera
    puntos = sorted((int(ms), float(v)) for ms, v, _n in vida)

    def vida_en(ms: int) -> Optional[float]:
        """La vida en ese instante, o None si nadie la vio cerca.

        Las muestras llegan una por segundo, así que valerse de una de hace más
        de segundo y medio ya es inventar. Sin esto, un gasto en el último
        segundo del tramo se comparaba consigo mismo —misma muestra antes y
        después—, daba cero daño y se apuntaba como MAL USADO cuando lo cierto
        es que no se pudo ver.
        """
        antes = [(t, v) for t, v in puntos if t <= ms]
        if not antes or ms - antes[-1][0] > 1500:
            return None
        return antes[-1][1]

    esperado = ritmo_base * ventana_ms / 1000.0
    for rotulo, patron in (("definitiva", r"^toque (\d+) definitiva$"),
                           ("ex", r"^toque (\d+) especial$")):
        usos, total, bien, sin_juzgar = 0, 0.0, 0, 0
        for m in re.finditer(patron, texto or "", re.M):
            ms = int(m.group(1))
            v0, v1 = vida_en(ms), vida_en(ms + ventana_ms)
            if v0 is None or v1 is None:
                sin_juzgar += 1     # sin barra a la vista no se juzga: no es un cero
                continue
            usos += 1
            saco = max(0.0, v0 - v1)
            total += saco
            if ritmo_base > 0 and saco > esperado:
                bien += 1
        if usos or sin_juzgar:
            fuera[rotulo] = (usos, round(total / usos, 4) if usos else 0.0)
            fuera[rotulo + "_bien"] = (bien, usos)
            if sin_juzgar:
                fuera[rotulo + "_sin_juzgar"] = (sin_juzgar, 0)
    return fuera


def cortes_creibles(resumen: Dict[str, Any], ritmo: int) -> List[int]:
    """Los cortes de combo del tramo, con los imposibles fuera.

    Un corte necesita que antes se diera un paso del combo, y entre paso y paso
    pasa al menos `ritmo` ms. Así que en un tramo de N segundos no caben más de
    N·1000/ritmo cortes: lo que pase de ahí no es una medida, es basura.

    Hace falta porque el 19 sep salieron **41.882 cortes en una vuelta de tres
    tramos** y 27.958 en un combo de Miyabi con 546 hechos — a 140 ms de ritmo,
    el techo de esa vuelta eran unos 300. Y no es cosmético: la nota de cada
    combo lleva `hechos/(hechos+cortados)`, así que ese número hundía a 0,02 el
    combo bueno de la guía y lo dejaba por debajo de una esquiva suelta. De
    dónde salen esos cortes sigue sin saberse —en seco no se reproduce—, pero un
    dato imposible no se usa para decidir a qué se juega.
    """
    cortados = [int(x) for x in (resumen.get("combos_cortados") or [])]
    if not cortados:
        return []
    seg = float(resumen.get("segundos") or 0.0)
    if seg <= 0 or ritmo <= 0:
        return cortados                  # sin reloj no se puede juzgar: se deja pasar
    techo = int(seg * 1000.0 / ritmo) + 10
    if sum(cortados) <= techo:
        return cortados
    logger.warning("práctica: %d cortes en %.1f s con ritmo %d ms no caben (techo %d): los dejo fuera. "
                   "Crudo: %s", sum(cortados), seg, ritmo, techo, cortados)
    return [0] * len(cortados)


class CombosAprendidos:
    """Qué sale de cada combo de cada agente, y cuáles se quedan para la rotación.

    🔴 18 sep. La nota era `(3·cadenas + 0,2·hechos) / minutos · terminados`, y
    como `minutos = hechos · lo que dura el combo`, eso se simplifica a
    **12000 / duración**: NO medía daño, medía brevedad. Ganaba siempre el combo
    de un solo botón —«especial (e)» nota 50,1, «EX (E)» 45,4, «definitiva (U)»
    42,9— y perdía la cadena de verdad de Miyabi, nota 12,1. De ahí que Enzo
    viera «ataque especial ataque especial todo el rato aporreando»: la fórmula
    le estaba PIDIENDO que aporreara. Ninguno de los combos de la ficha de Miyabi
    (`E a a a a:2500`…) entró nunca en una rotación.

    Ahora la nota es **daño por minuto**, con el juez que ya se mide en cada
    tramo: la vida que se le quita al enemigo, repartida entre los combos según
    el tiempo que ocupó cada uno. Un tramo donde no se ve la barra no cuenta ni a
    favor ni en contra. Y mientras no haya daño medido, la nota es `None`: sin
    juez se hace caso a la GUÍA (el orden de la ficha), nunca al botón más corto.
    """

    MIN_VECES = 3
    # Menos tiempo con juez que esto y el reparto es ruido, no una medida.
    MIN_MS_CON_JUEZ = 4000

    def __init__(self, ruta: Optional[Path] = None):
        self.ruta = ruta
        # agente → pasos → {"nombre", "hechos", "cortados", "cadenas", "ms"}
        self.datos: Dict[str, Dict[str, Dict[str, Any]]] = {}
        if ruta is not None and ruta.exists():
            try:
                self.datos = json.loads(ruta.read_text("utf-8"))
            except (OSError, ValueError) as e:
                logger.warning("práctica: no pude leer los combos aprendidos (%s)", e)

    def _de(self, agente: str, combo: Combo) -> Dict[str, Any]:
        d = self.datos.setdefault(agente, {}).setdefault(
            combo.pasos, {"nombre": combo.nombre, "hechos": 0, "cortados": 0, "cadenas": 0, "ms": 0,
                          "danio": 0.0, "ms_juez": 0})
        d.setdefault("danio", 0.0)          # fichas guardadas antes del 18 sep
        d.setdefault("ms_juez", 0)
        if combo.nombre:
            d["nombre"] = combo.nombre
        return d

    def apuntar(self, agente: str, combos: Sequence[Combo], resumen: Dict[str, Any], ritmo: int,
                danio: float = 0.0) -> None:
        """Los contadores de la nota van uno por combo, en el orden en que se mandaron.

        `danio` es lo que se le quitó al enemigo en ESTE tramo (barras de vida).
        Se reparte entre los combos según el tiempo que ocupó cada uno, que es lo
        más justo que se puede hacer sin un marcador por golpe: si un combo llenó
        la mitad del tramo, se lleva la mitad de lo que se quitó. Con `danio` a
        cero —la barra no se vio— el tramo no suma daño a nadie, pero los hechos
        y los cortes se siguen apuntando.
        """
        hechos = resumen.get("combos_hechos") or []
        cortados = cortes_creibles(resumen, ritmo)
        cadenas = resumen.get("cadenas_combo") or []
        reparto = [(hechos[i] if i < len(hechos) else 0) * duracion_ms(c.pasos, ritmo)
                   for i, c in enumerate(combos)]
        total_ms = sum(reparto)
        for i, c in enumerate(combos):
            d = self._de(agente, c)
            h = hechos[i] if i < len(hechos) else 0
            d["hechos"] += h
            d["cortados"] += cortados[i] if i < len(cortados) else 0
            d["cadenas"] += cadenas[i] if i < len(cadenas) else 0
            d["ms"] += reparto[i]
            if danio > 0 and total_ms > 0:
                d["danio"] += danio * reparto[i] / total_ms
                d["ms_juez"] += reparto[i]

    def nota(self, agente: str, pasos: str) -> Optional[float]:
        """Daño por minuto de este combo, o `None` si aún no hay juez que lo diga.

        `None` no es «malo»: es «no lo sé todavía», y `para_la_rotacion` lo
        traduce en hacer caso a la ficha. Es justo lo contrario de lo que pasaba
        antes, cuando la falta de medida se rellenaba premiando al más corto.
        """
        d = self.datos.get(agente, {}).get(pasos)
        if not d or d["hechos"] + d["cortados"] < self.MIN_VECES:
            return None
        if d.get("ms_juez", 0) < self.MIN_MS_CON_JUEZ or d.get("danio", 0.0) <= 0:
            return None
        minutos = d["ms_juez"] / 60000.0
        terminados = d["hechos"] / (d["hechos"] + d["cortados"])
        return d["danio"] / minutos * terminados

    @staticmethod
    def de_la_guia(c: Combo) -> bool:
        """¿Este combo lo escribió un tutorial, o lo compuse yo con la ficha?

        Lo que sale de un tutorial es una rotación que alguien juega de verdad;
        lo compuesto es una posibilidad. Con las dos sin medir, va delante la del
        tutorial: es lo que pidió Enzo cuando dijo que aprendiera viendo
        gameplay, no probando botones al azar.
        """
        return "guía" in (getattr(c, "porque", "") or "").lower()

    def para_la_rotacion(self, agente: str, combos: Sequence[Combo], n: int = COMBOS_EN_ROTACION) -> List[Combo]:
        """Los que más daño hacen; y lo aún no medido, con los del tutorial delante.

        🔴 18 sep: con 3 huecos de rotación, Yanagi salía con «cadena básica ·
        carrerilla · especial» y su combo propio —«Ruten tras el golpe 3»,
        `a a a e a a a e`, el que la guía escribe— quedaba FUERA por ir el cuarto
        en la ficha. Sin nota que los separe, manda de dónde viene el combo.
        """
        medidos = [(self.nota(agente, c.pasos), i, c) for i, c in enumerate(combos)]
        elegidos = [c for _n, _i, c in sorted((m for m in medidos if m[0] is not None),
                                              key=lambda m: (-m[0], m[1]))[:n]]
        sin_medir = [(0 if self.de_la_guia(c) else 1, i, c) for nota, i, c in medidos if nota is None]
        for _guia, _i, c in sorted(sin_medir, key=lambda x: (x[0], x[1])):
            if len(elegidos) >= n:
                break
            if c not in elegidos:
                elegidos.append(c)
        return elegidos

    def resumen(self, agente: str, tope: int = 5) -> str:
        filas = []
        for pasos, d in self.datos.get(agente, {}).items():
            n = self.nota(agente, pasos)
            filas.append((n if n is not None else -1.0,
                          f"{d.get('nombre') or pasos} ({pasos}): {d['hechos']} hechos, {d['cortados']} cortados, "
                          f"{d['cadenas']} cadenas, "
                          + (f"nota {n:.2f} barras/min" if n is not None else "sin juez todavía")))
        filas.sort(key=lambda f: -f[0])
        return " · ".join(t for _n, t in filas[:tope]) or "nada todavía"

    def guardar(self) -> None:
        if self.ruta is None:
            return
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps(self.datos, ensure_ascii=False, indent=1), "utf-8")


def rotacion_de_video(equipo: Sequence[str] = (), carpeta: Optional[Path] = None
                     ) -> Dict[str, Any]:
    """La rotacion aprendida de un gameplay: quien aguanta y quien entra y sale.

    Sesion 75. Enzo: «ella tambien puede ver las rotaciones desde los tutoriales».
    De un gameplay de SU equipo (Miyabi Yanagi Astra) salio esto: el principal se
    queda ~5,2 s y cada apoyo entra **1,0 s** —suelta su habilidad y sale—, con un
    relevo cada 5,6 s. Es lo que hace Enzo (135 relevos en 5,7 min) y lo contrario
    de los turnos largos que habia puestos.

    Devuelve {nombre corto: segundos}; el papel de principal va al que consta como
    tal en memoria/jugador/equipo_zzz.json.
    """
    import json as _json
    from celestia_lib.ver_tutorial_zzz import rotacion_ejecutable
    from celestia_lib.tiempos_combos_zzz import EQUIPO as RUTA_EQUIPO
    raiz = carpeta or MEM_DIR / "zzz"
    fichas = sorted(raiz.glob("aprendido_video_*.json"))
    if not fichas:
        return {}
    try:
        d = _json.loads(fichas[0].read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    r = rotacion_ejecutable((d.get("gameplay") or {}).get("rotacion") or [])
    if not r:
        return {}
    try:
        principal = str(_json.loads(RUTA_EQUIPO.read_text("utf-8")).get("principal") or "")
    except (OSError, ValueError):
        principal = ""
    corto = principal.split()[-1] if principal else ""
    largo = float(r["turno_s"].get(r["principal"], 5.0))
    otros = [float(v) for k, v in r["turno_s"].items() if k != r["principal"]]
    breve = min(otros) if otros else 1.0
    # Cada agente se apunta con TODAS las partes de su nombre: la practica llama
    # «Astra» a quien el catalogo llama «Astra Yao», y por coger solo el ultimo
    # trozo la rotacion no le llegaba a ella.
    fuera: Dict[str, Any] = {}
    for nombre in (equipo or ([principal] if principal else [])):
        es_principal = corto and corto in nombre.split()
        for parte in nombre.split():
            if len(parte) > 2:
                fuera[parte] = largo if es_principal else breve
    fuera["_de"] = fichas[0].name
    fuera["_relevo_cada_s"] = r.get("cada_s")
    return fuera


def defensas_saltadas_de(agente: str, diario: Optional[Path] = None,
                         ultimos: int = 12) -> Tuple[int, int]:
    """(defensas saltadas, destellos) de los ultimos tramos de ese agente.

    Sesion 75, medido el 17 sep con el enemigo atacando: Miyabi tuvo 17 destellos
    y **se salto 13** porque sus combos llevan `a:2500` —dedo sostenido 2,5 s para
    el ataque cargado— y con el dedo puesto el reflejo no puede defender. Nangong
    Yu, con combos sin mantener, se salto 0 de 14. El 18 % de perfectas no era un
    problema de timing: era que ni lo intentaba.
    """
    import json as _json
    ruta = diario or LOG_DIR / "practica_equipo_diario.jsonl"
    saltadas = destellos = 0
    try:
        filas = [l for l in ruta.read_text("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return 0, 0
    vistos = 0
    for linea in reversed(filas):
        try:
            fila = _json.loads(linea)
        except ValueError:
            continue
        tr = fila.get("tramo")
        if not tr or tr.get("agente") != agente:
            continue
        saltadas += int(tr.get("saltadas") or 0)
        destellos += int(tr.get("avisos") or 0)
        vistos += 1
        if vistos >= ultimos:
            break
    return saltadas, destellos


def duracion_combo(pasos: str, ritmo: int) -> float:
    """Lo que tarda en ejecutarse un combo, en segundos.

    Sesión 75, medido en la partida de Enzo: hace **135 relevos en 5,7 minutos**,
    o sea que entra, suelta su jugada y sale — turnos de 2 o 3 segundos. Los
    turnos de 24 s que le puse a Celestia para que le cupieran los combos van en
    dirección contraria: el turno tiene que durar LO QUE DURA LA JUGADA, ni más
    (se queda pegada pegando de más) ni menos (la corta a medias).

    Un paso normal cuesta `ritmo`; uno sostenido («a:2500»), lo que se mantenga.
    """
    total = 0.0
    for paso in str(pasos).split():
        if ":" in paso:
            _tecla, _, ms = paso.partition(":")
            total += int(ms) / 1000.0 + 0.10
        elif paso.startswith("."):
            total += int(paso[1:]) / 1000.0
        else:
            total += ritmo / 1000.0
    return round(total, 2)


@dataclass
class Medida:
    """Lo que pasó en el tramo de un agente."""
    agente: str
    segundos: float
    toques: int = 0
    ex: int = 0
    assist: int = 0
    definitiva: int = 0
    relevo: bool = False
    entra: str = ""
    nota: str = ""
    # Con el reflejo nativo: destellos vistos, defensas perfectas y fallidas.
    avisos: int = 0
    perfectas: int = 0
    fallidas: int = 0
    contraataques: int = 0
    cadenas: int = 0
    # Con combos (S73): qué se practicaba y cómo fueron.
    foco: str = ""
    combos: int = 0
    cortados: int = 0
    saltadas: int = 0
    # El juez de verdad (S75): vida del enemigo quitada en el tramo, sus muertes, y
    # cuánto de ese rato se fue en ir a buscarle. Hasta ahora la práctica contaba
    # cadenas y relevos —lo que se podía contar sin juez— y no el daño, que es lo
    # que Enzo quiere ver: «aprovechar todo el daño que puede hacer el equipo».
    barras: float = 0.0
    muertes: int = 0
    buscando_s: float = 0.0
    # Lo que NO es jugar, medido por partes (19 sep): entre tramo y tramo se iba
    # el 25 % del reloj —3,6 s de mediana, todos con relevo de por medio— y no
    # había forma de saber cuánto era arrancar el reflejo y cuánto confirmar el
    # relevo mirando la pantalla. Sin repartirlo no se puede atacar lo que pesa.
    arranque_s: float = 0.0     # lo que tarda el reflejo de más sobre el tramo pedido
    relevo_s: float = 0.0       # pulsar el relevo y verlo entrar
    # Más jueces (19 sep, Enzo): «con Astra Yao no va a estar nunca haciendo
    # daño y se contará como fallo, pero su rol es hacer que los demás hagan
    # más». Un apoyo no se mide por su daño ni por sus EX: el buff de Astra
    # (Idyllic Cadenza, que sube el daño de TODO el equipo) sale de su especial
    # normal, y mirando sólo `ex` daba 0,0 — parecía que no hacía nada cuando
    # llevaba 254 especiales. Y al revés: las EX que se intentan sin energía no
    # salen, y contarlas como hechas infla a quien no pega.
    especiales: int = 0
    ex_falladas: int = 0
    # Dentro del tramo: cuánto se pasa con el dedo en la pantalla y cuánto sin
    # tocar nada. El «reloj jugando» sólo miraba los huecos ENTRE tramos, y
    # dentro había otro agujero que no miraba nadie.
    tocando_s: float = 0.0
    quieto_s: float = 0.0
    # Huecos de más de medio segundo sin hacer NADA. «Quieto» a secas no valía:
    # entre golpe y golpe hay 140 ms de ritmo y un tap ocupa 35, así que dos
    # tercios del tramo salen «sin tocar» aunque se pelee sin parar. Esto es lo
    # que de verdad es estar plantado delante del enemigo.
    parado_s: float = 0.0
    # El recurso (Enzo, 19 sep): cuánto tiempo lo tuvo cargado —que esperar NO
    # es una falta— y cuánto saca cada vez que lo gasta, que es lo que dice si
    # la espera valió la pena.
    ex_cargada_s: float = 0.0
    definitiva_cargada_s: float = 0.0
    # Las EX que salieron pese a que el aro del botón decía que no había energía.
    # Sin esto el panel seguía marcando «0 EX» para quien sí las estaba lanzando.
    ex_a_ciegas: int = 0
    barras_por_definitiva: float = 0.0
    barras_por_ex: float = 0.0
    # «Quiero otro de cuando se usa bien y cuando no» (Enzo, 19 sep). Tres
    # respuestas, no dos: bien, mal y NO LO SÉ — un gasto que no se pudo medir
    # (sin barra a la vista) no es un gasto malo.
    definitivas_bien: int = 0
    definitivas_juzgadas: int = 0
    ex_bien: int = 0
    ex_juzgadas: int = 0
    gastos_sin_juzgar: int = 0
    # Parte de los fotogramas del tramo en que se le vio la barra al enemigo.
    # Es lo que separa un tramo de pelea de un tramo de dar mandobles al aire.
    visto: float = 0.0
    # El marcador de estilo del juego al acabar el tramo (None: no se pudo leer).
    # Ver `subida_del_marcador`.
    marcador: Optional[int] = None


class Practica:
    # Una cadena o una definitiva quitan la interfaz 2-5 s: se le da margen.
    ESPERA_INTERFAZ_S = 8.0
    # Lo que se le da al relevo para que entre el siguiente agente. Ver
    # `_esperar_relevo`: se mira hasta verlo, no se duerme a ciegas.
    ESPERA_RELEVO_S = 2.5
    # Y como mucho estas miradas: cada una cuesta una captura (0,6 s) más el
    # reconocimiento del retrato (0,24 s), así que mirar sin tope saldría más
    # caro que el `dormir` a ciegas que vino a sustituir.
    MIRADAS_RELEVO = 4
    # Entre dos miradas seguidas. La captura ya cuesta ~0,6 s de por sí, que es
    # ritmo de sobra: el 0,8 de antes era esperar dos veces por lo mismo.
    RESPIRO_S = 0.15
    # Sin interfaz tanto rato, la pelea terminó o hay otra pantalla delante.
    FUERA_DE_PELEA_S = 25.0
    RELEVOS_FALLIDOS_SEGUIDOS = 3
    # Por debajo de esto no había nadie delante: el tramo no enseña nada de
    # combos. 🔴 18 sep, 67 tramos medidos: la MITAD vio al enemigo menos del
    # 22 % de los fotogramas y un cuarto, menos del 2,7 %. De ahí salían los
    # «106 hechos» de un combo del que no se sabe si hace daño: 38 combos
    # aprendidos y CERO con juez. Un combo lanzado al aire no es un combo hecho
    # — Enzo ya lo dijo de los contadores: «el juez es la pantalla».
    VISTO_MINIMO = 0.20

    def __init__(self, mando: Any, rotacion: Rotacion, retratos: hud.RetratosDelEquipo,
                 vigia: Callable[..., str], diario: Optional[Path] = None,
                 parar_si: Optional[Callable[[], bool]] = None,
                 leer_puntos: Optional[Callable[[Any], Optional[int]]] = None,
                 segundos: Optional[Dict[str, float]] = None,
                 latido: Optional[Callable[[], None]] = None,
                 dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], float] = time.time,
                 reflejo: Optional[Callable[[int, Ajustes], str]] = None,
                 esperas: Optional[EsperasAprendidas] = None,
                 combos_de: Optional[Callable[[str], Sequence[Combo]]] = None,
                 combos_aprendidos: Optional[CombosAprendidos] = None,
                 leer_marcador: Optional[Callable[[bytes], Optional[int]]] = None):
        self.mando = mando
        # El marcador de estilo, leído en la captura que ya se hace al final de
        # cada tramo. Sólo por formas, sin modelo: se lee en cada relevo y
        # preguntar tanto al modelo se comería la cuota (ver marcador_zzz).
        self.leer_marcador = leer_marcador
        # El reflejo nativo sustituye al vigía de shell cuando está: esquiva o
        # releva según el color del destello (ver reflejo_zzz.py).
        self.reflejo = reflejo
        self.esperas = esperas
        # Con fichas de habilidades, cada vuelta practica un foco y cada tramo
        # juega combos del agente que está dentro.
        self.combos_de = combos_de
        self.combos_aprendidos = combos_aprendidos
        self._foco_i = 0
        # Sesión 75. Enzo: «tiene que hacer el combo del equipo, aprovechar todo el
        # daño que puede hacer el equipo sin parar; esto no es jugar». Y los números
        # le dan la razón: practicando, Yanagi hizo 0,0004 barras/s —prueba esquivas y
        # combos malos, la carrerilla salió 45 veces y quita 1 barra cada 1000 s—
        # frente a 0,0338 con su mejor combo. En este modo no se practica: se juega
        # con lo mejor que ya está medido, y se mide cuánto daño hace el equipo.
        self.a_por_todas = False
        # {agente: segundos} sacado de un gameplay (ver rotacion_de_video).
        self.rotacion_video: Dict[str, Any] = {}
        # La rotación entera dentro del binario (`--turnos`): una llamada por
        # vuelta en vez de una por agente. 22 sep: con la rotación en Python,
        # cada cambio costaba 2,27 s de mirar y arrancar —el 49 % del combate
        # con turnos de 1-2 s— y Miyabi, que es la que pega, estaba en el campo
        # el 19 % del tiempo.
        self.rotacion_dentro = False
        self.ciclos_por_vuelta = 2
        # Sólo defender (sin atacar) para que cada destello cuente y las esperas
        # se calibren de verdad: atacando salen 0-2 destellos por ronda.
        self.solo_defensa = False
        self.rot = rotacion
        self.retratos = retratos
        self.vigia = vigia
        self.diario = diario
        # Blindado: en este PRoot `PARAR.exists()` lanza Errno 38 en vez de
        # devolver False, y aquí se pisa en cada vuelta del bucle (S68 se comió
        # 6 horas por esto). Si no se puede comprobar, se para.
        self.parar_si = (lambda f=parar_si: freno_pisado(f)) if parar_si else (lambda: False)
        self.leer_puntos = leer_puntos
        self.latido = latido
        self._ultimo_latido = reloj()
        self.dormir = dormir
        self.reloj = reloj
        self.orden = [a.llamado for a in rotacion.equipo] or [t.agente for t in rotacion.tramos]
        self.tramo_de = {t.agente: t for t in rotacion.tramos}
        self.segundos = {t.agente: float(t.segundos) for t in rotacion.tramos}
        for k, v in (segundos or {}).items():
            if k in self.segundos:
                self.segundos[k] = float(v)
        # Lo aprendido con el tope de antes (16 s) o el plan por papel (12 s el
        # principal) entra en el margen de ahora.
        self.segundos = {k: min(SEG_MAX, max(SEG_MIN, v)) for k, v in self.segundos.items()}
        self._prueba: Optional[Tuple[str, float, float, str]] = None
        self._ya_mirados: set = set()       # a quién ya se le comprobó la lista
        self.relevo_anticipado = True       # pulsar y seguir, sin esperar a verlo
        # Ritmos medidos por configuración (tiempos + con qué se midió).
        self._muestras: Dict[Tuple, List[float]] = {}
        self._k = 0
        self.mejor: Optional[Tuple[float, Dict[str, float], str]] = None
        self.vueltas: List[Dict[str, Any]] = []

    @property
    def foco(self) -> str:
        """Lo que practica la vuelta en curso ("" sin fichas: la práctica de antes)."""
        if self.combos_de is None:
            return ""
        if self.a_por_todas:
            return "rotacion"      # a ganar, no a probar
        return FOCOS[self._foco_i % len(FOCOS)]

    # ── mirar ───────────────────────────────────────────────────────────
    def _captura(self) -> Optional[Any]:
        try:
            cap = self.mando.ver()
        except Exception as e:
            logger.warning("práctica: no pude ver la pantalla (%s)", e)
            return None
        return cap if cap is not None and getattr(cap, "png", None) else None

    def _esperar_interfaz(self, tope_s: float) -> Optional[Any]:
        fin = self.reloj() + tope_s
        while True:
            cap = self._captura()
            if cap is not None and hud.interfaz_a_la_vista(cap.png):
                return cap
            if self.reloj() >= fin or self.parar_si():
                return None
            self.dormir(self.RESPIRO_S)

    def _esperar_relevo(self, antes: Any) -> Tuple[Optional[Any], bool]:
        """Mira hasta que el retrato CAMBIE. Devuelve (captura, ¿cambió?).

        🔴 18 sep, 824 tramos: 5,7 s de mediana entre tramo y tramo — 84 de los
        187 minutos de práctica, el 45 % del reloj, fuera del juego. El hueco
        era un `dormir(0.8)` a ciegas para dar tiempo a la animación del relevo,
        y una sola mirada después. Si la mirada caía antes de que entrase el
        agente, el relevo se daba por fallido y se PULSABA OTRA VEZ — que no es
        sólo tiempo perdido: mete a un tercero en el campo y deja el tramo
        contando a quien no es.

        Mirar en bucle no cuesta más (la captura son ~0,6 s, ritmo de sobra),
        entra en cuanto el cambio está, y no pulsa dos veces por llegar pronto.
        """
        fin = self.reloj() + self.ESPERA_RELEVO_S
        ultima = None
        for mirada in range(self.MIRADAS_RELEVO):
            cap = self._captura()
            if cap is not None and hud.interfaz_a_la_vista(cap.png):
                # Una captura idéntica a la anterior no puede dar otro resultado,
                # y compararla cuesta microsegundos contra los 0,24 s de mirar
                # los retratos. Con la pantalla quieta —que es el caso cuando el
                # relevo no entra— esto ahorra el análisis entero.
                if ultima is None or cap.png != ultima.png:
                    ultima = cap
                    if hud.cambio_de_retrato(antes.png, cap.png) > hud.CAMBIO_DE_RELEVO:
                        return cap, True
            if self.reloj() >= fin or self.parar_si():
                break
            if mirada < self.MIRADAS_RELEVO - 1:
                self.dormir(self.RESPIRO_S)
        return ultima, False

    def _visto_de_verdad(self, quien: str) -> None:
        """A quien se ve peleando, se le tiene: manda el campo sobre la lista.

        19 sep: Nangong Yu llevaba desde el 17 en «los que no tengo» —no salió
        en una búsqueda de la pantalla de selección— y el equipo se armaba sin
        él, con Yanagi en su sitio. Pero quien entraba al campo era él, 16
        tramos con la cara reconocida al 99 %, y al no estar en la tabla de
        tiempos jugaba 6 s en vez de 16: el que más daño hacía del equipo
        (1,12 barras/min frente a 0,56) era el que menos jugaba.
        """
        if not quien or quien in self._ya_mirados:
            return
        self._ya_mirados.add(quien)
        try:
            from celestia_lib.seleccion_zzz import si_que_lo_tengo
            if si_que_lo_tengo(quien):
                self._apuntar({"aviso": f"a {quien} le tenía por no-tengo y está peleando: lo borro de esa lista"})
                logger.warning("práctica: %s estaba en «los que no tengo» y está en el campo", quien)
        except Exception as e:                  # pragma: no cover - lista rota
            logger.warning("práctica: no pude revisar la lista de los que no tengo (%s)", e)

    def _puntos(self, cap: Any) -> Optional[int]:
        if self.leer_puntos is None or cap is None:
            return None
        try:
            return self.leer_puntos(cap)
        except Exception as e:
            logger.warning("práctica: no pude leer los puntos (%s)", e)
            return None

    def _marcador(self, cap: Any) -> Optional[int]:
        if self.leer_marcador is None or cap is None:
            return None
        try:
            n = self.leer_marcador(cap.png)
        except Exception as e:
            logger.warning("práctica: no pude leer el marcador (%s)", e)
            return None
        return n if n is not None and 0 <= n <= TOPE_MARCADOR else None

    def siguiente(self, nombre: str) -> str:
        if nombre in self.orden:
            return self.orden[(self.orden.index(nombre) + 1) % len(self.orden)]
        return ""

    # ── jugar ───────────────────────────────────────────────────────────
    def jugar_tramo(self, agente: str) -> Medida:
        """El rato de un agente en el campo, dentro del móvil."""
        t = self.tramo_de.get(agente)
        seg = float(self.segundos.get(agente, t.segundos if t else 6.0))
        if self.foco in ("esquiva", "combos"):
            seg = max(seg, SEG_PRACTICA)
        if self.a_por_todas:
            # El turno dura la jugada del agente, ni más ni menos. Antes eran 8 s
            # fijos y cortaban los combos a medias (Miyabi: 4 toques en 8 s); luego
            # los puse en 24 s y sobraba tiempo pegando de más. Enzo, medido en
            # vídeo, releva cada 2-3 s: entra, suelta y sale.
            # Si hay rotacion aprendida de un gameplay, manda ella: dice cuanto
            # aguanta el principal y cuanto entran los apoyos.
            del_video = self.rotacion_video.get(agente) if self.rotacion_video else None
            if del_video:
                seg = max(1.0, min(TRAMO_A_POR_TODAS_S, float(del_video)))
            else:
                combos = self.combos_del_tramo(agente)
                if combos:
                    suya = duracion_combo(combos[0].pasos, RITMO_A_POR_TODAS)
                    seg = max(MIN_TRAMO_S, min(TRAMO_A_POR_TODAS_S, suya + MARGEN_RELEVO_S))
                else:
                    seg = max(seg, MIN_TRAMO_S)
        if self.reflejo is not None:
            return self._tramo_con_reflejo(agente, t, seg)
        reglas: List[Tuple[Punto, Punto, str]] = []
        nombres: List[str] = []
        if t is None or t.habilidad:
            reglas.append((EX_ANILLO, EX, "encendido"))
            nombres.append("ex")
        # El ASSIST va siempre: el juego lo ofrece cuando el enemigo ataca, y
        # dejarlo pasar es comerse el golpe.
        reglas.append((RELEVO, RELEVO, "apagado"))
        nombres.append("assist")
        if t is not None and t.definitiva:
            reglas.append((DEFINITIVA_ANILLO, DEFINITIVA, "encendido"))
            nombres.append("definitiva")
        try:
            salida = self.vigia(ATACAR, reglas, segundos=max(1, int(round(seg))), guarda=GUARDA)
        except Exception as e:
            logger.warning("práctica: el vigía falló (%s)", e)
            salida = ""
        c = leer_nota_del_vigia(salida, nombres)
        return Medida(agente or "¿?", seg, c.get("toques", 0), c.get("ex", 0),
                      c.get("assist", 0), c.get("definitiva", 0))

    @staticmethod
    def primero_el_medido(agente: str, lista: Sequence[Combo]) -> List[Combo]:
        """El combo que MÁS vida le quitó al enemigo, el primero de la vuelta.

        Sesión 75. Hasta ahora la rotación ordenaba por cadenas abiertas por minuto,
        que es lo que se podía contar sin juez de daño. Ya hay juez: el 17 sep, de
        Yanagi, `a a a a a` a 140 ms quitó una barra cada 29,6 s frente a los 161 s
        del aporreo. Si eso está medido, se juega eso primero — y si no, todo sigue
        igual que antes.
        """
        lista = list(lista)
        try:
            from celestia_lib.tiempos_combos_zzz import mejor
            gana = mejor(agente)
        except Exception:                      # pragma: no cover - sin fichero aún
            gana = None
        if not gana:
            return lista
        pasos = gana.rsplit("@", 1)[0].strip()
        for i, c in enumerate(lista):
            if (c.pasos or "").strip() == pasos:
                return [c] + lista[:i] + lista[i + 1:]
        return lista

    def _turno(self, agente: str) -> Optional[float]:
        """Lo que va a durar el turno de `agente` en una vuelta de rotación."""
        if self.rotacion_video:
            v = self.rotacion_video.get(agente)
            return float(v) if v else None
        if self.a_por_todas:
            return None              # ahí el turno sale de la jugada, no de una tabla
        v = self.segundos.get(agente)
        return float(v) if v is not None else None

    def combos_del_tramo(self, agente: str) -> List[Combo]:
        """Qué combos juega `agente` en este tramo, según lo que practica la vuelta."""
        if self.combos_de is None:
            return []
        try:
            suyos = list(self.combos_de(agente) or [])
        except Exception as e:
            logger.warning("práctica: sin combos de %s (%s)", agente, e)
            suyos = []
        foco = self.foco
        if foco == "esquiva":
            lista = [Combo(nombre, pasos, "práctica de esquivas", agente) for nombre, pasos in COMBOS_DE_ESQUIVA]
        elif foco == "rotacion" and self.a_por_todas:
            lista = self.primero_el_medido(agente, suyos)[:1] or suyos[:1]
            # Si a este agente le estan saltando defensas, sus combos con el dedo
            # sostenido le estan costando mas de lo que dan: se juega uno sin
            # mantener, que si deja defender y contraatacar.
            saltadas, destellos = defensas_saltadas_de(agente)
            if destellos >= 4 and saltadas >= destellos * 0.4:
                sueltos = [c for c in suyos if ":" not in (c.pasos or "")]
                if sueltos:
                    lista = self.primero_el_medido(agente, sueltos)[:1]
                    logger.info("práctica: %s se salta %d de %d defensas → combo sin mantener (%s)",
                                agente, saltadas, destellos, lista[0].pasos if lista else "?")
        elif foco == "rotacion" and self.combos_aprendidos is not None:
            lista = self.primero_el_medido(agente, self.combos_aprendidos.para_la_rotacion(agente, suyos))
        elif foco == "rotacion":
            lista = self.primero_el_medido(agente, suyos[:COMBOS_EN_ROTACION])
        else:
            lista = suyos
        if foco == "rotacion":
            # Un apoyo que entra 1-2 s no tiene tiempo de un combo: suelta su EX
            # (o su especial) y se va, que es lo que se ve en el gameplay y lo que
            # dice la guía de Astra. Antes sólo pasaba con la rotación de vídeo;
            # con los turnos cortos de ahora vale para cualquier rotación.
            turno = self._turno(agente)
            if turno is not None and turno <= TURNO_DE_ENTRAR_Y_SALIR:
                orden = {"E": 0, "e": 1, "U": 2}
                clave = sorted((c for c in suyos if (c.pasos or "").strip() in orden),
                               key=lambda c: orden[(c.pasos or "").strip()])
                if clave:
                    lista = clave[:1]
        # El binario coge los doce primeros válidos y cuenta en ESE orden: los
        # contadores de su nota sólo se entienden con esta misma lista.
        return [c for c in lista if pasos_validos(c.pasos)][:12]

    def _tramo_con_reflejo(self, agente: str, t: Any, seg: float) -> Medida:
        """El tramo con el binario del móvil: ataca, y ante un destello defiende.

        La especial entra en la combinación sólo si el tramo la usa, y la
        definitiva se intenta cada pocos segundos si el plan la pide (pulsarla
        sin carga no hace nada). Las esperas desde el destello las elige lo
        aprendido, y cada defensa de la nota cuenta para la espera que tenía.
        Con fichas, el ataque son los combos del agente (ver `combos_del_tramo`).
        """
        if self.solo_defensa:
            # Un ataque cada mucho: el reflejo se dedica a mirar destellos y
            # responder, que es lo que se quiere medir.
            aj = Ajustes(ritmo=60_000, especial=0, definitiva=0)
            if self.esperas is not None:
                aj.espera_rojo = self.esperas.elegir("rojo")
                aj.espera_dorado = self.esperas.elegir("dorado")
            try:
                salida = self.reflejo(max(1, int(round(seg))), aj) if self.reflejo else ""
            except Exception as e:
                logger.warning("práctica: el reflejo falló en defensa (%s)", e)
                salida = ""
            nota = leer_nota(salida)
            r = nota["resumen"]
            if self.esperas is not None:
                self.esperas.apuntar(nota, {"rojo": aj.espera_rojo, "dorado": aj.espera_dorado})
                self.esperas.guardar()
            return Medida(agente=agente, segundos=seg, foco="defensa",
                          avisos=int(r.get("avisos", 0)), perfectas=int(r.get("perfectas", 0)),
                          fallidas=int(r.get("fallidas", 0)),
                          contraataques=int(r.get("contraataques", 0)),
                          saltadas=int(r.get("defensas_saltadas", 0)), nota=salida[:200])
        aj = Ajustes(especial=4 if (t is None or t.habilidad) else 0,
                     definitiva=6 if (t is not None and t.definitiva) else 0,
                     # Medido el 17 sep con el juez de la vida: aporrear metiendo la
                     # especial cada 4 toques da una barra cada 161 s, y la cadena
                     # limpia una cada 29,6 s. Pulsarla sin energía rompe la cadena y
                     # no hace el EX, que es lo que pega. Sólo con el anillo encendido.
                     especial_con_energia=True,
                     # Pero un combo que pide la EX NO se veta por el aro del
                     # botón: ese aro no es la energía (Enzo, 19 sep) y por él
                     # Astra lanzó cero EX en toda una partida teniéndola. Si de
                     # verdad no hay, sale el especial normal y no se pierde nada.
                     ex_a_ciegas=True)
        if self.esperas is not None:
            aj.espera_rojo = self.esperas.elegir("rojo")
            aj.espera_dorado = self.esperas.elegir("dorado")
        combos = self.combos_del_tramo(agente)
        if combos:
            aj.combos = [c.pasos for c in combos]
            # En la rotación la definitiva se guarda para quien la tenga en su tramo;
            # en las demás vueltas se practica con quien esté dentro.
            # Enzo, 17 sep: «los tiempos no son el problema, es Celestia que no sabe
            # jugar». Y es cierto: en la partida de esa noche salieron **0
            # definitivas** con el anillo cargado (`definitiva: True` en el propio
            # log), 0 cadenas y 0 defensas perfectas. La definitiva es el daño más
            # gratis del juego: cuando se juega a ganar, se suelta siempre que esté
            # cargada, sin reservarla para el turno de nadie.
            aj.auto_definitiva = (self.a_por_todas or self.foco != "rotacion"
                                  or (t is not None and t.definitiva))
        t0 = self.reloj()
        try:
            salida = self.reflejo(max(1, int(round(seg))), aj)
        except Exception as e:
            logger.warning("práctica: el reflejo falló (%s)", e)
            salida = ""
        sobra = self.reloj() - t0 - seg
        guarda_el_crudo(salida, agente or "¿?", aj.ritmo)
        nota = leer_nota(salida)
        r = nota["resumen"]
        try:
            from celestia_lib.vida_enemigo_zzz import vida_quitada
            quitado, muertes = vida_quitada(nota.get("vida_enemigo") or [])
        except Exception as e:                      # pragma: no cover - juez roto
            logger.warning("práctica: sin juez de vida (%s)", e)
            quitado, muertes = 0.0, 0
        # El listón para «bien usado» es lo que ese agente hace SIN gastar nada,
        # en este mismo tramo: así no hay umbral inventado ni comparaciones con
        # otro personaje, que tienen kits distintos.
        rinde = rinde_el_recurso(salida, nota.get("vida_enemigo") or [],
                                 ritmo_base=(quitado / seg) if seg > 0 else 0.0)
        if self.esperas is not None:
            self.esperas.apuntar(nota, {"rojo": aj.espera_rojo, "dorado": aj.espera_dorado})
        fotogramas = int(r.get("fotogramas", 0) or 0)
        visto = (int(r.get("enemigo_visto", 0) or 0) / fotogramas) if fotogramas else 0.0
        ciego = bool(combos) and visto < self.VISTO_MINIMO
        if combos and r and self.combos_aprendidos is not None and not ciego:
            # El juez del tramo va con los combos: sin él la nota volvería a
            # premiar al botón más corto, que es como se acabó aporreando.
            self.combos_aprendidos.apuntar(agente, combos, r, aj.ritmo, danio=float(quitado))
        ex = int(r.get("ex_reales", 0)) if combos else int(r.get("especiales", 0))
        m = Medida(agente or "¿?", seg,
                   toques=int(r.get("ataques", 0)) + int(r.get("especiales", 0)),
                   ex=ex, assist=int(r.get("asistencias", 0)),
                   definitiva=int(r.get("definitivas", 0)), avisos=int(r.get("avisos", 0)),
                   perfectas=int(r.get("perfectas", 0)) + int(r.get("asist_perfectas", 0)),
                   fallidas=int(r.get("fallidas", 0)),
                   contraataques=int(r.get("contraataques", 0)),
                   cadenas=int(r.get("cadenas", 0)), foco=self.foco,
                   barras=round(float(quitado), 3), muertes=int(muertes),
                   buscando_s=round(float(r.get("buscando_ms") or 0) / 1000.0, 1),
                   visto=round(visto, 3),
                   combos=sum(r.get("combos_hechos") or []), cortados=sum(cortes_creibles(r, aj.ritmo)),
                   arranque_s=round(max(0.0, sobra), 2),
                   especiales=int(r.get("especiales", 0)),
                   ex_falladas=int(r.get("ex_sin_energia", 0)),
                   tocando_s=round(float(r.get("tocando_ms") or 0) / 1000.0, 1),
                   quieto_s=round(float(r.get("quieto_ms") or 0) / 1000.0, 1),
                   parado_s=round(float(r.get("parado_ms") or 0) / 1000.0, 1),
                   ex_a_ciegas=int(r.get("ex_a_ciegas", 0)),
                   ex_cargada_s=round(float(r.get("ex_cargada_ms") or 0) / 1000.0, 1),
                   definitiva_cargada_s=round(float(r.get("definitiva_cargada_ms") or 0) / 1000.0, 1),
                   barras_por_definitiva=rinde.get("definitiva", (0, 0.0))[1],
                   barras_por_ex=rinde.get("ex", (0, 0.0))[1],
                   definitivas_bien=rinde.get("definitiva_bien", (0, 0))[0],
                   definitivas_juzgadas=rinde.get("definitiva_bien", (0, 0))[1],
                   ex_bien=rinde.get("ex_bien", (0, 0))[0],
                   ex_juzgadas=rinde.get("ex_bien", (0, 0))[1],
                   gastos_sin_juzgar=(rinde.get("definitiva_sin_juzgar", (0, 0))[0]
                                      + rinde.get("ex_sin_juzgar", (0, 0))[0]),
                   saltadas=int(r.get("defensas_saltadas", 0)))
        if ciego:
            m.nota = (f"le vi el {visto * 100:.0f} % del tramo: no aprendo combos "
                      f"de esto (hace falta {self.VISTO_MINIMO * 100:.0f} %)")
        if nota["error_tactil"]:
            m.nota = "el reflejo no pudo tocar la pantalla"
        elif not r:
            m.nota = "el reflejo no dejó resumen: " + (salida or "")[:80]
        elif nota["motivo"] == "sin_interfaz":
            m.nota = "el reflejo dejó de ver la interfaz de pelea"
        return m

    def plan_de_la_rotacion(self, primero: str = "") -> List[Tuple[str, float, str]]:
        """(agente, segundos, pasos) en el orden del relevo, desde quien está dentro."""
        orden = list(self.orden)
        if primero in orden:
            i = orden.index(primero)
            orden = orden[i:] + orden[:i]
        plan: List[Tuple[str, float, str]] = []
        for agente in orden:
            combos = self.combos_del_tramo(agente)
            if not combos:
                return []
            seg = self._turno(agente)
            if not seg:
                seg = float(self.segundos.get(agente, MIN_TRAMO_S))
            plan.append((agente, max(1.0, float(seg)), combos[0].pasos))
        return plan

    def jugar_rotacion(self, actual: str) -> List[Medida]:
        """Una vuelta entera —todos los turnos— en una sola llamada al reflejo."""
        plan = self.plan_de_la_rotacion(actual)
        if not plan or self.reflejo is None:
            return []
        aj = Ajustes(especial=0, definitiva=0, especial_con_energia=True, ex_a_ciegas=True)
        if self.esperas is not None:
            aj.espera_rojo = self.esperas.elegir("rojo")
            aj.espera_dorado = self.esperas.elegir("dorado")
        aj.combos = [pasos for _a, _s, pasos in plan]
        aj.turnos = [int(seg * 1000) for _a, seg, _p in plan]
        aj.auto_definitiva = True
        seg_total = max(3, int(round(self.ciclos_por_vuelta * sum(s for _a, s, _p in plan))))
        t0 = self.reloj()
        try:
            salida = self.reflejo(seg_total, aj)
        except Exception as e:
            logger.warning("práctica: el reflejo falló en la rotación (%s)", e)
            return []
        guarda_el_crudo(salida, "rotacion", aj.ritmo)
        nota = leer_nota(salida)
        if self.esperas is not None:
            self.esperas.apuntar(nota, {"rojo": aj.espera_rojo, "dorado": aj.espera_dorado})
        medidas = medidas_por_turno(salida, [(a, pasos) for a, _s, pasos in plan])
        if not medidas:
            logger.warning("práctica: la rotación no dejó turnos: %s", (salida or "")[:120])
            return []
        # Lo que el reflejo tardó de más sobre lo pedido, al primero de la vuelta.
        medidas[0].arranque_s = round(max(0.0, self.reloj() - t0 - seg_total), 2)
        if nota["error_tactil"]:
            medidas[0].nota = "el reflejo no pudo tocar la pantalla"
        elif nota["motivo"] == "sin_interfaz":
            medidas[0].nota = "el reflejo dejó de ver la interfaz de pelea"
        return medidas

    def relevar(self, m: Medida) -> str:
        """Pulsa el relevo, COMPRUEBA que entró alguien, y dice quién.

        Devuelve "" sólo si la interfaz no vuelve (la pelea se acabó). Si el
        relevo no entra, devuelve a quien sigue dentro.
        """
        empezo = self.reloj()
        antes = self._esperar_interfaz(self.ESPERA_INTERFAZ_S)
        if antes is None:
            m.nota = "la interfaz no vuelve tras el tramo"
            m.relevo_s = round(self.reloj() - empezo, 2)
            return ""
        m.marcador = self._marcador(antes)
        dentro = self.retratos.quien(antes.png)[0]
        self._visto_de_verdad(dentro)
        if dentro and dentro != m.agente:
            # Con el relevo anticipado esto ya no es sólo un ASSIST: también
            # puede ser que el relevo anterior no llegara a entrar. En los dos
            # casos el tramo lo jugó quien se ve, así que se apunta a su nombre
            # en vez de mentir en el diario. Es la red que permite no mirar: el
            # error no se evita, se corrige antes de guardar nada.
            m.nota = (f"un ASSIST metió a {dentro} a mitad de tramo" if m.relevo
                      else f"el tramo lo jugó {dentro}, no {m.agente}")
            m.agente = dentro
        base = dentro or m.agente
        # Una cara nueva sólo se apunta si se sabe de quién es. Tras un ASSIST
        # sin reconocer a quien quedó dentro, «el siguiente» sería una
        # suposición, y una huella mal nombrada estropea todas las vueltas.
        seguro = bool(dentro) or m.assist == 0
        # ── el relevo sin esperar a verlo ────────────────────────────────
        # Enzo, 19 sep: «siempre espera a que pase algo o ver algo en pantalla
        # para accionar y tarda mucho… y si ya sabe lo que hace cada botón, que
        # piense las acciones de más adelante y las haga casi instantáneas».
        # Medido esa noche: confirmar el relevo costaba 2,5 s de los 3,8 del
        # hueco, 47 veces por partida — casi 2 minutos de 14 mirando algo que
        # iba a pasar igual, mientras el reflejo del tramo siguiente tarda 0,8 s
        # en arrancar de todas formas. Así que se pulsa y se sigue: quién entró
        # se sabrá en la captura del final del tramo, que ya se hace, y si no
        # fue quien tocaba el tramo se apunta a su nombre (arriba).
        # Sólo cuando se conocen las caras de todo el equipo: si falta alguna,
        # hay que mirar para aprenderla.
        if self.relevo_anticipado and all(a in self.retratos.huellas for a in self.orden):
            self.mando.tocar(RELEVO)
            quien = self.siguiente(base)
            m.relevo, m.entra = True, quien
            m.relevo_s = round(self.reloj() - empezo, 2)
            return quien
        for _intento in range(2):
            self.mando.tocar(RELEVO)
            despues, cambio = self._esperar_relevo(antes)
            if despues is None:
                m.nota = "la interfaz no vuelve tras el relevo"
                return ""
            if cambio:
                m.relevo_s = round(self.reloj() - empezo, 2)
                quien = self.retratos.quien(despues.png)[0]
                if not quien:
                    quien = self.siguiente(base)
                    if quien and seguro:
                        self.retratos.apuntar(quien, despues.png)
                        m.nota = (m.nota + "; " if m.nota else "") + f"aprendí la cara de {quien}"
                m.relevo, m.entra = True, quien
                return quien
            antes = despues
        m.relevo_s = round(self.reloj() - empezo, 2)
        m.nota = (m.nota + "; " if m.nota else "") + "pulsé el relevo dos veces y no entró nadie"
        return base

    # ── aprender ────────────────────────────────────────────────────────
    @staticmethod
    def valorar(tramos: Sequence[Medida], duracion_s: float,
                puntos_antes: Optional[int], puntos: Optional[int],
                marcador_previo: Optional[int] = None,
                desde_previo: float = 0.0) -> Tuple[float, str]:
        """Cuánto rindió una vuelta, por minuto, y con qué se midió."""
        minutos = max(duracion_s, 1.0) / 60.0
        # 22 sep: primero la nota del propio juego, si se pudo leer buena parte
        # de la vuelta. La barra del enemigo decía que turnos de 16 s rendían más
        # porque con un enemigo pequeño la pierde de vista (6 «muertes» en 144 s
        # de vídeo, todas ruido), y el aprendizaje le hizo caso.
        subida, medido = subida_del_marcador(tramos, marcador_previo, desde_previo)
        if medido >= MARCADOR_CUBRE_MIN * max(duracion_s, 1.0):
            return subida / (medido / 60.0), "estilo"
        # Lo primero, el DAÑO: es lo único que dice si se está jugando bien.
        # Mientras no hubo juez de vida sólo quedaban el marcador y las acciones,
        # y las acciones premian al que aporrea barato: el 19 sep, con el reparto
        # decidido por acciones/min, Astra —de apoyo, 0,56 barras/min— se llevaba
        # el 43 % del reloj y Nangong Yu —1,12 barras/min, el que más pega— el
        # 18 %. Con el juez puesto (S75) esto ya se puede medir por lo que
        # importa. Si en la vuelta no se le quitó vida a nadie, se sigue con lo
        # de antes: cero barras puede ser que no se viera la barra, no que no se
        # hiciera daño.
        barras = sum(t.barras for t in tramos)
        if barras > 0:
            return barras / minutos, "daño"
        # 11 sep 2026, Entrenamiento libre: el marcador subió hasta 3000 y se
        # quedó ahí tres vueltas mientras se seguía pegando (cadenas incluidas).
        # Medido con puntos eso es «0 por minuto», y cada prueba de tiempos se
        # juzgaba contra un cero que no dice nada. Si hubo golpes de verdad y el
        # número no se movió, esa vuelta se mide por acciones.
        parado = (puntos is not None and puntos == puntos_antes
                  and sum(t.toques for t in tramos) >= TOQUES_PARA_MARCADOR_PARADO)
        if parado:
            logger.warning("práctica: el marcador sigue en %s tras %d toques: mido por acciones",
                           puntos, sum(t.toques for t in tramos))
        if (not parado and puntos is not None and puntos_antes is not None
                and puntos >= puntos_antes):
            return (puntos - puntos_antes) / minutos, "puntos"
        # Un ataque en cadena es lo que más dice: sólo sale si se aturdió al enemigo.
        acciones = sum(t.ex + 2 * t.assist + 3 * t.definitiva + 3 * t.perfectas + 4 * t.cadenas
                       + (1 if t.relevo else 0) for t in tramos)
        return acciones / minutos, "acciones"

    def _clave(self, fuente: str) -> Tuple:
        return (tuple(sorted(self.segundos.items())), fuente)

    def ajustar(self, ritmo: float, fuente: str) -> str:
        """Decide con la vuelta que acaba de terminar. Devuelve la decisión, en claro.

        Primero se mide la configuración de ahora hasta tener `MIN_MUESTRAS`
        vueltas; luego se prueba mover el tiempo de un agente y se mide igual.
        Se queda sólo si rinde un `MARGEN` más; si no se nota, vuelve a lo que
        había — un cambio que no demuestra nada no se queda por casualidad.
        """
        clave = self._clave(fuente)
        muestras = self._muestras.setdefault(clave, [])
        muestras.append(ritmo)
        media = sum(muestras) / len(muestras)
        if len(muestras) >= MIN_MUESTRAS and (
                self.mejor is None or (self.mejor[2] == fuente and media > self.mejor[0])):
            self.mejor = (media, dict(self.segundos), fuente)
        if len(muestras) < MIN_MUESTRAS:
            return (f"sigo midiendo así ({len(muestras)} de {MIN_MUESTRAS} vueltas, "
                    f"{media:.1f} por minuto con {fuente})")
        if self._prueba is not None:
            agente, antes, base, fuente_antes = self._prueba
            self._prueba = None
            probado = self.segundos.get(agente, antes)
            if fuente != fuente_antes:
                self.segundos[agente] = antes
                return (f"no se puede comparar (medí con {fuente} y antes con "
                        f"{fuente_antes}): {agente} vuelve a {antes:.0f} s")
            if media > base * (1 + MARGEN):
                return (f"{agente} con {probado:.0f} s rinde más ({media:.1f} frente a "
                        f"{base:.1f} por minuto, en {len(muestras)} vueltas): me lo quedo")
            self.segundos[agente] = antes
            if media < base * (1 - MARGEN):
                return (f"{agente} con {probado:.0f} s rindió menos ({media:.1f} frente a "
                        f"{base:.1f} por minuto): vuelve a {antes:.0f} s")
            return (f"{agente} con {probado:.0f} s no se nota ({media:.1f} frente a "
                    f"{base:.1f} por minuto): vuelve a {antes:.0f} s")
        for _ in range(max(1, len(self.orden))):
            if not self.orden:
                break
            agente = self.orden[self._k % len(self.orden)]
            # Primero se prueba a alargar a todos; en la pasada siguiente, a acortar.
            direccion = PASO if (self._k // len(self.orden)) % 2 == 0 else -PASO
            self._k += 1
            actual = self.segundos.get(agente, 6.0)
            nuevo = min(SEG_MAX, max(SEG_MIN, actual + direccion))
            if nuevo != actual:
                self._prueba = (agente, actual, media, fuente)
                self.segundos[agente] = nuevo
                return (f"pruebo {agente} con {nuevo:.0f} s en el campo (estaba en "
                        f"{actual:.0f}; así rendía {media:.1f} por minuto)")
        return "no hay nada más que probar"

    # ── la práctica entera ─────────────────────────────────────────────
    def _apuntar(self, datos: Dict[str, Any]) -> None:
        if self.diario is None:
            return
        try:
            self.diario.parent.mkdir(parents=True, exist_ok=True)
            with self.diario.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(self.reloj(), 1), **datos}, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("práctica: no pude escribir el diario (%s)", e)

    def practicar(self, minutos: float = 10.0, max_vueltas: int = 40) -> Dict[str, Any]:
        fin = self.reloj() + minutos * 60
        cap = self._esperar_interfaz(self.FUERA_DE_PELEA_S)
        if cap is None:
            return self._cierre("no veo la interfaz de pelea: no estoy en un combate")
        actual = self.retratos.quien(cap.png)[0]
        if not actual and self.orden:
            actual = self.orden[0]
            self.retratos.apuntar(actual, cap.png)
            self._apuntar({"aviso": f"no reconozco a quien está dentro: supongo que es {actual}"})
        puntos_antes = self._puntos(cap)
        marcador_previo = self._marcador(cap)
        desde_previo = 0.0
        tramos: List[Medida] = []
        fallidos = 0
        motivo = "se acabó el tiempo"
        inicio_vuelta = self.reloj()
        while self.reloj() < fin and len(self.vueltas) < max_vueltas:
            if self.parar_si():
                motivo = "me dijiste que parara"
                break
            if self.latido is not None and self.reloj() - self._ultimo_latido >= LATIDO_S:
                try:
                    self.latido()
                except Exception as e:
                    logger.warning("práctica: el latido falló (%s)", e)
                self._ultimo_latido = self.reloj()
            if self.rotacion_dentro:
                # Una llamada por VUELTA: los relevos los hace el binario.
                medidas = self.jugar_rotacion(actual)
                if not medidas:
                    motivo = "la rotación no se pudo jugar dentro del móvil"
                    break
                cap = self._esperar_interfaz(self.ESPERA_INTERFAZ_S)
                if cap is None:
                    motivo = "la pelea ha terminado o hay otra pantalla delante"
                    break
                # La lectura del marcador va ANTES de apuntar los turnos: si no,
                # el diario los guarda con «marcador: null» aunque el juez sí lo
                # use, y luego no hay forma de revisar con qué se decidió.
                medidas[-1].marcador = self._marcador(cap)
                for m in medidas:
                    tramos.append(m)
                    self._apuntar({"tramo": asdict(m)})
                quien = self.retratos.quien(cap.png)[0]
                if quien:
                    self._visto_de_verdad(quien)
                    actual = quien
                elif medidas[-1].entra:
                    actual = medidas[-1].entra
            else:
                m = self.jugar_tramo(actual)
                entra = self.relevar(m)
                tramos.append(m)
                self._apuntar({"tramo": asdict(m)})
                if not entra and m.nota.startswith("la interfaz no vuelve"):
                    motivo = "la pelea ha terminado o hay otra pantalla delante"
                    break
                fallidos = 0 if m.relevo else fallidos + 1
                if fallidos >= self.RELEVOS_FALLIDOS_SEGUIDOS:
                    motivo = "pulso el relevo y no entra nadie: paro para no machacar a ciegas"
                    break
                actual = entra or actual
            if self.rotacion_dentro or len(tramos) >= len(self.orden):
                if not self.rotacion_dentro:
                    cap = self._captura()
                puntos = self._puntos(cap)
                ritmo, fuente = self.valorar(tramos, self.reloj() - inicio_vuelta,
                                             puntos_antes, puntos, marcador_previo, desde_previo)
                foco = self.foco
                # Los tiempos del relevo sólo se comparan entre vueltas que juegan
                # igual: una de esquivas rinde distinto por lo que practica, no por
                # cuánto está cada una en el campo.
                if not foco or foco == "rotacion":
                    decision = self.ajustar(ritmo, fuente)
                else:
                    decision = f"vuelta de {foco}: no mueve los tiempos del relevo"
                vuelta = {"n": len(self.vueltas) + 1, "foco": foco, "ritmo": round(ritmo, 2), "fuente": fuente,
                          "puntos": puntos, "ex": sum(t.ex for t in tramos),
                          "assist": sum(t.assist for t in tramos),
                          "definitiva": sum(t.definitiva for t in tramos),
                          "relevos": sum(1 for t in tramos if t.relevo),
                          "avisos": sum(t.avisos for t in tramos),
                          "perfectas": sum(t.perfectas for t in tramos),
                          "fallidas": sum(t.fallidas for t in tramos),
                          "cadenas": sum(t.cadenas for t in tramos),
                          "combos": sum(t.combos for t in tramos),
                          "cortados": sum(t.cortados for t in tramos),
                          "saltadas": sum(t.saltadas for t in tramos),
                          "tramos": len(tramos), "decision": decision,
                          "marcadores": [t.marcador for t in tramos],
                          "segundos": dict(self.segundos)}
                self.vueltas.append(vuelta)
                self._apuntar({"vuelta": vuelta})
                self._foco_i += 1
                if puntos is not None:
                    puntos_antes = puntos
                marcador_previo, desde_previo = ultima_lectura(tramos, marcador_previo, desde_previo)
                tramos = []
                inicio_vuelta = self.reloj()
        return self._cierre(motivo)

    def _cierre(self, motivo: str) -> Dict[str, Any]:
        # Lo que queda en uso es lo mejor medido, no lo último probado: una
        # prueba a medias al acabar el tiempo no ha demostrado nada.
        if self._prueba is not None:
            agente, antes, _r, _f = self._prueba
            self.segundos[agente] = antes
            self._prueba = None
        if self.esperas is not None:
            self.esperas.guardar()
        if self.combos_aprendidos is not None:
            self.combos_aprendidos.guardar()
        r = {"motivo": motivo, "vueltas": len(self.vueltas), "segundos": dict(self.segundos),
             "esperas": self.esperas.resumen() if self.esperas is not None else None,
             "mejor": ({"ritmo": round(self.mejor[0], 2), "segundos": self.mejor[1],
                        "fuente": self.mejor[2]} if self.mejor else None)}
        self._apuntar({"fin": r})
        return r
