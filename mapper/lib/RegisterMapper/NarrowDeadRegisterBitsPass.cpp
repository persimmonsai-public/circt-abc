//===- NarrowDeadRegisterBitsPass.cpp --------------------------*- C++ -*-===//
//
// `--enable-bit-narrowing`, ON by default since verification -- see
// Passes.td and this file's correctness-discipline notes below.
//
// The QoR roadmap's register-count-gap investigation found circt-abc emits
// 2,325 DFF instances vs. Yosys's 1,951 for identical ibex RTL (+19%), and
// root-caused the dominant mechanism as something circt-abc's flow has NO
// equivalent for anywhere: Yosys's synthesis-grade `opt` does per-bit
// constant-propagation/dead-bit-elimination WITHIN an otherwise-live
// multi-bit register (concretely: ibex CSR registers like `dcsr_q`/
// `mstatus_q`/`cpuctrlsts_part_q` have reserved/unimplemented bit fields
// that are held forever but never actually read by anything). This is a
// DIFFERENT question than RemoveDeadRegistersPass.cpp answers (mapper/lib/
// RegisterMapper/RemoveDeadRegistersPass.cpp, not wired into the default
// pipeline) -- that pass asks "is this WHOLE register ever read", already
// measured to close only ~6% of the 374-bit gap on its own. This pass asks
// "which INDIVIDUAL bits of an otherwise-live register are ever read",
// narrowing just the dead ones.
//
// Scoped to hw::HWModuleOp (per-module, like RegisterLegalizePass/
// ClockGateInferencePass), deliberately NOT requiring the whole-design
// early flatten RemoveDeadRegistersPass's own file comment says it needs
// for full cross-module reachability: an earlier session attempt to
// flatten the whole design before register-mapper (for a related but
// different purpose) hit a real, serious MLIR graph-region false-cycle bug
// and was abandoned entirely -- not a risk worth taking here. The
// consequence is a real, honest scoping limitation: a value that escapes
// this module only through an `hw.instance` operand (a call into another
// submodule) is conservatively treated as FULLY live at that boundary,
// since this analysis has no visibility into what the callee does with
// individual bits of its own input port. This misses some real
// opportunities but never narrows a bit that's actually live -- narrowing
// only fires on bits genuinely, provably dead within THIS module's own
// visible logic.
//
// Algorithm: backward per-bit liveness (a Value -> APInt live-bit-mask
// map), seeded from every real demand this module can see -- every
// `hw.output` operand and every `hw.instance` operand, each demanding ALL
// of its own bits -- then propagated backward through the handful of comb
// ops whose bit-level semantics are simple enough to reason about
// precisely (extract/concat/mux/and/or/xor); anything else (arithmetic,
// comparisons, reductions, an unrecognized op) is an "escape hatch" that
// conservatively demands ALL bits of every operand rather than attempt
// partial reasoning. A `seq.firreg`'s `data` result is a TERMINAL
// collection point for this analysis, not something to propagate further
// into: only `next`/`clk`/`reset`/`resetValue` describe how the register's
// value gets WRITTEN, which is irrelevant to "which bits of the STORED
// value are ever READ" -- deliberately never visiting them here means a
// self-referencing hold-value cycle (`next = comb.mux(enable, new, <this
// same data>)`) can never manufacture its own liveness, the same free
// property RemoveDeadRegistersPass gets from never visiting a firreg's
// write-side operands until its `data` is already independently proven
// live by a REAL consumer.
//
// A second, coarser "opaque" propagation channel (a plain Value set, no
// bit-mask) exists ONLY to stay safe at a non-integer-typed boundary this
// analysis cannot reason about bit-for-bit: `hw-aggregate-to-comb` (run
// earlier in the pipeline, before this pass, per run_circt_native_synth.sh)
// converts array/struct-typed logic into bitcast+comb sequences, so
// `hw.bitcast` ops genuinely appear on real paths between a firreg and a
// real output in this design, not as a hypothetical edge case. A precise
// per-bit demand cannot be pushed through a `hw.bitcast` (it can
// arbitrarily reinterpret/reorder bits, e.g. int<->struct<->array), so
// reaching one hands the demand off to the opaque channel for its input
// instead (or, if the OTHER side of the bitcast happens to be a plain
// integer again, hands it straight back into the precise channel as a
// full-width demand). Any firreg reached (even partially) via the opaque
// channel -- including any firreg that is itself aggregate/array/struct-
// typed, out of scope for this analysis entirely -- is excluded from
// narrowing outright, never narrowed on a partial/best-effort basis.
//
// Correctness stance: when in doubt, a bit (or a whole firreg) is LIVE,
// never dead. Every "escape hatch" in this pass demands MORE than it
// might need to, never less.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Milestone 10: does a `dont-touch` list entry (bare "<firreg_name>" or
// qualified "<module_name>:<firreg_name>") match this firreg in this
// module? A bare entry (no ':') matches the name in ANY module, exactly
// preserving milestones 6-9's original behavior; a qualified entry only
// matches within the named module.
static bool dontTouchEntryMatches(StringRef entry, StringRef moduleName,
                                   StringRef firregName) {
  auto colon = entry.find(':');
  if (colon == StringRef::npos)
    return entry == firregName;
  return entry.substr(0, colon) == moduleName &&
         entry.substr(colon + 1) == firregName;
}

struct NarrowDeadRegisterBitsPass
    : public PassWrapper<NarrowDeadRegisterBitsPass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowDeadRegisterBitsPass)

  NarrowDeadRegisterBitsPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  NarrowDeadRegisterBitsPass(const NarrowDeadRegisterBitsPass &other)
      : PassWrapper(other) {
    preciseBitcast = other.preciseBitcast.getValue();
    enableBitNarrowing = other.enableBitNarrowing.getValue();
    dontTouch = other.dontTouch;
  }

  StringRef getArgument() const final { return "narrow-dead-register-bits"; }
  StringRef getDescription() const final {
    return "Prototype (OFF by default): narrow individual dead output bits "
           "of otherwise-live seq.firreg registers, so register-mapper "
           "never instantiates a real DFF for them";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  // hw.bitcast reinterprets a packed bit pattern; it never permutes bits.
  // With this on, a per-bit demand passes through a same-width integer
  // bitcast, and through an integer -> aggregate -> integer pair, exactly
  // (the aggregate in the middle keeps whatever other demands it has). OFF
  // by default so the shipped behaviour (whole-value demand at every
  // bitcast) is unchanged unless asked for; narrow-dead-output-bits relies
  // on it, since a zeroed aggregate output port reaches the register only
  // through such a pair.
  Option<bool> preciseBitcast{
      *this, "precise-bitcast",
      llvm::cl::desc("Propagate per-bit demand through same-width bitcasts "
                      "and integer->aggregate->integer bitcast pairs"),
      llvm::cl::init(false)};
  Option<bool> enableBitNarrowing{
      *this, "enable-bit-narrowing",
      llvm::cl::desc("Enable per-bit dead-register-bit narrowing -- ON by "
                      "default since verification (4 synthetic trick-case "
                      "tests plus a real per-module LEC with control-"
                      "experiment confirmation); a higher-risk class of "
                      "change than this project's other passes, see this "
                      "pass's file comment"),
      llvm::cl::init(true)};

  // Design-management-layer "directive/customization" milestone 6's one
  // concrete enforcement point (scripts/circt_flow/design/directives.py
  // has the full reasoning): a comma-separated list of exact seq.firreg
  // names to exclude from narrowing entirely. Empty by default -- identical
  // behavior to not having this option at all. This is deliberately NOT a
  // new code path: it forces a firreg through the SAME pre-existing,
  // already-verified "leave this firreg fully alone" branch this pass
  // already takes for any firreg reached via the opaque (non-integer)
  // channel (see the exclude-set population below) -- narrowing a register
  // less than the analysis would otherwise allow can only ever be
  // conservative, never wrong, so this hook carries none of the risk a
  // directive that could force MORE aggressive narrowing would.
  // Milestone 10 (design-management-layer directive surface): each entry
  // is EITHER a bare "<firreg_name>" (matches that name in every module,
  // the original milestone-6 behavior, fully preserved for backward
  // compatibility) OR a qualified "<module_name>:<firreg_name>" (matches
  // only within that one hw.module) -- see dontTouchEntryMatches() below.
  // Needed because seq.firreg names are only unique WITHIN a module, not
  // globally: a bare name given today silently touches every like-named
  // firreg in every module instance sharing that name (confirmed on real
  // ibex_top: `rdata_q` appears in 11 different modules).
  ListOption<std::string> dontTouch{
      *this, "dont-touch",
      llvm::cl::desc("Comma-separated seq.firreg names to exclude from "
                      "narrowing entirely, regardless of what the liveness "
                      "analysis would otherwise allow -- each entry is "
                      "either a bare name (matches in every module) or a "
                      "qualified <module_name>:<firreg_name> (matches only "
                      "in that module) -- empty by default (no effect)")};

  void runOnOperation() override {
    if (!enableBitNarrowing)
      return;
    hw::HWModuleOp module = getOperation();
    Block *body = module.getBodyBlock();
    Operation *terminator = body->getTerminator();

    llvm::DenseMap<Value, APInt> liveMask; // precise per-bit demand, integer-typed values only
    llvm::DenseSet<Value> opaqueLive;       // coarse "whole value used somewhere", non-integer values
    SmallVector<Value> intWorklist;
    SmallVector<Value> opaqueWorklist;

    auto bitWidthOf = [](Value v) -> unsigned {
      auto intTy = dyn_cast<IntegerType>(v.getType());
      return intTy ? intTy.getWidth() : 0;
    };

    auto addOpaque = [&](Value v) {
      if (!opaqueLive.insert(v).second)
        return;
      opaqueWorklist.push_back(v);
    };

    auto addDemand = [&](Value v, const APInt &mask) {
      if (mask.isZero())
        return;
      unsigned w = bitWidthOf(v);
      if (w == 0) {
        // Shouldn't normally happen (call sites below only pass
        // integer-typed Values here), but fall back to opaque tracking
        // rather than silently dropping a real demand if it ever does.
        addOpaque(v);
        return;
      }
      auto it = liveMask.find(v);
      if (it == liveMask.end()) {
        liveMask.try_emplace(v, mask);
        intWorklist.push_back(v);
        return;
      }
      APInt merged = it->second | mask;
      if (merged != it->second) {
        it->second = merged;
        intWorklist.push_back(v);
      }
    };

    // Shared escape hatch for any op this analysis doesn't specifically
    // understand the bit-level semantics of: route a single operand to
    // whichever channel fits its type, conservatively demanding the WHOLE
    // value rather than attempting partial reasoning.
    auto demandWholeOperand = [&](Value operand) {
      unsigned w = bitWidthOf(operand);
      if (w > 0)
        addDemand(operand, APInt::getAllOnes(w));
      else
        addOpaque(operand);
    };

    // --- Seed from every real, externally-visible demand in this module. ---
    for (Value operand : terminator->getOperands())
      demandWholeOperand(operand);
    for (auto instOp : body->getOps<hw::InstanceOp>())
      for (Value operand : instOp->getOperands())
        demandWholeOperand(operand);

    // --- Precise integer-typed backward propagation. ---
    llvm::DenseMap<Operation *, APInt> firregLiveMask;
    auto drainPrecise = [&] {
    while (!intWorklist.empty()) {
      Value v = intWorklist.pop_back_val();
      APInt mask = liveMask.lookup(v);
      Operation *def = v.getDefiningOp();
      if (!def)
        continue; // block argument (module input): nothing further upstream

      if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
        // `v` is this firreg's `data` result -- the analysis target itself.
        // Deliberately do NOT propagate into next/clk/reset/resetValue --
        // see file comment for why.
        auto it = firregLiveMask.find(firreg.getOperation());
        if (it == firregLiveMask.end())
          firregLiveMask.try_emplace(firreg.getOperation(), mask);
        else
          it->second |= mask;
        continue;
      }
      if (auto ext = dyn_cast<comb::ExtractOp>(def)) {
        unsigned inW = bitWidthOf(ext.getInput());
        APInt inMask = APInt::getZero(inW);
        inMask.insertBits(mask, ext.getLowBit());
        addDemand(ext.getInput(), inMask);
        continue;
      }
      if (auto cat = dyn_cast<comb::ConcatOp>(def)) {
        // MSB-first operand order (this project's own established, verified
        // convention -- see e.g. RegisterMapperPass.cpp's own concat-
        // building code): walk operands from LAST (LSB) to FIRST (MSB)
        // while accumulating a running LSB-relative bit offset.
        unsigned offset = 0;
        for (int i = static_cast<int>(cat.getInputs().size()) - 1; i >= 0;
             --i) {
          Value operand = cat.getInputs()[i];
          unsigned w = bitWidthOf(operand);
          addDemand(operand, mask.extractBits(w, offset));
          offset += w;
        }
        continue;
      }
      if (auto mux = dyn_cast<comb::MuxOp>(def)) {
        // Either leg could be the one actually selected at runtime, so a
        // live output bit demands that SAME bit from BOTH legs.
        addDemand(mux.getTrueValue(), mask);
        addDemand(mux.getFalseValue(), mask);
        addDemand(mux.getCond(), APInt(1, 1));
        continue;
      }
      if (auto andOp = dyn_cast<comb::AndOp>(def)) {
        for (Value operand : andOp.getInputs())
          addDemand(operand, mask);
        continue;
      }
      if (auto orOp = dyn_cast<comb::OrOp>(def)) {
        for (Value operand : orOp.getInputs())
          addDemand(operand, mask);
        continue;
      }
      if (auto xorOp = dyn_cast<comb::XorOp>(def)) {
        for (Value operand : xorOp.getInputs())
          addDemand(operand, mask);
        continue;
      }
      if (auto bitcast = dyn_cast<hw::BitcastOp>(def)) {
        // Default: hand off to the opaque channel for its input, even if
        // that input also happens to be integer-typed (the shipped,
        // conservative behaviour). `precise-bitcast`: hw.bitcast is a
        // reinterpretation of the packed bits, so bit i of the result IS
        // bit i of the input -- pass the mask through a same-width integer
        // input, or through an integer->aggregate->integer pair.
        if (preciseBitcast) {
          Value in = bitcast.getInput();
          if (bitWidthOf(in) == mask.getBitWidth()) {
            addDemand(in, mask);
            continue;
          }
          if (auto inner = in.getDefiningOp<hw::BitcastOp>())
            if (bitWidthOf(inner.getInput()) == mask.getBitWidth()) {
              addDemand(inner.getInput(), mask);
              continue;
            }
        }
        demandWholeOperand(bitcast.getInput());
        continue;
      }

      // Escape hatch: arithmetic, comparisons, reductions, replicate, or
      // any other op kind -- conservatively demand ALL bits of every
      // operand.
      for (Value operand : def->getOperands())
        demandWholeOperand(operand);
    }
    };

    // --- Opaque (non-integer-typed) backward propagation. ---
    // Exists to correctly hand demand back into the precise channel at an
    // hw.bitcast boundary, and to mark any firreg reached this way
    // (aggregate/struct/array-typed, or a plain-integer one reached only
    // through a non-integer path) as excluded from narrowing entirely --
    // this pass only ever narrows a plain scalar-integer firreg whose
    // ENTIRE reachability is through the precise channel.
    llvm::DenseSet<Operation *> excludeFromNarrowing;

    // Directive/customization hook (design-management-layer milestone 6,
    // see the `dontTouch` option comment above): force any firreg whose
    // exact name appears in `dontTouch` through this SAME set, exactly as
    // if the opaque channel had reached it. Checked before the opaque
    // worklist below only for locality of the two `excludeFromNarrowing`
    // populators; order between them does not matter since both simply add
    // to the same set before the rewrite loop consults it.
    if (!dontTouch.empty()) {
      StringRef moduleName = module.getModuleName();
      for (auto firreg : body->getOps<seq::FirRegOp>()) {
        StringRef name = firreg.getName();
        if (name.empty())
          continue;
        for (StringRef entry : dontTouch)
          if (dontTouchEntryMatches(entry, moduleName, name)) {
            excludeFromNarrowing.insert(firreg.getOperation());
            break;
          }
      }
    }

    auto drainOpaque = [&] {
    while (!opaqueWorklist.empty()) {
      Value v = opaqueWorklist.pop_back_val();
      Operation *def = v.getDefiningOp();
      if (!def)
        continue;
      if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
        excludeFromNarrowing.insert(firreg.getOperation());
        continue;
      }
      if (auto bitcast = dyn_cast<hw::BitcastOp>(def)) {
        demandWholeOperand(bitcast.getInput());
        continue;
      }
      for (Value operand : def->getOperands())
        demandWholeOperand(operand);
    }
    };

    // --- Global fixpoint (2026-08-27 soundness fix; two REAL bugs found
    // with trivial reproducers while building narrow-array-registers, both
    // present since this pass first shipped and both capable of silently
    // replacing a LIVE register with constant zero):
    //
    //   Bug A ("opaque handoff never drains"): the opaque loop hands
    //   demand back into the precise channel at hw.bitcast boundaries
    //   (that was its whole documented purpose), but the precise worklist
    //   was only ever drained ONCE, *before* the opaque loop ran -- so any
    //   demand entering through a bitcast (e.g. an integer register whose
    //   only consumer is an aggregate-typed output port) never propagated,
    //   and the register was declared wholly dead. Reproducer: a single
    //   i16 firreg feeding `hw.output` through one `hw.bitcast` to
    //   !hw.array<2xi8> -- the register was replaced with constant 0.
    //
    //   Bug B ("no demand through register stages"): a firreg's data
    //   result is deliberately a terminal collection point (correct for
    //   avoiding hold-mux self-liveness), but nothing ever RE-SEEDED
    //   demand from a live register's `next`/`resetValue` cones -- so an
    //   upstream register whose only consumers sit inside a downstream
    //   register's next-value logic received zero demand and was removed.
    //   Reproducer: a plain 2-flop shift register (`b = firreg(a)`,
    //   `a = firreg(d)`, output b) -- `a` was replaced with constant 0.
    //   CONFIRMED to have fired on the real shipped baseline:
    //   ibex_multdiv_slow's `multdiv_count_q` (a real 5-bit down-counter
    //   with a decrementer and terminal-count compare, kept by Yosys) was
    //   absent from the shipped netlist because its entire fanout
    //   terminates in other registers' next cones (FSM state / operand
    //   registers).
    //
    // Fix: iterate both drains and register-feedback seeding to a global
    // fixpoint. Every register that will be KEPT (any live bit, excluded
    // from narrowing, aggregate-typed -- which the rewrite below never
    // touches -- or dont-touch'd) must in turn demand its own operands:
    // next/resetValue at the register's live mask (the rewrite compacts
    // them to exactly those bits), clock/reset-condition in full. Masks
    // only ever GROW, so this fix is strictly conservative: it can only
    // keep MORE than the buggy version kept, never less.
    llvm::DenseMap<Operation *, APInt> firregSeededMask;
    llvm::DenseSet<Operation *> firregSeededWhole;
    auto seedRegOperandsWhole = [&](seq::FirRegOp firreg) {
      if (!firregSeededWhole.insert(firreg.getOperation()).second)
        return false;
      demandWholeOperand(firreg.getNext());
      if (Value rv = firreg.getResetValue())
        demandWholeOperand(rv);
      if (Value rst = firreg.getReset())
        demandWholeOperand(rst);
      addOpaque(firreg.getClk());
      return true;
    };

    bool feedbackChanged = true;
    while (feedbackChanged || !intWorklist.empty() ||
           !opaqueWorklist.empty()) {
      drainPrecise();
      drainOpaque();
      if (!intWorklist.empty() || !opaqueWorklist.empty())
        continue; // opaque handoff refilled the precise channel: re-drain.
      feedbackChanged = false;
      for (auto firreg : body->getOps<seq::FirRegOp>()) {
        Operation *op = firreg.getOperation();
        if (firregSeededWhole.contains(op))
          continue;
        bool aggregate = !isa<IntegerType>(firreg.getNext().getType());
        if (aggregate || excludeFromNarrowing.contains(op)) {
          if (seedRegOperandsWhole(firreg))
            feedbackChanged = true;
          continue;
        }
        auto maskIt = firregLiveMask.find(op);
        if (maskIt == firregLiveMask.end())
          continue; // no live bits (yet): keeps nothing alive.
        unsigned width =
            cast<IntegerType>(firreg.getNext().getType()).getWidth();
        auto [doneIt, inserted] =
            firregSeededMask.try_emplace(op, APInt::getZero(width));
        APInt delta = maskIt->second & ~doneIt->second;
        if (delta.isZero())
          continue;
        addDemand(firreg.getNext(), delta);
        if (Value rv = firreg.getResetValue())
          addDemand(rv, delta);
        if (Value rst = firreg.getReset())
          demandWholeOperand(rst);
        addOpaque(firreg.getClk());
        doneIt->second |= delta;
        feedbackChanged = true;
      }
    }

    // --- Rewrite: narrow every plain-integer firreg with >=1 dead bit. ---
    unsigned numNarrowed = 0, totalBitsDropped = 0;
    for (auto firreg :
         llvm::make_early_inc_range(body->getOps<seq::FirRegOp>())) {
      auto intTy = dyn_cast<IntegerType>(firreg.getNext().getType());
      if (!intTy)
        continue; // aggregate-typed firreg: out of scope, never narrowed.
      if (excludeFromNarrowing.contains(firreg.getOperation()))
        continue; // reached (even partially) through the opaque channel.

      unsigned width = intTy.getWidth();
      auto maskIt = firregLiveMask.find(firreg.getOperation());
      APInt mask = maskIt == firregLiveMask.end() ? APInt::getZero(width)
                                                   : maskIt->second;

      unsigned liveCount = mask.popcount();
      if (liveCount == width)
        continue; // no dead bits: nothing to narrow.

      OpBuilder builder(firreg);
      Location loc = firreg.getLoc();

      if (liveCount == 0) {
        // Wholly dead -- the same case RemoveDeadRegistersPass's
        // whole-register removal targets (that pass isn't wired into the
        // default pipeline, so this is real, additional coverage from the
        // same underlying analysis, not a duplicate). No register needed
        // at all.
        Value zero =
            hw::ConstantOp::create(builder, loc, APInt::getZero(width));
        firreg.getData().replaceAllUsesWith(zero);
        firreg.erase();
        ++numNarrowed;
        totalBitsDropped += width;
        continue;
      }

      // Partial narrowing: build a new, narrower firreg carrying only the
      // live bits (compacted in ascending original-bit-index order), and
      // reconstruct a value of the ORIGINAL width for firreg.getData()'s
      // existing uses by placing each live bit back at its original
      // position and filling every dead position with a plain zero
      // constant bit. register-mapper (which runs after this pass) then
      // only ever instantiates a real DFF for the narrower firreg; the
      // reconstruction is pure combinational glue that ABC/canonicalize
      // trivially const-folds away on the dead side.
      SmallVector<unsigned> liveBits; // ascending original bit indices
      for (unsigned i = 0; i < width; ++i)
        if (mask[i])
          liveBits.push_back(i);

      // `createOrFold`, NOT plain `create`, matters here: `resetValue` (when
      // a reset is present) is always a real, literal `hw.constant` by this
      // point in the pipeline (register-legalize's own precondition,
      // enforced by register-mapper's `m_ConstantInt` check below) --
      // extracting/concatenating its bits with plain `create` would leave a
      // `comb.extract`/`comb.concat` *of* a constant rather than folding
      // straight back down to a literal `hw.constant`, which is exactly
      // the shape register-mapper's own pattern match requires. Harmless
      // (a no-op fold) when `wide` isn't a compile-time constant, e.g. for
      // `next`.
      Type i1Type = builder.getIntegerType(1);
      auto extractBit = [&](Value v, unsigned bit) -> Value {
        return builder.createOrFold<comb::ExtractOp>(loc, i1Type, v, bit);
      };

      // Compacted `next`/`resetValue`: narrow bit k (ascending) = original
      // bit liveBits[k] of the wide value.
      auto compact = [&](Value wide) -> Value {
        SmallVector<Value> concatOperands; // MSB-first for comb.concat
        for (unsigned k = liveCount; k-- > 0;)
          concatOperands.push_back(extractBit(wide, liveBits[k]));
        return liveCount == 1
                   ? concatOperands[0]
                   : builder.createOrFold<comb::ConcatOp>(loc,
                                                           concatOperands);
      };

      Value newNext = compact(firreg.getNext());
      Value reset = firreg.getReset();
      Value newResetValue =
          reset ? compact(firreg.getResetValue()) : Value();

      std::string newName = (Twine(firreg.getName()) + "_narrow").str();
      seq::FirRegOp newFirreg;
      if (reset) {
        newFirreg = seq::FirRegOp::create(
            builder, loc, newNext, firreg.getClk(),
            builder.getStringAttr(newName), reset, newResetValue,
            firreg.getInnerSymAttr(), firreg.getIsAsync(),
            firreg.getPresetAttr());
      } else {
        newFirreg = seq::FirRegOp::create(
            builder, loc, newNext, firreg.getClk(),
            builder.getStringAttr(newName), firreg.getInnerSymAttr(),
            firreg.getPresetAttr());
      }

      // Reconstruct the original width: MSB-first concat of, for each
      // original bit position, either an extract of the new narrow
      // register (if live) or a plain 0 constant (if dead).
      Value zeroBit = hw::ConstantOp::create(builder, loc, APInt::getZero(1));
      SmallVector<Value> reconOperands; // MSB-first
      for (unsigned i = width; i-- > 0;) {
        if (mask[i]) {
          unsigned rank = std::distance(liveBits.begin(),
                                         llvm::find(liveBits, i));
          reconOperands.push_back(extractBit(newFirreg.getData(), rank));
        } else {
          reconOperands.push_back(zeroBit);
        }
      }
      Value reconstructed =
          width == 1 ? reconOperands[0]
                     : comb::ConcatOp::create(builder, loc, reconOperands)
                           .getResult();

      firreg.getData().replaceAllUsesWith(reconstructed);
      firreg.erase();
      ++numNarrowed;
      totalBitsDropped += (width - liveCount);
    }

    if (numNarrowed > 0)
      llvm::errs() << "[narrow-dead-register-bits] narrowed " << numNarrowed
                   << " firreg(s), dropping " << totalBitsDropped
                   << " dead bit(s) total, in " << module.getModuleName()
                   << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createNarrowDeadRegisterBitsPass() {
  return std::make_unique<NarrowDeadRegisterBitsPass>();
}

} // namespace circt_abc
