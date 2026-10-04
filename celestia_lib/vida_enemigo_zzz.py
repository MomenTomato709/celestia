"""La vida del enemigo de ZZZ en el vídeo pequeño del reflejo (192x86, RGB).

Sesión 74, 15 sep 2026. Para aprender los tiempos de cada combo hace falta un
juez que dé el juego, y el marcador PTS se para en 3000. La barra del enemigo
no: se grabó una pelea contra el enemigo de nivel 70 del Campo de pruebas (952
fotogramas) y en el vídeo la barra es UNA fila de 13-15 px:

  · la vida, en verde→amarillo, empieza SIEMPRE en verde;
  · el daño reciente, en rojo, justo después;
  · lo perdido, gris oscuro fundido con el borde (a tamaño real es gris medio);
  · debajo, un borde oscuro o el medidor de aturdimiento; a la derecha, un icono.

Va donde vaya el enemigo. El «3000 PTS» de arriba a la izquierda también es
amarillo con borde negro y la primera versión se quedó con él en el 100 % de los
fotogramas: su zona no cuenta. Con el enemigo lejos un píxel es un 7 % de vida,
así que el largo de la barra se suaviza (mediana de los últimos 9) y la vida se
da por segundos.

Es el espejo de `barra_enemigo()` en `agente_movil/reflejo_zzz.c`: los tests
pasan los mismos fotogramas por los dos y exigen las mismas cifras.
"""
from collections import deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

AN, AL = 192, 86
Y0, Y1 = 0.13, 0.605                  # por debajo del equipo, encima de los botones
LARGO_MIN, LARGO_MAX = 8 / 192, 34 / 192
SALTO_X = 14 / 192                    # más que esto entre dos vistas: otro sitio
MARCADOR = (0.25, 0.27)               # el «PTS», arriba a la izquierda
MEDIANA_DE = 9

Barra = Tuple[int, int, int, int, int]   # x0, y, lleno, rojo, vacío (px)


def clases(F: np.ndarray) -> Dict[str, np.ndarray]:
    A = F.astype(np.int16)
    r, g, b = A[..., 0], A[..., 1], A[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    K = mx < 55
    V = (g > 120) & (g > r + 25) & (b < 110)
    Y = (r > 140) & (g > 100) & (b < 90) & ~V
    R = (r > 110) & (g < 80) & (b < 80)
    W = (r > 170) & (g > 170) & (b > 170)
    G = (np.abs(r - g) < 20) & (np.abs(g - b) < 25) & (r >= 55) & (r <= 170)
    OSC = K | ((mx < 90) & (mx - mn < 35))
    SAT = (mx > 110) & (mx - mn > 70) & ~V & ~Y & ~R
    return {"K": K, "V": V, "Y": Y, "R": R, "W": W, "G": G, "OSC": OSC, "SAT": SAT}


def barra(F: np.ndarray) -> Optional[Barra]:
    """La barra del enemigo en un fotograma (alto x ancho x 3), o None si no se ve."""
    al, an = F.shape[:2]
    c = clases(F)
    relleno = c["V"] | c["Y"]
    bajo_ok = c["K"] | c["G"] | c["W"] | c["Y"] | c["R"] | c["OSC"]
    y0, y1 = int(Y0 * al), int(Y1 * al)
    lmin, lmax = int(LARGO_MIN * an + 0.5), int(LARGO_MAX * an + 0.5)
    mx_x, mx_y = int(MARCADOR[0] * an), int(MARCADOR[1] * al)
    mejor, mejor_nota = None, 0.0
    for y in range(y0, y1):
        V, K, R, OSC, SAT, fila = c["V"][y], c["K"][y], c["R"][y], c["OSC"][y], c["SAT"][y], relleno[y]
        x = 0
        while x < an:
            if not V[x]:
                x += 1
                continue
            x0 = x
            while x < an and (fila[x] or (x + 1 < an and fila[x + 1] and not K[x])):
                x += 1
            x1 = x
            while x < an and R[x]:
                x += 1
            xr = fin = x
            fallos = 0
            # lo perdido: oscuro, tolerando UN píxel suelto; para en el icono
            while x < an and not SAT[x]:
                if OSC[x] or R[x]:
                    fin, fallos = x + 1, 0
                else:
                    fallos += 1
                    if fallos > 1:
                        break
                x += 1
            xk = max(fin, xr)
            largo = xk - x0
            x = x1
            if not lmin <= largo <= lmax or (x0 < mx_x and y < mx_y):
                continue
            debajo = max(float(bajo_ok[yy, x0:xk].mean()) for yy in (y + 1, y + 2) if yy < al)
            encima = float(relleno[y - 1, x0:x1].mean()) if y > 0 else 0.0
            if debajo < 0.7 or encima > 0.5:
                continue
            nota = debajo * largo
            if nota > mejor_nota:
                mejor, mejor_nota = (x0, y, x1 - x0, xr - x1, xk - xr), nota
    return mejor


class SeguidorDeVida:
    """La vida en milésimas con el largo de la barra suavizado.

    El largo cambia despacio (es la distancia) y un fotograma en que el final se
    ve mal no debe hundir la vida. Si la barra salta de sitio, es otra distancia
    u otro enemigo: el largo se mide de cero.
    """

    def __init__(self, an: int = AN) -> None:
        self.largos: deque = deque(maxlen=MEDIANA_DE)
        self.x: Optional[int] = None
        self.salto = int(SALTO_X * an + 0.5)

    def vida(self, b: Optional[Barra]) -> Optional[int]:
        if b is None:
            return None
        x0, _y, lleno, rojo, vacio = b
        if self.x is not None and abs(x0 - self.x) > self.salto:
            self.largos.clear()
        self.x = x0
        self.largos.append(lleno + rojo + vacio)
        ref = float(np.median(self.largos))
        return min(1000, int(1000.0 * lleno / ref)) if ref > 0 else 0


def vida_por_segundo(fotogramas: Iterable[np.ndarray], fps: float) -> List[Tuple[int, float, int]]:
    """(ms, vida 0-1, fotogramas con barra) de cada segundo en que se vio: lo mismo
    que imprime el binario en sus líneas «enemigo»."""
    seguidor: Optional[SeguidorDeVida] = None
    fuera: List[Tuple[int, float, int]] = []
    actual, vidas = -1, []

    def cerrar() -> None:
        if actual >= 0 and vidas:
            fuera.append((actual * 1000, float(np.median(vidas)) / 1000.0, len(vidas)))

    for i, F in enumerate(fotogramas):
        s = int(i * 1000.0 / fps) // 1000
        if s != actual:
            cerrar()
            actual, vidas = s, []
        if seguidor is None:
            seguidor = SeguidorDeVida(F.shape[1])
        v = seguidor.vida(barra(F))
        if v is not None:
            vidas.append(v)
    cerrar()
    return fuera


def leer_volcado(ruta: Path, an: int = AN, al: int = AL) -> np.ndarray:
    """Los fotogramas que guarda el reflejo con --volcar, sin cargarlos en memoria."""
    n = Path(ruta).stat().st_size // (an * al * 3)
    return np.memmap(ruta, dtype=np.uint8, mode="r", shape=(n, al, an, 3))


VISTOS_MIN = 8            # de ~31 fotogramas por segundo: con menos, ese segundo no cuenta
REAPARECE_DESDE = 0.85    # sin hueco de barra, la vida tiene que volver casi llena…
REAPARECE_TRAS_HUECO = 0.6  # …con hueco basta esto: hay enemigos cuya barra entera se lee 0,7-0,8
REAPARECE_SUBE = 0.3      # …y muy por encima de lo último: murió y salió otro enemigo
HUECO_DE_MUERTE_S = 2.0   # la barra desaparece mientras muere y reaparece


def _recta(puntos: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Recta de Theil-Sen (pendiente, ordenada): la mediana de las pendientes entre cada par.

    Aguanta que casi un tercio de las lecturas sean disparates, que es lo que da
    la barra en el vídeo pequeño.
    """
    pendientes = [(v2 - v1) / (t2 - t1) for i, (t1, v1) in enumerate(puntos)
                  for t2, v2 in puntos[i + 1:] if t2 > t1]
    p = float(np.median(pendientes)) if pendientes else 0.0
    return p, float(np.median([v - p * t for t, v in puntos]))


def vidas_del_enemigo(serie: Iterable[Tuple[int, float, int]]) -> List[List[Tuple[float, float]]]:
    """Los segundos bien vistos, (s, vida), en un trozo por cada enemigo.

    Sale otro cuando la vida vuelve muy por encima de lo último. Si la barra
    desapareció un rato (muere y reaparece), basta con que la mediana de las tres
    lecturas siguientes pase de `REAPARECE_TRAS_HUECO`: el enemigo que puso Enzo
    el 15 sep reaparecía leyéndose 0,71-0,82, no lleno. Si no desapareció, las tres
    tienen que verse llenas: dos lecturas altas sueltas son baile de la medida.
    """
    puntos = [(ms / 1000.0, v) for ms, v, n in serie if n >= VISTOS_MIN]
    vidas: List[List[Tuple[float, float]]] = [[]]
    for i, (t, v) in enumerate(puntos):
        actual = vidas[-1]
        luego = [w for _t, w in puntos[i:i + 3]]
        if len(actual) >= 3 and len(luego) >= 2:
            hubo_hueco = t - actual[-1][0] >= HUECO_DE_MUERTE_S
            llena = (float(np.median(luego)) >= REAPARECE_TRAS_HUECO if hubo_hueco
                     else min(luego) >= REAPARECE_DESDE)
            antes = float(np.median([w for _t, w in actual[-3:]]))
            if llena and float(np.median(luego)) - antes >= REAPARECE_SUBE:
                vidas.append([])
        vidas[-1].append((t, v))
    return [v for v in vidas if v]


def vida_quitada(serie: Iterable[Tuple[int, float, int]]) -> Tuple[float, int]:
    """(barras de vida quitadas, enemigos muertos) de las líneas «enemigo» de un tramo.

    El juez de los combos. La lectura de un segundo puede errar media barra (en
    la pelea real del 15 sep: 0,70 → 0,23 → 0,50 en tres segundos), así que ni se
    suman las bajadas ni se toma la más baja: en cada enemigo se ajusta una recta
    robusta y lo quitado es lo que baja de principio a fin. El enemigo del Campo
    de pruebas reaparece solo al morir (lo confirmó Enzo): al que murió se le
    cuenta entero lo que tenía al empezar.
    """
    vidas = vidas_del_enemigo(serie)
    quitado = 0.0
    for k, vida in enumerate(vidas):
        if len(vida) >= 3:
            p, b = _recta(vida)
            al_empezar = min(1.0, max(0.0, b + p * vida[0][0]))
            al_acabar = min(1.0, max(0.0, b + p * vida[-1][0]))
        else:
            al_empezar = al_acabar = float(np.median([v for _t, v in vida]))
        murio = k < len(vidas) - 1
        quitado += al_empezar if murio else max(0.0, al_empezar - al_acabar)
    return quitado, max(0, len(vidas) - 1)
