//===- SpecializeMuxArmsPass.cpp ------------------------------------------===//
//
// Inside each arm of `comb.mux %c, %a, %b`, rewrite %a's cone under c = 1 and
// %b's cone under c = 0.
//
// WHY.  llhd-remove-control-flow lowers a control-flow join by incoming-edge
// predicates, and an always_comb FSM such as cva6's load_unit
//
//     IDLE: if (accept) begin
//             data_req = 1;
//             if (!data_gnt) state_d = WAIT_GNT; else ...
//
// comes out as
//
//     %idle = icmp eq %state, 0
//     %r    = mux %idle, and(%accept, or(!%gnt, ..., or(%idle, ...))), ...
//
// The OR is constant 1 under its own mux select, so data_req does not depend
// on data_gnt -- but STRUCTURALLY it does, and comb canonicalisation never
// looks at a mux's select from inside its arms. In an SoC built around CVA6
// with an hpdcache, that one structural edge (and the same one into
// translation_req_o) closes a bit-level false combinational loop through
// the hpdcache arbiter (req -> gnt -> core_req_ready -> data_gnt) that a
// commercial flow does not report for the same RTL. The loop survives ABC
// (each partition sees only its half), makes STA break an arbitrary arc,
// and puts the deepest cva6 path around it.
//
// WHAT.  For every mux, specialise each arm under the select's value plus
// what that value implies:
//   c = and(x..) = 1  =>  every x = 1      c = or(x..) = 0  =>  every x = 0
//   c = xor(x, 1)     =>  x = !c
//   c = icmp eq v, K = 1  =>  icmp eq v, K' = (K' == K), icmp ne likewise
//   c = icmp ne v, K = 0  =>  same as eq = 1
// The specialisation walks backward from the arm through comb ops, at most
// `max-depth` levels and `max-nodes` ops per arm, and clones only the ops
// whose result actually changes; the original stays for every other user.
// Function-preserving by construction (a mux arm is only observed when the
// select has that value). Run canonicalize afterwards to fold the constants.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "CombFacts.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {

namespace {

using Facts = CombFacts;

struct SpecializeMuxArmsPass
    : public PassWrapper<SpecializeMuxArmsPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SpecializeMuxArmsPass)

  SpecializeMuxArmsPass() = default;
  SpecializeMuxArmsPass(const SpecializeMuxArmsPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    maxDepth = other.maxDepth.getValue();
    maxNodes = other.maxNodes.getValue();
  }

  StringRef getArgument() const final { return "specialize-mux-arms"; }
  StringRef getDescription() const final {
    return "Rewrite each comb.mux arm under its select's value";
  }


  Option<bool> enable{*this, "enable", llvm::cl::init(false),
                      llvm::cl::desc("Enable the rewrite (OFF by default)")};
  Option<unsigned> maxDepth{
      *this, "max-depth", llvm::cl::init(8),
      llvm::cl::desc("Levels of comb logic searched below each arm")};
  Option<unsigned> maxNodes{
      *this, "max-nodes", llvm::cl::init(512),
      llvm::cl::desc("Ops visited per arm before giving up")};

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  unsigned numArms = 0, numGateInputs = 0, numCloned = 0, numConst = 0;
  static constexpr unsigned maxGateInputs = 16;

  /// Specialise `v` under `facts`; returns `v` itself when nothing changes.
  Value specialize(Value v, const Facts &facts, unsigned depth,
                   unsigned &budget, OpBuilder &b,
                   llvm::DenseMap<Value, Value> &memo) {
    if (auto it = memo.find(v); it != memo.end())
      return it->second;
    if (v.getType().isInteger(1))
      if (auto known = facts.lookup(v)) {
        b.setInsertionPointAfterValue(v);
        Value c = b.create<hw::ConstantOp>(v.getLoc(), APInt(1, *known));
        ++numConst;
        return memo[v] = c;
      }
    Operation *op = v.getDefiningOp();
    if (!op || depth == 0 || budget == 0 || op->getNumResults() != 1 ||
        !isa<comb::AndOp, comb::OrOp, comb::XorOp, comb::MuxOp, comb::ICmpOp,
             comb::ConcatOp, comb::ExtractOp, comb::ReplicateOp>(op))
      return memo[v] = v;
    --budget;
    memo[v] = v; // cycle guard (graph regions)
    SmallVector<Value> ops;
    bool changed = false;
    for (Value x : op->getOperands()) {
      Value y = specialize(x, facts, depth - 1, budget, b, memo);
      changed |= y != x;
      ops.push_back(y);
    }
    if (!changed)
      return v;
    b.setInsertionPoint(op);
    Operation *clone = b.clone(*op);
    clone->setOperands(ops);
    // keep SSA dominance trivially valid: every new operand is defined
    // before `op` (constants are placed right after their source value,
    // clones right before their original), so placing the clone at `op`
    // is safe in an SSA block; graph regions do not care.
    ++numCloned;
    return memo[v] = clone->getResult(0);
  }

  void runOnOperation() override {
    if (!enable)
      return;
    SmallVector<comb::MuxOp> muxes;
    getOperation().walk([&](comb::MuxOp m) { muxes.push_back(m); });
    OpBuilder b(&getContext());
    for (comb::MuxOp m : muxes) {
      Value c = m.getCond();
      for (unsigned arm = 0; arm < 2; ++arm) {
        Facts facts;
        facts.add(c, arm == 0);
        Value v = arm == 0 ? m.getTrueValue() : m.getFalseValue();
        unsigned budget = maxNodes;
        llvm::DenseMap<Value, Value> memo;
        Value nv = specialize(v, facts, maxDepth, budget, b, memo);
        if (nv != v) {
          m->setOperand(arm == 0 ? 1 : 2, nv);
          ++numArms;
        }
      }
    }
    // An i1 AND is a mux too: and(x1..xn) = x_k when every other x_j = 1
    // (and 0 otherwise, whatever x_k is), so x_k may be specialised under
    // {x_j = 1, j != k}; dually an OR's inputs under {x_j = 0}. This is the
    // shape a one-hot-decoded mux takes after decode-mux-chains /
    // canonicalisation: or(and(state == K, ..., or(state == K, ...)), ...).
    SmallVector<Operation *> gates;
    getOperation().walk([&](Operation *op) {
      if (isa<comb::AndOp, comb::OrOp>(op) && op->getResult(0).getType().isInteger(1) &&
          op->getNumOperands() >= 2 && op->getNumOperands() <= maxGateInputs)
        gates.push_back(op);
    });
    for (Operation *g : gates) {
      bool isAnd = isa<comb::AndOp>(g);
      for (unsigned k = 0; k < g->getNumOperands(); ++k) {
        Facts facts;
        for (unsigned j = 0; j < g->getNumOperands(); ++j)
          if (j != k)
            facts.add(g->getOperand(j), isAnd);
        Value v = g->getOperand(k);
        unsigned budget = maxNodes;
        llvm::DenseMap<Value, Value> memo;
        Value nv = specialize(v, facts, maxDepth, budget, b, memo);
        if (nv != v) {
          g->setOperand(k, nv);
          ++numGateInputs;
        }
      }
    }
    if (numArms || numGateInputs)
      llvm::errs() << "[specialize-mux-arms] rewrote " << numArms
                   << " mux arm(s) and " << numGateInputs << " and/or input(s): " << numConst << " value(s) decided, "
                   << numCloned << " op(s) cloned\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createSpecializeMuxArmsPass() {
  return std::make_unique<SpecializeMuxArmsPass>();
}

} // namespace circt_abc
