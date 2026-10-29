//===- LowerClockInvertersPass.cpp ----------------------------*- C++ -*-===//
//
// See Passes.td's LowerClockInverters description for the "why". Short
// version: a negedge-clocked process leaves an
// `seq.from_clock(seq.clock_inv(seq.to_clock(%x)))` chain sitting in ordinary
// combinational logic, where it is nothing but an inverter on an i1 that
// happens to be a clock net. Nothing downstream of the mapper understands
// `seq.clock_inv` -- AIGER is a bit-level format with no clock type at all --
// so the chain blackboxes an otherwise perfectly mappable module.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct LowerClockInvertersPass
    : public PassWrapper<LowerClockInvertersPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerClockInvertersPass)

  LowerClockInvertersPass() = default;
  // No Options, so the implicitly-defaulted copy constructor is fine here --
  // but see ExternalizeMappedCellsPass for why one must be written by hand
  // the moment an Option is added.
  LowerClockInvertersPass(const LowerClockInvertersPass &other) = default;

  StringRef getArgument() const final { return "lower-clock-inverters"; }
  StringRef getDescription() const final {
    return "Rewrite seq clock cast/invert chains back into comb logic";
  }

  void runOnOperation() override {
    ModuleOp module = getOperation();
    size_t numInverted = 0, numIdentity = 0;

    // Walk `seq.from_clock` and look BACKWARDS: that is the single point
    // where a clock value re-enters i1 land, so every chain this pass cares
    // about ends at one. Collect first, rewrite after -- replacing uses
    // while walking would mutate the region under the walker.
    SmallVector<seq::FromClockOp> fromClocks;
    module.walk([&](seq::FromClockOp op) { fromClocks.push_back(op); });

    for (auto fromClock : fromClocks) {
      Value clockVal = fromClock.getInput();
      bool inverted = false;

      // Any odd number of inversions is an inverter, any even number is a
      // pass-through. Chains longer than one appear after CSE merges two
      // negedge users of the same clock.
      while (auto invOp = clockVal.getDefiningOp<seq::ClockInverterOp>()) {
        inverted = !inverted;
        clockVal = invOp.getInput();
      }

      // The chain has to bottom out at a `seq.to_clock`, i.e. the clock came
      // from an i1 in this same body. A clock arriving as a real `!seq.clock`
      // block argument or from a `seq.clock_gate` has no i1 to fall back to,
      // and is left exactly as it was.
      auto toClock = clockVal.getDefiningOp<seq::ToClockOp>();
      if (!toClock)
        continue;

      // An even number of inversions is a pure identity, and rewriting it
      // would be correct -- but it is left ALONE on purpose. Such pairs are
      // common (166 of them on ibex) and harmless: they have always been
      // present and every design maps fine with them, because they never
      // reach AIGER. Rewriting them churns `struct.mlir` design-wide, which
      // costs the byte-identity regression gate its meaning in exchange for
      // no measured benefit. Only the inverting case is a real blocker.
      if (!inverted) {
        ++numIdentity;
        continue;
      }

      OpBuilder builder(fromClock);
      auto one = hw::ConstantOp::create(builder, fromClock.getLoc(),
                                        builder.getI1Type(), 1);
      Value result = comb::XorOp::create(builder, fromClock.getLoc(),
                                         toClock.getInput(), one.getResult(),
                                         /*twoState=*/true)
                         .getResult();
      ++numInverted;

      fromClock.getResult().replaceAllUsesWith(result);
      fromClock.erase();
    }

    // The `seq.clock_inv`/`seq.to_clock` ops themselves are left for
    // canonicalize/DCE: they are pure, so any that no longer feed a real
    // clock port die on their own, and any that DO still feed one (a
    // genuinely negedge-clocked register whose clock port wants a
    // `!seq.clock`) must stay.
    llvm::errs() << "[lower-clock-inverters] rewrote " << numInverted
                 << " inverted clock cast chain(s), left " << numIdentity
                 << " identity pair(s) alone\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createLowerClockInvertersPass() {
  return std::make_unique<LowerClockInvertersPass>();
}

} // namespace circt_abc
