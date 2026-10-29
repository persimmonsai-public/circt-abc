//===- RegisterConstantPropagationPass.cpp -----------------*- C++ -*-===//
//
// `--propagate-register-constants` (ON by default). Measured on real
// ibex_top: a confirmed, SAT-verified-correct true no-op (byte-identical
// struct.mlir with the flag on vs. off) -- the hypothesized CSR
// reserved-bit win (dcsr_q/mstatus_q/cpuctrlsts_part_q) does not
// materialize because those fields live inside reusable per-instance leaf
// modules (ibex_csr/ibex_csr_4/ibex_csr_8), where the caller-side
// constant-driving fact is invisible to this pass's (deliberately)
// per-hw.module-scoped analysis -- see "NOT YET DONE" below. Enabled by
// default anyway because it is real, formally verified infrastructure
// (a real sequential SAT proof via Yosys equiv_induct: 22/22 $equiv cells
// proven) with zero measured behavior change on this design, kept on so
// it's live for whatever design or future cross-instance extension
// benefits from it next, rather than bit-rotting behind an unused flag.
//
// NarrowDeadRegisterBitsPass answers "which bits of an otherwise-live
// register are ever READ" (a liveness question). This pass answers a
// different question: "is this register's VALUE always the same known
// constant, across reset and every reachable update", regardless of
// whether anything reads it. NarrowDeadRegisterBitsPass's own file comment
// documents exactly why this gap matters and is real: ibex's CSR reserved/
// unimplemented bit fields (`dcsr_q`/`mstatus_q`/`cpuctrlsts_part_q` in
// `ibex_cs_registers`) are read (through a wide CSR readback mux), so
// they're correctly LIVE by that pass's analysis, but the value read is
// always the same constant -- closing that needs actual constant
// propagation through the register itself, which is what this pass does.
//
// Scope (deliberately narrow, see "Correctness stance" below): only
// firregs with an async reset and a LITERAL constant `resetValue` --
// exactly register-mapper's own precondition shape (see
// RegisterMapperPass.cpp's `m_ConstantInt` check), which is also what
// RegisterLegalizePass leaves every reset-bearing firreg in by the time
// this pass would run. A firreg with no reset at all is left untouched:
// this pass has no reliable, independently-derived candidate constant to
// test for it (deriving one from `next`'s own internal structure, e.g. a
// reset-mux RegisterLegalizePass folded in, would require materially more
// machinery and is explicitly out of scope here). Only plain scalar-
// integer firregs are handled -- aggregate-typed firregs (`next`'s type is
// not an IntegerType) are skipped outright, same scoping RemoveDeadRegisters
// and NarrowDeadRegisterBits both use.
//
// Algorithm: an optimistic, whole-register fixed-point. For a firreg F with
// constant resetValue C, hypothesize that F's `data` result is ALWAYS C,
// then try to prove `next` also always evaluates to C under that
// hypothesis, by a recursive evaluator understanding the same handful of
// bit-exact comb ops NarrowDeadRegisterBitsPass's liveness walk does
// (`comb.extract`/`comb.concat`/`comb.mux`/`comb.and`/`comb.or`/`comb.xor`,
// plus `hw.constant`/any other op `mlir::m_ConstantInt` recognizes, plus
// an `hw.bitcast` of a constant aggregate -- see `evaluateAggregate`) --
// anything else (arithmetic, comparisons, an `hw.instance`
// result, a module input, a not-yet-confirmed OTHER firreg) makes the
// value UNRESOLVED, and an unresolved operand anywhere in the expression
// makes the whole evaluation fail (this pass never guesses). A
// `comb.mux` with an unresolved condition still resolves if BOTH legs
// independently evaluate to the SAME constant (the common `mux(enable,
// new_value, <this register's own data>)` hold pattern: if `new_value`
// independently evaluates to C too, the mux is C regardless of `enable`).
//
// Whole-MODULE fixed point (not just whole-register): confirming firreg A
// constant can unblock evaluating firreg B's `next` if B's update logic
// reads A's `data` -- so candidates are retried in rounds until a full
// round confirms nothing new, letting a genuine same-module dependency
// chain resolve in whatever order it needs to, rather than requiring a
// single evaluation order to get lucky.
//
// Correctness stance, matching every other pass in this project: when in
// doubt, DO NOT propagate. A false-positive constant here is silent
// mis-synthesis of a real RISC-V core's control logic, categorically worse
// than a missed optimization -- so every operand that can't be resolved
// through the exact op list above (an `hw.bitcast`, an arithmetic op, a
// comparison, a primary input, an `hw.instance` result, a not-yet-
// confirmed firreg) makes evaluation fail outright rather than falling
// back to any kind of best guess. Structural recursion is guarded against
// accidental infinite recursion (should not occur in well-formed acyclic
// comb IR outside the firreg data/next boundary this pass special-cases,
// but never assumed) by an in-progress set that returns "unresolved" on
// re-entry rather than looping.
//
// NOT YET DONE (documented, real limitation, not attempted here): a
// PER-BIT version of this same fixed point (mirroring
// NarrowDeadRegisterBitsPass's per-bit liveness mask, but for
// constant-ness instead) would additionally catch a constant reserved
// field living INSIDE an otherwise genuinely-live, non-constant wider
// register -- this whole-register version only fires when the ENTIRE
// register is provably constant. See this pass's own measured result
// below for whether real ibex_top needed that generalization or not.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

#include <optional>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct RegisterConstantPropagationPass
    : public PassWrapper<RegisterConstantPropagationPass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RegisterConstantPropagationPass)

  RegisterConstantPropagationPass() = default;
  RegisterConstantPropagationPass(const RegisterConstantPropagationPass &other)
      : PassWrapper(other) {
    enablePropagation = other.enablePropagation.getValue();
    foldUnknownOps = other.foldUnknownOps.getValue();
    mutualFixpoint = other.mutualFixpoint.getValue();
    absorbingOperands = other.absorbingOperands.getValue();
  }

  StringRef getArgument() const final {
    return "propagate-register-constants";
  }
  StringRef getDescription() const final {
    return "ON by default (verified no-op on ibex_top, see file comment): "
           "replace a seq.firreg with a literal constant when its reset "
           "value and every reachable update both provably resolve to the "
           "SAME constant";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<bool> enablePropagation{
      *this, "enable-register-constant-propagation",
      llvm::cl::desc("Enable whole-register constant propagation through "
                     "seq.firreg -- ON by default (SAT-verified correct, "
                     "confirmed a true no-op on real ibex_top), see this "
                     "pass's file comment for scope and correctness "
                     "reasoning"),
      llvm::cl::init(true)};

  // OFF by default (project convention for a new lever). See the generic
  // constant fold in evaluateImpl for what it buys and why it is sound.
  Option<bool> foldUnknownOps{
      *this, "fold-unknown-ops",
      llvm::cl::desc("Resolve any pure integer op whose operands all "
                     "evaluate to constants via the op's own folder"),
      llvm::cl::init(false)};

  // OFF by default (project convention for a new lever).
  Option<bool> mutualFixpoint{
      *this, "mutual-fixpoint",
      llvm::cl::desc("Prove a mutually-dependent GROUP of registers "
                     "constant together (greatest fixed point)"),
      llvm::cl::init(false)};

  // OFF by default (project convention for a new lever).
  Option<bool> absorbingOperands{
      *this, "absorbing-operands",
      llvm::cl::desc("Let a zero operand of comb.and (all-ones of comb.or) "
                     "resolve the result even when a sibling operand does "
                     "not"),
      llvm::cl::init(false)};

  // Evaluate `v` under the hypothesis that `target`'s data result equals
  // `hypothesis`, using `confirmed` for other already-proven-constant
  // firregs in this module. Returns std::nullopt if `v` cannot be proven
  // constant through the deliberately narrow op list this pass
  // understands -- never a guess.
  std::optional<APInt>
  evaluate(Value v, Operation *target, const APInt &hypothesis,
           const llvm::DenseMap<Operation *, APInt> &confirmed,
           llvm::DenseMap<Value, std::optional<APInt>> &memo,
           llvm::SmallPtrSetImpl<Value> &inProgress) {
    auto memoIt = memo.find(v);
    if (memoIt != memo.end())
      return memoIt->second;

    if (!inProgress.insert(v).second) {
      // Structural cycle outside the firreg data/next special-case below
      // -- should not happen in well-formed comb IR, but never assume
      // constant on one.
      return std::nullopt;
    }

    std::optional<APInt> result =
        evaluateImpl(v, target, hypothesis, confirmed, memo, inProgress);
    inProgress.erase(v);
    memo[v] = result;
    return result;
  }

  // Evaluate the [lowBit, lowBit+width) slice of `input` (the operand of a
  // comb.extract), pushing the range request through nested
  // comb.extract/comb.concat structurally instead of first evaluating
  // `input`'s FULL value -- necessary so a genuinely-constant slice (e.g.
  // a reserved field concatenated alongside other, unresolvable live
  // bits in the same wider bus) can still be proven even though the
  // WHOLE concatenated value cannot be. Falls back to the generic
  // whole-value evaluator (then slices) once neither push-through
  // applies. Recursion here only ever descends into strictly-upstream
  // (def-use-acyclic) comb operands, so it needs no separate cycle guard
  // beyond `evaluate`'s own for the values it eventually bottoms out at.
  std::optional<APInt>
  evaluateExtract(Value input, unsigned lowBit, unsigned width,
                   Operation *target, const APInt &hypothesis,
                   const llvm::DenseMap<Operation *, APInt> &confirmed,
                   llvm::DenseMap<Value, std::optional<APInt>> &memo,
                   llvm::SmallPtrSetImpl<Value> &inProgress) {
    if (auto innerExt =
            dyn_cast_or_null<comb::ExtractOp>(input.getDefiningOp()))
      return evaluateExtract(innerExt.getInput(), lowBit + innerExt.getLowBit(),
                              width, target, hypothesis, confirmed, memo,
                              inProgress);
    if (auto cat = dyn_cast_or_null<comb::ConcatOp>(input.getDefiningOp())) {
      APInt result = APInt::getZero(width);
      unsigned offset = 0; // LSB-relative offset of the current operand.
      for (int i = static_cast<int>(cat.getInputs().size()) - 1; i >= 0;
           --i) {
        Value operand = cat.getInputs()[i];
        unsigned w = cast<IntegerType>(operand.getType()).getWidth();
        unsigned lo = std::max(offset, lowBit);
        unsigned hi = std::min(offset + w, lowBit + width);
        if (lo < hi) { // this operand overlaps the requested range.
          auto sub = evaluateExtract(operand, lo - offset, hi - lo, target,
                                      hypothesis, confirmed, memo,
                                      inProgress);
          if (!sub)
            return std::nullopt;
          result.insertBits(*sub, lo - lowBit);
        }
        offset += w;
      }
      return result;
    }
    auto whole =
        evaluate(input, target, hypothesis, confirmed, memo, inProgress);
    if (!whole)
      return std::nullopt;
    return whole->extractBits(width, lowBit);
  }

  // Evaluate an AGGREGATE-typed (array/struct) value to the packed
  // integer it bitcasts to. This exists for exactly one shape, which
  // `--lower-aggregate-registers` creates and which the integer evaluator
  // below would otherwise refuse: that pass rewrites an aggregate-typed
  // firreg as an integer one by wrapping `next` in an `hw.bitcast` and
  // packing the `hw.aggregate_constant` reset into an `hw.constant`. The
  // reset side therefore arrives as a literal this pass already matches,
  // while the `next` side arrives behind a bitcast of an aggregate --
  // one op past what `evaluateImpl` understood, so a register that is
  // constant by construction (CVA6's `pmpaddr_q`: `next` and the reset
  // value are the SAME `hw.aggregate_constant`, 2048 bits of it) was
  // being missed for want of seeing through that single cast.
  //
  // Bit order is not guessed. `hw.aggregate_constant` is packed with
  // `hw::aggregateAttrToAPInt`, the same call `--lower-aggregate-registers`
  // used on the reset value and the same one `hw.bitcast`'s own folder
  // uses. `hw.array_create` / `hw.array_concat` / `hw.struct_create` are
  // packed first-operand-at-MSB, which is precisely how HWAggregateToComb
  // lowers all three (`HWArrayCreateLikeOpConversion` and
  // `HWStructCreateOpConversion` both emit `comb.concat` of the operands
  // in order, and `comb.concat` puts its first operand at the MSB) -- so
  // a value proven constant here packs identically to the integer this
  // aggregate is going to become downstream.
  //
  // Integer-typed leaves fall back to the ordinary evaluator, so an
  // aggregate built out of evaluable integer values (not only literals)
  // still resolves. Anything else is unresolved, same stance as
  // everywhere else here: never a guess.
  std::optional<APInt>
  evaluateAggregate(Value v, Operation *target, const APInt &hypothesis,
                    const llvm::DenseMap<Operation *, APInt> &confirmed,
                    llvm::DenseMap<Value, std::optional<APInt>> &memo,
                    llvm::SmallPtrSetImpl<Value> &inProgress) {
    if (isa<IntegerType>(v.getType()))
      return evaluate(v, target, hypothesis, confirmed, memo, inProgress);

    int64_t signedWidth = hw::getBitWidth(v.getType());
    if (signedWidth < 0)
      return std::nullopt; // no static bit width: out of scope.
    unsigned width = static_cast<unsigned>(signedWidth);

    Operation *def = v.getDefiningOp();
    if (!def)
      return std::nullopt; // a real module input.

    if (!inProgress.insert(v).second)
      return std::nullopt; // never assume constant on a structural cycle.
    auto result = evaluateAggregateImpl(def, width, target, hypothesis,
                                        confirmed, memo, inProgress);
    inProgress.erase(v);
    return result;
  }

  std::optional<APInt>
  evaluateAggregateImpl(Operation *def, unsigned width,
                        Operation *target, const APInt &hypothesis,
                        const llvm::DenseMap<Operation *, APInt> &confirmed,
                        llvm::DenseMap<Value, std::optional<APInt>> &memo,
                        llvm::SmallPtrSetImpl<Value> &inProgress) {
    if (auto agg = dyn_cast<hw::AggregateConstantOp>(def)) {
      APInt bits;
      if (failed(hw::aggregateAttrToAPInt(agg.getType(), agg.getFieldsAttr(),
                                          bits)))
        return std::nullopt;
      return bits.zextOrTrunc(width);
    }

    if (auto bitcast = dyn_cast<hw::BitcastOp>(def)) {
      auto inner = evaluateAggregate(bitcast.getInput(), target, hypothesis,
                                     confirmed, memo, inProgress);
      if (!inner || inner->getBitWidth() != width)
        return std::nullopt;
      return inner;
    }

    // First operand at MSB (see the bit-order note above).
    auto packFirstAtMSB = [&](ValueRange operands) -> std::optional<APInt> {
      APInt result = APInt::getZero(width);
      unsigned offset = 0;
      for (Value operand : llvm::reverse(operands)) {
        int64_t w = hw::getBitWidth(operand.getType());
        if (w < 0)
          return std::nullopt;
        auto opv = evaluateAggregate(operand, target, hypothesis, confirmed,
                                     memo, inProgress);
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

  std::optional<APInt>
  evaluateImpl(Value v, Operation *target, const APInt &hypothesis,
               const llvm::DenseMap<Operation *, APInt> &confirmed,
               llvm::DenseMap<Value, std::optional<APInt>> &memo,
               llvm::SmallPtrSetImpl<Value> &inProgress) {
    auto intTy = dyn_cast<IntegerType>(v.getType());
    if (!intTy)
      return std::nullopt; // aggregate/non-integer: out of scope.
    unsigned width = intTy.getWidth();

    APInt literal;
    if (matchPattern(v, m_ConstantInt(&literal)))
      return literal.zextOrTrunc(width);

    Operation *def = v.getDefiningOp();
    if (!def)
      return std::nullopt; // block argument: a real module input.

    if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
      if (firreg.getOperation() == target)
        return hypothesis;
      auto it = confirmed.find(firreg.getOperation());
      if (it != confirmed.end())
        return it->second;
      return std::nullopt; // not (yet, this round) known constant.
    }
    if (auto ext = dyn_cast<comb::ExtractOp>(def))
      return evaluateExtract(ext.getInput(), ext.getLowBit(), width, target,
                              hypothesis, confirmed, memo, inProgress);
    if (auto cat = dyn_cast<comb::ConcatOp>(def)) {
      // MSB-first operand order (this project's established convention).
      APInt result = APInt::getZero(width);
      unsigned offset = 0;
      for (int i = static_cast<int>(cat.getInputs().size()) - 1; i >= 0;
           --i) {
        Value operand = cat.getInputs()[i];
        auto opv = evaluate(operand, target, hypothesis, confirmed, memo,
                             inProgress);
        if (!opv)
          return std::nullopt;
        unsigned w = cast<IntegerType>(operand.getType()).getWidth();
        result.insertBits(*opv, offset);
        offset += w;
      }
      return result;
    }
    if (auto mux = dyn_cast<comb::MuxOp>(def)) {
      auto cond = evaluate(mux.getCond(), target, hypothesis, confirmed,
                            memo, inProgress);
      if (cond)
        return evaluate(cond->isOne() ? mux.getTrueValue()
                                       : mux.getFalseValue(),
                         target, hypothesis, confirmed, memo, inProgress);
      // Condition not resolvable: either leg could run at runtime, so
      // this only resolves if BOTH legs independently agree on the same
      // constant (e.g. the common `mux(enable, new_value, <own data>)`
      // hold pattern, when `new_value` also happens to equal C).
      auto t = evaluate(mux.getTrueValue(), target, hypothesis, confirmed,
                         memo, inProgress);
      if (!t)
        return std::nullopt;
      auto f = evaluate(mux.getFalseValue(), target, hypothesis, confirmed,
                         memo, inProgress);
      if (!f || *t != *f)
        return std::nullopt;
      return t;
    }
    if (auto andOp = dyn_cast<comb::AndOp>(def)) {
      // Absorbing element: a zero bit in any operand forces that bit of
      // the result to zero no matter what the other operands do, so an
      // operand we cannot resolve does not have to sink the whole
      // expression. This is not a micro-optimisation -- it is what lets a
      // tied-off block's enable resolve. CVA6's prefetcher arms on
      // `and(csr_base_q[0], <throttle comparison>)`, where the first
      // operand is zero under the hypothesis and the second depends on an
      // unreset register this pass will never resolve; requiring every
      // operand cost the whole 12-register group.
      APInt result = APInt::getAllOnes(width);
      bool anyUnresolved = false;
      for (Value operand : andOp.getInputs()) {
        auto opv = evaluate(operand, target, hypothesis, confirmed, memo,
                             inProgress);
        if (!opv) {
          if (!absorbingOperands)
            return std::nullopt;
          anyUnresolved = true;
          continue;
        }
        result &= *opv;
        if (absorbingOperands && result.isZero())
          return result; // absorbed; the rest cannot matter.
      }
      return anyUnresolved ? std::nullopt : std::optional<APInt>(result);
    }
    if (auto orOp = dyn_cast<comb::OrOp>(def)) {
      // Dual of the `comb.and` case above: all-ones absorbs.
      APInt result = APInt::getZero(width);
      bool anyUnresolved = false;
      for (Value operand : orOp.getInputs()) {
        auto opv = evaluate(operand, target, hypothesis, confirmed, memo,
                             inProgress);
        if (!opv) {
          if (!absorbingOperands)
            return std::nullopt;
          anyUnresolved = true;
          continue;
        }
        result |= *opv;
        if (absorbingOperands && result.isAllOnes())
          return result;
      }
      return anyUnresolved ? std::nullopt : std::optional<APInt>(result);
    }
    if (auto xorOp = dyn_cast<comb::XorOp>(def)) {
      APInt result = APInt::getZero(width);
      for (Value operand : xorOp.getInputs()) {
        auto opv = evaluate(operand, target, hypothesis, confirmed, memo,
                             inProgress);
        if (!opv)
          return std::nullopt;
        result ^= *opv;
      }
      return result;
    }

    if (auto bitcast = dyn_cast<hw::BitcastOp>(def)) {
      // The `--lower-aggregate-registers` shape: an aggregate packed into
      // this register's integer type. See evaluateAggregate's comment.
      auto packed = evaluateAggregate(bitcast.getInput(), target, hypothesis,
                                      confirmed, memo, inProgress);
      if (!packed || packed->getBitWidth() != width)
        return std::nullopt;
      return *packed;
    }

    // Generic constant fold. Everything above is a hand-written evaluator for
    // an op this pass has always understood; the ops it does NOT understand --
    // comparisons and arithmetic above all -- are what stop a real state
    // machine from resolving. CVA6's tied-off hardware prefetcher is the
    // measured case (cv32a60x, 2026-09-17): its registers hold because their
    // write enables are `comb.icmp ceq %state_q, 0`, so even with `state_q`
    // confirmed constant the enable stayed unresolved and all twelve registers
    // survived, ~800 flops and 3,366 netlist references a boundary-optimising
    // flow does not emit at all.
    //
    // Soundness is the same argument the hand-written cases make, and no
    // weaker: we fold ONLY when every operand has independently resolved
    // to a literal under the current hypothesis, and we delegate the
    // actual arithmetic to the op's own folder rather than re-deriving it
    // here (re-implementing `comb.icmp`'s signedness or a shift's
    // out-of-range behaviour by hand is exactly the kind of silent
    // mis-synthesis this pass refuses to risk). A folder that declines,
    // or returns anything but a single integer attribute, is unresolved.
    //
    // Restricted to pure, region-free, single-result integer ops with at
    // least one operand: side-effecting or region-carrying ops are not
    // evaluable by substitution, and the zero-operand case is already
    // covered by the `m_ConstantInt` match above.
    if (!foldUnknownOps)
      return std::nullopt;
    if (def->getNumResults() != 1 || def->getNumRegions() != 0 ||
        def->getNumOperands() == 0 || !mlir::isPure(def))
      return std::nullopt;

    SmallVector<Attribute> operandConsts;
    operandConsts.reserve(def->getNumOperands());
    for (Value operand : def->getOperands()) {
      auto intOperandTy = dyn_cast<IntegerType>(operand.getType());
      if (!intOperandTy)
        return std::nullopt;
      auto opv =
          evaluate(operand, target, hypothesis, confirmed, memo, inProgress);
      if (!opv)
        return std::nullopt;
      operandConsts.push_back(IntegerAttr::get(intOperandTy, *opv));
    }

    SmallVector<OpFoldResult> foldResults;
    if (failed(def->fold(operandConsts, foldResults)) ||
        foldResults.size() != 1)
      return std::nullopt;
    auto folded = dyn_cast_or_null<IntegerAttr>(
        dyn_cast<Attribute>(foldResults.front()));
    if (!folded)
      return std::nullopt;
    return folded.getValue().zextOrTrunc(width);
  }

  void runOnOperation() override {
    if (!enablePropagation)
      return;
    hw::HWModuleOp module = getOperation();
    Block *body = module.getBodyBlock();

    llvm::DenseMap<Operation *, APInt> confirmed;
    llvm::DenseMap<Operation *, APInt> hypotheses; // candidate -> reset const
    // Program order, so neither fixed point depends on DenseMap iteration
    // order over pointers (which is not stable run to run).
    SmallVector<Operation *> candidates;

    for (auto firreg : body->getOps<seq::FirRegOp>()) {
      if (!isa<IntegerType>(firreg.getNext().getType()))
        continue; // aggregate-typed: out of scope.
      Value reset = firreg.getReset();
      if (!reset)
        continue; // no reliable, independently-derived candidate constant.
      APInt resetConst;
      if (!matchPattern(firreg.getResetValue(), m_ConstantInt(&resetConst)))
        continue; // defensive; register-legalize should already guarantee
                   // this for every reset-bearing firreg reaching here.
      auto width = cast<IntegerType>(firreg.getNext().getType()).getWidth();
      if (hypotheses
              .try_emplace(firreg.getOperation(), resetConst.zextOrTrunc(width))
              .second)
        candidates.push_back(firreg.getOperation());
    }

    if (mutualFixpoint) {
      // Greatest fixed point. The least fixed point below grows `confirmed`
      // from nothing, so it can only ever confirm a register whose `next`
      // depends on registers ALREADY proven -- and a group that is only JOINTLY
      // constant never bootstraps. CVA6's tied-off hardware prefetcher is
      // exactly that shape (cv32a60x, measured 2026-09-17): `state_q`'s next
      // selects on `icmp ceq(state_q, 0)` and its taken leg holds unless
      // `csr_base_q.enable` fires, while `csr_base_q`'s own write enable is a
      // function of `state_q`. Each is constant because the other is; neither
      // is provable alone, and all twelve of that block's registers survive --
      // ~800 flops, and 3,366 netlist references to logic a boundary-optimising
      // flow does not emit at all.
      //
      // So start from the opposite end: ASSUME every candidate sits at its
      // reset value, then delete any whose `next` does not evaluate to that
      // value under the assumption, and repeat until the set is stable.
      //
      // Soundness, by induction on clock cycles. Base: immediately after
      // reset every candidate holds its reset constant, by construction
      // (the scope filter admits only async-reset firregs with a literal
      // reset value). Step: if every survivor holds its reset constant at
      // cycle k, then each survivor's `next` evaluates to its own reset
      // constant -- which is what the sweep verified, using nothing but
      // literals and other survivors' values -- so every survivor still
      // holds it at cycle k+1. A register whose `next` we could not resolve
      // is dropped, never assumed, so the argument never rests on an
      // unproven value. The set only shrinks, so this terminates; and since
      // removals are batched per sweep, the result does not depend on the
      // order candidates are visited in.
      //
      // Note this is strictly stronger than the least fixed point, not an
      // alternative to it: any register the least one confirms is also a
      // survivor here, since its proof never needed an assumption.
      llvm::DenseMap<Operation *, APInt> assumed = hypotheses;
      bool shrank = true;
      while (shrank) {
        shrank = false;
        SmallVector<Operation *> doomed;
        for (Operation *op : candidates) {
          auto it = assumed.find(op);
          if (it == assumed.end())
            continue;
          APInt hyp = it->second;
          auto firreg = cast<seq::FirRegOp>(op);
          llvm::DenseMap<Value, std::optional<APInt>> memo;
          llvm::SmallPtrSet<Value, 16> inProgress;
          auto next =
              evaluate(firreg.getNext(), op, hyp, assumed, memo, inProgress);
          if (!next || *next != hyp) {
            doomed.push_back(op);
            // RCP_DEBUG=<module name substring> reports why a candidate
            // was dropped. The greatest fixed point fails silently -- an
            // unresolved operand deep in a state machine looks exactly
            // like a register that genuinely moves -- and hand-tracing the
            // prefetcher's SSA graph to find the `comb.and` that needed
            // the absorbing rule is not a thing to repeat.
            if (const char *dbg = getenv("RCP_DEBUG"))
              if (module.getModuleName().contains(dbg))
                llvm::errs()
                    << "[rcp-debug] drop "
                    << cast<seq::FirRegOp>(op).getName() << " ("
                    << (next ? "next=" + llvm::toString(*next, 10, false)
                             : std::string("unresolved"))
                    << ", want " << llvm::toString(hyp, 10, false) << ")\n";
          }
        }
        for (Operation *op : doomed) {
          assumed.erase(op);
          shrank = true;
        }
      }
      confirmed = std::move(assumed);
    } else {
      // Least fixed point: a round may confirm a firreg whose `next`
      // depends on another firreg only confirmed in an earlier round.
      bool changed = true;
      while (changed) {
        changed = false;
        for (Operation *op : candidates) {
          if (confirmed.count(op))
            continue;
          APInt hyp = hypotheses.find(op)->second;
          auto firreg = cast<seq::FirRegOp>(op);
          llvm::DenseMap<Value, std::optional<APInt>> memo;
          llvm::SmallPtrSet<Value, 16> inProgress;
          auto next = evaluate(firreg.getNext(), op, hyp, confirmed, memo,
                                inProgress);
          if (next && *next == hyp) {
            confirmed.try_emplace(op, hyp);
            changed = true;
          }
        }
      }
    }

    if (confirmed.empty())
      return;

    for (Operation *op : candidates) {
      auto it = confirmed.find(op);
      if (it == confirmed.end())
        continue;
      auto firreg = cast<seq::FirRegOp>(op);
      OpBuilder builder(firreg);
      Value c = hw::ConstantOp::create(builder, firreg.getLoc(), it->second);
      firreg.getData().replaceAllUsesWith(c);
    }
    for (Operation *op : candidates)
      if (confirmed.count(op))
        cast<seq::FirRegOp>(op).erase();

    llvm::errs() << "[propagate-register-constants] resolved "
                 << confirmed.size() << " firreg(s) to a literal constant in "
                 << module.getModuleName() << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRegisterConstantPropagationPass() {
  return std::make_unique<RegisterConstantPropagationPass>();
}

} // namespace circt_abc
