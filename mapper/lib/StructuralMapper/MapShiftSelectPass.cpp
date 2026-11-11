//===- MapShiftSelectPass.cpp ----------------------------------*- C++ -*-===//
//
// `--map-shift-select`: build a decoded one-hot select over shifted copies of
// one word (the output form of `decode-mux-chains` for a `case` over a shift
// amount) as a two-level AND-OR network of library cells before ABC.
//
// WHY. `decode-mux-chains` turns `case (amt) k: y = x <<< s_k` into
//     y = OR_k (sel_k ? (x <<< s_k) : 0),  sel_k = (amt == c_k)
// and ABC then re-derives a log shifter out of it: a chain of 2:1 muxes,
// several levels of full-width stages whose every stage output switches
// whenever the data or the shift amount changes. The same function as
// AND-OR logic with a partially decoded select switches far less: a data
// bit toggling reaches only the one AND term whose select is hot.
//
// MECHANISM. Under the one-hot invariant (the selects are `icmp eq amt, c_k`
// of one `amt` with distinct constants, plus at most one "none of them" arm,
// all checked syntactically) the select factors:
//     sel_s = Q_q & R_r          (s = smin + R*q + r)
//     Q_q = OR of the selects of shift group q,  R_r = OR over those with
//     remainder r
// because exactly one select is hot (or none), and then Q_q & R_r is true
// iff that select is (q,r). The word then needs two radix levels, not one
// term per (arm, bit):
//     y[m]   = OR_r R_r & base(m - r)              (radix level, shared)
//     out[j] = OR_q Q_q & y[j - smin - R*q]
// where base(i) is the unshifted word (0 below bit 0, sign/top bit extended
// above it). Each AND-OR is built from the library's AO22/AND2/OR cells by
// truth table, absorbing polarity (an AOI22 feeding a NAND), so the ABC
// that follows treats the network as opaque cells and cannot rebuild the
// mux ladder. The arm words are verified bit by bit to be shifts of one
// base word; any mismatch leaves the OR untouched.
//
// OFF by default (project convention): enable-shift-select=true.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/ScopeExit.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <climits>
#include <string>
#include <array>
#include <functional>
#include <map>
#include <set>
#include <tuple>
#include <vector>

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

struct MapShiftSelectPass
    : public PassWrapper<MapShiftSelectPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MapShiftSelectPass)

  MapShiftSelectPass() = default;
  MapShiftSelectPass(const MapShiftSelectPass &o) : PassWrapper(o) {
    enablePass = o.enablePass.getValue();
    libertyPath = o.libertyPath.getValue();
    minArms = o.minArms.getValue();
    minWidth = o.minWidth.getValue();
    radix = o.radix.getValue();
    wide = o.wide.getValue();
    wide4 = o.wide4.getValue();
    recipes = o.recipes.getValue();
    chains = o.chains.getValue();
    directDecode = o.directDecode.getValue();
    defaultDontCare = o.defaultDontCare.getValue();
    selectMaxFanout = o.selectMaxFanout.getValue();
  }

  StringRef getArgument() const final { return "map-shift-select"; }
  StringRef getDescription() const final {
    return "Build a one-hot select over shifted copies of one word as a "
           "two-level AND-OR network of cells before ABC";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  Option<bool> enablePass{*this, "enable-shift-select",
                          llvm::cl::desc("Enable the rewrite (OFF by default)"),
                          llvm::cl::init(false)};
  Option<std::string> libertyPath{
      *this, "liberty", llvm::cl::desc("Liberty file holding the gate cells")};
  Option<unsigned> minArms{
      *this, "min-arms", llvm::cl::init(12),
      llvm::cl::desc("only selects with at least this many arms")};
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(16),
      llvm::cl::desc("only words at or above this width")};
  Option<bool> wide{
      *this, "wide-cells", llvm::cl::init(false),
      llvm::cl::desc("use three-term AND-OR cells (AO222) where the library has them")};
  Option<bool> wide4{
      *this, "wide4-cells", llvm::cl::init(false),
      llvm::cl::desc("use four-term AND-OR cells (AO2222/AOI2222) where the library has them")};
  Option<bool> chains{
      *this, "chains", llvm::cl::init(false),
      llvm::cl::desc("also take a priority mux chain over distinct "
                     "constants of one amount (run before canonicalization)")};
  Option<std::string> recipes{
      *this, "recipes", llvm::cl::init(""),
      llvm::cl::desc("mixed-radix recipes, ';'-separated, each "
                     "'R1,R2,..@c1,c2,..,cK': digit radices of the shift "
                     "amount (the last digit takes the rest) and the cell "
                     "width (AND-OR terms per cell) of each level; the "
                     "cheapest by cell area is built. Empty = the two-level "
                     "radix network")};
  Option<bool> directDecode{
      *this, "direct-decode", llvm::cl::init(false),
      llvm::cl::desc("build the digit selects as small cell networks of the "
                     "raw bits of the amount (found by evaluating the amount "
                     "expression over its leaf bits) instead of an OR of "
                     "per-arm comparators left to the logic mapper; the digit "
                     "boundary offset is chosen by area")};
  Option<bool> defaultDontCare{
      *this, "default-dont-care", llvm::cl::init(false),
      llvm::cl::desc("with direct-decode: amounts matched by no constant arm "
                     "(the default arm) are don't cares and continue the "
                     "affine amount-to-shift relation of the constant arms; "
                     "only valid when the design never uses the default arm's "
                     "result")};
  Option<unsigned> selectMaxFanout{
      *this, "select-max-fanout", llvm::cl::init(0),
      llvm::cl::desc("with direct-decode: annotate the instance driving each "
                     "digit select with circt_abc.max_fanout = N so the "
                     "flow's fanout stage leaves a net of at most N loads "
                     "unbuffered (0 = no annotation)")};
  Option<unsigned> radix{
      *this, "radix", llvm::cl::init(4),
      llvm::cl::desc("shift-amount radix of the first AND-OR level")};

  struct Sig {
    Value v;
    bool neg = false;
    int k = -1; // -1 = not a constant
  };
  const LibertyLibrary *cellLib = nullptr;
  llvm::StringMap<hw::HWModuleExternOp> cellDecls;
  unsigned nCells = 0;
  bool freeInv = true; // false: gate() prefers a cell with the exact function
  double areaAcc = 0.0; // liberty area of the cells created so far
  SmallVector<Operation *> created;
  llvm::DenseMap<Value, Value> invCache;
  std::map<std::pair<void *, unsigned>, Value> extractCache;

  //--- cell emitter (same polarity-tracking scheme as map-prefix-adders) ----

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

  std::optional<Sig> gateOnce(OpBuilder &b, Location loc, ModuleOp top,
                              TruthTable f, SmallVector<Sig, 4> in) {
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
      return Sig{in[0].v, f == 0x1, -1};
    auto match = cellLib->lookup(n, f, /*inversionIsFree=*/freeInv);
    if (!match)
      return std::nullopt;
    auto decl = declFor(top, *match->cell);
    SmallVector<Value, 6> args;
    for (unsigned slot = 0; slot < match->cell->arity(); ++slot)
      args.push_back(in[match->leafForCellInputSlot[slot]].v);
    auto inst = hw::InstanceOp::create(
        b, loc, decl,
        StringAttr::get(top.getContext(),
                        (Twine("shs_") + Twine(nCells++)).str()),
        args);
    created.push_back(inst);
    areaAcc += match->cell->area;
    return Sig{inst.getResult(0), match->inverted, -1};
  }

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
                        (Twine("shs_") + Twine(nCells++)).str()),
        SmallVector<Value, 1>{v});
    created.push_back(inst);
    areaAcc += match->cell->area;
    return invCache[v] = inst.getResult(0);
  }

  std::optional<Sig> gate(OpBuilder &b, Location loc, ModuleOp top,
                          TruthTable f, SmallVector<Sig, 4> in) {
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

  static TruthTable orN(unsigned n) {
    TruthTable t = 0;
    for (unsigned a = 1; a < (1u << n); ++a)
      t |= TruthTable(1) << a;
    return t;
  }
  static TruthTable and2() { return 0x8; }
  static TruthTable ao222() {
    TruthTable t = 0;
    for (unsigned a = 0; a < 64; ++a)
      if (((a & 1) && (a & 2)) || ((a & 4) && (a & 8)) || ((a & 16) && (a & 32)))
        t |= TruthTable(1) << a;
    return t;
  }
  static TruthTable ao22() {
    TruthTable t = 0;
    for (unsigned a = 0; a < 16; ++a)
      if (((a & 1) && (a & 2)) || ((a & 4) && (a & 8)))
        t |= TruthTable(1) << a;
    return t;
  }

  // One four-term cell over terms (a_i & b_i), i = 0..3, none constant,
  // computing OR_i(a_i & b_i). With true operands the inverting AND-OR cell
  // (AOI2222) gives the complement; with complemented operands the inverting
  // OR-AND cell (OAI2222) gives the OR itself. The mode follows the majority
  // polarity of the operands so that few inverters are needed; the returned
  // Sig tracks the output polarity.
  std::optional<Sig> gate4(OpBuilder &b, Location loc, ModuleOp top,
                           ArrayRef<std::pair<Sig, Sig>> t) {
    if (t.size() != 4)
      return std::nullopt;
    unsigned negs = 0;
    for (auto &[s, d] : t)
      for (const Sig *x : {&s, &d}) {
        if (x->k >= 0)
          return std::nullopt;
        negs += x->neg;
      }
    bool complement = negs > 8 - negs; // operands carried as complements
    const WideAndOrCell *cell = cellLib->lookupWideAndOr(4, true, complement);
    if (!cell)
      return std::nullopt;
    SmallVector<Value, 8> args;
    for (auto &[s, d] : t)
      for (const Sig *x : {&s, &d}) {
        // want the value of x (true form) or its complement
        std::optional<Value> v;
        if (complement == x->neg)
          v = x->v; // already in the wanted form
        else
          v = invOf(b, loc, top, x->v);
        if (!v)
          return std::nullopt;
        args.push_back(*v);
      }
    CellPattern pat;
    pat.name = cell->name;
    for (auto &p : cell->pins)
      pat.inputPins.push_back(p);
    pat.outputPin = cell->outputPin;
    auto inst = hw::InstanceOp::create(
        b, loc, declFor(top, pat),
        StringAttr::get(top.getContext(),
                        (Twine("shs_") + Twine(nCells++)).str()),
        args);
    created.push_back(inst);
    areaAcc += cell->area;
    // AOI2222(true operands) = !OR ; OAI2222(complemented operands) = OR.
    return Sig{inst.getResult(0), !complement, -1};
  }

  using SigId = std::array<uintptr_t, 3>;
  // The memo keys are sorted, and the order of the sorted operands decides
  // which cell input each operand lands on. A key built from the Value's
  // address would make that order depend on the allocator (a different pin
  // order, same function, from run to run; the pass also erases the cells of
  // candidate builds it discards, and a later cell can reuse their address).
  // A value is identified by the order in which this pass first sees it: the
  // number is kept in a temporary attribute of the defining operation (so it
  // dies with the operation), block arguments by a map. The attribute is
  // removed again when the pass ends.
  llvm::DenseMap<const void *, uintptr_t> argIds;
  uintptr_t nextId = 1;
  static constexpr const char *kIdAttr = "shs.id";
  uintptr_t stableId(Value v) {
    if (!v)
      return 0;
    if (Operation *o = v.getDefiningOp()) {
      auto a = o->getAttrOfType<IntegerAttr>(kIdAttr);
      if (!a) {
        a = IntegerAttr::get(IntegerType::get(o->getContext(), 64),
                             (int64_t)nextId++);
        o->setAttr(kIdAttr, a);
      }
      return ((uintptr_t)a.getInt() << 8) | cast<OpResult>(v).getResultNumber();
    }
    return (argIds.try_emplace(v.getAsOpaquePointer(), nextId++).first->second
            << 8) | 0xff;
  }
  SigId idOf(const Sig &x) {
    return {stableId(x.v), (uintptr_t)x.neg, (uintptr_t)(x.k + 1)};
  }
  std::map<std::vector<SigId>, Sig> orMemo;
  std::map<std::vector<std::array<uintptr_t, 6>>, Sig> andOrMemo;

  // OR of any number of Sigs, in cells of at most four inputs. Equal operand
  // sets share one network.
  std::optional<Sig> orReduce(OpBuilder &b, Location loc, ModuleOp top,
                              SmallVector<Sig> xs) {
    if (xs.empty())
      return Sig{Value(), false, 0};
    if (xs.size() == 1)
      return xs[0];
    std::vector<SigId> key;
    for (const Sig &x : xs)
      key.push_back(idOf(x));
    std::sort(key.begin(), key.end());
    key.erase(std::unique(key.begin(), key.end()), key.end());
    auto hit = orMemo.find(key);
    if (hit != orMemo.end())
      return hit->second;
    auto r = orReduceImpl(b, loc, top, xs);
    if (r)
      orMemo[key] = *r;
    return r;
  }

  std::optional<Sig> orReduceImpl(OpBuilder &b, Location loc, ModuleOp top,
                                  SmallVector<Sig> xs) {
    while (xs.size() > 1) {
      SmallVector<Sig> next;
      for (size_t i = 0; i < xs.size();) {
        size_t n = std::min<size_t>(4, xs.size() - i);
        if (n == 1) {
          next.push_back(xs[i]);
          ++i;
          continue;
        }
        std::optional<Sig> r;
        for (size_t g = n; g >= 2 && !r; --g) {
          SmallVector<Sig, 4> in(xs.begin() + i, xs.begin() + i + g);
          r = gate(b, loc, top, orN(g), in);
          if (r) {
            n = g;
            break;
          }
        }
        if (!r)
          return std::nullopt;
        next.push_back(*r);
        i += n;
      }
      xs = std::move(next);
    }
    return xs[0];
  }

  // OR over (a_i & b_i) terms. Terms with the same data b are merged first
  // (a1&b | a2&b = (a1|a2)&b), then AO22 pairs, an AND2 for an odd one, and an
  // OR. Equal term sets share one network.
  std::optional<Sig> andOr(OpBuilder &b, Location loc, ModuleOp top,
                           ArrayRef<std::pair<Sig, Sig>> terms) {
    std::map<SigId, SmallVector<Sig>> byData;
    std::map<SigId, Sig> dataOf;
    for (auto &[sel, dat] : terms) {
      byData[idOf(dat)].push_back(sel);
      dataOf[idOf(dat)] = dat;
    }
    SmallVector<std::pair<Sig, Sig>> merged;
    for (auto &[id, sels] : byData) {
      auto sel = orReduce(b, loc, top, sels);
      if (!sel)
        return std::nullopt;
      merged.push_back({*sel, dataOf[id]});
    }
    return andOrMerged(b, loc, top, merged, 0);
  }

  // OR over (sel_i & data_i) terms whose data are already distinct. `cap` is
  // the most terms one cell takes (0 = the option default: 4 with wide4-cells,
  // 3 with wide-cells, else 2). Equal term sets share one network.
  std::optional<Sig> andOrMerged(OpBuilder &b, Location loc, ModuleOp top,
                                 ArrayRef<std::pair<Sig, Sig>> merged,
                                 unsigned cap) {
    if (!cap)
      cap = wide4.getValue() ? 4 : wide.getValue() ? 3 : 2;
    std::vector<std::array<uintptr_t, 6>> key;
    for (auto &[sel, dat] : merged) {
      SigId si = idOf(sel), di = idOf(dat);
      key.push_back({si[0], si[1], si[2], di[0], di[1], di[2]});
    }
    std::sort(key.begin(), key.end());
    key.push_back({cap, 0, 0, 0, 0, 0});
    auto hit = andOrMemo.find(key);
    if (hit != andOrMemo.end())
      return hit->second;
    SmallVector<Sig> parts;
    for (size_t i = 0; i < merged.size();) {
      size_t n = std::min<size_t>(cap, merged.size() - i);
      std::optional<Sig> p;
      if (n == 4 && wide4.getValue()) {
        p = gate4(b, loc, top, merged.slice(i, 4));
        if (!p)
          n = 3;
      }
      if (!p && n == 3 && (wide.getValue() || wide4.getValue()) &&
          cellLib->lookup(6, ao222(), true))
        p = gate(b, loc, top, ao222(),
                 {merged[i].first, merged[i].second, merged[i + 1].first,
                  merged[i + 1].second, merged[i + 2].first,
                  merged[i + 2].second});
      if (!p && n >= 2) {
        n = 2;
        p = gate(b, loc, top, ao22(),
                 {merged[i].first, merged[i].second, merged[i + 1].first,
                  merged[i + 1].second});
      }
      if (!p && n == 1)
        p = gate(b, loc, top, and2(), {merged[i].first, merged[i].second});
      if (!p)
        return std::nullopt;
      parts.push_back(*p);
      i += n;
    }
    auto r = orReduce(b, loc, top, parts);
    if (r)
      andOrMemo[key] = *r;
    return r;
  }


  //--- matching --------------------------------------------------------------

  struct Leaf {
    Value v;       // root value (null for a constant)
    unsigned bit;  // bit of the root
    int k = -1;    // constant 0/1
    bool operator==(const Leaf &o) const {
      return v == o.v && bit == o.bit && k == o.k;
    }
  };

  static Leaf leafOf(Value word, unsigned j) {
    auto [src, sbit] = bitSource(word, j);
    if (auto c = src.getDefiningOp<hw::ConstantOp>())
      return Leaf{Value(), 0, (int)c.getValue()[sbit]};
    return Leaf{src, sbit, -1};
  }

  struct Arm {
    Sig sel;
    Value word;
  };

  static bool isZero(Value v) {
    auto c = v.getDefiningOp<hw::ConstantOp>();
    return c && c.getValue().isZero();
  }

  // eq-compare of `amt` against a constant: returns (amt, constant).
  static std::optional<std::pair<Value, APInt>> eqConst(Value s) {
    auto ic = s.getDefiningOp<comb::ICmpOp>();
    if (!ic)
      return std::nullopt;
    auto p = ic.getPredicate();
    if (p != comb::ICmpPredicate::eq && p != comb::ICmpPredicate::ceq &&
        p != comb::ICmpPredicate::weq)
      return std::nullopt;
    if (auto c = ic.getRhs().getDefiningOp<hw::ConstantOp>())
      return std::make_pair(ic.getLhs(), c.getValue());
    if (auto c = ic.getLhs().getDefiningOp<hw::ConstantOp>())
      return std::make_pair(ic.getRhs(), c.getValue());
    return std::nullopt;
  }

  Value extractBit(OpBuilder &b, Location loc, Value root, unsigned bit) {
    if (root.getType().isInteger(1))
      return root;
    auto key = std::make_pair(root.getAsOpaquePointer(), bit);
    auto it = extractCache.find(key);
    if (it != extractCache.end())
      return it->second;
    auto e = comb::ExtractOp::create(b, loc, b.getI1Type(), root, bit);
    created.push_back(e);
    return extractCache[key] = e.getResult();
  }


  //--- mixed-radix recipe ----------------------------------------------------
  //
  // The shift t (0 .. T-1, from the smallest shift) is written in mixed radix,
  // t = d1 + R1*(d2 + R2*(d3 + ...)), the last digit taking the rest. Level k
  // forms V_k(m) = OR_d S_k,d & V_{k-1}(m - d*stride_k) with V_0 the base word,
  // so a datum fans out to R1 first-level cells, the first-level results are
  // shared by every later term, and the output is V_K(j - smin). Compared with
  // the two-level network, a later level may use few-term cells (a toggling
  // first-level value then reaches only the cells whose select is hot) and
  // every level is built in the polarity its operands already have: the
  // selects of a level are made in the polarity of the data they gate (they
  // are ordinary logic of the shift amount, left to the logic mapper), so an
  // AOI level feeds an OAI level feeds an AOI level and no inverter appears.
  // When the arms cover every value of the amount (a default arm, or all 2^w
  // constants) the OR of ALL digit selects of a level is 1, so a level whose
  // terms all carry the same datum passes it on without a gate (the sign
  // extension of a shifted word costs nothing).
  //
  // A conditional negate feeding the shifter, sign ? -{0,x} : {0,x}, is built
  // as an OR-prefix ripple with one NAND and one AOI per bit instead of a
  // negation adder: s_k = x_k ^ (sign & p_k), p_k = OR(x[k-1:0]).

  struct NegMatch {
    Value sign;
    SmallVector<Leaf> x; // bits 0 .. N-2 of the magnitude
  };

  static std::optional<NegMatch> matchNegate(Value root, unsigned N) {
    auto mux = root.getDefiningOp<comb::MuxOp>();
    if (!mux || N < 3) return std::nullopt;
    Value tv = mux.getTrueValue(), fv = mux.getFalseValue();
    NegMatch m;
    m.sign = mux.getCond();
    for (unsigned k = 0; k + 1 < N; ++k) {
      Leaf l = leafOf(fv, k);
      if (l.k >= 0) return std::nullopt;
      m.x.push_back(l);
    }
    if (leafOf(fv, N - 1).k != 0) return std::nullopt;
    if (auto sub = tv.getDefiningOp<comb::SubOp>()) {
      // sign ? 0 - {0,x} : {0,x}
      auto z = sub.getLhs().getDefiningOp<hw::ConstantOp>();
      if (z && z.getValue().isZero() && sub.getRhs() == fv)
        return m;
      return std::nullopt;
    }
    auto add = tv.getDefiningOp<comb::AddOp>();
    if (!add || add.getNumOperands() != 2)
      return std::nullopt;
    Value cat;
    bool one = false;
    for (Value o : add.getInputs()) {
      if (auto c = o.getDefiningOp<hw::ConstantOp>()) {
        if (c.getValue() != 1)
          return std::nullopt;
        one = true;
      } else
        cat = o;
    }
    if (!one || !cat)
      return std::nullopt;
    // cat = {1, ~x}
    auto cc = cat.getDefiningOp<comb::ConcatOp>();
    if (!cc || cc.getNumOperands() != 2)
      return std::nullopt;
    auto top1 = cc.getInputs()[0].getDefiningOp<hw::ConstantOp>();
    auto xo = cc.getInputs()[1].getDefiningOp<comb::XorOp>();
    if (!top1 || top1.getValue() != 1 || !xo || xo.getNumOperands() != 2) return std::nullopt;
    Value xv;
    for (Value o : xo.getInputs()) {
      if (auto c = o.getDefiningOp<hw::ConstantOp>()) {
        if (!c.getValue().isAllOnes())
          return std::nullopt;
      } else
        xv = o;
    }
    if (!xv) return std::nullopt;
    for (unsigned k = 0; k + 1 < N; ++k)
      if (!(leafOf(xv, k) == m.x[k])) return std::nullopt;
    return m;
  }

  Value combConst1(OpBuilder &b, Location loc) {
    auto k = hw::ConstantOp::create(b, loc, APInt(1, 1));
    created.push_back(k);
    return k.getResult();
  }
  Value combNot(OpBuilder &b, Location loc, Value v) {
    auto x = comb::XorOp::create(b, loc, ValueRange{v, combConst1(b, loc)}, false);
    created.push_back(x);
    return x.getResult();
  }

  // Sig of x-bit / sign for the negate ripple.
  std::optional<SmallVector<Sig>> buildNegate(OpBuilder &b, Location loc,
                                              ModuleOp top, const NegMatch &m,
                                              unsigned N) {
    SmallVector<Sig> x;
    for (const Leaf &l : m.x)
      x.push_back(Sig{extractBit(b, loc, l.v, l.bit), false, -1});
    Sig G{m.sign, false, -1}, nG{m.sign, true, -1};
    unsigned n = N - 1; // magnitude bits
    SmallVector<Sig> p(n + 1), s(N);
    p[1] = x[0];
    freeInv = false; // the ripple and the NANDs keep true polarity
    struct Restore {
      bool &f;
      ~Restore() { f = true; }
    } restore{freeInv};
    for (unsigned k = 1; k < n; ++k) {
      auto r = gate(b, loc, top, orN(2), {p[k], x[k]});
      if (!r) return std::nullopt;
      p[k + 1] = *r;
    }
    // s_0 = x_0, carried as the complement of one shared inverter like the
    // other (AOI-made, inverted) bits
    auto nx0 = invOf(b, loc, top, x[0].v);
    if (!nx0)
      return std::nullopt;
    s[0] = Sig{*nx0, true, -1};
    freeInv = true;
    // (nG & x) | (G & p_{k+1} & ~(x & p_k))
    TruthTable f = 0;
    for (unsigned a = 0; a < 32; ++a)
      if (((a & 1) && (a & 2)) || ((a & 4) && (a & 8) && (a & 16)))
        f |= TruthTable(1) << a;
    for (unsigned k = 1; k < n; ++k) {
      auto q = gate(b, loc, top, 0x7, {x[k], p[k]}); // NAND2
      if (!q) return std::nullopt;
      auto r = gate(b, loc, top, f, {nG, x[k], G, p[k + 1], *q});
      if (!r) return std::nullopt;
      s[k] = *r;
    }
    auto top_ = gate(b, loc, top, and2(), {G, p[n]});
    if (!top_)
      return std::nullopt;
    s[n] = *top_;
    return s;
  }

  //--- direct decode of the digit selects -------------------------------------
  //
  // The one-hot selects compare ONE amount expression with constants. The
  // amount (an add of a constant to a slice of a register, say) is evaluated
  // over every assignment of its leaf bits; each assignment then belongs to
  // exactly one arm and therefore has one shift. The digit selects of the
  // mixed-radix recipe are functions of the leaf bits only, so they are
  // synthesised as small cell networks of those bits (a recursive Shannon /
  // single-cell decomposition costed by liberty area, with sharing between
  // selects), not as an OR of per-arm comparators that the logic mapper turns
  // into an adder and a comparator network. The offset `a` of the digit
  // boundary (t -> t + a) is a free parameter: an amount that is a raw field
  // plus a constant has raw-bit digits for the right `a`.

  struct DecodeIn {
    SmallVector<std::pair<Value, unsigned>> leaves; // (word, bit) per variable
    std::vector<long> shiftOf;   // per assignment; LONG_MIN = no arm (zero)
    std::vector<long> contOf;    // affine continuation shift (default arms)
    std::vector<uint8_t> isDefault;
    bool affine = false;
  };

  struct Evaluator {
    SmallVector<std::pair<Value, unsigned>> leaves;
    std::map<std::pair<const void *, unsigned>, unsigned> idx;
    llvm::DenseMap<Value, APInt> constWordCache;
    bool ok = true;
    unsigned addLeaf(Value v, unsigned bit) {
      auto key = std::make_pair(v.getAsOpaquePointer(), bit);
      auto it = idx.find(key);
      if (it != idx.end())
        return it->second;
      leaves.push_back({v, bit});
      return idx[key] = leaves.size() - 1;
    }
    static bool supported(Operation *op) {
      return isa<hw::ConstantOp, comb::ExtractOp, comb::ConcatOp,
                 comb::ReplicateOp, comb::AddOp, comb::SubOp, comb::XorOp,
                 comb::AndOp, comb::OrOp>(op);
    }
    // all = bits of `v` needed in [lo, lo+w)
    void collect(Value v, unsigned lo, unsigned w, unsigned depth = 0) {
      if (!ok)
        return;
      unsigned W = v.getType().getIntOrFloatBitWidth();
      Operation *op = v.getDefiningOp();
      if (!op || !supported(op) || depth > 12) {
        if (W > 64) {
          ok = false;
          return;
        }
        for (unsigned j = lo; j < lo + w && j < W; ++j)
          addLeaf(v, j);
        return;
      }
      if (isa<hw::ConstantOp>(op))
        return;
      if (auto e = dyn_cast<comb::ExtractOp>(op)) {
        collect(e.getInput(), e.getLowBit() + lo, w, depth + 1);
        return;
      }
      if (auto c = dyn_cast<comb::ConcatOp>(op)) {
        unsigned pos = W;
        for (Value in : c.getInputs()) {
          unsigned iw = in.getType().getIntOrFloatBitWidth();
          pos -= iw;
          // overlap of [lo, lo+w) with [pos, pos+iw)
          unsigned a = std::max(lo, pos), e2 = std::min(lo + w, pos + iw);
          if (a < e2)
            collect(in, a - pos, e2 - a, depth + 1);
        }
        return;
      }
      if (auto r = dyn_cast<comb::ReplicateOp>(op)) {
        unsigned iw = r.getInput().getType().getIntOrFloatBitWidth();
        collect(r.getInput(), 0, std::min(iw, w + lo), depth + 1);
        if (iw > 0)
          collect(r.getInput(), 0, iw, depth + 1);
        return;
      }
      // add/sub/xor/and/or: bits up to lo+w of every operand
      for (Value in : op->getOperands())
        collect(in, 0, std::min(W, lo + w), depth + 1);
    }
    APInt eval(Value v, uint64_t A, unsigned depth = 0) {
      unsigned W = v.getType().getIntOrFloatBitWidth();
      Operation *op = v.getDefiningOp();
      if (!op || !supported(op) || depth > 12) {
        APInt r(W, 0);
        for (unsigned j = 0; j < W; ++j) {
          auto it = idx.find({v.getAsOpaquePointer(), j});
          if (it != idx.end() && ((A >> it->second) & 1))
            r.setBit(j);
        }
        return r;
      }
      if (auto c = dyn_cast<hw::ConstantOp>(op))
        return c.getValue();
      if (auto e = dyn_cast<comb::ExtractOp>(op))
        return eval(e.getInput(), A, depth + 1).extractBits(W, e.getLowBit());
      if (auto c = dyn_cast<comb::ConcatOp>(op)) {
        APInt r(W, 0);
        unsigned pos = W;
        for (Value in : c.getInputs()) {
          APInt x = eval(in, A, depth + 1);
          pos -= x.getBitWidth();
          r.insertBits(x, pos);
        }
        return r;
      }
      if (auto r = dyn_cast<comb::ReplicateOp>(op)) {
        APInt x = eval(r.getInput(), A, depth + 1);
        APInt out(W, 0);
        for (unsigned p = 0; p + x.getBitWidth() <= W; p += x.getBitWidth())
          out.insertBits(x, p);
        return out;
      }
      APInt acc = eval(op->getOperand(0), A, depth + 1);
      for (unsigned i = 1; i < op->getNumOperands(); ++i) {
        APInt x = eval(op->getOperand(i), A, depth + 1);
        if (isa<comb::AddOp>(op))
          acc += x;
        else if (isa<comb::SubOp>(op))
          acc -= x;
        else if (isa<comb::XorOp>(op))
          acc ^= x;
        else if (isa<comb::AndOp>(op))
          acc &= x;
        else
          acc |= x;
      }
      return acc;
    }
  };

  // A truth table over a (sorted) subset of the decode's variables.
  struct TT {
    std::vector<unsigned> vars;
    std::vector<uint8_t> bits; // 2^|vars| entries; bit i of the index = vars[i]
    std::string key() const {
      std::string k;
      for (unsigned v : vars)
        k += std::to_string(v) + ",";
      k += ':';
      k.append(bits.begin(), bits.end());
      for (char &c : k)
        if (c == 0 || c == 1)
          c += '0';
      return k;
    }
    bool isConst(int &val) const {
      if (bits.empty())
        return false;
      for (uint8_t b : bits)
        if (b != bits[0])
          return false;
      val = bits[0];
      return true;
    }
  };
  static TT cofactorTT(const TT &f, unsigned pos, int val) {
    TT r;
    for (unsigned i = 0; i < f.vars.size(); ++i)
      if (i != pos)
        r.vars.push_back(f.vars[i]);
    r.bits.assign(1u << r.vars.size(), 0);
    for (unsigned a = 0; a < r.bits.size(); ++a) {
      unsigned lo = a & ((1u << pos) - 1), hi = a >> pos;
      unsigned full = lo | (val << pos) | (hi << (pos + 1));
      r.bits[a] = f.bits[full];
    }
    return r;
  }
  static TT normalizeTT(TT f) {
    for (unsigned i = 0; i < f.vars.size();) {
      TT c0 = cofactorTT(f, i, 0), c1 = cofactorTT(f, i, 1);
      if (c0.bits == c1.bits) {
        f = std::move(c0);
        i = 0;
      } else
        ++i;
    }
    return f;
  }
  static TT notTT(TT f) {
    for (auto &b : f.bits)
      b ^= 1;
    return f;
  }

  struct SPlan {
    double cost = 1e30;
    int kind = -1; // 0 const, 1 literal, 2 cell, 3 shannon
    unsigned pos = 0;
    int op = 0;
  };
  bool verboseSel = false;
  std::map<std::string, SPlan> planMemo;
  std::map<std::string, Sig> builtMemo;
  SmallVector<Sig> leafSigs;

  double gateArea(unsigned n, TruthTable t) {
    auto m = cellLib->lookup(n, t, true);
    return m ? m->cell->area : 1e30;
  }
  static TruthTable ttOfSmall(const TT &f) {
    TruthTable t = 0;
    for (unsigned a = 0; a < f.bits.size(); ++a)
      if (f.bits[a])
        t |= TruthTable(1) << a;
    return t;
  }
  static TruthTable opTT(int op) {
    switch (op) {
    case 0: return 0x8;
    case 1: return 0x4;
    case 2: return 0xE;
    case 3: return 0xD;
    case 4: return 0x6;
    }
    TruthTable t = 0; // mux (x, F0, F1)
    for (unsigned a = 0; a < 8; ++a) {
      bool x = a & 1, f0 = a & 2, f1 = a & 4;
      if (x ? f1 : f0)
        t |= TruthTable(1) << a;
    }
    return t;
  }
  static bool allConst(const TT &t, int v) {
    for (uint8_t b : t.bits)
      if (b != v)
        return false;
    return true;
  }
  // classify the Shannon split of f on variable position `pos`
  static int splitOp(const TT &f0, const TT &f1) {
    if (allConst(f0, 0))
      return 0; // x & f1
    if (allConst(f1, 0))
      return 1; // ~x & f0
    if (allConst(f1, 1))
      return 2; // x | f0
    if (allConst(f0, 1))
      return 3; // ~x | f1
    if (f0.bits.size() == f1.bits.size() && f0.vars == f1.vars) {
      bool x = true;
      for (unsigned i = 0; i < f0.bits.size(); ++i)
        if (f0.bits[i] == f1.bits[i])
          x = false;
      if (x)
        return 4; // x ^ f0
    }
    return 5;
  }
  const SPlan &planOf(const TT &f) {
    std::string k = f.key();
    auto it = planMemo.find(k);
    if (it != planMemo.end())
      return it->second;
    SPlan p;
    int cv;
    if (f.isConst(cv)) {
      p.kind = 0;
      p.cost = 0;
    } else if (f.vars.size() == 1) {
      p.kind = 1;
      p.cost = 0;
    } else {
      if (f.vars.size() <= 5) {
        double a = gateArea(f.vars.size(), ttOfSmall(f));
        if (a < 1e29) {
          p.kind = 2;
          p.cost = a;
        }
      }
      for (unsigned pos = 0; pos < f.vars.size(); ++pos) {
        TT f0 = normalizeTT(cofactorTT(f, pos, 0));
        TT f1 = normalizeTT(cofactorTT(f, pos, 1));
        int op = splitOp(cofactorTT(f, pos, 0), cofactorTT(f, pos, 1));
        if (op == 6 || op == 7) op = 5;
        // sub-functions are normalized; classification used the raw ones
        double c = gateArea(op == 5 ? 3 : 2, opTT(op));
        if (c > 1e29 && (op == 4 || op == 5)) {
          // no XOR / mux cell: (x & f1) | (~x & f0)
          op = 6;
          c = gateArea(4, ao22());
          if (c > 1e29) {
            op = 7;
            c = 2 * gateArea(2, and2()) + gateArea(2, orN(2));
          }
        }
        if (c > 1e29)
          continue;
        if (op == 0 || op == 3)
          c += planOf(f1).cost;
        else if (op == 1 || op == 2 || op == 4)
          c += planOf(f0).cost;
        else
          c += planOf(f0).cost + planOf(f1).cost;
        c += 1e-4; // prefer fewer levels on ties through recursion depth
        if (c < p.cost) {
          p.kind = 3;
          p.pos = pos;
          p.op = op;
          p.cost = c;
        }
      }
    }
    return planMemo[k] = p;
  }
  std::optional<Sig> buildTT(OpBuilder &b, Location loc, ModuleOp top,
                             const TT &f) {
    std::string k = f.key();
    auto it = builtMemo.find(k);
    if (it != builtMemo.end())
      return it->second;
    int cv;
    if (f.isConst(cv))
      return builtMemo[k] = Sig{Value(), false, cv};
    if (f.vars.size() == 1) {
      Sig s = leafSigs[f.vars[0]];
      if (f.bits[1] == 0) // function is ~x
        s.neg = !s.neg;
      return builtMemo[k] = s;
    }
    SPlan p = planOf(f);
    std::optional<Sig> r;
    if (p.kind == 2) {
      SmallVector<Sig, 4> in;
      for (unsigned v : f.vars)
        in.push_back(leafSigs[v]);
      r = gate(b, loc, top, ttOfSmall(f), in);
    } else if (p.kind == 3) {
      TT f0 = normalizeTT(cofactorTT(f, p.pos, 0));
      TT f1 = normalizeTT(cofactorTT(f, p.pos, 1));
      Sig x = leafSigs[f.vars[p.pos]];
      auto g0 = (p.op == 0 || p.op == 3) ? std::optional<Sig>(Sig{})
                                         : buildTT(b, loc, top, f0);
      auto g1 = (p.op == 1 || p.op == 2 || p.op == 4)
                    ? std::optional<Sig>(Sig{})
                    : buildTT(b, loc, top, f1);
      if (!g0 || !g1)
        return std::nullopt;
      Sig nx = x;
      nx.neg = !nx.neg;
      switch (p.op) {
      case 0: case 3: r = gate(b, loc, top, opTT(p.op), {x, *g1}); break;
      case 1: case 2: case 4: r = gate(b, loc, top, opTT(p.op), {x, *g0}); break;
      case 6: r = gate(b, loc, top, ao22(), {x, *g1, nx, *g0}); break;
      case 7: {
        auto t1 = gate(b, loc, top, and2(), {x, *g1});
        auto t0 = gate(b, loc, top, and2(), {nx, *g0});
        if (t1 && t0)
          r = gate(b, loc, top, orN(2), {*t1, *t0});
        break;
      }
      default: r = gate(b, loc, top, opTT(5), {x, *g0, *g1}); break;
      }
    }
    if (!r)
      return std::nullopt;
    return builtMemo[k] = *r;
  }
  // The select of the assignments marked in `on`, as a Sig whose wire has
  // polarity `wantNeg` when a cell of that output polarity exists.
  std::optional<Sig> buildSelect(OpBuilder &b, Location loc, ModuleOp top,
                                 const std::vector<uint8_t> &on, unsigned L,
                                 bool wantNeg) {
    TT f;
    for (unsigned i = 0; i < L; ++i)
      f.vars.push_back(i);
    f.bits = on;
    f = normalizeTT(f);
    std::optional<Sig> best;
    for (int pass = 0; pass < 2; ++pass) {
      // pass 0: build the select as it is; pass 1: build its complement
      auto r = buildTT(b, loc, top, pass ? notTT(f) : f);
      if (!r)
        continue;
      Sig s = *r;
      if (pass && s.k < 0)
        s.neg = !s.neg;
      else if (pass)
        s.k = 1 - s.k;
      if (s.k >= 0 || s.neg == wantNeg)
        return s;
      if (!best)
        best = s;
    }
    return best;
  }
  // The leaf bit of variable i as a Sig of the current insertion point.
  void makeLeafSigs(OpBuilder &b, Location loc, const DecodeIn &d) {
    leafSigs.clear();
    for (auto &[v, bit] : d.leaves)
      leafSigs.push_back(Sig{extractBit(b, loc, v, bit), false, -1});
  }
  void annotateFanout(const Sig &s) {
    if (!selectMaxFanout.getValue() || s.k >= 0)
      return;
    if (Operation *op = s.v.getDefiningOp())
      if (isa<hw::InstanceOp>(op))
        op->setAttr("circt_abc.max_fanout",
                    IntegerAttr::get(IntegerType::get(op->getContext(), 32),
                                     selectMaxFanout.getValue()));
  }

  struct RecipeIn {
    Value root;
    unsigned N;
    bool signFill;
    const std::map<long, SmallVector<Sig>> *byShift;
    long smin;
    bool complete;
    std::optional<NegMatch> neg;
    const DecodeIn *dec = nullptr;
  };

  // Builds the output bits (LSB first); nullopt when a needed cell is missing.
  std::optional<SmallVector<Value>>
  attemptRecipe(OpBuilder &b, Location loc, ModuleOp top, const RecipeIn &in,
                ArrayRef<unsigned> Rs, ArrayRef<unsigned> caps, bool useNeg,
                unsigned W, unsigned off = 0) {
    unsigned K = Rs.size() + 1;
    std::vector<long> stride(K + 2, 1);
    for (unsigned k = 1; k < K; ++k)
      stride[k + 1] = stride[k] * Rs[k - 1];
    auto digit = [&](unsigned k, long t) -> long {
      return k < K ? (t / stride[k]) % Rs[k - 1] : t / stride[K];
    };
    std::vector<std::map<long, SmallVector<Sig>>> selArms(K + 1);
    // direct decode: digit of every assignment, per level
    std::vector<std::vector<long>> dg;
    unsigned nA = 0, nL = 0;
    if (in.dec) {
      const DecodeIn &d = *in.dec;
      nL = d.leaves.size();
      nA = 1u << nL;
      dg.assign(K + 1, std::vector<long>(nA, -1));
      for (unsigned A = 0; A < nA; ++A) {
        long sh = d.shiftOf[A];
        if (d.isDefault[A] && defaultDontCare.getValue() && d.affine)
          sh = d.contOf[A];
        if (sh == LONG_MIN)
          continue;
        long u = sh - in.smin + off;
        if (u < 0)
          return std::nullopt;
        for (unsigned k = 1; k <= K; ++k) {
          dg[k][A] = digit(k, u);
          selArms[k][dg[k][A]]; // present
        }
      }
      makeLeafSigs(b, loc, d);
      planMemo.clear();
      builtMemo.clear();
    } else
      for (auto &[sh, sels] : *in.byShift)
        for (unsigned k = 1; k <= K; ++k)
          for (const Sig &x : sels)
            selArms[k][digit(k, sh - in.smin)].push_back(x);
    std::optional<SmallVector<Sig>> negBits;
    if (useNeg) {
      negBits = buildNegate(b, loc, top, *in.neg, in.N);
      if (!negBits)
        return std::nullopt;
    }
    auto data0 = [&](long i) -> Sig {
      if (i < 0 || (i >= (long)in.N && !in.signFill))
        return Sig{Value(), false, 0};
      unsigned bit = (unsigned)std::min<long>(i, (long)in.N - 1);
      if (negBits)
        return (*negBits)[bit];
      return Sig{extractBit(b, loc, in.root, bit), false, -1};
    };
    std::map<std::tuple<unsigned, std::vector<long>, bool>, Sig> selCache;
    bool selFail = false;
    auto selFor = [&](unsigned k, const std::set<long> &D, bool wantNeg) {
      auto key = std::make_tuple(k, std::vector<long>(D.begin(), D.end()),
                                 wantNeg);
      auto it = selCache.find(key);
      if (it != selCache.end())
        return it->second;
      if (in.dec) {
        std::vector<uint8_t> on(nA, 0);
        for (unsigned A = 0; A < nA; ++A)
          on[A] = dg[k][A] >= 0 && D.count(dg[k][A]);
        unsigned before = nCells;
        auto r = buildSelect(b, loc, top, on, nL, wantNeg);
        if (verboseSel && r) {
          llvm::errs() << "[map-shift-select] select level " << k << " digits {";
          for (long d : D)
            llvm::errs() << d << ",";
          llvm::errs() << "} on=" << std::count(on.begin(), on.end(), 1) << "/" << nA
                       << " new-cells=" << (nCells - before) << " neg=" << r->neg << "\n";
        }
        if (!r) {
          selFail = true;
          return selCache[key] = Sig{Value(), false, 0};
        }
        annotateFanout(*r);
        return selCache[key] = *r;
      }
      SmallVector<Value> ops;
      for (long d : D)
        for (const Sig &x : selArms[k][d]) {
          Value v = x.neg ? combNot(b, loc, x.v) : x.v;
          if (!llvm::is_contained(ops, v))
            ops.push_back(v);
        }
      Value v = ops[0];
      if (ops.size() > 1) {
        auto o = comb::OrOp::create(b, loc, ValueRange(ops), false);
        created.push_back(o);
        v = o.getResult();
      }
      if (wantNeg)
        v = combNot(b, loc, v);
      return selCache[key] = Sig{v, wantNeg, -1};
    };
    bool ok = true;
    std::map<std::pair<unsigned, long>, Sig> memo;
    std::function<Sig(unsigned, long)> V = [&](unsigned k, long m) -> Sig {
      if (!ok)
        return Sig{Value(), false, 0};
      if (k == 0)
        return data0(m);
      auto mk = std::make_pair(k, m);
      auto it = memo.find(mk);
      if (it != memo.end())
        return it->second;
      std::map<SigId, std::pair<Sig, std::set<long>>> groups;
      for (auto &[d, _] : selArms[k]) {
        long m2 = m - d * stride[k];
        if (m2 < 0)
          continue;
        Sig c = V(k - 1, m2);
        if (!ok)
          return Sig{Value(), false, 0};
        if (c.k >= 0)
          continue; // constant 0 contributes nothing
        auto &g = groups[idOf(c)];
        g.first = c;
        g.second.insert(d);
      }
      Sig r{Value(), false, 0};
      if (groups.size() == 1 && in.complete &&
          groups.begin()->second.second.size() == selArms[k].size()) {
        r = groups.begin()->second.first; // every digit selects the same datum
      } else if (!groups.empty()) {
        SmallVector<std::pair<Sig, Sig>> terms;
        for (auto &[id, g] : groups)
          terms.push_back({selFor(k, g.second, g.first.neg), g.first});
        if (selFail) {
          ok = false;
          return Sig{Value(), false, 0};
        }
        auto o = andOrMerged(b, loc, top, terms, caps[k - 1]);
        if (!o) {
          ok = false;
          return Sig{Value(), false, 0};
        }
        r = *o;
      }
      return memo[mk] = r;
    };
    SmallVector<Value> bits;
    for (unsigned j = 0; j < W && ok; ++j) {
      Sig o = V(K, (long)j - in.smin + (long)off);
      if (!ok)
        break;
      auto v = materialize(b, loc, top, o);
      if (!v) {
        ok = false;
        break;
      }
      bits.push_back(*v);
    }
    if (!ok)
      return std::nullopt;
    return bits;
  }

  static SmallVector<unsigned> parseList(StringRef s) {
    SmallVector<unsigned> r;
    SmallVector<StringRef> parts;
    s.split(parts, ',', -1, false);
    for (StringRef p : parts) {
      unsigned v = 0;
      if (p.trim().getAsInteger(10, v) || v < 1)
        return {};
      r.push_back(v);
    }
    return r;
  }

  void discardCreated() {
    for (Operation *op : llvm::reverse(created))
      op->erase();
    created.clear();
  }

  void clearCaches() {
    invCache.clear();
    extractCache.clear();
    orMemo.clear();
    andOrMemo.clear();
  }


  std::optional<DecodeIn> makeDecode(Value amt, const SmallVector<Arm> &arms,
                                     ArrayRef<long> shift, int negIdx,
                                     bool &complete) {
    Evaluator ev;
    unsigned aw = amt.getType().getIntOrFloatBitWidth();
    ev.collect(amt, 0, aw);
    if (!ev.ok || ev.leaves.empty() || ev.leaves.size() > 12)
      return std::nullopt;
    std::map<uint64_t, long> cs;
    for (auto [k, a] : llvm::enumerate(arms)) {
      if ((int)k == negIdx)
        continue;
      auto ec = eqConst(a.sel.v);
      if (!ec)
        return std::nullopt;
      cs[ec->second.getZExtValue()] = shift[k];
    }
    DecodeIn d;
    d.leaves = ev.leaves;
    unsigned L = d.leaves.size(), N = 1u << L;
    long delta = 0;
    bool first = true;
    d.affine = true;
    for (auto &[v, sh] : cs) {
      if (first)
        delta = sh - (long)v;
      else if (sh - (long)v != delta)
        d.affine = false;
      first = false;
    }
    d.shiftOf.assign(N, LONG_MIN);
    d.contOf.assign(N, LONG_MIN);
    d.isDefault.assign(N, 0);
    complete = true;
    for (unsigned A = 0; A < N; ++A) {
      uint64_t v = ev.eval(amt, A).getZExtValue();
      auto it = cs.find(v);
      if (it != cs.end()) {
        d.shiftOf[A] = d.contOf[A] = it->second;
      } else if (negIdx >= 0) {
        d.shiftOf[A] = shift[negIdx];
        d.contOf[A] = (long)v + delta;
        d.isDefault[A] = 1;
      } else {
        complete = false;
        if (defaultDontCare.getValue() && d.affine) {
          d.contOf[A] = (long)v + delta;
          d.isDefault[A] = 1;
        }
      }
    }
    if (!complete && defaultDontCare.getValue() && d.affine)
      complete = true;
    return d;
  }

  // Picks the recipe of least cell area and builds it. Returns the output
  // bits (LSB first), or nullopt when no recipe can be built.
  std::optional<SmallVector<Value>>
  buildRecipe(OpBuilder &b, Location loc, ModuleOp top, const RecipeIn &in,
              unsigned W) {
    struct Cand {
      SmallVector<unsigned> Rs, caps;
      bool neg;
      unsigned off = 0;
    };
    SmallVector<Cand> cands;
    SmallVector<StringRef> items;
    StringRef(recipes.getValue()).split(items, ';', -1, false);
    for (StringRef it : items) {
      auto [rs, cs] = it.split('@');
      Cand c;
      c.Rs = parseList(rs);
      c.caps = parseList(cs);
      if (c.Rs.empty() && !rs.trim().empty())
        continue;
      if (c.caps.size() != c.Rs.size() + 1)
        continue;
      // The ripple negate is always preferred when the shape matches (the
      // alternative leaves the negation to the logic mapper, whose cost is
      // not counted here).
      c.neg = in.neg.has_value();
      if (in.dec && !c.Rs.empty()) {
        // digit boundary offsets 0 .. R1-1
        for (unsigned a = 0; a < c.Rs[0]; ++a) {
          Cand ca = c;
          ca.off = a;
          cands.push_back(ca);
        }
      } else
        cands.push_back(c);
    }
    double best = 0;
    int bestIdx = -1;
    unsigned cells0 = nCells;
    for (auto [i, c] : llvm::enumerate(cands)) {
      clearCaches();
      areaAcc = 0;
      created.clear();
      auto r = attemptRecipe(b, loc, top, in, c.Rs, c.caps, c.neg, W, c.off);
      double a = areaAcc;
      nCells = cells0;
      llvm::errs() << "[map-shift-select] candidate " << i << " neg=" << c.neg << " off=" << c.off << (r ? " area=" : " FAILED area=") << a << " cells=" << created.size() << "\n";
      discardCreated();
      if (r && (bestIdx < 0 || a < best)) {
        best = a;
        bestIdx = i;
      }
    }
    if (bestIdx < 0)
      return std::nullopt;
    clearCaches();
    areaAcc = 0;
    created.clear();
    nCells = cells0;
    const Cand &c = cands[bestIdx];
    verboseSel = true;
    auto r = attemptRecipe(b, loc, top, in, c.Rs, c.caps, c.neg, W, c.off);
    verboseSel = false;
    llvm::errs() << "[map-shift-select] recipe off=" << c.off << " radices=";
    for (unsigned x : c.Rs)
      llvm::errs() << x << ",";
    llvm::errs() << " caps=";
    for (unsigned x : c.caps)
      llvm::errs() << x << ",";
    llvm::errs() << " negate-ripple=" << c.neg << " area=" << areaAcc << "\n";
    return r;
  }


  // Returns true when the OR was rewritten.
  bool rewrite(comb::OrOp orOp, ModuleOp top, unsigned &declined) {
    unsigned W = orOp.getType().getIntOrFloatBitWidth();
    SmallVector<Arm> arms;
    for (Value in : orOp.getInputs()) {
      auto mux = in.getDefiningOp<comb::MuxOp>();
      if (!mux)
        return false;
      if (isZero(mux.getFalseValue()))
        arms.push_back({Sig{mux.getCond(), false, -1}, mux.getTrueValue()});
      else if (isZero(mux.getTrueValue()))
        arms.push_back({Sig{mux.getCond(), true, -1}, mux.getFalseValue()});
      else
        return false;
    }
    return rewriteArms(orOp.getOperation(), std::move(arms), W, top, declined);
  }

  // A priority chain mux(c1, w1, mux(c2, w2, ... default)) whose conditions
  // compare one amount with distinct constants is the same one-hot select,
  // the default being the arm "none of them". Run before canonicalization has
  // pushed the select into the words (the chain is then still one word wide).
  bool rewriteChain(comb::MuxOp top_, ModuleOp top, unsigned &declined) {
    unsigned W = top_.getType().getIntOrFloatBitWidth();
    SmallVector<Arm> arms;
    SmallVector<Value> conds;
    Value v = top_.getResult();
    while (auto m = v.getDefiningOp<comb::MuxOp>()) {
      if (!eqConst(m.getCond()))
        break;
      arms.push_back({Sig{m.getCond(), false, -1}, m.getTrueValue()});
      conds.push_back(m.getCond());
      v = m.getFalseValue();
    }
    if (arms.size() < minArms.getValue())
      return false;
    OpBuilder b(top_);
    auto none = comb::OrOp::create(b, top_.getLoc(), ValueRange(conds), false);
    arms.push_back({Sig{none.getResult(), true, -1}, v});
    unsigned nArms = arms.size();
    bool done = rewriteArms(top_.getOperation(), std::move(arms), W, top, declined);
    llvm::errs() << "[map-shift-select] chain of " << nArms << " arms, width " << W
                 << (done ? ": rewritten\n" : ": left alone\n");
    if (!done)
      none.erase();
    return done;
  }

  bool rewriteArms(Operation *resOp, SmallVector<Arm> arms, unsigned W,
                   ModuleOp top, unsigned &declined) {
    // One-hot invariant, checked syntactically.
    Value amt;
    llvm::DenseSet<uint64_t> seen;
    SmallVector<Value> posSels;
    int negIdx = -1;
    for (auto [i, a] : llvm::enumerate(arms)) {
      if (a.sel.neg) {
        if (negIdx >= 0)
          return false;
        negIdx = i;
        continue;
      }
      auto ec = eqConst(a.sel.v);
      if (!ec || (amt && ec->first != amt) || ec->second.getBitWidth() > 63)
        return false;
      amt = ec->first;
      if (!seen.insert(ec->second.getZExtValue()).second)
        return false;
      posSels.push_back(a.sel.v);
    }
    if (negIdx >= 0) {
      auto none = arms[negIdx].sel.v.getDefiningOp<comb::OrOp>();
      if (!none || none.getNumOperands() != posSels.size())
        return false;
      llvm::DenseSet<Value> ps(posSels.begin(), posSels.end());
      for (Value o : none.getInputs())
        if (!ps.count(o))
          return false;
    }
    // Leaves of every arm; the shift of an arm from one distinguishing leaf.
    SmallVector<SmallVector<Leaf>> L(arms.size());
    Value root;
    for (auto [k, a] : llvm::enumerate(arms)) {
      for (unsigned j = 0; j < W; ++j) {
        Leaf lf = leafOf(a.word, j);
        L[k].push_back(lf);
        if (lf.k < 0) {
          if (!root)
            root = lf.v;
          else if (root != lf.v)
            return false;
        }
      }
    }
    if (!root || !root.getType().isInteger())
      return false;
    unsigned N = root.getType().getIntOrFloatBitWidth();
    if (N < 2)
      return false;
    // The word above the top bit of `root` is either sign-extended (the top
    // bit repeated) or zero-filled; the first mode every arm satisfies wins.
    bool signFill = true;
    auto baseLeaf = [&](long i) -> Leaf {
      if (i < 0)
        return Leaf{Value(), 0, 0};
      if (i >= (long)N && !signFill)
        return Leaf{Value(), 0, 0};
      return Leaf{root, (unsigned)std::min<long>(i, (long)N - 1), -1};
    };
    SmallVector<long> shift(arms.size());
    auto verify = [&]() -> bool {
      for (auto [k, a] : llvm::enumerate(arms)) {
        bool found = false;
        for (unsigned j = 0; j < W && !found; ++j)
          if (L[k][j].k < 0 && L[k][j].bit + 1 < N) {
            shift[k] = (long)j - (long)L[k][j].bit;
            found = true;
          }
        if (!found)
          return false;
        for (unsigned j = 0; j < W; ++j)
          if (!(L[k][j] == baseLeaf((long)j - shift[k])))
            return false;
      }
      return true;
    };
    if (!verify()) {
      signFill = false;
      if (!verify())
        return false;
    }
    // Group by shift (equal shifts have identical words by the check above).
    std::map<long, SmallVector<Sig>> byShift;
    for (auto [k, a] : llvm::enumerate(arms))
      byShift[shift[k]].push_back(a.sel);
    long smin = byShift.begin()->first;
    if (!recipes.getValue().empty()) {
      bool complete = negIdx >= 0;
      if (!complete) {
        unsigned aw = amt.getType().getIntOrFloatBitWidth();
        complete = aw < 20 && (seen.size() == (1ull << aw));
      }
      RecipeIn in{root, N, signFill, &byShift, smin, complete,
                  matchNegate(root, N)};
      std::optional<DecodeIn> dec;
      if (directDecode.getValue()) {
        bool decComplete = complete;
        dec = makeDecode(amt, arms, shift, negIdx, decComplete);
        if (dec) {
          in.dec = &*dec;
          in.complete = decComplete;
        } else
          llvm::errs() << "[map-shift-select] direct-decode: amount not "
                          "evaluable, comparators kept\n";
      }
      OpBuilder b(resOp);
      Location loc = resOp->getLoc();
      clearCaches();
      created.clear();
      auto bitsOpt = buildRecipe(b, loc, top, in, W);
      if (!bitsOpt) {
        ++declined;
        return false;
      }
      SmallVector<Value> msbFirst(bitsOpt->rbegin(), bitsOpt->rend());
      Value out = W == 1 ? msbFirst[0]
                         : comb::ConcatOp::create(b, loc, msbFirst).getResult();
      resOp->getResult(0).replaceAllUsesWith(out);
      resOp->erase();
      created.clear();
      return true;
    }
    unsigned R = std::max(2u, radix.getValue());
    OpBuilder b(resOp);
    Location loc = resOp->getLoc();
    created.clear();
    invCache.clear();
    extractCache.clear();
    orMemo.clear();
    andOrMemo.clear();
    unsigned cellsBefore = nCells;
    bool ok = true;
    auto fail = [&] { ok = false; };
    // Group selects.
    std::map<long, SmallVector<Sig>> qSel, rSel;
    for (auto &[s, sels] : byShift) {
      long t = s - smin;
      for (Sig x : sels) {
        qSel[t / R].push_back(x);
        rSel[t % R].push_back(x);
      }
    }
    std::map<long, Sig> Q, Rr;
    for (auto &[q, v] : qSel) {
      auto r = orReduce(b, loc, top, v);
      if (!r) {
        fail();
        break;
      }
      Q[q] = *r;
    }
    for (auto &[r, v] : rSel) {
      if (!ok)
        break;
      auto x = orReduce(b, loc, top, v);
      if (!x) {
        fail();
        break;
      }
      Rr[r] = *x;
    }
    auto dataSig = [&](long i) -> Sig {
      Leaf lf = baseLeaf(i);
      if (lf.k >= 0)
        return Sig{Value(), false, lf.k};
      return Sig{extractBit(b, loc, lf.v, lf.bit), false, -1};
    };
    std::map<long, Sig> Y;
    auto yOf = [&](long m) -> std::optional<Sig> {
      auto it = Y.find(m);
      if (it != Y.end())
        return it->second;
      SmallVector<std::pair<Sig, Sig>> terms;
      for (auto &[r, rs] : Rr)
        if (m - r >= 0)
          terms.push_back({rs, dataSig(m - r)});
      auto y = andOr(b, loc, top, terms);
      if (y)
        Y[m] = *y;
      return y;
    };
    SmallVector<Value> bitsLsbFirst;
    for (unsigned j = 0; j < W && ok; ++j) {
      SmallVector<std::pair<Sig, Sig>> terms;
      for (auto &[q, qs] : Q) {
        long m = (long)j - smin - (long)R * q;
        if (m < 0)
          continue;
        auto y = yOf(m);
        if (!y) {
          fail();
          break;
        }
        terms.push_back({qs, *y});
      }
      if (!ok)
        break;
      auto o = andOr(b, loc, top, terms);
      if (!o) {
        fail();
        break;
      }
      auto v = materialize(b, loc, top, *o);
      if (!v) {
        fail();
        break;
      }
      bitsLsbFirst.push_back(*v);
    }
    if (!ok) {
      for (Operation *op : llvm::reverse(created))
        op->erase();
      created.clear();
      nCells = cellsBefore;
      ++declined;
      return false;
    }
    SmallVector<Value> msbFirst(bitsLsbFirst.rbegin(), bitsLsbFirst.rend());
    Value out = W == 1 ? msbFirst[0]
                       : comb::ConcatOp::create(b, loc, msbFirst).getResult();
    resOp->getResult(0).replaceAllUsesWith(out);
    resOp->erase();
    created.clear();
    return true;
  }

  void runOnOperation() override {
    if (!enablePass.getValue())
      return;
    ModuleOp top = getOperation();
    auto dropIds = llvm::scope_exit(
        [&] { top.walk([](Operation *o) { o->removeAttr(kIdAttr); }); });
    if (libertyPath.getValue().empty()) {
      top.emitError("map-shift-select: liberty= is required");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      top.emitError("map-shift-select: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    cellLib = &*lib;
    SmallVector<comb::OrOp> work;
    top.walk([&](comb::OrOp o) {
      if (o.getType().getIntOrFloatBitWidth() >= minWidth.getValue() &&
          o.getNumOperands() >= minArms.getValue())
        work.push_back(o);
    });
    SmallVector<comb::MuxOp> chainTops;
    if (chains.getValue())
      top.walk([&](comb::MuxOp m) {
        if (!eqConst(m.getCond()) ||
            m.getType().getIntOrFloatBitWidth() < minWidth.getValue())
          return;
        for (Operation *u : m.getResult().getUsers())
          if (auto um = dyn_cast<comb::MuxOp>(u))
            if (um.getFalseValue() == m.getResult() && eqConst(um.getCond()))
              return; // not the head of the chain
        chainTops.push_back(m);
      });
    unsigned nDone = 0, nDeclined = 0, cellsBefore = nCells;
    for (comb::MuxOp m : chainTops)
      if (rewriteChain(m, top, nDeclined))
        ++nDone;
    for (comb::OrOp o : work)
      if (rewrite(o, top, nDeclined))
        ++nDone;
    if (nDone || nDeclined)
      llvm::errs() << "[map-shift-select] " << nDone
                   << " one-hot select(s) built as radix-" << radix.getValue()
                   << " AND-OR networks, " << (nCells - cellsBefore)
                   << " cell(s); " << nDeclined << " declined\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createMapShiftSelectPass() {
  return std::make_unique<MapShiftSelectPass>();
}

} // namespace circt_abc
