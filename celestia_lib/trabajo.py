"""El cuaderno de pasos de un encargo largo: lo que pinta el panel «Trabajando».

Enzo, 5 oct 2026: «me gustaría ver cómo Celestia lo hace en tiempo real para no
aburrirme y tener un punto más crítico en cómo lo hace y ver cómo comprueba».
Diseño: docs/superpowers/specs/2026-10-05-panel-trabajando-design.md.

Cada encargo largo (un proyecto, aprender una habilidad, la obra…) abre un
cuaderno y apunta cada cosa en el momento en que pasa. El chat lo lee por
`/trabajos/<id>` y pinta el panel. Regla: aquí sólo se apunta lo que ha pasado
de verdad; el panel nunca se inventa nada.

Un JSONL por encargo, sólo se añade: si el proceso muere a mitad de una línea,
esa línea se salta al leer y lo anterior sigue valiendo.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .paths import MEM_DIR

logger = logging.getLogger("celestia_v1")

CARPETA = MEM_DIR / "trabajos"
MAX_GUARDADOS = 30
ATASCADO_S = 180.0
# Distingue este proceso de uno anterior: un cuaderno sin terminar de otro
# arranque, callado desde antes de que arrancase este, es uno que se cortó
# porque Celestia se reinició a mitad. Si otro proceso vivo lo sigue
# escribiendo (el simulador del panel), sus pasos son posteriores y no se cortó.
ARRANQUE = uuid.uuid4().hex
ARRANQUE_T = time.time()
OCULTOS = ("coste",)                       # se cuentan, no se enseñan en el chat
MAX_TEXTO = 300
MAX_DETALLE = 4000
_ID_RE = re.compile(r"^\d{14}[0-9a-f]{6}$")

_lock = threading.Lock()
_contadores: Dict[str, int] = {}
_tamanos: Dict[str, int] = {}             # lo que medía el cuaderno tras escribir aquí
_sin_cuaderno: set = set()                # ya avisados de que no se puede escribir
_paradas: set = set()
_terminados: set = set()
_actual: contextvars.ContextVar = contextvars.ContextVar("trabajo_actual", default=None)


def _corto(texto: Any, largo: int = MAX_TEXTO) -> str:
    return " ".join(str(texto or "").split())[:largo]


def _titulo(texto: Any, largo: int = 80) -> str:
    """Corto entre palabras, con «…»: cortado a mitad salía «en Valladoli»."""
    texto = _corto(texto, 10 ** 4)
    if len(texto) <= largo:
        return texto
    recorte = texto[:largo]
    if " " in recorte:
        recorte = recorte.rsplit(" ", 1)[0]
    return recorte.rstrip(" ,.;:") + "…"


def _detalle(texto: Any) -> str:
    texto = str(texto or "")
    if len(texto) <= MAX_DETALLE:
        return texto
    return texto[:MAX_DETALLE] + f"\n… (recortado: {len(texto) - MAX_DETALLE} caracteres más)"


def _ruta(id_: str) -> Path:
    return CARPETA / f"{id_}.jsonl"


def _escribir(id_: str, tipo: str, datos: Dict[str, Any]) -> None:
    """Añade un suceso con su número de orden. En este proceso todos los
    escritores pasan por aquí (el productor y la ruta de parar); si el cuaderno
    ha crecido por otro lado (otro proceso), se recuenta: un número repetido
    hace que el panel, que pide «desde=n», se salte sucesos.

    Nunca lanza: el cuaderno es para mirar, y si no se puede escribir (PRoot
    da Errno 38, OneDrive bloquea el fichero mientras sincroniza) el trabajo
    de verdad tiene que seguir igual. Se avisa una vez en el registro."""
    with _lock:
        ruta = _ruta(id_)
        try:
            try:
                tam = ruta.stat().st_size
            except OSError:
                tam = 0
            if id_ not in _contadores or _tamanos.get(id_) != tam:
                try:
                    _contadores[id_] = sum(1 for _ in open(ruta, encoding="utf-8", errors="replace"))
                except OSError:
                    _contadores[id_] = 0
            _contadores[id_] += 1
            linea = json.dumps({"n": _contadores[id_], "t": round(time.time(), 3), "tipo": tipo,
                                **datos}, ensure_ascii=False, default=str)
            with open(ruta, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
            _tamanos[id_] = ruta.stat().st_size
        except (OSError, ValueError, TypeError) as e:
            _tamanos.pop(id_, None)
            if id_ not in _sin_cuaderno:
                _sin_cuaderno.add(id_)
                logger.warning("Cuaderno %s: no se pudo apuntar «%s» (%s); el trabajo sigue",
                               id_, tipo, e)


class Trabajo:
    def __init__(self, id_: str):
        self.id = id_
        self.abierto = True

    def plan(self, pasos: List[str]) -> None:
        _escribir(self.id, "plan", {"pasos": [_corto(p, 200) for p in list(pasos)[:20]]})

    def etapa(self, indice: int) -> None:
        _escribir(self.id, "etapa", {"indice": int(indice)})

    def paso(self, texto: str, detalle: str = "") -> None:
        _escribir(self.id, "paso", {"texto": _corto(texto), "detalle": _detalle(detalle)})

    def archivo(self, ruta: str, lineas: Optional[int] = None) -> None:
        _escribir(self.id, "archivo", {"ruta": _corto(ruta, 200), "lineas": lineas})

    def comprobacion(self, que: str, ok: Optional[bool], motivo: str = "",
                     detalle: str = "") -> None:
        _escribir(self.id, "comprobacion", {"que": _corto(que), "ok": ok,
                                            "motivo": _corto(motivo), "detalle": _detalle(detalle)})

    def correccion(self, texto: str, detalle: str = "") -> None:
        _escribir(self.id, "correccion", {"texto": _corto(texto), "detalle": _detalle(detalle)})

    def vista_previa(self, ruta: str) -> None:
        # Sólo una ruta relativa que no sale de la carpeta: el navegador
        # normaliza «vista/../../x» antes de pedirlo, y el iframe acabaría
        # cargando otra ruta de Celestia elegida por el modelo.
        ruta = str(ruta or "").replace("\\", "/")
        partes = ruta.split("/")
        if not ruta or ruta.startswith("/") or ":" in partes[0] or ".." in partes:
            return
        _escribir(self.id, "vista_previa", {"ruta": ruta[:200]})

    def fijar_carpeta(self, carpeta: Path) -> None:
        _escribir(self.id, "carpeta", {"carpeta": str(carpeta)})

    def coste(self, dolares: float) -> None:
        if dolares and dolares > 0:
            _escribir(self.id, "coste", {"dolares": round(float(dolares), 6)})

    def terminar(self, resultado: str, ok: bool = True, parado: bool = False) -> None:
        """`parado` lo dice quien trabaja cuando de verdad se ha detenido: que
        se pulsara «Parar» no basta (lo que acaba igual no está «parado»)."""
        if self.id in _terminados:
            return
        estado = "parado" if parado else ("terminado" if ok else "error")
        _escribir(self.id, "fin", {"estado": estado, "resultado": _corto(resultado)})
        _terminados.add(self.id)
        _paradas.discard(self.id)

    def parar_pedido(self) -> bool:
        return _parada_pedida(self.id)


def _parada_pedida(id_: str) -> bool:
    if id_ in _paradas:
        return True
    # Pedida en otro proceso (el servidor, si quien trabaja va aparte, como el
    # simulador del panel): sólo se ve en el cuaderno.
    if any(e.get("tipo") == "parar_pedido" for e in _sucesos(id_)):
        _paradas.add(id_)
        return True
    return False


class _Nulo:
    """El «encargo» cuando no hay ninguno: todo se acepta y nada se apunta."""
    id = ""
    abierto = False

    def __getattr__(self, _nombre):
        return lambda *a, **k: None

    def parar_pedido(self) -> bool:
        return False


NULO = _Nulo()


class Perezoso:
    """Abre el cuaderno de verdad sólo si alguien apunta algo (el empeño: si
    no usa ninguna herramienta, no hay panel)."""

    def __init__(self, titulo: str, hilo: str = ""):
        self._titulo, self._hilo = titulo, hilo
        self._real: Optional[Trabajo] = None
        self._coste = 0.0

    @property
    def abierto(self) -> bool:
        return self._real is not None

    @property
    def id(self) -> str:
        return self._real.id if self._real else ""

    def _t(self) -> Trabajo:
        if self._real is None:
            self._real = abrir(self._titulo, self._hilo)
            if self._coste:
                self._real.coste(self._coste)
                self._coste = 0.0
        return self._real

    def coste(self, dolares: float) -> None:
        """El coste no abre el panel: elegir herramienta también se paga, y una
        tarjeta vacía por cada mensaje que pasa por el empeño no es trabajo.
        Se guarda y se apunta si el cuaderno llega a abrirse."""
        if self._real is not None:
            self._real.coste(dolares)
        elif dolares and dolares > 0:
            self._coste += float(dolares)

    def parar_pedido(self) -> bool:
        return bool(self._real and self._real.parar_pedido())

    def terminar(self, resultado: str, ok: bool = True, parado: bool = False) -> None:
        if self._real:
            self._real.terminar(resultado, ok, parado=parado)

    def __getattr__(self, nombre):
        return getattr(self._t(), nombre)


def _podar() -> None:
    try:
        cuadernos = sorted(CARPETA.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return                                # otro proceso podó a la vez
    for viejo in cuadernos[:-MAX_GUARDADOS] if len(cuadernos) > MAX_GUARDADOS else []:
        try:
            viejo.unlink()
        except OSError:
            pass


def abrir(titulo: str, hilo: str = "", carpeta: Optional[Path] = None) -> Trabajo:
    try:
        CARPETA.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass                  # PRoot: Errno 38 aunque exista; _escribir avisa si de verdad falla
    id_ = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
    # El hilo puede no ser texto (un MagicMock en las pruebas, None en Termux).
    _escribir(id_, "inicio", {"titulo": _titulo(titulo),
                              "hilo": hilo[:64] if isinstance(hilo, str) else "",
                              "carpeta": str(carpeta) if carpeta else "",
                              "arranque": ARRANQUE, "pid": os.getpid()})
    _podar()
    return Trabajo(id_)


def actual():
    return _actual.get() or NULO


@contextlib.contextmanager
def en(t):
    token = _actual.set(t)
    try:
        yield t
    finally:
        _actual.reset(token)


# La app de Android (celestia_movil/vigilia.py) no deja que el móvil se duerma
# mientras hay encargos en marcha: con la pantalla apagada Android para la CPU, y un
# proyecto de diez minutos se quedaba a medias hasta volver a encenderla. Se le dice
# cuántos hay en marcha cada vez que cambia.
AL_CAMBIAR_EN_MARCHA: Optional[Callable[[int], None]] = None
_en_marcha = 0
_en_marcha_lock = threading.Lock()


def _contar_en_marcha(cambio: int) -> None:
    global _en_marcha
    with _en_marcha_lock:
        _en_marcha = max(0, _en_marcha + cambio)
        n = _en_marcha
    gancho = AL_CAMBIAR_EN_MARCHA
    if gancho is not None:
        try:
            gancho(n)
        except Exception as e:                # un fallo de la app no para el encargo
            logger.debug("Aviso de encargos en marcha: %s", e)


def lanzar(fn: Callable, *args, titulo: str, hilo: str = "", nombre_hilo: str = "trabajo",
           **kwargs) -> Trabajo:
    """Abre un cuaderno y corre `fn` en segundo plano con él como `actual()`.
    Si `fn` no lo termina, lo termina esto: nunca se queda «trabajando»."""
    t = abrir(titulo, hilo)

    def correr():
        _contar_en_marcha(+1)
        try:
            with en(t):
                try:
                    resultado = fn(*args, **kwargs)
                except Exception as e:
                    t.terminar(f"Falló: {e}", ok=False)
                    # Al registro, no hacia arriba: en un hilo suelto, relanzarla
                    # sólo vuelca la traza por la consola y nadie la recoge.
                    logger.exception("Encargo %s: falló", t.id)
                    return
                t.terminar(resultado if isinstance(resultado, str) and resultado else "Hecho")
        finally:
            _contar_en_marcha(-1)

    threading.Thread(target=correr, daemon=True, name=nombre_hilo).start()
    return t


def _sucesos(id_: str) -> List[Dict[str, Any]]:
    if not _ID_RE.match(id_ or ""):
        return []
    try:
        texto = _ruta(id_).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    sucesos = []
    for linea in texto.splitlines():
        try:
            sucesos.append(json.loads(linea))
        except ValueError:
            continue                          # una línea a medio escribir
    return sucesos


def leer(id_: str, desde: int = 0, con_ocultos: bool = False) -> Optional[Dict[str, Any]]:
    sucesos = _sucesos(id_)
    if not sucesos or sucesos[0].get("tipo") != "inicio":
        return None
    ini = sucesos[0]
    fin = next((e for e in reversed(sucesos) if e.get("tipo") == "fin"), None)
    if fin:
        estado = fin.get("estado", "terminado")
    elif ini.get("arranque") != ARRANQUE and sucesos[-1].get("t", 0) < ARRANQUE_T:
        estado = "interrumpido"
    elif time.time() - sucesos[-1].get("t", 0) > ATASCADO_S:
        estado = "atascado"
    else:
        estado = "trabajando"
    datos = {
        "id": id_, "titulo": ini.get("titulo", ""), "hilo": ini.get("hilo", ""),
        "inicio": ini.get("t", 0), "estado": estado,
        "segundos": round((fin["t"] if fin else time.time()) - ini.get("t", 0), 1),
        "ultimo": sucesos[-1].get("n", 0),
        "eventos": [e for e in sucesos if e.get("n", 0) > desde
                    and (con_ocultos or e.get("tipo") not in OCULTOS)],
    }
    if con_ocultos:
        datos["coste"] = round(sum(e.get("dolares", 0) for e in sucesos if e.get("tipo") == "coste"), 6)
    return datos


def recientes(hilo: Optional[str] = None, limite: int = 10) -> List[Dict[str, Any]]:
    try:
        cuadernos = sorted(CARPETA.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    salida = []
    for p in cuadernos:
        d = leer(p.stem, desde=10 ** 9)
        if d is None or (hilo and d["hilo"] != hilo):
            continue
        d.pop("eventos")
        salida.append(d)
        if len(salida) >= limite:
            break
    return salida


def pedir_parada(id_: str) -> bool:
    d = leer(id_, desde=10 ** 9)
    if d is None or d["estado"] not in ("trabajando", "atascado"):
        return False
    _paradas.add(id_)
    _escribir(id_, "parar_pedido", {})
    return True


def se_puede_previsualizar(contenido: str) -> bool:
    """Una página que el navegador abre tal cual: HTML completo y sin plantilla
    de servidor (Jinja, Django, Handlebars…), que en la vista saldría en crudo."""
    bajo = contenido.lower()
    return ("<html" in bajo or "<!doctype" in bajo) and "{%" not in contenido and "{{" not in contenido


ACTUALIZAN_LA_VISTA = (".html", ".htm", ".css", ".js")


def archivo_de_vista(id_: str, ruta: str) -> Optional[Path]:
    """Un archivo de la carpeta del encargo, o None. Nunca nada de fuera."""
    carpeta = ""
    for e in _sucesos(id_):
        if e.get("tipo") in ("inicio", "carpeta") and e.get("carpeta"):
            carpeta = e["carpeta"]
    if not carpeta or not ruta or "\\" in ruta:
        return None
    base = Path(carpeta).resolve()
    try:
        destino = (base / ruta).resolve()
    except (OSError, ValueError):
        return None
    if not destino.is_relative_to(base) or not destino.is_file():
        return None
    return destino
