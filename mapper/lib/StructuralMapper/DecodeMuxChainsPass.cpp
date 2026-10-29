//===- DecodeMuxChainsPass.cpp -----------------------------*- C++ -*-===//
//
// `--decode-mux-chains`: rewrite priority mux chains whose conditions are
// provably mutually exclusive into a decoded one-hot AND-OR select.
//
// Why: the 2026-09-02 per-RTL-module AIG census (same instrument on both
// flows: yosys techmap + ABC strash/dc2) put circt-abc's remaining gate-
// count gap vs. Yosys in the modules dominated by wide selects: the
// register-file read (fixed at the array_get lowering), the CSR readback
// `case` (cs_registers 1.21x), and the instruction decoders' `unique case`
// (decoder 2.21x, compressed_decoder 1.41x). CIRCT lowers a `case` to a
// chain of 2:1 muxes -- ~3 AIG nodes per link per bit, and the chain
// structure survives ABC's dc2 -- while Yosys emits a `$pmux`: each arm
// ANDed with its (shared, decoded) select and OR-reduced, ~2 nodes per
// arm per bit.
//
// The rewrite is only valid when at most one condition of the chain can
// be true at a time (priority does not matter). Exclusivity is decided
// STRUCTURALLY, never by search: each condition is flattened through
// `comb.and` into a conjunction of `comb.icmp eq <key>, <constant>` /
// `comb.icmp ne` / bare i1 literals; two conditions are exclusive iff they
// share a key with different eq-constants, or one has `eq(k,C)` and the
// other `ne(k,C)`, or they contain the same i1 value in opposite polarity.
// Anything that cannot be flattened is opaque and the chain is left
// alone. The default (innermost false leg) keeps its value when no
// condition holds: `none = ~(c_1 | ... | c_n)` is one more arm.
//
// Chains shorter than `min-chain` (default 4) links are not rewritten:
// for two or three arms the mux chain is not worse than the decoded form.
//
// Correctness stance: the rewrite is a local, function-preserving
// identity under the exclusivity precondition (mux(c1, a, mux(c2, b, d))
// with c1 & c2 == 0 equals c1&a | c2&b | ~(c1|c2)&d), and the precondition
// is checked syntactically. Gated like every netlist-changing pass here:
// SAT reproducers, ibex byte/area, the top-level miters.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// One literal of a flattened condition.
struct Lit {
  enum Kind { Eq, Ne, Bit } kind;
  Value key;      // Eq/Ne: the compared value; Bit: the i1 value
  APInt constant; // Eq/Ne
  bool negated;   // Bit: polarity
};

struct DecodeMuxChainsPass
    : public PassWrapper<DecodeMuxChainsPass, OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(DecodeMuxChainsPass)

  DecodeMuxChainsPass() = default;
  DecodeMuxChainsPass(const DecodeMuxChainsPass &other) : PassWrapper(other) {
    minChain = other.minChain.getValue();
  }

  StringRef getArgument() const final { return "decode-mux-chains"; }
  StringRef getDescription() const final {
    return "Rewrite priority mux chains with structurally mutually exclusive "
           "conditions into a decoded one-hot AND-OR select (the $pmux "
           "form)";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<unsigned> minChain{
      *this, "min-chain",
      llvm::cl::desc("Minimum number of chain links to rewrite (0 = off)"),
      llvm::cl::init(4)};
  // Chains whose conditions are NOT provably exclusive can still be
  // decoded: sel_i = c_i & ~(c_0 | ... | c_{i-1}) is one-hot by
  // construction, so the same AND-OR form is an unconditional identity at
  // the cost of one OR and one AND per link on the (shared, 1-bit)
  // selects. Per data bit a link goes from 3 AIG nodes (mux) to 2
  // (and/or), and the depth from one level per link to a prefix chain on
  // the selects plus a log-depth OR. CVA6's TLB hit-select is 16 links x
  // 64 bits of exactly this. OFF by default (project convention).
  Option<bool> priorityDecode{
      *this, "priority-decode",
      llvm::cl::desc("Also decode non-exclusive chains with priority-encoded "
                      "one-hot selects"),
      llvm::cl::init(false)};
  Option<unsigned> priorityMinWidth{
      *this, "priority-min-width",
      llvm::cl::desc("Minimum data width for a priority-decoded chain"),
      llvm::cl::init(2)};

  using Term = SmallVector<Lit>;          // conjunction of literals
  using DNF = SmallVector<Term>;          // disjunction of conjunctions
  static constexpr unsigned kMaxTerms = 64;

  // Flatten a condition into a DNF through comb.or (union) and comb.and
  // (cartesian product, capped), with xor-with-true / icmp ne as negation
  // of a single literal. Any other i1 value is an opaque literal, usable
  // only for polarity exclusivity against its own negation. Returns false
  // when the DNF would exceed the cap.
  static bool flatten(Value c, DNF &out, unsigned depth = 0) {
    if (depth > 12)
      return false;
    if (auto icmp = c.getDefiningOp<comb::ICmpOp>()) {
      APInt k;
      if (matchPattern(icmp.getRhs(), m_ConstantInt(&k))) {
        auto pred = icmp.getPredicate();
        if (pred == comb::ICmpPredicate::eq || pred == comb::ICmpPredicate::ceq) {
          out.push_back({Lit{Lit::Eq, icmp.getLhs(), k, false}});
          return true;
        }
        if (pred == comb::ICmpPredicate::ne || pred == comb::ICmpPredicate::cne) {
          out.push_back({Lit{Lit::Ne, icmp.getLhs(), k, false}});
          return true;
        }
      }
      out.push_back({Lit{Lit::Bit, c, APInt(), false}});
      return true;
    }
    if (auto orOp = c.getDefiningOp<comb::OrOp>()) {
      for (Value in : orOp.getInputs()) {
        DNF sub;
        if (!flatten(in, sub, depth + 1))
          return false;
        out.append(sub.begin(), sub.end());
        if (out.size() > kMaxTerms)
          return false;
      }
      return true;
    }
    if (auto andOp = c.getDefiningOp<comb::AndOp>()) {
      DNF acc;
      acc.push_back(Term{});
      for (Value in : andOp.getInputs()) {
        DNF sub;
        if (!flatten(in, sub, depth + 1))
          return false;
        DNF next;
        for (const Term &a : acc)
          for (const Term &b : sub) {
            Term t(a.begin(), a.end());
            t.append(b.begin(), b.end());
            next.push_back(std::move(t));
            if (next.size() > kMaxTerms)
              return false;
          }
        acc = std::move(next);
      }
      out.append(acc.begin(), acc.end());
      return out.size() <= kMaxTerms;
    }
    if (auto xorOp = c.getDefiningOp<comb::XorOp>()) {
      APInt k;
      if (xorOp.getNumOperands() == 2 &&
          matchPattern(xorOp.getInputs()[1], m_ConstantInt(&k)) &&
          k.isAllOnes()) {
        DNF inner;
        if (flatten(xorOp.getInputs()[0], inner, depth + 1) &&
            inner.size() == 1 && inner[0].size() == 1) {
          Lit l = inner[0][0];
          if (l.kind == Lit::Eq)
            l.kind = Lit::Ne;
          else if (l.kind == Lit::Ne)
            l.kind = Lit::Eq;
          else
            l.negated = !l.negated;
          out.push_back({l});
          return true;
        }
        // ~(a | b | ...) or ~(a & b): opaque as a whole
      }
    }
    out.push_back({Lit{Lit::Bit, c, APInt(), false}});
    return true;
  }

  static bool exclusiveTerms(const Term &a, const Term &b) {
    for (const Lit &x : a)
      for (const Lit &y : b) {
        if (x.kind == Lit::Bit && y.kind == Lit::Bit) {
          if (x.key == y.key && x.negated != y.negated)
            return true;
          continue;
        }
        if (x.kind == Lit::Bit || y.kind == Lit::Bit)
          continue;
        if (x.key != y.key)
          continue;
        if (x.kind == Lit::Eq && y.kind == Lit::Eq && x.constant != y.constant)
          return true;
        if (x.kind != y.kind && x.constant == y.constant)
          return true; // eq(k,C) vs ne(k,C)
      }
    return false;
  }
  static bool exclusive(const DNF &a, const DNF &b) {
    for (const Term &x : a)
      for (const Term &y : b)
        if (!exclusiveTerms(x, y))
          return false;
    return true;
  }

  // ---- exact fallback: truth tables over a small support -------------
  // A value's bits as 2^n-entry tables over the support assignment; each
  // table is a vector of 64-bit words. Support bits are the i1 slices of
  // every non-comb leaf value reachable from the condition cones.
  static constexpr unsigned kMaxSupport = 16;
  using Table = SmallVector<uint64_t, 16>;         // one bit of a value
  using Tables = SmallVector<Table, 4>;             // all bits of a value

  struct TTContext {
    unsigned n = 0;                                 // support bits
    llvm::DenseMap<Value, unsigned> leafBase;       // leaf value -> first support bit
    llvm::DenseMap<Value, Tables> memo;
    unsigned words() const { return n <= 6 ? 1 : (1u << (n - 6)); }
    Table constantTable(bool one) const {
      Table t(words(), one ? ~0ull : 0ull);
      if (n < 6 && one)
        t[0] = (1ull << (1u << n)) - 1;
      return t;
    }
    Table varTable(unsigned bit) const {
      Table t(words(), 0);
      unsigned total = 1u << n;
      for (unsigned a = 0; a < total; ++a)
        if ((a >> bit) & 1)
          t[a >> 6] |= 1ull << (a & 63);
      return t;
    }
  };

  static bool isCombOp(Operation *op) {
    return op && (isa<comb::AndOp, comb::OrOp, comb::XorOp, comb::MuxOp,
                      comb::ExtractOp, comb::ConcatOp, comb::ReplicateOp,
                      comb::ICmpOp, hw::ConstantOp>(op));
  }

  // Collect leaves (non-comb values) of the cones; false if the support
  // would exceed kMaxSupport bits.
  static bool collectSupport(ArrayRef<Value> roots, TTContext &ctx) {
    SmallVector<Value> stack(roots.begin(), roots.end());
    llvm::SmallPtrSet<Value, 32> seen;
    while (!stack.empty()) {
      Value v = stack.pop_back_val();
      if (!seen.insert(v).second)
        continue;
      Operation *def = v.getDefiningOp();
      if (isCombOp(def) && !isa<hw::ConstantOp>(def)) {
        for (Value in : def->getOperands())
          stack.push_back(in);
        continue;
      }
      if (isa_and_nonnull<hw::ConstantOp>(def))
        continue;
      auto ty = dyn_cast<IntegerType>(v.getType());
      if (!ty)
        return false;
      if (ctx.leafBase.count(v))
        continue;
      if (ctx.n + ty.getWidth() > kMaxSupport)
        return false;
      ctx.leafBase[v] = ctx.n;
      ctx.n += ty.getWidth();
    }
    return true;
  }

  static bool evalTT(Value v, TTContext &ctx, Tables &out) {
    auto it = ctx.memo.find(v);
    if (it != ctx.memo.end()) {
      out = it->second;
      return true;
    }
    auto ty = dyn_cast<IntegerType>(v.getType());
    if (!ty)
      return false;
    unsigned w = ty.getWidth();
    Tables r;
    auto lb = ctx.leafBase.find(v);
    if (lb != ctx.leafBase.end()) {
      for (unsigned b = 0; b < w; ++b)
        r.push_back(ctx.varTable(lb->second + b));
      ctx.memo[v] = r; out = r; return true;
    }
    Operation *def = v.getDefiningOp();
    APInt k;
    if (matchPattern(v, m_ConstantInt(&k))) {
      for (unsigned b = 0; b < w; ++b)
        r.push_back(ctx.constantTable(k[b]));
      ctx.memo[v] = r; out = r; return true;
    }
    if (!def)
      return false;
    unsigned W = ctx.words();
    auto bitwise = [&](auto fn, ValueRange ins, Table init) -> bool {
      for (unsigned b = 0; b < w; ++b) r.push_back(init);
      for (Value in : ins) {
        Tables t;
        if (!evalTT(in, ctx, t) || t.size() != w) return false;
        for (unsigned b = 0; b < w; ++b)
          for (unsigned i = 0; i < W; ++i)
            r[b][i] = fn(r[b][i], t[b][i]);
      }
      return true;
    };
    if (auto op = dyn_cast<comb::AndOp>(def)) {
      if (!bitwise([](uint64_t a, uint64_t b) { return a & b; }, op.getInputs(), ctx.constantTable(true))) return false;
    } else if (auto op = dyn_cast<comb::OrOp>(def)) {
      if (!bitwise([](uint64_t a, uint64_t b) { return a | b; }, op.getInputs(), ctx.constantTable(false))) return false;
    } else if (auto op = dyn_cast<comb::XorOp>(def)) {
      if (!bitwise([](uint64_t a, uint64_t b) { return a ^ b; }, op.getInputs(), ctx.constantTable(false))) return false;
    } else if (auto op = dyn_cast<comb::MuxOp>(def)) {
      Tables c, t, f;
      if (!evalTT(op.getCond(), ctx, c) || !evalTT(op.getTrueValue(), ctx, t) || !evalTT(op.getFalseValue(), ctx, f)) return false;
      for (unsigned b = 0; b < w; ++b) {
        Table x(W);
        for (unsigned i = 0; i < W; ++i) x[i] = (c[0][i] & t[b][i]) | (~c[0][i] & f[b][i]);
        r.push_back(x);
      }
    } else if (auto op = dyn_cast<comb::ExtractOp>(def)) {
      Tables in;
      if (!evalTT(op.getInput(), ctx, in)) return false;
      for (unsigned b = 0; b < w; ++b) r.push_back(in[op.getLowBit() + b]);
    } else if (auto op = dyn_cast<comb::ConcatOp>(def)) {
      for (int i = static_cast<int>(op.getInputs().size()) - 1; i >= 0; --i) {
        Tables in;
        if (!evalTT(op.getInputs()[i], ctx, in)) return false;
        r.append(in.begin(), in.end());
      }
    } else if (auto op = dyn_cast<comb::ReplicateOp>(def)) {
      Tables in;
      if (!evalTT(op.getInput(), ctx, in)) return false;
      while (r.size() < w) r.append(in.begin(), in.end());
    } else if (auto op = dyn_cast<comb::ICmpOp>(def)) {
      Tables a, bb;
      if (!evalTT(op.getLhs(), ctx, a) || !evalTT(op.getRhs(), ctx, bb) || a.size() != bb.size()) return false;
      auto pred = op.getPredicate();
      bool eq = pred == comb::ICmpPredicate::eq || pred == comb::ICmpPredicate::ceq || pred == comb::ICmpPredicate::weq;
      bool ne = pred == comb::ICmpPredicate::ne || pred == comb::ICmpPredicate::cne || pred == comb::ICmpPredicate::wne;
      if (!eq && !ne) return false;
      Table x = ctx.constantTable(true);
      for (unsigned b = 0; b < a.size(); ++b)
        for (unsigned i = 0; i < W; ++i) x[i] &= ~(a[b][i] ^ bb[b][i]);
      if (ne) { Table all = ctx.constantTable(true); for (unsigned i = 0; i < W; ++i) x[i] = all[i] & ~x[i]; }
      r.push_back(x);
    } else {
      return false;
    }
    if (r.size() != w) return false;
    ctx.memo[v] = r; out = r; return true;
  }

  // Exact pairwise exclusivity of the conditions when their joint support
  // is small enough to enumerate. Returns false when not decidable here.
  static bool exclusiveByTruthTable(ArrayRef<Value> conds) {
    TTContext ctx;
    if (!collectSupport(conds, ctx))
      return false;
    SmallVector<Table> tabs;
    for (Value c : conds) {
      Tables t;
      if (!evalTT(c, ctx, t) || t.size() != 1)
        return false;
      tabs.push_back(t[0]);
    }
    unsigned W = ctx.words();
    for (unsigned i = 0; i < tabs.size(); ++i)
      for (unsigned j = i + 1; j < tabs.size(); ++j)
        for (unsigned k = 0; k < W; ++k)
          if (tabs[i][k] & tabs[j][k])
            return false;
    return true;
  }

  void runOnOperation() override {
    if (minChain == 0)
      return;
    hw::HWModuleOp module = getOperation();
    Block *body = module.getBodyBlock();
    unsigned rewritten = 0, links = 0;
    unsigned declined = 0, declinedLinkBits = 0, priorityDecoded = 0;
    // Collect chain heads: a mux whose result is NOT itself the false leg of
    // another mux (so we walk from the outermost link inward).
    llvm::SmallPtrSet<Operation *, 32> innerLinks;
    for (auto mux : body->getOps<comb::MuxOp>())
      if (auto inner = mux.getFalseValue().getDefiningOp<comb::MuxOp>())
        if (inner->hasOneUse())
          innerLinks.insert(inner.getOperation());
    SmallVector<comb::MuxOp> heads;
    for (auto mux : body->getOps<comb::MuxOp>())
      if (!innerLinks.contains(mux.getOperation()) &&
          isa<IntegerType>(mux.getType()))
        heads.push_back(mux);

    for (comb::MuxOp head : heads) {
      // Walk the chain: cond_k / value_k, default at the end.
      SmallVector<Value> conds, vals;
      Value cur = head.getResult();
      comb::MuxOp mux = head;
      while (mux) {
        conds.push_back(mux.getCond());
        vals.push_back(mux.getTrueValue());
        cur = mux.getFalseValue();
        auto next = cur.getDefiningOp<comb::MuxOp>();
        if (next && next->hasOneUse())
          mux = next;
        else
          break;
      }
      if (conds.size() < minChain)
        continue;
      // Exclusivity: every pair.
      SmallVector<DNF> lits(conds.size());
      bool ok = true;
      for (unsigned i = 0; i < conds.size() && ok; ++i)
        ok = flatten(conds[i], lits[i]);
      for (unsigned i = 0; i < conds.size() && ok; ++i)
        for (unsigned j = i + 1; j < conds.size() && ok; ++j)
          ok = exclusive(lits[i], lits[j]);
      // Structural test inconclusive: try the exact small-support one.
      if (!ok && !exclusiveByTruthTable(conds)) {
        unsigned width = cast<IntegerType>(head.getType()).getWidth();
        if (!priorityDecode || width < priorityMinWidth) {
          ++declined;
          declinedLinkBits += conds.size() * width;
          continue;
        }
        // Priority-encode the selects in place: conds[0] is the outermost
        // link and wins.
        OpBuilder pb(head);
        Location ploc = head.getLoc();
        Value trueBit = hw::ConstantOp::create(pb, ploc, APInt(1, 1));
        Value anyPrev;
        SmallVector<Value> sels;
        for (unsigned i = 0; i < conds.size(); ++i) {
          Value sel = conds[i];
          if (anyPrev) {
            Value notPrev =
                pb.createOrFold<comb::XorOp>(ploc, anyPrev, trueBit, true);
            sel = pb.createOrFold<comb::AndOp>(ploc, conds[i], notPrev, true);
            anyPrev = pb.createOrFold<comb::OrOp>(ploc, anyPrev, conds[i], true);
          } else {
            anyPrev = conds[i];
          }
          sels.push_back(sel);
        }
        conds = sels;
        ++priorityDecoded;
      }
      // Rewrite at the head.
      OpBuilder b(head);
      Location loc = head.getLoc();
      unsigned width = cast<IntegerType>(head.getType()).getWidth();
      auto rep = [&](Value c) -> Value {
        return width == 1 ? c
                          : b.createOrFold<comb::ReplicateOp>(
                                loc, b.getIntegerType(width), c);
      };
      SmallVector<Value> terms;
      for (unsigned i = 0; i < conds.size(); ++i)
        terms.push_back(b.createOrFold<comb::AndOp>(loc, rep(conds[i]),
                                                    vals[i], true));
      Value any = conds.size() == 1
                      ? conds[0]
                      : b.createOrFold<comb::OrOp>(loc, conds, true);
      Value trueBit = hw::ConstantOp::create(b, loc, APInt(1, 1));
      Value none = b.createOrFold<comb::XorOp>(loc, any, trueBit, true);
      terms.push_back(b.createOrFold<comb::AndOp>(loc, rep(none), cur, true));
      Value decoded = b.createOrFold<comb::OrOp>(loc, terms, true);
      head.getResult().replaceAllUsesWith(decoded);
      ++rewritten;
      links += conds.size();
    }
    if (rewritten)
      llvm::errs() << "[decode-mux-chains] " << module.getModuleName() << ": "
                   << rewritten << " chain(s), " << links << " link(s)"
                   << (priorityDecoded ? (Twine(", ") + Twine(priorityDecoded) +
                                          " priority-decoded").str()
                                       : std::string())
                   << "\n";
    if (declined)
      llvm::errs() << "[decode-mux-chains] " << module.getModuleName()
                   << ": declined " << declined << " non-exclusive chain(s), "
                   << declinedLinkBits << " link-bit(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<circt::hw::HWModuleOp>>
createDecodeMuxChainsPass() {
  return std::make_unique<DecodeMuxChainsPass>();
}

} // namespace circt_abc
