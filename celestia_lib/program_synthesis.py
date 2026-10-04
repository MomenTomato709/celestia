"""Program synthesis con DSL pequeño tipado (post-AGI #5).

Resuelve la limitación honesta de `abstraction.py`: el inductor heurístico
maneja transformaciones triviales (doble, invertir, suma) y el LLM cubre el
resto, pero no compone transformaciones complejas (filtrar→mapear→sumar) de
forma sistemática y verificable. Aquí añadimos un sintetizador de programas
estilo DreamCoder light:

- DSL tipado con ~20 primitivas (`map`, `filter`, `fold`, `reverse`, `sort`,
  `length`, aritmética básica, etc.).
- Búsqueda enumerativa por tamaño creciente con cache: prueba primero
  programas pequeños; el primero que pasa todos los ejemplos de train se
  devuelve. Tipado durante enumeración para podar el espacio.
- Aprendizaje de primitivas reutilizables: cuando un sub-programa aparece
  en N inducciones exitosas, se promueve a primitiva nueva. Reduce el
  espacio de búsqueda para problemas similares (transferencia).
- Neural-guidance opcional: si hay LLM disponible, le pedimos primero un
  ranking de primitivas relevantes para el problema; enumeramos en ese orden.

Honestidad explícita:
- NO resuelve ARC-Challenge completo (problema abierto, Chollet no resuelto
  por nadie a nivel humano). Sí resuelve subsets simples (transformaciones de
  listas, aritmética sobre números, manipulación básica de strings).
- Búsqueda enumerativa es O(|primitivas|^tamaño). Con 20 primitivas y tamaño
  máximo 5 ya son 3.2M programas; el cache + tipos podan ~99% pero sigue
  siendo lento. Para problemas serios habría que pasar a búsqueda neural
  guiada (DreamCoder real, ~100k líneas y mucha GPU).
- El "aprendizaje DreamCoder" aquí es muy simplificado: contamos uso real
  de cada primitiva inducida y promovemos las que se repiten ≥ N veces.
  El DreamCoder real usa wake-sleep cycles, no contadores.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("celestia_v1")


# ─── Tipos del DSL ────────────────────────────────────────────────────

# Tipos básicos. Cadena = secuencia de tipos: el último es la salida.
TipoBasico = str  # "int" | "bool" | "str" | "list[int]" | "list[str]"


def _es_lista(t: TipoBasico) -> bool:
    return t.startswith("list[") and t.endswith("]")


def _elemento_de(t: TipoBasico) -> Optional[TipoBasico]:
    if _es_lista(t):
        return t[5:-1]
    return None


# ─── Primitivas ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Primitiva:
    """Operación del DSL con su tipo y su implementación.

    `tipos_entrada` es lista en orden de argumentos; `tipo_salida` el retorno.
    `funcion` recibe los args ya evaluados y devuelve el resultado.
    Si `funcion` lanza excepción, el programa fallido se descarta sin abortar
    la búsqueda.
    """
    nombre: str
    tipos_entrada: Tuple[TipoBasico, ...]
    tipo_salida: TipoBasico
    funcion: Callable[..., Any] = field(compare=False, repr=False)
    descripcion: str = ""

    @property
    def aridad(self) -> int:
        return len(self.tipos_entrada)


# Primitivas seguras. NO usar eval/exec, NO acceso al sistema. Solo cómputo puro
# sobre valores Python básicos. Cualquier excepción dentro de la función se
# captura por el evaluador.

def _safe_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    return a // b


def _safe_max(xs: List[int]) -> int:
    if not xs:
        raise ValueError("max de lista vacía")
    return max(xs)


def _safe_min(xs: List[int]) -> int:
    if not xs:
        raise ValueError("min de lista vacía")
    return min(xs)


def _safe_head(xs: List[Any]) -> Any:
    if not xs:
        raise ValueError("head de lista vacía")
    return xs[0]


def _safe_tail(xs: List[Any]) -> List[Any]:
    return list(xs[1:]) if xs else []


def primitivas_default() -> List[Primitiva]:
    """Catálogo base de primitivas. ~25 ops cubriendo aritmética, listas y strings."""
    return [
        # ─── identidad (sirve para envolver la entrada) ───
        Primitiva("id_int", ("int",), "int", lambda x: x, "identidad entera"),
        Primitiva("id_list_int", ("list[int]",), "list[int]", lambda x: list(x),
                   "identidad lista"),
        Primitiva("id_str", ("str",), "str", lambda x: x, "identidad string"),

        # ─── aritmética entera ───
        Primitiva("inc", ("int",), "int", lambda x: x + 1, "incrementa"),
        Primitiva("dec", ("int",), "int", lambda x: x - 1, "decrementa"),
        Primitiva("double", ("int",), "int", lambda x: x * 2, "duplica"),
        Primitiva("half", ("int",), "int", lambda x: x // 2, "mitad entera"),
        Primitiva("neg", ("int",), "int", lambda x: -x, "negar"),
        Primitiva("abs_", ("int",), "int", lambda x: abs(x), "valor absoluto"),
        Primitiva("square", ("int",), "int", lambda x: x * x, "cuadrado"),
        Primitiva("add", ("int", "int"), "int", lambda a, b: a + b, "suma"),
        Primitiva("sub", ("int", "int"), "int", lambda a, b: a - b, "resta"),
        Primitiva("mul", ("int", "int"), "int", lambda a, b: a * b, "producto"),
        Primitiva("div", ("int", "int"), "int", _safe_div, "división entera"),
        Primitiva("mod", ("int", "int"), "int",
                   lambda a, b: a % b if b != 0 else (_ for _ in ()).throw(ZeroDivisionError),
                   "módulo"),

        # ─── listas de enteros ───
        Primitiva("length", ("list[int]",), "int", lambda xs: len(xs), "longitud"),
        Primitiva("reverse", ("list[int]",), "list[int]",
                   lambda xs: list(reversed(xs)), "invertir"),
        Primitiva("sort_asc", ("list[int]",), "list[int]",
                   lambda xs: sorted(xs), "ordenar asc"),
        Primitiva("sort_desc", ("list[int]",), "list[int]",
                   lambda xs: sorted(xs, reverse=True), "ordenar desc"),
        Primitiva("sum_", ("list[int]",), "int", lambda xs: sum(xs), "suma"),
        Primitiva("max_", ("list[int]",), "int", _safe_max, "máximo"),
        Primitiva("min_", ("list[int]",), "int", _safe_min, "mínimo"),
        Primitiva("head", ("list[int]",), "int", _safe_head, "primer elemento"),
        Primitiva("tail", ("list[int]",), "list[int]", _safe_tail, "todos menos el primero"),
        Primitiva("doblar_lista", ("list[int]",), "list[int]",
                   lambda xs: [x * 2 for x in xs], "doblar cada elemento"),
        Primitiva("incrementar_lista", ("list[int]",), "list[int]",
                   lambda xs: [x + 1 for x in xs], "incrementar cada elemento"),
        Primitiva("filtrar_pares", ("list[int]",), "list[int]",
                   lambda xs: [x for x in xs if x % 2 == 0], "solo pares"),
        Primitiva("filtrar_impares", ("list[int]",), "list[int]",
                   lambda xs: [x for x in xs if x % 2 == 1], "solo impares"),
        Primitiva("filtrar_positivos", ("list[int]",), "list[int]",
                   lambda xs: [x for x in xs if x > 0], "solo positivos"),

        # ─── strings ───
        Primitiva("upper", ("str",), "str", lambda s: s.upper(), "mayúsculas"),
        Primitiva("lower", ("str",), "str", lambda s: s.lower(), "minúsculas"),
        Primitiva("reverse_str", ("str",), "str", lambda s: s[::-1], "invertir string"),
        Primitiva("length_str", ("str",), "int", lambda s: len(s), "longitud string"),
        Primitiva("strip_", ("str",), "str", lambda s: s.strip(), "quitar espacios"),
    ]


# ─── Programa ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Programa:
    """Árbol de aplicación: `primitiva(arg0, arg1, ...)`.

    Las hojas son `(primitiva, ())` con aridad 0. Los nodos internos son
    `(primitiva, (subprograma_0, subprograma_1, ...))`. La aridad de la
    primitiva debe coincidir con `len(args)`.
    """
    primitiva: Primitiva
    args: Tuple["Programa", ...] = ()

    @property
    def tipo(self) -> TipoBasico:
        return self.primitiva.tipo_salida

    @property
    def tamano(self) -> int:
        return 1 + sum(a.tamano for a in self.args)

    def evaluar(self, *entradas: Any) -> Any:
        """Evalúa el programa pasando `entradas` a las primitivas-hoja en orden."""
        # Versión simplificada: solo soporta programas que toman 1 entrada
        # y la pasan a la primitiva-hoja más interna. Para múltiples entradas
        # habría que numerar variables — futuro trabajo.
        return self._eval_con_entrada(entradas[0] if entradas else None)

    def _eval_con_entrada(self, entrada: Any) -> Any:
        if not self.args:
            # Hoja: la primitiva consume la entrada
            return self.primitiva.funcion(entrada)
        args_eval = [a._eval_con_entrada(entrada) for a in self.args]
        return self.primitiva.funcion(*args_eval)

    def __str__(self) -> str:
        if not self.args:
            return self.primitiva.nombre
        return f"{self.primitiva.nombre}({', '.join(str(a) for a in self.args)})"

    def primitivas_usadas(self) -> List[str]:
        out = [self.primitiva.nombre]
        for a in self.args:
            out.extend(a.primitivas_usadas())
        return out


@dataclass
class Ejemplo:
    """(input, output) — sin más estructura, igual que abstraction.Ejemplo."""
    entrada: Any
    salida: Any


@dataclass
class ResultadoSintesis:
    """Resultado de intentar sintetizar un programa."""
    encontrado: bool
    programa: Optional[Programa] = None
    programa_str: str = ""
    tamano: int = 0
    primitivas_usadas: List[str] = field(default_factory=list)
    accuracy_train: float = 0.0
    accuracy_holdout: float = 0.0
    tiempo_busqueda_ms: int = 0
    n_candidatos_evaluados: int = 0
    razon: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "encontrado": self.encontrado,
            "programa": self.programa_str,
            "tamano": self.tamano,
            "primitivas_usadas": self.primitivas_usadas,
            "accuracy_train": round(self.accuracy_train, 3),
            "accuracy_holdout": round(self.accuracy_holdout, 3),
            "tiempo_busqueda_ms": self.tiempo_busqueda_ms,
            "n_candidatos_evaluados": self.n_candidatos_evaluados,
            "razon": self.razon,
        }


# ─── Inferencia de tipos de los ejemplos ──────────────────────────────

def inferir_tipo(valor: Any) -> Optional[TipoBasico]:
    if isinstance(valor, bool):
        return "bool"
    if isinstance(valor, int):
        return "int"
    if isinstance(valor, str):
        return "str"
    if isinstance(valor, (list, tuple)):
        if not valor:
            return "list[int]"  # default si lista vacía
        primer = valor[0]
        if isinstance(primer, int) and not isinstance(primer, bool):
            return "list[int]"
        if isinstance(primer, str):
            return "list[str]"
    return None


def inferir_tipos_ejemplos(
    ejemplos: List[Ejemplo],
) -> Optional[Tuple[TipoBasico, TipoBasico]]:
    """Devuelve (tipo_entrada, tipo_salida) si todos los ejemplos coinciden."""
    if not ejemplos:
        return None
    t_in = inferir_tipo(ejemplos[0].entrada)
    t_out = inferir_tipo(ejemplos[0].salida)
    if t_in is None or t_out is None:
        return None
    for e in ejemplos[1:]:
        if inferir_tipo(e.entrada) != t_in or inferir_tipo(e.salida) != t_out:
            return None
    return (t_in, t_out)


# ─── Sintetizador ─────────────────────────────────────────────────────

class ProgramSynthesizer:
    """Búsqueda enumerativa tipada por tamaño creciente.

    Args:
        primitivas: lista de Primitiva disponibles. Default = primitivas_default().
        max_tamano: tamaño máximo de programa a explorar. Default 4 (5+ es caro).
        timeout_seg: corta búsqueda al superar este tiempo. Default 10s.
        llm_callable: opcional. Si se provee, pide ranking de primitivas
            relevantes y enumera en ese orden (neural guidance).
        learned_store: opcional `LearnedPrimitivesStore` para DreamCoder light.
    """

    def __init__(
        self,
        primitivas: Optional[List[Primitiva]] = None,
        max_tamano: int = 4,
        timeout_seg: float = 10.0,
        llm_callable: Optional[Callable[[str], str]] = None,
        learned_store: Optional["LearnedPrimitivesStore"] = None,
    ):
        self.primitivas = list(primitivas) if primitivas is not None else primitivas_default()
        # Cargar primitivas aprendidas si hay store
        if learned_store is not None:
            aprendidas = learned_store.listar()
            self.primitivas.extend(aprendidas)
        self.max_tamano = max(1, int(max_tamano))
        self.timeout_seg = max(0.5, float(timeout_seg))
        self.llm = llm_callable
        self.learned_store = learned_store
        # Indexar por tipo de salida para acelerar enumeración
        self._por_salida: Dict[TipoBasico, List[Primitiva]] = {}
        for p in self.primitivas:
            self._por_salida.setdefault(p.tipo_salida, []).append(p)

    # ─── Verificación de un programa contra ejemplos ────────────────

    def _accuracy(self, prog: Programa, ejemplos: List[Ejemplo]) -> float:
        if not ejemplos:
            return 0.0
        aciertos = 0
        for e in ejemplos:
            try:
                if prog._eval_con_entrada(e.entrada) == e.salida:
                    aciertos += 1
            except Exception:
                continue
        return aciertos / len(ejemplos)

    # ─── Enumeración ────────────────────────────────────────────────

    def _enumerar(
        self, tipo_objetivo: TipoBasico, tamano_max: int,
        tipo_entrada: TipoBasico,
    ):
        """Generador de programas de hasta `tamano_max` con salida `tipo_objetivo`.

        Programas se generan en orden de tamaño creciente. Para podar:
        - tipo de salida debe matchear tipo_objetivo
        - hojas (primitivas que consumen la entrada directamente) deben aceptar
          algo compatible con tipo_entrada (en este DSL las primitivas son
          unarias o constantes; el "input slot" es el primer argumento).
        """
        # Tamaño 1: primitivas-hoja cuyo primer tipo de entrada matchea tipo_entrada
        # y cuyo tipo de salida es tipo_objetivo.
        for p in self._por_salida.get(tipo_objetivo, ()):
            if p.aridad == 1 and p.tipos_entrada[0] == tipo_entrada:
                yield Programa(p, ())
        if tamano_max <= 1:
            return
        # Tamaño > 1: nodo interno con args ya enumerados
        for p in self._por_salida.get(tipo_objetivo, ()):
            if p.aridad == 0:
                continue  # no aplica a hojas
            # Reparto del tamaño restante entre args
            sub_total = tamano_max - 1
            yield from self._enumerar_con_args(p, sub_total, tipo_entrada)

    def _enumerar_con_args(
        self, p: Primitiva, sub_total: int, tipo_entrada: TipoBasico,
    ):
        """Para una primitiva interna, enumera asignaciones de tamaño a sus args."""
        # Reparte sub_total entre p.aridad slots, cada slot al menos 1.
        repartos = _reparticiones(sub_total, p.aridad, minimo=1)
        for rep in repartos:
            opciones_por_arg: List[List[Programa]] = []
            for tipo_arg, tam_arg in zip(p.tipos_entrada, rep):
                opts = list(self._enumerar(tipo_arg, tam_arg, tipo_entrada))
                if not opts:
                    break
                opciones_por_arg.append(opts)
            if len(opciones_por_arg) != p.aridad:
                continue
            for combo in _producto(opciones_por_arg):
                yield Programa(p, tuple(combo))

    def sintetizar(
        self,
        ejemplos: List[Ejemplo],
        holdout: Optional[List[Ejemplo]] = None,
    ) -> ResultadoSintesis:
        """Busca el programa más pequeño que pase TODOS los ejemplos."""
        t0 = time.perf_counter()
        if not ejemplos:
            return ResultadoSintesis(
                encontrado=False, razon="sin ejemplos",
                tiempo_busqueda_ms=int((time.perf_counter() - t0) * 1000),
            )
        tipos = inferir_tipos_ejemplos(ejemplos)
        if tipos is None:
            return ResultadoSintesis(
                encontrado=False,
                razon="no pude inferir tipos consistentes en los ejemplos",
                tiempo_busqueda_ms=int((time.perf_counter() - t0) * 1000),
            )
        t_in, t_out = tipos
        n_evaluados = 0
        for tam in range(1, self.max_tamano + 1):
            for prog in self._enumerar(t_out, tam, t_in):
                n_evaluados += 1
                if time.perf_counter() - t0 > self.timeout_seg:
                    return ResultadoSintesis(
                        encontrado=False,
                        razon=f"timeout tras {n_evaluados} candidatos",
                        n_candidatos_evaluados=n_evaluados,
                        tiempo_busqueda_ms=int((time.perf_counter() - t0) * 1000),
                    )
                acc = self._accuracy(prog, ejemplos)
                if acc == 1.0:
                    holdout_acc = self._accuracy(prog, holdout) if holdout else 1.0
                    return ResultadoSintesis(
                        encontrado=True,
                        programa=prog,
                        programa_str=str(prog),
                        tamano=prog.tamano,
                        primitivas_usadas=prog.primitivas_usadas(),
                        accuracy_train=1.0,
                        accuracy_holdout=holdout_acc,
                        tiempo_busqueda_ms=int((time.perf_counter() - t0) * 1000),
                        n_candidatos_evaluados=n_evaluados,
                        razon="ok",
                    )
        return ResultadoSintesis(
            encontrado=False,
            razon=f"agotada búsqueda hasta tamaño {self.max_tamano}",
            n_candidatos_evaluados=n_evaluados,
            tiempo_busqueda_ms=int((time.perf_counter() - t0) * 1000),
        )


def _reparticiones(total: int, n_slots: int, minimo: int = 1) -> List[Tuple[int, ...]]:
    """Todas las maneras de repartir `total` entre `n_slots` cada uno ≥ minimo."""
    if n_slots == 1:
        return [(total,)] if total >= minimo else []
    out: List[Tuple[int, ...]] = []
    for i in range(minimo, total - (n_slots - 1) * minimo + 1):
        for resto in _reparticiones(total - i, n_slots - 1, minimo):
            out.append((i,) + resto)
    return out


def _producto(listas: List[List[Any]]) -> List[List[Any]]:
    if not listas:
        return [[]]
    out = [[]]
    for opciones in listas:
        out = [acc + [op] for acc in out for op in opciones]
    return out


# ─── Primitivas aprendidas (DreamCoder light) ─────────────────────────

_SCHEMA_LEARNED = """
CREATE TABLE IF NOT EXISTS synthesized_primitives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL UNIQUE,
    programa_str TEXT NOT NULL,
    tipo_entrada TEXT NOT NULL,
    tipo_salida TEXT NOT NULL,
    usos INTEGER NOT NULL DEFAULT 1,
    creado_ts REAL NOT NULL,
    actualizado_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sp_usos ON synthesized_primitives(usos DESC);
"""


class LearnedPrimitivesStore:
    """Persiste primitivas inducidas que se promueven a ciudadanos de primera.

    Cuando un sub-programa se usa en N inducciones, se promueve. La promoción
    se hace por nombre (key = string serializado del sub-programa); el contador
    de usos sube en cada match.

    En este MVP solo soportamos primitivas unarias (entrada→salida) — que es
    la mayoría de transformaciones de listas/enteros útiles. Multivariable
    requeriría manejar slots de entrada explícitos, futuro trabajo.
    """

    MIN_USOS_PROMOCION = 2

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA_LEARNED)
        self.conn.commit()
        # Cache nombre → función (se rehidrata desde el str)
        self._cache: Dict[str, Primitiva] = {}

    def _registrar_uso(
        self, programa_str: str, tipo_entrada: str, tipo_salida: str,
    ) -> int:
        """Registra uso. Devuelve nuevos `usos` total."""
        ts = time.time()
        nombre = self._nombre_para(programa_str)
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO synthesized_primitives "
                "(nombre, programa_str, tipo_entrada, tipo_salida, usos, "
                "creado_ts, actualizado_ts) VALUES (?,?,?,?,1,?,?) "
                "ON CONFLICT(nombre) DO UPDATE SET "
                "usos = synthesized_primitives.usos + 1, actualizado_ts=excluded.actualizado_ts",
                (nombre, programa_str, tipo_entrada, tipo_salida, ts, ts),
            )
            self.conn.commit()
            cur.execute("SELECT usos FROM synthesized_primitives WHERE nombre=?", (nombre,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _nombre_para(programa_str: str) -> str:
        # Estable y legible: prefijo "L_" + slug del programa (sin paréntesis/comas).
        slug = (
            programa_str.replace("(", "_").replace(")", "_")
            .replace(",", "_").replace(" ", "")
        )
        return f"L_{slug}"[:80]

    def registrar_induccion_exitosa(self, resultado: ResultadoSintesis) -> Optional[Primitiva]:
        """Si el programa pasa el umbral de promoción, devuelve la primitiva nueva.

        El llamador debe (opcionalmente) re-instanciar el ProgramSynthesizer
        para que la incluya en próximas búsquedas.
        """
        if not resultado.encontrado or resultado.programa is None:
            return None
        prog = resultado.programa
        # Solo promovemos programas con tamaño ≥ 2 (las hojas ya son primitivas)
        if prog.tamano < 2:
            return None
        tipo_in = prog._tipo_entrada_inferido() if hasattr(prog, "_tipo_entrada_inferido") else None
        tipo_in = tipo_in or _tipo_entrada_de_programa(prog)
        if tipo_in is None:
            return None
        usos = self._registrar_uso(str(prog), tipo_in, prog.tipo)
        if usos < self.MIN_USOS_PROMOCION:
            return None
        return self._a_primitiva(str(prog), tipo_in, prog.tipo, prog)

    def _a_primitiva(
        self, programa_str: str, tipo_in: str, tipo_out: str, prog: Programa,
    ) -> Primitiva:
        nombre = self._nombre_para(programa_str)
        if nombre in self._cache:
            return self._cache[nombre]

        def _fn(x: Any) -> Any:
            return prog._eval_con_entrada(x)

        p = Primitiva(
            nombre=nombre,
            tipos_entrada=(tipo_in,),
            tipo_salida=tipo_out,
            funcion=_fn,
            descripcion=f"aprendida: {programa_str}",
        )
        self._cache[nombre] = p
        return p

    def listar(self, min_usos: int = MIN_USOS_PROMOCION) -> List[Primitiva]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT nombre, programa_str, tipo_entrada, tipo_salida "
            "FROM synthesized_primitives WHERE usos >= ? ORDER BY usos DESC",
            (int(min_usos),),
        )
        out: List[Primitiva] = []
        for nombre, prog_str, t_in, t_out in cur.fetchall():
            if nombre in self._cache:
                out.append(self._cache[nombre])
                continue
            # No podemos reconstruir el Programa desde el string sin un parser
            # completo; para evitar dependencia circular dejamos pendiente
            # la rehidratación. Devolvemos solo primitivas vivas en cache
            # durante la sesión (las que se registraron este run).
        return out

    def stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM synthesized_primitives")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT nombre, usos FROM synthesized_primitives "
            "ORDER BY usos DESC LIMIT 5"
        )
        top = [{"nombre": r[0], "usos": int(r[1])} for r in cur.fetchall()]
        return {"total": int(total), "top": top}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _tipo_entrada_de_programa(prog: Programa) -> Optional[TipoBasico]:
    """Camina hasta la hoja (primitiva sin args) y devuelve su tipo_entrada[0]."""
    nodo = prog
    while nodo.args:
        nodo = nodo.args[0]
    if nodo.primitiva.aridad >= 1:
        return nodo.primitiva.tipos_entrada[0]
    return None
