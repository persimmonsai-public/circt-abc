//===- LowerBarrelShiftsPass.cpp ---------------------------------*- C++ -*-===//
//
// `--lower-barrel-shifts`: rewrite every variable comb.shl / shr_u / shr_s
// into log2(width) stages of 2:1 muxes, stage i shifting by 2^i when amount
// bit i is set, plus one out-of-range term (any amount bit at or above
// log2(width)) that forces the zero or sign fill.
//
// Why: CombToSynth lowers a variable shift to a mux TREE whose leaves are the
// `width` distinct constant-shifted copies of the operand, selected by the
// amount bits, then a bounds-check mux. That is log depth but about
// width*(width-1) mux bits and an enormous fanout on the low amount bits,
// where the barrel form has width*log2(width) mux bits, each amount bit
// driving `width` selects. ABC cannot recover the sharing (the tree's
// intermediate values are different functions). Function-preserving: a shift
// by an amount of 2^k or more shifts every bit out, so the stages need no
// special case below that.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/MathExtras.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct LowerBarrelShiftsPass
    : public PassWrapper<LowerBarrelShiftsPass, OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerBarrelShiftsPass)

  LowerBarrelShiftsPass() = default;
  LowerBarrelShiftsPass(const LowerBarrelShiftsPass &o) : PassWrapper(o) {
    enable = o.enable.getValue();
    minWidth = o.minWidth.getValue();
    minAmountBits = o.minAmountBits.getValue();
  }

  Option<bool> enable{*this, "enable", llvm::cl::init(false),
                      llvm::cl::desc("off = no-op (byte-identical)")};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("only rewrite shifts of at least this width")};
  Option<unsigned> minAmountBits{
      *this, "min-amount-bits", llvm::cl::init(2),
      llvm::cl::desc("only rewrite when the amount has more significant bits "
                     "than this (a 1-bit amount is one mux already)")};

  StringRef getArgument() const final { return "lower-barrel-shifts"; }
  StringRef getDescription() const final {
    return "Lower variable shifts to log2(width) stages of 2:1 muxes";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  void runOnOperation() override {
    if (!enable.getValue())
      return;
    hw::HWModuleOp module = getOperation();
    SmallVector<Operation *> work;
    module.walk([&](Operation *op) {
      if (!isa<comb::ShlOp, comb::ShrUOp, comb::ShrSOp>(op))
        return;
      if (op->getOperand(1).getDefiningOp<hw::ConstantOp>())
        return;
      unsigned w = op->getResult(0).getType().getIntOrFloatBitWidth();
      if (w < minWidth.getValue())
        return;
      // Amount bits that can matter: those below ceil(log2(w)) select a
      // stage; anything above is the out-of-range term.
      if (op->getOperand(1).getType().getIntOrFloatBitWidth() <=
          minAmountBits.getValue())
        return;
      work.push_back(op);
    });
    unsigned n = 0;
    for (Operation *op : work) {
      OpBuilder b(op);
      Location loc = op->getLoc();
      Value x = op->getOperand(0), amt = op->getOperand(1);
      unsigned w = x.getType().getIntOrFloatBitWidth();
      unsigned nAmt = amt.getType().getIntOrFloatBitWidth();
      // An amount that is concat(0...0, v) has no out-of-range term: look
      // through the zero extension and work on v.
      if (auto cat = amt.getDefiningOp<comb::ConcatOp>())
        if (cat.getNumOperands() == 2)
          if (auto hiC = cat.getOperand(0).getDefiningOp<hw::ConstantOp>())
            if (hiC.getValue().isZero()) {
              amt = cat.getOperand(1);
              nAmt = amt.getType().getIntOrFloatBitWidth();
            }
      unsigned k = llvm::Log2_32_Ceil(w);
      bool left = isa<comb::ShlOp>(op), arith = isa<comb::ShrSOp>(op);
      Value sign;
      if (arith)
        sign = comb::ExtractOp::create(b, loc, b.getI1Type(), x, w - 1);
      auto fill = [&](unsigned cnt) -> Value {
        if (arith)
          return comb::ReplicateOp::create(b, loc, b.getIntegerType(cnt), sign);
        return hw::ConstantOp::create(b, loc, b.getIntegerType(cnt), 0);
      };
      Value r = x;
      for (unsigned i = 0; i < k && i < nAmt; ++i) {
        unsigned sh = 1u << i; // < w by the choice of k
        Value sel = comb::ExtractOp::create(b, loc, b.getI1Type(), amt, i);
        Value moved;
        if (left)
          moved = comb::ConcatOp::create(
              b, loc,
              ValueRange{comb::ExtractOp::create(b, loc, b.getIntegerType(w - sh),
                                                 r, 0),
                         fill(sh)});
        else
          moved = comb::ConcatOp::create(
              b, loc,
              ValueRange{fill(sh),
                         comb::ExtractOp::create(b, loc, b.getIntegerType(w - sh),
                                                 r, sh)});
        r = comb::MuxOp::create(b, loc, sel, moved, r);
      }
      if (nAmt > k) {
        Value hi = comb::ExtractOp::create(b, loc, b.getIntegerType(nAmt - k),
                                           amt, k);
        Value zero = hw::ConstantOp::create(b, loc, b.getIntegerType(nAmt - k), 0);
        Value oob = comb::ICmpOp::create(b, loc, comb::ICmpPredicate::ne, hi,
                                         zero);
        r = comb::MuxOp::create(b, loc, oob, fill(w), r);
      }
      op->getResult(0).replaceAllUsesWith(r);
      op->erase();
      ++n;
    }
    if (n)
      llvm::errs() << "[lower-barrel-shifts] " << module.getModuleName() << ": "
                   << n << " shift(s) rewritten\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createLowerBarrelShiftsPass() {
  return std::make_unique<LowerBarrelShiftsPass>();
}

} // namespace circt_abc
