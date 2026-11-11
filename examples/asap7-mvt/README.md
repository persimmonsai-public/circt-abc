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
before placement. `mvt.abc` bounds `&deepsyn` by steps without improvement
and has no wall-clock limit, so a run is reproducible whatever the load, and
each cell is that one run (see Reproducibility):

| arm | WNS at tt | hold at ff | leakage at ss | leakage at tt | area um^2 | flavours |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| rvt | -670 ps | +2.8 ps | 21.8 uW | 1.58 uW | 1,577 | R 10,118 |
| lvt | -305 ps | +0.7 ps | 147 uW | 15.1 uW | 1,573 | L 10,085 |
| slvt | -82 ps | +5.2 ps | 942 uW | 153 uW | 1,573 | SL 10,090 |
| mvt | +0.6 ps | +2.8 ps | 27.6 uW | 2.55 uW | 1,590 | R 10,084, L 63, SL 69 |

At this clock the multi-Vt arm meets setup, and none of the single-flavour
arms does (SLVT misses by 82 ps). The multi-Vt netlist leaks about 34x less
than all-SLVT at ss, 1.27x the all-RVT netlist that misses timing by 670 ps,
for about 1% more area than all-SLVT: it is RVT except 132 cells on the
critical paths.

How the `mvt` arm gets there: ABC maps on the priced library (a cell's price
is its area plus W times its ss leakage) with a target of half the period
less the flop overhead. It uses 25 LVT and 27 SLVT cells and ends at -526 ps.
`vt_swap` then makes 892 swaps to faster flavours on failing paths (+16 ps
after them) and 770 back toward RVT where slack allows, finishing at +0.6
ps, with hold at ff unchanged (+2.8 ps before and after).

### Reproducibility

`mvt.abc` runs `&deepsyn` with a bound on steps without improvement (`-J 3`)
and no `-T`, so the same input gives the same netlist on any machine and at
any load, and the table above is one run, not a median. (An earlier version of
the script limited `&deepsyn` by 10 seconds of wall clock, which made what ABC
mapped depend on how many of its seeded iterations the machine finished; its
results varied from run to run and were quoted as medians and ranges. `-J 3`
was chosen as a small bound: on ibex the whole synthesis took 186 s with it
against 78 s with `-T 10`, which cut the chains short; `-J 1` took 139 s.)

The clock is where the flow is today on ASAP7, not where ibex can go:
ORFS's own asap7/ibex design is constrained at 1000 ps. ABC's load-aware delay
model is optimistic on these libraries (on the RVT arm `&nf` relaxes its
704 ps target to between 816 and 1,187 ps in its own estimate, where OpenSTA
puts the paths far later), and the worst paths are long carry-style chains.
Closing that gap is separate work. At 1300 ps the `mvt` arm misses by 227 ps,
at 1200 by 347 (W 0.0001).

## Calibration

`mapping_target.scale` (target = scale x (period - flop overhead)), on ibex
with the multi-Vt pack (W 0.0001) and `vt_swap`, WNS at tt after the swap:

| period | scale 0.5 | scale 0.75 | scale 1.0 |
| :--- | ---: | ---: | ---: |
| 1500 ps | -47.7 ps | -115.0 ps | -200.0 ps |
| 1800 ps | +0.7 ps | +0.1 ps | -55.9 ps |
| 2000 ps | +0.9 ps | +0.4 ps | +0.0 ps |

0.5 is the packs' default, so the controls get the same target. On the RVT
control the scale matters little: at 1000 ps, scales 0.3, 0.5, 0.75 and 1.0
give -1.17, -1.17, -1.17 and -1.31 ns, since ABC cannot reach any of those
targets with RVT alone and relaxes them to its own best delay.

The flop overhead is the worst clock-to-Q + setup over the flops the
mapper may use (90.7 ps here). The platform's scan flops (`SDF*`,
`dont_use`) are slower and are not counted.

W, the leakage price in um^2 per pW (`run.sh -W`), `mvt` arm at 1500 ps:

| W | ABC's flavours | after vt_swap | WNS at tt | leakage at ss | area um^2 |
| :--- | :--- | :--- | ---: | ---: | ---: |
| 0 | R 2,227, SL 7,871 | R 9,868, L 137, SL 93 | -71.4 ps | 30.0 uW | 1,572 |
| 0.00001 (default) | R 10,164, L 25, SL 27 | R 10,084, L 63, SL 69 | +0.6 ps | 27.6 uW | 1,590 |
| 0.0001 | R 10,439, L 27, SL 17 | R 10,300, L 85, SL 98 | -47.7 ps | 29.3 uW | 1,632 |
| 0.001 | R 10,778, L 23, SL 14 | R 10,079, L 359, SL 377 | -142 ps | 62.7 uW | 1,681 |

Only the default W meets timing in this set. `vt_swap` fixes what the mapping
leaves and recovers what it overspends, but its fix steps stop when an
iteration no longer improves setup, so a mapping that starts far from the
answer can end short of it: with W = 0, ABC maps 78% of the cells SLVT, the
swap's recovery moves nearly all of them back (15,579 down against 160 up)
and the run ends 71 ps short. A price makes the mapping start close to the
answer, and the swap then has less to do (892 swaps up and 770 down at the
default W). Higher prices bend the mapping toward larger structures (more
area) for no gain in leakage.

## P&R

`--pnr` hands each arm's netlist to stock ORFS (asap7 platform, `CORNER=TC` so
the platform times at tt like the packs; ORFS's own SDC template, real CTS).
Every flow config has a `pnr:` section; `ASAP7_USE_VT` tells the platform which
flavours' LEFs and libraries to load (a single-flavour control sets its own
flavour and its tie cells with `pnr.tie_hi` / `pnr.tie_lo`):

```bash
python3 -m circt_flow examples/asap7-mvt/designs/ibex/flow_mvt.yaml ~/runs/ibex_mvt_pnr --sta --vt-swap --pnr
```

Results are ORFS's final report (post-route, SPEF parasitics); no P&R result is
quoted here. Two things to know when reading one:

* OpenROAD's `repair_timing` has its own Vt swap move, run in floorplan and
  again after global routing, and its buffers are chosen from every flavour
  the platform loads, so the flavours of a synthesized netlist need not survive
  placement.
* The flow config's `pnr:` section controls it: `pnr.vt_swap: false` (sets
  SKIP_VT_SWAP and SKIP_CRIT_VT_SWAP and OPT_POST_GRT_WNS=0) and
  `pnr.dont_use: [globs]` (added to the platform's DONT_USE_CELLS before each
  step that runs repair_design or repair_timing, through ORFS's
  `PRE_<step>_TCL` hooks).

## What is here

| path | what |
| :--- | :--- |
| `lib/make_libs.py` | writes `asap7_{rvt,lvt,slvt}_{tt,ss,ff}.lib` (the platform's five files per flavour and corner, merged and slimmed; per-state leakage averaged into `cell_leakage_power`) and `asap7_mvt_tt_abc.lib` (tt timing, ss leakage prices, the platform's dont_use cells left out) |
| `techpack.yaml` | the multi-Vt pack: `vt_flavours` (`_ASAP7_75t_(R\|L\|SL)$`), corners, `read_lib -l` + `abc/mvt.abc`, `mapping_target.scale` 0.5, `fanout_buffer` `BUFx2_ASAP7_75t_R` |
| `techpack_{rvt,lvt,slvt}.yaml` | the single-flavour controls |
| `abc/mvt.abc` | the abc-bridge default script with `&nf -l -D {period}` (as mvt-mini's) |
| `designs/ibex/` | `func.sdc` (1500 ps) and a flow config per arm |
| `run.sh` | the four arms and the check |
