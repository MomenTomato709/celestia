"""Las piezas de un proyecto generado: que salgan enteras y comprobadas.

Examen de nivel (5 oct 2026), la web de la peluquería: el `index.html` acababa a
mitad del pie y el CSS a mitad de una regla (cada fichero se pedía con un tope de
2.500 tokens y lo que se cortaba se guardaba tal cual), `hero.jpg` era un script
de Python (el sistema decía «responde ÚNICAMENTE con código Python» para todo),
se inventó un teléfono y un correo, y nadie miró nada antes de entregarlo. Con
MiMo, además, el plan llegaba vacío y el proyecto moría sin reintentar.

Aquí, sin red ni modelo:
  `leer_plan(texto)` saca la lista de ficheros aunque venga con texto alrededor,
  `es_binaria(ruta)` dice qué no se puede escribir como texto,
  `cortado(ruta, contenido)` y `comprobar(ruta, contenido)` miran cada pieza,
  `unir(contenido, siguiente)` pega la continuación sin repetir lo ya escrito y
  `referencias_rotas(carpeta)` busca lo que el HTML enlaza y no existe.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set, Tuple

SISTEMA_ARCHIVO = (
    "Eres un programador sénior. Escribes UN archivo de un proyecto. Responde ÚNICAMENTE "
    "con el contenido de ese archivo, entero, sin explicaciones ni bloques markdown.")

REGLAS = (
    "Reglas:\n"
    "- Escribe el archivo ENTERO: nada de «...», «resto igual» ni «añade aquí».\n"
    "- No inventes datos de contacto (teléfono, correo, dirección, redes) que no estén en "
    "la petición, ni pongas marcadores como «[Tu teléfono]» a la vista: lo que no se ha "
    "dado, no se enseña (sin esa línea o ese botón).\n"
    "- Ningún texto de relleno ni corchetes por rellenar en lo que ve el visitante.\n"
    "- No hay imágenes ni archivos binarios en el proyecto: usa degradados CSS, emojis o "
    "SVG dentro del HTML.\n"
    "- Usa sólo los archivos de la estructura: no enlaces otros que no existen.")

# Juicio de la web (7 oct 2026, 7,5 frente a 9,6): sin menú en el ordenador, unas
# tijeras de marca de agua a pantalla completa y los precios sólo pintados por JS.
REGLAS_WEB = (
    "Diseño de la web:\n"
    "- El menú se ve en el ordenador; el botón de menú (hamburguesa) sólo en el móvil.\n"
    "- Contraste siempre: nada de texto claro sobre fondo claro ni oscuro sobre oscuro.\n"
    "- Ninguna ilustración ni SVG decorativo que tape el contenido u ocupe media pantalla.\n"
    "- Lo importante (servicios, precios, horario, dirección) escrito en el HTML; el "
    "JavaScript, para lo interactivo, no para pintar el contenido principal.\n"
    "- Que parezca de una marca, no una plantilla: jerarquía tipográfica clara, mucho aire, "
    "una paleta corta y coherente, y móvil primero.\n"
    # 11 oct 2026: bajo el logo salía «[object Object]» (un objeto en textContent).
    "- Lo que pintes con JavaScript, siempre un texto: nunca un objeto ni algo que pueda "
    "quedar «undefined» («[object Object]» a la vista).")
_EXT_REGLAS_WEB = (".html", ".htm", ".css", ".scss", ".js", ".mjs", ".jsx", ".tsx", ".vue")

# Examen de nivel, 11 oct 2026 (cuentas-viaje): las dos pasadas que arrancaban metían
# a quien paga en el reparto aunque no estuviera en «--entre» («un regalo de Ana entre
# Luis y Marta» lo pagaba también Ana), aceptaban un importe de 0 y redondeaban en
# silencio «12,345». Ninguna era un despiste: lo decidió el modelo por su cuenta.
REGLAS_CODIGO = (
    "Fiel a la petición, al pie de la letra:\n"
    "- Haz lo que pide y como lo pide. No añadas por tu cuenta reglas que cambian el "
    "resultado: meter en una lista a alguien o algo que no está en ella, cambiar un "
    "formato que se ha pedido, corregir o redondear en silencio lo que llega.\n"
    "- Lo que no tiene sentido (un valor cero o negativo donde no cabe, más decimales de "
    "los que admite, una lista vacía) es un error que se explica, no algo que se arregla "
    "callando.")
# Cuentas-viaje (11 oct 2026, ya con la caja): sus 27 pruebas pasaban y el reparto por
# pesos fallaba en algunas combinaciones que ninguna miraba. Las pruebas que pillan eso
# son las de muchos casos al azar sobre una regla que se cumple siempre.
REGLAS_PRUEBAS = (
    "Pruebas que pillen fallos de verdad:\n"
    "- Además de los ejemplos, comprueba las reglas que tienen que cumplirse SIEMPRE (que "
    "algo cuadre, que no se pierda ni se invente nada, que el resultado sea válido) con "
    "cientos de datos generados al azar con una semilla fija (random.Random(1234)), en un "
    "bucle dentro de una prueba: no escribas cientos de pruebas a mano.\n"
    "- Compacto: de 150 a 400 líneas, pocas pruebas bien elegidas (un fichero de 1.900 "
    "líneas se cortó y no compilaba).\n"
    "- Prueba los bordes: vacío, un solo elemento, cantidades que no salen exactas, valores "
    "grandes, repetidos y errores de uso (con assertRaises cuando el código lanza una "
    "excepción o sale con SystemExit).")
_EXT_REGLAS_CODIGO = (".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".lua", ".luau",
                      ".java", ".kt", ".go", ".rs", ".rb", ".php", ".sh", ".cs", ".swift",
                      ".c", ".cpp", ".h")


def reglas_para(ruta: str) -> str:
    """Las reglas de siempre; si el fichero es código, las de ser fiel a la petición, y
    si es de una web, las de diseño."""
    ext = PurePosixPath(ruta).suffix.lower()
    reglas = REGLAS
    if ext in _EXT_REGLAS_CODIGO:
        reglas += "\n" + REGLAS_CODIGO
        if es_prueba(ruta):
            reglas += "\n" + REGLAS_PRUEBAS
    if ext in _EXT_REGLAS_WEB:
        reglas += "\n" + REGLAS_WEB
    return reglas


BINARIAS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
            ".avif", ".heic", ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".woff",
            ".woff2", ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz", ".rar", ".7z", ".exe",
            ".bin", ".so", ".dll", ".apk", ".jar", ".pyc", ".db", ".sqlite"}
MAX_CONTINUACIONES = 2
_COLA = 600                     # lo último que se le enseña para que siga


def leer_plan(texto: str) -> Optional[list]:
    """La lista de ficheros del plan, o None si no hay forma de leerla."""
    limpio = re.sub(r"```(?:json)?", "", texto or "").strip()
    candidatos = [limpio]
    ini, fin = limpio.find("["), limpio.rfind("]")
    if 0 <= ini < fin:
        candidatos.append(limpio[ini:fin + 1])
    for c in candidatos:
        try:
            datos = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(datos, dict):
            datos = next((v for v in datos.values() if isinstance(v, list)), None)
        if isinstance(datos, list):
            plan = [a for a in datos if isinstance(a, dict) and isinstance(a.get("ruta"), str)
                    and a["ruta"].strip()]
            if plan:
                return plan
    return None


def es_binaria(ruta: str) -> bool:
    return Path(str(ruta)).suffix.lower() in BINARIAS


def _balance(texto: str, abre: str, cierra: str, css: bool = False) -> int:
    # Sin comentarios ni cadenas: un «{» dentro de un texto no cuenta. En CSS no hay
    # comentarios «//» (y «url(https://…)» se comería el resto de la línea).
    linea = "" if css else r"|//[^\n]*"
    sin = re.sub(r"/\*[\s\S]*?\*/" + linea + r"|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|"
                 r"`(?:\\.|[^`\\])*`", "", texto)
    return sin.count(abre) - sin.count(cierra)


# Examen de nivel, 11 oct 2026 (cuentas-viaje, DeepSeek): saldos.py acababa en una
# línea «</final>» y no compilaba; rehecho, salió igual.
_ENVOLTORIO_RE = re.compile(
    r"^[ \t]*</?(?:final|answer|output|response|respuesta|archivo|file)\b[^>\n]*>[ \t]*$", re.I)


def _sin_envoltorio(texto: str) -> str:
    """Sin las etiquetas sueltas con las que el modelo envuelve a veces el archivo."""
    lineas = texto.split("\n")
    antes = len(lineas)
    while lineas and _ENVOLTORIO_RE.match(lineas[0]):
        lineas.pop(0)
    while lineas and _ENVOLTORIO_RE.match(lineas[-1]):
        lineas.pop()
    return texto if len(lineas) == antes else "\n".join(lineas).strip("\n")


def quitar_vallas(texto: str) -> str:
    """Quita el «```html … ```» que pone el modelo alrededor del archivo (el
    limpiador de siempre sólo conocía python/bash, y dejaba «html» en la línea 1),
    y las etiquetas sueltas («</final>») de antes o después.
    Sólo el de fuera: los bloques de un README son suyos."""
    t = _sin_envoltorio((texto or "").strip())
    m = re.match(r"^```[\w+.-]*[ \t]*\n([\s\S]*?)\n?```\s*$", t)
    if m:
        return _sin_envoltorio(m.group(1))
    sin_apertura = re.sub(r"^```[\w+.-]*[ \t]*\n", "", t)   # cortado: sin valla de cierre
    return _sin_envoltorio(sin_apertura) if sin_apertura != t else t


def cortado(ruta: str, contenido: str) -> str:
    """Por qué parece que el archivo se quedó a medias, o ''."""
    ext = Path(ruta).suffix.lower()
    c = (contenido or "").rstrip()
    if not c:
        return "está vacío"
    if ext in (".html", ".htm"):
        if re.search(r"<html\b", c, re.I) and not re.search(r"</html\s*>\s*$", c, re.I):
            return "el HTML no llega a cerrar </html>"
        if re.search(r"<[^>]*$", c[-200:]):
            return "acaba a mitad de una etiqueta"
    elif ext in (".css", ".scss", ".js", ".mjs", ".ts", ".json", ".java", ".c", ".cpp",
                 ".cs", ".go", ".rs", ".php", ".kt", ".swift", ".luau", ".lua"):
        if ext in (".lua", ".luau"):
            return ""
        if _balance(c, "{", "}", css=ext in (".css", ".scss")) > 0:
            return "quedan llaves «{» sin cerrar"
    elif ext == ".py":
        try:
            compile(c, ruta, "exec")
        except SyntaxError as e:
            if re.search(r"EOF|never closed|unexpected end", str(e), re.I):
                return f"Python a medias ({e.msg})"
    return ""


class _Etiquetas(HTMLParser):
    VACIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
              "source", "track", "wbr", "param", "keygen", "command"}
    CUENTAN = {"html", "head", "body", "div", "section", "main", "header", "footer", "nav",
               "form", "ul", "ol", "table", "article", "aside", "script", "style", "select"}

    def __init__(self):
        super().__init__()
        self.abiertas: List[str] = []
        self.sin_cerrar: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.CUENTAN and tag not in self.VACIAS:
            self.abiertas.append(tag)

    def handle_endtag(self, tag):
        # Un </body> con un <div> abierto dentro: el <div> se quedó sin cerrar.
        if tag in self.CUENTAN and tag in self.abiertas:
            while self.abiertas:
                quitada = self.abiertas.pop()
                if quitada == tag:
                    break
                self.sin_cerrar.append(quitada)


# JSX («<div>» dentro del código) no es JavaScript para node: comprobarlo daría
# «no compila» y se rehacía un fichero bueno (revisión, 6 oct 2026).
_JSX_RE = re.compile(r"(?:return|=>|=)\s*\(?\s*<[A-Za-z]")
_ESM_RE = re.compile(r"^\s*(?:import\s|export\s)", re.M)
MAX_SCRIPTS = 3


def _node_check(contenido: str) -> str:
    node = shutil.which("node")
    if not node or _JSX_RE.search(contenido):
        return ""
    sufijo = ".mjs" if _ESM_RE.search(contenido) else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=sufijo, delete=False, encoding="utf-8") as f:
        f.write(contenido)
        nombre = f.name
    try:
        r = subprocess.run([node, "--check", nombre], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            linea = next((l for l in (r.stderr or "").splitlines() if "Error" in l), "")
            return linea.strip()[:160] or "JavaScript con errores de sintaxis"
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        Path(nombre).unlink(missing_ok=True)
    return ""


def se_puede_validar(ruta: str) -> bool:
    return Path(ruta).suffix.lower() in (".py", ".json")


def vale_tal_cual(ruta: str, contenido: str) -> bool:
    """¿Compila (un .py) o se lee (un .json) tal como está?"""
    ext = Path(ruta).suffix.lower()
    try:
        if ext == ".py":
            compile(contenido, ruta, "exec")
            return True
        if ext == ".json":
            json.loads(re.sub(r"/\*[\s\S]*?\*/|^\s*//[^\n]*", "", contenido, flags=re.M))
            return True
    except (SyntaxError, ValueError):
        return False
    return False


def comprobar(ruta: str, contenido: str) -> List[str]:
    """Los fallos que se ven sin ejecutar nada: cortado, mal cerrado, no compila."""
    fallos = []
    motivo = cortado(ruta, contenido)
    if motivo:
        fallos.append(motivo)
    ext = Path(ruta).suffix.lower()
    c = contenido or ""
    if ext in (".html", ".htm") and not motivo:
        p = _Etiquetas()
        try:
            p.feed(c)
            p.close()
        except Exception as e:                       # HTMLParser casi nunca se queja
            fallos.append(f"HTML ilegible: {e}")
        sin_cerrar = [t for t in p.sin_cerrar + p.abiertas if t not in ("html", "head", "body")]
        if sin_cerrar:
            fallos.append("etiquetas sin cerrar: " + ", ".join(sorted(set(sin_cerrar))))
        # JavaScript dentro del HTML: sólo el que lo es («application/ld+json», una
        # plantilla o «text/babel» no se ejecutan como JS).
        scripts = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)</script>", c, re.I)
        revisados = 0
        for atributos, js in scripts:
            tipo = re.search(r"\btype\s*=\s*[\"']?([\w/+.-]+)", atributos, re.I)
            if tipo and tipo.group(1).lower() not in ("module", "text/javascript",
                                                      "application/javascript"):
                continue
            if not js.strip() or revisados >= MAX_SCRIPTS:
                continue
            revisados += 1
            error = _node_check(("export {};\n" if tipo and tipo.group(1).lower() == "module"
                                 else "") + js)
            if error:
                fallos.append(f"el <script> no compila: {error}")
                break
    elif ext in (".js", ".mjs") and not motivo:
        error = _node_check(c)
        if error:
            fallos.append(f"no compila: {error}")
    elif ext == ".json" and not motivo:
        try:
            json.loads(c)
        except ValueError as e:
            # tsconfig.json y compañía llevan comentarios: eso no es un fallo.
            try:
                json.loads(re.sub(r"/\*[\s\S]*?\*/|^\s*//[^\n]*", "", c, flags=re.M))
            except ValueError:
                fallos.append(f"JSON inválido: {e}")
    elif ext == ".py" and not motivo:
        try:
            compile(c, ruta, "exec")
        except SyntaxError as e:
            fallos.append(f"no compila: {e.msg} (línea {e.lineno})")
        else:
            fallos += [f"llamada que no casa con su función: {x}" for x in llamadas_mal(c)]
            fallos += [f"usa {nombre} sin haberlo definido (línea {linea})"
                       for nombre, linea in sin_definir(c)[:3]]
    return fallos


def llamadas_mal(codigo: str) -> List[str]:
    """Llamadas a funciones del propio fichero con un número de argumentos que no
    casa, leyendo el código sin ejecutarlo (examen de nivel, 7 oct 2026: `def
    main(argv)` y al final `sys.exit(main())`: TypeError nada más arrancar). No se
    ejecuta nada generado: eso Enzo lo quitó a propósito (25 sep 2026)."""
    import ast
    try:
        arbol = ast.parse(codigo)
    except SyntaxError:
        return []
    firmas = {}
    for nodo in arbol.body:                         # sólo las de nivel de módulo
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = nodo.args
            if a.vararg or a.kwarg:
                continue
            posicionales = [x.arg for x in a.posonlyargs + a.args]
            obligatorios = len(posicionales) - len(a.defaults)
            solo_nombre = {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None}
            firmas[nodo.name] = (posicionales, obligatorios, {x.arg for x in a.kwonlyargs},
                                 solo_nombre)
    # Si el nombre se reasigna en algún sitio, no se sabe a qué llama: fuera.
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Store):
            firmas.pop(nodo.id, None)
    fallos = []
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
                and nodo.func.id in firmas):
            continue
        if any(isinstance(x, ast.Starred) for x in nodo.args) or any(
                k.arg is None for k in nodo.keywords):
            continue                                 # f(*a) o f(**k): no se sabe
        posicionales, obligatorios, kwonly, kwonly_oblig = firmas[nodo.func.id]
        dados = len(nodo.args)
        por_nombre = {k.arg for k in nodo.keywords}
        faltan = [x for x in posicionales[dados:obligatorios] if x not in por_nombre]
        faltan += [x for x in kwonly_oblig if x not in por_nombre]
        sobran = dados > len(posicionales)
        raros = por_nombre - set(posicionales) - kwonly
        if faltan or sobran or raros:
            motivo = (f"le falta {', '.join(faltan)}" if faltan else
                      "le sobran argumentos" if sobran else
                      f"no tiene el parámetro {', '.join(sorted(raros))}")
            fallos.append(f"línea {nodo.lineno}: {nodo.func.id}() {motivo}")
    return fallos


# ── Lo que cada fichero ofrece a los demás y lo que espera de ellos ──────────
# Examen de nivel, 7 oct 2026 (ordenar-fotos): cada fichero sólo veía el plan, y
# se inventaba el nombre de lo que usaba de los otros: `plan.py` pedía
# `obtener_fecha` a un `metadatos.py` que la había llamado `fecha_de_foto`, y
# `cli.py` pedía `aplicar_plan` a un `ejecutor.py` que tenía `ejecutar`.
# ImportError nada más arrancar. Sólo Python, leído sin ejecutar nada.
MAX_INTERFAZ = 8000             # con lo que dice cada docstring
MAX_DOC = 300


def _arbol(contenido: str):
    import ast
    try:
        return ast.parse(contenido)
    except (SyntaxError, ValueError):
        return None


def _nivel_de_modulo(cuerpo) -> Iterable:
    """Las sentencias de nivel de módulo, también dentro de if/try/with (el
    «try: from PIL import Image» de siempre), pero no dentro de funciones."""
    import ast
    for nodo in cuerpo:
        yield nodo
        if isinstance(nodo, (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith)):
            yield from _nivel_de_modulo(nodo.body + nodo.orelse
                                        if hasattr(nodo, "orelse") else nodo.body)
        elif isinstance(nodo, ast.Try) or type(nodo).__name__ == "TryStar":
            yield from _nivel_de_modulo(nodo.body + nodo.orelse + nodo.finalbody
                                        + [s for h in nodo.handlers for s in h.body])


def _nombres_asignados(objetivo) -> Iterable[str]:
    import ast
    if isinstance(objetivo, ast.Name):
        yield objetivo.id
    elif isinstance(objetivo, (ast.Tuple, ast.List)):
        for x in objetivo.elts:
            yield from _nombres_asignados(x)


def _definidos(contenido: str) -> Optional[Set[str]]:
    """Los nombres de nivel de módulo; None si no se puede saber (no compila, lleva
    `import *` o un `__getattr__` de módulo)."""
    import ast
    arbol = _arbol(contenido)
    if arbol is None:
        return None
    nombres: Set[str] = set()
    for nodo in _nivel_de_modulo(arbol.body):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.add(nodo.name)
        elif isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                nombres.update(_nombres_asignados(objetivo))
        elif isinstance(nodo, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            nombres.update(_nombres_asignados(nodo.target))
        elif isinstance(nodo, (ast.With, ast.AsyncWith)):
            for item in nodo.items:
                nombres.update(_nombres_asignados(item.optional_vars))
        elif isinstance(nodo, ast.Import):
            nombres.update(a.asname or a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            if any(a.name == "*" for a in nodo.names):
                return None
            nombres.update(a.asname or a.name for a in nodo.names)
    # «global CONFIG» dentro de una función también crea el nombre en el módulo.
    nombres.update(n for g in ast.walk(arbol) if isinstance(g, ast.Global) for n in g.names)
    return None if "__getattr__" in nombres else nombres


# ── Lo que se usa y no existe en ninguna parte ──────────────────────────────
# Examen de nivel, 11 oct 2026 (cuentas-viaje, DeepSeek): el comentario de encima
# se tragó «FICHERO_POR_DEFECTO = …» (se perdió un salto de línea al pegar la
# continuación) y «def cargar(ruta=FICHERO_POR_DEFECTO)» daba NameError al importar;
# en la pasada siguiente, guardar() llamaba a validar_estado y la función era
# _validar_estado. Compilaba, así que nadie lo vio. En los 385 .py del repositorio
# sólo salta en entregas del examen que fallaban de verdad.
_SIN_DEFINIR_OK = {"WindowsError", "unicode", "basestring", "long", "xrange", "raw_input",
                   "unichr", "file", "reload", "reduce", "__file__", "__builtins__",
                   "__path__", "__cached__", "__loader__", "__spec__", "__package__",
                   "__class__", "__module__", "__qualname__", "__annotations__", "__dict__"}


def sin_definir(contenido: str) -> List[Tuple[str, int]]:
    """(nombre, línea) de lo que el fichero usa y no está definido en ninguna parte
    de él ni es de Python: NameError al importarlo o al llamar a esa función. Vacío
    si no se puede saber (no compila, `import *`, globals(), match…)."""
    import ast
    import builtins
    arbol = _arbol(contenido)
    if arbol is None or re.search(r"\b(?:globals|vars|exec|locals)\s*\(|__getattr__", contenido):
        return []
    if _definidos(contenido) is None or any(type(n).__name__ == "Match" for n in ast.walk(arbol)):
        return []
    # Generoso con lo definido: cualquier nombre que se asigne, importe o declare en
    # el fichero (también dentro de funciones) cuenta. Sólo se avisa de lo que no
    # aparece definido en ninguna parte.
    definidos = set(dir(builtins)) | _SIN_DEFINIR_OK
    for n in ast.walk(arbol):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            definidos.add(n.id)
        elif isinstance(n, ast.arg):
            definidos.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definidos.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            definidos.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            definidos.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            definidos.update(n.names)
    vistos: Dict[str, int] = {}
    for n in ast.walk(arbol):
        if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id not in definidos and n.id not in vistos):
            vistos[n.id] = n.lineno
    return sorted(vistos.items(), key=lambda x: x[1])


def _modulo_de(importador: str, nivel: int, modulo: Optional[str],
               rutas: Set[str]) -> Optional[str]:
    """El fichero del proyecto al que apunta un import, o None si es de fuera."""
    partes = modulo.split(".") if modulo else []
    if nivel:
        base = PurePosixPath(importador).parent
        for _ in range(nivel - 1):
            base = base.parent
        bases = [base]
    elif partes:
        # Absoluto: desde la raíz del proyecto o desde la carpeta del propio script.
        bases = [PurePosixPath("."), PurePosixPath(importador).parent]
    else:
        return None
    for b in bases:
        cand = b.joinpath(*partes)
        for r in (f"{cand.as_posix()}.py", f"{cand.as_posix()}/__init__.py"):
            r = r[2:] if r.startswith("./") else r
            if r in rutas:
                return r
    return None


def _es_submodulo(destino: str, nombre: str, rutas: Set[str]) -> bool:
    """`from paquete import modulo`: el nombre es un fichero, no algo definido."""
    if not destino.endswith("__init__.py"):
        return False
    carpeta = destino[:-len("__init__.py")]
    return f"{carpeta}{nombre}.py" in rutas or f"{carpeta}{nombre}/__init__.py" in rutas


def _submodulo(destino: str, nombre: str, rutas: Set[str]) -> Optional[str]:
    carpeta = destino[:-len("__init__.py")]
    for r in (f"{carpeta}{nombre}.py", f"{carpeta}{nombre}/__init__.py"):
        if r in rutas:
            return r
    return None


def usos(ruta: str, contenido: str, rutas: Set[str]) -> Dict[str, Set[str]]:
    """Lo que un .py usa de otros ficheros del proyecto: {fichero: {nombres}}. Lo
    que importa con «from x import y» y lo que pide a un módulo suyo con «x.y».

    Examen de nivel, 11 oct 2026 (cuentas-viaje): `cuentas.py` hacía «from cuentas_lib
    import reparto, almacen» y llamaba a reparto.repartir_igual y almacen.cargar, que
    se llamaban repartir_gasto y cargar_gastos. Sólo se miraba el «from x import y»."""
    import ast
    arbol = _arbol(contenido) if ruta.endswith(".py") else None
    if arbol is None:
        return {}
    pedidos: Dict[str, Set[str]] = {}
    modulos: Dict[str, str] = {}                  # nombre en este fichero → fichero
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for a in nodo.names:
                if a.asname or "." not in a.name:     # «import a.b» ata «a», no «a.b»
                    destino = _modulo_de(ruta, 0, a.name, rutas)
                    if destino and destino != ruta:
                        modulos[a.asname or a.name] = destino
            continue
        if not isinstance(nodo, ast.ImportFrom):
            continue
        destino = _modulo_de(ruta, nodo.level, nodo.module, rutas)
        if not destino or destino == ruta:
            continue
        nombres = set()
        for a in nodo.names:
            if a.name == "*":
                continue
            if _es_submodulo(destino, a.name, rutas):
                modulos[a.asname or a.name] = _submodulo(destino, a.name, rutas)
            else:
                nombres.add(a.name)
        if nombres:
            pedidos.setdefault(destino, set()).update(nombres)
    if modulos:
        # Si el nombre también es una variable o un argumento, «x.y» puede no ser el módulo.
        atados = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, (ast.Store, ast.Del))}
        atados |= {a.arg for a in ast.walk(arbol) if isinstance(a, ast.arg)}
        atados |= {d.name for d in ast.walk(arbol) if isinstance(
            d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)) and d.name}
        for n in ast.walk(arbol):
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    and isinstance(n.value, ast.Name) and n.value.id in modulos
                    and n.value.id not in atados):
                destino = modulos[n.value.id]
                if n.attr.startswith("__") and n.attr.endswith("__"):
                    continue                          # __file__, __name__…: los tiene todo módulo
                if destino != ruta and not _es_submodulo(destino, n.attr, rutas):
                    pedidos.setdefault(destino, set()).add(n.attr)
    return pedidos


# ── Lo mismo en una web: clases e ids que casen entre HTML, CSS y JS ────────
# Juicio del 7 oct 2026: en la web de la peluquería, 73 de las 103 clases del HTML
# no tenían estilo en el CSS (que definía otras 65 que nadie usaba): menú apilado,
# texto blanco sobre fondo claro, unas tijeras a pantalla completa.
_EXT_WEB_HTML = (".html", ".htm")
_EXT_WEB_CSS = (".css", ".scss")
_EXT_WEB_JS = (".js", ".mjs", ".jsx", ".ts", ".tsx")
_CLASS_ATTR_RE = re.compile(r"""\bclass(?:Name)?\s*=\s*["']([^"'{}$]+)["']""")
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*["']([A-Za-z_][\w-]*)["']""")
_CLASSLIST_RE = re.compile(r"""classList\.(?:add|toggle|replace)\(\s*["']([\w-]+)["']""")
_JS_ID_RE = re.compile(r"""(?:getElementById\(\s*["']|querySelector(?:All)?\(\s*["']#)([A-Za-z_][\w-]*)""")
_CSS_SELECTOR_RE = re.compile(r"([^{}]+)\{")
_CSS_CLASE_RE = re.compile(r"\.([A-Za-z_][\w-]*)")
_CSS_ID_RE = re.compile(r"#([A-Za-z_][\w-]*)")
_FRAMEWORK_CSS_RE = re.compile(r"""<(?:link|script)[^>]+(?:tailwind|bootstrap|bulma|foundation|"""
                               r"""materialize|picocss|@pico|uikit|semantic-ui)""", re.I)
MAX_NOMBRES = 150
UMBRAL_SIN_ESTILO = 0.3


def _clases_html(contenido: str) -> Set[str]:
    return {c for grupo in _CLASS_ATTR_RE.findall(contenido or "") for c in grupo.split()}


def _ids_html(contenido: str) -> Set[str]:
    return set(_ID_ATTR_RE.findall(contenido or ""))


def _selectores_css(contenido: str) -> Tuple[Set[str], Set[str]]:
    sin_comentarios = re.sub(r"/\*.*?\*/", " ", contenido or "", flags=re.S)
    clases, ids = set(), set()
    for selector in _CSS_SELECTOR_RE.findall(sin_comentarios):
        if selector.strip().startswith("@"):
            continue
        clases.update(_CSS_CLASE_RE.findall(selector))
        ids.update(_CSS_ID_RE.findall(selector))
    return clases, ids


def _clases_de_js(contenido: str) -> Set[str]:
    """Las que el JS pone en la página: en el HTML que pinta y con classList."""
    return _clases_html(contenido) | set(_CLASSLIST_RE.findall(contenido or ""))


def _lista(nombres: Iterable[str]) -> str:
    return ", ".join(sorted(nombres)[:MAX_NOMBRES])


def _interfaz_web(ruta: str, contenido: str) -> str:
    ext = PurePosixPath(ruta).suffix.lower()
    if ext in _EXT_WEB_HTML:
        clases, ids = _clases_html(contenido), _ids_html(contenido)
        return "\n".join(x for x in (f"clases que usa: {_lista(clases)}" if clases else "",
                                     f"ids: {_lista(ids)}" if ids else "") if x)
    if ext in _EXT_WEB_CSS:
        clases, ids = _selectores_css(contenido)
        return "\n".join(x for x in (f"clases con estilo: {_lista(clases)}" if clases else "",
                                     f"ids con estilo: {_lista(ids)}" if ids else "") if x)
    if ext in _EXT_WEB_JS:
        busca = set(_JS_ID_RE.findall(contenido or ""))
        pone = _clases_de_js(contenido)
        define = set(_JS_TOPE_RE.findall(_js_sin_textos(contenido))) | set(
            _JS_GLOBAL_ASIG_RE.findall(_js_sin_textos(contenido)))
        exporta, _ = _js_exporta(contenido)
        return "\n".join(x for x in (
            f"define (nombres exactos): {_lista(define)}" if define else "",
            f"exporta: {_lista(exporta)}" if exporta else "",
            f"ids que busca en la página: {_lista(busca)}" if busca else "",
            f"clases que pone: {_lista(pone)}" if pone else "") if x)
    return ""


# Y las variables entre ficheros JS (juicio de Gemini, 11 oct 2026: datos.js definía
# DATOS y app.js, citas.js y horario.js buscaban DATOS_NEGOCIO o Datos: servicios,
# precios y horario vacíos en pantalla).
_JS_INCORPORADOS = frozenset((
    "Math JSON Date Object Array Number String Boolean Promise Intl Set Map WeakMap WeakSet "
    "Error TypeError RangeError SyntaxError URL URLSearchParams FormData Event CustomEvent "
    "KeyboardEvent MouseEvent HTMLElement HTMLInputElement HTMLFormElement Node NodeList Element "
    "Document Window Symbol RegExp Reflect Proxy BigInt ArrayBuffer Uint8Array DataView Blob "
    "File FileReader Image Audio Notification WebSocket Worker Response Request Headers "
    "TextEncoder TextDecoder DOMParser XMLHttpRequest IntersectionObserver MutationObserver "
    "ResizeObserver AbortController Infinity NaN React ReactDOM Storage Location History "
    "Navigator Screen Performance Crypto CSS Selection Range Function").split())
_JS_DECL_RE = re.compile(
    r"\b(?:const|let|var|function\*?|class)\s+([A-Za-z_$][\w$]*)")
_JS_DESTRUCT_RE = re.compile(r"\b(?:const|let|var)\s*[{\[]([^}\]=]*)[}\]]")
_JS_TOPE_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:const|let|var|function\*?|class)\s+"
    r"([A-Za-z_$][\w$]*)", re.M)
_JS_GLOBAL_ASIG_RE = re.compile(
    r"\b(?:window|globalThis|self|global|root)\.([A-Za-z_$][\w$]*)\s*=(?!=)")
# «window.DATOS_NEGOCIO || {}»: lo que se lee de window. Sólo con mayúscula: lo del
# navegador (window.location, window.innerWidth…) va en minúscula.
_JS_GLOBAL_LEE_RE = re.compile(r"\b(?:window|globalThis|self)\.([A-Z][\w$]*)\b(?!\s*=(?!=))")
_JS_USO_RE = re.compile(r"(?<![\w$.])([A-Z][A-Za-z0-9_$]*)\s*(?=[.\[(])")
_JS_IMPORT_RE = re.compile(
    r"""\bimport\s+(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*["'](\.{1,2}/[^"']+)["']""")
_JS_EXPORT_RE = re.compile(
    r"\bexport\s+(?:async\s+)?(?:const|let|var|function\*?|class)\s+([A-Za-z_$][\w$]*)")
_JS_EXPORT_LISTA_RE = re.compile(r"\bexport\s*\{([^}]*)\}")
_JS_TANTEO_RE = re.compile(
    r"(?:window|globalThis|self)\.[A-Z][\w$]*(?:\s*\|\|\s*(?:window|globalThis|self)\.[A-Z][\w$]*)+")
_SCRIPT_EXTERNO_RE = re.compile(r"""<script[^>]+src\s*=\s*["']https?://""", re.I)


def _js_sin_textos(codigo: str) -> str:
    """El código sin comentarios ni cadenas: «Horario (martes…)» en un texto no es una
    variable."""
    codigo = re.sub(r"/\*.*?\*/", " ", codigo or "", flags=re.S)
    codigo = re.sub(r"(?<![:\\])//[^\n]*", " ", codigo)
    return re.sub(r"`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", "''", codigo)


def _js_definidos(codigo: str) -> Set[str]:
    limpio = _js_sin_textos(codigo)
    nombres = set(_JS_DECL_RE.findall(limpio)) | set(_JS_GLOBAL_ASIG_RE.findall(limpio))
    for grupo in _JS_DESTRUCT_RE.findall(limpio):
        nombres.update(re.findall(r"([A-Za-z_$][\w$]*)\s*(?:,|$|=)", grupo))
    for grupo in re.findall(r"\bimport\s+([^'\"]*?)\s+from\b", limpio):
        nombres.update(re.findall(r"([A-Za-z_$][\w$]*)\s*(?:,|\}|$)", grupo))
    return nombres


def _js_exporta(codigo: str) -> Tuple[Set[str], bool]:
    limpio = _js_sin_textos(codigo)
    nombres = set(_JS_EXPORT_RE.findall(limpio))
    for lista in _JS_EXPORT_LISTA_RE.findall(limpio):
        for trozo in lista.split(","):
            partes = trozo.split()
            if partes:
                nombres.add(partes[-1])
    return nombres, bool(re.search(r"\bexport\s+default\b", limpio))


def _js_scripts(html: str) -> str:
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html or "",
                                flags=re.S | re.I))


def _js_que_no_casan(jss: Dict[str, str], htmls: Dict[str, str]) -> List[Tuple[str, str]]:
    problemas = []
    rutas = set(jss)
    definidos = set().union(*(_js_definidos(c) for c in jss.values()),
                            *(_js_definidos(_js_scripts(h)) for h in htmls.values()))
    con_externos = any(_SCRIPT_EXTERNO_RE.search(h) for h in htmls.values())
    for ruta, codigo in jss.items():
        # Lo que se importa de otro fichero del proyecto tiene que exportarse.
        for defecto, lista, origen in _JS_IMPORT_RE.findall(codigo):
            base = PurePosixPath(ruta).parent
            destino = (base / origen).as_posix()
            destino = re.sub(r"(^|/)\./", r"\1", destino)
            while "/../" in destino or destino.startswith("../"):
                nuevo = re.sub(r"[^/]+/\.\./", "", destino, count=1)
                if nuevo == destino:
                    break
                destino = nuevo
            if destino not in rutas and destino + ".js" in rutas:
                destino += ".js"
            if destino not in rutas:
                continue
            exporta, por_defecto = _js_exporta(jss[destino])
            pedidos = [x.split(" as ")[0].strip() for x in lista.split(",") if x.strip()]
            faltan = [x for x in pedidos if x and x not in exporta]
            if defecto and not por_defecto:
                faltan.append(f"{defecto} (por defecto)")
            if faltan:
                problemas.append((ruta, f"importa {', '.join(faltan)} de {destino}, que no lo "
                                        f"exporta (exporta: {_lista(exporta) or 'nada'})"))
        # Las variables globales de otros ficheros: «DATOS_NEGOCIO» donde sólo hay «DATOS».
        limpio = _js_sin_textos(codigo)
        usados = set(_JS_USO_RE.findall(limpio))
        leidos = set(_JS_GLOBAL_LEE_RE.findall(limpio))
        # Tantear varios nombres en una misma expresión («window.Almacenamiento ||
        # window.Storage») vale si alguno existe; leer uno solo que no existe
        # («window.DATOS_NEGOCIO || {}») no.
        en_cadena = set()
        for cadena in _JS_TANTEO_RE.findall(limpio):
            nombres = set(re.findall(r"\.([A-Z][\w$]*)", cadena))
            en_cadena |= nombres
            if not (nombres & definidos):
                usados |= nombres
        usados |= leidos - en_cadena
        usados -= set(re.findall(r"\btypeof\s+([A-Za-z_$][\w$]*)", limpio))
        sueltos = {n for n in usados - definidos - _JS_INCORPORADOS
                   if not con_externos or (n.isupper() and len(n) > 2)}
        if sueltos:
            problemas.append((ruta, f"usa {_lista(sueltos)}, que no define ningún archivo "
                                    f"(los otros definen: {_lista(definidos)[:300]})"))
    return problemas


def nombres_que_no_casan(escritos: Dict[str, str]) -> List[Tuple[str, str]]:
    """(fichero a rehacer, por qué): el CSS que no da estilo a buena parte de las
    clases del HTML, y el JS que busca ids que la página no tiene."""
    def de(exts):
        return {r: c for r, c in escritos.items() if PurePosixPath(r).suffix.lower() in exts}
    htmls, csss, jss = de(_EXT_WEB_HTML), de(_EXT_WEB_CSS), de(_EXT_WEB_JS)
    if not htmls:
        return []
    usadas = set().union(*(_clases_html(c) for c in htmls.values()),
                         *(_clases_de_js(c) for c in jss.values()))
    ids_pagina = set().union(*(_ids_html(c) for c in htmls.values()),
                             *(_ids_html(c) for c in jss.values()))
    problemas = []
    # Con Tailwind o Bootstrap las clases («flex», «row») vienen de fuera: no se miran.
    con_framework = any(_FRAMEWORK_CSS_RE.search(c) for c in htmls.values())
    if csss and usadas and not con_framework:
        con_estilo = set().union(*(_selectores_css(c)[0] for c in csss.values()))
        sin = usadas - con_estilo
        if len(sin) >= 5 and len(sin) / len(usadas) > UMBRAL_SIN_ESTILO:
            principal = max(csss, key=lambda r: len(csss[r]))
            problemas.append((principal, f"no da estilo a {len(sin)} de las {len(usadas)} "
                                         f"clases que usa la página ({_lista(sin)[:300]})"))
    for r, c in jss.items():
        faltan = set(_JS_ID_RE.findall(c)) - ids_pagina
        if faltan:
            problemas.append((r, f"busca ids que la página no tiene: {_lista(faltan)}"))
    return problemas + _js_que_no_casan(jss, htmls)


def interfaz(ruta: str, contenido: str) -> str:
    """Lo que un fichero ofrece a los demás: en un .py, las firmas de sus funciones
    y clases públicas, sin el cuerpo; en una web, las clases y los ids."""
    import ast
    web = _interfaz_web(ruta, contenido)
    if web:
        return web
    arbol = _arbol(contenido) if ruta.endswith(".py") else None
    if arbol is None:
        return ""

    def doc(nodo, sangria):
        # Qué devuelve y qué espera: con sólo la firma, cuentas.py le pasaba un dict a
        # quien devolvía una lista de tuplas (examen de nivel, 11 oct 2026).
        d = ast.get_docstring(nodo) or ""
        d = " ".join(d.split())
        return f"\n{sangria}    # {d[:MAX_DOC]}{'…' if len(d) > MAX_DOC else ''}" if d else ""

    def firma(f, sangria=""):
        flecha = f" -> {ast.unparse(f.returns)}" if f.returns is not None else ""
        pre = "async def" if isinstance(f, ast.AsyncFunctionDef) else "def"
        return f"{sangria}{pre} {f.name}({ast.unparse(f.args)}){flecha}" + doc(f, sangria)

    lineas = []
    for nodo in _nivel_de_modulo(arbol.body):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not nodo.name.startswith("_"):
                lineas.append(firma(nodo))
        elif isinstance(nodo, ast.ClassDef) and not nodo.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in nodo.bases)
            lineas.append(f"class {nodo.name}" + (f"({bases})" if bases else "") + ":"
                          + doc(nodo, ""))
            for x in nodo.body:
                if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name):
                    lineas.append(f"    {x.target.id}: {ast.unparse(x.annotation)}")
                elif (isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and (not x.name.startswith("_") or x.name == "__init__")):
                    lineas.append(firma(x, "    "))
        elif isinstance(nodo, ast.Assign):
            for n in (n for o in nodo.targets for n in _nombres_asignados(o)):
                if n.isupper() and not n.startswith("_"):
                    lineas.append(f"{n} = …")
    return "\n".join(lineas)


# ── Las pruebas, con el código delante ───────────────────────────────────────
# Examen de nivel, 11 oct 2026 (cuentas-viaje): las pruebas sólo veían las firmas
# («def parsear_importe(texto)») y se imaginaron que devolvía céntimos (2550) cuando
# el código daba Decimal("25.50"): 23 de 64 fallaban. Ahora van detrás del código
# en el plan y ven entero el código que prueban.
MAX_CODIGO_PRUEBAS = 24000
_FAMILIAS = ((".py",), (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"))


def _familia(ruta: str) -> Optional[Tuple[str, ...]]:
    ext = PurePosixPath(ruta).suffix.lower()
    return next((f for f in _FAMILIAS if ext in f), None)


def es_prueba(ruta: str) -> bool:
    """test_x.py, x_test.py, conftest.py, x.test.js, x.spec.ts…"""
    nombre = PurePosixPath(ruta).name.lower()
    return _familia(nombre) is not None and (
        nombre.startswith(("test_", "conftest"))
        or re.search(r"(?:_test\.py|\.(?:test|spec)\.[cm]?[jt]sx?)$", nombre) is not None)


def pruebas_al_final(plan: list) -> list:
    """El plan con las pruebas detrás del resto, para que vean el código ya escrito."""
    return ([a for a in plan if not es_prueba(str(a.get("ruta", "")))]
            + [a for a in plan if es_prueba(str(a.get("ruta", "")))])


_PRINCIPALES = {"main.py", "__main__.py", "app.py", "cli.py", "run.py", "manage.py"}


def _turno_py(ruta: str, principales: Set[str]) -> int:
    p = PurePosixPath(ruta)
    if p.name in principales:
        return 3                                  # el programa que se ejecuta, el último
    if len(p.parts) > 1 and p.name != "__init__.py":
        return 0                                  # los módulos de un paquete
    return 1 if p.name == "__init__.py" else 2    # quien los reexporta y los sueltos


def orden_de_escritura(plan: list, peticion: str = "") -> list:
    """Las pruebas al final y, en Python, los módulos antes de quien los usa: el
    `cuentas.py` del examen (11 oct 2026) se escribió antes que los módulos que
    usaba y sin saber qué devolvían (dos veces: en gastos/*.py y a su lado). El
    programa principal (main.py, app.py… o el que nombra la petición) va el último
    de los .py. Lo que no es Python se queda en su sitio."""
    plan = pruebas_al_final(plan)
    principales = _PRINCIPALES | {m.lower() for m in re.findall(r"[\w-]+\.py\b", peticion or "")}
    huecos = [i for i, a in enumerate(plan) if str(a.get("ruta", "")).endswith(".py")
              and not es_prueba(str(a.get("ruta", "")))]
    ordenados = sorted((plan[i] for i in huecos),
                       key=lambda a: _turno_py(str(a.get("ruta", "")), principales))
    plan = list(plan)
    for i, a in zip(huecos, ordenados):
        plan[i] = a
    return plan


def contexto(ruta: str, escritos: Dict[str, str], planeadas: Iterable[str] = (),
             codigo_entero: bool = False) -> str:
    """Para el prompt de `ruta`: lo que ya ofrecen los ficheros escritos y lo que
    esos ficheros esperan de éste. Vacío si no hay nada que decir. Si `ruta` es de
    pruebas (o `codigo_entero`: al cambiar un proyecto que ya existe), el código de su mismo
    lenguaje va entero, no sólo las firmas."""
    rutas = set(escritos) | set(planeadas) | {ruta}
    familia = _familia(ruta) if es_prueba(ruta) or codigo_entero else None
    enteros, total_enteros = [], 0
    bloques, total = [], 0
    for r, c in escritos.items():
        if r == ruta or es_prueba(r):
            continue                              # las pruebas no ofrecen nada a nadie
        if familia and _familia(r) == familia and total_enteros + len(c) <= MAX_CODIGO_PRUEBAS:
            enteros.append(f"--- {r} ---\n{c.rstrip()}")
            total_enteros += len(c)
            continue
        i = interfaz(r, c)
        if i and total + len(i) <= MAX_INTERFAZ:
            bloques.append(f"# {r}\n{i}")
            total += len(i)
    esperan = [f"- {r} importa de aquí: {', '.join(sorted(n))}"
               for r, c in escritos.items() if r != ruta
               for d, n in usos(r, c, rutas).items() if d == ruta]
    texto = ""
    if enteros and not es_prueba(ruta):
        texto += ("El resto del código del proyecto, tal como está ahora. Usa sus nombres, "
                  "tipos y formas de llamar EXACTOS:\n" + "\n\n".join(enteros))
    elif enteros:
        texto += ("El código que vas a probar, YA escrito y entero. Usa sus nombres, tipos y "
                  "formas de llamar EXACTOS (lo que devuelve cada función, qué error lanza, qué "
                  "imprime y con qué código sale); lo que compruebes, que sea lo que pide la "
                  "petición:\n" + "\n\n".join(enteros))
    if bloques:
        texto += ("\n\nLo que YA está escrito en otros archivos del proyecto. Usa estos nombres "
                  "y firmas EXACTOS; no inventes otros ni cambies sus argumentos:\n"
                  + "\n\n".join(bloques))
    if esperan:
        texto += ("\n\nArchivos ya escritos importan de ESTE archivo, así que tiene que "
                  "definir exactamente esos nombres:\n" + "\n".join(esperan))
    return texto.strip()


def imports_rotos(escritos: Dict[str, str]) -> List[Tuple[str, str, List[str]]]:
    """(importador, fichero, nombres que pide y el fichero no define)."""
    rutas = set(escritos)
    rotos = []
    for ruta, contenido in escritos.items():
        for destino, nombres in usos(ruta, contenido, rutas).items():
            definidos = _definidos(escritos[destino])
            if definidos is None:
                continue
            faltan = sorted(n for n in nombres if n not in definidos)
            if faltan:
                rotos.append((ruta, destino, faltan))
    return rotos


def prompt_continuar(ruta: str, contenido: str) -> str:
    return (f"El archivo {ruta} se cortó antes de terminar. Lo último que escribiste fue:\n"
            f"<<<\n{contenido[-_COLA:]}\n>>>\n"
            "Sigue EXACTAMENTE desde ahí, sin repetir nada de lo anterior, hasta terminar el "
            "archivo. Responde sólo con lo que falta.")


def _costura(texto: str) -> Tuple[int, int]:
    """Lo mal que queda un .py: (no compila, nombres que usa sin definirlos)."""
    try:
        compile(texto, "<costura>", "exec")
    except (SyntaxError, ValueError):
        return (1, 0)
    return (0, len(sin_definir(texto)))


def unir(contenido: str, siguiente: str, ruta: str = "") -> str:
    """Pega la continuación quitando lo que repita del final de lo ya escrito.

    En un .py, si no se repite nada, mira si la costura va en la misma línea o en
    una nueva: lo cortado llega sin su salto de línea final (se limpia con strip) y
    «#: … otro.» + «FICHERO_POR_DEFECTO = …» quedó en un comentario (11 oct 2026)."""
    junto = _unir(contenido, siguiente)
    if (ruta.endswith(".py") and junto == contenido + siguiente and contenido.strip()
            and siguiente.strip() and not contenido.endswith("\n")
            and not siguiente.startswith("\n")):
        con_salto = contenido + "\n" + siguiente
        if _costura(con_salto) < _costura(junto):
            return con_salto
    return junto


def _unir(contenido: str, siguiente: str) -> str:
    siguiente = siguiente or ""
    # La valla de cierre del «```html» de fuera llega con la continuación. Sólo se
    # quita si deja las vallas desparejadas: en un README cortado dentro de un
    # bloque, ese ``` es el cierre del bloque.
    if (contenido + siguiente).count("```") % 2 == 1:
        siguiente = re.sub(r"\n?```\s*$", "", siguiente)
    for k in range(min(len(siguiente), _COLA), 7, -1):     # 8 o más: «});» no cuenta
        if contenido.endswith(siguiente[:k]):
            return contenido + siguiente[k:]
    # Una línea a medias que la continuación vuelve a escribir entera («<foot» →
    # «<footer>…»). Sólo si la reescribe más larga y hay algo de texto: un «}» que
    # viene detrás de otro «}» es otra llave, no una repetición.
    ultima = contenido.rsplit("\n", 1)[-1]
    primera = siguiente.lstrip().split("\n", 1)[0]
    if (len(ultima.strip()) >= 4 and primera.startswith(ultima.strip())
            and len(primera) > len(ultima.strip())):
        return contenido[:len(contenido) - len(ultima)] + siguiente.lstrip()
    return contenido + siguiente


_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#?]+)""", re.I)


def referencias_rotas(carpeta: Path) -> List[str]:
    """Lo que las páginas enlazan dentro del proyecto y no existe («hero.jpg»)."""
    rotas = []
    base = Path(carpeta)
    for pagina in base.rglob("*.htm*"):
        try:
            html = pagina.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ref in _REF_RE.findall(html):
            ref = ref.strip()
            if not ref or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", ref, re.I):
                continue                               # http:, mailto:, tel:, data:…
            destino = (pagina.parent / ref).resolve()
            try:
                destino.relative_to(base.resolve())
            except ValueError:
                continue
            if not destino.exists():
                rotas.append(f"{pagina.relative_to(base)} → {ref}")
    return sorted(set(rotas))
