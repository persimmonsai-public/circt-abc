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
| `doc/postlayout.md` | post-layout flavour recovery on a placed netlist and its parasitics |
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

* Post-layout flavour recovery (`doc/postlayout.md`, `pnr.post_layout`):
  on a placed netlist and its SPEF (written by a tech pack's own P&R
  command, or given to `python3 -m circt_flow.postlayout` from any P&R
  result; the built-in ORFS stage refuses the key), OpenSTA moves
  cells along the pack's flavour ladder to close setup and back down to give
  leakage back, with the clock network frozen and only footprint-equal swaps,
  so the placement stays (`eco.tcl` applies the swaps to the database).
  Options: sensitivity fix, leakage-ordered recovery, a ladder cap with a
  closing pass, pair recovery, hold tolerance, false paths to and from
  ports, a second parasitic view. The result's `final` timing is what a
  closure ladder judges. The synthesis-time `vt_swap:` takes the same leakage controls.
* P&R recipe options (`doc/techpacks.md`, `circt_flow/pnr_recipe.py`):
  minimum input delay and a given SDC (also on the ORFS stage), gated-clock
  net skipping, clock-gate sizing by measured arrival (optionally with a
  separate tolerance for gates that clock registers in the fan-in of an
  output port), root-buffer flattening, a
  don't-use list for the post-clock-tree repair, dual-corner hold repair,
  and a placement check that reads the logs for a failed legalization (also
  on the ORFS stage). The clock-tree and hold-corner options act only
  through a tech pack's own P&R command, which receives them as environment
  variables and sources the Tcl procedures in
  `circt_flow/pnr_tcl/cts_recipe.tcl`; the ORFS stage refuses them.
* Checks around a pack's P&R command: the pin-node capacitance OpenROAD's
  global-route SPEF writer leaves out is restored (`circt_flow/spef_pincap.py`,
  `pnr_tcl/spef_export.tcl`), an SDF with too few net-delay entries, or
  with nearly all of them zero, is reported (`circt_flow/sdf_tools.py`), and escaped names a SPEF reader may skip are
  reported (`circt_flow/netlist_names.py`).
* Closure ladder (`doc/ladder.md`, "Steps a flow adds"): code that runs the
  ladder can give it hooks: `finish_rung` after each rung's P&R result
  (raising `ladder.RungRejected` rejects the rung), `check_shared_placement`
  once on the shared placement before any rung, and `earlier_stage`, which
  refuses a laddered key that acts before the restart stage. The hooks are
  for code that runs the ladder from Python; the built-in stages use none of
  the three.
* Behaviour change: fan-out buffering now sees the member pins of a liberty
  `bundle` (a multibit register's `Q0..Qn`, `D0..Dn`) by default, so on a
  design with multibit banks the nets the banks drive are buffered like
  those of single-bit flops. `fanout_buffer: {bundle_pins: false}` restores
  the earlier behaviour; a liberty without bundle groups is unaffected.
* `multibit: {single: true}` (opt-in): the bits no bank takes become
  instances of a single-bit cell the pack declares for that class
  (`single: {cell, data, out}`), with the class's clock, reset and tie
  pins, instead of the liberty's plain flop.
* `multibit: {tie: {<pin>: 0|1|last_q}}` (opt-in): for a pin the pack's
  classes tie, gives the tie value in every class that ties that pin,
  instead of the pack's value.
* Behaviour change for packs with their own P&R command: after a
  successful command the flow rewrites in place a `*.spef` under the P&R
  directory when the export helper `spef_export_global_route` wrote it (a
  log there holds the helper's lines for the file's path, with the size and
  CRC-32 the file still has), its `*CAP` lines sum to
  less than its `*D_NET` totals (the pin-node capacitance OpenROAD's
  global-route estimate leaves out), it has `*CAP` entries and no coupling
  capacitors, and it parses completely; the original is kept as
  `<file>.spef.orig`. Every other SPEF is left as written and reported in
  one line. On by default; `pnr: {spef_restore_pincap: false}` turns it
  off. A file the check cannot read or parse is reported and never fails
  the stage.
* A pack command's environment now always carries `CTS_RECIPE_TCL`,
  `SPEF_EXPORT_TCL` and `SPEF_RESTORE_PINCAP`, and `hold_margin`,
  `setup_margin` and `cts_args` set `HOLD_MARGIN`, `SETUP_MARGIN` and
  `CTS_EXTRA_ARGS` for it; these replace variables of the same name in the
  calling shell.
* Fan-out buffering with `bundle_pins` (above) also counts the member
  input pins of bundled cells as loads.
* Closure-ladder placements and rung records made by the previous release
  are made again once after the upgrade.
* New warnings: escaped netlist names that a SPEF reader may skip (before a
  pack command), and an SDF with too few net-delay entries or with nearly
  all of them zero (after it).
* `pnr.hold_margin`, `pnr.setup_margin` and `vt_swap.margin_ps` must be
  finite numbers: an infinite or NaN value, passed on before, is now
  refused when the configuration is read (a quoted number is accepted as
  before). The new time keys are checked the same way.

## License

Apache-2.0 WITH LLVM-exception (see `LICENSE`). Patches to other projects are
under those projects' licenses (see `patches/README`).
