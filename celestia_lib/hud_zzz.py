"""Leer la interfaz de una pelea de ZZZ: quién está en el campo y qué está listo.

10 sep 2026, con el equipo de Enzo en el campo de pruebas. Para jugar un equipo
hay que saber cosas que no dice ningún texto —quién está dentro, si su EX está
cargado, si el juego ofrece una asistencia— y todas se ven en la interfaz FIJA
de la pelea. Aquí se leen midiendo colores, sin modelo y sin OCR.

Cifras medidas sobre capturas del móvil de Enzo (guardadas en
`logs/zzz_ref/hud_*.png`, que es contra lo que van las pruebas):

    EX (la estrella) ....... anillo de color: saturación 118-185 encendida, 2 gris
    relevo (amarillo) ...... centro amarillo s≈204; con «ASSIST» enseña un
                             retrato y cae a 13-29
    atacar (el puño) ....... gris estable (111,113,112): la interfaz está a la vista
    retrato grande ......... cambia un 19-32 % al pulsar el relevo, 0 % al pulsar el EX;
                             parecido de la misma agente 1,00 y de dos distintas 0,44-0,66

⚠️ Lo que se probó para saber QUIÉN es cada agente y no sirve (S72): comparar el
retrato con los iconos de la wiki. El retrato grande es otro dibujo, y a Yixuan
ni el pequeño se le parece. Aquí el retrato se compara contra la PROPIA interfaz
—el que se vio cuando se sabía quién estaba dentro—, que es comparar el juego
consigo mismo.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image

logger = logging.getLogger("celestia_v1")

Imagen = Union[bytes, bytearray, np.ndarray, Image.Image]

# ── Dónde está cada cosa, en fracción de la pantalla apaisada ─────────────
PUNTO_ATACAR = (0.8125, 0.753)
PUNTO_EX = (0.740, 0.845)
PUNTO_RELEVO = (0.8855, 0.657)
PUNTO_DEFINITIVA = (0.8855, 0.468)
PUNTO_ESQUIVAR = (0.8855, 0.845)
RETRATO_ACTIVO = (205 / 2412, 63 / 1084, 360 / 2412, 127 / 1084)

# El anillo donde un botón pinta su color, en fracción del ALTO de la pantalla.
# El centro no vale: el dibujo del icono es oscuro encendido o apagado (s 7-25).
ANILLO = (20 / 1084, 35 / 1084)

SAT_ENCENDIDO = 60           # anillo: 118-185 encendido, 2 gris
SAT_RELEVO_NORMAL = 120      # centro del relevo: ~204 amarillo, 13-29 con ASSIST
BRILLO_ATACAR = (95, 130)    # el puño gris, visto de cerca
SAT_ATACAR = 14
# El puño va dibujado sobre un disco casi negro. Medido en las 15 capturas de
# pelea: el 60-61 % del círculo de radio 0,06·alto es oscuro (canal máximo < 50);
# una pantalla gris lisa da un 0 %.
DISCO_ATACAR = 0.06          # radio, en fracción del ALTO
OSCURO = 50
DISCO_OSCURO_MIN = 0.40
CAMBIO_DE_RELEVO = 0.08      # retrato grande: 0,19-0,32 al relevar
MISMO_RETRATO = 0.85         # misma agente 1,00 · distintas 0,44-0,66

# Lo mismo para el vigía, que mira un VÍDEO de 96 px de ancho: ahí un botón mide
# dos píxeles y el anillo se mezcla con el centro. Medido reescalando las mismas
# capturas: EX 70-112 encendido y 3 gris; relevo ~152 amarillo y 25-38 con ASSIST.
VIDEO_SAT_ENCENDIDO = 35
VIDEO_SAT_RELEVO = 90


def _rgb(img: Imagen) -> np.ndarray:
    if isinstance(img, np.ndarray):
        return img.astype(float)
    if isinstance(img, (bytes, bytearray)):
        img = Image.open(io.BytesIO(img))
    return np.asarray(img.convert("RGB"), dtype=float)


def _media_cerca(A: np.ndarray, punto: Tuple[float, float], lado: int = 13) -> np.ndarray:
    """Color medio de un cuadrado pequeño: un píxel suelto es ruido de compresión."""
    al, an, _ = A.shape
    x, y, m = int(punto[0] * an), int(punto[1] * al), lado // 2
    z = A[max(0, y - m):y + m + 1, max(0, x - m):x + m + 1].reshape(-1, 3)
    return z.mean(axis=0) if len(z) else np.zeros(3)


def saturacion_anillo(img: Imagen, centro: Tuple[float, float],
                      anillo: Tuple[float, float] = ANILLO) -> float:
    """Saturación mediana del anillo de un botón redondo. 0 = gris."""
    A = _rgb(img)
    al, an, _ = A.shape
    cx, cy = centro[0] * an, centro[1] * al
    r0, r1 = anillo[0] * al, anillo[1] * al
    x0, x1 = int(max(0, cx - r1)), int(min(an, cx + r1 + 1))
    y0, y1 = int(max(0, cy - r1)), int(min(al, cy + r1 + 1))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    sub = A[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - cx, yy - cy)
    zona = sub[(d >= r0) & (d < r1)]
    if not len(zona):
        return 0.0
    return float(np.median(zona.max(axis=1) - zona.min(axis=1)))


def encendido(img: Imagen, centro: Tuple[float, float]) -> bool:
    return saturacion_anillo(img, centro) >= SAT_ENCENDIDO


def ex_listo(img: Imagen) -> bool:
    """¿Tiene energía para el EX? La estrella se colorea cuando sí."""
    return encendido(img, PUNTO_EX)


def definitiva_lista(img: Imagen) -> bool:
    return encendido(img, PUNTO_DEFINITIVA)


def interfaz_a_la_vista(img: Imagen) -> bool:
    """¿Se ve la interfaz de pelea? El puño gris es lo único que nunca cambia.

    Hace falta como guardia: durante una cadena, una definitiva o una carga la
    interfaz desaparece, y un botón «sin amarillo» ahí no significa ASSIST.

    🔴 19 sep: con mirar sólo el gris del puño, la pantalla de carga del juego
    —gris lisa— pasó por pelea, y el catálogo de pantallas se la aprendió con esa
    etiqueta. Por eso se exige también el disco oscuro sobre el que va el puño.
    """
    A = _rgb(img)
    # Los 13 px se midieron a 1084 de alto. En los vídeos de 432 abarcaban el
    # trazo del puño y el negro de al lado, y no se veía ni una pelea de Enzo.
    m = _media_cerca(A, PUNTO_ATACAR, lado=max(3, round(13 * A.shape[0] / 1084)))
    brillo = float(m.mean())
    if not (BRILLO_ATACAR[0] <= brillo <= BRILLO_ATACAR[1] and float(m.max() - m.min()) < SAT_ATACAR):
        return False
    return _fraccion_oscura(A, PUNTO_ATACAR, DISCO_ATACAR) >= DISCO_OSCURO_MIN


def _fraccion_oscura(A: np.ndarray, centro: Tuple[float, float], radio: float) -> float:
    """Qué parte del círculo (radio en fracción del alto) es casi negra."""
    al, an, _ = A.shape
    cx, cy, r = centro[0] * an, centro[1] * al, radio * al
    x0, x1 = int(max(0, cx - r)), int(min(an, cx + r + 1))
    y0, y1 = int(max(0, cy - r)), int(min(al, cy + r + 1))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    yy, xx = np.mgrid[y0:y1, x0:x1]
    zona = A[y0:y1, x0:x1][np.hypot(xx - cx, yy - cy) < r]
    return float((zona.max(axis=1) < OSCURO).mean()) if len(zona) else 0.0


def ofrece_asistencia(img: Imagen) -> bool:
    """¿El botón de relevo enseña «ASSIST»? Pierde el amarillo y enseña un retrato."""
    A = _rgb(img)
    if not interfaz_a_la_vista(A):
        return False
    m = _media_cerca(A, PUNTO_RELEVO)
    return float(m.max() - m.min()) < SAT_RELEVO_NORMAL


# ── La vida del enemigo (sesión 74) ───────────────────────────────────────
# Para aprender tiempos hay que ver si un golpe ENTRA, y el marcador PTS se
# queda en 3000 en pocas vueltas. La barra verde que el juego pinta junto al
# enemigo sí se mueve con cada golpe si no es invencible. Medida en las tres
# capturas de pelea: verde (69, 150, 49) de media, 129-130 × 21 px a 2412 de
# ancho, del mismo tamaño aunque el enemigo esté en otro sitio de la pantalla.
# ⚠️ Sin calibrar aún CÓMO se vacía: en las tres el enemigo estaba entero.
BANDA_ENEMIGO = (0.13, 0.60)          # por debajo del equipo, por encima de los botones
BARRA_ALTO = (4 / 1084, 30 / 1084)
BARRA_ANCHO_MIN = 40 / 2412


def barra_del_enemigo(img: Imagen) -> Optional[Tuple[float, float, float, float]]:
    """El tramo verde de la vida del enemigo: (x, y, ancho, alto) en fracciones
    de la pantalla, o None si no se ve (muerto, fuera de cuadro, en un menú)."""
    from scipy import ndimage
    A = _rgb(img)
    al, an, _ = A.shape
    R, G, B = A[..., 0], A[..., 1], A[..., 2]
    verde = (G > 170) & (B < 120) & (G > R + 20)
    verde[: int(BANDA_ENEMIGO[0] * al)] = False
    verde[int(BANDA_ENEMIGO[1] * al):] = False
    etiquetas, _n = ndimage.label(verde)
    mejor = None
    for sl in ndimage.find_objects(etiquetas):
        if sl is None:
            continue
        alto, ancho = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if not BARRA_ALTO[0] * al <= alto <= BARRA_ALTO[1] * al:
            continue
        if ancho < BARRA_ANCHO_MIN * an or ancho < 4 * alto:
            continue                      # el cuerpo verde de un enemigo no es una barra
        if mejor is None or ancho > mejor[2]:
            mejor = (sl[1].start, sl[0].start, ancho, alto)
    if mejor is None:
        return None
    x, y, ancho, alto = mejor
    return (x / an, y / al, ancho / an, alto / al)


# Lo que hay junto al enemigo, visto ampliado en la pelea del 11 sep (Yanagi,
# 0537 PTS): arriba la VIDA, gruesa, en degradado de verde a amarillo, con un
# codo que baja por la izquierda; debajo, fina, el ATURDIMIENTO, amarillo sobre
# gris; a la derecha su porcentaje («58» con 12 de 21 tramos amarillos; «01» y
# «00» con la barra gris entera). La vida llena mide ~124 px a 2412 de ancho.
# ⚠️ Cómo se pinta la vida PERDIDA no se ha visto: en las cuatro capturas estaba
# llena. Por eso se mide lo lleno contra el largo de la barra entera, no contra
# lo que haya detrás.
LARGO_VIDA_LLENA = 124 / 2412


def _clase_medidor(p: np.ndarray) -> str:
    r, g, b = (int(v) for v in p)
    if max(r, g, b) < 60:
        return "K"                                   # borde oscuro
    if g > 150 and g > r + 20 and b < 120:
        return "V"                                   # verde
    if r > 150 and g > 110 and b < 90:
        return "Y"                                   # amarillo / naranja
    if abs(r - g) < 22 and abs(g - b) < 28 and 60 <= r <= 150:
        return "G"                                   # gris: vacío
    return "."


def medidores_del_enemigo(img: Imagen) -> Optional[Dict[str, float]]:
    """{"vida": 0-1, "aturdimiento": 0-1} del enemigo, o None si no se ve.

    Sirve de juez para aprender tiempos: el aturdimiento sube con cada golpe
    que ENTRA (y baja solo con el tiempo); la vida, cuando no es invencible.
    """
    barra = barra_del_enemigo(img)
    if barra is None:
        return None
    A = _rgb(img)
    al, an, _ = A.shape
    x, y = int(barra[0] * an), int(barra[1] * al)
    ancho, alto = int(barra[2] * an), int(barra[3] * al)
    paso = max(1, round(an / 603))                   # 4 px a 2412 de ancho
    fin = min(an, x + int(ancho * 2.3))

    def tira(fila: int) -> str:
        return "".join(_clase_medidor(A[fila, xx]) for xx in range(x, fin, paso))

    def sin_borde_inicial(t: str) -> int:
        """Dónde empieza lo pintado: el primer muestreo puede caer en el borde
        oscuro de la barra, y tomarlo por el final daba vida 0 con la barra
        llena (fila de arriba de la captura del «58»)."""
        i = 0
        while i < min(len(t), 3) and t[i] in "K.":
            i += 1
        return i

    lleno = 0
    t = tira(min(al - 1, int(y + alto * 0.25)))
    for c in t[sin_borde_inicial(t):]:
        if c in "VY":
            lleno += 1
        elif c == "K":
            break
    vida = min(1.0, lleno * paso / (LARGO_VIDA_LLENA * an))

    t = tira(min(al - 1, int(y + alto * 0.80)))
    i = sin_borde_inicial(t)
    while i < len(t) and t[i] == "V":                # el codo de la vida
        i += 1
    while i < len(t) and t[i] in "K.":               # su borde
        i += 1
    amarillo = gris = 0
    for c in t[i:]:
        if c == "Y":
            amarillo += 1
        elif c == "G":
            gris += 1
        elif c == "K":
            break
    aturdimiento = amarillo / (amarillo + gris) if amarillo + gris else 0.0
    return {"vida": round(vida, 2), "aturdimiento": round(aturdimiento, 2)}


def retrato_activo(img: Imagen) -> np.ndarray:
    """El retrato grande de arriba a la izquierda, reducido: la huella de quién está."""
    A = _rgb(img)
    al, an, _ = A.shape
    x0, y0, x1, y1 = RETRATO_ACTIVO
    trozo = A[int(y0 * al):int(y1 * al), int(x0 * an):int(x1 * an)].astype(np.uint8)
    return np.asarray(Image.fromarray(trozo).resize((48, 20), Image.BILINEAR), dtype=float)


def parecido(a: np.ndarray, b: np.ndarray) -> float:
    """Correlación normalizada entre dos huellas del mismo tamaño. -1 a 1."""
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    a = a - a.mean()
    b = b - b.mean()
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))


def cambio_de_retrato(antes: Imagen, despues: Imagen) -> float:
    """Cuánto cambió el retrato grande, de 0 a 1. Por encima de 0,08 hubo relevo."""
    a, b = retrato_activo(antes), retrato_activo(despues)
    return float(np.abs(a - b).mean() / 255.0)


class RetratosDelEquipo:
    """Quién está en el campo, reconocido contra la propia interfaz del juego.

    Se apunta la huella de cada agente la primera vez que se sabe quién está
    dentro (al empezar, y después de cada relevo comprobado), y desde ahí se
    reconoce a cualquiera en una comparación. Si la huella no se parece a
    ninguna, contesta "" — no la del más parecido: un relevo que no entró
    tiene que notarse, no disimularse.
    """

    def __init__(self) -> None:
        self.huellas: Dict[str, np.ndarray] = {}

    def apuntar(self, nombre: str, img: Imagen) -> None:
        if nombre:
            self.huellas[nombre] = retrato_activo(img)

    def quien(self, img: Imagen) -> Tuple[str, float]:
        huella = retrato_activo(img)
        mejor, nota = "", -1.0
        for nombre, h in self.huellas.items():
            p = parecido(huella, h)
            if p > nota:
                mejor, nota = nombre, p
        return (mejor, nota) if nota >= MISMO_RETRATO else ("", nota)

    def guardar(self, ruta: Path) -> None:
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(json.dumps({n: h.astype(int).tolist() for n, h in self.huellas.items()}))
        except OSError as e:
            logger.warning("hud: no pude guardar los retratos (%s)", e)

    def cargar(self, ruta: Path) -> "RetratosDelEquipo":
        try:
            datos = json.loads(ruta.read_text())
            self.huellas = {n: np.asarray(h, dtype=float) for n, h in datos.items()}
        except (OSError, ValueError):
            self.huellas = {}
        return self


def estado(img: Imagen) -> Dict[str, bool]:
    """Todo lo que se sabe de un vistazo, para el registro de la práctica."""
    A = _rgb(img)
    return {"interfaz": interfaz_a_la_vista(A), "ex": ex_listo(A),
            "definitiva": definitiva_lista(A), "assist": ofrece_asistencia(A)}
