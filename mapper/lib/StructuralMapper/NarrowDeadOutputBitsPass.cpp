//===- NarrowDeadOutputBitsPass.cpp ------------------------------*- C++ -*-===//
//
// `--narrow-dead-output-bits`: force to zero the bits of a module's output
// ports that no instance of the module ever reads, so the per-module
// narrow-dead-register-bits that follows can retire the registers and
// logic behind them.
//
// Why: per-bit liveness stops at a port. CVA6's TLB hands the MMU a 64-bit
// PTE per lookup; the MMU registers it whole (`dtlb_pte_q`) and then reads
// 47 of the 64 bits (v, r, x, a, d, rsw, n and the 10 reserved bits are
// never read). Inside the TLB every one of the 16 entries' 64 content bits
// looks live, so 17 x 16 x 2 = 544 flops and their update logic survive.
// A boundary-optimising flow keeps exactly 47 bits per entry; this pass is
// that boundary optimisation.
//
// Design-level. Modules are visited callers-first (a topological order of
// the instance graph); for each module the live mask of every output port
// is the OR, over all its instances, of the caller's backward per-bit
// liveness at that instance result. The caller's liveness is the analysis
// narrow-dead-register-bits runs, seeded by ITS OWN output masks (all-ones
// at a root) and by every instance operand (a callee input is
// conservatively fully live), propagated backward through extract / concat
// / replicate / mux / and / or / xor, through a register from data to next
// (bit i of the stored value is bit i of the next value; the reset value
// and clock/reset are whole), and through hw.bitcast as an identity on the
// packed bit pattern -- hw.bitcast reinterprets bits, it does not permute
// them, and the same op defines the packing on both sides of every port
// this pass sees. Anything else demands every bit of every operand.
//
// Rewrite: an output port with dead bits gets its hw.output operand
// replaced by the same value with those bits forced to zero (built as an
// integer and bitcast back to an aggregate port type). This changes what
// the MODULE computes on those bits and is therefore only sound as a
// design-level transformation -- exactly as prune-unused-residual-outputs'
// propagate-constant-outputs is -- so it is gated by top-level miters,
// never per-module ones. Roots (modules nothing instantiates) keep every
// output bit.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/HW/HWTypes.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/MapVector.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Packed bit width of a value's type, 0 when it has none (clocks, opaque
// types): such values are never narrowed and always demanded whole.
static unsigned packedWidth(Type t) {
  int64_t w = hw::getBitWidth(t);
  return w > 0 ? (unsigned)w : 0;
}

struct NarrowDeadOutputBitsPass
    : public PassWrapper<NarrowDeadOutputBitsPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowDeadOutputBitsPass)

  NarrowDeadOutputBitsPass() = default;
  NarrowDeadOutputBitsPass(const NarrowDeadOutputBitsPass &other)
      : PassWrapper(other) {
    minBits = other.minBits.getValue();
    verbose = other.verbose.getValue();
  }

  StringRef getArgument() const final { return "narrow-dead-output-bits"; }
  StringRef getDescription() const final {
    return "Zero the output-port bits no instance of a module reads, so "
           "per-module dead-bit narrowing can retire the logic behind them";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<unsigned> minBits{
      *this, "min-bits",
      llvm::cl::desc("Only rewrite a port with at least this many dead bits"),
      llvm::cl::init(1)};
  Option<bool> verbose{
      *this, "verbose",
      llvm::cl::desc("Report every rewritten port, not only those with >= 16 "
                      "dead bits"),
      llvm::cl::init(false)};

  // Per-module output live masks; an empty APInt marks an untracked port.
  llvm::DenseMap<Operation *, SmallVector<APInt>> outLive;

  // Backward per-bit liveness of one module, seeded from its own output
  // masks; accumulates into the callees' outLive at every instance result.
  void analyzeModule(hw::HWModuleOp module,
                     const llvm::DenseMap<StringAttr, hw::HWModuleOp> &mods) {
    Block *body = module.getBodyBlock();
    Operation *terminator = body->getTerminator();
    llvm::DenseMap<Value, APInt> live;
    SmallVector<Value> worklist;

    auto demand = [&](Value v, const APInt &mask) {
      if (mask.isZero())
        return;
      auto it = live.find(v);
      if (it == live.end()) {
        live.try_emplace(v, mask);
        worklist.push_back(v);
        return;
      }
      APInt merged = it->second | mask;
      if (merged != it->second) {
        it->second = merged;
        worklist.push_back(v);
      }
    };
    auto demandAll = [&](Value v) {
      unsigned w = packedWidth(v.getType());
      // Width-less values (clocks) have nothing to narrow upstream of them
      // that this pass tracks, except an instance result: see below.
      if (w == 0) {
        if (auto res = dyn_cast<OpResult>(v))
          if (auto inst = dyn_cast<hw::InstanceOp>(res.getOwner()))
            if (auto callee = mods.lookup(inst.getModuleNameAttr().getAttr()))
              if (auto *masks = outLive.count(callee) ? &outLive[callee]
                                                      : nullptr)
                (*masks)[res.getResultNumber()] = APInt(); // untracked
        return;
      }
      demand(v, APInt::getAllOnes(w));
    };

    // Seeds: own outputs at their live masks, every instance operand whole.
    const SmallVector<APInt> &myMasks = outLive[module];
    for (auto [i, operand] : llvm::enumerate(terminator->getOperands())) {
      unsigned w = packedWidth(operand.getType());
      if (w == 0 || myMasks[i].getBitWidth() == 0)
        demandAll(operand);
      else
        demand(operand, myMasks[i]);
    }
    for (auto inst : body->getOps<hw::InstanceOp>())
      for (Value operand : inst->getOperands())
        demandAll(operand);

    while (!worklist.empty()) {
      Value v = worklist.pop_back_val();
      APInt mask = live.lookup(v);
      unsigned w = mask.getBitWidth();
      Operation *def = v.getDefiningOp();
      if (!def)
        continue; // module input
      if (auto inst = dyn_cast<hw::InstanceOp>(def)) {
        auto callee = mods.lookup(inst.getModuleNameAttr().getAttr());
        if (!callee || !outLive.count(callee))
          continue;
        APInt &m = outLive[callee][cast<OpResult>(v).getResultNumber()];
        if (m.getBitWidth() == w)
          m |= mask;
        continue;
      }
      if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
        demand(firreg.getNext(), mask);
        if (Value rv = firreg.getResetValue())
          demandAll(rv);
        if (Value rst = firreg.getReset())
          demandAll(rst);
        continue;
      }
      if (auto ext = dyn_cast<comb::ExtractOp>(def)) {
        APInt inMask = APInt::getZero(packedWidth(ext.getInput().getType()));
        inMask.insertBits(mask, ext.getLowBit());
        demand(ext.getInput(), inMask);
        continue;
      }
      if (auto cat = dyn_cast<comb::ConcatOp>(def)) {
        unsigned offset = 0; // comb.concat: FIRST operand at the MSB
        for (int i = (int)cat.getInputs().size() - 1; i >= 0; --i) {
          Value operand = cat.getInputs()[i];
          unsigned ow = packedWidth(operand.getType());
          demand(operand, mask.extractBits(ow, offset));
          offset += ow;
        }
        continue;
      }
      if (auto rep = dyn_cast<comb::ReplicateOp>(def)) {
        unsigned iw = packedWidth(rep.getInput().getType());
        APInt inMask = APInt::getZero(iw);
        for (unsigned off = 0; off + iw <= w; off += iw)
          inMask |= mask.extractBits(iw, off);
        demand(rep.getInput(), inMask);
        continue;
      }
      if (auto mux = dyn_cast<comb::MuxOp>(def)) {
        demand(mux.getTrueValue(), mask);
        demand(mux.getFalseValue(), mask);
        demand(mux.getCond(), APInt(1, 1));
        continue;
      }
      if (isa<comb::AndOp, comb::OrOp, comb::XorOp>(def)) {
        for (Value operand : def->getOperands())
          demand(operand, mask);
        continue;
      }
      if (auto bc = dyn_cast<hw::BitcastOp>(def)) {
        if (packedWidth(bc.getInput().getType()) == w) {
          demand(bc.getInput(), mask);
          continue;
        }
      }
      for (Value operand : def->getOperands())
        demandAll(operand);
    }
  }

  void runOnOperation() override {
    ModuleOp top = getOperation();
    llvm::DenseMap<StringAttr, hw::HWModuleOp> mods;
    SmallVector<hw::HWModuleOp> all;
    for (auto m : top.getOps<hw::HWModuleOp>()) {
      mods[m.getModuleNameAttr()] = m;
      all.push_back(m);
    }
    // Instance graph, callers -> callees; Kahn's order puts every caller
    // before its callees.
    llvm::DenseMap<Operation *, unsigned> callerCount;
    llvm::DenseMap<Operation *, SmallVector<hw::HWModuleOp>> callees;
    for (auto m : all) {
      llvm::SmallPtrSet<Operation *, 8> seen;
      for (auto inst : m.getBodyBlock()->getOps<hw::InstanceOp>()) {
        auto callee = mods.lookup(inst.getModuleNameAttr().getAttr());
        if (!callee || !seen.insert(callee).second)
          continue;
        callees[m].push_back(callee);
        ++callerCount[callee];
      }
    }
    outLive.clear();
    for (auto m : all) {
      SmallVector<APInt> masks;
      bool root = callerCount.lookup(m) == 0;
      for (Value operand : m.getBodyBlock()->getTerminator()->getOperands()) {
        unsigned w = packedWidth(operand.getType());
        masks.push_back(w == 0 ? APInt()
                               : root ? APInt::getAllOnes(w)
                                      : APInt::getZero(w));
      }
      outLive[m] = std::move(masks);
    }
    SmallVector<hw::HWModuleOp> order, ready;
    for (auto m : all)
      if (callerCount.lookup(m) == 0)
        ready.push_back(m);
    while (!ready.empty()) {
      auto m = ready.pop_back_val();
      order.push_back(m);
      for (auto c : callees[m])
        if (--callerCount[c] == 0)
          ready.push_back(c);
    }
    if (order.size() != all.size()) {
      top.emitWarning("narrow-dead-output-bits: instance graph is not a "
                      "DAG; nothing narrowed");
      return;
    }
    for (auto m : order)
      analyzeModule(m, mods);

    // Rewrite.
    unsigned modulesTouched = 0, portsTouched = 0, bitsZeroed = 0;
    for (auto m : order) {
      Operation *terminator = m.getBodyBlock()->getTerminator();
      const SmallVector<APInt> &masks = outLive[m];
      bool touched = false;
      for (auto [i, operand] : llvm::enumerate(terminator->getOperands())) {
        const APInt &mask = masks[i];
        unsigned w = mask.getBitWidth();
        if (w == 0 || mask.isAllOnes())
          continue;
        unsigned dead = w - mask.popcount();
        if (dead < minBits)
          continue;
        OpBuilder b(terminator);
        Location loc = terminator->getLoc();
        Type portType = operand.getType();
        Type intType = b.getIntegerType(w);
        // Integer view of the operand. Peel an existing integer->aggregate
        // bitcast rather than stacking another on it: narrow-dead-register-
        // bits treats a bitcast as opaque (whole-value demand), so a
        // bitcast pair between the slices and the register would make the
        // zeroing invisible to it.
        Value intVal = operand;
        if (!isa<IntegerType>(portType)) {
          auto bc = operand.getDefiningOp<hw::BitcastOp>();
          if (bc && bc.getInput().getType() == intType)
            intVal = bc.getInput();
          else
            intVal = hw::BitcastOp::create(b, loc, intType, operand);
        }
        Value replacement;
        if (mask.isZero()) {
          replacement = hw::ConstantOp::create(b, loc, APInt::getZero(w));
        } else {
          SmallVector<Value> pieces; // LSB first
          for (unsigned bit = 0; bit < w;) {
            bool liveRun = mask[bit];
            unsigned run = 1;
            while (bit + run < w && mask[bit + run] == liveRun)
              ++run;
            if (liveRun)
              pieces.push_back(comb::ExtractOp::create(
                  b, loc, b.getIntegerType(run), intVal, bit));
            else
              pieces.push_back(
                  hw::ConstantOp::create(b, loc, APInt::getZero(run)));
            bit += run;
          }
          std::reverse(pieces.begin(), pieces.end());
          replacement = pieces.size() == 1
                            ? pieces[0]
                            : comb::ConcatOp::create(b, loc, pieces);
        }
        if (!isa<IntegerType>(portType))
          replacement = hw::BitcastOp::create(b, loc, portType, replacement);
        terminator->setOperand(i, replacement);
        touched = true;
        ++portsTouched;
        bitsZeroed += dead;
        if (verbose || dead >= 16)
          llvm::errs() << "[narrow-dead-output-bits] " << m.getModuleName()
                       << " out#" << i << ": " << dead << " of " << w
                       << " bit(s) dead in every instance\n";
      }
      if (touched)
        ++modulesTouched;
    }
    llvm::errs() << "[narrow-dead-output-bits] " << modulesTouched
                 << " module(s), " << portsTouched << " port(s), "
                 << bitsZeroed << " output bit(s) zeroed\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowDeadOutputBitsPass() {
  return std::make_unique<NarrowDeadOutputBitsPass>();
}

} // namespace circt_abc
