"""Closure ladder: one placement, several variants of the later stages in
parallel, the lowest variant that passes its acceptance check is the result.

A flow config declares it in its `pnr:` section:

    pnr:
      ladder:
        param: hold_margin            # a `pnr:` key (or set.<ORFS VARIABLE>)
        values: [0, 10, 20]           # the rungs, lowest first
        mode: parallel                # parallel (default) | sequential
        jobs: 3                       # rungs running at once
        from_stage: cts               # first stage the parameter acts in
        submit: "srun -c 8 {cmd}"      # optional: run each job through this
        accept:
          metrics: {setup_ws: {min: 0}, hold_ws: {min: 0}}
          command: ["./check.sh"]     # optional: exit 0 pass, 1 reject

Placement is done once (a run that stops after the stage before `from_stage`).
Each rung then restarts from that placement with its own value of the
parameter, runs the remaining stages and the acceptance check. The result is
the lowest rung whose check passed, exactly what running the rungs one after
another and stopping at the first that passes gives; in parallel mode it
takes the wall-clock of one rung and costs the CPU time of the rungs that
were not needed. doc/ladder.md has the full contract.

This module holds the mechanism (specification, process runner, manifest
guard, decision, report); the two P&R backends it can drive (the ORFS stage
and a tech pack's own `pnr.command`) are the classes at the end.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import re
import os
import shlex
import socket
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# Exit codes of a finished ladder (the flow's own errors are 1).
EXIT_ACCEPTED = 0
EXIT_NONE_ACCEPTED = 5      # every rung was rejected
EXIT_UNVERIFIED = 6         # a rung passed or could not be judged: not accepted
EXIT_RUNG_FAILED = 7        # a rung's P&R run failed
EXIT_INTERRUPTED = 130

ACCEPTED, REJECTED, UNVERIFIED, ERROR = "accepted", "rejected", "unverified", "error"
CANCELLED = "cancelled"
# What a job reports when its submit command ended without the job's own
# completion marker (it did not block, or the job was lost).
NO_MARKER = -1000
# Bumped when the meaning of a saved placement or rung record changes (the
# verdict logic, the restart copy, what the plans hash): old ones are then
# not honoured.
FORMAT = 2


class LadderError(ValueError):
    """A ladder section that cannot be run (raised before anything runs)."""


class LadderStuck(LadderError):
    """Processes of an earlier job survived SIGKILL: the run stops."""


class LadderBusy(LadderError):
    """Another run holds the directory's lock."""


class LadderFailed(RuntimeError):
    """The ladder finished without an accepted rung; `code` is the exit code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """A bound on one number of pnr_metrics.json."""
    key: str
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass(frozen=True)
class CornerCheck:
    """A bound on the worst slack of the rung's routed result timed with other
    libraries (an OpenSTA run on the result's netlist, SDC and SPEF)."""
    name: str
    libs: tuple
    checks: tuple          # Checks on setup_ws / hold_ws


@dataclass(frozen=True)
class Accept:
    checks: tuple = (Check("setup_ws", min=0.0), Check("hold_ws", min=0.0),
                     Check("drc_errors", max=0.0))
    corners: tuple = ()
    command: Optional[tuple] = None
    timeout: Optional[float] = None

    def digest(self) -> str:
        """The acceptance definition, including the content of every file it
        names (the command's script, local Liberty files)."""
        files = {}
        for w in list(self.command or ()) + [l for c in self.corners
                                              for l in c.libs]:
            if os.path.isfile(w):
                files[w] = _file_sha(Path(w))
        # The corner job's OpenSTA binary and image come from the environment.
        # (only when corners are configured: nothing else runs OpenSTA).
        env = ({k: os.environ.get(k) for k in ("OPENSTA", "ORFS_IMAGE")}
               if self.corners else {})
        if env.get("OPENSTA") and os.path.isfile(env["OPENSTA"]):
            files[env["OPENSTA"]] = _file_sha(Path(env["OPENSTA"]))
        return _digest({"format": FORMAT, "env": env,
                        "checks": [(c.key, c.min, c.max) for c in self.checks],
                        "corners": [(c.name, c.libs,
                                     [(k.key, k.min, k.max) for k in c.checks])
                                    for c in self.corners],
                        "command": self.command, "files": files})


@dataclass(frozen=True)
class LadderSpec:
    param: str
    values: tuple
    mode: str = "parallel"
    jobs: Optional[int] = None
    from_stage: Optional[str] = None
    submit: Optional[str] = None
    cancel: Optional[str] = None
    accept: Accept = field(default_factory=Accept)
    fresh: bool = False

    @property
    def slots(self) -> int:
        if self.mode == "sequential":
            return 1
        return self.jobs or len(self.values)


_KEYS = {"param", "values", "mode", "jobs", "from_stage", "submit", "cancel",
         "accept", "fresh"}
_ACCEPT_KEYS = {"metrics", "corners", "command", "timeout"}
_STA_KEYS = {"setup_ws", "hold_ws"}


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v):
        raise LadderError(f"{where}: expected a finite number, got {v!r}")
    return float(v)


def _checks(d, where: str, allowed=None) -> tuple:
    if not isinstance(d, dict) or not d:
        raise LadderError(f"{where}: expected a non-empty mapping "
                          "{key: {min: N, max: N}}")
    out = []
    for k, b in d.items():
        if allowed is not None and k not in allowed:
            raise LadderError(f"{where}.{k}: not one of "
                              f"{', '.join(sorted(allowed))}")
        if not isinstance(b, dict) or not b or set(b) - {"min", "max"}:
            raise LadderError(f"{where}.{k}: give min and/or max")
        lo = _num(b["min"], f"{where}.{k}.min") if "min" in b else None
        hi = _num(b["max"], f"{where}.{k}.max") if "max" in b else None
        out.append(Check(str(k), lo, hi))
    return tuple(out)


def check_template(tpl: str, keys, where: str, need=()) -> None:
    """A submit/cancel template is a format string over plain fields only:
    each `{field}` must be one of `keys` (no attribute or index access, no
    conversion, no format spec); a literal brace is written `{{` or `}}`; every
    field in `need` must occur at least once (a `{{cmd}}` is a literal, not the
    field)."""
    import string
    try:
        parts = list(string.Formatter().parse(tpl))
    except ValueError as e:
        raise LadderError(f"{where}: not a valid template ({e}); write a "
                          "literal brace as {{ or }}")
    used = set()
    for _lit, field, spec, conv in parts:
        if field is None:
            continue
        if field not in keys or spec or conv:
            raise LadderError(
                f"{where}: {{{field}{('!' + conv) if conv else ''}"
                f"{(':' + spec) if spec else ''}}} is not a placeholder; "
                f"placeholders: {', '.join('{' + k + '}' for k in keys)} "
                "(no attributes, indexes, conversions or format specs; write "
                "a literal brace as {{ or }})")
        used.add(field)
    for n in need:
        if not (used & set(n if isinstance(n, tuple) else (n,))):
            names = n if isinstance(n, tuple) else (n,)
            raise LadderError(f"{where}: the template must contain "
                              + " or ".join("{" + x + "}" for x in names))


SUBMIT_KEYS = ("cmd", "cmdq", "name", "log", "cwd")
CANCEL_KEYS = ("name",)


def parse_ladder(d, where: str = "pnr.ladder") -> LadderSpec:
    """Validate a `pnr: ladder:` mapping. Errors name the key."""
    if not isinstance(d, dict):
        raise LadderError(f"{where}: expected a mapping")
    bad = set(d) - _KEYS
    if bad:
        raise LadderError(f"{where}.{sorted(bad)[0]}: unknown key; allowed: "
                          + ", ".join(sorted(_KEYS)))
    for req in ("param", "values"):
        if req not in d:
            raise LadderError(f"{where}.{req}: required")
    param = d["param"]
    if not isinstance(param, str) or not param or param.startswith("ladder"):
        raise LadderError(f"{where}.param: a `pnr:` key name (or "
                          "set.<VARIABLE>)")
    values = d["values"]
    if not isinstance(values, list) or not values:
        raise LadderError(f"{where}.values: a non-empty list")
    for v in values:
        if isinstance(v, bool):
            raise LadderError(f"{where}.values: {v!r} is a boolean; give "
                              "numbers, strings or lists")
        if isinstance(v, float) and not math.isfinite(v):
            raise LadderError(f"{where}.values: {v!r} is not a finite number")
        if isinstance(v, int) and abs(v) > 10 ** 15:
            raise LadderError(f"{where}.values: {v!r} is out of range")

    def ident(v):      # 0 and 0.0 are the same rung
        return (float(v) if isinstance(v, (int, float))
                else json.dumps(v, sort_keys=True))
    if len({ident(v) for v in values}) != len(values):
        raise LadderError(f"{where}.values: rung values must differ")
    nums = [v for v in values if isinstance(v, (int, float))]
    if nums and len(nums) != len(values):
        raise LadderError(f"{where}.values: either all numbers or none (the "
                          "order of the rungs is the order of the numbers)")
    if len(nums) == len(values) and any(b <= a for a, b in zip(nums, nums[1:])):
        raise LadderError(f"{where}.values: numbers must increase: the rungs "
                          "are tried lowest first and the lowest that passes "
                          "is the result")
    mode = d.get("mode", "parallel")
    if mode not in ("parallel", "sequential"):
        raise LadderError(f"{where}.mode: parallel or sequential, got {mode!r}")
    jobs = d.get("jobs")
    if jobs is not None and (isinstance(jobs, bool) or not isinstance(jobs, int)
                             or jobs < 1):
        raise LadderError(f"{where}.jobs: a positive integer")
    stage = d.get("from_stage")
    if stage is not None and (not isinstance(stage, str) or not stage):
        raise LadderError(f"{where}.from_stage: a stage name")
    for k in ("submit", "cancel"):
        if k in d and (not isinstance(d[k], str) or not d[k].strip()):
            raise LadderError(f"{where}.{k}: a command template string")
    if "submit" in d:
        check_template(d["submit"], SUBMIT_KEYS, f"{where}.submit",
                       (("cmd", "cmdq"),))
    if "cancel" in d:
        check_template(d["cancel"], CANCEL_KEYS, f"{where}.cancel", ("name",))
    fresh = d.get("fresh", False)
    if not isinstance(fresh, bool):
        raise LadderError(f"{where}.fresh: true or false")
    a = d.get("accept") or {}
    if not isinstance(a, dict):
        raise LadderError(f"{where}.accept: expected a mapping")
    bad = set(a) - _ACCEPT_KEYS
    if bad:
        raise LadderError(f"{where}.accept.{sorted(bad)[0]}: unknown key; "
                          f"allowed: {', '.join(sorted(_ACCEPT_KEYS))}")
    checks = (_checks(a["metrics"], f"{where}.accept.metrics")
              if "metrics" in a else Accept().checks)
    corners = []
    for name, cd in (a.get("corners") or {}).items():
        w = f"{where}.accept.corners.{name}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name)):
            raise LadderError(f"{w}: a corner name is letters, digits, _ . -")
        if not isinstance(cd, dict) or "libs" not in cd:
            raise LadderError(f"{w}: needs libs: [liberty files] and "
                              "setup_ws / hold_ws bounds")
        libs = cd["libs"]
        if not isinstance(libs, list) or not libs or \
                not all(isinstance(x, str) for x in libs):
            raise LadderError(f"{w}.libs: a non-empty list of paths")
        rest = {k: v for k, v in cd.items() if k != "libs"}
        corners.append(CornerCheck(str(name), tuple(libs),
                                   _checks(rest, w, _STA_KEYS)))
    cmd = a.get("command")
    if cmd is not None and (not isinstance(cmd, list) or not cmd
                            or not all(isinstance(x, str) for x in cmd)):
        raise LadderError(f"{where}.accept.command: a non-empty list of words")
    timeout = a.get("timeout")
    if timeout is not None:
        timeout = _num(timeout, f"{where}.accept.timeout")
        if timeout <= 0:
            raise LadderError(f"{where}.accept.timeout: positive seconds")
    return LadderSpec(param=param, values=tuple(values), mode=mode, jobs=jobs,
                      from_stage=stage, submit=d.get("submit"),
                      cancel=d.get("cancel"),
                      accept=Accept(checks, tuple(corners),
                                    tuple(cmd) if cmd else None, timeout),
                      fresh=fresh)


def _write_atomic(path: Path, text: str) -> None:
    """Write a file so that a reader (or a rerun after a crash) sees either
    the old content or the whole new one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str)
                          .encode()).hexdigest()


def apply_param(p: dict, param: str, value) -> dict:
    """A copy of the `pnr:` mapping with the parameter set: `set.<VAR>` sets
    an ORFS variable, anything else is a `pnr:` key."""
    q = copy.deepcopy(p)
    q.pop("ladder", None)
    if param.startswith("set."):
        q.setdefault("set", {})
        q["set"] = dict(q["set"] or {})
        q["set"][param[4:]] = value
    else:
        q[param] = value
    return q


# ---------------------------------------------------------------------------
# Processes: only what this run started is ever signalled
# ---------------------------------------------------------------------------


def _host_id() -> str:
    """Names the machine (and boot) a process record was made on: pids mean
    nothing on another host."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = ""
    return f"{socket.gethostname()}/{boot}"


def _proc_start(pid: int) -> Optional[str]:
    """The start time field of /proc/<pid>/stat: with the pid it identifies
    one process (pids are reused). None without /proc (Linux only): a record
    is then never matched and nothing is signalled."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return data.rsplit(")", 1)[-1].split()[19]


@dataclass
class Job:
    name: str
    argv: list
    env: dict
    cwd: Path
    log: Path
    # Light jobs (a corner STA) run locally even with a submit template.
    local: bool = False
    # Run when the job is cancelled, after its process group is signalled
    # (e.g. `docker kill <name>`).
    kill_cmd: Optional[list] = None


class Handle:
    def __init__(self, popen: subprocess.Popen, job: Job, runner: "Runner",
                 status: Optional[Path] = None, token: str = ""):
        self.popen, self.job, self.runner = popen, job, runner
        self.token = token
        self.pgid = popen.pid           # start_new_session: pid == pgid
        self.key = ""
        self.status = status            # the job's completion marker, if any
        self.jobname = ""

    def wait(self, stop: threading.Event, timeout: Optional[float] = None
             ) -> Optional[int]:
        """The job's exit status; None when `stop` was set or the timeout
        passed (the job is then killed); NO_MARKER when the job ran through a
        submit command that ended without the job's completion marker."""
        t0 = time.monotonic()
        while True:
            try:
                rc = self.popen.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if stop.is_set() or (timeout is not None
                                 and time.monotonic() - t0 > timeout):
                self.kill()
                return None
        if self.status is None:
            self.runner.forget(self)
            return rc
        try:
            code_s, tok = self.status.read_text().split()
            code = int(code_s)
            if tok != self.token:       # not the marker of this attempt
                raise ValueError(tok)
        except (OSError, ValueError):
            # The submit command ended and the job did not say it had: the job
            # may still be running. Stop it, or keep it on record.
            self.runner.orphan(self)
            return NO_MARKER
        self.runner.forget(self)
        return code

    def kill(self) -> None:
        self.runner.kill(self)


def _wait_exit_nowait(pid: int, timeout: float) -> bool:
    """Wait until the child has exited without reaping it (a zombie keeps its
    pid and group id reserved). False when the timeout passes first."""
    end = time.monotonic() + timeout
    while True:
        try:
            r = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        if r is not None:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _group_alive(pgid: int) -> bool:
    """True while a process of the group is running. A zombie (a process that
    has ended and waits to be reaped by its parent, or by init, which a
    container may not have) is not running."""
    try:
        names = os.listdir("/proc")
    except OSError:
        names = None
    if names is None:                       # no /proc: signal 0 is all there is
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    for n in names:
        if not n.isdigit():
            continue
        try:
            data = Path(f"/proc/{n}/stat").read_text()
        except OSError:
            continue
        fields = data.rsplit(")", 1)[-1].split()
        if len(fields) > 2 and fields[0] != "Z" and int(fields[2]) == pgid:
            return True
    return False


def _exec(status: str, token: str, cmd: list) -> int:
    """`python -m circt_flow.ladder --exec <marker> <token> -- <command>`: run
    the command and record its exit status and the ladder's token for this
    attempt in the marker when it ends. A job that went through a submit
    command is judged by the marker, not by the exit status of the submit
    command, which can fail or return early on its own.

    One attempt runs once: the launcher claims `<marker>.started` before it
    runs the command, and a second start of the same attempt (a requeued or
    duplicated scheduler job) refuses to run and leaves the marker alone, so
    two copies never write one run directory."""
    try:
        fd = os.open(status + ".started", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("closure ladder: this job was already started once; not "
              "running it a second time", file=sys.stderr)
        return 125
    os.close(fd)
    try:
        os.unlink(status)
    except FileNotFoundError:
        pass
    child = []

    def fwd(sig, frm):
        if child:
            child[0].terminate()
    signal.signal(signal.SIGTERM, fwd)
    signal.signal(signal.SIGINT, fwd)
    try:
        p = subprocess.Popen(cmd)
    except OSError as e:
        print(f"closure ladder: cannot run {cmd[0]}: {e}", file=sys.stderr)
        rc = 127
    else:
        child.append(p)
        rc = p.wait()
    tmp = status + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{rc} {token}\n")
    os.replace(tmp, status)
    return rc


class Runner:
    """Starts a job as its own process group (local) or through the submit
    template, and remembers every one it started, so that cancelling touches
    nothing else."""

    def __init__(self, state_dir: Path, submit: Optional[str] = None,
                 cancel: Optional[str] = None, grace: float = 5.0):
        self.state = state_dir / "processes.json"
        self.submit, self.cancel_tpl, self.grace = submit, cancel, grace
        self.tag = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self._lock = threading.Lock()
        self._live: dict[int, dict] = {}
        self._n = 0
        self.unresolved: list = []

    def _save(self) -> None:
        tmp = self.state.with_name(self.state.name + ".tmp")
        tmp.write_text(json.dumps(list(self._live.values()), indent=1))
        os.replace(tmp, self.state)

    def _run_cancel(self, jobname: str) -> bool:
        """Run the cancel template for a job name; True when it exited 0."""
        if not (self.cancel_tpl and jobname):
            return False
        r = subprocess.run(["bash", "-c", self.cancel_tpl.format(name=jobname)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0

    def reap_stale(self) -> list:
        """Stop what an earlier, interrupted run of this directory started and
        left running. A scheduler job is cancelled by its recorded name (names
        are unique to the run, so the cancel template is valid from any host)
        and its record dropped once cancel succeeded. A local process is
        signalled only if the record is from this host and boot and its pid
        and start time still match; a record from an earlier boot of this host
        is dropped (nothing local survives a reboot); a local process recorded
        on another host is left alone, kept, and reported. A record written
        just before a job was started (no pid yet) is only cancelled by name.
        A process that survived SIGKILL in an earlier run is `unkillable`: while
        it is alive the run refuses to start (LadderStuck). Records that could
        not be dealt with stay in `self.unresolved`, with what to do."""
        killed, keep = [], []
        self.unresolved = []
        if not self.state.is_file():
            return killed
        try:
            old = json.loads(self.state.read_text())
        except ValueError:
            old = []
        me_host, _, me_boot = _host_id().partition("/")
        clear = ("; once it is known to be gone, delete "
                 f"{self.state} to stop this warning")
        signalled = []
        for e in old:
            host, _, boot = str(e.get("host", "")).partition("/")
            pid, start0 = e.get("pid"), e.get("start")
            if e.get("unkillable") and host == me_host and boot == me_boot \
                    and pid and _group_alive(pid):
                lead = _proc_start(pid)
                if start0 is not None and lead is not None and lead != start0:
                    continue         # the group id was recycled: not ours
                raise LadderStuck(
                    f"closure ladder: processes of {e.get('name')} (process "
                    f"group {pid}) survived SIGKILL in an earlier run and are "
                    "still running; they may write into the run directory. "
                    f"End them (for example `kill -9 -{pid}`) and run again, "
                    "or use another output directory. If they are known to be "
                    f"gone, delete {self.state}.")
            if e.get("jobname"):
                if self.cancel_tpl:
                    if self._run_cancel(e["jobname"]):
                        killed.append(e["name"])
                    else:
                        keep.append(e)
                        self.unresolved.append(
                            f"{e['name']} (cancel template failed for "
                            f"{e['jobname']}{clear})")
                elif e.get("orphan"):
                    keep.append(e)
                    self.unresolved.append(
                        f"{e['name']} (job {e['jobname']} may still be "
                        f"running; no cancel template to stop it{clear})")
            if host != me_host:
                if not e.get("jobname"):
                    keep.append(e)
                    self.unresolved.append(
                        f"{e.get('name')} (a local process recorded on "
                        f"another host, {host}: not touched{clear})")
                continue
            if boot != me_boot or not pid:
                continue
            start = _proc_start(pid)
            if start is not None and start == start0:
                try:
                    os.killpg(pid, signal.SIGTERM)
                    signalled.append(e)
                    if e["name"] not in killed:
                        killed.append(e["name"])
                except OSError:
                    pass
        if signalled:
            time.sleep(min(self.grace, 2.0))
            for e in signalled:
                start = _proc_start(e["pid"])
                if start is not None and start == e.get("start"):
                    try:
                        os.killpg(e["pid"], signal.SIGKILL)
                    except OSError:
                        pass
        with self._lock:
            self._live = {f"old{i}": e for i, e in enumerate(keep)}
            self._save()
        return killed

    def spawn(self, job: Job) -> Handle:
        with self._lock:
            self._n += 1
            key = f"ladder-{self.tag}-{self._n:06d}"
        jobname = key
        env = dict(job.env)
        status, token = None, ""
        if self.submit and not job.local:
            status = job.log.with_name(f"{job.log.name}.{jobname}.status")
            for stale in (status, status.with_name(status.name + ".started")):
                stale.unlink(missing_ok=True)
            token = uuid.uuid4().hex
            inner = [sys.executable, "-m", "circt_flow.ladder", "--exec",
                     str(status), token, "--", *job.argv]
            e2 = _package_env(env)
            kv = [f"{k}={v}" for k, v in e2.items()
                  if k.startswith("CIRCT_FLOW_") or k == "PYTHONPATH"]
            line = shlex.join(["env", *kv, *inner])
            cmd = self.submit.format(
                cmd=line, cmdq=shlex.quote(line), name=jobname,
                log=shlex.quote(str(job.log)), cwd=shlex.quote(str(job.cwd)))
            argv = ["bash", "-c", cmd]
        else:
            argv = job.argv
        jn = jobname if status is not None else ""
        # An intent record goes down first (no pid yet): if the ladder is
        # killed between starting the job and completing the record, a later
        # run can still cancel the scheduler job by its name.
        with self._lock:
            self._live[key] = {"name": job.name, "pid": None, "start": None,
                               "host": _host_id(), "jobname": jn}
            self._save()
        fh = open(job.log, "ab")
        try:
            popen = subprocess.Popen(argv, cwd=job.cwd, env=env, stdout=fh,
                                     stderr=subprocess.STDOUT,
                                     start_new_session=True)
        except BaseException:
            with self._lock:
                self._live.pop(key, None)
                self._save()
            raise
        finally:
            fh.close()
        h = Handle(popen, job, self, status, token)
        h.jobname, h.key = jn, key
        try:
            with self._lock:
                self._live[key].update(pid=popen.pid,
                                       start=_proc_start(popen.pid))
                self._save()
        except BaseException:
            # A job that is not on record could never be cancelled or reaped:
            # stop it now and report the failure.
            try:
                os.killpg(popen.pid, signal.SIGKILL)
            except OSError:
                pass
            popen.wait()
            with self._lock:
                self._live.pop(key, None)
            raise
        return h

    def orphan(self, h: Handle) -> bool:
        """A submit command ended without its job's completion marker, so the
        job may still be running (and writing into its directory). Run the
        cancel template; with no cancel template (or a failing one) the job
        stays on record (processes.json) as an orphan and is reported again by
        the next run. True when cancel succeeded."""
        # The leader has been reaped, so its group id may be anyone's by now:
        # nothing is signalled here, only the scheduler job is cancelled.
        if self.cancel_tpl and h.jobname and self._run_cancel(h.jobname):
            self.forget(h)
            return True
        with self._lock:
            e = self._live.get(h.key)
            if e is not None:
                e["orphan"] = True
                self._save()
        return False

    def forget(self, h: Handle) -> None:
        with self._lock:
            self._live.pop(h.key, None)
            self._save()

    def kill(self, h: Handle) -> None:
        """Stop the job's process group: SIGTERM, then after the grace period
        SIGKILL to the whole group whether or not its leader has exited (the
        leader is not reaped until then, so the group id cannot belong to
        anything else), then check that nothing of the group is left. If
        something is, the record stays (marked unkillable) and the ladder
        stops with LadderStuck."""
        if h.popen.poll() is None:
            try:
                os.killpg(h.pgid, signal.SIGTERM)
            except OSError:
                pass
            _wait_exit_nowait(h.popen.pid, self.grace)
            try:
                os.killpg(h.pgid, signal.SIGKILL)
            except OSError:
                pass
            h.popen.wait()
            end = time.monotonic() + 10
            while _group_alive(h.pgid) and time.monotonic() < end:
                time.sleep(0.05)
            if _group_alive(h.pgid):
                self._run_cancel(h.jobname)
                with self._lock:
                    e = self._live.get(h.key)
                    if e is not None:
                        e["unkillable"] = True
                        self._save()
                raise LadderStuck(
                    f"closure ladder: processes of {h.job.name} (process "
                    f"group {h.pgid}) are still running after SIGKILL; not "
                    "continuing while they may write into the run directory. "
                    f"End them (for example `kill -9 -{h.pgid}`) and run "
                    "again, or use another output directory; the group is "
                    "recorded in processes.json and later runs refuse to "
                    "start while it is alive.")
        self._run_cancel(h.jobname)
        if h.job.kill_cmd:
            # `docker kill` fails until the daemon has created the container:
            # try again for a few seconds.
            for _ in range(12):
                r = subprocess.run(h.job.kill_cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                if r.returncode == 0:
                    break
                time.sleep(0.25)
        self.forget(h)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


_EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*[a-z][\w.]*(: .*)?$")


def ends_in_traceback(text: str) -> bool:
    """True when the text ends the way an uncaught Python exception ends: the
    last `Traceback (most recent call last):` is followed only by indented
    frame lines (and the `During handling...` chain notices) and one final
    unindented line of the form `Name` or `Name: message`, where Name has a
    lower-case letter (`ValueError`, `StopIteration`, `KeyboardInterrupt`;
    `REJECT: too slow` is not one). Anything after that is a script's own
    output. A heuristic: a multi-line message, or a script's own final line
    shaped like an exception, is not recognised; a check that wants an
    unmistakable crash should catch its own errors and exit with status 2."""
    i = text.rfind("Traceback (most recent call last):")
    if i < 0:
        return False
    lines = [l for l in text[i:].splitlines()[1:] if l.strip()]
    if not lines or not _EXC_LINE.match(lines[-1]):
        return False
    chain = ("During handling of the above exception",
             "The above exception was the direct cause")
    return all(l[0] in " \t" or l.startswith(chain) for l in lines[:-1])


def check_metrics(metrics: dict, checks: Sequence[Check]) -> tuple:
    """(verdict, reasons) of the built-in checks on a metrics dict. A number
    that is absent or not finite cannot be judged: unverified, never a pass."""
    rejected, missing = [], []
    for c in checks:
        v = metrics.get(c.key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(v):
            missing.append(f"{c.key} not reported")
            continue
        if c.min is not None and v < c.min:
            rejected.append(f"{c.key} {v:g} < {c.min:g}")
        if c.max is not None and v > c.max:
            rejected.append(f"{c.key} {v:g} > {c.max:g}")
    if rejected:
        return REJECTED, rejected
    if missing:
        return UNVERIFIED, missing
    return ACCEPTED, []


def combine(results: Sequence[tuple]) -> tuple:
    """One verdict from several checks: any rejection rejects (a definite
    failure outranks one that could not complete), else any unverified check
    makes the rung unverified; accepted only when every check passed."""
    reasons = [r for v, rs in results if v == REJECTED for r in rs]
    if reasons:
        return REJECTED, reasons
    reasons = [r for v, rs in results if v == UNVERIFIED for r in rs]
    if reasons:
        return UNVERIFIED, reasons
    return ACCEPTED, []


def decide(verdicts: Sequence[Optional[str]]) -> tuple:
    """The ladder's outcome from the rungs' verdicts in rung order (None: not
    yet known). ("pending", None) until the answer is fixed; ("accepted", i)
    for the lowest accepted rung once every rung below it is rejected;
    ("unverified" | "error", i) when the first rung that is not rejected could
    not be judged or failed (no higher rung can be accepted past it);
    ("none", None) when every rung is rejected. Depends on the order only, so
    a higher rung finishing first changes nothing."""
    for i, v in enumerate(verdicts):
        if v is None or v == CANCELLED:
            return "pending", None
        if v == REJECTED:
            continue
        return v, i
    return "none", None


# ---------------------------------------------------------------------------
# The backends' common face
# ---------------------------------------------------------------------------


class Backend:
    """What the ladder needs of a P&R implementation. `plan` returns the
    manifest a run in that directory would record; `place` / `rung` / `full`
    return the Job to run; `diff` lists what forbids restarting a rung from a
    placement."""

    supports_restart = True

    def prepare(self, spec: LadderSpec) -> None: ...
    def plan(self, d: Path, value) -> dict: ...
    def place(self, d: Path, value, place_dir: Path) -> Job: ...
    def rung(self, d: Path, value, place_dir: Path, i: int) -> Job: ...
    def full(self, d: Path, value, i: int) -> Job: ...
    def diff(self, placed: dict, plan: dict) -> list: ...
    def key(self, plan: dict) -> dict:
        """The part of a plan a placement depends on: what a rung may not
        change. Default: all of it."""
        return plan
    def metrics(self, d: Path) -> dict: ...
    def corner_job(self, d: Path, corner: CornerCheck) -> Job: ...
    def corner_result(self, d: Path, corner: CornerCheck) -> dict: ...


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class RungResult:
    index: int
    value: object
    verdict: Optional[str] = None
    reasons: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    seconds: float = 0.0
    reused: bool = False
    dir: str = ""

    def as_dict(self) -> dict:
        return {"index": self.index, "value": self.value,
                "verdict": self.verdict, "reasons": self.reasons,
                "metrics": self.metrics, "seconds": round(self.seconds, 1),
                "reused": self.reused, "dir": self.dir}


class Ladder:
    def __init__(self, spec: LadderSpec, backend: Backend, out: Path,
                 env: Optional[dict] = None, log=None):
        self.spec, self.be = spec, backend
        self.out = Path(out).resolve()
        self.base = self.out / "ladder"
        self.env = dict(os.environ if env is None else env)
        self.log = log or (lambda m: print(m, flush=True))
        self.runner: Optional[Runner] = None
        self.stop = threading.Event()        # the whole ladder is over
        self.rung_stop: list[threading.Event] = []
        self.results: list[RungResult] = []
        self.sem = threading.Semaphore(spec.slots)
        self.fatal: Optional[BaseException] = None
        self.lock = threading.Lock()
        self.done = threading.Condition(self.lock)

    # -- layout ------------------------------------------------------------

    def rung_dir(self, i: int) -> Path:
        return self.base / f"rung_{i}"

    @property
    def place_dir(self) -> Path:
        return self.base / "place"

    # -- the manifest guard ------------------------------------------------

    def _manifest_hash(self, m: dict) -> str:
        return _digest(m)

    def check_plans(self) -> dict:
        """Plan every rung before anything runs. A rung whose value changes
        what runs before the restart stage cannot start from the placement:
        refuse the ladder now. Returns the placement's plan."""
        spec, be = self.spec, self.be
        plans = []
        self.plans = plans
        for i, v in enumerate(spec.values):
            # Planned in a directory of its own: writing the inputs again
            # into an existing placement would give them new modification
            # times, and make would then redo the placement's stages.
            d = self.base / "plan" / f"rung_{i}"
            d.mkdir(parents=True, exist_ok=True)
            plans.append(be.plan(d, v))
        if be.supports_restart:
            for i, pl in enumerate(plans[1:], 1):
                bad = be.diff(plans[0], pl)
                if bad:
                    raise LadderError(
                        f"closure ladder: {spec.param}={spec.values[i]!r} cannot "
                        f"start from a placement made with "
                        f"{spec.param}={spec.values[0]!r}: it changes "
                        "something that acts before the restart stage "
                        "(or an unknown option counts as earlier):\n  "
                        + "\n  ".join(bad))
        return plans[0]

    # -- one rung ----------------------------------------------------------

    def _cached(self, i: int, plan_hash: str) -> Optional[RungResult]:
        f = self.rung_dir(i) / "rung_result.json"
        if not f.is_file():
            return None
        try:
            r = json.loads(f.read_text())
        except ValueError:
            return None
        # A record is valid for this rung only if everything that can change
        # its result is unchanged: its value, the whole plan of the rung (every
        # option of every stage, the inputs, the tool), the acceptance
        # definition with the content of the files it names, the placement.
        if (r.get("value") != self.spec.values[i]
                or r.get("accept") != self.accept_digest
                or r.get("format") != FORMAT
                or r.get("rung_plan") != _digest(self.plans[i])
                or r.get("placement") != plan_hash
                or r.get("verdict") not in (ACCEPTED, REJECTED)):
            return None
        return RungResult(i, r["value"], r["verdict"], r.get("reasons", []),
                          r.get("metrics", {}), r.get("seconds", 0.0), True,
                          str(self.rung_dir(i)))

    def _check_tasks(self, i: int, d: Path, metrics: dict, stop: threading.Event):
        """The acceptance checks of one rung, as (name, function) pairs; the
        slow ones start together with the quick one."""
        spec, be = self.spec, self.be
        tasks = [("metrics", lambda s: check_metrics(metrics, spec.accept.checks))]
        for c in spec.accept.corners:
            def corner(s, c=c):
                try:
                    job = be.corner_job(d, c)
                    job.log.unlink(missing_ok=True)
                    h = self.runner.spawn(job)
                    rc = h.wait(s)
                    if rc is None:
                        return CANCELLED, []
                    if rc != 0:
                        return UNVERIFIED, [f"corner {c.name}: OpenSTA run "
                                            f"exited {rc} (see {job.log})"]
                    got = be.corner_result(d, c)
                except Exception as e:      # the check could not be made
                    return UNVERIFIED, [f"corner {c.name}: {e}"]
                v, rs = check_metrics(got, c.checks)
                return v, [f"corner {c.name}: {r}" for r in rs]
            tasks.append((f"corner {c.name}", corner))
        if spec.accept.command:
            tasks.append(("command", lambda s: self._user_command(i, d, s)))
        return tasks

    def _user_command(self, i: int, d: Path, stop: threading.Event) -> tuple:
        spec = self.spec
        env = {**self.env, "CIRCT_FLOW_RUNG_DIR": str(d),
               "CIRCT_FLOW_RUNG_INDEX": str(i),
               "CIRCT_FLOW_LADDER_PARAM": spec.param,
               "CIRCT_FLOW_LADDER_VALUE": json.dumps(spec.values[i]),
               "CIRCT_FLOW_OUT": str(self.out),
               "CIRCT_FLOW_PNR_METRICS": str(d / "pnr_metrics.json")}
        job = Job(f"check rung {i}", list(spec.accept.command), env, d,
                  d / "check.log")
        try:
            h = self.runner.spawn(job)
        except Exception as e:
            return UNVERIFIED, [f"command could not start: {e}"]
        rc = h.wait(stop, spec.accept.timeout)
        if rc is None:
            if stop.is_set():
                return CANCELLED, []
            return UNVERIFIED, [f"command timed out after "
                                f"{spec.accept.timeout:g} s"]
        if rc == NO_MARKER:
            return UNVERIFIED, ["the submit command ended without the check's "
                                "completion marker (it must block until the "
                                "job has finished)"]
        if rc == 0:
            return ACCEPTED, []
        if rc == 1:
            try:
                tail = (d / "check.log").read_text(errors="replace")[-4000:]
            except OSError:
                tail = ""
            if ends_in_traceback(tail):
                # exit status 1 is also what an uncaught Python exception
                # gives: a crash is not a verdict
                return UNVERIFIED, ["command exited 1 and check.log ends in a "
                                    "Python traceback: a crash, not a verdict"]
            return REJECTED, ["command exited 1"]
        return UNVERIFIED, [f"command exited {rc} (not 0 or 1: the check did "
                            "not complete)"]

    def _judge(self, i: int, d: Path, metrics: dict,
               stop: threading.Event) -> tuple:
        """Run the checks side by side; the first rejection stops the others."""
        results: dict[str, tuple] = {}
        tasks = self._check_tasks(i, d, metrics, stop)
        sub_stop = threading.Event()

        def run(name, fn):
            try:
                r = fn(sub_stop)
            except LadderStuck as e:
                self.fatal = e
                r = (UNVERIFIED, [f"{name}: {e}"])
            except Exception as e:
                r = (UNVERIFIED, [f"{name}: {type(e).__name__}: {e}"])
            results[name] = r
            if r[0] == REJECTED:
                sub_stop.set()
        threads = [threading.Thread(target=run, args=t, daemon=True)
                   for t in tasks]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            if stop.is_set():
                sub_stop.set()
            time.sleep(0.05)
        if stop.is_set():
            return CANCELLED, []
        missing = [n for n, _ in tasks if n not in results]
        if missing:       # a check that left no verdict cannot pass
            return UNVERIFIED, [f"no verdict from {', '.join(missing)}"]
        done = [r for r in results.values() if r[0] != CANCELLED]
        return combine(done)

    def _run_rung(self, i: int, plan_hash: str, restart: bool) -> RungResult:
        """One rung; any exception becomes the rung's ERROR verdict, so that
        the outcome rule always gets an answer from every rung."""
        try:
            return self._run_rung_inner(i, plan_hash, restart)
        except LadderStuck:
            raise
        except Exception as e:
            self.started[i].set()
            return RungResult(i, self.spec.values[i], ERROR,
                              [f"{type(e).__name__}: {e}"],
                              dir=str(self.rung_dir(i)))

    def _run_rung_inner(self, i: int, plan_hash: str, restart: bool
                        ) -> RungResult:
        spec, be = self.spec, self.be
        v, d = spec.values[i], self.rung_dir(i)
        res = RungResult(i, v, dir=str(d))
        cached = self._cached(i, plan_hash)
        if cached:
            self.started[i].set()
            return cached
        stop = self.rung_stop[i]
        # Take a slot in rung order, so that a lower rung never waits for a
        # higher one when `jobs` is smaller than the number of rungs.
        while i and not self.started[i - 1].wait(0.1):
            if stop.is_set():
                break
        self.sem.acquire()
        try:
            cancelled = stop.is_set() or self.stop.is_set()
            if not cancelled:
                self.log(f"[closure ladder] rung {i} ({spec.param}={v}) started")
            self.started[i].set()
            if cancelled:
                res.verdict = CANCELLED
                return res
            t0 = time.monotonic()
            # Nothing a check reads may be left from an earlier run: the rung
            # directory is emptied, so that what this run produces is judged,
            # or nothing. (The placement and the plans are elsewhere.)
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            job = (be.rung(d, v, self.place_dir, i) if restart
                   else be.full(d, v, i))
            try:
                h = self.runner.spawn(job)
            except Exception as e:
                res.verdict, res.reasons = ERROR, [f"cannot start: {e}"]
                return res
            rc = h.wait(stop)
            if rc is None:
                res.verdict = CANCELLED
                return res
            if rc == NO_MARKER:
                res.verdict, res.reasons = ERROR, [
                    "the submit command ended without the job's completion "
                    f"marker (it must block until the job has finished; see "
                    f"{job.log})"]
                res.seconds = time.monotonic() - t0
                return res
            if rc != 0:
                res.verdict, res.reasons = ERROR, [
                    f"P&R run exited {rc} (see {job.log})"]
                res.seconds = time.monotonic() - t0
                return res
            try:
                res.metrics = be.metrics(d)
            except Exception as e:
                res.verdict, res.reasons = ERROR, [f"no result: {e}"]
                res.seconds = time.monotonic() - t0
                return res
            res.verdict, res.reasons = self._judge(i, d, res.metrics, stop)
            res.seconds = time.monotonic() - t0
            if res.verdict in (ACCEPTED, REJECTED):
                _write_atomic(d / "rung_result.json", json.dumps(
                    {"value": v, "verdict": res.verdict,
                     "reasons": res.reasons, "metrics": res.metrics,
                     "seconds": res.seconds, "accept": self.accept_digest,
                     "format": FORMAT, "rung_plan": _digest(self.plans[i]),
                     "placement": plan_hash}, indent=1, sort_keys=True))
            return res
        finally:
            self.sem.release()

    # -- the placement -----------------------------------------------------

    def _place(self, plan: dict) -> tuple:
        """Make (or reuse) the placement. Returns (seconds, reused)."""
        d, be = self.place_dir, self.be
        marker = d / "PLACED"
        h = self._manifest_hash({"format": FORMAT, "key": be.key(plan)})
        if marker.is_file():
            if marker.read_text().strip() == h:
                self.log("[closure ladder] placement reused (manifest matches)")
                return 0.0, True
            raise LadderError(
                f"closure ladder: {d} holds a placement made from different inputs "
                "or options (its manifest differs from this run's). A "
                "restart is only allowed from a placement with the same "
                "earlier-stage inputs; remove the directory to place again.")
        t0 = time.monotonic()
        import shutil
        shutil.rmtree(d, ignore_errors=True)     # no PLACED: a half-made placement
        d.mkdir(parents=True, exist_ok=True)
        job = be.place(d, self.spec.values[0], d)
        self.log("[closure ladder] placing once")
        hd = self.runner.spawn(job)
        rc = hd.wait(self.stop)
        if rc is None:
            raise LadderFailed("closure ladder: interrupted during placement",
                               EXIT_INTERRUPTED)
        if rc == NO_MARKER:
            raise LadderFailed("closure ladder: the submit command ended without the "
                               "placement job's completion marker (it must "
                               f"block until the job has finished; see "
                               f"{job.log})", EXIT_RUNG_FAILED)
        if rc != 0:
            raise LadderFailed(f"closure ladder: the placement run exited {rc} "
                               f"(see {job.log})", EXIT_RUNG_FAILED)
        _write_atomic(marker, h + "\n")
        return time.monotonic() - t0, False

    # -- the whole ladder --------------------------------------------------

    def run(self, clean=(), finalize=None) -> dict:
        """Run the ladder under the directory's lock. `clean`: paths removed
        once the lock is held (the previous run's results); `finalize(report)`
        runs before the lock is released (the caller's result files)."""
        self.base.mkdir(parents=True, exist_ok=True)
        lockf = open(self.base / "ladder.lock", "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lockf.close()
            raise LadderBusy(f"closure ladder: {self.base} is in use by another run")
        try:
            return self._run_locked(clean, finalize)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()

    def _run_locked(self, clean, finalize) -> dict:
        spec, be = self.spec, self.be
        result = self.base / "RESULT.json"
        # Nothing of an earlier run may pass for this run's result.
        for p in (result, *map(Path, clean)):
            if p.is_symlink() or p.exists():
                p.unlink()
        try:
            self.accept_digest = spec.accept.digest()
            self.runner = Runner(self.base, spec.submit, spec.cancel)
            reaped = self.runner.reap_stale()
        except LadderError as e:       # refused before anything ran
            _write_atomic(result, json.dumps(
                {"state": "refused", "accepted": None,
                 "message": f"{type(e).__name__}: {e}", "param": spec.param,
                 "mode": spec.mode, "rungs": []},
                indent=1, sort_keys=True) + "\n")
            raise
        for n in reaped:
            self.log(f"[closure ladder] stopped a leftover process of an "
                     f"interrupted run: {n}")
        for n in self.runner.unresolved:
            self.log(f"[closure ladder] WARNING: leftover job not stopped: {n}")
        if spec.fresh:
            import shutil
            for sub in ["place", "plan", *(f"rung_{i}" for i in
                                           range(len(spec.values)))]:
                shutil.rmtree(self.base / sub, ignore_errors=True)
        self.rung_stop = [threading.Event() for _ in spec.values]
        self.started = [threading.Event() for _ in spec.values]
        self.results = [RungResult(i, v, dir=str(self.rung_dir(i)))
                        for i, v in enumerate(spec.values)]
        restart = be.supports_restart
        old = {}
        if threading.current_thread() is threading.main_thread():
            def onsig(sig, frm):
                self.stop.set()
                for e in self.rung_stop:
                    e.set()
            old = {s: signal.signal(s, onsig)
                   for s in (signal.SIGINT, signal.SIGTERM)}
        t_start = time.monotonic()
        try:
            plan = self.check_plans()
            plan_hash = self._manifest_hash({"format": FORMAT,
                                             "key": be.key(plan)})
            if restart:
                place_s, reused = self._place(plan)
            else:
                place_s, reused = 0.0, False
                self.log("[closure ladder] the P&R command does not declare stage "
                         "restart for this parameter: every rung runs in full")
            outcome = self._rungs(plan_hash, restart)
        except BaseException as e:
            # A run that ends in an exception still says so in RESULT.json.
            _write_atomic(result, json.dumps(
                {"state": state_of(e),
                 "accepted": None, "message": f"{type(e).__name__}: {e}",
                 "param": spec.param, "mode": spec.mode,
                 "rungs": [r.as_dict() for r in self.results]},
                indent=1, sort_keys=True, default=str) + "\n")
            raise
        finally:
            self.stop.set()
            for e in self.rung_stop:
                e.set()
            for s, f in old.items():
                signal.signal(s, f)
        wall = time.monotonic() - t_start
        state, idx = outcome if outcome else ("interrupted", None)
        report = {"state": state,
                  "accepted": idx if state == ACCEPTED else None,
                  "decided_at": idx,
                  "value": spec.values[idx] if state == ACCEPTED else None,
                  "param": spec.param, "mode": spec.mode,
                  "restart": restart, "wall_seconds": round(wall, 1),
                  "place_seconds": round(place_s, 1) if restart else None,
                  "rungs": [r.as_dict() for r in self.results]}
        _write_atomic(result, json.dumps(report, indent=1, sort_keys=True,
                                         default=str) + "\n")
        self._print(report)
        if finalize is not None:
            finalize(report)
        return report

    def _rungs(self, plan_hash: str, restart: bool):
        """Run the rungs; returns decide()'s outcome, or None if interrupted."""
        spec, n = self.spec, len(self.spec.values)
        verdicts: list[Optional[str]] = [None] * n

        def finish(i: int, r: RungResult):
            with self.done:
                self.results[i] = r
                verdicts[i] = r.verdict
                self.done.notify_all()
            if r.verdict != CANCELLED:
                self.log(f"[closure ladder] rung {i} ({spec.param}={r.value}) "
                         f"{r.verdict.upper()}"
                         + (": " + "; ".join(r.reasons) if r.reasons else "")
                         + (" (reused)" if r.reused else ""))

        if spec.mode == "sequential":
            for i in range(n):
                finish(i, self._run_rung(i, plan_hash, restart))
                if self.fatal is not None:
                    raise self.fatal
                if self.stop.is_set():
                    return None
                if decide(verdicts)[0] != "pending":
                    break
            for i, v in enumerate(verdicts):
                if v is None:
                    self.results[i].verdict = "not run"
            return decide(verdicts)
        def worker(i):
            try:
                r = self._run_rung(i, plan_hash, restart)
            except LadderStuck as e:        # stop everything, then refuse
                self.fatal = e
                for ev in self.rung_stop:
                    ev.set()
                self.started[i].set()
                r = RungResult(i, spec.values[i], ERROR, [str(e)],
                               dir=str(self.rung_dir(i)))
            except BaseException as e:      # a rung must always give an answer
                self.started[i].set()
                r = RungResult(i, spec.values[i], ERROR,
                               [f"{type(e).__name__}: {e}"],
                               dir=str(self.rung_dir(i)))
            finish(i, r)
        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(n)]
        for t in threads:
            t.start()
        with self.done:
            while True:
                st, k = decide(verdicts)
                if st != "pending" or self.stop.is_set():
                    break
                self.done.wait(timeout=0.5)
        interrupted = self.stop.is_set() and st == "pending"
        # The outcome is fixed: the rungs above it are not needed.
        for j in range(n):
            if k is None or j > k or interrupted:
                self.rung_stop[j].set()
        for t in threads:
            t.join()
        if self.fatal is not None:
            raise self.fatal
        if interrupted:
            return None
        for j in range(n):
            if verdicts[j] == CANCELLED:
                self.results[j].verdict = "not needed"
        return decide(verdicts)

    def _print(self, rep: dict) -> None:
        spec = self.spec
        for r in rep["rungs"]:
            self.log(f"[closure ladder]   rung {r['index']}  {spec.param}="
                     f"{r['value']}  {str(r['verdict']).upper():10}"
                     + (f" {r['seconds']:.0f} s" if r["seconds"] else "")
                     + ("  " + "; ".join(r["reasons"]) if r["reasons"] else ""))
        if rep["state"] == ACCEPTED:
            self.log(f"[closure ladder] ACCEPTED rung {rep['accepted']} "
                     f"({spec.param}={rep['value']}), {rep['mode']}, "
                     f"{rep['wall_seconds']:.0f} s")
        else:
            self.log(f"[closure ladder] NOT ACCEPTED ({rep['state']}), "
                     f"{rep['mode']}, {rep['wall_seconds']:.0f} s")


def state_of(e: BaseException) -> str:
    """The RESULT.json / pnr_metrics.json state of a run that ended in an
    exception: refused (a declaration or manifest the ladder will not run),
    interrupted, or error."""
    if isinstance(e, LadderError):
        return "refused"
    if isinstance(e, LadderFailed) and e.code == EXIT_INTERRUPTED:
        return "interrupted"
    return "error"


def exit_code(state: str) -> int:
    return {ACCEPTED: EXIT_ACCEPTED, "none": EXIT_NONE_ACCEPTED,
            UNVERIFIED: EXIT_UNVERIFIED, ERROR: EXIT_RUNG_FAILED,
            "interrupted": EXIT_INTERRUPTED}[state]


# ---------------------------------------------------------------------------
# Backend 1: the ORFS stage (orfs_pnr.py)
# ---------------------------------------------------------------------------

# The first ORFS stage a `pnr:` key acts in, for the keys that act at CTS or
# later. A `set.<VARIABLE>` parameter is looked up in orfs_pnr's table.
# setup_margin is not here: ORFS's floorplan stage already repairs setup to
# it, so rungs of it do not share a placement.
_ORFS_PARAM_STAGE = {
    "hold_margin": "cts", "cts_args": "cts", "cts_buffers": "cts",
    "cts_cluster_size": "cts", "cts_cluster_diameter": "cts",
}
_STOP_TARGET = {"cts": "place", "route": "cts"}
# The `pnr:` keys the ORFS stage reads (stages.orfs_argv). Any other key as a
# ladder parameter would change nothing: every rung would run the same flow.
_ORFS_PNR_KEYS = {
    "platform", "clock_port", "clock_period", "clock_latency", "utilization",
    "place_density", "set", "tie_hi", "tie_lo", "vt_swap", "dont_use",
    "cts_buffers", "cts_cluster_size", "cts_cluster_diameter", "hold_margin",
    "setup_margin", "cts_args"}


def _package_env(env: dict) -> dict:
    """The environment of a job that runs `python -m circt_flow.<module>`:
    this package's parent directory on PYTHONPATH."""
    root = str(Path(__file__).resolve().parent.parent)
    e = dict(env)
    e["PYTHONPATH"] = root + (os.pathsep + e["PYTHONPATH"]
                              if e.get("PYTHONPATH") else "")
    return e


def orfs_from_stage(spec: LadderSpec) -> str:
    from . import orfs_pnr as pnr_mod
    if not spec.param.startswith("set.") and spec.param not in _ORFS_PNR_KEYS:
        raise LadderError(
            f"ladder.param: {spec.param!r} is not a `pnr:` key the ORFS stage "
            "reads, so the rungs would all be the same run; use one of "
            f"{', '.join(sorted(_ORFS_PNR_KEYS))} or set.<VARIABLE>")
    stage = spec.from_stage
    if stage is None:
        if spec.param.startswith("set."):
            stage = pnr_mod.stage_of_var(spec.param[4:], "0", {})
        else:
            stage = _ORFS_PARAM_STAGE.get(spec.param)
        if stage not in pnr_mod.RESTART_STAGES:
            raise LadderError(
                f"closure ladder: {spec.param!r} is not known to act at the clock "
                "tree stage or later in ORFS; if it does, say so with "
                "from_stage: cts (or route). A parameter that acts earlier "
                "(utilization, setup_margin, ...) cannot be laddered from "
                "one placement.")
    if stage not in pnr_mod.RESTART_STAGES:
        raise LadderError(f"ladder.from_stage: for ORFS one of "
                          f"{', '.join(pnr_mod.RESTART_STAGES)}, got {stage!r}")
    return stage


def _tcl_word(s) -> str:
    """A string as one Tcl word, whatever it contains."""
    return "".join("\\" + c if c in " \t\n$[]{}\"\\;" else c for c in str(s))


class OrfsBackend(Backend):
    def __init__(self, cfg, spec: LadderSpec):
        self.cfg, self.spec = cfg, spec
        self.stage = orfs_from_stage(spec)
        self.platform = str(cfg.design.pnr.get("platform", "asap7"))

    def _argv(self, d: Path, value) -> list:
        from .stages import orfs_argv
        p = apply_param(self.cfg.design.pnr, self.spec.param, value)
        return orfs_argv(self.cfg, p, d)

    def _job(self, d: Path, value, extra: list, name: str) -> Job:
        d.mkdir(parents=True, exist_ok=True)
        return Job(name, [sys.executable, "-m", "circt_flow.orfs_pnr",
                          *self._argv(d, value), *extra],
                   _package_env(dict(os.environ)), d, d / "ladder_run.log")

    def plan(self, d: Path, value) -> dict:
        from . import orfs_pnr as pnr_mod
        rc = pnr_mod.main([*map(str, self._argv(d, value)), "--plan"])
        if rc != 0:
            raise LadderError(f"closure ladder: could not plan the run in {d}")
        return json.loads((d / "plan_manifest.json").read_text())

    def place(self, d: Path, value, place_dir: Path) -> Job:
        return self._job(d, value, ["--target", _STOP_TARGET[self.stage]],
                         "placement")

    def rung(self, d: Path, value, place_dir: Path, i: int) -> Job:
        return self._job(d, value, ["--from-dir", str(place_dir),
                                    "--from-stage", self.stage], f"rung {i}")

    def full(self, d: Path, value, i: int) -> Job:
        return self._job(d, value, [], f"rung {i}")

    def diff(self, placed: dict, plan: dict) -> list:
        from . import orfs_pnr as pnr_mod
        return pnr_mod.restart_diff(placed, plan, self.stage)

    def key(self, plan: dict) -> dict:
        from . import orfs_pnr as pnr_mod
        return pnr_mod.restart_key(plan, self.stage)

    def metrics(self, d: Path) -> dict:
        m = json.loads((d / "pnr_metrics.json").read_text())
        if m.get("status") != "ok":
            raise RuntimeError(f"status {m.get('status')!r}")
        return m

    def corner_job(self, d: Path, corner: CornerCheck) -> Job:
        """OpenSTA on the routed result with the corner's libraries (in the
        ORFS image, or $OPENSTA), as a job of the ladder's runner: it has a
        name, is stopped with the rung, and writes corner_<name>.log."""
        from . import orfs_pnr as pnr_mod
        from .sta import STA_BIN
        top = self.cfg.design.top_module
        res = d / "work" / "results" / self.platform / top / "base"
        files = {k: res / f"6_final.{k}" for k in ("v", "sdc", "spef")}
        for k, f in files.items():
            if not f.is_file():
                raise FileNotFoundError(f"{f.name} missing: no routed result")
        tcl = d / f"corner_{corner.name}.tcl"
        tcl.write_text(
            "".join(f"read_liberty {_tcl_word(l)}\n" for l in corner.libs)
            + f"read_verilog {_tcl_word(files['v'])}\n"
            f"link_design {_tcl_word(top)}\n"
            f"read_sdc {_tcl_word(files['sdc'])}\n"
            f"read_spef {_tcl_word(files['spef'])}\n"
            'puts "CIRCTFLOW-BEGIN"\n'
            "report_worst_slack -max -digits 6\n"
            "report_worst_slack -min -digits 6\n"
            'puts "CIRCTFLOW-END"\nexit\n')
        log = d / f"corner_{corner.name}.log"
        local = os.environ.get("OPENSTA")
        kill = None
        if local:
            cmd = [local, "-no_init", "-no_splash", "-exit", str(tcl)]
        else:
            image = os.environ.get("ORFS_IMAGE", pnr_mod.DEFAULT_IMAGE)
            mounts = {str(d.resolve())}
            pdir = self.cfg.design.pack_pnr.get("orfs_platform_dir")
            if pdir:
                mounts.add(str(Path(pdir).resolve()))
            for l in corner.libs:      # libraries outside the image
                if not l.startswith("/OpenROAD-flow-scripts/"):
                    mounts.add(str(Path(l).resolve().parent))
            for m in mounts:
                if ":" in m or "," in m:
                    raise LadderError(
                        f"closure ladder: {m!r} contains ':' or ',' and cannot "
                        "be mounted into the OpenSTA container")
            name = f"sta-{uuid.uuid4().hex[:12]}"
            cmd = ["docker", "run", "--rm", "--name", name,
                   "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                   *[a for m in sorted(mounts) for a in ("-v", f"{m}:{m}")],
                   image, STA_BIN, "-no_init", "-no_splash", "-exit", str(tcl)]
            kill = ["docker", "kill", name]
        return Job(f"corner {corner.name}", cmd, dict(os.environ), d, log,
                   local=True, kill_cmd=kill)

    def corner_result(self, d: Path, corner: CornerCheck) -> dict:
        log = d / f"corner_{corner.name}.log"
        text = log.read_text(errors="replace")
        body = text.split("CIRCTFLOW-BEGIN", 1)[-1]
        m = re.search(r"worst slack max (\S+)", body)
        h = re.search(r"worst slack min (\S+)", body)
        if "CIRCTFLOW-END" not in text or not (m and h):
            raise RuntimeError(f"OpenSTA did not finish (see {log.name})")
        return {"setup_ws": float(m.group(1)), "hold_ws": float(h.group(1))}


# ---------------------------------------------------------------------------
# Backend 2: a tech pack's own P&R command
# ---------------------------------------------------------------------------


def _with_pnr(cfg, pnr: dict):
    """The flow config with its `pnr:` section replaced (the rung's copy)."""
    import dataclasses
    if dataclasses.is_dataclass(cfg.design):
        design = dataclasses.replace(cfg.design, pnr=pnr)
    else:
        design = copy.copy(cfg.design)
        design.pnr = pnr
    if dataclasses.is_dataclass(cfg):
        return dataclasses.replace(cfg, design=design)
    c = copy.copy(cfg)
    c.design = design
    return c


class CommandBackend(Backend):
    """The pack's `pnr.command`, run once per stage split with

        CIRCT_FLOW_STOP_AFTER=<stage>                    (the placement run)
        CIRCT_FLOW_START_AT=<stage> CIRCT_FLOW_START_FROM=<dir>   (a rung)

    when the pack declares `stage_restart: [<stage>, ...]` (the stages the
    command can stop after and start at, in order) and `from_stage` is one of
    them. Without that every rung is a full run."""

    def __init__(self, cfg, spec: LadderSpec):
        self.cfg, self.spec = cfg, spec
        self._jobs: dict = {}
        stages = cfg.design.pack_pnr.get("stage_restart") or []
        st = spec.from_stage
        self.supports_restart = bool(st and st in stages
                                     and stages.index(st) >= 1)
        self.stop_stage = stages[stages.index(st) - 1] \
            if self.supports_restart else None
        if st is None and stages:
            raise LadderError(
                "ladder.from_stage: required with a pack P&R command: the "
                "first stage the parameter acts in (one of "
                + ", ".join(stages) + ")")

    def _env(self, base: dict, value, i: Optional[int]) -> dict:
        e = dict(base)
        e["CIRCT_FLOW_LADDER_PARAM"] = self.spec.param
        e["CIRCT_FLOW_LADDER_VALUE"] = json.dumps(value)
        if i is not None:
            e["CIRCT_FLOW_LADDER_RUNG"] = str(i)
        return e

    def _job(self, d: Path, value, i, extra_env: dict, name: str) -> Job:
        from .stages import pack_command
        # The command sees the rung's value in its `pnr:` section as well
        # (and so in whatever the flow derives from it), not only in
        # CIRCT_FLOW_LADDER_VALUE.
        cmd, env = pack_command(
            _with_pnr(self.cfg, apply_param(self.cfg.design.pnr,
                                            self.spec.param, value)), d)
        (d / "pnr_metrics.json").unlink(missing_ok=True)
        env = self._env(env, value, i)
        env.update(extra_env)
        job = Job(name, cmd, env, Path(self.cfg.design.pack_root).resolve(),
                  d / "pnr.log")
        self._jobs[d] = job
        return job

    def plan(self, d: Path, value) -> dict:
        from .stages import pack_command
        cmd, env = pack_command(
            _with_pnr(self.cfg, apply_param(self.cfg.design.pnr,
                                            self.spec.param, value)), d)
        top = self.cfg.design.top_module
        # The environment the flow itself builds for the command (the tech
        # pack's wire RC files, the recipe variables): the values, and the
        # content of the files they name. The user's own environment is not
        # part of it.
        here = str(Path(d).resolve())
        flow_env = {k: v.replace(here, "<dir>") for k, v in env.items()
                    if os.environ.get(k) != v}
        flow_env_files = {k: _path_sha(Path(env[k])) for k in flow_env
                          if env[k] and os.path.exists(env[k])}
        p = apply_param(self.cfg.design.pnr, self.spec.param, None)
        words = cmd[:-len(["--netlist", "", "--sdc", "", "--top", "",
                           "--out", "", "--pack", ""])]
        root = Path(self.cfg.design.pack_root)

        def as_path(w):
            q = Path(w)
            return q if q.is_absolute() else root / q
        # What is covered: the whole command line, the content of every word
        # of it that is a file (the script), the netlist and SDC it is given,
        # every `pnr:` option, and the files and directories the pack lists in
        # `stage_inputs` (its data). Nothing else the command reads is known.
        extra = {str(x): _path_sha(Path(x)) for x in
                 (self.cfg.design.pack_pnr.get("stage_inputs") or [])}
        return {"version": FORMAT,
                "flow_env": {k: v for k, v in flow_env.items()
                             if k not in flow_env_files},
                "flow_env_files": flow_env_files,
                "tool": {"command": words,
                         "command_files": {w: _file_sha(as_path(w))
                                           for w in words
                                           if as_path(w).is_file()},
                         "stage_inputs": extra},
                "inputs": {"netlist": _file_sha(d / f"{top}.v"),
                           "sdc": _file_sha(d / "constraint.sdc")},
                # Every option counts as acting before the restart stage:
                # this backend cannot tell which ones do not.
                "options": {"all": json.loads(json.dumps(p, default=str))},
                "from_stage": self.spec.from_stage}

    def place(self, d: Path, value, place_dir: Path) -> Job:
        return self._job(d, value, None,
                         {"CIRCT_FLOW_STOP_AFTER": self.stop_stage},
                         "placement")

    def rung(self, d: Path, value, place_dir: Path, i: int) -> Job:
        return self._job(d, value, i, {
            "CIRCT_FLOW_START_AT": self.spec.from_stage,
            "CIRCT_FLOW_START_FROM": str(place_dir)}, f"rung {i}")

    def full(self, d: Path, value, i: int) -> Job:
        return self._job(d, value, i, {}, f"rung {i}")

    def diff(self, placed: dict, plan: dict) -> list:
        return [] if placed == plan else ["the P&R command's inputs differ"]

    def metrics(self, d: Path) -> dict:
        """After the rung's command: the flow's own finishing step for a pack
        command (stages.pack_finish), then its metrics."""
        from .stages import pack_finish
        job = self._jobs.get(d)
        f = pack_finish(self.cfg, d, 0, job.argv if job else [],
                        job.log if job else None)
        return json.loads(Path(f).read_text())

    def corner_job(self, d: Path, corner: CornerCheck) -> Job:
        raise RuntimeError("accept.corners needs the ORFS backend; give the "
                           "pack's own corner check as accept.command")

    def corner_result(self, d: Path, corner: CornerCheck) -> dict:
        raise RuntimeError("accept.corners needs the ORFS backend")


def _file_sha(p: Path) -> Optional[str]:
    """Content hash of a file; None when it does not exist. A file that
    exists and cannot be read is an error: it cannot be part of a key."""
    from . import orfs_pnr as pnr_mod
    if not os.path.lexists(p):
        return None
    try:
        return pnr_mod._sha(Path(p))
    except pnr_mod.HashError as e:
        raise LadderError(f"closure ladder: {e}")


def _path_sha(p: Path) -> Optional[str]:
    """Content hash of a file, or of a directory tree (see orfs_pnr._tree_sha:
    links are followed, an unreadable file or a broken link is an error);
    None when it does not exist."""
    from . import orfs_pnr as pnr_mod
    p = Path(p)
    if not p.is_dir():
        return _file_sha(p)
    try:
        return pnr_mod._tree_sha(p)
    except pnr_mod.HashError as e:
        raise LadderError(f"closure ladder: {e}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def preflight(spec: LadderSpec, backend: "Backend", cfg) -> None:
    """What can be known before a placement is made: refuse a ladder whose
    acceptance cannot run, instead of finding out after every rung."""
    import shutil
    if spec.accept.corners and not isinstance(backend, OrfsBackend):
        raise LadderError(
            "closure ladder: accept.corners needs the built-in ORFS stage; "
            "with a pack's own P&R command give its corner check as "
            "accept.command")
    for c in spec.accept.corners:
        for lib in c.libs:
            if not lib.startswith("/OpenROAD-flow-scripts/") \
                    and not os.path.exists(lib):
                raise LadderError(f"closure ladder: accept.corners.{c.name}: "
                                  f"{lib} does not exist")
    cmd = spec.accept.command
    if cmd:
        exe = cmd[0]
        found = (exe if os.sep in exe else shutil.which(exe))
        if not found or not os.path.isfile(found) \
                or not os.access(found, os.X_OK):
            raise LadderError(
                f"closure ladder: accept.command {exe!r} is not an executable "
                "file (a word that starts with ./ or ../ is relative to the "
                "flow config; a bare name is looked up on PATH)")


def _resolve_command(spec: LadderSpec, base: Optional[Path]) -> LadderSpec:
    """Relative paths in the acceptance are relative to the flow config's
    directory, not to the directory the flow is started from: a command word
    or a corner library that starts with ./ or ../, or that names an existing
    file or directory there, becomes an absolute path. Other words (`python3`)
    are left alone."""
    if base is None or not (spec.accept.command or spec.accept.corners):
        return spec

    def fix(w: str, always: bool) -> str:
        if os.path.isabs(w):
            return w
        try:                 # a long inline word (`bash -c "<script>"`) is no path
            if always and w.startswith(("./", "../")) or (base / w).exists():
                return str((base / w).resolve())
        except (OSError, ValueError):
            pass
        return w
    words = (tuple(fix(w, True) for w in spec.accept.command)
             if spec.accept.command else None)
    corners = tuple(CornerCheck(c.name, tuple(fix(l, False) for l in c.libs),
                                c.checks) for c in spec.accept.corners)
    acc = Accept(spec.accept.checks, corners, words, spec.accept.timeout)
    return LadderSpec(**{**spec.__dict__, "accept": acc})


def run_flow(cfg, mode: Optional[str] = None, jobs: Optional[int] = None,
             log=None, fresh: bool = False) -> Path:
    """Run the ladder the flow config's `pnr.ladder` declares on the flow's
    netlist. Returns <out>/pnr/pnr_metrics.json of the accepted rung; raises
    LadderFailed (with the exit code) when no rung was accepted."""
    spec = parse_ladder(cfg.design.pnr["ladder"])
    over = {}
    if mode:
        over["mode"] = mode
    if jobs is not None:
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
            raise LadderError("closure ladder: --jobs must be an integer >= 1")
        over["jobs"] = jobs
    if fresh:
        over["fresh"] = True
    submit = os.environ.get("CIRCT_FLOW_LADDER_SUBMIT")
    if submit:
        check_template(submit, SUBMIT_KEYS, "CIRCT_FLOW_LADDER_SUBMIT",
                       (("cmd", "cmdq"),))
        over["submit"] = submit
    cancel = os.environ.get("CIRCT_FLOW_LADDER_CANCEL")
    if cancel:
        check_template(cancel, CANCEL_KEYS, "CIRCT_FLOW_LADDER_CANCEL", ("name",))
        over["cancel"] = cancel
    if over:
        spec = LadderSpec(**{**spec.__dict__, **over})
    if spec.cancel and not spec.submit:
        raise LadderError("closure ladder: `cancel` needs `submit` (a local "
                          "job is stopped by signalling its process group)")
    spec = _resolve_command(spec, getattr(cfg.design, "config_dir", None))
    backend = (CommandBackend if "command" in cfg.design.pack_pnr
               else OrfsBackend)(cfg, spec)
    out = Path(cfg.pnr_dir).resolve()        # every path below is absolute
    out.mkdir(parents=True, exist_ok=True)
    preflight(spec, backend, cfg)
    # The result of an earlier run is not this run's: the link and the
    # metrics are removed once the directory's lock is held and written (from
    # the metrics the rung was judged on) only for an accepted rung.
    final = out / "pnr_metrics.json"
    link = out / "accepted"
    lad = Ladder(spec, backend, out, log=log)

    def finalize(rep):
        if rep["state"] != ACCEPTED:
            _write_atomic(final, json.dumps(
                {"status": f"closure ladder: {rep['state']}"}, indent=2) + "\n")
            return
        # The judged metrics, not a re-read of a file an orphan could have
        # written to since (same format as the backends write).
        _write_atomic(final, json.dumps(
            rep["rungs"][rep["accepted"]]["metrics"], indent=2,
            sort_keys=True) + "\n")
        link.symlink_to(os.path.relpath(lad.rung_dir(rep["accepted"]), out))

    try:
        rep = lad.run(clean=(link, final), finalize=finalize)
    except LadderBusy:
        raise                       # not our directory: touch nothing
    except Exception as e:
        _write_atomic(final, json.dumps(
            {"status": f"closure ladder: {state_of(e)}"}, indent=2) + "\n")
        raise
    if rep["state"] != ACCEPTED:
        raise LadderFailed(
            f"closure ladder: no rung accepted ({rep['state']}); see "
            f"{out / 'ladder' / 'RESULT.json'}", exit_code(rep["state"]))
    return final


def main(argv: Optional[list] = None) -> int:
    """python3 -m circt_flow.ladder <flow.yaml> <out_dir> [--mode M] [--jobs N]

    Run the flow config's ladder on the netlist an earlier flow run left in
    <out_dir> (1_synth/<top>_openroad.v), without running synthesis again."""
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--exec"]:     # the launcher of a job behind a submit command
        i = argv.index("--")
        return _exec(argv[1], argv[2], argv[i + 1:])
    from .config import FlowConfig
    ap = argparse.ArgumentParser(prog="python3 -m circt_flow.ladder",
                                 description=main.__doc__)
    ap.add_argument("config")
    ap.add_argument("out_dir")
    ap.add_argument("--mode", choices=("parallel", "sequential"))
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--fresh", action="store_true",
                    help="discard an earlier placement and rung results")
    a = ap.parse_args(argv)
    try:
        cfg = FlowConfig.load(a.config, a.out_dir)
        if not (cfg.design.pnr or {}).get("ladder"):
            print("ERROR: the flow config has no pnr.ladder", file=sys.stderr)
            return 1
        if not cfg.openroad_netlist.exists():
            print(f"ERROR: {cfg.openroad_netlist} not found: run the flow "
                  "(through its sv2v step) first", file=sys.stderr)
            return 1
        run_flow(cfg, a.mode, a.jobs, fresh=a.fresh)
    except LadderFailed as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return e.code
    except (LadderError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
