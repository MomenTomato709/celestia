"""El documento que te han pasado, entero y a mano en toda la conversación.

Hasta el 6 oct 2026 al modelo sólo le llegaban los 4.000 primeros caracteres del
adjunto, y en las preguntas siguientes ni eso: con un reglamento de 200.000
contestó de memoria general («depende del municipio…») y falló 7 de 8 preguntas
del examen de nivel. Peor: el texto del adjunto pasaba por el detector de
órdenes, y una frase del reglamento («…los elementos comunes, en los términos
que fije la Junta…») se tomó por domótica y acabó creando una habilidad.

Ahora el adjunto va aparte de lo que el usuario escribe:

  `preparar(texto, hilo, canal)` separa lo que pide de lo que se adjunta, guarda
  el documento entero para ese chat y devuelve el bloque para el modelo: el
  documento si es corto; si es largo, su índice, el principio y los trozos que
  tratan de lo que pide. En los mensajes siguientes del mismo chat, si la
  pregunta va del documento, los trozos que la tratan y las palabras de la
  pregunta que el documento no nombra (la piscina que no existe: decir que no
  aparece, no inventar una cuota).

Los trozos se eligen con BM25 sobre los párrafos (sin modelo, sin red, sin
gastar nada): las palabras que salen en todas partes pesan poco y las raras
mucho, que es lo que hace falta en un texto lleno de paja.

Seguridad (revisión, 6 oct 2026): la marca del adjunto va en el texto del
mensaje y cualquiera puede escribirla a mano. Por eso el archivo sólo se relee
si se subió de verdad por ESE canal hace menos de una hora (`registrar_subida`,
que llaman `/subir` y el puente de WhatsApp), es un fichero normal dentro de
`recibidos/` (nada de enlaces simbólicos) y pasa las rutas vetadas de siempre.
Si no, vale sólo lo que traía el propio mensaje, y se dice que es un trozo.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .paths import RECIBIDOS_DIR

logger = logging.getLogger(__name__)

# La marca que ponen el chat web (chat.html), el puente de WhatsApp
# (`bp_chat.anexar_archivo_recibido`) y el examen al adjuntar un archivo.
MARCA_RE = re.compile(r"\n*\[CONTENIDO DEL ARCHIVO «(.{1,300}?)» QUE ACABAS DE ABRIR([^\]\n]*)\]\n?")
# Lo que el modelo reconoce como anexo del sistema (orchestrator._BLOQUE_ANEXADO_RE).
CABECERA = "[DOCUMENTO"

ENTERO_HASTA = 12_000        # hasta aquí se le pasa entero; más, por trozos
MAX_LEIDO = 1_000_000        # unas 500 páginas: más, ni cabe en el móvil ni se lee
MAX_PAGINAS_PDF = 500
PRINCIPIO = 1_500            # del documento largo, el principio (título, de qué va)
MAX_INDICE = 1_500
TROZOS = 6
MAX_TROZO = 700
MAX_HILOS = 8                # chats con documentos abiertos a la vez
MAX_DOCS_POR_HILO = 3        # el reglamento y, por ejemplo, un informe que entregó ella
VIGENCIA_SUBIDA_S = 3600     # una subida vale para abrirla durante una hora
VENTANA_TURNOS = 6           # tras tantos mensajes sin hablar de él, se aparca

_ARTICULO_RE = re.compile(
    r"^\s*(?:(?:art[íi]culo|art\.)\s*(?P<art>\d+\w*)|cl[áa]usula\s+(?P<cla>\d+\w*|[a-záéíóú]+)|"
    r"(?P<ord>primera|segunda|tercera|cuarta|quinta|sexta|s[ée]ptima|octava|novena|"
    r"d[ée]cima)\s*[.\-–:])", re.I)
_SECCION_RE = re.compile(r"^\s*(?:cap[íi]tulo|t[íi]tulo|secci[óo]n|anexo|parte)\b.*|^\s*#{1,6}\s.*",
                         re.I)
_MD_RE = re.compile(r"^#{1,6}\s")
_PALABRA_RE = re.compile(r"[a-zñ0-9]+")
# La pregunta habla del documento: lo nombra. Sólo sustantivos: con «dice», «sale» o
# «pone», «¿qué sale hoy en la tele?» se llevaba el reglamento.
_HABLA_DEL_DOC_RE = re.compile(
    r"\b(?:documento|archivo|fichero|reglamento|contrato|pdf|estatutos?|normas?|normativa|"
    r"bases|pliego|manual|informe|acta|art[íi]culos?|cap[íi]tulos?|apartados?|cl[áa]usulas?|"
    r"p[áa]ginas?|anexos?)\b", re.I)
# «¿Y el día 3?», «¿y cuándo es la junta?»: sigue con lo que se estaba hablando.
_SIGUE_RE = re.compile(
    r"^\s*¿?\s*y\s+(?:el|la|los|las|lo|en|qu[eé]|cu[aá]nt[oa]s?|cu[aá]ndo|c[oó]mo|d[oó]nde|"
    r"qui[eé]n(?:es)?|por\s+qu[eé]|a\s+qu[eé]|de\s+qu[eé])\b", re.I)
# Lo de actualidad no va de un documento, aunque esté abierto.
_ACTUALIDAD_RE = re.compile(
    r"\b(?:hoy|ma[ñn]ana|ayer|ahora|esta\s+(?:semana|noche|tarde)|este\s+(?:finde|fin\s+de\s+"
    r"semana|mes|a[ñn]o)|noticias?|tele|televisi[oó]n|partido|resultados?|bolsa|el\s+tiempo)\b",
    re.I)
# Lo escrito con el adjunto que no pide nada («?», «👀», «ok»): que lo mire.
_NADA_PEDIDO_RE = re.compile(r"^\W*(?:ok|vale|mira|toma|ah[ií]\s+va|esto|eso|ten)?\W*$", re.I)

_VACIAS = set("""
a al algo algun alguna alguno algunos ante antes aqui asi aun cada como con contra cual
cuales cualquier cuando cuanto cuanta cuantos cuantas de del desde donde dos el ella ellas
ello ellos en entre era eran es esa esas ese eso esos esta estas este esto estos estar esta
fue fueron ha han has hay hasta hace hacer la las le les lo los mas me mi mis mucho muy nada
ni no nos nosotros o otra otras otro otros para pero poco por porque puede pueden puedo que
quien quienes se sea segun ser si sin sobre solo son su sus tal tambien tan tanto te tener
tengo tiene tienen todo todos toda todas tu tus un una unas uno unos usted y ya yo vez veces
partir hora pasa paso dime decir digas sabes saber quiero quieres necesito oye bueno vale
pues entonces ahora luego mio mia tuyo tuya nuestro nuestra casa cosa cosas vamos voy va
dia dias cuesta cuestan pone dice dicen sale salen viene vienen aparece aparecen habla
menciona nombra leetelo lee leer bien pregunta preguntas concretas concreto tenemos
""".split())
# Un trozo que puntúa menos que esto respecto al mejor es paja que comparte una
# palabra suelta con la pregunta: no se pasa.
CORTE_RELATIVO = 0.35
LINEA_LARGA = 120             # una línea así ya es un párrafo; las cortas, texto partido


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower())
                   if unicodedata.category(c) != "Mn")


def _raiz(palabra: str) -> str:
    """Una raíz tosca: «revisan» y «revisará», «bombonas» y «bombona», juntas."""
    if len(palabra) > 5 and palabra.endswith("es"):
        palabra = palabra[:-2]
    elif len(palabra) > 4 and palabra.endswith("s"):
        palabra = palabra[:-1]
    return palabra[:6]


def _palabras(texto: str) -> List[str]:
    return [p for p in _PALABRA_RE.findall(_sin_tildes(texto))
            if (len(p) > 2 or p.isdigit()) and p not in _VACIAS]


def _terminos(texto: str) -> List[str]:
    return [_raiz(p) for p in _palabras(texto)]


def nombre_limpio(nombre: str) -> str:
    """Un nombre de archivo que no pueda salirse de su carpeta (el mismo criterio
    con el que `/subir` guarda: sin barras, «..» ni dos puntos, 80 caracteres)."""
    base = Path(str(nombre or "archivo")).name
    base = re.sub(r"[^\w.\- ]", "_", base, flags=re.UNICODE).strip(". ")
    return (base or "archivo")[:80]


@dataclass
class _Trozo:
    texto: str
    donde: str                 # «Artículo 24 (CAPÍTULO 3. CONVIVENCIA Y RUIDOS)»
    terminos: Counter = field(default_factory=Counter)


@dataclass
class Documento:
    nombre: str
    texto: str
    trozos: List[_Trozo]
    df: Counter
    media: float
    indice: str
    vocabulario: Set[str]
    en_espanol: bool
    parcial: bool = False      # sólo lo que traía el mensaje: el archivo no estaba
    ultimo_uso: int = 0        # el turno del chat en que se usó por última vez

    @property
    def largo(self) -> bool:
        return len(self.texto) > ENTERO_HASTA

    @property
    def como_se_tiene(self) -> str:
        return ("SÓLO EL PRINCIPIO (el resto no lo tienes: si algo no está aquí, di que no "
                "lo puedes ver, no que no aparezca)" if self.parcial else "entero")


# ── Trocear y buscar ─────────────────────────────────────────────────────

def _por_frases(texto: str) -> List[str]:
    """Trozos de hasta ~700 caracteres: por frases y, si una frase no se acaba (una
    tabla aplanada), por palabras. Nunca se tira texto: lo que no se indexa no existe."""
    if len(texto) <= MAX_TROZO:
        return [texto]
    piezas, actual = [], ""
    frases = re.split(r"(?<=[.;:!?])\s+", texto)
    for frase in frases:
        partes = [frase]
        if len(frase) > MAX_TROZO:
            palabras, partes, ventana = frase.split(), [], ""
            for p in palabras:
                if ventana and len(ventana) + len(p) + 1 > MAX_TROZO:
                    partes.append(ventana)
                    ventana = ""
                ventana = f"{ventana} {p}".strip()
            if ventana:
                partes.append(ventana)
        for parte in partes:
            if actual and len(actual) + len(parte) > MAX_TROZO:
                piezas.append(actual)
                actual = ""
            actual = f"{actual} {parte}".strip()
    if actual:
        piezas.append(actual)
    return piezas


def _etiqueta(m: "re.Match") -> str:
    if m.group("art"):
        return f"Artículo {m.group('art')}"
    if m.group("cla"):
        return f"Cláusula {m.group('cla').lower()}"
    return f"Cláusula {m.group('ord').lower()}"


def _partir(texto: str) -> List[_Trozo]:
    """Párrafos con su sitio (artículo, cláusula, capítulo), de hasta ~700 caracteres.

    Una línea larga es un párrafo por sí sola (el reglamento: cada línea, una
    norma). Las cortas seguidas son un párrafo partido al ancho de la página
    (un PDF) y se juntan hasta la siguiente línea en blanco o el siguiente título.
    """
    trozos: List[_Trozo] = []
    articulo = seccion = ""
    seccion_md = False                  # «## Día 3» (un informe suyo), no «CAPÍTULO 4»
    junto: List[str] = []

    def soltar():
        if not junto:
            return
        donde = " ".join(p for p in (articulo, f"({seccion})" if seccion else "") if p)
        # El título cuenta: «¿y el día 3?» busca lo que va bajo «## Día 3».
        de_seccion = Counter(_terminos(seccion)) if seccion_md else Counter()
        for pieza in _por_frases(" ".join(junto)):
            trozos.append(_Trozo(pieza, donde, Counter(_terminos(pieza)) + de_seccion))
        junto.clear()

    for linea in texto.splitlines():
        limpia = linea.strip()
        if not limpia:
            soltar()
            continue
        m = _ARTICULO_RE.match(limpia)
        if m or _SECCION_RE.match(limpia):
            soltar()
            if m:
                articulo = _etiqueta(m)
                if len(limpia) < 20:
                    continue            # «Artículo 24.» a secas
            else:
                seccion, articulo = limpia.lstrip("# ")[:80], ""
                seccion_md = bool(_MD_RE.match(limpia))
                continue                # el título va en el índice y en el «dónde»
        if len(limpia) >= LINEA_LARGA:
            soltar()
            junto.append(limpia)
            soltar()
        else:
            junto.append(limpia)
            if sum(len(j) for j in junto) > MAX_TROZO:
                soltar()
    soltar()
    return trozos


def _indice(texto: str) -> str:
    lineas = [l.strip() for l in texto.splitlines()
              if l.strip() and _SECCION_RE.match(l.strip())]
    articulos = sum(1 for l in texto.splitlines()
                    if _ARTICULO_RE.match(l) and _ARTICULO_RE.match(l).group("art"))
    cabeza = "\n".join(lineas)[:MAX_INDICE]
    if articulos:
        cabeza = (cabeza + "\n" if cabeza else "") + f"({articulos} artículos en total)"
    return cabeza


def _parece_espanol(texto: str) -> bool:
    muestra = _sin_tildes(texto[:20_000])
    palabras = _PALABRA_RE.findall(muestra)
    if len(palabras) < 30:
        return True
    marcas = sum(1 for p in palabras if p in {"el", "la", "los", "las", "que", "de", "del",
                                              "y", "en", "por", "para", "con", "una", "es"})
    return marcas / len(palabras) > 0.08


def analizar(nombre: str, texto: str, parcial: bool = False) -> Documento:
    trozos = _partir(texto)
    df: Counter = Counter()
    for t in trozos:
        df.update(set(t.terminos))
    media = (sum(sum(t.terminos.values()) for t in trozos) / len(trozos)) if trozos else 1.0
    # El vocabulario de verdad (con los títulos de capítulo): para no decir que
    # «jardín» no sale cuando es el título del capítulo 10.
    vocabulario = set(_terminos(texto))
    return Documento(nombre, texto, trozos, df, media or 1.0, _indice(texto), vocabulario,
                     _parece_espanol(texto), parcial)


def _idf(doc: Documento, termino: str) -> float:
    n = len(doc.trozos)
    d = doc.df.get(termino, 0)
    return math.log(1 + (n - d + 0.5) / (d + 0.5))


def buscar(doc: Documento, pregunta: str, n: int = TROZOS) -> Tuple[List[_Trozo], List[str]]:
    """Los trozos que mejor tratan de la pregunta, en el orden del documento, y las
    palabras de la pregunta que el documento no nombra en ningún sitio."""
    palabras: Dict[str, str] = {}
    for p in _palabras(pregunta):
        palabras.setdefault(_raiz(p), p)
    if not palabras or not doc.trozos:
        return [], []
    # En otro idioma, que no salga «alquiler» no dice nada: no se avisa.
    ausentes = ([p for r, p in palabras.items() if r not in doc.vocabulario and not p.isdigit()]
                if doc.en_espanol else [])
    k1, b = 1.4, 0.6
    puntos = []
    for i, t in enumerate(doc.trozos):
        largo = sum(t.terminos.values()) or 1
        s = 0.0
        for r in palabras:
            f = t.terminos.get(r, 0)
            if f:
                s += _idf(doc, r) * f * (k1 + 1) / (f + k1 * (1 - b + b * largo / doc.media))
        if s > 0:
            puntos.append((s, i))
    puntos.sort(reverse=True)
    tope = puntos[0][0] if puntos else 0
    elegidos = sorted(i for s, i in puntos[:n] if s >= tope * CORTE_RELATIVO)
    return [doc.trozos[i] for i in elegidos], ausentes


def _bloque_trozos(doc: Documento, pregunta: str, con_ausentes: bool = True) -> str:
    trozos, ausentes = buscar(doc, pregunta)
    partes = [f"— {t.donde}: {t.texto}" if t.donde else f"— {t.texto}" for t in trozos]
    if con_ausentes and ausentes:
        # Sólo un aviso: «horario» no sale y el artículo de las obras sí da las horas.
        # Con la piscina, en cambio, es justo lo que hay que decir.
        partes.append("Palabras de la pregunta que no salen en ningún sitio del documento: "
                      f"{', '.join(ausentes)}. Si lo que pregunta no está en estos trozos, "
                      "di que el documento no lo menciona; no lo supongas ni des una cifra.")
    return "\n".join(partes)


def _rara(doc: Documento, pregunta: str) -> bool:
    n = len(doc.trozos) or 1
    return any(0 < doc.df.get(r, 0) / n < 0.05 for r in set(_terminos(pregunta)))


# ── Lo que hay abierto en cada chat ──────────────────────────────────────

_abiertos: "OrderedDict[str, OrderedDict[str, Documento]]" = OrderedDict()
_turnos: Dict[str, int] = {}
_subidas: Dict[Tuple[str, str], Tuple[Path, float]] = {}
_lock = threading.Lock()


def _guardar(hilo: str, doc: Documento) -> None:
    hilo = hilo or ""
    with _lock:
        docs = _abiertos.setdefault(hilo, OrderedDict())
        doc.ultimo_uso = _turnos.get(hilo, 0)
        docs[doc.nombre] = doc
        docs.move_to_end(doc.nombre)
        while len(docs) > MAX_DOCS_POR_HILO:
            docs.popitem(last=False)
        _abiertos.move_to_end(hilo)
        while len(_abiertos) > MAX_HILOS:
            viejo, _ = _abiertos.popitem(last=False)
            _turnos.pop(viejo, None)


def abierto(hilo: str) -> Optional[Documento]:
    """El último documento abierto en ese chat."""
    with _lock:
        docs = _abiertos.get(hilo or "")
        return next(reversed(docs.values())) if docs else None


def cerrar(hilo: str) -> None:
    with _lock:
        _abiertos.pop(hilo or "", None)
        _turnos.pop(hilo or "", None)


def abrir_texto(hilo: str, nombre: str, texto: str) -> None:
    """Un texto de la propia conversación (el informe que acaba de entregar) como
    documento abierto del chat, junto a los que hubiera."""
    if texto and texto.strip():
        _guardar(hilo, analizar(nombre, texto.strip()[:MAX_LEIDO]))


def registrar_subida(ruta: Path, canal: str = "", nombre: str = "") -> None:
    """Un archivo que acaba de llegar de verdad (por `/subir` o el puente de
    WhatsApp): sólo estos se releen enteros, y sólo desde su canal. `nombre` es el
    que pondrá la marca del mensaje (el chat guarda `<segundos>_<nombre>`)."""
    ahora = time.time()
    with _lock:
        for clave in [k for k, (_, ts) in _subidas.items() if ahora - ts > VIGENCIA_SUBIDA_S]:
            _subidas.pop(clave, None)
        _subidas[((canal or "").lower(), nombre_limpio(nombre or Path(ruta).name))] = (
            Path(ruta), ahora)


def _subida(nombre: str, canal: str, carpeta: Path) -> Optional[Path]:
    with _lock:
        dato = _subidas.get(((canal or "").lower(), nombre_limpio(nombre)))
    if not dato or time.time() - dato[1] > VIGENCIA_SUBIDA_S:
        return None
    ruta = dato[0]
    try:
        if ruta.is_symlink() or not ruta.is_file():
            return None
        if ruta.resolve().parent != Path(carpeta).resolve():
            return None
    except OSError:
        return None
    try:
        from .tools import AgentTools
        if AgentTools._ruta_segura(str(ruta)) is None:
            return None
    except Exception:
        return None
    return ruta


def leer(ruta: Path) -> str:
    """Todo el texto legible del archivo (sin el tope del chat). Una página de PDF
    que no se deja leer no tira las demás."""
    ext = ruta.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            partes, total = [], 0
            for pagina in PdfReader(str(ruta)).pages[:MAX_PAGINAS_PDF]:
                try:
                    trozo = pagina.extract_text() or ""
                except Exception:
                    trozo = ""
                partes.append(trozo)
                total += len(trozo)
                if total > MAX_LEIDO:
                    break
            return "\n".join(partes)[:MAX_LEIDO]
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(str(ruta)).paragraphs)[:MAX_LEIDO]
        with open(ruta, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_LEIDO)
    except Exception as e:
        logger.info("No pude leer entero %s: %s", ruta.name, e)
        return ""


def para_herramienta(hilo: str, maximo: int = 12_000) -> str:
    """El documento recién usado en el chat, para lo que no pasa por el modelo de la
    charla: «hazme un PDF con esto», «créame la app que te describo en el adjunto»."""
    doc = abierto(hilo)
    with _lock:
        turno = _turnos.get(hilo or "", 0)
    if not doc or turno - doc.ultimo_uso > 1:
        return ""
    return f"[Documento «{doc.nombre}»]\n{doc.texto[:maximo]}"


def preparar(texto: str, hilo: str, carpeta: Optional[Path] = None,
             canal: str = "") -> Tuple[str, str]:
    """(lo que el usuario escribe, el bloque del documento para el modelo).

    Con adjunto: el documento se guarda para este chat y el bloque es el documento
    (corto) o su índice, el principio y lo que viene al caso (largo). Sin adjunto:
    si la pregunta va de algún documento abierto en el chat, sus trozos. Si no, el
    bloque va vacío.
    """
    texto = texto or ""
    hilo = hilo or ""
    with _lock:
        _turnos[hilo] = turno = _turnos.get(hilo, 0) + 1
    m = MARCA_RE.search(texto)
    if not m:
        return texto, _seguimiento(texto, hilo, turno)

    nombre = m.group(1).strip()
    pedido = texto[:m.start()].strip()
    if _NADA_PEDIDO_RE.match(pedido):
        pedido = f"Échale un vistazo a «{nombre}» y dime qué hay dentro."
    ruta = _subida(nombre, canal, carpeta or RECIBIDOS_DIR)
    entero = leer(ruta).strip() if ruta else ""
    # Sin el archivo a mano (no se subió por aquí, otro aparato, se borró) vale lo
    # que traía el mensaje, y si el chat avisó de que era el principio, se dice.
    parcial = not entero and "solo el principio" in (m.group(2) or "")
    contenido = entero or texto[m.end():].strip()
    if not contenido:
        return pedido, ""
    doc = analizar(nombre, contenido[:MAX_LEIDO], parcial=parcial)
    _guardar(hilo, doc)
    logger.info("Documento abierto en el chat %s: %s (%d caracteres, %d trozos%s)",
                hilo or "-", nombre, len(contenido), len(doc.trozos),
                ", sólo el principio" if parcial else "")
    if not doc.largo:
        return pedido, (f"{CABECERA} «{nombre}» QUE ACABAS DE ABRIR, {doc.como_se_tiene}. "
                        "Habla SOLO de lo que pone aquí; si algo no aparece, dilo en vez de "
                        f"suponerlo]\n{contenido}")
    largo = f"{len(contenido):,}".replace(",", ".")
    partes = [f"{CABECERA} «{nombre}» QUE ACABAS DE ABRIR — es largo ({largo} caracteres): "
              "lo tienes ENTERO guardado y en cada pregunta te llegarán los trozos que "
              "vengan al caso. Habla SOLO de lo que pone; si algo no aparece, dilo en vez "
              "de suponerlo. No copies frases sueltas del documento: di de qué trata con "
              "tus palabras]"]
    if doc.indice:
        partes.append(f"Índice:\n{doc.indice}")
    partes.append(f"Principio:\n{contenido[:PRINCIPIO]}")
    # Al abrirlo no se avisa de palabras ausentes: «léetelo bien» no es una pregunta.
    trozos = _bloque_trozos(doc, pedido, con_ausentes=False) if _rara(doc, pedido) else ""
    if trozos:
        partes.append(f"Lo que trata de lo que te pide:\n{trozos}")
    return pedido, "\n\n".join(partes)


_PREGUNTA_RE = re.compile(r"\?|^\s*¿|^\s*(?:c[oó]mo|qu[eé]|cu[aá]l(?:es)?|d[oó]nde|cu[aá]ndo|"
                          r"cu[aá]nt[oa]s?|qui[eé]n(?:es)?|hay|tenemos|se\s+puede|puedo)\b", re.I)


def _seguimiento(texto: str, hilo: str, turno: int) -> str:
    """Los trozos del documento abierto que tratan de la pregunta, si va de él.

    Va de él si lo nombra («el reglamento») o usa una palabra rara suya; o, si se
    usó hace poco (VENTANA_TURNOS), si comparte algo con él o sigue el hilo («¿y
    cuándo es la junta?»). Lo de actualidad («¿qué echan hoy en la tele?») nunca:
    con el documento delante no se busca en internet.
    """
    with _lock:
        docs = list(reversed(list((_abiertos.get(hilo) or {}).values())))
    # Con varios abiertos (el reglamento y un informe suyo) gana el que tiene algo que
    # decir; el aviso de «eso no sale» sólo si ninguno trae nada.
    aviso: Optional[Tuple[Documento, str]] = None
    for doc in docs:
        reciente = turno - doc.ultimo_uso <= VENTANA_TURNOS
        nombrado = bool(_HABLA_DEL_DOC_RE.search(texto)) or any(
            p in _palabras(texto) for p in _palabras(Path(doc.nombre).stem) if len(p) > 3)
        if not (nombrado or _rara(doc, texto)) and (
                not reciente or _ACTUALIDAD_RE.search(texto) or not _palabras(texto)):
            continue
        trozos, ausentes = buscar(doc, texto)
        if trozos or nombrado or _SIGUE_RE.search(texto):
            bloque = _bloque_para(doc, texto)
            if bloque:
                doc.ultimo_uso = turno
                return bloque
        elif (aviso is None and ausentes and reciente and _PREGUNTA_RE.search(texto)
              and doc.en_espanol and not doc.parcial):
            # «¿Hay piscina?»: nada que encaje, y eso es lo que hay que decir… si la
            # pregunta va del documento. Si va de otra cosa, que lo ignore.
            aviso = (doc, f"{CABECERA} «{doc.nombre}» DE ESTA CONVERSACIÓN — por si la "
                          "pregunta va de él: estas palabras no salen en ningún sitio del "
                          f"documento: {', '.join(ausentes)}. Si te pregunta por lo que pone, "
                          "di que no lo menciona (no lo supongas ni des una cifra); si habla "
                          "de otra cosa, ignora esto]")
    if aviso:
        aviso[0].ultimo_uso = turno
        return aviso[1]
    return ""


def _bloque_para(doc: Documento, texto: str) -> str:
    if not doc.largo:
        # Corto, entero: el historial sólo guarda 700 caracteres por turno.
        return (f"{CABECERA} «{doc.nombre}» DE ESTA CONVERSACIÓN, {doc.como_se_tiene}, por si "
                f"la pregunta va de él. Contesta con lo que pone; si no está, dilo]\n{doc.texto}")
    trozos = _bloque_trozos(doc, texto)
    if not trozos:
        return ""
    return (f"{CABECERA} «{doc.nombre}» DE ESTA CONVERSACIÓN — los trozos que tratan de esta "
            "pregunta, sacados del documento entero. Contesta con lo que pone y di de qué "
            "artículo o apartado sale; si no está, di que el documento no lo dice]\n" + trozos)
