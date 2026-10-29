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

Instead of `liberties:`, a pack may take them from an ORFS image's platform
with `orfs: {platform, liberties}` as `profiles/asap7.yaml` does; they are
extracted into the cache and merged into one file.

`${VAR}` and `${VAR:-default}` are expanded from the environment in every
path; an unset `${VAR}` with no default is an error. Paths are relative to
the pack directory. The first liberty is the primary one, which the
single-liberty passes read.

`python3 -m circt_flow.techpack check <name|path>` loads a pack and checks
that every liberty, `flow:` path and `pnr:` path exists and that the ICG and
tie cells are cells of the liberties.

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
with a private design keeps its manifest beside the flow config.

## Precedence

Each setting a pack can default resolves, highest first:

1. the command line (`python3 -m circt_flow ... --sv2v-docker-image ...`);
2. the environment (`SV2V_DOCKER_IMAGE`, `ENABLE_CLASSICAL_RESUB`,
   `ABC_PARTITIONS`, or the `CIRCT_FLOW_*` knob itself);
3. the pack's `flow:` default;
4. the flow's built-in default.

The keys a `flow:` section may set are `profile.FLOW_DEFAULTS`:
`sv2v_docker_image`, `abc_script_file` (a path in the pack),
`abc_read_lib_args`, `enable_classical_resub`, `partitions`, and `env`.
`env` sets defaults for the flow's `CIRCT_FLOW_*` pass knobs (the ones
`stages.py` documents); only `CIRCT_FLOW_` names are accepted, a YAML
boolean means `1`/`0`, and a knob the environment already sets keeps its
value. The flow prints each default it applies.

The built-in sv2v image is `circt-abc`, the image this repository builds;
inside that image `SV2V` is set and no image is needed.

## Place and route

`--pnr` runs place and route after synthesis, from the flow config's
`pnr:` section (`clock_port` is required; `platform`, `clock_period`,
`clock_latency`, `utilization`, `place_density` and `set:` are ORFS's).
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
