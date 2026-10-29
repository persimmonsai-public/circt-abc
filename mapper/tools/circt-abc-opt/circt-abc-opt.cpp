//===- circt-abc-opt.cpp - circt-abc pass driver -------------*- C++ -*-===//
//
// Standard MLIR *-opt tool shape (MlirOptMain over a DialectRegistry),
// registering the CIRCT dialects the structural mapper operates on plus
// circt-abc's own passes.
//
// TODO(M0): confirm MlirOptMain's exact signature/include path and the
// CIRCT dialect registration calls against the real, built headers --
// written from standard MLIR tool convention, not yet compiled.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/LTL/LTLDialect.h"
#include "circt/Dialect/Sim/SimDialect.h"
#include "circt/Dialect/Verif/VerifDialect.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/HW/HWPasses.h"
#include "circt/Dialect/LLHD/LLHDDialect.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/Synth/SynthDialect.h"
#include "circt/Dialect/Synth/Transforms/SynthPasses.h"
#include "circt/Conversion/Passes.h"
#include "circt/Dialect/Datapath/DatapathDialect.h"

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/ControlFlow/IR/ControlFlow.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Dialect/UB/IR/UBOps.h"
#include "mlir/IR/Dialect.h"
#include "mlir/IR/MLIRContext.h"
#include "mlir/Support/LogicalResult.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "mlir/Transforms/Passes.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<circt::hw::HWDialect, circt::comb::CombDialect,
                   circt::seq::SeqDialect, circt::llhd::LLHDDialect,
                   circt::synth::SynthDialect,
                   circt::datapath::DatapathDialect>();
  // Raw circt-verilog output, before any LLHD lowering, still carries the
  // frontend's own dialects: `cf`/`arith` for un-lowered process control flow
  // and `sim`/`verif`/`ltl` for $display, assertions and their properties.
  // --strip-simulation-constructs is specifically the pass that has to read
  // that IR, so this tool has to be able to parse it. Without these,
  // circt-abc-opt fails at PARSE time with "Dialect `sim' not found", which
  // reads like a missing pass rather than a missing registration.
  registry.insert<circt::sim::SimDialect, circt::verif::VerifDialect,
                   circt::ltl::LTLDialect, mlir::cf::ControlFlowDialect,
                   mlir::arith::ArithDialect>();
  // ImportVerilog also emits `scf.if` (function bodies with early returns)
  // and `ub.unreachable` (after $fatal / sim.terminate). Without them the
  // tool fails to PARSE such IR ("Dialect `scf' not found").
  registry.insert<mlir::scf::SCFDialect, mlir::ub::UBDialect>();

  // Matches the `-name CirctAbc` given to mlir_tablegen() for Passes.td
  // (see mapper/include/circt-abc/CMakeLists.txt) -- MLIR's
  // -gen-pass-decls emits register<Name>Passes() under GEN_PASS_REGISTRATION.
  circt_abc::registerCirctAbcPasses();
  // HW-dialect transforms: the structural-mapping stage re-runs
  // --hw-aggregate-to-comb AFTER the register passes, because on
  // aggregate-typed registers (CVA6's bht is array<16xarray<2xstruct<...>>>)
  // clock-gate-inference and register-mapper insert enable muxes AT THE
  // REGISTER'S TYPE, reintroducing aggregate comb ops after the first
  // de-aggregation -- and an aggregate constant in a residual module makes
  // it unmappable to AIGER.
  circt::hw::registerPasses();
  // --canonicalize (and friends) -- required immediately after
  // --outline-residual-logic to remove the residual ops that pass
  // deliberately leaves un-erased (see its own comment for why).
  mlir::registerTransformsPasses();
  // CIRCT's Synth LongestPathAnalysis (used by the timing-aware datapath
  // lowering inside abc-bridge) lowers modules through a TEXTUAL pipeline
  // ("hw.module(hw-aggregate-to-comb,convert-comb-to-synth,cse,canonicalize)"),
  // which only parses if those passes are registered in this tool -- found
  // 2026-09-03 on a cv64a6 FMA group whose shape reached that path
  // ("'convert-comb-to-synth' does not refer to a registered pass").
  circt::registerConvertCombToSynth();   // only the pass the analysis names
  circt::registerConvertCombToDatapath(); // for the arch-propagation test
  circt::synth::registerPasses();

  return mlir::asMainReturnCode(
      mlir::MlirOptMain(argc, argv, "circt-abc-opt\n", registry));
}
