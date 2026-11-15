"""Hooks of the session API:
how a hook is described in the run record, and how one tool invocation is
split at pass boundaries so that a pass-level hook sees the intermediate IR.

The hook registry for stages is the session's (`s.hooks.pre/post`); the
pass-level hooks (`s.hooks.pre_pass/post_pass`) live in the pipeline
(pipeline_edit.Pipeline.hook) because they are resolved against its passes.
A hook is code: the record names it (function, file, line) and marks the
run as not reproducible from settings.json alone.
"""
from __future__ import annotations

import inspect
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

KINDS = ("pre", "post", "pre_pass", "post_pass")


def describe(fn: Callable) -> dict:
    """{name, file, line} of a hook callable (what the run record keeps)."""
    target = inspect.unwrap(fn)
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None) \
        or type(target).__name__
    try:
        file = inspect.getsourcefile(target) or inspect.getfile(target)
    except TypeError:
        file = None
    code = getattr(target, "__code__", None)
    if code is None and hasattr(target, "__call__"):
        code = getattr(getattr(target, "__call__"), "__code__", None)
    return {"name": name, "file": file or "?", "line": code.co_firstlineno if code else None}


@dataclass
class PassHook:
    """A pass-level hook: `fn(session, ir_path)` called with the IR file
    just before (`after` False) or just after (`after` True) pass `ref` of
    `stage`."""
    stage: str
    ref: str
    fn: Callable
    after: bool
    where: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "post_pass" if self.after else "pre_pass"

    def record(self) -> dict:
        return {"kind": self.kind, "stage": self.stage, "pass": self.ref, **self.where}


def record_entries(stage_pre=(), stage_post=(), pass_hooks=()) -> List[dict]:
    """The run record's list of hooks: stage hooks as (stage, fn) pairs,
    pass hooks as PassHook."""
    out = [{"kind": "pre", "stage": st, **describe(fn)} for st, fn in stage_pre]
    out += [{"kind": "post", "stage": st, **describe(fn)} for st, fn in stage_post]
    out += [h.record() for h in pass_hooks]
    return out


# ---------------------------------------------------------------- splitting

@dataclass
class Segment:
    """One piece of a split command: passes[lo:hi] from `src` to `dst`
    (an empty piece is a copy), then the hooks at its end boundary."""
    lo: int
    hi: int
    src: str
    dst: str
    hooks: List[PassHook]
    stdout: Optional[str] = None


def plan_split(n_passes: int, at: Dict[int, List[PassHook]], src: str, dst: str,
               stdout: Optional[str], ir_path: Callable[[int], str]) -> List[Segment]:
    """Cut a command of `n_passes` passes at the boundaries in `at`
    ({boundary b in 0..n: hooks}; b = before pass b). Pieces run in order,
    each from the previous piece's file; the hooks of boundary b see
    ir_path(b). The last piece writes `dst`. The command's stdout goes to
    the last piece that runs passes (the one that prints, export-verilog)."""
    segs: List[Segment] = []
    cur, pos = src, 0
    for b in sorted(at):
        tmp = ir_path(b)
        segs.append(Segment(pos, b, cur, tmp, list(at[b])))
        cur, pos = tmp, b
    segs.append(Segment(pos, n_passes, cur, dst, []))
    runs = [s for s in segs if s.hi > s.lo]
    if runs:
        runs[-1].stdout = stdout
    return segs


def run_split(segs: List[Segment], build: Callable, run_plain: Callable,
              call_hook: Callable, env=None) -> None:
    """Run the pieces of plan_split: `build(lo, hi, src, dst)` gives the
    argv of passes[lo:hi]; `call_hook(hook, ir)` runs one hook."""
    for s in segs:
        if s.hi > s.lo:
            run_plain(build(s.lo, s.hi, s.src, s.dst), stdout=s.stdout, env=env)
        elif s.src != s.dst and s.dst != "/dev/null":
            Path(s.dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s.src, s.dst)
        for h in s.hooks:
            call_hook(h, s.dst)
