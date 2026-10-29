//===- Passes.h - circt-abc pass declarations ------------------*- C++ -*-===//
//
// TODO(M0): verify the exact mlir::Pass / mlir::OperationPass include path
// and registration macros below against the real MLIR checkout -- written
// from standard, long-stable MLIR out-of-tree-pass convention, but every
// other guessed API name in this repo has been getting a verification pass
// once real headers are available, and this should be no exception.
//
//===----------------------------------------------------------------------===//

#ifndef CIRCT_ABC_PASSES_H
#define CIRCT_ABC_PASSES_H

#include "mlir/Pass/Pass.h"
#include <memory>

namespace mlir {
class ModuleOp;
} // namespace mlir

namespace circt {
namespace hw {
class HWModuleOp;
} // namespace hw
} // namespace circt

namespace circt_abc {

/// M0 scaffold: no-op pass that walks the module and reports HW/Comb op
/// counts, to validate the out-of-tree mapper/ build against a real CIRCT
/// checkout. Real structural-mapping logic lands starting at M1/M2.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createStructuralMapperPass();

/// See Passes.td's ExternalizeMappedCells description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createExternalizeMappedCellsPass();

/// See Passes.td's LowerFirMem description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createLowerFirMemPass();

/// See Passes.td's StripSimulationConstructs description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createStripSimulationConstructsPass();

/// See Passes.td's LowerClockInverters description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createLowerClockInvertersPass();

/// See Passes.td's OutlineResidualLogic description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createOutlineResidualLogicPass();

/// See Passes.td's DecodeMuxChains description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createDecodeMuxChainsPass();

/// See Passes.td's MergeResidualModules description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMergeResidualModulesPass();

/// See Passes.td's ABCBridge description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createABCBridgePass();

/// See Passes.td's RegisterLegalize description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRegisterLegalizePass();

/// See Passes.td's RegisterMapper description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createRegisterMapperPass();

/// See Passes.td's RemoveDeadRegisters description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRemoveDeadRegistersPass();

/// See Passes.td's ClockGateInference description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createClockGateInferencePass();

/// See Passes.td's NarrowDeadRegisterBits description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createNarrowDeadRegisterBitsPass();

/// See Passes.td's RegisterConstantPropagation description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRegisterConstantPropagationPass();

/// See Passes.td's DatapathApplyArch description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createDatapathApplyArchPass();

/// See Passes.td's DatapathAnnotate description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createDatapathAnnotatePass();

/// See Passes.td's NarrowWholeValueReads description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowWholeValueReadsPass();

/// See Passes.td's NarrowExtendedMultiplies description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowExtendedMultipliesPass();

/// See Passes.td's NarrowMuxToDifferingBits description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowMuxToDifferingBitsPass();

/// See Passes.td's SpecializeMuxArms description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSpecializeMuxArmsPass();

/// See Passes.td's SelectChainPrefixOr description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSelectChainPrefixOrPass();

/// See Passes.td's NarrowDeadOutputBits description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowDeadOutputBitsPass();

/// See Passes.td's MapCompressorCells description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapCompressorCellsPass();

/// See Passes.td's SpecializeConstantInputs description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSpecializeConstantInputsPass();

/// See Passes.td's NarrowSparseShifts description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowSparseShiftsPass();

/// See Passes.td's LocalizeDynamicRmw description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createLocalizeDynamicRmwPass();

/// See Passes.td's OutlineDatapathOps description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createOutlineDatapathOpsPass();

/// See Passes.td's PruneUnusedResidualOutputs description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createPruneUnusedResidualOutputsPass();

/// See Passes.td's SetAdderArch description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createSetAdderArchPass();

/// See Passes.td's LowerAggregateRegisters description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createLowerAggregateRegistersPass();

/// See Passes.td's NarrowConstantRegisterBits description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowConstantRegisterBitsPass();

/// See Passes.td's NarrowArrayRegisters description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createNarrowArrayRegistersPass();

/// See Passes.td's DedupRegisters description.
std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createDedupRegistersPass();

/// See Passes.td's RemoveIdentityBuffers description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createRemoveIdentityBuffersPass();

/// Replace an AND2+XOR2 pair over the same two operands with one
/// two-output half-adder cell. ABC's mappers skip multi-output cells by
/// construction, so this peephole is the only way a half adder reaches
/// the netlist. See FuseHalfAddersPass.cpp.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createFuseHalfAddersPass();

/// Lower a wide `comb.add` into a ripple chain of the library's own
/// two-output adder cells (a half adder for bit 0, full adders for the
/// rest). ABC's mapper cannot emit either, so only part of a critical
/// adder's carry chain ends up covered by adder cells. See
/// MapAdderChainsPass.cpp.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapAdderChainsPass();

/// Technology-aware resubstitution on the mapped netlist: enumerate
/// structural cuts over library cells and substitute a cheaper single
/// cell computing the same function (ASP-DAC'25 section 3.3). See
/// ResubMappedPass.cpp.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createResubMappedPass();

/// Cap net fanout on the mapped netlist with a balanced buffer tree,
/// touching only nets above the cap. See LimitFanoutPass.cpp.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createLimitFanoutPass();

/// See Passes.td's MapShellLogic description.
std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapShellLogicPass();

#define GEN_PASS_REGISTRATION
#include "circt-abc/Passes.h.inc"

} // namespace circt_abc

#endif // CIRCT_ABC_PASSES_H
