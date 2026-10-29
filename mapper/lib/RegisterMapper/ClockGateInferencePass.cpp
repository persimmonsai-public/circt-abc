//===- ClockGateInferencePass.cpp ------------------------------*- C++ -*-===//
//
// See Passes.td's ClockGateInference description for the "why" (cell-usage
// comparison against a real Yosys netlist found circt-abc's flow uses ~4.7x
// more discrete mux cells and ~1/7th as many clock-gate cells -- traced to
// Yosys's synth.tcl running a dedicated `clockgate` inference pass on plain
// enable-conditioned registers, which nothing in this flow's pipeline does:
// register-mapper always bit-blasts whatever `next` shape it's given into a
// plain always-clocked DFF, so an RTL idiom like
// `always @(posedge clk) if (en) q <= d;` keeps its `next = mux(en, d, q)`
// hold-value mux forever instead of ever being converted into a gated-clock
// DFF with no hold-mux at all).
//
// Scoped to mlir::ModuleOp (not hw::HWModuleOp), even though the actual rewrite
// is entirely per-hw.module -- same reason RegisterMapperPass is
// ModuleOp-scoped: this pass introduces ONE shared top-level `hw.module.extern`
// declaration for the ICG cell, cached across every hw.module it processes. A
// first version scoped this per hw::HWModuleOp instead and hit a real bug: MLIR
// runs OperationPass<hw::HWModuleOp> instances in parallel across sibling
// modules by default, each getting its own CLONED pass object (via the copy
// ctor), so two clones independently created and tried to declare the same
// extern symbol -> "redefinition of symbol" verifier error. A single
// ModuleOp-scoped pass with a manual internal loop (matching
// RegisterMapperPass's own structure exactly) has one pass instance, one cache,
// no cloning, no collision.
//
// Must run AFTER RegisterLegalize and BEFORE RegisterMapper:
// RegisterLegalize may wrap a sync/non-constant-reset firreg's `next` in an
// outer `mux(reset, resetValue, next)`, which would sit on top of (and
// hide) the enable-mux shape this pass looks for -- running after it keeps
// the common case (async, constant-resetValue reset, which RegisterLegalize
// leaves untouched) working, at the cost of not recognizing the enable
// pattern underneath a sync-reset wrapper (a coverage gap, not a
// correctness risk: such a firreg is simply left ungated, exactly as it is
// today). RegisterMapper must run after this pass so it sees the
// already-gated clock and the hold-mux-free `next` directly, with no
// special-casing of its own needed.
//
// Algorithm, per hw::HWModuleOp:
//   1. For each seq.firreg F, check whether F.next is a comb.mux whose
//      TRUE or FALSE leg is exactly F's own `data` result (the classic
//      "hold current value" self-loop) and whose OTHER leg is a distinct
//      value -- this is the only enable shape recognized (a plain
//      `if (en) q <= d;`/`if (stall) q <= q; else q <= d;` idiom, covering
//      both cond polarities). Anything else (unconditional load, a
//      multi-way mux tree, etc.) is left untouched.
//   2. Group matching firregs by (clock, active-high-load-enable) --
//      canonicalizing polarity so `mux(cond, hold=data, load=new)` (loads
//      when cond=0) and `mux(cond, load=new, hold=data)` (loads when
//      cond=1) with the same underlying enable end up in the same group,
//      inverting the condition (once per distinct condition, cached) for
//      the former case.
//   3. Skip any group whose *total* bit width is below `minGroupWidth`: an
//      integrated clock-gating cell only pays for itself once it replaces
//      enough per-bit hold-muxes to beat its own cost -- below that, gating
//      would be a net regression, the same class of "freezing costs more
//      than it saves" economics mistake Pattern 2 (nested mux-of-mux
//      fusion, see StructuralMapperPass.cpp) was measured making. In a
//      typical library the cheapest ICG cell costs one to two 2:1 muxes,
//      so the default of 2 is the rounded-up breakeven, deliberately
//      conservative since the pass always picks the cheapest cell
//      available, never the priciest.
//   4. For each surviving group: instantiate ONE shared instance of
//      whichever real posedge-precontrol integrated clock-gating cell
//      LibertyLibrary::lookupICGCell() found to be cheapest across the
//      given liberty file(s) -- see LibertyLibrary.h/.cpp's ICGCellPattern
//      for how candidates are found and restricted (excludes the wrong
//      clock edge and unvalidated cell types). A library may offer
//      several functionally identical (same `state_function`) ICG cells,
//      possibly in different liberty files, and the design's own RTL
//      clock-gate wrapper may instantiate one that is not the cheapest;
//      this pass's own newly inferred gates use whichever is genuinely
//      cheapest, independent of what a pre-existing RTL instantiation
//      elsewhere in the design
//      picked. (CK = seq.from_clock(sharedClock), E = enable, SE =
//      tied-low constant), cast its output back to !seq.clock via
//      seq.to_clock, and rebuild every firreg in the group with `next`
//      replaced by the bare load value (hold-mux gone) and `clk` replaced
//      by the gated clock -- reset/resetValue/isAsync/preset/name are
//      carried over unchanged (an async reset's clear/preset pin at the
//      liberty-cell level is independent of the clock signal entirely, so
//      gating the clock does not interact with it).
//
// This pass follows RegisterLegalizePass.cpp's own convention for
// modifying a firreg: build a brand-new seq::FirRegOp with the desired
// operands and replace-all-uses-with, rather than mutating operands
// in place.
//
// Bit-level enables (`bit-level-enables=true`, OFF by default -- project
// convention for a new lever). The top-level shape above misses the
// entry-array idiom. A TLB of, say, 16 entries x 112 bits has `next` = a
// 16-link chain of ARRAY-WIDE muxes -- flush / per-entry update, each link
// a concat of slices of the previous link with one entry replaced -- so no
// mux at the register's width has the register's own data as a leg, step 1
// leaves it, and register-mapper bit-blasts the chain into ~2 hold-muxes
// per bit, which then make up most of the TLB's area. Per BIT the same
// chain is exactly a hold-mux tree: entry i's bit resolves through
// concat/extract provenance to "load the update when !flush & upd_en_i,
// else q", i.e. one clock gate per entry.
//
// Per register: for each bit, walk `next` through comb.concat / extract /
// replicate / round-trip hw.bitcast and comb.mux, resolving (enable, load)
// with the enable a small canonical DNF over i1 condition Values (a leg
// that resolves to the register's own bit is a hold: enable FALSE; a leg
// that resolves to anything else is a load: enable TRUE). Bits are grouped
// by DNF; each group becomes its own sub-register on a gated clock (shared
// design-wide with the whole-register candidates through the same
// EnableKey / min-group-width machinery), and the original data is
// re-assembled from the sub-registers. Bits whose enable is TRUE (always
// load), FALSE (never changes: narrow-constant-register-bits' job), or
// whose DNF exceeds the cap stay on the plain clock with their original
// next. Function-preserving by construction: on any cycle where a gated
// part's enable is 0, every one of its bits' next equals its q.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "../StructuralMapper/CombFacts.h"
#include "../StructuralMapper/IROrder.h"
#include "../StructuralMapper/LibertyLibrary.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/SymbolTable.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/MapVector.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Support/raw_ostream.h"

#include <map>
#include <optional>
#include <tuple>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Milestone 10 (design-management-layer directive surface): does a
// `dont-touch` list entry (bare "<firreg_name>" or qualified
// "<module_name>:<firreg_name>") match this firreg in this module? A
// bare entry (no ':') matches the name in ANY module, exactly preserving
// milestone 7's original behavior; a qualified entry only matches within
// the named module. Mirrors NarrowDeadRegisterBitsPass.cpp's own
// dontTouchEntryMatches() -- small enough, and specific enough to each
// pass's own option, that a shared header wasn't judged worth it.
static bool dontTouchEntryMatches(StringRef entry, StringRef moduleName,
                                   StringRef firregName) {
  auto colon = entry.find(':');
  if (colon == StringRef::npos)
    return entry == firregName;
  return entry.substr(0, colon) == moduleName &&
         entry.substr(colon + 1) == firregName;
}

struct GatedCandidate {
  seq::FirRegOp firreg;
  Value newValue; // the value loaded when the (canonicalized) enable is 1.
  unsigned width;
};

// A canonical, order-independent key for an enable expression, used to
// widen this pass's grouping predicate beyond exact SSA `Value` identity
// (today's baseline) to also catch a commutative `comb.and`/`comb.or`
// built from the same operand multiset in a different order -- e.g.
// `comb.and %a, %b` and `comb.and %b, %a` are the exact same function but
// are two distinct, unequal SSA values (and a plain CSE pass would not
// merge them either, since CSE requires the operand *list*, not just the
// set, to match). This deliberately goes no further than that: it never
// reasons about semantic equivalence ACROSS different operations (no
// De Morgan's, no idempotence/absorption, no constant folding of the
// operand set) -- that needs a real equivalence checker, out of scope for
// this conservative widening. Two same-kind (both AndOp or both OrOp)
// nested chains are additionally flattened via associativity (`(a&b)&c`
// and `a&(b&c)` and `a&b&c` are all literally the same value), since
// AndOp/OrOp are n-ary in this dialect and this pass runs BEFORE the
// pipeline's own `canonicalize` step would otherwise flatten/normalize
// them itself (see this file's header comment on pass ordering) --
// exactly the gap a same-run `--cse` experiment (no change in the group
// count) already showed CSE alone does not close.
struct EnableKey {
  // 0 = exact-value fallback (today's behavior: anything that isn't an
  //     immediate AndOp/OrOp result, e.g. a bare port or a comb.xor from
  //     polarity inversion), 1 = AndOp-shaped, 2 = OrOp-shaped.
  unsigned kind = 0;
  bool twoState = false;          // valid iff kind != 0; must also match.
  Value value;                    // valid iff kind == 0.
  SmallVector<Value, 4> operands; // valid iff kind != 0; sorted by IR
                                   // position so operand ORDER doesn't
                                   // affect equality/hashing, only the
                                   // multiset does.

  bool operator==(const EnableKey &other) const {
    if (kind != other.kind)
      return false;
    if (kind == 0)
      return value == other.value;
    return twoState == other.twoState && operands == other.operands;
  }
};

// Recursively flattens same-kind (OpTy), same-`twoState` associative
// chains into a flat leaf list -- purely syntactic (only ever unfolds the
// SAME op it started with, gated on a matching `twoState` bit too), so
// this can never change what a value actually computes, only how many
// levels of it this key's equality can see through.
template <typename OpTy>
static void flattenAssociativeOperands(Value v, bool expectedTwoState,
                                        SmallVectorImpl<Value> &leaves) {
  if (auto op = v.getDefiningOp<OpTy>()) {
    if (op.getTwoState() == expectedTwoState) {
      for (Value operand : op.getInputs())
        flattenAssociativeOperands<OpTy>(operand, expectedTwoState, leaves);
      return;
    }
  }
  leaves.push_back(v);
}

static EnableKey canonicalizeEnable(Value enable) {
  EnableKey key;
  if (auto andOp = enable.getDefiningOp<comb::AndOp>()) {
    key.kind = 1;
    key.twoState = andOp.getTwoState();
    flattenAssociativeOperands<comb::AndOp>(enable, key.twoState,
                                             key.operands);
  } else if (auto orOp = enable.getDefiningOp<comb::OrOp>()) {
    key.kind = 2;
    key.twoState = orOp.getTwoState();
    flattenAssociativeOperands<comb::OrOp>(enable, key.twoState,
                                            key.operands);
  } else {
    key.kind = 0;
    key.value = enable;
    return key;
  }
  // IR order, not pointer order: equality only needs SOME canonical order,
  // but a pointer order differs from process to process (see IROrder.h).
  llvm::sort(key.operands, irOrderLess);
  return key;
}

// One (clock, canonicalized-enable) group: all its members share a clock
// and are driven by provably-identical (see EnableKey above) enables, so
// they can safely share ONE physical clock-gate cell wired from
// `representativeEnable` -- any one member's actual enable Value works as
// the physical wire since, by construction, every member's own enable is
// semantically identical to it.
struct EnableGroup {
  EnableKey key;
  Value representativeEnable;
  SmallVector<GatedCandidate, 4> members;
  // Bit-level parts (plan index, part index) sharing this enable.
  SmallVector<std::pair<unsigned, unsigned>, 4> bitMembers;
  Value gatedClock; // set once the group is accepted and its ICG built
};

//===----------------------------------------------------------------------===//
// Bit-level enables: per-bit (enable, load) resolution. See the file
// comment's "Bit-level enables" section.
//===----------------------------------------------------------------------===//

// Where does bit `bit` of `v` come from? Walks comb.concat / comb.extract /
// comb.replicate and a round-trip hw.bitcast (integer -> aggregate ->
// the same integer, which lower-aggregate-registers leaves on a lowered
// register's `next`) down to the first value that is none of those.
static std::pair<Value, unsigned> bitProvenance(Value v, unsigned bit,
                                                unsigned fuel = 64) {
  if (!fuel)
    return {v, bit};
  if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
    // comb.concat puts its FIRST operand at the MSB.
    unsigned pos = v.getType().getIntOrFloatBitWidth();
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      if (bit >= pos && bit < pos + ow)
        return bitProvenance(operand, bit - pos, fuel - 1);
    }
    return {v, bit};
  }
  if (auto ex = v.getDefiningOp<comb::ExtractOp>())
    return bitProvenance(ex.getInput(), bit + ex.getLowBit(), fuel - 1);
  if (auto rep = v.getDefiningOp<comb::ReplicateOp>()) {
    unsigned iw = rep.getInput().getType().getIntOrFloatBitWidth();
    return bitProvenance(rep.getInput(), bit % iw, fuel - 1);
  }
  if (auto bc = v.getDefiningOp<hw::BitcastOp>()) {
    if (bc.getInput().getType() == bc.getType())
      return bitProvenance(bc.getInput(), bit, fuel - 1);
    if (auto inner = bc.getInput().getDefiningOp<hw::BitcastOp>())
      if (inner.getInput().getType() == bc.getType())
        return bitProvenance(inner.getInput(), bit, fuel - 1);
  }
  return {v, bit};
}

// Slice [offset, offset+width) of `v`, reusing a concat operand where the
// cut lines up with one so the common case builds nothing.
static Value sliceBits(OpBuilder &b, Location loc, Value v, unsigned offset,
                       unsigned width) {
  unsigned total = v.getType().getIntOrFloatBitWidth();
  if (offset == 0 && width == total)
    return v;
  if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
    unsigned pos = total;
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      if (pos <= offset && offset + width <= pos + ow)
        return sliceBits(b, loc, operand, offset - pos, width);
    }
  }
  return comb::ExtractOp::create(b, loc, b.getIntegerType(width), v, offset);
}

// A literal of an enable: an i1 Value in either polarity.
struct BitLit {
  Value v;
  bool neg;
};
// IR order, never pointer order: the cube and DNF orders built from this
// become the operand order of the enable's comb.and / comb.or (see
// enableValue), so a pointer order made this pass's output -- and the whole
// flow's netlist -- differ from run to run on identical input. See IROrder.h.
static bool litLess(const BitLit &a, const BitLit &b) {
  if (a.v != b.v)
    return irOrderLess(a.v, b.v);
  return a.neg < b.neg;
}
static bool litEq(const BitLit &a, const BitLit &b) {
  return a.v == b.v && a.neg == b.neg;
}
using Cube = SmallVector<BitLit, 4>; // sorted by litLess, no duplicates
static bool cubeLess(const Cube &a, const Cube &b) {
  return std::lexicographical_compare(a.begin(), a.end(), b.begin(), b.end(),
                                      litLess);
}
static bool cubeEq(const Cube &a, const Cube &b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), litEq);
}

// A canonical sum of products over BitLits. Empty = FALSE (the bit never
// loads: a provable hold), one empty cube = TRUE (unconditional load).
// Canonical enough that two bits with the same enable function reached
// through the same conditions compare equal (sorted, unique, absorbed);
// it never proves equivalence across different condition Values.
struct Dnf {
  bool opaque = false;        // over the size cap: handled exactly as TRUE.
  SmallVector<Cube, 4> cubes; // sorted by cubeLess, unique, absorbed.

  static Dnf falseValue() { return Dnf(); }
  static Dnf trueValue() {
    Dnf d;
    d.cubes.push_back(Cube());
    return d;
  }
  bool isFalse() const { return !opaque && cubes.empty(); }
  bool isTrue() const {
    return opaque || (cubes.size() == 1 && cubes[0].empty());
  }
  bool operator==(const Dnf &o) const {
    if (opaque != o.opaque || cubes.size() != o.cubes.size())
      return false;
    for (size_t i = 0; i < cubes.size(); ++i)
      if (!cubeEq(cubes[i], o.cubes[i]))
        return false;
    return true;
  }
};
// Enables wider than this are not worth an ICG's enable logic; the bit is
// left ungated (its next is untouched, so this only costs coverage).
static constexpr unsigned kDnfMaxCubes = 16;
static constexpr unsigned kDnfMaxLits = 8;

static void dnfNormalize(Dnf &d) {
  if (d.opaque) {
    d.cubes.clear();
    return;
  }
  llvm::sort(d.cubes, cubeLess);
  d.cubes.erase(std::unique(d.cubes.begin(), d.cubes.end(), cubeEq),
                d.cubes.end());
  // Absorption: a cube that contains another cube adds nothing.
  SmallVector<Cube, 4> kept;
  for (size_t i = 0; i < d.cubes.size(); ++i) {
    bool absorbed = false;
    for (size_t j = 0; j < d.cubes.size() && !absorbed; ++j)
      if (j != i && d.cubes[j].size() < d.cubes[i].size() &&
          std::includes(d.cubes[i].begin(), d.cubes[i].end(),
                        d.cubes[j].begin(), d.cubes[j].end(), litLess))
        absorbed = true;
    if (!absorbed)
      kept.push_back(d.cubes[i]);
  }
  d.cubes = std::move(kept);
  if (d.cubes.size() > kDnfMaxCubes) {
    d.opaque = true;
    d.cubes.clear();
  }
}

static Dnf dnfAnd(BitLit lit, const Dnf &d) {
  if (d.opaque)
    return d;
  Dnf r;
  for (const Cube &c : d.cubes) {
    bool contradiction = false, present = false;
    for (const BitLit &l : c)
      if (l.v == lit.v)
        (l.neg != lit.neg ? contradiction : present) = true;
    if (contradiction)
      continue;
    Cube n = c;
    if (!present)
      n.insert(std::upper_bound(n.begin(), n.end(), lit, litLess), lit);
    if (n.size() > kDnfMaxLits) {
      r.opaque = true;
      r.cubes.clear();
      return r;
    }
    r.cubes.push_back(std::move(n));
  }
  dnfNormalize(r);
  return r;
}

static Dnf dnfOr(const Dnf &a, const Dnf &b) {
  if (a.opaque)
    return a;
  if (b.opaque)
    return b;
  Dnf r = a;
  r.cubes.append(b.cubes.begin(), b.cubes.end());
  dnfNormalize(r);
  return r;
}

// The value a bit loads when its enable is 1, as a hash-consed tree so
// two bits (or two legs of one mux) that load the same thing compare by
// index. Index 0 is the don't-care of a bit that only ever holds.
struct LoadNode {
  enum Kind { DontCare, Leaf, Mux } kind = DontCare;
  Value v;          // Leaf: the source value; Mux: the condition.
  unsigned bit = 0; // Leaf: the bit of `v`.
  unsigned t = 0, f = 0; // Mux: the legs.
};

class BitLevelAnalyzer {
public:
  struct BitRes {
    Dnf en;
    unsigned load = 0;
    bool operator==(const BitRes &o) const {
      return load == o.load && en == o.en;
    }
  };
  // Deeper mux nesting than this is left as an unconditional load; it
  // bounds the recursion, nothing else.
  static constexpr unsigned kMaxMuxDepth = 512;

  BitLevelAnalyzer() { nodes.push_back(LoadNode()); }

  unsigned leaf(Value v, unsigned bit) {
    auto key = std::make_pair(v, bit);
    auto it = leafIndex.find(key);
    if (it != leafIndex.end())
      return it->second;
    LoadNode n;
    n.kind = LoadNode::Leaf;
    n.v = v;
    n.bit = bit;
    nodes.push_back(n);
    return leafIndex[key] = nodes.size() - 1;
  }
  unsigned mux(Value c, unsigned t, unsigned f) {
    auto key = std::make_tuple(c.getAsOpaquePointer(), t, f);
    auto it = muxIndex.find(key);
    if (it != muxIndex.end())
      return it->second;
    LoadNode n;
    n.kind = LoadNode::Mux;
    n.v = c;
    n.t = t;
    n.f = f;
    nodes.push_back(n);
    return muxIndex[key] = nodes.size() - 1;
  }
  const LoadNode &node(unsigned i) const { return nodes[i]; }

  // (enable, load) of bit `bit` of register `q` whose next is `next`.
  BitRes analyzeBit(Value q, Value next, unsigned bit) {
    memo.clear();
    return walk(q, bit, next, bit, 0);
  }

  // Materialize the value of a list of load nodes (LSB first) as one
  // integer, merging runs of consecutive leaf bits into a single slice.
  Value materializeBits(OpBuilder &b, Location loc, ArrayRef<unsigned> loads) {
    SmallVector<Value, 8> pieces; // LSB first
    for (size_t i = 0; i < loads.size();) {
      const LoadNode &n = nodes[loads[i]];
      if (n.kind == LoadNode::Leaf) {
        size_t j = i + 1;
        while (j < loads.size()) {
          const LoadNode &m = nodes[loads[j]];
          if (m.kind != LoadNode::Leaf || m.v != n.v ||
              m.bit != n.bit + (j - i))
            break;
          ++j;
        }
        pieces.push_back(sliceBits(b, loc, n.v, n.bit, j - i));
        i = j;
        continue;
      }
      pieces.push_back(materializeNode(b, loc, loads[i]));
      ++i;
    }
    if (pieces.size() == 1)
      return pieces[0];
    std::reverse(pieces.begin(), pieces.end()); // comb.concat is MSB first
    return comb::ConcatOp::create(b, loc, pieces);
  }

  // The enable Value of a DNF, cached per DNF so equal enables are the
  // same Value (and canonicalizeEnable then keys them identically).
  Value enableValue(OpBuilder &b, Location loc, const Dnf &d,
                    llvm::DenseMap<Value, Value> &invertedCache) {
    for (auto &[k, v] : enableCache)
      if (k == d)
        return v;
    auto litValue = [&](BitLit l) -> Value {
      if (!l.neg)
        return l.v;
      auto it = invertedCache.find(l.v);
      if (it != invertedCache.end())
        return it->second;
      Value t = hw::ConstantOp::create(b, loc, APInt(1, 1));
      Value x = comb::XorOp::create(b, loc, l.v, t);
      return invertedCache[l.v] = x;
    };
    SmallVector<Value, 4> cubeVals;
    for (const Cube &c : d.cubes) {
      SmallVector<Value, 4> lits;
      for (const BitLit &l : c)
        lits.push_back(litValue(l));
      cubeVals.push_back(lits.size() == 1
                             ? lits[0]
                             : comb::AndOp::create(b, loc, lits, false));
    }
    Value r = cubeVals.size() == 1 ? cubeVals[0]
                                   : comb::OrOp::create(b, loc, cubeVals, false);
    enableCache.emplace_back(d, r);
    return r;
  }

  // Select don't-cares (`select-dont-care=true`). A gated part's load is
  // only observed on cycles its enable is 1, so a mux select `c` in the
  // load may be replaced by any `g` that equals `c` whenever the enable is
  // 1. For `c = and(f1..fn)` take g = and(S), S a subset of the factors:
  // g >= c always, and g = c on every enable cube that either implies c or
  // refutes some factor in S (dually for `c = or(..)`). The typical case is
  // a counter's synchronous clear `mux(start & req_valid & ..., 0, q+1)`
  // whose increment cubes all imply `state != IDLE`: under the enable,
  // `start` reduces to `state == IDLE`, and the late request terms leave
  // the D input (hpdcache_cmo's cmoh_set_q: 47 levels vs a reference's 11).
  // A select every cube decides the same way folds to that leg.
  unsigned simplifyLoad(unsigned idx, ArrayRef<CombFacts> cubes, OpBuilder &b,
                        Location loc, llvm::DenseMap<unsigned, unsigned> &memo,
                        unsigned &numRewritten) {
    if (nodes[idx].kind != LoadNode::Mux)
      return idx;
    if (auto it = memo.find(idx); it != memo.end())
      return it->second;
    LoadNode n = nodes[idx]; // copy: mux() below may grow `nodes`
    unsigned t = simplifyLoad(n.t, cubes, b, loc, memo, numRewritten);
    unsigned f = simplifyLoad(n.f, cubes, b, loc, memo, numRewritten);
    Value c = n.v;
    // Every cube decides c the same way: the other leg is never loaded.
    std::optional<bool> common;
    bool allDecided = true;
    for (const CombFacts &cube : cubes) {
      auto d = cube.lookup(c);
      if (!d || (common && *common != *d)) {
        allDecided = false;
        break;
      }
      common = d;
    }
    unsigned r;
    if (allDecided && common) {
      r = *common ? t : f;
      ++numRewritten;
    } else {
      Value g = reduceSelect(c, cubes, b, loc);
      if (g != c)
        ++numRewritten;
      r = (t == f) ? t : mux(g, t, f);
    }
    return memo[idx] = r;
  }

private:
  template <typename OpTy>
  static void flattenGate(Value v, SmallVectorImpl<Value> &leaves,
                          unsigned depth = 0) {
    if (auto op = v.getDefiningOp<OpTy>(); op && depth < 4) {
      for (Value x : op.getInputs())
        flattenGate<OpTy>(x, leaves, depth + 1);
      return;
    }
    leaves.push_back(v);
  }

  // The smallest-found factor subset of an AND/OR select that is exact on
  // every cube (see simplifyLoad); `c` itself when there is none.
  Value reduceSelect(Value c, ArrayRef<CombFacts> cubes, OpBuilder &b,
                     Location loc) {
    bool isAnd = c.getDefiningOp<comb::AndOp>() != nullptr;
    if (!isAnd && !c.getDefiningOp<comb::OrOp>())
      return c;
    SmallVector<Value, 8> factors;
    if (isAnd)
      flattenGate<comb::AndOp>(c, factors);
    else
      flattenGate<comb::OrOp>(c, factors);
    if (factors.size() < 2 || factors.size() > 32)
      return c;
    // An AND select needs, on each cube that does not imply c, a factor in S
    // the cube refutes; an OR select, a factor in S the cube asserts.
    bool killer = !isAnd;
    SmallVector<uint32_t, 8> needs; // per constrained cube: factors that hit
    for (const CombFacts &cube : cubes) {
      auto d = cube.lookup(c);
      if (d && *d == isAnd)
        continue; // and: c = 1 here, so every factor is 1; or: dually.
      uint32_t hits = 0;
      for (auto [i, x] : llvm::enumerate(factors))
        if (auto v = cube.lookup(x); v && *v == killer)
          hits |= 1u << i;
      if (!hits)
        return c;
      needs.push_back(hits);
    }
    // Greedy hitting set over the constrained cubes.
    uint32_t chosen = 0;
    while (true) {
      uint32_t open = 0;
      SmallVector<unsigned, 32> count(factors.size(), 0);
      for (uint32_t h : needs)
        if (!(h & chosen))
          for (unsigned i = 0; i < factors.size(); ++i)
            if (h & (1u << i))
              ++count[i], open = 1;
      if (!open)
        break;
      unsigned best = std::max_element(count.begin(), count.end()) - count.begin();
      chosen |= 1u << best;
    }
    if (!chosen)
      return c; // no constrained cube: every cube implies c (handled above)
    SmallVector<Value, 8> kept;
    for (unsigned i = 0; i < factors.size(); ++i)
      if (chosen & (1u << i))
        kept.push_back(factors[i]);
    if (kept.size() == factors.size())
      return c;
    if (kept.size() == 1)
      return kept[0];
    if (isAnd)
      return comb::AndOp::create(b, loc, kept, false);
    return comb::OrOp::create(b, loc, kept, false);
  }

  BitRes walk(Value q, unsigned bit, Value v, unsigned vbit, unsigned depth) {
    auto [root, rb] = bitProvenance(v, vbit);
    auto key = std::make_pair(root, rb);
    auto it = memo.find(key);
    if (it != memo.end())
      return it->second;
    BitRes res;
    auto m = root.getDefiningOp<comb::MuxOp>();
    if (root == q && rb == bit) {
      res.en = Dnf::falseValue(); // hold
      res.load = 0;
    } else if (m && depth < kMaxMuxDepth && isa<IntegerType>(m.getType())) {
      BitRes t = walk(q, bit, m.getTrueValue(), rb, depth + 1);
      BitRes f = walk(q, bit, m.getFalseValue(), rb, depth + 1);
      if (t == f) {
        // Pass-through link: both legs carry this bit unchanged.
        res = t;
      } else {
        Value c = m.getCond();
        res.en = dnfOr(dnfAnd({c, false}, t.en), dnfAnd({c, true}, f.en));
        if (res.en.isTrue()) {
          res.en = Dnf::trueValue();
          res.load = leaf(root, rb);
        } else if (t.load == 0) {
          res.load = f.load;
        } else if (f.load == 0) {
          res.load = t.load;
        } else {
          res.load = mux(c, t.load, f.load);
        }
      }
    } else {
      res.en = Dnf::trueValue();
      res.load = leaf(root, rb);
    }
    memo[key] = res;
    return res;
  }

  Value materializeNode(OpBuilder &b, Location loc, unsigned idx) {
    auto it = nodeValue.find(idx);
    if (it != nodeValue.end())
      return it->second;
    const LoadNode &n = nodes[idx];
    Value r;
    if (n.kind == LoadNode::Leaf) {
      r = sliceBits(b, loc, n.v, n.bit, 1);
    } else {
      Value t = materializeNode(b, loc, n.t);
      Value f = materializeNode(b, loc, n.f);
      r = comb::MuxOp::create(b, loc, n.v, t, f);
    }
    nodeValue[idx] = r;
    return r;
  }

  std::vector<LoadNode> nodes;
  llvm::DenseMap<std::pair<Value, unsigned>, unsigned> leafIndex;
  std::map<std::tuple<const void *, unsigned, unsigned>, unsigned> muxIndex;
  llvm::DenseMap<std::pair<Value, unsigned>, BitRes> memo;
  llvm::DenseMap<unsigned, Value> nodeValue;
  SmallVector<std::pair<Dnf, Value>, 8> enableCache;
};

// One same-enable slice of a register under bit-level analysis.
struct BitPart {
  Dnf en;
  SmallVector<unsigned, 8> bits;  // register bit indices, ascending
  SmallVector<unsigned, 8> loads; // load node per bit, same order
  Value clock;                    // group identity: (clock, groupIdx)
  unsigned groupIdx = 0;
  bool gated = false;             // decided once group widths are known
  Value next;                     // materialized when gated
};

// A register the bit-level analysis wants to split.
struct BitPlan {
  seq::FirRegOp firreg;
  SmallVector<BitPart, 4> parts;
  SmallVector<unsigned, 8> ungated; // bits that stay on the plain clock
  bool hasReset = false;
  APInt resetBits;
  std::optional<APInt> preset;
};

struct ClockGateInferencePass
    : public PassWrapper<ClockGateInferencePass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ClockGateInferencePass)

  ClockGateInferencePass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  ClockGateInferencePass(const ClockGateInferencePass &other)
      : PassWrapper(other) {
    enableClockGating = other.enableClockGating.getValue();
    minGroupWidth = other.minGroupWidth.getValue();
    libertyPath = other.libertyPath.getValue();
    dontTouch = other.dontTouch;
    bitLevelEnables = other.bitLevelEnables.getValue();
    bitLevelMaxWidth = other.bitLevelMaxWidth.getValue();
    verifyWithMux = other.verifyWithMux.getValue();
    selectDontCare = other.selectDontCare.getValue();
  }

  StringRef getArgument() const final { return "clock-gate-inference"; }
  StringRef getDescription() const final {
    return "Convert shared-enable seq.firreg groups into a gated-clock DFF "
           "with no hold-mux, using a real integrated clock-gating cell";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<bool> enableClockGating{
      *this, "enable-clock-gating",
      llvm::cl::desc("Enable clock-gating inference -- ON by default (see "
                      "this pass's description)"),
      llvm::cl::init(true)};
  Option<unsigned> minGroupWidth{
      *this, "min-group-width",
      llvm::cl::desc("Skip groups whose total bit width is below this "
                      "(area-breakeven-derived default: 2)"),
      llvm::cl::init(2)};
  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Comma-separated list of liberty file(s) to "
                      "search for the cheapest real posedge-precontrol "
                      "integrated clock-gating cell (required when "
                      "clock-gating is enabled) -- comma, not space, since "
                      "MLIR's own pass-options parser tokenizes on "
                      "whitespace")};
  // Design-management-layer directive surface, milestone 7: a second real
  // enforcement point (see mapper/lib/RegisterMapper/
  // NarrowDeadRegisterBitsPass.cpp's own "dont-touch" option, milestone 6, for
  // the precedent this follows). Naming a firreg here routes it through this
  // pass's OWN pre-existing, already-correct "leave ungated" bail-out (the same
  // `continue` every non-hold-mux-shaped or aggregate-typed firreg already
  // takes below) -- no new code path, so this can only ever leave MORE firregs
  // ungated than default, never gate one incorrectly. Milestone 10: each entry
  // is EITHER a bare "<firreg_name>" (matches in every module, milestone 7's
  // original behavior, fully preserved) OR a qualified
  // "<module_name>:<firreg_name>" (matches only within that module) -- needed
  // because seq.firreg names are only unique WITHIN a module, not globally (on
  // ibex, `rdata_q` appears in many different modules).
  ListOption<std::string> dontTouch{
      *this, "dont-touch",
      llvm::cl::desc("Comma-separated seq.firreg names to exclude from "
                      "clock-gating (leaves them as an ordinary ungated "
                      "hold-mux firreg) -- each entry is either a bare "
                      "name (matches in every module) or a qualified "
                      "<module_name>:<firreg_name> (matches only in that "
                      "module) -- empty by default, identical behavior to "
                      "not having this option")};
  Option<bool> bitLevelEnables{
      *this, "bit-level-enables",
      llvm::cl::desc("Also resolve per-BIT enables through concat/extract/"
                      "mux chains (the entry-array idiom the top-level "
                      "hold-mux shape misses), splitting a register into "
                      "same-enable sub-registers. OFF by default."),
      llvm::cl::init(false)};
  Option<unsigned> bitLevelMaxWidth{
      *this, "bit-level-max-width",
      llvm::cl::desc("Registers wider than this are not analysed per bit"),
      llvm::cl::init(8192)};
  // Verification mode: every accepted group is rebuilt on the PLAIN clock
  // with `next = mux(enable, load, q)` instead of a gated clock. The
  // result must be sequentially equivalent to the input with no clock-
  // gating cell in the picture, so a Yosys miter checks the enable / load
  // resolution and the register splitting exactly; the ICG step itself is
  // the one proven on the synthetic equiv_induct model (see Passes.td).
  Option<bool> verifyWithMux{
      *this, "verify-with-mux",
      llvm::cl::desc("Rebuild gated groups as hold-muxes on the plain clock "
                      "(miter-only mode, never for synthesis)"),
      llvm::cl::init(false)};

  Option<bool> selectDontCare{
      *this, "select-dont-care",
      llvm::cl::desc("Bit-level parts: rewrite the load's mux selects under "
                      "the part's enable (see simplifyLoad). OFF by default."),
      llvm::cl::init(false)};

  hw::HWModuleExternOp getOrCreateIcgExternDecl(ModuleOp topModule,
                                                 const ICGCellPattern &cell) {
    if (icgExternDecl)
      return icgExternDecl;

    // A pre-existing RTL clock gate elsewhere in the design (e.g. a
    // technology-specific prim_clock_gating wrapper) may already have
    // externalized an `hw.module.extern` under THIS SAME cell's name (if the
    // liberty-driven search happened to pick the very cell that RTL already
    // uses) -- reuse that declaration instead of creating a colliding
    // duplicate. If the search picked a genuinely different cell (common: the
    // RTL wrapper names one ICG cell, while the liberty-driven search picks a
    // cheaper equivalent), there is no pre-existing decl to find and a fresh
    // one is created below -- both cells coexisting in the same design is fine,
    // they're independent, functionally compatible instances.
    if (auto existing = mlir::SymbolTable::lookupSymbolIn(
            topModule, StringAttr::get(topModule.getContext(), cell.name))) {
      icgExternDecl = cast<hw::HWModuleExternOp>(existing);
      return icgExternDecl;
    }

    MLIRContext *ctx = topModule.getContext();
    Type i1 = IntegerType::get(ctx, 1);
    SmallVector<hw::PortInfo, 4> ports;
    auto addInput = [&](StringRef name, unsigned argNum) {
      hw::PortInfo port;
      port.name = StringAttr::get(ctx, name);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Input;
      port.argNum = argNum;
      ports.push_back(port);
    };
    // Pin order matches this cell's own pin_name_map(CK, E, SE, ECK).
    addInput(cell.clockPin, 0);
    addInput(cell.enablePin, 1);
    addInput(cell.scanEnablePin, 2);
    hw::PortInfo outPort;
    outPort.name = StringAttr::get(ctx, cell.outputPin);
    outPort.type = i1;
    outPort.dir = hw::ModulePort::Direction::Output;
    outPort.argNum = 0;
    ports.push_back(outPort);

    OpBuilder builder = OpBuilder::atBlockBegin(topModule.getBody());
    icgExternDecl = hw::HWModuleExternOp::create(
        builder, topModule.getLoc(), StringAttr::get(ctx, cell.name), ports);
    return icgExternDecl;
  }

  // Processes one hw.module; returns (numFirregsGated, numGroupsGated).
  std::pair<size_t, size_t> processModule(hw::HWModuleOp module,
                                           ModuleOp topModule,
                                           const ICGCellPattern &icgCell) {
    // Outer key = clock (exact SSA identity -- two firregs never share a
    // physical clock net unless they're driven by the literal same
    // `!seq.clock` value, so no widening is useful here). Inner: a small
    // per-clock list of groups, matched by EnableKey rather than a map,
    // since the per-clock candidate counts here are small enough that a
    // linear scan is simpler and just as fast as building a hasher for
    // EnableKey's variable-length operand list.
    llvm::MapVector<Value, SmallVector<EnableGroup, 4>> groupsByClock;
    llvm::DenseMap<Value, Value> invertedCache;
    /// Old firreg data -> its rebuilt replacement, so a member whose next
    /// value is another gated register still resolves after that register is
    /// erased. See the lookup at its use below.
    llvm::DenseMap<Value, Value> rebuilt;
    BitLevelAnalyzer bitLevel;
    SmallVector<BitPlan, 8> plans;

    // Index, never a pointer: `groupsByClock` is a MapVector whose value
    // type is a SmallVector, so inserting a later clock reallocates the
    // MapVector's storage and moves every group vector. A raw
    // `EnableGroup *` taken here would dangle, and the observed symptom is
    // members ending up attached to the wrong group -- a firreg getting
    // another register's replacement value, of another width.
    auto findOrCreateGroup = [&](Value clock, Value enable) -> unsigned {
      EnableKey key = canonicalizeEnable(enable);
      SmallVector<EnableGroup, 4> &clockGroups = groupsByClock[clock];
      for (auto [i, g] : llvm::enumerate(clockGroups))
        if (g.key == key)
          return i;
      clockGroups.push_back(EnableGroup{key, enable, {}, {}, Value()});
      return clockGroups.size() - 1;
    };

    // Bit-level path. Adds a BitPlan (and returns true) when at least one
    // bit has a non-trivial enable; the group widths decide below whether
    // any part is actually gated.
    auto analyzeBitLevel = [&](seq::FirRegOp firreg, unsigned width) -> bool {
      if (width > bitLevelMaxWidth || firreg.getInnerSymAttr())
        return false;
      BitPlan plan;
      plan.firreg = firreg;
      if (firreg.getReset()) {
        auto rv = firreg.getResetValue().getDefiningOp<hw::ConstantOp>();
        if (!rv)
          return false; // register-legalize leaves async+constant; be safe.
        plan.hasReset = true;
        plan.resetBits = rv.getValue();
      }
      if (auto preset = firreg.getPresetAttr())
        plan.preset = preset.getValue();
      Value q = firreg.getData(), next = firreg.getNext();
      for (unsigned b = 0; b < width; ++b) {
        BitLevelAnalyzer::BitRes res = bitLevel.analyzeBit(q, next, b);
        if (res.en.isTrue() || res.en.isFalse()) {
          plan.ungated.push_back(b);
          continue;
        }
        BitPart *part = nullptr;
        for (BitPart &p : plan.parts)
          if (p.en == res.en) {
            part = &p;
            break;
          }
        if (!part) {
          plan.parts.push_back(BitPart());
          part = &plan.parts.back();
          part->en = res.en;
        }
        part->bits.push_back(b);
        part->loads.push_back(res.load);
      }
      if (plan.parts.empty())
        return false;
      OpBuilder builder(firreg);
      unsigned planIdx = plans.size();
      for (auto [pi, part] : llvm::enumerate(plan.parts)) {
        Value enable =
            bitLevel.enableValue(builder, firreg.getLoc(), part.en, invertedCache);
        part.clock = firreg.getClk();
        part.groupIdx = findOrCreateGroup(part.clock, enable);
        groupsByClock[part.clock][part.groupIdx].bitMembers.push_back(
            {planIdx, (unsigned)pi});
      }
      plans.push_back(std::move(plan));
      return true;
    };

    for (auto firreg : module.getBodyBlock()->getOps<seq::FirRegOp>()) {
      // Directive surface, milestone 7 (see dontTouch's own option comment
      // above): route a named firreg through the SAME "left ungated" outcome
      // every ineligible firreg below already takes.
      if (!dontTouch.empty()) {
        StringRef name = firreg.getName();
        if (!name.empty()) {
          StringRef moduleName = module.getModuleName();
          bool matched = false;
          for (StringRef entry : dontTouch)
            if (dontTouchEntryMatches(entry, moduleName, name)) {
              matched = true;
              break;
            }
          if (matched)
            continue;
        }
      }
      auto intType = dyn_cast<IntegerType>(firreg.getNext().getType());
      if (!intType)
        continue; // aggregate-typed firreg: left ungated, a coverage gap.

      auto mux = firreg.getNext().getDefiningOp<comb::MuxOp>();
      Value data = firreg.getData();
      Value enable, newValue;
      if (mux) {
        Value cond = mux.getCond();
        Value trueVal = mux.getTrueValue();
        Value falseVal = mux.getFalseValue();
        if (falseVal == data && trueVal != data) {
          enable = cond;
          newValue = trueVal;
        } else if (trueVal == data && falseVal != data) {
          newValue = falseVal;
          auto it = invertedCache.find(cond);
          if (it != invertedCache.end()) {
            enable = it->second;
          } else {
            OpBuilder builder(mux);
            Value trueConst =
                hw::ConstantOp::create(builder, mux.getLoc(), APInt(1, 1));
            enable =
                comb::XorOp::create(builder, mux.getLoc(), cond, trueConst);
            invertedCache[cond] = enable;
          }
        }
      }
      // Bit-level first: where it applies it subsumes the whole-register
      // shape (one part covering every bit materializes to the same load
      // value and the same enable), and where the whole-register shape is
      // an outer hold around per-entry updates (`mux(flush, q, chain)`) it
      // finds the per-entry enables the outer gate alone would leave as
      // hold-muxes. Falls back to the whole-register path for the registers
      // it declines (inner_sym, non-constant reset, over the width cap).
      if (bitLevelEnables && analyzeBitLevel(firreg, intType.getWidth()))
        continue;
      if (!enable)
        continue; // not a self-referencing hold-mux at the register's width.

      // Defensive: a member's replacement value must have the firreg's own
      // type, since the new firreg is built from it and then
      // replaceAllUsesWith'd over the old one's uses. A mismatch silently
      // rewrites unrelated logic to the wrong width -- observed on OpenC910
      // as `comb.mux(i1, i8, i2) -> i8` surviving into the output.
      if (newValue.getType() != firreg.getResult().getType())
        continue;

      unsigned idx = findOrCreateGroup(firreg.getClk(), enable);
      groupsByClock[firreg.getClk()][idx].members.push_back(
          {firreg, newValue, intType.getWidth()});
    }

    // Decide every group on its total width and build its clock gate.
    size_t numGroupsGated = 0, numFirregsGated = 0;
    for (auto &clockEntry : groupsByClock) {
      Value clock = clockEntry.first;
      for (auto &group : clockEntry.second) {
        unsigned totalWidth = 0;
        for (auto &m : group.members)
          totalWidth += m.width;
        for (auto [pi, part] : group.bitMembers)
          totalWidth += plans[pi].parts[part].bits.size();
        if (totalWidth < minGroupWidth)
          continue;

        hw::HWModuleExternOp icgDecl =
            getOrCreateIcgExternDecl(topModule, icgCell);
        OpBuilder builder = OpBuilder::atBlockTerminator(module.getBodyBlock());
        Location loc = module.getLoc();
        Value clockI1 = seq::FromClockOp::create(builder, loc, clock);
        Value tieLo = hw::ConstantOp::create(builder, loc, APInt(1, 0));
        std::string instName = ("clock_gate_" + Twine(numGroupsGated)).str();
        auto icgInst = hw::InstanceOp::create(
            builder, loc, icgDecl, builder.getStringAttr(instName),
            ArrayRef<Value>{clockI1, group.representativeEnable, tieLo});
        group.gatedClock =
            seq::ToClockOp::create(builder, loc, icgInst.getResult(0));
        for (auto [pi, part] : group.bitMembers)
          plans[pi].parts[part].gated = true;
        ++numGroupsGated;
      }
    }

    // Materialize every split register's part values BEFORE any register
    // is rebuilt: a part's load may be a slice of another register's data,
    // and an op in the IR follows that register's replaceAllUsesWith where
    // a Value held here would not.
    size_t numRegistersSplit = 0, numBitsGatedBitLevel = 0;
    unsigned numSelectsRewritten = 0;
    for (BitPlan &plan : plans) {
      bool any = false;
      for (BitPart &part : plan.parts)
        any |= part.gated;
      if (!any)
        continue;
      OpBuilder b(plan.firreg);
      Location loc = plan.firreg.getLoc();
      for (BitPart &part : plan.parts) {
        if (part.gated) {
          if (selectDontCare) {
            SmallVector<CombFacts, 4> cubes;
            for (const Cube &cube : part.en.cubes) {
              cubes.emplace_back();
              for (const BitLit &l : cube)
                cubes.back().add(l.v, !l.neg);
            }
            llvm::DenseMap<unsigned, unsigned> memo;
            for (unsigned &load : part.loads)
              load = bitLevel.simplifyLoad(load, cubes, b, loc, memo,
                                           numSelectsRewritten);
          }
          part.next = bitLevel.materializeBits(b, loc, part.loads);
          numBitsGatedBitLevel += part.bits.size();
        } else {
          plan.ungated.append(part.bits.begin(), part.bits.end());
        }
      }
      llvm::sort(plan.ungated);
      ++numRegistersSplit;
    }

    // Rebuild the whole-register members.
    for (auto &clockEntry : groupsByClock) {
      for (auto &group : clockEntry.second) {
        if (!group.gatedClock)
          continue;
        for (auto &m : group.members) {
          seq::FirRegOp firreg = m.firreg;
          OpBuilder fbuilder(firreg);
          seq::FirRegOp newFirreg;
          // A member's replacement value is frequently ANOTHER gated
          // register's data -- these registers feed each other. Once that
          // other register has been rebuilt and erased below, the Value
          // stored here refers to a dead op: replaceAllUsesWith rewrites uses
          // in the IR, but not handles we are holding. Reading it then yields
          // garbage, which showed up on OpenC910 as a register rebuilt at
          // another register's width (`comb.mux(i1, i8, i2) -> i8`).
          // Resolve through the rebuilt-register map before use.
          if (Value mapped = rebuilt.lookup(m.newValue))
            m.newValue = mapped;
          Value clk = verifyWithMux ? firreg.getClk() : group.gatedClock;
          if (firreg.getReset()) {
            newFirreg = seq::FirRegOp::create(
                fbuilder, firreg.getLoc(), m.newValue, clk,
                firreg.getNameAttr(), firreg.getReset(),
                firreg.getResetValue(), firreg.getInnerSymAttr(),
                firreg.getIsAsync(), firreg.getPresetAttr());
          } else {
            newFirreg = seq::FirRegOp::create(
                fbuilder, firreg.getLoc(), m.newValue, clk,
                firreg.getNameAttr(), firreg.getInnerSymAttr(),
                firreg.getPresetAttr());
          }
          if (verifyWithMux)
            newFirreg.getNextMutable().assign(comb::MuxOp::create(
                fbuilder, firreg.getLoc(), group.representativeEnable,
                m.newValue, newFirreg.getData()));
          rebuilt[firreg.getData()] = newFirreg.getData();
          firreg.getData().replaceAllUsesWith(newFirreg.getData());
          firreg.erase();
          ++numFirregsGated;
        }
      }
    }

    // Split the bit-level registers: one sub-register per gated part on
    // its group's gated clock, one for the rest on the plain clock, and
    // the original data re-assembled from their slices.
    for (BitPlan &plan : plans) {
      bool any = false;
      for (BitPart &part : plan.parts)
        any |= part.gated;
      if (!any)
        continue;
      seq::FirRegOp firreg = plan.firreg;
      unsigned width = firreg.getType().getIntOrFloatBitWidth();
      OpBuilder b(firreg);
      Location loc = firreg.getLoc();
      MLIRContext *ctx = firreg.getContext();
      auto gather = [&](const APInt &src, ArrayRef<unsigned> bits) {
        APInt r(bits.size(), 0);
        for (auto [i, bit] : llvm::enumerate(bits))
          if (src[bit])
            r.setBit(i);
        return r;
      };
      auto makeReg = [&](ArrayRef<unsigned> bits, Value next, Value clk,
                         const Twine &suffix) -> Value {
        unsigned w = bits.size();
        if (Value mapped = rebuilt.lookup(next))
          next = mapped;
        IntegerAttr presetAttr;
        if (plan.preset)
          presetAttr = IntegerAttr::get(IntegerType::get(ctx, w),
                                        gather(*plan.preset, bits));
        StringAttr nameAttr = b.getStringAttr(firreg.getName() + suffix);
        if (plan.hasReset) {
          Value rv = hw::ConstantOp::create(b, loc, gather(plan.resetBits, bits));
          return seq::FirRegOp::create(b, loc, next, clk, nameAttr,
                                       firreg.getReset(), rv,
                                       hw::InnerSymAttr(), firreg.getIsAsync(),
                                       presetAttr)
              .getData();
        }
        return seq::FirRegOp::create(b, loc, next, clk, nameAttr,
                                     hw::InnerSymAttr(), presetAttr)
            .getData();
      };
      // bit -> (sub-register data, position in it)
      SmallVector<std::pair<Value, unsigned>> where(width);
      unsigned partNo = 0;
      for (BitPart &part : plan.parts) {
        if (!part.gated)
          continue;
        EnableGroup &g = groupsByClock[part.clock][part.groupIdx];
        Value clk = verifyWithMux ? firreg.getClk() : g.gatedClock;
        Value d = makeReg(part.bits, part.next, clk, "_cg" + Twine(partNo++));
        if (verifyWithMux) {
          auto reg = cast<seq::FirRegOp>(d.getDefiningOp());
          reg.getNextMutable().assign(comb::MuxOp::create(
              b, loc, g.representativeEnable, reg.getNext(), d));
        }
        for (auto [i, bit] : llvm::enumerate(part.bits))
          where[bit] = {d, i};
      }
      if (!plan.ungated.empty()) {
        SmallVector<unsigned, 8> loads;
        for (unsigned bit : plan.ungated)
          loads.push_back(bitLevel.leaf(firreg.getNext(), bit));
        Value next = bitLevel.materializeBits(b, loc, loads);
        Value d = makeReg(plan.ungated, next, firreg.getClk(), "_ug");
        for (auto [i, bit] : llvm::enumerate(plan.ungated))
          where[bit] = {d, i};
      }
      SmallVector<Value, 8> pieces; // LSB first
      for (unsigned bit = 0; bit < width;) {
        auto [d, pos] = where[bit];
        unsigned run = 1;
        while (bit + run < width && where[bit + run].first == d &&
               where[bit + run].second == pos + run)
          ++run;
        pieces.push_back(sliceBits(b, loc, d, pos, run));
        bit += run;
      }
      Value reassembled = pieces.front();
      if (pieces.size() > 1) {
        std::reverse(pieces.begin(), pieces.end());
        reassembled = comb::ConcatOp::create(b, loc, pieces);
      }
      rebuilt[firreg.getData()] = reassembled;
      firreg.getData().replaceAllUsesWith(reassembled);
      firreg.erase();
      ++numFirregsGated;
    }
    if (bitLevelEnables && numRegistersSplit)
      llvm::errs() << "[clock-gate-inference] " << module.getModuleName()
                   << ": bit-level split " << numRegistersSplit
                   << " register(s), " << numBitsGatedBitLevel
                   << " bit(s) gated"
                   << (selectDontCare ? (", " + Twine(numSelectsRewritten) +
                                         " load select(s) rewritten").str()
                                      : std::string())
                   << "\n";
    return {numFirregsGated, numGroupsGated};
  }

  void runOnOperation() override {
    ModuleOp topModule = getOperation();
    if (!enableClockGating)
      return;
    icgExternDecl = {};

    if (libertyPath.empty()) {
      topModule.emitError("clock-gate-inference requires --liberty= when "
                           "--enable-clock-gating=true (the default)");
      return signalPassFailure();
    }

    // Search every given liberty file for the cheapest real ICG cell -- unlike
    // RegisterMapperPass/StructuralMapperPass (which only ever see the one
    // liberty file the rest of the pipeline passes them, since that's where all
    // FF/combinational cells they need live), the cheapest real ICG cell of a
    // PDK may live in a DIFFERENT liberty file than the default one, so this
    // option accepts a comma-separated list -- kept alive as a vector (not
    // discarded after picking the winner) so the winning ICGCellPattern's
    // pointer, taken from whichever library owns it, stays valid for the rest
    // of this function.
    SmallVector<StringRef, 4> libPaths;
    StringRef(libertyPath).split(libPaths, ',', /*MaxSplit=*/-1,
                                  /*KeepEmpty=*/false);
    std::vector<LibertyLibrary> libs;
    libs.reserve(libPaths.size());
    for (StringRef path : libPaths) {
      auto loaded = LibertyLibrary::loadFromFile(path);
      if (!loaded) {
        topModule.emitError("clock-gate-inference: failed to load liberty "
                             "file ")
            << path;
        return signalPassFailure();
      }
      libs.push_back(std::move(*loaded));
    }

    const ICGCellPattern *chosen = nullptr;
    for (const LibertyLibrary &lib : libs)
      if (const ICGCellPattern *icg = lib.lookupICGCell())
        if (!chosen || icg->area < chosen->area)
          chosen = icg;

    if (!chosen) {
      llvm::errs() << "[clock-gate-inference] no real posedge-precontrol "
                       "integrated clock-gating cell found in the given "
                       "liberty file(s); skipping (0 firregs gated)\n";
      return;
    }
    llvm::errs() << "[clock-gate-inference] selected " << chosen->name
                 << " (area=" << chosen->area << ") as the clock-gating "
                 << "cell\n";

    size_t totalFirregsGated = 0, totalGroupsGated = 0;
    for (auto hwModule : topModule.getOps<hw::HWModuleOp>()) {
      auto [numFirregs, numGroups] =
          processModule(hwModule, topModule, *chosen);
      totalFirregsGated += numFirregs;
      totalGroupsGated += numGroups;
    }

    llvm::errs() << "[clock-gate-inference] gated " << totalFirregsGated
                 << " firreg(s) into " << totalGroupsGated
                 << " shared clock-gate cell(s)\n";
  }

private:
  hw::HWModuleExternOp icgExternDecl;
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createClockGateInferencePass() {
  return std::make_unique<ClockGateInferencePass>();
}

} // namespace circt_abc
