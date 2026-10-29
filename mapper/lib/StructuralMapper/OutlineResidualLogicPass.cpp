//===- OutlineResidualLogicPass.cpp --------------------------*- C++ -*-===//
//
// See Passes.td's OutlineResidualLogic description for the "why" (AIGER
// export cannot handle a module containing hw.instance ops at all).
//
// Algorithm:
//   0. `hw.module` bodies are GRAPH regions, not SSA-CFG regions: ops may
//      appear in ANY order relative to their data dependencies (a netlist
//      has no inherent "textual" order -- confirmed directly on the real
//      ibex design, e.g. a `comb.concat` textually preceding the
//      `comb.mux` that produces one of its operands, which round-trips
//      fine as-is). Cloning residual ops one at a time via IRMapping
//      needs a genuine dependency order, so sort the block first
//      (`sortTopologically`) -- see its `isOperandReady` callback below
//      for why hw.instance results need special handling here too (a
//      register's clock-enable self-feedback makes the graph LOOK
//      cyclic to a generic sort once register-mapper has run, even
//      though it isn't really: an hw.instance is a sequential boundary).
//   1. "residual" = every op except hw.instance and the terminator.
//   2. live-ins = operands of residual ops defined outside the residual
//      set (block arguments, or hw.instance results).
//   3. live-outs = results of residual ops used outside the residual set
//      (by an hw.instance operand, or by the terminator).
//   4. Build a new private hw.module named `<original>_residual` with
//      live-ins as inputs, live-outs as outputs; clone the residual ops
//      into it (in the now-valid dependency order, via IRMapping) and
//      wire its terminator to the mapped live-out values.
//   5. In the original module, instantiate the new module wired to the
//      original live-in/live-out values, redirect all external uses of
//      each live-out value to the new instance's result. The now-dead
//      residual ops are deliberately left in place, NOT manually erased
//      here (two different manual-erasure attempts both corrupted real
//      IR on the actual ibex design before the true root cause above was
//      found) -- run `--canonicalize` immediately after this pass to
//      remove them via MLIR's own dead-code elimination instead.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/Analysis/TopologicalSortUtils.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/IRMapping.h"
#include "llvm/ADT/SetVector.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct OutlineResidualLogicPass
    : public PassWrapper<OutlineResidualLogicPass,
                          OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(OutlineResidualLogicPass)

  OutlineResidualLogicPass() = default;
  // MLIR clones passes through this copy constructor, and Pass::Option is not
  // copyable, so adding any Option to this pass makes the implicit one
  // deleted. Every Option must be copied here by hand: one omitted from this
  // list silently reverts to its init() default with NO diagnostic. That has
  // already bitten ABCBridgePass for real.
  OutlineResidualLogicPass(const OutlineResidualLogicPass &other)
      : PassWrapper(other) {
    groupCount = other.groupCount.getValue();
    groupIndex = other.groupIndex.getValue();
    moduleSuffix = other.moduleSuffix.getValue();
  }

  StringRef getArgument() const final { return "outline-residual-logic"; }
  StringRef getDescription() const final {
    return "Outline non-instance logic into its own module for AIG/ABC processing";
  }

  // K-way splitting of the residual logic itself (plan PART-1), as opposed to
  // MergeResidualModulesPass's grouping of whole RTL modules. Default
  // `group-count=1` outlines everything into one module -- byte-for-byte the
  // behaviour that existed before this option.
  //
  // At K>1, run K instances of the pass in one pipeline. Each instance
  // outlines a contiguous chunk of whatever residual ops are left and leaves
  // the rest, so instance i takes 1/(K-i) of the remainder and the K chunks
  // come out evenly sized over the original block order. Block order here is
  // roughly topological (the pass sorts before collecting), so a chunk is a
  // band of logic depth rather than a random scatter, which keeps a chunk's
  // internal dependencies inside it.
  //
  // Splitting by op count is deliberate and its limits are known: ABC runtime
  // does not track partition size (AP4's straggler was 357 ops and 49 s while
  // the largest partition, 1,692 ops, was not the slowest), so this balances
  // the wrong quantity. It is still the right first cut, because PART-0
  // measured the QoR cost of partition count as flat above K=2 -- so the
  // usable strategy is many small partitions plus a work-stealing pool, and
  // for that "many, roughly even" is all the partitioner has to deliver.
  Option<unsigned> groupCount{
      *this, "group-count", llvm::cl::init(1),
      llvm::cl::desc("Split the residual logic across this many pass "
                     "instances (default 1 = outline all of it into one "
                     "module, the shipped behaviour).")};
  Option<unsigned> groupIndex{
      *this, "group-index", llvm::cl::init(0),
      llvm::cl::desc("Which chunk this pass instance outlines, 0-based. Only "
                     "meaningful with group-count > 1.")};

  // Suffix for this instance's outlined module name, so a second instance can
  // run after the first without colliding on the symbol.
  //
  // This exists for the feedback cut. Ops left behind to break a combinational
  // cycle stay in the parent, which is NOT a leaf module, so abc-bridge never
  // maps them -- and ExportVerilog then emits them as inline behavioural
  // expressions in the residual instance's port map
  // (`.in2070 (_u_residual_out532 & _u_residual_out531)`), which is not
  // gate-level Verilog and is exactly the kind of thing OpenSTA's reader
  // chokes on. Running the pass a second time with a suffix outlines those
  // leftovers into their own leaf module, so abc-bridge maps them too. The
  // cut ops are acyclic among themselves by construction (removing them is
  // what broke every cycle), so the second instance needs no cut of its own.
  Option<std::string> moduleSuffix{
      *this, "module-suffix", llvm::cl::init(""),
      llvm::cl::desc("Appended to the outlined module's name. Lets a second "
                     "pass instance outline whatever the first left behind.")};

  static bool isKept(Operation *op) { return isa<hw::InstanceOp>(op); }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();

    // Never re-outline this pass's own output. A second instance (see
    // module-suffix) would otherwise pull the first instance's residual body
    // into a further nested module, leaving the two leaves instantiated at
    // different hierarchy levels -- which merge-residual-modules then rejects
    // with "target instantiation sites are not all in one flat scope".
    if (module.getModuleName().contains("_residual"))
      return;

    Block *body = module.getBodyBlock();
    Operation *terminator = body->getTerminator();

    // See this file's header comment (point 0) for why both the sort
    // itself and the custom isOperandReady callback are needed: without
    // the callback, a register's clock-enable self-feedback -- `next =
    // comb.mux(enable, newValue, <register's own Q>)` -- makes the graph
    // look cyclic once register-mapper has replaced the firreg's result
    // with the reconstructed value (confirmed: sortTopologically returned
    // false on the real ibex design specifically because of this), even
    // though it isn't a real combinational cycle -- an hw.instance is a
    // sequential boundary, its output has no same-cycle dependency on its
    // own input. Marking every hw.instance result (and block argument)
    // unconditionally "ready" sidesteps that false cycle: the sort then
    // only needs a valid order among genuine residual-to-residual
    // dependencies, which -- being real combinational logic -- are always
    // acyclic.
    (void)sortTopologically(body, [](Value value, Operation *) {
      Operation *def = value.getDefiningOp();
      return !def || isKept(def);
    });

    // Collect residual ops (everything but kept instances and the terminator).
    SmallVector<Operation *> residualOps;
    for (Operation &op : *body)
      if (&op != terminator && !isKept(&op))
        residualOps.push_back(&op);

    if (residualOps.empty())
      return; // Nothing to outline (e.g. an all-instance module already).

    // Take this instance's chunk. Each instance sees only what earlier ones
    // left behind, so taking the first 1/(K-i) of the remainder yields K
    // evenly sized contiguous chunks of the original order.
    unsigned groups = std::max(1u, groupCount.getValue());
    if (groups > 1) {
      if (groupIndex.getValue() >= groups)
        return;
      unsigned remainingGroups = groups - groupIndex.getValue();
      size_t take = std::max<size_t>(1, residualOps.size() / remainingGroups);
      if (take < residualOps.size())
        residualOps.truncate(take);
    }

    llvm::SmallDenseSet<Operation *> residualSet(residualOps.begin(),
                                                  residualOps.end());

    // Keep the outlined module ACYCLIC by leaving a small feedback vertex set
    // behind in the parent.
    //
    // Why this is needed at all: the residual graph can contain combinational
    // cycles once this pass runs on a FLATTENED module. On real ibex_top one
    // cycle exists, 135 ops, running
    //   alu_operand_b[31] -> ... -> stall_id -> ... -> lsu_req
    //                     -> ... -> alu_op_b_mux_sel -> alu_operand_b
    // i.e. it spans four different RTL modules, which is exactly why outlining
    // per-module never encountered it: each hw.instance is a boundary, so the
    // loop was always cut by the hierarchy. The cycle is present in
    // circt-opt --hw-flatten-modules output itself, before this pass runs --
    // it is not introduced here.
    //
    // The cycle is FALSE (Yosys reports no logic loop on the same RTL and maps
    // the whole design as one network), but proving that needs mux-tree
    // reachability analysis -- Yosys's opt_muxtree -- which CIRCT's
    // canonicalizer has no equivalent of. Rather than prove it, cut it: an op
    // left in the parent still gets mapped through the ordinary live-in /
    // live-out ports, and the loop then routes through the parent exactly as
    // it does in the pre-flatten pipeline today. Semantics are unchanged; only
    // the partition boundary moves.
    //
    // The set is tiny in practice -- 4 ops of 7,080 on ibex_top -- because a
    // greedy max-(in x out)-degree choice inside each SCC cuts every cycle
    // through the busiest node first. Excluded ops are reported so a growing
    // set is visible rather than silent.
    SmallVector<Operation *> feedbackCut;
    while (true) {
      // Index the current residual set and build op -> operand-defining-op
      // edges restricted to it.
      llvm::DenseMap<Operation *, unsigned> index;
      for (auto [i, op] : llvm::enumerate(residualOps))
        index[op] = i;
      unsigned n = residualOps.size();
      SmallVector<SmallVector<unsigned, 4>> succ(n);
      for (unsigned i = 0; i < n; ++i)
        for (Value operand : residualOps[i]->getOperands())
          if (Operation *def = operand.getDefiningOp())
            if (auto it = index.find(def); it != index.end())
              succ[i].push_back(it->second);

      // Iterative Tarjan: find one non-trivial SCC, if any.
      SmallVector<int> idxOf(n, -1), low(n, 0);
      SmallVector<bool> onStack(n, false);
      SmallVector<unsigned> stack;
      SmallVector<unsigned> scc;
      unsigned counter = 0;
      for (unsigned root = 0; root < n && scc.empty(); ++root) {
        if (idxOf[root] != -1)
          continue;
        SmallVector<std::pair<unsigned, unsigned>> work{{root, 0}};
        while (!work.empty() && scc.empty()) {
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
            if (comp.size() > 1)
              scc = std::move(comp);
          }
          unsigned finished = v;
          work.pop_back();
          if (!work.empty())
            low[work.back().first] =
                std::min(low[work.back().first], low[finished]);
        }
      }
      if (scc.empty())
        break; // acyclic

      // Cut the busiest node in this SCC: the one on the most cycles through
      // it, approximated by in-degree x out-degree within the SCC.
      llvm::SmallDenseSet<unsigned> inScc(scc.begin(), scc.end());
      unsigned victim = scc.front();
      uint64_t bestScore = 0;
      for (unsigned v : scc) {
        uint64_t out = llvm::count_if(
            succ[v], [&](unsigned w) { return inScc.contains(w); });
        uint64_t in = 0;
        for (unsigned w : scc)
          in += llvm::count_if(
              succ[w], [&](unsigned x) { return x == v; });
        if (in * out >= bestScore) {
          bestScore = in * out;
          victim = v;
        }
      }
      Operation *cut = residualOps[victim];
      feedbackCut.push_back(cut);
      residualSet.erase(cut);
      llvm::erase(residualOps, cut);
      if (residualOps.empty())
        return;
    }

    if (!feedbackCut.empty())
      llvm::errs() << "[outline-residual-logic] left " << feedbackCut.size()
                   << " op(s) in " << module.getModuleName()
                   << " to break combinational cycle(s) in the residual "
                      "logic\n";

    // Live-ins: operands of residual ops defined outside the residual set,
    // in first-seen order for a stable, deterministic port list.
    //
    // WALK nested regions: a residual op can carry one (an unlowered
    // llhd.process is residual by this pass's definition, registers being
    // instances by now), and a parent-module value referenced ONLY from
    // inside that region must become a port like any other live-in. Values
    // defined inside the residual op's own regions (block args of the
    // process body, ops within it) are excluded -- they travel with the
    // clone. Latent until surrounding IR ordering changed; surfaced as an
    // IsolatedFromAbove violation on OpenC910's ct_ebiu_write_channel.
    llvm::SetVector<Value> liveIns;
    for (Operation *op : residualOps)
      op->walk([&](Operation *nested) {
        for (Value operand : nested->getOperands()) {
          if (Operation *def = operand.getDefiningOp()) {
            if (residualSet.contains(def) || op->isAncestor(def))
              continue;
          } else {
            auto *ownerOp = operand.getParentBlock()->getParentOp();
            if (op == ownerOp || op->isAncestor(ownerOp))
              continue;
          }
          liveIns.insert(operand);
        }
      });

    // Live-outs: results of residual ops used outside the residual set.
    llvm::SetVector<Value> liveOuts;
    for (Operation *op : residualOps)
      for (Value result : op->getResults())
        for (Operation *user : result.getUsers())
          if (!residualSet.contains(user)) {
            liveOuts.insert(result);
            break;
          }

    OpBuilder builder(module);
    // At K=1 this is exactly the historical name, so the default pipeline's
    // output is unchanged; chunks only get a suffix when there is more than
    // one. The suffix must make the name unique across instances, since all K
    // land in the same ModuleOp.
    auto residualName = (module.getModuleName() + "_residual").str();
    if (groups > 1)
      residualName += "_g" + std::to_string(groupIndex.getValue());
    residualName += moduleSuffix.getValue();

    SmallVector<hw::PortInfo> ports;
    for (auto [i, value] : llvm::enumerate(liveIns)) {
      hw::PortInfo port;
      port.name = builder.getStringAttr(("in" + Twine(i)).str());
      port.dir = hw::ModulePort::Direction::Input;
      port.type = value.getType();
      ports.push_back(port);
    }
    for (auto [i, value] : llvm::enumerate(liveOuts)) {
      hw::PortInfo port;
      port.name = builder.getStringAttr(("out" + Twine(i)).str());
      port.dir = hw::ModulePort::Direction::Output;
      port.type = value.getType();
      ports.push_back(port);
    }

    builder.setInsertionPoint(module);
    auto residualModule = hw::HWModuleOp::create(
        builder, module.getLoc(), builder.getStringAttr(residualName), ports);
    // Deliberately left PUBLIC (not private): `--hw-flatten-modules` only
    // inlines private modules, and the native-synth pipeline runs it on
    // the shell hierarchy (see run_circt_native_synth.sh) between this
    // pass and --merge-residual-modules specifically so it collapses every
    // *other* (purely structural, instance-only) level of nesting down to
    // one module while leaving these `_residual` modules untouched as
    // siblings -- inlining them here too would destroy the "zero-instance
    // leaf" property both --merge-residual-modules and abc-bridge rely on
    // to find them. See MergeResidualModulesPass.cpp's file comment.

    // Clone residual ops into the new module, in original order, remapping
    // operands via IRMapping as we go (live-ins map to the new module's
    // block arguments; results of already-cloned residual ops map to their
    // clones' results).
    IRMapping mapping;
    for (auto [i, value] : llvm::enumerate(liveIns))
      mapping.map(value, residualModule.getBodyBlock()->getArgument(i));

    // Clone in two phases rather than relying on body order being
    // def-before-use. A single-phase clone silently leaves any operand whose
    // residual def has not been cloned yet pointing at the PARENT module's
    // value, and hw.HWModuleOp is IsolatedFromAbove, so the verifier then
    // rejects the result with "using value defined outside the region". The
    // whole-body sortTopologically above is best-effort (its result is
    // deliberately ignored) and does not guarantee that order: on a
    // flattened top module, `%9 = comb.concat %338, %333` appears at body
    // line 54 with %338 defined at line 587.
    //
    // Sorting the residual ops instead would NOT be sufficient, because
    // post-flatten the residual-to-residual graph is genuinely cyclic at
    // WORD level: a Kahn sort here left 2,150 of 7,704 ops unresolved on
    // real ibex. Those are false cycles, not combinational loops -- once
    // submodule instances are inlined, a path that left a module and came
    // back is a direct comb edge, and `concat`/`extract` chains let two
    // values each depend on disjoint bit ranges of the other. The design has
    // no bit-level loop, which is why AIGER export downstream still works.
    //
    // Two phases sidestep the ordering question entirely: after phase 1
    // every residual value has a mapping entry, so phase 2 can resolve any
    // operand still pointing outside. hw.module's body is a graph region, so
    // a use appearing above its def is legal in the result.
    OpBuilder residualBuilder(residualModule.getBodyBlock()->getTerminator());
    for (Operation *op : residualOps)
      residualBuilder.clone(*op, mapping);

    for (Operation *op : residualOps)
      if (Operation *cloned = mapping.lookupOrNull(op))
        // Walk NESTED regions too: an outlined op can carry a region (an
        // unlowered llhd.process is 'residual' by this pass's definition,
        // since registers are instances by now), and operands inside it
        // also reference the source module. Patching only the top-level
        // operands leaves those dangling -- latent until surrounding IR
        // reorders defs after the region-carrying op, then an
        // IsolatedFromAbove violation ('llhd.wait op using value defined
        // outside the region').
        cloned->walk([&](Operation *nested) {
          for (OpOperand &operand : nested->getOpOperands())
            if (Value mapped = mapping.lookupOrNull(operand.get()))
              operand.set(mapped);
        });

    SmallVector<Value> mappedLiveOuts;
    for (Value value : liveOuts)
      mappedLiveOuts.push_back(mapping.lookup(value));
    residualModule.getBodyBlock()->getTerminator()->setOperands(
        mappedLiveOuts);

    // Instantiate the outlined module in the original module, wired to the
    // ORIGINAL live-in/live-out values.
    builder.setInsertionPoint(terminator);
    SmallVector<Value> instanceInputs(liveIns.begin(), liveIns.end());
    auto instance = hw::InstanceOp::create(
        builder, module.getLoc(), residualModule, builder.getStringAttr("u_residual"),
        instanceInputs);

    for (auto [i, result] : llvm::enumerate(instance.getResults())) {
      Value value = liveOuts[i];
      value.replaceAllUsesWith(result);
    }

    // Erase the now-dead residual ops, cycle-safely: drop every one's
    // operands FIRST, then erase. After the liveOut redirection above no
    // residual result is used from outside the set, so once they stop
    // referencing each other nothing holds a use and erase always succeeds.
    //
    // This replaces an older "leave them for --canonicalize" approach, which
    // worked only while the residual graph was acyclic. Outlining a
    // FLATTENED module makes it cyclic at word level (see the clone comment
    // above), and mutually-referencing dead ops keep each other alive:
    // canonicalize then aborts with "'comb.mux' op operation destroyed but
    // still has uses". Breaking the references first is the standard
    // teardown for cyclic garbage and is what the two earlier manual
    // attempts recorded below were missing.
    for (Operation *op : residualOps)
      op->dropAllReferences();
    for (Operation *op : residualOps)
      op->erase();

    // Historical note on why this took three attempts.
    // Two different manual approaches (reverse-program-order erase, then a
    // replace-with-placeholder-and-runRegionDCE sweep matching CIRCT's own
    // AIGERRunner.cpp pattern) both produced corrupt IR on the real ibex
    // design -- the first crashed immediately ("operation destroyed but
    // still has uses"), the second silently left a dangling Value that
    // only segfaulted later at print/export time. Root cause not fully
    // chased down (suspected: some residual op's result is redirected by
    // the liveOut replaceAllUsesWith above in a way that interacts badly
    // with erasing that SAME op's operands' producers in this same sweep),
    // but every residual op is now guaranteed unused (Pure, no remaining
    // uses after the redirection above) regardless of the exact
    // mechanism -- so leave them in place and rely on a plain
    // `--canonicalize` run immediately after this pass (already required
    // in scripts/run_circt_native_synth.sh's pipeline) to remove them via
    // MLIR's own battle-tested dead-code elimination instead of
    // reimplementing it here. Do NOT export Verilog on the output of THIS
    // pass without an intervening canonicalize -- the dead ops would
    // otherwise show up as spurious unused wires in the netlist.
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<hw::HWModuleOp>>
createOutlineResidualLogicPass() {
  return std::make_unique<OutlineResidualLogicPass>();
}

} // namespace circt_abc
