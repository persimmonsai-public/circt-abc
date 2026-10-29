# ABC recipe behind the `abc-bridge` pass

`abc-bridge` (`ABCBridge.cpp`) is the zero-Yosys path for technology
mapping: residual combinational logic goes to a real ABC subprocess as
AIGER, and ABC's mapped Verilog comes back into MLIR as instances of real
liberty cells. This file records the recipe it runs and why the mechanism
is built the way it is.

Two complete synthesis paths exist side by side:

- `scripts/run_circt_synth.sh` (M1a, Yosys delegation): `circt-verilog`
  elaborates, `circt-opt --lower-seq-to-sv --export-verilog` converts to
  plain behavioral Verilog, and a reference Yosys synthesis script
  (`synth.tcl`, run with an exact-version Yosys) does everything with its
  own `proc`/`memory`/`dfflibmap`/`abc` recipe.
- `scripts/run_circt_native_synth.sh` (M1b, zero Yosys):
  `register-legalize`/`register-mapper` (real liberty FF cells) +
  `outline-residual-logic` + `abc-bridge` + `--hw-flatten-modules`. Each
  outlined module is mapped by ABC in isolation, so it misses some of the
  cross-module sharing a single global flat run gets; the later
  merge-residual-modules work exists to recover it.

## The recipe

`abc-bridge` replicates the reference `synth.tcl`'s **high-effort** branch,
which is its real default (`ABC_HIGH_EFFORT` defaults to `"1"`). An
earlier transcription copied the low-effort branch instead, at roughly a
quarter of the real optimization effort, so every comparison against that
baseline has to use this one:

```
abc_script = "strash;scorr;dc2"
  + ";&get -n;&deepsyn -J 100 -I 4 -S 200 -T 10;&put"   # deep AIG restructuring
  + ";strash;&get -n;&dch -f;&if -C 32 -K 6;&put"        # &if-based exploration
  + ";strash;&get -n;&resub -N 2;&put"                   # resubstitution
  + ";strash;&get -n;&dch -f;&nf -C 32 -D $clk;&put"     # final area/timing map
  + ";strash;&get -n;&nf -D $clk;&put"                   # final cleanup
abc -D $abc_clock_period -liberty $synth_lib -script $abc_script
```

`synth.tcl` then runs `dfflibmap`/`simplemap` and two more full `abc`
invocations, but those exist to finish mapping registers and any generic
cells left around them. `abc-bridge`'s target is always purely
combinational (registers are already real liberty cells via
`register-mapper`), so only the final cleanup pass (the last line above)
is replicated, chained in the same ABC process since there is no
Yosys-level bookkeeping to redo in between.

`-C 32` is `ABC_CUT_SIZE` and `-f` on `&dch` is `ABC_DCH_FLAGS`, both
defaults of the reference script. Replicate the defaults first; don't add
knobs speculatively.

**Not replicated**: the conditional `amap`/`ABC_USE_AMAP` and retiming
branches. The reference runs leave both off, so they don't affect the
controlled comparison. Re-check against `synth.tcl` itself before
changing `ABCBridge` further: this file's own history is why "trust the
transcription" isn't enough.

**Known issue, not root-caused**: `&resub -N 2` reliably prints
`Error: Abc_FrameUpdateGia(): Transformation has failed.` on large merged
residual networks. The AND count before and after is unchanged, which
looks like a clean rollback rather than corruption, and SAT-equivalence
checks on the most exercised signals agree. (`&resub -N 2` appears to be
a no-op stub in this ABC: `Gia_ManResub2` returns NULL.)

## Why the bridge is built this way

- `synth-abc-runner`/`AIGERRunner` (circt-opt passes) round-trip through
  **AIGER only**. AIGER is a 2-input AND/INVERT graph format and cannot
  carry named standard-cell instances, so those passes are only useful
  for ABC's AIG-level restructuring (`&dch`, `&resub`, `dc2`, `scorr`),
  never for liberty-based cell mapping.
- `circt-synth` has no `--liberty` flag, and its
  `synth-tech-mapper`/`synth-generic-lut-mapper` passes take no cell
  library. `circt-synth in.mlir --top <mod> --convert-to-comb` does a full
  comb -> AIG -> comb round trip with native optimization, entirely in
  MLIR, but it does not produce named liberty cells.
- So the liberty mapping is a real ABC subprocess using the traditional
  `-liberty <lib> -script "<recipe>"` interface, whose `write_verilog`
  output names real cells with no MLIR body. Re-importing goes through
  `circt-verilog` itself rather than a bespoke netlist parser:
  `circt-verilog` handles blackbox instantiation correctly once given
  declarations, which `scripts/gen_liberty_blackbox_stubs.py` derives from
  the liberty's pin lists (covering every cell ABC might pick).
- **Use ABC's own liberty reader.** CIRCT's `ImportLiberty` and Yosys's
  `read_liberty` frontend can fail on real PDK liberty syntax (e.g.
  `time_unit : 1ns ;`). ABC has a separate internal reader (`read_lib`,
  what `-liberty <file>` uses, and what `synth.tcl`'s recipe depends on)
  that parses the same files fine.
- `yosys-abc` is built from the pinned Yosys tag (only the CMake
  `yosys-abc` target), so results match the reference Yosys exactly. See
  `third_party.lock` for provenance.

## The mechanism, step by step

Validated by hand on a small mux-shaped module with real tools before it
was automated:

1. `circt-synth in.mlir --top X --until-after=comb-lowering -o aig.mlir`:
   comb-to-AIG lowering, still in MLIR (`synth.aig.and_inv` ops).
2. `circt-translate --export-aiger aig.mlir -o out.aig`: a real AIGER
   file. Requires a single-hw.module input and **does not support
   `seq.firreg`/registers** (hard errors on `seq.to_clock`). That is the
   right architecture, not a gap: registers never enter the AIG path,
   exactly like `synth.tcl`'s `dfflibmap` maps them separately from the
   combinational `abc` path. Outlining must cut registers into free
   inputs/outputs before this step.
3. Real ABC with the recipe: `read_lib <liberty>; read_aiger <aig>; ...;
   write_verilog <out>.v`. **Use `read_aiger`, not the generic `read`**
   (its extension-based dispatch did not work), and put `read_lib` inside
   the script on one semicolon-separated line: a top-level `-liberty`
   flag combined with `-c "<multi-line script>"` failed silently, and
   embedded newlines triggered a confusing argument-parsing failure. On
   the mux test this produced a real 2:1 mux cell instance.
4. Re-import with `circt-verilog --ir-hw --top <name> mapped.v stubs.sv`
   (the generated stub file): a correct `hw.instance`.
5. **The re-imported cell module is not a true extern.** `circt-verilog`
   synthesizes a definite driver (constant 0) for the stub's unassigned
   output, giving it a body that looks like real logic.
   `--externalize-mapped-cells --cell-names-file=<names.txt>`
   (`ExternalizeMappedCellsPass.cpp`) converts each matching `hw.module`
   into a genuine `hw.module.extern`, reusing the rewrite of CIRCT's own
   `circt-reduce` `ModuleExternalizer` pattern (`HWReductions.cpp`).
6. Steps 1-5 are now the `abc-bridge` pass, run on every outlined residual
   module. Registers are cut out ahead of it by
   `register-legalize`/`register-mapper`, and `outline-residual-logic`'s
   `hw.instance`-is-kept boundary picks them up once they are real
   instances. Automating it on a real design surfaced further bugs, each
   described at its fix site in `ABCBridge.cpp`:
   - a false cycle in `OutlineResidualLogicPass`'s topological sort
     (register self-feedback looks cyclic to a generic dependency sorter
     once mapped, although an `hw.instance` is a real sequential
     boundary);
   - AIGER is bit-level, so word-level ports need an explicit MLIR-side
     extract/concat bridge after re-import;
   - aggregate-type legalization gaps (array-typed ports,
     `hw.struct_inject`) and `comb.icmp`'s wildcard-equality predicates;
   - pass nesting and visibility: `PassManager::addPass` needs an explicit
     `nest<hw::HWModuleOp>()` for anchor-specific passes, and a
     re-imported module defaults to public visibility, which silently
     defeats `--hw-flatten-modules`'s final inlining.
