"""Detección de hardware y recomendación de modelos según recursos.

Extraído del monolito en sesión 15. Detecta sus propias deps (torch, bitsandbytes)
sin depender de flags globales del módulo padre.
"""
import logging
import os
import sys
from typing import Any, Dict, Optional  # noqa: F401  (Any usado en return)

logger = logging.getLogger("celestia_v1")

# Detección local de deps opcionales (no usar las del módulo padre)
try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

try:
    import bitsandbytes  # noqa: F401
    _HAS_BITSANDBYTES = True
except ImportError:
    _HAS_BITSANDBYTES = False


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


class ResourceManager:
    """Detecta CPU/GPU/RAM y recomienda modelos LLM, visión y embedding según hardware."""

    def __init__(self):
        self.has_gpu = _HAS_TORCH and torch.cuda.is_available()
        self.device = "cuda" if self.has_gpu else "cpu"
        self.gpu_name = ""
        self.vram_gb = 0.0
        self.ram_gb = 4.0

        if self.has_gpu:
            try:
                self.gpu_name = torch.cuda.get_device_name(0)
                props = torch.cuda.get_device_properties(0)
                self.vram_gb = props.total_memory / (1024 ** 3)
            except Exception:
                pass
            logger.info("GPU: %s (%.1fGB VRAM)", self.gpu_name, self.vram_gb)
        else:
            self.ram_gb = self._detect_ram_gb()
            logger.info("CPU — RAM disponible: %.1fGB", self.ram_gb)

    def _detect_ram_gb(self) -> float:
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 ** 2)
        except Exception:
            pass
        return 4.0

    def vram_usage_fraction(self) -> float:
        if not self.has_gpu:
            return 0.0
        try:
            total = torch.cuda.get_device_properties(0).total_memory
            used = torch.cuda.memory_allocated(0)
            return used / total
        except Exception:
            return 0.0

    def recommend_model(self) -> str:
        if self.has_gpu:
            if self.vram_gb >= 40:
                return "Qwen/Qwen2.5-72B-Instruct"
            if self.vram_gb >= 24:
                return "Qwen/Qwen2.5-32B-Instruct"
            if self.vram_gb >= 16:
                return "Qwen/Qwen2.5-14B-Instruct"
            if self.vram_gb >= 10:
                return "Qwen/Qwen2.5-7B-Instruct"
            if self.vram_gb >= 6:
                return "Qwen/Qwen2.5-3B-Instruct"
            if self.vram_gb >= 4:
                return "Qwen/Qwen2.5-1.5B-Instruct"
            return "Qwen/Qwen2.5-0.5B-Instruct"
        return "Qwen/Qwen2.5-0.5B-Instruct"

    def recommend_vision_model(self) -> Optional[str]:
        """Modelo de visión local para análisis instantáneo de UI."""
        if not self.has_gpu:
            return None
        if self.vram_gb >= 24:
            return "Qwen/Qwen2-VL-72B-Instruct"
        if self.vram_gb >= 16:
            return "Qwen/Qwen2-VL-7B-Instruct"
        if self.vram_gb >= 8:
            return "Qwen/Qwen2-VL-2B-Instruct"
        return None

    def modo_ejecucion(self) -> str:
        """Clasifica el setup actual para configurar Celestia óptimamente."""
        if PLATAFORMA == "android":
            return "movil"
        if not self.has_gpu:
            return "pc-cpu"
        if self.vram_gb >= 24:
            return "pc-gpu-alto"
        if self.vram_gb >= 12:
            return "pc-gpu-medio"
        if self.vram_gb >= 6:
            return "pc-gpu-bajo"
        return "pc-cpu"

    def recommend_gguf_model(self) -> Dict[str, Any]:
        ram = self.ram_gb
        if ram >= 6:
            return {
                "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
                "filename": "qwen2.5-3b-instruct-q4_k_m.gguf",
                "size_gb": 2.0,
                "display": "Qwen2.5-3B Q4_K_M (~2GB) — buena calidad en móvil",
            }
        if ram >= 3:
            return {
                "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                "size_gb": 1.1,
                "display": "Qwen2.5-1.5B Q4_K_M (~1.1GB) — mejor equilibrio",
            }
        return {
            "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
            "size_gb": 0.4,
            "display": "Qwen2.5-0.5B Q4_K_M (~400MB) — mínimo",
        }

    def recommend_embed_model(self) -> str:
        if self.has_gpu and self.vram_gb >= 8:
            return "BAAI/bge-large-en-v1.5"
        if self.has_gpu and self.vram_gb >= 4:
            return "all-mpnet-base-v2"
        return "all-MiniLM-L6-v2"

    def should_use_4bit(self, model_name: str) -> bool:
        if not _HAS_BITSANDBYTES or not self.has_gpu:
            return False
        large_tags = ("7b", "8b", "13b", "14b", "30b", "32b", "34b", "70b", "72b")
        return any(tag in model_name.lower() for tag in large_tags)

    def torch_dtype(self) -> Any:
        if not _HAS_TORCH:
            return None
        if self.has_gpu:
            try:
                if torch.cuda.is_bf16_supported():
                    return torch.bfloat16
            except Exception:
                pass
            return torch.float16
        return torch.float32

    def usable_vram_gb(self) -> float:
        """VRAM disponible real (total menos lo que usa el SO, ~1GB)."""
        if not self.has_gpu:
            return 0.0
        try:
            total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            reserved = torch.cuda.memory_reserved(0) / (1024 ** 3)
            return max(0.0, total - reserved - 1.0)
        except Exception:
            return max(0.0, self.vram_gb - 1.0)
