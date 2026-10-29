//===- RemoveDeadRegistersPass.cpp -------------------------*- C++ -*-===//
//
// `seq.firreg` is deliberately declared with real MemoryEffects (MemWrite/
// MemRead/MemAlloc) in CIRCT's own SeqOps.td -- it models genuine hardware
// state, and CIRCT's generic infrastructure (MLIR's own canonicalizer/DCE,
// and CIRCT's own `--hw-imdce`) is correctly conservative about it: neither
// will ever remove a firreg purely because its result is unused, since from
// a general-purpose-compiler standpoint that "write" might matter for
// simulation/debug/observability even when nothing reads the value back.
// synth.tcl's Yosys-based flow does NOT have this conservatism -- its
// `opt`/`clean` passes are synthesis-grade and happily remove registers
// with no real (transitively-traced) consumer, confirmed to matter a lot in
// practice: ibex's `imd_val_q` register (used only by RV32B-gated bit-manip
// logic, dead when `RV32B` defaults to `RV32BNone`, as this flow's build
// does) is genuinely, provably unused end-to-end, yet survives every
// existing CIRCT cleanup pass this flow already runs.
//
// This pass closes that gap: a plain mark-and-sweep DCE, seeded ONLY from
// the module's real `hw.output` operands (never from a firreg's own
// existence), that treats `seq.firreg` as prunable like any other op once
// nothing live depends on its `data` result. This deliberately handles the
// "self-referencing hold-value" pattern common to registers (`next =
// comb.mux(enable, new, <this register's own data>)`) correctly: since the
// worklist only ever visits a firreg's `next`/`clk`/`reset`/`resetValue`
// operands AFTER its `data` result has already been marked live by some
// REAL (externally-reachable) consumer, a register whose data never
// escapes to a real output is never "kick-started" into the live set at
// all, even though its own next-value mux still nominally "uses" it -- a
// classic dead strongly-connected component that a naive
// "does this op have zero uses" pass could never catch, because every op
// inside such a cycle always has at least one use (something else in the
// same dead cycle).
//
// Precondition: this must run on a module where firregs are still plain
// `seq.firreg` ops (before register-mapper bit-blasts them into opaque
// `hw.instance`s of extern standard cells, which are no longer analyzable
// this way at all) -- and, for cross-module reachability to work at all
// without reimplementing --hw-imdce's own intermodule port-pruning logic,
// after the WHOLE design has already been flattened into one module (see
// run_circt_native_synth.sh's pipeline comment for why flattening this
// early -- unlike after register-mapper -- does not hit the graph-region
// false-cycle issue OutlineResidualLogicPass's own comment documents).
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct RemoveDeadRegistersPass
    : public PassWrapper<RemoveDeadRegistersPass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RemoveDeadRegistersPass)

  StringRef getArgument() const final { return "remove-dead-registers"; }
  StringRef getDescription() const final {
    return "Remove seq.firreg ops (and logic exclusively feeding them) with "
           "no real, transitively-live consumer -- CIRCT's own DCE passes "
           "deliberately never do this since firreg models real hardware "
           "state";
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();
    Block *body = module.getBodyBlock();
    Operation *terminator = body->getTerminator();

    // --- Mark: backward reachability from the module's real outputs. ---
    llvm::DenseSet<Value> live;
    SmallVector<Value> worklist(terminator->getOperands().begin(),
                                 terminator->getOperands().end());
    // The sweep below never erases hw.instance ops, so everything an
    // instance consumes is live by definition -- seed instance operands
    // as roots. Without this, a dead op feeding a kept instance gets
    // erased while still referenced ("operation destroyed but still has
    // uses"). ibex never tripped it (its only instances this early are
    // extern clock gates fed by block arguments); CVA6's hierarchical
    // driver context, where sram_cache instances sit beside the dead
    // verification-mirror registers, does.
    // Erasure candidates are exactly: top-level, result-bearing,
    // region-free, non-instance ops. EVERYTHING else is kept, and
    // anything kept makes its operands live roots -- including values
    // captured from inside nested regions (llhd.process bodies survive
    // to this point in hierarchical driver contexts, and a top-level op
    // referenced only from inside such a region is NOT dead). Without
    // this completeness, the erase loop destroys ops that kept ops (an
    // hw.instance operand, a verif.assert, a region capture) still
    // reference: "operation destroyed but still has uses".
    auto isCandidate = [&](Operation &op) {
      return &op != terminator && op.getNumResults() > 0 &&
             !isa<hw::InstanceOp>(op) && op.getNumRegions() == 0;
    };
    for (Operation &op : *body) {
      if (!isCandidate(op))
        worklist.append(op.operand_begin(), op.operand_end());
      for (Region &region : op.getRegions())
        region.walk([&](Operation *inner) {
          worklist.append(inner->operand_begin(), inner->operand_end());
        });
    }
    while (!worklist.empty()) {
      Value v = worklist.pop_back_val();
      if (!live.insert(v).second)
        continue; // already visited
      Operation *def = v.getDefiningOp();
      if (!def)
        continue; // block argument (a module input) -- nothing further to trace

      if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
        // Only reached here because `v` (this firreg's `data` result) was
        // ALREADY marked live by some real consumer -- so its own
        // next-value computation genuinely is needed. Do NOT treat the
        // firreg's mere existence as a reason to visit these; that's
        // exactly the special-casing that keeps a truly-dead register
        // (and its self-referencing hold-value mux) alive forever.
        worklist.push_back(firreg.getNext());
        worklist.push_back(firreg.getClk());
        if (Value reset = firreg.getReset())
          worklist.push_back(reset);
        if (Value resetValue = firreg.getResetValue())
          worklist.push_back(resetValue);
        continue;
      }

      for (Value operand : def->getOperands())
        worklist.push_back(operand);
    }

    // --- Sweep: any op, none of whose results are live, is dead. ---
    // Deliberately never touch hw.instance (of a real extern cell, e.g. ibex's
    // own pre-existing technology clock-gate cell -- the only kind of instance
    // still present this early, since the whole design is already flattened by
    // the time this pass runs): an opaque extern's outputs being unused doesn't
    // prove the instantiation itself is side-effect-free, unlike ordinary
    // comb/hw ops this analysis otherwise reasons about.
    SmallVector<Operation *> dead;
    for (Operation &op : *body) {
      if (!isCandidate(op))
        continue; // exactly the keep-set the mark phase seeded from
      bool anyLive = llvm::any_of(
          op.getResults(), [&](Value r) { return live.contains(r); });
      if (!anyLive)
        dead.push_back(&op);
    }

    // Dead ops may reference each other (e.g. a dead firreg and its own
    // dead next-value mux form a 2-cycle) -- sever every dead op's own
    // operand references FIRST, for every dead op, before erasing any of
    // them, so no op is ever erased while something (dead or not) still
    // references its results. See Operation::dropAllReferences()'s own
    // doc comment: "an essential step in breaking cyclic dependencies
    // between references when they are to be deleted."
    for (Operation *op : dead)
      op->dropAllReferences();
    for (Operation *op : dead)
      op->erase();

    if (!dead.empty())
      llvm::errs() << "[remove-dead-registers] removed " << dead.size()
                   << " dead op(s) in " << module.getModuleName() << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createRemoveDeadRegistersPass() {
  return std::make_unique<RemoveDeadRegistersPass>();
}

} // namespace circt_abc
