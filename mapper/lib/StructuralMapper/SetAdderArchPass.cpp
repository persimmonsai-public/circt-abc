//===- SetAdderArchPass.cpp --------------------------------------*- C++ -*-===//
//
// `--set-adder-arch=arch=<RIPPLE-CARRY|SKLANSKEY|BRENT-KUNG|KOGGE-STONE>`:
// stamp CIRCT's `synth.test.arch` attribute on every `comb.add` and
// `comb.icmp` at or above `min-width` bits, so the bridge's internal
// convert-comb-to-synth lowers them with the chosen prefix structure instead
// of its width heuristic (<8 ripple, <=32 Sklansky, >32 Kogge-Stone).
//
// Why: a per-module census (same recipe, same liberty) put circt's residual
// combinational content slightly BELOW the Yosys reference's overall, with
// the remaining circt excess concentrated in the wide-adder modules (64-bit
// counters, multdiv, cs_registers, compressed_decoder): parallel-prefix
// adders cost area that ripple/Brent-Kung do not, and at a relaxed clock
// most of ibex's adders have the slack. Area-vs-depth is a
// per-design choice; this pass makes it a flow knob. Function-preserving by
// construction (each architecture is CIRCT's own proven lowering); gate on
// the standing RTL-vs-netlist miter when landing a default.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct SetAdderArchPass
    : public PassWrapper<SetAdderArchPass, OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SetAdderArchPass)

  SetAdderArchPass() = default;
  SetAdderArchPass(const SetAdderArchPass &o) : PassWrapper(o) {
    arch = o.arch.getValue();
    minWidth = o.minWidth.getValue();
  }

  Option<std::string> arch{
      *this, "arch", llvm::cl::init(""),
      llvm::cl::desc("RIPPLE-CARRY | SKLANSKEY | BRENT-KUNG | KOGGE-STONE "
                     "(empty = leave CombToSynth's width heuristic)")};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("only stamp adds/compares at or above this width")};

  StringRef getArgument() const final { return "set-adder-arch"; }
  StringRef getDescription() const final {
    return "Stamp synth.test.arch on comb.add/comb.icmp so CombToSynth uses "
           "the chosen adder architecture instead of its width heuristic";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  void runOnOperation() override {
    if (arch.getValue().empty())
      return;
    hw::HWModuleOp module = getOperation();
    auto attr = StringAttr::get(&getContext(), arch.getValue());
    unsigned n = 0;
    module.walk([&](Operation *op) {
      if (!isa<comb::AddOp, comb::ICmpOp>(op))
        return;
      auto ty = dyn_cast<IntegerType>(op->getOperand(0).getType());
      if (!ty || ty.getWidth() < minWidth.getValue())
        return;
      op->setAttr("synth.test.arch", attr);
      ++n;
    });
    if (n)
      llvm::errs() << "[set-adder-arch] " << module.getModuleName() << ": "
                   << n << " add/icmp op(s) -> " << arch.getValue() << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createSetAdderArchPass() {
  return std::make_unique<SetAdderArchPass>();
}

} // namespace circt_abc
