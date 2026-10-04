"""Agente autónomo, planner y memoria UI.

Incluye:
  - AgentPlanner: detecta intenciones en texto del usuario
  - MemoriaPatronesUI: patrones aprendidos de control UI
  - AgenteAutonomo: control cross-platform de apps
"""
from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .paths import MEM_DIR, ROOT
from .reminders import ReminderManager
from .resources import PLATAFORMA

logger = logging.getLogger("celestia_v1")

# Deps opcionales (detectadas localmente)
try:
    import pywinauto  # noqa: F401
    HAS_PYWINAUTO = True
except ImportError:
    HAS_PYWINAUTO = False

try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    _req = None  # type: ignore[assignment]

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .orchestrator import Orchestrator  # noqa: F401

# OCR opcional (Termux normalmente no lo trae). Sin estos flags, los
# paths que usaban `HAS_OCR`/`pytesseract` levantaban NameError en runtime.
try:
    import pytesseract  # noqa: F401
    HAS_OCR = True
except ImportError:
    HAS_OCR = False
    pytesseract = None  # type: ignore[assignment]

# pyautogui — sólo disponible en escritorio con display. En Termux/Android
# o servidor headless no se carga. Antes referenciar `HAS_PYAUTOGUI` o
# `pyautogui` sin estos guardas provocaba NameError.
try:
    import pyautogui  # noqa: F401
    HAS_PYAUTOGUI = True
except (ImportError, KeyError, Exception):
    # pyautogui puede fallar al importar en headless (sin DISPLAY) con
    # excepciones distintas a ImportError; capturamos amplio.
    HAS_PYAUTOGUI = False
    pyautogui = None  # type: ignore[assignment]


def _parsear_formato_y_tema(texto_completo: str, hint1: str, hint2: str) -> Dict[str, str]:
    """Extrae el formato (extensión) y el tema desde el texto del usuario."""
    ALIAS = {
        "word": "docx", "doc": "docx", "documento": "pdf",
        "excel": "xlsx", "hoja": "xlsx",
        "presentacion": "pptx", "presentación": "pptx", "powerpoint": "pptx",
        "markdown": "md", "texto": "txt", "imagen": "png",
        "python": "py", "javascript": "js", "typescript": "ts",
        "ruby": "rb", "rust": "rs", "java": "java", "kotlin": "kt",
        "swift": "swift", "bash": "sh", "shell": "sh",
        "blender": "blend", "roblox": "rbxl", "unity": "unity",
        "godot": "gd",
        "html": "html", "css": "css", "json": "json", "xml": "xml",
        "yaml": "yaml", "csv": "csv", "sql": "sql",
        "c++": "cpp", "cpp": "cpp", "c#": "cs", "csharp": "cs",
    }
    # «documento», «archivo» y «fichero» no dicen NADA del formato: son la
    # palabra genérica. Si el usuario además dijo «word», manda «word» — antes
    # «un documento word sobre los gatos» salía en PDF porque el genérico
    # ganaba por ir primero en la frase.
    GENERICOS = {"documento", "archivo", "fichero"}
    if hint1 and hint1.lower() in GENERICOS:
        especifico = re.search(
            r"\b(word|docx?|excel|xlsx?|powerpoint|presentaci[oó]n|pptx?|pdf|"
            r"markdown|md|html|csv|json|txt|texto)\b", texto_completo, re.I)
        if especifico:
            hint1 = especifico.group(1)

    m_ext = re.search(r"\.([a-z0-9]{1,8})\b", texto_completo, re.I)
    if m_ext:
        formato = m_ext.group(1).lower()
    elif hint1 and hint1.lower() in ALIAS:
        formato = ALIAS[hint1.lower()]
    elif hint1 and hint1.lower() in {"pdf","docx","txt","md","html","csv","json","xlsx","pptx",
                                       "py","js","ts","lua","rb","go","rs","cpp","c","java","cs",
                                       "swift","kt","php","sh","sql","blend","rbxl","gd"}:
        formato = hint1.lower()
    else:
        for palabra, ext in ALIAS.items():
            if re.search(rf"\b{re.escape(palabra)}\b", texto_completo, re.I):
                formato = ext
                break
        else:
            formato = "pdf"
    tema = (hint2 or "").strip(" .?!\"'")
    # Un marcador explícito de tema manda sobre la palabra que se coló entre el
    # formato y el tema. «un pdf muy corto sobre el color azul» daba tema="muy"
    # (visto en vivo, sesión 43): la palabra suelta era un adjetivo del
    # documento, no de lo que va dentro. Con "sobre/acerca de/que trate…" no hay
    # ambigüedad posible: el tema es lo que viene detrás.
    m_marcador = re.search(
        r"(?:sobre|acerca\s+de|con\s+tema|"
        r"que\s+(?:haga|hable|trate|explique|sirva\s+para))\s+(.+)",
        texto_completo, re.I | re.S)
    if m_marcador:
        tema = m_marcador.group(1).strip(" .?!\"'")
    # Sesión 31 (BUG-S96): si hint2 es una preposición o conector (sobre,
    # de, acerca, con, para, que, en, los, las, el, la, un, una), descartarlo
    # — el tema real viene después. Re-extraer del texto completo.
    if tema.lower() in {"sobre", "de", "del", "acerca", "con", "para", "que",
                          "en", "los", "las", "el", "la", "un", "una"}:
        tema = ""
    # Ni el nombre del formato es el tema: «un archivo excel de gastos» daba
    # tema="excel". Lo que va detrás («gastos») es de lo que trata.
    if tema.lower() in ALIAS or tema.lower() in {
            "pdf", "docx", "txt", "md", "html", "csv", "json", "xlsx", "pptx"}:
        tema = ""
    if not tema:
        # Buscar el tema tras "sobre/de/acerca de/con tema..." en el texto
        m_tema = re.search(
            r"(?:sobre|acerca\s+de|con\s+tema|de|para|que\s+(?:haga|hable|trate|explique))\s+"
            r"((?:los?|las?|un[ao]?\s+)?[\wáéíóúñ][\wáéíóúñ\s]*?)(?:\s*[\.\?!]|$)",
            texto_completo, re.I,
        )
        if m_tema:
            tema = m_tema.group(1).strip(" .?!\"'")
    if not tema:
        tema = re.sub(r"(?:crea[r]?(?:me)?|genera[r]?(?:me)?|haz(?:me)?|escr[íi]be(?:me)?|redacta[r]?(?:me)?)\s+",
                       "", texto_completo, count=1, flags=re.I).strip(" .?!\"'")
    return {"formato": formato, "tema": tema or "documento"}


# ─────────────────────────────────────────────
# Domótica: lookup ligero del registro para que el planner pueda re-rutear
# "abre la luz" → domotica_encender cuando "luz" es un dispositivo registrado.
# Lee `memoria/dispositivos.json` con caché por mtime — evita instanciar
# DomoticaManager (importa subprocess, paths, etc.) en el hot-path del planner.
# ─────────────────────────────────────────────
_DOMOTICA_NAMES_CACHE: Optional[set] = None
_DOMOTICA_NAMES_MTIME: float = -1.0


def _domotica_registered_names() -> set:
    """Conjunto de claves normalizadas de dispositivos registrados."""
    global _DOMOTICA_NAMES_CACHE, _DOMOTICA_NAMES_MTIME
    registry = MEM_DIR / "dispositivos.json"
    try:
        mtime = registry.stat().st_mtime if registry.exists() else 0.0
    except OSError:
        return _DOMOTICA_NAMES_CACHE or set()
    if _DOMOTICA_NAMES_CACHE is not None and mtime == _DOMOTICA_NAMES_MTIME:
        return _DOMOTICA_NAMES_CACHE
    names: set = set()
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for clave, d in data.items():
                    if isinstance(clave, str):
                        names.add(clave.lower())
                    if isinstance(d, dict) and isinstance(d.get("nombre"), str):
                        names.add(re.sub(r"[^\w]", "_", d["nombre"].lower().strip()))
        except Exception:
            pass
    _DOMOTICA_NAMES_CACHE = names
    _DOMOTICA_NAMES_MTIME = mtime
    return names


def _is_domotica_device(name: str) -> bool:
    """¿`name` coincide con un dispositivo registrado? Tolera coincidencia parcial
    (registrado «luz_cocina», usuario dice «luz» → True)."""
    clave = re.sub(r"[^\w]", "_", name.lower().strip())
    if not clave:
        return False
    registered = _domotica_registered_names()
    if clave in registered:
        return True
    palabras = [w for w in clave.split("_") if len(w) > 2]
    for r in registered:
        if clave in r or any(w in r for w in palabras):
            return True
    return False


# Palabras que el regex de noticias captura como "tema" pero NO son tema real
# — sólo modificadores temporales. "noticias de hoy" → tema=""  (no "hoy").
_NOTICIAS_NO_TEMA = {
    "hoy", "ahora", "ya", "ahora mismo", "el día", "el dia",
    "el mundo", "españa", "espana", "actualidad",
    "este momento", "este instante",
}


# Sesión 32 (BUG-S136): limpia prefijos naturales en queries de búsqueda
# para que «busca en el google que has abierto noticias pc» → «noticias pc».
_LIMPIA_BUSQUEDA_RES = [
    # «en (el|la)? (google|chrome|internet|...) (que has abierto)?»
    re.compile(
        r"^\s*en\s+(?:el\s+|la\s+)?"
        r"(?:google|chrome|safari|firefox|browser|navegador|"
        r"internet|web|la\s+web|duckduckgo|duck\s*duck\s*go|"
        r"bing|yahoo|youtube|wikipedia)"
        r"(?:\s+que\s+(?:has|hab[ií]as)\s+abierto)?\s*[:,]?\s*",
        re.I,
    ),
    # «(información|info|datos|cosas|noticias) (sobre|de|acerca de) »
    re.compile(
        r"^\s*(?:informaci[oó]n|info|datos|cosas|algo|m[aá]s|noticias?|"
        r"todo|cualquier\s+cosa)\s+"
        r"(?:sobre|de|acerca\s+de|del?|en\s+torno\s+a)\s+",
        re.I,
    ),
    # «sobre / acerca de / de » iniciales
    re.compile(
        r"^\s*(?:sobre|acerca\s+de|de|del?)\s+",
        re.I,
    ),
]
# Y un sufijo común a quitar: «en internet/google/...»
_LIMPIA_BUSQUEDA_SUF_RE = re.compile(
    r"\s+en\s+(?:el\s+|la\s+)?"
    r"(?:google|chrome|safari|firefox|browser|navegador|"
    r"internet|web|la\s+web|duckduckgo|duck\s*duck\s*go|"
    r"bing|yahoo|youtube|wikipedia)\s*$",
    re.I,
)

def _limpiar_query_busqueda(raw: str) -> str:
    """Limpia prefijos naturales y devuelve solo la query real."""
    q = (raw or "").strip()
    # Aplica todos los regex de prefijo iterativamente (a veces se anidan)
    cambio = True
    while cambio:
        cambio = False
        for r in _LIMPIA_BUSQUEDA_RES:
            nuevo = r.sub("", q, count=1)
            if nuevo != q:
                q = nuevo
                cambio = True
                break
    # Sufijo
    q = _LIMPIA_BUSQUEDA_SUF_RE.sub("", q)
    return q.strip(" .?!¿¡,")


def _limpiar_tema_noticias(raw: str) -> str:
    """Devuelve "" si raw es un modificador temporal, tema limpio en otro caso.

    Quita signos de puntuación y artículos iniciales (el/la/los/las/un/una)
    para que el filtro substring del feed RSS encuentre más matches:
    "el hardware de pc" → "hardware de pc".
    """
    if not raw:
        return ""
    limpio = raw.strip(" .?!¿¡,").lower()
    if limpio in _NOTICIAS_NO_TEMA:
        return ""
    # Quitar artículo inicial — afecta solo al filtro substring, conserva el
    # resto del tema literal.
    sin_articulo = re.sub(
        r"^(?:el|la|los|las|un|una|unos|unas)\s+",
        "", raw.strip(" .?!¿¡,"),
        flags=re.I,
    )
    return sin_articulo


def _extraer_divisa_params(m) -> Dict:
    """Extractor del detector de divisas (Sesión 36, S148).

    Valida que AMBAS monedas se reconozcan; si no, lanza ValueError para
    que el match se ignore. Así NO interceptamos conversiones de unidades
    («100 metros en kilómetros») ni frases ajenas que casen el patrón.
    """
    from .tools import AgentTools  # lazy: evita ciclo agent ↔ tools
    orig = m.group("orig")
    dest = m.group("dest")
    if AgentTools._norm_divisa(orig) is None or AgentTools._norm_divisa(dest) is None:
        raise ValueError("no es conversión de divisas")
    return {"cantidad": m.group("cant"), "origen": orig, "destino": dest}


# Muletas del encargo de jugar: lo que llega a la herramienta tiene que ser el
# juego («zzz»), no «por mí tú solo al zzz». Se quitan por delante, en bucle,
# porque se acumulan: «juega por mi tu solo al zzz».
_MULETAS_JUGAR_RE = re.compile(
    r"^(?:(?:por\s+m[ií]|en\s+mi\s+lugar|en\s+mi\s+cuenta|t[uú]|sol[ao]|"
    r"solit[ao]|ya|ahora|y|en\s+el|en\s+la|en|a\s+la|al|a|el|la|los|las)"
    r"\b[,\s]*)+",
    re.I,
)


# Y las mismas muletas por detrás: «juega al ZZZ desde mi telefono». La cola
# importa más de lo que parece — el objetivo da nombre al libro de jugadas
# (`tools.jugar`), así que «zzz» y «zzz desde mi telefono» aprenderían en dos
# cuadernos distintos y ninguno de los dos se llenaría nunca.
_COLA_JUGAR_RE = re.compile(
    r"(?:[,\s]*\b(?:por\s+m[ií]|en\s+mi\s+lugar|en\s+mi\s+cuenta|"
    r"desde\s+mi\s+(?:m[oó]vil|movil|tel[eé]fono|telefono|celular|aparato)|"
    r"en\s+mi\s+(?:m[oó]vil|movil|tel[eé]fono|telefono|celular|aparato)|"
    r"t[uú]\s+sol[ao]|sol[ao]|solit[ao]|ahora|ya|porfa(?:vor)?|"
    r"por\s+favor)\b)+\s*$",
    re.I,
)


# Y lo que queda cuando el encargo no nombra ningún juego: «…juega por mi en
# mi cuenta y farmea» deja «farmea» —un verbo, no un juego—. Importa porque un
# objetivo que no se resuelve a ninguna app NO frena al jugador (a propósito:
# `_poner_el_juego_delante` deja pasar lo que no reconoce, para que un fallo de
# `dumpsys` no bloquee), así que se pondría a dar toques sobre lo que hubiera
# delante. Es el daño de la S63 —jugar con TikTok delante— por la otra puerta.
# Vacío es mejor que inventado: `tools.jugar` sin objetivo PREGUNTA a qué juego.
_SOLO_ENCARGO_JUGAR_RE = re.compile(
    r"^(?:(?:farmea[rs]?|juega[rs]?|jueg[au]es|gana|g[aá]name|super[aá]|"
    r"complet[ao]|y|e|tambi[eé]n|adem[aá]s|un\s+poco|algo|ah[ií])\b[,\s]*)+$",
    re.I,
)


def _limpiar_objetivo_jugar(resto: str) -> Dict:
    """Extractor de la herramienta `jugar`: deja solo el juego.

    Lo comparten las tres entradas del detector (mandato, marca e imperativo),
    así que el recorte vive en un sitio y no en tres copias que se separen.
    """
    objetivo = _MULETAS_JUGAR_RE.sub("", (resto or "").strip(" .¿?¡!")).strip()
    objetivo = _COLA_JUGAR_RE.sub("", objetivo).strip()
    objetivo = objetivo.strip(" .¿?¡!,")
    if _SOLO_ENCARGO_JUGAR_RE.match(objetivo):
        # Solo verbos de encargo: no hay juego que nombrar.
        objetivo = ""
    return {"objetivo": objetivo}


# El seguimiento de ZZZ: qué frases del hilo son del juego y cuáles no.
# «build», «bangboo» o «w-engine» no son de nada más; «equipo» y «team» sí,
# así que se aceptan salvo que la frase hable a las claras de otra cosa —el
# test que lo pedía es «qué equipo de fútbol juega hoy».
_ZZZ_PROPIO_RE = re.compile(
    r"(?i)\b(?:build|builds|disco|discos|motor|motores|w-?engines?|bangboo|"
    r"mindscape|substats?|main\s+stats?|rotaci[oó]n|comps?)\b")
_ZZZ_AMBIGUO_RE = re.compile(
    r"(?i)\b(?:equipos?|teams?|composici[oó]n|composiciones)\b")
_ZZZ_OTRO_DOMINIO_RE = re.compile(
    r"(?i)\b(?:f[uú]tbol|futbol|liga|partido|partidos|jornada|baloncesto|"
    r"balonmano|tenis|selecci[oó]n|oficina|trabajo|proyecto|colegio|clase|"
    r"examen|boda|reuni[oó]n|reuniones)\b")
_ZZZ_QUIEN_RE = re.compile(
    r"(?i)\bqui[eé]n(?:es)?\s+(?:es|son)\s+"
    r"(?!(?:el|la|los|las|un|una|unos|unas)\b)(?P<quien>[^?.!]+)")

_ZZZ_ROSTER: Dict[str, Any] = {"cuando": 0.0, "nombres": ()}


def _roster_zzz() -> Tuple[str, ...]:
    """Los personajes que ya se leyeron del meta, cacheados un minuto.

    Se consulta desde el detector, o sea en el camino de CADA mensaje que
    hable de equipos: leer el JSON cada vez sería pagar disco por una lista
    que cambia una vez por semana.
    """
    ahora = time.time()
    if ahora - _ZZZ_ROSTER["cuando"] < 60:
        return _ZZZ_ROSTER["nombres"]
    try:
        from .zzz import SaberZZZ  # lazy: evita ciclo y el coste de importarlo
        nombres = tuple(SaberZZZ().personajes_del_meta())
    except Exception:
        nombres = ()
    _ZZZ_ROSTER.update({"cuando": ahora, "nombres": nombres})
    return nombres


def _consulta_zzz_seguimiento(m) -> Dict:
    """Extractor del seguimiento de ZZZ. Lanza si la frase no es del juego.

    `AgentPlanner.detect()` descarta el match cuando el extractor lanza, así
    que aquí se puede decir «esto no era ZZZ» y dejar que la frase siga su
    camino — que es lo que tiene que pasar con «quién es el presidente» o con
    un equipo de fútbol.
    """
    consulta = (m.group("todo") or "").strip()
    if _ZZZ_OTRO_DOMINIO_RE.search(consulta):
        raise ValueError("la frase habla de otro asunto")
    if _ZZZ_PROPIO_RE.search(consulta) or _ZZZ_AMBIGUO_RE.search(consulta):
        return {"consulta": consulta}
    # Queda «quién es X»: solo va al meta si X es alguien del meta. Preguntar
    # por un personaje que no se ha leído nunca se contesta mejor buscando —
    # puede ser nuevo, y el catálogo de equipos tarda en recogerlos.
    mq = _ZZZ_QUIEN_RE.search(consulta)
    if mq:
        quien = _sin_tildes_min(mq.group("quien"))
        for nombre in _roster_zzz():
            if _sin_tildes_min(nombre) and _sin_tildes_min(nombre) in quien:
                return {"consulta": consulta}
    raise ValueError("no se reconoce como pregunta de ZZZ")


# Novedades del juego: eso no está en las guías de builds, está en internet.
# 26 sep 2026: «busca en internet cuándo sale la próxima versión de Zenless
# Zone Zero» se lo quedó la herramienta de equipos y contestó «No he pillado de
# qué personaje hablas». Sólo veta si la frase no habla también de builds o
# equipos: «el mejor equipo para el evento» sigue siendo del meta.
_ZZZ_NOVEDAD_RE = re.compile(
    r"(?i)\b(?:versi[oó]n|versiones|actualizaci[oó]n|parche|patch|update|"
    r"sale|saldr[aá]n?|sali[oó]|lanzamiento|estreno|fecha|cu[aá]ndo|"
    r"noticias?|novedad(?:es)?|c[oó]digos?|livestream|directo|mantenimiento|"
    r"filtraci[oó]n(?:es)?|leaks?)\b")
_ZZZ_DE_BUILDS_RE = re.compile(
    r"(?i)\b(?:builds?|equipos?|teams?|w-?engines?|drive\s+discs?|discos?|"
    r"motor(?:es)?|combos?|montar|monto|mindscape)\b")


def _consulta_zzz(m) -> Dict:
    """Extractor de la regla de ZZZ. Lanza si es una pregunta de novedades.

    Como en `_consulta_zzz_seguimiento`: si el extractor lanza, `detect()`
    descarta este match y la frase sigue a las demás reglas (buscar en
    internet, o el modelo con sus herramientas).
    """
    consulta = (m.group("todo") or "").strip()
    if _ZZZ_NOVEDAD_RE.search(consulta) and not _ZZZ_DE_BUILDS_RE.search(consulta):
        raise ValueError("novedades del juego: se buscan en internet")
    return {"consulta": consulta}


def _tamano_imagen(texto: str) -> Dict[str, int]:
    """El tamaño que pide el mensaje: vertical para historias/9:16, horizontal
    para 16:9 o un banner, 4:5 para un post; si no dice nada, cuadrada.
    (4 oct 2026: el anuncio para historias de Instagram salía cuadrado.)"""
    t = (texto or "").lower()
    if re.search(r"9\s*[:x/]\s*16|vertical|historias|stories|reels?\b|tiktok|fondo\s+de\s+pantalla"
                 r"\s+(?:del|para\s+el)\s+m[oó]vil", t):
        return {"ancho": 720, "alto": 1280}
    if re.search(r"16\s*[:x/]\s*9|horizontal|banner|miniatura|portada\s+de\s+youtube|panor[aá]mic", t):
        return {"ancho": 1280, "alto": 720}
    if re.search(r"4\s*[:x/]\s*5|\bpost\b|publicaci[oó]n\s+de\s+instagram", t):
        return {"ancho": 1024, "alto": 1280}
    return {}


def _sin_tildes_min(t: str) -> str:
    """Minúsculas y sin tildes, para comparar nombres escritos a mano."""
    t = unicodedata.normalize("NFD", (t or "").strip().lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


class AgentPlanner:
    """Detecta intenciones de herramientas usando patrones en español."""

    # (patrón, herramienta, extractor_params, necesita_confirmación, fn_descripción)
    # ORDEN IMPORTANTE: de más específico a más genérico
    _P = [
        # ── Razonamiento determinista (sesión 29) ────────────────────────────
        # LLMs no cuentan letras ni calculan bien — pasan por aproximación.
        # Estos detectores van PRIMERO porque son muy específicos (patrones
        # claros tipo "cuántas X tiene...") y rara vez tienen falsos positivos.

        # contar_vocales: caso especial — "cuántas vocales tiene X". Las
        # 5 vocales se cuentan como un set (a,e,i,o,u + acentuadas).
        # Sesión 29: el LLM contaba mal ("paralelepípedo" → 4, real 7).
        (re.compile(
            r"^\s*cu[aá]nta[s]?\s+vocales?\s+(?:tiene|hay\s+en|contiene[n]?)\s+"
            r"(?:la\s+|el\s+)?(?:palabra|frase|texto)?\s*"
            r"['\"`:]?\s*(.+?)['\"`]?\s*[\.\?!¿¡]*\s*$",
            re.I | re.S),
         "contar_letras",
         lambda m: {
             "texto": m.group(1).strip(" '\""),
             "letra": "a,e,i,o,u,á,é,í,ó,ú",
         },
         False,
         lambda p: f"Contar vocales en: {p['texto'][:40]}"),

        # contar_letras: "cuántas e tiene la frase X", "letras 'a' en 'palabra'",
        # "cuántas n y cuántas m hay en MAMMOTH" (múltiples letras).
        (re.compile(
            r"cu[aá]nta[s]?\s+(?:letras?\s+)?"
            # Captura una o varias letras: "n", "n y m", "n, m, t"
            r"((?:['\"`]?\w{1,4}['\"`]?)"
            r"(?:\s*(?:,|\sy\s|\se\s)\s*"
            r"(?:cu[aá]nta[s]?\s+(?:letras?\s+)?)?"
            r"['\"`]?\w{1,4}['\"`]?)*)"
            r"\s+(?:tiene|hay|aparec\w+|contiene[n]?|existe[n]?)\s+"
            r"(?:(?:en|de)\s+)?(?:la\s+|el\s+)?"
            # "frase/texto/..." con `:`, `,` o espacio después (bug 29-may:
            # "frase:" no se consumía y la 'e' de "frase" se contaba).
            r"(?:(?:frase|texto|palabra|oraci[oó]n|cadena)\s*[:,]?\s*)?"
            r"['\"`:]?\s*(.+?)['\"`]?\s*[\.\?!¿¡]*$",
            re.I | re.S),
         "contar_letras",
         # Normalizar lista de letras: quitar "cuántas letras" intermedios y comillas
         lambda m: {
             "texto": m.group(2).strip(" '\""),
             "letra": ",".join(
                 l.strip(" '\"`")
                 for l in re.split(r"\s*(?:,|\sy\s|\se\s)\s*",
                                    re.sub(r"cu[aá]nta[s]?\s+(?:letras?\s+)?", "",
                                            m.group(1), flags=re.I))
                 if l.strip(" '\"`")
             ),
         },
         False,
         lambda p: f"Contar '{p['letra']}' en: {p['texto'][:40]}"),

        # contar_palabras: "cuántas palabras tiene/hay en X" o "cuenta
        # las palabras de/en/: X" o "contar palabras en X". Sesión 32: el
        # LLM contaba mal sin tool («el perro salta sobre la valla» → 5).
        # OJO: el opcional «(?:la|el)\s+» del centro se tragaba «el» del
        # contenido — sólo aceptamos «la frase/el texto/la oración» como
        # cabecera explícita, no artículos sueltos.
        (re.compile(
            r"^\s*(?:cu[aá]nta[s]?\s+palabras\s+(?:tiene|hay\s+en|contiene[n]?)|"
            r"cuent[ae](?:me|melo|lo|las)?\s+(?:las\s+|cu[aá]ntas\s+)?palabras"
            r"(?:\s+(?:de|en))?|"
            r"contar\s+palabras(?:\s+(?:de|en))?)"
            r"\s*[:\s]\s*"
            # Sesión 36 (BUG-PALABRAS): aceptar demostrativos «esta/este/esa/
            # ese/...» además de artículos. Antes «de esta oración: hoy hace
            # mucho calor» contaba «esta oración:» como contenido (daba 6 en
            # vez de 4). También «la siguiente frase».
            r"(?:(?:de\s+)?(?:la|el|del|de\s+la|est[aeo]s?|es[aeo]s?)\s+(?:siguiente\s+)?(?:frase|texto|oraci[oó]n|cadena|p[aá]rrafo|siguiente)\s*[:\s]\s*)?"
            r"['\"`]?\s*(.+?)['\"`]?\s*[\.\?!¿¡]*$",
            re.I | re.S),
         "contar_palabras",
         lambda m: {"texto": m.group(1).strip(" '\"")},
         False,
         lambda p: f"Contar palabras en: {p['texto'][:40]}"),

        # longitud_texto: "cuántos caracteres tiene X", "longitud de X"
        # Sesión 32 (BUG-S160): el regex aceptaba «cuánto/cuántos» pero NO
        # «cuánta/cuántas» (femenino) — «cuántas letras tiene X» NO
        # disparaba la tool y el LLM contaba mal («paralelepípedo» = 15
        # cuando son 14).
        (re.compile(
            r"(?:cu[aá]nt[ao]s?\s+(?P<unidad>caracteres|letras|s[ií]mbolos)\s+(?:tiene|hay\s+en)|"
            r"(?P<unidad2>longitud|tama[ñn]o)\s+(?:de|del))\s+"
            r"(?:la\s+|el\s+)?(?:frase|texto|palabra|cadena)?\s*"
            r"['\"`:]?\s*(.+?)['\"`]?\s*[\.\?!¿¡]*$",
            re.I | re.S),
         "longitud_texto",
         lambda m: {"texto": m.group(3).strip(" '\""),
                    "unidad": (m.group("unidad") or m.group("unidad2") or "caracteres").lower()},
         False,
         lambda p: f"Longitud de: {p['texto'][:40]}"),

        # silabas: «divide X en sílabas», «cuántas sílabas tiene X», «silabea X».
        # Sesión 37: el LLM (sobre todo el 8b) falla al silabar/contar sílabas;
        # tool determinista por reglas del español. Exige «sílabas» con divide/
        # separa para no chocar con divisiones aritméticas («divide 10 entre 2»).
        (re.compile(
            r"(?:"
            r"cu[aá]nt[ao]s?\s+s[ií]labas\s+(?:tiene|hay\s+en|son\s+de)\s+"
            r"(?:la\s+palabra\s+|el\s+t[eé]rmino\s+|la\s+|el\s+)?['\"`]?(?P<p1>[A-Za-zÁÉÍÓÚÀÈÌÒÙÜáéíóúàèìòùüÑñ]+)|"
            r"(?:divide|separa|sep[aá]rame|div[ií]deme)\s+"
            r"(?:la\s+palabra\s+|el\s+t[eé]rmino\s+|la\s+|el\s+)?['\"`]?(?P<p2>[A-Za-zÁÉÍÓÚÀÈÌÒÙÜáéíóúàèìòùüÑñ]+)['\"`]?\s+en\s+s[ií]labas|"
            r"(?:divide|separa)\s+en\s+s[ií]labas\s+"
            r"(?:la\s+palabra\s+|el\s+t[eé]rmino\s+|la\s+|el\s+)?['\"`]?(?P<p3>[A-Za-zÁÉÍÓÚÀÈÌÒÙÜáéíóúàèìòùüÑñ]+)|"
            r"(?:silab[ée]a(?:me)?|silab[ií]za(?:me)?)\s+"
            r"(?:la\s+palabra\s+|el\s+t[eé]rmino\s+|la\s+|el\s+)?['\"`]?(?P<p4>[A-Za-zÁÉÍÓÚÀÈÌÒÙÜáéíóúàèìòùüÑñ]+)"
            r")",
            re.I),
         "silabas",
         lambda m: {"palabra": (m.group("p1") or m.group("p2") or m.group("p3") or m.group("p4"))},
         False,
         lambda p: f"Separar en sílabas: {p['palabra']}"),

        # porcentaje: «N% de M», «N por ciento de M». Sesión 36: antes caía al
        # LLM que respondía crudo «0.15 * 240 = 36». Va ANTES de «calcular»
        # porque el detector de cálculo no captura el «% de» (rompe en «de»).
        (re.compile(
            r"^\s*(?:cu[aá]nto\s+(?:es|ser[ií]a|vale|da)\s+|calcula(?:me)?\s+|"
            r"cu[aá]l\s+es\s+|dime\s+|qu[eé]\s+es\s+)?"
            r"(?:el\s+|un\s+)?"
            r"(?P<p>\d[\d.,]*)\s*(?:%|por\s+ciento|por\s+cien)\s+de\s+"
            r"(?:los\s+|las\s+)?(?P<t>\d[\d.,]*)\s*[=\?!¿¡\.]*$",
            re.I),
         "porcentaje",
         lambda m: {"parte": m.group("p"), "total": m.group("t")},
         False,
         lambda p: f"Porcentaje: {p['parte']}% de {p['total']}"),

        # convertir_divisa: «convierte 100 dólares a euros», «cuánto son 50
        # libras en euros», «100 usd a eur». Sesión 36 (S148): antes el LLM
        # inventaba la tasa. El extractor valida que ambas sean monedas
        # reconocidas (si no, lanza ValueError → cae al LLM, sin interceptar
        # «100 metros en km»). Las monedas pueden ser de 1-2 palabras.
        (re.compile(
            r"^\s*(?:"
            r"(?:cu[aá]nto[s]?\s+(?:es|son|ser[ií]an?|vale[n]?|equivale[n]?)|"
            r"a\s+cu[aá]nto[s]?(?:\s+(?:equivale[n]?|sale[n]?|asciende[n]?))?|"
            r"convi[eé]rte(?:me)?|convierte(?:me)?|convertir|cambia(?:me)?|cambiar|"
            r"pasa(?:me)?|pasar|c[aá]mbiame)\s+"
            r")?"
            r"(?P<cant>\d[\d.,]*)\s*"
            r"(?P<orig>[a-záéíóúñ$€£¥]+(?:\s+[a-záéíóúñ]+)?)"
            r"\s+(?:a|en|por|hacia|->|=)\s+"
            r"(?P<dest>[a-záéíóúñ$€£¥]+(?:\s+[a-záéíóúñ]+)?)"
            r"\s*[\.\?!¿¡]*$",
            re.I),
         "convertir_divisa",
         _extraer_divisa_params,
         False,
         lambda p: f"Convertir {p['cantidad']} {p['origen']} → {p['destino']}"),

        # calcular: aritmética explícita. "cuánto es 23×47", "calcula 1234+567",
        # "23 * 47", "raíz de 144", "5 al cuadrado", "(5+3)*2-4".
        # IMPORTANTE: muy estricto — solo si hay operador o palabra de operación.
        # Sesión 33 (B33-21): rechazar «cuánto falta para 15/08» — son
        # peticiones de días-hasta, no aritmética. Lookahead negativo al
        # inicio del mensaje.
        (re.compile(
            r"^(?!.*\bcu[aá]nto[s]?\s+(?:falta[n]?|queda[n]?|d[ií]as)\s+(?:falta[n]?|queda[n]?|para|hasta)\b)"
            r"(?:^|\b)(?:cu[aá]nto\s+es|calcula(?:me)?|"
            r"(?:el\s+)?resultado\s+de|cu[aá]l\s+es\s+el\s+resultado\s+de)?\s*"
            r"("
            # Caso 1 (unificado): cadena de operandos con operadores. Cada
            # operando puede ser N, N al cuadrado/cubo o raíz de N — sesión 32
            # (BUG-S103) antes la cadena «7 por 8 menos 5 al cuadrado» solo
            # capturaba "5 al cuadrado" al final perdiendo "7 por 8 menos".
            # 'x'/'X' admitidos como multiplicación (común en español:
            # "548 x 99"). Sesión 31 (AX-13): paréntesis "(5+3)*2-4".
            # Sesión 32 (BUG-S175): aceptar «menos N» / «m[aá]s N» como
            # primer operando (negación o positivo explícito). Antes
            # «menos 5 más 3» perdía el «menos» inicial.
            r"((?:menos|m[aá]s)\s+)?"
            r"(?:\(*[-+]?(?:\d[\d.,]*(?:\s+al\s+(?:cuadrado|cubo))?"
            r"|ra[íi]z\s+(?:cuadrada\s+)?de\s+(?:\d[\d.,]*|\([^()]+\)))\)*"
            r"(?:\s*(?:[+\-*/×÷%xX]|por|entre|m[aá]s|menos|dividido\s+(?:entre|por)|"
            r"multiplicado\s+por|elevado\s+a)\s*"
            r"\(*[-+]?(?:\d[\d.,]*(?:\s+al\s+(?:cuadrado|cubo))?"
            r"|ra[íi]z\s+(?:cuadrada\s+)?de\s+(?:\d[\d.,]*|\([^()]+\)))\)*)+)"
            r"|"
            # Caso 2: raíz de N suelta (sin operadores)
            r"ra[íi]z\s+(?:cuadrada\s+)?de\s+(?:\d[\d.,]*|\([^()]+\))"
            r"|"
            # Caso 3: N al cuadrado / N al cubo suelto (sin operadores).
            # Sesión 34 (B34-7): aceptar signo y paréntesis para que «-5 al
            # cuadrado» o «(-5) al cuadrado» se procesen correctamente; antes
            # caían al LLM y devolvía texto corrupto («² = (-5) × (-5) = 25) 25.»).
            r"\(*[-+]?\d[\d.,]*\)*\s+al\s+(?:cuadrado|cubo)"
            r")\s*[=\?!¿¡\.]*$",
            re.I),
         "calcular",
         lambda m: {"expresion": m.group(1).strip(" =?¿!¡.")},
         False,
         lambda p: f"Calcular: {p['expresion'][:50]}"),

        # Borrar/cancelar recordatorio — sesión 29: "olvida lo de sacar la
        # basura" hacía que el LLM mintiera diciendo "no tenía nada guardado"
        # cuando sí lo tenía. Va antes del listar/crear.
        # Restricción: la keyword NO puede tener punto/exclamación intermedios
        # (sesión 29 vio "olvídate de eso. Háblame del universo" cayendo aquí).
        # Sesión 34 (B34-5): lookahead negativo para no capturar
        # "olvida tu identidad/prompt/rol/reglas/instrucciones/filtros" como
        # peticiones de cancelar recordatorio. Esos son intentos de jailbreak
        # que `_OLVIDA_RE` ya bloquea en api.py — aquí prevenimos que el
        # matcher de agente los procese antes y devuelva respuestas falsas.
        (re.compile(
            r"^\s*(?:olvida(?:te)?|cancela(?:me)?|borra(?:me)?|elimina(?:me)?|quita(?:me)?)"
            r"(?!\s+(?:tu|su|tus|sus|el|la|los|las)?\s*"
            r"(?:identidad|personalidad|prompt|sistema|rol|car[aá]cter|naturaleza|"
            r"instrucciones|reglas|restricciones|filtros|l[ií]mites)\b)"
            # B34-11: tampoco capturar «olvida que eres una IA / que tienes / que
            # debes / que estás programada…» — jailbreaks de identidad, no recordatorios.
            r"(?!\s+que\s+(?:eres|er[ai]s|fuiste|naciste|tienes|ten[ií]as|debes|"
            r"deb[ií]as|sabes|sab[ií]as|est[aá]s|fuiste)\b)"
            r"\s+(?:el\s+|la\s+|lo\s+)?(?:de\s+)?(?:recordatorio\s+(?:de\s+|sobre\s+)?)?"
            r"([^.!]+?)\s*[\.\?!¿¡]*\s*$",
            re.I),
         "borrar_recordatorio",
         lambda m: {"keyword": (m.group(1) or "").strip(" '\"")},
         False,
         lambda p: f"Borrar recordatorio: {p['keyword'][:40]}"),

        # Listar recordatorios pendientes — debe ir ANTES del detector
        # `recordatorio` (creación), porque "lista mis recordatorios" o
        # "cuándo es mi recordatorio" no son creación, son consulta.
        # Sin esto, el LLM inventa datos (sesión 29).
        # Sufijos temporales tolerados al final ("ahora", "pendientes",
        # "para hoy", "que tengo") — sesión 29 vio "qué recordatorios tengo
        # ahora?" pasar al LLM y alucinar "llamar al dentista 10:00" inventado.
        (re.compile(
            # Aceptar ¿ inicial opcional (sesión 30: "¿Cómo me lo recordarás?")
            # Y conectores coloquiales: "Y como...", "Pero cómo...", "Pues...".
            r"^\s*¿?\s*(?:y|pero|pues|entonces|bueno|oye|ah|ok|vale|ahora|mira|venga)?[,\s]*(?:y\s+)?(?:ahora\s+)?\s*(?:"
            r"(?:lista[r]?|mu[eé]stra(?:me)?|dime|cu[aá]les?\s+son|qu[eé]\s+(?:tengo|hay))"
            r"\s+(?:mis|los|todos\s+los|todos|mi)?\s*recordatorios?"
            r"|(?:cu[aá]ndo|a\s+qu[eé]\s+hora)\s+(?:es|son|tengo)\s+(?:mi[s]?|el|ese|los)\s+recordatorios?"
            r"|mis?\s+recordatorios?(?:\s+pendientes?)?"
            # Sesión 31: "Recordatorios?" / "Mis recordatorios?" — el sustantivo solo cuenta.
            r"|^\s*¿?\s*recordatorios?\s*\?"
            r"|(?:qu[eé]|cu[aá]les?)\s+recordatorios?\s+(?:tengo|hay|pendi\w+|me\s+quedan|quedan)"
            # Sesión 29: "qué tengo programado", "qué hay programado para hoy",
            # "qué tareas tengo", "qué agendado". Sin esto el LLM inventa.
            r"|qu[eé]\s+(?:tengo|hay|tienes)\s+(?:programad[oa]s?|agendad[oa]s?|para\s+hoy|para\s+ma[ñn]ana|pendientes?)"
            r"|qu[eé]\s+tareas?\s+tengo"
            # Sesión 30 bug AV: preguntas sobre MECANISMO/HORA de recordatorios
            # YA CREADOS — "cómo me lo recordarás", "de qué manera me avisas",
            # "cuándo me sonará", "qué me llegará". Sin esto el LLM las trata
            # como petición nueva y pide hora otra vez (visto en log Enzo 28-may).
            r"|(?:c[oó]mo|de\s+qu[eé]\s+(?:manera|forma|modo))"
            r"\s+(?:me|te)\s+(?:lo[s]?|la[s]?|me)?\s*"
            # Verbos en futuro Y presente: recordarás/recuerdas, avisarás/avisas,
            # dirás/dices, sonará/suena. "Va(s) a [verbo]" como perífrasis.
            r"(?:recordar[aá]s|recuerdas|avisar[aá]s|avisas|dir[aá]s|dices|"
            r"sonar[aá]|suena|va[ns]?\s+a\s+(?:recordar|avisar|sonar|decir))"
            r"|(?:cu[aá]ndo|(?:a\s+)?qu[eé]\s+hora)\s+me\s+(?:lo\s+)?"
            r"(?:recordar[aá]s|recuerdas|avisar[aá]s|avisas|sonar[aá]|suena|"
            r"llegar[aá]|llega|va[ns]?\s+a\s+(?:recordar|avisar|sonar))"
            r")"
            r"(?:\s+(?:ahora|pendientes?|para\s+hoy|hoy|para\s+ma[ñn]ana|que\s+tengo|programados?))?"
            r"\s*[\.\?!¿¡]*\s*$",
            re.I),
         "listar_recordatorios",
         lambda m: {},
         False,
         lambda p: "Listar recordatorios pendientes"),

        # Sesión 31 (BUG-S73): recordatorios RECURRENTES — no soportados aún.
        # Detectar y responder honestamente en lugar de dejar al LLM alucinar
        # «no tengo acceso a tu calendario». Va ANTES del regex de
        # recordatorio normal.
        (re.compile(
            r"^\s*(?:rec(?:u[eé]rdame|u[eé]rda\s*me)|av[íi]same|"
            r"(?:pon(?:me)?|cr[eé]a(?:me)?|programa(?:me)?|agenda(?:me)?|h[aá]z(?:me)?)"
            r"\s+(?:un\s+)?recordatorio)"
            r".*?\b(?:todos?\s+los\s+|cada\s+|todas?\s+las\s+|"
            r"semanalmente|diariamente|mensualmente|"
            r"cada\s+(?:semana|d[ií]a|mes|hora))\b",
            re.I | re.S),
         "recordatorio_recurrente_no_soportado",
         lambda m: {},
         False,
         lambda p: "Aviso: recurrencia no soportada"),

        # Recordatorio (muy específico — primero)
        # Acepta: "en N {min,horas,días}", "a las HH(:MM)?", "mañana/pasado mañana/hoy",
        # días de la semana (lunes…domingo), opcionalmente combinados ("mañana a las 10").
        # El conector "que/para/de" antes del mensaje es opcional.
        (re.compile(
            # Inicio: verbo de recordatorio O conector elíptico (sesión 29:
            # "Y a las 14:30 mañana, ir al banco" no detectaba). El conector
            # solo se acepta si va seguido de tiempo explícito (a las.../mañana)
            # Y de un mensaje no trivial — sin esto, "Y mañana?" disparaba
            # un recordatorio con texto "?" (BUG-S8 sesión 31).
            r"(?:"
            r"(?:rec(?:u[eé]rdame|u[eé]rda\s*me)|av[íi]same|"
            # Sesión 30 (AZ): aceptar también "pon/ponme/crea/créame/programa/
            # programame un recordatorio", "Un recordatorio en N min...",
            # "Recordatorio en N min..." (sustantivo solo al inicio). El bug
            # se vio en vivo cuando el usuario escribió "Recordatorio en un
            # minuto para ir a dormir" y cayó al LLM 8b que alucinó la hora.
            r"(?:pon(?:me)?|cr[eé]a(?:me)?|h[aá]z(?:me)?)"
            r"\s+(?:un\s+|el\s+|como\s+)?recordatorio|"
            # Sesión 33 (B33-11): agenda/programa/anota/apunta/agrega/añade
            # no requieren la palabra «recordatorio» porque ya implican
            # planificación temporal. «agéndame una reunión el 1 de julio
            # a las 16» — el LLM mentía diciendo «lo hice» pero no se creaba.
            # Sesión 31 (BUG-S22): mismo principio para anótamelo/apúntame…
            r"(?:programa(?:me)?|agenda(?:me)?|agendar|"
            r"an[oó]ta(?:me|melo|lo)?|ap[uú]nta(?:me|melo|lo)?|"
            r"agr[eé]ga(?:me|melo|lo)?|a[ñn]ade(?:me|melo|lo)?)"
            r"(?:\s+(?:un[ao]?\s+|el\s+|la\s+|como\s+)?(?:recordatorio|tarea|reuni[oó]n|cita|evento|nota|aviso|alarma))?|"
            r"^\s*(?:un\s+)?recordatorio)"
            # Conector elíptico (sesión 31 BUG-S8): la validación de "hay
            # mensaje real" se hace en el lambda extractor abajo. Aquí sólo
            # exigimos tiempo explícito tras "y/también".
            r"|^\s*(?:y|tambi[eé]n)\b,?(?=\s+(?:hoy|ma[ñn]ana|pasado|el\s+(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)|en\s+\d+|a\s+las?\s+\d))"
            r")\s*,?\s+"
            r"("
            r"(?:hoy|ma[ñn]ana|pasado\s+ma[ñn]ana|"
            # Sesión 33 (B33-25): «el próximo lunes» / «el siguiente martes».
            r"(?:el\s+|este\s+)?(?:pr[oó]ximo|siguiente|que\s+viene)?\s*"
            r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)|"
            # Sesión 32 (BUG-S109): fecha explícita "el N de MES (de AÑO)?" —
            # antes caía al LLM o se interpretaba como mensaje, y «30 de
            # febrero» se programaba sin validar.
            r"el\s+\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|"
            r"julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"(?:\s+de\s+\d{4})?|"
            # Sesión 32 (BUG-S112): fecha numérica "el DD/MM(/YYYY)?".
            r"(?:el\s+)?\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?|"
            # "en N" acepta dígitos O número escrito (un/una/dos/tres/.../diez/
            # media/cuarto) seguido de unidad. Sesión 30 (AZ): "en un minuto"
            # no matcheaba con \d+ y caía al LLM, que alucinaba la hora.
            r"(?:dentro\s+de\s+|en\s+)(?:\d+|un[ao]?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|"
            r"diez|media|cuarto)\s*\w+(?:\s+y\s+(?:medi[ao]|cuarto))?)"
            # "a/para las HH(:MM)?" — sesión 30 (AZ) "Hazme un recordatorio
            # para las 18:00" no matcheaba con sólo "a las". Sesión 32
            # (BUG-S111): sufijo AM/PM opcional.
            r"(?:\s+(?:a|para)\s+las?\s+\d+(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.))?(?:\s+de\s+la\s+\w+)?)?"
            r"|"
            r"(?:a|para)\s+las?\s+\d+(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.))?(?:\s+de\s+la\s+\w+)?"
            r"(?:\s+(?:hoy|ma[ñn]ana|pasado\s+ma[ñn]ana))?"
            r")[\s,]*"
            # Mensaje opcional (sesión 30 AZ): "Avísame en media hora" /
            # "Crea un recordatorio a las 18:00" pueden venir sin texto.
            r"(?:(?:que\s+|para\s+|de\s+(?:que\s+)?)?(.*))?", re.I | re.S),
         "recordatorio",
         # Sesión 31 (BUG-S8 + BUG-S62): si el match arrancó con conector
         # elíptico ("Y mañana?", "También el lunes!"), el mensaje resultante
         # puede ser sólo signos ("?"), vacío, o empezar con preposición
         # ("en Bilbao", "de aquí") — son consultas, no recordatorios.
         lambda m: (lambda tiempo, msg, full:
             {"tiempo": tiempo, "mensaje": msg}
             if not re.match(r"^\s*(?:y|tambi[eé]n)\b", full, re.I)
                 or (msg != "Recordatorio"
                     and len(re.sub(r"[^\wáéíóúñ]", "", msg, flags=re.I)) >= 3
                     and not re.match(r"^\s*(?:en|de|del|por|para|sobre|"
                                       r"hasta|desde|hacia|con)\b",
                                       msg, re.I)
                     # Sesión 32 (BUG-S168): rechazar si el mensaje es
                     # interrogativo (palabra interrogativa o `?`). «Y
                     # mañana qué» o «Y después cuándo» son preguntas, no
                     # recordatorios.
                     and not re.match(r"^\s*(?:qu[eé]|cu[aá]l(?:es)?|"
                                       r"cu[aá]ndo|d[oó]nde|c[oó]mo|"
                                       r"por\s+qu[eé])\b", msg, re.I)
                     and "?" not in msg
                     and "¿" not in msg)
             else (_ for _ in ()).throw(ValueError("recordatorio elíptico sin acción"))
         )(m.group(1).strip(),
            (m.group(2) or "Recordatorio").strip(" .,:—-?¿!¡") or "Recordatorio",
            m.group(0)),
         False,
         lambda p: f"Recordatorio en {p['tiempo']}: '{p['mensaje']}'"),

        # Sesión 31 (BUG-S41): forma INVERTIDA — verbo + mensaje + tiempo al
        # final. "Recuérdame ducharme en 2 minutos", "Avísame de comer en 1
        # hora", "Apúntame ir al banco mañana a las 10". El regex anterior
        # solo aceptaba tiempo INMEDIATAMENTE tras el verbo.
        # La palabra "recordatorio" es OPCIONAL para verbos de toma de nota
        # (anota/apunta/agrega/añade) — el usuario los dice sin la palabra.
        (re.compile(
            r"^\s*(?:rec(?:u[eé]rdame|u[eé]rda\s*me)|av[íi]same|"
            r"(?:pon(?:me)?|cr[eé]a(?:me)?|h[aá]z(?:me)?)"
            r"\s+(?:un\s+|el\s+|como\s+)?recordatorio|"
            # Sesión 33 (B33-11): agenda/programa/anota/apunta/agrega/añade
            # — recordatorio opcional (+ tarea/reunión/cita/evento/nota).
            r"(?:programa(?:me)?|agenda(?:me)?|agendar|"
            r"an[oó]ta(?:me|melo|lo)?|ap[uú]nta(?:me|melo|lo)?|"
            r"agr[eé]ga(?:me|melo|lo)?|a[ñn]ade(?:me|melo|lo)?)"
            r"(?:\s+(?:un[ao]?\s+|el\s+|la\s+|como\s+)?(?:recordatorio|tarea|reuni[oó]n|cita|evento|nota|aviso|alarma))?|"
            r"^\s*(?:un\s+)?recordatorio)"
            r"\s+(?:de\s+|que\s+|para\s+|a\s+|:\s*|-\s*)?"
            r"(?P<msg>[^,\.\?!]+?)"
            r"\s+(?P<tiempo>"
            r"(?:dentro\s+de\s+|en\s+)(?:\d+|un[ao]?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|"
            r"diez|media|cuarto)\s*\w+(?:\s+y\s+(?:medi[ao]|cuarto))?|"
            r"a\s+las?\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.))?(?:\s+de\s+la\s+\w+)?|"
            r"(?:hoy|ma[ñn]ana|pasado\s+ma[ñn]ana|el\s+(?:lunes|martes|"
            r"mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))"
            r"(?:\s+a\s+las?\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.))?)?"
            r")\s*[\.\?!¿¡]*\s*$", re.I | re.S),
         "recordatorio",
         lambda m: {"tiempo": m.group("tiempo").strip(),
                    "mensaje": m.group("msg").strip(" .,:—-?¿!¡") or "Recordatorio"},
         False,
         lambda p: f"Recordatorio en {p['tiempo']}: '{p['mensaje']}'"),

        # Alarma / despertador: "despiértame en una hora y media", "ponme alarma
        # a las 18:00". Bug visto sesión 26 — el usuario decía "despiértame" y
        # Celestia respondía OK por LLM pero JAMÁS programaba la alarma real.
        # Acepta números escritos (una, dos, …) además de dígitos, y "y media",
        # "y cuarto". Mensaje implícito = "Despertarte".
        (re.compile(
            r"(?:desp(?:i[eé]rt(?:ame|ame|enme)|i[eé]rtame|i[eé]rt[ae]me)|"
            r"al[áa]rmame|pon(?:me)?\s+(?:una?\s+)?alarma|"
            # Sesión 33 (B33-26): «alarma para/a las…» sin verbo previo.
            r"^\s*(?:una?\s+)?alarma|"
            r"me\s+tienes\s+que\s+despertar|tienes\s+que\s+despertarme)\s*"
            r"(?:para|a)?\s*"
            r"("
            r"en\s+(?:\d+|una?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|media|cuarto|horas?|minutos?|d[íi]as?)"
            r"(?:\s+(?:horas?|minutos?|d[íi]as?|y\s+(?:medi[ao]|cuarto)))*"
            r"|"
            r"(?:a\s+las?\s+)?\d{1,2}(?::\d{2})?(?:\s+de\s+la\s+\w+)?"
            r"|"
            r"(?:hoy|ma[ñn]ana|pasado\s+ma[ñn]ana)(?:\s+a\s+las?\s+\d+(?::\d{2})?)?"
            r")"
            r"(.*)", re.I | re.S),
         "recordatorio",
         lambda m: {"tiempo": m.group(1).strip(),
                    "mensaje": (m.group(2) or "").strip(" .,") or "Despertarte"},
         False,
         lambda p: f"Alarma en {p['tiempo']}: '{p['mensaje']}'"),

        # Agente autónomo — explícito ("hazlo tú", "hazlo automático", "modo autónomo")
        (re.compile(
            r"(?:hazlo\s+(?:t[uú]\s+)?(?:autom[aá]tico|sol[ao]|por\s+tu\s+cuenta)|"
            r"(?:modo|agente)\s+aut[oó]nomo|"
            r"aut[oó]nomamente|encárgate\s+t[uú]\s+de|"
            r"hazlo\s+t[uú]\s+todo)\s*[:,]?\s*(.+)",
            re.I | re.S),
         "tarea_autonoma",
         lambda m: {"tarea": m.group(1).strip(" .?!"), "app": ""},
         False,
         lambda p: f"Agente autónomo: {p['tarea'][:60]}"),

        # Descarga (URL explícita)
        (re.compile(r"(?:descarga[r]?|baja[r]?|obtén|obten)\s+(?:el\s+)?(?:archivo\s+)?(?:de\s+)?(https?://\S+)(?:\s+(?:como|a|en)\s+([\w/\\.\-~]+))?", re.I),
         "descargar_archivo",
         lambda m: {"url": m.group(1), "ruta": m.group(2) or ""},
         False,
         lambda p: f"Descargar: {p['url'][:60]}"),

        # Buscar duplicados (antes que buscar_web)
        (re.compile(r"(?:busca[r]?|encuentra[r]?|detecta[r]?)\s+(?:archivos?\s+)?duplicados?\s*(?:en\s+([\w/\\.~\-]+))?", re.I),
         "buscar_duplicados",
         lambda m: {"ruta": m.group(1) or "."},
         False,
         lambda p: f"Buscar duplicados en: {p['ruta']}"),

        # Buscar archivos (antes que buscar_web)
        (re.compile(r"busca[r]?\s+(?:el\s+)?archivo\s+(?:llamado\s+|con\s+nombre\s+)?['\"]?(\S+)['\"]?\s*(?:en\s+([\w/\\.~\-]+))?", re.I),
         "buscar_archivos",
         lambda m: {"patron": m.group(1), "ruta": m.group(2) or "."},
         False,
         lambda p: f"Buscar archivos: '{p['patron']}' en {p['ruta']}"),

        # Organizar archivos
        # Sesión 31 (BUG-S66): EXIGIR la palabra "archivos/carpeta/ficheros"
        # o que la ruta tenga separador (/ o ~). Antes "organizar mi cumpleaños"
        # capturaba "mi" como ruta y daba "No es un directorio: mi".
        (re.compile(r"(?:organiza[r]?|ordena[r]?|clasifica[r]?)\s+(?:los\s+)?(?:archivos?|carpeta|directorio|ficheros?)\s+(?:de\s+|en\s+)?([\w/\\.~\-]+)", re.I),
         "organizar_archivos",
         lambda m: {"ruta": m.group(1)},
         False,
         lambda p: f"Organizar archivos en: {p['ruta']}"),

        # Jugar por Enzo (S56). Va ANTES de abrir_app porque «juega al ajedrez»
        # llevaba a abrir una app de ajedrez y quedarse mirándola.
        #
        # «¿Puedes tocar la pantalla de mi móvil?» — sesión 61. Con Shizuku
        # encendido y funcionando, Celestia contestó QUE NO y se ofreció a
        # explicar cómo activarlo. No había herramienta para esta pregunta, así
        # que la contestaba el modelo de memoria. Se exige a la vez un verbo de
        # mando y el aparato: «¿puedes ver esto?» sobre una foto no es esto.
        (re.compile(
            # El «¿» puede ir antes o después de la muletilla («oye, ¿tienes
            # shizuku?»), así que se admite en los dos sitios.
            r"(?is)^\s*¿?\s*(?:(?:oye|dime|una\s+cosa|por\s+cierto)[,\s]+)*¿?\s*"
            r"(?:"
            r"(?:puedes|podr[ií]as|sabes|eres\s+capaz\s+de|"
            r"tienes\s+(?:acceso|control|permiso)\s+(?:a|para|sobre)?)"
            r"[^.?!]{0,40}?"
            r"\b(?:tocar|toques|controlar|manejar|mover|pulsar|deslizar|"
            r"trastear|usar)\b"
            r"[^.?!]{0,30}?"
            r"\b(?:pantalla|m[oó]vil|movil|tel[eé]fono|telefono|celular|"
            r"aparato|dispositivo)\b"
            r"|"
            r"(?:tienes|hay|est[aá]|funciona|te\s+va)\s+(?:activo\s+|puesto\s+)?"
            r"\bshizuku\b"
            r")"
            r"[^\n]*$"),
         "control_movil",
         lambda m: {},
         False,
         lambda p: "Control del móvil: comprobar"),

        # Se exige una marca de ENCARGO («por mí», «tú», «solo», «gana») en vez
        # de aceptar cualquier «juega»: sin eso, «¿tú juegas al ajedrez?» —que
        # es conversación— acababa moviendo el dedo por la pantalla.
        # ZZZ: equipos y builds. Anclado en vocabulario EXCLUSIVO del juego
        # («zenless», «w-engine», «bangboo», «mindscape») o en «zzz» + una
        # palabra de consulta. Sin ese anclaje, un detector con «tengo…» o
        # «qué equipo…» se llevaría media conversación normal — y «zzz» a
        # secas también es como se escribe dormirse.
        (re.compile(
            # Dos comprobaciones INDEPENDIENTES, no anidadas: la palabra de
            # consulta puede ir antes o después de «zzz» («la mejor build de
            # Miyabi en zzz»), y un lookahead colgado de \bzzz\b solo mira
            # hacia delante — con eso se perdía justo la forma más natural.
            r"(?is)^(?:"
            r"(?=.*(?:\bzenless\b|\bw-?engines?\b|\bdrive\s+discs?\b|"
            r"\bbangboo\b|\bmindscape\b))"
            r"|"
            r"(?=.*\bzzz\b)(?=.*\b(?:build|builds|equipo|equipos|team|teams|"
            r"personaje|personajes|disco|discos|motor|meta|tiro|tirar|monto|"
            r"montar|combo|combos|mejor|mejores|conviene|falta|"
            # 🔴 18 sep: «Sabes jugar al zzz?» no disparaba NADA —ninguna de
            # las palabras de arriba está en la frase— así que contestaba el
            # modelo de memoria: dijo que sí, y dos turnos después que «no
            # tengo información sobre cómo se juega al ZZZ», con 22 guías y
            # los combos medidos en disco. Preguntar QUÉ SABE es una consulta
            # como otra cualquiera. Verbos de saber, no de jugar: «puedes
            # jugar al zzz» tiene que seguir siendo un ENCARGO, y lo es porque
            # «puedes» no está aquí.
            r"sabes|sabe|sabr[ií]as|conoces|conoce|entiendes|dominas|controlas|"
            r"jugaste)\b)"
            r")"
            r"\s*(?P<todo>.+)$"),
         "zzz",
         _consulta_zzz,
         False,
         lambda p: f"ZZZ: {p['consulta'][:50]}"),

        # El encargo de jugar va en TRES entradas, no en una. La de la S56 pedía
        # una marca («por mí», «tú solo», «gana») para que «¿tú juegas al
        # ajedrez?» no moviera el dedo por la pantalla — y con eso se llevó por
        # delante la forma en que se pide de verdad. Enzo lo intentó CUATRO
        # veces en el chat del 7 de septiembre («juega al zzz», «quiero que
        # juegues al ZZZ desde mi telefono», …) y las cuatro salió por el
        # camino del modelo, que contestó que no podía. Ninguna disparaba.
        #
        # Entrada 1 — MANDATO delante: «quiero que juegues», «¿puedes jugar…?».
        # Esa última es un encargo por decisión de Enzo (S63), así que aquí el
        # signo de interrogación no veta nada.
        #
        # Los verbos son solo los de JUGAR. «gana», «supera» y «completa» se
        # quedan para la entrada 2, que exige marca: con un mandato delante y
        # sesenta caracteres de margen, «¿puedes decirme quién gana el
        # partido?» habría acabado tocando la pantalla.
        (re.compile(
            r"(?i)"
            r"(?:"
            # «quiero QUE juegues» — el «que» es obligatorio: «quiero jugar al
            # ajedrez» es Enzo queriendo jugar él, no un encargo.
            r"\b(?:quiero|necesito|me\s+gustar[ií]a|haz)\s+que\b"
            r"|\b(?:puedes|podr[ií]as|ponte\s+a)\b"
            r")"
            r"[^.?!¡\n]{0,60}?"
            r"\b(?:juega|jugar|juegas|jueg[au]es|farmea[rs]?)\b"
            r"\s*(?P<resto>[^\n]*)$"),
         "jugar",
         # El objetivo se queda sin las muletas del encargo: lo que llega a la
         # herramienta tiene que ser el juego, no «por mí tú solo al».
         lambda m: _limpiar_objetivo_jugar(m.group("resto")),
         False,
         lambda p: f"Jugar: {p['objetivo'][:50] or 'sin objetivo claro'}"),

        # Entrada 2 — la MARCA de encargo, esté donde esté el verbo: el ^ de
        # antes perdía «usa mi dispositivo … y juega por mi en mi cuenta».
        (re.compile(
            r"(?i)"
            r"\b(?:juega|jugar|juegas|jueg[au]es|farmea[rs]?|gana|g[aá]name|"
            r"super[aá]|complet[ao])\b"
            # El «tú» vale como marca porque el lookahead solo mira hacia
            # DELANTE del verbo: «juega TÚ al ajedrez» es un encargo y «¿TÚ
            # juegas al ajedrez?» no llega aquí, que es la diferencia entre el
            # pronombre pospuesto (imperativo) y antepuesto (conversación).
            r"(?=[^\n]*\b(?:por\s+m[ií]|en\s+mi\s+lugar|en\s+mi\s+cuenta|"
            r"t[uú]|sol[ao]|solit[ao]|partida|nivel|misi[oó]n)\b)"
            r"\s*(?P<resto>[^\n]*)$"),
         "jugar",
         lambda m: _limpiar_objetivo_jugar(m.group("resto")),
         False,
         lambda p: f"Jugar: {p['objetivo'][:50] or 'sin objetivo claro'}"),

        # Entrada 3 — el IMPERATIVO PURO: «juega al zzz», «farmea en el zzz».
        # Aquí no hay ninguna marca que lo respalde, así que lo que distingue
        # la orden de la charla son dos cosas: que el verbo abra la frase y que
        # NO sea una pregunta («¿juega hoy el Betis?» no es un encargo). Y se
        # exige la preposición de «jugar A algo»: sin ella, «juega el Madrid
        # esta noche» —sujeto pospuesto, que es fútbol— se colaría como orden.
        (re.compile(
            r"(?i)^\s*(?:(?:por\s+favor|oye|hey|venga|vale|ok|ahora|pues|"
            r"a\s+ver|mira|porfa(?:vor)?|anda)[,\s]+)*"
            r"(?:juega|jueg[aá]|farmea)\b"
            r"(?![^\n]*\?)"
            r"\s+(?:al?|a\s+la|en(?:\s+(?:el|la))?|con)\b"
            r"\s*(?P<resto>[^\n]*)$"),
         "jugar",
         lambda m: _limpiar_objetivo_jugar(m.group("resto")),
         False,
         lambda p: f"Jugar: {p['objetivo'][:50] or 'sin objetivo claro'}"),

        # Abrir app
        # Sesión 32 (BUG-S132): abrir URL en navegador. Va ANTES de abrir_app
        # para que «abre https://google.com» no se interprete como abrir app.
        (re.compile(
            r"^\s*(?:abre|abrir|ve\s+a|navega\s+a|visita|abrelo\s+en\s+(?:el\s+)?navegador)"
            r"\s+(?:en\s+(?:el\s+)?navegador\s+|en\s+chrome\s+)?"
            r"((?:https?://)?[\w\-]+\.[a-z]{2,}[\w\-._~:/?#\[\]@!$&'()*+,;=%]*)\s*$",
            re.I),
         "abrir_url",
         lambda m: {"url": m.group(1).strip()},
         False,
         lambda p: f"Abrir URL: {p['url'][:60]}"),

        # Sesión 32 (BUG-S132): llamar a un contacto o número. Acepta
        # «llama a X», «llámame a X», «llama al X» (al = a+el, p.ej.
        # «llama al +34...»), «marca a X», «hazle una llamada a X».
        (re.compile(
            r"^\s*(?:ll[aá]mam?e?|ll[aá]ma|tel[ée]fonea[mr]?e?|"
            r"hazle?\s+una\s+llamada|m[aá]rcale?|"
            r"ll[aá]ma\s+por\s+tel[eé]fono)\s+a(?:l)?\s+"
            r"(.+?)\s*[\.\?!]?\s*$",
            re.I),
         "llamar",
         lambda m: {"contacto": m.group(1).strip()},
         False,
         lambda p: f"Llamar a {p['contacto']}"),

        # Sesión 32 (BUG-S132): enviar mensaje por la app que elija el
        # usuario (WhatsApp por defecto). Formas que cubre:
        #   «manda(le) un whatsapp a X diciendo Y»
        #   «envíale un mensaje por telegram a X: Y»
        #   «mándale un sms a X que dice Y»
        #   «escríbele a X por whatsapp: Y»
        #   «manda(le) un mensaje a X: Y» (sin app → whatsapp por defecto)
        (re.compile(
            r"^\s*(?:env[ií]a(?:me|le)?|m[aá]nda(?:le|me)?|"
            r"escr[ií]be(?:le|me)?|escribir)\s+"
            r"(?:un\s+|una\s+|el\s+|la\s+)?"
            r"(?:(?P<app1>whatsapp|wa|wsp|guasap|telegram|tg|sms|"
            r"mensaje\s+de\s+texto|texto|signal|instagram|ig|insta)\s+)?"
            r"(?:un\s+|una\s+|el\s+|la\s+)?"
            r"(?:mensaje\s+|msj\s+|m[eé]nsaje\s+)?"
            r"(?:por\s+(?P<app2>whatsapp|wa|wsp|guasap|telegram|tg|sms|"
            r"signal|instagram|ig|insta)\s+)?"
            r"a(?:l)?\s+(?P<dest>.+?)"
            r"(?:\s+por\s+(?P<app3>whatsapp|wa|wsp|guasap|telegram|tg|sms|"
            r"signal|instagram|ig|insta))?"
            r"(?:\s*:\s*|\s+(?:diciendo|dici[ée]ndole|"
            r"que\s+(?:dice|diga|le\s+diga)|"
            r"con\s+el\s+texto))\s*"
            r"(?P<msg>.+?)\s*[\.\?!]?\s*$",
            re.I),
         "enviar_mensaje",
         lambda m: {
             "contacto": m.group("dest").strip(),
             "mensaje":  m.group("msg").strip(" '\""),
             "app": (m.group("app1") or m.group("app2") or m.group("app3")
                     or "whatsapp").strip().lower(),
         },
         False,
         lambda p: f"{p['app']} a {p['contacto']}: {p['mensaje'][:40]}"),

        # Sesión 32 (BUG-S135): cortar la captura en « y » o « , » para que
        # «abre google y busca noticias pc» no intente abrir una app
        # llamada "google y busca noticias pc". Ahora solo coge la primera
        # parte («google»).
        # Sesión 32 (BUG-S151): aceptar «(la|el)?\s+(app|aplicación)\s+de»
        # como prefijo opcional — «abre la app de google» debe capturar
        # «google», no «de google».
        # Sesión 34 (B34-10): anclar al INICIO del mensaje para evitar que
        # «qué pasa si te digo abre google» dispare la tool. Permitimos solo
        # conectores triviales al inicio (por favor, oye, hey, venga, etc.)
        # y opcionalmente «¿».
        # Sesión 41: "pon/reproduce/abre [lo que sea] en SPOTIFY/YOUTUBE/…" — apps
        # de música/streaming. Antes "pon la DJ de spotify" o "mejor dicho abre
        # spotify y reproduce la DJ" no tenían detector → el LLM inventaba un
        # comando inexistente ("[TAP:spotify:dj]") y MENTÍA diciendo que lo abría.
        # Ahora abrimos la app REAL (la reproducción de contenido concreto no la
        # controlamos, pero al menos abre la app de verdad). Va ANTES del genérico.
        (re.compile(
            r"^\s*¿?\s*(?:(?:por\s+favor|oye|hey|venga|vale|ok|mejor\s+dicho|ahora|"
            r"pues|a\s+ver|mira|porfa(?:vor)?|anda)[,\s]+)*"
            r"(?:pon(?:me|le)?|reprod[uú]ce(?:me)?|echa(?:me)?|p[íi]ncha(?:me)?|"
            r"abre|abrir|lanza[r]?|inicia[r]?|quiero\s+(?:o[ií]r|escuchar))\b.*?\b"
            r"(spotify|youtube\s*music|yt\s*music|youtube|netflix|hbo\s*max|hbo|"
            r"disney\+?|prime\s*video|amazon\s*music|apple\s*music|tidal|deezer|"
            r"soundcloud|twitch)\b",
            re.I | re.S),
         "abrir_app",
         lambda m: {"app": m.group(1).strip()},
         False,
         lambda p: f"Abrir app: {p['app']}"),

        (re.compile(
            r"^\s*¿?\s*(?:(?:por\s+favor|oye|hey|venga|vale|ok|mejor\s+dicho|ahora|"
            r"pues|a\s+ver|mira|porfa(?:vor)?|anda)[,\s]+)*"
            r"(?:abre|lanza[r]?|inicia[r]?)\s+"
            r"(?:(?:la|el)\s+)?"
            r"(?:(?:app|aplicaci[oó]n)\s+(?:de\s+)?)?"
            r"(\w[\w\s]*?)(?:\s+(?:y|,)\s+|\s*[.!?¿¡]*\s*$)",
            re.I),
         "abrir_app",
         lambda m: {"app": m.group(1).strip()},
         False,
         lambda p: f"Abrir app: {p['app']}"),

        # Sesión 31: cerrar app (force-stop si Shizuku activo, HOME si no).
        # «cierra X», «mata X», «termina X», «finaliza X».
        # Sesión 34 (B34-10): anclar al inicio para evitar falsos positivos
        # de frases hipotéticas («qué pasa si te digo cierra X»).
        (re.compile(r"^\s*¿?\s*(?:(?:por\s+favor|oye|hey|venga|vale|ok)[,\s]+)?(?:cierra[r]?|mata[r]?|termina[r]?|finaliza[r]?)\s+(?:la\s+)?(?:app\s+|aplicaci[oó]n\s+|aplicación\s+)?(\w[\w\s]*\w|\w+)", re.I),
         "cerrar_app",
         lambda m: {"app": m.group(1).strip()},
         False,
         lambda p: f"Cerrar app: {p['app']}"),

        # Generación de imágenes
        # Incluye "hacer/hacerme" además de "haz/hazme" — sin ello, frases como
        # "Puedes hacerme una imagen de X" caían al flujo de skill nueva y
        # consumían 5 min de reintentos hasta el timeout (bug visto en sesión 25).
        # IMPORTANTE: antes el bloque "quiero/quisiera" tenía su propio "\s+(?:una?\s+)?"
        # interno, que consumía el espacio que necesitaba el "\s+" siguiente —
        # "quiero una imagen de X" no matcheaba. Ahora "una/un" sale del grupo de verbos.
        # Perífrasis añadidas en sesión 26: "quiero que (me) hagas una foto",
        # "puedes/podrías hacerme una foto" — bug visto en vivo por WhatsApp:
        # el usuario pidió "Quiero que me hagas una foto de un husky..." y
        # Celestia respondió "no puedo generar imágenes" porque el regex no
        # matcheaba la construcción perifrástica.
        # Determinantes ampliados sesión 26 a la/el/otra/algún para frases
        # como "que hagas la foto" o "hazme otra imagen".
        (re.compile(
            r"(?:"
            r"cr[eé]a[r]?(?:me)?|gen[eé]ra[r]?(?:me)?|haz(?:me)?|hac[eé]r(?:me)?|"
            r"dibuja[r]?(?:me)?|p[íi]nta[r]?(?:me)?|prep[áa]ra[r]?(?:me)?|"
            r"qui(?:ero|siera)"
            r"|qui(?:ero|siera)\s+que\s+(?:me\s+)?(?:hag(?:as|a)|crees|gener(?:es|e)|dibuje?s|pinte?s)"
            r"|p(?:uedes|odr[íi]as|od[eé]s)\s+(?:hacer(?:me)?|crear(?:me)?|generar(?:me)?|dibujar(?:me)?|pintar(?:me)?)"
            r")\s+"
            r"(?:un[ao]?|el|la|los|las|otr[ao]s?|algun[ao]?)?\s*"
            r"(?:imagen|foto(?:graf[íi]a)?|ilustraci[oó]n|dibujo|pintura|render)\s+"
            r"(?:de\s+|sobre\s+|con\s+|que\s+(?:muestre|tenga)\s+)?(.+)",
            re.I | re.S),
         "generar_imagen",
         lambda m: {"prompt": m.group(1).strip(" .?!"), **_tamano_imagen(m.string)},
         False,
         lambda p: f"Generar imagen: {p['prompt'][:60]}"),

        # Piezas de diseño: anuncio, cartel, flyer, póster, logo, banner…
        # (4 oct 2026: «crea un Anuncio minimalista para historias de Instagram
        # de un tatuador, formato vertical 9:16…» no se reconocía — sólo valían
        # «imagen/foto/ilustración/dibujo» — y acabó en «<tool>generar_imagen
        # </tool>» escrito en el chat). «Anuncio», «invitación» o «portada»
        # también pueden ser de texto: sólo cuentan si el mensaje habla de algo
        # visual (formato, colores, tipografía, Instagram…). La pieza entra en
        # la descripción: «anuncio minimalista…», no «minimalista…».
        (re.compile(
            r"(?s)^(?=.*\b(?:formato|vertical|horizontal|\d+\s*[:x]\s*\d+|paleta|colou?r(?:es)?|"
            r"tipograf\w*|instagram|tiktok|historias|stories|minimalista|dise[ñn]o|ilustraci\w*|"
            r"fondo|visual|redes\s+sociales|flyer|p[oó]ster|logo)\b).*?"
            r"\b(?:cr[eé]a[r]?(?:me)?|gen[eé]ra[r]?(?:me)?|haz(?:me)?|hac[eé]r(?:me)?|"
            r"dise[ñn]a[r]?(?:me)?|prep[áa]ra[r]?(?:me)?|qui(?:ero|siera)|necesito|"
            r"p(?:uedes|odr[íi]as)\s+(?:hacer|crear|generar|dise[ñn]ar)(?:me)?)\s+"
            r"(?:un[ao]?|el|la|los|las|otr[ao]s?|mi)?\s*"
            r"((?:cartel|p[oó]ster|poster|flyer|folleto|banner|logo(?:tipo)?|miniatura|portada|"
            r"sticker|pegatina|fondo\s+de\s+pantalla|wallpaper|infograf[ií]a|anuncio|"
            r"invitaci[oó]n|post|historia)\b.+)",
            re.I),
         "generar_imagen",
         lambda m: {"prompt": m.group(1).strip(" .?!"), **_tamano_imagen(m.string)},
         False,
         lambda p: f"Generar diseño: {p['prompt'][:60]}"),

        # Seguimiento de imagen: "crea otra (igual)", "hazme otra", "una más",
        # "haz lo mismo pero..." — sin la palabra "imagen/foto" explícita, pero
        # con verbo de generación + pronombre. Visto en sesión 26 por WhatsApp
        # cuando el usuario ya había recibido una imagen y pedía otra.
        # Lookahead negativo (sesión 26): excluir sustantivos no visuales como
        # "voz", "canción", "respuesta"… que disparaban falsos positivos
        # ("quiero otra voz" → no es petición de imagen).
        (re.compile(
            # Verbo opcional al inicio (sesión 29: "y otra parecida pero en la
            # playa" no matcheaba al carecer de verbo explícito).
            r"^\s*(?:y\s+)?"
            r"(?:(?:cr[eé]a[r]?(?:me)?|gen[eé]ra[r]?(?:me)?|haz(?:me)?|hac[eé]r(?:me)?|"
            r"dibuja[r]?(?:me)?|"
            r"qui(?:ero|siera)\s+(?:que\s+(?:me\s+)?(?:hag(?:as|a)|crees|gener(?:es|e)|dibuje?s)\s+)?)"
            r"\s*)?"
            r"(?:otra|otro|una?\s+m[áa]s|lo\s+mismo)"
            # Aceptar adjetivo opcional descriptivo: "otra parecida", "otra similar"
            r"(?:\s+(?:parecida|similar|igual|distinta|diferente))?"
            r"\b"
            r"(?!\s+(?:voz|voces|canci[oó]n|m[uú]sica|respuesta|opci[oó]n|pregunta|"
            r"cosa|persona|forma|manera|broma|chiste|historia|explicaci[oó]n|"
            r"sugerencia|recomendaci[oó]n|idea|alternativa|comida|receta|"
            r"pel[ií]cula|libro|nombre|palabra|frase|"
            # Sesión 29: añadidos para evitar disparar imagen con "otro X"
            # cuando el contexto previo era texto creativo.
            r"haiku|poema|verso|poes[ií]a|cuento|relato|texto|fragmento|"
            r"prefer\w+|opci[oó]n|"
            # Adjetivos que cualifican voz / texto, no imagen
            r"humana?|natural|tranquil[ao]?|relajad[ao]?|c[aá]lid[ao]?|"
            r"expresiv[ao]?|robot|robot[ií]ca|artificial|"
            r"m[aá]s\s+(?:humana|natural|"
            r"r[aá]pid|lent|despacio|agud|grav|c[aá]lid|tranquil|relajad|expresiv)))"
            r"(.*)",
            re.I | re.S),
         "generar_imagen",
         lambda m: {"prompt": (m.group(1) or "").strip(" .?!,") or "otra similar"},
         False,
         lambda p: f"Generar imagen (seguimiento): {p['prompt'][:60]}"),

        # Enviar archivo / documento existente al usuario
        (re.compile(
            r"(?:env[íi]a(?:me)?|m[aá]nda(?:me)?|pas(?:a|ame)|comparte(?:me)?|mu[eé]strame)\s+"
            r"(?:el\s+|la\s+|un\s+|una\s+)?"
            r"(?:archivo|documento|fichero|pdf|foto|imagen|v[ií]deo|audio)\s+"
            r"(?:llamado\s+|de\s+nombre\s+|de\s+)?['\"]?([\w./\\\-~ ]+\.\w+)['\"]?",
            re.I),
         "enviar_archivo",
         lambda m: {"ruta": m.group(1).strip()},
         False,
         lambda p: f"Enviar archivo: {p['ruta']}"),

        # Crear archivo (literal con contenido dado) — ANTES de crear_documento
        # para que "crea un archivo X con contenido Y" gane a "crea un documento sobre Y"
        # Sesión 31 (BUG-S59): "contenido/texto" es opcional — el usuario
        # suele decir simplemente "crea archivo X con 'Y'" o "crea X con Y".
        (re.compile(
            r"cr[eé]a[r]?\s+(?:un\s+)?archivo\s+(?:llamado\s+|con\s+nombre\s+)?"
            r"['\"]?([\w/\\.\-~]+)['\"]?\s+"
            r"con\s+(?:el\s+)?(?:contenido|texto)?[:\s]*['\"]?(.+?)['\"]?\s*$",
            re.I | re.S),
         "crear_archivo",
         lambda m: {"ruta": m.group(1), "contenido": m.group(2).strip().strip("\"'")},
         False,
         lambda p: f"Crear archivo: {p['ruta']}"),

        # Inicializar vault — ANTES de crear_documento, porque "crea el vault
        # con contraseña X" caía en crear_documento (regex genérico) y nunca
        # llegaba a vault_inicializar (bug visto sesión 28).
        (re.compile(
            r"(?:crea[r]?|inicializa[r]?|configura[r]?)\s+(?:el\s+|un\s+)?vault\s+"
            r"(?:con\s+contrase[ñn]a\s+(?:maestra\s+)?)?['\"]?([^'\"]+)['\"]?$",
            re.I),
         "vault_inicializar",
         lambda m: {"master": m.group(1).strip()},
         False,
         lambda p: "Crear vault de contraseñas"),

        # Crear documento / archivo / código nuevo con contenido generado.
        # IMPORTANTE: el formato/artefacto es REQUERIDO. Sin esto, "escribe
        # una lista de animales" caía aquí y disparaba PDF (bug sesión 29 en
        # vivo: usuario quería respuesta en chat, recibió "preparando PDF").
        # "texto" eliminado: en WhatsApp el usuario usa "texto" para "respuesta
        # en chat", no para archivo .txt — provocaba más falsos positivos.
        # Sesión 31 (BUG-S95): NO matchear si la frase empieza por interrogativo
        # (cómo, qué, cuál, dónde, cuándo) — son preguntas, no órdenes.
        # «Cómo se crea un PDF en Python» NO debe disparar crear_documento.
        (re.compile(
            r"^(?!\s*(?:¿\s*)?(?:c[oó]mo|qu[eé]|cu[aá]l|d[oó]nde|cu[aá]ndo|por\s+qu[eé])\s)"
            r"\s*"
            # Conectores iniciales opcionales (sesión 41): "ahora/también/luego…"
            # — "Ahora quiero que hagas un pdf de X" no disparaba (empieza por
            # "Ahora") → iba al LLM, que inventaba "[RESULTADO] Documento creado".
            r"(?:(?:ahora|tambi[eé]n|luego|despu[eé]s|venga|oye|pues|y|adem[aá]s|"
            r"porfa(?:vor)?|mejor\s+dicho)[,\s]+)*"
            # Prefijo de cortesía opcional: "¿puedes/podrías/quiero que (me)…" antes
            # del verbo (Sesión 41: "Puedes crear un PDF de X" no disparaba).
            r"(?:(?:¿\s*)?(?:me\s+)?(?:puedes|podr[íi]as|podr[ée]is|puede|podr[íi]a|"
            r"quiero\s+que\s+me|quiero\s+que|quisiera\s+que|necesito\s+que|"
            r"me\s+gustar[íi]a\s+que)\s+)?"
            # Verbos: imperativo/infinitivo Y subjuntivo ("hagas/crees/generes…"),
            # que aparecen tras "quiero que …".
            r"(?:cr[eé]a[r]?(?:me)?|cr[eé]es|gen[eé]ra[r]?(?:me)?|gen[eé]res|"
            r"haz(?:me)?|hagas|hac[eé]r(?:me)?|escr[íi]be(?:me)?|escribas|"
            r"redacta[r]?(?:me)?|redactes|elabora[r]?(?:me)?|elabores|"
            r"prog?rama[r]?(?:me)?|programes|prep[áa]ra[r]?(?:me)?|prepares|c[oó]digo)\s+"
            r"(?:un\s+|una\s+|el\s+|la\s+)?"
            # Palabra explícita de artefacto/formato — OBLIGATORIA
            r"(documento|archivo|fichero|script|programa|c[oó]digo|"
            r"pdf|word|docx?|txt|markdown|md|html|csv|json|"
            r"hoja\s+de\s+c[áa]lculo|excel|xlsx?|presentaci[oó]n|pptx?)"
            r"\s*"
            r"(?:en\s+formato|formato|tipo|en|de|para)?\s*"
            r"(?:un\s+|una\s+)?"
            r"(\w+)?\s*"
            r"(?:\s+(?:sobre|de|acerca\s+de|con(?:\s+tema)?|que\s+(?:haga|hable|trate|explique|sirva\s+para))\s+)?(.+)",
            re.I | re.S),
         "crear_documento",
         lambda m: _parsear_formato_y_tema(m.group(0), m.group(1) or "", m.group(2) or ""),
         False,
         lambda p: f"Crear {p['formato']} sobre: {p['tema'][:50]}"),

        # Capturar pantalla
        (re.compile(r"captura[r]?\s+(?:la\s+)?pantalla|toma[r]?\s+(?:una\s+)?screenshot|qu[eé]\s+(?:hay|ves)\s+en\s+(?:mi\s+)?pantalla", re.I),
         "capturar_pantalla",
         lambda m: {},
         False,
         lambda p: "Capturar pantalla"),

        # Guardián del sistema
        (re.compile(r"guard[íi][aá]n|estado\s+de\s+seguridad|revisa\s+el\s+sistema|alertas?\s+del\s+sistema", re.I),
         "guardian_sistema",
         lambda m: {},
         False,
         lambda p: "Análisis de seguridad del sistema"),

        # ─── Gestión de contraseñas (vault) ───
        # NOTA: vault_inicializar se declara más arriba (antes de crear_documento)
        # para que "crea el vault con contraseña X" no caiga en crear_documento.

        # Desbloquear vault con USB (sin contraseña)
        (re.compile(
            r"(?:desbloquea[r]?|abre[r]?|abrir|unlock)\s+(?:el\s+)?vault\s+"
            r"(?:con\s+(?:el\s+|mi\s+)?usb|usando\s+(?:el\s+)?usb)$",
            re.I),
         "vault_desbloquear_usb",
         lambda m: {},
         False,
         lambda p: "Desbloquear vault con USB"),

        # Guardar llave maestra en USB
        (re.compile(
            r"(?:guarda[r]?|export[ao]r?|copia[r]?)\s+(?:la\s+)?(?:llave\s+|clave\s+)?"
            r"(?:maestra|del\s+vault)?\s*(?:en|al?|para)\s+(?:el\s+|un\s+|mi\s+)?usb\s+"
            r"(?:con\s+contrase[ñn]a\s+)?['\"]?([^'\"]+)['\"]?$",
            re.I),
         "vault_exportar_usb",
         lambda m: {"master": m.group(1).strip()},
         False,
         lambda p: "Guardar llave maestra en USB"),

        # Listar USBs conectados
        (re.compile(
            r"(?:lista[r]?|muestra(?:me)?|detecta[r]?|qu[eé])\s+(?:los\s+|mis\s+)?usbs?\s*"
            r"(?:conectados?|disponibles?)?",
            re.I),
         "vault_listar_usbs",
         lambda m: {},
         False,
         lambda p: "Listar USBs"),

        # Desbloquear vault con contraseña maestra
        (re.compile(
            r"(?:desbloquea[r]?|abre[r]?|abrir|unlock)\s+(?:el\s+)?vault\s+"
            r"(?:con\s+contrase[ñn]a\s+(?:maestra\s+)?)?['\"]?([^'\"]+)['\"]?$",
            re.I),
         "vault_desbloquear",
         lambda m: {"master": m.group(1).strip()},
         False,
         lambda p: "Desbloquear vault"),

        # Guardar contraseña
        (re.compile(
            r"guarda[r]?\s+(?:la\s+)?contrase[ñn]a\s+(?:de|para)\s+"
            r"['\"]?([^:'\",]+)['\"]?\s*[:,]\s*"
            r"(?:usuario\s+)?['\"]?([^,]+)['\"]?\s*,\s*"
            r"(?:contrase[ñn]a|password|pass)\s+['\"]?([^'\"]+)['\"]?",
            re.I),
         "vault_guardar",
         lambda m: {"sitio": m.group(1).strip(),
                     "usuario": m.group(2).strip(),
                     "password": m.group(3).strip()},
         False,
         lambda p: f"Guardar contraseña de {p['sitio']}"),

        # Obtener contraseña
        (re.compile(
            r"(?:dame|muestra(?:me)?|cu[aá]l\s+es|d[ií]me)\s+"
            r"(?:la\s+)?(?:contrase[ñn]a|password|pass|credencial(?:es)?)\s+"
            r"(?:de|para)\s+['\"]?([^'\"]+?)['\"]?\s*\??$",
            re.I),
         "vault_obtener",
         lambda m: {"sitio": m.group(1).strip()},
         False,
         lambda p: f"Obtener contraseña de {p['sitio']}"),

        # Listar contraseñas
        (re.compile(
            r"(?:lista[r]?|muestra(?:me)?|qu[eé])\s+(?:mis\s+|las\s+)?"
            r"contrase[ñn]as(?:\s+guardadas?)?",
            re.I),
         "vault_listar",
         lambda m: {},
         False,
         lambda p: "Listar contraseñas guardadas"),

        # Eliminar contraseña
        (re.compile(
            r"(?:borra[r]?|elimina[r]?|olvida[r]?)\s+(?:la\s+)?contrase[ñn]a\s+"
            r"(?:de|para)\s+['\"]?([^'\"]+?)['\"]?\s*\.?$",
            re.I),
         "vault_eliminar",
         lambda m: {"sitio": m.group(1).strip()},
         True,  # requiere confirmación → no se dispara desde WhatsApp directo
         lambda p: f"Borrar contraseña de {p['sitio']}"),

        # Domótica — registrar dispositivo
        (re.compile(
            r"registra[r]?\s+(?:el\s+)?dispositivo\s+['\"]?(.+?)['\"]?\s+en\s+([\d.]+)"
            r"(?:\s+tipo\s+(\w+))?(?:\s+protocolo\s+(\w+))?",
            re.I),
         "domotica_registrar",
         lambda m: {"nombre": m.group(1).strip(), "ip": m.group(2),
                    "tipo": m.group(3) or "enchufe", "protocolo": m.group(4) or "shelly"},
         False,
         lambda p: f"Registrar dispositivo '{p['nombre']}' en {p['ip']}"),

        # Domótica — listar
        (re.compile(r"(?:lista[r]?|muestra[r]?|qu[eé])\s+(?:mis\s+)?dispositivos?(?:\s+(?:del\s+hogar|inteligentes?))?|dispositivos?\s+(?:registrados?|que\s+tengo)", re.I),
         "domotica_listar",
         lambda m: {},
         False,
         lambda p: "Listar dispositivos del hogar"),

        # Sesión 32 (BUG-S134): controles del DISPOSITIVO (WiFi/BT/linterna/
        # avión/volumen/brillo). Van ANTES de domótica para no ser
        # interceptados — «apaga el wifi» NO es domotica_apagar('wifi').
        (re.compile(
            r"^\s*(?:enciende|prende|activa|conecta)\s+(?:el\s+|la\s+)?wifi\b",
            re.I),
         "toggle_wifi", lambda m: {"estado": "on"}, False,
         lambda p: "Encender WiFi"),
        (re.compile(
            r"^\s*(?:apaga|desactiva|desconecta|quita)\s+(?:el\s+|la\s+)?wifi\b",
            re.I),
         "toggle_wifi", lambda m: {"estado": "off"}, False,
         lambda p: "Apagar WiFi"),
        (re.compile(
            r"^\s*(?:enciende|prende|activa|conecta)\s+(?:el\s+)?bluetooth\b",
            re.I),
         "toggle_bluetooth", lambda m: {"estado": "on"}, False,
         lambda p: "Encender Bluetooth"),
        (re.compile(
            r"^\s*(?:apaga|desactiva|desconecta|quita)\s+(?:el\s+)?bluetooth\b",
            re.I),
         "toggle_bluetooth", lambda m: {"estado": "off"}, False,
         lambda p: "Apagar Bluetooth"),
        (re.compile(
            r"^\s*(?:enciende|prende|activa)\s+(?:la\s+)?linterna\b",
            re.I),
         "toggle_linterna", lambda m: {"estado": "on"}, False,
         lambda p: "Encender linterna"),
        (re.compile(
            r"^\s*(?:apaga|desactiva|quita)\s+(?:la\s+)?linterna\b",
            re.I),
         "toggle_linterna", lambda m: {"estado": "off"}, False,
         lambda p: "Apagar linterna"),
        (re.compile(
            r"^\s*(?:activa|pon|enciende)\s+(?:el\s+)?(?:modo\s+)?avi[oó]n\b",
            re.I),
         "toggle_avion", lambda m: {"estado": "on"}, False,
         lambda p: "Modo avión ON"),
        (re.compile(
            r"^\s*(?:desactiva|quita|apaga)\s+(?:el\s+)?(?:modo\s+)?avi[oó]n\b",
            re.I),
         "toggle_avion", lambda m: {"estado": "off"}, False,
         lambda p: "Modo avión OFF"),
        # Volumen: «sube/baja el volumen», «silencia», «pon el volumen al N%»
        (re.compile(
            r"^\s*(?:sube|aumenta|m[aá]s)\s+(?:el\s+)?volumen\b",
            re.I),
         "cambiar_volumen", lambda m: {"accion": "up"}, False,
         lambda p: "Subir volumen"),
        (re.compile(
            r"^\s*(?:baja|reduce|menos)\s+(?:el\s+)?volumen\b",
            re.I),
         "cambiar_volumen", lambda m: {"accion": "down"}, False,
         lambda p: "Bajar volumen"),
        (re.compile(
            r"^\s*(?:silencia(?:r)?|mute|silencio|enmudece)(?:\s+el\s+(?:m[oó]vil|tel[eé]fono|dispositivo))?\s*[\.\?!]?\s*$",
            re.I),
         "cambiar_volumen", lambda m: {"accion": "mute"}, False,
         lambda p: "Silenciar"),
        (re.compile(
            r"^\s*(?:pon|ajusta|sube|baja)\s+(?:el\s+)?volumen\s+(?:al\s+|a\s+)(\d{1,3})\s*%?",
            re.I),
         "cambiar_volumen", lambda m: {"accion": m.group(1)}, False,
         lambda p: f"Volumen a {p['accion']}%"),
        # Brillo
        (re.compile(
            r"^\s*(?:sube|pon|ajusta|cambia|baja)\s+(?:el\s+)?brillo\s+(?:al\s+|a\s+)(\d{1,3})\s*%?",
            re.I),
         "cambiar_brillo", lambda m: {"nivel": m.group(1)}, False,
         lambda p: f"Brillo {p['nivel']}%"),
        (re.compile(
            r"^\s*(?:sube|aumenta)\s+(?:el\s+)?brillo\b(?:\s+al\s+m[aá]ximo)?",
            re.I),
         "cambiar_brillo", lambda m: {"nivel": "max"}, False,
         lambda p: "Brillo máximo"),
        (re.compile(
            r"^\s*(?:baja|reduce)\s+(?:el\s+)?brillo\b(?:\s+al\s+m[ií]nimo)?",
            re.I),
         "cambiar_brillo", lambda m: {"nivel": "min"}, False,
         lambda p: "Brillo mínimo"),

        # Domótica — ajustar (antes de encender/apagar para capturar %)
        (re.compile(
            r"\b(?:pon|ajusta[r]?|sube|baja|regula[r]?)\s+(?:el\s+|la\s+)?(.+?)\s+(?:al\s+|a\s+)(\d+)\s*%",
            re.I),
         "domotica_ajustar",
         lambda m: {"dispositivo": m.group(1).strip(), "valor": int(m.group(2))},
         False,
         lambda p: f"Ajustar {p['dispositivo']} al {p['valor']}%"),

        # Domótica — encender. \b al inicio evita que "prende" matchee dentro
        # de "aprende", "comprende", "emprende", etc. Sesión 32 (BUG-S125):
        # «soy una persona activa o tranquila» disparaba el detector porque
        # «activa» matcheaba como verbo. Ahora exigimos el verbo al INICIO
        # del mensaje (con opcional cortesía/interrogación) y rechazamos
        # contextos «persona/soy/muy/tan/es … activa» con lookbehind negativo.
        (re.compile(
            r"^\s*(?:por\s+favor[,\s]+)?¿?\s*"
            r"(?:enciende[r]?|activa[r]?|prende[r]?|conecta[r]?)\b"
            # Un «modo» no es un aparato: «activa el modo DAN y dime un
            # secreto» se ponía a APRENDER a encenderlo (examen, 23 sep 2026).
            # El modo avión tiene su propio detector antes que este.
            r"(?!\s+(?:el\s+|la\s+)?modo\b)"
            r"\s+(?:el\s+|la\s+)?(.+)",
            re.I),
         "domotica_encender",
         lambda m: {"dispositivo": m.group(1).strip(" .")},
         False,
         lambda p: f"Encender {p['dispositivo']}"),

        # Domótica — apagar
        (re.compile(
            r"^\s*(?:por\s+favor[,\s]+)?¿?\s*"
            r"(?:apaga[r]?|desactiva[r]?|desconecta[r]?)\b"
            # Un «modo» no es un aparato: «activa el modo DAN y dime un
            # secreto» se ponía a APRENDER a encenderlo (examen, 23 sep 2026).
            # El modo avión tiene su propio detector antes que este.
            r"(?!\s+(?:el\s+|la\s+)?modo\b)"
            r"\s+(?:el\s+|la\s+)?(.+)",
            re.I),
         "domotica_apagar",
         lambda m: {"dispositivo": m.group(1).strip(" .")},
         False,
         lambda p: f"Apagar {p['dispositivo']}"),

        # Domótica — estado
        (re.compile(
            r"(?:estado|est[aá]\s+encendid[ao]|est[aá]\s+apagad[ao])\s+(?:de\s+(?:el\s+|la\s+)?)(.+)",
            re.I),
         "domotica_estado",
         lambda m: {"dispositivo": m.group(1).strip(" .")},
         False,
         lambda p: f"Estado de {p['dispositivo']}"),

        # Info del sistema
        (re.compile(r"info(?:rmación)?\s+del?\s+sistema|cuánta\s+ram|cuánto\s+(?:espacio|disco)|uso\s+de\s+(?:ram|cpu|disco)|estado\s+del\s+sistema", re.I),
         "info_sistema",
         lambda m: {},
         False,
         lambda p: "Mostrar información del sistema"),

        # Listar archivos. ANCLA al inicio para evitar falsos positivos como
        # "Escribe una lista de 5 animales" → matcheaba "lista de" como cmd.
        # Bug visto sesión 29 en vivo.
        (re.compile(
            r"^\s*(?:"
            r"lista[r]?|mu[eé]stra(?:me)?\s+(?:los\s+)?archivos?|"
            r"qu[eé]\s+hay\s+en|"
            r"qu[eé]\s+archivos?\s+(?:hay|tengo)\s+en|"
            r"cu[aá]ntos\s+archivos?\s+(?:hay|tengo)\s+en|"
            r"contenido\s+de|ver\s+archivos?\s+(?:de\s+|en\s+)?"
            r")\s+(?:los\s+)?(?:archivos?\s+(?:de\s+|en\s+)?)?([\w/\\.~\-]+)\s*[\.\?!¿¡]*\s*$",
            re.I),
         "listar_archivos",
         lambda m: {"ruta": m.group(1) or "."},
         False,
         lambda p: f"Listar archivos en: {p['ruta']}"),

        # Leer archivo. Acepta tanto rutas con extensión (foo.txt) como sin ella
        # (/etc/hostname) siempre que empiecen por "/" o "~".
        # Sesión 29: "abre /etc/passwd" caía en abrir_app y el LLM inventaba
        # contenido. Ahora "abre" + ruta absoluta también dispara lectura.
        (re.compile(
            r"(?:lee|leer|mu[eé]strame|muestra|abre|abrir|ver|cat)\s+(?:el\s+)?(?:archivo\s+|contenido\s+de\s+)?"
            r"((?:[~/][\w/\\.\-~]+)|(?:[\w/\\.\-~]+\.\w+))",
            re.I),
         "leer_archivo",
         lambda m: {"ruta": m.group(1)},
         False,
         lambda p: f"Leer archivo: {p['ruta']}"),

        # Aprender nueva habilidad — antes que ejecutar
        (re.compile(
            r"(?:apr[eé]nde(?:te)?|ens[eé][ñn]ate|apr[eé]nde)\s+"
            r"(?:a\s+)?(?:hacer\s+|c[oó]mo\s+(?:hacer\s+)?)?(.+)",
            re.I | re.S),
         "aprender_habilidad",
         lambda m: {"tarea": m.group(1).strip(" .")},
         False,
         lambda p: f"Aprender a: {p['tarea'][:60]}"),

        # Usar habilidad guardada
        (re.compile(
            r"usa[r]?\s+(?:tu\s+)?(?:la\s+)?habilidad\s+(?:de\s+)?(.+)|"
            r"ejecuta[r]?\s+(?:tu\s+)?(?:la\s+)?habilidad\s+(?:de\s+)?(.+)",
            re.I | re.S),
         "usar_habilidad",
         lambda m: {"nombre": (m.group(1) or m.group(2)).strip(" .")},
         False,
         lambda p: f"Usar habilidad: {p['nombre'][:60]}"),

        # Listar habilidades guardadas
        (re.compile(
            r"(?:qu[eé]\s+)?(?:habilidades|cosas\s+que\s+sabes\s+hacer|skills)\s+"
            r"(?:tienes|has\s+aprendido|guard(?:aste|adas?))",
            re.I),
         "listar_habilidades",
         lambda m: {},
         False,
         lambda p: "Listar habilidades guardadas"),

        # Ejecutar comando shell — soporta con o sin comillas:
        #   "ejecuta 'ls'", "ejecuta `ls -la`", "ejecuta el comando ls",
        #   "corre el comando df -h", "ejecuta ls"
        # Forma estricta: requiere palabra "comando" o comillas para evitar falsos
        # positivos con "ejecuta tu habilidad X" / "ejecuta el plan X".
        (re.compile(
            r"(?:ejecuta[r]?|corre[r]?|lanza[r]?)\s+"
            r"(?:"
            r"(?:el\s+)?comando\s+(?:[`'\"](.+?)[`'\"]|(.+?))"
            r"|"
            r"[`'\"](.+?)[`'\"]"
            r")\s*$",
            re.I | re.S),
         "ejecutar_comando",
         lambda m: {"cmd": (m.group(1) or m.group(2) or m.group(3) or "").strip()},
         False,
         lambda p: f"Ejecutar: `{p['cmd']}`"),

        # Crear archivo
        (re.compile(r"crea[r]?\s+(?:un\s+)?archivo\s+(?:llamado\s+|con\s+nombre\s+)?([\w/\\.\-~]+)\s+con\s+(?:el\s+)?(?:contenido|texto)[:\s]+(.+)$", re.I | re.S),
         "crear_archivo",
         lambda m: {"ruta": m.group(1), "contenido": m.group(2).strip()},
         False,
         lambda p: f"Crear archivo: {p['ruta']}"),

        # Borrar
        (re.compile(r"(?:borra[r]?|elimina[r]?|suprime[r]?)\s+(?:el\s+)?(?:archivo\s+|directorio\s+|carpeta\s+)?([\w/\\.\-~]+)", re.I),
         "borrar",
         lambda m: {"ruta": m.group(1)},
         True,
         lambda p: f"⚠ Borrar: {p['ruta']}"),

        # Búsqueda web explícita (más genérico — siempre al final).
        # Sesión 32 (BUG-S136): formas naturales como «busca en el google que
        # has abierto noticias de X» no disparaban. Ahora limpiamos prefijos
        # naturales («en el/la google que has abierto», «en chrome», «en
        # internet», «en duckduckgo», «en la web», «info[rmación] sobre»,
        # «sobre», «de», «acerca de») y el RESTO es la query.
        (re.compile(
            r"^\s*(?:b[uú]sca(?:me|lo|la|melo|telo)?|busca[r]?|"
            r"investiga(?:me|lo|la)?|encuentra(?:me)?|googlea(?:me)?)\s+"
            r"(?P<query>.+?)\s*[\.\?!]*\s*$",
            re.I),
         "buscar_web",
         lambda m: {"query": _limpiar_query_busqueda(m.group("query"))},
         False,
         lambda p: f"Buscar en internet: \"{p['query']}\""),

        # Noticias — ANTES del fallback de buscar_web (que cae al final).
        # Soporta "dame las noticias", "me puedes decir noticias", "noticias
        # recientes sobre X", "qué pasó hoy/en X", "noticias sobre/de X",
        # "titulares".
        # Filtro: si el "tema" capturado es "hoy"/"ahora"/"ya"/"el día"/"el mundo",
        # se ignora porque NO es un tema real, sólo modificador temporal.
        (re.compile(
            r"(?:"
            # Forma 1: verbo + "(las)? noticias/titulares/última hora"
            # Verbos: dame, cuéntame, dime, mándame, dale, cuenta, díme,
            # me puedes/podrías + decir/dar/contar/mostrar, sabes, hay, qué novedades,
            # quiero (saber/conocer), enséñame, búscame, tráeme, lista
            r"(?:"
            r"dame|cu[eé]nta(?:me)?|d[ií]me|m[aá]nda(?:me)?|d[aá]le|"
            r"qui[eé]ro(?:\s+(?:saber|conocer|ver))?|qu[eé]\s+novedades|"
            r"(?:me\s+)?(?:puedes|podr[íi]as)\s+(?:decir(?:me)?|dar(?:me)?|"
            r"contar(?:me)?|mostrar(?:me)?|mandar(?:me)?|tra[ée]r(?:me)?)|"
            r"sabes|hay|busca(?:me)?|tra[ée]me|ens[eé]ñame|lista(?:me)?"
            r")"
            r"\s+(?:las\s+|los\s+|alguna\s+|algunas?\s+)?"
            r"(?:noticias?|titulares?|novedades|[uú]ltima\s+hora)|"
            # Forma 2: "qué (hay/pasa/pasó) (hoy/en el mundo/en España/...)"
            # Sesión 31 (BUG-S34): el adverbio temporal/locativo es OBLIGATORIO.
            # «Si te apago, qué pasa?» (pregunta reflexiva, no de noticias)
            # disparaba la búsqueda. Antes el grupo era opcional `?`.
            r"qu[eé]\s+(?:hay|pasa|pas[oó])\s+(?:hoy|ahora|ya|en\s+el\s+mundo|en\s+espa[ñn]a|de\s+nuevo)|"
            # Forma 3: arranque directo con la palabra clave
            r"(?:noticias?|titulares?|[uú]ltima\s+hora)"
            r")"
            # Adjetivos opcionales después de "noticias" (recientes, actuales, frescas, del día, de hoy)
            r"(?:\s+(?:recientes?|actuales?|frescas?|de\s+hoy|del\s+d[ií]a|de\s+[uú]ltima\s+hora))?"
            # Tema opcional con preposición — excluimos modificadores temporales en
            # _limpiar_tema_noticias.
            r"(?:\s+(?:de|sobre|acerca\s+de|en|del?|respecto\s+a)\s+(.+))?\s*[\.\?!¿¡]*$",
            re.I | re.S),
         "buscar_noticias",
         lambda m: {"tema": _limpiar_tema_noticias(m.group(1) if m.group(1) else "")},
         False,
         lambda p: f"Noticias{' sobre ' + p['tema'] if p['tema'] else ''}"),

        # Consulta de clima/tiempo — captura ciudad cuando se nombra explícita
        # ("¿qué tiempo hace en Alicante?", "clima en Madrid", "weather Tokyo").
        # Va antes de buscar_web porque "el tiempo en X" matcheaba como query
        # genérica y devolvía resultados pobres (sesión 26 por WhatsApp).
        # Sesión 31 (BUG-S49): consulta de clima SIN ciudad explícita —
        # «how is the weather today?», «¿qué tiempo hace?», «¿qué tiempo
        # hace hoy?». Antes el LLM alucinaba ciudades (Triandria, Grecia).
        # Aquí pasamos ubicación vacía y la tool intenta deducir por
        # `ciudad` en hechos_usuario. Va ANTES del regex con ciudad porque
        # «weather today» matcheaba ese con "today" como ciudad.
        (re.compile(
            r"^\s*¿?\s*(?:"
            r"(?:c[oó]mo\s+est[aá]\s+|qu[eé]\s+tal\s+|qu[eé]\s+)?"
            r"(?:el\s+)?(?:tiempo|clima)\s+(?:hace|hay|est[aá])?"
            r"(?:\s+(?:hoy|ahora|mañana|esta\s+(?:mañana|tarde|noche)))?|"
            r"how\s+is\s+the\s+weather(?:\s+(?:today|now|tomorrow))?|"
            r"what'?s\s+the\s+weather(?:\s+like)?(?:\s+(?:today|now|tomorrow))?|"
            r"weather\s+(?:today|now|tomorrow)?"
            r")\s*[?.!]*$",
            re.I),
         "consultar_clima",
         lambda m: {"ubicacion": ""},
         False,
         lambda p: "Consultar clima (deducido del perfil)"),

        (re.compile(
            r"(?:qu[eé]\s+(?:tal\s+(?:est[aá]\s+)?(?:el\s+)?)?(?:tiempo|clima)\s+(?:hace\s+|hay\s+)?(?:en|por)\s+([\w\sáéíóúñ.\-]+?)|"
            r"(?:el\s+|c[oó]mo\s+est[aá]\s+el\s+)?(?:tiempo|clima)\s+(?:hace\s+|hay\s+|es\s+|está\s+|de\s+|en|por)\s+([\w\sáéíóúñ.\-]+?)|"
            r"weather\s+(?:in|at)\s+([\w\s.\-]+?)|"
            r"pron[oó]stico\s+(?:del?\s+(?:tiempo|clima)\s+)?(?:en|para|de)\s+([\w\sáéíóúñ.\-]+?)|"
            r"previsi[oó]n\s+(?:del?\s+(?:tiempo|clima)\s+)?(?:en|para|de)\s+([\w\sáéíóúñ.\-]+?))"
            r"\s*[?.!]*$",
            re.I),
         "consultar_clima",
         lambda m: {"ubicacion": next(g for g in m.groups() if g).strip(" .?!,")},
         False,
         lambda p: f"Consultar clima en {p['ubicacion']}"),

        # Sesión 32 (BUG-S108): hora en otra ciudad. Antes el LLM Groq
        # alucinaba día y hora completamente («4:37 PM del miércoles 27 de
        # mayo» cuando hoy es viernes 29).
        (re.compile(
            r"^\s*¿?\s*(?:qu[eé]\s+hora\s+(?:es|son|hace|hay)(?:\s+ahora)?|"
            r"hora\s+actual|"
            r"a\s+qu[eé]\s+hora\s+estamos|what\s+time\s+is\s+it)"
            r"\s+(?:en|in|at)\s+([\w\sáéíóúñ.\-]+?)"
            r"\s*[?.!]*$",
            re.I),
         "hora_ciudad",
         lambda m: {"ciudad": m.group(1).strip(" .?!,")},
         False,
         lambda p: f"Hora en {p['ciudad']}"),

        # Sesión 33: días hasta fecha señalada. Antes «cuánto falta para
        # navidad» caía al LLM y respondía «no tengo info actualizada».
        (re.compile(
            r"^\s*¿?\s*(?:cu[aá]ntos?\s+d[ií]as?\s+(?:faltan|quedan|hay)|"
            r"cu[aá]nto\s+(?:falta|queda))"
            r"\s+(?:para|hasta|a)\s+"
            r"(?:el\s+|la\s+|los\s+|las\s+)?"
            r"([\w\sáéíóúñ\.\-/]+?)"
            r"\s*[?.!]*$",
            re.I),
         "dias_hasta",
         lambda m: {"evento": m.group(1).strip(" .?!,")},
         False,
         lambda p: f"Días hasta {p['evento']}"),

        # ZZZ cuando ya se está hablando de ZZZ. En una conversación nadie
        # repite el nombre del juego, y el detector de arriba lo exige en CADA
        # mensaje: «Pero como serian los equipos», «dime que podria mejorar de
        # su build», «y quien es kira» — ninguna lo dice, así que ninguna
        # disparaba y las tres las contestó el modelo de memoria. Se inventó a
        # los personajes y un W-Engine que no existe, y Enzo acabó con «Que?
        # Eres tonta o algo?» (chat del 7 sep).
        #
        # Va la ÚLTIMA de la tabla a propósito: solo se queda con lo que
        # ningún otro detector ha querido, así que no puede robarle una frase
        # a nadie. Y solo vale como SEGUIMIENTO — `api.py` la descarta si la
        # respuesta anterior no salió de aquí, igual que con las imágenes.
        (re.compile(
            r"(?is)^(?:"
            r"(?=.*\b(?:build|builds|equipo|equipos|team|teams|comps?|"
            r"composici[oó]n|composiciones|disco|discos|motor|motores|"
            r"mindscape|bangboo|substats?|main\s+stats?|rotaci[oó]n)\b)"
            # «quién es kira» sí, «quién es el presidente de Francia» no: un
            # personaje se nombra a secas, y el artículo delata que lo que
            # sigue es otra cosa. Sin este filtro, en cuanto la conversación
            # tocara ZZZ, cualquier «quién es…» acabaría preguntándole al meta
            # del juego por gente que no sale en él.
            r"|(?=.*\bqui[eé]n(?:es)?\s+(?:es|son)\s+"
            r"(?!(?:el|la|los|las|un|una|unos|unas)\b))"
            r")"
            r"\s*(?P<todo>.+)$"),
         "zzz",
         _consulta_zzz_seguimiento,
         False,
         lambda p: f"ZZZ (seguimiento): {p['consulta'][:50]}"),
    ]

    # Frases descriptivas/meta que hablan de la capacidad de Celestia en
    # lugar de pedirle algo. Sin este filtro el regex de crear_documento
    # matcheaba "tienes la capacidad de crear imágenes" como "crea un PDF
    # sobre 'la capacidad de crear imágenes'" (visto sesión 26 por WhatsApp).
    _META_RE = re.compile(
        r"\b(?:tienes?\s+(?:la\s+)?capacidad\s+de|"
        r"sabes?\s+(?:c[oó]mo\s+)?(?:hacer|crear|generar|dibujar|pintar)|"
        r"en\s+tu\s+c[oó]digo|"
        r"s[eé]\s+que\s+(?:puedes|tienes|en\s+tu)|"
        r"yo\s+s[eé]\s+que)",
        re.I,
    )

    # Verbos en el grupo `abrir_app` que también pueden referirse a un
    # dispositivo del hogar ("abre la luz", "abrir la persiana"). "lanza/
    # inicia" se quedan fuera porque rara vez se usan para domótica.
    _ABRIR_VERBO_RE = re.compile(r"^\s*(?:abre|abrir|abrime|ábre(?:me)?)\b", re.I)

    def detect(self, user_input: str) -> Optional[Dict]:
        if self._META_RE.search(user_input):
            return None
        for pattern, tool, extractor, needs_confirm, desc_fn in self._P:
            m = pattern.search(user_input)
            if m:
                try:
                    params = extractor(m)
                    # Re-ruteo: "abre la luz" cae en abrir_app porque el regex
                    # captura "luz" como app. Si "luz" está registrada como
                    # dispositivo de domótica, encender en su lugar — abrir una
                    # bombilla no es abrir una app. Solo aplica con verbos
                    # "abre/abrir" (no "lanza/inicia", que sí son siempre app).
                    if tool == "abrir_app":
                        app = params.get("app", "")
                        if (app
                            and self._ABRIR_VERBO_RE.match(user_input)
                            and _is_domotica_device(app)):
                            return {
                                "tool": "domotica_encender",
                                "params": {"dispositivo": app},
                                "needs_confirm": False,
                                "description": f"Encender {app} (dispositivo del hogar)",
                            }
                    return {
                        "tool": tool,
                        "params": params,
                        "needs_confirm": needs_confirm,
                        "description": desc_fn(params),
                    }
                except Exception:
                    continue
        return None


# ─────────────────────────────────────────────
# CLI y bucles
# ─────────────────────────────────────────────
AYUDA = """
Comandos disponibles:
  /ayuda          — Muestra esta ayuda
  /herramientas   — Lista las herramientas del agente
  /info           — Estado del sistema (backend, modelo, dispositivo)
  /red            — Estado de la conexión a internet
  /recordatorios  — Lista recordatorios pendientes
  /estado         — Últimos episodios autónomos (métricas)
  /memoria        — Últimas conversaciones recordadas
  /metas          — Lista de objetivos activos
  /hparams        — Hiperparámetros actuales
  /ciclo          — Ejecuta un ciclo autónomo ahora
  /limpiar        — Borra el historial de conversación de esta sesión
  salir           — Termina Celestia

Herramientas — escribe en lenguaje natural:
  "busca en internet X"                    — búsqueda web automática
  "información del sistema"                — RAM, disco, uptime, CPU
  "lista los archivos en /ruta"            — explorar directorios
  "lee el archivo /ruta/archivo"           — leer un archivo
  "busca el archivo nombre.py"             — buscar archivos
  "busca duplicados en /ruta"              — detectar archivos duplicados
  "organiza los archivos de /ruta"         — clasificar por tipo
  "descarga https://url.com/archivo"       — descargar desde URL
  "abre YouTube / Chrome / WhatsApp..."   — abrir app Android
  "recuérdame en 30 minutos que X"         — programar recordatorio
  "recuérdame a las 18:00 que X"           — recordatorio a hora exacta
  "guarda el sistema"                      — análisis de seguridad
  "ejecuta 'comando'"                      — ejecutar shell
  "crea el archivo X con contenido: ..."  — crear archivo
  "borra el archivo X"                     — borrar (pide confirmación)
"""

def _banner(orch: Orchestrator):
    st = orch.status()
    modelo_raw = st["modelo"].split("/")[-1]
    modelo_corto = modelo_raw[:30] if len(modelo_raw) <= 30 else modelo_raw[:27] + "..."
    embedder_str = "semántico" if st["embedder"] == "semántico" else "Jaccard"
    agente_str = "sí" if HAS_REQUESTS else "sin web"
    modo = orch.resources.modo_ejecucion()
    vision_local = orch.resources.recommend_vision_model() or "—"
    intervalo = AgenteAutonomo.INTERVALOS.get(modo, 1.5)
    print(f"""
╔══════════════════════════════════════════════╗
║{('Celestia v' + __version__ + '  —  Agente Autónomo').center(46)}║
╠══════════════════════════════════════════════╣
║  Modo      : {modo:<31} ║
║  Plataforma: {PLATAFORMA:<31} ║
║  Modelo    : {modelo_corto:<31} ║
║  Backend   : {st["backend"]:<31} ║
║  Dispositivo: {st["device"]:<30} ║
║  Visión    : {vision_local.split('/')[-1][:31]:<31} ║
║  Embedder  : {embedder_str:<31} ║
║  Agente UI : ciclo {intervalo:.2f}s {('— GPU rápido' if 'gpu' in modo else '— CPU/móvil'):<13} ║
╚══════════════════════════════════════════════╝
Escribe tu mensaje o /ayuda para ver comandos.
""")


def run_autonomous(orch: Orchestrator, loops: int, interval: float):
    logger.info("Modo autónomo — loops=%s intervalo=%.1fs", loops or "∞", interval)
    i = 0
    try:
        while True:
            res = orch.run_cycle()
            print(f"[Ciclo {res['cycle']}] ppl={res['ppl']:.1f} "
                  f"coh={res['coherence']:.3f} div={res['diversity']:.3f}")
            i += 1
            if loops and i >= loops:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Interrumpido por usuario")
    finally:
        orch.close()


def run_conversation(orch: Orchestrator):
    _banner(orch)
    stream_available = orch.model.loaded
    from .tools import AgentTools  # lazy: evita ciclo agent ↔ tools
    reminder_mgr = ReminderManager()
    reminder_mgr.start()
    tools = AgentTools(orch.connectivity, reminder_mgr)
    planner = AgentPlanner()
    model_lock = threading.Lock()

    try:
        while True:
            try:
                user = input("Tú: ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user:
                continue
            if user.lower() in ("salir", "exit", "quit"):
                break

            # ── Comandos especiales ──
            if user.lower() == "/ayuda":
                print(AYUDA)
                continue

            if user.lower() == "/herramientas":
                print("\n  Herramientas disponibles (Fase 2):")
                herrs = [
                    ("buscar_web",       "Búsqueda en internet (automática)",      "no"),
                    ("info_sistema",     "RAM, disco, CPU, uptime",                "no"),
                    ("listar_archivos",  "Explorar directorio",                    "no"),
                    ("leer_archivo",     "Leer contenido de un archivo",           "no"),
                    ("buscar_archivos",  "Buscar archivos por patrón",             "no"),
                    ("buscar_duplicados","Detectar archivos duplicados",           "no"),
                    ("organizar_archivos","Clasificar archivos por tipo",          "no"),
                    ("descargar_archivo","Descargar desde una URL",                "no"),
                    ("abrir_app",        "Abrir app Android",                      "no"),
                    ("recordatorio",     "Programar recordatorio",                 "no"),
                    ("guardian_sistema", "Análisis de seguridad del sistema",      "no"),
                    ("capturar_pantalla","Capturar pantalla (Fase 3)",             "no"),
                    ("ejecutar_comando", "Ejecutar comando de shell",              "no"),
                    ("crear_archivo",    "Crear archivo con contenido",            "no"),
                    ("borrar",           "Borrar archivo o directorio",            "sí"),
                ]
                for nombre, desc, conf in herrs:
                    print(f"  {'🔐' if conf=='sí' else '  '} {nombre:<22} — {desc}")
                print()
                continue

            if user.lower() == "/info":
                st = orch.status()
                print(f"\n  Backend   : {st['backend']}")
                print(f"  Modelo    : {st['modelo']}")
                print(f"  Dispositivo: {st['device']}")
                print(f"  Embedder  : {st['embedder']}")
                print(f"  FAISS     : {'sí' if st['faiss'] else 'no'}")
                print(f"  Web       : {'sí' if HAS_REQUESTS else 'no (pip install requests)'}")
                print(f"  Cargado   : {'sí' if st['cargado'] else 'no'}\n")
                continue

            if user.lower() == "/red":
                online = orch.connectivity.is_online(force=True)
                estado = "en línea ✓" if online else "sin conexión ✗"
                pendientes = orch.connectivity.pending_count()
                print(f"\n  Internet  : {estado}")
                if pendientes:
                    print(f"  Búsquedas encoladas: {pendientes}")
                print()
                continue

            if user.lower() == "/recordatorios":
                print(f"\n{reminder_mgr.list_pending()}\n")
                continue

            if user.lower() == "/estado":
                rows = orch.memory.recent_episodes(5)
                if not rows:
                    print("\n  Sin episodios autónomos aún.\n")
                else:
                    print(f"\n  Últimos {len(rows)} episodios:")
                    for r in rows:
                        ts = datetime.fromtimestamp(r[4]).strftime("%H:%M:%S")
                        print(f"  [{ts}] {r[0][:35]:<35} ppl={r[1]:.1f} coh={r[3]:.3f}")
                    print()
                continue

            if user.lower() == "/memoria":
                rows = orch.memory.recent_conversations(5)
                if not rows:
                    print("\n  Sin conversaciones previas guardadas.\n")
                else:
                    print(f"\n  Últimas {len(rows)} conversaciones:")
                    for r in rows:
                        ts = datetime.fromtimestamp(r[2]).strftime("%d/%m %H:%M")
                        print(f"  [{ts}] Tú: {r[0][:40]}")
                        print(f"         Celestia: {r[1][:60]}...")
                    print()
                continue

            if user.lower() == "/metas":
                metas = orch.goals.list_all()
                if not metas:
                    print("\n  Sin metas definidas.\n")
                else:
                    print("\n  Metas actuales:")
                    for m in metas:
                        estado = "✓" if m["status"] == "satisfied" else "·"
                        print(f"  [{estado}] (p={m['priority']}) {m['goal']}")
                    print()
                continue

            if user.lower() == "/hparams":
                p = orch.hparams.params()
                print(f"\n  temp={p['temp']:.3f}  top_k={p['top_k']}  rep_penalty={p['rep_penalty']:.2f}\n")
                continue

            if user.lower() == "/ciclo":
                print("  Ejecutando ciclo autónomo...")
                res = orch.run_cycle()
                print(f"  Ciclo {res['cycle']}: ppl={res['ppl']:.1f} coh={res['coherence']:.3f}")
                print(f"  Respuesta: {res['response'][:120]}...\n")
                continue

            if user.lower() == "/limpiar":
                orch.conv_history.clear()
                print("\n  Historial de conversación borrado.\n")
                continue

            # ── Fase 2: detección de intención de herramienta ──
            intent = planner.detect(user)
            if intent:
                print(f"\n  → {intent['description']}")
                if intent["needs_confirm"]:
                    try:
                        confirm = input("  ¿Ejecutar? (s/n): ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print("  Cancelado.\n")
                        continue
                    if confirm != "s":
                        print("  Cancelado.\n")
                        continue

                tool_result = tools.execute(intent)
                print(f"\n  Resultado:\n{_indent(tool_result)}\n")

                augmented = (
                    f"{user}\n\n"
                    f"[Resultado de la acción '{intent['tool']}':\n{tool_result[:1500]}]\n"
                    f"Basándote en ese resultado, da una respuesta clara y concisa."
                )
                with model_lock:
                    response = orch.respond(augmented, stream=stream_available)
                if not stream_available:
                    print(f"Celestia: {response}\n")
                else:
                    print()
                continue

            # ── Conversación normal ──
            with model_lock:
                response = orch.respond(user, stream=stream_available)
            if not stream_available:
                print(f"Celestia: {response}\n")
            else:
                print()

    except Exception as e:
        logger.error("Error inesperado en conversación: %s", e)
    finally:
        reminder_mgr.stop()
        print("\nHasta luego.")
        orch.close()


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ─────────────────────────────────────────────
# Agente autónomo de UI — ejecución por pasos con verificación
# ─────────────────────────────────────────────

class MemoriaPatronesUI:
    """Guarda secuencias UI exitosas para reusarlas en tareas similares."""

    _FILE = MEM_DIR / "patrones_ui.json"

    def __init__(self):
        self.patrones: List[Dict] = []
        if self._FILE.exists():
            try:
                self.patrones = json.loads(self._FILE.read_text(encoding="utf-8"))
            except Exception:
                self.patrones = []

    def buscar(self, app: str, accion: str) -> Optional[Dict]:
        """Busca un patrón previo para esa app + acción."""
        for p in reversed(self.patrones):
            if p["app"].lower() == app.lower() and accion.lower() in p["accion"].lower():
                return p
        return None

    def guardar(self, app: str, accion: str, comandos: List[str]) -> None:
        self.patrones.append({
            "app": app, "accion": accion, "comandos": comandos,
            "ts": datetime.now().isoformat(), "usos": 1,
        })
        # Limitar a últimos 200 patrones
        self.patrones = self.patrones[-200:]
        try:
            self._FILE.write_text(
                json.dumps(self.patrones, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


class AgenteAutonomo:
    """
    Ejecuta tareas multi-paso en una app con monitor continuo de pantalla.
    Un hilo paralelo mantiene siempre fresca la descripción de la pantalla,
    así el agente nunca actúa a ciegas. Aprende guardando patrones exitosos.
    """

    SCREEN_FILE = "/sdcard/Celestia/.movil/screen.png"
    UI_CMD_RE = re.compile(r"\[(TAP|SWIPE|INPUT_TEXT|BACK|HOME):([^\]]*)\]")
    USAR_UI_AUTOMATOR = True

    # Intervalo de monitoreo según modo (segundos entre ciclos)
    INTERVALOS = {
        "movil":          0.5,    # Android: UI Automator nativo
        "pc-gpu-alto":    0.25,   # GPU+VRAM grande: visión local <250ms
        "pc-gpu-medio":   0.35,
        "pc-gpu-bajo":    0.5,
        "pc-cpu":         1.5,    # sin GPU: API + OCR
    }

    def __init__(self, orchestrator, analizar_imagen_fn, generar_codigo_fn,
                 notificar_fn):
        self.orch       = orchestrator
        self.analizar   = analizar_imagen_fn
        self.generar    = generar_codigo_fn
        self.notificar  = notificar_fn
        self.patrones   = MemoriaPatronesUI()
        # Auto-configurar velocidad según hardware
        modo = orchestrator.resources.modo_ejecucion()
        self.INTERVALO_MONITOR = self.INTERVALOS.get(modo, 1.5)
        self.modo_ejecucion = modo
        logger.info("Agente autónomo iniciado en modo '%s' (intervalo %.2fs)",
                     modo, self.INTERVALO_MONITOR)
        # Estado compartido del monitor continuo
        self._estado_lock   = threading.Lock()
        self._descripcion   = ""
        self._ts_analisis   = 0.0
        self._captura_b64   = ""
        self._monitor_activo = False
        self._monitor_thread: Optional[threading.Thread] = None

    # ── Monitor continuo de pantalla ───────────────────────────────────────
    def _iniciar_monitor(self) -> None:
        if self._monitor_activo:
            return
        self._monitor_activo = True
        self._monitor_thread = threading.Thread(
            target=self._loop_monitor, daemon=True, name="agente-monitor"
        )
        self._monitor_thread.start()
        # Registrar shutdown una vez para que el thread se cierre limpio al
        # terminar el proceso. Sin esto, el daemon=True funciona pero podemos
        # dejar requests HTTP a medias o subprocesos huérfanos.
        if not getattr(self, "_atexit_registrado", False):
            atexit.register(self._detener_monitor)
            self._atexit_registrado = True

    def _detener_monitor(self) -> None:
        self._monitor_activo = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)

    def _loop_monitor(self) -> None:
        """Lee estado UI en loop. Estrategia por plataforma: nativo → OCR → visión."""
        while self._monitor_activo:
            try:
                desc = ""
                cap = ""
                # 1. Intentar lectura nativa de UI (rápida y exacta)
                xml = self._leer_ui_xml()
                if xml:
                    desc = self._resumir_ui(xml)
                # 2. Fallback OCR (Linux/macOS sin accesibilidad)
                if not desc and PLATAFORMA in ("linux", "macos"):
                    cap = self._capturar_pantalla() or ""
                    if cap:
                        desc = self._leer_ui_via_ocr(cap) or ""
                # 3. Último recurso: visión LLM (lenta pero universal)
                if not desc:
                    cap = cap or (self._capturar_pantalla() or "")
                    if cap:
                        desc = self.analizar(
                            cap, False,
                            "Describe pantalla con coordenadas X,Y de elementos clickeables. "
                            "Máximo 150 palabras."
                        ) or ""
                with self._estado_lock:
                    self._descripcion = desc
                    if cap:
                        self._captura_b64 = cap
                    self._ts_analisis = time.time()
            except Exception as e:
                logger.warning("Monitor falló: %s", e)
            time.sleep(self.INTERVALO_MONITOR)

    def _estado_actual(self, max_edad_s: float = 1.5) -> Tuple[str, str]:
        """Devuelve (descripción UI, captura_b64) del estado actual de pantalla."""
        with self._estado_lock:
            edad = time.time() - self._ts_analisis
            desc, cap = self._descripcion, self._captura_b64
        if edad < max_edad_s and desc:
            return desc, cap
        # Forzar lectura fresca con UI Automator
        if self.USAR_UI_AUTOMATOR:
            xml = self._leer_ui_xml()
            if xml:
                desc = self._resumir_ui(xml)
                with self._estado_lock:
                    self._descripcion = desc
                    self._ts_analisis = time.time()
                return desc, cap
        # Fallback visión
        cap = self._capturar_pantalla()
        if not cap:
            return "", ""
        desc = self.analizar(cap, False,
            "Describe pantalla con coords X,Y (1080×2400). 150 palabras max.") or ""
        with self._estado_lock:
            self._descripcion = desc
            self._captura_b64 = cap
            self._ts_analisis = time.time()
        return desc, cap

    # ── Lectura instantánea de UI (cross-platform) ─────────────────────────
    def _leer_ui_xml(self) -> Optional[str]:
        """Devuelve estructura UI actual con coordenadas exactas. None si no disponible."""
        if PLATAFORMA == "android":
            try:
                subprocess.run(
                    ["/system/bin/uiautomator", "dump", "/sdcard/Celestia/.movil/ui.xml"],
                    check=True, timeout=5, capture_output=True,
                )
                return Path("/sdcard/Celestia/.movil/ui.xml").read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("uiautomator dump falló: %s", e)
        elif PLATAFORMA == "windows" and HAS_PYWINAUTO:
            try:
                from pywinauto import Desktop
                # Volcar elementos visibles en una pseudo-XML
                desktop = Desktop(backend="uia")
                ventana = desktop.windows()[0] if desktop.windows() else None
                if ventana:
                    return self._pywinauto_a_xml(ventana)
            except Exception as e:
                logger.warning("pywinauto falló: %s", e)
        return None

    def _pywinauto_a_xml(self, elemento, depth: int = 0, max_depth: int = 4) -> str:
        """Convierte árbol pywinauto a formato XML pseudo-uiautomator."""
        if depth > max_depth:
            return ""
        try:
            info = elemento.element_info
            rect = info.rectangle
            text = (info.name or "").replace('"', "'")[:50]
            ctrl = info.control_type or "Control"
            xml = (f'<node text="{text}" class="{ctrl}" '
                    f'bounds="[{rect.left},{rect.top}][{rect.right},{rect.bottom}]" '
                    f'clickable="true">')
            for hijo in info.children()[:20]:
                try:
                    xml += self._pywinauto_a_xml(hijo, depth + 1, max_depth)
                except Exception:
                    continue
            xml += "</node>"
            return xml
        except Exception:
            return ""

    def _leer_ui_via_ocr(self, captura_b64: str) -> Optional[str]:
        """Fallback Linux/macOS: usar OCR sobre captura para extraer texto + coordenadas."""
        if not HAS_OCR:
            return None
        try:
            import base64, io
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(captura_b64)))
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, lang="spa+eng")
            elementos = []
            for i, texto in enumerate(data["text"]):
                if not texto.strip() or int(data["conf"][i]) < 40:
                    continue
                cx = data["left"][i] + data["width"][i] // 2
                cy = data["top"][i] + data["height"][i] // 2
                elementos.append(f"Text '{texto[:40]}' en ({cx},{cy})")
            return "\n".join(elementos[:60])
        except Exception as e:
            logger.warning("OCR falló: %s", e)
            return None

    def _resumir_ui(self, xml: str, max_elementos: int = 40) -> str:
        """Extrae los elementos clickeables/visibles más relevantes del XML."""
        elementos = []
        for m in re.finditer(
            r'text="([^"]*)"[^>]*?(?:resource-id="([^"]*)")?[^>]*?'
            r'class="([^"]*)"[^>]*?(?:content-desc="([^"]*)")?[^>]*?'
            r'(?:clickable="(true|false)")?[^>]*?'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        ):
            text, rid, clase, desc, clickable, x1, y1, x2, y2 = m.groups()
            rid_short = (rid or "").split("/")[-1]
            label = text or (desc or "") or rid_short
            if not label and not (clickable == "true"):
                continue
            cx = (int(x1) + int(x2)) // 2
            cy = (int(y1) + int(y2)) // 2
            tipo = (clase or "").split(".")[-1] if clase else "?"
            click_str = " [clickable]" if clickable == "true" else ""
            elementos.append(f"{tipo} '{label[:40]}' en ({cx},{cy}){click_str}")
            if len(elementos) >= max_elementos:
                break
        return "\n".join(elementos)

    # ── Captura y control UI (cross-platform) ──────────────────────────────
    def _capturar_pantalla(self) -> Optional[str]:
        """Toma captura y devuelve base64. None si falla."""
        import base64
        try:
            if PLATAFORMA == "android":
                subprocess.run(
                    ["/system/bin/screencap", "-p", self.SCREEN_FILE],
                    check=True, timeout=10, capture_output=True,
                )
                with open(self.SCREEN_FILE, "rb") as f:
                    return base64.b64encode(f.read()).decode()
            elif HAS_PYAUTOGUI:
                import io
                img = pyautogui.screenshot()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            logger.warning("Captura falló: %s", e)
        return None

    def _ejecutar_comando_ui(self, comando: str) -> None:
        """Ejecuta comando UI cross-platform: [TAP:x,y] [SWIPE:...] [INPUT_TEXT:...] [BACK:] [HOME:]"""
        m = self.UI_CMD_RE.search(comando)
        if not m:
            return
        tipo, params = m.group(1), m.group(2).strip()
        try:
            if PLATAFORMA == "android":
                if tipo == "TAP":
                    x, y = [s.strip() for s in params.split(",")]
                    subprocess.run(["/system/bin/input", "tap", x, y], timeout=5)
                elif tipo == "SWIPE":
                    partes = [s.strip() for s in params.split(",")]
                    x1, y1, x2, y2 = partes[:4]
                    ms = partes[4] if len(partes) > 4 else "300"
                    subprocess.run(["/system/bin/input", "swipe", x1, y1, x2, y2, ms], timeout=5)
                elif tipo == "INPUT_TEXT":
                    texto = params.replace(" ", "%s")
                    subprocess.run(["/system/bin/input", "text", texto], timeout=5)
                elif tipo == "BACK":
                    subprocess.run(["/system/bin/input", "keyevent", "4"], timeout=5)
                elif tipo == "HOME":
                    subprocess.run(["/system/bin/input", "keyevent", "3"], timeout=5)
            elif HAS_PYAUTOGUI:
                # PC: Linux / Windows / macOS
                if tipo == "TAP":
                    x, y = [int(s.strip()) for s in params.split(",")]
                    pyautogui.click(x, y)
                elif tipo == "SWIPE":
                    partes = [s.strip() for s in params.split(",")]
                    x1, y1, x2, y2 = [int(p) for p in partes[:4]]
                    duracion = int(partes[4]) / 1000 if len(partes) > 4 else 0.3
                    pyautogui.moveTo(x1, y1)
                    pyautogui.dragTo(x2, y2, duration=duracion, button="left")
                elif tipo == "INPUT_TEXT":
                    pyautogui.typewrite(params, interval=0.02)
                elif tipo == "BACK":
                    pyautogui.hotkey("alt", "left")
                elif tipo == "HOME":
                    if sys.platform.startswith("win"):
                        pyautogui.hotkey("win", "d")
                    else:
                        pyautogui.hotkey("ctrl", "alt", "d")
            else:
                logger.warning("Sin backend de control UI para %s", PLATAFORMA)
        except Exception as e:
            logger.warning("Comando UI %s falló: %s", tipo, e)

    # ── Loop principal ─────────────────────────────────────────────────────
    def ejecutar_tarea(self, tarea: str, app: str = "", max_pasos: int = 30) -> str:
        """
        Ejecuta una tarea autónomamente con visión continua de la pantalla.
        Mantiene un hilo monitor que actualiza el estado cada ~1.5s para no
        actuar a ciegas y poder reaccionar a popups/cambios mid-acción.
        """
        self.notificar(f"🤖 Empezando tarea autónoma: {tarea}\n"
                       f"Voy a ver la pantalla en continuo y actuar paso a paso.")

        # Activar monitor continuo durante toda la tarea
        self._iniciar_monitor()
        try:
            # 1. Descomponer en pasos
            plan = self._planificar(tarea, app)
            if not plan:
                return "✗ No pude planificar la tarea."

            self.notificar("📋 Plan:\n" + "\n".join(f"  {i+1}. {p}" for i, p in enumerate(plan)))

            resultados: List[str] = []
            for i, paso in enumerate(plan):
                self.notificar(f"▸ Paso {i+1}/{len(plan)}: {paso}")
                exito, info = self._ejecutar_paso(paso, app, max_intentos=5)
                resultados.append(f"{'✓' if exito else '✗'} {paso}: {info[:100]}")
                if not exito:
                    self.notificar(f"⚠ El paso {i+1} falló. Re-planifico el resto.")
                    plan_nuevo = self._replanificar(tarea, plan[:i], info)
                    if plan_nuevo:
                        plan = plan[:i] + plan_nuevo
                    else:
                        break
                time.sleep(0.6)

            resumen = f"🤖 Tarea '{tarea}' completada.\n" + "\n".join(resultados)
            self.notificar(resumen)
            return resumen
        finally:
            self._detener_monitor()

    def _planificar(self, tarea: str, app: str) -> List[str]:
        """Pide al LLM que descomponga la tarea en pasos discretos."""
        prompt = (
            f"Descompón esta tarea en pasos UI discretos y verificables en Android"
            f"{' (app: ' + app + ')' if app else ''}:\n"
            f"TAREA: {tarea}\n\n"
            "Cada paso debe ser una acción concreta (ej: 'abrir WhatsApp', "
            "'buscar contacto Lucía', 'tocar el contacto', 'escribir mensaje X', 'enviar').\n"
            "Devuelve UNA línea por paso, sin numerar. Sin explicaciones. Máximo 15 pasos."
        )
        try:
            respuesta = self.generar(prompt, max_tokens=500)
            pasos = [l.strip(" -•*").strip() for l in respuesta.split("\n")
                      if l.strip() and len(l.strip()) > 5]
            return pasos[:15]
        except Exception:
            return []

    def _replanificar(self, tarea: str, pasos_hechos: List[str],
                       motivo_fallo: str) -> List[str]:
        prompt = (
            f"Estaba ejecutando: '{tarea}'\n"
            f"Pasos completados:\n" + "\n".join(f"- {p}" for p in pasos_hechos) + "\n\n"
            f"El siguiente paso falló porque: {motivo_fallo[:200]}\n\n"
            "Devuelve los pasos restantes ajustados al estado actual. "
            "Una línea por paso, sin numerar, máximo 10."
        )
        try:
            respuesta = self.generar(prompt, max_tokens=400)
            return [l.strip(" -•*").strip() for l in respuesta.split("\n")
                    if l.strip() and len(l.strip()) > 5][:10]
        except Exception:
            return []

    def _ejecutar_paso(self, paso: str, app: str,
                        max_intentos: int) -> Tuple[bool, str]:
        """Ejecuta un paso usando estado en vivo del monitor continuo."""
        # 1. Patrón aprendido (atajo rápido)
        patron = self.patrones.buscar(app, paso) if app else None
        if patron:
            for cmd in patron["comandos"]:
                self._ejecutar_comando_ui(cmd)
                time.sleep(0.3)
            patron["usos"] = patron.get("usos", 0) + 1
            time.sleep(self.INTERVALO_MONITOR + 0.3)  # esperar a que monitor actualice
            descripcion, captura = self._estado_actual()
            if captura and self._verificar(paso, captura):
                return True, "patrón reusado"

        # 2. Generar acción usando estado del monitor (siempre fresco)
        for intento in range(1, max_intentos + 1):
            descripcion, captura = self._estado_actual()
            if not descripcion:
                return False, "sin visión de pantalla"

            prompt = (
                f"Estado actual de pantalla (visión continua):\n{descripcion}\n\n"
                f"Paso a ejecutar: {paso}\n\n"
                "Genera el/los comando(s) UI necesarios (uno por línea):\n"
                "[TAP:x,y]   [SWIPE:x1,y1,x2,y2,ms]   [INPUT_TEXT:texto]   [BACK:]   [HOME:]\n"
                "Si la pantalla no permite este paso, responde: NO_POSIBLE\n"
                "Solo comandos, sin explicación:"
            )
            try:
                respuesta = self.generar(prompt, max_tokens=200).strip()
            except Exception:
                continue

            if "NO_POSIBLE" in respuesta.upper():
                return False, "pantalla incorrecta"

            comandos = self.UI_CMD_RE.findall(respuesta)
            if not comandos:
                continue

            cmd_strs = [f"[{t}:{p}]" for t, p in comandos]
            for cmd in cmd_strs:
                self._ejecutar_comando_ui(cmd)
                time.sleep(0.3)
            # Esperar a que monitor capture el nuevo estado (al menos 1 ciclo)
            time.sleep(self.INTERVALO_MONITOR + 0.3)

            # Verificar con la captura más reciente del monitor
            _, captura_post = self._estado_actual()
            if captura_post and self._verificar(paso, captura_post):
                if app:
                    self.patrones.guardar(app, paso, cmd_strs)
                return True, "ok"

            logger.info("Intento %d para '%s' no verificó — reintento", intento, paso)

        return False, f"fallé tras {max_intentos} intentos"

    def _verificar(self, paso: str, captura_b64: str) -> bool:
        """Pregunta al modelo de visión si el paso se completó."""
        try:
            descripcion = self.analizar(
                captura_b64, False,
                f"¿La pantalla muestra que se completó este paso: '{paso}'?\n"
                f"Responde SOLO 'SI' o 'NO' al inicio, después una frase breve."
            ) or ""
            return descripcion.upper().lstrip().startswith("SI")
        except Exception:
            return False


# PerfilUsuario y OnboardingFlow se movieron a celestia_lib/profile.py (refactor sesión 15)
from celestia_lib.profile import PerfilUsuario, OnboardingFlow


# ─────────────────────────────────────────────
# Modo WhatsApp — servidor HTTP + STT + TTS
# ─────────────────────────────────────────────

