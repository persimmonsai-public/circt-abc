//===- PruneUnusedResidualOutputsPass.cpp ------------------------*- C++ -*-===//
//
// `--prune-unused-residual-outputs`: for every `*_residual` module with
// exactly one instance, erase the output ports whose instance results have
// no uses, then drop the corresponding hw.output operands so a following
// canonicalize/cse deletes the now-dead cones inside the residual.
//
// Why: residuals are outlined per RTL module BEFORE the design is
// flattened, so an output the enclosing hierarchy never consumes still
// leaves the residual as a port; merge-residual-modules turns every such
// port into a primary output of the merged AIG and ABC keeps its whole cone.
// Yosys's flatten + opt_clean removes those cones before its ABC. Measured
// on ibex flow6 (2026-09-03): 56 of 1,386 residual outputs (484 bits) are
// unused in the flattened top (if_stage 112 bits, core 76, cs_registers 72,
// decoder 68, one counter's full 64-bit value, ...).
//
// Placement: after hw-flatten-modules (so the single instance sees the whole
// design), before merge-residual-modules. Function-preserving: it only
// removes values nothing reads.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/HW/HWInstanceGraph.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/IRMapping.h"
#include "llvm/ADT/DenseSet.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct PruneUnusedResidualOutputsPass
    : public PassWrapper<PruneUnusedResidualOutputsPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PruneUnusedResidualOutputsPass)

  PruneUnusedResidualOutputsPass() = default;
  PruneUnusedResidualOutputsPass(const PruneUnusedResidualOutputsPass &other)
      : PassWrapper(other) {
    anyModule = other.anyModule.getValue();
    constOutputs = other.constOutputs.getValue();
    outputFields = other.outputFields.getValue();
  }

  // The pass was written for `*_residual` modules after flattening. The same
  // liveness argument holds for ANY private module: an output port whose
  // instance result nothing reads can be removed, and the cone driving it
  // then dies. Restricting it by name was a scoping choice, not a
  // correctness one.
  //
  // It matters before the register passes, not only after flattening. In
  // CVA6 the prefetcher's four CSR readback outputs
  // (`hwpf_base_o` / `param_o` / `throttle_o` / `status_o` on
  // `cva6_hpdcache_subsystem`) each occur EXACTLY ONCE in the whole design
  // -- at their own definition -- so nothing reads them. They are the only
  // escape for `hwpf_stride.csr_throttle_q`, a reset-less 32-bit register
  // x4 instances. Pruning them makes that register dead by pure liveness,
  // where the alternative was the X-optimistic `--fold-unreset-holds`.
  // The dual of the aggregate fix in narrow-constant-register-bits:
  // constants do not cross a port boundary OUTWARD either. 91 of CVA6's 823
  // module output operands are driven by a literal constant, and every
  // caller treats the corresponding instance result as opaque. Replacing
  // those results with the constant lets the caller fold, and the port then
  // prunes as unused, killing the cone inside the callee. Pure
  // substitution of equals -- no assumption.
  // Whole-port pruning only fires when NOTHING reads a port. The finer case
  // is a struct-typed output of which only some FIELDS are read: CVA6's
  // instruction FIFO returns
  //   struct<instr: i32, cf: i3, ex: i2, ex_vaddr: i32, ex_gpaddr: i34,
  //          ex_tinst: i32, ex_gva: i1>
  // and its only uses are struct_extract of `ex`, `instr` and `cf`.
  // `ex_vaddr` is written into the FIFO and never read -- 32 bits x 4 entries
  // x 2 FIFOs = 256 flops. Per-bit liveness does not cross an aggregate port,
  // so nothing sees it.
  //
  // Rewrite: when EVERY use of a struct-typed instance result (at every
  // instance) is a struct_extract, replace the port with one scalar port per
  // USED field. The callee emits struct_extract of the original value for
  // each kept field, so the dropped fields' cones die; the caller's extracts
  // become direct results. Sound: a field no instance reads cannot affect
  // anything.
  Option<bool> outputFields{
      *this, "prune-unused-output-fields", llvm::cl::init(false),
      llvm::cl::desc("Split a struct-typed output port into one port per "
                     "field that some instance actually extracts (OFF by "
                     "default)")};


  Option<bool> constOutputs{
      *this, "propagate-constant-outputs", llvm::cl::init(false),
      llvm::cl::desc("Replace instance results whose callee output is a "
                     "literal constant with that constant (OFF by default)")};

  Option<bool> anyModule{
      *this, "any-module", llvm::cl::init(false),
      llvm::cl::desc("Prune unused outputs of ANY private module, not just "
                     "*_residual ones (OFF by default)")};

  StringRef getArgument() const final {
    return "prune-unused-residual-outputs";
  }
  StringRef getDescription() const final {
    return "Erase residual-module output ports whose single instance result "
           "is unused, so the dead cones can be removed before ABC";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect>();
  }

  // Find ONE struct-typed output port whose instances only ever
  // struct_extract a proper subset of its fields, and split it into one
  // scalar port per used field. Returns true if it changed anything.
  //
  // Applies a single case then returns, so the caller rescans from scratch.
  // Rebuilding instances invalidates any instance list held across the
  // mutation -- doing one case per scan is what keeps that safe, and costs
  // only a rescan of ~142 modules.
  bool splitOneStructOutput(mlir::ModuleOp top) {
    llvm::DenseMap<StringAttr, SmallVector<hw::InstanceOp, 4>> insts;
    top.walk([&](hw::InstanceOp i) {
      insts[i.getModuleNameAttr().getAttr()].push_back(i);
    });
    for (auto mod : top.getOps<hw::HWModuleOp>()) {
      if (!anyModule && !mod.getModuleName().ends_with("_residual"))
        continue;
      auto it = insts.find(mod.getModuleNameAttr());
      if (it == insts.end() || it->second.empty())
        continue;
      auto *term = mod.getBodyBlock()->getTerminator();
      for (unsigned i = 0, e = it->second.front().getNumResults(); i < e; ++i) {
        auto st = dyn_cast<hw::StructType>(
            hw::getCanonicalType(it->second.front().getResult(i).getType()));
        if (!st || st.getElements().size() < 2)
          continue;
        llvm::SmallDenseSet<StringAttr> used;
        bool allExtracts = true, anyUse = false;
        for (hw::InstanceOp cur : it->second)
          for (Operation *u : cur.getResult(i).getUsers()) {
            anyUse = true;
            auto ex = dyn_cast<hw::StructExtractOp>(u);
            if (!ex) { allExtracts = false; break; }
            used.insert(ex.getFieldNameAttr());
          }
        if (!allExtracts || !anyUse || used.empty() ||
            used.size() == st.getElements().size())
          continue;

        // Callee: one extra output per used field.
        const unsigned base = mod.getNumOutputPorts();
        OpBuilder tb(term);
        Value drv = term->getOperand(i);
        SmallVector<StringAttr> keptNames;
        SmallVector<Value> outs(term->getOperands().begin(),
                                term->getOperands().end());
        for (auto elem : st.getElements()) {
          if (!used.count(elem.name)) continue;
          outs.push_back(
              hw::StructExtractOp::create(tb, term->getLoc(), drv, elem.name));
          keptNames.push_back(elem.name);
        }
        hw::OutputOp::create(tb, term->getLoc(), outs);
        term->erase();

        SmallVector<std::pair<unsigned, hw::PortInfo>> ins;
        for (unsigned k = 0; k < keptNames.size(); ++k) {
          hw::PortInfo pi;
          pi.name = tb.getStringAttr(mod.getOutputName(i).str() + "_" +
                                     keptNames[k].str());
          pi.dir = hw::ModulePort::Direction::Output;
          pi.type = outs[base + k].getType();
          pi.argNum = base + k;
          ins.push_back({base, pi});
        }
        mod.insertPorts({}, ins);

        // Callers: rebuild with the extra results, rewire the extracts.
        // Snapshot the list first -- erasing invalidates it.
        SmallVector<hw::InstanceOp, 4> targets(it->second.begin(),
                                               it->second.end());
        for (hw::InstanceOp cur : targets) {
          SmallVector<Type> rts;
          SmallVector<Attribute> rns;
          auto oldNames = cur.getResultNames();
          unsigned nOld = cur.getNumResults();
          for (unsigned q = 0; q < nOld; ++q) {
            rts.push_back(cur.getResult(q).getType());
            rns.push_back(oldNames[q]);
          }
          for (unsigned k = 0; k < keptNames.size(); ++k) {
            rts.push_back(outs[base + k].getType());
            rns.push_back(tb.getStringAttr(mod.getOutputName(i).str() + "_" +
                                           keptNames[k].str()));
          }
          OpBuilder ib(cur);
          auto ni = hw::InstanceOp::create(
              ib, cur.getLoc(), rts, cur.getInstanceNameAttr(),
              cur.getModuleNameAttr(), cur.getInputs(), cur.getArgNamesAttr(),
              ib.getArrayAttr(rns), cur.getParametersAttr(),
              cur.getInnerSymAttr());
          for (unsigned q = 0; q < nOld; ++q)
            cur.getResult(q).replaceAllUsesWith(ni.getResult(q));
          cur.erase();
          SmallVector<Operation *> toErase;
          for (Operation *u : ni.getResult(i).getUsers()) {
            auto ex = dyn_cast<hw::StructExtractOp>(u);
            if (!ex) continue;
            for (unsigned k = 0; k < keptNames.size(); ++k)
              if (keptNames[k] == ex.getFieldNameAttr()) {
                ex.getResult().replaceAllUsesWith(ni.getResult(nOld + k));
                toErase.push_back(ex);
                break;
              }
          }
          for (Operation *o : toErase) o->erase();
        }
        ++nFields;
        return true;
      }
    }
    return false;
  }

  unsigned nFields = 0;

  void runOnOperation() override {
    mlir::ModuleOp top = getOperation();
    // Splitting rebuilds hw.instance ops, so it must run BEFORE the
    // instance map below is built -- otherwise that map holds erased ops
    // and the dead-port scan reads garbage result numbers.
    if (outputFields)
      for (unsigned guard = 0; guard < 256 && splitOneStructOutput(top);
           ++guard) {}
    // module name -> its instances (only *_residual modules matter)
    llvm::DenseMap<StringAttr, SmallVector<hw::InstanceOp, 2>> instances;
    top.walk([&](hw::InstanceOp inst) {
      instances[inst.getModuleNameAttr().getAttr()].push_back(inst);
    });
    unsigned nPorts = 0, nBits = 0, nMods = 0, nConst = 0;
    for (auto mod : llvm::make_early_inc_range(top.getOps<hw::HWModuleOp>())) {
      if (!anyModule && !mod.getModuleName().ends_with("_residual"))
        continue;
      auto it = instances.find(mod.getModuleNameAttr());
      if (it == instances.end() || it->second.empty())
        continue;
      // A port is dead only if its result is unused at EVERY instance --
      // the same meet that makes any cross-instance fact sound. The pass
      // originally required exactly one instance; that ruled out CVA6's
      // `hwpf_stride` (4 instances), whose dead `csr_throttle_o` is the only
      // thing keeping a reset-less 32-bit register alive in each of them.
      hw::InstanceOp inst = it->second.front();

      // Constant outputs first: substituting them makes the port unused, so
      // the pruning below then removes it and the cone that fed it.
      if (constOutputs) {
        auto *term = mod.getBodyBlock()->getTerminator();
        for (unsigned i = 0, e = term->getNumOperands(); i < e; ++i) {
          Value drv = term->getOperand(i);
          Operation *def = drv.getDefiningOp();
          // Accept a constant, or a bitcast of one (how aggregate-typed
          // constant outputs appear after lower-aggregate-registers).
          Operation *cst = nullptr;
          if (isa_and_nonnull<hw::ConstantOp, hw::AggregateConstantOp>(def))
            cst = def;
          else if (auto bc = dyn_cast_or_null<hw::BitcastOp>(def)) {
            Operation *inner = bc.getInput().getDefiningOp();
            if (isa_and_nonnull<hw::ConstantOp, hw::AggregateConstantOp>(inner))
              cst = def; // clone the bitcast chain
          }
          if (!cst)
            continue;
          for (hw::InstanceOp cur : it->second) {
            if (cur.getResult(i).use_empty())
              continue;
            OpBuilder cb(cur);
            IRMapping map;
            if (auto bc = dyn_cast<hw::BitcastOp>(cst)) {
              Operation *innerClone = cb.clone(*bc.getInput().getDefiningOp());
              map.map(bc.getInput(), innerClone->getResult(0));
            }
            Operation *clone = cb.clone(*cst, map);
            cur.getResult(i).replaceAllUsesWith(clone->getResult(0));
            ++nConst;
          }
        }
      }

      SmallVector<unsigned> dead;
      for (auto res : inst.getResults()) {
        unsigned idx = res.getResultNumber();
        bool unusedEverywhere = true;
        for (hw::InstanceOp other : it->second)
          if (!other.getResult(idx).use_empty()) {
            unusedEverywhere = false;
            break;
          }
        if (unusedEverywhere)
          dead.push_back(idx);
      }
      if (dead.empty())
        continue;
      // erase from the callee: output ports + hw.output operands
      auto outputOp = cast<hw::OutputOp>(mod.getBodyBlock()->getTerminator());
      llvm::BitVector eraseMask(mod.getNumOutputPorts());
      for (unsigned i : dead) {
        eraseMask.set(i);
        nBits += hw::getBitWidth(inst.getResult(i).getType());
      }
      // hw.output operands are the module's output values in order
      SmallVector<Value> keptOutputs;
      for (unsigned i = 0, e = outputOp.getNumOperands(); i < e; ++i)
        if (!eraseMask.test(i))
          keptOutputs.push_back(outputOp.getOperand(i));
      OpBuilder b(outputOp);
      hw::OutputOp::create(b, outputOp.getLoc(), keptOutputs);
      outputOp.erase();
      // module port list: erase the dead outputs
      SmallVector<unsigned> deadOutputIdx(dead.begin(), dead.end());
      mod.erasePorts({}, deadOutputIdx);
      // instance: rebuild without the dead results
      SmallVector<Type> newResultTypes;
      SmallVector<Attribute> newResultNames;
      auto oldNames = inst.getResultNames();
      for (unsigned i = 0, e = inst.getNumResults(); i < e; ++i)
        if (!eraseMask.test(i)) {
          newResultTypes.push_back(inst.getResult(i).getType());
          newResultNames.push_back(oldNames[i]);
        }
      for (hw::InstanceOp cur : it->second) {
        OpBuilder ib(cur);
        auto newInst = hw::InstanceOp::create(
            ib, cur.getLoc(), newResultTypes, cur.getInstanceNameAttr(),
            cur.getModuleNameAttr(), cur.getInputs(), cur.getArgNamesAttr(),
            ib.getArrayAttr(newResultNames), cur.getParametersAttr(),
            cur.getInnerSymAttr());
        unsigned k = 0;
        for (unsigned i = 0, e = cur.getNumResults(); i < e; ++i)
          if (!eraseMask.test(i))
            cur.getResult(i).replaceAllUsesWith(newInst.getResult(k++));
        cur.erase();
      }
      nPorts += dead.size();
      ++nMods;
    }
    if (nFields)
      llvm::errs() << "[prune-unused-residual-outputs] split " << nFields
                   << " struct output(s) into used fields\n";
    if (nConst)
      llvm::errs() << "[prune-unused-residual-outputs] substituted " << nConst
                   << " constant instance result(s)\n";
    if (nPorts)
      llvm::errs() << "[prune-unused-residual-outputs] erased " << nPorts
                   << " unused output port(s) / " << nBits << " bit(s) from "
                   << nMods << " residual module(s)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createPruneUnusedResidualOutputsPass() {
  return std::make_unique<PruneUnusedResidualOutputsPass>();
}

} // namespace circt_abc
