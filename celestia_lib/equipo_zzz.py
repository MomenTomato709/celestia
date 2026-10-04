"""Saber jugar un EQUIPO de ZZZ: quién va, qué hace cada uno y en qué orden.

Enzo, 10 sep 2026: «tiene que saber qué personajes tiene, qué equipo lleva y
cómo se usa ese equipo, combinaciones de ese equipo», y después: «voy a poner
un equipo mío y que aprenda cómo funciona».

`zzz.py` ya sabe QUIÉNES van juntos (los equipos del meta). Lo que faltaba es
saber JUGARLOS, y son tres piezas que no se pisan:

  1. **El catálogo** — los agentes que existen y la especialidad de cada uno
     (Stun, Attack, Anomaly, Support, Defense, Rupture). Sale de la tabla de la
     wiki, con fecha. Es lo que convierte un nombre en un papel.
  2. **La guía de cada agente** — cómo se juega y sus combos, de Prydwen. Es el
     texto de la guía recortado, no un resumen del modelo: un resumen del
     modelo sonaría igual de seguro estando mal.
  3. **La rotación** — en qué orden entran. Esto NO se descarga: es criterio y
     sale de las especialidades con reglas. Quien aturde abre, el apoyo entra
     antes que el daño, y el daño fuerte se guarda para el final de la vuelta.

Todo determinista, por la regla de `zzz.py`: el meta lo pone la web, el
criterio lo pone el código, y la extracción son expresiones regulares sobre el
HTML. Sale igual con cualquier modelo y sin red (desde la caché).

⚠️ Lo que este fichero NO hace: reconocer quién está en pantalla. Eso es mirar
píxeles y vive aparte; aquí se entra ya con los nombres.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from celestia_lib.zzz import DIAS_FRESCURA, _descargar, _limpiar_html, normalizar
from celestia_lib.paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

WIKI_API = "https://zenless-zone-zero.fandom.com/api.php"
PRYDWEN = "https://www.prydwen.gg"


# ─────────────────────────────── el catálogo ───────────────────────────────

@dataclass
class Agente:
    """Un agente tal y como lo describe la wiki."""
    nombre: str                # «Hoshimi Miyabi»: el de la wiki
    corto: str = ""            # «Miyabi»: el que escribe el juego
    rango: str = ""            # S · A
    atributo: str = ""         # Ether · Ice · …
    especialidad: str = ""     # Attack · Stun · Anomaly · Support · Defense · Rupture
    tipo: str = ""             # Slash · Strike · Pierce
    icono: str = ""            # dirección del icono en la wiki
    faccion: str = ""          # Victoria Housekeeping Co. · …: algunas pasivas piden la misma

    @property
    def llamado(self) -> str:
        """Cómo se le nombra al jugar: el corto si lo hay."""
        return self.corto or self.nombre


def _una_linea(fragmento: str) -> str:
    t = _html.unescape(re.sub(r"<[^>]+>", " ", fragmento or "")).replace("­", "")
    return " ".join(t.split())


def _lineas(fragmento: str) -> List[str]:
    """HTML → líneas de texto, sin perder dónde acababa cada cosa.

    Aplanarlo todo en una sola línea —lo que hace `zzz._texto`— destroza justo
    lo que más importa de una guía: los combos son LISTAS («Cloud-Shaper»,
    «Ultimate», «Chain Attack»), y pegadas en una frase ya no se sabe dónde
    acaba un paso y empieza el siguiente.
    """
    t = _limpiar_html(fragmento or "")
    t = re.sub(r"(?i)<br\s*/?>|</(?:p|li|div|h[1-6]|tr|ul|ol)>", "\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("­", "")
    return [" ".join(l.split()) for l in t.split("\n") if l.strip()]


def _celda(celda: str) -> str:
    """Texto de una celda; si sólo tiene imagen, su `alt`."""
    texto = _una_linea(celda)
    if texto:
        return texto
    m = re.search(r'(?i)\balt="([^"]+)"', celda)
    return _una_linea(m.group(1)) if m else ""


def extraer_agentes(pagina: str) -> List[Agente]:
    """La tabla «Agent List» de la wiki → agentes. Vacío si no está.

    Se ancla en la CABECERA (Name, Specialty…) y no en la posición de la tabla
    ni de las columnas: la página tiene cuatro tablas, y la wiki reordena
    columnas cuando añade una.
    """
    t = _limpiar_html(pagina or "")
    for tabla in re.findall(r"(?is)<table.*?</table>", t):
        filas = re.findall(r"(?is)<tr.*?</tr>", tabla)
        if len(filas) < 2:
            continue
        cab = [_celda(c).lower() for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", filas[0])]
        if "name" not in cab or "specialty" not in cab:
            continue
        col = {n: cab.index(n) for n in
               ("icon", "name", "rank", "attribute", "specialty", "attack type", "faction")
               if n in cab}
        agentes: List[Agente] = []
        for f in filas[1:]:
            celdas = re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", f)
            if len(celdas) <= max(col.values()):
                continue
            nombre = _celda(celdas[col["name"]])
            if not nombre:
                continue
            rango = ""
            if "rank" in col:
                m = re.search(r"AgentRank\s*([SAB])\b", celdas[col["rank"]])
                rango = m.group(1) if m else _celda(celdas[col["rank"]])[:1]
            icono = ""
            if "icon" in col:
                # `src` suele ser un GIF de relleno en `data:`; el bueno es `data-src`.
                m = re.search(r'(?i)(?:data-src|src)="(https?://[^"]+)"', celdas[col["icon"]])
                if m:
                    icono = re.sub(r"/scale-to-width-down/\d+", "", _html.unescape(m.group(1)))
            agentes.append(Agente(
                nombre=nombre, rango=rango,
                atributo=_celda(celdas[col["attribute"]]) if "attribute" in col else "",
                especialidad=_celda(celdas[col["specialty"]]),
                tipo=_celda(celdas[col["attack type"]]) if "attack type" in col else "",
                icono=icono,
                faccion=_celda(celdas[col["faction"]]) if "faction" in col else ""))
        if agentes:
            return agentes
    return []


def nombre_corto(wikitext: str) -> str:
    """El `brief_name` de la ficha de la wiki, limpio. "" si no lo tiene.

    Viene sucio de maneras que se vieron en la wiki real: «Neko&shy;mata» (un
    guion invisible para partir la palabra) y, cuando no hay, un comentario
    «<!--Agent briefname-->» en su lugar.
    """
    # ⚠️ Espacios de la MISMA línea, no `\s*`: con el campo vacío, `\s*` se comía
    # el salto de línea y el nombre corto de Yixuan salía «realname =» (S72).
    m = re.search(r"(?im)^\|[ \t]*brief_name[ \t]*=[ \t]*(.*)$", wikitext or "")
    if not m:
        return ""
    v = re.sub(r"(?s)<!--.*?-->", "", m.group(1))
    return _una_linea(v).strip(" |")


def _fichas(texto: str) -> set:
    return {p for p in normalizar((texto or "").replace("&", " ")).split() if p}


def buscar_agente(texto: str, catalogo: Sequence[Agente]) -> Optional[Agente]:
    """Un nombre como lo escriba quien sea → el agente del catálogo."""
    buscado = normalizar(texto)
    if not buscado:
        return None
    for a in catalogo:
        if buscado in (normalizar(a.nombre), normalizar(a.corto)):
            return a
    fichas = _fichas(texto)
    candidatos = [a for a in catalogo
                  if fichas and (fichas <= _fichas(a.nombre) or fichas == _fichas(a.corto))
                  and any(len(p) >= 4 for p in fichas)]
    return candidatos[0] if len(candidatos) == 1 else None


def agentes_en_texto(texto: str, catalogo: Sequence[Agente]) -> List[Agente]:
    """Los agentes que salen nombrados en un texto leído de la pantalla.

    El texto del OCR llega pegado y con basura —en la pantalla de equipo se
    leyó «miyabigood yixuan … shungwan»—, así que un nombre largo se busca
    DENTRO de las palabras. Uno corto no: «Ben» está dentro de «beneficios» y
    «Aria» dentro de «variable»; ésos tienen que ser una palabra entera.
    """
    plano = normalizar(texto)
    palabras = set(plano.split())
    pegado = plano.replace(" ", "")
    hallados: List[Tuple[int, Agente]] = []
    for a in catalogo:
        pos = -1
        for n in (a.corto, a.nombre):
            nn = normalizar(n)
            if not nn:
                continue
            if len(nn.replace(" ", "")) >= 5:
                i = pegado.find(nn.replace(" ", ""))
            else:
                i = pegado.find(nn) if nn in palabras else -1
            if i >= 0:
                pos = i if pos < 0 else min(pos, i)
        if pos >= 0:
            hallados.append((pos, a))
    hallados.sort(key=lambda h: h[0])
    fuera: List[Agente] = []
    for _p, a in hallados:
        if a not in fuera:
            fuera.append(a)
    return fuera


def equipo_desde_pantalla(palabras: Sequence[object], catalogo: Sequence[Agente]) -> List[Agente]:
    """Los agentes nombrados en una pantalla de equipo, de IZQUIERDA A DERECHA.

    Es la vía fiable para saber quién va: por la cara no se puede (S72, ni con
    iconos de la wiki ni con modelos de visión). Y el orden no es adorno: en la
    pantalla de equipo van en el orden en que entran al relevo.

    Las palabras son las del OCR (`jugador.Palabra`: texto y centro). Dos
    trampas cubiertas: un nombre de dos palabras («Ye Shunguang») llega partido
    en dos cajas, así que se prueban también parejas de la misma línea; y dos
    nombres seguidos («Lucia» junto a «Miyabi») no se mezclan, porque de una
    pareja sólo cuenta lo que no estaba ya en cada palabra por separado.
    """
    items: List[Tuple[float, float, str]] = []
    for p in palabras or []:
        texto = (getattr(p, "texto", "") or "").strip()
        centro = getattr(p, "centro", None)
        if texto and centro is not None:
            items.append((float(centro.x), float(centro.y), texto))
    hallados: Dict[str, Tuple[float, Agente]] = {}

    def apuntar(agentes: Sequence[Agente], x: float) -> None:
        for a in agentes:
            if a.nombre not in hallados or x < hallados[a.nombre][0]:
                hallados[a.nombre] = (x, a)

    solos = [agentes_en_texto(t, catalogo) for _x, _y, t in items]
    for i, (x, y, texto) in enumerate(items):
        apuntar(solos[i], x)
        # La palabra más cercana a su derecha en la misma línea.
        vecina = min(((x2 - x, j) for j, (x2, y2, _t) in enumerate(items)
                      if j != i and abs(y2 - y) < 0.03 and 0 < x2 - x < 0.15), default=None)
        if vecina is None:
            continue
        j = vecina[1]
        ya = {a.nombre for a in solos[i]} | {a.nombre for a in solos[j]}
        apuntar([a for a in agentes_en_texto(texto + " " + items[j][2], catalogo)
                 if a.nombre not in ya], x)
    return [a for _x, a in sorted(hallados.values(), key=lambda h: h[0])]


# ─────────────────────────────── la guía ───────────────────────────────

def extraer_indice_prydwen(pagina: str) -> Dict[str, str]:
    """El índice de personajes de Prydwen → {dirección: nombre que muestra}."""
    indice: Dict[str, str] = {}
    for m in re.finditer(r'(?is)<a[^>]+href="/zenless/characters/([a-z0-9-]+)"[^>]*>(.*?)</a>',
                         pagina or ""):
        if m.group(1) in indice:
            continue
        alt = re.search(r'alt="([^"]+)"', m.group(2))
        visible = _una_linea(alt.group(1)) if alt else _una_linea(m.group(2))
        if visible:
            indice[m.group(1)] = visible
    return indice


def slug_prydwen(agente: Agente, indice: Dict[str, str]) -> str:
    """En qué página de Prydwen está ese agente. "" si no la hay.

    Los nombres no coinciden letra a letra entre las dos webs, y los casos
    difíciles son de verdad, no de manual: «Soldier 0 - Anby» es «Anby: Soldier
    0» (y no «Anby»), «Starlight - Billy Kid» es «Billy - Starlight» (y no
    «Billy»), y «Lucia» y «Lucy» son dos personajes distintos.
    """
    propios = [normalizar(n) for n in (agente.corto, agente.nombre) if n]
    mias = _fichas(agente.nombre) | _fichas(agente.corto)
    mejor, nota_mejor = "", (0, 0)
    for slug, visible in indice.items():
        suyas = _fichas(visible)
        if not suyas:
            continue
        comunes = suyas & mias
        if normalizar(visible) in propios:
            nota = 3
        elif suyas in (_fichas(agente.nombre), _fichas(agente.corto)):
            nota = 2
        elif (suyas <= _fichas(agente.nombre)
              or (agente.corto and _fichas(agente.corto) <= suyas)) \
                and any(len(p) >= 4 for p in comunes):
            nota = 1
        else:
            continue
        # A igual nota gana la que comparte más palabras: «Billy - Starlight»
        # le gana a «Billy» para «Starlight - Billy Kid».
        if (nota, len(comunes)) > nota_mejor:
            mejor, nota_mejor = slug, (nota, len(comunes))
    return mejor


# Las secciones de una guía de Prydwen que dicen CÓMO SE JUEGA. Lo demás
# —motores, discos, estadísticas— es para montar al personaje, no para jugarlo.
# Medido sobre páginas reales (10 sep 2026): Yixuan trae «How to play»,
# Miyabi «Fight Opener / Standard Combo / Ultimate Combo», Lucia «Key
# Mechanics» y «Buffs & How to access them», Nangong Yu «Chain & Ultimate».
_TITULOS_DE_JUEGO = re.compile(
    r"(?i)how to play|rotation|combo|opener|playstyle|key mechanic|how to access"
    r"|chain\s*&\s*ultimate")

# Lo que cabe de cada sección. Un combo entero son 6-10 pasos cortos.
TOPE_SECCION = 900


# Cada equipo de «Teams (Shiyu Defense)»: «Rank 4 · App. rate: 15.57 % · Avg.
# Score» y las tres imágenes de sus miembros (`alt`). Formato medido en las
# páginas de Yixuan y Miyabi el 10 sep 2026.
_BLOQUE_EQUIPO_RE = re.compile(
    r"(?is)Rank\W+(\d+).{0,400}?App\.\s*rate:\W+([\d.]+)(.{0,1500}?)(?=Rank\W+\d+|\Z)")


def extraer_equipos_usados(cuerpo: str, modo: str, tope: int = 15) -> List[Dict[str, object]]:
    """Los equipos que MÁS SE USAN con un personaje, con su porcentaje de uso.

    Es el dato que faltaba para hacer equipos buenos y no sólo «válidos»: a Enzo
    le salió Yixuan + Lucia + Miyabi, que cumplía todas las reglas sueltas —dos
    que hacen daño, un apoyo, tres pasivas activas— y nadie lo juega: con Yixuan
    se usa Dialyn o Ju Fufu (15,6 % y 6,4 %), y Miyabi va con Nangong Yu y Yuzuha.
    """
    t = _html.unescape(re.sub(r"<(?!img)[^>]+>", " ", cuerpo or ""))
    fuera: List[Dict[str, object]] = []
    for m in _BLOQUE_EQUIPO_RE.finditer(t):
        alts = [_una_linea(x) for x in re.findall(r'(?i)\balt="([^"]+)"', m.group(0))]
        alts = [x for x in dict.fromkeys(alts) if x][:3]
        if len(alts) == 3:
            try:
                fuera.append({"modo": modo, "rank": int(m.group(1)),
                              "uso": float(m.group(2)), "miembros": alts})
            except ValueError:
                continue
        if len(fuera) >= tope:
            break
    return fuera


def extraer_guia_prydwen(pagina: str) -> Dict[str, list]:
    """Página de un personaje en Prydwen → {"como": [(título, texto)], "sinergias": [...]}.

    Prydwen parte la página en secciones `<div class="content-header">` y, dentro
    de algunas, subsecciones `<h5>`. Se cortan por ahí y se quedan las que
    enseñan a jugar. Las sinergias son las imágenes (`alt`) de la sección
    «Synergy», que es donde la guía nombra a los compañeros que le van bien.
    """
    t = _limpiar_html(pagina or "")
    cortes: List[Tuple[int, int, str, str]] = []
    for m in re.finditer(r'(?is)<div class="content-header[^"]*">(.*?)</div>', t):
        cortes.append((m.start(), m.end(), _una_linea(m.group(1)), "seccion"))
    for m in re.finditer(r"(?is)<h([5-6])[^>]*>(.*?)</h\1>", t):
        cortes.append((m.start(), m.end(), _una_linea(m.group(2)), "sub"))
    cortes.sort()

    como: List[Tuple[str, str]] = []
    sinergias: List[str] = []
    usados: List[Dict[str, object]] = []
    tomado_hasta = -1
    for i, (ini, fin_titulo, titulo, nivel) in enumerate(cortes):
        if nivel == "seccion":
            fin = next((c[0] for c in cortes[i + 1:] if c[3] == "seccion"), len(t))
        else:
            fin = cortes[i + 1][0] if i + 1 < len(cortes) else len(t)
        cuerpo = t[fin_titulo:fin]
        if nivel == "seccion" and titulo.lower().startswith("teams ("):
            modo = titulo[titulo.find("(") + 1:titulo.rfind(")")].strip() or titulo
            usados += extraer_equipos_usados(cuerpo, modo)
            continue
        if nivel == "seccion" and titulo.lower().startswith("synerg"):
            for alt in re.findall(r'(?i)\balt="([^"]+)"', cuerpo):
                nombre = _una_linea(alt)
                if nombre and nombre not in sinergias:
                    sinergias.append(nombre)
            continue
        if ini < tomado_hasta or not _TITULOS_DE_JUEGO.search(titulo):
            continue
        texto = "\n".join(_lineas(cuerpo))[:TOPE_SECCION].strip()
        if len(texto) >= 20:
            como.append((titulo, texto))
            tomado_hasta = fin
    return {"como": como, "sinergias": sinergias, "pasiva": extraer_pasiva(pagina),
            "equipos_usados": usados}


# ─────────────────────────────── la rotación ───────────────────────────────

# Qué papel cumple cada especialidad dentro de una vuelta. Esto sí vive en el
# código: es cómo funciona el combate del juego (aturdir → castigar), no una
# lista de personajes, y no cambia con los parches.
PAPEL = {"stun": "aturdidor", "attack": "daño", "anomaly": "daño", "rupture": "daño",
         "support": "apoyo", "defense": "defensa"}

ESPECIALIDAD_ES = {"attack": "ataque", "stun": "aturdimiento", "anomaly": "anomalía",
                   "support": "apoyo", "defense": "defensa", "rupture": "ruptura"}

# Cuánto está cada uno en el campo, de salida. Son el PUNTO DE PARTIDA de la
# práctica, no la respuesta: lo que ella mida jugando es lo que los ajusta.
SEG_APOYO = 3.0
SEG_ATURDIDOR = 8.0
SEG_SECUNDARIO = 6.0
SEG_PRINCIPAL = 12.0


@dataclass
class Tramo:
    """El rato que un agente está en el campo dentro de una vuelta."""
    agente: str
    papel: str
    segundos: float
    habilidad: bool = True        # usar su EX en cuanto se encienda
    definitiva: bool = False      # usar la definitiva si está lista
    que_hace: str = ""


@dataclass
class Rotacion:
    equipo: List[Agente] = field(default_factory=list)
    tramos: List[Tramo] = field(default_factory=list)
    principal: str = ""

    def resumen(self) -> str:
        """Una línea para el registro: «Lucia (apoyo, 3 s) → … »."""
        return " → ".join(
            f"{t.agente} ({'principal' if t.agente == self.principal else t.papel or '¿?'}, "
            f"{t.segundos:.0f} s)" for t in self.tramos)

    def para_el_prompt(self) -> str:
        if not self.equipo:
            return ""
        lineas = ["TU EQUIPO, en el orden del relevo (el botón de relevo pasa al siguiente):"]
        for i, a in enumerate(self.equipo, 1):
            esp = ESPECIALIDAD_ES.get(a.especialidad.lower(), a.especialidad.lower() or "papel desconocido")
            marca = ", daño PRINCIPAL" if a.llamado == self.principal else ""
            lineas.append(f"{i}. {a.llamado} — {esp}{marca}")
        lineas.append("UNA VUELTA de este equipo:")
        for t in self.tramos:
            lineas.append(f"- {t.agente} (~{t.segundos:.0f} s): {t.que_hace}")
        lineas.append("Si el botón de relevo enseña «ASSIST», púlsalo en el acto: es una asistencia.")
        return "\n".join(lineas)


def plan_de_rotacion(equipo: Sequence[Agente], principal: str = "") -> Rotacion:
    """El orden en que se juega un equipo, deducido de las especialidades.

    El relevo sólo AVANZA —del primero al segundo, del segundo al tercero y
    vuelta al primero (medido en el móvil de Enzo, 10 sep 2026)—, así que la
    vuelta no se inventa un orden: sigue el del equipo y sólo decide por quién
    se empieza. Se abre con quien aturde; sin aturdidor, justo después del daño
    principal, para que éste llegue el último con todo lo de los demás encima.
    """
    agentes = [a for a in equipo if a and a.nombre]
    if not agentes:
        return Rotacion()
    papeles = [PAPEL.get((a.especialidad or "").lower(), "") for a in agentes]
    daño = [i for i, p in enumerate(papeles) if p == "daño"]
    # Si se sabe quién hace el daño fuerte (por los equipos que se juegan), manda
    # eso. Si no, el primero de los de daño — que con dos de Anomalía elegía mal:
    # a Yanagi en vez de Miyabi, en la primera práctica con ese equipo.
    elegido = next((i for i in daño if principal and principal in (agentes[i].nombre, agentes[i].llamado)), None)
    principal = elegido if elegido is not None else (daño[0] if daño else 0)
    aturdidor = next((i for i, p in enumerate(papeles) if p == "aturdidor"), None)
    inicio = aturdidor if aturdidor is not None else (principal + 1) % len(agentes)

    tramos: List[Tramo] = []
    for k in range(len(agentes)):
        i = (inicio + k) % len(agentes)
        a, p = agentes[i], papeles[i]
        if i == principal:
            tramos.append(Tramo(a.llamado, p or "daño", SEG_PRINCIPAL, True, True,
                                "el daño fuerte: sus EX y la definitiva, mejor con el enemigo aturdido"))
        elif p == "aturdidor":
            tramos.append(Tramo(a.llamado, p, SEG_ATURDIDOR, True, False,
                                "llena el aturdimiento: ataques seguidos y su EX"))
        elif p in ("apoyo", "defensa"):
            tramos.append(Tramo(a.llamado, p, SEG_APOYO, True, False,
                                "entra, usa su EX para potenciar al equipo y cambia"))
        elif p == "daño":
            tramos.append(Tramo(a.llamado, p, SEG_SECUNDARIO, True, False,
                                "daño de acompañamiento: acumula y deja sitio al principal"))
        else:
            tramos.append(Tramo(a.llamado, "", SEG_SECUNDARIO, True, False,
                                "no sé su papel: ataca, usa su EX y cambia"))
    return Rotacion(list(agentes), tramos, agentes[principal].llamado)


def _consejo(guia: Dict) -> str:
    """Lo primero que dice la guía sobre cómo se juega, en una o dos frases."""
    for _titulo, texto in guia.get("como") or []:
        primera = texto.split("\n")[0]
        frases = re.split(r"(?<=[.!?])\s+", primera)
        corto = " ".join(frases[:2]).strip()
        if len(corto) >= 30:
            return corto[:260]
    return ""


# ─────────────────────────────── hacer equipos ───────────────────────────────
#
# Enzo, 10 sep 2026: «necesita saber hacer equipos, todas las opciones de los
# equipos, y ya entrar al combate sabiendo qué personajes usa». Es la salida
# buena a lo que no funcionó ese día: reconocer a los agentes por la cara en
# plena pelea. Si el equipo lo elige ella, sabe quién va antes de entrar.
#
# El criterio es código, no opinión del modelo, y cada punto de la nota lleva
# su razón escrita para poder discutirla.

# Lo que la guía escribe tras «Additional Ability» —el nombre de la pasiva— y
# la condición: «When another character in your squad is a Stun, Support, or
# Defense character:», «… shares the same Attribute or Faction:». Formato
# medido en 8 páginas de Prydwen el 10 sep 2026.
_PASIVA_RE = re.compile(
    r"(?is)Additional Ability\s*(.{0,120}?)\s*When another character in your squad\s*(.{0,200}?):")
_ESPECIALIDADES = ("attack", "stun", "anomaly", "support", "defense", "rupture")


def extraer_pasiva(pagina: str) -> Dict[str, object]:
    """La condición de la pasiva de equipo de un agente. {} si la página no la trae."""
    t = _una_linea(_limpiar_html(pagina or ""))
    m = _PASIVA_RE.search(t)
    if not m:
        return {}
    cond = " ".join(m.group(2).split())
    bajo = cond.lower()
    return {"nombre": " ".join(m.group(1).split())[:60],
            "especialidades": [e for e in _ESPECIALIDADES if re.search(rf"\b{e}\b", bajo)],
            # «same Attribute or Faction» son las dos cosas, no sólo la primera.
            "misma_faccion": bool(re.search(r"same (?:attribute (?:or|and) )?faction", bajo)),
            "mismo_atributo": bool(re.search(r"same (?:faction (?:or|and) )?attribute", bajo)),
            "condicion": "When another character in your squad " + cond}


def pasiva_activa(agente: Agente, pasiva: Dict, miembros: Sequence[Agente]) -> bool:
    """¿Se le activa a `agente` su pasiva de equipo con estos compañeros?"""
    if not pasiva:
        return False
    for c in miembros:
        if c is agente or c.nombre == agente.nombre:
            continue
        if (c.especialidad or "").lower() in (pasiva.get("especialidades") or []):
            return True
        if pasiva.get("misma_faccion") and c.faccion and c.faccion == agente.faccion:
            return True
        if pasiva.get("mismo_atributo") and c.atributo and c.atributo == agente.atributo:
            return True
    return False


@dataclass
class OpcionDeEquipo:
    miembros: List[Agente] = field(default_factory=list)
    nota: float = 0.0
    razones: List[str] = field(default_factory=list)
    meta: str = ""
    pasivas_activas: List[str] = field(default_factory=list)
    principal: str = ""        # quien hace el daño fuerte, según los equipos que se juegan

    def nombres(self) -> List[str]:
        return [a.llamado for a in self.miembros]

    def resumen(self) -> str:
        return f"{' + '.join(self.nombres())} — nota {self.nota:.0f}: " + "; ".join(self.razones)


# Lo que vale cada cosa. Pesos de criterio, a la vista para discutirlos.
#
# 🔴 Primera versión (10 sep 2026) y por qué se cambió: puntuaba reglas sueltas
# —hay daño, hay apoyo, se activan pasivas— y la mejor opción salió Yixuan +
# Lucia + Miyabi. Enzo: «¿qué equipo de mierda es?». Con razón: Miyabi es de
# Anomalía y Lucia apoya a la Ruptura, no hay aturdidor, y la pasiva de Miyabi
# se activa con cualquier apoyo, así que «3 de 3 activas» no decía nada. Ahora
# manda lo que se USA de verdad, y las reglas sólo desempatan y avisan.
NOTA_USADO = 60          # el trío exacto se juega en Shiyu Defense / Deadly Assault
NOTA_USO_POR_PUNTO = 2   # y más cuanto más se usa (por punto de %), hasta 40
NOTA_GUIA = 40           # el trío exacto que recomienda una guía
NOTA_GUIA_DOS = 8        # dos de los tres de un equipo de guía
NOTA_SINERGIA = 8        # la guía de uno nombra a otro del trío
NOTA_SIN_DAÑO = -60      # sin nadie que haga daño no hay equipo
NOTA_DAÑO = 20
NOTA_ATURDIDOR = 10
NOTA_APOYO = 10
NOTA_TRES_DAÑO = -8      # tres de daño y nadie que aturda ni apoye
NOTA_DOS_ATURDIDORES = -6
NOTA_PASIVA = 4          # se activan con demasiado poco para ser sinergia
NOTA_ESTILOS_CRUZADOS = -25   # dos de daño de estilos distintos que ninguna guía junta
NOTA_APOYO_QUE_NO_ENCAJA = -10  # el apoyo potencia otro estilo que el del daño
NOTA_SIN_ATURDIDOR = -10      # ataque o ruptura sin nadie que aturda

# Y cuando no se tienen los personajes de los equipos que se juegan. Enzo, 10 sep
# 2026: «¿y si no tengo los personajes de los equipos más usados?». Un equipo
# usado no es sólo tres nombres: dice qué HUECO cubre cada uno. Yixuan + Dialyn +
# Lucia es «Yixuan, un aturdidor y el apoyo de Ruptura»; sin Dialyn, otro aturdidor
# que se tenga hace ese papel. Se puntúa lo que se PARECE, por debajo de lo que se
# juega tal cual, porque eso está probado y lo sustituido no.
NOTA_PARECIDO = 30            # × similitud, más el uso (hasta 20)
PARECIDO_MINIMO = 0.6         # por debajo, no se parece a nada
# Uso mínimo para que un equipo cuente como «se juega»: como modelo, como trío
# probado o para decir «te falta». Las listas de Prydwen llegan hasta equipos del
# 0,04 %, y con uno así —Miyabi + Yixuan + Astra, 0,0 %— el equipo malo volvía a
# colarse como «parecido a uno que se usa». Eso no lo usa nadie.
USO_MINIMO = 0.5
SIM_MISMO = 1.0               # el mismo personaje
SIM_MISMA_ESPECIALIDAD = 0.5  # otro, pero cubre el mismo hueco
SIM_MISMO_ATRIBUTO = 0.15
SIM_MISMA_FACCION = 0.15


def _juntos(a: Agente, b: Agente, sinergias: Dict[str, Sequence[str]]) -> bool:
    """¿La guía de uno nombra al otro? En cualquiera de los dos sentidos."""
    def nombra(x: Agente, y: Agente) -> bool:
        return any(normalizar(s) in (normalizar(y.llamado), normalizar(y.nombre))
                   for s in sinergias.get(x.nombre) or [])
    return nombra(a, b) or nombra(b, a)


def evaluar_equipo(miembros: Sequence[Agente], pasivas: Dict[str, Dict],
                   equipos_guia: Sequence[Tuple[str, Sequence[str]]],
                   sinergias: Dict[str, Sequence[str]],
                   usos: Optional[Dict[frozenset, Tuple[float, str]]] = None,
                   plantillas: Optional[Sequence[Tuple[List[Agente], float, str]]] = None) -> OpcionDeEquipo:
    """La nota de un trío y, sobre todo, POR QUÉ. Sin modelo."""
    from itertools import combinations
    miembros = list(miembros)
    papel = {a.nombre: PAPEL.get((a.especialidad or "").lower(), "") for a in miembros}
    de = lambda p: [a for a in miembros if papel[a.nombre] == p]
    nota, razones = 0.0, []

    principal = ""
    uso = (usos or {}).get(frozenset(a.nombre for a in miembros))
    if uso:
        nota += NOTA_USADO + min(40.0, NOTA_USO_POR_PUNTO * uso[0])
        razones.append(f"se juega de verdad: {uso[0]:.1f} % de uso en {uso[1]}")
        principal = uso[2] if len(uso) > 2 else ""
    elif plantillas:
        mejor = None
        for usado, uso_p, modo in plantillas:
            sim, pares = parecido_de_trios(miembros, usado, pasivas)
            if sim < PARECIDO_MINIMO:
                continue
            puntos = sim * (NOTA_PARECIDO + min(20.0, uso_p))
            if mejor is None or puntos > mejor[0]:
                mejor = (puntos, sim, usado, uso_p, modo, pares)
        if mejor is not None:
            puntos, sim, usado, uso_p, modo, pares = mejor
            nota += puntos
            # El que ocupa el hueco del primero del equipo usado hace de principal.
            principal = next((m.nombre for u, m in pares if u.nombre == usado[0].nombre), "")
            cambios = [f"{m.llamado} en lugar de {u.llamado}" for u, m in pares if u.nombre != m.nombre]
            razones.append(f"se parece a {' + '.join(x.llamado for x in usado)} ({uso_p:.1f} % en {modo}): "
                           + ("; ".join(cambios) + " (mismo papel)" if cambios else "los mismos"))

    daño, aturden = de("daño"), de("aturdidor")
    apoyan = de("apoyo") + de("defensa")
    if not daño:
        nota += NOTA_SIN_DAÑO
        razones.append("nadie hace daño")
    else:
        nota += NOTA_DAÑO
        razones.append(f"daño: {', '.join(a.llamado for a in daño)}")
    if aturden:
        nota += NOTA_ATURDIDOR + (NOTA_DOS_ATURDIDORES if len(aturden) > 1 else 0)
        razones.append(f"aturde: {', '.join(a.llamado for a in aturden)}")
    if apoyan:
        nota += NOTA_APOYO
        razones.append(f"apoya: {', '.join(a.llamado for a in apoyan)}")
    if len(daño) == len(miembros) and len(miembros) == 3:
        nota += NOTA_TRES_DAÑO
        razones.append("nadie aturde ni apoya")

    # Estilos que no se ayudan: Ruptura con Anomalía, Ataque con Anomalía…
    esp = lambda a: (a.especialidad or "").lower()
    for a, b in combinations(daño, 2):
        if esp(a) != esp(b) and not _juntos(a, b, sinergias):
            nota += NOTA_ESTILOS_CRUZADOS
            razones.append(f"{a.llamado} ({ESPECIALIDAD_ES.get(esp(a), esp(a))}) y {b.llamado} "
                           f"({ESPECIALIDAD_ES.get(esp(b), esp(b))}) no se ayudan")
    if any(esp(a) in ("attack", "rupture") for a in daño) and not aturden:
        nota += NOTA_SIN_ATURDIDOR
        razones.append("ataque o ruptura sin nadie que aturda")
    # Un apoyo cuya pasiva pide otro estilo de daño no está potenciando a nadie.
    for ap in apoyan:
        quiere = set((pasivas.get(ap.nombre) or {}).get("especialidades") or []) - {"stun", "support", "defense"}
        if not quiere or not daño:
            continue
        fuera = [d for d in daño if esp(d) not in quiere and not _juntos(ap, d, sinergias)]
        if len(fuera) == len(daño):
            nota += NOTA_APOYO_QUE_NO_ENCAJA
            razones.append(f"{ap.llamado} potencia a otro estilo, no a {', '.join(d.llamado for d in daño)}")
        elif fuera:
            # Ya resta el cruce de estilos; aquí sólo se dice a quién deja fuera.
            razones.append(f"{ap.llamado} no potencia a {', '.join(d.llamado for d in fuera)}")

    activas = [a.llamado for a in miembros if pasiva_activa(a, pasivas.get(a.nombre) or {}, miembros)]
    nota += NOTA_PASIVA * len(activas)
    sin_dato = [a.llamado for a in miembros if not pasivas.get(a.nombre)]
    texto = f"pasivas de equipo activas: {len(activas)} de {len(miembros)}"
    if activas:
        texto += f" ({', '.join(activas)})"
    if sin_dato:
        texto += f"; sin datos de {', '.join(sin_dato)}"
    razones.append(texto)

    propios = {a.nombre for a in miembros}
    meta, dos = "", ""
    for nombre_equipo, nombres in equipos_guia:
        comunes = len(propios & set(nombres))
        if comunes == len(miembros) == len(set(nombres)):
            meta = nombre_equipo
            break
        if comunes == 2 and not dos:
            dos = nombre_equipo
    if meta:
        nota += NOTA_GUIA
        razones.append(f"equipo de la guía: «{meta}»")
    elif dos:
        nota += NOTA_GUIA_DOS
        razones.append(f"dos de los tres de «{dos}»")

    llamados = {normalizar(a.llamado): a for a in miembros}
    llamados.update({normalizar(a.nombre): a for a in miembros})
    pares = []
    for a in miembros:
        for s_ in sinergias.get(a.nombre) or []:
            b = llamados.get(normalizar(s_))
            if b is not None and b.nombre != a.nombre:
                pares.append(f"{a.llamado}→{b.llamado}")
    if pares:
        nota += NOTA_SINERGIA * len(pares)
        razones.append(f"la guía los junta: {', '.join(pares)}")
    return OpcionDeEquipo(miembros, nota, razones, meta, activas, principal)


def _parecido_de(a: Agente, b: Agente, pasivas: Optional[Dict[str, Dict]] = None,
                 estilos_de_daño: Sequence[str] = ()) -> float:
    """Cuánto hace `b` el papel de `a`. 0 si son de especialidad distinta.

    ⚠️ Dos apoyos no son intercambiables por ser apoyos: Lucia potencia la
    Ruptura y Yuzuha la Anomalía. Con la primera versión, Lucia salía «en lugar
    de Yuzuha» en equipos de Miyabi —el mismo error del «¿qué equipo de mierda
    es?» con otra cara—. Un apoyo sólo cubre el hueco si su pasiva pide el
    estilo de daño del equipo donde entra.
    """
    if a.nombre == b.nombre:
        return SIM_MISMO
    if not a.especialidad or (a.especialidad or "").lower() != (b.especialidad or "").lower():
        return 0.0
    if PAPEL.get((b.especialidad or "").lower()) in ("apoyo", "defensa") and pasivas:
        quiere = set((pasivas.get(b.nombre) or {}).get("especialidades") or []) - {"stun", "support", "defense"}
        if quiere and estilos_de_daño and not quiere & set(estilos_de_daño):
            return 0.0
    return (SIM_MISMA_ESPECIALIDAD + (SIM_MISMO_ATRIBUTO if a.atributo and a.atributo == b.atributo else 0.0)
            + (SIM_MISMA_FACCION if a.faccion and a.faccion == b.faccion else 0.0))


def parecido_de_trios(mio: Sequence[Agente], usado: Sequence[Agente],
                      pasivas: Optional[Dict[str, Dict]] = None) -> Tuple[float, List[Tuple[Agente, Agente]]]:
    """Cuánto se parece un trío a un equipo que se juega, y quién hace de quién.

    Se prueba cada forma de emparejar a los tres y se queda la mejor: el orden en
    que vengan no importa. Devuelve (0-1, [(el del equipo usado, el mío)]).
    """
    from itertools import permutations
    mio, usado = list(mio), list(usado)
    if len(mio) != len(usado) or not mio:
        return 0.0, []
    estilos = [(x.especialidad or "").lower() for x in mio
               if PAPEL.get((x.especialidad or "").lower()) == "daño"]
    mejor, pares = 0.0, []
    for orden in permutations(mio):
        sims = [_parecido_de(u, m, pasivas, estilos) for u, m in zip(usado, orden)]
        if 0.0 in sims:
            continue
        total = sum(sims) / len(sims)
        if total > mejor:
            mejor, pares = total, list(zip(usado, orden))
    return mejor, pares


def plantillas_de_uso(guias: Dict[str, Dict], catalogo: Sequence[Agente]) -> List[Tuple[List[Agente], float, str]]:
    """Los equipos usados con sus AGENTES del catálogo (para ver qué hueco cubre cada uno)."""
    fuera: Dict[frozenset, Tuple[List[Agente], float, str]] = {}
    for g in guias.values():
        for e in (g or {}).get("equipos_usados") or []:
            agentes = [buscar_agente(n, catalogo) for n in e.get("miembros") or []]
            if len(agentes) != 3 or any(x is None for x in agentes):
                continue
            clave = frozenset(x.nombre for x in agentes)
            uso = float(e.get("uso") or 0)
            if uso < USO_MINIMO:
                continue
            if len(clave) == 3 and uso > fuera.get(clave, ([], -1.0, ""))[1]:
                fuera[clave] = (agentes, uso, str(e.get("modo") or ""))
    return sorted(fuera.values(), key=lambda t: -t[1])


def usos_de_equipos(guias: Dict[str, Dict], catalogo: Sequence[Agente]) -> Dict[frozenset, Tuple[float, str]]:
    """Los equipos usados de las guías, con los nombres llevados al catálogo. Uso máximo por trío."""
    fuera: Dict[frozenset, Tuple[float, str]] = {}
    for g in guias.values():
        for e in (g or {}).get("equipos_usados") or []:
            agentes = [buscar_agente(n, catalogo) for n in e.get("miembros") or []]
            if len(agentes) != 3 or any(a is None for a in agentes):
                continue
            clave = frozenset(a.nombre for a in agentes)
            uso = float(e.get("uso") or 0)
            if uso < USO_MINIMO:
                continue
            if len(clave) == 3 and uso > fuera.get(clave, (-1.0, ""))[0]:
                # El PRIMERO de la lista es el que hace el daño fuerte: «Miyabi,
                # Yanagi, Astra Yao», «Yixuan, Dialyn, Lucia», «Remielle, Miyabi,
                # Velina» (medido en Prydwen, 10 sep 2026). Prydwen llama «Anomaly
                # DPS» a Miyabi y a Yanagi por igual, así que el papel no lo dice.
                fuera[clave] = (uso, str(e.get("modo") or ""), agentes[0].nombre)
    return fuera


def lo_que_falta(plantilla: Sequence[Agente], usos: Dict[frozenset, Tuple[float, str]],
                 cuantos: int = 6) -> List[Tuple[str, float, List[str]]]:
    """Para los equipos que más se usan en los que YA tiene a dos: quién falta.

    [(el que falta, % de uso, el trío)], de más usado a menos, sin repetir a quién falta.
    """
    tengo = {a.nombre for a in plantilla}
    fuera: Dict[str, Tuple[str, float, List[str]]] = {}
    for trio, (uso, *_resto) in usos.items():
        falta = [n for n in trio if n not in tengo]
        if len(falta) == 1 and uso > fuera.get(falta[0], ("", -1.0, []))[1]:
            fuera[falta[0]] = (falta[0], uso, sorted(trio))
    return sorted(fuera.values(), key=lambda f: -f[1])[:cuantos]


def opciones_de_equipo(plantilla: Sequence[Agente], pasivas: Dict[str, Dict],
                       equipos_guia: Sequence[Tuple[str, Sequence[str]]],
                       sinergias: Dict[str, Sequence[str]], cuantas: int = 10,
                       usos: Optional[Dict[frozenset, Tuple[float, str]]] = None,
                       plantillas: Optional[Sequence[Tuple[List[Agente], float, str]]] = None) -> List[OpcionDeEquipo]:
    """TODOS los tríos posibles con la plantilla, de mejor a peor. Los `cuantas` primeros."""
    from itertools import combinations
    vistos = {}
    for trio in combinations(plantilla, 3):
        clave = tuple(sorted(a.nombre for a in trio))
        if clave not in vistos:
            vistos[clave] = evaluar_equipo(trio, pasivas, equipos_guia, sinergias, usos, plantillas)
    ops = sorted(vistos.values(), key=lambda o: (-o.nota, sorted(o.nombres())))
    return ops[:cuantas] if cuantas else ops


def orden_para_elegir(miembros: Sequence[Agente], principal: str = "") -> List[Agente]:
    """En qué orden se colocan en la pantalla de equipo: el de su vuelta.

    El primero es quien empieza la pelea, y el relevo sigue ese orden. Por eso
    se colocan como dice `plan_de_rotacion`, no como vengan.
    """
    rot = plan_de_rotacion(miembros, principal)
    por_llamado = {a.llamado: a for a in miembros}
    orden = [por_llamado[t.agente] for t in rot.tramos if t.agente in por_llamado]
    return orden or list(miembros)


# ─────────────────────────────── con memoria ───────────────────────────────

class SaberDeEquipo:
    """Catálogo, guías y rotación, guardados en disco con fecha.

    Todo lo que sale a la red entra por el constructor, igual que en
    `zzz.SaberZZZ`: es lo que deja probarlo sin internet y comprobar que una
    fuente caída se nota en vez de rellenarse con nada.
    """

    def __init__(self, cache: Optional[str] = None,
                 descargar: Optional[Callable[[str], str]] = None,
                 ahora: Optional[Callable[[], float]] = None):
        self.cache = Path(cache) if cache else MEM_DIR / "zzz"
        self._descargar = descargar or _descargar
        self._ahora = ahora or time.time

    # -- disco -----------------------------------------------------------
    def _fichero(self, clave: str) -> Path:
        return self.cache / (re.sub(r"[^a-z0-9_-]+", "_", clave.lower()) + ".json")

    def _leer(self, clave: str) -> Optional[dict]:
        try:
            f = self._fichero(clave)
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except Exception as e:
            logger.warning("equipo: caché ilegible %s (%s)", clave, e)
            return None

    def _guardar(self, clave: str, datos: dict) -> None:
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            self._fichero(clave).write_text(json.dumps(datos, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        except Exception as e:
            logger.warning("equipo: no pude guardar %s (%s)", clave, e)

    def _fresco(self, datos: Optional[dict]) -> bool:
        if not datos:
            return False
        return (self._ahora() - float(datos.get("obtenido") or 0)) / 86400 < DIAS_FRESCURA

    # -- catálogo --------------------------------------------------------
    def catalogo(self, refrescar: bool = False) -> List[Agente]:
        guardado = self._leer("agentes")
        if not refrescar and self._fresco(guardado):
            return [Agente(**a) for a in guardado.get("agentes", [])]
        url = WIKI_API + "?" + urllib.parse.urlencode(
            {"action": "parse", "page": "Agent/List", "prop": "text", "format": "json"})
        try:
            pagina = json.loads(self._descargar(url) or "{}")["parse"]["text"]["*"]
        except (ValueError, KeyError, TypeError):
            pagina = ""
        agentes = extraer_agentes(pagina)
        if not agentes:
            # Vacío NO se guarda: envenenaría la caché una semana.
            logger.warning("equipo: la wiki no dio la lista de agentes")
            return [Agente(**a) for a in (guardado or {}).get("agentes", [])]
        cortos: Dict[str, str] = {}
        nombres = [a.nombre for a in agentes]
        for i in range(0, len(nombres), 40):
            url = WIKI_API + "?" + urllib.parse.urlencode(
                {"action": "query", "prop": "revisions", "rvprop": "content",
                 "rvslots": "main", "titles": "|".join(nombres[i:i + 40]),
                 "format": "json", "formatversion": "2"})
            try:
                datos = json.loads(self._descargar(url) or "{}")
            except ValueError:
                continue
            for p in datos.get("query", {}).get("pages", []) or []:
                revs = p.get("revisions") or []
                texto = revs[0].get("slots", {}).get("main", {}).get("content", "") if revs else ""
                cortos[p.get("title", "")] = nombre_corto(texto)
        for a in agentes:
            a.corto = cortos.get(a.nombre, "")
        self._guardar("agentes", {"obtenido": self._ahora(),
                                  "fuente": "zenless-zone-zero.fandom.com · Agent/List",
                                  "agentes": [asdict(a) for a in agentes]})
        return agentes

    def agente(self, texto: str) -> Optional[Agente]:
        return buscar_agente(texto, self.catalogo())

    def equipo(self, nombres: Sequence[str]) -> List[Agente]:
        """Nombres → agentes. Uno que no esté en el catálogo va sin papel, pero va."""
        cat = self.catalogo()
        return [buscar_agente(n, cat) or Agente(nombre=n) for n in nombres if n]

    # -- guías -----------------------------------------------------------
    def indice_prydwen(self, refrescar: bool = False) -> Dict[str, str]:
        guardado = self._leer("prydwen_indice")
        if not refrescar and self._fresco(guardado):
            return dict(guardado.get("indice", {}))
        indice = extraer_indice_prydwen(self._descargar(f"{PRYDWEN}/zenless/characters"))
        if not indice:
            return dict((guardado or {}).get("indice", {}))
        self._guardar("prydwen_indice", {"obtenido": self._ahora(), "indice": indice})
        return indice

    def guia(self, agente: Agente, refrescar: bool = False) -> Dict:
        slug = slug_prydwen(agente, self.indice_prydwen())
        if not slug:
            return {}
        guardado = self._leer(f"guia_{slug}")
        # Una guía guardada antes de que se leyera la pasiva de equipo no vale
        # para hacer equipos: se vuelve a pedir.
        if (not refrescar and self._fresco(guardado) and "pasiva" in guardado
                and "equipos_usados" in guardado):
            return guardado
        url = f"{PRYDWEN}/zenless/characters/{slug}"
        g = extraer_guia_prydwen(self._descargar(url))
        if not g["como"] and not g["sinergias"] and not g["pasiva"] and not g["equipos_usados"]:
            return guardado or {}
        datos = {"obtenido": self._ahora(), "fuente": url, "agente": agente.nombre,
                 "como": [list(c) for c in g["como"]], "sinergias": g["sinergias"],
                 "pasiva": g["pasiva"], "equipos_usados": g["equipos_usados"]}
        self._guardar(f"guia_{slug}", datos)
        return datos

    # -- lo que va a la partida -----------------------------------------
    def plan(self, nombres: Sequence[str], principal: str = "") -> Rotacion:
        return plan_de_rotacion(self.equipo(nombres), principal)

    def ficha_para_jugar(self, nombres: Sequence[str], tope: int = 1200) -> str:
        """El equipo, su vuelta y una frase de la guía de cada uno. Para el prompt.

        Acotado a propósito: va en CADA mirada, y la cuota de Groq son 7.000
        tokens por minuto. Cada línea de más son miradas de menos.
        """
        rot = self.plan(nombres)
        partes = [rot.para_el_prompt()]
        for a in rot.equipo:
            if not a.especialidad:
                continue
            try:
                consejo = _consejo(self.guia(a))
            except Exception as e:
                logger.warning("equipo: sin guía de %s (%s)", a.nombre, e)
                consejo = ""
            if consejo:
                partes.append(f"Guía de {a.llamado}: {consejo}")
        return "\n".join(p for p in partes if p)[:tope]

    # -- hacer equipos ---------------------------------------------------
    def plantilla(self) -> List[Agente]:
        """Los agentes que TIENE Enzo.

        Primero lo leído de la LISTA DEL JUEGO, que es la única fuente fiable: el
        inventario guardado decía que tenía a Nangong Yu y al montar el equipo
        salió «no encuentro a Nangong Yu en tu lista» (17 sep). Con eso se eligió
        un equipo imposible y la partida se perdió montándolo.
        """
        from celestia_lib.seleccion_zzz import los_que_no_tengo, los_que_tengo
        from celestia_lib.zzz import SaberZZZ
        del_juego = los_que_tengo()
        # La lista del juego se lee a trozos: sólo se usa como plantilla cuando ya
        # está casi entera. Mientras, manda el inventario MENOS los que el juego no
        # encontró al montar, que es el dato seguro.
        nombres = del_juego if len(del_juego) >= 15 else SaberZZZ(cache=str(self.cache)).inventario()
        fuera = {n.lower() for n in los_que_no_tengo()}
        nombres = [n for n in nombres if str(n).lower() not in fuera]
        return [a for a in self.equipo(nombres) if a.especialidad]

    def equipos_de_las_guias(self) -> List[Tuple[str, List[str]]]:
        """Los equipos de Game8, con los nombres ya llevados al catálogo."""
        from celestia_lib.zzz import SaberZZZ
        cat = self.catalogo()
        fuera: List[Tuple[str, List[str]]] = []
        try:
            equipos = SaberZZZ(cache=str(self.cache)).todos_los_equipos()
        except Exception as e:
            logger.warning("equipo: sin equipos de las guías (%s)", e)
            return fuera
        for e in equipos:
            nombres = [a.nombre for a in (buscar_agente(m, cat) for m in e.miembros) if a]
            if len(nombres) == 3:
                fuera.append((e.nombre, nombres))
        return fuera

    def _guias_de(self, plantilla: Sequence[Agente]) -> Dict[str, Dict]:
        guias: Dict[str, Dict] = {}
        for a in plantilla:
            try:
                guias[a.nombre] = self.guia(a)
            except Exception as e:
                logger.warning("equipo: sin guía de %s (%s)", a.nombre, e)
                guias[a.nombre] = {}
        return guias

    def opciones(self, nombres: Optional[Sequence[str]] = None, cuantas: int = 10) -> List[OpcionDeEquipo]:
        """Las mejores opciones de equipo con la plantilla (la guardada, o `nombres`)."""
        plantilla = self.equipo(nombres) if nombres else self.plantilla()
        plantilla = [a for a in plantilla if a.especialidad]
        guias = self._guias_de(plantilla)
        pasivas = {n: g.get("pasiva") or {} for n, g in guias.items()}
        sinergias = {n: list(g.get("sinergias") or []) for n, g in guias.items()}
        cat = self.catalogo()
        return opciones_de_equipo(plantilla, pasivas, self.equipos_de_las_guias(), sinergias,
                                  cuantas, usos_de_equipos(guias, cat), plantillas_de_uso(guias, cat))

    def que_falta(self, nombres: Optional[Sequence[str]] = None,
                  cuantos: int = 6) -> List[Tuple[str, float, List[str]]]:
        """Qué personaje falta para los equipos más usados con los que ya se tiene."""
        plantilla = [a for a in (self.equipo(nombres) if nombres else self.plantilla()) if a.especialidad]
        return lo_que_falta(plantilla, usos_de_equipos(self._guias_de(plantilla), self.catalogo()), cuantos)
