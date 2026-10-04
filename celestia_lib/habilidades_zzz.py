"""Las técnicas de cada agente de ZZZ con CÓMO se hacen, sus pasivas y los combos que salen de ahí.

Enzo, 11 sep 2026 (S73), tras la primera práctica larga: «que se aprenda cada
equipo, cada variable, cada combo, cada combinación, los tiempos, los esquives,
cada pasiva … para sacar provecho de cada personaje». Esa práctica esquivaba y
relevaba, pero de combos sólo sabía «ataca y pulsa la especial cada 4 toques».

Tres piezas, deterministas como `equipo_zzz` (la web pone los hechos, el código
el criterio, y se extrae con expresiones regulares):

  1. **Leer la ficha de Prydwen** («Skills» y «Core skills»). De cada frase de
     una técnica sale el GESTO (pulsar, mantener, cargar…), el MOMENTO (durante
     la esquiva, tras el 3.er golpe…) y el REQUISITO (energía, decibelios, «2
     puntos de Fallen Frost», un estado). Los iconos de los botones NO vienen en
     el HTML —queda un doble espacio: «pressing  will activate»—, así que el
     botón lo pone el tipo de técnica.
  2. **De qué vive cada pasiva**: asistencias rápidas, cadenas, desorden… en
     etiquetas que la práctica puede usar, y dicho en claro.
  3. **Combos que el reflejo sabe ejecutar**, en pasos cortos (ver `PASOS_RE`).
     Qué combo rinde más NO se decide aquí: eso lo mide la práctica.

Formato medido en las fichas de los 21 agentes de Enzo (11 sep 2026): 13 tipos
de técnica y 124 frases de entrada distintas.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from celestia_lib.equipo_zzz import PRYDWEN, Agente, SaberDeEquipo, _lineas, _una_linea, slug_prydwen
from celestia_lib.zzz import _limpiar_html

logger = logging.getLogger("celestia_v1")

# ── los pasos que entiende el reflejo ───────────────────────────────────────
#   a      pulsar atacar                a:1500  mantener atacar 1500 ms
#   e      pulsar la especial           e:1500  mantener la especial
#   E      EX: la especial SÓLO si su anillo dice que hay energía (si no, se salta)
#   d      esquivar
#   U      definitiva, sólo si está cargada
#   .300   esperar 300 ms
PASOS_RE = re.compile(r"^(?:[aeEdU](?::\d{2,5})?|\.\d{2,5})$")

# «Hold to activate» de una técnica que gasta un recurso: Miyabi carga hasta tres
# niveles y suelta sola al agotar el Fallen Frost, así que pasarse no estropea
# nada y quedarse corto sí. Un «hold to charge» es un golpe cargado corto.
MANTENER_MS = 2500
CARGAR_MS = 1500
# «Hold down or pause for a short while, and then press» (Anby, tras el 3.er golpe).
PAUSA_MS = 400


# Lo que acepta `leer_combos` en agente_movil/reflejo_zzz.c: fuera de esto el
# binario ni arranca, así que aquí se rechaza antes de mandarlo.
MAX_PASOS = 32


def pasos_validos(pasos: str) -> bool:
    trozos = (pasos or "").split()
    if not trozos or len(trozos) > MAX_PASOS:
        return False
    for t in trozos:
        if not PASOS_RE.match(t):
            return False
        if t.startswith(".") and int(t[1:]) > 5000:
            return False
        if ":" in t and not 40 <= int(t.split(":")[1]) <= 8000:
            return False
    return True


# ─────────────────────────────── leer la ficha ───────────────────────────────

_TIPOS = (("EX Special Attack", "ex"), ("Special Attack", "especial"), ("Basic Attack", "basico"),
          ("Dash Attack", "carrerilla"), ("Dodge Counter", "contraataque"), ("Dodge", "esquiva"),
          ("Quick Assist", "asistencia_rapida"), ("Defensive Assist", "asistencia_defensiva"),
          ("Evasive Assist", "asistencia_evasiva"), ("Assist Follow-Up", "continuacion"),
          ("Chain Attack", "cadena"), ("Ultimate", "definitiva"), ("Entry Skill", "entrada"))
_TIPO_DE = dict(_TIPOS)
_ENCABEZADO = re.compile(r"^(" + "|".join(re.escape(t) for t, _ in _TIPOS) + r")\s*:\s*(.+)$")


@dataclass
class Entrada:
    """Una forma de lanzar una técnica, sacada de una frase de la ficha."""
    gesto: str               # pulsar · mantener · pulsar_o_mantener · repetir · cargar · soltar · pulsar_otra_vez
    momento: str = ""        # durante_esquiva · tras_esquiva_perfecta · durante_golpe_3 · tras_golpe_3 · …
    requisito: str = ""      # energía · decibelios · «Fallen Frost ≥ 2» · «estado Idyllic Cadenza» · …
    frase: str = ""


@dataclass
class Tecnica:
    tipo: str                # basico · carrerilla · esquiva · contraataque · especial · ex · cadena · definitiva · …
    nombre: str
    texto: str = ""
    entradas: List[Entrada] = field(default_factory=list)
    golpes: int = 0          # «up to five forward slashes»: los golpes de su cadena


@dataclass
class Pasiva:
    clase: str               # principal (Core Passive) · equipo (Additional Ability)
    nombre: str
    condicion: str = ""      # «When another character in your squad is …»
    texto: str = ""


@dataclass
class Combo:
    nombre: str
    pasos: str
    porque: str = ""
    agente: str = ""
    # Qué hace falta para que esta jugada exista: «estado Idyllic Cadenza», la
    # energía, los decibelios… Enzo, 19 sep, pidiendo jueces: «la activación o
    # gasto de la barra de recurso exclusivo o medidor de agente». Sin esto no
    # se puede saber si el medidor propio de cada uno se usa o se desperdicia:
    # los combos llegan al reflejo como teclas sueltas y el requisito se perdía
    # por el camino.
    necesita: str = ""


# El orden importa: «repeatedly press or hold» no es «press or hold», y cualquier
# «hold» que no sea de cargar es mantener.
_GESTOS = (
    ("repetir", re.compile(r"(?i)\brepeatedly press\b|\bpress repeatedly\b")),
    ("pulsar_o_mantener", re.compile(r"(?i)\bpress or hold\b")),
    ("pulsar_otra_vez", re.compile(r"(?i)\bpress(?: \w+)? again\b")),
    ("cargar", re.compile(r"(?i)\bhold(?: down)? to charge\b|\bcharge continuously\b")),
    ("mantener", re.compile(r"(?i)\bhold\b")),
    ("soltar", re.compile(r"(?i)\brelease(?: the button)? to\b")),
    ("pulsar", re.compile(r"(?i)\b(?:press|pressing|tap)\b")),
)
# El verbo tiene que ser una ORDEN al jugador: al empezar la frase o tras una coma
# («With enough energy, press…», «…, but pressing will…»). Sin esto, «Nangong Yu
# can hold up to 100 Downbeats» era una técnica que se mantiene.
_IMPERATIVO = re.compile(r"(?i)(?:^|[,;:]\s*|\b(?:then|and|or|but|quickly|repeatedly)\s+)"
                         r"(?:repeatedly\s+)?(?:press|pressing|hold|tap|release)\b(?! up to)")

_MOMENTOS = (
    ("tras_esquiva_perfecta", re.compile(r"(?i)during a perfect dodge|perfect dodge is triggered|"
                                         r"after triggering a perfect dodge")),
    ("durante_esquiva", re.compile(r"(?i)during a dodge|while dodging|\bto dodge, then\b")),
    ("tras_asistencia_defensiva", re.compile(r"(?i)after a defensive assist")),
    ("tras_asistencia_evasiva", re.compile(r"(?i)after an evasive assist")),
    ("tras_asistencia_rapida", re.compile(r"(?i)after triggering a quick assist")),
    ("ante_ataque", re.compile(r"(?i)about to be attacked")),
    ("companero_lanzado", re.compile(r"(?i)\bis launched\b")),
    ("cadena", re.compile(r"(?i)chain attack is triggered")),
)
_DURANTE_GOLPE = re.compile(r"(?i)during the (\d)(?:st|nd|rd|th)\b(?:[- ]hit| and \d(?:st|nd|rd|th) hits)")
# «After the 2nd hit of…», «After unleashing the 3rd hit…», «If activated after the 3rd, 4th, or 5th hit».
_TRAS_GOLPE = re.compile(r"(?i)after (?:unleashing |performing )?the (\d)(?:st|nd|rd|th)(?:[- ]hit\b|,)")
_TRAS_TECNICA = re.compile(r"(?i)\bafter (?:using|activating|consuming|the move\b|the skill\b)")
_SIGUE_DESDE = re.compile(r"(?i)follow up with the (\d)(?:st|nd|rd|th) hit")

_NUMEROS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_GOLPES = re.compile(r"(?i)\bup to (\d|two|three|four|five|six|seven|eight) (?:[\w-]+ ){0,2}"
                     r"(?:slashes|hits|strikes|attacks|punches|shots|kicks|thrusts|swings)\b")


def _frases(lineas: Iterable[str]) -> Iterable[str]:
    for linea in lineas:
        for f in re.split(r"(?<=[.;!])\s+|(?<=:)\s+(?=[A-Z])", linea or ""):
            f = " ".join(f.split())
            if f:
                yield f


def _momento(frase: str) -> str:
    for nombre, rx in _MOMENTOS:
        if rx.search(frase):
            return nombre
    m = _DURANTE_GOLPE.search(frase)
    if m:
        return f"durante_golpe_{m.group(1)}"
    m = _TRAS_GOLPE.search(frase)
    if m:
        return f"tras_golpe_{m.group(1)}"
    return "tras_tecnica" if _TRAS_TECNICA.search(frase) else ""


def _requisito(frase: str) -> str:
    """Lo que hace falta para lanzarla. Los nombres propios (recursos, estados) van con mayúscula."""
    if re.search(r"(?i)\benough energ", frase):
        return "energía"
    if re.search(r"(?i)decibel rating is at maximum", frase):
        return "decibelios"
    m = re.search(r"[Ww]ith at least (\d+) points? of ([A-Z][\w' -]*?)(?=,| hold| press)", frase)
    if m:
        return f"{m.group(2)} ≥ {m.group(1)}"
    m = re.search(r"(?:(?<!not )[Ww]hile in|[Dd]uring|(?<!not )\b[Ii]n) the ([A-Z][\w' -]*?) state", frase)
    if m:
        return f"estado {m.group(1)}"
    m = re.search(r"\b(?:is in|while in|in) ([A-Z][\w' -]*?) Mode\b", frase)
    if m:
        return f"modo {m.group(1)}"
    m = re.search(r"[Ww]ith enough ([A-Z][\w' -]*?)(?=,| press| hold)", frase)
    if m:
        return m.group(1)
    m = re.search(r"[Ww]hen ([A-Z][\w' -]*?) is active", frase)
    if m:
        return f"{m.group(1)} activo"
    # «When Remielle has Voidflare», «With 6 Qingming Sword Force and while…»: la cifra va delante.
    m = re.search(r"\b(?:has|[Ww]ith) (?:stored |\d+ )?([A-Z][\w' -]*?)(?=,| and | hold| press)", frase)
    return m.group(1) if m else ""


def entradas_de(lineas: Sequence[str]) -> List[Entrada]:
    fuera: List[Entrada] = []
    for f in _frases(lineas):
        if not _IMPERATIVO.search(f):
            continue
        gesto = next((g for g, rx in _GESTOS if rx.search(f)), "")
        if gesto:
            fuera.append(Entrada(gesto, _momento(f), _requisito(f), f[:220]))
    return fuera


def golpes_de(texto: str) -> int:
    m = _GOLPES.search(texto or "")
    if not m:
        return 0
    n = m.group(1).lower()
    return int(n) if n.isdigit() else _NUMEROS.get(n, 0)


_NOMBRE_RE = re.compile(r'(?is)<p class="skill-name">(.*?)</p>')
_DESCRIPCION_RE = re.compile(r'(?is)<div class="skill-description[^"]*">(.*?)</div>')
_CABECERA_RE = re.compile(r'(?is)<div class="content-header[^"]*">(.*?)</div>')


def _secciones(pagina: str) -> Dict[str, str]:
    t = _limpiar_html(pagina or "")
    cortes = [(m.start(), m.end(), _una_linea(m.group(1))) for m in _CABECERA_RE.finditer(t)]
    return {titulo: t[fin: (cortes[i + 1][0] if i + 1 < len(cortes) else len(t))]
            for i, (_ini, fin, titulo) in enumerate(cortes)}


def pares_de(fragmento: str) -> List[Tuple[str, List[str]]]:
    """Cada `skill-name` con las líneas de SU descripción (la primera que venga antes del siguiente nombre)."""
    nombres = list(_NOMBRE_RE.finditer(fragmento or ""))
    fuera = []
    for i, m in enumerate(nombres):
        fin = nombres[i + 1].start() if i + 1 < len(nombres) else len(fragmento)
        d = _DESCRIPCION_RE.search(fragmento, m.end(), fin)
        fuera.append((_una_linea(m.group(1)), _lineas(d.group(1)) if d else []))
    return fuera


def leer_tecnicas(pares: Sequence[Tuple[str, Sequence[str]]]) -> List[Tecnica]:
    fuera: List[Tecnica] = []
    for nombre, lineas in pares:
        texto = "\n".join(lineas)
        m = _ENCABEZADO.match(nombre)
        if m:
            fuera.append(Tecnica(_TIPO_DE[m.group(1)], m.group(2).strip().strip('"“”'), texto,
                                 entradas_de(lineas), golpes_de(texto)))
        elif fuera and lineas:
            # «Idyllic Cadenza»: una mecánica sin tipo que vive dentro de la técnica
            # de antes. Su texto se queda (dice en qué estado se entra); sus frases
            # no son entradas de ESA técnica, así que no se apuntan como tales.
            fuera[-1].texto += f"\n{nombre}: {texto}"
    return fuera


# Las líneas de los controles de nivel que se cuelan entre nombre y descripción.
_RELLENO = re.compile(r"^(?:Lv\.?\s*\d+|BASE ATK\s*:\s*\d+|[A-Za-z .]+\s0)$")


def leer_pasivas(pares: Sequence[Tuple[str, Sequence[str]]]) -> List[Pasiva]:
    fuera: List[Pasiva] = []
    for nombre, lineas in pares:
        m = re.match(r"^(Core Passive|Additional Ability)\s*:?\s*(.+)$", nombre)
        if not m:
            continue
        lineas = [l for l in lineas if not _RELLENO.match(l)]
        condicion = ""
        if lineas and re.match(r"(?i)^when another character in your squad", lineas[0]):
            condicion = lineas.pop(0).rstrip(":").strip()
        fuera.append(Pasiva("principal" if m.group(1) == "Core Passive" else "equipo",
                            m.group(2).strip(), condicion, "\n".join(lineas)))
    return fuera


def extraer_habilidades_prydwen(pagina: str) -> Dict[str, list]:
    s = _secciones(pagina)
    return {"tecnicas": leer_tecnicas(pares_de(s.get("Skills", ""))),
            "pasivas": leer_pasivas(pares_de(s.get("Core skills", "")))}


# ─────────────────────────────── las pasivas ───────────────────────────────

# De qué vive una pasiva, y qué hacer en la pelea para aprovecharla.
_PROVECHO = (
    ("asistencia_rapida", r"(?i)quick assist",
     "vive de las asistencias rápidas: cuando el relevo las ofrezca, púlsalo"),
    ("cadena", r"(?i)chain attack", "elígela en los ataques en cadena"),
    ("asistencia_perfecta", r"(?i)defensive assist|evasive assist|perfect assist|precise assist",
     "releva en el destello dorado: sus asistencias cuentan"),
    ("esquiva_perfecta", r"(?i)perfect dodge|dodge counter", "busca la esquiva perfecta y contraataca en el acto"),
    ("ex", r"(?i)ex special attack", "gasta su energía en el EX"),
    ("definitiva", r"(?i)\bultimate\b|\bdecibel", "suelta la definitiva en cuanto esté cargada"),
    ("desorden", r"(?i)\bdisorder\b", "junta dos anomalías distintas para provocar desorden"),
    ("aturdir", r"(?i)\bdaze\b|\bstun(?:ned)?\b", "sube el aturdimiento y castiga al enemigo aturdido"),
    ("tras_golpe", r"(?i)after the \d(?:st|nd|rd|th)[- ]hit", "remata la cadena básica con la especial"),
    ("relevo", r"(?i)\bswitch(?:es|ed|ing)? (?:in|on-field)\b", "entra y sale a menudo: gana al cambiar"),
)


def provecho_de(pasivas: Sequence[Pasiva]) -> List[Dict[str, str]]:
    vistos: Dict[str, Dict[str, str]] = {}
    for p in pasivas:
        for etiqueta, rx, consejo in _PROVECHO:
            if etiqueta in vistos or not re.search(rx, p.texto):
                continue
            frase = next((f for f in _frases(p.texto.split("\n")) if re.search(rx, f)), "")
            vistos[etiqueta] = {"etiqueta": etiqueta, "consejo": consejo, "pasiva": p.nombre, "frase": frase[:220]}
    return list(vistos.values())


# ─────────────────────────────── los combos ───────────────────────────────

def _norma(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


_PASO_GUIA = re.compile(r"^(EX Special Attack|Special Attack|Basic Attack|Dash Attack|Dodge Counter|Dodge|"
                        r"Ultimate|Chain Attack|Quick Assist)\b\s*:?\s*(.*)$")


def _solo_se_mantiene(t: Optional[Tecnica]) -> bool:
    return bool(t and t.entradas) and all(e.gesto in ("mantener", "cargar") for e in t.entradas[:1])


def combos_de_la_guia(como: Sequence[Sequence[str]], tecnicas: Sequence[Tecnica], agente: str = "") -> List[Combo]:
    """Los combos que la guía escribe paso a paso («EX Special Attack: Hisetsu P1», «Basic Attack: …»).

    Una línea que no es un paso cierra el combo que iba; «Example 2:» le pone
    nombre al siguiente. Una cadena o una asistencia también lo cierran: eso ya
    no lo lanza quien está en el campo.
    """
    fuera: List[Combo] = []
    for titulo, texto in como or []:
        etiqueta, pasos = "", []
        for linea in list((texto or "").split("\n")) + [""]:
            linea = linea.strip()
            m = _PASO_GUIA.match(linea)
            tipo = m.group(1) if m else ""
            if not m or tipo in ("Chain Attack", "Quick Assist", "Dodge Counter"):
                if len(pasos) >= 2:
                    nombre = f"{titulo.rstrip(':')} · {etiqueta}" if etiqueta else titulo.rstrip(":")
                    fuera.append(Combo(nombre, " ".join(pasos), "de la guía de Prydwen", agente))
                pasos = []
                e = re.match(r"(?i)^(example \d+)\s*:?$", linea)
                if e:
                    etiqueta = e.group(1).capitalize()
                continue
            resto = m.group(2)
            if tipo == "EX Special Attack":
                pasos.append("E")
            elif tipo == "Special Attack":
                pasos.append("e")
            elif tipo == "Basic Attack":
                t = next((x for x in tecnicas if x.tipo == "basico" and _norma(x.nombre)
                          and _norma(x.nombre) in _norma(resto)), None)
                cargado = re.search(r"(?i)charged|\bhold", resto) or _solo_se_mantiene(t)
                pasos.append(f"a:{MANTENER_MS}" if cargado else "a")
            elif tipo == "Dash Attack":
                pasos += ["d", "a"]
            elif tipo == "Dodge":
                pasos.append("d")
            elif tipo == "Ultimate":
                pasos.append("U")
    return fuera


_LETRA = {"ex": "E", "especial": "e", "definitiva": "U"}


def componer_combos(tecnicas: Sequence[Tecnica], como: Sequence[Sequence[str]] = (),
                    pasivas: Sequence[Pasiva] = (), agente: str = "") -> List[Combo]:
    """Los combos de un agente: primero los de su guía, luego los que dicen sus técnicas."""
    combos = combos_de_la_guia(como, tecnicas, agente)

    def poner(nombre: str, pasos: str, porque: str, necesita: str = "") -> None:
        if pasos_validos(pasos) and all(c.pasos != pasos for c in combos):
            combos.append(Combo(nombre, pasos, porque, agente, necesita))

    basicos = [t for t in tecnicas if t.tipo == "basico"]
    principal = next((t for t in basicos if t.entradas and not t.entradas[0].requisito
                      and not t.entradas[0].momento
                      and t.entradas[0].gesto in ("pulsar", "pulsar_o_mantener", "repetir")), None)
    golpes = principal.golpes if principal and 3 <= principal.golpes <= 8 else 5
    if principal:
        poner("cadena básica", " ".join(["a"] * golpes), f"{principal.nombre}: {golpes} golpes seguidos")

    def quien_da(recurso: str) -> Optional[Tecnica]:
        """La técnica que da el recurso que otra gasta («gain 2 points of Fallen Frost»)."""
        rx = re.compile(r"(?i)\b(?:gain|gains|obtain|obtains|restores?)\b[^.]{0,30}" + re.escape(recurso))
        for tipo in ("ex", "especial", "definitiva"):
            for t in tecnicas:
                if t.tipo == tipo and rx.search(t.texto):
                    return t
        return None

    def quien_entra(estado: str) -> Optional[Tecnica]:
        rx = re.compile(r"(?i)\benter(?:s|ing)? the " + re.escape(estado) + r" state")
        for tipo in ("especial", "ex", "definitiva"):
            for t in tecnicas:
                if t.tipo == tipo and rx.search(t.texto):
                    return t
        return None

    for t in tecnicas:
        if t.tipo == "basico":
            for i, e in enumerate(t.entradas):
                estado = e.requisito.startswith(("estado ", "modo "))
                m = re.match(r"durante_golpe_(\d)", e.momento)
                if m and e.gesto in ("cargar", "mantener", "repetir", "pulsar_o_mantener") and not estado:
                    n = int(m.group(1))
                    poner(f"{t.nombre}: golpe {n} cargado", " ".join(["a"] * (n - 1) + [f"a:{CARGAR_MS}"]), e.frase)
                    continue
                m = re.match(r"tras_golpe_(\d)", e.momento)
                if m and not estado and t is not principal:
                    n = int(m.group(1))
                    poner(t.nombre, " ".join(["a"] * n + [f".{PAUSA_MS}", "a"]), e.frase)
                    continue
                if i == 0 and e.gesto in ("mantener", "cargar") and not e.momento and not estado:
                    pasos = f"a:{MANTENER_MS if e.gesto == 'mantener' else CARGAR_MS}"
                    fuente = quien_da(e.requisito.split(" ≥")[0]) if e.requisito else None
                    if fuente is not None:
                        poner(f"{fuente.nombre} + {t.nombre}", f"{_LETRA[fuente.tipo]} {pasos}", e.frase,
                              necesita=e.requisito)
                    poner(t.nombre, pasos, e.frase)
                    continue
                if i == 0 and e.requisito.startswith("estado ") and e.gesto == "pulsar" and not e.momento:
                    fuente = quien_entra(e.requisito[len("estado "):])
                    if fuente is not None:
                        poner(f"{fuente.nombre} → {t.nombre}", f"{_LETRA[fuente.tipo]} a", e.frase,
                              necesita=e.requisito)
        elif t.tipo == "carrerilla":
            if any(e.momento == "durante_esquiva" for e in t.entradas):
                poner("carrerilla", "d a", t.nombre)
        elif t.tipo in ("especial", "ex"):
            letra = _LETRA[t.tipo]
            primera = t.entradas[0] if t.entradas else None
            if primera is not None and not primera.requisito.startswith(("estado ", "modo ")):
                poner("EX" if t.tipo == "ex" else "especial", letra, t.nombre)
                # En ZZZ el EX Special es el MISMO botón con energía, y lo tienen
                # todos. Pero el combo con «E» sólo salía si la ficha traía una
                # sección «EX Special Attack:», y la de Astra Yao describe su EX
                # dentro del texto del especial. Resultado (19 sep, medido): de
                # sus 10 combos ninguno pedía la EX, y en 235 s de juego lanzó
                # CERO — siendo su EX la que da el buff de daño al equipo y las
                # cadenas, o sea su trabajo entero. Sin esto, un apoyo se pasa
                # la partida sin hacer lo único que se le pide.
                if t.tipo == "especial" and not any(x.tipo == "ex" for x in tecnicas):
                    poner("EX", _LETRA["ex"], t.nombre)
            if any(e.gesto == "pulsar_otra_vez" for e in t.entradas):
                poner(f"{t.nombre} doble", f"{letra} {letra}", t.nombre)
            if any(e.gesto == "cargar" and not e.requisito.startswith("estado ") for e in t.entradas):
                poner(f"{t.nombre} cargada", f"{letra}:{CARGAR_MS}", t.nombre)
            m = _TRAS_GOLPE.search(t.texto)
            if m:
                n = int(m.group(1))
                pasos = ["a"] * n + [letra]
                s = _SIGUE_DESDE.search(t.texto)
                if s and golpes >= int(s.group(1)):
                    # Swift Ruten de Yanagi: tras la especial se sigue desde el 3.er golpe.
                    pasos += ["a"] * (golpes - int(s.group(1)) + 1) + [letra]
                poner(f"{t.nombre} tras el golpe {n}", " ".join(pasos), m.group(0))
        elif t.tipo == "definitiva":
            poner("definitiva", "U", t.nombre)

    for p in pasivas:
        m = _TRAS_GOLPE.search(p.texto)
        if m and re.search(r"(?i)special attack", p.texto):
            n = int(m.group(1))
            poner(f"{p.nombre}: especial tras el golpe {n}", " ".join(["a"] * n + ["e"]), m.group(0))
    return combos


# ─────────────────────────────── guardar y consultar ───────────────────────────────

class SaberDeHabilidades:
    """La ficha de técnicas, pasivas y combos de cada agente, en caché con fecha.

    Va encima de `SaberDeEquipo`: usa su índice de Prydwen, su descarga y su
    caché, así que se prueba igual —sin red— y una ficha vacía no se guarda.
    """

    def __init__(self, saber: Optional[SaberDeEquipo] = None):
        self.saber = saber or SaberDeEquipo()

    def ficha(self, agente: Agente, refrescar: bool = False) -> Dict:
        slug = slug_prydwen(agente, self.saber.indice_prydwen())
        if not slug:
            return {}
        clave = f"habilidades_{slug}"
        guardado = self.saber._leer(clave)
        if not refrescar and self.saber._fresco(guardado):
            return guardado
        url = f"{PRYDWEN}/zenless/characters/{slug}"
        h = extraer_habilidades_prydwen(self.saber._descargar(url))
        if not h["tecnicas"]:
            # Vacío NO se guarda: una web caída envenenaría la caché una semana.
            logger.warning("habilidades: la ficha de %s no trajo técnicas", agente.nombre)
            return guardado or {}
        try:
            como = self.saber.guia(agente).get("como") or []
        except Exception as e:
            logger.warning("habilidades: sin guía de %s (%s)", agente.nombre, e)
            como = []
        combos = componer_combos(h["tecnicas"], como, h["pasivas"], agente.llamado)
        datos = {"obtenido": self.saber._ahora(), "fuente": url, "agente": agente.nombre,
                 "llamado": agente.llamado,
                 "tecnicas": [asdict(t) for t in h["tecnicas"]],
                 "pasivas": [asdict(p) for p in h["pasivas"]],
                 "provecho": provecho_de(h["pasivas"]),
                 "combos": [asdict(c) for c in combos]}
        self.saber._guardar(clave, datos)
        return datos

    def combos(self, agente: Agente) -> List[Combo]:
        return [Combo(**c) for c in self.ficha(agente).get("combos", [])]
