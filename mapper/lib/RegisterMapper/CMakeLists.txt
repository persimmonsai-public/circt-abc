# Register (flip-flop) technology mapping: RegisterLegalizePass folds
# sync/non-constant-reset seq.firreg ops into D-input mux logic, and
# RegisterMapperPass bit-blasts the remaining (no-reset or async-constant-
# reset) firregs into real liberty flip-flop cells. RegisterMapperPass
# reuses StructuralMapper/LibertyLibrary.{h,cpp}'s liberty scanner (now
# extended with FF-cell extraction) rather than duplicating a parser, so
# this library depends on CirctAbcStructuralMapper for that shared code.
add_mlir_library(CirctAbcRegisterMapper
  ClockGateInferencePass.cpp
  DedupRegistersPass.cpp
  NarrowArrayRegistersPass.cpp
  NarrowDeadRegisterBitsPass.cpp
  RegisterConstantPropagationPass.cpp
  NarrowConstantRegisterBitsPass.cpp
  LowerAggregateRegistersPass.cpp
  RegisterLegalizePass.cpp
  LowerFirMemPass.cpp
  RegisterMapperPass.cpp
  RemoveDeadRegistersPass.cpp

  DEPENDS
  CirctAbcPassesIncGen

  LINK_LIBS PUBLIC
  MLIRIR
  MLIRPass
  MLIRSupport
  MLIRTransformUtils
  CIRCTHW
  CIRCTComb
  CIRCTSeq
  CirctAbcStructuralMapper
)
