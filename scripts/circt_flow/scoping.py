"""Scoped settings.

A scoped setting is a command applied to a selection of design objects:

    gate_register   register bit ranges   data-following clock gating
                                          (pass data-gate-enables; EXPERIMENTAL)
    map_adders      adders (width windows) a prefix network (map-prefix-adders)
                                          or a ripple of adder cells
                                          (map-adder-chains) on those adders

It is stored as a JSON-able record (ScopedSetting) in the registry option
`syn.scoped` (a list), so it travels like every other option: `--set`, a
flow.yaml `options:` entry, the Python session, settings.json and
`Session.from_settings`. Its targets are what the pass boundary receives,
resolved when the command was given (never live objects):

    register_bits   "[module/]reg@lo-hi"   (module only when the name is not
                                           unique: the pass matches any module)
    adders          "width:lo-hi" / "width:lo-"  (the passes select by width)

Applied or refused, per target: before the structural stage runs, every
target is checked against the core IR (a target that matches nothing, a
slice outside its register, a register that is not a plain hold mux,
overlapping slices: refused, OptionError); after it ran, against the pass's
own report on stderr (a slice the pass skipped, an instance that built
nothing: refused). `<out>/settings.json` records the result per target
(`scoped_applied`).

Legacy forms, translated to scoped entries and deprecated like the legacy
environment variables (warning now; refused under --strict and from
options.LEGACY_ENV_REMOVED_IN on):

    syn.clock_gating.data_gate  (CIRCT_FLOW_DATA_GATE)   reg@lo-hi[@zero|xor],...
    syn.adders.prefix2.*        (CIRCT_FLOW_PREFIX2_*)   a second prefix window
    syn.adders.cells2.*         (CIRCT_FLOW_ADDCH2_*)    a second cell window

Command-line identity: the scoped entries are emitted where the second
instances were (cells after syn.adders.cells.*, prefix after
syn.adders.prefix.*), in the same text, legacy translation first: a legacy
run builds the same pass command as before, and the scoped form of the same
settings builds the same command too.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import options
from .options import OptionError, Option

OPTION = "syn.scoped"

# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class Command:
    name: str
    kind: str                         # what the targets are
    params: Tuple[Option, ...]
    forms: Dict[str, Tuple[str, ...]] = field(default_factory=dict)  # form -> its params
    experimental: bool = False
    help: str = ""

    def param(self, name: str) -> Option:
        for p in self.params:
            if p.name == name:
                return p
        raise OptionError(f"{self.name}: unknown argument {name!r} (arguments: "
                          f"{', '.join(p.name for p in self.params)})")


COMMANDS: Dict[str, Command] = {
    "gate_register": Command(
        "gate_register", "register_bits",
        (Option("detect", "choice", "zero",
                "zero: a slice holds when it and its load are both zero; xor: when its load equals it",
                choices=("zero", "xor")),),
        experimental=True,
        help="data-following clock gating of register bit ranges (pass data-gate-enables, before "
             "clock-gate-inference); no sequential equivalence proof"),
    "map_adders": Command(
        "map_adders", "adders",
        (Option("form", "choice", "prefix", "prefix: a parallel-prefix network of cells "
                "(map-prefix-adders); cells: a ripple of two-output adder cells (map-adder-chains)",
                choices=("prefix", "cells")),
         Option("arch", "choice", "SKLANSKEY", "prefix form: architecture", choices=options.ARCHS),
         Option("isolate_inputs", "bool", False, "prefix form: first level one NAND2 + one NOR2 per operand bit"),
         Option("boundary", "int", None, "prefix form: 1 = NAND2/NOR2 g/t boundary on both operands, ABC "
                "maps the rest", range=(0, 1)),
         Option("increments", "bool", True, "cells form: x + 1 as an inverter + half-adder ripple")),
        forms={"prefix": ("arch", "isolate_inputs", "boundary"), "cells": ("increments",)},
        help="build the selected adders as a prefix network or a ripple of adder cells"),
}

# ASCII digits only (a non-ASCII digit would pass \d and be refused late by the pass)
_REG_TARGET = re.compile(r"^(?:(?P<module>[^,@/\s]+)/)?(?P<reg>[^,@/\s]+)@(?P<lo>[0-9]+)-(?P<hi>[0-9]+)$")
_WIDTH_TARGET = re.compile(r"^width:(?P<lo>[0-9]+)-(?P<hi>[0-9]*)$")


def parse_target(kind: str, t: str, where: str):
    if kind == "register_bits":
        m = _REG_TARGET.match(t)
        if not m or int(m.group("hi")) < int(m.group("lo")):
            raise OptionError(f"{where}: register target {t!r} is not [module/]reg@lo-hi (lo <= hi)")
        return (m.group("module"), m.group("reg"), int(m.group("lo")), int(m.group("hi")))
    if kind == "adders":
        m = _WIDTH_TARGET.match(t)
        if not m or int(m.group("lo")) < 1 or (m.group("hi") and int(m.group("hi")) < int(m.group("lo"))):
            raise OptionError(f"{where}: adder target {t!r} is not width:lo-hi or width:lo- (1 <= lo <= hi)")
        return (int(m.group("lo")), int(m.group("hi")) if m.group("hi") else None)
    raise OptionError(f"{where}: unknown target kind {kind!r}")


def parse_target_as_given(kind: str, t: str):
    """A LEGACY form's target as written, without the lo <= hi rule (the
    legacy variable passes such a value on to the pass unchanged); None when it does not
    even have the shape."""
    if kind == "register_bits":
        m = _REG_TARGET.match(t)
        return m and (m.group("module"), m.group("reg"), int(m.group("lo")), int(m.group("hi")))
    m = _WIDTH_TARGET.match(t)
    return m and (int(m.group("lo")), int(m.group("hi")) if m.group("hi") else None)


def _legacy_make(command: str, targets, values: dict, where: str, strict: bool) -> "ScopedSetting":
    """make() for a legacy translation. Without --strict a target the legacy
    variable accepts but the scoped form refuses (a slice lo > hi, an empty width
    window) is kept as written: the legacy pass text is emitted unchanged and
    the target is reported "unchecked" with a warning (precheck)."""
    try:
        return make(command, targets, values, "legacy", where)
    except OptionError:
        if strict:
            raise
        c = COMMANDS[command]
        if not all(parse_target_as_given(c.kind, x) for x in targets):
            raise
        vals = dict(values)
        own = ("form", *c.forms.get(vals.get("form"), ())) if c.forms else \
            tuple(prm.name for prm in c.params)
        for prm in c.params:                     # defaults as make() fills them
            if prm.name in own and prm.name not in vals and prm.default is not None:
                vals[prm.name] = prm.default
        return ScopedSetting(c.kind, tuple(targets), command, vals, "legacy")


def width_target(lo: int, hi: Optional[int]) -> str:
    return f"width:{lo}-{'' if hi is None else hi}"


@dataclass(frozen=True)
class ScopedSetting:
    """One scoped setting (the record the session and settings.json keep).
    """
    kind: str
    targets: Tuple[str, ...]
    command: str
    values: Dict[str, Any]
    set_at: str = ""

    def to_record(self) -> dict:
        d = {"command": self.command, "kind": self.kind, "targets": list(self.targets),
             "values": dict(self.values)}
        if self.set_at:
            d["set_at"] = self.set_at
        return d

    def describe(self) -> str:
        vals = " ".join(f"{k}={v}" for k, v in sorted(self.values.items()))
        return f"{self.command}({', '.join(self.targets)}{'; ' + vals if vals else ''})"


def make(command: str, targets: Iterable[str], values: Optional[dict] = None,
         set_at: str = "", where: str = OPTION) -> ScopedSetting:
    """A validated ScopedSetting: known command, targets of its kind, typed
    arguments (defaults filled in), arguments of its form only."""
    if command not in COMMANDS:
        import difflib
        near = difflib.get_close_matches(command, list(COMMANDS), 2, 0.5)
        raise OptionError(f"{where}: unknown scoped command {command!r} (commands: "
                          f"{', '.join(sorted(COMMANDS))})"
                          + (f"; did you mean {', '.join(near)}?" if near else ""))
    c = COMMANDS[command]
    targets = tuple(targets)
    if not targets:
        raise OptionError(f"{where}: {command}: no targets (an empty selection is refused)")
    for t in targets:
        if not isinstance(t, str):
            raise OptionError(f"{where}: {command}: target {t!r} is not a string")
        parse_target(c.kind, t, f"{where}: {command}")
    if len(set(targets)) != len(targets):
        raise OptionError(f"{where}: {command}: a target is given twice")
    given = dict(values or {})
    out = {}
    for p in c.params:
        if p.name in given:
            v = p.parse(given.pop(p.name), f"{where}: {command}")
            if v is not None:
                out[p.name] = v
        elif p.default is not None:
            out[p.name] = p.default
    if given:
        c.param(next(iter(given)))          # raises: unknown argument
    if c.forms:
        form = out.get("form")
        foreign = [n for f, ns in c.forms.items() if f != form for n in ns
                   if n in (values or {})]
        if foreign:
            raise OptionError(f"{where}: {command}: {', '.join(foreign)} not an argument of "
                              f"form={form} (form {form} takes {', '.join(c.forms[form]) or 'none'})")
        for f, ns in c.forms.items():
            if f != form:
                for n in ns:
                    out.pop(n, None)
    return ScopedSetting(c.kind, targets, command, out, set_at)


def from_record(d, where: str = OPTION) -> ScopedSetting:
    if isinstance(d, ScopedSetting):
        d = d.to_record()
    if not isinstance(d, dict):
        raise OptionError(f"{where}: an entry must be a mapping "
                          "{command, targets, values[, kind, set_at]}, got " + repr(d))
    extra = set(d) - {"command", "kind", "targets", "values", "set_at"}
    if extra:
        raise OptionError(f"{where}: unknown key(s) {', '.join(sorted(extra))} in a scoped entry")
    if str(d.get("set_at", "")) == "legacy":
        raise OptionError(f"{where}: set_at 'legacy' is reserved for the translation of the legacy "
                          "forms (scoping.legacy_entries); a scoped entry cannot carry it")
    if "command" not in d or "targets" not in d:
        raise OptionError(f"{where}: a scoped entry needs command and targets")
    t = d["targets"]
    if isinstance(t, str):
        t = [t]
    if not isinstance(t, (list, tuple)):
        raise OptionError(f"{where}: targets must be a list of strings")
    s = make(str(d["command"]), t, d.get("values") or {}, str(d.get("set_at", "")), where)
    if d.get("kind") not in (None, s.kind):
        raise OptionError(f"{where}: {s.command} acts on {s.kind}, not {d['kind']!r}")
    return s


def from_value(value, where: str = OPTION) -> Tuple[ScopedSetting, ...]:
    """The `syn.scoped` option value as validated settings."""
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise OptionError(f"{where}: must be a list of scoped entries (JSON / YAML), "
                              f"got {value!r}") from None
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise OptionError(f"{where}: must be a list of scoped entries, got {value!r}")
    return tuple(from_record(d, f"{where}[{i}]") for i, d in enumerate(value))


# ---------------------------------------------------------------- legacy forms

LEGACY_OPTIONS = {
    "syn.clock_gating.data_gate": "gate_register",
    "syn.adders.prefix2.min_width": "map_adders", "syn.adders.prefix2.max_width": "map_adders",
    "syn.adders.prefix2.arch": "map_adders", "syn.adders.prefix2.isolate_inputs": "map_adders",
    "syn.adders.prefix2.boundary": "map_adders",
    "syn.adders.cells2.min_width": "map_adders", "syn.adders.cells2.max_width": "map_adders",
}

_WARNED: set = set()


def _legacy_set(s) -> List[str]:
    return [n for n in LEGACY_OPTIONS if n in s.values and s.source(n) != "default"
            and s.values[n] is not None and s.values[n] != ""]


def legacy_entries(s) -> List[Tuple[str, ScopedSetting]]:
    """(origin, ScopedSetting) of the legacy forms set in settings `s`."""
    out = []
    strict = bool(getattr(s, "strict", False))
    dg = s.values.get("syn.clock_gating.data_gate")
    if dg is not None and str(dg).strip():
        by_mode: Dict[str, List[str]] = {}
        for part in str(dg).split(","):
            f = part.strip().split("@")
            mode = f[2] if len(f) == 3 else "zero"
            by_mode.setdefault(mode, []).append("@".join(f[:2]))
        for mode, ts in by_mode.items():
            out.append(("syn.clock_gating.data_gate (" + s.source("syn.clock_gating.data_gate") + ")",
                        _legacy_make("gate_register", ts, {"detect": mode},
                                     "syn.clock_gating.data_gate", strict)))
    for pre in ("syn.adders.cells2", "syn.adders.prefix2"):
        lo, hi = s.values.get(pre + ".min_width"), s.values.get(pre + ".max_width")
        if lo is not None and hi and hi < lo and strict:
            raise OptionError(f"{pre}.max_width={hi} is below {pre}.min_width={lo}: "
                              "the width window is empty")
    if s.values.get("syn.adders.cells2.min_width") is not None:
        hi = s.values.get("syn.adders.cells2.max_width") or None
        out.append(("syn.adders.cells2.* (" + s.source("syn.adders.cells2.min_width") + ")",
                    _legacy_make("map_adders", [width_target(s.values["syn.adders.cells2.min_width"], hi)],
                                 {"form": "cells"}, "syn.adders.cells2", strict)))
    if s.values.get("syn.adders.prefix2.min_width") is not None:
        hi = s.values.get("syn.adders.prefix2.max_width") or None
        v = {"form": "prefix", "arch": s.values.get("syn.adders.prefix2.arch"),
             "isolate_inputs": bool(s.values.get("syn.adders.prefix2.isolate_inputs"))}
        if s.values.get("syn.adders.prefix2.boundary") is not None:
            v["boundary"] = s.values["syn.adders.prefix2.boundary"]
        out.append(("syn.adders.prefix2.* (" + s.source("syn.adders.prefix2.min_width") + ")",
                    _legacy_make("map_adders", [width_target(s.values["syn.adders.prefix2.min_width"], hi)],
                                 v, "syn.adders.prefix2", strict)))
    return out


_REPLACEMENT = {
    "gate_register": "syn.scoped gate_register (s.gate_register(s.flops(...).bits(lo, hi)))",
    "map_adders": "syn.scoped map_adders (s.map_adders(s.adders(width=(lo, hi)), ...))"}


def advice(names) -> Optional[str]:
    """The form to recommend instead of options `names` when every one of them
    is a legacy form of a scoped command (options.py's legacy-variable
    messages recommend it rather than the dotted legacy name, which is
    itself deprecated); None otherwise."""
    cmds = {LEGACY_OPTIONS.get(n) for n in names}
    if not names or None in cmds or len(cmds) != 1:
        return None
    return _REPLACEMENT[cmds.pop()]


def _family(name: str) -> str:
    return name if name == "syn.clock_gating.data_gate" else name.rsplit(".", 1)[0] + ".*"


def _set_by(s, names) -> str:
    """'CIRCT_FLOW_PREFIX2_ADDERS='56' (env); syn.adders.prefix2.arch (argument)'."""
    out = []
    reg = options.registry()
    used = getattr(s, "legacy_used", {}) or {}
    for n in names:
        src = s.source(n)
        vs = [v for v in reg[n].legacy_env if v in used] if src == "env" else []
        out.append(", ".join(f"{v}={used[v]!r}" for v in vs) + " (env)" if vs else f"{n} ({src})")
    return "; ".join(out)


def check(s) -> None:
    """Called by options.resolve: `syn.scoped` must be a valid list of scoped
    entries; a legacy form is deprecated (one warning per form, naming what
    set it and the scoped form to use; refused under --strict and from
    LEGACY_ENV_REMOVED_IN on). A second-window option without its
    min_width (prefix2.arch, cells2.max_width, ...) builds nothing: warned as
    ignored (as before), refused under --strict."""
    if OPTION in s.values:
        from_value(s.values[OPTION])
    used = _legacy_set(s)
    if not used:
        return
    if all(n in s.values for n in LEGACY_OPTIONS):
        legacy_entries(s)               # the translation is valid
    removed = options.CURRENT_RELEASE >= options.LEGACY_ENV_REMOVED_IN
    fams: Dict[str, List[str]] = {}
    for n in used:
        fams.setdefault(_family(n), []).append(n)
    for fam, names in fams.items():
        rep = _REPLACEMENT[LEGACY_OPTIONS[names[0]]]
        lo = fam[:-1] + "min_width" if fam.endswith(".*") else None
        inert = lo is not None and s.values.get(lo) is None
        if inert:
            msg = (f"{', '.join(names)} (set by {_set_by(s, names)}) has no effect without "
                   f"{lo}: no second window is built, the value is ignored (as before); "
                   f"a second window is {rep}")
        else:
            msg = (f"{fam} is a legacy form (set by {_set_by(s, names)}): use {rep}; it is "
                   f"translated to that scoped form now and no longer accepted from release "
                   f"{options.LEGACY_ENV_REMOVED_IN} on")
        if s.strict:
            raise OptionError("--strict: " + msg)
        if removed:
            raise OptionError(msg)
        key = fam + (" (ignored)" if inert else "")
        if key not in _WARNED:
            _WARNED.add(key)
            print(f"[circt_flow] WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- plan

@dataclass
class Target:
    """One target of one entry at application time."""
    origin: str                      # "syn.scoped[2]" / "syn.clock_gating.data_gate (env)"
    setting: ScopedSetting
    target: str
    form: str                        # data_gate | cells | prefix
    members: List[str] = field(default_factory=list)   # ids matched in the core IR
    status: str = "pending"          # applied | refused | unchecked
    detail: str = ""
    legacy: bool = False             # a translation of a legacy form (its text goes verbatim)

    def arg(self, lib: str) -> str:
        """The pass argument this target is applied by (data gate: the slice)."""
        v = self.setting.values
        if self.form == "data_gate":
            return f"{self.target}@{v.get('detect', 'zero')}"
        lo, hi = (parse_target_as_given("adders", self.target) if self.legacy
                  else parse_target("adders", self.target, ""))
        if self.form == "cells":
            return (f"--map-adder-chains=liberty={lib} enable-adder-chains=true "
                    f"min-width={lo} max-width={hi or 0}"
                    + ("" if v.get("increments", True) else " increments=false"))
        return (f"--map-prefix-adders=enable-prefix-adders=true liberty={lib} "
                f"min-width={lo} max-width={hi or 0} arch={v.get('arch', 'SKLANSKEY')}"
                + (" isolate-inputs=true" if v.get("isolate_inputs") else "")
                + (f" boundary={v['boundary']}" if v.get("boundary") is not None else ""))

    def as_record(self) -> dict:
        return {"origin": self.origin, "command": self.setting.command, "target": self.target,
                "values": dict(self.setting.values), "members": list(self.members),
                "status": self.status, "detail": self.detail}


@dataclass
class Plan:
    targets: List[Target]
    legacy_data_gate: str = ""       # the legacy grammar text, passed on verbatim
    lib: str = ""
    key: str = ""

    def record(self) -> None:
        APPLIED[self.key] = [t.as_record() for t in self.targets]

    def data_gate_args(self) -> List[str]:
        parts = [self.legacy_data_gate] if self.legacy_data_gate else []
        parts += [t.arg(self.lib) for t in self.targets if t.form == "data_gate" and not t.legacy]
        return ["--data-gate-enables=slices=" + ",".join(parts)] if parts else []

    def adder_args(self, form: str) -> List[str]:
        return [t.arg(self.lib) for t in self.targets if t.form == form]


# the per-target record of the last structural stage of each output directory
# (settings.json "scoped_applied")
APPLIED: Dict[str, List[dict]] = {}


def _key(cfg) -> str:
    return str(Path(cfg.out_dir).resolve())


def entries(s) -> List[Tuple[str, ScopedSetting]]:
    """Every scoped setting in effect, in emission order: legacy translations
    first, then syn.scoped in its order."""
    out = legacy_entries(s)
    for i, x in enumerate(from_value(s.values.get(OPTION))):
        out.append((f"{OPTION}[{i}]", x))
    return out


def plan(cfg, check_core: bool = True) -> Plan:
    """The plan of the structural stage for `cfg`: its targets, checked
    against the core IR when it exists (refusals raise OptionError)."""
    S = cfg.settings
    APPLIED.pop(_key(cfg), None)
    ents = entries(S) if hasattr(S, "values") else []
    targets = []
    for origin, x in ents:
        form = "data_gate" if x.command == "gate_register" else x.values["form"]
        for t in x.targets:
            targets.append(Target(origin, x, t, form, legacy=not origin.startswith(OPTION)))
    p = Plan(targets, legacy_data_gate=(str(S.values.get("syn.clock_gating.data_gate") or "").strip()
                                        if hasattr(S, "values") else ""),
             lib=str(cfg.design.lib), key=_key(cfg))
    if not targets:
        return p
    for origin, x in ents:
        if x.set_at == "legacy":
            print(f"[circt_flow] legacy {origin} -> scoped {x.describe()}")
    overlaps(p, S)
    core = Path(cfg.core_mlir)
    if check_core and core.exists():
        try:
            precheck(p, core, cfg.design.top_module, bool(getattr(S, "strict", False)), S)
        finally:
            p.record()
    else:
        for t in targets:
            t.status, t.detail = "unchecked", "no core IR to check against"
    p.record()
    return p


def _lenient(refused, strict: bool):
    """Without --strict a target of a
    LEGACY form that does not apply behaves as the legacy variable does (the pass text is
    unchanged; it is passed on with a warning, status "unchecked"); scoped
    forms and every form under --strict are refused. Returns the refusals
    that stand."""
    keep = []
    for t, why in refused:
        if t.legacy and not strict:
            t.status = "unchecked"
            t.detail = f"{why} (legacy form, not strict: passed on unchanged)"
            key = (t.origin, t.target, why)
            if key not in _WARNED:
                _WARNED.add(key)
                print(f"[circt_flow] WARNING: legacy {t.origin} {t.target}: {why}; passed on "
                      f"unchanged (refused under --strict and from release "
                      f"{options.LEGACY_ENV_REMOVED_IN})", file=sys.stderr)
        else:
            keep.append((t, why))
    return keep


def _global_windows(S) -> List[Tuple[str, str, int, Optional[int]]]:
    """The global (first) instances of the adder passes, in emission order:
    (form, label, lo, hi)."""
    out = []
    if S is None or not hasattr(S, "values"):
        return out
    for form, pre in (("cells", "syn.adders.cells"), ("prefix", "syn.adders.prefix")):
        lo = S.values.get(pre + ".min_width")
        if lo is not None:
            hi = S.values.get(pre + ".max_width") or None
            out.append((form, f"{pre}.* (the global instance, {width_target(lo, hi)})", lo, hi))
    return out


def overlaps(p: Plan, S) -> None:
    """One adder, one pass instance. An instance lowers the adders it builds,
    so a width two instances select is built by the earlier one only, and the
    later one would be recorded "applied" for adders it never saw. In emission
    order (cells global instance, cells targets, prefix global instance,
    prefix targets) a target whose width window meets an earlier window is
    refused, before anything runs (a legacy form without --strict: warned and
    passed on, as the legacy variable is)."""
    strict = bool(getattr(S, "strict", False))
    seen: List[Tuple[str, int, Optional[int]]] = []
    refused = []

    def meets(lo, hi, lo2, hi2):
        return (hi is None or lo2 <= hi) and (hi2 is None or lo <= hi2)
    for form in ("cells", "prefix"):
        for f2, lab, lo, hi in _global_windows(S):
            if f2 == form:
                seen.append((lab, lo, hi))
        for t in p.targets:
            if t.form != form:
                continue
            g = (parse_target_as_given("adders", t.target) if t.legacy
                 else parse_target("adders", t.target, t.origin))
            if not g or (g[1] is not None and g[1] < g[0]):
                continue                         # an empty legacy window (precheck says so)
            hit = [lab for lab, lo, hi in seen if meets(g[0], g[1], lo, hi)]
            if hit:
                refused.append((t, f"its width window meets {'; '.join(hit)}, which already "
                                   "selects those adders (one adder, one instance: the earlier "
                                   "instance builds them)"))
                continue
            seen.append((f"{t.origin} {t.target}", g[0], g[1]))
    refused = _lenient(refused, strict)
    if refused:
        for t, why in refused:
            t.status, t.detail = "refused", why
        p.record()
        raise OptionError("scoped setting(s) refused before the structural stage: "
                          + "; ".join(f"{t.origin} {t.setting.command} {t.target}: {why}"
                                      for t, why in refused))


def precheck(p: Plan, core: Path, top: Optional[str], strict: bool = True, S=None) -> None:
    from .design import objects
    snap = objects.parse_core(core, top)
    refused = []
    by_reg: Dict[Tuple[str, str], List[Tuple[int, int, str]]] = {}
    for t in p.targets:
        if t.legacy:
            g = parse_target_as_given("register_bits" if t.form == "data_gate" else "adders",
                                      t.target)
            lo_, hi_ = (g[2], g[3]) if t.form == "data_gate" else g
            if hi_ is not None and hi_ < lo_:
                refused.append((t, "an empty " + ("slice (lo > hi)" if t.form == "data_gate"
                                                  else "width window (max below min)")))
                continue
        if t.form == "data_gate":
            mod, reg, lo, hi = parse_target("register_bits", t.target, t.origin)
            regs = [r for r in snap.registers if r.kind == "firreg" and r.name == reg
                    and (mod is None or r.module == mod)]
            if not regs:
                refused.append((t, "matched no register (seq.firreg) in the core IR"))
                continue
            t.members = [r.id + f"[{hi}:{lo}]" for r in regs]
            for r in regs:
                if hi >= r.width:
                    refused.append((t, f"{r.id} is {r.width} bits wide"))
                elif not r.hold_mux:
                    refused.append((t, f"{r.id}: next is not a plain hold mux (the pass would skip it)"))
                else:
                    by_reg.setdefault(r.key, []).append((lo, hi, t.target))
        else:
            lo, hi = parse_target("adders", t.target, t.origin)
            ms = [o for o in snap.operators if o.kind == "adder" and o.width >= lo
                  and (hi is None or o.width <= hi)]
            t.members = [o.id for o in ms]
            if not ms:
                refused.append((t, "matched no adder in the core IR"))
    for key, sl in by_reg.items():
        sl.sort()
        for (a_lo, a_hi, a_t), (b_lo, b_hi, b_t) in zip(sl, sl[1:]):
            if b_lo <= a_hi:
                for t in p.targets:
                    if t.target in (a_t, b_t):
                        refused.append((t, f"slices {a_t} and {b_t} of {key[0]}/{key[1]} overlap"))
    for t in p.targets:
        if t.status == "pending":
            t.status = "checked"
    refused = _lenient(refused, strict)
    if refused:
        for t, why in refused:
            t.status, t.detail = "refused", why
        raise OptionError("scoped setting(s) refused before the structural stage: "
                          + "; ".join(f"{t.origin} {t.setting.command} {t.target}: {why}"
                                      for t, why in refused))


# ---------------------------------------------------------------- pass report

@contextmanager
def capture_stderr(active: bool = True):
    """Collect what child processes write to fd 2 (and re-emit it) while the
    block runs. Yields a list that holds the captured text afterwards."""
    box: List[str] = []
    if not active:
        yield box
        return
    sys.stderr.flush()
    saved = os.dup(2)
    with tempfile.TemporaryFile(mode="w+b") as tmp:
        os.dup2(tmp.fileno(), 2)
        try:
            yield box
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
            tmp.seek(0)
            text = tmp.read().decode(errors="replace")
            box.append(text)
            if text:
                sys.stderr.write(text)
                sys.stderr.flush()


_DG_SKIP = re.compile(r"^\[data-gate-enables\] slice (\S+) (matched no register|found but skipped)")
_DG_DONE = re.compile(r"^\[data-gate-enables\] (\d+) register\(s\) rewritten")
_CELLS = re.compile(r"^\[map-adder-chains\] lowered (\d+) comb\.add")
_PREFIX = re.compile(r"^\[map-prefix-adders\] (\d+) add\(s\) built")


_INSTANCE = {"cells": ("--map-adder-chains=", "enable-adder-chains=true"),
             "prefix": ("--map-prefix-adders=", "enable-prefix-adders=true")}


def _expected_executed(p: Plan, S) -> List[str]:
    """The pass arguments the structural stage builds for `p` without
    pipeline edits (unit tests that call postcheck directly)."""
    out = list(p.data_gate_args())
    vals = getattr(S, "values", {})
    if vals.get("syn.adders.cells.min_width") is not None:
        out.append("--map-adder-chains=enable-adder-chains=true (global)")
    out += p.adder_args("cells")
    if vals.get("syn.adders.prefix.min_width") is not None:
        out.append("--map-prefix-adders=enable-prefix-adders=true (global)")
    out += p.adder_args("prefix")
    return out


def postcheck(p: Plan, report: str, S, executed: Optional[List[str]] = None) -> None:
    """Refuse every target the run did not apply. `executed`: the arguments
    of the commands that ran (stages.EXECUTED during the stage), so a pass
    a pipeline edit removed, replaced or moved is seen; `report`: their
    stderr. Each enabled map-adder-chains / map-prefix-adders run prints one
    report line, so the k-th line is the k-th instance in `executed`: a
    target is applied only when its own instance built something."""
    if not p.targets:
        return
    if executed is None:
        executed = _expected_executed(p, S)
    lines = report.splitlines()
    refused = []
    dg = [t for t in p.targets if t.form == "data_gate"]
    if dg:
        arg = p.data_gate_args()[0]
        idx = [i for i, a in enumerate(executed) if a == arg]
        cgi = [i for i, a in enumerate(executed) if a.startswith("--clock-gate-inference")]
        if not idx:
            for t in dg:
                refused.append((t, "data-gate-enables did not run with its slices (the pass "
                                   "pipeline was edited?)"))
        elif cgi and min(cgi) < idx[0]:
            for t in dg:
                refused.append((t, "data-gate-enables ran after clock-gate-inference: the slices "
                                   "would not be gated"))
        elif not any(_DG_DONE.match(x) for x in lines):
            for t in dg:
                refused.append((t, "no data-gate-enables report"))
        else:
            skipped = {m.group(1): m.group(2) for x in lines if (m := _DG_SKIP.match(x))}
            for t in dg:
                _, reg, lo, hi = (parse_target_as_given("register_bits", t.target) if t.legacy
                                  else parse_target("register_bits", t.target, ""))
                why = skipped.get(f"{reg}@{lo}-{hi}")
                if why:
                    refused.append((t, f"the pass reports: {why}"))
                else:
                    t.status, t.detail = "applied", "data-gate-enables rewrote it"
    for form, rx in (("cells", _CELLS), ("prefix", _PREFIX)):
        ts = [t for t in p.targets if t.form == form]
        if not ts:
            continue
        flag, on = _INSTANCE[form]
        inst = [a for a in executed if a.startswith(flag) and on in a]
        counts = [int(m.group(1)) for x in lines if (m := rx.match(x))]
        if len(counts) != len(inst):
            for t in ts:
                refused.append((t, f"the pass report has {len(counts)} line(s) for {len(inst)} "
                                   f"{flag[2:-1]} instance(s): not attributable (a binary that "
                                   "reports every run is needed)"))
            continue
        used = set()
        for t in ts:
            arg = t.arg(p.lib)
            k = next((i for i, a in enumerate(inst) if a == arg and i not in used), None)
            if k is None:
                refused.append((t, "its pass instance did not run (the pass pipeline was edited?)"))
                continue
            used.add(k)
            if counts[k] == 0:
                refused.append((t, "its pass instance built nothing (an instance before it may "
                                   "have taken its adders)"))
            else:
                t.status, t.detail = "applied", f"pass report: {counts[k]} add(s)"
    refused = _lenient(refused, bool(getattr(S, "strict", False)))
    p.record()
    if refused:
        for t, why in refused:
            t.status, t.detail = "refused", why
        p.record()
        raise OptionError("scoped setting(s) refused at the pass: "
                          + "; ".join(f"{t.origin} {t.setting.command} {t.target}: {why}"
                                      for t, why in refused))


@contextmanager
def applying(p: Plan, cfg):
    """Around the structural stage's tool run: capture its report and check
    it when the stage output was written (a stubbed run is not checked)."""
    if not p.targets:
        yield
        return
    from . import stages
    out = Path(cfg.struct_mlir)
    before = out.stat().st_mtime_ns if out.exists() else None
    n0 = len(stages.EXECUTED)
    with capture_stderr() as box:
        yield
    if out.exists() and out.stat().st_mtime_ns != before:
        executed = [str(a) for c in stages.EXECUTED[n0:] for a in c]
        postcheck(p, box[0] if box else "", cfg.settings, executed)
    else:
        for t in p.targets:
            if t.status not in ("refused", "unchecked"):
                t.status, t.detail = "unchecked", "the stage output was not written"
        p.record()


def record_extra(cfg) -> dict:
    """settings.json addition: the per-target result of the last structural
    stage of this run (nothing when no scoped setting was in effect)."""
    a = APPLIED.get(_key(cfg))
    return {"scoped_applied": list(a)} if a else {}
