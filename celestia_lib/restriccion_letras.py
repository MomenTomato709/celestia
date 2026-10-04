"""Encargos con letras prohibidas («la única vocal permitida es la a»).

Un modelo de lenguaje no ve letras sino trozos de palabra, así que este tipo de
encargo lo falla casi siempre y además da la frase por buena. Chat del 3 jun
2026: cinco intentos, todos con «e», «o» o «u» dentro. Y el 3 oct, ya en el
portátil: «La casa canta al calamar y calabaza y cacahuata…». Contar letras sí
es algo que el código hace bien, así que se comprueba aquí y no se confía en
el modelo (ver la regla de funcionar con cualquier modelo).
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Set

VOCALES = set("aeiou")

# Una letra sola, quizá entre comillas: «'a'», «a». Detrás no puede venir otra
# letra (con tilde incluida): «el móvil» no prohíbe la «m».
_LETRA = r"['\"«“]?([a-zñ])(?![^\W\d_])['\"»”]?"

# «la única vocal permitida sea la 'a'», «solo con la vocal a», «solo puedes
# usar la vocal a»
_SOLO_VOCAL_RE = re.compile(
    r"(?:[uú]nica\s+vocal(?:\s+(?:permitida|que\s+(?:puedes|se\s+puede)\s+usar))?"
    r"(?:\s+(?:sea|es|ser[aá]))?(?:\s+la)?|"
    r"s[oó]lo\s+(?:con\s+|puedes\s+usar\s+|usando\s+)?la\s+vocal)\s+" + _LETRA,
    re.I)
# «sin la letra e», «sin usar la e», «sin la vocal o». El artículo o «letra»
# son obligatorios: «con o sin y» no es un encargo de letras.
_SIN_LETRA_RE = re.compile(
    r"\bsin\s+(?:usar\s+)?(?:la\s+(?:letra\s+|vocal\s+)?|letra\s+|vocal\s+)" + _LETRA,
    re.I)
# «no puedes usar ni la e, ni la i, ni la o ni la u»
_NO_PUEDES_RE = re.compile(r"no\s+(?:puedes|se\s+puede)\s+usar([^.\n]{0,80})", re.I)
_LETRA_SUELTA_RE = re.compile(r"\b(?:la|el|ni)\s+" + _LETRA + r"(?=\W|$)", re.I)


def _base(c: str) -> str:
    """La letra sin tilde ni diéresis (la ñ se queda: no es una n)."""
    if c in "ñÑ":
        return "ñ"
    return unicodedata.normalize("NFD", c)[0].lower()


def prohibidas(encargo: str) -> Set[str]:
    """Las letras que el encargo no deja usar (vacío si no hay restricción)."""
    t = encargo or ""
    fuera: Set[str] = set()
    m = _SOLO_VOCAL_RE.search(t)
    if m and m.group(1).lower() in VOCALES:
        fuera |= VOCALES - {m.group(1).lower()}
    for m in _SIN_LETRA_RE.finditer(t):
        fuera.add(m.group(1).lower())
    for m in _NO_PUEDES_RE.finditer(t):
        for l in _LETRA_SUELTA_RE.finditer(m.group(1)):
            fuera.add(l.group(1).lower())
    return fuera


def infracciones(respuesta: str, fuera: Set[str]) -> List[str]:
    """Las palabras de la respuesta que llevan alguna letra prohibida.

    Se cuentan LETRAS: la «y» es una consonante y vale aunque suene a «i».
    Contarla como «i» dio por mala una frase buena de DeepSeek y gastó dos
    reintentos de pago (3 oct 2026).
    """
    if not fuera:
        return []
    malas: List[str] = []
    for palabra in re.findall(r"[^\W\d_]+", respuesta or ""):
        letras = {_base(c) for c in palabra}
        if letras & fuera:
            if palabra not in malas:
                malas.append(palabra)
    return malas


def frase_de(respuesta: str) -> str:
    """Lo que hay que comprobar: lo entrecomillado si lo hay (la frase en sí),
    o la respuesta entera."""
    m = re.search(r"[«“\"]([^«»“”\"]{10,})[»”\"]", respuesta or "")
    return m.group(1) if m else (respuesta or "")


def aviso(malas: List[str], fuera: Set[str]) -> str:
    """Lo que se le dice a Enzo cuando ni con reintentos sale bien."""
    letras = ", ".join(sorted(fuera))
    lista = ", ".join(f"«{p}»" for p in malas[:6])
    return (f"Ojo: no lo he conseguido del todo. Se me cuelan letras prohibidas "
            f"({letras}) en {lista}. Contar letras se me da mal; si quieres, "
            f"lo intento con una frase más corta.")


def mejor(intentos: List[str], fuera: Set[str]) -> Optional[str]:
    """El intento con menos palabras fallidas (el primero si empatan)."""
    if not intentos:
        return None
    return min(intentos, key=lambda r: len(infracciones(frase_de(r), fuera)))
