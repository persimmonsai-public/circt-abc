//===- StructuralMapperPass.cpp ------------------------------*- C++ -*-===//
//
// M2: real mux/compare-fusion structural mapping (Pattern 1). Targets an
// earlier project's confirmed-open gap: a single-bit `comb.mux` whose
// true/false leg is itself a single-use, 2-input
// `comb.and`/`comb.or`/`comb.xor` gets fused into the matching compound
// standard cell (AOI21/OAI21/AO21/OA21- class), instead of leaving Yosys/ABC to
// emit a discrete mux cell plus a separate gate -- the shape identified as
// ibex's largest architecturally-reachable QoR gap against commercial synthesis
// (concentrated in the 3-4-input compound-gate bucket).
//
// v1 scope (deliberately conservative):
//   - Only i1-typed (single-bit boolean) muxes -- this is exactly ibex's
//     control/select-logic domain (Pattern 1's target), not wide
//     arithmetic/data muxes.
//   - Only a DATA leg (true or false value) may be fused, not the select
//     -- fusing the select produces a different (4-5 input, AOI211-class)
//     shape not yet handled here.
//   - The fused sub-gate must be `hasOneUse()`: this IS the
//     fanout/sharing-economics rule for v1 -- absorbing a leg that has
//     other consumers would force it to be computed twice, which is
//     exactly the mechanism that sank every prior structural-preservation
//     attempt in that earlier project. A more nuanced area-delta
//     version (allowing a beneficial multi-use absorption) is future work,
//     not attempted here.
//   - Matching is brute-force truth-table comparison (cones are <=4
//     inputs, so this is cheap and -- per the project's "mandatory
//     brute-force truth-table re-simulation" correctness discipline --
//     inherently exhaustive, not a heuristic) rather than proper NPN-class
//     canonicalization; LibertyLibrary::lookup() tries all permutations of
//     the cone's leaves against every liberty cell of matching arity.
//
// Pattern 1's actual result on the real ibex design: 0/93 matches --
// root-caused (not a bug/coverage gap) to a Boolean-algebraic wall:
// `mux(cond, p AND q, other)` expands to `c.p.q | !c.o`, so `cond` appears
// in BOTH polarities -- the function is *binate* in `cond` -- while every
// AOI/OAI/AO/OA-class compound liberty cell is provably *unate* (each
// input appears once, one polarity) and structurally cannot realize a
// binate function.
//
// Pattern 2 (added later): nested mux-of-mux ("wide select") fusion.
// `comb.mux %s1, (comb.mux %s0, %d, %c), (comb.mux %s0, %b, %a)` -- an
// OUTER mux whose true/false legs are BOTH themselves `comb.mux`, sharing
// the exact SAME (single) inner select `%s0`, distinct from the outer
// select `%s1` -- realizes `Y = s1 ? (s0?d:c) : (s0?b:a)`, i.e. a genuine
// 4:1 select over independent select bits {s1,s0}. This has NO binate
// obstruction (both legs are already select structures, not a
// select-mixed-with-logic shape), and libraries have real cells for exactly
// this (MUX4-class: pins A,B,C,D,S0,S1,Y). Confirmed present on the
// real design (17 genuine matches out of 454 total muxes, cross-checked
// via a standalone script before implementing this). Needs
// `LibertyLibrary`'s truth-table type widened from 4 to 6 inputs (4 data
// + 2 select) -- see LibertyLibrary.h/.cpp.
//
// With no --liberty= option given, the pass falls back to its original M0
// no-op census behavior (op-count reporting only) so existing callers/tests
// that don't pass one still get well-defined behavior.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

enum class GateKind { And, Or, Xor };

// Everything needed to rewrite one matched mux+gate cone.
struct FusionCandidate {
  Value cond, p, q, other;
  bool fuseTrue; // true: the AND/OR/XOR feeds the mux's *true* leg.
  GateKind gate;
  Operation *gateOp;
};

std::optional<FusionCandidate> classifyLeg(comb::MuxOp muxOp, bool fuseTrue) {
  Value fuseLeg = fuseTrue ? muxOp.getTrueValue() : muxOp.getFalseValue();
  Value otherLeg = fuseTrue ? muxOp.getFalseValue() : muxOp.getTrueValue();
  Operation *def = fuseLeg.getDefiningOp();
  if (!def || !def->hasOneUse())
    return std::nullopt;

  FusionCandidate cand;
  cand.cond = muxOp.getCond();
  cand.other = otherLeg;
  cand.fuseTrue = fuseTrue;
  cand.gateOp = def;

  if (auto andOp = dyn_cast<comb::AndOp>(def)) {
    if (andOp.getInputs().size() != 2)
      return std::nullopt;
    cand.gate = GateKind::And;
    cand.p = andOp.getInputs()[0];
    cand.q = andOp.getInputs()[1];
  } else if (auto orOp = dyn_cast<comb::OrOp>(def)) {
    if (orOp.getInputs().size() != 2)
      return std::nullopt;
    cand.gate = GateKind::Or;
    cand.p = orOp.getInputs()[0];
    cand.q = orOp.getInputs()[1];
  } else if (auto xorOp = dyn_cast<comb::XorOp>(def)) {
    if (xorOp.getInputs().size() != 2)
      return std::nullopt;
    cand.gate = GateKind::Xor;
    cand.p = xorOp.getInputs()[0];
    cand.q = xorOp.getInputs()[1];
  } else {
    return std::nullopt;
  }
  return cand;
}

bool evalGate(GateKind g, bool p, bool q) {
  switch (g) {
  case GateKind::And:
    return p && q;
  case GateKind::Or:
    return p || q;
  case GateKind::Xor:
    return p != q;
  }
  llvm_unreachable("unhandled GateKind");
}

// Builds the cone's leaf list (de-duplicated by Value identity -- e.g. the
// mux's condition might coincide with one of the gate's own inputs) and its
// truth table indexed by leaf assignment.
struct ConeInfo {
  SmallVector<Value, 4> leaves;
  TruthTable table;
};

ConeInfo buildCone(const FusionCandidate &cand) {
  ConeInfo cone;
  auto leafIndex = [&](Value v) -> unsigned {
    for (auto [i, lv] : llvm::enumerate(cone.leaves))
      if (lv == v)
        return i;
    cone.leaves.push_back(v);
    return cone.leaves.size() - 1;
  };
  unsigned condIdx = leafIndex(cand.cond);
  unsigned pIdx = leafIndex(cand.p);
  unsigned qIdx = leafIndex(cand.q);
  unsigned otherIdx = leafIndex(cand.other);

  unsigned arity = cone.leaves.size();
  TruthTable table = 0;
  for (unsigned a = 0; a < (1u << arity); ++a) {
    bool c = (a >> condIdx) & 1;
    bool p = (a >> pIdx) & 1;
    bool q = (a >> qIdx) & 1;
    bool o = (a >> otherIdx) & 1;
    bool g = evalGate(cand.gate, p, q);
    bool out = cand.fuseTrue ? (c ? g : o) : (c ? o : g);
    if (out)
      table |= (static_cast<TruthTable>(1) << a);
  }
  cone.table = table;
  return cone;
}

// Defense-in-depth: independently re-simulate the cone against the matched
// cell's own truth table under the claimed leaf<->slot wiring, before
// committing the rewrite. Per the project's correctness discipline this is a
// hard gate, not optional instrumentation -- a mismatch here means a bug in
// buildCone()/LibertyLibrary::lookup(), and must abort the match rather
// than silently produce a wrong netlist.
bool verifyMatch(const ConeInfo &cone, const CellMatch &match) {
  unsigned arity = cone.leaves.size();
  // For every leaf assignment, the cone's output must equal the cell's
  // output when the cell's input slots are driven by the corresponding
  // leaves per leafForCellInputSlot.
  for (unsigned assign = 0; assign < (1u << arity); ++assign) {
    unsigned cellAssign = 0;
    for (unsigned slot = 0; slot < arity; ++slot) {
      unsigned leaf = match.leafForCellInputSlot[slot];
      unsigned bit = (assign >> leaf) & 1;
      cellAssign |= bit << slot;
    }
    bool coneOut = (cone.table >> assign) & 1;
    bool cellOut = (match.cell->table >> cellAssign) & 1;
    if (match.inverted)
      cellOut = !cellOut;
    if (coneOut != cellOut)
      return false;
  }
  return true;
}

//===----------------------------------------------------------------------===//
// Pattern 2: nested mux-of-mux ("wide select") fusion.
//===----------------------------------------------------------------------===//

// Everything needed to rewrite one matched wide-select cone. `a`/`b`/`c`/`d`
// may be any integer width (matching the outer mux's own result type);
// `s1`/`s0` are always i1 (comb.mux's `cond` operand is always i1, by its
// own definition, regardless of the mux's data width).
struct WideMuxCandidate {
  Value s1, s0, a, b, c, d;
  comb::MuxOp innerTrue, innerFalse; // to be erased once the rewrite commits
};

// Recognizes `mux(s1, mux(s0,d,c), mux(s0,b,a))` == `s1 ? (s0?d:c) : (s0?b:a)`
// -- a genuine 4:1 select over independent select bits {s1,s0}, matching
// real MUX4-class library cells (pins A,B,C,D,S0,S1,Y). Both inner
// muxes must be single-use (they're being eliminated, replaced by one wide
// instance) -- the data legs a/b/c/d are NOT required to be single-use,
// since they're only rewired to a new consumer, never computed twice.
std::optional<WideMuxCandidate> classifyWideMux(comb::MuxOp outer) {
  if (!isa<IntegerType>(outer.getResult().getType()))
    return std::nullopt; // v1 scope: integer data only, no aggregates.

  auto innerTrue = outer.getTrueValue().getDefiningOp<comb::MuxOp>();
  auto innerFalse = outer.getFalseValue().getDefiningOp<comb::MuxOp>();
  if (!innerTrue || !innerFalse || !innerTrue->hasOneUse() ||
      !innerFalse->hasOneUse())
    return std::nullopt;

  Value s0 = innerTrue.getCond();
  if (s0 != innerFalse.getCond() || s0 == outer.getCond())
    return std::nullopt; // not the shared-inner-select shape.

  WideMuxCandidate cand;
  cand.s1 = outer.getCond();
  cand.s0 = s0;
  cand.d = innerTrue.getTrueValue();
  cand.c = innerTrue.getFalseValue();
  cand.b = innerFalse.getTrueValue();
  cand.a = innerFalse.getFalseValue();
  cand.innerTrue = innerTrue;
  cand.innerFalse = innerFalse;
  return cand;
}

// Builds the (structural, width-independent) cone for a wide-mux candidate:
// leaves are the up-to-6 distinct Values {s1,s0,a,b,c,d} (de-duplicated by
// identity, same as buildCone), and the table encodes
// `Y = s1 ? (s0?d:c) : (s0?b:a)` treating every leaf as an independent
// Boolean variable -- this is deliberately width-independent (the same
// relationship holds identically at every bit position of a/b/c/d, since
// the select bits s1/s0 are already always 1-bit), so this single cone
// drives the liberty lookup once, then the same match is reused for every
// bit of the actual (possibly wide) rewrite.
ConeInfo buildWideMuxCone(const WideMuxCandidate &cand) {
  ConeInfo cone;
  auto leafIndex = [&](Value v) -> unsigned {
    for (auto [i, lv] : llvm::enumerate(cone.leaves))
      if (lv == v)
        return i;
    cone.leaves.push_back(v);
    return cone.leaves.size() - 1;
  };
  unsigned s1Idx = leafIndex(cand.s1);
  unsigned s0Idx = leafIndex(cand.s0);
  unsigned aIdx = leafIndex(cand.a);
  unsigned bIdx = leafIndex(cand.b);
  unsigned cIdx = leafIndex(cand.c);
  unsigned dIdx = leafIndex(cand.d);

  unsigned arity = cone.leaves.size();
  TruthTable table = 0;
  for (unsigned assign = 0; assign < (1u << arity); ++assign) {
    bool s1 = (assign >> s1Idx) & 1;
    bool s0 = (assign >> s0Idx) & 1;
    bool a = (assign >> aIdx) & 1;
    bool b = (assign >> bIdx) & 1;
    bool c = (assign >> cIdx) & 1;
    bool d = (assign >> dIdx) & 1;
    bool out = s1 ? (s0 ? d : c) : (s0 ? b : a);
    if (out)
      table |= (static_cast<TruthTable>(1) << assign);
  }
  cone.table = table;
  return cone;
}

struct StructuralMapperPass
    : public PassWrapper<StructuralMapperPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(StructuralMapperPass)

  StructuralMapperPass() = default;
  StructuralMapperPass(const StructuralMapperPass &other)
      : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    enableWideMuxFusion = other.enableWideMuxFusion.getValue();
    wideMuxSelectFanoutThreshold = other.wideMuxSelectFanoutThreshold.getValue();
  }

  StringRef getArgument() const final { return "structural-mapper"; }
  StringRef getDescription() const final {
    return "RTL-level structural technology mapping on HW/Comb IR (M2: "
           "mux/compare-fusion pattern)";
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Path to the liberty file used to build the "
                      "truth-table cell-matching oracle")};

  // OFF by default: Pattern 2 is structurally correct (SAT-verified) but
  // measured as a real area REGRESSION on the whole real design --
  // freezing these cones as opaque
  // hw.instance boundaries blocks ABC's own fanout/buffer-load-balancing
  // optimization around the shared select signals more than the fused
  // cells themselves save. Kept available (not deleted) as tested
  // infrastructure for a future area-delta-aware version; run_circt_
  // native_synth.sh's default pipeline must not regress silently just
  // because structural-mapper is unconditionally in it.
  Option<bool> enableWideMuxFusion{
      *this, "enable-wide-mux-fusion",
      llvm::cl::desc("Enable Pattern 2 (nested mux-of-mux wide-select "
                      "fusion) -- OFF by default, see this option's "
                      "declaration comment"),
      llvm::cl::init(false)};

  // Tier 3 roadmap item 11 prototype: conservative economics gate on top of
  // Pattern 2, OFF by default (threshold 0 == unlimited == gate disabled,
  // so the ORIGINAL, already-measured Pattern 2 behavior is reproduced
  // whenever this isn't explicitly set) -- see passesSelectFanoutGate()'s
  // doc comment for the full rationale. Independent of enableWideMuxFusion
  // itself so the two can be A/B'd against each other.
  Option<unsigned> wideMuxSelectFanoutThreshold{
      *this, "wide-mux-select-fanout-threshold",
      llvm::cl::desc("Reject a Pattern-2 wide-mux match whose s0 or s1 "
                      "select signal has more than this many total SSA "
                      "uses in its hw.module (economics gate motivated by "
                      "Pattern 2's measured regression root cause; 0 = "
                      "unlimited, i.e. gate disabled)"),
      llvm::cl::init(0)};

  hw::HWModuleExternOp getOrCreateExternDecl(ModuleOp module,
                                              const CellPattern &cell) {
    if (auto existing = externDecls.lookup(cell.name))
      return existing;

    SmallVector<hw::PortInfo, 5> ports;
    Type i1 = IntegerType::get(module.getContext(), 1);
    for (auto [idx, pinName] : llvm::enumerate(cell.inputPins)) {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), pinName);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Input;
      port.argNum = idx;
      ports.push_back(port);
    }
    {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), cell.outputPin);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Output;
      port.argNum = 0;
      ports.push_back(port);
    }

    OpBuilder builder = OpBuilder::atBlockBegin(module.getBody());
    auto externOp = hw::HWModuleExternOp::create(
        builder, module.getLoc(),
        StringAttr::get(module.getContext(), cell.name), ports);
    externDecls[cell.name] = externOp;
    return externOp;
  }

  bool tryMatchAndRewrite(comb::MuxOp muxOp, ModuleOp module) {
    for (bool fuseTrue : {true, false}) {
      auto cand = classifyLeg(muxOp, fuseTrue);
      if (!cand)
        continue;
      ConeInfo cone = buildCone(*cand);
      unsigned arity = cone.leaves.size();
      if (arity < 2 || arity > 4)
        continue;

      auto match = library->lookup(arity, cone.table);
      if (!match)
        continue;
      if (!verifyMatch(cone, *match)) {
        // Should be unreachable if buildCone()/lookup() are both correct;
        // treat as a hard bug rather than silently miscompiling.
        muxOp.emitError("structural-mapper: internal truth-table mismatch "
                         "for candidate cell ")
            << match->cell->name << " -- refusing to rewrite";
        continue;
      }

      hw::HWModuleExternOp externOp = getOrCreateExternDecl(module, *match->cell);
      SmallVector<Value, 4> operands;
      for (unsigned slot = 0; slot < arity; ++slot)
        operands.push_back(cone.leaves[match->leafForCellInputSlot[slot]]);

      OpBuilder builder(muxOp);
      std::string instName =
          ("u_" + match->cell->name + "_" + Twine(instanceCounter++)).str();
      auto inst = hw::InstanceOp::create(
          builder, muxOp.getLoc(), externOp,
          builder.getStringAttr(instName), operands);
      Value result = inst.getResult(0);
      if (match->inverted) {
        // Most of this PDK's real compound gates are inherently
        // output-inverted (see CellMatch::inverted) -- a single extra INV
        // here is still a net win vs. the discrete mux+gate this replaces,
        // and is far simpler than pushing the inversion into every
        // consumer. ABC/Yosys will happily absorb a trailing inverter into
        // a downstream gate's input polarity during its own mapping pass
        // if one exists, same as it would for any other INV in the design.
        Value trueConst = hw::ConstantOp::create(builder, muxOp.getLoc(),
                                                  APInt(1, 1));
        result = comb::XorOp::create(builder, muxOp.getLoc(), result,
                                      trueConst);
      }
      muxOp.getResult().replaceAllUsesWith(result);
      Operation *gateOp = cand->gateOp;
      muxOp.erase();
      if (gateOp->use_empty())
        gateOp->erase();
      return true;
    }
    return false;
  }

  // Pattern 2. `erased` records every op this call (or an earlier Pattern-2
  // match in the same sweep) has erased -- needed because, unlike Pattern
  // 1 (which only ever erases a gate op that's never itself a candidate
  // mux), a committed match here erases TWO other `comb.mux` ops that may
  // ALSO be present -- as their own, now-stale entries -- in the caller's
  // upfront-collected candidate list. `runOnOperation` checks this set
  // before processing each cached candidate, so a match that consumes a
  // not-yet-visited candidate never leaves it dangling.
  // Tier 3 roadmap item 11: conservative area-delta-aware economics gate.
  // Pattern 2's own measured root cause for its area regression is NOT the
  // fused cells being wrong or too big -- it's that freezing each matched
  // cone as an opaque hw.instance boundary blocks ABC's own fanout/buffer-
  // load-balancing optimization specifically on the SHARED select signals
  // (s0/s1) that end up feeding many separate frozen MUX4 instances
  // (the buffer count alone roughly tripled). This is a real cost
  // ABC would otherwise absorb by restructuring/rebalancing that fanout
  // tree; once frozen behind an instance boundary ABC can no longer do that
  // restructuring across it.
  //
  // A precise cost model would need to predict ABC's own future decisions,
  // which is intractable -- so this is deliberately a blunt, conservative
  // proxy: snapshot each select signal's TOTAL SSA use count (every
  // consumer in the hw.module, not just other wide-mux candidates) BEFORE
  // any Pattern-2 rewriting begins in this module, and refuse to commit a
  // match whose s0 or s1 exceeds a small fixed threshold. Taking the
  // snapshot upfront (rather than re-querying live use-counts as matches
  // commit and erase inner muxes) makes the decision independent of walk
  // order -- a match's gate outcome doesn't depend on which other matches
  // happened to be visited first.
  //
  // This ONLY narrows what gets matched (a strict subset of the already
  // SAT-verified-correct Pattern 2 matches) -- it can reject a real match,
  // never accept an invalid one, so it adds no new correctness risk beyond
  // what Pattern 2 already carries.
  bool passesSelectFanoutGate(const WideMuxCandidate &cand,
                               const llvm::DenseMap<Value, unsigned> &selectFanout) {
    if (wideMuxSelectFanoutThreshold == 0)
      return true; // 0 == gate disabled (reproduces original ungated Pattern 2).
    unsigned s0Fanout = selectFanout.lookup(cand.s0);
    unsigned s1Fanout = selectFanout.lookup(cand.s1);
    return s0Fanout <= wideMuxSelectFanoutThreshold &&
           s1Fanout <= wideMuxSelectFanoutThreshold;
  }

  bool tryMatchWideMuxAndRewrite(comb::MuxOp muxOp, ModuleOp module,
                                  llvm::SmallPtrSetImpl<Operation *> &erased,
                                  const llvm::DenseMap<Value, unsigned> &selectFanout,
                                  unsigned &numRejectedByGate) {
    auto cand = classifyWideMux(muxOp);
    if (!cand)
      return false;

    if (!passesSelectFanoutGate(*cand, selectFanout)) {
      ++numRejectedByGate;
      return false;
    }

    ConeInfo cone = buildWideMuxCone(*cand);
    unsigned arity = cone.leaves.size();
    if (arity < 2 || arity > 6)
      return false;

    auto match = library->lookup(arity, cone.table);
    if (!match)
      return false;
    if (!verifyMatch(cone, *match)) {
      muxOp.emitError("structural-mapper: internal truth-table mismatch "
                       "for candidate cell ")
          << match->cell->name << " -- refusing to rewrite";
      return false;
    }

    hw::HWModuleExternOp externOp = getOrCreateExternDecl(module, *match->cell);
    Type resultTy = muxOp.getResult().getType();
    int64_t width = hw::getBitWidth(resultTy);

    OpBuilder builder(muxOp);
    Location loc = muxOp.getLoc();
    SmallVector<Value> bitResults; // LSB-first, per-bit instance outputs.
    for (int64_t bit = 0; bit < width; ++bit) {
      SmallVector<Value, 6> operands;
      for (unsigned slot = 0; slot < arity; ++slot) {
        Value leaf = cone.leaves[match->leafForCellInputSlot[slot]];
        // Every leaf here is either one of the two (always-i1) select
        // operands, used as-is at every bit position, or one of the
        // (outer-mux-width) data legs, sliced per bit -- distinguished by
        // the leaf's OWN width, not by which of s1/s0/a/b/c/d it is.
        if (hw::getBitWidth(leaf.getType()) == 1)
          operands.push_back(leaf);
        else
          operands.push_back(
              comb::ExtractOp::create(builder, loc, builder.getIntegerType(1),
                                       leaf, bit));
      }
      std::string instName = ("u_" + match->cell->name + "_" +
                               Twine(instanceCounter++))
                                  .str();
      auto inst = hw::InstanceOp::create(builder, loc, externOp,
                                          builder.getStringAttr(instName),
                                          operands);
      bitResults.push_back(inst.getResult(0));
    }

    Value result;
    if (width == 1) {
      result = bitResults.front();
    } else {
      SmallVector<Value> concatOperands(llvm::reverse(bitResults));
      result = comb::ConcatOp::create(builder, loc, concatOperands);
    }
    if (result.getType() != resultTy)
      result = hw::BitcastOp::create(builder, loc, resultTy, result);
    if (match->inverted) {
      // Invert the WHOLE reassembled word at once (equivalent to, but
      // simpler than, inverting each of the `width` per-bit instance
      // outputs individually before concatenation).
      Value allOnes = hw::ConstantOp::create(
          builder, loc, APInt::getAllOnes(width));
      result = comb::XorOp::create(builder, loc, result, allOnes);
    }

    muxOp.getResult().replaceAllUsesWith(result);
    Operation *innerTrueOp = cand->innerTrue.getOperation();
    Operation *innerFalseOp = cand->innerFalse.getOperation();
    erased.insert(muxOp.getOperation());
    erased.insert(innerTrueOp);
    erased.insert(innerFalseOp);
    muxOp.erase();
    innerTrueOp->erase();
    innerFalseOp->erase();
    return true;
  }

  void runCensusMode(ModuleOp module) {
    size_t numHwModules = 0, numMux = 0, numICmp = 0, numAnd = 0, numOr = 0,
           numXor = 0;
    module.walk([&](hw::HWModuleOp hwModule) {
      ++numHwModules;
      hwModule.walk([&](Operation *op) {
        if (isa<comb::MuxOp>(op))
          ++numMux;
        else if (isa<comb::ICmpOp>(op))
          ++numICmp;
        else if (isa<comb::AndOp>(op))
          ++numAnd;
        else if (isa<comb::OrOp>(op))
          ++numOr;
        else if (isa<comb::XorOp>(op))
          ++numXor;
      });
    });
    llvm::errs() << "[structural-mapper/M0 census] hw.modules=" << numHwModules
                 << " comb.mux=" << numMux << " comb.icmp=" << numICmp
                 << " comb.and=" << numAnd << " comb.or=" << numOr
                 << " comb.xor=" << numXor << "\n";
  }

  void runOnOperation() override {
    ModuleOp module = getOperation();

    if (libertyPath.empty()) {
      runCensusMode(module);
      return;
    }

    auto loaded = LibertyLibrary::loadFromFile(libertyPath);
    if (!loaded) {
      module.emitError("structural-mapper: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    library = std::move(loaded);
    externDecls.clear();
    // Seed the cache with declarations an EARLIER pass already created
    // (map-compressor-cells emits its own cell externs), or the first
    // lookup of the same cell here would declare a second @<cell> and the
    // module would fail to verify on a duplicate symbol.
    for (auto ext : module.getOps<hw::HWModuleExternOp>())
      externDecls[ext.getModuleName()] = ext;
    instanceCounter = 0;

    size_t numMuxesSeen = 0, numFused = 0, numWideMuxFused = 0;
    unsigned numWideMuxRejectedByGate = 0;
    for (auto hwModule :
         llvm::make_early_inc_range(module.getOps<hw::HWModuleOp>())) {
      // Pattern 2 first, over EVERY integer-typed mux (any width) --
      // collected upfront like Pattern 1's own loop below, but unlike
      // Pattern 1, a committed match here erases TWO other entries in
      // this SAME list (the consumed inner muxes), so each cached
      // candidate is checked against `erased` before use: hw.module
      // bodies are graph regions (arbitrary walk order, not necessarily
      // dependency order -- see OutlineResidualLogicPass.cpp's own
      // comment), so an outer mux can easily be visited, and its inner
      // legs erased, before the walk would otherwise have reached those
      // same inner muxes' own list entries.
      llvm::SmallPtrSet<Operation *, 16> erased;
      if (enableWideMuxFusion) {
        SmallVector<comb::MuxOp, 16> wideMuxes;
        hwModule.walk([&](comb::MuxOp m) {
          if (isa<IntegerType>(m.getResult().getType()))
            wideMuxes.push_back(m);
        });
        // Snapshot every candidate's select-signal use count BEFORE any
        // rewriting in this hw.module, so the gate's decisions are stable
        // and independent of walk/rewrite order -- see
        // passesSelectFanoutGate()'s doc comment.
        llvm::DenseMap<Value, unsigned> selectFanout;
        for (comb::MuxOp m : wideMuxes) {
          if (auto c = classifyWideMux(m)) {
            selectFanout.try_emplace(c->s0, c->s0.getNumUses());
            selectFanout.try_emplace(c->s1, c->s1.getNumUses());
          }
        }
        for (comb::MuxOp muxOp : wideMuxes) {
          if (erased.contains(muxOp.getOperation()))
            continue; // consumed as an inner leg by an earlier match above.
          if (tryMatchWideMuxAndRewrite(muxOp, module, erased, selectFanout,
                                         numWideMuxRejectedByGate))
            ++numWideMuxFused;
        }
      }

      SmallVector<comb::MuxOp, 16> muxes;
      hwModule.walk([&](comb::MuxOp m) {
        if (m.getResult().getType().isSignlessInteger(1))
          muxes.push_back(m);
      });
      for (comb::MuxOp muxOp : muxes) {
        // Safe to iterate without re-checking liveness: a rewrite only ever
        // erases the mux it matched plus (optionally) its fused gate op,
        // which is always an And/Or/Xor -- never another entry in `muxes`.
        // (Pattern 2 above already ran to completion on this hw.module and
        // only erases muxes it matched -- classifyLeg() itself never
        // matches a comb.mux-typed leg anyway, since it only recognizes
        // And/Or/Xor, so there's no possibility of Pattern 2 leaving a
        // stale reference for THIS loop.)
        ++numMuxesSeen;
        if (tryMatchAndRewrite(muxOp, module))
          ++numFused;
      }
    }

    llvm::errs() << "[structural-mapper] liberty cells indexed="
                 << library->size() << " muxes examined=" << numMuxesSeen
                 << " fused=" << numFused << " wide-muxes fused="
                 << numWideMuxFused << " wide-muxes rejected by fanout gate="
                 << numWideMuxRejectedByGate << " extern cells used="
                 << externDecls.size() << "\n";
  }

private:
  std::optional<LibertyLibrary> library;
  llvm::StringMap<hw::HWModuleExternOp> externDecls;
  unsigned instanceCounter = 0;
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createStructuralMapperPass() {
  return std::make_unique<StructuralMapperPass>();
}

} // namespace circt_abc
