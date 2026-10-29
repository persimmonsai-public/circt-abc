//===- RemoveIdentityBuffersPass.cpp ------------------------*- C++ -*-===//
//
// `--remove-identity-buffers` (ON by default -- landed OFF per the
// project convention, then promoted with explicit user sign-off; see the
// enableRemoval option's own description for the full diligence record,
// including the honest post-P&R WNS spread across ABC-noise netlist
// instances that is this promotion's accepted cost axis).
//
// Targets the last itemized slice of the buffer-cell gap vs. the Yosys
// reference flow: the promoted-default netlist carries well over a thousand
// buffer instances where the Yosys reference netlist ships ZERO buffer cells
// pre-P&R. These buffers are an artifact of the outline/merge port
// architecture: ABC must physically drive every merged-residual-module
// boundary port with a distinct net (see MergeResidualModulesPass.cpp's
// port-count story), and once
// `--hw-flatten-modules` erases those module boundaries the buffers survive
// as pure logical identities that nothing downstream removes -- they are
// opaque `hw.instance` ops referencing `hw.module.extern` cell declarations,
// so MLIR's own canonicalize/DCE cannot see through them.
//
// A buffer cell's liberty `function` is literally the identity of its one
// input pin (e.g. `function : "A"`). Removing such an instance and rewiring
// its loads to its input is a LOGICAL no-op; the cell's electrical
// (drive-strength) role is exactly what OpenROAD's own resizer
// (`repair_design`/`repair_timing`) owns downstream, and letting P&R
// re-insert buffers where real placement and loading demand them is precisely
// the Yosys reference methodology this project benchmarks against.
//
// Identity detection is liberty-driven, not a hardcoded name list: a cell
// qualifies iff LibertyLibrary parsed it as a single-input combinational
// cell whose truth table is the identity (table == 0b10 under the
// library's own bit-`a`-is-output-for-assignment-`a` convention) -- the
// same oracle discipline every other liberty-consuming pass here uses.
//
// Conservative guards (when in doubt, leave the instance alone):
//   - the instance must reference an `hw.module.extern` (a real cell
//     declaration), not an ordinary `hw.module` that merely shares a
//     cell's name;
//   - exactly one operand and one result, of identical type;
//   - no inner symbol on the instance (someone could be referring to it
//     hierarchically).
//
// Buffer CHAINS (a buffer feeding another buffer) need no special
// handling: victims are collected first, then each is RAUW'd and erased;
// replaceAllUsesWith rewrites the downstream buffer's operand in place,
// so every chain collapses to the original driver regardless of
// visitation order. A buffer whose output feeds a top-level output port
// simply leaves `hw.output` reading the buffer's input directly --
// ExportVerilog emits a plain `assign`, covered by a synthetic test.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct RemoveIdentityBuffersPass
    : public PassWrapper<RemoveIdentityBuffersPass,
                          OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RemoveIdentityBuffersPass)

  RemoveIdentityBuffersPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  RemoveIdentityBuffersPass(const RemoveIdentityBuffersPass &other)
      : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    enableRemoval = other.enableRemoval.getValue();
  }

  StringRef getArgument() const final { return "remove-identity-buffers"; }
  StringRef getDescription() const final {
    return "ON by default (promoted, see Passes.td): remove hw.instance ops of "
           "liberty cells whose function is the identity (buffers), "
           "rewiring loads to the buffer input -- pre-P&R buffering is "
           "OpenROAD's resizer's job, per the Yosys reference methodology";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file used to derive the identity-function "
                      "(buffer) cell set -- same file abc-bridge maps "
                      "against")};

  Option<bool> enableRemoval{
      *this, "enable-identity-buffer-removal",
      llvm::cl::desc("Enable identity-buffer removal -- ON by default, "
                      "promoted with explicit sign-off after the full "
                      "diligence record (whole-design BMC d2 SUCCESS + "
                      "identity-by-construction argument; real P&R x2 "
                      "clean on each of two real netlist instances: "
                      "smaller signed-off area and power than the "
                      "baseline; post-P&R WNS varies with the ABC-noise "
                      "netlist instance -- the accepted cost axis)"),
      llvm::cl::init(true)};

  void runOnOperation() override {
    if (!enableRemoval.getValue())
      return;
    if (libertyPath.getValue().empty()) {
      getOperation().emitError(
          "remove-identity-buffers: liberty= is required when "
          "enable-identity-buffer-removal is set");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      getOperation().emitError(
          "remove-identity-buffers: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }

    mlir::ModuleOp top = getOperation();
    MLIRContext *ctx = top.getContext();

    // Identity-cell name set, restricted to names that are ALSO declared
    // as hw.module.extern in this design (the guard against an ordinary
    // hw.module coincidentally sharing a cell name).
    llvm::DenseSet<StringAttr> identityExterns;
    llvm::DenseSet<StringRef> identityNames;
    for (StringRef name : lib->identityCellNames())
      identityNames.insert(name);
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (identityNames.contains(ext.getModuleName()))
        identityExterns.insert(
            StringAttr::get(ctx, ext.getModuleName()));

    unsigned removed = 0;
    for (auto hwmod : top.getOps<hw::HWModuleOp>()) {
      SmallVector<hw::InstanceOp> victims;
      for (auto inst :
           hwmod.getBodyBlock()->getOps<hw::InstanceOp>()) {
        if (!identityExterns.contains(
                inst.getModuleNameAttr().getAttr()))
          continue;
        if (inst.getNumOperands() != 1 || inst.getNumResults() != 1)
          continue;
        if (inst.getOperand(0).getType() !=
            inst.getResult(0).getType())
          continue;
        if (inst.getInnerSymAttr())
          continue;
        victims.push_back(inst);
      }
      for (auto inst : victims) {
        inst.getResult(0).replaceAllUsesWith(inst.getOperand(0));
        inst.erase();
        ++removed;
      }
    }
    if (removed > 0)
      llvm::errs() << "[remove-identity-buffers] removed " << removed
                   << " identity-buffer instance(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createRemoveIdentityBuffersPass() {
  return std::make_unique<RemoveIdentityBuffersPass>();
}

} // namespace circt_abc
