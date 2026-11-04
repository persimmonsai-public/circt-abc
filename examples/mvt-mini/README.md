# mvt-mini: a small test bed for multi-Vt, multi-voltage synthesis

An invented library and two-domain designs that are small enough to debug
in minutes. The goal is circt-abc choosing threshold flavours (RVT, LVT,
SLVT) during mapping, each power domain being synthesized on its own
supply's libraries, and mapping that accounts for process corners and
operating modes. No foundry data. Everything here is public, and the
pack says `public: true`.

```bash
examples/mvt-mini/run.sh ~/runs/mvt-mini --sta      # whole matrix, ~2 min
examples/mvt-mini/run.sh ~/runs/mvt-mini --only core_hv -W 0.01
examples/mvt-mini/run.sh ~/runs/mvt-mini --only mvt_top --sta  # UPF, shifters, MCMM STA
examples/mvt-mini/run.sh ~/runs/mvt-mini --only mvt_soc --sta  # modes x corners
# or the flow alone (from scripts/):
python3 -m circt_flow ../examples/mvt-mini/designs/mvt_top/flow_ls.yaml ~/runs/mvt_top_ls --sta
```

`run.sh` regenerates the libraries and builds the merged priced ones. It
checks the six profiles and runs each domain through the flow three ways:
RVT with the stock ABC script, RVT with `abc/mvt.abc` as the control, and
the multi-Vt profile. It then runs `mvt_top` with both cores as power
domains on their own rails (`techpack_rails.yaml`, mapped at tt), with its
power intent (UPF, crossings, per-mode SDCs), once more with level
shifters inserted (`mvt_top_ls`), and `mvt_soc` (a clock per domain, modes
`func` and `turbo`, corners tt/ss/ff, `techpack_corners.yaml`). It writes
a per-flavour summary (`vt_report.py`): for `mvt_top`, one line per domain
against that rail's real libraries; for `mvt_soc`, one line per domain x
mode x corner. With `--sta`, that summary includes OpenSTA's worst slack
from the ORFS image, and for `mvt_top`, `mvt_top_ls` and `mvt_soc` the
table of the flow's own STA stage (per domain x corner x mode, and the
whole chip).

## What is here

| path | what |
| :--- | :--- |
| `lib/make_libs.py` | writes `mini_{rvt,lvt,slvt}_{0p75v,0p55v}_{tt,ss,ff}.lib` and `mini_ls.lib` (generated, gitignored) |
| `lib/mini_mvt_<supply>_tt_abc.lib` | run.sh: the merged priced library, tt timing and tt leakage |
| `lib/mini_mvt_<supply>_ss_ffleak_abc.lib` | run.sh: the merged priced library, ss (setup corner) timing and ff (leakage corner) leakage prices |
| `techpack.yaml` | 0.75 V tt, RVT/LVT/SLVT listed with RVT first: mapped logic is RVT (baseline) |
| `techpack_0p55.yaml` | the same at 0.55 V |
| `techpack_mvt.yaml`, `techpack_mvt_0p55.yaml` | the merged leakage-priced liberty first, so ABC chooses the flavour; `flow:` sets `read_lib -l` and `abc/mvt.abc` |
| `techpack_rails.yaml` | schema 2, mapped at tt: rails `VDD_HV` (0.75 V, default) and `VDD_LV` (0.55 V), each with its tt merged priced liberty first and `read_lib -l` + `abc/mvt.abc`, and its tt/ss/ff libraries for timing (`corners:`, setup and leakage tt); `mini_ls.lib` as level shifters |
| `techpack_corners.yaml` | schema 2 with `corners:` tt/ss/ff (setup ss, leakage ff, hold ff): the same rails, each mapping on its `ss_ffleak` priced liberty and listing its libraries per corner |
| `abc/mvt.abc` | the abc-bridge default script with `&nf -l -D {period}` |
| `rtl/` | `core_hv` (8x8 MAC, 0.75 V), `core_lv` (LFSR + sum + compare, 0.55 V), `mvt_top` joining them on one clock, `mvt_soc` on a clock each |
| `designs/<core>/` | manifest (a local `source: {path: ...}`) and `flow.yaml` / `flow_mvt.yaml` |
| `designs/mvt_top/` | `flow.yaml` with `power:`: `u_lv` on `VDD_LV`, `u_hv` on `VDD_HV`, the glue on the default rail; modes `func.sdc` (1.2 ns) and `eco.sdc` (1.6 ns); `sta:` (off unless `--sta`); `flow_ls.yaml`, the same with `insert_level_shifters: true` |
| `designs/mvt_soc/` | `flow.yaml` with `power:`, `modes:` (`func.sdc`, `turbo.sdc`) and `mapping_target:` on `techpack_corners.yaml` |
| `vt_report.py` | cells, real area and leakage per flavour, optional OpenSTA slack |
| `run.sh` | the matrix |

The library has the shape of a real multi-Vt library. Cell names carry the
flavour (`MNAND2_X1_S`) but not the supply or corner, so the same cell at
0.55 V and 0.75 V, tt, ss and ff, has one name. All flavours have the same
area. A lower threshold is
faster and leaks more, and it gains more speed at the low supply. Every
cell has `area` followed by `cell_leakage_power`, which is the input
`scripts/mvt/make_abc_lib.py` prices.

Corners (invented): tt is typical at 25 C; ss is slow and cold (delays
x1.20 at 0.75 V and x1.30 at 0.55 V, leakage x0.4); ff is fast and hot
(delays x0.85 / x0.80, leakage x3). Every delay, setup and hold value
scales. The tt libraries hold exactly the cells that were there before
corners existed. Only the file and library names gained `_tt`, and every
netlist of the existing arms is byte-identical.

## Findings (2026-11-03; rerun 2026-11-04 with classical `resub` in
`abc/mvt.abc`)

`core_hv` at a 1,000 ps clock on the 0.75 V libraries. WNS is from OpenSTA
with zero I/O delays; leakage is in nW, from the real libraries.

| arm | flavours | leakage | WNS ns |
| :--- | :--- | ---: | ---: |
| `flow`: RVT, stock ABC script | 423 R | 499 | -0.330 |
| `flow_l`: RVT, `abc/mvt.abc` (control) | 442 R | 515 | -0.238 |
| `flow_mvt`: merged priced lib, `-W 0.002` | 351 R / 28 L / 54 S | 2,073 | -0.012 |

Timing-driven mapping is worth about 90 ps. Flavour choice is worth about
another 225 ps, for 4.0x the leakage. `core_lv` meets its 1.5 ns clock in
all three arms, and its multi-Vt arm stays almost all RVT (2 L).

What it took for ABC to choose flavours at all:

1. **ABC's default genlib cannot tell flavours apart.** `read_lib` gives
   every cell of a function class the class's average delay
   (`Abc_SclComputeDelayClassPin`). RVT, LVT, SLVT, X1 and X2 therefore
   have the same delay, `&nf` sees only their prices, and RVT always wins.
   `read_lib -l` (load-aware genlib) keeps each cell's own delay.
2. **`&nf` ignores `-D` unless it runs with `-l`** (giaNf.c,
   `Nf_ManPerformMappingInt`). Without `-l`, different periods give
   byte-identical netlists. `abc/mvt.abc` sets it through the pack, so no
   code change is needed.
3. **The load-aware genlib needs NLDM tables with at least three load
   points.** It fits each pin's load slope between the first and the
   second-to-last load index (`Abc_SclComputeLoadDelayPin`). With two
   points that is one point, every slope is zero, and `&nf` was far too
   optimistic against STA. That is why the library now has 5x5 tables.
   With them, `&nf`'s own estimate (its "Relaxing user-specified delay
   target ... to X" line, not `print_stats`' load-free `delay =`) is a
   steady ~1.2x pessimistic against `stime` and STA, which is
   conservative. Its target is the clock period, which leaves out the
   flops' clock-to-Q and setup (~95 ps here), so the tightest case misses
   by a few ps. A flow config's `mapping_target:` now subtracts them (see
   Modes and corners).
4. **The leakage price must be non-zero, but its exact value matters
   little.** At `-W 0`, equal prices and the delay tie-break give 400 SLVT
   of 437 cells, +97 ps of slack and 10,313 nW (20x). Any W from 0.0005 to
   0.05 lands near 2,000-2,600 nW and within 30 ps of timing.

### After mapping: the threshold-voltage swap (2026-11-04)

`run.sh --vt-swap` adds circt_flow's `vt_swap` stage (circt_flow/vtswap.py,
doc/techpacks.md): OpenSTA moves cells on failing paths to a faster
flavour, flops and clock gates included, then moves every cell back toward
RVT while the slack holds. The flavours of a cell share area and pins, so
only cell types change. On the `flow_mvt` arm:

| `flow_mvt` | flavours | leakage nW | WNS ns |
| :--- | :--- | ---: | ---: |
| `core_hv` @ 1 ns, mapped | 351 R / 28 L / 54 S | 2,073 | -0.012 |
| `core_hv` @ 1 ns, + vt_swap | 371 R / 35 L / 27 S | 1,361 | +0.000 |
| `core_lv` @ 1.5 ns, mapped | 161 R / 2 L | 109.3 | +0.100 |
| `core_lv` @ 1.5 ns, + vt_swap | 163 R | 105.4 | +0.069 |

The swap meets `core_hv`'s clock with 34% less leakage than ABC's own
flavour choice: ABC's area recovery, priced by leakage, still spends fast
cells where real timing does not need them. In the power runs the swap
works on the chip view (every path, crossings and level shifters
included): `mvt_top` keeps both modes, and on `mvt_soc` it cuts the
known unsynchronized crossing's turbo violation at ss from -0.122 to
-0.038 ns, the rest being cells already at SLVT.

## Bearing on real libraries

* **A library without timing tables maps under unit delays.** A
  function-and-area liberty with no `cell_rise` tables gives ABC nothing
  to tell flavours apart with, and `-D` cannot matter. A multi-Vt run
  needs NLDM-carrying libraries: `scripts/mvt/slim_liberty.py` builds
  them from full foundry liberties.
* Real libraries' tables usually have five or more load points, so
  finding 3 should bite less there.

## Mixed rails: `mvt_top`

`designs/mvt_top/flow.yaml` puts `u_lv` (core_lv) on `VDD_LV` and `u_hv`
(core_hv) on `VDD_HV`; the glue (one flop and a gate) is on the default
rail, `VDD_HV`. The flow maps each domain with its own rail's liberties
only and writes `<out>/1_synth/mvt_top.v`, a hierarchical netlist (the
top, `core_lv`, `core_hv`), and `<out>/power_domains.json`. Its two modes
are `func` (1.2 ns) and `eco` (1.6 ns), with 0.1 ns I/O delays.
`mapping_target.flop_overhead: false` keeps ABC's target at the tightest
period, 1,200 ps, so the netlists are byte-identical to those of the
single-period config. At `-W 0.002` (re-measured 2026-11-04; leakage from
each rail's real tt libraries):

| domain | rail | flavours | leakage |
| :--- | :--- | :--- | ---: |
| lv (`core_lv`) | VDD_LV, 0.55 V | 154 R / 5 L / 3 S | 144 |
| hv (`core_hv`) | VDD_HV, 0.75 V | 398 R / 19 L / 21 S | 1,181 |
| remainder | VDD_HV, 0.75 V | 3 R | 6 |

At 1,300 ps the same flow gives `core_hv` 412 R / 9 L / 5 S and 681 nW
(+0.072 ns), and `core_lv` 150 nW (+0.095 ns). The numbers depend on the
priced libraries, so they hold for run.sh's own `-W`; libraries left over
from a run with another `-W` give other ones.

## Power intent, level shifters and MCMM timing: `mvt_top`

The same run also writes:

* `<out>/power_intent.json`, the crossings. 59 nets cross domains, and 28
  of them cross between voltages: 19 go down into `u_lv` (`clk_i`,
  `rst_ni`, `en_i`, `thresh_i[15:0]`), and 9 come up out of it
  (`sample[7:0]` into `u_hv`, `over_lv` into the glue). The other 31
  (`u_hv` and the glue, both 0.75 V) need no shifter.
* `<out>/mvt_top.upf`, which declares:
  * `PD_TOP -include_scope`, `PD_lv -elements {u_lv}` and
    `PD_hv -elements {u_hv}`;
  * supplies `VDD_HV`, `VDD_LV` and `VSS`, and the supply sets
    `SS_VDD_HV` and `SS_VDD_LV`, each domain's primary;
  * three strategies, each placing its shifters in the receiving domain:
    `PD_lv` inputs `high_to_low -location self` (powered by `VDD_LV`),
    `PD_lv` outputs `low_to_high -location parent` (into the glue), and
    `PD_hv` inputs `low_to_high -location self` (`sample` into `u_hv`),
    each with `-input_supply_set` / `-output_supply_set`.

  `mini_ls.lib`'s shifters have supply pins: `MLSLH_X1` has `VDDL` on
  the input side and `VDD` on the output side, and `MLSHL_X1` has only
  `VDDL`. With shifters inserted, the UPF wires them with
  `connect_supply_net -pins`. Each `MLSLH_X1` gets `VDDL` on `VDD_LV`
  and `VDD` on `VDD_HV`; each `MLSHL_X1` gets `VDDL` on `VDD_LV`; every
  `VSS` goes to `VSS`. The check confirms each pin's rail voltage is
  within the cell's input or output range.
* `<out>/sdc/{func,eco}.sdc`.

`flow_ls.yaml` (`run.sh` arm `mvt_top_ls`) inserts the shifters. Its
OpenSTA netlist gains 19 `MLSHL_X1` and 9 `MLSLH_X1` in the top (the clock
into `u_lv` goes through one). The flow checks that this netlist is the
unshifted one with each shifter read as a buffer. Yosys checks it too: it
reads each cell's liberty function (the shifters are `Y = A`), flattens
both netlists, and `equiv_make` / `equiv_simple` / `equiv_induct` prove
all 2,973 `$equiv` cells. Swapping two shifter outputs leaves 18
unproven, and the flow's own check names the two pins. The same proof, on
a small netlist, is a test (`tests/test_tools.py`, skipped without
yosys). With the option off (`flow.yaml`), nothing is inserted.

`--sta` times each domain x mode x corner (tt/ss/ff): each domain module
linked alone with its rail's real libraries at that corner. Each WNS cell
below is ns, with TNS in brackets where negative. The last column is the
chip view: the whole netlist, cells renamed per rail, so paths between
domains are timed too. It gives the worst endpoint in each domain at
tt / ss / ff. Results re-measured 2026-11-04, at run.sh's `-W 0.002`:

| domain | rail | mode | WNS tt | WNS ss | WNS ff | chip tt / ss / ff |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: |
| lv | VDD_LV | func | +0.081 | -0.255 (-0.930) | +0.304 | +0.081 / -0.255 / +0.304 |
| lv | VDD_LV | eco | +0.481 | +0.145 | +0.688 | +0.481 / +0.145 / +0.704 |
| hv | VDD_HV | func | +0.035 | -0.199 (-0.858) | +0.210 | +0.035 / -0.199 / +0.210 |
| hv | VDD_HV | eco | +0.435 | +0.202 | +0.610 | +0.435 / +0.202 / +0.610 |
| top | VDD_HV | func | +1.050 | +1.040 | +1.058 | +1.029 / +0.984 / +1.042 |
| top | VDD_HV | eco | +1.450 | +1.440 | +1.458 | +1.429 / +1.384 / +1.442 |

* **Mapped at tt, `mvt_top` misses `func` at ss** by 255 ps (lv) and
  199 ps (hv). It meets `eco` everywhere. `mvt_soc` maps at ss and meets
  there.
* **The chip view agrees with the per-domain views for lv and hv.** It
  lowers the top's slack (+1.029 against +1.050 at tt), because there the
  `over_lv` path starts at `u_lv`'s real flop instead of a zero-delay
  boundary.
* **With the shifters (`mvt_top_ls`), every domain row is unchanged.**
  The chip top falls to +0.967 / +0.921 / +0.998 (func) and +1.367 /
  +1.321 / +1.398 (eco), the `MLSLH_X1` on `over_lv`. The shifter on the
  clock into `u_lv` delays launch and capture alike, so lv's internal
  slack does not move.

The same stage on `mvt_soc` (`run.sh --only mvt_soc --sta`) gives
`hv turbo@ss` +0.088 ns in the per-domain view, as in the section below.
Its chip view fails `turbo`:

| mode | chip WNS tt | ss | ff | worst endpoint |
| :--- | ---: | ---: | ---: | :--- |
| func | +0.128 | +0.078 | +0.160 | `u_hv/a_q_bit1/D` |
| turbo | -0.072 | -0.122 | -0.040 | `u_hv/a_q_bit1/D` |

The failing path is `sample`, from a `clk_lv` flop into a `clk_hv` flop,
with no synchronizer. In turbo, OpenSTA's closest edges are 100 ps apart
(`clk_lv` at 22.0 ns, `clk_hv` at 22.1 ns). The example's SDCs declare no
clock groups. The per-domain views could not see this path: there the
crossing is a zero-delay boundary.

## Modes and corners: `mvt_soc`

`mvt_soc` is `mvt_top` with a clock per domain: `clk_lv_i` clocks `u_lv`
(core_lv, VDD_LV) and `clk_hv_i` clocks `u_hv` (core_hv, VDD_HV) and the
glue. `designs/mvt_soc/flow.yaml` names two modes:

| mode | `clk_hv` | `clk_lv` |
| :--- | ---: | ---: |
| `func.sdc` | 1.5 ns | 1.8 ns |
| `turbo.sdc` | 1.3 ns | 2.2 ns |

`techpack_corners.yaml` makes ss the setup corner, ff the leakage
corner and ff, the fastest, the hold corner (vt_swap's hold guard and the
STA summary's hold entry; the SDCs give `b_i`/`en_i` a -min input delay of
a flop's clock-to-Q, 0.04 ns, so inputs do not fail hold by construction:
hold at ff, chip view, is +0.030 ns). Each rail maps on `mini_mvt_<supply>_ss_ffleak_abc.lib`, which has
ss timing and prices of `S * (area + W * ff leakage)`. The flow traces
which clock reaches which domain and logs each domain's target, the
tightest period over the modes minus the rail's worst ss flop clock-to-Q +
setup:

```
[power] mapping target lv: ... tightest func 1800 ps; flop overhead 232.68 ps (MDFF_X1_R in mini_rvt_0p55v_ss.lib: clk->Q 183.28 + setup 49.4); target floor(1 * (1800 - 232.68)) = 1567 ps
[power] mapping target hv: ... tightest turbo 1300 ps; flop overhead 113.3 ps (MDFF_X1_R in mini_rvt_0p75v_ss.lib: clk->Q 89.3 + setup 24); target floor(1 * (1300 - 113.3)) = 1186 ps
```

So hv follows turbo and lv follows func. Results (re-measured 2026-11-04) at run.sh's
`-W 0.002`: OpenSTA on each domain module of the stitched netlist, with
that corner's real libraries and zero I/O delays; leakage in nW at that
corner.

| domain | flavours | leak tt / ss / ff | WNS func ss | WNS turbo ss | WNS turbo tt / ff |
| :--- | :--- | ---: | ---: | ---: | ---: |
| lv | 161 R / 3 L / 3 S | 144 / 58 / 432 | +0.325 | +0.725 | +1.065 / +1.292 |
| hv | 352 R / 35 L / 63 S | 2,509 / 1,003 / 7,526 | +0.288 | **+0.088** | +0.290 / +0.441 |

The hv domain meets its tightest mode (turbo) at the setup corner with
88 ps to spare. That margin is `&nf`'s pessimism: it relaxed the 1,186 ps
target to estimates of 1,224-1,362 ps across hv's ABC runs, while STA puts
the logic at about 1,099 ps (1,300 - 88 - 113), 1.11-1.24x below `&nf`.

**The setup corner's timing matters.** Here the hv core (same extracted
MLIR, same 1,186 ps target) is mapped with three priced libraries and timed
at ss for turbo:

| mapping library (timing / leakage priced) | flavours | leak ss / ff | WNS turbo ss |
| :--- | :--- | ---: | ---: |
| tt / tt (`mini_mvt_0p75v_tt_abc.lib`) | 364 R / 37 L / 38 S | 734 / 5,508 | **-0.058** |
| ss / ss | 366 R / 27 L / 36 S | 643 / 4,822 | +0.028 |
| ss / ff (`techpack_corners.yaml`) | 352 R / 35 L / 63 S | 1,003 / 7,526 | +0.088 |

Mapping against tt timing misses ss by 58 ps. Both ss-timed libraries meet
it. The leakage corner changes the netlist, but in this library ff
leakage is tt's x3 for every flavour, so it acts as a larger `W`, not as a
different flavour ranking. The two ss-timed results differ by about as
much as the `-W` sweep's points do (`core_hv`: 2,073 to 2,549 nW for `W`
from 0.0005 to 0.05), so read the difference as the mapper's noise, not
a corner effect.

**`mapping_target.scale`** trades the margin for leakage. The same flow
for hv at turbo, ss:

| scale | target | flavours | leak ff | WNS turbo ss |
| ---: | ---: | :--- | ---: | ---: |
| 1.0 (default) | 1,186 | 352 R / 35 L / 63 S | 7,526 | +0.088 |
| 1.1 | 1,305 | 335 R / 48 L / 67 S | 7,873 | +0.040 |
| 1.2 | 1,424 | 375 R / 34 L / 27 S | 4,507 | -0.021 |

At 1.1 it still meets, but with no less leakage; at 1.2 it saves 40% and
misses by 21 ps. The returns are as noisy as the `-W` sweep's, so the
default stays 1.0, which is conservative.

## Not done yet

* **Hold is checked, not signed off, and I/O delays do not reach
  mapping.** `corners.hold` names the hold corner, and the STA summary
  reports hold there by path class (reg-to-reg, from inputs, to outputs),
  but with ideal clocks: real hold signoff is P&R's, after CTS. The SDCs'
  I/O delays reach the STA (per domain, mapped onto its boundary) but not
  the mapping target.
* **The per-domain boundary budget is an assumption.** A domain port that
  does not reach a top-level port gets `sta.boundary_budget` (0.5) of the
  period as its I/O delay in that domain's own view, and the top's own
  view uses 0 at the domain pins. The chip view times those paths for
  real.
* **The example SDCs declare no clock groups.** `mvt_soc`'s lv-to-hv
  crossings (no synchronizer) fail `turbo` in the chip view; see above.
* **Power intent is all-on.** The UPF has one all-on power state, with
  no isolation, retention or power switches. Level shifters are inserted
  only in the top module of the OpenSTA netlist. `mvt_top.v` stays
  unshifted, and domains nested in a remainder module are refused. No
  UPF reader, P&R tool or low-power static checker has read the UPF in
  this tree.
* **One level-shifter library for every corner.** `mini_ls.lib` is a tt
  library, and the STA reads it at ss and ff as well.
* **Registers and the ICG map RVT.** The register mapper reads only the
  primary liberty and picks the cheapest flop, which is RVT; the clock-gate
  cell is picked by area across all liberties, the first library winning a
  tie. With `--vt-swap` the swap moves flops and clock gates to faster
  flavours where timing needs it; without it they stay RVT.
* **vt_swap fixes setup, and only guards hold.** It does not fix hold
  violations (P&R does). It keeps the swap from making hold worse at the
  hold corner: moves that would are undone and frozen. Setup is judged on
  the setup corner's chip view only.
* **Area from the merged arms' own flow reports is a price, not an area.**
  Use `vt_report.py`, which reads the real libraries.
