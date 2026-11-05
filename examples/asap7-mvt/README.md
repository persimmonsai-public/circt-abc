# asap7-mvt: multi-Vt synthesis on a real open library

ibex_top on ASAP7's three threshold flavours (RVT, LVT, SLVT), through
circt-abc's flow: a leakage-priced multi-Vt library for the mapper, then
the flow's `vt_swap` stage, against single-flavour controls. ASAP7 is the
open predictive 7 nm PDK (BSD-3). The libraries are the ORFS asap7
platform's NLDM ones, built into `lib/` by `lib/make_libs.py` from the ORFS
image, so nothing in `lib/` is committed. Every pack says `public: true`.

It is the real-library companion to `examples/mvt-mini`. ASAP7 has one
supply, so multi-rail and power domains stay on mvt-mini.

```bash
examples/asap7-mvt/run.sh ~/runs/asap7-mvt              # four arms, 3 to 6 min
examples/asap7-mvt/run.sh ~/runs/asap7-mvt --only mvt -W 0.001
# or one arm with the flow alone (from scripts/, after run.sh built lib/):
python3 -m circt_flow ../examples/asap7-mvt/designs/ibex/flow_mvt.yaml ~/runs/ibex_mvt --sta --vt-swap
```

`run.sh` builds the libraries, checks the four packs and runs four arms in
parallel, each with the flow's STA stage at tt, ss and ff:

| arm | pack | what |
| :--- | :--- | :--- |
| `rvt` | `techpack_rvt.yaml` | RVT only, mapped load-aware and timing-driven (`abc/mvt.abc`) |
| `lvt` | `techpack_lvt.yaml` | LVT only, the same way |
| `slvt` | `techpack_slvt.yaml` | SLVT only, the same way |
| `mvt` | `techpack.yaml` | the leakage-priced multi-Vt library, then `vt_swap` |

Each arm's netlist is reported per flavour (`examples/mvt-mini/vt_report.py`:
cells, real area, leakage) at tt and at ss. The run then checks that the
`mvt` arm meets setup at tt and hold at ff, and that every single-flavour
arm that meets setup leaks more at ss than `mvt`.

## Corners

The platform's three: tt (0.7 V, 25 C), ss (0.63 V, 100 C), ff (0.77 V,
0 C). The packs time setup at tt, as the platform's tt-centred flows do.
Leakage is priced and compared at ss: at 100 C it is ASAP7's leakiest
corner, 13.7x tt for RVT and 6.1x for SLVT, while ff is 0 C and leaks
less than tt (0.67x for the RVT netlist, 0.64x for SLVT). Hold is checked
at ff, the fastest corner.

ASAP7's flavours are ten times apart in leakage at tt (RVT : LVT : SLVT
1 : 9.6 : 97 per cell, at the same area). At ss, SLVT's ratio to RVT
drops to 43.

## Result (ibex_top at 1500 ps)

From `run.sh` with its defaults, OpenSTA in the ORFS image, ideal clocks,
before placement. Each cell is the median of six runs, with the range
(min to max) in brackets; the runs are noisy (see Run-to-run noise):

| arm | WNS at tt | hold at ff | leakage at ss | leakage at tt | area um^2 | flavours (median) |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| rvt | -658 ps (-706 to -610) | +2.8 ps | 21.8 uW (21.6 to 21.9) | 1.59 uW (1.58 to 1.59) | 1,583 (1,577 to 1,584) | R 10,132 |
| lvt | -170 ps (-212 to -121) | +0.7 ps | 147 uW (147 to 150) | 15.1 uW (15.1 to 15.3) | 1,578 (1,574 to 1,587) | L 10,054 |
| slvt | -18 ps or +39 to +49 ps (three runs each) | +5.2 ps | 946 uW (937 to 948) | 154 uW (152 to 154) | 1,582 (1,572 to 1,583) | SL 10,105 |
| mvt | +0.1 ps (+0.1 to +0.4) | +2.8 ps | 27.2 uW (26.8 to 28.2) | 2.48 uW (2.44 to 2.64) | 1,600 (1,596 to 1,605) | R 10,126, L 53, SL 66 |

(Over thirteen mvt runs in all, the six above, four more of the image and
three of a local build: leakage at ss 26.8 to 28.2 uW with a median of
27.3, area 1,596 to 1,605 um^2.)

At this clock the multi-Vt arm meets setup in every run, SLVT in three of
six (+49, +49 and +39 ps; the other three miss by 18 ps), RVT and LVT in
none. The multi-Vt netlist leaks about 35x less than all-SLVT at ss (33x to
35x), 1.25x the all-RVT netlist (1.23x to 1.31x) that misses timing by 610
to 706 ps, for about 1% more area than all-SLVT (0.8% to 2.1%): it is RVT
except 99 to 152 cells on the critical paths (median 119).

How the `mvt` arm gets there: ABC maps on the priced library (a cell's
price is its area plus W times its ss leakage) with a target of half the
period less the flop overhead. It uses 26 to 34 LVT and 21 to 29 SLVT cells
(medians 32 and 25) and ends at -427 to -592 ps (median -438).
`vt_swap` then makes 954 to 1,391 swaps to faster flavours on failing paths
(median 1,105; +2.0 to +9.3 ps after them) and 868 to 1,255 back toward
RVT where slack allows (median 1,001), finishing at +0.1 to +0.4 ps, with
hold at ff unchanged (+2.8 ps before and after in every run).

### Run-to-run noise

The `mvt.abc` script runs `&deepsyn -T 10`, which stops after 10 seconds of
wall clock, so what ABC maps depends on how many of its (seeded,
deterministic) iterations the machine finished: the load on the machine
shows up in the netlist. The outcomes are discrete: in thirteen `mvt` runs
at the default W there were five distinct mappings (ABC's flavours, swaps
and leakage came out the same to the digit within each), and the most common
one (26.85 uW, 954 swaps up and 868 down, 1,596 um^2) is 6 of the 13 runs.
Under `-T 1` three runs gave one and the same netlist; under `-T 30` two of
three did, a different one from the `-T 10` ones. The ORFS image and a
local build, with different ABC and CIRCT binaries but the same libraries
and OpenSTA (3.1.0), give the same set of outcomes. The single-flavour arms
move too: RVT between -610 and -706 ps, LVT -121 to -212, SLVT -18 to +49.
Numbers in this README are therefore a median and a range, and a re-run
that lands anywhere in the range is not a regression.

The clock is where the flow is today on ASAP7, not where ibex can go:
ORFS's own asap7/ibex design is constrained at 1000 ps. ABC's load-aware delay
model is optimistic on these libraries (on the RVT arm `&nf` ends at 0.98 to
1.03 ns, relaxing its 704 ps target, where OpenSTA puts the paths at 2.11 to
2.21 ns), and the worst paths are long carry-style chains. Closing that gap
is separate work. At 1300 ps the `mvt` arm misses by about 180 ps (106 to
198 over three runs), at 1200 by about 210 (193 to 263; W 0.0001).

## Calibration

`mapping_target.scale` (target = scale x (period - flop overhead)), on ibex
with the multi-Vt pack (W 0.0001) and `vt_swap`, WNS at tt after the swap,
median of three runs (min to max):

| period | scale 0.5 | scale 0.75 | scale 1.0 |
| :--- | ---: | ---: | ---: |
| 1500 ps | +0.2 ps | -30 ps (-224 to -30) | -108 ps (-149 to -108) |
| 1800 ps | +0.1 ps (+0.1 to +0.2) | +0.4 ps | +0.3 ps |
| 2000 ps | +0.3 ps (+0.3 to +1.5) | +0.0 ps (+0.0 to +1.0) | +0.2 ps |

A cell with one value was the same in all three runs. 0.5 is the packs'
default, so the controls get the same target. On the RVT control the scale
matters little: at 1000 ps, scales 0.3, 0.5, 0.75 and 1.0 give medians of
-1.16, -1.11, -1.16 and -1.15 ns (-1.11 to -1.21 over the twelve runs),
since ABC cannot reach any of those targets with RVT alone and relaxes
them to its own best delay.

The flop overhead is the worst clock-to-Q + setup over the flops the
mapper may use (90.7 ps here). The platform's scan flops (`SDF*`,
`dont_use`) are slower and are not counted.

W, the leakage price in um^2 per pW (`run.sh -W`), `mvt` arm at 1500 ps,
median of three runs (six for the default; min to max where they differ):

| W | ABC's flavours | after vt_swap | WNS at tt | leakage at ss | area um^2 |
| :--- | :--- | :--- | ---: | ---: | ---: |
| 0 | R 2,192, SL 7,927 | R 9,890, L 149, SL 80 | +0.0 ps (-18.1 to +0.0) | 28.7 uW (28.7 to 30.4) | 1,580 (1,580 to 1,583) |
| 0.00001 (default) | R 10,189, L 32, SL 25 | R 10,126, L 53, SL 66 | +0.1 ps (+0.1 to +0.4) | 27.2 uW (26.8 to 28.2) | 1,600 (1,596 to 1,605) |
| 0.0001 | R 10,403, L 30, SL 34 | R 10,289, L 99, SL 79 | +0.2 ps | 28.2 uW (27.9 to 28.2) | 1,639 (1,639 to 1,641) |
| 0.001 | R 10,712, L 28, SL 22 | R 10,521, L 150, SL 91 | +0.1 ps | 28.5 uW | 1,685 |

The flavour counts are medians; the ranges of the swapped counts are R 9,861
to 9,890, L 149 to 194, SL 80 to 93 at W = 0, and R 10,289 to 10,383, L 83
to 99, SL 79 to 82 at 0.0001; ABC's own at W = 0 are R 2,192 to 2,214 and SL
7,927 to 7,934. W = 0.001 gave the same netlist in all three runs.

Every W meets timing here in all but one run (W = 0, once, by 18 ps),
because `vt_swap` fixes what the mapping leaves and recovers what it
overspends: with W = 0, ABC maps 78% of the cells SLVT, and the swap's
recovery moves nearly all of them back. A price makes the mapping start
close to the answer, and the swap then has less to do (a median of 1,105
swaps up and 1,001 down at the default W against 11 and 15,556 at W = 0).
Higher prices bend the mapping toward larger structures (more area) for
little leakage.

## P&R

`--pnr` hands each arm's netlist to stock ORFS (asap7 platform, 1500 ps,
utilization 40, `CORNER=TC` so the platform times at tt like the packs;
ORFS's own SDC template, 20% I/O delays, real CTS). Every flow config has
a `pnr:` section; `ASAP7_USE_VT` tells the platform which flavours' LEFs
and libraries to load. Numbers are from ORFS's final report (post-route,
SPEF parasitics), leakage from its `report_power`:

```bash
python3 -m circt_flow examples/asap7-mvt/designs/ibex/flow_mvt.yaml ~/runs/ibex_mvt_pnr --sta --vt-swap --pnr
```

Each arm on the platform with only its own flavour (`pnr.set.ASAP7_USE_VT`
as in the configs; the controls also set `pnr.tie_hi/tie_lo` to their
flavour's tie cells), `mvt` with all three:

| arm | setup WNS | TNS | hold WS | leakage | area um^2 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| rvt | -448 ps | -49,642 ps | +43 ps | 2.0 uW | 2,021 |
| lvt | -43 ps | -152 ps | +28 ps | 17.1 uW | 1,926 |
| slvt | +32 ps | 0 | +24 ps | 171 uW | 1,906 |
| mvt | +22 ps | 0 | +43 ps | 16.5 uW | 1,960 |

0 DRC errors in all four. Area is the placed instances (with taps, tie
cells, buffers); the synthesis areas were 1,572 to 1,605.

**The flavours do not survive placement as mapped.** OpenROAD's
`repair_timing` has its own Vt swap move (`VtSwapMove`, run in floorplan
and again after global routing, hard-coded in ORFS's `floorplan.tcl` and
`global_route.tcl`), and its buffers are chosen from every flavour loaded.
In the `mvt` netlist (synthesis R 10,108, L 36, SL 63): 501 of the 10,007
cells that keep their names change flavour (469 R to SL, 17 R to L, 15 L to
SL), none go back toward RVT (the move only speeds up), 3,966 cells are
new (2,228 tie cells, the rest mostly buffers; by flavour 2,760 R, 985 L, 221 SL), and the
final netlist is R 12,182, L 1,023, SL 768. By the synthesis report's
averaged-leakage method that is 19.5 uW at tt against 2.44 uW before
placement. `repair_design` itself only resizes and buffers; the flavour
changes come from `repair_timing`. `SKIP_VT_SWAP=1 SKIP_CRIT_VT_SWAP=1`
alone does not stop them (591 flavour changes, same timing): they switch
the swap off in the floorplan, CTS and global-route `repair_timing` calls
(logged as `-skip_vt_swap -skip_crit_vt_swap`), but `global_route.tcl` then
runs one more `repair_timing -setup -sequence "vt_swap reroute"`, switched
by `OPT_POST_GRT_WNS` (default 1) and not by the SKIP flags, and that one
makes 576 R to SL swaps. Nothing is dont_touch.

With the platform loading all three flavours the `rvt` control is no
longer RVT-only either: its 10,048 RVT cells end as R 12,023, L 1,280,
SL 801 (post-route WNS +25.6 ps, 18.1 uW). So the single-flavour
controls above need the single-flavour platform. Against them, `mvt` meets
timing at 16.5 uW, a tenth of all-SLVT, but the P&R tool did the Vt
assignment that `vt_swap` had already done before placement: it fixed
-610 ps (RVT) to +26 ps with 536 swaps to SLVT and kept the leakage close
to `mvt`'s. Caveats: one seed, one utilization, ORFS's SDC rather than
`func.sdc`.

### Controlling the swap in P&R

`pnr.vt_swap: false` (SKIP_VT_SWAP, SKIP_CRIT_VT_SWAP, OPT_POST_GRT_WNS=0)
and `pnr.dont_use: [globs]` (added to the platform's DONT_USE_CELLS before
each step that runs repair_design or repair_timing, through ORFS's
`PRE_<step>_TCL` hooks) in the flow's `pnr:` section. `mvt` arm, ibex,
1500 ps, util 40, TC, all three flavours loaded, same synthesized netlist.
Leakage is the averaged-library method of the synthesis report applied to
the final netlist (tt), with ORFS's own `report_power` leakage in
parentheses; flavour counts are R / L / SL of the final netlist (synthesis:
10,108 / 36 / 63):

| run | setup WNS | TNS | hold WS | R / L / SL | leakage tt | area um^2 |
| :--- | ---: | ---: | ---: | :--- | ---: | ---: |
| default (swap on) | +22.1 ps | 0 | +43 | 12,182 / 1,023 / 768 | 19.5 uW (16.5) | 1,960 |
| A `vt_swap: false` | -158.8 ps | -2,357 ps | +44 | 12,666 / 1,233 / 147 | 9.3 uW (7.8) | 1,959 |
| B `dont_use: ["*_ASAP7_75t_SL"]` | -18.0 ps | -63 ps | +45 | 12,998 / 875 / 107 | 5.3 uW (4.3) | 1,969 |
| SKIP flags only (post-GRT swap left on) | +23.0 ps | 0 | +44 | 12,075 / 1,068 / 903 | 20.1 uW (16.6) | 1,959 |
| `RECOVER_POWER=100`, `OPT_POST_GRT_WNS=0` | +23.5 ps | 0 | +42 | 12,451 / 758 / 764 | 19.1 uW (16.2) | 1,959 |
| D averaged-leakage libraries loaded in OpenROAD | +28.6 ps | 0 | +38 | 12,170 / 980 / 745 | 18.3 uW (18.3) | 1,950 |

* A: the mapped cells keep their flavour (1 of 10,007 changes, an L to R
  resize), but the netlist ends 159 ps short of the clock: synthesis plus
  `vt_swap` had assumed a netlist without P&R's buffers and wires. Of the
  3,966 to 4,039 new cells, 1,198 are L buffers (the others R, 84 SL).
* B: 578 R to L, 40 R to SL and 4 L to SL flavour changes among the kept
  cells, new buffers R (3,707) or L (266), and the run lands 18 ps short.
  The 44 changes to SL happen although SL is in dont_use (the 63 mapped SL
  cells stay, so the SL count is 63 mapped + 44 swapped = 107); why the swap
  move picked them is not established here. The B run through an
  overridden DONT_USE_CELLS and the same run set up through `pnr.dont_use`
  (the hook) give identical numbers.
* dont_touch on the mapped cells fails in ORFS in three placements of the
  hook, none reaching a result: on all instances at floorplan, `RSZ-3006`
  in global placement (timing-driven buffering cannot insert before a
  dont_touch load); on all instances before CTS, `ODB-0370` (CTS cannot
  rewire the flops' CLK pins); on the non-sequential cells before global
  route, `RSZ-3006` again in `repair_design`. It would also stop sizing, so
  it is not a way to keep the flavour while sizing.
* D: OpenROAD reads `cell_leakage_power` from the libraries, and the swap
  does not use it (the same 499 flavour changes, 471 R to SL). The same
  holds of `RECOVER_POWER`: it ran 272 resizes and moved leakage by 0.4 uW.
  (The platform's own libraries carry only per-state leakage, no
  `cell_leakage_power`.)

Against the single-flavour controls (own-flavour platform, same netlists):

| arm | setup WNS | leakage tt (averaged) | area um^2 |
| :--- | ---: | ---: | ---: |
| rvt | -448 ps | 2.5 uW | 2,021 |
| lvt | -43 ps | 21.7 uW | 1,926 |
| slvt | +32 ps | 215.9 uW | 1,906 |

The only controlled `mvt` run that closes timing at 1500 ps is the one
with the swap on (19.5 uW), 11 times below `slvt`; the controlled runs
that keep the flavours (A, B) do not close. `mvt` B has 4 times less
leakage than `lvt` and 25 ps better WNS, but not equal timing: neither
reaches 0. All single numbers are one seed; B was run twice (identical).

## What is here

| path | what |
| :--- | :--- |
| `lib/make_libs.py` | writes `asap7_{rvt,lvt,slvt}_{tt,ss,ff}.lib` (the platform's five files per flavour and corner, merged and slimmed; per-state leakage averaged into `cell_leakage_power`) and `asap7_mvt_tt_abc.lib` (tt timing, ss leakage prices, the platform's dont_use cells left out) |
| `techpack.yaml` | the multi-Vt pack: `vt_flavours` (`_ASAP7_75t_(R\|L\|SL)$`), corners, `read_lib -l` + `abc/mvt.abc`, `mapping_target.scale` 0.5, `fanout_buffer` `BUFx2_ASAP7_75t_R` |
| `techpack_{rvt,lvt,slvt}.yaml` | the single-flavour controls |
| `abc/mvt.abc` | the abc-bridge default script with `&nf -l -D {period}` (as mvt-mini's) |
| `designs/ibex/` | `func.sdc` (1500 ps) and a flow config per arm |
| `run.sh` | the four arms and the check |
