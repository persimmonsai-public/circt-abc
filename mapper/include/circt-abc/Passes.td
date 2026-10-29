//===- Passes.td - circt-abc pass definitions ------------*- tablegen -*-===//
//
// Pass definitions for circt-abc. Each pass's file comment carries its
// design notes; StructuralMapperPass.cpp's describes the pattern set.
//
//===----------------------------------------------------------------------===//

#ifndef CIRCT_ABC_PASSES_TD
#define CIRCT_ABC_PASSES_TD

include "mlir/Pass/PassBase.td"

def StructuralMapper : Pass<"structural-mapper", "mlir::ModuleOp"> {
  let summary = "RTL-level structural technology mapping on HW/Comb IR "
                "(M2: mux/compare-fusion and wide-select fusion patterns)";
  let description = [{
    Pattern 1 (always on when --liberty= is given): walks HW/Comb dialect
    IR looking for a single-bit `comb.mux` whose true/false leg is a
    single-use 2-input `comb.and`/`comb.or`/`comb.xor`, brute-force
    truth-table matches the resulting cone against every cell in the given
    liberty file (all leaf permutations tried, cheapest-area match wins),
    and -- on a match -- freezes the cone as an `hw.instance` of the
    matched cell (declared as a fresh `hw.module.extern`), before any
    lowering to AIG. Residual (unmatched) logic is left untouched for
    Yosys/ABC (M1's delegation path) or a later CombToSynth + ABC-runner
    pass to handle. **Root-caused, real-design result: matches 0/93
    candidate sites, always** -- not a coverage gap or bug, but a
    Boolean-algebraic wall: the cone's function is binate in the mux's
    select, while every AOI/OAI/AO/OA-class compound liberty cell is
    provably unate. See StructuralMapperPass.cpp's file comment for the
    full writeup.

    Pattern 2 (`--enable-wide-mux-fusion`, OFF by default): recognizes a
    `comb.mux` whose true/false legs are BOTH themselves `comb.mux`,
    sharing one inner select distinct from the outer select -- a genuine
    4:1 select over independent select bits, matching real wide-mux
    liberty cells (MUX4-class cells). Structurally sound (no binate
    obstruction) and SAT-verified correct (both synthetically and via the
    pass's own mandatory per-match truth-table re-verification), but
    measured as a real AREA REGRESSION on a whole real design: freezing
    these cones as
    opaque `hw.instance` boundaries blocks ABC's own fanout/buffer-
    load-balancing optimization around the shared select signals more
    than the fused cells save (the buffer count alone roughly tripled). Left
    available (not deleted) as tested infrastructure for a future
    area-delta-aware version, per the design rule "absorbing a leg is only
    safe when... cheaper after accounting for" rule -- not currently
    attempted. See StructuralMapperPass.cpp's file comment.

    With no --liberty= given, falls back to the original M0 behavior:
    performs no rewriting, just reports HW/Comb op-count statistics.
  }];
  let constructor = "circt_abc::createStructuralMapperPass()";
  let options = [
    Option<"libertyPath", "liberty", "std::string", "",
           "Path to the liberty file used to build the truth-table "
           "cell-matching oracle; omit for M0 census-only mode">,
    Option<"enableWideMuxFusion", "enable-wide-mux-fusion", "bool", "false",
           "Enable Pattern 2 (nested mux-of-mux wide-select fusion) -- "
           "OFF by default, see this pass's description for why">,
  ];
}

def OutlineResidualLogic : Pass<"outline-residual-logic", "hw::HWModuleOp"> {
  let summary = "Outline non-instance logic into its own module for AIG/ABC processing";
  let description = [{
    `circt-translate --export-aiger` (and CIRCT's `ExportAIGER`/`AIGERRunner`
    machinery more generally, for the final liberty-mapping step which
    cannot round-trip through AIGER at all -- AIGER is a pure 2-input
    AND/INVERT format, it cannot carry named standard-cell instances)
    requires a module containing ONLY AIG-representable ops: any
    `hw.instance` (e.g. a register already mapped to a real DFF liberty
    cell, or -- once the structural mapper lands -- an already-matched
    compound gate) makes the whole module unexportable, confirmed this
    session ("unhandled operation: hw.instance").

    This pass extracts every op that is NOT an `hw.instance` (and not the
    terminator) into a new, separate, private `hw.module` named
    `<original>_residual`, replacing the extracted ops in the original
    module with a single instance of that new module wired to the same
    live-in/live-out values. The original module's `hw.instance` ops are
    left untouched in place; the now-dead residual ops are deliberately
    NOT erased by this pass itself (two different manual-erasure attempts
    both corrupted real IR -- see OutlineResidualLogicPass.cpp's comment)
    -- run `--canonicalize` immediately afterward to remove them via
    MLIR's own dead-code elimination. Run `--hw-flatten-modules` after the
    outlined module has been ABC-mapped and reimported to inline
    everything back into one flat module (matching `synth.tcl`'s flat
    `write_verilog` output shape).
  }];
  let constructor = "circt_abc::createOutlineResidualLogicPass()";
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def MergeResidualModules : Pass<"merge-residual-modules", "mlir::ModuleOp"> {
  let summary = "Merge every zero-instance leaf hw.module into one, for a "
                "single whole-design abc-bridge call";
  let description = [{
    abc-bridge invokes one ABC subprocess per zero-instance leaf hw.module
    (one per `--outline-residual-logic`-created `_residual` submodule, one
    per original ibex submodule) -- ~28 isolated ABC calls on the real
    design, each optimizing its own slice of logic with no visibility into
    any other module. This is the documented root cause of M1b's QoR gap:
    no cross-module sharing, no global optimization scope, unlike Yosys/M1a's
    single global ABC call.

    This pass merges every zero-instance leaf module in the design into one
    combined module and collapses their (assumed-unique) instantiation
    sites into a single new instance of it, so a following abc-bridge run
    finds exactly one target. Precondition: every target's instantiation
    site must already be in the SAME parent block -- run
    `--hw-flatten-modules` on the shell (structural, instance-only)
    hierarchy first; see MergeResidualModulesPass.cpp's file comment for
    why `--outline-residual-logic`'s modules are deliberately left public
    (not private) so that flatten collapses the shells without also
    inlining -- and thereby destroying -- these leaves.
  }];
  let constructor = "circt_abc::createMergeResidualModulesPass()";
  let dependentDialects = ["hw::HWDialect"];
}

def ExternalizeMappedCells : Pass<"externalize-mapped-cells", "mlir::ModuleOp"> {
  let summary = "Convert hw.module standard-cell placeholders into hw.module.extern";
  let description = [{
    Real standard-cell instances -- both ones pre-existing in RTL (e.g. an
    clock-gating cell) and ones ABC's mapper produces when its Verilog
    output is re-imported via circt-verilog -- arrive as ordinary
    `hw.module`s with a body, because circt-verilog synthesizes a definite
    driver (typically constant 0) for every unassigned output port rather
    than leaving the module truly bodiless. That body is not real logic and
    must never be treated as such by later passes (folded, inlined, used
    for cost analysis, etc.) -- this pass converts each `hw.module` whose
    name appears in the given cell-name list into a real `hw.module.extern`
    (declaration only), using the same rewrite CIRCT's own circt-reduce
    `ModuleExternalizer` reduction pattern uses (HWReductions.cpp).
  }];
  let constructor = "circt_abc::createExternalizeMappedCellsPass()";
  let options = [
    Option<"cellNamesFile", "cell-names-file", "std::string", "",
           "Path to a file listing one standard-cell module name per line "
           "(e.g. derived from gen_liberty_blackbox_stubs.py's output)">,
  ];
  let dependentDialects = ["hw::HWDialect"];
}

def LowerFirMem : Pass<"lower-firmem", "mlir::ModuleOp"> {
  let summary = "Lower small seq.firmem memories to per-word registers";
  let description = [{
    `llhd-mem2reg` promotes small unpacked-array storage (a depth-2 FIFO's
    data words) into `seq.firmem`, which nothing downstream of the mapper
    understands: AIGER has no memory primitive, and a residual module
    carrying one is blackboxed. For memories this small, flops are the right
    implementation anyway.

    Scope, deliberately narrow: read latency 0 (combinational read), write
    latency 1, whole-word writes, read/write ports only, depth <= max-depth.
    Deeper or masked memories are presumed to become macros and left alone.
  }];
  let constructor = "circt_abc::createLowerFirMemPass()";
  let options = [
    Option<"maxDepth", "max-depth", "unsigned", "64",
           "Only lower memories up to this depth">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect",
                            "seq::SeqDialect"];
}

def StripSimulationConstructs : Pass<"strip-simulation-constructs", "mlir::ModuleOp"> {
  let summary = "Erase assertion, $display/$fopen and initial-block processes";
  let description = [{
    SVA assertions, `$fopen`/`$display` and `initial` blocks survive
    elaboration as `llhd.process` ops. No LLHD lowering pass will ever lower
    them, because there is nothing to lower them TO: they describe what a
    simulator should do, not what the hardware is. `LowerProcesses` reports
    them as `no wait op` (an assertion process loops forever with no wait)
    or `contains halt terminator` (an initial block runs once and stops), and
    they then hold their module's signals hostage -- `sig2reg` cannot promote
    a signal an unlowered process still touches, so the module keeps its
    `llhd.sig` and gets blackboxed.

    On CVA6 this is the WHOLE residue at that stage: 21 assertion processes
    and 3 initial blocks, out of 32 processes total, and nothing else.

    A process is erased when it contains a `verif` op, or (under
    `strip-initial`, the default) when it terminates in `llhd.halt`. Erasure
    takes the process's transitive consumers with it -- but only if every one
    of them is itself a `verif`/`ltl`/`sim` op or an `llhd.drv` that exists to
    carry the result into a signal. If any consumer is real hardware the
    process is left completely alone and counted as kept, because a
    half-erased process would leave dangling values behind.

    `strip-initial` is separable because it is the one judgement call here:
    an `initial` block has no silicon meaning and ASIC synthesis ignores it,
    but that IS a semantic choice rather than a pure simplification. CVA6's
    `tc_sram` is the motivating case -- its initial block zeroes an array
    whose signal already has that same all-zero initial value, so erasing it
    changes nothing at all there.
  }];
  let constructor = "circt_abc::createStripSimulationConstructsPass()";
  let options = [
    Option<"stripInitial", "strip-initial", "bool", "true",
           "Also erase `initial` blocks (processes terminating in llhd.halt)">,
  ];
  let dependentDialects = ["hw::HWDialect", "llhd::LLHDDialect"];
}

def LowerClockInverters : Pass<"lower-clock-inverters", "mlir::ModuleOp"> {
  let summary = "Rewrite seq clock cast/invert chains back into comb logic";
  let description = [{
    A negedge-clocked process lowers to a clock that is inverted in the `seq`
    dialect: `seq.from_clock(seq.clock_inv(seq.to_clock(%x)))`. When that
    chain feeds ordinary combinational logic rather than a register's clock
    port -- which is exactly what happens when the inverted clock is also
    used as data, or is passed out of a module as an i1 -- it is nothing but
    an inverter that has taken a round trip through `!seq.clock`.

    Nothing downstream understands it. AIGER is a bit-level format with no
    clock type, so `abc-bridge` cannot export a module containing one, and
    the module gets blackboxed instead: on OpenC910 this was the entire
    reason `ct_had_sm` and `ct_had_serial` dropped out of the netlist, each
    for a single three-op chain among ~100 otherwise plain comb ops.

    This pass rewrites `from_clock(clock_inv^n(to_clock(%x)))` to
    `comb.xor(%x, true)` for ODD `n`. Even `n` is a pure identity and is
    deliberately left alone: such pairs are common (166 on ibex) and have
    always been harmless, so rewriting them would churn `struct.mlir`
    design-wide and cost the byte-identity regression gate its meaning for
    no measured benefit.

    It only fires when the chain bottoms out at a `seq.to_clock`, so a clock
    that genuinely arrives as a `!seq.clock` (a block argument, or a
    `seq.clock_gate` output) is left alone, as is any `seq.clock_inv` still
    feeding a real clock port.

    Ordering matters: it must run AFTER `register-mapper` and BEFORE
    `outline-residual-logic`. A negedge register keeps its clock as a
    `!seq.clock` until register-mapper swaps it for a DFF cell whose CK port
    is an i1, and that swap is what inserts the closing `seq.from_clock`.
  }];
  let constructor = "circt_abc::createLowerClockInvertersPass()";
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect",
                            "seq::SeqDialect"];
}

def ABCBridge : Pass<"abc-bridge", "mlir::ModuleOp"> {
  let summary = "Map residual comb logic to real liberty cells via a "
                "real ABC subprocess (no Yosys in the loop)";
  let description = [{
    For each target `hw.module` (by default: every module with zero
    `hw.instance` ops inside it, e.g. one produced by
    `--outline-residual-logic`) -- lowers it to `synth.aig` form
    (`convert-comb-to-synth`), exports real AIGER, runs a real ABC
    subprocess with a tuned mapping recipe (see RECIPE.md),
    re-imports ABC's mapped Verilog output (which names real liberty cells)
    in-process via `circt::importVerilog`, externalizes the resulting cell
    placeholder modules into `hw.module.extern`, and splices the mapped
    result back in under the target's original symbol name so existing
    `hw.instance` references elsewhere keep working unchanged.

    This is the CIRCT-native alternative to M1's Yosys-delegation path
    (`scripts/run_circt_synth.sh`): no Yosys/`synth.tcl` involved anywhere
    in this pass. See RECIPE.md's "Full mechanism validated end-to-end"
    section for the manually-verified recipe this automates.
  }];
  let constructor = "circt_abc::createABCBridgePass()";
  let options = [
    Option<"libertyPath", "liberty", "std::string", "",
           "Path to the liberty file ABC maps against">,
    Option<"abcPath", "abc-path", "std::string", "\"abc\"",
           "Path to (or name on PATH of) the real ABC binary to invoke">,
    Option<"stubsPath", "stubs", "std::string", "",
           "Path to a blackbox-cell-declaration .sv file (e.g. from "
           "gen_liberty_blackbox_stubs.py) covering any cell ABC's mapper "
           "might emit, so re-import can bind them">,
    Option<"clockPeriodPs", "clock-period-ps", "unsigned", "1000",
           "Clock period in picoseconds, passed to ABC's -D flag">,
    Option<"targetModule", "target", "std::string", "",
           "Name of the single hw.module to map; if empty, every "
           "zero-instance, non-top-level hw.module is processed">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect",
                           "synth::SynthDialect"];
}

def RegisterLegalize : Pass<"register-legalize", "hw::HWModuleOp"> {
  let summary = "Fold sync/non-constant-reset seq.firreg ops into D-input mux logic";
  let description = [{
    `seq.firreg`'s liberty-mappable shapes (see RegisterMapper) are limited to
    "no reset" and "async reset to a CONSTANT value" -- exactly what the three
    flip-flop classes this flow targets (plain, async-clear, async-preset) can
    express, since none of them has a separate synchronous-reset variant and
    none can reset to a non-constant value. This pass finds every `seq.firreg`
    whose reset is either synchronous (`!isAsync`) or async-but-non-constant,
    and rewrites it in place: `next` becomes `comb.mux(reset, resetValue, next)`
    and the reset/resetValue operands are dropped entirely, folding the reset
    semantics into ordinary D-input logic (exactly how a synchronous reset is
    really implemented in silicon with these cells, and the only option
    available for a non-constant async reset value, which the target cells
    cannot express directly either). Firregs already in a mappable shape (no
    reset, or async + constant resetValue) are left untouched.

    Must run before RegisterMapper in the pipeline: it guarantees every
    remaining `seq.firreg` reset (if any) is async with a constant
    resetValue, the only 2 shapes RegisterMapper needs to handle.
  }];
  let constructor = "circt_abc::createRegisterLegalizePass()";
  let dependentDialects = ["comb::CombDialect", "seq::SeqDialect"];
}

def RemoveDeadRegisters : Pass<"remove-dead-registers", "hw::HWModuleOp"> {
  let summary = "Remove seq.firreg ops with no real, transitively-live consumer";
  let description = [{
    `seq.firreg` is deliberately declared with real MemoryEffects in CIRCT's
    own SeqOps.td (it models genuine hardware state), so neither MLIR's
    generic canonicalizer/DCE nor CIRCT's own `--hw-imdce` will ever remove
    one purely because its result is unused -- confirmed to matter in
    practice: ibex's `imd_val_q` register (read only by RV32B-gated
    bit-manip logic, dead when `RV32B` defaults to `RV32BNone`, as this
    flow's build does) is genuinely unused end-to-end yet survives every
    existing CIRCT cleanup pass this flow already runs.

    Plain mark-and-sweep DCE seeded only from the module's real `hw.output`
    operands: a firreg's `next`/`clk`/`reset`/`resetValue` operands are only
    visited once its own `data` result has already been marked live by some
    real consumer, so a register whose value never escapes to a real output
    is never "kick-started" into the live set -- correctly handling the
    common self-referencing hold-value pattern (`next =
    comb.mux(enable, new, <this register's own data>)`), a dead cycle a
    naive "zero uses" check could never catch (every op in such a cycle
    always has at least one use: something else in the same cycle).
    Deliberately never removes an `hw.instance` (of a real extern cell) even
    if its results are unused -- an opaque extern's side effects, if any,
    aren't provable from here.

    Must run on plain `seq.firreg`s, before register-mapper bit-blasts them
    into opaque `hw.instance`s of extern cells (no longer analyzable this
    way), and after the whole design is flattened into one module (for
    cross-module reachability, without reimplementing `--hw-imdce`'s own
    intermodule port-pruning logic).
  }];
  let constructor = "circt_abc::createRemoveDeadRegistersPass()";
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def NarrowDeadRegisterBits : Pass<"narrow-dead-register-bits", "hw::HWModuleOp"> {
  let summary = "Narrow individual dead bits of otherwise-live seq.firreg "
                "registers (ON by default since verification)";
  let description = [{
    `--enable-bit-narrowing`, ON by default. QoR roadmap Tier-2
    register-count-gap investigation found circt-abc emitting noticeably more
    DFF instances than Yosys for identical ibex RTL, root-caused to something
    circt-abc's flow has NO equivalent for anywhere: Yosys's synthesis-grade
    `opt` does per-bit constant-propagation/dead-bit elimination WITHIN an
    otherwise-live multi-bit register (e.g. ibex CSR registers with
    reserved/unimplemented bit fields, held forever but never read). This is a
    different question than `RemoveDeadRegisters` answers ("is this WHOLE
    register ever read", measured to close only a small part of the gap on its
    own) -- this pass asks "which INDIVIDUAL bits of an otherwise-live register
    are ever read", narrowing only the dead ones.

    Backward per-bit liveness (a Value -> APInt live-bit-mask map), seeded
    from every real `hw.output`/`hw.instance` operand in this module
    (each demanding ALL its own bits), propagated precisely through
    `comb.extract`/`comb.concat`/`comb.mux`/`comb.and`/`comb.or`/`comb.xor`
    -- anything else (arithmetic, comparisons, an unrecognized op) is an
    escape hatch that conservatively demands ALL bits of every operand. A
    firreg's `data` result is a terminal collection point, never
    propagated further into `next`/`clk`/`reset` (irrelevant to "which
    bits are ever READ"), so a self-referencing hold-value cycle can never
    manufacture its own liveness. A second, coarser "opaque" channel
    handles non-integer-typed boundaries (`hw.bitcast`, genuinely present
    on real paths since `hw-aggregate-to-comb` converts aggregate logic
    into bitcast+comb sequences earlier in the pipeline) safely -- any
    firreg reached even partially through it (including any aggregate-
    typed firreg) is excluded from narrowing outright, never narrowed on a
    partial/best-effort basis. When in doubt, a bit (or a whole firreg) is
    treated as LIVE, never dead.

    Scoped per hw::HWModuleOp, deliberately NOT requiring the whole-design
    early flatten `RemoveDeadRegisters` says it needs for cross-module
    reachability (an earlier session attempt to flatten before
    register-mapper hit a real, serious MLIR graph-region false-cycle bug
    and was abandoned) -- a value escaping only through an `hw.instance`
    operand is conservatively treated as fully live at that boundary, a
    real but safe scoping limitation.

    Must run on plain `seq.firreg`s, before register-mapper bit-blasts
    them into opaque `hw.instance`s. See NarrowDeadRegisterBitsPass.cpp's
    file comment for the full design and correctness reasoning.

    **RESULT (ibex_top): an area win** -- 13 firregs narrowed (25 bits
    dropped total) across `ibex_fetch_fifo`, `ibex_load_store_unit`,
    `ibex_top`, `ibex_controller`, `ibex_multdiv_slow`,
    `ibex_prefetch_buffer`. Did NOT fire on the originally-hypothesized
    CSR reserved-bit fields (`dcsr_q`/`mstatus_q`/`cpuctrlsts_part_q` in
    `ibex_cs_registers`) -- those bits are read (with an always-constant
    value) through a wide CSR readback mux, making them LIVE by this
    pass's (correct, conservative) analysis; closing that specific gap
    needs constant propagation through registers, a different
    optimization this pass does not implement. Verified via 4 synthetic
    tests (basic narrowing, a concat/extract round-trip trick case, a
    self-referencing hold-mux trick case, a reset-value-folding case) and
    a real per-module LEC on `ibex_prefetch_buffer` (the pass's own real
    narrowing target) with a control-experiment confirming the LEC's own
    non-convergence is a pre-existing tool limitation, not a bug from
    this pass.
  }];
  let constructor = "circt_abc::createNarrowDeadRegisterBitsPass()";
  let options = [
    Option<"enableBitNarrowing", "enable-bit-narrowing", "bool", "true",
           "Enable per-bit dead-register-bit narrowing -- ON by default "
           "since verification, a higher-risk class of change than this "
           "project's other passes">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def RegisterConstantPropagation : Pass<"propagate-register-constants", "hw::HWModuleOp"> {
  let summary = "Replace a seq.firreg with a literal constant when it is "
                "provably always the same value (ON by default, verified "
                "no-op on ibex_top)";
  let description = [{
    NarrowDeadRegisterBitsPass answers "which bits of an otherwise-live
    register are ever READ" (a liveness question). This pass answers a
    different one: "is this register's VALUE always the same known
    constant, across reset and every reachable update", regardless of
    whether anything reads it -- directly targeting the gap
    NarrowDeadRegisterBitsPass's own file comment documents as real but
    unclosed: ibex's CSR reserved/unimplemented bit fields
    (`dcsr_q`/`mstatus_q`/`cpuctrlsts_part_q` in `ibex_cs_registers`) are
    read through a wide CSR readback mux (so correctly LIVE), but the
    value read is always the same constant.

    Scope: only firregs with an async reset and a literal constant
    resetValue -- exactly register-mapper's own precondition shape, and
    what RegisterLegalizePass leaves every reset-bearing firreg in by this
    point. A firreg with no reset at all has no reliable, independently-
    derived candidate constant and is left untouched. Only plain
    scalar-integer firregs are handled; aggregate-typed ones are
    out of scope, same as NarrowDeadRegisterBits/RemoveDeadRegisters.

    Algorithm: an optimistic, whole-register fixed point. Hypothesize a
    firreg's `data` is always its reset constant C, then try to prove
    `next` also always evaluates to C under that hypothesis, via a
    recursive evaluator understanding the same bit-exact op list
    NarrowDeadRegisterBitsPass's liveness walk does
    (`comb.extract`/`comb.concat`/`comb.mux`/`comb.and`/`comb.or`/
    `comb.xor`, plus any real `hw.constant`) -- anything else (arithmetic,
    comparisons, `hw.bitcast`, an `hw.instance` result, a module input, a
    not-yet-confirmed other firreg) makes the value UNRESOLVED, and an
    unresolved operand anywhere fails the whole evaluation; this pass
    never guesses. A `comb.mux` with an unresolved condition still
    resolves if BOTH legs independently agree on the same constant (the
    common `mux(enable, new_value, <own data>)` hold pattern). Retried in
    rounds to a whole-module fixed point, so one firreg's own confirmation
    can unblock evaluating another's `next` in a later round.

    Three options, all OFF by default, strengthen this and only pay
    together (`CIRCT_FLOW_RCP_SEQUENTIAL=1` sets all three). They exist
    because the version above cannot delete a block whose parent ties it
    off -- CVA6's hardware stride prefetcher, which a commercial flow's
    boundary optimisation removes entirely while we shipped 12 registers
    per instance and 3,366 netlist references to it:

    * `fold-unknown-ops` resolves ANY pure, region-free, single-result
      integer op whose operands have all independently resolved, by asking
      the op's own folder rather than re-deriving the arithmetic here.
      This is what lets a `comb.icmp` state comparison evaluate; the
      prefetcher's state machine is built on them.
    * `absorbing-operands` lets a zero operand of `comb.and` (all-ones of
      `comb.or`) settle the result even when a sibling operand is
      unresolved, which is valid because zero absorbs. The prefetcher arms
      on `and(<its own enable bit>, <throttle comparison>)`, where the
      first operand is zero under the hypothesis and the second depends on
      an unreset register this pass will never resolve.
    * `mutual-fixpoint` replaces the least fixed point with a greatest
      one: assume EVERY candidate holds its reset value, then drop any
      whose `next` does not evaluate to that value under the assumption,
      and repeat until stable. The least fixed point grows from nothing,
      so it can only confirm a register whose proof rests on registers
      already proven, and a group that is only JOINTLY constant never
      bootstraps -- which is exactly the prefetcher's shape, where each
      register is constant because the others are. Sound by induction on
      cycles: every survivor holds its reset constant after reset, and if
      all survivors hold it at cycle k then each survivor's `next`
      evaluates to it at k+1, using nothing but literals and other
      survivors. Strictly stronger than the least fixed point, not an
      alternative to it.

    The greatest fixed point starts by assuming what it wants to prove, so
    it is the direction that needs the most care: a register whose `next`
    cannot be resolved is dropped rather than assumed, and removals are
    batched per sweep so the result does not depend on visit order. See
    `mapper/test/propagate_register_constants_sequential.mlir`, whose
    `@not_constant` and `@mutual_broken` cases are the soundness controls.

    Correctness stance, matching every other pass in this project: when in
    doubt, do NOT propagate -- a false-positive constant here is silent
    mis-synthesis of a real RISC-V core's control logic. See
    RegisterConstantPropagationPass.cpp's file comment for full reasoning,
    including the documented, NOT-yet-attempted per-bit generalization
    (would additionally catch a constant field living inside an otherwise
    genuinely-live wider register) that a whole-register-only version
    cannot reach.

    Must run on plain `seq.firreg`s, before register-mapper bit-blasts
    them into opaque `hw.instance`s.

    RESULT (measured on real ibex_top): a confirmed, formally-verified true
    no-op (byte-identical struct.mlir with the option on vs. off). The
    hypothesized CSR reserved-bit win does not materialize because
    `dcsr_q`/`mstatus_q`/`cpuctrlsts_part_q` live inside reusable
    per-instance leaf modules (`ibex_csr`/`ibex_csr_4`/`ibex_csr_8`) --
    the caller-side fact that only the reserved bit positions are ever
    driven with a constant lives one hierarchy level up, invisible to this
    pass's (deliberately) per-hw.module-scoped analysis. Enabled by
    default anyway: real, SAT-verified-correct infrastructure (Yosys
    equiv_induct: 22/22 $equiv cells proven) with zero measured behavior
    change on this design, left on for whatever design or future
    cross-instance extension benefits from it next.
  }];
  let constructor = "circt_abc::createRegisterConstantPropagationPass()";
  let options = [
    Option<"enablePropagation", "enable-register-constant-propagation",
           "bool", "true",
           "Enable whole-register constant propagation through seq.firreg "
           "-- ON by default (SAT-verified correct, confirmed a true "
           "no-op on real ibex_top)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def DatapathApplyArch : Pass<"dp-apply-arch", "mlir::ModuleOp"> {
  let summary = "Stamp synth.test.arch per dp-annotate op id from an override JSON (PHYS-DP write-back)";
  let constructor = "circt_abc::createDatapathApplyArchPass()";
  let options = [
    Option<"jsonPath", "json", "std::string", "\"\"", "overrides {op id: ARCH}">,
    Option<"minWidth", "min-width", "unsigned", "8", "must match dp-annotate">
  ];
}

def DatapathAnnotate : Pass<"dp-annotate", "mlir::ModuleOp"> {
  let summary = "Sidecar JSON of wide arithmetic ops with launch/capture "
                "register anchors (identity without boundaries)";
  let description = [{
    PHYS-DP step 1 (second design). Outlining bare ops cost ibex area;
    annotation leaves the IR untouched. Registers keep their names
    through the flow, so an op's capture registers identify it in the
    placed design (endpoint slack = budget, placement = neighbourhood).
  }];
  let constructor = "circt_abc::createDatapathAnnotatePass()";
  let options = [
    Option<"jsonPath", "json", "std::string", "\"\"", "sidecar JSON path">,
    Option<"minWidth", "min-width", "unsigned", "8", "annotate ops at/above this width">
  ];
}

def NarrowSparseShifts : Pass<"narrow-sparse-shifts", "mlir::ModuleOp"> {
  let summary = "Rewrite a variable shift whose amount has few non-constant "
                "bits into a mux over the reachable constant shift amounts";
  let description = [{
    Second instance of the blind spot NarrowExtendedMultiplies fixes:
    CombToSynth does not look at where a shift's AMOUNT came from. CVA6's
    `cva6_icache` has six 128-bit `comb.shru` whose amount is
    `concat(c0_i121, s0, s1, c0_i5)` -- provably one of {0, 32, 64, 96}, the
    classic "select a 32-bit word from a cache line at a variable offset".
    Measured: that shift costs 1,788 AIG nodes, BYTE-IDENTICAL to a fully
    variable 128-bit shift, where the equivalent 4:1 mux of aligned slices
    costs 12. A full 7-stage barrel shifter is built and the five stages
    shifting by 1/2/4/8/16 are provably dead. Those six shifts are 23.4% of
    CVA6's entire combinational AIG.

    When the amount is a `comb.concat` whose non-constant operands total at
    most `max-variable-bits` bits, enumerate the 2^n reachable amounts, shift
    by each CONSTANT amount (free -- the lowering turns it into extract +
    concat), and select with a mux tree. Applies to shru / shl / shrs alike.
  }];
  let constructor = "circt_abc::createNarrowSparseShiftsPass()";
  let options = [
    Option<"enable", "enable-narrow-sparse-shifts", "bool", "false",
           "Enable the rewrite (OFF by default)">,
    Option<"maxVariableBits", "max-variable-bits", "unsigned", "3",
           "Only rewrite when the amount has at most this many non-constant "
           "bits (the mux tree is 2^n wide)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def NarrowExtendedMultiplies : Pass<"narrow-extended-multiplies", "mlir::ModuleOp"> {
  let summary = "Rewrite a 2W-bit multiply of sign-extended W-bit operands "
                "into a zero-extended multiply plus a W-bit sign correction";
  let description = [{
    CVA6's `multiplier` holds one i64 `comb.mul` whose operands are both
    `concat(replicate(sign_bit), <i32>)`. CombToSynth does not exploit that:
    measured, an i64 multiply of two sign-extended i32 values costs 22,962
    AIG nodes, BYTE-IDENTICAL to a genuine 64x64 multiply of unconstrained
    i64 inputs -- 4,096 partial products where 1,024 are needed. It does
    exploit a constant-zero high half (12,914 nodes), but CVA6's high halves
    are a replicate of the sign bit gated by the signed/unsigned control, so
    nothing folds.

    With A = concat(replicate(sa), a) == a - sa*2^W (mod 2^2W),
      mul(A, B) = mul(zext a, zext b) - ((sa?b:0 + sb?a:0) << W)
    The W-bit correction may wrap; only its low W bits survive the shift, so
    a plain W-bit add is exact. The rewrite hands the lowering two
    zero-extended operands, the case it already optimises.

    Measured on the 32-bit shape: 22,962 -> 15,200 AIG nodes, -7,762 (-34%),
    a large share of CVA6's combinational excess. Mixed
    signedness (RISC-V `mulhsu`) reduces too -- a zero-extended side simply
    contributes no correction term. Proven by exhaustive combinational SAT at
    4 and 8 bits per half, on top of the width-independent algebra.
  }];
  let constructor = "circt_abc::createNarrowExtendedMultipliesPass()";
  let options = [
    Option<"enable", "enable-narrow-extended-multiplies", "bool", "false",
           "Enable the rewrite (OFF by default)">,
    Option<"minWidth", "min-half-width", "unsigned", "8",
           "Only rewrite when each half is at least this wide">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def SpecializeConstantInputs : Pass<"specialize-constant-module-inputs", "mlir::ModuleOp"> {
  let summary = "Replace a module input every instance drives with the same "
                "literal by that literal inside the body";
  let description = [{
    The inward dual of `prune-unused-residual-outputs`'s
    `propagate-constant-outputs`. cv32a60x ties off the hardware stride
    prefetcher in `cva6.sv` (the upstream RTL flags it `/*FIXME*/`) and the
    tie-off reaches our IR at the cva6 top as `hw.constant 0` and
    `hw.aggregate_constant`, but the block sits three port boundaries down,
    where those signals are block arguments, so nothing propagates it.
    Boundary optimisation deletes the block; without this pass we ship
    3,366 references and ~800 flops of it.

    Callers-first so specialisation cascades. The port is not removed, so
    instance signatures stay valid; canonicalize/CSE and the dead-register
    passes collapse what the constant enables. Roots are skipped. Changes
    what a module computes for inputs it is never given, so it is sound at
    the DESIGN level only: gate with a top-level miter. OFF by default.
  }];
  let constructor = "circt_abc::createSpecializeConstantInputsPass()";
  let options = [
    Option<"enablePass", "enable-specialize-constant-inputs", "bool", "false",
           "Enable the rewrite (OFF by default)">,
  ];
  let dependentDialects = ["hw::HWDialect"];
}

def MapCompressorCells : Pass<"map-compressor-cells", "mlir::ModuleOp"> {
  let summary = "Lower multiply compressor trees to full/half-adder cell "
                "instances";
  let description = [{
    A full adder mapped from the AIG costs two XNOR2s plus a majority gate,
    typically more than the library's own full-adder cell; `&nf` never
    emits a two-output cell, and `&deepsyn` re-derives the tree whatever
    the lowering wrote (Booth, timing-aware, reduce-delay and the adder
    architectures all land within about a percent). This pass runs CIRCT's
    comb -> datapath conversion, lowers the Booth partial products to comb
    while keeping every `datapath.compress`, then replaces each compress
    with a Dadda reduction of FA/HA instances. Cell instances are
    boundaries for outline-residual-logic, so ABC maps only the partial
    products and the final add. OFF by default; see
    MapCompressorCellsPass.cpp.
  }];
  let constructor = "circt_abc::createMapCompressorCellsPass()";
  let options = [
    Option<"enablePass", "enable-compressor-cells", "bool", "false",
           "Enable the rewrite (OFF by default)">,
    Option<"libertyPath", "liberty", "std::string", "",
           "Liberty file holding the full/half-adder cells">,
    Option<"minWidth", "min-width", "unsigned", "16",
           "Only convert designs holding a comb.mul at least this wide">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "datapath::DatapathDialect"];
}

def NarrowDeadOutputBits : Pass<"narrow-dead-output-bits", "mlir::ModuleOp"> {
  let summary = "Zero the output-port bits no instance of a module reads";
  let description = [{
    Per-bit liveness stops at a port. CVA6's TLB hands the MMU a 64-bit PTE;
    the MMU registers it whole and reads 47 of the 64 bits, so inside the
    TLB every one of 16 x 64 content bits looks live and 17 x 16 x 2 = 544
    flops plus their update logic survive. A boundary-optimising flow keeps
    exactly 47 bits per entry; this pass is that boundary optimisation.

    Design-level: modules callers-first; each output port's live mask is the
    OR over all instances of the caller's backward per-bit liveness at that
    instance result (the narrow-dead-register-bits analysis, seeded by the
    caller's own output masks, through registers data -> next and through
    hw.bitcast as a bit identity). Dead bits are forced to zero at the
    callee's `hw.output`; narrow-dead-register-bits then retires what fed
    them. Changes the module's function on unread bits: sound at the design
    level only, gated by top-level miters. Roots keep every bit. OFF by
    default in the flow (`CIRCT_FLOW_DEAD_OUTPUT_BITS=1`).
  }];
  let constructor = "circt_abc::createNarrowDeadOutputBitsPass()";
  let options = [
    Option<"minBits", "min-bits", "unsigned", "1",
           "Only rewrite a port with at least this many dead bits">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def NarrowMuxToDifferingBits : Pass<"narrow-mux-to-differing-bits", "mlir::ModuleOp"> {
  let summary = "Narrow a wide comb.mux to the bits where its arms differ";
  let description = [{
    `convert-comb-to-synth` keeps `synth.aig.and_inv` at the WORD level, so a
    `comb.mux` of width W costs 4*W AIG nodes once `exportAIGER` bit-blasts
    it -- correct when the arms are unrelated, catastrophic for the
    read-modify-write shape a scoreboard write produces:

    ```
    %n    = comb.concat extract(%v, p+1, W-p-1), %true, extract(%v, 0, p)
    %next = comb.mux %en, %n, %v : iW
    ```

    One bit set under a condition, expressed as a W-bit mux. On cv32a60x's
    scoreboard W is 1087 and the chain is 22 deep. The AIGER exporter's
    structural hashing cannot recover it: where both arms are the same
    literal `x` the mux still builds AND(c,x), AND(!c,x) and their OR, three
    nodes only provably equal to `x` by Boolean reasoning -- which is ABC's
    job, and which ABC then spends its whole per-group budget doing. Measured
    on cv32a60x: group g30 (the scoreboard) exports a 68,254-AND AIGER, 24%
    of the design's entire 282,487-node pre-optimisation total.

    This cuts both arms at the concat's boundaries and rebuilds
    `concat(mux(c, a[i], b[i]) ...)`. `comb.extract` of a `comb.concat` folds
    to the concat operand, so the a-side slices are free, and wherever a
    slice of `a` IS the matching slice of `b` the mux disappears. Applied
    only when at least one slice folds away; otherwise it is the same total
    mux width in more ops, and is skipped.

    Function-preserving by construction: every output bit is either the mux
    it already was, or a value both arms agreed on.
  }];
  let constructor = "circt_abc::createNarrowMuxToDifferingBitsPass()";
  let options = [
    Option<"enable", "enable-narrow-mux", "bool", "false",
           "Enable the rewrite (OFF by default)">,
    Option<"minWidth", "min-width", "unsigned", "8",
           "Only rewrite muxes at least this wide">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def SpecializeMuxArms : Pass<"specialize-mux-arms", "mlir::ModuleOp"> {
  let summary = "Rewrite each comb.mux arm under its select's value";
  let description = [{
    Inside `mux(c, a, b)`, `a` is only observed when c = 1 and `b` only when
    c = 0. llhd-remove-control-flow lowers an always_comb join by incoming-
    edge predicates, which leaves arms like `and(x, or(!gnt, ..., c))` -- a
    tautology under the arm's own select that comb canonicalisation never
    sees, but a STRUCTURAL dependence on `gnt`. CVA6's load_unit has two
    (data_req and translation_req_o on data_gnt), and in an SoC with an
    hpdcache they close a false combinational loop through the arbiter.

    For each arm, walk back through comb logic (bounded by max-depth and
    max-nodes) and replace values decided by the select -- the select
    itself, the operands of an AND select that is 1 / an OR select that is
    0, the input of an inverting XOR, and `icmp eq/ne v, K'` once the select
    fixes `v == K` -- with constants, cloning only the ops that change.
    Function-preserving by construction. Run canonicalize afterwards.
  }];
  let constructor = "circt_abc::createSpecializeMuxArmsPass()";
  let options = [
    Option<"enable", "enable", "bool", "false", "Enable the rewrite">,
    Option<"maxDepth", "max-depth", "unsigned", "8", "Levels searched per arm">,
    Option<"maxNodes", "max-nodes", "unsigned", "512", "Ops visited per arm">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def SelectChainPrefixOr : Pass<"select-chain-prefix-or", "mlir::ModuleOp"> {
  let summary = "Collapse a pass-through mux select chain into a prefix OR";
  let description = [{
    A daisy chain whose stages forward their input when it is "taken"
    (e.g. a systolic-array PE row:
    `out_k = in_k.valid & !in_k.mac_read ? in_k : (pipe_valid ? pipe : 0)`)
    computes each stage's select p_{k+1} from the previous stage's mux
    output, so the selects form a serial chain that grows by a few cells
    per stage.

    Under p_k = 1 the mux forwards in_k, whose valid/mac_read bits p_k was
    computed from, so p_{k+1} = p_k | q_k with q_k a function of the mux's
    other arm only; unrolled, p_k = p_0 | q_0 | ... | q_{k-1}.

    For each i1 mux select X that is an and/or/xor cone (max-depth,
    max-nodes) reading a comb.mux M through single-bit extracts, Shannon-
    expand X on M's select s and evaluate the true cofactor under s = 1 and
    its CombFacts implications, through concat/extract/decided muxes. If it
    is a constant c, X becomes `c ? s | X0 : !s & X0` (X0 = X with M := its
    false arm); else X is untouched. Pass-made ORs are then flattened into
    one n-ary OR (up to max-or-width operands): the prefix OR.
    Function-preserving by construction. Needs the chain inside one module:
    run after merge-residual-modules with internalize-self-fed-ports.

    Measured: NOT a lever, off by default. internalize-self-fed-ports
    alone already lets ABC find p_{k-1} | q; this pass adds nothing after
    ABC, which re-shares the flattened ORs back into the chain. Where such
    a path is wire-bound, the fix is a register in the RTL.
  }];
  let constructor = "circt_abc::createSelectChainPrefixOrPass()";
  let options = [
    Option<"enable", "enable", "bool", "false", "Enable the rewrite">,
    Option<"maxDepth", "max-depth", "unsigned", "4", "Levels searched per select">,
    Option<"maxNodes", "max-nodes", "unsigned", "32", "Ops per select cone">,
    Option<"maxOrWidth", "max-or-width", "unsigned", "64", "Operands of a flattened prefix OR">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def NarrowWholeValueReads : Pass<"narrow-whole-value-reads", "mlir::ModuleOp"> {
  let summary = "Push comb.extract through merged words inside combinational "
                "SCCs (breaks the word-level false cycles sig2reg/mem2reg create)";
  let description = [{
    Direction 0 root cause: the RTL has no combinational loop;
    llhd-sig2reg / mem2reg merge every drive to a partially-driven signal
    into one word and resolve every slice read as an extract of it, so a
    tree `a[k] = f(a[2k+1], a[2k+2])` (common_cells lzc, rr_arb_tree) or a
    field-wise always_comb becomes `word = f(word)`: a word-level SCC that
    is acyclic per bit. Every feedback-cut op of the shipped cv64a6 flow
    (all 3,724 `u_shell_` cells, on the sound critical paths 5-12 times
    each) is this. Inside every non-trivial SCC the pass pushes extracts
    through n-ary and/or/xor, mux, concat, constant shifts and same-width
    bitcast pairs to the slice drivers, folds, and repeats. Modules without
    word-level cycles are untouched. Runs before outline-residual-logic.
  }];
  let constructor = "circt_abc::createNarrowWholeValueReadsPass()";
  let dependentDialects = ["circt::comb::CombDialect", "circt::hw::HWDialect"];
}

def LocalizeDynamicRmw : Pass<"localize-dynamic-rmw", "mlir::ModuleOp"> {
  let summary = "Rewrite dynamic-index read-modify-write array injects into "
                "array_create of per-entry muxes over constant-index reads";
  let description = [{
    The RTL idiom `mem_n[idx].field = f(mem_n[idx].field)` reaches the
    mapper as `array_inject %a[%i], g(array_get %a[%i])`; hw-aggregate-to-comb
    lowers it per entry k to `mux(i == k, g(muxtree(a, i)), a[k])`, where
    `muxtree(a, i) == a[k]` only under the select -- an observability
    don't-care ABC's SAT sweeping cannot use (it inflates the CVA6
    scoreboard's `ex` cone several-fold under the structured join
    lowering). Before de-aggregation this pass rewrites every such inject
    (non-constant index, element cone reaching a read of the same array SSA
    value at the same index SSA value) into `array_create` of
    `mux(i == k, g_k(a[k]), a[k])`, cloning only the read-dependent ops of g
    per entry; the rest of g stays shared. Runs right before the stage-1
    `--hw-aggregate-to-comb`. Skips injects whose dependent cone exceeds
    max-cone-ops, whose N x cone exceeds max-clone-ops, single-entry arrays,
    and cones holding instances / regions / side effects.
  }];
  let constructor = "circt_abc::createLocalizeDynamicRmwPass()";
  let options = [
    Option<"maxConeOps", "max-cone-ops", "unsigned", "64",
           "skip an inject whose read-dependent element cone has more ops">,
    Option<"maxCloneOps", "max-clone-ops", "unsigned", "4096",
           "skip an inject when array entries x cone ops exceeds this">
  ];
  let dependentDialects = ["circt::comb::CombDialect", "circt::hw::HWDialect"];
}

def OutlineDatapathOps : Pass<"outline-datapath-ops", "mlir::ModuleOp"> {
  let summary = "Outline each wide arithmetic comb op into its own residual "
                "module (per-op identity, ABC group, architecture choice)";
  let description = [{
    PHYS-DP step 1: add/sub/mul, ordered icmp, variable shifts at or above
    min-width become `<module>_dp<K>_<op>_residual` instantiated as
    `dp<K>`, before outline-residual-logic. Constant-operand ops are left
    alone. Gives a physical loop per-op identity in the netlist/DEF and
    lets it re-lower and re-map one op's group alone.
  }];
  let constructor = "circt_abc::createOutlineDatapathOpsPass()";
  let options = [
    Option<"minWidth", "min-width", "unsigned", "8",
           "outline ops whose operand width is at least this">
  ];
  let dependentDialects = ["comb::CombDialect", "hw::HWDialect"];
}

def PruneUnusedResidualOutputs : Pass<"prune-unused-residual-outputs", "mlir::ModuleOp"> {
  let summary = "Erase residual output ports whose single instance result is "
                "unused, so dead cones can be removed before ABC";
  let description = [{
    Residuals are outlined per RTL module before flattening, so outputs the
    hierarchy never consumes still reach ABC as primary outputs of the
    merged AIG and keep their cones. Runs after hw-flatten-modules, before
    merge-residual-modules; function-preserving (removes only unread
    values). ibex flow6: 56 of 1,386 residual outputs / 484 bits unused.
  }];
  let constructor = "circt_abc::createPruneUnusedResidualOutputsPass()";
  let options = [
    Option<"anyModule", "any-module", "bool", "false",
           "Prune unused outputs of ANY private module, not just *_residual "
           "ones. The liveness argument is identical; the name filter was a "
           "scoping choice. CVA6: the prefetcher's four CSR readback outputs "
           "on cva6_hpdcache_subsystem are never read, and they are the only "
           "escape for hwpf_stride.csr_throttle_q (reset-less, 32 bits x4), "
           "so pruning them kills it by liveness instead of X-optimism.">,
  ];
  let dependentDialects = ["hw::HWDialect"];
}

def SetAdderArch : Pass<"set-adder-arch", "hw::HWModuleOp"> {
  let summary = "Stamp synth.test.arch on comb.add/comb.icmp so CombToSynth "
                "uses the chosen adder architecture (area knob)";
  let description = [{
    CombToSynth picks ripple (<8 bits), Sklansky (<=32) or Kogge-Stone (>32) by
    width; the parallel-prefix choices cost area that ripple or Brent-Kung do
    not. A per-module census localized circt's remaining combinational excess vs
    the Yosys reference to the wide-adder modules. Empty arch = no-op
    (byte-identical).
  }];
  let constructor = "circt_abc::createSetAdderArchPass()";
  let options = [
    Option<"arch", "arch", "std::string", "\"\"",
           "RIPPLE-CARRY | SKLANSKEY | BRENT-KUNG | KOGGE-STONE">,
    Option<"minWidth", "min-width", "unsigned", "8",
           "only stamp adds/compares at or above this width">
  ];
  let dependentDialects = ["comb::CombDialect", "hw::HWDialect"];
}

def LowerAggregateRegisters : Pass<"lower-aggregate-registers", "hw::HWModuleOp"> {
  let summary = "Rewrite aggregate-typed seq.firregs as integer-typed ones "
                "(bitcasts + packed constant reset) so async resets with "
                "aggregate-constant values survive register-legalize";
  let description = [{
    RegisterLegalize accepts a reset only when its value matches
    m_ConstantInt; an aggregate register's hw.aggregate_constant reset is
    treated as non-constant and the ASYNC reset is folded into D-logic,
    i.e. silently made synchronous (49 registers on cv32a60x; caught by
    a whole-design miter with a free reset on rvfi_probes_o).
    This pass runs after narrow-array-registers and before
    register-legalize and packs the reset with hw::aggregateAttrToAPInt,
    CIRCT's own convention. Registers with a preset or a non-aggregate-
    constant reset value are left alone. ibex: one reset-less struct
    register lowered, area-neutral.
  }];
  let constructor = "circt_abc::createLowerAggregateRegistersPass()";
  let dependentDialects = ["hw::HWDialect", "seq::SeqDialect"];
}

def NarrowConstantRegisterBits : Pass<"narrow-constant-register-bits", "mlir::ModuleOp"> {
  let summary = "Per-bit register constant propagation across single-instance "
                "module boundaries (direction 2: the CSR reserved/read-only "
                "bits)";
  let description = [{
    The per-bit, cross-instance generalization RegisterConstantPropagation's
    description names as "NOT yet attempted". A known-bits lattice is
    evaluated through constant/extract/concat/replicate/mux/and/or/xor, and
    additionally through a block argument or an hw.instance result of a
    module that has exactly ONE instance in the design (the caller's
    operand, resp. the callee's hw.output operand). Every reset-literal
    integer firreg starts with all bits hypothesized equal to its reset
    value; a design-wide greatest fixed point drops each bit whose `next`
    is not provably that reset bit. All-constant registers become literals,
    partially-constant ones are narrowed to their variable bits (fresh
    firreg <name>_cbits, uses reconstructed as a concat of literal bits and
    extracts). Port widths never change. Motivation: a register census --
    ibex_cs_registers holds ~41 flops Yosys
    proves constant (dcsr 32 vs 9, mtvec 32 vs 25, cpuctrlsts 8 vs 2,
    mstatus 6 vs 3) because the caller drives the CSR leaf's write port
    with literal-constant bit positions and the leaf holds through its own
    read-back.
  }];
  let constructor = "circt_abc::createNarrowConstantRegisterBitsPass()";
  let options = [
    Option<"enable", "enable-narrow-constant-register-bits", "bool", "true",
           "Enable the pass (ON by default)">,
    Option<"multiInstance", "cross-multi-instance", "bool", "false",
           "Resolve a module input by MEETING the known bits of every "
           "instance's operand, instead of requiring the module to have "
           "exactly one instance (OFF by default). Sound because every fact "
           "this analysis derives lives on a Value in a module DEFINITION "
           "and the rewrite it drives is applied to that definition, so the "
           "meet is exactly the condition making the fact true for all "
           "instances; a zero-instance module (the top) keeps unknown "
           "inputs. cv32a60x: 771 -> 1046 constant bits, all of it "
           "hwpf_stride's CSR registers (4 instances, csr_*_set_i tied off "
           "from above cva6_hpdcache_wrapper) -- which is also why only a "
           "TOP-LEVEL miter can gate it.">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def DecodeMuxChains : Pass<"decode-mux-chains", "hw::HWModuleOp"> {
  let summary = "Rewrite priority mux chains with structurally mutually "
                "exclusive conditions into a decoded one-hot AND-OR select";
  let description = [{
    The $pmux form Yosys emits for `case` statements. CIRCT lowers a case
    to a chain of 2:1 muxes (~3 AIG nodes per link per bit, and the chain
    survives ABC's dc2); the decoded form is ~2 nodes per arm per bit with
    the decode shared. Applied only when every pair of chain conditions is
    structurally exclusive (a shared icmp-eq key with different constants,
    eq vs ne of the same constant, or the same i1 in opposite polarity)
    and the chain has at least `min-chain` links. Motivated by a
    per-module census against Yosys that found the decoder modules the
    furthest above it after dc2.
  }];
  let constructor = "circt_abc::createDecodeMuxChainsPass()";
  let options = [
    Option<"minChain", "min-chain", "unsigned", "4",
           "Minimum number of chain links to rewrite (0 = off)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def NarrowArrayRegisters : Pass<"narrow-array-registers", "hw::HWModuleOp"> {
  let summary = "Narrow an array-typed seq.firreg observed only through one "
                "constant bit-slice down to a register of just that slice "
                "(OFF by default, unpromoted)";
  let description = [{
    Targets the first source of this flow's register-count gap against
    Yosys: a generate-loop with one
    `always_ff` per entry of an array variable lowers to one FULL-ARRAY
    `seq.firreg` per loop iteration -- ibex_fetch_fifo's 3-entry `rdata_q`
    becomes three 96-bit `!hw.array<3xi32>` registers (288 netlist DFFs
    where Yosys produces 96), ibex_id_stage's `imd_val_q[2]` two 68-bit
    ones (+260 dead DFF bits total, most of the itemized register gap vs.
    Yosys). Each copy is only observed at its own entry's slice of its
    integer bitcast; `narrow-dead-register-bits` cannot catch this because
    its per-bit liveness treats `hw.bitcast` as an all-bits-live escape
    hatch (its integer-typed sibling `err_q` IS already narrowed by that
    pass, confirming the bitcast hatch is the blocker).

    Pattern (verified against real `_core_deagg.mlir`): an array firreg
    whose single use is a bitcast to iTotal, whose next is
    bitcast(mux(en, W, <that same bitcast>)), and whose bitcast is
    otherwise only read by comb.extract ops at ONE constant strict-subset
    slice. Rewritten to a plain iW register of the owned slice; correct
    because the hold operand is bit-diagonal and all other bits are
    syntactically unobservable -- guarded by an explicit bounded backward
    walk proving the enable/write/reset cones never read the register's
    dead bits (rejecting the register entirely otherwise). Must run
    BEFORE register-legalize, on plain seq.firreg ops.
  }];
  let constructor = "circt_abc::createNarrowArrayRegistersPass()";
  let options = [
    Option<"enableArrayNarrowing", "enable-array-register-narrowing",
           "bool", "true",
           "Enable array-register slice narrowing -- ON by default, "
           "promoted with explicit sign-off after full diligence "
           "(BMC depths 2/6/10 with/without the pass on the fixed "
           "frontend, and P&R: smaller area and power, WNS-neutral, "
           "with some TNS as the one accepted cost)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def DedupRegisters : Pass<"dedup-registers", "hw::HWModuleOp"> {
  let summary = "Merge exact-duplicate seq.firreg replicas (identical "
                "next/clock/reset modulo self-reference) -- OFF by default, "
                "unpromoted";
  let description = [{
    Targets the second source of this flow's register-count gap against
    Yosys: deliberate RTL register
    replication that Yosys's `opt_merge` undoes (+48 DFF bits on real
    ibex_top -- ibex_if_stage's `instr_rdata_alu_id_o`, a comment-
    documented fanout-reduction replica of `instr_rdata_id_o`, and
    `instr_expanded_id_o`, bit-identical to `instr_rdata_c_id_o`).

    Matching subtlety (why a naive same-next-SSA hash can never fire):
    each replica's next is a hold-mux referencing its OWN register, so the
    next values are always different SSA values. Two registers are
    duplicates when their nexts are structurally identical modulo
    substituting each register's own result for the other's -- implemented
    as a bounded conservative walk through pure ops (comb + hw.constant)
    where the only permitted non-literal leaf match is (A's result, B's
    result) in that one orientation; cross-coupled registers deliberately
    do NOT match. Requires same type/clock/reset/preset and no inner
    symbol on the duplicate. When in doubt, doesn't merge.

    CAUTION: merging deliberately UNDOES an intentional RTL timing
    optimization (fanout reduction on a wide instruction bus) -- adopt
    only with real post-P&R timing data. Initial-state caveat for
    no-reset registers is shared with Yosys opt_merge (sound in the
    2-state model every check in this project uses; Yosys's published
    baseline netlist already ships the merged form). Must run on plain
    `seq.firreg`s, before register-mapper.
  }];
  let constructor = "circt_abc::createDedupRegistersPass()";
  let options = [
    Option<"enableDedup", "enable-register-dedup", "bool", "false",
           "Enable duplicate-register merging -- OFF by default "
           "(unpromoted)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def MapShellLogic : Pass<"map-shell-logic", "mlir::ModuleOp"> {
  let summary = "Map post-abc-bridge leftover comb ops (the outliner's "
                "feedback-cut mux/and/or/xor) to liberty cells per bit";
  let description = [{
    `outline-residual-logic` cuts a greedy feedback vertex set so each
    flattened residual module is acyclic; the cut ops stay in the parent
    shell (they read residual outputs and feed residual inputs). On ibex
    the cut landed on pure wiring; on CVA6 it landed on 4 muxes + 1 or --
    real logic, which ExportVerilog emits as `?:`/`|` operator
    expressions and OpenSTA's structural Verilog reader rejects with a
    syntax error at floorplan time. This pass bit-blasts each leftover
    mux/and/or/xor and maps every bit through LibertyLibrary's
    cheapest-cell truth-table lookup (no hardcoded cell names); an
    output-inverted match gets a real INV-class cell, never a comb.xor.
    Any other leftover comb op class is reported loudly and left alone.
    Runs after abc-bridge, alongside remove-identity-buffers.
  }];
  let constructor = "circt_abc::createMapShellLogicPass()";
  let options = [
    Option<"libertyPath", "liberty", "std::string", "",
           "Liberty file used to look up mux/and/or/xor/inv cells by "
           "truth table -- same file abc-bridge maps against">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def LimitFanout : Pass<"limit-fanout", "mlir::ModuleOp"> {
  let summary = "Cap net fanout on the mapped netlist with a balanced "
                "buffer tree, touching only nets above the cap";
  let description = [{
    Without a fanout limit, the worst nets of a mapped netlist carry
    hundreds of loads where a commercially synthesised netlist of the same
    design stays below a hundred, because commercial synthesis applies a
    max-fanout constraint and neither ABC nor emap applies one at all.
    OpenROAD then builds those buffer trees during `repair_design`, which
    shows up as area added at placement.

    ABC's own `buffer` was measured and rejected as too blunt: it adds
    about the same area at caps of 16, 64 and 128, nearly independent of
    the cap, because its gain target resizes the whole netlist. This pass
    touches only the offending nets, so its cost is ~ceil(loads/cap)
    buffers per net.

    Clock nets are skipped (a logic buffer in a clock path is CTS's job
    and a skew hazard). The buffer cell is the liberty's cheapest
    single-input identity cell, never a hardcoded name. MUST run after
    `remove-identity-buffers`, which would otherwise delete every buffer
    inserted here. OFF by default per the project convention.
  }];
  let constructor = "circt_abc::createLimitFanoutPass()";
}

def ResubMapped : Pass<"resub-mapped", "mlir::ModuleOp"> {
  let summary = "[CLOSED, measured worse] Technology-aware resubstitution "
                "on the mapped netlist";
  let description = [{
    **CLOSED. Measured slightly worse than the baseline.** Invoked by
    nothing; kept so the negative result stays reproducible. The number is
    real rather than an artefact of a missing database: it was obtained
    only after `scripts/gen_resub_db.py` supplied the full 3,984-class
    table, and that generator has no other consumer. An earlier assessment
    called this "the single largest remaining lever"; that was wrong. A
    cell-merge headroom study independently closed the same question: the
    headroom is a few hundredths of a percent, i.e. the cover is locally
    optimal.

    Implements the scalable core of ASP-DAC'25 "Area-Oriented Optimization
    After Standard-Cell Mapping" (section 3.3, Structural Network
    Exploration): enumerate structural cuts over the MAPPED graph, extract
    each cut's functionality, look it up in a database of mapped
    sub-networks, and substitute when the database entry is cheaper than
    the sub-circuit inside the cut.

    It finds what the mapper could not because the mapper enumerates cuts
    of the AIG and the emitted netlist inherits that covering -- the
    paper's *structural bias*. Cuts of the mapped graph group logic along
    different boundaries, so a two- or three-cell sub-circuit here can
    turn out to be a single library cell.

    Deviations from the paper, deliberate and documented in the source:
    the "database" is LibertyLibrary::lookup(), i.e. one-cell netlists
    rather than the paper's area-optimal 4-input P-class database; don't
    cares (paper 3.2) are not used, so every substitution is exact on the
    completely specified cut function; and dependency cuts (paper 3.4),
    the non-local half, are absent. Expect part of the reported 5.47%.

    Substitutions are taken only when they STRICTLY reduce area, with an
    inverted match paying for its inverter first. OFF by default per the
    project convention; CEC-gate it like any other netlist change.
  }];
  let constructor = "circt_abc::createResubMappedPass()";
}

def FuseHalfAdders : Pass<"fuse-half-adders", "mlir::ModuleOp"> {
  let summary = "[CLOSED, superseded] Replace an AND2+XOR2 pair over "
                "the same two operands with one two-output half-adder cell";
  let description = [{
    **CLOSED. Measured as noise and superseded; not in the flow and
    invoked by nothing.** Kept so the negative result stays reproducible.
    This was the wrong end of the problem -- recovering half-adders after
    the AIG has already flattened them, rather than never flattening them.
    Its successors do the latter from the operator level:
    `map-adder-chains` (`CIRCT_FLOW_ADDER_CELLS`) and `map-compressor-cells`
    (`CIRCT_FLOW_MUL_CELLS`). Mapped sums do not pair with their carries,
    which caps this approach at a tiny design-wide gain even in principle.

    ABC never emits a half-adder cell, and cannot: a half adder has two
    outputs (`CO = A&B`, `S = A^B`), and every ABC cell-set builder
    (`Mio_CollectRoots`, `Mio_CollectRootsNew`, `Mio_CollectRootsNew2`)
    carries `if (pGate->pTwin) // skip large and multi-output`. `&nf`
    reaches its cell set through `Mio_CollectRootsNewDefault2` and matches
    single-output cuts, so a two-output cell can never be a cut's match,
    and a netlist mapped by `&nf` contains no half- or full-adder cells.

    The substitution is a pure re-encoding of two gates the mapper already
    chose -- the AND2's result becomes CO, the XOR2's becomes S, both read
    the same two nets -- so equivalence is structural, not a claim needing
    SAT. The pass refuses any substitution that does not strictly reduce
    area, so a liberty with no cheaper half adder is a silent no-op. Cell
    roles come from the liberty (exact truth tables 0b1000 and 0b0110, and
    LibertyLibrary's cheapest two-output half adder), never a name list.
    OFF by default per the project convention.
  }];
  let constructor = "circt_abc::createFuseHalfAddersPass()";
}

def MapAdderChains : Pass<"map-adder-chains", "mlir::ModuleOp"> {
  let summary = "Lower a wide comb.add into a ripple chain of the library's "
                "two-output half/full-adder cells";
  let description = [{
    ABC's `&nf` can never emit an adder cell: `Mio_CollectRootsNew2`
    (mioUtils.c) skips every gate with a `pTwin`, i.e. every multi-output
    cell, so the whole full/half-adder family is invisible to the mapper.
    The emap path does support two-output cells, but only finds an adder
    where the cut boundaries happen to line up, so part of a carry chain
    is covered by adder cells and the remaining bits cost two to three
    generic gates each.

    Emits a plain ripple, deliberately: it is the structure a full-adder
    cell exists to serve (its CI->CO arc is typically the fastest carry
    path in a library), and a parallel-prefix form cannot use two-output
    adder cells at all. Constant-operand adds are skipped -- an increment
    is not a two-addend carry chain. Cell roles come from the liberty
    (exact truth tables 0x96 and 0xE8 over three inputs), never a name
    list. OFF by default per the project convention.
  }];
  let constructor = "circt_abc::createMapAdderChainsPass()";
}

def RemoveIdentityBuffers : Pass<"remove-identity-buffers", "mlir::ModuleOp"> {
  let summary = "Remove hw.instance ops of identity-function (buffer) "
                "liberty cells, rewiring loads to the buffer input";
  let description = [{
    The promoted-default netlist carries well over a thousand buffer
    instances where the Yosys reference netlist ships ZERO buffer cells
    pre-P&R
    -- an artifact of the outline/merge port architecture (ABC must drive
    every merged-residual-module boundary port with a distinct net; after
    `--hw-flatten-modules` erases those boundaries the buffers survive as
    pure logical identities that canonicalize cannot see through, since
    they are opaque `hw.instance` ops of `hw.module.extern` cells).

    Removing an identity-function instance and rewiring its loads to its
    input is a logical no-op; the electrical (drive-strength) role belongs
    to OpenROAD's own resizer downstream, which re-inserts buffers where
    real placement/loading demands them -- exactly the Yosys reference
    methodology. Identity cells are derived FROM THE LIBERTY FILE (arity-1
    combinational cells whose truth table is the identity), not from a
    name pattern. Conservative guards: the instance must reference a real
    `hw.module.extern`, have exactly one operand/one result of identical
    type, and carry no inner symbol. Runs after abc-bridge, before the
    final flatten/export.
  }];
  let constructor = "circt_abc::createRemoveIdentityBuffersPass()";
  let options = [
    Option<"libertyPath", "liberty", "std::string", "",
           "Liberty file used to derive the identity-function (buffer) "
           "cell set -- same file abc-bridge maps against">,
    Option<"enableRemoval", "enable-identity-buffer-removal", "bool",
           "true",
           "Enable identity-buffer removal -- ON by default, promoted "
           "with explicit sign-off (BMC d2 SUCCESS + identity-by-"
           "construction; P&R verified on two different real netlist "
           "instances, 2 clean runs each: smaller signed-off area and "
           "power than the baseline; post-P&R WNS varies with the "
           "ABC-noise netlist instance -- the accepted cost axis)">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect"];
}

def ClockGateInference : Pass<"clock-gate-inference", "mlir::ModuleOp"> {
  let summary = "Convert shared-enable seq.firreg groups into a gated-clock "
                "DFF with no hold-mux, using a real ICG cell";
  let description = [{
    ON by default (`--enable-clock-gating`). A cell-usage comparison
    against a Yosys-synthesized ibex_top netlist found circt-abc's flow
    using several times more discrete mux cells and far fewer clock-gate
    cells -- traced to Yosys's `synth.tcl` running a dedicated `clockgate`
    inference pass on plain enable-conditioned registers with NO
    pre-existing RTL-level clock gating, which nothing in this flow's
    pipeline does: RegisterMapper always bit-blasts whatever `next` shape
    it is given into a plain always-clocked DFF, so an RTL idiom like
    `always @(posedge clk) if (en) q <= d;` keeps its
    `next = mux(en, d, q)` hold-value mux forever.

    Walks each hw.module's seq.firreg ops looking for exactly that
    self-referencing hold-mux shape (either cond polarity), groups matches
    by (clock, canonicalized active-high-load-enable), and -- for any
    group whose total bit width meets `--min-group-width` (default 2, the
    rounded-up area breakeven between a typical cheapest ICG cell and a
    per-bit hold-mux) -- instantiates ONE shared instance of whichever
    real posedge-precontrol integrated clock-gating cell is cheapest
    across the given `--liberty=` file(s) (see LibertyLibrary.h's
    ICGCellPattern) gating that clock, then rebuilds every firreg in the
    group with the hold-mux gone (`next` = the bare load value) and `clk`
    = the gated clock. Reset/resetValue/isAsync/preset are carried over
    unchanged (an async clear/preset pin is independent of the clock
    signal). Pass ALL of the design's liberty files (not just the first,
    which is all RegisterMapper/StructuralMapper need): the cheapest
    candidate may live in a different file than the default, and may not
    be the cell the design's own RTL clock-gate wrapper uses.

    Must run after RegisterLegalize (so a sync-reset firreg's `next` is
    already normalized) and before RegisterMapper (which must see the
    already-gated clock and hold-mux-free `next` directly). See
    ClockGateInferencePass.cpp's file comment for the full design and
    known coverage gaps (aggregate-typed firregs, and the enable pattern
    hidden under a sync-reset wrapper, are both left ungated rather than
    mishandled).

    **RESULT (ibex_top): an area win** -- 72 firregs gated into 57 shared
    ICG cells. The core transformation rule was proven via Yosys
    `equiv_induct` sequential induction on a synthetic test using a real
    flip-flop cell and a liberty-derived behavioral model, run against two
    functionally identical ICG cells with identical results -- neither
    real cell has a SAT model the generic checker can use directly. Real
    per-module LEC was also attempted on two actual gated submodules
    (`ibex_load_store_unit`, `ibex_compressed_decoder`) -- both hit
    pre-existing `equiv_induct` limitations (induction-depth give-up;
    base-case divergence), each confirmed via a control run (baseline
    netlist, no clock-gate-inference) to fail identically, so neither is
    attributable to this pass. This pass groups less aggressively than
    Yosys's own `clockgate` pass does (exact-SSA-value enable matching
    only).
  }];
  let constructor = "circt_abc::createClockGateInferencePass()";
  let options = [
    Option<"enableClockGating", "enable-clock-gating", "bool", "true",
           "Enable clock-gating inference -- ON by default, see this "
           "pass's description for why">,
    Option<"libertyPath", "liberty", "std::string", "",
           "Comma-separated list of liberty file(s) to search for "
           "the cheapest real posedge-precontrol integrated clock-gating "
           "cell (required when clock-gating is enabled)">,
    Option<"minGroupWidth", "min-group-width", "unsigned", "2",
           "Skip groups whose total bit width is below this">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect", "seq::SeqDialect"];
}

def RegisterMapper : Pass<"register-mapper", "mlir::ModuleOp"> {
  let summary = "Bit-blast seq.firreg ops to real liberty flip-flop cells";
  let description = [{
    Maps every `seq.firreg` (after RegisterLegalize has normalized resets
    to "none" or "async + constant") to real liberty flip-flop cells,
    one 1-bit cell instance per bit of the register's width: a clock-only
    cell for bits with no reset, an async-clear cell for bits whose
    constant resetValue bit is 0, and an async-preset cell for bits whose
    constant resetValue
    bit is 1 -- selected generically from the liberty file's `ff()` groups
    (see LibertyLibrary::lookupFF*()), not hard-coded to specific cell
    names. Per-bit D inputs come from `comb.extract`ing the firreg's
    `next` operand; the clock pin is fed by a single shared
    `seq.from_clock` cast of the firreg's `clk`; the reset/set pin is
    wired directly to the firreg's (always active-high, by seq.firreg's
    own convention) `reset` operand for clear-class cells, or through a
    `comb.xor`-based inverter for preset-class cells (whose SN pin is
    active-low). The per-bit Q outputs are reassembled (MSB-first
    `comb.concat`) into the register's original `data` result, and the
    `seq.firreg` is erased.
  }];
  let constructor = "circt_abc::createRegisterMapperPass()";
  let options = [
    Option<"libertyPath", "liberty", "std::string", "",
           "Path to the liberty file whose ff() groups supply the "
           "plain/async-clear/async-preset flip-flop cells to map to">,
  ];
  let dependentDialects = ["hw::HWDialect", "comb::CombDialect",
                           "seq::SeqDialect"];
}

#endif // CIRCT_ABC_PASSES_TD
