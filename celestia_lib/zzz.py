"""Saber de ZZZ — el meta lo pone la web, el criterio lo pone el código.

Enzo (6 sep 2026): «quiero que también sepa hacer equipos, las mejores
combinaciones y mejores builds para los personajes», y cuando le dije que mi
conocimiento del juego tenía fecha de caducidad: «para eso tiene acceso a
Google, puede buscar y tener siempre lo más nuevo».

Tenía razón, y la comprobación fue inmediata: la guía de equipos de Game8
estaba actualizada al 4 de septiembre de 2026 y hablaba de la versión 3.1/3.2,
con personajes (Remielle, Promeia, Velina, Ye Shunguang, Dialyn, Sunna) que yo
no había visto nunca. Un catálogo escrito de memoria habría nacido viejo y,
peor, habría **sonado** seguro estando equivocado.

De ahí el reparto de este fichero, que es lo único importante que hay que
entender de él:

  · **Los hechos vienen de fuera.** Qué personajes existen, qué W-Engine lleva
    cada uno, qué set de discos, qué equipos se están usando: todo eso se
    descarga, se extrae y se guarda con **fecha y fuente**. Nada de eso se
    escribe aquí a mano.

  · **El criterio vive aquí.** Cruzar el meta con lo que Enzo tiene de verdad,
    decir qué equipo puede montar hoy y a cuál le falta un personaje, ordenar
    por cuánto le falta. Eso es aritmética, es estable, y no cambia con el
    parche.

Y la extracción es **determinista**: expresiones regulares sobre las tablas del
HTML, cero llamadas al modelo. Sale igual con Groq, con el modelo local o sin
red (desde la caché). Es la regla de [[feedback_funcionar_cualquier_modelo]]:
si una función depende de que el LLM esté fino ese día, no es una función.

Lo que este fichero NO hace, dicho antes de que nadie se ilusione: no juega. El
saber de qué equipo montar y la mano para pelear son dos problemas distintos y
el segundo vive en `jugador.py`, esperando a que adb esté emparejado. Este de
aquí funciona hoy, sin Shizuku y sin tocar el móvil.

Una honestidad más, metida en el código y no solo en este comentario: si la web
cambia de formato y la extracción se queda a cero, `salud()` lo dice y las
consultas avisan. Una build vacía servida como buena es peor que un error.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from celestia_lib.paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

# Cuántos días vale una consulta guardada antes de volver a preguntar a la web.
# Una semana es el equilibrio: el meta de ZZZ se mueve con los parches (cada
# ~6 semanas) y con los personajes nuevos, no de un día para otro. Y cada
# refresco son ~500 KB de descarga en los datos de Enzo.
DIAS_FRESCURA = 7

# Tope de descarga por página. Game8 pesa ~500 KB; medio mega de margen evita
# que una redirección rara a un vídeo se coma la RAM del móvil. La lección de
# los 268 MB de la S58 es que en este aparato no hay lecturas «pequeñas».
TOPE_DESCARGA = 3 * 1024 * 1024

_UA = "Mozilla/5.0 (Linux; Android 13) Celestia/1.0"


# ─────────────────────────── limpieza de HTML ───────────────────────────

def _limpiar_html(t: str) -> str:
    """Quita lo que solo es ruido: scripts, estilos y comentarios.

    En la página de equipos son 22 KB de anuncios de 513 KB totales, pero sobre
    todo son trampas para el que parsea: dentro de un <script> hay comillas,
    `<tr>` en cadenas de texto y hasta comentarios en japonés.
    """
    t = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', t)
    t = re.sub(r'(?s)<!--.*?-->', ' ', t)
    return t


def _texto(fragmento: str) -> str:
    """Fragmento de HTML → texto plano, sin perder a los personajes.

    En Game8 los miembros de un equipo son iconos y su nombre solo vive en el
    `alt` de la imagen... unas veces. Otras está también escrito al lado. Así
    que el `alt` no se pega siempre —eso daba «Miyabi Miyabi»— ni se tira
    siempre —eso daba equipos vacíos—: se añade **solo lo que no esté ya**.

    Las comillas del atributo pueden ser dobles, simples o ninguna; Game8 usa
    las tres en la misma página.
    """
    alts: List[str] = []
    for m in re.finditer(r"""(?i)<img[^>]*\balt=("([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>""",
                         fragmento):
        alt = (m.group(2) or m.group(3) or m.group(4) or "").strip()
        if alt:
            alts.append(_html.unescape(alt))
    sin_img = re.sub(r'(?i)<img[^>]*>', ' ', fragmento)
    sin_img = re.sub(r'(?i)<br\s*/?>', ' ', sin_img)
    plano = re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', sin_img))).strip()

    # Los `alt` de Game8 llevan prefijo de juego: alt='ZZZ - Hailstorm Shrine'
    # junto al texto «Hailstorm Shrine». Sin quitarlo, ninguno de los dos
    # contiene al otro y la celda salía como «ZZZ - Hailstorm Shrine Hailstorm
    # Shrine». Lo enseñó la página real; con el fixture no se veía.
    # Dos adornos, el mismo problema: el prefijo del juego («ZZZ - Hailstorm
    # Shrine») y el sufijo de los iconos («Branch and Blade Song Icon»). En
    # ambos casos el `alt` dice lo mismo que el texto de al lado con una
    # palabra de más, y esa palabra bastaba para que se colaran los dos.
    alts = [re.sub(r'(?i)\s+icon\s*$', '',
                   re.sub(r'(?i)^\s*zzz\s*[-–—:]\s*', '', a)).strip() for a in alts]

    comparable = normalizar(plano)
    if not comparable:
        return " ".join(dict.fromkeys(a for a in alts if a)).strip()
    # El `alt` solo entra si dice algo que el texto no dice ya — en cualquiera
    # de los dos sentidos, porque a veces el largo es el alt y a veces el texto.
    extras = [a for a in alts
              if normalizar(a) and normalizar(a) not in comparable
              and comparable not in normalizar(a)]
    return (" ".join(dict.fromkeys(extras)) + " " + plano).strip() if extras else plano


def _filas(tabla: str) -> List[List[str]]:
    """Tabla HTML → lista de filas, cada una lista de celdas con texto."""
    salida: List[List[str]] = []
    for f in re.findall(r'(?is)<tr.*?</tr>', tabla):
        celdas = [_texto(c) for c in re.findall(r'(?is)<t[dh][^>]*>(.*?)</t[dh]>', f)]
        celdas = [c for c in celdas if c]
        if celdas:
            salida.append(celdas)
    return salida


def _filas_partidas(tabla: str) -> List[List[List[str]]]:
    """Como `_filas`, pero sin aplanar lo que la celda separa con `<hr>`.

    Hace falta porque las páginas de personaje meten **el nombre y el rol en la
    misma celda** («Miyabi ─ Anomaly»). Aplanando eso queda «Miyabi Anomaly» y
    ya no hay forma de saber dónde acaba el nombre: se pierden todos los
    equipos de todas las páginas de personaje, que es justo lo que pasaba.
    """
    salida: List[List[List[str]]] = []
    for f in re.findall(r'(?is)<tr.*?</tr>', tabla):
        celdas: List[List[str]] = []
        for c in re.findall(r'(?is)<t[dh][^>]*>(.*?)</t[dh]>', f):
            trozos = [_texto(x) for x in re.split(r'(?i)<hr[^>]*>', c)]
            trozos = [x for x in trozos if x]
            if trozos:
                celdas.append(trozos)
        if celdas:
            salida.append(celdas)
    return salida


def _tablas(t: str) -> List[str]:
    return re.findall(r'(?is)<table.*?</table>', t)


def normalizar(nombre: str) -> str:
    """Nombre de personaje → forma comparable.

    Hace falta porque el mismo personaje aparece como «Miyabi», «Hoshimi
    Miyabi», «miyabi» y a veces con acentos según la página. Comparar sin esto
    es no encontrar la mitad de las coincidencias.
    """
    s = unicodedata.normalize("NFD", (nombre or "").strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r'[^a-z0-9 ]+', '', s).strip()


# ─────────────────────────────── los datos ───────────────────────────────

@dataclass
class Equipo:
    """Un equipo recomendado, tal y como lo publica la fuente."""
    nombre: str = ""
    roles: List[str] = field(default_factory=list)      # DPS · Stun · Support
    miembros: List[str] = field(default_factory=list)
    alternativas: List[str] = field(default_factory=list)
    bangboo: str = ""
    fuente: str = ""

    def completo(self) -> bool:
        return len(self.miembros) >= 3


@dataclass
class Build:
    """La build de un personaje: con qué se le equipa y por qué."""
    personaje: str = ""
    motor: str = ""                                      # mejor W-Engine
    motores_alt: List[str] = field(default_factory=list)
    discos: str = ""                                     # set 4-pc + 2-pc
    discos_alt: List[str] = field(default_factory=list)
    stats: Dict[str, str] = field(default_factory=dict)  # {"6": "ATK", "5": "Ice DMG"}
    substats: List[str] = field(default_factory=list)    # por orden de prioridad
    equipos: List[Equipo] = field(default_factory=list)
    fuente: str = ""
    obtenido: float = 0.0                                # epoch
    version_juego: str = ""

    def vacia(self) -> bool:
        """Ni motor ni discos = no se extrajo nada, por mucho que haya objeto."""
        return not (self.motor or self.discos)

    def dias(self) -> float:
        return (time.time() - self.obtenido) / 86400 if self.obtenido else 1e9


# ───────────────────────────── la extracción ─────────────────────────────

def version_del_juego(t: str) -> str:
    """La versión más alta que se nombre en la página.

    Sirve para dos cosas: decirle a Enzo de qué parche es el consejo, y notar
    que la guía se ha quedado atrás sin tener que fiarse de la fecha, que
    muchas páginas mienten (se «actualizan» solo por tocar los anuncios).
    """
    vistas = re.findall(r'(?i)\bversion\s+(\d+\.\d+)', t)
    return max(vistas, key=lambda v: tuple(int(x) for x in v.split("."))) if vistas else ""


_ETIQUETAS_MOTOR = ("best w-engine", "w-engine")
_ETIQUETAS_ALT = ("alt. w-engines", "alt w-engines", "alternative w-engines")
_ETIQUETAS_DISCOS = ("drive disc build", "best drive disc", "drive discs")
_ETIQUETAS_STATS = ("disc main stats", "main stats", "main stat")


def _lista_numerada(celda: str) -> List[str]:
    """«1. Fusion Compiler 2. Electro-Lip Gloss» → los dos nombres.

    Game8 mete las alternativas en una sola celda numerada. Sin partirla, la
    build guardaría una cadena inútil de 80 caracteres en vez de una lista.
    """
    partes = re.split(r'\s*\d+\.\s*', celda)
    return [p.strip(" ·,") for p in partes if p.strip(" ·,")]


def _stats_por_ranura(celda: str) -> Dict[str, str]:
    """«6: ATK 5: Ice DMG 4: CRIT Rate» → {"6": "ATK", "5": "Ice DMG", ...}.

    Las ranuras 1-3 de ZZZ tienen stat fija; las que se eligen son la 4, 5 y 6,
    y son justo las que decide una build.
    """
    fuera: Dict[str, str] = {}
    trozos = re.findall(r'([456])\s*:\s*([^0-9]+?)(?=\s*[456]\s*:|$)', celda)
    for ranura, valor in trozos:
        v = valor.strip(" ·,/")
        if v:
            fuera[ranura] = v
    return fuera


def extraer_build(pagina: str, personaje: str = "", fuente: str = "") -> Build:
    """Página de personaje → `Build`. Determinista, sin modelo."""
    t = _limpiar_html(pagina)
    b = Build(personaje=personaje.strip(), fuente=fuente,
              obtenido=time.time(), version_juego=version_del_juego(t))

    for tabla in _tablas(t):
        filas = _filas(tabla)
        for i, fila in enumerate(filas):
            if len(fila) < 2:
                continue
            etiqueta, valor = fila[0].lower().strip(), fila[1]
            if not b.motor and any(etiqueta == e for e in _ETIQUETAS_MOTOR):
                b.motor = valor
            elif not b.motores_alt and any(etiqueta.startswith(e) for e in _ETIQUETAS_ALT):
                b.motores_alt = _lista_numerada(valor)
            elif not b.discos and any(etiqueta.startswith(e) for e in _ETIQUETAS_DISCOS):
                b.discos = valor
            elif not b.substats and etiqueta.startswith("disc substat"):
                b.substats = [x.strip() for x in re.split(r'[,·/]', valor) if x.strip()]
            elif not b.stats and any(etiqueta.startswith(e) for e in _ETIQUETAS_STATS):
                # Solo la ranura 6 vive en esta fila: Game8 pone la 5 y la 4 en
                # filas SUELTAS de una celda («5: Ice DMG»), sin etiqueta. Sin
                # leer las de continuación, una build sale con un tercio de sus
                # stats y nadie se entera — que es peor que salir vacía.
                crudo = " ".join(fila[1:])
                for siguiente in filas[i + 1:]:
                    if len(siguiente) == 1 and re.match(r'^[456]\s*:', siguiente[0]):
                        crudo += " " + siguiente[0]
                    else:
                        break
                b.stats = _stats_por_ranura(crudo)

    # Las alternativas de discos viven en su propia tabla, con valoración.
    for tabla in _tablas(t):
        filas = _filas(tabla)
        if not filas or "drive disc" not in " ".join(filas[0]).lower():
            continue
        for fila in filas[1:]:
            if len(fila) >= 2 and fila[0] and fila[0] != b.discos:
                b.discos_alt.append(fila[0])
        if not b.discos and b.discos_alt:
            b.discos, b.discos_alt = b.discos_alt[0], b.discos_alt[1:]

    b.equipos = extraer_equipos(pagina, fuente=fuente)
    return b


# Las especialidades como las escribe el juego («Attack») y como las escriben
# las guías al hablar de un puesto en el equipo («DPS»). Hacen falta las dos:
# la guía general usa DPS y las páginas de personaje usan Attack, así que con
# media lista los equipos de TODOS los personajes de ataque salían a cero.
_ROLES = {"attack", "dps", "stun", "support", "anomaly", "defense",
          "sub-dps", "sub dps", "rupture"}


def extraer_equipos(pagina: str, fuente: str = "") -> List[Equipo]:
    """Página → equipos recomendados.

    El patrón de Game8 es constante: un encabezado con el nombre del equipo y,
    debajo, una tabla cuya primera fila son los roles (DPS | Stun | Support) y
    la segunda los personajes. Se ancla en los ROLES, no en el orden de las
    tablas: hay 43 tablas en la página y la mayoría son de navegación.
    """
    t = _limpiar_html(pagina)
    encabezados = [(m.start(), _texto(m.group(1)))
                   for m in re.finditer(r'(?is)<h[23][^>]*>(.*?)</h[23]>', t)]
    equipos: List[Equipo] = []
    vistas: set = set()

    for m in re.finditer(r'(?is)<table.*?</table>', t):
        filas = _filas(m.group(0))
        if len(filas) < 2:
            continue
        roles = [c.strip() for c in filas[0]]
        if len(roles) < 3 or not all(r.lower() in _ROLES for r in roles):
            continue
        miembros = [c for c in filas[1] if c][:len(roles)]
        if len(miembros) < 3:
            continue
        # El encabezado más cercano por encima da nombre al equipo.
        previos = [txt for pos, txt in encabezados if pos < m.start() and txt]
        nombre = previos[-1] if previos else ""
        alt: List[str] = []
        for fila in filas[2:]:
            if fila and fila[0].lower().startswith("alternativ"):
                alt = [c for c in fila[1:] if c]
        equipos.append(Equipo(nombre=nombre, roles=roles, miembros=miembros,
                              alternativas=alt, fuente=fuente))
        vistas.add(m.start())

    # Patrón B — el de las páginas de personaje: cada celda trae «Nombre ─ Rol»
    # y, a la derecha, una columna de Bangboo. Es tan común como el patrón A y
    # sin él una página de personaje devuelve cero equipos.
    for m in re.finditer(r'(?is)<table.*?</table>', t):
        if m.start() in vistas:
            continue
        filas = _filas_partidas(m.group(0))
        if len(filas) < 2:
            continue
        cabecera = [" ".join(c) for c in filas[0]]
        hay_bangboo = bool(cabecera) and "bangboo" in cabecera[-1].lower()
        for fila in filas[1:]:
            agentes = [c for c in fila if len(c) >= 2 and c[1].lower() in _ROLES]
            if len(agentes) < 3:
                continue
            bangboo = ""
            if hay_bangboo and len(fila) > len(agentes):
                bangboo = fila[-1][0]
            nombre = cabecera[0] if cabecera else ""
            if not nombre or nombre.lower() in _ROLES:
                previos = [txt for pos, txt in encabezados if pos < m.start() and txt]
                nombre = previos[-1] if previos else ""
            equipos.append(Equipo(nombre=nombre,
                                  roles=[c[1] for c in agentes],
                                  miembros=[c[0] for c in agentes],
                                  bangboo=bangboo, fuente=fuente))
            break   # una tabla, un equipo: las filas siguientes son notas
    return equipos


# ──────────────────── de dónde salen los datos (la red) ────────────────────

# Sitios en los que se confía, por orden. La lista existe para NO tragarse el
# primer resultado del buscador: una consulta como «mejor build Miyabi» trae
# vídeos, foros y tiendas de cuentas, y ninguno se parsea. Fuera de esta lista
# no se descarga nada.
SITIOS = ("game8.co", "prydwen.gg")

# Lo que hay que tener en la cabeza en CUALQUIER pelea de ZZZ, en el orden en
# que hace falta. Es una lista fija y no algo que decida un modelo: son las
# fichas que se le pasan siempre, y si mañana el buscador o el LLM del día
# fallan, esto sigue diciendo lo mismo. [[feedback_funcionar_cualquier_modelo]]
# Van los dos idiomas porque el juego de Enzo está en español y la wiki en
# inglés: la ficha se guarda bajo los dos nombres.
MECANICAS_CLAVE = (
    "Dodge", "Esquiva", "Perfect Dodge", "Chain Attack", "Ataque en cadena",
    "Basic Attack", "Ataque básico", "Special Attack", "Ataque especial",
    "Assist", "Asistencia", "Defensive Assist", "Ultimate",
    "Habilidad definitiva", "Decibel Rating", "Stun", "Aturdimiento", "Daze",
    "Anomaly", "Anomalía",
)


def _searxng_url() -> str:
    """El buscador propio de Celestia. Es el mismo que usa `buscar_web`."""
    url = os.environ.get("CELESTIA_SEARXNG_URL", "").strip().rstrip("/")
    return url or "http://127.0.0.1:8888"


def _descargar(url: str, timeout: float = 25.0) -> str:
    """Baja una página con tope de tamaño. Cadena vacía si algo falla."""
    try:
        import urllib.request
        pet = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(pet, timeout=timeout) as r:
            crudo = r.read(TOPE_DESCARGA)
        return crudo.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("zzz: no se pudo descargar %s (%s)", url[:70], e)
        return ""


def _buscar(consulta: str, timeout: float = 20.0) -> List[Tuple[str, str]]:
    """Consulta → [(url, título)], solo de los sitios de confianza."""
    try:
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({"q": consulta, "format": "json"})
        pet = urllib.request.Request(f"{_searxng_url()}/search?{q}",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(pet, timeout=timeout) as r:
            datos = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except Exception as e:
        logger.warning("zzz: la búsqueda falló (%s)", e)
        return []
    fuera = []
    for r in datos.get("results", []):
        url = r.get("url", "")
        if any(s in url for s in SITIOS):
            fuera.append((url, r.get("title", "")))
    return fuera


# ─────────────────────── cómo se juega: el tutorial ───────────────────────

# Enzo, 8 sep 2026: «tiene que saber jugar, si no sabe cómo va que vea un
# tutorial». Un vídeo no le sirve —no puede verlo, y aunque pudiera, un tutorial
# de YouTube no le dice dónde están los botones en la pantalla de Enzo—, pero un
# tutorial **escrito** sí: le da las mecánicas y, sobre todo, **la señal visual**
# de cada una. Eso último es lo que enlaza con el `Vigia`: «esquiva cuando
# aparezca el destello» es exactamente una regla de «mira este trozo de pantalla
# y dispara cuando cambie».
#
# Y va aquí, en `zzz.py`, por la regla de este módulo: el meta lo pone la web y
# el criterio lo pone el código. Escribir de memoria cómo se juega a ZZZ sería
# el mismo error que escribir un catálogo de personajes.

# Las mecánicas que interesan. Se busca por palabra en los encabezados de la
# guía, no por posición: las páginas se reordenan cada parche.
# ⚠️ De MÁS específica a más genérica, y no es cosmético: un encabezado se
# asigna a la primera que casa, y «Use a Basic Attack After Performing a Perfect
# Dodge» contiene «dodge». Con «esquiva» arriba, ese titular se lo quedaba ella
# y las otras dos mecánicas desaparecían de la lista (visto al probarlo contra
# la guía real). «dodge» a secas va la última.
_MECANICAS = (
    ("contraataque", ("dodge counter", "counter attack", "counterattack")),
    ("parada", ("parry", "perfect assist", "defensive assist")),
    ("cadena", ("chain attack",)),
    ("especial", ("ex special", "special attack")),
    ("basico", ("basic attack",)),
    ("relevo", ("quick assist", "switch", "swap")),
    ("definitiva", ("ultimate", "decibel")),
    ("aturdir", ("daze", "stun")),
    ("esquiva", ("dodge",)),
)

# Un encabezado y lo que va debajo, hasta el siguiente encabezado.
_SECCION_RE = re.compile(
    r"<h([2-4])[^>]*>(.{3,140}?)</h\1>(.{0,2500}?)(?=<h[2-4]|\Z)", re.S | re.I)

# Frases que en estas guías son relleno de web, no tutorial.
_RELLENO = ("related guide", "comment", "author", "premium", "ranking",
            "gaming news", "popular games", "all rights reserved", "tier list",
            "redeem code", "free member", "site interface", "game tools",
            "walkthrough wiki", "latest news")


def extraer_mecanicas(pagina: str, fuente: str = "") -> List[Tuple[str, str]]:
    """De una guía a pares (mecánica, cómo se hace). Regex, sin modelo.

    Lo que se busca son **encabezados**, porque en estas guías el encabezado ES
    la instrucción: «Dodge When the Flashing Light Appears», «Use a Basic Attack
    After Performing a Perfect Dodge». Eso ya es el tutorial; el párrafo de
    debajo casi siempre lo repite más largo.

    Una entrada por mecánica, y **no la primera: la que enseña algo**. La
    primera suele ser el título del apartado —«How to Dodge in ZZZ»—, que no
    dice cómo se hace; la buena viene después y trae la condición o la señal:
    «Dodge When the Flashing Light Appears». Quedarse con la primera daba una
    lista de titulares inútiles, que es lo que salió al probarlo contra la guía
    de verdad.
    """
    candidatos: Dict[str, List[str]] = {}
    for m in _SECCION_RE.finditer(pagina):
        titulo = _texto(m.group(2)).strip()
        if not titulo or len(titulo) > 120:
            continue
        bajo = titulo.lower()
        if any(r in bajo for r in _RELLENO):
            continue
        for nombre, claves in _MECANICAS:
            if not any(c in bajo for c in claves):
                continue
            # El encabezado es la instrucción; el cuerpo sólo se usa si el
            # encabezado es un título pelado («Dodge») que no explica nada.
            texto = titulo
            if len(titulo.split()) <= 2:
                cuerpo = _texto(m.group(3)).strip()
                frase = re.split(r"(?<=[.!?])\s", cuerpo)[0] if cuerpo else ""
                if 10 < len(frase) < 220:
                    texto = f"{titulo}: {frase}"
            candidatos.setdefault(nombre, []).append(texto)
            break
    return [(nombre, _la_que_enseña(candidatos[nombre]))
            for nombre, _claves in _MECANICAS if nombre in candidatos]


# Palabras que convierten un titular en una instrucción: dicen CUÁNDO o CÓMO.
_ENSEÑA = ("when", "after", "press", "appears", "hold", "tap", "use ", "while",
           "before", "during", "flashing", "light", "button")


# Dónde escriben las guías cómo se juega un equipo. Se buscan estos rótulos y
# se lee lo que viene debajo: es la misma forma que ya funciona para las
# mecánicas, donde el encabezado ES la instrucción.
# ⚠️ Sin exigir que vaya en un <h2>: en la guía de Game8 de un equipo, «How to
# Play» aparece cinco veces y NINGUNA es un encabezado (medido el 10 sep 2026,
# que es cuando esto devolvía cero). Se busca el rótulo donde esté y se lee lo
# que viene detrás.
_ROTACION_RE = re.compile(
    r"(?is)(rotation|how to play|playstyle|team combo)(.{80,2500}?)"
    r"(?=rotation|how to play|\Z)")


def _extraer_rotacion(pagina: str, personajes: Sequence[str]) -> str:
    """El trozo de la guía que explica el orden de juego. "" si no lo dice.

    Se exige que nombre a alguno del equipo: una sección «Rotation» de otra
    página distinta explicaría el orden de otros personajes, y eso es peor que
    no tener nada — parece conocimiento y manda mal.
    """
    t = _texto(_limpiar_html(pagina))
    nombres = {normalizar(p) for p in personajes if p}
    for m in _ROTACION_RE.finditer(t):
        limpio = " ".join(m.group(2).split())
        if len(limpio) < 60:
            continue
        if nombres and not any(n in normalizar(limpio) for n in nombres):
            continue
        # Y que hable de PELEAR, no del índice de la página. El primer intento
        # devolvió «Related Articles · Miyabi Builds · List of Contents…»: casa
        # con el rótulo y no dice nada de cómo se juega.
        if not _suena_a_rotacion(limpio):
            continue
        return limpio[:600]
    return ""


# Lo que tiene que nombrar un texto para ser una rotación y no un índice: las
# piezas con las que se juega un turno en este juego.
_DE_COMBATE = ("chain attack", "swap", "ex special", "ultimate", "stun",
               "dodge", "assist", "anomaly", "daze", "basic attack", "cadena",
               "definitiva", "esquiva", "aturd")


def _suena_a_rotacion(texto: str) -> bool:
    bajo = texto.lower()
    return sum(1 for t in _DE_COMBATE if t in bajo) >= 2


def _la_que_enseña(textos: Sequence[str]) -> str:
    """De varios encabezados de la misma mecánica, el que instruye.

    «How to Dodge in ZZZ» y «Dodge When the Flashing Light Appears» hablan de
    lo mismo; sólo el segundo sirve para jugar. El criterio es determinista:
    gana el que trae una condición o una señal, y a igualdad, el más largo
    —que en estas guías es el más concreto.
    """
    def nota(t: str) -> Tuple[int, int]:
        bajo = t.lower()
        return (sum(1 for p in _ENSEÑA if p in bajo), len(t))
    return max(textos, key=nota)


# ────────────────────────────── el saber ──────────────────────────────

class SaberZZZ:
    """Lo que Celestia sabe de ZZZ: fresco de la web, guardado con fecha.

    Todo lo que toca el mundo exterior (buscar, descargar, el reloj) entra por
    el constructor. No es ceremonia: es lo que permite que los tests corran sin
    red y sin esperar, y que una fuente caída se pueda imitar para comprobar
    que el aviso sale de verdad.
    """

    def __init__(self, cache: Optional[str] = None,
                 buscar: Optional[Callable[[str], List[Tuple[str, str]]]] = None,
                 descargar: Optional[Callable[[str], str]] = None,
                 ahora: Optional[Callable[[], float]] = None):
        raiz = Path(cache) if cache else MEM_DIR / "zzz"
        self.cache = raiz
        self._buscar = buscar or _buscar
        self._descargar = descargar or _descargar
        self._ahora = ahora or time.time

    # -- caché en disco --------------------------------------------------
    def _fichero(self, clave: str) -> Path:
        """Clave → nombre de fichero, CONSERVANDO el prefijo.

        `normalizar` tira los guiones bajos (solo deja letras, dígitos y
        espacios), así que «build_miyabi» acababa en «buildmiyabi.json» y el
        `glob("build_*.json")` de `todos_los_equipos` no encontraba nunca nada.
        Peor: el test que comprobaba que una build vacía NO se guarda pasaba
        por este motivo y no por el suyo. Se normaliza la parte variable y el
        guión bajo se repone después.
        """
        seguro = re.sub(r'\s+', '_', normalizar(clave.replace("_", " "))) or "_"
        return self.cache / f"{seguro}.json"

    def _leer_cache(self, clave: str) -> Optional[dict]:
        f = self._fichero(clave)
        try:
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("zzz: caché ilegible %s (%s)", f.name, e)
        return None

    def _guardar_cache(self, clave: str, datos: dict) -> None:
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            self._fichero(clave).write_text(
                json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning("zzz: no se pudo guardar la caché (%s)", e)

    def _fresco(self, datos: Optional[dict]) -> bool:
        if not datos:
            return False
        edad = (self._ahora() - float(datos.get("obtenido") or 0)) / 86400
        return edad < DIAS_FRESCURA

    # -- consultas -------------------------------------------------------
    def build(self, personaje: str, refrescar: bool = False) -> Build:
        """La build de un personaje. De la caché si está fresca, si no, de la web."""
        clave = f"build_{normalizar(personaje)}"
        guardado = self._leer_cache(clave)
        if not refrescar and self._fresco(guardado):
            return _build_desde_dict(guardado)

        for url, titulo in self._buscar(f"{personaje} best build W-Engine drive discs ZZZ"):
            if normalizar(personaje).split()[-1] not in normalizar(titulo):
                continue    # el título ni menciona al personaje: no es su página
            pagina = self._descargar(url)
            if not pagina:
                continue
            b = extraer_build(pagina, personaje, url)
            b.obtenido = self._ahora()
            if not b.vacia():
                self._guardar_cache(clave, asdict(b))
                return b
            logger.warning("zzz: %s no soltó ninguna build (¿cambió el formato?)", url[:70])

        # Nada nuevo: mejor la caché vieja que nada, pero DICIENDO que es vieja.
        if guardado:
            return _build_desde_dict(guardado)
        return Build(personaje=personaje, obtenido=0.0)

    def equipos(self, refrescar: bool = False) -> List[Equipo]:
        """Los equipos del meta, de la guía general."""
        guardado = self._leer_cache("equipos")
        if not refrescar and self._fresco(guardado):
            return [Equipo(**e) for e in guardado.get("equipos", [])]

        for url, _t in self._buscar("ZZZ best team comps Zenless Zone Zero"):
            pagina = self._descargar(url)
            if not pagina:
                continue
            eq = extraer_equipos(pagina, url)
            if eq:
                self._guardar_cache("equipos", {"obtenido": self._ahora(),
                                                "fuente": url,
                                                "version_juego": version_del_juego(pagina),
                                                "equipos": [asdict(e) for e in eq]})
                return eq
        return [Equipo(**e) for e in (guardado or {}).get("equipos", [])]

    def como_se_juega(self, refrescar: bool = False) -> List[Tuple[str, str]]:
        """El tutorial: qué mecánicas hay y con qué señal se disparan.

        Enzo, 8 sep 2026: «tiene que saber jugar, si no sabe cómo va que vea un
        tutorial». Esto es ese tutorial, y viene de la web por la misma razón
        que el meta: lo que yo recuerde del juego está desfasado.

        **A diferencia del meta, esto no caduca cada parche** —esquivar cuando
        destella se hace igual desde que salió el juego—, pero se refresca con
        el mismo criterio para no tener dos reglas distintas en el módulo.

        Se juntan VARIAS fuentes a propósito: cada guía explica bien dos o tres
        mecánicas y de pasada las demás, así que con una sola quedan huecos.
        """
        guardado = self._leer_cache("como_se_juega")
        if not refrescar and self._fresco(guardado):
            return [(m["mecanica"], m["como"]) for m in guardado.get("mecanicas", [])]

        juntas: Dict[str, str] = {}
        fuentes: List[str] = []
        for url, _t in self._buscar(
                "Zenless Zone Zero combat guide dodge counter chain attack")[:3]:
            pagina = self._descargar(url)
            if not pagina:
                continue
            nuevas = extraer_mecanicas(pagina, url)
            if nuevas:
                fuentes.append(url)
            for nombre, texto in nuevas:
                # La primera fuente que explique una mecánica se queda con
                # ella: si no, la última en llegar pisaría a la mejor.
                juntas.setdefault(nombre, texto)
        if not juntas:
            # Una extracción vacía NO se guarda: envenenaría la caché una
            # semana entera. Es la misma regla que en el resto del módulo.
            return [(m["mecanica"], m["como"])
                    for m in (guardado or {}).get("mecanicas", [])]
        self._guardar_cache("como_se_juega", {
            "obtenido": self._ahora(), "fuentes": fuentes,
            "mecanicas": [{"mecanica": k, "como": v} for k, v in juntas.items()]})
        return list(juntas.items())

    def resumen_para_jugar(self, tope: int = 6) -> str:
        """Lo aprendido, en las líneas que caben en el prompt del jugador.

        Va a un prompt que ya lleva una imagen de 1.024 tokens, así que aquí
        sobra cualquier adorno: una línea por mecánica y la señal que la
        dispara, que es lo único que se puede usar mientras se juega.
        """
        mecanicas = self.como_se_juega()
        if not mecanicas:
            return ""
        lineas = [f"- {nombre}: {como}" for nombre, como in mecanicas[:tope]]
        return "Lo que he leído de cómo se juega a este juego:\n" + "\n".join(lineas)

    def como_se_usa(self, personajes: Sequence[str],
                    refrescar: bool = False) -> str:
        """Cómo se JUEGA ese equipo: el orden en que se usa a cada uno.

        Enzo, 10 sep 2026: «tiene que saber qué personajes tiene, qué equipo
        lleva y cómo se usa ese equipo, combinaciones de ese equipo».

        Saber quiénes van juntos —lo que ya hace `equipos()`— no es saber
        jugarlos: un equipo de ZZZ es un ORDEN. Primero el que aturde, luego el
        de daño mientras el enemigo está aturdido, y los relevos entre medias.
        Eso está escrito en las guías bajo «Rotation» o «How to Play», y es lo
        que se trae aquí.
        """
        if not personajes:
            return ""
        clave = "rotacion_" + re.sub(r"[^a-z0-9]+", "_",
                                     "_".join(sorted(normalizar(p) for p in personajes)))[:60]
        guardado = self._leer_cache(clave)
        if not refrescar and self._fresco(guardado):
            return guardado.get("rotacion", "")

        consulta = ("Zenless Zone Zero " + " ".join(personajes[:3]) +
                    " team rotation how to play")
        rotacion = ""
        for url, _t in self._buscar(consulta)[:3]:
            pagina = self._descargar(url)
            if not pagina:
                continue
            rotacion = _extraer_rotacion(pagina, personajes)
            if rotacion:
                break
        self._guardar_cache(clave, {"rotacion": rotacion})
        return rotacion

    def mejor_equipo(self, mios: Sequence[str]) -> Optional[Tuple[Equipo, List[str]]]:
        """El mejor equipo que se puede montar con los personajes que SE TIENEN.

        Enzo, 10 sep 2026: «sigo esperando que ella seleccione los personajes y
        haga el equipo Celestia; con toda la información que ha aprendido
        debería saber cuáles personajes van con quién».

        Y sí: de la guía salen 35 equipos con sus roles. Lo que ninguna wiki
        puede saber es **cuáles tiene él**, porque eso está en su cuenta y sólo
        se ve leyendo la pantalla de Agentes. Por eso esto recibe la lista de
        fuera en vez de inventársela.

        Devuelve (equipo, los que faltan). Gana el que menos falte, y a
        igualdad, el que antes aparezca en la guía — que es el orden en que la
        fuente los recomienda.
        """
        tengo = {normalizar(n) for n in mios if n}
        if not tengo:
            return None
        mejor: Optional[Tuple[Equipo, List[str]]] = None
        for equipo in self.equipos():
            if not equipo.miembros:
                continue
            faltan = [m for m in equipo.miembros if normalizar(m) not in tengo]
            if mejor is None or len(faltan) < len(mejor[1]):
                mejor = (equipo, faltan)
            if not faltan:
                break
        return mejor

    def personajes_del_meta(self) -> List[str]:
        """Los nombres que YA tengo leídos, sin salir a la red.

        Lo usa el detector para decidir si «quién es kira» va al meta del juego
        o sigue su camino como una pregunta normal, y un detector no puede
        quedarse esperando a una descarga: si no hay nada guardado, la
        respuesta honesta es «no conozco a nadie» y la frase pasa de largo.
        """
        guardado = self._leer_cache("equipos") or {}
        nombres: List[str] = []
        for e in guardado.get("equipos", []) or []:
            nombres += list(e.get("miembros") or []) + list(e.get("alternativas") or [])
        try:
            for f in sorted(self.cache.glob("build_*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("personaje"):
                    nombres.append(d["personaje"])
        except Exception:
            pass
        return sorted({n.strip() for n in nombres if n and n.strip()})

    def todos_los_equipos(self) -> List[Equipo]:
        """La guía general MÁS los equipos de cada build ya consultada.

        Los equipos de la página de un personaje no salen en la guía general, y
        son justo los más útiles: son los que ese personaje puede formar. Sin
        unirlos, a Enzo se le decía «ninguno completo» teniendo en la caché un
        equipo que puede montar entero. Se descartan los repetidos por sus
        miembros, que el mismo equipo aparece en las dos fuentes.
        """
        vistos: Dict[Tuple[str, ...], Equipo] = {}
        for e in self.equipos():
            vistos.setdefault(tuple(sorted(normalizar(m) for m in e.miembros)), e)
        try:
            ficheros = sorted(self.cache.glob("build_*.json"))
        except Exception:
            ficheros = []
        for f in ficheros:
            try:
                datos = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for d in datos.get("equipos") or []:
                try:
                    e = Equipo(**d)
                except TypeError:
                    continue
                vistos.setdefault(tuple(sorted(normalizar(m) for m in e.miembros)), e)
        return list(vistos.values())

    # -- el criterio propio ----------------------------------------------
    def montables(self, inventario: Sequence[str],
                  equipos: Optional[Sequence[Equipo]] = None) -> Dict[str, Any]:
        """Cruza el meta con lo que Enzo TIENE. Aquí no se consulta a nadie.

        Devuelve tres cosas, que son las tres preguntas reales de un jugador:
        qué puedo montar ya, a qué equipo le falta solo uno, y a quién me
        conviene tirar — que es el personaje que desbloquea más equipos.
        """
        tengo = {normalizar(p) for p in inventario if p and p.strip()}
        eq = list(equipos) if equipos is not None else self.todos_los_equipos()

        listos, a_uno, lejos = [], [], []
        falta_cuenta: Dict[str, int] = {}
        nombre_real: Dict[str, str] = {}

        for e in eq:
            faltan = [m for m in e.miembros if normalizar(m) not in tengo]
            if not faltan:
                listos.append(e)
            elif len(faltan) == 1:
                a_uno.append((e, faltan[0]))
            else:
                lejos.append((e, faltan))
            for m in faltan:
                clave = normalizar(m)
                falta_cuenta[clave] = falta_cuenta.get(clave, 0) + 1
                nombre_real.setdefault(clave, m)

        # A quién tirar: el que aparece en más equipos que hoy no puedes montar.
        deseados = sorted(falta_cuenta.items(), key=lambda kv: -kv[1])
        return {
            "listos": listos,
            "a_uno": a_uno,
            "lejos": lejos,
            "mas_deseados": [(nombre_real[k], n) for k, n in deseados[:5]],
            "total": len(eq),
        }

    # -- lo que Enzo tiene ------------------------------------------------
    def inventario(self) -> List[str]:
        """Los personajes de Enzo. Vacío mientras no los diga o se lean."""
        return list((self._leer_cache("inventario") or {}).get("personajes", []))

    def guardar_inventario(self, personajes: Sequence[str]) -> List[str]:
        """Guarda la lista SIN duplicados y sin perder los que ya había."""
        actual = {normalizar(p): p for p in self.inventario()}
        for p in personajes:
            p = (p or "").strip(" ,.;")
            if p:
                actual[normalizar(p)] = p
        lista = sorted(actual.values(), key=str.lower)
        self._guardar_cache("inventario", {"obtenido": self._ahora(), "personajes": lista})
        return lista

    # -- honestidad ------------------------------------------------------
    def salud(self) -> str:
        """¿Sigue funcionando la extracción? Se responde MIRANDO, no suponiendo."""
        eq = self.equipos()
        if not eq:
            return ("⚠ No tengo equipos: la fuente no responde o cambió de formato. "
                    "No me inventaré un meta que no he podido leer.")
        guardado = self._leer_cache("equipos") or {}
        dias = (self._ahora() - float(guardado.get("obtenido") or 0)) / 86400
        ver = guardado.get("version_juego") or "?"
        return (f"{len(eq)} equipos leídos de {guardado.get('fuente', 'la web')} "
                f"(versión {ver}, hace {dias:.0f} días).")


def _build_desde_dict(d: dict) -> Build:
    """Reconstruye una Build de la caché sin que un campo nuevo la rompa."""
    d = dict(d)
    equipos = [Equipo(**e) for e in d.pop("equipos", []) or []]
    validos = {f for f in Build.__dataclass_fields__ if f != "equipos"}
    return Build(equipos=equipos, **{k: v for k, v in d.items() if k in validos})


# ──────────────────────── de consulta a respuesta ────────────────────────

_RE_TENGO = re.compile(
    r"(?i)\b(?:tengo|desbloque[eé]|consegu[ií]|saqu[eé]|poseo|mis\s+personajes\s+son)\b"
    r"[:\s]*(?P<lista>.+)$")
_RE_BUILD = re.compile(
    r"(?i)\b(?:build|construcci[oó]n|equipar|equipo\s+de|motor|w-?engine|discos?|"
    r"c[oó]mo\s+(?:monto|subo|mejoro))\b")
# Preguntar por los equipos del meta sin nombrar a nadie. Va aparte de
# _RE_BUILD porque son dos preguntas distintas: «con qué equipo a Miyabi» es
# una build, «cuáles son los mejores equipos» es el meta entero.
_RE_EQUIPOS = re.compile(
    r"(?i)\b(?:equipos?|team|teams|comps?|composici[oó]n|composiciones|"
    r"combinaciones?)\b")
# «¿Sabes jugar al zzz?», «¿qué sabes del zzz?», «¿lo has jugado?».
#
# 🔴 18 sep 2026, el último chat real de Enzo con ella: preguntó «Sabes jugar al
# zzz?» y contestó primero que sí y dos turnos después «no tengo información
# sobre cómo se juega al ZZZ en mis datos actuales» — teniendo en disco 20 guías
# de personajes, los combos medidos, el equipo montado y un jugador entero. No
# fallaba el saber: fallaba que nadie preguntaba al disco, así que contestaba el
# modelo de memoria, y su memoria de este juego está vieja. Preguntar qué sabe
# es una consulta como cualquier otra, y la respuesta está guardada.
_RE_QUE_SE = re.compile(
    r"(?i)\b(?:sabes|sabr[ií]as|sabe|conoces|conoce|entiendes|dominas|controlas|"
    r"has\s+jugado|jugaste|te\s+sabes|qu[eé]\s+sabes|qu[eé]\s+tal\s+se\s+te\s+da)\b")
_RE_MONTABLE = re.compile(
    r"(?i)\b(?:qu[eé]\s+(?:equipos?|equipo)\s+(?:puedo|podr[ií]a)|puedo\s+montar|"
    r"con\s+lo\s+que\s+tengo|a\s+qui[eé]n\s+(?:tiro|invoco|saco)|"
    r"por\s+qui[eé]n\s+tiro|qu[eé]\s+me\s+(?:falta|conviene))\b")


def _partir_lista(texto: str) -> List[str]:
    """«a Miyabi, Anby y Soukaku» → los tres nombres, sin la preposición.

    El «a» de «tengo A Miyabi» se pegaba al primer nombre y lo guardaba como
    «a Miyabi», que no casa con nada: el equipo salía como «te falta Miyabi»
    teniendo a Miyabi. Un fallo mudo y muy tonto, de los que solo se ven
    ejecutando la frase entera de verdad.
    """
    trozos = re.split(r'\s*(?:,|;|\by\b|\be\b|\+|/)\s*', texto, flags=re.I)
    limpios = []
    for t in trozos:
        t = re.sub(r'(?i)^(?:a|al|a\s+la|el|la|los|las|un[oa]?)\s+', '', t.strip(" .¿?¡!"))
        t = t.strip(" .¿?¡!")
        if t:
            limpios.append(t)
    return limpios


def responder(consulta: str, saber: Optional[SaberZZZ] = None) -> str:
    """Consulta en español → respuesta con fuente y fecha. Sin modelo.

    El enrutado es de expresiones regulares a propósito: es la misma regla que
    el resto de Celestia sigue desde la S37 —una función que solo acierta
    cuando el LLM está fino no es una función— y aquí importa el doble, porque
    lo que se responde son datos de una web, con su fecha. Redondear eso o
    reescribirlo es justo lo que no puede pasar.
    """
    s = saber or SaberZZZ()
    consulta = (consulta or "").strip()
    if not consulta:
        return "Dime qué quieres saber de ZZZ: una build, un equipo, o qué puedes montar."

    # 1. «Tengo a Miyabi, Anby y Soukaku» — se apunta y se contesta con ello.
    m = _RE_TENGO.search(consulta)
    if m:
        lista = s.guardar_inventario(_partir_lista(m.group("lista")))
        resumen = _resumen_montables(s, lista)
        return f"Apuntado: {len(lista)} personajes.\n\n{resumen}"

    # 2. «¿Qué puedo montar?» / «¿a quién tiro?» — necesita saber qué tiene.
    if _RE_MONTABLE.search(consulta):
        inv = s.inventario()
        if not inv:
            return ("Todavía no sé qué personajes tienes. Dímelos y lo calculo: "
                    "«tengo a Miyabi, Anby y Soukaku…».")
        return _resumen_montables(s, inv)

    # 3. Build de un personaje concreto.
    nombre = _personaje_de(consulta, s)
    if nombre and _RE_BUILD.search(consulta):
        return _resumen_build(s.build(nombre))

    # 4. Un nombre a secas: sus equipos, que es lo que casi siempre se pregunta.
    if nombre:
        b = s.build(nombre)
        if b.vacia():
            return (f"No he podido leer nada fiable sobre {nombre}. "
                    f"{s.salud()}")
        return _resumen_build(b)

    # 5. «Los mejores equipos», sin personaje de por medio. Enzo lo preguntó
    #    dos veces seguidas («Dime los mejores equipos…», «Pero como serian los
    #    equipos») y las dos cayeron en el «no he pillado» de abajo — o sea que
    #    contestó el modelo, de memoria y sin fuente, con equipos de CUATRO
    #    cuando en ZZZ son de tres. Tuvo que corregirla él.
    if _RE_EQUIPOS.search(consulta):
        return _resumen_equipos(s)

    # 6. «¿Sabes jugar a esto?» — lo que hay en disco, no lo que recuerde el modelo.
    if _RE_QUE_SE.search(consulta):
        return _lo_que_se_del_juego(s)

    return ("No he pillado de qué personaje hablas. Prueba con «la mejor build de "
            "Miyabi», «equipos con Ellen» o «¿qué puedo montar?».")


def _lo_que_se_del_juego(s: Optional[SaberZZZ] = None) -> str:
    """Qué sé de ZZZ, contando lo que hay en disco. Sin modelo y sin adornos.

    Todo lo que dice sale de un fichero que se cuenta en el momento: si mañana
    hay más guías, lo dirá; si se borran, también. Es lo contrario de una lista
    escrita a mano en el system prompt, que envejece sola y miente en cuanto
    cambia algo — y Enzo lleva dos podas pidiendo que el prompt no engorde.
    """
    raiz = MEM_DIR
    partes: List[str] = []

    def cuantos(carpeta: str, patron: str) -> int:
        try:
            return len(list((raiz / carpeta).glob(patron)))
        except OSError:
            return 0

    guias = cuantos("zzz", "guia_*.json")
    if guias:
        partes.append(f"guías de {guias} personajes (build, motor, discos y equipos)")

    equipo = []
    try:
        equipo = json.loads((raiz / "jugador/equipo_zzz.json").read_text("utf-8")).get("equipo", [])
    except (OSError, ValueError):
        pass
    if equipo:
        partes.append("el equipo que tienes montado: " + ", ".join(equipo))

    combos = {}
    try:
        combos = json.loads((raiz / "jugador/combos_zzz.json").read_text("utf-8"))
    except (OSError, ValueError):
        pass
    if combos:
        n = sum(len(v) for v in combos.values())
        partes.append(f"{n} combos medidos de {len(combos)} personajes, con sus tiempos")

    pantallas = cuantos("jugador", "pantallas_zzz.json")
    if pantallas:
        partes.append("las pantallas del juego, aprendidas a base de mirarlas")

    if not partes:
        return ("De ZZZ no tengo nada guardado todavía. Puedo buscarlo: pregúntame "
                "por una build («la mejor build de Miyabi») o por los equipos.")

    cabeza = "Sí, y te digo exactamente qué tengo guardado de ZZZ:"
    cuerpo = "\n".join(f"· {p}" for p in partes)
    cola = ("\n\nY jugarlo lo juego yo en tu móvil: dime «juega al zzz» y me pongo. "
            "Lo que se me da mal te lo digo sin adornos — todavía hago poco daño "
            "por partida, y lo estoy midiendo.")
    return f"{cabeza}\n{cuerpo}{cola}"


def _personaje_de(consulta: str, s: SaberZZZ) -> str:
    """Saca el personaje de la frase cotejándolo con los del meta.

    Cotejar contra la lista real evita dos cosas: inventarse un personaje que
    no existe, y quedarse con «la» o «mejor» como si fueran nombres.
    """
    conocidos: Dict[str, str] = {}
    for e in s.equipos():
        for m in list(e.miembros) + list(e.alternativas):
            conocidos.setdefault(normalizar(m), m)
    plano = normalizar(consulta)
    # El más largo primero: «Ye Shunguang» antes que «Ye».
    for clave in sorted(conocidos, key=len, reverse=True):
        if re.search(rf'\b{re.escape(clave)}\b', plano):
            return conocidos[clave]
    return ""


def _resumen_build(b: Build) -> str:
    """La build, en el formato en que se lee de un vistazo en el móvil."""
    if b.vacia():
        return f"No tengo build de {b.personaje}: no pude leer la fuente."
    lineas = [f"⚔️ {b.personaje}"]
    if b.motor:
        lineas.append(f"Motor: {b.motor}")
    if b.motores_alt:
        lineas.append(f"  alternativas: {', '.join(b.motores_alt[:3])}")
    if b.discos:
        lineas.append(f"Discos: {b.discos}")
    if b.stats:
        orden = [f"{k}: {b.stats[k]}" for k in ("4", "5", "6") if k in b.stats]
        lineas.append(f"Stats principales: {' · '.join(orden)}")
    if b.substats:
        lineas.append(f"Substats por orden: {', '.join(b.substats)}")
    for e in b.equipos[:3]:
        gente = " · ".join(f"{m} ({r})" for m, r in zip(e.miembros, e.roles)) \
            if e.roles else " · ".join(e.miembros)
        lineas.append(f"• {e.nombre or 'Equipo'}: {gente}"
                      + (f" + {e.bangboo}" if e.bangboo else ""))
    lineas.append(_pie(b.fuente, b.dias(), b.version_juego))
    return "\n".join(lineas)


def _resumen_montables(s: SaberZZZ, inventario: Sequence[str]) -> str:
    m = s.montables(inventario)
    if not m["total"]:
        return s.salud()
    lineas = [f"Con tus {len(inventario)} personajes, de {m['total']} equipos del meta:"]
    if m["listos"]:
        lineas.append(f"✅ Puedes montar {len(m['listos'])} ya:")
        for e in m["listos"][:5]:
            lineas.append(f"   • {e.nombre or 'Equipo'}: {' · '.join(e.miembros)}")
    else:
        lineas.append("❌ Ninguno completo todavía.")
    if m["a_uno"]:
        lineas.append(f"🔸 A un personaje de {len(m['a_uno'])}:")
        for e, falta in m["a_uno"][:5]:
            lineas.append(f"   • {e.nombre or 'Equipo'} — te falta {falta}")
    if m["mas_deseados"]:
        lineas.append("🎯 Quien más te desbloquea: " +
                      ", ".join(f"{n} ({c} equipos)" for n, c in m["mas_deseados"][:3]))
    return "\n".join(lineas)


def _resumen_equipos(s: SaberZZZ) -> str:
    """Los mejores equipos del meta, con su fuente y su fecha.

    Se enseñan los completos y nada más: un equipo del que solo se leyeron dos
    miembros no es un equipo, y publicarlo a medias invita justo a lo que esto
    viene a evitar — que alguien rellene el hueco de memoria.
    """
    eq = [e for e in s.equipos() if e.completo()]
    if not eq:
        return s.salud()
    lineas = [f"Los mejores equipos del meta ahora mismo (son de 3):"]
    for e in eq[:6]:
        roles = f"  [{' · '.join(e.roles)}]" if e.roles else ""
        lineas.append(f"• {e.nombre or 'Equipo'}: {' · '.join(e.miembros)}{roles}")
        if e.bangboo:
            lineas.append(f"    Bangboo: {e.bangboo}")
        if e.alternativas:
            lineas.append(f"    alternativas: {', '.join(e.alternativas[:3])}")
    lineas.append(f"— {s.salud()}")
    return "\n".join(lineas)


def _pie(fuente: str, dias: float, version: str) -> str:
    """De dónde salió y de cuándo. Siempre, porque un consejo de ZZZ caduca."""
    sitio = re.sub(r'^https?://(www\.)?', '', fuente or "").split("/")[0] or "la web"
    cuando = "hoy" if dias < 1 else f"hace {dias:.0f} días"
    ver = f", versión {version}" if version else ""
    return f"— {sitio}, leído {cuando}{ver}"
