"""Lo que aprende de medir sus combos: cuánto daño hace cada uno, por segundo.

Sesión 75, 16 sep 2026. Enzo, cuando le conté que había que lanzar la medición:
«pero eso lo debería hacer sola, ¿lo sabes? No tú tocando las cosas». Tenía
razón: medir estaba hecho, pero el veredicto —qué combo gana, cuál no vale— lo
sacaba yo leyendo el diario a mano, y sin guardarlo en ningún sitio no servía
para la siguiente partida.

Aquí vive lo aprendido: por agente y combo, la vida del enemigo que quita por
segundo, con cuántos tramos se midió y cuándo. Se escribe solo al acabar una
tanda (`scripts/medir_combos_zzz.py`) y lo lee quien vaya a pelear, para empezar
por el que más daño hace en vez de por el primero de la lista.

Una medida con más tramos válidos gana a una con menos; a igualdad de tramos,
manda la más reciente. Así una tanda corta no borra el trabajo de una larga.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from celestia_lib.paths import MEM_DIR

RUTA = MEM_DIR / "jugador" / "tiempos_combos_zzz.json"
EQUIPO = MEM_DIR / "jugador" / "equipo_zzz.json"

# Con menos tramos válidos que esto, lo medido es una anécdota: se guarda, pero
# `mejor()` no lo propone (la noche del 16 sep, un solo tramo limpio de cinco).
MIN_VALIDOS = 2


def leer(ruta: Path = RUTA) -> Dict[str, Any]:
    try:
        datos = json.loads(ruta.read_text("utf-8"))
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def aprender(datos: Dict[str, Any], agente: str, tabla: Sequence[Dict[str, Any]],
             fecha: str) -> Dict[str, Any]:
    """Mezcla el resumen de una tanda con lo que ya se sabía. Puro: no toca disco.

    `tabla` es lo que devuelve `resumen_por_variante()`: una fila por variante con
    `media` (barras por segundo), `validos` y `tramos`.
    """
    agente = (agente or "").strip() or "sin agente"
    fuera = dict(datos)
    suyo = dict(fuera.get(agente) or {})
    for fila in tabla:
        variante = str(fila.get("variante") or "").strip()
        if not variante or fila.get("media") in (None, 0):
            continue                      # sin medida buena no se aprende nada
        validos = int(fila.get("validos") or 0)
        if validos <= 0:
            continue
        antes = suyo.get(variante) or {}
        if int(antes.get("validos") or 0) > validos:
            continue                      # lo de antes se midió mejor: se queda
        suyo[variante] = {"por_segundo": round(float(fila["media"]), 4),
                          "segundos_por_barra": fila.get("segundos_por_barra"),
                          "validos": validos, "tramos": int(fila.get("tramos") or 0),
                          "muertes": int(fila.get("muertes") or 0), "fecha": fecha}
    fuera[agente] = suyo
    return fuera


def guardar(agente: str, tabla: Sequence[Dict[str, Any]], fecha: str,
            ruta: Path = RUTA) -> Dict[str, Any]:
    """Aprende la tanda y lo deja escrito. Devuelve lo que sabe de ese agente."""
    datos = aprender(leer(ruta), agente, tabla, fecha)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
    return datos.get((agente or "").strip() or "sin agente", {})


def mejor(agente: str, ruta: Path = RUTA, min_validos: int = MIN_VALIDOS) -> Optional[str]:
    """El combo que más vida le quita, de los medidos con tramos suficientes."""
    suyo = (leer(ruta).get((agente or "").strip()) or {})
    buenos = [(v.get("por_segundo") or 0.0, k) for k, v in suyo.items()
              if int(v.get("validos") or 0) >= min_validos]
    return max(buenos)[1] if buenos else None


def orden_aprendido(agente: str, variantes: Sequence[str], ruta: Path = RUTA) -> List[str]:
    """Las variantes, empezando por las que ya se sabe que pegan más fuerte.

    Lo no medido va después de lo medido, en su orden: así una tanda corta mide
    primero lo que promete, y si la batería se acaba a medias (pasó el 16 sep a
    los cinco tramos) lo perdido es lo menos interesante.
    """
    suyo = (leer(ruta).get((agente or "").strip()) or {})
    def peso(v: str) -> float:
        return -float((suyo.get(v) or {}).get("por_segundo") or -1e-9)
    return sorted(variantes, key=lambda v: (v not in suyo, peso(v)))


def quien_pelea(ruta: Path = EQUIPO) -> str:
    """El agente principal del equipo montado, para medir SUS combos.

    Las fichas de habilidades van por el nombre corto («Hoshimi Miyabi» está
    guardada como Miyabi), así que se devuelve ése. Cadena vacía si no consta:
    entonces se miden las variantes de siempre y no se inventa un agente.
    """
    try:
        d = json.loads(ruta.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""
    nombre = str(d.get("principal") or "").strip()
    if not nombre:
        equipo = d.get("equipo") or []
        nombre = str(equipo[0]).strip() if equipo else ""
    return nombre.split()[-1] if nombre else ""
