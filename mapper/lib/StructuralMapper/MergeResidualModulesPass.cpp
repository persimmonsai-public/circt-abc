//===- MergeResidualModulesPass.cpp ------------------------*- C++ -*-===//
//
// abc-bridge invokes one ABC subprocess per zero-instance leaf hw.module
// (see ABCBridgePass::runOnOperation) -- with one `_residual` module per
// original ibex submodule (OutlineResidualLogic runs per hw.module), that
// means ~28 isolated ABC calls on the real design, each optimizing its own
// slice of combinational logic with zero visibility into any other
// module's logic. This is the documented root cause of M1b's QoR gap vs.
// both Yosys (single global ABC call) and M1a (same): no cross-module
// sharing, no global optimization scope.
//
// This pass merges every zero-instance leaf hw.module in the design into
// ONE combined module (and collapses their instantiation sites into a
// single new instance of it), so that a *later* abc-bridge run finds
// exactly one target and invokes ABC exactly once over the whole design's
// residual logic -- matching Yosys/M1a's global-optimization scope.
//
// Precondition: every target's instantiation site must already live in the
// SAME parent block. OutlineResidualLogic's `_residual` modules are each
// instantiated inside their own distinct original submodule (e.g.
// `ibex_alu_residual` inside `ibex_alu`, `ibex_controller_residual` inside
// `ibex_controller`) -- those are different scopes. Getting them into one
// scope needs `--hw-flatten-modules` run on the *shell* hierarchy first
// (see scripts/run_circt_native_synth.sh's pipeline comment) -- which is
// why OutlineResidualLogic deliberately does NOT mark its `_residual`
// modules private: `--hw-flatten-modules` only inlines private modules, so
// leaving them public is what lets that flatten collapse every *other*
// (purely structural, instance-only) level of the hierarchy down to one
// module while leaving the `_residual` leaves as siblings for this pass to
// find and merge, rather than inlining their bodies too and destroying the
// "zero-instance leaf" property abc-bridge and this pass both rely on.
//
// Algorithm, once all targets are confirmed co-located:
//   1. Build one new module whose ports are the concatenation of every
//      target's own ports (all inputs first, in target order, then all
//      outputs, in target order -- same convention OutlineResidualLogic
//      itself uses), name-prefixed by the owning target for readability.
//   2. Clone each target's body into the new module in turn, remapping
//      that target's block arguments to its own slice of the new module's
//      arguments via IRMapping. Each target's body is already
//      topologically valid on its own (OutlineResidualLogic guarantees
//      this); concatenating N independently-valid, mutually-independent
//      sequences (these are leaf modules -- no target's logic depends on
//      another's) needs no re-sorting.
//   3. Replace all N old instantiation sites with one new instance of the
//      merged module, wired from the concatenation of their original
//      operands, and redirect their result uses to the corresponding
//      slice of the new instance's results.
//   4. Erase the old instances and the old (now-unreferenced) target
//      modules.
//
//===----------------------------------------------------------------------===//

#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/ADT/StringMap.h"
#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/IRMapping.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Twine.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {
// AIGER carries bit-level combinational logic only. Two things genuinely
// break its exporter, both observed on OpenC910:
//   * an op outside the `hw`/`comb` dialects -- a leftover `llhd.sig`/`drv`
//     that sig2reg declined to promote, or a `seq.to_clock`
//     ("unhandled operation", then "AIGER export failed").
//   * `hw.aggregate_constant`, which is a `hw` op and so slips past the
//     dialect check, but has no bit-level form.
//
// An aggregate-TYPED value does NOT belong on that list, even though an
// earlier revision of this predicate rejected one. Checked directly:
// `ct_prio_12_residual` carries `hw.bitcast (i4) -> !hw.array<2xi2>` feeding a
// `comb.mux` on the array type, and abc-bridge maps it end to end without
// complaint -- CombToSynth reduces the bitcast away. Rejecting on type alone
// blackboxed 21 instantiations of the ct_prio family that were always
// mappable.
static bool isUnmappableToAiger(hw::HWModuleOp m) {
  bool bad = false;
  m.getBodyBlock()->walk([&](Operation *op) {
    StringRef dialect = op->getName().getDialectNamespace();
    // hw.aggregate_constant used to be rejected here ("has no bit-level
    // form"), and that was true when written -- but CombToSynth has since
    // gained the aggregate lowerings (it marks AggregateConstantOp illegal
    // and converts it), and a direct test maps CVA6's branch_unit_residual
    // (a struct-typed output fed by an aggregate zero) with exit 0. The
    // rejection had re-blackboxed 15 CVA6 residuals whose only offense was
    // a struct-typed module output. Same lesson as M9, second occurrence:
    // an unmeasured conservative guard silently deletes logic.
    if (dialect != "hw" && dialect != "comb") {
      bad = true;
      return WalkResult::interrupt();
    }
    return WalkResult::advance();
  });
  return bad;
}


struct MergeResidualModulesPass
    : public PassWrapper<MergeResidualModulesPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MergeResidualModulesPass)

  MergeResidualModulesPass() = default;
  MergeResidualModulesPass(const MergeResidualModulesPass &other)
      : PassWrapper(other) {
    dedupPassthroughPorts = other.dedupPassthroughPorts.getValue();
    skipUnmappable = other.skipUnmappable.getValue();
    internalizeSelfFed = other.internalizeSelfFed.getValue();
    freeDeadOutputs = other.freeDeadOutputs.getValue();
    isolateSubstr = other.isolateSubstr.getValue();
    isolateEach = other.isolateEach.getValue();
    groupMap = other.groupMap.getValue();
  }

  StringRef getArgument() const final { return "merge-residual-modules"; }
  StringRef getDescription() const final {
    return "Merge every zero-instance leaf hw.module into one, so a "
           "following abc-bridge run maps the whole design's residual "
           "logic in a single ABC call instead of one per module";
  }

  // Post-M3 roadmap item: a per-module `--cse` (already in the pipeline,
  // right before --outline-residual-logic) cannot see across DIFFERENT
  // original hw.modules, so if two or more targets each independently (a)
  // take the SAME external value as one of their own live-ins, AND (b)
  // pass it straight through, unmodified, as one of their own live-outs
  // (OutlineResidualLogicPass creates such a port whenever a residual op
  // -- not yet canonicalized away at outline time -- computes an identity
  // function on a live-in), every such target gets its OWN dedicated
  // output port for what is, after merging, the exact same signal.
  // Confirmed on real ibex_top: dozens of separate buffer cells inside
  // abc_bridge_merged_residual buffering the literal same net (one bit of
  // a register array re-exported through 76 separate output ports; a
  // clock-gate ECK net through 28) -- ABC's own mapper has to insert one
  // buffer per redundant port to give each its own driven net.
  //
  // This dedup is intentionally narrow and identity-only, matching the
  // buffer-dedup CSE fix's own discipline: it collapses an output only
  // when it is (a) a target's own block argument passed straight to its
  // terminator, or (b) a `comb.extract` of a target's own block argument
  // at a specific (low bit, width), and two or more targets share the
  // SAME external driving value for that argument. It does NOT attempt
  // any deeper value-equivalence reasoning (e.g. two targets computing
  // structurally-identical-but-freshly-cloned logic from the same
  // inputs) -- that's a real, larger opportunity but a different, riskier
  // kind of fix, out of scope here.
  //
  // RESULT (measured on real ibex_top, both directly comparable via the same
  // circt-abc-opt invocation with only this flag toggled): a real but SMALL
  // win, removing only a small fraction of the buffers whose count motivated
  // this fix, for a negligible pre-P&R area change. Investigated why the win is
  // this small: the dominant real duplication pattern (e.g. one bit of a bus
  // independently extracted -- via `comb.extract ... from N` -- and re-exported
  // by dozens of DIFFERENT original submodules) turns out NOT to be an
  // exact-identity case at all: each submodule's own instantiation-site operand
  // for "the same logical signal" is a genuinely DIFFERENT MLIR Value even
  // after --hw-flatten-modules (confirmed on real ibex_top: ~56 distinct
  // `<target>_residual_0_inN` arguments independently extract bit 31 of what is
  // only the SAME signal in a broader, non-identity sense) -- closing that gap
  // would need real value-equivalence reasoning across the shell hierarchy,
  // explicitly out of scope for this fix. Correctness: verified mechanistically
  // on a synthetic two-target test exercising both the shared-input and
  // shared-extract-output cases -- after the fix, a downstream `comb.xor` of
  // what were two independently-computed duplicate values folds all the way to
  // `hw.constant false` via canonicalize, directly proving both original
  // consumers were correctly rewired to the one shared result. A full
  // ibex_top-scale SAT equivalence proof was attempted (the same Yosys-miter
  // methodology used for the ABC-shim work) but is not practical here: Yosys's
  // `equiv_make` matches cells by name across the two netlists, and ABC's own
  // internal gate-numbering is not stable between separately-mapped netlists
  // (confirmed: even re-running the identical recipe on the identical input
  // twice produces differently-numbered gates), so name-based cell
  // correspondence between a dedup-enabled and dedup-disabled run of the WHOLE
  // chip crashes Yosys's own equiv_make (a real Yosys/methodology limitation,
  // not evidence of a bug in this fix) -- a real full-chip proof would need a
  // differently-scoped (non-name-based) comparison harness, not attempted here
  // given the small size of the win at stake.
  //
  // REVERTED TO OFF BY DEFAULT (real P&R failure found and root-caused).
  // Running the full pipeline through real place-and-route (OpenROAD) on
  // ibex_top with this flag at its old default (true) hits a DETERMINISTIC
  // routing failure -- reproduced twice from the identical netlist, same
  // failure both times: `[ERROR GRT-0183] Net ..._Y: the 3D maze router ran out
  // of legal paths (heap underflow) - the net is boxed in by local congestion
  // or blockages.` A control run with this option OFF completes the full
  // floorplan/place/cts/route/finish flow cleanly, matching the
  // previously-documented good post-P&R result almost exactly.
  //
  // Root-caused with a mapped-netlist census rather than assumed: the specific
  // failing net traces to a buffer on one of the register file's per-bank
  // clock-gate ECK outputs -- but that exact clock-gate/buffer cluster (same
  // cell count, same 32-DFF fanout) is BYTE-IDENTICAL between the dedup=true
  // and dedup=false mapped networks, ruling out "dedup directly increased this
  // net's fanout" as the cause. A full cell-type census instead found the true
  // effect: dedup=true's abc-bridge target module differs from dedup=false's by
  // a small net cell count, built from HUNDREDS of per-cell-type deltas that
  // mostly cancel (single cell types differing by hundreds of instances) --
  // i.e. removing ~173 ports from abc-bridge's ABC-mapped target changes the
  // shape of ABC's OWN technology-mapping decisions across the WHOLE
  // residual-logic network, not just the ~30 buffers this option's own dedup
  // logic directly touches. The routing failure is an emergent, diffuse
  // consequence of that different (same-area, functionally-equivalent)
  // gate-level shape placing differently in this specific instance -- not a
  // systematic, gateable pattern in this pass's own port-merging logic (there
  // is no fanout/clock-net-specific condition to add here; the actual differing
  // cells are spread across the whole design, not concentrated near the failing
  // net). Given that, and the option's own tiny area upside, OFF is the safe
  // default; ON remains available for anyone who wants the small synthesis-only
  // win and can tolerate re-verifying P&R for their own die/config.
  Option<bool> dedupPassthroughPorts{
      *this, "dedup-passthrough-ports", llvm::cl::init(false),
      llvm::cl::desc("Share one merged input port across targets that take "
                     "the identical external value, and collapse output "
                     "ports that are a pure passthrough (or same-offset "
                     "extract) of a now-shared input into one. OFF by "
                     "default -- small real synthesis-only win on ibex_top "
                     "(a few dozen fewer buffers, negligible area) but a "
                     "confirmed, deterministic "
                     "real place-and-route FAILURE (GRT-0183, boxed-in "
                     "net) traced to ABC's own mapping being sensitive to "
                     "this option's ~173-port reduction in its input "
                     "boundary -- see the comment above this option for "
                     "the full root-cause investigation.")};

  // K-way grouping, for measuring the QoR-vs-partition-count curve (plan
  // PART-0). Default `group-count=1` merges everything into one module --
  // byte-for-byte the behaviour that existed before this option, since the
  // filter and the name suffix are both no-ops at K=1.
  //
  // At K>1 the pass merges only the targets in its own group, so K instances
  // of the pass in one pipeline produce K merged modules, which abc-bridge
  // then maps as K independent partitions. Grouping is by position in a
  // name-sorted target list rather than by `i % K`, so a group is a
  // contiguous run: adjacent targets tend to be related logic, which keeps
  // whatever cross-module structural sharing survives inside a group instead
  // of scattering it. Already-merged modules are skipped by name so the K
  // pass instances do not consume each other's output.
  // Leave modules AIGER cannot represent out of the merge. Merging one in
  // poisons the whole merged module -- abc-bridge then has to skip the single
  // combined target and maps nothing at all. Excluded modules stay separate,
  // so abc-bridge skips just those. Off by default; see abc-bridge's option
  // of the same name.
  // The merged module's port list is the CONCATENATION of its targets'.
  // Where target A's output feeds target B's input, the parent ends up wiring
  // the merged instance's OWN RESULT back into its OWN OPERAND -- so in the
  // AIG that abc-bridge exports, that combinational path is cut into an
  // unrelated (PO, PI) pair. Measured on ibex_top: **2,934 of 5,139 input
  // bits (57%)** are driven by the residual's own outputs.
  //
  // Nothing can cross such a break: structural hashing will not merge two
  // cones computing the same function from opposite sides of it, algebraic
  // extraction cannot find a common divisor across it, `&dch` cannot prove an
  // equivalence that only holds under the parent's wiring, and no mapper cut
  // can span it. It costs LITERALS, not duplicate cells, which is why a
  // duplicate-cell census (a few dozen cells on ibex) wrongly clears it.
  //
  // This internalises them: inside the merged module, replace the block
  // argument for such an input with the value already driving the
  // corresponding output. The port stays in the signature (so the parent's
  // wiring and every downstream splice-by-port keep working) and simply goes
  // unused, which is exactly what lets ABC see one whole cone.
  //
  // Safe by construction: the substitution follows the PARENT'S OWN wiring,
  // which is acyclic in any valid design -- but a cycle check is done anyway
  // and any pair that would close a loop is skipped rather than trusted.
  // See the "MEASURED A NET LOSS" note at the use site: constant-driving dead
  // outputs helps emap's reported area and hurts the finished netlist.
  Option<bool> freeDeadOutputs{
      *this, "free-dead-output-ports", llvm::cl::init(false),
      llvm::cl::desc("Constant-drive merged-instance outputs that no longer "
                     "have a consumer (measured a net loss on ibex: helps the "
                     "raw mapped netlist, hurts the finished one)")};
  Option<bool> internalizeSelfFed{
      *this, "internalize-self-fed-ports", llvm::cl::init(false),
      llvm::cl::desc("Internalise merged-instance inputs that are driven by "
                     "that same instance's outputs, so the residual AIG sees "
                     "whole combinational cones instead of severed ones")};
  Option<bool> skipUnmappable{
      *this, "skip-unmappable", llvm::cl::init(false),
      llvm::cl::desc("Exclude residual modules containing LLHD signals or "
                     "aggregate constants from the merge, so one unmappable "
                     "module does not make the merged target unmappable.")};

  Option<unsigned> groupCount{
      *this, "group-count", llvm::cl::init(1),
      llvm::cl::desc("Number of merged residual modules to produce across "
                     "this many pass instances (default 1 = merge all into "
                     "one, the shipped behaviour).")};
  // PHYS-DP: targets whose module name contains this substring are merged
  // ONLY by the last group (index group-count-1), which merges nothing
  // else -- so outlined datapath ops get their own ABC group and a
  // per-group recipe that does not touch the rest of the design.
  Option<std::string> isolateSubstr{
      *this, "isolate-substr", llvm::cl::init(""),
      llvm::cl::desc("module-name substring routed exclusively to the last group")};
  // isolate-each: every matching module gets its OWN trailing group (the
  // last N of group-count, in name order), so its mapped gates carry the
  // op's identity through the netlist prefix and can be seeded at the op's
  // previous location by an incremental placer.
  Option<bool> isolateEach{
      *this, "isolate-each", llvm::cl::init(false),
      llvm::cl::desc("with isolate-substr: one trailing group per matching module")};
  // group-map: JSON {module name: group index} pinning the non-isolated
  // modules to fixed groups (the baseline's grouping), so an iteration that
  // adds isolated groups beyond the baseline count changes NO other
  // group's membership -- saved per-group netlists stay valid for reuse.
  // Modules absent from the map fall into group 0.
  Option<std::string> groupMap{
      *this, "group-map", llvm::cl::init(""),
      llvm::cl::desc("JSON {module: group index} pinning non-isolated modules")};
  Option<unsigned> groupIndex{
      *this, "group-index", llvm::cl::init(0),
      llvm::cl::desc("Which group this pass instance merges, 0-based. Only "
                     "meaningful with group-count > 1.")};

  // One entry per (target module, instantiation site) PAIR, not per target
  // module: a target instantiated N times (e.g. ibex_cs_registers'
  // replicated per-CSR submodules) is N physically distinct pieces of
  // logic computing different values from different inputs -- each site
  // gets its own independent clone of the target's body, not a single
  // shared one, exactly as if outlining had never happened.
  struct TargetInfo {
    hw::HWModuleOp target;
    hw::InstanceOp site;
    unsigned siteIndex; // this target's Nth site, for unique port naming
  };

  void runOnOperation() override {
    ModuleOp module = getOperation();

    // --- Collect zero-instance leaf modules (same criterion abc-bridge
    // itself uses) and every instantiation site of each. ---
    llvm::DenseMap<StringRef, SmallVector<hw::InstanceOp>> sitesByName;
    module.walk([&](hw::InstanceOp inst) {
      sitesByName[inst.getReferencedModuleName()].push_back(inst);
    });

    SmallVector<TargetInfo> targets;
    SmallVector<hw::HWModuleOp> unmappable;
    for (auto m : module.getOps<hw::HWModuleOp>()) {
      if (!m.getBodyBlock()->getOps<hw::InstanceOp>().empty())
        continue; // not a leaf: has instances of its own
      // Skip this pass's own output, so K instances in one pipeline do not
      // consume each other's merged modules as fresh targets.
      if (m.getModuleName().starts_with("abc_bridge_merged_residual"))
        continue;
      if (skipUnmappable && isUnmappableToAiger(m)) {
        unmappable.push_back(m);
        continue;
      }
      auto sites = sitesByName.lookup(m.getModuleName());
      for (auto [i, site] : llvm::enumerate(sites))
        targets.push_back({m, site, static_cast<unsigned>(i)});
    }

    // Turn unmappable modules into extern declarations here rather than in
    // abc-bridge: in the PARTITIONED path abc-bridge always runs with
    // `target=`, so its own skip path never executes and these modules would
    // survive with their LLHD bodies intact -- which makes ExportVerilog
    // refuse the entire design at the very end of the flow. Doing it at merge
    // time covers both paths. Safe to run in every one of the K pass
    // instances: after the first, these are `hw.module.extern` and no longer
    // appear in `getOps<hw::HWModuleOp>()`.
    for (hw::HWModuleOp m : unmappable) {
      OpBuilder b(m);
      hw::HWModuleExternOp::create(b, m->getLoc(), m.getModuleNameAttr(),
                                   m.getPortList(), StringRef(),
                                   m.getParameters());
      m.getBodyBlock()->walk([](Operation *op) { op->dropAllReferences(); });
      m.erase();
    }

    // Restrict to this instance's group. Sort by name first so the split is
    // deterministic and independent of module order in the input file.
    unsigned groups = std::max(1u, groupCount.getValue());
    const unsigned groupsTotal = groups; // naming uses the configured count
    if (groups > 1) {
      // By value, not const&: hw::HWModuleOp::getModuleName() is non-const,
      // and TargetInfo is two op handles plus an unsigned.
      llvm::stable_sort(targets, [](TargetInfo a, TargetInfo b) {
        StringRef an = a.target.getModuleName(), bn = b.target.getModuleName();
        if (an != bn)
          return an < bn;
        return a.siteIndex < b.siteIndex;
      });
      // Split by MODULE, never by (module, site) pair. A target module
      // instantiated at N sites contributes N entries, and the merge erases
      // the target module once done -- so if two groups each owned some of
      // its sites, the first to run would delete a module the second still
      // references ("Cannot find module definition 'ibex_csr_0_residual'").
      // Bucketing the distinct module names keeps every site of a module in
      // one group.
      SmallVector<StringRef> names;
      for (TargetInfo t : targets)
        if (names.empty() || names.back() != t.target.getModuleName())
          names.push_back(t.target.getModuleName());
      // isolate-substr: the last group takes exactly the matching modules,
      // the other groups split the rest
      bool isolatedDone = false;
      if (!isolateSubstr.getValue().empty()) {
        SmallVector<StringRef> iso, rest;
        for (StringRef n : names)
          (n.contains(isolateSubstr.getValue()) ? iso : rest).push_back(n);
        unsigned nIso = isolateEach.getValue() ? (unsigned)iso.size() : 1u;
        if (nIso >= groups) nIso = groups - 1; // keep at least one group for the rest
        bool trailing = groupIndex.getValue() + nIso >= groups;
        if (trailing) {
          llvm::SmallDenseSet<StringRef> mine;
          if (isolateEach.getValue()) {
            unsigned k = groupIndex.getValue() - (groups - nIso); // 0..nIso-1
            if (k < iso.size()) mine.insert(iso[k]);
          } else {
            mine.insert(iso.begin(), iso.end());
          }
          SmallVector<TargetInfo> selected;
          for (TargetInfo t : targets)
            if (mine.contains(t.target.getModuleName()))
              selected.push_back(t);
          targets = std::move(selected);
          isolatedDone = true;
        } else {
          names = std::move(rest);
          groups -= nIso; // the remaining groups share the non-isolated modules
        }
      }
      if (!isolatedDone && !groupMap.getValue().empty()) {
        // pinned grouping: this instance takes exactly the modules mapped
        // to its index (unmapped ones go to group 0)
        auto buf = llvm::MemoryBuffer::getFile(groupMap.getValue());
        if (!buf) { module.emitError("merge-residual-modules: cannot read group-map"); return signalPassFailure(); }
        auto parsed = llvm::json::parse(buf.get()->getBuffer());
        if (!parsed || !parsed->getAsObject()) { module.emitError("merge-residual-modules: group-map is not a JSON object"); return signalPassFailure(); }
        llvm::StringMap<unsigned> pin;
        for (auto &kv : *parsed->getAsObject())
          if (auto v = kv.second.getAsInteger()) pin[kv.first.str()] = (unsigned)*v;
        llvm::SmallDenseSet<StringRef> mine;
        for (StringRef n : names) {
          auto it = pin.find(n);
          unsigned idx = it == pin.end() ? 0u : it->second;
          if (idx == groupIndex.getValue()) mine.insert(n);
        }
        SmallVector<TargetInfo> selected;
        for (TargetInfo t : targets)
          if (mine.contains(t.target.getModuleName()))
            selected.push_back(t);
        targets = std::move(selected);
        isolatedDone = true;
      }
      if (!isolatedDone) {
      // Each instance only ever sees what earlier ones left behind -- they
      // erase the modules they merge -- so a fixed [i*n/K, (i+1)*n/K) slice
      // of the CURRENT list silently drops modules: on ibex, groups took
      // 6+5+4+3 of 27 and nine residual modules were never mapped at all.
      // Taking the first 1/(K-i) of the remainder instead covers every module
      // exactly once.
      if (groupIndex.getValue() >= groups)
        return;
      size_t n = names.size();
      unsigned remainingGroups = groups - groupIndex.getValue();
      size_t take = n / remainingGroups;
      if (take == 0)
        take = n; // fewer modules left than groups: this one takes the rest
      llvm::SmallDenseSet<StringRef> mine(names.begin(), names.begin() + take);
      SmallVector<TargetInfo> selected;
      for (TargetInfo t : targets)
        if (mine.contains(t.target.getModuleName()))
          selected.push_back(t);
      targets = std::move(selected);
      }
    }

    // A lone target is normally left as it is; an isolated group may hold a
    // single outlined datapath op, and a group-map (depth-class) group may
    // hold a single module, and both must still become the partitioner's
    // abc_bridge_merged_residual_gN module -- otherwise that module is never
    // handed to ABC (found 2026-10-12: aes_residual alone in its depth band
    // reached the netlist as an unmapped module).
    if (targets.empty() ||
        (targets.size() == 1 && isolateSubstr.getValue().empty() &&
         groupMap.getValue().empty()))
      return; // nothing to merge

    Block *commonParent = targets.front().site->getBlock();
    for (auto &info : targets) {
      if (info.site->getBlock() != commonParent) {
        module.emitError(
            "merge-residual-modules: target instantiation sites are not "
            "all in one flat scope -- run --hw-flatten-modules on the "
            "shell hierarchy before this pass (see "
            "scripts/run_circt_native_synth.sh's pipeline comment)");
        return signalPassFailure();
      }
    }

    MLIRContext *ctx = &getContext();
    OpBuilder builder(ctx);
    bool dedup = dedupPassthroughPorts.getValue();
    unsigned rawPortCount = 0;

    // --- 1. Build the merged module's INPUT ports, in site order. When
    // dedup is enabled, two targets whose corresponding instantiation-site
    // operand is the literal SAME Value share one merged input port
    // instead of getting one each.
    llvm::DenseMap<Value, unsigned> externalValueToMergedArg;
    SmallVector<hw::PortInfo> mergedPorts;
    SmallVector<Value> mergedOperands; // one per DISTINCT input port
    SmallVector<SmallVector<unsigned>> targetInputMergedArg(targets.size());

    for (auto [targetIdx, info] : llvm::enumerate(targets)) {
      unsigned localInputIdx = 0;
      for (hw::PortInfo port : info.target.getPortList()) {
        if (!port.isInput())
          continue;
        ++rawPortCount;
        Value external = info.site.getOperand(localInputIdx);
        if (dedup) {
          auto it = externalValueToMergedArg.find(external);
          if (it != externalValueToMergedArg.end()) {
            targetInputMergedArg[targetIdx].push_back(it->second);
            ++localInputIdx;
            continue;
          }
        }
        unsigned mergedArgIdx = mergedPorts.size();
        hw::PortInfo p = port;
        p.name = builder.getStringAttr((info.target.getModuleName() + "_" +
                                         Twine(info.siteIndex) + "_" +
                                         port.getName())
                                            .str());
        mergedPorts.push_back(p);
        mergedOperands.push_back(external);
        if (dedup)
          externalValueToMergedArg[external] = mergedArgIdx;
        targetInputMergedArg[targetIdx].push_back(mergedArgIdx);
        ++localInputIdx;
      }
    }

    // --- 2. Build the merged module's OUTPUT ports. Two trivial,
    // exact-identity shapes reuse a previously-allocated output port for
    // the same shared merged argument, instead of getting a dedicated new
    // one, when dedup is enabled:
    //   (a) a pure passthrough -- the terminator operand IS one of the
    //       target's own block arguments, unmodified;
    //   (b) a `comb.extract` of one of the target's own block arguments
    //       with a specific (low bit, width) -- two such extracts with
    //       the identical (shared argument, low bit, width) triple
    //       compute the definitionally identical value, not merely a
    //       structurally-similar one (this is what real ibex_top's
    //       dominant duplication pattern turned out to be: many different
    //       original submodules each independently extracting the SAME
    //       single bit of a shared bus as one of their own outputs,
    //       confirmed by tracing a real 76-copy buffer cluster back to 76
    //       distinct `comb.extract ... from 31` ops on what dedup-of-
    //       inputs above already unified into one shared merged
    //       argument).
    // Every other (genuinely computed) output still gets its own
    // dedicated new port, exactly as before -- no reasoning beyond exact
    // structural identity of these two trivial op shapes is attempted.
    llvm::DenseMap<uint64_t, unsigned> passthroughOutputForKey;
    SmallVector<SmallVector<unsigned>> targetOutputMergedResult(targets.size());
    unsigned numOutputPorts = 0;
    constexpr uint64_t kWholeSentinel = 0xFFFFu;

    for (auto [targetIdx, info] : llvm::enumerate(targets)) {
      Block *targetBody = info.target.getBodyBlock();
      Operation *terminator = targetBody->getTerminator();
      unsigned localOutputIdx = 0;
      for (hw::PortInfo port : info.target.getPortList()) {
        if (!port.isOutput())
          continue;
        ++rawPortCount;
        Value termOperand = terminator->getOperand(localOutputIdx);
        bool matched = false;
        uint64_t key = 0;
        if (dedup) {
          Value base = termOperand;
          uint64_t encLow = kWholeSentinel, encWidth = kWholeSentinel;
          if (auto extractOp = termOperand.getDefiningOp<comb::ExtractOp>()) {
            base = extractOp.getInput();
            encLow = extractOp.getLowBit();
            encWidth = cast<IntegerType>(extractOp.getType()).getWidth();
          }
          if (auto blockArg = dyn_cast<BlockArgument>(base))
            if (blockArg.getOwner() == targetBody) {
              unsigned mergedArgIdx =
                  targetInputMergedArg[targetIdx][blockArg.getArgNumber()];
              key = (uint64_t(mergedArgIdx) << 32) | (encLow << 16) | encWidth;
              matched = true;
            }
        }
        if (matched) {
          auto it = passthroughOutputForKey.find(key);
          if (it != passthroughOutputForKey.end()) {
            targetOutputMergedResult[targetIdx].push_back(it->second);
            ++localOutputIdx;
            continue;
          }
        }
        unsigned mergedOutIdx = numOutputPorts++;
        if (matched)
          passthroughOutputForKey[key] = mergedOutIdx;
        hw::PortInfo p = port;
        p.name = builder.getStringAttr((info.target.getModuleName() + "_" +
                                         Twine(info.siteIndex) + "_" +
                                         port.getName())
                                            .str());
        mergedPorts.push_back(p);
        targetOutputMergedResult[targetIdx].push_back(mergedOutIdx);
        ++localOutputIdx;
      }
    }

    // At K=1 this is exactly the historical name, so the default pipeline's
    // output is unchanged; groups only get a suffix when there is more than
    // one of them.
    std::string mergedName = "abc_bridge_merged_residual";
    if (groupsTotal > 1)
      mergedName += "_g" + std::to_string(groupIndex.getValue());

    builder.setInsertionPointToStart(module.getBody());
    auto mergedModule = hw::HWModuleOp::create(
        builder, module.getLoc(), builder.getStringAttr(mergedName),
        mergedPorts);
    mergedModule.setPrivate();

    // --- 3. Clone each target's body into the merged module, remapping
    // block arguments through the (possibly-shared) merged argument
    // indices computed above. ---
    OpBuilder bodyBuilder = OpBuilder::atBlockBegin(mergedModule.getBodyBlock());
    SmallVector<Value> mergedOutputs(numOutputPorts);
    for (auto [targetIdx, info] : llvm::enumerate(targets)) {
      Block *targetBody = info.target.getBodyBlock();
      IRMapping mapping;
      for (auto [i, arg] : llvm::enumerate(targetBody->getArguments()))
        mapping.map(arg, mergedModule.getBodyBlock()->getArgument(
                              targetInputMergedArg[targetIdx][i]));

      Operation *terminator = targetBody->getTerminator();
      // Two-phase clone, for the same reason OutlineResidualLogicPass and
      // ABCBridge do it: hw.module bodies are graph regions with no
      // guaranteed def-before-use order, and a single-phase clone silently
      // leaves an operand whose def has not been cloned yet pointing at the
      // SOURCE module's value. The merged module is IsolatedFromAbove, so
      // that either fails the verifier or -- because the source module is
      // erased right after -- aborts with "operation destroyed but still has
      // uses". Not hypothetical: it fires on the real ibex design as soon as
      // outlining runs post-flatten, whose output preserves the flat top's
      // original (unordered) op order.
      SmallVector<Operation *> targetOps;
      for (Operation &op : *targetBody)
        if (&op != terminator)
          targetOps.push_back(&op);

      for (Operation *op : targetOps)
        bodyBuilder.clone(*op, mapping);

      for (Operation *op : targetOps)
        if (Operation *cloned = mapping.lookupOrNull(op))
          // Nested regions too -- see OutlineResidualLogicPass.
          cloned->walk([&](Operation *nested) {
            for (OpOperand &operand : nested->getOpOperands())
              if (Value mapped = mapping.lookupOrNull(operand.get()))
                operand.set(mapped);
          });

      for (auto [j, v] : llvm::enumerate(terminator->getOperands()))
        mergedOutputs[targetOutputMergedResult[targetIdx][j]] =
            mapping.lookup(v);
    }
    mergedModule.getBodyBlock()->getTerminator()->setOperands(mergedOutputs);

    // --- 4. Replace the N old instances with one new instance. ---
    OpBuilder siteBuilder(ctx);
    siteBuilder.setInsertionPoint(targets.front().site);
    auto mergedInstance = hw::InstanceOp::create(
        siteBuilder, module.getLoc(), mergedModule,
        siteBuilder.getStringAttr("u_" + mergedName), mergedOperands);

    // --- 5. Redirect uses via the per-target/per-output-index -> merged
    // result-index map computed above (NOT a running per-target offset --
    // shared passthrough outputs make target-to-target result counts
    // non-uniform), then erase the old instances and modules (each target
    // module def erased only once, even if it had multiple sites/entries
    // above). ---
    llvm::SmallPtrSet<Operation *, 8> erasedTargets;
    for (auto [targetIdx, info] : llvm::enumerate(targets)) {
      for (auto [j, result] : llvm::enumerate(info.site.getResults()))
        result.replaceAllUsesWith(
            mergedInstance.getResult(targetOutputMergedResult[targetIdx][j]));
      info.site.erase();
    }
    for (auto &info : targets)
      if (erasedTargets.insert(info.target).second)
        info.target.erase();

    // --- 6. Internalise inputs fed by our own outputs. Only visible HERE:
    // before step 5 the operands still pointed at the OLD instances. ---
    if (internalizeSelfFed) {
      Block *body = mergedModule.getBodyBlock();
      Operation *term = body->getTerminator();
      unsigned done = 0, skippedLoop = 0, skippedSelf = 0;
      for (unsigned p = 0, e = mergedInstance.getNumOperands(); p < e; ++p) {
        auto res = dyn_cast<OpResult>(mergedInstance.getOperand(p));
        if (!res || res.getOwner() != mergedInstance.getOperation())
          continue;
        BlockArgument arg = body->getArgument(p);
        Value driver = term->getOperand(res.getResultNumber());
        if (driver == arg) { ++skippedSelf; continue; }

        // Would substituting close a combinational loop? Walk back from the
        // driver; if we reach this block argument, skip the pair.
        bool loops = false;
        SmallVector<Value, 32> work{driver};
        llvm::SmallPtrSet<Value, 32> seen;
        while (!work.empty() && !loops) {
          Value v = work.pop_back_val();
          if (!seen.insert(v).second)
            continue;
          if (v == arg) { loops = true; break; }
          if (Operation *def = v.getDefiningOp())
            for (Value in : def->getOperands())
              work.push_back(in);
        }
        if (loops) { ++skippedLoop; continue; }

        arg.replaceAllUsesWith(driver);
        // NB: the parent's wiring is deliberately LEFT INTACT. Breaking the
        // self-loop (feeding the port a constant so the output result looks
        // dead) was tried: `--canonicalize` still does not remove the now-dead
        // output ports (out stays 1,322), and it perturbs the alias pass below
        // by making many operands share one constant. No benefit, so the port
        // keeps its original driver and simply goes unused inside.
        ++done;
      }

      // Third case, and the natural completion of the first: once an input is
      // internalised, the output that was feeding it may have no consumer left
      // in the parent at all -- it is computed, exported, and wired straight
      // back into a port nobody reads. On ibex that is 187 results / 2,049
      // bits. It is still a PO of the AIG, which forces the mapper to
      // materialise that net as a cell output instead of absorbing the driver
      // into its consumers.
      //
      // Drive those outputs with a constant. The port stays (splice-by-port
      // again), the internal consumers still use the real driver -- only the
      // artificial "must exist as an output" constraint goes away. An output
      // is eligible only if EVERY use of the instance result is as an operand
      // of this same instance, i.e. nobody outside reads it.
      // MEASURED A NET LOSS -- off unless freeDeadOutputs is set. Constant-
      // driving a dead output does NOT remove the port: the parent still wires
      // it, so ExportVerilog has to materialise ~2,049 constant drivers and
      // buffers. emap's own reported area drops (its `area=` is the RAW
      // mapped .v) while the FINISHED netlist grows.
      // To make this pay, the port itself must be removed from the module
      // signature and the instance -- not attempted here.
      unsigned deadOuts = 0;
      if (freeDeadOutputs) {
        llvm::DenseSet<Value> selfOperands;
        for (Value v : mergedInstance.getOperands())
          selfOperands.insert(v);
        SmallVector<Value> outs(term->getOperands().begin(),
                                term->getOperands().end());
        OpBuilder cb(ctx);
        cb.setInsertionPointToStart(body);
        for (auto [q, r] : llvm::enumerate(mergedInstance.getResults())) {
          if (!selfOperands.contains(r))
            continue;
          bool onlySelf = true;
          for (OpOperand &u : r.getUses())
            if (u.getOwner() != mergedInstance.getOperation()) {
              onlySelf = false;
              break;
            }
          if (!onlySelf)
            continue;
          auto ty = dyn_cast<IntegerType>(outs[q].getType());
          if (!ty)
            continue;
          outs[q] = hw::ConstantOp::create(cb, mergedModule.getLoc(),
                                           APInt(ty.getWidth(), 0));
          ++deadOuts;
        }
        if (deadOuts)
          term->setOperands(outs);
      }
      llvm::errs() << "[merge-residual-modules] freed " << deadOuts
                   << " dead output port(s)\n";

      // Same identity argument, second case: two input ports driven by the
      // SAME parent value. Those are not severed paths but ALIASES -- the
      // optimiser sees one signal as two independent variables, so it can
      // share nothing between logic built on them. On ibex: 115 ports /
      // 950 bits. Unify them onto the first occurrence.
      llvm::DenseMap<Value, unsigned> firstSeen;
      unsigned aliased = 0;
      for (unsigned p = 0, e = mergedInstance.getNumOperands(); p < e; ++p) {
        Value v = mergedInstance.getOperand(p);
        auto it = firstSeen.find(v);
        if (it == firstSeen.end()) {
          firstSeen[v] = p;
          continue;
        }
        BlockArgument dup = body->getArgument(p);
        BlockArgument rep = body->getArgument(it->second);
        if (dup == rep || dup.getType() != rep.getType())
          continue;
        dup.replaceAllUsesWith(rep);
        ++aliased;
      }

      llvm::errs() << "[merge-residual-modules] internalised " << done
                   << " self-fed port(s)"
                   << " (skipped " << skippedSelf << " passthrough, "
                   << skippedLoop << " would-loop); unified " << aliased
                   << " aliased port(s)\n";
    }

    llvm::errs() << "[merge-residual-modules] merged " << targets.size()
                 << " zero-instance module(s) into one (" << mergedPorts.size()
                 << " ports, " << rawPortCount << " before dedup)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMergeResidualModulesPass() {
  return std::make_unique<MergeResidualModulesPass>();
}

} // namespace circt_abc
