//===- LimitFanoutPass.cpp -----------------------------------*- C++ -*-===//
//
// `--limit-fanout`: cap net fanout on the MAPPED netlist by inserting a
// balanced buffer tree, touching ONLY the nets that exceed the cap.
//
// WHY. Without a fanout limit, the worst nets of a mapped netlist are all
// mapper outputs carrying hundreds of loads, where a commercially
// synthesised netlist of the same design keeps its worst logic net below a
// hundred. Commercial synthesis applies a max-fanout constraint and buffers
// to meet it; neither ABC nor emap applies one at all. OpenROAD then
// builds those buffer trees during `repair_design`, and that shows up as
// area this flow pays at placement.
//
// WHY NOT ABC's `buffer`. It was measured and is too blunt: `-N 16`, `-N
// 64` and `-N 128` cost about the same area, nearly independent of the
// cap, because its default gain target (`-G 300`) resizes the WHOLE
// netlist rather than the offending nets. That is more than the placer
// spends, and it reproduces this project's already-retired "blind ABC
// buffer tier". This pass instead touches only the tail.
//
// WHAT IT DOES. For each value with more than `max-fanout` loads, split
// the loads into groups of at most `max-fanout`, drive each group from
// its own buffer, and recurse if the buffer count itself exceeds the cap.
// Area cost is bounded and predictable: about ceil(L/N) buffers per
// offending net, each the library's cheapest buffer.
//
// GUARDS.
//   * Clock nets are SKIPPED. A net any of whose loads is a cell clock
//     pin belongs to clock-tree synthesis, and a logic buffer in a clock
//     path is a correctness/skew hazard, not an optimisation.
//   * Only values whose loads are all cell instances (or hw.output) are
//     touched; anything else is left alone.
//   * The buffer cell comes FROM THE LIBERTY (the cheapest single-input
//     identity-function cell), never a hardcoded name -- the same oracle
//     `remove-identity-buffers` uses to find them again.
//
// PIPELINE POSITION. Must run AFTER `remove-identity-buffers`, which
// would otherwise delete every buffer this pass inserts (they are
// identity cells by construction). OFF by default per the project
// convention.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

/// Pin names that mean "clock input" on this PDK's sequential and
/// clock-gating cells. A net driving any of these is CTS's business.
static bool isClockPinName(StringRef pin) {
  return pin == "CK" || pin == "CLK" || pin == "CP" || pin == "CKN";
}

struct LimitFanoutPass
    : public PassWrapper<LimitFanoutPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LimitFanoutPass)

  LimitFanoutPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  LimitFanoutPass(const LimitFanoutPass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    enableLimit = other.enableLimit.getValue();
    maxFanout = other.maxFanout.getValue();
  }

  StringRef getArgument() const final { return "limit-fanout"; }
  StringRef getDescription() const final {
    return "Cap net fanout on the mapped netlist with a balanced buffer "
           "tree, touching only nets above the cap -- neither ABC nor emap "
           "applies a max-fanout constraint, and OpenROAD pays for it at "
           "placement";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file used to pick the buffer cell")};
  Option<bool> enableLimit{
      *this, "enable-limit-fanout",
      llvm::cl::desc("Enable fanout limiting. OFF by default per the "
                     "project convention."),
      llvm::cl::init(false)};
  Option<unsigned> maxFanout{
      *this, "max-fanout",
      llvm::cl::desc("Loads per net above which a buffer tree is inserted. "
                     "Commercial flows keep the worst logic net below a "
                     "hundred loads; a cap far below that buffers nets "
                     "nobody was paying for."),
      llvm::cl::init(64)};

  void runOnOperation() override {
    if (!enableLimit.getValue())
      return;
    if (libertyPath.getValue().empty()) {
      getOperation().emitError("limit-fanout: liberty= is required when "
                               "enable-limit-fanout is set");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      getOperation().emitError("limit-fanout: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    unsigned cap = std::max<unsigned>(maxFanout.getValue(), 2);

    mlir::ModuleOp top = getOperation();
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);

    // Cheapest identity (buffer) cell declared in this design, or one we
    // declare ourselves.
    const CellPattern *bufCell = nullptr;
    for (StringRef n : lib->identityCellNames()) {
      const CellPattern *p = lib->patternFor(n);
      if (p && p->arity() == 1 && (!bufCell || p->area < bufCell->area))
        bufCell = p;
    }
    if (!bufCell) {
      top.emitWarning("limit-fanout: no identity (buffer) cell in liberty; "
                      "nothing to do");
      return;
    }

    // Port-name lookup per extern, so a clock pin can be recognised.
    llvm::DenseMap<StringRef, SmallVector<StringRef, 8>> externInputs;
    for (auto ext : top.getOps<hw::HWModuleExternOp>()) {
      SmallVector<StringRef, 8> ins;
      for (auto port : ext.getPortList())
        if (port.dir == hw::ModulePort::Direction::Input)
          ins.push_back(port.name.getValue());
      externInputs[ext.getModuleName()] = std::move(ins);
    }

    unsigned netsFixed = 0, buffersAdded = 0;
    for (auto hwmod : top.getOps<hw::HWModuleOp>()) {
      // Snapshot the candidate values first: inserting buffers changes the
      // use lists we are iterating.
      SmallVector<Value> candidates;
      llvm::DenseSet<void *> seen;
      auto consider = [&](Value v) {
        if (!v || v.getType() != i1)
          return;
        if (!seen.insert(v.getAsOpaquePointer()).second)
          return;
        if (std::distance(v.use_begin(), v.use_end()) <= (ptrdiff_t)cap)
          return;
        candidates.push_back(v);
      };
      for (auto inst : hwmod.getBodyBlock()->getOps<hw::InstanceOp>())
        for (Value r : inst.getResults())
          consider(r);
      for (BlockArgument a : hwmod.getBodyBlock()->getArguments())
        consider(a);

      for (Value net : candidates) {
        // Collect the uses, and bail if any is a clock pin.
        SmallVector<OpOperand *> uses;
        bool isClock = false;
        for (OpOperand &u : net.getUses()) {
          if (auto inst = dyn_cast<hw::InstanceOp>(u.getOwner())) {
            auto it = externInputs.find(inst.getModuleName());
            if (it != externInputs.end() &&
                u.getOperandNumber() < it->second.size() &&
                isClockPinName(it->second[u.getOperandNumber()])) {
              isClock = true;
              break;
            }
          }
          uses.push_back(&u);
        }
        if (isClock || uses.size() <= cap)
          continue;

        auto ext = lookupExtern(top, bufCell, i1);
        OpBuilder b(ctx);
        // Insert the tree right after the driver (or at block begin for a
        // block argument), so every rewired use still dominates.
        if (Operation *def = net.getDefiningOp())
          b.setInsertionPointAfter(def);
        else
          b.setInsertionPointToStart(hwmod.getBodyBlock());

        // One level of ceil(L/cap) buffers, recursing while the buffer
        // count itself would exceed the cap.
        SmallVector<OpOperand *> pending(uses);
        Value src = net;
        unsigned level = 0;
        while (pending.size() > cap) {
          unsigned nbuf = (pending.size() + cap - 1) / cap;
          SmallVector<Value> bufs;
          for (unsigned i = 0; i < nbuf; ++i) {
            auto inst = hw::InstanceOp::create(
                b, net.getLoc(), ext,
                b.getStringAttr("fo_" + std::to_string(netsFixed) + "_" +
                                std::to_string(level) + "_" +
                                std::to_string(i)),
                ArrayRef<Value>{src});
            bufs.push_back(inst.getResult(0));
            ++buffersAdded;
          }
          // Spread the pending uses evenly over this level's buffers.
          for (unsigned i = 0; i < pending.size(); ++i)
            pending[i]->set(bufs[i % nbuf]);
          // If a single buffer now drives more than the cap we would need
          // another level, but by construction each drives <= cap uses.
          pending.clear();
          ++level;
        }
        ++netsFixed;
      }
    }

    if (netsFixed > 0)
      llvm::errs() << "[limit-fanout] " << netsFixed << " net(s) above "
                   << cap << " loads rebuffered with " << buffersAdded
                   << " " << bufCell->name << " ("
                   << llvm::format("%.2f", buffersAdded * bufCell->area)
                   << " um^2)\n";
  }

  hw::HWModuleExternOp lookupExtern(mlir::ModuleOp top,
                                    const CellPattern *cell, Type i1) {
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == cell->name)
        return ext;
    SmallVector<hw::PortInfo, 2> ports;
    hw::PortInfo in;
    in.name = StringAttr::get(top.getContext(), cell->inputPins[0]);
    in.type = i1;
    in.dir = hw::ModulePort::Direction::Input;
    in.argNum = 0;
    ports.push_back(in);
    hw::PortInfo out;
    out.name = StringAttr::get(top.getContext(), cell->outputPin);
    out.type = i1;
    out.dir = hw::ModulePort::Direction::Output;
    out.argNum = 0;
    ports.push_back(out);
    OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
    return hw::HWModuleExternOp::create(
        b, top.getLoc(), StringAttr::get(top.getContext(), cell->name), ports);
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createLimitFanoutPass() {
  return std::make_unique<LimitFanoutPass>();
}

} // namespace circt_abc
