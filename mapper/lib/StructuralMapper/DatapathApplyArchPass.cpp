//===- DatapathApplyArchPass.cpp ---------------------------------*- C++ -*-===//
//
// `--dp-apply-arch=json=<overrides.json>`: the write-back half of the
// PHYS-DP loop. Reads `{ "<op id>": "<ARCH>", ... }` keyed by the ids
// dp-annotate assigns (`<module>_dp<K>`, deterministic: module name plus
// the op's position in the same walk order) and stamps `synth.test.arch`
// on exactly those ops, overriding whatever the global default put there.
// Ops not listed keep their current attribute. Runs at the same pipeline
// point as set-adder-arch (before canonicalize/structural-mapper), so a
// re-synthesis with an override file re-lowers only the chosen ops.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// must match DatapathAnnotatePass's op selection exactly
static bool isWideDatapathOp(Operation *op, unsigned minWidth) {
  if (!isa<comb::AddOp, comb::SubOp, comb::MulOp, comb::ICmpOp, comb::ShlOp,
           comb::ShrUOp, comb::ShrSOp>(op))
    return false;
  if (op->getNumResults() != 1 || op->getNumOperands() < 2)
    return false;
  if (isa<comb::ShlOp, comb::ShrUOp, comb::ShrSOp>(op) &&
      op->getOperand(1).getDefiningOp<hw::ConstantOp>())
    return false;
  if (auto cmp = dyn_cast<comb::ICmpOp>(op)) {
    switch (cmp.getPredicate()) {
    case comb::ICmpPredicate::slt: case comb::ICmpPredicate::sle:
    case comb::ICmpPredicate::sgt: case comb::ICmpPredicate::sge:
    case comb::ICmpPredicate::ult: case comb::ICmpPredicate::ule:
    case comb::ICmpPredicate::ugt: case comb::ICmpPredicate::uge:
      break;
    default:
      return false;
    }
  }
  auto ty = dyn_cast<IntegerType>(op->getOperand(0).getType());
  return ty && ty.getWidth() >= minWidth;
}

struct DatapathApplyArchPass
    : public PassWrapper<DatapathApplyArchPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(DatapathApplyArchPass)

  DatapathApplyArchPass() = default;
  DatapathApplyArchPass(const DatapathApplyArchPass &o) : PassWrapper(o) {
    jsonPath = o.jsonPath.getValue();
    minWidth = o.minWidth.getValue();
  }

  Option<std::string> jsonPath{*this, "json", llvm::cl::init(""),
                               llvm::cl::desc("overrides: {op id: ARCH}")};
  Option<unsigned> minWidth{*this, "min-width", llvm::cl::init(8),
                            llvm::cl::desc("must match dp-annotate")};

  StringRef getArgument() const final { return "dp-apply-arch"; }
  StringRef getDescription() const final {
    return "Stamp synth.test.arch per dp-annotate op id from an override JSON";
  }

  void runOnOperation() override {
    if (jsonPath.getValue().empty())
      return;
    auto buf = llvm::MemoryBuffer::getFile(jsonPath.getValue());
    if (!buf) {
      getOperation().emitError("dp-apply-arch: cannot read ") << jsonPath.getValue();
      return signalPassFailure();
    }
    auto parsed = llvm::json::parse(buf.get()->getBuffer());
    if (!parsed || !parsed->getAsObject()) {
      getOperation().emitError("dp-apply-arch: not a JSON object");
      return signalPassFailure();
    }
    llvm::StringMap<std::string> overrides;
    for (auto &kv : *parsed->getAsObject())
      if (auto v = kv.second.getAsString())
        overrides[kv.first.str()] = v->str();
    unsigned applied = 0;
    for (auto mod : getOperation().getOps<hw::HWModuleOp>()) {
      unsigned k = 0;
      mod.walk([&](Operation *op) {
        if (!isWideDatapathOp(op, minWidth.getValue()))
          return;
        std::string id = (mod.getModuleName() + "_dp" + Twine(k)).str();
        ++k;
        auto it = overrides.find(id);
        if (it == overrides.end())
          return;
        op->setAttr("synth.test.arch", StringAttr::get(&getContext(), it->second));
        ++applied;
      });
    }
    llvm::errs() << "[dp-apply-arch] applied " << applied << " of "
                 << overrides.size() << " override(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createDatapathApplyArchPass() {
  return std::make_unique<DatapathApplyArchPass>();
}

} // namespace circt_abc
