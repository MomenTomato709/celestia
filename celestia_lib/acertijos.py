"""Resolución determinista de acertijos lógicos clásicos.

Sesión 30 (M-B específico): para patrones de acertijo bien conocidos, el LLM
los falla porque "razona" superficialmente sobre el lenguaje sin captar el
truco. Aquí codificamos respuestas canónicas para esos patrones.

Catálogo actual:
- familiar_n_hijos: «mi padre tiene N hijos, [N-1 nombres]. ¿el último?»
  → el narrador. Variante: «el padre de X tiene N hijas, [N-1 nombres]» → X.

Cómo crece este módulo (M-B genérico, futuro): añadir resolutores nuevos
y registrarlos en `RESOLUTORES`. Cada resolutor recibe el texto y devuelve
Optional[str] con la respuesta (None si no reconoce).
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional


# Numerales en español → entero. Cubre 2-12 que es lo que aparece en
# acertijos típicos.
_NUMERALES = {
    "una": 1, "uno": 1, "un": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
}

_ORDINALES = {
    "primero": 1, "primera": 1,
    "segundo": 2, "segunda": 2,
    "tercero": 3, "tercer": 3, "tercera": 3,
    "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5,
    "sexto": 6, "sexta": 6,
    "séptimo": 7, "septimo": 7, "séptima": 7, "septima": 7,
    "octavo": 8, "octava": 8,
    "noveno": 9, "novena": 9,
    "décimo": 10, "decimo": 10,
}


def _parse_n(s: str) -> Optional[int]:
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return _NUMERALES.get(s)


# ─────────────────────────────────────────────────────────────────────────
# Resolutor: «mi padre tiene N hijos» o «el padre de X tiene N hijos»
# ─────────────────────────────────────────────────────────────────────────

# Captura todo el bloque del acertijo. El patrón tiene 3 partes detectables:
#   1) cuenta:   "(mi|el|la) (padre|madre|tío|tía|abuelo|abuela) [de X] tiene N hijos/hijas/nietos"
#   2) listado:  "tres se llaman A, B, C" / "se llaman A, B y C" / "sus nombres son A, B, C"
#   3) pregunta: "¿cómo se llama (el|la) (cuarto|quinta|último|restante|N-ésimo)?"
_REL_FAMILIAR = r"(?:padre|madre|t[ií]o|t[ií]a|abuelo|abuela|pap[áa]|mam[áa])"
_DESCENDIENTES = r"(?:hijos|hijas|nietos|nietas|sobrinos|sobrinas)"

_CUENTA_RE = re.compile(
    r"(?P<poss>mi|el|la)\s+"
    r"(?P<fam>" + _REL_FAMILIAR + r")"
    r"(?:\s+de\s+(?P<de>[A-Za-zÁÉÍÓÚÑáéíóúñ]+))?"
    # tiene / tuvo / tenía — aceptar pretérito/imperfecto también
    r"\s+(?:tiene|tuvo|ten[ií]a)\s+"
    r"(?P<n>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<desc>" + _DESCENDIENTES + r")",
    re.I,
)

# Listado de N-1 nombres. Aceptamos formatos:
#   "se llaman A, B y C"
#   "se llaman A, B, C"
#   "sus nombres son A, B, C"
#   "tres de ellos son A, B y C"
#   "Sus nombres: A, B, C"
#   ", A, B y C." (sin verbo, justo después de la cuenta)
_LISTADO_RE = re.compile(
    # Introductores aceptados:
    #   "se llaman" / "se llama" (sing — útil para N=2)
    #   "sus nombres son" / "sus nombres:"
    #   "una/uno/dos/.../ocho se llama(n)"
    #   "tres son" / "cuatro son" (sin "llamados" — el filtro de nombres
    #     propios capitalizados después descarta "tres son rojas" porque
    #     "rojas" no se considera nombre propio).
    #   "son llamados/llamadas"
    r"(?:"
    r"(?:una|uno|dos|tres|cuatro|cinco|seis|siete|ocho)\s+(?:de\s+ellos\s+)?"
    r"(?:se\s+llaman?|son)|"
    r"se\s+llaman?|sus\s+nombres\s+(?:son|:)|"
    r"son\s+(?:llamados|llamadas)"
    r")\s*"
    r"(?P<nombres>[^.?!¿¡]+?)"
    r"(?=\s*[.?!¿¡]|\s*¿|\s*$)",
    re.I,
)
# Fallback sin verbo introductor: lista explícita de nombres separados por
# comas y "y" justo después de la cuenta. Acepta 1 nombre solo (caso N=2).
_LISTADO_FALLBACK_RE = re.compile(
    r"[,:]\s*(?P<nombres>(?:[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s*,\s*|\s+y\s+))*"
    r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)",
)

_PREGUNTA_RE = re.compile(
    r"¿?\s*(?:"
    # Forma larga: "cómo se llama / cuál es / quién es / qué nombre tiene"
    r"(?:c[oó]mo\s+se\s+llama|"
    r"cu[aá]l\s+(?:es\s+(?:el\s+nombre\s+)?(?:del?|de\s+la)|el)|"
    r"qui[eé]n\s+es|"
    r"qu[eé]\s+nombre\s+tiene)"
    # Artículo opcional: "del/de la" ya pueden haber consumido el artículo
    # (ej. "cuál es el nombre del cuarto"); o puede venir explícito (ej.
    # "cómo se llama el cuarto").
    r"\s+(?:el|la|los|las)?\s*"
    r"|"
    # Forma corta: "¿el cuarto?" / "¿la quinta?" / "¿el último?" — sólo
    # artículo + orden, sin verbo. Aceptado porque el contexto ya está
    # acotado al acertijo (cuenta+listado precedentes).
    r"(?:el|la)\s+"
    r")"
    r"(?P<orden>" + "|".join(_ORDINALES.keys()) + r"|[uú]ltim[oa]|restante|"
    r"que\s+falta|otro|otra)\s*\??",
    re.I,
)


def _extraer_nombres(texto_listado: str) -> List[str]:
    """Devuelve los nombres propios separados por ',' o ' y ', en orden."""
    # Reemplazar ' y ' por ',' y dividir.
    s = re.sub(r"\s+y\s+", ",", texto_listado, flags=re.I)
    partes = [p.strip(" .;:") for p in s.split(",")]
    nombres = []
    for p in partes:
        if not p:
            continue
        # Quedarnos sólo con tokens tipo nombre propio (capitalizado o todo
        # minúscula es OK; descartar palabras-conector residuales).
        if re.fullmatch(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+(?:\s+[A-Za-zÁÉÍÓÚÑáéíóúñ]+)?", p):
            nombres.append(p)
    return nombres


def resolver_familiar_n_hijos(texto: str) -> Optional[str]:
    """Si el texto es el acertijo familiar/N-hijos, devuelve la respuesta;
    en caso contrario, None."""
    m_cuenta = _CUENTA_RE.search(texto)
    if not m_cuenta:
        return None
    n = _parse_n(m_cuenta.group("n"))
    if not n or n < 2:
        return None
    poss = m_cuenta.group("poss").lower()  # mi / el / la
    de = m_cuenta.group("de")              # nombre tras "de" o None
    desc = m_cuenta.group("desc").lower()  # hijos / hijas / ...

    # Listado de nombres: buscar después de la cuenta. IMPORTANTE: acotar la
    # ventana al primer signo de pregunta para que "se llama la tercera" en
    # la pregunta no se confunda con un listado.
    cola = texto[m_cuenta.end():]
    corte = re.search(r"[?¿]", cola)
    cola_listado = cola[:corte.start()] if corte else cola
    m_list = (_LISTADO_RE.search(cola_listado)
              or _LISTADO_FALLBACK_RE.search(cola_listado))
    if not m_list:
        return None
    nombres = _extraer_nombres(m_list.group("nombres"))
    if len(nombres) != n - 1:
        # No es el patrón clásico: o sobran o faltan nombres.
        # Si listan N nombres, no hay misterio; si listan < N-1, ambigüo.
        return None

    # Pregunta: buscar después del listado.
    cola2 = cola[m_list.end():]
    m_preg = _PREGUNTA_RE.search(cola2)
    if not m_preg:
        return None
    orden_raw = m_preg.group("orden").lower()
    # Si la pregunta indica orden específico (ej: "cuarto"), verificar que
    # coincida con N. Si pide "último/restante/que falta", N implícito.
    if orden_raw in _ORDINALES:
        pos = _ORDINALES[orden_raw]
        if pos != n:
            # Pregunta por un orden que no es el faltante → no aplica nuestra
            # respuesta canónica.
            return None
    # else: "último", "restante", "que falta", "otro" → asumimos N-ésimo.

    # Sesión 31 (BUG-I): concordancia de género. Si los descendientes son
    # "hijas"/"nietas"/"sobrinas", usar femenino ("la que falta", "una de las").
    femenino = desc.endswith("as")
    art_def = "la" if femenino else "el"
    art_indef_pl = "las" if femenino else "los"
    pronombre = "ella" if femenino else "él/ella"

    # Caso A: "mi padre/madre/... tiene N hijos" → narrador es uno de los hijos.
    if poss == "mi" and not de:
        razonamiento = (
            f"(piensa: el narrador dice 'mi {m_cuenta.group('fam').lower()}', "
            f"así que {pronombre} es {'una' if femenino else 'uno'} de "
            f"{art_indef_pl} {desc} mencionados. Tras listar "
            f"{n-1} nombres, {art_def} {desc.rstrip('s')} que falta es "
            f"{'la propia' if femenino else 'el propio'} narrador{'a' if femenino else ''}.)"
        )
        return (f"{razonamiento} {art_def.capitalize()} que falta eres "
                f"tú {'misma' if femenino else 'mismo'} "
                f"(el narrador del acertijo).")

    # Caso B: "el/la padre/madre de X tiene N hijos, [N-1 nombres listados sin X]"
    # → el que falta es X (porque "padre de X" implica que X es descendiente).
    if poss in ("el", "la") and de:
        # Verificar que X NO esté en los nombres listados.
        nombres_lower = {n_.lower() for n_ in nombres}
        if de.lower() not in nombres_lower:
            razonamiento = (
                f"(piensa: la pregunta dice '{poss} {m_cuenta.group('fam').lower()} "
                f"de {de}', así que {de} es {'una' if femenino else 'uno'} de "
                f"{art_indef_pl} {desc}. Listan {n-1} nombres sin {de} → "
                f"{art_def} {desc.rstrip('s')} que falta es {de}.)"
            )
            return (f"{razonamiento} {art_def.capitalize()} que falta se "
                    f"llama {de}.")

    return None


# ─────────────────────────────────────────────────────────────────────────
# Resolutor: «N camisas tardan T horas en secarse al sol» — paralelismo
# ─────────────────────────────────────────────────────────────────────────

# Sólo aplicar para secado al sol/aire libre/tendedero — son procesos
# físicamente paralelos. NO aplicar a "lavar a mano" o "en la lavadora".
#
# Sesión 31 (BUG-BB): el regex monolítico previo exigía un orden fijo
# (objeto → tardan T en secarse → al sol), perdiéndose variantes naturales
# como «si tiendo 1 camisa al sol y tarda 2 horas en secarse, ¿cuánto tardan
# 5 camisas en secarse al sol?». Refactor: 3 sondas independientes —
# (a) presencia de modalidad solar/aire, (b) declaración de N objeto + T
# unidad + secar, (c) pregunta con M objeto.
_AL_SOL_RE = re.compile(
    r"\b(?:al\s+sol|al\s+aire(?:\s+libre)?|"
    r"(?:en|al)\s+(?:el\s+)?tendedero|"
    r"al\s+viento|colgad[ao]s?(?:\s+al\s+sol)?)\b",
    re.I,
)

_PRENDAS_RE = r"camisas?|prendas?|s[aá]banas?|toallas?|playeras?|camisetas?|calcetines"

_CAMISAS_DECL_RE = re.compile(
    # «1 camisa … 2 horas … secar» en cualquier orden razonable.
    # Capturamos el objeto declarado + tiempo + unidad, exigiendo verbo
    # de secado en la cláusula. El "si" inicial es opcional.
    r"(?:si\s+(?:tiendo|tengo|pongo|cuelgo)\s+)?"
    r"(?P<n>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<obj>" + _PRENDAS_RE + r")\b"
    r"[^.?!]{0,80}?"
    r"(?P<t>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<u>horas?|minutos?|d[ií]as?)"
    r"[^.?!]{0,40}?"
    r"\bsecar(?:se|las|los)?\b",
    re.I,
)

# Fallback declarativo con orden inverso: «tarda T horas en secar … N camisas»
_CAMISAS_DECL_INV_RE = re.compile(
    r"tard[ao]n?\s+"
    r"(?P<t>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<u>horas?|minutos?|d[ií]as?)"
    r"\s+en\s+secar(?:se|las|los)?"
    r"[^.?!]{0,60}?"
    r"(?P<n>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<obj>" + _PRENDAS_RE + r")",
    re.I,
)

# Sesión 31 (AX-12): tercer patrón declarativo: «N camisas se secan en T horas».
# Difiere de _CAMISAS_DECL_RE en que el verbo de secado va ANTES del tiempo
# («se secan en …» vs «… 2 horas en secarse»).
_CAMISAS_DECL_SE_RE = re.compile(
    r"(?:si\s+)?"
    r"(?P<n>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<obj>" + _PRENDAS_RE + r")\b"
    r"[^.?!]{0,40}?"
    r"\bse\s+secan\s+(?:en\s+)?"
    r"(?P<t>\d+|" + "|".join(_NUMERALES.keys()) + r")\s+"
    r"(?P<u>horas?|minutos?|d[ií]as?)",
    re.I,
)

_CAMISAS_PREG_RE = re.compile(
    # Acepta «¿cuánto (tiempo) tardan …», «¿cuántos minutos tardan …»
    # y «¿en cuánto tiempo se secan …». El objeto al final es opcional
    # (sesión 31, AX-12): «¿cuánto tardan 20?» se entiende implícitamente
    # como el mismo objeto declarado antes.
    r"¿?\s*(?:en\s+)?cu[aá]nto(?:s)?"
    r"(?:\s+(?:tiempo|horas?|minutos?|d[ií]as?))?\s+"
    r"(?:tarda(?:n|r[ií]an?|r[aá]n)?|necesita(?:n|r[ií]an?|r[aá]n)?|"
    r"se\s+secan)\s+"
    r"(?P<m>\d+|" + "|".join(_NUMERALES.keys()) + r")"
    r"(?:\s+(?:m[aá]s\s+)?(?P<obj>" + _PRENDAS_RE + r"))?",
    re.I,
)


def resolver_camisas_paralelo(texto: str) -> Optional[str]:
    # 1) Modalidad solar/aire debe aparecer al menos una vez.
    if not _AL_SOL_RE.search(texto):
        return None
    # 2) Declaración: N objeto + T unidad + secar (en cualquier orden razonable).
    decl = (_CAMISAS_DECL_RE.search(texto)
            or _CAMISAS_DECL_INV_RE.search(texto)
            or _CAMISAS_DECL_SE_RE.search(texto))
    if not decl:
        return None
    t = _parse_n(decl.group("t"))
    if not t:
        return None
    unidad = decl.group("u").lower()
    obj_decl = decl.group("obj").lower()
    # 3) Pregunta: ¿cuánto tardan M (objeto)?
    preg = _CAMISAS_PREG_RE.search(texto)
    if not preg:
        return None
    obj_preg = preg.group("obj")
    # Si la pregunta omite el objeto («¿cuánto tardan 20?»), asumir que es
    # el mismo que el declarado (sesión 31, AX-12). Si lo lleva, debe
    # compartir el mismo lema (camisa/camisas, etc.); con objetos distintos
    # (camisas vs sábanas) abortar — puede ser un acertijo mezclado.
    if obj_preg is not None:
        if obj_decl[:5] != obj_preg.lower()[:5]:
            return None
    razonamiento = (
        f"(piensa: las {obj_decl} se secan en paralelo al sol, no en serie. "
        f"El tiempo NO depende del número de prendas, sólo de las condiciones "
        f"físicas. Si {decl.group('n')} tardan {t} {unidad}, {preg.group('m')} "
        f"tardan lo mismo.)"
    )
    return f"{razonamiento} Tardan {t} {unidad}."


# ─────────────────────────────────────────────────────────────────────────
# Resolutor: «dos padres y dos hijos pescan tres peces»
# ─────────────────────────────────────────────────────────────────────────

# Tres personas: abuelo, padre e hijo. El padre del medio es a la vez "padre"
# (del hijo) e "hijo" (del abuelo). Así son 2 padres + 2 hijos = 3 personas.
_DOS_PADRES_RE = re.compile(
    r"(?:dos|2)\s+padres?\s+y\s+(?:dos|2)\s+hijos?\s+"
    r"(?:van\s+a\s+(?:pescar|cazar|comer)|salen\s+(?:a|de)|"
    r"se\s+sientan|comen|llegan|entran|cazan|pescan)"
    r"[^.?!]{0,140}?"
    r"(?:cada\s+uno|cogen?|toman?|piden?|pescan?|cazan?|comen?)?"
    r"[^.?!]{0,80}?"
    r"(?:tres|3)\s+(?:peces|piezas|platos|sillas|sombreros|presas)"
    r"[^?]*\?",
    re.I | re.S,
)


def resolver_dos_padres_dos_hijos(texto: str) -> Optional[str]:
    m = _DOS_PADRES_RE.search(texto)
    if not m:
        return None
    razonamiento = (
        "(piensa: dos padres + dos hijos no son 4 personas si una persona "
        "cumple ambos roles. Abuelo–padre–hijo son 3 personas: el del medio "
        "es padre del joven Y hijo del mayor. Así hay 2 padres y 2 hijos, "
        "pero sólo 3 individuos.)"
    )
    return (f"{razonamiento} Son 3 personas (abuelo, padre, hijo): el padre "
            f"del medio cuenta como padre Y como hijo a la vez.")


# ─────────────────────────────────────────────────────────────────────────
# Registro de resolutores. Para añadir más patrones (M-B genérico), basta
# con escribir una función nueva y añadirla aquí. Devuelven Optional[str].
#
# PRÓXIMO PASO (M-B genérico real, no implementado aún):
# - resolver_algebraico(texto): parsear ecuaciones del lenguaje natural
#   ("tengo el doble de tu edad", "entre los dos sumamos N"), montar
#   sistema de ecuaciones lineales y resolver con sympy/numpy.
# - resolver_csp(texto): para acertijos de asignación (Einstein/zebra,
#   Sudoku verbal, etc.) usar python-constraint.
# Mientras tanto, añadir resolutores específicos cuando aparezcan acertijos
# que el LLM falla — cada uno toma ~30 min y cubre una familia de variantes.
# ─────────────────────────────────────────────────────────────────────────
_PLUMAS_HIERRO_RE = re.compile(
    r"qu[eé]\s+pesa\s+m[aá]s.*?"
    r"(?:un\s+)?kilo\s+(?:de\s+)?(plumas?|algod[oó]n|paja|esponja)"
    r".*?(?:un\s+)?kilo\s+(?:de\s+)?(hierro|plomo|acero|oro|piedra)",
    re.I | re.S,
)
_PLUMAS_HIERRO_INV_RE = re.compile(
    r"qu[eé]\s+pesa\s+m[aá]s.*?"
    r"(?:un\s+)?kilo\s+(?:de\s+)?(hierro|plomo|acero|oro|piedra)"
    r".*?(?:un\s+)?kilo\s+(?:de\s+)?(plumas?|algod[oó]n|paja|esponja)",
    re.I | re.S,
)


def resolver_kilo_plumas(texto: str) -> Optional[str]:
    """Sesión 32 (BUG-S149): acertijo clásico. Un kilo es un kilo,
    independientemente del material. El LLM Groq fallaba diciendo «el hierro
    es más denso así que pesa más»."""
    m1 = _PLUMAS_HIERRO_RE.search(texto)
    m2 = _PLUMAS_HIERRO_INV_RE.search(texto)
    if m1:
        ligero, denso = m1.group(1), m1.group(2)
    elif m2:
        denso, ligero = m2.group(1), m2.group(2)
    else:
        return None
    # Sesión 33 (B33-15): concordancia. «algodón» es masculino singular,
    # «plumas» femenino plural, «paja» femenino singular, «esponja» femenino
    # singular. El template anterior decía «las algodon» (mal).
    art_ligero = {
        "plumas": ("las", "más plumas"),
        "pluma": ("la", "más plumas"),
        "algodon": ("el", "más algodón"),
        "algodón": ("el", "más algodón"),
        "paja": ("la", "más paja"),
        "esponja": ("la", "más esponja"),
    }.get(ligero.lower(), ("el", f"más {ligero}"))
    return (f"Pesan exactamente lo mismo: un kilo es un kilo. "
            f"Aunque el {denso} es mucho más denso que {art_ligero[0]} {ligero}, "
            f"la masa es la misma — un kilogramo. Lo que cambia es el VOLUMEN: "
            f"necesitas muchísimo {art_ligero[1]} para alcanzar ese kilo.")


RESOLUTORES: List[Callable[[str], Optional[str]]] = [
    resolver_familiar_n_hijos,
    resolver_camisas_paralelo,
    resolver_dos_padres_dos_hijos,
    resolver_kilo_plumas,
]


def resolver(texto: str) -> Optional[str]:
    """Intenta resolver el texto con cada resolutor; devuelve la primera
    respuesta no-None, o None si ningún resolutor reconoce el patrón."""
    for fn in RESOLUTORES:
        try:
            r = fn(texto)
        except Exception:
            r = None
        if r:
            return r
    return None
