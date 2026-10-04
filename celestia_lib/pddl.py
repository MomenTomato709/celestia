"""Planificador clásico tipo PDDL light.

Implementación honesta de planificación simbólica en estilo STRIPS:
estado = conjunto de literales positivos (closed-world assumption),
acciones con preconditions/effects formales, búsqueda BFS/A* sobre el
espacio de estados con grounding por instanciación de parámetros.

Diseñado para integrarse con `MultiObjectivePlanner.planificar_pddl(...)`:
el LLM traduce petición natural a un `PDDLProblem`, este módulo encuentra
plan óptimo determinista, y `PlanExecutor` lo ejecuta vía `AgentTools`.

Filosofía: el LLM hace lo que hace bien (entender lenguaje natural y
extraer estado/objetivo), el solver hace lo que hace bien (búsqueda
completa y óptima sobre un grafo discreto). Se evita pedirle al LLM
que "razone secuencias", lo que es justamente donde más alucina.

Limitaciones honestas:
- Sin tipos, sin durative actions, sin numeric fluents (PDDL 1.0 puro).
- Heurística simple: literales del goal aún no satisfechos. No es admisible
  en el sentido estricto (puede subestimar), pero práctica para dominios
  pequeños. Para dominios grandes habría que añadir FastDownward.
- Grounding completo: con N objetos y M parámetros por acción, hay N^M
  grounded actions. OK para dominios de la asistente personal (<50 objetos),
  no para benchmarks IPC.
"""
from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("celestia_v1")


# ─── Literales ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Literal:
    """Hecho atómico: `predicate(arg1, arg2, …)`.

    `negated=True` marca un literal usado en effects para borrar un hecho
    del estado. En preconditions también puede aparecer negado (negación
    como fallo bajo closed-world).
    """
    predicate: str
    args: Tuple[str, ...] = ()
    negated: bool = False

    def __str__(self) -> str:
        sig = f"{self.predicate}({', '.join(self.args)})" if self.args else self.predicate
        return f"¬{sig}" if self.negated else sig

    def positivo(self) -> "Literal":
        return Literal(self.predicate, self.args, False)

    def negativo(self) -> "Literal":
        return Literal(self.predicate, self.args, True)

    def substituir(self, binding: Dict[str, str]) -> "Literal":
        """Aplica un binding `?var → constante` a los argumentos."""
        nuevos = tuple(binding.get(a, a) for a in self.args)
        return Literal(self.predicate, nuevos, self.negated)

    def es_ground(self) -> bool:
        return not any(a.startswith("?") for a in self.args)


def lit(predicate: str, *args: str, negated: bool = False) -> Literal:
    """Helper constructor compacto."""
    return Literal(predicate, tuple(args), negated)


# ─── Acciones y dominio ───────────────────────────────────────────────

@dataclass(frozen=True)
class Action:
    """Esquema de acción no instanciada.

    `parameters` son variables `?x` que aparecen en pre/effects. El planner
    genera grounded actions probando todas las asignaciones posibles desde
    `objects` del problema.

    `cost` permite preferir planes de menor coste en A* (default 1).
    """
    name: str
    parameters: Tuple[str, ...]
    preconditions: FrozenSet[Literal]
    effects: FrozenSet[Literal]
    cost: float = 1.0

    def __str__(self) -> str:
        return f"{self.name}({', '.join(self.parameters)})"


@dataclass(frozen=True)
class GroundedAction:
    """Acción con parámetros ya sustituidos por constantes."""
    name: str
    args: Tuple[str, ...]
    preconditions: FrozenSet[Literal]
    effects: FrozenSet[Literal]
    cost: float

    def __str__(self) -> str:
        return f"{self.name}({', '.join(self.args)})" if self.args else self.name


# ─── Estados ──────────────────────────────────────────────────────────

State = FrozenSet[Literal]


def state_satisface(state: State, preconditions: Iterable[Literal]) -> bool:
    """¿El estado satisface todas las preconditions?

    Bajo closed-world: literal positivo se exige en state; literal negativo
    se exige ausente.
    """
    for p in preconditions:
        if p.negated:
            if p.positivo() in state:
                return False
        else:
            if p not in state:
                return False
    return True


def aplicar_efectos(state: State, effects: Iterable[Literal]) -> State:
    """Aplica delete-list primero (effects negativos), luego add-list."""
    s = set(state)
    for e in effects:
        if e.negated:
            s.discard(e.positivo())
    for e in effects:
        if not e.negated:
            s.add(e)
    return frozenset(s)


# ─── Problema ─────────────────────────────────────────────────────────

@dataclass
class PDDLProblem:
    """Problema de planificación: dominio + estado inicial + goal + objetos."""
    actions: Tuple[Action, ...]
    initial: State
    goal: FrozenSet[Literal]
    objects: Tuple[str, ...]

    def grounded_actions(self) -> List[GroundedAction]:
        """Enumera todas las acciones ground viables.

        Filtra ya en grounding las acciones cuya precondición positiva
        contradice closed-world trivialmente (acelera búsqueda).
        """
        out: List[GroundedAction] = []
        objs = self.objects or ()
        for action in self.actions:
            if not action.parameters:
                out.append(GroundedAction(
                    name=action.name, args=(),
                    preconditions=action.preconditions,
                    effects=action.effects,
                    cost=action.cost,
                ))
                continue
            for combo in itertools.product(objs, repeat=len(action.parameters)):
                binding = dict(zip(action.parameters, combo))
                pre = frozenset(p.substituir(binding) for p in action.preconditions)
                eff = frozenset(e.substituir(binding) for e in action.effects)
                out.append(GroundedAction(
                    name=action.name, args=combo,
                    preconditions=pre, effects=eff, cost=action.cost,
                ))
        return out


# ─── Búsqueda ─────────────────────────────────────────────────────────

class PlanNotFound(Exception):
    """No existe plan que satisfaga el goal dentro del límite."""


def heuristica_goal_count(state: State, goal: Iterable[Literal]) -> float:
    """Cantidad de literales del goal aún no satisfechos.

    No admisible en el sentido formal (acciones pueden lograr varios
    literales a la vez, así que sobreestima a veces). Es la heurística
    `h_add` simplificada — práctica y rápida.
    """
    pendientes = 0
    for g in goal:
        if g.negated:
            if g.positivo() in state:
                pendientes += 1
        else:
            if g not in state:
                pendientes += 1
    return float(pendientes)


def a_star(
    problem: PDDLProblem,
    max_nodes: int = 10000,
    max_depth: int = 50,
    heuristica: Callable[[State, Iterable[Literal]], float] = heuristica_goal_count,
) -> List[GroundedAction]:
    """A* sobre estados. Devuelve plan óptimo (mínimo coste) o lanza PlanNotFound.

    Args:
        max_nodes: límite de expansiones (defensivo contra dominios explosivos).
        max_depth: profundidad máxima de plan considerada.
        heuristica: f(state, goal) → estimación de coste restante.
    """
    grounded = problem.grounded_actions()
    if not grounded and not state_satisface(problem.initial, problem.goal):
        raise PlanNotFound("sin acciones y goal no satisfecho")

    # Caso degenerado: goal ya satisfecho
    if state_satisface(problem.initial, problem.goal):
        return []

    # Frontera: (f, contador_desempate, g, state, plan)
    contador = itertools.count()
    inicial_h = heuristica(problem.initial, problem.goal)
    frontera: List[Tuple[float, int, float, State, Tuple[GroundedAction, ...]]] = []
    heapq.heappush(frontera, (inicial_h, next(contador), 0.0, problem.initial, ()))
    visitados: Dict[State, float] = {problem.initial: 0.0}
    nodos = 0

    while frontera and nodos < max_nodes:
        _, _, g, state, plan = heapq.heappop(frontera)
        nodos += 1
        if state_satisface(state, problem.goal):
            logger.debug("PDDL: plan encontrado en %d nodos, longitud %d", nodos, len(plan))
            return list(plan)
        if len(plan) >= max_depth:
            continue
        for ga in grounded:
            if not state_satisface(state, ga.preconditions):
                continue
            nuevo = aplicar_efectos(state, ga.effects)
            if nuevo == state:  # acción no-op: evita ciclos infinitos
                continue
            nuevo_g = g + ga.cost
            if nuevo in visitados and visitados[nuevo] <= nuevo_g:
                continue
            visitados[nuevo] = nuevo_g
            h = heuristica(nuevo, problem.goal)
            f = nuevo_g + h
            heapq.heappush(
                frontera, (f, next(contador), nuevo_g, nuevo, plan + (ga,))
            )

    raise PlanNotFound(f"sin solución tras {nodos} nodos")


def bfs(
    problem: PDDLProblem,
    max_nodes: int = 10000,
    max_depth: int = 30,
) -> List[GroundedAction]:
    """BFS pura (sin heurística). Garantiza plan más corto en número de pasos.

    Más lenta que A* en general, pero útil cuando el coste no importa
    y se prefiere mínima longitud.
    """
    if state_satisface(problem.initial, problem.goal):
        return []
    grounded = problem.grounded_actions()
    from collections import deque
    frontera: "deque[Tuple[State, Tuple[GroundedAction, ...]]]" = deque()
    frontera.append((problem.initial, ()))
    visitados = {problem.initial}
    nodos = 0
    while frontera and nodos < max_nodes:
        state, plan = frontera.popleft()
        nodos += 1
        if len(plan) >= max_depth:
            continue
        for ga in grounded:
            if not state_satisface(state, ga.preconditions):
                continue
            nuevo = aplicar_efectos(state, ga.effects)
            if nuevo == state or nuevo in visitados:
                continue
            if state_satisface(nuevo, problem.goal):
                return list(plan + (ga,))
            visitados.add(nuevo)
            frontera.append((nuevo, plan + (ga,)))
    raise PlanNotFound(f"BFS sin solución tras {nodos} nodos")


# ─── Dominio por defecto para Celestia ────────────────────────────────

def dominio_celestia() -> Tuple[Action, ...]:
    """Catálogo de acciones que mapean a `AgentTools`.

    Convenciones:
    - Predicados de estado mundial: `existe(?archivo)`, `online()`,
      `conocido(?obj)`, `enviado(?archivo)`, `usuario_informado(?asunto)`,
      `documento_creado(?tema_formato)`.
    - `online()` se asume true en estado inicial salvo que se indique lo
      contrario (el traductor LLM lo añade desde `ConnectivityManager`).
    - Las acciones que requieren red llevan `online()` en preconditions.

    Para añadir nuevas acciones del dominio, ampliar esta lista. El nombre
    de la acción DEBE coincidir con un `tool` de `AgentTools.execute`.
    """
    return (
        Action(
            name="leer_archivo",
            parameters=("?ruta",),
            preconditions=frozenset({lit("existe", "?ruta")}),
            effects=frozenset({lit("conocido", "?ruta")}),
        ),
        Action(
            name="listar_archivos",
            parameters=("?ruta",),
            preconditions=frozenset(),
            effects=frozenset({lit("listado", "?ruta")}),
        ),
        Action(
            name="crear_archivo",
            parameters=("?ruta", "?contenido"),
            preconditions=frozenset(),
            effects=frozenset({lit("existe", "?ruta")}),
        ),
        Action(
            name="crear_documento",
            parameters=("?tema_formato",),
            preconditions=frozenset(),
            effects=frozenset({
                lit("documento_creado", "?tema_formato"),
                lit("existe", "?tema_formato"),
            }),
        ),
        Action(
            name="descargar_archivo",
            parameters=("?url", "?ruta"),
            preconditions=frozenset({lit("online")}),
            effects=frozenset({lit("existe", "?ruta")}),
        ),
        Action(
            name="enviar_archivo",
            parameters=("?ruta",),
            preconditions=frozenset({lit("existe", "?ruta")}),
            effects=frozenset({lit("enviado", "?ruta")}),
            cost=2.0,  # enviar es más caro que leer/listar
        ),
        Action(
            name="generar_imagen",
            parameters=("?prompt", "?ruta"),
            preconditions=frozenset({lit("online")}),
            effects=frozenset({lit("existe", "?ruta")}),
            cost=3.0,  # más caro: tarda y cuesta tokens/inferencia
        ),
        Action(
            name="buscar_web",
            parameters=("?query",),
            preconditions=frozenset({lit("online")}),
            effects=frozenset({lit("buscado", "?query")}),
            cost=1.5,
        ),
        Action(
            name="info_sistema",
            parameters=(),
            preconditions=frozenset(),
            effects=frozenset({lit("sistema_inspeccionado")}),
        ),
        Action(
            name="informar_usuario",
            parameters=("?asunto",),
            preconditions=frozenset(),
            effects=frozenset({lit("usuario_informado", "?asunto")}),
            cost=0.5,  # comunicar al usuario es barato y casi siempre útil
        ),
        Action(
            name="recordatorio",
            parameters=("?tiempo", "?mensaje"),
            preconditions=frozenset(),
            effects=frozenset({lit("recordatorio_programado", "?mensaje")}),
        ),
    )


# ─── Args de tool desde GroundedAction ────────────────────────────────

# Mapeo nombre_acción → (parametros_PDDL → kwargs_AgentTools)
# Convierte los args posicionales del plan a los kwargs que espera
# `AgentTools.execute({"tool": ..., "params": {...}})`.
_ARG_MAPPERS: Dict[str, Tuple[str, ...]] = {
    "leer_archivo":      ("ruta",),
    "listar_archivos":   ("ruta",),
    "crear_archivo":     ("ruta", "contenido"),
    "crear_documento":   ("tema",),
    "descargar_archivo": ("url", "ruta"),
    "enviar_archivo":    ("ruta",),
    "generar_imagen":    ("prompt", "ruta"),
    "buscar_web":        ("query",),
    "info_sistema":      (),
    "informar_usuario":  ("asunto",),
    "recordatorio":      ("tiempo", "mensaje"),
}


def grounded_a_intent(ga: GroundedAction) -> Dict[str, object]:
    """Convierte una acción ground a dict `{tool, params}` para AgentTools.execute.

    Devuelve None-equivalente (dict con tool y params vacíos) si la acción
    no está en `_ARG_MAPPERS` — el caller debería tratarla como paso
    informativo sin tool.
    """
    keys = _ARG_MAPPERS.get(ga.name)
    if keys is None:
        return {"tool": ga.name, "params": {}}
    if len(keys) != len(ga.args):
        # Defensivo: si el mapping no cuadra con los args, mejor pasar todo
        # bajo un solo "arg" para que execute() pueda fallar limpio.
        return {"tool": ga.name, "params": {"args_raw": list(ga.args)}}
    params = {k: v for k, v in zip(keys, ga.args)}
    return {"tool": ga.name, "params": params}
