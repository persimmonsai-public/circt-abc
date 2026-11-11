//===- LowerCarryInAddsPass.cpp ----------------------------------*- C++ -*-===//
//
// `--lower-carry-in-adds`: rewrite a three-operand comb.add whose third
// operand is a zero-extended single bit, `a + b + zext(c)`, into ONE two-operand
// add one bit wider, `{a,1} + {b,c}`, whose upper bits are the result.
//
// Why: CombToSynth only recognises a carry-in that is the constant 1. Any other
// three-operand add goes through its variadic lowering, which splits it into
// two serial binary adds, `a + (b + c)`, and drops the `synth.test.arch`
// attribute on the way, so both halves get the width heuristic's architecture.
// The result is an incrementer followed by an adder: a conditional-invert
// add/subtract (`a + (b ^ sub) + sub`, the form every add-or-subtract unit is
// written in) pays two carry-propagate delays for a single carry-in.
//
// The identity: bit 0 of {a,1} + {b,c} is 1 + c, which has sum ~c and carry c,
// so bits [w:1] equal a + b + c exactly (mod 2^w). The prefix adder's first
// level absorbs the carry-in at no depth cost. Function-preserving; proven by a
// Yosys SAT miter (scripts/circt_flow/tests/test_arith_lowering.py).
//
// The architecture attribute (set-adder-arch / dp-apply-arch) is copied to the
// new add, so run this pass before them or after: either order works.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// If `v` is concat(0...0, x : i1) (or the equivalent zero-extension), return x.
static Value matchZeroExtendedBit(Value v) {
  auto ty = dyn_cast<IntegerType>(v.getType());
  if (!ty || ty.getWidth() < 2)
    return {};
  auto cat = v.getDefiningOp<comb::ConcatOp>();
  if (!cat || cat.getNumOperands() != 2)
    return {};
  auto hi = cat.getOperand(0).getDefiningOp<hw::ConstantOp>();
  if (!hi || !hi.getValue().isZero())
    return {};
  Value lo = cat.getOperand(1);
  if (!lo.getType().isInteger(1))
    return {};
  return lo;
}

struct LowerCarryInAddsPass
    : public PassWrapper<LowerCarryInAddsPass, OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerCarryInAddsPass)

  LowerCarryInAddsPass() = default;
  LowerCarryInAddsPass(const LowerCarryInAddsPass &o) : PassWrapper(o) {
    enable = o.enable.getValue();
    minWidth = o.minWidth.getValue();
  }

  Option<bool> enable{*this, "enable", llvm::cl::init(false),
                      llvm::cl::desc("off = no-op (byte-identical)")};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("only rewrite adds at or above this width")};

  StringRef getArgument() const final { return "lower-carry-in-adds"; }
  StringRef getDescription() const final {
    return "Rewrite a + b + zext(c) into one wider two-operand add so the "
           "adder architecture applies to a single carry-propagate add";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  void runOnOperation() override {
    if (!enable.getValue())
      return;
    hw::HWModuleOp module = getOperation();
    SmallVector<comb::AddOp> work;
    module.walk([&](comb::AddOp add) {
      if (add.getNumOperands() != 3 || add.getType().getIntOrFloatBitWidth() <
                                           minWidth.getValue())
        return;
      work.push_back(add);
    });
    unsigned n = 0;
    for (comb::AddOp add : work) {
      int cin = -1;
      Value cbit;
      for (unsigned i = 0; i < 3 && cin < 0; ++i)
        if (Value x = matchZeroExtendedBit(add.getOperand(i))) {
          // A constant carry-in is CombToSynth's own fast path: leave it.
          if (x.getDefiningOp<hw::ConstantOp>())
            continue;
          cin = i;
          cbit = x;
        }
      if (cin < 0)
        continue;
      Value a = add.getOperand(cin == 0 ? 1 : 0);
      Value b = add.getOperand(cin == 2 ? 1 : 2);
      unsigned w = add.getType().getIntOrFloatBitWidth();
      OpBuilder bld(add);
      Location loc = add.getLoc();
      Value one = hw::ConstantOp::create(bld, loc, APInt(1, 1));
      Value a1 = comb::ConcatOp::create(bld, loc, ValueRange{a, one});
      Value b1 = comb::ConcatOp::create(bld, loc, ValueRange{b, cbit});
      auto wide = comb::AddOp::create(bld, loc, ValueRange{a1, b1}, true);
      if (auto arch = add->getAttr("synth.test.arch"))
        wide->setAttr("synth.test.arch", arch);
      Value res = comb::ExtractOp::create(bld, loc, add.getType(), wide, 1);
      (void)w;
      add.getResult().replaceAllUsesWith(res);
      add.erase();
      ++n;
    }
    if (n)
      llvm::errs() << "[lower-carry-in-adds] " << module.getModuleName() << ": "
                   << n << " add(s) rewritten\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createLowerCarryInAddsPass() {
  return std::make_unique<LowerCarryInAddsPass>();
}

} // namespace circt_abc
