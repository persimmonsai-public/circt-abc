//===- NarrowArrayRegistersPass.cpp -------------------------*- C++ -*-===//
//
// `--narrow-array-registers` (ON by default -- landed OFF per the project
// convention, then promoted with explicit user sign-off after the full
// diligence record: the fetch_fifo BMC blocker resolved -- it was a real,
// since-fixed Sig2Reg offset miscompilation plus a harness bug, not this
// pass -- BMC passing at depths 2/6/10 with and without the pass on the
// fixed frontend, and two clean, byte-identical real P&R runs: smaller final
// area and power than the pre-promotion default, WNS-neutral, with some TNS
// as the one real, accepted cost).
//
// Targets the first source of this flow's register-count gap against
// Yosys: a generate-loop with one `always_ff` per
// entry of a packed/unpacked array variable lowers, through the frontend,
// to one FULL-ARRAY `seq.firreg` per loop iteration -- e.g.
// ibex_fetch_fifo's 3-entry `rdata_q` becomes THREE 96-bit
// `!hw.array<3xi32>` registers (288 netlist DFFs where Yosys produces 96),
// and ibex_id_stage's `imd_val_q[2]` becomes TWO 68-bit registers. Each
// copy is only ever OBSERVED at its own entry's bit-slice; the other
// entries' flops are real, dead hardware.
//
// Why the existing `narrow-dead-register-bits` pass cannot catch this: by
// the time the mapper pipeline runs (after `--hw-aggregate-to-comb`), each
// such register is an ARRAY-typed firreg whose next/data values cross
// `hw.bitcast` boundaries -- and that pass's per-bit liveness analysis
// deliberately treats `hw.bitcast` as a conservative all-bits-live escape
// hatch (see its file comment). The integer-typed variant of the very same
// RTL pattern (ibex_fetch_fifo's `err_q`, three i3 copies) IS already
// narrowed by that pass -- confirming the bitcast escape hatch, not the
// hold-path cycle, is what keeps the +260 array bits alive.
//
// The observed IR shape at this pass's pipeline position (verified against
// real ibex_top `_core_deagg.mlir`, both with and without reset):
//
//   %next_i  = comb.mux %en, %write_i, %hold_i : iTotal
//   %next    = hw.bitcast %next_i : (iTotal) -> !hw.array<NxiW>
//   %F       = seq.firreg %next clock %clk [reset async %rst, %rv] : array
//   %hold_i  = hw.bitcast %F : (!hw.array<NxiW>) -> iTotal       // "FInt"
//   %obs     = comb.extract %hold_i from OFF : (iTotal) -> iw    // owned
//
// where %obs (one or more extracts, all at the same [OFF, OFF+w) slice) is
// the ONLY external observation of %F.
//
// Correctness argument (the whole point -- deliberately does NOT depend on
// analyzing the write value's other bits): bits of F outside [OFF, OFF+w)
// are unobservable -- every use of F is either (a) the identity hold
// operand of its own next-mux, which is bit-diagonal (bit i of the hold
// branch reads only bit i of F), or (b) an extract inside the owned slice.
// Therefore F is observationally equivalent to a w-bit register holding
// just the owned slice:
//
//   %d    = comb.extract %write_i from OFF : iw
//   %v    = seq.firreg (comb.mux %en, %d, %v) clock %clk
//             [reset %rst, (comb.extract rvInt from OFF)] : iw
//
// ... PROVIDED nothing in the cones of %en, %write_i, or the reset value
// reads F's DEAD bits (if they did, those bits would be observable through
// the live slice after all). This is checked explicitly with a bounded
// backward walk that stops at sequential/structural boundaries (other
// firregs, instances, block arguments, constants) and rejects the register
// entirely if it ever reaches F through anything other than an extract
// fully contained in the owned slice. When in doubt, the register is left
// completely untouched -- a false positive here is silent mis-synthesis of
// a real RISC-V core's instruction FIFO, categorically worse than a missed
// optimization.
//
// Placed FIRST in the mapper pipeline (before register-legalize) when
// enabled, so downstream passes see the narrowed integer registers:
// clock-gate-inference can gate the new hold-mux-shaped scalars,
// narrow-dead-register-bits can narrow residual dead bits inside the owned
// slice (Yosys gets imd_val_q to 65 bits, not 68), and the imd_val copies'
// previously NON-constant reset values (an inject of the resolved signal)
// become plain constants after slicing -- strictly friendlier to
// register-legalize/register-mapper's constant-reset preconditions.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/HW/HWTypes.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct NarrowArrayRegistersPass
    : public PassWrapper<NarrowArrayRegistersPass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowArrayRegistersPass)

  NarrowArrayRegistersPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  NarrowArrayRegistersPass(const NarrowArrayRegistersPass &other)
      : PassWrapper(other) {
    enableArrayNarrowing = other.enableArrayNarrowing.getValue();
  }

  StringRef getArgument() const final { return "narrow-array-registers"; }
  StringRef getDescription() const final {
    return "ON by default (promoted after full diligence, see Passes.td): "
           "narrow an array-typed seq.firreg that is only ever observed "
           "through one constant bit-slice of its integer bitcast down to "
           "a register of just that slice";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<bool> enableArrayNarrowing{
      *this, "enable-array-register-narrowing",
      llvm::cl::desc("Enable array-register slice narrowing -- ON by "
                      "default, promoted with explicit sign-off after the "
                      "full diligence record: BMC at depths 2/6/10 with "
                      "and without the pass on the fixed frontend, plus "
                      "two clean, byte-identical real P&R runs (smaller "
                      "final area and power than the default, "
                      "WNS-neutral, some TNS -- the one real, "
                      "known cost)"),
      llvm::cl::init(true)};

  // Resolve bits [off, off+w) of `v` to a compile-time constant by walking
  // chains of comb.concat (descending into the single operand that fully
  // covers the requested range) and slicing hw.constant leaves. Returns
  // std::nullopt if the range is not fully covered by one traversable
  // operand at any level or the leaf is not a literal constant -- the
  // caller then falls back to a plain runtime extract. Purpose: a narrowed
  // register's reset value is frequently SEMANTICALLY constant at the owned
  // slice even when the full reset expression is not (the real
  // ibex_id_stage imd_val_q shape: reset = concat(<other copy's slice>,
  // c0) -- the owned half is the literal constant). Materializing the
  // literal keeps the reset expressible by register-legalize's async
  // constant-reset path (a true async-clear flop), instead of being folded
  // into D-input logic as a syntactically-non-constant reset -- which both
  // costs fold-in logic AND demotes the RTL's async-reset semantics to
  // sync-only on those bits.
  static std::optional<APInt> foldSliceToConstant(Value v, unsigned off,
                                                   unsigned w) {
    constexpr unsigned kDepthBudget = 64;
    for (unsigned depth = 0; depth < kDepthBudget; ++depth) {
      Operation *def = v.getDefiningOp();
      if (!def)
        return std::nullopt;
      if (auto cst = dyn_cast<hw::ConstantOp>(def))
        return cst.getValue().extractBits(w, off);
      if (auto cat = dyn_cast<comb::ConcatOp>(def)) {
        // comb.concat lists operands MSB-first; walk from the LSB end.
        unsigned lo = 0;
        Value covering;
        for (Value operand : llvm::reverse(cat.getOperands())) {
          unsigned ow = cast<IntegerType>(operand.getType()).getWidth();
          if (off >= lo && off + w <= lo + ow) {
            covering = operand;
            off -= lo;
            break;
          }
          lo += ow;
        }
        if (!covering)
          return std::nullopt; // range straddles operands: give up
        v = covering;
        continue;
      }
      return std::nullopt; // any other op: give up (conservative)
    }
    return std::nullopt;
  }

  // Bounded backward walk from `roots`: returns true iff no value in the
  // combinational cone reads `fInt` (the register's integer view) other
  // than through a comb.extract fully contained in [off, off+width), and
  // never reads the array-typed register result `fArr` directly at all.
  // Sequential/structural boundaries (other firregs, instances, block
  // arguments, constants, clocks) terminate the walk -- reading some OTHER
  // register's output is always fine; only THIS register's dead bits are
  // being protected.
  static bool coneAvoidsDeadBits(ArrayRef<Value> roots, Value fArr,
                                  Value fInt, unsigned off, unsigned width) {
    constexpr unsigned kOpBudget = 100000;
    llvm::DenseSet<Value> visited;
    SmallVector<Value> worklist(roots.begin(), roots.end());
    unsigned steps = 0;
    while (!worklist.empty()) {
      Value v = worklist.pop_back_val();
      if (!visited.insert(v).second)
        continue;
      if (++steps > kOpBudget)
        return false; // budget exceeded: reject conservatively
      // Reaching the register itself through anything other than the
      // allowed extracts (handled below, before their operand is pushed)
      // means a dead bit could leak into observable logic.
      if (v == fArr || v == fInt)
        return false;
      Operation *def = v.getDefiningOp();
      if (!def)
        continue; // block argument: leaf
      if (isa<hw::ConstantOp, seq::FirRegOp, hw::InstanceOp>(def))
        continue; // sequential/structural boundary or literal: leaf
      if (auto ext = dyn_cast<comb::ExtractOp>(def)) {
        if (ext.getInput() == fInt) {
          unsigned lo = ext.getLowBit();
          unsigned w = cast<IntegerType>(ext.getType()).getWidth();
          if (lo >= off && lo + w <= off + width)
            continue; // read of the owned (live) slice: allowed leaf
          return false; // read of a dead bit: reject
        }
      }
      // Any other op (comb.*, hw.bitcast, hw.array_*, unknown): traverse
      // its operands -- the only danger is reaching fArr/fInt, checked at
      // pop time above.
      for (Value operand : def->getOperands())
        worklist.push_back(operand);
    }
    return true;
  }

  void runOnOperation() override {
    if (!enableArrayNarrowing.getValue())
      return;
    hw::HWModuleOp module = getOperation();
    Block *body = module.getBodyBlock();
    if (!body)
      return;

    unsigned numNarrowed = 0, totalBitsDropped = 0;

    for (auto firreg :
         llvm::make_early_inc_range(body->getOps<seq::FirRegOp>())) {
      // -- Shape checks, all bail-to-untouched. --
      auto arrTy = dyn_cast<hw::ArrayType>(firreg.getType());
      if (!arrTy)
        continue;
      if (firreg.getPresetAttr() || firreg.getInnerSymAttr())
        continue;

      // Exactly one use of the array result: a bitcast to an integer of
      // the same total width ("FInt"). Every read of F goes through it.
      if (!firreg.getData().hasOneUse())
        continue;
      auto fIntBitcast =
          dyn_cast<hw::BitcastOp>(firreg.getData().use_begin()->getOwner());
      if (!fIntBitcast)
        continue;
      auto totalTy = dyn_cast<IntegerType>(fIntBitcast.getType());
      if (!totalTy)
        continue;
      unsigned totalWidth = totalTy.getWidth();
      Value fInt = fIntBitcast.getResult();

      // next = bitcast(mux(en, W, FInt)) -- with the bitcast and mux each
      // single-use, so the whole chain can be erased deterministically.
      auto nextBitcast = firreg.getNext().getDefiningOp<hw::BitcastOp>();
      if (!nextBitcast || !nextBitcast.getResult().hasOneUse())
        continue;
      auto mux = nextBitcast.getInput().getDefiningOp<comb::MuxOp>();
      if (!mux || !mux.getResult().hasOneUse())
        continue;
      Value en = mux.getCond();
      Value writeVal;
      bool writeOnTrue;
      if (mux.getFalseValue() == fInt) {
        writeVal = mux.getTrueValue();
        writeOnTrue = true;
      } else if (mux.getTrueValue() == fInt) {
        writeVal = mux.getFalseValue();
        writeOnTrue = false;
      } else {
        continue; // not a self-hold mux
      }

      // All uses of FInt: the mux hold operand plus comb.extract ops that
      // all read one identical strict-subset slice [off, off+w).
      bool sawSliceExtract = false, shapeOk = true;
      unsigned off = 0, w = 0;
      SmallVector<comb::ExtractOp> ownedReads;
      for (Operation *user : fInt.getUsers()) {
        if (user == mux)
          continue;
        auto ext = dyn_cast<comb::ExtractOp>(user);
        if (!ext || ext.getInput() != fInt) {
          shapeOk = false;
          break;
        }
        unsigned lo = ext.getLowBit();
        unsigned ew = cast<IntegerType>(ext.getType()).getWidth();
        if (!sawSliceExtract) {
          off = lo;
          w = ew;
          sawSliceExtract = true;
        } else if (lo != off || ew != w) {
          shapeOk = false; // observed at more than one distinct slice
          break;
        }
        ownedReads.push_back(ext);
      }
      if (!shapeOk || !sawSliceExtract || w >= totalWidth)
        continue;

      // Reset value's owned slice (if any): materialize an integer view.
      Value resetVal = firreg.getResetValue();
      Value rvInt;
      if (resetVal) {
        if (auto rvBc = resetVal.getDefiningOp<hw::BitcastOp>();
            rvBc && isa<IntegerType>(rvBc.getInput().getType()))
          rvInt = rvBc.getInput();
        // else: created below, only after all checks pass.
      }

      // The load-bearing safety check: nothing feeding the enable, the
      // write value, or the reset value may read F's dead bits.
      SmallVector<Value> roots{en, writeVal};
      if (resetVal)
        roots.push_back(rvInt ? rvInt : resetVal);
      if (!coneAvoidsDeadBits(roots, firreg.getData(), fInt, off, w))
        continue;

      // -- Rewrite. --
      OpBuilder builder(firreg);
      Location loc = firreg.getLoc();
      Type sliceTy = builder.getIntegerType(w);

      Value d = builder.createOrFold<comb::ExtractOp>(loc, sliceTy, writeVal,
                                                       off);
      Value newRv;
      if (resetVal) {
        if (!rvInt)
          rvInt = hw::BitcastOp::create(builder, loc,
                                         builder.getIntegerType(totalWidth),
                                         resetVal);
        // Prefer a literal constant when the owned slice of the reset
        // expression is compile-time resolvable (see foldSliceToConstant's
        // comment for why this matters beyond area: it preserves the
        // reset's ASYNC semantics through register-legalize).
        if (auto cst = foldSliceToConstant(rvInt, off, w))
          newRv = hw::ConstantOp::create(builder, loc, *cst);
        else
          newRv = builder.createOrFold<comb::ExtractOp>(loc, sliceTy, rvInt,
                                                         off);
      }

      // New narrow register, same name (it IS the same logical register,
      // observed slice only); placeholder next replaced right after the
      // new self-hold mux exists.
      seq::FirRegOp newF;
      if (firreg.getReset()) {
        newF = seq::FirRegOp::create(builder, loc, d, firreg.getClk(),
                                      firreg.getNameAttr(), firreg.getReset(),
                                      newRv, firreg.getInnerSymAttr(),
                                      firreg.getIsAsync(),
                                      firreg.getPresetAttr());
      } else {
        newF = seq::FirRegOp::create(builder, loc, d, firreg.getClk(),
                                      firreg.getNameAttr(),
                                      firreg.getInnerSymAttr(),
                                      firreg.getPresetAttr());
      }
      Value newMux =
          writeOnTrue
              ? comb::MuxOp::create(builder, loc, en, d, newF.getData())
                    .getResult()
              : comb::MuxOp::create(builder, loc, en, newF.getData(), d)
                    .getResult();
      if (auto twoState = mux->getAttr("twoState"))
        newMux.getDefiningOp()->setAttr("twoState", twoState);
      newF->setOperand(0, newMux); // next

      for (comb::ExtractOp ext : ownedReads) {
        ext.getResult().replaceAllUsesWith(newF.getData());
        ext.erase();
      }

      // Deterministic teardown of the old self-cycle: break the next link,
      // then erase inward. Each erase below is guarded by the single-use
      // preconditions established above.
      firreg->setOperand(0, firreg.getData());
      nextBitcast.erase(); // its only use (firreg.next) is gone
      mux.erase();          // its only use (nextBitcast) is gone
      fIntBitcast.erase();  // remaining use (mux hold) is gone
      firreg->dropAllReferences();
      firreg.erase();

      ++numNarrowed;
      totalBitsDropped += (totalWidth - w);
    }

    if (numNarrowed > 0)
      llvm::errs() << "[narrow-array-registers] narrowed " << numNarrowed
                   << " array firreg(s), dropping " << totalBitsDropped
                   << " dead bit(s) total, in " << module.getModuleName()
                   << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createNarrowArrayRegistersPass() {
  return std::make_unique<NarrowArrayRegistersPass>();
}

} // namespace circt_abc
