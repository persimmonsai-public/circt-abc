# Scripting the flow in Python

The session API: a recipe is a Python script, the flow is a set of stages you can run one at a time, options
are typed attributes. No Tcl anywhere. The option reference is `doc/options.md` (generated:
`python3 -m circt_flow options --markdown`); the typed stub of the attribute database is `doc/db_types.pyi`
(generated: `python3 -m circt_flow options --stub`). Section 7 covers object queries and scoped
commands, section 8 the pass pipeline and hooks, section 9 a tech pack's own P&R options. `examples/scripting/` has runnable recipes on the
`examples/mvt-mini` design `core_lv`.

## 1. Three ways in

```sh
# a recipe script, with a session ready in `s` (flow.yaml and run directory on the command line)
python3 -m circt_flow run my_recipe.py flow.yaml out --set syn.fanout.max=12 -- any script args
# a script continuing a run directory a session wrote
python3 -m circt_flow run next_steps.py --resume out
# an interactive Python shell with the session loaded
python3 -m circt_flow shell flow.yaml out
# the command line, unchanged
python3 -m circt_flow flow.yaml out --vt-swap --sta --set syn.fanout.max=12
```

`run` and `shell` take `--set NAME=VALUE` (repeatable), `--settings SETTINGS_JSON` (replay a run's options),
`--strict` (refuse legacy `CIRCT_FLOW_*` / `CIRCT_VT_*` variables), `--resume OUT_DIR`, and the command
line's `--vt-swap`, `--sta`, `--abc-script-file PATH`. Run from `scripts/` or with `scripts/` on `PYTHONPATH`.

A recipe script sees these globals: `s` (also `session`; when a flow.yaml and run directory, or `--resume`,
were given), `Session`, `SessionError`, `options`, `argv` (the arguments after `--`). A script that defines
`recipe(s)` has it called with the session. A script may also make its own sessions
(`Session("flow.yaml", "out")`). Every run directory a script's session wrote gets a copy of the script
(`<out>/recipe.py`), next to `settings.json` and `session.json`; a second, different script writing into the
same directory (`--resume`) is kept as `recipe_2.py` (and so on), never over the first. An error (a refused option, a stage that
cannot run) ends the script with exit code 1 and the message.

The shell is a normal Python REPL (`code.InteractiveConsole`) with the same globals plus `db` (= `s.db`).
Tab completes attribute paths (`s.db.syn.vt_<TAB>`) and option names inside a string
(`s.set("syn.vt_<TAB>`); `help("syn.vt_swap")` prints the options of a namespace, `help("syn.fanout.max")`
one option; anything else is Python's own `help`. Commands can be piped in (`... shell f.yaml out < cmds.py`).

## 2. The attribute database `s.db`

Every option of the registry (`scripts/circt_flow/options.py`) is an attribute in its namespace:
`syn.` (synthesis), `cts.`, `hold.`, `post.`, `pnr.` (P&R), `flow.` (pack provenance, read only), `run.`.

```python
s.db.syn.adders.cells.min_width = 14         # validated now: name, type, range, choices, pattern
s.db.syn.adders.cells.min_width              # 14 (the resolved value)
s.db["syn.fanout.max"] = 12                  # the same by dotted name; s.set / s.get also work
s.db.source("syn.fanout.max")                # 'argument' (or default, techpack, recipe, env)
s.db.set_at("syn.fanout.max")                # 'script:/path/my_recipe.py:7'
s.db.reset("syn.fanout")                     # one option, or every option under a namespace
print(s.db.help("syn.fanout.max"))           # type, default, value, source, binds, stage that reads it
help(s.db.syn.vt_swap)                       # every option of the namespace with its help
print(s.report_changed())                    # every option not at its built-in default, with source
print(s.report_settings("syn.adders"))       # value, default, source, help
s.db.validate()                              # resolve everything now
```

What is refused, and when:

* at assignment: an unknown name (with the nearest valid names), a wrong type, a value out of range or not
  among the choices, a malformed pattern (`syn.clock_gating.data_gate`), a pack-only option;
* at `s.db.validate()`, or before the next stage runs (so before anything runs with it): combinations
  (`syn.shifters.window_count` without `syn.shifters.form = "window"`), and the keys of a flow.yaml section
  (type `object` in the reference: `syn.vt_swap.*`, `syn.multibit.*`, ...), which that section's own parser
  checks;
* an option read by a stage that has already run in this run directory (it could not take effect any more).
  `s.invalidate("elaborate")` re-opens that stage and every stage after it. Synthesis options count from
  `elaborate`; `syn.fanout_buffer.*` from `fanout`, `syn.vt_swap.*` from `vt_swap`, `syn.sta.*` from `sta`,
  `pnr.* cts.* hold.* post.* run.*` from `place_and_route`, except `pnr.clock_port` (the shorthand mode's clock:
  calibration and the SDCs, so `elaborate`); `syn.scoped` from `map_datapath` (`s.db.info(name).stage`; the rule is
  "the earliest stage that reads it", checked by a test against the configuration fields each option sets).

Precedence is the flow's (doc/options.md): a script assignment is an argument and wins over the flow.yaml,
the tech pack and (during the transition) a legacy environment variable.

A name that is both an option and a namespace (`syn.clock_gating`, `syn.multibit`, `post.recover`) reads as
the namespace; its own value is `s.db["syn.clock_gating"]` (or `s.db.syn.clock_gating.get()`); assigning
`s.db.syn.clock_gating = True` sets it.

## 3. Stages and results

The flow is seven stages, in this order (`s.stages`):

| stage | does | input | output |
|---|---|---|---|
| `elaborate` | front end, LLHD lowering, de-aggregation, directive checks, cell stubs | RTL / manifest | core IR (`s.artifacts["core"]`) |
| `map_datapath` | registers, clock gating, datapath operators, structural mapper, outlining | core IR | structural IR (`"struct"`) |
| `syn_map` | flatten, merge, ABC mapping, netlist export, OpenSTA form | structural IR | `"netlist"`, `"openroad_netlist"` |
| `fanout` | high-fanout buffering (`fanout_buffer:`) | netlist | netlist, `fanout.json` |
| `vt_swap` | threshold-flavour swap | netlist | netlist, `vt_swap.json` |
| `sta` | multi-corner multi-mode STA | netlist | `sta/summary.json` |
| `place_and_route` | P&R (`pnr:`) | netlist | `pnr/` |

```python
r = s.elaborate()          # each named call returns a StageResult
r.status                   # 'done' or 'skipped'
r.produced                 # {'core': '.../top_core_deagg.mlir', 'stubs': ...}
r.metrics                  # {'registers': ..., 'modules': ..., 'instances': ...}
r.settings                 # the options (not at default) the stage ran with
r.commands                 # the commands it ran, exactly
print(r)                   # one line
s.map_datapath()
r = s.syn_map()
r.metrics                  # {'cells': ..., 'area': ..., 'top_cells': {...}, 'cell_types': ...}
s.vt_swap(); s.sta()       # metrics: wns_ns, tns_ns, hold_wns_ns, vt_flavours, vt_up, vt_down / sta_views
print(s.report_stages())   # every stage: done / skipped / not run, with its metrics
s.report_qor()             # the latest cell count, area (liberty units), timing: a dict
s.write_netlist("final.v")
```

`s.vt_swap()` and `s.sta()` called by name run the stage, as `--vt-swap` / `--sta` do.
`s.run()` runs the whole flow exactly as the command line does (it is the same function, `pipeline.run`;
the stages the config does not want are skipped) and returns a `FlowResult` (`.stages`, `.metrics`,
`.netlist`, `["syn_map"]`). `s.run(start="syn_map", stop="sta")` runs a range; on a resumed session
`s.run()` continues with the first stage not done. A stage needs its input stage done (`map_datapath` needs
`elaborate`; the netlist stages need `syn_map`); running a stage again re-opens the stages after it. A
design with power domains runs only as a whole (`s.run()`).

Load arguments (the command line's flags) are keywords of `Session(...)` or of `s.configure(...)`:
`abc_script_file`, `run_vt_swap`, `run_sta`, `partitions`, `circt_abc_opt`, ... Like an option, a load argument
belongs to the earliest stage that reads it (`run_vt_swap`: vt_swap, `run_sta`: sta, the others: elaborate):
`s.configure` refuses it once that stage has run or is running, and otherwise applies it from the next stage on.

## 4. Single passes

```python
s.elaborate()
s.current_ir                                          # the core IR (after map_datapath: the structural IR)
r = s.run_pass("canonicalize")                        # -> <out>/passes/01_canonicalize.mlir
s.run_pass("map-prefix-adders", enable_prefix_adders=True, arch="KOGGE-STONE", replace=True)
s.map_datapath()                                      # reads the IR that pass rewrote
```

The pass name and its option names (Python spelling, `_` for `-`) are checked against the tool's own pass
list (`circt-abc-opt --help`); a misspelt one is refused with the nearest names. `replace=True` writes the
result over the input (for the current IR: the file the next stage reads). Every pass run is recorded in
`settings.json`'s pipeline. A `replace=True` run makes the record not reproducible from its settings alone, as
hooks do: `settings.json` gets `"run_pass_replaced": {"reproducible_from_settings": false, "passes": [...]}`.

## 5. Records, resuming, replaying

Every stage rewrites `<out>/settings.json` (value and source of every option, the tool commits, the recipe
file hashes, every command so far; for a session also `set_at`) and `<out>/session.json` (the stages done
with their results, the options, the load arguments).

```python
s = Session.resume("out")             # same flow.yaml, options, load arguments; done stages marked done
s.run()                               # continues with the first stage not done
s2 = Session.from_settings("flow.yaml", "out2", "out/settings.json")   # a new run with the same options
s.write_settings("x.json")            # a record of the present settings, anywhere
```

`Session.resume` compares what the stages done were made from (session.json `"inputs"`: the recipe files'
sha256 — flow.yaml, tech pack, manifest, ABC script, mode SDCs —, the legacy `CIRCT_FLOW_*` / `CIRCT_VT_*`
variables, the circt-abc commit) with the present ones and refuses a change (`allow_changed_inputs=True`, or
`--allow-changed-inputs` with `--resume`: continued with a warning); a file given again under another path by a
load argument is not compared. `Session.from_settings` of a record with hooks or `run_pass_replaced` warns that
those are not replayed (the new session has the options, scoped settings and pipeline edits).

`python3 -m circt_flow options --diff A/settings.json B/settings.json` lists what differs between two runs.

## 6. Worked examples

`examples/scripting/core_lv_recipe.py` (run from `scripts/`, after `examples/mvt-mini/run.sh` or
`python3 -I ../examples/mvt-mini/lib/make_libs.py ../examples/mvt-mini/lib` has written the libraries):

```python
db = s.db
db.syn.adders.cells.min_width = 14          # adds of 14 bits and more: a ripple of adder cells
db.syn.fanout.max = 12                      # balanced buffer trees above 12 loads
print(s.elaborate())
print(s.map_datapath())
r = s.syn_map()
print(r)
print(s.report_changed())
print(s.report_stages())
q = s.report_qor()
print({k: q.get(k) for k in ("cells", "area")})
```

```sh
python3 -m circt_flow run ../examples/scripting/core_lv_recipe.py \
    ../examples/mvt-mini/designs/core_lv/flow.yaml /tmp/core_lv_recipe --strict
```

Trying a setting between stages in one run directory:

```python
s = Session("flow.yaml", "out")
s.elaborate(); s.map_datapath(); s.syn_map()
for order in ("inst", "leak"):                    # a vt_swap option: open until vt_swap runs
    s.invalidate("vt_swap")
    s.db.syn.vt_swap.recover_order = order
    r = s.vt_swap()
    print(order, r.metrics["wns_ns"], r.metrics["vt_flavours"])
```

Every iteration starts from the same netlist: `fanout` and `vt_swap` rewrite `<top>_openroad.v` in place, so the
session keeps each one's input (`<out>/session/<stage>_input_openroad.v`) and puts it back when the stage runs
again; running the stage before it (`syn_map`, or `fanout` for `vt_swap`) replaces the kept input.

A loop over settings, each in its own run directory, collecting results:

```python
rows = []
for w in (6, 14, 24):
    t = Session("flow.yaml", f"out_w{w}")
    t.db.syn.adders.cells.min_width = w
    res = t.run()
    rows.append((w, res.metrics["cells"], res.metrics.get("area")))
```

## 7. Object queries and scoped commands

Code: `scripts/circt_flow/query.py` (queries, scoped commands), `scripts/circt_flow/design/objects.py` (what the
queries read), `scripts/circt_flow/scoping.py` (records, legacy forms, application checks); tests
`tests/test_scoping.py`, `tests/test_session.py`. A recipe can select design objects (registers, register bit ranges, adders,
multipliers, shifters, clock gates, mapped cells) and apply a command to the
selection instead of setting a global width window or a string grammar.

### What can be queried, and when

| query | objects | available after | read from |
|---|---|---|---|
| `s.flops(pattern, **attrs)` | `Register`: `name`, `module`, `width`, `kind` (firreg / compreg), `clock`, `clock_gate` (RTL gate), `enable`, `hold_mux`, `reset`, `reset_signal`, `reset_value`, `instances`; with the struct IR also `cells`, `multibit`, `gates` | elaborate | core IR (`<top>_core_deagg.mlir`); struct IR for the cells |
| `s.adders`, `s.multipliers`, `s.shifters` | `Operator`: `kind`, `op` (add sub mul shl shru shrs), `width`, `module`, `index`, `operands`, `const_operands`, `instances`, `id` | elaborate | core IR |
| `s.icgs(pattern, **attrs)` | `ClockGate`: `name`, `module`, `cell`, `origin` (inferred / rtl), `enable`, `clock`, `fanout`, `registers`, `register_cells`, `bits`, `instances` | map_datapath | struct IR (`<top>_struct.mlir`) |
| `s.design.cells(pattern, **attrs)` | `MappedCell`: `name`, `cell`, `module` | syn_map | `<top>_openroad.v` |
| `s.modules(pattern)` | `name`, `instances` | elaborate | core IR |

A query before its stage raises `StageNotRun` / `SessionError` ("... needs
stage elaborate run first"); in a session the stage counts as run when it ran
in the session (or the run directory it resumed) or its output file is there
(a run directory the command line wrote). A scoped command is refused once
`map_datapath` has run (`s.invalidate("map_datapath")` re-opens it).
`s.design` is the query object itself (`query.design_of(s)`), with
`operators(kind)`, `cells(...)` and the rest. Objects are per module **definition**: a register of a module
instantiated twice is one object with two `instances` paths, and a command on
it acts on both (the passes rewrite definitions).

Collections are ordered by a stable key (module, then name or index), are
immutable, compose with `|`, `&`, `-` (same kind), iterate like lists, and
filter with `.filter(pattern, **attrs)` and `.where(predicate)`. A pattern is
an fnmatch pattern tried against the name, `module/name` and every
hierarchical instance path: `*` and `?` are wildcards and `[...]` is a
CHARACTER CLASS (`"r[12]_q"` matches `r1_q` and `r2_q`, not a name with
brackets; `[[]` matches a literal `[`). An attribute filter is a value (a
string is a pattern; for a tuple attribute, any element), an inclusive
`(lo, hi)` TUPLE range for a number (`None` = no bound), a list or set of
such values meaning any of them (`width=[8, 16, (24, 32)]`; for adders the
windows are their union; an empty list is refused), or a predicate. An empty collection is falsy
and its repr says `<empty ... collection: query>`; `.require()` raises on it.

Register bit ranges: `flops("sum_q").bits(3, 10)` is bits 3..10 of each
selected register; `.split(4)` cuts every selected range (or whole register)
into consecutive 4-bit slices.

Adder width windows: a collection made by `adders(width=...)` and combined
only by width filters and set operations remembers that it is "every adder
whose width is in these windows" (`.windows`); that is what the passes can be
given. Adder ids (`<module>/adder<k>`) number the adders of a module in text
order: stable for a given core IR, not across RTL edits.

### Scoped commands

| command | selection | arguments | pass |
|---|---|---|---|
| `s.gate_register(sel, detect="zero")` (EXPERIMENTAL) | registers or register bit ranges whose `next` is a plain hold mux | `detect`: `zero` (hold when load and slice are both zero) or `xor` (hold when the load equals the slice) | `data-gate-enables` (before clock-gate-inference); no sequential equivalence proof |
| `s.map_adders(sel, form="prefix", ...)` | adders | `form`: `prefix` (`arch`, `isolate_inputs`, `boundary`) or `cells` (`increments`) | `map-prefix-adders` / `map-adder-chains` |

Each call records one `ScopedSetting(kind, targets, command, values, set_at)`
in the option `syn.scoped`. The targets are what the pass boundary receives:
`[module/]reg@lo-hi` (the module is added when the register name exists in
more than one module, since the pass matches a bare name in every module) and
`width:lo-hi` / `width:lo-`. `map_adders` on a selection that is not a set of
width windows (a predicate or another attribute filter drops the windows) is
accepted only when it holds every adder of each width it touches (then one
window per run of consecutive selected widths) (the passes select by width only); otherwise it is refused and says
which adders stand in the way. An empty selection is refused unless
`allow_empty=True` (then nothing is recorded).

Applied or refused, per target:

1. When the command is given: types and choices of its arguments, the form's
   own arguments only, a non-empty selection, registers with a hold mux.
2. When the config is loaded (`syn.scoped` from any source): the record is
   validated (unknown command, bad target, bad value: OptionError).
3. Before the structural stage, against the core IR: a target that matches no
   register / no adder, a slice outside its register, a register without a
   hold mux, overlapping slices of one register: refused (the run stops).
   Before that, against the windows alone: one adder, one pass instance. In
   emission order (the global `syn.adders.cells.*` instance, cells targets,
   the global `syn.adders.prefix.*` instance, prefix targets) a target whose
   width window meets an earlier window is refused (the earlier instance would
   build those adders and the later one be recorded for adders it never saw).
4. After the structural stage, against the commands that actually ran (a
   pipeline edit included) and the passes' own report: a target whose pass
   did not run (removed or replaced by an edit), a data-gate pass that ran
   after clock-gate-inference, a slice the pass skipped, a window whose own
   pass instance built nothing (for example because a global
   `syn.adders.prefix.*` / `syn.adders.cells.*` instance before it took its
   adders): refused. map-prefix-adders and map-adder-chains print one report
   line per enabled run, also at 0, so the k-th line is the k-th instance of
   the command; a report with a different number of lines (an older binary)
   is not attributable and refuses the targets of that pass.

`settings.json` keeps the scoped settings as the value of `syn.scoped` (so
`Session.from_settings` / `--settings` replay them) and, under
`scoped_applied`, each target's origin, members in the core IR, status
(`applied`, `refused`, `checked`: core IR only, `unchecked`) and detail. A run
without scoped settings has no `scoped_applied` key and the same pass command
as before.

### Worked examples

`examples/scripting/core_lv_scoped.py` on `core_lv` (a 16-bit LFSR feeding a running sum, both behind an
enable):

```python
s.elaborate()                                              # the queries read the core IR

# data-following clock gating of two slices of the running sum (bits 1-4 and 5-12)
for lo, hi in ((1, 4), (5, 12)):
    s.gate_register(s.flops("sum_q").bits(lo, hi), detect="zero")

# a ripple of adder cells on the adds of 14 to 70 bits
s.map_adders(s.adders(width=(14, 70)), form="cells")

s.run(start="map_datapath", stop="syn_map")
```

The same as settings, without Python (flow.yaml `options:`, or `--set
syn.scoped=<yaml>` on the command line):

```yaml
options:
  syn.scoped:
    - {command: gate_register, targets: [sum_q@1-4, sum_q@5-12], values: {detect: zero}}
    - {command: map_adders, targets: ["width:14-70"], values: {form: cells}}
```

`examples/scripting/core_lv_legacy.py` is the same recipe with the legacy options
(`syn.clock_gating.data_gate`, `syn.adders.cells2.*`): the same pass command and netlist, with deprecation
warnings, refused under `--strict`.

Exploring before deciding:

```python
s.adders().filter(width=(12, None))          # wide adds, by module and index
[(a.id, a.width, a.op) for a in s.adders(op="sub")]
wide = s.flops(width=(12, None)) - s.flops("lfsr*")
s.icgs(registers="sum_q")                    # after map_datapath: the gates of sum_q's parts
[(g.name, g.fanout, g.bits) for g in s.icgs(fanout=(1, 2))]
s.flops("sum_q").one().multibit              # (cell instance, bits) of its multi-bit cells
```

Errors you can expect:

```python
s.map_adders(s.adders(op="sub"))
# QueryError: map_adders: adders(op='sub') cannot be given to the pass: it selects by width only
#             and width 32 also has <module>/adder1, ... (select whole widths, ...)
s.gate_register(s.flops("nothing*"))
# QueryError: flops('nothing*'): the selection is empty (it matched nothing)
```

### Legacy forms

| legacy form (option / variable) | translated to | status |
|---|---|---|
| `syn.clock_gating.data_gate` / `CIRCT_FLOW_DATA_GATE` = `[module/]reg@lo-hi[@zero\|xor],...` | `gate_register` entries (one per mode), targets as written | deprecated |
| `syn.adders.prefix2.*` / `CIRCT_FLOW_PREFIX2_*` | `map_adders(form=prefix)` on `width:min-max` | deprecated |
| `syn.adders.cells2.*` / `CIRCT_FLOW_ADDCH2_MIN/_MAX` | `map_adders(form=cells)` on `width:min-max` | deprecated |
| `syn.adders.prefix.*`, `syn.adders.cells.*` (first instances) | kept as global options (not deprecated) | unchanged |

Same rule as the legacy environment variables: accepted with a warning that
names the scoped form and the removal release, refused under `--strict` now
and in every mode from release `options.LEGACY_ENV_REMOVED_IN` (13) on. The
translated entries are printed (`[circt_flow] legacy ... -> scoped ...`),
checked like scoped ones and emitted at the
same place in the same text, so a legacy run builds the same pass command as
before. Without `--strict` a legacy target that does not apply (matches
nothing, a slice the pass skips, a window whose instance built nothing)
is passed on as the legacy variable always was: the pass text is unchanged, a
warning names the reason and `scoped_applied` records it "unchecked"; under
`--strict` it is refused like a scoped one. The same holds for a legacy value
the scoped form refuses: a maximum below its minimum
(`CIRCT_FLOW_PREFIX2_ADDERS=56` with `_MAX_WIDTH=20`, an empty window) or a slice
written high-low (`CIRCT_FLOW_DATA_GATE=sum_q@3-0`): the legacy pass text, a
warning, "unchecked"; refused under `--strict` and as a scoped form.

Emission order in the structural stage: legacy data-gate text, then
`gate_register` targets (one `data-gate-enables` instance); after
`syn.adders.cells.*`'s instance the cells-form targets (legacy first, then
`syn.scoped` order); after `syn.adders.prefix.*`'s instance the prefix-form
targets (same order). One target is one pass instance.

### Limits

- The passes select adders by width, so scoped adder selections are width
  windows; selection by module, name or connected register needs an `on=`
  option in the passes (not built).
- The pre-check reads the core IR, before the structural stage's own
  rewrites (carry-in folding, compressor trees); the post-check reads the
  pass report (one line per instance, see above).
- In a power-domain flow every domain sub-run checks the targets against its
  own core IR: a register that lives in one domain only is refused in the
  others.
- Multiplier and shifter queries exist; no scoped command uses them yet.

## 8. The pass pipeline and hooks

Code: `scripts/circt_flow/pipeline_edit.py` (plan, edits, application), `scripts/circt_flow/hooks.py` (hook record,
split at a pass); tests `tests/test_pipeline_edit.py`, `tests/test_pipeline_session.py`.

### The pipeline as data

`s.pipeline` holds, per stage, the passes the flow plans to run, with their options. The plan is obtained without
running a tool: the stage's own command builders in `stages.py` run against a recording stand-in for the tool
(`stages.RUNNER`) on a scratch run directory, and the paths are mapped back to the run directory. It is therefore
exactly the command lines the stage would run for the present settings (with no edits, the golden command lines
of `tests/data/golden_cmdlines.json`; a test checks that for every golden case).

```python
print(s.report_passes())             # = print(s.pipeline): every stage, its commands, their passes
s.pipeline.passes("map_datapath")    # [{ref, name, options, command, step, origin}, ...]
s.pipeline.commands("syn_map")       # the command lines as they will run (edits applied)
```

Stages with pass lists: `elaborate` (localize-dynamic-rmw; the de-aggregation command), `map_datapath` (the
register / clock-gating / datapath / structural-mapper command), `syn_map` (flatten, outline, prune, merge + ABC
bridge, netlist export). `fanout`, `vt_swap`, `sta`, `place_and_route` run Python or OpenROAD steps, not pass lists:
an edit or pass hook there is refused. In a partitioned run (`--partitions N`) the per-group ABC commands are not
editable (the commands around them are).

#### References to a pass

A pass is referred to by name, `"register-mapper"`, when it occurs once in the stage, or as `"name#k"`, its k-th
occurrence in the stage (1-based, counted over the stage's commands in order): `"map-prefix-adders#2"`,
`"canonicalize#3"`. A bare name that occurs more than once is refused as ambiguous (the message lists the valid
`#k`). `report_passes()` prints each pass with its reference.

### Edits

```python
p = s.pipeline
p.insert("map_datapath", "canonicalize", after="map-prefix-adders#2")
p.insert("map_datapath", "set-adder-arch", before="register-mapper", arch="SKLANSKY")   # options as keywords
p.insert("syn_map", "cse")                                     # no anchor: after the stage's last pass
p.remove("syn_map", "remove-identity-buffers")
p.replace("map_datapath", "set-adder-arch", "set-adder-arch", arch="KOGGE-STONE")
p.move("map_datapath", "narrow-whole-value-reads", before="structural-mapper")
p.edits          # the list, in order (each a dict: op, stage, pass / ref, after / before)
p.clear()        # drop every edit: the default pipeline
```

A pass is given as `"name"`, `"--name"` or `"name=opt=value opt2=value"` (the tool's own syntax); keyword options
use Python spelling (`min_width=56` is `min-width=56`; `True` is `true`). Edits apply in order, each to the
pipeline as the edits before it left it (so after an insert the `#k` of later occurrences shift). A move resolves
both references before the pass is taken out. A pass may move between commands of the same stage when the
destination tool registers it.

Every edit is applied or refused, at once, when it is made: a refused edit raises `PipelineError` (an `OptionError`)
and is not recorded. Refused:

- an unknown stage, a stage without a pass list, an unknown reference, an ambiguous bare name, `#k` out of range;
- a pass the command's tool does not register, an option the pass does not have (checked against the tool's own
  `--help` listing, read once per binary; when the binary cannot be run the names are not checked);
- removing `abc-bridge` or `export-verilog`, replacing either by another pass, or moving either to another command
  (they define what their command produces: the mapped design, the netlist);
- `after=` and `before=` together; a move without an anchor or onto itself;
- an edit (or pass hook) on a stage that has already run in the run directory (`s.invalidate(stage)` re-opens it);
- any edit on a design with power domains (`power.run` runs its own sub-flows).

Before the first tool of each stage runs, every stage's edits are checked again on the configuration the run uses
(a setting changed after an edit can remove its anchor: that is an error then, before any tool runs). While the stage
runs, each command it builds must be the planned one, otherwise the run stops before that command.

With no edits and no hooks nothing of this is consulted (`stages.RUNNER` stays `None`): same command lines, same
netlists.

#### The record

The settings record (`settings.json`) gets `"pipeline_edits": [...]` when there are edits (not otherwise), so a run
is reproduced from its record: `Session.from_settings(...)` / `Session.resume(...)` replay them
(`s.pipeline.load_edits(list)`, all or nothing). `"pipeline"` lists the commands as executed (edited).

### Hooks

```python
s.hooks.pre("syn_map", lambda s: print("about to map"))              # fn(session)
s.hooks.pre("elaborate", lambda s: s.db.syn.fanout.__setattr__("max", 12))   # an option set in a hook
s.hooks.post("map_datapath", lambda s, r: print(r.metrics))          # fn(session, StageResult)

def look(s, ir):                                                     # fn(session, ir_path: Path)
    print(ir, sum(1 for l in open(ir) if "seq.firreg" in l))
s.hooks.post_pass("map_datapath", "register-mapper", look)
s.hooks.pre_pass("syn_map", "abc-bridge", look)
```

What a hook writes is applied or refused, never ignored. The configuration of a stage is resolved after its pre
hooks (in `s.run()` too), so an option set in a stage's pre hook acts on that stage and the later ones. While a
stage runs (a pass hook is inside it; a post hook comes after it), a write that concerns the running stage or an
earlier one is refused with an error naming the stage: an `s.db` option, a scoped command (`s.gate_register`,
`s.map_adders`: map_datapath), `s.set_pack_option` (place_and_route), a pipeline edit of that stage, an
`s.configure` load argument. A write for a later stage is accepted and takes effect when that stage starts. An
option of a stage that has already run is refused as everywhere (`s.invalidate(stage)` re-opens it). A design with power domains runs as a whole: stage hooks and pipeline edits
are refused there (`s.run()` says so before anything runs).

A pass hook splits the stage's tool invocation at that pass: the passes before the boundary run into an
intermediate file `<out>/pipeline/NN_<stage>_cmdC_after_<pass>.mlir` (or `before_<pass>` for the first pass), the
hook is called with that path, and the rest of the command runs from it (a hook may read or rewrite the file). Several
hooks on one command make several pieces; the command's stdout (the netlist of `export-verilog`) goes to the last
piece that runs passes. A pre-hook on a command's first pass sees a copy of the command's input. Each hook call is
listed in the record's `"pipeline"` as `(pre_pass)` / `(post_pass)` with the hook's name, file and line. The split
itself may change the output in principle (the IR is printed and parsed once more); a run without hooks is not split.

Hooks are code, so the record names them and cannot replay them: with any hook registered, `settings.json` has

```json
"hooks": {"reproducible_from_settings": false, "note": "...",
          "registered": [{"kind": "post_pass", "stage": "map_datapath", "pass": "register-mapper",
                          "name": "look", "file": "/path/recipe.py", "line": 12}]}
```

`s.hooks.clear()` drops all hooks; `s.pipeline.clear_hooks()` only the pass hooks.

### Without a session

`Pipeline(cfg=FlowConfig)` gives the same plan, edits and text for a loaded configuration;
`with p.applied(stage, cfg): <stage functions>` runs the stage functions with the edits applied (what the session
does around each stage).

## 9. The tech pack's own P&R options

A pack whose own P&R command declares its options in a registry (`pnr.settings_registry`
of its techpack.yaml; doc/pack-options.md) takes them as a settings file:

```python
s.set_pack_option("report.label", "trial-3")    # validated against the pack's registry now
# s.unset_pack_option("report.label") takes it back: the profile's or the pack's default applies again
s.pack_options                                  # what place_and_route hands over
s.pack_option_sources                           # where each value comes from
s.place_and_route()                             # the command gets --settings <out>/pnr/pack_settings.json
p = s.write_pack_settings()                     # the same content, to run the pack's command by hand
```

`python3 -m circt_flow options --pack <pack>` lists the pack's options. What is handed over
is the pack profile's `pnr.settings_defaults`, the session's values over them (options set
with `s.set_pack_option`, and options the pack shares with the flow set through `s.db`), and
the registry's `recipe_dir_option` when an option read from the pack's recipe directory needs
it and it is not set; `s.pack_options`, `s.pack_option_sources` and `s.write_pack_settings()`
show exactly that (doc/pack-options.md). Without pack options nothing changes. The session's
own values are recorded (`settings.json` "pack_options", `session.json`) and replayed by
`Session.resume` / `Session.from_settings`. The closure ladder hands the same settings to the
placement and every rung, and compares their content when it reuses a placement. A pack
without its own command (the ORFS stage) has no pack options; options it shares with the flow,
set through `s.db`, act through the flow's `pnr:` section.

On the command line of `run` and `shell`, `--set NAME=VALUE` takes the pack's options too: a name
that is not one of circt-abc's options goes to `s.set_pack_option` (the same checks). A session
keeps its pack options across runs of one run directory (`--resume`), so a script that sets an
option in one mode only takes it back in the others with `s.unset_pack_option`. As with every
option, one of a stage that has already run is refused until the stage is re-opened
(`s.invalidate("place_and_route")`).

## 10. Not here yet

* Scoped commands for multipliers, shifters and clock gates (queries only); adder scopes are width windows.
