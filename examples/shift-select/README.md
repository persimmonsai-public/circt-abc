# shift-select: a `case` over a shift amount, as cells

`align_unit` (`rtl/align_unit.sv`) shifts a 16-bit word left by a 4-bit amount
into a 32-bit result, written as a `case` with one arm per amount and registered
at both ends. It is the shape `map-shift-select` recognises: a select over
distinct constants of one amount, over shifted copies of one word. This example
runs it through the flow on ASAP7 RVT with the pass off (`base`) and on (`shift`),
compares cells, area and depth, proves the two netlists equal, and checks that
two runs give the same netlist.

```bash
examples/shift-select/run.sh ~/runs/shift-select                  # about 15 s
examples/shift-select/run.sh ~/runs/shift-select --determinism    # + a second shift run
```

The libraries are ASAP7's (the ORFS platform's NLDM files, built into `lib/` by
`examples/asap7-mvt/lib/make_libs.py`; nothing in `lib/` is committed). The pack,
`techpack.yaml`, is one flavour (RVT) with load-aware timing-driven mapping
(`abc/rvt.abc`).

## What the pass is

A `case` over a shift amount reaches the mapper as a priority mux chain, or a
one-hot select, over shifted copies of one word. Left to ABC it comes back as a
logarithmic shifter: every stage is a row of 2:1 muxes, and a change of the
data or of the amount switches the stages in a cascade. `map-shift-select`
builds the select instead as a mixed-radix network of AND-OR cells: the amount
is split into digits (here three digits of radix 2 and a last digit that takes the rest, for the 16 values), each
digit is decoded once, and each result bit is an AND-OR of a few decoded
selects and data bits. ABC treats the cells as boundaries and maps the logic
around them. See `doc/passes.md` for the options. ASAP7 has three-term AND-OR
cells (AO222, AOI222) but no four-term cell, so `flow_shift.yaml` runs with
`wide: 1`; a library with four-term cells would use `wide: 2`.

`flow_shift.yaml`:

```yaml
shift_select:
  min_arms: 8
  wide: 1
  recipes: "2,2,2@3,3,3,2"    # digit radices @ the AND-OR terms per cell, per level
```

All options used here are exact (the function is unchanged for every input);
`default_dont_care`, the one that is not, is off.

## Result (this run, before P&R)

One run of `run.sh` (OpenSTA 3.1.0 in the ORFS image, mapper and ABC of this
tree). Cells and area are of the synthesized netlist `1_synth/align_unit.v`
(before the fanout stage), area is the liberty's real area, depth is the
longest chain of combinational cells between flip-flops (`report.py`), WNS is
OpenSTA's setup slack at the tt corner for a 400 ps clock.

| arm | cells | of which combinational | area (um2) | combinational area (um2) | depth (cells) | setup WNS at tt |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 249 | 198 | 43.244 | 23.911 | 6 | +109.0 ps |
| shift | 196 | 145 | 28.737 | 9.404 | 6 | +126.1 ps |

On this design the select as cells is smaller (combinational area 61% lower)
at the same depth. What the pass is for is also the switching activity of the
shifter (its stages no longer toggle in a cascade), which neither area nor
depth measures and this example does not measure. How well it does depends on
the recipe and the library: `recipes` takes several, separated by `;`, and the
pass builds the one of least cell area; treat the recipe as something to search.

## Checks

* **Equivalence.** `run.sh` reads both netlists with ASAP7's liberty (cell
  functions), builds a miter of the whole design with Yosys and proves it for four
  cycles from the all-zero state (`async2sync; sat -seq 4 -set-init-zero`). The
  design has two register stages, so four cycles cover every input sequence that
  reaches an output. A negative control changes one NOR2 into a NAND2 in the shift
  netlist and the proof must then fail.
* **Determinism.** `--determinism` runs the shift arm twice and compares the two
  netlists (the location comments of `export-verilog`, which name the output
  directory, aside). `scripts/circt_flow/tests/test_shift_example.py` does the same
  in the test suite (skipped when the libraries are not built).

Clock gating is switched off in this example (`CIRCT_FLOW_NO_CLOCK_GATING=1`): a
gated clock is outside what a Yosys miter proves, and the example is about the
select. `CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1` makes the netlist one flat module, which
a design whose top has no sub-modules needs for the fanout stage and OpenSTA.

## Closure ladder

`ladder.sh` runs `align_unit` through place and route with the flow's closure
ladder (`doc/ladder.md`): the placement is made once, then one run of the clock
tree, routing and the acceptance check per hold margin, all at the same time,
and the lowest margin whose check passes is the result.

```bash
examples/shift-select/ladder.sh ~/runs/ladder       # ORFS needed
```

`designs/align_unit/flow_ladder.yaml` declares it:

```yaml
pnr:
  ladder:
    param: hold_margin
    values: [0, 10, 20, 30]          # ps
    accept:
      metrics:
        setup_ws: {min: 0}
        hold_ws: {min: 20}           # illustrative threshold, ps
        drc_errors: {max: 0}
      corners:                       # the routed result timed at the fast corner
        ff: {libs: [...], setup_ws: {min: 0}, hold_ws: {min: 0}}
```

**This is a demonstration with an illustrative threshold.** ORFS's hold repair
may already leave a positive hold slack at margin 0, in which case the plain
checks (slack at least 0) accept the first rung and there is nothing for a
ladder to do. The acceptance asks for 20 ps of hold slack instead, a threshold
picked only so that more than one rung is needed. The script runs the ladder
twice from the same synthesized netlist, sequentially (rung after rung, from
one placement) and in parallel, then a straight run (no ladder, no restart) at
the accepted margin, and checks that the three results are the same.

With the thresholds of `flow_ladder.yaml` the rungs end like this:

| hold margin | verdict |
| ---: | :--- |
| 0 ps | rejected: hold slack below the 20 ps threshold |
| 10 ps | rejected: hold slack below the 20 ps threshold |
| 20 ps | **accepted** |
| 30 ps | not run (sequential); in parallel either stopped as not needed or finished, and not used |

The fast-corner check starts together with the plain checks and is stopped as
soon as one of them rejects the rung.

The accepted rung is 20 ps in both modes. The parallel ladder needs the
wall-clock of the placement plus one rung where the sequential one needs the
placement plus the rungs up to the accepted one, one after the other (the script
prints both). In the parallel ladder a rung above the accepted one is stopped
if it is still running when the outcome is fixed; if it has already finished,
its result is not used. The accepted results of the sequential ladder,
the parallel ladder and the straight run are identical: `4_cts.odb`,
`5_route.odb`, `6_final.odb`, `6_final.v`, `6_final.def`, `6_final.sdc`,
`6_final.spef` (but for its date line) and `pnr_metrics.json`, byte for byte.
A restart from the placement is the same as a straight run; the rungs differ
only in what happens after the placement. The CPU time of the parallel run is
that of every rung that started, including those above the accepted one, in exchange for the
shorter wall-clock.

On a cluster, each job can go through a scheduler instead of a local process:
`CIRCT_FLOW_LADDER_SUBMIT='sbatch --wait -c 16 --job-name={name} -o {log} --wrap {cmdq}'`
(see "Where jobs run" in `doc/ladder.md`).
