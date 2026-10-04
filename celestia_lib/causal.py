"""Simulación causal sobre grafo + predictor Markov del próximo acto.

Resuelve la limitación honesta del world model: el grafo simbólico responde
«qué pasa ahora» pero no «qué pasará si…». Aquí añadimos:

- `CausalGraph`: relaciones causa→efecto con probabilidad, delay y duración
  aprendidas por observación (actualización bayesiana incremental sobre un
  prior Beta(1,1)).
- `CausalSimulator`: Monte Carlo sobre el grafo causal. Dada una causa, proyecta
  efectos N pasos en el futuro con su distribución de probabilidad y tiempos.
- `MarkovPredictor`: predice el siguiente acto del usuario en función del
  contexto (hora-del-día bucketizada + día de la semana + última acción).
  Implementación: tabla de transiciones contadas, top-K por contexto.

Filosofía honesta:
- Esto NO es JEPA ni causal-discovery moderno (Pearl/Bareinboim). Es un
  simulador estadístico Bayesiano sobre relaciones declarativas. Suficiente
  para responder «si tomo café ahora, ¿cuándo se me pasará el efecto?» con
  los datos del usuario; insuficiente para descubrir leyes causales nuevas.
- Bayesian update con prior Beta(1,1) (uniforme): cada observación positiva
  incrementa α, cada observación negativa incrementa β. Probabilidad
  estimada = α / (α + β). Confianza ≈ α + β. Esto evita overfit con poca data.
- El predictor Markov no usa LLM, ni embeddings, ni transformers. Es una
  tabla de frecuencias por (bucket_hora, dia_semana, contexto_str). Si el
  usuario quiere predicciones más sofisticadas, este sería el bloque a
  reemplazar (no a extender) por un modelo más serio.
- Para que la demo no exija meses de uso, ofrecemos un `seed_dataset()` con
  10 relaciones causales habituales (café, alcohol, ejercicio, comida pesada,
  pantalla nocturna, etc.) que el usuario puede borrar/ajustar.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

logger = logging.getLogger("celestia_v1")


# ─── Constantes ───────────────────────────────────────────────────────

PRIOR_ALFA = 1.0           # Beta(1,1) — uniforme
PRIOR_BETA = 1.0
CONFIANZA_MIN_REPORTABLE = 3  # α+β ≥ 3 para reportar como aprendido (no semilla)

# Cubetas horarias para el predictor (suficientemente gruesas para generalizar)
BUCKETS_HORARIOS = [
    (0, 6, "madrugada"),
    (6, 12, "mañana"),
    (12, 18, "tarde"),
    (18, 24, "noche"),
]


def bucket_hora(hora: int) -> str:
    """Mapea hora 0-23 a bucket nombrado."""
    h = max(0, min(23, int(hora)))
    for ini, fin, nombre in BUCKETS_HORARIOS:
        if ini <= h < fin:
            return nombre
    return "noche"


_DIAS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


# ─── Datatypes ────────────────────────────────────────────────────────

@dataclass
class CausalLink:
    """Relación causal aprendida con estadísticas bayesianas.

    `alfa` y `beta` son los parámetros del posterior Beta. La probabilidad
    estimada es `alfa / (alfa + beta)` y la confianza crece con `alfa+beta`.

    `delay_seg_media` y `duracion_seg_media` son medias móviles simples; con
    poca data son ruidosas, por eso `n_obs_temporal` rastrea cuántas
    observaciones tienen información temporal.
    """
    causa: str
    efecto: str
    alfa: float = PRIOR_ALFA
    beta: float = PRIOR_BETA
    delay_seg_media: float = 0.0
    duracion_seg_media: float = 0.0
    n_obs_temporal: int = 0
    fuente: str = ""               # "seed" | "user" | "auto"
    id: Optional[int] = None
    creado_ts: float = field(default_factory=time.time)
    actualizado_ts: float = field(default_factory=time.time)

    @property
    def probabilidad(self) -> float:
        s = self.alfa + self.beta
        return self.alfa / s if s > 0 else 0.5

    @property
    def confianza(self) -> float:
        """0-1 según volumen de evidencia. Asintótico: nunca alcanza 1.0."""
        s = (self.alfa + self.beta) - (PRIOR_ALFA + PRIOR_BETA)
        return s / (s + 10.0) if s > 0 else 0.0

    @property
    def n_observaciones(self) -> int:
        return int(round(self.alfa + self.beta - PRIOR_ALFA - PRIOR_BETA))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "causa": self.causa,
            "efecto": self.efecto,
            "probabilidad": round(self.probabilidad, 4),
            "confianza": round(self.confianza, 3),
            "n_observaciones": self.n_observaciones,
            "delay_seg_media": int(self.delay_seg_media),
            "duracion_seg_media": int(self.duracion_seg_media),
            "fuente": self.fuente,
        }


@dataclass
class EventoSimulado:
    """Un efecto proyectado en el futuro por el simulador."""
    evento: str
    probabilidad: float          # P(este efecto ocurra dada la cadena causal)
    tiempo_desde_inicio_seg: float
    duracion_seg: float
    profundidad: int             # nº de saltos causales desde el evento raíz
    cadena: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evento": self.evento,
            "probabilidad": round(self.probabilidad, 4),
            "tiempo_desde_inicio_seg": int(self.tiempo_desde_inicio_seg),
            "duracion_seg": int(self.duracion_seg),
            "profundidad": self.profundidad,
            "cadena": self.cadena,
        }


# ─── Schema SQLite ────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS causal_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    causa TEXT NOT NULL,
    efecto TEXT NOT NULL,
    alfa REAL NOT NULL DEFAULT 1.0,
    beta REAL NOT NULL DEFAULT 1.0,
    delay_seg_media REAL NOT NULL DEFAULT 0,
    duracion_seg_media REAL NOT NULL DEFAULT 0,
    n_obs_temporal INTEGER NOT NULL DEFAULT 0,
    fuente TEXT DEFAULT '',
    creado_ts REAL NOT NULL,
    actualizado_ts REAL NOT NULL,
    UNIQUE(causa, efecto)
);
CREATE TABLE IF NOT EXISTS user_acciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accion TEXT NOT NULL,
    bucket_hora TEXT NOT NULL,
    dia_semana TEXT NOT NULL,
    contexto TEXT DEFAULT '',
    accion_previa TEXT DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_causal_causa ON causal_links(causa);
CREATE INDEX IF NOT EXISTS idx_causal_efecto ON causal_links(efecto);
CREATE INDEX IF NOT EXISTS idx_acciones_ctx ON user_acciones(bucket_hora, dia_semana);
CREATE INDEX IF NOT EXISTS idx_acciones_prev ON user_acciones(accion_previa);
CREATE INDEX IF NOT EXISTS idx_acciones_ts ON user_acciones(ts);
"""


# ─── CausalGraph ──────────────────────────────────────────────────────

class CausalGraph:
    """Grafo causal persistido en SQLite con actualización bayesiana incremental."""

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ─── CRUD ────────────────────────────────────────────────────────

    @staticmethod
    def _normalizar(s: str) -> str:
        return (s or "").strip().lower()

    def obtener(self, causa: str, efecto: str) -> Optional[CausalLink]:
        c, e = self._normalizar(causa), self._normalizar(efecto)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, causa, efecto, alfa, beta, delay_seg_media, "
            "duracion_seg_media, n_obs_temporal, fuente, creado_ts, actualizado_ts "
            "FROM causal_links WHERE causa=? AND efecto=?",
            (c, e),
        )
        row = cur.fetchone()
        return self._row_a_link(row) if row else None

    def listar(
        self, causa: Optional[str] = None,
        efecto: Optional[str] = None,
        min_confianza: float = 0.0,
        limit: int = 200,
    ) -> List[CausalLink]:
        cur = self.conn.cursor()
        wheres: List[str] = []
        params: List[Any] = []
        if causa:
            wheres.append("causa=?")
            params.append(self._normalizar(causa))
        if efecto:
            wheres.append("efecto=?")
            params.append(self._normalizar(efecto))
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(int(limit))
        cur.execute(
            f"SELECT id, causa, efecto, alfa, beta, delay_seg_media, "
            f"duracion_seg_media, n_obs_temporal, fuente, creado_ts, actualizado_ts "
            f"FROM causal_links {where_sql} ORDER BY (alfa - beta) DESC LIMIT ?",
            params,
        )
        out: List[CausalLink] = []
        for row in cur.fetchall():
            link = self._row_a_link(row)
            if link.confianza >= min_confianza:
                out.append(link)
        return out

    def _row_a_link(self, row: tuple) -> CausalLink:
        return CausalLink(
            id=row[0], causa=row[1], efecto=row[2],
            alfa=row[3], beta=row[4],
            delay_seg_media=row[5], duracion_seg_media=row[6],
            n_obs_temporal=row[7], fuente=row[8] or "",
            creado_ts=row[9], actualizado_ts=row[10],
        )

    def upsert(self, link: CausalLink) -> int:
        """Inserta o actualiza completo. Para observaciones incrementales usar `observar()`."""
        c, e = self._normalizar(link.causa), self._normalizar(link.efecto)
        ts = time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO causal_links "
                "(causa, efecto, alfa, beta, delay_seg_media, duracion_seg_media, "
                "n_obs_temporal, fuente, creado_ts, actualizado_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(causa, efecto) DO UPDATE SET "
                "alfa=excluded.alfa, beta=excluded.beta, "
                "delay_seg_media=excluded.delay_seg_media, "
                "duracion_seg_media=excluded.duracion_seg_media, "
                "n_obs_temporal=excluded.n_obs_temporal, "
                "fuente=COALESCE(NULLIF(excluded.fuente, ''), causal_links.fuente), "
                "actualizado_ts=excluded.actualizado_ts",
                (
                    c, e, link.alfa, link.beta,
                    link.delay_seg_media, link.duracion_seg_media,
                    link.n_obs_temporal, link.fuente, link.creado_ts or ts, ts,
                ),
            )
            self.conn.commit()
            cur.execute("SELECT id FROM causal_links WHERE causa=? AND efecto=?", (c, e))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    # ─── Observación / actualización bayesiana ──────────────────────

    def observar(
        self,
        causa: str,
        efecto: str,
        ocurrio: bool,
        delay_seg: Optional[float] = None,
        duracion_seg: Optional[float] = None,
        fuente: str = "auto",
    ) -> CausalLink:
        """Actualiza la creencia P(efecto | causa) con una observación.

        Posterior Beta(α+1, β) si `ocurrio=True`, Beta(α, β+1) si no.
        Si se aporta `delay_seg` y/o `duracion_seg`, también actualiza
        las medias móviles (welford-like simplificado).
        """
        c, e = self._normalizar(causa), self._normalizar(efecto)
        if not c or not e:
            raise ValueError("causa y efecto no pueden estar vacíos")
        link = self.obtener(c, e) or CausalLink(causa=c, efecto=e, fuente=fuente)
        if ocurrio:
            link.alfa += 1.0
        else:
            link.beta += 1.0
        if ocurrio and delay_seg is not None:
            link.n_obs_temporal += 1
            n = link.n_obs_temporal
            link.delay_seg_media = (
                link.delay_seg_media + (float(delay_seg) - link.delay_seg_media) / n
            )
        if ocurrio and duracion_seg is not None:
            # Reusa el mismo contador de muestras temporales — simplificación
            # consciente: si la duración llega sin delay, no inflamos el contador
            # otra vez. Compromiso aceptable mientras no haya >100 observaciones.
            denom = max(link.n_obs_temporal, 1)
            link.duracion_seg_media = (
                link.duracion_seg_media
                + (float(duracion_seg) - link.duracion_seg_media) / denom
            )
        # Solo sobrescribir fuente si era vacía (preservar "seed" frente a "auto")
        if not link.fuente:
            link.fuente = fuente
        link.actualizado_ts = time.time()
        self.upsert(link)
        # Re-obtenemos para llevar el ID asignado por SQLite
        return self.obtener(c, e) or link

    def borrar(self, causa: str, efecto: str) -> bool:
        c, e = self._normalizar(causa), self._normalizar(efecto)
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM causal_links WHERE causa=? AND efecto=?", (c, e))
            self.conn.commit()
            return cur.rowcount > 0

    def efectos_de(
        self, causa: str, min_prob: float = 0.0, min_confianza: float = 0.0,
    ) -> List[CausalLink]:
        """Devuelve enlaces salientes de `causa` filtrados por probabilidad/confianza."""
        out = self.listar(causa=causa, min_confianza=min_confianza)
        return [l for l in out if l.probabilidad >= min_prob]

    # ─── Seed dataset ───────────────────────────────────────────────

    SEED = [
        # (causa, efecto, prob_inicial, delay_seg, duracion_seg)
        ("tomar_cafe",         "alerta_aumenta",      0.85, 30 * 60,  120 * 60),
        ("tomar_cafe",         "insomnio",            0.30, 6 * 3600, 4 * 3600),
        ("beber_alcohol",      "sueño_aumenta",       0.70, 60 * 60,  3 * 3600),
        ("beber_alcohol",      "deshidratacion",      0.60, 3 * 3600, 8 * 3600),
        ("hacer_ejercicio",    "cansancio_fisico",    0.75, 30 * 60,  2 * 3600),
        ("hacer_ejercicio",    "endorfinas_suben",    0.80, 15 * 60,  90 * 60),
        ("comer_pesado",       "somnolencia",         0.65, 30 * 60,  90 * 60),
        ("pantalla_noche",     "dificultad_dormir",   0.55, 60 * 60,  3 * 3600),
        ("dormir_mal",         "cansancio_diurno",    0.75, 4 * 3600, 12 * 3600),
        ("meditar",            "calma",               0.70, 10 * 60,  60 * 60),
    ]

    def cargar_seed(self, sobrescribir: bool = False) -> int:
        """Carga dataset semilla. Si `sobrescribir=False`, salta enlaces existentes.

        Devuelve número de enlaces creados (no actualizados).
        """
        n = 0
        for causa, efecto, prob, delay, duracion in self.SEED:
            existente = self.obtener(causa, efecto)
            if existente and not sobrescribir:
                continue
            # Codifica la probabilidad inicial como α+β = 5 (peso bajo, fácil
            # de override con observaciones reales)
            peso = 5.0
            alfa = PRIOR_ALFA + peso * prob
            beta = PRIOR_BETA + peso * (1 - prob)
            link = CausalLink(
                causa=causa, efecto=efecto,
                alfa=alfa, beta=beta,
                delay_seg_media=float(delay),
                duracion_seg_media=float(duracion),
                n_obs_temporal=1,
                fuente="seed",
            )
            self.upsert(link)
            n += 1
        return n

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── CausalSimulator ──────────────────────────────────────────────────

class CausalSimulator:
    """Simulación Monte Carlo sobre `CausalGraph`.

    Dado un evento causa, ejecuta N simulaciones; en cada una, recorre el grafo
    encadenando efectos según sus probabilidades. Agrega resultados como
    distribución de eventos futuros con probabilidad y tiempo esperado.

    Args:
        graph: CausalGraph subyacente.
        max_profundidad: cuántos saltos causales encadenar. Default 3.
        n_simulaciones: por defecto en simular_evento. Default 200.
        semilla: opcional, para tests deterministas.
    """

    def __init__(
        self, graph: CausalGraph,
        max_profundidad: int = 3,
        n_simulaciones: int = 200,
        semilla: Optional[int] = None,
    ):
        self.graph = graph
        self.max_profundidad = max(1, int(max_profundidad))
        self.n_simulaciones = max(10, int(n_simulaciones))
        self._rng = random.Random(semilla)

    def simular_evento(
        self,
        causa: str,
        horizonte_seg: Optional[float] = None,
        n_simulaciones: Optional[int] = None,
        max_profundidad: Optional[int] = None,
    ) -> List[EventoSimulado]:
        """Devuelve la distribución de efectos futuros.

        Cada evento agregado: probabilidad estimada (frecuencia en simulaciones),
        tiempo medio de aparición, duración media, profundidad típica.
        """
        n_sim = n_simulaciones or self.n_simulaciones
        prof_max = max_profundidad or self.max_profundidad
        horizonte = float(horizonte_seg) if horizonte_seg else float("inf")
        # Acumuladores por evento
        acumulado: Dict[str, Dict[str, Any]] = {}

        for _ in range(n_sim):
            stack: List[Tuple[str, float, int, List[str]]] = [(causa, 0.0, 0, [causa])]
            visitados_sim: set = set()
            while stack:
                ev_actual, t_actual, prof, cadena = stack.pop()
                if prof >= prof_max:
                    continue
                if ev_actual in visitados_sim:
                    continue
                visitados_sim.add(ev_actual)
                enlaces = self.graph.efectos_de(ev_actual)
                for link in enlaces:
                    # Sample bernoulli con P estimada
                    if self._rng.random() >= link.probabilidad:
                        continue
                    t_efecto = t_actual + (link.delay_seg_media or 0.0)
                    if t_efecto > horizonte:
                        continue
                    acc = acumulado.setdefault(link.efecto, {
                        "n": 0, "t_total": 0.0, "dur_total": 0.0,
                        "prof_total": 0, "cadenas": [],
                    })
                    acc["n"] += 1
                    acc["t_total"] += t_efecto
                    acc["dur_total"] += link.duracion_seg_media
                    acc["prof_total"] += prof + 1
                    # Solo guardamos hasta 3 cadenas como muestra
                    if len(acc["cadenas"]) < 3:
                        acc["cadenas"].append(cadena + [link.efecto])
                    stack.append((link.efecto, t_efecto, prof + 1, cadena + [link.efecto]))

        out: List[EventoSimulado] = []
        for evento, acc in acumulado.items():
            n = acc["n"]
            out.append(EventoSimulado(
                evento=evento,
                probabilidad=n / n_sim,
                tiempo_desde_inicio_seg=acc["t_total"] / n,
                duracion_seg=acc["dur_total"] / n,
                profundidad=int(round(acc["prof_total"] / n)),
                cadena=acc["cadenas"][0] if acc["cadenas"] else [],
            ))
        out.sort(key=lambda e: (-e.probabilidad, e.tiempo_desde_inicio_seg))
        return out

    def que_pasa_si(
        self,
        causa: str,
        horizonte_seg: Optional[float] = None,
        umbral_prob: float = 0.2,
    ) -> str:
        """Genera una respuesta natural en español sobre los efectos probables.

        Solo lista efectos con probabilidad ≥ umbral. Si no hay nada, devuelve
        una frase honesta diciendo que no tiene información.
        """
        efectos = self.simular_evento(causa, horizonte_seg=horizonte_seg)
        relevantes = [e for e in efectos if e.probabilidad >= umbral_prob]
        if not relevantes:
            return (
                f"No tengo información causal suficiente sobre «{causa}» "
                f"para predecir consecuencias con confianza."
            )
        partes = [f"Si {causa.replace('_', ' ')}, lo más probable es:"]
        for ev in relevantes[:5]:
            cuando = _formatear_tiempo(ev.tiempo_desde_inicio_seg)
            cuanto = _formatear_tiempo(ev.duracion_seg)
            partes.append(
                f"  • {ev.evento.replace('_', ' ')} "
                f"({int(ev.probabilidad * 100)}%) "
                f"en ~{cuando}, dura ~{cuanto}"
            )
        return "\n".join(partes)


def _formatear_tiempo(seg: float) -> str:
    seg = max(0, int(seg))
    if seg < 60:
        return f"{seg}s"
    if seg < 3600:
        return f"{seg // 60} min"
    h = seg / 3600.0
    if h < 24:
        return f"{h:.1f}h" if h < 10 else f"{int(h)}h"
    d = h / 24.0
    return f"{d:.1f}d"


# ─── MarkovPredictor ──────────────────────────────────────────────────

class MarkovPredictor:
    """Predice el siguiente acto del usuario con frecuencias por contexto.

    Contexto = (bucket_hora, dia_semana, accion_previa, contexto_extra).
    Se guarda cada acción registrada. La predicción cuenta frecuencias en
    el contexto solicitado y devuelve top-K por probabilidad (laplace
    smoothing α=0.5 para evitar ceros).

    Diseño honesto:
    - Sin RNN, sin embeddings. Es una tabla de contingencia. Robusto y
      explicable. Si en algún momento hay millones de acciones, refactorizar.
    - Las acciones son strings opacos (`tomar_cafe`, `salir_correr`,
      `revisar_email`). El usuario o el orchestrator deciden el vocabulario.
    """

    LAPLACE_ALFA = 0.5

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ─── Registro ────────────────────────────────────────────────────

    def registrar_accion(
        self,
        accion: str,
        ts: Optional[float] = None,
        contexto: str = "",
        accion_previa: str = "",
    ) -> int:
        accion = (accion or "").strip().lower()
        if not accion:
            raise ValueError("accion no puede estar vacía")
        ts = float(ts if ts is not None else time.time())
        dt = datetime.fromtimestamp(ts)
        bucket = bucket_hora(dt.hour)
        dia = _DIAS_ES[dt.weekday()]
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO user_acciones "
                "(accion, bucket_hora, dia_semana, contexto, accion_previa, ts) "
                "VALUES (?,?,?,?,?,?)",
                (accion, bucket, dia,
                 (contexto or "").strip().lower(),
                 (accion_previa or "").strip().lower(), ts),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def ultima_accion(self) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, accion, bucket_hora, dia_semana, contexto, "
            "accion_previa, ts FROM user_acciones ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "accion": row[1], "bucket_hora": row[2],
            "dia_semana": row[3], "contexto": row[4],
            "accion_previa": row[5], "ts": row[6],
        }

    # ─── Predicción ──────────────────────────────────────────────────

    def predecir_siguiente(
        self,
        bucket: Optional[str] = None,
        dia_semana: Optional[str] = None,
        accion_previa: str = "",
        top_k: int = 3,
        modo_contexto: str = "estricto",
    ) -> List[Dict[str, Any]]:
        """Top-K acciones más probables en el contexto dado.

        Si bucket/dia no se especifican, usa el ahora.
        `modo_contexto`:
          - "estricto": exige match exacto de bucket+dia+accion_previa.
          - "relajado": si estricto no devuelve nada, prueba sin accion_previa,
            luego sin dia_semana, luego solo bucket.
          - "global": cuenta sobre todas las acciones (baseline).
        """
        if bucket is None or dia_semana is None:
            now = datetime.now()
            bucket = bucket or bucket_hora(now.hour)
            dia_semana = dia_semana or _DIAS_ES[now.weekday()]
        bucket = (bucket or "").strip().lower()
        dia_semana = (dia_semana or "").strip().lower()
        previa = (accion_previa or "").strip().lower()

        if modo_contexto == "global":
            cuentas = self._contar(None, None, "")
        elif modo_contexto == "estricto":
            cuentas = self._contar(bucket, dia_semana, previa)
        else:  # relajado
            cuentas = self._contar(bucket, dia_semana, previa)
            if not cuentas:
                cuentas = self._contar(bucket, dia_semana, "")
            if not cuentas:
                cuentas = self._contar(bucket, None, "")
            if not cuentas:
                cuentas = self._contar(None, None, "")
        if not cuentas:
            return []
        total = sum(cuentas.values())
        # Laplace smoothing
        k = len(cuentas)
        denom = total + self.LAPLACE_ALFA * k
        rank = sorted(
            (
                {
                    "accion": acc,
                    "probabilidad": round((n + self.LAPLACE_ALFA) / denom, 4),
                    "n": n,
                }
                for acc, n in cuentas.items()
            ),
            key=lambda d: -d["probabilidad"],
        )
        return rank[: int(top_k)]

    def _contar(
        self, bucket: Optional[str], dia: Optional[str], previa: str,
    ) -> Dict[str, int]:
        wheres: List[str] = []
        params: List[Any] = []
        if bucket:
            wheres.append("bucket_hora=?")
            params.append(bucket)
        if dia:
            wheres.append("dia_semana=?")
            params.append(dia)
        if previa:
            wheres.append("accion_previa=?")
            params.append(previa)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT accion, COUNT(*) FROM user_acciones {where_sql} "
            f"GROUP BY accion",
            params,
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}

    def stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_acciones")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT accion) FROM user_acciones")
        distintas = cur.fetchone()[0]
        cur.execute(
            "SELECT accion, COUNT(*) AS n FROM user_acciones "
            "GROUP BY accion ORDER BY n DESC LIMIT 5"
        )
        top = [{"accion": r[0], "n": int(r[1])} for r in cur.fetchall()]
        return {"acciones_total": total, "acciones_distintas": distintas, "top": top}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
