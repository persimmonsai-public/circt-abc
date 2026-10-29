# Standard MLIR tablegen pattern (-gen-pass-decls), matching how CIRCT's
# own lib/Transforms/*/Passes.td are wired up.
set(LLVM_TARGET_DEFINITIONS Passes.td)
mlir_tablegen(Passes.h.inc -gen-pass-decls -name CirctAbc)
add_public_tablegen_target(CirctAbcPassesIncGen)
