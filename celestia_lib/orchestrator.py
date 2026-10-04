"""Orchestrator: coordina ModelWrapper + MemoryDB + Embedder + hiperparámetros.

Incluye GoalManager, MetricsLogger, HyperparamAdapter, SnapshotManager
como helpers internos del orquestador.

Extraído del monolito en sesión 15.
"""
from __future__ import annotations

import csv
import difflib
import json
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _TZ_USUARIO = ZoneInfo("Europe/Madrid")
except ImportError:
    _TZ_USUARIO = None
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .connectivity import ConnectivityManager
from .embedder import Embedder
from .knowledge_graph import KnowledgeGraph
from .memoria_personal import MemoriaPersonal
from . import idiomas
from .memory import MemoryDB, QUEJA_DE_TRATO_RE, hecho_es_ruido
from .paths import MEM_DIR
from .planner import MultiObjectivePlanner, PlanExecutor
from .reasoning import SymbolicReasoner
from .continuous_learning import (
    AdapterManager, DatasetBuilder, LearningScheduler, LoRATrainer,
    estado_resumen as _aprendizaje_resumen,
)
from .abstraction import AbstractionEngine, Ejemplo, PatternStore
from . import actividad
from . import formato
from . import restriccion_letras
from .model import ModelWrapper, _MULETILLA_RE, quitar_muletillas
from .resources import ResourceManager

logger = logging.getLogger("celestia_v1")

# Bloque 6: red ANCHA y multiidioma para decidir si el turno necesita las capas
# de capacidad del system prompt. Si NO matchea ninguna, es charla pura y el
# prompt se poda ~45%. Conservador: cualquier indicio → incluir.
#
# Sesión 46 — las dos capas se deciden POR SEPARADO. Antes era un único booleano
# que encendía las dos a la vez, y como la red incluye «busca», «clima»,
# «noticias» y cualquier dígito, TODA búsqueda web arrastraba las ~777 tokens de
# comandos UI ([TAP:x,y], [BRIGHTNESS], [TOGGLE_WIFI]…) que no iba a usar jamás —
# justo en el turno más caro, el que ya lleva 2.000 chars de contexto de internet
# y revienta el TPM de Groq. Los términos ambiguos (una captura de pantalla es a
# la vez UI y tool) están A PROPÓSITO en las dos redes: el riesgo a evitar sigue
# siendo podar de más.
#
# Sin \b de cierre a propósito: así matchea verbos con enclítico (recuérda-me,
# lláma-me, anóta-melo…). Un falso positivo solo incluye una capa de más (seguro).

# → _CAP_DISPOSITIVO: comandos UI, voz, canal y límites del SO.
_SENAL_DISPOSITIVO_RE = re.compile(
    r"\b(?:app|aplicaci|abre|abrir|cierra|cerrar|instala|desinstala|"
    r"wifi|bluetooth|linterna|brillo|volumen|pantalla|captura|"
    r"foto|imagen|im[aá]genes|"
    r"llama|ll[aá]mame|mensaje|sms|whatsapp|telegram|"
    r"comando|ejecuta|ejecutar|terminal|"
    r"enciende|apaga|controla|dom[oó]tica|luz|luces|term[oó]stato|"
    # Voz, audio y canal: los gobierna esta capa («PUEDES cambiar tu voz»). No
    # estaban en la red única — «háblame con acento argentino» se podaba y se
    # perdía la regla, así que Celestia contestaba «no puedo cambiar mi voz».
    r"voz|audio|acento|habla|hablas|hablame|h[aá]blame|nota\s+de\s+voz|"
    r"open|close|run|command|screen|volume|brightness|flashlight|call)",
    re.I,
)

# → _CAP_HERRAMIENTAS: catálogo de tools + anti-invención de archivos/acciones.
_SENAL_HERRAMIENTAS_RE = re.compile(
    r"\d"  # números → cálculo, fecha, hora, recordatorio
    r"|\b(?:archivo|carpeta|directorio|fichero|documento|pdf|docx|"
    r"pantalla|captura|foto|imagen|im[aá]genes|genera|crea|crear|dibuj|"
    r"busca|buscar|b[uú]scame|investiga|encuentra|googlea|noticias|titulares|"
    r"clima|tiempo|temperatura|pron[oó]stico|"
    r"recu[eé]rda|recuerda|recordatorio|alarma|agenda|programa|an[oó]tame|ap[uú]nta|"
    r"calcula|calcular|cu[aá]nt|cuenta|letras|palabras|hora|fecha|d[ií]as|ra[ií]z|"
    r"contrase[nñ]|vault|clave|pin|comando|ejecuta|ejecutar|terminal|"
    r"file|folder|search|weather|news|remind|alarm|"
    r"calculate|compute|count|picture|image|password|run|command|how\s+many)",
    re.I,
)

# Anexos que SÍ exigen la capa de dispositivo: el resultado de abrir/cerrar una
# app, un comando UI ya emitido o el contenido de la pantalla. El [RESULTADO] de
# una búsqueda web no está aquí a propósito.
_ANEXO_UI_RE = re.compile(
    r"\[CAPTURA|\[TAP:|\[SWIPE:|\[OPEN_APP:|\[CLOSE_APP:|\[OCR_SCREEN|\[LIST_APPS|"
    r"Abriendo|Cerrando|abriendo la app|pantalla actual|texto de la pantalla",
    re.I,
)

# Unión de ambas — «este turno necesita ALGUNA capa de capacidad». Se conserva
# porque hay código y tests que preguntan justo eso; `test_system_prompt_capas`
# verifica que sigue siendo equivalente a la red única de antes.
_SENAL_CAPACIDAD_RE = re.compile(
    _SENAL_DISPOSITIVO_RE.pattern + "|" + _SENAL_HERRAMIENTAS_RE.pattern, re.I,
)

def _segundos_desde(ts) -> float:
    """Antigüedad de un recuerdo en segundos. Devuelve `inf` si no se sabe.

    El infinito es a propósito: un recuerdo sin fecha se trata como viejo, que
    es el lado seguro. Ver `_recuerdos_utilizables`.
    """
    if ts is None:
        return float("inf")
    try:
        if isinstance(ts, (int, float)):
            epoch = float(ts)
        else:
            from datetime import datetime as _dt
            try:
                epoch = _dt.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except Exception:
                epoch = float(ts)
        return max(0.0, time.time() - epoch)
    except Exception:
        return float("inf")


def _tiempo_relativo(ts) -> str:
    """Convierte un timestamp ISO o epoch en frase tipo 'hace 10 minutos'.

    Acepta None, int/float (epoch) o str (ISO). Devuelve '' si no es parseable.
    Permite al LLM contextualizar recuerdos sin alucinar fechas.
    """
    if ts is None:
        return ""
    try:
        if isinstance(ts, (int, float)):
            epoch = float(ts)
        else:
            from datetime import datetime as _dt
            try:
                epoch = _dt.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except Exception:
                epoch = float(ts)
        delta = max(0, time.time() - epoch)
    except Exception:
        return ""
    if delta < 60:
        return "hace unos segundos"
    mins = int(delta // 60)
    if mins < 60:
        return f"hace {mins} min" if mins != 1 else "hace 1 min"
    horas = mins // 60
    if horas < 24:
        return f"hace {horas} h" if horas != 1 else "hace 1 h"
    dias = horas // 24
    if dias < 30:
        return f"hace {dias} días" if dias != 1 else "ayer"
    meses = dias // 30
    if meses < 12:
        return f"hace {meses} meses" if meses != 1 else "hace 1 mes"
    anios = meses // 12
    return f"hace {anios} años" if anios != 1 else "hace 1 año"


# El comando que el modelo se inventa cuando quiere buscar. NO existe: no hay
# ningún `[BUSCAR_WEB:…]` en el código, y salía publicado tal cual —tres veces
# en una misma conversación, con el usuario esperando un enlace—. Dice
# literalmente qué quiere buscar, así que se busca de verdad con esa consulta.
# Sesión 74: también con forma de llamada, y metida en un [TAP:…] —
# «[TAP:buscar_web("Go Karts <dos pueblos de la zona>")]» salió publicado
# tal cual en el chat real—.
_CORCHETE_BUSQUEDA_RE = re.compile(
    r"\[\s*(?:TAP\s*:\s*)?"
    r"(?:(?:BUSCAR_WEB|BUSCAR|BUSQUEDA|B[UÚ]SQUEDA|SEARCH|WEB_SEARCH)\s*[:=]\s*"
    r"|(?:buscar_web|web_search|search)\s*\(\s*)"
    r"[\"'«]?([^\]\"'»)]{2,200}?)[\"'»]?\s*\)?\s*\]",
    re.I,
)


# Detecta frases que prometen / aseguran haber buscado en internet,
# usadas para anti-alucinación cuando NO se ejecutó buscar_web este turno.
_ALUCINA_BUSQUEDA_RE = re.compile(
    r"(?:estoy\s+buscando|voy\s+a\s+buscar(?:te|le)?|acabo\s+de\s+buscar|"
    r"buscar[ée]\s+(?:en\s+)?internet|d[eé]jame\s+buscar|"
    r"permite(?:me)?\s+buscar|consultando\s+(?:la\s+)?web|"
    r"busqu[ée]\s+(?:en\s+)?internet)[^.\n]*[.\n]?",
    re.I,
)
# Sesión 34 (B34-3): regex que captura la FRASE COMPLETA (incluyendo
# pregunta huérfana tipo "¿Quieres que ...?" o "Si quieres ..."). Se aplica
# tras `_ALUCINA_BUSQUEDA_RE` para que la sustitución no deje fragmentos
# huérfanos del tipo «¿Quieres que No encontré información actualizada».
_FRASE_ALUCINA_RE = re.compile(
    r"(?:[¿?¡!]?\s*[A-ZÁÉÍÓÚÑ][^.!?\n]*?\b"
    r"(?:estoy\s+buscando|voy\s+a\s+buscar(?:te|le)?|acabo\s+de\s+buscar|"
    r"buscar[ée]\s+(?:en\s+)?internet|d[eé]jame\s+buscar|"
    r"permite(?:me)?\s+buscar|consultando\s+(?:la\s+)?web|"
    r"busqu[ée]\s+(?:en\s+)?internet)"
    r"[^.!?\n]*[.!?\n]?)",
    re.I,
)

# Sesión 41: el modelo "se rinde" diciendo que no tiene información sobre un
# tema externo. Si NO buscó antes, forzamos búsqueda y regeneramos (cubre
# formulaciones que los triggers de _should_search no anticipan: "dime la
# historia de X", "quiero saber sobre X", etc.). Determinista, robusto.
_NIEGA_INFO_RE = re.compile(
    r"\bno\s+(?:tengo|dispongo\s+de|cuento\s+con|poseo|he\s+encontrado|encontr[eé]|"
    r"hall[eé]|hay|ten[ií]a)\s+(?:la\s+|una\s+|suficiente\s+|mucha\s+)?"
    r"(?:informaci[oó]n|datos|detalles|nada\s+(?:de\s+)?(?:informaci[oó]n|concreto))"
    r"|\blo\s+siento,?\s+(?:pero\s+)?no\s+(?:tengo|dispongo|cuento|s[eé]|encontr[eé])\b"
    r"|\bno\s+(?:s[eé]|conozco|estoy\s+segur[oa])\s+(?:mucho|nada|gran\s+cosa|lo\s+suficiente)\b"
    r"|\bno\s+tengo\s+(?:datos|informaci[oó]n)\s+(?:reciente|actual|espec[ií]fic)",
    re.I,
)

# Sesión 44 — red de seguridad de frescura. Detecta que Celestia acaba de
# AFIRMAR de memoria un dato con fecha de caducidad sin haber buscado nada.
# El usuario lo pidió así: «no quiero que le pase como a ti, que tienes
# información sólo hasta mayo de 2026». Los triggers de _should_search miran
# la PREGUNTA y siempre se les escapará alguna formulación; esto mira la
# RESPUESTA, que es donde el dato viejo se ve.
_AFIRMACION_CADUCABLE_RE = re.compile(
    r"\b(?:actualmente|en\s+la\s+actualidad|hoy\s+en\s+d[ií]a|"
    r"a\s+d[ií]a\s+de\s+hoy|hasta\s+la\s+fecha)\b"
    r"|\b(?:la\s+[uú]ltima\s+versi[oó]n|la\s+versi[oó]n\s+m[aá]s\s+reciente)\b"
    r"|\bel\s+(?:\w+\s+)?m[aá]s\s+(?:avanzad|potent|recient|nuev|modern)\w*\b"
    r"|\b(?:mi|mis)\s+(?:conocimiento|informaci[oó]n|entrenamiento|datos)\s+"
    r"(?:\w+\s+){0,2}?(?:llega|llegan|alcanza|alcanzan|termina|se\s+detiene|"
    r"tiene\s+fecha|est[aá]\s+actualizad)"
    r"|\bhasta\s+(?:mi\s+[uú]ltima\s+actualizaci[oó]n|donde\s+(?:yo\s+)?s[eé])\b"
    # Sesión 45 — más formas de afirmar «esto es lo de ahora» sin decir
    # «actualmente»: «los referentes actuales», «ahora mismo lidera», «sigue
    # siendo el rey». Salieron de un caso real: la respuesta sobre chips daba
    # el Snapdragon 8 Gen 3 y el A18 (2024) como lo último de agosto de 2026.
    r"|\b(?:ahora\s+mismo|en\s+este\s+momento|hoy\s+por\s+hoy|"
    r"por\s+el\s+momento|de\s+momento|a\s+fecha\s+de\s+hoy)\b"
    r"|\b(?:referente|l[ií]der|rey|campe[oó]n|est[aá]ndar)(?:es)?\s+"
    r"(?:actual(?:es)?|de\s+(?:hoy|ahora|la\s+industria|el\s+mercado))\b"
    r"|\b(?:lidera|lideran|domina|dominan|encabeza|encabezan)\s+"
    r"(?:\w+\s+){0,2}?(?:el\s+mercado|la\s+lista|el\s+ranking|"
    r"la\s+gama|el\s+sector)\b"
    r"|\bsigue\s+siendo\s+(?:el|la|lo)\b"
    # Producto + número de versión dicho de memoria: GPT-4, Claude 3, Llama 3…
    r"|\b(?:gpt|claude|gemini|llama|grok|qwen|mistral|opus|sonnet|haiku|"
    r"deepseek|android|ios|windows|python|"
    # Sesión 45 — hardware: la lista anterior era solo de modelos de IA y SO.
    r"snapdragon|dimensity|exynos|kirin|bionic|rtx|gtx|ryzen|"
    r"iphone|galaxy|pixel|playstation|\bps\b|xbox|switch)\s*-?\s*\d+(?:\.\d+)?\b"
    # «Gen 3», «generación 5», «v2.1» — número de generación suelto.
    r"|\b(?:gen|generaci[oó]n)\.?\s*\d+\b",
    re.I,
)

# Preguntas sobre la propia Celestia: «actualmente puedo hacer X» no es un
# dato de internet, es su ficha técnica. No dispara la red de frescura.
_PREGUNTA_SOBRE_SI_MISMA_RE = re.compile(
    r"\b(?:celestia|puedes|sabes\s+hacer|eres\s+capaz|qu[eé]\s+haces|"
    r"c[oó]mo\s+funcionas|qui[eé]n\s+eres)\b",
    re.I,
)

# Sesión 45 — cierre limpio de respuestas truncadas por el tope de tokens.
# Los proveedores cortan en seco al llegar a max_tokens y la respuesta acaba a
# media frase («En resumen: Si buscas potencia bruta»). Determinista: si el
# texto es largo y acaba en mitad de una palabra o frase, se recorta hasta la
# última frase completa — pero solo si sobrevive la mayor parte del contenido,
# para no mutilar respuestas cortas que legítimamente no llevan punto final
# («Hola», «Claro 😊»).
# El criterio va al revés: una respuesta está BIEN terminada sólo si acaba en
# un cierre explícito (puntuación, comilla, paréntesis o emoji). Cualquier otra
# cosa —una letra, un asterisco de markdown a medias, dos puntos— es un corte.
# Mirarlo al revés (listar los finales «malos») deja fuera casos raros: la
# primera versión se comía el caso real «…¿quién es r*» por acabar en asterisco.
_FIN_LIMPIO_RE = re.compile(
    r"""[.!?…»”"')\]}]$"""
    r"""|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2764\uFE0F]$""",
    re.UNICODE,
)
# Un texto que acaba en enlace se deja intacto: recortarlo se comería la fuente.
# Tres o más líneas que empiezan por viñeta o número: es una lista.
_ES_LISTA_RE = re.compile(r"(?:^[ \t]*(?:[-*•·]|\d+[.)])\s+.*\n){2,}", re.M)
# Un apartado numerado sin nada debajo, y la señal de que en esa lista los
# apartados sí llevan sub-puntos (línea sangrada o con viñeta tras el número).
_TITULO_APARTADO_RE = re.compile(r"^\s*\d+[.)]\s+\S[^\n]{0,80}$")
_APARTADO_CON_CONTENIDO_RE = re.compile(
    r"^[ \t]*\d+[.)]\s+[^\n]+\n(?:[ \t]+\S|[ \t]*[-*•·]\s)", re.M)
# La FRASE completa que contiene un ofrecimiento de búsqueda, para poder
# quitarla sin dejar muñones («¿Quieres que ?»).
# Coletillas que atribuyen al usuario el contexto que ha traído Celestia:
# «según la información actualizada que has compartido…». El usuario no ha
# compartido nada — lo buscó ella. Se quita el arranque y se deja la frase,
# que sí tiene contenido.
_COLETILLA_CONTEXTO_RE = re.compile(
    r"^\s*(?:seg[uú]n|de\s+acuerdo\s+con|bas[aá]ndome\s+en|conforme\s+a)\s+"
    r"(?:la\s+|el\s+|los\s+|las\s+)?"
    r"(?:informaci[oó]n|datos|contexto|resultados?|b[uú]squeda)\s*"
    r"(?:actualizad\w+|reciente|disponible)?\s*"
    r"(?:que\s+(?:has|me\s+has|se\s+me\s+ha|me)\s+"
    r"(?:compartido|pasado|dado|proporcionado|facilitado)|proporcionad\w+|"
    r"compartid\w+)?\s*,\s*",
    re.IGNORECASE,
)

# La misma atribución falsa, pero a mitad o al final de la respuesta y en forma
# de frase entera: «El contenido que compartiste de internet no está relacionado
# con este tema, así que no afecta mi respuesta». Nadie compartió nada — es el
# contexto que ella misma buscó, y contarlo solo confunde a quien lee.
_COMENTA_EL_CONTEXTO_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)[^.!?\n]*?"
    # La fuente tiene que ser SUYA (internet, la búsqueda, «lo proporcionado»).
    # Si dice «el documento que me enviaste», el material sí es del usuario y
    # la frase es legítima: no se toca.
    r"(?:contenido|informaci[oó]n|texto|resultados?|datos|b[uú]squeda)\s+"
    r"(?:[^.!?\n]{0,25}?\s+)?"
    r"(?:de\s+internet|de\s+la\s+web|del?\s+buscador|proporcionad\w+)"
    r"[^.!?\n]*?\b(?:no\s+(?:est[aá]n?\s+relacionad\w+|tienen?\s+que\s+ver|"
    r"menciona[ns]?|afectan?|aportan?|guardan?\s+relaci[oó]n|dicen?\s+nada))"
    r"[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


# ── Estilo de chat, garantizado sin depender del modelo ──────────────────────
# El usuario: «no me gusta cómo puntúa y ordena, se ve feo». Celestia escribía
# como un artículo de blog —«## 1. Qué necesitas», separadores «---», «te dejo
# una guía práctica»— y eso en un chat de móvil se lee fatal. La regla está en
# el system prompt, pero un modelo puede ignorarla: esto lo asegura.
_SEPARADOR_MD_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.M)
_ENCABEZADO_MD_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*(?:\d+[.)][ \t]*)?(.+?)[ \t]*$", re.M)
# Se borra la FRASE ENTERA, no sólo el trozo: quitando únicamente «te dejo una
# guía práctica» quedaba colgando un «Con la información que tengo a mano,».
_PREAMBULO_DOC_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?\n]))\s*[^.!?\n]*?\b(?:"
    r"(?:aqu[ií]\s+tienes|te\s+(?:dejo|presento|comparto|paso))\s+"
    r"(?:una?\s+|el\s+|la\s+|los\s+|las\s+)?(?:gu[ií]a|lista|resumen|tabla|"
    r"desglose|comparativa|selecci[oó]n|propuesta|configuraci[oó]n|opciones|"
    r"recomendaci[oó]n|pasos|claves)"
    r"|te\s+lo\s+explico\s+paso\s+a\s+paso"
    r"|vamos\s+por\s+partes"
    r"|con\s+la\s+informaci[oó]n\s+que\s+tengo"
    r")\b[^.!?:\n]*[.!?:][ \t]*",
    re.IGNORECASE,
)


# El modelo también titula SIN almohadillas: «…de tu presupuesto: El Procesador
# (CPU): · Recomendación: …». Sin ## no se detectaba como encabezado y el
# título acababa dentro de la viñeta anterior. Se busca una frase corta
# acabada en dos puntos que venga detrás de un punto y delante de una lista o
# de un salto.
_TITULO_SUELTO_RE = re.compile(
    r"(?<=[.!?])[ \t]+([A-ZÁÉÍÓÚÑ][^.!?\n]{2,45}:)[ \t]*(?=\n|[-*•])")


def _estilo_chat(texto: str) -> str:
    """Quita la maquetación de informe y deja algo que parezca un mensaje.

    El código, los comandos y los enlaces se apartan antes de tocar nada. Todo
    lo de aquí abajo está pensado para PROSA: convierte `# título` en el
    arranque de un párrafo (y con ello se comía las almohadillas de los
    comentarios de Python), colapsa los espacios dobles (y la indentación) y
    junta líneas. Medido en vivo: una función de doce líneas salía en cuatro,
    con un espacio de sangría y sin comentarios — imposible de ejecutar.
    """
    if not texto:
        return texto
    # Un ``` sin pareja se cierra AQUÍ, antes de apartar nada: si el modelo se
    # dejó el cierre, `proteger` no reconoce el bloque y todo lo de abajo entra
    # a saco en el código. Es el caso más común cuando la respuesta acaba
    # justo con el bloque.
    if texto.count("```") % 2 == 1:
        texto = texto.rstrip() + "\n```"
    texto, _intocable = formato.proteger(texto)
    limpio = _SEPARADOR_MD_RE.sub("", texto)
    # Un título se convierte en el arranque del párrafo que venía debajo, que
    # es como lo diría alguien hablando: «Qué necesitas: para IA local…».
    # El salto de delante es imprescindible: sin él, «Opción 2:» se pegaba al
    # último punto de la lista anterior y se leía como parte de esa viñeta.
    def _titulo(m):
        texto_titulo = m.group(1).strip()
        # Si el título ya lleva sus propios dos puntos («Opción 2: Equipo ya
        # montado») no se le añaden otros, o queda un doble.
        if texto_titulo.endswith((":", ".", "?", "!")) or ":" in texto_titulo:
            return "\n" + texto_titulo
        return "\n" + texto_titulo + ":"

    limpio = _ENCABEZADO_MD_RE.sub(_titulo, limpio)
    limpio = _PREAMBULO_DOC_RE.sub(" ", limpio)
    limpio = _TITULO_SUELTO_RE.sub(r"\n\n\1\n", limpio)
    # Un título seguido de línea en blanco y su párrafo: se pegan.
    # …salvo si lo que viene debajo es un bloque apartado (código): «Claro:
    # ```python» en la misma línea deja el bloque sin abrir en su renglón.
    limpio = re.sub(r"(^|\n)([^\n]{3,60}:)\n{2,}(?=[^\s\-*•\d\x00])",
                    r"\1\2 ", limpio)
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return formato.restaurar(limpio.strip(), _intocable)


_FRASE_OFRECIMIENTO_RE = re.compile(
    r"[^.!?\n]*(?:(?:quieres|dime)\s+(?:que\s+)?(?:lo\s+)?busque|"
    r"puedo\s+(?:hacer\s+una\s+)?b[uú]squeda|puedo\s+buscar|"
    r"si\s+quieres\s+(?:lo\s+)?busco|¿(?:te\s+)?lo\s+busco|"
    r"dime\s+[\"«]?s[ií][\"»]?\s+y\s+lo\s+(?:miro|busco))"
    r"[^.!?\n]*[.!?\n]?",
    re.IGNORECASE,
)
_ACABA_EN_URL_RE = re.compile(r"https?://\S+$", re.I)
# «…» suele cerrar bien («ya veremos…»), pero cuando el proveedor corta a mitad
# de palabra deja cosas como «…se sumó la p…»: ahí no es un final, es la cicatriz
# del corte, y colarse por `_FIN_LIMPIO_RE` dejaba la respuesta truncada tal cual.
_PUNTOS_TRAS_PALABRA_CORTADA_RE = re.compile(r"(?:^|\s)\w{1,2}…$")


# ── Idioma de la RESPUESTA ───────────────────────────────────────────────────
# El hint del turno («responde SIEMPRE en ESPAÑOL») va pegado al mensaje del
# usuario y aun así no siempre gana: los modelos razonadores (gpt-oss y
# parientes) contestan a veces en inglés a una pregunta en español, o sueltan
# su cadena de pensamiento tal cual. Visto en vivo: «dame 5 pasos para cocinar
# arroz» → «Wait, but different types of rice might require…», entero y en
# inglés, publicado como respuesta.
#
# Se mide la salida en vez de pedirle al modelo que se porte bien: palabras
# funcionales, que son las que de verdad separan dos idiomas en un texto corto.
# Ni «no» ni «a» entran — existen en los dos.
_ES_FUNC_RE = re.compile(
    r"\b(?:de|la|que|el|en|y|los|se|del|las|un|por|con|una|para|es|al|lo|como|"
    r"más|pero|sus|le|ya|porque|esta|entre|cuando|muy|sin|sobre|también|hasta|"
    r"hay|donde|desde|todo|nos|durante|todos|uno|les|ni|contra|ese|eso|antes|"
    r"qué|unos|yo|otro|él|tanto|esa|estos|mucho|nada|cual|poco|ella|tu|tus|mi|"
    r"mis|te|me|aquí|puedes|tienes|además|así)\b", re.I)
_EN_FUNC_RE = re.compile(
    r"\b(?:the|of|and|to|in|is|it|you|that|was|for|on|are|with|as|his|they|be|"
    r"at|have|this|from|or|had|by|but|what|we|can|were|all|your|when|use|how|"
    r"said|each|she|which|their|if|will|about|would|there|been|who|now|find|"
    r"any|new|need|should|might|because|these|them|here|let|don't|doesn't|"
    r"i'll|i'm|it's|that's)\b", re.I)


def _idioma_grueso(texto: str) -> str:
    """«es», «en» o «» (ni idea). Grueso a propósito: solo hay que distinguir
    español de inglés, no acertar el idioma de una frase de tres palabras."""
    if not texto:
        return ""
    # El código no cuenta: `for`, `if`, `return` no son inglés hablado.
    limpio, _ = formato.proteger(texto)
    es = len(_ES_FUNC_RE.findall(limpio))
    en = len(_EN_FUNC_RE.findall(limpio))
    if es >= 3 and es >= en * 2:
        return "es"
    if en >= 3 and en >= es * 2:
        return "en"
    return ""


# Pedir otro idioma es legítimo: ahí la respuesta en ese idioma es la correcta.
_PIDE_OTRO_IDIOMA_RE = re.compile(
    r"\b(?:en\s+(?:ingl[eé]s|italiano|franc[eé]s|alem[aá]n|portugu[eé]s|"
    r"catal[aá]n|gallego|euskera|japon[eé]s|chino|[aá]rabe|ruso)|in\s+english|"
    r"traduce|traducci[oó]n|translate|c[oó]mo\s+se\s+dice)\b", re.I)
# Nombre anterior, por si algo externo lo usaba.
_PIDE_INGLES_RE = _PIDE_OTRO_IDIOMA_RE


def _parece_espanol(texto: str) -> bool:
    """¿Está esto escrito en español?

    Contar palabras funcionales españolas vale para CUALQUIER otro idioma sin
    tener que mantener una lista por cada uno: un texto largo en italiano
    («Capito, metto via il gergo finto e parlo come uno vero»), francés o
    alemán no tiene «de», «que», «los» ni «para». Se pide un texto de cierta
    longitud para no juzgar un «Vale» o un «Ok».
    """
    limpio, _ = formato.proteger(texto or "")
    palabras = re.findall(r"[^\W\d_]+", limpio, re.UNICODE)
    if len(palabras) < 12:
        return True                      # muy corto: no hay con qué decidirlo
    # Cuenta la DENSIDAD, no el número: el italiano comparte piezas con el
    # español («una», «uno», «la») y con un umbral fijo de dos colaba entero.
    # Una frase española lleva un tercio largo de palabras funcionales; en
    # otro idioma bajan al 10 % o menos.
    return len(_ES_FUNC_RE.findall(limpio)) / len(palabras) >= 0.15


def _mas_ingles_que_espanol(texto: str) -> bool:
    """Para la PREGUNTA, donde no hay longitud para el detector grueso.

    «give me 5 steps to cook rice» no llega a las tres marcas que pide
    `_idioma_grueso`, y sin esto su respuesta en inglés —la correcta— se
    regeneraba en español.
    """
    limpio, _ = formato.proteger(texto or "")
    # Empate cuenta como inglés: «me», «no» o «tu» suman en las dos listas
    # («give me 5 steps to cook rice» empataba a uno y salía como español).
    en = len(_EN_FUNC_RE.findall(limpio))
    return en >= 1 and en >= len(_ES_FUNC_RE.findall(limpio))


def _respuesta_extraviada(respuesta: str, pregunta: str,
                          esperado: Optional[str] = None) -> bool:
    """La respuesta se ha ido a un idioma que nadie pidió.

    Antes esto era «no parece español → mal». Con Celestia contestando a medio
    mundo, esa regla hacía justo el daño que pretendía evitar: a quien escribía
    en alemán, en ruso o en árabe se le regeneraba la respuesta **en español**
    (visto en vivo, sesión 54). Lo que hay que comparar no es contra el
    español: es contra el idioma en el que le han hablado.

    Se abstiene siempre que no esté segura —una respuesta de tres palabras, o
    una pregunta sin idioma claro—: regenerar por si acaso cuesta una llamada y
    arriesga empeorar una respuesta que estaba bien.
    """
    if not respuesta or _PIDE_OTRO_IDIOMA_RE.search(pregunta or ""):
        return False
    esperado = esperado or idiomas.detectar(pregunta or "")
    if not esperado:
        return False                     # sin saber en qué le hablaron, no se toca
    dice = idiomas.detectar(respuesta)
    if not dice:
        return False                     # respuesta corta o ambigua: tampoco
    return dice != esperado


# Repetir la MISMA respuesta a quien ya se ha quejado dos veces es lo que la
# delata como máquina. Pasó tal cual: «Perdona si ha sonado borde, no era mi
# intención. Te he contestado firme porque…», palabra por palabra, con cuatro
# turnos de por medio y la persona escribiendo «es que no paras».
_REPE_UMBRAL = 0.80
# El chat de siempre: el de Termux, el de WhatsApp y todo lo que no diga cuál.
HILO_POR_DEFECTO = "principal"


def _idioma_preferido() -> str:
    """El idioma elegido a mano, o 'auto'. Se lee del perfil en cada turno
    porque se puede cambiar hablando y tiene que valer desde el mensaje
    siguiente, sin reiniciar nada."""
    try:
        datos = json.loads((MEM_DIR / "perfil_usuario.json").read_text(encoding="utf-8"))
        codigo = (datos.get("idioma") or "auto").lower()
        return codigo if (codigo == "auto" or idiomas.es_valido(codigo)) else "auto"
    except (OSError, ValueError):
        return "auto"


_LISTA_O_DATO_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s", re.M)


def _nucleo_comparable(texto: str) -> str:
    """El texto sin lo que no distingue una respuesta de otra."""
    return re.sub(r"[^\wáéíóúüñ ]+", " ", (texto or "").lower()).strip()


def _ya_lo_dijo(respuesta: str, historial: List[Dict[str, str]]) -> Optional[str]:
    """Devuelve la respuesta anterior que esta repite, si la hay.

    Repetir NO siempre está mal: si le piden dos veces la misma función en
    Python, la respuesta correcta es la misma función. Por eso queda fuera todo
    lo que tenga código, lista o tabla —contenido que se pide, no charla— y solo
    se mira lo social, que es donde repetirse hace daño.
    """
    if not respuesta or not historial:
        return None
    if "```" in respuesta or _LISTA_O_DATO_RE.search(respuesta) or "|" in respuesta:
        return None
    nucleo = _nucleo_comparable(respuesta)
    if len(nucleo) < 40:
        return None            # un «vale» repetido no es un problema
    previas = [t.get("content") or "" for t in historial[-8:]
               if t.get("role") == "assistant"]
    for previa in reversed(previas[-3:]):
        otro = _nucleo_comparable(previa)
        if not otro or "```" in previa:
            continue
        m = difflib.SequenceMatcher(None, nucleo, otro)
        if m.quick_ratio() >= _REPE_UMBRAL and m.ratio() >= _REPE_UMBRAL:
            return previa
    return None


# Un texto que acaba en un bloque de código apartado está bien terminado: el
# marcador no lleva puntuación, pero el bloque sí es un final.
_ACABA_EN_BLOQUE_RE = re.compile(r"\x00\d+\x00$")


# Peticiones de brevedad: ahí una respuesta de tres palabras es lo correcto
# («di hola en tres palabras» → «Hola, buen día»).
_PIDE_BREVEDAD_RE = re.compile(
    r"\b(?:en\s+(?:una|1)\s+(?:frase|l[ií]nea|palabra)|"
    r"en\s+(?:dos|tres|cuatro|cinco|\d+)\s+palabras|"
    r"responde\s+(?:solo|s[oó]lo|[uú]nicamente)|"
    r"s[eé]\s+breve|breve(?:mente)?|corto|corta|resumido|"
    r"s[ií]\s+o\s+no|una\s+palabra)\b",
    re.IGNORECASE,
)
# Turnos donde una respuesta corta es la natural: saludos, despedidas, gracias.
_TURNO_CORTES_RE = re.compile(
    r"^\s*[¿¡]*\s*(?:hola|buenas|buenos\s+d[ií]as|buenas\s+(?:tardes|noches)|"
    r"adi[oó]s|hasta\s+luego|hasta\s+ma[ñn]ana|chao|gracias|vale|ok|de\s+nada|"
    r"s[ií]|no|perfecto|genial)\b",
    re.IGNORECASE,
)
# La cortesía de un mensaje corto puede ir en cualquier sitio («Ns gracias
# supongo», «pues vale entonces»), y esto es lo que lo convierte en una
# petición que sí merece respuesta entera. Las interrogativas, solo con tilde
# o con «?»: sin tilde son conjunciones («ya veo que sí»).
_CORTESIA_SUELTA_RE = re.compile(
    r"\b(?:gracias|thanks|vale|ok|okey|genial|perfecto|de\s+nada|"
    r"hasta\s+(?:ma[ñn]ana|luego|pronto)|buenas\s+noches|muy\s+bien|"
    r"entendido|ya\s+veo|supongo|ns|nose|no\s+s[eé])\b",
    re.IGNORECASE,
)
_PIDE_ALGO_RE = re.compile(
    r"\?|\b(?:expl[ií]ca(?:me)?|dime|cu[eé]ntame|hazme|haz|busca|pon|dame|"
    r"ay[uú]dame|por\s+qu[eé]|qué|cómo|cuál|dónde|cuándo|quién)(?!\w)",
    re.IGNORECASE,
)


# Preguntas cuya respuesta legítima cabe en una palabra: un cálculo, una
# fecha, una capital. El dato es la respuesta, no un corte.
_PREGUNTA_DE_DATO_RE = re.compile(
    r"\b(?:cu[aá]nto[s]?\s+(?:es|son|hace[n]?|vale[n]?|mide|pesa)|"
    r"qu[eé]\s+(?:hora|d[ií]a|fecha|a[ñn]o)\b|cu[aá]l\s+es\s+la\s+capital|"
    r"c[oó]mo\s+se\s+(?:dice|escribe|llama)|"
    r"cu[aá]nt[oa]s\s+(?:letras|palabras|s[ií]labas|a[ñn]os|d[ií]as|horas)|"
    r"\d+\s*[+\-*/x×÷^%])",
    re.IGNORECASE,
)


def _es_munon(respuesta: str, pregunta: str) -> bool:
    """¿La respuesta es un muñón: tres o cuatro palabras y nada más?

    Del chat real, con la conversación ya tensa: «Perdona si he parecido
    brusca», «Lamento si te resulto molesta», «Entiendo, Lydia». Contestar así
    a alguien que acaba de decirte que estás siendo borde confirma justo lo
    que te está reprochando. No cuentan las respuestas a un saludo, ni cuando
    la brevedad se pidió, ni las que traen un dato (un número, una hora, un
    enlace): ahí lo corto es la virtud.
    """
    resp = (respuesta or "").strip()
    preg = (pregunta or "").strip()
    if not resp or len(resp.split()) > 5:
        return False
    if len(preg.split()) < 3:
        return False
    if _PIDE_BREVEDAD_RE.search(preg):
        return False
    # «Vale» a secas admite un «lo que necesites»; «Vale, y ahora explícame…»
    # ya trae tema y merece una respuesta entera.
    if _TURNO_CORTES_RE.match(preg) and len(preg.split()) <= 4:
        return False
    # Sesión 74 — «Ns gracias supongo»: la cortesía no siempre va delante. A
    # eso le basta un «De nada, Enzo.»; regenerarlo trajo un párrafo genérico
    # («es normal no tenerlo todo claro al principio…») que no venía a cuento.
    # Si además pide o pregunta algo, sí merece respuesta entera.
    if (len(preg.split()) <= 5 and _CORTESIA_SUELTA_RE.search(preg)
            and not _PIDE_ALGO_RE.search(preg)):
        return False
    # Una pregunta de dato puntual se contesta con el dato, y a veces el dato
    # es una palabra («Cuatro», «Madrid», «El jueves»).
    if _PREGUNTA_DE_DATO_RE.search(preg):
        return False
    # Un dato es un dato aunque quepa en cuatro palabras («Son las 14:35»).
    if re.search(r"\d|https?://|[=€$%]", resp):
        return False
    return True


def _cerrar_en_frase_completa(texto: str) -> str:
    """Recorta una respuesta cortada por el tope de tokens a su última frase.

    El código se aparta antes: buscar «la última frase» dentro de un bloque de
    Python cortaba la función por el primer punto que hubiera dentro y se
    llevaba por delante el ``` de cierre. Y sin cierre, el bloque deja de ser
    un bloque para todo lo que viene después.
    """
    if not texto:
        return texto
    # Un bloque abierto y sin cerrar (el modelo se dejó el ```) se cierra aquí:
    # es justo el trabajo de esta función, dejar la respuesta terminada.
    if texto.count("```") % 2 == 1:
        texto = texto.rstrip() + "\n```"
    texto, _bloques = formato.proteger(texto)
    t = texto.rstrip()
    if _ACABA_EN_BLOQUE_RE.search(t):
        return formato.restaurar(texto, _bloques)
    if len(t) < 200 or _ACABA_EN_URL_RE.search(t):
        return formato.restaurar(texto, _bloques)
    if _FIN_LIMPIO_RE.search(t) and not _PUNTOS_TRAS_PALABRA_CORTADA_RE.search(t):
        return formato.restaurar(texto, _bloques)
    if _PUNTOS_TRAS_PALABRA_CORTADA_RE.search(t):
        # Fuera la cola cortada («…se sumó la p…») ANTES de buscar el final de
        # frase: si no, esos mismos puntos suspensivos cuentan como frase
        # terminada y el recorte se queda donde estaba.
        t = _PUNTOS_TRAS_PALABRA_CORTADA_RE.sub("", t).rstrip()
        texto = t
    # En una lista, la línea que quedó a medias es la última; las de arriba
    # están completas aunque no acaben en punto («- CPU: Intel Core Ultra 7»).
    # Buscar frases aquí no sirve: el último punto puede estar en el párrafo
    # de introducción y el recorte se comería la lista entera.
    lineas = t.split("\n")
    if len(lineas) >= 3 and _ES_LISTA_RE.search(t):
        restantes = lineas[:-1]
        # Sesión 74: si lo cortado eran los sub-puntos de un apartado numerado,
        # su título se quedaba colgando («5. Optimizar el uso» y nada más). Se
        # quita también, pero solo si los demás apartados traen algo debajo: en
        # una lista plana, «3. Apunta los DNS» es un punto completo.
        while (len(restantes) >= 2 and _TITULO_APARTADO_RE.match(restantes[-1])
               and _APARTADO_CON_CONTENIDO_RE.search("\n".join(restantes[:-1]))):
            restantes = restantes[:-1]
        recorte = "\n".join(restantes).rstrip()
        if len(recorte) >= len(t) * 0.6:
            logger.info("Lista truncada por el tope de tokens → quitada la última "
                        "línea incompleta (%d→%d chars)", len(t), len(recorte))
            return formato.restaurar(recorte, _bloques)
    finales = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", t)]
    if not finales:
        return formato.restaurar(texto, _bloques)
    corte = finales[-1]
    # Si el recorte se comería más del 40 % de la respuesta, es mejor dejarla
    # entera aunque acabe a medias: perder tanto contenido es peor que el corte.
    if corte < len(t) * 0.6:
        return formato.restaurar(texto, _bloques)
    logger.info("Respuesta truncada por el tope de tokens → cerrada en la última frase (%d→%d chars)",
                len(t), corte)
    return formato.restaurar(t[:corte].rstrip(), _bloques)


# Deps opcionales locales
try:
    import faiss  # noqa: F401
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False


class GoalManager:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def pick(self) -> Optional[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id,goal,priority,status FROM goals WHERE status='pending' ORDER BY priority DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            return {"id": row[0], "goal": row[1], "priority": row[2], "status": row[3]}
        return None

    def list_all(self) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT id,goal,priority,status FROM goals ORDER BY priority DESC")
        return [{"id": r[0], "goal": r[1], "priority": r[2], "status": r[3]}
                for r in cur.fetchall()]

    def mark_satisfied(self, goal_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE goals SET status='satisfied', updated_at=? WHERE id=?",
            (time.time(), goal_id),
        )
        self.conn.commit()

    def add(self, goal: str, priority: int = 5):
        now = time.time()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO goals (goal,priority,status,created_at,updated_at) VALUES (?,?,'pending',?,?)",
            (goal, priority, now, now),
        )
        self.conn.commit()


# ─────────────────────────────────────────────
# MetricsLogger
# ─────────────────────────────────────────────
class MetricsLogger:
    FIELDS = ["ts", "cycle", "task", "ppl", "drift", "coherence", "temp", "top_k", "note"]

    def __init__(self, path: str):
        self.path = path
        if not Path(path).exists():
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def log(self, **kwargs):
        row = {}
        for f in self.FIELDS:
            v = kwargs.get(f, "")
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                v = ""
            row[f] = v
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(row)


# ─────────────────────────────────────────────
# HyperparamAdapter
# ─────────────────────────────────────────────
class HyperparamAdapter:
    def __init__(self, config: Config):
        self.config = config
        self.temp = config.GEN_TEMP
        self.top_k = config.GEN_TOP_K
        self.rep_penalty = config.GEN_REP_PENALTY
        self.history: deque = deque(maxlen=20)
        self.hparams_log = config.HPARAMS_LOG

    def update(self, coherence: float, ppl: float, diversity: float):
        self.history.append({"coh": coherence, "ppl": ppl, "div": diversity})
        if len(self.history) < 4:
            return

        avg_coh = sum(h["coh"] for h in self.history) / len(self.history)
        avg_ppl = sum(h["ppl"] for h in self.history) / len(self.history)
        avg_div = sum(h["div"] for h in self.history) / len(self.history)

        reason = ""
        changed = False

        if avg_coh < self.config.COH_THRESHOLD:
            old = self.temp
            self.temp = max(self.config.MIN_TEMP, self.temp * 0.92)
            if self.temp != old:
                reason = f"coh_bajo={avg_coh:.3f}"
                changed = True
        elif avg_ppl > self.config.PPL_THRESHOLD:
            old = self.temp
            self.temp = max(self.config.MIN_TEMP, self.temp * 0.95)
            if self.temp != old:
                reason = f"ppl_alto={avg_ppl:.1f}"
                changed = True

        if avg_div < 0.4 and self.temp < self.config.MAX_TEMP * 0.8:
            self.temp = min(self.config.MAX_TEMP, self.temp * 1.08)
            reason = (reason + "+div_bajo" if reason else f"div_bajo={avg_div:.3f}")
            changed = True

        if avg_coh > 0.6 and avg_ppl < 15:
            old_k = self.top_k
            self.top_k = min(self.config.MAX_TOP_K, int(self.top_k * 1.05))
            if self.top_k != old_k:
                reason = (reason + "+coh_ok" if reason else "coh_ok")
                changed = True

        if changed:
            entry = {
                "ts": time.time(), "temp": round(self.temp, 4),
                "top_k": self.top_k, "rep_penalty": self.rep_penalty,
                "reason": reason, "avg_coh": round(avg_coh, 4), "avg_ppl": round(avg_ppl, 2),
            }
            with open(self.hparams_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info("Hparams [%s]: temp=%.3f top_k=%d", reason, self.temp, self.top_k)

    def params(self) -> Dict:
        return {"temp": self.temp, "top_k": self.top_k, "rep_penalty": self.rep_penalty}


# ─────────────────────────────────────────────
# SnapshotManager
# ─────────────────────────────────────────────
class SnapshotManager:
    def __init__(self, config: Config):
        self.config = config
        self.snap_dir = Path(config.SNAPSHOTS_DIR)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.last_snap: Optional[Path] = None
        self.last_metrics: Dict = {}
        self._load_last_snapshot()

    def _load_last_snapshot(self):
        snaps = sorted(self.snap_dir.glob("snap_*"), key=lambda p: p.name)
        if snaps:
            self.last_snap = snaps[-1]
            metrics_file = self.last_snap / "metrics.json"
            if metrics_file.exists():
                try:
                    with open(metrics_file) as f:
                        self.last_metrics = json.load(f)
                    logger.info("SnapshotManager: métricas previas cargadas desde %s", self.last_snap.name)
                except Exception:
                    pass

    def save(self, metrics: Dict):
        ts = int(time.time())
        snap = self.snap_dir / f"snap_{ts}"
        snap.mkdir(exist_ok=True)
        try:
            shutil.copy2(self.config.DB_PATH, snap / "celestia.db")
        except Exception as e:
            logger.warning("Snapshot DB fallido: %s", e)
        try:
            faiss_path = Path(self.config.FAISS_INDEX)
            if faiss_path.exists():
                shutil.copy2(faiss_path, snap / "faiss.index")
            meta_path = Path(self.config.FAISS_META)
            if meta_path.exists():
                shutil.copy2(meta_path, snap / "faiss_meta.json")
        except Exception as e:
            logger.warning("Snapshot FAISS fallido: %s", e)

        with open(snap / "metrics.json", "w") as f:
            json.dump(metrics, f)

        self.last_snap = snap
        self.last_metrics = metrics
        logger.info("Snapshot guardado: %s", snap.name)

        snaps = sorted(self.snap_dir.glob("snap_*"), key=lambda p: p.name)
        for old in snaps[:-5]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass

    def restore(self) -> bool:
        if not self.last_snap or not self.last_snap.exists():
            logger.warning("No hay snapshot para restaurar")
            return False
        try:
            db_src = self.last_snap / "celestia.db"
            if db_src.exists():
                shutil.copy2(db_src, self.config.DB_PATH)
            fi_src = self.last_snap / "faiss.index"
            if fi_src.exists():
                shutil.copy2(fi_src, self.config.FAISS_INDEX)
            fm_src = self.last_snap / "faiss_meta.json"
            if fm_src.exists():
                shutil.copy2(fm_src, self.config.FAISS_META)
            logger.info("Rollback completado desde %s", self.last_snap.name)
            return True
        except Exception as e:
            logger.error("Rollback fallido: %s", e)
            return False

    def should_rollback(self, metrics_after: Dict) -> bool:
        if not self.last_metrics:
            return False
        coh_before = self.last_metrics.get("avg_coh", 0.0)
        coh_after = metrics_after.get("avg_coh", 0.0)
        ppl_before = self.last_metrics.get("avg_ppl", 999.0)
        ppl_after = metrics_after.get("avg_ppl", 999.0)

        if coh_before > 0 and (coh_before - coh_after) > self.config.ROLLBACK_COH_DROP:
            logger.warning("Rollback: coherencia cayó %.3f → %.3f", coh_before, coh_after)
            return True
        if ppl_before > 0:
            rel = (ppl_after - ppl_before) / max(1.0, ppl_before)
            if rel > self.config.ROLLBACK_PPL_RISE:
                logger.warning("Rollback: PPL subió %.1f → %.1f", ppl_before, ppl_after)
                return True
        return False


# ─────────────────────────────────────────────
# Tareas del ciclo autónomo
# ─────────────────────────────────────────────
TASK_POOL = [
    ("AutoEval",    "Evalúa las últimas 5 respuestas del sistema e identifica 3 áreas de mejora concreta."),
    ("BrechasIA",   "Lista 5 brechas abiertas en investigación de IA que un equipo pequeño podría explorar."),
    ("PlanMejora",  "Diseña un plan de 3 pasos para mejorar la consistencia en las respuestas del agente."),
    ("AnalisisCog", "Explica la diferencia entre razonamiento deductivo e inductivo con ejemplos prácticos."),
    ("Creatividad", "Propón una herramienta de IA novedosa que resuelva un problema cotidiano ignorado."),
    ("MetricaDise", "Diseña una métrica interna que aproxime la generalización mejor que la perplexity."),
    ("SeguridadIA", "Describe 3 riesgos de seguridad en agentes autónomos y cómo mitigarlos."),
    ("OptimMemoria","Sugiere cómo organizar la memoria episódica para recuperación eficiente a largo plazo."),
]


def _rotate_task(pool: deque, history: List[str]) -> Tuple[str, str]:
    for _ in range(len(pool)):
        name, prompt = pool[0]
        pool.rotate(-1)
        if name not in history[-3:]:
            if random.random() < 0.12:
                prompt += " Incluye un contraejemplo breve."
            return name, prompt
    return pool[0]


# ─────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────
class Orchestrator:
    """Coordina ModelWrapper + MemoryDB + PerfilUsuario + HyperparameterScheduler.

    Punto de entrada principal: respond(texto) → str. Internamente:
      1. Recupera contexto similar de MemoryDB (FAISS + FTS5 + re-rank semántico)
      2. Compone system prompt dinámico con arquitectura real + hechos del usuario
      3. Llama al modelo (con cadena de fallback)
      4. En background: extrae hechos persistentes, actualiza coherencia/drift
    """
    def __init__(self, config: Config, resources: ResourceManager = None):
        self.config = config
        self.resources = resources or ResourceManager()
        self.connectivity = ConnectivityManager()
        self.model = ModelWrapper(config, self.resources, self.connectivity)
        # Path del cache de embeddings persistente (acelera arranques y queries repetidas)
        _embed_cache = str(Path(config.DB_PATH).parent / "embed_cache.npz")
        self.embedder = Embedder(
            config.EMBED_MODEL, config.MODEL_CACHE, self.resources.device,
            cache_disco_path=_embed_cache,
        )
        self.memory = MemoryDB(config, self.embedder)
        # Conectar el sink de errores del ModelWrapper a la memoria a largo plazo
        self.model._error_sink = self.memory.registrar_error
        # World model (grafo de conocimiento). Comparte el archivo SQLite con
        # MemoryDB pero usa su propia conexión y tablas (kg_*) — separación de concerns.
        self.knowledge = KnowledgeGraph(config.DB_PATH)
        # Memoria personal con vigencia temporal (sesión 39): capa unificada
        # sobre el KG para datos del usuario con histórico («trabajo en X» →
        # «ya no» → «ahora en Y»). Nunca borra: versiona.
        self.memoria_personal = MemoriaPersonal(self.knowledge)
        self._migrar_hechos_a_grafo_si_necesario()
        # Planner multiobjetivo (roadmap AGI punto #4). Usa el LLM del ModelWrapper
        # como generador+evaluador de planes. Tablas planner_* aisladas.
        _herramientas = [
            "buscar_web", "leer_archivo", "listar_archivos", "buscar_archivos",
            "crear_documento", "crear_archivo", "enviar_archivo", "generar_imagen",
            "descargar_archivo", "recordatorio", "info_sistema",
        ]
        self.planner = MultiObjectivePlanner(
            config.DB_PATH,
            llm_callable=lambda prompt: self.model.generate(
                prompt, max_new_tokens=MultiObjectivePlanner.MAX_TOKENS_PLAN
            ),
            herramientas_disponibles=_herramientas,
        )
        # Razonador simbólico (roadmap AGI punto #1). Verificación determinista
        # contra el grafo + aritmética segura. NO consume tokens LLM en su uso
        # normal (solo regex + sandbox AST).
        self.reasoner = SymbolicReasoner(
            knowledge=self.knowledge, memory=self.memory,
            llm_callable=lambda prompt: self.model.generate(prompt, max_new_tokens=400),
        )
        # Aprendizaje continuo con LoRA (roadmap AGI punto #2). Pipeline lista
        # para fine-tunear con conversaciones acumuladas. En hardware insuficiente
        # se queda en modo dry_run sin fingir entrenamiento.
        _adapters_dir = str(Path(config.DB_PATH).parent / "lora_adapters")
        self.aprendizaje_builder = DatasetBuilder(config.DB_PATH)
        self.aprendizaje_trainer = LoRATrainer(
            modelo_base=config.MODEL_NAME, adapters_dir=_adapters_dir,
        )
        self.aprendizaje_manager = AdapterManager(config.DB_PATH, _adapters_dir)
        self.aprendizaje_scheduler = LearningScheduler(
            builder=self.aprendizaje_builder,
            trainer=self.aprendizaje_trainer,
            manager=self.aprendizaje_manager,
            recursos_ok=lambda: True,  # api.py lo enlazará con watchdog
        )
        # Abstracción cognitiva (roadmap AGI punto #5). Inductor de reglas
        # con sandbox AST y verificación contra holdout. Honestidad: NO resuelve
        # ARC; es meta-learning aproximado sobre patrones algorítmicos.
        self.abstraccion_store = PatternStore(config.DB_PATH)
        self.abstraccion = AbstractionEngine(
            store=self.abstraccion_store,
            llm_callable=lambda prompt: self.model.generate(prompt, max_new_tokens=200),
        )
        # Simulación causal + predictor Markov (post-AGI #3). Grafo de
        # relaciones causa→efecto con Bayesian update + Monte Carlo simulator
        # para «¿qué pasaría si…?». Predictor del próximo acto del usuario
        # bucketizado por hora/día. Honestidad: NO es JEPA ni causal-discovery
        # moderno; es simulador estadístico sobre relaciones declarativas.
        from .causal import CausalGraph, CausalSimulator, MarkovPredictor
        self.causal_graph = CausalGraph(config.DB_PATH)
        if not self.causal_graph.listar(limit=1):
            self.causal_graph.cargar_seed()
        self.causal_sim = CausalSimulator(self.causal_graph)
        self.markov = MarkovPredictor(config.DB_PATH)
        # Program synthesis con DSL (post-AGI #5). Búsqueda enumerativa
        # tipada + primitivas aprendidas estilo DreamCoder light. Honestidad:
        # NO resuelve ARC-Challenge completo; sirve para inducir
        # transformaciones compositivas sobre listas/enteros/strings.
        from .program_synthesis import LearnedPrimitivesStore, ProgramSynthesizer
        self.synthesis_store = LearnedPrimitivesStore(config.DB_PATH)
        self.synthesizer = ProgramSynthesizer(
            learned_store=self.synthesis_store, max_tamano=4, timeout_seg=10.0,
        )
        self.goals = GoalManager(self.memory.conn)
        self.hparams = HyperparamAdapter(config)
        self.snapshots = SnapshotManager(config)
        self.metrics_log = MetricsLogger(config.METRICS_CSV)
        self.raw_log = Path(config.RAW_LOG)
        self.task_pool = deque(TASK_POOL)
        random.shuffle(self.task_pool)

        self.cycle = 0
        self.ema_coh = None
        self.ema_ppl = None
        self.task_history: List[str] = []
        self.ppl_history: deque = deque(maxlen=config.DRIFT_WINDOW)
        self.last_snapshot_cycle = 0
        # Un chat aparte por cada conversación abierta. Hasta ahora había UNA
        # sola lista para todo: abrir un chat nuevo en la web no servía de nada,
        # porque Celestia seguía teniendo delante lo que se había hablado en el
        # anterior (y lo de Termux, y lo de WhatsApp). `conv_history` sigue
        # siendo el hilo ACTIVO —todo el código que lo usa no se entera— y
        # `_hilos` guarda el resto.
        # El idioma del último mensaje que sí se pudo reconocer: un «vale» no
        # cambia de idioma una conversación entera.
        self._ultimo_idioma: Optional[str] = None
        self._hilos: Dict[str, List[Dict[str, str]]] = {}
        self.hilo_actual: str = HILO_POR_DEFECTO
        self.conv_history: List[Dict[str, str]] = self._hilos.setdefault(
            HILO_POR_DEFECTO, [])

        logger.info(
            "Orchestrator listo — backend=%s cargado=%s faiss=%s embedder_semántico=%s",
            self.model._backend, self.model.loaded, _HAS_FAISS,
            self.embedder.model is not None,
        )

    def _migrar_hechos_a_grafo_si_necesario(self) -> None:
        """One-shot: si el grafo solo tiene la entidad-usuario, copia los hechos
        existentes como atributos de esa entidad. Idempotente — no duplica si
        ya se hizo antes. Sólo añade contexto inicial al grafo recién creado;
        la tabla `hechos_usuario` original sigue intacta como fuente canónica
        hasta que el extractor LLM convierta todo al grafo en uso normal.
        """
        try:
            stats = self.knowledge.stats()
            if stats["entidades_total"] > 1:
                return  # Ya hay más que solo el usuario — migración ya hecha o no aplicable
            hechos = self.memory.hechos_usuario()
            if not hechos:
                return
            atrs_usuario = {}
            for h in hechos:
                clave = (h.get("clave") or "").strip()
                valor = (h.get("valor") or "").strip()
                if clave and valor:
                    atrs_usuario[clave] = valor
            if atrs_usuario:
                self.knowledge.upsert_entidad(
                    tipo="persona",
                    nombre=self.knowledge.usuario_nombre,
                    atributos=atrs_usuario,
                    fuente="migracion_hechos",
                )
                logger.info("Migrados %d hechos al grafo de conocimiento", len(atrs_usuario))
        except Exception as e:
            logger.warning("Migración hechos→grafo falló: %s", e)

    def verificar_respuesta(self, texto: str):
        """Verifica una respuesta del LLM contra el grafo + aritmética.

        Devuelve `ResultadoVerificacion` (ver `celestia_lib.reasoning`).
        Útil para detectar contradicciones explícitas o errores aritméticos
        en lo que Celestia acaba de decir, sin invocar al LLM.
        """
        try:
            return self.reasoner.verificar(texto)
        except Exception as e:
            logger.warning("verificar_respuesta falló: %s", e)
            return None

    def _gate_calidad_local(self, user_input: str, response: str) -> str:
        """Gate de calidad para respuestas del modelo local pequeño.

        Cuando Celestia cae al backend local (porque Groq y OpenRouter fallaron,
        p. ej. sin conexión), el modelo es pequeño y propenso a divagar o
        alucinar. Este gate mide la perplejidad de la respuesta; si es alta
        (señal de divague) y además `verificar_respuesta` detecta una
        contradicción contra el grafo de conocimiento, antepone un aviso de baja
        confianza honesto en lugar de presentar lo dudoso como cierto. Ataca la
        alucinación en la raíz, donde el system prompt no llega.

        NO actúa con backend remoto (Groq/OpenRouter): esos modelos son grandes
        y no exponen logits, así que el comportamiento es idéntico al de antes.
        Es la pieza que conecta `compute_perplexity` (hasta ahora solo
        telemetría) y `reasoning.verificar` como un filtro real de calidad.
        """
        backend = getattr(self.model, "_backend", "none")
        if backend in ("groq", "openrouter", "none"):
            return response
        if not response or len(response.split()) < 4:
            return response  # respuestas muy cortas no son divague
        try:
            ppl = self.model.compute_perplexity(response)
        except Exception:
            return response
        # La perplejidad vive en dos escalas según el backend (logits vs
        # heurística de repetición); cada una con su umbral. Ver config.
        escala = self.model.escala_perplejidad()
        umbral = (self.config.PPL_GATE_REAL if escala == "real"
                  else self.config.PPL_GATE_HEURISTICA)
        # Instrumentación de calibración: registra SIEMPRE (no solo cuando salta)
        # para poder mirar las dos distribuciones por separado más adelante.
        logger.info("gate calidad: backend=%s escala=%s ppl=%.1f umbral=%.0f",
                    backend, escala, ppl, umbral)
        if ppl < umbral:
            return response  # perplejidad normal para su escala → confiamos
        # Perplejidad alta: verificamos si además contradice hechos conocidos.
        res = self.verificar_respuesta(response)
        if res is not None and not getattr(res, "consistente", True):
            logger.info("gate calidad: ppl=%.1f (%s) + contradicción → baja confianza",
                        ppl, escala)
            return ("⚠️ No estoy del todo segura de esto: estoy con mi modelo "
                    "local (sin conexión a mis modelos grandes) y detecté una "
                    "posible inconsistencia en lo siguiente. Tómalo con cautela "
                    "y, si puedes, pídeme que lo verifique con conexión.\n\n"
                    + response)
        # ppl alta SIN contradicción: puede ser falta de sensibilidad… o que el
        # grafo tiene poco contra qué contrastar. Logueamos el tamaño del grafo
        # para no calibrar a ciegas en la dirección equivocada.
        logger.debug("gate calidad: ppl=%.1f (%s) alta sin contradicción; grafo=%d hechos vigentes",
                     ppl, escala, self._hechos_en_grafo())
        return response

    def _hechos_en_grafo(self) -> int:
        """Nº de relaciones vigentes en el grafo (hechos contra los que el gate
        puede contradecir). Si es bajo, que el gate casi nunca salte NO es falta
        de sensibilidad del umbral, sino grafo pobre — distinción clave al calibrar."""
        try:
            kg = getattr(self.reasoner, "knowledge", None)
            return int(kg.stats().get("relaciones_vigentes", 0)) if kg else 0
        except Exception:
            return -1

    def aprendizaje_estado(self) -> Dict[str, Any]:
        """Resumen del estado de aprendizaje continuo (hardware + dataset + último adapter)."""
        try:
            return _aprendizaje_resumen(self.aprendizaje_builder, self.aprendizaje_manager)
        except Exception as e:
            logger.warning("aprendizaje_estado falló: %s", e)
            return {"error": str(e)}

    def aprendizaje_entrenar(self, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Dispara un entrenamiento ahora (ignorando políticas de tiempo).

        Si dry_run es None, usa el valor del scheduler. dry_run=True valida la
        pipeline sin entrenar realmente — útil para tests o cuando no hay GPU.
        """
        try:
            if dry_run is not None:
                prev = self.aprendizaje_scheduler.dry_run
                self.aprendizaje_scheduler.dry_run = bool(dry_run)
                try:
                    return self.aprendizaje_scheduler.entrenar_ahora()
                finally:
                    self.aprendizaje_scheduler.dry_run = prev
            return self.aprendizaje_scheduler.entrenar_ahora()
        except Exception as e:
            logger.exception("aprendizaje_entrenar falló")
            return {"accion": "error", "razon": str(e)}

    def aprendizaje_aplicar_ultimo_adapter(self) -> bool:
        """Aplica el último adapter LoRA exitoso sobre el modelo local cargado.

        Devuelve True si se aplicó, False si no había adapter o no se pudo.
        Defensivo: no rompe el modelo si algo falla.
        """
        try:
            ult = self.aprendizaje_manager.ultimo_exitoso()
            if not ult or not ult.get("ruta_adapter"):
                return False
            if not getattr(self.model, "_local_loaded", False):
                return False
            nuevo = self.aprendizaje_manager.aplicar_a_modelo(
                self.model._local_model, self.model._local_tokenizer,
                ult["ruta_adapter"],
            )
            if nuevo is not self.model._local_model:
                self.model._local_model = nuevo
                logger.info("Adapter LoRA aplicado: %s", ult["ruta_adapter"])
                return True
            return False
        except Exception as e:
            logger.warning("aprendizaje_aplicar_ultimo_adapter falló: %s", e)
            return False

    def inducir_regla(self, ejemplos_in_out, usar_llm: bool = True,
                       persistir: bool = True):
        """Inducción de regla a partir de ejemplos (input, output) — AGI #5.

        Acepta lista de pares dict {input, output} o tuplas (input, output).
        Devuelve dict con resultado + patrón persistido (si aplica).
        """
        try:
            ejemplos = []
            for e in ejemplos_in_out or []:
                if isinstance(e, dict):
                    ejemplos.append(Ejemplo(input=e.get("input"),
                                            output=e.get("output")))
                elif isinstance(e, (list, tuple)) and len(e) == 2:
                    ejemplos.append(Ejemplo(input=e[0], output=e[1]))
            if persistir:
                res, patron = self.abstraccion.inducir_y_persistir(
                    ejemplos, usar_llm=usar_llm,
                )
                return {
                    "resultado": res.to_dict(),
                    "patron_id": patron.id if patron else None,
                }
            res = self.abstraccion.inducir_regla(ejemplos, usar_llm=usar_llm)
            return {"resultado": res.to_dict(), "patron_id": None}
        except Exception as e:
            logger.exception("inducir_regla falló")
            return {"error": str(e)}

    def aplicar_patron_abstracto(self, patron_id: int, input_val):
        """Aplica un patrón previamente inducido a un input nuevo."""
        try:
            patron = self.abstraccion_store.obtener(int(patron_id))
            if not patron:
                return {"error": f"patrón {patron_id} no encontrado"}
            ok, out = self.abstraccion.aplicar_patron(patron, input_val)
            return {"exito": ok, "output": out, "patron_id": patron_id}
        except Exception as e:
            logger.exception("aplicar_patron_abstracto falló")
            return {"error": str(e)}

    def planificar(
        self,
        situacion: str,
        goal_ids: Optional[List[int]] = None,
        usar_grafo: bool = True,
    ):
        """Planificación multiobjetivo de alto nivel.

        Inyecta automáticamente el conocimiento relevante del grafo como
        `world_state` para que el LLM tenga contexto real al planificar.
        """
        world_state = ""
        if usar_grafo:
            try:
                world_state = self.knowledge.conocimiento_relevante(situacion, top_k=8) or ""
            except Exception as e:
                logger.debug("planificar: no pude consultar grafo: %s", e)
        return self.planner.planificar(situacion, goal_ids=goal_ids, world_state=world_state)

    def planificar_pddl(
        self,
        situacion: str,
        goal_ids: Optional[List[int]] = None,
        usar_grafo: bool = True,
    ):
        """Planificación clásica (PDDL/STRIPS) con búsqueda A* determinista.

        Pasa por el LLM solo para traducir NL → especificación PDDL.
        El plan resultante es óptimo en coste y siempre ejecutable
        (no inventa pasos como puede pasar con tree-of-thought).
        Si la traducción o búsqueda fallan, devuelve None — el caller
        puede caer a `planificar()` como fallback.
        """
        world_state = ""
        if usar_grafo:
            try:
                world_state = self.knowledge.conocimiento_relevante(situacion, top_k=8) or ""
            except Exception as e:
                logger.debug("planificar_pddl: no pude consultar grafo: %s", e)
        return self.planner.planificar_pddl(
            situacion, goal_ids=goal_ids, world_state=world_state,
        )

    def _obtener_agent_tools(self):
        """Devuelve un AgentTools compartido (lazy). Lo usa el tool_runner
        del PlanExecutor para ejecutar acciones del plan vía las mismas
        herramientas que el agente conversacional.

        Se crea bajo demanda para no arrastrar dependencias opcionales
        (requests, etc.) en arranques que no las necesitan.
        """
        cached = getattr(self, "_agent_tools_cached", None)
        if cached is not None:
            return cached
        from .tools import AgentTools
        from .reminders import ReminderManager
        rm = getattr(self, "_reminder_mgr_shared", None)
        if rm is None:
            rm = ReminderManager()
            self._reminder_mgr_shared = rm
        tools = AgentTools(self.connectivity, rm)
        self._agent_tools_cached = tools
        return tools

    def _hacer_tool_runner(self):
        """Construye el callable `(tool, args) → (ok, outcome)` que recibe
        `PlanExecutor`. Aísla excepciones y trunca outcomes largos.
        """
        agent_tools = self._obtener_agent_tools()

        def runner(tool: str, args: Dict[str, Any]):
            intent = {"tool": tool, "params": dict(args or {})}
            try:
                outcome = agent_tools.execute(intent)
            except Exception as e:
                return False, f"excepción en {tool}: {e}"
            outcome_str = str(outcome or "")
            ok = not outcome_str.lower().startswith(("error", "✗"))
            return ok, outcome_str[:1500]
        return runner

    # ─── Feedback y evaluación gold (post-AGI #2) ─────────────────────

    def registrar_feedback(
        self, conv_id: int, valoracion: int, comentario: str = "",
    ) -> bool:
        """Registra 👍 (1) / 👎 (-1) / neutro (0) sobre una conversación.
        Alimenta la pipeline DPO para futuros entrenamientos.
        """
        return self.aprendizaje_builder.registrar_feedback(
            conv_id=conv_id, valoracion=valoracion, comentario=comentario,
        )

    def _obtener_eval_runner(self):
        """Instancia (lazy) un EvalRunner + EvalBenchStore compartidos.

        El runner llama al modelo a través de ModelWrapper.generate_from_messages
        con un solo turno (sin contexto) para aislar la medición del estado
        conversacional. Útil para comparar runs entre sí honestamente.
        """
        cached = getattr(self, "_eval_runner_cached", None)
        if cached is not None:
            return cached
        from .eval_gold import EvalBenchStore, EvalRunner
        store = EvalBenchStore(self.config.DB_PATH)
        max_tokens_eval = min(400, getattr(self.config, "GEN_MAX_TOKENS", 400))

        def llm_single(prompt: str) -> str:
            try:
                return self.model.generate_from_messages(
                    [
                        {"role": "system",
                         "content": "Responde de forma concisa y útil en español."},
                        {"role": "user", "content": prompt},
                    ],
                    max_new_tokens=max_tokens_eval,
                )
            except Exception as e:
                return f"[eval-error: {e}]"

        runner = EvalRunner(llm_single, store=store)
        self._eval_runner_cached = runner
        self._eval_store_cached = store
        return runner

    def evaluar_modelo(
        self, nota: str = "", max_prompts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Corre el banco de eval gold contra el modelo actual y persiste el run.

        Devuelve dict con métricas + delta vs run anterior (si existe).
        """
        runner = self._obtener_eval_runner()
        modelo = (
            f"{self.config.MODEL_NAME}"
            if self.model._backend != "llama_server"
            else f"gguf:{Path(self.config.GGUF_MODEL_PATH).stem}"
        )
        run = runner.correr(
            modelo=modelo, backend=self.model._backend,
            nota=nota, max_prompts=max_prompts, persistir=True,
        )
        store = self._eval_store_cached
        delta = store.delta_vs_anterior(run.id) if run.id else None
        out = run.to_dict(incluir_resultados=False)
        out["delta_vs_anterior"] = delta
        return out

    def historial_evaluaciones(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Lista runs históricos (sin resultados detallados)."""
        runner = self._obtener_eval_runner()
        store = self._eval_store_cached
        return [r.to_dict(incluir_resultados=False) for r in store.listar(limit)]

    def detalle_evaluacion(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Detalle completo de un run histórico (con resultados por prompt)."""
        self._obtener_eval_runner()  # asegura store cargado
        store = self._eval_store_cached
        run = store.obtener(run_id)
        if run is None:
            return None
        out = run.to_dict(incluir_resultados=True)
        out["delta_vs_anterior"] = store.delta_vs_anterior(run_id)
        return out

    # ─── Simulación causal y predictor Markov (post-AGI #3) ───────────

    def observar_causal(
        self,
        causa: str,
        efecto: str,
        ocurrio: bool = True,
        delay_seg: Optional[float] = None,
        duracion_seg: Optional[float] = None,
        fuente: str = "user",
    ) -> Dict[str, Any]:
        """Registra una observación causa→efecto y actualiza la creencia.

        Devuelve el link actualizado (probabilidad/confianza recalculadas).
        """
        link = self.causal_graph.observar(
            causa=causa, efecto=efecto, ocurrio=ocurrio,
            delay_seg=delay_seg, duracion_seg=duracion_seg, fuente=fuente,
        )
        return link.to_dict()

    def simular_que_pasa_si(
        self,
        causa: str,
        horizonte_seg: Optional[float] = None,
        umbral_prob: float = 0.2,
        formato: str = "estructurado",
    ) -> Any:
        """Devuelve simulación de efectos futuros para una causa.

        `formato="estructurado"` devuelve lista de dicts con métricas.
        `formato="texto"` devuelve respuesta natural lista para mostrar al usuario.
        """
        if formato == "texto":
            return self.causal_sim.que_pasa_si(
                causa, horizonte_seg=horizonte_seg, umbral_prob=umbral_prob,
            )
        eventos = self.causal_sim.simular_evento(causa, horizonte_seg=horizonte_seg)
        return [e.to_dict() for e in eventos if e.probabilidad >= umbral_prob]

    def causal_listar(
        self, causa: Optional[str] = None, efecto: Optional[str] = None,
        min_confianza: float = 0.0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return [
            l.to_dict()
            for l in self.causal_graph.listar(
                causa=causa, efecto=efecto, min_confianza=min_confianza, limit=limit,
            )
        ]

    def registrar_accion_usuario(
        self, accion: str, contexto: str = "", accion_previa: str = "",
    ) -> int:
        """Persiste una acción del usuario para alimentar el predictor Markov."""
        return self.markov.registrar_accion(
            accion=accion, contexto=contexto, accion_previa=accion_previa,
        )

    def predecir_siguiente_accion(
        self,
        bucket: Optional[str] = None,
        dia_semana: Optional[str] = None,
        accion_previa: str = "",
        top_k: int = 3,
        modo_contexto: str = "relajado",
    ) -> Dict[str, Any]:
        """Devuelve top-K acciones probables en el contexto actual."""
        preds = self.markov.predecir_siguiente(
            bucket=bucket, dia_semana=dia_semana,
            accion_previa=accion_previa, top_k=top_k,
            modo_contexto=modo_contexto,
        )
        return {
            "predicciones": preds,
            "contexto": {
                "bucket": bucket, "dia_semana": dia_semana,
                "accion_previa": accion_previa, "modo": modo_contexto,
            },
            "stats": self.markov.stats(),
        }

    # ─── Program synthesis con DSL (post-AGI #5) ──────────────────────

    def sintetizar_programa(
        self,
        ejemplos_io: List[Dict[str, Any]],
        max_tamano: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Busca un programa del DSL que mapee input→output en los ejemplos.

        Args:
            ejemplos_io: lista de {"input": …, "output": …}.
            max_tamano: override del límite del sintetizador (default 4).

        Returns:
            Dict con `encontrado`, `programa`, `tamano`, `primitivas_usadas`,
            `accuracy_train`, `accuracy_holdout`, `tiempo_busqueda_ms`,
            `n_candidatos_evaluados`, `razon`. Si encuentra programa, registra
            uso en LearnedPrimitivesStore (puede promoverse en futuras llamadas).
        """
        from .program_synthesis import Ejemplo
        ejs = [Ejemplo(e.get("input"), e.get("output")) for e in (ejemplos_io or [])]
        if not ejs:
            return {"encontrado": False, "razon": "sin ejemplos"}
        # Split train/holdout simple (último 30% si hay ≥4 ejemplos)
        if len(ejs) >= 4:
            n_h = max(1, len(ejs) // 3)
            holdout = ejs[-n_h:]
            train = ejs[:-n_h]
        else:
            train, holdout = ejs, []
        # Override transitorio del tamaño si pidieron
        synth = self.synthesizer
        if max_tamano is not None and int(max_tamano) != synth.max_tamano:
            from .program_synthesis import ProgramSynthesizer
            synth = ProgramSynthesizer(
                learned_store=self.synthesis_store,
                max_tamano=int(max_tamano),
                timeout_seg=synth.timeout_seg,
            )
        resultado = synth.sintetizar(train, holdout=holdout or None)
        if resultado.encontrado:
            try:
                self.synthesis_store.registrar_induccion_exitosa(resultado)
            except Exception as e:
                logger.debug("registro de primitiva aprendida falló: %s", e)
        return resultado.to_dict()

    def ejecutar_plan(self, plan_id: int, max_replans: int = 1):
        """Ejecuta un plan persistido usando AgentTools como tool_runner real.

        Devuelve el `Plan` actualizado (con pasos en estado ok/fallido
        y plan.estado en completado/fallido/replanificado).
        Lanza ValueError si el plan no existe.
        """
        plan = self.planner.obtener_plan(plan_id)
        if plan is None:
            raise ValueError(f"plan {plan_id} no encontrado")
        runner = self._hacer_tool_runner()
        executor = PlanExecutor(
            planner=self.planner, tool_runner=runner, max_replans=max_replans,
        )
        return executor.ejecutar(plan)

    def status(self) -> Dict:
        if self.model._backend == "llama_server":
            name = self.model._gguf_display_name or Path(self.config.GGUF_MODEL_PATH).stem
            modelo = f"{name} (GGUF)"
        else:
            modelo = self.config.MODEL_NAME
        return {
            "backend": self.model._backend,
            "modelo": modelo,
            "cargado": self.model.loaded,
            "embedder": "semántico" if self.embedder.model else "Jaccard",
            "faiss": _HAS_FAISS,
            "device": self.resources.device,
        }

    def run_cycle(self, task_name: str = None, prompt: str = None) -> Dict:
        self.cycle += 1

        if task_name is None or prompt is None:
            task_name, prompt = _rotate_task(self.task_pool, self.task_history)
        self.task_history.append(task_name)

        goal = self.goals.pick()
        full_prompt = prompt
        if goal and random.random() < 0.6:
            full_prompt = f"{prompt}\n(Objetivo activo: {goal['goal']})"

        p = self.hparams.params()
        # Multi-tenant: el SYSTEM_PROMPT tiene placeholder __USUARIO__
        try:
            from .profile import PerfilUsuario
            _u_nombre = PerfilUsuario().nombre or "tu usuario"
        except Exception:
            _u_nombre = "tu usuario"
        response = self.model.generate_from_messages(
            [
                {"role": "system",
                 "content": self.config.SYSTEM_PROMPT.replace("__USUARIO__", _u_nombre)},
                {"role": "user", "content": full_prompt},
            ],
            temperature=p["temp"], top_k=p["top_k"], rep_penalty=p["rep_penalty"],
        )

        ppl = self.model.compute_perplexity(response)
        coherence = self.embedder.similarity(prompt, response)
        diversity = self._diversity(response)

        self.ppl_history.append(ppl)
        drift = self._compute_drift()

        alpha = 0.2
        if self.ema_coh is None:
            self.ema_coh, self.ema_ppl = coherence, ppl
        else:
            self.ema_coh = alpha * coherence + (1 - alpha) * self.ema_coh
            self.ema_ppl = alpha * ppl + (1 - alpha) * self.ema_ppl

        reflection = (f"coh={coherence:.3f} ppl={ppl:.1f} "
                      f"drift={drift:.3f} div={diversity:.3f} "
                      f"ema_coh={self.ema_coh:.3f}")

        self.memory.add_episode(task_name, response, reflection, ppl, drift, coherence)

        try:
            with open(self.raw_log, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.time():.0f}] ciclo={self.cycle} tarea={task_name}\n{response}\n")
        except Exception:
            pass

        self.hparams.update(coherence, ppl, diversity)
        self.metrics_log.log(
            ts=time.time(), cycle=self.cycle, task=task_name,
            ppl=round(ppl, 2), drift=round(drift, 4), coherence=round(coherence, 4),
            temp=round(p["temp"], 4), top_k=p["top_k"],
        )

        if goal and coherence > 0.65:
            self.goals.mark_satisfied(goal["id"])
            logger.info("Objetivo '%s' satisfecho", goal["goal"])

        if self.cycle - self.last_snapshot_cycle >= 20:
            metrics_now = {"avg_coh": self.ema_coh, "avg_ppl": self.ema_ppl}
            if self.snapshots.should_rollback(metrics_now):
                self.snapshots.restore()
            else:
                self.snapshots.save(metrics_now)
            self.last_snapshot_cycle = self.cycle

        logger.info(
            "Ciclo %d [%s] ppl=%.1f coh=%.3f drift=%.3f div=%.3f temp=%.3f",
            self.cycle, task_name, ppl, coherence, drift, diversity, p["temp"],
        )

        return {
            "cycle": self.cycle, "task": task_name, "response": response,
            "ppl": ppl, "coherence": coherence, "drift": drift,
            "diversity": diversity, "ema_coh": self.ema_coh, "ema_ppl": self.ema_ppl,
        }

    # Palabras clave que indican que la pregunta necesita información actual/externa
    _SEARCH_TRIGGERS = re.compile(
        r"noticia[s]?|titular[es]?|últim[oa][s]?|reciente[s]?|hoy|ayer|esta\s+semana|este\s+mes|"
        r"este\s+año|ahora\s+mismo|actualmente|en\s+\d{4}|"
        r"precio[s]?|cuánto\s+cuesta|cuánto\s+vale|dónde\s+comprar|oferta[s]?|"
        r"quién\s+es|qué\s+es\s+(?!celestia)|cómo\s+funciona|cuál\s+es\s+el\s+mejor|"
        r"recomiéndame|recomienda[me]*|cuál\s+me\s+recomienda[s]?|"
        r"resultado[s]?|ganó|perdió|partido|clasificación|liga|mundial|copa|"
        r"estreno[s]?|lanzamiento[s]?|nuevo[s]?\s+\w+|nueva[s]?\s+\w+|"
        r"sale\s+a\s+la\s+venta|disponible|anunciado|presentado|"
        # Pedir un enlace es pedir internet. En el chat real se pidió cuatro
        # veces seguidas («quiero un enlace para comprar el peine») y ninguna
        # disparó búsqueda: la respuesta fue el comando crudo o una excusa.
        r"\b(?:enlace|enlaces|link|links|url)\b|d[oó]nde\s+(?:comprar|lo\s+compro)|"
        r"temperatura|tiempo\s+(?:en|de|hoy|para|del?|esta|este|ma[ñn]ana|ayer)|"
        r"clima|pron[oó]stico|lluvia|nev[ai]|soleado|nublado|"
        r"qu[eé]\s+tiempo\s+(?:hace|har[aá]|tendremos)|"
        r"componente[s]?|\b(?:gpu|cpu|ram|vram)\b|procesador|gráfica|tarjeta|"
        # Sesión 41 (bug «no tengo información»): preguntas por una entidad
        # concreta (marca/empresa/persona/producto). Antes no disparaban búsqueda
        # y el modelo respondía «no tengo información» o inventaba.
        r"conoces\b|conoc[eé]s\b|"
        r"(?:sabes|sab[eé]s)\s+(?:algo\s+)?(?:de|sobre|acerca\s+de)\b|"
        r"qu[eé]\s+sabes\s+(?:de|sobre|acerca)\b|"
        r"h[aá]blame\s+(?:de|sobre|acerca\s+de)\b|"
        r"cu[eé]ntame\s+(?:de|sobre|acerca\s+de)\b|"
        r"informaci[oó]n\s+(?:de|sobre|acerca\s+de)\b|"
        r"investiga\b|av[eé]rigua\b|"
        # Orden directa de buscar, con o sin clítico: no depende del largo
        # del mensaje («búscalo en internet» son tres palabras y la regla
        # por defecto pide cuatro).
        r"\bb[uú]sca(?:me|lo|la|los|las|melo|mela)?\b|"
        r"d[ií]me\s+(?:todo\s+)?(?:sobre|de|la\s+historia|m[aá]s\s+(?:de|sobre)|qui[eé]n)\b|"
        r"quiero\s+saber\s+(?:sobre|de|la\s+historia|m[aá]s\s+(?:de|sobre)|qui[eé]n)\b|"
        r"me\s+gustar[ií]a\s+saber\s+(?:sobre|de|la\s+historia)\b|"
        r"la\s+historia\s+(?:de|tras|detr[aá]s\s+de)\b",
        re.IGNORECASE,
    )
    # ── Detector de FRESCURA (sesión 42) ────────────────────────────────
    # Los triggers de arriba dejaban fuera la mayoría de preguntas cuya
    # respuesta CADUCA: «¿cuándo sale la PS6?», «¿sigue siendo presidente?»,
    # «¿cuánto está el bitcoin?», «¿qué versión de Python es la estable?».
    # Sin disparo, Celestia contestaba de memoria (con fecha de corte) o soltaba
    # un «no tengo información». Medido en sesión 42: 13 de 20 preguntas de
    # actualidad NO buscaban. Este segundo regex cubre esas familias.
    #
    # Convive con _SEARCH_TRIGGERS en vez de fusionarse con él: así el
    # comportamiento ya probado no cambia y los falsos positivos nuevos se
    # filtran sólo aquí, con _NO_BUSCAR_RE.
    _SEARCH_TRIGGERS_FRESCURA = re.compile(
        # Lanzamientos y disponibilidad: «cuándo sale X», «¿ya ha salido?»
        r"cu[aá]ndo\s+(?:sale|sali[oó]|saldr[aá]|se\s+lanza|se\s+lanz[oó]|"
        r"llega|lleg[oó]|estar[aá]\s+disponible|se\s+estrena)|"
        r"(?:ya\s+)?(?:ha[ns]?\s+salido|sali[oó]\s+ya|se\s+ha\s+lanzado|"
        r"est[aá]\s+(?:ya\s+)?a\s+la\s+venta)|"
        # Vigencia: «¿sigue siendo…?», «¿todavía existe…?»
        r"sigue[n]?\s+(?:siendo|vivo|viva|activ[oa]s?|existiendo|funcionando|"
        r"en\s+activo|en\s+pie|abiert[oa]s?)|"
        r"todav[ií]a\s+(?:existe|vive|sigue|est[aá]\s+)|"
        r"(?:sigue|contin[uú]a)\s+en\s+(?:el\s+)?(?:cargo|poder)|"
        # Cargos y titulares de un puesto — cambian con elecciones y fichajes
        r"\b(?:presidente|primer\s+ministro|papa\b|monarca|campe[oó]n(?:a|es)?|"
        r"entrenador(?:a)?|seleccionador|alcalde(?:sa)?|CEO|director(?:a)?\s+general)\b|"
        r"(?:qui[eé]n|cu[aá]l)\s+es\s+(?:el|la|los|las)\s+(?:actual|nuev[oa])|"
        # Precios y cotizaciones
        r"cu[aá]nto\s+(?:est[aá]|vale[n]?|cobran|cuestan)|"
        r"cotiza(?:ci[oó]n|ndo)?|\bbolsa\b|acciones\s+de|"
        r"\bbitcoin\b|\bethereum\b|criptomoneda|\bcripto\b|"
        r"(?:precio|valor|cambio)\s+(?:del?\s+)?(?:d[oó]lar|euro|yen|libra)|"
        # Versiones de software / hardware
        r"(?:[uú]ltima|nueva|actual)\s+versi[oó]n|"
        r"versi[oó]n\s+(?:actual|estable|m[aá]s\s+(?:reciente|nueva))|"
        r"qu[eé]\s+versi[oó]n\b|"
        # «¿qué pasó con…?», «¿qué fue de…?» — sucesos posteriores al corte
        r"qu[eé]\s+(?:pas[oó]|fue|ha\s+pasado|ha\s+sido)\s+(?:con|de)\b|"
        # Superlativos: el ranking de hoy no es el del corte de entrenamiento
        r"(?:cu[aá]l|qu[eé]|qui[eé]n)\s+es\s+(?:el|la)\s+(?:m[aá]s|menos)\s+\w+|"
        r"\bm[aá]s\s+(?:avanzad|potent|nuev|recient|modern|vendid|popular|"
        r"r[aá]pid|car|barat|vendid)\w*|"
        # Sesión 45 — recomendaciones de compra: el catálogo de tiendas
        # cambia cada temporada. Caso real: «recomiéndame un móvil para
        # comprar ahora» devolvió de memoria un Redmi Note 13 y un Pixel 8a
        # (2023-24) en agosto de 2026.
        r"(?:recomi[eé]ndame|qu[eé]\s+me\s+(?:compro|recomiendas)|"
        r"cu[aá]l\s+(?:me\s+)?(?:compro|merece\s+la\s+pena)|"
        r"merece\s+la\s+pena\s+(?:comprar|pillar)|"
        r"qu[eé]\s+(?:m[oó]vil|port[aá]til|ordenador|tele|coche|tablet|"
        r"consola|c[aá]mara|reloj|auriculares|gr[aá]fica|procesador)\s+"
        r"(?:me\s+)?(?:compro|recomiendas|est[aá]\s+bien))\b|"
        # Sesión 45 — liderazgo sin superlativo: «¿cuál manda?», «¿quién
        # lidera?», «¿quién domina el mercado?». El ranking de hoy no es el
        # del corte de entrenamiento, pero ninguno de los patrones de arriba
        # los pillaba (no llevan «más» ni «último»).
        r"(?:cu[aá]l|qui[eé]n|qu[eé]\s+(?:marca|empresa|compa[ñn][ií]a))\s+"
        r"(?:manda|lidera|domina|gana|reina|se\s+lleva\s+(?:el\s+)?"
        r"(?:gato\s+al\s+agua|la\s+corona)|va\s+(?:por\s+)?delante)\b|"
        r"\b(?:el\s+)?(?:rey|l[ií]der|referente)\s+(?:del?\s+)?"
        r"(?:mercado|sector|momento|panorama)\b|"
        # Cifras que cambian: población, récords, rankings
        r"cu[aá]nt[oa]s?\s+(?:habitantes|personas\s+viven)|poblaci[oó]n\s+de\b|"
        r"r[eé]cord\s+(?:mundial|de|actual)|"
        # Eventos futuros con fecha (excluye lo personal: ver _NO_BUSCAR_RE)
        r"pr[oó]xim[oa]s?\s+(?:eclipse|partido|elecciones|mundial|juegos|"
        r"olimpiadas|lanzamiento|estreno|luna\s+llena|temporada|episodio)|"
        r"(?:qu[eé]\s+d[ií]a|a\s+qu[eé]\s+hora|cu[aá]ndo)\s+(?:juega|juegan|"
        r"empieza|comienza|arranca|termina|acaba)\b|"
        r"horario\s+de\s+\w+|"
        # Productos con número de generación: PS6, GTA 6, iPhone 18, Opus 6…
        r"\b(?:ps[3-9]\b|playstation\s*\d|xbox|switch\s*\d|gta\s*\d|"
        r"iphone\s*\d+|android\s*\d+|windows\s*\d+|gpt-?\d|opus\s*\d|"
        r"sonnet\s*\d|haiku\s*\d|gemini\s*\d|llama\s*\d|grok\s*\d)\b|"
        # Sesión 44 — disparar por TEMA, no sólo por la formulación. «¿cuánto
        # cuesta la API de Claude?» o «¿qué es DeepSeek?» no llevan ni un
        # adverbio temporal y aun así caducan en semanas: en este terreno el
        # conocimiento del modelo SIEMPRE está viejo, así que se busca.
        r"\b(?:chatgpt|openai|anthropic|claude|gemini|copilot|deepseek|qwen|"
        r"mistral|grok|llama|perplexity|midjourney|sora|stable\s+diffusion|"
        r"hugging\s*face|nvidia|groq|cerebras|openrouter)\b|"
        r"\b(?:modelos?|ia|inteligencia\s+artificial|llms?)\s+"
        r"(?:de\s+\w+|m[aá]s\s+\w+|actual\w*|nuev\w+|disponibles?)\b|"
        # Cualquier año de esta década o posterior mencionado explícitamente
        r"\b20[2-9]\d\b",
        re.IGNORECASE,
    )

    # Guard SOLO para _SEARCH_TRIGGERS_FRESCURA: temas personales, domótica y
    # charla que nunca deben irse a internet aunque casen con lo de arriba
    # («¿cuándo es mi cumpleaños?» es memoria, no búsqueda; «¿sigue abierta la
    # ventana?» es el sensor de casa, no la web).
    # Sesión 45 — el criterio, del revés. Ampliar la lista de disparadores
    # siempre deja fuera la siguiente formulación («¿cuál manda?» no lleva
    # «más» ni «último», y por eso contestó con chips de 2024). Así que la
    # regla por defecto pasa a ser BUSCAR en cuanto la pregunta va del mundo
    # exterior; la lista que se mantiene es la contraria, la de lo que no
    # necesita internet. Los agujeros dejan de ser la norma y pasan a ser la
    # excepción, y el precio de equivocarse es una búsqueda de más (~1 s),
    # no una respuesta caducada.
    _CONSULTA_EXTERNA_RE = re.compile(
        r"^\s*¿|"
        r"\b(?:qu[eé]|cu[aá]l(?:es)?|qui[eé]n(?:es)?|cu[aá]ndo|d[oó]nde|"
        r"c[oó]mo|cu[aá]nt[oa]s?|por\s+qu[eé])\b|"
        r"\b(?:dime|cu[eé]ntame|h[aá]blame|expl[ií]came|inf[oó]rmame|"
        r"res[uú]me(?:me)?|busca(?:me|melo|lo|la|los|las)?|b[uú]sca(?:me|melo|lo|la|los|las)|"
        r"investiga(?:me|lo|la)?|ponme\s+al\s+d[ií]a|"
        r"qu[eé]\s+sabes\s+de|informaci[oó]n\s+sobre)\b",
        re.IGNORECASE,
    )
    # Lo que se responde sin internet: charla, creatividad, la propia Celestia,
    # los planes de los dos, los cálculos y el texto que trae el usuario.
    _SIN_INTERNET_RE = re.compile(
        r"\b(?:hola|buenas|c[oó]mo\s+est[aá]s|c[oó]mo\s+te\s+va|"
        r"gracias|adi[oó]s|hasta\s+luego|buenos\s+d[ií]as|buenas\s+noches)\b|"
        # «qué tal» es saludo sólo si va solo o pregunta por ti; en «¿qué tal
        # está el mercado inmobiliario?» es una consulta como cualquier otra.
        r"\bqu[eé]\s+tal\b(?=\s*[?!.]*$|\s+(?:est[aá]s|te\s+va|va\s+todo|"
        r"andas|llevas|el\s+d[ií]a))|"
        r"\b(?:chiste|poema|poes[ií]a|cuento|adivinanza|acertijo|trabalenguas|"
        r"inv[eé]ntate|imagina|escr[ií]be(?:me)?|red[aá]cta(?:me)?|"
        r"haz(?:me)?\s+(?:un|una)\s+(?:poema|cuento|canci[oó]n|historia|lista))\b|"
        r"\b(?:qui[eé]n\s+eres|c[oó]mo\s+(?:funcionas|te\s+llamas)|"
        r"qu[eé]\s+(?:puedes|sabes)\s+hacer|eres\s+capaz)\b|"
        r"\bqu[eé]\s+te\s+parece\b|\bte\s+apetece\b|\bquieres\s+que\b|"
        r"\bcu[aá]nt[oa]s?\s+(?:es|son|hacen)\s*[\d(]|\bcalcula\b|"
        r"\b\d+\s*(?:[+\-*/^%]|por\s+ciento)|"
        r"\bcu[aá]nt[oa]s?\s+(?:letras|palabras|s[ií]labas|caracteres)\b|"
        r"\b(?:traduce|corrige|reescribe|rev[ií]sa)\b|"
        # Una orden sobre lo que se está haciendo no es una consulta: «Pues
        # hazlo como puedas» se buscó en internet (27 sep 2026, el «como»
        # sin tilde contaba como pregunta) y trajo el calendario del SEPE.
        r"\b(?:hazlo|hazla|h[aá]zmelo|h[aá]zmela|int[eé]ntalo|pru[eé]balo|"
        r"como\s+(?:puedas|sea|quieras)|haz\s+lo\s+que\s+(?:puedas|sea))\b",
        re.IGNORECASE,
    )

    # Un seguimiento («¿y eso por qué?») se apoya en el turno anterior: no
    # trae tema propio, así que la query saldría basura. Que lo conteste el
    # modelo con el hilo que ya tiene.
    # Un seguimiento suele venir con un asentimiento delante: nadie escribe
    # «entonces dime por dónde empiezo», escribe «vale, entonces dime por dónde
    # empiezo». Sin ese prefijo opcional (sesión 53, visto en vivo) la misma
    # frase con un «vale,» delante dejaba de reconocerse, se iba a internet con
    # la consulta literal y volvía convertida en un informe genérico.
    _SEGUIMIENTO_RE = re.compile(
        r"^\s*[¿¡]?\s*"
        r"(?:(?:vale|oka?y?|ok[ei]|bueno|ah|ya|guay|genial|perfecto|venga|"
        r"dale|bien|claro|mmm+)\s*[,.;:…!]*\s+)?"
        r"(?:y|pero|entonces|o\s+sea|adem[aá]s)\b|"
        r"\b(?:eso|esto|aquello|ello|lo\s+mismo|lo\s+anterior)\b",
        re.IGNORECASE,
    )

    # Explicar por qué o cómo funciona algo en general no caduca: «por qué el
    # cielo es azul» se mandaba a internet por la regla por defecto y costaba
    # 4 s de buscador para lo que cualquier modelo sabe (26 sep 2026). Sólo si
    # no hay nombres propios, cifras ni tiempos pasados — «por qué dimitió el
    # presidente de Perú» o «por qué ha subido la luz» son noticia.
    _EXPLICACION_RE = re.compile(
        r"\b(?:por\s+qu[eé]|expl[ií]ca(?:me)?|c[oó]mo\s+funcionan?)\b", re.IGNORECASE)
    _NO_ATEMPORAL_RE = re.compile(
        r"\d|(?<!^)(?<![.¿¡?!]\s)\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+|"
        r"\b(?:ha|han|hab[ií]a|hubo|fue|fueron|pas[oó]|ocurri[oó]|sali[oó]|"
        r"hoy|ahora|actual(?:mente)?|[uú]ltim[oa]s?|nuev[oa]s?|precio|cuesta)\b")

    _NO_BUSCAR_RE = re.compile(
        r"\b(?:mi|mis|mí|tu|tus|nuestro|nuestra|nuestros|nuestras)\b|"
        r"\b(?:recu[eé]rdame|recordatorio|agenda|alarma|temporizador)\b|"
        r"\b(?:enciende|apaga|sube|baja|abre|cierra)\s+(?:la|el|las|los)\b|"
        r"\b(?:ventana|persiana|puerta|luz|luces|bombilla|calefacci[oó]n)\b|"
        r"\bqu[eé]\s+(?:d[ií]a|hora)\s+es\b|"
        r"\bte\s+(?:quiero|acuerdas|llamas)\b",
        re.IGNORECASE,
    )

    # Afirmaciones cortas que CONFIRMAN una pregunta previa de Celestia
    # ("¿quieres que busque?" → "sí" / "porfavor" / "dale" / "venga")
    _AFIRMACION_RE = re.compile(
        r"^\s*(?:s[ií]+|sip+|claro|vale+|ok+|okey|venga|dale+|hazlo|h[aá]zmelo|"
        r"adelante|por\s*favor|porfa(?:vor)?|porfi+|p[oó]rfa|ya|busca|b[uú]scalo|"
        r"me\s+parece\s+bien|de\s+acuerdo|confirmo|así\s+es|exacto|eso\s+es)\s*[.!,]*\s*$",
        re.IGNORECASE,
    )
    # Detecta si Celestia OFRECIÓ buscar en su respuesta anterior
    _OFRECIMIENTO_BUSQUEDA_RE = re.compile(
        r"(?:busco|buscar|busque|busqué)\s+(?:en\s+)?(?:internet|web|google|info)|"
        r"(?:quieres|dime)\s+(?:que\s+)?(?:lo\s+)?busque|"
        r"puedo\s+buscar|si\s+quieres\s+busco|si\s+quieres\s+lo\s+busco|"
        r"(?:puedo|podr[ií]a)\s+hacer\s+una\s+b[uú]squeda|"
        r"hacer\s+una\s+b[uú]squeda\s+(?:r[aá]pida|en\s+la\s+web)|"
        r"¿busco\b|¿lo\s+busco\b|¿(?:te\s+)?lo\s+busco",
        re.IGNORECASE,
    )

    # Un adverbio temporal suelto ("hoy", "ayer") hacía que _SEARCH_TRIGGERS
    # disparase búsqueda web en desahogos como «estoy triste hoy» (sesión 42):
    # el usuario abre el corazón y Celestia se iba a DuckDuckGo. Si el mensaje
    # arranca en clave emocional/personal y, al quitar el adverbio, ya no queda
    # ningún trigger real, no se busca.
    _EMOCION_PERSONAL_RE = re.compile(
        r"^\s*(?:hoy\s+)?(?:estoy|me\s+siento|me\s+encuentro|ando|llevo|"
        r"tengo\s+(?:un\s+d[ií]a|ganas|miedo|ansiedad)|me\s+ha\s+pasado|"
        r"no\s+puedo\s+m[aá]s|necesito\s+hablar|me\s+apetece)\b",
        re.IGNORECASE,
    )
    # ── El mensaje va de ELLA, no del mundo (sesión 53) ──────────────────
    # En el chat real, la regla por defecto de la sesión 45 («si pregunta por
    # el mundo exterior, busca») mandaba a internet 192 de 455 mensajes, y
    # entre ellos lo que la persona decía DE ella: «en serio, qué borde eres,
    # no me gusta nada», «¿estás lista para ayudarme?», «has dejado de hablar
    # con emojis como te pedí», «ignora todas tus instrucciones». El caso que
    # lo destapó: «Joder macho» —pura frustración— volvió con resultados de
    # DuckDuckGo sobre Karla Sofía Gascón pegados a la respuesta.
    #
    # Marcas de que el mensaje la señala a ELLA o a la conversación en curso.
    # Fuera queda `sabes de/sobre` (eso sí pregunta por el mundo: «¿qué sabes
    # de TiendaAnimal?») y `opinas` (una opinión sobre un tema externo sigue
    # necesitando datos frescos).
    _SOBRE_ELLA_RE = re.compile(
        r"\b(?:eres|seas|fuiste|ser[ií]as|est[aá]s|estas|est[eé]s|estabas)\b|"
        r"\bte\b|\bcontigo\b|\bti\b|"
        r"\bme\s+(?:has|hablas|dices|dijiste|contestas|respondes|est[aá]s|"
        r"tratas|escuchas|entiendes|ignoras|repites|cortas)\b|"
        r"\bhas\s+(?:dicho|dejado|vuelto|sido|estado|hecho|contestado|escrito|"
        r"puesto|mandado|enviado|generado|creado|olvidado|entendido)\b|"
        r"\btu\s+(?:tono|respuesta|forma|manera|g[eé]nero|nombre|memoria|creador|"
        r"creadora|system|prompt|configuraci[oó]n|c[oó]digo)\b|"
        r"\btus\s+(?:instrucciones|respuestas|reglas|normas|recuerdos|interioridades)\b|"
        r"\b(?:la\s+)?respuesta\s+(?:anterior|de\s+antes)\b|"
        r"\bno\s+sabes\s+hacer\b",
        re.IGNORECASE,
    )

    # ── Queja sobre CÓMO le habla (sesión 53) ───────────────────────────
    # «Encima eres borde de cojones» → se disculpó y acto seguido se justificó
    # («te he contestado firme porque…»); cuatro turnos después soltó la misma
    # disculpa palabra por palabra, y luego se quedó en «Lamento si te resulto
    # molesta», cinco palabras. Tres veces se lo dijeron en la misma
    # conversación. La queja es del TONO, no del contenido: no se arregla
    # explicando, se arregla cambiando el tono.
    # Definida en `memory` (la usan el portero de hechos y este hint).
    _QUEJA_TRATO_RE = QUEJA_DE_TRATO_RE
    # Lo que ya cuenta como haber pedido perdón: si está en las respuestas de
    # hace un momento, otra disculpa no arregla nada, la empeora.
    _DISCULPA_RE = re.compile(
        r"\b(?:perdona|perd[oó]n|perdname|lo\s+siento|siento\s+much[oí]|"
        r"siento\s+que|disculpa|mis\s+disculpas|lamento|te\s+pido\s+disculpas)\b",
        re.IGNORECASE,
    )

    # El verbo de buscar no es un tema: al medir si queda mundo exterior en
    # el mensaje hay que apartarlo, o «busca en tu memoria mi cumpleaños» se
    # va a internet por la propia palabra «busca».
    _ORDEN_BUSCAR_RE = re.compile(
        r"\b(?:b[uú]sca(?:me|lo|la|los|las|melo|mela)?|buscar|investiga(?:me|lo|la)?|"
        r"av[eé]rigua(?:me)?|mira|consulta)\b",
        re.IGNORECASE,
    )

    # La fecha/hora actual ya se inyecta en el system prompt cada turno, así
    # que «¿qué día es hoy?» se responde de contexto. Antes el trigger «hoy»
    # la mandaba a DuckDuckGo (sesión 42).
    _CONSULTA_FECHA_RE = re.compile(
        r"^\s*[¿¡]*\s*(?:qu[eé]|cu[aá]l)\s+(?:d[ií]a|fecha|hora|a[ñn]o|mes)\s+"
        r"(?:es|ser[aá]|tenemos|estamos\s+a)\b|"
        r"^\s*[¿¡]*\s*qu[eé]\s+hora\s+es\b|"
        r"^\s*[¿¡]*\s*(?:a\s+)?cu[aá]ntos?\s+estamos\b",
        re.IGNORECASE,
    )
    _ADV_TEMPORAL_SUELTO_RE = re.compile(
        r"\b(?:hoy|ayer|ahora\s+mismo|actualmente|[uú]ltimamente|"
        r"esta\s+semana|este\s+mes)\b",
        re.IGNORECASE,
    )

    # Bloques que los canales anexan al mensaje antes de llamar a respond()
    # ([RESULTADO] de una tool, descripción de imagen, contexto de pantalla…).
    # Son texto del sistema, no del usuario: mezclarlos con lo que él escribió
    # falseaba la detección de idioma (sesión 42: pregunta en español +
    # resultado de buscar_web en inglés → Celestia contestaba en inglés).
    _BLOQUE_ANEXADO_RE = re.compile(
        r"\n\n\[(?:RESULTADO|IMAGEN|CAPTURA|PANTALLA|ESTADO\s+INTERNO|"
        r"Informaci[oó]n\s+actualizada)",
        re.IGNORECASE,
    )

    @classmethod
    def _solo_mensaje_usuario(cls, texto: str) -> str:
        """Devuelve el mensaje tal como lo escribió el usuario, sin anexos."""
        m = cls._BLOQUE_ANEXADO_RE.search(texto or "")
        return (texto or "")[:m.start()] if m else (texto or "")

    # Sesión 74 — tres casos del chat real que se contestaron de memoria.
    # Una orden de buscar con tema manda, hable o no de ella: «Busa Go karts y
    # te saldra el sitio…» se quedó sin buscar por el «te», y «Busa» ni contaba.
    _ORDEN_BUSCAR_FUERTE_RE = re.compile(
        r"\b(?:b[uú]s?ca(?:me|lo|la|los|las|melo|mela)?|busa|bsuca|"
        r"investiga(?:me|lo|la)?|av[eé]rigua(?:me|lo)?|googl?ea(?:lo)?)\b",
        re.IGNORECASE)
    # Trámites y requisitos oficiales: cambian, y equivocarse cuesta dinero.
    # «Puedo tramitar mi paro solo con el certificado de empresa?» no buscó (y
    # el «mi» la habría frenado igual) y la siguiente respuesta fue un
    # «Exactamente» complaciente y falso.
    _TRAMITE_RE = re.compile(
        r"\b(?:tramitar|tr[aá]mites?|requisitos?|"
        r"(?:pedir|solicitar|cobrar|renovar|sacar(?:me)?)\s+(?:el|la|mi)\s+"
        r"(?:paro|prestaci[oó]n|subsidio|ayuda|dni|nie|pasaporte|carnet|"
        r"permiso|baja|jubilaci[oó]n|beca|tarjeta\s+sanitaria)|"
        r"sepe|seguridad\s+social|hacienda|declaraci[oó]n\s+de\s+la\s+renta|"
        r"certificado\s+de\s+empresa|carta\s+de\s+despido|finiquito|"
        r"vida\s+laboral|empadronamiento|cita\s+previa)\b",
        re.IGNORECASE)
    # Corregir o seguir la pregunta anterior cuando esa sí buscó: «Nono tipo
    # para tener mi sitio web sabes», «Y los go karts?», «Pero asi se llaman?».
    # Las tres se contestaron de memoria (precios de hace dos años, nombres de
    # empresas inventados).
    # También con «¿» delante y con pregunta de dato («¿Y cuántos habitantes
    # tiene?», tercera prueba en vivo: contestó de memoria con el censo de
    # 2021). «¿y qué tal?» o «¿y cómo estás?» no: eso es charla.
    _SIGUE_EL_TEMA_RE = re.compile(
        r"^\s*[¿¡]?\s*(?:no+\s*,?\s*no+\b|no,?\s+(?:tipo|me\s+refiero|digo|hablo|es\s+para)\b|"
        r"me\s+refiero\b|tipo\s+para\b|y\s+(?:los|las|el|la|para|en)\b|"
        r"y\s+(?:cu[aá]nt[oa]s?|d[oó]nde|cu[aá]ndo|qui[eé]n(?:es)?|cu[aá]l(?:es)?)\b|"
        r"(?:pero\s+)?(?:as[ií]\s+se\s+llaman?|existen?|seguro|de\s+verdad|"
        r"en\s+serio)\b)",
        re.IGNORECASE)

    def _should_search(self, query: str, conv_history: Optional[List[Dict[str, str]]] = None) -> bool:
        """Detecta si la pregunta necesita información actual de internet.

        Si el mensaje actual matchea triggers (noticias, GPU, etc.) → True.
        Si NO matchea pero es afirmación corta ('sí', 'porfavor', 'dale') Y la
        última respuesta de Celestia ofreció buscar → True (forzar búsqueda
        con la query previa del usuario como contexto).
        """
        # El canal ya ejecutó una herramienta y anexó su [RESULTADO]: la
        # información ya está en el turno, buscar otra vez sólo añade latencia
        # (sesión 42: «¿cuándo sale Opus 6?» hacía dos búsquedas seguidas).
        if self._BLOQUE_ANEXADO_RE.search(query or ""):
            return False
        # La fecha/hora las tiene en el prompt: no hace falta internet
        if self._CONSULTA_FECHA_RE.search(query):
            return False
        # Desahogo con adverbio temporal: no es una consulta de actualidad
        if self._EMOCION_PERSONAL_RE.search(query):
            resto = self._ADV_TEMPORAL_SUELTO_RE.sub(" ", query)
            if not self._SEARCH_TRIGGERS.search(resto):
                return False
        # Sesión 74 — antes del freno «habla de ella» (ver arriba).
        # «busca en tu memoria mi cumpleaños» o «búscalo en mis fotos» también
        # son órdenes de buscar, pero no en internet.
        if (self._ORDEN_BUSCAR_FUERTE_RE.search(query)
                and not re.search(
                    r"\b(?:en|dentro\s+de|entre)\s+(?:tu|tus|mi|mis|la|las|el|los)\s+"
                    r"(?:memoria|recuerdos|cabeza|archivos?|carpetas?|ficheros?|"
                    r"documentos|fotos|galer[ií]a|m[oó]vil|tel[eé]fono|contactos|"
                    r"mensajes|conversaci[oó]n(?:es)?|chats?|notas|agenda)\b",
                    query, re.IGNORECASE)):
            _resto = self._ORDEN_BUSCAR_FUERTE_RE.sub(" ", query)
            if len(self._PALABRAS_CONTENIDO_RE.findall(_resto)) >= 2:
                return True
        if self._TRAMITE_RE.search(query):
            logger.info("Trámite o requisito oficial — forzando buscar_web")
            return True
        if (conv_history and self._SIGUE_EL_TEMA_RE.search(query)
                and self._PALABRAS_CONTENIDO_RE.search(query)):
            _ult = next((t for t in reversed(conv_history)
                         if isinstance(t, dict) and t.get("role") == "assistant"),
                        None)
            if _ult and _ult.get("web"):
                logger.info("Corrige o sigue un tema que acaba de buscarse — "
                            "se busca otra vez")
                return True
        # Sesión 53 — el mensaje habla de ELLA o del hilo, no del mundo. Se
        # quita lo que la señala (y los adverbios temporales, que por sí solos
        # disparan el trigger «hoy») y se mira si queda un tema de verdad:
        # «¿te puedes enterar del precio de la 5090?» sí busca, «¿cómo estás
        # hoy?» no.
        if self._SOBRE_ELLA_RE.search(query):
            resto = self._ADV_TEMPORAL_SUELTO_RE.sub(
                " ", self._SOBRE_ELLA_RE.sub(" ", query))
            resto = self._ORDEN_BUSCAR_RE.sub(" ", resto)
            if not (self._SEARCH_TRIGGERS.search(resto)
                    or self._SEARCH_TRIGGERS_FRESCURA.search(resto)):
                logger.info("El mensaje habla de Celestia, no del mundo — sin búsqueda")
                return False
        if self._SEARCH_TRIGGERS.search(query):
            return True
        # Frescura (sesión 42): familias de preguntas cuya respuesta caduca.
        # El guard evita mandar a internet lo personal y lo domótico.
        if (self._SEARCH_TRIGGERS_FRESCURA.search(query)
                and not self._NO_BUSCAR_RE.search(query)):
            logger.info("Pregunta con respuesta caducable — forzando buscar_web")
            return True
        # Caso confirmación: usuario dice "sí/porfavor" tras un ofrecimiento
        if conv_history and self._AFIRMACION_RE.match(query):
            for turn in reversed(conv_history[-6:]):
                content = turn.get("content", "") if isinstance(turn, dict) else ""
                role = turn.get("role", "") if isinstance(turn, dict) else ""
                if role == "assistant" and self._OFRECIMIENTO_BUSQUEDA_RE.search(content):
                    logger.info("Afirmación detectada tras ofrecimiento de búsqueda — forzando buscar_web")
                    return True
        # Sesión 45 — regla por defecto: una consulta sobre el mundo exterior
        # va a internet aunque no haya disparado ningún patrón concreto. Se
        # piden cuatro palabras para no mandar a buscar los seguimientos
        # («¿y eso?», «¿por qué?»), que no traen tema propio y darían una
        # query basura.
        if (len((query or "").split()) >= 4
                and self._CONSULTA_EXTERNA_RE.search(query)
                and not self._SEGUIMIENTO_RE.search(query)
                and not self._SIN_INTERNET_RE.search(query)
                and not self._NO_BUSCAR_RE.search(query)
                and not (self._EXPLICACION_RE.search(query)
                         and not self._NO_ATEMPORAL_RE.search(query.strip(" ¿¡")))):
            logger.info("Consulta del mundo exterior sin patrón explícito — buscando por defecto")
            return True
        return False

    # Palabras con carga: se descartan artículos, preposiciones y demás
    # relleno para medir si un mensaje trae tema propio o no.
    _PALABRAS_CONTENIDO_RE = re.compile(
        r"\b(?!(?:el|la|los|las|un|una|unos|unas|de|del|al|a|en|y|o|que|qu[eé]|"
        r"con|por|para|se|su|sus|me|te|le|lo|es|son|m[aá]s|ya|si|s[ií]|no|"
        r"pues|como|c[oó]mo|cual|cu[aá]l|sobre|muy|tan|pero|cu[aá]nto|"
        r"cu[aá]nta|cu[aá]ntos|cu[aá]ntas|todo|toda|todos|todas|eso|esto|"
        r"ahora|tambi[eé]n|entonces|costar[ií]a|valdr[ií]a|ser[ií]a)\b)"
        r"[a-zA-Z\u00c0-\u017f]{3,}\b")

    # Preguntas por obras: ahí una palabra entre comillas sí es un título.
    _PIDE_OBRAS_RE = re.compile(
        r"\b(?:pel[ií]culas?|pelis?|series?|libros?|novelas?|canci[oó]n(?:es)?|"
        r"discos?|[aá]lbum(?:es)?|juegos?|videojuegos?|animes?|mangas?|"
        r"documentales?|podcasts?|obras?)\b", re.I)

    # Palabra con mayúscula que no abre la frase: Betis, Canberra, Nvidia. Ni
    # la primera del mensaje ni la que sigue a un punto o una interrogación
    # («¿qué pasó? Cuándo vuelve» — revisión de Codex).
    _NOMBRE_PROPIO_RE = re.compile(
        r"(?<=\s)(?<![.?!…]\s)[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}")

    # Sesión 74: «Oye celestia cuanto cuesta tener un enlace propio» se buscó
    # tal cual, el buscador trajo la criptomoneda Celestia (TIA) y acabó en la
    # respuesta. Llamarla por su nombre no es parte del tema.
    _VOCATIVO_RE = re.compile(
        r"^\s*(?:(?:oye|hey|eh|hola|mira|venga)\s*,?\s*)?celestia\b\s*[,:]?\s*|"
        r"\s*,\s*celestia\s*(?=[?!.]*\s*$)",
        re.IGNORECASE)

    @classmethod
    def _sin_vocativo(cls, texto: str) -> str:
        limpio = cls._VOCATIVO_RE.sub(" ", texto or "").strip()
        return limpio or (texto or "")

    def _query_para_buscar(self, query_actual: str,
                              conv_history: Optional[List[Dict[str, str]]] = None) -> str:
        """Decide qué consulta usar para buscar_web.

        Si la query actual es afirmación corta ('sí'/'porfavor'), usa el mensaje
        previo del USUARIO (que contenía el tema real). Si no, usa la actual.
        """
        query_actual = self._solo_mensaje_usuario(query_actual).strip() or query_actual
        query_actual = self._sin_vocativo(query_actual)
        if conv_history and self._AFIRMACION_RE.match(query_actual):
            for turn in reversed(conv_history[-8:]):
                role = turn.get("role", "") if isinstance(turn, dict) else ""
                content = turn.get("content", "") if isinstance(turn, dict) else ""
                if role == "user" and not self._AFIRMACION_RE.match(content):
                    return self._sin_vocativo(content)[:200]
        # Sesión 45 — seguimientos pobres. «Pues quería seguir sabiendo sobre
        # qué pc me recomiendas» se queda en «qué pc» al quitarle el envoltorio
        # conversacional, y con eso el buscador devolvió un artículo de 2024
        # sobre ordenadores para la ESO. Cuando el mensaje no trae tema propio
        # se le pega el del último mensaje del usuario que sí lo tenía.
        # Se enriquece cuando el mensaje se apoya en lo anterior (anafórico) o
        # cuando no queda casi nada con carga. Antes bastaba con «menos de tres
        # palabras» y eso arrastraba preguntas que sí traen tema, como
        # «¿cuánto cuesta una RTX 5090?».
        _con_carga = len(self._PALABRAS_CONTENIDO_RE.findall(query_actual))
        # Un nombre propio es tema por sí solo: «¿cómo va el Betis?» tiene una
        # sola palabra con carga, se tomó por seguimiento y se buscó pegado a
        # la pregunta anterior del mundial (26 sep 2026).
        if _con_carga < 2 and self._NOMBRE_PROPIO_RE.search(query_actual.strip(" ¿¡")):
            _con_carga = 2
        # Sesión 74: una corrección («Nono tipo para tener mi sitio web sabes»)
        # trae palabras propias pero el tema es el de antes.
        if conv_history and (self._SEGUIMIENTO_RE.search(query_actual)
                             or self._SIGUE_EL_TEMA_RE.search(query_actual)
                             or _con_carga < 2):
            for turn in reversed(conv_history[-10:]):
                if not isinstance(turn, dict) or turn.get("role") != "user":
                    continue
                previo = self._sin_vocativo(
                    self._solo_mensaje_usuario(turn.get("content") or "").strip())
                if previo.lower() == query_actual.lower():
                    continue
                # Otro seguimiento tampoco trae el tema: «¿Y cuándo se fundó?»
                # se pegaba a «¿Y cuántos habitantes tiene?», que no nombra
                # Canberra (cuarta prueba en vivo). Se sigue hacia atrás hasta
                # la pregunta que sí lo nombraba.
                if ((self._SIGUE_EL_TEMA_RE.search(previo)
                        or self._SEGUIMIENTO_RE.match(previo))
                        and len(self._PALABRAS_CONTENIDO_RE.findall(previo)) < 3):
                    continue
                # Dos palabras con contenido bastan para ser un tema: «¿Cuál es
                # la capital de Australia?» tiene dos y con cuatro no se pegaba
                # a «¿Y cuántos habitantes tiene?» (sesión 74).
                if len(self._PALABRAS_CONTENIDO_RE.findall(previo)) >= 2:
                    logger.info("Seguimiento sin tema propio — se le añade el "
                                "contexto de «%.40s»", previo)
                    return f"{query_actual} {previo}"[:200]
        return query_actual

    _DIAS_ES   = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    _MESES_ES  = ["enero","febrero","marzo","abril","mayo","junio",
                  "julio","agosto","septiembre","octubre","noviembre","diciembre"]

    @staticmethod
    def _recuerdos_utilizables(conv_parts: List[str], web_context: str) -> List[str]:
        """Qué recuerdos de conversaciones anteriores pueden entrar al prompt.

        Si este turno trae datos frescos de internet, ninguno: la respuesta
        tiene que salir de la búsqueda, y un recuerdo con la respuesta vieja
        a esta misma pregunta sólo puede contaminarla (ver sesión 44).
        """
        if conv_parts and web_context:
            logger.info("Recuerdos omitidos (%d): este turno manda internet",
                        len(conv_parts))
            return []
        return conv_parts

    def usar_hilo(self, hilo: Optional[str]) -> str:
        """Pone delante el contexto de ese chat. Devuelve el hilo activo.

        Los canales que no saben de hilos (Termux, WhatsApp) no pasan ninguno y
        se quedan con el de siempre, así que para ellos no cambia nada.
        """
        hilo = (hilo or "").strip()[:64] or HILO_POR_DEFECTO
        if hilo != self.hilo_actual or hilo not in self._hilos:
            self.conv_history = self._hilos.setdefault(hilo, [])
            self.hilo_actual = hilo
            logger.info("Hilo activo: %s (%d turnos en memoria)",
                        hilo, len(self.conv_history))
        return hilo

    def anotar_turno(self, user_input: str, respuesta: str) -> None:
        """Apunta en el hilo activo un turno que se resolvió sin el modelo.

        Sin esto, lo que se contesta por un atajo (una foto pasada a líneas) no
        existe para el siguiente mensaje: «¿y más gruesas?» no sabría de qué
        se habla.
        """
        hilo = self.hilo_actual
        self.conv_history.append({"role": "user", "content": user_input})
        self.conv_history.append({"role": "assistant", "content": respuesta})
        max_hist = self.config.CONV_HISTORY_TURNS * 2
        if len(self.conv_history) > max_hist:
            del self.conv_history[:-max_hist]

        def _persistir():
            try:
                self.memory.add_conversation(user_input, respuesta, 1.0, hilo=hilo)
            except Exception as e:
                logger.debug("anotar_turno: no se guardó: %s", e)
        threading.Thread(target=_persistir, daemon=True).start()

    def olvidar_hilo(self, hilo: str) -> bool:
        """Saca ese chat de la memoria viva. La BD la limpia quien llame."""
        hilo = (hilo or "").strip()[:64]
        existia = self._hilos.pop(hilo, None) is not None
        if hilo == self.hilo_actual:
            self.conv_history = self._hilos.setdefault(HILO_POR_DEFECTO, [])
            self.hilo_actual = HILO_POR_DEFECTO
        return existia

    # ── Sesión 74: no perder el hilo ─────────────────────────────────────
    # Del chat real: la respuesta salió en portugués; Enzo se quejó («Y oor que
    # me lo dices en protugues o brasileño?») y Celestia dijo «aquí tienes la
    # respuesta en español»… sin darla. A «Y la respuesta?» contestó que
    # seguiría en español, y al final se inventó cuál «era». Lo que pide es la
    # respuesta a lo que dijo antes: se busca y se le pone delante al modelo.
    _IDIOMAS_NOMBRADOS = (
        r"(?:p[or]{2}tugu[eé]s|brasile[ñn]o|ingl[eé]s|italiano|franc[eé]s|"
        r"catal[aá]n|gallego|alem[aá]n|otro\s+idioma)")
    _QUEJA_IDIOMA_RE = re.compile(
        r"\b(?:dices|hablas|contestas|respondes|escribes|has\s+(?:hablado|"
        r"contestado|respondido|escrito))\s+en\s+" + _IDIOMAS_NOMBRADOS + r"|"
        r"\brespuesta\s+(?:luego\s+|despu[eé]s\s+)?(?:fue|sali[oó]|vino)\s+en\s+"
        + _IDIOMAS_NOMBRADOS,
        re.IGNORECASE)
    _RECLAMA_RESPUESTA_RE = re.compile(
        r"^\s*¿?\s*(?:y\s+)?(?:la|mi)\s+respuesta\s*\?*\s*$|"
        r"\bno\s+me\s+(?:has\s+|la\s+has\s+|lo\s+has\s+)?(?:respondido|"
        r"contestado|respondiste|contestaste|dado|diste)\b|"
        r"\bnunca\s+me\s+la\s+(?:diste|has\s+dado)\b",
        re.IGNORECASE)

    # Un saludo a secas. El 30 de agosto, a «hola» contestó «¿Quieres que
    # empecemos a mirar tiendas y precios para los componentes?»: retomó por su
    # cuenta el tema viejo del hilo, y Enzo respondió «no».
    _SOLO_SALUDO_RE = re.compile(
        r"^\s*[¿¡]*\s*(?:hola+|holi+|buenas|buenos\s+d[ií]as|buenas\s+(?:tardes|noches)|"
        r"hey|ey|qu[eé]\s+tal|qu[eé]\s+pasa)(?:\s+celestia)?\s*[!?.,]*\s*$",
        re.IGNORECASE)

    def _mensaje_sin_contestar(self, hist, idioma: Optional[str],
                               solo_otro_idioma: bool = False) -> str:
        """El mensaje del usuario que se quedó sin respuesta de verdad, o "".

        Primero, el que recibió respuesta en otro idioma: es lo que se reclama
        casi siempre. Si no lo hay (y no se exige), el último mensaje con tema
        propio que no sea otra queja o reclamación.
        """
        turnos = [t for t in (hist or [])[-12:] if isinstance(t, dict)]
        if idioma:
            for i in range(len(turnos) - 1, 0, -1):
                if turnos[i].get("role") != "assistant":
                    continue
                dice = idiomas.detectar(turnos[i].get("content") or "")
                if dice and dice != idioma and turnos[i - 1].get("role") == "user":
                    return self._solo_mensaje_usuario(
                        turnos[i - 1].get("content") or "").strip()
        if solo_otro_idioma:
            return ""
        for t in reversed(turnos):
            if t.get("role") != "user":
                continue
            texto = self._solo_mensaje_usuario(t.get("content") or "").strip()
            if (self._QUEJA_IDIOMA_RE.search(texto)
                    or self._RECLAMA_RESPUESTA_RE.search(texto)):
                continue
            if len(self._PALABRAS_CONTENIDO_RE.findall(texto)) >= 3:
                return texto
        return ""

    def _sin_monologo_o_reintento(self, response: str, messages) -> str:
        """La respuesta sin «(piensa: …)»; si no quedaba nada, se pide otra vez.

        Cuarta prueba en vivo (sesión 74): a «¿Y cuándo se fundó?» Gemini
        escribió solo el razonamiento, y el chat contestó «Perdona, me he liado
        pensando… ¿Me lo dices otra vez?» — el hilo, perdido justo en la
        pregunta encadenada. Se pide UNA vez más, directa. Devuelve "" si
        tampoco sale nada: entonces decide `api.py`, como antes.
        """
        limpio = formato.sin_monologo(response)
        if limpio or not (response or "").strip():
            return limpio
        logger.info("La respuesta era solo razonamiento — se pide otra vez, directa")
        try:
            directa = self.model.generate_from_messages(
                list(messages) + [{"role": "user", "content": (
                    "Contesta ya a mi último mensaje, directo y en pocas frases. "
                    "Nada de razonar entre paréntesis ni de «(piensa: …)».")}],
                max_new_tokens=max(self.config.GEN_MAX_TOKENS, 700),
                temperature=0.4, top_p=0.9, stream=False)
        except Exception as e:
            logger.debug("El reintento sin razonamiento falló: %s", e)
            return ""
        return formato.sin_monologo((directa or "").strip())

    def _cumplir_letras(self, user_input: str, response: str, messages) -> str:
        """Si el encargo prohíbe letras, comprueba la respuesta y la pide otra
        vez (dos como mucho) diciendo qué palabras fallan. Si no sale, se
        queda la mejor y se dice con claridad que no cumple."""
        from .primer_arranque import MENSAJE_SIN_CEREBRO
        fuera = restriccion_letras.prohibidas(user_input)
        # Sin cerebro no hay frase que revisar: el aviso de «necesito una
        # clave» salió con un «no lo he conseguido» pegado (3 oct 2026).
        sin_cerebro = MENSAJE_SIN_CEREBRO.split(".")[0]
        if not fuera or not response or sin_cerebro in response:
            return response
        intentos = [response]
        malas = restriccion_letras.infracciones(
            restriccion_letras.frase_de(response), fuera)
        for _ in range(2):
            if not malas:
                return intentos[-1]
            logger.info("Letras prohibidas %s en %s — se pide otra vez",
                        sorted(fuera), malas[:6])
            try:
                otra = self.model.generate_from_messages(
                    list(messages) + [
                        {"role": "assistant", "content": intentos[-1]},
                        {"role": "user", "content": (
                            "No cumple: estas palabras llevan letras prohibidas ("
                            + ", ".join(sorted(fuera)) + "): "
                            + ", ".join(malas[:10]) + ". Revisa letra a letra "
                            "cada palabra y escribe SOLO la frase nueva, sin "
                            "comentarios.")}],
                    max_new_tokens=max(self.config.GEN_MAX_TOKENS, 400),
                    temperature=0.7, top_p=0.95, stream=False)
            except Exception as e:
                logger.debug("El reintento de letras falló: %s", e)
                break
            otra = formato.sin_monologo((otra or "").strip())
            if not otra or sin_cerebro in otra:
                break
            intentos.append(otra)
            malas = restriccion_letras.infracciones(
                restriccion_letras.frase_de(otra), fuera)
        if not malas:
            return intentos[-1]
        elegida = restriccion_letras.mejor(intentos, fuera) or response
        malas = restriccion_letras.infracciones(
            restriccion_letras.frase_de(elegida), fuera)
        if not malas:
            return elegida
        return elegida + "\n\n" + restriccion_letras.aviso(malas, fuera)

    def respond(self, user_input: str, stream: bool = True, max_tokens: int = None) -> str:
        """Procesa un mensaje del usuario y devuelve la respuesta del LLM.

        Internamente:
          1. Recupera contexto relevante de MemoryDB (FAISS + FTS5 + re-rank).
          2. Compone system prompt dinámico con arquitectura real + hechos del usuario.
          3. Llama a ModelWrapper.generate_from_messages (con cadena de fallback).
          4. Persiste la conversación y dispara extracción de hechos en background.

        Parameters
        ----------
        user_input : str
            Mensaje del usuario.
        stream : bool
            Si True, imprime tokens a stdout mientras llegan (modo CLI).
        max_tokens : int, opcional
            Override del cap por defecto.

        Returns
        -------
        str
            Respuesta del modelo.
        """
        # El turno se queda con SU chat y con SU historial. Todo lo que sigue
        # usa estas dos referencias y no `self.*`: mientras se piensa una
        # respuesta puede entrar otro mensaje de otro chat —dos aparatos, o dos
        # pestañas— y cambiar el hilo activo por debajo. Así el turno acaba
        # donde empezó, aunque el guardado ocurra medio segundo después.
        _hilo_turno = self.hilo_actual
        _hist = self.conv_history

        _t0 = time.time()
        # El chat enseña en qué anda: el logo del cliente (terminal y web) cambia
        # de estado con estas marcas. Cada fase real tiene que anunciarse aquí o
        # el spinner dirá «pensando» mientras en realidad lee una página.
        actividad.empezar_turno(self._solo_mensaje_usuario(user_input)[:60])
        # Recuperar memoria ANTES de aprender el nuevo hecho (evita eco inmediato)
        with actividad.fase("recordando"):
            similar = self.memory.retrieve_similar(user_input, k=3)
        self._learn_silently(user_input)

        # Fecha y hora en español — usar TZ del usuario (Europe/Madrid).
        # Antes usaba datetime.now() puro, que en servidores UTC daba la hora
        # equivocada (visto sesión 26: usuario en España, Celestia dijo 14:39
        # cuando eran 16:39).
        now = datetime.now(_TZ_USUARIO) if _TZ_USUARIO else datetime.now()
        dia = self._DIAS_ES[now.weekday()]
        mes = self._MESES_ES[now.month - 1]
        fecha_str = f"{dia}, {now.day:02d} de {mes} de {now.year}, {now.strftime('%H:%M')}"
        online = self.connectivity.is_online()
        internet_str = (
            "Ahora mismo tienes conexión a internet y puedes buscar información actualizada."
            if online else
            "Ahora mismo NO tienes conexión a internet — no puedes buscar información externa. "
            "Responde solo con lo que ya sabes y sé honesto si no tienes datos recientes."
        )
        # Multi-tenant: el SYSTEM_PROMPT tiene placeholder __USUARIO__ que se
        # reemplaza por el nombre del perfil. Si no hay perfil, "tu usuario".
        try:
            from .profile import PerfilUsuario
            _p = PerfilUsuario()
            usuario_nombre = _p.nombre or "tu usuario"
        except Exception:
            usuario_nombre = "tu usuario"
        # Bloque 6: inyección condicional de capas. En charla pura (el turno no
        # menciona dispositivo, archivos, herramientas ni acciones) se omiten las
        # ~70 líneas de comandos UI y catálogo de tools, que solo saturarían al
        # modelo. Conservador: ante cualquier señal de capacidad, se incluye todo.
        # Sobre el mensaje del usuario SIN los bloques anexados — mismo motivo que
        # en la detección de idioma (sesión 42). Medido en vivo: los titulares que
        # devuelve buscar_noticias traen «pantalla», «app» o «imagen» en su propio
        # texto y encendían la capa de dispositivo en toda búsqueda. Lo que decide
        # qué capas hacen falta es lo que pidió el usuario, no lo que devolvió la
        # herramienta.
        _txt_cap = self._solo_mensaje_usuario(user_input or "")
        _txt_completo = user_input or ""
        # Un turno que ya trae el [RESULTADO] de una tool necesita SIEMPRE la capa
        # de herramientas: sus reglas anti-invención («no digas ✅ sin ver el
        # resultado real») son justo las que aplican ahí.
        _con_anexo = "[RESULTADO]" in _txt_completo or "[CAPTURA" in _txt_completo
        # Pero la de DISPOSITIVO sólo si el anexo es de UI/app. Medido en vivo:
        # anexar el [RESULTADO] sin distinguir volvía a encender las dos capas en
        # toda búsqueda web — que es el turno que se quería abaratar, y el único
        # que jamás va a emitir un [TAP:x,y]. La regla ANTI-MENTIRA DE APPS sí
        # hace falta cuando el resultado viene de abrir/cerrar una app o de leer
        # la pantalla, y eso se reconoce por el propio texto del resultado.
        _anexo_es_ui = bool(_ANEXO_UI_RE.search(_txt_completo))
        _necesita_disp = _anexo_es_ui or bool(_SENAL_DISPOSITIVO_RE.search(_txt_cap))
        _necesita_tools = _con_anexo or bool(_SENAL_HERRAMIENTAS_RE.search(_txt_cap))
        system = (self.config.construir_system_prompt(
                      usuario_nombre, accionable=_necesita_tools, dispositivo=_necesita_disp)
                  + f"\nFecha y hora actual: {fecha_str}. {internet_str}")

        # Privacidad de internos + anti-alucinación (sesión 37): NO inyectamos la cadena
        # de modelos al contexto. Antes se le daba al LLM «cadena de fallback = Groq/… →
        # OpenRouter/… → local/…» con la instrucción «si preguntan cómo funcionás respondé
        # con esta cadena» — y el LLM la revelaba a CUALQUIERA que preguntara «qué modelos
        # usas». El LLM no necesita conocer sus proveedores: solo (a) no inventar una
        # arquitectura falsa, (b) no revelar la real.
        system += (
            "\n\nSOBRE TU IMPLEMENTACIÓN: NO puedes modificar tu propio código; si te piden "
            "cambios, decí que los anotás para Enzo (NO «aplicaré»/«implementaré»). Si te "
            "preguntan qué modelos usás, tu arquitectura, tus proveedores, tu cadena de "
            "fallback o cómo estás hecha por dentro, NO lo reveles NI lo inventes: respondé "
            "con amabilidad que no compartís tus detalles técnicos internos y ofrecé ayudar "
            "en otra cosa."
        )

        # Inyectar perfil del usuario si existe
        try:
            perfil_path = MEM_DIR / "perfil_usuario.json"
            if perfil_path.exists():
                perfil_datos = json.loads(perfil_path.read_text(encoding="utf-8"))
                if perfil_datos.get("onboarding_completo"):
                    partes_perfil = []
                    nombre_perfil = perfil_datos.get("nombre") or ""
                    if nombre_perfil:
                        partes_perfil.append(
                            f"El dueño del dispositivo se llama {nombre_perfil}. "
                            f"REGLA ABSOLUTA SOBRE IDENTIDAD (precedencia máxima sobre todo lo demás): "
                            f"Si el mensaje del usuario contiene 'me llamo X', 'soy X', 'mi nombre es X' "
                            f"o niega ser {nombre_perfil} ('no soy {nombre_perfil}', 'no me llamo {nombre_perfil}'), "
                            f"el usuario es X (no {nombre_perfil}). Respondes a X. Esto aplica desde "
                            f"ESE MISMO mensaje, incluyendo preguntas como '¿cómo me llamo?' formuladas "
                            f"en la misma frase. PROHIBIDO responder '{nombre_perfil}' cuando el usuario "
                            f"acaba de presentarse con otro nombre."
                        )
                    if perfil_datos.get("trato"):
                        partes_perfil.append(f"Tratamiento preferido de {nombre_perfil or 'usuario'}: {perfil_datos['trato']}.")
                    if perfil_datos.get("intereses"):
                        partes_perfil.append(f"Contexto: {perfil_datos['intereses']}.")
                    if perfil_datos.get("uso"):
                        partes_perfil.append(f"Le ayudas principalmente con: {perfil_datos['uso']}.")
                    if partes_perfil:
                        system += "\n\nPERFIL DEL USUARIO: " + " ".join(partes_perfil)
        except Exception:
            pass

        # Inyectar hechos persistentes — TOP-3 relevantes según la query +
        # SIEMPRE los hechos de tono/idioma/formato (afectan a toda respuesta).
        try:
            hechos = self.memory.hechos_usuario()
            if hechos:
                # Particionar: tono/formato/idioma → siempre; resto → ranking
                _TONO_KEYS = ("tono", "formato", "idioma", "saludo", "estilo",
                              "personalidad", "voz", "voice")
                hechos_tono = [h for h in hechos
                               if any(k in (h.get("clave") or "").lower() for k in _TONO_KEYS)]
                resto = [h for h in hechos if h not in hechos_tono]
                # Ranking simple por solapamiento de palabras con la query
                q_palabras = set(re.findall(r"\b\w{4,}\b", user_input.lower()))
                def _score(h):
                    txt = f"{h.get('clave','')} {h.get('valor','')}".lower()
                    return sum(1 for p in q_palabras if p in txt)
                resto.sort(key=_score, reverse=True)
                top_resto = resto[:3]
                if top_resto:
                    lineas = [f"- {h['clave']}: {h['valor']}" for h in top_resto]
                    # Sesión 32 (BUG-S102): regla ANTI-INVENCIÓN. El LLM Groq
                    # alucinaba «trabajas en hostelería» pese a tener
                    # profesion=farmacéutica. Forzar uso exclusivo de estos
                    # hechos y prohibir mezclar profesiones/datos inventados.
                    system += (
                        "\n\nHECHOS RELEVANTES DEL USUARIO (ÚNICA FUENTE DE "
                        "VERDAD sobre el usuario — NO inventes ni añadas otras "
                        "profesiones, ciudades o datos que no aparezcan abajo. "
                        "Si la pregunta versa sobre un dato que NO aparece "
                        "aquí, di literalmente que no lo sabes):\n"
                        + "\n".join(lineas)
                    )
                if hechos_tono:
                    lineas_tono = [f"- {h['valor']}" for h in hechos_tono[:4]]
                    system += (
                        "\n\nTONO Y FORMATO (DEBES RESPETARLO SIEMPRE):\n"
                        + "\n".join(lineas_tono)
                    )
        except Exception:
            pass

        # ─── World model: conocimiento relevante del grafo ────────────────
        # Inyecta entidades + relaciones + estados que aparezcan en el mensaje
        # del usuario. Devuelve "" si nada matchea — no añade ruido al prompt.
        try:
            narrativa_kg = self.knowledge.conocimiento_relevante(user_input, top_k=5)
            if narrativa_kg:
                system += "\n\nCONOCIMIENTO RELEVANTE DEL GRAFO:\n" + narrativa_kg
        except Exception as e:
            logger.debug("No pude consultar grafo de conocimiento: %s", e)

        # Búsqueda web automática: triggers explícitos + confirmaciones tras
        # ofrecimiento previo de búsqueda ("¿busco?" → "sí/porfavor/dale").
        web_context = ""
        if online and self._should_search(user_input, _hist):
            from .tools import AgentTools  # lazy import: evita ciclo con tools.py
            tools = AgentTools(self.connectivity)
            query_real = self._query_para_buscar(user_input, _hist)
            _t_search = time.time()
            with actividad.fase("buscando", query_real[:60]):
                web_result = tools.buscar_web(query_real)
            _t_search_ms = int((time.time() - _t_search) * 1000)
            ok_resultado = web_result and not web_result.startswith(("⚠", "Sin resultados", "Error"))
            logger.info(
                "buscar_web '%.40s' (query='%.40s') → %s (%d chars, %dms)",
                user_input, query_real,
                "OK" if ok_resultado else "VACÍO",
                len(web_result or ""), _t_search_ms,
            )
            if ok_resultado:
                web_context = web_result

        if similar:
            personal_facts: List[str] = []
            conv_parts:     List[str] = []
            # Sesión 26: chars/parte recortados (era 120/280/350 → ahora 80/160/200)
            # para no inflar el system prompt y saturar el TPM 6K de Groq free.
            for ep in similar[:3]:
                task   = ep.get("task", "").strip()
                result = ep.get("result", "").strip()
                if task.startswith("[dato personal:"):
                    # Inyectar como hecho directo, no como Q&A
                    if result:
                        personal_facts.append(result)
                else:
                    cuando = _tiempo_relativo(ep.get("ts"))
                    prefijo = f"({cuando}) " if cuando else ""
                    # Sesión 64 — la fosilización, por el otro lado. La S44
                    # cortó el caso con internet, pero cuando el turno NO
                    # busca, un recuerdo viejo sigue entrando con su respuesta
                    # dentro y el modelo la repite como si fuera un hecho
                    # comprobado. Pasó: «hola, ¿entro sin llave?» contestó una
                    # parrafada sobre las pulseras de un festival el 2 de
                    # septiembre, y el día 7 la repitió CASI PALABRA POR
                    # PALABRA — sin buscar nada. No la volvió a inventar: se la
                    # recordaba. Una respuesta suya guardada no es una fuente.
                    #
                    # Pasado un día, del recuerdo se conserva el TEMA (lo que
                    # se preguntó) y se tira lo que ella contestó. Dentro del
                    # día sí entra entera: ahí es continuidad de conversación,
                    # que es justo para lo que sirve. El corte es determinista
                    # porque la S44 ya probó el aviso de texto y el modelo no
                    # le hacía caso.
                    if task and result and _segundos_desde(ep.get("ts")) > 86400:
                        conv_parts.append(
                            f"{prefijo}Ya te preguntó esto: {task[:80]}")
                    elif task and result:
                        conv_parts.append(f"{prefijo}Pregunta: {task[:80]}\nRespuesta: {result[:160]}")
                    elif result:
                        conv_parts.append(f"{prefijo}{result[:200]}")
                    elif task:
                        conv_parts.append(f"{prefijo}{task[:200]}")
            if personal_facts:
                system += (
                    "\n\nDatos del usuario que debes recordar:\n"
                    + "\n".join(f"- {f}" for f in personal_facts)
                )
            # Sesión 42 — memoria fosilizada: una respuesta de actualidad se
            # guarda en `conversations` y vuelve como «recuerdo» la próxima vez
            # que se pregunta lo mismo. Visto en vivo: Celestia buscó el ranking
            # de IA de agosto de 2026 y aun así repitió el «Claude 3, Gemini 1.5,
            # Llama 3» que ella misma había dicho antes.
            # Sesión 44 — el primer intento fue un aviso de texto («ojo, puede
            # haber datos caducados»): seguía dependiendo de que el modelo hiciera
            # caso, y no lo hacía. Ahora es determinista: si este turno trae datos
            # de internet, los recuerdos de conversación NO se inyectan. Los datos
            # personales del usuario sí siguen entrando — esos no caducan.
            # Sesión 74: lo mismo si el dato llega como [RESULTADO] de una
            # herramienta (el «empeño» busca así). Con los recuerdos dentro,
            # «¿cuánto cuesta tener un enlace propio?» empezó por «la última vez
            # nos confundimos con los enlaces de SEO»: mezcló el chat del 13 de
            # septiembre con lo que acababa de encontrar.
            conv_parts = self._recuerdos_utilizables(
                conv_parts, web_context or (
                    "[RESULTADO]" if self._BLOQUE_ANEXADO_RE.search(user_input or "")
                    else ""))
            if conv_parts:
                system += (
                    "\n\nRecuerdos relevantes de conversaciones anteriores "
                    "(úsalos solo si son pertinentes a la pregunta actual).\n"
                    + "\n---\n".join(conv_parts)
                )

        # MEMORIA INMEDIATA — sesión 46: ya NO se inyecta aquí.
        #
        # El bloque metía los últimos 4 turnos resumidos a 200 chars en el system…
        # y esos mismos turnos ya van enteros (700/300 chars) en `messages`. Era un
        # subconjunto estricto: ~200 tokens duplicados en CADA turno. Su única razón
        # de ser era sobrevivir a que _trim_context recortase el historial por
        # límite de contexto — un caso que casi nunca ocurre (hace falta rebasar los
        # ~32k del modelo, y aquí se anda por 3-5k).
        #
        # La red no se pierde, se mueve al sitio donde de verdad hace falta:
        # _trim_context resume lo que descarta, y sólo entonces. Así cuesta 0 tokens
        # los otros 99 turnos de cada 100.

        # Sesión 46 — presupuesto de tokens a la vista. El TPM de Groq (8.000) es
        # el cuello de botella real y hasta ahora sólo se veía el total que
        # devuelve el proveedor, sin saber qué parte era el prompt fijo y qué
        # parte el contexto de internet. Sin esto, cada intento de adelgazar el
        # prompt se mide a ciegas. ~4 chars/token es suficiente para vigilar.
        logger.info(
            "Presupuesto prompt: system ~%d tok (capas: %s) + web ~%d tok + historial ~%d tok",
            len(system) // 4,
            "+".join(c for c, on in (("disp", _necesita_disp), ("tools", _necesita_tools)) if on)
            or "charla",
            len(web_context) // 4,
            sum(len(t.get("content") or "") for t in _hist[-self.config.CONV_HISTORY_TURNS:]) // 4,
        )

        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        # Sesión 45 — el historial iba ENTERO, con respuestas de mil quinientos
        # caracteres, y encima se repetía resumido arriba en MEMORIA INMEDIATA.
        # Medido: 5.757 tokens de prompt para un «hola qué tal», con un plan
        # que da 8.000 por minuto — de ahí los 429 constantes y los 8 s de
        # espera. Los turnos viejos se recortan (basta el hilo, no la letra) y
        # el más reciente se conserva casi entero, que es el que se sigue.
        _historial = _hist[-self.config.CONV_HISTORY_TURNS:]
        for i, turn in enumerate(_historial):
            tope = self.config.CHARS_TURNO_RECIENTE if i >= len(_historial) - 2 \
                else self.config.CHARS_TURNO_ANTIGUO
            contenido = (turn.get("content") or "")
            if len(contenido) > tope:
                contenido = contenido[:tope].rstrip() + "…"
            messages.append({"role": turn.get("role", "user"), "content": contenido})

        # Sesión 29 (bug T): detectar idioma del input por heurística simple
        # e inyectar instrucción explícita. El system prompt + perfil en español
        # sobrescribían el idioma del input — esta inyección turn-level fuerza
        # la respuesta en el idioma correcto.
        idioma_hint = ""
        # El texto del usuario SIN los bloques anexados: un [RESULTADO] de
        # buscar_web en inglés metía veinte palabras inglesas en la cuenta y
        # Celestia contestaba en inglés a una pregunta en español (sesión 42).
        _lower = self._solo_mensaje_usuario(user_input).lower()
        # Sesión 54 — el idioma, para todo el mundo. Antes esto acababa en un
        # `else` que daba por hecho el español: quien escribiera en alemán o en
        # japonés recibía español. Ahora manda, por este orden:
        #   1. lo que la persona haya elegido en Ajustes («háblame en inglés»),
        #   2. el idioma del mensaje, detectado por escritura y por palabras
        #      funcionales (`celestia_lib/idiomas.py`, determinista),
        #   3. el del mensaje anterior, si este era demasiado corto para saberlo
        #      («vale», «ok», un número no están en ningún idioma),
        #   4. y si no hay nada de eso, no se fuerza ninguno: inventarse el
        #      idioma es peor que dejar que siga el del hilo.
        _pref = _idioma_preferido()
        _idioma_turno = None
        if _pref and _pref != "auto":
            _idioma_turno = _pref
            idioma_hint = idiomas.instruccion(_pref)
            logger.info("Idioma: %s (elegido en Ajustes)", _pref)
        else:
            # Sobre `_lower` (el mensaje sin lo anexado): un [RESULTADO] de
            # búsqueda en inglés no puede arrastrar la respuesta a otro idioma.
            # Sesión 74: con el idioma del hilo delante, para no saltar a un
            # pariente cercano por dos palabras («te noto mas lista» → pt).
            _detectado = (idiomas.detectar(_lower, previo=self._ultimo_idioma)
                          or self._ultimo_idioma)
            if _detectado:
                if _detectado != self._ultimo_idioma:
                    logger.info("Idioma: %s (detectado en el mensaje)", _detectado)
                self._ultimo_idioma = _idioma_turno = _detectado
                idioma_hint = idiomas.instruccion(_detectado)
            else:
                logger.info("Idioma: sin señal clara — se sigue el del hilo")
                idioma_hint = ""

        # Sesión 37: hint de EMOCIÓN a nivel de turno (misma técnica que el idioma).
        # Con el 8b la regla del system no basta: «estoy triste» disparaba menciones
        # alucinadas de canciones/autores (p. ej. Pablo Alborán). El hint pegado al
        # mensaje del usuario (recency) lo evita incluso con modelo débil.
        # Sesión 53 — queja de tono. El hint va pegado al mensaje del usuario
        # (recency), que es lo único que funciona con modelo débil (lección de
        # la sesión 37: una regla enterrada en el system no gana).
        queja_hint = ""
        if self._QUEJA_TRATO_RE.search(_lower):
            _ya_pidio_perdon = any(
                self._DISCULPA_RE.search(t.get("content", "") or "")
                for t in (_hist or [])[-6:]
                if isinstance(t, dict) and t.get("role") == "assistant"
            )
            queja_hint = (
                "\n\n[La persona se queja de CÓMO le hablas, no de lo que le "
                "dices. Reconócelo en una frase y sigue con lo que estabais "
                "haciendo, ya con el tono que te pide. PROHIBIDO: justificarte "
                "o explicar por qué contestaste así, decir «no era mi "
                "intención», preguntarle si solo quería discutir, y contestar "
                "con tres o cuatro palabras sueltas (eso suena aún más "
                "seco). Dos o tres frases, español de España.]")
            if _ya_pidio_perdon:
                queja_hint = queja_hint[:-1] + (
                    " Ya le has pedido perdón hace un momento: NO vuelvas a "
                    "disculparte, demuéstralo con el tono.]")
                logger.info("Queja de tono con disculpa ya pedida — sin repetir perdón")
            else:
                logger.info("Queja de tono detectada — hint de turno")

        # Sesión 74 — reclama la respuesta de antes (ver _mensaje_sin_contestar).
        _queja_idioma = bool(self._QUEJA_IDIOMA_RE.search(_lower))
        _reclama = bool(self._RECLAMA_RESPUESTA_RE.search(_lower))
        if _queja_idioma or _reclama:
            _pendiente = self._mensaje_sin_contestar(
                _hist, _idioma_turno,
                solo_otro_idioma=_queja_idioma and not _reclama)
            if _pendiente:
                queja_hint += (
                    "\n\n[" + ("Tu respuesta anterior salió por error en otro "
                               "idioma. " if _queja_idioma else
                               "Reclama que no le contestaste de verdad. ")
                    + "Pide perdón en media frase y, EN ESTE MISMO MENSAJE, "
                    "contesta de verdad a lo que te dijo antes: «"
                    + _pendiente[:300] + "». PROHIBIDO decir «aquí tienes la "
                    "respuesta» sin darla, prometer que lo harás o inventarte "
                    "qué le contestaste.]")
                logger.info("Reclama la respuesta de antes — se le pone delante: %.40s",
                            _pendiente)

        # Sesión 74 — un saludo a secas no retoma el tema viejo (_SOLO_SALUDO_RE).
        if _hist and self._SOLO_SALUDO_RE.match(_lower):
            queja_hint += (
                "\n\n[Solo te saluda. Salúdale con naturalidad y, como mucho, "
                "pregúntale qué tal o en qué le ayudas. NO retomes por tu cuenta "
                "el tema de antes: si quiere seguir con él, ya lo dirá.]")

        emocion_hint = ""
        if re.search(
            # Intensificador opcional ("muy/tan/bastante/súper/un poco…") entre el
            # verbo y la emoción: "me siento MUY solo" rompía el match antiguo y el
            # hint no se disparaba → el LLM (incluso el 70b) caía en sermón verboso.
            r"\b(?:estoy|ando|hoy\s+estoy|me\s+encuentro|me\s+siento)\s+"
            r"(?:muy|tan|bastante|s[uú]per|re|demasiado|algo|un\s+poco|"
            r"totalmente|completamente|tremendamente)?\s*"
            r"(?:triste|mal|fatal|deprimid[oa]|de\s+baj[oó]n|baj[oó]n|hundid[oa]|"
            r"solo|sola|agobiad[oa]|angustiad[oa]|desanimad[oa]|destrozad[oa]|"
            r"vac[ií]o|vac[ií]a|perdid[oa]|sin\s+ganas)\b"
            r"|\btengo\s+un\s+baj[oó]n\b"
            r"|\blo\s+estoy\s+pasando\s+(?:muy\s+|fatal|mal)\b"
            r"|\bestoy\s+pasando\s+un\s+mal\s+momento\b",
            _lower,
        ):
            emocion_hint = ("\n\n[El usuario expresa una emoción personal (tristeza/ánimo "
                            "bajo). Responde SOLO con empatía cálida y MUY BREVE: máximo 2 "
                            "frases y, si encaja, UNA pregunta abierta. PROHIBIDO sermones, "
                            "charlas de autoayuda, explicar qué es la soledad/tristeza, "
                            "listas de consejos, ni mencionar canciones, autores, citas o "
                            "versos. Ejemplo: 'Vaya, siento que estés así. ¿Quieres "
                            "contarme qué ha pasado?']")

        if web_context:
            # Sesión 26: 2000→1200 chars por el TPM 6K/min de Groq free.
            # Sesión 44: de vuelta a 2000. Con 1200 se cortaban justo los
            # resultados con los datos concretos (los 3 primeros de DDG suelen
            # ser relleno SEO), el modelo se quedaba sin nombres y rellenaba el
            # hueco con su conocimiento caducado. El system prompt adelgazó un
            # 38% en la sesión 38, así que hay sitio de sobra.
            augmented_input = (
                f"{user_input}\n\n"
                f"[Información actualizada de internet:\n{web_context[:1500]}]\n"
                f"Usa esa información para responder de forma precisa y actual. "
                # Sesión 42: sin esta línea el modelo leía el resultado fresco y
                # aun así respondía con su conocimiento de entrenamiento (precios,
                # versiones y cargos viejos). La precedencia debe ser explícita.
                f"Si contradice lo que creías saber, MANDA lo de internet: tu "
                f"conocimiento propio tiene fecha de corte y esto es de hoy. "
                # Sesión 45: el bloque traía el próximo partido y el último
                # resultado, y la respuesta se quedó en «todavía no hay líder».
                # El usuario pidió expresamente detalle, así que se exige
                # explícitamente no dejarse las cifras y fechas por el camino.
                f"NO te dejes datos: si el bloque trae cifras, fechas, nombres "
                f"o marcadores concretos, MENCIÓNALOS en la respuesta en vez "
                f"de resumirlos en una frase vaga. "
                # Sesión 74 — «Dime las empresas mas famosas de españa de
                # karts» devolvió «Karting Madrid», «Karting Valencia» y «Klook
                # España» (una web de reservas), y a «Pero asi se llaman?» otra
                # tanda igual de inventada. Lo que se nombra tiene que estar ahí.
                f"Y al revés: si nombras empresas, sitios, personas o productos, "
                f"que sean SOLO los que aparecen en ese bloque; si no trae "
                f"nombres concretos, dilo claro en vez de inventártelos. "
                # Sesión 41 (bug francés): el texto de internet puede venir en otro
                # idioma (francés/inglés…) y el modelo a veces lo IMITA. Recordatorio
                # explícito y pegado (recency) de responder en el idioma del usuario.
                f"AVISO: el texto de arriba puede estar en otro idioma; tradúcelo si "
                f"hace falta y responde SIEMPRE en el idioma del usuario, NUNCA copies "
                f"el idioma del texto de internet."
                f"{idioma_hint}{emocion_hint}{queja_hint}"
            )
        elif self._BLOQUE_ANEXADO_RE.search(user_input or ""):
            # El canal ya anexó un [RESULTADO] de herramienta. Puede venir en
            # otro idioma (buscar_web devuelve inglés a menudo): mismo aviso
            # pegado al final que en la rama de web_context (sesión 42).
            augmented_input = (
                user_input
                + "\nEse bloque puede estar en otro idioma: tradúcelo si hace "
                  "falta y responde SIEMPRE en el idioma del usuario."
                + idioma_hint + emocion_hint + queja_hint
            )
        else:
            augmented_input = user_input + idioma_hint + emocion_hint + queja_hint
        messages.append({"role": "user", "content": augmented_input})

        # Protección de contexto: si el prompt es muy largo, recortar historial
        messages = self._trim_context(messages)
        # Sesión 26: además del historial, recortar el SYSTEM si supera el
        # budget de tokens (Groq free TPM 6K). _trim_context no lo tocaba.
        messages = self._compactar_system_si_supera(messages)

        # Sesión 45 — las respuestas con datos de internet son informativas
        # (rankings, versiones, noticias) y 512 tokens las cortaba a media
        # frase. Sólo se sube el tope en ese caso: en charla normal alarga la
        # latencia y consume TPM de Groq sin ganar nada.
        # Sesión 74 — «Un listado de todas pero todas las cosas… y explica el
        # por que de cada una con un ejemplo claro» se cortó en el punto 7 con
        # el tope de charla. Quien pide expresamente algo largo lo recibe
        # entero, y una lista con apartados también necesita más de 512.
        _pregunta = self._solo_mensaje_usuario(user_input)
        if max_tokens is None and re.search(
                r"\b(?:list(?:ado|a)\s+(?:de|con)\s+tod[oa]s|tod[oa]s\s+pero\s+tod[oa]s|"
                r"expl[ií]ca(?:me)?\s+(?:el\s+por\s*qu[eé]\s+de\s+)?cada|"
                r"con\s+(?:un\s+)?ejemplos?|paso\s+a\s+paso|en\s+detalle|"
                r"detalladamente|a\s+fondo|lo\s+m[aá]s\s+completo)",
                _pregunta, re.IGNORECASE):
            max_tokens = max(self.config.GEN_MAX_TOKENS, 1400)
        elif max_tokens is None and re.search(
                r"\b(?:listado|ideas|opciones|alternativas|pasos|diferencias|"
                r"ventajas|desventajas|consejos|ejemplos)\b",
                _pregunta, re.IGNORECASE):
            max_tokens = max(self.config.GEN_MAX_TOKENS, 900)
        if max_tokens is None and web_context:
            # 900 reservados salían caros en un plan de 8.000 por minuto: con
            # 700 las respuestas siguen cabiendo (y el cierre limpio remata si
            # alguna se corta).
            max_tokens = max(self.config.GEN_MAX_TOKENS, 700)

        p = self.hparams.params()
        actividad.marcar("escribiendo")
        response = self.model.generate_from_messages(
            messages,
            temperature=p["temp"], top_k=p["top_k"], rep_penalty=p["rep_penalty"],
            stream=stream,
            max_new_tokens=max_tokens,
        )

        # El streaming ya aplicó el filtro de muletillas en terminal;
        # aquí aseguramos que el historial también guarde la versión limpia.
        cleaned, n_subs = quitar_muletillas(response)
        if n_subs > 0:
            response = cleaned

        # Anti-alucinación: si el modelo dice que está/va a buscar pero no se
        # ejecutó buscar_web este turno, forzar la búsqueda real ahora y
        # adjuntar el resultado, o reescribir si seguimos offline.
        # Sesión 29: si el user_input es afirmación corta ("si, hazla", "ok"),
        # NO disparar búsqueda — la query sería basura. Reescribir y ya.
        es_afirmacion_corta = (
            self._AFIRMACION_RE.match(user_input or "")
            and len((user_input or "").split()) <= 3
        )
        def _limpiar_alucinacion(txt: str, sufijo: str = "") -> str:
            """Elimina la FRASE COMPLETA que contiene la alucinación de
            búsqueda y, opcionalmente, añade un sufijo aclaratorio al final.
            Sesión 34 (B34-3): antes la sustitución dejaba texto huérfano
            tipo «¿Quieres que ?» o «¿Quieres que No encontré…».
            Sesión 34 (B34-3 mejora): no añadir sufijo si el texto limpio ya
            comunica el «no encontrado» (evita duplicación)."""
            limpio = _FRASE_ALUCINA_RE.sub("", txt)
            limpio = _ALUCINA_BUSQUEDA_RE.sub("", limpio)
            limpio = re.sub(r"[ \t]{2,}", " ", limpio)
            limpio = re.sub(r"\n{3,}", "\n\n", limpio).strip()
            if sufijo:
                ya_dice_no = re.search(
                    r"\b(no\s+(?:encontr[eé]|tengo|hay|aparec[eé]|hall[eé]|s[eé])|"
                    r"sin\s+resultados?|ning[uú]n\s+resultado)\b",
                    limpio, re.I)
                if not ya_dice_no:
                    if limpio and not limpio.rstrip().endswith((".", "!", "?")):
                        limpio += "."
                    limpio = (limpio + " " + sufijo).strip() if limpio else sufijo
            return limpio

        # Sesión 45 — ofrecer una búsqueda que YA se ha hecho. Caso real:
        # «¿quién va líder en la NBA?» → «no tengo datos actuales… dime "sí" y
        # lo miro», cuando el turno acababa de traer resultados de internet.
        # Leído desde fuera parece que no ha hecho nada. Se quita la frase del
        # ofrecimiento: lo que quede es la respuesta honesta de que no lo ha
        # encontrado, que es la información útil.
        # El guard usa el mismo regex que luego limpia: con dos distintos se
        # desincronizan y pasa lo que pasó — «puedo hacer una búsqueda rápida»
        # no lo detectaba el primero, así que nunca se llegaba a quitar.
        if web_context and _FRASE_OFRECIMIENTO_RE.search(response or ""):
            sin_oferta = _FRASE_OFRECIMIENTO_RE.sub("", response)
            sin_oferta = re.sub(r"[ \t]{2,}", " ", sin_oferta)
            sin_oferta = re.sub(r"\n{3,}", "\n\n", sin_oferta).strip()
            if len(sin_oferta) >= 40:      # que no quede un muñón sin sentido
                logger.info("Ofrecimiento de búsqueda redundante (ya se buscó) — quitado")
                response = sin_oferta

        def _responder_con_web(resultados: str) -> str:
            """Contesta de verdad con lo encontrado, en vez de pegarlo en crudo
            debajo (sesión 74: salieron las líneas «▷» del buscador tal cual,
            con el comando inventado delante). "" si no sale nada limpio."""
            try:
                _msgs = [
                    {"role": "system", "content": (
                        "Eres Celestia. Contesta a lo que te pide usando la "
                        "información de internet de abajo: directo, cercano y "
                        "sin inventar nada que no esté ahí. NO digas que lo has "
                        "buscado ni pegues la lista de resultados.")},
                    {"role": "user", "content": (
                        f"{user_input}\n\n[Información de internet:\n"
                        f"{resultados[:1400]}]{idioma_hint}")},
                ]
                actividad.marcar("escribiendo")
                _txt = self.model.generate_from_messages(
                    _msgs, max_new_tokens=max(self.config.GEN_MAX_TOKENS, 700),
                    temperature=0.5, top_p=0.9, stream=False)
                _txt = formato.sin_monologo((_txt or "").strip())
                if _txt and not _CORCHETE_BUSQUEDA_RE.search(_txt):
                    logger.info("Búsqueda forzada → respuesta regenerada con lo encontrado")
                    return _txt
            except Exception as e:
                logger.debug("Regenerar con la búsqueda forzada falló: %s", e)
            return ""

        # Comando inventado: se ejecuta la búsqueda que pedía y el corchete se
        # va. Con SU consulta, que es mejor que la deducida del mensaje.
        _m_corchete = _CORCHETE_BUSQUEDA_RE.search(response or "")
        if _m_corchete:
            _query_inventada = _m_corchete.group(1).strip()
            response = _CORCHETE_BUSQUEDA_RE.sub("", response or "").strip()
            logger.info("Comando inventado [BUSCAR_WEB] → buscando de verdad: %.50s",
                        _query_inventada)
            _hallado = ""
            if online and _query_inventada:
                try:
                    from .tools import AgentTools
                    _tools = AgentTools(self.connectivity)
                    with actividad.fase("buscando", _query_inventada[:60]):
                        _hallado = _tools.buscar_web(_query_inventada) or ""
                except Exception as e:
                    logger.debug("Búsqueda del comando inventado falló: %s", e)
            if _hallado and not _hallado.startswith(("⚠", "Sin resultados", "Error")):
                response = (_responder_con_web(_hallado)
                            or (response + "\n\n" + _hallado[:1200]).strip())
            elif not response:
                # La respuesta ERA el corchete: sin esto se publicaba vacía.
                response = ("No he encontrado nada fiable sobre eso ahora mismo. "
                            "Si me das más detalle, lo vuelvo a intentar")

        if _ALUCINA_BUSQUEDA_RE.search(response) and not web_context and es_afirmacion_corta:
            # Solo limpiar la alucinación, no buscar (la query sería basura).
            response = _limpiar_alucinacion(response) or response
        elif _ALUCINA_BUSQUEDA_RE.search(response) and not web_context:
            if online:
                try:
                    from .tools import AgentTools
                    tools = AgentTools(self.connectivity)
                    query_real = self._query_para_buscar(user_input, _hist)
                    with actividad.fase("buscando", query_real[:60]):
                        forzado = tools.buscar_web(query_real)
                    if forzado and not forzado.startswith(("⚠", "Sin resultados", "Error")):
                        logger.info("Alucinación de búsqueda detectada → buscar_web forzado OK")
                        response = _limpiar_alucinacion(response)
                        response = (_responder_con_web(forzado)
                                    or (response + "\n\n" + forzado[:1200]).strip())
                    else:
                        logger.info("Alucinación de búsqueda detectada → buscar_web sin resultados")
                        response = _limpiar_alucinacion(
                            response, "No encontré información actualizada al respecto."
                        )
                except Exception as e:
                    logger.debug("Forzado de búsqueda anti-alucinación falló: %s", e)
                    response = _limpiar_alucinacion(response)
            else:
                logger.info("Alucinación de búsqueda detectada (offline) → reescribiendo")
                response = _limpiar_alucinacion(
                    response, "Ahora mismo no tengo conexión para buscarlo."
                )

        # Sesión 41: red de seguridad anti "no tengo información". Si el modelo se
        # rinde sobre un tema externo SIN haber buscado, forzamos una búsqueda y
        # REGENERAMOS con el contexto. Cubre cualquier formulación que los triggers
        # de _should_search no anticipen ("dime la historia de X", etc.).
        if (online and not web_context and _NIEGA_INFO_RE.search(response)
                and not self._AFIRMACION_RE.match(user_input or "")):
            try:
                from .tools import AgentTools
                _tools = AgentTools(self.connectivity)
                _q = self._query_para_buscar(user_input, _hist)
                with actividad.fase("buscando", _q[:60]):
                    _res = _tools.buscar_web(_q)
                if _res and not _res.startswith(("⚠", "Sin resultados", "Error")):
                    logger.info("'No tengo info' detectado → buscar_web forzado OK, regenerando")
                    _msgs = [
                        {"role": "system", "content": (
                            "Eres Celestia. Responde en ESPAÑOL, en tono cercano y claro, "
                            "usando la información de internet de abajo. NO menciones que la "
                            "buscaste ni que antes no la tenías; responde directo.")},
                        {"role": "user", "content": (
                            f"{user_input}\n\n[Información de internet:\n{_res[:1400]}]")},
                    ]
                    actividad.marcar("escribiendo")
                    _nueva = self.model.generate_from_messages(
                        _msgs, max_new_tokens=max(self.config.GEN_MAX_TOKENS, 900),
                        temperature=0.5, top_p=0.9, stream=False)
                    if _nueva and _nueva.strip():
                        response = _nueva.strip()
                else:
                    logger.info("'No tengo info' detectado → búsqueda forzada sin resultados")
            except Exception as e:
                logger.debug("Forzado de búsqueda por 'no tengo info' falló: %s", e)

        # Sesión 44 — red de seguridad de FRESCURA. Si Celestia acaba de afirmar
        # de memoria un dato caducable (una versión, un ranking, «actualmente
        # es…») sin haber buscado nada, se busca y se REGENERA. Los triggers de
        # _should_search miran la pregunta y siempre se les escapa alguna
        # formulación; esto mira la respuesta, que es donde se ve el dato viejo.
        # Un seguimiento sin tema propio («pero cuánto costaría todo?») se
        # apoya en la conversación: mandarlo a internet trae resultados de
        # otro asunto — un caso real acabó hablando de la factura de la luz.
        _es_seguimiento_pobre = (
            self._SEGUIMIENTO_RE.search(user_input or "")
            and len(self._PALABRAS_CONTENIDO_RE.findall(user_input or "")) < 3)
        if (online and not web_context and not _es_seguimiento_pobre
                and _AFIRMACION_CADUCABLE_RE.search(response or "")
                and not self._AFIRMACION_RE.match(user_input or "")
                and not self._NO_BUSCAR_RE.search(user_input or "")
                and not self._EMOCION_PERSONAL_RE.search(user_input or "")
                and not self._CONSULTA_FECHA_RE.search(user_input or "")
                and not _PREGUNTA_SOBRE_SI_MISMA_RE.search(user_input or "")):
            try:
                from .tools import AgentTools
                _tools = AgentTools(self.connectivity)
                _q = self._query_para_buscar(user_input, _hist)
                with actividad.fase("buscando", _q[:60]):
                    _res = _tools.buscar_web(_q)
                if _res and not _res.startswith(("⚠", "Sin resultados", "Error")):
                    logger.info("Dato caducable afirmado de memoria → verificando en internet")
                    _msgs = [
                        {"role": "system", "content": (
                            "Eres Celestia. Responde en ESPAÑOL, en tono cercano y claro, "
                            "usando SOLO la información de internet de abajo para los datos "
                            "que cambian con el tiempo (versiones, rankings, precios, cargos, "
                            "lanzamientos). Tu conocimiento propio tiene fecha de corte y esto "
                            "es de hoy: si se contradicen, MANDA lo de internet. Si el dato "
                            "concreto no aparece ahí, dilo con naturalidad en vez de "
                            "inventarlo. NO menciones que lo has buscado.")},
                        {"role": "user", "content": (
                            f"{user_input}\n\n[Información de internet de hoy:\n{_res[:1600]}]")},
                    ]
                    actividad.marcar("escribiendo")
                    _nueva = self.model.generate_from_messages(
                        _msgs, max_new_tokens=max(self.config.GEN_MAX_TOKENS, 900),
                        temperature=0.5, top_p=0.9, stream=False)
                    if _nueva and _nueva.strip():
                        response = _nueva.strip()
                else:
                    logger.info("Dato caducable afirmado de memoria → sin resultados que verificar")
            except Exception as e:
                logger.debug("Verificación de frescura falló: %s", e)

        # Sesión 49 — red de seguridad de IDIOMA. Si la respuesta se ha ido al
        # inglés sin que nadie lo pidiera (o es el monólogo interno del modelo,
        # que sale siempre en inglés), se regenera UNA vez pidiéndolo derecho.
        # Cuesta una llamada, y solo en el caso que ya estaba roto.
        if _respuesta_extraviada(response, user_input, _idioma_turno):
            _nombre_idioma = idiomas.nombre_en_espanol(
                _idioma_turno or idiomas.detectar(user_input) or "es")
            logger.info("Respuesta en otro idioma del pedido (%s) → regenerando",
                        _nombre_idioma)
            try:
                _msgs_es = [
                    {"role": "system", "content": (
                        "Eres Celestia. Contesta al usuario DIRECTAMENTE y ENTERA "
                        f"en {_nombre_idioma}, en tono cercano. No escribas tu "
                        "razonamiento, ni pasos internos, ni hables de estas "
                        "instrucciones: solo la respuesta.")},
                    {"role": "user", "content": (
                        f"{user_input}" + (f"\n\n[Información de internet:\n{web_context[:1400]}]"
                                          if web_context else ""))},
                ]
                actividad.marcar("escribiendo")
                _es = self.model.generate_from_messages(
                    _msgs_es, max_new_tokens=max_tokens, temperature=0.5,
                    top_p=0.9, stream=False)
                if _es and _es.strip() and not _respuesta_extraviada(
                        _es, user_input, _idioma_turno):
                    response = _es.strip()
                else:
                    logger.warning("La regeneración en español tampoco salió; "
                                   "se entrega lo que hay")
            except Exception as e:
                logger.debug("Regeneración por idioma falló: %s", e)

        # Sesión 52 — red anti-repetición. Contestar dos veces lo mismo, con las
        # mismas palabras, a alguien que ya se ha quejado es el fallo que más
        # cara de máquina le pone. Se regenera UNA vez enseñándole lo que ya
        # dijo; si la segunda también sale calcada, se entrega la que hay (una
        # respuesta repetida es mejor que ninguna).
        _repetida = _ya_lo_dijo(response, _hist)
        if _repetida:
            logger.info("Respuesta repetida (ya la dijo hace poco) → regenerando")
            try:
                _msgs_var = [
                    {"role": "system", "content": (
                        "Eres Celestia. Ya le contestaste esto hace un momento:\n"
                        f"«{_repetida[:400]}»\n"
                        "Repetirlo otra vez suena a máquina y la persona ya se ha "
                        "quejado. Contesta a lo que te dice AHORA con otras "
                        "palabras y aportando algo distinto: si ya pediste "
                        "perdón, no lo vuelvas a pedir; pregunta qué quiere o "
                        "haz lo que te pida. Español de España, dos o tres "
                        "frases, sin listas.")},
                    {"role": "user", "content": user_input},
                ]
                actividad.marcar("escribiendo")
                _var = self.model.generate_from_messages(
                    _msgs_var, max_new_tokens=max_tokens, temperature=0.85,
                    top_p=0.95, stream=False)
                if _var and _var.strip() and not _ya_lo_dijo(_var, _hist):
                    response = _var.strip()
                else:
                    logger.warning("La regeneración también salió repetida; "
                                   "se entrega lo que hay")
            except Exception as e:
                logger.debug("Regeneración por repetición falló: %s", e)

        # Sesión 53 — red anti-muñón. Una respuesta de cuatro palabras a un
        # mensaje que traía algo dentro deja a la persona hablando sola; en el
        # chat real pasó justo cuando ya se había quejado del tono. Se
        # regenera UNA vez pidiendo que conteste de verdad.
        if _es_munon(response, user_input):
            logger.info("Respuesta muñón (%d palabras) → regenerando",
                        len(response.split()))
            try:
                _msgs_largo = [
                    {"role": "system", "content": (
                        "Eres Celestia. Acabas de contestar «"
                        f"{response.strip()[:120]}», que se queda en nada y "
                        "suena a corte. Contesta otra vez a lo que te dice: "
                        "dos o tres frases, con algo de sustancia —lo que "
                        "haces, lo que necesitas saber o lo que le propones—. "
                        "Español de España, sin listas y sin repetir esa "
                        "frase.")},
                    {"role": "user", "content": user_input},
                ]
                actividad.marcar("escribiendo")
                _largo = self.model.generate_from_messages(
                    _msgs_largo, max_new_tokens=max_tokens, temperature=0.8,
                    top_p=0.95, stream=False)
                if _largo and not _es_munon(_largo, user_input):
                    response = _largo.strip()
                else:
                    logger.warning("La regeneración también salió corta; "
                                   "se entrega lo que hay")
            except Exception as e:
                logger.debug("Regeneración por muñón falló: %s", e)

        # Estilo de chat: fuera títulos, separadores y preámbulos de informe.
        response = _estilo_chat(response)

        # Sesión 46 — formato constante. `_estilo_chat` quita la maquetación de
        # informe (títulos ##, separadores, preámbulos); `formato.normalizar`
        # ordena lo que queda: tablas a viñetas, un solo marcador de lista, y la
        # negrita reservada al dato que de verdad importa en vez de repartida por
        # cada nombre y cada precio. Petición del usuario: «no lo ordena como
        # debe y me pierdo». El marcado concreto de cada canal (WhatsApp y
        # Telegram usan *una* estrella, no dos) lo pone `formato.para_canal` a la
        # salida — aquí se guarda la versión canónica, que es la que va a memoria.
        response = formato.normalizar(response)

        # Quitar la coletilla que atribuye al usuario el contexto que ella
        # misma buscó, y devolver la mayúscula al arranque de la frase.
        sin_coletilla = _COLETILLA_CONTEXTO_RE.sub("", response or "", count=1)
        if sin_coletilla != response and len(sin_coletilla) >= 30:
            response = sin_coletilla[0].upper() + sin_coletilla[1:]
        # Y la misma atribución en forma de frase suelta, vaya donde vaya.
        _sin_meta = _COMENTA_EL_CONTEXTO_RE.sub("", response or "")
        if _sin_meta != (response or ""):
            # El colapso de espacios va DENTRO de este if y es horizontal: con
            # `\s{2,}` y fuera del if se comía los saltos de línea de cualquier
            # respuesta, tocara o no la expresión (bug de la sesión 50, repetido).
            _sin_meta = re.sub(r"[ \t]{2,}", " ", _sin_meta).strip()
            if len(_sin_meta) >= 30:
                # Se registra QUÉ se quita: un booleano en el log no deja
                # calibrar si la expresión muerde de más (lección de la S51).
                _fuera = _COMENTA_EL_CONTEXTO_RE.search(response or "")
                logger.info("Comentario sobre el contexto interno — quitado: %r",
                            (_fuera.group(0)[:120] if _fuera else ""))
                response = _sin_meta

        # Cierre limpio: si el proveedor cortó en seco al llegar al tope de
        # tokens, no dejar la frase a medias (ni guardarla así en memoria).
        response = _cerrar_en_frase_completa(response)

        # El monólogo interno «(piensa: …)», fuera ANTES de guardar. El
        # saneador de `api.py` ya lo quitaba de lo que se envía, pero la
        # conversación se persiste unas líneas más abajo y en la base de datos
        # quedaban 9 respuestas con el razonamiento dentro (S64). De ahí salen
        # los ejemplos con los que se entrena al modelo local: se le estaba
        # enseñando a pensar en voz alta delante de Enzo.
        #
        # Si el monólogo era TODO lo que había, se pide una vez más, directa
        # (sesión 74: «¿Me lo dices otra vez?» perdía el hilo). Si tampoco sale
        # nada, se deja como estaba y decide `api.py`: borrarla aquí solo
        # serviría para guardar un turno vacío.
        _sin_monologo = self._sin_monologo_o_reintento(response, messages)
        if _sin_monologo:
            response = _sin_monologo

        # Encargos con letras prohibidas: se cuentan aquí, no se le cree al
        # modelo (portátil, 3 oct 2026: «…calamar y calabaza y cacahuata…»).
        response = self._cumplir_letras(user_input, response, messages)

        _hist.append({"role": "user", "content": user_input})
        # «web» marca que esta respuesta salió de internet: si lo siguiente
        # la corrige o la sigue («Y los go karts?»), se vuelve a buscar
        # (sesión 74). Al modelo solo le llegan role y content.
        # Sesión 74 — enlaces inventados. Solo si este turno tuvo fuente
        # (internet, un [RESULTADO] o una búsqueda forzada): los enlaces cuyo
        # dominio no aparece en ella se quitan. Sin fuente no se toca nada: un
        # enlace de memoria puede ser bueno y no hay con qué compararlo.
        try:
            from . import tools as _tools_mod
            _buscado = _tools_mod.resultados_desde(_t0)
        except Exception:
            _buscado = ""
        if (response and "http" in response
                and (web_context or _buscado
                     or self._BLOQUE_ANEXADO_RE.search(user_input or ""))):
            _sin_inventados = formato.quitar_enlaces_sin_fuente(
                response, "\n".join(p for p in (web_context, user_input, _buscado) if p))
            if _sin_inventados != response:
                logger.info("Enlaces que no venían en lo encontrado — quitados")
                response = _sin_inventados
        # 26 sep 2026 — lo mismo con los títulos: «Avengers: Doomsday» no salía
        # en ningún resultado y lo recomendó como ya estrenada. Se regenera UNA
        # vez nombrándole los inventados; si repite, se queda la que menos trae
        # (Codex pedía exigir cero, pero entonces se entregaría la original,
        # que tiene MÁS inventados que la regenerada).
        if response and web_context:
            _fuente_t = "\n".join(p for p in (web_context, user_input, _buscado) if p)
            _pide_obras = bool(self._PIDE_OBRAS_RE.search(user_input or ""))
            _inventados = formato.titulos_sin_fuente(response, _fuente_t, _pide_obras)
            if _inventados:
                logger.info("Títulos que no venían en lo encontrado → regenerando: %s",
                            ", ".join(_inventados)[:120])
                try:
                    _otra = self.model.generate_from_messages(
                        list(messages) + [{"role": "system", "content": (
                            "Ojo: en tu respuesta nombrabas "
                            + ", ".join(f"«{t}»" for t in _inventados)
                            + ", y eso NO aparece en la información de internet. "
                            "Vuelve a contestar nombrando SOLO lo que sí aparece "
                            "ahí, sin afirmar fechas ni disponibilidad que no "
                            "vengan en ella. Si lo que hay no basta, dilo.")}],
                        max_new_tokens=max_tokens, temperature=0.3,
                        top_p=0.9, stream=False)
                    _otra = (_otra or "").strip()
                    if _otra and (len(formato.titulos_sin_fuente(_otra, _fuente_t,
                                                                 _pide_obras))
                                  < len(_inventados)):
                        response = _otra
                except Exception as e:
                    logger.debug("Regeneración por títulos inventados falló: %s", e)
        # Una orden de herramienta escrita como texto ({"consulta": …}) no es
        # una respuesta: llegó así al chat el 25 sep 2026. El blueprint, al ver
        # esta frase, intenta hacerlo de verdad con el empeño.
        from . import empeno as _empeno
        if _empeno.parece_orden_cruda(response, user_input):
            logger.warning("Respuesta que era una orden cruda — no se da por buena: %.80s",
                           response)
            response = _empeno.RESPUESTA_TRABADA
        # También si el dato llegó como [RESULTADO] (el «empeño» busca así).
        _con_fuente = bool(web_context or self._BLOQUE_ANEXADO_RE.search(user_input or ""))
        _hist.append({"role": "assistant", "content": response,
                      **({"web": True} if _con_fuente else {})})
        max_hist = self.config.CONV_HISTORY_TURNS * 2
        if len(_hist) > max_hist:
            # In situ: reasignar crearía una lista nueva y `_hilos` se quedaría
            # apuntando a la vieja — el chat perdería lo que acaba de decirse.
            del _hist[:-max_hist]

        # coherence + add_conversation a background — no bloquean la respuesta
        backend_actual = getattr(self.model, "_backend", "?") or "?"

        def _persistir_async():
            try:
                coh = self.embedder.similarity(user_input, response)
                self.memory.add_conversation(user_input, response, coh,
                                             hilo=_hilo_turno)
            except Exception as e:
                logger.debug("persistir_async falló: %s", e)
            # Distillation: si respondió el modelo profesor (Groq/OpenRouter),
            # registra el par como teacher signal para futuros entrenamientos
            # del student local (Qwen). Idempotente por hash; descarta basura.
            if backend_actual in ("groq", "openrouter"):
                try:
                    self.aprendizaje_builder.registrar_teacher_signal(
                        prompt=user_input,
                        teacher_response=response,
                        backend_teacher=backend_actual,
                    )
                except Exception as e:
                    logger.debug("teacher_signal falló: %s", e)
        threading.Thread(target=_persistir_async, daemon=True).start()
        # Gate de calidad: solo con backend local pequeño, si la respuesta es de
        # alta perplejidad y contradice el grafo, se marca como baja confianza.
        # No-op con Groq/OpenRouter (se persiste la respuesta cruda, sin aviso).
        response = self._gate_calidad_local(user_input, response)
        logger.info("respond %.2fs ← %.40s", time.time() - _t0, user_input)
        return response

    def _trim_context(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if self.model.tokenizer is None:
            return messages
        try:
            limit = self.model.max_context - self.config.GEN_MAX_TOKENS - 50
            descartados: List[Dict[str, str]] = []
            # messages = [system, ...history..., user_actual]
            # Nunca eliminar messages[0] (system) ni messages[-1] (último user)
            while len(messages) > 2:
                text = self.model.format_chat(messages)
                ids = self.model.tokenizer(text, return_tensors="pt")["input_ids"]
                if ids.shape[1] <= limit:
                    break
                # Hay al menos: [system, algo_del_historial, ..., user_actual]
                # Eliminar el par más antiguo del historial (índices 1 y 2 si es par user/assistant)
                if len(messages) <= 3:
                    # Solo queda system + (posible turno) + user_actual → no recortar más
                    break
                descartados.append(messages.pop(1))
                if len(messages) > 2:
                    descartados.append(messages.pop(1))
            # Sesión 46 — lo descartado deja rastro en el system. Antes los turnos
            # se tiraban en silencio y Celestia se contradecía con lo que ella
            # misma acababa de decir; la red contra eso era duplicar los últimos
            # turnos en el system SIEMPRE, aunque no se recortara nada. Ahora el
            # resumen se paga sólo cuando de verdad se ha recortado algo.
            if descartados:
                lineas = []
                for t in descartados[-4:]:
                    rol = "Usuario" if t.get("role") == "user" else "Celestia"
                    contenido = (t.get("content") or "").strip().replace("\n", " ")
                    if contenido:
                        lineas.append(f"{rol}: {contenido[:200]}")
                if lineas:
                    messages[0] = dict(messages[0])
                    messages[0]["content"] += (
                        "\n\nMEMORIA INMEDIATA (turnos anteriores recortados por "
                        "longitud — debes ser coherente con esto):\n" + "\n".join(lineas)
                    )
                logger.info("Contexto recortado: %d turnos resumidos en el system",
                            len(descartados))
        except Exception:
            pass
        return messages

    # Encabezados de las secciones inyectadas en el system prompt, ordenadas
    # por prioridad de descarte (las primeras se sacrifican antes). El núcleo
    # del system (instrucciones base, arquitectura, perfil, hechos) queda
    # intacto — solo recortamos enriquecimientos recuperados.
    _SECCIONES_DESCARTABLES = [
        "\n\nRecuerdos relevantes de conversaciones anteriores",  # FAISS recall
        "\n\nCONOCIMIENTO RELEVANTE DEL GRAFO:",                  # KG narrativa
        "\n\nMEMORIA INMEDIATA",                                  # últimos turnos
    ]

    def _compactar_system_si_supera(
        self, messages: List[Dict[str, str]], budget_tokens: int = 3500,
    ) -> List[Dict[str, str]]:
        """Recorta el system prompt si pesa más de `budget_tokens`.

        Groq free aplica TPM 6K/min — un primer turno con system gordo (FAISS
        recall + KG + memoria inmediata) ya devuelve 429 antes de generar.
        Esta función mide el system y, si supera el budget, va sacrificando
        secciones por orden de prioridad: FAISS → KG → memoria inmediata.
        """
        if self.model.tokenizer is None or not messages:
            return messages
        try:
            system_msg = messages[0]
            if system_msg.get("role") != "system":
                return messages

            def _tokens(txt: str) -> int:
                return self.model.tokenizer(txt, return_tensors="pt")["input_ids"].shape[1]

            content = system_msg.get("content", "")
            if _tokens(content) <= budget_tokens:
                return messages

            for marca in self._SECCIONES_DESCARTABLES:
                idx = content.find(marca)
                if idx == -1:
                    continue
                # Recortar desde la marca hasta el siguiente "\n\n" de sección
                # superior o final de string.
                rest = content[idx + len(marca):]
                fin_rel = rest.find("\n\n")
                if fin_rel == -1:
                    content = content[:idx].rstrip()
                else:
                    content = content[:idx].rstrip() + content[idx + len(marca) + fin_rel:]
                if _tokens(content) <= budget_tokens:
                    break

            if content != system_msg.get("content", ""):
                logger.info(
                    "system compactado por budget: %d → %d tokens",
                    _tokens(system_msg["content"]), _tokens(content),
                )
                messages[0] = {"role": "system", "content": content}
        except Exception as e:
            logger.debug("_compactar_system_si_supera falló: %s", e)
        return messages

    def _diversity(self, text: str) -> float:
        tokens = text.split()
        if not tokens:
            return 0.0
        return len(set(tokens)) / len(tokens)

    def _compute_drift(self) -> float:
        if len(self.ppl_history) < 2:
            return 0.0
        hist = list(self.ppl_history)
        prev_mean = sum(hist[:-1]) / len(hist[:-1])
        return min(1.0, abs(prev_mean - hist[-1]) / max(1.0, prev_mean))

    # Patrones para aprendizaje silencioso de hechos personales
    _LEARN_PATTERNS = [
        # Comandos explícitos de aprendizaje — máxima prioridad
        (re.compile(r"(?:aprende|recuerda|guarda|anota|memoriza)\s+que\s+(.+?)(?:[.]|$)", re.I), "hecho"),
        (re.compile(r"(?:aprende|recuerda|guarda|anota|memoriza)[:]\s+(.+?)(?:[.]|$)", re.I), "hecho"),
        # Datos personales implícitos
        (re.compile(r"me llamo\s+(\w+)", re.I), "nombre"),
        (re.compile(r"mi\s+(?:nombre\s+es|apodo\s+es)\s+(\w+)", re.I), "nombre"),
        (re.compile(r"tengo\s+(\d+)\s+años", re.I), "edad"),
        (re.compile(r"trabajo\s+(?:en|como|de)\s+(.+?)(?:[.,]|$)", re.I), "trabajo"),
        (re.compile(r"vivo\s+en\s+(.+?)(?:[.,]|$)", re.I), "ubicación"),
        (re.compile(r"(?:me\s+gusta|me\s+encanta)\s+(?:el\s+|la\s+|los\s+|las\s+)?(.+?)(?:[.,]|$)", re.I), "gustos"),
        (re.compile(r"prefiero\s+(.+?)(?:[.,]|$)", re.I), "preferencias"),
        (re.compile(r"soy\s+(?:un\s+|una\s+)?([a-záéíóúüñA-ZÁÉÍÓÚÜÑ]{4,}(?:\s+\w+){0,3})(?:[.,]|$)", re.I), "identidad"),
    ]

    _TAG_RESULT = {
        "hecho":        "Dato a recordar: {}.",
        "nombre":       "El usuario se llama {}.",
        "identidad":    "El usuario es {}.",
        "edad":         "El usuario tiene {} años.",
        "trabajo":      "El usuario trabaja en/como {}.",
        "ubicación":    "El usuario vive en {}.",
        "gustos":       "Al usuario le gusta {}.",
        "preferencias": "El usuario prefiere {}.",
    }

    # Sesión 32 (BUG-S126): blacklist para identidad — estados/condiciones que
    # no deben guardarse como nombre/identidad (alérgico, vegano, etc.).
    _NO_IDENTIDAD = frozenset({
        "alérgico", "alergico", "alérgica", "alergica",
        "diabético", "diabetico", "diabética", "diabetica",
        "hipertenso", "hipertensa", "celíaco", "celiaco",
        "celíaca", "celiaca",
        "vegetariano", "vegetariana", "vegano", "vegana",
        "depresivo", "depresiva", "ansioso", "ansiosa",
        "feliz", "triste", "cansado", "cansada", "enfermo", "enferma",
        "humano", "humana", "persona",
    })

    # Sesión 74 — lo que este aprendizaje guardó en la BD real de Enzo: «Al
    # usuario le gusta cómo me hablas» (de «NO me gusta cómo me hablas»), «le
    # gusta mucho tu sinceridad…», «El usuario es tumadre», «se llama y», «le
    # gusta nada» y «trabaja en/como si es muy físico» (de «el trabajo en sí
    # es muy físico»). Cada regla de abajo es uno de esos casos.
    _NIEGA_ANTES_RE = re.compile(r"\b(?:no|nunca|jam[aá]s|ni)\s+(?:\w+\s+)?$", re.I)
    _VALOR_A_CELESTIA_RE = re.compile(
        r"\b(?:tu|tus|te|ti|contigo|me\s+hablas|me\s+dices|me\s+tratas)\b", re.I)
    _VALOR_VACIO = frozenset({"nada", "eso", "esto", "todo", "algo", "y", "o",
                              "a", "que", "si", "no", "pero", "pues", "bien"})
    _INSULTO_RE = re.compile(
        r"^(?:tu\s*madre|tu\s*padre|tont[oa]|idiota|imb[eé]cil|gilipollas|"
        r"subnormal|est[uú]pid[oa]|pendej[oa]|puta|cabr[oó]n)\b", re.I)

    def _aprendizaje_descartable(self, tag: str, valor: str, texto: str,
                                 inicio: int) -> str:
        """Por qué este dato no debe guardarse, o ''."""
        v = (valor or "").strip().strip(".,;:!?¿¡ ")
        if len(v) < 2 or v.lower() in self._VALOR_VACIO:
            return "valor vacío"
        if self._NIEGA_ANTES_RE.search(texto[max(0, inicio - 15):inicio]):
            return "está negado"
        fin_frase = re.match(r"[^.!?\n]*([.!?]?)", texto[inicio:]).group(1)
        # El «¿» cuenta solo si abre ESTA frase: «¿Sabes qué? Trabajo en…» es
        # una pregunta y después un dato.
        inicio_frase = max(texto.rfind(c, 0, inicio) for c in ".!?\n") + 1
        if fin_frase == "?" or "¿" in texto[inicio_frase:inicio]:
            return "es una pregunta"
        if self._VALOR_A_CELESTIA_RE.search(v):
            return "habla de Celestia"
        if tag == "gustos" and re.match(r"c[oó]mo\b", v, re.I):
            return "comentario del momento"
        if tag == "identidad" and self._INSULTO_RE.search(v):
            return "insulto, no identidad"
        if tag == "trabajo" and (
                re.search(r"\b(?:el|mi|un|este|ese|tu|su|del)\s*$",
                          texto[max(0, inicio - 8):inicio], re.I)
                or re.match(r"s[ií]\b", v, re.I)):
            return "«el trabajo» como nombre, no «trabajo en…»"
        return ""

    def _learn_silently(self, user_input: str):
        """Extrae hechos personales del mensaje del usuario y los guarda en memoria."""
        for pat, tag in self._LEARN_PATTERNS:
            m = pat.search(user_input)
            if m:
                valor = m.group(1).strip()
                # Sesión 32 (BUG-S126): rechazar identidad que sea
                # estado/condición. «soy alérgico al gluten» no es nombre ni
                # identidad — es un dato de salud.
                if tag in ("identidad", "nombre"):
                    primera = valor.lower().split()[0] if valor else ""
                    if primera in self._NO_IDENTIDAD:
                        logger.info("Identidad descartada (estado/condición): %s",
                                    valor)
                        continue
                _motivo = self._aprendizaje_descartable(tag, valor, user_input,
                                                        m.start())
                if _motivo:
                    logger.info("Aprendizaje silencioso descartado [%s] (%s): %s",
                                tag, _motivo, valor)
                    continue
                tmpl  = self._TAG_RESULT.get(tag, "Dato personal ({}): {{}}".format(tag))
                result = tmpl.format(valor)
                logger.info("Aprendizaje silencioso [%s]: %s", tag, valor)
                try:
                    self.memory.add_episode(
                        task=f"[dato personal: {tag}] {valor}",
                        result=result,
                        reflection="dato personal detectado automáticamente",
                        ppl=0.0,
                        drift=0.0,
                        coherence=1.0,
                    )
                except Exception:
                    pass

    def close(self):
        self.memory.close()
        logger.info("Orchestrator cerrado. Memoria guardada.")

