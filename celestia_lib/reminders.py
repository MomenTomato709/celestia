"""Recordatorios con disparo automático (Fase 2 del roadmap).

Extraído del monolito en sesión 15.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tz import from_ts_usuario

logger = logging.getLogger("celestia_v1")


def parse_reminder_time(s: str) -> Optional[float]:
    """Convierte expresión de tiempo natural a segundos desde ahora.

    Acepta: 'en 30 seg', 'en 5 min', 'en 1 hora y 20 min', 'a las 8:30',
    'a las 8 de la tarde'. Devuelve None si no matchea.
    """
    s = s.lower().strip()
    m = re.match(r"en\s+(\d+)\s*seg(?:undo)?s?", s)
    if m:
        return float(m.group(1))
    m = re.match(r"en\s+(\d+)\s*min(?:uto)?s?", s)
    if m:
        return float(m.group(1)) * 60
    m = re.match(r"en\s+(?:(\d+)\s*hora?s?\s*(?:y\s*)?)?(\d+)?\s*min(?:uto)?s?", s)
    if m and (m.group(1) or m.group(2)):
        return (float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60)
    m = re.match(r"en\s+(\d+)\s*hora?s?", s)
    if m:
        return float(m.group(1)) * 3600
    m = re.match(r"a\s+las?\s+(\d{1,2})(?::(\d{2}))?(?:\s+de\s+la\s+(ma[ñn]ana|tarde|noche))?", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        periodo = m.group(3) or ""
        if "tarde" in periodo or "noche" in periodo:
            if h < 12:
                h += 12
        now = datetime.now()
        target = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=target.day + 1)
        return (target - now).total_seconds()
    return None


class ReminderManager:
    """Gestiona recordatorios con disparo automático por terminal y bridge.

    Con `archivo`, los pendientes se guardan en disco: hasta el 4 oct 2026
    vivían sólo en memoria y un reinicio (o apagar el PC) se los llevaba. Al
    volver a arrancar, los que vencieron mientras estaba apagada salen ya.
    """

    def __init__(self, archivo: Optional[Path] = None):
        self._reminders: List[Dict] = []
        self._lock = threading.Lock()
        self._bridge = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="reminder-checker")
        self._archivo = Path(archivo) if archivo else None
        if self._archivo:
            self._cargar()

    def _cargar(self) -> None:
        try:
            datos = json.loads(self._archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for r in datos if isinstance(datos, list) else []:
            if isinstance(r, dict) and isinstance(r.get("at"), (int, float)) and r.get("msg"):
                self._reminders.append({"at": float(r["at"]), "msg": str(r["msg"]),
                                        "retries": int(r.get("retries") or 0),
                                        "canal": str(r.get("canal") or "")})
        if self._reminders:
            logger.info("Recordatorios pendientes recuperados: %d", len(self._reminders))

    def _guardar(self) -> None:
        """Escribe los pendientes (llamar con el lock cogido)."""
        if not self._archivo:
            return
        try:
            self._archivo.parent.mkdir(parents=True, exist_ok=True)
            temporal = self._archivo.with_suffix(".tmp")
            temporal.write_text(json.dumps(self._reminders, ensure_ascii=False), encoding="utf-8")
            os.replace(temporal, self._archivo)
        except OSError as e:
            logger.warning("No pude guardar los recordatorios: %s", e)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_bridge(self, bridge: Any) -> None:
        self._bridge = bridge

    # Sesión 30 (AW): número máximo de reintentos si el bridge falla al
    # disparar. Tras MAX_RETRIES se descarta (y se loggea WARNING). Antes
    # bastaba con que `bridge.send` lanzara una excepción para perder el
    # recordatorio silenciosamente — el usuario no se enteraba.
    MAX_DELIVERY_RETRIES = 5
    RETRY_BACKOFF_SECONDS = 30  # re-intentar tras 30s, 60s, 90s, ...

    def add(self, seconds: float, mensaje: str) -> str:
        at = time.time() + seconds
        # Por dónde se pidió: el aviso vuelve por ahí (el chat, WhatsApp…).
        try:
            from .bandeja import CANAL_PETICION
            canal = CANAL_PETICION.get() or ""
        except Exception:
            canal = ""
        with self._lock:
            self._reminders.append({"at": at, "msg": mensaje, "retries": 0, "canal": canal})
            self._guardar()
        # Sesión 30 bug AU: server Termux a veces en UTC; formatear en TZ del
        # usuario (Europe/Madrid por defecto) para que vea hora coherente.
        dt_obj = from_ts_usuario(at)
        return f"✅ Recordatorio programado para {self._fmt_when(dt_obj)}: '{mensaje}'"

    def list_pending(self) -> str:
        with self._lock:
            pending = list(self._reminders)
        if not pending:
            return "No hay recordatorios pendientes."
        lines = [
            f"  ⏰ {self._fmt_when(from_ts_usuario(r['at']))} — {r['msg']}"
            for r in pending
        ]
        return "\n".join(lines)

    # Sesión 33 (B33-10): formatear fecha relativa («hoy», «mañana», «el
    # 15 de junio») junto a la hora cuando NO es hoy. Antes solo decía
    # «10:00» y el usuario no sabía qué día.
    @staticmethod
    def _fmt_when(dt_obj) -> str:
        from datetime import datetime, timedelta
        try:
            ahora = datetime.now(dt_obj.tzinfo) if dt_obj.tzinfo else datetime.now()
        except Exception:
            ahora = datetime.now()
        hoy = ahora.date()
        objetivo = dt_obj.date()
        hora = dt_obj.strftime("%H:%M")
        if objetivo == hoy:
            return f"las {hora}"
        if objetivo == hoy + timedelta(days=1):
            return f"mañana a las {hora}"
        if objetivo == hoy - timedelta(days=1):
            return f"ayer a las {hora}"
        # Más lejos en el tiempo: fecha completa.
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                 "julio", "agosto", "septiembre", "octubre", "noviembre",
                 "diciembre"]
        fecha = f"el {dt_obj.day} de {meses[dt_obj.month-1]}"
        if dt_obj.year != hoy.year:
            fecha += f" de {dt_obj.year}"
        return f"{fecha} a las {hora}"

    # Sesión 31 (BUG-BD): stopwords mínimas para que «cancela el recordatorio
    # DE beber agua» no exija que el mensaje contenga "de"/"el".
    _STOPWORDS_KW = frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas",
        "de", "del", "al", "a", "en", "con", "para", "por",
        "y", "o", "u", "me", "te", "se", "lo", "le",
        "que", "mi", "mis", "tu", "tus",
    })

    @classmethod
    def _tokens_significativos(cls, texto: str) -> list:
        import re as _re
        return [t for t in _re.findall(r"\b[\wáéíóúñü]+\b", (texto or "").lower())
                if t not in cls._STOPWORDS_KW and len(t) >= 3]

    # Sufijos verbales/nominales del español. Se eliminan al final si dejan
    # un stem de ≥3 caracteres. Orden: más largos primero (greedy match).
    _SUFIJOS_VERB = (
        "ándome", "iéndome", "ándote", "iéndote",
        "aremos", "eremos", "iremos", "arían", "erían", "irían",
        "asteis", "isteis", "abais", "íamos", "iamos",
        "amos", "emos", "imos", "arás", "erás", "irás",
        "ará", "erá", "irá", "aría", "ería", "iría",
        "ando", "endo", "iendo", "ado", "ada", "ido", "ida",
        "aste", "iste", "ándo", "iéndo",
        "arán", "erán", "irán", "asen", "iesen",
        "aban", "ían", "ían",
        "ar", "er", "ir", "as", "es", "an", "en", "ad", "ed", "id",
        "ará", "erá", "irá",
        "ó", "é", "í", "á", "a", "e", "o",
    )

    @classmethod
    def _stem_es(cls, palabra: str) -> str:
        """Mini-stemmer ES: quita sufijos comunes y revierte diptongos
        irregulares (ue→o, ie→e) para que duermo→dorm y cuenta→cont
        empaten con dormir y contar."""
        w = palabra.lower()
        for suf in cls._SUFIJOS_VERB:
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[:-len(suf)]
                break
        # Revertir diptongos típicos: c-uenta → c-onta, d-uerm → d-orm,
        # c-ierra → c-erra, s-iente → s-ente. Sólo si quedan ≥3 chars.
        if len(w) >= 3:
            w = w.replace("ue", "o", 1).replace("ie", "e", 1)
        # Quitar tildes
        for a, b in (("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u")):
            w = w.replace(a, b)
        return w

    @classmethod
    def _coinciden_aprox(cls, palabra_kw: str, palabra_msg: str) -> bool:
        """True si dos palabras son la misma raíz aproximada (beber/beba/bebe).
        Combina tres heurísticas: stemming ES, prefijo común y SequenceMatcher."""
        a, b = palabra_kw.lower(), palabra_msg.lower()
        if a == b:
            return True
        # Heurística 1: stemming español manual (cubre irregulares dormir/duermo
        # y regulares beber/beba).
        sa, sb = cls._stem_es(a), cls._stem_es(b)
        if sa == sb and len(sa) >= 3:
            return True
        if len(sa) >= 4 and len(sb) >= 4 and (sa.startswith(sb) or sb.startswith(sa)):
            return True
        # Heurística 2: prefijo en originales (palabras cortas, no verbos).
        umbral = 3 if min(len(a), len(b)) <= 5 else 4
        if len(a) >= umbral and len(b) >= umbral and a[:umbral] == b[:umbral]:
            return True
        # Heurística 3: SequenceMatcher como fallback (umbral conservador para
        # evitar falsos positivos tipo agua vs apaga).
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, a, b).ratio()
        return ratio >= 0.72

    @classmethod
    def _kw_match(cls, kw: str, mensaje: str) -> bool:
        """True si CADA token significativo del kw aparece (aprox) en el mensaje.
        Match exacto sigue funcionando: si la frase entera coincide, OK."""
        kw_low = kw.lower().strip()
        msg_low = mensaje.lower()
        if kw_low in msg_low:
            return True
        kw_tokens = cls._tokens_significativos(kw)
        msg_tokens = cls._tokens_significativos(mensaje)
        if not kw_tokens or not msg_tokens:
            return False
        for kt in kw_tokens:
            if not any(cls._coinciden_aprox(kt, mt) for mt in msg_tokens):
                return False
        return True

    def remove_by_keyword(self, keyword: str) -> str:
        """Elimina recordatorio(s) cuyo mensaje contenga `keyword` (case-insensitive).
        Keywords especiales: "todo"/"todos"/"todas" → borra TODOS los recordatorios.

        Sesión 31 (BUG-BD): comparación tolerante a variantes verbales — «beber»
        matchea «beba», «despertar» matchea «despiértame», etc. Antes se exigía
        substring exacto, lo que rompía con verbos conjugados.
        """
        kw = (keyword or "").strip().lower()
        if not kw:
            return "Dime qué recordatorio quieres olvidar."
        # Sesión 29: "olvida todo" debe borrar todos, no buscar literal "todo".
        # Sesión 31 (BUG-N): aceptar también "los recordatorios" / "recordatorios"
        # sin "todos", "los pendientes", etc.
        borrar_todo = kw in {
            "todo", "todos", "todas", "todos los recordatorios",
            "todos mis recordatorios", "mis recordatorios",
            "los recordatorios", "recordatorios", "las alarmas", "alarmas",
            "los pendientes", "pendientes",
        }
        # Sesión 31 (BUG-L): ordinales. "el primero / la segunda / el N" → borrar
        # el recordatorio de esa posición de la lista (ordenada por hora).
        _ORDINALES = {
            "primero": 1, "primera": 1, "primer": 1, "1": 1, "uno": 1,
            "segundo": 2, "segunda": 2, "2": 2, "dos": 2,
            "tercero": 3, "tercera": 3, "tercer": 3, "3": 3, "tres": 3,
            "cuarto": 4, "cuarta": 4, "4": 4, "cuatro": 4,
            "quinto": 5, "quinta": 5, "5": 5, "cinco": 5,
        }
        _ULTIMO = {"último", "última", "ultimo", "ultima"}
        pos_ordinal: Optional[int] = None
        # Sesión 37 (BUG-S10): «cancela el primer recordatorio» llegaba como
        # keyword 'primer recordatorio' y se buscaba literal (no como ordinal).
        # Normalizar: quitar artículos/posesivos y el sustantivo recordatorio/
        # alarma/aviso; aceptar apócopes (primer/tercer) y «último».
        kw_norm = kw
        for _pre in ("el ", "la ", "los ", "las ", "mi ", "mis "):
            kw_norm = kw_norm.replace(_pre, "")
        kw_norm = re.sub(r"\b(recordatorios?|alarmas?|avisos?|pendientes?)\b", "", kw_norm).strip()
        if kw_norm in _ORDINALES:
            pos_ordinal = _ORDINALES[kw_norm]
        elif kw_norm in _ULTIMO:
            pos_ordinal = -1  # marcador: último de la lista
        with self._lock:
            antes = len(self._reminders)
            # Sesión 31 (BUG-S20): si pedimos borrado total con lista vacía,
            # mensaje claro en lugar de "no tengo ningún recordatorio que
            # contenga 'todos'".
            if borrar_todo and antes == 0:
                return "No hay recordatorios pendientes que borrar."
            if borrar_todo:
                quitados = list(self._reminders)
                self._reminders = []
            elif pos_ordinal is not None:
                # Ordenar por hora ascendente para que "primero" = más próximo.
                ordenados = sorted(self._reminders, key=lambda r: r["at"])
                if not ordenados:
                    return "No hay recordatorios pendientes que borrar."
                if pos_ordinal == -1:
                    target = ordenados[-1]  # último
                elif pos_ordinal > len(ordenados):
                    return (f"Sólo tienes {len(ordenados)} recordatorio(s) "
                            f"pendiente(s); no hay un {kw_norm}.")
                else:
                    target = ordenados[pos_ordinal - 1]
                quitados = [target]
                self._reminders = [r for r in self._reminders if r is not target]
            else:
                quitados = [r for r in self._reminders if self._kw_match(kw, r["msg"])]
                self._reminders = [r for r in self._reminders if not self._kw_match(kw, r["msg"])]
            despues = len(self._reminders)
            if despues != antes:
                self._guardar()
        n = antes - despues
        if n == 0:
            return f"No tengo ningún recordatorio que contenga '{keyword}'."
        if borrar_todo:
            # Sesión 31 (BUG-S21): concordancia singular/plural.
            if n == 1:
                return "✓ Borré el recordatorio pendiente."
            return f"✓ Borré los {n} recordatorios pendientes."
        nombres = ", ".join(f"'{r['msg']}'" for r in quitados)
        return f"✓ Borré {n} recordatorio(s): {nombres}."

    def _run(self) -> None:
        while not self._stop.wait(20):
            self._check()

    def _check(self) -> None:
        """Dispara recordatorios cuya hora pasó. Si bridge.send falla, NO
        descarta: reencola con retries++ y backoff progresivo. Sólo descarta
        tras MAX_DELIVERY_RETRIES — antes (sesión <30) un fallo silencioso
        del bridge perdía el recordatorio sin que el usuario se enterara.
        """
        now = time.time()
        with self._lock:
            remaining, fired_items = [], []
            for r in self._reminders:
                (fired_items if r["at"] <= now else remaining).append(r)
            self._reminders = remaining
            if fired_items:
                self._guardar()

        # Reencolamos aquí (fuera del lock) tras intentar entregar.
        to_requeue: List[Dict] = []
        for r in fired_items:
            msg = r["msg"]
            print(f"\n  ⏰ RECORDATORIO: {msg}\nTú: ", end="", flush=True)
            delivered = True
            if self._bridge:
                try:
                    # Si el puente sabe de canales, el aviso vuelve por donde
                    # se pidió el recordatorio.
                    envio_a = getattr(self._bridge, "send_a", None)
                    if envio_a:
                        envio_a(f"⏰ {msg}", "Recordatorio — Celestia", r.get("canal", ""))
                    else:
                        self._bridge.send(f"⏰ {msg}",
                                           title="Recordatorio — Celestia")
                except Exception as e:
                    delivered = False
                    retries = r.get("retries", 0) + 1
                    if retries >= self.MAX_DELIVERY_RETRIES:
                        logger.warning(
                            "Recordatorio %r descartado tras %d reintentos: %s",
                            msg, retries, e)
                    else:
                        logger.warning(
                            "Bridge.send falló (intento %d/%d) para %r — "
                            "reencolando con backoff: %s",
                            retries, self.MAX_DELIVERY_RETRIES, msg, e)
                        # Reencolar con next attempt en backoff progresivo.
                        to_requeue.append({
                            "at": now + self.RETRY_BACKOFF_SECONDS * retries,
                            "msg": msg,
                            "retries": retries,
                            "canal": r.get("canal", ""),
                        })
            if delivered:
                logger.info("Recordatorio entregado: %r", msg)

        if to_requeue:
            with self._lock:
                self._reminders.extend(to_requeue)
                self._guardar()
