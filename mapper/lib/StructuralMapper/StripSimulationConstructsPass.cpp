//===- StripSimulationConstructsPass.cpp ----------------------*- C++ -*-===//
//
// See Passes.td's StripSimulationConstructs description for the "why". Short
// version: SVA assertions, `$fopen`/`$display` and `initial` blocks survive
// elaboration as `llhd.process` ops that no LLHD lowering pass will ever
// lower, because there is nothing to lower them TO -- they describe
// simulation behaviour, not hardware. Left in place they hold a module's
// signals hostage and blackbox it.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/LLHD/LLHDOps.h"
#include "circt/Dialect/LLHD/LLHDTypes.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

/// True for a dialect that describes what a SIMULATOR should do, never what
/// the hardware is: `verif`/`ltl` (assertions, properties) and `sim`
/// (file I/O, formatting, `$display`).
static bool isSimulationDialect(Operation *op) {
  StringRef d = op->getName().getDialectNamespace();
  return d == "verif" || d == "ltl" || d == "sim";
}

struct StripSimulationConstructsPass
    : public PassWrapper<StripSimulationConstructsPass,
                         OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(StripSimulationConstructsPass)

  StripSimulationConstructsPass() = default;
  // See ExternalizeMappedCellsPass for why this is written out by hand the
  // moment a Pass::Option is present.
  StripSimulationConstructsPass(const StripSimulationConstructsPass &other)
      : PassWrapper(other) {
    stripInitial = other.stripInitial.getValue();
  }

  StringRef getArgument() const final {
    return "strip-simulation-constructs";
  }
  StringRef getDescription() const final {
    return "Erase assertion, $display/$fopen and initial-block processes";
  }

  Option<bool> stripInitial{
      *this, "strip-initial",
      llvm::cl::desc("Also erase `initial` blocks (processes terminating in "
                      "llhd.halt). True to synthesis semantics, where an "
                      "initial block has no silicon meaning"),
      llvm::cl::init(true)};

  /// Erase `op` and everything that (transitively) consumes its results,
  /// provided every one of those consumers is itself simulation-only or a
  /// drive that exists solely to carry the result into a signal. Returns
  /// false and changes NOTHING if some consumer is real hardware -- a
  /// half-erased process would leave dangling values behind.
  bool eraseWithConsumers(Operation *op) {
    SmallVector<Operation *> worklist{op};
    SmallPtrSet<Operation *, 8> doomed;
    while (!worklist.empty()) {
      Operation *cur = worklist.pop_back_val();
      if (!doomed.insert(cur).second)
        continue;
      for (Value result : cur->getResults()) {
        for (Operation *user : result.getUsers()) {
          if (doomed.contains(user))
            continue;
          // A drive fed by a doomed value has nothing left to drive with.
          // Anything else must be simulation-only to be safe to remove.
          if (!isa<llhd::DriveOp>(user) && !isSimulationDialect(user))
            return false;
          worklist.push_back(user);
        }
      }
    }
    // Drop uses first: the set is closed under "consumes a doomed result", so
    // once every operand reference inside it is gone, nothing outside refers
    // in, and erase order stops mattering.
    for (Operation *dead : doomed)
      dead->dropAllReferences();
    for (Operation *dead : doomed)
      dead->erase();
    return true;
  }

  void runOnOperation() override {
    ModuleOp module = getOperation();
    size_t numAssert = 0, numInitial = 0, numKept = 0, numLooseSim = 0;

    SmallVector<llhd::ProcessOp> candidates;
    module.walk([&](llhd::ProcessOp proc) {
      bool hasVerif = false, hasHalt = false, hasTerminate = false,
           drives = false;
      proc.walk([&](Operation *op) {
        StringRef name = op->getName().getStringRef();
        if (op->getName().getDialectNamespace() == "verif")
          hasVerif = true;
        else if (isa<llhd::HaltOp>(op))
          hasHalt = true;
        else if (isa<llhd::DriveOp>(op))
          drives = true;
        if (name == "sim.terminate" || name == "ub.unreachable")
          hasTerminate = true;
      });
      // A `$fatal` check (e.g. an immediate `assert final ... else $fatal`)
      // imports with no verif op at all: a process that probes, compares and
      // ends in sim.terminate + ub.unreachable. If it drives nothing, it has
      // no hardware meaning; a process that also drives signals is left to
      // the mixed-process path below.
      if (hasVerif || (hasHalt && stripInitial) || (hasTerminate && !drives))
        candidates.push_back(proc);
    });

    size_t numInnerVerif = 0;
    for (auto proc : candidates) {
      bool isInitial = false;
      proc.walk([&](llhd::HaltOp) { isInitial = true; });
      if (eraseWithConsumers(proc)) {
        (isInitial ? numInitial : numAssert)++;
        continue;
      }
      // A MIXED process: it computes real results AND carries assertions,
      // so the whole-process erase correctly refused. Strip the simulation
      // ops surgically instead -- verif ops (assert/assume/cover) have no
      // results, so erasing them is always safe; their ltl/sim feeder
      // cones then die iteratively. Left in, these ops make the module
      // unmappable and it ends up a BLACKBOX (cv64a6's pmp_entry /
      // issue_read_operands, the first real designs to hit this path).
      SmallVector<Operation *> sinks;
      proc.walk([&](Operation *op) {
        if (isSimulationDialect(op) && op->getNumResults() == 0)
          sinks.push_back(op);
      });
      for (Operation *op : sinks) {
        op->erase();
        ++numInnerVerif;
      }
      bool changed = true;
      while (changed) {
        changed = false;
        SmallVector<Operation *> dead;
        proc.walk([&](Operation *op) {
          if (isSimulationDialect(op) && op->use_empty())
            dead.push_back(op);
        });
        for (Operation *op : dead) {
          op->erase();
          changed = true;
        }
      }
      // Only counts as "kept" if simulation ops genuinely remain.
      bool stillHasSim = false;
      proc.walk([&](Operation *op) {
        if (isSimulationDialect(op))
          stillHasSim = true;
      });
      if (stillHasSim)
        ++numKept;
    }

    // Assertions do not only live in llhd.process bodies: an
    // `assert property` guarded by procedural `if`s can elaborate into an
    // `llhd.combinational` region (cv64a6's pmp_entry carries 63 of them,
    // behind cf.cond_br chains). Same surgical rule as mixed processes:
    // zero-result simulation ops are always safe to erase wherever they
    // sit, and their ltl/sim feeder cones die iteratively. The dead
    // branches they leave behind are ordinary control flow for
    // canonicalize / llhd-remove-control-flow to collapse.
    {
      SmallVector<Operation *> sinks;
      module.walk([&](Operation *op) {
        if (isSimulationDialect(op) && op->getNumResults() == 0)
          sinks.push_back(op);
      });
      for (Operation *op : sinks) {
        op->erase();
        ++numInnerVerif;
      }
      bool changed = !sinks.empty();
      while (changed) {
        changed = false;
        SmallVector<Operation *> dead;
        module.walk([&](Operation *op) {
          if (isSimulationDialect(op) && op->use_empty())
            dead.push_back(op);
        });
        for (Operation *op : dead) {
          op->erase();
          changed = true;
        }
      }
    }

    // `llhd.final` is "a process that runs at the end of simulation" -- there
    // is no end of simulation in silicon, so it is unconditionally dead here.
    // It has no results, so nothing can depend on it. This is what was left
    // holding CVA6's TOP module, which would have blackboxed the entire
    // design over a single `final` block.
    size_t numFinal = 0;
    if (stripInitial) {
      SmallVector<llhd::FinalOp> finals;
      module.walk([&](llhd::FinalOp op) { finals.push_back(op); });
      for (auto op : finals) {
        op->dropAllReferences();
        op->erase();
        ++numFinal;
      }
    }

    // A module can EXPORT one of its signals by reference: an output port of
    // type `!llhd.ref`, which the frontend emits so cross-module simulation
    // constructs (assertions, `unread` lint silencers) can observe the
    // signal. CVA6's `cva6_fifo_v3_0` exports its own `push_i` shadow signal
    // this way. After the constructs above are stripped, no instance ever
    // uses the ref result -- but the `hw.output` keeps the signal alive, and
    // sig2reg refuses any signal with a user that is not a probe or drive,
    // so one dead port blackboxes the module. Drop ref-typed output ports
    // whose instance results are all unused, rebuilding the instances.
    size_t numRefPorts = 0;
    {
      // Instances by referenced module name.
      DenseMap<StringAttr, SmallVector<hw::InstanceOp>> instances;
      module.walk([&](hw::InstanceOp inst) {
        instances[inst.getReferencedModuleNameAttr()].push_back(inst);
      });

      for (auto hwModule : module.getOps<hw::HWModuleOp>()) {
        if (!hwModule.isPrivate())
          continue;
        SmallVector<unsigned> refOutputs;
        for (auto [i, type] : llvm::enumerate(hwModule.getOutputTypes()))
          if (isa<llhd::RefType>(type))
            refOutputs.push_back(i);
        if (refOutputs.empty())
          continue;

        auto insts = instances.lookup(hwModule.getModuleNameAttr());
        bool used = false;
        for (auto inst : insts)
          for (unsigned idx : refOutputs)
            if (!inst.getResult(idx).use_empty())
              used = true;
        if (used)
          continue;

        // Drop the operands from the terminator, then the ports, then
        // rebuild each instance without the erased results.
        auto *terminator = hwModule.getBodyBlock()->getTerminator();
        for (unsigned idx : llvm::reverse(refOutputs))
          terminator->eraseOperand(idx);
        hwModule.modifyPorts({}, {}, {}, refOutputs);

        llvm::DenseSet<unsigned> erasedIdx(refOutputs.begin(),
                                           refOutputs.end());
        for (auto inst : insts) {
          OpBuilder builder(inst);
          auto newInst = hw::InstanceOp::create(
              builder, inst.getLoc(), hwModule, inst.getInstanceNameAttr(),
              SmallVector<Value>(inst.getOperands()));
          unsigned newIdx = 0;
          for (auto [oldIdx, result] : llvm::enumerate(inst.getResults())) {
            if (erasedIdx.contains(oldIdx))
              continue;
            result.replaceAllUsesWith(newInst.getResult(newIdx++));
          }
          inst.erase();
        }
        numRefPorts += refOutputs.size();
      }
    }

    // `$display`-style ops can also sit directly in a module body rather than
    // inside a process. They have no results anything else could depend on.
    SmallVector<Operation *> loose;
    module.walk([&](Operation *op) {
      if (isSimulationDialect(op) && isa<hw::HWModuleOp>(op->getParentOp()))
        loose.push_back(op);
    });
    for (Operation *op : loose)
      if (op->use_empty()) {
        op->erase();
        ++numLooseSim;
      }

    llvm::errs() << "[strip-simulation-constructs] erased " << numAssert
                 << " assertion process(es), " << numInitial
                 << " initial block(s), " << numFinal << " final block(s), "
                 << numRefPorts << " dead ref port(s), " << numLooseSim
                 << " loose simulation op(s), " << numInnerVerif
                 << " verif op(s) inside mixed processes; kept " << numKept
                 << " with real hardware consumers\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createStripSimulationConstructsPass() {
  return std::make_unique<StripSimulationConstructsPass>();
}

} // namespace circt_abc
