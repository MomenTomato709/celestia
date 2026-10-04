#!/usr/bin/env python3
"""Deja el texto que escribe Celestia con un formato constante y legible.

El problema, dicho por el usuario: «Celestia no usa bien el markdown, no lo
ordena como debe y me pierdo». Mirando lo que había guardado en la BD se veía
por qué:

  - **la negrita había perdido el significado**: iban en negrita el nombre de
    cada componente, cada precio y cada total, así que no destacaba nada;
  - **títulos disfrazados**: el system prompt le prohíbe los `##`, así que el
    modelo hacía encabezados poniendo una línea entera en negrita
    (`**Configuración base (≈ 1 500 €)**`) — sin jerarquía real;
  - **viñetas mezcladas** `-` y `*` en la misma conversación, y líneas
    terminadas en dos espacios (el salto forzado de markdown) que dejaban los
    bloques apelmazados.

La regla 8 del system prompt ya prohíbe casi todo esto y el modelo se la salta
igual, así que aquí no se pide: se aplica. Determinista, sin depender de lo
bueno que sea el LLM del día.

Dos pasos separados a propósito:

  `normalizar(texto)`      — estructura canónica, igual para todos los canales.
  `para_canal(texto, ...)` — traduce esa estructura al dialecto de cada sitio,
                             porque no todos entienden el mismo marcado:

      canal       negrita      listas
      WhatsApp    *así*        sin listas nativas → viñeta «• »
      Telegram    *así*        (parse_mode="Markdown", el legacy)
      Discord     **así**      markdown estándar
      terminal    ANSI         lo pinta `render_terminal`
      web/app     markdown     lo renderiza el front

El módulo es puro (texto → texto) para poder probarlo sin canal ni terminal.
"""
from __future__ import annotations

import re

# Viñeta canónica interna. Se elige «- » porque es markdown válido; los canales
# que no entienden listas la cambian por «• » en `para_canal`.
VINETA = "- "

# Cuántas negritas se toleran en un mismo párrafo de prosa antes de considerar
# que son adorno. Dos deja marcar «entre 4 400 € y 5 000 €» y poco más, que es
# justo el caso en que la negrita sí ayuda.
MAX_NEGRITAS_POR_PARRAFO = 2

# ── Piezas que NO se tocan ───────────────────────────────────────────────────
# Los comandos UI se ejecutan solos y las URLs se rompen si se les mete mano.
_INTOCABLE_RE = re.compile(
    r"```.*?```"                       # bloques de código
    r"|`[^`\n]+`"                      # código en línea
    r"|\[[A-Z_]+:[^\]]*\]"             # [TAP:x,y], [OPEN_APP:…], [BRIGHTNESS:…]
    r"|https?://\S+",                  # enlaces
    re.S,
)


def proteger(texto: str):
    """Aparta lo intocable (código, comandos UI, enlaces) y deja marcadores.

    Es público porque lo necesita también el saneado de `api.py`: allí hay
    treinta expresiones regulares pensadas para PROSA (quitar encabezados,
    aplanar negritas, colapsar espacios) que, sueltas sobre un bloque de
    código, le arrancan la indentación y hasta las almohadillas de los
    comentarios. Lo que no es prosa se aparta antes y vuelve al final.
    """
    guardado: list[str] = []

    def _sacar(m: re.Match) -> str:
        guardado.append(m.group(0))
        return f"\x00{len(guardado) - 1}\x00"

    return _INTOCABLE_RE.sub(_sacar, texto), guardado


def restaurar(texto: str, guardado: list[str]) -> str:
    """Devuelve a su sitio lo que apartó `proteger`."""
    def _meter(m: re.Match) -> str:
        return guardado[int(m.group(1))]

    return re.sub(r"\x00(\d+)\x00", _meter, texto)


# ── Tablas ───────────────────────────────────────────────────────────────────
_FILA_TABLA_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")
# La fila de guiones que separa la cabecera: |---|---|
_SEPARADOR_TABLA_RE = re.compile(r"^[ \t]*\|[\s:|-]+\|[ \t]*$")


def _tabla_a_lista(texto: str) -> str:
    """Convierte una tabla markdown en viñetas.

    En una pantalla de móvil una tabla no cabe — se parte en columnas de una
    letra y es ilegible. Cada fila pasa a una viñeta con las celdas separadas
    por «·», y la cabecera se usa para etiquetar cuando aporta algo.
    """
    lineas = texto.split("\n")
    salida: list[str] = []
    i = 0
    while i < len(lineas):
        if not _FILA_TABLA_RE.match(lineas[i]):
            salida.append(lineas[i])
            i += 1
            continue

        # Recoger el bloque entero de la tabla
        bloque = []
        while i < len(lineas) and (_FILA_TABLA_RE.match(lineas[i])
                                   or _SEPARADOR_TABLA_RE.match(lineas[i])):
            bloque.append(lineas[i])
            i += 1

        filas = [
            [c.strip() for c in _FILA_TABLA_RE.match(f).group(1).split("|")]
            for f in bloque if _FILA_TABLA_RE.match(f) and not _SEPARADOR_TABLA_RE.match(f)
        ]
        if not filas:
            continue

        cabecera = filas[0] if len(filas) > 1 and _SEPARADOR_TABLA_RE.match(
            bloque[1] if len(bloque) > 1 else "") else None
        cuerpo = filas[1:] if cabecera else filas

        for fila in cuerpo:
            celdas = [c for c in fila if c]
            if not celdas:
                continue
            if cabecera and len(cabecera) == len(fila):
                # «Equipo: Alavés · Puntos: 7» sólo si la cabecera dice algo;
                # con cabeceras vacías se queda en la lista de valores.
                partes = [f"{h}: {c}" for h, c in zip(cabecera, fila) if h and c]
                salida.append(VINETA + " · ".join(partes or celdas))
            else:
                salida.append(VINETA + " · ".join(celdas))
    return "\n".join(salida)


# ── Pseudo-títulos y negrita de adorno ───────────────────────────────────────
# Separadores que pueden quedar sueltos entre dos negritas de una misma línea.
_SOLO_SEPARADORES = " \t:.-–—·|"
# Viñeta cuya etiqueta va entera en negrita: «- **CPU: Ryzen 7** (~380 €)».
_NEGRITA_EN_VINETA_RE = re.compile(
    r"^([ \t]*(?:[-*•]|\d+[.)])[ \t]+)\*\*(.+?)\*\*", re.M)
# Marcadores de lista: se unifican todos al canónico.
_MARCADOR_LISTA_RE = re.compile(r"^([ \t]*)[*•+][ \t]+", re.M)
_GUION_LISTA_RE = re.compile(r"^([ \t]*)-[ \t]+", re.M)
_NEGRITA_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _quitar_negrita_de_adorno(texto: str) -> str:
    """Deja la negrita sólo donde de verdad señala algo.

    Regla, deliberadamente simple para que sea predecible: se va la negrita que
    ocupa una línea entera (es un título disfrazado) y la que envuelve la
    etiqueta de una viñeta (el nombre del elemento ya destaca por estar en la
    viñeta). Se queda la que aparece dentro de prosa corrida — «se vende entre
    **4 400 € y 5 000 €**» — que es el único caso en que ayuda a leer.
    """
    # 1. Línea entera en negrita → línea de sección, en texto plano y con dos
    #    puntos para que se lea como lo que es: lo que viene debajo.
    #
    #    Se mira línea a línea y no con una sola regex porque el encabezado
    #    puede venir partido en varias negritas —«**Precio total aproximado:**
    #    **1 620 €**»— y una regex con `.+?` acaba capturando los asteriscos de
    #    en medio y los deja escritos en la respuesta.
    lineas = []
    for linea in texto.split("\n"):
        desnuda = linea.strip()
        if "**" in desnuda and not _NEGRITA_RE.sub("", desnuda).strip(_SOLO_SEPARADORES):
            titulo = _NEGRITA_RE.sub(r"\1", desnuda).strip()
            if not titulo.endswith((":", ".", "?", "!")) and ":" not in titulo:
                titulo += ":"
            lineas.append(titulo)
        else:
            lineas.append(linea)
    texto = "\n".join(lineas)

    # 2. Etiqueta de viñeta en negrita → sin negrita.
    texto = _NEGRITA_EN_VINETA_RE.sub(r"\1\2", texto)

    # 3. Tope por párrafo: si un párrafo va lleno de negritas, son adorno.
    partes = re.split(r"(\n[ \t]*\n)", texto)
    for idx, parte in enumerate(partes):
        if idx % 2:                                  # el separador, intacto
            continue
        if len(_NEGRITA_RE.findall(parte)) > MAX_NEGRITAS_POR_PARRAFO:
            partes[idx] = _NEGRITA_RE.sub(r"\1", parte)
    return "".join(partes)


# ── Espaciado ────────────────────────────────────────────────────────────────
# Dos espacios al final de línea = salto forzado de markdown. Fuera: dejan el
# texto apelmazado sin separación real entre bloques.
_SALTO_FORZADO_RE = re.compile(r"[ \t]+$", re.M)


def _espaciado(texto: str) -> str:
    """Una línea en blanco entre bloques; ninguna dentro de una lista."""
    texto = _SALTO_FORZADO_RE.sub("", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)

    es_vineta = lambda l: bool(re.match(r"^[ \t]*(?:-|\d+[.)])[ \t]+", l))

    salida: list[str] = []
    for linea in texto.split("\n"):
        # La referencia es la última línea CON CONTENIDO, no la anterior a
        # secas: si se mira `salida[-1]` cuando el modelo ha dejado un hueco
        # entre viñetas, esa línea está vacía y la lista nunca se junta.
        anterior = next((l for l in reversed(salida) if l.strip()), "")
        if es_vineta(linea) and es_vineta(anterior):
            # Dos viñetas seguidas son UNA lista: fuera los huecos de en medio.
            while salida and not salida[-1].strip():
                salida.pop()
        elif es_vineta(linea) and anterior and not anterior.rstrip().endswith(":"):
            # Una lista que arranca pegada a un párrafo respira con un hueco.
            if salida and salida[-1].strip():
                salida.append("")
        salida.append(linea)
    return "\n".join(salida).strip()


# El monólogo interno del modelo. El system prompt le pide razonar entre
# paréntesis —«(piensa: …)»— antes de contestar, porque con eso acierta más
# acertijos; lo que nunca debe pasar es que ese razonamiento se publique.
#
# Vive aquí, y no en el saneador de `api.py` donde nació, porque hacían falta
# en DOS sitios: el saneador limpia lo que se envía, pero la conversación se
# guarda antes de pasar por él y en la base de datos quedaban 9 respuestas con
# el monólogo dentro (S64). De ahí salen los ejemplos con los que se entrena
# al modelo local, así que se le estaba enseñando justo lo que no debe hacer.
_MONOLOGO_RE = re.compile(
    r"\(\s*(?:piensa|pienso|pensando|raz(?:ono|onando)|reflexion(?:o|ando)|"
    r"nota\s+interna|internamente|an[aá]lisis\s+interno)\b\s*:?.*?\)",
    re.I | re.S,
)
_MONOLOGO_LINEA_RE = re.compile(
    r"(?im)^\s*(?:piensa|pienso|razono|reflexiono)\s*:.*$")
# Y el paréntesis que el modelo abre y no cierra nunca: «(piensa: El usuario me
# pide enlaces de Amazon…» y ahí se acaba el mensaje.
_MONOLOGO_ABIERTO_RE = re.compile(
    r"\(\s*(?:piensa|pienso|pensando|raz(?:ono|onando)|reflexion(?:o|ando)|"
    r"nota\s+interna|internamente|an[aá]lisis\s+interno)\b\s*:?[^)]*$",
    re.I | re.S,
)


# El monólogo puede traer paréntesis dentro —«(piensa: … del Campeonato de
# España (CEK) de la RFEDA … actual)»— y `.*?\)` se paraba en el primero: se
# publicó «de la RFEDA donde destacan pilotos…», con el razonamiento dentro y
# el paréntesis final colgando (sesión 74, chat real). El cierre que le toca se
# encuentra contando niveles.
_MONOLOGO_INICIO_RE = re.compile(
    r"\(\s*(?:piensa|pienso|pensando|raz(?:ono|onando)|reflexion(?:o|ando)|"
    r"nota\s+interna|internamente|an[aá]lisis\s+interno)\b\s*:?",
    re.I,
)


def _quitar_monologos(texto: str) -> str:
    """Quita cada «(piensa: …)» hasta SU paréntesis de cierre; sin cierre,
    hasta el final (el modelo lo abrió y no llegó a cerrarlo)."""
    partes = []
    pos = 0
    while True:
        m = _MONOLOGO_INICIO_RE.search(texto, pos)
        if not m:
            partes.append(texto[pos:])
            return "".join(partes)
        partes.append(texto[pos:m.start()])
        nivel, fin = 0, len(texto)
        for i in range(m.start(), len(texto)):
            if texto[i] == "(":
                nivel += 1
            elif texto[i] == ")":
                nivel -= 1
                if nivel == 0:
                    fin = i + 1
                    break
        pos = fin


# Sesión 74 — con resultados de internet delante, el modelo más flojo escribió
# «gokarts<pueblo>.com» y «gokarts<otro-pueblo>.com»: dominios con buena pinta
# que no venían en lo encontrado. Un enlace inventado manda a una web que no
# existe, o que es de otro. Si hay fuente y el dominio no sale en ella, el
# enlace se va; si la línea solo servía para darlo («Sitio web oficial: …»),
# se va la línea entera.
_ENLACE_MD_RE = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^)\s]+)\)")
_URL_SUELTA_RE = re.compile(r"https?://[^\s)\]>»\"']+")
_LINEA_DE_ENLACE_RE = re.compile(
    r"^\s*(?:[-*•·]\s*)?(?:sitio\s+web(?:\s+oficial)?|p[aá]gina(?:\s+web|\s+oficial)?|"
    r"web(?:\s+oficial)?|enlace|link|url)\s*:", re.I)


def _dominio(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def quitar_enlaces_sin_fuente(respuesta: str, fuente: str) -> str:
    """Quita los enlaces cuyo dominio no aparece en `fuente`. Sin fuente no
    toca nada: sin algo con qué comparar, quitar sería adivinar."""
    if not respuesta or not fuente:
        return respuesta
    fuente_baja = fuente.lower()

    def _vale(url: str) -> bool:
        dom = _dominio(url)
        return bool(dom) and dom in fuente_baja

    lineas = []
    for linea in respuesta.split("\n"):
        quitado = False

        def _md(m):
            nonlocal quitado
            if _vale(m.group(2)):
                return m.group(0)
            quitado = True
            return m.group(1)

        def _suelta(m):
            nonlocal quitado
            if _vale(m.group(0)):
                return m.group(0)
            quitado = True
            return ""

        nueva = _ENLACE_MD_RE.sub(_md, linea)
        # Las que ya son enlace markdown aceptado no se vuelven a mirar.
        partes = re.split(r"(\[[^\]\n]{1,120}\]\(https?://[^)\s]+\))", nueva)
        nueva = "".join(p if i % 2 else _URL_SUELTA_RE.sub(_suelta, p)
                        for i, p in enumerate(partes))
        if quitado and _LINEA_DE_ENLACE_RE.match(linea):
            continue
        lineas.append(nueva.rstrip() if quitado else nueva)
    return "\n".join(lineas)


# Títulos citados entre comillas: «Dune», "Dune: Part Three", “Avengers”.
_TITULO_CITADO_RE = re.compile(r'[«“"]([A-ZÁÉÍÓÚÑ0-9][^«»“”"\n]{1,60})[»”"]')
_PALABRA_TITULO_RE = re.compile(r"[a-z0-9ñ]{4,}")
# Palabras de relleno de título que el modelo traduce o añade por su cuenta
# («Part Three» cuando la fuente dice «la tercera de Dune»): no cuentan.
_GENERICAS_TITULO = frozenset("""
    part parte three tres four cuatro five cinco chapter capitulo volume volumen
    season temporada pelicula serie saga movie film final""".split())


def _sin_tildes(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower())
                   if unicodedata.category(c) != "Mn")


def titulos_sin_fuente(respuesta: str, fuente: str,
                       pide_obras: bool = False) -> list[str]:
    """Los títulos citados en `respuesta` de los que `fuente` no dice nada.

    26 sep 2026: a «recomiéndame una peli de ciencia ficción reciente» contestó
    «Avengers: Doomsday», que no salía en ningún resultado, y la dio por
    estrenada. Un título está respaldado si TODAS sus palabras con contenido
    (4+ letras, sin las genéricas de título) están en la fuente como palabras
    enteras: con una sola bastaba que la fuente dijera «Avengers» para dar por
    buena «Avengers: Doomsday» (revisión de Codex).

    Una sola palabra entre comillas sólo cuenta como título si se preguntaba
    por obras (`pide_obras`): «contraste «Alta»» en una respuesta sobre fotos
    disparaba la regeneración y costó 8 s (26 sep 2026).
    """
    if not respuesta or not fuente:
        return []
    fuente_p = set(_PALABRA_TITULO_RE.findall(_sin_tildes(fuente)))
    fuera = []
    for m in _TITULO_CITADO_RE.finditer(respuesta):
        titulo = m.group(1).strip()
        if len(titulo.split()) < 2 and not pide_obras:
            continue
        palabras = [p for p in _PALABRA_TITULO_RE.findall(_sin_tildes(titulo))
                    if p not in _GENERICAS_TITULO]
        if palabras and not all(p in fuente_p for p in palabras):
            if titulo not in fuera:
                fuera.append(titulo)
    return fuera


# Sesión 74 — las marcas de cita que gpt-oss trae de su entrenamiento: «452 670
# habitantes según el censo de 2021 【Wikipedia 2021 Census】». No enlazan a nada
# y en el chat son ruido. Son andamiaje del modelo, como el «(piensa: …)».
_MARCA_DE_CITA_RE = re.compile(r"\s*【[^】\n]{1,80}】")


# El razonamiento sin paréntesis: un párrafo que habla del usuario en tercera
# persona o de lo que ella tiene que hacer. 27 sep 2026, con Gemini: «Sin
# embargo, no tengo la capacidad de "recrear"… Necesito explicarle que sin la
# foto, no puedo hacer nada. La información de internet sobre el calendario…
# no es relevante para esta tarea.» y DESPUÉS la respuesta de verdad.
_PARRAFO_META_RE = re.compile(
    r"(?i)\b(?:necesito\s+explicarle|debo\s+(?:explicarle|decirle|responderle|contestarle)|"
    r"tengo\s+que\s+(?:explicarle|decirle|responderle)|voy\s+a\s+(?:explicarle|responderle)|"
    r"el\s+usuario\s+me\s+(?:pide|ha\s+pedido|pregunta|dice|ha\s+dicho)|"
    r"la\s+informaci[oó]n\s+(?:de\s+internet|del\s+bloque|proporcionada)\b[^.]{0,120}?"
    r"no\s+es\s+relevante)")


def _sin_parrafos_meta(texto: str) -> str:
    parrafos = re.split(r"\n\s*\n", texto)
    if len(parrafos) < 2:
        return texto
    quedan = [p for p in parrafos if not _PARRAFO_META_RE.search(p)]
    # Si todo era razonamiento, se deja: decide quien llama (ver abajo).
    return "\n\n".join(quedan).strip() if quedan else texto


def sin_monologo(texto: str) -> str:
    """Quita el «(piensa: …)» y las marcas «【…】». Devuelve "" si no quedaba nada más.

    Ese vacío es información, no un fallo: significa que el modelo escribió
    SOLO el razonamiento y no llegó a contestar. Quien llame decide qué hacer
    con eso —`api.py` pide la respuesta de otra forma— pero nadie debe
    publicar el monólogo por el hecho de que fuera lo único que había.
    """
    if not texto:
        return texto or ""
    limpio = _quitar_monologos(texto)
    limpio = _MONOLOGO_LINEA_RE.sub("", limpio).strip()
    limpio = _MONOLOGO_ABIERTO_RE.sub("", limpio).strip()
    limpio = _MARCA_DE_CITA_RE.sub("", limpio).strip()
    return _sin_parrafos_meta(limpio)


def normalizar(texto: str) -> str:
    """Estructura canónica: la misma respuesta, ordenada igual siempre."""
    if not texto or not texto.strip():
        return texto

    protegido, guardado = proteger(texto)
    # Unificar marcadores ANTES de mirar las negritas, para que la regla de la
    # viñeta valga igual escriba el modelo «-», «*» o «•».
    protegido = _MARCADOR_LISTA_RE.sub(r"\1" + VINETA, protegido)
    protegido = _quitar_negrita_de_adorno(protegido)
    protegido = _espaciado(protegido)
    return restaurar(protegido, guardado)


# ── Dialectos por canal ──────────────────────────────────────────────────────
# WhatsApp y Telegram (parse_mode="Markdown", el legacy) marcan la negrita con
# un solo asterisco; con dos se ven los asteriscos literales en pantalla.
_CANALES_UN_ASTERISCO = ("whatsapp", "wa", "telegram")
# Canales sin listas nativas: la viñeta tiene que ser un carácter de verdad.
_CANALES_SIN_LISTAS = ("whatsapp", "wa", "telegram", "termux", "terminal", "cli")
# Quién sabe pintar una tabla de verdad. Es lista blanca a propósito: un canal
# desconocido recibe viñetas, que se leen en cualquier sitio. El chat web tiene
# `<table>` con sus estilos desde la sesión 47 y hasta ahora no veía ni una:
# la tabla se aplanaba en `normalizar()`, o sea para todo el mundo a la vez.
_CANALES_CON_TABLAS = ("web",)


def para_canal(texto: str, canal: str = "") -> str:
    """Traduce el markdown canónico al dialecto del canal indicado.

    No normaliza: se asume que el texto ya pasó por `normalizar()`. Son dos
    pasos separados porque la estructura se decide una vez, al generar, y la
    traducción depende de por dónde salga cada copia del mensaje.
    """
    if not texto:
        return texto
    canal = (canal or "").strip().lower()

    protegido, guardado = proteger(texto)
    if canal not in _CANALES_CON_TABLAS:
        protegido = _tabla_a_lista(protegido)
    if canal in _CANALES_UN_ASTERISCO:
        protegido = _NEGRITA_RE.sub(r"*\1*", protegido)
    if canal in _CANALES_SIN_LISTAS:
        protegido = _GUION_LISTA_RE.sub(r"\1• ", protegido)
    return restaurar(protegido, guardado)


# Nombres anteriores, por si algo externo los usaba.
_proteger = proteger
_restaurar = restaurar
