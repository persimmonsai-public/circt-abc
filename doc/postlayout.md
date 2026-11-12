# Post-layout flavour recovery

`circt_flow/postlayout.py` changes cell flavours (threshold voltage, or any
ladder a pack declares in `vt_flavours:`) on a placed, clock-tree-built
netlist, timed with its parasitics. It closes setup where a faster flavour is
needed and gives leakage back where a slower one still meets timing. Only
cell types change: instances, nets and placement stay, so the result can be
applied to the placed database with the `eco.tcl` it writes.

What it needs to run: **either** a tech pack whose own P&R command
(`pnr.command`) leaves `placed.v` and `placed.spef` in the P&R directory (the
flow then runs the stage after the command; no pack in this repository has
such a command), **or** a placed netlist and its SPEF from any P&R flow, an
ORFS result for example, given to the command line below. The flow's
built-in ORFS stage does not write these files and refuses `pnr.post_layout`.

The synthesis-time swap (`vt_swap:`, [`techpacks.md`](techpacks.md)) times
an unplaced netlist with ideal clocks; this stage runs the same OpenSTA
fix and recovery loops after P&R, where wire load and clock skew are known.

## When it runs

In a flow, the stage is declared in the flow config's `pnr:` section and runs
after a tech pack's own P&R command (`pnr.command`):

```yaml
pnr:
  post_layout:
    margin_ps: 25          # ps (all *_ps keys are ps on every library)
    false_to: [outputs]
    recover_order: leak
    ladder_cap: L
```

The pack needs `vt_flavours:` (the ladder, slowest first), and its P&R
command leaves in the P&R directory:

| file | content |
| :--- | :--- |
| `placed.v` | the placed netlist, instance names as in the P&R database |
| `placed.spef` | its parasitics |
| `placed.lef.list` | optional: one LEF path per line (footprint check) |
| `constraint.sdc` | the SDC the flow wrote for the command (already there) |

When the command left no netlist or SPEF under those names (the pack's
`pnr.results`, else the defaults) the flow stops with an error naming them. With the ORFS stage the flow stops with an error when
`post_layout` is set; run the command line on the netlist and SPEF ORFS
wrote instead.

By hand, on any placed netlist and SPEF:

```
python3 -m circt_flow.postlayout --pack <pack.yaml> [--rail R] \
    --netlist placed.v --spef placed.spef --sdc constraints.sdc \
    --top <module> --out <dir> [--lef a.lef ...] [options]
python3 -m circt_flow.postlayout ... --measure-only   # timing only
```

OpenSTA runs from `$OPENSTA` when it is set, else from the ORFS image.

On an ORFS result (`<run>/results/<platform>/<design>/<variant>/` of a
completed run) the inputs are:

| option | ORFS file |
| :--- | :--- |
| `--netlist` | `6_final.v` (written without physical-only cells such as fillers and tap cells) |
| `--spef` | `6_final.spef` (OpenRCX, written when the platform has extraction rules, as ASAP7 has; it carries coupling capacitors, so the pin-capacitance restore leaves it as it is) |
| `--sdc` | `6_final.sdc` |
| `--lef` | the platform's cell LEFs (for ASAP7 the `lef/asap7sc7p5t_28_*_1x_*.lef` of every flavour in the pack), for the footprint check |
| `--pack` | a pack with `vt_flavours:` for those libraries, e.g. `examples/asap7-mvt/techpack.yaml` |

`eco.tcl` swaps masters in the OpenROAD database it is sourced into
(`6_final.odb` loaded in `openroad`): every flavour it moves cells to must
have its LEF in that database (ORFS loads the other flavours' LEFs only
when the platform is configured for them), or the swap fails and is
reported (`ECO FAIL`).

## What the stage does

One OpenSTA session reads the setup-corner libraries (and the pack's hold
corner, when it has one, as a second corner), the netlist, the SDC with the
stage's own additions, and the SPEF; clocks are propagated.

* **Frozen clock network.** Every cell with an output pin on a clock (clock
  buffers, clock gates) keeps its cell, so skew does not move.
* **Footprint rule.** A cell goes to another flavour only when both have
  the same LEF size (with `--lef` or `placed.lef.list`; a cell the LEFs do
  not list is not swapped) or, without LEFs, the same liberty area.
  `allow_footprint_change: true` lifts the rule; the record then lists the
  size changes.
* **Fix.** While the worst setup slack is below `margin_ps`, cells on the
  failing paths move up the ladder. `fix: sens` (default) tries each
  candidate alone and moves the best `sens_frac` of them by slack gained per
  leakage added; `fix: bulk` moves every cell of the worst `paths` paths one
  step. `ladder_cap` names the highest token the fix may use.
* **Closing pass.** With `ladder_escalate` set, when the capped fix leaves
  the worst slack below `close_margin_ps`, the cap is lifted to that token
  and a sensitivity fix runs once more on the failing paths only.
* **Recovery** (`recover: true`). Cells step down the ladder in batches
  that stay only if the worst slack holds. `recover_order: leak` tries the
  highest-leakage cells first; `inst` keeps netlist order.
  `recover_swap: N` then tries the N highest-leakage cells that could not
  move down alone once more, each paired with one cell of the worst path that
  moves up for less leakage than the pair saves; a pair stays only if the
  worst slack holds.
* **Hold.** A move that takes a hold endpoint below
  `min(0, its slack at the start) - hold_tol_ps` is undone and that cell is
  frozen. `hold_scope: r2r` (default) watches register-to-register hold,
  `all` every hold endpoint.
* **False paths.** `false_from` (input ports) and `false_to` (output
  ports, or `outputs`) are not optimised; the before/after measurements
  still include them.
* **Parasitics.** OpenROAD's global-route SPEF writer leaves the pin-node
  capacitance out of the `*CAP` lines. With `pnr.spef_restore_pincap: true`
  (default; the same key that restores the command's exports) the stage times a copy with that capacitance put back
  (`circt_flow/spef_pincap.py`); `false` times the file as written and
  warns. `also_spef: <file>` (relative to the flow config) adds a second
  parasitic view, for example a placement estimate next to a global-route
  one, as a third OpenSTA corner: fix and recovery keep the worst slack over
  both views. It gets the same pin-capacitance restore as the first SPEF.
  It needs a pack hold corner (refused without one), and the before/after
  measurements in `eco.json` use the first SPEF only.

## Keys of `pnr.post_layout`

`post_layout: true` uses the defaults; `false` (or no section) turns it off.

| key | default | meaning |
| :--- | :--- | :--- |
| `enabled` | true | |
| `margin_ps` | 0 | setup slack the fix works to |
| `paths` | 1000 | failing paths looked at per iteration |
| `max_iter` | 40 | fix iterations |
| `recover` | true | recovery after the fix |
| `fix` | `sens` | `sens` or `bulk` |
| `sens_frac` | 0.25 | share of the useful candidates moved per iteration |
| `recover_order` | `inst` | `inst` or `leak` |
| `ladder_cap` | (none) | highest token the fix may use |
| `ladder_escalate` | (none) | token the closing pass may use |
| `close_margin_ps` | 0 | slack the closing pass works to |
| `recover_swap` | 0 | pair recovery over this many cells (0 = off) |
| `hold_scope` | `r2r` | `r2r` or `all` |
| `hold_tol_ps` | 0 | hold slack a move may lose below the endpoint's floor |
| `false_from` | [] | input ports not optimised |
| `false_to` | [] | output ports (or `outputs`) not optimised |
| `allow_footprint_change` | false | lift the footprint rule |
| `also_spef` | (none) | a second SPEF timed as a third corner |

Times are in ps and converted to the libraries' time unit. The command line
takes the same options (`--margin-ps`, `--recover-order`, `--ladder-cap`,
`--ladder-escalate`, `--close-margin-ps`, `--recover-swap`, `--hold-tol-ps`,
`--false-from`, `--false-to`, `--also-spef`, `--no-spef-restore-pincap`, ...; see `--help`).

The same leakage controls exist for the synthesis-time swap: the flow
config's `vt_swap:` takes `fix`, `sens_frac`, `recover_order`, `ladder_cap`,
`ladder_escalate`, `close_margin_ps` and `allow_footprint_change` (with an
unplaced netlist, the wire model standing in for the SPEF). Absent, the
synthesis-time swap is unchanged (`fix: bulk`, netlist order, no cap, the
same generated script). Any one of them makes the synthesis-time swap run
with this stage's machinery, which also changes, for that run:

* hold protection: register-to-register hold paths only, each endpoint
  held at min(0, its slack at the start) (instead of the worst hold slack of
  all paths at min(0, its start value)), and the moves on newly failing
  hold paths undone for up to six rounds per iteration;
* acceptance of a fix iteration: one that lowers the total negative slack
  while the worst slack stays is kept;
* flavour steps: only between cells of equal liberty area, unless
  `allow_footprint_change: true`.

The clock network is not frozen (the netlist is unplaced); `keep_clock_cells`
applies to every fix and recovery step either way.

## Output

In `<pnr>/post_layout/`:

* `<top>_postopt.v`: the input netlist with only the swapped instances'
  cell types changed;
* `eco.tcl`: the same swaps for an open OpenROAD database (`replace_cell`,
  or the master swapped in the database where the resizer refuses);
* `eco.json`: every swap, the vetoes (`eco_stat`), the cells held for hold,
  and before/after worst and total setup slack, hold slack,
  register-to-register violators, input-to-register hold, leakage and area
  (state-averaged liberty leakage);
* `paths_before.rpt`, `paths_after.rpt`: the worst setup paths.

`pnr_metrics.json` gets a `post_layout` summary and a `final` mapping
(`setup_ws`, `setup_tns`, `hold_ws`, `hold_tns`): the stage's own
measurement after its swaps, over the corners it defines, null where it has
no finite number; a `final` the command wrote itself keeps its other keys.
The command's own top-level numbers stay; a closure ladder
([`ladder.md`](ladder.md)) judges `final` over them. `final` is the timing
of the swapped netlist (`<top>_postopt.v`): the command's layout database
does not contain the swaps until `eco.tcl` is applied to it, so an accepted
rung's database is not yet the accepted netlist.

## Checks around a pack's P&R command

Before the command, the flow warns when the netlist has escaped identifiers
a SPEF reader may skip (`circt_flow/netlist_names.py`). After the command,
and before the stage, it also:

* restores the pin-node capacitance of each global-route SPEF the command
  exported with `spef_export_global_route` (`pnr_tcl/spef_export.tcl`;
  `pnr.spef_restore_pincap`, default true; other SPEFs, and files it cannot
  parse, are left as written and reported);
* warns about an SDF with too few net-delay entries, or with nearly all of
  them zero, and about one it cannot read (`circt_flow/sdf_tools.py
  health`);
* with `pnr.placement_check: warn|fail`, reads the logs for a failed
  legalization (`circt_flow/placement_health.py`); this also works on the
  ORFS stage.
