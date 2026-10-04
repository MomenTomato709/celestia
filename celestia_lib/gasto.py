"""El dinero de verdad: cuánto puede gastar hoy un proveedor de pago.

Enzo (23 sep 2026): «los 20 pero al mes, nada de que se gaste antes del mes y
volver a recargar». No basta con un tope mensual —se podría fundir en tres
días—: el tope se REPARTE. Cada día puede gastar lo que queda del ciclo entre
los días que faltan, así que un día flojo deja más para los siguientes y un
día fuerte nunca se come el de mañana. Cuando se acaba lo de hoy, la cadena
sigue con los proveedores gratis.

El ciclo empieza el día del primer gasto y dura un mes natural (23 sep → 23
oct), que es cuando Enzo recarga; no el mes del calendario, que el 23 de
septiembre dejaría 20 $ para una semana.

Se cuenta siempre a precio de HORA PUNTA (el doble que el resto del día): el
contador se pasa de caro, nunca se queda corto.

Cada llamada RESERVA antes su coste máximo posible y lo ajusta al real al
volver (Codex, 23 sep 2026: sólo se miraba si quedaba cupo, no si alcanzaba).
Si la llamada se corta sin saber qué se cobró, se queda apuntado el máximo.
El contador lo comparten procesos distintos (Celestia y el examen), así que
cada lectura-y-escritura va bajo un candado entre procesos.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger("celestia_v1")

# $ por millón de tokens, hora punta (api-docs.deepseek.com, 23 sep 2026).
PRECIOS_DEEPSEEK: Dict[str, Dict[str, float]] = {
    "deepseek-flash":  {"cache": 0.006, "entrada": 0.30, "salida": 1.20},
    "deepseek-v4-pro": {"cache": 0.044, "entrada": 1.32, "salida": 3.96},
}


def coste_uso(uso: dict, modelo: str) -> float:
    """Dólares de una respuesta según su `usage`. Un modelo sin precio conocido
    se cobra como el más caro: mejor pasarse que quedarse corto."""
    p = PRECIOS_DEEPSEEK.get(modelo) or max(PRECIOS_DEEPSEEK.values(), key=lambda x: x["salida"])
    uso = uso or {}
    cache = int(uso.get("prompt_cache_hit_tokens") or 0)
    entrada = int(uso.get("prompt_tokens") or 0)
    nueva = int(uso.get("prompt_cache_miss_tokens") or max(entrada - cache, 0))
    # La salida ya incluye el razonamiento (completion_tokens_details.reasoning_tokens).
    salida = int(uso.get("completion_tokens") or 0)
    return (cache * p["cache"] + nueva * p["entrada"] + salida * p["salida"]) / 1e6


# La salida se acota SIEMPRE: sin `max_tokens`, DeepSeek escribe hasta
# 393.216 tokens (≈0,47 $ de una sola respuesta).
MAX_TOKENS_SALIDA = 8192


def acotar_max_tokens(max_tokens) -> int:
    try:
        n = int(max_tokens or 0)
    except (TypeError, ValueError):
        n = 0
    return min(n, MAX_TOKENS_SALIDA) if n > 0 else 2048


def coste_maximo(payload: bytes, max_tokens: int, modelo: str) -> float:
    """Lo MÁS que puede costar una petición antes de mandarla. Ningún token
    ocupa menos de un byte, así que un token de entrada sin caché por cada
    byte del cuerpo es un techo de verdad (en español salen ~4 bytes por
    token: se reserva de más y se devuelve al liquidar). Y `max_tokens`
    enteros de salida, razonamiento incluido."""
    p = PRECIOS_DEEPSEEK.get(modelo) or max(PRECIOS_DEEPSEEK.values(), key=lambda x: x["salida"])
    return (len(payload) * p["entrada"] + acotar_max_tokens(max_tokens) * p["salida"]) / 1e6


def _mas_un_mes(d: date) -> date:
    """El mismo día del mes siguiente (el 31 de enero pasa al 28/29 de febrero)."""
    a, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(a, m, min(d.day, calendar.monthrange(a, m)[1]))


class PresupuestoMensual:
    """Tope mensual repartido por días. `ruta=None` = sólo en memoria (tests)."""

    # Candado entre procesos: un directorio, porque `mkdir` es atómico y
    # `flock` no existe en este PRoot (Errno 38). Se tiene milisegundos —nunca
    # durante la llamada de red—, pero con el swap lleno una lectura de /sdcard
    # se ha visto tardar minutos: sólo se da por muerto a los 10 minutos
    # (Codex, 23 sep 2026). Mientras, DeepSeek no se usa: cuesta respuestas
    # de los gratis, nunca dinero. Y al soltarlo se comprueba que sigue siendo
    # nuestro, para no abrirle la puerta a nadie.
    CANDADO_ESPERA_S = 3.0
    CANDADO_CADUCA_S = 600.0

    def __init__(self, tope_mes: float, ruta: Optional[Path] = None,
                 hoy: Callable[[], date] = lambda: datetime.now().date()):
        self.tope_mes = float(tope_mes)
        self.ruta = ruta
        self._hoy = hoy
        self._lock = threading.Lock()
        self._memoria: Optional[dict] = None

    # ── Estado en disco ────────────────────────────────────────────────
    def _leer(self) -> Optional[dict]:
        """El estado, `{}` si aún no hay ninguno, o None si NO se sabe.

        None apaga el gasto: un fichero ilegible (el Errno 38 de este PRoot)
        tratado como «vacío» pondría el contador a cero y abriría el grifo.
        """
        if self.ruta is None:
            return dict(self._memoria or {})
        try:
            with open(self.ruta, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("Presupuesto: no puedo leer %s (%s) — no gasto a ciegas", self.ruta, e)
            return None

    def _escribir(self, estado: dict) -> None:
        if self.ruta is None:
            self._memoria = dict(estado)
            return
        tmp = self.ruta.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False)
        os.replace(tmp, self.ruta)

    def _al_dia(self, estado: dict) -> dict:
        """Abre ciclo nuevo si toca y separa lo de hoy de lo de antes."""
        hoy = self._hoy()
        inicio = estado.get("inicio_ciclo")
        if not inicio or hoy >= _mas_un_mes(date.fromisoformat(inicio)):
            # Ciclos de un mes contados desde el primero, sin saltarse ninguno.
            ini = date.fromisoformat(inicio) if inicio else hoy
            while hoy >= _mas_un_mes(ini):
                ini = _mas_un_mes(ini)
            estado = {"inicio_ciclo": ini.isoformat(), "gastado_ciclo": 0.0,
                      "dia": hoy.isoformat(), "gastado_hoy": 0.0}
        if estado.get("dia") != hoy.isoformat():
            estado["dia"] = hoy.isoformat()
            estado["gastado_hoy"] = 0.0
        return estado

    @contextmanager
    def _candado(self):
        """Da True con el candado puesto, o False si no se consigue. Con dinero
        no hay «seguir sin candado»: sin él, no se gasta."""
        with self._lock:
            if self.ruta is None:
                yield True
                return
            dir_candado = self.ruta.with_suffix(".candado")
            limite = time.time() + self.CANDADO_ESPERA_S
            tengo = False
            mio = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
            while True:
                try:
                    os.mkdir(dir_candado)
                    try:
                        (dir_candado / mio).touch()
                    except OSError:
                        os.rmdir(dir_candado)       # no dejarlo huérfano
                        raise
                    tengo = True
                    break
                except FileExistsError:
                    try:
                        if time.time() - os.stat(dir_candado).st_mtime > self.CANDADO_CADUCA_S:
                            logger.warning("Presupuesto: retiro un candado abandonado")
                            for marca in dir_candado.iterdir():
                                marca.unlink()
                            os.rmdir(dir_candado)
                            continue
                    except OSError:
                        pass
                except OSError as e:
                    logger.warning("Presupuesto: no puedo poner el candado (%s) — no gasto", e)
                    break
                if time.time() >= limite:
                    logger.warning("Presupuesto: candado ocupado %.0f s — no gasto", self.CANDADO_ESPERA_S)
                    break
                time.sleep(0.02)
            try:
                yield tengo
            finally:
                if tengo:
                    try:
                        # Si nos lo retiraron por viejo, ya no es nuestro.
                        (dir_candado / mio).unlink()
                        os.rmdir(dir_candado)
                    except OSError:
                        pass

    # ── Consultas ──────────────────────────────────────────────────────
    def cupo_hoy(self, estado: dict) -> float:
        """Lo que puede gastar hoy: lo que queda del ciclo (sin contar lo de
        hoy) entre los días que faltan, hoy incluido."""
        hoy = self._hoy()
        fin = _mas_un_mes(date.fromisoformat(estado["inicio_ciclo"]))
        dias = max((fin - hoy).days, 1)
        antes = estado["gastado_ciclo"] - estado["gastado_hoy"]
        return max(self.tope_mes - antes, 0.0) / dias

    def puede_gastar(self) -> bool:
        """Mirada rápida (sin candado) para saber si merece la pena intentarlo.
        Lo que de verdad autoriza una llamada es `reservar`."""
        if self.tope_mes <= 0:
            return False
        with self._lock:
            estado = self._leer()
            if estado is None:
                return False
            estado = self._al_dia(estado)
            return (estado["gastado_hoy"] < self.cupo_hoy(estado)
                    and estado["gastado_ciclo"] < self.tope_mes)

    def reservar(self, maximo: float, adelanto: float = 0.0) -> bool:
        """Aparta `maximo` $ si caben en el cupo de hoy y en el del ciclo.

        `adelanto`: lo que se puede tomar HOY de los días que vienen, cuando
        Enzo lo autoriza para un trabajo concreto (24 sep: «si todo funciona da
        igual gastar un poco más del límite diario»). El tope del mes no se
        adelanta nunca: sólo se reparte distinto."""
        if self.tope_mes <= 0:
            return False
        with self._candado() as tengo:
            if not tengo:
                return False
            estado = self._leer()
            if estado is None:
                return False
            estado = self._al_dia(estado)
            if (estado["gastado_hoy"] + maximo > self.cupo_hoy(estado) + max(adelanto, 0.0)
                    or estado["gastado_ciclo"] + maximo > self.tope_mes):
                return False
            return self._sumar(estado, maximo)

    def liquidar(self, reservado: float, real: float) -> None:
        """Cambia lo reservado por lo que costó de verdad. Si no se puede,
        se queda la reserva, que es el máximo: el error va hacia el lado caro."""
        if real > reservado + 1e-9:
            # No debería pasar (la reserva es un techo). Si pasa, el cálculo del
            # techo está mal y no hay que fiarse de él: se apaga hasta reiniciar.
            logger.warning("Presupuesto: costó %.4f $ y se reservaron %.4f $ — apago el gasto",
                           real, reservado)
            self.tope_mes = 0.0
        with self._candado() as tengo:
            if not tengo:
                return
            estado = self._leer()
            if estado is not None:
                self._sumar(self._al_dia(estado), real - reservado)

    def _sumar(self, estado: dict, dolares: float) -> bool:
        estado["gastado_ciclo"] = round(max(estado["gastado_ciclo"] + dolares, 0.0), 6)
        estado["gastado_hoy"] = round(max(estado["gastado_hoy"] + dolares, 0.0), 6)
        try:
            self._escribir(estado)
            return True
        except Exception as e:
            logger.warning("Presupuesto: no pude apuntar %.4f $ (%s) — apago el gasto", dolares, e)
            self.tope_mes = 0.0
            return False

    def apuntar(self, dolares: float) -> None:
        """Suma un gasto. Si no se puede guardar, se apaga el gasto en memoria:
        un gasto que no queda apuntado es un gasto que se repetirá sin límite."""
        if dolares <= 0:
            return
        with self._candado() as tengo:
            estado = self._leer() if tengo else None
            if estado is None:
                self.tope_mes = 0.0
                return
            self._sumar(self._al_dia(estado), dolares)

    def resumen(self) -> dict:
        """Para contárselo a Enzo: gastado, cupo de hoy y cuándo se renueva."""
        with self._lock:
            estado = self._leer()
            if estado is None:
                return {"legible": False}
            estado = self._al_dia(estado)
            return {
                "legible": True,
                "tope_mes": self.tope_mes,
                "gastado_ciclo": round(estado["gastado_ciclo"], 4),
                "gastado_hoy": round(estado["gastado_hoy"], 4),
                "cupo_hoy": round(self.cupo_hoy(estado), 4),
                "renueva": _mas_un_mes(date.fromisoformat(estado["inicio_ciclo"])).isoformat(),
            }
