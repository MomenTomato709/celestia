"""Los iconos de todos los agentes de ZZZ, con su nombre: quién es quién.

Enzo, 17 sep 2026: «quiero que desde ya le ponga nombre a todos los iconos, y así
sea más fácil reconocer todos los personajes y más rápido luego en la búsqueda de
cada personaje dentro del juego».

Antes de esto, al leer un vídeo salían «agente 0, agente 1, agente 2»: se veía
que cambiaban pero no quiénes eran. Con el catálogo etiquetado, un retrato del
HUD —de un vídeo o del móvil— se compara contra todos y sale un nombre.

Cada icono se guarda en `memoria/zzz/iconos/<slug>.png` y su huella en
`memoria/zzz/iconos.json`, así que reconocer no vuelve a bajar nada: es comparar
vectores de 16x16, que cuesta microsegundos.

⚠️ Honestidad sobre lo que esto puede y no puede: en la sesión 72 se midió que el
RETRATO GRANDE del HUD no empareja con el icono de la wiki (es otro dibujo) y que
los modelos de visión fallaban al elegir entre iconos. Lo que se compara aquí es
el **cuadrito del equipo**, que sí es el arte del icono. La precisión se mide con
los retratos que ya están etiquetados del equipo de Enzo, no se supone.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from celestia_lib.paths import MEM_DIR

CARPETA = MEM_DIR / "zzz" / "iconos"
INDICE = MEM_DIR / "zzz" / "iconos.json"
LADO = 16               # el mismo tamaño que usa ver_tutorial_zzz.cara()
MIN_CONFIANZA = 0.12    # margen mínimo con el segundo para dar un nombre


def slug(nombre: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(nombre).lower()).strip("-")


# Peso del color frente a la forma. Sólo con gris a 16x16, Yanagi ganaba a Lowell
# por 0,023 y Miyabi no salía ni entre los tres primeros: la forma de un retrato
# pequeño se parece a cualquier otra. Lo que distingue a estos personajes es la
# paleta —Astra rosa, Miyabi azul hielo, Yanagi morado—, así que el color pesa más.
PESO_COLOR = 2.0


def huella(imagen: Any) -> Optional[List[float]]:
    """La huella comparable: forma (16x16 en gris) + color (histograma por canal)."""
    from PIL import Image
    if imagen is None:
        return None
    im = imagen if isinstance(imagen, Image.Image) else Image.fromarray(np.asarray(imagen))
    im = im.convert("RGB")
    g = np.asarray(im.convert("L").resize((LADO, LADO)), dtype=np.float32).ravel()
    if float(g.std()) < 5.0:
        return None
    forma = (g - g.mean()) / (g.std() + 1e-6)
    A = np.asarray(im.resize((32, 32)), dtype=np.float32)
    color = []
    for c in range(3):
        hist, _ = np.histogram(A[..., c], bins=8, range=(0, 256))
        color.append(hist / max(1.0, hist.sum()))
    # El histograma se centra y se escala para que pese como la forma, no más.
    col = np.concatenate(color)
    col = (col - col.mean()) / (col.std() + 1e-6) * PESO_COLOR
    return np.concatenate([forma, col]).round(4).tolist()


def cargar(indice: Path = INDICE) -> Dict[str, Any]:
    try:
        d = json.loads(indice.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _parecido(a: np.ndarray, b: np.ndarray) -> float:
    """1 = idénticos. Coseno, que aguanta cambios de brillo y contraste."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


def quien_es(recorte: Any, catalogo: Optional[Dict[str, Any]] = None,
             indice: Path = INDICE) -> Tuple[Optional[str], float]:
    """El agente al que más se parece el recorte, y cuánto margen hay.

    Devuelve (nombre, margen). `margen` es la diferencia con el segundo mejor: si
    es pequeña, el parecido no distingue y vale más decir «no sé» que acertar por
    suerte. Nombre `None` cuando no hay con qué comparar o no hay margen.
    """
    cat = catalogo if catalogo is not None else cargar(indice)
    h = huella(recorte)
    if not h or not cat.get("agentes"):
        return None, 0.0
    v = np.asarray(h, dtype=np.float32)
    puntos = []
    for nombre, datos in cat["agentes"].items():
        w = np.asarray(datos.get("huella") or [], dtype=np.float32)
        if w.size == v.size:
            puntos.append((_parecido(v, w), nombre))
    if not puntos:
        return None, 0.0
    puntos.sort(reverse=True)
    mejor, segundo = puntos[0], (puntos[1] if len(puntos) > 1 else (0.0, ""))
    margen = mejor[0] - segundo[0]
    return (mejor[1] if margen >= MIN_CONFIANZA else None), round(margen, 3)


def ordenar_por_parecido(recorte: Any, catalogo: Optional[Dict[str, Any]] = None,
                         cuantos: int = 5, indice: Path = INDICE) -> List[Tuple[str, float]]:
    """Los más parecidos, para ver de qué poco o mucho se decide un nombre."""
    cat = catalogo if catalogo is not None else cargar(indice)
    h = huella(recorte)
    if not h or not cat.get("agentes"):
        return []
    v = np.asarray(h, dtype=np.float32)
    puntos = []
    for nombre, datos in cat["agentes"].items():
        w = np.asarray(datos.get("huella") or [], dtype=np.float32)
        if w.size == v.size:
            puntos.append((nombre, round(_parecido(v, w), 3)))
    puntos.sort(key=lambda x: -x[1])
    return puntos[:cuantos]


def guardar_indice(agentes: Dict[str, Dict[str, Any]], indice: Path = INDICE) -> Path:
    indice.parent.mkdir(parents=True, exist_ok=True)
    indice.write_text(json.dumps({"agentes": agentes, "lado": LADO},
                                 ensure_ascii=False, indent=1), "utf-8")
    return indice
