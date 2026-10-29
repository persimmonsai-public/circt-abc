//===- LowerAggregateRegistersPass.cpp ---------------------*- C++ -*-===//
//
// `--lower-aggregate-registers`: rewrite every aggregate-typed (struct /
// array) `seq.firreg` as an integer-typed one of the same bit width, with
// `hw.bitcast`s on its next input and data output and its
// `hw.aggregate_constant` reset value packed into an `hw.constant`.
//
// Why: RegisterLegalize keeps a reset only when the reset value matches
// `m_ConstantInt`; an aggregate register's reset is an
// `hw.aggregate_constant`, which it treats as "non-constant" and FOLDS the
// asynchronous reset into the D-input logic -- an async reset silently
// becomes a synchronous one. On cv32a60x that touched 49 registers
// (csr_buffer, csr_regfile's mstatus/pmpcfg/pmpaddr, issue/fu_data,
// branch_predict, load buffers, ...). The 2026-09-02 whole-design miter
// with a free reset input caught it on `rvfi_probes_o` at a step where
// the solver re-asserted reset: gold shows the reset value at once, the
// folded gate register only at the next clock edge. Only observable while
// reset is asserted, but a deviation from the RTL nonetheless, and the
// legalized register also costs a reset mux in logic instead of the
// library's async-reset flop.
//
// Placement: after narrow-array-registers (which needs the array type) and
// before register-legalize. The bit packing is CIRCT's own
// (`hw::aggregateAttrToAPInt`, the same convention HWAggregateToComb uses
// for the value side), so the later aggregate lowering of the bitcasts is
// exact. Registers with a preset attribute, or a reset value that is not
// an aggregate constant, are left alone (RegisterLegalize handles them as
// before). ibex has one reset-less struct register (the load-store
// unit's capability permissions): lowered too, area-neutral.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/HW/HWTypes.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/raw_ostream.h"

#include <optional>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct LowerAggregateRegistersPass
    : public PassWrapper<LowerAggregateRegistersPass,
                         OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerAggregateRegistersPass)

  StringRef getArgument() const final { return "lower-aggregate-registers"; }
  StringRef getDescription() const final {
    return "Rewrite aggregate-typed seq.firregs as integer-typed ones with "
           "bitcasts and a packed constant reset, so async resets with "
           "aggregate-constant values survive register-legalize";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, seq::SeqDialect>();
  }

  // An aggregate reset value is usually a single `hw.aggregate_constant`,
  // but it does not have to be: CVA6's `csr_regfile.pmpcfg_q` resets to
  // `hw.array_create` of 64 copies of one constant struct, which
  // `--canonicalize` does not collapse into an aggregate constant. Fold
  // that structural form here rather than skipping the register, so a
  // 512-bit provably-constant register is not left behind purely because
  // of how its reset literal happens to be spelled.
  //
  // Bit order matches HWAggregateToComb, which lowers `hw.array_create`,
  // `hw.array_concat` and `hw.struct_create` alike to a `comb.concat` of
  // their operands in order -- i.e. FIRST operand at the MSB -- and
  // `hw.aggregate_constant` via `hw::aggregateAttrToAPInt`, the same call
  // used directly below. Anything that is not one of those forms bottoming
  // out at literals is not folded (returns nullopt), never guessed at.
  static std::optional<APInt> packConstantAggregate(Value v, unsigned depth = 0) {
    if (depth > 64)
      return std::nullopt; // defensive: never recurse unboundedly.
    int64_t signedWidth = hw::getBitWidth(v.getType());
    if (signedWidth < 0)
      return std::nullopt;
    unsigned width = static_cast<unsigned>(signedWidth);

    APInt literal;
    if (mlir::matchPattern(v, mlir::m_ConstantInt(&literal)))
      return literal.zextOrTrunc(width);

    Operation *def = v.getDefiningOp();
    if (!def)
      return std::nullopt;

    if (auto agg = dyn_cast<hw::AggregateConstantOp>(def)) {
      APInt bits;
      if (failed(hw::aggregateAttrToAPInt(agg.getType(), agg.getFieldsAttr(),
                                          bits)))
        return std::nullopt;
      return bits.zextOrTrunc(width);
    }
    if (auto bitcast = dyn_cast<hw::BitcastOp>(def)) {
      auto inner = packConstantAggregate(bitcast.getInput(), depth + 1);
      if (!inner || inner->getBitWidth() != width)
        return std::nullopt;
      return inner;
    }

    auto packFirstAtMSB = [&](ValueRange operands) -> std::optional<APInt> {
      APInt result = APInt::getZero(width);
      unsigned offset = 0;
      for (Value operand : llvm::reverse(operands)) {
        int64_t w = hw::getBitWidth(operand.getType());
        if (w < 0)
          return std::nullopt;
        auto opv = packConstantAggregate(operand, depth + 1);
        if (!opv || opv->getBitWidth() != static_cast<unsigned>(w))
          return std::nullopt;
        if (offset + static_cast<unsigned>(w) > width)
          return std::nullopt;
        result.insertBits(*opv, offset);
        offset += static_cast<unsigned>(w);
      }
      if (offset != width)
        return std::nullopt;
      return result;
    };

    if (auto arrayCreate = dyn_cast<hw::ArrayCreateOp>(def))
      return packFirstAtMSB(arrayCreate.getInputs());
    if (auto arrayConcat = dyn_cast<hw::ArrayConcatOp>(def))
      return packFirstAtMSB(arrayConcat.getInputs());
    if (auto structCreate = dyn_cast<hw::StructCreateOp>(def))
      return packFirstAtMSB(structCreate.getInput());
    return std::nullopt;
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();
    unsigned lowered = 0, skipped = 0;
    for (auto firreg : llvm::make_early_inc_range(
             module.getBodyBlock()->getOps<seq::FirRegOp>())) {
      Type ty = firreg.getType();
      if (isa<IntegerType>(ty))
        continue;
      int64_t width = hw::getBitWidth(ty);
      if (width <= 0 || firreg.getPresetAttr()) {
        ++skipped;
        continue;
      }
      OpBuilder b(firreg);
      Location loc = firreg.getLoc();
      Type intTy = b.getIntegerType(width);
      Value resetValue, reset = firreg.getReset();
      if (reset) {
        auto bits = packConstantAggregate(firreg.getResetValue());
        if (!bits) {
          ++skipped; // leave to register-legalize's existing handling
          continue;
        }
        resetValue =
            hw::ConstantOp::create(b, loc, bits->zextOrTrunc(width));
      }
      Value nextI = hw::BitcastOp::create(b, loc, intTy, firreg.getNext());
      seq::FirRegOp newReg;
      if (reset)
        newReg = seq::FirRegOp::create(b, loc, nextI, firreg.getClk(),
                                       firreg.getNameAttr(), reset, resetValue,
                                       firreg.getInnerSymAttr(),
                                       firreg.getIsAsync(),
                                       firreg.getPresetAttr());
      else
        newReg = seq::FirRegOp::create(b, loc, nextI, firreg.getClk(),
                                       firreg.getNameAttr(),
                                       firreg.getInnerSymAttr(),
                                       firreg.getPresetAttr());
      Value dataAgg = hw::BitcastOp::create(b, loc, ty, newReg.getData());
      firreg.getData().replaceAllUsesWith(dataAgg);
      firreg.erase();
      ++lowered;
    }
    if (lowered || skipped)
      llvm::errs() << "[lower-aggregate-registers] " << module.getModuleName()
                   << ": " << lowered << " aggregate firreg(s) lowered to "
                   << "integer, " << skipped << " left\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createLowerAggregateRegistersPass() {
  return std::make_unique<LowerAggregateRegistersPass>();
}

} // namespace circt_abc
