//===- FuseHalfAddersPass.cpp --------------------------------*- C++ -*-===//
//
// `--fuse-half-adders`: post-mapping peephole that replaces an AND2 + XOR2
// pair sharing BOTH operands with one two-output half-adder cell (HA).
//
// WHY THIS CANNOT COME FROM THE MAPPER. An HA is a two-output cell (`CO = A&B`,
// `S = A^B`). ABC reads it -- `read_lib` even warns "Detected N multi-output
// cells" -- but every cell-set builder then drops it: `Mio_CollectRoots`,
// `Mio_CollectRootsNew` and `Mio_CollectRootsNew2` all carry
// `if (pGate->pTwin) // skip large and multi-output`, and `&nf` reaches its
// cell set through `Mio_CollectRootsNewDefault2` (giaNf.c:320). `&nf` also
// matches single-output cuts, so a two-output cell could never be a cut's match
// even if it survived. Consequence: a netlist mapped by `&nf` contains no half-
// or full-adder cells at all. ABC's own `amap` mapper DOES link multi-output
// cells as twins (amapRead.c), so the capability exists upstream -- just not on
// the path this flow uses.
//
// WHY A PEEPHOLE IS SOUND HERE. The substitution is a pure re-encoding of
// two gates the mapper already chose: the AND2's result becomes the HA's
// CO, the XOR2's becomes its S, and both read the same two nets. No logic
// is restructured and no net is re-timed, so equivalence is structural.
//
// The gain is small: a library's HA cell is typically somewhat smaller than the
// AND2 + XOR2 it replaces, and a CPU core has on the order of a hundred such
// pairs. The pass refuses any substitution that does not strictly reduce area,
// so a liberty whose HA is not cheaper than its parts is a silent no-op rather
// than a regression.
//
// Conservative guards (when in doubt, leave the pair alone):
//   - both instances must reference an `hw.module.extern` (a real cell
//     declaration), not an ordinary `hw.module` sharing a cell's name;
//   - exactly two operands and one result each, all i1;
//   - neither carries an inner symbol (someone may refer to it
//     hierarchically);
//   - both must live in the same block, and the HA is inserted at the
//     LATER of the two so neither result is used before it is defined;
//   - each instance is fused at most once (an AND2 feeding two different
//     XOR2s over the same operands yields one fusion, not two).
//
// Cell identification is liberty-driven, never a name list: the AND2 half
// is any arity-2 cell whose truth table is exactly 0b1000, the XOR2 half
// exactly 0b0110, and the HA is LibertyLibrary's cheapest
// HalfAdderCellPattern. Same oracle discipline as every other
// liberty-consuming pass here.
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
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Truth tables over (bit0 = first input, bit1 = second input).
static constexpr TruthTable kAndTable = 8; // a & b
static constexpr TruthTable kXorTable = 6; // a ^ b

struct FuseHalfAddersPass
    : public PassWrapper<FuseHalfAddersPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(FuseHalfAddersPass)

  FuseHalfAddersPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  FuseHalfAddersPass(const FuseHalfAddersPass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    enableFusion = other.enableFusion.getValue();
  }

  StringRef getArgument() const final { return "fuse-half-adders"; }
  StringRef getDescription() const final {
    return "Replace an AND2+XOR2 pair over the same two operands with one "
           "two-output half-adder cell -- ABC's mappers skip multi-output "
           "cells by construction, so this is the only way an HA reaches "
           "the netlist";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file used to derive the AND2/XOR2/HA cell "
                     "set -- same file abc-bridge maps against")};

  Option<bool> enableFusion{
      *this, "enable-half-adder-fusion",
      llvm::cl::desc("Enable half-adder fusion. OFF by default per the "
                     "project convention: a new netlist-changing pass "
                     "lands off and is promoted only on measured N>=2 "
                     "evidence plus an equivalence gate."),
      llvm::cl::init(false)};

  void runOnOperation() override {
    if (!enableFusion.getValue())
      return;
    if (libertyPath.getValue().empty()) {
      getOperation().emitError("fuse-half-adders: liberty= is required when "
                               "enable-half-adder-fusion is set");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      getOperation().emitError("fuse-half-adders: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    const HalfAdderCellPattern *ha = lib->lookupHalfAdder();
    if (!ha)
      return; // this liberty has no half-adder cell: nothing to do

    mlir::ModuleOp top = getOperation();
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);

    // Cell-name sets, restricted to names ALSO declared as hw.module.extern
    // in this design (guards against an ordinary hw.module coincidentally
    // sharing a cell's name -- the same guard remove-identity-buffers uses).
    llvm::DenseSet<StringRef> andNames, xorNames;
    for (StringRef n : lib->cellNamesWithTable(2, kAndTable))
      andNames.insert(n);
    for (StringRef n : lib->cellNamesWithTable(2, kXorTable))
      xorNames.insert(n);
    llvm::DenseSet<StringAttr> andExterns, xorExterns;
    for (auto ext : top.getOps<hw::HWModuleExternOp>()) {
      if (andNames.contains(ext.getModuleName()))
        andExterns.insert(StringAttr::get(ctx, ext.getModuleName()));
      if (xorNames.contains(ext.getModuleName()))
        xorExterns.insert(StringAttr::get(ctx, ext.getModuleName()));
    }
    if (andExterns.empty() || xorExterns.empty())
      return;

    // Declare the HA extern once, reusing a pre-existing decl if some
    // earlier pass already emitted one.
    hw::HWModuleExternOp haExtern;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == ha->name) {
        haExtern = ext;
        break;
      }

    auto isFusable = [&](hw::InstanceOp inst,
                         const llvm::DenseSet<StringAttr> &externs) {
      if (!externs.contains(inst.getModuleNameAttr().getAttr()))
        return false;
      if (inst.getNumOperands() != 2 || inst.getNumResults() != 1)
        return false;
      if (inst.getOperand(0).getType() != i1 ||
          inst.getOperand(1).getType() != i1 ||
          inst.getResult(0).getType() != i1)
        return false;
      if (inst.getInnerSymAttr())
        return false;
      // A gate whose two inputs are the same net is not half of an adder
      // (a&a == a, a^a == 0); leave it for canonicalization.
      return inst.getOperand(0) != inst.getOperand(1);
    };

    // Operand pair, order-normalised: both a&b and a^b are commutative, so
    // an AND2 and an XOR2 wired in opposite pin order are still a pair.
    auto keyOf = [](hw::InstanceOp inst) {
      Value a = inst.getOperand(0), b = inst.getOperand(1);
      if (a.getAsOpaquePointer() > b.getAsOpaquePointer())
        std::swap(a, b);
      return std::make_pair(a, b);
    };

    double andArea = 0.0, xorArea = 0.0;
    unsigned fused = 0;

    for (auto hwmod : top.getOps<hw::HWModuleOp>()) {
      // Index the XOR2 instances of each block by operand pair, then walk
      // the AND2s looking for a partner. Blocks are keyed alongside the
      // operands so a pair is never fused across block boundaries.
      llvm::DenseMap<std::pair<Value, Value>, SmallVector<hw::InstanceOp, 2>>
          xorsByKey;
      for (auto inst : hwmod.getBodyBlock()->getOps<hw::InstanceOp>())
        if (isFusable(inst, xorExterns))
          xorsByKey[keyOf(inst)].push_back(inst);
      if (xorsByKey.empty())
        continue;

      SmallVector<std::pair<hw::InstanceOp, hw::InstanceOp>> pairs;
      llvm::DenseSet<Operation *> claimed;
      for (auto inst : hwmod.getBodyBlock()->getOps<hw::InstanceOp>()) {
        if (!isFusable(inst, andExterns))
          continue;
        auto it = xorsByKey.find(keyOf(inst));
        if (it == xorsByKey.end())
          continue;
        for (hw::InstanceOp x : it->second) {
          if (claimed.contains(x.getOperation()) || x.getOperation() == inst)
            continue;
          // Only strictly-profitable substitutions.
          double a = lib->areaOf(inst.getModuleName());
          double b = lib->areaOf(x.getModuleName());
          if (a <= 0.0 || b <= 0.0 || ha->area >= a + b)
            continue;
          claimed.insert(x.getOperation());
          claimed.insert(inst.getOperation());
          andArea += a;
          xorArea += b;
          pairs.push_back({inst, x});
          break;
        }
      }

      for (auto [andInst, xorInst] : pairs) {
        if (!haExtern) {
          SmallVector<hw::PortInfo, 4> ports;
          for (auto [idx, pinName] : llvm::enumerate(ha->inputPins)) {
            hw::PortInfo p;
            p.name = StringAttr::get(ctx, pinName);
            p.type = i1;
            p.dir = hw::ModulePort::Direction::Input;
            p.argNum = idx;
            ports.push_back(p);
          }
          // Output order is fixed here and mirrored by the result indices
          // used below: 0 = CO (carry, the AND2's value), 1 = S (sum, the
          // XOR2's value).
          // A named array, not `ArrayRef<std::string>{...}`: an ArrayRef built
          // from a braced list points at a temporary that dies at the end of
          // the full expression, and llvm::enumerate holds it across the
          // loop (crashes in StringAttr::get on the dangling string).
          const std::string haOutPins[2] = {ha->carryPin, ha->sumPin};
          for (auto [idx, pinName] : llvm::enumerate(haOutPins)) {
            hw::PortInfo p;
            p.name = StringAttr::get(ctx, pinName);
            p.type = i1;
            p.dir = hw::ModulePort::Direction::Output;
            p.argNum = idx;
            ports.push_back(p);
          }
          OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
          haExtern = hw::HWModuleExternOp::create(
              b, top.getLoc(), StringAttr::get(ctx, ha->name), ports);
        }

        // Insert at the LATER of the two instances: both results are
        // defined there, so no use precedes its definition.
        Operation *later = andInst->isBeforeInBlock(xorInst.getOperation())
                               ? xorInst.getOperation()
                               : andInst.getOperation();
        OpBuilder b(later);
        b.setInsertionPointAfter(later);
        auto inst = hw::InstanceOp::create(
            b, later->getLoc(), haExtern,
            StringAttr::get(ctx, (Twine("ha_") + Twine(fused)).str()),
            ArrayRef<Value>{andInst.getOperand(0), andInst.getOperand(1)});
        andInst.getResult(0).replaceAllUsesWith(inst.getResult(0)); // CO
        xorInst.getResult(0).replaceAllUsesWith(inst.getResult(1)); // S
        andInst.erase();
        xorInst.erase();
        ++fused;
      }
    }

    if (fused > 0)
      llvm::errs() << "[fuse-half-adders] fused " << fused
                   << " AND2+XOR2 pair(s) into " << ha->name << " ("
                   << llvm::format("%.2f", andArea + xorArea - fused * ha->area)
                   << " um^2 saved)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createFuseHalfAddersPass() {
  return std::make_unique<FuseHalfAddersPass>();
}

} // namespace circt_abc
