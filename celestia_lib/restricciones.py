"""Lo que no se puede comer: alergias, intolerancias y dietas que te han contado.

Examen de nivel (5 oct 2026): Marta dijo en el tercer mensaje que es alérgica a
TODOS los frutos secos, y en el 26 Celestia le recomendó turrón de Jijona «con
almendra marcona». El dato estaba guardado, pero al modelo sólo le llegaban los
tres hechos del usuario que compartían palabras con la pregunta, y «turrón» no
comparte ninguna con «alergia: frutos secos».

Aquí, sin modelo:
  `de_hechos(hechos)` saca las restricciones de lo guardado,
  `habla_de_comer(texto)` dice si la pregunta va de comida,
  `nota(restricciones)` es el aviso que se le pone al modelo en ese turno, y
  `incumple(respuesta, restricciones)` encuentra lo que se ha colado (almendra,
  praliné, mazapán…) cuando no va negado («sin almendras», «evita la nuez») ni es
  el propio aviso («ojo, que muchas llevan frutos secos escondidos»).

Revisión independiente (6 oct 2026): la primera versión marcaba como fallo justo
las respuestas buenas que nombran la alergia, dejaba pasar «Sin duda, un turrón
de Jijona con almendra» y confundía «almendros en flor», «natación» o
«salmonela» con alérgenos. Ahora cada alérgeno es una palabra entera.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

# Lo que lleva cada alérgeno aunque no lo nombre: expresiones regulares sobre el
# texto en minúsculas y sin tildes, cada una una palabra entera (con su plural).
_LLEVAN: Dict[str, Tuple[str, ...]] = {
    "frutos secos": (
        r"almendras?", r"almendrad[oa]s?", r"avellanas?", r"nuez", r"nueces", r"pistachos?",
        r"anacardos?", r"cacahuetes?", r"mani(?:es)?", r"pinon(?:es)?", r"macadamias?",
        r"pecanas?", r"pralines?", r"mazapan(?:es)?", r"marcona", r"jijona", r"xixona",
        r"turron(?:es)?\s+(?:de\s+)?(?:alicante|jijona|xixona|almendras?|duros?|blandos?)",
        r"alicante\s+duro", r"guirlache", r"peladillas?", r"panellets?", r"nutella",
        r"nocilla", r"frangipanes?", r"pesto", r"crema\s+de\s+cacahuete",
        r"leche\s+de\s+almendras?", r"tarta\s+de\s+santiago", r"pan\s+de\s+cadiz",
        r"garrapinad[oa]s?", r"frutos\s+secos"),
    "cacahuete": (r"cacahuetes?", r"mani(?:es)?", r"crema\s+de\s+cacahuete"),
    "gluten": (r"trigo", r"cebada", r"centeno", r"pan(?:es)?", r"pasta(?:s)?(?!\s+de\s+dientes)",
               r"cuscus", r"seitan", r"rebozad[oa]s?", r"empanad[oa]s?", r"bizcochos?",
               r"galletas?"),
    "lactosa": (r"leche(?!\s+(?:de\s+(?:avena|soja|arroz|almendras?|coco)|sin\s+lactosa))",
                r"nata", r"quesos?", r"mantequilla", r"yogur(?:es|t|ts)?", r"bechamel",
                r"helados?(?=\s+de\b)"),
    "marisco": (r"gambas?", r"langostinos?", r"mejillon(?:es)?", r"almejas?", r"calamar(?:es)?",
                r"pulpo", r"cangrejos?", r"bogavantes?", r"mariscos?", r"chipiron(?:es)?"),
    "huevo": (r"huevos?", r"mayonesa", r"tortillas?", r"merengues?", r"flan(?:es)?"),
    "pescado": (r"merluza", r"salmon", r"atun", r"bacalao", r"sardinas?", r"boquerones?",
                r"pescados?"),
}
_SINONIMOS = {"nueces": "frutos secos", "fruto seco": "frutos secos",
              "cacahuetes": "cacahuete", "mani": "cacahuete", "celiaca": "gluten",
              "celiaco": "gluten", "celiaquia": "gluten", "lacteos": "lactosa",
              "leche": "lactosa", "mariscos": "marisco", "crustaceos": "marisco",
              "huevos": "huevo"}
_DIETAS = {"vegetarian": ("carne", "pollo", "cerdo", "ternera", "jamon", "chorizo", "bacon",
                          "beicon", "pescado", "salmon", "atun", "merluza", "gamba",
                          "langostino", "anchoa", "cordero", "pavo", "solomillo", "chuleta"),
           "vegan": ("carne", "pollo", "cerdo", "ternera", "jamon", "pescado", "huevo", "leche",
                     "queso", "nata", "mantequilla", "miel", "yogur", "atun", "gamba")}
_RELACIONES = {"pareja": ["novio", "novia", "pareja", "marido", "mujer", "esposo", "esposa"],
               "hermano": ["hermano"], "hermana": ["hermana"], "madre": ["madre", "mama"],
               "padre": ["padre", "papa"], "hijo": ["hijo"], "hija": ["hija"]}

# Sin «como» (también es «tal como», «trabajo como»), ni «menú de ajustes», «plato de
# ducha», «lavar los platos», «receta médica» o «dulces sueños».
_COMER_RE = re.compile(
    r"\b(?:com(?:er|emos|eis)|comidas?|cen(?:ar|as)|desayun\w*|merend\w*|almorz\w*|almuerzo|"
    r"picar|picoteo|snack|aperitivo|tapas?|recetas?\s+(?:de|para)\b|"
    r"men[uú]s?\b(?!\s+de\s+(?:ajustes|configuraci|opciones|inicio))|"
    r"(?<!lavar\slos\s)(?<!fregar\slos\s)platos?\b(?!\s+de\s+ducha)|postres?|"
    r"dulces?\b(?!\s+sue[ñn]os)|tartas?|bizcocho|galletas?|turr[oó]n\w*|chocolate|"
    r"bomb[oó]n\w*|helados?|cocin(?:ar|o|as|amos|ado)\b|restaurante\w*|bocadillo|ensalada|"
    r"lista\s+de\s+la\s+compra|regalo\s+(?:de\s+)?comida|cesta\s+de\s+navidad|ros[cq]u[oó]n|"
    r"polvorones?|mazap[aá]n|qu[eé]\s+(?:me\s+)?(?:hago|preparo)\s+de\s+(?:comer|cenar))\b",
    re.I)
# Negación justo delante, sin cruzar una coma, un «con», «pero», «y»… («Sin duda, un
# turrón con almendra» sí recomienda almendra).
_NEGACION_RE = re.compile(
    r"(?:\bsin\b(?!\s+duda)|\bevit\w*|\bnada\s+(?:de|que)\b|\bno\s+(?:lleva|llevan|lleve|lleven|"
    r"tiene|tienen|tenga|contiene|contienen|contenga|uses|use|pongas|ponga|comas|tomes)\b|"
    r"\blibres?\s+de\b|\bcuidado\s+con\b|\bojo\s+con\b|\bal[eé]rgic\w*|\balergi\w*|"
    r"\bintoleran\w*|\btrazas?\b|\bexcepto\b|\bsustitu\w*|\ben\s+vez\s+de\b|\bcambia\w*|"
    r"\bcero\b|\bdescart\w*|\bquita\w*)"
    r"(?:(?![,;:.()\n]|\b(?:con|pero|y|o|aunque|mas|tambien)\b).){0,30}$", re.I)
# Una frase que avisa del alérgeno («ojo, que muchas llevan frutos secos escondidos»,
# «evita el pesto, que lleva piñones»): nombrarlo ahí no es recomendarlo. Sin «sin»:
# «Sin duda, un turrón con almendra» sí lo recomienda.
_FRASE_DE_AVISO_RE = re.compile(
    r"\b(?:alergi\w*|al[eé]rgic\w*|intoleran\w*|ojo|cuidado|escondid\w*|trazas?|"
    r"etiquetas?|revisa\w*|evit\w*|descart\w*)\b", re.I)
_NEGATIVO_RE = re.compile(r"^\s*(?:ningun[oa]?|nada|no\s+tengo|no\s+soy|ya\s+no)\b|"
                          r"\b(?:ya\s+no|no\s+soy|dej[eé]\s+de\s+ser)\b", re.I)


@dataclass(frozen=True)
class Restriccion:
    quien: str          # «tú» o el nombre («Andrés»)
    que: str            # «frutos secos», «vegetariano»
    llevan: Tuple[str, ...]
    dieta: bool = False
    relacion: str = ""  # «pareja», «hermano»: cómo se le nombra sin decir su nombre

    @property
    def mia(self) -> bool:
        return self.quien == "tú"

    @property
    def texto(self) -> str:
        quien = "el usuario" if self.mia else self.quien
        if self.dieta:
            return f"{quien} es {self.que}"
        return f"{quien} tiene alergia o intolerancia a {self.que}"

    def viene_al_caso(self, pregunta: str) -> bool:
        """Las de otra persona sólo cuentan si la pregunta la incluye."""
        if self.mia:
            return True
        llano = _llano(pregunta)
        nombres = [_llano(self.quien)] + _RELACIONES.get(self.relacion, [])
        return any(re.search(rf"\b{re.escape(n)}\b", llano) for n in nombres if n) or bool(
            re.search(r"\b(?:para\s+(?:los\s+)?dos|para\s+\w+\s+y\s+para\s+mi|"
                      r"nosotros|los\s+dos|en\s+pareja|invitad\w*|viene\s+a\s+comer)\b", llano))


def _llano(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (texto or "").lower())
                   if unicodedata.category(c) != "Mn")


def _alergenos(valor: str) -> List[str]:
    v = _llano(valor)
    hallados = []
    for nombre, canon in list(_SINONIMOS.items()) + [(k, k) for k in _LLEVAN]:
        if re.search(rf"\b{re.escape(nombre)}\b", v) and canon not in hallados:
            hallados.append(canon)
    if "frutos secos" in hallados and "cacahuete" in hallados:
        hallados.remove("cacahuete")          # ya va dentro
    return hallados


def de_hechos(hechos: Iterable[dict]) -> List[Restriccion]:
    """Las restricciones que se deducen de los hechos guardados del usuario.

    Sólo lo que es suyo o de alguien con nombre: «alergia_hija: Lucía es alérgica al
    huevo» es de Lucía, «comida_favorita: lasaña vegetariana» no es una dieta, y
    «alergia: ninguna» o «ya no soy vegetariana» no restringen nada.
    """
    out: List[Restriccion] = []
    for h in hechos or []:
        clave = _llano(str(h.get("clave") or "")).strip()
        valor = str(h.get("valor") or "")
        llano = _llano(valor)
        if _NEGATIVO_RE.search(llano):
            continue
        nombre = re.match(r"\s*((?-i:[A-ZÁÉÍÓÚÑ])[a-záéíóúñ]+)\b", valor)
        if any(k in clave for k in ("alergi", "intoleran", "celiac")):
            # Con un nombre propio delante, la alergia es de esa persona.
            quien = nombre.group(1) if nombre else "tú"
            for a in _alergenos(valor):
                out.append(Restriccion(quien, a, _LLEVAN[a]))
        # «pareja: Andrés (es vegetariano)», «dieta: vegana».
        if clave in ("dieta", "alimentacion") or clave in _RELACIONES:
            for dieta, prohibido in _DIETAS.items():
                if re.search(rf"\b{dieta}[oa]s?\b", llano):
                    quien, relacion = "tú", ""
                    if clave in _RELACIONES:
                        quien, relacion = (nombre.group(1) if nombre else clave), clave
                    que = "vegetariano" if dieta == "vegetarian" else "vegano"
                    out.append(Restriccion(quien, que, prohibido, dieta=True, relacion=relacion))
    vistos, unicas = set(), []
    for r in out:
        if (r.quien, r.que) not in vistos:
            vistos.add((r.quien, r.que))
            unicas.append(r)
    return unicas


def habla_de_comer(texto: str) -> bool:
    return bool(_COMER_RE.search(texto or ""))


# Lo que se cuenta en el mismo mensaje en que se pide: «yo soy alérgico al cacahuete,
# hazme las cenas». Los hechos se guardan después de contestar, así que la nota no
# salía (examen de nivel, menú de la semana, 7 oct 2026).
_PREP = r"(?:a\s+(?:la\s+|los\s+|las\s+)?|al\s+)?"
_EN_EL_MENSAJE_RE = re.compile(
    r"\b(?:soy|es|son|somos)\s+(?:al[eé]rgic[oa]s?|intolerantes?)\s+" + _PREP
    + r"([^.;:()\n]{2,80}?)(?=\s*(?:[.;:()\n]|,?\s*\b(?:pero|así|que|para)\b|$))"
    r"|\btengo\s+(?:alergia|intolerancia)\s+" + _PREP
    + r"([^.;:()\n]{2,80}?)(?=\s*(?:[.;:()\n]|,?\s*\b(?:pero|así|que|para)\b|$))"
    r"|\b(?:soy|es|son|somos)\s+(vegetarian[oa]s?|vegan[oa]s?|cel[ií]ac[oa]s?)\b",
    re.I)


def del_mensaje(texto: str) -> List[Restriccion]:
    """Las alergias y dietas que se dicen en el propio mensaje."""
    hechos = []
    for m in _EN_EL_MENSAJE_RE.finditer(texto or ""):
        if m.group(1) or m.group(2):
            hechos.append({"clave": "alergia", "valor": (m.group(1) or m.group(2)).strip()})
        elif m.group(3) and _llano(m.group(3)).startswith("celiac"):
            hechos.append({"clave": "alergia", "valor": "gluten"})
        elif m.group(3):
            hechos.append({"clave": "dieta", "valor": m.group(3)})
    return de_hechos(hechos)


def nota(restricciones: Sequence[Restriccion], pregunta: str = "") -> str:
    restricciones = [r for r in restricciones if r.viene_al_caso(pregunta)]
    if not restricciones:
        return ""
    lineas = "; ".join(r.texto for r in restricciones)
    # Juez del examen (menú, 7 oct): «no advierte del riesgo de trazas en productos
    # comprados como salsas o tortillas». A un alérgico, eso hay que decírselo.
    trazas = (" Si propones algo comprado (salsas, tortillas, masas, especias, chocolate, "
              "galletas, pan…), recuérdale mirar en la etiqueta las trazas («puede contener…»)."
              if any(not r.dieta for r in restricciones) else "")
    return ("[ESTADO INTERNO — RESTRICCIONES DE COMIDA, OBLIGATORIAS: "
            f"{lineas}. Nada de lo que recomiendes puede llevarlo, ni de ingrediente "
            "escondido (turrón de almendra, praliné, mazapán, pesto, bechamel…), aunque "
            "lo traiga lo de internet. Si lo que pide suele llevarlo, propón una versión "
            f"que no lo lleve y di que es por eso.{trazas}]")


def _frase(texto: str, pos: int) -> str:
    ini = max(texto.rfind(".", 0, pos), texto.rfind("\n", 0, pos), texto.rfind("!", 0, pos),
              texto.rfind("?", 0, pos)) + 1
    fines = [i for i in (texto.find(".", pos), texto.find("\n", pos)) if i >= 0]
    return texto[ini:min(fines) if fines else len(texto)]


def incumple(respuesta: str, restricciones: Sequence[Restriccion]) -> List[str]:
    """Lo que la respuesta recomienda y no debería («almendra», «praliné»), salvo
    cuando va negado justo delante («sin almendras», «cero frutos secos») o en una
    frase que avisa de ello («evita el pesto, que lleva piñones»). Sólo las
    alergias del propio usuario: son las que hacen daño; una dieta ajena es cosa del
    aviso."""
    texto = _llano(respuesta) + " "
    # Para nombrarlo como venía («praliné», no «praline»): quitar tildes no cambia
    # la longitud en español, así que las posiciones valen para el original.
    original = (respuesta + " ") if len(texto) == len(respuesta) + 1 else texto
    malas: List[str] = []
    for r in restricciones:
        if not r.mia or r.dieta:
            continue
        for patron in r.llevan:
            for m in re.finditer(rf"\b(?:{patron})\b", texto):
                if _NEGACION_RE.search(texto[max(0, m.start() - 60):m.start()]):
                    continue
                if _FRASE_DE_AVISO_RE.search(_frase(texto, m.start())):
                    continue
                malas.append(original[m.start():m.end()].strip().lower())
                break
    return list(dict.fromkeys(malas))
