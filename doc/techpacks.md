# Tech packs: running the flow on your own technology

A **tech pack** is a directory that holds everything technology-specific
the flow needs: the library profile, optional flow defaults, optional
place-and-route setup, and optionally your own designs. It lives outside
this repository, ideally in a private repository of its own, so a PDK under
NDA, a foundry's liberties or a tuned recipe never has to be in the
circt-abc tree. The flow finds a pack by name and runs it with no code
change and no fork, and the leak audit stops its names from being
committed here.

`examples/techpack-template/` is a complete, runnable pack on an invented
library: copy it to start your own.

## Quick start

```bash
cp -r examples/techpack-template ~/techpacks/mytech
cd ~/techpacks/mytech && git init      # keep it in a private repository
# edit techpack.yaml: name, liberties, icg, tie, dont_use, deny_terms
export CIRCT_ABC_TECH_PATH=~/techpacks
python3 -m circt_flow.techpack check mytech
python3 -m circt_flow ~/techpacks/mytech/designs/ibex/flow.yaml ~/runs/ibex --pnr
```

## Layout

```
<pack>/
  techpack.yaml        # required
  lib/ ...             # liberties, unless techpack.yaml points at ${VAR}
  abc/recipe.abc       # optional: an ABC script for this technology
  orfs-platform/       # optional: an ORFS platform (config.mk, LEFs, ...)
  pnr/ ...             # optional: your own P&R command
  designs/<name>/      # optional: flow configs, private manifests
```

A pack is data and configuration only. Anything that needs code becomes a
generic, option-driven pass or stage in circt-abc itself, motivated
generically and tested on a hermetic liberty under `mapper/test/`. The one
exception is the P&R command below, which the flow only runs and never
imports.

## techpack.yaml

```yaml
schema: 1                        # required; the format version
name: mytech                     # what flow configs call it
liberties:                       # relative to the pack, or ${VAR}/...
  - ${MYTECH_PDK:-/opt/mytech}/lib/stdcell_tt.lib
time_unit: ns
icg: {cell: ICG_X1, clk: CK, en: E, test_en: SE, out: GCK}
dont_use: ["*_X0P5", "SDFF*"]    # glob patterns ABC must not use
tie: {hi: "TIEHI:Z", lo: "TIELO:Z"}

deny_terms: ["mytech", "MYFOUNDRY"]  # the audit's extra strings
deny_exceptions: []                  # names the audit lets through
# public: true                       # a pack whose content is all public

flow:                            # defaults; see Precedence
  abc_script_file: abc/recipe.abc
  sv2v_docker_image: circt-abc
  env:
    CIRCT_FLOW_ADDER_ARCH: RIPPLE-CARRY

pnr:                             # optional; one of
  orfs_platform_dir: orfs-platform
  # command: ["./pnr/run.py"]
```

A library with no integrated clock-gating cell gives a composite one
instead: `icg: {netlist: icg/my_icg.v, cell: my_icg, clk: ..., en: ...,
test_en: ..., out: ...}`, where the netlist is a structural Verilog module
named `cell`, built from the library's own cells. The front end compiles it
with the design and keeps each cell it instantiates as a library cell; the
GT3 pack's falling-edge-flop gate is the example.

Instead of `liberties:`, a pack may take them from an ORFS image's platform
with `orfs: {platform, liberties}` as `profiles/asap7.yaml` does; they are
extracted into the cache and merged into one file.

`${VAR}` and `${VAR:-default}` are expanded from the environment in every
path; an unset `${VAR}` with no default is an error. Paths are relative to
the pack directory. The first liberty is the primary one, which the
single-liberty passes read. A pack with several supplies uses schema 2
(Rails, below).

`python3 -m circt_flow.techpack check <name|path>` loads a pack and checks
that every liberty, `flow:` path and `pnr:` path exists and that the ICG and
tie cells are cells of the liberties. Tie cells are only used by P&R, so
they may instead come from the pack's own ORFS platform (`lib/*.lib` under
`orfs_platform_dir`), for a library that has none.

## Multi-Vt, multi-voltage: how the pieces fit

A pack that lets synthesis choose threshold flavours (RVT/LVT/SLVT) and
put power domains on their own supplies uses these, in this order:

1. **Timed liberties.** ABC needs NLDM tables to tell flavours apart
   (`read_lib -l`); a `_yosys`-style library without timing maps them all
   alike. `scripts/mvt/slim_liberty.py` slims a foundry liberty to what
   the flow needs.
2. **A priced mapping library per rail** (`scripts/mvt/make_abc_lib.py`):
   every flavour merged into one library whose areas are prices,
   `S * (area + W * leakage)`, so area recovery prefers low-leakage cells
   where there is slack. It leaves out `dont_use` cells (the mappers do not
   read the attribute), except those `--keep` names (clock gates). It is
   the rail's FIRST liberty; the real per-flavour libraries follow it.
3. **Load-aware, target-driven ABC**: `flow.abc_read_lib_args: "-l"` and
   an `abc_script_file` whose `&nf` runs with `-l -D {period}`
   (`examples/mvt-mini/abc/mvt.abc`).
4. **A calibrated target** (`flow.mapping_target`, "Modes and the mapping
   target"): ABC's delay model can be far from STA's (optimistic by a
   factor of two or more on some libraries), and with the target at the
   period such a library maps everything at the slowest flavour.
5. **Fanout buffering** (`flow.fanout_buffer`, "Fanout buffering"):
   nets that leave an ABC partition reach STA unbuffered, and on a large
   design their load, not the flavours, sets the worst path.
6. **`vt_flavours:` and `vt_swap:`** ("Threshold-voltage swap"): after
   synthesis, OpenSTA fixes failing paths with faster flavours (flops and
   clock gates included, which the mappers always place at the slowest
   flavour) and recovers leakage everywhere else.
7. **Rails, power domains and corners** (below), with the UPF, level
   shifters, per-mode SDCs and MCMM STA they bring.

`examples/mvt-mini` is the public walk-through of all of it on an invented
library; a pack for a foundry library applies the same pieces to it.

## Rails (schema 2)

A design whose blocks sit on different supplies needs one set of
liberties per supply. The same cell names usually exist at every supply,
so a netlist name never says which rail a cell is on; the rail is a
property of where the cell sits. `schema: 2` describes the supplies as
rails:

```yaml
schema: 2
name: mytech
icg: {cell: ICG_X1, clk: CK, en: E, test_en: SE, out: GCK}
tie: {hi: "TIEHI:Z", lo: "TIELO:Z"}
flow: {abc_script_file: abc/recipe.abc}  # pack defaults, every rail
default_rail: VDD                 # where undeclared logic goes
rails:
  VDD:
    voltage: 0.75
    liberties: [lib/stdcell_0p75v.lib]  # first = the rail's primary one
  VDD_LV:
    voltage: 0.55
    liberties: [lib/stdcell_0p55v.lib]
    flow: {abc_read_lib_args: "-l"}     # per-rail flow overrides
    # icg:, tie:, dont_use: may also be overridden per rail
level_shifters:
  liberties: [lib/level_shifters.lib]
  corners:                          # optional, with `corners:` below
    ss: [lib/level_shifters_ss.lib]
```

There is no top-level `liberties:`; each rail lists its own, primary
first, and its `flow:` is merged over the pack's key by key. Seen as a
single-rail profile (a flow config without `power:`), a schema-2 pack is
its default rail; `pdk.rail: <name>` in a flow config maps the whole design
on another rail. A schema-1 pack is one implicit rail, named `default`.

`techpack check` checks each rail on its own (its liberties and flow paths
exist, its ICG and tie cells are cells of its own liberties) and the
level-shifter liberties, and prints one line per rail. The audit refuses
the names in every rail's liberties and the level shifters'.

`level_shifters:` lists the liberties of the cells that
`power.insert_level_shifters` (see Power domains) puts on signals crossing
between rails. A cell qualifies when it has `is_level_shifter : true`, a
`level_shifter_type` of `LH` (low to high), `HL` or `LH_HL`, one input and
one output pin whose `function` is that input (no enable), and, when it
gives them, an `input_voltage_range` / `output_voltage_range` that hold
the two rails' voltages. The smallest-area qualifying cell is used. A pack with
`corners:` may add `level_shifters.corners: {<corner>: [libs]}`: the
shifters' liberties at that corner (names checked against `corners.names`;
a corner not listed reads `liberties`, and `liberties` may be omitted when
every corner is listed). The STA stage's top-domain and chip views read
their own corner's shifters, and vt_swap reads the setup corner's with the
setup libraries and the hold corner's with the hold libraries.
`techpack check` requires every corner's shifters to exist and have the
setup corner's cell names. Shifter cells are not renamed per rail.

### Corners

A schema-2 pack may also give each rail's liberties per process /
temperature corner. A profile-level `corners:` block names the corners and
their roles, and every rail lists every corner:

```yaml
corners:
  names: [tt, ss, ff]
  setup: ss          # the setup (mapping-delay) corner
  leakage: ff        # the corner whose leakage prices the mapping library
  hold: ff           # optional: the corner hold is checked at
rails:
  VDD:
    voltage: 0.75
    liberties: [lib/mapping_0p75v.lib, lib/stdcell_0p75v_ss.lib]
    corners:
      tt: [lib/stdcell_0p75v_tt.lib]
      ss: [lib/stdcell_0p75v_ss.lib]
      ff: [lib/stdcell_0p75v_ff.lib]
```

A rail's `liberties:` stay what the mapper reads, primary first. Left out,
they are the setup corner's list. For a multi-Vt rail, the primary one is
the priced liberty `scripts/mvt/make_abc_lib.py` merges. Given
`--leakage-lib`, it takes the timing from the setup corner's libraries and
the leakage it prices from the leakage corner's:

```bash
scripts/mvt/make_abc_lib.py lib/mapping_0p75v.lib lib/{rvt,lvt}_0p75v_ss.lib \
  -W 0.002 -L lib/rvt_0p75v_ff.lib -L lib/lvt_0p75v_ff.lib
```

The mapping target's flop overhead (see "Modes and the mapping target"
below) comes from the setup corner's flops. `techpack check` also checks
every corner's liberties: they must exist and have the same cell names as
the setup corner's, and each must hold every cell of the rail's mapping
liberties, so a netlist mapped once can be timed at any corner. The STA stage
(`--sta`, see Power domains) times every corner with that corner's
liberties. The audit covers every corner's liberties. A pack without `corners:` has one
implicit corner, the rail's liberties.

`hold:` names the corner hold is checked at: vt_swap's hold guard times
hold paths there, and the STA summary's `hold` entry reports it. Left
out, it is the setup corner. Pick the fastest corner, which is not always
the cold one: with temperature inversion (FinFET and GAA processes at low
supply) the fast-process corner is fastest hot, so measure it: time a
netlist's register-to-register hold at each fast corner and take the
worst. A rail `corners:` without the
pack-level block, a rail missing a corner, or `corners:` in a schema-1 pack
is an error.

### Mapping-library provenance

The priced library (a multi-Vt rail's primary liberty, built by
`scripts/mvt/make_abc_lib.py`) is derived from the rail's real liberties.
If those change (a re-characterised corner, a new list entry) or the file
is edited, it is stale: the flow would map with prices that no longer
describe the libraries it times with. So `make_abc_lib.py` writes, as the
first line of its output, one machine-readable comment:

```
/* make_abc_lib provenance v1: {"inputs":[{"name","sha256","size"},...],
   "leakage_libs":[...same...],"W":..,"S":..,"exclude":[..],"keep":[..],
   "keep_dont_use":..,"cells":N,"dropped":{..},"tool":{"git","sha256"},
   "body_sha256":"..","body_size":N} */
```

(really one line; `*/` inside the JSON is escaped). `body_sha256` hashes
every byte after that line. Hashes are streamed; names are file names, not
paths.

`python3 -m circt_flow.techpack check` applies this rule to each rail that
has a priced library: with `corners:`, a rail whose first liberty is not
one of its setup corner's files; without `corners:`, a rail whose first
liberty has a provenance line (nothing else marks a primary as priced). Let
SRC be the rail's setup corner list (without corners, its `liberties:` after
the first) and LEAK the leakage corner's list. The library is

- `ok` when all of these hold:
  1. its recorded `inputs` and SRC are the same set of files by sha256
     (order and location do not matter);
  2. its recorded `leakage_libs` and LEAK are the same set by sha256; when
     it records none, the leakage corner must be the setup corner (or the
     pack has no corners);
  3. the file after its first line still hashes to `body_sha256`;
  4. every cell of the library is a cell of a file in SRC.
- `stale` when the line is present and any of these fails. The reason names
  the file ("input X changed since the library was built", "input X is in
  the rail's list but was not an input", "edited after it was built", ...).
  `check` prints it as an `ERROR` and exits 1.
- `unverified` when there is no provenance line (a library built before
  the line existed, or not built by this tool).
  `check` prints a `WARNING` and exits 0.

`W`, `S`, the `-X` / `--keep` patterns and the tool revision are recorded
but not compared: the pack does not state them, so the file's own record is
all there is. The fix for a stale library is to rebuild it (the
`examples/*/run.sh` scripts do).

The flow runs the same rule when it loads a pack's profile
(`pdk.profile`), controlled by the environment variable
`CIRCT_ABC_PROVENANCE`: `warn` (default) prints stale and unverified rails
to stderr once per process, `error` refuses a stale library (unverified
still only warns), `off` skips the check. Cost on ASAP7 (about 107 MB of
liberties): 0.55 s cold, once per process.

## Power domains

A flow config's `power:` section puts parts of the design on rails:

```yaml
pdk:
  profile: mytech                  # a schema-2 pack
power:
  domains:
    - {name: lv, rail: VDD_LV, instances: [u_core_lv]}   # instance paths,
    - {name: aon, rail: VDD_AON, module: always_on}       # or a module
```

Everything else is on `default_rail`. An instance path is dot-separated
from the top (`u_sub.u_core`); `module:` means every instance of that
module. Unknown rails, unknown instances, an instance in two domains and
nested domains are errors. A domain's module must not also be instantiated
outside the domain or in another domain (a per-rail copy is not built yet);
a module elaborated with different parameters is a different module, so
two parameterizations may be on different rails.

The flow then elaborates the design once (`<out>/hier/`), synthesizes each
domain's module as its own top through the ordinary flow on its rail's
liberties and flow defaults only (`<out>/domains/<name>/`, from a generated
`flow.yaml` that can be re-run on its own), synthesizes the rest on the
default rail with the domain modules as black boxes (`<out>/remainder/`),
and stitches `<out>/1_synth/<top>.v`, a hierarchical netlist whose domain
boundaries are module boundaries, plus the OpenSTA-compatible one.
`<out>/power_domains.json` records, per domain, the rail and voltage, the
instance paths, the module, the liberties (with the primary one's SHA-256)
and ABC settings it was mapped with, its netlists, and a check that every
cell it uses is a cell of its rail's liberties.

With modes, each domain gets its own mapping target (see "Modes and the
mapping target"). `examples/mvt-mini/`'s `mvt_top` is the worked example.
Not done yet: `--pnr` and `--dont-touch`/`--dont-gate` with `power:`, a
domain module also instantiated outside its domain, and nested domains.
The domain name `top` is reserved for the top's own domain.

### Power intent

After stitching, the flow reads the OpenSTA form of the netlist
(`<out>/1_synth/<top>_openroad.v`, which needs sv2v) and writes:

* `<out>/power_intent.json`: every net of the top module whose driver and
  some load are in different domains. A domain's side is an instance of
  its module. Every other instance in the top, and the top's ports, are
  the top's own domain on `default_rail`. Each crossing records the net,
  the driver and loads, both domains and rails, and the rule from the
  rails' voltages: `low_to_high`, `high_to_low`, or `same_voltage`, where
  no shifter is needed.
* `<out>/<top>.upf` (UPF 2.0, IEEE 1801-2009) contains:
  * one supply port and net per rail, plus the ground (`power.ground`,
    default `VSS`, which may not share a rail's name), with
    `connect_supply_net`;
  * a supply set per rail, `SS_<rail>`: its power net and the ground;
  * `create_power_domain PD_TOP -include_scope` for the top's own domain,
    and `PD_<name> -elements {<instances>}` per domain, each with
    `-supply {primary SS_<rail>}`;
  * `add_port_state` per supply with an all-on `create_pst` /
    `add_pst_state`;
  * `set_level_shifter` strategies that put every shifter in its
    **receiving** domain, so a single-supply high-to-low cell is powered
    by the low rail it drives. A crossing into a power domain is a
    strategy on that domain's inputs with `-location self`. A crossing
    into the top's own domain is a strategy on the driving domain's
    outputs with `-location parent`. Each has `-input_supply_set` (the
    driving rail's), `-output_supply_set` (the receiving rail's) and the
    crossing ports in `-elements`.

  Isolation, retention and power switches are not described: every
  supply is always on.
* `<out>/sdc/<mode>.sdc`, one per mode, for place and route: the mode's
  SDC file as written, under a header. The `clock_period_ps` shorthand
  becomes its clock with zero I/O delays.

`power_domains.json` points at all of these under `intent`.

The analysis needs every domain instance to be an instance of the top
module. A domain nested inside a module of the remainder is refused, and
so are `inout` ports and pins.

### Level-shifter insertion

```yaml
power:
  insert_level_shifters: true      # default false: P&R usually inserts them
  domains: [...]
```

With it on, every crossing whose rule is not `same_voltage` gets one
shifter per net and receiving domain, in the top module. The cell comes
from the pack's `level_shifters` liberties for that direction and those
voltages (see Rails). Its input is the driver's net, and its output, a new
net `<net>_ls_<domain>`, feeds every load of that domain on the net.
Instances are named `ls_<domain>_<net>`.

* The shifted netlist replaces `<top>_openroad.v`, and the unshifted one
  is kept as `<top>_unshifted_openroad.v`. `<top>.v`, the ExportVerilog
  form, stays unshifted.
* The flow then checks that the two netlists are the same circuit, with
  every shifter read as a buffer. Every module but the top must be
  byte-identical. In the top, the ports and the other instances must
  match, and every input pin and output port must be driven by the same
  source, following shifters. Every crossing that needs a shifter must
  have one. A failure stops the run.
* The UPF has no level-shifter strategies then. Each inserted shifter is
  an element of the domain it drives into, so a high-to-low shifter into a
  low-voltage domain is on that domain's supply, and the UPF lists the
  instances in comments.
* `connect_supply_net -pins` wires every inserted shifter's supply pins:
  the input side to the driving rail, the output side to the receiving
  rail, the ground to the ground. A pin's side comes from the liberty:
  the power pin an input pin names in `related_power_pin` is on the input
  side, the one an output pin names on the output side (a single-supply
  cell's one rail is the output side), and `related_ground_pin` or a
  ground `pg_type` marks the ground. This works on libraries whose
  `pg_pin` groups were stripped, as long as the signal pins keep
  `related_power_pin`. A shifter whose liberty names no supply pins gets
  a warning and a `NOT CONNECTED` comment, never a guess.
* Nothing is rewritten when no crossing needs a shifter.
* A domain output that drives a top-level output port with no net in
  between cannot be shifted. That case is refused, naming the net.
* With the option off (the default), nothing is inserted and the netlists
  are those of a run without it.

### Static timing: `--sta` and `sta:`

```yaml
sta:                     # the section turns STA on; or pass --sta
  enabled: true          # false keeps the settings with STA off (--sta wins)
  corners: [tt, ss]      # default: every corner of the pack
  modes: [func]          # default: every mode
  chip: true             # power flows: also time the whole chip
  boundary_budget: 0.5   # outside share of the period at domain boundaries
  image: openroad/orfs:26Q3-277-gf9ec54a6d
```

A corner or mode that `corners:` / `modes:` names but the run lacks fails
when the config loads, before synthesis.

The stage runs OpenSTA from the ORFS image, or the binary `$OPENSTA`
names, after synthesis. It writes `<out>/sta/summary.json` and prints a
table of WNS/TNS (setup) per domain x corner x mode. Each view's SDC, Tcl
and log are kept under `<out>/sta/`.

A view fails the stage on an OpenSTA `Error`, and also on a warning that
means the view is not the one asked for: a cell linked as a black box, an
SDC object not found, or a constraint left with no objects (those
constraints would otherwise be dropped silently). Other warnings are kept
in the summary as `sta_warnings`.

**Domains are timed separately.** OpenSTA binds a netlist cell to a
liberty cell by name, and the same names exist on every rail and at every
corner. One link of a mixed-rail netlist would therefore time every cell
of a name with one rail's library. So each domain module is linked alone,
`link_design <module>`, with its own rail's liberties at the corner. The
domain's SDC is derived from the mode:

* each mode clock whose port reaches a domain port (through the top's
  nets, assigns and level shifters) is created on that port with the
  mode's period, and the mode's other clocks are virtual;
* a domain port that reaches a top-level port with a mode
  `set_input_delay` / `set_output_delay` gets that delay, and one that
  reaches a top-level port the mode leaves unconstrained gets 0;
* every other boundary port, one that meets another domain or the top's
  glue, gets `boundary_budget` (default 0.5) times the period of the
  domain's own clock: the share left to the logic on the other side.
  With 0.5, two domains that each pass their own view leave each other
  half the period, so a direct path between them fits; the glue and
  shifters in between are not counted.

The top's own domain is linked with the domain modules as boundary cells:
a generated liberty with their pins and no timing arcs. Its SDC is the
mode's own plus 0 delays at the domain instances' pins.

**The chip view times boundaries exactly.** The budget is an assumption,
and the top's own view still uses 0 at the domain pins. The chip view
(`chip: true`, power flows only) times the real paths. It copies the stitched netlist with every cell renamed after
its rail (`<rail>__<cell>`) and each rail's liberties at the corner with
the same renaming (`<out>/sta/libs/<corner>/<rail>/`). It then times the
whole design, cross-domain paths and inserted shifters included, with the
mode's own SDC. Each endpoint is attributed to the domain it sits in.

**Hold.** Every view also records its worst hold slack by path class:
`hold_r2r_ns` (register to register), `hold_in_ns` (from inputs) and
`hold_out_ns` (to outputs). The summary's `hold` entry is the worst of
each over the views at the pack's hold corner, and the table prints it.
In a power flow with a chip view, only the chip views count: a domain
view's boundary delays are assumptions (a port the mode leaves
unconstrained gets 0 there, which fails hold by the flop's hold time),
while the chip view times the real paths.

```
[sta] hold at ff (chip view, ideal clocks): worst +0.0050; reg-to-reg +0.0120, from inputs +0.0200, to outputs +0.0050
```

The I/O classes are only as real as the mode's `-min` I/O delays. An
input delay of 0 straight into a flop fails hold by the flop's hold time.
Give inputs a `set_input_delay -min` of an upstream flop's clock-to-Q at
the hold corner. When several bits of a domain port carry different `-min`
delays, the domain view takes the smallest.

Limits:

* the summary's WNS/TNS are setup timing, and hold is timed with ideal
  clocks: a check of the netlist and the constraints, not signoff;
* a clock that reaches a domain through logic other than level shifters
  is not traced, so that domain's own view is unclocked (with a warning);
* a domain's own view holds no level shifters (they are in the top and
  chip views);
* without corners, a rail's mapping liberties are used. Those may include
  a priced merged liberty, whose cells duplicate the real ones; OpenSTA
  binds the first library it read that has the cell.

A flow without `power:` gets one view per corner and mode, the top on its
rail. Neither the netlist nor any other output changes when STA is off.

### Fanout buffering: `flow.fanout_buffer` and `fanout_buffer:`

```yaml
# in the pack (or a rail's flow:)
flow:
  fanout_buffer: {cell: BUF_X4, input: A, output: Y, max_fanout: 16}
# in a flow config: override any key, or turn it off
fanout_buffer: {max_fanout: 32}      # or: fanout_buffer: false
```

ABC maps each partition on its own and never sees a net that leaves it,
and the register and clock-gate logic outside ABC buffers nothing either,
so a flop or gate driving hundreds of pins across partitions reaches STA
with its whole load on one output. On a large design those nets, not the
logic, set the worst path, and the vt_swap stage, which only changes
flavours, cannot fix them: it moves cells faster chasing a load it cannot
change.

When the pack (or the flow config) sets a buffer cell, a single-domain
run buffers its flat OpenSTA netlist after synthesis, before vt_swap and
STA (`circt_flow/fanout.py`): every net driven by one cell output with
more than `max_fanout` cell input pins, none of them a clock pin
(`clock : true`; clock trees are CTS's), gets a balanced tree of the
buffer cell. Loads are grouped in instance-name order, so pins of one
hierarchy branch share a buffer, a stand-in for placement. The driver
keeps its net, so ports and `assign`s that read it are untouched. Nets
driven by a port are left alone. The unbuffered netlist is kept as
`<top>_prebuf_openroad.v`, and `<out>/fanout.json` lists the counts and
the largest nets. P&R's `repair_design` rebuilds the trees on a real
placement; this stage exists so that timing before P&R, and the swap
driven by it, see realistic loads.

**In a power flow** the stage runs after the intent step (crossings, UPF,
level shifters) and before vt_swap, on the stitched OpenSTA netlist, one
module at a time with that module's rail's cells (the top and the glue on
the default rail), with the buffer of that module's rail (below). A
submodule's input port counts as a driver: a domain's flops sit in its
module and ABC's logic in a `_residual` submodule, so a flop's net fans
out through a port, and its tree is built on the load side, inside the
submodule. The top's own inputs are left alone, as in a flat run. Nets
that cross a domain boundary end on module ports, so the crossings and
their level shifters stay as the intent step left them, and every buffer
sits in a module of its domain (the UPF names module instances). A module
under domains of two rails is not buffered (a warning says so).

**Per rail.** A buffer cell from one rail's library is wrong on another
rail, so on a schema-2 pack `fanout_buffer` can also sit in a rail's
`flow:`:

```yaml
flow:                         # pack level: the default for every rail
  fanout_buffer: {cell: BUF_X4, max_fanout: 16}
rails:
  VDD_LV:
    flow:
      fanout_buffer: {cell: BUF_X2, max_fanout: 8}   # keys override one by one
  VDD_RET:
    flow:
      fanout_buffer: {enabled: false}                # this rail is not buffered
```

A rail's keys (`cell`, `input`, `output`, `max_fanout`) override the
pack-level ones key by key, so a rail may change only the limit and keep
the pack's cell; `enabled: false` turns the rail's buffering off. A flow
config's `fanout_buffer:` is applied over every rail's spec, and
`fanout_buffer: false` turns all of them off. In a power flow each module
is buffered with the cell, pins and limit of its own rail, checked against
that rail's setup-corner libraries (and the level shifters'); a rail with
no spec is left alone, and `<out>/fanout.json` lists each rail's cell and
limit and, per module, the cell used. A flat run (`pdk.rail:`, or the
default rail) reads the spec from that rail. `techpack check` prints each
rail's buffer and fails when the cell is not in the rail's liberties.

### Wire model: `flow.wire_model` and `wire_model:`

Flow STA runs with ideal clocks and no parasitics, and so does the
vt_swap stage's timing. After place and route every net also carries a
wire, and on a deep-submicron node the wire capacitance is comparable to
the pin capacitance. A design that the flow times as just meeting its clock can then
miss it after P&R. A wire model puts an expected wire on every net before there is a
placement, so the STA stage and vt_swap time, and size flavours, against
something close to what P&R will extract.

The pack declares it (a property of the node and the P&R recipe, fitted once
from extracted parasitics: see `scripts/fit_wire_model.py` for the fit; the numbers
in the examples of this page are illustrative):

```yaml
flow:
  wire_model:
    cap_per_fanout_ff: 0.3      # wire capacitance per load pin, fF (illustrative)
    cap_base_ff: 0.2            # plus this per net, fF (illustrative)
    res_per_fanout_ohm: 0       # optional wire resistance, ohm, per load pin
    res_base_ohm: 0             # plus this per net
```

A net with `n` load pins (instance inputs and output ports, over the
flattened netlist) gets `C = cap_base_ff + cap_per_fanout_ff * n`, as
`set_load` on the net. With a resistance (`R = res_base_ohm +
res_per_fanout_ohm * n`, from the driver to each load) the flow writes a SPEF
with one pi model per net instead and reads it; it carries the capacitance
too, because OpenSTA ignores a net's parasitics once `set_load` has given it
a capacitance. Capacitance alone follows P&R's extraction well when the
wire resistance is small against the cells' drive; add a resistance only if
the extraction shows wire delay.

The model is off unless the pack declares it, so existing packs are
unchanged. A flow config's `wire_model:` overrides it: `false` turns a
pack's off, `true` uses the pack's, and a mapping replaces the named keys
(and can declare a model on a pack that has none).

It applies to the STA stage (every view: domains, chip, corners; the
summary's `wire_model` records it) and to the vt_swap stage (the whole
OpenSTA session: the fix and recovery steps size against it; the record in
`vt_swap.json` carries it, and so do `scripts/mvt/vt_swap.py`'s). It does not
reach ABC's mapping, whose delay target stays `mapping_target`, nor the
register-mapper's criticality list (`multibit_timing`). Because the swap
then sees the wires, the flavours it chooses are faster than without the
model, which costs leakage and may not be reachable at all when the mapped
netlist's wire-free slack was spent by `mapping_target`; the wire-aware
target is a different (lower) `mapping_target.scale`. A model is a
per-net mean: a net P&R routes long still fails against it. `rc_corner: <corner>` (optional) names the
pack `wire_rc:` corner the numbers were fitted at; each view then scales
them to its own corner's wire RC (see "Wire RC" below).

### Wire RC: `wire_rc:`

A placed design's wire parasitics come from per-layer RC (OpenROAD's
`set_layer_rc`) and, before routing, one RC per micron for every signal and
clock wire (`set_wire_rc`). Those numbers belong to the node and the
extraction corner, so the pack names them, one OpenROAD Tcl file per
corner:

```yaml
wire_rc:
  tt: ${MYTECH_RC}/rc_typical.tcl          # a path, or
  ss:                                     # a path and its sha256 pin
    path: ${MYTECH_RC}/rc_cmax.tcl
    sha256: 3f5c...
  ff: ${MYTECH_RC}/rc_cmin.tcl
```

A private pack names files only: the values stay in the PDK's (or the P&R
flow's) tree and the pack carries at most their sha256, like the library
provenance check. A `public: true` pack may give the values inline instead
(ohm and fF per micron; `clock` defaults to `signal`):

```yaml
wire_rc:
  tt:
    signal: {res_ohm_per_um: 20, cap_ff_per_um: 0.2}
    clock:  {res_ohm_per_um: 10, cap_ff_per_um: 0.1}
    layers: {M2: {res_ohm_per_um: 25, cap_ff_per_um: 0.2}}
    vias:   {V1: {res_ohm: 10}}
```

A pack without `corners:` gives one entry (`wire_rc: <path>`, or the
mapping). With corners, every key is a corner name, and the setup and hold
corners must be covered. Every corner is a file or every corner is inline,
not a mix.

`techpack check` prints each file with its size and sha256 and the pin's
verdict, and fails when a file is missing or empty, when it no longer
matches its pin, or when the setup or hold corner has no entry. With a
local `tclsh` it also reads each file's `set_wire_rc -signal` (an error
when a wire model scales by it, below; a warning otherwise).

Who uses it:

* **P&R.** The stage gives OpenROAD the setup corner's file. ORFS gets a
  copy in the run directory as `SET_RC_TCL` on the make command line, which
  overrides the platform `config.mk`'s own `setRC.tcl`. A pack's own P&R
  command gets `CIRCT_FLOW_WIRE_RC` (the setup corner's file) and
  `CIRCT_FLOW_WIRE_RC_<CORNER>` for every corner in its environment.
* **STA and vt_swap**, with a wire model that names the corner it was
  fitted at:

  ```yaml
  flow:
    wire_model: {cap_base_ff: 0.2, cap_per_fanout_ff: 0.3, rc_corner: tt}
  ```

  These stages time a netlist without a placement, so RC per micron cannot
  be applied directly; instead each view scales the model to its corner:
  capacitances by the view corner's signal capacitance per micron over
  `rc_corner`'s, resistances likewise. The factors are read from the files
  inside the OpenSTA session (a child interpreter where `set_layer_rc` /
  `set_wire_rc` only record their arguments, and a helper that an RC file may call to read environment overrides is stubbed),
  so the files' units cancel; all of a pack's files must use the same
  units. vt_swap times setup at the setup corner and hold at the hold
  corner, so it puts `-max` loads scaled to the first and `-min` loads
  scaled to the second. A view at a corner with no `wire_rc` entry fails.
  The STA summary records each file and its sha256 (`wire_rc`), and the
  view logs print the factors (`circtflow wire rc: ss against tt: C x ..`).

Without `rc_corner` the wire model is not scaled, and without `wire_rc`
nothing changes: P&R uses the platform's RC.

### Multibit flops: `flow.multibit` and `multibit:`

A pack that has multibit flop cells declares them under `flow:`; a flow config turns the mapping on with `multibit: true`. The register mapper then packs bits that share clock, reset and flop class into banks of the declared widths before ABC (design in `doc/passes.md`, "multibit banking"). The pack names every cell, pin and width; the pass names none.

```yaml
flow:
  multibit:
    default_family: a
    cross_register: false        # true: pool leftover bits across registers
    families:
      a:
        clear:                   # async clear-to-0 bits
          cell: TBK{n}_CLR_A  # {n} = width
          widths: [8, 4, 2]
          clock: CK
          reset: R
          reset_active_high: true
          data: "D{i}"           # {i} = bit within the bank
          out: "Q{i}"
          tie: {SE: 0, SI: last_q}   # 0, 1 or last_q (the bank's own last output)
        none: {cell: TBK{n}_NRS_A, widths: [8, 4, 2], clock: CK, data: "D{i}", out: "Q{i}", tie: {SE: 0, SI: last_q}}
      single: {}                 # a family with no classes keeps bits single-bit
```

The flow config's `multibit:` takes `true` or a mapping: `default_family`, `cross_register`, `overrides` (`{reg: <regex on the register name>, family: <name>}`, first match wins; names are module-local), `overrides_file` (a JSON list of the same) and `activity` (`{file: rates.json, hot_family, hot_toggle_rate}`; `python -m circt_flow.multibit_activity <tcf>` makes the rates file). `activity` also takes `rule: energy` (below). `python -m circt_flow.multibit_timing <out_dir> --margin 0.05` (an example margin, in ns) reads a finished run's OpenSTA view and writes an `overrides_file` that sends the registers on near-critical paths to the `single` family: a multibit cell's clk-to-Q and setup can differ from the single-bit flop's, so banking everything can cost slack that a rerun with that list gets back. `circt_flow techpack check` verifies every declared cell is in the rail's liberties. Cells with inverted outputs, async-set classes and enable flops are not mapped (bits of those stay single-bit).

#### Activity-driven family choice: `multibit.energy` and `activity: {rule: energy}`

A library's multibit families trade clock-pin energy against data energy, leakage and area, and which one is cheapest depends on how often a register is clocked and how often its bits change. A pack declares a per-bit energy model next to the families (the numbers below are example values only):

```yaml
flow:
  multibit:
    energy:
      period_ns: 1.0                  # default; the activity file may set its own
      area_weight_nw_per_um2: 5       # what one unit of area is worth in nW (0: power only)
      families:                       # only the families to compare
        a: {clock_aj: 10, q_aj: 10, d_aj: 10, leak_nw: 1, area_um2: 2}
        b: {clock_aj: 10, q_aj: 100, d_aj: 10, leak_nw: 2, area_um2: 1}
```

`clock_aj` is the internal energy per clock pulse and bit, `q_aj` per output toggle, `d_aj` per input toggle. The flow config's `multibit: {activity: {file: act.json, rule: energy, candidates: [a, b], area_weight_nw_per_um2: ...}}` then gives every register the family that minimises `(clk*clock_aj + q*q_aj + d*d_aj)/period_ns + leak_nw + area_weight*area_um2` (nW per bit); ties and registers missing from the file stay on `default_family`. The terms scale with the register's own activity, so which family is cheapest differs between busy and quiet registers; with the example values above the area term decides for quiet ones. Without `rule: energy` nothing changes (the threshold rule and the static default are as before).

The file comes from a simulation of an earlier run's netlist: `python -m circt_flow.multibit_activity --netlist run.v nets.tsv --cycles N -o act.json` (or a TCF in place of the TSV; a TSV is `net block toggles ...`, block 0 the whole span). The netlist supplies the register-mapper's own instance names (`<reg>_mb<a>_<b>`, `<reg>_bit<k>`), the clock pin and the output/input pin names (`--clock-pin`, `--out-pin-re`, `--in-pin-re`); the file is `{"period_ns": T, "registers": {reg: {"q", "d", "clk", "bits"}}}` with names as the mapper sees them. The coefficients are the pack's own numbers; the flow code names no cell.

### Clock-gate economy: `flow.clock_gating` and `clock_gating:`

`clock-gate-inference` builds one clock gate per (clock, enable) group. On a
design whose enables are many spellings of a few functions that is far more
gates than needed, and every gate costs clock-network power (its own clock pin,
internal power, the clock buffers CTS builds on its output). The economy
options trim them; none names a cell, and nothing happens unless the flow
config opts in with `clock_gating:` (`true`, or a mapping that overrides the
pack's values). A pack declares what a gate costs on its library, in units of
one flop bit's clock-pin power:

```yaml
flow:                                  # the pack (example values)
  clock_gating:
    merge_equivalent_enables: true     # one gate per enable FUNCTION
    gate_cost_bits: 4.0                # a gate costs 4 flop clock pins
    gate_idle_fraction: 0.4            # share of cycles a gated group idles
    enable_op_cost_bits: 0.5           # per synthesized enable cell
    hold_mux_bits: 0.0                 # saving per bit of the hold mux
    min_gate_bits: 4                   # never gate fewer bits
    max_gate_depth: 3                  # gates in series on a clock path
```

```yaml
clock_gating: true                     # the flow config
```

The options are described in `doc/passes.md`.

### Slow-cell filter: `flow.slow_cell_filter`

ABC's load-aware delay model is optimistic for cells whose delay is dominated
by a tall transistor stack (4-input NAND/NOR, wide AOI, and so on), so area
recovery can pick them although a netlist with more levels of 2-input cells is
faster after placement. `flow.slow_cell_filter: {ratio: 1.3, slew_ps: 30,
load_ff: 2.5}` (example values; opt-in; absent means off, or `CIRCT_FLOW_SLOW_CELL_RATIO=<ratio>`)
lists the combinational single-output cells whose delay at one reference slew
and load exceeds `ratio` times the median of all of them and hands them to
ABC's `read_lib` as `-X <cell>`. Nothing in it knows a cell name: delays come
from the liberty's NLDM tables, and the pack's `vt_flavours` regex makes a cell
and its flavour twins one family, judged by its slowest member.

### Shift select: `shift_select:`

A flow config's `shift_select:` section turns on the `map-shift-select` pass
(a `case` over a shift amount as a network of AND-OR cells) without environment
variables; the options, the one that is not exact (`default_dont_care`) and the
example `examples/shift-select` are in `doc/passes.md`. The key lives in the
flow config only, never in a pack.

### Threshold-voltage swap: `--vt-swap` and `vt_swap:`

```yaml
# the tech pack: how a cell name carries its flavour, slowest first
vt_flavours:
  tokens: [R, L, SL]
  re: '_(SL|R|L)_'           # the token is the group (default `_([A-Z]+)$`)

# the flow config: the section turns the stage on; or pass --vt-swap
vt_swap:
  false_from: [rst_ni]       # ports whose paths are not timed
  paths: 200                 # failing paths whose cells move per iteration
  max_iter: 30
  recover: true
  margin_ps: 0               # setup slack to keep in hand for P&R
  keep_clock_cells: false    # leave clock gates at the flavour synthesis gave them
  false_to: [outputs]        # endpoints whose paths are not timed (ports, or `outputs`)
```

`keep_clock_cells: true` leaves every cell other than a buffer or inverter that
drives a clock net (the clock gates) at its synthesized flavour; `false_to`
lists output ports (or the word `outputs`) whose paths the swap ignores, like
`false_from` does for input ports.

ABC chooses flavours on its own delay model, which can be far from the
libraries' (by a factor of two or more on some libraries), and the
register mapper and
clock-gate inference always place the slowest flavour. The stage corrects
both with real timing. It runs OpenSTA on the OpenSTA netlist after
synthesis, before the STA stage, with the rail's setup-corner libraries
(its mapping liberties when the pack has no corners, so such a pack must
not list a priced library first) and the tightest mode's SDC plus
`false_from:`:

1. **Fix.** While the worst setup slack is below `margin_ps`, every
   instance on the worst `paths` paths below it moves one flavour faster,
   flops and clock gates included. An iteration that does not improve the
   worst slack is undone and ends the loop (faster cells load their
   drivers slightly more, which can hurt on long, heavily loaded paths).
   Faster cells shorten hold paths too. When an iteration takes the worst
   hold slack below `min(0, hold slack before the swap)`, its moved cells
   on the failing hold paths go back and are frozen for the rest of the
   swap (`held` in the record); if hold is still worse, the whole
   iteration is undone. Hold is timed on the same SDC with ideal clocks
   at the pack's hold corner (`corners.hold`; its libraries are read
   with `read_liberty -min` and the setup corner's with `-max`, so one
   OpenSTA session times both). This keeps the swap from creating
   violations rather than signing hold off.
2. **Recover.** Instances above the slowest flavour step one flavour
   slower in batches of 256: a batch stays if the worst slack holds at
   `min(margin_ps, slack after fixing)` (and, for a design the fix did not
   close, the total negative slack at its value after fixing), otherwise
   it is undone and
   halved, down to single cells; passes repeat until nothing moves. Timing
   updates then grow with the cells that must stay fast rather than with
   every candidate (one update per cell is far too slow on a large
   netlist).

The swap assumes the flavours of one cell have the same pins, so it renames
cell types and nothing else (a flavour with another area changes the
netlist's area). The unswapped netlist is kept as
`<top>_preswap_openroad.v`, and `<out>/vt_swap.json` lists every swap with
the slack before and after. `scripts/mvt/vt_swap.py` runs the same swap on
any netlist.

**In a power flow** the stage runs after the intent step (crossings, UPF,
level shifters) and before STA, on the **chip view** as the STA stage
builds it: the whole stitched netlist with every cell renamed after its
rail (`<rail>__<cell>`) and each rail's setup-corner libraries renamed the
same way, timed with the mode's own SDC. Every path is exact, cross-domain
ones and level shifters included. The tightest mode fixes and recovers;
each other mode then fixes only. The changes go back into the stitched
netlist with the prefixes stripped; a module under domains of two rails
is not swapped (a warning says so).

Swapping each domain alone in its own view was tried first and is not
sound: the view misses the glue and shifters on the domain's inputs, so
its recovery can leave a cross-domain path that passes in the domain's own
view failing in the chip view.

**Per rail.** `vt_flavours:` may also be set in a rail
(`rails.<name>.vt_flavours`), replacing the pack-level list for that rail:
a rail can have a different ladder or leave a flavour out of the swap,
and a rail with none inherits the pack's. The list limits the swap only:
cells ABC mapped to a flavour outside it stay as they are, so keeping a
flavour out of a rail altogether also takes `dont_use` or a priced
library without it. A flat
run uses the run's rail's list. In the chip view each cell moves along
the list of its rail (the `<rail>__` prefix tells), so cells of a rail
never reach a flavour the rail does not list; level shifters never move.
When the rails' lists differ, `vt_swap.json` records them and the flavour
counts are keyed `<rail>.<token>`. `techpack check` prints each rail's
list and, for each flavour, how many cell families (names equal but for
the flavour token) lack it, which the swap skips silently.

`examples/mvt-mini` shows the swap on its cores and power designs (its
README has the numbers).

## Modes and the mapping target

`design.clock_period_ps` is the single-mode shorthand: one clock of that
period on the design's clock port (`pnr.clock_port`, else the manifest's
`constraints.clock_port`). A flow config may instead name one SDC file per
mode:

```yaml
modes:
  func:  func.sdc                 # relative to the flow config
  turbo: {sdc: turbo.sdc}
mapping_target:                   # optional; these are the defaults
  flop_overhead: auto             # auto | <ps> | false
  flop_cells: []                  # restrict `auto` to these flops
  scale: 1.0                      # in (0, 2]
```

With `modes:`, `clock_period_ps` may be left out; it then defaults to the
tightest clock of any mode. The SDC subset read is listed below. Anything
else is printed as a warning and ignored:

* `create_clock -period <p> [-name <n>] [get_ports <port>]` (or
  `{<ports>}`); without `-name` the clock is named after its port; without
  a port it is a virtual clock, which clocks no domain;
* `set_input_delay` / `set_output_delay <d> -clock <n> [-max|-min] <ports>`
  (not used for mapping; the STA stage maps them onto each domain's
  boundary, see "Static timing");
* `set_units -time ns|ps`. Otherwise SDC times are in the pack's
  `time_unit`.

`#` comments, `;` and `\` continuations are honoured. Tcl variables and
expressions are not.

**The mapping target.** With `modes:` or `mapping_target:`, ABC no longer
gets the raw clock period as its delay target (`clock-period-ps`, the
`{period}` of an `abc_script_file`, `&nf -D`). For each domain (the whole
design without `power:`) it gets

```
target = floor(scale * (min over modes of the domain's clock period - flop overhead))
```

* A domain's clocks are the SDC clocks whose ports reach its instances'
  ports in the elaborated hierarchy (directly, or through a
  `seq.to_clock`/`from_clock`/`hw.wire`), or its `clocks:` list
  (`- {name: hv, rail: VDD, instances: [u_hv], clocks: [clk_hv]}`). The
  part of the design outside every domain gets every port clock.
* `flop_overhead: auto` subtracts the worst clock-to-Q + setup over the
  flops (cells with an `ff` group) of the domain's rail at the setup
  corner. A pack without corners uses its mapping liberties. Clock-to-Q
  is read at the smallest clock transition in its table and a load of 4x
  the flop's largest input-pin capacitance (FO4, interpolated); setup at
  the smallest transitions of its constraint table; each is the larger of
  rise and fall. `flop_cells` restricts the search, for instance to the
  flops the register mapper picks. A number subtracts that many ps, and
  `false` subtracts nothing.
* `scale` multiplies the result. A value above 1 corrects for ABC's
  estimate being pessimistic: `&nf -l`'s own delay estimate ran about
  1.2x above STA on `examples/mvt-mini`.

The derivation is printed per domain (`[mapping-target]` /
`[power] mapping target`), written to `<out>/mapping_target.json`, and for
a power flow stored in `power_domains.json` under each domain's
`mapping_target`, with the modes and corners. Each domain's generated
`flow.yaml` carries the target as `design.abc_delay_target_ps` and the
tightest period as `clock_period_ps`. A flow config may also set
`design.abc_delay_target_ps` directly. Without `modes:` and
`mapping_target:` (in the flow config or the pack), nothing changes: ABC
gets `clock_period_ps`.

**A pack-level calibration.** How optimistic ABC's delay model is depends
on the library, so a tech pack may set `flow: {mapping_target: {...}}`
(the same keys), and a rail may override it in its own `flow:`. Every run
on the pack then calibrates; in a power flow each domain uses its own
rail's. A flow config's `mapping_target:` replaces the pack's. When the
load-aware model is optimistic, a target at the period leaves ABC all
the slack it thinks it has to recover leakage with: it chooses the
slowest flavour everywhere and the netlist misses timing in STA. A
`scale` below 1 corrects that. Calibrate it per rail, since the error
depends on the supply too: sweep `scale` on a representative design at a
clock the slowest flavour cannot meet, and keep the value with the least
leakage that still meets timing after vt_swap.

## Finding a pack

A flow config names the technology:

```yaml
pdk:
  profile: mytech
```

`profile:` is either a path (it contains a `/` or ends in `.yaml`, and is
relative to the flow config) or a name, looked up in order:

1. a pack enclosing the flow config's directory, whose `name:` matches, so
   a pack's own `designs/*/flow.yaml` needs no setup;
2. `<dir>/<name>/techpack.yaml` for each `<dir>` on `CIRCT_ABC_TECH_PATH`
   (colon-separated);
3. a `<name>-techpack` or `circt-abc-techpack-<name>` checkout beside this
   repository;
4. `profiles/<name>.yaml` in this repository.

`python3 -m circt_flow.techpack list` prints the packs the audit would see
and whether each is public; `techpack root <name>` prints a pack's
directory, for scripts that read files from it.

`design.manifest:` is a path relative to the flow config, or a bare name
for a design in this repository's `designs/<name>/manifest.yaml`. A pack
with a private design keeps its manifest beside the flow config. A
manifest's `source:` is a git repository at a pinned commit, or `path:`, a
directory relative to the manifest, for RTL kept beside it.

A `source:` checkout is cached under the flow's cache root, named for the
design and the first 12 characters of the commit. Flows that run in
parallel (`examples/asap7-mvt/run.sh` starts four) share it: the first
fetches under an exclusive file lock (`.<name>.lock` beside the checkout)
and the others wait, then find it at the pinned commit. A checkout counts
as ready when its `HEAD` is the pinned commit; a directory that exists only
because `git init` ran before the fetch does not.

## Precedence

Each setting a pack can default resolves, highest first:

1. the command line (`python3 -m circt_flow ... --sv2v-docker-image ...`);
2. the environment (`SV2V_DOCKER_IMAGE`, `ENABLE_CLASSICAL_RESUB`,
   `ABC_PARTITIONS`, or the `CIRCT_FLOW_*` knob itself);
3. the pack's `flow:` default;
4. the flow's built-in default.

The keys a `flow:` section may set are `profile.FLOW_DEFAULTS`:
`sv2v_docker_image`, `abc_script_file` (a path in the pack),
`abc_read_lib_args`, `enable_classical_resub`, `partitions`,
`mapping_target` (see "Modes and the mapping target"), `fanout_buffer`
(see "Fanout buffering"; a rail's own overrides it key by key), and `env`.
`env` sets defaults for the flow's `CIRCT_FLOW_*` pass knobs (the ones
`stages.py` documents); only `CIRCT_FLOW_` names are accepted, a YAML
boolean means `1`/`0`, and a knob the environment already sets keeps its
value. The flow prints each default it applies.

`CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1` moves the outlining of unmapped logic
after the hierarchy is flattened, so ABC receives one residual module
spanning the design instead of one per module. The exported netlist is
still flat: the single `<top>_residual` module is made private before the
final flatten, so the fanout stage, sv2v and OpenSTA's flat view see one
module.

The built-in sv2v image is `circt-abc`, the image this repository builds;
inside that image `SV2V` is set and no image is needed.

## Place and route

`--pnr` runs place and route after synthesis, from the flow config's
`pnr:` section (`clock_port` is required; `platform`, `clock_period`,
`clock_latency`, `utilization`, `place_density` and `set:` are ORFS's).
The ORFS stage also takes clock-tree and margin controls from the flow
config's `pnr:` (`cts_buffers`, `cts_cluster_size`, `cts_cluster_diameter`,
`cts_args`, `hold_margin`, `setup_margin`); a pack's `pnr:` may give the same
keys as defaults, and the flow config's value wins.
What runs depends on the pack's own `pnr:`:

* **Nothing:** stock ORFS on the built-in platform the flow config names.
* **`orfs_platform_dir`:** stock ORFS on the pack's platform directory.
  The stage sets `PLATFORM_DIR` in the run's `config.mk`, which ORFS's
  `scripts/variables.mk` takes ahead of its built-in platforms, and mounts
  the directory read-only into the ORFS container. ORFS itself is not
  modified, so the result is still a stock-ORFS result. The directory needs
  ORFS's platform files (`config.mk` and what it names); the flow config's
  `platform:` is the name ORFS reports under.
* **`command`:** any other tool. The flow runs the command from the pack
  directory with

  ```
  --netlist <out>/pnr/<top>.v --sdc <out>/pnr/constraint.sdc --top <top>
  --out <out>/pnr --pack <pack dir>
  ```

  appended. The netlist is the OpenSTA-compatible one with the pack's tie
  cells on its constants; the SDC is the one the ORFS stage writes. The
  command must write `<out>/pnr/pnr_metrics.json` with any subset of the
  keys `orfs_pnr.METRICS` names (`instance_count`, `stdcell_area`,
  `setup_ws`, `drc_errors`, ...) and exit 0; the flow adds `status`. Words
  of the command that start with `./` or `../` are relative to the pack,
  and every word has `${VAR}` expanded. The template's `pnr/run.py` is a
  stand-in that only counts cells.

  A pack whose command can stop after a stage and restart from one declares
  `stage_restart: [place, cts, route]` in its `pnr:` (stage names in flow
  order); the flow's closure ladder (`pnr: {ladder: ...}`,
  [`doc/ladder.md`](ladder.md)) then places once and runs the variants of a
  later-stage option from that placement. Without it the ladder runs every
  variant in full.

The flow config's `pnr:` section also takes four options for the ORFS
stage (they do not apply to a pack's own `command`):

* `tie_hi` / `tie_lo` (`CELL:PIN`): the tie cells for constants 1 and 0,
  replacing the pack's `tie`. A one-bit literal in the netlist (in any base
  or spelling) that is an `assign` or net-declaration right-hand side, a pin
  connection, a positional argument or an element of a concatenation is tied
  with them before ORFS runs (OpenROAD reads a literal net as a power net,
  which the router refuses). A one-bit literal in an expression (an operand
  of an operator, a ternary branch) or on an instance pin inside a `generate`
  body is not rewritten; the stage reports it in a warning. Use them when the
  platform carries only another flavour's cells (`ASAP7_USE_VT`).
* `vt_swap: false` passes `--no-vt-swap`: OpenROAD's `repair_timing` may
  not change a cell's threshold-voltage flavour, so the flavours the flow
  chose stay. Sizing and buffering still happen.
* `dont_use`: a list of cell-name globs, each passed as `--dont-use`. The
  resizer is kept off those cells when it picks new buffers and resizes
  (a hook adds them to the platform's `DONT_USE_CELLS` before every step
  that runs `repair_design` or `repair_timing`). Cells already in the
  netlist are left alone. This does **not** stop OpenROAD's Vt swap, which
  still moves cells to a flavour the list names; `vt_swap: false` does.

## Docker

The circt-abc image runs a pack without a rebuild:

```bash
docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp \
  -v ~/techpacks:/tech:ro -e CIRCT_ABC_TECH_PATH=/tech \
  -v $PWD/out:/out circt-abc \
  python3 -m circt_flow /tech/mytech/designs/ibex/flow.yaml /out/ibex
```

A PDK the pack names through `${VAR}` needs its own mount and variable.

## Keeping a private pack private

**Outputs.** A private pack's netlists name its cells. The flow refuses an
output directory inside this repository's working tree when the profile is
a pack without `public: true` (`CIRCT_ABC_ALLOW_REPO_OUT=1` overrides).
Liberties it extracts from an ORFS image for a private pack go under
`$CIRCT_ABC_CACHE` (default `~/.cache/circt-abc`) with mode 0700.

**The audit.** `python3 -m circt_flow.techpack audit [paths...]` refuses the
names of every installed private pack, meaning the packs on
`CIRCT_ABC_TECH_PATH` and the tech-pack checkouts beside this repository,
except those with `public: true`. A pack's names are:

* every cell and library name of six or more characters in its liberties
  (case-sensitive), plus its ICG and tie cells;
* its `deny_terms` (case-insensitive);
* minus its `deny_exceptions`.

The list is built at run time and never written down. A pack whose
liberties are not installed is audited on its own names, with a warning.
With paths, the audit scans those files and directories (inside a git work
tree, the files git does not ignore); with none, it scans the file names and lines
the staged diff adds. It prints each hit and exits 1 if there are any.

**The hook.** `git config core.hooksPath scripts/git-hooks` installs a
pre-commit hook that refuses vendor files by path (liberty, LEF, GDS and so
on, outside the hermetic fixtures) and then runs the audit on the staged
diff. A clone where committing a pack's names is allowed (a private fork
that keeps its own reports, say) turns the audit off with
`git config circt-abc.audit false`; the path check stays on.

## Reference packs

* `examples/techpack-template/` (in this repository): an invented library,
  `lib/template.lib`, generated by `lib/make_lib.py`. It has enough cells to
  map ibex (13,457 cells, 82 of them clock gates, on 2026-10-29) and a
  stand-in P&R command. Its `.lib` is exempt from the hook's path check.
* `circt-abc-techpack-asap7` (public): ASAP7 from the ORFS image, with ibex
  configured as ORFS's own asap7/ibex design. `public: true`.
* `circt-abc-techpack-gt3` (public): GT3, Georgia Tech's open 3-nm GAAFET
  predictive PDK, read from a GT3 checkout. It shows a composite ICG, a
  library whose one flop has both async pins, and an ORFS platform a pack
  generates (buried power rail, abstract tie cells). `public: true`.
