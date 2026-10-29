//===- MapShellLogicPass.cpp --------------------------------*- C++ -*-===//
//
// `--map-shell-logic`: map the handful of comb ops that legitimately
// survive abc-bridge OUTSIDE any residual module to real liberty cells.
//
// Why such ops exist at all: `outline-residual-logic` cuts a greedy
// feedback vertex set so each flattened residual module is acyclic (see
// OutlineResidualLogicPass.cpp). The cut ops stay behind in the parent
// "shell" module -- they read residual outputs and feed residual inputs,
// which is exactly what makes them uncuttable into the residual. On ibex
// the cut landed on pure wiring (extract/concat), which ExportVerilog
// emits as concat/select expressions OpenSTA's structural Verilog reader
// accepts. On CVA6 the cut landed on 4 `comb.mux` + 1 `comb.or` -- real
// logic, exported as `?:`/`|` operator expressions, which OpenSTA rejects
// outright (`[ERROR STA-0171] ... syntax error` at the first `?:` during
// floorplan). A mapped netlist must be 100% cells + wiring.
//
// Mapping strategy: bit-blast each leftover mux/and/or/xor and map every
// bit through LibertyLibrary::lookup() -- the same cheapest-cell
// truth-table oracle every other liberty-consuming pass here uses, so no
// PDK cell name is hardcoded. A 2:1 mux bit is a 3-input table (0xD8 in
// the library's bit-`a`-is-output-for-assignment-`a` convention); a MUX2
// cell (`(S0&B)|((!S0)&A)`) matches it directly. Variadic and/or/xor ops
// first decompose into binary trees of the same op, then each binary op
// maps per-bit. If lookup() only matches an output-inverted cell, the
// complement is realized with an explicit INV-class cell (arity-1 table
// 0b01) -- NOT a comb.xor, which would itself be a leftover.
//
// Constant bits (a cut mux arm is often a concat containing literal
// zeros) simply flow into cell input pins as extract-of-constant;
// ExportVerilog emits them as constant pin connections, which OpenSTA
// accepts. No folding cleverness -- these are a handful of ops per
// design, and correctness beats area at this scale (the whole CVA6 cut
// set is ~600 bits against an 88k-cell netlist).
//
// Deliberately NOT handled (loud remark, not silent): any other comb op
// class showing up post-bridge (icmp/add/...). The feedback cut has only
// ever produced mux/logical ops plus wiring; if that changes, the remark
// names the op so the gap is visible instead of resurfacing as an
// OpenSTA syntax error three stages later. extract/concat/replicate and
// hw.constant are wiring, always fine, never touched.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Truth tables in LibertyLibrary's convention: bit `a` of the table is
// the output for the assignment where leaf `i` gets bit `i` of `a`.
// Mux leaves are ordered [sel, trueVal, falseVal].
constexpr TruthTable kMuxTable = 0xD8; // sel ? t : f
constexpr TruthTable kAndTable = 0b1000;
constexpr TruthTable kOrTable = 0b1110;
constexpr TruthTable kXorTable = 0b0110;
constexpr TruthTable kInvTable = 0b01;

struct MapShellLogicPass
    : public PassWrapper<MapShellLogicPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MapShellLogicPass)

  MapShellLogicPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  MapShellLogicPass(const MapShellLogicPass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
  }

  StringRef getArgument() const final { return "map-shell-logic"; }
  StringRef getDescription() const final {
    return "Map post-abc-bridge leftover comb ops (the outliner's "
           "feedback-cut mux/and/or/xor in shell modules) to liberty "
           "cells per bit, so the exported netlist is 100% cells + "
           "wiring (OpenSTA rejects operator expressions)";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file used to look up mux/and/or/xor/inv "
                     "cells by truth table -- same file abc-bridge maps "
                     "against")};

  // Lazily-created extern decl per cell name (same shape as
  // StructuralMapperPass::getOrCreateExternDecl; duplicated rather than
  // shared because that one lives in an anonymous namespace and carries
  // pass-local state).
  llvm::StringMap<hw::HWModuleExternOp> externDecls;
  unsigned instanceCounter = 0;

  hw::HWModuleExternOp getOrCreateExternDecl(ModuleOp module,
                                             const CellPattern &cell) {
    if (auto existing = externDecls.lookup(cell.name))
      return existing;
    // Reuse a pre-existing extern decl of the same cell (abc-bridge or the
    // structural mapper usually declared it already).
    for (auto ext : module.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == cell.name) {
        externDecls[cell.name] = ext;
        return ext;
      }

    SmallVector<hw::PortInfo, 5> ports;
    Type i1 = IntegerType::get(module.getContext(), 1);
    for (auto [idx, pinName] : llvm::enumerate(cell.inputPins)) {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), pinName);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Input;
      port.argNum = idx;
      ports.push_back(port);
    }
    {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), cell.outputPin);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Output;
      port.argNum = 0;
      ports.push_back(port);
    }
    OpBuilder builder = OpBuilder::atBlockBegin(module.getBody());
    auto externOp = hw::HWModuleExternOp::create(
        builder, module.getLoc(),
        StringAttr::get(module.getContext(), cell.name), ports);
    externDecls[cell.name] = externOp;
    return externOp;
  }

  // Instantiate the cheapest cell matching `table` over `leaves` (already
  // i1). Returns the mapped i1 result, or null if no cell matches.
  Value emitCell(ModuleOp module, OpBuilder &builder, Location loc,
                 const LibertyLibrary &lib, TruthTable table,
                 ArrayRef<Value> leaves) {
    auto match = lib.lookup(leaves.size(), table);
    if (!match)
      return {};
    hw::HWModuleExternOp externOp =
        getOrCreateExternDecl(module, *match->cell);
    SmallVector<Value, 4> operands;
    for (unsigned slot = 0; slot < leaves.size(); ++slot)
      operands.push_back(leaves[match->leafForCellInputSlot[slot]]);
    std::string instName =
        ("u_shell_" + match->cell->name + "_" + Twine(instanceCounter++))
            .str();
    auto inst =
        hw::InstanceOp::create(builder, loc, externOp,
                               builder.getStringAttr(instName), operands);
    Value result = inst.getResult(0);
    if (match->inverted) {
      // Realize the complement with a real INV-class cell, not a
      // comb.xor (which would itself be leftover logic).
      Value inv = emitCell(module, builder, loc, lib, kInvTable, {result});
      if (!inv)
        return {};
      result = inv;
    }
    return result;
  }

  Value extractBit(OpBuilder &builder, Location loc, Value v, unsigned bit) {
    if (v.getType().getIntOrFloatBitWidth() == 1)
      return v;
    return comb::ExtractOp::create(builder, loc, v, bit, 1);
  }

  // Map one binary/mux op per bit. Returns true on success (op replaced
  // and erased).
  bool mapPerBit(ModuleOp module, const LibertyLibrary &lib, Operation *op,
                 TruthTable table, ArrayRef<Value> wordOperands) {
    auto intTy = dyn_cast<IntegerType>(op->getResult(0).getType());
    if (!intTy)
      return false;
    unsigned width = intTy.getWidth();
    OpBuilder builder(op);
    Location loc = op->getLoc();
    // comb.concat is MSB-first: collect bit width-1 down to 0.
    SmallVector<Value> bits;
    for (int bit = width - 1; bit >= 0; --bit) {
      SmallVector<Value, 3> leaves;
      for (Value w : wordOperands)
        leaves.push_back(extractBit(builder, loc, w, bit));
      Value mapped = emitCell(module, builder, loc, lib, table, leaves);
      if (!mapped) {
        op->emitError("map-shell-logic: no liberty cell matches table ")
            << table << " over " << leaves.size() << " inputs";
        return false;
      }
      bits.push_back(mapped);
    }
    Value replacement =
        width == 1 ? bits.front()
                   : comb::ConcatOp::create(builder, loc, bits).getResult();
    op->getResult(0).replaceAllUsesWith(replacement);
    op->erase();
    return true;
  }

  void runOnOperation() override {
    if (libertyPath.getValue().empty()) {
      getOperation().emitError("map-shell-logic: liberty= is required");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      getOperation().emitError(
          "map-shell-logic: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }

    ModuleOp top = getOperation();
    unsigned mapped = 0, skipped = 0;
    bool failed = false;

    for (auto hwmod : top.getOps<hw::HWModuleOp>()) {
      // Snapshot: mapping creates new extract/concat ops in the block.
      SmallVector<Operation *> worklist;
      for (Operation &op : *hwmod.getBodyBlock())
        if (isa<comb::MuxOp, comb::AndOp, comb::OrOp, comb::XorOp>(op))
          worklist.push_back(&op);

      // Decompose variadic and/or/xor into binary trees first (of the
      // same op kind), appending the new binaries to the worklist.
      for (unsigned i = 0; i < worklist.size(); ++i) {
        Operation *op = worklist[i];
        if (isa<comb::MuxOp>(op) || op->getNumOperands() <= 2)
          continue;
        OpBuilder builder(op);
        auto makeBinary = [&](Value a, Value b) {
          Operation *bin = builder.create(
              mlir::OperationState(op->getLoc(), op->getName().getStringRef(),
                                   {a, b}, {op->getResult(0).getType()}, {}));
          worklist.push_back(bin);
          return bin->getResult(0);
        };
        SmallVector<Value> vals(op->getOperands());
        while (vals.size() > 2) {
          SmallVector<Value> next;
          for (unsigned j = 0; j + 1 < vals.size(); j += 2)
            next.push_back(makeBinary(vals[j], vals[j + 1]));
          if (vals.size() % 2)
            next.push_back(vals.back());
          vals = std::move(next);
        }
        op->getResult(0).replaceAllUsesWith(makeBinary(vals[0], vals[1]));
        op->erase();
        worklist[i] = nullptr;
      }

      for (Operation *op : worklist) {
        if (!op)
          continue;
        bool ok = false;
        if (auto mux = dyn_cast<comb::MuxOp>(op))
          ok = mapPerBit(top, *lib, op, kMuxTable,
                         {mux.getCond(), mux.getTrueValue(),
                          mux.getFalseValue()});
        else if (isa<comb::AndOp>(op))
          ok = mapPerBit(top, *lib, op, kAndTable,
                         {op->getOperand(0), op->getOperand(1)});
        else if (isa<comb::OrOp>(op))
          ok = mapPerBit(top, *lib, op, kOrTable,
                         {op->getOperand(0), op->getOperand(1)});
        else if (isa<comb::XorOp>(op))
          ok = mapPerBit(top, *lib, op, kXorTable,
                         {op->getOperand(0), op->getOperand(1)});
        if (ok)
          ++mapped;
        else
          failed = true;
      }

      // Loud visibility for any OTHER leftover comb logic this pass does
      // not know how to map -- it would resurface as an OpenSTA syntax
      // error at floorplan time otherwise.
      for (Operation &op : *hwmod.getBodyBlock()) {
        if (op.getDialect() &&
            op.getDialect()->getNamespace() == "comb" &&
            !isa<comb::ExtractOp, comb::ConcatOp, comb::ReplicateOp>(op)) {
          op.emitRemark("map-shell-logic: unhandled leftover comb op "
                        "(will export as an operator expression)");
          ++skipped;
        }
      }
    }

    if (failed)
      return signalPassFailure();
    if (mapped > 0 || skipped > 0)
      llvm::errs() << "[map-shell-logic] mapped " << mapped
                   << " leftover op(s), " << skipped << " unhandled\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapShellLogicPass() {
  return std::make_unique<MapShellLogicPass>();
}

} // namespace circt_abc
