"""Buscar la rotacion que mas mata: probar variantes y quedarse con la mejor.

Enzo, 17 sep 2026: «1,23 no es la meta, la meta es hacer mas que 1,23, explotar
al maximo ese equipo». Y tiene razon en que el liston no es empatar con el: lo que
una maquina puede hacer y una persona no es **probar veinte rotaciones y medirlas**
en vez de jugar la que le parece mejor.

Una rotacion aqui es: en que orden entran los agentes y cuanto aguanta cada uno.
De cada una se mide lo mismo (muertes por minuto, que es lo unico que el juez no
subestima — ver [[proyecto_liston_enzo]]) y gana la que mas mate, no la que mejor
suene.

Lo que sale de un gameplay ([[ver_tutorial_zzz]]) entra aqui como **candidata de
partida**, no como verdad: es el punto desde el que buscar.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from celestia_lib.paths import MEM_DIR

RUTA = MEM_DIR / "jugador" / "rotaciones_zzz.json"
# Menos de esto no es una prueba: es una anecdota. Con 3 min de pelea y 1 muerte
# por minuto ya se distinguen dos rotaciones que se lleven el 30 %.
MIN_SEGUNDOS = 150.0


@dataclass
class Rotacion:
    """Un orden de entrada y lo que aguanta cada uno, con lo que dio al probarla."""
    orden: List[str]
    turnos: Dict[str, float]
    de: str = ""                      # de donde salio: video, variante, a mano
    probada_s: float = 0.0
    muertes: int = 0
    barras: float = 0.0

    @property
    def muertes_min(self) -> Optional[float]:
        if self.probada_s < MIN_SEGUNDOS:
            return None               # sin pelea suficiente no se opina
        return round(self.muertes / (self.probada_s / 60.0), 3)

    @property
    def barras_min(self) -> Optional[float]:
        """Barras de vida quitadas por minuto.

        Con un BOSS no hay muertes que contar: la ronda 17 del 18 sep quito 3,11
        barras a Tanatos en 474 s y murio 0 veces. Las muertes siguen siendo la
        unidad buena cuando el enemigo muere; cuando no, manda el dano.
        """
        if self.probada_s < MIN_SEGUNDOS:
            return None
        return round(self.barras / (self.probada_s / 60.0), 3)

    def clave(self) -> str:
        return "|".join(self.orden) + "#" + ",".join(
            f"{k}:{self.turnos[k]:.1f}" for k in sorted(self.turnos))


def variantes(base: Rotacion, pasos: Sequence[float] = (2.0, 0.5)) -> List[Rotacion]:
    """Rotaciones vecinas a una que ya se conoce: mismo orden, otros tiempos, y
    el orden rotado.

    No se inventan tiempos absurdos: se estira y se encoge lo que aguanta cada
    uno, que es la palanca que se vio en el gameplay (principal 5,2 s, apoyos 1 s).
    Se prueba **primero estirando**: con los tiempos del video tal cual hizo 0
    muertes en 225 s, y su propia busqueda de la misma partida encontro que Miyabi
    rendia mas con 12 s que con 10. Encoger va en la direccion contraria.
    """
    fuera: List[Rotacion] = []
    for factor in pasos:
        turnos = {k: round(max(1.0, min(20.0, v * factor)), 1) for k, v in base.turnos.items()}
        if turnos != base.turnos:
            fuera.append(Rotacion(list(base.orden), turnos, de=f"x{factor} de {base.de or 'base'}"))
    # Y empezar por otro: en ZZZ quien abre decide si el primer aturdimiento llega
    # con el daño dentro o fuera.
    for giro in range(1, len(base.orden)):
        orden = base.orden[giro:] + base.orden[:giro]
        fuera.append(Rotacion(orden, dict(base.turnos), de=f"empieza {orden[0]}"))
    return fuera


def jugable(r: "Rotacion") -> bool:
    """Falso si la rotacion habla de agentes que el juego no encuentra.

    18 sep: la «mejor» del marcador era `Nangong Yu → Astra → Miyabi` (0,246
    muertes/min), un equipo que no se puede montar, y la busqueda se dedicaba a
    explorar vecinas suyas. Una rotacion que no se puede jugar no compite.
    """
    try:
        from celestia_lib.seleccion_zzz import los_que_no_tengo
        negra = {n.lower() for n in los_que_no_tengo()}
        cortos = {n.split()[-1].lower() for n in negra}
    except Exception:
        return True
    for nombre in list(r.orden) + list(r.turnos):
        bajo = str(nombre).lower()
        if bajo in negra or bajo.split()[-1] in cortos:
            return False
    return True


def leer(ruta: Path = RUTA) -> List[Rotacion]:
    try:
        crudo = json.loads(ruta.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    fuera = []
    for d in crudo.get("rotaciones", []):
        try:
            fuera.append(Rotacion(list(d["orden"]), {k: float(v) for k, v in d["turnos"].items()},
                                  de=d.get("de", ""), probada_s=float(d.get("probada_s") or 0),
                                  muertes=int(d.get("muertes") or 0),
                                  barras=float(d.get("barras") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return [r for r in fuera if jugable(r)]


def guardar(rotaciones: Sequence[Rotacion], ruta: Path = RUTA) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps({"rotaciones": [
        {"orden": r.orden, "turnos": r.turnos, "de": r.de, "probada_s": round(r.probada_s, 1),
         "muertes": r.muertes, "barras": round(r.barras, 3),
         "muertes_min": r.muertes_min} for r in rotaciones]},
        ensure_ascii=False, indent=1), "utf-8")
    return ruta


def apuntar(rotaciones: List[Rotacion], jugada: Rotacion) -> List[Rotacion]:
    """Suma lo jugado a la rotacion que ya estuviera, o la anade."""
    for r in rotaciones:
        if r.clave() == jugada.clave():
            r.probada_s += jugada.probada_s
            r.muertes += jugada.muertes
            r.barras += jugada.barras
            return rotaciones
    rotaciones.append(jugada)
    return rotaciones


def mejor(rotaciones: Sequence[Rotacion]) -> Optional[Rotacion]:
    """La que mas mata por minuto; si nadie mata, la que mas dano hace.

    Contra un boss las muertes son todas cero y comparar por ellas deja la
    busqueda ciega: entonces decide el dano (barras por minuto).
    """
    buenas = [r for r in rotaciones if r.muertes_min is not None]
    if not buenas:
        return None
    if any((r.muertes_min or 0) > 0 for r in buenas):
        return max(buenas, key=lambda r: r.muertes_min)
    return max(buenas, key=lambda r: (r.barras_min or 0.0))


def siguiente_a_probar(rotaciones: Sequence[Rotacion]) -> Optional[Rotacion]:
    """Que jugar ahora: lo que no se ha probado bastante, y si no, una vecina de
    la mejor. Asi la busqueda no se queda dando vueltas a lo que ya sabe."""
    for r in rotaciones:
        if r.muertes_min is None:
            return r
    campeona = mejor(rotaciones)
    if campeona is None:
        return None
    conocidas = {r.clave() for r in rotaciones}
    for v in variantes(campeona):
        if v.clave() not in conocidas:
            return v
    return None
