//===- NarrowConstantRegisterBitsPass.cpp ------------------*- C++ -*-===//
//
// `--narrow-constant-register-bits`: per-BIT register constant propagation
// that can see across single-instance module boundaries.
//
// Why a second pass next to `propagate-register-constants`: that pass is
// whole-register ("is the entire value always its reset constant") and
// per-hw.module ("a block argument or an hw.instance result is
// unresolved"), and on ibex it is a verified no-op for exactly those two
// reasons (its own file comment). The CSR reserved/read-only bits that
// direction 2 targets (2026-09-02 register census: dcsr 32 vs 9 flops in
// Yosys, mtvec 32 vs 25, cpuctrlsts 8 vs 2, mstatus 6 vs 3 -- ~41 flops)
// are (a) a SUBSET of a register's bits, and (b) only provably constant
// because the CALLER (ibex_cs_registers) drives the CSR leaf module's
// write-data port with a concat of literal constants and the leaf's own
// read-back, while the leaf's register does `wr_en ? wr_data : q`.
//
// Analysis: a known-bits lattice per value (known mask + value), evaluated
// through the same op set the whole-register pass understands
// (constant/extract/concat/mux/and/or/xor, plus replicate), with two
// additions:
//   * a block argument of a module that has EXACTLY ONE hw.instance in the
//     design resolves to that instance's operand in the parent, and an
//     hw.instance result of such a module resolves to the callee's
//     hw.output operand -- i.e. the evaluator walks through a unique
//     instantiation in both directions (the parent's hold path feeds the
//     leaf's own output back into its input). Modules with several
//     instances stay opaque (no guessing); the top module has none.
//   * the hypothesis per register is per bit: every reset-literal register
//     starts with all bits hypothesized equal to its reset value, and a
//     greatest-fixed-point iteration (Jacobi rounds over the whole design)
//     drops every bit whose `next` is not provably that reset bit under the
//     current hypotheses. Monotone decreasing, so it terminates; nothing
//     is ever assumed that a later round could refute without dropping it.
//
// Rewrite: a register whose bits are ALL constant is replaced by the
// literal (as before); a register with SOME constant bits is narrowed to
// its variable bits (the NarrowDeadRegisterBits mechanics: compacted
// next/reset, a fresh firreg named <name>_cbits, uses reconstructed as a
// concat of the constant bits and extracts of the narrowed register).
// Port widths never change, so the parent's logic is untouched here and
// folds through canonicalize afterwards.
//
// Scope, as in the sibling passes: plain integer-typed firregs with a
// reset and a literal reset value; aggregate-typed or reset-less firregs
// are never touched.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct KnownBits {
  APInt known; // 1 = this bit's value is proven
  APInt value; // meaningful where known
  static KnownBits unknown(unsigned w) {
    return {APInt::getZero(w), APInt::getZero(w)};
  }
  static KnownBits constant(const APInt &c) {
    return {APInt::getAllOnes(c.getBitWidth()), c};
  }
  bool allKnown() const { return known.isAllOnes(); }
};

struct NarrowConstantRegisterBitsPass
    : public PassWrapper<NarrowConstantRegisterBitsPass,
                         OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowConstantRegisterBitsPass)

  NarrowConstantRegisterBitsPass() = default;
  NarrowConstantRegisterBitsPass(const NarrowConstantRegisterBitsPass &other)
      : PassWrapper(other) {
    enable = other.enable.getValue();
    multiInstance = other.multiInstance.getValue();
    aggregates = other.aggregates.getValue();
    foldUnresetHolds = other.foldUnresetHolds.getValue();
  }

  StringRef getArgument() const final { return "narrow-constant-register-bits"; }
  StringRef getDescription() const final {
    return "Per-bit register constant propagation across single-instance "
           "module boundaries: bits of a reset-literal seq.firreg whose "
           "every update provably keeps the reset value are replaced by "
           "the literal and the register is narrowed to its variable bits";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect>();
  }

  Option<bool> enable{
      *this, "enable-narrow-constant-register-bits",
      llvm::cl::desc("Enable the pass (ON by default)"), llvm::cl::init(true)};

  // OFF by default, project convention for a new lever. See the
  // "multi-instance" comment block below for what it changes and why it is
  // sound; measured headroom on cv32a60x is ~1,156 register bits.
  // OFF by default. Aggregate values (structs/arrays) are opaque to the
  // integer lattice, so a constant that crosses a module boundary INSIDE a
  // struct is invisible. That is not hypothetical: CVA6's `instr_queue`
  // drives its FIFOs with
  //   struct_create(instr, cf, ex, ex_vaddr, c0_i34, c0_i32, false)
  // -- 67 of 136 bits per entry are literal zeros (the RISC-V hypervisor
  // fields, absent from cv32a60x) -- and 536 flops store them because the
  // port is struct-typed and the write is a dynamic-index array_inject.
  // `--hw-aggregate-to-comb` does not help: it de-aggregates values INSIDE a
  // module and leaves port types alone. CIRCT's `--hw-flatten-io` would, but
  // it only handles hw::StructType and cannot legalize CVA6's
  // array-of-struct ports.
  Option<bool> aggregates{
      *this, "cross-aggregates",
      llvm::cl::desc("Evaluate aggregate (struct/array) values as packed "
                     "known-bits, so constants crossing an aggregate-typed "
                     "port or sitting in an array register are visible "
                     "(OFF by default)"),
      llvm::cl::init(false)};

  // OFF by default, and unlike every other option here it rests on an
  // ASSUMPTION rather than a proof: that an unreset register's power-up value
  // is a don't-care. A register with no reset whose `next` provably equals its
  // own `data` never changes, so its value is whatever it powered up as;
  // picking a constant is legal only under X-optimism. That is the standard
  // synthesis convention and what commercial synthesis does ("replacing a
  // flip-flop with a logic constant"), but it is NOT equivalence-preserving
  // against a simulator that models X, so it stays opt-in.
  //
  // It matters because one such register gates a whole block: CVA6's
  // `hwpf_stride.csr_throttle_q` is
  //   mux(csr_throttle_set_i & rst_ni, <new>, csr_throttle_q)
  // and `csr_throttle_set_i` is driven by a literal `c0_i4` three levels up
  // at `cva6_hpdcache_subsystem`, so it is a pure self-hold. Being
  // unhypothesizable it reads as UNKNOWN and poisons the prefetcher's FSM
  // and counters -- 595 flops that commercial synthesis deletes outright.
  Option<bool> foldUnresetHolds{
      *this, "fold-unreset-holds",
      llvm::cl::desc("Also hypothesise reset-less registers as zero, so a "
                     "register that provably only holds is replaced by a "
                     "constant (X-optimistic; OFF by default)"),
      llvm::cl::init(false)};

  Option<bool> multiInstance{
      *this, "cross-multi-instance",
      llvm::cl::desc("Resolve a module input by MEETING the known bits of "
                     "every instance's operand, instead of requiring the "
                     "module to have exactly one instance (OFF by default)"),
      llvm::cl::init(false)};

  // ---- design-wide context -------------------------------------------
  // module name -> its unique hw.instance (nullptr when 0 or >1 instances)
  llvm::DenseMap<StringRef, hw::InstanceOp> uniqueInstance;
  // module name -> EVERY hw.instance of it (only populated/used under
  // --cross-multi-instance).
  //
  // Why meeting over all instances is sound, and why the instance-RESULT
  // rule then needs no instance check at all: every fact this analysis
  // derives lives on a Value inside a module DEFINITION, so it is shared by
  // all of that module's instances, and the rewrite it drives (replacing a
  // register's bits with literals) is likewise applied to the definition.
  // Meeting the operands of every instance is exactly the condition that
  // makes such a fact true for all of them. Once module inputs obey that
  // rule, a value read out of a callee is instance-independent too, so
  // reading it through any one instance is sound. A module with ZERO
  // instances (the top) keeps UNKNOWN inputs -- an empty meet would be
  // "all bits known", which for real primary inputs is exactly wrong.
  llvm::DenseMap<StringRef, llvm::SmallVector<hw::InstanceOp, 4>> allInstances;
  llvm::DenseMap<StringRef, hw::HWModuleOp> modulesByName;
  // firreg -> current per-bit hypothesis (known = still-hypothesized bits,
  // value = reset literal)
  llvm::DenseMap<Operation *, KnownBits> regHyp;
  // per-round memo
  llvm::DenseMap<Value, KnownBits> memo;
  llvm::SmallPtrSet<Value, 32> inProgress;
  // Explicit recursion budget. `inProgress` stops CYCLES but not DEPTH, and
  // the aggregate rules recurse once per element -- a nested
  // array-of-struct-of-array chases a lot of stack. Overflowing it is a
  // segfault, so bound it and return unknown instead (sound: unknown is the
  // lattice bottom).
  unsigned depth = 0;
  static constexpr unsigned kMaxDepth = 256;

  KnownBits eval(Value v) {
    unsigned width;
    if (auto intTy = dyn_cast<IntegerType>(v.getType())) {
      width = intTy.getWidth();
    } else if (aggregates) {
      // Packed width, MSB-first, matching HWAggregateToComb's lowering of
      // struct_create / array_create to comb.concat (first operand at MSB)
      // and hw::aggregateAttrToAPInt for aggregate constants.
      int64_t w = hw::getBitWidth(v.getType());
      if (w <= 0)
        return KnownBits::unknown(1);
      width = static_cast<unsigned>(w);
    } else {
      return KnownBits::unknown(1);
    }
    auto it = memo.find(v);
    if (it != memo.end())
      return it->second;
    if (!inProgress.insert(v).second)
      return KnownBits::unknown(width); // combinational cycle guard
    if (depth >= kMaxDepth) {
      inProgress.erase(v);
      return KnownBits::unknown(width);
    }
    ++depth;
    KnownBits r = evalImpl(v, width);
    --depth;
    inProgress.erase(v);
    // The two guards above manufacture an `unknown` that describes WHERE the
    // walk started, not the value, and it gets memoized along with every
    // result it fed into -- so which registers narrow depends on the order
    // the roots are walked in. That order is fixed to IR order below for
    // exactly this reason. (Not caching guarded results would make the
    // answer order-INDEPENDENT rather than merely reproducible, and a
    // little more precise, but it re-evaluates every cone reachable through
    // a cycle: tried, and cv32a60x's structural mapping had not finished
    // after 208 s against ~2 s with the cache, so it was abandoned rather
    // than measured to completion. The pessimism is sound either way.)
    memo.try_emplace(v, r);
    return r;
  }

  KnownBits evalSlice(Value input, unsigned lowBit, unsigned width) {
    KnownBits in = eval(input);
    // `eval` falls back to unknown(1) for anything it cannot size (a
    // non-integer type with aggregates off, or getBitWidth() <= 0), and
    // APInt::trunc requires the target width to be SMALLER than the
    // source -- so slicing such a value is undefined behaviour rather
    // than a conservative answer. Guard it: out-of-range means unknown.
    if (in.known.getBitWidth() < lowBit + width || width == 0)
      return KnownBits::unknown(width ? width : 1);
    return {in.known.lshr(lowBit).trunc(width),
            in.value.lshr(lowBit).trunc(width)};
  }

  KnownBits evalImpl(Value v, unsigned width) {
    APInt literal;
    if (matchPattern(v, m_ConstantInt(&literal)))
      return KnownBits::constant(literal.zextOrTrunc(width));

    if (auto arg = dyn_cast<BlockArgument>(v)) {
      // A module input: resolvable only through a unique instantiation.
      auto module = dyn_cast<hw::HWModuleOp>(arg.getOwner()->getParentOp());
      if (!module)
        return KnownBits::unknown(width);
      unsigned idx = arg.getArgNumber();
      if (multiInstance) {
        auto ai = allInstances.find(module.getModuleName());
        if (ai == allInstances.end() || ai->second.empty())
          return KnownBits::unknown(width); // top module: a real input.
        KnownBits r = KnownBits::unknown(width);
        bool first = true;
        for (hw::InstanceOp inst : ai->second) {
          if (idx >= inst.getNumOperands())
            return KnownBits::unknown(width);
          KnownBits o = eval(inst.getOperand(idx));
          if (first) {
            r = o;
            first = false;
            continue;
          }
          // Meet: a bit survives only where every instance knows it AND
          // they all agree on its value.
          APInt agree = ~(r.value ^ o.value);
          r.known &= o.known & agree;
          r.value &= r.known;
        }
        return r;
      }
      auto ui = uniqueInstance.find(module.getModuleName());
      if (ui == uniqueInstance.end() || !ui->second)
        return KnownBits::unknown(width);
      hw::InstanceOp inst = ui->second;
      if (idx >= inst.getNumOperands())
        return KnownBits::unknown(width);
      return eval(inst.getOperand(idx));
    }

    Operation *def = v.getDefiningOp();
    if (!def)
      return KnownBits::unknown(width);

    if (auto firreg = dyn_cast<seq::FirRegOp>(def)) {
      auto h = regHyp.find(firreg.getOperation());
      if (h == regHyp.end())
        return KnownBits::unknown(width);
      return h->second;
    }
    if (auto inst = dyn_cast<hw::InstanceOp>(def)) {
      // An instance result: resolvable through the callee's hw.output when
      // this is the callee's unique instantiation.
      auto calleeIt = modulesByName.find(inst.getModuleName());
      if (calleeIt == modulesByName.end())
        return KnownBits::unknown(width);
      if (!multiInstance) {
        auto ui = uniqueInstance.find(inst.getModuleName());
        if (ui == uniqueInstance.end() || ui->second != inst)
          return KnownBits::unknown(width);
      }
      Block *body = calleeIt->second.getBodyBlock();
      auto output = dyn_cast<hw::OutputOp>(body->getTerminator());
      if (!output)
        return KnownBits::unknown(width);
      unsigned idx = cast<OpResult>(v).getResultNumber();
      if (idx >= output.getNumOperands())
        return KnownBits::unknown(width);
      return eval(output.getOperand(idx));
    }
    if (aggregates) {
      // Packing convention is HWAggregateToComb's: struct_create /
      // array_create / array_concat all lower to comb.concat of their
      // operands in order, i.e. FIRST operand at the MSB.
      auto packFirstAtMSB = [&](ValueRange operands) {
        KnownBits r = KnownBits::unknown(width);
        unsigned offset = 0;
        for (Value in : llvm::reverse(operands)) {
          int64_t w = hw::getBitWidth(in.getType());
          if (w < 0 || offset + static_cast<unsigned>(w) > width)
            return KnownBits::unknown(width);
          KnownBits o = eval(in);
          if (o.known.getBitWidth() != static_cast<unsigned>(w))
            return KnownBits::unknown(width);
          r.known.insertBits(o.known, offset);
          r.value.insertBits(o.value, offset);
          offset += static_cast<unsigned>(w);
        }
        return r;
      };
      if (auto sc = dyn_cast<hw::StructCreateOp>(def))
        return packFirstAtMSB(sc.getInput());
      if (auto ac = dyn_cast<hw::ArrayCreateOp>(def))
        return packFirstAtMSB(ac.getInputs());
      if (auto acc = dyn_cast<hw::ArrayConcatOp>(def))
        return packFirstAtMSB(acc.getInputs());
      if (auto bc = dyn_cast<hw::BitcastOp>(def)) {
        KnownBits in = eval(bc.getInput());
        if (in.known.getBitWidth() == width)
          return in;
        return KnownBits::unknown(width);
      }
      if (auto agg = dyn_cast<hw::AggregateConstantOp>(def)) {
        APInt bits;
        if (succeeded(hw::aggregateAttrToAPInt(agg.getType(),
                                               agg.getFieldsAttr(), bits)))
          return KnownBits::constant(bits.zextOrTrunc(width));
        return KnownBits::unknown(width);
      }
      if (auto se = dyn_cast<hw::StructExtractOp>(def)) {
        // Field offset from the LSB: fields after this one, first at MSB.
        auto st = dyn_cast<hw::StructType>(
            hw::getCanonicalType(se.getInput().getType()));
        if (!st)
          return KnownBits::unknown(width);
        auto elems = st.getElements();
        unsigned off = 0;
        bool found = false;
        for (int i = static_cast<int>(elems.size()) - 1; i >= 0; --i) {
          int64_t w = hw::getBitWidth(elems[i].type);
          if (w < 0)
            return KnownBits::unknown(width);
          if (elems[i].name == se.getFieldNameAttr()) { found = true; break; }
          off += static_cast<unsigned>(w);
        }
        if (!found)
          return KnownBits::unknown(width);
        return evalSlice(se.getInput(), off, width);
      }
      if (auto ag = dyn_cast<hw::ArrayGetOp>(def)) {
        auto at = dyn_cast<hw::ArrayType>(
            hw::getCanonicalType(ag.getInput().getType()));
        if (!at || width == 0)
          return KnownBits::unknown(width);
        unsigned n = at.getNumElements();
        APInt idx;
        if (matchPattern(ag.getIndex(), m_ConstantInt(&idx))) {
          uint64_t i = idx.getZExtValue();
          if (i >= n)
            return KnownBits::unknown(width);
          // element i sits at offset i*width from the LSB (index 0 is the
          // LAST concat operand, i.e. the LSB end)
          return evalSlice(ag.getInput(), static_cast<unsigned>(i) * width,
                           width);
        }
        // Dynamic index: the result is SOME element, so a bit is known only
        // where every element agrees.
        KnownBits r = evalSlice(ag.getInput(), 0, width);
        for (unsigned i = 1; i < n; ++i) {
          KnownBits e = evalSlice(ag.getInput(), i * width, width);
          APInt agree = ~(r.value ^ e.value);
          r.known &= e.known & agree;
          r.value &= r.known;
        }
        return r;
      }
      if (auto ai = dyn_cast<hw::ArrayInjectOp>(def)) {
        // array_inject(arr, idx, val): element i becomes
        //   idx == i ? val : arr[i]
        // so a bit of element i is known only where arr[i] and val agree --
        // sound whatever the index does, and exactly what makes the FIFO's
        // constant fields provable.
        auto at = dyn_cast<hw::ArrayType>(
            hw::getCanonicalType(ai.getInput().getType()));
        if (!at)
          return KnownBits::unknown(width);
        unsigned n = at.getNumElements();
        if (n == 0 || width % n != 0)
          return KnownBits::unknown(width);
        unsigned ew = width / n;
        KnownBits val = eval(ai.getElement());
        if (val.known.getBitWidth() != ew)
          return KnownBits::unknown(width);
        KnownBits r = KnownBits::unknown(width);
        for (unsigned i = 0; i < n; ++i) {
          KnownBits old = evalSlice(ai.getInput(), i * ew, ew);
          APInt agree = ~(old.value ^ val.value);
          APInt k = old.known & val.known & agree;
          APInt v = old.value & k;
          r.known.insertBits(k, i * ew);
          r.value.insertBits(v, i * ew);
        }
        return r;
      }
    }

    if (auto ext = dyn_cast<comb::ExtractOp>(def))
      return evalSlice(ext.getInput(), ext.getLowBit(), width);
    if (auto cat = dyn_cast<comb::ConcatOp>(def)) {
      KnownBits r = KnownBits::unknown(width);
      unsigned offset = 0;
      for (int i = static_cast<int>(cat.getInputs().size()) - 1; i >= 0; --i) {
        Value operand = cat.getInputs()[i];
        unsigned w = cast<IntegerType>(operand.getType()).getWidth();
        KnownBits o = eval(operand);
        r.known.insertBits(o.known, offset);
        r.value.insertBits(o.value, offset);
        offset += w;
      }
      return r;
    }
    if (auto cmp = dyn_cast<comb::ICmpOp>(def)) {
      // No comparison rule existed at all, so every `comb.icmp` read as
      // UNKNOWN. That is what kept CVA6's prefetcher alive: its FSM next
      // state is mux(icmp ceq(<all-constant concat>, 0), ...) and the
      // comparison is the only thing between the lattice and a provably
      // dead block.
      //
      // `ceq`/`cne` are CASE equality (SystemVerilog ===). On the 2-state
      // values synthesis works with they are exactly `eq`/`ne`, which is
      // also how CombToSynth lowers them; treating them otherwise here
      // would be strictly more conservative than the rest of the flow.
      KnownBits a = eval(cmp.getLhs()), b = eval(cmp.getRhs());
      if (!a.known.isAllOnes() || !b.known.isAllOnes() ||
          a.value.getBitWidth() != b.value.getBitWidth())
        return KnownBits::unknown(width);
      const APInt &x = a.value, &y = b.value;
      bool r;
      switch (cmp.getPredicate()) {
      case comb::ICmpPredicate::eq:
      case comb::ICmpPredicate::ceq:
      case comb::ICmpPredicate::weq:  r = x.eq(y);  break;
      case comb::ICmpPredicate::ne:
      case comb::ICmpPredicate::cne:
      case comb::ICmpPredicate::wne:  r = x.ne(y);  break;
      case comb::ICmpPredicate::ult:  r = x.ult(y); break;
      case comb::ICmpPredicate::ule:  r = x.ule(y); break;
      case comb::ICmpPredicate::ugt:  r = x.ugt(y); break;
      case comb::ICmpPredicate::uge:  r = x.uge(y); break;
      case comb::ICmpPredicate::slt:  r = x.slt(y); break;
      case comb::ICmpPredicate::sle:  r = x.sle(y); break;
      case comb::ICmpPredicate::sgt:  r = x.sgt(y); break;
      case comb::ICmpPredicate::sge:  r = x.sge(y); break;
      default: return KnownBits::unknown(width);
      }
      return KnownBits::constant(APInt(width, r ? 1 : 0));
    }

    if (auto rep = dyn_cast<comb::ReplicateOp>(def)) {
      KnownBits o = eval(rep.getInput());
      unsigned w = o.known.getBitWidth();
      KnownBits r = KnownBits::unknown(width);
      for (unsigned off = 0; off + w <= width; off += w) {
        r.known.insertBits(o.known, off);
        r.value.insertBits(o.value, off);
      }
      return r;
    }
    if (auto mux = dyn_cast<comb::MuxOp>(def)) {
      KnownBits c = eval(mux.getCond());
      if (c.allKnown())
        return eval(c.value.isOne() ? mux.getTrueValue()
                                     : mux.getFalseValue());
      KnownBits t = eval(mux.getTrueValue());
      KnownBits f = eval(mux.getFalseValue());
      APInt known = t.known & f.known & ~(t.value ^ f.value);
      return {known, t.value & known};
    }
    if (auto andOp = dyn_cast<comb::AndOp>(def)) {
      APInt knownZero = APInt::getZero(width), knownOne = APInt::getAllOnes(width);
      for (Value operand : andOp.getInputs()) {
        KnownBits o = eval(operand);
        knownZero |= o.known & ~o.value;
        knownOne &= o.known & o.value;
      }
      return {knownZero | knownOne, knownOne};
    }
    if (auto orOp = dyn_cast<comb::OrOp>(def)) {
      APInt knownOne = APInt::getZero(width), knownZero = APInt::getAllOnes(width);
      for (Value operand : orOp.getInputs()) {
        KnownBits o = eval(operand);
        knownOne |= o.known & o.value;
        knownZero &= o.known & ~o.value;
      }
      return {knownOne | knownZero, knownOne};
    }
    if (auto xorOp = dyn_cast<comb::XorOp>(def)) {
      APInt known = APInt::getAllOnes(width), value = APInt::getZero(width);
      for (Value operand : xorOp.getInputs()) {
        KnownBits o = eval(operand);
        known &= o.known;
        value ^= o.value;
      }
      return {known, value & known};
    }
    return KnownBits::unknown(width);
  }

  void runOnOperation() override {
    if (!enable)
      return;
    ModuleOp top = getOperation();

    // ---- design context: modules and their instance counts -------------
    llvm::DenseMap<StringRef, unsigned> instCount;
    llvm::DenseMap<StringRef, hw::InstanceOp> firstInst;
    for (auto module : top.getOps<hw::HWModuleOp>()) {
      modulesByName.try_emplace(module.getModuleName(), module);
      module.walk([&](hw::InstanceOp inst) {
        auto &n = instCount[inst.getModuleName()];
        ++n;
        if (n == 1)
          firstInst[inst.getModuleName()] = inst;
        if (multiInstance)
          allInstances[inst.getModuleName()].push_back(inst);
      });
    }
    for (auto &[name, n] : instCount)
      uniqueInstance[name] = n == 1 ? firstInst[name] : hw::InstanceOp();

    // ---- hypotheses: every reset-literal integer firreg, all bits ------
    // `regOrder` is the same set as `regHyp` in IR order. The fixed point
    // below must walk it in THAT order, not in `regHyp`'s: a DenseMap keyed
    // by Operation * iterates in pointer order, which differs between
    // processes (see `eval`'s note on the memo).
    llvm::DenseMap<Operation *, APInt> resetOf;
    SmallVector<Operation *> regOrder;
    for (auto module : top.getOps<hw::HWModuleOp>()) {
      for (auto firreg : module.getBodyBlock()->getOps<seq::FirRegOp>()) {
        auto intTy = dyn_cast<IntegerType>(firreg.getNext().getType());
        if (!intTy)
          continue;
        APInt resetConst;
        if (firreg.getReset()) {
          if (!matchPattern(firreg.getResetValue(), m_ConstantInt(&resetConst)))
            continue;
        } else if (foldUnresetHolds) {
          // No reset: hypothesise zero. The greatest fixed point below drops
          // every bit it cannot prove, so a genuine data register (loaded each
          // cycle) simply loses all its bits and nothing changes; only a
          // provable hold survives. See foldUnresetHolds' comment for why this
          // is an assumption and not a proof.
          resetConst = APInt::getZero(intTy.getWidth());
        } else {
          continue;
        }
        APInt r = resetConst.zextOrTrunc(intTy.getWidth());
        resetOf.try_emplace(firreg.getOperation(), r);
        if (regHyp.try_emplace(firreg.getOperation(), KnownBits::constant(r))
                .second)
          regOrder.push_back(firreg.getOperation());
      }
    }
    if (regHyp.empty())
      return;

    // ---- greatest fixed point, Jacobi rounds ----------------------------
    bool changed = true;
    unsigned rounds = 0;
    while (changed) {
      changed = false;
      ++rounds;
      memo.clear();
      llvm::DenseMap<Operation *, APInt> newKnown;
      for (Operation *op : regOrder) {
        KnownBits hyp = regHyp.find(op)->second;
        if (hyp.known.isZero())
          continue;
        auto firreg = cast<seq::FirRegOp>(op);
        KnownBits nb = eval(firreg.getNext());
        const APInt &reset = resetOf[op];
        APInt keep = hyp.known & nb.known & ~(nb.value ^ reset);
        if (keep != hyp.known) {
          newKnown[op] = keep;
          changed = true;
        }
      }
      for (auto &[op, keep] : newKnown)
        regHyp[op].known = keep;
    }

    // ---- rewrite ---------------------------------------------------------
    unsigned numConst = 0, numNarrowed = 0, totalBits = 0;
    llvm::StringMap<unsigned> perModule;
    for (auto module : top.getOps<hw::HWModuleOp>()) {
      for (auto firreg : llvm::make_early_inc_range(
               module.getBodyBlock()->getOps<seq::FirRegOp>())) {
        auto h = regHyp.find(firreg.getOperation());
        if (h == regHyp.end() || h->second.known.isZero())
          continue;
        const APInt &mask = h->second.known; // constant bits
        const APInt &reset = resetOf[firreg.getOperation()];
        unsigned width = mask.getBitWidth();
        OpBuilder builder(firreg);
        Location loc = firreg.getLoc();
        totalBits += mask.popcount();
        perModule[module.getModuleName()] += mask.popcount();
        if (mask.isAllOnes()) {
          Value c = hw::ConstantOp::create(builder, loc, reset);
          firreg.getData().replaceAllUsesWith(c);
          firreg.erase();
          ++numConst;
          continue;
        }
        SmallVector<unsigned> varBits; // ascending original indices
        for (unsigned i = 0; i < width; ++i)
          if (!mask[i])
            varBits.push_back(i);
        unsigned varCount = varBits.size();
        Type i1Type = builder.getIntegerType(1);
        auto extractBit = [&](Value v, unsigned bit) -> Value {
          return builder.createOrFold<comb::ExtractOp>(loc, i1Type, v, bit);
        };
        auto compact = [&](Value wide) -> Value {
          SmallVector<Value> ops; // MSB-first
          for (unsigned k = varCount; k-- > 0;)
            ops.push_back(extractBit(wide, varBits[k]));
          return varCount == 1 ? ops[0]
                               : builder.createOrFold<comb::ConcatOp>(loc, ops);
        };
        Value newNext = compact(firreg.getNext());
        std::string newName = (Twine(firreg.getName()) + "_cbits").str();
        // A reset-less register (only reachable under fold-unreset-holds)
        // has a NULL resetValue -- compacting it would dereference null.
        seq::FirRegOp newFirreg =
            firreg.getReset()
                ? seq::FirRegOp::create(
                      builder, loc, newNext, firreg.getClk(),
                      builder.getStringAttr(newName), firreg.getReset(),
                      compact(firreg.getResetValue()),
                      firreg.getInnerSymAttr(), firreg.getIsAsync(),
                      firreg.getPresetAttr())
                : seq::FirRegOp::create(
                      builder, loc, newNext, firreg.getClk(),
                      builder.getStringAttr(newName),
                      firreg.getInnerSymAttr(), firreg.getPresetAttr());
        SmallVector<Value> recon; // MSB-first
        for (unsigned i = width; i-- > 0;) {
          if (mask[i]) {
            recon.push_back(hw::ConstantOp::create(
                builder, loc, APInt(1, reset[i] ? 1 : 0)));
          } else {
            unsigned rank = llvm::find(varBits, i) - varBits.begin();
            recon.push_back(extractBit(newFirreg.getData(), rank));
          }
        }
        Value reconstructed =
            width == 1 ? recon[0]
                       : builder.createOrFold<comb::ConcatOp>(loc, recon);
        firreg.getData().replaceAllUsesWith(reconstructed);
        firreg.erase();
        ++numNarrowed;
      }
    }
    if (numConst + numNarrowed) {
      llvm::errs() << "[narrow-constant-register-bits] " << rounds
                   << " round(s): " << numConst
                   << " firreg(s) replaced by literals, " << numNarrowed
                   << " narrowed; " << totalBits << " constant bit(s) removed";
      for (auto &kv : perModule)
        llvm::errs() << "; " << kv.first() << ": " << kv.second;
      llvm::errs() << "\n";
    }
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createNarrowConstantRegisterBitsPass() {
  return std::make_unique<NarrowConstantRegisterBitsPass>();
}

} // namespace circt_abc
