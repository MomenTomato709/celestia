"""Abstracción cognitiva — roadmap AGI punto #5.

Resuelve la crítica "no hay verdadera abstracción cognitiva" añadiendo una
capa de meta-learning sobre patrones algorítmicos:

- `AbstractionEngine.inducir_regla()`: dados N ejemplos (input → output),
  pide al LLM una regla en formato Python ejecutable y la **verifica** sobre
  ejemplos de holdout. Si la regla no acierta, se descarta.

- `AbstractionEngine.aplicar_regla()`: ejecuta la regla en un sandbox AST
  seguro (sin eval, sin exec, sin imports, sin __class__, sin atributos
  arbitrarios) sobre un input nuevo.

- `PatternStore`: persiste los patrones útiles (descripción, código, ejemplos,
  contadores éxito/fallo) y permite búsqueda por descripción.

Filosofía honesta (documentar al usuario):

* Esto NO resuelve ARC-Challenge ni captura "intuición visual" estilo Chollet.
  Es meta-learning aproximado sobre patrones algorítmicos cuya estructura
  puede expresarse como expresión Python segura.
* La inducción depende del LLM; si genera una regla mala, la verificación
  sobre holdout la descarta.
* Funciona bien para transformaciones de strings/listas/números. Falla en
  problemas que requieren sentido común profundo o razonamiento espacial.
* El sandbox AST es deliberadamente restrictivo. Rechaza cualquier
  construcción que pueda escapar (Attribute, Import, dunders, etc.).

Sin imports pesados al top-level.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("celestia_v1")


# ─── Sandbox de evaluación ─────────────────────────────────────────────

# Built-ins seguros que el LLM puede usar en las reglas. Cualquier otro
# nombre que no esté aquí se rechaza al validar el AST.
_SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs, "len": len, "min": min, "max": max, "sum": sum,
    "sorted": sorted, "reversed": reversed, "list": list, "tuple": tuple,
    "set": set, "dict": dict, "str": str, "int": int, "float": float,
    "bool": bool, "round": round, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
    "ord": ord, "chr": chr, "divmod": divmod, "pow": pow,
}

# Métodos de string/list permitidos. Llamadas a otros métodos se rechazan.
_SAFE_METHODS = frozenset({
    "upper", "lower", "title", "strip", "lstrip", "rstrip", "swapcase",
    "split", "rsplit", "splitlines", "join", "replace", "startswith",
    "endswith", "count", "find", "index", "isdigit", "isalpha", "isalnum",
    "isspace", "capitalize", "format",
    "append", "extend", "pop", "insert", "remove", "reverse", "sort",
    "copy", "clear", "keys", "values", "items", "get",
})

# Nodos AST permitidos al validar. Cualquier otro tipo lanza ValueError.
_ALLOWED_NODES = frozenset({
    "Expression", "Module", "Expr", "Constant", "Name", "Load", "Store",
    "BinOp", "UnaryOp", "BoolOp", "Compare", "IfExp",
    "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow", "MatMult",
    "USub", "UAdd", "Not", "Invert", "BitAnd", "BitOr", "BitXor",
    "LShift", "RShift", "And", "Or",
    "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn",
    "List", "Tuple", "Set", "Dict", "ListComp", "SetComp", "DictComp",
    "GeneratorExp", "comprehension",
    "Subscript", "Slice", "Index",  # Index obsoleto en py>=3.9 pero por compat
    "Call", "keyword", "Starred",
    "Lambda", "arguments", "arg",
    "Attribute",
})


class SandboxError(Exception):
    """Levantada cuando el código de regla contiene una construcción prohibida."""


def _validar_ast(tree: ast.AST) -> None:
    """Recorre el AST y rechaza cualquier nodo/construcción no segura."""
    for node in ast.walk(tree):
        name = type(node).__name__
        if name not in _ALLOWED_NODES:
            raise SandboxError(f"Nodo prohibido: {name}")
        # Rechazar dunders y atributos peligrosos
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr not in _SAFE_METHODS:
                raise SandboxError(f"Atributo prohibido: {node.attr}")
        # Llamadas: solo funciones whitelisted, métodos whitelisted o lambdas inline
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id not in _SAFE_BUILTINS:
                    raise SandboxError(f"Función prohibida: {func.id}")
            elif isinstance(func, ast.Attribute):
                if func.attr not in _SAFE_METHODS:
                    raise SandboxError(f"Método prohibido: {func.attr}")
            elif isinstance(func, ast.Lambda):
                # Lambda inline: el body ya se valida por el walk. OK.
                pass
            else:
                raise SandboxError("Llamada compleja prohibida")
        # Nombres: deben ser x, args de lambda/comprehension, o builtins
        if isinstance(node, ast.Name):
            # No filtramos aquí — el eval pasará solo SAFE_BUILTINS + {"x": input}
            pass


def evaluar_regla_segura(codigo: str, x: Any, timeout_ms: int = 500) -> Any:
    """Evalúa la expresión `codigo` sobre `x` en un sandbox restringido.

    `codigo` debe ser una expresión Python (o lambda) que usa `x` como input.
    Ejemplos: `"x * 2"`, `"x[::-1]"`, `"sum(x)"`, `"[i*2 for i in x]"`,
    `"(lambda v: v.upper())(x)"`.

    Lanza SandboxError si el código viola el sandbox. Lanza la excepción
    original si la evaluación falla por otro motivo (tipo, índice, etc.).
    """
    codigo = (codigo or "").strip()
    if not codigo:
        raise SandboxError("código vacío")
    if len(codigo) > 2000:
        raise SandboxError("código demasiado largo (>2000 chars)")
    try:
        tree = ast.parse(codigo, mode="eval")
    except SyntaxError as e:
        raise SandboxError(f"sintaxis inválida: {e}") from None
    _validar_ast(tree)
    # eval con globals/locals restringidos
    globs = {"__builtins__": {}, **_SAFE_BUILTINS}
    locs = {"x": x}
    return eval(compile(tree, "<sandbox>", "eval"), globs, locs)


# ─── Dataclasses ────────────────────────────────────────────────────────


@dataclass
class Ejemplo:
    input: Any
    output: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"input": self.input, "output": self.output}


@dataclass
class Patron:
    descripcion: str
    codigo: str  # expresión Python que toma `x` y devuelve la salida
    ejemplos: List[Ejemplo] = field(default_factory=list)
    accuracy_holdout: float = 0.0
    exito_count: int = 0
    fallo_count: int = 0
    ts: float = 0.0
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "descripcion": self.descripcion,
            "codigo": self.codigo,
            "ejemplos": [e.to_dict() for e in self.ejemplos],
            "accuracy_holdout": self.accuracy_holdout,
            "exito_count": self.exito_count,
            "fallo_count": self.fallo_count,
            "ts": self.ts,
        }


# ─── PatternStore ──────────────────────────────────────────────────────


def _init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS abstraccion_patrones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL,
        descripcion TEXT,
        codigo TEXT,
        ejemplos_json TEXT,
        accuracy_holdout REAL,
        exito_count INTEGER DEFAULT 0,
        fallo_count INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_abs_ts ON abstraccion_patrones(ts);
    """)
    conn.commit()


class PatternStore:
    """Persistencia simple para patrones inducidos."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        _init_schema(self.conn)

    def guardar(self, patron: Patron) -> int:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO abstraccion_patrones
            (ts, descripcion, codigo, ejemplos_json, accuracy_holdout,
             exito_count, fallo_count)
            VALUES (?,?,?,?,?,?,?)
        """, (
            patron.ts or time.time(), patron.descripcion, patron.codigo,
            json.dumps([e.to_dict() for e in patron.ejemplos], default=str),
            float(patron.accuracy_holdout),
            int(patron.exito_count), int(patron.fallo_count),
        ))
        self.conn.commit()
        patron.id = int(cur.lastrowid)
        return patron.id

    def obtener(self, patron_id: int) -> Optional[Patron]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ts, descripcion, codigo, ejemplos_json,
                   accuracy_holdout, exito_count, fallo_count
            FROM abstraccion_patrones WHERE id = ?
        """, (int(patron_id),))
        row = cur.fetchone()
        if not row:
            return None
        return self._row_a_patron(row)

    def listar(self, limit: int = 50) -> List[Patron]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ts, descripcion, codigo, ejemplos_json,
                   accuracy_holdout, exito_count, fallo_count
            FROM abstraccion_patrones
            ORDER BY ts DESC LIMIT ?
        """, (int(limit),))
        return [self._row_a_patron(r) for r in cur.fetchall()]

    def buscar(self, query: str, limit: int = 5) -> List[Patron]:
        """Búsqueda por descripción (LIKE simple, tolerante a mayúsculas)."""
        q = f"%{query.lower().strip()}%"
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ts, descripcion, codigo, ejemplos_json,
                   accuracy_holdout, exito_count, fallo_count
            FROM abstraccion_patrones
            WHERE LOWER(descripcion) LIKE ?
            ORDER BY accuracy_holdout DESC, exito_count DESC
            LIMIT ?
        """, (q, int(limit)))
        return [self._row_a_patron(r) for r in cur.fetchall()]

    def actualizar_contadores(self, patron_id: int,
                              exito: bool = False, fallo: bool = False) -> None:
        cur = self.conn.cursor()
        if exito:
            cur.execute(
                "UPDATE abstraccion_patrones SET exito_count = exito_count + 1 WHERE id = ?",
                (int(patron_id),),
            )
        if fallo:
            cur.execute(
                "UPDATE abstraccion_patrones SET fallo_count = fallo_count + 1 WHERE id = ?",
                (int(patron_id),),
            )
        self.conn.commit()

    def eliminar(self, patron_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM abstraccion_patrones WHERE id = ?", (int(patron_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def _row_a_patron(self, row) -> Patron:
        ejemplos_data = json.loads(row[4] or "[]")
        ejemplos = [Ejemplo(input=e["input"], output=e["output"])
                    for e in ejemplos_data]
        return Patron(
            id=row[0], ts=row[1], descripcion=row[2], codigo=row[3],
            ejemplos=ejemplos, accuracy_holdout=row[5] or 0.0,
            exito_count=row[6] or 0, fallo_count=row[7] or 0,
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ─── AbstractionEngine ─────────────────────────────────────────────────


_PROMPT_INDUCCION = """Eres un inductor de reglas. Dado un conjunto de ejemplos (input → output),
deduce la regla Python más simple posible que los explique.

Reglas que DEBES seguir:
- La regla debe ser UNA expresión Python que use la variable `x` como input.
- NO uses imports, eval, exec, atributos privados (con guión bajo), ni acceso a builtins peligrosos.
- Funciones permitidas: abs, len, min, max, sum, sorted, reversed, list, tuple, set, dict,
  str, int, float, bool, round, range, enumerate, zip, map, filter, any, all, ord, chr, divmod, pow.
- Métodos permitidos: upper, lower, strip, split, join, replace, startswith, endswith,
  count, find, append, extend, pop, reverse, sort, keys, values, items, get.

Ejemplos:
{ejemplos}

Devuelve SOLO un JSON válido sin texto adicional:
{{"descripcion": "<frase corta>", "codigo": "<expresión Python que usa x>"}}
"""


@dataclass
class ResultadoInduccion:
    descripcion: str
    codigo: str
    accuracy_train: float
    accuracy_holdout: float
    fallos: List[str] = field(default_factory=list)
    aceptada: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AbstractionEngine:
    """Inductor + verificador de reglas. Persiste patrones útiles en store."""

    def __init__(self, store: PatternStore,
                 llm_callable: Optional[Callable[[str], str]] = None,
                 umbral_aceptacion: float = 1.0):
        """umbral_aceptacion: accuracy mínima sobre holdout para aceptar (1.0 = perfecta)."""
        self.store = store
        self.llm = llm_callable
        self.umbral = float(umbral_aceptacion)

    # ─── Heurísticas para reglas comunes (sin LLM) ──────────────────

    def _heuristicas_simples(self, ejemplos: List[Ejemplo]) -> List[Tuple[str, str]]:
        """Devuelve lista de (descripcion, codigo) candidatos a probar antes del LLM.

        Cubre transformaciones triviales muy frecuentes para evitar gastar tokens.
        """
        candidatos: List[Tuple[str, str]] = [
            ("identidad", "x"),
            ("doble", "x * 2"),
            ("mitad", "x // 2"),
            ("incrementar uno", "x + 1"),
            ("cuadrado", "x * x"),
            ("absoluto", "abs(x)"),
            ("a mayúsculas", "x.upper()"),
            ("a minúsculas", "x.lower()"),
            ("invertir string", "x[::-1]"),
            ("invertir lista", "list(reversed(x))"),
            ("longitud", "len(x)"),
            ("suma de elementos", "sum(x)"),
            ("máximo", "max(x)"),
            ("mínimo", "min(x)"),
            ("ordenar", "sorted(x)"),
            ("ordenar descendente", "sorted(x, reverse=True)"),
            ("doblar cada elemento", "[i * 2 for i in x]"),
            ("primer elemento", "x[0]"),
            ("último elemento", "x[-1]"),
        ]
        return candidatos

    # ─── Verificación ────────────────────────────────────────────────

    def verificar_regla(self, codigo: str,
                        ejemplos: List[Ejemplo]) -> Tuple[float, List[str]]:
        """Aplica `codigo` a cada input y compara con output. Devuelve (accuracy, fallos)."""
        if not ejemplos:
            return 0.0, ["sin ejemplos"]
        aciertos = 0
        fallos: List[str] = []
        for e in ejemplos:
            try:
                out = evaluar_regla_segura(codigo, e.input)
                if out == e.output:
                    aciertos += 1
                else:
                    fallos.append(f"input={e.input!r} esperado={e.output!r} obtenido={out!r}")
            except SandboxError as se:
                fallos.append(f"sandbox: {se}")
            except Exception as ex:
                fallos.append(f"{type(ex).__name__}: {ex}")
        return aciertos / len(ejemplos), fallos

    # ─── Inducción ───────────────────────────────────────────────────

    def _split_train_holdout(self, ejemplos: List[Ejemplo]) -> Tuple[List[Ejemplo], List[Ejemplo]]:
        n = len(ejemplos)
        if n < 3:
            return ejemplos, ejemplos  # con muy pocos, verificamos en todos
        n_holdout = max(1, n // 3)
        return ejemplos[:-n_holdout], ejemplos[-n_holdout:]

    def inducir_regla(self, ejemplos: List[Ejemplo],
                       usar_llm: bool = True) -> ResultadoInduccion:
        """Induce una regla a partir de ejemplos. Prueba heurísticas → LLM → verifica."""
        if not ejemplos:
            return ResultadoInduccion(
                descripcion="", codigo="", accuracy_train=0.0,
                accuracy_holdout=0.0, fallos=["sin ejemplos"], aceptada=False,
            )

        train, holdout = self._split_train_holdout(ejemplos)

        # 1) Heurísticas rápidas
        for desc, codigo in self._heuristicas_simples(ejemplos):
            acc_train, _ = self.verificar_regla(codigo, train)
            if acc_train == 1.0:
                acc_hold, fallos_hold = self.verificar_regla(codigo, holdout)
                aceptada = acc_hold >= self.umbral
                return ResultadoInduccion(
                    descripcion=desc, codigo=codigo, accuracy_train=acc_train,
                    accuracy_holdout=acc_hold, fallos=fallos_hold,
                    aceptada=aceptada,
                )

        # 2) Si hay LLM disponible, pedimos regla
        if usar_llm and self.llm is not None:
            try:
                ejemplos_txt = "\n".join(
                    f"  x = {e.input!r}  →  {e.output!r}" for e in train
                )
                prompt = _PROMPT_INDUCCION.format(ejemplos=ejemplos_txt)
                respuesta = self.llm(prompt) or ""
                desc, codigo = self._parsear_respuesta_llm(respuesta)
                if codigo:
                    acc_train, _ = self.verificar_regla(codigo, train)
                    acc_hold, fallos_hold = self.verificar_regla(codigo, holdout)
                    aceptada = acc_hold >= self.umbral
                    return ResultadoInduccion(
                        descripcion=desc or "regla LLM", codigo=codigo,
                        accuracy_train=acc_train, accuracy_holdout=acc_hold,
                        fallos=fallos_hold, aceptada=aceptada,
                    )
            except Exception as e:
                logger.debug("inducción LLM falló: %s", e)

        return ResultadoInduccion(
            descripcion="(sin regla)", codigo="",
            accuracy_train=0.0, accuracy_holdout=0.0,
            fallos=["heurísticas no acertaron y LLM no produjo regla válida"],
            aceptada=False,
        )

    @staticmethod
    def _parsear_respuesta_llm(respuesta: str) -> Tuple[str, str]:
        """Extrae descripcion y codigo de la respuesta JSON (tolerante a ruido)."""
        if not respuesta:
            return "", ""
        # Busca el primer JSON object en la respuesta
        m = re.search(r"\{[^{}]*\}", respuesta, re.DOTALL)
        if not m:
            return "", ""
        try:
            data = json.loads(m.group(0))
        except Exception:
            return "", ""
        desc = str(data.get("descripcion") or "").strip()
        codigo = str(data.get("codigo") or "").strip()
        return desc, codigo

    # ─── Aplicación de patrones guardados ────────────────────────────

    def aplicar_patron(self, patron: Patron, input_val: Any) -> Tuple[bool, Any]:
        """Aplica un patrón guardado a un input nuevo.

        Devuelve (exito, output). Si la evaluación falla, devuelve (False, error_msg).
        Actualiza contadores si el patrón tiene id.
        """
        try:
            out = evaluar_regla_segura(patron.codigo, input_val)
            if patron.id is not None:
                self.store.actualizar_contadores(patron.id, exito=True)
            return True, out
        except Exception as e:
            if patron.id is not None:
                self.store.actualizar_contadores(patron.id, fallo=True)
            return False, f"{type(e).__name__}: {e}"

    def inducir_y_persistir(self, ejemplos: List[Ejemplo],
                              usar_llm: bool = True) -> Tuple[ResultadoInduccion, Optional[Patron]]:
        """Induce y, si la regla es aceptada, la persiste como Patron."""
        res = self.inducir_regla(ejemplos, usar_llm=usar_llm)
        if not res.aceptada:
            return res, None
        patron = Patron(
            descripcion=res.descripcion, codigo=res.codigo,
            ejemplos=ejemplos[:], accuracy_holdout=res.accuracy_holdout,
            ts=time.time(),
        )
        self.store.guardar(patron)
        return res, patron
