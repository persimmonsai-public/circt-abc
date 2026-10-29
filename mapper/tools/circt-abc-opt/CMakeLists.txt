# TODO(M0): confirm MLIROptLib / CIRCT dialect-registration library target
# names against the real CIRCTConfig.cmake/MLIRConfig.cmake exports.
add_llvm_executable(circt-abc-opt circt-abc-opt.cpp)

llvm_update_compile_flags(circt-abc-opt)
target_link_libraries(circt-abc-opt PRIVATE
  CirctAbcStructuralMapper
  CirctAbcABCBridge
  CirctAbcRegisterMapper
  MLIROptLib
  MLIRIR
  MLIRSupport
  MLIRTransforms
  CIRCTHW
  CIRCTHWTransforms
  CIRCTComb
  CIRCTSeq
  CIRCTLLHD
  CIRCTSynth
  CIRCTCombToSynth
  CIRCTExportAIGER
  CIRCTImportVerilog
  CIRCTDebug
  CIRCTMoore
  CIRCTSim
  CIRCTVerif
  CIRCTLTL
  MLIRArithDialect
  MLIRControlFlowDialect
  MLIRFuncDialect
  MLIRLLVMDialect
  MLIRMathDialect
  MLIRSCFDialect
  MLIRUBDialect
)

mlir_check_all_link_libraries(circt-abc-opt)
