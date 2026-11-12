# circt-abc

A synthesis flow from SystemVerilog to a mapped standard-cell netlist built on
[CIRCT](https://github.com/llvm/circt) and [ABC](https://github.com/YosysHQ/abc),
with place and route on stock
[OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts)
(ORFS). The public example targets the open ASAP7 library.

The flow keeps the design in CIRCT's hw/comb/seq dialects from elaboration to
the mapped netlist. Registers and clock gates are mapped by passes in
`mapper/` that read their cells from the liberty (optional passes do the same
for adder and multiplexer structures); the remaining combinational logic goes
to ABC as an AIG and comes back mapped.

## Quick start

Build the image (OpenROAD, ORFS and the asap7 platform come from the pinned
ORFS base image; CIRCT, the mapper, ABC, emap-bridge and sv2v are built from
this tree):

```bash
docker build -t circt-abc .
```

Run ibex on ASAP7, synthesis and place and route:

```bash
mkdir -p out
docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -v $PWD/out:/out circt-abc \
  python3 -m circt_flow ../designs/ibex/asap7_flow.yaml /out/ibex --pnr
```

The RTL is fetched at the commit pinned in `designs/ibex/manifest.yaml`. The
mapped netlist is `out/ibex/1_synth/ibex_top_openroad.v`; ORFS's results are
under `out/ibex/pnr/`, summarised in `out/ibex/pnr/pnr_metrics.json`.

## Layout

| path | what |
| :--- | :--- |
| `mapper/` | the MLIR passes (`circt-abc-opt`), their lit tests, and `emap-bridge` |
| `scripts/circt_flow/` | the flow driver: front end, stages, ORFS P&R stage |
| `designs/` | design manifests (source, files, defines) and flow configs |
| `profiles/` | library profiles (ASAP7): liberties, ICG and tie cells, dont_use; other libraries come as tech packs |
| `examples/techpack-template/` | a runnable tech pack on an invented library, to copy |
| `doc/passes.md` | the pass reference: what each pass does, its options and flow switches |
| `examples/shift-select/` | a `case` over a shift amount, with `map-shift-select` off and on, on ASAP7 |
| `doc/techpacks.md` | tech packs: format, lookup, P&R from a pack, the leak audit |
| `doc/ladder.md` | the closure ladder: one placement, several variants of the later P&R stages in parallel, the lowest one that passes is the result |
| `patches/` | patches to CIRCT, ABC and mockturtle (see `patches/README`) |
| `scripts/verify/` | equivalence checks: register-mapper miter, top-level miter |
| `Dockerfile` | the circt-abc image |

## Other technologies: tech packs

A library the repository does not ship (a foundry PDK under NDA, say) plugs
in as a **tech pack**: a directory outside this repository with a
`techpack.yaml` (liberties, which may point into a PDK installed elsewhere,
the clock-gate and tie cells, dont_use patterns, optional flow defaults) and
the flow configs that use it. A flow config names the library,
`pdk: {profile: <name>}`, and the flow finds it in the pack enclosing the
config, on `CIRCT_ABC_TECH_PATH`, or as a `<name>-techpack` or
`circt-abc-techpack-<name>` checkout beside this repository.
`python3 -m circt_flow.techpack check <name>` validates one. A pack can also
set defaults for the flow's knobs and bring its own place and route: an ORFS
platform directory, or a command of its own.

`examples/techpack-template/` is a complete pack on an invented library to
copy as the start of yours, and the `circt-abc-techpack-asap7` repository,
published beside this one, is the ASAP7 pack (the same profile as
`profiles/asap7.yaml`). `circt-abc-techpack-gt3`, also beside it, is the
pack for GT3, an open 3-nm predictive PDK with no clock-gate or tie cell,
and builds its own ORFS platform. With the hook installed
(`git config core.hooksPath scripts/git-hooks`), `techpack audit` refuses
any commit that names a private pack's cells. The format, the lookup order,
P&R from a pack and the audit are in [`doc/techpacks.md`](doc/techpacks.md).

## Building without Docker

```bash
scripts/build-circt-from-source.sh   # CIRCT at third_party/CIRCT_REV + patches/circt
scripts/build-mapper.sh              # circt-abc-opt
```

`third_party.lock` lists every pinned dependency.

## Verification

`scripts/verify/register_mapper_miter.sh` proves the register mapping of a
design equivalent to its RTL registers with a bounded sequential miter; every
SUCCESS must be paired with a deliberately broken gate that FAILs.
`scripts/design_level_cec.sh` compares two mapped netlists.

## Status

Early. ibex runs end to end on ASAP7; CVA6 manifests and an ASAP7 baseline
against ORFS's own synthesis are next.

## Changes in this release

* The closure ladder (`doc/ladder.md`): one placement, several variants of a
  clock-tree-or-later P&R option run side by side, the lowest that passes its
  acceptance check is the result.
* Behaviour change outside the ladder: the ORFS stage drives one-bit constants
  from the tie cells in more places. It ties a one-bit literal that is an
  element of a concatenation (`{1'h0, a}`), a positional port argument, a pin
  connection, the right-hand side of a continuous assignment (also with a bit
  select, several targets or a delay) or of a net declaration (`wire w = 1'b1;`),
  in any base or spelling (`1'b0`, `1'sB1`, `1'd0`, `1 'h0_`). OpenROAD read such
  a literal as a power net and the router refused it. A one-bit literal in an
  expression (an operand, a ternary branch, a lone parenthesis, an event) is not
  rewritten; the stage warns about it. Literals in comments, strings, attributes,
  escaped identifiers, parameter overrides and `parameter` / `localparam` /
  `defparam` declarations, and in function, task, specify and primitive bodies
  and compiler directive lines, are not nets and are not rewritten or counted.
  A literal on an instance pin inside a `generate` body is not rewritten
  either, but it is counted and reported by the warning (the P&R tool may read
  it as a supply). An operand of an operator (`a == 1'b0`) is an expression:
  not rewritten, counted for the warning. For existing users: a netlist that has such literals gets
  extra tie cells and nets (instance count, area, power and placement change),
  and the cells are numbered `tie_const_<n>` in text order across the whole
  netlist, where the earlier code tied all `assign x = 1'h0;` statements first
  and then the pin literals: the numbers can shift even when no new kind of
  literal is present. A name the netlist already uses is skipped, each module of
  a multi-module netlist declares its own tie nets and cells, and a second pass
  over a tied netlist changes nothing. The same netlist is what a tech pack's own
  P&R command receives, so it gets the extra tie cells too. The ORFS stage's
  command line now carries absolute paths for the netlist, run directory,
  platform directory and wire-RC file.
* `cts_args` values that start with `-` (`-no_insertion_delay`) now reach the
  ORFS stage's parser, and so do negative margins (`hold_margin: -1e-05`): every
  option value is passed as `--option=value`; as a separate word argparse read
  them as options and the stage failed.

## License

Apache-2.0 WITH LLVM-exception (see `LICENSE`). Patches to other projects are
under those projects' licenses (see `patches/README`).
