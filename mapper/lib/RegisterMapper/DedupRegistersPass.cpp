//===- DedupRegistersPass.cpp -------------------------------*- C++ -*-===//
//
// `--dedup-registers` (OFF by default -- landing convention for new
// correctness-affecting passes: land off, measure, promote deliberately).
//
// Targets the second source of this flow's register-count gap against
// Yosys: deliberate RTL register REPLICATION that
// Yosys's `opt_merge` undoes but nothing in this flow did (+48 DFF bits on
// real ibex_top). Both real instances live in ibex_if_stage:
//
//   %67 = comb.mux bin %en, %instr, %instr_rdata_id_o : i32
//   %instr_rdata_id_o     = seq.firreg %67 clock %clk : i32
//   %68 = comb.mux bin %en, %instr, %instr_rdata_alu_id_o : i32
//   %instr_rdata_alu_id_o = seq.firreg %68 clock %clk : i32
//
// (and the same shape for the i16 pair instr_rdata_c_id_o /
// instr_expanded_id_o). The RTL comment documents the replica as a
// fanout-reduction copy -- functionally identical by construction, kept
// distinct purely as a physical-design hint.
//
// CRITICAL MATCHING SUBTLETY (why a naive "same next SSA value" hash can
// never fire): each replica's next-value is a hold-mux that references its
// OWN register result, so the two next values are always DIFFERENT SSA
// values. Two registers are true duplicates when their next values are
// structurally identical *modulo substituting each register's own result
// for the other's*. This pass implements exactly that: a bounded,
// conservative structural-equivalence walk where the only permitted
// non-literal operand match is the pair (A's own result, B's own result),
// in that orientation only. Cross-coupled registers (A's next reading B
// and vice versa) deliberately do NOT match -- their equality would depend
// on initial-state assumptions this pass refuses to make.
//
// Merge conditions (ALL required; when in doubt, don't merge):
//   - same result type, same clock (literal SSA value);
//   - resets: both absent, or literally the same reset SSA value with
//     equivalent reset values (under the same bounded walk);
//   - equal preset attributes (or both absent);
//   - the duplicate carries no inner symbol (someone could be referring to
//     it hierarchically); the survivor may keep its own;
//   - next values structurally equivalent under the self-substitution rule,
//     through PURE ops only (comb dialect + hw.constant), with attribute
//     dictionaries equal except `sv.namehint`, to a small depth bound.
//
// Initial-state caveat (shared with Yosys `opt_merge`, documented rather
// than hidden): for no-reset registers, merging assumes both replicas hold
// equal values from the start. In 4-state semantics both power up X; in
// the 2-state model every equivalence check in this project uses (and that
// Yosys's own published baseline netlist embodies -- it ships the merged
// version), the merge is sound. Registers whose value is consumed before
// first write would be X-indeterminate in EITHER form.
//
// The merge itself: RAUW the duplicate's result with the survivor's and
// erase the duplicate. The duplicate's now-dead next cone (its private
// hold-mux) is left for the pipeline's existing --canonicalize DCE.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct DedupRegistersPass
    : public PassWrapper<DedupRegistersPass, OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(DedupRegistersPass)

  DedupRegistersPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  DedupRegistersPass(const DedupRegistersPass &other) : PassWrapper(other) {
    enableDedup = other.enableDedup.getValue();
  }

  StringRef getArgument() const final { return "dedup-registers"; }
  StringRef getDescription() const final {
    return "Prototype (OFF by default): merge exact-duplicate seq.firreg "
           "replicas (identical next/clock/reset modulo each register's "
           "own self-reference in its hold path)";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<bool> enableDedup{
      *this, "enable-register-dedup",
      llvm::cl::desc("Enable duplicate-register merging -- OFF by default "
                      "(unpromoted; undoes deliberate RTL fanout-reduction "
                      "replication, adopt only with real post-P&R timing "
                      "data -- see this pass's file comment)"),
      llvm::cl::init(false)};

  // Structural equivalence of `a` and `b`, where the ONLY permitted
  // non-literal leaf match is (a == selfA && b == selfB) -- each side's
  // reference to its own register, same orientation. Walks only pure ops
  // (comb dialect, hw.constant); any other defining op, block argument
  // mismatch, or exhausted depth fails the match.
  static bool equivalent(Value a, Value b, Value selfA, Value selfB,
                         unsigned depth) {
    if (a == b)
      return true;
    if (a == selfA && b == selfB)
      return true;
    if (depth == 0)
      return false;
    Operation *defA = a.getDefiningOp();
    Operation *defB = b.getDefiningOp();
    if (!defA || !defB)
      return false;
    if (defA->getName() != defB->getName())
      return false;
    // Pure-op allowlist: comb dialect ops and hw.constant only. Anything
    // with regions, side effects, or unknown semantics fails conservatively.
    bool pureA = isa<comb::CombDialect>(defA->getDialect()) ||
                 isa<hw::ConstantOp>(defA);
    if (!pureA || defA->getNumRegions() != 0)
      return false;
    if (defA->getNumResults() != 1 || defB->getNumResults() != 1)
      return false;
    if (defA->getNumOperands() != defB->getNumOperands())
      return false;
    // Attribute dictionaries must match, ignoring the semantically-inert
    // sv.namehint carried by many comb ops.
    auto strip = [](Operation *op) {
      SmallVector<NamedAttribute> kept;
      for (NamedAttribute na : op->getAttrs())
        if (na.getName() != "sv.namehint")
          kept.push_back(na);
      return kept;
    };
    SmallVector<NamedAttribute> attrsA = strip(defA), attrsB = strip(defB);
    if (attrsA.size() != attrsB.size())
      return false;
    for (auto [na, nb] : llvm::zip(attrsA, attrsB))
      if (na.getName() != nb.getName() || na.getValue() != nb.getValue())
        return false;
    for (auto [oa, ob] : llvm::zip(defA->getOperands(), defB->getOperands()))
      if (!equivalent(oa, ob, selfA, selfB, depth - 1))
        return false;
    return true;
  }

  // Whether `dup` can be merged into `keep` (replacing all of dup's uses
  // with keep's result). Conservative: every condition must hold exactly.
  static bool canMerge(seq::FirRegOp keep, seq::FirRegOp dup) {
    if (keep == dup)
      return false;
    if (keep.getResult().getType() != dup.getResult().getType())
      return false;
    if (keep.getClk() != dup.getClk())
      return false;
    // The duplicate must not be hierarchically referenced.
    if (dup.getInnerSymAttr())
      return false;
    if (keep.getPresetAttr() != dup.getPresetAttr())
      return false;
    Value rK = keep.getReset(), rD = dup.getReset();
    if (static_cast<bool>(rK) != static_cast<bool>(rD))
      return false;
    constexpr unsigned kDepth = 4;
    if (rK) {
      if (rK != rD)
        return false;
      if (!equivalent(keep.getResetValue(), dup.getResetValue(),
                      keep.getResult(), dup.getResult(), kDepth))
        return false;
    }
    return equivalent(keep.getNext(), dup.getNext(), keep.getResult(),
                      dup.getResult(), kDepth);
  }

  void runOnOperation() override {
    if (!enableDedup.getValue())
      return;
    hw::HWModuleOp module = getOperation();
    unsigned merged = 0;
    bool changed = true;
    while (changed) {
      changed = false;
      SmallVector<seq::FirRegOp> regs(
          module.getBodyBlock()->getOps<seq::FirRegOp>());
      for (size_t i = 0; i < regs.size() && !changed; ++i) {
        for (size_t j = i + 1; j < regs.size() && !changed; ++j) {
          seq::FirRegOp keep = regs[i], dup = regs[j];
          if (!canMerge(keep, dup))
            continue;
          dup.getResult().replaceAllUsesWith(keep.getResult());
          dup.erase();
          ++merged;
          changed = true; // rescan: a merge can enable further merges
        }
      }
    }
    if (merged > 0)
      llvm::errs() << "[dedup-registers] merged " << merged
                   << " duplicate firreg(s) in " << module.getModuleName()
                   << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createDedupRegistersPass() {
  return std::make_unique<DedupRegistersPass>();
}

} // namespace circt_abc
