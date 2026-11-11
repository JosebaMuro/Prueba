#!/usr/bin/env bash
# Compila el motor llama.cpp (llama-server) para Android arm64 sin el NDK, usando zig como
# compilador cruzado (binario estático musl, válido en cualquier Android de 64 bits).
# Resultado: jniLibs/arm64-v8a/libllama-server.so, que build_android.sh empaqueta en la APK.
# Requisitos: python3 con pip, cmake, ninja. Tarda 15-30 minutos.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD; WORK=${WORK:-$HERE/.engine}; LLAMA_PY=${LLAMA_PY:-0.3.36}
mkdir -p "$WORK" && cd "$WORK"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q ziglang
# El código fuente oficial de llama.cpp viaja dentro del paquete llama-cpp-python de PyPI
[ -d "llama_cpp_python-$LLAMA_PY" ] || { ./venv/bin/pip download -q --no-binary :all: --no-deps "llama-cpp-python==$LLAMA_PY"; tar xzf "llama_cpp_python-$LLAMA_PY.tar.gz"; }
SRC=$WORK/llama_cpp_python-$LLAMA_PY/vendor/llama.cpp
mkdir -p zigbin
for t in cc c++; do printf '#!/bin/sh\nexec %s -m ziglang %s -target aarch64-linux-musl "$@"\n' "$WORK/venv/bin/python" $t > zigbin/zig-$t; done
for t in ar ranlib; do printf '#!/bin/sh\nexec %s -m ziglang %s "$@"\n' "$WORK/venv/bin/python" $t > zigbin/zig-$t; done
chmod +x zigbin/*
cat > zigbin/toolchain.cmake <<TC
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_C_COMPILER $WORK/zigbin/zig-cc)
set(CMAKE_CXX_COMPILER $WORK/zigbin/zig-c++)
set(CMAKE_AR $WORK/zigbin/zig-ar)
set(CMAKE_RANLIB $WORK/zigbin/zig-ranlib)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
TC
export ZIG_GLOBAL_CACHE_DIR=$WORK/zigcache ZIG_LOCAL_CACHE_DIR=$WORK/zigcache
cmake -S "$SRC" -B build -G Ninja -DCMAKE_TOOLCHAIN_FILE="$WORK/zigbin/toolchain.cmake" -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF \
  -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
  -DCMAKE_C_FLAGS="-mcpu=generic+v8_2a+dotprod+fullfp16" -DCMAKE_CXX_FLAGS="-mcpu=generic+v8_2a+dotprod+fullfp16" \
  -DCMAKE_EXE_LINKER_FLAGS="-static -s"
ninja -C build llama-server
mkdir -p "$HERE/jniLibs/arm64-v8a"
cp build/bin/llama-server "$HERE/jniLibs/arm64-v8a/libllama-server.so"
ls -lh "$HERE/jniLibs/arm64-v8a/libllama-server.so"
echo "Ahora ejecuta: bash build_android.sh"
