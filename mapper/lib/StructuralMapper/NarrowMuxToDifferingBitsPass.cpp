//===- NarrowMuxToDifferingBitsPass.cpp -----------------------------------===//
//
// Narrow a `comb.mux` to the bits where its two arms actually differ.
//
// WHY THIS IS WORTH A PASS.  `convert-comb-to-synth` keeps `synth.aig.and_inv`
// at the WORD level: a `comb.mux` of width W lowers to four W-bit `and_inv`
// ops, i.e. 4*W AIG nodes when `exportAIGER` bit-blasts it. That is correct
// and cheap when the arms are unrelated. It is catastrophic for the
// read-modify-write shape a scoreboard write produces, which is a chain of
//
//     %n    = comb.concat extract(%v, p+1, W-p-1), %true, extract(%v, 0, p)
//     %next = comb.mux %en, %n, %v : iW
//
// -- one bit set under a condition, expressed as a W-bit mux. On cv32a60x's
// scoreboard W is 1087 and the chain is 22 muxes deep, so a handful of
// single-bit updates cost ~72,000 AIG nodes. The AIGER exporter's structural
// hashing cannot recover it: for a bit where both arms are the SAME literal
// `x`, the mux still builds AND(c,x), AND(!c,x) and their OR -- three nodes
// that are only provably equal to `x` by Boolean reasoning, which is ABC's
// job and which ABC then spends its whole per-group budget on. Measured on
// cv32a60x: group g30 (the scoreboard) exports a 68,254-AND AIGER, 24% of the
// design's entire 282,487-node pre-optimisation total.
//
// WHAT IT DOES.  For `mux(c, a, b)` where either arm is a `comb.concat`, cut
// BOTH arms at that concat's boundaries and rebuild:
//
//     concat( mux(c, a[i], b[i]) for each slice i )
//
// `comb.extract` of a `comb.concat` folds to the concat operand, so the
// a-side slices cost nothing, and wherever a slice of `a` turns out to BE the
// matching slice of `b` the mux disappears entirely. Only applied when at
// least one slice folds away -- otherwise the rewrite is a wash (the same
// total mux width, just more ops) and is skipped.
//
// Function-preserving by construction: every output bit is either the same
// mux it was, or a value both arms already agreed on.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/Debug.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {

namespace {

struct NarrowMuxToDifferingBitsPass
    : public PassWrapper<NarrowMuxToDifferingBitsPass,
                         OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowMuxToDifferingBitsPass)

  NarrowMuxToDifferingBitsPass() = default;
  NarrowMuxToDifferingBitsPass(const NarrowMuxToDifferingBitsPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    minWidth = other.minWidth.getValue();
    maxWidth = other.maxWidth.getValue();
    maxRuns = other.maxRuns.getValue();
  }

  Option<bool> enable{
      *this, "enable-narrow-mux", llvm::cl::init(false),
      llvm::cl::desc("Narrow a comb.mux to the bits where its arms differ "
                     "(OFF by default)")};
  // Below this width the rewrite cannot pay for the extra ops it creates.
  Option<unsigned> minWidth{
      *this, "min-width", llvm::cl::init(8),
      llvm::cl::desc("Only rewrite muxes at least this wide (default 8)")};
  // Bit-by-bit provenance is O(width); a pathologically wide mux would cost
  // more to analyse than it can save.
  Option<unsigned> maxWidth{
      *this, "max-width", llvm::cl::init(65536),
      llvm::cl::desc("Skip muxes wider than this (default 65536)")};
  // A mux whose arms agree in many scattered places fragments into that many
  // ops; past this the op count costs more than the nodes saved.
  Option<unsigned> maxRuns{
      *this, "max-runs", llvm::cl::init(16),
      llvm::cl::desc("Skip if the agree/differ pattern has more runs than "
                     "this (default 16)")};

  StringRef getArgument() const final { return "narrow-mux-to-differing-bits"; }
  StringRef getDescription() const final {
    return "Rewrite a wide comb.mux whose arms agree on most bits into a "
           "concat of narrow muxes over the bits that actually differ";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<comb::CombDialect, hw::HWDialect>();
  }

  // Slice [offset, offset+width) of `v`, folding through a comb.concat so a
  // slice that lines up with one of its operands returns that operand
  // unchanged (that is what makes the a-side free and the equality check
  // below meaningful).
  static Value slice(OpBuilder &b, Location loc, Value v, unsigned offset,
                     unsigned width) {
    unsigned total = v.getType().getIntOrFloatBitWidth();
    if (offset == 0 && width == total)
      return v;
    if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
      // comb.concat puts its FIRST operand at the MSB.
      unsigned pos = total;
      for (Value operand : concat.getInputs()) {
        unsigned ow = operand.getType().getIntOrFloatBitWidth();
        pos -= ow;
        if (pos == offset && ow == width)
          return operand;
        if (pos <= offset && offset + width <= pos + ow)
          return slice(b, loc, operand, offset - pos, width);
      }
    }
    return comb::ExtractOp::create(b, loc, b.getIntegerType(width), v, offset);
  }

  // The slice of `v` at [offset, offset+width) IF it is already materialised
  // as one of a concat's operands; null otherwise. Builds nothing, so it is
  // safe to call before deciding whether to rewrite.
  static Value sliceOfConcat(Value v, unsigned offset, unsigned width) {
    auto concat = v.getDefiningOp<comb::ConcatOp>();
    if (!concat)
      return {};
    unsigned pos = v.getType().getIntOrFloatBitWidth();
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      if (pos == offset && ow == width)
        return operand;
    }
    return {};
  }

  // The concat boundaries to cut at, as (offset, width) from the LSB up.
  static bool boundaries(Value v, SmallVectorImpl<std::pair<unsigned, unsigned>> &out) {
    auto concat = v.getDefiningOp<comb::ConcatOp>();
    if (!concat)
      return false;
    unsigned pos = v.getType().getIntOrFloatBitWidth();
    SmallVector<std::pair<unsigned, unsigned>> rev;
    for (Value operand : concat.getInputs()) {
      unsigned ow = operand.getType().getIntOrFloatBitWidth();
      pos -= ow;
      rev.push_back({pos, ow});
    }
    out.assign(llvm::reverse(rev).begin(), llvm::reverse(rev).end());
    return out.size() > 1;
  }

  // Where does bit `bit` of `v` actually come from? Walks down through
  // `comb.concat` and `comb.extract` to the first value that is neither, and
  // returns (that value, the bit index in it). Two slices of two DIFFERENT
  // values agree exactly when their bits resolve to the same place -- which
  // a structural comparison of the arms cannot see once a chain's second
  // link slices the first link's rebuilt concat.
  static std::pair<Value, unsigned> provenance(Value v, unsigned bit,
                                               unsigned fuel = 64) {
    if (!fuel)
      return {v, bit};
    if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
      // comb.concat puts its FIRST operand at the MSB.
      unsigned pos = v.getType().getIntOrFloatBitWidth();
      for (Value operand : concat.getInputs()) {
        unsigned ow = operand.getType().getIntOrFloatBitWidth();
        pos -= ow;
        if (bit >= pos && bit < pos + ow)
          return provenance(operand, bit - pos, fuel - 1);
      }
      return {v, bit};
    }
    if (auto ex = v.getDefiningOp<comb::ExtractOp>())
      return provenance(ex.getInput(), bit + ex.getLowBit(), fuel - 1);
    if (auto rep = v.getDefiningOp<comb::ReplicateOp>()) {
      unsigned iw = rep.getInput().getType().getIntOrFloatBitWidth();
      return provenance(rep.getInput(), bit % iw, fuel - 1);
    }
    return {v, bit};
  }

  // Same bit, or the same constant bit value under two different (already
  // CSEd, but be safe) constant ops.
  static bool sameBit(Value a, unsigned ab, Value b, unsigned bb) {
    auto [ar, ai] = provenance(a, ab);
    auto [br, bi] = provenance(b, bb);
    if (ar == br && ai == bi)
      return true;
    auto ac = ar.getDefiningOp<hw::ConstantOp>();
    auto bc = br.getDefiningOp<hw::ConstantOp>();
    if (ac && bc)
      return ac.getValue()[ai] == bc.getValue()[bi];
    return false;
  }

  // Slice [offset, offset+width) of `v`, reusing a concat operand where the
  // cut lines up with one so the common case builds nothing.
  static Value sliceValue(OpBuilder &b, Location loc, Value v, unsigned offset,
                          unsigned width) {
    unsigned total = v.getType().getIntOrFloatBitWidth();
    if (offset == 0 && width == total)
      return v;
    if (auto concat = v.getDefiningOp<comb::ConcatOp>()) {
      unsigned pos = total;
      for (Value operand : concat.getInputs()) {
        unsigned ow = operand.getType().getIntOrFloatBitWidth();
        pos -= ow;
        if (pos <= offset && offset + width <= pos + ow)
          return sliceValue(b, loc, operand, offset - pos, width);
      }
    }
    return comb::ExtractOp::create(b, loc, b.getIntegerType(width), v, offset);
  }

  bool rewriteOne(comb::MuxOp mux) {
    auto intTy = dyn_cast<IntegerType>(mux.getType());
    if (!intTy || intTy.getWidth() < minWidth || intTy.getWidth() > maxWidth)
      return false;
    unsigned W = intTy.getWidth();
    Value tArm = mux.getTrueValue(), fArm = mux.getFalseValue();

    // Runs of bits (LSB first) that agree / differ.
    SmallVector<std::pair<unsigned, unsigned>> runs; // (start, width)
    SmallVector<bool> runAgrees;
    bool cur = sameBit(tArm, 0, fArm, 0);
    unsigned start = 0, agreeing = cur ? 1 : 0;
    for (unsigned i = 1; i < W; ++i) {
      bool s = sameBit(tArm, i, fArm, i);
      if (s)
        ++agreeing;
      if (s != cur) {
        runs.push_back({start, i - start});
        runAgrees.push_back(cur);
        cur = s;
        start = i;
      }
      // Fragmenting into many pieces costs more ops than it saves nodes.
      if (runs.size() > maxRuns)
        return false;
    }
    runs.push_back({start, W - start});
    runAgrees.push_back(cur);

    if (!agreeing)
      return false; // nothing to win
    if (runs.size() == 1)
      return false; // the WHOLE mux is redundant; canonicalize owns that case

    OpBuilder b(mux);
    Location loc = mux.getLoc();
    SmallVector<Value> parts; // LSB first
    for (auto [i, run] : llvm::enumerate(runs)) {
      auto [off, w] = run;
      Value tv = sliceValue(b, loc, tArm, off, w);
      if (runAgrees[i]) {
        parts.push_back(tv);
        continue;
      }
      parts.push_back(
          comb::MuxOp::create(b, loc, mux.getCond(), tv,
                              sliceValue(b, loc, fArm, off, w)));
    }
    std::reverse(parts.begin(), parts.end()); // comb.concat is MSB first
    Value repl = comb::ConcatOp::create(b, loc, parts);
    mux.getResult().replaceAllUsesWith(repl);
    mux.erase();
    numBitsSaved += agreeing;
    return true;
  }

  unsigned numBitsSaved = 0;

  void runOnOperation() override {
    if (!enable)
      return;
    unsigned rewritten = 0;
    // Walk to a fixed point: narrowing one mux in a chain exposes the next,
    // because the chain's next link slices THIS mux's result.
    bool changed = true;
    while (changed) {
      changed = false;
      SmallVector<comb::MuxOp> worklist;
      getOperation().walk([&](comb::MuxOp m) { worklist.push_back(m); });
      for (comb::MuxOp m : worklist) {
        if (!m.getOperation()->getBlock())
          continue; // already erased
        if (rewriteOne(m)) {
          ++rewritten;
          changed = true;
        }
      }
    }
    if (rewritten)
      llvm::errs() << "[narrow-mux-to-differing-bits] narrowed " << rewritten
                   << " mux(es), " << numBitsSaved
                   << " slice(s) of agreeing bits removed\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowMuxToDifferingBitsPass() {
  return std::make_unique<NarrowMuxToDifferingBitsPass>();
}

} // namespace circt_abc
