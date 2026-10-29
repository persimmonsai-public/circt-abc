#!/usr/bin/env bash
# Clones and builds CIRCT (+ its bundled LLVM/MLIR) from source, with the
# slang-based SystemVerilog frontend enabled. Adapted from v80_sim's proven
# recipe (scripts/build-circt-from-source.sh in that repo).
#
# The flow runs on CIRCT at third_party/CIRCT_REV plus the local patch series
# in patches/circt/ (see patches/circt/BASE). A checkout that already carries
# exactly that series is built as is; a checkout sitting at the base gets the
# series applied with git am on a `circt-abc` branch. Anything else (other
# local commits, uncommitted edits) is refused, so the script never discards
# local CIRCT work; FORCE_CHECKOUT=ON overrides that.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS_ROOT="${CIRCT_ABC_DEPS_ROOT:-"$ROOT_DIR/third_party"}"
CIRCT_REPO="${CIRCT_REPO:-https://github.com/llvm/circt.git}"
CIRCT_BASE="${CIRCT_BASE:-$(cat "$ROOT_DIR/third_party/CIRCT_REV")}"
PATCH_DIR="${PATCH_DIR:-"$ROOT_DIR/patches/circt"}"
CIRCT_SRC="${CIRCT_SRC:-"$DEPS_ROOT/circt"}"
LLVM_BUILD="${LLVM_BUILD:-"$CIRCT_SRC/llvm/build"}"
CIRCT_BUILD="${CIRCT_BUILD:-"$CIRCT_SRC/build"}"
BUILD_TYPE="${BUILD_TYPE:-Release}"
GENERATOR="${GENERATOR:-Ninja}"
DEFAULT_JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
if [[ "$DEFAULT_JOBS" -gt 16 ]]; then
    DEFAULT_JOBS=16
fi
JOBS="${JOBS:-$DEFAULT_JOBS}"
CMAKE_BIN="${CMAKE_BIN:-cmake}"
ENABLE_LLD="${ENABLE_LLD:-OFF}"
ENABLE_ASSERTIONS="${ENABLE_ASSERTIONS:-ON}"
CIRCT_ENABLE_SLANG_FRONTEND="${CIRCT_ENABLE_SLANG_FRONTEND:-ON}"
CIRCT_SLANG_BUILD_FROM_SOURCE="${CIRCT_SLANG_BUILD_FROM_SOURCE:-ON}"
RUN_CHECKS="${RUN_CHECKS:-OFF}"
FORCE_CHECKOUT="${FORCE_CHECKOUT:-OFF}"
PREPARE_ONLY="${PREPARE_ONLY:-OFF}"

mkdir -p "$DEPS_ROOT"

if [[ ! -e "$CIRCT_SRC/.git" ]]; then
    git clone "$CIRCT_REPO" "$CIRCT_SRC"
fi
g() { git -C "$CIRCT_SRC" "$@"; }

if ! g cat-file -e "$CIRCT_BASE^{commit}" 2>/dev/null; then
    g fetch origin "$CIRCT_BASE"
fi

shopt -s nullglob
PATCHES=("$PATCH_DIR"/0*.patch)
shopt -u nullglob
[[ ${#PATCHES[@]} -gt 0 ]] || { echo "ERROR: no patches in $PATCH_DIR" >&2; exit 1; }

# Stable patch ids: equal when two series make the same changes, whatever
# their commit hashes, committers or dates.
series_ids() { git patch-id --stable | cut -d' ' -f1; }
WANT_IDS="$(cat "${PATCHES[@]}" | series_ids)"
HEAD_IDS=""
if g merge-base --is-ancestor "$CIRCT_BASE" HEAD 2>/dev/null; then
    HEAD_IDS="$(g log -p --reverse --no-merges "$CIRCT_BASE..HEAD" | series_ids)"
fi
DIRTY="$(g status --porcelain --untracked-files=no)"

if [[ -z "$DIRTY" && -n "$HEAD_IDS" && "$HEAD_IDS" == "$WANT_IDS" ]]; then
    echo "INFO: $CIRCT_SRC already carries the ${#PATCHES[@]}-patch series on $CIRCT_BASE, building as is."
else
    if [[ "$FORCE_CHECKOUT" != "ON" ]] && { [[ -n "$DIRTY" ]] || [[ "$(g rev-parse HEAD)" != "$CIRCT_BASE" ]]; }; then
        echo "ERROR: $CIRCT_SRC is neither at the base $CIRCT_BASE nor at the base plus" >&2
        echo "       exactly the series in $PATCH_DIR (HEAD $(g rev-parse --short HEAD)${DIRTY:+, uncommitted edits})." >&2
        echo "       Refusing to check out over it. Commit or stash local work, or rerun" >&2
        echo "       with FORCE_CHECKOUT=ON to reset a circt-abc branch to the base." >&2
        exit 1
    fi
    g checkout -B circt-abc "$CIRCT_BASE"
    # A fixed committer and committer date make the resulting commit hashes
    # the same on every machine.
    GIT_COMMITTER_NAME=circt-abc GIT_COMMITTER_EMAIL=circt-abc@localhost \
        g am --quiet --committer-date-is-author-date "${PATCHES[@]}"
    echo "INFO: applied ${#PATCHES[@]} patches from $PATCH_DIR on $CIRCT_BASE"
fi
g submodule update --init --depth 1

# patches/circt-createIncrement-i1-writeback-fix.patch is not applied: its
# i1 writeback is patch 0038 of the series, and its remaining
# hierarchical-value hunk was never part of the flow (see patches/circt/BASE).

if [[ "$PREPARE_ONLY" == "ON" ]]; then
    echo "INFO: PREPARE_ONLY=ON, source ready at $CIRCT_SRC ($(g rev-parse --short HEAD))."
    exit 0
fi

"$CMAKE_BIN" -S "$CIRCT_SRC/llvm/llvm" -B "$LLVM_BUILD" -G "$GENERATOR" \
    -DLLVM_ENABLE_PROJECTS=mlir \
    -DLLVM_TARGETS_TO_BUILD=host \
    -DLLVM_ENABLE_ASSERTIONS="$ENABLE_ASSERTIONS" \
    -DLLVM_ENABLE_LLD="$ENABLE_LLD" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE"

"$CMAKE_BIN" --build "$LLVM_BUILD" --parallel "$JOBS"

if [[ "$RUN_CHECKS" == "ON" ]]; then
    "$CMAKE_BIN" --build "$LLVM_BUILD" --target check-mlir --parallel "$JOBS"
fi

# Two host-dependent breakages, both seen 2026-10-27 with cmake 3.31:
# - cmake can find an OR-tools install (e.g. in ~/.local) and put its
#   include dir, with its own Boost, on every compile line; CIRCT does not
#   need OR-tools, so never look for it.
# - slang's bundled Boost.Regex leaves standalone mode whenever any
#   <boost/version.hpp> is visible (system boost-devel is enough), and with
#   CIRCT's -fno-exceptions the link then fails on boost::throw_exception.
"$CMAKE_BIN" -S "$CIRCT_SRC" -B "$CIRCT_BUILD" -G "$GENERATOR" \
    -DMLIR_DIR="$LLVM_BUILD/lib/cmake/mlir" \
    -DLLVM_DIR="$LLVM_BUILD/lib/cmake/llvm" \
    -DLLVM_ENABLE_ASSERTIONS="$ENABLE_ASSERTIONS" \
    -DLLVM_ENABLE_LLD="$ENABLE_LLD" \
    -DCIRCT_SLANG_FRONTEND_ENABLED="$CIRCT_ENABLE_SLANG_FRONTEND" \
    -DCIRCT_SLANG_BUILD_FROM_SOURCE="$CIRCT_SLANG_BUILD_FROM_SOURCE" \
    -DCMAKE_DISABLE_FIND_PACKAGE_ortools=ON \
    -DCMAKE_CXX_FLAGS="${CMAKE_CXX_FLAGS:-} -DBOOST_REGEX_STANDALONE" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE"

"$CMAKE_BIN" --build "$CIRCT_BUILD" --parallel "$JOBS"

if [[ "$RUN_CHECKS" == "ON" ]]; then
    "$CMAKE_BIN" --build "$CIRCT_BUILD" --target check-circt --parallel "$JOBS"
fi

cat <<EOF

CIRCT source build is ready:
  CIRCT source: $CIRCT_SRC (third_party/CIRCT_REV + patches/circt/)
  LLVM build:  $LLVM_BUILD
  CIRCT build: $CIRCT_BUILD

Useful PATH entries:
  export PATH="$CIRCT_BUILD/bin:$LLVM_BUILD/bin:\$PATH"

Next: scripts/build-mapper.sh to build the out-of-tree structural mapper
against this CIRCT build.
EOF
