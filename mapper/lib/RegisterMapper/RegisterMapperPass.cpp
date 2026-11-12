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
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Regex.h"
#include "llvm/Support/raw_ostream.h"

#include <functional>
#include <map>
#include <tuple>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {


//===----------------------------------------------------------------------===//
// MultibitSpec: how a technology pack declares its multibit flops.
//
// The mapper never names a library cell. A pack states, in JSON:
//
//   { "default_family": "a",
//     "cross_register": false,            // pool leftover bits across regs
//     "overrides": [ {"reg": "<regex on the register name>",
//                     "family": "b"} ],  // first match wins
//     "families": {
//       "a": {
//         "clear": {                       // async clear-to-0 bits
//           "cell": "TMB{n}_A",         // {n} = bank width
//           "widths": [8, 4, 2],           // widths the library has
//           "clock": "CK", "reset": "R",   // reset pin is optional
//           "reset_active_high": true,
//           "data": "D{i}", "out": "Q{i}", // {i} = bit within the bank
//           "tie": {"SE": 0, "SI": "last_q"}   // 0, 1 or "last_q"
//         },
//         "none": { ... } } } }            // bits with no reset
//
// Only bits whose class the family declares are banked; async-set bits and
// classes a family leaves out stay single-bit cells. `default_family` is the
// static choice; `overrides` is the hook an activity-driven selection
// writes (a script maps a switching-activity file to register names).
// "last_q" ties a pin to the bank's own last output, which a pack uses for a
// scan-in pin that a constant-0 scan enable never selects (no tie cell).
//===----------------------------------------------------------------------===//
struct MbClass {
  std::string cellTmpl, clock, reset, data, out;
  bool hasReset = false;
  bool resetActiveHigh = true;
  SmallVector<unsigned, 4> widths; // descending
  // Pin -> 0 / 1 / -1 (own last output).
  SmallVector<std::pair<std::string, int>, 2> ties;
  // Optional single-bit cell of the class (`single: {cell, data, out}`): a
  // bit this class's banker leaves over becomes one instance of it, with the
  // class's clock, reset and tie pins, instead of the liberty's plain flop.
  std::shared_ptr<MbClass> single;
};
struct MbFamily {
  std::string name;
  std::map<std::string, MbClass> classes; // "clear", "none"
};
struct MbSpec {
  bool active = false;
  bool crossRegister = false;
  std::string defaultFamily;
  std::map<std::string, MbFamily> families;
  std::vector<std::pair<std::unique_ptr<llvm::Regex>, std::string>> overrides;

  static std::string subst(const std::string &tmpl, StringRef key,
                           unsigned v) {
    std::string out = tmpl;
    std::string k = ("{" + key + "}").str();
    size_t at;
    while ((at = out.find(k)) != std::string::npos)
      out.replace(at, k.size(), std::to_string(v));
    return out;
  }
  const MbClass *classFor(const std::string &fam, StringRef cls) const {
    auto f = families.find(fam);
    if (f == families.end())
      return nullptr;
    auto c = f->second.classes.find(cls.str());
    return c == f->second.classes.end() ? nullptr : &c->second;
  }
  std::string familyFor(StringRef regName) const {
    for (auto &o : overrides)
      if (o.first->match(regName))
        return o.second;
    return defaultFamily;
  }
  // Fills *this from JSON; returns an error message or "".
  std::string load(StringRef path) {
    auto buf = llvm::MemoryBuffer::getFile(path);
    if (!buf)
      return ("cannot read " + path).str();
    auto parsed = llvm::json::parse((*buf)->getBuffer());
    if (!parsed)
      return ("invalid JSON in " + path + ": " +
              llvm::toString(parsed.takeError())).str();
    auto *root = parsed->getAsObject();
    if (!root)
      return "top level must be an object";
    defaultFamily = root->getString("default_family").value_or("").str();
    crossRegister = root->getBoolean("cross_register").value_or(false);
    auto *fams = root->getObject("families");
    if (!fams)
      return "missing \"families\"";
    for (auto &kv : *fams) {
      MbFamily fam;
      fam.name = kv.first.str();
      auto *fo = kv.second.getAsObject();
      if (!fo)
        return "family " + fam.name + " must be an object";
      for (auto &ck : *fo) {
        auto *co = ck.second.getAsObject();
        if (!co)
          return "class " + ck.first.str() + " must be an object";
        MbClass c;
        c.cellTmpl = co->getString("cell").value_or("").str();
        c.clock = co->getString("clock").value_or("").str();
        c.data = co->getString("data").value_or("").str();
        c.out = co->getString("out").value_or("").str();
        if (c.cellTmpl.empty() || c.clock.empty() || c.data.empty() ||
            c.out.empty() || c.cellTmpl.find("{n}") == std::string::npos ||
            c.data.find("{i}") == std::string::npos ||
            c.out.find("{i}") == std::string::npos)
          return "class " + ck.first.str() + " of " + fam.name +
                 " needs cell ({n}), clock, data ({i}), out ({i}), widths";
        if (auto r = co->getString("reset")) {
          c.hasReset = true;
          c.reset = r->str();
        }
        if (ck.first == "clear" && !c.hasReset)
          return "class clear of " + fam.name + " needs a reset pin";
        c.resetActiveHigh = co->getBoolean("reset_active_high").value_or(true);
        auto *w = co->getArray("widths");
        if (!w || w->empty())
          return "class " + ck.first.str() + " of " + fam.name +
                 " needs widths";
        for (auto &x : *w) {
          auto n = x.getAsInteger();
          if (!n || *n < 2)
            return "widths must be integers >= 2";
          c.widths.push_back(*n);
        }
        llvm::sort(c.widths, std::greater<unsigned>());
        if (auto *t = co->getObject("tie"))
          for (auto &tv : *t) {
            if (auto n = tv.second.getAsInteger())
              c.ties.emplace_back(tv.first.str(), (int)*n);
            else if (tv.second.getAsString() &&
                     *tv.second.getAsString() == "last_q")
              c.ties.emplace_back(tv.first.str(), -1);
            else
              return "tie values are 0, 1 or \"last_q\"";
          }
        if (auto *so = co->getObject("single")) {
          auto sc = std::make_shared<MbClass>(c);
          sc->cellTmpl = so->getString("cell").value_or("").str();
          sc->data = so->getString("data").value_or("D").str();
          sc->out = so->getString("out").value_or("Q").str();
          if (sc->cellTmpl.empty())
            return "class " + ck.first.str() + " of " + fam.name +
                   ": single needs a cell";
          sc->widths.assign(1, 1);
          c.single = std::move(sc);
        }
        fam.classes[ck.first.str()] = std::move(c);
      }
      families[fam.name] = std::move(fam);
    }
    if (!families.count(defaultFamily))
      return "default_family " + defaultFamily + " is not a family";
    if (auto *ov = root->getArray("overrides"))
      for (auto &o : *ov) {
        auto *oo = o.getAsObject();
        if (!oo)
          return "overrides entries must be objects";
        auto re = oo->getString("reg").value_or("");
        auto f = oo->getString("family").value_or("");
        if (!families.count(f.str()))
          return "override family " + f.str() + " is not a family";
        auto rx = std::make_unique<llvm::Regex>(re);
        std::string err;
        if (!rx->isValid(err))
          return "override regex " + re.str() + ": " + err;
        overrides.emplace_back(std::move(rx), f.str());
      }
    active = true;
    return "";
  }
};

// One bank candidate bit of a register mapped in a multibit-capable run.
struct MbBit {
  unsigned reg;            // index into the pending registers
  unsigned bit;            // index within the register
  Value d;                 // the bit's D input
  Value clockI1;           // the register's from_clock cast
  Value reset;             // the register's (active-high) reset, or null
  Value resetOperand;      // what a single-bit cell's reset pin gets
  const FFCellPattern *single; // the single-bit cell, if the bit stays single
  const MbClass *cls;
  std::string family, className;
};
struct PendingReg {
  seq::FirRegOp firreg;
  Type origType;
  std::string name;
  SmallVector<Value> qBits;
  unsigned open = 0; // bits whose Q is not yet known
};

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
    multibitPath = other.multibitPath.getValue();
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
  Option<std::string> multibitPath{
      *this, "multibit",
      llvm::cl::desc("Path to a JSON multibit-flop spec (families, cell-name "
                     "templates, pin names, widths; see the MultibitSpec "
                     "comment in RegisterMapperPass.cpp). Empty: every "
                     "bit is a single-bit cell.")};

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

  // One single-bit cell instance; returns its Q (null on failure).
  Value emitSingle(OpBuilder &builder, Location loc, ModuleOp topModule,
                   const FFCellPattern &cellPat, Value clockI1, Value dBit,
                   Value resetOperand,
                   const std::function<Value(bool)> &constant,
                   StringRef instName) {
    hw::HWModuleExternOp externOp = getOrCreateExternDecl(topModule, cellPat);
    if (!externOp)
      return {};
    // Operands by port name, in the declaration's input order (a reused
    // declaration from the design may order the pins differently).
    llvm::StringMap<Value> byPin;
    byPin[cellPat.clockPin] = clockI1;
    byPin[cellPat.dataPin] =
        cellPat.dataInverted
            ? comb::XorOp::create(builder, loc, dBit, constant(true))
                  .getResult()
            : dBit;
    if (cellPat.hasReset)
      byPin[cellPat.resetPin] = resetOperand;
    for (const auto &tie : cellPat.tiedPins)
      byPin[tie.first] = constant(tie.second);
    SmallVector<Value, 4> operands;
    for (auto port : externOp.getPortList())
      if (port.dir == hw::ModulePort::Direction::Input)
        operands.push_back(byPin.lookup(port.name.getValue()));
    auto inst = hw::InstanceOp::create(builder, loc, externOp,
                                       builder.getStringAttr(instName),
                                       operands);
    ++perCellCounts[cellPat.name];
    ++totalBitInstances;
    return inst.getResult(0);
  }

  // The extern declaration of a multibit cell of width `n`, from the spec.
  hw::HWModuleExternOp getOrCreateMbDecl(ModuleOp module, const MbClass &c,
                                         unsigned n) {
    std::string name = MbSpec::subst(c.cellTmpl, "n", n);
    if (auto existing = externDecls.lookup(name))
      return existing;
    llvm::StringSet<> wantIn{c.clock};
    if (c.hasReset)
      wantIn.insert(c.reset);
    for (auto &t : c.ties)
      wantIn.insert(t.first);
    for (unsigned b = 0; b < n; ++b)
      wantIn.insert(MbSpec::subst(c.data, "i", b));
    if (auto *sym = SymbolTable::lookupSymbolIn(module, name)) {
      auto ext = dyn_cast<hw::HWModuleExternOp>(sym);
      if (!ext) {
        sym->emitError() << "register-mapper: '" << name
                         << "' is defined here as a module, not the cell";
        return {};
      }
      llvm::StringSet<> haveIn, haveOut;
      for (auto port : ext.getPortList())
        (port.dir == hw::ModulePort::Direction::Input ? haveIn : haveOut)
            .insert(port.name.getValue());
      bool same = haveIn.size() == wantIn.size();
      for (auto &w : wantIn)
        same &= haveIn.contains(w.getKey());
      for (unsigned b = 0; b < n; ++b)
        same &= haveOut.contains(MbSpec::subst(c.out, "i", b));
      if (!same) {
        ext.emitError() << "register-mapper: existing declaration of '"
                        << name << "' does not have the multibit spec's pins";
        return {};
      }
      externDecls[name] = ext;
      return ext;
    }
    Type i1 = IntegerType::get(module.getContext(), 1);
    SmallVector<hw::PortInfo, 16> ports;
    unsigned argNum = 0;
    auto add = [&](StringRef pn, hw::ModulePort::Direction dir, unsigned num) {
      hw::PortInfo port;
      port.name = StringAttr::get(module.getContext(), pn);
      port.type = i1;
      port.dir = dir;
      port.argNum = num;
      ports.push_back(port);
    };
    add(c.clock, hw::ModulePort::Direction::Input, argNum++);
    if (c.hasReset)
      add(c.reset, hw::ModulePort::Direction::Input, argNum++);
    for (auto &t : c.ties)
      add(t.first, hw::ModulePort::Direction::Input, argNum++);
    for (unsigned b = 0; b < n; ++b)
      add(MbSpec::subst(c.data, "i", b), hw::ModulePort::Direction::Input,
          argNum++);
    for (unsigned b = 0; b < n; ++b)
      add(MbSpec::subst(c.out, "i", b), hw::ModulePort::Direction::Output, b);
    OpBuilder builder = OpBuilder::atBlockBegin(module.getBody());
    auto externOp = hw::HWModuleExternOp::create(
        builder, module.getLoc(), StringAttr::get(module.getContext(), name),
        ports);
    externDecls[name] = externOp;
    return externOp;
  }

  // Bit-blasts one firreg; returns false (and signals pass failure) on an
  // unsupported shape.
  bool mapFirReg(seq::FirRegOp firreg, ModuleOp topModule,
                 const LibertyLibrary &lib, SmallVectorImpl<PendingReg> &pend,
                 std::vector<MbBit> &mbBits) {
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
    unsigned open = 0; // bits deferred to the multibit banker
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

      std::string instName = (namePrefix + "_bit" + Twine(i)).str();
      if (mb.active) {
        StringRef cname = !reset ? "none" : !constResetValue[i] ? "clear" : "";
        std::string fam = mb.familyFor(namePrefix);
        const MbClass *cls = cname.empty() ? nullptr : mb.classFor(fam, cname);
        if (cls) {
          // Deferred: banked (or emitted single) once every register of the
          // module has been seen.
          mbBits.push_back({(unsigned)pend.size(), i, dBit, clockI1, reset,
                            resetOperand, cellPat, cls, fam, cname.str()});
          ++open;
          continue;
        }
      }
      auto sharedConst = [&](bool v) { return constant(v); };
      Value q = emitSingle(builder, loc, topModule, *cellPat, clockI1, dBit,
                           resetOperand, sharedConst, instName);
      if (!q)
        return false;
      qBits[i] = q;
    }

    if (open) {
      PendingReg pr;
      pr.firreg = firreg;
      pr.origType = origType;
      pr.name = namePrefix;
      pr.qBits = qBits;
      pr.open = open;
      pend.push_back(std::move(pr));
      return true;
    }
    finishReg(firreg, origType, qBits);
    return true;
  }

  // Concatenates a register's per-bit Q results back into its data result
  // and erases the firreg.
  void finishReg(seq::FirRegOp firreg, Type origType, ArrayRef<Value> qBits) {
    OpBuilder builder(firreg);
    Location loc = firreg.getLoc();
    unsigned width = qBits.size();
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
  }

  // Forms and emits the banks of one module's deferred bits, then finishes
  // every register that was waiting on them.
  bool bankModule(ModuleOp topModule, SmallVectorImpl<PendingReg> &pend,
                  std::vector<MbBit> &bits) {
    using Key = std::tuple<const void *, const void *, std::string,
                           std::string>;
    std::map<Key, SmallVector<unsigned>> groups; // bit indices, reg order
    for (unsigned k = 0; k < bits.size(); ++k) {
      MbBit &b = bits[k];
      Key key{pend[b.reg].firreg.getClk().getAsOpaquePointer(),
              b.reset ? b.reset.getAsOpaquePointer() : nullptr, b.className,
              b.family};
      groups[key].push_back(k);
    }
    struct Bank {
      const MbClass *cls;
      SmallVector<unsigned> members;
    };
    std::vector<Bank> banks;
    SmallVector<unsigned> singles;
    // Greedy: largest declared width that fits; the tail (< smallest
    // width) comes back as leftover.
    auto chunk = [&](const MbClass &cls, ArrayRef<unsigned> seq,
                     bool onlyMax, SmallVectorImpl<unsigned> &left) {
      size_t pos = 0;
      while (pos < seq.size()) {
        size_t rem = seq.size() - pos;
        unsigned w = 0;
        for (unsigned cand : cls.widths)
          if (cand <= rem && (!onlyMax || cand == cls.widths[0])) {
            w = cand;
            break;
          }
        if (!w)
          break;
        banks.push_back({&cls, SmallVector<unsigned>(seq.begin() + pos,
                                                     seq.begin() + pos + w)});
        pos += w;
      }
      left.append(seq.begin() + pos, seq.end());
    };
    // The groups are keyed by the clock and reset values' addresses, so the
    // map order is the allocator's: visit them in the order of their first
    // bit (registers in module order) so banks, their instance numbers and
    // the netlist do not depend on it.
    std::vector<SmallVector<unsigned> *> order;
    for (auto &g : groups)
      order.push_back(&g.second);
    llvm::stable_sort(order, [](SmallVector<unsigned> *a, SmallVector<unsigned> *b) {
      return a->front() < b->front();
    });
    for (SmallVector<unsigned> *gp : order) {
      SmallVector<unsigned> &gs = *gp;
      const MbClass &cls = *bits[gs[0]].cls;
      SmallVector<unsigned> pool;
      size_t lo = 0;
      while (lo < gs.size()) {
        size_t hi = lo;
        while (hi < gs.size() &&
               bits[gs[hi]].reg == bits[gs[lo]].reg)
          ++hi;
        ArrayRef<unsigned> run(gs.begin() + lo, gs.begin() + hi);
        if (mb.crossRegister)
          chunk(cls, run, /*onlyMax=*/true, pool);
        else
          chunk(cls, run, /*onlyMax=*/false, singles);
        lo = hi;
      }
      if (!pool.empty())
        chunk(cls, pool, /*onlyMax=*/false, singles);
    }

    // Bits of a class that declares a single-bit cell: width-1 banks.
    {
      SmallVector<unsigned> rest;
      for (unsigned k : singles) {
        if (const MbClass *sc = bits[k].cls->single.get())
          banks.push_back({sc, SmallVector<unsigned>(1, k)});
        else
          rest.push_back(k);
      }
      singles = std::move(rest);
    }

    unsigned counter = 0;
    for (Bank &bk : banks) {
      const MbClass &c = *bk.cls;
      unsigned n = bk.members.size();
      MbBit &first = bits[bk.members[0]];
      unsigned lastReg = 0;
      for (unsigned m : bk.members)
        lastReg = std::max(lastReg, bits[m].reg);
      OpBuilder builder(pend[lastReg].firreg);
      Location loc = pend[lastReg].firreg.getLoc();
      hw::HWModuleExternOp ext = getOrCreateMbDecl(topModule, c, n);
      if (!ext)
        return false;
      llvm::StringMap<Value> byPin;
      byPin[c.clock] = first.clockI1;
      if (c.hasReset) {
        Value r = first.reset;
        if (!c.resetActiveHigh)
          r = comb::XorOp::create(
              builder, loc, r,
              hw::ConstantOp::create(builder, loc, APInt(1, 1)));
        byPin[c.reset] = r;
      }
      int lastQTie = -1;
      for (auto &t : c.ties)
        byPin[t.first] = hw::ConstantOp::create(
            builder, loc, APInt(1, t.second == 1 ? 1 : 0));
      for (unsigned b = 0; b < n; ++b)
        byPin[MbSpec::subst(c.data, "i", b)] = bits[bk.members[b]].d;
      SmallVector<Value, 16> operands;
      unsigned opIdx = 0;
      for (auto port : ext.getPortList())
        if (port.dir == hw::ModulePort::Direction::Input) {
          operands.push_back(byPin.lookup(port.name.getValue()));
          for (auto &t : c.ties)
            if (t.second < 0 && t.first == port.name.getValue())
              lastQTie = opIdx;
          ++opIdx;
        }
      bool sameReg = true;
      for (unsigned m : bk.members)
        sameReg &= bits[m].reg == first.reg;
      std::string instName =
          n == 1 && c.widths.size() == 1 && c.widths[0] == 1
              ? (pend[first.reg].name + "_bit" + Twine(first.bit)).str()
              : (pend[first.reg].name + "_mb" + Twine(first.bit) + "_" +
                 Twine(bits[bk.members.back()].bit) +
                 (sameReg ? "" : "_x" + Twine(counter++)))
                    .str();
      auto inst = hw::InstanceOp::create(builder, loc, ext,
                                         builder.getStringAttr(instName),
                                         operands);
      if (lastQTie >= 0)
        inst->setOperand(lastQTie, inst.getResult(n - 1));
      for (unsigned b = 0; b < n; ++b) {
        MbBit &m = bits[bk.members[b]];
        pend[m.reg].qBits[m.bit] = inst.getResult(b);
        --pend[m.reg].open;
      }
      if (n == 1 && c.widths.size() == 1 && c.widths[0] == 1) {
        // A pack's single-bit cell for a bit the banker left over: counted
        // with the single-bit cells, and named in a clause of its own.
        ++perSingleCounts[MbSpec::subst(c.cellTmpl, "n", n)];
        ++packSingles;
        ++leftoverSingles;
      } else {
        ++perBankCounts[MbSpec::subst(c.cellTmpl, "n", n)];
        bankedBits += n;
        ++totalBanks;
      }
    }
    // Bits the banker left single.
    for (unsigned k : singles) {
      MbBit &m = bits[k];
      PendingReg &pr = pend[m.reg];
      OpBuilder builder(pr.firreg);
      Location loc = pr.firreg.getLoc();
      auto fresh = [&](bool v) -> Value {
        return hw::ConstantOp::create(builder, loc, APInt(1, v));
      };
      Value q = emitSingle(builder, loc, topModule, *m.single, m.clockI1, m.d,
                           m.resetOperand, fresh,
                           (pr.name + "_bit" + Twine(m.bit)).str());
      if (!q)
        return false;
      pr.qBits[m.bit] = q;
      --pr.open;
      ++leftoverSingles;
    }
    for (PendingReg &pr : pend) {
      assert(pr.open == 0 && "deferred bit neither banked nor single");
      finishReg(pr.firreg, pr.origType, pr.qBits);
    }
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
    mb = MbSpec();
    perBankCounts.clear();
    perSingleCounts.clear();
    totalBanks = bankedBits = leftoverSingles = packSingles = 0;
    if (!multibitPath.empty()) {
      std::string err = mb.load(multibitPath);
      if (!err.empty()) {
        module.emitError("register-mapper: multibit spec ")
            << multibitPath.getValue() << ": " << err;
        return signalPassFailure();
      }
    }

    for (auto hwModule :
         llvm::make_early_inc_range(module.getOps<hw::HWModuleOp>())) {
      SmallVector<PendingReg> pend;
      std::vector<MbBit> mbBits;
      for (auto firreg : llvm::make_early_inc_range(
               hwModule.getBodyBlock()->getOps<seq::FirRegOp>())) {
        if (!mapFirReg(firreg, module, lib, pend, mbBits))
          return signalPassFailure();
      }
      if (!pend.empty() && !bankModule(module, pend, mbBits))
        return signalPassFailure();
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
    if (mb.active) {
      std::string bs;
      llvm::raw_string_ostream bo(bs);
      bool f = true;
      for (auto &e : perBankCounts) {
        bo << (f ? "" : ", ") << e.second << " " << e.first;
        f = false;
      }
      llvm::errs() << "[register-mapper] multibit: " << totalBanks
                   << " bank(s) holding " << bankedBits << " bit(s) ("
                   << bo.str() << "); " << (totalBitInstances + packSingles)
                   << " single-bit cell(s), of which " << leftoverSingles
                   << " were bank candidates left over";
      if (packSingles) {
        std::string ss;
        llvm::raw_string_ostream so(ss);
        bool g = true;
        for (auto &e : perSingleCounts) {
          so << (g ? "" : ", ") << e.second << " " << e.first;
          g = false;
        }
        llvm::errs() << "; " << packSingles
                     << " of the single-bit cells are the pack's (" << so.str()
                     << ")";
      }
      llvm::errs() << "\n";
    }
  }

private:
  llvm::StringMap<hw::HWModuleExternOp> externDecls;
  llvm::StringMap<unsigned> perCellCounts;
  std::map<std::string, unsigned> perBankCounts;
  std::map<std::string, unsigned> perSingleCounts;
  MbSpec mb;
  size_t totalBanks = 0, bankedBits = 0, leftoverSingles = 0, packSingles = 0;
  size_t totalBitInstances = 0;
  size_t numFirregsMapped = 0;
  unsigned anonFirRegCounter = 0;
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createRegisterMapperPass() {
  return std::make_unique<RegisterMapperPass>();
}

} // namespace circt_abc
