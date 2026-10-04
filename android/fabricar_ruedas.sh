#!/usr/bin/env bash
# Fabrica para la app de Android (Chaquopy, Python 3.12) una librería que en
# PyPI sólo existe compilada para PC. Hoy: primp, el motor HTTP del
# metabuscador (ddgs). Enzo (4 oct 2026): «para el buscador de la app de
# Android nada de DuckDuckGo: fabricar primp para Android para que funcione el
# mismo metabuscador que en el PC».
#
#   bash android/fabricar_ruedas.sh 2.0.1 aarch64-linux-android arm64-v8a
#   bash android/fabricar_ruedas.sh 2.0.1 x86_64-linux-android x86_64
#
# Sólo en Linux x86_64 con el NDK de Android (las máquinas de GitHub lo traen)
# y Rust. Deja la rueda en ./ruedas/. La usa .github/workflows/ruedas_android.yml.
#
# Cómo: el código fuente de PyPI, compilado con cargo y el NDK; enlazado con el
# libpython3.12.so del propio Chaquopy (Android exige enlazar libpython, y sin
# abi3 para que no busque un libpython3.so que Chaquopy no trae); y montado
# como rueda con la etiqueta de plataforma que entiende el pip de Chaquopy.
set -euo pipefail

VERSION="$1"           # versión de primp
RUST="$2"              # objetivo de Rust: aarch64-linux-android / x86_64-linux-android
ABI="$3"               # el de Android: arm64-v8a / x86_64
API=24                 # el mínimo de Chaquopy para Python 3.12
PY_CHAQUOPY="3.12.12-0"

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
NDK="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}}"
[ -d "$NDK" ] || { echo "✗ no encuentro el NDK de Android"; exit 1; }
BIN="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
CLANG="$BIN/${RUST}${API}-clang"
[ -x "$CLANG" ] || { echo "✗ no está $CLANG"; exit 1; }

TRABAJO="${RUNNER_TEMP:-/tmp}/rueda-$ABI"
rm -rf "$TRABAJO" && mkdir -p "$TRABAJO" && cd "$TRABAJO"

echo "▸ El Python de Chaquopy para $ABI (de ahí sale libpython3.12.so)"
curl -fsSL -o target.zip \
  "https://repo1.maven.org/maven2/com/chaquo/python/target/$PY_CHAQUOPY/target-$PY_CHAQUOPY-$ABI.zip"
unzip -q target.zip "jniLibs/$ABI/libpython3.12.so" -d target
LIBDIR="$TRABAJO/target/jniLibs/$ABI"

echo "▸ El código de primp $VERSION (PyPI)"
python3 - "$VERSION" <<'PY'
import json, sys, urllib.request
v = sys.argv[1]
datos = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/primp/{v}/json"))
for f in datos["urls"]:
    if f["packagetype"] == "sdist":
        urllib.request.urlretrieve(f["url"], "fuente.tar.gz")
    if "abi3-manylinux_2_17_x86_64" in f["filename"]:
        urllib.request.urlretrieve(f["url"], "plantilla.whl")   # __init__, tipos, METADATA
PY
tar xzf fuente.tar.gz
cd "primp-$VERSION"
# Sin abi3: un .so para Python 3.12 exacto, enlazado con libpython3.12.
sed -i 's/"abi3-py310", //' crates/primp-python/Cargo.toml
grep -q abi3 crates/primp-python/Cargo.toml && { echo "✗ sigue con abi3"; exit 1; }
# Sin el DNS propio (hickory): en Android quiere el «contexto» de Java para
# leer los servidores del sistema (ndk-context) y, como nadie se lo da, la
# primera búsqueda moría con «android context was not initialized» (visto en
# el emulador, 4 oct 2026). Sin él, primp usa el DNS normal del sistema.
sed -i '/"hickory-dns",/d' crates/primp-python/Cargo.toml
grep -q hickory crates/primp-python/Cargo.toml && { echo "✗ sigue con hickory"; exit 1; }

cat > pyo3.cfg <<EOF
implementation=CPython
version=3.12
shared=true
abi3=false
lib_name=python3.12
lib_dir=$LIBDIR
pointer_width=64
build_flags=
suppress_build_script_link_lines=false
EOF
export PYO3_CONFIG_FILE="$PWD/pyo3.cfg"

echo "▸ Rust para $RUST"
rustup target add "$RUST"
VAR="$(echo "$RUST" | tr 'a-z-' 'A-Z_')"
export "CARGO_TARGET_${VAR}_LINKER=$CLANG"
export "CC_${RUST//-/_}=$CLANG"
export "CXX_${RUST//-/_}=${CLANG}++"
export "AR_${RUST//-/_}=$BIN/llvm-ar"
export ANDROID_NDK_ROOT="$NDK" ANDROID_NDK_HOME="$NDK"

echo "▸ Compilando (tarda: TLS, HTTP/2, DNS…)"
cargo build --release --locked --target "$RUST" --manifest-path crates/primp-python/Cargo.toml
SO="target/$RUST/release/libprimp.so"
"$BIN/llvm-strip" "$SO"
"$BIN/llvm-readelf" -d "$SO" | grep NEEDED

echo "▸ Montando la rueda"
mkdir -p "$RAIZ/ruedas"
python3 "$RAIZ/android/montar_rueda.py" "$TRABAJO/plantilla.whl" "$SO" \
  "cp312-cp312-android_${API}_${ABI//-/_}" "$RAIZ/ruedas"
ls -la "$RAIZ/ruedas"
