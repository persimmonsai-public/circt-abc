//===- LowerFirMemPass.cpp ------------------------------------*- C++ -*-===//
//
// See Passes.td's LowerFirMem description for the "why". Short version:
// `llhd-mem2reg` promotes small unpacked-array storage (the data words of a
// depth-2/4 FIFO) into `seq.firmem`, which nothing downstream of the mapper
// understands -- AIGER has no memory primitive, and ExportVerilog rejects a
// bare firmem outside the SV lowering. For memories this small, flops are
// the right implementation anyway.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct LowerFirMemPass
    : public PassWrapper<LowerFirMemPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerFirMemPass)

  LowerFirMemPass() = default;
  // See ExternalizeMappedCellsPass: with a Pass::Option member the defaulted
  // copy constructor is deleted; copy the option VALUE explicitly.
  LowerFirMemPass(const LowerFirMemPass &other) : PassWrapper(other) {
    maxDepth = other.maxDepth.getValue();
  }

  StringRef getArgument() const final { return "lower-firmem"; }
  StringRef getDescription() const final {
    return "Lower small seq.firmem memories to per-word registers";
  }

  Option<unsigned> maxDepth{
      *this, "max-depth",
      llvm::cl::desc("Only lower memories up to this depth; deeper ones are "
                      "presumed to become macros and are left alone"),
      llvm::cl::init(64)};

  void runOnOperation() override {
    size_t numLowered = 0, numSkipped = 0;
    SmallVector<seq::FirMemOp> mems;
    getOperation().walk([&](seq::FirMemOp op) { mems.push_back(op); });

    for (auto memOp : mems) {
      auto memType = memOp.getType();
      uint64_t depth = memType.getDepth();
      unsigned width = memType.getWidth();

      // Scope: combinational read (latency 0), registered write (latency 1),
      // whole-word writes, read/write ports only. Everything in scope was
      // observed on CVA6 (2x32, 2x3, 4x64 FIFO data words); anything else is
      // left for a real memory flow.
      bool ok = memOp.getReadLatency() == 0 && memOp.getWriteLatency() == 1 &&
                depth <= maxDepth && depth >= 1;
      SmallVector<seq::FirMemReadOp> reads;
      SmallVector<seq::FirMemWriteOp> writes;
      for (auto *user : memOp.getMemory().getUsers()) {
        if (auto r = dyn_cast<seq::FirMemReadOp>(user))
          reads.push_back(r);
        else if (auto w = dyn_cast<seq::FirMemWriteOp>(user)) {
          if (w.getMask())
            ok = false; // masked writes out of scope
          writes.push_back(w);
        } else
          ok = false; // read-write ports out of scope
      }
      if (!ok) {
        ++numSkipped;
        continue;
      }

      if (writes.empty()) {
        // A never-written memory holds undefined contents forever; leave it
        // alone rather than inventing values.
        ++numSkipped;
        continue;
      }

      OpBuilder builder(memOp);
      auto loc = memOp.getLoc();
      auto dataType = builder.getIntegerType(width);
      unsigned addrWidth = llvm::Log2_64_Ceil(std::max<uint64_t>(depth, 2));
      Value clock = writes.front().getClk();

      // One register per word. Each register's next-value mux references the
      // register's own result (the hold arm), so create every register with
      // a placeholder next -- a real value, the graph region tolerates the
      // forward shape but Operation::create does not tolerate null operands
      // -- and patch it after the muxes exist.
      Value zero = hw::ConstantOp::create(builder, loc, dataType, 0);
      SmallVector<seq::FirRegOp> words;
      for (uint64_t i = 0; i < depth; ++i) {
        auto reg = seq::FirRegOp::create(
            builder, loc, /*next=*/zero, clock,
            builder.getStringAttr(memOp.getName().value_or("mem").str() +
                                  "_word" + std::to_string(i)));
        words.push_back(reg);
      }

      // Apply the write ports: next_i = mux(en && addr==i, data, word_i),
      // chained across ports in op order (multiple writers to one word in
      // the same cycle resolve last-port-wins, matching `wuw undefined`'s
      // freedom).
      for (auto [i, reg] : llvm::enumerate(words)) {
        Value next = reg.getResult();
        for (auto w : writes) {
          Value idx = hw::ConstantOp::create(
              builder, loc,
              APInt(w.getAddress().getType().getIntOrFloatBitWidth(), i));
          Value sel = comb::ICmpOp::create(builder, loc,
                                           comb::ICmpPredicate::eq,
                                           w.getAddress(), idx, true);
          if (auto en = w.getEnable())
            sel = comb::AndOp::create(builder, loc, en, sel, true);
          next =
              comb::MuxOp::create(builder, loc, sel, w.getData(), next, true);
        }
        reg.getNextMutable().assign(next);
      }

      // Apply the read ports: a mux tree over the words by address.
      // Latency 0 means the read is combinational; the port's clock and
      // enable have no observable effect on the data (an enabled-off read
      // yields an unspecified value in FIRRTL semantics).
      for (auto r : reads) {
        Value result = words[0].getResult();
        for (uint64_t i = 1; i < depth; ++i) {
          Value idx = hw::ConstantOp::create(
              builder, loc, APInt(r.getAddress().getType().getIntOrFloatBitWidth(), i));
          Value sel = comb::ICmpOp::create(builder, loc, comb::ICmpPredicate::eq,
                                           r.getAddress(), idx, true);
          result = comb::MuxOp::create(builder, loc, sel, words[i].getResult(),
                                       result, true);
        }
        r.getData().replaceAllUsesWith(result);
      }

      for (auto r : reads)
        r.erase();
      for (auto w : writes)
        w.erase();
      memOp.erase();
      (void)dataType; (void)addrWidth;
      ++numLowered;
    }

    llvm::errs() << "[lower-firmem] lowered " << numLowered
                 << " memory(ies) to registers, skipped " << numSkipped
                 << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createLowerFirMemPass() {
  return std::make_unique<LowerFirMemPass>();
}

} // namespace circt_abc
