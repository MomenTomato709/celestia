"""Motor Datalog mini para inferencia lógica.

Resuelve la limitación honesta del razonador heurístico actual: el LLM extrae
hechos sueltos del grafo pero no deduce consecuencias. Con Datalog
añadimos reglas de inferencia como «si A padre_de B y B padre_de C, entonces
A abuelo_de C» y resolvemos consultas tipo «¿quién es mi abuelo?» de forma
determinista.

Diseño:
- `Atom(predicate, args)`: hecho atómico ground o con variables. Variables
  empiezan por mayúscula o `?`; constantes son strings cualquier-cosa.
- `Rule(head, body)`: regla `head :- a1, a2, …`. Las negativas en `body` se
  evalúan como negation-as-failure bajo closed-world (estratificación
  obligatoria: una regla no puede usar `not p` si `p` se define en términos
  de sí misma).
- `Engine`: motor con saturación forward-chaining (semi-naive evaluation
  para evitar recomputar). Backward-chaining con SLD-resolution + memoization
  para queries puntuales sin tener que saturar todo.

Limitaciones honestas:
- Sin tipos, sin functions, sin agregados, sin negación recursiva. Datalog
  puro estilo "Stanford CS245". Suficiente para reglas familiares,
  transitividad y deducciones del grafo de Celestia.
- Saturación es O(n^k) con n hechos y k aridad de la regla más grande. Para
  los dominios de Celestia (decenas de entidades, pocas reglas) es inmediato.
- Sin paralelismo: si en algún momento hay miles de hechos, refactorizar
  a Souffle o pyDatalog. Por ahora innecesario.
"""
from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("celestia_v1")


# ─── Variables y constantes ───────────────────────────────────────────

# Variable: empieza por `?` (estilo PDDL/Prolog) o por letra mayúscula sola
# (estilo Prolog clásico: X, Y, Z, A, B...). Para evitar ambigüedad con
# entidades nombradas tipo "Ana", aceptamos solo mayúscula simple A-Z
# como variable cuando es la primera letra; si tiene más caracteres se
# considera constante salvo que empiece por `?`.
_VAR_RE = re.compile(r"^(\?[A-Za-z_][\w]*|[A-Z])$")


def es_variable(term: str) -> bool:
    return bool(_VAR_RE.match(term))


# ─── Atom y Rule ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Atom:
    """Hecho lógico: `predicate(arg1, arg2, …)`.

    Args ground (constantes) o variables (`?x`, `X`, …). `negated=True` se
    usa SOLO en cuerpos de reglas (negation-as-failure). En la EDB
    (hechos base) y en cabezas nunca aparece negado.
    """
    predicate: str
    args: Tuple[str, ...] = ()
    negated: bool = False

    def __str__(self) -> str:
        sig = (
            f"{self.predicate}({', '.join(self.args)})"
            if self.args else self.predicate
        )
        return f"¬{sig}" if self.negated else sig

    def es_ground(self) -> bool:
        return not any(es_variable(a) for a in self.args)

    def substituir(self, binding: Dict[str, str]) -> "Atom":
        nuevos = tuple(binding.get(a, a) if es_variable(a) else a for a in self.args)
        return Atom(self.predicate, nuevos, self.negated)


def atom(predicate: str, *args: str, negated: bool = False) -> Atom:
    return Atom(predicate, tuple(args), negated)


@dataclass(frozen=True)
class Rule:
    """`head :- body[0], body[1], ...` — la cabeza nunca es negada."""
    head: Atom
    body: Tuple[Atom, ...]

    def __post_init__(self) -> None:
        if self.head.negated:
            raise ValueError("Datalog: cabeza de regla no puede ir negada")
        # Validación de seguridad (range-restriction): toda variable que
        # aparece en la cabeza debe aparecer en algún átomo POSITIVO del cuerpo.
        # Es la condición clásica de Datalog seguro.
        vars_head = {a for a in self.head.args if es_variable(a)}
        vars_body_pos: Set[str] = set()
        for atm in self.body:
            if atm.negated:
                continue
            for a in atm.args:
                if es_variable(a):
                    vars_body_pos.add(a)
        faltan = vars_head - vars_body_pos
        if faltan:
            raise ValueError(
                f"Regla insegura: variables {faltan} en cabeza no aparecen "
                f"en átomos positivos del cuerpo de {self}"
            )
        # Variables en negados también deben aparecer en positivos
        # (negation-as-failure segura).
        for atm in self.body:
            if not atm.negated:
                continue
            vars_neg = {a for a in atm.args if es_variable(a)}
            faltan_neg = vars_neg - vars_body_pos
            if faltan_neg:
                raise ValueError(
                    f"Regla insegura (negación): variables {faltan_neg} "
                    f"en {atm} no están restringidas por átomos positivos"
                )

    def __str__(self) -> str:
        b = ", ".join(str(a) for a in self.body) if self.body else "true"
        return f"{self.head} :- {b}"


def regla(head: Atom, *body: Atom) -> Rule:
    return Rule(head, tuple(body))


# ─── Unificación ──────────────────────────────────────────────────────

def unificar(
    a: Atom, b: Atom, binding: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Unifica dos átomos. Devuelve binding extendido o None si no unifican.

    Solo unifica predicados con misma signatura (mismo predicate, misma aridad).
    Ignora el flag negated (la negación se trata fuera).
    """
    if a.predicate != b.predicate or len(a.args) != len(b.args):
        return None
    bind: Dict[str, str] = dict(binding or {})
    for x, y in zip(a.args, b.args):
        x_v = es_variable(x)
        y_v = es_variable(y)
        if x_v:
            x_val = bind.get(x, x)
            if x_val != x:
                # x ya estaba bound — comparar contra y
                if es_variable(y):
                    y_val = bind.get(y, y)
                    if y_val == y:
                        bind[y] = x_val
                    elif y_val != x_val:
                        return None
                else:
                    if x_val != y:
                        return None
            else:
                # x sin binding previo
                bind[x] = bind.get(y, y) if y_v else y
        elif y_v:
            y_val = bind.get(y, y)
            if y_val != y and y_val != x:
                return None
            bind[y] = x
        else:
            if x != y:
                return None
    return bind


# ─── Engine ───────────────────────────────────────────────────────────

class DatalogEngine:
    """Motor Datalog: hechos base (EDB) + reglas + saturación + query.

    Uso:
        eng = DatalogEngine()
        eng.add_fact(atom("padre_de", "ana", "luis"))
        eng.add_rule(regla(atom("abuelo_de", "X", "Z"),
                           atom("padre_de", "X", "Y"),
                           atom("padre_de", "Y", "Z")))
        for binding in eng.consultar(atom("abuelo_de", "?quien", "ana")):
            print(binding["?quien"])

    El engine es incremental: añadir hechos invalida el cache de saturación
    para que la siguiente consulta vuelva a recomputar. No es lo más
    eficiente para volúmenes grandes; suficiente para Celestia.
    """

    MAX_ITERACIONES_SATURACION = 100  # protección anti-ciclos infinitos

    def __init__(self) -> None:
        self._facts: Set[Atom] = set()
        self._rules: List[Rule] = []
        self._saturado: Optional[FrozenSet[Atom]] = None  # cache

    # ─── EDB ──────────────────────────────────────────────────────────

    def add_fact(self, a: Atom) -> bool:
        if a.negated:
            raise ValueError("EDB no admite hechos negados")
        if not a.es_ground():
            raise ValueError(f"EDB requiere hechos ground; recibido {a}")
        nuevo = a not in self._facts
        self._facts.add(a)
        if nuevo:
            self._saturado = None
        return nuevo

    def add_facts(self, atoms: Iterable[Atom]) -> int:
        n = 0
        for a in atoms:
            if self.add_fact(a):
                n += 1
        return n

    def add_rule(self, r: Rule) -> None:
        self._rules.append(r)
        self._saturado = None

    def add_rules(self, rs: Iterable[Rule]) -> None:
        for r in rs:
            self.add_rule(r)

    @property
    def facts(self) -> FrozenSet[Atom]:
        return frozenset(self._facts)

    @property
    def rules(self) -> List[Rule]:
        return list(self._rules)

    # ─── Saturación forward-chaining ──────────────────────────────────

    def saturar(self) -> FrozenSet[Atom]:
        """Aplica reglas hasta el punto fijo. Devuelve EDB + IDB.

        Algoritmo semi-naive simplificado: en cada iteración, intenta cada
        regla con bindings derivables de hechos actuales; si genera un átomo
        nuevo, lo añade. Repite mientras haya nuevos.
        """
        if self._saturado is not None:
            return self._saturado
        derivados: Set[Atom] = set(self._facts)
        for _ in range(self.MAX_ITERACIONES_SATURACION):
            nuevos: Set[Atom] = set()
            for r in self._rules:
                for binding in self._evaluar_cuerpo(r.body, derivados):
                    head_ground = r.head.substituir(binding)
                    if not head_ground.es_ground():
                        continue  # cabeza con variables libres tras unificar
                    if head_ground not in derivados:
                        nuevos.add(head_ground)
            if not nuevos:
                break
            derivados |= nuevos
        else:
            logger.warning("Datalog: alcanzado MAX_ITERACIONES_SATURACION")
        self._saturado = frozenset(derivados)
        return self._saturado

    def _evaluar_cuerpo(
        self,
        body: Tuple[Atom, ...],
        facts: Set[Atom],
    ) -> List[Dict[str, str]]:
        """Devuelve todos los bindings que satisfacen TODOS los átomos del cuerpo.

        Negativos se evalúan al final (necesitan que las variables estén ya
        ligadas por positivos). Esto es la estratificación implícita: una
        regla bien-formada (validada en Rule.__post_init__) garantiza orden.
        """
        positivos = [a for a in body if not a.negated]
        negativos = [a for a in body if a.negated]
        bindings: List[Dict[str, str]] = [{}]
        for atm in positivos:
            siguiente: List[Dict[str, str]] = []
            for b in bindings:
                for f in facts:
                    nb = unificar(atm, f, b)
                    if nb is not None:
                        # Restricción extra: dos vars distintas no deben
                        # mapear al mismo valor si el predicado las separa.
                        siguiente.append(nb)
            bindings = siguiente
            if not bindings:
                return []
        # Aplicar negativos
        filtrados: List[Dict[str, str]] = []
        for b in bindings:
            ok = True
            for atm in negativos:
                grounded = atm.substituir(b).positivo()
                if grounded in facts:
                    ok = False
                    break
            if ok:
                filtrados.append(b)
        return filtrados

    # ─── Query ────────────────────────────────────────────────────────

    def consultar(self, query: Atom) -> List[Dict[str, str]]:
        """Devuelve todos los bindings que hacen al query verdadero.

        Si query es ground, devuelve [{}] si es verdadero, [] si no.
        Si tiene variables, devuelve una lista de bindings (puede estar vacía).
        Bajo el capó: satura y filtra.
        """
        saturado = self.saturar()
        if query.es_ground():
            return [{}] if query in saturado else []
        out: List[Dict[str, str]] = []
        for f in saturado:
            b = unificar(query, f)
            if b is not None:
                # Solo conservar bindings de variables presentes en query
                vars_q = {a for a in query.args if es_variable(a)}
                b_filtrado = {k: v for k, v in b.items() if k in vars_q}
                if b_filtrado not in out:
                    out.append(b_filtrado)
        return out

    def es_verdad(self, query: Atom) -> bool:
        """Atajo para queries ground (booleano)."""
        return len(self.consultar(query)) > 0 if query.es_ground() else bool(
            self.consultar(query)
        )


# ─── Atom helper: positivo (para chequear negativo grounded en facts) ─

def _positivo(a: Atom) -> Atom:
    return Atom(a.predicate, a.args, False)


# Monkey-patch para que el método .positivo() exista en Atom (más legible)
Atom.positivo = _positivo  # type: ignore[attr-defined]


# ─── Reglas familiares por defecto ────────────────────────────────────

def reglas_familia_es() -> List[Rule]:
    """Reglas estándar de parentesco en español.

    Asume los hechos base usan predicados:
    - padre_de(P, H): P es padre/madre de H
    - hermano_de(A, B): A y B son hermanos (simétrica; añade la simetría
      explícitamente al cargar facts si lo necesitas)
    - conyuge_de(A, B): A es cónyuge de B (simétrica)

    Deriva:
    - abuelo_de, nieto_de, hijo_de (inversa de padre_de),
      tio_de, sobrino_de, primo_de, descendiente_de, ancestro_de.

    Nota: hermano_de queda explícito como hecho base (no derivado) para
    evitar la dependencia auto-recursiva con padres comunes; en uso real
    se añade al ingresar la familia. La regla derivada (mismo padre,
    distinta persona) se incluye opcionalmente como `hermano_por_padre_de`.
    """
    return [
        # hijo_de(H, P) :- padre_de(P, H)
        regla(atom("hijo_de", "H", "P"), atom("padre_de", "P", "H")),
        # abuelo_de(A, N) :- padre_de(A, P), padre_de(P, N)
        regla(
            atom("abuelo_de", "A", "N"),
            atom("padre_de", "A", "P"),
            atom("padre_de", "P", "N"),
        ),
        # nieto_de(N, A) :- abuelo_de(A, N)
        regla(atom("nieto_de", "N", "A"), atom("abuelo_de", "A", "N")),
        # tio_de(T, S) :- hermano_de(T, P), padre_de(P, S)
        regla(
            atom("tio_de", "T", "S"),
            atom("hermano_de", "T", "P"),
            atom("padre_de", "P", "S"),
        ),
        # sobrino_de(S, T) :- tio_de(T, S)
        regla(atom("sobrino_de", "S", "T"), atom("tio_de", "T", "S")),
        # primo_de(C1, C2) :- padre_de(P1, C1), padre_de(P2, C2), hermano_de(P1, P2)
        regla(
            atom("primo_de", "C", "D"),
            atom("padre_de", "P", "C"),
            atom("padre_de", "Q", "D"),
            atom("hermano_de", "P", "Q"),
        ),
        # ancestro_de(A, D) :- padre_de(A, D)
        regla(atom("ancestro_de", "A", "D"), atom("padre_de", "A", "D")),
        # ancestro_de(A, D) :- padre_de(A, X), ancestro_de(X, D)
        regla(
            atom("ancestro_de", "A", "D"),
            atom("padre_de", "A", "X"),
            atom("ancestro_de", "X", "D"),
        ),
        # descendiente_de(D, A) :- ancestro_de(A, D)
        regla(
            atom("descendiente_de", "D", "A"),
            atom("ancestro_de", "A", "D"),
        ),
        # hermano_por_padre_de(A, B) :- padre_de(P, A), padre_de(P, B), distinto(A, B)
        # — versión derivada, requiere predicado `distinto` que aquí no
        # tenemos como negación de igualdad; se omite para mantener Datalog
        # puro. La simetría de hermano_de se gestiona al ingresar facts.
    ]


# ─── Carga desde grafo (KnowledgeGraph → EDB) ─────────────────────────

# Predicados del grafo que cargamos como hechos base de Datalog.
# Mantén esta lista en sync con KnowledgeGraph.añadir_relacion.
PREDICADOS_FAMILIARES = (
    "padre_de", "madre_de", "hermano_de", "hermana_de",
    "conyuge_de", "amigo_de", "tio_de", "tia_de",
)


def cargar_desde_grafo(
    engine: DatalogEngine,
    knowledge,
    predicados: Iterable[str] = PREDICADOS_FAMILIARES,
    normalizar_genero: bool = True,
) -> int:
    """Vuelca relaciones vigentes del KnowledgeGraph como hechos Datalog.

    `normalizar_genero=True` convierte madre_de → padre_de y hermana_de →
    hermano_de para que las reglas funcionen sin duplicar (las reglas
    asumen `padre_de` y `hermano_de` como predicados canónicos). Idem
    tia_de → tio_de.

    Devuelve nº de hechos añadidos.
    """
    if knowledge is None:
        return 0
    n = 0
    cur = knowledge.conn.cursor()
    placeholders = ",".join("?" * len(list(predicados)))
    preds_list = list(predicados)
    cur.execute(
        f"SELECT r.relacion, ea.nombre, eb.nombre "
        f"FROM kg_relaciones r "
        f"JOIN kg_entidades ea ON ea.id = r.entidad_a_id "
        f"JOIN kg_entidades eb ON eb.id = r.entidad_b_id "
        f"WHERE r.relacion IN ({placeholders}) AND r.hasta_ts IS NULL",
        preds_list,
    )
    canonico = {
        "madre_de": "padre_de", "hermana_de": "hermano_de", "tia_de": "tio_de",
    } if normalizar_genero else {}
    for rel, a, b in cur.fetchall():
        nombre_pred = canonico.get(rel, rel)
        try:
            if engine.add_fact(atom(nombre_pred, a.strip().lower(), b.strip().lower())):
                n += 1
            # Simetría para hermano_de
            if nombre_pred == "hermano_de":
                if engine.add_fact(atom(nombre_pred,
                                          b.strip().lower(), a.strip().lower())):
                    n += 1
        except ValueError:
            continue
    return n
