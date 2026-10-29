//===- RegisterLegalizePass.cpp -------------------------------*- C++ -*-===//
//
// See Passes.td's RegisterLegalize description for the "why". This pass
// runs per hw::HWModuleOp, walking its seq::FirRegOp ops and folding any
// whose reset is NOT already in a liberty-cell-mappable shape (see
// RegisterMapperPass.cpp) into plain D-input mux logic:
//
//   sync reset (isAsync=false):    the target liberty cells have no
//     separate sync-reset variant at all -- a sync reset is always really
//     implemented as `next = mux(reset, resetValue, originalNext)` feeding
//     a plain (no-reset-pin) register, so this is the only correct
//     lowering, not merely a convenient fallback.
//   async reset with a NON-constant resetValue: the target cells' clear/
//     preset pins can only drive their output to a hard-wired 0 or 1 (the
//     transistor-level clear/set structure has no way to load an arbitrary
//     value), so a non-constant resetValue cannot be expressed by an async
//     clear/preset pin either -- same mux-into-D-input fallback applies.
//
// A firreg with no reset, or an async reset whose resetValue is a constant,
// is already exactly the shape RegisterMapperPass expects and is left
// untouched.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "llvm/ADT/APInt.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct RegisterLegalizePass
    : public PassWrapper<RegisterLegalizePass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RegisterLegalizePass)

  StringRef getArgument() const final { return "register-legalize"; }
  StringRef getDescription() const final {
    return "Fold sync/non-constant-reset seq.firreg ops into D-input mux "
           "logic, ahead of register-mapper";
  }

  // Hand-rolled PassWrapper, not a TableGen `*Base<>` (see ABCBridge.cpp's
  // header comment for the "why" shared across this whole project) -- must
  // manually declare the dialects this pass's own IR-building code
  // introduces (comb.mux) plus the one it walks (seq.firreg).
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, seq::SeqDialect>();
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();
    size_t numFolded = 0;

    for (auto firreg : llvm::make_early_inc_range(
             module.getBodyBlock()->getOps<seq::FirRegOp>())) {
      Value reset = firreg.getReset();
      if (!reset)
        continue; // no reset at all: already mappable as-is.

      Value resetValue = firreg.getResetValue();
      APInt constResetValue;
      bool isConstant =
          matchPattern(resetValue, m_ConstantInt(&constResetValue));
      if (firreg.getIsAsync() && isConstant)
        continue; // already the shape register-mapper expects.

      // Fold: next := mux(reset, resetValue, next); drop reset entirely.
      OpBuilder builder(firreg);
      Value muxedNext = comb::MuxOp::create(builder, firreg.getLoc(), reset,
                                             resetValue, firreg.getNext());
      auto newFirreg = seq::FirRegOp::create(
          builder, firreg.getLoc(), muxedNext, firreg.getClk(),
          firreg.getNameAttr(), firreg.getInnerSymAttr(),
          firreg.getPresetAttr());
      firreg.getData().replaceAllUsesWith(newFirreg.getData());
      firreg.erase();
      ++numFolded;
    }

    llvm::errs() << "[register-legalize] folded " << numFolded
                 << " sync/non-constant-reset registers into D-input logic\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRegisterLegalizePass() {
  return std::make_unique<RegisterLegalizePass>();
}

} // namespace circt_abc
