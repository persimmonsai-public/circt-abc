cmake_minimum_required(VERSION 3.20)

if(POLICY CMP0116)
  cmake_policy(SET CMP0116 OLD)
endif()

# Out-of-tree MLIR/CIRCT pass library -- built against an already-built
# CIRCT (see ../scripts/build-circt-from-source.sh + build-mapper.sh), not
# a CIRCT fork; the CIRCT changes the flow needs are a patch series on a
# pinned upstream commit (../patches/circt/).
#
# Standalone-project pattern (find_package(MLIR)/find_package(CIRCT)) is
# modeled on MLIR's own out-of-tree "standalone" example
# (mlir/examples/standalone in llvm-project) plus CIRCT's equivalent
# CIRCTConfig.cmake export -- both installed into
# third_party/circt/{llvm/build,build}/lib/cmake/{mlir,circt} by the CIRCT
# build script.
#
# TODO(M0): verify every find_package() and exported target/library name
# below against the ACTUAL CIRCTConfig.cmake / MLIRConfig.cmake once the
# CIRCT build completes -- these are written from documented/typical CIRCT
# conventions (CIRCT<DialectName> per dialect, e.g. CIRCTHW/CIRCTComb/
# CIRCTSeq, confirmed to exist in the source tree; CIRCTSynth/
# CIRCTImportLiberty/CIRCTTargetDebugInfo etc. are plausible but
# unconfirmed names) and must not be trusted blindly, the same discipline
# applied to every other guessed CLI flag/API name in this repo so far.
project(circt-abc-mapper LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Without this, add_llvm_executable() places binaries under
# <build>/tools/<name>/<name> instead of <build>/bin/<name> -- every script
# in this repo (lower_ibex.sh, build-mapper.sh) assumes the latter.
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

find_package(MLIR REQUIRED CONFIG)
find_package(CIRCT REQUIRED CONFIG)

message(STATUS "Using MLIRConfig.cmake in: ${MLIR_DIR}")
message(STATUS "Using CIRCTConfig.cmake in: ${CIRCT_DIR}")
message(STATUS "Using LLVMConfig.cmake in: ${LLVM_DIR}")

list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${CIRCT_CMAKE_DIR}")

include(TableGen)
include(AddLLVM)
include(AddMLIR)
include(HandleLLVMOptions)
# CIRCT's exported cmake dir ships an AddCIRCT.cmake analogous to AddMLIR --
# TODO(M0): confirm the filename/macros it provides (e.g. add_circt_library)
# once available; add_mlir_library is used below as the safer, definitely-
# exported fallback until that's confirmed.
if(EXISTS "${CIRCT_CMAKE_DIR}/AddCIRCT.cmake")
  include(AddCIRCT)
endif()

include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${CIRCT_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)
include_directories(${CMAKE_CURRENT_BINARY_DIR}/include)
link_directories(${LLVM_BUILD_LIBRARY_DIR})
add_definitions(${LLVM_DEFINITIONS})

add_subdirectory(include)
add_subdirectory(lib)
add_subdirectory(tools)

# Regression tests. Until 2026-09-19 the `// RUN:` lines in mapper/test/
# were never executed by anything -- there was no lit config, no test
# CMakeLists and no check target, and the only evidence they had ever run
# was two stray `%t.json` files where lit's `%t` had never been
# substituted. Configure with
#   -DLLVM_EXTERNAL_LIT=<llvm-build>/bin/llvm-lit
# since an out-of-tree build has no lit of its own, then `ninja
# check-circt-abc`.
set(CIRCT_ABC_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR})
set(CIRCT_ABC_BINARY_DIR ${CMAKE_CURRENT_BINARY_DIR})
set(CIRCT_ABC_TOOLS_DIR ${CMAKE_BINARY_DIR}/bin)
add_subdirectory(test)
