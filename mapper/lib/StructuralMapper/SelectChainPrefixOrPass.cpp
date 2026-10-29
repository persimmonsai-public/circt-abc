//===- SelectChainPrefixOrPass.cpp ----------------------------------------===//
//
// Collapse a pass-through select chain into a prefix OR.
//
// WHY.  A daisy chain whose stages forward their input unchanged when the
// input is "taken", e.g. a systolic-array PE row:
//
//     p_k   = in_k.valid & !in_k.mac_read
//     out_k = p_k ? in_k : (pipe_valid_k ? pipe_k : 0);   in_{k+1} = out_k
//
// computes each stage's select from the PREVIOUS stage's mux output, so the
// selects form a serial chain: p_{k+1} = f(mux(p_k, in_k, B_k)), a few
// cells per PE, two of them driving the wide data mux, so the path depth
// grows linearly along the row.
//
// But under p_k = 1 the mux forwards in_k, whose valid/mac_read bits are
// exactly the ones p_k was computed from, so p_{k+1} is 1:
//
//     p_{k+1} = p_k ? 1 : f(B_k) = p_k | q_k,        q_k = f(B_k)
//
// and unrolled, p_k = p_0 | q_0 | ... | q_{k-1}: a prefix OR of stage-local
// terms, with no dependence on the previous stage's mux.
//
// WHAT.  For every i1 value X that is the select of some comb.mux and is an
// and/or/xor cone (bounded by max-depth / max-nodes) reading a comb.mux M
// only through single-bit extracts, Shannon-expand X on M's select s:
//
//     X = s ? X[M := M.true] : X[M := M.false]
//
// and evaluate the true cofactor under s = 1 and what s = 1 implies
// (CombFacts: AND operands, inverted XORs, equality compares), seeing
// through concat / extract / decided muxes. If it is decided to a constant
// c, X is replaced by  c ? s | X0 : !s & X0  with X0 = X[M := M.false].
// Otherwise X is left alone (the rewrite only fires when it removes X's
// dependence on M.true). Finally, the ORs this creates are flattened into
// one n-ary OR each (operands of ORs the pass made are spliced in, up to
// max-or-width), which is the prefix OR.
//
// Function-preserving by construction (Shannon expansion; each cofactor
// substitutes M by the arm M equals under that value of s).
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "CombFacts.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SetVector.h"
#include "llvm/ADT/SmallVector.h"

#include <optional>

using namespace mlir;
using namespace circt;

namespace circt_abc {

namespace {

struct SelectChainPrefixOrPass
    : public PassWrapper<SelectChainPrefixOrPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SelectChainPrefixOrPass)

  SelectChainPrefixOrPass() = default;
  SelectChainPrefixOrPass(const SelectChainPrefixOrPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    maxDepth = other.maxDepth.getValue();
    maxNodes = other.maxNodes.getValue();
    maxOrWidth = other.maxOrWidth.getValue();
  }

  StringRef getArgument() const final { return "select-chain-prefix-or"; }
  StringRef getDescription() const final {
    return "Collapse a pass-through mux select chain into a prefix OR";
  }

  Option<bool> enable{*this, "enable", llvm::cl::init(false),
                      llvm::cl::desc("Enable the rewrite (OFF by default)")};
  Option<unsigned> maxDepth{
      *this, "max-depth", llvm::cl::init(4),
      llvm::cl::desc("Levels of i1 and/or/xor searched below each select")};
  Option<unsigned> maxNodes{
      *this, "max-nodes", llvm::cl::init(32),
      llvm::cl::desc("Ops in a select's cone before giving up")};
  Option<unsigned> maxOrWidth{
      *this, "max-or-width", llvm::cl::init(64),
      llvm::cl::desc("Operands of a flattened prefix OR")};

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  // (input, bit) -> an existing single-bit extract of it, so facts recorded
  // on that extract can be found from a bit position.
  llvm::DenseMap<std::pair<Value, unsigned>, Value> bitIndex;

  static bool isConeOp(Operation *op) {
    return op && isa<comb::AndOp, comb::OrOp, comb::XorOp>(op) &&
           op->getResult(0).getType().isInteger(1);
  }

  /// Collect the cone of `x` (i1 and/or/xor ops, root first) and its leaves.
  bool collectCone(Value x, SmallVectorImpl<Operation *> &cone,
                   llvm::SetVector<Value> &leaves) {
    llvm::DenseSet<Operation *> seen;
    SmallVector<std::pair<Value, unsigned>> work{{x, 0}};
    while (!work.empty()) {
      auto [v, d] = work.pop_back_val();
      Operation *op = v.getDefiningOp();
      if (!isConeOp(op) || d >= maxDepth) {
        leaves.insert(v);
        continue;
      }
      if (!seen.insert(op).second)
        continue;
      if (cone.size() >= maxNodes)
        return false;
      cone.push_back(op);
      for (Value in : op->getOperands())
        work.push_back({in, d + 1});
    }
    return true;
  }

  /// The mux M a leaf reads one bit of, if the leaf is extract(M, b) : i1.
  static std::optional<std::pair<comb::MuxOp, unsigned>> muxBit(Value leaf) {
    auto ex = leaf.getDefiningOp<comb::ExtractOp>();
    if (!ex || !ex.getType().isInteger(1))
      return std::nullopt;
    auto m = ex.getInput().getDefiningOp<comb::MuxOp>();
    if (!m)
      return std::nullopt;
    return std::make_pair(m, ex.getLowBit());
  }

  /// Value of bit `b` of `w` under `facts`, if decided.
  std::optional<bool> evalBit(Value w, unsigned b, const CombFacts &facts,
                              unsigned depth) {
    if (depth > 16)
      return std::nullopt;
    if (w.getType().isInteger(1))
      return eval(w, facts, depth + 1);
    if (auto c = w.getDefiningOp<hw::ConstantOp>())
      return c.getValue()[b];
    if (auto it = bitIndex.find({w, b}); it != bitIndex.end())
      if (auto k = facts.lookup(it->second))
        return k;
    Operation *op = w.getDefiningOp();
    if (auto cat = dyn_cast_or_null<comb::ConcatOp>(op)) {
      // concat operands are MSB first
      unsigned lo = cast<IntegerType>(w.getType()).getWidth();
      for (Value in : cat.getInputs()) {
        unsigned wd = cast<IntegerType>(in.getType()).getWidth();
        lo -= wd;
        if (b >= lo && b < lo + wd)
          return evalBit(in, b - lo, facts, depth + 1);
      }
      return std::nullopt;
    }
    if (auto ex = dyn_cast_or_null<comb::ExtractOp>(op))
      return evalBit(ex.getInput(), ex.getLowBit() + b, facts, depth + 1);
    if (auto m = dyn_cast_or_null<comb::MuxOp>(op))
      if (auto s = eval(m.getCond(), facts, depth + 1))
        return evalBit(*s ? m.getTrueValue() : m.getFalseValue(), b, facts,
                       depth + 1);
    return std::nullopt;
  }

  /// Value of i1 `v` under `facts`, if decided; `sub` maps a leaf to the
  /// (wide value, bit) it reads in the cofactor being evaluated.
  llvm::DenseMap<Value, std::pair<Value, unsigned>> sub;
  std::optional<bool> eval(Value v, const CombFacts &facts, unsigned depth) {
    if (depth > 16)
      return std::nullopt;
    if (auto it = sub.find(v); it != sub.end())
      return evalBit(it->second.first, it->second.second, facts, depth + 1);
    if (auto k = facts.lookup(v))
      return k;
    if (auto c = v.getDefiningOp<hw::ConstantOp>())
      return c.getValue().isAllOnes();
    Operation *op = v.getDefiningOp();
    if (!op)
      return std::nullopt;
    if (isa<comb::AndOp, comb::OrOp>(op)) {
      bool isAnd = isa<comb::AndOp>(op);
      bool all = true;
      for (Value in : op->getOperands()) {
        auto k = eval(in, facts, depth + 1);
        if (k && *k != isAnd)
          return !isAnd; // a controlling value
        all &= k.has_value();
      }
      if (all)
        return isAnd;
      return std::nullopt;
    }
    if (auto x = dyn_cast<comb::XorOp>(op)) {
      bool parity = false;
      for (Value in : x.getInputs()) {
        auto k = eval(in, facts, depth + 1);
        if (!k)
          return std::nullopt;
        parity ^= *k;
      }
      return parity;
    }
    if (auto ex = dyn_cast<comb::ExtractOp>(op))
      if (v.getType().isInteger(1))
        return evalBit(ex.getInput(), ex.getLowBit(), facts, depth + 1);
    if (auto m = dyn_cast<comb::MuxOp>(op))
      if (auto s = eval(m.getCond(), facts, depth + 1))
        return eval(*s ? m.getTrueValue() : m.getFalseValue(), facts,
                    depth + 1);
    return std::nullopt;
  }

  unsigned numRewritten = 0, numFlattened = 0;

  void runOnOperation() override {
    if (!enable)
      return;
    for (auto hwMod : getOperation().getOps<hw::HWModuleOp>())
      runOnModule(hwMod);
    if (numRewritten)
      llvm::errs() << "[select-chain-prefix-or] " << numRewritten
                   << " select(s) cofactored, " << numFlattened
                   << " prefix OR(s) flattened\n";
  }

  void runOnModule(hw::HWModuleOp hwMod) {
    bitIndex.clear();
    llvm::SetVector<Value> selects;
    hwMod.walk([&](Operation *op) {
      if (auto ex = dyn_cast<comb::ExtractOp>(op)) {
        if (ex.getType().isInteger(1))
          bitIndex.try_emplace({ex.getInput(), ex.getLowBit()}, ex);
      } else if (auto m = dyn_cast<comb::MuxOp>(op)) {
        if (isConeOp(m.getCond().getDefiningOp()))
          selects.insert(m.getCond());
      }
    });

    OpBuilder b(&getContext());
    // Phase 1: decide every rewrite on the ORIGINAL IR (each select's
    // cofactor uses its predecessor's original structure), then apply.
    SmallVector<std::pair<Value, Value>> repl;
    llvm::SetVector<Operation *> madeOrs; // ordered: IR order must be deterministic
    for (Value x : selects) {
      SmallVector<Operation *> cone;
      llvm::SetVector<Value> leaves;
      if (!collectCone(x, cone, leaves))
        continue;
      llvm::SetVector<Operation *> muxes;
      for (Value l : leaves)
        if (auto mb = muxBit(l))
          muxes.insert(mb->first);
      for (Operation *mop : muxes) {
        auto m = cast<comb::MuxOp>(mop);
        Value s = m.getCond();
        if (s == x)
          continue;
        CombFacts facts;
        facts.add(s, true);
        sub.clear();
        for (Value l : leaves)
          if (auto mb = muxBit(l); mb && mb->first == m)
            sub[l] = {m.getTrueValue(), mb->second};
        auto c = eval(x, facts, 0);
        sub.clear();
        if (!c)
          continue;
        // X0 = X[M := M.false]: clone the cone, leaves of M read M.false.
        b.setInsertionPoint(x.getDefiningOp());
        Location loc = x.getLoc();
        llvm::DenseMap<Value, Value> map;
        for (Value l : leaves)
          if (auto mb = muxBit(l); mb && mb->first == m)
            map[l] = comb::ExtractOp::create(b, loc, b.getIntegerType(1),
                                              m.getFalseValue(), mb->second);
        for (Operation *op : llvm::reverse(cone)) {
          Operation *cl = b.clone(*op);
          for (OpOperand &o : cl->getOpOperands())
            if (auto it = map.find(o.get()); it != map.end())
              o.set(it->second);
          map[op->getResult(0)] = cl->getResult(0);
        }
        Value x0 = map[x];
        Value nx;
        if (*c) {
          nx = comb::OrOp::create(b, loc, ValueRange{s, x0}, false);
          madeOrs.insert(nx.getDefiningOp());
        } else {
          Value t = hw::ConstantOp::create(b, loc, APInt(1, 1));
          Value ns = comb::XorOp::create(b, loc, ValueRange{s, t}, false);
          nx = comb::AndOp::create(b, loc, ValueRange{ns, x0}, false);
        }
        repl.push_back({x, nx});
        ++numRewritten;
        break;
      }
    }
    for (auto [x, nx] : repl)
      x.replaceAllUsesWith(nx);

    // Phase 2: splice the operands of pass-made ORs into their users that
    // the pass also made: or(or(p0, q0), q1) -> or(p0, q0, q1).
    // All lists are computed before any OR is replaced, so a user's operand
    // still names the pass-made OR it splices.
    llvm::DenseMap<Operation *, SmallVector<Value>> flat;
    std::function<SmallVector<Value>(Operation *)> flatten =
        [&](Operation *o) -> SmallVector<Value> {
      if (auto it = flat.find(o); it != flat.end())
        return it->second;
      flat[o] = SmallVector<Value>(o->getOperands()); // cycle guard
      llvm::SetVector<Value> ops;
      for (Value in : o->getOperands()) {
        Operation *d = in.getDefiningOp();
        if (d && d != o && madeOrs.contains(d)) {
          SmallVector<Value> inner = flatten(d);
          if (ops.size() + inner.size() <= maxOrWidth) {
            ops.insert(inner.begin(), inner.end());
            continue;
          }
        }
        ops.insert(in);
      }
      SmallVector<Value> res(ops.begin(), ops.end());
      flat[o] = res;
      return res;
    };
    SmallVector<Operation *> made(madeOrs.begin(), madeOrs.end());
    for (Operation *o : made)
      (void)flatten(o);
    for (Operation *o : made) {
      const SmallVector<Value> &ops = flat[o];
      if (ops.size() == o->getNumOperands())
        continue;
      b.setInsertionPoint(o);
      Value n = comb::OrOp::create(b, o->getLoc(), ops, false);
      o->getResult(0).replaceAllUsesWith(n);
      ++numFlattened;
    }
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSelectChainPrefixOrPass() {
  return std::make_unique<SelectChainPrefixOrPass>();
}

} // namespace circt_abc
