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
    l2RangeGate = o.l2RangeGate.getValue();
    selectDupFanout = o.selectDupFanout.getValue();
    l2RangeAligned = o.l2RangeAligned.getValue();
    l2RangeCompose = o.l2RangeCompose.getValue();
    windowForm = o.windowForm.getValue();
    windowCount = o.windowCount.getValue();
    windowCopyCost = o.windowCopyCost.getValue();
    windowClip = o.windowClip.getValue();
    windowPerNode = o.windowPerNode.getValue();
    windowCuts = o.windowCuts.getValue();
    windowTypes = o.windowTypes.getValue();
    windowGeneral = o.windowGeneral.getValue();
    windowTypesMax = o.windowTypesMax.getValue();
    windowDirect = o.windowDirect.getValue();
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
  Option<unsigned> l2RangeGate{
      *this, "l2-range-gate", llvm::cl::init(0),
      llvm::cl::desc("with direct-decode and a recipe of two or more digits: "
                     "gate each node of the level below the last one with "
                     "the coarse range of the last digit that consumes it, "
                     "so the node is quiet unless its range is the active "
                     "one (select = digit AND range). N = width of a range in "
                     "last-digit values: the consumers of a node are split "
                     "into ranges of N consecutive digit values, one node "
                     "copy per range (N=1: one copy per consumer, joint "
                     "decode; a large N keeps every node shared and only "
                     "gates the nodes that are not used for every digit). "
                     "A node consumed by one digit needs no last-level "
                     "select. 0 = off")};
  Option<bool> l2RangeCompose{
      *this, "l2-range-compose", llvm::cl::init(false),
      llvm::cl::desc("with l2-range-gate: build each gated select as an AND "
                     "of the digit select and the range select (shared with "
                     "the other selects of that digit set / range) instead "
                     "of one network per gated select")};
  Option<bool> l2RangeAligned{
      *this, "l2-range-aligned", llvm::cl::init(false),
      llvm::cl::desc("with l2-range-gate=N: a node's range is the whole "
                     "aligned block of N last-digit values that holds a "
                     "consumer (digit / N), not only the consumers inside "
                     "it: fewer distinct range selects (shared by all nodes "
                     "of a block), nodes at the "
                     "ends of the word are active for a few digits they do "
                     "not use")};
  Option<unsigned> selectDupFanout{
      *this, "select-dup-fanout", llvm::cl::init(0),
      llvm::cl::desc("with direct-decode: a digit-select gate drives at most "
                     "N loads; further loads get a copy of the gate (same "
                     "inputs) instead of a buffer. The copies are annotated "
                     "circt_abc.max_fanout = max(N, select-max-fanout). "
                     "0 = one gate per select")};
  Option<bool> windowForm{
      *this, "window-form", llvm::cl::init(false),
      llvm::cl::desc("with direct-decode and a recipe of two or more digits: "
                     "build the window form. The levels below the one before "
                     "the last stay shared and ungated; every node of the "
                     "level before the last exists once per WINDOW (a "
                     "contiguous range of shift amounts) that holds an amount "
                     "using it, selected by 'digit AND window' and quiet "
                     "outside it; the last level selects a node copy with a "
                     "select that only has to tell the last digits inside "
                     "the copy's window apart (a pass-through when the window "
                     "lies inside one last digit). The windows are chosen by "
                     "a small search over the partitions of the amount range "
                     "(cost: copies against the activity of their outputs)")};
  Option<unsigned> windowCount{
      *this, "window-count", llvm::cl::init(0),
      llvm::cl::desc("with window-form: the number of windows (0 = chosen by "
                     "the cost)")};
  Option<unsigned> windowCopyCost{
      *this, "window-copy-cost", llvm::cl::init(150),
      llvm::cl::desc("with window-form: cost of one node copy (the two loads "
                     "it puts on the shared nodes below and its cell), in "
                     "percent of one always-active load")};
  Option<bool> windowClip{
      *this, "window-clip", llvm::cl::init(false),
      llvm::cl::desc("with window-form: a copy's select covers only the "
                     "amounts of its window that use the node (more distinct "
                     "selects, lower activity) instead of the whole window "
                     "(shared by every copy of the window)")};
  Option<bool> windowPerNode{
      *this, "window-per-node", llvm::cl::init(false),
      llvm::cl::desc("with window-form: each node takes the chosen windows or "
                     "a single copy over the whole amount range (selected by "
                     "its digit alone), whichever its own cost prefers")};
  Option<std::string> windowCuts{
      *this, "window-cuts", llvm::cl::init(""),
      llvm::cl::desc("with window-form: explicit cuts of the amount range "
                     "(comma-separated shifts counted from the smallest one "
                     "plus the digit offset, as the window report prints "
                     "them) instead of the search; the digit offset is then "
                     "the one the recipe search picks")};
  Option<bool> windowGeneral{
      *this, "window-general", llvm::cl::init(false),
      llvm::cl::desc("with window-form: overlapping windows, each a range of "
                     "blocks (values of the gated level's digit and above) "
                     "with its own digit phase; every output picks, per "
                     "block, a node copy whose window holds the block "
                     "(greedy cover minimising copies); window types from "
                     "window-types or a search over sets of up to "
                     "window-types-max types")};
  Option<std::string> windowTypes{
      *this, "window-types", llvm::cl::init(""),
      llvm::cl::desc("with window-general: explicit window types "
                     "'phase:first-last;...' in blocks (block = shift from "
                     "the smallest plus the digit offset, divided by the "
                     "stride of the gated level)")};
  Option<unsigned> windowTypesMax{
      *this, "window-types-max", llvm::cl::init(2),
      llvm::cl::desc("with window-general: most window types the search "
                     "combines (1 to 3)")};
  Option<unsigned> windowDirect{
      *this, "window-direct", llvm::cl::init(0),
      llvm::cl::desc("with window-general: an output may take a lower-level "
                     "node directly under a block select (no copy cell) at "
                     "this cost per requirement, percent of one always-active "
                     "load; the cover then picks copies by cost per covered "
                     "requirement. 0 = off")};
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
    if (l2RangeGate.getValue() || selectDupFanout.getValue()) {
      // Mixed operand polarities: make them uniform by inverting the minority
      // (the cell is then an OAI with complemented operands or an AOI with true
      // ones), not always the complemented ones.
      unsigned nn = 0, np = 0;
      for (auto &x : in)
        if (x.k < 0)
          (x.neg ? nn : np)++;
      if (np && nn && np < nn) {
        auto in2 = in;
        bool okInv = true;
        for (auto &x : in2)
          if (x.k < 0 && !x.neg) {
            auto v = invOf(b, loc, top, x.v);
            if (!v) {
              okInv = false;
              break;
            }
            x = Sig{*v, true, -1};
          }
        if (okInv)
          if (auto r = gateOnce(b, loc, top, f, in2))
            return r;
      }
    }
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
      if (!p) {
        // a term with a constant-1 select (a range-exact node) is just its
        // datum: OR it with the AND-OR of the other terms of the chunk
        SmallVector<std::pair<Sig, Sig>> rest;
        SmallVector<Sig> plain;
        for (size_t t = i; t < i + n; ++t)
          if (merged[t].first.k == 1)
            plain.push_back(merged[t].second);
          else
            rest.push_back(merged[t]);
        if (plain.empty() || n == 1)
          return std::nullopt;
        if (!rest.empty()) {
          auto q = andOrMerged(b, loc, top, rest, cap);
          if (!q)
            return std::nullopt;
          plain.push_back(*q);
        }
        p = orReduce(b, loc, top, plain);
        if (!p)
          return std::nullopt;
      }
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

  //--- window form ------------------------------------------------------------
  //
  // The level before the last (KL = K-1) is where a shared node toggles for
  // shift amounts whose output never uses it: a node V_KL(m) serves up to R_K
  // outputs (one per last digit) and its select is a digit of level KL alone,
  // so it is live for every amount. The window form keeps the levels below KL
  // shared and ungated, and builds each node of level KL once per WINDOW (a
  // contiguous range of amounts, from a partition of the amount range) that
  // holds an amount using it:
  //     V_KL,w(m) = OR_d (S_KL,d & Win_w) & V_KL-1(m - d*stride_KL)
  // so the copy is 0 outside its window. An output then takes, for each last
  // digit d and each window w that holds amounts of d, the copy V_KL,w of its
  // node under a select T that is 1 on the amounts of w with last digit d and
  // 0 on the other amounts of w (anything elsewhere: the copy is 0 there). T
  // is 1 (a pass-through) when w holds no other last digit; otherwise the
  // cheapest of: the exact digit select (shared by every window), "not one of
  // the other digits of the window" (an active-low range select), or the
  // digit inside the window. Every amount without an arm gets 0 from the
  // gated selects (the copies), as in the recipe network.
  //
  // The partition is chosen by a small search over the cuts at multiples of
  // stride_KL (fewer if there are many): cost = per copy (window-copy-cost +
  // activity * (its loads + 1)), activity = share of the amounts in its
  // select, plus 0.5 per last-level term and 1 per distinct window select.
  // Polarity: each level's selects are built in the polarity of the data
  // they gate (as in the recipe network), so levels alternate AOI / OAI.
  template <typename VFn, typename UseSelFn>
  std::optional<SmallVector<Value>> buildWindowForm(
      OpBuilder &b, Location loc, ModuleOp top, const RecipeIn &in, unsigned K,
      const std::vector<long> &stride, ArrayRef<unsigned> caps,
      const std::vector<std::vector<long>> &dg, const std::vector<long> &uOf,
      std::vector<std::map<long, SmallVector<Sig>>> &selArms, unsigned nA,
      unsigned nL, unsigned W, unsigned off, VFn &V, UseSelFn &useSel) {
    const unsigned KL = K - 1;
    const long SK = stride[K];
    std::map<long, unsigned> wt;
    unsigned nLegal = 0;
    for (unsigned A = 0; A < nA; ++A)
      if (uOf[A] >= 0) {
        ++wt[uOf[A]];
        ++nLegal;
      }
    if (wt.empty())
      return std::nullopt;
    long umin = wt.begin()->first, umax = wt.rbegin()->first;
    // nodes of level KL and the last digits that consume them
    std::map<long, std::set<long>> cons;
    for (unsigned j = 0; j < W; ++j) {
      long jj = (long)j - in.smin + (long)off;
      for (auto &[d, _] : selArms[K])
        if (jj - d * SK >= 0)
          cons[jj - d * SK].insert(d);
    }
    // the ungated groups of a node: datum -> level-KL digits
    struct Grp {
      Sig datum;
      std::set<long> D;
    };
    std::map<long, std::vector<Grp>> grpOf;
    std::map<long, bool> plain; // node is one datum for every digit (complete)
    for (auto &[m, _] : cons) {
      std::map<SigId, Grp> g;
      std::vector<SigId> order;
      for (auto &[d, __] : selArms[KL]) {
        long m2 = m - d * stride[KL];
        if (m2 < 0)
          continue;
        Sig c = V(KL - 1, m2, {});
        if (c.k >= 0)
          continue;
        auto id = idOf(c);
        if (!g.count(id))
          order.push_back(id);
        g[id].datum = c;
        g[id].D.insert(d);
      }
      std::vector<Grp> v;
      for (auto &id : order)
        v.push_back(g[id]);
      plain[m] = in.complete && v.size() == 1 &&
                 v[0].D.size() == selArms[KL].size();
      grpOf[m] = std::move(v);
    }
    // candidate cuts
    long G = stride[KL];
    std::vector<long> cuts;
    for (;;) {
      cuts.clear();
      for (long c = (umin / G + 1) * G; c <= umax; c += G)
        cuts.push_back(c);
      if (cuts.size() <= 12)
        break;
      G *= 2;
    }
    auto windowsOf = [&](unsigned mask) {
      std::vector<std::pair<long, long>> ws;
      long lo = umin;
      for (unsigned i = 0; i < cuts.size(); ++i)
        if (mask >> i & 1) {
          ws.push_back({lo, cuts[i]});
          lo = cuts[i];
        }
      ws.push_back({lo, umax + 1});
      return ws;
    };
    // active amounts of node m in window w (sorted); empty = no copy
    auto activeOf = [&](long m, std::pair<long, long> w, bool clip) {
      std::vector<long> act;
      bool used = false;
      const auto &C = cons[m];
      for (auto it = wt.lower_bound(w.first); it != wt.end() && it->first < w.second;
           ++it) {
        bool u = C.count(it->first / SK);
        used |= u;
        if (u || !clip)
          act.push_back(it->first);
      }
      if (!used)
        act.clear();
      return act;
    };
    double A0 = windowCopyCost.getValue() / 100.0;
    bool clip = windowClip.getValue();
    // cost of node m under windows ws: copies, last-level terms, select sets
    auto nodeCost = [&](long m, const std::vector<std::pair<long, long>> &ws,
                        double &cost, unsigned &terms,
                        std::vector<std::vector<long>> &distinct) {
      const auto &C = cons[m];
      for (auto &w : ws) {
        auto act = activeOf(m, w, clip);
        if (act.empty())
          continue;
        unsigned fo = 0, aw = 0;
        for (long d : C) {
          bool has = false;
          for (long u : act)
            has |= u / SK == d;
          fo += has;
        }
        for (long u : act)
          aw += wt[u];
        cost += A0 + (double)aw / nLegal * (fo + 1);
        terms += fo;
        distinct.push_back(act);
      }
    };
    double bestCost = 1e30;
    unsigned bestMask = 0;
    if (!windowCuts.getValue().empty()) {
      // explicit cuts: all must lie inside the amount range (validated for
      // syntax at pass start)
      std::vector<long> ex;
      std::string e;
      if (!parseCuts(windowCuts.getValue(), ex, e)) {
        windowNote = "window-cuts: " + e;
        return std::nullopt;
      }
      for (long v : ex)
        if (!(v > umin && v <= umax)) {
          windowNote = "window-cuts: cut " + std::to_string(v) +
                       " is outside the shift range (" + std::to_string(umin) +
                       "," + std::to_string(umax) + "]";
          return std::nullopt;
        }
      cuts = ex;
    }
    if (cuts.size() >= 31) {
      windowNote = "window form: too many cuts";
      return std::nullopt;
    }
    for (unsigned mask = 0; mask < (1u << cuts.size()); ++mask) {
      if (!windowCuts.getValue().empty() && mask + 1 != (1u << cuts.size()))
        continue; // explicit cuts: all of them
      auto ws = windowsOf(mask);
      if (windowCount.getValue() && ws.size() != windowCount.getValue())
        continue;
      double cost = 0;
      unsigned terms = 0;
      std::set<std::vector<long>> distinct;
      for (auto &[m, C] : cons) {
        if (plain[m] || grpOf[m].empty())
          continue;
        double c1 = 0;
        unsigned t1 = 0;
        std::vector<std::vector<long>> d1;
        nodeCost(m, ws, c1, t1, d1);
        if (windowPerNode.getValue() && ws.size() > 1) {
          double c2 = 0;
          unsigned t2 = 0;
          std::vector<std::vector<long>> d2;
          nodeCost(m, {{umin, umax + 1}}, c2, t2, d2);
          if (c2 < c1) {
            c1 = c2, t1 = t2, d1 = d2;
          }
        }
        cost += c1;
        terms += t1;
        distinct.insert(d1.begin(), d1.end());
      }
      cost += 0.5 * terms + 1.0 * distinct.size();
      if (cost < bestCost - 1e-9) {
        bestCost = cost;
        bestMask = mask;
      }
    }
    if (bestCost > 1e29) {
      if (windowCount.getValue())
        windowNote = "window-count " + std::to_string(windowCount.getValue()) +
                     " is not reachable";
      return std::nullopt;
    }
    auto ws = windowsOf(bestMask);
    // the windows of each node: the chosen partition, or (window-per-node)
    // the whole range when that is cheaper for the node
    std::map<long, std::vector<unsigned>> nodeWins;
    unsigned nSingle = 0;
    {
      unsigned whole = ws.size();
      bool addWhole = false;
      for (auto &[m, C] : cons) {
        std::vector<unsigned> all;
        for (unsigned wi = 0; wi < ws.size(); ++wi)
          all.push_back(wi);
        if (windowPerNode.getValue() && ws.size() > 1 && !plain[m] &&
            !grpOf[m].empty()) {
          double c1 = 0, c2 = 0;
          unsigned t1 = 0, t2 = 0;
          std::vector<std::vector<long>> d1, d2;
          nodeCost(m, ws, c1, t1, d1);
          nodeCost(m, {{umin, umax + 1}}, c2, t2, d2);
          if (c2 < c1) {
            all = {whole};
            addWhole = true;
            ++nSingle;
          }
        }
        nodeWins[m] = all;
      }
      if (addWhole)
        ws.push_back({umin, umax + 1});
    }
    // selects ------------------------------------------------------------------
    auto selCostOf = [&](const std::vector<uint8_t> &on) {
      TT f;
      for (unsigned i = 0; i < nL; ++i)
        f.vars.push_back(i);
      f.bits = on;
      f = normalizeTT(f);
      int cv;
      if (f.isConst(cv))
        return 0.0;
      return std::min(planOf(f).cost, planOf(notTT(f)).cost);
    };
    std::map<std::pair<std::vector<uint8_t>, bool>, Sig> wsCache;
    bool fail = false;
    unsigned nSel = 0;
    auto selOf = [&](unsigned lvl, const std::vector<uint8_t> &on,
                     bool wantNeg) -> Sig {
      auto ck = std::make_pair(on, wantNeg);
      std::vector<long> kv(on.begin(), on.end());
      auto key = std::make_tuple(1000 + lvl, kv, wantNeg, std::vector<long>());
      auto it = wsCache.find(ck);
      if (it != wsCache.end())
        return useSel(key, it->second);
      auto r = buildSelect(b, loc, top, on, nL, wantNeg);
      if (!r) {
        fail = true;
        return Sig{Value(), false, 0};
      }
      ++nSel;
      Sig sel = *r;
      if (sel.k < 0 && sel.neg != wantNeg) {
        // the select in the polarity of its data: one shared inverter on the
        // select, never one per datum
        auto v = invOf(b, loc, top, sel.v);
        if (!v) {
          fail = true;
          return Sig{Value(), false, 0};
        }
        annotateFanout(sel);
        sel = Sig{*v, wantNeg, -1};
      }
      annotateFanout(sel);
      wsCache[ck] = sel;
      return useSel(key, sel);
    };
    // node copies ----------------------------------------------------------------
    std::map<std::pair<long, unsigned>, Sig> copyMemo;
    unsigned nCopies = 0;
    auto copyOf = [&](long m, unsigned wi) -> Sig {
      auto mk = std::make_pair(m, wi);
      auto it = copyMemo.find(mk);
      if (it != copyMemo.end())
        return it->second;
      auto act = activeOf(m, ws[wi], clip);
      std::set<long> actS(act.begin(), act.end());
      SmallVector<std::pair<Sig, Sig>> terms;
      for (auto &g : grpOf[m]) {
        std::vector<uint8_t> on(nA, 0);
        for (unsigned A = 0; A < nA; ++A)
          on[A] = uOf[A] >= 0 && actS.count(uOf[A]) && g.D.count(dg[KL][A]);
        Sig s = selOf(KL, on, g.datum.neg);
        if (fail)
          return Sig{Value(), false, 0};
        if (s.k == 0)
          continue;
        terms.push_back({s, g.datum});
      }
      Sig r{Value(), false, 0};
      if (!terms.empty()) {
        auto o = andOrMerged(b, loc, top, terms, caps[KL - 1]);
        if (!o) {
          fail = true;
          return r;
        }
        r = *o;
        ++nCopies;
      }
      return copyMemo[mk] = r;
    };
    // last level -------------------------------------------------------------------
    unsigned nPass = 0, nTerms = 0;
    SmallVector<Value> bits;
    for (unsigned j = 0; j < W; ++j) {
      long jj = (long)j - in.smin + (long)off;
      struct T3 {
        Sig datum;
        int wi; // -1: an ungated (plain) node
        long m;
        std::set<long> D;
      };
      std::map<std::pair<SigId, int>, T3> groups;
      std::vector<std::pair<SigId, int>> order;
      for (auto &[d, _] : selArms[K]) {
        long m = jj - d * SK;
        if (m < 0 || grpOf[m].empty())
          continue;
        auto add = [&](Sig c, int wi) {
          if (c.k >= 0)
            return;
          auto key = std::make_pair(idOf(c), wi);
          if (!groups.count(key)) {
            order.push_back(key);
            groups[key] = T3{c, wi, m, {}};
          }
          groups[key].D.insert(d);
        };
        if (plain[m]) {
          add(grpOf[m][0].datum, -1);
          continue;
        }
        for (unsigned wi : nodeWins[m]) {
          bool has = false;
          for (auto it = wt.lower_bound(ws[wi].first);
               it != wt.end() && it->first < ws[wi].second; ++it)
            has |= it->first / SK == d;
          if (!has)
            continue;
          Sig c = copyOf(m, wi);
          if (fail)
            return std::nullopt;
          add(c, wi);
        }
      }
      SmallVector<std::pair<Sig, Sig>> terms;
      for (auto &key : order) {
        T3 &g = groups[key];
        std::set<long> actS;
        if (g.wi >= 0) {
          auto act = activeOf(g.m, ws[g.wi], clip);
          actS.insert(act.begin(), act.end());
        }
        std::vector<uint8_t> must0(nA, 0);
        bool any0 = false;
        for (unsigned A = 0; A < nA; ++A) {
          bool inD = uOf[A] >= 0 && g.D.count(dg[K][A]);
          must0[A] = g.wi >= 0 ? (uOf[A] >= 0 && actS.count(uOf[A]) && !inD)
                               : !inD;
          any0 |= must0[A];
        }
        Sig s{Value(), false, 1};
        if (any0) {
          std::vector<uint8_t> ca(nA), cb(nA), cc(nA);
          for (unsigned A = 0; A < nA; ++A) {
            bool inD = uOf[A] >= 0 && g.D.count(dg[K][A]);
            ca[A] = inD;
            cb[A] = !must0[A];
            cc[A] = inD && (g.wi < 0 || actS.count(uOf[A]));
          }
          const std::vector<uint8_t> *pick = &ca;
          double best = 1e30;
          for (auto *c : {&ca, &cb, &cc}) {
            double cst = wsCache.count({*c, g.datum.neg}) ? 0.0 : selCostOf(*c);
            if (cst < best - 1e-9) {
              best = cst;
              pick = c;
            }
          }
          s = selOf(K, *pick, g.datum.neg);
          if (fail)
            return std::nullopt;
        } else
          ++nPass;
        if (s.k == 0)
          continue;
        terms.push_back({s, g.datum});
      }
      nTerms += terms.size();
      Sig o{Value(), false, 0};
      if (terms.size() == 1 && terms[0].first.k == 1)
        o = terms[0].second;
      else if (!terms.empty()) {
        auto r = andOrMerged(b, loc, top, terms, caps[K - 1]);
        if (!r)
          return std::nullopt;
        o = *r;
      }
      auto v = materialize(b, loc, top, o);
      if (!v)
        return std::nullopt;
      bits.push_back(*v);
    }
    if (verboseSel) {
      llvm::errs() << "[map-shift-select] window form: windows";
      for (auto &w : ws)
        llvm::errs() << " [" << w.first << "," << w.second << ")";
      unsigned nodes = 0;
      for (auto &[m, _] : cons)
        nodes += !plain[m] && !grpOf[m].empty();
      llvm::errs() << " cost=" << bestCost << " nodes=" << nodes
                   << " copies=" << nCopies << " single-copy nodes=" << nSingle
                   << " selects=" << nSel
                   << " last-level terms=" << nTerms << " pass-through=" << nPass
                   << "\n";
    }
    return bits;
  }

  //--- general window form -----------------------------------------------------
  //
  // A block is a value of floor(u / s), s = stride of the gated level KL: the
  // lower levels (shared, ungated) resolve u mod s. An output j needs, for an
  // amount in block b, the lower-level node V_{KL-1}(j' - b*s). A window type
  // t = (phase p, blocks [b0, b1]) gives node copies (m, t):
  //     N_{m,t} = OR_{b in [b0,b1]} (S_{t,r(b)}) & V_{KL-1}(m - r(b)*s),
  //     r(b) = (b - p) mod R_KL,  S_{t,r} = amount in a block of t with residue r
  // which is live on the blocks of t only, and serves output j for block b
  // when V_{KL-1}(m - r(b)*s) is the node that j needs for b. Types may
  // overlap and have different phases. Every
  // (output, block) requirement is covered by one copy (greedy cover: the copy
  // covering most open requirements first); an output then ORs its copies,
  // each under a select that is 1 on the blocks it takes from that copy and 0
  // on the copy's other live blocks where the copy carries another node.
  template <typename VFn, typename UseSelFn>
  std::optional<SmallVector<Value>> buildWindowGeneral(
      OpBuilder &b, Location loc, ModuleOp top, const RecipeIn &in, unsigned K,
      const std::vector<long> &stride, ArrayRef<unsigned> Rs,
      ArrayRef<unsigned> caps, const std::vector<long> &uOf, unsigned nA,
      unsigned nL, unsigned W, unsigned off, VFn &V, UseSelFn &useSel) {
    const unsigned KL = K - 1;
    const long S = stride[KL];
    const long R = Rs[KL - 1];
    std::map<long, unsigned> bw; // block -> legal assignments
    unsigned nLegal = 0;
    for (unsigned A = 0; A < nA; ++A)
      if (uOf[A] >= 0) {
        ++bw[uOf[A] / S];
        ++nLegal;
      }
    if (bw.empty())
      return std::nullopt;
    long bmin = bw.begin()->first, bmax = bw.rbegin()->first;
    auto lower = [&](long x) -> Sig {
      if (x < 0)
        return Sig{Value(), false, 0};
      return V(KL - 1, x, {});
    };
    auto rmod = [&](long b, long p) { return ((b - p) % R + R) % R; };
    struct Ty {
      long p, b0, b1;
    };
    // requirements: (output j, block b) -> lower-level node id
    struct Req {
      unsigned j;
      long jj, b;
      Sig need;
    };
    std::vector<Req> reqs;
    std::map<unsigned, Sig> plainOut; // complete: every block needs one datum
    for (unsigned j = 0; j < W; ++j) {
      long jj = (long)j - in.smin + (long)off;
      std::vector<Req> rj;
      for (auto &[bb, _] : bw) {
        Sig c = lower(jj - bb * S);
        if (c.k >= 0)
          continue;
        rj.push_back({j, jj, bb, c});
      }
      bool same = in.complete && rj.size() == bw.size();
      for (auto &r : rj)
        same &= idOf(r.need) == idOf(rj[0].need);
      if (same && !rj.empty()) {
        plainOut[j] = rj[0].need;
        continue;
      }
      reqs.insert(reqs.end(), rj.begin(), rj.end());
    }
    double A0 = windowCopyCost.getValue() / 100.0;
    // greedy cover for a type set; returns cost and the assignment
    auto cover = [&](const std::vector<Ty> &T, std::vector<int> *asg,
                     std::vector<std::pair<long, int>> *nodes) -> double {
      // candidate node of requirement i under type ti: m = jj - (b - r)*S
      std::map<std::pair<long, int>, std::vector<unsigned>> cand;
      for (unsigned i = 0; i < reqs.size(); ++i)
        for (unsigned ti = 0; ti < T.size(); ++ti) {
          const Ty &t = T[ti];
          if (reqs[i].b < t.b0 || reqs[i].b > t.b1)
            continue;
          long m = reqs[i].jj - (reqs[i].b - rmod(reqs[i].b, t.p)) * S;
          cand[{m, (int)ti}].push_back(i);
        }
      std::vector<int> who(reqs.size(), -1);
      std::vector<std::pair<long, int>> picked;
      unsigned open = reqs.size();
      std::vector<uint8_t> done(reqs.size(), 0);
      for (auto &x : who)
        (void)x;
      for (unsigned i = 0; i < reqs.size(); ++i) {
        bool any = false;
        for (unsigned ti = 0; ti < T.size() && !any; ++ti)
          any = reqs[i].b >= T[ti].b0 && reqs[i].b <= T[ti].b1;
        if (!any && windowDirect.getValue() == 0)
          return 1e30;
      }
      double cost = 0;
      unsigned terms = 0;
      double Ad = windowDirect.getValue() / 100.0;
      while (open) {
        const std::pair<long, int> *best = nullptr;
        unsigned bestN = 0;
        double bestLive = 0, bestRatio = 1e30;
        for (auto &[k, v] : cand) {
          unsigned n = 0;
          std::set<unsigned> o;
          for (unsigned i : v)
            if (!done[i])
              ++n, o.insert(reqs[i].j);
          if (!n)
            continue;
          const Ty &t = T[k.second];
          double live = 0;
          for (auto &[bb, w] : bw)
            if (bb >= t.b0 && bb <= t.b1)
              live += w;
          if (Ad > 0) {
            double ratio = (A0 + live / nLegal * (o.size() + 1)) / n;
            if (ratio < bestRatio - 1e-12) {
              best = &k;
              bestN = n;
              bestLive = live;
              bestRatio = ratio;
            }
          } else if (n > bestN || (n == bestN && live < bestLive)) {
            best = &k;
            bestN = n;
            bestLive = live;
          }
        }
        if (Ad > 0 && (!best || bestRatio > Ad)) {
          // the rest directly from the lower nodes (one shared node per x)
          std::map<long, int> dn;
          for (unsigned i = 0; i < reqs.size(); ++i)
            if (!done[i]) {
              long x = reqs[i].jj - reqs[i].b * S;
              if (!dn.count(x)) {
                dn[x] = picked.size();
                picked.push_back({x, -1});
              }
              done[i] = 1;
              who[i] = dn[x];
              --open;
              cost += Ad;
              ++terms;
            }
          break;
        }
        if (!best || !bestN)
          return 1e30;
        std::set<unsigned> outs;
        for (unsigned i : cand[*best])
          if (!done[i]) {
            done[i] = 1;
            who[i] = picked.size();
            outs.insert(reqs[i].j);
            --open;
          }
        cost += A0 + bestLive / nLegal * (outs.size() + 1);
        terms += outs.size();
        picked.push_back(*best);
      }
      cost += 0.5 * terms + 1.0 * T.size() * R;
      if (asg)
        *asg = who;
      if (nodes)
        *nodes = picked;
      return cost;
    };
    // window types: explicit, or a search
    std::vector<Ty> T;
    double bestCost = 1e30;
    if (!windowTypes.getValue().empty()) {
      std::vector<WTy> pt;
      std::string e;
      if (!parseTypes(windowTypes.getValue(), pt, e)) {
        windowNote = "window-types: " + e;
        return std::nullopt;
      }
      for (auto &t : pt) {
        if (t.p >= R || t.b0 < bmin || t.b1 > bmax) {
          windowNote = "window-types: '" + std::to_string(t.p) + ":" +
                       std::to_string(t.b0) + "-" + std::to_string(t.b1) +
                       "' is outside phase 0.." + std::to_string(R - 1) +
                       ", blocks " + std::to_string(bmin) + ".." +
                       std::to_string(bmax);
          return std::nullopt;
        }
        T.push_back({t.p, t.b0, t.b1});
      }
      bestCost = cover(T, nullptr, nullptr);
    } else {
      std::vector<Ty> all;
      for (long p = 0; p < R; ++p)
        for (long l = bmin; l <= bmax; ++l)
          for (long h = l; h <= bmax; ++h)
            all.push_back({p, l, h});
      unsigned mx = std::min(3u, std::max(1u, windowTypesMax.getValue()));
      std::vector<unsigned> idx;
      std::function<void(unsigned)> rec = [&](unsigned from) {
        if (!idx.empty()) {
          std::vector<Ty> cand;
          for (unsigned i : idx)
            cand.push_back(all[i]);
          double c = cover(cand, nullptr, nullptr);
          if (c < bestCost - 1e-9) {
            bestCost = c;
            T = cand;
          }
        }
        if (idx.size() == mx)
          return;
        for (unsigned i = from; i < all.size(); ++i) {
          idx.push_back(i);
          rec(i + 1);
          idx.pop_back();
        }
      };
      rec(0);
    }
    if (bestCost > 1e29) {
      if (!windowTypes.getValue().empty())
        windowNote = "window-types: the types do not cover every block need";
      return std::nullopt;
    }
    std::vector<int> asg;
    std::vector<std::pair<long, int>> nodes;
    cover(T, &asg, &nodes);
    // selects
    std::map<std::pair<std::vector<uint8_t>, bool>, Sig> wsCache;
    bool fail = false;
    unsigned nSel = 0;
    auto selOf = [&](unsigned lvl, const std::vector<uint8_t> &on,
                     bool wantNeg) -> Sig {
      auto ck = std::make_pair(on, wantNeg);
      std::vector<long> kv(on.begin(), on.end());
      auto key = std::make_tuple(3000 + lvl, kv, wantNeg, std::vector<long>());
      auto it = wsCache.find(ck);
      if (it != wsCache.end())
        return useSel(key, it->second);
      auto r = buildSelect(b, loc, top, on, nL, wantNeg);
      if (!r) {
        fail = true;
        return Sig{Value(), false, 0};
      }
      ++nSel;
      Sig sel = *r;
      if (sel.k < 0 && sel.neg != wantNeg) {
        auto v = invOf(b, loc, top, sel.v);
        if (!v) {
          fail = true;
          return Sig{Value(), false, 0};
        }
        annotateFanout(sel);
        sel = Sig{*v, wantNeg, -1};
      }
      annotateFanout(sel);
      wsCache[ck] = sel;
      return useSel(key, sel);
    };
    auto selCostOf = [&](const std::vector<uint8_t> &on) {
      TT f;
      for (unsigned i = 0; i < nL; ++i)
        f.vars.push_back(i);
      f.bits = on;
      f = normalizeTT(f);
      int cv;
      if (f.isConst(cv))
        return 0.0;
      return std::min(planOf(f).cost, planOf(notTT(f)).cost);
    };
    // the datum a copy carries for block bb
    auto copyDatum = [&](const std::pair<long, int> &n, long bb) -> Sig {
      if (n.second < 0)
        return bw.count(bb) ? lower(n.first) : Sig{Value(), false, 0};
      const Ty &t = T[n.second];
      if (bb < t.b0 || bb > t.b1 || !bw.count(bb))
        return Sig{Value(), false, 0};
      return lower(n.first - rmod(bb, t.p) * S);
    };
    std::map<unsigned, Sig> copySig;
    std::set<unsigned> plainCopies;
    auto copyOf = [&](unsigned ni) -> Sig {
      auto it = copySig.find(ni);
      if (it != copySig.end())
        return it->second;
      const auto &n = nodes[ni];
      if (n.second < 0) { // a lower node used directly: live everywhere
        plainCopies.insert(ni);
        return copySig[ni] = lower(n.first);
      }
      std::map<SigId, std::pair<Sig, std::set<long>>> g;
      std::vector<SigId> order;
      for (auto &[bb, _] : bw) {
        Sig c = copyDatum(n, bb);
        if (c.k >= 0)
          continue;
        auto id = idOf(c);
        if (!g.count(id))
          order.push_back(id);
        g[id].first = c;
        g[id].second.insert(bb);
      }
      const Ty &ty = T[n.second];
      if (in.complete && order.size() == 1 && ty.b0 <= bmin && ty.b1 >= bmax &&
          g[order[0]].second.size() == bw.size())
      {
        plainCopies.insert(ni);
        return copySig[ni] = g[order[0]].first; // live everywhere, no cell
      }
      SmallVector<std::pair<Sig, Sig>> terms;
      for (auto &id : order) {
        auto &[d, B] = g[id];
        std::vector<uint8_t> on(nA, 0);
        for (unsigned A = 0; A < nA; ++A)
          on[A] = uOf[A] >= 0 && B.count(uOf[A] / S);
        Sig sl = selOf(KL, on, d.neg);
        if (fail)
          return Sig{Value(), false, 0};
        if (sl.k == 0)
          continue;
        terms.push_back({sl, d});
      }
      Sig r{Value(), false, 0};
      if (!terms.empty()) {
        auto o = andOrMerged(b, loc, top, terms, caps[KL - 1]);
        if (!o) {
          fail = true;
          return r;
        }
        r = *o;
      }
      return copySig[ni] = r;
    };
    // outputs
    std::vector<std::vector<unsigned>> reqOf(W);
    for (unsigned i = 0; i < reqs.size(); ++i)
      reqOf[reqs[i].j].push_back(i);
    unsigned nTerms = 0, nPass = 0;
    std::set<unsigned> usedCopies;
    SmallVector<Value> bits;
    for (unsigned j = 0; j < W; ++j) {
      if (plainOut.count(j)) {
        auto v = materialize(b, loc, top, plainOut[j]);
        if (!v)
          return std::nullopt;
        bits.push_back(*v);
        continue;
      }
      std::map<int, std::set<long>> take; // copy -> blocks taken
      for (unsigned i : reqOf[j])
        take[asg[i]].insert(reqs[i].b);
      SmallVector<std::pair<Sig, Sig>> terms;
      // copies that are the same signal (plain copies) form one term
      std::map<SigId, std::tuple<Sig, std::set<long>, std::vector<unsigned>>> grp;
      std::vector<SigId> gorder;
      for (auto &[ni, B] : take) {
        Sig c = copyOf(ni);
        if (fail)
          return std::nullopt;
        if (c.k >= 0)
          continue;
        usedCopies.insert(ni);
        auto id = idOf(c);
        if (!grp.count(id))
          gorder.push_back(id);
        auto &gg = grp[id];
        std::get<0>(gg) = c;
        std::get<1>(gg).insert(B.begin(), B.end());
        std::get<2>(gg).push_back(ni);
      }
      for (auto &gid : gorder) {
        auto &[c, B, members] = grp[gid];
        unsigned ni = members[0];
        // blocks where a member is live with another node than j needs
        std::set<long> bad;
        for (auto &[bb, _] : bw) {
          if (B.count(bb))
            continue;
          long jj = (long)j - in.smin + (long)off;
          Sig need = lower(jj - bb * S);
          for (unsigned mi : members) {
            Sig cd = plainCopies.count(mi) ? c : copyDatum(nodes[mi], bb);
            if (cd.k >= 0)
              continue;
            if (need.k < 0 && idOf(need) == idOf(cd))
              continue; // carries what j needs there too
            bad.insert(bb);
          }
        }
        Sig sl{Value(), false, 1};
        if (!bad.empty()) {
          std::vector<uint8_t> ca(nA), cb(nA), cc(nA);
          const Ty t = nodes[ni].second >= 0 ? T[nodes[ni].second] : Ty{0, LONG_MIN, LONG_MAX};
          for (unsigned A = 0; A < nA; ++A) {
            long bb = uOf[A] >= 0 ? uOf[A] / S : LONG_MIN;
            bool inB = uOf[A] >= 0 && B.count(bb);
            ca[A] = inB;
            cb[A] = !(uOf[A] >= 0 && bad.count(bb));
            cc[A] = inB && (plainCopies.count(ni) || (bb >= t.b0 && bb <= t.b1));
          }
          const std::vector<uint8_t> *pick = &ca;
          double best = 1e30;
          for (auto *cnd : {&ca, &cb, &cc}) {
            double cst = wsCache.count({*cnd, c.neg}) ? 0.0 : selCostOf(*cnd);
            if (cst < best - 1e-9) {
              best = cst;
              pick = cnd;
            }
          }
          sl = selOf(K, *pick, c.neg);
          if (fail)
            return std::nullopt;
        } else
          ++nPass;
        if (sl.k == 0)
          continue;
        terms.push_back({sl, c});
      }
      // equal copies (shared Sigs) merge their selects through andOrMerged's
      // datum grouping only when the selects agree; keep them as terms
      nTerms += terms.size();
      Sig o{Value(), false, 0};
      SmallVector<std::pair<Sig, Sig>> tp, tn;
      for (auto &t : terms)
        (t.second.neg ? tn : tp).push_back(t);
      if (terms.size() == 1 && terms[0].first.k == 1)
        o = terms[0].second;
      else if (!tp.empty() && !tn.empty()) {
        // data of both polarities (copies and direct lower nodes): one
        // AND-OR per polarity, then their OR
        auto rp = andOrMerged(b, loc, top, tp, caps[K - 1]);
        auto rn = andOrMerged(b, loc, top, tn, caps[K - 1]);
        if (!rp || !rn)
          return std::nullopt;
        auto r = orReduce(b, loc, top, {*rp, *rn});
        if (!r)
          return std::nullopt;
        o = *r;
      } else if (!terms.empty()) {
        auto r = andOrMerged(b, loc, top, terms, caps[K - 1]);
        if (!r)
          return std::nullopt;
        o = *r;
      }
      auto v = materialize(b, loc, top, o);
      if (!v)
        return std::nullopt;
      bits.push_back(*v);
    }
    if (verboseSel) {
      std::set<long> pos;
      std::set<SigId> cells;
      for (unsigned ni : usedCopies)
        if (!plainCopies.count(ni)) {
          pos.insert(nodes[ni].first);
          cells.insert(idOf(copySig[ni]));
        }
      llvm::errs() << "[map-shift-select] window form (general): types";
      for (auto &t : T)
        llvm::errs() << " " << t.p << ":" << t.b0 << "-" << t.b1;
      llvm::errs() << " cost=" << bestCost << " copies=" << usedCopies.size()
                   << " copy-cells=" << cells.size() << " positions=" << pos.size() << " requirements="
                   << reqs.size() << " selects=" << nSel
                   << " last-level terms=" << nTerms << " pass-through=" << nPass
                   << "\n";
    }
    return bits;
  }

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
    std::vector<long> uOf; // window form: the shift (from the smallest) of an assignment
    unsigned nA = 0, nL = 0;
    if (in.dec) {
      const DecodeIn &d = *in.dec;
      nL = d.leaves.size();
      nA = 1u << nL;
      dg.assign(K + 1, std::vector<long>(nA, -1));
      uOf.assign(nA, -1);
      for (unsigned A = 0; A < nA; ++A) {
        long sh = d.shiftOf[A];
        if (d.isDefault[A] && defaultDontCare.getValue() && d.affine)
          sh = d.contOf[A];
        if (sh == LONG_MIN)
          continue;
        long u = sh - in.smin + off;
        if (u < 0)
          return std::nullopt;
        uOf[A] = u;
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
    using SelKey =
        std::tuple<unsigned, std::vector<long>, bool, std::vector<long>>;
    std::map<SelKey, Sig> selCache;
    // select-dup-fanout: the copy of a select gate in use and its loads
    std::map<SelKey, std::pair<Sig, unsigned>> selCur;
    bool selFail = false;
    auto useSel = [&](const SelKey &key, Sig base) -> Sig {
      unsigned cap = selectDupFanout.getValue();
      if (!in.dec || !cap || base.k >= 0 || !base.v ||
          !isa_and_nonnull<hw::InstanceOp>(base.v.getDefiningOp()))
        return base;
      auto &u = selCur[key];
      if (!u.first.v && u.first.k < 0) {
        u.first = base;
        u.second = 0;
      }
      if (u.second >= cap) {
        Operation *c = b.clone(*base.v.getDefiningOp());
        c->removeAttr(kIdAttr);
        c->setAttr("instanceName",
                   StringAttr::get(top.getContext(),
                                   (Twine("shs_") + Twine(nCells++)).str()));
        created.push_back(c);
        u.first = Sig{c->getResult(0), base.neg, -1};
        u.second = 0;
      }
      ++u.second;
      return u.first;
    };
    std::function<Sig(unsigned, const std::set<long> &, bool,
                      const std::vector<long> &)>
        selFor = [&](unsigned k, const std::set<long> &D, bool wantNeg,
                     const std::vector<long> &R) -> Sig {
      auto key = std::make_tuple(k, std::vector<long>(D.begin(), D.end()),
                                 wantNeg, R);
      auto it = selCache.find(key);
      if (it != selCache.end())
        return useSel(key, it->second);
      if (in.dec && !R.empty() && l2RangeCompose.getValue()) {
        // select = digit select AND range select, each shared with the other
        // selects of the same digit set / range (one AND cell per pair)
        std::set<long> Rs(R.begin(), R.end());
        std::optional<Sig> best;
        for (int pol = 0; pol < 2 && !(best && best->neg == wantNeg); ++pol) {
          Sig x = selFor(k, D, pol, {});
          Sig y = selFor(K, Rs, pol, {});
          if (selFail)
            return selCache[key] = Sig{Value(), false, 0};
          if (auto g = gate(b, loc, top, and2(), {x, y}))
            if (!best || g->neg == wantNeg)
              best = *g;
        }
        if (best) {
          annotateFanout(*best);
          selCache[key] = *best;
          return useSel(key, *best);
        }
      }
      if (in.dec) {
        std::vector<uint8_t> on(nA, 0);
        for (unsigned A = 0; A < nA; ++A) {
          on[A] = dg[k][A] >= 0 && D.count(dg[k][A]);
          if (on[A] && !R.empty())
            on[A] = dg[K][A] >= 0 &&
                    std::binary_search(R.begin(), R.end(), dg[K][A]);
        }
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
        selCache[key] = *r;
        return useSel(key, *r);
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
    std::map<std::tuple<unsigned, long, std::vector<long>>, Sig> memo;
    // l2-range-gate: the level below the last one is gated by the range of
    // last-digit values that consume the node.
    unsigned gateW = in.dec && K >= 2 ? l2RangeGate.getValue() : 0;
    auto consumers = [&](long m2) {
      std::vector<long> r;
      for (auto &[d, _] : selArms[K]) {
        long j = m2 + d * stride[K] + in.smin - (long)off;
        if (j >= 0 && j < (long)W)
          r.push_back(d);
      }
      return r;
    };
    auto rangeOf = [&](long m2, long d) -> std::vector<long> {
      std::vector<long> r;
      if (l2RangeAligned.getValue()) {
        for (auto &[x, _] : selArms[K])
          if (x / (long)gateW == d / (long)gateW)
            r.push_back(x);
      } else
        for (long x : consumers(m2))
          if (x / (long)gateW == d / (long)gateW)
            r.push_back(x);
      if (r.size() >= selArms[K].size())
        r.clear(); // every digit consumes it: nothing to gate
      return r;
    };
    std::function<Sig(unsigned, long, const std::vector<long> &)> V =
        [&](unsigned k, long m, const std::vector<long> &R) -> Sig {
      if (!ok)
        return Sig{Value(), false, 0};
      if (k == 0)
        return data0(m);
      auto mk = std::make_tuple(k, m, R);
      auto it = memo.find(mk);
      if (it != memo.end())
        return it->second;
      std::map<SigId, std::pair<Sig, std::set<long>>> groups;
      std::map<SigId, bool> single; // last-level term whose node is range-exact
      for (auto &[d, _] : selArms[k]) {
        long m2 = m - d * stride[k];
        if (m2 < 0)
          continue;
        std::vector<long> R2;
        if (gateW && k == K)
          R2 = rangeOf(m2, d);
        Sig c = V(k - 1, m2, R2);
        if (!ok)
          return Sig{Value(), false, 0};
        if (c.k >= 0)
          continue; // constant 0 contributes nothing
        auto &g = groups[idOf(c)];
        g.first = c;
        g.second.insert(d);
        if (gateW && k == K)
          single[idOf(c)] = R2.size() == 1;
      }
      Sig r{Value(), false, 0};
      if (groups.size() == 1 && in.complete && R.empty() &&
          groups.begin()->second.second.size() == selArms[k].size()) {
        r = groups.begin()->second.first; // every digit selects the same datum
      } else if (!groups.empty()) {
        SmallVector<std::pair<Sig, Sig>> terms;
        for (auto &[id, g] : groups) {
          if (gateW && k == K && g.second.size() == 1 && single[id]) {
            terms.push_back({Sig{Value(), false, 1}, g.first});
            continue;
          }
          terms.push_back(
              {selFor(k, g.second, g.first.neg,
                      gateW && k + 1 == K ? R : std::vector<long>()),
               g.first});
        }
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
    if (in.dec && K >= 2 && windowForm.getValue() &&
        windowGeneral.getValue()) {
      auto r = buildWindowGeneral(b, loc, top, in, K, stride, Rs, caps, uOf,
                                  nA, nL, W, off, V, useSel);
      if (!ok)
        return std::nullopt;
      return r;
    }
    if (in.dec && K >= 2 && windowForm.getValue()) {
      auto r = buildWindowForm(b, loc, top, in, K, stride, caps, dg, uOf,
                               selArms, nA, nL, W, off, V, useSel);
      if (!ok)
        return std::nullopt;
      return r;
    }
    SmallVector<Value> bits;
    for (unsigned j = 0; j < W && ok; ++j) {
      Sig o = V(K, (long)j - in.smin + (long)off, {});
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

  // Window-form option parsers (also checked in the Python option check).
  static constexpr size_t kMaxWindowCuts = 12;
  static bool asciiDigits(StringRef t) {
    if (t.empty() || t.size() > 9)
      return false;
    return llvm::all_of(t, [](char c) { return c >= '0' && c <= '9'; });
  }
  // "c1,c2,..." distinct ASCII integers (sorted on return), at most kMaxWindowCuts.
  static bool parseCuts(StringRef s, std::vector<long> &out, std::string &err) {
    SmallVector<StringRef> parts;
    s.split(parts, ',', -1, false);
    if (parts.empty()) {
      err = "no cut";
      return false;
    }
    if (parts.size() > kMaxWindowCuts) {
      err = "more than " + std::to_string(kMaxWindowCuts) + " cuts";
      return false;
    }
    for (StringRef p : parts) {
      p = p.trim();
      if (!asciiDigits(p)) {
        err = "'" + p.str() + "' is not an ASCII integer";
        return false;
      }
      long v = std::stol(p.str());
      if (llvm::is_contained(out, v)) {
        err = "duplicate cut " + p.str();
        return false;
      }
      out.push_back(v);
    }
    std::sort(out.begin(), out.end());
    return true;
  }
  struct WTy {
    long p, b0, b1;
  };
  // "phase:first-last;..." ASCII integers, first <= last.
  static bool parseTypes(StringRef s, std::vector<WTy> &out, std::string &err) {
    SmallVector<StringRef> parts;
    s.split(parts, ';', -1, false);
    if (parts.empty()) {
      err = "no window type";
      return false;
    }
    for (StringRef pt : parts) {
      auto [ph, rg] = pt.trim().split(':');
      auto [lo, hi] = rg.split('-');
      ph = ph.trim();
      lo = lo.trim();
      hi = hi.trim();
      if (!asciiDigits(ph) || !asciiDigits(lo) || !asciiDigits(hi)) {
        err = "'" + pt.str() + "' is not phase:first-last (ASCII integers)";
        return false;
      }
      WTy t{std::stol(ph.str()), std::stol(lo.str()), std::stol(hi.str())};
      if (t.b0 > t.b1) {
        err = "'" + pt.str() + "': first > last";
        return false;
      }
      out.push_back(t);
    }
    return true;
  }
  // Set when a requested window form could not be applied for its
  // parameters; the pass then fails (applied or refused).
  std::string windowErr, windowNote;

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
    windowNote.clear();
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
    if (bestIdx < 0) {
      if (windowForm.getValue() && !windowNote.empty() && windowErr.empty())
        windowErr = windowNote;
      return std::nullopt;
    }
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
    if (windowForm.getValue()) {
      std::string e;
      std::vector<long> cv;
      std::vector<WTy> tv;
      if (!windowCuts.getValue().empty() &&
          !parseCuts(windowCuts.getValue(), cv, e)) {
        top.emitError("map-shift-select: window-cuts: ") << e;
        return signalPassFailure();
      }
      if (!windowTypes.getValue().empty() &&
          !parseTypes(windowTypes.getValue(), tv, e)) {
        top.emitError("map-shift-select: window-types: ") << e;
        return signalPassFailure();
      }
    }
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
    if (!windowErr.empty()) {
      top.emitError("map-shift-select: window form requested but not "
                    "applied: ")
          << windowErr;
      return signalPassFailure();
    }
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
