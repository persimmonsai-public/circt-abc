#!/usr/bin/env bash
# Builds libabc_shim.so: a small extern "C" shim (abc_shim.cpp) linked
# against third_party/yosys/build/libyosys-abc.a, the already-built
# static library containing the entire vendored ABC source tree (built by
# Yosys's own CMake, no ABC build-system changes needed here).
#
# Defines/flags below are copied verbatim from
# third_party/yosys/build/compile_commands.json's own entry for
# libyosys-abc.dir object files, so this shim is ABI-compatible with that
# archive (same ABC_NAMESPACE, same struct layouts).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ABC_SRC="$ROOT/third_party/yosys/abc/src"
ABC_LIB="$ROOT/third_party/yosys/build/libyosys-abc.a"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$ABC_LIB" ]]; then
  echo "ERROR: $ABC_LIB not found -- build third_party/yosys first." >&2
  exit 1
fi

g++ \
  -DABC_NAMESPACE=abc -DABC_NO_DYNAMIC_LINKING -DABC_NO_HISTORY=1 \
  -DABC_NO_RLIMIT -DABC_USE_CUDD=1 -DABC_USE_PTHREADS -DABC_USE_STDINT_H=1 \
  -DWIN32_NO_DLL \
  -I"$ABC_SRC" \
  -std=gnu++20 -fPIC -fpermissive -fno-exceptions \
  -Wno-write-strings -Wno-attributes -Wno-deprecated-declarations \
  -Wno-format -Wno-sizeof-pointer-memaccess \
  -shared -o "$HERE/libabc_shim.so" "$HERE/abc_shim.cpp" \
  -Wl,--whole-archive "$ABC_LIB" -Wl,--no-whole-archive \
  -lstdc++ -lpthread -lm

echo "built: $HERE/libabc_shim.so"
