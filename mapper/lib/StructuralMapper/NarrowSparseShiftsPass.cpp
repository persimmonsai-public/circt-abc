//===- NarrowSparseShiftsPass.cpp ----------------------------*- C++ -*-===//
//
// `--narrow-sparse-shifts` (OFF by default).
//
// Second instance of the same blind spot `--narrow-extended-multiplies`
// fixes: CombToSynth does not look at where a shift's AMOUNT came from.
//
// CVA6's `cva6_icache` contains six 128-bit `comb.shru` whose amount is
//
//     comb.concat %c0_i121, %in45, %in46, %c0_i5 : i121, i1, i1, i5
//
// i.e. 121 constant-zero high bits, two variable bits, five constant-zero
// low bits -- the shift amount is provably one of {0, 32, 64, 96}, the
// classic "select a 32-bit word from a cache line at a variable offset".
// Measured with `circt-opt --convert-comb-to-synth`:
//
//     128-bit shru, amount = concat(0, s0, s1, 0)   1,788 AIG nodes
//     128-bit shru, amount fully variable           1,788   <- IDENTICAL
//     the equivalent 4:1 mux of aligned slices          12
//
// A full 7-stage barrel shifter is built, of which the five stages shifting
// by 1/2/4/8/16 are provably dead. Those six shifts are 23.4% of CVA6's
// entire combinational AIG.
//
// Rewrite: when the amount is a `comb.concat` whose non-constant operands
// contribute at most `max-variable-bits` bits in total, enumerate the
// 2^n reachable amounts, emit `comb.shru`/`shl`/`shrs` by each CONSTANT
// amount (which the lowering turns into a free extract+concat), and select
// with a mux tree on the variable bits. n is capped because the tree is
// 2^n wide; at n = 2 that is four constant shifts and three muxes.
//
// Applies to shru / shl / shrs alike -- the amount is the operand whose
// provenance matters, not the direction.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

#include <optional>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct NarrowSparseShiftsPass
    : public PassWrapper<NarrowSparseShiftsPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowSparseShiftsPass)

  NarrowSparseShiftsPass() = default;
  NarrowSparseShiftsPass(const NarrowSparseShiftsPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    maxVariableBits = other.maxVariableBits.getValue();
  }

  StringRef getArgument() const final { return "narrow-sparse-shifts"; }
  StringRef getDescription() const final {
    return "Rewrite a variable shift whose amount has few non-constant bits "
           "into a mux over the reachable constant shift amounts";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<bool> enable{
      *this, "enable-narrow-sparse-shifts", llvm::cl::init(false),
      llvm::cl::desc("Enable the rewrite (OFF by default)")};
  Option<unsigned> maxVariableBits{
      *this, "max-variable-bits", llvm::cl::init(3),
      llvm::cl::desc("Only rewrite when the shift amount has at most this "
                     "many non-constant bits (the mux tree is 2^n wide)")};

  // One variable bit of the amount, and the amount-bit position it sits at.
  struct VarBit {
    Value bit;       // i1
    unsigned weight; // bit position within the amount
  };

  // Decompose a shift amount into (constant base, variable bits). Returns
  // nullopt unless every operand of the amount's `comb.concat` is either a
  // constant or a single-bit value -- a multi-bit variable operand would
  // need its own decomposition and is left alone.
  static std::optional<std::pair<APInt, SmallVector<VarBit, 4>>>
  decompose(Value amt, unsigned width) {
    auto cat = amt.getDefiningOp<comb::ConcatOp>();
    if (!cat)
      return std::nullopt;
    APInt base = APInt::getZero(width);
    SmallVector<VarBit, 4> vars;
    // comb.concat is MSB-first; walk from the LSB end tracking the offset.
    unsigned offset = 0;
    for (Value in : llvm::reverse(cat.getInputs())) {
      auto inTy = dyn_cast<IntegerType>(in.getType());
      if (!inTy)
        return std::nullopt;
      unsigned w = inTy.getWidth();
      APInt c;
      if (matchPattern(in, m_ConstantInt(&c))) {
        base.insertBits(c.zextOrTrunc(w), offset);
      } else if (w == 1) {
        vars.push_back({in, offset});
      } else {
        return std::nullopt; // a wide variable field: out of scope
      }
      offset += w;
    }
    return std::make_pair(base, vars);
  }

  void runOnOperation() override {
    if (!enable)
      return;
    unsigned rewritten = 0;
    SmallVector<Operation *, 8> dead;

    getOperation().walk([&](Operation *op) {
      Value lhs, amt;
      if (auto s = dyn_cast<comb::ShrUOp>(op)) { lhs = s.getLhs(); amt = s.getRhs(); }
      else if (auto s = dyn_cast<comb::ShlOp>(op)) { lhs = s.getLhs(); amt = s.getRhs(); }
      else if (auto s = dyn_cast<comb::ShrSOp>(op)) { lhs = s.getLhs(); amt = s.getRhs(); }
      else return;

      auto ty = dyn_cast<IntegerType>(op->getResult(0).getType());
      if (!ty)
        return;
      unsigned width = ty.getWidth();
      auto d = decompose(amt, width);
      if (!d)
        return;
      auto &[base, vars] = *d;
      unsigned n = vars.size();
      // Nothing to gain if the amount is fully variable in this width, and
      // the tree must stay small.
      if (n == 0 || n > maxVariableBits)
        return;

      OpBuilder b(op);
      Location loc = op->getLoc();
      unsigned combos = 1u << n;
      SmallVector<Value, 16> leaves(combos);
      for (unsigned m = 0; m < combos; ++m) {
        APInt a = base;
        for (unsigned i = 0; i < n; ++i)
          a.setBitVal(vars[i].weight, (m >> i) & 1);
        Value c = hw::ConstantOp::create(b, loc, a);
        Value shifted;
        if (isa<comb::ShrUOp>(op))
          shifted = comb::ShrUOp::create(b, loc, lhs, c, false);
        else if (isa<comb::ShlOp>(op))
          shifted = comb::ShlOp::create(b, loc, lhs, c, false);
        else
          shifted = comb::ShrSOp::create(b, loc, lhs, c, false);
        leaves[m] = shifted;
      }
      // Mux tree, LSB variable bit selected innermost.
      for (unsigned i = 0; i < n; ++i) {
        unsigned half = 1u << (n - i - 1);
        for (unsigned j = 0; j < half; ++j)
          leaves[j] = comb::MuxOp::create(b, loc, vars[n - i - 1].bit,
                                          leaves[j + half], leaves[j], false);
        leaves.resize(half ? half : 1);
      }
      op->getResult(0).replaceAllUsesWith(leaves[0]);
      dead.push_back(op);
      ++rewritten;
    });

    for (Operation *op : dead)
      op->erase();
    if (rewritten)
      llvm::errs() << "[narrow-sparse-shifts] rewrote " << rewritten
                   << " shift(s) with a sparse amount\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowSparseShiftsPass() {
  return std::make_unique<NarrowSparseShiftsPass>();
}

} // namespace circt_abc
