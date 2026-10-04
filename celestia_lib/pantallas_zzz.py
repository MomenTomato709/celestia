"""Reconocer una pantalla de ZZZ por su aspecto, sin OCR.

Enzo, 18 sep 2026: «pero es que tarda mucho, sabes, con OCR siempre tarda mucho,
puede tardar 30 minutos incluso». Y es cierto y está medido: cada vistazo con
tesseract cuesta unos **20 segundos** sobre arte de juego, así que de una ronda de
7 minutos sólo 2 eran pelea; montar el equipo son 10 minutos, casi todos leyendo.

Pero las pantallas de un menú **siempre son iguales**: el título, la ciudad, la
Agenda, Tácticas, el equipo, la pausa, Personajes. No hace falta leerlas, basta
reconocerlas. Se reduce la captura a 32x32 en gris, se normaliza (fuera brillo y
contraste) y se compara con las que ya se han visto: unos milisegundos frente a
veinte segundos.

El OCR se sigue usando para lo que de verdad cambia —los nombres, los niveles— y
para aprender una pantalla nueva: la primera vez se lee, y a partir de ahí se
reconoce. Es la misma idea que el libro de jugadas del jugador.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from celestia_lib.paths import MEM_DIR

RUTA = MEM_DIR / "jugador" / "pantallas_zzz.json"
LADO = 32
# Distancia coseno por debajo de la cual dos capturas son la misma pantalla.
# Medido con capturas reales del juego: la misma pantalla con otro personaje o
# otro aviso queda en 0,05-0,15; pantallas distintas pasan de 0,35.
IGUAL = 0.18
# Cuántas firmas se guardan por pantalla (la misma pantalla cambia de fondo).
MAX_POR_PANTALLA = 6


def firma(png: bytes) -> Optional[List[float]]:
    """Huella de una captura: 32x32 en gris, normalizada."""
    if not png:
        return None
    from io import BytesIO

    from PIL import Image
    try:
        im = Image.open(BytesIO(png)).convert("L").resize((LADO, LADO))
    except Exception:
        return None
    g = np.asarray(im, dtype=np.float32).ravel()
    if float(g.std()) < 3.0:
        return None                      # pantalla en negro: no dice nada
    return ((g - g.mean()) / (g.std() + 1e-6)).round(3).tolist()


def _distancia(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


def leer(ruta: Path = RUTA) -> Dict[str, List[List[float]]]:
    try:
        d = json.loads(ruta.read_text("utf-8"))
        return {k: [list(map(float, f)) for f in v] for k, v in (d.get("pantallas") or {}).items()}
    except (OSError, ValueError, TypeError):
        return {}


def guardar(catalogo: Dict[str, List[List[float]]], ruta: Path = RUTA) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps({"pantallas": catalogo, "lado": LADO},
                               ensure_ascii=False), "utf-8")
    return ruta


def reconocer(png: bytes, catalogo: Optional[Dict[str, List[List[float]]]] = None,
              ruta: Path = RUTA, igual: float = IGUAL) -> Tuple[Optional[str], float]:
    """(nombre, distancia) de la pantalla conocida más parecida, o (None, 1.0)."""
    cat = catalogo if catalogo is not None else leer(ruta)
    f = firma(png)
    if not f or not cat:
        return None, 1.0
    v = np.asarray(f, dtype=np.float32)
    mejor, cual = 1.0, None
    for nombre, firmas in cat.items():
        for g in firmas:
            w = np.asarray(g, dtype=np.float32)
            if w.size != v.size:
                continue
            d = _distancia(v, w)
            if d < mejor:
                mejor, cual = d, nombre
    return (cual, round(mejor, 3)) if cual is not None and mejor <= igual else (None, round(mejor, 3))


def aprender(png: bytes, nombre: str, catalogo: Optional[Dict[str, List[List[float]]]] = None,
             ruta: Path = RUTA) -> Dict[str, List[List[float]]]:
    """Apunta esta captura como ejemplo de `nombre`. Devuelve el catálogo."""
    cat = catalogo if catalogo is not None else leer(ruta)
    f = firma(png)
    if not f or not nombre or nombre == "otra":
        return cat                        # «otra» no es una pantalla: es no saber
    suyas = cat.setdefault(nombre, [])
    # Si ya hay una casi idéntica, no se duplica.
    v = np.asarray(f, dtype=np.float32)
    for g in suyas:
        w = np.asarray(g, dtype=np.float32)
        if w.size == v.size and _distancia(v, w) < 0.05:
            return cat
    suyas.append(f)
    del suyas[:-MAX_POR_PANTALLA]
    guardar(cat, ruta)
    return cat


def olvidar(png: bytes, nombre: str, catalogo: Optional[Dict[str, List[List[float]]]] = None,
            ruta: Path = RUTA, igual: float = IGUAL) -> int:
    """Borra de `nombre` la firma que más se parece a esta captura. Devuelve 1 o 0.

    Una firma mal etiquetada no se corrige sola: la pantalla se reconoce sin volver
    a leer nada (19 sep: Tácticas guardada como «agenda», y la carga del juego como
    «pelea»). Cuando una prueba que no depende del catálogo lo desmiente, se olvida.
    Sólo la que coincidió, no todas las parecidas: si el desmentido fuera un falso
    negativo, se pierde una firma que se vuelve a aprender, no la pantalla entera.
    """
    cat = catalogo if catalogo is not None else leer(ruta)
    f = firma(png)
    if not f or nombre not in cat:
        return 0
    v = np.asarray(f, dtype=np.float32)
    cerca = [(_distancia(v, np.asarray(g, dtype=np.float32)), i)
             for i, g in enumerate(cat[nombre]) if len(g) == v.size]
    if not cerca or min(cerca)[0] > igual:
        return 0
    del cat[nombre][min(cerca)[1]]
    if not cat[nombre]:
        del cat[nombre]
    guardar(cat, ruta)
    return 1


# ── qué pestaña está abierta ────────────────────────────────────────────────
# El amarillo de la pestaña activa, medido el 18 sep sobre capturas reales:
# activa (189,156,24); las demás, gris neutro (92,92,92). Lo que las separa no
# es el brillo —que se parece— sino que al amarillo le falta el azul.
AMARILLO_MIN_R = 140      # la activa ronda 190; la gris, 92
AMARILLO_MIN_RB = 80      # R−B: 165 en la activa, 0 en la gris


def pestana_activa(png: bytes, x: float, y: float, radio: float = 0.012) -> bool:
    """¿La pestaña que hay en (x, y) está abierta?

    Hace falta porque el catálogo de firmas se aprende de lo que diga el OCR, y
    el OCR falla: el 18 sep la Agenda con la pestaña Tácticas abierta quedó
    guardada como «agenda» —no se leyó el botón «Entrenamiento libre»— y a
    partir de ahí se reconoció así siempre, sin volver a leer nada. El jugador
    pulsaba entonces la pestaña «Tácticas» estando YA en Tácticas: cuatro
    minutos dando vueltas y de vuelta a casa sin pelear.

    Esto no depende ni del OCR ni del catálogo: mira el color de la pestaña, que
    son milisegundos frente a los ~20 s de un vistazo con tesseract. Las dos
    pantallas sí se distinguen por su firma (0,508 de distancia); lo que no se
    distinguía era una etiqueta mal puesta.
    """
    if not png:
        return False
    from io import BytesIO

    from PIL import Image
    try:
        im = Image.open(BytesIO(png)).convert("RGB")
    except Exception:
        return False
    an, al = im.size
    # El recuadro es más alto que ancho: las pestañas son bajas y se pisan entre
    # ellas por los lados, así que se estrecha en x y se estira en y.
    caja = (int((x - radio) * an), int((y - radio * 2) * al),
            int((x + radio) * an), int((y + radio * 2) * al))
    trozo = np.asarray(im.crop(caja), dtype=np.float32).reshape(-1, 3)
    if not trozo.size:
        return False
    r, g, b = trozo.mean(axis=0)
    return bool(r >= AMARILLO_MIN_R and (r - b) >= AMARILLO_MIN_RB and g > b)
