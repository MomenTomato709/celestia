"""Leer un marcador de puntos del juego sin gastar cuota: aprender sus dígitos.

10 sep 2026, practicando el equipo de Enzo en el reto «Técnica de apoyo». El
marcador («0035 PTS») es la mejor medida de si una vuelta rinde, y leerlo tenía
dos caminos malos:

- el OCR no lee esos dígitos: son cursiva de adorno y sólo sacaba «de apoyo»;
- el modelo sí (3 de 3), pero cada lectura son ~2.250 tokens y la cuota diaria
  de Groq se acabó a mitad de la primera práctica (197.896 de 200.000).

Así que se hace lo que ya hace la escuela con las pantallas: **preguntar una
vez y dejar de preguntar**. El marcador usa siempre la misma letra. Cuando el
modelo lee un número, cada dígito recortado se guarda con su valor; a partir de
ahí se lee comparando formas, gratis y en milisegundos. Sólo se vuelve a
preguntar si aparece una forma que no se ha visto nunca.

Cómo se parte, medido en tres capturas reales (0020, 0024, 0035): brillo mínimo
de los tres canales, umbral en el percentil 97 menos 15 —con umbral fijo, el
0020 de una animación de aparición daba cero dígitos; con Otsu entraban el
contorno y las letras de «PTS»—, y manchas de al menos 18 px de alto.

🔴 22 sep 2026 — el marcador tiene NIVELES y el recorte de antes no los veía.
Pasados unos 1000 puntos sale una insignia a la izquierda («UPROAR» azul,
«BLASTING» verde, «MAXIMUM» naranja a los 3000) y los dígitos se vuelven de
color. Con el brillo MÍNIMO un dígito naranja es oscuro, y la insignia empuja
el número a la derecha hasta que la zona de antes cortaba la última cifra. Por
eso las lecturas se perdían justo cuando mejor se jugaba. Ahora:

- relleno = el canal máximo si el píxel es de color vivo, el mínimo si no
  (blanco o color, los dos salen claros);
- la zona es más ancha, y del número se quita por ALTURA lo que no es cifra: la
  insignia es más alta (y lo que haya a su izquierda, chispas incluidas) y
  «PTS» y la etiqueta de debajo son más bajas;
- si con eso no salen cuatro cifras, se descartan las manchas sin el borde
  negro que lleva todo el texto del marcador: los destellos del escenario
  (el 0035 tenía uno detrás) no lo tienen.

Medido: las seis capturas reales de referencia, bien partidas (antes, el 3000
naranja no); y en 43 marcadores de vídeo leídos a ojo, 0 lecturas equivocadas
con `PARECIDO_MIN` — lo que no se reconoce es «no sé», nunca otro número.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger("celestia_v1")

# Dónde está el marcador, en fracción de la pantalla apaisada. Ancha: con
# insignia el número se corre a la derecha (ver arriba).
ZONA_PUNTOS = (0.03, 0.125, 0.33, 0.25)
# Medidas a la resolución del móvil (alto 1084); se escalan con la captura.
ALTO_MIN = 18
AREA_MIN = 60
ALTO_REF = 1084
FORMA = (20, 28)            # ancho × alto al que se normaliza cada dígito
PARECIDO_MIN = 0.80         # por debajo, esa forma no se conoce
MUESTRAS_POR_DIGITO = 12       # blancas y de tres colores (22 sep)
CIFRAS = 4                  # el marcador rellena con ceros: «0020»
# Las formas guardadas con el recorte de antes (brillo mínimo, en gris) no se
# parecen a las de ahora (máscara): mezclarlas daría lecturas falsas.
VERSION_FORMAS = 2


def _componentes(b: np.ndarray, oscuro: np.ndarray, alto_min: int, area_min: int,
                 con_borde: bool) -> List[Tuple[int, Tuple[slice, slice], int]]:
    """(x, caja, alto) de cada mancha de la máscara, de izquierda a derecha."""
    from scipy import ndimage
    etiquetas, _n = ndimage.label(b)
    cajas = []
    for i, sl in enumerate(ndimage.find_objects(etiquetas), start=1):
        if sl is None:
            continue
        alto = sl[0].stop - sl[0].start
        if alto < alto_min or int((etiquetas[sl] == i).sum()) < area_min:
            continue
        if con_borde:
            y0, y1 = max(0, sl[0].start - 4), min(b.shape[0], sl[0].stop + 4)
            x0, x1 = max(0, sl[1].start - 4), min(b.shape[1], sl[1].stop + 4)
            suya = etiquetas[y0:y1, x0:x1] == i
            anillo = ndimage.binary_dilation(suya, iterations=3) & ~suya & ~b[y0:y1, x0:x1]
            if anillo.sum() == 0 or (oscuro[y0:y1, x0:x1] & anillo).sum() / anillo.sum() < 0.5:
                continue
        cajas.append((sl[1].start, sl, alto))
    return sorted(cajas, key=lambda c: c[0])


def _cifras(cajas: List[Tuple[int, Tuple[slice, slice], int]]) -> List[Tuple[int, Tuple[slice, slice], int]]:
    """De las manchas, las del número: fuera la insignia, «PTS» y la etiqueta."""
    if not cajas:
        return []
    hmax = max(h for _x, _sl, h in cajas)
    altos = [c for c in cajas if c[2] >= 0.55 * hmax]
    ys = min(c[1][0].start for c in altos)
    ye = max(c[1][0].stop for c in altos)
    fila = [c for c in cajas if c[1][0].start >= ys - 3 and c[1][0].stop <= ye + 3]
    if not fila:
        return []
    mediana = float(np.median([c[2] for c in fila]))
    insignia = [i for i, c in enumerate(fila) if c[2] > 1.2 * mediana]
    if insignia:
        fila = fila[max(insignia) + 1:]
    if not fila:
        return []
    alto_cifra = max(c[2] for c in fila)
    return [c for c in fila if c[2] >= 0.8 * alto_cifra]


def partir(png: bytes, zona: Tuple[float, float, float, float] = ZONA_PUNTOS) -> List[np.ndarray]:
    """Las cuatro cifras del marcador, de izquierda a derecha, ya normalizadas.

    Lista vacía si no salen exactamente cuatro: medio número no es un número.
    """
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception as e:
        logger.warning("marcador: no pude abrir la captura (%s)", e)
        return []
    an, al = im.size
    x0, y0, x1, y1 = zona
    rgb = np.asarray(im.crop((int(x0 * an), int(y0 * al), int(x1 * an), int(y1 * al))), dtype=float)
    if rgb.size == 0:
        return []
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    relleno = np.where(mx - mn > 90, mx, mn)
    oscuro = mx < 90
    escala = al / ALTO_REF
    alto_min = max(6, int(round(ALTO_MIN * escala)))
    area_min = max(15, int(round(AREA_MIN * escala * escala)))
    # Primero el umbral que se adapta (el 0020 a mitad de animación sólo sale
    # así); luego uno fijo, que no depende de lo más brillante de la zona (en
    # el 3000 lo era la chispa de la insignia).
    for b in (relleno > float(np.percentile(relleno, 97)) - 15.0,
              (mx > 170) & ((mn > 150) | (mx - mn > 90))):
        for con_borde in (False, True):
            fila = _cifras(_componentes(b, oscuro, alto_min, area_min, con_borde))
            if len(fila) == CIFRAS:
                return [np.asarray(Image.fromarray((b[sl] * 255).astype(np.uint8)).resize(FORMA, Image.BILINEAR),
                                   dtype=float) for _x, sl, _h in fila]
    return []


def _parecido(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))


class LectorDeMarcador:
    """Lee el número comparando formas; aprende las formas cuando alguien lo lee."""

    def __init__(self, ruta: Optional[Path] = None):
        self.ruta = ruta
        self.formas: Dict[str, List[np.ndarray]] = {}
        if ruta is not None:
            self.cargar()

    def cargar(self) -> None:
        try:
            datos = json.loads(self.ruta.read_text())
        except (OSError, ValueError, AttributeError):
            self.formas = {}
            return
        if not isinstance(datos, dict) or datos.get("_version") != VERSION_FORMAS:
            logger.info("marcador: las formas de %s son del recorte de antes: empiezo de cero", self.ruta)
            self.formas = {}
            return
        self.formas = {d: [np.asarray(m, dtype=float) for m in ms]
                       for d, ms in datos.items() if not d.startswith("_")}

    def guardar(self) -> None:
        if self.ruta is None:
            return
        try:
            # En este PRoot `mkdir(exist_ok=True)` puede lanzar Errno 38 aunque
            # el directorio exista: que eso no impida escribir.
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            datos: Dict[str, object] = {"_version": VERSION_FORMAS}
            datos.update({d: [m.astype(int).tolist() for m in ms] for d, ms in self.formas.items()})
            self.ruta.write_text(json.dumps(datos))
        except OSError as e:
            logger.warning("marcador: no pude guardar los dígitos (%s)", e)

    def _digito(self, forma: np.ndarray) -> Tuple[str, float]:
        mejor, nota = "", -1.0
        for d, muestras in self.formas.items():
            for m in muestras:
                p = _parecido(forma, m)
                if p > nota:
                    mejor, nota = d, p
        return mejor, nota

    # El reto se para en «3000 PTS MAXIMUM»: por encima, la lectura está mal.
    TOPE = 3000
    # Parecido a partir del cual un dígito ya conocido se da por seguro.
    SEGURO = 0.93

    def leer(self, png: bytes) -> Optional[int]:
        """El número, o None si hay alguna forma que no reconoce con seguridad."""
        formas = partir(png)
        if not formas or not self.formas:
            return None
        cifras = []
        for f in formas:
            d, nota = self._digito(f)
            if nota < PARECIDO_MIN:
                return None
            cifras.append(d)
        valor = int("".join(cifras))
        # Sesión 74: con los ceros guardados como «9», «0024» se leía 9924 y la
        # práctica lo daba por bueno. Un valor imposible es «no sé».
        return valor if valor <= self.TOPE else None

    def aprender_si_cuadra(self, png: bytes, valor: int) -> bool:
        """Aprende de una lectura ajena (el modelo) solo si es posible y no
        contradice ningún dígito que ya se reconoce con seguridad.

        Sesión 74: una lectura equivocada con el mismo número de cifras se
        aprendió tal cual y dejó los ceros etiquetados como «9». Desde entonces
        «0024» salía 9924 y «0020» o «0035» no se leían: la práctica medía con
        un marcador roto sin que nada lo dijera.
        """
        if not 0 <= int(valor) <= self.TOPE:
            logger.info("marcador: %s es imposible (tope %s): no lo aprendo", valor, self.TOPE)
            return False
        formas = partir(png)
        texto = str(int(valor))
        if not formas or len(texto) > len(formas):
            return False
        for d, f in zip(texto.zfill(len(formas)), formas):
            conocido, nota = self._digito(f)
            if conocido and nota >= self.SEGURO and conocido != d:
                logger.info("marcador: la lectura %s dice «%s» donde reconozco «%s» (%.2f): "
                            "no la aprendo", valor, d, conocido, nota)
                return False
        return self.aprender(png, valor)

    def aprender(self, png: bytes, valor: int) -> bool:
        """Guarda la forma de cada dígito de `valor`. False si no cuadra el recuento.

        El marcador enseña ceros a la izquierda («0020»), así que se rellena
        hasta el número de manchas. Si el valor tiene MÁS cifras que manchas,
        o se partió mal o se leyó mal: no se aprende nada, que una forma mal
        etiquetada estropea todas las lecturas siguientes.
        """
        formas = partir(png)
        texto = str(int(valor))
        if not formas or len(texto) > len(formas):
            return False
        texto = texto.zfill(len(formas))
        for d, f in zip(texto, formas):
            lista = self.formas.setdefault(d, [])
            if all(_parecido(f, m) < 0.97 for m in lista):
                lista.append(f)
                del lista[:-MUESTRAS_POR_DIGITO]
        self.guardar()
        return True

    def conocidos(self) -> List[str]:
        return sorted(self.formas)
