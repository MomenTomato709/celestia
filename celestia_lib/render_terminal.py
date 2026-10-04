#!/usr/bin/env python3
"""Convierte el markdown que escribe Celestia en algo legible en el móvil.

El chat de Termux imprimía la respuesta tal cual llegaba, así que una
clasificación de LaLiga aterrizaba como `| 1 | Alavés | 7 | 3 | 2–1–0 |` en
una pantalla de cuarenta columnas, y los `**` de la negrita se veían como
asteriscos sueltos. Aquí se traduce todo eso a texto de terminal: negritas
reales, viñetas, y tablas que se convierten en fichas cuando no caben.

El módulo es puro (texto → texto) para poder probarlo sin terminal.
"""
from __future__ import annotations

import re
import shutil
from typing import List, Optional

# ── Códigos ANSI ─────────────────────────────────────────────────────────────
# Los colores salen de la paleta de la marca cuando el terminal da para tanto,
# y caen a los dieciséis de siempre cuando no. Así el markdown de una respuesta
# se ve del mismo violeta y del mismo cian que el logo, en vez de con los
# colores por defecto de la consola.
from .marca import Tinta as _Tinta

_PALETA = _Tinta()


def _tono(nombre: str, respaldo: str) -> str:
    return _PALETA.codigo(nombre) or respaldo


NEGRITA   = "\033[1m"
TENUE     = "\033[2m"
CURSIVA   = "\033[3m"
SUBRAYADO = "\033[4m"
FIN       = "\033[0m"
CIAN      = _tono("cian",   "\033[96m")
GRIS      = _tono("ceniza", "\033[90m")
VIOLETA   = _tono("iris",   "\033[95m")

ANCHO_MINIMO = 32
ANCHO_POR_DEFECTO = 78
# Debajo de esto, una tabla se lee mejor como fichas que como columnas.
COLUMNAS_MINIMAS_POR_CELDA = 6

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def ancho_terminal(por_defecto: int = ANCHO_POR_DEFECTO) -> int:
    """Ancho utilizable, con un mínimo para que nada quede en columna de uno."""
    try:
        ancho = shutil.get_terminal_size((por_defecto, 24)).columns
    except Exception:                            # pragma: no cover
        ancho = por_defecto
    return max(ANCHO_MINIMO, ancho)


def _visible(texto: str) -> int:
    """Longitud sin contar los códigos de color, que no ocupan pantalla."""
    return len(_ANSI_RE.sub("", texto))


class _Estilo:
    """Aplica color o no, según haya terminal de verdad."""

    def __init__(self, color: bool = True):
        self.color = color

    def __call__(self, texto: str, *codigos: str) -> str:
        if not self.color or not codigos:
            return texto
        return "".join(codigos) + texto + FIN


# ── Estilos dentro de una línea ──────────────────────────────────────────────

def _inline(texto: str, e: _Estilo) -> str:
    """Negritas, cursivas y `código` a códigos de terminal."""
    # El orden importa: primero lo de tres y dos marcas, luego lo de una, o
    # «**texto**» se comería como cursiva el asterisco suelto.
    texto = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: e(m.group(1), NEGRITA, CURSIVA), texto)
    texto = re.sub(r"\*\*(.+?)\*\*", lambda m: e(m.group(1), NEGRITA), texto)
    # La cursiva no puede empezar ni acabar en espacio: sin eso, «3 * 4 * 5»
    # se veía como una cursiva que se comía el 4.
    texto = re.sub(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])",
                   lambda m: e(m.group(1), CURSIVA), texto)
    texto = re.sub(r"(?<![\w_])_(\S(?:[^_\n]*\S)?)_(?![\w_])",
                   lambda m: e(m.group(1), CURSIVA), texto)
    texto = re.sub(r"`([^`\n]+?)`", lambda m: e(m.group(1), CIAN), texto)
    # Enlaces markdown: se queda el texto y el destino en gris detrás.
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                   lambda m: f"{m.group(1)} {e(m.group(2), GRIS)}", texto)
    return _sin_restos_markdown(texto)


def _sin_restos_markdown(texto: str) -> str:
    """Borra las marcas que no llegaron a formar un par.

    Las conversiones de arriba sólo entienden el markdown bien escrito, y el
    modelo deja negritas abiertas o que cruzan un salto de línea. Medido sobre
    veinticinco respuestas reales: 118 «**» y 59 asteriscos sueltos seguían
    llegando a la pantalla, que es justo lo que el usuario no quería ver.
    """
    texto = texto.replace("**", "").replace("__", "")
    # Asterisco o guion bajo pegados a una palabra: es énfasis a medio cerrar.
    # Uno rodeado de espacios se respeta, que ahí es una multiplicación.
    texto = re.sub(r"(?<=\s)([*_])(?=\S)|(?<=\S)([*_])(?=\s|$)", "", texto)
    # Y el que se queda solo al final de la línea («…a medias *»). Uno con
    # espacio a los dos lados y algo detrás sigue siendo una multiplicación.
    texto = re.sub(r"(?<=\s)[*_][ \t]*$", "", texto, flags=re.M)
    # Backtick suelto, sin su pareja.
    texto = re.sub(r"`", "", texto)
    return texto


def _ajustar(texto: str, ancho: int, sangria: str = "", primera: Optional[str] = None) -> List[str]:
    """Parte el texto en líneas que caben, respetando la sangría.

    Se mide lo visible, no la cadena: si contara los códigos de color, las
    líneas con negrita saldrían mucho más cortas que las demás.
    """
    encabezado = primera if primera is not None else sangria
    hueco = max(8, ancho - len(sangria))
    palabras = texto.split()
    if not palabras:
        return []
    lineas: List[str] = []
    actual = ""
    for palabra in palabras:
        if not actual:
            actual = palabra
        elif _visible(actual) + 1 + _visible(palabra) <= hueco:
            actual += " " + palabra
        else:
            lineas.append(actual)
            actual = palabra
    if actual:
        lineas.append(actual)
    salida = [encabezado + lineas[0]]
    salida += [sangria + l for l in lineas[1:]]
    return salida


# ── Tablas ───────────────────────────────────────────────────────────────────

def _es_separador(linea: str) -> bool:
    """La línea de guiones que va bajo la cabecera de una tabla."""
    return bool(re.fullmatch(r"[\s|:-]+", linea)) and "-" in linea and "|" in linea


def _celdas(linea: str) -> List[str]:
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def _render_tabla(filas: List[List[str]], ancho: int, e: _Estilo) -> List[str]:
    """Columnas si caben; fichas si no.

    En un móvil a cuarenta columnas, una tabla de cinco campos no cabe de
    ninguna manera: alinearla produce una papilla. Una ficha por fila se lee.
    """
    if not filas:
        return []
    # Las celdas también llevan markdown dentro («**CPU**»), y era el único
    # sitio del render que no pasaba por aquí: medido sobre respuestas reales,
    # ahí se quedaban 118 «**» que llegaban a la pantalla.
    filas = [[_inline(celda, e) for celda in fila] for fila in filas]
    columnas = max(len(f) for f in filas)
    filas = [f + [""] * (columnas - len(f)) for f in filas]
    anchos = [max(_visible(f[i]) for f in filas) for i in range(columnas)]
    total = sum(anchos) + 3 * (columnas - 1)

    if total <= ancho and min(anchos) >= 1 and ancho // columnas >= COLUMNAS_MINIMAS_POR_CELDA:
        salida = []
        for n, fila in enumerate(filas):
            celdas = [fila[i].ljust(anchos[i]) for i in range(columnas)]
            linea = "  ".join(celdas).rstrip()
            salida.append(e(linea, NEGRITA) if n == 0 else linea)
            if n == 0:
                salida.append(e("─" * min(total, ancho), GRIS))
        return salida

    # Modo ficha: la primera columna titula y el resto son campo: valor.
    cabecera, cuerpo = filas[0], filas[1:]
    salida = []
    for fila in cuerpo:
        # Las dos primeras columnas SIEMPRE van juntas en el título
        # («CPU · Ryzen 7 9800X3D», «1 · Deportivo Alavés»). Antes se fusionaban
        # sólo si la primera era muy corta, y la misma tabla salía con unas
        # filas de una forma y otras de otra.
        titulo = fila[0] or "—"
        if columnas > 1 and fila[1]:
            titulo = f"{titulo} · {fila[1]}" if fila[0] else fila[1]
            campos = list(zip(cabecera[2:], fila[2:]))
        else:
            campos = list(zip(cabecera[1:], fila[1:]))
        # El título también se ajusta: si no, una fila larga se salía de la
        # pantalla justo en la parte que más se mira.
        lineas_titulo = _ajustar(titulo, ancho, sangria="  ")
        salida.append(e(lineas_titulo[0], NEGRITA))
        salida += [e(l, NEGRITA) for l in lineas_titulo[1:]]
        # La etiqueta sólo se repite si es corta y aporta («Puntos: 6»). Con
        # cabeceras como «Modelo recomendado (según guías 2026)» repetidas en
        # cada ficha, el texto se vuelve ilegible: ahí manda el valor.
        detalles = [f"{k}: {v}" if k and len(k) <= 12 else v
                    for k, v in campos if v]
        if detalles:
            salida += _ajustar(" · ".join(detalles), ancho, sangria="    ")
    return salida


# El modelo a veces pega la primera viñeta al párrafo que la presenta:
# «te recomiendo esta configuración: * Tarjeta Gráfica: RTX 5070…». Como las
# listas se detectan a principio de línea, esa primera se quedaba dentro del
# texto con su asterisco a la vista.
_VINETA_PEGADA_RE = re.compile(
    r"(?<=[:.])\s+([*•])\s+(?=[A-ZÁÉÍÓÚÑ¿¡\w])")
# Una segunda viñeta en la misma línea ya delata que aquello era una lista.
_VINETA_EN_SERIE_RE = re.compile(
    r"(?<=[a-záéíóúñ)\.])\s+([*•])\s+(?=[A-ZÁÉÍÓÚÑ][\wáéíóúñ]{2,})")


def _separar_vinetas_pegadas(texto: str) -> str:
    """Baja a su propia línea las viñetas que quedaron dentro del párrafo."""
    salida = []
    for linea in texto.split("\n"):
        # Sin al menos una viñeta en mitad de la línea no hay nada que hacer,
        # y así una multiplicación como «3 * 4» no se toca nunca.
        if _VINETA_PEGADA_RE.search(linea):
            linea = _VINETA_PEGADA_RE.sub(r"\n\1 ", linea)
            linea = _VINETA_EN_SERIE_RE.sub(r"\n\1 ", linea)
        salida.append(linea)
    return "\n".join(salida)


# ── Render principal ─────────────────────────────────────────────────────────

def render(texto: str, ancho: Optional[int] = None, color: bool = True,
           sangria: str = "") -> str:
    """Markdown → texto de terminal legible."""
    if not texto:
        return ""
    e = _Estilo(color)
    ancho = (ancho or ancho_terminal()) - len(sangria)
    ancho = max(ANCHO_MINIMO, ancho)

    lineas = _separar_vinetas_pegadas(texto.replace("\r\n", "\n")).split("\n")
    salida: List[str] = []
    tabla: List[List[str]] = []
    en_codigo = False

    def _vaciar_tabla():
        if tabla:
            salida.extend(_render_tabla(tabla, ancho, e))
            tabla.clear()

    for cruda in lineas:
        linea = cruda.rstrip()

        # Bloques de código: se respetan tal cual, sin tocar nada.
        if linea.strip().startswith("```"):
            _vaciar_tabla()
            en_codigo = not en_codigo
            salida.append(e("─" * min(ancho, 40), GRIS))
            continue
        if en_codigo:
            salida.append(e("  " + linea, CIAN))
            continue

        # Tablas: se acumulan para pintarlas juntas.
        if linea.count("|") >= 2 and not _es_separador(linea):
            tabla.append(_celdas(linea))
            continue
        if _es_separador(linea):
            continue                              # la raya de la cabecera sobra
        _vaciar_tabla()

        if not linea.strip():
            if salida and salida[-1] != "":
                salida.append("")
            continue

        # Encabezados
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", linea)
        if m:
            salida.append(e(_inline(m.group(2).strip(), e).upper(), NEGRITA, VIOLETA))
            continue

        # Separadores horizontales
        if re.fullmatch(r"\s*([-*_])\1{2,}\s*", linea):
            salida.append(e("─" * min(ancho, 40), GRIS))
            continue

        # Citas
        m = re.match(r"^\s*>\s?(.*)$", linea)
        if m:
            salida += _ajustar(_inline(m.group(1), e), ancho, "   ",
                               e("│ ", GRIS) + "")
            continue

        # Listas con viñeta
        m = re.match(r"^(\s*)[-*•]\s+(.*)$", linea)
        if m:
            nivel = len(m.group(1)) // 2
            base = "  " * nivel
            salida += _ajustar(_inline(m.group(2), e), ancho,
                               sangria=base + "   ",
                               primera=base + e(" • ", VIOLETA))
            continue

        # Listas numeradas
        m = re.match(r"^(\s*)(\d{1,2})[.)]\s+(.*)$", linea)
        if m:
            nivel = len(m.group(1)) // 2
            base = "  " * nivel
            marca = e(f" {m.group(2)}. ", VIOLETA)
            salida += _ajustar(_inline(m.group(3), e), ancho,
                               sangria=base + "    ", primera=base + marca)
            continue

        # Párrafo normal
        salida += _ajustar(_inline(linea.strip(), e), ancho)

    _vaciar_tabla()

    while salida and salida[-1] == "":
        salida.pop()
    if sangria:
        return "\n".join(sangria + l if l else "" for l in salida)
    return "\n".join(salida)
