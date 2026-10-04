#!/usr/bin/env python3
"""La cara de Celestia: su logo, su paleta y cómo se dibuja en un terminal.

Celestia no es Claude y no puede llevar su asterisco. Su símbolo es **el
orbe**: un núcleo en forma de destello de cuatro puntas rodeado por dos anillos
orbitales inclinados, con un satélite recorriendo el de delante. No es un dibujo
plano guardado como texto — está definido en geometría 3D y se proyecta en cada
fotograma, así que gira de verdad: los anillos se abren y se cierran según el
ángulo, y lo que pasa por detrás se apaga. Ese mismo orbe es el que dibuja la
web en SVG, con las constantes de aquí (`geometria_web()`), para que el logo del
móvil y el del navegador sean el mismo objeto y no dos dibujos parecidos.

El lienzo usa braille (⣿): cada carácter son 2×4 píxeles independientes, así que
en las cuarenta columnas de Termux caben curvas suaves en vez de barras `#`. El
color va por celda, promediando los píxeles encendidos, lo que da el degradado
violeta→cian sin pintarlo a mano.

Todo el módulo es texto → texto y no toca la terminal: se puede probar sin TTY.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# ── Paleta ───────────────────────────────────────────────────────────────────
# Un único sitio para los colores. El terminal los usa como truecolor y la web
# los recibe como variables CSS (`css_variables()`), así que un cambio aquí se
# ve en los dos chats a la vez.
PALETA: Dict[str, str] = {
    "noche":    "#0B0D17",   # fondo profundo
    "noche2":   "#12162A",   # superficie elevada
    "nebulosa": "#1C2140",   # bordes, separadores
    "violeta":  "#8B7CF6",   # primario — el color de Celestia
    "iris":     "#A78BFA",   # primario claro
    "cian":     "#4FD1E0",   # secundario
    "aurora":   "#6EE7B7",   # todo va bien
    "rosa":     "#F472B6",   # el satélite
    "ambar":    "#FBBF24",   # atención
    "carmin":   "#FB7185",   # error
    "estrella": "#F5F3FF",   # texto principal
    "polvo":    "#8890B5",   # texto secundario
    "ceniza":   "#5A6186",   # texto apagado
}

# El degradado del logo: el núcleo tira a blanco-violeta y los anillos se
# enfrían hacia el cian según se alejan del centro.
GRADIENTE = ("#C4B5FD", "#8B7CF6", "#4FD1E0")

FIN = "\033[0m"


def rgb(color: str) -> Tuple[int, int, int]:
    """'#8B7CF6' → (139, 124, 246)."""
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hexa(r: float, g: float, b: float) -> str:
    """Al revés: componentes (0-255, se recortan) → '#rrggbb'."""
    def _c(v: float) -> int:
        return max(0, min(255, int(round(v))))
    return f"#{_c(r):02X}{_c(g):02X}{_c(b):02X}"


def mezclar(a: str, b: str, t: float) -> str:
    """Interpola entre dos colores. t=0 → a, t=1 → b."""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = rgb(a)
    rb, gb, bb = rgb(b)
    return hexa(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def rampa(t: float, paradas: Sequence[str] = GRADIENTE) -> str:
    """Un punto del degradado de varias paradas (t de 0 a 1)."""
    if len(paradas) == 1:
        return paradas[0]
    t = max(0.0, min(1.0, t))
    tramo = t * (len(paradas) - 1)
    i = min(int(tramo), len(paradas) - 2)
    return mezclar(paradas[i], paradas[i + 1], tramo - i)


def atenuar(color: str, factor: float) -> str:
    """Acerca un color al fondo. factor=1 lo deja igual, 0 lo apaga."""
    return mezclar(PALETA["noche"], color, max(0.0, min(1.0, factor)))


# ── Capacidad de color del terminal ──────────────────────────────────────────
# Termux, un `ssh` viejo y un pipe a un fichero no pintan igual. En vez de
# asumir truecolor y llenar un log de basura ANSI, se pregunta.
TRUECOLOR, C256, C16, PLANO = "truecolor", "256", "16", "plano"

# Aproximaciones a la paleta en los 16 colores de toda la vida, para cuando no
# hay nada mejor. El violeta cae en el magenta brillante; el cian, en cian.
_ANSI16 = {
    "violeta": "\033[95m", "iris": "\033[95m", "cian": "\033[96m",
    "aurora": "\033[92m", "rosa": "\033[95m", "ambar": "\033[93m",
    "carmin": "\033[91m", "estrella": "\033[97m", "polvo": "\033[37m",
    "ceniza": "\033[90m", "noche": "\033[30m", "noche2": "\033[30m",
    "nebulosa": "\033[90m",
}


def capacidad_color(stream=None) -> str:
    """Qué sabe pintar este terminal: truecolor, 256, 16 o nada.

    Se mira `NO_COLOR` primero (es el estándar de facto para pedir salida sin
    color) y luego COLORTERM/TERM. Sin TTY se devuelve `PLANO`: en un pipe o un
    log los escapes solo estorban.
    """
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return PLANO
    forzado = (os.environ.get("CELESTIA_COLOR") or "").strip().lower()
    if forzado in (TRUECOLOR, C256, C16, PLANO):
        return forzado
    try:
        if not stream.isatty():
            return PLANO
    except Exception:
        return PLANO
    if (os.environ.get("COLORTERM") or "").lower() in ("truecolor", "24bit"):
        return TRUECOLOR
    term = (os.environ.get("TERM") or "").lower()
    # Termux se anuncia como xterm-256color y sí admite truecolor.
    if "256" in term or os.environ.get("TERMUX_VERSION"):
        return TRUECOLOR if os.environ.get("TERMUX_VERSION") else C256
    if term in ("dumb", ""):
        return PLANO
    return C16


def _a256(color: str) -> int:
    """Color hex al índice más cercano del cubo 6×6×6 de xterm."""
    r, g, b = rgb(color)
    if abs(r - g) < 12 and abs(g - b) < 12:          # gris: la rampa 232-255
        return 232 + min(23, int(r / 255 * 23))
    def _n(v: int) -> int:
        return min(5, int(round(v / 255 * 5)))
    return 16 + 36 * _n(r) + 6 * _n(g) + _n(b)


class Tinta:
    """Pinta según lo que aguante el terminal. Llamable: `t(texto, '#8B7CF6')`.

    Acepta indistintamente un nombre de la paleta ('violeta') o un hex, para no
    tener que recordar cuál es cuál en cada llamada.
    """

    def __init__(self, capacidad: Optional[str] = None):
        self.capacidad = capacidad or capacidad_color()

    @property
    def activa(self) -> bool:
        return self.capacidad != PLANO

    def codigo(self, color: str) -> str:
        """Solo la secuencia de escape del color de primer plano."""
        if self.capacidad == PLANO:
            return ""
        nombre = color if color in PALETA else ""
        valor = PALETA.get(color, color)
        if self.capacidad == TRUECOLOR:
            r, g, b = rgb(valor)
            return f"\033[38;2;{r};{g};{b}m"
        if self.capacidad == C256:
            return f"\033[38;5;{_a256(valor)}m"
        if nombre:
            return _ANSI16.get(nombre, "")
        # Un hex suelto en un terminal de 16 colores: se busca el de la paleta
        # más cercano y se usa su equivalente ANSI.
        return _ANSI16.get(_nombre_cercano(valor), "")

    def __call__(self, texto: str, color: str = "", *extras: str) -> str:
        if not texto or self.capacidad == PLANO or (not color and not extras):
            return texto
        pre = (self.codigo(color) if color else "") + "".join(extras)
        return f"{pre}{texto}{FIN}" if pre else texto


def _nombre_cercano(valor: str) -> str:
    """El nombre de la paleta cuyo color se parece más a este hex."""
    r, g, b = rgb(valor)
    mejor, dist_min = "estrella", float("inf")
    for nombre, hx in PALETA.items():
        rr, gg, bb = rgb(hx)
        d = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if d < dist_min:
            mejor, dist_min = nombre, d
    return mejor


# ── Lienzo braille ───────────────────────────────────────────────────────────
# Cada carácter braille es una rejilla de 2×4 puntos que se encienden por
# separado, así que una celda de terminal rinde ocho píxeles. Es la única forma
# de dibujar una elipse decente en cuarenta columnas.
_BITS = ((0x01, 0x02, 0x04, 0x40),      # columna izquierda, de arriba a abajo
         (0x08, 0x10, 0x20, 0x80))      # columna derecha
BRAILLE_BASE = 0x2800


class Lienzo:
    """Rejilla de píxeles que se imprime como caracteres braille.

    Cada píxel guarda su color; al convertir a texto, el color de una celda es
    la media de los píxeles encendidos que contiene. De ahí sale el degradado
    del logo sin tener que decidir a mano de qué color va cada carácter.
    """

    def __init__(self, ancho_px: int, alto_px: int):
        # El braille agrupa de 2 en 2 y de 4 en 4: se redondea hacia arriba para
        # no perder la última fila o columna de píxeles.
        self.ancho = max(2, ancho_px + (-ancho_px % 2))
        self.alto = max(4, alto_px + (-alto_px % 4))
        self._px: Dict[Tuple[int, int], Tuple[float, str]] = {}

    def punto(self, x: float, y: float, color: str, brillo: float = 1.0) -> None:
        """Enciende el píxel (redondeando), si cae dentro y tiene brillo."""
        if brillo <= 0.04:
            return
        ix, iy = int(round(x)), int(round(y))
        if not (0 <= ix < self.ancho and 0 <= iy < self.alto):
            return
        previo = self._px.get((ix, iy))
        # Dos trazos en el mismo píxel: manda el más brillante. Así el satélite
        # no queda tapado por la órbita que lo lleva.
        if previo is None or brillo > previo[0]:
            self._px[(ix, iy)] = (brillo, color)

    def linea(self, x0: float, y0: float, x1: float, y1: float,
              color: str, brillo: float = 1.0) -> None:
        """Segmento recto, muestreado por longitud (no hace falta Bresenham:
        los trazos del logo son cortos y esto se lee mejor)."""
        pasos = max(2, int(math.hypot(x1 - x0, y1 - y0) * 2))
        for i in range(pasos + 1):
            t = i / pasos
            self.punto(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, color, brillo)

    def celdas(self) -> List[List[Tuple[str, str]]]:
        """Filas de (carácter, color). El vacío es ' ' con color ''."""
        filas: List[List[Tuple[str, str]]] = []
        for cy in range(self.alto // 4):
            fila: List[Tuple[str, str]] = []
            for cx in range(self.ancho // 2):
                mascara = 0
                sr = sg = sb = 0.0
                peso = 0.0
                for dx in (0, 1):
                    for dy in (0, 1, 2, 3):
                        p = self._px.get((cx * 2 + dx, cy * 4 + dy))
                        if p is None:
                            continue
                        mascara |= _BITS[dx][dy]
                        brillo, color = p
                        r, g, b = rgb(color)
                        # El brillo pondera la media: un píxel tenue no debe
                        # arrastrar el color de toda la celda.
                        sr += r * brillo
                        sg += g * brillo
                        sb += b * brillo
                        peso += brillo
                if not mascara:
                    fila.append((" ", ""))
                else:
                    fila.append((chr(BRAILLE_BASE + mascara),
                                 hexa(sr / peso, sg / peso, sb / peso)))
            filas.append(fila)
        return filas

    def lineas(self, tinta: Optional[Tinta] = None) -> List[str]:
        """El dibujo ya montado, una cadena por fila."""
        tinta = tinta or Tinta()
        salida: List[str] = []
        for fila in self.celdas():
            if not tinta.activa:
                salida.append("".join(c for c, _ in fila).rstrip())
                continue
            # Se agrupan celdas contiguas del mismo color en un solo escape:
            # sin esto, un logo de 24 columnas se lleva 24 secuencias ANSI por
            # fila y el repintado del spinner parpadea.
            trozos: List[str] = []
            actual = ""
            buffer = ""
            for car, color in fila:
                if car == " ":
                    buffer += " "
                    continue
                if color != actual:
                    if buffer:
                        trozos.append(tinta(buffer, actual) if actual else buffer)
                    buffer, actual = "", color
                buffer += car
            if buffer:
                trozos.append(tinta(buffer, actual) if actual else buffer)
            salida.append("".join(trozos).rstrip())
        return salida


# ── El orbe: geometría ───────────────────────────────────────────────────────
# El logo vive en coordenadas normalizadas (-1..1 en los dos ejes) y se escala
# al tamaño que haga falta. La web usa exactamente estos números.

INCLINACION = math.radians(24)     # cuánto se inclina el plano de las órbitas
SEGUNDO_ANILLO = math.radians(68)  # el segundo anillo, girado respecto al primero
RADIO_ANILLO = 0.94
RADIO_NUCLEO = 0.24      # el destello ocupa poco: si crece, tapa los anillos
                         # y el orbe se lee como una bola en vez de una estrella
RAYOS = 0.60             # los cuatro rayos finos, en diagonal. Van en diagonal a
                         # propósito: en la horizontal se confundían con el
                         # anillo, que casi siempre se proyecta aplastado
PUNTOS_ANILLO = 132                # muestras por vuelta con el logo pequeño; en
                                   # uno grande se sube (ver `_muestras`) o el
                                   # anillo sale punteado


def _rot_x(p: Tuple[float, float, float], a: float):
    x, y, z = p
    ca, sa = math.cos(a), math.sin(a)
    return (x, y * ca - z * sa, y * sa + z * ca)


def _rot_y(p: Tuple[float, float, float], a: float):
    x, y, z = p
    ca, sa = math.cos(a), math.sin(a)
    return (x * ca + z * sa, y, -x * sa + z * ca)


def _muestras(escala: float) -> int:
    """Cuántos puntos hacen falta por vuelta para que el trazo salga continuo.

    Un anillo de radio `escala` píxeles mide 2πr; con menos de ~4 muestras por
    píxel el braille lo dibuja a rayas.
    """
    return max(PUNTOS_ANILLO, int(escala * 8))


def _anillo(fase: float, giro_propio: float, radio: float = RADIO_ANILLO,
            muestras: int = PUNTOS_ANILLO) -> List[Tuple[float, float, float]]:
    """Un anillo circular en 3D, inclinado y girado según la fase.

    La proyección es ortográfica (nos quedamos con x e y), así que el círculo se
    ve como una elipse que se abre y se cierra al girar: de ahí sale la
    sensación de volumen sin dibujar ni una sombra.
    """
    puntos = []
    for i in range(muestras):
        t = 2 * math.pi * i / muestras
        p = (radio * math.cos(t), radio * math.sin(t), 0.0)
        p = _rot_y(p, giro_propio)
        p = _rot_x(p, INCLINACION)
        p = _rot_y(p, fase)
        puntos.append(p)
    return puntos


def _satelite(fase: float, avance: float) -> Tuple[float, float, float]:
    """Dónde está el punto que recorre el anillo de delante."""
    p = (RADIO_ANILLO * math.cos(avance), RADIO_ANILLO * math.sin(avance), 0.0)
    p = _rot_x(p, INCLINACION)
    return _rot_y(p, fase)


def _radio_destello(angulo: float, punta: float = RADIO_NUCLEO) -> float:
    """Radio del núcleo en un ángulo dado: una astroide de cuatro puntas.

    `(|cos|^p + |sin|^p)^(-1/p)` con p<1 curva los lados hacia dentro, que es lo
    que distingue un destello de una estrella de picos rectos. Con p=0.30 la
    punta mide unas siete veces el valle: por debajo de eso el núcleo se lee
    como una bola y el logo pierde su forma. Por debajo de 0.38 las puntas se
    quedan en un pelo de un píxel y se pierden en un icono pequeño.
    """
    p = 0.38
    c, s = abs(math.cos(angulo)), abs(math.sin(angulo))
    base = (c ** p + s ** p) ** (-1.0 / p)
    return punta * base * 3.1


# Proporción áurea: se usa para repartir el polvo. Multiplicar el índice por
# ella y quedarse con la parte decimal da una secuencia que parece aleatoria
# pero es siempre la misma, así que el terminal, el navegador y el PNG colocan
# las motas en el mismo sitio sin compartir un generador.
PHI = 1.6180339887498948
PERIODO_RESPIRACION = 4.6          # segundos por ciclo, como una respiración lenta
POLVO = 16                         # motas orbitando alrededor


def respiracion(t: float, periodo: float = PERIODO_RESPIRACION) -> float:
    """De 0 a 1 y vuelta, pero como respira algo vivo: inhala rápido y exhala
    despacio.

    Un seno puro sube y baja igual y se lee como un metrónomo. El truco es
    partir el ciclo en dos tramos de distinta duración y suavizar cada uno con
    una curva sin esquinas, para que no se note el empalme.
    """
    x = (t % periodo) / periodo
    corte = 0.34                                  # un tercio inhalar, dos exhalar
    u = x / corte if x < corte else 1.0 - (x - corte) / (1.0 - corte)
    return u * u * (3.0 - 2.0 * u)                # smoothstep


def _mota(i: int) -> Tuple[float, float, float, float]:
    """(radio, inclinación propia, velocidad, tamaño) de una mota de polvo."""
    a = (i * PHI) % 1.0
    b = (i * 0.7548776662) % 1.0
    c = (i * 0.5698402910) % 1.0
    # El radio va de dentro del anillo a justo por fuera. Repartidas por toda la
    # caja parecían suciedad; pegadas al orbe se leen como su atmósfera. El
    # techo (1.06) está calculado para que ni la mota más lejana se salga del
    # lienzo con la escala que usa `dibujar_orbe`.
    return (0.42 + 0.64 * a,
            (b - 0.5) * 1.5,                      # cada una en su propio plano
            0.45 + 0.85 * c,                      # unas adelantan a otras
            0.35 + 0.65 * b)


def polvo(fase: float, t: float, n: int = POLVO):
    """Las motas que orbitan el orbe, con su brillo. Vive fuera de los anillos.

    Es lo que separa un logo que gira de algo que está vivo: el ojo necesita
    movimiento pequeño e irregular alrededor del movimiento grande.
    """
    for i in range(n):
        radio, incl, vel, tam = _mota(i)
        ang = (i * PHI * 6.283) + t * vel * 0.6
        p = (radio * math.cos(ang), radio * math.sin(ang), 0.0)
        p = _rot_x(p, incl)
        p = _rot_y(p, fase * 0.55)                # se dejan arrastrar, no siguen
        prof = (p[2] / radio + 1) / 2
        # Cada mota tiene su propio parpadeo, desfasado del resto.
        centelleo = 0.55 + 0.45 * math.sin(t * (1.1 + vel) + i * 2.4)
        yield p, (0.12 + 0.55 * prof) * centelleo, tam


def chispas(t: float, n: int = 3):
    """Destellos que saltan por las puntas del núcleo y se apagan.

    Nacen escalonadas (cada una con su desfase) y duran menos de lo que tardan
    en repetirse, así que aparecen de una en una y sin ritmo aparente.
    """
    for k in range(n):
        ciclo = 2.3 + k * 0.77                    # periodos primos entre sí
        u = ((t + k * 1.31) % ciclo) / ciclo
        if u > 0.34:                              # apagada la mayor parte del rato
            continue
        avance = u / 0.34
        # Sale por una punta distinta cada vez.
        cual = int((t + k * 1.31) / ciclo) % 4
        ang = cual * math.pi / 2
        # Se aleja poco: más lejos se salía del lienzo y aparecían puntos
        # sueltos pegados al nombre.
        r = RADIO_NUCLEO * 3.1 * (1.0 + 0.35 * avance)
        yield (r * math.cos(ang), r * math.sin(ang), 0.0), (1.0 - avance) ** 1.6


def geometria_web() -> Dict[str, float]:
    """Las mismas constantes, para que el SVG del navegador dibuje este orbe."""
    return {
        "inclinacion_grados": math.degrees(INCLINACION),
        "segundo_anillo_grados": math.degrees(SEGUNDO_ANILLO),
        "radio_anillo": RADIO_ANILLO,
        "radio_nucleo": RADIO_NUCLEO,
        "punta_destello": RADIO_NUCLEO * 3.1,
        "rayos": RAYOS,
        "polvo": POLVO,
        "phi": PHI,
        "periodo_respiracion": PERIODO_RESPIRACION,
    }


# ── Estados: el logo dice lo que Celestia está haciendo ──────────────────────
# Cada estado cambia el color, la velocidad y el aspecto del orbe. No es
# decoración: `hablar.py` los conmuta con lo que devuelve /actividad, así que
# el logo es una lectura de lo que pasa por dentro.
ESTADOS: Dict[str, Dict[str, object]] = {
    "reposo":     {"verbo": "",                  "color": "violeta", "vel": 0.35, "pulso": 0.18},
    "escuchando": {"verbo": "te escucho",        "color": "cian",    "vel": 0.55, "pulso": 0.30},
    "pensando":   {"verbo": "pensando",          "color": "violeta", "vel": 1.60, "pulso": 0.45},
    "buscando":   {"verbo": "buscando",           "color": "cian",   "vel": 2.40, "pulso": 0.55},
    "leyendo":    {"verbo": "leyendo",           "color": "cian",    "vel": 1.90, "pulso": 0.40},
    "recordando": {"verbo": "recordando",        "color": "iris",    "vel": 1.10, "pulso": 0.35},
    "actuando":   {"verbo": "manos a la obra",   "color": "aurora",  "vel": 2.10, "pulso": 0.50},
    "escribiendo": {"verbo": "escribiendo",      "color": "aurora",  "vel": 1.30, "pulso": 0.60},
    "error":      {"verbo": "algo ha fallado",   "color": "carmin",  "vel": 0.25, "pulso": 0.10},
}


def estado_valido(nombre: str) -> str:
    return nombre if nombre in ESTADOS else "reposo"


def _paradas(estado: str) -> Tuple[str, str, str]:
    """El degradado del orbe teñido por el estado: el color del estado manda en
    los anillos, pero el núcleo se queda claro para que siga siendo el mismo
    logo y no una mancha de color distinto en cada fase."""
    est = ESTADOS[estado_valido(estado)]
    acento = PALETA[str(est["color"])]
    return (mezclar("#FFFFFF", acento, 0.35), acento,
            mezclar(acento, PALETA["cian"], 0.55))


def dibujar_orbe(ancho_celdas: int = 22, fase: float = 0.0,
                 estado: str = "reposo", pulso: float = 0.0,
                 t: Optional[float] = None) -> Lienzo:
    """Pinta el orbe en un lienzo braille listo para imprimir.

    `fase` es el ángulo de giro en radianes y `pulso` (0..1) la respiración del
    núcleo. `t` es el tiempo en segundos, para lo que no depende del giro: el
    polvo que orbita y las chispas. Devuelve el lienzo en vez de texto para
    poder componerlo con el wordmark antes de teñirlo.
    """
    ancho_celdas = max(8, ancho_celdas)
    ancho_px = ancho_celdas * 2
    # El braille es más alto que ancho por celda (4 filas de puntos frente a 2
    # columnas), así que para que el orbe salga redondo en pantalla el alto en
    # píxeles tiene que ser aproximadamente el mismo que el ancho.
    alto_px = ancho_px
    lz = Lienzo(ancho_px, alto_px)
    cx, cy = (lz.ancho - 1) / 2, (lz.alto - 1) / 2
    escala = min(cx, cy) * 0.92
    claro, medio, frio = _paradas(estado)

    def coloca(p, color, brillo):
        lz.punto(cx + p[0] * escala, cy - p[1] * escala, color, brillo)

    # Polvo: va primero, debajo de todo, para que el orbe lo tape al pasar.
    # En un lienzo pequeño se omite — a esa escala son ruido, no ambiente.
    if t is not None and ancho_celdas >= 14:
        for p, brillo, tam in polvo(fase, t):
            coloca(p, mezclar(frio, claro, tam), brillo * 0.75)

    # Anillos: el de detrás (z<0) se apaga hasta casi desaparecer, que es lo que
    # convierte dos elipses cruzadas en una esfera.
    for giro, tono, fuerza in ((0.0, medio, 1.0), (SEGUNDO_ANILLO, frio, 0.72)):
        for p in _anillo(fase, giro, muestras=_muestras(escala)):
            profundidad = (p[2] / RADIO_ANILLO + 1) / 2      # 0 detrás, 1 delante
            brillo = (0.16 + 0.84 * profundidad ** 1.7) * fuerza
            coloca(p, mezclar(frio, tono, profundidad), brillo)

    # Rayos: cuatro trazos finísimos que salen por los valles del destello y se
    # apagan hacia la punta. Rellenan las diagonales, así que el núcleo se lee
    # como una estrella de ocho puntas —cuatro largas y cuatro cortas— en vez de
    # como un rombo. Van antes que el relleno para que el núcleo los tape en el
    # centro.
    largo = RAYOS * (1.0 + 0.10 * pulso)
    for ang in (math.pi / 4, 3 * math.pi / 4, 5 * math.pi / 4, 7 * math.pi / 4):
        pasos = max(4, int(largo * escala))
        for j in range(pasos + 1):
            t = j / pasos
            r = largo * t
            coloca((r * math.cos(ang), r * math.sin(ang), 0.0),
                   mezclar(claro, medio, t), (1.0 - t) ** 2.4 * 0.55)

    # Núcleo: destello de cuatro puntas, relleno con degradado radial.
    respiro = 1.0 + 0.16 * pulso
    pasos_ang = max(240, int(escala * 12))
    for i in range(pasos_ang):
        ang = 2 * math.pi * i / pasos_ang
        r_max = _radio_destello(ang) * respiro
        pasos = max(3, int(r_max * escala * 2))
        for j in range(pasos + 1):
            t = j / pasos
            r = r_max * t
            # Del blanco del centro al violeta de las puntas, apagándose al
            # final para que el destello no acabe en un borde duro.
            color = mezclar(claro, medio, t ** 0.7)
            coloca((r * math.cos(ang), r * math.sin(ang), 0.0), color,
                   1.0 - 0.45 * t ** 2)

    # Chispas: saltan por las puntas del destello y se apagan. Van después del
    # núcleo porque tienen que verse por encima de él.
    if t is not None:
        for p, brillo in chispas(t):
            coloca(p, mezclar(claro, PALETA["estrella"], 0.5), brillo)

    # Satélite: el punto que recorre el anillo de delante. Lleva una estela
    # corta detrás para que se lea el sentido del giro aunque el fotograma esté
    # congelado.
    avance = fase * 2.4
    for k in range(7):
        p = _satelite(fase, avance - k * 0.13)
        profundidad = (p[2] / RADIO_ANILLO + 1) / 2
        if profundidad < 0.42:            # ha pasado por detrás del núcleo
            continue
        brillo = (1.0 - k / 8) * (0.45 + 0.55 * profundidad)
        coloca(p, mezclar(PALETA["rosa"], PALETA["estrella"], 0.45 - k * 0.06),
               brillo)
    return lz


# ── Wordmark ─────────────────────────────────────────────────────────────────
# "CELESTIA" con las letras separadas: en un banner pequeño, el espaciado hace
# más por que parezca una marca que cualquier tipografía ASCII.
def wordmark(tinta: Optional[Tinta] = None, subtitulo: str = "") -> List[str]:
    tinta = tinta or Tinta()
    letras = "CELESTIA"
    pintadas = [tinta(c, rampa(i / max(1, len(letras) - 1)))
                for i, c in enumerate(letras)]
    linea = " ".join(pintadas)
    salida = [linea]
    if subtitulo:
        salida.append(tinta(subtitulo, "ceniza"))
    return salida


# ── Banner de arranque ───────────────────────────────────────────────────────
def banner(fase: float = 0.0, ancho_logo: int = 20, subtitulo: str = "",
           tinta: Optional[Tinta] = None, estado: str = "reposo",
           pulso: float = 0.0, t: Optional[float] = None) -> List[str]:
    """El orbe con el wordmark al lado, centrado verticalmente.

    Devuelve una lista de líneas para que quien llama decida si las imprime de
    golpe o las va animando.
    """
    tinta = tinta or Tinta()
    lz = dibujar_orbe(ancho_logo, fase, estado, pulso, t)
    filas = lz.lineas(tinta)
    texto = wordmark(tinta, subtitulo)
    # El wordmark se ancla a la mitad del orbe; con logos bajos se pega arriba
    # para no salirse.
    inicio = max(0, len(filas) // 2 - len(texto) // 2)
    salida: List[str] = []
    for i, fila in enumerate(filas):
        hueco = ancho_logo - _visible_len(fila)
        derecha = ""
        j = i - inicio
        if 0 <= j < len(texto):
            derecha = "   " + texto[j]
        salida.append(fila + " " * max(0, hueco) + derecha)
    return salida


def _visible_len(texto: str) -> int:
    """Longitud sin contar escapes ANSI, que no ocupan pantalla."""
    fuera = []
    escapando = False
    for c in texto:
        if escapando:
            if c == "m":
                escapando = False
            continue
        if c == "\033":
            escapando = True
            continue
        fuera.append(c)
    return len(fuera)


# ── Spinner: el logo en una sola línea ───────────────────────────────────────
# El braille de 2×4 no rinde en una línea, así que la versión mini del orbe usa
# la fila de puntos como pista orbital: el satélite la recorre de ida y vuelta,
# con el núcleo fijo en el centro.
_PISTA = "⠁⠂⠄⡀⢀⠠⠐⠈"          # los ocho puntos de una celda, en orden circular
_NUCLEOS = "✦✧✦✧"


def marco_spinner(i: int, estado: str = "pensando",
                  tinta: Optional[Tinta] = None) -> str:
    """Un fotograma del orbe en miniatura: núcleo latiendo y satélite en órbita."""
    tinta = tinta or Tinta()
    est = ESTADOS[estado_valido(estado)]
    acento = str(est["color"])
    nucleo = _NUCLEOS[(i // 3) % len(_NUCLEOS)]
    # La órbita en 1D: el satélite recorre seis posiciones, tres a cada lado,
    # y cambia de altura con el braille para sugerir la elipse.
    ciclo = i % 12
    izquierda = [" ", " ", " ", "⠄", "⠂", "⠁"][ciclo % 6] if ciclo >= 6 else " "
    derecha = ["⠈", "⠐", "⠠", " ", " ", " "][ciclo % 6] if ciclo < 6 else " "
    return (tinta(izquierda, "rosa") + tinta(nucleo, acento)
            + tinta(derecha, "rosa"))


def linea_estado(i: int, estado: str = "pensando", segundos: float = 0.0,
                 tinta: Optional[Tinta] = None, detalle: str = "",
                 ancho: int = 0) -> str:
    """La línea completa del spinner: orbe, verbo, de qué va y, si tarda, los
    segundos.

    El contador solo aparece pasados unos segundos: antes es ruido, y después es
    la diferencia entre «está trabajando» y «se ha colgado». El detalle (la
    consulta que está buscando, por ejemplo) se recorta al ancho que quede: en
    una pantalla de cuarenta columnas, una query larga partiría la línea y el
    spinner dejaría un rastro de basura al repintarse.
    """
    tinta = tinta or Tinta()
    est = ESTADOS[estado_valido(estado)]
    verbo = str(est["verbo"]) or "un momento"
    puntos = "." * (1 + (i // 4) % 3)
    cuerpo = f"{marco_spinner(i, estado, tinta)} {tinta(verbo, 'polvo')}{tinta(puntos, 'ceniza')}"
    cola = f"  {segundos:.0f}s" if segundos >= 4 else ""
    if detalle:
        # 3 del orbe + 1 + verbo + puntos + ' · ' + cola, y un par de margen.
        libre = (ancho or 78) - (len(verbo) + len(puntos) + len(cola) + 10)
        if libre >= 8:
            recortado = detalle if len(detalle) <= libre else detalle[:libre - 1] + "…"
            cuerpo += tinta(" · " + recortado, "ceniza")
    return cuerpo + tinta(cola, "ceniza")


# ── Exportación a la web ─────────────────────────────────────────────────────
def css_variables(prefijo: str = "--c") -> str:
    """La paleta como variables CSS, para no repetir los hex en el HTML."""
    return "\n".join(f"  {prefijo}-{nombre}: {valor};"
                     for nombre, valor in PALETA.items())


def _demo() -> None:                                  # pragma: no cover
    """`python3 -m celestia_lib.marca` — el logo girando en el terminal."""
    import time
    tinta = Tinta()
    ancho = 22
    try:
        print("\033[?25l", end="")                    # esconder el cursor
        for k in range(240):
            t = k * 0.05
            fase = k * 0.09
            pulso = respiracion(t)
            estado = ["reposo", "pensando", "buscando", "escribiendo"][(k // 60) % 4]
            lineas = banner(fase, ancho, f"· {ESTADOS[estado]['verbo'] or 'en calma'}",
                            tinta, estado, pulso, t)
            print("\n".join(lineas))
            time.sleep(0.05)
            print(f"\033[{len(lineas)}A", end="")     # volver arriba y repintar
        print(f"\033[{len(lineas)}B", end="")
    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h", end="")                    # devolver el cursor


if __name__ == "__main__":                            # pragma: no cover
    _demo()
