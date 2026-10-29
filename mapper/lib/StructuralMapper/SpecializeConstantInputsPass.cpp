//===- SpecializeConstantInputsPass.cpp --------------------------*- C++ -*-===//
//
// `--specialize-constant-module-inputs`: replace a module input port that
// every instance drives with the SAME literal by that literal inside the
// module body.
//
// Why: this flow has the outward direction and not the inward one.
// `prune-unused-residual-outputs propagate-constant-outputs=true`
// substitutes a callee's constant OUTPUTS at its callers;
// `narrow-constant-register-bits cross-multi-instance=true` meets instance
// operands but only to narrow registers. Nothing specialises a module body
// on a constant it is always given.
//
// Measured consequence (cv32a60x): `cva6.sv` ties off the hardware stride
// prefetcher and leaves its outputs dangling -- the upstream RTL flags this
// itself with `/*FIXME*/` -- and the tie-off survives into our IR at the cva6
// top as `hw.constant 0` and `hw.aggregate_constant [0,0,0,0]`. But the
// prefetcher sits three port boundaries down (cva6 -> cva6_hpdcache_subsystem
// -> i_dcache -> hwpf_stride_wrapper), where those signals are block
// arguments, so the constant never reaches it. A boundary-optimising flow
// deletes the whole block; without this pass our netlist carries 3,366
// references to it and ~800 flops.
//
// Callers-first, like NarrowDeadOutputBits, so a specialisation cascades:
// once a caller's own body folds, the constants it passes down become
// visible to the next level. Aggregate literals are handled, since the
// tie-offs here are `hw.aggregate_constant`.
//
// Correctness: the replacement is valid exactly when every instance drives
// the port with the same literal, which is checked directly. The port
// itself is NOT removed, so every instance signature stays valid and no
// caller is rewritten; canonicalize and CSE then collapse what the
// constant enables. A module with no instances (a root) is skipped, since
// its inputs are design inputs. Like the outward pass this changes what a
// module computes for inputs it is never given, so it is sound at the
// DESIGN level only and must be gated by a top-level miter, never a
// per-module one.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct SpecializeConstantInputsPass
    : public PassWrapper<SpecializeConstantInputsPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SpecializeConstantInputsPass)

  SpecializeConstantInputsPass() = default;
  SpecializeConstantInputsPass(const SpecializeConstantInputsPass &other)
      : PassWrapper(other) {
    enablePass = other.enablePass.getValue();
  }

  StringRef getArgument() const final {
    return "specialize-constant-module-inputs";
  }
  StringRef getDescription() const final {
    return "Replace a module input every instance drives with the same "
           "literal by that literal inside the module body";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect>();
  }

  Option<bool> enablePass{
      *this, "enable-specialize-constant-inputs",
      llvm::cl::desc("Enable the rewrite (OFF by default)"),
      llvm::cl::init(false)};

  // The literal an operand carries, or null when it is not a literal.
  static Attribute literalOf(Value v) {
    if (auto c = v.getDefiningOp<hw::ConstantOp>())
      return c.getValueAttr();
    if (auto a = v.getDefiningOp<hw::AggregateConstantOp>())
      return a.getFieldsAttr();
    return {};
  }

  static Operation *materialize(OpBuilder &b, Location loc, Type type,
                                Attribute lit) {
    if (auto intAttr = dyn_cast<IntegerAttr>(lit))
      return hw::ConstantOp::create(b, loc, intAttr);
    if (auto arr = dyn_cast<ArrayAttr>(lit))
      return hw::AggregateConstantOp::create(b, loc, type, arr);
    return nullptr;
  }

  void runOnOperation() override {
    if (!enablePass.getValue())
      return;
    ModuleOp top = getOperation();

    llvm::DenseMap<StringAttr, hw::HWModuleOp> mods;
    SmallVector<hw::HWModuleOp> all;
    for (auto m : top.getOps<hw::HWModuleOp>()) {
      mods[m.getModuleNameAttr()] = m;
      all.push_back(m);
    }

    // Instance graph; Kahn order puts every caller before its callees so a
    // specialisation cascades downward in one sweep.
    llvm::DenseMap<Operation *, unsigned> callerCount;
    llvm::DenseMap<Operation *, SmallVector<hw::HWModuleOp>> callees;
    for (auto m : all) {
      llvm::SmallPtrSet<Operation *, 8> seen;
      for (auto inst : m.getBodyBlock()->getOps<hw::InstanceOp>()) {
        auto callee = mods.lookup(inst.getModuleNameAttr().getAttr());
        if (!callee || !seen.insert(callee).second)
          continue;
        callees[m].push_back(callee);
        ++callerCount[callee];
      }
    }
    SmallVector<hw::HWModuleOp> order, ready;
    llvm::DenseMap<Operation *, unsigned> remaining = callerCount;
    for (auto m : all)
      if (remaining.lookup(m) == 0)
        ready.push_back(m);
    while (!ready.empty()) {
      auto m = ready.pop_back_val();
      order.push_back(m);
      for (auto c : callees[m])
        if (--remaining[c] == 0)
          ready.push_back(c);
    }
    if (order.size() != all.size()) {
      top.emitWarning("specialize-constant-module-inputs: instance graph is "
                      "not a DAG; nothing specialised");
      return;
    }

    // instances of each module, collected once and kept current: a module
    // is only processed after all of its callers, so its instance operands
    // already reflect their own specialisation.
    llvm::DenseMap<Operation *, SmallVector<hw::InstanceOp>> instances;
    for (auto m : all)
      for (auto inst : m.getBodyBlock()->getOps<hw::InstanceOp>())
        if (auto callee = mods.lookup(inst.getModuleNameAttr().getAttr()))
          instances[callee].push_back(inst);

    unsigned modulesTouched = 0, portsSpecialised = 0;
    for (auto m : order) {
      auto &insts = instances[m];
      if (insts.empty())
        continue; // a root: its inputs are design inputs
      Block *body = m.getBodyBlock();
      bool touched = false;
      for (auto [argIdx, arg] : llvm::enumerate(body->getArguments())) {
        if (arg.use_empty())
          continue;
        Attribute lit;
        bool agree = true;
        for (auto inst : insts) {
          if (argIdx >= inst->getNumOperands()) {
            agree = false;
            break;
          }
          Attribute a = literalOf(inst->getOperand(argIdx));
          if (!a || (lit && a != lit)) {
            agree = false;
            break;
          }
          lit = a;
        }
        if (!agree || !lit)
          continue;
        OpBuilder b = OpBuilder::atBlockBegin(body);
        Operation *k = materialize(b, m.getLoc(), arg.getType(), lit);
        if (!k)
          continue;
        arg.replaceAllUsesWith(k->getResult(0));
        ++portsSpecialised;
        touched = true;
      }
      if (touched)
        ++modulesTouched;
    }
    llvm::errs() << "[specialize-constant-module-inputs] " << portsSpecialised
                 << " port(s) in " << modulesTouched << " module(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSpecializeConstantInputsPass() {
  return std::make_unique<SpecializeConstantInputsPass>();
}

} // namespace circt_abc
