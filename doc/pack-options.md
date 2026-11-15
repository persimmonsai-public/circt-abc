# A tech pack's own P&R options

A tech pack that runs its own P&R command (`pnr: {command: [...]}` in its
`techpack.yaml`, doc/techpacks.md) can declare the options of that command in a
JSON registry and name it in the same section:

```yaml
pnr:
  command: ["./pnr/run.py"]
  settings_registry: pnr/options.json     # relative to the pack
```

circt-abc never imports pack code: `scripts/circt_flow/pack_options.py` reads the
registry and maps every entry onto the flow's `options.Option`, so a pack option
is validated exactly as a flow option is (type, range, choices, pattern).
`examples/techpack-template` has a complete registry (`pnr/options.json`): common
P&R options of an OpenROAD-style command, under the flow's own names where the
two coincide, and four `report.*` options that the template's stand-in command
honours.

## The registry file

Strict JSON, for example:

```json
{
 "format": 1,
 "legacy_env_removed_in": 13,
 "namespaces": ["mytool"],
 "recipe_dir": "pnr/scripts",
 "recipe_dir_option": "mytool.scripts_dir",
 "recipe_dir_consumers": ["script"],
 "options": [
  {"name": "mytool.effort", "type": "choice", "choices": ["low", "high"],
   "default": "low", "help": "placement effort", "var": "MYTOOL_EFFORT"},
  {"name": "mytool.extra", "type": "str", "default": null, "consumers": ["script"],
   "help": "a script of the scripts directory to source after placement"},
  {"name": "mytool.scripts_dir", "type": "path", "default": null,
   "help": "where the command reads its scripts"}
 ]
}
```

| key | meaning |
| :--- | :--- |
| `format` | 1 |
| `legacy_env_removed_in` | must equal the flow's `options.LEGACY_ENV_REMOVED_IN` (drift rule D4) |
| `namespaces` | the first name parts the pack's options may use (D5) |
| `options[]` | `name`, `type` (`bool int float str path choice`), `default` (null: unset), `help`, and optionally `unit`, `range` `[lo, hi]` (null: open), `choices`, `pattern` (a full-match regular expression), `empty_ok`, `var` (the variable the command reads; default the name), `legacy_env`, `shared`, `stage` (the P&R stage it acts in, shown by `options --pack`), `consumers` (tags naming what reads it; see `recipe_dir_consumers`), `group` (a free-text label the listing prints and `--group` selects) |
| `requires` | optional: `{option: [other, value]}`, the command uses `option` only when `other` has that value; listed by `options --pack`, both names checked by `check`, enforced by the command (the other value may come from a source the flow does not see) |
| `recipe_dir`, `recipe_dir_option`, `recipe_dir_consumers` | optional: a directory of the pack the command reads files from, the option that carries it, and the consumer tags of the options that need it (below) |

Other keys, top-level or per option, are left to the pack's own tools: the flow
reads only the keys above.

An option marked `shared` is one of the flow's own options (the same dotted name,
`doc/options.md`): its value comes from the flow and is handed to the pack
command too. A pack option that does the same as a flow option under another
name (e.g. `cts.gate_sizing` and the flow config's `pnr.cts_size_gates`) is not
merged with it: the command receives both, the flow's value as the
pack-command environment variable and the pack option in the settings file, and
the command decides which wins (the template's command reads only the file).

## Commands

* `python3 -m circt_flow options --pack <pack> [PATTERN] [--group G] [--markdown|--json]`
  lists the flow's options and then the pack's (`<pack>`: the pack directory or
  its techpack `.yaml`; `--group G`: only the pack options labelled G, also in
  the `--markdown` table; a group no option carries is an error that lists the
  pack's groups; `--group` without `--pack` is refused).
* `pack_options.load(pack)` -> `{name: Option}`; `pack_options.check(pack)` -> the
  problems the drift rules find:
  * D1: a shared option exists among the flow's options, with the same type,
    choices, range and default where the flow types it itself;
  * D2: an option that is not marked shared does not use a flow option's name;
  * D3: its legacy variable is not one of the flow's (unless it is the shared
    option's own);
  * D4: `legacy_env_removed_in` equals the flow's;
  * D5: every name is in a listed namespace, and an option that is not shared is
    not in the flow's own `syn.` namespace;

  and a `requires` entry that is not `[other, value]`, or names an option the
  registry does not have.
* `pack_options.write_settings(pack, {name: value}, path)` validates every value
  (unknown names and bad values are refused; nothing is written then) and writes
  `{"options": {...}}`, the settings file the command's `--settings FILE` reads.
* `pack_options.handover(pack, pack_pnr, given)` -> `({name: value}, {name:
  source})`: what the P&R stage hands over (below).

## In a session

```python
s.set_pack_option("report.label", "trial-3")   # validated against the pack's registry now
s.db.pnr.spef_restore_pincap = False            # a shared option: set as a flow option
s.pack_options                                  # what place_and_route hands over
s.pack_option_sources                           # where each of those values comes from
s.place_and_route()                             # the command gets --settings <out>/pnr/pack_settings.json
s.write_pack_settings("my_settings.json")       # the same content, to run the command by hand
```

What the pack's command is handed is built in one place
(`pack_options.handover`), for a session and for a run without one alike:

1. the pack profile's own values, `pnr.settings_defaults` of its techpack.yaml
   (a relative `path` value is relative to the pack):

   ```yaml
   pnr:
     command: ["./pnr/run.py"]
     settings_registry: pnr/options.json
     settings_defaults: {mytool.effort: high}    # checked against the registry when the pack loads
   ```

2. over them, a session's values: options set with `s.set_pack_option`, and
   options the pack shares with the flow set through `s.db` (a relative `path`
   value is relative to the current directory);
3. the recipe directory: when an option whose `consumers` include one of
   `recipe_dir_consumers` (default `recipe`) is handed over and
   `recipe_dir_option` is not (nor is its legacy variable set in the
   environment), `recipe_dir_option` gets the pack's `recipe_dir`; a registry
   that names no such directory is refused with the option to set.

Every value is validated against the registry. `s.pack_options` and
`s.write_pack_settings()` hold exactly those values, and
`s.pack_option_sources` says for each whether it comes from
`pnr.settings_defaults`, `set_pack_option`, `s.db` or the registry's
`recipe_dir`. When there is anything to hand over, the P&R stage writes it to
`pack_settings.json` in the command's output directory (`<out>/pnr/`, or each
job's directory under the closure ladder) and puts `--settings <that file>` in
front of the usual arguments; with nothing to hand over there is no file and no
argument. A session's own values are recorded (`settings.json` "pack_options",
`session.json`) and replayed by `Session.resume` / `Session.from_settings`; the
profile's values come from the pack again.

### With the closure ladder

A pack command laddered with `pnr.ladder` (doc/ladder.md) gets the same
settings on the placement run and on every rung, the profile's
`settings_defaults` and a session's values alike. Each job writes its own
`pack_settings.json`; the ladder's manifest takes the file's content and not
its directory, so the jobs of one ladder agree, a second run with the same
settings reuses the placement and the finished rungs, and a change of any
handed-over value is a change of the placement's inputs (refused until the
placement directory is removed). The ladder's acceptance command
(`accept.command`) gets the rung's file as `CIRCT_FLOW_PACK_SETTINGS` (unset
when nothing is handed over), so a check that reads pack options sees the
values the rung ran with; a rung's recorded verdict is reused only with the
same settings content. The flow runs no other command of the pack: its
finishing step and the placement check run inside the flow, and Python hooks
see the values as `s.pack_options` (a session) or `cfg.design.pack_settings`
with the profile's `pnr.settings_defaults` (a ladder hook). A pack option
cannot be the ladder's parameter: the ladder varies a key of the flow
config's `pnr:` section.

### Without the pack's command

The ORFS stage (a pack with `orfs_platform_dir` and no `command`, with or
without the ladder) takes no settings file: such a pack declares no registry,
so there are no pack options to set, and values given to the stage anyway
(`cfg.design.pack_settings`) are refused, never dropped. Options the flow
shares with a pack act through the flow config's `pnr:` section there, as on
the command line.

The template's command (`examples/techpack-template/pnr/run.py`) reads
`--settings` and applies the `report.*` options (the label and the note go to
`report.json` beside `pnr_metrics.json`); it is a stand-in without layout, so it
accepts the other options of its registry and does not use them.
