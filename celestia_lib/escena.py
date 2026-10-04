"""En qué tipo de pantalla está: menú, mundo, combate o diálogo.

Enzo, 10 sep 2026: «la forma en la que Celestia aprende son dos: si hay texto es
menú y si no es combate. Cuando el juego tiene mapa donde no hay combate y es
más interactuar, moverse por el mapa…».

Tenía razón y era un fallo de raíz. Con dos estados, una pantalla de mundo sin
texto se tomaba por una pelea, y una con texto por un menú: mal las dos veces. Y
de ahí salía todo lo demás — tocaba botones donde había que andar, y buscaba
rótulos donde había que esquivar.

Los cuatro sitios donde puede estar un juego de acción en un móvil, y qué se
hace en cada uno:

    MENÚ ...... botones con fondo liso. Se TOCA.
    DIÁLOGO ... texto abajo, a veces un retrato. Se toca para pasar.
    MUNDO ..... se anda con el joystick, se gira la cámara, se interactúa.
    COMBATE ... botones de acción a la derecha; se golpea y se esquiva.

## Por qué se decide con reglas y no preguntando al modelo

Porque esto se pregunta en CADA vuelta, y una mirada son 1.556 tokens de una
cuota que ya es el muro. Las señales que se usan aquí —cuántos botones ve el
marcador, dónde cae el texto, cuánto se mueve la pantalla sola— salen de cosas
que el jugador ya calcula para otra cosa, así que son gratis.

Y cuando no está claro, se dice `""` en vez de inventarse una: no saber es un
resultado, y quien pregunte tiene que distinguirlo de saber que es un menú.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

logger = logging.getLogger("celestia_v1")

MENU = "menu"
DIALOGO = "dialogo"
MUNDO = "mundo"
COMBATE = "combate"

# Cuántos botones sobre fondo liso hacen un menú. El marcador busca manchas que
# no son fondo: en un menú encuentra muchas, y sobre el arte de un juego
# encuentra cero (medido en la sesión 61).
CAJAS_DE_MENU = 6

# Dónde empieza la banda de abajo, donde los juegos ponen los diálogos.
BANDA_DE_DIALOGO = 0.62

# Cuánto texto hay que leer ahí abajo para llamarlo diálogo. Un botón suelto no
# es una conversación.
LETRAS_DE_DIALOGO = 25

# Dónde viven los botones de acción en un combate: abajo a la derecha.
ZONA_DE_ACCION = (0.62, 0.55)

# Por encima de esto, la pantalla cambia demasiado para ser un menú: sólo una
# pelea (o una cinemática) se mueve así.
MOVIMIENTO_DE_PELEA = 0.15


def deducir(cajas: Sequence, palabras: Sequence,
            movimiento: Optional[float] = None,
            controles_visibles: bool = False) -> str:
    """Qué clase de pantalla es. `""` si no está claro.

    `movimiento` es cuánto ha cambiado la pantalla **sin tocar nada**, de 0 a 1.
    Es la señal que separa un mundo quieto de una pelea: en una pelea nunca
    está quieto.
    """
    abajo = _texto_de_abajo(palabras)
    if len(abajo) >= LETRAS_DE_DIALOGO and len(cajas) < CAJAS_DE_MENU:
        return DIALOGO
    # Un menú con animación se mueve un poco; una pelea se mueve MUCHO. Por
    # encima de este listón no hay menú que valga, y hace falta porque en el
    # campo de pruebas de ZZZ el fondo es liso y el marcador encuentra
    # «botones» por todas partes en plena pelea.
    if movimiento is not None and movimiento >= MOVIMIENTO_DE_PELEA:
        return COMBATE
    if len(cajas) >= CAJAS_DE_MENU:
        return MENU
    if controles_visibles:
        # Con los botones de acción a la vista, lo único que distingue mundo de
        # combate es si aquello se mueve solo.
        if movimiento is not None and movimiento >= 0.05:
            return COMBATE
        return MUNDO
    if movimiento is not None and movimiento >= 0.08:
        return COMBATE
    return ""


def _texto_de_abajo(palabras: Sequence) -> str:
    letras: List[str] = []
    for p in palabras:
        centro = getattr(p, "centro", None)
        if centro is None or getattr(centro, "y", 0) < BANDA_DE_DIALOGO:
            continue
        texto = (getattr(p, "texto", "") or "").strip()
        if texto:
            letras.append(texto)
    return " ".join(letras)


def como_se_juega_aqui(escena: str) -> str:
    """Qué recordarle al modelo según dónde esté. Vacío si no se sabe.

    Es corto a propósito: cada línea de más son tokens en cada mirada, y la
    cuota es lo que limita cuántas jugadas por minuto se pueden dar.
    """
    if escena == MUNDO:
        return ("Estás en el mundo, no en un menú: para moverte usa \"andar\" "
                "con el joystick (dx/dy y ms), y \"camara\" para girar la "
                "vista. Tocar la pantalla aquí no te mueve.\n")
    if escena == COMBATE:
        return ("Estás en un combate: golpea con el botón grande de abajo a la "
                "derecha, esquiva cuando algo destelle y usa la definitiva "
                "cuando esté cargada.\n")
    if escena == DIALOGO:
        return ("Esto es una conversación: se pasa tocando la pantalla, y si "
                "hay opciones se elige una.\n")
    return ""
