"""Encontrar los botones redondos de un juego de móvil, sin preguntarle a nadie.

10 sep 2026. El descubridor de controles preguntaba al modelo a qué ALTURA está
cada botón y luego tocaba ahí para ver si «reaccionaba». Las dos mitades
fallaban:

- la altura la contestó mal (dijo 0,33 y 0,22, que en la pantalla de ZZZ es el
  escenario);
- y «reaccionó» no significa nada en un juego 3D: tocar CUALQUIER sitio mueve
  la cámara, así que la pantalla siempre cambia.

Resultado: dio por comprobados dos controles que estaban sobre el suelo del
escenario. Y eso es peor que no tener ninguno, porque el jugador se los cree.

Mirando la captura de la pelea se ve lo que había que ver desde el principio:
**los botones de acción son círculos**, todos parecidos, agrupados a la derecha.
Eso no hay que preguntarlo — se mide. Aquí se detectan por forma, con lo que ya
está instalado (numpy + scipy), y sale una lista de centros ordenados por
tamaño: en un juego de acción el más grande es el de atacar.
"""

from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger("celestia_v1")

# Dónde mirar: la mitad derecha, de media pantalla hacia abajo. Es donde todo
# juego de móvil pone los botones de acción, y mirar sólo ahí evita confundir
# con lo redondo del escenario o de la interfaz de arriba.
ZONA_DE_ACCION = (0.55, 0.30, 1.0, 1.0)      # x0, y0, x1, y1 en fracción

# Un botón ocupa entre esto y esto de la altura de la pantalla. Por debajo son
# iconos de adorno; por encima, trozos de escenario.
MIN_LADO = 0.05
MAX_LADO = 0.22

# Cuánto puede alejarse de un círculo: alto y ancho parecidos, y el área tiene
# que llenar buena parte de su caja (un círculo llena π/4 ≈ 0,785).
PROPORCION = 0.65
LLENADO = 0.55


def circulos(png: bytes, zona: Optional[Tuple[float, float, float, float]] = None
             ) -> List[Tuple[float, float, float]]:
    """Los botones redondos que hay: [(x, y, tamaño)] en fracción, de mayor a menor.

    El tamaño es el lado de su caja, también en fracción, y sirve para
    ordenarlos: en un juego de acción el botón grande es el de atacar y los
    pequeños de alrededor son las habilidades.
    """
    try:
        import numpy as np
        from PIL import Image
        from .ndimage_lite import ndimage_o_lite  # la app de Android no tiene scipy
        ndimage = ndimage_o_lite()
    except ImportError as e:                       # pragma: no cover
        logger.warning("sin numpy/scipy/PIL no puedo buscar botones (%s)", e)
        return []
    try:
        im = Image.open(io.BytesIO(png)).convert("L")
    except Exception as e:
        logger.warning("no pude abrir la captura (%s)", e)
        return []

    an, al = im.size
    x0, y0, x1, y1 = zona or ZONA_DE_ACCION
    caja = (int(x0 * an), int(y0 * al), int(x1 * an), int(y1 * al))
    recorte = np.asarray(im.crop(caja), dtype=float)
    if recorte.size == 0:
        return []
    rec_al, rec_an = recorte.shape

    # Los bordes: un botón es un contorno cerrado, y lo que lo delata es el
    # cambio brusco de luz. Se buscan bordes y no colores porque el botón puede
    # ser claro sobre oscuro o al revés según el juego y la escena.
    gy, gx = np.gradient(recorte)
    fuerza = np.hypot(gx, gy)
    # El percentil no se supone, se midió sobre la pelea real de ZZZ: con 75,
    # 82 y 88 salían DOS botones de los cinco que hay, y con 93 los cinco. Un
    # umbral bajo deja pasar el ruido del escenario, se pega a los botones al
    # dilatar y los funde con el fondo.
    umbral = float(np.percentile(fuerza, 93))
    bordes = fuerza > max(umbral, 8.0)
    # Cerrar el contorno: los bordes salen finos y entrecortados. Dos pasos, no
    # tres: con tres se perdía uno de los cinco (medido en la misma captura).
    gordos = ndimage.binary_dilation(bordes, iterations=2)
    llenos = ndimage.binary_fill_holes(gordos)
    if llenos is None:
        return []
    llenos = ndimage.binary_erosion(llenos, iterations=2)

    etiquetas, cuantas = ndimage.label(llenos)
    if not cuantas:
        return []
    fuera: List[Tuple[float, float, float]] = []
    for i, rebanada in enumerate(ndimage.find_objects(etiquetas), start=1):
        if rebanada is None:
            continue
        fy, fx = rebanada
        alto = fy.stop - fy.start
        ancho = fx.stop - fx.start
        lado_al = alto / al
        lado_an = ancho / an
        if not (MIN_LADO <= lado_al <= MAX_LADO):
            continue
        # Redondo: alto y ancho parecidos. En una pantalla apaisada hay que
        # comparar en PÍXELES, no en fracción, o cualquier cosa parece ancha.
        if min(alto, ancho) / max(alto, ancho) < PROPORCION:
            continue
        area = int((etiquetas[rebanada] == i).sum())
        if area / float(alto * ancho) < LLENADO:
            continue
        cx = (caja[0] + fx.start + ancho / 2) / an
        cy = (caja[1] + fy.start + alto / 2) / al
        fuera.append((cx, cy, max(lado_al, lado_an)))
    fuera.sort(key=lambda c: -c[2])
    return fuera


def el_de_atacar(botones: List[Tuple[float, float, float]]
                 ) -> Optional[Tuple[float, float]]:
    """El botón de golpear: el más grande y el más a la derecha y abajo.

    Es la convención de todos los juegos de acción en móvil, y por eso se puede
    dar por buena sin preguntar: el resto de botones de la mano derecha son más
    pequeños y se colocan alrededor de él.
    """
    if not botones:
        return None
    mayor = botones[0][2]
    grandes = [b for b in botones if b[2] >= mayor * 0.9]
    elegido = max(grandes, key=lambda b: b[0] + b[1])
    return (elegido[0], elegido[1])
