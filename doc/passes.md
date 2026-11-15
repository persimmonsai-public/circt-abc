# Pass reference

The mapper lives in `mapper/`: each pass is declared in
`mapper/include/circt-abc/Passes.td` (summary and description; the options of
`map-shift-select` beyond the first few are declared in its source file), implemented
under `mapper/lib/`, tested under `mapper/test/` (FileCheck, run by
`ninja -C mapper/build check-circt-abc`) and, for the ones that change logic,
checked for equivalence by a Yosys SAT miter in
`scripts/circt_flow/tests/`. `scripts/circt_flow/stages.py` wires them into the
flow. This page is the map: what each published pass does, why it exists, how to
turn it on, and which of its results are exact.

**Convention.** A pass that changes the netlist's structure ships **off** and is
switched on by an option (`enable...=true`), a flow-config key or an environment
variable. The "default" column below says which. Every option can be given to
`circt-abc-opt` directly:

```
circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=cells.lib min-width=16" in.mlir
```

Options of one pass are separated by spaces inside one quoted argument; list
values that contain spaces use a comma instead (a liberty list, for example).

In the flow every switch is an option of the registry
(`scripts/circt_flow/options.py`, reference `doc/options.md`), set with
`--set NAME=VALUE`, a flow config's `options:` mapping, a tech pack's
`flow.options:` or the Python session (`doc/scripting.md`). The
`CIRCT_FLOW_*` / `CIRCT_VT_*` names in the tables below are the legacy
variables those options replace: accepted with a warning naming the option
(`python3 -m circt_flow options --legacy` prints the table), refused under
`--strict` and from release 13 on.

## Registers

| pass | default | what it does |
| :--- | :--- | :--- |
| `register-legalize` | on | Folds synchronous and non-constant resets into the D input, so the mapper sees two shapes: no reset, or asynchronous reset to a constant. |
| `register-mapper` | on | Bit-blasts every `seq.firreg` to flip-flop cells found in the liberty by function: a plain flop, an asynchronous-clear flop, an asynchronous-preset flop (a library with fewer shapes is mapped through inverted-D and tied-pin fallbacks). With `multibit=` it packs bits into multibit cells first. |
| `data-gate-enables` | off, **experimental** | Rewrites named slices of a register whose next state is a plain hold mux so that the slice holds when loading would not change it (`zero`: load and slice both zero; `xor`: load equal to the slice), so bit-level `clock-gate-inference` puts the slice on a gate of its own whose enable follows the data. The rewrite is the identity on the register (proven combinationally by a SAT miter in `test_mapper_capabilities.py`); the gated result is a clock-gating equivalence, for which there is no sequential proof here. The compare ends at the gate's enable pin: check its setup slack. Option `slices=[module/]reg@lo-hi[@zero\|xor],...`; flow: `syn.clock_gating.data_gate` (legacy `CIRCT_FLOW_DATA_GATE`) or the scoped command `gate_register` (`doc/scripting.md`, section 7). Runs before `clock-gate-inference`. |
| `clock-gate-inference` | on | Turns groups of registers that share a hold enable into one gated clock with the hold mux removed, using the cheapest integrated clock-gating cell of the liberty (found by its `clock_gating_integrated_cell` attribute). Its options are listed below. |
| `narrow-dead-register-bits`, `propagate-register-constants`, `narrow-constant-register-bits`, `remove-dead-registers`, `dedup-registers`, `narrow-array-registers`, `lower-aggregate-registers`, `lower-firmem`, `lower-clock-inverters` | see `Passes.td` | Register clean-ups that run before mapping: dead and constant bits, aggregate and array registers, small memories as registers. `dedup-registers` is off. |

### Multibit banking (`register-mapper multibit=<spec.json>`)

The mapper decides banking on the register bits before ABC runs, while the
clock (a clock gate's output included), the reset and the flop class of every
bit are known, so a bank never straddles clock domains. Bits that share all
three are packed, in index order, into banks of the widths the spec declares
(for example 8, 4, 2); leftover bits and asynchronous-set bits stay single-bit
cells. The pass names no cell: the spec (families, cell-name templates with
`{n}` for the bank width and `{i}` for the bit, pin names, tie values for
unused pins) comes from the tech pack's `flow.multibit:` section
(`scripts/circt_flow/multibit.py` documents the schema; the flow writes the JSON
and passes it). `cross_register: true` pools leftover bits of different
registers. A class may name a single-bit cell (`single: {cell, data, out}`
in the spec): its leftover bits then become instances of that cell, with
the class's clock, reset and tie pins, instead of the liberty's plain flop
(the flow passes it only with `multibit: {single: true}`). Banks are created in the order of the first bit of each group, so
the result does not depend on how the compiler allocated its objects (see
"Determinism"). Off unless the flow config says `multibit:`.

### Clock-gate economy options (`clock-gate-inference`)

All off or neutral by default; none names a cell.

| option | effect |
| :--- | :--- |
| `min-group-width`, `min-gate-bits` | Do not gate a group of fewer register bits than this (the larger wins). |
| `merge-equivalent-enables` | One gate for groups whose enables are the same Boolean function of the same leaf signals (decided with a BDD, not by comparing operand lists), and no gate for a group whose enable is a tautology. |
| `gate-cost-bits`, `gate-idle-fraction`, `enable-op-cost-bits`, `hold-mux-bits` | A savings test in units of one flop bit's clock-pin power: a group is gated only when `bits * (idle-fraction + hold-mux-bits) >= gate-cost-bits + enable-op-cost-bits * (new enable cells)`. |
| `max-gate-depth` | A register whose clock already passes through this many gates (gates in the RTL included) is not gated again. |
| `bit-level-enables`, `select-dont-care`, `verify-with-mux` | Resolve enables per bit through concat/extract/mux chains and split a register into same-enable parts; rewrite the load mux selects under the part's own enable; rebuild gated groups as hold muxes on the plain clock (for a miter only). |
| `dont-touch=<names>` | Registers that keep their hold mux. |

Flow: `clock_gating:` in the flow config, with the numbers under
`flow.clock_gating:` in the pack (`scripts/circt_flow/clockgating.py`).
Environment: `CIRCT_FLOW_NO_CLOCK_GATING=1` turns the pass off,
`CIRCT_FLOW_CG_BITLEVEL=0` disables the bit-level resolution,
`CIRCT_FLOW_CG_SELECT_DC=1` enables the select rewrite.

## Arithmetic and shifters

All of these run on the word-level IR before ABC, build their result from
library cells (found by function, never by name) or from simpler word-level
operators, and are exact: the function is unchanged for every input. The one
exception, `default-dont-care` of `map-shift-select`, is called out in its
section.

| pass | default | flow switch | what it does and why |
| :--- | :--- | :--- | :--- |
| `set-adder-arch` | `BRENT-KUNG` in the flow | `CIRCT_FLOW_ADDER_ARCH` | Stamps the adder architecture on `comb.add` / `comb.icmp` so the lowering uses it. |
| `lower-carry-in-adds` | off | `CIRCT_FLOW_ADD_CARRY_IN=1` | `a + b + zext(c)` (a variable single-bit third operand) is lowered by CombToSynth as two serial adds; `{a,1} + {b,c}` is the same sum in one carry-propagate add, so one architecture choice applies to it. Option `min-width`. |
| `lower-barrel-shifts` | off | `CIRCT_FLOW_BARREL_SHIFT=1` | A variable shift becomes log2(width) stages of 2:1 muxes sharing their intermediate values. The default lowering builds a mux tree over every shifted copy (about width squared mux bits for the same depth). Options `min-width`, `min-amount-bits`. |
| `map-adder-chains` | off | `CIRCT_FLOW_ADDER_CELLS=<min-width>` | ABC cannot emit a two-output cell, so a library's full/half-adder cells are invisible to it. This builds a wide add as a ripple chain of those cells (found by function, output-inverted shapes included). Options `min-width`, `max-width` (short chains only), `increments` (also `x + 1` as a half-adder chain). Flow: `CIRCT_FLOW_ADDER_CELLS_MAX`, `CIRCT_FLOW_ADDER_CELLS_INCREMENTS=0`. A second instance for another width range: `syn.adders.cells2.*` (legacy `CIRCT_FLOW_ADDCH2_MIN`, `_MAX`), or the scoped command `map_adders(form="cells")` on a width window. |
| `map-prefix-adders` | off | `CIRCT_FLOW_PREFIX_ADDERS=<min-width>` | ABC's area recovery (`scorr`, `dc2`, `&deepsyn`) folds a parallel-prefix adder back into a near-ripple chain. This builds `a + b`, `a + b + 1` and `a + b + zext(c)` as a prefix network of library cells (Sklansky, Kogge-Stone, Han-Carlson or Brent-Kung, or each op's own `synth.test.arch` attribute) before ABC, and cells are boundaries ABC cannot undo. Options `min-width`, `max-width`, `arch`, `boundary=1` (each operand bit gets a NAND2 + NOR2 generate/propagate boundary and ABC maps everything behind it), `isolate-inputs=true` (the first prefix level is one NAND2 and one NOR2 per operand bit only). Flow: `CIRCT_FLOW_PREFIX_ARCH`, `CIRCT_FLOW_PREFIX_MAX_WIDTH`. A second instance for another width range: `syn.adders.prefix2.*` (legacy `CIRCT_FLOW_PREFIX2_ADDERS=<min-width>`, `_MAX_WIDTH`, `_ARCH`, `_ISOLATE=1`, `_BOUNDARY=1`), or the scoped command `map_adders(form="prefix")` on a width window. Each instance prints one report line, also when it built nothing. |
| `map-compressor-cells` | off | `CIRCT_FLOW_MUL_CELLS=1` | Lowers multiplier compressor trees to full/half-adder cell instances (Dadda), with an optional Brent-Kung final adder built from cells (`prefix-final-adder`, `CIRCT_FLOW_MUL_CELLS_PREFIX=1`). Options `min-width`, `merge-adds` (fold `add(x, zext(add(rows of one compress)))` into one compress when the inner sum provably cannot wrap), `flatten-adds` (flatten nested adds, zero-extensions, shifts and compress row pairs into one compress plus one add; width-safe), `flatten-gated`, `flatten-max-terms`. Flow: `CIRCT_FLOW_MUL_CELLS_MIN_WIDTH`, `_MERGE`, `_FLATTEN`, `_FLATTEN_GATED=0`. Tree shape and cell use (all off by default): `tree=dadda\|wallace\|array` (`_TREE`), `gate-compressors` (every full/half adder as plain logic for ABC; `_GATES=1`), `cell-stages=N` (adder cells in the first N reduction stages only; `_CELL_STAGES`), `nand-pp` (two-input AND partial products enter the tree complemented; the self-dual adder cells carry the complement, undone once at the result rows; `_NANDPP=1`), `map-final-adder` (each tree's final two-row add is tagged so `map-adder-chains` leaves it to ABC; `_MAPFINAL=1`) with `map-final-min-width=N` (only final adds of at least N bits; `_MAPFINAL_MINW`). |
| `limit-fanout` | off | pack `flow.fanout_buffer`, `CIRCT_FLOW_MAX_FANOUT` | Caps net fanout on the mapped netlist with a balanced buffer tree and touches only nets above the cap. A driver annotated `circt_abc.max_fanout = N` has its limit raised to N when N exceeds the global option; `map-shift-select` sets it on its digit selects when asked. |

### `map-shift-select`

A `case` over a shift amount reaches the mapper as a priority mux chain (or,
after `decode-mux-chains`, as a one-hot select) over shifted copies of one
word: `OR_k (sel_k ? word << s_k : 0)`. Left to ABC, such a select tends to come
back as a logarithmic shifter in which every stage switches on each change of
the data or of the amount. `map-shift-select` factors the select, under a
syntactically checked one-hot invariant, into a mixed-radix AND-OR network of
library cells that ABC treats as boundaries. The patterns it accepts, each
independent of the others:

* the select conditions are equality tests of one amount against distinct
  constants (`chains=true` also takes the mux chain before canonicalization);
* every arm is a shifted copy of one root word, the bits above and below it
  filled with zeros or with the root's top bit (sign fill), the same fill in
  every arm; arms with the same shift are merged;
* the last arm may stand for "none of the above" (the default of a `case`);
* the root may be a conditionally negated word (the `sub` form, or invert and
  add one), which then becomes a ripple inside the network;
* the amount may be any expression of leaf bits that can be evaluated
  (a slice, a slice plus a constant with wrap-around, ...): with
  `direct-decode` the digit selects are built from the raw bits.

Exact options (the result equals the source for every input, default arm
included):

| option | meaning |
| :--- | :--- |
| `enable-shift-select` | Turn the pass on (default off). `liberty=<file>` supplies the cells. |
| `min-arms`, `min-width` | Only selects with at least this many arms, over words at least this wide (defaults 12 and 16). |
| `radix` | Radix of the first AND-OR level (the two-level network, default 4). |
| `wide-cells`, `wide4-cells` | Use three-term (AO222) or four-term (AO2222) AND-OR cells where the library has them; polarity is chosen per operand set. |
| `chains` | Also take a priority mux chain of distinct constants of one amount; run before canonicalization (the flow's early hook does). |
| `recipes` | Mixed-radix recipes, `;`-separated, each `R1,R2,..@c1,c2,..,cK`: the digit radices of the amount (the last digit takes the rest) and the cell width of each level. The recipe of least cell area is built. |
| `direct-decode` | Build each digit select as a small cell network of the raw bits of the amount (found by evaluating the amount expression over its leaf bits), instead of an OR of per-arm comparators that the logic mapper turns into an adder plus a comparator network; the digit-boundary offset is chosen by area. |
| `select-max-fanout=N` | With `direct-decode`: annotate the driver of each digit select with `circt_abc.max_fanout = N`, so the fanout stage leaves a net of at most N loads unbuffered. |
| `select-dup-fanout=N` | With `direct-decode`: a select gate drives at most N loads; a further copy of the gate (same inputs) takes the rest instead of a buffer. |
| `l2-range-gate=N` | With `direct-decode` and a recipe of two or more digits: a node of the level below the last one is selected by "digit AND coarse range of the last digit", so it is quiet unless its range is the active one. The last digits that consume a node are split into ranges of N consecutive values, one node copy per range (N = 1: one copy per consumer and no last-level select; a large N keeps every node shared and gates only the nodes not used for every digit). |
| `l2-range-aligned` | With `l2-range-gate`: a node's range is the whole aligned block of N last-digit values that holds a consumer (range selects shared by the nodes of a block). |
| `l2-range-compose` | With `l2-range-gate`: a gated select is the AND of the digit select and the range select, both shared. |
| `window-form` | With `direct-decode` and a recipe of two or more digits: the window form (below) instead of the recipe network. Its options: `window-count`, `window-copy-cost`, `window-clip`, `window-per-node`, `window-cuts`, `window-general`, `window-types`, `window-types-max`, `window-direct`. |

**`default-dont-care` is the one option that is not exact.** With
`direct-decode`, the amounts that match no constant arm (the default arm) are
treated as don't cares, and the affine amount-to-shift relation of the constant
arms is continued through them, which makes the select networks smaller. The
result is equal to the source for every amount that matches a constant arm and
for no others. It is therefore valid **if and only if the shifter's result is
never used when the amount matches no constant arm**: it reaches no output and
no register that is later observed. If the design can ever use the default
arm's result, the netlist is functionally wrong for those amounts and no tool
here will say so. Establish the condition on the RTL (or by simulating the
whole design) before enabling the option, enable it for that design only, and
never from a generic default, pack or profile: `shift_select:` is a flow-config key only
(a pack's `flow:` section has no such key).
`mapper/test/map-shift-select/shift_affine_care.mlir` shows the use (the
output is masked by the care set, so the miter proves equality on the care set
only) and `test_shift_select_dont_care_negative_control` shows the difference
when the mask is absent.

Flow config:

```yaml
shift_select:
  early: true             # run first in the structural stage (default)
  min_arms: 12
  wide: 2                 # 0 none, 1 three-term cells, 2 four-term cells
  recipes: "2,2@2,2,3"    # string or list of strings
  direct_decode: true
  default_dont_care: false   # per-design assumption, see above
  select_max_fanout: 8
```

`shift_select: true` is `early: true` with the defaults. The environment
variables `CIRCT_FLOW_SHIFT_SELECT_EARLY=1`, `CIRCT_FLOW_SHIFT_SELECT=1` (the
older position, after `decode-mux-chains`) and
`CIRCT_FLOW_SHIFT_SELECT_{MIN_ARMS,RADIX,WIDE,RECIPES,MAXFO,DIRECT,DONTCARE}`
win over the config. A library without four-term cells (ASAP7 has the
three-term AO222/AOI222 but no four-term cell) runs with `wide: 1`.
`examples/shift-select/` runs the pass on ASAP7 and reports what it changes.
The flow keys of the options above are `select_dup_fanout`, `l2_range_gate`,
`l2_range_aligned`, `l2_range_compose`, `form: window` and `window_count`,
`window_copy_cost`, `window_clip`, `window_per_node`, `window_cuts`,
`window_general`, `window_types`, `window_types_max`, `window_direct`
(registry `syn.shifters.*`; a window option without `form: window`, or the
window form without `direct_decode` and the pass, is refused, not ignored).

**The window form** (`window-form`, exact) is a different generator, not a
gate on the recipe network. The levels below the one before the last stay
shared and ungated. Each node of the level before the last is built once per
window, a contiguous range of shift amounts holding an amount that uses the
node, and selected by "digit AND window" (built from the raw amount bits), so
the copy is 0 outside its window. The last level only tells the last digits
inside a copy's window apart: a pass-through when the window lies inside one
last digit, otherwise the cheapest of the exact digit select, an active-low
range select or the digit inside the window. The windows partition the amount
range, with cuts at multiples of the stride of the gated level, chosen by an
exhaustive search with the cost "per copy: `window-copy-cost` (percent of one
always-active load, default 150) + activity x (loads + 1); 0.5 per last-level
term; 1 per distinct window select". `window-count` fixes the number of
windows, `window-clip` restricts a copy's select to the amounts of its window
that use the node, `window-per-node` lets a node take one copy over the whole
range when its own cost prefers it, and `window-cuts` replaces the search by
explicit cuts. `window-general` generalises the windows: a window type is a
range of blocks (the amounts that share every digit from the gated level up)
with its own digit phase, so types may overlap; every (output, block) need is
covered by one node copy (greedy cover); `window-types` gives the types
explicitly (`phase:first-last;...`), otherwise a search combines up to
`window-types-max` of them, and `window-direct=C` lets an output take a
lower-level node directly under a block select at cost C per need. Selects
are built in the polarity of the data they gate, so the levels alternate
AOI / OAI. Malformed `window-cuts` / `window-types` are refused when the
options are read; cuts or types outside the shifter's range, or a
`window-count` no cut set reaches, fail the pass instead of leaving the
shifter to the logic mapper. Tests: `mapper/test/map-shift-select/shift_window.mlir`
and the SAT miters `test_shift_select_window_form_*` on generated shifters.

## Leakage-swap keys (`vt_swap:` and `pnr.post_layout:`)

`flop_cap: <ladder token>` (both sections, and `--flop-cap` of
`python3 -m circt_flow.postlayout`): the swap never moves a flop (a cell whose
name contains `DFF`, the swap script's test; a library whose flop names lack
`DFF` gets no cap) above that token of the pack's `vt_flavours` ladder; empty
is no cap. Options `syn.vt_swap.flop_cap` and `post.flop_cap` (legacy
`CIRCT_VT_FLOP_CAP`).

`pnr.post_layout.recover_opts` / `--recover-opts` (opt-in): a comma list of
`seq_first` (flops lead the recovery order), `seq_pair` (flops lead the pair
candidates), `comp=N` (up to N compensating cells per pair, their leakage
together below nine tenths of the saving) and `pass=N` (repeat the pair
recovery N times); validated when the config is read.

Block tail recovery (`pnr.post_layout.recover_block: "<regex>@<ps>"`, option
`post.recover.block`, `--recover-block`; the `recover_opts` token
`blk=<regex>@<ps>` is the same setting, both at once are refused): after the
pair recovery, the cells whose instance name matches the regular expression
(no comma, no white space) go down with their own slack floor of `<ps>`
picoseconds; the other cells keep the global margin. Off by default (the swap
script is then unchanged). The block floor may not exceed the global
`margin_ps` (refused: the recovery would clamp it); an expression that
matches no cell instance is an error, and the number of matched cells is
recorded (`eco_stat.blk_matched`). The expression is checked as a Tcl regular
expression (`tclsh` when available; otherwise `\b`, `(?P<` and lookbehind
are refused).

## Structural mapping, ABC and cleanup

| pass | what it does |
| :--- | :--- |
| `structural-mapper` | Matches small cones (a mux over an and/or/xor, wide AND-OR select fusions) against every cell of the liberty by truth table, cheapest area wins. The liberty reader recognises wide AND-OR cells (up to the 8-input pairing form) by function. |
| `outline-residual-logic`, `merge-residual-modules`, `prune-unused-residual-outputs`, `externalize-mapped-cells` | Move the logic that is not a cell instance into residual modules that ABC can take, merge them for a whole-design ABC call, drop outputs nobody reads, and declare real cells as externs. |
| `abc-bridge` | Lowers each residual to an AIG, runs an ABC script (default recipe below, or `abc-script-file`), re-imports the mapped Verilog. |
| `map-shell-logic`, `remove-identity-buffers` | Map the leftover word-level ops of the top shell to cells; remove buffer instances that are logical identities. |
| `narrow-sparse-shifts`, `narrow-extended-multiplies`, `narrow-mux-to-differing-bits`, `narrow-dead-output-bits`, `specialize-constant-module-inputs`, `specialize-mux-arms`, `select-chain-prefix-or`, `narrow-whole-value-reads`, `localize-dynamic-rmw`, `decode-mux-chains` | Word-level rewrites that expose structure CombToSynth cannot see (a shift whose amount is one of a few constants, a multiply of sign-extended operands, a mux that differs in a few bits, ...). Each is documented in `Passes.td`. |

Passes closed after measurement (`resub-mapped`, `fuse-half-adders`) are kept
so the negative result stays reproducible; nothing invokes them.

### The ABC recipe and determinism of `&deepsyn`

The default script is `strash; scorr; dc2; &get -n; &deepsyn -J <n> -I 4 -S 200;
&put; ...` followed by `&dch`, `&if`, `&resub` and a load-aware `&nf`. `&deepsyn
-J n -I k -S s` runs k chains, one after the other, with seeds s, s+1, ...; a
chain ends after n steps in a row without improvement. That bound depends on
the input and the seed only, so the result is reproducible on any machine and
at any load. The option `-T <seconds>` is a wall-clock limit per chain: on a
loaded machine a chain is cut short and the same input gives another netlist,
so **no recipe in this repository uses it**. The built-in recipe takes `-J` from
the option `deepsyn-no-improve` (default 3, flow variable
`CIRCT_FLOW_DEEPSYN_J`); `deepsyn-time-sec` (`CIRCT_FLOW_DEEPSYN_SEC`) adds a
`-T` and is kept only to reproduce old measurements. A larger `-J` buys area
for runtime, deterministically. The `-J`, `-I` and `-S` values in
`recipes/*.abc` and the example scripts are examples, not tuned settings.

`&deepsyn` seeds s..s+3 for four chains, so two runs whose seeds differ by less
than four share chains; use seeds at least four apart when comparing.

## Determinism

The same input must give the same netlist, byte for byte, on any machine:

* `map-shift-select` identifies each value by the order in which it first sees
  it (a temporary `shs.id` attribute on the defining operation, removed at the
  end of the pass), never by its address, so the pin order of the commutative
  inputs of a cell does not depend on the allocator.
* `register-mapper` visits multibit groups in the order of their first bit, not
  in the order of the clock/reset objects' addresses.
* `&deepsyn` has no wall-clock limit (above).

`scripts/circt_flow/tests/test_shift_select.py` runs the shifter pass many times
on one input and compares the outputs, and checks that no helper attribute is
left behind.
