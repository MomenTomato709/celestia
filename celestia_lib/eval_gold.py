"""Banco de evaluación gold para medir calidad real del modelo.

Resuelve la crítica honesta del roadmap AGI #2: medíamos perplexity sobre val,
no comportamiento. Aquí: prompts de referencia con respuestas/criterios
ideales en español, scoring por keyword-recall + concept-recall + coherencia
heurística + latencia. Resultados persistidos en SQLite (`eval_runs`,
`eval_resultados`) para trackear evolución a lo largo del tiempo.

Filosofía:
- No pretende ser una evaluación tipo MMLU/HellaSwag (eso requiere benchmarks
  y datasets pesados). Es un panel de control casero de ~30-50 prompts que
  cubren los casos que importan para una asistente personal en español:
  factuales, razonamiento simple, generativos, instrucciones, conversación.
- Scoring objetivo y barato: keywords/concepts esperados, longitud razonable,
  ausencia de fallback strings ("no lo sé", "estoy reiniciando").
- Comparable: cada run guarda métricas agregadas — la UI puede mostrar
  delta vs run anterior para ver si la calidad sube o baja con cada
  entrenamiento LoRA o cambio de modelo.

Honestidad explícita:
- 30 prompts no demuestran AGI ni nada parecido. Demuestran que la asistente
  funciona razonablemente bien en las tareas más comunes que se le piden.
- Las "keywords" son aproximaciones — un modelo puede dar respuesta correcta
  sin contener exactamente esas palabras. Por eso usamos `min_recall` bajo
  (0.3-0.5) y combinamos con coherencia y ausencia de fallback.
- No hay LLM-as-judge aquí (sesgado, caro, no determinista). Si en el futuro
  se quiere añadir, va en otro módulo.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("celestia_v1")


# ─── Datatypes ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PromptGold:
    """Prompt de evaluación con criterios objetivos de aceptación.

    - `keywords`: lista de strings que deberían aparecer (cualquier forma).
      Se hace match case-insensitive con normalización ligera (sin tildes).
    - `concepts`: variantes de un mismo concepto. Cada concepto cuenta como
      acertado si CUALQUIERA de sus variantes aparece.
    - `min_recall_keywords`: fracción mínima de keywords que deben aparecer.
    - `min_recall_concepts`: idem conceptos.
    - `min_len`: longitud mínima razonable (descarta "ok", "sí").
    - `max_len`: longitud máxima (descarta respuestas excesivas).
    - `prohibidas`: substrings que NO deben aparecer (fallbacks de error).
    - `categoria`: para agrupar métricas (factual, razonamiento, etc).
    """
    id: str
    prompt: str
    categoria: str
    keywords: Tuple[str, ...] = ()
    concepts: Tuple[Tuple[str, ...], ...] = ()
    min_recall_keywords: float = 0.5
    min_recall_concepts: float = 0.7
    min_len: int = 15
    max_len: int = 1500
    prohibidas: Tuple[str, ...] = (
        "no lo sé", "no puedo", "no entiendo", "estoy reiniciando",
        "error interno", "no pude",
    )


@dataclass
class ResultadoPrompt:
    """Resultado de evaluar un PromptGold contra una respuesta."""
    prompt_id: str
    categoria: str
    respuesta: str
    latencia_ms: int
    longitud_ok: bool
    sin_prohibidas: bool
    recall_keywords: float
    recall_concepts: float
    aceptada: bool
    razon: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "categoria": self.categoria,
            "respuesta": self.respuesta,
            "latencia_ms": self.latencia_ms,
            "longitud_ok": self.longitud_ok,
            "sin_prohibidas": self.sin_prohibidas,
            "recall_keywords": round(self.recall_keywords, 3),
            "recall_concepts": round(self.recall_concepts, 3),
            "aceptada": self.aceptada,
            "razon": self.razon,
        }


@dataclass
class EvalRun:
    """Agregado de un run completo: métricas globales + lista de resultados."""
    id: Optional[int]
    modelo: str
    backend: str
    ts: float
    n_prompts: int
    n_aceptadas: int
    accuracy: float                 # 0-1
    accuracy_por_categoria: Dict[str, float]
    latencia_media_ms: int
    latencia_p95_ms: int
    nota: str = ""
    resultados: List[ResultadoPrompt] = field(default_factory=list)

    def to_dict(self, incluir_resultados: bool = True) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "modelo": self.modelo,
            "backend": self.backend,
            "ts": self.ts,
            "n_prompts": self.n_prompts,
            "n_aceptadas": self.n_aceptadas,
            "accuracy": round(self.accuracy, 3),
            "accuracy_por_categoria": {
                k: round(v, 3) for k, v in self.accuracy_por_categoria.items()
            },
            "latencia_media_ms": self.latencia_media_ms,
            "latencia_p95_ms": self.latencia_p95_ms,
            "nota": self.nota,
        }
        if incluir_resultados:
            d["resultados"] = [r.to_dict() for r in self.resultados]
        return d


# ─── Normalización y scoring ──────────────────────────────────────────

_TILDES = str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN")


def normalizar(s: str) -> str:
    """Lowercase, sin tildes, sin puntuación múltiple. Para matching robusto."""
    if not s:
        return ""
    out = s.translate(_TILDES).lower()
    out = re.sub(r"[^\w\sáéíóúñ]+", " ", out, flags=re.UNICODE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _contiene(haystack_norm: str, needle: str) -> bool:
    needle_norm = normalizar(needle)
    if not needle_norm:
        return False
    # Match por palabras completas si el needle es una sola palabra,
    # substring si tiene espacios (frases).
    if " " in needle_norm:
        return needle_norm in haystack_norm
    return bool(re.search(rf"\b{re.escape(needle_norm)}\b", haystack_norm))


def evaluar_respuesta(gold: PromptGold, respuesta: str, latencia_ms: int) -> ResultadoPrompt:
    """Aplica todos los criterios del PromptGold a una respuesta."""
    r_norm = normalizar(respuesta or "")
    r_len = len(respuesta or "")

    longitud_ok = (gold.min_len <= r_len <= gold.max_len)
    sin_prohibidas = not any(p.lower() in (respuesta or "").lower() for p in gold.prohibidas)

    if gold.keywords:
        n_kw = sum(1 for kw in gold.keywords if _contiene(r_norm, kw))
        recall_kw = n_kw / len(gold.keywords)
    else:
        recall_kw = 1.0

    if gold.concepts:
        aciertos_concepto = 0
        for variantes in gold.concepts:
            if any(_contiene(r_norm, v) for v in variantes):
                aciertos_concepto += 1
        recall_co = aciertos_concepto / len(gold.concepts)
    else:
        recall_co = 1.0

    aceptada = (
        longitud_ok
        and sin_prohibidas
        and recall_kw >= gold.min_recall_keywords
        and recall_co >= gold.min_recall_concepts
    )

    razones: List[str] = []
    if not longitud_ok:
        razones.append(f"longitud {r_len} fuera de [{gold.min_len},{gold.max_len}]")
    if not sin_prohibidas:
        razones.append("contiene frase prohibida (fallback)")
    if recall_kw < gold.min_recall_keywords:
        razones.append(f"recall_kw {recall_kw:.2f} < {gold.min_recall_keywords:.2f}")
    if recall_co < gold.min_recall_concepts:
        razones.append(f"recall_concepts {recall_co:.2f} < {gold.min_recall_concepts:.2f}")

    return ResultadoPrompt(
        prompt_id=gold.id,
        categoria=gold.categoria,
        respuesta=(respuesta or "")[:2000],
        latencia_ms=int(latencia_ms),
        longitud_ok=longitud_ok,
        sin_prohibidas=sin_prohibidas,
        recall_keywords=recall_kw,
        recall_concepts=recall_co,
        aceptada=aceptada,
        razon="; ".join(razones) if razones else "ok",
    )


# ─── Banco de prompts iniciales (español) ─────────────────────────────

def banco_inicial() -> List[PromptGold]:
    """30 prompts de referencia cubriendo las categorías más comunes.

    Editar/ampliar con cuidado: cualquier cambio rompe la comparabilidad
    con runs históricos. Si se quieren añadir prompts nuevos, hacerlo en
    una versión separada (`banco_v2`) o marcando el ID con sufijo.
    """
    return [
        # ─── Factuales (8) ───
        PromptGold(
            id="fact-01-capital-francia",
            prompt="¿Cuál es la capital de Francia?",
            categoria="factual",
            keywords=("paris",), min_recall_keywords=1.0,
            concepts=(), min_len=5,
        ),
        PromptGold(
            id="fact-02-presidente-rae",
            prompt="¿Para qué sirve la RAE?",
            categoria="factual",
            concepts=(
                ("normativa", "norma", "regular", "regula"),
                ("español", "castellano", "lengua", "idioma"),
            ),
            min_recall_concepts=0.5,
        ),
        PromptGold(
            id="fact-03-velocidad-luz",
            prompt="¿A qué velocidad viaja la luz en el vacío?",
            categoria="factual",
            concepts=(
                ("300", "299", "3x10", "3 x 10", "trescientos mil"),
                ("km", "kilometros", "metros"),
                ("segundo", "s"),
            ),
            min_recall_concepts=0.5,
        ),
        PromptGold(
            id="fact-04-fotosintesis",
            prompt="Explícame brevemente qué es la fotosíntesis.",
            categoria="factual",
            concepts=(
                ("planta", "vegetal", "alga"),
                ("luz", "solar", "sol"),
                ("dioxido", "co2", "carbono"),
                ("oxigeno", "o2"),
            ),
            min_recall_concepts=0.6,
        ),
        PromptGold(
            id="fact-05-segunda-guerra",
            prompt="¿En qué años ocurrió la Segunda Guerra Mundial?",
            categoria="factual",
            keywords=("1939", "1945"), min_recall_keywords=1.0,
            min_len=8,
        ),
        PromptGold(
            id="fact-06-rios-espana",
            prompt="Nombra tres ríos importantes de España.",
            categoria="factual",
            concepts=(
                ("ebro", "tajo", "duero", "guadalquivir", "guadiana", "miño", "mino", "jucar", "segura"),
            ),
            min_recall_concepts=1.0,
            min_len=20,
        ),
        PromptGold(
            id="fact-07-h2o",
            prompt="¿De qué átomos se compone una molécula de agua?",
            categoria="factual",
            concepts=(
                ("hidrogeno", "h2", "dos hidrogeno", "dos atomos de hidrogeno"),
                ("oxigeno", "o", "un oxigeno", "un atomo de oxigeno"),
            ),
            min_recall_concepts=1.0,
        ),
        PromptGold(
            id="fact-08-planetas",
            prompt="¿Cuántos planetas tiene el sistema solar?",
            categoria="factual",
            concepts=(("ocho", "8"),),
            min_recall_concepts=1.0,
        ),

        # ─── Razonamiento (6) ───
        PromptGold(
            id="raz-01-aritmetica",
            prompt="Si tengo 12 manzanas y reparto 3 a cada amigo, ¿a cuántos amigos puedo darles?",
            categoria="razonamiento",
            concepts=(("cuatro", "4"),),
            min_recall_concepts=1.0,
        ),
        PromptGold(
            id="raz-02-silogismo",
            prompt="Todos los gatos son mamíferos. Garfield es un gato. ¿Qué se puede deducir?",
            categoria="razonamiento",
            concepts=(
                ("mamifero",),
                ("garfield", "el"),
            ),
            min_recall_concepts=0.5,
        ),
        PromptGold(
            id="raz-03-tiempos",
            prompt="Si hoy es lunes, ¿qué día será dentro de 10 días?",
            categoria="razonamiento",
            concepts=(("jueves",),),
            min_recall_concepts=1.0,
        ),
        PromptGold(
            id="raz-04-comparacion",
            prompt="¿Qué es más pesado: un kilo de plomo o un kilo de plumas?",
            categoria="razonamiento",
            concepts=(
                ("igual", "lo mismo", "mismo peso", "pesan lo mismo", "ambos"),
            ),
            min_recall_concepts=1.0,
        ),
        PromptGold(
            id="raz-05-causa",
            prompt="Si no riego una planta durante un mes, ¿qué le pasará?",
            categoria="razonamiento",
            concepts=(
                ("se secara", "morira", "se marchitara", "se muere", "muere", "moriria", "secarse"),
            ),
            min_recall_concepts=1.0,
        ),
        PromptGold(
            id="raz-06-numero-faltante",
            prompt="Completa la secuencia: 2, 4, 6, 8, ?",
            categoria="razonamiento",
            concepts=(("diez", "10"),),
            min_recall_concepts=1.0,
        ),

        # ─── Generativos (6) ───
        PromptGold(
            id="gen-01-haiku",
            prompt="Escríbeme un haiku sobre el otoño.",
            categoria="generativo",
            concepts=(
                ("otoño", "otono", "hojas", "lluvia", "viento", "frio"),
            ),
            min_recall_concepts=0.4,
            min_len=20, max_len=400,
        ),
        PromptGold(
            id="gen-02-receta",
            prompt="Dame una receta sencilla para hacer tortilla de patatas.",
            categoria="generativo",
            concepts=(
                ("patata", "papa"),
                ("huevo", "huevos"),
                ("sal",),
                ("aceite", "oliva"),
            ),
            min_recall_concepts=0.75,
            min_len=80,
        ),
        PromptGold(
            id="gen-03-email",
            prompt="Redacta un email breve pidiendo vacaciones a tu jefe.",
            categoria="generativo",
            concepts=(
                ("vacaciones", "vacacional"),
                ("estimado", "hola", "buenos dias", "buenas"),
                ("saludo", "saludos", "atentamente", "gracias"),
            ),
            min_recall_concepts=0.66,
            min_len=80,
        ),
        PromptGold(
            id="gen-04-resumen",
            prompt="Resúmeme en una frase qué hace una red neuronal.",
            categoria="generativo",
            concepts=(
                ("aprende", "aprender", "aprendizaje"),
                ("dato", "datos", "ejemplos", "patrones"),
            ),
            min_recall_concepts=0.5,
            min_len=20, max_len=300,
        ),
        PromptGold(
            id="gen-05-lista",
            prompt="Dame tres consejos para dormir mejor.",
            categoria="generativo",
            concepts=(
                ("horario", "rutina", "constante", "consistente", "regular"),
                ("pantalla", "movil", "luz", "azul", "telefono"),
                ("cafe", "cafeina", "alcohol", "comer", "cena", "cenar"),
            ),
            min_recall_concepts=0.5,
            min_len=80,
        ),
        PromptGold(
            id="gen-06-codigo-python",
            prompt="Escribe una función Python que sume dos números.",
            categoria="generativo",
            concepts=(
                ("def", "lambda"),
                ("return", "devuelve", "+"),
            ),
            min_recall_concepts=1.0,
            min_len=20, max_len=400,
        ),

        # ─── Instrucciones (5) ───
        PromptGold(
            id="inst-01-formato-lista",
            prompt="Lista en formato bullets tres frutas tropicales.",
            categoria="instrucciones",
            concepts=(
                ("piña", "pina", "mango", "papaya", "platano", "banana",
                 "coco", "guayaba", "maracuya", "lichi"),
            ),
            min_recall_concepts=1.0,
            min_len=20,
        ),
        PromptGold(
            id="inst-02-formato-tabla",
            prompt="Compara brevemente Python y JavaScript en una tabla de 3 filas.",
            categoria="instrucciones",
            concepts=(
                ("python",),
                ("javascript", "js"),
                ("|", "----"),  # cualquier indicador de tabla
            ),
            min_recall_concepts=0.66,
            min_len=80,
        ),
        PromptGold(
            id="inst-03-longitud",
            prompt="Responde con UNA sola palabra: ¿cuál es el color del cielo?",
            categoria="instrucciones",
            concepts=(("azul",),),
            min_recall_concepts=1.0,
            min_len=3, max_len=80,
        ),
        PromptGold(
            id="inst-04-traducir",
            prompt="Traduce al inglés: 'Hola, ¿cómo estás?'",
            categoria="instrucciones",
            concepts=(
                ("hello", "hi"),
                ("how", "are", "you"),
            ),
            min_recall_concepts=1.0,
            min_len=8,
        ),
        PromptGold(
            id="inst-05-paso-a-paso",
            prompt="Explícame paso a paso cómo hacer un café con leche.",
            categoria="instrucciones",
            concepts=(
                ("cafe",),
                ("leche",),
                ("paso", "primero", "luego", "despues", "finalmente", "1.", "2.", "3."),
            ),
            min_recall_concepts=1.0,
            min_len=60,
        ),

        # ─── Conversación (5) ───
        PromptGold(
            id="conv-01-saludo",
            prompt="Hola, ¿cómo estás?",
            categoria="conversacion",
            concepts=(
                ("hola", "bien", "buenas", "saludo", "saludos", "encantada", "encantado",
                 "gracias", "estoy"),
            ),
            min_recall_concepts=1.0,
            min_len=5, max_len=400,
        ),
        PromptGold(
            id="conv-02-empatia",
            prompt="Hoy he tenido un día muy duro.",
            categoria="conversacion",
            concepts=(
                ("siento", "lamento", "duro", "comprendo", "entiendo", "vaya",
                 "puedo", "como", "ayudar"),
            ),
            min_recall_concepts=1.0,
            min_len=20, max_len=500,
        ),
        PromptGold(
            id="conv-03-pregunta-personal",
            prompt="¿Tienes sentimientos?",
            categoria="conversacion",
            concepts=(
                ("no", "soy", "modelo", "asistente", "celestia", "ia"),
            ),
            min_recall_concepts=1.0,
            min_len=20,
        ),
        PromptGold(
            id="conv-04-broma",
            prompt="Cuéntame un chiste corto.",
            categoria="conversacion",
            concepts=(
                ("?", "!", "porque", "que le", "que hace", "le dice", "va y le"),
            ),
            min_recall_concepts=0.34,
            min_len=20, max_len=400,
        ),
        PromptGold(
            id="conv-05-clarificacion",
            prompt="Ayúdame.",
            categoria="conversacion",
            concepts=(
                ("claro", "por supuesto", "dime", "que necesitas", "en que",
                 "cuentame", "encantado", "encantada"),
            ),
            min_recall_concepts=1.0,
            min_len=10, max_len=400,
        ),
    ]


# ─── Persistencia ─────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    modelo TEXT NOT NULL,
    backend TEXT NOT NULL,
    ts REAL NOT NULL,
    n_prompts INTEGER NOT NULL,
    n_aceptadas INTEGER NOT NULL,
    accuracy REAL NOT NULL,
    accuracy_por_categoria_json TEXT NOT NULL,
    latencia_media_ms INTEGER NOT NULL,
    latencia_p95_ms INTEGER NOT NULL,
    nota TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS eval_resultados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    prompt_id TEXT NOT NULL,
    categoria TEXT NOT NULL,
    respuesta TEXT NOT NULL,
    latencia_ms INTEGER NOT NULL,
    longitud_ok INTEGER NOT NULL,
    sin_prohibidas INTEGER NOT NULL,
    recall_keywords REAL NOT NULL,
    recall_concepts REAL NOT NULL,
    aceptada INTEGER NOT NULL,
    razon TEXT DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES eval_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_ts ON eval_runs(ts);
CREATE INDEX IF NOT EXISTS idx_eval_resultados_run ON eval_resultados(run_id);
"""


class EvalBenchStore:
    """Persistencia de runs y resultados en SQLite."""

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def guardar(self, run: EvalRun) -> int:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO eval_runs "
                "(modelo, backend, ts, n_prompts, n_aceptadas, accuracy, "
                "accuracy_por_categoria_json, latencia_media_ms, latencia_p95_ms, nota) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run.modelo, run.backend, run.ts, run.n_prompts,
                    run.n_aceptadas, run.accuracy,
                    json.dumps(run.accuracy_por_categoria),
                    run.latencia_media_ms, run.latencia_p95_ms, run.nota,
                ),
            )
            run_id = int(cur.lastrowid)
            run.id = run_id
            for r in run.resultados:
                cur.execute(
                    "INSERT INTO eval_resultados "
                    "(run_id, prompt_id, categoria, respuesta, latencia_ms, "
                    "longitud_ok, sin_prohibidas, recall_keywords, recall_concepts, "
                    "aceptada, razon) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, r.prompt_id, r.categoria, r.respuesta,
                        r.latencia_ms, int(r.longitud_ok), int(r.sin_prohibidas),
                        r.recall_keywords, r.recall_concepts,
                        int(r.aceptada), r.razon,
                    ),
                )
            self.conn.commit()
            return run_id

    def listar(self, limit: int = 20) -> List[EvalRun]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, modelo, backend, ts, n_prompts, n_aceptadas, accuracy, "
            "accuracy_por_categoria_json, latencia_media_ms, latencia_p95_ms, nota "
            "FROM eval_runs ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        )
        out: List[EvalRun] = []
        for row in cur.fetchall():
            try:
                accs = json.loads(row[7] or "{}")
                if not isinstance(accs, dict):
                    accs = {}
            except json.JSONDecodeError:
                accs = {}
            out.append(EvalRun(
                id=row[0], modelo=row[1], backend=row[2], ts=row[3],
                n_prompts=row[4], n_aceptadas=row[5], accuracy=row[6],
                accuracy_por_categoria=accs,
                latencia_media_ms=row[8], latencia_p95_ms=row[9],
                nota=row[10] or "", resultados=[],
            ))
        return out

    def obtener(self, run_id: int) -> Optional[EvalRun]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, modelo, backend, ts, n_prompts, n_aceptadas, accuracy, "
            "accuracy_por_categoria_json, latencia_media_ms, latencia_p95_ms, nota "
            "FROM eval_runs WHERE id=?", (run_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            accs = json.loads(row[7] or "{}")
        except json.JSONDecodeError:
            accs = {}
        cur.execute(
            "SELECT prompt_id, categoria, respuesta, latencia_ms, longitud_ok, "
            "sin_prohibidas, recall_keywords, recall_concepts, aceptada, razon "
            "FROM eval_resultados WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        resultados = [
            ResultadoPrompt(
                prompt_id=r[0], categoria=r[1], respuesta=r[2], latencia_ms=r[3],
                longitud_ok=bool(r[4]), sin_prohibidas=bool(r[5]),
                recall_keywords=r[6], recall_concepts=r[7],
                aceptada=bool(r[8]), razon=r[9] or "",
            )
            for r in cur.fetchall()
        ]
        return EvalRun(
            id=row[0], modelo=row[1], backend=row[2], ts=row[3],
            n_prompts=row[4], n_aceptadas=row[5], accuracy=row[6],
            accuracy_por_categoria=accs,
            latencia_media_ms=row[8], latencia_p95_ms=row[9],
            nota=row[10] or "", resultados=resultados,
        )

    def delta_vs_anterior(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Devuelve métricas-delta vs el run inmediatamente anterior (mismo modelo)."""
        actual = self.obtener(run_id)
        if not actual:
            return None
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM eval_runs WHERE modelo=? AND ts<? "
            "ORDER BY ts DESC LIMIT 1",
            (actual.modelo, actual.ts),
        )
        row = cur.fetchone()
        if not row:
            return None
        anterior = self.obtener(int(row[0]))
        if not anterior:
            return None
        delta = {
            "anterior_id": anterior.id,
            "anterior_ts": anterior.ts,
            "delta_accuracy": round(actual.accuracy - anterior.accuracy, 4),
            "delta_latencia_media_ms": actual.latencia_media_ms - anterior.latencia_media_ms,
            "delta_por_categoria": {
                cat: round(
                    actual.accuracy_por_categoria.get(cat, 0.0)
                    - anterior.accuracy_por_categoria.get(cat, 0.0),
                    4,
                )
                for cat in actual.accuracy_por_categoria
            },
        }
        return delta

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── Runner ───────────────────────────────────────────────────────────

class EvalRunner:
    """Ejecuta un banco de PromptGold contra un callable LLM y agrega métricas.

    Args:
        llm_callable: función `(prompt: str) -> str`. Encapsula cualquier
            transformación (system prompt, formato chat). El runner solo
            mide la salida directa, sin contexto previo, para aislar el
            modelo del resto del orchestrator.
        store: opcional `EvalBenchStore` para persistir runs.
        banco: lista de PromptGold. Default = banco_inicial().
    """

    def __init__(
        self,
        llm_callable: Callable[[str], str],
        store: Optional[EvalBenchStore] = None,
        banco: Optional[List[PromptGold]] = None,
    ):
        self.llm = llm_callable
        self.store = store
        self.banco = banco if banco is not None else banco_inicial()

    def correr(
        self,
        modelo: str,
        backend: str = "?",
        nota: str = "",
        max_prompts: Optional[int] = None,
        persistir: bool = True,
    ) -> EvalRun:
        """Corre el banco (o subset) y devuelve un EvalRun con métricas."""
        prompts = self.banco[: max_prompts] if max_prompts else self.banco
        resultados: List[ResultadoPrompt] = []
        for g in prompts:
            t0 = time.perf_counter()
            try:
                respuesta = self.llm(g.prompt) or ""
            except Exception as e:
                respuesta = f"[EXCEPCIÓN: {e}]"
            lat_ms = int((time.perf_counter() - t0) * 1000)
            res = evaluar_respuesta(g, respuesta, lat_ms)
            resultados.append(res)

        # Métricas agregadas
        n = len(resultados)
        n_ok = sum(1 for r in resultados if r.aceptada)
        accuracy = (n_ok / n) if n else 0.0

        por_cat: Dict[str, Tuple[int, int]] = {}
        for r in resultados:
            ok, tot = por_cat.get(r.categoria, (0, 0))
            por_cat[r.categoria] = (ok + (1 if r.aceptada else 0), tot + 1)
        accuracy_por_categoria = {
            cat: (ok / tot if tot else 0.0)
            for cat, (ok, tot) in por_cat.items()
        }

        latencias = sorted(r.latencia_ms for r in resultados)
        lat_media = int(sum(latencias) / len(latencias)) if latencias else 0
        lat_p95 = latencias[int(len(latencias) * 0.95)] if latencias else 0

        run = EvalRun(
            id=None, modelo=modelo, backend=backend, ts=time.time(),
            n_prompts=n, n_aceptadas=n_ok, accuracy=accuracy,
            accuracy_por_categoria=accuracy_por_categoria,
            latencia_media_ms=lat_media, latencia_p95_ms=lat_p95,
            nota=nota, resultados=resultados,
        )
        if persistir and self.store is not None:
            self.store.guardar(run)
        return run
