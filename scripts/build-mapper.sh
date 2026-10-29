#!/usr/bin/env bash
# Build the out-of-tree structural-mapper pass library against an
# already-built CIRCT (see scripts/build-circt-from-source.sh). Fast
# iteration loop: touch a .cpp under mapper/lib/, re-run this script,
# `ninja -C mapper/build` alone is even faster once configured once.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CIRCT_SRC=${CIRCT_SRC:-$ROOT/third_party/circt}
LLVM_BUILD=${LLVM_BUILD:-$CIRCT_SRC/llvm/build}
CIRCT_BUILD=${CIRCT_BUILD:-$CIRCT_SRC/build}
BUILD_DIR=${BUILD_DIR:-$ROOT/mapper/build}
DEFAULT_JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
[[ "$DEFAULT_JOBS" -gt 16 ]] && DEFAULT_JOBS=16
JOBS="${JOBS:-$DEFAULT_JOBS}"

for d in "$LLVM_BUILD" "$CIRCT_BUILD"; do
    [[ -d "$d" ]] || { echo "ERROR: $d not found -- run scripts/build-circt-from-source.sh first" >&2; exit 1; }
done

cmake -S "$ROOT/mapper" -B "$BUILD_DIR" -G Ninja \
    -DMLIR_DIR="$LLVM_BUILD/lib/cmake/mlir" \
    -DLLVM_DIR="$LLVM_BUILD/lib/cmake/llvm" \
    -DCIRCT_DIR="$CIRCT_BUILD/lib/cmake/circt" \
    -DCMAKE_BUILD_TYPE=Release

cmake --build "$BUILD_DIR" --target circt-abc-opt --parallel "$JOBS"

echo "Built: $BUILD_DIR/bin/circt-abc-opt"
