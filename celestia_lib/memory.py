"""Memoria persistente de Celestia: SQLite (relacional + FTS5) + FAISS (semántico).

Extraído del monolito en sesión 15 como parte del refactor mayor.
Depende de Config y Embedder de celestia_lib.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import unicodedata
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .embedder import Embedder

logger = logging.getLogger("celestia_v1")

# Defensa en profundidad de privacidad: una clave de hecho que delate una
# credencial NUNCA debe persistirse, aunque algún extractor (síncrono o LLM)
# lo intente y el valor en sí no tenga formato reconocible (p.ej. "Pizza1234").
_CLAVE_CREDENCIAL_RE = re.compile(
    r"contrase|password|passwd|\bclave\b|\bpin\b|\bcvv\b|\biban\b|tarjeta|"
    r"secret|token|credencial|n[uú]mero\s+de\s+cuenta|"
    # Códigos que abren algo (Codex, 23 sep 2026: «mi código de alarma es
    # 1234» se guardaba en el perfil). El código postal no es secreto.
    r"c[oó]digo\s+(?:de(?:l)?\s+(?:la\s+|el\s+|mi\s+)?)?(?:alarma|acceso|"
    r"portal|puerta|caja\s+fuerte|seguridad|desbloqueo|verificaci[oó]n)|combinaci[oó]n\s+de",
    re.I,
)

# ── Red final: qué merece guardarse como «hecho» ────────────────────────────
# Un hecho es algo que sigue siendo verdad mañana. Los dos extractores (el de
# regex y el del LLM) llenaban la tabla de otra cosa: peticiones del momento
# («quiero un pdf de…», «dame la clasificación de LaLiga»), trozos de frase sin
# sujeto («será para IA y gaming») y muletillas sueltas («profesion: pero»).
# Enseñadas después en «¿qué sabes de mí?», parecían el volcado de una base de
# datos de alguien a quien no conoce.
#
# La comprobación vive AQUÍ, en el único sitio por donde pasan todos los
# caminos de escritura, y no dentro de cada extractor: el extractor decide qué
# mirar, esto decide qué merece quedarse. Y la vista usa esta misma función,
# para que enseñar y guardar no puedan discrepar (esa separación es justo la
# que dejó peticiones viejas a la vista mientras el filtro nuevo las tapaba).

# Muletillas y conectores: solos no son el dato de nadie.
_PALABRAS_SUELTAS = frozenset({
    "pero", "y", "o", "que", "porque", "asi", "así", "asiq", "asique",
    "entonces", "pues", "vale", "ok", "bueno", "nada", "algo", "eso", "esto",
    "aquello", "si", "sí", "no", "tal", "cual", "cosa", "cosas", "hola",
    "adios", "adiós", "gracias", "ya", "aun", "aún", "tambien", "también",
    "ademas", "además", "igual", "quiza", "quizá", "quizas", "quizás",
    "claro", "venga", "anda", "joder", "vaya", "uf", "aja", "ajá",
})

# Lo que se le pide a Celestia. Dos formas: la orden directa («dame…», «hazme
# un resumen») y el deseo en primera persona con algo entregable detrás
# («quiero un pdf de…»). «Quiero ser piloto» o «necesito gafas» NO entran: el
# deseo solo cuenta como petición cuando lo que sigue es algo que ella produce.
_ENTREGABLES = (r"pdf|lista|listado|resumen|res[úu]men|imagen|foto|tabla|"
                r"gr[áa]fico|grafico|c[óo]digo|codigo|receta|informe|documento|"
                r"texto|traducci[óo]n|clasificaci[óo]n|clasificacion|"
                r"an[áa]lisis|analisis|plan|gu[íi]a|guia|ejemplo|script")
_PETICION_A_CELESTIA_RE = re.compile(
    r"^\s*(?:"
    r"(?:me\s+)?(?:quiero|quisiera|querr[íi]a|necesito|me\s+gustar[íi]a)\s+"
    rf"(?:que\s+me\s+\w+\s+)?(?:un[ao]?s?\s+|el\s+|la\s+|los\s+|las\s+)?(?:{_ENTREGABLES})\b"
    r"|d[ae]me\b|d[ií]melo\b|ponme\b|h[áa]zme(?:lo)?\b|"
    r"orden[ae]\b|ord[ée]nam[e]\b|ordename\b|"
    r"puedes\b|podr[íi]as\b|ser[íi]as\s+capaz\b|quiero\s+que\s+(?:me\s+)?\w+"
    r")",
    re.I,
)

# Trozo de una conversación, no una frase que se sostenga sola. Empieza por
# conector o por un verbo sin sujeto: «será para IA y gaming», «pero en este
# chat no puedes…». Fuera de su turno no significan nada.
_FRAGMENTO_RE = re.compile(
    r"^\s*(?:pero|y|o|sino|aunque|adem[áa]s|tambi[ée]n|entonces|luego|"
    r"as[íi]\s+que|es\s+que|ser[áa]|ser[íi]a|era|ser[áa]n|"
    r"por\s+eso|o\s+sea|total|encima)\b",
    re.I,
)

# Va dirigido a Celestia, no describe al usuario: «no me hables en…», «no
# puedes enviarme…», «tu género debe ser…». Como INSTRUCCIÓN es legítimo y se
# respeta; como dato personal o preferencia es una clave mal puesta.
_HABLA_DE_CELESTIA_RE = re.compile(
    r"\b(?:no\s+me\s+(?:hables|digas|cuentes|env[íi]es|mandes)|"
    r"deja\s+de\s+\w+|no\s+puedes\b|no\s+te\s+he\s+pedido|"
    r"tu\s+(?:g[ée]nero|genero|tono|forma|nombre|voz|personalidad)\b|"
    r"debes\s+\w+|tienes\s+que\s+\w+)",
    re.I,
)

# La queja del TRATO («eres muy borde conmigo», «no me gusta cómo me hablas»)
# la usan dos sitios: el orquestador, para cambiar el tono en ese turno, y el
# portero de aquí abajo, para no guardarla como si fuera un dato de la persona
# («habla = no me gusta cómo me hablas» se coló así). Vive en un solo sitio
# porque dos criterios para lo mismo acaban discrepando (lección de la S49).
QUEJA_DE_TRATO_RE = re.compile(
    r"\b(?:eres|est[aá]s|te\s+pones|suenas|has\s+sido|has\s+estado)\s+"
    r"(?:muy\s+|un\s+poco\s+|bastante\s+|s[uú]per\s+|demasiado\s+|re\s+)?"
    r"(?:borde|seca|seco|cortante|brusca|brusco|fr[ií]a|fr[ií]o|"
    r"antip[aá]tic[oa]|desagradable|maleducad[oa]|groser[oa]|pesad[oa]|"
    r"rancia|estirada)\b|"
    r"\bqu[eé]\s+(?:borde|seca|seco|brusca|brusco|fr[ií]a|fr[ií]o|"
    r"antip[aá]tic[oa]|desagradable|pesada|pesado)\s+(?:eres|est[aá]s)\b|"
    r"\bno\s+me\s+gusta\s+(?:c[oó]mo|la\s+forma|la\s+manera|tu\s+tono)\b|"
    r"\bme\s+(?:hablas|contestas|respondes|tratas)\s+(?:mal|fatal|as[ií]|"
    r"de\s+malas)\b|"
    r"\b(?:deja|para)\s+de\s+(?:ser|hablarme|contestarme|tratarme)\b|"
    r"\bes\s+q(?:ue)?\s+no\s+paras\b|"
    r"\bme\s+est[aá]s\s+(?:agobiando|cansando|hartando)\b",
    re.IGNORECASE,
)


# La protesta de un turno concreto («no te he pedido que me digas todo eso»)
# se parece mucho a una instrucción permanente («deja de poner el punto final»),
# y el extractor las confundía. La diferencia está en el tiempo verbal: la queja
# mira a lo que acaba de pasar; la instrucción, a lo que quiere de ahora en
# adelante. Solo la segunda merece guardarse.
_QUEJA_DEL_MOMENTO_RE = re.compile(
    r"^\s*(?:no\s+te\s+he\s+pedido|eso\s+no\s+(?:es|era)\s+lo\s+que|"
    r"ya\s+te\s+(?:lo\s+)?(?:he\s+)?(?:dicho|dije)|"
    r"otra\s+vez\s+(?:lo\s+)?mismo|te\s+(?:he\s+)?dicho\s+que\s+no)\b",
    re.I,
)

# Claves canónicas que describen a la persona. Si el tipo dice «instrucción»,
# la clave está equivocada (así se guardó «pareja = Tu genero debe ser neutro»).
_CLAVES_DE_PERSONA = frozenset({
    "nombre", "apellido", "edad", "cumpleanos", "cumpleaños",
    "fecha_nacimiento", "pareja", "profesion", "profesión", "trabajo",
    "mascota", "ciudad", "pais", "país", "telefono", "teléfono", "email",
})

# Lo que TIENE que aparecer en el valor para que la clave cuadre. Sin esto,
# «cumpleanos = Las medidas son de una husky de casi 2 años» pasa por fecha.
_MESES = (r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
          r"setiembre|octubre|noviembre|diciembre")
_COHERENCIA_VALOR = {
    "cumpleanos": re.compile(rf"\d|{_MESES}", re.I),
    "cumpleaños": re.compile(rf"\d|{_MESES}", re.I),
    "fecha_nacimiento": re.compile(rf"\d|{_MESES}", re.I),
    "edad": re.compile(r"\d"),
    "telefono": re.compile(r"\d{6,}"),
    "teléfono": re.compile(r"\d{6,}"),
    "email": re.compile(r"@"),
}

# Un verbo de mando, solo, no es un dato de nadie. Quedó apuntado «profesión =
# explica» de una frase que empezaba por ahí, y luego se recitaba como si fuera
# el oficio de la persona. Solo cuenta cuando el valor es esa palabra y nada más:
# «profesor de dibujo» o «escribo guiones» son oficios de verdad.
_VERBOS_DE_ORDEN = frozenset({
    "explica", "explicame", "explícame", "dime", "dame", "cuenta", "cuentame",
    "cuéntame", "busca", "buscame", "búscame", "haz", "hazme", "pon", "ponme",
    "mira", "escribe", "traduce", "resume", "calcula", "abre", "cierra",
    "manda", "envia", "envía", "llama", "recuerda", "muestra", "muestrame",
    "muéstrame", "ensename", "enséñame", "ayuda", "ayudame", "ayúdame",
    "dibuja", "crea", "genera", "repite", "sigue", "para", "espera",
})

# Cuántas palabras admite como mucho el valor de una clave de persona. Un dato
# es corto por naturaleza; una frase de veinte palabras es otra cosa.
_MAX_PALABRAS_DATO = 8


# Palabras que no distinguen un hecho de otro: si dos frases solo comparten
# éstas, no hablan de lo mismo.
_VACIAS_COMPARAR = _PALABRAS_SUELTAS | frozenset({
    "para", "por", "con", "sin", "una", "uno", "unos", "unas", "del", "las",
    "los", "que", "como", "muy", "mas", "más", "the", "quiero", "querria",
    "quisiera", "necesito", "gustaria", "gustaría", "poder", "puedo", "tengo",
    "hacer", "haber", "estar", "sobre", "desde", "hasta", "entre", "cuando",
    # De dos letras: se dejan pasar las que sí distinguen («pc», «ia», «tv»),
    # así que las vacías hay que nombrarlas una a una.
    "de", "la", "el", "en", "un", "mi", "tu", "su", "lo", "al", "es", "ni",
    "se", "me", "te", "le", "yo", "ha", "he", "va", "ve", "da",
})


def _tokens_significativos(texto: str) -> set:
    """Las palabras que de verdad dicen de qué va la frase."""
    limpio = unicodedata.normalize("NFKD", (texto or "").lower())
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    # Dos letras basta: «pc» e «ia» son justo lo que distingue un plan de otro.
    return {p for p in re.findall(r"[a-z0-9]+", limpio)
            if len(p) >= 2 and p not in _VACIAS_COMPARAR}


def es_el_mismo_hecho(a: str, b: str) -> bool:
    """True si dos valores son la misma cosa dicha dos veces.

    El extractor guardaba el mismo interés una vez por cada frase en que
    salía: «me quiero montar un pc gaming para 1440p», «quiero un pc para IA
    gama media», «me quiero montar un pc para IA y gaming»… ocho filas para un
    solo plan, que luego se recitaban seguidas. Se comparan las palabras con
    contenido (Jaccard ≥ 0,6): «me gusta el café» y «me gusta el té» comparten
    la parte vacía y no se funden, que es lo que hay que proteger.
    """
    ta, tb = _tokens_significativos(a), _tokens_significativos(b)
    if len(ta) < 3 or len(tb) < 3:
        return False          # frases cortas: sin margen para acertar
    union = ta | tb
    return bool(union) and len(ta & tb) / len(union) >= 0.6


def hecho_es_ruido(tipo: str, clave: str, valor: str) -> str:
    """El motivo por el que esto NO es un hecho, o '' si sí lo es.

    Devuelve texto (no bool) para poder decir en el log por qué se rechaza:
    afinar estas reglas sin saber cuál saltó es a ciegas.
    """
    va = (valor or "").strip()
    cl = (clave or "").strip().lower()
    ti = (tipo or "").strip().lower()
    if not va or not cl:
        return "vacío"

    palabras = va.split()
    if len(palabras) == 1 and va.lower().strip(".,;:!?") in _PALABRAS_SUELTAS:
        return "muletilla suelta"
    if _PETICION_A_CELESTIA_RE.match(va):
        return "petición del momento"
    if _FRAGMENTO_RE.match(va):
        return "trozo de frase sin sujeto"
    if _QUEJA_DEL_MOMENTO_RE.match(va):
        return "protesta de un turno concreto"
    # Quejarse del tono no es contar algo de uno mismo: «no me gusta cómo me
    # hablas» se guardó como «habla = no me gusta cómo me hablas» y pasaba a
    # figurar entre los datos de la persona.
    if QUEJA_DE_TRATO_RE.search(va):
        return "queja del trato, no un dato de la persona"
    if _HABLA_DE_CELESTIA_RE.search(va) and ti != "instruccion_recurrente":
        return "habla de Celestia, no del usuario"
    if ti == "instruccion_recurrente" and cl in _CLAVES_DE_PERSONA:
        return "instrucción guardada bajo una clave de dato personal"
    if cl in _CLAVES_DE_PERSONA:
        if len(palabras) == 1 and va.lower().strip(".,;:!?") in _VERBOS_DE_ORDEN:
            return "una orden, no un dato"
        if len(palabras) > _MAX_PALABRAS_DATO:
            return "demasiado largo para ser un dato"
        patron = _COHERENCIA_VALOR.get(cl)
        if patron is not None and not patron.search(va):
            return f"el valor no parece un(a) {cl}"
    return ""


# Sesión 74 — el grafo guardó «de sitio profesional ni para campeonato» como
# concepto: era media frase de «No hablo de sitio profesional ni para
# campeonato». Un nombre no empieza por preposición ni es una frase entera.
_ARRANQUE_DE_FRASE = frozenset({
    "de", "del", "para", "por", "en", "con", "sin", "a", "al", "ni", "que",
    "y", "o", "pero", "si", "no", "como", "cuando", "donde"})


def entidad_es_ruido(tipo: str, nombre: str) -> str:
    """El motivo por el que esto NO es una entidad del grafo, o '' si lo es."""
    palabras = (nombre or "").strip().split()
    if not palabras:
        return "vacía"
    if palabras[0].lower() in _ARRANQUE_DE_FRASE:
        return "empieza como un trozo de frase"
    if len(palabras) > 5:
        return "es una frase, no un nombre"
    return ""


# Deps opcionales
try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False
    faiss = None  # type: ignore[assignment]

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]


def _con_lock(metodo):
    """Serializa el método con el RLock de la instancia.

    Equivale a envolver TODO el cuerpo del método en `with self._lock:`, pero
    de forma DRY y sin re-indentar métodos largos (menos propenso a errores).
    Se aplica a CUALQUIER método que haga INSERT/UPDATE/DELETE o que toque
    faiss_index / faiss_meta / episode_count. El lock es un RLock reentrante,
    así que es seguro que un método decorado (p. ej. add_conversation) llame a
    otro decorado (p. ej. _save_faiss) sin deadlock.
    """
    @functools.wraps(metodo)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return metodo(self, *args, **kwargs)
    return wrapper


class MemoryDB:
    """Memoria persistente de Celestia. Combina SQLite (relacional + FTS5) con
    FAISS (semántico) para recall de conversaciones y episodios.

    Tablas: conversations, episodes, errores, aprendizajes, hechos_usuario,
    auto_reflexiones, salud_medicaciones, salud_sintomas, salud_habitos.

    Al arrancar: hidrata FTS5 si vacío, carga short_mem desde SQLite, cierra
    aprendizajes huérfanos (en_curso > 30 min), activa WAL para concurrencia.

    short_mem: deque RAM con los últimos N episodios para retrieve_similar rápido.
    """
    def __init__(self, config: Config, embedder: Embedder):
        self.config = config
        self.embedder = embedder
        # Serializa escrituras a SQLite + FAISS entre threads (handler /mensaje,
        # watchdog, auto-reflexión, backup). WAL + busy_timeout cubren la
        # contención ENTRE conexiones SQLite, pero NO (a) el mismo objeto
        # Connection usado por dos threads ni (b) FAISS (no thread-safe en
        # escritura → segfault/índice corrupto). RLock (reentrante) porque
        # add_* puede llamar a _save_faiss() u otros métodos que también lockean.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        # WAL: lecturas no bloquean escrituras y viceversa (crítico para watchdog +
        # auto-reflexión + endpoint /mensaje accediendo en paralelo). synchronous=NORMAL
        # es suficiente — WAL ya garantiza durability ante crashes del proceso.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            # busy_timeout: WAL no elimina la contención writer-writer. Sin este
            # PRAGMA, dos threads escribiendo a la vez disparan "database is
            # locked" (visto en logs: registrar_error desde watchdog vs handler
            # /mensaje). Con 5 s SQLite reintenta internamente antes de fallar.
            # Sesión 31: subido de 5s → 15s. Bajo carga real (extractor de
            # hechos + watchdog + auto-reflexión paralelos en el mismo
            # proceso) 5s no bastaba y el upsert de hechos se descartaba
            # con "database is locked", dejando hechos antiguos contradictorios
            # vigentes (caso: "perro Toby" no actualizaba "mascota").
            self.conn.execute("PRAGMA busy_timeout=15000")
        except Exception as _e:
            logger.warning("No se pudo aplicar PRAGMA WAL: %s", _e)
        self._init_schema()
        self.short_mem: deque = deque(maxlen=config.SHORT_MEM_LIMIT)
        self.faiss_index = None
        self.faiss_meta: Dict[str, Any] = {}
        self.episode_count = 0
        self._load_faiss()
        self._hidratar_fts()
        self._hidratar_short_mem()
        self._cerrar_aprendizajes_huerfanos()
        self._iniciar_backup_diario()
        logger.info("MemoryDB en %s (short_mem: %d entradas, fts5: %s)",
                     config.DB_PATH, len(self.short_mem),
                     "✓" if self._has_fts5 else "✗")

    def _iniciar_backup_diario(self) -> None:
        """Lanza thread daemon que cada 24h hace backup del DB con rotación 7 días.
        Hecho con `sqlite3 .backup` (atomic) en lugar de copia raw para no corromper
        si hay escritura concurrente. Backups en memoria/backups/celestia_YYYYMMDD.db.

        Skipped si DB es ':memory:' (tests) o si el path tiene caracteres especiales.
        """
        import threading as _t
        # No iniciar backup para DBs en memoria (tests). El path resuelve a '.'.
        if self.config.DB_PATH == ":memory:" or self.config.DB_PATH.startswith(":"):
            return
        try:
            backup_dir = Path(self.config.DB_PATH).parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("No pude crear directorio de backups: %s", e)
            return

        def _loop():
            # Esperar 60s antes del primer backup (que arranque normal)
            time.sleep(60)
            while True:
                try:
                    fecha = datetime.now().strftime("%Y%m%d")
                    destino = backup_dir / f"celestia_{fecha}.db"
                    # Usar la API SQLite oficial de backup (atomic, hot copy)
                    with sqlite3.connect(str(destino)) as dst:
                        self.conn.backup(dst)
                    logger.info("Backup DB → %s (%d KB)", destino.name,
                                  destino.stat().st_size // 1024)
                    # Rotación: conservar últimos 7
                    backups = sorted(backup_dir.glob("celestia_*.db"))
                    for viejo in backups[:-7]:
                        try: viejo.unlink()
                        except OSError: pass
                except Exception as e:
                    logger.warning("Backup DB falló: %s", e)
                # Próximo backup en 24h
                time.sleep(24 * 3600)

        _t.Thread(target=_loop, daemon=True, name="memorydb-backup").start()

    @_con_lock
    def _cerrar_aprendizajes_huerfanos(self, timeout_seg: int = 1800) -> None:
        """Marca como terminado_error los aprendizajes que quedaron 'en_curso' tras
        un crash previo. Se ejecuta al arrancar para que la introspección
        ('qué estás aprendiendo') refleje la realidad y no entradas zombie."""
        try:
            cutoff = time.time() - timeout_seg
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE aprendizajes SET estado='terminado_error', "
                "fin_ts=?, resultado='cerrado al arrancar — sesión previa interrumpida' "
                "WHERE estado='en_curso' AND inicio_ts < ?",
                (time.time(), cutoff),
            )
            n = cur.rowcount
            self.conn.commit()
            if n:
                logger.info("Cerrados %d aprendizajes huérfanos (en_curso > %ds)", n, timeout_seg)
        except Exception as e:
            logger.warning("_cerrar_aprendizajes_huerfanos falló: %s", e)

    def _hidratar_fts(self) -> None:
        """Si FTS5 está activo pero vacío (primer arranque tras añadirlo), rellénalo
        con todas las conversaciones existentes para que se puedan recuperar.
        """
        if not getattr(self, "_has_fts5", False):
            return
        try:
            cur = self.conn.cursor()
            n_fts = cur.execute("SELECT COUNT(*) FROM conversations_fts").fetchone()[0]
            n_conv = cur.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            if n_conv > 0 and n_fts < n_conv:
                logger.info("Hidratando FTS5: %d conversaciones pendientes", n_conv - n_fts)
                cur.execute("INSERT INTO conversations_fts(conversations_fts) VALUES ('rebuild')")
                self.conn.commit()
        except Exception as e:
            # En debug no se ve: la búsqueda de conversaciones se queda coja y
            # nadie se entera hasta que Celestia «no recuerda» algo.
            logger.warning("Hidratación FTS falló (la búsqueda por texto queda coja): %s", e)

    def _hidratar_short_mem(self) -> None:
        """Rellena short_mem al arrancar con las últimas conversaciones y episodios.
        Así el contexto reciente sobrevive a reinicios.
        """
        try:
            cur = self.conn.cursor()
            # Mezclar conversations + episodes ordenadas por ts desc
            limit = self.config.SHORT_MEM_LIMIT
            # Conversations recientes
            cur.execute(
                "SELECT user_input, ai_response, ts, coherence "
                "FROM conversations ORDER BY ts DESC LIMIT ?", (limit,)
            )
            convs = [
                {
                    "task":      f"[conv] {r[0] or ''}",
                    "result":    r[1] or "",
                    "ts":        r[2],
                    "coherence": r[3] or 0.0,
                }
                for r in cur.fetchall()
            ]
            # Episodes recientes
            cur.execute(
                "SELECT task, result, ppl, drift, coherence, ts "
                "FROM episodes ORDER BY ts DESC LIMIT ?", (limit,)
            )
            eps = [
                {
                    "task": r[0] or "", "result": r[1] or "",
                    "ppl": r[2], "drift": r[3], "coherence": r[4], "ts": r[5],
                }
                for r in cur.fetchall()
            ]
            # Combinar por timestamp descendente y poblar
            combinados = sorted(convs + eps, key=lambda x: x["ts"] or 0, reverse=True)
            for ep in combinados[:limit]:
                self.short_mem.append(ep)  # append por orden cronológico inverso
        except Exception as e:
            logger.debug("Hidratación de short_mem falló: %s", e)

    # Columnas que se han ido añadiendo con el tiempo: (tabla, columna, tipo).
    # `CREATE TABLE IF NOT EXISTS` no modifica una tabla que ya existe, así que
    # sin esto una mejora solo llegaría a las instalaciones nuevas y dejaría
    # rotas las que ya tienen datos dentro. Añadir aquí y nada más: SQLite hace
    # el ALTER en microsegundos y las filas viejas quedan a NULL.
    _COLUMNAS_NUEVAS = (
        ("conversations", "hilo", "TEXT"),   # sesión 54: varios chats a la vez
    )

    def _migrar_esquema(self, cur):
        """Pone al día una base creada por una versión anterior."""
        for tabla, columna, tipo in self._COLUMNAS_NUEVAS:
            try:
                existentes = {r[1] for r in cur.execute(f"PRAGMA table_info({tabla})")}
                if not existentes:
                    continue          # la tabla aún no existe: la crea el script
                if columna in existentes:
                    continue
                cur.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
                logger.info("Base de datos al día: %s.%s añadida", tabla, columna)
            except Exception as e:
                # Que no arranque por una migración sería peor que la migración.
                logger.warning("No pude añadir %s.%s: %s", tabla, columna, e)

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_input TEXT,
            ai_response TEXT,
            ts REAL,
            coherence REAL,
            hilo TEXT
        );
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT,
            result TEXT,
            reflection TEXT,
            ppl REAL,
            drift REAL,
            coherence REAL,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT,
            priority INTEGER DEFAULT 5,
            status TEXT DEFAULT 'pending',
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS hparams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            temp REAL,
            top_k INTEGER,
            rep_penalty REAL,
            reason TEXT
        );
        -- ─── Auto-conocimiento ──────────────────────────────────────
        CREATE TABLE IF NOT EXISTS errores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            contexto TEXT,    -- dónde sucedió (función, endpoint)
            tipo TEXT,        -- tipo de excepción o categoría
            mensaje TEXT,     -- mensaje de error
            accion TEXT       -- qué hizo Celestia para recuperarse
        );
        CREATE TABLE IF NOT EXISTS aprendizajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tarea TEXT,
            estado TEXT,      -- 'en_curso' | 'terminado_ok' | 'terminado_error'
            intentos INTEGER,
            inicio_ts REAL,
            fin_ts REAL,
            resultado TEXT,
            ruta_skill TEXT   -- ruta del .py guardado si éxito
        );
        CREATE TABLE IF NOT EXISTS hechos_usuario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            tipo TEXT,        -- 'preferencia' | 'dato_personal' | 'instruccion_recurrente'
            clave TEXT,       -- ej: 'tono', 'horario_dormir'
            valor TEXT
        );
        CREATE TABLE IF NOT EXISTS auto_reflexiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            periodo TEXT,     -- 'diaria' | 'semanal' | 'ad-hoc'
            resumen TEXT,     -- resumen narrativo del periodo
            fallos_detectados TEXT,   -- JSON con patrones
            acciones_propuestas TEXT  -- JSON con plan de mejora
        );
        -- Asistente de salud básico (fase 3 del roadmap)
        CREATE TABLE IF NOT EXISTS salud_medicaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,           -- "ibuprofeno 400mg"
            horario TEXT,                    -- "08:00,14:00,20:00" (CSV) o "cada 8h"
            dias TEXT,                       -- "diario" o "L,M,X" o ISO dates
            notas TEXT,
            activa INTEGER DEFAULT 1,        -- 1 si vigente, 0 si pausada
            creada_ts REAL
        );
        CREATE TABLE IF NOT EXISTS salud_sintomas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            sintoma TEXT NOT NULL,           -- "dolor de cabeza", "mareo", etc.
            intensidad INTEGER,              -- 1-10
            notas TEXT,                      -- contexto: "tras comer", "al despertar"
            relacion TEXT                    -- JSON: ["medicacion:ibuprofeno"]
        );
        CREATE TABLE IF NOT EXISTS salud_habitos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            habito TEXT NOT NULL,            -- "agua", "ejercicio", "sueño_h"
            valor REAL                       -- vasos, minutos, horas, etc.
        );
        CREATE INDEX IF NOT EXISTS idx_errores_ts ON errores(ts);
        CREATE INDEX IF NOT EXISTS idx_aprendizajes_estado ON aprendizajes(estado);
        CREATE INDEX IF NOT EXISTS idx_aprendizajes_inicio_ts ON aprendizajes(inicio_ts);
        CREATE INDEX IF NOT EXISTS idx_conversations_ts ON conversations(ts);
        CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts);
        CREATE INDEX IF NOT EXISTS idx_hechos_tipo_clave ON hechos_usuario(tipo, clave);
        CREATE INDEX IF NOT EXISTS idx_hechos_ts ON hechos_usuario(ts);
        CREATE INDEX IF NOT EXISTS idx_errores_tipo ON errores(tipo);
        CREATE INDEX IF NOT EXISTS idx_reflexiones_ts ON auto_reflexiones(ts);
        """)
        self._migrar_esquema(cur)
        # FTS5 sobre conversations (full-text search con normalización de tildes).
        # Si FTS5 no está disponible en esta build de SQLite, caemos a LIKE.
        self._has_fts5 = False
        try:
            cur.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                user_input, ai_response, ts UNINDEXED, coherence UNINDEXED,
                content='conversations', content_rowid='id',
                tokenize="unicode61 remove_diacritics 2"
            );
            CREATE TRIGGER IF NOT EXISTS conv_ai AFTER INSERT ON conversations BEGIN
                INSERT INTO conversations_fts(rowid, user_input, ai_response, ts, coherence)
                VALUES (new.id, new.user_input, new.ai_response, new.ts, new.coherence);
            END;
            CREATE TRIGGER IF NOT EXISTS conv_ad AFTER DELETE ON conversations BEGIN
                INSERT INTO conversations_fts(conversations_fts, rowid, user_input, ai_response)
                VALUES ('delete', old.id, old.user_input, old.ai_response);
            END;
            """)
            self._has_fts5 = True
        except Exception as e:
            logger.info("FTS5 no disponible (%s) — usando LIKE como respaldo", e)
        cur.execute("SELECT COUNT(*) FROM goals")
        if cur.fetchone()[0] == 0:
            now = time.time()
            goals = [
                ("Mejorar coherencia de respuestas", 9, now),
                ("Optimizar uso de memoria episódica", 7, now),
                ("Reducir perplexity en tareas frecuentes", 7, now),
                ("Mantener diversidad de respuestas", 6, now),
            ]
            cur.executemany(
                "INSERT INTO goals (goal,priority,status,created_at,updated_at) VALUES (?,?,'pending',?,?)",
                [(g, p, t, t) for g, p, t in goals],
            )
        self.conn.commit()

    def _load_faiss(self):
        if not _HAS_FAISS or not _HAS_NUMPY or not self.embedder.model:
            return
        try:
            if Path(self.config.FAISS_INDEX).exists() and Path(self.config.FAISS_META).exists():
                self.faiss_index = faiss.read_index(self.config.FAISS_INDEX)
                with open(self.config.FAISS_META, "r", encoding="utf-8") as f:
                    self.faiss_meta = json.load(f)
                self.episode_count = len(self.faiss_meta)
                logger.info("FAISS cargado: %d vectores", self.episode_count)
        except Exception as e:
            logger.warning("FAISS carga fallida: %s", e)

    @_con_lock
    def _save_faiss(self):
        if not _HAS_FAISS or self.faiss_index is None:
            return
        # Índice y metadatos se escriben por separado: si el móvil corta en
        # medio quedan descasados (un índice nuevo con los nombres viejos) y la
        # memoria devuelve recuerdos que no son. Con temporales, o entran los
        # dos o no entra ninguno.
        tmp_idx = str(self.config.FAISS_INDEX) + ".tmp"
        tmp_meta = str(self.config.FAISS_META) + ".tmp"
        try:
            faiss.write_index(self.faiss_index, tmp_idx)
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(self.faiss_meta, f, ensure_ascii=False)
            os.replace(tmp_idx, self.config.FAISS_INDEX)
            os.replace(tmp_meta, self.config.FAISS_META)
        except Exception as e:
            logger.warning("FAISS guardado fallido: %s", e)
            for t in (tmp_idx, tmp_meta):
                try:
                    os.unlink(t)
                except OSError:
                    pass

    @_con_lock
    def add_episode(self, task: str, result: str, reflection: str,
                    ppl: float, drift: float, coherence: float):
        """Persiste un episodio (task→result→reflection) con sus métricas.

        Episodios son entradas del modo autónomo (no conversaciones WhatsApp).
        Se indexa en FAISS para retrieve_similar.
        """
        now = time.time()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO episodes (task,result,reflection,ppl,drift,coherence,ts) VALUES (?,?,?,?,?,?,?)",
            (task[:1000], result[:4000], reflection[:2000],
             float(ppl), float(drift), float(coherence), now),
        )
        self.conn.commit()

        ep = {"task": task, "result": result, "ppl": ppl,
              "drift": drift, "coherence": coherence, "ts": now}
        self.short_mem.appendleft(ep)

        if _HAS_FAISS and _HAS_NUMPY and self.embedder.model:
            vec = self.embedder.encode(task + " " + result[:200])
            if vec is not None:
                if self.faiss_index is None:
                    self.faiss_index = faiss.IndexFlatL2(self.embedder.dim)
                    self.faiss_meta = {}
                self.faiss_index.add(vec.reshape(1, -1))
                self.faiss_meta[str(self.episode_count)] = {
                    "task": task[:200], "result": result[:300], "ts": now, "coh": coherence
                }
                self.episode_count += 1
                if self.episode_count % self.config.FAISS_SAVE_EVERY == 0:
                    self._save_faiss()

    @_con_lock
    def add_conversation(self, user: str, ai: str, coherence: float,
                         hilo: Optional[str] = None):
        """Persiste un par (user, ai) en SQLite, short_mem y FAISS.

        FTS5 se actualiza automáticamente vía triggers. coherence se usa
        en el re-rank semántico (conversaciones con coherence alta pesan más).
        `hilo` dice a qué chat pertenece; lo de antes de que existieran los
        chats se queda en NULL y cuenta como el de siempre.
        """
        now = time.time()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO conversations (user_input,ai_response,ts,coherence,hilo) "
            "VALUES (?,?,?,?,?)",
            (user[:2000], ai[:4000], now, float(coherence),
             (hilo or None) and str(hilo)[:64]),
        )
        self.conn.commit()

        # También al short_mem (RAM, para recuperación inmediata)
        ep = {
            "task":      f"[conv] {user[:200]}",
            "result":    ai[:300],
            "ts":        now,
            "coherence": float(coherence),
        }
        self.short_mem.appendleft(ep)

        # Y al FAISS — para que se recupere por similitud entre sesiones
        if _HAS_FAISS and _HAS_NUMPY and self.embedder.model:
            try:
                vec = self.embedder.encode(user + " " + ai[:200])
                if vec is not None:
                    if self.faiss_index is None:
                        self.faiss_index = faiss.IndexFlatL2(self.embedder.dim)
                        self.faiss_meta = {}
                    self.faiss_index.add(vec.reshape(1, -1))
                    self.faiss_meta[str(self.episode_count)] = {
                        "task":   f"[conv] {user[:200]}",
                        "result": ai[:300],
                        "ts":     now,
                        "coh":    float(coherence),
                    }
                    self.episode_count += 1
                    if self.episode_count % self.config.FAISS_SAVE_EVERY == 0:
                        self._save_faiss()
            except Exception as e:
                logger.debug("FAISS indexado de conversación falló: %s", e)

    # Stopwords en español para limpiar queries antes de buscar
    _STOPWORDS_ES = frozenset({
        "el","la","los","las","un","una","unos","unas","del","de","al","a","y","o","u",
        "que","qué","como","cómo","cuando","cuándo","donde","dónde","quien","quién",
        "para","por","con","sin","sobre","entre","desde","hasta","hacia","contra",
        "mi","tu","su","mis","tus","sus","me","te","se","nos","os","le","les","lo",
        "es","ser","fue","era","eres","soy","sos","son","están","está","estás","estoy",
        "han","hay","ha","he","has","había","habían","habrá",
        "muy","más","mas","menos","pero","porque","si","sí","no","ni","ya",
        "yo","tú","él","ella","ellos","ellas","nosotros","vosotros","usted","ustedes",
        "este","esta","esto","ese","esa","eso","aquel","aquella","aquello",
        "todo","toda","todos","todas","mucho","mucha","muchos","muchas","poco","poca",
        "algo","alguien","nada","nadie","cada","otro","otra","mismo","misma",
        "aquí","ahí","allí","ahora","luego","entonces","hoy","ayer","mañana",
        "también","tambien","solo","sólo","tan","tanto","cuanto","cuánto",
        "voy","vamos","viene","vienen","hace","hizo","hacer","hecho",
        "puedo","puedes","puede","pueden","podemos","poder",
    })

    def _tokenize_query(self, query: str) -> List[str]:
        return [w for w in re.findall(r"\w{3,}", query.lower())
                if w not in self._STOPWORDS_ES][:8]

    def _buscar_fts(self, query: str, k: int) -> List[Dict[str, Any]]:
        """Busca conversaciones en FTS5 (búsqueda full-text con normalización + BM25)."""
        if not getattr(self, "_has_fts5", False):
            return []
        palabras = self._tokenize_query(query)
        if not palabras:
            return []
        # OR entre palabras (cualquiera puede matchear, BM25 los ordena)
        fts_query = " OR ".join(palabras)
        out: List[Dict[str, Any]] = []
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT user_input, ai_response, ts, coherence, rank "
                "FROM conversations_fts WHERE conversations_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, k * 3),
            )
            for u, a, ts, coh, rank in cur.fetchall():
                # rank de FTS5 es negativo (más negativo = mejor). Convertir a [0,1].
                # BM25 típico va de -10 a 0; mapeamos a similitud.
                bm25_norm = max(0.0, min(1.0, -float(rank) / 10.0))
                out.append({
                    "task":      f"[conv] {u or ''}",
                    "result":    a or "",
                    "ts":        ts, "coherence": coh or 0.0,
                    "_score_fts": bm25_norm,
                })
        except Exception as e:
            logger.debug("FTS5 search falló: %s", e)
        return out

    def _buscar_like(self, query: str, k: int) -> List[Dict[str, Any]]:
        """Respaldo cuando FTS5 no está. Busca en user_input + ai_response."""
        palabras = self._tokenize_query(query)
        if not palabras:
            return []
        out: List[Dict[str, Any]] = []
        try:
            cur = self.conn.cursor()
            clauses = " OR ".join(
                ["LOWER(user_input) LIKE ?", "LOWER(ai_response) LIKE ?"] * len(palabras)
            )
            params: List[Any] = []
            for w in palabras:
                params.append(f"%{w}%"); params.append(f"%{w}%")
            cur.execute(
                f"SELECT user_input, ai_response, ts, coherence FROM conversations "
                f"WHERE {clauses} ORDER BY ts DESC LIMIT ?",
                params + [k * 3],
            )
            for u, a, ts, coh in cur.fetchall():
                texto_full = f"{(u or '').lower()} {(a or '').lower()}"
                matches = sum(1 for w in palabras if w in texto_full)
                score = matches / max(1, len(palabras))
                out.append({
                    "task":      f"[conv] {u or ''}",
                    "result":    a or "",
                    "ts":        ts, "coherence": coh or 0.0,
                    "_score_lex": score,
                })
        except Exception as e:
            # Tragado en silencio, un fallo de SQLite se ve igual que «no hay
            # nada que recordar»: son cosas muy distintas.
            logger.warning("Búsqueda léxica fallida (devuelvo %d de los que había): %s", len(out), e)
        return out

    @staticmethod
    def _decay_temporal(ts: Optional[float], vida_dias: float = 90.0) -> float:
        """Decaimiento exponencial suave. Recientes ≈ 1.0, antiguos no llegan a 0."""
        if not ts:
            return 0.85  # neutral si no hay timestamp
        edad_dias = max(0.0, (time.time() - ts) / 86400.0)
        # Mantenemos un piso de 0.4 para que lo viejo no se pierda totalmente
        return 0.4 + 0.6 * math.exp(-edad_dias / vida_dias)

    # Tope duro de candidatos a re-rankear (latencia bajo control)
    _RERANK_MAX_CANDIDATOS = 10

    def _rerank_semantico(self, candidatos: List[Dict[str, Any]],
                            query: str, k: int) -> List[Dict[str, Any]]:
        """Recalcula similitud semántica real para los top candidatos.
        Combina: similitud semántica * decay temporal * boost por coherencia.
        Optimización: encoding en batch (1 llamada al embedder en vez de N).
        """
        if not candidatos:
            return []

        # Deduplicar por task ANTES del encoding pesado
        vistos, dedup = set(), []
        for c in candidatos:
            key = c.get("task", "")[:60]
            if key and key not in vistos:
                vistos.add(key)
                dedup.append(c)
        # Cap duro para no pasar de N al embedder
        dedup = dedup[: self._RERANK_MAX_CANDIDATOS]

        textos = [(c.get("task", "") + " " + c.get("result", ""))[:500] for c in dedup]
        sims: List[float] = [0.0] * len(dedup)

        # Encoding en batch (sentence-transformers acepta lista — 1 call para todos)
        if self.embedder.model and _HAS_NUMPY:
            try:
                qv = self.embedder.model.encode(query, convert_to_numpy=True).astype("float32")
                tvs = self.embedder.model.encode(textos, convert_to_numpy=True, batch_size=32).astype("float32")
                # Norma vectorizada
                q_norm = float(np.linalg.norm(qv) + 1e-9)
                t_norms = np.linalg.norm(tvs, axis=1) + 1e-9
                dots = tvs @ qv
                cos_sims = dots / (t_norms * q_norm)
                sims = [max(0.0, float(s)) for s in cos_sims]
            except Exception as e:
                logger.debug("Rerank batch falló, fallback Jaccard: %s", e)
                sims = [Embedder._jaccard(query, t) for t in textos]
        else:
            sims = [Embedder._jaccard(query, t) for t in textos]

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for cand, sim in zip(dedup, sims):
            lex = cand.get("_score_fts") or cand.get("_score_lex") or 0.0
            base = 0.6 * sim + 0.4 * lex if lex else sim
            decay = self._decay_temporal(cand.get("ts"))
            coh_boost = 1.0 + 0.2 * max(0.0, min(1.0, cand.get("coherence", 0.0)))
            scored.append((base * decay * coh_boost, cand))

        scored.sort(reverse=True, key=lambda x: x[0])
        out: List[Dict[str, Any]] = []
        for _, ep in scored[:k]:
            ep.pop("_score_fts", None)
            ep.pop("_score_lex", None)
            out.append(ep)
        return out

    # Mensajes muy cortos/triviales no necesitan memoria contextual — skip rápido
    _TRIVIAL_QUERY_RE = re.compile(
        r"^\s*(?:hola|hi|hey|buenas|buenos\s+d[ií]as|buenas\s+(?:tardes|noches)|"
        r"gracias|ok|okay|vale|listo|perfecto|claro|s[ií]|no|"
        r"qu[eé]\s+tal|c[oó]mo\s+(?:est[aá]s|va)|me\s+alegro|"
        r"adi[oó]s|chao|hasta\s+luego|(?:ja){2,}|(?:je){2,}|(?:ji){2,}|(?:jo){2,})[\s\.!?¿¡]*$",
        re.I,
    )

    def retrieve_similar(self, query: str, k: int = 5) -> List[Dict]:
        """Recupera los k episodios/conversaciones más relevantes para `query`.

        Combina tres fuentes:
          - FTS5 (BM25 lexical sobre conversations)
          - FAISS (similitud vectorial con embedder)
          - short_mem (deque RAM con los últimos N episodios)

        Aplica decay temporal (90 días) y boost por coherencia. Si la query
        es trivial (≤3 palabras o matchea `_TRIVIAL_QUERY_RE`) devuelve [].

        Cap de query: si supera 25 palabras o 200 chars, se trunca. Queries
        muy largas no aportan mejor recall en FTS y aumentan el coste del
        embedding ~10x sin mejorar la calidad.
        """
        # Mensajes triviales/muy cortos → no buscar memoria (ahorra ~3s/turno)
        if len(query.split()) <= 3 or self._TRIVIAL_QUERY_RE.match(query):
            return []
        # Cap de longitud: tokens del embedder + BM25 saturan en frases largas
        palabras = query.split()
        if len(palabras) > 25:
            query = " ".join(palabras[:25])
        if len(query) > 200:
            query = query[:200]

        # 1. FTS5 PRIMERO — rápido (10-50ms), BM25 ya es buen ranking
        candidatos_fts = self._buscar_fts(query, k) if getattr(self, "_has_fts5", False) else self._buscar_like(query, k)

        # ATAJO: si FTS dio resultados con score alto, devolverlos sin pasar por rerank semántico.
        # El rerank con embedder en CPU cuesta 1-3s; FTS BM25 sin rerank ya es bastante decente
        # y con decay temporal aplicado da resultados aceptables al 90% de queries.
        if candidatos_fts:
            top_score = candidatos_fts[0].get("_score_fts", 0.0)
            if top_score >= 0.45:  # confianza alta en BM25 → no necesita rerank
                # Aplicar decay temporal manualmente para que lo viejo no domine
                for c in candidatos_fts:
                    c["_score_final"] = c.get("_score_fts", 0.0) * self._decay_temporal(c.get("ts"))
                candidatos_fts.sort(key=lambda c: c.get("_score_final", 0), reverse=True)
                for c in candidatos_fts[:k]:
                    c.pop("_score_fts", None)
                    c.pop("_score_final", None)
                return candidatos_fts[:k]

        # 2. Caso difícil: FTS no dio buena señal → entonces sí hacemos el rerank con embedder
        candidatos: List[Dict[str, Any]] = list(candidatos_fts)

        # Memoria reciente (short_mem)
        for ep in self.short_mem:
            candidatos.append({
                "task":      ep.get("task", ""),
                "result":    ep.get("result", ""),
                "ts":        ep.get("ts"),
                "coherence": ep.get("coherence", 0.0),
            })

        # FAISS — episodios vectorizados
        if _HAS_FAISS and _HAS_NUMPY and self.faiss_index is not None and self.embedder.model:
            try:
                qv = self.embedder.encode(query)  # encode fuera del lock (lento, no toca estado compartido)
                if qv is not None:
                    # search + lectura de faiss_meta bajo el lock: FAISS no es
                    # thread-safe y buscar durante un .add() concurrente puede
                    # segfaultear o leer meta inconsistente.
                    with self._lock:
                        D, I = self.faiss_index.search(qv.reshape(1, -1), k * 3)
                        metas = [self.faiss_meta.get(str(int(idx)))
                                 for idx in I[0] if idx >= 0]
                    for meta in metas:
                        if not meta:
                            continue
                        candidatos.append({
                            "task":      meta.get("task", ""),
                            "result":    meta.get("result", meta.get("task", "")),
                            "ts":        meta.get("ts"),
                            "coherence": meta.get("coh", 0.0),
                        })
            except Exception:
                pass

        # Rerank semántico (con cap duro a _RERANK_MAX_CANDIDATOS)
        return self._rerank_semantico(candidatos, query, k)

    def recent_episodes(self, n: int = 10) -> List[Tuple]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT task, ppl, drift, coherence, ts FROM episodes ORDER BY ts DESC LIMIT ?", (n,)
        )
        return cur.fetchall()

    def recent_conversations(self, n: int = 5) -> List[Tuple]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT user_input, ai_response, ts FROM conversations ORDER BY ts DESC LIMIT ?", (n,)
        )
        return cur.fetchall()

    def close(self):
        self._save_faiss()
        self.conn.close()

    # ── Auto-conocimiento: registros ─────────────────────────────────────
    @_con_lock
    def registrar_error(self, contexto: str, tipo: str, mensaje: str,
                          accion: str = "") -> None:
        """Persiste un error que Celestia detectó en sí misma."""
        try:
            self.conn.execute(
                "INSERT INTO errores (ts, contexto, tipo, mensaje, accion) VALUES (?,?,?,?,?)",
                (time.time(), contexto[:200], tipo[:80], mensaje[:1000], accion[:300]),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning("registrar_error falló: %s", e)

    @_con_lock
    def registrar_aprendizaje(self, tarea: str, estado: str, intentos: int,
                                inicio_ts: float, fin_ts: Optional[float] = None,
                                resultado: str = "", ruta_skill: str = "") -> None:
        """Persiste el estado de un aprendizaje. Upsert por tarea (último gana)."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT id FROM aprendizajes WHERE tarea=? AND inicio_ts=?",
                          (tarea[:300], inicio_ts))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE aprendizajes SET estado=?, intentos=?, fin_ts=?, resultado=?, ruta_skill=? WHERE id=?",
                    (estado, intentos, fin_ts, resultado[:1000], ruta_skill[:300], row[0]),
                )
            else:
                cur.execute(
                    "INSERT INTO aprendizajes (tarea, estado, intentos, inicio_ts, fin_ts, resultado, ruta_skill) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (tarea[:300], estado, intentos, inicio_ts, fin_ts,
                     resultado[:1000], ruta_skill[:300]),
                )
            self.conn.commit()
        except Exception as e:
            logger.warning("registrar_aprendizaje falló: %s", e)

    @_con_lock
    def registrar_hecho_usuario(self, tipo: str, clave: str, valor: str) -> str:
        """Hecho persistente sobre el usuario (preferencia, dato, instrucción).
        Upsert por (tipo, clave) — el valor más reciente gana.

        Devuelve: 'nuevo' si era un hecho que no existía, 'actualizado' si ya estaba
        con otro valor, 'sin_cambio' si el valor era idéntico al existente, 'error' si falló.
        El caller usa esto para decidir si notificar al usuario ('anotado: X').

        Privacidad (red final): si la clave delata una credencial
        (contraseña/clave/PIN/CVV/IBAN/tarjeta…), NO se persiste — devuelve
        'rechazado_sensible'. Cubre todos los caminos de extracción.

        Reintenta con backoff exponencial si la DB está locked — bajo carga
        (preload del modelo, escritura de auto-reflexión, varios /mensaje en
        paralelo) SQLite WAL puede saturarse y devolver 'database is locked'.
        """
        if _CLAVE_CREDENCIAL_RE.search(f"{tipo} {clave}"):
            logger.info("Hecho sensible NO guardado (clave credencial): %.40s", clave)
            return "rechazado_sensible"
        # Red final de calidad: aquí pasan los dos extractores y lo que venga
        # después, así que es el sitio donde se decide qué es un hecho.
        _motivo = hecho_es_ruido(tipo, clave, valor)
        if _motivo:
            logger.info("Hecho NO guardado (%s): %.40s = %.60s",
                        _motivo, clave, valor)
            return "rechazado_ruido"
        last_err = None
        # Sesión 31: 8 reintentos con espera total ~10s (más busy_timeout=15s
        # del PRAGMA dan margen para escrituras lentas concurrentes). El bug
        # raíz "perro Toby" no se guardaba por "database is locked" con la
        # cadena anterior (5 intentos, ~2.7s total).
        for intento, espera in enumerate((0, 0.1, 0.3, 0.8, 1.5, 2.0, 2.5, 3.0), start=1):
            if espera:
                time.sleep(espera)
            try:
                # Transacción atómica con `with self.conn:` — garantiza commit
                # automático y libera el lock rápidamente en caso de éxito.
                with self.conn:
                    cur = self.conn.cursor()
                    cur.execute("SELECT id, valor FROM hechos_usuario WHERE tipo=? AND clave=?",
                                  (tipo[:40], clave[:80]))
                    row = cur.fetchone()
                    if row:
                        if (row[1] or "") == valor[:500]:
                            return "sin_cambio"
                        cur.execute("UPDATE hechos_usuario SET valor=?, ts=? WHERE id=?",
                                      (valor[:500], time.time(), row[0]))
                        return "actualizado"
                    # Antes de abrir fila nueva: ¿no será esto mismo con
                    # otras palabras? El extractor sacaba una clave distinta
                    # por cada frase en que salía el tema, y la tabla acababa
                    # con ocho versiones del mismo plan. Se actualiza la que
                    # ya estaba (la clave nueva incluida: manda la última).
                    cur.execute("SELECT id, valor FROM hechos_usuario WHERE tipo=?",
                                (tipo[:40],))
                    for _id, _valor in cur.fetchall():
                        if es_el_mismo_hecho(_valor or "", valor):
                            cur.execute(
                                "UPDATE hechos_usuario SET clave=?, valor=?, ts=? "
                                "WHERE id=?",
                                (clave[:80], valor[:500], time.time(), _id))
                            logger.info("Hecho fundido con uno anterior: %.40s",
                                        clave)
                            return "actualizado"
                    cur.execute(
                        "INSERT INTO hechos_usuario (ts, tipo, clave, valor) VALUES (?,?,?,?)",
                        (time.time(), tipo[:40], clave[:80], valor[:500]),
                    )
                    return "nuevo"
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower():
                    break  # otro tipo de error: no reintentar
                continue
            except Exception as e:
                last_err = e
                break
        logger.warning("registrar_hecho_usuario falló tras reintentos: %s", last_err)
        return "error"

    @_con_lock
    def add_auto_reflexion(self, periodo: str, resumen: str,
                              fallos: Dict, acciones: List) -> None:
        try:
            self.conn.execute(
                "INSERT INTO auto_reflexiones (ts, periodo, resumen, fallos_detectados, acciones_propuestas) "
                "VALUES (?,?,?,?,?)",
                (time.time(), periodo[:20], resumen[:4000],
                 json.dumps(fallos, ensure_ascii=False)[:2000],
                 json.dumps(acciones, ensure_ascii=False)[:2000]),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning("add_auto_reflexion falló: %s", e)

    # ── Auto-conocimiento: consultas ─────────────────────────────────────
    def estadisticas_periodo(self, segundos_atras: float = 86400) -> Dict[str, Any]:
        """Resumen de actividad en las últimas N segundos (default: 24h)."""
        cur = self.conn.cursor()
        desde = time.time() - segundos_atras
        out: Dict[str, Any] = {"periodo_horas": round(segundos_atras / 3600, 1)}
        try:
            out["conversaciones"] = cur.execute(
                "SELECT COUNT(*) FROM conversations WHERE ts > ?", (desde,)
            ).fetchone()[0]
            out["errores"] = cur.execute(
                "SELECT COUNT(*) FROM errores WHERE ts > ?", (desde,)
            ).fetchone()[0]
            out["aprendizajes_ok"] = cur.execute(
                "SELECT COUNT(*) FROM aprendizajes WHERE estado='terminado_ok' AND fin_ts > ?",
                (desde,)
            ).fetchone()[0]
            out["aprendizajes_falla"] = cur.execute(
                "SELECT COUNT(*) FROM aprendizajes WHERE estado='terminado_error' AND fin_ts > ?",
                (desde,)
            ).fetchone()[0]
            out["aprendizajes_en_curso"] = cur.execute(
                "SELECT COUNT(*) FROM aprendizajes WHERE estado='en_curso'"
            ).fetchone()[0]
            coh_row = cur.execute(
                "SELECT AVG(coherence) FROM conversations WHERE ts > ?", (desde,)
            ).fetchone()
            out["coherencia_media"] = round(coh_row[0], 3) if coh_row[0] else None
        except Exception as e:
            out["error"] = str(e)
        return out

    def fallos_recurrentes(self, segundos_atras: float = 604800,
                              min_repeticiones: int = 3) -> List[Tuple[str, int]]:
        """Tipos de error que se repitieron >= min_repeticiones en la última semana."""
        try:
            desde = time.time() - segundos_atras
            cur = self.conn.cursor()
            cur.execute(
                "SELECT tipo, COUNT(*) c FROM errores WHERE ts > ? "
                "GROUP BY tipo HAVING c >= ? ORDER BY c DESC",
                (desde, min_repeticiones),
            )
            return cur.fetchall()
        except Exception:
            return []

    def aprendizajes_recientes(self, n: int = 10) -> List[Dict[str, Any]]:
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT tarea, estado, intentos, inicio_ts, fin_ts, resultado "
                "FROM aprendizajes ORDER BY COALESCE(fin_ts, inicio_ts) DESC LIMIT ?",
                (n,),
            )
            return [
                {"tarea": r[0], "estado": r[1], "intentos": r[2],
                 "inicio_ts": r[3], "fin_ts": r[4], "resultado": r[5]}
                for r in cur.fetchall()
            ]
        except Exception:
            return []

    def errores_recientes(self, n: int = 10) -> List[Dict[str, Any]]:
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT ts, contexto, tipo, mensaje, accion FROM errores "
                "ORDER BY ts DESC LIMIT ?", (n,),
            )
            return [
                {"ts": r[0], "contexto": r[1], "tipo": r[2],
                 "mensaje": r[3], "accion": r[4]}
                for r in cur.fetchall()
            ]
        except Exception:
            return []

    def hechos_usuario(self) -> List[Dict[str, str]]:
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT tipo, clave, valor, ts FROM hechos_usuario ORDER BY ts DESC")
            return [{"tipo": r[0], "clave": r[1], "valor": r[2], "ts": r[3]}
                    for r in cur.fetchall()]
        except Exception:
            return []

    def ultima_reflexion(self) -> Optional[Dict[str, Any]]:
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT ts, periodo, resumen, fallos_detectados, acciones_propuestas "
                "FROM auto_reflexiones ORDER BY ts DESC LIMIT 1"
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "ts": r[0], "periodo": r[1], "resumen": r[2],
                "fallos": json.loads(r[3]) if r[3] else {},
                "acciones": json.loads(r[4]) if r[4] else [],
            }
        except Exception:
            return None

    # ── Asistente de salud (skeleton para iterar) ─────────────────────────
    @_con_lock
    def registrar_medicacion(self, nombre: str, horario: str = "",
                              dias: str = "diario", notas: str = "") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO salud_medicaciones (nombre, horario, dias, notas, activa, creada_ts) "
            "VALUES (?,?,?,?,1,?)",
            (nombre[:100], horario[:100], dias[:60], notas[:300], time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    @_con_lock
    def pausar_medicacion(self, nombre: str) -> int:
        cur = self.conn.cursor()
        cur.execute("UPDATE salud_medicaciones SET activa=0 WHERE LOWER(nombre)=LOWER(?)",
                      (nombre,))
        self.conn.commit()
        return cur.rowcount

    def medicaciones_activas(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        return [
            {"nombre": r[0], "horario": r[1], "dias": r[2], "notas": r[3]}
            for r in cur.execute(
                "SELECT nombre, horario, dias, notas FROM salud_medicaciones "
                "WHERE activa=1 ORDER BY nombre"
            )
        ]

    @_con_lock
    def registrar_sintoma(self, sintoma: str, intensidad: int = 5,
                          notas: str = "", relacion: str = "") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO salud_sintomas (ts, sintoma, intensidad, notas, relacion) "
            "VALUES (?,?,?,?,?)",
            (time.time(), sintoma[:100], max(1, min(10, int(intensidad or 5))),
             notas[:300], relacion[:200]),
        )
        self.conn.commit()
        return cur.lastrowid

    def sintomas_recientes(self, dias: int = 7) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        return [
            {"ts": r[0], "sintoma": r[1], "intensidad": r[2], "notas": r[3]}
            for r in cur.execute(
                "SELECT ts, sintoma, intensidad, notas FROM salud_sintomas "
                "WHERE ts > ? ORDER BY ts DESC LIMIT 100",
                (time.time() - dias * 86400,),
            )
        ]

    @_con_lock
    def registrar_habito(self, habito: str, valor: float) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO salud_habitos (ts, habito, valor) VALUES (?,?,?)",
            (time.time(), habito[:60], float(valor)),
        )
        self.conn.commit()
        return cur.lastrowid

    def habitos_periodo(self, dias: int = 7) -> Dict[str, List[Dict[str, Any]]]:
        cur = self.conn.cursor()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in cur.execute(
            "SELECT ts, habito, valor FROM salud_habitos WHERE ts > ? ORDER BY ts",
            (time.time() - dias * 86400,),
        ):
            out.setdefault(r[1], []).append({"ts": r[0], "valor": r[2]})
        return out

    def texto_introspectivo(self) -> str:
        """Devuelve una descripción breve del estado actual de Celestia,
        pensada para inyectar en un prompt o responder al usuario.
        """
        try:
            stats_24h = self.estadisticas_periodo(86400)
            stats_7d  = self.estadisticas_periodo(604800)
            ult_apr   = self.aprendizajes_recientes(5)
            fallos    = self.fallos_recurrentes(604800, min_repeticiones=2)
            partes = [
                f"Últimas 24h: {stats_24h.get('conversaciones', 0)} conversaciones, "
                f"{stats_24h.get('errores', 0)} errores propios detectados, "
                f"{stats_24h.get('aprendizajes_ok', 0)} skills aprendidas con éxito, "
                f"{stats_24h.get('aprendizajes_falla', 0)} fracasos.",
                f"Última semana: {stats_7d.get('conversaciones', 0)} conversaciones, "
                f"coherencia media {stats_7d.get('coherencia_media')}.",
            ]
            if ult_apr:
                partes.append("Últimos aprendizajes:")
                for a in ult_apr[:5]:
                    emoji = {"terminado_ok": "✓", "terminado_error": "✗", "en_curso": "▸"}.get(a["estado"], "?")
                    partes.append(f"  {emoji} '{a['tarea'][:80]}' ({a['intentos'] or 0} intentos)")
            if fallos:
                partes.append("Tipos de error recurrentes (última semana):")
                for tipo, n in fallos[:5]:
                    partes.append(f"  · {tipo}: {n} veces")
            return "\n".join(partes)
        except Exception as e:
            return f"(no pude leer mi propio estado: {e})"

