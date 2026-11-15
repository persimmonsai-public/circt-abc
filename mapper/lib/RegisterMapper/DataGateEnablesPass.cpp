//===- DataGateEnablesPass.cpp ----------------------------------*- C++ -*-===//
//
// Data-following clock gating for named register slices (opt-in, off unless
// `slices=` is given). See Passes.td's DataGateEnables description.
//
// For a seq.firreg whose `next` is the plain hold shape
// `mux(c, load, q)` / `mux(c, q, load)`, and a named slice [lo, hi] of it, the
// pass rewrites the load so that the slice also holds when loading would not
// change it:
//
//   mode=zero : slice := (load_s == 0 && q_s == 0) ? q_s : load_s
//   mode=xor  : slice := (load_s == q_s)           ? q_s : load_s
//
// Both are the identity function of the register (the held value equals the
// loaded one in the cases chosen), so the rewrite is combinationally
// equivalent. What it changes is the SHAPE clock-gate-inference reads: the
// slice bits' enable becomes `c && !hold_s`, a different function from the
// other bits', so bit-level-enables=true (the flow default) puts the slice on
// its own gate whose enable also depends on the data. The data compare ends
// at that gate's enable pin: its setup path starts at the register's D cone.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct SliceSpec {
  std::string module; // empty: any module
  std::string reg;
  unsigned lo = 0, hi = 0;
  bool xorMode = false;
};

// "[module/]reg@lo-hi[@zero|xor]"
static bool parseSpec(StringRef entry, SliceSpec &out) {
  SmallVector<StringRef, 3> f;
  entry.split(f, '@');
  if (f.size() < 2 || f.size() > 3)
    return false;
  StringRef nm = f[0];
  auto slash = nm.find('/');
  if (slash != StringRef::npos) {
    out.module = nm.substr(0, slash).str();
    nm = nm.substr(slash + 1);
  }
  out.reg = nm.str();
  auto dash = f[1].find('-');
  if (dash == StringRef::npos)
    return false;
  if (f[1].substr(0, dash).getAsInteger(10, out.lo) ||
      f[1].substr(dash + 1).getAsInteger(10, out.hi) || out.hi < out.lo)
    return false;
  if (f.size() == 3) {
    if (f[2] == "xor")
      out.xorMode = true;
    else if (f[2] != "zero")
      return false;
  }
  return !out.reg.empty();
}

struct DataGateEnablesPass
    : public PassWrapper<DataGateEnablesPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(DataGateEnablesPass)

  DataGateEnablesPass() = default;
  DataGateEnablesPass(const DataGateEnablesPass &other) : PassWrapper(other) {
    slices = other.slices;
  }

  StringRef getArgument() const final { return "data-gate-enables"; }
  StringRef getDescription() const final {
    return "Make named register slices hold when their load would not change "
           "them, so clock-gate-inference gates them by data";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  ListOption<std::string> slices{
      *this, "slices",
      llvm::cl::desc("Comma-separated [module/]reg@lo-hi[@zero|xor] slices of "
                     "seq.firreg registers to gate by data (default mode "
                     "zero). Empty: the pass does nothing.")};

  void runOnOperation() override {
    SmallVector<SliceSpec, 4> specs;
    for (const std::string &e : slices) {
      SliceSpec s;
      if (!parseSpec(e, s)) {
        getOperation().emitError("data-gate-enables: bad slice '") << e << "'";
        return signalPassFailure();
      }
      specs.push_back(s);
    }
    if (specs.empty())
      return;
    unsigned done = 0;
    SmallVector<bool, 4> used(specs.size(), false);
    // a slice whose register was found but not rewritten (the reason was
    // printed when it was skipped)
    SmallVector<bool, 4> found(specs.size(), false);
    for (auto module : getOperation().getOps<hw::HWModuleOp>()) {
      SmallVector<seq::FirRegOp, 8> regs;
      for (auto r : module.getBodyBlock()->getOps<seq::FirRegOp>())
        regs.push_back(r);
      for (seq::FirRegOp reg : regs) {
        SmallVector<unsigned, 4> mine;
        for (auto [i, s] : llvm::enumerate(specs))
          if (s.reg == reg.getName() &&
              (s.module.empty() || s.module == module.getModuleName()))
            mine.push_back(i);
        if (mine.empty())
          continue;
        for (unsigned i : mine)
          found[i] = true;
        Value q = reg.getData();
        unsigned width = q.getType().getIntOrFloatBitWidth();
        auto mux = reg.getNext().getDefiningOp<comb::MuxOp>();
        Value load, cond;
        bool holdOnTrue = false;
        if (mux && mux.getFalseValue() == q && mux.getTrueValue() != q) {
          load = mux.getTrueValue();
          cond = mux.getCond();
        } else if (mux && mux.getTrueValue() == q && mux.getFalseValue() != q) {
          load = mux.getFalseValue();
          cond = mux.getCond();
          holdOnTrue = true;
        } else {
          llvm::errs() << "[data-gate-enables] " << module.getModuleName()
                       << "/" << reg.getName()
                       << ": next is not a plain hold mux; skipped\n";
          continue;
        }
        // Sorted, non-overlapping, inside the register.
        llvm::sort(mine, [&](unsigned a, unsigned b) {
          return specs[a].lo < specs[b].lo;
        });
        bool ok = true;
        unsigned prevHi = 0;
        for (auto [k, i] : llvm::enumerate(mine)) {
          if (specs[i].hi >= width || (k && specs[i].lo <= prevHi))
            ok = false;
          prevHi = specs[i].hi;
        }
        if (!ok) {
          llvm::errs() << "[data-gate-enables] " << reg.getName()
                       << ": slices overlap or exceed width " << width << "\n";
          continue;
        }
        OpBuilder b(reg);
        Location loc = reg.getLoc();
        auto slice = [&](Value v, unsigned lo, unsigned w) -> Value {
          if (lo == 0 && w == width)
            return v;
          return comb::ExtractOp::create(b, loc, b.getIntegerType(w), v, lo);
        };
        SmallVector<Value, 8> pieces; // MSB first
        unsigned pos = width;
        for (auto it = mine.rbegin(); it != mine.rend(); ++it) {
          const SliceSpec &s = specs[*it];
          unsigned w = s.hi - s.lo + 1;
          if (pos > s.hi + 1)
            pieces.push_back(slice(load, s.hi + 1, pos - (s.hi + 1)));
          Value ls = slice(load, s.lo, w), qs = slice(q, s.lo, w);
          Value zero = hw::ConstantOp::create(b, loc, APInt(w, 0));
          Value same;
          if (s.xorMode) {
            same = comb::ICmpOp::create(b, loc, comb::ICmpPredicate::eq, ls, qs);
          } else {
            Value lz =
                comb::ICmpOp::create(b, loc, comb::ICmpPredicate::eq, ls, zero);
            Value qz =
                comb::ICmpOp::create(b, loc, comb::ICmpPredicate::eq, qs, zero);
            SmallVector<Value, 2> both{lz, qz};
            same = comb::AndOp::create(b, loc, both, false);
          }
          pieces.push_back(comb::MuxOp::create(b, loc, same, qs, ls));
          pos = s.lo;
          used[*it] = true;
        }
        if (pos > 0)
          pieces.push_back(slice(load, 0, pos));
        Value newLoad = pieces.size() == 1
                            ? pieces[0]
                            : comb::ConcatOp::create(b, loc, pieces);
        Value newNext = holdOnTrue
                            ? comb::MuxOp::create(b, loc, cond, q, newLoad)
                            : comb::MuxOp::create(b, loc, cond, newLoad, q);
        reg.getNextMutable().assign(newNext);
        ++done;
        llvm::errs() << "[data-gate-enables] " << module.getModuleName() << "/"
                     << reg.getName() << ": " << mine.size()
                     << " slice(s) rewritten\n";
      }
    }
    for (auto [i, s] : llvm::enumerate(specs))
      if (!used[i])
        llvm::errs() << "[data-gate-enables] slice " << s.reg << "@" << s.lo
                     << "-" << s.hi
                     << (found[i] ? " found but skipped (reason above)\n"
                                  : " matched no register\n");
    llvm::errs() << "[data-gate-enables] " << done << " register(s) rewritten\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createDataGateEnablesPass() {
  return std::make_unique<DataGateEnablesPass>();
}

} // namespace circt_abc
