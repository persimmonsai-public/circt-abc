//===- MapCompressorCellsPass.cpp --------------------------------*- C++ -*-===//
//
// `--map-compressor-cells`: lower every multiply's carry-save compressor
// tree to explicit two-output full/half-adder CELL instances (FA/HA)
// before ABC ever sees it, the way map-adder-chains does for a ripple add.
//
// Why: once a multiplier is in the AIG, every knob on the lowering --
// Booth, timing-aware compressors, reduce-delay, the adder architectures --
// lands within about a percent, because `&deepsyn`/`dc2` re-derive the tree
// from the AIG regardless of how it was written and `&nf` can never emit a
// two-output cell (every Mio_CollectRoots* skips pTwin gates). A full adder
// mapped from the AIG costs two XNOR2s plus a majority gate, which is
// typically larger than the library's own full-adder cell, and the 3-input
// XOR of the sum is not something the mapper's 6-input cuts recover once
// the tree has been restructured. Emitting the cells here takes the tree
// out of ABC's hands: cell instances are boundaries for
// outline-residual-logic, so ABC maps only the partial-product selection
// and the final adder.
//
// Mechanism: run CIRCT's own comb -> datapath conversion (comb.mul becomes
// datapath.partial_product + datapath.compress + comb.add) and its
// datapath -> comb lowering with `lower-compress=false`, so the Booth
// partial products are ordinary comb logic while every `datapath.compress`
// survives; then replace each compress with a Dadda reduction built from
// FA/HA instances (constant-zero bits of the shifted rows are dropped
// at build time). The final two-row add stays a comb.add for set-adder-arch
// / CombToSynth, exactly as before. Function-preserving by construction
// (each adder is a re-encoding of three bits into two of equal weight sum);
// gated like every netlist-changing pass here.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "LibertyLibrary.h"
#include "circt/Conversion/CombToDatapath.h"
#include "circt/Conversion/DatapathToComb.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/Datapath/DatapathDialect.h"
#include "circt/Dialect/Datapath/DatapathOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Transforms/Passes.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Where does bit `bit` of `v` come from, through concat/extract/replicate?
static std::pair<Value, unsigned> bitSource(Value v, unsigned bit,
                                            unsigned fuel = 64) {
  if (!fuel)
    return {v, bit};
  if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
    unsigned pos = v.getType().getIntOrFloatBitWidth();
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      if (bit >= pos && bit < pos + ow)
        return bitSource(operand, bit - pos, fuel - 1);
    }
    return {v, bit};
  }
  if (auto ex = v.getDefiningOp<comb::ExtractOp>())
    return bitSource(ex.getInput(), bit + ex.getLowBit(), fuel - 1);
  if (auto rep = v.getDefiningOp<comb::ReplicateOp>()) {
    unsigned iw = rep.getInput().getType().getIntOrFloatBitWidth();
    return bitSource(rep.getInput(), bit % iw, fuel - 1);
  }
  return {v, bit};
}


// Upper bound of the unsigned value of `row`: every bit that is not a
// constant zero counts as one (constants count as themselves).
static APInt rowUpperBound(Value row, unsigned bits) {
  unsigned w = row.getType().getIntOrFloatBitWidth();
  APInt ub(bits, 0);
  for (unsigned j = 0; j < w; ++j) {
    auto [src, sb] = bitSource(row, j);
    if (auto k = src.getDefiningOp<hw::ConstantOp>())
      if (!k.getValue()[sb])
        continue;
    ub += APInt::getOneBitSet(bits, j);
  }
  return ub;
}

// `merge-adds`: add(other, zext(add(r0, r1))), where r0/r1 are the two rows
// of one compress that feed nothing else, becomes ONE compress over `other`
// and the original rows plus a single carry-propagate add. Exact when the
// inner tree's inputs cannot sum to 2^Win or more (the inner add then never
// wraps, so its value is the plain integer sum of the rows). Returns the
// number of merges. This removes the intermediate carry-propagate add.
static unsigned mergeAddsIntoCompress(ModuleOp top) {
  unsigned merged = 0;
  for (bool changed = true; changed;) {
    changed = false;
    SmallVector<comb::AddOp> outers;
    for (auto hwmod : top.getOps<hw::HWModuleOp>())
      hwmod.walk([&](comb::AddOp a) {
        if (a.getNumOperands() == 2)
          outers.push_back(a);
      });
    for (comb::AddOp outer : outers) {
      unsigned W = outer.getType().getIntOrFloatBitWidth();
      for (unsigned idx = 0; idx < 2 && !changed; ++idx) {
        Value v = outer.getOperand(idx), other = outer.getOperand(1 - idx);
        if (!v.hasOneUse())
          continue;
        Value innerV = v;
        Operation *ext = nullptr;
        if (auto cc = v.getDefiningOp<comb::ConcatOp>()) {
          auto ops = cc.getInputs();
          bool zext = ops.size() >= 2;
          for (unsigned i = 0; zext && i + 1 < ops.size(); ++i) {
            auto k = ops[i].getDefiningOp<hw::ConstantOp>();
            zext = k && k.getValue().isZero();
          }
          if (!zext)
            continue;
          innerV = ops.back();
          ext = cc;
          if (!innerV.hasOneUse())
            continue;
        }
        auto inner = innerV.getDefiningOp<comb::AddOp>();
        if (!inner || inner.getNumOperands() != 2)
          continue;
        auto r0 = dyn_cast<OpResult>(inner.getOperand(0));
        auto r1 = dyn_cast<OpResult>(inner.getOperand(1));
        if (!r0 || !r1 || r0.getOwner() != r1.getOwner() ||
            r0.getResultNumber() == r1.getResultNumber())
          continue;
        auto comp = dyn_cast<datapath::CompressOp>(r0.getOwner());
        if (!comp || comp.getNumResults() != 2 || !r0.hasOneUse() ||
            !r1.hasOneUse())
          continue;
        unsigned Win = innerV.getType().getIntOrFloatBitWidth();
        if (Win > W)
          continue;
        unsigned bits = W + 32;
        APInt ub(bits, 0);
        for (Value row : comp.getInputs())
          ub += rowUpperBound(row, bits);
        if (!ub.ult(APInt::getOneBitSet(bits, Win)))
          continue;
        OpBuilder b(outer);
        Location loc = outer.getLoc();
        SmallVector<Value> ins{other};
        for (Value row : comp.getInputs()) {
          if (Win < W) {
            Value z = hw::ConstantOp::create(b, loc, APInt(W - Win, 0));
            row = comb::ConcatOp::create(b, loc, ValueRange{z, row});
          }
          ins.push_back(row);
        }
        auto nc = datapath::CompressOp::create(b, loc, ins, 2);
        auto add = comb::AddOp::create(b, loc, nc.getResults(), true);
        if (auto arch = outer->getAttr("synth.test.arch"))
          add->setAttr("synth.test.arch", arch);
        outer.getResult().replaceAllUsesWith(add.getResult());
        outer.erase();
        if (ext)
          ext->erase();
        inner.erase();
        comp.erase();
        ++merged;
        changed = true;
      }
      if (changed)
        break;
    }
  }
  return merged;
}


// ---------------------------------------------------------------------------
// `flatten-adds`: general, width-safe flattening of an add expression into ONE
// compress tree plus a single carry-propagate add (a merged carry-save
// structure).
//
// An expression is expanded into terms; a term is a leaf value, placed at a bit
// offset, ANDed with a list of gate conditions. Expansion rules (sum of terms
// == value modulo 2^width of the expanded value):
//   add(x, y, ..)          terms of every operand                  (modular)
//   add(r0, r1) of one compress whose two results feed only this add
//                          the compress's own input rows           (modular)
//   concat(0.., X, 0..)    terms of X shifted left by the low zeros; the high
//                          zeros are a zero-extension, which is only sound when
//                          X cannot wrap: the integer sum of the upper bounds of
//                          X's terms must stay below 2^width(X)
//   mux(c, X, 0)           terms of X, each gated by c (c*X == sum c*t_i)
// everything else is a leaf. Every value that is expanded (not a leaf) must have
// all its uses reached by this expansion, otherwise its carry-propagate add would
// survive next to the copies: such a value is demoted to a leaf and the
// expansion restarts. Roots are comb.add ops; the original root is replaced by
// compress(terms -> 2) + add.
struct FlatTerm {
  Value leaf;
  unsigned shift = 0;
  SmallVector<std::pair<Value, bool>, 2> gates; // (condition, inverted)
};
struct FlatPlan {
  SmallVector<FlatTerm> terms;
  APInt ub;
};
struct FlatCtx {
  bool gating = true;
  DenseMap<Value, SmallPtrSet<Operation *, 4>> visits;
  DenseSet<Value> forbidden;
  SmallVector<Value> consumedValues;
  SmallVector<Operation *> consumedOps;
  bool bad = false;
  Value badValue;
};
static constexpr unsigned kFlatBits = 256;

static unsigned useCount(Value v) { return std::distance(v.use_begin(), v.use_end()); }

static FlatPlan flatLeaf(Value v) {
  FlatPlan p;
  p.ub = APInt(kFlatBits, 0);
  auto isZero = [&](Value x) {
    auto k = x.getDefiningOp<hw::ConstantOp>();
    return k && k.getValue().isZero();
  };
  if (isZero(v))
    return p;
  unsigned shift = 0;
  // Peel constant-zero padding off a concat so that gating applies to the
  // payload only, not to its zero bits.
  if (auto cc = v.getDefiningOp<comb::ConcatOp>()) {
    auto ops = cc.getInputs();
    unsigned first = 0, last = ops.size();
    while (first < last && isZero(ops[first]))
      ++first;
    while (last > first + 1 && isZero(ops[last - 1]))
      --last;
    if (first < last && last == first + 1 && !isZero(ops[first]) &&
        (first > 0 || last < ops.size())) {
      for (unsigned i = last; i < ops.size(); ++i)
        shift += ops[i].getType().getIntOrFloatBitWidth();
      v = ops[first];
    }
  }
  FlatTerm t;
  t.leaf = v;
  t.shift = shift;
  p.terms.push_back(t);
  p.ub = rowUpperBound(v, kFlatBits).shl(shift);
  return p;
}

static FlatPlan flatExpand(FlatCtx &c, Value v, Operation *parent);

static FlatPlan flatExpandAdd(FlatCtx &c, ValueRange ops, Operation *parent) {
  FlatPlan out;
  out.ub = APInt(kFlatBits, 0);
  SmallVector<bool> used(ops.size(), false);
  for (unsigned i = 0; i < ops.size(); ++i) {
    auto r0 = dyn_cast<OpResult>(ops[i]);
    if (!r0 || used[i])
      continue;
    auto comp = dyn_cast<datapath::CompressOp>(r0.getOwner());
    if (!comp || comp.getNumResults() != 2 ||
        c.forbidden.count(comp.getResult(0)) ||
        c.forbidden.count(comp.getResult(1)))
      continue;
    int j = -1;
    for (unsigned k = 0; k < ops.size(); ++k)
      if (k != i && !used[k] && ops[k] == comp.getResult(1 - r0.getResultNumber())) {
        j = k;
        break;
      }
    if (j < 0)
      continue;
    used[i] = used[j] = true;
    c.visits[ops[i]].insert(parent);
    c.visits[ops[j]].insert(parent);
    c.consumedValues.push_back(comp.getResult(0));
    c.consumedValues.push_back(comp.getResult(1));
    c.consumedOps.push_back(comp);
    for (Value row : comp.getInputs()) {
      FlatPlan l = flatExpand(c, row, comp);
      out.terms.append(l.terms.begin(), l.terms.end());
      out.ub += l.ub;
    }
  }
  for (unsigned i = 0; i < ops.size(); ++i) {
    if (used[i])
      continue;
    FlatPlan p = flatExpand(c, ops[i], parent);
    out.terms.append(p.terms.begin(), p.terms.end());
    out.ub += p.ub;
  }
  return out;
}

static FlatPlan flatExpand(FlatCtx &c, Value v, Operation *parent) {
  unsigned Wv = v.getType().getIntOrFloatBitWidth();
  Operation *def = v.getDefiningOp();
  if (!def || c.forbidden.count(v) || isa<datapath::CompressOp>(def))
    return flatLeaf(v);
  if (auto add = dyn_cast<comb::AddOp>(def)) {
    c.visits[v].insert(parent);
    c.consumedValues.push_back(v);
    c.consumedOps.push_back(def);
    return flatExpandAdd(c, add.getInputs(), def);
  }
  if (auto cc = dyn_cast<comb::ConcatOp>(def)) {
    auto ops = cc.getInputs();
    unsigned first = 0, last = ops.size();
    auto isZero = [&](Value x) {
      auto k = x.getDefiningOp<hw::ConstantOp>();
      return k && k.getValue().isZero();
    };
    while (first < last && isZero(ops[first]))
      ++first;
    while (last > first + 1 && isZero(ops[last - 1]))
      --last;
    if (last != first + 1 || (first == 0 && last == ops.size()))
      return flatLeaf(v);
    Value x = ops[first];
    if (isZero(x))
      return flatLeaf(v);
    unsigned Wx = x.getType().getIntOrFloatBitWidth();
    unsigned lo = Wv;
    for (unsigned i = 0; i < last; ++i)
      lo -= ops[i].getType().getIntOrFloatBitWidth();
    // Dry-run X so that a rejected zero-extension leaves no side effects.
    FlatCtx trial = c;
    trial.visits[v].insert(parent);
    FlatPlan p = flatExpand(trial, x, def);
    if (first > 0 && !p.ub.ult(APInt::getOneBitSet(kFlatBits, Wx)))
      return flatLeaf(v);
    c = trial;
    c.consumedValues.push_back(v);
    c.consumedOps.push_back(def);
    for (auto &t : p.terms)
      t.shift += lo;
    p.ub = p.ub.shl(lo);
    return p;
  }
  if (auto mx = dyn_cast<comb::MuxOp>(def)) {
    if (!c.gating)
      return flatLeaf(v);
    auto isZero = [&](Value x) {
      auto k = x.getDefiningOp<hw::ConstantOp>();
      return k && k.getValue().isZero();
    };
    Value x;
    bool inv = false;
    if (isZero(mx.getFalseValue()) && !isZero(mx.getTrueValue()))
      x = mx.getTrueValue();
    else if (isZero(mx.getTrueValue()) && !isZero(mx.getFalseValue())) {
      x = mx.getFalseValue();
      inv = true;
    } else
      return flatLeaf(v);
    // Only gate expressions that actually contain an add; gating a plain
    // leaf is no better than the mux that is already there.
    Operation *xd = x.getDefiningOp();
    if (!xd || !(isa<comb::AddOp, comb::ConcatOp>(xd)))
      return flatLeaf(v);
    FlatCtx trial = c;
    trial.visits[v].insert(parent);
    FlatPlan p = flatExpand(trial, x, def);
    if (trial.consumedOps.size() == c.consumedOps.size())
      return flatLeaf(v); // nothing below to merge: keep the mux as a leaf
    c = trial;
    c.consumedValues.push_back(v);
    c.consumedOps.push_back(def);
    for (auto &t : p.terms)
      t.gates.push_back({mx.getCond(), inv});
    return p;
  }
  return flatLeaf(v);
}

static unsigned flattenAddsIntoCompress(ModuleOp top, bool gating,
                                        unsigned maxTerms, unsigned maxInputs) {
  unsigned flattened = 0;
  DenseSet<Operation *> done;
  for (bool changed = true; changed;) {
    changed = false;
    SmallVector<comb::AddOp> roots;
    for (auto hwmod : top.getOps<hw::HWModuleOp>())
      hwmod.walk([&](comb::AddOp a) {
        if (a.getNumOperands() >= 2 && !done.count(a))
          roots.push_back(a);
      });
    for (comb::AddOp root : roots) {
      if (!root->getParentOp())
        continue;
      unsigned W = root.getType().getIntOrFloatBitWidth();
      FlatCtx ctx;
      ctx.gating = gating;
      FlatPlan plan;
      bool ok = false;
      for (unsigned attempt = 0; attempt < 32; ++attempt) {
        ctx.visits.clear();
        ctx.consumedValues.clear();
        ctx.consumedOps.clear();
        plan = flatExpandAdd(ctx, root.getInputs(), root);
        Value bad;
        for (Value x : ctx.consumedValues) {
          for (Operation *u : x.getUsers())
            if (!ctx.visits[x].count(u)) {
              bad = x;
              break;
            }
          if (bad)
            break;
        }
        if (!bad) {
          ok = true;
          break;
        }
        ctx.forbidden.insert(bad);
      }
      if (!ok || ctx.consumedOps.empty() || plan.terms.size() < 2 ||
          plan.terms.size() > maxTerms)
        continue;
      // Something must actually be merged: a consumed add or compress pair.
      if (!llvm::any_of(ctx.consumedOps, [](Operation *o) {
            return isa<comb::AddOp, datapath::CompressOp>(o);
          }))
        continue;
      OpBuilder b(root);
      Location loc = root.getLoc();
      SmallVector<Value> rows;
      DenseMap<Value, Value> notCache;
      for (FlatTerm &t : plan.terms) {
        Value leaf = t.leaf;
        unsigned wl = leaf.getType().getIntOrFloatBitWidth();
        if (t.shift >= W)
          continue; // shifted out entirely
        if (t.shift + wl > W) {
          wl = W - t.shift;
          leaf = comb::ExtractOp::create(b, loc, b.getIntegerType(wl), leaf, 0);
        }
        if (!t.gates.empty()) {
          SmallVector<Value> cs;
          for (auto [cnd, inv] : t.gates) {
            Value cv = cnd;
            if (inv) {
              auto &slot = notCache[cnd];
              if (!slot) {
                Value one = hw::ConstantOp::create(b, loc, APInt(1, 1));
                slot = comb::XorOp::create(b, loc, ValueRange{cnd, one}, true);
              }
              cv = slot;
            }
            cs.push_back(cv);
          }
          Value g = cs.size() == 1 ? cs[0]
                                   : comb::AndOp::create(b, loc, cs, true).getResult();
          Value z = hw::ConstantOp::create(b, loc, APInt(wl, 0));
          leaf = comb::MuxOp::create(b, loc, g, leaf, z);
        }
        SmallVector<Value> parts;
        if (W - t.shift - wl)
          parts.push_back(hw::ConstantOp::create(b, loc, APInt(W - t.shift - wl, 0)));
        parts.push_back(leaf);
        if (t.shift)
          parts.push_back(hw::ConstantOp::create(b, loc, APInt(t.shift, 0)));
        rows.push_back(parts.size() == 1 ? parts[0]
                                         : comb::ConcatOp::create(b, loc, parts).getResult());
      }
      (void)maxInputs;
      Value result;
      if (rows.empty())
        result = hw::ConstantOp::create(b, loc, APInt(W, 0));
      else if (rows.size() == 1)
        result = rows[0];
      else if (rows.size() == 2) {
        auto add = comb::AddOp::create(b, loc, rows, true);
        done.insert(add);
        result = add.getResult();
      } else {
        auto nc = datapath::CompressOp::create(b, loc, rows, 2);
        auto add = comb::AddOp::create(b, loc, nc.getResults(), true);
        done.insert(add);
        result = add.getResult();
      }
      if (auto arch = root->getAttr("synth.test.arch"))
        if (auto *d = result.getDefiningOp())
          d->setAttr("synth.test.arch", arch);
      root.getResult().replaceAllUsesWith(result);
      done.erase(root);
      root.erase();
      // erase consumed ops that are now dead, users before definitions
      for (bool again = true; again;) {
        again = false;
        for (Operation *op : ctx.consumedOps) {
          if (!op || !op->getParentOp() || !op->use_empty())
            continue;
          op->erase();
          again = true;
          for (auto &o : ctx.consumedOps)
            if (o == op)
              o = nullptr;
        }
      }
      ++flattened;
      changed = true;
      break;
    }
  }
  return flattened;
}

struct MapCompressorCellsPass
    : public PassWrapper<MapCompressorCellsPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MapCompressorCellsPass)

  MapCompressorCellsPass() = default;
  MapCompressorCellsPass(const MapCompressorCellsPass &other)
      : PassWrapper(other) {
    enablePass = other.enablePass.getValue();
    libertyPath = other.libertyPath.getValue();
    minWidth = other.minWidth.getValue();
    prefixFinalAdder = other.prefixFinalAdder.getValue();
    mergeAdds = other.mergeAdds.getValue();
    flattenAdds = other.flattenAdds.getValue();
    flattenGated = other.flattenGated.getValue();
    flattenMaxTerms = other.flattenMaxTerms.getValue();
  }

  StringRef getArgument() const final { return "map-compressor-cells"; }
  StringRef getDescription() const final {
    return "Lower multiply compressor trees to full/half-adder cell instances "
           "(Dadda) before ABC, which cannot emit two-output cells";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect,
                    datapath::DatapathDialect>();
  }

  Option<bool> enablePass{
      *this, "enable-compressor-cells",
      llvm::cl::desc("Enable the rewrite (OFF by default)"),
      llvm::cl::init(false)};
  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file holding the full/half-adder cells")};
  Option<unsigned> minWidth{
      *this, "min-width",
      llvm::cl::desc("Only designs holding a comb.mul at least this wide are "
                      "converted (the conversion touches every multiply and "
                      "multi-operand add)"),
      llvm::cl::init(16)};

  Option<bool> flattenAdds{
      *this, "flatten-adds",
      llvm::cl::desc("Flatten whole add expressions (nested adds, zero-"
                      "extensions, shifts, compress row pairs) into one "
                      "compress plus one add; width-safe"),
      llvm::cl::init(false)};
  Option<bool> flattenGated{
      *this, "flatten-gated",
      llvm::cl::desc("With flatten-adds, distribute mux(c, sum, 0) gating "
                      "over the terms of the sum"),
      llvm::cl::init(true)};
  Option<unsigned> flattenMaxTerms{
      *this, "flatten-max-terms",
      llvm::cl::desc("upper bound on the rows of a flattened compress"),
      llvm::cl::init(16)};
  Option<bool> mergeAdds{
      *this, "merge-adds",
      llvm::cl::desc("Fold add(x, zext(add(rows of one compress))) into one "
                      "compress when the inner sum provably cannot wrap"),
      llvm::cl::init(false)};

  // The final carry-propagate adder, built here as a Brent-Kung prefix
  // network of real cells instead of being handed back to ABC as a
  // comb.add. OFF by default (project convention for a new lever).
  //
  // Why it is needed at all: with the tree taken out of ABC's hands, the
  // final add becomes an ISOLATED cone from primary inputs inside its ABC
  // group, and the area-only `&deepsyn` collapses the Brent-Kung structure
  // CombToSynth gave it into a near-ripple, which is smaller. On a 64x64
  // multiplier the worst path through the compressor cells then spends a
  // few tree stages in the adder cells and most of its delay in the final
  // 128-bit adder, so the product cone ends up deeper than the whole cone
  // without this pass: the area saved costs timing unless the final adder
  // keeps a prefix structure.
  Option<bool> prefixFinalAdder{
      *this, "prefix-final-adder",
      llvm::cl::desc("Build each tree's final two-row add as a Brent-Kung "
                      "prefix network of cells rather than a comb.add"),
      llvm::cl::init(false)};

  //--- A small cell emitter with polarity tracking --------------------------
  //
  // A Sig is either a constant (k = 0/1) or a Value that carries the signal
  // itself (neg=false) or its complement (neg=true). Carrying the polarity,
  // instead of inverting eagerly, lets each gate absorb its inputs'
  // inversions into its own truth table; the library's cheapest match is
  // then usually an INVERTING compound cell (AOI21/OAI21/NAND2/NOR2), and a
  // prefix tree built this way alternates AOI and OAI levels on its own --
  // the standard CMOS prefix-adder construction -- with no explicit INVs.
  struct Sig {
    Value v;
    bool neg = false;
    int k = -1; // -1 = not a constant
  };
  const LibertyLibrary *cellLib = nullptr;
  llvm::StringMap<hw::HWModuleExternOp> cellDecls;
  unsigned nPrefixCells = 0;
  // Every op one prefix build creates, so a build that fails part-way can be
  // rolled back completely. Without this a failed build left its gates in
  // the module with no users -- silent dead area in the netlist.
  SmallVector<Operation *> created;
  llvm::DenseMap<Value, Value> invCache; // one inverter per signal, shared

  hw::HWModuleExternOp declFor(ModuleOp top, const CellPattern &cell) {
    if (auto d = cellDecls.lookup(cell.name))
      return d;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == cell.name)
        return cellDecls[cell.name] = ext;
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);
    SmallVector<hw::PortInfo, 5> ports;
    for (auto [idx, pin] : llvm::enumerate(cell.inputPins)) {
      hw::PortInfo p;
      p.name = StringAttr::get(ctx, pin);
      p.type = i1;
      p.dir = hw::ModulePort::Direction::Input;
      p.argNum = idx;
      ports.push_back(p);
    }
    hw::PortInfo o;
    o.name = StringAttr::get(ctx, cell.outputPin);
    o.type = i1;
    o.dir = hw::ModulePort::Direction::Output;
    o.argNum = 0;
    ports.push_back(o);
    OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
    return cellDecls[cell.name] = hw::HWModuleExternOp::create(
               b, top.getLoc(), StringAttr::get(ctx, cell.name), ports);
  }

  // Emit f(in...) -- `f` is a truth table over `in` in the LibertyLibrary
  // convention (bit a = f at the assignment giving input i bit i of a).
  // Returns nullopt only if the library has no cell for the function.
  std::optional<Sig> gateOnce(OpBuilder &b, Location loc, ModuleOp top,
                              TruthTable f, SmallVector<Sig, 3> in) {
    // Absorb input polarity: f'(a) = f(a ^ negmask).
    unsigned n = in.size();
    auto remap = [](TruthTable t, unsigned n, auto fn) {
      TruthTable r = 0;
      for (unsigned a = 0; a < (1u << n); ++a)
        if ((t >> fn(a)) & 1)
          r |= TruthTable(1) << a;
      return r;
    };
    unsigned mask = 0;
    for (unsigned i = 0; i < n; ++i)
      if (in[i].k < 0 && in[i].neg)
        mask |= 1u << i;
    f = remap(f, n, [&](unsigned a) { return a ^ mask; });
    for (auto &s : in)
      s.neg = false;
    // Fold constants and drop inputs f does not depend on, one at a time.
    for (unsigned i = 0; i < in.size();) {
      unsigned m = in.size();
      auto cofactor = [&](unsigned bit) {
        return remap(f, m - 1, [&](unsigned a) {
          unsigned lo = a & ((1u << i) - 1), hi = a >> i;
          return lo | (bit << i) | (hi << (i + 1));
        });
      };
      TruthTable f0 = cofactor(0), f1 = cofactor(1);
      if (in[i].k >= 0 || f0 == f1) {
        f = in[i].k == 1 ? f1 : f0;
        in.erase(in.begin() + i);
        // Restart: folding a constant can remove the function's dependence
        // on an input already examined (G_hi | (P_hi & 0) no longer reads
        // P_hi), and leaving that dead input in makes the lookup miss.
        i = 0;
        continue;
      }
      ++i;
    }
    n = in.size();
    TruthTable full = n == 6 ? ~TruthTable(0) : (TruthTable(1) << (1u << n)) - 1;
    f &= full;
    if (f == 0)
      return Sig{Value(), false, 0};
    if (f == full)
      return Sig{Value(), false, 1};
    if (n == 1)
      return Sig{in[0].v, f == 0x1, -1}; // f is x (0b10) or !x (0b01)
    auto match = cellLib->lookup(n, f, /*inversionIsFree=*/true);
    if (!match) {
      if (getenv("MCC_DEBUG"))
        llvm::errs() << "[mcc-debug] no cell for arity " << n << " tt 0x"
                     << llvm::utohexstr(f) << "\n";
      return std::nullopt;
    }
    auto decl = declFor(top, *match->cell);
    SmallVector<Value, 6> args;
    for (unsigned slot = 0; slot < match->cell->arity(); ++slot)
      args.push_back(in[match->leafForCellInputSlot[slot]].v);
    auto inst = hw::InstanceOp::create(
        b, loc, decl,
        StringAttr::get(top.getContext(),
                        (Twine("cpa_") + Twine(nPrefixCells++)).str()),
        args);
    created.push_back(inst);
    return Sig{inst.getResult(0), match->inverted, -1};
  }

  // A real inverter cell on `v` (cached). Bypasses gateOnce, whose
  // single-input case deliberately emits nothing and just flips polarity.
  std::optional<Value> invOf(OpBuilder &b, Location loc, ModuleOp top,
                             Value v) {
    if (auto hit = invCache.lookup(v))
      return hit;
    auto match = cellLib->lookup(1, /*!x*/ 0x1);
    if (!match || match->inverted)
      return std::nullopt;
    auto inst = hw::InstanceOp::create(
        b, loc, declFor(top, *match->cell),
        StringAttr::get(top.getContext(),
                        (Twine("cpa_") + Twine(nPrefixCells++)).str()),
        SmallVector<Value, 1>{v});
    created.push_back(inst);
    return invCache[v] = inst.getResult(0);
  }

  // gateOnce, and if a mixed-polarity function has no cell -- e.g.
  // !a | (b & !c), which is what a prefix combine sees when its operands
  // come from levels of opposite polarity -- invert the complemented inputs
  // physically and retry with every input in true polarity.
  std::optional<Sig> gate(OpBuilder &b, Location loc, ModuleOp top,
                          TruthTable f, SmallVector<Sig, 3> in) {
    if (auto r = gateOnce(b, loc, top, f, in))
      return r;
    bool any = false;
    for (auto &x : in)
      if (x.k < 0 && x.neg) {
        auto v = invOf(b, loc, top, x.v);
        if (!v)
          return std::nullopt;
        x = Sig{*v, false, -1};
        any = true;
      }
    return any ? gateOnce(b, loc, top, f, in) : std::nullopt;
  }

  // The same function, but asking for the result held in `wantNeg`
  // polarity: query the complement and flip. Used to keep a prefix level's
  // signals in ONE polarity, so the next level's inputs are uniform and it
  // needs no inverters.
  std::optional<Sig> gateWant(OpBuilder &b, Location loc, ModuleOp top,
                              TruthTable f, SmallVector<Sig, 3> in,
                              bool wantNeg) {
    if (!wantNeg)
      return gate(b, loc, top, f, in);
    unsigned n = in.size();
    TruthTable full = (TruthTable(1) << (1u << n)) - 1;
    auto r = gate(b, loc, top, ~f & full, in);
    if (!r)
      return std::nullopt;
    if (r->k >= 0)
      r->k = 1 - r->k;
    else
      r->neg = !r->neg;
    return r;
  }

  // A Sig as a real i1 Value of the TRUE polarity (one INV if it is held
  // complemented; constants become hw.constant).
  std::optional<Value> materialize(OpBuilder &b, Location loc, ModuleOp top,
                                   Sig s) {
    if (s.k >= 0) {
      auto k = hw::ConstantOp::create(b, loc, APInt(1, s.k));
      created.push_back(k);
      return k.getResult();
    }
    if (!s.neg)
      return s.v;
    return invOf(b, loc, top, s.v);
  }

  // Brent-Kung over two W-bit operands given bit by bit (LSB first); the
  // carry-out of the top bit is dropped, as in a W-bit comb.add.
  std::optional<SmallVector<Value>> buildPrefixAdder(OpBuilder &b,
                                                     Location loc,
                                                     ModuleOp top,
                                                     ArrayRef<Sig> x,
                                                     ArrayRef<Sig> y) {
    created.clear();
    invCache.clear();
    auto r = buildPrefixAdderImpl(b, loc, top, x, y);
    if (!r) // roll back everything this attempt emitted
      for (Operation *op : llvm::reverse(created))
        op->erase();
    created.clear();
    return r;
  }

  std::optional<SmallVector<Value>> buildPrefixAdderImpl(OpBuilder &b,
                                                         Location loc,
                                                         ModuleOp top,
                                                         ArrayRef<Sig> x,
                                                         ArrayRef<Sig> y) {
    constexpr TruthTable AND2 = 0x8, XOR2 = 0x6;
    // G_hi | (P_hi & G_lo) over inputs (G_hi, P_hi, G_lo):
    // bit a set iff a0 | (a1 & a2)  ->  a in {1,3,5,6,7}.
    constexpr TruthTable AO21 = 0xEA;
    unsigned W = x.size();
    SmallVector<Sig> g(W), p(W), G(W), P(W);
    SmallVector<unsigned> lo(W);
    for (unsigned i = 0; i < W; ++i) {
      auto gi = gate(b, loc, top, AND2, {x[i], y[i]});
      if (!gi)
        return std::nullopt;
      // p in g's polarity (XNOR where g came out as NAND), so the first
      // prefix level sees uniform inputs.
      auto pi = gateWant(b, loc, top, XOR2, {x[i], y[i]},
                         gi->k < 0 && gi->neg);
      if (!pi)
        return std::nullopt;
      g[i] = G[i] = *gi;
      p[i] = P[i] = *pi;
      lo[i] = i;
    }
    auto combine = [&](unsigned hi, unsigned lw) -> bool {
      auto nG = gate(b, loc, top, AO21, {G[hi], P[hi], G[lw]});
      if (!nG)
        return false;
      unsigned newLo = lo[lw];
      // Once a span reaches bit 0 its P is never read again: such a node is
      // only ever the LOW side of a later combine, and that result reaches
      // bit 0 too. Skipping those AND2s is a large share of the area.
      if (newLo > 0) {
        auto nP = gateWant(b, loc, top, AND2, {P[hi], P[lw]},
                           nG->k < 0 && nG->neg);
        if (!nP)
          return false;
        P[hi] = *nP;
      }
      G[hi] = *nG;
      lo[hi] = newLo;
      return true;
    };
    unsigned top2 = 1;
    while (top2 * 2 <= W)
      top2 *= 2;
    for (unsigned d = 1; d < W; d *= 2)            // up-sweep
      for (unsigned i = 2 * d - 1; i < W; i += 2 * d)
        if (!combine(i, i - d))
          return std::nullopt;
    for (unsigned d = top2 / 2; d >= 1; d /= 2)     // down-sweep
      for (unsigned i = 3 * d - 1; i < W; i += 2 * d)
        if (!combine(i, i - d))
          return std::nullopt;
    SmallVector<Value> sum(W);
    for (unsigned i = 0; i < W; ++i) {
      assert((i == 0 || lo[i - 1] == 0) && "prefix schedule left a gap");
      std::optional<Sig> s =
          i == 0 ? std::optional<Sig>(p[0])
                 : gate(b, loc, top, XOR2, {p[i], G[i - 1]});
      if (!s)
        return std::nullopt;
      auto v = materialize(b, loc, top, *s);
      if (!v)
        return std::nullopt;
      sum[i] = *v;
    }
    return sum;
  }

  struct CellDecl {
    hw::HWModuleExternOp op;
    SmallVector<unsigned, 3> inIdx;
    unsigned sumIdx = 0, carryIdx = 0;
    bool valid = false;
  };

  CellDecl getOrCreateDecl(ModuleOp top, StringRef name,
                           ArrayRef<std::string> inputPins, StringRef sumPin,
                           StringRef carryPin) {
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);
    CellDecl decl;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == name) {
        decl.op = ext;
        break;
      }
    if (!decl.op) {
      SmallVector<hw::PortInfo, 5> ports;
      for (auto [idx, pin] : llvm::enumerate(inputPins)) {
        hw::PortInfo p;
        p.name = StringAttr::get(ctx, pin);
        p.type = i1;
        p.dir = hw::ModulePort::Direction::Input;
        p.argNum = idx;
        ports.push_back(p);
      }
      const StringRef outPins[2] = {carryPin, sumPin};
      for (auto [idx, pin] : llvm::enumerate(outPins)) {
        hw::PortInfo p;
        p.name = StringAttr::get(ctx, pin);
        p.type = i1;
        p.dir = hw::ModulePort::Direction::Output;
        p.argNum = idx;
        ports.push_back(p);
      }
      OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
      decl.op = hw::HWModuleExternOp::create(b, top.getLoc(),
                                             StringAttr::get(ctx, name), ports);
    }
    llvm::StringMap<unsigned> inPos, outPos;
    unsigned nIn = 0, nOut = 0;
    for (const hw::PortInfo &port : decl.op.getPortList()) {
      if (port.dir == hw::ModulePort::Direction::Output)
        outPos[port.getName()] = nOut++;
      else
        inPos[port.getName()] = nIn++;
    }
    for (StringRef pin : inputPins) {
      auto it = inPos.find(pin);
      if (it == inPos.end())
        return decl;
      decl.inIdx.push_back(it->second);
    }
    auto s = outPos.find(sumPin), c = outPos.find(carryPin);
    if (s == outPos.end() || c == outPos.end() || nIn != inputPins.size() ||
        nOut != 2)
      return decl;
    decl.sumIdx = s->second;
    decl.carryIdx = c->second;
    decl.valid = true;
    return decl;
  }

  void runOnOperation() override {
    if (!enablePass.getValue())
      return;
    ModuleOp top = getOperation();
    bool anyMul = false;
    for (auto hwmod : top.getOps<hw::HWModuleOp>())
      hwmod.walk([&](comb::MulOp mul) {
        auto ty = dyn_cast<IntegerType>(mul.getType());
        if (ty && ty.getWidth() >= minWidth.getValue())
          anyMul = true;
      });
    if (!anyMul)
      return;
    if (libertyPath.getValue().empty()) {
      top.emitError("map-compressor-cells: liberty= is required");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      top.emitError("map-compressor-cells: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    const FullAdderCellPattern *fa = lib->lookupFullAdder();
    const HalfAdderCellPattern *ha = lib->lookupHalfAdder();
    if (!fa)
      return;
    CellDecl faDecl = getOrCreateDecl(
        top, fa->name,
        ArrayRef<std::string>(fa->inputPins.begin(), fa->inputPins.end()),
        fa->sumPin, fa->carryPin);
    if (!faDecl.valid) {
      top.emitWarning("map-compressor-cells: existing declaration of ")
          << fa->name << " does not match the liberty pin names; skipping";
      return;
    }
    std::optional<CellDecl> haDecl;
    if (ha) {
      CellDecl d = getOrCreateDecl(
          top, ha->name,
          ArrayRef<std::string>(ha->inputPins.begin(), ha->inputPins.end()),
          ha->sumPin, ha->carryPin);
      if (d.valid)
        haDecl = d;
    }

    // The datapath round trip below cannot lower around LLHD residue (e.g. an
    // llhd.combinational holding a cf loop); CIRCT's convert-datapath-to-comb
    // then fails WITHOUT a diagnostic, and this pass used to exit 1 silently.
    // Name the residue instead.
    {
      Operation *residue = nullptr;
      top.walk<WalkOrder::PreOrder>([&](Operation *op) {
        if (!residue && op->getName().getDialectNamespace() == "llhd")
          residue = op;   // pre-order: the outermost LLHD op is reported
      });
      if (residue) {
        residue->emitError("map-compressor-cells: LLHD op '")
            << residue->getName()
            << "' left after lowering; the datapath conversion cannot run "
               "around it (lower or remove it first)";
        return signalPassFailure();
      }
    }
    // comb -> datapath, then datapath -> comb with the compress ops kept.
    {
      PassManager pm(top.getContext(), ModuleOp::getOperationName());
      pm.addPass(circt::createConvertCombToDatapath());
      circt::ConvertDatapathToCombOptions opts;
      opts.lowerCompress = false;
      opts.timingAware = false;
      pm.addPass(circt::createConvertDatapathToComb(opts));
      pm.addPass(createCanonicalizerPass());
      if (failed(runPipeline(pm, top))) {
        top.emitError("map-compressor-cells: the comb -> datapath -> comb "
                      "round trip failed (convert-comb-to-datapath / "
                      "convert-datapath-to-comb); see any diagnostic above");
        return signalPassFailure();
      }
    }

    MLIRContext *ctx = top.getContext();
    if (mergeAdds.getValue()) {
      unsigned n = mergeAddsIntoCompress(top);
      llvm::errs() << "[map-compressor-cells] merge-adds: " << n
                   << " carry-propagate add(s) folded into compress trees\n";
    }
    if (flattenAdds.getValue()) {
      unsigned n = flattenAddsIntoCompress(top, flattenGated.getValue(),
                                           flattenMaxTerms.getValue(), 0);
      llvm::errs() << "[map-compressor-cells] flatten-adds: " << n
                   << " add expression(s) flattened into compress trees\n";
    }
    Type i1 = IntegerType::get(ctx, 1);
    unsigned nTrees = 0, nFull = 0, nHalf = 0, nBits = 0, nPrefixAdders = 0;
    cellLib = &*lib;
    cellDecls.clear();
    nPrefixCells = 0;
    SmallVector<datapath::CompressOp> targets;
    for (auto hwmod : top.getOps<hw::HWModuleOp>())
      hwmod.walk([&](datapath::CompressOp c) { targets.push_back(c); });

    for (datapath::CompressOp comp : targets) {
      OpBuilder b(comp);
      Location loc = comp.getLoc();
      unsigned W = comp.getInputs().front().getType().getIntOrFloatBitWidth();
      unsigned nOut = comp.getNumResults();
      // Columns of live bits, LSB first.
      SmallVector<SmallVector<Value, 8>> col(W);
      for (Value row : comp.getInputs()) {
        for (unsigned j = 0; j < W; ++j) {
          auto [src, sb] = bitSource(row, j);
          if (auto k = src.getDefiningOp<hw::ConstantOp>()) {
            if (!k.getValue()[sb])
              continue; // a zero adds nothing
            col[j].push_back(hw::ConstantOp::create(b, loc, APInt(1, 1)));
            continue;
          }
          col[j].push_back(comb::ExtractOp::create(b, loc, i1, src, sb));
          ++nBits;
        }
      }
      auto maxHeight = [&] {
        size_t h = 0;
        for (auto &c : col)
          h = std::max(h, c.size());
        return h;
      };
      auto fullAdder = [&](Value x, Value y, Value z) {
        SmallVector<Value, 3> args(3);
        args[faDecl.inIdx[0]] = x;
        args[faDecl.inIdx[1]] = y;
        args[faDecl.inIdx[2]] = z;
        auto inst = hw::InstanceOp::create(
            b, loc, faDecl.op,
            StringAttr::get(ctx, (Twine("cmp_fa_") + Twine(nFull)).str()),
            args);
        ++nFull;
        return std::make_pair(inst.getResult(faDecl.sumIdx),
                              inst.getResult(faDecl.carryIdx));
      };
      auto halfAdder = [&](Value x, Value y) {
        if (haDecl) {
          SmallVector<Value, 2> args(2);
          args[haDecl->inIdx[0]] = x;
          args[haDecl->inIdx[1]] = y;
          auto inst = hw::InstanceOp::create(
              b, loc, haDecl->op,
              StringAttr::get(ctx, (Twine("cmp_ha_") + Twine(nHalf)).str()),
              args);
          ++nHalf;
          return std::make_pair(inst.getResult(haDecl->sumIdx),
                                inst.getResult(haDecl->carryIdx));
        }
        Value zero = hw::ConstantOp::create(b, loc, APInt(1, 0));
        return fullAdder(x, y, zero);
      };
      // Dadda: reduce stage by stage to the largest Dadda height below the
      // current maximum, counting the carries a column receives from the
      // column below IN THIS STAGE toward its height.
      while (maxHeight() > nOut) {
        size_t h = maxHeight();
        size_t target = nOut; // 2, 3, 4, 6, 9, 13, ...
        while (target * 3 / 2 < h)
          target = target * 3 / 2;
        if (target >= h)
          target = nOut;
        SmallVector<SmallVector<Value, 8>> next(W);
        SmallVector<Value, 8> carriesIn;
        for (unsigned j = 0; j < W; ++j) {
          SmallVector<Value, 8> in = std::move(col[j]);
          SmallVector<Value, 8> carriesOut;
          size_t height = in.size() + carriesIn.size();
          size_t reduceBy = height > target ? height - target : 0;
          size_t pos = 0;
          while (reduceBy >= 2 && pos + 3 <= in.size()) {
            auto [s, c] = fullAdder(in[pos], in[pos + 1], in[pos + 2]);
            next[j].push_back(s);
            carriesOut.push_back(c);
            pos += 3;
            reduceBy -= 2;
          }
          if (reduceBy >= 1 && pos + 2 <= in.size()) {
            auto [s, c] = halfAdder(in[pos], in[pos + 1]);
            next[j].push_back(s);
            carriesOut.push_back(c);
            pos += 2;
          }
          for (; pos < in.size(); ++pos)
            next[j].push_back(in[pos]);
          next[j].append(carriesIn.begin(), carriesIn.end());
          carriesIn = std::move(carriesOut); // into column j+1 (dropped past W)
        }
        col = std::move(next);
      }
      // Assemble the result rows: row r takes the r-th bit of every column.
      SmallVector<Value> results;
      for (unsigned r = 0; r < nOut; ++r) {
        SmallVector<Value> msbFirst;
        for (int j = W - 1; j >= 0; --j) {
          if (r < col[j].size())
            msbFirst.push_back(col[j][r]);
          else
            msbFirst.push_back(hw::ConstantOp::create(b, loc, APInt(1, 0)));
        }
        results.push_back(W == 1 ? msbFirst[0]
                                 : comb::ConcatOp::create(b, loc, msbFirst));
      }
      // Optionally build the final two-row add here, as cells, so ABC never
      // gets the chance to area-recover its prefix structure into a ripple.
      // Only the shape DatapathToComb produces is handled: both rows feed
      // exactly one comb.add of the tree's width and nothing else.
      comb::AddOp finalAdd;
      if (prefixFinalAdder && nOut == 2 && comp.getResult(0).hasOneUse() &&
          comp.getResult(1).hasOneUse()) {
        Operation *u0 = *comp.getResult(0).getUsers().begin();
        Operation *u1 = *comp.getResult(1).getUsers().begin();
        auto add = dyn_cast<comb::AddOp>(u0);
        if (add && u0 == u1 && add.getNumOperands() == 2 &&
            add.getType().getIntOrFloatBitWidth() == W)
          finalAdd = add;
      }
      if (finalAdd) {
        auto asSig = [&](Value v) {
          if (auto k = v.getDefiningOp<hw::ConstantOp>())
            return Sig{Value(), false, k.getValue().isOne() ? 1 : 0};
          return Sig{v, false, -1};
        };
        SmallVector<Sig> xs(W), ys(W);
        for (unsigned j = 0; j < W; ++j) {
          xs[j] = col[j].size() > 0 ? asSig(col[j][0]) : Sig{Value(), false, 0};
          ys[j] = col[j].size() > 1 ? asSig(col[j][1]) : Sig{Value(), false, 0};
        }
        if (auto sum = buildPrefixAdder(b, loc, top, xs, ys)) {
          SmallVector<Value> msbFirst(sum->rbegin(), sum->rend());
          Value v = W == 1 ? msbFirst[0]
                           : comb::ConcatOp::create(b, loc, msbFirst).getResult();
          finalAdd.getResult().replaceAllUsesWith(v);
          finalAdd.erase();
          ++nPrefixAdders;
        }
      }
      comp.replaceAllUsesWith(results);
      comp.erase();
      ++nTrees;
    }
    llvm::errs() << "[map-compressor-cells] " << nTrees << " compressor tree(s), "
                 << nBits << " input bit(s) -> " << nFull << " " << fa->name
                 << " + " << nHalf << " " << (ha ? ha->name : std::string("(no HA)"))
                 << "\n";
    if (prefixFinalAdder)
      llvm::errs() << "[map-compressor-cells] " << nPrefixAdders
                   << " final adder(s) built as Brent-Kung prefix networks, "
                   << nPrefixCells << " cell(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapCompressorCellsPass() {
  return std::make_unique<MapCompressorCellsPass>();
}

} // namespace circt_abc
