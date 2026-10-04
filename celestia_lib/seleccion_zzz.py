"""Montar el equipo en ZZZ: elegir cada agente por su nombre y pulsar «Select».

Enzo, 10 sep 2026: «no quiero que escoja los equipos desde el equipo
predeterminado, quiero que seleccione el equipo y que le dé a select».

Es la otra mitad de hacer equipos (`equipo_zzz.opciones_de_equipo`): elegir el
trío no sirve si luego se monta desde un equipo guardado por otro. Aquí se
monta a mano, personaje a personaje, y por eso al entrar al combate se sabe
quién va sin tener que reconocer a nadie por la cara.

Todo va por TEXTO leído en la pantalla —el nombre del agente, el rótulo del
botón—, no por coordenadas fijas ni por el modelo: los nombres se buscan en el
catálogo de la wiki (`equipo_zzz.agentes_en_texto`), y un rótulo es un rótulo en
cualquier modelo. [[feedback_funcionar_cualquier_modelo]]

Lo que falta ver en el móvil y aquí va por parámetro: cómo se abre cada hueco y
hacia dónde se desliza la lista.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from celestia_lib.equipo_zzz import Agente, agentes_en_texto, equipo_desde_pantalla
from celestia_lib.jugador import Punto
from celestia_lib.paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

# El botón de confirmar la elección. En el libro de jugadas de ZZZ sale «AGENT SELECT»
# como título en inglés con el juego en español, así que valen los dos idiomas.
_SELECT_RE = re.compile(r"(?i)^(select|seleccionar|elegir|equipar|confirmar)$")

# Lo que no se toca nunca al montar el equipo. Los predeterminados, por la regla
# de Enzo; lo de pagar, por la de siempre.
_PROHIBIDO_RE = re.compile(
    r"(?i)predeterminad|preset|^equipo\s*\d+$|^team\s*\d+$|comprar|recarga|polícrom|polychrome|tienda|purchase")


def es_prohibido(texto: str) -> bool:
    return bool(_PROHIBIDO_RE.search((texto or "").strip()))


def _centro(p: Any) -> Optional[Punto]:
    c = getattr(p, "centro", None)
    return Punto(float(c.x), float(c.y)) if c is not None else None


def donde_esta(palabras: Sequence[Any], agente: Agente, catalogo: Sequence[Agente]) -> Optional[Punto]:
    """Dónde pone el nombre de ese agente. None si no se ve.

    Vale para el nombre en una caja («Miyabi»), pegado a otra cosa por el OCR
    («miyabigood») o partido en dos cajas seguidas de la misma línea («Ye»
    «Shunguang»); en ese caso se toca entre las dos.
    """
    items = [(p, _centro(p), (getattr(p, "texto", "") or "").strip()) for p in palabras or []]
    items = [(p, c, t) for p, c, t in items if c is not None and t]
    for _p, c, t in items:
        if any(a.nombre == agente.nombre for a in agentes_en_texto(t, catalogo)):
            return c
    for _p, c, t in items:
        vecinas = [(c2.x - c.x, c2, t2) for _p2, c2, t2 in items
                   if abs(c2.y - c.y) < 0.03 and 0 < c2.x - c.x < 0.15]
        if not vecinas:
            continue
        _dx, c2, t2 = min(vecinas, key=lambda v: v[0])
        if any(a.nombre == agente.nombre for a in agentes_en_texto(f"{t} {t2}", catalogo)):
            return Punto((c.x + c2.x) / 2, (c.y + c2.y) / 2)
    return None


def boton_select(palabras: Sequence[Any]) -> Optional[Punto]:
    """El botón de confirmar la elección. Si hay varios, el más bajo (suele ser el de la ficha)."""
    candidatos = [(p, _centro(p)) for p in palabras or []
                  if _SELECT_RE.match((getattr(p, "texto", "") or "").strip())]
    candidatos = [(p, c) for p, c in candidatos if c is not None]
    if not candidatos:
        return None
    return max(candidatos, key=lambda pc: pc[1].y)[1]


@dataclass
class Resultado:
    ok: bool
    pasos: List[str] = field(default_factory=list)
    motivo: str = ""


class MontadorDeEquipo:
    """Elige agentes en la lista del juego y pulsa «Select», comprobando cada paso.

    Nada se toca a ciegas: antes de cada toque se mira que el juego siga delante
    (`sigue_en_juego`) y que lo que se va a tocar no sea algo prohibido.
    """

    def __init__(self, mando: Any, leer: Callable[[Any], Sequence[Any]], catalogo: Sequence[Agente],
                 sigue_en_juego: Optional[Callable[[], bool]] = None,
                 desliz: Tuple[Punto, Punto] = (Punto(0.5, 0.75), Punto(0.5, 0.35)),
                 max_deslices: int = 8, espera_s: float = 1.2,
                 dormir: Callable[[float], None] = time.sleep):
        self.mando = mando
        self.leer = leer
        self.catalogo = list(catalogo)
        self._preguntar_si_sigue = sigue_en_juego or (lambda: True)
        self.desliz = desliz
        self.max_deslices = max_deslices
        self.espera_s = espera_s
        self.dormir = dormir

    def sigue_en_juego(self, intentos: int = 3, respiro_s: float = 1.5) -> bool:
        """Lo pregunta hasta `intentos` veces: sólo un «no» sostenido cuenta.

        🔴 18 sep: un único vistazo dijo «ZZZ no está delante» y tiró los 10 min
        de recorrido que costó llegar al equipo; medio minuto después el juego
        seguía delante y apaisado. Un parpadeo (el juego rotando, un aviso del
        sistema) se distingue de Enzo cogiendo el móvil en que el parpadeo se va
        solo. Mirar dos veces más cuesta 3 s; rendirse costaba la partida.
        """
        for queda in range(intentos - 1, -1, -1):
            if self._preguntar_si_sigue():
                return True
            if queda:
                self.dormir(respiro_s)
        return False

    def _mirar(self) -> Sequence[Any]:
        cap = self.mando.ver()
        return self.leer(cap) if cap is not None else []

    def _tocar(self, punto: Punto, rotulo: str, r: Resultado) -> bool:
        if es_prohibido(rotulo):
            r.motivo = f"no toco «{rotulo}»: montar desde ahí no es elegir el equipo"
            return False
        if not self.sigue_en_juego():
            r.motivo = "el juego no está delante: no toco nada"
            return False
        if self.mando.tocar(punto) is False:
            r.motivo = f"no me dejan tocar «{rotulo}»: hay otra cosa delante"
            return False
        r.pasos.append(f"toco «{rotulo}» en ({punto.x:.2f}, {punto.y:.2f})")
        self.dormir(self.espera_s)
        return True

    def elegir(self, agente: Agente) -> Resultado:
        """Con la lista de agentes delante: busca a `agente`, lo toca y pulsa «Select»."""
        r = Resultado(False)
        for vuelta in range(self.max_deslices + 1):
            palabras = self._mirar()
            punto = donde_esta(palabras, agente, self.catalogo)
            if punto is None:
                if vuelta == self.max_deslices:
                    break
                self.mando.deslizar(self.desliz[0], self.desliz[1], 400)
                r.pasos.append("no está a la vista: deslizo la lista")
                self.dormir(self.espera_s)
                continue
            if not self._tocar(punto, agente.llamado, r):
                return r
            boton = boton_select(self._mirar())
            if boton is None:
                r.motivo = f"toqué a {agente.llamado} pero no aparece el botón de «Select»"
                return r
            if not self._tocar(boton, "Select", r):
                return r
            r.ok = True
            return r
        r.motivo = f"no encuentro a {agente.llamado} en la lista (¿no lo tienes?)"
        return r

    def recoger_plantilla(self) -> List[Agente]:
        """Recorre la lista de agentes deslizando y apunta todos los que ve. No toca a nadie."""
        vistos: List[Agente] = []
        sin_nuevos = 0
        for _ in range(self.max_deslices + 1):
            nuevos = [a for a in equipo_desde_pantalla(self._mirar(), self.catalogo)
                      if all(a.nombre != v.nombre for v in vistos)]
            vistos += nuevos
            sin_nuevos = 0 if nuevos else sin_nuevos + 1
            if sin_nuevos >= 2:
                break
            self.mando.deslizar(self.desliz[0], self.desliz[1], 400)
            self.dormir(self.espera_s)
        return vistos


# ─────────────────────── la pantalla real de ZZZ (10 sep 2026) ───────────────────────
#
# Medida en el móvil de Enzo, no supuesta. Dos pantallas:
#
#   EQUIPO ........ tres huecos con «+ SELECT» en el centro y «EMPTY» abajo;
#                   «Combatir» abajo a la derecha. (Y «Equipo predeterminado»,
#                   que no se toca.) El OCR no lee los «SELECT», sí los «+».
#   AGENT SELECT .. a la derecha, una cuadrícula inclinada de retratos SIN nombre,
#                   con su insignia de rango (S/A) amarilla; abajo a la izquierda,
#                   el NOMBRE del agente marcado, en texto; a la derecha del todo,
#                   el botón «SELECT» en vertical, que el OCR tampoco lee.
#
# Por eso aquí no se busca el nombre en la cuadrícula: se toca un retrato —sólo
# lo marca—, se lee el nombre de abajo a la izquierda, y si es el que se busca se
# pulsa «SELECT». Comprobado: tocar el retrato de pelo blanco escribió «Yixuan».

HUECOS_DEL_EQUIPO = (Punto(0.315, 0.498), Punto(0.500, 0.498), Punto(0.685, 0.498))
BOTON_COMBATIR = Punto(0.882, 0.943)
BANDA_HUECOS = (0.42, 0.58)          # la fila donde viven los tres huecos
CERCA_HUECO = 0.06                   # un «+» más lejos que esto no es ese hueco


def huecos_a_la_vista(palabras: Sequence[Any],
                      esperados: Sequence[Punto] = HUECOS_DEL_EQUIPO) -> Tuple[Punto, ...]:
    """Los huecos del equipo corregidos con los «+» que se ven de verdad.

    🔴 18 sep, 12:47: «abrí el hueco 1 pero la lista no aparece: paro». Los «+»
    estaban en x = 0,293 / 0,478 / 0,664 y aquí se tocaba en 0,315 / 0,500 /
    0,685: **53 px a la derecha** del sitio. Las coordenadas fijas se midieron en
    una captura y valen hasta que el juego mueve un pixel; el «+» sí está en la
    pantalla, con el OCR leyéndolo al 83-93 % de confianza.

    Se usan para corregir el DESVÍO de los tres a la vez, no para tocar sólo los
    que se ven: un hueco ya ocupado no tiene «+», y aun así hay que saber dónde
    está. Con cero «+» (equipo lleno) se devuelven los de siempre.
    """
    vistos = [p.centro for p in palabras
              if p.texto.strip() in ("+", "＋", "*")
              and BANDA_HUECOS[0] <= p.centro.y <= BANDA_HUECOS[1]]
    desvios = []
    for c in vistos:
        cerca = min(esperados, key=lambda e: abs(e.x - c.x))
        if abs(cerca.x - c.x) <= CERCA_HUECO:
            desvios.append((c.x - cerca.x, c.y - cerca.y))
    if not desvios:
        return tuple(esperados)
    ax = sum(d[0] for d in desvios) / len(desvios)
    ay = sum(d[1] for d in desvios) / len(desvios)
    return tuple(Punto(e.x + ax, e.y + ay) for e in esperados)
BOTON_SELECT_VERTICAL = Punto(0.930, 0.500)
ZONA_NOMBRE_MARCADO = (0.03, 0.80, 0.42, 0.12)      # x, y, ancho, alto
ESCALA_NOMBRE = 2.0                                  # a 1,0 y 1,5 salía con basura

# Cada retrato cae encima de su insignia de rango: medido con Yixuan, insignia en
# (0,753; 0,581) y retrato en (0,770; 0,478).
DESDE_INSIGNIA = (0.017, -0.103)


def insignias_de_rango(png: bytes) -> List[Punto]:
    """Las insignias S/A de la cuadrícula: un círculo amarillo de ~38 px por retrato.

    Las estrellas de favorito también son amarillas, pero más pequeñas (~26 px) y
    menos llenas; y el retrato marcado lleva un marco amarillo enorme que se
    descarta por tamaño. Medido sobre la captura real: 11 insignias + 1 marcado.
    """
    import io
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    try:
        A = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=int)
    except Exception:
        return []
    al, an, _ = A.shape
    r, g, b = A[..., 0], A[..., 1], A[..., 2]
    mascara = (r > 200) & (g > 130) & (b < 90) & (r - b > 130)
    mascara[:, :int(0.58 * an)] = False
    etiquetas, _n = ndimage.label(mascara)
    fuera: List[Punto] = []
    for i, sl in enumerate(ndimage.find_objects(etiquetas), start=1):
        if sl is None:
            continue
        alto, ancho = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        area = int((etiquetas[sl] == i).sum())
        if 540 <= area <= 720 and 32 <= ancho <= 44 and 32 <= alto <= 44:
            fuera.append(Punto((sl[1].start + ancho / 2) / an, (sl[0].start + alto / 2) / al))
    return sorted(fuera, key=lambda p: (round(p.y, 2), p.x))


def retratos_de_la_cuadricula(png: bytes) -> List[Punto]:
    """Dónde tocar cada retrato visible (menos el que ya está marcado)."""
    dx, dy = DESDE_INSIGNIA
    return [Punto(p.x + dx, p.y + dy) for p in insignias_de_rango(png) if p.y + dy > 0.02]


CARAS = MEM_DIR / "jugador" / "caras_cuadricula_zzz.json"
# Lo que ocupa un retrato en la cuadrícula, medido sobre la captura real
# (`logs/zzz_ref/seleccion_sin_nombres.png`): las tarjetas van inclinadas, así
# que se recorta el centro, que es lo que no cambia con la inclinación.
CARA_ANCHO, CARA_ALTO = 0.055, 0.115
LADO_CARA = 24                    # la firma, como la de las pantallas


def cara_en(png: bytes, p: Punto) -> Optional[List[float]]:
    """Huella del retrato que hay en ese punto de la cuadrícula.

    Hace falta porque **la cuadrícula no tiene nombres**: sólo cara, nivel y
    elemento (19 sep, ver `logs/zzz_ref/seleccion_sin_nombres.png`). Buscar por
    el nombre escrito ahí es imposible, y eso costó dos días: Nangong Yu acabó
    en «los que no tengo» el 17 y el 19 se pasó 12 minutos buscándole. Con la
    cara aprendida una vez, encontrarle es mirar, no leer.
    """
    import io

    import numpy as np
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return None
    an, al = im.size
    caja = (int((p.x - CARA_ANCHO / 2) * an), int((p.y - CARA_ALTO / 2) * al),
            int((p.x + CARA_ANCHO / 2) * an), int((p.y + CARA_ALTO / 2) * al))
    if caja[0] < 0 or caja[1] < 0 or caja[2] > an or caja[3] > al:
        return None
    g = np.asarray(im.crop(caja).resize((LADO_CARA, LADO_CARA)), dtype=np.float32).ravel()
    if float(g.std()) < 3.0:
        return None                   # un hueco vacío no es una cara
    return ((g - g.mean()) / (g.std() + 1e-6)).round(3).tolist()


# Cuántas huellas se guardan de cada agente. Una sola no basta: el mismo retrato
# cambia según cómo esté la cuadrícula (dentro del equipo, marcado, otra página),
# y el 22 sep las caras aprendidas por la mañana no valieron por la tarde — el
# montaje se fue a 7,5 min leyendo nombres. Es lo mismo que le pasaba a cada
# dígito del marcador con sus colores.
MUESTRAS_POR_CARA = 6
# Dos huellas más parecidas que esto son la misma: no hace falta guardar las dos.
CARA_REPETIDA = 0.02


def caras_sabidas(ruta: Path = CARAS) -> Dict[str, List[List[float]]]:
    """{nombre: [huella, ...]}. Lee también el formato de una huella suelta."""
    import json
    try:
        crudo = json.loads(ruta.read_text("utf-8")).get("caras") or {}
    except (OSError, ValueError, TypeError):
        return {}
    fuera: Dict[str, List[List[float]]] = {}
    for nombre, valor in crudo.items():
        try:
            if valor and isinstance(valor[0], (list, tuple)):
                fuera[nombre] = [[float(x) for x in h] for h in valor if h]
            elif valor:
                fuera[nombre] = [[float(x) for x in valor]]
        except (TypeError, ValueError):
            continue
    return fuera


def _distancia(a, b) -> float:
    import numpy as np
    v = np.asarray(a, dtype=np.float32)
    w = np.asarray(b, dtype=np.float32)
    if v.size != w.size:
        return 1.0
    return float(1.0 - np.dot(v, w) / (np.linalg.norm(v) * np.linalg.norm(w) + 1e-6))


def apuntar_cara(nombre: str, huella: Sequence[float], ruta: Path = CARAS) -> None:
    import json
    if not nombre or not huella:
        return
    caras = caras_sabidas(ruta)
    suyas = caras.setdefault(nombre, [])
    nueva = [float(x) for x in huella]
    if any(_distancia(nueva, h) < CARA_REPETIDA for h in suyas):
        return
    suyas.append(nueva)
    del suyas[:-MUESTRAS_POR_CARA]
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        ruta.write_text(json.dumps({"caras": caras}, ensure_ascii=False), "utf-8")
    except OSError as e:
        logger.warning("selección: no pude guardar la cara de %s (%s)", nombre, e)


def quienes_por_cara(png: bytes, puntos: Sequence[Punto],
                     ruta: Path = CARAS, igual: float = 0.25) -> Dict[int, str]:
    """{i del retrato: nombre} de los que se reconocen por la cara.

    22 sep: al montar el equipo, si la cara del buscado no estaba en la página
    se tocaba y se LEÍA el nombre de cada retrato —unos 20 s cada uno— aunque
    todas esas caras ya se conocieran. Yanagi tardó 2,5 min y Astra 3,5 en
    ponerse. Sabiendo quién hay, la página se pasa de largo.
    """
    sabidas = caras_sabidas(ruta)
    if not sabidas:
        return {}
    fuera: Dict[int, str] = {}
    for i, p in enumerate(puntos):
        h = cara_en(png, p)
        if not h:
            continue
        mejor, cual = igual, None
        for n, muestras in sabidas.items():
            for v in muestras:
                d = _distancia(v, h)
                if d < mejor:
                    mejor, cual = d, n
        if cual:
            fuera[i] = cual
    return fuera


def donde_esta_su_cara(png: bytes, nombre: str, puntos: Sequence[Punto],
                       ruta: Path = CARAS, igual: float = 0.25) -> Optional[Punto]:
    """En qué retrato de los visibles está ese agente, por cualquiera de sus caras."""
    suyas = caras_sabidas(ruta).get(nombre)
    if not suyas:
        return None
    mejor, cual = igual, None
    for p in puntos:
        h = cara_en(png, p)
        if not h:
            continue
        for v in suyas:
            d = _distancia(v, h)
            if d < mejor:
                mejor, cual = d, p
    return cual


def nombre_marcado(cap: Any, ojo: Any, catalogo: Sequence[Agente]) -> Optional[Agente]:
    """El agente cuyo nombre pone abajo a la izquierda. None si no se lee."""
    from celestia_lib.jugador import Zona
    try:
        palabras = ojo.leer(cap, escala=ESCALA_NOMBRE, zona=Zona(*ZONA_NOMBRE_MARCADO))
    except Exception as e:
        logger.warning("selección: no pude leer el nombre marcado (%s)", e)
        return None
    encontrados = agentes_en_texto(" ".join((p.texto or "") for p in palabras), catalogo)
    return encontrados[0] if encontrados else None


class MontadorEnCuadricula:
    """Monta el equipo en la pantalla real: hueco → retratos → nombre → «SELECT».

    Enzo: «quiero que seleccione el equipo y que le dé a select». Aquí no hay
    nombres en la cuadrícula, así que se marca cada retrato (marcar no elige
    nada), se lee el nombre de abajo a la izquierda y, cuando es el que toca, se
    pulsa el «SELECT» vertical. La cuadrícula se desliza, y como sus filas son
    iguales, un desplazamiento de una fila justa parece «no se ha movido»: por
    eso las páginas se cuentan por NOMBRES nuevos, no midiendo cuánto se movió.
    """

    ARRIBA = (Punto(0.80, 0.20), Punto(0.80, 0.90), 250)
    PAGINA = (Punto(0.80, 0.85), Punto(0.80, 0.30), 700)

    def __init__(self, mando: Any, catalogo: Sequence[Agente],
                 nombre: Optional[Callable[[Any], Optional[Agente]]] = None,
                 retratos: Optional[Callable[[bytes], List[Punto]]] = None,
                 sigue_en_juego: Optional[Callable[[], bool]] = None,
                 max_paginas: int = 8, espera_s: float = 1.6,
                 dormir: Callable[[float], None] = time.sleep):
        self.mando = mando
        self.catalogo = list(catalogo)
        if nombre is None:
            from celestia_lib.jugador import Ojo
            ojo = Ojo()
            nombre = lambda cap: nombre_marcado(cap, ojo, self.catalogo)
        self.nombre = nombre
        self.retratos = retratos or retratos_de_la_cuadricula
        self._preguntar_si_sigue = sigue_en_juego or (lambda: True)
        self.max_paginas = max_paginas
        self.espera_s = espera_s
        self.dormir = dormir

    def sigue_en_juego(self, intentos: int = 3, respiro_s: float = 1.5) -> bool:
        """Igual que en `MontadorDeEquipo`: un «no» suelto es un parpadeo, no Enzo."""
        for queda in range(intentos - 1, -1, -1):
            if self._preguntar_si_sigue():
                return True
            if queda:
                self.dormir(respiro_s)
        return False

    def _tocar(self, punto: Punto, rotulo: str, r: Resultado, espera: Optional[float] = None) -> bool:
        if es_prohibido(rotulo):
            r.motivo = f"no toco «{rotulo}»"
            return False
        if not self.sigue_en_juego():
            r.motivo = "el juego no está delante: no toco nada"
            return False
        # 🔴 22 sep: `sigue_en_juego()` dijo que sí y el candado del mando dijo
        # que no (Enzo abrió Discord un segundo después). Los toques se perdían
        # sin que nadie se enterara, la lista se «leyó» sin un solo nombre y de
        # ahí salió «no encuentro a Miyabi» → a la lista de los que no tengo.
        if self.mando.tocar(punto) is False:
            r.motivo = f"no me dejan tocar «{rotulo}»: hay otra cosa delante"
            return False
        r.pasos.append(f"toco {rotulo}")
        self.dormir(self.espera_s if espera is None else espera)
        return True

    def _recorrer(self, r: Resultado, buscado: Optional[Agente] = None,
                  desde_arriba: bool = True, leer_todo: bool = False) -> Tuple[Optional[Agente], List[str]]:
        """Marca retratos página a página. Para en `buscado` (sin pulsar SELECT) o al final.

        El final de la lista es cuando deslizar ya no enseña a nadie que no
        estuviera en la página anterior. No vale «dos páginas sin nombres nuevos»:
        al seguir una lectura con los ya conocidos, eso paraba al principio.
        """
        if desde_arriba:
            for _ in range(5):
                self.mando.deslizar(*self.ARRIBA)
                self.dormir(0.8)
        vistos: List[str] = []
        anterior: Optional[set] = None
        sin_lectura = 0
        for _pagina in range(self.max_paginas):
            en_pagina: set = set()
            visitados: List[Punto] = []
            for _pasada in range(3):
                cap = self.mando.ver()
                png_pagina = getattr(cap, "png", b"") or b""
                pendientes = [p for p in self.retratos(png_pagina)
                              if all(abs(p.x - v.x) > 0.03 or abs(p.y - v.y) > 0.03 for v in visitados)]
                if not pendientes:
                    break
                # Si ya se le conoce la cara, no hace falta ir tocando y leyendo
                # uno a uno: se le ve. Leer cada nombre cuesta ~20 s de OCR, y
                # con 20 agentes eso son los 12 minutos que se perdieron el 19.
                if buscado is not None:
                    suyo = donde_esta_su_cara(png_pagina, buscado.nombre, pendientes)
                    if suyo is not None and self._tocar(suyo, "su retrato", r):
                        visto = self.nombre(self.mando.ver())
                        if visto is not None and visto.nombre == buscado.nombre:
                            # La cuadrícula le pinta distinto según cómo esté
                            # (en el equipo, marcado, otra página): cada vez que
                            # el nombre confirma la cara, se guarda esa variante.
                            confirmada = cara_en(png_pagina, suyo)
                            if confirmada:
                                apuntar_cara(visto.nombre, confirmada)
                            return visto, vistos
                        # La cara dijo que era y el nombre dice que no: manda el
                        # nombre, y se sigue mirando uno a uno como siempre.
                        visitados.append(suyo)
                    elif not leer_todo:
                        # Su cara no está aquí. A los que SÍ se les conoce la cara
                        # no hay que leerlos: se les da por vistos y sólo se lee a
                        # los desconocidos. 22 sep: con una sola cara nueva en la
                        # página se leía la página entera, y el montaje tardó 6 min.
                        conocidos = quienes_por_cara(png_pagina, pendientes)
                        for i, n in conocidos.items():
                            en_pagina.add(n)
                            if n not in vistos:
                                vistos.append(n)
                            # Dado por visto: si no, la pasada siguiente lo lee igual.
                            visitados.append(pendientes[i])
                        if len(conocidos) == len(pendientes):
                            r.pasos.append(f"la página es de {len(conocidos)} caras conocidas: paso")
                            break
                        if conocidos:
                            r.pasos.append(f"conozco {len(conocidos)} de {len(pendientes)}: sólo leo los demás")
                            pendientes = [p for i, p in enumerate(pendientes) if i not in conocidos]
                for p in pendientes:
                    if not self._tocar(p, "un retrato", r):
                        return None, vistos
                    visitados.append(p)
                    a = self.nombre(self.mando.ver())
                    if a is None:
                        self.dormir(1.0)
                        a = self.nombre(self.mando.ver())
                    if a is None:
                        continue
                    # Se le ha leído el nombre: se le guarda la cara para no
                    # tener que volver a leerlo nunca.
                    huella = cara_en(png_pagina, p)
                    if huella:
                        apuntar_cara(a.nombre, huella)
                    en_pagina.add(a.nombre)
                    if a.nombre not in vistos:
                        vistos.append(a.nombre)
                    if buscado is not None and a.nombre == buscado.nombre:
                        return a, vistos
            if not en_pagina:
                sin_lectura += 1
                if sin_lectura >= 2:
                    break
            elif anterior is not None and en_pagina <= anterior:
                break
            else:
                sin_lectura = 0
            anterior = en_pagina or anterior
            self.mando.deslizar(*self.PAGINA)
            self.dormir(1.8)
        return None, vistos

    def leer_plantilla(self, desde_arriba: bool = True) -> Tuple[List[str], Resultado]:
        """Todos los agentes de la lista, sin elegir a ninguno."""
        r = Resultado(False)
        _a, vistos = self._recorrer(r, desde_arriba=desde_arriba)
        r.ok = bool(vistos)
        return vistos, r

    def elegir(self, agente: Agente) -> Resultado:
        """Con la lista delante: busca a `agente` y pulsa «SELECT»."""
        r = Resultado(False)
        encontrado, _vistos = self._recorrer(r, agente)
        if r.motivo:
            return r
        if encontrado is None and not r.motivo:
            # Pasar páginas por las caras es rápido, pero una cara mal guardada
            # escondería al que se busca. Antes de decir «no lo tienes» —que se
            # cree para siempre— se mira otra vez leyendo los nombres.
            r.pasos.append("no le he visto pasando por las caras: miro otra vez leyendo")
            encontrado, mas = self._recorrer(r, agente, leer_todo=True)
            _vistos = list(dict.fromkeys(list(_vistos) + list(mas)))
            if encontrado is not None:
                if not self._tocar(BOTON_SELECT_VERTICAL, "SELECT", r, espera=2.5):
                    return r
                r.ok = True
                return r
        if r.motivo:
            return r
        if encontrado is None:
            # Sin nombres leídos no se ha mirado la lista: no se concluye nada.
            # Un «no lo tengo» falso se cree para siempre y deja al equipo sin
            # su mejor agente ([[etiquetas_por_suposicion]]).
            if len(_vistos) < LEIDOS_PARA_CONCLUIR:
                r.motivo = (f"no pude leer la lista ({len(_vistos)} nombres): "
                            f"no concluyo si tienes a {agente.llamado}")
                return r
            r.motivo = f"no encuentro a {agente.llamado} en tu lista"
            apuntar_los_que_tengo(_vistos)
            apuntar_que_no_tengo(agente.nombre or agente.llamado)
            return r
        if not self._tocar(BOTON_SELECT_VERTICAL, "SELECT", r, espera=2.5):
            return r
        r.ok = True
        return r

    def montar(self, orden: Sequence[Agente],
               huecos: Optional[Sequence[Punto]] = None) -> Resultado:
        """Desde la pantalla de equipo: cada hueco, su agente, en este orden.

        `huecos` son los sitios MEDIDOS en la pantalla de hoy (ver
        `huecos_a_la_vista`); sin ellos se usan los de siempre.
        """
        sitios = tuple(huecos) if huecos else tuple(HUECOS_DEL_EQUIPO)
        r = Resultado(False)
        for i, agente in enumerate(list(orden)[:len(sitios)]):
            if not self._tocar(sitios[i], f"el hueco {i + 1}", r, espera=2.5):
                return r
            sub = self.elegir(agente)
            r.pasos += [f"hueco {i + 1}: {x}" for x in sub.pasos[-2:]]
            if not sub.ok:
                r.motivo = f"hueco {i + 1}: {sub.motivo}"
                return r
        r.ok = True
        return r

# Los agentes que Enzo TIENE de verdad, leidos de la lista del juego. El
# inventario guardado se equivoca: el 17 sep eligio un equipo con Nangong Yu
# —«nota 138» del meta— y al montarlo salio «no encuentro a Nangong Yu en tu
# lista». La lista de seleccion de equipo solo muestra lo que tienes, asi que es
# la fuente fiable; el inventario, una suposicion.
QUE_TENGO = MEM_DIR / "jugador" / "agentes_del_juego.json"


# Menos nombres leídos que esto en un recorrido entero: no se miró la lista.
LEIDOS_PARA_CONCLUIR = 5


def apuntar_los_que_tengo(vistos: Sequence[str]) -> None:
    """Guarda (sumando) los nombres leidos en la lista de seleccion."""
    import json
    nombres = sorted({" ".join(str(v).split()) for v in (vistos or []) if len(str(v)) > 2})
    if len(nombres) < 3:
        return          # una lectura de dos nombres no es una plantilla
    antes = []
    try:
        antes = list(json.loads(QUE_TENGO.read_text("utf-8")).get("agentes") or [])
    except (OSError, ValueError):
        antes = []
    juntos = sorted(set(antes) | set(nombres))
    try:
        QUE_TENGO.parent.mkdir(parents=True, exist_ok=True)
        QUE_TENGO.write_text(json.dumps(
            {"agentes": juntos, "de": "lista de seleccion del juego",
             "cuando": __import__("time").strftime("%Y-%m-%d %H:%M")},
            ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def los_que_tengo() -> List[str]:
    """Lo leido del juego, o lista vacia si aun no se ha leido nunca."""
    import json
    try:
        return list(json.loads(QUE_TENGO.read_text("utf-8")).get("agentes") or [])
    except (OSError, ValueError):
        return []

# Y los que NO aparecen. La lista blanca de arriba se lee a trozos —una pasada vio
# 7 de los 21— y fiarse de ella descartaria a Astra, que si esta. La lista negra
# es segura: si el juego no lo encuentra al montar, no lo tienes, y punto.
NO_TENGO = MEM_DIR / "jugador" / "agentes_que_no_tengo.json"


def apuntar_que_no_tengo(nombre: str) -> None:
    import json
    nombre = " ".join(str(nombre or "").split())
    if len(nombre) < 3:
        return
    # Si la lista leída del propio juego dice que sí lo tienes, esto es un fallo
    # de lectura, no un descubrimiento (22 sep: Miyabi, con 22 agentes leídos el
    # 17 y el 22 incluyéndola).
    try:
        sabidos = set(json.loads(QUE_TENGO.read_text("utf-8")).get("agentes") or [])
    except (OSError, ValueError):
        sabidos = set()
    if nombre in sabidos:
        logger.warning("selección: no apunto «%s» como que no lo tienes: la lista del juego dice que sí", nombre)
        return
    antes = los_que_no_tengo()
    if nombre in antes:
        return
    try:
        NO_TENGO.parent.mkdir(parents=True, exist_ok=True)
        NO_TENGO.write_text(json.dumps(
            {"agentes": sorted(set(antes) | {nombre}),
             "de": "no aparecio en la lista de seleccion del juego",
             "cuando": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def si_que_lo_tengo(nombre: str) -> bool:
    """Se le ha VISTO jugando: sea lo que sea que dijera la lista, lo tiene.

    «No apareció en la lista de selección» se guardaba como «no lo tengo», y ese
    dato se creía para siempre. El 17 sep Nangong Yu entró ahí por no salir en
    una búsqueda; el 19, con él peleando en 16 tramos (cara reconocida al 99 %),
    el equipo seguía armándose sin él: su sitio lo ocupaba Yanagi, que no entra
    al campo jamás, y como el que entraba no estaba en la tabla de tiempos
    jugaba el mínimo de 6 s en vez de 16. El mejor del equipo —1,12 barras/min
    frente a 0,56 de Astra— era el que menos jugaba.

    Verlo en el campo es la prueba más fuerte que hay: manda sobre la lista.
    Devuelve si hubo que borrar algo.
    """
    import json
    nombre = " ".join(str(nombre or "").split())
    antes = los_que_no_tengo()
    quedan = [n for n in antes if n.lower() != nombre.lower()
              and n.split()[-1].lower() != nombre.split()[-1].lower()] if nombre else antes
    if len(quedan) == len(antes):
        return False
    try:
        NO_TENGO.write_text(json.dumps(
            {"agentes": quedan,
             "de": "no aparecio en la lista de seleccion del juego",
             "cuando": time.strftime("%Y-%m-%d %H:%M"),
             "ultimo_borrado": f"{nombre}: se le vio jugando"}, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        return False
    return True


def los_que_no_tengo() -> List[str]:
    import json
    try:
        return list(json.loads(NO_TENGO.read_text("utf-8")).get("agentes") or [])
    except (OSError, ValueError):
        return []
