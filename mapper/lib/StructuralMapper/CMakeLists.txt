# TODO(M0): confirm exact CIRCT dialect library target names below against
# the real CIRCTConfig.cmake export -- CIRCTHW/CIRCTComb/CIRCTSeq are
# confirmed to exist as dialects in the source tree, but the exact CMake
# *target* names they're exported under need a real build to check, same
# discipline as everywhere else here.
add_mlir_library(CirctAbcStructuralMapper
  StructuralMapperPass.cpp
  LibertyLibrary.cpp
  MapShellLogicPass.cpp
  RemoveIdentityBuffersPass.cpp
  FuseHalfAddersPass.cpp
  MapAdderChainsPass.cpp
  ResubMappedPass.cpp
  LimitFanoutPass.cpp
  DecodeMuxChainsPass.cpp
  SetAdderArchPass.cpp
  PruneUnusedResidualOutputsPass.cpp
  NarrowExtendedMultipliesPass.cpp
  NarrowMuxToDifferingBitsPass.cpp
  SpecializeMuxArmsPass.cpp
  SelectChainPrefixOrPass.cpp
  NarrowDeadOutputBitsPass.cpp
  MapCompressorCellsPass.cpp
  SpecializeConstantInputsPass.cpp
  NarrowSparseShiftsPass.cpp
  NarrowWholeValueReadsPass.cpp
  LocalizeDynamicRmwPass.cpp
  OutlineDatapathOpsPass.cpp
  DatapathAnnotatePass.cpp
  DatapathApplyArchPass.cpp
  ExternalizeMappedCellsPass.cpp
  LowerClockInvertersPass.cpp
  StripSimulationConstructsPass.cpp
  OutlineResidualLogicPass.cpp
  MergeResidualModulesPass.cpp

  DEPENDS
  CirctAbcPassesIncGen

  LINK_LIBS PUBLIC
  CIRCTCombToDatapath
  CIRCTDatapathToComb
  CIRCTDatapath
  MLIRAnalysis
  MLIRIR
  MLIRPass
  MLIRSupport
  MLIRTransformUtils
  CIRCTHW
  CIRCTComb
  CIRCTSeq
  CIRCTLLHD
)
