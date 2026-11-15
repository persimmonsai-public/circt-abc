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
| `doc/options.md` | every option of the flow: name, type, default, what it binds to, the legacy variable it replaces (generated) |
| `doc/pack-options.md` | a tech pack's own P&R options: the registry file, `options --pack`, the session's `set_pack_option` |
| `doc/scripting.md` | the Python session API: recipes as scripts, stages one at a time, object queries, scoped commands, the editable pass pipeline |
| `examples/scripting/` | Python recipes on the `examples/mvt-mini` design `core_lv` |
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

* Option registry (`scripts/circt_flow/options.py`, reference
  `doc/options.md`): every switch of the flow is a named, typed option
  (`syn.*`, `pnr.*`, `cts.*`, `hold.*`, `post.*`, `run.*`) with its default,
  range or choices and what it binds to. It is set with `--set NAME=VALUE`,
  a flow config's `options:` mapping, a tech pack's `flow.options:`, or
  replayed from an earlier run with `--settings <settings.json>`. Every run
  writes `settings.json`: the value and source of every option, the tool
  commits, the hashes of the recipe files and the commands as executed.
  `python3 -m circt_flow options [PATTERN]` lists the options,
  `--legacy` the variable-to-option table, `--diff A B` what differs
  between two runs' records.
* Deprecation: the `CIRCT_FLOW_*` / `CIRCT_VT_*` environment variables are
  accepted, with one warning each naming the option that replaces it, in
  this release and the next; from release 13 on they are refused. `--strict`
  refuses them now, and refuses any unknown name with those prefixes.
* Behaviour changes of the legacy variables: a flag variable must be `1` or
  `0` (any other value is an error; earlier releases read it as off, or as on for a
  default-on flag); a number has no blank and no leading zero. A tech pack's
  `flow.env` default now ranks below the flow config and the environment
  (it was copied into the process environment, above the flow config's
  `shift_select:` keys, and inherited by child processes).
* Python session API (`doc/scripting.md`, `examples/scripting/`):
  `python3 -m circt_flow run RECIPE.py FLOW.yaml OUT` runs a recipe written
  as a Python script and `python3 -m circt_flow shell FLOW.yaml OUT` opens
  a Python shell with the session loaded. Options are typed attributes
  (`s.db.syn.fanout.max = 12`, validated at assignment); the flow is seven
  stages that run one at a time or together, each returning a result with
  its outputs, metrics, settings and commands; single passes run with
  `s.run_pass`; a run directory resumes with `Session.resume`, which refuses
  inputs that changed since (recipe files, legacy variables, the circt-abc
  commit) unless told to continue. `s.run()` is
  the command line's code path and writes the same netlists.
* Object queries and scoped commands: registers, register bit ranges,
  adders, multipliers, shifters, clock gates and mapped cells are queried
  from the flow's own stage files (`s.flops`, `s.adders`, `s.icgs`, ...),
  and `s.gate_register` / `s.map_adders` apply a command to a selection
  (option `syn.scoped`). Each target is checked against the design before
  the structural stage and against the passes' reports after it; a target
  that does not apply is refused, and `settings.json` records each one.
* A tech pack's own P&R options (`doc/pack-options.md`): a pack with its
  own P&R command may declare the command's options in a JSON registry
  (`pnr.settings_registry` of its techpack.yaml). `python3 -m circt_flow
  options --pack <pack>` lists them, a session sets them
  (`s.set_pack_option`, validated against the registry) and the flow hands
  them to the command as `--settings FILE`, also through the closure
  ladder; a pack profile can preset them (`pnr.settings_defaults`), and
  `s.pack_options` / `s.write_pack_settings()` show what is handed over,
  presets included, with `s.pack_option_sources` saying where each value
  comes from. Drift rules check the registry against the flow's
  own options. `examples/techpack-template` has a registry and a command
  that reads it.
* Editable pass pipeline and hooks: `s.pipeline` lists the passes each
  stage will run (obtained without running a tool), edits insert, remove,
  replace or move a pass, and hooks call Python before or after a stage or
  a pass (the stage's command is split at that pass). Edits are recorded
  and replayed; a run with hooks is marked as not reproducible from its
  record alone. Without edits and hooks the commands are unchanged.
* New mapper options, all off by default (`doc/passes.md`):
  `data-gate-enables` (experimental: named register slices hold when
  loading would not change them, so bit-level clock gating gives them a
  gate whose enable follows the data); `map-prefix-adders` `boundary` and
  `isolate-inputs`, and a second instance of `map-prefix-adders` and of
  `map-adder-chains` for another width range; `map-compressor-cells`
  `tree=wallace|array`, `gate-compressors`, `cell-stages`, `nand-pp`,
  `map-final-adder`, `map-final-min-width`; `map-shift-select`
  `l2-range-gate`, `l2-range-aligned`, `l2-range-compose`,
  `select-dup-fanout` and the window form (`window-form` and its
  `window-*` options). The new shifter options are exact.
* Leakage swap: `flop_cap` (in `vt_swap:` and `pnr.post_layout:`) keeps
  flops at or below a token of the pack's flavour ladder;
  `pnr.post_layout.recover_opts` (`seq_first`, `seq_pair`, `comp=N`,
  `pass=N`) refines the leakage recovery, and `recover_block:
  "<regex>@<ps>"` adds a recovery pass for the cells of one block with a
  slack floor of its own (at most `margin_ps`).

## License

Apache-2.0 WITH LLVM-exception (see `LICENSE`). Patches to other projects are
under those projects' licenses (see `patches/README`).
