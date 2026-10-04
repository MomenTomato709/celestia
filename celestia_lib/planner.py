"""Planificador multiobjetivo con tree-of-thought (roadmap AGI punto #4).

Resuelve la crítica "Celestia hace task-execution, no razonamiento autónomo":
en vez de ejecutar single-shot, genera N planes alternativos para una
situación + objetivos, los evalúa multi-criterio (probabilidad de éxito,
coste, riesgo, alineación con cada objetivo), selecciona el mejor, y lo
ejecuta paso a paso con re-planificación al fallo.

Diseño:
- Una sola llamada LLM produce N planes Y sus scores (eficiencia + coherencia).
- Persistencia SQLite en `memoria/celestia.db` (tablas planner_*).
- Conexión propia (no comparte la de MemoryDB) — mantiene separación de concerns,
  permite tests con `:memory:` y aislamiento de errores.
- Sin imports pesados al top-level (regla aprendida del fix de embedder).

Filosofía honesta: esto NO es un planner de IA simbólica clásica tipo STRIPS/PDDL.
Es tree-of-thought con LLM como generador y juez. Suficiente para razonar sobre
tareas complejas del usuario (organizar, planear, secuenciar) sin pretender ser
un solver óptimo.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .pddl import (
    Action as PDDLAction,
    GroundedAction,
    Literal,
    PDDLProblem,
    PlanNotFound,
    a_star,
    bfs,
    dominio_celestia,
    grounded_a_intent,
    lit,
)

logger = logging.getLogger("celestia_v1")


# ─── Datatypes ────────────────────────────────────────────────────────

@dataclass
class Goal:
    """Objetivo persistente con prioridad."""
    descripcion: str
    prioridad: int = 5                    # 1-10 (10 = máxima)
    criterio_exito: str = ""              # texto libre
    estado: str = "activo"                # activo|completado|abandonado
    id: Optional[int] = None
    creado_ts: float = field(default_factory=time.time)
    actualizado_ts: float = field(default_factory=time.time)


@dataclass
class PlanPaso:
    """Paso individual de un plan."""
    orden: int
    descripcion: str
    tool: Optional[str] = None            # None = paso de razonamiento sin tool
    args: Dict[str, Any] = field(default_factory=dict)
    criterio_exito: str = ""
    estado: str = "pendiente"             # pendiente|ejecutando|ok|fallido
    outcome: str = ""
    id: Optional[int] = None
    ejecutado_ts: Optional[float] = None


@dataclass
class Plan:
    """Plan completo con pasos y métricas multi-criterio."""
    situacion: str
    goal_ids: List[int]
    descripcion: str
    pasos: List[PlanPaso]
    probabilidad_exito: float = 0.5       # 0-1 estimado por LLM
    coste: float = 0.5                    # 0-1 estimado por LLM
    riesgo: float = 0.5                   # 0-1 estimado por LLM
    alineacion_goals: Dict[int, float] = field(default_factory=dict)  # goal_id → 0-1
    score: float = 0.0                    # 0-100, calculado por scoring multi-criterio
    justificacion: str = ""
    estado: str = "propuesto"             # propuesto|ejecutando|completado|fallido|replanificado
    plan_padre_id: Optional[int] = None
    id: Optional[int] = None
    creado_ts: float = field(default_factory=time.time)
    completado_ts: Optional[float] = None


# ─── Scoring ───────────────────────────────────────────────────────────

def calcular_score(
    prob_exito: float,
    coste: float,
    riesgo: float,
    alineacion_goals: Dict[int, float],
    prioridades_goals: Dict[int, int],
) -> float:
    """Score multiobjetivo en escala 0-100.

    Fórmula:
        score = 100 * P(éxito) * (1 - coste/2) * (1 - riesgo/2) * mean_ponderada(alineación)

    Donde mean_ponderada usa las prioridades como pesos. Goals sin alineación
    explícita se asume 0.5 (neutro). Goals sin prioridad explícita se asume 5.

    El factor `1 - x/2` para coste/riesgo (no `1 - x`) evita que un plan con
    coste=1 quede en 0: aún con coste máximo, el plan vale la mitad. Coherente
    con la idea de que un plan caro pero seguro y útil puede preferirse a uno
    barato pero arriesgado.
    """
    prob = max(0.0, min(1.0, prob_exito))
    c = max(0.0, min(1.0, coste))
    r = max(0.0, min(1.0, riesgo))
    if not alineacion_goals or not prioridades_goals:
        alineacion_promedio = 0.5
    else:
        suma_pesos = 0.0
        suma_alin_pond = 0.0
        for gid, prio in prioridades_goals.items():
            alin = max(0.0, min(1.0, alineacion_goals.get(gid, 0.5)))
            suma_pesos += prio
            suma_alin_pond += alin * prio
        alineacion_promedio = (suma_alin_pond / suma_pesos) if suma_pesos > 0 else 0.5
    raw = prob * (1 - c / 2) * (1 - r / 2) * alineacion_promedio
    return round(100.0 * raw, 2)


# ─── Planner principal ────────────────────────────────────────────────

# Prompt LLM separado como constante para que tests puedan inspeccionarlo
# sin invocar el LLM real.
_PROMPT_GENERAR_PLANES = """Eres el planificador estratégico de Celestia. Genera EXACTAMENTE {n} planes ALTERNATIVOS para la situación, evalúa cada uno multi-criterio, y devuelve TODO en JSON.

SITUACIÓN: «{situacion}»

OBJETIVOS (id: descripción [prioridad 1-10]):
{goals_fmt}

{world_state_block}HERRAMIENTAS DISPONIBLES:
{tools_fmt}

Para cada plan:
- 3 a 7 pasos secuenciales.
- Cada paso: descripcion, tool (nombre exacto de herramienta o null si es razonamiento), args (dict), criterio_exito.
- Evaluación realista: probabilidad_exito ∈ [0,1], coste ∈ [0,1], riesgo ∈ [0,1], alineacion_goals (dict goal_id → 0-1).
- Justificación breve de por qué este plan funciona.

Los 3 planes deben ser GENUINAMENTE DISTINTOS (diferente estrategia, no variantes), para que la evaluación discrimine.

Responde EXCLUSIVAMENTE con JSON (sin markdown):
{{
  "planes": [
    {{
      "descripcion": "resumen del plan en una línea",
      "pasos": [
        {{"descripcion": "...", "tool": "buscar_web|leer_archivo|...|null", "args": {{}}, "criterio_exito": "..."}}
      ],
      "probabilidad_exito": 0.0,
      "coste": 0.0,
      "riesgo": 0.0,
      "alineacion_goals": {{}},
      "justificacion": "..."
    }}
  ]
}}"""


class MultiObjectivePlanner:
    """Genera, evalúa, persiste y recupera planes multiobjetivo.

    Args:
        db_path: ruta al archivo SQLite (o ":memory:" para tests).
        llm_callable: función `(prompt: str, max_tokens: int = ...) -> str` que
            ejecuta el LLM. Si es None, `planificar()` lanza RuntimeError.
            En tests, pasar un mock que devuelva JSON fijo.
        herramientas_disponibles: lista de nombres de tools que el LLM puede
            incluir en los pasos. Si None, el prompt no lista herramientas
            (el LLM puede inventarlas — útil para tests, no para producción).
        n_alternativas: cuántos planes generar por situación. Default 3.
    """

    N_ALTERNATIVAS_DEFAULT = 3
    MAX_TOKENS_PLAN = 2000

    def __init__(
        self,
        db_path: Union[str, Path],
        llm_callable: Optional[Callable[[str], str]] = None,
        herramientas_disponibles: Optional[List[str]] = None,
        n_alternativas: int = N_ALTERNATIVAS_DEFAULT,
    ):
        self.db_path = str(db_path)
        self.llm = llm_callable
        self.herramientas = herramientas_disponibles or []
        self.n_alternativas = max(1, min(int(n_alternativas), 5))
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ─── Schema ───────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS planner_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descripcion TEXT NOT NULL,
            prioridad INTEGER NOT NULL DEFAULT 5,
            criterio_exito TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo',
            creado_ts REAL NOT NULL,
            actualizado_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS planner_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            situacion TEXT NOT NULL,
            goal_ids_json TEXT NOT NULL,
            descripcion TEXT,
            probabilidad_exito REAL NOT NULL DEFAULT 0.5,
            coste REAL NOT NULL DEFAULT 0.5,
            riesgo REAL NOT NULL DEFAULT 0.5,
            alineacion_goals_json TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            justificacion TEXT,
            estado TEXT NOT NULL DEFAULT 'propuesto',
            plan_padre_id INTEGER,
            creado_ts REAL NOT NULL,
            completado_ts REAL,
            FOREIGN KEY (plan_padre_id) REFERENCES planner_plans(id)
        );
        CREATE TABLE IF NOT EXISTS planner_pasos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            orden INTEGER NOT NULL,
            descripcion TEXT NOT NULL,
            tool TEXT,
            args_json TEXT,
            criterio_exito TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'pendiente',
            outcome TEXT DEFAULT '',
            ejecutado_ts REAL,
            FOREIGN KEY (plan_id) REFERENCES planner_plans(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_planner_goals_estado ON planner_goals(estado);
        CREATE INDEX IF NOT EXISTS idx_planner_plans_estado ON planner_plans(estado);
        CREATE INDEX IF NOT EXISTS idx_planner_plans_creado ON planner_plans(creado_ts);
        CREATE INDEX IF NOT EXISTS idx_planner_pasos_plan ON planner_pasos(plan_id, orden);
        """)
        self.conn.commit()

    # ─── CRUD goals ────────────────────────────────────────────────────

    def crear_goal(
        self,
        descripcion: str,
        prioridad: int = 5,
        criterio_exito: str = "",
    ) -> Goal:
        descripcion = descripcion.strip()
        if not descripcion:
            raise ValueError("descripcion no puede estar vacía")
        prioridad = max(1, min(10, int(prioridad)))
        ts = time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO planner_goals "
                "(descripcion, prioridad, criterio_exito, estado, creado_ts, actualizado_ts) "
                "VALUES (?,?,?,?,?,?)",
                (descripcion, prioridad, criterio_exito, "activo", ts, ts),
            )
            self.conn.commit()
            return Goal(
                id=int(cur.lastrowid),
                descripcion=descripcion,
                prioridad=prioridad,
                criterio_exito=criterio_exito,
                creado_ts=ts,
                actualizado_ts=ts,
            )

    def obtener_goal(self, goal_id: int) -> Optional[Goal]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM planner_goals WHERE id=?", (goal_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        return Goal(**{k: d[k] for k in (
            "id", "descripcion", "prioridad", "criterio_exito", "estado",
            "creado_ts", "actualizado_ts",
        )})

    def listar_goals(self, estado: Optional[str] = "activo") -> List[Goal]:
        cur = self.conn.cursor()
        if estado:
            cur.execute(
                "SELECT * FROM planner_goals WHERE estado=? ORDER BY prioridad DESC, creado_ts ASC",
                (estado,),
            )
        else:
            cur.execute("SELECT * FROM planner_goals ORDER BY prioridad DESC, creado_ts ASC")
        cols = [d[0] for d in cur.description]
        goals = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            goals.append(Goal(**{k: d[k] for k in (
                "id", "descripcion", "prioridad", "criterio_exito", "estado",
                "creado_ts", "actualizado_ts",
            )}))
        return goals

    def actualizar_estado_goal(self, goal_id: int, estado: str) -> bool:
        if estado not in ("activo", "completado", "abandonado"):
            raise ValueError(f"estado inválido: {estado}")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE planner_goals SET estado=?, actualizado_ts=? WHERE id=?",
                (estado, time.time(), goal_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ─── Generación de planes vía LLM ─────────────────────────────────

    def _format_goals(self, goals: List[Goal]) -> str:
        if not goals:
            return "(ningún objetivo específico — usa sentido común)"
        return "\n".join(
            f"  {g.id}: {g.descripcion} [prioridad {g.prioridad}]"
            + (f" | éxito: {g.criterio_exito}" if g.criterio_exito else "")
            for g in goals
        )

    def _format_tools(self) -> str:
        if not self.herramientas:
            return "(ninguna específica — puedes describir pasos sin tool)"
        return "  " + ", ".join(self.herramientas)

    def _construir_prompt(
        self, situacion: str, goals: List[Goal], world_state: str = ""
    ) -> str:
        world_block = (
            f"CONTEXTO ACTUAL DEL MUNDO:\n{world_state.strip()}\n\n"
            if world_state.strip() else ""
        )
        return _PROMPT_GENERAR_PLANES.format(
            n=self.n_alternativas,
            situacion=situacion.strip(),
            goals_fmt=self._format_goals(goals),
            world_state_block=world_block,
            tools_fmt=self._format_tools(),
        )

    def _parsear_respuesta_llm(
        self,
        raw: str,
        situacion: str,
        goals: List[Goal],
    ) -> List[Plan]:
        """Convierte respuesta LLM en lista de Plans. Tolerante a JSON malformado."""
        # Limpia markdown si lo trae
        limpio = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            data = json.loads(limpio)
        except json.JSONDecodeError as e:
            logger.warning("Planner: JSON malformado del LLM: %s", e)
            # Intentar extraer el primer bloque {...} balanceado
            m = re.search(r"\{[\s\S]*\}", limpio)
            if not m:
                return []
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return []
        planes_raw = data.get("planes") if isinstance(data, dict) else None
        if not isinstance(planes_raw, list):
            return []
        prioridades = {g.id: g.prioridad for g in goals if g.id is not None}
        goal_ids = [g.id for g in goals if g.id is not None]
        planes: List[Plan] = []
        for p in planes_raw[: self.n_alternativas]:
            if not isinstance(p, dict):
                continue
            pasos_raw = p.get("pasos") or []
            pasos: List[PlanPaso] = []
            for i, ps in enumerate(pasos_raw if isinstance(pasos_raw, list) else []):
                if not isinstance(ps, dict):
                    continue
                tool = ps.get("tool")
                if isinstance(tool, str) and tool.lower() in ("null", "ninguna", "none", ""):
                    tool = None
                args = ps.get("args") if isinstance(ps.get("args"), dict) else {}
                pasos.append(PlanPaso(
                    orden=i + 1,
                    descripcion=str(ps.get("descripcion", "")).strip()[:500],
                    tool=tool if isinstance(tool, str) else None,
                    args=args,
                    criterio_exito=str(ps.get("criterio_exito", "")).strip()[:300],
                ))
            # Coerciones defensivas de los floats
            def _f(v, default=0.5):
                try:
                    return max(0.0, min(1.0, float(v)))
                except (ValueError, TypeError):
                    return default
            prob = _f(p.get("probabilidad_exito"), 0.5)
            coste = _f(p.get("coste"), 0.5)
            riesgo = _f(p.get("riesgo"), 0.5)
            alin_raw = p.get("alineacion_goals") or {}
            alineacion = {}
            if isinstance(alin_raw, dict):
                for k, v in alin_raw.items():
                    try:
                        gid = int(k)
                        alineacion[gid] = _f(v, 0.5)
                    except (ValueError, TypeError):
                        continue
            score = calcular_score(prob, coste, riesgo, alineacion, prioridades)
            planes.append(Plan(
                situacion=situacion,
                goal_ids=goal_ids,
                descripcion=str(p.get("descripcion", "")).strip()[:500],
                pasos=pasos,
                probabilidad_exito=prob,
                coste=coste,
                riesgo=riesgo,
                alineacion_goals=alineacion,
                score=score,
                justificacion=str(p.get("justificacion", "")).strip()[:1000],
            ))
        return planes

    def planificar(
        self,
        situacion: str,
        goal_ids: Optional[List[int]] = None,
        world_state: str = "",
        persistir: bool = True,
    ) -> Optional[Plan]:
        """Genera N planes, evalúa y devuelve el mejor.

        Args:
            situacion: descripción libre del problema/petición.
            goal_ids: ids de goals activos a considerar. Si None, usa todos los
                goals activos.
            world_state: contexto del mundo (típicamente `KnowledgeGraph.conocimiento_relevante`).
            persistir: si True, guarda el plan en SQLite (con sus pasos).

        Returns:
            El Plan ganador (max score), o None si el LLM no devolvió nada usable.
        """
        if self.llm is None:
            raise RuntimeError("Planner: llm_callable no configurado")
        if goal_ids is not None:
            goals = [g for g in (self.obtener_goal(gid) for gid in goal_ids) if g]
        else:
            goals = self.listar_goals(estado="activo")
        prompt = self._construir_prompt(situacion, goals, world_state)
        try:
            raw = self.llm(prompt)
        except Exception as e:
            logger.warning("Planner: LLM falló: %s", e)
            return None
        if not raw:
            return None
        planes = self._parsear_respuesta_llm(raw, situacion, goals)
        if not planes:
            logger.warning("Planner: LLM no devolvió planes válidos")
            return None
        mejor = max(planes, key=lambda p: p.score)
        if persistir:
            self.guardar_plan(mejor)
        return mejor

    # ─── Persistencia de planes ───────────────────────────────────────

    def guardar_plan(self, plan: Plan) -> int:
        ts = time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO planner_plans "
                "(situacion, goal_ids_json, descripcion, probabilidad_exito, coste, "
                "riesgo, alineacion_goals_json, score, justificacion, estado, "
                "plan_padre_id, creado_ts, completado_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.situacion,
                    json.dumps(plan.goal_ids),
                    plan.descripcion,
                    plan.probabilidad_exito,
                    plan.coste,
                    plan.riesgo,
                    json.dumps(plan.alineacion_goals),
                    plan.score,
                    plan.justificacion,
                    plan.estado,
                    plan.plan_padre_id,
                    plan.creado_ts or ts,
                    plan.completado_ts,
                ),
            )
            plan_id = int(cur.lastrowid)
            plan.id = plan_id
            for paso in plan.pasos:
                cur.execute(
                    "INSERT INTO planner_pasos "
                    "(plan_id, orden, descripcion, tool, args_json, criterio_exito, "
                    "estado, outcome, ejecutado_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, paso.orden, paso.descripcion, paso.tool,
                        json.dumps(paso.args), paso.criterio_exito,
                        paso.estado, paso.outcome, paso.ejecutado_ts,
                    ),
                )
                paso.id = int(cur.lastrowid)
            self.conn.commit()
            return plan_id

    def obtener_plan(self, plan_id: int) -> Optional[Plan]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM planner_plans WHERE id=?", (plan_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        cur.execute(
            "SELECT * FROM planner_pasos WHERE plan_id=? ORDER BY orden",
            (plan_id,),
        )
        cols_p = [c[0] for c in cur.description]
        pasos: List[PlanPaso] = []
        for r in cur.fetchall():
            dp = dict(zip(cols_p, r))
            try:
                args = json.loads(dp.get("args_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            pasos.append(PlanPaso(
                id=dp["id"],
                orden=dp["orden"],
                descripcion=dp["descripcion"],
                tool=dp["tool"],
                args=args,
                criterio_exito=dp["criterio_exito"] or "",
                estado=dp["estado"],
                outcome=dp["outcome"] or "",
                ejecutado_ts=dp["ejecutado_ts"],
            ))
        try:
            goal_ids = json.loads(d["goal_ids_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            goal_ids = []
        try:
            alineacion = {int(k): float(v) for k, v in
                          json.loads(d["alineacion_goals_json"] or "{}").items()}
        except (json.JSONDecodeError, TypeError):
            alineacion = {}
        return Plan(
            id=d["id"],
            situacion=d["situacion"],
            goal_ids=goal_ids,
            descripcion=d["descripcion"] or "",
            pasos=pasos,
            probabilidad_exito=d["probabilidad_exito"],
            coste=d["coste"],
            riesgo=d["riesgo"],
            alineacion_goals=alineacion,
            score=d["score"],
            justificacion=d["justificacion"] or "",
            estado=d["estado"],
            plan_padre_id=d["plan_padre_id"],
            creado_ts=d["creado_ts"],
            completado_ts=d["completado_ts"],
        )

    def listar_planes(
        self, estado: Optional[str] = None, limit: int = 20
    ) -> List[Plan]:
        cur = self.conn.cursor()
        if estado:
            cur.execute(
                "SELECT id FROM planner_plans WHERE estado=? "
                "ORDER BY creado_ts DESC LIMIT ?",
                (estado, limit),
            )
        else:
            cur.execute(
                "SELECT id FROM planner_plans ORDER BY creado_ts DESC LIMIT ?",
                (limit,),
            )
        ids = [r[0] for r in cur.fetchall()]
        out = []
        for pid in ids:
            p = self.obtener_plan(pid)
            if p:
                out.append(p)
        return out

    def actualizar_paso(
        self,
        paso_id: int,
        estado: str,
        outcome: str = "",
    ) -> bool:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE planner_pasos SET estado=?, outcome=?, ejecutado_ts=? WHERE id=?",
                (estado, outcome, time.time(), paso_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def actualizar_estado_plan(
        self, plan_id: int, estado: str, completar: bool = False
    ) -> bool:
        ts = time.time() if completar else None
        with self._lock:
            cur = self.conn.cursor()
            if completar:
                cur.execute(
                    "UPDATE planner_plans SET estado=?, completado_ts=? WHERE id=?",
                    (estado, ts, plan_id),
                )
            else:
                cur.execute(
                    "UPDATE planner_plans SET estado=? WHERE id=?",
                    (estado, plan_id),
                )
            self.conn.commit()
            return cur.rowcount > 0

    # ─── Planificación PDDL (clásica, determinista) ───────────────────

    _PROMPT_NL_A_PDDL = """Eres un traductor de lenguaje natural a planificación PDDL (STRIPS).
Convierte la petición del usuario en un problema PDDL solvable por búsqueda BFS/A*.

PETICIÓN: «{situacion}»

CONTEXTO DEL MUNDO ACTUAL (estado conocido):
{world_state}

ACCIONES DISPONIBLES (nombre y parámetros):
{acciones_fmt}

PREDICADOS DISPONIBLES (usa SOLO estos):
- existe(?archivo): el archivo/recurso existe localmente
- online(): hay conexión a internet
- conocido(?obj): ya conozco/leí ese objeto
- enviado(?archivo): el archivo ya se envió al usuario
- listado(?ruta): ya listé esa ruta
- buscado(?query): ya busqué eso
- documento_creado(?tema): documento generado
- usuario_informado(?asunto): ya informé al usuario sobre algo
- sistema_inspeccionado(): ya consulté el estado del sistema
- recordatorio_programado(?mensaje): recordatorio creado

Devuelve EXCLUSIVAMENTE JSON, sin markdown:
{{
  "initial": ["online()", "existe(informe.pdf)"],
  "goal": ["enviado(informe.pdf)"],
  "objects": ["informe.pdf", "user@example.com"]
}}

Reglas:
- `initial`: lista de literales VERDADEROS al empezar (closed-world: lo que no listes se asume falso). Incluye `online()` si hay internet.
- `goal`: lista de literales que deben ser verdaderos al terminar.
- `objects`: TODAS las constantes que aparezcan en initial/goal (rutas, urls, queries, etc.).
- Sintaxis: `predicado(arg1, arg2, ...)` o `predicado()` para 0-aridad. Negación: `¬predicado(x)`.
- Sé conciso: solo los objetos y literales estrictamente necesarios para que el solver encuentre plan."""

    _LITERAL_RE = re.compile(r"^\s*(¬|!|not\s+)?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)\s*$")

    @classmethod
    def _parsear_literal(cls, raw: str) -> Optional[Literal]:
        if not raw or not isinstance(raw, str):
            return None
        m = cls._LITERAL_RE.match(raw)
        if not m:
            return None
        neg = bool(m.group(1))
        pred = m.group(2)
        args_raw = m.group(3).strip()
        if not args_raw:
            args: Tuple[str, ...] = ()
        else:
            args = tuple(a.strip() for a in args_raw.split(",") if a.strip())
        return Literal(predicate=pred, args=args, negated=neg)

    def _formatear_acciones(self, dominio: Tuple[PDDLAction, ...]) -> str:
        lineas = []
        for a in dominio:
            params = ", ".join(a.parameters) if a.parameters else ""
            pre = ", ".join(str(p) for p in a.preconditions) or "—"
            eff = ", ".join(str(e) for e in a.effects) or "—"
            lineas.append(f"  {a.name}({params})  pre: [{pre}]  eff: [{eff}]")
        return "\n".join(lineas)

    def _llm_a_pddl_problem(
        self,
        situacion: str,
        world_state: str,
        dominio: Tuple[PDDLAction, ...],
    ) -> Optional[PDDLProblem]:
        """Pide al LLM la traducción a PDDL y la valida. None si no parsea."""
        if self.llm is None:
            raise RuntimeError("Planner: llm_callable no configurado para PDDL")
        prompt = self._PROMPT_NL_A_PDDL.format(
            situacion=situacion.strip(),
            world_state=(world_state.strip() or "(sin contexto adicional)"),
            acciones_fmt=self._formatear_acciones(dominio),
        )
        try:
            raw = self.llm(prompt)
        except Exception as e:
            logger.warning("PDDL: LLM falló: %s", e)
            return None
        if not raw:
            return None
        limpio = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            data = json.loads(limpio)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", limpio)
            if not m:
                logger.warning("PDDL: respuesta LLM no es JSON: %.200s", limpio)
                return None
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        ini_lits = [self._parsear_literal(s) for s in (data.get("initial") or [])]
        goal_lits = [self._parsear_literal(s) for s in (data.get("goal") or [])]
        ini_lits = [l for l in ini_lits if l is not None]
        goal_lits = [l for l in goal_lits if l is not None]
        if not goal_lits:
            logger.warning("PDDL: el LLM no devolvió goal usable")
            return None
        objs_raw = data.get("objects") or []
        objects = tuple(str(o).strip() for o in objs_raw if str(o).strip()) if isinstance(objs_raw, list) else ()
        # Asegurar que toda constante en initial/goal está en objects
        nombres_objs = set(objects)
        for l in list(ini_lits) + list(goal_lits):
            for a in l.args:
                if not a.startswith("?") and a not in nombres_objs:
                    nombres_objs.add(a)
        objects = tuple(sorted(nombres_objs))
        return PDDLProblem(
            actions=dominio,
            initial=frozenset(ini_lits),
            goal=frozenset(goal_lits),
            objects=objects,
        )

    def _pddl_plan_a_plan(
        self,
        plan_ground: List[GroundedAction],
        situacion: str,
        goals: List[Goal],
        problem: PDDLProblem,
    ) -> Plan:
        """Convierte secuencia de GroundedActions en un `Plan` con `PlanPaso`s."""
        pasos: List[PlanPaso] = []
        for i, ga in enumerate(plan_ground):
            intent = grounded_a_intent(ga)
            tool_name = str(intent.get("tool") or "") or None
            args = intent.get("params") if isinstance(intent.get("params"), dict) else {}
            descripcion = f"{ga.name}({', '.join(ga.args)})" if ga.args else ga.name
            pasos.append(PlanPaso(
                orden=i + 1,
                descripcion=descripcion,
                tool=tool_name,
                args=args or {},
                criterio_exito=", ".join(str(e) for e in ga.effects)[:300],
            ))
        prioridades = {g.id: g.prioridad for g in goals if g.id is not None}
        goal_ids = [g.id for g in goals if g.id is not None]
        # Métricas: PDDL determinista — éxito alto, coste = suma de costs, riesgo bajo
        coste_total = sum(ga.cost for ga in plan_ground)
        coste_norm = min(1.0, coste_total / 10.0)
        alineacion = {gid: 0.8 for gid in goal_ids}
        score = calcular_score(
            prob_exito=0.9, coste=coste_norm, riesgo=0.1,
            alineacion_goals=alineacion, prioridades_goals=prioridades,
        )
        return Plan(
            situacion=situacion,
            goal_ids=goal_ids,
            descripcion=f"Plan PDDL ({len(plan_ground)} acciones, coste {coste_total:.1f})",
            pasos=pasos,
            probabilidad_exito=0.9,
            coste=coste_norm,
            riesgo=0.1,
            alineacion_goals=alineacion,
            score=score,
            justificacion=(
                f"Generado por búsqueda PDDL clásica (A*) sobre el dominio Celestia. "
                f"{len(plan_ground)} acciones, óptimo en coste. Estado inicial: "
                f"{len(problem.initial)} literales; goal: {len(problem.goal)} literales."
            ),
        )

    def planificar_pddl(
        self,
        situacion: str,
        goal_ids: Optional[List[int]] = None,
        world_state: str = "",
        dominio: Optional[Tuple[PDDLAction, ...]] = None,
        persistir: bool = True,
        max_nodes: int = 5000,
    ) -> Optional[Plan]:
        """Genera plan vía PDDL clásico (LLM traduce, A* resuelve).

        Determinista, óptimo en coste, no inventa pasos. Si el LLM no
        consigue traducir o el solver no encuentra plan, devuelve None
        para que el caller pueda caer a `planificar()` (tree-of-thought).
        """
        if dominio is None:
            dominio = dominio_celestia()
        if goal_ids is not None:
            goals = [g for g in (self.obtener_goal(gid) for gid in goal_ids) if g]
        else:
            goals = self.listar_goals(estado="activo")
        problem = self._llm_a_pddl_problem(situacion, world_state, dominio)
        if problem is None:
            return None
        try:
            plan_ground = a_star(problem, max_nodes=max_nodes)
        except PlanNotFound as e:
            logger.info("PDDL: %s", e)
            return None
        if not plan_ground:
            # Goal ya satisfecho — devolver plan vacío con paso informativo
            plan_ground = []
        plan = self._pddl_plan_a_plan(plan_ground, situacion, goals, problem)
        if persistir:
            self.guardar_plan(plan)
        return plan

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── Executor ──────────────────────────────────────────────────────────

class PlanExecutor:
    """Ejecuta un Plan paso a paso, observa resultados, replanifica al fallo.

    Args:
        planner: instancia de MultiObjectivePlanner (para persistir cambios + replan).
        tool_runner: callable `(tool_name: str, args: dict) -> tuple[bool, str]`
            que invoca una herramienta y devuelve (éxito, outcome). En tests, mock.
            Si None, los pasos con tool quedan como "fallido" — útil sólo para razonamiento.
        max_replans: máximo de re-planificaciones cuando un paso falla. Default 1.
    """

    def __init__(
        self,
        planner: MultiObjectivePlanner,
        tool_runner: Optional[Callable[[str, Dict[str, Any]], tuple]] = None,
        max_replans: int = 1,
    ):
        self.planner = planner
        self.tool_runner = tool_runner
        self.max_replans = max(0, int(max_replans))

    def ejecutar_paso(self, paso: PlanPaso) -> bool:
        """Ejecuta un paso. Devuelve True si OK, False si falló."""
        if paso.estado in ("ok", "fallido"):
            return paso.estado == "ok"
        # Sin tool → es paso de razonamiento; lo marcamos OK automáticamente
        if not paso.tool:
            paso.estado = "ok"
            paso.outcome = "(paso de razonamiento — sin tool)"
            paso.ejecutado_ts = time.time()
            if paso.id is not None:
                self.planner.actualizar_paso(paso.id, "ok", paso.outcome)
            return True
        if self.tool_runner is None:
            paso.estado = "fallido"
            paso.outcome = "tool_runner no configurado"
            paso.ejecutado_ts = time.time()
            if paso.id is not None:
                self.planner.actualizar_paso(paso.id, "fallido", paso.outcome)
            return False
        try:
            ok, outcome = self.tool_runner(paso.tool, paso.args or {})
        except Exception as e:
            ok, outcome = False, f"excepción: {e}"
        paso.estado = "ok" if ok else "fallido"
        paso.outcome = (outcome or "")[:1000]
        paso.ejecutado_ts = time.time()
        if paso.id is not None:
            self.planner.actualizar_paso(paso.id, paso.estado, paso.outcome)
        return ok

    def ejecutar(self, plan: Plan) -> Plan:
        """Ejecuta plan completo. Replanifica si un paso falla y quedan reintentos."""
        if plan.id is None:
            self.planner.guardar_plan(plan)
        self.planner.actualizar_estado_plan(plan.id, "ejecutando")
        plan.estado = "ejecutando"
        replans_usados = 0
        idx = 0
        while idx < len(plan.pasos):
            paso = plan.pasos[idx]
            ok = self.ejecutar_paso(paso)
            if ok:
                idx += 1
                continue
            # Paso falló — intentar replanificar si quedan reintentos
            if replans_usados >= self.max_replans:
                plan.estado = "fallido"
                self.planner.actualizar_estado_plan(plan.id, "fallido", completar=True)
                return plan
            replans_usados += 1
            nuevo_plan = self._replanificar(plan, paso)
            if nuevo_plan is None:
                plan.estado = "fallido"
                self.planner.actualizar_estado_plan(plan.id, "fallido", completar=True)
                return plan
            # Sustituir plan en curso y reiniciar ejecución del nuevo
            self.planner.actualizar_estado_plan(plan.id, "replanificado", completar=True)
            plan = nuevo_plan
            self.planner.actualizar_estado_plan(plan.id, "ejecutando")
            idx = 0
        plan.estado = "completado"
        self.planner.actualizar_estado_plan(plan.id, "completado", completar=True)
        return plan

    def _replanificar(self, plan_fallido: Plan, paso_fallido: PlanPaso) -> Optional[Plan]:
        """Genera nuevo plan tras un fallo. Pasa el contexto del fallo al LLM."""
        situacion_nueva = (
            f"REPLANIFICACIÓN. Situación original: {plan_fallido.situacion}\n"
            f"El plan anterior falló en el paso: «{paso_fallido.descripcion}»\n"
            f"Outcome del fallo: {paso_fallido.outcome}\n"
            "Genera planes alternativos que eviten este fallo."
        )
        try:
            nuevo = self.planner.planificar(
                situacion_nueva,
                goal_ids=plan_fallido.goal_ids,
                persistir=True,
            )
            if nuevo is not None:
                nuevo.plan_padre_id = plan_fallido.id
                # Persistir el padre del nuevo plan
                with self.planner._lock:
                    cur = self.planner.conn.cursor()
                    cur.execute(
                        "UPDATE planner_plans SET plan_padre_id=? WHERE id=?",
                        (plan_fallido.id, nuevo.id),
                    )
                    self.planner.conn.commit()
            return nuevo
        except Exception as e:
            logger.warning("Replanificación falló: %s", e)
            return None
