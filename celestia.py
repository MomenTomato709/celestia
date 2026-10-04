#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Celestia — Agente Autónomo
Base técnica perfecta: generación, memoria, métricas, hiperparámetros,
recursos, modo offline, snapshots, capa de agente y plan post-AGI 5/5.

La versión vive en `celestia_lib.__version__` (fuente única).
"""

from __future__ import annotations

import os
import sys
import json
import math
import base64
import time
import random
import shutil
import logging
import sqlite3
import argparse
import re
import csv
import atexit
import signal
import subprocess
import tarfile
import threading
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Control de UI cross-platform (PC)
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    HAS_PYAUTOGUI = True
except Exception:
    HAS_PYAUTOGUI = False

# OCR para leer texto en pantalla sin visión LLM (cross-platform)
try:
    import pytesseract
    HAS_OCR = True
except Exception:
    HAS_OCR = False

# Lectura de árbol UI en PC
try:
    import pywinauto  # Windows
    HAS_PYWINAUTO = True
except Exception:
    HAS_PYWINAUTO = False

def _detectar_plataforma() -> str:
    if os.path.exists("/system/bin/input") and os.path.exists("/system/bin/screencap"):
        return "android"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "unknown"

PLATAFORMA = _detectar_plataforma()

# Cargar variables de entorno desde .env si existe. En los tests no: se traían
# las claves reales (DeepSeek es de pago) y los proveedores apagados del dueño,
# y un test de la cadena fallaba o no según lo que hubiera en ese .env.
_env_file = Path(os.environ.get("CELESTIA_DATOS", "").strip()
                 or Path(__file__).resolve().parent).expanduser() / ".env"
if _env_file.exists() and not os.environ.get("CELESTIA_EN_TESTS"):
    with open(_env_file) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                # Quitar comillas envolventes: VAR="valor" o VAR='valor'
                _v = _v.strip().strip('"').strip("'")
                os.environ.setdefault(_k.strip(), _v)

# Suprimir advertencias de HuggingFace antes de importar nada
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

import warnings
warnings.filterwarnings("ignore", message="flock not supported")
warnings.filterwarnings("ignore", category=FutureWarning)

# ─────────────────────────────────────────────
# Dependencias opcionales — sin sys.exit() nunca
# ─────────────────────────────────────────────
HAS_TORCH = False
HAS_TRANSFORMERS = False
HAS_SENTENCE_TRANSFORMERS = False
HAS_FAISS = False
HAS_SKLEARN = False
HAS_NUMPY = False
HAS_BITSANDBYTES = False
HAS_LLAMA_CPP = False

try:
    import torch
    HAS_TORCH = True
except ImportError:
    pass

try:
    from transformers import AutoModelForCausalLM, AutoTokenizer
    HAS_TRANSFORMERS = True
except ImportError:
    pass

try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    pass

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    pass

try:
    from sklearn.cluster import KMeans
    import numpy as np
    HAS_SKLEARN = True
except ImportError:
    pass

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import bitsandbytes  # noqa: F401
    HAS_BITSANDBYTES = True
except ImportError:
    pass

try:
    from llama_cpp import Llama
    HAS_LLAMA_CPP = True
except ImportError:
    pass

# ─── Dependencias opcionales — Modo WhatsApp ───
HAS_FLASK = False
HAS_FASTER_WHISPER = False
HAS_EDGE_TTS = False

try:
    from flask import Flask, request as flask_request, jsonify
    HAS_FLASK = True
except ImportError:
    pass

try:
    from faster_whisper import WhisperModel
    HAS_FASTER_WHISPER = True
except ImportError:
    pass

try:
    import edge_tts as _edge_tts  # noqa: F401
    HAS_EDGE_TTS = True
except ImportError:
    pass

# ─────────────────────────────────────────────
# Rutas base
# ─────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent

# ─────────────────────────────────────────────
# llama-server (binario pre-compilado)
# ─────────────────────────────────────────────
_LLAMA_TARBALL = ROOT / "bin" / "llama-bin.tar.gz"
_LLAMA_EXTRACT_DIR = Path("/tmp/llama-bins/llama-b9009")
_LLAMA_SERVER_BIN = _LLAMA_EXTRACT_DIR / "llama-server"
_LLAMA_SERVER_PORT = 18080
HAS_LLAMA_SERVER_BIN = _LLAMA_TARBALL.exists()
from celestia_lib.paths import MEM_DIR, LOG_DIR, SKILLS_DIR  # noqa: E402
MEM_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

# La regex de muletillas vive en `celestia_lib.model`, que es donde se
# aplica: aquí había una copia que el import de más abajo pisaba, así que
# corregir una dejaba la otra intacta y esperando (sesión 53).

# ─────────────────────────────────────────────
# Logging — INFO al archivo, WARNING+ a consola
# ─────────────────────────────────────────────
# Storage thread-local para el request_id de la request HTTP actual.
# Lo usa JSONFormatter para anotar todos los logs del thread con el mismo id.
import contextvars as _ctx
_REQUEST_ID: _ctx.ContextVar = _ctx.ContextVar("request_id", default="-")


class JSONFormatter(logging.Formatter):
    """Logs como JSON una línea, ideal para enviar a Loki/CloudWatch/Datadog.

    Activar con env: LOG_FORMAT=json
    Si no está activo, usa formato humano normal.
    """
    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        obj = {
            "ts":      datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "request_id": _REQUEST_ID.get(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Campos extra opcionales que el caller puso con logger.info(..., extra={...})
        for k in ("user", "endpoint", "duration_ms", "status_code"):
            v = getattr(record, k, None)
            if v is not None:
                obj[k] = v
        return _json.dumps(obj, ensure_ascii=False)


def _podar_registros(carpeta: Path, actual: Path, cuantos: int = 10) -> None:
    """Conserva los `cuantos` registros anteriores CON algo dentro, más `actual`.

    Los vacíos no cuentan (un arranque fallido no debe desplazar a uno con
    datos) y se borran; `actual` tampoco cuenta, que al crearlo aún está vacío
    (lo señaló Codex, 22 sep).
    """
    try:
        anteriores = sorted((p for p in carpeta.glob("celestia_v1_*.log") if p != actual),
                            key=lambda p: p.stat().st_mtime)
        con_algo = []
        for p in anteriores:
            if p.stat().st_size == 0:
                try: p.unlink()
                except OSError: pass
            else:
                con_algo.append(p)
        for p in con_algo[:-cuantos]:
            try: p.unlink()
            except OSError: pass
    except Exception:
        pass


def _setup_logging() -> logging.Logger:
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    # 22 sep: cada test que importa este módulo creaba aquí su registro, y la
    # limpieza de abajo se llevaba por delante los de la Celestia de verdad (el
    # del 21 sep desapareció en una batería). tests/__init__.py marca los tests.
    carpeta = LOG_DIR
    if os.environ.get("CELESTIA_EN_TESTS"):
        import tempfile
        carpeta = Path(tempfile.gettempdir()) / "celestia_tests_logs"
        carpeta.mkdir(parents=True, exist_ok=True)
    log_file = carpeta / f"celestia_v1_{ts}.log"

    # Decidir formato según env LOG_FORMAT (json|text, default text)
    fmt_kind = os.environ.get("LOG_FORMAT", "text").lower()
    if fmt_kind == "json":
        text_formatter = JSONFormatter()
        console_formatter = JSONFormatter()
    else:
        text_formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [req=%(request_id)s] %(name)s: %(message)s",
            defaults={"request_id": "-"},
        )
        console_formatter = logging.Formatter("⚠ %(message)s")

    # Rotación: max 2 MB por archivo, 3 backups (.log, .log.1, .log.2, .log.3)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file, mode="a", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(text_formatter)

    # Filter que inyecta el request_id al record para que aparezca en %(request_id)s
    class _RequestIdFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.request_id = _REQUEST_ID.get()
            return True

    file_handler.addFilter(_RequestIdFilter())

    # Limpieza de logs viejos: conservar los últimos 10 archivos celestia_v1_*.log
    # (cada arranque crea uno nuevo — sin esto se acumulan indefinidamente)
    _podar_registros(carpeta, log_file)

    # Cap del training_data.jsonl: conservar últimas 2000 entradas. Crece monótonamente
    # con cada conversación; sin tope llena disco y ralentiza few-shot reads.
    try:
        td = MEM_DIR / "training_data.jsonl"
        if td.exists():
            lines = td.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(lines) > 2000:
                td.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silenciar librerías externas ruidosas
    for noisy in ("httpx", "httpcore", "huggingface_hub", "transformers",
                  "sentence_transformers", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    return logging.getLogger("celestia_v1")

logger = _setup_logging()

# Config se movió a celestia_lib/config.py (refactor sesión 15)
from celestia_lib.config import Config


# ResourceManager se movió a celestia_lib/resources.py (refactor sesión 15)
from celestia_lib.resources import ResourceManager


# ─────────────────────────────────────────────
# ConnectivityManager — detección offline + cola de búsquedas
# ─────────────────────────────────────────────
# ConnectivityManager se movió a celestia_lib/connectivity.py (refactor sesión 15)
from celestia_lib.connectivity import ConnectivityManager


# ReminderManager y parse_reminder_time se movieron a celestia_lib/reminders.py
from celestia_lib.reminders import ReminderManager, parse_reminder_time as _parse_reminder_time


# ─────────────────────────────────────────────
# ModelWrapper — llama-cpp (CPU/móvil) y transformers (GPU/CPU)
# ─────────────────────────────────────────────
# ModelWrapper se movió a celestia_lib/model.py (refactor sesión 15)
from celestia_lib.model import ModelWrapper, _MULETILLA_RE

# ─────────────────────────────────────────────
# Embedder — sentence-transformers con fallback Jaccard
# ─────────────────────────────────────────────
# Embedder se movió a celestia_lib/embedder.py (refactor sesión 15)
from celestia_lib.embedder import Embedder


# ─────────────────────────────────────────────
# MemoryDB — SQLite persistente + FAISS opcional
# ─────────────────────────────────────────────
# MemoryDB se movió a celestia_lib/memory.py (refactor sesión 15)
from celestia_lib.memory import MemoryDB

# ─────────────────────────────────────────────
# GoalManager
# ─────────────────────────────────────────────
# Orchestrator + helpers (GoalManager, MetricsLogger, HyperparamAdapter, SnapshotManager)
# se movieron a celestia_lib/orchestrator.py (refactor sesión 15)
from celestia_lib.orchestrator import Orchestrator, GoalManager, MetricsLogger, HyperparamAdapter, SnapshotManager

# ─────────────────────────────────────────────
# FASE 2 — Capa de Agente
# ─────────────────────────────────────────────

# DomoticaManager + DOMOTICA_SKILLS_DIR se movieron a celestia_lib/domotica.py
from celestia_lib.domotica import DomoticaManager, DOMOTICA_SKILLS_DIR

# AgentTools se movió a celestia_lib/tools.py (refactor sesión 15)
from celestia_lib.tools import AgentTools

from celestia_lib.agent import (
    AgentPlanner,
    MemoriaPatronesUI, AgenteAutonomo, _parsear_formato_y_tema,
    run_conversation, run_autonomous,
)
# WhatsAppAPI + _build_perfiles_plataforma + asignación PERFILES_PLATAFORMA
# se movieron a celestia_lib/api.py (refactor sesión 15 — última pieza)
from celestia_lib.api import WhatsAppAPI, _REQUEST_ID, _html_escape
from celestia_lib import __version__

def _puerto_libre(puerto: int) -> bool:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", puerto)) != 0


def _wa_pidfile(puerto: int) -> Path:
    """Pidfile POR PUERTO. Dos instancias en puertos distintos (p. ej. producción
    en 8765 y un QA aislado en otro puerto) NO deben matarse entre sí: antes el
    lockfile era global y cualquier 2ª instancia mandaba SIGTERM a la 1ª."""
    return MEM_DIR / f"celestia_wa_{puerto}.pid"


def _proceso_vivo(pid: int) -> bool:
    """True si el proceso existe. En Linux/Mac os.kill(pid, 0) NO mata: solo
    comprueba.

    En Windows SÍ mata: os.kill con cualquier señal que no sea Ctrl+C es
    TerminateProcess, y con un PID que ya no existe lanza WinError 87. Visto
    en el portátil (3 oct 2026): el lanzador apaga a Celestia sin que limpie su
    pidfile, así que el segundo arranque reventaba aquí y no volvía a abrir; y
    si Windows hubiera dado ese PID a otro programa, lo habría matado.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _proceso_vivo_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existe pero pertenece a otro usuario
    except OSError:
        return False
    return True


def _proceso_vivo_windows(pid: int) -> bool:
    """Pregunta a Windows por el proceso sin tocarlo (OpenProcess + código de
    salida: STILL_ACTIVE es 259)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # 5 = acceso denegado: existe, pero es de otro usuario.
        return ctypes.get_last_error() == 5
    try:
        codigo = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(codigo)):
            return False
        return codigo.value == 259
    finally:
        k32.CloseHandle(h)


def _parece_celestia(pid: int) -> bool:
    """¿Ese PID es un Python? Windows recicla PIDs enseguida, y cerrar «la
    sesión anterior» por un pidfile viejo podía cerrar cualquier otro programa.
    Fuera de Windows se deja como estaba."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        largo = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(largo)):
            return False
        return Path(buf.value).name.lower() in ("python.exe", "pythonw.exe")
    finally:
        k32.CloseHandle(h)


def _gestionar_lock_puerto(puerto: int) -> bool:
    """Arranque seguro del modo WhatsApp mediante PID-file.

    En lugar de matar procesos por coincidencia de texto en `ps aux` (frágil,
    no-portable a Windows, con race del PID reciclado entre el read y el kill),
    usamos un lockfile con NUESTRO PID. Solo terminamos una instancia previa de
    Celestia identificada por ese PID concreto. Si el puerto lo ocupa un proceso
    ajeno, NO matamos nada.

    Devuelve True si el puerto quedó libre para arrancar; False si sigue ocupado
    por algo que no es una instancia previa nuestra.
    """
    # 1) ¿Hay un pidfile de una instancia anterior nuestra EN ESTE PUERTO?
    pf = _wa_pidfile(puerto)
    if pf.exists():
        try:
            pid_previo = int(pf.read_text().strip())
        except (ValueError, OSError):
            pid_previo = 0
        if (pid_previo and pid_previo != os.getpid() and _proceso_vivo(pid_previo)
                and _parece_celestia(pid_previo)):
            try:
                os.kill(pid_previo, signal.SIGTERM)
                print(f"  ↻ Sesión anterior de Celestia (PID {pid_previo}) cerrada.")
                for _ in range(20):  # esperar a que muera (máx 6 s)
                    time.sleep(0.3)
                    if not _proceso_vivo(pid_previo):
                        break
            except (ProcessLookupError, PermissionError):
                pass
        # Pidfile obsoleto o ya tratado: lo limpiamos.
        try:
            pf.unlink()
        except OSError:
            pass
    # 2) Comprobar el puerto (puede tardar en soltarse tras el SIGTERM).
    if _puerto_libre(puerto):
        return True
    for _ in range(16):  # máx 8 s
        time.sleep(0.5)
        if _puerto_libre(puerto):
            return True
    # 3) Sigue ocupado por un proceso ajeno: no matamos nada por seguridad.
    return False


def _escribir_pidfile(puerto: int) -> None:
    try:
        _wa_pidfile(puerto).write_text(str(os.getpid()))
    except OSError:
        pass


def _borrar_pidfile(puerto: int) -> None:
    """Borra el pidfile solo si es nuestro (no toca el de otra instancia)."""
    try:
        pf = _wa_pidfile(puerto)
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink()
    except OSError:
        pass


def run_whatsapp(orch: "Orchestrator", puerto: int = 8765, siempre_voz: bool = False):
    """Modo WhatsApp: levanta la API HTTP y espera mensajes del bridge."""
    if not _gestionar_lock_puerto(puerto):
        print(f"\n  ✗ El puerto {puerto} está ocupado por un proceso ajeno a Celestia. "
              f"No mato procesos por seguridad. Cierra ese proceso o arranca con "
              f"otro puerto: --wa-puerto N")
        orch.close()
        sys.exit(2)
    _escribir_pidfile(puerto)
    try:
        api = WhatsAppAPI(orch, puerto=puerto, siempre_voz=siempre_voz)
        api.iniciar()
    except RuntimeError as e:
        print(f"\n  ✗ {e}")
        orch.close()
    except KeyboardInterrupt:
        print("\n  WhatsApp API detenida.")
    finally:
        _borrar_pidfile(puerto)
        orch.close()


# ─────────────────────────────────────────────
# Argumentos CLI
# ─────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description=f"Celestia v{__version__} — Agente Autónomo")
    p.add_argument("--version", action="version", version=f"Celestia {__version__}")
    p.add_argument(
        "--modo",
        choices=["conversacion", "autonomo", "whatsapp"],
        default="conversacion",
        help="conversacion (por defecto) | autonomo | whatsapp",
    )
    p.add_argument("--loops",    type=int,   default=0,    help="Ciclos en modo autónomo (0=infinito)")
    p.add_argument("--intervalo",type=float, default=5.0,  help="Segundos entre ciclos / polling")
    p.add_argument("--modelo",   type=str,   default=None, help="Nombre del modelo HuggingFace a usar")
    p.add_argument("--wa-puerto", type=int,  default=8765,  help="Puerto HTTP para el modo WhatsApp")
    p.add_argument("--wa-voz",    action="store_true",       help="Responder siempre con audio en modo WhatsApp")
    return p.parse_args()


def main():
    args = parse_args()
    config = Config()
    resources = ResourceManager()

    if args.modelo:
        config.MODEL_NAME = args.modelo
    else:
        config.MODEL_NAME = resources.recommend_model()

    config.EMBED_MODEL = resources.recommend_embed_model()

    logger.info("=== Celestia v%s iniciando — modo=%s modelo=%s ===",
                __version__, args.modo, config.MODEL_NAME)
    logger.info("Deps: torch=%s transformers=%s sentence_transformers=%s faiss=%s",
                HAS_TORCH, HAS_TRANSFORMERS, HAS_SENTENCE_TRANSFORMERS, HAS_FAISS)

    orch = Orchestrator(config, resources)

    if args.modo == "autonomo":
        run_autonomous(orch, loops=args.loops, interval=args.intervalo)
    elif args.modo == "whatsapp":
        # La Celestia de verdad es la única que puede mandar avisos al móvil.
        from celestia_lib import push
        push.habilitar(puerto=args.wa_puerto)
        # Una copia instalada (o la app de Android) mira si hay versión nueva
        # al poco de arrancar y cada 6 horas, y avisa una vez por versión.
        from celestia_lib import actualizar
        actualizar.vigilar(lambda texto, titulo: push.avisar(texto, titulo=titulo))
        run_whatsapp(orch, puerto=args.wa_puerto, siempre_voz=args.wa_voz)
    else:
        run_conversation(orch)


if __name__ == "__main__":
    main()
