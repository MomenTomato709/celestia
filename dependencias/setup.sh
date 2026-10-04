#!/usr/bin/env bash
# Celestia v1.0 — Instalación de dependencias
# Uso:
#   bash dependencias/setup.sh                    # instalar todo
#   bash dependencias/setup.sh --descargar-modelo # instalar + descargar modelo GGUF para móvil

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
GGUF_DIR="$ROOT_DIR/memoria/model_cache"
DOWNLOAD_MODEL=false

for arg in "$@"; do
    [ "$arg" = "--descargar-modelo" ] && DOWNLOAD_MODEL=true
done

echo "=== Celestia v1.0 — Instalación de dependencias ==="
echo "Directorio raíz: $ROOT_DIR"

# Detectar Python
if command -v /data/data/com.termux/files/usr/bin/python3 &>/dev/null; then
    PYTHON=/data/data/com.termux/files/usr/bin/python3
    PIP="$PYTHON -m pip"
    PLATFORM="termux"
    echo "Plataforma: Termux ($PYTHON)"
elif command -v python3 &>/dev/null; then
    PYTHON=python3
    PIP="pip3"
    PLATFORM="linux"
    echo "Plataforma: Linux ($PYTHON)"
else
    echo "ERROR: No se encontró Python 3"; exit 1
fi

echo "Versión de Python: $($PYTHON --version)"

# Detectar GPU CUDA
HAS_CUDA=false
if $PYTHON -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    HAS_CUDA=true
    VRAM=$($PYTHON -c "import torch; print(round(torch.cuda.get_device_properties(0).total_memory/1024**3,1))" 2>/dev/null || echo "?")
    GPU_NAME=$($PYTHON -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null || echo "desconocida")
    echo "GPU: $GPU_NAME (${VRAM}GB VRAM) — CUDA disponible"
else
    echo "Sin GPU CUDA — modo CPU/llama-cpp"
fi
echo ""

install_pkg() {
    echo "Instalando: $1"
    $PIP install "$1" -q && echo "  ✓ $1" || echo "  ✗ $1 (fallido — continuando)"
}

# ── Núcleo ────────────────────────────────────
install_pkg "transformers>=4.40.0"
install_pkg "accelerate>=0.27.0"
install_pkg "tokenizers>=0.15.0"
install_pkg "sentencepiece>=0.1.99"

# ── Memoria vectorial ─────────────────────────
install_pkg "sentence-transformers>=2.7.0"
if [ "$HAS_CUDA" = true ]; then
    install_pkg "faiss-gpu"
else
    install_pkg "faiss-cpu"
fi

# ── Utilidades ────────────────────────────────
install_pkg "numpy>=1.24.0"
install_pkg "scikit-learn>=1.3.0"
install_pkg "huggingface-hub>=0.20.0"

# ── GPU: bitsandbytes para quantización 4-bit ──
if [ "$HAS_CUDA" = true ]; then
    install_pkg "bitsandbytes>=0.43.0"
fi

# ── CPU/móvil: llama-cpp-python (inferencia rápida en ARM) ──
if [ "$HAS_CUDA" = false ]; then
    echo ""
    echo "--- Instalando llama-cpp-python (puede tardar, compila C++) ---"
    if [ "$PLATFORM" = "termux" ]; then
        # En Termux necesitamos clang y cmake (pkg install clang cmake)
        if command -v clang &>/dev/null && command -v cmake &>/dev/null; then
            CMAKE_ARGS="-DLLAMA_NATIVE=ON" $PIP install llama-cpp-python -q \
                && echo "  ✓ llama-cpp-python (ARM nativo)" \
                || echo "  ✗ llama-cpp-python — instala: pkg install clang cmake"
        else
            echo "  ! Primero ejecuta: pkg install clang cmake"
            echo "  Luego vuelve a ejecutar este script"
        fi
    else
        $PIP install llama-cpp-python -q \
            && echo "  ✓ llama-cpp-python" \
            || echo "  ✗ llama-cpp-python (continúa con transformers)"
    fi
fi

# ── Descargar modelo GGUF para móvil ──────────
if [ "$DOWNLOAD_MODEL" = true ]; then
    echo ""
    echo "=== Descargando modelo GGUF ==="
    mkdir -p "$GGUF_DIR"

    # Detectar RAM disponible para elegir modelo
    AVAIL_RAM_MB=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "3000")
    echo "RAM disponible: ~${AVAIL_RAM_MB}MB"

    if [ "$AVAIL_RAM_MB" -ge 6000 ]; then
        REPO="Qwen/Qwen2.5-3B-Instruct-GGUF"
        FILENAME="qwen2.5-3b-instruct-q4_k_m.gguf"
        echo "Modelo elegido: Qwen2.5-3B Q4_K_M (~2GB RAM)"
    elif [ "$AVAIL_RAM_MB" -ge 2500 ]; then
        REPO="Qwen/Qwen2.5-1.5B-Instruct-GGUF"
        FILENAME="qwen2.5-1.5b-instruct-q4_k_m.gguf"
        echo "Modelo elegido: Qwen2.5-1.5B Q4_K_M (~1.1GB RAM)"
    else
        REPO="Qwen/Qwen2.5-0.5B-Instruct-GGUF"
        FILENAME="qwen2.5-0.5b-instruct-q4_k_m.gguf"
        echo "Modelo elegido: Qwen2.5-0.5B Q4_K_M (~400MB RAM)"
    fi

    DEST="$GGUF_DIR/model.gguf"
    echo "Descargando $FILENAME desde HuggingFace..."
    $PYTHON -c "
from huggingface_hub import hf_hub_download
import shutil, os
path = hf_hub_download(repo_id='$REPO', filename='$FILENAME', local_dir='$GGUF_DIR')
dest = '$DEST'
if os.path.abspath(path) != os.path.abspath(dest):
    shutil.move(path, dest)
print('Modelo guardado en:', dest)
" && echo "  ✓ Modelo descargado: $DEST" || echo "  ✗ Descarga fallida — comprueba conexión"
fi

# ── Verificación ──────────────────────────────
echo ""
echo "=== Verificación ==="
$PYTHON -c "import torch; print(f'torch {torch.__version__} — CUDA: {torch.cuda.is_available()}')" 2>/dev/null || echo "torch: no disponible"
$PYTHON -c "import transformers; print(f'transformers {transformers.__version__}')" 2>/dev/null || echo "transformers: no disponible"
$PYTHON -c "import sentence_transformers; print('sentence-transformers: ok')" 2>/dev/null || echo "sentence-transformers: no disponible"
$PYTHON -c "import faiss; print('faiss: ok')" 2>/dev/null || echo "faiss: no disponible"
$PYTHON -c "import llama_cpp; print('llama-cpp-python: ok')" 2>/dev/null || echo "llama-cpp-python: no disponible"
$PYTHON -c "
from pathlib import Path
p = Path('$GGUF_DIR/model.gguf')
if p.exists():
    print(f'modelo GGUF: ok ({p.stat().st_size/1024**3:.2f}GB)')
else:
    print('modelo GGUF: no encontrado — ejecuta con --descargar-modelo')
" 2>/dev/null

echo ""
echo "=== Instalación completada ==="
echo ""
echo "Comandos de uso:"
echo "  $PYTHON $ROOT_DIR/celestia.py --modo conversacion"
echo "  $PYTHON $ROOT_DIR/celestia.py --modo autonomo --loops 20"
echo ""
echo "Para descargar el modelo GGUF (móvil):"
echo "  bash dependencias/setup.sh --descargar-modelo"
