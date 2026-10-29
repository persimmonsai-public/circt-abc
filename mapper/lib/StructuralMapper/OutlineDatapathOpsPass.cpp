//===- OutlineDatapathOpsPass.cpp --------------------------------*- C++ -*-===//
//
// `--outline-datapath-ops[=min-width=N]`: outline every wide arithmetic
// comb op (add, sub, mul, the ordered icmp predicates, shl, shru, shrs) into
// its own single-op module `<module>_dp<K>_<op>_residual`, instantiated as
// `dp<K>`, so that each op
//   * is its own ABC group with stable ports (identity through the merged
//     netlist and the DEF: the instance name and port names survive),
//   * carries a per-instance architecture choice (CombToSynth's
//     `synth.test.arch` attribute is copied onto the outlined op) that a
//     physical loop can rewrite and re-map alone, reusing every other group,
//   * is timed and loaded per instance from a placed design.
//
// PHYS-DP step 1 (2026-09-03). The `_residual` suffix makes the existing
// machinery (merge-residual-modules' leaf-module targeting,
// prune-unused-residual-outputs, the census) treat them as residuals. Runs
// before outline-residual-logic, which then sees the dp instances as
// boundaries. M15's outline-after-flatten result argues small boundaries
// act as depth constraints, so the boundary cost is expected to be benign;
// the ibex area regression is the gate.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/ADT/StringSet.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

static const char *opTag(Operation *op) {
  if (isa<comb::AddOp>(op))
    return "add";
  if (isa<comb::SubOp>(op))
    return "sub";
  if (isa<comb::MulOp>(op))
    return "mul";
  if (isa<comb::ICmpOp>(op))
    return "cmp";
  if (isa<comb::ShlOp>(op))
    return "shl";
  if (isa<comb::ShrUOp>(op))
    return "shru";
  if (isa<comb::ShrSOp>(op))
    return "shrs";
  return nullptr;
}

static bool isOrderedCmp(comb::ICmpOp cmp) {
  switch (cmp.getPredicate()) {
  case comb::ICmpPredicate::slt:
  case comb::ICmpPredicate::sle:
  case comb::ICmpPredicate::sgt:
  case comb::ICmpPredicate::sge:
  case comb::ICmpPredicate::ult:
  case comb::ICmpPredicate::ule:
  case comb::ICmpPredicate::ugt:
  case comb::ICmpPredicate::uge:
    return true;
  default:
    return false;
  }
}

struct OutlineDatapathOpsPass
    : public PassWrapper<OutlineDatapathOpsPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(OutlineDatapathOpsPass)

  OutlineDatapathOpsPass() = default;
  OutlineDatapathOpsPass(const OutlineDatapathOpsPass &o) : PassWrapper(o) {
    minWidth = o.minWidth.getValue();
    idsPath = o.idsPath.getValue();
  }

  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("outline ops whose operand width is at least this")};
  Option<std::string> idsPath{
      *this, "ids", llvm::cl::init(""),
      llvm::cl::desc("file with one dp-annotate op id per line: outline ONLY "
                     "those ops (the PHYS-DP loop's critical set); empty = all")};

  StringRef getArgument() const final { return "outline-datapath-ops"; }
  StringRef getDescription() const final {
    return "Outline each wide arithmetic comb op into its own residual "
           "module (per-op identity, group, and architecture choice)";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  llvm::StringSet<> onlyIds;

  void runOnOperation() override {
    mlir::ModuleOp top = getOperation();
    unsigned total = 0;
    onlyIds.clear();
    if (!idsPath.getValue().empty()) {
      auto buf = llvm::MemoryBuffer::getFile(idsPath.getValue());
      if (!buf) {
        top.emitError("outline-datapath-ops: cannot read ") << idsPath.getValue();
        return signalPassFailure();
      }
      for (auto line : llvm::split(buf.get()->getBuffer(), '\n')) {
        auto t = line.trim();
        if (!t.empty()) onlyIds.insert(t);
      }
    }
    for (auto module : llvm::make_early_inc_range(top.getOps<hw::HWModuleOp>())) {
      if (module.getModuleName().ends_with("_residual"))
        continue;
      // (op, dp-annotate index): the outlined module is named by the op's
      // annotate index so `<m>_dpop<K>_` is the op id `<m>_dp<K>` -- the
      // loop's seeding/budget tools join the two by that K
      SmallVector<std::pair<Operation *, unsigned>> targets;
      unsigned walkIndex = 0; // dp-annotate's per-module op counter
      module.walk([&](Operation *op) {
        const char *tag = opTag(op);
        if (!tag || op->getNumResults() != 1 || op->getNumOperands() < 2)
          return;
        // shifts: outline only variable-amount ones (constant shifts are wiring)
        if (isa<comb::ShlOp, comb::ShrUOp, comb::ShrSOp>(op) &&
            op->getOperand(1).getDefiningOp<hw::ConstantOp>())
          return;
        if (auto cmp = dyn_cast<comb::ICmpOp>(op))
          if (!isOrderedCmp(cmp))
            return;
        auto ty = dyn_cast<IntegerType>(op->getOperand(0).getType());
        if (!ty || ty.getWidth() < minWidth.getValue())
          return;
        // dp-annotate counts every wide op (constant operands included) in
        // this walk order; keep the same numbering so ids line up
        unsigned myIndex = walkIndex++;
        std::string id = (module.getModuleName() + "_dp" + Twine(myIndex)).str();
        if (!onlyIds.empty() && !onlyIds.count(id))
          return;
        // pure cone by construction: a single op. Constant-operand ops
        // (increments, compares against constants) are skipped in the
        // outline-everything mode, but an explicit ids list may name them:
        // a 64-bit counter increment is a real carry chain (cv64a6's perf
        // counters fail timing under the area body).
        if (onlyIds.empty())
          for (Value v : op->getOperands())
            if (v.getDefiningOp<hw::ConstantOp>())
              return;
        targets.push_back({op, myIndex});
      });
      if (targets.empty())
        continue;
      OpBuilder builder(module);
      for (auto [op, k] : targets) {
        const char *tag = opTag(op);
        // "_dpop" is distinctive on purpose: plain "_dp" also occurs in real
        // module names (fpnew's T-Head divsqrt: ct_vfdsu_scalar_dp), and the
        // partitioner's isolate-substr must catch only outlined ops
        std::string name = (module.getModuleName() + "_dpop" + Twine(k) + "_" +
                            tag + "_residual")
                               .str();
        // Constant-aware boundary: an operand that is a concat with constant
        // pieces (a zero-extension: fpnew's 53-bit mantissas extended to 106
        // bits) crosses the boundary as its non-constant pieces only; the
        // constants and the concat are rebuilt inside the module, so ABC sees
        // the constant bits (106x106 multiplier -> 9,522 instead of 13,665
        // cells at the same depth). A direct constant operand is cloned in.
        struct Piece { Value v; bool constant; };
        SmallVector<SmallVector<Piece, 4>, 4> pieces;   // per operand
        SmallVector<hw::PortInfo> ports;
        SmallVector<Value> ins;                          // instance operands
        for (auto [i, v] : llvm::enumerate(op->getOperands())) {
          SmallVector<Piece, 4> ps;
          if (auto cc = v.getDefiningOp<comb::ConcatOp>()) {
            for (Value e : cc.getInputs())
              ps.push_back({e, (bool)e.getDefiningOp<hw::ConstantOp>()});
            if (llvm::none_of(ps, [](const Piece &q) { return q.constant; }))
              ps.assign(1, Piece{v, false});
          } else {
            ps.push_back({v, (bool)v.getDefiningOp<hw::ConstantOp>()});
          }
          for (auto [j, q] : llvm::enumerate(ps)) {
            if (q.constant)
              continue;
            hw::PortInfo p;
            p.name = builder.getStringAttr(
                ps.size() == 1 ? ("a" + Twine(i)).str()
                               : ("a" + Twine(i) + "_" + Twine(j)).str());
            p.dir = hw::ModulePort::Direction::Input;
            p.type = q.v.getType();
            ports.push_back(p);
            ins.push_back(q.v);
          }
          pieces.push_back(std::move(ps));
        }
        {
          hw::PortInfo p;
          p.name = builder.getStringAttr("y");
          p.dir = hw::ModulePort::Direction::Output;
          p.type = op->getResult(0).getType();
          ports.push_back(p);
        }
        builder.setInsertionPoint(module);
        auto dpModule = hw::HWModuleOp::create(
            builder, op->getLoc(), builder.getStringAttr(name), ports);
        IRMapping mapping;
        OpBuilder inner(dpModule.getBodyBlock()->getTerminator());
        unsigned argIdx = 0;
        for (auto [i, v] : llvm::enumerate(op->getOperands())) {
          auto &ps = pieces[i];
          SmallVector<Value> rebuilt;
          for (auto &q : ps) {
            if (q.constant)
              rebuilt.push_back(inner.clone(*q.v.getDefiningOp())->getResult(0));
            else
              rebuilt.push_back(dpModule.getBodyBlock()->getArgument(argIdx++));
          }
          if (ps.size() == 1)
            mapping.map(v, rebuilt[0]);
          else
            mapping.map(v, comb::ConcatOp::create(inner, op->getLoc(), rebuilt)
                               .getResult());
        }
        Operation *cloned = inner.clone(*op, mapping);
        dpModule.getBodyBlock()->getTerminator()->setOperands(
            cloned->getResult(0));
        OpBuilder site(op);
        auto inst = hw::InstanceOp::create(
            site, op->getLoc(), dpModule,
            builder.getStringAttr(("dp" + Twine(k)).str()), ins);
        op->getResult(0).replaceAllUsesWith(inst.getResult(0));
        op->erase();
      }
      total += targets.size();
    }
    if (total)
      llvm::errs() << "[outline-datapath-ops] outlined " << total
                   << " wide arithmetic op(s) into per-op residual modules\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createOutlineDatapathOpsPass() {
  return std::make_unique<OutlineDatapathOpsPass>();
}

} // namespace circt_abc
