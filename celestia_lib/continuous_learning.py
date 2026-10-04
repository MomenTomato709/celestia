"""Aprendizaje continuo con LoRA — roadmap AGI punto #2.

Resuelve la crítica "no hay aprendizaje profundo continuo real" añadiendo una
pipeline completa para fine-tunear el modelo base local con las conversaciones
que va acumulando Celestia.

Componentes:

- `DatasetBuilder`: extrae conversaciones desde `MemoryDB` (SQLite), las
  convierte a formato chat estándar (user/assistant), aplica filtros
  (coherencia mínima, longitud, dedupe), genera splits train/val.

- `LoRATrainer`: configura PEFT/LoRA sobre el modelo base. Detecta hardware
  (GPU/RAM); si los recursos son insuficientes, devuelve estado
  ``hardware_insuficiente`` sin fingir entrenamiento. Persiste adaptadores y
  métricas en SQLite y en disco.

- `AdapterManager`: gestiona adaptadores entrenados. Lista, carga el más
  reciente, aplica al `ModelWrapper`. Soporta múltiples adaptadores.

- `LearningScheduler`: dispara entrenamiento cuando hay ≥N conversaciones
  nuevas no usadas y los recursos están disponibles (sin alertas del
  watchdog).

Honestidad explícita (documentar al usuario):

* En proot Android sin GPU dedicada el entrenamiento real es inviable. La
  pipeline queda lista para correr cuando haya hardware (PC con GPU, server).
* LoRA sobre un modelo de 0.5B parámetros NO es AGI; es ajuste fino limitado.
* Medimos perplexity sobre val_set, no comportamiento real. Una mejora de
  perplexity no garantiza mejor utilidad en conversación.

Sin imports pesados al top-level (regla aprendida del fix del embedder).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("celestia_v1")


# ─── Constantes ─────────────────────────────────────────────────────────

ESTADO_DRY_RUN = "dry_run"
ESTADO_OK = "ok"
ESTADO_ERROR = "error"
ESTADO_HARDWARE_INSUFICIENTE = "hardware_insuficiente"
ESTADO_SIN_DATOS = "sin_datos"

# Umbrales por defecto (overrideables por kwargs)
DEFAULT_MIN_COHERENCIA = 0.5
DEFAULT_MIN_LONGITUD_USER = 5      # chars
DEFAULT_MIN_LONGITUD_AI = 10       # chars
DEFAULT_MAX_LONGITUD_USER = 2000
DEFAULT_MAX_LONGITUD_AI = 4000
DEFAULT_VAL_SPLIT = 0.1
DEFAULT_MIN_EJEMPLOS_ENTRENAR = 50
DEFAULT_HORAS_ENTRE_ENTRENAMIENTOS = 24
DEFAULT_RAM_MINIMA_GB = 6.0        # umbral conservador para Qwen 0.5B + LoRA
DEFAULT_LORA_RANK = 8
DEFAULT_LORA_ALPHA = 16
DEFAULT_LORA_DROPOUT = 0.05
DEFAULT_LR = 2e-4
DEFAULT_BATCH = 2
DEFAULT_EPOCHS = 1
DEFAULT_MAX_SEQ_LEN = 512


# ─── Dataclasses ────────────────────────────────────────────────────────


@dataclass
class EjemploEntrenamiento:
    """Una conversación lista para fine-tuning en formato chat."""
    conv_id: int
    user: str
    assistant: str
    coherencia: float
    ts: float
    hash: str

    def to_chat(self) -> List[Dict[str, str]]:
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ]


@dataclass
class DatasetSplit:
    train: List[EjemploEntrenamiento] = field(default_factory=list)
    val: List[EjemploEntrenamiento] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.train) + len(self.val)


@dataclass
class ResultadoEntrenamiento:
    estado: str
    train_id: Optional[int] = None
    ruta_adapter: Optional[str] = None
    n_ejemplos: int = 0
    perplexity_base: Optional[float] = None
    perplexity_final: Optional[float] = None
    duracion_seg: float = 0.0
    mensaje: str = ""


# ─── Sandbox de schema ──────────────────────────────────────────────────


def _init_schema(conn: sqlite3.Connection) -> None:
    """Crea tablas idempotentes para tracking de entrenamientos."""
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS lora_entrenamientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL,
        modelo_base TEXT,
        n_ejemplos INTEGER,
        perplexity_base REAL,
        perplexity_final REAL,
        duracion_seg REAL,
        ruta_adapter TEXT,
        estado TEXT,
        mensaje TEXT,
        config_json TEXT
    );
    CREATE TABLE IF NOT EXISTS lora_ejemplos_usados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id INTEGER,
        train_id INTEGER,
        hash TEXT,
        ts REAL
    );
    CREATE TABLE IF NOT EXISTS teacher_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt TEXT NOT NULL,
        teacher_response TEXT NOT NULL,
        backend_teacher TEXT NOT NULL,
        ts REAL NOT NULL,
        hash TEXT NOT NULL UNIQUE,
        usado INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS conversation_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id INTEGER NOT NULL,
        valoracion INTEGER NOT NULL,
        comentario TEXT DEFAULT '',
        ts REAL NOT NULL,
        UNIQUE(conv_id)
    );
    CREATE INDEX IF NOT EXISTS idx_lora_train_ts ON lora_entrenamientos(ts);
    CREATE INDEX IF NOT EXISTS idx_lora_ejemplos_train ON lora_ejemplos_usados(train_id);
    CREATE INDEX IF NOT EXISTS idx_lora_ejemplos_conv ON lora_ejemplos_usados(conv_id);
    CREATE INDEX IF NOT EXISTS idx_teacher_signals_ts ON teacher_signals(ts);
    CREATE INDEX IF NOT EXISTS idx_teacher_signals_usado ON teacher_signals(usado);
    CREATE INDEX IF NOT EXISTS idx_feedback_conv ON conversation_feedback(conv_id);
    """)
    conn.commit()


# ─── DatasetBuilder ─────────────────────────────────────────────────────


class DatasetBuilder:
    """Extrae conversaciones de SQLite y produce un dataset listo para LoRA."""

    def __init__(self, db_path: str,
                 min_coherencia: float = DEFAULT_MIN_COHERENCIA,
                 min_longitud_user: int = DEFAULT_MIN_LONGITUD_USER,
                 min_longitud_ai: int = DEFAULT_MIN_LONGITUD_AI,
                 max_longitud_user: int = DEFAULT_MAX_LONGITUD_USER,
                 max_longitud_ai: int = DEFAULT_MAX_LONGITUD_AI):
        self.db_path = db_path
        self.min_coherencia = float(min_coherencia)
        self.min_longitud_user = int(min_longitud_user)
        self.min_longitud_ai = int(min_longitud_ai)
        self.max_longitud_user = int(max_longitud_user)
        self.max_longitud_ai = int(max_longitud_ai)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        _init_schema(self.conn)

    # ─── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _hash_par(user: str, assistant: str) -> str:
        h = hashlib.sha256()
        h.update(user.strip().lower().encode("utf-8", "ignore"))
        h.update(b"\x1f")
        h.update(assistant.strip().lower().encode("utf-8", "ignore"))
        return h.hexdigest()

    def _es_valido(self, user: str, assistant: str, coherencia: float) -> bool:
        if not isinstance(user, str) or not isinstance(assistant, str):
            return False
        u, a = user.strip(), assistant.strip()
        if len(u) < self.min_longitud_user or len(u) > self.max_longitud_user:
            return False
        if len(a) < self.min_longitud_ai or len(a) > self.max_longitud_ai:
            return False
        if coherencia < self.min_coherencia:
            return False
        # Heurística anti-error: respuestas fallback no aportan
        bajas = ("estoy reiniciando", "no lo sé", "no pude", "error interno")
        a_lower = a.lower()
        if any(a_lower.startswith(b) for b in bajas) and len(a) < 80:
            return False
        return True

    def _ids_ya_usados(self) -> set:
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT conv_id FROM lora_ejemplos_usados WHERE conv_id IS NOT NULL")
        return {row[0] for row in cur.fetchall()}

    # ─── API pública ────────────────────────────────────────────────────

    def construir(self, limite: Optional[int] = None,
                  excluir_usados: bool = True,
                  val_split: float = DEFAULT_VAL_SPLIT,
                  semilla: int = 1234) -> DatasetSplit:
        """Devuelve un DatasetSplit con train/val.

        excluir_usados=True descarta conversaciones ya consumidas por entrenamientos anteriores
        (clave para aprendizaje incremental sin reusar data).
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, user_input, ai_response, ts, coherence
            FROM conversations
            ORDER BY ts DESC
        """)
        filas = cur.fetchall()

        usados = self._ids_ya_usados() if excluir_usados else set()
        vistos_hash: set = set()
        ejemplos: List[EjemploEntrenamiento] = []

        for conv_id, user, ai, ts, coh in filas:
            if conv_id in usados:
                continue
            coh = float(coh or 0.0)
            user = user or ""
            ai = ai or ""
            if not self._es_valido(user, ai, coh):
                continue
            h = self._hash_par(user, ai)
            if h in vistos_hash:
                continue
            vistos_hash.add(h)
            ejemplos.append(EjemploEntrenamiento(
                conv_id=int(conv_id), user=user.strip(), assistant=ai.strip(),
                coherencia=coh, ts=float(ts or 0.0), hash=h,
            ))
            if limite and len(ejemplos) >= limite:
                break

        # Split determinista: ordena por hash y separa
        ejemplos.sort(key=lambda e: e.hash)
        n_val = max(1, int(len(ejemplos) * val_split)) if ejemplos else 0
        # Asegura que val no se coma todo cuando hay pocos
        if n_val >= len(ejemplos):
            n_val = max(0, len(ejemplos) - 1)
        val = ejemplos[:n_val]
        train = ejemplos[n_val:]

        # Reordena por ts (más útil para presentación)
        train.sort(key=lambda e: e.ts)
        val.sort(key=lambda e: e.ts)
        return DatasetSplit(train=train, val=val)

    def stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        # La tabla `conversations` la crea MemoryDB. En tests aislados
        # (DatasetBuilder con :memory:) puede no existir → devolvemos 0.
        try:
            cur.execute("SELECT COUNT(*) FROM conversations")
            total = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM conversations
                WHERE coherence >= ? AND LENGTH(user_input) >= ?
                  AND LENGTH(ai_response) >= ?
            """, (self.min_coherencia, self.min_longitud_user, self.min_longitud_ai))
            elegibles = cur.fetchone()[0]
        except sqlite3.OperationalError:
            total = 0
            elegibles = 0
        usados = len(self._ids_ya_usados())
        cur.execute("SELECT COUNT(*) FROM teacher_signals WHERE usado=0")
        teacher_libres = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM conversation_feedback WHERE valoracion=1"
        )
        feedback_pos = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM conversation_feedback WHERE valoracion=-1"
        )
        feedback_neg = cur.fetchone()[0]
        return {
            "total_conversations": total,
            "elegibles": elegibles,
            "ya_usados": usados,
            "disponibles_aprox": max(0, elegibles - usados),
            "teacher_signals_disponibles": teacher_libres,
            "feedback_positivos": feedback_pos,
            "feedback_negativos": feedback_neg,
        }

    # ─── Teacher signals (distillation Groq/OpenRouter → Qwen) ──────────

    def registrar_teacher_signal(
        self, prompt: str, teacher_response: str,
        backend_teacher: str, ts: Optional[float] = None,
    ) -> Optional[int]:
        """Guarda una respuesta del modelo profesor para usar en distillation.

        Idempotente por hash(prompt+respuesta): si ya existe, devuelve None
        (no añade duplicado). Esto permite llamarlo desde el loop respond
        sin preocuparse por evitar duplicados manualmente.
        """
        prompt = (prompt or "").strip()
        resp = (teacher_response or "").strip()
        if not self._es_valido(prompt, resp, self.min_coherencia):
            return None
        h = self._hash_par(prompt, resp)
        ts = float(ts if ts is not None else time.time())
        try:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO teacher_signals "
                "(prompt, teacher_response, backend_teacher, ts, hash, usado) "
                "VALUES (?,?,?,?,?,0)",
                (prompt, resp, backend_teacher, ts, h),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # Duplicado por UNIQUE(hash). Esperado en uso normal.
            return None

    def from_teacher_signals(
        self, limite: Optional[int] = None,
        excluir_usados: bool = True,
        val_split: float = DEFAULT_VAL_SPLIT,
    ) -> DatasetSplit:
        """Construye dataset a partir de teacher_signals (distillation).

        Cada signal se trata como un par (user=prompt, assistant=teacher_response).
        El conv_id se ajusta a -signal_id para no chocar con conversations reales.
        """
        cur = self.conn.cursor()
        if excluir_usados:
            cur.execute(
                "SELECT id, prompt, teacher_response, ts, hash "
                "FROM teacher_signals WHERE usado=0 ORDER BY ts DESC"
            )
        else:
            cur.execute(
                "SELECT id, prompt, teacher_response, ts, hash "
                "FROM teacher_signals ORDER BY ts DESC"
            )
        ejemplos: List[EjemploEntrenamiento] = []
        for sid, user, ai, ts, h in cur.fetchall():
            user = user or ""
            ai = ai or ""
            if not self._es_valido(user, ai, 1.0):
                continue
            ejemplos.append(EjemploEntrenamiento(
                conv_id=-int(sid),  # negativo para distinguir de conversations
                user=user.strip(), assistant=ai.strip(),
                coherencia=1.0, ts=float(ts or 0.0), hash=h,
            ))
            if limite and len(ejemplos) >= limite:
                break
        ejemplos.sort(key=lambda e: e.hash)
        n_val = max(1, int(len(ejemplos) * val_split)) if ejemplos else 0
        if n_val >= len(ejemplos):
            n_val = max(0, len(ejemplos) - 1)
        val = ejemplos[:n_val]
        train = ejemplos[n_val:]
        train.sort(key=lambda e: e.ts)
        val.sort(key=lambda e: e.ts)
        return DatasetSplit(train=train, val=val)

    def marcar_teacher_signals_usados(self, signal_ids: List[int]) -> int:
        """Marca signals como ya usados en un entrenamiento. Devuelve nº afectados."""
        if not signal_ids:
            return 0
        # signal_ids vienen como conv_id negativo desde el dataset; los normalizamos.
        positivos = [abs(int(sid)) for sid in signal_ids]
        placeholders = ",".join("?" * len(positivos))
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE teacher_signals SET usado=1 WHERE id IN ({placeholders})",
            positivos,
        )
        self.conn.commit()
        return cur.rowcount

    # ─── Feedback (DPO pipeline lista, sin entrenar todavía) ───────────

    def registrar_feedback(
        self, conv_id: int, valoracion: int, comentario: str = "",
    ) -> bool:
        """Registra 👍 (1) / 👎 (-1) / neutro (0) sobre una conversación.

        Idempotente por conv_id: si ya hay feedback, lo actualiza.
        """
        if valoracion not in (-1, 0, 1):
            raise ValueError(f"valoracion debe ser -1/0/1, recibido: {valoracion}")
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO conversation_feedback (conv_id, valoracion, comentario, ts) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(conv_id) DO UPDATE SET "
            "valoracion=excluded.valoracion, comentario=excluded.comentario, ts=excluded.ts",
            (int(conv_id), int(valoracion), str(comentario or "")[:500], time.time()),
        )
        self.conn.commit()
        return True

    def from_feedback(self, limite: Optional[int] = None) -> List[Dict[str, Any]]:
        """Devuelve pares (prompt, preferred, rejected) listos para DPO.

        Para cada conversación con feedback positivo busca la respuesta-actual
        como preferida. La "rejected" se sintetiza como el primer fallback de
        la conversación (si existe) o como la respuesta de una conversación
        con feedback negativo al mismo o similar prompt.

        Versión simplificada: ahora mismo devuelve solo los positivos como
        (prompt, preferred, rejected="") — el entrenamiento DPO real requiere
        emparejado más fino que dejamos para cuando haya volumen suficiente.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT cf.conv_id, c.user_input, c.ai_response, cf.valoracion "
            "FROM conversation_feedback cf "
            "JOIN conversations c ON c.id = cf.conv_id "
            "WHERE cf.valoracion = 1 "
            "ORDER BY cf.ts DESC"
        )
        rows = cur.fetchall()
        if limite:
            rows = rows[:limite]
        # Para cada positivo, intentar emparejar con un negativo (cualquier conv).
        cur.execute(
            "SELECT c.user_input, c.ai_response FROM conversation_feedback cf "
            "JOIN conversations c ON c.id = cf.conv_id "
            "WHERE cf.valoracion = -1 ORDER BY cf.ts DESC"
        )
        negativos = cur.fetchall()
        pares: List[Dict[str, Any]] = []
        for conv_id, user, preferred, _val in rows:
            rejected = ""
            # Busca un negativo con el mismo o similar prompt
            for n_user, n_resp in negativos:
                if (n_user or "").strip() == (user or "").strip():
                    rejected = n_resp
                    break
            pares.append({
                "conv_id": int(conv_id),
                "prompt": (user or "").strip(),
                "preferred": (preferred or "").strip(),
                "rejected": (rejected or "").strip(),
            })
        return pares

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── AdapterManager ─────────────────────────────────────────────────────


class AdapterManager:
    """Persiste, lista y carga adaptadores LoRA entrenados."""

    def __init__(self, db_path: str, adapters_dir: str):
        self.db_path = db_path
        self.adapters_dir = Path(adapters_dir)
        self.adapters_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        _init_schema(self.conn)

    def listar(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ts, modelo_base, n_ejemplos, perplexity_base,
                   perplexity_final, duracion_seg, ruta_adapter, estado, mensaje
            FROM lora_entrenamientos
            ORDER BY ts DESC
        """)
        out = []
        for row in cur.fetchall():
            out.append({
                "id": row[0], "ts": row[1], "modelo_base": row[2],
                "n_ejemplos": row[3], "perplexity_base": row[4],
                "perplexity_final": row[5], "duracion_seg": row[6],
                "ruta_adapter": row[7], "estado": row[8], "mensaje": row[9],
            })
        return out

    def ultimo_exitoso(self) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ts, modelo_base, n_ejemplos, perplexity_base,
                   perplexity_final, duracion_seg, ruta_adapter, estado, mensaje
            FROM lora_entrenamientos
            WHERE estado = ? AND ruta_adapter IS NOT NULL
            ORDER BY ts DESC
            LIMIT 1
        """, (ESTADO_OK,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "ts": row[1], "modelo_base": row[2],
            "n_ejemplos": row[3], "perplexity_base": row[4],
            "perplexity_final": row[5], "duracion_seg": row[6],
            "ruta_adapter": row[7], "estado": row[8], "mensaje": row[9],
        }

    def registrar(self, resultado: ResultadoEntrenamiento,
                  modelo_base: str, config: Dict[str, Any],
                  ejemplos_conv_ids: List[int]) -> int:
        """Persiste un entrenamiento + marca conversations como usadas."""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO lora_entrenamientos
            (ts, modelo_base, n_ejemplos, perplexity_base, perplexity_final,
             duracion_seg, ruta_adapter, estado, mensaje, config_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            time.time(), modelo_base, resultado.n_ejemplos,
            resultado.perplexity_base, resultado.perplexity_final,
            resultado.duracion_seg, resultado.ruta_adapter, resultado.estado,
            resultado.mensaje, json.dumps(config, default=str),
        ))
        train_id = cur.lastrowid
        # Marca convs como usadas SOLO si el entrenamiento tuvo éxito o dry_run
        if resultado.estado in (ESTADO_OK, ESTADO_DRY_RUN):
            now = time.time()
            for conv_id in ejemplos_conv_ids:
                cur.execute("""
                    INSERT INTO lora_ejemplos_usados (conv_id, train_id, ts)
                    VALUES (?,?,?)
                """, (int(conv_id), train_id, now))
        self.conn.commit()
        return int(train_id)

    def aplicar_a_modelo(self, modelo, tokenizer, ruta_adapter: str):
        """Carga el adaptador LoRA sobre el modelo base.

        Devuelve el modelo con el adaptador aplicado, o el modelo original
        si peft/los pesos no están disponibles. Nunca lanza — modo defensivo.
        """
        try:
            from peft import PeftModel  # import perezoso
            ruta = Path(ruta_adapter)
            if not ruta.exists():
                logger.warning("Adaptador no encontrado: %s", ruta_adapter)
                return modelo
            return PeftModel.from_pretrained(modelo, str(ruta))
        except ImportError:
            logger.info("peft no disponible — adaptador no aplicado")
            return modelo
        except Exception as e:
            logger.warning("No se pudo aplicar adaptador %s: %s", ruta_adapter, e)
            return modelo

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── Detección de recursos ─────────────────────────────────────────────


def _ram_total_gb() -> float:
    """RAM total del sistema en GB. Devuelve 0.0 si no se puede detectar."""
    try:
        import psutil  # type: ignore
        return float(psutil.virtual_memory().total) / (1024 ** 3)
    except Exception:
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)
        except Exception:
            return 0.0
    return 0.0


def _gpu_disponible() -> bool:
    try:
        import torch  # type: ignore
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _peft_disponible() -> bool:
    try:
        import peft  # noqa: F401
        return True
    except Exception:
        return False


def detectar_hardware(ram_minima_gb: float = DEFAULT_RAM_MINIMA_GB) -> Dict[str, Any]:
    """Devuelve un dict con el estado del hardware y si es entrenable."""
    ram = _ram_total_gb()
    gpu = _gpu_disponible()
    peft = _peft_disponible()
    entrenable = peft and (gpu or ram >= ram_minima_gb)
    return {
        "ram_gb": round(ram, 2),
        "gpu": gpu,
        "peft": peft,
        "entrenable": entrenable,
        "razon": _razon_no_entrenable(ram, gpu, peft, ram_minima_gb),
    }


def _razon_no_entrenable(ram: float, gpu: bool, peft: bool, ram_min: float) -> str:
    if not peft:
        return "peft no instalado (pip install peft transformers accelerate)"
    if not gpu and ram < ram_min:
        return f"sin GPU y RAM {ram:.1f}GB < {ram_min}GB requeridos"
    return ""


# ─── LoRATrainer ────────────────────────────────────────────────────────


class LoRATrainer:
    """Entrena LoRA sobre el modelo base. Modo dry_run si no hay hardware."""

    def __init__(self, modelo_base: str, adapters_dir: str,
                 ram_minima_gb: float = DEFAULT_RAM_MINIMA_GB,
                 rank: int = DEFAULT_LORA_RANK,
                 alpha: int = DEFAULT_LORA_ALPHA,
                 dropout: float = DEFAULT_LORA_DROPOUT,
                 lr: float = DEFAULT_LR,
                 batch: int = DEFAULT_BATCH,
                 epochs: int = DEFAULT_EPOCHS,
                 max_seq_len: int = DEFAULT_MAX_SEQ_LEN):
        self.modelo_base = modelo_base
        self.adapters_dir = Path(adapters_dir)
        self.adapters_dir.mkdir(parents=True, exist_ok=True)
        self.ram_minima_gb = float(ram_minima_gb)
        self.config = {
            "rank": int(rank), "alpha": int(alpha), "dropout": float(dropout),
            "lr": float(lr), "batch": int(batch), "epochs": int(epochs),
            "max_seq_len": int(max_seq_len),
        }

    def _format_chat_text(self, ejemplo: EjemploEntrenamiento) -> str:
        return (
            f"<|im_start|>user\n{ejemplo.user}<|im_end|>\n"
            f"<|im_start|>assistant\n{ejemplo.assistant}<|im_end|>"
        )

    def entrenar(self, split: DatasetSplit,
                 dry_run: bool = False) -> ResultadoEntrenamiento:
        """Entrena (o simula) un LoRA sobre el split.

        dry_run=True valida la pipeline sin tocar GPU/disco — útil para tests.
        Si el hardware no alcanza y dry_run=False, devuelve hardware_insuficiente.
        """
        t0 = time.time()
        if split.total == 0:
            return ResultadoEntrenamiento(
                estado=ESTADO_SIN_DATOS, n_ejemplos=0,
                duracion_seg=time.time() - t0,
                mensaje="Dataset vacío.",
            )

        hw = detectar_hardware(self.ram_minima_gb)
        if dry_run:
            return ResultadoEntrenamiento(
                estado=ESTADO_DRY_RUN, n_ejemplos=split.total,
                duracion_seg=time.time() - t0,
                mensaje=f"Dry run OK. Hardware: {hw}",
            )

        if not hw["entrenable"]:
            return ResultadoEntrenamiento(
                estado=ESTADO_HARDWARE_INSUFICIENTE, n_ejemplos=split.total,
                duracion_seg=time.time() - t0,
                mensaje=hw["razon"],
            )

        # Entrenamiento real — imports perezosos
        try:
            return self._entrenar_real(split, hw, t0)
        except Exception as e:
            logger.exception("Error durante entrenamiento LoRA")
            return ResultadoEntrenamiento(
                estado=ESTADO_ERROR, n_ejemplos=split.total,
                duracion_seg=time.time() - t0,
                mensaje=f"{type(e).__name__}: {e}",
            )

    def _entrenar_real(self, split: DatasetSplit, hw: Dict[str, Any],
                       t0: float) -> ResultadoEntrenamiento:
        """Implementación real del entrenamiento. Solo se invoca si hw["entrenable"]."""
        # Imports perezosos — no penalizar import del módulo
        import torch  # type: ignore
        from transformers import (  # type: ignore
            AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments,
            DataCollatorForLanguageModeling,
        )
        from peft import LoraConfig, get_peft_model, TaskType  # type: ignore
        from datasets import Dataset  # type: ignore

        device = "cuda" if hw["gpu"] else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(self.modelo_base)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        base = AutoModelForCausalLM.from_pretrained(
            self.modelo_base,
            torch_dtype=torch.float16 if hw["gpu"] else torch.float32,
        )
        base.to(device)

        ppl_base = self._calcular_perplexity(base, tokenizer, split.val, device)

        lora_cfg = LoraConfig(
            r=self.config["rank"], lora_alpha=self.config["alpha"],
            lora_dropout=self.config["dropout"],
            bias="none", task_type=TaskType.CAUSAL_LM,
        )
        model = get_peft_model(base, lora_cfg)

        def _tokenizar(textos: List[str]):
            return tokenizer(
                textos, truncation=True, padding="max_length",
                max_length=self.config["max_seq_len"], return_tensors=None,
            )

        train_texts = [self._format_chat_text(e) for e in split.train]
        val_texts = [self._format_chat_text(e) for e in split.val]
        train_ds = Dataset.from_dict(_tokenizar(train_texts))
        val_ds = Dataset.from_dict(_tokenizar(val_texts)) if val_texts else None

        ts = int(time.time())
        out_dir = self.adapters_dir / f"adapter_{ts}"
        args = TrainingArguments(
            output_dir=str(out_dir / "trainer"),
            num_train_epochs=self.config["epochs"],
            per_device_train_batch_size=self.config["batch"],
            learning_rate=self.config["lr"],
            logging_steps=10,
            save_strategy="no",
            report_to=[],
        )
        collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
        trainer = Trainer(
            model=model, args=args, train_dataset=train_ds,
            eval_dataset=val_ds, data_collator=collator,
        )
        trainer.train()

        ppl_final = self._calcular_perplexity(model, tokenizer, split.val, device)
        out_dir.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(str(out_dir))
        tokenizer.save_pretrained(str(out_dir))

        return ResultadoEntrenamiento(
            estado=ESTADO_OK, n_ejemplos=split.total,
            ruta_adapter=str(out_dir),
            perplexity_base=ppl_base, perplexity_final=ppl_final,
            duracion_seg=time.time() - t0,
            mensaje=f"OK device={device}",
        )

    def _calcular_perplexity(self, model, tokenizer,
                             ejemplos: List[EjemploEntrenamiento],
                             device: str) -> Optional[float]:
        if not ejemplos:
            return None
        import torch  # type: ignore
        try:
            model.eval()
            losses: List[float] = []
            for e in ejemplos[:32]:  # cap para velocidad
                text = self._format_chat_text(e)
                enc = tokenizer(text, return_tensors="pt",
                                truncation=True, max_length=self.config["max_seq_len"])
                input_ids = enc["input_ids"].to(device)
                with torch.no_grad():
                    out = model(input_ids=input_ids, labels=input_ids)
                losses.append(float(out.loss.item()))
            if not losses:
                return None
            avg_loss = sum(losses) / len(losses)
            import math
            return float(math.exp(avg_loss))
        except Exception as e:
            logger.debug("Perplexity fallo: %s", e)
            return None


# ─── LearningScheduler ─────────────────────────────────────────────────


class LearningScheduler:
    """Dispara entrenamientos cuando se cumplen condiciones (loop background)."""

    def __init__(self, builder: DatasetBuilder, trainer: LoRATrainer,
                 manager: AdapterManager,
                 min_ejemplos: int = DEFAULT_MIN_EJEMPLOS_ENTRENAR,
                 horas_entre: float = DEFAULT_HORAS_ENTRE_ENTRENAMIENTOS,
                 recursos_ok: Optional[Callable[[], bool]] = None,
                 dry_run: bool = False,
                 intervalo_check_seg: float = 3600.0):
        self.builder = builder
        self.trainer = trainer
        self.manager = manager
        self.min_ejemplos = int(min_ejemplos)
        self.horas_entre = float(horas_entre)
        self.recursos_ok = recursos_ok or (lambda: True)
        self.dry_run = bool(dry_run)
        self.intervalo_check_seg = float(intervalo_check_seg)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─── Política ─────────────────────────────────────────────────

    def _puede_entrenar(self) -> Tuple[bool, str]:
        ultimo = self.manager.ultimo_exitoso()
        if ultimo:
            edad_horas = (time.time() - float(ultimo["ts"])) / 3600.0
            if edad_horas < self.horas_entre:
                return False, f"último entrenamiento hace {edad_horas:.1f}h (<{self.horas_entre}h)"
        stats = self.builder.stats()
        if stats["disponibles_aprox"] < self.min_ejemplos:
            return False, f"solo {stats['disponibles_aprox']} ejemplos (<{self.min_ejemplos})"
        if not self.recursos_ok():
            return False, "recursos del sistema no disponibles (watchdog)"
        return True, ""

    def tick(self) -> Dict[str, Any]:
        """Una iteración: si se puede, entrena. Devuelve estado."""
        ok, razon = self._puede_entrenar()
        if not ok:
            return {"accion": "skip", "razon": razon}
        return self.entrenar_ahora()

    def entrenar_ahora(self) -> Dict[str, Any]:
        """Fuerza un entrenamiento ignorando políticas (excepto recursos)."""
        split = self.builder.construir()
        if split.total == 0:
            return {"accion": "skip", "razon": "dataset vacío tras filtros"}
        resultado = self.trainer.entrenar(split, dry_run=self.dry_run)
        conv_ids = [e.conv_id for e in split.train + split.val]
        train_id = self.manager.registrar(
            resultado, self.trainer.modelo_base, self.trainer.config, conv_ids,
        )
        return {
            "accion": "entrenado", "train_id": train_id,
            "estado": resultado.estado, "n_ejemplos": resultado.n_ejemplos,
            "ppl_base": resultado.perplexity_base,
            "ppl_final": resultado.perplexity_final,
            "duracion_seg": resultado.duracion_seg,
            "mensaje": resultado.mensaje,
        }

    # ─── Loop ─────────────────────────────────────────────────────

    def iniciar(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="LearningScheduler")
        self._thread.start()

    def detener(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("LearningScheduler.tick crash")
            self._stop.wait(self.intervalo_check_seg)


# ─── Helpers HTTP-friendly ─────────────────────────────────────────────


def estado_resumen(builder: DatasetBuilder,
                   manager: AdapterManager,
                   ram_minima_gb: float = DEFAULT_RAM_MINIMA_GB) -> Dict[str, Any]:
    """Resumen para endpoint /aprendizaje/estado."""
    return {
        "hardware": detectar_hardware(ram_minima_gb),
        "dataset": builder.stats(),
        "ultimo_entrenamiento": manager.ultimo_exitoso(),
        "historial_n": len(manager.listar()),
    }
