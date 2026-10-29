//===- MapAdderChainsPass.cpp ---------------------------------*- C++ -*-===//
//
// `--map-adder-chains`: lower a wide `comb.add` straight into a chain of the
// library's own two-output adder cells (a half adder for bit 0, full adders
// for the rest), instead of letting it be bit-blasted into an AIG and
// rediscovered -- or not -- by cut covering.
//
// WHY. ABC's `&nf` can NEVER emit an adder cell: `Mio_CollectRootsNew2`
// (mioUtils.c) skips every gate with a `pTwin`, i.e. every multi-output
// cell, so the whole full/half-adder family is invisible to the mapper. The
// emap path does support two-output cells, but it only finds an adder where
// the cut boundaries happen to line up, so part of a carry chain is covered
// by adder cells and the remaining bits cost two to three generic gates
// each. Building the chain out of adder cells by construction -- which is
// what a datapath synthesiser does -- removes that difference.
//
// SHAPE. A plain ripple, deliberately: it is the structure a full-adder cell
// exists to serve (its CI->CO arc is typically the fastest carry path a
// library offers), and a parallel-prefix form cannot use two-output adder
// cells at all. Adds whose operand is constant are skipped -- an increment
// is not a carry chain of full adders and the constant folds away.
//
// OFF by default per the project convention: a netlist-changing pass lands
// off and is promoted only on measured evidence plus an equivalence gate.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct MapAdderChainsPass
    : public PassWrapper<MapAdderChainsPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MapAdderChainsPass)

  MapAdderChainsPass() = default;
  MapAdderChainsPass(const MapAdderChainsPass &o) : PassWrapper(o) {
    libertyPath = o.libertyPath.getValue();
    enablePass = o.enablePass.getValue();
    minWidth = o.minWidth.getValue();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file supplying the full/half-adder cell shapes"),
      llvm::cl::init("")};
  Option<bool> enablePass{
      *this, "enable-adder-chains",
      llvm::cl::desc("Enable adder-chain mapping (OFF by default)"),
      llvm::cl::init(false)};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("only lower comb.add at or above this operand width")};
  Option<bool> lowerIncrements{
      *this, "increments", llvm::cl::init(true),
      llvm::cl::desc("also lower `x + 1` into an INV + half-adder chain "
                     "(an incrementer's carry chain is exactly a chain of "
                     "half adders)")};

  StringRef getArgument() const final { return "map-adder-chains"; }
  StringRef getDescription() const final {
    return "Lower wide comb.add into a ripple chain of the library's "
           "two-output half/full-adder cells, which ABC's mapper cannot emit";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  // Finds (or creates) the extern declaration for a two-output adder cell and
  // returns the port indices its instances will use. Reusing a declaration
  // some other pass already emitted is the common case, and its port ORDER is
  // not ours to assume -- look the pins up by name.
  struct CellDecl {
    hw::HWModuleExternOp op;
    SmallVector<unsigned, 3> inIdx; // per inputPins[]
    unsigned sumIdx = 0, carryIdx = 0;
    // False when a pin name did not resolve against the declaration we found
    // -- e.g. some other pass declared the same cell with different port
    // names. Wiring by a guessed index would silently mis-connect the
    // netlist, so the pass declines to rewrite instead.
    bool valid = false;
  };

  CellDecl getOrCreateDecl(mlir::ModuleOp top, StringRef name,
                           ArrayRef<std::string> inputPins, StringRef sumPin,
                           StringRef carryPin) {
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);
    CellDecl decl;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == name) {
        decl.op = ext;
        break;
      }
    if (!decl.op) {
      SmallVector<hw::PortInfo, 5> ports;
      for (auto [idx, pin] : llvm::enumerate(inputPins)) {
        hw::PortInfo p;
        p.name = StringAttr::get(ctx, pin);
        p.type = i1;
        p.dir = hw::ModulePort::Direction::Input;
        p.argNum = idx;
        ports.push_back(p);
      }
      // NOT `ArrayRef<StringRef>{carryPin, sumPin}`: an ArrayRef built from a
      // braced list points at a temporary array that dies at the end of the
      // full expression, and llvm::enumerate holds it across the loop --
      // which crashes in StringAttr::get on a dangling StringRef.
      const StringRef outPins[2] = {carryPin, sumPin};
      for (auto [idx, pin] : llvm::enumerate(outPins)) {
        hw::PortInfo p;
        p.name = StringAttr::get(ctx, pin);
        p.type = i1;
        p.dir = hw::ModulePort::Direction::Output;
        p.argNum = idx;
        ports.push_back(p);
      }
      OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
      decl.op = hw::HWModuleExternOp::create(b, top.getLoc(),
                                             StringAttr::get(ctx, name), ports);
    }
    // Resolve pin names to operand/result positions on the declaration we
    // are actually going to instantiate.
    llvm::StringMap<unsigned> inPos, outPos;
    unsigned nIn = 0, nOut = 0;
    for (const hw::PortInfo &port : decl.op.getPortList()) {
      if (port.dir == hw::ModulePort::Direction::Output)
        outPos[port.getName()] = nOut++;
      else
        inPos[port.getName()] = nIn++;
    }
    for (StringRef pin : inputPins) {
      auto it = inPos.find(pin);
      if (it == inPos.end()) {
        decl.valid = false;
        return decl;
      }
      decl.inIdx.push_back(it->second);
    }
    auto s = outPos.find(sumPin), c = outPos.find(carryPin);
    if (s == outPos.end() || c == outPos.end() || nIn != inputPins.size() ||
        nOut != 2) {
      decl.valid = false;
      return decl;
    }
    decl.sumIdx = s->second;
    decl.carryIdx = c->second;
    decl.valid = true;
    return decl;
  }

  void runOnOperation() override {
    if (!enablePass.getValue())
      return;
    mlir::ModuleOp top = getOperation();
    if (libertyPath.getValue().empty()) {
      top.emitError("map-adder-chains: liberty= is required when "
                    "enable-adder-chains is set");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      top.emitError("map-adder-chains: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    const FullAdderCellPattern *fa = lib->lookupFullAdder();
    const HalfAdderCellPattern *ha = lib->lookupHalfAdder();
    if (!fa) {
      // Without a full adder there is no chain to build; a half adder alone
      // only covers bit 0. Silently do nothing, like every other
      // liberty-driven pass here.
      return;
    }

    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);

    // Collect first: the rewrite erases the AddOp it is walking.
    SmallVector<comb::AddOp> targets;
    for (auto hwmod : top.getOps<hw::HWModuleOp>())
      hwmod.walk([&](comb::AddOp add) {
        if (add.getNumOperands() != 2)
          return; // variadic adds are lowered to pairs upstream
        auto ty = dyn_cast<IntegerType>(add.getType());
        if (!ty || ty.getWidth() < minWidth.getValue())
          return;
        // A constant operand is an increment, not a two-addend carry chain.
        // `x + 1` still has a real carry chain -- and it is exactly a chain of
        // HALF adders (s_i = x_i ^ c, c_i = x_i & c), which cut covering never
        // emits -- so take it when `increments` is on and the constant is
        // exactly 1. Any other constant needs per-bit XNOR/OR logic that no
        // single library cell provides, so leave those to ordinary covering.
        unsigned nConst = 0;
        bool isPlusOne = false;
        for (Value v : add.getOperands())
          if (auto c = v.getDefiningOp<hw::ConstantOp>()) {
            ++nConst;
            isPlusOne = c.getValue().isOne();
          }
        if (nConst > 1)
          return;
        if (nConst == 1 && !(lowerIncrements.getValue() && isPlusOne))
          return;
        targets.push_back(add);
      });
    if (targets.empty())
      return;

    CellDecl faDecl = getOrCreateDecl(
        top, fa->name,
        ArrayRef<std::string>(fa->inputPins.begin(), fa->inputPins.end()),
        fa->sumPin, fa->carryPin);
    if (!faDecl.valid) {
      top.emitWarning("map-adder-chains: existing declaration of ")
          << fa->name << " does not match the liberty pin names; skipping";
      return;
    }
    std::optional<CellDecl> haDecl;
    if (ha) {
      CellDecl d = getOrCreateDecl(
          top, ha->name,
          ArrayRef<std::string>(ha->inputPins.begin(), ha->inputPins.end()),
          ha->sumPin, ha->carryPin);
      if (d.valid)
        haDecl = d;
    }

    unsigned nAdders = 0, nFull = 0, nHalf = 0;
    double area = 0.0;
    for (comb::AddOp add : targets) {
      OpBuilder b(add);
      Location loc = add.getLoc();
      Value lhs = add.getOperand(0), rhs = add.getOperand(1);
      unsigned width = cast<IntegerType>(add.getType()).getWidth();

      // `x + 1`: bit 0 is s0 = ~x0 with carry-out x0 (no cell needed beyond
      // the inverter), and every higher bit is HA(x_i, carry). Without a
      // half adder in the liberty there is nothing to build it from, so fall
      // through to the generic path (which the target filter has already
      // excluded, so this simply cannot fire).
      bool increment = false;
      if (auto c = rhs.getDefiningOp<hw::ConstantOp>())
        increment = true;
      else if (auto c = lhs.getDefiningOp<hw::ConstantOp>()) {
        increment = true;
        std::swap(lhs, rhs); // keep the variable operand in `lhs`
      }
      if (increment) {
        if (!haDecl)
          continue;
        SmallVector<Value> isums(width);
        Value icarry;
        for (unsigned i = 0; i < width; ++i) {
          Value xi = comb::ExtractOp::create(b, loc, i1, lhs, i);
          if (i == 0) {
            Value one = hw::ConstantOp::create(b, loc, APInt(1, 1));
            isums[0] = comb::XorOp::create(b, loc, xi, one);
            icarry = xi;
            continue;
          }
          SmallVector<Value, 2> args(2);
          args[haDecl->inIdx[0]] = xi;
          args[haDecl->inIdx[1]] = icarry;
          auto hinst = hw::InstanceOp::create(
              b, loc, haDecl->op,
              StringAttr::get(ctx, (Twine("inc_ha_") + Twine(nAdders) + "_" +
                                    Twine(i))
                                       .str()),
              args);
          isums[i] = hinst.getResult(haDecl->sumIdx);
          icarry = hinst.getResult(haDecl->carryIdx);
          ++nHalf;
          area += ha->area;
        }
        SmallVector<Value> imsb(isums.rbegin(), isums.rend());
        Value iresult = comb::ConcatOp::create(b, loc, imsb);
        add.getResult().replaceAllUsesWith(iresult);
        add.erase();
        ++nAdders;
        continue;
      }

      SmallVector<Value> sums(width);
      Value carry;
      for (unsigned i = 0; i < width; ++i) {
        Value ai = comb::ExtractOp::create(b, loc, i1, lhs, i);
        Value bi = comb::ExtractOp::create(b, loc, i1, rhs, i);
        hw::InstanceOp inst;
        if (i == 0 && haDecl) {
          SmallVector<Value, 2> args(2);
          args[haDecl->inIdx[0]] = ai;
          args[haDecl->inIdx[1]] = bi;
          inst = hw::InstanceOp::create(
              b, loc, haDecl->op,
              StringAttr::get(ctx, (Twine("add_ha_") + Twine(nAdders)).str()),
              args);
          sums[i] = inst.getResult(haDecl->sumIdx);
          carry = inst.getResult(haDecl->carryIdx);
          ++nHalf;
          area += ha->area;
          continue;
        }
        if (i == 0) {
          // No half adder in this liberty: feed a constant-zero carry-in.
          carry = hw::ConstantOp::create(b, loc, APInt(1, 0));
        }
        SmallVector<Value, 3> args(3);
        args[faDecl.inIdx[0]] = ai;
        args[faDecl.inIdx[1]] = bi;
        args[faDecl.inIdx[2]] = carry;
        inst = hw::InstanceOp::create(
            b, loc, faDecl.op,
            StringAttr::get(ctx,
                            (Twine("add_fa_") + Twine(nAdders) + "_" + Twine(i))
                                .str()),
            args);
        sums[i] = inst.getResult(faDecl.sumIdx);
        carry = inst.getResult(faDecl.carryIdx);
        ++nFull;
        area += fa->area;
      }
      // comb.concat is MSB-first; sums[] is LSB-first.
      SmallVector<Value> msbFirst(sums.rbegin(), sums.rend());
      Value result = comb::ConcatOp::create(b, loc, msbFirst);
      add.getResult().replaceAllUsesWith(result);
      add.erase();
      ++nAdders;
    }

    llvm::errs() << "[map-adder-chains] lowered " << nAdders
                 << " comb.add op(s) to " << nFull << " " << fa->name
                 << " + " << nHalf << " "
                 << (ha ? ha->name : std::string("(none)")) << " ("
                 << llvm::format("%.2f", area) << " um^2)\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapAdderChainsPass() {
  return std::make_unique<MapAdderChainsPass>();
}

} // namespace circt_abc
