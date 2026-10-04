"""Buscar la manera antes de decir que no.

Enzo (4 sep 2026): «que se le pueda pedir cualquier cosa y que aunque ella no
sepa, busque la manera de hacerlo sí o sí».

De dónde sale esto: preguntándole cuántas líneas tenía un fichero SUYO,
contestaba «¿podrías facilitarme la URL del repositorio?»; y por sus propias
conversaciones guardadas, «no tengo acceso desde esta interfaz». Las dos son
falsas —el fichero está en su disco y la base de datos también—, pero no eran
mentiras del modelo: de las herramientas que se le ofrecían, **ninguna sabía
abrir un fichero**, y encima se le daba un solo tiro (elegir UNA herramienta,
ejecutarla y a otra cosa).

Aquí están las dos mitades que faltaban:

1. **Encadenar.** Mirar algo, ver lo que sale y decidir el siguiente paso con
   eso delante. Un tiro suelto no resuelve «cuántas líneas tiene X»: primero
   hay que abrirlo.
2. **No dar por buena la rendición.** Si la respuesta que iba a salir es un «no
   puedo» o un «dame tú el dato», se mira si de verdad no había forma. Esa red
   es la que convierte «no tengo acceso a mi base de datos» en ir a contarlas.

Y lo que «sí o sí» NO puede significar, porque si no esto sale caro y además
miente: insistir a lo tonto. Hay tope de pasos, no se repite una llamada que ya
se hizo, cada resultado se recorta antes de volver al modelo, y lo que no se
puede hacer se dice — como hace la guía de canales con Node.
"""
from __future__ import annotations

import json
import logging
import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("celestia_v1")

MAX_PASOS = 3            # mirar, mirar otra vez y responder: más es dar vueltas
MAX_EVIDENCIA = 1800     # un fichero entero no cabe (ni hace falta) en el contexto
MAX_SEGUNDOS = 45        # al otro lado hay alguien esperando en un chat


def _recortar(resultado: str) -> str:
    """Recorta lo que vuelve al modelo, pero SIN perder de qué tamaño era.

    Un fichero entero no cabe en el contexto. El problema es que al recortarlo
    se pierde justo lo que suelen preguntar: «¿cuántas líneas tiene?». Pasó en
    vivo — leía el CHANGELOG y contestaba que el fragmento estaba recortado, en
    vez del número. Así que el recorte lleva la cuenta dentro.
    """
    if len(resultado) <= MAX_EVIDENCIA:
        return resultado
    lineas = resultado.count("\n") + 1
    return (resultado[:MAX_EVIDENCIA] +
            f"\n… (recortado para que quepa. El contenido COMPLETO tiene "
            f"{lineas} líneas y {len(resultado)} caracteres — si te preguntan "
            f"cuántas líneas o cuánto ocupa, es este dato, no el del trozo.)")


@dataclass
class Evidencia:
    herramienta: str
    params: Dict[str, Any]
    resultado: str

    def linea(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"[{self.herramienta}({args})]\n{self.resultado}"


@dataclass
class Intento:
    evidencias: List[Evidencia] = field(default_factory=list)
    respuesta: str = ""

    @property
    def hubo_suerte(self) -> bool:
        return bool(self.evidencias)


# ── ¿Se estaba rindiendo? ───────────────────────────────────────────────────
# Determinista a propósito: hay que poder decidirlo con cualquier modelo detrás
# y sin gastar otra llamada solo para preguntarle si se ha rendido.
_RENDICION_RE = re.compile(
    r"\b(?:no\s+(?:puedo|tengo\s+(?:acceso|forma|manera)|dispongo|s[ée])\b"
    r"|no\s+me\s+es\s+posible"
    r"|me\s+temo\s+que\s+no"
    r"|(?:podr[íi]as|puedes)\s+(?:facilitarme|pasarme|decirme|indicarme)"
    r"|necesitar[íi]a\s+que\s+me\s+(?:pases|des|digas)"
    r"|no\s+est[áa]\s+a\s+mi\s+alcance)",
    re.I)

# Cosas que NO son rendición aunque lo parezcan: negarse a algo por criterio
# propio es una respuesta, no un fallo, y volver a intentarlo sería insistirle
# a quien ya ha dicho que no.
_NEGATIVA_LEGITIMA_RE = re.compile(
    r"\b(?:no\s+(?:quiero|voy\s+a|deber[íi]a)|prefiero\s+no|"
    r"no\s+me\s+parece|eso\s+no\s+estar[íi]a\s+bien)", re.I)


def parece_rendicion(texto: str) -> bool:
    """¿Esta respuesta es un «no puedo» que quizá no había que dar?"""
    t = (texto or "").strip()
    if not t or _NEGATIVA_LEGITIMA_RE.search(t):
        return False
    return bool(_RENDICION_RE.search(t))


# ── «Sí» a lo que ella misma ofreció ────────────────────────────────────────
# Chat real (25 sep 2026): «Si quieres que inicie la búsqueda, dime sí» → «Si» →
# volvía a explicar el plan y a pedir permiso, dos veces, sin buscar nunca. Un
# «sí» suelto no dispara ninguna herramienta y el modelo no ata cabos solo.
_SI_RE = re.compile(
    r"^\W*(?:s[ií]|claro|vale|venga|dale|ok(?:ey|ay)?|adelante|hazlo|h[aá]gale|"
    r"pues\s+(?:s[ií]|adelante|hazlo|claro)|por\s+supuesto|de\s+acuerdo|perfecto|"
    r"s[ií]\s+porfa(?:vor)?|va)\b", re.I)
_OFRECE_RE = re.compile(
    r"(?:quieres\s+que|te\s+parece\s+(?:bien\s+)?que|dime\s+\W?s[ií]\b|"
    r"responde\s+\W?s[ií]\b|si\s+quieres|te\s+(?:lo\s+)?(?:busco|preparo|hago|miro)|"
    r"proceder[ée]|paso\s+a\s+la\s+acci[oó]n|voy\s+a\s+(?:comenzar|empezar|buscar|"
    r"hacer|preparar|investigar))", re.I)


_PERO_RE = re.compile(
    r"\b(?:no|pero|espera|aguarda|a[uú]n|todav[ií]a|luego|despu[eé]s|ma[ñn]ana|"
    r"antes|otro\s+d[ií]a|m[aá]s\s+tarde|mejor\s+no|nada)\b", re.I)
# Si el usuario pidió JSON (o código), un objeto JSON es la respuesta buena
_PIDE_JSON_RE = re.compile(
    r"\b(?:json|diccionario|objeto|estructura|formato|c[oó]digo|api|schema|esquema)\b",
    re.I)


def oferta_aceptada(texto_usuario: str, ultima_propia: str) -> Optional[str]:
    """Si el usuario dice «sí» a algo que Celestia acababa de ofrecer, la orden
    completa para hacerlo; si no, None. Determinista: vale con cualquier modelo."""
    t = (texto_usuario or "").strip()
    if not t or len(t) > 40 or not _SI_RE.match(t):
        return None
    # «claro que no», «vale, pero espera», «sí, luego»: empieza por sí y no lo es
    if _PERO_RE.search(t):
        return None
    oferta = (ultima_propia or "").strip()
    if not oferta or not _OFRECE_RE.search(oferta):
        return None
    return ("El usuario acaba de decir «" + t + "» a lo que le ofreciste. HAZLO YA con "
            "las herramientas, sin volver a explicar el plan ni a pedir permiso. Lo "
            "que le ofreciste:\n«" + oferta[-900:] + "»")


# ── ¿Ha salido una orden en vez de una respuesta? ───────────────────────────
# Chat real (25 sep 2026): Groq devolvió vacío, contestó NVIDIA, y lo que llegó
# al chat fue {"consulta": "juegos más jugados…", "cantidad": 10, "fuente":
# "web"}: los argumentos de una herramienta escritos como texto.
RESPUESTA_TRABADA = ("Perdona, se me ha trabado la respuesta: me ha salido una orden "
                     "interna en vez de texto. ¿Me lo repites?")


_ORDEN_CORCHETES_RE = re.compile(r"\[\s*[a-z][a-z_]{2,40}\s*:?\s*\{[^\]]*\}\s*\]")
# Lo mismo con etiquetas, como escriben otros modelos (DeepSeek, 4 oct 2026:
# «<tool>generar_imagen</tool>» salió tal cual en el chat).
_ORDEN_ETIQUETA_RE = re.compile(
    r"<\s*(?:tool|tool_call|function(?:_call)?|invoke)\b[^>]*>.*?"
    r"<\s*/\s*(?:tool|tool_call|function(?:_call)?|invoke)\s*>", re.I | re.S)


def parece_orden_cruda(texto: str, peticion: str = "") -> bool:
    """¿La respuesta es una orden de herramienta y no algo que se pidió?

    Las claves no sirven para decidirlo: en el chat eran «consulta» y
    «fuente», que no son parámetros de ninguna herramienta (se las inventó el
    modelo). Lo que sí distingue es si el usuario pidió JSON o código."""
    if _PIDE_JSON_RE.search(peticion or ""):
        return False
    # 27 sep 2026: «La vuelvo a intentar… [generar_imagen: {"prompt": …}]»
    # llegó así al chat, dos veces, y luego «sigo intentándolo» sin nada en
    # marcha. Una orden entre corchetes en cualquier parte de la respuesta es
    # una promesa que nadie va a cumplir.
    # Dentro de un bloque de código es un ejemplo, no una orden («el formato
    # sería `[generar_imagen: {…}]`» — revisión de Codex).
    sin_codigo = re.sub(r"```.*?```|`[^`\n]*`", " ", texto or "", flags=re.S)
    if _ORDEN_CORCHETES_RE.search(sin_codigo) or _ORDEN_ETIQUETA_RE.search(sin_codigo):
        return True
    t = (texto or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        datos = json.loads(t)
    except ValueError:
        # JSON roto pero con pinta de claves: «{"consulta": "…", …}»
        return bool(re.match(r'\{\s*"[\w\-]{1,30}"\s*:', t))
    return isinstance(datos, dict) and bool(datos)


# ── El bucle ────────────────────────────────────────────────────────────────

_SISTEMA = (
    "Eres el motor de herramientas de Celestia, que vive en el móvil del "
    "usuario. Antes de decir que no puedes con algo, MIRA con las herramientas "
    "que tienes: los ficheros del proyecto, sus carpetas y su base de datos "
    "están en este mismo dispositivo, no en internet. No pidas al usuario datos "
    "que puedes averiguar tú.\n"
    # Sesión 74: con solo lo de arriba, «¿Y cuántos habitantes tiene?» (se
    # hablaba de Canberra) acabó listando las carpetas del proyecto.
    "Lo que es del MUNDO (lugares, personas, cifras, precios, actualidad) se "
    "busca con buscar_web; los ficheros son para preguntas sobre ficheros. Si "
    "la pregunta se apoya en lo que se venía hablando («¿y cuántos tiene?»), "
    "entiéndela con esa conversación.\n"
    "Encadena si hace falta: primero localiza o abre, y luego responde con lo "
    "que hayas visto. Cuando ya tengas lo necesario, no llames a más "
    "herramientas."
)


def _turnos_de_contexto(contexto: Any) -> List[Dict[str, str]]:
    """Los últimos turnos del chat, recortados: dan el tema, no la charla entera."""
    turnos = []
    for t in (contexto if isinstance(contexto, list) else [])[-4:]:
        if isinstance(t, dict) and t.get("role") in ("user", "assistant") and t.get("content"):
            turnos.append({"role": t["role"], "content": str(t["content"])[:300]})
    return turnos


def intentar(texto_usuario: str,
             modelo,
             ejecutar: Callable[[str, Dict[str, Any]], str],
             tools_payload: List[Dict],
             nombres_validos,
             max_pasos: int = MAX_PASOS,
             contexto: Any = None) -> Intento:
    """Deja que el modelo encadene herramientas hasta tener con qué responder.

    `ejecutar(nombre, params)` corre la herramienta de verdad; se le pasa desde
    fuera para que esto se pueda probar sin tocar el disco ni la red.
    `contexto` son los últimos turnos del chat: sin ellos, un seguimiento como
    «¿Y cuántos habitantes tiene?» no dice de qué se está hablando.
    """
    intento = Intento()
    # Tope de reloj además del de pasos: cada paso es una llamada al modelo más
    # la herramienta, y tres pasos lentos son minuto y medio mirando una
    # pantalla parada. Mejor responder con lo que se tenga.
    limite = time.time() + MAX_SEGUNDOS
    mensajes = ([{"role": "system", "content": _SISTEMA}]
                + _turnos_de_contexto(contexto)
                + [{"role": "user", "content": texto_usuario}])
    ya_pedidas: set = set()

    for paso in range(max_pasos):
        if time.time() > limite:
            logger.info("empeño: se acabó el tiempo tras %d paso(s)", paso)
            break
        try:
            llamadas = modelo.function_call(mensajes, tools_payload)
        except Exception as e:
            logger.debug("empeño: el modelo no pudo elegir herramienta: %s", e)
            break
        if not llamadas:
            break

        llamada = llamadas[0]
        nombre = llamada.get("name", "")
        params = llamada.get("arguments", {}) or {}
        if nombre not in nombres_validos:
            # Defensa en profundidad: aunque el modelo se invente «borrar», por
            # aquí no se ejecuta. Es la misma regla que ya había con un tiro.
            logger.warning("empeño: descarto herramienta no permitida %r", nombre)
            break

        firma = (nombre, json.dumps(params, sort_keys=True, default=str))
        if firma in ya_pedidas:
            break                      # pedir dos veces lo mismo es dar vueltas
        ya_pedidas.add(firma)

        try:
            resultado = str(ejecutar(nombre, params))
        except Exception as e:
            resultado = f"(no salió: {e})"
            logger.info("empeño: %s falló: %s", nombre, e)

        recortado = _recortar(resultado)
        # La misma respuesta otra vez es dar vueltas aunque los parámetros
        # cambien: pasó leyendo el mismo fichero como «CHANGELOG.md» y como
        # «/root/Celestia/CHANGELOG.md», que para la firma son distintos.
        if intento.evidencias and intento.evidencias[-1].resultado == recortado:
            break
        intento.evidencias.append(Evidencia(nombre, params, recortado))
        logger.info("empeño paso %d: %s → %.60s", paso + 1, nombre, recortado)

        # El resultado vuelve al modelo como contexto, no como `role: tool`: así
        # funciona igual con cualquier backend, sin depender de que respete el
        # protocolo de tool_call_id.
        mensajes.append({"role": "assistant", "content": f"He mirado: {nombre}"})
        mensajes.append({"role": "user", "content":
                         f"Resultado de {nombre}:\n{recortado}\n\n"
                         f"Si con esto ya puedes responder, no llames a más "
                         f"herramientas."})

    return intento


def redactar(texto_usuario: str, intento: Intento, modelo, contexto: Any = None) -> str:
    """La respuesta final a partir de lo que se ha visto.

    Se le pasa al modelo lo encontrado y se le pide que conteste con ESO. Sin
    este paso, la respuesta sería el volcado crudo de la herramienta — que para
    «¿cuántas líneas tiene el CHANGELOG?» sería el CHANGELOG entero.
    Con `contexto` (los últimos turnos) un seguimiento se entiende al redactar.
    """
    if not intento.evidencias:
        return ""
    visto = "\n\n".join(e.linea() for e in intento.evidencias)
    previos = _turnos_de_contexto(contexto)
    charla = ("Lo que se venía hablando:\n" + "\n".join(
        f"- {'Usuario' if t['role'] == 'user' else 'Celestia'}: {t['content']}"
        for t in previos) + "\n\n") if previos else ""
    prompt = (
        "Has mirado en el dispositivo y esto es lo que has encontrado:\n\n"
        f"{visto}\n\n"
        f"{charla}"
        f"Pregunta del usuario: {texto_usuario}\n\n"
        "Contesta en español, en corto y con lo que has visto. Si lo que "
        "encontraste no responde del todo, dilo claramente y di qué te falta. "
        "No te inventes nada que no esté arriba."
    )
    try:
        return (modelo.generate(prompt, max_new_tokens=320, temperature=0.2) or "").strip()
    except Exception as e:
        logger.warning("empeño: no pude redactar la respuesta: %s", e)
        # Mejor lo crudo que nada: es feo, pero es el dato que pidió.
        return intento.evidencias[-1].resultado
