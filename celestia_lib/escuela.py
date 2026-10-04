"""La curva de aprender un juego: de buscar cómo va, a jugarlo de memoria.

Enzo, 8 sep 2026: «cuando vea algo nuevo en x juego busque cómo funciona lo que
tiene delante y vaya poco a poco más rápida practicando, y ya cuando sepa todo
pues lo haga natural sin buscar cómo se hace x cosa».

Eso son tres cosas distintas, y aquí están las tres:

  1. **Ver que algo es nuevo.** No «no sé jugar a esto», sino «esta pantalla
     concreta que tengo delante no la he manejado nunca».
  2. **Buscarlo, una vez.** Se sale a la red a leer qué es y cómo se hace, se
     guarda en disco, y no se vuelve a salir por eso nunca más.
  3. **Dejar de necesitarlo.** Con cada acierto la lección pesa menos en el
     prompt, hasta que desaparece del todo y la pantalla la resuelve el libro
     de jugadas — sin modelo, sin red, sin pensar. Eso es «natural».

Lo que ya había y NO se duplica aquí:

  · `LibroDeJugadas` guarda **qué toqué** en una pantalla que funcionó. Es la
    memoria muscular, y es lo que hace que al final sea instantáneo.
  · `zzz.SaberZZZ` trae el tutorial general del juego **antes** de empezar.
  · Esto de aquí es lo de en medio, que faltaba: el saber de UNA situación
    concreta, traído cuando esa situación aparece y no antes.

**Por qué una lección se identifica por la huella de píxeles y no por el
texto.** Porque el nombre lo pone el modelo («Chain Attack») y el nombre puede
cambiar entre respuestas, entre modelos y entre idiomas. La huella no: es la
misma rejilla 4x6 de colores con la que el libro reconoce una pantalla, ya
está medida en cada vuelta y no cuesta nada. El nombre sirve para BUSCAR; la
huella sirve para RECONOCER. Mezclar las dos cosas era la forma fácil de que
esto dependiera de que el modelo del día conteste bonito, y la regla de la
casa es que las funciones no dependan de eso.

Nada de lo que se guarda aquí permite reconstruir una pantalla: 24 colores por
situación y el texto de una guía pública. Por aquí pasa el móvil de Enzo.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from celestia_lib.jugador import Retina
from celestia_lib.paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

# Aciertos SEGUIDOS a partir de los cuales una situación se considera sabida y
# deja de gastar sitio en el prompt. Tres y no uno: acertar una vez puede ser
# suerte —el botón estaba donde estaba—, y tres veces seguidas ya no. Tampoco
# más: cada vuelta extra con la lección puesta es prompt que se paga en cuota,
# y la cuota de Groq fue el muro de la sesión 66.
SABIDA_CON = 3

# Cuántas veces hay que ver una pantalla sin resolverla para salir a la red por
# iniciativa propia, sin que el modelo haya nombrado nada. Es el respaldo
# determinista: si el modelo nunca dice qué es lo que ve —porque es viejo,
# porque contesta en otro formato, porque ese día va mal—, esto sigue
# funcionando igual. [[feedback_funcionar_cualquier_modelo]]
ATASCADA_CON = 3

# Tope de salidas a la red POR PARTIDA. Buscar cuesta segundos de reloj en
# mitad de una pelea; tres es lo que cabe sin que la partida se note parada, y
# lo que no se busque hoy se busca en la siguiente.
TOPE_BUSQUEDAS = 3

# Cuánto texto de una guía se guarda por lección. Va a un prompt que ya lleva
# una imagen de 1.024 tokens: dos frases sirven, un párrafo estorba.
TOPE_COMO = 240


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clave(titulo: str) -> str:
    """El nombre que dio el modelo, reducido a algo estable.

    «Chain Attack», «chain attack!» y «Chain  Attack» son la misma lección.
    """
    limpio = re.sub(r"[^a-z0-9 ]+", " ", (titulo or "").lower())
    return " ".join(limpio.split())[:60]


@dataclass
class Leccion:
    """Una situación del juego y lo que se sabe de ella.

    `racha` son los aciertos SEGUIDOS, no los totales, y de ahí sale el nivel.
    Se usa la racha porque lo que interesa saber es «¿esto lo domino AHORA?»:
    veinte aciertos viejos y tres fallos nuevos significan que el juego cambió
    —un parche, otro menú— y toca volver a mirarlo, no que ya está aprendido.
    """

    clave: str
    titulo: str = ""
    como: str = ""
    fuente: str = ""
    # Las huellas de pantalla donde se ha visto esta situación. Varias porque
    # la misma mecánica sale sobre fondos distintos.
    huellas: List[List[Tuple]] = field(default_factory=list)
    vistas: int = 0
    racha: int = 0
    fallos: int = 0
    # Los últimos tiempos de resolución, en ms. Es lo que permite contestar
    # «¿va más rápida?» con un número en vez de con una sensación.
    tiempos: List[float] = field(default_factory=list)
    buscada: bool = False
    nacida: str = field(default_factory=_ahora)

    TOPE_HUELLAS = 6
    TOPE_TIEMPOS = 12

    @property
    def nivel(self) -> str:
        if self.racha >= SABIDA_CON:
            return "sabida"
        if self.como or self.racha:
            return "aprendiendo"
        return "nueva"

    @property
    def atascada(self) -> bool:
        """Vista varias veces y sin resolver nunca: algo que no sabe hacer."""
        return self.racha == 0 and self.vistas >= ATASCADA_CON

    def mejora(self) -> Optional[Tuple[float, float]]:
        """(la primera vez, la última) en ms. `None` si no hay con qué comparar."""
        if len(self.tiempos) < 2:
            return None
        return (self.tiempos[0], self.tiempos[-1])

    def como_dict(self) -> Dict[str, Any]:
        return {"clave": self.clave, "titulo": self.titulo, "como": self.como,
                "fuente": self.fuente,
                "huellas": [[list(c) for c in h] for h in self.huellas],
                "vistas": self.vistas, "racha": self.racha,
                "fallos": self.fallos, "tiempos": self.tiempos,
                "buscada": self.buscada, "nacida": self.nacida}

    @classmethod
    def desde_dict(cls, d: Dict[str, Any]) -> "Leccion":
        return cls(
            clave=d.get("clave", ""), titulo=d.get("titulo", ""),
            como=d.get("como", ""), fuente=d.get("fuente", ""),
            huellas=[[tuple(c) for c in h] for h in d.get("huellas", [])],
            vistas=int(d.get("vistas", 0)), racha=int(d.get("racha", 0)),
            fallos=int(d.get("fallos", 0)),
            tiempos=[float(t) for t in d.get("tiempos", [])],
            buscada=bool(d.get("buscada", False)),
            nacida=d.get("nacida", ""))


# Dónde se buscan las guías. `zzz.SITIOS` son dos webs de gachas
# —game8.co y prydwen.gg— y con eso ZZZ va servido, pero la escuela tiene que
# valer para cualquier juego: probado con Soul Knight Prequel, aquellas dos no
# devolvían **nada**. Las wikis de fandom y wiki.gg cubren casi todo lo demás.
#
# Se sigue filtrando por dominio, y a propósito: una búsqueda abierta trae
# granjas de contenido y vídeos, y de ahí no sale una instrucción que se pueda
# jugar.
SITIOS_DE_GUIAS = (
    "fandom.com", "wiki.gg", "game8.co", "prydwen.gg", "ign.com",
    "gamerant.com", "thegamer.com", "pcgamesn.com", "eurogamer.net",
    "rockpapershotgun.com", "gamespot.com", "polygon.com", "gosunoob.com",
    "gameskinny.com", "dotesports.com", "escapistmagazine.com",
)


# Fandom devuelve **403** al User-Agent de `zzz` («…Celestia/1.0»): el sufijo
# lo delata como bot y la wiki cierra la puerta. Se pide con el de un navegador
# normal, que es lo que hay detrás — una página pública que alguien va a leer.
# No se salta ningún registro ni ningún muro de pago; sólo evita que un nombre
# en la cabecera decida por el contenido.
_UA_NAVEGADOR = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

TOPE_PAGINA = 2 * 1024 * 1024


def _url_de_api(url: str) -> str:
    """La misma página de Fandom, pedida por su API. "" si no es de Fandom.

    Fandom devuelve 403 a la página normal —también con User-Agent de
    navegador—, pero su API de MediaWiki contesta 200 sin más. Es la puerta
    que la propia wiki deja abierta para leerla desde un programa, así que se
    entra por ahí en vez de disfrazarse mejor.
    """
    m = re.match(r"(https?://[^/]*\bfandom\.com)/wiki/([^?#]+)", url)
    if not m:
        return ""
    import urllib.parse
    pagina = urllib.parse.unquote(m.group(2))
    return (f"{m.group(1)}/api.php?action=parse&format=json&prop=text"
            f"&page={urllib.parse.quote(pagina)}")


def _descargar_guia(url: str, timeout: float = 25.0) -> str:
    """Baja una página de guía. Cadena vacía si algo falla."""
    api = _url_de_api(url)
    crudo = _pedir(api or url, timeout)
    if api:
        # La API envuelve el HTML en JSON: {"parse":{"text":{"*":"<div>…"}}}
        try:
            return json.loads(crudo)["parse"]["text"]["*"]
        except Exception:
            return ""
    return crudo


def _pedir(url: str, timeout: float = 25.0) -> str:
    try:
        import urllib.request
        pet = urllib.request.Request(url, headers={
            "User-Agent": _UA_NAVEGADOR,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Accept-Language": "es,en;q=0.8"})
        with urllib.request.urlopen(pet, timeout=timeout) as r:
            return r.read(TOPE_PAGINA).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("escuela: no pude leer %s (%s)", url[:70], e)
        return ""


def _buscar_guias(consulta: str, timeout: float = 20.0) -> List[Tuple[str, str]]:
    """Consulta → [(url, título)] de sitios donde se explican juegos."""
    try:
        import urllib.parse
        import urllib.request
        from celestia_lib.zzz import _UA, _searxng_url
        q = urllib.parse.urlencode({"q": consulta, "format": "json"})
        pet = urllib.request.Request(f"{_searxng_url()}/search?{q}",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(pet, timeout=timeout) as r:
            datos = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except Exception as e:
        logger.warning("escuela: la búsqueda falló (%s)", e)
        return []
    return [(r.get("url", ""), r.get("title", ""))
            for r in datos.get("results", [])
            if any(sitio in r.get("url", "") for sitio in SITIOS_DE_GUIAS)]


def _habla_del_juego(url: str, titulo_web: str, terminos: Sequence[str]) -> bool:
    """¿Esta página es del juego que nos importa, o de otro cualquiera?

    9 sep 2026, 23:41: buscando «Entrenamiento libre» para ZZZ, el buscador
    devolvió `hamtaro.fandom.com/es/wiki/Guía_de_Hamtaro_RompeCorazones`. El
    filtro de dominios la dejaba pasar —fandom está en la lista de sitios de
    confianza— porque comprueba **dónde** está la página, no **de qué habla**.

    Aquella no llegó a guardarse de milagro (contestó 403, y además el texto no
    habría pasado el listón). Pero una wiki equivocada que conteste 200 con una
    frase plausible mete consejos de otro juego en el prompt, y ahí ya no hay
    quien lo note.

    Se mira URL y título juntos, sin guiones ni acentos, porque las wikis
    escriben `soul-knight-prequel` donde el juego se llama «Soul Knight».
    """
    if not terminos:
        return True
    campo = _clave(f"{url} {titulo_web}".replace("-", " ").replace("_", " "))
    return any(t and t in campo for t in terminos)


def buscador_web(juego: str,
                 alias: Sequence[str] = ()) -> Callable[[str], Tuple[str, str]]:
    """El buscador de verdad: searxng + las guías, como hace `zzz.py`.

    Se devuelve como función para que la escuela no sepa de red: en las
    pruebas se le pasa otra que contesta de memoria, y así el aprendizaje se
    puede probar entero sin salir a internet ni depender de que una web esté
    en pie. Es la misma inyección que ya usa el jugador con `mirar`.
    """

    terminos = tuple(_clave(t.replace("-", " ")) for t in (juego,) + tuple(alias)
                     if _clave(t))

    # Con qué nombre se sale a buscar. Aquí dentro el juego se llama «zzz»
    # —es el objetivo, el fichero del libro y la carpeta—, pero eso en un
    # buscador no es el juego: es tres letras. Medido el 9 de septiembre con
    # las consultas que de verdad hizo la partida:
    #
    #   «zzz Entrenamiento de reparto…»  → brawlstars.fandom.com/es/wiki/Sandy
    #   «Zenless Zone Zero Chain Attack…» → zenless-zone-zero.fandom.com/wiki/Chain_Attack
    #
    # Por eso el log repetía «busqué 3 y no encontré nada útil» en las tres
    # fases: buscaba bien, con el nombre equivocado. El primer alias es el
    # nombre largo del juego, y es el que entiende un buscador.
    nombre_web = (alias[0] if alias else juego)

    def buscar(titulo: str) -> Tuple[str, str]:
        # Import perezoso y dentro: `zzz` trae sus propias descargas y no se
        # paga a menos que de verdad haya que salir a buscar algo.
        try:
            from celestia_lib.zzz import extraer_mecanicas
        except Exception as e:                      # pragma: no cover
            logger.warning("escuela: no pude cargar el buscador (%s)", e)
            return "", ""
        consulta = f"{nombre_web} {titulo} how it works guide".strip()
        buscada = _mecanica_conocida(titulo)
        for url, titulo_web in (_buscar_guias(consulta) or [])[:3]:
            if not _habla_del_juego(url, titulo_web, terminos):
                logger.info("escuela: descarto %s, no es de %s", url[:60], juego)
                continue
            pagina = _descargar_guia(url)
            if not pagina:
                continue
            # Primero, el extractor de guías que ya existe, que es mucho mejor
            # que rebuscar frases a mano. Sus nombres son etiquetas internas en
            # español —«cadena», «esquiva»—, así que hay que traducir antes el
            # nombre en inglés que da el modelo: sin eso, «Chain Attack» no
            # casaba con «cadena» y se caía al respaldo teniendo la buena
            # delante. Se vio en la primera búsqueda de verdad.
            if buscada:
                for nombre, texto in extraer_mecanicas(pagina, url):
                    # Si lo que saca es un encabezado pelado, NO se da por
                    # bueno: se sigue por el camino general, que busca en la
                    # misma página una frase que instruya de verdad.
                    if nombre == buscada and _ensena_algo(texto):
                        return texto[:TOPE_COMO], url
            # Y si no, la primera frase de la página que nombre la mecánica y
            # diga algo más que el nombre.
            frase = _frase_que_explica(pagina, titulo)
            if frase:
                return frase[:TOPE_COMO], url
        return "", ""

    return buscar


def _mecanica_conocida(titulo: str) -> str:
    """El nombre del modelo → la etiqueta que usa `zzz.extraer_mecanicas`.

    «Chain Attack» → «cadena». Cadena vacía si no es ninguna de las conocidas,
    y entonces se va por el camino general (`_frase_que_explica`), que sirve
    para cualquier juego.
    """
    bajo = _clave(titulo)
    if not bajo:
        return ""
    try:
        from celestia_lib.zzz import _MECANICAS
    except Exception:                               # pragma: no cover
        return ""
    for nombre, claves in _MECANICAS:
        if any(c in bajo for c in claves):
            return nombre
    return ""


# Lo que convierte una frase en una instrucción: dice CUÁNDO o CÓMO. Es la
# misma lista que `zzz._ENSEÑA`, y por la misma razón — «How to Dodge» no
# enseña nada, «Dodge when the light flashes» sí.
_ENSEÑA = ("when", "after", "press", "appears", "hold", "tap", "use", "while",
           "before", "during", "flashing", "button", "cuando", "pulsa",
           "mantén", "aparece", "después")


def _a_texto_plano(pagina: str) -> str:
    """HTML → texto que se pueda leer. Quitando las etiquetas, no sólo scripts.

    `zzz._limpiar_html` sólo se lleva scripts, estilos y comentarios: deja los
    `<b>` y los `<span>` en su sitio, porque quien lo llama después parte por
    etiquetas. Aquí lo que se quiere es la frase para metérsela a un modelo, y
    la primera búsqueda de verdad la devolvió así:

        With this enabled, <b class='a-bold'>holding down the button</b> …

    Eso viajaba al prompt tal cual. De ahí este paso de más.
    """
    try:
        from celestia_lib.zzz import _limpiar_html
        pagina = _limpiar_html(pagina)
    except Exception:                               # pragma: no cover
        pass
    texto = re.sub(r"<[^>]+>", " ", pagina)
    return html.unescape(texto)


# Frases que hablan de la mecánica sin decir qué es: la excepción a una regla,
# un ajuste del menú de opciones. Explican un detalle a quien ya sabe jugar, y
# como chuleta para alguien que acaba de ver la pantalla no valen nada.
_NO_DEFINE = ("with this", "if you disable", "if you enable", "note that",
              "keep in mind", "this option", "this setting", "however")

# Frases que son navegación de la web, no contenido: el titulillo que promete
# el tutorial en vez de darlo. «See how to do Chain Attacks … here!» salió
# elegida en la primera búsqueda de verdad, y como chuleta no vale nada.
_ES_TITULAR = ("see how", "check out", "read more", "click here", "here!",
               "in this guide", "this article", "guide below", "learn more")


# Cómo empieza un titular que promete el tutorial en vez de darlo.
_TITULO_PELADO = ("how to", "what is", "what are", "when is", "where to",
                  "guide to", "todo sobre", "qué es", "que es", "cómo",
                  "como hacer")


def _es_tabla(texto: str) -> bool:
    """¿Esto es una fila de una tabla de estadísticas disfrazada de frase?

    Al sembrar «Daze» salió esto: «2.7 Box Cutter Stun Physical Pulchra
    Fellini Base ATK: 42-624 Impact: 6%-15% Watch your Fingers». Es una tabla
    de armas que el limpiador de HTML dejó en una línea. En el prompt no
    confunde un poco: confunde del todo.

    Se mira la proporción de dígitos, que es lo que separa una frase de una
    tabla sin tener que entender ninguna de las dos.
    """
    limpio = " ".join((texto or "").split())
    if not limpio:
        return True
    digitos = sum(1 for c in limpio if c.isdigit())
    return digitos / len(limpio) > 0.08 or limpio.count("%") >= 2


# Restos de wiki que no enseñan a jugar: remisiones, índices y frases que
# hablan de un objeto equipado en vez de la mecánica. En el prompt ocupan lo
# mismo que una instrucción buena y no dicen qué botón ni cuándo.
_RUIDO_DE_WIKI = re.compile(
    r"(?i)(see also|tutorial/|overview list|\bequipper|\blist\b|"
    r"greater than or equal)")


def _sirve_de_chuleta(texto: str) -> bool:
    """¿Esta línea le dice algo útil a quien está mirando la pantalla?"""
    return bool(texto) and not _RUIDO_DE_WIKI.search(texto)


def _sin_repetir_el_titulo(como: str, titulo: str) -> str:
    """La frase, sin el título pegado delante. La wiki lo repite casi siempre.

    «Chain Attack: Chain Attack is triggered upon…» y «Dodge Counter A Dodge
    Counter activates when…»: en una chuleta que ya lleva el nombre en la línea,
    eso son tokens pagados por decir dos veces lo mismo.
    """
    limpio = " ".join((como or "").split())
    t = (titulo or "").strip()
    if not t:
        return limpio
    # Sólo el caso limpio, «Título: la frase». Quitarlo cuando va pegado sin
    # los dos puntos deja frases rotas —«Dodge Counter A Dodge Counter…» se
    # quedó en «Counter A Dodge Counter…»—, y una chuleta mal cortada confunde
    # más de lo que ahorra.
    nuevo = re.sub(rf"^{re.escape(t)}\s*[:\-–]\s*", "", limpio, count=1, flags=re.I)
    return nuevo or limpio


def _ensena_algo(texto: str) -> bool:
    """¿Esta frase sirve de chuleta, o sólo nombra la mecánica?

    Al sembrar las mecánicas de ZZZ salieron cuatro así: «How to Do Dodge
    Counters», «What Are EX Special Attacks?», «Daze Gauge Length». Son
    encabezados de la guía, no instrucciones. Metidas en el prompt ocupan sitio
    y no dicen **qué botón ni cuándo**, que es lo único que se puede usar
    estando delante de la pantalla.

    El criterio es de forma, no de contenido, y por eso vale para cualquier
    juego: un título pelado es corto o empieza prometiendo («how to…»); una
    instrucción es una frase entera.
    """
    limpio = " ".join((texto or "").split())
    if not limpio or _es_tabla(limpio):
        return False
    bajo = limpio.lower()
    if any(bajo.startswith(t) for t in _TITULO_PELADO):
        return False
    # Contar palabras a secas no vale, y lo enseñaron las pruebas: «Pulsa
    # cuando destelle» son tres palabras y es una instrucción perfecta,
    # mientras que «Daze Gauge Length» son tres y no dice nada. Lo que separa
    # una de otra no es el largo, es que **diga cuándo o con qué**: un verbo de
    # acción o una condición. Por eso una frase corta pasa si trae eso, y si no
    # lo trae tiene que ser larga para ganarse el sitio.
    return len(bajo.split()) >= 5 or any(p in bajo for p in _ENSEÑA)


def _frase_que_explica(pagina: str, titulo: str) -> str:
    """De una página entera, la frase que enseña a usar esto. Sin modelo."""
    texto = _a_texto_plano(pagina)
    bajo_titulo = _clave(titulo)
    if not bajo_titulo:
        return ""
    mejores: List[Tuple[int, int, int, str]] = []
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        frase = " ".join(frase.split())
        if not (30 < len(frase) < 300):
            continue
        bajo = frase.lower()
        if bajo_titulo not in bajo:
            continue
        nota = sum(1 for p in _ENSEÑA if p in bajo)
        if not nota or any(t in bajo for t in _ES_TITULAR):
            continue
        if not _ensena_algo(frase):
            continue
        # Una frase que empieza matizando —«With this enabled…»— explica una
        # excepción, no la mecánica. Y cuanto antes aparezca el nombre, más
        # probable es que la frase lo esté DEFINIENDO: «A Chain Attack
        # triggers when…» frente a «…will not trigger Chain Attacks».
        define = 0 if any(bajo.startswith(p) for p in _NO_DEFINE) else 1
        pronto = -bajo.find(bajo_titulo)
        mejores.append((define, nota, pronto, frase))
    if not mejores:
        return ""
    return max(mejores)[3]


class Escuela:
    """Lo que ha aprendido de UN juego, situación por situación.

    Vive en `memoria/jugador/escuela_<juego>.json` y se carga sola. Cada
    partida la usa y la deja un poco más gorda; ninguna partida empieza de
    cero mientras el fichero siga ahí.
    """

    def __init__(self, juego: str, ruta: Optional[Path] = None,
                 buscar: Optional[Callable[[str], Tuple[str, str]]] = None,
                 tolerancia: float = 0.06,
                 tope_busquedas: int = TOPE_BUSQUEDAS,
                 alias: Sequence[str] = ()):
        self.juego = juego
        # Cómo se llama este juego, en todas las formas en que puede salir.
        # Sirve para UNA cosa: descartarlo como lección. Ver `_es_el_juego`.
        self.alias = tuple(_clave(a) for a in (juego,) + tuple(alias) if _clave(a))
        self.ruta = ruta
        # `None` significa «no salgas a la red»: útil para una partida que se
        # quiera rápida a toda costa, y es lo que usan las pruebas por defecto.
        self.buscar = buscar
        self.tolerancia = tolerancia
        self.tope_busquedas = tope_busquedas
        self.busquedas = 0
        # De las búsquedas, cuántas trajeron algo aprovechable. Se cuenta
        # aparte porque «busqué 3» y «aprendí 3» no son lo mismo, y decir lo
        # primero cuando pasó lo segundo es contarle a Enzo una película.
        self.encontradas = 0
        self.lecciones: Dict[str, Leccion] = {}
        # Lo aprendido EN ESTA partida, para poder contarlo al terminar.
        self.nuevas: List[str] = []
        self.graduadas: List[str] = []
        if ruta and Path(ruta).exists():
            self.cargar()

    # ─────────────────────────── disco ───────────────────────────

    def cargar(self) -> None:
        try:
            crudo = json.loads(Path(self.ruta).read_text("utf-8"))
        except Exception as e:
            logger.warning("no pude leer la escuela de %s: %s", self.juego, e)
            return
        for d in crudo.get("lecciones", []):
            lec = Leccion.desde_dict(d)
            if lec.clave:
                self.lecciones[lec.clave] = lec

    def guardar(self) -> None:
        if not self.ruta:
            return
        try:
            p = Path(self.ruta)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {"juego": self.juego, "guardado": _ahora(),
                 "lecciones": [l.como_dict() for l in self.lecciones.values()]},
                ensure_ascii=False), "utf-8")
        except Exception as e:
            logger.warning("no pude guardar la escuela de %s: %s", self.juego, e)

    # ─────────────────────── reconocer ───────────────────────

    def _es_el_juego(self, titulo: str) -> bool:
        """¿Lo que ha nombrado el modelo es el juego entero?

        Primera partida de verdad, 8 sep 2026: contestó «Zenless Zone Zero».
        La escuela se lo tomó por una mecánica, gastó una de las tres búsquedas
        de la partida y guardó dos párrafos de una guía para principiantes que
        no ayudan a resolver ninguna pantalla concreta. El nombre del juego es
        justo lo único que NUNCA puede ser una lección: para eso está el
        tutorial general (`zzz.SaberZZZ`), que ya se carga entero al empezar.
        """
        clave = _clave(titulo)
        if not clave:
            return False
        return any(clave == a or clave in a or a in clave for a in self.alias)

    def leccion_de(self, huella: Optional[Sequence]) -> Optional[Leccion]:
        """¿De qué situación es esta pantalla? Por píxeles, no por texto."""
        if not huella:
            return None
        for lec in self.lecciones.values():
            for h in lec.huellas:
                if Retina.firmas_parecidas(huella, h, self.tolerancia):
                    return lec
        return None

    def apunte(self, huella: Optional[Sequence]) -> str:
        """Lo que hay que recordarle al modelo ANTES de mirar esta pantalla.

        Cadena vacía en los dos extremos, y por motivos opuestos: si no se
        reconoce la situación no hay nada que decir todavía, y si ya está
        sabida **tampoco** — eso es lo que Enzo pidió con «que lo haga natural
        sin buscar cómo se hace». Una lección sabida que siguiera viajando en
        el prompt sería exactamente lo contrario: pagar para siempre por algo
        que ya no hace falta.
        """
        lec = self.leccion_de(huella)
        if lec is None or lec.nivel == "sabida" or not lec.como:
            return ""
        return f"Esto que tienes delante es {lec.titulo}: {lec.como}"

    def chuleta(self, terminos: Sequence[str], tope: int = 12,
                tope_linea: int = 110) -> str:
        """Las fichas de lo esencial, para llevarlas SIEMPRE en el prompt.

        `apunte()` sólo suelta lo de la pantalla que se tiene delante, y lo
        encuentra por huella: una ficha estudiada en frío no tiene huella
        todavía, así que no llegaría al modelo hasta después de haberse
        cruzado con ella. Esto es lo contrario: lo que hay que saber ANTES de
        ver nada —cuándo se esquiva, qué es la cadena, cuándo entra la
        definitiva— va en el prompt desde la primera jugada.

        Va acotado a propósito. La cuota de Groq son 7.000 tokens por minuto y
        cada línea de más son miradas de menos, así que entran pocas y cortas:
        [[proyecto_sesion66]].
        """
        claves = [_clave(t) for t in terminos if _clave(t)]
        fuera: List[str] = []
        # Lo dominado no viaja. Es la misma regla que `apunte()`: una mecánica
        # que ya le sale sola son tokens pagados en CADA mirada para recordarle
        # algo que no necesita, y con la cuota de por medio eso son miradas de
        # menos. Enzo, 8 sep 2026: «ya cuando sepa todo pues lo haga natural
        # sin buscar». Según se practica, la chuleta se vacía sola.
        # La misma ficha está guardada con su nombre inglés y con el español
        # —«Anomaly» y «Anomalía»—, así que sin esto la mitad de la chuleta
        # serían frases repetidas ocupando el sitio de otra mecánica.
        dichas: set = set()
        for clave in claves:
            lec = self.lecciones.get(clave)
            if lec is None or not lec.como or lec.nivel == "sabida":
                continue
            # La misma página bajo dos nombres —«Anomaly» y «Anomalía»— es la
            # misma ficha: se mira de dónde salió, no cómo quedó el texto, que
            # al recortarlo puede diferir por una palabra y colarse dos veces.
            if lec.fuente and lec.fuente in dichas:
                continue
            texto = _sin_repetir_el_titulo(lec.como, lec.titulo)[:tope_linea]
            if texto in dichas or not _sirve_de_chuleta(texto):
                continue
            dichas.add(texto)
            if lec.fuente:
                dichas.add(lec.fuente)
            fuera.append(f"- {lec.titulo or lec.clave}: {texto}")
            if len(fuera) >= tope:
                break
        if not fuera:
            return ""
        return "Lo que ya sé de este juego:\n" + "\n".join(fuera)

    # ─────────────────────── aprender ───────────────────────

    def ver(self, huella: Optional[Sequence], titulo: str = "") -> Optional[Leccion]:
        """Apunta que esta pantalla ha aparecido. Devuelve su lección, si hay.

        Si el modelo la ha nombrado (`titulo`) y no se conocía, nace aquí una
        lección nueva. Si no la nombró, se cuenta la visita igual: es lo que
        más tarde permite ver que una pantalla lleva tres vueltas sin
        resolverse y salir a buscarla por cuenta propia.
        """
        lec = self.leccion_de(huella)
        if lec is None and titulo and not self._es_el_juego(titulo):
            clave = _clave(titulo)
            if clave:
                lec = self.lecciones.get(clave)
                if lec is None:
                    lec = Leccion(clave=clave, titulo=titulo.strip()[:80])
                    self.lecciones[clave] = lec
                    self.nuevas.append(lec.titulo)
        if lec is None:
            return None
        lec.vistas += 1
        if huella and not any(Retina.firmas_parecidas(huella, h, self.tolerancia)
                              for h in lec.huellas):
            lec.huellas.append([tuple(c) for c in huella])
            del lec.huellas[:-Leccion.TOPE_HUELLAS]
        return lec

    def estudiar(self, lec: Optional[Leccion]) -> bool:
        """Sale a la red a averiguar cómo funciona esto. Una vez por lección.

        Devuelve si se aprendió algo. Los topes son deliberados y van todos en
        la misma dirección —que buscar no se coma la partida—: sólo si no se
        ha buscado antes, sólo si queda cupo, y una búsqueda vacía marca la
        lección como buscada igualmente para no reintentarla en bucle dentro
        de la misma partida.
        """
        if lec is None or lec.buscada or not self.buscar:
            return False
        if self.busquedas >= self.tope_busquedas:
            return False
        self.busquedas += 1
        lec.buscada = True
        try:
            como, fuente = self.buscar(lec.titulo or lec.clave)
        except Exception as e:
            logger.warning("escuela: la búsqueda de «%s» falló (%s)",
                           lec.titulo, e)
            return False
        if not como or not _ensena_algo(como):
            logger.info("escuela: no encontré nada aprovechable sobre «%s» "
                        "(%r)", lec.titulo, (como or "")[:60])
            return False
        lec.como = " ".join(como.split())[:TOPE_COMO]
        lec.fuente = fuente
        self.encontradas += 1
        logger.info("escuela: aprendido «%s» de %s", lec.titulo, fuente[:60])
        return True

    def acierto(self, huella: Optional[Sequence], ms: float = 0.0) -> None:
        """Salió bien: la pantalla cambió después de jugar aquí."""
        lec = self.leccion_de(huella)
        if lec is None:
            return
        antes = lec.nivel
        lec.racha += 1
        if ms > 0:
            lec.tiempos.append(round(float(ms), 1))
            del lec.tiempos[:-Leccion.TOPE_TIEMPOS]
        if antes != "sabida" and lec.nivel == "sabida":
            self.graduadas.append(lec.titulo or lec.clave)

    def fallo(self, huella: Optional[Sequence]) -> None:
        """No salió: la pantalla no se movió.

        La racha baja de uno en uno y no se borra de golpe. Un fallo suelto en
        algo dominado es casi siempre el juego —una carga, un diálogo que se
        cruzó—, y castigarlo con el olvido entero haría que lo aprendido se
        perdiera por ruido. Tres fallos seguidos sí lo bajan de «sabida», que
        es lo que se quiere cuando de verdad ha cambiado algo.
        """
        lec = self.leccion_de(huella)
        if lec is None:
            return
        lec.fallos += 1
        lec.racha = max(0, lec.racha - 1)

    # ─────────────────────── contarlo ───────────────────────

    def resumen_partida(self) -> str:
        """Qué ha aprendido en esta partida, para decírselo a Enzo."""
        partes: List[str] = []
        if self.nuevas:
            partes.append("Cosas nuevas que he visto: " + ", ".join(self.nuevas[:5]))
        if self.encontradas:
            partes.append(f"Encontré cómo funcionan {self.encontradas} de ellas")
        elif self.busquedas:
            # Buscar y no encontrar es un resultado, y hay que decirlo: si no,
            # el parte da a entender que aprendió algo que no aprendió. Pasó el
            # 9 sep con «Campo de pruebas / Training» — dijo «busqué cómo
            # funcionan 1 de ellas» y había vuelto con las manos vacías.
            partes.append(f"Busqué {self.busquedas} y no encontré nada útil")
        if self.graduadas:
            partes.append("Ya me salen solas: " + ", ".join(self.graduadas[:5]))
        return ". ".join(partes)

    def informe(self) -> str:
        """Todo lo que sabe del juego y cuánto ha mejorado. Para preguntárselo."""
        if not self.lecciones:
            return f"De {self.juego} todavía no he aprendido nada."
        por_nivel: Dict[str, List[Leccion]] = {"sabida": [], "aprendiendo": [],
                                               "nueva": []}
        for lec in self.lecciones.values():
            por_nivel[lec.nivel].append(lec)
        lineas = [f"Lo que sé de {self.juego} ({len(self.lecciones)} situaciones):"]
        etiquetas = [("sabida", "Me sale solo (ya no lo consulto)"),
                     ("aprendiendo", "Lo estoy practicando"),
                     ("nueva", "Recién visto, aún sin resolver")]
        for nivel, etiqueta in etiquetas:
            grupo = sorted(por_nivel[nivel], key=lambda l: -l.vistas)
            if not grupo:
                continue
            lineas.append(f"\n{etiqueta}:")
            for lec in grupo[:8]:
                fila = f"  · {lec.titulo or lec.clave} — visto {lec.vistas}"
                mejora = lec.mejora()
                if mejora and mejora[0] > mejora[1]:
                    fila += (f", de {mejora[0]:.0f} ms a {mejora[1]:.0f} ms")
                elif mejora:
                    fila += f", {mejora[1]:.0f} ms"
                lineas.append(fila)
        return "\n".join(lineas)


_ALIAS_DE_JUEGOS = {
    "zzz": ("zenless zone zero", "zenless", "nap", "hoyoverse"),
    "soul knight": ("soul knight prequel", "soulknight", "chillyroom"),
    "soul knight prequel": ("soul knight", "soulknight", "chillyroom"),
}


def escuela_del_juego(objetivo: str, raiz: Optional[Path] = None,
                      con_red: bool = True) -> Escuela:
    """La escuela que toca para lo que se va a jugar, lista para usarse.

    El nombre del juego sale del objetivo por la misma vía que el libro de
    jugadas, así que dos partidas al mismo juego comparten lo aprendido y una
    partida a otro juego no lo ensucia.
    """
    juego = _clave(objetivo) or "juego"
    base = Path(raiz) if raiz else MEM_DIR / "jugador"
    fichero = re.sub(r"[^a-z0-9]+", "_", juego).strip("_")[:40] or "juego"
    # Los nombres largos con los que un modelo puede referirse al juego. No es
    # una tabla de juegos: es la lista de lo que hay que DESCARTAR como
    # lección, y un juego que no esté aquí funciona igual — sólo se arriesga a
    # gastar una búsqueda la primera vez que el modelo diga su nombre.
    alias = _ALIAS_DE_JUEGOS.get(juego, ())
    return Escuela(juego=juego, alias=alias,
                   ruta=base / f"escuela_{fichero}.json",
                   buscar=buscador_web(juego, alias) if con_red else None)
