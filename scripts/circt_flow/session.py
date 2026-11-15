"""The Python session API of the flow. doc/scripting.md is the user guide.

    from circt_flow import Session
    s = Session("flow.yaml", "out", run_vt_swap=True, run_sta=True)
    s.db.syn.adders.cells.min_width = 14          # validated at once
    r = s.elaborate(); print(r.metrics)           # stages one at a time ...
    s.map_datapath(); s.syn_map(); s.vt_swap(); s.sta()
    print(s.report_qor()); s.write_settings()
    s.run()                                       # ... or the whole flow (pipeline.run)
    Session.resume("out").run()                   # continue a run directory

The full run (`s.run()` with no start/stop) is pipeline.run itself, the code
path of the command line, with the session's hooks and result collection
around each stage: its netlists are the command line's. Stages run one at a
time go through the same stage functions (pipeline.SYNTH_STAGES).

An option is set (s.db / s.set) with source "argument", and where the script
set it is recorded (settings.json "set_at"). An option read by a stage that
has already run in this run directory is refused (it could no longer take
effect); s.invalidate(stage) re-opens that stage and the ones after it.
"""
from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import options, provenance_settings
from .db import AttrDB, SettingsReport
from .results import FlowResult, PassResult, StageResult
from . import results as results_mod

STATE_FILE = "session.json"
_PKG = Path(__file__).resolve().parent


class SessionError(RuntimeError):
    """A stage or pass that cannot run in the session's present state."""


try:
    # scoping.py owns the definition; this copy is used without it
    from .scoping import ScopedSetting  # noqa: F401
except ImportError:
    @dataclass(frozen=True)
    class ScopedSetting:
        """A setting on selected design objects (scoping.py builds
        them). `targets`: resolved names / ids (JSON-able), never live objects."""
        kind: str                       # "register" | "adder" | "multiplier" | "shifter" | "icg"
        targets: tuple
        command: str                    # e.g. "gate_register"
        values: tuple = ()              # ((key, value), ...)
        set_at: str = ""

        def as_dict(self) -> dict:
            return {"kind": self.kind, "targets": list(self.targets), "command": self.command,
                    "values": dict(self.values), "set_at": self.set_at}

        @classmethod
        def from_dict(cls, d: dict) -> "ScopedSetting":
            return cls(d["kind"], tuple(d["targets"]), d["command"],
                       tuple(sorted(d.get("values", {}).items())), d.get("set_at", ""))


def _scoped_record(x) -> dict:
    return x.as_dict() if hasattr(x, "as_dict") else x.to_record()


def _scoped_from(d: dict):
    if hasattr(ScopedSetting, "from_dict"):
        return ScopedSetting.from_dict(d)
    from .scoping import from_record
    return from_record(d)


# ---------------------------------------------------------------- stages

def _pipeline():
    from . import pipeline
    return pipeline


def stage_names() -> tuple:
    return _pipeline().STAGE_NAMES


# what each stage needs done before it (data, not order: re-running a stage
# re-opens the stages after it)
NEEDS = {"elaborate": (), "map_datapath": ("elaborate",), "syn_map": ("map_datapath",),
         "fanout": ("syn_map",), "vt_swap": ("syn_map",), "sta": ("syn_map",),
         "place_and_route": ("syn_map",)}
# the settings.json label written after a stage (pipeline.run's labels)
PROVENANCE_LABEL = {"sta": "synthesis", "place_and_route": "pnr"}


# Options of a later namespace that an earlier stage also reads (the
# configuration field they set, FIELD_STAGE, is read earlier): an option
# belongs to the EARLIEST stage that reads it. tests/test_session.py derives,
# for every option, the configuration fields it changes and the module
# sources that name it, and fails when stage_of() is later than any reader.
EARLY = {
    # design.clock_port: the shorthand mode's clock (effective_modes): the
    # mapping-target calibration before elaborate, vt_swap's and STA's SDCs
    "pnr.clock_port": "elaborate",
}

# The earliest stage that reads each field of FlowConfig / DesignConfig (the
# configuration the stages get). "elaborate" also covers what pipeline.prepare
# reads before the first stage (calibration). A new field must be added here
# (the test fails otherwise).
FIELD_STAGE = {
    # DesignConfig
    "pnr": "place_and_route", "pack_pnr": "place_and_route",
    "pack_settings": "place_and_route",
    "fanout": "fanout", "fanout_override": "fanout",
    "vt_swap": "vt_swap", "sta": "sta",
    **{f: "elaborate" for f in (
        "top_module", "clock_period_ps", "synth_lib_tt", "manifest", "profile", "dont_use", "tie",
        "flow_defaults", "pack_root", "config_dir", "pack_private", "rail", "voltage", "core_mlir",
        "power", "modes", "mapping_target", "mapping_target_from_pack", "abc_delay_target_ps",
        "clock_port", "time_unit", "multibit", "wire_model", "wire_rc", "wire_rc_setup",
        "clock_gating", "power_options")},
    # FlowConfig (besides design / settings / load_overrides)
    "run_vt_swap": "vt_swap", "run_sta": "sta",
    **{f: "elaborate" for f in (
        "config_path", "out_dir", "circt_abc_opt", "circt_opt", "abc_bin", "enable_classical_resub",
        "partitions", "abc_pool_size", "partition_workdir", "abc_script_file", "abc_read_lib_args",
        "abc_save_aig_dir", "abc_use_datapath", "abc_save_mapped_dir", "abc_use_mapped_dir",
        "abc_use_mapped_if_present", "abc_keep_buffers", "skip_unmappable", "sv2v_docker_image",
        "directives", "root")},
}


def stage_of(name: str) -> str:
    """The earliest stage that reads option `name` (conservative: a synthesis
    option counts from elaborate on). An option of a stage that has run is
    refused (Session._check_open)."""
    if name in EARLY:
        return EARLY[name]
    if name.startswith(("pnr.", "cts.", "hold.", "post.", "run.")):
        return "place_and_route"
    if name.startswith("syn.vt_swap."):
        return "vt_swap"
    if name.startswith("syn.sta."):
        return "sta"
    if name.startswith("syn.fanout_buffer."):
        return "fanout"
    if name == "syn.scoped":
        return "map_datapath"           # read by the structural stage only
    return "elaborate"


def _caller() -> str:
    """script:<file>:<line> of the first frame outside this package."""
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        try:
            inside = Path(fn).resolve().parent == _PKG
        except (OSError, ValueError):
            inside = False
        if not inside and not fn.startswith("<frozen"):
            if fn.startswith("<"):
                return f"shell:{fn}:{f.f_lineno}"
            return f"script:{fn}:{f.f_lineno}"
        f = f.f_back
    return "script:?"


def _near(name, names) -> str:
    near = difflib.get_close_matches(str(name), list(names), 3, 0.5)
    return f" (did you mean {', '.join(near)}?)" if near else ""


# ---------------------------------------------------------------- hooks

class Hooks:
    """Python callables around stages: pre(stage, fn) calls fn(session)
    before the stage, post(stage, fn) calls fn(session, result) after it.
    stage "*" means every stage. Pass-level hooks: pre_pass / post_pass
    (s.pipeline)."""

    def __init__(self, session):
        self._s = session
        self._pre: List[tuple] = []
        self._post: List[tuple] = []

    @staticmethod
    def _check(stage: str) -> None:
        if stage != "*" and stage not in stage_names():
            raise SessionError(f"unknown stage {stage!r}{_near(stage, stage_names())}; "
                               f"stages: {', '.join(stage_names())}")

    def pre(self, stage: str, fn: Callable) -> Callable:
        self._check(stage)
        self._pre.append((stage, fn))
        return fn

    def post(self, stage: str, fn: Callable) -> Callable:
        self._check(stage)
        self._post.append((stage, fn))
        return fn

    def pre_pass(self, stage: str, pass_ref: str, fn: Callable) -> Callable:
        """fn(session, ir_path) before pass pass_ref of stage (the
        Pipeline splits that command there)."""
        return self._s.pipeline.hook(stage, pass_ref, fn, after=False)

    def post_pass(self, stage: str, pass_ref: str, fn: Callable) -> Callable:
        return self._s.pipeline.hook(stage, pass_ref, fn, after=True)

    def clear(self) -> None:
        self._pre.clear()
        self._post.clear()
        if self._s._pipe is not None:
            self._s._pipe.clear_hooks()

    def run_pre(self, stage: str) -> None:
        for st, fn in list(self._pre):
            if st in ("*", stage):
                fn(self._s)

    def run_post(self, stage: str, result) -> None:
        for st, fn in list(self._post):
            if st in ("*", stage):
                fn(self._s, result)

    def __repr__(self):
        return (f"Hooks(pre={[(s, getattr(f, '__name__', f)) for s, f in self._pre]}, "
                f"post={[(s, getattr(f, '__name__', f)) for s, f in self._post]})")


# ---------------------------------------------------------------- session

class Session:
    """One run directory of the flow, driven from Python. `config`: the
    flow.yaml; `out_dir`: the run directory; `settings`: {option: value}
    set as arguments; `load_kw`: FlowConfig.load's keyword arguments (the
    command line's flags: abc_script_file, run_sta, run_vt_swap,
    partitions, ...)."""

    def __init__(self, config, out_dir, settings: Optional[dict] = None,
                 strict: bool = False, **load_kw):
        self.config, self.out_dir = Path(config), Path(out_dir)
        self.strict, self.load_kw = strict, load_kw
        self._given: Dict[str, Any] = {}
        self._pack_given: Dict[str, Any] = {}
        self._set_at: Dict[str, str] = {}
        self._scoped: List[ScopedSetting] = []
        self._cfg = None
        self._cfg_key = None
        self._base = None
        self._settings_cache = None
        self._run_cfg = None
        self._run_key = None
        self._done: List[str] = []
        self._running: Optional[str] = None       # the stage whose code is running now
        self._in_run = False                      # inside s.run() / run_stage()
        self._inputs: Dict[str, str] = {}
        self._start_key = None
        self.results: Dict[str, StageResult] = {}
        self._executed: List[list] = []
        self._libtable = None
        self._pipe = None
        self._hooks = None
        self._npass = 0
        self._replaced: List[dict] = []           # run_pass(replace=True): not replayable
        self._git = None                          # the circt-abc commit (once per session)
        self.db = AttrDB(self)
        self._hooks = Hooks(self)
        for k, v in (settings or {}).items():
            self._set(k, v, where="settings")

    def __repr__(self):
        return (f"Session({str(self.config)!r}, {str(self.out_dir)!r}; done: "
                f"{', '.join(self._done) or 'nothing'}; {len(self._given)} option(s) set)")

    def configure(self, **load_kw) -> None:
        """Change load arguments (abc_script_file, run_vt_swap, run_sta,
        partitions, ...: FlowConfig.load's keywords) before any stage ran."""
        from .config import FlowConfig
        import inspect
        known = set(inspect.signature(FlowConfig.load).parameters) - {
            "cls", "config_path", "out_dir", "settings", "strict", "env"}
        bad = sorted(set(load_kw) - known)
        if bad:
            raise SessionError(f"unknown load argument(s) {', '.join(bad)}; known: "
                               f"{', '.join(sorted(known))}")
        # a load argument is read from the stage FIELD_STAGE gives its field
        # on (run_vt_swap: vt_swap, partitions: from elaborate): refused once
        # that stage has run or is running, else applied (the configuration
        # of the next stage is resolved with it)
        for k in sorted(load_kw):
            self._refuse_closed(FIELD_STAGE.get(k, "elaborate"), f"load argument {k}")
        self.load_kw.update(load_kw)
        self._cfg = None
        if not self._done and self._running is None and self._run_cfg is not None \
                and not self._in_run:
            self._run_cfg = None

    # ------------------------------------------------ options
    def set(self, name: str, value: Any) -> None:
        """Set an option (an argument-level source); unknown names and bad
        values raise options.OptionError at once."""
        self._set(name, value)

    def get(self, name: str) -> Any:
        return self.db.get(name)

    def reset(self, name: str) -> None:
        self._reset(name)

    def report(self, pattern: str = ""):
        """[(name, value, source, help)] (short form; report_settings()
        returns the full report)."""
        s = self._settings()
        return [(n, s[n], s.source(n), o.help)
                for n, o in sorted(options.registry().items()) if pattern in n]

    def _set(self, name: str, value: Any, depth: int = 0, where: Optional[str] = None) -> None:
        o = options.lookup(name, "Session.set")
        if not o.settable:
            raise options.not_settable(o, "Session.set")
        v = o.parse(value, "Session.set")
        self._check_open(name)
        if v is None:
            self._given.pop(name, None)
            self._set_at.pop(name, None)
        else:
            self._given[name] = v
            self._set_at[name] = where or _caller()
        self._changed()

    def _reset(self, name: str) -> None:
        reg = options.registry()
        if name in reg:
            names = [name]
        else:
            names = [n for n in reg if n.startswith(name.rstrip(".") + ".")]
            if not names:
                options.lookup(name, "Session.reset")
        for n in names:
            if n in self._given:
                self._check_open(n)
                self._given.pop(n)
                self._set_at.pop(n, None)
        self._changed()

    def _closed(self, stage: str) -> bool:
        """`stage` has run, or is running, or comes before the stage that is
        running now (a pass hook): a setting it reads can no longer act."""
        if stage in self._done:
            return True
        if self._running is None:
            return False
        names = stage_names()
        return names.index(stage) <= names.index(self._running)

    def _refuse_closed(self, stage: str, what: str) -> None:
        if stage in self._done:
            raise SessionError(
                f"{what} is read by stage {stage}, which has already run in {self.out_dir}: "
                f"it could not take effect. Re-open the stage first (s.invalidate({stage!r})) "
                f"or use a new run directory")
        if self._closed(stage):
            raise SessionError(
                f"{what} is read by stage {stage}, and stage {self._running} is running now "
                f"(this is a hook inside it): it could not take effect. Set it before "
                f"{stage} starts (in a pre hook of {stage} or earlier), or for a later stage only")

    def _check_open(self, name: str) -> None:
        self._refuse_closed(stage_of(name), name)

    def _changed(self) -> None:
        self._settings_cache = None

    def _add_scoped(self, item: ScopedSetting) -> None:
        if not all(hasattr(item, f) for f in ("kind", "targets", "command")):
            raise TypeError("add_scoped takes a ScopedSetting")
        self._refuse_closed("map_datapath", f"scoped setting {getattr(item, 'command', '?')}")
        self._scoped.append(item)
        self._changed()

    def _reset_scoped(self, command: Optional[str] = None) -> None:
        self._scoped = [x for x in self._scoped if command is not None and x.command != command]
        self._changed()

    def _effective_given(self) -> Dict[str, Any]:
        """The options set, plus what the scoped settings materialise to
        (circt_flow.query.materialise)."""
        given = dict(self._given)
        if not self._scoped:
            return given
        try:
            from . import query
        except ImportError:
            raise SessionError("scoped settings need circt_flow.query") from None
        for k, v in query.materialise(self, tuple(self._scoped)).items():
            if k in given and given[k] != v:
                raise options.OptionError(
                    f"{k} is set to {given[k]!r} and, by scoped settings, to {v!r}: keep one")
            given[k] = v
            self._set_at.setdefault(k, "scoped")
        return given

    @staticmethod
    def _key(given) -> str:
        return json.dumps({k: options._jsonable(v) for k, v in sorted(given.items())},
                          sort_keys=True)

    def _load(self, force: bool = False):
        """The FlowConfig of the present settings (loaded again only when
        they changed)."""
        given = self._effective_given()
        key = self._cfg_key_of(given)
        if self._cfg is not None and key == self._cfg_key and not force:
            return self._cfg
        from .config import FlowConfig
        cfg = FlowConfig.load(self.config, self.out_dir, settings=dict(given) or None,
                              strict=self.strict, **self.load_kw)
        self._cfg, self._cfg_key = cfg, key
        self._settings_cache = (self._key(given), cfg.settings)
        if self._base is None:
            import yaml
            raw = yaml.safe_load(Path(self.config).read_text()) or {}
            self._base = (raw, cfg.design.flow_defaults, str(self.config),
                          f"tech pack {cfg.design.profile}")
        return cfg

    def _cfg_key_of(self, given) -> str:
        """The settings and the load arguments a FlowConfig is made from."""
        return self._key(given) + "|" + json.dumps(
            {k: str(v) for k, v in sorted(self.load_kw.items())}, sort_keys=True)

    @property
    def cfg(self):
        return self._load()

    def _settings(self):
        """The resolved options (cheap after the first config load)."""
        given = self._effective_given()
        key = self._key(given)
        if self._settings_cache is not None and self._settings_cache[0] == key:
            return self._settings_cache[1]
        if self._base is None:
            return self._load().settings
        from .config import resolve_settings
        raw, pack, where, pack_where = self._base
        s = resolve_settings(raw, pack, dict(given) or None, os.environ, self.strict,
                             where, pack_where)
        self._settings_cache = (key, s)
        return s

    def _explicit(self) -> Dict[str, Any]:
        s = self._settings()
        return {k: options._jsonable(v) for k, v in sorted(s.overridden().items())}

    # ------------------------------------------------ settings record
    def _extra(self) -> dict:
        extra = {"pack_options": {k: options._jsonable(v) for k, v in sorted(self._pack_given.items())},
                 "set_at": dict(sorted(self._set_at.items())),
                 "scoped": [_scoped_record(x) for x in self._scoped]}
        if self._replaced:
            # like hooks: the run is not reproducible from the options alone
            extra["run_pass_replaced"] = {
                "reproducible_from_settings": False,
                "note": "run_pass(replace=True) rewrote a stage's output: the options of this "
                        "record do not give that file again",
                "passes": [dict(x) for x in self._replaced]}
        try:
            from .pipeline_edit import run_record       # edits, hooks
        except ImportError:
            return extra
        return {**extra, **run_record(self)}

    def _load_edits(self, rec: dict) -> None:
        if rec.get("pipeline_edits"):
            self.pipeline.load_edits(rec["pipeline_edits"])

    @contextlib.contextmanager
    def _active(self):
        """The process-wide records (stages.EXECUTED, the settings record's
        session fields) hold this session's while it works."""
        from . import stages
        saved_extra = dict(provenance_settings.EXTRA)
        saved_executed = list(stages.EXECUTED)
        provenance_settings.EXTRA.clear()
        provenance_settings.EXTRA.update(self._extra())
        stages.EXECUTED[:] = self._executed
        try:
            yield
        finally:
            # this session's commands stay its own; an enclosing session (a
            # session run inside another one's hook) gets its record back
            self._executed = list(stages.EXECUTED)
            stages.EXECUTED[:] = saved_executed
            provenance_settings.EXTRA.clear()
            provenance_settings.EXTRA.update(saved_extra)

    def write_settings(self, path=None) -> Path:
        """A settings record of the present settings at `path` (default
        <out>/session_settings.json; the stages write <out>/settings.json
        themselves)."""
        cfg = self._load()
        with self._active():
            return provenance_settings.write(
                cfg, "session", path or Path(self.out_dir) / "session_settings.json")

    @classmethod
    def from_settings(cls, config, out_dir, settings_json, **kw) -> "Session":
        """A new session with the options of an earlier run's settings.json
        (every option not at its built-in default there, as arguments) and
        its scoped settings."""
        rec = provenance_settings.read(settings_json)
        lost = [k for k in ("hooks", "run_pass_replaced")
                if isinstance(rec.get(k), dict)
                and rec[k].get("reproducible_from_settings") is False]
        if lost:
            what = {"hooks": "hooks (Python code)",
                    "run_pass_replaced": "run_pass(replace=True) rewrites"}
            print(f"[circt_flow] WARNING: {settings_json} records "
                  f"{' and '.join(what[k] for k in lost)} that a settings record cannot "
                  "replay: the new session has the options, scoped settings and pipeline "
                  "edits only; register the hooks / repeat the run_pass calls in the "
                  "script to reproduce that run", file=sys.stderr)
        s = cls(config, out_dir, settings=provenance_settings.explicit(rec), **kw)
        for d in rec.get("scoped", ()):
            s._scoped.append(_scoped_from(d))
        s._pack_given.update(rec.get("pack_options", {}))
        s._load_edits(rec)
        return s

    # ------------------------------------------------ resumability
    def _write_state(self) -> None:
        load_kw = {}
        for k, v in self.load_kw.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                load_kw[k] = v
            elif isinstance(v, Path):
                load_kw[k] = str(v)
        st = {"format": 1, "config": str(self.config.resolve()), "cwd": os.getcwd(),
              "strict": self.strict, "load_kw": load_kw,
              "dropped_load_kw": sorted(k for k in self.load_kw if k not in load_kw),
              "given": {k: options._jsonable(v) for k, v in sorted(self._given.items())},
              "pack_given": {k: options._jsonable(v) for k, v in sorted(self._pack_given.items())},
              "set_at": dict(sorted(self._set_at.items())),
              "scoped": [_scoped_record(x) for x in self._scoped],
              "done": list(self._done),
              "in_place_inputs": dict(self._inputs),
              "results": [self.results[n].as_dict() for n in self._done if n in self.results],
              "executed": self._executed,
              "run_pass_replaced": [dict(x) for x in self._replaced],
              "inputs": self._input_record()}
        p = Path(self.out_dir) / STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1) + "\n")
        os.replace(tmp, p)

    def _input_record(self) -> dict:
        """What the stages done so far were made from, besides the options
        (session.json "inputs", compared by resume): the recipe files (flow.yaml,
        tech pack, manifest, ABC script, mode SDCs; path and sha256), the legacy
        CIRCT_FLOW_* / CIRCT_VT_* variables of the environment, the circt-abc
        commit."""
        cfg = self._load()
        if self._git is None:
            self._git = provenance_settings._git(cfg.root)
        env = {k: v for k, v in sorted(os.environ.items())
               if k.startswith(options.LEGACY_PREFIXES) and not options.is_interface_var(k)}
        return {"recipe_files": provenance_settings.recipe_files(cfg), "legacy_env": env,
                "circt_abc": dict(self._git)}

    def _check_resumed(self, st: dict, allow_changed: bool) -> None:
        """The inputs recorded in session.json against the
        present ones. A recipe file of the same path with other content, a
        changed legacy variable, another circt-abc commit: refused (the done
        stages were made from the old ones), or with allow_changed=True a
        warning. A file whose path changed was chosen again (a load argument
        given to resume) and is not compared."""
        rec = st.get("inputs")
        if rec is None:
            print(f"[circt_flow] WARNING: {self.out_dir / STATE_FILE} predates the input record: "
                  "flow.yaml, environment and circt-abc commit are not compared", file=sys.stderr)
            return
        now = self._input_record()
        diffs = []
        for role, old in sorted(rec.get("recipe_files", {}).items()):
            new = now["recipe_files"].get(role)
            if new is not None and new["path"] == old["path"] and new["sha256"] != old["sha256"]:
                diffs.append(f"{role} {old['path']} changed")
            elif new is None and old.get("sha256") is not None and not Path(old["path"]).exists():
                diffs.append(f"{role} {old['path']} is gone")
        oe, ne = rec.get("legacy_env", {}), now["legacy_env"]
        for k in sorted(set(oe) | set(ne)):
            if oe.get(k) != ne.get(k):
                diffs.append(f"environment {k}: {oe.get(k)!r} then, {ne.get(k)!r} now")
        oc, nc = rec.get("circt_abc", {}), now["circt_abc"]
        if oc.get("commit") != nc.get("commit"):
            diffs.append(f"circt-abc commit {str(oc.get('commit'))[:12]} then, "
                         f"{str(nc.get('commit'))[:12]} now")
        elif oc.get("dirty") or nc.get("dirty"):
            print("[circt_flow] WARNING: the circt-abc tree has uncommitted changes: resume "
                  "cannot tell whether they changed since the stages done ran", file=sys.stderr)
        if not diffs:
            return
        msg = (f"{self.out_dir} was made from other inputs than the present ones: "
               + "; ".join(diffs))
        if not allow_changed:
            raise SessionError(msg + ". The stages done would not be what these inputs give: "
                               "use a new run directory, or resume with "
                               "allow_changed_inputs=True (--allow-changed-inputs) to continue anyway")
        print(f"[circt_flow] WARNING: {msg} (allow_changed_inputs: continued)", file=sys.stderr)

    @classmethod
    def resume(cls, out_dir, settings: Optional[dict] = None,
               allow_changed_inputs: bool = False, **kw) -> "Session":
        """Re-open a run directory a session wrote (<out>/session.json): the
        same flow.yaml, options, scoped settings and load arguments, with the
        stages already done marked done. `settings` are set after that (an
        option of a done stage is refused); `kw` override load arguments.
        Inputs that changed since (recipe file contents, legacy variables,
        the circt-abc commit) are refused unless `allow_changed_inputs`."""
        out_dir = Path(out_dir)
        p = out_dir / STATE_FILE
        if not p.is_file():
            raise SessionError(f"{p} not found: {out_dir} was not written by a session "
                               "(a command-line run directory has no stage record; rerun it)")
        st = json.loads(p.read_text())
        if st.get("format") != 1:
            raise SessionError(f"{p}: session record format {st.get('format')!r} is not "
                               "supported (1): rerun into a new directory")
        if st.get("dropped_load_kw") and not all(k in kw for k in st["dropped_load_kw"]):
            raise SessionError(f"{out_dir} was run with load argument(s) "
                               f"{', '.join(st['dropped_load_kw'])} that are not recorded; "
                               "pass them again to resume")
        load_kw = dict(st["load_kw"])
        for k, v in list(load_kw.items()):
            # a relative path given then is relative to the directory it was run from
            if isinstance(v, str) and k.endswith(("_file", "_dir", "_workdir")) \
                    and not os.path.isabs(v) and Path(st["cwd"]).resolve() != Path.cwd().resolve():
                load_kw[k] = str(Path(st["cwd"]) / v)
        load_kw.update(kw)
        strict = load_kw.pop("strict", st.get("strict", False))
        s = cls(st["config"], out_dir, settings=None, strict=strict, **load_kw)
        for k, v in st["given"].items():
            s._set(k, v, where=st["set_at"].get(k, "resumed"))
        s._scoped = [_scoped_from(d) for d in st.get("scoped", ())]
        s._pack_given = dict(st.get("pack_given", {}))
        s._done = list(st["done"])
        s._inputs = dict(st.get("in_place_inputs", {}))
        s.results = {d["stage"]: StageResult.from_dict(d) for d in st.get("results", ())}
        s._executed = [list(c) for c in st.get("executed", ())]
        s._replaced = [dict(x) for x in st.get("run_pass_replaced", ())]
        s._check_resumed(st, allow_changed_inputs)
        rec = out_dir / "settings.json"
        if rec.is_file():
            r = provenance_settings.read(rec)
            s._load_edits(r)
            if r.get("scoped_applied"):
                # the per-target record of the structural stage that ran
                # before (scoping keeps it per output directory)
                from . import scoping
                scoping.APPLIED[str(out_dir.resolve())] = list(r["scoped_applied"])
        for k, v in (settings or {}).items():
            s._set(k, v)
        return s

    def invalidate(self, stage: str) -> None:
        """Mark `stage` and every stage after it as not run (their options can
        be set again; their outputs are rewritten when they run)."""
        names = stage_names()
        if stage not in names:
            raise SessionError(f"unknown stage {stage!r}{_near(stage, names)}")
        i = names.index(stage)
        self._done = [n for n in self._done if names.index(n) < i]
        for n in names[i:]:
            self.results.pop(n, None)

    def done(self, stage: str) -> bool:
        return stage in self._done

    def require(self, stage: str, what: str = "this") -> None:
        if stage not in self._done:
            raise SessionError(f"{what} needs stage {stage} run first (s.{stage}())")

    def require_snapshot(self, stage: str) -> None:
        """For a design query: `stage` ran in this session (or the run
        directory it resumed), or its output is on disk (a run directory
        another run wrote, e.g. the command line's)."""
        if stage in self._done:
            return
        snap = {"elaborate": "core", "map_datapath": "struct", "syn_map": "openroad_netlist"}
        if stage in snap and Path(self.artifacts[snap[stage]]).exists():
            return
        self.require(stage, "this query")

    # ------------------------------------------------ stages
    @property
    def stages(self) -> tuple:
        return stage_names()

    def _prepared(self):
        cfg = self._load()
        if cfg.design.power is not None:
            raise SessionError("a design with power domains runs only as a whole flow: s.run()")
        if self._run_cfg is None:
            rc = _pipeline().prepare(cfg, executed=self._executed)
            self._run_cfg, self._run_key = rc, self._cfg_key
        elif self._run_key != self._cfg_key:
            self._run_cfg, self._run_key = _pipeline().calibrate(cfg), self._cfg_key
        return self._run_cfg

    def run_stage(self, name: str, force: bool = True) -> StageResult:
        """Run one stage. `force` (the named methods): a stage that is off in
        this config (vt_swap / sta without --vt-swap / --sta and without an
        enabled section) runs anyway, as --vt-swap / --sta would make it;
        False: it is skipped, as in the full flow."""
        names = stage_names()
        if name not in names:
            raise SessionError(f"unknown stage {name!r}{_near(name, names)}; "
                               f"stages: {', '.join(names)}")
        missing = [n for n in NEEDS[name] if n not in self._done]
        if missing:
            raise SessionError(f"stage {name} needs {', '.join(missing)} run first")
        fn = dict(_pipeline().SYNTH_STAGES + _pipeline().PNR_STAGES)[name]
        with self._active(), self._running_flow():
            self._prepared()
            self.invalidate(name)
            return self._exec(name, fn, None, force=force, single=True)

    @contextlib.contextmanager
    def _running_flow(self):
        outer = self._in_run
        self._in_run = True
        try:
            yield
        finally:
            self._in_run = outer

    def _check_inputs(self, name: str, cfg) -> None:
        need = {"map_datapath": [cfg.core_mlir], "syn_map": [cfg.struct_mlir],
                "fanout": [cfg.openroad_netlist], "vt_swap": [cfg.openroad_netlist],
                "sta": [cfg.openroad_netlist], "place_and_route": [cfg.openroad_netlist]}
        gone = [str(p) for p in need.get(name, ()) if not Path(p).exists()]
        if gone:
            raise SessionError(f"stage {name}: input(s) missing: {', '.join(gone)} "
                               "(the earlier stage's output was removed? s.invalidate it and rerun)")

    def _on_stage(self, name, fn, cfg):
        if self._run_cfg is None:                 # the calibrated cfg of pipeline.run
            self._run_cfg, self._run_key = cfg, self._cfg_key
        # after a hook changed an option, the reloaded config replaces
        # pipeline.run's (the stages read only the cfg they are given)
        return self._exec(name, fn, self._run_cfg, force=False, single=False)

    # ------------------------------------------------ stages that edit the netlist in place
    # fanout and vt_swap rewrite <top>_openroad.v in place: a stage run again
    # (after s.invalidate) must start from the netlist it got the first time,
    # not from its own (or a later stage's) output. Its input is kept in
    # <out>/session/<stage>_input_openroad.v until the stage before it runs
    # again.
    IN_PLACE = ("fanout", "vt_swap")

    def _in_place_input(self, name: str, cfg) -> None:
        if name not in self.IN_PLACE:
            return
        net = Path(cfg.openroad_netlist)
        keep = Path(cfg.out_dir) / "session" / f"{name}_input_openroad.v"
        if name in self._inputs and keep.is_file():
            shutil.copyfile(keep, net)                # the true input again
        elif net.is_file():
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(net, keep)
            self._inputs[name] = str(keep)

    def _drop_in_place_after(self, name: str) -> None:
        names = stage_names()
        for st in list(self._inputs):
            if names.index(st) > names.index(name):
                with contextlib.suppress(OSError):
                    Path(self._inputs[st]).unlink()
                del self._inputs[st]

    def _stage_cfg(self, given_cfg):
        """The configuration a stage runs with, resolved AFTER its pre hooks
        (an option a hook set applies to this stage and the later ones; one
        of a stage already run was refused by the hook's s.db write)."""
        key = self._cfg_key_of(self._effective_given())
        if given_cfg is not None and key == self._run_key:
            return given_cfg
        return self._prepared()

    def _exec(self, name, fn, given_cfg, force: bool, single: bool) -> StageResult:
        from . import stages
        self._hooks.run_pre(name)
        cfg = self._stage_cfg(given_cfg)
        if single:
            self._check_inputs(name, cfg)
        self._in_place_input(name, cfg)
        n0 = len(stages.EXECUTED)
        t0 = time.time()
        if name == "place_and_route":
            fn = self._pnr_stage(fn, cfg)
        self._running = name
        try:
            with self._pass_context(name, cfg):
                status = fn(cfg, force=True) if force else fn(cfg)
        finally:
            self._running = None
        res = StageResult(name, status or "done",
                          commands=[list(c) for c in stages.EXECUTED[n0:]],
                          settings={k: options._jsonable(v) for k, v in
                                    sorted(cfg.settings.overridden().items())},
                          seconds=time.time() - t0)
        self._collect(res, cfg)
        self._executed = list(stages.EXECUTED)
        self._done = [n for n in self._done if n != name] + [name]
        self.results[name] = res
        self._drop_in_place_after(name)
        if single:
            provenance_settings.write(cfg, stage=PROVENANCE_LABEL.get(name, name))
        self._write_state()
        self._hooks.run_post(name, res)
        return res

    # ------------------------------------------------ the tech pack's own P&R options
    def _pack_root(self, cfg=None):
        cfg = cfg or self._load()
        return getattr(cfg.design, "pack_root", None)

    def _pack_pnr(self, cfg=None) -> dict:
        cfg = cfg or self._load()
        return dict(getattr(cfg.design, "pack_pnr", None) or {})

    def _pack_registry(self, cfg=None) -> dict:
        """{name: Option} of the tech pack's own P&R options (the registry its
        techpack.yaml's pnr.settings_registry names); {} for a pack without one."""
        from . import pack_options
        root, pnr = self._pack_root(cfg), self._pack_pnr(cfg)
        if not pack_options.has_registry(root, pnr):
            return {}
        return pack_options.load(root, pnr)

    def set_pack_option(self, name: str, value: Any) -> None:
        """Set one of the tech pack's own P&R options (its registry;
        `python3 -m circt_flow options --pack <pack>` lists them); validated
        now, handed to the pack's P&R command as a settings file by
        place_and_route(). An option the pack shares with circt-abc (the
        registry marks it shared) is set through s.db and handed over too."""
        from . import pack_options
        reg = self._pack_registry()
        if not reg:
            raise SessionError(f"the tech pack ({self._pack_root()}) declares no P&R option "
                               "registry (pnr.settings_registry in its techpack.yaml)")
        o = reg.get(name)
        if o is None:
            raise options.OptionError(f"unknown pack option {name!r}{_near(name, reg)}")
        self._refuse_closed("place_and_route", name)
        self._pack_given[name] = pack_options.parse_value(o, value, "set_pack_option")
        self._set_at[f"pack:{name}"] = _caller()

    def _pack_given_all(self, reg=None) -> Dict[str, Any]:
        """The pack options this session sets: shared ones set through s.db
        and set_pack_option's values (over them)."""
        if reg is None:
            reg = self._pack_registry() if (self._pack_given or self._given) else {}
        out = {k: v for k, v in self._given.items() if k in reg}
        out.update(self._pack_given)
        return out

    def _pack_handover(self, cfg=None):
        """pack_options.handover for this session: ({name: value}, {name: source})."""
        from . import pack_options
        root, pnr = self._pack_root(cfg), self._pack_pnr(cfg)
        if not pack_options.has_registry(root, pnr):
            return {}, {}
        given = self._pack_given_all(pack_options.load(root, pnr))
        srcs = {k: ("s.db (an option shared with the flow)" if k not in self._pack_given
                    else "set_pack_option") for k in given}
        try:
            return pack_options.handover(root, pnr, given, srcs)
        except options.OptionError as e:
            raise SessionError(str(e)) from None

    @property
    def pack_options(self) -> Dict[str, Any]:
        """The pack P&R options place_and_route hands to the pack's command:
        the profile's pnr.settings_defaults, this session's values over them
        (set_pack_option; shared options set through s.db) and the pack's
        recipe directory when an option needs it (pack_options.handover; the
        same values the stage writes). s.pack_option_sources says where each
        comes from."""
        return {k: options._jsonable(v) for k, v in self._pack_handover()[0].items()}

    @property
    def pack_option_sources(self) -> Dict[str, str]:
        """{name: source} of s.pack_options: "pnr.settings_defaults (the pack
        profile)", "set_pack_option", "s.db (an option shared with the flow)"
        or "registry recipe_dir (for ...)"."""
        return self._pack_handover()[1]

    def write_pack_settings(self, path=None) -> Path:
        """s.pack_options as the pack's settings file (default
        <out>/pnr/pack_settings.json, the file place_and_route writes), e.g.
        to run the pack's own P&R entry with `--settings FILE` outside the
        flow."""
        from . import pack_options
        root, pnr = self._pack_root(), self._pack_pnr()
        if not self._pack_registry():
            raise SessionError(f"the tech pack ({root}) declares no P&R option registry "
                               "(pnr.settings_registry in its techpack.yaml)")
        path = (Path(path) if path is not None
                else Path(self.out_dir) / "pnr" / pack_options.SETTINGS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        return pack_options.write_settings(root, self._pack_handover()[0], path, pnr)

    @staticmethod
    def _pnr_route(cfg) -> str:
        """How stages.orfs_pnr runs this config's P&R: "ladder" (pnr.ladder),
        "pack" (the pack's own command, pnr.command) or "orfs"."""
        if (cfg.design.pnr or {}).get("ladder"):
            return "ladder"
        return "pack" if "command" in (cfg.design.pack_pnr or {}) else "orfs"

    def _pnr_stage(self, fn, cfg):
        """The P&R stage with this session's pack P&R options in the config
        (cfg.design.pack_settings): stages.pack_command writes them over the
        profile's settings_defaults to <out>/pack_settings.json and gives the
        pack command `--settings` (pack_options.handover), also on every job
        of the closure ladder, which compares the file's content and not its
        directory. Without a pack command (ORFS) options set with
        set_pack_option are refused; options shared with circt-abc set
        through s.db act through the flow's pnr: section there."""
        if "command" not in self._pack_pnr(cfg):
            if self._pack_given:
                raise SessionError(
                    f"pack option(s) {', '.join(sorted(self._pack_given))} (set_pack_option) "
                    f"reach only a tech pack's own P&R command (pnr.command); this run uses "
                    f"ORFS. Options shared with circt-abc can be set through s.db (they act "
                    f"through the flow's pnr: section)")
            return fn
        values, src = self._pack_handover(cfg)        # refused here, before the stage runs
        given = self._pack_given_all()
        if not given:
            return fn          # the pack profile's settings_defaults alone: stages.pack_command
        noted = {k: f"session: {s_}" for k, s_ in src.items()
                 if s_.startswith("registry recipe_dir")}
        if noted:
            self._set_at.update({f"pack:{k}": v for k, v in noted.items()})
            provenance_settings.EXTRA.update({k: v for k, v in self._extra().items() if v})
        import dataclasses
        from . import stages

        def stage(c, force=False):
            d = dataclasses.replace(c.design, pack_settings=dict(given))
            stages.orfs_pnr(dataclasses.replace(c, design=d))
        return stage

    @contextlib.contextmanager
    def _pass_context(self, name: str, cfg=None):
        if self._pipe is None or not self._pipe.active():
            yield
            return
        with self._pipe.applied(name, cfg):
            yield

    def _lib_table(self, cfg):
        if self._libtable is None:
            from .pipeline import setup_libs
            from .postlayout import liberty_table
            try:
                self._libtable = liberty_table(setup_libs(cfg.design))
            except (OSError, ValueError):
                self._libtable = {}
        return self._libtable

    def _collect(self, res: StageResult, cfg) -> None:
        out = Path(cfg.out_dir)

        def have(**kw):
            return {k: str(v) for k, v in kw.items() if v is not None and Path(v).exists()}
        n = res.stage
        if res.status == "skipped":
            return
        if n == "elaborate":
            res.produced = have(core=cfg.core_mlir, stubs=cfg.stubs)
            res.metrics = results_mod.ir_metrics(cfg.core_mlir)
        elif n == "map_datapath":
            res.produced = have(struct=cfg.struct_mlir)
            res.metrics = results_mod.ir_metrics(cfg.struct_mlir)
        elif n in ("syn_map", "fanout", "vt_swap"):
            res.produced = have(mapped=cfg.mapped_mlir if n == "syn_map" else None,
                                netlist=cfg.netlist if n == "syn_map" else None,
                                openroad_netlist=cfg.openroad_netlist,
                                fanout_json=out / "fanout.json" if n == "fanout" else None,
                                vt_swap_json=out / "vt_swap.json" if n == "vt_swap" else None)
            res.metrics = results_mod.netlist_metrics(cfg.openroad_netlist, self._lib_table(cfg))
            if n == "fanout":
                res.metrics.update(results_mod.fanout_metrics(out))
            if n == "vt_swap":
                res.metrics.update(results_mod.vt_swap_metrics(out))
        elif n == "sta":
            res.produced = have(summary=out / "sta" / "summary.json")
            res.metrics = results_mod.sta_metrics(out)
        elif n == "place_and_route":
            res.produced = have(pnr_dir=cfg.pnr_dir)
            res.metrics = results_mod.pnr_metrics(cfg.pnr_dir)

    def elaborate(self) -> StageResult:
        """Front end, LLHD lowering, de-aggregation, cell stubs."""
        return self.run_stage("elaborate")

    def map_datapath(self) -> StageResult:
        """Registers, clock gating, datapath operators, structural mapper."""
        return self.run_stage("map_datapath")

    def syn_map(self) -> StageResult:
        """Flatten, merge, ABC mapping, netlist export (OpenSTA form)."""
        return self.run_stage("syn_map")

    def fanout(self) -> StageResult:
        """High-fanout buffering (fanout_buffer:)."""
        return self.run_stage("fanout")

    def vt_swap(self) -> StageResult:
        """Threshold-flavour swap with OpenSTA (as --vt-swap)."""
        return self.run_stage("vt_swap")

    def sta(self) -> StageResult:
        """Multi-corner multi-mode STA (as --sta)."""
        return self.run_stage("sta")

    def place_and_route(self) -> StageResult:
        """P&R of the OpenSTA netlist (the flow.yaml's pnr:; stages.orfs_pnr)."""
        return self.run_stage("place_and_route")

    def run(self, pnr: bool = False, start: Optional[str] = None,
            stop: Optional[str] = None) -> FlowResult:
        """The flow. Without start/stop on a fresh session: pipeline.run (the
        command line's code path, byte-identical outputs), stages run only
        when the config wants them. With start/stop, or on a resumed
        session: the stages from `start` (default: the first not done) to
        `stop`, each as run_stage(name, force=False)."""
        names = list(stage_names())
        synth = [n for n, _ in _pipeline().SYNTH_STAGES]
        if start is None and stop is None and not self._done:
            cfg = self._load()
            if cfg.design.power is not None and (self._hooks._pre or self._hooks._post
                                                 or (self._pipe is not None and self._pipe.active())):
                raise SessionError("a design with power domains runs as a whole (power.run runs its "
                                   "own sub-flows): stage hooks and pipeline edits are not applied "
                                   "there; clear them (s.hooks.clear(), s.pipeline.clear())")
            with self._active(), self._running_flow():
                self._run_cfg = None
                self._start_key = self._cfg_key
                _pipeline().run(cfg, pnr=pnr, on_stage=self._on_stage)
                if self.results and self._run_key != self._start_key:
                    # a hook changed an option during the run: the record is
                    # the configuration the last stages ran with
                    provenance_settings.write(self._run_cfg, stage="pnr" if pnr else "synthesis")
            note = "" if self.results else \
                "power-domain design: run by power.run (no per-stage results)"
            return FlowResult([self.results[n] for n in names if n in self.results], note)
        last = stop or ("place_and_route" if pnr else synth[-1])
        if last not in names:
            raise SessionError(f"unknown stage {last!r}{_near(last, names)}")
        first = start or next((n for n in names if n not in self._done), None)
        if first is None:
            return FlowResult([self.results[n] for n in names if n in self.results])
        if first not in names:
            raise SessionError(f"unknown stage {first!r}{_near(first, names)}")
        for n in names[names.index(first):names.index(last) + 1]:
            self.run_stage(n, force=False)
        return FlowResult([self.results[n] for n in names if n in self.results])

    # ------------------------------------------------ passes
    @property
    def artifacts(self) -> Dict[str, Path]:
        cfg = self._load()
        return {"core": cfg.core_mlir, "struct": cfg.struct_mlir, "flat": cfg.flat_mlir,
                "mapped": cfg.mapped_mlir, "netlist": cfg.netlist,
                "openroad_netlist": cfg.openroad_netlist}

    @property
    def current_ir(self) -> Path:
        """The IR file the next stage reads: the core IR after elaborate,
        the structural IR after map_datapath (after syn_map the design is a
        netlist: s.artifacts)."""
        a = self.artifacts
        if "map_datapath" in self._done and "syn_map" not in self._done:
            return a["struct"]
        if "elaborate" in self._done and "map_datapath" not in self._done:
            return a["core"]
        raise SessionError("no IR between stages now: the current IR exists after elaborate "
                           "(core) and after map_datapath (struct), before syn_map")

    def run_pass(self, name: str, input=None, output=None, replace: bool = False,
                 **pass_options) -> PassResult:
        """Run one circt-abc-opt pass. `input`: an IR file (default: the
        current IR); `output`: default <out>/passes/<nn>_<name>.mlir;
        `replace`: the result replaces the input file (for the current IR:
        the next stage reads it; recorded: settings.json "run_pass_replaced"
        marks the run not reproducible from its settings, as hooks do). The
        pass name and its option names (Python spelling: _ for -) are checked
        against the tool's own pass list."""
        from . import passes, stages
        cfg = self._load()
        spec = passes.pass_arg(cfg.circt_abc_opt, name, pass_options)
        src = Path(input) if input is not None else self.current_ir
        if not src.is_file():
            raise SessionError(f"run_pass {name}: input {src} does not exist")
        self._npass += 1
        dst = Path(output) if output is not None else \
            Path(cfg.out_dir) / "passes" / f"{self._npass:02d}_{name}.mlir"
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(cfg.circt_abc_opt), spec, str(src), "-o", str(dst)]
        t0 = time.time()
        with self._active():
            stages._run(cmd)
        replaced = None
        if replace:
            shutil.copyfile(dst, src)
            replaced = str(src)
            # recorded: settings.json marks the run not reproducible from settings
            self._replaced.append({"pass": name, "command": [str(c) for c in cmd],
                                   "replaced": replaced})
            if Path(self.out_dir).is_dir():
                self._write_state()
        return PassResult(name, cmd, str(src), str(dst), replaced, time.time() - t0)

    # ------------------------------------------------ pass pipeline and hooks
    # (circt_flow/pipeline_edit.py and circt_flow/hooks.py)
    @property
    def pipeline(self):
        """The pass pipeline as data (pipeline_edit.Pipeline)."""
        if self._pipe is None:
            try:
                from . import pipeline_edit
            except ImportError:
                raise NotImplementedError("s.pipeline: the editable pass pipeline needs "
                                          "circt_flow/pipeline_edit.py") from None
            self._pipe = pipeline_edit.Pipeline(self)
        return self._pipe

    @property
    def hooks(self) -> "Hooks":
        """Python callables before / after stages (and passes)."""
        return self._hooks

    # ------------------------------------------------ reports
    def report_settings(self, pattern: str = "", changed: bool = False) -> SettingsReport:
        return self.db.report(pattern, changed=changed)

    def report_changed(self) -> SettingsReport:
        return self.db.changed()

    def report_stages(self) -> str:
        rows = [f"run directory {self.out_dir}"]
        for n in stage_names():
            r = self.results.get(n)
            rows.append(f"  {n:16s} " + (r.text().split(": ", 1)[1] if r else
                                         ("done" if n in self._done else "not run")))
        return "\n".join(rows)

    def report_qor(self) -> dict:
        """The latest netlist and timing metrics ({cells, area, top_cells,
        wns_ns, tns_ns, hold_wns_ns, ...}; later stages win)."""
        out: Dict[str, Any] = {}
        for n in stage_names():
            if n in self.results:
                out.update({k: v for k, v in self.results[n].metrics.items() if v is not None})
        return out

    def report_passes(self) -> str:
        """The pass pipeline (Pipeline.text())."""
        return self.pipeline.text()

    def write_netlist(self, path) -> Path:
        """Copy the current OpenSTA-form netlist to `path`."""
        self.require("syn_map", "write_netlist")
        src = self.artifacts["openroad_netlist"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        return Path(path)

    # ------------------------------------------------ queries (query.py)
    def _query(self, what: str, *a, **kw):
        try:
            from . import query
        except ImportError:
            raise NotImplementedError(f"s.{what}(): object queries need "
                                      "circt_flow/query.py") from None
        return getattr(query, what)(self, *a, **kw)

    def flops(self, *a, **kw):
        return self._query("flops", *a, **kw)

    def adders(self, *a, **kw):
        return self._query("adders", *a, **kw)

    def multipliers(self, *a, **kw):
        return self._query("multipliers", *a, **kw)

    def shifters(self, *a, **kw):
        return self._query("shifters", *a, **kw)

    def icgs(self, *a, **kw):
        return self._query("icgs", *a, **kw)

    def modules(self, *a, **kw):
        return self._query("modules", *a, **kw)

    @property
    def design(self):
        return self._query("design_of")

    def gate_register(self, *a, **kw):
        """Data-following clock gating of selected register bits."""
        return self._query("gate_register", *a, **kw)

    def map_adders(self, *a, **kw):
        """Map selected adders (prefix form or cells)."""
        return self._query("map_adders", *a, **kw)
