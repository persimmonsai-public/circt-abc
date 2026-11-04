"""Modes, their clocks, and the calibrated mapping delay target.

A flow config may name one SDC file per mode (doc/techpacks.md, "Modes and
the mapping target"):

    modes:
      func:  func.sdc            # or {sdc: func.sdc}
      turbo: turbo.sdc
    mapping_target:              # optional; these are the defaults
      flop_overhead: auto        # auto | <ps> | false
      flop_cells: []             # restrict `auto` to these cells
      scale: 1.0

`design.clock_period_ps` stays the single-mode shorthand: one mode,
SHORTHAND_MODE, with one clock of that period on the design's clock port.

The SDC subset read here (anything else is warned about and ignored):

    create_clock -period <p> [-name <n>] [get_ports <port>|{<ports>}]
    set_input_delay  <d> -clock <n> [-max|-min] <ports>
    set_output_delay <d> -clock <n> [-max|-min] <ports>
    set_units -time <ns|ps>     # else times are in the profile's time_unit
    set_false_path -from <ports>  # also carried onto power-domain ports

Other timing exceptions (set_multicycle_path, set_clock_groups, ...) are
noted, not read: OpenSTA applies them where it reads the SDC as written.

`#` comments, `;` separators and `\\` line continuations are honoured; no
Tcl variables or expressions. create_clock without a port is a virtual
clock: it is kept, but it clocks no domain.

The mapping target is what ABC gets as clock-period-ps (the `{period}` of
an abc_script_file, &nf's -D). For each domain (the whole design without
`power:`) it is

    target = floor(scale * (min over modes of the domain's clock period
                            - flop overhead))

A domain's clocks are the clocks of the top-level ports that reach its
instances' ports (traced through the elaborated hierarchy), or the
domain's `clocks:`. The flop overhead (`auto`) is the worst clock-to-Q +
setup over the flops (cells with an `ff` group) of the domain's rail's
liberties at the profile's setup corner (its mapping liberties without
corners): clock-to-Q at the clock pin's smallest table transition and an
output load of 4x the flop's largest input capacitance (FO4,
interpolated), setup at the smallest transitions of its constraint table;
each the larger of rise and fall. `flop_cells` limits the flops considered
(for instance to the ones the register mapper picks). The derivation is
logged and written to the run's mapping_target.json (power_domains.json
for a power flow).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SHORTHAND_MODE = "default"
SHORTHAND_CLOCK = "clk"
TARGET_KEYS = {"flop_overhead", "flop_cells", "scale"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNIT_PS = {"ps": 1.0, "ns": 1000.0, "us": 1e6}


class ModeError(ValueError):
    """A mode, SDC or mapping-target setting that cannot be honoured."""


def unit_ps(unit: str) -> float:
    """Picoseconds per `unit` ("ns", "1ns", "ps", "10ps", ...)."""
    m = re.fullmatch(r"\s*([\d.]*)\s*([a-z]+)\s*", str(unit).lower())
    if not m or m.group(2) not in _UNIT_PS:
        raise ModeError(f"unknown time unit {unit!r}")
    return float(m.group(1) or 1) * _UNIT_PS[m.group(2)]


# ---------------------------------------------------------------------------
# SDC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    name: str
    period_ps: float
    ports: Tuple[str, ...] = ()  # () for a virtual clock


@dataclass(frozen=True)
class IODelay:
    kind: str  # "input" or "output"
    delay_ps: float
    clock: str
    targets: str  # the object list as written
    minmax: str = ""  # "", "max" or "min"


@dataclass(frozen=True)
class Mode:
    name: str
    sdc: Optional[Path]
    clocks: Dict[str, Clock]
    io_delays: Tuple[IODelay, ...] = ()
    warnings: Tuple[str, ...] = ()
    # Top-level ports of `set_false_path -from [get_ports ...]` (no -to or
    # -through): the power flow's domain views carry them onto the domain
    # ports those inputs reach (sta.domain_sdc).
    false_from: Tuple[str, ...] = ()


def _commands(text: str) -> List[Tuple[int, str]]:
    """(line, command text) of each command: continuations joined,
    comments dropped, split on newlines and `;` outside brackets."""
    # Continuation lines joined; a command keeps its first line's number.
    lines: List[Tuple[int, str]] = []
    pending, start = "", 0
    for ln, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = ln
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        lines.append((start, pending + raw))
        pending = ""
    if pending:
        lines.append((start, pending))
    out = []
    for ln, line in lines:
        cur, depth, i = "", 0, 0
        while i < len(line):
            c = line[i]
            if c == "#" and depth == 0 and not cur.strip():
                break
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            if c == ";" and depth == 0:
                if cur.strip():
                    out.append((ln, cur.strip()))
                cur = ""
            else:
                cur += c
            i += 1
        if cur.strip():
            out.append((ln, cur.strip()))
    return out


def _words(cmd: str) -> List[str]:
    """Tcl-ish words: [...] and {...} (nested) and "..." are one word."""
    words, i = [], 0
    while i < len(cmd):
        if cmd[i].isspace():
            i += 1
            continue
        if cmd[i] in "[{":
            open_, close = cmd[i], "]" if cmd[i] == "[" else "}"
            depth, j = 0, i
            while j < len(cmd):
                if cmd[j] == open_:
                    depth += 1
                elif cmd[j] == close:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            words.append(cmd[i:j + 1])
            i = j + 1
        elif cmd[i] == '"':
            j = cmd.find('"', i + 1)
            j = len(cmd) if j < 0 else j
            words.append(cmd[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < len(cmd) and not cmd[j].isspace():
                j += 1
            words.append(cmd[i:j])
            i = j
    return words


def _get_ports(word: str) -> Optional[Tuple[str, ...]]:
    """The port names of `[get_ports X]` / `[get_ports {X Y}]`, or None."""
    m = re.fullmatch(r"\[\s*get_ports\s+(.*)\]", word.strip(), re.S)
    if not m:
        return None
    arg = m.group(1).strip()
    if arg.startswith("{") and arg.endswith("}"):
        arg = arg[1:-1]
    arg = arg.replace('"', " ")
    return tuple(arg.split())


def _number(w: str, at: str) -> float:
    try:
        return float(w)
    except ValueError:
        raise ModeError(f"{at}: {w!r} is not a number") from None


# Timing exceptions: an SDC file is passed to OpenSTA as written (single-
# domain STA, vt_swap, the chip view), so these act there; only
# `set_false_path -from [get_ports ...]` is also mapped onto power domains.
EXCEPTIONS = {"set_false_path", "set_multicycle_path", "set_max_delay",
              "set_min_delay", "set_clock_groups", "set_disable_timing",
              "set_case_analysis"}


def parse_sdc(text: str, where: str = "<sdc>", time_unit: str = "ns"
              ) -> Tuple[Dict[str, Clock], List[IODelay], List[str]]:
    """(clocks by name, I/O delays, warnings) of an SDC text in the subset
    this module documents; times are converted to ps (`time_unit` unless
    the SDC says set_units -time)."""
    c, d, w, _ = parse_sdc_full(text, where, time_unit)
    return c, d, w


def parse_sdc_full(text: str, where: str = "<sdc>", time_unit: str = "ns"
                   ) -> Tuple[Dict[str, Clock], List[IODelay], List[str],
                              List[str]]:
    """parse_sdc, plus the ports of `set_false_path -from [get_ports ...]`
    (Mode.false_from)."""
    scale = unit_ps(time_unit)
    clocks: Dict[str, Clock] = {}
    delays: List[IODelay] = []
    warnings: List[str] = []
    false_from: List[str] = []
    for ln, cmd in _commands(text):
        at = f"{where}:{ln}"
        w = _words(cmd)
        op, args = w[0], w[1:]
        if op == "set_units":
            rest = []
            i = 0
            while i < len(args):
                if args[i] == "-time" and i + 1 < len(args):
                    scale = unit_ps(args[i + 1])
                    i += 2
                else:
                    rest.append(args[i])
                    i += 1
            if rest:
                warnings.append(f"{at}: set_units: ignored {' '.join(rest)}")
        elif op == "create_clock":
            name, period, ports, ignored = None, None, None, []
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-name", "-period") and i + 1 < len(args):
                    if a == "-name":
                        name = args[i + 1]
                    else:
                        period = _number(args[i + 1], f"{at}: create_clock")
                    i += 2
                    continue
                if a in ("-waveform",) and i + 1 < len(args):
                    ignored += args[i:i + 2]
                    i += 2
                    continue
                if a.startswith("-"):
                    ignored.append(a)
                elif ports is None and _get_ports(a) is not None:
                    ports = _get_ports(a)
                else:
                    raise ModeError(f"{at}: create_clock: the source must be "
                                    f"[get_ports ...], got {a!r}")
                i += 1
            if ignored:
                warnings.append(f"{at}: create_clock: ignored "
                                + " ".join(ignored))
            if period is None or period <= 0:
                raise ModeError(f"{at}: create_clock needs -period > 0")
            if name is None:
                if not ports:
                    raise ModeError(f"{at}: a virtual clock needs -name")
                name = ports[0]
            if name in clocks:
                raise ModeError(f"{at}: clock {name} is defined twice")
            clocks[name] = Clock(name, period * scale, tuple(ports or ()))
        elif op in ("set_input_delay", "set_output_delay"):
            value, clock, minmax, targets, ignored = None, None, "", None, []
            i = 0
            while i < len(args):
                a = args[i]
                if a == "-clock" and i + 1 < len(args):
                    clock = args[i + 1]
                    i += 2
                    continue
                if a in ("-max", "-min"):
                    minmax = a[1:]
                elif a.startswith("-") and not re.fullmatch(
                        r"-[\d.]+(e[-+]?\d+)?", a):
                    ignored.append(a)
                elif value is None and not a.startswith("["):
                    value = _number(a, f"{at}: {op}")
                elif targets is None:
                    targets = a
                else:
                    raise ModeError(f"{at}: {op}: unexpected {a!r}")
                i += 1
            if ignored:
                warnings.append(f"{at}: {op}: ignored {' '.join(ignored)}")
            if value is None or clock is None or targets is None:
                raise ModeError(f"{at}: {op} needs a delay, -clock and the "
                                "ports")
            if clock not in clocks:
                raise ModeError(f"{at}: {op}: clock {clock} is not defined "
                                "(create_clock it first)")
            delays.append(IODelay(op.split("_")[1], value * scale, clock,
                                  targets, minmax))
        elif op == "set_false_path" and len(args) == 2 and \
                args[0] == "-from" and _get_ports(args[1]) is not None:
            false_from += [p for p in _get_ports(args[1])
                           if p not in false_from]
        elif op in EXCEPTIONS:
            warnings.append(f"{at}: {op}: applies where OpenSTA reads this "
                            "SDC as written (single-domain STA, vt_swap, the "
                            "chip view), not in power-domain views")
        else:
            warnings.append(f"{at}: {op}: not in the supported SDC subset; "
                            "ignored")
    return clocks, delays, warnings, false_from


def load_mode(name: str, sdc: Path, time_unit: str = "ns") -> Mode:
    clocks, delays, warnings, ff = parse_sdc_full(Path(sdc).read_text(),
                                                  str(sdc), time_unit)
    if not any(c.ports for c in clocks.values()):
        raise ModeError(f"mode {name}: {sdc} defines no clock on a port")
    return Mode(name, Path(sdc), clocks, tuple(delays), tuple(warnings),
                tuple(ff))


def shorthand_mode(period_ps: float, port: Optional[str]) -> Mode:
    """design.clock_period_ps as a mode: one clock on `port` (on every
    port, i.e. every domain, when the port is unknown)."""
    return Mode(SHORTHAND_MODE, None, {SHORTHAND_CLOCK: Clock(
        SHORTHAND_CLOCK, float(period_ps), (port,) if port else ())})


def parse_modes(d, base: Path, where, time_unit: str = "ns"
                ) -> Tuple[Mode, ...]:
    """A flow config's `modes:` (name -> sdc path or {sdc: path}), the SDC
    paths relative to `base`."""
    if not isinstance(d, dict) or not d:
        raise ModeError(f"{where}: modes: must map each mode name to its SDC "
                        "file")
    out = []
    for name, v in d.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise ModeError(f"{where}: modes: {name!r} is not an identifier")
        if isinstance(v, dict):
            if set(v) != {"sdc"}:
                raise ModeError(f"{where}: modes.{name} takes one key, sdc")
            v = v["sdc"]
        if not isinstance(v, str):
            raise ModeError(f"{where}: modes.{name}: give the SDC path")
        sdc = (base / v).resolve()
        if not sdc.is_file():
            raise ModeError(f"{where}: modes.{name}: {sdc} does not exist")
        out.append(load_mode(name, sdc, time_unit))
    return tuple(out)


@dataclass(frozen=True)
class TargetSpec:
    """The flow config's mapping_target: section. flop_overhead: "auto",
    a number of ps, or None (no subtraction)."""
    flop_overhead: object = "auto"
    flop_cells: Tuple[str, ...] = ()
    scale: float = 1.0


def parse_target(d, where) -> TargetSpec:
    if d is None:
        return TargetSpec()
    if not isinstance(d, dict) or set(d) - TARGET_KEYS:
        bad = sorted(set(d) - TARGET_KEYS) if isinstance(d, dict) else ["?"]
        raise ModeError(f"{where}: mapping_target.{bad[0]} is not a key; "
                        f"allowed: {', '.join(sorted(TARGET_KEYS))}")
    fo = d.get("flop_overhead", "auto")
    if fo is False or fo is None:
        fo = None
    elif fo != "auto":
        if isinstance(fo, bool) or not isinstance(fo, (int, float)) or fo < 0:
            raise ModeError(f"{where}: mapping_target.flop_overhead must be "
                            f"auto, false or ps >= 0, got {fo!r}")
        fo = float(fo)
    cells = d.get("flop_cells") or []
    if isinstance(cells, str) or not all(isinstance(c, str) for c in cells):
        raise ModeError(f"{where}: mapping_target.flop_cells must be a list "
                        "of cell names")
    scale = d.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) \
            or not 0 < scale <= 2:
        raise ModeError(f"{where}: mapping_target.scale must be in (0, 2], "
                        f"got {scale!r}")
    return TargetSpec(fo, tuple(cells), float(scale))


# ---------------------------------------------------------------------------
# Flop overhead from a liberty
# ---------------------------------------------------------------------------

_TOK = re.compile(r'\s*(?:(/\*.*?\*/)|("(?:[^"\\]|\\.)*")|([(){}:;,])'
                  r'|(\\\n)|([^\s(){}:;,"]+))', re.S)


@dataclass
class Group:
    kind: str
    args: List[str]
    attrs: Dict[str, str] = field(default_factory=dict)
    groups: List["Group"] = field(default_factory=list)
    complex: Dict[str, List[str]] = field(default_factory=dict)

    def sub(self, kind: str) -> List["Group"]:
        return [g for g in self.groups if g.kind == kind]


def _tokens(text: str) -> List[str]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ModeError(f"liberty: cannot tokenize near {text[pos:pos+40]!r}")
        pos = m.end()
        if m.group(1) or m.group(4):
            continue
        out.append(m.group(2) or m.group(3) or m.group(5))
    return out


def parse_group(text: str) -> Group:
    """One liberty group (`kind (args) { ... }`), nested, as a Group:
    simple attributes in attrs (quotes stripped), complex attributes
    (`name (args);`) in complex, groups in groups."""
    toks = _tokens(text)
    pos = 0

    def unq(t):
        return t[1:-1] if t.startswith('"') else t

    def group():
        nonlocal pos
        kind = toks[pos]
        pos += 1
        args = []
        assert toks[pos] == "("
        pos += 1
        while toks[pos] != ")":
            if toks[pos] != ",":
                args.append(unq(toks[pos]))
            pos += 1
        pos += 1
        g = Group(kind, args)
        assert toks[pos] == "{", f"expected {{ after {kind}"
        pos += 1
        while toks[pos] != "}":
            name = toks[pos]
            if toks[pos + 1] == ":":
                val = []
                pos += 2
                while toks[pos] != ";" and toks[pos] != "}":
                    val.append(unq(toks[pos]))
                    pos += 1
                if toks[pos] == ";":
                    pos += 1
                g.attrs[name] = " ".join(val)
                continue
            # name ( args ) followed by { (group) or ; (complex attribute)
            j = pos + 2
            while toks[j] != ")":
                j += 1
            if j + 1 < len(toks) and toks[j + 1] == "{":
                g.groups.append(group())
            else:
                g.complex[name] = [unq(t) for t in toks[pos + 2:j] if t != ","]
                pos = j + 1
                if pos < len(toks) and toks[pos] == ";":
                    pos += 1
        pos += 1
        return g

    return group()


def _floats(s: str) -> List[float]:
    return [float(x) for x in re.split(r"[,\s]+", s.strip()) if x]


def _table(tab: Group, templates: Dict[str, Group]):
    """(variables, indices, values rows) of a lookup table group."""
    tmpl = templates.get(tab.args[0]) if tab.args else None
    variables, idx = [], []
    for k in (1, 2, 3):
        var = tmpl.attrs.get(f"variable_{k}") if tmpl else None
        ix = tab.complex.get(f"index_{k}") or (
            tmpl.complex.get(f"index_{k}") if tmpl else None)
        if var is None and ix is None:
            break
        variables.append(var or "")
        idx.append(_floats(" ".join(ix or [])))
    rows = [_floats(r) for r in tab.complex.get("values", [])]
    return variables, idx, rows


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if len(xs) == 1:
        return ys[0]
    for k in range(len(xs) - 1):
        if x <= xs[k + 1] or k == len(xs) - 2:
            x0, x1, y0, y1 = xs[k], xs[k + 1], ys[k], ys[k + 1]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def eval_table(tab: Group, templates: Dict[str, Group],
               load: Optional[float] = None) -> float:
    """A table at the smallest transition(s) and, for a load variable,
    `load` (linear interpolation, extrapolated at the ends); a scalar
    table's value."""
    variables, idx, rows = _table(tab, templates)
    if not rows:
        raise ModeError(f"liberty: table {tab.kind} has no values")
    if len(rows) == 1 and len(rows[0]) == 1:
        return rows[0][0]
    # Each row is one index_1 value; within a row, index_2.
    def is_load(v):
        return "capacitance" in v
    if len(variables) == 1 or len(rows) == 1:
        line = rows[0]
        if variables and is_load(variables[0]) and load is not None:
            return _interp(idx[0], line, load)
        return line[0]
    if is_load(variables[0]) and load is not None:
        col = [r[0] for r in rows]  # smallest transition (index_2)
        return _interp(idx[0], col, load)
    if len(variables) > 1 and is_load(variables[1]) and load is not None:
        return _interp(idx[1], rows[0], load)
    return rows[0][0]


@dataclass(frozen=True)
class FlopTiming:
    cell: str
    library: str
    clk_to_q: float  # library time units
    setup: float
    load: float

    @property
    def overhead(self) -> float:
        return self.clk_to_q + self.setup


_CELL_START = re.compile(r'^[ \t]*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)\s*\{',
                         re.M)


def flop_timings(libs: Iterable[Path], cells: Sequence[str] = ()
                 ) -> Tuple[List[FlopTiming], str]:
    """Clock-to-Q and setup of every flop (cell with an ff group) of
    `libs` (only `cells` when given), and the libraries' time unit."""
    from .profile import _block_end
    out: List[FlopTiming] = []
    unit = None
    for lib in libs:
        text = Path(lib).read_text(errors="ignore")
        m = re.search(r'time_unit\s*:\s*"?([^";]+)"?\s*;', text)
        u = (m.group(1).strip() if m else "1ns")
        if unit is not None and unit_ps(u) != unit_ps(unit):
            raise ModeError(f"{lib}: time_unit {u} differs from {unit}")
        unit = u
        templates = {}
        for tm in re.finditer(r'^[ \t]*lu_table_template\s*\(', text, re.M):
            g = parse_group(text[tm.start():_block_end(
                text, text.index("{", tm.start()))])
            templates[g.args[0]] = g
        for cm in _CELL_START.finditer(text):
            name = cm.group(1)
            if cells and name not in cells:
                continue
            body = text[cm.start():_block_end(text, cm.end() - 1)]
            if not re.search(r"^\s*ff\s*\(", body, re.M):
                continue
            g = parse_group(body)
            pins = [p for p in g.groups if p.kind == "pin"]
            caps = [float(p.attrs["capacitance"]) for p in pins
                    if p.attrs.get("direction") == "input"
                    and "capacitance" in p.attrs]
            load = 4 * max(caps) if caps else 0.0
            c2q, setup = [], []
            for p in pins:
                for t in p.sub("timing"):
                    tt = t.attrs.get("timing_type", "")
                    if tt in ("rising_edge", "falling_edge"):
                        c2q += [eval_table(x, templates, load)
                                for x in t.groups
                                if x.kind in ("cell_rise", "cell_fall")]
                    elif tt in ("setup_rising", "setup_falling"):
                        setup += [eval_table(x, templates)
                                  for x in t.groups
                                  if x.kind in ("rise_constraint",
                                                "fall_constraint")]
            if not c2q:
                continue  # no clock-to-output arc: not a timing flop
            out.append(FlopTiming(name, Path(lib).name, max(c2q),
                                  max(setup) if setup else 0.0, load))
    return out, unit or "1ns"


def flop_overhead(libs: Sequence[Path], cells: Sequence[str] = ()) -> dict:
    """The worst flop's clock-to-Q + setup over `libs` (see the module
    docstring), in ps, with what it was derived from."""
    flops, unit = flop_timings(libs, cells)
    if not flops:
        raise ModeError("mapping_target: no flop (cell with an ff group and "
                        "a clock-to-output arc) in "
                        + ", ".join(Path(p).name for p in libs)
                        + (f" among {', '.join(cells)}" if cells else ""))
    k = unit_ps(unit)
    worst = max(flops, key=lambda f: f.overhead)
    return {"rule": "worst clock-to-Q + setup over the flops"
                    + (" " + ",".join(cells) if cells else "")
                    + ": clock-to-Q at the smallest clock transition and a "
                      "load of 4x the flop's largest input capacitance, "
                      "setup at the smallest transitions",
            "cell": worst.cell, "library": worst.library,
            "clk_to_q_ps": round(worst.clk_to_q * k, 3),
            "setup_ps": round(worst.setup * k, 3),
            "load": worst.load,
            "overhead_ps": round(worst.overhead * k, 3),
            "flops_considered": len(flops),
            "liberties": [str(p) for p in libs]}


# ---------------------------------------------------------------------------
# Domain clocks and the target
# ---------------------------------------------------------------------------


def domain_clocks(mode: Mode, feeding: Optional[Iterable[str]],
                  names: Optional[Sequence[str]] = None) -> List[Clock]:
    """The clocks of `mode` that clock a domain: those named in `names`
    (the domain's `clocks:`), else those whose ports are among the
    top-level ports `feeding` the domain (every port clock when `feeding`
    is None), plus a portless shorthand clock (it clocks everything)."""
    if names:
        return [mode.clocks[n] for n in names if n in mode.clocks]
    feed = None if feeding is None else set(feeding)
    out = []
    for c in mode.clocks.values():
        if not c.ports:
            if mode.sdc is None:  # shorthand with no port: everything
                out.append(c)
            continue
        if feed is None or feed & set(c.ports):
            out.append(c)
    return out


def derive_target(name: str, modes: Sequence[Mode],
                  feeding: Optional[Iterable[str]], spec: TargetSpec,
                  overhead: Optional[dict],
                  clock_names: Optional[Sequence[str]] = None) -> dict:
    """The mapping-target derivation for one domain (`name`): per mode its
    clocks and period, the tightest, the flop overhead (`overhead`, from
    flop_overhead(), used when spec.flop_overhead is "auto"), the scale,
    and target_ps, the integer ABC gets."""
    if clock_names:
        known = {c for m in modes for c in m.clocks}
        bad = [c for c in clock_names if c not in known]
        if bad:
            raise ModeError(f"domain {name}: clocks {', '.join(bad)} are not "
                            "defined in any mode")
    per_mode = {}
    for m in modes:
        cl = domain_clocks(m, feeding, clock_names)
        if cl:
            per_mode[m.name] = {
                "clocks": {c.name: c.period_ps for c in cl},
                "period_ps": min(c.period_ps for c in cl)}
    if not per_mode:
        raise ModeError(f"domain {name}: no clock of any mode ("
                        + ", ".join(m.name for m in modes) + ") reaches it; "
                        "give the domain `clocks:`")
    tight = min(per_mode, key=lambda k: per_mode[k]["period_ps"])
    period = per_mode[tight]["period_ps"]
    if spec.flop_overhead is None:
        ov, ov_rec = 0.0, None
    elif spec.flop_overhead == "auto":
        if overhead is None:
            raise ModeError(f"domain {name}: flop_overhead auto needs the "
                            "rail's flops")
        ov, ov_rec = overhead["overhead_ps"], overhead
    else:
        ov, ov_rec = float(spec.flop_overhead), {"given_ps": spec.flop_overhead}
    target = math.floor(spec.scale * (period - ov))
    if target <= 0:
        raise ModeError(f"domain {name}: mapping target {target} ps <= 0 "
                        f"(period {period} ps, overhead {ov} ps)")
    return {
        "domain": name, "modes": per_mode, "tightest_mode": tight,
        "period_ps": period, "flop_overhead_ps": ov,
        "flop_overhead": ov_rec, "scale": spec.scale, "target_ps": target,
        "formula": f"floor({spec.scale:g} * ({period:g} - {ov:g})) = "
                   f"{target}"}


def log_target(rec: dict, prefix: str = "[mapping-target]") -> str:
    modes = ", ".join(
        f"{m}: " + "/".join(f"{c}={p:g}" for c, p in v["clocks"].items())
        for m, v in rec["modes"].items())
    ov = rec["flop_overhead"]
    ovs = (f"{rec['flop_overhead_ps']:g} ps ({ov['cell']} in {ov['library']}"
           f": clk->Q {ov['clk_to_q_ps']:g} + setup {ov['setup_ps']:g})"
           if ov and "cell" in ov else f"{rec['flop_overhead_ps']:g} ps")
    return (f"{prefix} {rec['domain']}: modes {modes} ps; tightest "
            f"{rec['tightest_mode']} {rec['period_ps']:g} ps; flop overhead "
            f"{ovs}; target {rec['formula']} ps")


# ---------------------------------------------------------------------------
# Which top-level ports reach an instance (the elaborated MLIR)
# ---------------------------------------------------------------------------

_ARG = re.compile(r"\bin\s+%([\w$.]+)\s*:")
_CONN = re.compile(r"([\w$]+)\s*:\s*%([\w$.#]+)\s*:")
_ALIAS = re.compile(r"%([\w$.#]+)\s*=\s*(?:seq\.to_clock|seq\.from_clock|"
                    r"hw\.wire|comb\.buffer)\s+%([\w$.#]+)")


def _module_text(text: str, module: str) -> Tuple[str, str]:
    """(header, body) of hw.module @module in a core MLIR text."""
    m = re.search(r"^  hw\.module(?: private)? @" + re.escape(module)
                  + r"\((.*?)\)\s*(?:attributes\s*\{.*?\}\s*)?\{",
                  text, re.M | re.S)
    if not m:
        raise ModeError(f"module {module} is not in the elaborated design")
    nxt = re.compile(r"^  hw\.module", re.M).search(text, m.end())
    return m.group(1), text[m.end():nxt.start() if nxt else len(text)]


def feeding_ports(text: str, top: str, path: str
                  ) -> Tuple[Dict[str, set], str]:
    """For instance `path` (dot-separated from `top`): ({port of the
    instantiated module: top-level input ports it is driven by, directly or
    through one seq.to_clock/from_clock/wire/buffer}, that module)."""
    hdr, body = _module_text(text, top)
    env = {a: {a} for a in _ARG.findall(hdr)}  # value -> top ports
    mod = top
    for seg in path.split("."):
        for a, b in _ALIAS.findall(body):
            if b in env:
                env.setdefault(a, set()).update(env[b])
        m = re.search(r'hw\.instance "' + re.escape(seg)
                      + r'"(?: sym @\S+)? @([^\s(]+)\((.*?)\)\s*->', body,
                      re.S)
        if not m:
            m = re.search(r'hw\.instance "' + re.escape(seg)
                          + r'"(?: sym @\S+)? @([^\s(]+)\((.*?)\)', body,
                          re.S)
        if not m:
            raise ModeError(f"instance {seg} (of {path}) is not in {mod}")
        mod = m.group(1)
        conns = {p: set(env.get(v, set()))
                 for p, v in _CONN.findall(m.group(2))}
        hdr, body = _module_text(text, mod)
        env = {a: conns.get(a, set()) for a in _ARG.findall(hdr)}
    return {p: s for p, s in env.items()}, mod
