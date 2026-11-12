"""Opt-in clock-tree and timing-corner options of the P&R stage.

The flow config's `pnr:` section (defaults unchanged when a key is absent).
Times are in the libraries' (and the SDC's) time unit. The values below are
illustrations for an ASAP7-style library in ps, not recommendations; on a
library in ns the same times are written a thousand times smaller (the 70
below becomes 0.07).

    pnr:
      input_delay_min: 70           # time unit: set_input_delay -min on
                                    # every non-clock input, appended to the
                                    # SDC the P&R and the STA stages read
      sdc_file: constraints.sdc     # use this SDC instead of the generated one
      cts_skip_fanout_le: 2         # gated clock nets of <= N loads (register
                                    # clock pins or further gates) are left to
                                    # their gate instead of getting a buffer tree
      cts_args: [-no_insertion_delay]
      cts_size_gates:               # post-CTS clock-gate sizing by measured arrival
        drive_re: '^(.*x)([0-9]+)(_.*)$'    # prefix, drive, suffix (pack-specific)
        drives: [1, 2, 4]                   # required: drive strengths to try
        tolerance: 8                        # required, time unit: the smallest
                                            # listed drive whose clock arrival is
                                            # within this of the fastest is kept
        variants: [..]; variant_re: '..'   # optional: other flavours of the gate
      cts_gate_port_tol: 4          # time unit; with cts_size_gates: the tolerance
                                    # for a clock gate that clocks a register in
                                    # the fan-in of an output port (see
                                    # cts_recipe.tcl); passed as CTS_GATE_PORT_TOL;
                                    # 0 or absent = the same tolerance for every
                                    # gate, and 0 in the flow config cancels a
                                    # pack default
      cts_flatten_root: true        # drop a lone root buffer that only feeds further buffers
      repair_dont_use: ['^SOME_FAMILY']   # regexes on liberty cell names that the
                                    # post-CTS repair may not swap a cell to
                                    # (keeps registers in the family synthesis chose)
      spef_restore_pincap: true     # default true: the global-route SPEF the command
                                    # exports gets the pin-node cap OpenROAD's writer
                                    # drops put back (spef_pincap.py); false = as written
      placement_check: warn         # warn|fail, absent = off: read the stage logs for
                                    # failed legalization (placement_health.py), put
                                    # placement_failures / placement_attempts in
                                    # pnr_metrics.json; fail stops the stage
      hold_corner: true             # dual-corner repair: the pack's hold-corner libraries
      hold_margin: 25               # (existing key, time unit) slack repair_timing -hold works to
      setup_margin: 0.0

For a pack's own P&R command (`pnr.command`) the options reach it as
environment variables (`env()`); the Tcl procedures that consume them are
pnr_tcl/cts_recipe.tcl, which the command's CTS script sources. For the ORFS
stage only `input_delay_min`, `sdc_file`, `placement_check`, `cts_args` and
the margins apply; it refuses the keys in PACK_COMMAND_ONLY (and
`spef_restore_pincap`, `post_layout`). `spef_restore_pincap` reaches the command as
SPEF_RESTORE_PINCAP and SPEF_EXPORT_TCL (pnr_tcl/spef_export.tcl, whose
`spef_export_global_route` the command's export step calls); after the
command the flow restores the SPEFs it exported below the output directory,
with the estimator lines found in the command's logs (stdout of the export).
The clock-gate drive field is found with the pack's own regular expression.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

TCL = Path(__file__).resolve().parent / "pnr_tcl" / "cts_recipe.tcl"
SPEF_TCL = Path(__file__).resolve().parent / "pnr_tcl" / "spef_export.tcl"

GATE_KEYS = {"drive_re", "drives", "tolerance", "variants", "variant_re"}


# Keys that reach only a pack's own P&R command (environment variables and
# the Tcl procedures of pnr_tcl/cts_recipe.tcl); the ORFS stage refuses them.
PACK_COMMAND_ONLY = frozenset({
    "cts_skip_fanout_le", "cts_size_gates", "cts_gate_port_tol",
    "cts_flatten_root", "repair_dont_use", "hold_corner"})


class RecipeError(ValueError):
    pass


# The result files a pack's own P&R command leaves in its output directory
# for the steps after it (post-layout, the LEF check): a pack's
# `pnr: {results: {...}}` may name them; these are the defaults.
RESULT_FILES = {"netlist": "placed.v", "spef": "placed.spef",
                "lef_list": "placed.lef.list"}


def result_files(pack_pnr: Optional[dict]) -> Dict[str, str]:
    """The names of a pack command's result files (RESULT_FILES, with the
    pack's `pnr.results` entries in their place)."""
    r = (pack_pnr or {}).get("results")
    if r is None:
        return dict(RESULT_FILES)
    if not isinstance(r, dict) or set(r) - set(RESULT_FILES):
        raise RecipeError("pnr.results takes " + ", ".join(sorted(RESULT_FILES)))
    for k, v in r.items():
        if (not isinstance(v, str) or not v or "/" in v or "\\" in v
                or v in (".", "..") or v != v.strip()):
            raise RecipeError(f"pnr.results.{k} must be a file name (no "
                              "directory part)")
    return {**RESULT_FILES, **r}


def placement_logs(pack_pnr: Optional[dict]) -> Optional[List[str]]:
    """A pack's `pnr.placement_logs`: glob patterns, relative to the P&R
    output directory, of the stage logs its own P&R command leaves (read by
    placement_check in that order); None when the pack does not set it
    (placement_health's directory layouts)."""
    v = (pack_pnr or {}).get("placement_logs")
    if v is None:
        return None
    if (not isinstance(v, list) or not v
            or not all(isinstance(x, str) and x and x == x.strip()
                       and not x.startswith(("/", "~")) and "\\" not in x
                       and ".." not in Path(x).parts for x in v)):
        raise RecipeError("pnr.placement_logs must be a non-empty list of "
                          "glob patterns relative to the P&R output "
                          "directory (no leading /, no ..)")
    return list(v)


def _num(v, name, positive=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RecipeError(f"pnr.{name} must be a number")
    if not math.isfinite(v):
        raise RecipeError(f"pnr.{name} must be a finite number")
    if positive and v <= 0:
        raise RecipeError(f"pnr.{name} must be positive")
    return float(v)


def _margin(v, name):
    """hold_margin / setup_margin: a number or a numeric string (passed to
    ORFS as given, whose argument parser reads it), finite."""
    if isinstance(v, bool):
        raise RecipeError(f"pnr.{name} must be a number")
    try:
        f = float(str(v))
    except ValueError:
        raise RecipeError(f"pnr.{name} must be a number (got {v!r})")
    if not math.isfinite(f):
        raise RecipeError(f"pnr.{name} must be a finite number (got {v!r})")
    return f


def parse(p: dict, pack_pnr: Optional[dict] = None) -> dict:
    """Validated recipe options from the flow's `pnr:` section (a pack's
    `pnr:` supplies defaults for `cts_*`/margin keys, as in stages.orfs_pnr).
    Returns only the keys that are set."""
    pack_pnr = pack_pnr or {}
    def pick(k):
        return p[k] if k in p else pack_pnr.get(k)
    out: dict = {}
    if pick("input_delay_min") is not None:
        out["input_delay_min"] = _num(pick("input_delay_min"), "input_delay_min")
        if out["input_delay_min"] < 0:
            raise RecipeError("pnr.input_delay_min must be >= 0")
    if pick("sdc_file") is not None:
        out["sdc_file"] = str(pick("sdc_file"))
    if pick("cts_skip_fanout_le") is not None:
        v = pick("cts_skip_fanout_le")
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise RecipeError("pnr.cts_skip_fanout_le must be an integer >= 0")
        if v:
            out["cts_skip_fanout_le"] = v
    g = pick("cts_size_gates")
    if g:
        if g is True:
            g = {}
        if not isinstance(g, dict) or set(g) - GATE_KEYS:
            raise RecipeError("pnr.cts_size_gates takes " + ", ".join(sorted(GATE_KEYS)))
        for k in ("drive_re", "drives", "tolerance"):
            if k not in g:
                raise RecipeError(f"pnr.cts_size_gates.{k} is required: the "
                                  "pack says where a cell name carries its "
                                  "drive, which drives exist and how close "
                                  "to the fastest arrival is close enough")
        try:
            rx = re.compile(g["drive_re"])
        except re.error as e:
            raise RecipeError(f"pnr.cts_size_gates.drive_re: {e}")
        if rx.groups != 3:
            raise RecipeError("pnr.cts_size_gates.drive_re needs three groups "
                              "(prefix, drive, suffix)")
        drives = g["drives"]
        if not drives or not all(isinstance(d, int) and not isinstance(d, bool)
                                 and d > 0 for d in drives):
            raise RecipeError("pnr.cts_size_gates.drives must be positive integers")
        if bool(g.get("variants")) != bool(g.get("variant_re")):
            raise RecipeError("pnr.cts_size_gates: variants and variant_re go together")
        out["cts_size_gates"] = {
            "drive_re": g["drive_re"], "drives": list(drives),
            "tolerance": _num(g["tolerance"], "cts_size_gates.tolerance"),
            "variants": [str(v) for v in g.get("variants", [])],
            "variant_re": str(g.get("variant_re", ""))}
    if pick("cts_flatten_root"):
        out["cts_flatten_root"] = True
    if pick("cts_gate_port_tol") is not None:
        v = _num(pick("cts_gate_port_tol"), "cts_gate_port_tol")
        if v < 0:
            raise RecipeError("pnr.cts_gate_port_tol must be >= 0 (0 = off)")
        if v:
            if not out.get("cts_size_gates"):
                raise RecipeError("pnr.cts_gate_port_tol needs pnr.cts_size_gates")
            out["cts_gate_port_tol"] = v
    du = pick("repair_dont_use")
    if du:
        if isinstance(du, str):
            du = [du]
        if not isinstance(du, list) or not all(isinstance(x, str) and x for x in du):
            raise RecipeError("pnr.repair_dont_use must be a list of regexes")
        for x in du:
            try:
                re.compile(x)
            except re.error as e:
                raise RecipeError(f"pnr.repair_dont_use {x!r}: {e}")
        out["repair_dont_use"] = list(du)
    if pick("spef_restore_pincap") is not None:
        v = pick("spef_restore_pincap")
        if not isinstance(v, bool):
            raise RecipeError("pnr.spef_restore_pincap must be true or false")
        out["spef_restore_pincap"] = v
    if pick("placement_check") is not None:
        v = pick("placement_check")
        if v not in ("warn", "fail"):
            raise RecipeError("pnr.placement_check must be 'warn' or 'fail' "
                              "(absent = off)")
        out["placement_check"] = v
    if pick("hold_corner"):
        out["hold_corner"] = pick("hold_corner")
    for k in ("hold_margin", "setup_margin"):
        if pick(k) is not None:
            out[k] = _margin(pick(k), k)
    return out


def env(opts: dict, hold_libs: Sequence[Path] = (),
        tt_libs: Sequence[Path] = (), cts_args: Sequence[str] = ()
        ) -> Dict[str, str]:
    """Environment for a pack's P&R command from parse()'s options. `hold_libs`
    are the pack's hold-corner liberty files (needed when hold_corner is set),
    `tt_libs` the setup-corner ones."""
    e: Dict[str, str] = {
        "CTS_RECIPE_TCL": str(TCL), "SPEF_EXPORT_TCL": str(SPEF_TCL),
        "SPEF_RESTORE_PINCAP": "1" if opts.get("spef_restore_pincap", True)
        else "0"}
    if "cts_skip_fanout_le" in opts:
        e["CTS_SKIP_FANOUT_LE"] = str(opts["cts_skip_fanout_le"])
    if cts_args:
        e["CTS_EXTRA_ARGS"] = " ".join(cts_args)
    if opts.get("cts_flatten_root"):
        e["CTS_FLATTEN_ROOT"] = "1"
    g = opts.get("cts_size_gates")
    if g:
        e["CTS_SIZE_GATES"] = "1"
        e["CTS_GATE_DRIVE_RE"] = g["drive_re"]
        e["CTS_GATE_DRIVES"] = " ".join(str(d) for d in g["drives"])
        e["CTS_GATE_TOL"] = f"{g['tolerance']:g}"
        if g["variants"]:
            e["CTS_GATE_VARIANTS"] = " ".join(g["variants"])
            e["CTS_GATE_VARIANT_RE"] = g["variant_re"]
    if opts.get("cts_gate_port_tol"):
        e["CTS_GATE_PORT_TOL"] = f"{opts['cts_gate_port_tol']:g}"
    if opts.get("repair_dont_use"):
        e["REPAIR_DONT_USE_RE"] = " ".join(opts["repair_dont_use"])
    if opts.get("hold_corner"):
        if not hold_libs:
            raise RecipeError("pnr.hold_corner needs a pack hold corner with "
                              "liberties (profile corners: hold)")
        e["LIB_FILES_HOLD"] = " ".join(str(l) for l in hold_libs)
        e["LIB_FILES_TT"] = " ".join(str(l) for l in tt_libs)
    for k, name in (("hold_margin", "HOLD_MARGIN"), ("setup_margin", "SETUP_MARGIN")):
        if k in opts:
            e[name] = f"{opts[k]:g}"
    return e


def _sdc_commands(sdc: str) -> List[str]:
    """The SDC's commands, comments removed and continuation lines joined
    (a `#` that starts a command, or follows `;`, starts a comment)."""
    text = re.sub(r"\\\n", " ", sdc)
    out = []
    for line in text.splitlines():
        for cmd in line.split(";"):
            cmd = cmd.strip()
            if not cmd or cmd.startswith("#"):
                if cmd.startswith("#"):
                    break           # the rest of the line is the comment
                continue
            out.append(cmd)
    return out


def _words(cmd: str) -> List[str]:
    """The Tcl words of one command: a braced, bracketed or quoted word is
    kept whole (with its delimiters), a backslash escapes the next
    character."""
    words, i, n = [], 0, len(cmd)
    while i < n:
        if cmd[i].isspace():
            i += 1
            continue
        j, depth, quote = i, 0, False
        while j < n:
            ch = cmd[j]
            if ch == "\\":
                j += 2
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            elif ch == '"' and (j == i or quote) and depth == 0:
                quote = not quote
            elif ch.isspace() and depth <= 0 and not quote:
                break
            j += 1
        words.append(cmd[i:j])
        i = j
    return words


def _arg_after(cmd: str, flag: str) -> Optional[str]:
    """The word after `flag` in `cmd`, a bracketed or braced word whole."""
    w = _words(cmd)
    for k in range(1, len(w) - 1):
        if w[k] == flag:
            return w[k + 1]
    return None


_PLAIN_NAME = re.compile(r"^[^\s$\[\]{}\"\\;]+$")


def _literal(word: str) -> Optional[str]:
    """The name a word spells when it needs no substitution: `n`, `{n}` or
    `"n"`; else None (a variable, a command, an escape, a list)."""
    if len(word) >= 2 and word[0] + word[-1] in ('{}', '""'):
        word = word[1:-1].strip()
    return word if _PLAIN_NAME.match(word) else None


def _object_in(word: str, commands) -> Optional[str]:
    """The one literal object of `[cmd x]` (cmd in `commands`), else None."""
    if not (word.startswith("[") and word.endswith("]")):
        return None
    w = _words(word[1:-1])
    if len(w) != 2 or w[0] not in commands:
        return None
    name = _literal(w[1])
    return name if name and not set(name) & set("*?") else None


def _create_clock_name(cmd: str) -> Optional[str]:
    """The name a create_clock command gives: its -name, else the name of
    its source object (a port or pin given as `x`, `{x}`, `[get_ports x]` or
    `[get_pins x]`), which is what STA names the clock after. RecipeError
    when the name needs substitution or the source is not one literal
    object; None for a command without either."""
    w = _words(cmd)[1:]
    name, sources, k = None, [], 0
    while k < len(w):
        if w[k] == "-name" and k + 1 < len(w):
            name = w[k + 1]
            k += 2
        elif w[k] in ("-period", "-waveform", "-comment") and k + 1 < len(w):
            k += 2
        elif w[k].startswith("-"):
            k += 1
        else:
            sources.append(w[k])
            k += 1
    if name is not None:
        lit = _literal(name)
        if lit is None:
            raise RecipeError(
                f"input_delay_min: the SDC names its clock by substitution "
                f"(create_clock -name {name}); give the SDC a set_input_delay "
                "with that clock, or put the set_input_delay -min into it")
        return lit
    if not sources:
        return None
    src = sources[0]
    lit = (_object_in(src, ("get_ports", "get_pins")) if src.startswith("[")
           else _literal(src))
    if lit is None or set(lit) & set("*?"):
        raise RecipeError(
            f"input_delay_min: create_clock with no -name on {src}: the clock "
            "is named after its source only when that is one port or pin "
            "written out; give it a -name")
    return lit


def _set_once(sdc_cmds: List[str], var: str) -> bool:
    return sum(1 for c in sdc_cmds
               if (_words(c) + ["", ""])[:2] == ["set", var]) == 1


def _input_delay_clock(ref: str, cmds: List[str]) -> Tuple[str, str]:
    """(key, spelling) of a set_input_delay -clock word: the key is the
    clock name when the word spells one (`clk`, `{clk}`, `"clk"`,
    `[get_clocks clk]`), else the word itself. A word with a variable is
    used as written only when every variable in it is set once, at the top
    level, in the SDC (its value at the end of the file is the one it had);
    other substitutions are refused."""
    lit = _literal(ref) or _object_in(ref, ("get_clocks",))
    if lit:
        return lit, (ref if ref[0] != '"' else "[get_clocks {%s}]" % lit)
    names = re.findall(r"\$\{?(\w+)", ref)
    if "$" in ref and (not names or not all(_set_once(cmds, v) for v in names)):
        raise RecipeError(
            f"input_delay_min: the SDC's set_input_delay names its clock by "
            f"substitution (-clock {ref}) of a variable it does not set once; "
            "put the set_input_delay -min into the SDC")
    return ref, ref


def append_input_delay_min(sdc: str, value: float, clock: Optional[str] = None
                           ) -> str:
    """`sdc` plus a set_input_delay -min on every non-clock input. `clock` is
    the clock to time it against; by default the clock the SDC's own
    set_input_delay commands use (as they spell it; `clk`, `"clk"` and
    `[get_clocks clk]` are one clock), else the SDC's one clock
    (`[get_clocks {name}]`, the name of create_clock's -name or of its
    source port or pin). Several clocks, none, or a name only substitution
    gives, are refused: give the SDC its own minimum input delay then."""
    if clock is None:
        cmds = _sdc_commands(sdc)
        used: Dict[str, str] = {}
        for c in cmds:
            if c.split()[0] == "set_input_delay":
                ref = _arg_after(c, "-clock")
                if ref:
                    key, spelling = _input_delay_clock(ref, cmds)
                    used.setdefault(key, spelling)
        if len(used) == 1:
            clock = next(iter(used.values()))
        elif len(used) > 1:
            raise RecipeError("input_delay_min: the SDC times its inputs "
                              f"against several clocks ({', '.join(used)}); "
                              "put the set_input_delay -min into the SDC")
        else:
            names = []
            for c in cmds:
                if c.split()[0] == "create_clock":
                    n = _create_clock_name(c)
                    if n and n not in names:
                        names.append(n)
            if len(names) > 1:
                raise RecipeError("input_delay_min: the SDC has several clocks "
                                  f"({', '.join(names)}) and no set_input_delay "
                                  "that says which the inputs use; put the "
                                  "set_input_delay -min into the SDC")
            if not names:
                raise RecipeError("input_delay_min: no clock found in the SDC")
            clock = "[get_clocks {%s}]" % names[0]
    if not sdc.endswith("\n"):
        sdc += "\n"
    return (sdc + "\n# pnr.input_delay_min: minimum input delay on every data input\n"
            f"set_input_delay -min {value:g} -clock {clock} [all_inputs -no_clocks]\n")
