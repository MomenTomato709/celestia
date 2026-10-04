"""Lo poco de `scipy.ndimage` que usa Celestia, hecho con numpy (y sin scipy).

La app de Android no puede llevar scipy: Chaquopy no lo tiene compilado para
Python 3.12 (comprobado el 4 oct 2026), y el calco de fotos (lineas.py) y el
buscador de botones (botones.py) se quedaban sin funcionar («Aquí todavía no
puedo convertir fotos»). Esto es la misma cuenta, con las mismas reglas que
scipy por defecto —vecindad en cruz, bordes en espejo («reflect»), el
etiquetado numerado en el orden en que aparece cada mancha—, y se usa sólo
donde scipy no está:

    try:
        from scipy import ndimage
    except ImportError:
        from . import ndimage_lite as ndimage

tests/test_ndimage_lite.py lo compara con scipy de verdad.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np

_CRUZ = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


def ndimage_o_lite():
    """`scipy.ndimage` si está (el PC, el móvil de Termux); si no, este módulo."""
    try:
        from scipy import ndimage
        return ndimage
    except ImportError:
        import sys
        return sys.modules[__name__]


def _desplazado(a: np.ndarray, dy: int, dx: int, relleno) -> np.ndarray:
    """`a` movido (dy, dx): out[y, x] = a[y - dy, x - dx]; lo que entra, `relleno`."""
    out = np.full_like(a, relleno)
    h, w = a.shape
    ys = slice(max(dy, 0), h + min(dy, 0))
    xs = slice(max(dx, 0), w + min(dx, 0))
    yo = slice(max(-dy, 0), h + min(-dy, 0))
    xo = slice(max(-dx, 0), w + min(-dx, 0))
    out[ys, xs] = a[yo, xo]
    return out


def _desplazamientos(estructura) -> List[Tuple[int, int]]:
    e = _CRUZ if estructura is None else np.asarray(estructura, dtype=bool)
    cy, cx = e.shape[0] // 2, e.shape[1] // 2
    return [(y - cy, x - cx) for y, x in zip(*np.nonzero(e))]


def binary_dilation(entrada, structure=None, iterations: int = 1) -> np.ndarray:
    a = np.asarray(entrada, dtype=bool)
    pasos = _desplazamientos(structure)
    for _ in range(max(iterations, 1)):
        out = np.zeros_like(a)
        for dy, dx in pasos:
            out |= _desplazado(a, dy, dx, False)
        a = out
    return a


def binary_erosion(entrada, structure=None, iterations: int = 1) -> np.ndarray:
    # Como scipy (border_value=0): fuera de la imagen cuenta como vacío.
    a = np.asarray(entrada, dtype=bool)
    pasos = _desplazamientos(structure)
    for _ in range(max(iterations, 1)):
        out = np.ones_like(a)
        for dy, dx in pasos:
            out &= _desplazado(a, -dy, -dx, False)
        a = out
    return a


def binary_closing(entrada, structure=None, iterations: int = 1) -> np.ndarray:
    return binary_erosion(binary_dilation(entrada, structure, iterations), structure, iterations)


def binary_opening(entrada, structure=None, iterations: int = 1) -> np.ndarray:
    return binary_dilation(binary_erosion(entrada, structure, iterations), structure, iterations)


def label(entrada, structure=None) -> Tuple[np.ndarray, int]:
    """Manchas conectadas (en cruz; con una estructura 3×3 llena, también en
    diagonal), numeradas 1, 2… en el orden en que aparecen leyendo por filas.

    Por tramos: cada fila se parte en tramos seguidos de píxeles y se unen los
    tramos de filas vecinas que se tocan. Así el bucle de Python va por tramos,
    no por píxeles (una foto de un millón de píxeles son unos miles)."""
    a = np.asarray(entrada, dtype=bool)
    if a.ndim != 2:
        raise ValueError("label: sólo imágenes 2D")
    diagonal = structure is not None and bool(np.asarray(structure, dtype=bool).all())
    h, w = a.shape
    borde = np.zeros((h, w + 2), dtype=np.int8)
    borde[:, 1:-1] = a
    cambio = np.diff(borde, axis=1)
    fil, ini = np.nonzero(cambio == 1)
    _f, fin = np.nonzero(cambio == -1)              # fin exclusivo, mismo orden
    n = len(fil)
    salida = np.zeros((h, w), dtype=np.int32)
    if not n:
        return salida, 0
    padre = list(range(n))

    def raiz(i: int) -> int:
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    holgura = 1 if diagonal else 0
    corte = np.searchsorted(fil, np.arange(h + 1)).tolist()
    ini_l, fin_l = ini.tolist(), fin.tolist()
    for f in range(1, h):
        i, i1 = corte[f - 1], corte[f]
        j, j1 = corte[f], corte[f + 1]
        while i < i1 and j < j1:
            if ini_l[i] < fin_l[j] + holgura and ini_l[j] < fin_l[i] + holgura:
                ri, rj = raiz(i), raiz(j)
                if ri != rj:
                    padre[max(ri, rj)] = min(ri, rj)
            if fin_l[i] < fin_l[j]:
                i += 1
            else:
                j += 1
    numero = {}
    etiqueta = np.empty(n, dtype=np.int32)
    for k in range(n):
        r = raiz(k)
        if r not in numero:
            numero[r] = len(numero) + 1
        etiqueta[k] = numero[r]
    largos = fin - ini
    filas = np.repeat(fil, largos)
    desde = np.repeat(np.cumsum(largos) - largos, largos)
    columnas = np.repeat(ini, largos) + (np.arange(int(largos.sum())) - desde)
    salida[filas, columnas] = np.repeat(etiqueta, largos)
    return salida, len(numero)


def find_objects(etiquetas) -> List[Optional[Tuple[slice, slice]]]:
    e = np.asarray(etiquetas)
    maximo = int(e.max()) if e.size else 0
    if maximo <= 0:
        return []
    ys, xs = np.nonzero(e > 0)
    cual = e[ys, xs] - 1
    y0 = np.full(maximo, np.iinfo(np.int64).max)
    x0 = y0.copy()
    y1 = np.full(maximo, -1)
    x1 = y1.copy()
    np.minimum.at(y0, cual, ys)
    np.minimum.at(x0, cual, xs)
    np.maximum.at(y1, cual, ys)
    np.maximum.at(x1, cual, xs)
    return [None if y1[i] < 0 else (slice(int(y0[i]), int(y1[i]) + 1),
                                     slice(int(x0[i]), int(x1[i]) + 1))
            for i in range(maximo)]


def binary_fill_holes(entrada, structure=None) -> np.ndarray:
    """Rellena los huecos: el fondo que no llega al borde de la imagen."""
    a = np.asarray(entrada, dtype=bool)
    fondo, _n = label(~a, structure)
    tocan = np.unique(np.concatenate([fondo[0], fondo[-1], fondo[:, 0], fondo[:, -1]]))
    exterior = np.isin(fondo, tocan[tocan > 0])
    return ~exterior


# ── Filtros (bordes en espejo, como el «reflect» de scipy) ─────────────────
def _nucleo_gauss(sigma: float, truncate: float = 4.0) -> np.ndarray:
    radio = int(truncate * float(sigma) + 0.5)
    x = np.arange(-radio, radio + 1, dtype=np.float64)
    phi = np.exp(-0.5 / (float(sigma) ** 2) * x * x)
    return phi / phi.sum()


def _correlar1d(a: np.ndarray, pesos: np.ndarray, eje: int) -> np.ndarray:
    radio = len(pesos) // 2
    a = np.moveaxis(np.asarray(a, dtype=np.float64), eje, -1)
    relleno = [(0, 0)] * (a.ndim - 1) + [(radio, radio)]
    p = np.pad(a, relleno, mode="symmetric")
    out = np.zeros_like(a)
    n = a.shape[-1]
    for k, peso in enumerate(pesos):
        out += peso * p[..., k:k + n]
    return np.moveaxis(out, -1, eje)


def _tipo(original, resultado: np.ndarray) -> np.ndarray:
    tipo = np.asarray(original).dtype
    if np.issubdtype(tipo, np.integer):
        return np.round(resultado).astype(tipo)
    return resultado.astype(tipo if np.issubdtype(tipo, np.floating) else np.float64)


def gaussian_filter1d(entrada, sigma: float, axis: int = -1, truncate: float = 4.0) -> np.ndarray:
    return _tipo(entrada, _correlar1d(entrada, _nucleo_gauss(sigma, truncate), axis))


def gaussian_filter(entrada, sigma: float, truncate: float = 4.0) -> np.ndarray:
    pesos = _nucleo_gauss(sigma, truncate)
    a = np.asarray(entrada)
    out = a.astype(np.float64)
    for eje in range(a.ndim):
        out = _correlar1d(out, pesos, eje)
    return _tipo(entrada, out)


def _ventana(size: int) -> Tuple[int, int]:
    """Cuánto mira a cada lado (el origen de scipy: centrado, y en tamaños
    pares un píxel más hacia atrás)."""
    antes = size // 2
    return antes, size - 1 - antes


def maximum_filter(entrada, size: int) -> np.ndarray:
    a = np.asarray(entrada)
    antes, despues = _ventana(int(size))
    out = a
    for eje in range(a.ndim):
        m = np.moveaxis(out, eje, -1)
        relleno = [(0, 0)] * (m.ndim - 1) + [(antes, despues)]
        p = np.pad(m, relleno, mode="symmetric")
        n = m.shape[-1]
        r = p[..., 0:n].copy()
        for k in range(1, antes + despues + 1):
            np.maximum(r, p[..., k:k + n], out=r)
        out = np.moveaxis(r, -1, eje)
    return out


def median_filter(entrada, size: int) -> np.ndarray:
    """Mediana en una ventana cuadrada, por trozos de filas para no gastar
    memoria (una ventana de 9×9 sobre una foto entera serían cientos de MB)."""
    a = np.asarray(entrada)
    size = int(size)
    antes, despues = _ventana(size)
    p = np.pad(a, ((antes, despues), (antes, despues)), mode="symmetric")
    h, w = a.shape
    out = np.empty_like(a)
    from numpy.lib.stride_tricks import sliding_window_view
    trozo = max(1, 4_000_000 // max(1, w * size * size))
    for f in range(0, h, trozo):
        bloque = p[f:f + min(trozo, h - f) + size - 1]
        vent = sliding_window_view(bloque, (size, size))
        out[f:f + vent.shape[0]] = np.median(vent, axis=(-2, -1)).astype(a.dtype)
    return out


def map_coordinates(entrada, coordinates: Sequence, order: int = 1,
                    cval: float = 0.0) -> np.ndarray:
    """Interpolación bilineal (order=1, modo «constant» de scipy: fuera de la
    imagen, `cval`)."""
    if order != 1:
        raise NotImplementedError("map_coordinates: sólo order=1")
    a = np.asarray(entrada, dtype=np.float64)
    yy = np.asarray(coordinates[0], dtype=np.float64)
    xx = np.asarray(coordinates[1], dtype=np.float64)
    h, w = a.shape
    fuera = (yy < 0) | (yy > h - 1) | (xx < 0) | (xx > w - 1)
    y = np.clip(yy, 0, h - 1)
    x = np.clip(xx, 0, w - 1)
    y0 = np.floor(y).astype(np.intp)
    x0 = np.floor(x).astype(np.intp)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy, fx = y - y0, x - x0
    arriba = a[y0, x0] * (1 - fx) + a[y0, x1] * fx
    abajo = a[y1, x0] * (1 - fx) + a[y1, x1] * fx
    out = arriba * (1 - fy) + abajo * fy
    out[fuera] = cval
    return _tipo(entrada, out)
