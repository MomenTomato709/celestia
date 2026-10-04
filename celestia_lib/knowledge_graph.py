"""Grafo de conocimiento persistente (world model — punto #3 del roadmap AGI).

Modela el mundo conocido por Celestia como un grafo dirigido con temporalidad:

- **Entidades**: las "cosas" (personas, lugares, objetos, conceptos, eventos,
  organizaciones). Identificadas por (tipo, nombre_normalizado) único.
- **Relaciones**: aristas dirigidas con vigencia temporal (`desde_ts`/`hasta_ts`).
  Vocabulario libre pero con catálogo canónico inicial.
- **Estados**: propiedades dinámicas con historial (humor, ubicación, salud...).
  Append-only — para "estado actual" se toma el más reciente.

Diseñado para complementar la tabla plana `hechos_usuario` de `MemoryDB`. Vive
en el mismo archivo SQLite (`memoria/celestia.db`) pero con conexión propia
para mantener separación de concerns y permitir tests con `:memory:`.

Honestidad: esto NO es un world model tipo JEPA (LeCun). Es una aproximación
simbólica útil para inyectar contexto al LLM y dar a Celestia algo parecido
a "comprensión persistente del mundo" — suficiente para demos y para razonar
sobre el usuario y su entorno sin reinventar la rueda del campo.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

logger = logging.getLogger("celestia_v1")


# Catálogo canónico inicial. NO es exhaustivo; el extractor LLM puede añadir más.
TIPOS_ENTIDAD = {"persona", "lugar", "objeto", "concepto", "evento", "organizacion"}

RELACIONES_CANONICAS = {
    # Familiares
    "pariente_de", "padre_de", "madre_de", "hijo_de", "hija_de",
    "hermano_de", "hermana_de", "tio_de", "tia_de", "primo_de", "abuelo_de",
    # Sociales
    "amigo_de", "conoce_a", "pareja_de", "ex_pareja_de", "colega_de",
    # Profesionales / organizacionales
    "trabaja_en", "estudia_en", "miembro_de", "fundo", "dirige",
    # Espaciales / posesión
    "vive_en", "esta_en", "tiene", "posee", "usa",
    # Preferenciales
    "gusta", "odia", "prefiere", "evita",
    # Eventos
    "asistio_a", "participo_en", "ocurrio_en",
    # Genéricas
    "es", "tipo_de", "relacionado_con",
}


def normalizar(texto: str) -> str:
    """lower + sin tildes + sin puntuación bordes — para búsqueda case-insensitive.

    Usa NFKD para descomponer caracteres y filtra los diacríticos. Mantiene
    espacios internos pero recorta los extremos. Idempotente.
    """
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpio = re.sub(r"[^\w\s-]", " ", sin_tildes, flags=re.UNICODE)
    return re.sub(r"\s+", " ", limpio).strip().lower()


# Heurística simple para "mi X" → tipo de relación. Se expandirá con uso real.
REFERENCIAS_FAMILIARES = {
    "tio": "tio_de", "tia": "tia_de",
    "padre": "padre_de", "madre": "madre_de", "papa": "padre_de", "mama": "madre_de",
    "hermano": "hermano_de", "hermana": "hermana_de",
    "hijo": "hijo_de", "hija": "hija_de",
    "primo": "primo_de", "prima": "primo_de",
    "abuelo": "abuelo_de", "abuela": "abuelo_de",
    "amigo": "amigo_de", "amiga": "amigo_de",
    "pareja": "pareja_de", "novio": "pareja_de", "novia": "pareja_de",
    "jefe": "trabaja_en", "compañero": "colega_de", "colega": "colega_de",
}


class KnowledgeGraph:
    """Grafo de conocimiento sobre SQLite con temporalidad y confianza.

    Thread-safe vía `threading.Lock` (un único lock para todas las escrituras —
    SQLite ya serializa internamente, pero el lock evita race conditions en
    la lógica de upsert + update timestamp).

    Args:
        db_path: ruta al archivo SQLite o ":memory:" para tests.
        usuario_nombre: nombre canónico de la entidad-usuario. Se crea
            automáticamente al inicializar si no existe (sirve de ancla
            para resolver referencias como "mi tío" → vecino del usuario).
    """

    # Confianza por defecto para inserts manuales (1.0 = certeza total).
    CONFIANZA_DEFAULT = 1.0

    # Decay multiplicativo aplicado a la confianza cuando se observa una
    # contradicción. Si confianza < 0.1, el hecho se considera obsoleto.
    DECAY_CONTRADICCION = 0.5

    def __init__(self, db_path: Union[str, Path], usuario_nombre: str = "usuario"):
        self.db_path = str(db_path)
        self.usuario_nombre = usuario_nombre
        self._lock = threading.Lock()
        # check_same_thread=False porque escrituras pueden venir del hilo
        # de extracción LLM que es distinto al hilo HTTP.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()
        self._asegurar_usuario()

    # ─── Schema ─────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS kg_entidades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            nombre TEXT NOT NULL,
            nombre_normalizado TEXT NOT NULL,
            alias_json TEXT,                   -- JSON array de aliases
            atributos_json TEXT,               -- JSON dict de propiedades estáticas
            confianza REAL NOT NULL DEFAULT 1.0,
            fuente TEXT,
            primera_mencion_ts REAL NOT NULL,
            ultima_mencion_ts REAL NOT NULL,
            UNIQUE(tipo, nombre_normalizado)
        );
        CREATE TABLE IF NOT EXISTS kg_relaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidad_a_id INTEGER NOT NULL,
            relacion TEXT NOT NULL,
            entidad_b_id INTEGER NOT NULL,
            desde_ts REAL NOT NULL,
            hasta_ts REAL,                     -- NULL = aún vigente
            confianza REAL NOT NULL DEFAULT 1.0,
            fuente TEXT,
            atributos_json TEXT,
            FOREIGN KEY (entidad_a_id) REFERENCES kg_entidades(id) ON DELETE CASCADE,
            FOREIGN KEY (entidad_b_id) REFERENCES kg_entidades(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS kg_estados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidad_id INTEGER NOT NULL,
            propiedad TEXT NOT NULL,
            valor TEXT NOT NULL,
            ts REAL NOT NULL,
            confianza REAL NOT NULL DEFAULT 1.0,
            fuente TEXT,
            FOREIGN KEY (entidad_id) REFERENCES kg_entidades(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_kg_ent_tipo ON kg_entidades(tipo);
        CREATE INDEX IF NOT EXISTS idx_kg_ent_norm ON kg_entidades(nombre_normalizado);
        CREATE INDEX IF NOT EXISTS idx_kg_ent_ult ON kg_entidades(ultima_mencion_ts);
        CREATE INDEX IF NOT EXISTS idx_kg_rel_a ON kg_relaciones(entidad_a_id);
        CREATE INDEX IF NOT EXISTS idx_kg_rel_b ON kg_relaciones(entidad_b_id);
        CREATE INDEX IF NOT EXISTS idx_kg_rel_tipo ON kg_relaciones(relacion);
        CREATE INDEX IF NOT EXISTS idx_kg_rel_vigentes ON kg_relaciones(hasta_ts);
        CREATE INDEX IF NOT EXISTS idx_kg_est_ent_prop ON kg_estados(entidad_id, propiedad, ts);
        """)
        self.conn.commit()

    def _asegurar_usuario(self) -> int:
        """Crea la entidad-usuario si no existe. Devuelve su id."""
        return self.upsert_entidad(
            tipo="persona",
            nombre=self.usuario_nombre,
            atributos={"rol": "usuario_principal"},
            fuente="sistema",
        )

    # ─── CRUD entidades ─────────────────────────────────────────────────

    def upsert_entidad(
        self,
        tipo: str,
        nombre: str,
        alias: Optional[Iterable[str]] = None,
        atributos: Optional[Dict[str, Any]] = None,
        confianza: float = CONFIANZA_DEFAULT,
        fuente: Optional[str] = None,
    ) -> int:
        """Inserta o actualiza una entidad. Devuelve su id.

        Si ya existe (mismo tipo + nombre normalizado), fusiona aliases y
        atributos, actualiza `ultima_mencion_ts`, y promedia la confianza
        (observación adicional refuerza el hecho).
        """
        if tipo not in TIPOS_ENTIDAD:
            logger.debug("Tipo de entidad no canónico: %s (se guarda igual)", tipo)
        nombre = nombre.strip()
        if not nombre:
            raise ValueError("nombre no puede estar vacío")
        nombre_norm = normalizar(nombre)
        alias_list = sorted({a.strip() for a in (alias or []) if a and a.strip()})
        ts = time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT id, alias_json, atributos_json, confianza "
                "FROM kg_entidades WHERE tipo=? AND nombre_normalizado=?",
                (tipo, nombre_norm),
            )
            row = cur.fetchone()
            if row is not None:
                ent_id, alias_existentes, atrs_existentes, conf_actual = row
                aliases_merge = self._merge_aliases(alias_existentes, alias_list)
                atrs_merge = self._merge_atributos(atrs_existentes, atributos)
                # Refuerzo bayesiano simple: conf_nueva = 1 - (1 - conf_a)*(1 - conf_b)
                conf_nueva = 1.0 - (1.0 - conf_actual) * (1.0 - confianza)
                cur.execute(
                    "UPDATE kg_entidades SET alias_json=?, atributos_json=?, "
                    "confianza=?, ultima_mencion_ts=? WHERE id=?",
                    (
                        json.dumps(aliases_merge, ensure_ascii=False) if aliases_merge else None,
                        json.dumps(atrs_merge, ensure_ascii=False) if atrs_merge else None,
                        min(1.0, conf_nueva),
                        ts,
                        ent_id,
                    ),
                )
                self.conn.commit()
                return int(ent_id)
            cur.execute(
                "INSERT INTO kg_entidades "
                "(tipo, nombre, nombre_normalizado, alias_json, atributos_json, "
                "confianza, fuente, primera_mencion_ts, ultima_mencion_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tipo,
                    nombre,
                    nombre_norm,
                    json.dumps(alias_list, ensure_ascii=False) if alias_list else None,
                    json.dumps(atributos, ensure_ascii=False) if atributos else None,
                    confianza,
                    fuente,
                    ts,
                    ts,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _merge_aliases(existente_json: Optional[str], nuevos: List[str]) -> List[str]:
        try:
            previos = json.loads(existente_json) if existente_json else []
        except (json.JSONDecodeError, TypeError):
            previos = []
        return sorted(set(previos) | set(nuevos))

    @staticmethod
    def _merge_atributos(
        existente_json: Optional[str], nuevos: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        try:
            previos = json.loads(existente_json) if existente_json else {}
        except (json.JSONDecodeError, TypeError):
            previos = {}
        if nuevos:
            previos.update({k: v for k, v in nuevos.items() if v is not None})
        return previos

    def obtener_entidad(self, id_o_nombre: Union[int, str], tipo: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Devuelve dict completo de una entidad, o None.

        Acepta id numérico o nombre (con o sin normalizar). Si pasa nombre y
        hay ambigüedad entre tipos, se puede acotar con el parámetro `tipo`.
        """
        cur = self.conn.cursor()
        if isinstance(id_o_nombre, int):
            cur.execute("SELECT * FROM kg_entidades WHERE id=?", (id_o_nombre,))
        else:
            nombre_norm = normalizar(id_o_nombre)
            if tipo:
                cur.execute(
                    "SELECT * FROM kg_entidades WHERE tipo=? AND nombre_normalizado=?",
                    (tipo, nombre_norm),
                )
            else:
                cur.execute(
                    "SELECT * FROM kg_entidades WHERE nombre_normalizado=? "
                    "ORDER BY ultima_mencion_ts DESC LIMIT 1",
                    (nombre_norm,),
                )
        row = cur.fetchone()
        return self._row_a_entidad(row, cur) if row else None

    def buscar_entidades(
        self, query: str, tipo: Optional[str] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Búsqueda por nombre o alias. Match exacto, prefijo y substring."""
        q_norm = normalizar(query)
        if not q_norm:
            return []
        cur = self.conn.cursor()
        if tipo:
            cur.execute(
                "SELECT * FROM kg_entidades WHERE tipo=? AND "
                "(nombre_normalizado = ? OR nombre_normalizado LIKE ? OR "
                "alias_json LIKE ?) ORDER BY "
                "CASE WHEN nombre_normalizado = ? THEN 0 "
                "     WHEN nombre_normalizado LIKE ? THEN 1 ELSE 2 END, "
                "ultima_mencion_ts DESC LIMIT ?",
                (tipo, q_norm, f"%{q_norm}%", f"%{query}%", q_norm, f"{q_norm}%", limit),
            )
        else:
            cur.execute(
                "SELECT * FROM kg_entidades WHERE "
                "nombre_normalizado = ? OR nombre_normalizado LIKE ? OR "
                "alias_json LIKE ? ORDER BY "
                "CASE WHEN nombre_normalizado = ? THEN 0 "
                "     WHEN nombre_normalizado LIKE ? THEN 1 ELSE 2 END, "
                "ultima_mencion_ts DESC LIMIT ?",
                (q_norm, f"%{q_norm}%", f"%{query}%", q_norm, f"{q_norm}%", limit),
            )
        return [self._row_a_entidad(r, cur) for r in cur.fetchall()]

    def listar_entidades(
        self, tipo: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        if tipo:
            cur.execute(
                "SELECT * FROM kg_entidades WHERE tipo=? "
                "ORDER BY ultima_mencion_ts DESC LIMIT ?",
                (tipo, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM kg_entidades ORDER BY ultima_mencion_ts DESC LIMIT ?",
                (limit,),
            )
        return [self._row_a_entidad(r, cur) for r in cur.fetchall()]

    def borrar_entidad(self, entidad_id: int) -> bool:
        """Borra entidad + sus relaciones + estados (cascade). True si existía."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM kg_entidades WHERE id=?", (entidad_id,))
            self.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_a_entidad(row: tuple, cur: sqlite3.Cursor) -> Dict[str, Any]:
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        for k in ("alias_json", "atributos_json"):
            if k in d and d[k]:
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k.replace("_json", "")] = None
            elif k in d:
                d[k.replace("_json", "")] = [] if k == "alias_json" else {}
            d.pop(k, None)
        return d

    # ─── CRUD relaciones ────────────────────────────────────────────────

    def _resolver_id(self, entidad: Union[int, str], tipo: Optional[str] = None) -> Optional[int]:
        if isinstance(entidad, int):
            return entidad
        ent = self.obtener_entidad(entidad, tipo=tipo)
        return int(ent["id"]) if ent else None

    def añadir_relacion(
        self,
        a: Union[int, str],
        relacion: str,
        b: Union[int, str],
        desde_ts: Optional[float] = None,
        confianza: float = CONFIANZA_DEFAULT,
        fuente: Optional[str] = None,
        atributos: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Crea arista vigente A -[relacion]-> B. Devuelve id o None si falla resolución."""
        a_id = self._resolver_id(a)
        b_id = self._resolver_id(b)
        if a_id is None or b_id is None:
            logger.warning("añadir_relacion: no pude resolver a=%s b=%s", a, b)
            return None
        if a_id == b_id:
            logger.debug("Ignorando auto-relación %s -> %s", a, relacion)
            return None
        rel = relacion.strip().lower()
        ts = desde_ts if desde_ts is not None else time.time()
        with self._lock:
            cur = self.conn.cursor()
            # Si ya existe relación vigente del mismo tipo entre A y B, no duplicar
            cur.execute(
                "SELECT id FROM kg_relaciones WHERE entidad_a_id=? AND relacion=? "
                "AND entidad_b_id=? AND hasta_ts IS NULL",
                (a_id, rel, b_id),
            )
            existente = cur.fetchone()
            if existente:
                return int(existente[0])
            cur.execute(
                "INSERT INTO kg_relaciones "
                "(entidad_a_id, relacion, entidad_b_id, desde_ts, hasta_ts, "
                "confianza, fuente, atributos_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    a_id, rel, b_id, ts, None, confianza, fuente,
                    json.dumps(atributos, ensure_ascii=False) if atributos else None,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finalizar_relacion(self, rel_id: int, hasta_ts: Optional[float] = None) -> bool:
        """Marca una relación como ya no vigente (cierra con `hasta_ts`)."""
        ts = hasta_ts if hasta_ts is not None else time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE kg_relaciones SET hasta_ts=? WHERE id=? AND hasta_ts IS NULL",
                (ts, rel_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def relaciones_de(
        self,
        entidad: Union[int, str],
        direccion: str = "salientes",
        vigentes_solo: bool = True,
    ) -> List[Dict[str, Any]]:
        """Relaciones de una entidad.

        Args:
            entidad: id o nombre.
            direccion: 'salientes' (A->B donde A=entidad), 'entrantes' (B<-A
                donde B=entidad), o 'ambas'.
            vigentes_solo: filtra `hasta_ts IS NULL`.
        """
        ent_id = self._resolver_id(entidad)
        if ent_id is None:
            return []
        cur = self.conn.cursor()
        vig = "AND hasta_ts IS NULL" if vigentes_solo else ""
        if direccion == "salientes":
            cur.execute(
                f"SELECT r.id, r.entidad_a_id, r.relacion, r.entidad_b_id, "
                f"r.desde_ts, r.hasta_ts, r.confianza, r.fuente, "
                f"ea.nombre AS a_nombre, eb.nombre AS b_nombre "
                f"FROM kg_relaciones r "
                f"JOIN kg_entidades ea ON ea.id = r.entidad_a_id "
                f"JOIN kg_entidades eb ON eb.id = r.entidad_b_id "
                f"WHERE r.entidad_a_id = ? {vig} ORDER BY r.desde_ts DESC",
                (ent_id,),
            )
        elif direccion == "entrantes":
            cur.execute(
                f"SELECT r.id, r.entidad_a_id, r.relacion, r.entidad_b_id, "
                f"r.desde_ts, r.hasta_ts, r.confianza, r.fuente, "
                f"ea.nombre AS a_nombre, eb.nombre AS b_nombre "
                f"FROM kg_relaciones r "
                f"JOIN kg_entidades ea ON ea.id = r.entidad_a_id "
                f"JOIN kg_entidades eb ON eb.id = r.entidad_b_id "
                f"WHERE r.entidad_b_id = ? {vig} ORDER BY r.desde_ts DESC",
                (ent_id,),
            )
        else:  # ambas
            cur.execute(
                f"SELECT r.id, r.entidad_a_id, r.relacion, r.entidad_b_id, "
                f"r.desde_ts, r.hasta_ts, r.confianza, r.fuente, "
                f"ea.nombre AS a_nombre, eb.nombre AS b_nombre "
                f"FROM kg_relaciones r "
                f"JOIN kg_entidades ea ON ea.id = r.entidad_a_id "
                f"JOIN kg_entidades eb ON eb.id = r.entidad_b_id "
                f"WHERE (r.entidad_a_id = ? OR r.entidad_b_id = ?) {vig} "
                f"ORDER BY r.desde_ts DESC",
                (ent_id, ent_id),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ─── CRUD estados ───────────────────────────────────────────────────

    def actualizar_estado(
        self,
        entidad: Union[int, str],
        propiedad: str,
        valor: str,
        confianza: float = CONFIANZA_DEFAULT,
        fuente: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> Optional[int]:
        """Registra un nuevo valor para una propiedad dinámica (append-only)."""
        ent_id = self._resolver_id(entidad)
        if ent_id is None:
            return None
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO kg_estados "
                "(entidad_id, propiedad, valor, ts, confianza, fuente) "
                "VALUES (?,?,?,?,?,?)",
                (ent_id, propiedad.strip().lower(), valor, ts, confianza, fuente),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def estado_actual(
        self, entidad: Union[int, str], propiedad: str
    ) -> Optional[Dict[str, Any]]:
        """Devuelve el valor más reciente de una propiedad."""
        ent_id = self._resolver_id(entidad)
        if ent_id is None:
            return None
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, propiedad, valor, ts, confianza, fuente "
            "FROM kg_estados WHERE entidad_id=? AND propiedad=? "
            "ORDER BY ts DESC LIMIT 1",
            (ent_id, propiedad.strip().lower()),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def historia_estado(
        self,
        entidad: Union[int, str],
        propiedad: str,
        desde_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Timeline completa de una propiedad."""
        ent_id = self._resolver_id(entidad)
        if ent_id is None:
            return []
        cur = self.conn.cursor()
        if desde_ts is not None:
            cur.execute(
                "SELECT id, propiedad, valor, ts, confianza, fuente "
                "FROM kg_estados WHERE entidad_id=? AND propiedad=? AND ts>=? "
                "ORDER BY ts ASC",
                (ent_id, propiedad.strip().lower(), desde_ts),
            )
        else:
            cur.execute(
                "SELECT id, propiedad, valor, ts, confianza, fuente "
                "FROM kg_estados WHERE entidad_id=? AND propiedad=? "
                "ORDER BY ts ASC",
                (ent_id, propiedad.strip().lower()),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ─── Queries de grafo ───────────────────────────────────────────────

    def vecinos(
        self,
        entidad: Union[int, str],
        profundidad: int = 1,
        vigentes_solo: bool = True,
    ) -> Dict[str, Any]:
        """BFS desde `entidad` hasta `profundidad`. Devuelve árbol con relaciones."""
        ent_id = self._resolver_id(entidad)
        if ent_id is None:
            return {}
        visitados = {ent_id}
        frontera = {ent_id}
        capas: List[List[Dict[str, Any]]] = []
        for _ in range(max(0, profundidad)):
            capa_actual: List[Dict[str, Any]] = []
            siguiente_frontera: set = set()
            for nid in frontera:
                rels = self.relaciones_de(nid, direccion="ambas", vigentes_solo=vigentes_solo)
                for r in rels:
                    otro_id = r["entidad_b_id"] if r["entidad_a_id"] == nid else r["entidad_a_id"]
                    if otro_id in visitados:
                        continue
                    visitados.add(otro_id)
                    siguiente_frontera.add(otro_id)
                    capa_actual.append(r)
            capas.append(capa_actual)
            frontera = siguiente_frontera
            if not frontera:
                break
        return {
            "raiz": self.obtener_entidad(ent_id),
            "capas": capas,
            "total_alcanzadas": len(visitados) - 1,
        }

    def resolver_referencia(self, texto: str) -> Optional[int]:
        """Convierte una mención ("mi tío", "Pablo", "el jefe") en un id.

        Heurística:
        1. Match exacto por nombre/alias normalizado.
        2. Si empieza por "mi/mis/el/la/los/las X" y X es relación familiar/social
           conocida, busca vecinos del usuario unidos por esa relación.
        3. None si nada matchea.

        Esta heurística es deliberadamente simple. Un extractor LLM más fino
        puede llamar a `upsert_entidad` directamente con el nombre canónico.
        """
        if not texto:
            return None
        t = normalizar(texto)
        # 1. Match directo
        entidades = self.buscar_entidades(texto, limit=1)
        if entidades:
            return int(entidades[0]["id"])
        # 2. "mi tío" → usuario --tio_de--> ?
        m = re.match(r"^(?:mi|mis|el|la|los|las)\s+(\w+)", t)
        if m:
            posible = m.group(1)
            relacion = REFERENCIAS_FAMILIARES.get(posible)
            if relacion:
                usuario_id = self._resolver_id(self.usuario_nombre)
                if usuario_id is not None:
                    cur = self.conn.cursor()
                    cur.execute(
                        "SELECT entidad_b_id FROM kg_relaciones "
                        "WHERE entidad_a_id=? AND relacion=? AND hasta_ts IS NULL "
                        "ORDER BY desde_ts DESC LIMIT 1",
                        (usuario_id, relacion),
                    )
                    row = cur.fetchone()
                    if row:
                        return int(row[0])
                    # También probar dirección inversa: ? --tio_de--> usuario
                    cur.execute(
                        "SELECT entidad_a_id FROM kg_relaciones "
                        "WHERE entidad_b_id=? AND relacion=? AND hasta_ts IS NULL "
                        "ORDER BY desde_ts DESC LIMIT 1",
                        (usuario_id, relacion),
                    )
                    row = cur.fetchone()
                    if row:
                        return int(row[0])
        return None

    def conocimiento_relevante(self, mensaje: str, top_k: int = 5) -> str:
        """Extrae narrativa con entidades + relaciones relevantes al mensaje.

        Diseñado para inyectarse en el system prompt del LLM. Recorre tokens
        del mensaje, resuelve los que matchean entidades conocidas, y devuelve
        un párrafo compacto con sus relaciones vigentes y estados actuales.

        Devuelve string vacío si no encuentra nada (no añade ruido al prompt).
        """
        if not mensaje:
            return ""
        candidatos: List[Tuple[int, float]] = []  # (entidad_id, score)
        # Heurística: cualquier token >=4 chars o secuencia de capitalizadas
        tokens = re.findall(r"\b[\w]{4,}\b", mensaje, flags=re.UNICODE)
        secuencias_cap = re.findall(r"\b[A-ZÁÉÍÓÚÑ][\w]+(?:\s+[A-ZÁÉÍÓÚÑ][\w]+)*\b", mensaje)
        vistos: set = set()
        for q in list(secuencias_cap) + tokens:
            for ent in self.buscar_entidades(q, limit=2):
                if ent["id"] in vistos:
                    continue
                vistos.add(ent["id"])
                score = float(ent.get("confianza", 1.0)) + float(ent.get("ultima_mencion_ts", 0)) / 1e12
                candidatos.append((int(ent["id"]), score))
        # También buscar referencias indirectas tipo "mi tío" en cualquier parte del texto.
        # `resolver_referencia` solo matchea al inicio; aquí extraemos todas las menciones.
        for m in re.finditer(r"\b(?:mi|mis|el|la|los|las)\s+(\w+)", normalizar(mensaje)):
            ref_id = self.resolver_referencia(f"mi {m.group(1)}")
            if ref_id is not None and ref_id not in vistos:
                vistos.add(ref_id)
                candidatos.append((ref_id, 2.0))
        if not candidatos:
            return ""
        candidatos.sort(key=lambda x: -x[1])
        partes: List[str] = []
        for ent_id, _ in candidatos[:top_k]:
            ent = self.obtener_entidad(ent_id)
            if not ent:
                continue
            linea = f"- {ent['nombre']} ({ent['tipo']})"
            rels = self.relaciones_de(ent_id, direccion="ambas", vigentes_solo=True)[:3]
            if rels:
                bits = []
                for r in rels:
                    if r["entidad_a_id"] == ent_id:
                        bits.append(f"{r['relacion']} {r['b_nombre']}")
                    else:
                        bits.append(f"{r['a_nombre']} {r['relacion']} él/ella")
                linea += ": " + "; ".join(bits)
            # Añadir estados actuales destacados (humor, ubicacion)
            for prop in ("humor", "ubicacion", "estado"):
                est = self.estado_actual(ent_id, prop)
                if est:
                    linea += f". {prop}={est['valor']}"
            partes.append(linea)
        return "\n".join(partes)

    # ─── Estadísticas e introspección ──────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT tipo, COUNT(*) FROM kg_entidades GROUP BY tipo")
        por_tipo = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM kg_entidades")
        n_ent = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM kg_relaciones WHERE hasta_ts IS NULL")
        n_rel_vig = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM kg_relaciones")
        n_rel_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM kg_estados")
        n_est = cur.fetchone()[0]
        return {
            "entidades_total": n_ent,
            "entidades_por_tipo": por_tipo,
            "relaciones_vigentes": n_rel_vig,
            "relaciones_total": n_rel_total,
            "estados_total": n_est,
        }

    # ─── Inferencia Datalog (deducción de relaciones implícitas) ───────

    def deducir_relaciones_implicitas(
        self,
        reglas_extra=None,
        normalizar_genero: bool = True,
    ):
        """Construye un DatalogEngine con todas las relaciones vigentes + reglas
        familiares (+ las extra que pases) y devuelve la saturación completa.

        Útil para responder «¿quién es mi abuelo?» sin que esté escrito
        explícitamente: con `padre_de(ana, luis)` y `padre_de(luis, marta)`
        deduce `abuelo_de(ana, marta)`.

        Returns:
            engine (DatalogEngine): para hacer queries adicionales.
            facts_deducidos (FrozenSet[Atom]): EDB + IDB.
        """
        # Import perezoso: evita ciclo y no penaliza arranque del KG.
        from .datalog import (
            DatalogEngine, cargar_desde_grafo, reglas_familia_es,
        )
        engine = DatalogEngine()
        cargar_desde_grafo(engine, self, normalizar_genero=normalizar_genero)
        engine.add_rules(reglas_familia_es())
        if reglas_extra:
            engine.add_rules(reglas_extra)
        return engine, engine.saturar()

    def relaciones_deducidas(
        self,
        entidad,
        relaciones: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Devuelve relaciones deducidas que tienen a `entidad` como sujeto u objeto.

        Filtra por relaciones tipo `abuelo_de`, `tio_de`, `primo_de`, etc.
        Cada item: {sujeto, relacion, objeto, deducida=True}.
        """
        from .datalog import es_variable  # noqa: F401  (mantener simetría)
        nombre = entidad.strip().lower() if isinstance(entidad, str) else None
        if not nombre:
            ent = self.obtener_entidad(entidad)
            if ent is None:
                return []
            nombre = ent["nombre"].strip().lower()
        engine, saturado = self.deducir_relaciones_implicitas()
        rels_filter = set(r.lower() for r in (relaciones or []))
        out: List[Dict[str, Any]] = []
        for f in saturado:
            if rels_filter and f.predicate not in rels_filter:
                continue
            if len(f.args) != 2:
                continue
            if nombre in f.args:
                out.append({
                    "sujeto": f.args[0],
                    "relacion": f.predicate,
                    "objeto": f.args[1],
                    "deducida": f not in engine.facts,
                })
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
