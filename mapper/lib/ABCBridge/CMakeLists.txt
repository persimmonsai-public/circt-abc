# Automates RECIPE.md's validated recipe: clone target module -> lower to
# synth.aig -> export real AIGER -> run a real ABC subprocess against the
# liberty -> re-import ABC's mapped Verilog in-process -> externalize
# cell placeholders -> splice back under the original symbol name. See
# ABCBridge.cpp's file comment for the full step-by-step mapping to
# RECIPE.md's manually-verified CLI sequence.
add_mlir_library(CirctAbcABCBridge
  ABCBridge.cpp

  LINK_LIBS PUBLIC
  MLIRIR
  MLIRPass
  MLIRSupport
  MLIRTransforms
  CIRCTHW
  CIRCTHWTransforms
  CIRCTComb
  CIRCTCombToSynth
  CIRCTCombToDatapath
  CIRCTDatapathToComb
  CIRCTDatapathTransforms
  CIRCTDatapath
  CIRCTSynthTransforms
  CIRCTExportAIGER
  CIRCTImportVerilog
  CIRCTSim
  MLIRArithDialect
  MLIRControlFlowDialect
  MLIRLLVMDialect
  MLIRMathDialect
  MLIRUBDialect
)
