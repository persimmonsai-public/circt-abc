//===- LocalizeDynamicRmwPass.cpp - localize dynamic-index RMW injects ----===//
//
// The RTL idiom `mem_n[idx].field = f(mem_n[idx].field)` (a read-modify-write
// at a dynamic index; five per write-back port in cva6's scoreboard) reaches
// the mapper as
//
//   %v = g(hw.array_get %a[%i])            ; struct_extract / struct_inject ...
//   %r = hw.array_inject %a[%i], %v
//
// and hw-aggregate-to-comb lowers it, per entry k, to
// `mux(i == k, g(muxtree(a, i)), a[k])`. `muxtree(a, i) == a[k]` holds only
// under the select -- an observability don't-care that ABC's SAT sweeping
// cannot see; the 2026-09-04 diagnosis showed the scoreboard's `ex` field
// cone mapping 3.8x larger once the structured join lowering stopped keeping
// the un-injected value as a separate mux arm (and even the old flow only
// removed the redundancy by accident of shape).
//
// This pass rewrites the idiom at the aggregate level, before de-aggregation:
//
//   %r = hw.array_create e_{N-1}, ..., e_0
//   e_k = comb.mux (icmp eq %i, k), g_k, (hw.array_get %a[k])
//   g_k = the ops of g that transitively depend on the dynamic read, cloned
//         with `array_get %a[%i]` replaced by `array_get %a[k]`
//
// Ops of g that do not depend on the read stay shared. Identity:
// array_inject gives result[k] = (i == k) ? g(a[i]) : a[k]; under i == k,
// a[i] == a[k]; an out-of-range i leaves the array unchanged in both forms.
// The don't-care is now structural (constant-index reads), so the
// canonicalizer folds the per-entry chains and ABC never sees the tree reads.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/HW/HWTypes.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SetVector.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

/// Ops the per-entry clone may contain: region-free, side-effect-free comb /
/// hw ops that are not instances.
static bool isCloneable(Operation *op) {
  if (op->getNumRegions() != 0 || isa<hw::InstanceOp>(op))
    return false;
  if (!isMemoryEffectFree(op))
    return false;
  StringRef dialect = op->getDialect()->getNamespace();
  return dialect == "comb" || dialect == "hw";
}

/// Registers: their result does not combinationally depend on their
/// operands, so the backward walk stops here.
static bool isSequentialBoundary(Operation *op) {
  return op->getDialect()->getNamespace() == "seq";
}

struct ModuleStats {
  unsigned candidates = 0;   // dynamic injects whose element reads a[i]
  unsigned rewritten = 0;
  unsigned skippedCone = 0;  // dependent cone above max-cone-ops
  unsigned skippedClone = 0; // N x cone above max-clone-ops
  unsigned skippedShape = 0; // dependent cone holds an uncloneable op
};

/// Rewrite one dynamic-index inject if its element depends on a read of the
/// same array at the same index. `touched` collects the original cone ops and
/// roots (dead-op sweep after the module is done).
static bool localizeInject(hw::ArrayInjectOp inject, unsigned maxConeOps,
                           unsigned maxCloneOps, ModuleStats &st,
                           llvm::SetVector<Operation *> &touched) {
  Value arr = inject.getInput();
  Value idx = inject.getIndex();
  Value elem = inject.getElement();
  APInt cst;
  if (matchPattern(idx, m_ConstantInt(&cst)))
    return false;
  auto arrTy = hw::type_dyn_cast<hw::ArrayType>(arr.getType());
  auto idxTy = dyn_cast<IntegerType>(idx.getType());
  if (!arrTy || !idxTy)
    return false;
  uint64_t n = arrTy.getNumElements();
  if (n < 2 || idxTy.getWidth() == 0 || idxTy.getWidth() >= 64 ||
      n > (1ull << idxTy.getWidth()))
    return false;
  Block *blk = inject->getBlock();
  Operation *elemDef = elem.getDefiningOp();
  if (!elemDef || elemDef->getBlock() != blk)
    return false;

  // Roots: reads of the same array SSA value at the same index SSA value.
  llvm::SmallDenseSet<Operation *, 4> roots;
  for (Operation *user : idx.getUsers())
    if (auto get = dyn_cast<hw::ArrayGetOp>(user))
      // (no block-order condition: the body is a graph region, the read may
      // be written after the inject)
      if (get.getInput() == arr && get.getIndex() == idx &&
          get->getBlock() == blk)
        roots.insert(get);
  if (roots.empty())
    return false;

  // Backward closure of the element inside the block (stops at roots and at
  // registers).
  llvm::SetVector<Operation *> back;
  SmallVector<Operation *> stack{elemDef};
  while (!stack.empty()) {
    Operation *op = stack.pop_back_val();
    if (!back.insert(op) || roots.count(op) || isSequentialBoundary(op))
      continue;
    for (Value v : op->getOperands())
      if (Operation *def = v.getDefiningOp())
        if (def->getBlock() == blk)
          stack.push_back(def);
  }
  SmallVector<Operation *> ordered(back.begin(), back.end());
  llvm::sort(ordered, [](Operation *a, Operation *b) {
    return a->isBeforeInBlock(b);
  });

  // Dependent set: roots and everything that transitively consumes one. The
  // module body is a graph region (a use may precede its definition), so
  // block order is not a dependency order: iterate to a fixpoint.
  llvm::SmallDenseSet<Operation *, 32> dep(roots.begin(), roots.end());
  for (bool changed = true; changed;) {
    changed = false;
    for (Operation *op : ordered) {
      if (dep.count(op))
        continue;
      for (Value v : op->getOperands())
        if (Operation *def = v.getDefiningOp())
          if (dep.count(def)) {
            dep.insert(op);
            changed = true;
            break;
          }
    }
  }
  // The inject itself in its own element's cone means a combinational cycle
  // (e.g. through an instance whose output feeds back): not an RMW we can
  // localize.
  if (dep.count(inject)) {
    ++st.skippedShape;
    return false;
  }
  // Cone in topological order (operands before users), so the per-entry
  // clone maps every operand before it is used; a cycle declines.
  SmallVector<Operation *> cone;
  bool uncloneable = false;
  {
    llvm::SmallDenseMap<Operation *, unsigned, 32> pending;
    SmallVector<Operation *> ready;
    for (Operation *op : ordered) {
      if (!dep.count(op) || roots.count(op))
        continue;
      if (!isCloneable(op))
        uncloneable = true;
      unsigned n = 0;
      for (Value v : op->getOperands())
        if (Operation *def = v.getDefiningOp())
          if (dep.count(def) && !roots.count(def))
            ++n;
      pending[op] = n;
      if (!n)
        ready.push_back(op);
    }
    while (!ready.empty()) {
      Operation *op = ready.pop_back_val();
      cone.push_back(op);
      for (Value r : op->getResults())
        for (Operation *user : r.getUsers()) {
          auto it = pending.find(user);
          if (it != pending.end() && it->second && --it->second == 0)
            ready.push_back(user);
        }
    }
    if (cone.size() != pending.size()) {
      ++st.skippedShape;
      return false;
    }
  }
  if (!dep.count(elemDef))
    return false; // the element does not read a[i]: not an RMW
  ++st.candidates;
  if (uncloneable) {
    ++st.skippedShape;
    return false;
  }
  if (cone.size() > maxConeOps) {
    ++st.skippedCone;
    return false;
  }
  if (cone.size() * n > maxCloneOps) {
    ++st.skippedClone;
    return false;
  }

  OpBuilder b(inject);
  Location loc = inject.getLoc();
  Type elemTy = arrTy.getElementType();
  unsigned w = idxTy.getWidth();
  SmallVector<Value> entries(n);
  for (uint64_t k = 0; k < n; ++k) {
    Value ck = hw::ConstantOp::create(b, loc, APInt(w, k));
    Value rk = hw::ArrayGetOp::create(b, loc, elemTy, arr, ck);
    IRMapping map;
    for (Operation *root : roots)
      map.map(root->getResult(0), rk);
    for (Operation *op : cone)
      b.clone(*op, map);
    Value ek = map.lookupOrDefault(elem);
    Value eq = comb::ICmpOp::create(b, loc, comb::ICmpPredicate::eq, idx, ck);
    entries[k] = comb::MuxOp::create(b, loc, eq, ek, rk);
  }
  // hw.array_create's first operand is the highest-index element.
  SmallVector<Value> operands;
  operands.reserve(n);
  for (uint64_t k = n; k-- > 0;)
    operands.push_back(entries[k]);
  Value res = hw::ArrayCreateOp::create(b, loc, inject.getType(), operands);
  inject.getResult().replaceAllUsesWith(res);
  // An earlier rewrite may have put this inject in `touched` (an inject of an
  // inject on the same array is part of that earlier element's cone); it must
  // not stay there once erased, or the dead-op sweep sorts a freed op.
  touched.remove(inject);
  inject.erase();
  for (Operation *op : cone)
    touched.insert(op);
  for (Operation *root : roots)
    touched.insert(root);
  ++st.rewritten;
  return true;
}

struct LocalizeDynamicRmwPass
    : public PassWrapper<LocalizeDynamicRmwPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LocalizeDynamicRmwPass)

  LocalizeDynamicRmwPass() = default;
  LocalizeDynamicRmwPass(const LocalizeDynamicRmwPass &o) : PassWrapper(o) {
    maxConeOps = o.maxConeOps.getValue();
    maxCloneOps = o.maxCloneOps.getValue();
  }

  Option<unsigned> maxConeOps{
      *this, "max-cone-ops", llvm::cl::init(64),
      llvm::cl::desc("skip an inject whose read-dependent element cone has "
                     "more ops than this")};
  Option<unsigned> maxCloneOps{
      *this, "max-clone-ops", llvm::cl::init(4096),
      llvm::cl::desc("skip an inject when (array entries x cone ops) exceeds "
                     "this")};

  StringRef getArgument() const final { return "localize-dynamic-rmw"; }
  StringRef getDescription() const final {
    return "Rewrite array_inject a[i], g(array_get a[i]) into array_create of "
           "per-entry mux(i == k, g(a[k]), a[k]) (constant-index reads)";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  void runOnOperation() override {
    unsigned modulesTouched = 0, totalRewritten = 0, totalSkipped = 0;
    for (auto module : getOperation().getOps<hw::HWModuleOp>()) {
      SmallVector<hw::ArrayInjectOp> injects;
      for (Operation &op : module.getBodyBlock()->getOperations())
        if (auto inj = dyn_cast<hw::ArrayInjectOp>(op))
          injects.push_back(inj);
      if (injects.empty())
        continue;
      ModuleStats st;
      llvm::SetVector<Operation *> touched;
      for (hw::ArrayInjectOp inj : injects)
        localizeInject(inj, maxConeOps, maxCloneOps, st, touched);
      // Sweep the originals the rewrite left dead (reverse block order so a
      // consumer goes before its producers).
      if (!touched.empty()) {
        SmallVector<Operation *> victims(touched.begin(), touched.end());
        llvm::sort(victims, [](Operation *a, Operation *b) {
          return b->isBeforeInBlock(a);
        });
        for (Operation *op : victims)
          if (op->use_empty() && isMemoryEffectFree(op))
            op->erase();
      }
      unsigned skipped = st.skippedCone + st.skippedClone + st.skippedShape;
      if (st.rewritten)
        ++modulesTouched;
      totalRewritten += st.rewritten;
      totalSkipped += skipped;
      if (st.candidates)
        llvm::errs() << "[localize-dynamic-rmw] " << module.getName() << ": "
                     << st.rewritten << " of " << st.candidates
                     << " dynamic RMW inject(s) localized"
                     << (skipped ? " (skipped: " : "")
                     << (st.skippedCone ? (Twine(st.skippedCone) +
                                           " over max-cone-ops ")
                                              .str()
                                        : "")
                     << (st.skippedClone ? (Twine(st.skippedClone) +
                                            " over max-clone-ops ")
                                               .str()
                                         : "")
                     << (st.skippedShape ? (Twine(st.skippedShape) +
                                            " uncloneable cone")
                                               .str()
                                         : "")
                     << (skipped ? ")" : "") << "\n";
    }
    llvm::errs() << "[localize-dynamic-rmw] " << modulesTouched
                 << " module(s) rewritten, " << totalRewritten
                 << " inject(s) localized, " << totalSkipped << " skipped\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createLocalizeDynamicRmwPass() {
  return std::make_unique<LocalizeDynamicRmwPass>();
}

} // namespace circt_abc
