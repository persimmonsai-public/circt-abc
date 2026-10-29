//===- NarrowExtendedMultipliesPass.cpp ---------------------*- C++ -*-===//
//
// `--narrow-extended-multiplies` (OFF by default).
//
// CVA6's `multiplier` module contains ONE `comb.mul` at i64 whose operands
// are both `comb.concat(comb.replicate(sign_bit), <i32 value>)` -- a 32-bit
// operand widened to 64. CombToSynth lowers that to a full 64x64 array:
// measured with `circt-opt --convert-comb-to-synth`, an i64 multiply of two
// sign-extended i32 values costs **22,962 AIG nodes, byte-identical to a
// genuine 64x64 multiply of two unconstrained i64 inputs**. The extension is
// not exploited at all, so we build 4,096 partial products where 1,024 are
// needed.
//
// It does exploit CONSTANT-zero high halves (the same multiply with
// zero-extended operands costs 12,914 nodes), but CVA6's high halves are
// `replicate` of a *dynamic* bit -- the sign bit gated by the instruction's
// signed/unsigned control, so that mulh / mulhu / mulhsu share one
// multiplier -- and nothing folds.
//
// Rewrite, for A = concat(replicate(sa), a), B = concat(replicate(sb), b),
// each half W bits wide:
//
//   A == a - sa*2^W  (mod 2^2W)   because the all-ones high half contributes
//                                 2^2W - 2^W, and 2^2W vanishes mod 2^2W
//   A*B == a*b - 2^W*(sa*b + sb*a)              (the sa*sb*2^2W term vanishes)
//
// so
//
//   mul(A, B) = mul(zext a, zext b) - (( sa?b:0 + sb?a:0 ) << W)
//
// The W-bit correction sum is allowed to wrap: only its low W bits survive
// the shift into the high half, so a plain W-bit add is exact here.
//
// The right-hand side hands the lowering two ZERO-extended operands, which
// is the case it already optimises. Measured on the 32-bit shape:
// 22,962 -> 15,200 AIG nodes, **-7,762 (-34%)**, a large share of CVA6's
// combinational excess.
//
// Mixed signedness is handled: an operand whose high half is a constant zero
// simply contributes no correction term, so RISC-V's `mulhsu` shape (one
// signed, one unsigned operand) reduces too. At least one side must be a
// `replicate`, otherwise there is nothing to gain and the op is left alone.
//
// Correctness: the identity above is exact and width-independent, and was
// additionally proven by exhaustive combinational SAT (Yosys
// `miter -equiv -flatten -make_assert` + `sat -prove-asserts`, which
// quantifies over ALL inputs) at 4 and 8 bits per half.
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

struct NarrowExtendedMultipliesPass
    : public PassWrapper<NarrowExtendedMultipliesPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowExtendedMultipliesPass)

  NarrowExtendedMultipliesPass() = default;
  NarrowExtendedMultipliesPass(const NarrowExtendedMultipliesPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    minWidth = other.minWidth.getValue();
  }

  StringRef getArgument() const final { return "narrow-extended-multiplies"; }
  StringRef getDescription() const final {
    return "Rewrite a 2W-bit comb.mul whose operands are W-bit values widened "
           "by a replicated sign bit into a zero-extended multiply plus a "
           "W-bit sign correction";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<bool> enable{
      *this, "enable-narrow-extended-multiplies", llvm::cl::init(false),
      llvm::cl::desc("Enable the rewrite (OFF by default)")};
  Option<unsigned> minWidth{
      *this, "min-half-width", llvm::cl::init(8),
      llvm::cl::desc("Only rewrite when each half is at least this wide -- "
                     "the saving scales with the partial-product count, and "
                     "narrow multiplies are not worth the correction logic")};

  // One operand of the multiply, decomposed. `low` is the W-bit value;
  // `sign` is the i1 whose replication forms the high half, or null when the
  // high half is a constant zero (an unsigned operand, no correction needed).
  struct Half {
    Value low;
    Value sign; // null => zero-extended
  };

  // Match `comb.concat(<W-bit high>, <W-bit low>)` where the high half is
  // either `comb.replicate(i1)` or a zero constant. Anything else -- a
  // non-constant high half, a concat with a different shape, unequal halves
  // -- is not a widened operand and returns nullopt.
  static std::optional<Half> matchHalf(Value v, unsigned halfWidth) {
    auto cat = v.getDefiningOp<comb::ConcatOp>();
    if (!cat || cat.getInputs().size() != 2)
      return std::nullopt;
    Value hi = cat.getInputs()[0], lo = cat.getInputs()[1];
    auto hiTy = dyn_cast<IntegerType>(hi.getType());
    auto loTy = dyn_cast<IntegerType>(lo.getType());
    if (!hiTy || !loTy || hiTy.getWidth() != halfWidth ||
        loTy.getWidth() != halfWidth)
      return std::nullopt;

    if (auto rep = hi.getDefiningOp<comb::ReplicateOp>()) {
      auto inTy = dyn_cast<IntegerType>(rep.getInput().getType());
      if (inTy && inTy.getWidth() == 1)
        return Half{lo, rep.getInput()};
      return std::nullopt;
    }
    APInt hiConst;
    if (matchPattern(hi, m_ConstantInt(&hiConst)) && hiConst.isZero())
      return Half{lo, Value()};
    return std::nullopt;
  }

  void runOnOperation() override {
    if (!enable)
      return;
    unsigned rewritten = 0, skipped = 0;

    getOperation().walk([&](comb::MulOp mul) {
      if (mul.getInputs().size() != 2)
        return;
      auto ty = dyn_cast<IntegerType>(mul.getType());
      if (!ty || ty.getWidth() % 2 != 0)
        return;
      unsigned w = ty.getWidth() / 2;
      if (w < minWidth)
        return;

      auto ha = matchHalf(mul.getInputs()[0], w);
      auto hb = matchHalf(mul.getInputs()[1], w);
      if (!ha || !hb) {
        ++skipped;
        return;
      }
      // Both already zero-extended: the lowering handles that case, and the
      // rewrite would add correction logic for no gain.
      if (!ha->sign && !hb->sign)
        return;

      OpBuilder b(mul);
      Location loc = mul.getLoc();
      Type halfTy = b.getIntegerType(w);
      Value zero = hw::ConstantOp::create(b, loc, APInt::getZero(w));

      Value az = comb::ConcatOp::create(b, loc, ValueRange{zero, ha->low});
      Value bz = comb::ConcatOp::create(b, loc, ValueRange{zero, hb->low});
      Value prod = comb::MulOp::create(b, loc, ValueRange{az, bz}, false);

      // corr = (sa ? b : 0) + (sb ? a : 0), W bits, wrapping is exact here.
      SmallVector<Value, 2> terms;
      if (ha->sign)
        terms.push_back(
            comb::MuxOp::create(b, loc, ha->sign, hb->low, zero, false));
      if (hb->sign)
        terms.push_back(
            comb::MuxOp::create(b, loc, hb->sign, ha->low, zero, false));
      Value corr = terms.size() == 1
                       ? terms[0]
                       : comb::AddOp::create(b, loc, terms, false).getResult();
      (void)halfTy;
      Value corrShifted =
          comb::ConcatOp::create(b, loc, ValueRange{corr, zero});
      Value result = comb::SubOp::create(b, loc, prod, corrShifted, false);

      mul.getResult().replaceAllUsesWith(result);
      mul.erase();
      ++rewritten;
    });

    if (rewritten || skipped)
      llvm::errs() << "[narrow-extended-multiplies] rewrote " << rewritten
                   << " widened multiply(s), left " << skipped
                   << " non-widened\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowExtendedMultipliesPass() {
  return std::make_unique<NarrowExtendedMultipliesPass>();
}

} // namespace circt_abc
