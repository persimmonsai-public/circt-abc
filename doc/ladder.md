# The closure ladder

A P&R run often has one option that is cheap to turn and expensive to get
wrong: a hold-repair margin that is too small leaves a violation, one that is
too large costs area and power. The usual answer is to try the margins one
after another, starting at the smallest, and to keep the first run that passes
its checks. That is correct and slow: every try repeats the placement, and the
tries wait for each other.

(This is the *closure* ladder, a P&R feature. It has nothing to do with the
Vt-flavour ladder of the swap and library documentation, which orders cell
flavours; the two are unrelated and the name "closure ladder" is used for this
one throughout.)

The closure ladder keeps the answer and drops the waiting. It places **once**. Each
**rung** (one value of the option) then restarts from that placement, runs the
stages that remain and an acceptance check, all rungs at the same time. The
result is the **lowest** rung whose check passed, which is what the sequential
loop would have chosen, in the wall-clock time of one rung. Higher rungs are
cancelled as soon as a lower one is known to be the answer. The price is CPU
time: the rungs that turn out not to be needed ran anyway.

```
placement (once) --+--> rung 0: CTS, routing, ..., checks --+
                   +--> rung 1: CTS, routing, ..., checks --+--> lowest rung that passed
                   +--> rung 2: CTS, routing, ..., checks --+
```

It is declared in the flow config's `pnr:` section and works with both P&R
backends of the flow (`doc/techpacks.md`, "Place and route"): the ORFS stage
and a tech pack's own `pnr.command`.

## Declaring it

The closure ladder is for Linux (it reads `/proc` and uses `flock`); the numbers in the examples (`jobs: 3`, `timeout: 3600`, `-c 8`, the rung values) are illustrative.

```yaml
pnr:
  platform: asap7
  clock_port: clk_i
  ladder:
    param: hold_margin           # the option that differs between rungs
    values: [0, 10, 20]          # the rungs, lowest (preferred) first
    mode: parallel               # parallel (default) or sequential
    jobs: 3                      # rungs running at once (default: all)
    from_stage: cts              # first stage the option acts in (see below)
    submit: "srun -c 8 {cmd}"    # optional: run each job through this
    cancel: "scancel --name {name}"   # optional, only with submit
    fresh: false                 # true: discard the placement and rung records
    accept:
      metrics:                   # bounds on pnr_metrics.json
        setup_ws: {min: 0}
        hold_ws:  {min: 0}
        drc_errors: {max: 0}
      corners:                   # optional, ORFS backend: other libraries
        fast:
          libs: [/path/to/fast.lib]
          hold_ws: {min: 0}
      command: ["./check.sh"]    # optional: a user check, exit status decides
      timeout: 3600              # seconds, for the command
```

| key | meaning |
| :--- | :--- |
| `param` | A `pnr:` key (`hold_margin`, `cts_args`, `cts_cluster_size`, ...), or `set.<VARIABLE>` for an ORFS variable (a name that starts with `ladder` is refused). Its value is replaced by each rung's. |
| `values` | The rungs. A list that mixes numbers and non-numbers is refused (it has no order). Numbers must increase (the rungs are tried lowest first and the lowest that passes is the result); a rung is a number (finite, not a boolean), a string or a list (`cts_args`); values must differ (`0` and `0.0` are the same rung). Non-numeric values keep the order given. |
| `mode` | `parallel`: all rungs at once. `sequential`: one after another from the same placement, stopping at the first accepted one. Both give the same answer. |
| `jobs` | At most this many rungs run (and are checked) at once. Default: every rung in `parallel`, one in `sequential`. |
| `from_stage` | The first stage the option can change. Known for the ORFS keys listed below; required for a pack command that declares `stage_restart`. |
| `submit`, `cancel` | See "Where jobs run". A template is checked when the config is loaded (and the environment override too): literal braces are doubled (`{{`, `}}`). |
| `fresh` | `true` (or `--fresh` of `python3 -m circt_flow.ladder`) removes the placement, the rung directories and their records before running: use it when something the manifest does not cover has changed (see "The manifest rule"). |
| `accept` | See "Acceptance". Absent: the neutral default below. |

The parameter must act at `from_stage` or later. An option that changes the
placement (utilization, density, anything the floorplan reads) cannot share one
placement and is refused before anything runs, see "The manifest rule". Hold
margin is the motivating case; the code does not treat it specially.

`python3 -m circt_flow <flow.yaml> <out> --pnr` runs the flow and then the
ladder. `python3 -m circt_flow.ladder <flow.yaml> <out> [--mode M] [--jobs N]`
runs only the ladder on the netlist an earlier flow run left in `<out>`.

## Acceptance

A rung is judged by checks that start together as soon as its P&R run ends;
the first rejection stops the others.

* **Built-in metric checks.** `accept.metrics` is a mapping from a number of
  the rung's `pnr_metrics.json` to a bound (`min`, `max`). The default is
  neutral: `setup_ws >= 0`, `hold_ws >= 0`, `drc_errors <= 0`. Giving `accept.metrics` **replaces** the default set: list every number you want checked. Slack bounds are
  in the platform's time unit, like the metrics themselves. A pack command
  that does not write one of the named numbers leaves the rung unverified; list
  the numbers it does write. A key may be a dotted path into a nested mapping
  of `pnr_metrics.json` (`group.name`). When the pack's command writes a
  `final` mapping, its entries are what the checks read in place of the same
  top-level keys; without one nothing changes.
* **Corner checks** (ORFS backend). `accept.corners.<name>` times the rung's
  routed netlist, SDC and SPEF with the listed Liberty files (OpenSTA in the
  ORFS image, or `$OPENSTA`) and bounds `setup_ws` / `hold_ws` of that corner.
  The OpenSTA run is a job of the ladder like the others: it has a name, runs
  beside the other checks, and is stopped (its container too) when another
  check rejects the rung, when the rung is cancelled and when the ladder is
  interrupted.
  Library paths that start with `/OpenROAD-flow-scripts/` are read inside the
  image; others are mounted.
* **User command.** `accept.command` runs once per rung in the rung's
  directory. Its **exit status is the check's**: with a `submit` template the
  ladder reads the status the command itself recorded when it ended (see
  "Where jobs run"), never the exit status of the submit command, so a
  scheduler wrapper that fails or returns early cannot turn into a pass or a
  rejection. Words that start with `./` or `../` are relative to the flow
  config. It gets the environment variables `CIRCT_FLOW_RUNG_DIR`,
  `CIRCT_FLOW_RUNG_INDEX`, `CIRCT_FLOW_LADDER_PARAM`,
  `CIRCT_FLOW_LADDER_VALUE` (JSON), `CIRCT_FLOW_OUT` (the flow's `pnr` directory, `<out>/pnr`) and
  `CIRCT_FLOW_PNR_METRICS`. **Exit 0: pass. Exit 1: reject. Exit 1 is also what an uncaught Python exception gives, so when `check.log` ends the way a crash ends the status is read as unverified: the last `Traceback (most recent call last):` followed only by indented lines (and the `During handling...` notices) and then one final line of the form `Name` or `Name: message`, where `Name` contains a lower-case letter (`ValueError`, `StopIteration`, `KeyboardInterrupt`). Anything after that final line, or a final line like `REJECT: too slow` (no lower-case letter in the first word), is the script's own output and the exit 1 stays a rejection. This is a heuristic: a multi-line message, or a script's own last line shaped like an exception, is misread. A check should catch its own errors and exit 2 for them. Anything else
  (another status, a crash, a timeout, not starting): the check did not
  complete.** This is where a slower check goes: a simulation, a signoff run.

A rung is **accepted** only if every check ran and passed. A rejection (a
failed bound or exit 1) outranks an incomplete check. A rung whose checks did
not all complete is **unverified**, never accepted; a rung whose P&R run failed
is an **error**.

## Which rung wins

The verdicts are read in rung order. The outcome is fixed by the first rung
that is not rejected: if it is accepted, that is the result; if it is
unverified or an error, the ladder stops there (the later rungs are not
considered, because the answer to "does a lower rung pass" is unknown); if
every rung is rejected there is no result. A higher rung that finishes first
changes nothing; the ladder waits for the lower ones. As soon as the outcome is
fixed, the rungs above it are cancelled.

Exit codes of the flow (and of `python3 -m circt_flow.ladder`): 0 accepted, 5
every rung rejected, 6 unverified, 7 a rung's P&R run or the placement run
failed, 130 interrupted, 1 a declaration or manifest the ladder refuses to run
(and every other error of the flow).
`<out>/pnr/ladder/RESULT.json` has the `state` (`accepted`, `none`,
`unverified`, `error`, `interrupted`, or, when the run ended in an exception,
`refused` / `error` / `interrupted` with a `message`), the verdict (`accepted`,
`rejected`, `unverified`, `error`, `not run` in sequential mode, `not needed`
in parallel mode, `cancelled` for a rung stopped by an interrupt, `null` for a
rung that never got a verdict), reasons and metrics of every rung, the mode and the
wall-clock times; the log prints one line per rung. `pnr_metrics.json` of a
run that was not accepted says `closure ladder: <state>`.

## Layout and resuming

```
<out>/pnr/
  pnr_metrics.json          the accepted rung's (status "closure ladder: <state>" otherwise)
  accepted -> ladder/rung_<i>   (only for an accepted rung; removed at the start of every run)
  ladder/
    place/                  the placement run
    rung_0/ rung_1/ ...     one run directory per rung
    plan/                   scratch: each rung's inputs and plan manifest
    RESULT.json  processes.json  ladder.lock
    rung_<i>/rung_result.json  the record of a judged rung; per job: ladder_run.log or
                            pnr.log, check.log, corner_<name>.{tcl,log}, <log>.<job>.status
    place/PLACED            the placement's manifest hash
```

Running again resumes. A placement that is already there is reused when its
manifest matches this run's. A rung whose result record matches is not run
again; a record matches only if **everything that can change that rung's
result** is the same: its value, the whole plan of the rung (every option of
every stage, including the ones that act at the restart stage and later, the
input hashes, the tool), the acceptance definition **with the content of the
files it names** (the check script, local Liberty files) and the placement.
Change `cts_args`, a route option or the check script and the rungs run again
from the (reused) placement. Unverified and failed rungs, and rungs that were
cancelled or interrupted, run again. A rung that runs starts in an **empty directory** (the placement and the plans are elsewhere): nothing a check reads can be left from an earlier run, and a job of an earlier run that is somehow still writing there, by a path relative to its working directory or through an open file, writes into a directory the ladder has already replaced (one that writes by absolute path writes into the new directory: that is why such a job is cancelled first). The final `pnr_metrics.json` is written from the metrics the rung was judged on, not read again from the rung directory.

`ladder/RESULT.json` is removed when a run starts and written atomically; a run that stops with an error (a stale placement, a refused plan, a failed placement) writes one with `state: refused` or `error` and a message, so it never shows an earlier run's verdict.

**Paths are part of the keys.** The placement key and the rung records contain absolute paths: the pack directory and the words of the pack command, the `stage_inputs`, the platform directory (as an ORFS option as well as by content), and the check command and Liberty files of the acceptance (resolved against the flow config's directory). Moving or renaming any of them, or the flow config, makes the next run refuse the placement or run the rungs again even though the content is the same; use `fresh` (or a new output directory) after such a move. The corner job's `OPENSTA` and `ORFS_IMAGE` count only when corners are configured.

Relative paths of the acceptance are relative to **the flow config's directory**, not to the directory the flow is started from. A command word that starts with `./` or `../` always is; any other command word is when a file or directory of that name exists next to the flow config (a bare `python3` is looked up on `PATH`). A corner library is when a file of that name exists next to the flow config (a path that starts with `./` is not special for libraries); otherwise it is used as given, and a library that does not exist is refused before anything runs.

What an interrupted run of the same directory left behind is stopped first:
each recorded local process whose pid and start time still match (a record without a start time or from an earlier boot of this host is never signalled, an earlier boot's is dropped; a local process recorded on another host is left alone, kept and reported once per run), and, with a
`cancel` template (which must contain `{name}`; a template without it is refused), **every** recorded scheduler job by its recorded name, from any host (names are unique to the run; the record is dropped once cancel succeeded). A job record is written before the job starts (without a pid), so even a hard kill of the ladder leaves the scheduler job's name to cancel. A job whose submit command returned without the job having finished is cancelled at once; without a `cancel` template it stays in `processes.json` as an orphan and every later run warns about it. A process of an earlier run that is still alive after SIGKILL is recorded as unkillable; the ladder stops at once (not just that rung) saying which process group to end, and a later run, `fresh` included, refuses to start while that group is alive. Two runs cannot
use one directory at the same time (`flock`: on a network file system that depends on the file system honouring it). A placement directory without its `PLACED` marker is emptied before it is placed into again. `fresh: true` redoes everything.

## The manifest rule

A restart is only valid from a placement made with the same earlier stages. So
every run records, in `stage_manifest.json`, its **inputs** (the netlist, the
SDC, the hook and RC files it was given, the platform directory, each as a
SHA-256), its **tool** (the container image reference and, when `docker image
inspect` can tell, its id; or the in-place installation's path and the hash of
its variable table) and its **options** grouped by the first stage each can
change.

A rung is refused, before anything runs (and for the ORFS stage again when it starts, which `orfs_pnr --from-dir` checks itself), if any input,
the tool, or any option of a stage before the restart stage differs from the
placement's. **An option the code does not know counts as belonging to the
first stage**, so a new or unlisted option can never slip into a restart: it
blocks the restart until someone declares where it acts. Nothing is ever
carried over silently.

The records also carry a format number of the ladder: a version that changes what a saved placement or rung record means does not honour old ones. The corner job's `OPENSTA` and `ORFS_IMAGE` (when corners are configured) and the files `accept` names are part of the acceptance digest.

Directory trees are hashed by name, kind and content with symbolic links followed (a link loop is cut at its second visit); a file that cannot be read, or a link that points nowhere, is an error that names the path, not a gap in the key. The acceptance digest covers files only, not directory-valued command words, the check's environment or what the check reads.

**What the manifest does not know.** For the ORFS stage: environment variables
a locally installed ORFS reads (they are not hashed), and an in-place upgrade
of the OpenROAD binaries under an unchanged path; with a container, the image
id covers the platform files only if `docker image inspect` works (otherwise
the mutable tag is all there is). When any of that changed, use `fresh`. A placement is identified by its manifest, not by its content: removing `place/` and placing again with a tool that does not repeat itself leaves rung records that refer to the earlier placement.

## The ORFS backend

The ORFS stage restarts with ORFS's own `make` targets and result files; ORFS
is not modified. The placement run is `make place` (or `make cts` for a
`from_stage` of `route`). A rung copies that run's `work/` tree (results, logs,
reports, objects) with the files' modification times, which is what `make`
reads to decide what is stale, drops the results of the restart stage and later
and the source run's metadata, copies the run's own input files with their
original times, and runs `make finish`. `make` then does only the remaining
stages. The result is the same as a straight run with the rung's value (the
tests check it with a stand-in makefile; `examples/shift-select/README.md`
checks it on the `align_unit` example, file by file).

The keys the ORFS stage knows act at these stages:

| first stage | options |
| :--- | :--- |
| `cts` | `hold_margin` (`HOLD_SLACK_MARGIN`, when it is not negative and `ENABLE_PLACE_REPAIR_TIMING` is off), `cts_args`, `cts_buffers`, `cts_cluster_size`, `cts_cluster_diameter` (all `CTS_ARGS`), `SKIP_CTS_REPAIR_TIMING`, `CTS_SNAPSHOTS` |
| `route` | `GLOBAL_ROUTE_ARGS`, `DETAILED_ROUTE_ARGS`, `SKIP_INCREMENTAL_REPAIR`, `SKIP_DETAILED_ROUTE`, antenna-repair and fill variables |
| anything else | the floorplan: refused as a ladder parameter |

`setup_margin` is in the last row: ORFS's floorplan already repairs setup to
it, so rungs of it do not share a placement. (A `from_stage: route` ladder
shares the placement **and** the clock tree.) The table is
`orfs_pnr._VAR_STAGE`; a variable not in it is treated as acting in the
floorplan.

`pnr: stage_inputs` is read for a pack command only (it is ignored with `orfs_platform_dir`).

Files the ladder and the ORFS stage write besides the ones listed: `stage_manifest.json` and `plan_manifest.json` (the manifest above), `<log>.<job>.status` and `<log>.<job>.status.started` (a job's completion marker), per-job logs; environment variables read: `ORFS_HOME` (ORFS in place), `ORFS_IMAGE`, `OPENSTA`. `jobs` is ignored in sequential mode.

`orfs_pnr` can be used on its own: `--target place` stops after the placement,
`--from-dir <that run> --from-stage cts` restarts.

## The command contract

A tech pack's own P&R command (`pnr.command`) is laddered when the pack
declares which stages it can stop after and start at:

```yaml
pnr:
  command: ["./pnr/run.py"]
  stage_restart: [place, cts, route]     # in flow order
```

and the flow config's `from_stage` is one of them but the first. The command
keeps its usual arguments (`--netlist --sdc --top --out --pack`) and its
`pnr_metrics.json` contract, and honours these environment variables:

| variable | meaning |
| :--- | :--- |
| `CIRCT_FLOW_STOP_AFTER=<stage>` | Run the stages up to and including `<stage>`, leave what a restart needs in `--out`, exit 0. Write no metrics. |
| `CIRCT_FLOW_START_AT=<stage>` and `CIRCT_FLOW_START_FROM=<dir>` | Start at `<stage>` from the state an earlier stopped run left in `<dir>` (read-only: copy what you need), run to the end, write `pnr_metrics.json` into `--out`. |
| `CIRCT_FLOW_LADDER_PARAM`, `CIRCT_FLOW_LADDER_VALUE` | The parameter and its value for this run (JSON). Set on every run of the ladder, the placement run included (it gets the first rung's value, and must not depend on it). |
| `CIRCT_FLOW_LADDER_RUNG` | The rung index (not set for the placement). |

`stage_inputs: [path, ...]` in the pack's `pnr:` lists files and directories
(relative to the pack) that the command reads and that a restart must not
outlive a change of: they are hashed into the placement's manifest.

**What the command backend's manifest covers**: the whole command line, the
content of every word of it that is a file (the script), the netlist and SDC
the flow gives it, the environment the flow itself builds for it (the pack's wire RC files and what they name, the variables the flow built for the command: values and file content), every option of the flow config's `pnr:` section (the
command cannot say which of them act before the restart stage, so all count as
acting before it; only the ladder parameter is left out), and the
`stage_inputs`. It does **not** cover anything else the command reads (data it
finds by itself, the environment, tools it calls). When such a thing changes,
list it in `stage_inputs` or run with `fresh`.

Requirements: a restart at `<stage>` from a run stopped before it gives the
same result as a straight run with the rung's value; the command exits non-zero
when it fails. The command cannot say which of the flow config's options act
before the restart stage, so the ladder treats **all** of them (other than
`param`) as acting before it: a changed option refuses to reuse a placement.

A command that does not declare `stage_restart`, or whose list lacks
`from_stage`, is not stopped or restarted: **every rung is a full run** with
`CIRCT_FLOW_LADDER_*` set, still in parallel. That costs the placement once per
rung and gives the same answer.

## Where jobs run

By default every job (the placement, each rung, each user check) is a local
process in its own process group, and `jobs` bounds how many rungs run at
once. With `submit` each job runs through a template instead. `{cmd}` (required)
is the job's command line, shell-quoted and prefixed with `env` and the
variables the job needs: the `CIRCT_FLOW_*` variables, `PYTHONPATH`, and every
variable the flow set or changed for the job (absent from, or different in,
the submitting process's environment). These values are written on the job's
command line, where process listings and scheduler records can show them: keep
secrets out of a job's environment and let the command read them from a file; `{name}` is a unique job name for this run;
`{cmdq}` is the same line as one quoted word (for `sbatch --wrap`); `{log}` and
`{cwd}` are its log file and directory. The template runs under
`bash -c` and **must block until the job has finished and exit with its
status**: `srun` does, and passes a termination signal on to the job, as does
`sbatch --wait` when `cancel` is given; a fire-and-forget submit command does
not. The ladder does not trust that: it runs the job behind a small launcher
(`python -m circt_flow.ladder --exec <marker> -- <command>`) that writes the
command's exit status to a marker file when the command ends, and judges the
job by the marker. If the submit command ends without the marker (it did not
block, or it failed itself, or the job was lost, or the marker carries a different token than the one issued for this attempt) a rung is an **error** and a
user check is **unverified**, never accepted and never rejected. The launcher runs the same Python and the same `circt_flow` tree as the ladder (`sys.executable` and `PYTHONPATH` are part of the command line): on an execution node where they differ the job ends without its marker. A timeout (`accept.timeout`) includes the time a job waits in a scheduler queue. One attempt of a job runs once: the launcher claims `<marker>.started` first, so a job that the scheduler starts a second time (a requeue, a duplicate) refuses to run and leaves the first attempt's marker alone; the rung is then judged on the first attempt if it finished, or is an error if it did not. A scheduler that requeues on node failure therefore fails the rung instead of running two copies into one directory; rerun the ladder to resume. The template
is a format string: write a literal brace as `{{` or `}}`; `{log}` and `{cwd}` are quoted for you (do not add quotes), and `{name}` is unique to the job (a fixed-width six-digit counter, so no name contains another). Examples:

```yaml
submit: "srun --job-name={name} --cpus-per-task=8 --output={log} {cmd}"
```

```yaml
submit: "sbatch --wait --job-name={name} --cpus-per-task=8 -o {log} --wrap {cmdq}"
cancel: "scancel --name {name}"     # a name is unique to this run
```

The environment variables `CIRCT_FLOW_LADDER_SUBMIT` and
`CIRCT_FLOW_LADDER_CANCEL` override `submit` and `cancel` (and are checked like
them), so the flow config can stay portable.

A timeout applies to the user check only; the P&R jobs and the corner STA have none. A check that times out because rungs ran side by side (contention) is unverified, so the two modes give the same answer only when no check times out.

**Cancelling touches only what this run started.** The ladder signals the
process groups it created (and, with `cancel`, runs the template for the unique
names it generated), nothing else; it records them in `processes.json`. An
ORFS container started by the ORFS stage is stopped with it.

## Cost

With `n` rungs, the placement runs once and each rung runs the stages from
`from_stage` on. Sequentially the wall-clock is the placement plus the rungs
up to the accepted one; in parallel it is the placement plus the slowest of
the rungs up to the accepted one (those above it are cancelled when it is
known). The CPU time of a parallel ladder is the placement plus every rung that
started, so up to `n` times the CPU time of the rungs a sequential ladder needs
when the first rung already passes. Choose `jobs` to the machines you have;
rungs share nothing, so they scale until the cores run out.
