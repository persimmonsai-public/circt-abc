//===- RegisterMapperPass.cpp ----------------------------------*- C++ -*-===//
//
// See Passes.td's RegisterMapper description for the "why". Must run AFTER
// register-legalize (see RegisterLegalizePass.cpp): by the time this pass
// runs, every remaining `seq.firreg`'s reset (if any) is guaranteed to be
// async with a CONSTANT resetValue -- exactly the 2 shapes
// (no-reset / async-constant-reset) the three flip-flop classes this pass
// targets can express (see LibertyLibrary.h's FFCellPattern: a plain flop
// with no reset, an async clear-to-0 flop and an async preset-to-1 flop).
//
// Algorithm, per seq::FirRegOp of bitwidth N:
//   1. Build ONE `seq.from_clock` cast of the firreg's clk, shared by all N
//      per-bit cell instances.
//   2. For each bit i in [0, N):
//      - `comb.extract` bit i of `next` as the D input.
//      - Pick the cell class: no reset -> plain flop (no reset pin);
//        reset present and constant resetValue bit i == 0 -> async-clear
//        flop (async clear, wired directly to the firreg's `reset`,
//        both active-high by seq.firreg's own convention -- no inverter
//        needed); reset present and bit i == 1 -> async-preset flop (async
//        preset, whose pin is active-LOW, so wired through a
//        `comb.xor`-based inverter of the firreg's `reset`).
//      - Instantiate the (cached, deduplicated-by-name) `hw.module.extern`
//        for that cell, operand order = [clock, data, (reset-or-set)]
//        matching the port order `getOrCreateExternDecl` declared it with.
//      A library without such cells (ASAP7's flops all output the
//      complement of D, and its one async-reset flop has both clear and
//      preset) is mapped through LibertyLibrary's fallback shapes: D is
//      driven with the complement of the next value (ABC absorbs the
//      inverter into the logic feeding it), and an unused async pin is
//      tied to its inactive level (both of them, for a no-reset register
//      on a library like GT3 whose one flop has clear and preset).
//   3. Reassemble the N per-bit Q results into the register's original
//      `data` result via a single MSB-first `comb.concat` (operand 0 =
//      most-significant bit N-1, ..., last operand = bit 0 -- comb.concat's
//      operand order is MSB-first, confirmed empirically elsewhere in this
//      project), then erase the firreg.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "../StructuralMapper/LibertyLibrary.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct RegisterMapperPass
    : public PassWrapper<RegisterMapperPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RegisterMapperPass)

  RegisterMapperPass() = default;
  // PassWrapper::clonePass() requires copy-constructibility, but
  // mlir::Pass::Option's own copy ctor is deleted -- same explicit-copy-ctor
  // pattern as ABCBridge.cpp/ExternalizeMappedCellsPass.cpp/
  // StructuralMapperPass.cpp all already use for this reason.
  RegisterMapperPass(const RegisterMapperPass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
  }

  StringRef getArgument() const final { return "register-mapper"; }
  StringRef getDescription() const final {
    return "Bit-blast seq.firreg ops to real liberty flip-flop cells";
  }

  // Hand-rolled PassWrapper (see ABCBridge.cpp's header comment for why
  // Passes.td's `dependentDialects` field is documentation-only for these
  // classes) -- must manually declare every dialect this pass's IR-building
  // code touches.
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Path to the liberty file whose ff() groups "
                      "supply the flip-flop cells to map to")};

  hw::HWModuleExternOp getOrCreateExternDecl(ModuleOp module,
                                              const FFCellPattern &cell) {
    if (auto existing = externDecls.lookup(cell.name))
      return existing;
    // The design may already declare this cell (RTL that instantiates the
    // library flop directly). Reuse that declaration when its ports are the
    // cell's pins -- the call site wires operands by port name, so its port
    // order does not matter -- and refuse clearly when they differ.
    if (auto *sym = SymbolTable::lookupSymbolIn(module, cell.name)) {
      auto ext = dyn_cast<hw::HWModuleExternOp>(sym);
      if (!ext) {
        sym->emitError() << "register-mapper: '" << cell.name
                         << "' is defined here as a module, not the library cell";
        return {};
      }
      llvm::StringSet<> want{cell.clockPin, cell.dataPin};
      if (cell.hasReset)
        want.insert(cell.resetPin);
      for (const auto &tie : cell.tiedPins)
        want.insert(tie.first);
      llvm::StringSet<> haveIn;
      bool outOk = false;
      for (auto port : ext.getPortList()) {
        if (port.dir == hw::ModulePort::Direction::Input)
          haveIn.insert(port.name.getValue());
        else if (port.name.getValue() == cell.outputPin)
          outOk = true;
      }
      bool same = outOk && haveIn.size() == want.size();
      for (auto &w : want)
        same &= haveIn.contains(w.getKey());
      if (!same) {
        ext.emitError() << "register-mapper: existing declaration of '"
                        << cell.name << "' does not have the cell's pins";
        return {};
      }
      externDecls[cell.name] = ext;
      return ext;
    }

    Type i1 = IntegerType::get(module.getContext(), 1);
    SmallVector<hw::PortInfo, 4> ports;
    auto addInput = [&](StringRef name, unsigned argNum) {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), name);
      port.type = i1;
      port.dir = hw::ModulePort::Direction::Input;
      port.argNum = argNum;
      ports.push_back(port);
    };
    // The call site wires operands by port name, in this declaration's
    // input order (clock, data, [reset/set], [tied pins]).
    addInput(cell.clockPin, 0);
    addInput(cell.dataPin, 1);
    unsigned argNum = 2;
    if (cell.hasReset)
      addInput(cell.resetPin, argNum++);
    for (const auto &tie : cell.tiedPins)
      addInput(tie.first, argNum++);

    hw::PortInfo outPort;
    outPort.name = StringAttr::get(module.getContext(), cell.outputPin);
    outPort.type = i1;
    outPort.dir = hw::ModulePort::Direction::Output;
    outPort.argNum = 0;
    ports.push_back(outPort);

    OpBuilder builder = OpBuilder::atBlockBegin(module.getBody());
    auto externOp = hw::HWModuleExternOp::create(
        builder, module.getLoc(), StringAttr::get(module.getContext(), cell.name),
        ports);
    externDecls[cell.name] = externOp;
    return externOp;
  }

  // Bit-blasts one firreg; returns false (and signals pass failure) on an
  // unsupported shape.
  bool mapFirReg(seq::FirRegOp firreg, ModuleOp topModule,
                 const LibertyLibrary &lib) {
    Type origType = firreg.getNext().getType();
    Location loc = firreg.getLoc();
    OpBuilder builder(firreg);

    // Array/struct-typed registers (e.g. a small per-lane register bank
    // like `!hw.array<3xi32>`) are bit-blasted the same way as a plain
    // integer: reinterpret the whole thing as one flat integer via
    // `hw.bitcast` (a real HW dialect op for exactly this, "reinterpret
    // one value as another of the same size" -- confirmed on the real
    // ibex design, which has array-typed firregs this pass must handle,
    // not a hypothetical edge case), do the ordinary per-bit logic below
    // in flat-integer space, then bitcast the reconstructed result back to
    // `origType` at the end. `createOrFold` (not plain `create`) matters
    // for the resetValue bitcast specifically: a constant aggregate
    // (`hw.aggregate_constant`) bitcast to an integer folds directly to a
    // plain `hw.constant` (BitcastOp has `hasFolder = true`), which is what
    // lets the existing `matchPattern(..., m_ConstantInt(...))` constant
    // check below keep working unmodified for the array case too.
    auto intType = dyn_cast<IntegerType>(origType);
    Value flatNext = firreg.getNext();
    Value flatResetValue = firreg.getResetValue();
    if (!intType) {
      int64_t bitWidth = hw::getBitWidth(origType);
      if (bitWidth <= 0) {
        firreg.emitError("register-mapper: firreg type ")
            << origType << " has no known flat bit width";
        return false;
      }
      intType = builder.getIntegerType(bitWidth);
      flatNext = builder.createOrFold<hw::BitcastOp>(loc, intType, flatNext);
      if (flatResetValue)
        flatResetValue =
            builder.createOrFold<hw::BitcastOp>(loc, intType, flatResetValue);
    }
    unsigned width = intType.getWidth();

    Value reset = firreg.getReset();
    APInt constResetValue;
    if (reset) {
      bool isConstant =
          matchPattern(flatResetValue, m_ConstantInt(&constResetValue));
      if (!isConstant || !firreg.getIsAsync()) {
        firreg.emitError(
            "register-mapper: expected no reset, or an async reset with a "
            "constant resetValue (did register-legalize run first?)");
        return false;
      }
      constResetValue = constResetValue.zextOrTrunc(width);
    }

    Value clockI1 = seq::FromClockOp::create(builder, loc, firreg.getClk());
    Value invertedReset; // lazily built xor(reset, 1), for preset-class bits
    Value oneConst, zeroConst; // lazily built, for inverted D / tied pins
    auto constant = [&](bool v) {
      Value &c = v ? oneConst : zeroConst;
      if (!c)
        c = hw::ConstantOp::create(builder, loc, APInt(1, v));
      return c;
    };

    std::string namePrefix;
    if (!firreg.getName().empty())
      namePrefix = firreg.getName().str();
    else
      namePrefix = ("firreg" + Twine(anonFirRegCounter++)).str();

    Type i1Type = builder.getIntegerType(1);
    SmallVector<Value> qBits(width);
    for (unsigned i = 0; i < width; ++i) {
      Value dBit =
          comb::ExtractOp::create(builder, loc, i1Type, flatNext, i);

      const FFCellPattern *cellPat = !reset               ? lib.lookupFFNoReset()
                                      : !constResetValue[i] ? lib.lookupFFClear()
                                                             : lib.lookupFFSet();
      if (!cellPat) {
        firreg.emitError("register-mapper: liberty file ")
            << libertyPath.getValue()
            << " has no flip-flop cell matching this firreg's reset shape";
        return false;
      }

      // Whichever class got picked, we already know we WANT its reset/set
      // pin to assert exactly when the firreg's (always active-high)
      // `reset` is 1 -- lookupFFClear() was only used for a bit whose
      // constant resetValue is 0, and lookupFFSet() only for a bit whose
      // constant resetValue is 1, so the pin's OWN drive condition, not
      // "clear vs. preset", is what decides whether to wire `reset`
      // directly or through an inverter: resetActiveHigh (parsed from the
      // liberty `clear`/`preset` expression's polarity, not hard-coded per
      // class) means the pin asserts on a bare 1, so `reset` wires
      // straight through; otherwise it asserts on 0, so it needs `reset`'s
      // complement, built once and shared across every such bit of this
      // firreg (the same comb.xor-based inverter idiom
      // StructuralMapperPass.cpp already uses for its own output
      // complementation).
      Value resetOperand;
      if (cellPat->hasReset) {
        if (cellPat->resetActiveHigh) {
          resetOperand = reset;
        } else {
          if (!invertedReset) {
            Value trueConst = hw::ConstantOp::create(builder, loc, APInt(1, 1));
            invertedReset = comb::XorOp::create(builder, loc, reset, trueConst);
          }
          resetOperand = invertedReset;
        }
      }

      hw::HWModuleExternOp externOp = getOrCreateExternDecl(topModule, *cellPat);
      if (!externOp)
        return false;
      // Operands by port name, in the declaration's input order (a reused
      // declaration from the design may order the pins differently).
      llvm::StringMap<Value> byPin;
      byPin[cellPat->clockPin] = clockI1;
      byPin[cellPat->dataPin] =
          cellPat->dataInverted
              ? comb::XorOp::create(builder, loc, dBit, constant(true))
                    .getResult()
              : dBit;
      if (cellPat->hasReset)
        byPin[cellPat->resetPin] = resetOperand;
      for (const auto &tie : cellPat->tiedPins)
        byPin[tie.first] = constant(tie.second);
      SmallVector<Value, 4> operands;
      for (auto port : externOp.getPortList())
        if (port.dir == hw::ModulePort::Direction::Input)
          operands.push_back(byPin.lookup(port.name.getValue()));

      std::string instName = (namePrefix + "_bit" + Twine(i)).str();
      auto inst = hw::InstanceOp::create(builder, loc, externOp,
                                          builder.getStringAttr(instName),
                                          operands);
      qBits[i] = inst.getResult(0);

      ++perCellCounts[cellPat->name];
      ++totalBitInstances;
    }

    Value reconstructed;
    if (width == 1) {
      reconstructed = qBits[0];
    } else {
      SmallVector<Value> concatOperands;
      for (unsigned i = width; i-- > 0;)
        concatOperands.push_back(qBits[i]); // MSB-first operand order.
      reconstructed = comb::ConcatOp::create(builder, loc, concatOperands);
    }

    if (origType != reconstructed.getType())
      reconstructed = hw::BitcastOp::create(builder, loc, origType, reconstructed);
    firreg.getData().replaceAllUsesWith(reconstructed);
    firreg.erase();
    ++numFirregsMapped;
    return true;
  }

  void runOnOperation() override {
    ModuleOp module = getOperation();
    if (libertyPath.empty()) {
      module.emitError("register-mapper requires --liberty=");
      return signalPassFailure();
    }

    auto loaded = LibertyLibrary::loadFromFile(libertyPath);
    if (!loaded) {
      module.emitError("register-mapper: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    LibertyLibrary lib = std::move(*loaded);
    externDecls.clear();
    perCellCounts.clear();
    totalBitInstances = 0;
    numFirregsMapped = 0;
    anonFirRegCounter = 0;

    for (auto hwModule :
         llvm::make_early_inc_range(module.getOps<hw::HWModuleOp>())) {
      for (auto firreg : llvm::make_early_inc_range(
               hwModule.getBodyBlock()->getOps<seq::FirRegOp>())) {
        if (!mapFirReg(firreg, module, lib))
          return signalPassFailure();
      }
    }

    std::string breakdown;
    llvm::raw_string_ostream os(breakdown);
    bool first = true;
    for (auto &entry : perCellCounts) {
      if (!first)
        os << ", ";
      first = false;
      os << entry.second << " " << entry.first();
    }
    llvm::errs() << "[register-mapper] mapped " << numFirregsMapped
                 << " firreg(s) -> " << totalBitInstances
                 << " bit-instance(s) (" << os.str() << ")\n";
  }

private:
  llvm::StringMap<hw::HWModuleExternOp> externDecls;
  llvm::StringMap<unsigned> perCellCounts;
  size_t totalBitInstances = 0;
  size_t numFirregsMapped = 0;
  unsigned anonFirRegCounter = 0;
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createRegisterMapperPass() {
  return std::make_unique<RegisterMapperPass>();
}

} // namespace circt_abc
