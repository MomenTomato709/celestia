"""Memoria personal con vigencia temporal (bitemporal).

Capa unificada sobre :class:`KnowledgeGraph` que actúa como **única fuente de
verdad** para los datos personales del usuario, manejando bien las
contradicciones en el tiempo: «trabajo en X» → «ya no» → «ahora en Y».

Principio: **nunca borrar, versionar con vigencia**. Un hecho que cambia se
*cierra* (deja de ser vigente) y el nuevo se *abre*. Así:

- ``consultar_actual('trabajo')``   → el valor vigente (o None si cesó).
- ``consultar_historia('trabajo')`` → el timeline completo (lo de antes y ahora).

Diseño en ``roadmap/MEMORIA_TEMPORAL.md``. Sesión 39.

Dos modelos según la naturaleza del dato:

* **Atributos dinámicos** (un valor «actual» que evoluciona: trabajo, ciudad,
  profesión, edad…) → ``kg_estados`` (append-only). El cese se marca con un
  valor centinela; el histórico se conserva intacto.
* **Relaciones** (con entidades nombradas, posiblemente varias a la vez:
  pareja, mascota, alergias, idiomas…) → ``kg_relaciones`` (``desde_ts`` /
  ``hasta_ts``). El cese llama a ``finalizar_relacion`` (no borra).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .knowledge_graph import KnowledgeGraph, normalizar
from .memory import hecho_es_ruido

logger = logging.getLogger("celestia_v1")

# Valor centinela: marca que un atributo dinámico dejó de tener valor («ya no
# trabajo»). El histórico conserva el valor anterior; el actual pasa a «cesado».
CENTINELA_CESE = "__CESADO__"

# ── Atributos dinámicos (singulares) → kg_estados ─────────────────────────
# Un único valor vigente que evoluciona en el tiempo.
ATRIBUTOS = {
    "nombre", "edad", "trabajo", "profesion", "ciudad", "pais",
    "estado_civil", "estudios", "genero", "equipo", "cumpleanos",
    "tipo_trabajo",  # derivado: hostelería, sanidad… (no pisa "trabajo")
}

# ── Relaciones (multivaluadas) → kg_relaciones ────────────────────────────
# categoria -> (relacion_canonica, tipo_entidad). Pueden coexistir varias
# vigentes (p.ej. varias alergias, varios hobbies).
RELACIONES: Dict[str, tuple] = {
    "pareja":        ("pareja_de", "persona"),
    "ex_pareja":     ("ex_pareja_de", "persona"),
    "mascota":       ("tiene_mascota", "concepto"),
    "hijo":          ("tiene_hijo", "persona"),
    "amigo":         ("amigo_de", "persona"),
    "anterior_jefe": ("tiene_anterior_jefe", "persona"),
    "jefe":          ("tiene_jefe", "persona"),
    "alergia":       ("tiene_alergia", "concepto"),
    "idioma":        ("habla_idioma", "concepto"),
    "hobby":         ("tiene_hobby", "concepto"),
}

# ── Sinónimos de categoría → categoría canónica ───────────────────────────
SINONIMOS = {
    "empleo": "trabajo", "curro": "trabajo", "laburo": "trabajo",
    "empresa": "trabajo", "trabajo_actual": "trabajo",
    "oficio": "profesion", "profesión": "profesion",
    "vivo": "ciudad", "residencia": "ciudad", "ubicacion": "ciudad",
    "ubicación": "ciudad", "localidad": "ciudad",
    "edad_anios": "edad", "años": "edad",
    "novia": "pareja", "novio": "pareja", "esposa": "pareja",
    "esposo": "pareja", "marido": "pareja", "mujer": "pareja",
    "perro": "mascota", "gato": "mascota",
    "alergias": "alergia", "hobbies": "hobby", "aficion": "hobby",
    "afición": "hobby", "idiomas": "idioma",
}


def categoria_canonica(categoria: str) -> str:
    """Normaliza una categoría a su forma canónica."""
    c = (categoria or "").strip().lower()
    return SINONIMOS.get(c, c)


def es_relacion(categoria: str) -> bool:
    return categoria_canonica(categoria) in RELACIONES


def es_atributo(categoria: str) -> bool:
    return categoria_canonica(categoria) in ATRIBUTOS


class MemoriaPersonal:
    """API de alto nivel para los hechos personales del usuario con vigencia.

    Todas las operaciones se delegan al :class:`KnowledgeGraph`, que ya es
    thread-safe (RLock interno). Esta capa solo decide *cómo* modelar cada
    categoría (atributo vs relación) y aplica la semántica de vigencia.
    """

    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg
        # id de la entidad-usuario (la crea si no existe).
        self._uid = kg._asegurar_usuario()

    # ─── Escritura ────────────────────────────────────────────────────────

    def registrar(
        self,
        categoria: str,
        valor: str,
        *,
        fuente: Optional[str] = None,
        confianza: float = 0.9,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Da de alta (o actualiza) un hecho personal.

        Returns un dict con ``accion`` ∈ {registrado, sin_cambio, ignorado}.
        Idempotente: registrar el mismo valor vigente no duplica.
        """
        cat = categoria_canonica(categoria)
        valor = (valor or "").strip()
        if not cat or not valor or valor == CENTINELA_CESE:
            return {"accion": "ignorado", "motivo": "vacio", "categoria": cat}
        # El mismo portero que guarda los hechos de `hechos_usuario`: aquí no
        # lo había, y por eso quedaron apuntadas como profesión las palabras
        # «pero» y «explica» —de dos frases cualquiera— que luego se recitaban
        # como un dato de la persona («¿a qué me dedico?» → «Eres pero»).
        motivo = hecho_es_ruido("dato_personal", cat, valor)
        if motivo:
            logger.info("Memoria personal: %s = %r rechazado (%s)",
                        cat, valor, motivo)
            return {"accion": "ignorado", "motivo": motivo, "categoria": cat}
        ts = ts if ts is not None else time.time()

        if cat in RELACIONES:
            return self._registrar_relacion(cat, valor, fuente, confianza, ts)
        # Por defecto (y para todo lo de ATRIBUTOS) → atributo dinámico.
        return self._registrar_atributo(cat, valor, fuente, confianza, ts)

    def _registrar_atributo(self, cat, valor, fuente, confianza, ts) -> Dict[str, Any]:
        actual = self.kg.estado_actual(self._uid, cat)
        if actual and normalizar(actual["valor"]) == normalizar(valor):
            return {"accion": "sin_cambio", "categoria": cat, "valor": valor}
        self.kg.actualizar_estado(
            self._uid, cat, valor, confianza=confianza,
            fuente=fuente or "memoria_personal", ts=ts,
        )
        return {"accion": "registrado", "tipo": "atributo", "categoria": cat,
                "valor": valor}

    def _registrar_relacion(self, cat, valor, fuente, confianza, ts) -> Dict[str, Any]:
        rel, tipo_ent = RELACIONES[cat]
        ent_id = self.kg.upsert_entidad(
            tipo=tipo_ent, nombre=valor, confianza=confianza,
            fuente=fuente or "memoria_personal",
        )
        rel_id = self.kg.añadir_relacion(
            self._uid, rel, ent_id, desde_ts=ts, confianza=confianza,
            fuente=fuente or "memoria_personal",
        )
        if rel_id is None:
            return {"accion": "ignorado", "motivo": "no_resuelto", "categoria": cat}
        return {"accion": "registrado", "tipo": "relacion", "categoria": cat,
                "valor": valor, "rel_id": rel_id}

    def cesar(
        self,
        categoria: str,
        *,
        valor: Optional[str] = None,
        nuevo_valor: Optional[str] = None,
        fuente: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """El usuario dice «ya no…». Cierra el hecho vigente SIN borrarlo.

        - Atributo: registra un estado centinela (el histórico se conserva).
        - Relación: ``finalizar_relacion`` de la(s) vigente(s). Si ``valor`` se
          indica, cierra solo esa; si no, cierra todas las de esa categoría.
        - Si ``nuevo_valor`` se indica, lo abre a continuación
          («ya no en X, ahora en Y»).
        """
        cat = categoria_canonica(categoria)
        ts = ts if ts is not None else time.time()
        if not cat:
            return {"accion": "ignorado", "motivo": "vacio"}

        if cat in RELACIONES:
            res = self._cesar_relacion(cat, valor, fuente, ts)
        else:
            res = self._cesar_atributo(cat, fuente, ts)

        if nuevo_valor:
            # El nuevo valor se abre justo después del cese (ts+1µs para orden).
            alta = self.registrar(cat, nuevo_valor, fuente=fuente,
                                   ts=ts + 1e-3)
            res["nuevo"] = alta
        return res

    def _cesar_atributo(self, cat, fuente, ts) -> Dict[str, Any]:
        actual = self.kg.estado_actual(self._uid, cat)
        if not actual or actual["valor"] == CENTINELA_CESE:
            return {"accion": "sin_cambio", "categoria": cat,
                    "motivo": "ya_no_vigente"}
        self.kg.actualizar_estado(
            self._uid, cat, CENTINELA_CESE, confianza=1.0,
            fuente=fuente or "cese", ts=ts,
        )
        return {"accion": "cesado", "tipo": "atributo", "categoria": cat,
                "valor_anterior": actual["valor"]}

    def _cesar_relacion(self, cat, valor, fuente, ts) -> Dict[str, Any]:
        rel, _ = RELACIONES[cat]
        vigentes = [r for r in self.kg.relaciones_de(self._uid, "salientes",
                                                      vigentes_solo=True)
                    if r["relacion"] == rel]
        if valor:
            vn = normalizar(valor)
            vigentes = [r for r in vigentes if normalizar(r["b_nombre"]) == vn]
        cerradas = []
        for r in vigentes:
            if self.kg.finalizar_relacion(r["id"], hasta_ts=ts):
                cerradas.append(r["b_nombre"])
        if not cerradas:
            return {"accion": "sin_cambio", "categoria": cat,
                    "motivo": "nada_vigente"}
        return {"accion": "cesado", "tipo": "relacion", "categoria": cat,
                "cerradas": cerradas}

    # ─── Lectura ──────────────────────────────────────────────────────────

    def consultar_actual(self, categoria: str):
        """Valor(es) vigente(s). Atributo → str|None; relación → list[str]."""
        cat = categoria_canonica(categoria)
        if cat in RELACIONES:
            rel, _ = RELACIONES[cat]
            return [r["b_nombre"] for r in
                    self.kg.relaciones_de(self._uid, "salientes", vigentes_solo=True)
                    if r["relacion"] == rel]
        est = self.kg.estado_actual(self._uid, cat)
        if not est or est["valor"] == CENTINELA_CESE:
            return None
        return est["valor"]

    def fue_cesado(self, categoria: str) -> bool:
        """True si el atributo tuvo valor y el usuario lo dio por terminado."""
        cat = categoria_canonica(categoria)
        if cat in RELACIONES:
            return False
        est = self.kg.estado_actual(self._uid, cat)
        return bool(est and est["valor"] == CENTINELA_CESE)

    def consultar_historia(self, categoria: str) -> List[Dict[str, Any]]:
        """Timeline de una categoría, del más antiguo al más reciente.

        Atributo: lista de {valor, desde, hasta, vigente}. El centinela de
        cese cierra el tramo del valor anterior (no aparece como valor propio).
        Relación: lista de {valor, desde, hasta, vigente}.
        """
        cat = categoria_canonica(categoria)
        if cat in RELACIONES:
            rel, _ = RELACIONES[cat]
            out = []
            for r in self.kg.relaciones_de(self._uid, "salientes", vigentes_solo=False):
                if r["relacion"] != rel:
                    continue
                out.append({
                    "valor": r["b_nombre"], "desde": r["desde_ts"],
                    "hasta": r["hasta_ts"], "vigente": r["hasta_ts"] is None,
                })
            out.sort(key=lambda d: d["desde"])
            return out
        # Atributo: convertir el append-log en tramos con apertura/cierre.
        eventos = self.kg.historia_estado(self._uid, cat)
        tramos: List[Dict[str, Any]] = []
        for ev in eventos:
            if ev["valor"] == CENTINELA_CESE:
                if tramos and tramos[-1]["hasta"] is None:
                    tramos[-1]["hasta"] = ev["ts"]
                    tramos[-1]["vigente"] = False
                continue
            if tramos and tramos[-1]["hasta"] is None:
                # Nuevo valor sin cese explícito: cierra el anterior.
                tramos[-1]["hasta"] = ev["ts"]
                tramos[-1]["vigente"] = False
            tramos.append({"valor": ev["valor"], "desde": ev["ts"],
                           "hasta": None, "vigente": True})
        return tramos

    def resumen(self, incluir_historico: bool = False) -> Dict[str, Any]:
        """Todo lo que Celestia sabe del usuario, listo para introspección.

        ``{"vigente": {categoria: valor|[valores]}, "historico": {...}}``
        """
        vigente: Dict[str, Any] = {}
        for cat in sorted(ATRIBUTOS):
            v = self.consultar_actual(cat)
            if v:
                vigente[cat] = v
        for cat in sorted(RELACIONES):
            v = self.consultar_actual(cat)
            if v:
                vigente[cat] = v
        out: Dict[str, Any] = {"vigente": vigente}
        if incluir_historico:
            hist: Dict[str, Any] = {}
            for cat in list(ATRIBUTOS) + list(RELACIONES):
                h = self.consultar_historia(cat)
                if len(h) > 1 or (h and not h[0]["vigente"]):
                    hist[cat] = h
            out["historico"] = hist
        return out
