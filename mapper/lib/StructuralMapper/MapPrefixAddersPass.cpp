//===- MapPrefixAddersPass.cpp -----------------------------------*- C++ -*-===//
//
// `--map-prefix-adders`: lower wide two-operand adds (and adds with a carry-in
// bit) to a parallel-prefix network of liberty CELLS before ABC runs, with a
// chosen architecture: Sklansky, Kogge-Stone, Han-Carlson or Brent-Kung.
//
// Why: the prefix structure CombToSynth builds is only the starting point for
// ABC, and the area-oriented restructuring in the default recipe (`scorr`,
// `dc2`, `&deepsyn`) folds it back into a near-ripple chain, which is smaller:
// a carry chain that is a few prefix levels deep in the subject graph comes
// out of the mapper far deeper. Cell instances are boundaries for the
// outliner, so ABC maps only the logic around the adder and cannot undo the
// structure. The emitter (polarity tracking, inverting AOI21/OAI21 compound
// cells) is the one map-compressor-cells' `prefix-final-adder` uses; this
// pass applies it to any wide add, with a per-op architecture choice.
//
// Operand forms handled: `a + b`; `a + b + zext(c)` and `a + b + 1` (a
// carry-in, folded in as bit -1 of the network). The sum is built with the
// carry-out dropped, as in a W-bit comb.add. Everything else is left to
// CombToSynth. Function-preserving; the unit tests are SAT-proven with Yosys
// against the original comb.add at many widths (see the test).
//
// NOTE: the emitter block below is a copy of the one in
// MapCompressorCellsPass.cpp; the two should share a header.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "LibertyLibrary.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

// Where does bit `bit` of `v` come from, through concat/extract/replicate?
static std::pair<Value, unsigned> bitSource(Value v, unsigned bit,
                                            unsigned fuel = 64) {
  if (!fuel)
    return {v, bit};
  if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
    unsigned pos = v.getType().getIntOrFloatBitWidth();
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      if (bit >= pos && bit < pos + ow)
        return bitSource(operand, bit - pos, fuel - 1);
    }
    return {v, bit};
  }
  if (auto ex = v.getDefiningOp<comb::ExtractOp>())
    return bitSource(ex.getInput(), bit + ex.getLowBit(), fuel - 1);
  if (auto rep = v.getDefiningOp<comb::ReplicateOp>()) {
    unsigned iw = rep.getInput().getType().getIntOrFloatBitWidth();
    return bitSource(rep.getInput(), bit % iw, fuel - 1);
  }
  return {v, bit};
}

enum class Arch { Sklansky, KoggeStone, HanCarlson, BrentKung };

static std::optional<Arch> parseArch(StringRef s) {
  if (s == "SKLANSKEY" || s == "SKLANSKY")
    return Arch::Sklansky;
  if (s == "KOGGE-STONE")
    return Arch::KoggeStone;
  if (s == "HAN-CARLSON")
    return Arch::HanCarlson;
  if (s == "BRENT-KUNG")
    return Arch::BrentKung;
  return std::nullopt;
}

struct MapPrefixAddersPass
    : public PassWrapper<MapPrefixAddersPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MapPrefixAddersPass)

  MapPrefixAddersPass() = default;
  MapPrefixAddersPass(const MapPrefixAddersPass &o) : PassWrapper(o) {
    enablePass = o.enablePass.getValue();
    libertyPath = o.libertyPath.getValue();
    minWidth = o.minWidth.getValue();
    maxWidth = o.maxWidth.getValue();
    archOpt = o.archOpt.getValue();
  }

  StringRef getArgument() const final { return "map-prefix-adders"; }
  StringRef getDescription() const final {
    return "Lower wide adds to parallel-prefix networks of cells before ABC, "
           "which area-recovers a prefix adder back into a ripple chain";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  Option<bool> enablePass{*this, "enable-prefix-adders",
                          llvm::cl::desc("Enable the rewrite (OFF by default)"),
                          llvm::cl::init(false)};
  Option<std::string> libertyPath{
      *this, "liberty", llvm::cl::desc("Liberty file holding the gate cells")};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(16),
      llvm::cl::desc("only adds at or above this width are built as cells")};
  Option<unsigned> maxWidth{
      *this, "max-width", llvm::cl::init(0),
      llvm::cl::desc("only adds at or below this width (0 = no limit)")};
  Option<std::string> archOpt{
      *this, "arch", llvm::cl::init("SKLANSKEY"),
      llvm::cl::desc("SKLANSKEY | KOGGE-STONE | HAN-CARLSON | BRENT-KUNG; "
                     "'attr' takes each op's synth.test.arch (default "
                     "SKLANSKEY when it has none)")};

  struct Sig {
    Value v;
    bool neg = false;
    int k = -1; // -1 = not a constant
  };
  const LibertyLibrary *cellLib = nullptr;
  llvm::StringMap<hw::HWModuleExternOp> cellDecls;
  unsigned nPrefixCells = 0;
  // Every op one prefix build creates, so a build that fails part-way can be
  // rolled back completely. Without this a failed build left its gates in
  // the module with no users -- silent dead area in the netlist.
  SmallVector<Operation *> created;
  llvm::DenseMap<Value, Value> invCache; // one inverter per signal, shared

  hw::HWModuleExternOp declFor(ModuleOp top, const CellPattern &cell) {
    if (auto d = cellDecls.lookup(cell.name))
      return d;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == cell.name)
        return cellDecls[cell.name] = ext;
    MLIRContext *ctx = top.getContext();
    Type i1 = IntegerType::get(ctx, 1);
    SmallVector<hw::PortInfo, 5> ports;
    for (auto [idx, pin] : llvm::enumerate(cell.inputPins)) {
      hw::PortInfo p;
      p.name = StringAttr::get(ctx, pin);
      p.type = i1;
      p.dir = hw::ModulePort::Direction::Input;
      p.argNum = idx;
      ports.push_back(p);
    }
    hw::PortInfo o;
    o.name = StringAttr::get(ctx, cell.outputPin);
    o.type = i1;
    o.dir = hw::ModulePort::Direction::Output;
    o.argNum = 0;
    ports.push_back(o);
    OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
    return cellDecls[cell.name] = hw::HWModuleExternOp::create(
               b, top.getLoc(), StringAttr::get(ctx, cell.name), ports);
  }

  // Emit f(in...) -- `f` is a truth table over `in` in the LibertyLibrary
  // convention (bit a = f at the assignment giving input i bit i of a).
  // Returns nullopt only if the library has no cell for the function.
  std::optional<Sig> gateOnce(OpBuilder &b, Location loc, ModuleOp top,
                              TruthTable f, SmallVector<Sig, 3> in) {
    // Absorb input polarity: f'(a) = f(a ^ negmask).
    unsigned n = in.size();
    auto remap = [](TruthTable t, unsigned n, auto fn) {
      TruthTable r = 0;
      for (unsigned a = 0; a < (1u << n); ++a)
        if ((t >> fn(a)) & 1)
          r |= TruthTable(1) << a;
      return r;
    };
    unsigned mask = 0;
    for (unsigned i = 0; i < n; ++i)
      if (in[i].k < 0 && in[i].neg)
        mask |= 1u << i;
    f = remap(f, n, [&](unsigned a) { return a ^ mask; });
    for (auto &s : in)
      s.neg = false;
    // Fold constants and drop inputs f does not depend on, one at a time.
    for (unsigned i = 0; i < in.size();) {
      unsigned m = in.size();
      auto cofactor = [&](unsigned bit) {
        return remap(f, m - 1, [&](unsigned a) {
          unsigned lo = a & ((1u << i) - 1), hi = a >> i;
          return lo | (bit << i) | (hi << (i + 1));
        });
      };
      TruthTable f0 = cofactor(0), f1 = cofactor(1);
      if (in[i].k >= 0 || f0 == f1) {
        f = in[i].k == 1 ? f1 : f0;
        in.erase(in.begin() + i);
        // Restart: folding a constant can remove the function's dependence
        // on an input already examined (G_hi | (P_hi & 0) no longer reads
        // P_hi), and leaving that dead input in makes the lookup miss.
        i = 0;
        continue;
      }
      ++i;
    }
    n = in.size();
    TruthTable full = n == 6 ? ~TruthTable(0) : (TruthTable(1) << (1u << n)) - 1;
    f &= full;
    if (f == 0)
      return Sig{Value(), false, 0};
    if (f == full)
      return Sig{Value(), false, 1};
    if (n == 1)
      return Sig{in[0].v, f == 0x1, -1}; // f is x (0b10) or !x (0b01)
    auto match = cellLib->lookup(n, f, /*inversionIsFree=*/true);
    if (!match) {
      if (getenv("MCC_DEBUG"))
        llvm::errs() << "[mcc-debug] no cell for arity " << n << " tt 0x"
                     << llvm::utohexstr(f) << "\n";
      return std::nullopt;
    }
    auto decl = declFor(top, *match->cell);
    SmallVector<Value, 6> args;
    for (unsigned slot = 0; slot < match->cell->arity(); ++slot)
      args.push_back(in[match->leafForCellInputSlot[slot]].v);
    auto inst = hw::InstanceOp::create(
        b, loc, decl,
        StringAttr::get(top.getContext(),
                        (Twine("cpa_") + Twine(nPrefixCells++)).str()),
        args);
    created.push_back(inst);
    return Sig{inst.getResult(0), match->inverted, -1};
  }

  // A real inverter cell on `v` (cached). Bypasses gateOnce, whose
  // single-input case deliberately emits nothing and just flips polarity.
  std::optional<Value> invOf(OpBuilder &b, Location loc, ModuleOp top,
                             Value v) {
    if (auto hit = invCache.lookup(v))
      return hit;
    auto match = cellLib->lookup(1, /*!x*/ 0x1);
    if (!match || match->inverted)
      return std::nullopt;
    auto inst = hw::InstanceOp::create(
        b, loc, declFor(top, *match->cell),
        StringAttr::get(top.getContext(),
                        (Twine("cpa_") + Twine(nPrefixCells++)).str()),
        SmallVector<Value, 1>{v});
    created.push_back(inst);
    return invCache[v] = inst.getResult(0);
  }

  // gateOnce, and if a mixed-polarity function has no cell -- e.g.
  // !a | (b & !c), which is what a prefix combine sees when its operands
  // come from levels of opposite polarity -- invert the complemented inputs
  // physically and retry with every input in true polarity.
  std::optional<Sig> gate(OpBuilder &b, Location loc, ModuleOp top,
                          TruthTable f, SmallVector<Sig, 3> in) {
    if (auto r = gateOnce(b, loc, top, f, in))
      return r;
    bool any = false;
    for (auto &x : in)
      if (x.k < 0 && x.neg) {
        auto v = invOf(b, loc, top, x.v);
        if (!v)
          return std::nullopt;
        x = Sig{*v, false, -1};
        any = true;
      }
    return any ? gateOnce(b, loc, top, f, in) : std::nullopt;
  }

  // The same function, but asking for the result held in `wantNeg`
  // polarity: query the complement and flip. Used to keep a prefix level's
  // signals in ONE polarity, so the next level's inputs are uniform and it
  // needs no inverters.
  std::optional<Sig> gateWant(OpBuilder &b, Location loc, ModuleOp top,
                              TruthTable f, SmallVector<Sig, 3> in,
                              bool wantNeg) {
    if (!wantNeg)
      return gate(b, loc, top, f, in);
    unsigned n = in.size();
    TruthTable full = (TruthTable(1) << (1u << n)) - 1;
    auto r = gate(b, loc, top, ~f & full, in);
    if (!r)
      return std::nullopt;
    if (r->k >= 0)
      r->k = 1 - r->k;
    else
      r->neg = !r->neg;
    return r;
  }

  // A Sig as a real i1 Value of the TRUE polarity (one INV if it is held
  // complemented; constants become hw.constant).
  std::optional<Value> materialize(OpBuilder &b, Location loc, ModuleOp top,
                                   Sig s) {
    if (s.k >= 0) {
      auto k = hw::ConstantOp::create(b, loc, APInt(1, s.k));
      created.push_back(k);
      return k.getResult();
    }
    if (!s.neg)
      return s.v;
    return invOf(b, loc, top, s.v);
  }

  // Prefix network over the bit positions 0..N-1 (position 0 may be the
  // carry-in). `combine(hi, lo)` merges the span ending at `lo` into the span
  // ending at `hi`; every architecture is a schedule of those.
  std::optional<SmallVector<Value>>
  buildPrefixAdder(OpBuilder &b, Location loc, ModuleOp top, ArrayRef<Sig> x,
                   ArrayRef<Sig> y, Arch arch, unsigned firstSum) {
    created.clear();
    invCache.clear();
    auto r = buildImpl(b, loc, top, x, y, arch, firstSum);
    if (!r)
      for (Operation *op : llvm::reverse(created))
        op->erase();
    created.clear();
    return r;
  }

  std::optional<SmallVector<Value>>
  buildImpl(OpBuilder &b, Location loc, ModuleOp top, ArrayRef<Sig> x,
            ArrayRef<Sig> y, Arch arch, unsigned firstSum) {
    constexpr TruthTable AND2 = 0x8, XOR2 = 0x6, AO21 = 0xEA;
    unsigned N = x.size();
    SmallVector<Sig> p(N), G(N), P(N);
    SmallVector<unsigned> lo(N);
    for (unsigned i = 0; i < N; ++i) {
      auto gi = gate(b, loc, top, AND2, {x[i], y[i]});
      if (!gi)
        return std::nullopt;
      auto pi = gateWant(b, loc, top, XOR2, {x[i], y[i]}, gi->k < 0 && gi->neg);
      if (!pi)
        return std::nullopt;
      G[i] = *gi;
      p[i] = P[i] = *pi;
      lo[i] = i;
    }
    bool ok = true;
    auto combine = [&](unsigned hi, unsigned lw) {
      if (!ok)
        return;
      auto nG = gate(b, loc, top, AO21, {G[hi], P[hi], G[lw]});
      if (!nG) {
        ok = false;
        return;
      }
      unsigned newLo = lo[lw];
      if (newLo > 0) { // a span reaching bit 0 never needs its P again
        auto nP = gateWant(b, loc, top, AND2, {P[hi], P[lw]},
                           nG->k < 0 && nG->neg);
        if (!nP) {
          ok = false;
          return;
        }
        P[hi] = *nP;
      }
      G[hi] = *nG;
      lo[hi] = newLo;
    };
    switch (arch) {
    case Arch::Sklansky:
      for (unsigned d = 1; d < N; d *= 2)
        for (unsigned i = 0; i < N; ++i)
          if (i & d)
            combine(i, (i & ~(d - 1)) - 1);
      break;
    case Arch::KoggeStone:
      for (unsigned d = 1; d < N; d *= 2)
        for (unsigned i = N; i-- > d;) // descending: sources still hold level d-1
          combine(i, i - d);
      break;
    case Arch::HanCarlson: {
      for (unsigned i = 1; i < N; i += 2)
        combine(i, i - 1);
      for (unsigned d = 2; d < N; d *= 2)
        for (unsigned i = N; i-- > d;)
          if (i & 1)
            combine(i, i - d);
      for (unsigned i = 2; i < N; i += 2)
        combine(i, i - 1);
      break;
    }
    case Arch::BrentKung: {
      unsigned top2 = 1;
      while (top2 * 2 <= N)
        top2 *= 2;
      for (unsigned d = 1; d < N; d *= 2)
        for (unsigned i = 2 * d - 1; i < N; i += 2 * d)
          combine(i, i - d);
      for (unsigned d = top2 / 2; d >= 1; d /= 2)
        for (unsigned i = 3 * d - 1; i < N; i += 2 * d)
          combine(i, i - d);
      break;
    }
    }
    if (!ok)
      return std::nullopt;
    SmallVector<Value> sum;
    for (unsigned i = firstSum; i < N; ++i) {
      if (i > 0 && lo[i - 1] != 0)
        return std::nullopt; // schedule left a gap: refuse rather than miscompute
      std::optional<Sig> s = i == 0 ? std::optional<Sig>(p[0])
                                    : gate(b, loc, top, XOR2, {p[i], G[i - 1]});
      if (!s)
        return std::nullopt;
      auto v = materialize(b, loc, top, *s);
      if (!v)
        return std::nullopt;
      sum.push_back(*v);
    }
    return sum;
  }

  Sig bitOf(OpBuilder &b, Location loc, Value v, unsigned bit) {
    auto [src, sbit] = bitSource(v, bit);
    if (auto k = src.getDefiningOp<hw::ConstantOp>())
      return Sig{Value(), false, (int)k.getValue()[sbit]};
    if (src.getType().isInteger(1))
      return Sig{src, false, -1};
    return Sig{comb::ExtractOp::create(b, loc, b.getI1Type(), src, sbit), false,
               -1};
  }

  void runOnOperation() override {
    if (!enablePass.getValue())
      return;
    ModuleOp top = getOperation();
    if (libertyPath.getValue().empty()) {
      top.emitError("map-prefix-adders: liberty= is required");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      top.emitError("map-prefix-adders: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    cellLib = &*lib;
    std::optional<Arch> fixed;
    if (archOpt.getValue() != "attr") {
      fixed = parseArch(archOpt.getValue());
      if (!fixed) {
        top.emitError("map-prefix-adders: unknown arch ") << archOpt.getValue();
        return signalPassFailure();
      }
    }
    SmallVector<comb::AddOp> work;
    top.walk([&](comb::AddOp add) {
      unsigned w = add.getType().getIntOrFloatBitWidth();
      if (w < minWidth.getValue() ||
          (maxWidth.getValue() && w > maxWidth.getValue()))
        return;
      if (add.getNumOperands() == 2 || add.getNumOperands() == 3)
        work.push_back(add);
    });
    unsigned nAdd = 0, nDeclined = 0;
    unsigned cellsBefore = nPrefixCells;
    for (comb::AddOp add : work) {
      unsigned W = add.getType().getIntOrFloatBitWidth();
      OpBuilder b(add);
      Location loc = add.getLoc();
      Value a, c;
      Sig cin{Value(), false, 0};
      bool hasCin = false;
      if (add.getNumOperands() == 2) {
        a = add.getOperand(0);
        c = add.getOperand(1);
      } else {
        // a + b + cin: cin is a constant 1 or a zero-extended bit.
        int ci = -1;
        for (unsigned i = 0; i < 3 && ci < 0; ++i) {
          Value v = add.getOperand(i);
          if (auto k = v.getDefiningOp<hw::ConstantOp>()) {
            if (k.getValue().isOne()) {
              ci = i;
              cin = Sig{Value(), false, 1};
            }
            continue;
          }
          auto cat = v.getDefiningOp<comb::ConcatOp>();
          if (!cat || cat.getNumOperands() != 2 ||
              !cat.getOperand(1).getType().isInteger(1))
            continue;
          auto hi = cat.getOperand(0).getDefiningOp<hw::ConstantOp>();
          if (!hi || !hi.getValue().isZero())
            continue;
          ci = i;
          cin = Sig{cat.getOperand(1), false, -1};
        }
        if (ci < 0) {
          ++nDeclined;
          continue;
        }
        hasCin = true;
        a = add.getOperand(ci == 0 ? 1 : 0);
        c = add.getOperand(ci == 2 ? 1 : 2);
      }
      unsigned off = hasCin ? 1 : 0;
      SmallVector<Sig> xs(W + off), ys(W + off);
      if (hasCin) {
        xs[0] = Sig{Value(), false, 1};
        ys[0] = cin;
      }
      for (unsigned i = 0; i < W; ++i) {
        xs[i + off] = bitOf(b, loc, a, i);
        ys[i + off] = bitOf(b, loc, c, i);
      }
      Arch arch = Arch::Sklansky;
      if (fixed)
        arch = *fixed;
      else if (auto at = add->getAttrOfType<StringAttr>("synth.test.arch"))
        if (auto pa = parseArch(at.getValue()))
          arch = *pa;
      auto sum = buildPrefixAdder(b, loc, top, xs, ys, arch, off);
      if (!sum || sum->size() != W) {
        ++nDeclined;
        continue;
      }
      SmallVector<Value> msbFirst(sum->rbegin(), sum->rend());
      Value v = W == 1 ? msbFirst[0]
                       : comb::ConcatOp::create(b, loc, msbFirst).getResult();
      add.getResult().replaceAllUsesWith(v);
      add.erase();
      ++nAdd;
    }
    if (nAdd || nDeclined)
      llvm::errs() << "[map-prefix-adders] " << nAdd << " add(s) built as "
                   << archOpt.getValue() << " prefix networks, "
                   << (nPrefixCells - cellsBefore) << " cell(s); " << nDeclined
                   << " left to CombToSynth\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapPrefixAddersPass() {
  return std::make_unique<MapPrefixAddersPass>();
}

} // namespace circt_abc
