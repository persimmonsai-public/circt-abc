"""Pipeline stages for the CIRCT-native synthesis flow.

Each function here is a direct Python port of one step of
scripts/run_circt_native_synth.sh -- same tool, same flags, same
file paths/naming. See that script's header comment for the full
pipeline rationale (register-legalize, clock-gate-inference,
narrow-dead-register-bits, register-mapper, structural-mapper,
outline-residual-logic, hw-flatten-modules, merge-residual-modules,
abc-bridge, and the final OpenSTA-compatibility post-processing).

Every stage shells out via `subprocess.run(..., check=True)`: a failing
tool invocation raises `subprocess.CalledProcessError` (command + return
code in the exception; the subprocess's own stderr/stdout stream straight
through to the caller's terminal exactly as it did under the bash `set -e`
script) instead of silently continuing.
"""
from __future__ import annotations

import re
import shutil
import os
import sys
import subprocess
from pathlib import Path
from typing import Sequence

from .config import FlowConfig


def _run(cmd: Sequence[str | Path], *, stdout: Path | None = None, env=None) -> None:
    """Run `cmd`, optionally redirecting stdout to the file at `stdout`
    (mirroring bash's `> file`). Raises subprocess.CalledProcessError on a
    non-zero exit, with the full argv attached."""
    str_cmd = [str(c) for c in cmd]
    if stdout is not None:
        with open(stdout, "w") as f:
            subprocess.run(str_cmd, stdout=f, env=env, check=True)
    else:
        subprocess.run(str_cmd, env=env, check=True)


def _binary_has_pass(binary: str | Path, pass_name: str) -> bool:
    """True if `binary --help` lists `--<pass_name>` (an opt tool prints
    every registered pass there). Lets a flow stage degrade to the previous
    behaviour instead of failing on a circt-abc-opt that predates a pass."""
    try:
        out = subprocess.run([str(binary), "--help"], capture_output=True,
                             text=True).stdout
    except OSError:
        return False
    return f"--{pass_name}" in out


# ---------------------------------------------------------------------------
# Stage 0: frontend + LLHD lowering (delegates to lower_ibex.sh, out of scope
# for this migration -- see task notes).
# ---------------------------------------------------------------------------


def run_lower_ibex(cfg: FlowConfig, env: dict | None = None) -> Path:
    """`CORE_MLIR_ONLY=1 TOP=$TOP OUT=$OUT_DIR lower_ibex.sh`, or, when the
    flow.yaml names a design manifest, circt_flow/frontend.py on it.

    Returns the resulting `${TOP}_core.mlir` path (cfg.core_mlir_raw).
    """
    import os

    if cfg.design.manifest is not None:
        from . import frontend, profile as profile_mod
        print("[circt_flow] frontend (manifest) + LLHD lowering")
        frontend.run(cfg.design.manifest,
                     profile_mod.load(cfg.design.profile), cfg.out_dir,
                     cfg.circt_opt.parent / "circt-verilog", cfg.circt_opt)
        return cfg.core_mlir_raw
    print("[circt_flow] frontend + LLHD lowering (stop at core MLIR)")
    merged_env = dict(os.environ if env is None else env)
    merged_env["CORE_MLIR_ONLY"] = "1"
    merged_env["TOP"] = cfg.design.top_module
    merged_env["OUT"] = str(cfg.out_dir)
    _run([cfg.lower_ibex_sh], env=merged_env)
    return cfg.core_mlir_raw


# ---------------------------------------------------------------------------
# Stage 1: hw-aggregate-to-comb (de-aggregate struct-typed ports/values).
# ---------------------------------------------------------------------------


def hw_aggregate_to_comb(cfg: FlowConfig) -> Path:
    src = cfg.core_mlir_raw
    # localize-dynamic-rmw (scoreboard_residual diagnosis, 2026-09-04):
    # `array_inject a[i], g(array_get a[i])` -- the RTL's
    # `mem[idx].field = f(mem[idx].field)` -- is rewritten on the
    # still-aggregate core IR into array_create of per-entry
    # mux(i == k, g(a[k]), a[k]) so the de-aggregated form reads each
    # entry at a constant index instead of through a mux tree whose
    # redundancy ABC cannot see. CIRCT_FLOW_LOCALIZE_RMW=0 restores the
    # previous (byte-identical) flow. Modules without the idiom are
    # untouched.
    if os.environ.get("CIRCT_FLOW_LOCALIZE_RMW") != "0":
        if _binary_has_pass(cfg.circt_abc_opt, "localize-dynamic-rmw"):
            src = cfg.core_mlir_rmw
            print(f"[circt_flow] localize-dynamic-rmw -> {src}")
            _run([cfg.circt_abc_opt, "--localize-dynamic-rmw",
                  cfg.core_mlir_raw, "-o", src])
        else:
            # A circt-abc-opt built before the pass existed: keep the
            # previous flow rather than failing on an unknown pass.
            print(f"[circt_flow] WARNING: {cfg.circt_abc_opt} lacks "
                  "--localize-dynamic-rmw; skipping (rebuild the mapper, or "
                  "set CIRCT_FLOW_LOCALIZE_RMW=0 to silence)")
    print(f"[circt_flow] hw-aggregate-to-comb -> {cfg.core_mlir}")
    _run(
        [
            cfg.circt_opt,
            "--hw-aggregate-to-comb",
            "--canonicalize",
            "--cse",
            src,
            "-o",
            cfg.core_mlir,
        ]
    )
    return cfg.core_mlir


# ---------------------------------------------------------------------------
# Stage 2: generate liberty blackbox cell stubs (idempotent -- skipped if the
# stubs file already exists, matching the bash script's `[[ ! -f "$STUBS" ]]`
# guard).
# ---------------------------------------------------------------------------


def gen_stubs(cfg: FlowConfig) -> Path:
    if not cfg.stubs.exists():
        print(f"[circt_flow] generating blackbox cell stubs -> {cfg.stubs}")
        _run(
            [
                "python3",
                cfg.gen_stubs_py,
                cfg.design.lib,
                "-o",
                cfg.stubs,
            ]
        )
    return cfg.stubs


# ---------------------------------------------------------------------------
# Stage 3: register-legalize + clock-gate-inference + narrow-dead-register-
# bits + register-mapper + structural-mapper + cse + outline-residual-logic.
# ---------------------------------------------------------------------------


def structural_mapping(cfg: FlowConfig) -> Path:
    print(
        "[circt_flow] narrow-array-registers + register-legalize + "
        "clock-gate-inference + propagate-register-constants + "
        "narrow-dead-register-bits + register-mapper + structural-mapper "
        f"+ cse + outline-residual-logic -> {cfg.struct_mlir}"
    )
    _run(
        [
            cfg.circt_abc_opt,
            # Small seq.firmem memories (llhd-mem2reg promotes depth-2/4 FIFO
            # data words into them) become per-word registers first: AIGER
            # has no memory primitive, so a residual carrying one blackboxes.
            # CIRCT_FLOW_PRUNE_ANY_MODULE=1: erase output ports that NO
            # instance reads, iterated so the deletion cascades down the
            # hierarchy (each level's pruning exposes the next). Pure
            # liveness, no assumption. On cv32a60x it removes 129 ports /
            # 3,603 bits over three rounds -- including the prefetcher's four
            # CSR readback outputs, which nothing in cv32a60x reads.
            # CIRCT_FLOW_PRUNE_CONST_OUTPUTS=1 additionally substitutes
            # instance results whose callee output is a literal constant --
            # the dual of the aggregate fix in narrow-constant-register-bits,
            # since constants do not cross a port boundary OUTWARD either.
            # 91 of CVA6's 823 output operands are literal constants. With it
            # the cascade reaches 248 ports / 5,004 bits instead of 129/3,603.
            *(([x for _ in range(3)
                  for x in ("--prune-unused-residual-outputs=any-module=true"
                            + (" propagate-constant-outputs=true"
                               if os.environ.get("CIRCT_FLOW_PRUNE_CONST_OUTPUTS") == "1"
                               else "")
                            # CIRCT_FLOW_PRUNE_OUTPUT_FIELDS=1: split a
                            # struct-typed output into one port per field
                            # some instance actually extracts. CVA6's
                            # instruction FIFO returns 7 fields and only
                            # `ex`, `instr` and `cf` are ever extracted --
                            # `ex_vaddr` is 32 bits x 4 entries x 2 FIFOs
                            # written and never read. Per-bit liveness does
                            # not cross an aggregate port, so nothing else
                            # sees it.
                            + (" prune-unused-output-fields=true"
                               if os.environ.get("CIRCT_FLOW_PRUNE_OUTPUT_FIELDS") == "1"
                               else ""),
                            "--canonicalize")])
              if (os.environ.get("CIRCT_FLOW_PRUNE_ANY_MODULE") == "1"
                  or os.environ.get("CIRCT_FLOW_PRUNE_CONST_OUTPUTS") == "1"
                  or os.environ.get("CIRCT_FLOW_PRUNE_OUTPUT_FIELDS") == "1") else []),
            # CIRCT_FLOW_SPECIALIZE_INPUTS=1: inward dual of the prune loop's
            # propagate-constant-outputs. A module input every instance
            # drives with the same literal becomes that literal inside the
            # body, so the register passes below can see it. cv32a60x ties
            # off the hardware prefetcher in cva6.sv but the tie-off sits
            # three port boundaries above the block, so nothing reached it
            # and the netlist kept the whole block, ~800 flops.
            # Design-level only (gate with a top-level miter). OFF by
            # default.
            # Iterated, like the prune loop above and for the same reason:
            # one round only sees ports an instance drives with a literal
            # that is ALREADY an `hw.constant`. Where a caller passes a
            # slice of a wider tie-off the operand is a `comb.extract`
            # until canonicalize folds it, so the cascade stops one level
            # short -- measured on cv32a60x, where round 1 specialised 111
            # ports but left the prefetcher's own `*_set_i` enables alone
            # and round 2 caught 10 more and reached them. Round 3 is the
            # fixed-point check and reports 0.
            *(([x for _ in range(3)
                  for x in ("--specialize-constant-module-inputs="
                            "enable-specialize-constant-inputs=true",
                            "--canonicalize", "--cse")])
              if os.environ.get("CIRCT_FLOW_SPECIALIZE_INPUTS") == "1" else []),
            "--lower-firmem",
            "--narrow-array-registers",
            # Aggregate-typed firregs -> integer firregs with a packed constant
            # reset, so register-legalize keeps their ASYNC reset (see Passes.td).
            "--lower-aggregate-registers",
            "--register-legalize",
            # An EARLY sequential constant pass, before clock gating. This
            # is an ordering fix, not a second lever: clock-gate-inference
            # removes a register's hold mux and moves the enable onto the
            # clock, so a register that provably holds its reset value
            # stops looking like one to a pass that reads only `next`.
            # Measured on cv32a60x: the prefetcher resolves 12 of 12
            # registers when nothing has gated it yet and only 8 of 12
            # after, and the netlist shows the survivors sitting on
            # `hwpf_stride_i/clock_gate_*` outputs. Same options as the
            # late invocation below, same env var.
            *(["--propagate-register-constants=fold-unknown-ops=true "
               "mutual-fixpoint=true absorbing-operands=true",
               "--canonicalize", "--cse"]
              if os.environ.get("CIRCT_FLOW_RCP_SEQUENTIAL") == "1" else []),
            # CIRCT_FLOW_NO_CLOCK_GATING=1 keeps every register on the plain
            # clock (miter-friendly gate netlists); default unchanged.
            cfg.directives.clock_gate_inference_option(cfg.design.all_libs)
            + (" enable-clock-gating=false"
               if os.environ.get("CIRCT_FLOW_NO_CLOCK_GATING") == "1" else "")
            # CIRCT_FLOW_CG_BITLEVEL=1: resolve enables per BIT through the
            # concat/extract/mux chains of an entry-array register and split
            # it into same-enable sub-registers on gated clocks. CVA6's TLB
            # (16 entries x 112 bits under a whole-array flush/update chain)
            # is invisible to the whole-register shape. OFF by default
            # (project convention for a new lever).
            # ON by default since 2026-09-15: an area win on both CVA6
            # configurations (N=2 non-overlapping runs), timing inside
            # noise, `cva6_tlb` miter SUCCESS.
            # CIRCT_FLOW_CG_BITLEVEL=0 disables.
            + ("" if os.environ.get("CIRCT_FLOW_CG_BITLEVEL") == "0"
               else " bit-level-enables=true")
            # CIRCT_FLOW_CG_SELECT_DC=1: rewrite each gated bit-level part's
            # load-mux selects under the part's own enable (ClockGateInference
            # select-dont-care). A counter clear `mux(start, 0, q+1)` whose
            # increment cubes all imply !idle selects on idle alone, so the
            # late request terms in `start` reach only the ICG enable, not D
            # (hpdcache_cmo's cmoh_set_q is the motivating case).
            # OFF by default.
            + (" select-dont-care=true"
               if os.environ.get("CIRCT_FLOW_CG_SELECT_DC") == "1" else ""),
            # De-aggregate BEFORE narrow-constant-register-bits, not only
            # after register-mapper. That pass evaluates a bit-level
            # known-bits lattice over comb ops and stops dead at any
            # aggregate op -- a struct-typed `comb.mux`, or the `hw.bitcast`
            # lower-aggregate-registers puts on an aggregate register's
            # `next`. Measured on cv32a60x (reproducible, from the pass's own
            # summary line): 7 registers replaced by literals / 15 narrowed /
            # 314 constant bits without this, 8 / 25 / 771 with it --
            # `load_store_unit`'s 131-bit `pmp_exception` among the new ones
            # (NrPMPEntries == 0 makes the whole PMP path constant, but the
            # value reaches that register through a struct mux). Netlist
            # effect: 10,669 -> 10,203 flops.
            # `--propagate-register-constants` is NOT affected by this
            # reorder (its output IR is bit-identical either way); it is
            # whole-register and already sees through the bitcast.
            # Idempotent: the flow runs this exact lowering again below,
            # after register-mapper reintroduces aggregate muxes of its own.
            "--hw-aggregate-to-comb",
            "--canonicalize",
            # CIRCT_FLOW_RCP_SEQUENTIAL=1: two extensions that only pay
            # together, and only on top of CIRCT_FLOW_SPECIALIZE_INPUTS.
            # `fold-unknown-ops` resolves any pure integer op whose
            # operands all evaluate to constants via the op's own folder,
            # which is what lets a `comb.icmp` state comparison resolve;
            # `mutual-fixpoint` swaps the least fixed point for a greatest
            # one, so a group of registers that is only JOINTLY constant is
            # provable. CVA6's tied-off hardware prefetcher needs both plus
            # the absorbing-element rule in `comb.and`: measured on
            # cv32a60x 2026-09-17, `hwpf_stride` goes 12 registers to 0 and
            # the module-definition firreg count 275 -> 256. Design-level
            # sound only (the specialisation changes what a module computes
            # for inputs it is never given), so gate with a TOP-LEVEL
            # miter. OFF by default.
            "--propagate-register-constants"
            + ("=fold-unknown-ops=true mutual-fixpoint=true"
               " absorbing-operands=true"
               if os.environ.get("CIRCT_FLOW_RCP_SEQUENTIAL") == "1" else ""),
            # Per-bit, cross-single-instance constant propagation (direction 2:
            # the CSR reserved/read-only bits); design-level, see Passes.td.
            # CIRCT_FLOW_NCRB_MULTI_INSTANCE=1 lifts the single-instance
            # restriction: a module input resolves to the MEET of every
            # instance's operand instead of requiring exactly one instance.
            # OFF by default (project convention for a new lever).
            *(["--narrow-constant-register-bits="
               + " ".join(
                   (["cross-multi-instance=true"]
                    if os.environ.get("CIRCT_FLOW_NCRB_MULTI_INSTANCE") == "1" else [])
                   # CIRCT_FLOW_NCRB_AGGREGATES=1: evaluate struct/array values
                   # as packed known-bits, so a constant crossing an
                   # aggregate-typed PORT or sitting in an array register is
                   # visible. hw-aggregate-to-comb does not cover this (it
                   # leaves port types alone) and CIRCT's hw-flatten-io cannot
                   # legalize CVA6's array-of-struct ports. cv32a60x: constant
                   # bits removed 1,046 -> 2,616.
                   + (["cross-aggregates=true"]
                      if os.environ.get("CIRCT_FLOW_NCRB_AGGREGATES") == "1" else [])
                   # CLOSED ON POLICY, not on numbers: X-optimistic. Treating an
            # unreset register as holding a known constant is the line this
            # project deliberately does not cross, so this stays off however
            # much area it would show.
            # CIRCT_FLOW_NCRB_UNRESET_HOLDS=1: also hypothesise
                   # reset-less registers as zero, so one that provably only
                   # holds becomes a constant. X-OPTIMISTIC -- it assumes an
                   # unreset register's power-up value is a don't-care, which
                   # is the standard synthesis convention but
                   # is NOT equivalence-preserving against an X-modelling
                   # simulator. OFF by default for that reason.
                   + (["fold-unreset-holds=true"]
                      if os.environ.get("CIRCT_FLOW_NCRB_UNRESET_HOLDS") == "1" else []))]
              if (os.environ.get("CIRCT_FLOW_NCRB_MULTI_INSTANCE") == "1"
                  or os.environ.get("CIRCT_FLOW_NCRB_AGGREGATES") == "1"
                  or os.environ.get("CIRCT_FLOW_NCRB_UNRESET_HOLDS") == "1")
              else ["--narrow-constant-register-bits"]),
            # CIRCT_FLOW_DEAD_OUTPUT_BITS=1: design-level, callers-first
            # per-bit liveness of every instance result; output-port bits no
            # instance reads are forced to zero at the callee's hw.output so
            # the per-module narrow-dead-register-bits below can retire what
            # fed them. CVA6's MMU reads 47 of the TLB's 64 PTE bits, so 17
            # per entry are dead. Sound at the design level only (gated by
            # top-level miters). OFF by default.
            # ON by default since 2026-09-15: an area win and -536 flops on
            # cv64a6, part of the cv32a60x win above; cv32a60x top-level
            # bounded miter SUCCESS (14.9 M vars, seq 4 from reset).
            # CIRCT_FLOW_DEAD_OUTPUT_BITS=0 disables.
            *([] if os.environ.get("CIRCT_FLOW_DEAD_OUTPUT_BITS") == "0"
              else ["--narrow-dead-output-bits"]),
            # With it, narrow-dead-register-bits must see through the
            # integer -> aggregate -> integer bitcast pair a zeroed aggregate
            # output leaves between the slices and the register (hw.bitcast
            # is a bit reinterpretation, never a permutation).
            cfg.directives.narrow_dead_register_bits_option()
            + ("" if os.environ.get("CIRCT_FLOW_DEAD_OUTPUT_BITS") == "0"
               else (("" if "=" in cfg.directives.narrow_dead_register_bits_option()
                      else "=")
                     + ("" if "=" not in cfg.directives.narrow_dead_register_bits_option()
                        else " ")
                     + "precise-bitcast=true")),
            # CIRCT_FLOW_DEDUP_REGISTERS=1 merges ibex's deliberate
            # fanout-reduction register replicas (-48 DFF bits). OFF by
            # default and measured NOT worth taking: pass-on made worst
            # slack clearly worse (2026-08-27). Enable only when trading
            # timing for area deliberately.
            *(["--dedup-registers=enable-register-dedup=true"]
              if os.environ.get("CIRCT_FLOW_DEDUP_REGISTERS") == "1" else []),
            f"--register-mapper=liberty={cfg.design.lib}",
            # Must sit between register-mapper and outline-residual-logic.
            # A negedge-clocked register keeps its clock as a `!seq.clock`
            # right up until register-mapper swaps it for a real DFF cell
            # whose CK port is an i1 -- THAT is what inserts the
            # `seq.from_clock` closing the cast chain, and outlining is what
            # would then carry the chain into a residual module where AIGER
            # cannot express it. A no-op on designs with no inverted clock
            # (ibex rewrites 0 and struct.mlir stays byte-identical).
            "--lower-clock-inverters",
            # Re-run de-aggregation AFTER the register passes: on an
            # aggregate-typed register (CVA6's bht is
            # array<16xarray<2xstruct<...>>>), clock-gate-inference and
            # register-mapper insert their enable/flush muxes AT THE
            # REGISTER'S TYPE, reintroducing aggregate comb ops that the
            # stage-1 de-aggregation already eliminated -- CVA6 measured 1
            # aggregate mux before this stage and 54 after. Any such op swept
            # into a residual module makes it unmappable to AIGER and
            # blackboxes real logic (42 of CVA6's residual modules). A no-op
            # on ibex, whose registers are integer-typed by this point.
            "--hw-aggregate-to-comb",
            # Decoded one-hot select for exclusive mux chains (case statements):
            # the $pmux form; canonicalize erases the dead chain links.
            # CLOSED -- measured an area win pre-P&R but a larger loss at
            # FINISH, because the decoded select needs buffering the mux
            # chain did not. A textbook case of pre-P&R being a screen and
            # finish being the result. Do not re-test without new information.
            # CIRCT_FLOW_PRIORITY_DECODE=1: also decode chains whose
            # conditions are NOT provably exclusive, with priority-encoded
            # one-hot selects (an unconditional identity). cv64a6 census:
            # 110 such chains, 7k+ link-bits, CVA6's TLB hit-select (16 x
            # 64) the largest. OFF by default (project convention).
            # CIRCT_FLOW_PRIORITY_MIN_WIDTH=<bits> restricts it to data
            # chains at least that wide (the depth loss measured with the
            # default came from narrow decoder/bound-table chains).
            "--decode-mux-chains"
            + ("=priority-decode=true priority-min-width="
               + os.environ.get("CIRCT_FLOW_PRIORITY_MIN_WIDTH", "2")
               if os.environ.get("CIRCT_FLOW_PRIORITY_DECODE") == "1" else ""),
            # CIRCT_FLOW_NARROW_EXT_MUL=1: rewrite a 2W-bit comb.mul whose
            # operands are W-bit values widened by a replicated sign bit into
            # a zero-extended multiply plus a W-bit correction. CombToSynth
            # does NOT exploit such an extension -- an i64 multiply of two
            # sign-extended i32 values costs 22,962 AIG nodes, byte-identical
            # to a genuine 64x64 multiply -- but it DOES exploit a constant
            # zero high half (12,914). CVA6's one multiply is exactly this
            # shape and the rewrite takes it to 15,200 (-34%). Runs BEFORE
            # set-adder-arch so the correction adder gets an architecture
            # too. Inert on ibex, which has no comb.mul at all. OFF by
            # default (project convention).
            *(["--narrow-extended-multiplies=enable-narrow-extended-multiplies=true"]
              if os.environ.get("CIRCT_FLOW_NARROW_EXT_MUL") == "1" else []),
            # CIRCT_FLOW_NARROW_SPARSE_SHIFT=1: same blind spot, shift side.
            # CombToSynth builds a full barrel shifter even when the amount
            # has provably-constant bits. CVA6's icache has six 128-bit shru
            # whose amount is concat(0_i121, s0, s1, 0_i5) -- one of
            # {0,32,64,96} -- and each costs 1,788 AIG nodes, identical to a
            # fully variable shift, where the 4:1 mux costs 12. Those six are
            # 23.4% of CVA6's combinational AIG. OFF by default.
            *(["--narrow-sparse-shifts=enable-narrow-sparse-shifts=true"]
              if os.environ.get("CIRCT_FLOW_NARROW_SPARSE_SHIFT") == "1" else []),
            # Adder architecture CombToSynth will use for comb.add/icmp >= 8
            # bits. Default BRENT-KUNG (2026-09-03: cv64a6 macro-flow P&R N=2
            # smaller std-cell area and power, timing met vs the width
            # heuristic's Sklansky/Kogge-Stone; ibex miter SUCCESS).
            # CIRCT_FLOW_ADDER_ARCH=<RIPPLE-CARRY|SKLANSKEY|BRENT-KUNG|
            # KOGGE-STONE|none> overrides; none = CombToSynth's heuristic.
            # CIRCT_FLOW_MUL_CELLS=1: lower every multiply's carry-save
            # compressor tree to full/half adder cell instances (Dadda)
            # before ABC, which cannot emit two-output cells and re-derives
            # the tree anyway; the Booth partial products and the final add
            # stay comb. OFF by default (project convention).
            # CIRCT_FLOW_MUL_CELLS_PREFIX=1 (with MUL_CELLS=1): also build each
            # tree's final two-row add as a Brent-Kung prefix network of cells.
            # Without it the final add is handed to ABC as an isolated cone,
            # and area-only `&deepsyn` collapses its prefix structure into a
            # near-ripple -- measured on cv64a6, where it was the
            # compressor-cell arm's worst path. That, not the adder cells,
            # was the timing loss this lever was closed on.
            *([f"--map-compressor-cells=enable-compressor-cells=true "
               f"liberty={cfg.design.lib}"
               + (" prefix-final-adder=true"
                  if os.environ.get("CIRCT_FLOW_MUL_CELLS_PREFIX") == "1" else "")]
              if os.environ.get("CIRCT_FLOW_MUL_CELLS") == "1" else []),
            *([f"--set-adder-arch=arch={os.environ.get('CIRCT_FLOW_ADDER_ARCH', 'BRENT-KUNG')}"]
              if os.environ.get("CIRCT_FLOW_ADDER_ARCH", "BRENT-KUNG") != "none" else []),
            # CIRCT_FLOW_ADDER_CELLS=<min-width>: lower comb.add at or above
            # that width straight into a ripple of the library's two-output
            # half/full adder cells, which ABC's mapper CANNOT emit
            # (Mio_CollectRootsNew2 skips every pTwin cell), so a ripple ABC
            # maps is carried partly through generic gates instead of
            # adder cells. Runs BEFORE set-adder-arch's consumer (CombToSynth)
            # ever sees the op, so the two do not fight: an add lowered here
            # is gone before the arch attribute matters. Unset = off.
            *([f"--map-adder-chains=liberty={cfg.design.lib} "
               f"enable-adder-chains=true "
               f"min-width={os.environ['CIRCT_FLOW_ADDER_CELLS']}"]
              if os.environ.get("CIRCT_FLOW_ADDER_CELLS") else []),
            "--canonicalize",
            f"--structural-mapper=liberty={cfg.design.lib}",
            "--cse",
            # CIRCT_FLOW_DP_OVERRIDES=<json>: per-op architecture overrides
            # keyed by dp-annotate ids (the PHYS-DP loop's write-back); runs
            # right after the global default stamp so it wins.
            *([f"--dp-apply-arch=json={os.environ['CIRCT_FLOW_DP_OVERRIDES']}"]
              if os.environ.get("CIRCT_FLOW_DP_OVERRIDES") else []),
            # CIRCT_FLOW_DP_JSON=<path> (PHYS-DP step 1): sidecar JSON of wide
            # arithmetic ops with their launch/capture flop anchors, taken
            # BEFORE outlining so anchors are mapped-flop instances whose
            # names survive to the netlist. IR untouched.
            *([f"--dp-annotate=json={os.environ['CIRCT_FLOW_DP_JSON']}"]
              if os.environ.get("CIRCT_FLOW_DP_JSON") else []),
            # CIRCT_FLOW_OUTLINE_DATAPATH=1 (PHYS-DP step 1): every wide
            # arithmetic op becomes its own residual (identity + per-op group).
            # CIRCT_FLOW_DP_IDS=<file>: outline only these dp-annotate ids
            # (the loop's critical set); without it, every wide op.
            *([("--outline-datapath-ops=ids=" + os.environ["CIRCT_FLOW_DP_IDS"])
               if os.environ.get("CIRCT_FLOW_DP_IDS") else "--outline-datapath-ops"]
              if os.environ.get("CIRCT_FLOW_OUTLINE_DATAPATH") == "1" else []),
            # CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1 (experiment, M7 re-measure):
            # defer outlining to stage 4 so one residual spans the flattened
            # design and ABC sees no module boundaries.
            # Direction 0: break the word-level false cycles sig2reg/mem2reg
            # create (every shipped feedback-cut op / shell cell on cv64a6)
            # before the outliner cuts them; CIRCT_FLOW_NARROW_WHOLE_VALUE_READS=0
            # restores the cut. Cycle-directed: modules without SCCs are untouched.
            *([] if os.environ.get("CIRCT_FLOW_NARROW_WHOLE_VALUE_READS") == "0"
              else ["--narrow-whole-value-reads", "--cse"]),
            *([] if os.environ.get("CIRCT_FLOW_OUTLINE_AFTER_FLATTEN") == "1"
              else ["--outline-residual-logic", "--canonicalize"]),
            cfg.core_mlir,
            "-o",
            cfg.struct_mlir,
        ]
    )
    return cfg.struct_mlir


# ---------------------------------------------------------------------------
# Stage 4: hw-flatten-modules (shell-only flatten, pre-merge).
# ---------------------------------------------------------------------------


def flatten_shell(cfg: FlowConfig) -> Path:
    print(f"[circt_flow] hw-flatten-modules (shell only, pre-merge) -> {cfg.flat_mlir}")
    _run(
        [
            cfg.circt_opt,
            "--hw-flatten-modules",
            cfg.struct_mlir,
            "-o",
            cfg.flat_mlir,
        ]
    )
    if os.environ.get("CIRCT_FLOW_OUTLINE_AFTER_FLATTEN") == "1":
        print("[circt_flow] outline-residual-logic AFTER flatten (one residual)")
        _run(
            [
                cfg.circt_abc_opt,
                "--outline-residual-logic",
                "--canonicalize",
                cfg.flat_mlir,
                "-o",
                str(cfg.flat_mlir) + ".outlined",
            ]
        )
        os.replace(str(cfg.flat_mlir) + ".outlined", cfg.flat_mlir)
    # Erase residual output ports the flattened design never reads (they
    # would become AIG primary outputs and ABC would keep their cones), then
    # canonicalize/cse the dead cones away. Default on (function-preserving by
    # construction); CIRCT_FLOW_PRUNE_RESIDUAL_OUTPUTS=0 disables.
    if os.environ.get("CIRCT_FLOW_PRUNE_RESIDUAL_OUTPUTS", "1") != "0":
        print("[circt_flow] prune-unused-residual-outputs + canonicalize/cse")
        _run(
            [
                cfg.circt_abc_opt,
                "--prune-unused-residual-outputs",
                "--canonicalize",
                "--cse",
                cfg.flat_mlir,
                "-o",
                str(cfg.flat_mlir) + ".pruned",
            ]
        )
        os.replace(str(cfg.flat_mlir) + ".pruned", cfg.flat_mlir)
    return cfg.flat_mlir


# ---------------------------------------------------------------------------
# Stage 5: merge-residual-modules + abc-bridge (the real ABC subprocess).
# ---------------------------------------------------------------------------


def _abc_bridge_opt(cfg: FlowConfig, extra: str = "") -> str:
    enable_classical_resub = "true" if cfg.enable_classical_resub else "false"
    opt = (
        f"--abc-bridge=liberty={cfg.design.lib} abc-path={cfg.abc_bin} "
        f"stubs={cfg.stubs} clock-period-ps={cfg.design.clock_period_ps} "
        f"enable-classical-resub={enable_classical_resub}"
    )
    if cfg.abc_script_file:
        opt += f" abc-script-file={cfg.abc_script_file}"
    if getattr(cfg, "abc_use_datapath", False):
        opt += " use-datapath=true"
    # CLOSED -- measured a loss. `&nf` has no drive-selection stage to spend
    # per-output slack on, and `-D 500` / `-D 350` return byte-identical
    # netlists. Do not re-test without new information.
    # CIRCT_FLOW_NF_RELAX=<n>: map with `&nf -t -R <n>` so each output's
    # required time is its own delay-optimal arrival plus n%, instead of the
    # network's global critical delay (which lets area recovery deepen every
    # non-critical cone to the critical path -- the mechanism behind a
    # long tail of needlessly deep paths). Unset = upstream behaviour.
    if os.environ.get("CIRCT_FLOW_NF_RELAX"):
        opt += f" nf-relax-per-output={os.environ['CIRCT_FLOW_NF_RELAX']}"
    # CIRCT_FLOW_DEEPSYN_SEC: &deepsyn's CPU-second budget (default 10).
    # It is PER ABC RUN, so it must be scaled when the partition count
    # changes -- 32 partitions at -T 10 give the design 320 CPU-seconds of
    # restructuring, one partition at -T 10 gives it 10. Comparing partition
    # counts without scaling this compares optimisation budgets, not
    # partitioning. On ibex (one residual) -T 600 gave a measurable win.
    if os.environ.get("CIRCT_FLOW_DEEPSYN_SEC"):
        opt += f" deepsyn-time-sec={os.environ['CIRCT_FLOW_DEEPSYN_SEC']}"
    if getattr(cfg, "abc_read_lib_args", None):
        # pass-option values cannot contain spaces (MLIR splits on them)
        opt += f" read-lib-args={str(cfg.abc_read_lib_args).strip().replace(' ', ',')}"
    if cfg.abc_save_mapped_dir:
        opt += f" save-mapped-dir={cfg.abc_save_mapped_dir}"
    if getattr(cfg, "abc_save_aig_dir", None):
        opt += f" save-aig-dir={cfg.abc_save_aig_dir}"
    if cfg.abc_use_mapped_dir:
        opt += f" use-mapped-dir={cfg.abc_use_mapped_dir}"
        if getattr(cfg, "abc_use_mapped_if_present", False):
            opt += " use-mapped-if-present=true"
    if cfg.skip_unmappable:
        opt += " skip-unmappable=true"
    return opt + (f" {extra}" if extra else "")


def _remove_identity_buffers_opt(cfg: FlowConfig) -> str:
    """remove-identity-buffers pass option string; off when the harness
    asked to keep its own buffers (abc_keep_buffers)."""
    opt = f"--remove-identity-buffers=liberty={cfg.design.lib}"
    if getattr(cfg, "abc_keep_buffers", False):
        opt += " enable-identity-buffer-removal=false"
    return opt


def _specialize_mux_arms_opts() -> list[str]:
    """CIRCT_FLOW_SPECIALIZE_MUX_ARMS=1: rewrite each mux arm (and each i1
    AND/OR input) under the value its select (siblings) must have for it to
    be observed. Removes the path-predicate false dependences
    llhd-remove-control-flow leaves in always_comb FSMs: in a CVA6 with an
    hpdcache, load_unit's data_req/translation_req_o on data_gnt closed a
    501-node false combinational loop through the hpdcache arbiter; with the
    pass cva6 has 0 SCCs, div op_a depth 179 -> 127 AIG levels, csr instret
    158 -> 108, and the bit-level graph is 3% smaller. Function-preserving
    by construction. Runs on the residual logic right before merging."""
    if os.environ.get("CIRCT_FLOW_SPECIALIZE_MUX_ARMS") != "1":
        return []
    return ["--specialize-mux-arms=enable=true", "--canonicalize", "--cse"]


def _select_prefix_or_opts() -> list[str]:
    """CIRCT_FLOW_SELECT_PREFIX_OR=1: collapse pass-through mux select chains
    into prefix ORs (select-chain-prefix-or). In a systolic array whose PEs
    forward a value combinationally, each PE's select is computed from the
    previous PE's mux, so the chain grows by a few cells per PE along the
    row. The rewrite makes each select p_0 | q_0 | ... | q_{k-1}. The chain
    crosses PE instances, so it needs the merged residual's self-fed ports
    internalised (the knob turns internalize-self-fed-ports on);
    single-partition path only. Function-preserving by construction. Runs
    after the last aggregate lowering, and lowers again because canonicalize
    can re-fold aggregate constants."""
    if os.environ.get("CIRCT_FLOW_SELECT_PREFIX_OR") != "1":
        return []
    return ["--select-chain-prefix-or=enable=true", "--canonicalize", "--cse",
            "--hw-aggregate-to-comb"]


def merge_and_map(cfg: FlowConfig) -> Path:
    if cfg.partitions > 1:
        return _merge_and_map_partitioned(cfg)
    print(
        "[circt_flow] merge-residual-modules + abc-bridge + "
        f"remove-identity-buffers -> {cfg.mapped_mlir}"
    )
    merge_opt = "--merge-residual-modules"
    _mopts = []
    if cfg.skip_unmappable:
        _mopts.append("skip-unmappable=true")
    # CLOSED -- measured within noise. The TLB residual ports turned out not
    # to be severed PO/PI pairs.
    # CIRCT_FLOW_INTERNALIZE_SELF_FED=1 internalises merged-instance inputs
    # that are driven by that same instance's outputs. Without it, 2,934 of
    # ibex's 5,139 residual input bits (57%) arrive as a severed (PO, PI) pair,
    # so the AIG ABC and emap optimise has that combinational path CUT -- no
    # structural hashing, no common-divisor extraction, no cut can span it.
    if (os.environ.get("CIRCT_FLOW_INTERNALIZE_SELF_FED") == "1"
            or os.environ.get("CIRCT_FLOW_SELECT_PREFIX_OR") == "1"):
        _mopts.append("internalize-self-fed-ports=true")
    # CIRCT_FLOW_FREE_DEAD_OUTPUTS=1 constant-drives merged-instance outputs
    # with no consumer. Recorded as a net loss (an emap win, a finished-
    # netlist loss) -- but that finished-netlist number came from a P&R
    # harness that was not yet deterministic, so it was never separable from
    # noise. Re-testable now; still OFF by default until it is re-measured
    # deterministically.
    if os.environ.get("CIRCT_FLOW_FREE_DEAD_OUTPUTS") == "1":
        _mopts.append("free-dead-output-ports=true")
    if _mopts:
        merge_opt += "=" + " ".join(_mopts)
    _run(
        [
            cfg.circt_abc_opt,
            *_specialize_mux_arms_opts(),
            merge_opt,
            "--canonicalize",
            # A third aggregate lowering, AFTER the canonicalize: struct-
            # typed RESET VALUES keep hw.aggregate_constant alive through
            # the register stages (a firreg operand is not a comb use), and
            # canonicalize itself re-folds constant struct fields back into
            # aggregate_constant -- so this must be the LAST thing before
            # the bridge, where convert-comb-to-synth refuses aggregates.
            # Idempotent and cheap at this stage (no LLHD regions remain).
            "--hw-aggregate-to-comb",
            *_select_prefix_or_opts(),
            # CIRCT_FLOW_NARROW_MUX=1: see the partitioned path below for
            # what this is and why. Same placement -- immediately before the
            # bridge, which is what bit-blasts the word-level mux.
            *(["--narrow-mux-to-differing-bits=enable-narrow-mux=true",
               "--canonicalize", "--cse"]
              if os.environ.get("CIRCT_FLOW_NARROW_MUX") == "1" else []),
            _abc_bridge_opt(cfg),
            f"--map-shell-logic=liberty={cfg.design.lib}",
            _remove_identity_buffers_opt(cfg),
            # CIRCT_FLOW_MAX_FANOUT=<N> caps net fanout with a balanced
            # buffer tree. MUST come after remove-identity-buffers, which
            # would delete the buffers this inserts. Measured 2026-09-07:
            # the worst net carried hundreds of loads, and OpenROAD pays for
            # those trees at placement.
            *([f"--limit-fanout=liberty={cfg.design.lib} "
               f"enable-limit-fanout=true "
               f"max-fanout={os.environ['CIRCT_FLOW_MAX_FANOUT']}"]
              if os.environ.get("CIRCT_FLOW_MAX_FANOUT") else []),
            cfg.flat_mlir,
            "-o",
            cfg.mapped_mlir,
        ]
    )
    return cfg.mapped_mlir


def _merge_and_map_partitioned(cfg: FlowConfig) -> Path:
    """Split the residual logic into `cfg.partitions` groups, map each in its
    own ABC process over a pool, then splice the results back.

    Worth being explicit that this changes the netlist: ABC optimises each
    group in isolation, so the result is NOT the same as the monolithic path
    (on ibex the partitioned netlist measured slightly larger). It exists
    because one whole-design ABC call stops being practical on large designs
    -- OpenC910's merged residual logic is a 6.2M-AND-gate AIGER, ~187x
    ibex's.
    """
    from . import abc_partition as ap

    k = cfg.partitions
    workdir = cfg.partition_workdir
    workdir.mkdir(parents=True, exist_ok=True)
    grouped = workdir / "grouped.mlir"

    print(f"[circt_flow] merge-residual-modules x{k} (partitioned) -> {grouped}")
    # CIRCT_FLOW_GROUP_BY_DEPTH=1: group residual modules by intrinsic AIG
    # depth instead of alphabetically (see depth_groups.py). &nf relaxes every
    # cone of a partition to the partition's critical delay, so on a large
    # SoC the alphabetical split put cva6's serdiv beside an interrupt
    # controller's long priority chain and rippled the divider's adder into
    # the design's worst path. Produces the group-map consumed below.
    group_map = os.environ.get("CIRCT_FLOW_GROUP_MAP")
    if os.environ.get("CIRCT_FLOW_GROUP_BY_DEPTH") == "1" and not group_map:
        group_map = str(workdir / "depth_group_map.json")
        _run([sys.executable, str(Path(__file__).with_name("depth_groups.py")),
              str(cfg.flat_mlir), str(k), group_map,
              "--opt", str(cfg.circt_abc_opt),
              "--report", str(workdir / "depth_groups.tsv")])
    merge_args = []
    for i in range(k):
        opt = f"--merge-residual-modules=group-count={k} group-index={i}"
        # CIRCT_FLOW_ISOLATE_SUBSTR=<substr>: modules containing it (the
        # outlined datapath ops, "_dp") go to the LAST group alone (PHYS-DP)
        if os.environ.get("CIRCT_FLOW_ISOLATE_SUBSTR"):
            opt += f" isolate-substr={os.environ['CIRCT_FLOW_ISOLATE_SUBSTR']}"
            if os.environ.get("CIRCT_FLOW_ISOLATE_EACH") == "1":
                opt += " isolate-each=true"
        # CIRCT_FLOW_GROUP_MAP=<json {module: group index}>: pin the
        # non-isolated modules to the baseline grouping (netlist reuse stays
        # valid when isolated groups are added beyond the baseline count)
        if group_map:
            opt += f" group-map={group_map}"
        if cfg.skip_unmappable:
            opt += " skip-unmappable=true"
        merge_args.append(opt)
    # CIRCT_FLOW_NARROW_MUX=1: narrow a wide comb.mux to the bits where its
    # arms actually differ, BEFORE abc-bridge bit-blasts it. convert-comb-to-
    # synth keeps synth.aig.and_inv at the word level, so a 1087-bit mux that
    # sets one bit (the scoreboard's read-modify-write write port) costs
    # 4*1087 AIG nodes; the exporter's structural hashing cannot recover it,
    # and ABC then spends the group's whole budget proving it away. cv32a60x
    # group g30 measured 68,254 -> 19,497 AIGER ANDs, ABC `cec`-equivalent.
    # OFF by default (project convention for a new lever).
    _run([cfg.circt_abc_opt, "--mlir-disable-threading",
          *_specialize_mux_arms_opts(), *merge_args,
          "--canonicalize", "--hw-aggregate-to-comb",
          *(["--narrow-mux-to-differing-bits=enable-narrow-mux=true",
             "--canonicalize", "--cse"]
            if os.environ.get("CIRCT_FLOW_NARROW_MUX") == "1" else []),
          cfg.flat_mlir, "-o", grouped])

    print(f"[circt_flow] abc-bridge on {k} partitions, pool={cfg.abc_pool_size}")
    # CIRCT_FLOW_GROUP_SCRIPTS=<json {group: abc script file}>: per-group ABC
    # body override (PHYS-DP: the depth-preserving body for groups holding a
    # timing-critical datapath op); other groups keep the default body.
    per_group = {}
    if os.environ.get("CIRCT_FLOW_GROUP_SCRIPTS"):
        import json as _json
        per_group = {g: f"abc-script-file={f}" for g, f in
                     _json.load(open(os.environ["CIRCT_FLOW_GROUP_SCRIPTS"])).items()}
        print(f"[circt_flow] per-group ABC scripts for {len(per_group)} group(s)")
    results = ap.map_groups(grouped, workdir / "parts", str(cfg.circt_abc_opt),
                            _abc_bridge_opt(cfg), cfg.abc_pool_size,
                            per_group_extra=per_group)
    ok = [r for r in results if r.returncode == 0]
    total = sum(r.seconds for r in results)
    slowest = max(results, key=lambda r: r.seconds)
    print(f"[circt_flow]   {len(ok)}/{len(results)} groups mapped; "
          f"{total/60:.0f} min of ABC work, slowest {slowest.name} "
          f"({slowest.seconds:.0f}s)")

    spliced = workdir / "spliced.mlir"
    ap.combine(grouped, results, spliced)

    # map-shell-logic + remove-identity-buffers still run over the whole
    # design, exactly as in the monolithic path -- neither is
    # partition-sensitive. map-shell-logic maps the outliner's feedback-cut
    # leftovers (real mux/or logic on CVA6) to cells so the exported netlist
    # is 100% cells + wiring (OpenSTA rejects operator expressions).
    print(f"[circt_flow] map-shell-logic + remove-identity-buffers -> "
          f"{cfg.mapped_mlir}")
    _run([cfg.circt_abc_opt, "--canonicalize",
          f"--map-shell-logic=liberty={cfg.design.lib}",
          _remove_identity_buffers_opt(cfg),
          spliced, "-o", cfg.mapped_mlir])
    return cfg.mapped_mlir


# ---------------------------------------------------------------------------
# Stage 6: final hw-flatten-modules + export-verilog -> the netlist that
# other tooling (the ORFS stage, the LEC scripts) depends on at
# exactly `1_synth/<top>.v`.
# ---------------------------------------------------------------------------


def export_netlist(cfg: FlowConfig) -> Path:
    cfg.synth_dir.mkdir(parents=True, exist_ok=True)
    print(f"[circt_flow] hw-flatten-modules + export-verilog -> {cfg.netlist}")
    _run(
        [
            cfg.circt_opt,
            cfg.mapped_mlir,
            "--hw-flatten-modules",
            "--export-verilog",
            "-o",
            "/dev/null",
        ],
        stdout=cfg.netlist,
    )
    print(f"[circt_flow] done: {cfg.netlist}")
    # CIRCT_FLOW_POST_MAP_SWEEP=1: bit-level dead-logic sweep of the mapped
    # netlist (post_map_sweep.py). Every register pass above runs before
    # ABC, so logic whose outputs only go unused after mapping survives;
    # on a large SoC that was a large share of the flops. OFF by default
    # until it has been measured on the cv32a60x/cv64a6 arms.
    # CIRCT_FLOW_SWEEP_STUBS adds blackbox stub files (macros) for the Yosys
    # read.
    if os.environ.get("CIRCT_FLOW_POST_MAP_SWEEP") == "1":
        post_map_sweep(cfg)
    return cfg.netlist


def post_map_sweep(cfg: FlowConfig) -> Path:
    swept = Path(str(cfg.netlist) + ".swept")
    cmd = [sys.executable, str(cfg.root / "scripts" / "circt_flow" / "post_map_sweep.py"),
           str(cfg.netlist), cfg.design.top_module, str(swept)]
    for lib in cfg.design.synth_lib_tt:
        cmd += ["--liberty", str(lib)]
    for stub in filter(None, os.environ.get("CIRCT_FLOW_SWEEP_STUBS", "").split(",")):
        cmd += ["--stubs", stub]
    _run(cmd)
    os.replace(swept, cfg.netlist)
    return cfg.netlist


# ---------------------------------------------------------------------------
# Stage 7 (optional): OpenSTA-compatibility post-processing via sv2v
# (struct-flatten) + stripping the `wire` keyword from port declarations.
# Skipped (with a warning, matching the bash script) if the sv2v Docker
# image is not present locally.
# ---------------------------------------------------------------------------

_WIRE_PORT_RE = re.compile(r"^(\s*)(input|output|inout) wire ", re.MULTILINE)
_DECL_INIT_RE = re.compile(r"^(\s*)wire(\s+\[[^\]]+\])?\s+(\S+)\s*=\s*(.+;)\s*$", re.MULTILINE)


def _docker_image_present(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def opensta_compat_netlist(cfg: FlowConfig) -> Path | None:
    # SV2V=<binary> runs sv2v directly (the circt-abc image sets it);
    # otherwise it runs from cfg.sv2v_docker_image.
    sv2v = os.environ.get("SV2V")
    if sv2v:
        print(f"[circt_flow] sv2v struct-flatten -> {cfg.sv2v_out}")
        _run([sv2v, f"--write={cfg.sv2v_out}", cfg.synth_dir / cfg.netlist.name])
    elif not _docker_image_present(cfg.sv2v_docker_image):
        print(
            f"[circt_flow] WARNING: docker image '{cfg.sv2v_docker_image}' not "
            f"found -- skipping OpenSTA-compat netlist ({cfg.openroad_netlist} "
            f"not produced). Build/pull it, or set SV2V_DOCKER_IMAGE / pass "
            f"sv2v_docker_image=, to enable this step."
        )
        return None
    else:
        print(f"[circt_flow] sv2v struct-flatten -> {cfg.sv2v_out}")
        synth_dir_abs = cfg.synth_dir.resolve()
        _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{synth_dir_abs}:{synth_dir_abs}",
                "--entrypoint",
                "sv2v",
                cfg.sv2v_docker_image,
                f"--write={cfg.sv2v_out}",
                str(synth_dir_abs / cfg.netlist.name),
            ]
        )

    print(f"[circt_flow] strip 'wire' from port decls -> {cfg.openroad_netlist}")
    text = cfg.sv2v_out.read_text()
    stripped = _WIRE_PORT_RE.sub(r"\1\2 ", text)
    # OpenSTA's reader mis-parses `wire [N:0] x = {...};` (declaration with
    # initializer) as a scalar assign (STA-0203) and drops it, leaving the net
    # undriven; a separate assign is aliased correctly. Split them.
    stripped, n_split = _DECL_INIT_RE.subn(
        lambda m: f"{m.group(1)}wire{m.group(2) or ''} {m.group(3)};\n{m.group(1)}assign {m.group(3)} = {m.group(4)}", stripped)
    print(f"[circt_flow] split {n_split} initialized wire declarations into decl + assign")
    cfg.openroad_netlist.write_text(stripped)
    print(f"[circt_flow] OpenSTA-compatible netlist -> {cfg.openroad_netlist}")
    return cfg.openroad_netlist


# ---------------------------------------------------------------------------
# Stage 8 (opt-in, `--pnr`): place and route on stock ORFS.
# ---------------------------------------------------------------------------


def orfs_pnr(cfg: FlowConfig) -> Path:
    """Run circt_flow.orfs_pnr on the OpenSTA-compatible netlist, configured
    by the flow.yaml's `pnr:` section. Returns <out>/pnr/pnr_metrics.json."""
    from . import orfs_pnr as pnr_mod

    p = cfg.design.pnr
    if not p:
        raise FileNotFoundError(
            "--pnr needs a `pnr:` section in the flow.yaml (platform, "
            "clock_port, ...)")
    if not cfg.openroad_netlist.exists():
        raise FileNotFoundError(
            f"{cfg.openroad_netlist} was not produced (the sv2v step needs "
            "its docker image); ORFS reads that netlist, not the raw one")
    argv = [str(cfg.openroad_netlist), str(cfg.pnr_dir),
            "--top", cfg.design.top_module,
            "--platform", str(p.get("platform", "asap7")),
            "--clock-port", str(p["clock_port"]),
            "--clock-period", str(p.get("clock_period",
                                        cfg.design.clock_period_ps)),
            "--clock-latency", str(p.get("clock_latency", 0))]
    if "utilization" in p:
        argv += ["--utilization", str(p["utilization"])]
    if "place_density" in p:
        argv += ["--place-density", str(p["place_density"])]
    for k, v in (p.get("set") or {}).items():
        argv += ["--set", f"{k}={v}"]
    if cfg.design.tie:
        argv += ["--tie-hi", cfg.design.tie["hi"],
                 "--tie-lo", cfg.design.tie["lo"]]
    print(f"[circt_flow] ORFS P&R -> {cfg.pnr_dir}")
    rc = pnr_mod.main(argv)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, ["orfs_pnr", *argv])
    return cfg.pnr_dir / "pnr_metrics.json"
