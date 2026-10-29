//===- NarrowWholeValueReadsPass.cpp - break word-level false cycles ------===//
//
// Direction 0, the false combinational loop, root-caused 2026-09-04: the RTL
// has no loop (Yosys reports none), CIRCT's lowering creates them. The main
// mechanism is WHOLE-VALUE READS: llhd-sig2reg / mem2reg accumulate every
// drive to a partially-driven signal into one merged word
// (`or(shl(drive_k, off), and(prev, ~mask))` chains, `concat`s, bitcast
// pairs) and resolve every read of a slice as `comb.extract` of that word,
// so bit i structurally depends on every drive to every other bit. A tree
// `a[k] = f(a[2k+1], a[2k+2])` (common_cells lzc, rr_arb_tree) or a
// field-wise always_comb becomes `word = f(word)`: a word-level SCC that is
// acyclic at the bit level. On cv64a6 every one of the shipped flow's 45
// feedback-cut ops (all 3,724 `u_shell_` cells, the 24 PMP lzc OR trees among
// them) is this mechanism, and the cut ops sit on the sound critical paths
// 5-12 times each.
//
// This pass is cycle-directed: inside every non-trivial SCC of a module's
// combinational dataflow it pushes `comb.extract` through the transparent
// word-merging ops to the actual slice drivers -- n-ary and/or/xor, mux,
// concat, shifts by a constant, same-width bitcast pairs -- then folds
// (canonicalization) and repeats until no SCC holds a rewritable extract.
// Modules without word-level cycles (ibex) are left byte-identical.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

static bool isCombGraphOp(Operation *op) {
  if (isa<hw::InstanceOp, hw::OutputOp, hw::ConstantOp>(op))
    return false;
  if (op->getNumResults() != 1 || op->getNumRegions() != 0)
    return false;
  StringRef dialect = op->getDialect()->getNamespace();
  return dialect == "comb" || dialect == "hw";
}

/// All non-trivial SCCs of the module body's combinational dataflow (op ->
/// operand-defining op, both in the graph set). Returns the number of SCCs
/// and fills `sccOf` (op -> scc index, only for ops in a non-trivial SCC).
static unsigned findSCCs(hw::HWModuleOp module,
                         llvm::DenseMap<Operation *, unsigned> &sccOf) {
  SmallVector<Operation *> ops;
  llvm::DenseMap<Operation *, unsigned> index;
  for (Operation &op : module.getBodyBlock()->getOperations())
    if (isCombGraphOp(&op)) {
      index[&op] = ops.size();
      ops.push_back(&op);
    }
  unsigned n = ops.size();
  SmallVector<SmallVector<unsigned, 4>> succ(n);
  for (unsigned i = 0; i < n; ++i)
    for (Value operand : ops[i]->getOperands())
      if (Operation *def = operand.getDefiningOp())
        if (auto it = index.find(def); it != index.end())
          succ[i].push_back(it->second);

  // Iterative Tarjan, all SCCs.
  SmallVector<int> idxOf(n, -1), low(n, 0);
  SmallVector<bool> onStack(n, false);
  SmallVector<unsigned> stack;
  unsigned counter = 0, numScc = 0;
  for (unsigned root = 0; root < n; ++root) {
    if (idxOf[root] != -1)
      continue;
    SmallVector<std::pair<unsigned, unsigned>> work{{root, 0}};
    while (!work.empty()) {
      auto &[v, pi] = work.back();
      if (pi == 0) {
        idxOf[v] = low[v] = counter++;
        stack.push_back(v);
        onStack[v] = true;
      }
      bool descended = false;
      while (pi < succ[v].size()) {
        unsigned w = succ[v][pi++];
        if (idxOf[w] == -1) {
          work.push_back({w, 0});
          descended = true;
          break;
        }
        if (onStack[w])
          low[v] = std::min(low[v], idxOf[w]);
      }
      if (descended)
        continue;
      if (low[v] == idxOf[v]) {
        SmallVector<unsigned> comp;
        while (true) {
          unsigned w = stack.pop_back_val();
          onStack[w] = false;
          comp.push_back(w);
          if (w == v)
            break;
        }
        if (comp.size() > 1) {
          for (unsigned w : comp)
            sccOf[ops[w]] = numScc;
          ++numScc;
        }
      }
      unsigned finished = v;
      work.pop_back();
      if (!work.empty())
        low[work.back().first] =
            std::min(low[work.back().first], low[finished]);
    }
  }
  return numScc;
}

/// Extract bits [lo, lo+w) of `v` (a narrower extract, or `v` itself).
static Value slice(OpBuilder &b, Location loc, Value v, unsigned lo,
                   unsigned w) {
  unsigned vw = v.getType().getIntOrFloatBitWidth();
  if (lo == 0 && w == vw)
    return v;
  return comb::ExtractOp::create(b, loc, v, lo, w).getResult();
}

/// Rewrite one extract whose input is defined by a transparent word op.
/// Returns true if rewritten.
static bool narrowExtract(comb::ExtractOp ex) {
  Operation *def = ex.getInput().getDefiningOp();
  if (!def)
    return false;
  OpBuilder b(ex);
  Location loc = ex.getLoc();
  unsigned lo = ex.getLowBit();
  unsigned w = ex.getType().getIntOrFloatBitWidth();
  Value result;

  if (isa<comb::OrOp, comb::AndOp, comb::XorOp>(def)) {
    SmallVector<Value> ins;
    for (Value in : def->getOperands())
      ins.push_back(slice(b, loc, in, lo, w));
    bool twoState = def->hasAttr("twoState");
    if (isa<comb::OrOp>(def))
      result = comb::OrOp::create(b, loc, ins, twoState);
    else if (isa<comb::AndOp>(def))
      result = comb::AndOp::create(b, loc, ins, twoState);
    else
      result = comb::XorOp::create(b, loc, ins, twoState);
  } else if (auto mux = dyn_cast<comb::MuxOp>(def)) {
    result = comb::MuxOp::create(
        b, loc, mux.getCond(), slice(b, loc, mux.getTrueValue(), lo, w),
        slice(b, loc, mux.getFalseValue(), lo, w), mux.getTwoState());
  } else if (auto cc = dyn_cast<comb::ConcatOp>(def)) {
    // Inputs are MSB first; walk from the LSB end.
    SmallVector<Value> pieces; // MSB first
    unsigned pos = 0;          // LSB offset of the current input
    auto inputs = cc.getInputs();
    for (int i = (int)inputs.size() - 1; i >= 0; --i) {
      Value in = inputs[i];
      unsigned iw = in.getType().getIntOrFloatBitWidth();
      unsigned a = std::max(lo, pos), z = std::min(lo + w, pos + iw);
      if (a < z)
        pieces.insert(pieces.begin(), slice(b, loc, in, a - pos, z - a));
      pos += iw;
    }
    if (pieces.empty())
      return false;
    result = pieces.size() == 1 ? pieces[0]
                                : comb::ConcatOp::create(b, loc, pieces);
  } else if (auto sh = dyn_cast<comb::ShlOp>(def)) {
    auto cst = sh.getRhs().getDefiningOp<hw::ConstantOp>();
    if (!cst)
      return false;
    uint64_t c = cst.getValue().getLimitedValue();
    unsigned xw = sh.getLhs().getType().getIntOrFloatBitWidth();
    if (c >= xw)
      return false;
    // bits [lo, lo+w) of (x << c)
    if (lo + w <= c) {
      result = hw::ConstantOp::create(b, loc, APInt(w, 0));
    } else if (lo >= c) {
      result = slice(b, loc, sh.getLhs(), lo - c, w);
    } else {
      unsigned zeros = c - lo;
      Value hi = slice(b, loc, sh.getLhs(), 0, w - zeros);
      Value z = hw::ConstantOp::create(b, loc, APInt(zeros, 0));
      result = comb::ConcatOp::create(b, loc, ValueRange{hi, z});
    }
  } else if (auto sh = dyn_cast<comb::ShrUOp>(def)) {
    auto cst = sh.getRhs().getDefiningOp<hw::ConstantOp>();
    if (!cst)
      return false;
    uint64_t c = cst.getValue().getLimitedValue();
    unsigned xw = sh.getLhs().getType().getIntOrFloatBitWidth();
    if (c >= xw)
      return false;
    // bits [lo, lo+w) of (x >> c) = x[lo+c ..], zero above xw
    if (lo + c >= xw) {
      result = hw::ConstantOp::create(b, loc, APInt(w, 0));
    } else if (lo + c + w <= xw) {
      result = slice(b, loc, sh.getLhs(), lo + c, w);
    } else {
      unsigned avail = xw - (lo + c);
      Value lowBits = slice(b, loc, sh.getLhs(), lo + c, avail);
      Value z = hw::ConstantOp::create(b, loc, APInt(w - avail, 0));
      result = comb::ConcatOp::create(b, loc, ValueRange{z, lowBits});
    }
  } else if (auto bc = dyn_cast<hw::BitcastOp>(def)) {
    // bitcast(bitcast(x)) with x of the same integer type as the outer
    // result: read x directly.
    auto inner = bc.getInput().getDefiningOp<hw::BitcastOp>();
    if (!inner || inner.getInput().getType() != bc.getType())
      return false;
    result = slice(b, loc, inner.getInput(), lo, w);
  } else {
    return false;
  }
  ex.getResult().replaceAllUsesWith(result);
  ex.erase();
  return true;
}

struct NarrowWholeValueReadsPass
    : public PassWrapper<NarrowWholeValueReadsPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowWholeValueReadsPass)

  NarrowWholeValueReadsPass() = default;
  NarrowWholeValueReadsPass(const NarrowWholeValueReadsPass &o)
      : PassWrapper(o) {
    maxRounds = o.maxRounds.getValue();
    dumpScc = o.dumpScc.getValue();
  }

  Option<unsigned> maxRounds{
      *this, "max-rounds", llvm::cl::init(1024),
      llvm::cl::desc("rewrite/fold rounds per module")};
  Option<bool> dumpScc{
      *this, "dump-scc", llvm::cl::init(false),
      llvm::cl::desc("print the op-kind histogram of every surviving SCC and "
                     "the kinds feeding its extracts")};

  StringRef getArgument() const final { return "narrow-whole-value-reads"; }
  StringRef getDescription() const final {
    return "Push comb.extract through merged words inside combinational SCCs "
           "(breaks the word-level false cycles sig2reg/mem2reg create)";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  void runOnOperation() override {
    MLIRContext *ctx = &getContext();
    RewritePatternSet patterns(ctx);
    for (RegisteredOperationName name : ctx->getRegisteredOperations())
      name.getCanonicalizationPatterns(patterns, ctx);
    FrozenRewritePatternSet frozen(std::move(patterns));

    unsigned modulesTouched = 0, totalRewrites = 0, sccBefore = 0,
             sccAfter = 0;
    for (auto module : getOperation().getOps<hw::HWModuleOp>()) {
      llvm::DenseMap<Operation *, unsigned> sccOf;
      unsigned before = findSCCs(module, sccOf);
      if (before == 0)
        continue;
      sccBefore += before;
      unsigned rewrites = 0, after = before;
      for (unsigned round = 0; round < maxRounds && after; ++round) {
        SmallVector<comb::ExtractOp> victims;
        for (auto [op, scc] : sccOf)
          if (auto ex = dyn_cast<comb::ExtractOp>(op))
            if (Operation *def = ex.getInput().getDefiningOp())
              if (auto it = sccOf.find(def);
                  it != sccOf.end() && it->second == scc)
                victims.push_back(ex);
        unsigned done = 0;
        for (comb::ExtractOp ex : victims)
          if (narrowExtract(ex))
            ++done;
        if (done == 0)
          break;
        rewrites += done;
        // Fold: extract(concat), extract(constant), and-with-0, dead ops.
        GreedyRewriteConfig cfg;
        (void)applyPatternsGreedily(module, frozen, cfg);
        sccOf.clear();
        after = findSCCs(module, sccOf);
      }
      sccAfter += after;
      if (after && dumpScc) {
        llvm::StringMap<unsigned> kinds, feeders;
        for (auto [op, scc] : sccOf) {
          kinds[op->getName().getStringRef()]++;
          if (auto ex = dyn_cast<comb::ExtractOp>(op))
            if (Operation *def = ex.getInput().getDefiningOp())
              feeders[(sccOf.count(def) ? "in-scc " : "outside ") +
                      def->getName().getStringRef().str()]++;
        }
        llvm::errs() << "  surviving SCC op kinds:";
        for (auto &kv : kinds)
          llvm::errs() << " " << kv.first() << "=" << kv.second;
        llvm::errs() << "\n  extract inputs:";
        for (auto &kv : feeders)
          llvm::errs() << " " << kv.first() << "=" << kv.second;
        llvm::errs() << "\n";
      }
      if (rewrites)
        ++modulesTouched;
      if (rewrites || after)
        llvm::errs() << "[narrow-whole-value-reads] " << module.getName()
                     << ": SCCs " << before << " -> " << after << ", "
                     << rewrites << " extract(s) narrowed\n";
      totalRewrites += rewrites;
    }
    llvm::errs() << "[narrow-whole-value-reads] " << modulesTouched
                 << " module(s) rewritten, " << totalRewrites
                 << " extract(s), SCCs " << sccBefore << " -> " << sccAfter
                 << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowWholeValueReadsPass() {
  return std::make_unique<NarrowWholeValueReadsPass>();
}

} // namespace circt_abc
