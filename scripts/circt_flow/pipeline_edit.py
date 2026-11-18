"""The pass pipeline as data.

Per stage, the passes the flow plans to run, with their options, obtained
without running a tool: the stage's command builders in stages.py run with a
recording stand-in for the tool (stages.RUNNER) on a scratch copy of the run
directory, so the plan is the exact command lines the stage would run. A
script edits the plan (insert / remove / replace / move), and hooks Python
callables before or after a pass (the stage's command is then split at that
pass so the callable sees the intermediate IR file). doc/scripting.md section 8
is the user guide.

    p = s.pipeline
    print(p.text())                                     # = s.report_passes()
    p.insert("map_datapath", "canonicalize", after="map-prefix-adders#2")
    p.remove("syn_map", "remove-identity-buffers")
    p.replace("map_datapath", "set-adder-arch", "set-adder-arch", arch="KOGGE-STONE")
    p.move("map_datapath", "narrow-whole-value-reads", before="structural-mapper")
    p.edits                                             # the record (settings.json "pipeline_edits")

Pass references: "name" (must occur once in the stage) or "name#k" (its k-th
occurrence in the stage, 1-based, counted over the stage's commands in
order). Each edit is checked when it is made (stage, reference, pass and
option names against the tool's own --help listing, a pass that defines a
command's output kept in it) against the plan as left by the edits before
it; an edit that is refused is not recorded. Before the first tool of a
stage runs, every stage's edits are checked again on the configuration the
run uses, and while the stage runs each command it builds must be the
planned one (else PipelineError, before that command runs). Without edits
and hooks nothing here is consulted: the flow is unchanged, byte for byte.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

from . import hooks as hooks_mod
from .options import OptionError


class PipelineError(OptionError):
    """An edit or hook of the pass pipeline that cannot be applied."""


# The flow's stages (pipeline.STAGE_NAMES once the session's stage split is
# in) and the stages.py functions that build each stage's pass commands.
STAGE_NAMES = ("elaborate", "map_datapath", "syn_map", "fanout", "vt_swap", "sta",
               "place_and_route")
PASS_STEPS = {"elaborate": ("hw_aggregate_to_comb",),
              "map_datapath": ("structural_mapping",),
              "syn_map": ("flatten_shell", "merge_and_map", "export_netlist")}
# What each stage runs besides its pass commands (shown by text()).
OTHER_STEPS = {"elaborate": "front end and LLHD lowering, directive checks, cell stubs",
               "syn_map": "OpenSTA-compatible netlist (sv2v)",
               "fanout": "fanout.py (high-fanout buffering), no pass list",
               "vt_swap": "vtswap.py (OpenSTA), no pass list",
               "sta": "sta.py (OpenSTA), no pass list",
               "place_and_route": "OpenROAD flow, no pass list"}
# A pass that defines what its command produces: never removed, replaced by
# another pass or moved to another command.
REQUIRED = {"abc-bridge": "maps the residual logic to cells",
            "export-verilog": "writes the netlist"}
# Flags of an opt tool that are not passes.
_TOOL_FLAGS = {"o", "liberty-dont-use", "allow-unregistered-dialect", "split-input-file", "verify-diagnostics",
               "verify-each", "pass-pipeline", "load-pass-plugin", "load-dialect-plugin",
               "show-dialects", "emit-bytecode", "no-implicit-module", "debug", "help",
               "version", "help-hidden", "print-ir-after-all", "print-ir-before-all"}


def stage_names() -> tuple:
    try:
        from . import pipeline
        return tuple(getattr(pipeline, "STAGE_NAMES", STAGE_NAMES))
    except ImportError:  # pragma: no cover
        return STAGE_NAMES


def _near(name, names) -> str:
    near = difflib.get_close_matches(str(name), list(names), 3, 0.5)
    return f" (did you mean {', '.join(near)}?)" if near else ""


# ---------------------------------------------------------------- tool listing

_LISTINGS: Dict[tuple, Dict[str, Set[str]]] = {}


def parse_help(text: str) -> Dict[str, Set[str]]:
    """{pass: {option}} from an opt tool's --help (a pass at six spaces, its
    options at eight)."""
    passes: Dict[str, Set[str]] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^      --([\w-]+)(\s|$)", line)
        if m:
            cur = m.group(1)
            passes[cur] = set()
            continue
        m = re.match(r"^        --([\w-]+)", line)
        if m and cur:
            passes[cur].add(m.group(1))
    return passes


def tool_listing(binary) -> Optional[Dict[str, Set[str]]]:
    """The passes and options `binary` registers (its --help), cached per
    binary and modification time; None when the binary cannot be run (the
    names are then not checked)."""
    p = Path(str(binary))
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _LISTINGS:
        try:
            out = subprocess.run([str(p), "--help"], capture_output=True, text=True,
                                 timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        _LISTINGS[key] = parse_help(out)
    return _LISTINGS[key] or None


# ---------------------------------------------------------------- data model

def _is_pass(arg: str) -> bool:
    if not arg.startswith("--") or len(arg) < 3:
        return False
    name = arg[2:].split("=", 1)[0]
    return name not in _TOOL_FLAGS and not name.startswith("mlir-")


def _opt_text(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(map(str, v))
    return str(v)


@dataclass
class PassEntry:
    """One pass of a planned command: `--name` or `--name=options`."""
    name: str
    options: Optional[str] = None
    origin: str = "default"        # default | inserted | replaced | moved

    @property
    def arg(self) -> str:
        return f"--{self.name}" + (f"={self.options}" if self.options is not None else "")

    @classmethod
    def parse(cls, arg: str, origin: str = "default") -> "PassEntry":
        body = arg[2:] if arg.startswith("--") else arg
        name, eq, opts = body.partition("=")
        return cls(name.strip(), opts if eq else None, origin)

    def option_names(self) -> List[str]:
        if not self.options:
            return []
        return [t.split("=", 1)[0] for t in self.options.split() if t]


def pass_spec(spec, options: Optional[dict] = None, origin: str = "inserted") -> PassEntry:
    """A pass from a script: "name", "--name", "name=opt=v opt2=w", or a
    PassEntry; keyword `options` (Python spelling, _ for -) are appended."""
    if isinstance(spec, PassEntry):
        e = PassEntry(spec.name, spec.options, origin)
    elif isinstance(spec, str) and spec.strip():
        e = PassEntry.parse(spec.strip(), origin)
    else:
        raise PipelineError(f"a pass is a name such as 'canonicalize' or "
                            f"'map-prefix-adders=min-width=14', not {spec!r}")
    if not re.fullmatch(r"[A-Za-z][\w-]*", e.name):
        raise PipelineError(f"{e.name!r} is not a pass name")
    if options:
        extra = " ".join(f"{k.replace('_', '-')}={_opt_text(v)}" for k, v in options.items())
        e.options = f"{e.options} {extra}" if e.options else extra
    return e


@dataclass
class Command:
    """One planned command of a stage: the argv with its passes taken out
    (rebuilt in place by argv()), the stdout file, and whether it is a pass
    command of an opt tool that may be edited."""
    step: str                       # the stages.py function that builds it
    template: List[str]             # argv with the passes removed
    passes: List[PassEntry]
    at: int                         # where the passes go in template
    stdout: Optional[str]
    editable: bool
    tool: str = ""

    def argv(self, passes: Optional[Sequence[PassEntry]] = None, src: Optional[str] = None,
             dst: Optional[str] = None) -> List[str]:
        t = list(self.template)
        if src is not None:
            t[self.input_index()] = src
        if dst is not None:
            t[self.output_index()] = dst
        ps = self.passes if passes is None else passes
        return t[:self.at] + [p.arg for p in ps] + t[self.at:]

    def input_index(self) -> int:
        pos = [i for i, a in enumerate(self.template[1:], 1)
               if not a.startswith("-") and self.template[i - 1] != "-o"]
        if len(pos) != 1:
            raise PipelineError(f"command of {self.step} has {len(pos)} input files; "
                                "a pass hook cannot split it")
        return pos[0]

    def output_index(self) -> int:
        if "-o" not in self.template[:-1]:
            raise PipelineError(f"command of {self.step} names no -o output; "
                                "a pass hook cannot split it")
        return self.template.index("-o") + 1

    def line(self) -> List[str]:
        return self.argv() + ([">", self.stdout] if self.stdout is not None else [])


def make_command(step: str, argv: List[str], stdout, opt_tools: Set[str]) -> Command:
    idx = [i for i, a in enumerate(argv) if i > 0 and _is_pass(a)]
    contiguous = bool(idx) and idx == list(range(idx[0], idx[0] + len(idx)))
    editable = argv[0] in opt_tools and contiguous
    if not editable:
        return Command(step, list(argv), [], len(argv), stdout, False, argv[0])
    tpl = argv[:idx[0]] + argv[idx[-1] + 1:]
    return Command(step, tpl, [PassEntry.parse(argv[i]) for i in idx], idx[0], stdout,
                   True, argv[0])


@dataclass
class StagePlan:
    stage: str
    commands: List[Command] = field(default_factory=list)

    def entries(self):
        """[(command index, pass index, entry)] over the editable commands."""
        return [(ci, pi, e) for ci, c in enumerate(self.commands) if c.editable
                for pi, e in enumerate(c.passes)]

    def refs(self) -> List[str]:
        """The stable reference of every pass, in order ("name" when it
        occurs once in the stage, else "name#k")."""
        names = [e.name for _, _, e in self.entries()]
        seen: Dict[str, int] = {}
        out = []
        for n in names:
            seen[n] = seen.get(n, 0) + 1
            out.append(n if names.count(n) == 1 else f"{n}#{seen[n]}")
        return out

    def find(self, ref) -> tuple:
        """(command index, pass index) of reference `ref`."""
        if not isinstance(ref, str) or not ref:
            raise PipelineError(f"a pass reference is 'name' or 'name#k', not {ref!r}")
        name, _, k = ref.lstrip("-").partition("#")
        hits = [(ci, pi) for ci, pi, e in self.entries() if e.name == name]
        if not hits:
            have = sorted({e.name for _, _, e in self.entries()})
            raise PipelineError(f"stage {self.stage} has no pass {name!r}{_near(name, have)}"
                                + ("" if have else "; it has no editable passes"))
        if k:
            if not k.isdigit() or not 1 <= int(k) <= len(hits):
                raise PipelineError(f"{ref!r}: stage {self.stage} has {len(hits)} "
                                    f"{name} pass(es); use {name}#1 .. {name}#{len(hits)}")
            return hits[int(k) - 1]
        if len(hits) > 1:
            raise PipelineError(f"{ref!r} is ambiguous: stage {self.stage} runs {name} "
                                f"{len(hits)} times; use {name}#1 .. {name}#{len(hits)}")
        return hits[0]


# ---------------------------------------------------------------- dry run

class _Recorder:
    def __init__(self):
        self.step = ""
        self.cmds: List[tuple] = []

    def __call__(self, cmd, stdout=None, env=None):
        self.cmds.append((self.step, [str(c) for c in cmd],
                          None if stdout is None else str(stdout)))


class _NoReplaceOS:
    """stages.os during a dry run: os, without moving files."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def replace(a, b):
        return None


def calibrated(cfg):
    """cfg as pipeline.calibrate would make it, without writing or printing."""
    if not getattr(cfg.design, "calibrate", False) or cfg.design.abc_delay_target_ps is not None:
        return cfg
    from .pipeline import mapping_target
    with contextlib.redirect_stdout(io.StringIO()):
        rec = mapping_target(cfg.design, cfg.design.top_module)
    return dataclasses.replace(cfg, design=dataclasses.replace(
        cfg.design, abc_delay_target_ps=rec["target_ps"]))


def plan(cfg, stage_list: Optional[Sequence[str]] = None,
         has_pass: Optional[Callable] = None) -> Dict[str, StagePlan]:
    """The planned commands of each stage for `cfg` (the configuration the
    stage functions get), without running a tool: the stage functions run
    on a scratch run directory with stages.RUNNER recording; paths are
    mapped back to cfg.out_dir. `has_pass(binary, name)`: the stand-in for
    stages._binary_has_pass (default: the real one, which reads the tool's
    --help)."""
    from . import stages, abc_partition
    names = stage_names()
    wanted = list(stage_list) if stage_list is not None else list(names)
    for st in wanted:
        if st not in names:
            raise PipelineError(f"unknown stage {st!r}{_near(st, names)}; stages: {', '.join(names)}")
    out = {st: StagePlan(st) for st in wanted}
    if not any(PASS_STEPS.get(st) for st in wanted):
        return out
    real = Path(cfg.out_dir)
    # A dry run leaves no trace in process state: the executed-command record
    # (stages.EXECUTED: the partitioned stage appends to it directly), the
    # per-directory scoped record and the once-only warnings (scoping,
    # options) are restored, and what the stage functions print is dropped.
    from . import options as _options, scoping as _scoping
    state = (list(stages.EXECUTED), dict(_scoping.APPLIED), set(_scoping._WARNED),
             set(_options._WARNED))
    try:
        return _plan(cfg, wanted, out, real, has_pass)
    finally:
        stages.EXECUTED[:] = state[0]
        _scoping.APPLIED.clear()
        _scoping.APPLIED.update(state[1])
        _scoping._WARNED.clear()
        _scoping._WARNED.update(state[2])
        _options._WARNED.clear()
        _options._WARNED.update(state[3])


def _plan(cfg, wanted, out, real, has_pass):
    from . import stages, abc_partition
    tmp_real = Path(tempfile.mkdtemp(prefix="p5plan_")).resolve()
    link = tmp_real.parent / (tmp_real.name + "_out")
    try:
        os.symlink(tmp_real, link)
        repl = {"out_dir": link}
        pw = Path(cfg.partition_workdir)
        if pw == real / "partitions":
            repl["partition_workdir"] = link / "partitions"
        dcfg = dataclasses.replace(cfg, **repl)
        mapped = Path(dcfg.mapped_mlir)
        mapped.parent.mkdir(parents=True, exist_ok=True)
        mapped.write_text(f"hw.module @{cfg.design.top_module}_residual() {{}}\n")

        class R:
            def __init__(self, n):
                self.name, self.returncode, self.seconds = n, 0, 1.0
        rec = _Recorder()
        saved = (stages.RUNNER, stages._binary_has_pass, stages.os,
                 abc_partition.map_groups, abc_partition.combine)
        cache: Dict[tuple, bool] = {}

        def hp(b, p):
            if (str(b), p) not in cache:
                cache[(str(b), p)] = (has_pass or saved[1])(b, p)
            return cache[(str(b), p)]
        try:
            stages.RUNNER = rec
            stages._binary_has_pass = hp
            stages.os = _NoReplaceOS()
            abc_partition.map_groups = lambda *a, **k: (
                rec.cmds.append((rec.step, ["map_groups", *[str(x) for x in a[1:]],
                                            json.dumps(k.get("per_group_extra"), sort_keys=True)],
                                 None)), [R("g0")])[1]
            abc_partition.combine = lambda *a, **k: None
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                for st in wanted:
                    for fn in PASS_STEPS.get(st, ()):
                        rec.step = fn
                        n0 = len(rec.cmds)
                        getattr(stages, fn)(dcfg)
                        out[st].commands += [
                            (s, a, o) for s, a, o in rec.cmds[n0:]]
        finally:
            (stages.RUNNER, stages._binary_has_pass, stages.os,
             abc_partition.map_groups, abc_partition.combine) = saved
    finally:
        with contextlib.suppress(OSError):
            os.unlink(link)
        shutil.rmtree(tmp_real, ignore_errors=True)

    def back(a: str) -> str:
        return a.replace(str(link), str(real)).replace(str(tmp_real), str(real.resolve()))
    tools = {str(cfg.circt_abc_opt), str(cfg.circt_opt)}
    for st in wanted:
        out[st].commands = [make_command(s, [back(x) for x in a],
                                         None if o is None else back(o), tools)
                            for s, a, o in out[st].commands]
    return out


# ---------------------------------------------------------------- edits

_OPS = ("insert", "remove", "replace", "move")


def _anchor(rec: dict):
    after, before = rec.get("after"), rec.get("before")
    if after is not None and before is not None:
        raise PipelineError("give after= or before=, not both")
    return after, before


def apply_edit(plans: Dict[str, StagePlan], rec: dict, listing: Callable) -> None:
    """Apply one edit record to `plans` in place (PipelineError if it
    cannot be)."""
    op = rec.get("op")
    if op not in _OPS:
        raise PipelineError(f"unknown pipeline edit {op!r}; edits: {', '.join(_OPS)}")
    st = rec.get("stage")
    names = stage_names()
    if st not in names:
        raise PipelineError(f"unknown stage {st!r}{_near(st, names)}; stages: {', '.join(names)}")
    sp = plans[st]
    if not sp.entries():
        raise PipelineError(f"stage {st} has no editable pass list ({OTHER_STEPS.get(st, 'none')})")
    if op in ("insert", "replace"):
        new = pass_spec(rec["pass"], origin="inserted" if op == "insert" else "replaced")
    if op in ("remove", "replace", "move"):
        ci, pi = sp.find(rec.get("ref"))
        old = sp.commands[ci].passes[pi]
    if op == "remove":
        if old.name in REQUIRED:
            raise PipelineError(f"pass {old.name} cannot be removed: it {REQUIRED[old.name]}")
        del sp.commands[ci].passes[pi]
        return
    if op == "replace":
        if old.name in REQUIRED and new.name != old.name:
            raise PipelineError(f"pass {old.name} cannot be replaced by another pass: "
                                f"it {REQUIRED[old.name]}")
        _check_pass(sp.commands[ci], new, listing)
        sp.commands[ci].passes[pi] = new
        return
    after, before = _anchor(rec)
    if op == "move":
        if after is None and before is None:
            raise PipelineError("move needs after= or before=")
        aci, api = sp.find(after if after is not None else before)
        anchor = sp.commands[aci].passes[api]
        if anchor is old:
            raise PipelineError(f"move: {rec.get('ref')!r} is its own anchor")
        if old.name in REQUIRED and aci != ci:
            raise PipelineError(f"pass {old.name} cannot leave its command: it "
                                f"{REQUIRED[old.name]}")
        _check_pass(sp.commands[aci], old, listing)
        del sp.commands[ci].passes[pi]
        api = next(i for i, e in enumerate(sp.commands[aci].passes) if e is anchor)
        moved = PassEntry(old.name, old.options, "moved" if old.origin == "default" else old.origin)
        sp.commands[aci].passes.insert(api + (1 if after is not None else 0), moved)
        return
    # insert
    if after is None and before is None:
        aci, api = sp.entries()[-1][:2]
        after = True
    else:
        aci, api = sp.find(after if after is not None else before)
    _check_pass(sp.commands[aci], new, listing)
    sp.commands[aci].passes.insert(api + (1 if after is not None else 0), new)


def _check_pass(cmd: Command, e: PassEntry, listing: Callable) -> None:
    known = listing(cmd.tool)
    if known is None:
        return
    if e.name not in known:
        raise PipelineError(f"unknown pass {e.name!r}{_near(e.name, known)}: not a pass of "
                            f"{cmd.tool} (the command of {cmd.step} it would run in)")
    for o in e.option_names():
        if o not in known[e.name]:
            raise PipelineError(f"pass {e.name} has no option {o!r}"
                                + (_near(o, known[e.name]) or
                                   f"; its options: {', '.join(sorted(known[e.name])) or 'none'}"))


# ---------------------------------------------------------------- the pipeline

class Pipeline:
    """The pass pipeline of a session (s.pipeline): the planned passes per
    stage, the script's edits to them, and pass-level hooks. Standalone
    (tests, tools): Pipeline(cfg=FlowConfig, listing=..., has_pass=...)."""

    def __init__(self, session=None, *, cfg=None, listing: Optional[Callable] = None,
                 has_pass: Optional[Callable] = None):
        self._s = session
        self._cfg0 = cfg
        self._listing = listing or tool_listing
        self._has_pass = has_pass
        self._edits: List[dict] = []
        self._hooks: List[hooks_mod.PassHook] = []
        self._ir_count = 0

    # ------------------------------------------------ configuration
    def _cfg(self, cfg=None):
        if cfg is not None:
            return cfg
        if self._cfg0 is not None:
            return calibrated(self._cfg0)
        s = self._s
        rc = getattr(s, "_run_cfg", None)
        if rc is not None:
            return rc
        if hasattr(s, "_load"):
            return calibrated(s._load())
        return calibrated(s.cfg)

    def _check_open(self, stage: str) -> None:
        done = getattr(self._s, "done", None)
        if callable(done) and stage in stage_names() and done(stage):
            raise PipelineError(f"stage {stage} has already run in this run directory: an edit "
                                f"or hook could no longer take effect (s.invalidate({stage!r}) "
                                "re-opens it)")
        # a stage running now (an edit from one of its pass hooks), or one
        # before it: its commands are already built
        closed = getattr(self._s, "_closed", None)
        if callable(closed) and stage in stage_names() and closed(stage):
            raise PipelineError(f"stage {stage} cannot be edited while stage "
                                f"{getattr(self._s, '_running', '?')} is running (an edit from a "
                                "hook inside it could not take effect); edit before it starts")

    # ------------------------------------------------ plan
    def planned(self, cfg=None, edits: Optional[List[dict]] = None) -> Dict[str, StagePlan]:
        """Every stage's plan with the edits applied (PipelineError on the
        first edit that does not apply)."""
        cfg = self._cfg(cfg)
        plans = plan(cfg, has_pass=self._has_pass)
        todo = self._edits if edits is None else edits
        if (todo or self._hooks) and getattr(cfg.design, "power", None) is not None:
            raise PipelineError("pipeline edits and pass hooks are not available for a design "
                                "with power domains (power.run runs its own sub-flows)")
        for i, rec in enumerate(todo, 1):
            try:
                apply_edit(plans, rec, self._listing)
            except PipelineError as e:
                raise PipelineError(f"pipeline edit {i} {json.dumps(rec)}: {e}") from None
        for h in self._hooks:
            self._resolve_hook(plans, h)
        return plans

    def passes(self, stage: str) -> List[dict]:
        """The passes of `stage` as planned now: [{ref, name, options,
        command, step, origin}]."""
        sp = self.planned()[stage] if stage in stage_names() else None
        if sp is None:
            raise PipelineError(f"unknown stage {stage!r}{_near(stage, stage_names())}")
        return [{"ref": r, "name": e.name, "options": e.options, "command": ci + 1,
                 "step": sp.commands[ci].step, "origin": e.origin}
                for (ci, pi, e), r in zip(sp.entries(), sp.refs())]

    def commands(self, stage: str) -> List[List[str]]:
        """The command lines of `stage` as they will run (no split by hooks)."""
        return [c.line() for c in self.planned()[stage].commands]

    def text(self) -> str:
        plans = self.planned()
        mark = {"default": " ", "inserted": "+", "replaced": "~", "moved": ">"}
        rows = []
        for st in stage_names():
            sp = plans[st]
            rows.append(f"stage {st}" + (f"  ({OTHER_STEPS[st]})" if st in OTHER_STEPS else ""))
            refs = iter(sp.refs())
            for ci, c in enumerate(sp.commands):
                if not c.editable:
                    rows.append(f"  [{ci + 1}] {c.step}: {' '.join(c.line())}  (not editable)")
                    continue
                rows.append(f"  [{ci + 1}] {c.step}: {Path(c.tool).name}"
                            + (f" > {c.stdout}" if c.stdout else ""))
                for e in c.passes:
                    r = next(refs)
                    hk = [h.kind for h in self._hooks if h.stage == st and h.ref == r]
                    rows.append((f"    {mark.get(e.origin, ' ')} {r:32s} {e.options or ''}"
                                 + (f"  [hook {', '.join(hk)}]" if hk else "")).rstrip())
        rows.append(f"edits: {len(self._edits)}" + "".join(
            f"\n  {i}. {json.dumps(r)}" for i, r in enumerate(self._edits, 1)))
        rows.append(f"pass hooks: {len(self._hooks)}" + "".join(
            f"\n  {h.kind} {h.stage} {h.ref}: {h.where.get('name')} "
            f"({h.where.get('file')}:{h.where.get('line')})" for h in self._hooks))
        rows.append("(+ inserted, ~ replaced, > moved)")
        return "\n".join(rows)

    __str__ = text

    def __repr__(self):
        return f"Pipeline(edits={len(self._edits)}, pass_hooks={len(self._hooks)})"

    # ------------------------------------------------ edits
    def _edit(self, rec: dict) -> dict:
        rec = {k: v for k, v in rec.items() if v is not None}
        self._check_open(rec.get("stage"))
        self.planned(edits=self._edits + [rec])        # raises: the edit is refused
        self._edits.append(rec)
        return dict(rec)

    def insert(self, stage: str, spec, after: Optional[str] = None,
               before: Optional[str] = None, **options) -> dict:
        """Insert pass `spec` ("name" or "name=opt=v ...", options also as
        keywords) after or before pass reference `after` / `before` (neither:
        after the stage's last pass)."""
        e = pass_spec(spec, options)
        _anchor({"after": after, "before": before})
        return self._edit({"op": "insert", "stage": stage, "pass": e.arg[2:],
                           "after": after, "before": before})

    def remove(self, stage: str, ref: str) -> dict:
        return self._edit({"op": "remove", "stage": stage, "ref": ref})

    def replace(self, stage: str, ref: str, spec, **options) -> dict:
        """Replace pass `ref` with `spec` (the same pass with other options,
        or another pass)."""
        e = pass_spec(spec, options)
        return self._edit({"op": "replace", "stage": stage, "ref": ref, "pass": e.arg[2:]})

    def move(self, stage: str, ref: str, after: Optional[str] = None,
             before: Optional[str] = None) -> dict:
        """Move pass `ref` after or before another pass of the same stage
        (references resolved on the pipeline before the move)."""
        return self._edit({"op": "move", "stage": stage, "ref": ref,
                           "after": after, "before": before})

    @property
    def edits(self) -> List[dict]:
        return copy.deepcopy(self._edits)

    def load_edits(self, edits: Sequence[dict]) -> None:
        """Replace the edits with a recorded list (settings.json
        "pipeline_edits"); all or nothing."""
        recs = [dict(r) for r in (edits or [])]
        for r in recs:
            if not isinstance(r, dict):
                raise PipelineError(f"a pipeline edit is a dict, not {r!r}")
            self._check_open(r.get("stage"))
        self.planned(edits=recs)
        self._edits = recs

    def clear(self) -> None:
        """Drop every edit: the default pipeline."""
        self._edits.clear()

    def validate(self, cfg=None) -> None:
        """Check every edit and hook now (they are also checked before the
        first tool of each stage runs)."""
        self.planned(cfg)

    # ------------------------------------------------ hooks
    def _resolve_hook(self, plans, h) -> tuple:
        sp = plans[h.stage]
        try:
            ci, pi = sp.find(h.ref)
            c = sp.commands[ci]
            c.input_index(), c.output_index()
        except PipelineError as e:
            raise PipelineError(f"{h.kind} hook on {h.stage} {h.ref}: {e}") from None
        return ci, pi

    def hook(self, stage: str, ref: str, fn: Callable, after: bool):
        """fn(session, ir_path) just before (after=False) or just after pass
        `ref` of `stage`; the stage's command is split there."""
        if not callable(fn):
            raise PipelineError(f"a hook is a callable, not {fn!r}")
        names = stage_names()
        if stage not in names:
            raise PipelineError(f"unknown stage {stage!r}{_near(stage, names)}; "
                                f"stages: {', '.join(names)}")
        self._check_open(stage)
        h = hooks_mod.PassHook(stage, ref, fn, bool(after), hooks_mod.describe(fn))
        self._hooks.append(h)
        try:
            self.planned()
        except PipelineError:
            self._hooks.pop()
            raise
        return fn

    def pre_pass(self, stage: str, ref: str, fn: Callable):
        return self.hook(stage, ref, fn, after=False)

    def post_pass(self, stage: str, ref: str, fn: Callable):
        return self.hook(stage, ref, fn, after=True)

    def clear_hooks(self) -> None:
        self._hooks.clear()

    @property
    def pass_hooks(self) -> List[hooks_mod.PassHook]:
        return list(self._hooks)

    # ------------------------------------------------ application
    def active(self, stage: Optional[str] = None) -> bool:
        if stage is None:
            return bool(self._edits or self._hooks)
        return any(r.get("stage") == stage for r in self._edits) or \
            any(h.stage == stage for h in self._hooks)

    @contextlib.contextmanager
    def applied(self, stage: str, cfg=None):
        """While the body runs stage `stage`'s functions with `cfg`: every
        edit of every stage is checked first (before any tool of this stage
        runs), then the stage's commands run edited and split at its hooked
        passes. Without edits and hooks: nothing."""
        if not self.active():
            yield
            return
        cfg = self._cfg(cfg)
        plans = self.planned(cfg)
        if not self.active(stage):
            yield
            return
        from . import stages
        base = plan(cfg, [stage], has_pass=self._has_pass)[stage]
        runner = _Runner(self, stage, plans[stage], base, cfg)
        prev = stages.RUNNER
        stages.RUNNER = runner
        try:
            yield
        except BaseException:
            stages.RUNNER = prev
            raise
        stages.RUNNER = prev
        runner.finish()

    def _call_hook(self, h, ir: str) -> None:
        from . import stages
        stages.record_step(h.kind, h.stage, h.ref, h.where.get("name"),
                           f"{h.where.get('file')}:{h.where.get('line')}", ir)
        h.fn(self._s, Path(ir))


class _Runner:
    """stages.RUNNER while an edited stage runs: each command of an opt
    tool must be the next planned one; it runs edited (and split at hooked
    passes). Other commands run unchanged."""

    def __init__(self, pipe: Pipeline, stage: str, sp: StagePlan, base: StagePlan, cfg):
        self.pipe, self.stage, self.sp, self.base, self.cfg = pipe, stage, sp, base, cfg
        self.tools = {str(cfg.circt_abc_opt), str(cfg.circt_opt)}
        self.todo = [ci for ci, c in enumerate(sp.commands) if c.tool in self.tools]
        self.at: Dict[int, Dict[int, list]] = {}
        for h in pipe._hooks:
            if h.stage != stage:
                continue
            ci, pi = sp.find(h.ref)
            b = pi + (1 if h.after else 0)
            self.at.setdefault(ci, {}).setdefault(b, []).append(h)

    def __call__(self, cmd, stdout=None, env=None):
        from . import stages
        cmd = [str(c) for c in cmd]
        so = None if stdout is None else str(stdout)
        if cmd[0] not in self.tools:
            return stages._run_plain(cmd, stdout=stdout, env=env)
        if not self.todo:
            raise PipelineError(f"stage {self.stage}: the flow ran a command its plan does not "
                                f"have: {' '.join(cmd)}")
        ci = self.todo.pop(0)
        c, base = self.sp.commands[ci], self.base.commands[ci]
        if cmd != base.argv() or so != base.stdout:
            raise PipelineError(f"stage {self.stage}: the flow built a command its plan does not "
                                f"have (planned: {' '.join(base.line())}; built: {' '.join(cmd)})")
        if not c.editable:
            return stages._run_plain(cmd, stdout=stdout, env=env)
        at = self.at.get(ci)
        if not at:
            return stages._run_plain(c.argv(), stdout=stdout, env=env)
        out_dir = Path(self.cfg.out_dir) / "pipeline"
        out_dir.mkdir(parents=True, exist_ok=True)
        names = [e.name for e in c.passes]

        def ir_path(b):
            self.pipe._ir_count += 1
            where = f"after_{names[b - 1]}" if b > 0 else f"before_{names[0]}"
            return str(out_dir / f"{self.pipe._ir_count:02d}_{self.stage}_cmd{ci + 1}_{where}.mlir")
        segs = hooks_mod.plan_split(len(c.passes), at, c.template[c.input_index()],
                                    c.template[c.output_index()], so, ir_path)
        hooks_mod.run_split(segs, lambda lo, hi, s, d: c.argv(c.passes[lo:hi], s, d),
                            stages._run_plain, self.pipe._call_hook, env=env)

    def finish(self) -> None:
        if self.todo:
            left = [" ".join(self.sp.commands[ci].argv()) for ci in self.todo]
            raise PipelineError(f"stage {self.stage}: planned command(s) did not run, so their "
                                f"edits or hooks did not apply: {left}")


# ---------------------------------------------------------------- the record

def run_record(session) -> dict:
    """The settings-record fields of a session's pipeline: "pipeline_edits"
    (only when there are edits) and "hooks" (only when a hook is
    registered: then the run is not reproducible from settings alone)."""
    out: dict = {}
    pipe = getattr(session, "_pipe", None)
    if pipe is not None and pipe._edits:
        out["pipeline_edits"] = pipe.edits
    hk = getattr(session, "hooks", None)
    entries = hooks_mod.record_entries(getattr(hk, "_pre", ()), getattr(hk, "_post", ()),
                                       pipe._hooks if pipe is not None else ())
    if entries:
        out["hooks"] = {"reproducible_from_settings": False,
                        "note": "hooks are Python code: this record names them but cannot "
                                "replay them",
                        "registered": entries}
    return out
