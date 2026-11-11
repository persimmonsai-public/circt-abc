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
