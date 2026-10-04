"""Razonador simbólico — capa de verificación (roadmap AGI punto #1).

Resuelve la crítica "el razonamiento depende del LLM base" añadiendo una
capa que NO usa LLM para la verificación final:

- Extrae afirmaciones (claims) del texto generado (regex ES heurístico, +
  LLM opcional para casos complejos).
- Verifica claims **relacionales** contra el grafo de conocimiento (si Pablo
  vive en Madrid en el grafo y la respuesta dice "vive en Barcelona", marca
  contradicción).
- Verifica claims **numéricos** con sandbox aritmético seguro
  (`ast.parse` + walk, sin `eval`, sin builtins).
- Devuelve resultado estructurado: `verificado | contradicho | no_verificable`.

Filosofía honesta: esto NO inventa razonamiento nuevo. Es una capa de
verificación simbólica que filtra alucinaciones evidentes del LLM sobre
hechos que SÍ están en el grafo o que SÍ son aritmética. Para razonamiento
abstracto sigue dependiendo del LLM (los puntos #1 y #5 del roadmap son
problemas abiertos del campo).

Sin imports pesados al top-level (regla aprendida).
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("celestia_v1")


# ─── Patrones de extracción heurísticos (español) ─────────────────────

# Cada tupla: (regex, relacion_canonica). Grupos nombrados:
#   sujeto: nombre propio (capitalizado, opcional dos palabras)
#   neg:    opcional "no/nunca/jamás/tampoco" antes del verbo → claim negado
#   objeto: complemento (lugar, persona, organización…) hasta puntuación
_SUJETO = r"(?P<sujeto>[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)"
_NEG = r"(?:(?P<neg>no|nunca|jam[aá]s|tampoco)\s+)?"
_OBJ_LUGAR = r"(?P<objeto>[A-ZÁÉÍÓÚÑ][\wáéíóúñ\s]+?)(?=[\.,;\n]|$)"
_OBJ_PERSONA = r"(?P<objeto>[\wáéíóúñ]+)"

_PATRONES_RELACIONALES: List[Tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}vive\s+en\s+{_OBJ_LUGAR}"), "vive_en"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}trabaja\s+en\s+{_OBJ_LUGAR}"), "trabaja_en"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}estudia\s+en\s+{_OBJ_LUGAR}"), "estudia_en"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:tío|tio|t[íi]a)\s+de\s+{_OBJ_PERSONA}"), "tio_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:padre|madre)\s+de\s+{_OBJ_PERSONA}"), "padre_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:herman[oa])\s+de\s+{_OBJ_PERSONA}"), "hermano_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:amig[oa]|colega)\s+de\s+{_OBJ_PERSONA}"), "amigo_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:abuel[oa])\s+de\s+{_OBJ_PERSONA}"), "abuelo_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:hij[oa])\s+de\s+{_OBJ_PERSONA}"), "hijo_de"),
    (re.compile(rf"\b{_SUJETO}\s+{_NEG}es\s+(?:el\s+|la\s+)?(?:prim[oa])\s+de\s+{_OBJ_PERSONA}"), "primo_de"),
]

# Cálculos aritméticos en texto. Captura expresión + resultado afirmado.
# Soporta: enteros, decimales con `.` o `,`, paréntesis simples, ±/×÷.
_PATRON_CALCULO = re.compile(
    r"(?P<expr>"
    r"(?:-?\d+(?:[\.,]\d+)?)"
    r"(?:\s*[+\-*/×÷]\s*-?\d+(?:[\.,]\d+)?)+"
    r")"
    r"\s*=\s*"
    r"(?P<resultado>-?\d+(?:[\.,]\d+)?)"
)


# ─── Datatypes ────────────────────────────────────────────────────────

@dataclass
class Claim:
    """Afirmación extraída de un texto."""
    texto: str                       # texto original de la afirmación
    tipo: str                        # 'relacional' | 'numerico' | 'atributivo' | 'general'
    sujeto: Optional[str] = None
    relacion: Optional[str] = None
    objeto: Optional[str] = None
    expresion: Optional[str] = None  # para numericos: la expr antes del '='
    valor_afirmado: Optional[float] = None  # para numericos
    negada: bool = False             # True si el claim afirma la negación


@dataclass
class Verificacion:
    """Resultado de verificar UN claim."""
    claim: Claim
    resultado: str                   # 'verificado' | 'contradicho' | 'no_verificable'
    metodo: str                      # 'grafo' | 'aritmetica' | 'hechos'
    evidencia: str = ""
    confianza: float = 1.0           # 0-1


@dataclass
class ResultadoVerificacion:
    """Resultado de verificar un texto completo."""
    texto: str
    consistente: bool                # True si no hay contradicciones claras
    verificaciones: List[Verificacion] = field(default_factory=list)

    @property
    def contradicciones(self) -> List[Verificacion]:
        return [v for v in self.verificaciones if v.resultado == "contradicho"]

    @property
    def verificadas(self) -> List[Verificacion]:
        return [v for v in self.verificaciones if v.resultado == "verificado"]

    def resumen(self) -> str:
        n_total = len(self.verificaciones)
        n_ver = len(self.verificadas)
        n_cont = len(self.contradicciones)
        if n_total == 0:
            return "Sin afirmaciones verificables"
        return f"{n_ver} verificado · {n_cont} contradicho · {n_total - n_ver - n_cont} no_verificable"


# ─── Sandbox aritmético seguro ────────────────────────────────────────

class _SafeArithVisitor(ast.NodeVisitor):
    """Visita un AST y verifica que solo contiene aritmética constante.

    Permite: Constant (números), BinOp (+ - * /), UnaryOp (USub, UAdd),
    Expression (raíz). Rechaza: Name, Call, Attribute, Subscript, etc.
    """
    PERMITIDOS = (ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp,
                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                  ast.USub, ast.UAdd, ast.Mod, ast.Pow)

    def __init__(self):
        self.es_seguro = True

    def generic_visit(self, node):
        if not isinstance(node, self.PERMITIDOS):
            self.es_seguro = False
            return
        super().generic_visit(node)


# Un exponente por encima de esto ya no es un cálculo, es una forma de tumbar
# el proceso: `9**9**9**9` se come toda la RAM antes de devolver nada.
_EXPONENTE_MAXIMO = 1000


def motivo_potencia_peligrosa(tree: ast.AST) -> Optional[str]:
    """¿Hay en este árbol una potencia capaz de colgar el proceso?

    Devuelve el motivo (para poder decírselo a quien pregunta) o None.

    Validar los nodos NO basta: `9**9**9**9` son todos nodos permitidos y aun
    así mata el proceso —comprobado, se lo lleva el OOM killer—, y en un móvil
    eso es tumbar a Celestia con un mensaje. La regla estaba en `tools.calcular`
    y faltaba en el evaluador del razonador: el mismo criterio en dos sitios
    acaba siendo dos criterios, así que ahora vive aquí y allí se importa.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow)):
            continue
        # Potencias anidadas: 9**9**9, (9**9)**9. Crecen tan rápido que el
        # tamaño del exponente ni siquiera hace falta mirarlo.
        subarboles = list(ast.walk(node.left)) + list(ast.walk(node.right))
        if any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.Pow) for s in subarboles):
            return "potencias anidadas"
        exp = node.right
        valor = None
        if isinstance(exp, ast.Constant) and isinstance(exp.value, (int, float)):
            valor = exp.value
        elif (isinstance(exp, ast.UnaryOp) and isinstance(exp.operand, ast.Constant)
              and isinstance(exp.operand.value, (int, float))):
            valor = exp.operand.value
        if valor is not None and abs(valor) > _EXPONENTE_MAXIMO:
            return f"exponente mayor que {_EXPONENTE_MAXIMO}"
    return None


def evaluar_aritmetica_segura(expr: str) -> Optional[float]:
    """Evalúa una expresión aritmética simple. Devuelve None si no es segura.

    Acepta enteros, decimales (con `.` o `,`), `+ - * / //% **`,
    paréntesis y unarios. NO acepta nombres, llamadas, atributos ni statements.

    NO usa `eval()` con builtins vacíos (sigue siendo inseguro frente a CPython
    expr tricks tipo `().__class__...`). Usa `ast.parse` + visitor whitelist
    + `eval` solo del AST validado y limitado a constantes/operadores.
    """
    if not expr:
        return None
    expr_norm = expr.strip().replace("×", "*").replace("÷", "/").replace(",", ".")
    try:
        tree = ast.parse(expr_norm, mode="eval")
    except (SyntaxError, ValueError):
        return None
    visitor = _SafeArithVisitor()
    visitor.visit(tree)
    if not visitor.es_seguro:
        return None
    # Los nodos pueden ser todos legales y la expresión seguir siendo una bomba.
    if motivo_potencia_peligrosa(tree):
        return None
    try:
        # Compilar el AST validado y evaluar con namespaces vacíos.
        # El AST ya fue validado: solo contiene operadores aritméticos.
        compilado = compile(tree, "<arith>", "eval")
        return float(eval(compilado, {"__builtins__": {}}, {}))  # noqa: S307
    except (ZeroDivisionError, OverflowError, ValueError, TypeError):
        return None


# ─── Razonador simbólico ──────────────────────────────────────────────

# Umbral relativo para considerar igual dos números (evita falsos contradicho
# por redondeo float).
_TOLERANCIA_NUMERICA = 1e-9


class SymbolicReasoner:
    """Verifica afirmaciones de un texto contra el grafo + aritmética.

    Args:
        knowledge: instancia de KnowledgeGraph. Si None, solo verifica numérico.
        memory: opcional. Si se provee, también consulta hechos_usuario para
            verificar atributos. (No usado en MVP, queda para extensión.)
        llm_callable: opcional. Si se provee, extracción de claims puede usar
            LLM para casos no cubiertos por regex. Si None, solo regex.

    Importante: la verificación contra el grafo NUNCA invoca al LLM — es
    lógica determinista. El LLM se usa solo (opcionalmente) para extraer
    claims complejos que el regex no captura.
    """

    def __init__(
        self,
        knowledge=None,
        memory=None,
        llm_callable: Optional[Callable[[str], str]] = None,
    ):
        self.knowledge = knowledge
        self.memory = memory
        self.llm = llm_callable

    # ─── Extracción de claims ─────────────────────────────────────────

    def extraer_claims(self, texto: str) -> List[Claim]:
        """Detecta afirmaciones relacionales y numéricas en el texto.

        Heurística regex sobre español. No usa LLM en esta vía (rápido y
        determinista). Si quieres extracción más completa, llama a
        `extraer_claims_llm` por separado.
        """
        if not texto:
            return []
        claims: List[Claim] = []
        # 1. Relacionales
        for patron, relacion in _PATRONES_RELACIONALES:
            for m in patron.finditer(texto):
                sujeto = (m.group("sujeto") or "").strip()
                objeto = (m.group("objeto") or "").strip().rstrip(".,;")
                negada = bool(m.group("neg"))
                if not sujeto or not objeto:
                    continue
                claims.append(Claim(
                    texto=m.group(0).strip(),
                    tipo="relacional",
                    sujeto=sujeto,
                    relacion=relacion,
                    objeto=objeto,
                    negada=negada,
                ))
        # 2. Numéricos
        for m in _PATRON_CALCULO.finditer(texto):
            expr = m.group("expr").strip()
            valor_str = m.group("resultado").strip().replace(",", ".")
            try:
                valor = float(valor_str)
            except ValueError:
                continue
            claims.append(Claim(
                texto=m.group(0).strip(),
                tipo="numerico",
                expresion=expr,
                valor_afirmado=valor,
            ))
        return claims

    def extraer_claims_llm(self, texto: str) -> List[Claim]:
        """Pide al LLM extracción estructurada (opcional, más cara).

        Devuelve [] si no hay LLM configurado o falla el JSON.
        """
        if self.llm is None or not texto:
            return []
        prompt = (
            "Extrae las afirmaciones factuales del siguiente texto como JSON. "
            "Para cada una, indica si es relacional (sujeto-relacion-objeto), "
            "numerica (expresion=resultado) o atributiva (entidad tiene propiedad valor).\n\n"
            f"TEXTO: «{texto[:1000]}»\n\n"
            "Responde EXCLUSIVAMENTE JSON:\n"
            '{"claims": [{"tipo":"relacional|numerico|atributivo","sujeto":"...","relacion":"...","objeto":"..."}]}'
        )
        try:
            raw = self.llm(prompt)
        except Exception as e:
            logger.debug("extraer_claims_llm: LLM falló: %s", e)
            return []
        try:
            import json
            limpio = re.sub(r"```(?:json)?|```", "", raw).strip()
            data = json.loads(limpio)
            items = data.get("claims") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
        except Exception:
            return []
        claims: List[Claim] = []
        for c in items[:20]:
            if not isinstance(c, dict):
                continue
            tipo = (c.get("tipo") or "general").strip().lower()
            claims.append(Claim(
                texto=str(c)[:200],
                tipo=tipo if tipo in ("relacional", "numerico", "atributivo") else "general",
                sujeto=(c.get("sujeto") or None),
                relacion=(c.get("relacion") or None),
                objeto=(c.get("objeto") or None),
            ))
        return claims

    # ─── Verificación contra grafo ────────────────────────────────────

    def verificar_contra_grafo(self, claim: Claim) -> Verificacion:
        """Verifica un claim relacional contra el grafo + reglas Datalog.

        Maneja claims positivos y negados:
        - positivo: si la relación existe (directa o deducida) → verificado.
          Si existe con otro objeto → contradicho. Si no existe → no_verificable.
        - negado: si la relación NO existe → verificado. Si SÍ existe → contradicho.

        Las relaciones deducidas (abuelo_de, tio_de, primo_de, …) se sacan
        vía DatalogEngine sobre los hechos vigentes del grafo.
        """
        if claim.tipo != "relacional" or not claim.sujeto or not claim.objeto:
            return Verificacion(claim=claim, resultado="no_verificable",
                                  metodo="grafo", evidencia="claim no relacional")
        if self.knowledge is None:
            return Verificacion(claim=claim, resultado="no_verificable",
                                  metodo="grafo", evidencia="grafo no disponible")
        # Resolver sujeto y objeto a IDs
        sujeto_ent = self.knowledge.obtener_entidad(claim.sujeto)
        objeto_ent = self.knowledge.obtener_entidad(claim.objeto)
        if sujeto_ent is None or objeto_ent is None:
            return Verificacion(
                claim=claim, resultado="no_verificable", metodo="grafo",
                evidencia=f"sujeto={'OK' if sujeto_ent else 'desconocido'}, "
                            f"objeto={'OK' if objeto_ent else 'desconocido'}",
            )
        sujeto_id = int(sujeto_ent["id"])
        objeto_id = int(objeto_ent["id"])
        # Buscar relación EXACTA en el grafo (capa rápida, hechos directos)
        relaciones = self.knowledge.relaciones_de(sujeto_id, direccion="salientes",
                                                          vigentes_solo=True)
        misma_relacion = [
            r for r in relaciones
            if r["relacion"] == claim.relacion and r["entidad_b_id"] == objeto_id
        ]
        misma_rel_otro_obj = [
            r for r in relaciones if r["relacion"] == claim.relacion
        ]
        existe_directa = bool(misma_relacion)

        # Capa de inferencia Datalog (relaciones deducidas como abuelo_de, tio_de…)
        existe_deducida = False
        if not existe_directa:
            try:
                from .datalog import atom as _at
                engine, _saturado = self.knowledge.deducir_relaciones_implicitas()
                sn = sujeto_ent["nombre"].strip().lower()
                on = objeto_ent["nombre"].strip().lower()
                existe_deducida = engine.es_verdad(_at(claim.relacion, sn, on))
            except Exception as e:
                logger.debug("Datalog falló al deducir: %s", e)

        existe = existe_directa or existe_deducida

        # ─── Claim negado ─────────────────────────────────────
        if claim.negada:
            if existe:
                evid = (f"el grafo {'sabe' if existe_directa else 'deduce'} "
                        f"que {claim.sujeto} -{claim.relacion}-> {claim.objeto}, "
                        f"pero la afirmación lo niega")
                return Verificacion(
                    claim=claim, resultado="contradicho", metodo="grafo",
                    evidencia=evid,
                    confianza=float(misma_relacion[0]["confianza"])
                              if misma_relacion else 0.8,
                )
            # No existe → la negación es consistente
            return Verificacion(
                claim=claim, resultado="verificado", metodo="grafo",
                evidencia=(f"el grafo no tiene {claim.relacion} "
                           f"entre {claim.sujeto} y {claim.objeto}"),
            )

        # ─── Claim positivo ───────────────────────────────────
        if existe_directa:
            return Verificacion(
                claim=claim, resultado="verificado", metodo="grafo",
                evidencia=f"relación existe en grafo: {claim.sujeto} -{claim.relacion}-> {claim.objeto}",
                confianza=float(misma_relacion[0].get("confianza", 1.0)),
            )
        if existe_deducida:
            return Verificacion(
                claim=claim, resultado="verificado", metodo="grafo",
                evidencia=(f"relación deducida por reglas: "
                           f"{claim.sujeto} -{claim.relacion}-> {claim.objeto}"),
                confianza=0.85,
            )
        if misma_rel_otro_obj:
            otros = [r["b_nombre"] for r in misma_rel_otro_obj]
            return Verificacion(
                claim=claim, resultado="contradicho", metodo="grafo",
                evidencia=(
                    f"el grafo dice {claim.sujeto} -{claim.relacion}-> {', '.join(otros)} "
                    f"(no {claim.objeto})"
                ),
                confianza=float(misma_rel_otro_obj[0].get("confianza", 1.0)),
            )
        return Verificacion(
            claim=claim, resultado="no_verificable", metodo="grafo",
            evidencia=f"el grafo no tiene información sobre {claim.relacion} de {claim.sujeto}",
        )

    # ─── Verificación numérica ────────────────────────────────────────

    def verificar_numerico(self, claim: Claim) -> Verificacion:
        if claim.tipo != "numerico" or not claim.expresion or claim.valor_afirmado is None:
            return Verificacion(claim=claim, resultado="no_verificable",
                                  metodo="aritmetica", evidencia="no es claim numérico")
        valor_real = evaluar_aritmetica_segura(claim.expresion)
        if valor_real is None:
            return Verificacion(claim=claim, resultado="no_verificable",
                                  metodo="aritmetica",
                                  evidencia="expresión no evaluable de forma segura")
        diferencia = abs(valor_real - claim.valor_afirmado)
        # Tolerancia: absoluta para enteros pequeños, relativa para grandes
        tolerancia = max(_TOLERANCIA_NUMERICA, abs(valor_real) * 1e-6)
        if diferencia <= tolerancia:
            return Verificacion(
                claim=claim, resultado="verificado", metodo="aritmetica",
                evidencia=f"{claim.expresion} = {valor_real}",
            )
        return Verificacion(
            claim=claim, resultado="contradicho", metodo="aritmetica",
            evidencia=f"{claim.expresion} = {valor_real}, no {claim.valor_afirmado}",
        )

    # ─── Verificación global ──────────────────────────────────────────

    def verificar(self, texto: str, usar_llm: bool = False) -> ResultadoVerificacion:
        """Extrae claims y verifica todos. Devuelve resultado agregado."""
        if not texto:
            return ResultadoVerificacion(texto=texto, consistente=True)
        claims = self.extraer_claims(texto)
        if usar_llm:
            claims_llm = self.extraer_claims_llm(texto)
            # Dedup simple: mismo texto/sujeto/objeto
            vistos = {(c.sujeto, c.relacion, c.objeto) for c in claims if c.tipo == "relacional"}
            for cl in claims_llm:
                k = (cl.sujeto, cl.relacion, cl.objeto)
                if cl.tipo == "relacional" and k not in vistos:
                    claims.append(cl)
                    vistos.add(k)
        verificaciones: List[Verificacion] = []
        for c in claims:
            if c.tipo == "relacional":
                verificaciones.append(self.verificar_contra_grafo(c))
            elif c.tipo == "numerico":
                verificaciones.append(self.verificar_numerico(c))
            else:
                verificaciones.append(Verificacion(
                    claim=c, resultado="no_verificable", metodo="hechos",
                    evidencia="tipo de claim sin verificador",
                ))
        contradicciones = [v for v in verificaciones if v.resultado == "contradicho"]
        return ResultadoVerificacion(
            texto=texto,
            consistente=(len(contradicciones) == 0),
            verificaciones=verificaciones,
        )
