#!/usr/bin/env python3
"""Place and route a mapped netlist with stock OpenROAD-flow-scripts (ORFS).

    python3 -m circt_flow.orfs_pnr <netlist.v> <run_dir> --top <module>
        (--sdc <file> | --clock-port <port> --clock-period <t>
                        [--clock-latency <t>])
        [--platform asap7] [--utilization 40] [--place-density D]
        [--set VAR=VALUE ...] [--image openroad/orfs:<tag>] [--target finish]
        [--tie-hi CELL:PIN --tie-lo CELL:PIN] [--platform-dir <dir>]
        [--no-vt-swap] [--dont-use PATTERN ...] [--wire-rc <rc.tcl>]
        [--cts-buffer CELL ...] [--cts-cluster-size N]
        [--cts-cluster-diameter UM] [--cts-arg FLAG ...]
        [--hold-margin T] [--setup-margin T]
        [--from-dir <placed run dir> --from-stage cts|route]

ORFS takes a pre-synthesised netlist through SYNTH_NETLIST_FILES: its synth
step then copies the netlist to 1_2_yosys.v instead of running Yosys
(scripts/synth_preamble.tcl), and floorplan onwards is ORFS's own flow on the
platform it names. Nothing in ORFS is modified; only its documented
variables (and, with --dont-use, its PRE_<step>_TCL hooks) are set, and
without --no-vt-swap / --dont-use / --set the result is a stock-ORFS result
for that platform.

This writes <run_dir>/config.mk (and constraint.sdc unless --sdc is given),
runs `make <target>` and `make metadata-generate` in the ORFS image with
WORK_HOME=<run_dir>/work, and writes <run_dir>/pnr_metrics.json from ORFS's
metadata.json. The run directory is mounted at its own path, so every path
in config.mk is the host path. With ORFS_HOME set (inside the circt-abc
image, which is built on the ORFS image), ORFS runs in place instead of in
a container.

With --platform-dir, the platform comes from that directory instead of
ORFS's own platforms/ (a tech pack's ORFS platform, doc/techpacks.md):
config.mk sets PLATFORM_DIR, which ORFS's scripts/variables.mk takes ahead
of its built-in platforms, and the directory is mounted read-only. ORFS
itself is still unmodified.

With --tie-hi/--tie-lo, every constant the netlist drives with a literal
(`assign n = 1'h1;`, or a literal on an instance pin) is driven by a tie
cell instead. OpenROAD reads a literal 1/0 net as a power/ground net, which
the router then refuses (DRT-0305); ORFS's own Yosys flow ties constants the
same way (hilomap), and its floorplan step duplicates each tie cell per load
(repair_tie_fanout).

With --no-vt-swap, OpenROAD's repair_timing may not change a cell's
threshold-voltage flavour: ORFS's SKIP_VT_SWAP and SKIP_CRIT_VT_SWAP stop
the swap in the floorplan, CTS and global-route repair_timing calls, and
OPT_POST_GRT_WNS=0 drops the extra post-global-route `repair_timing
-sequence "vt_swap reroute"` that global_route.tcl hard-codes (the SKIP_*
flags do not reach it). Sizing and buffering still happen. Each --dont-use
PATTERN (a cell-name glob, repeatable) is added to the platform's
DONT_USE_CELLS before every step that runs repair_design/repair_timing, by a
Tcl hook (dont_use.tcl) on the PRE_<step>_TCL variables; it keeps the
resizer from choosing those cells (e.g. `*_ASAP7_75t_SL`, the fastest
flavour) for new buffers and resizes. It does not stop repair_timing's Vt
swap (measured on ASAP7: swaps to SL still happen; use --no-vt-swap for
that), and leaves the cells already in the
netlist alone.

Clock tree and repair controls (all ORFS variables, none of them cell
names in this code: a tech pack declares its clock buffers):
--cts-buffer CELL (repeatable) is the buffer list of clock_tree_synthesis
(CTS_BUF_LIST); --cts-cluster-size / --cts-cluster-diameter its sink
clustering; --cts-arg adds any other clock_tree_synthesis flag (for example
-no_insertion_delay, which stops CTS padding every branch to match the clock
gates' own delay). Any of them sets CTS_ARGS, which in ORFS replaces the
variables it would otherwise assemble, so the result is ORFS's own default
(-sink_clustering_enable -repair_clock_nets) plus what was asked.
--hold-margin / --setup-margin are HOLD_SLACK_MARGIN / SETUP_SLACK_MARGIN, the
slack the post-CTS and post-route repair_timing works to (a positive
hold margin makes it over-fix hold; a negative one skips it).

With --wire-rc, OpenROAD's wire RC (set_layer_rc / set_wire_rc) comes from
that Tcl file instead of the platform's setRC.tcl: it is copied into the run
directory and named as SET_RC_TCL on the make command line, which overrides
the platform config.mk's assignment (the flow passes a tech pack's
`wire_rc:` entry for the setup corner, wire_rc.py).

Restart (--from-dir / --from-stage, used by the closure ladder,
doc/ladder.md). A run with --target place (or cts) leaves ORFS's results
for the stages up to there under <run_dir>/work. A second run with
--from-dir <that run dir> --from-stage cts (or route) copies those results
and logs, with their modification times (ORFS's make decides what is stale
by them), plus the run's input files, and lets make do the remaining stages
only. Every run writes stage_manifest.json: its inputs (netlist, SDC, hook
and RC files, platform directory) as SHA-256, the tool (image), and every
config.mk variable grouped by the first ORFS stage it can change. The restart
is refused unless the stages before --from-stage have the same inputs, tool
and variables as in the source run; a variable that this file does not know
counts as belonging to the first stage. The result of a restart is the result
of a straight run with the same variables (checked in the ladder's tests and
in examples/shift-select/README.md).

The clock period is in the platform's own time unit (ps for asap7). The
generated SDC is ORFS's own asap7 template: a clock on --clock-port, a
virtual clock of the same period for the I/O, input and output delays of 20%
of the period on every other port against the virtual clock, and
--clock-latency (default 0) on both clocks. ORFS's own designs set that
latency per design.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_IMAGE = "openroad/orfs:26Q3-277-gf9ec54a6d"
ORFS_ROOT = "/OpenROAD-flow-scripts"

# Metrics copied from ORFS's metadata.json into pnr_metrics.json, under
# shorter names. A key ORFS did not produce (an earlier --target, say) is
# left out rather than reported as zero.
METRICS = {
    "synth_instance_count": "synth__design__instance__count",
    "instance_count": "finish__design__instance__count",
    "instance_area": "finish__design__instance__area",
    "stdcell_area": "finish__design__instance__area__stdcell",
    "core_area": "finish__design__core__area",
    "utilization": "finish__design__instance__utilization",
    "setup_ws": "finish__timing__setup__ws",
    "setup_tns": "finish__timing__setup__tns",
    "hold_ws": "finish__timing__hold__ws",
    "power_total": "finish__power__total",
    "drc_errors": "detailedroute__route__drc_errors",
    "wirelength": "detailedroute__route__wirelength",
}


def write_sdc(path: Path, top: str, clock_port: str, period: float,
              latency: float) -> None:
    path.write_text(f"""current_design {top}

set clk_name core_clock
set clk_port_name {clock_port}
set clk_period {period}
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]

create_clock -name $clk_name -period $clk_period $clk_port
set clk_io_name vclk_$clk_name
create_clock -name $clk_io_name -period $clk_period
set_clock_latency {latency} [get_clocks $clk_name]
set_clock_latency {latency} [get_clocks $clk_io_name]

set non_clock_inputs [all_inputs -no_clocks]

set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name [all_outputs]
""")


# ---------------------------------------------------------------------------
# Constants in a netlist: a small tokenizer finds the 1-bit literals that are
# nets of the design, and `tie_constants` drives each from a tie cell.
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lcomment>//[^\n]*)
  | (?P<bcomment>/\*.*?(?:\*/|\Z))
  | (?P<attr>\(\*(?!\s*\)).*?(?:\*\)|\Z))
  | (?P<string>"(?:[^"\\\n]|\\.)*"?)
  | (?P<esc>\\\S+)
  | (?P<number>(?:\d[\d_]*)?\s*'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+|\d[\d_]*)
  | (?P<ident>[A-Za-z_$][\w$]*)
  | (?P<punct>.)
""", re.X | re.S)

_ONE_BIT = re.compile(r"1\s*'\s*[sS]?[bBoOdDhH]\s*([01])_*$")

# Constructs whose literals are not nets of the design (constants the
# language requires, or code that is not structure): skipped up to the
# closing keyword (or the `;` of a statement).
_SKIP_TO = {"function": "endfunction", "task": "endtask",
            "generate": "endgenerate", "specify": "endspecify",
            "primitive": "endprimitive", "table": "endtable"}


def _tokens(text: str) -> list:
    """(kind, start, end) of every token that is part of the design: white
    space, comments, attributes `(* ... *)` and compiler directive lines are
    dropped, strings and escaped identifiers are single tokens, and everything
    that is not a net of the design is dropped too: a parameter override
    `#( ... )`, a `parameter` / `localparam` / `defparam` statement, and the
    bodies of function, task, generate, specify and primitive."""
    toks = []
    for m in _TOKEN.finditer(text):
        k = m.lastgroup
        if k in ("ws", "lcomment", "bcomment", "attr"):
            continue
        toks.append((k, m.start(), m.end()))
    out, i, n = [], 0, len(toks)

    def word(j):
        return text[toks[j][1]:toks[j][2]]
    while i < n:
        k, a, b = toks[i]
        t = text[a:b]
        if k == "punct" and t == "`" and \
                not text[text.rfind("\n", 0, a) + 1:a].strip():
            # a compiler directive line (`define, `include, ...) with its
            # backslash continuations: not structure
            end = a
            while True:
                nl = text.find("\n", end)
                nl = len(text) if nl < 0 else nl
                if nl > 0 and text[nl - 1] == "\\" and nl < len(text):
                    end = nl + 1
                    continue
                end = nl
                break
            while i < n and toks[i][1] < end:
                i += 1
            continue
        if k == "punct" and t == "#" and i + 1 < n and word(i + 1) == "(":
            depth, j = 0, i + 1
            while j < n:
                if toks[j][0] == "punct" and word(j) == "(":
                    depth += 1
                elif toks[j][0] == "punct" and word(j) == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        if k == "ident" and t in ("parameter", "localparam", "defparam"):
            while i < n and not (toks[i][0] == "punct" and word(i) == ";"):
                i += 1
            i += 1
            continue
        if k == "ident" and t in _SKIP_TO:
            end = _SKIP_TO[t]
            while i < n and not (toks[i][0] == "ident" and word(i) == end):
                i += 1
            i += 1
            continue
        out.append(toks[i])
        i += 1
    return out


def _find_ties(text: str) -> list:
    """The literals `tie_constants` drives: [(kind, start, end, value, extra)],
    in text order. kind is `assign` (`assign x = 1'b0;`, one target token; the
    whole statement is replaced by a tie cell driving the target) or `net` (the
    literal is replaced by a net the tie cell drives). `net` covers a pin
    connection `.P(1'b0)`, an element of a concatenation `{a, 1'b0}`, a
    positional argument, and the constant driver of an `assign` with a bit
    select or several targets, with a delay, or of a `wire w = 1'b0;`. Only a
    literal of width 1 in any base, with underscores, signs and white space as
    Verilog allows (`1'b0`, `1'sB1`, `1 'd0`, `1'h0_`), is a constant; x and z
    digits and wider literals are left alone. A one-bit literal anywhere else
    (an operand of an operator, a ternary branch, a lone parenthesis, an event)
    is an expression, not a net: it is not rewritten, and `remaining_constants`
    reports it."""
    toks = _tokens(text)
    txt = [text[a:b] for _, a, b in toks]
    found = []
    for i, (k, a, b) in enumerate(toks):
        if k != "number":
            continue
        m = _ONE_BIT.match(txt[i])
        if not m:
            continue
        val = m.group(1)
        prev = txt[i - 1] if i else ""
        nxt = txt[i + 1] if i + 1 < len(toks) else ""
        if i >= 3 and txt[i - 3] == "assign" and prev == "=" and nxt == ";" \
                and toks[i - 2][0] in ("ident", "esc"):
            found.append(("assign", toks[i - 3][1], toks[i + 1][2], val,
                          txt[i - 2]))
        elif prev == "(" and nxt == ")" and i >= 3 and txt[i - 3] == ".":
            found.append(("net", a, b, val, None))
        elif prev in ("{", ",") and nxt in (",", "}", ")"):
            found.append(("net", a, b, val, None))
        elif prev == "(" and nxt == "," and not (i >= 3 and txt[i - 3] == "."):
            found.append(("net", a, b, val, None))       # first positional port
        elif prev == "=" and nxt in (";", ",") \
                and not (i >= 2 and txt[i - 2] in ("=", "!", "<", ">")):
            # the right-hand side of a continuous assignment or a net
            # declaration: look back to the first token of the statement
            j = i - 1
            while j > 0 and txt[j - 1] not in (";", "endmodule", ")") \
                    and not (txt[j - 1] == "begin" or txt[j - 1] == "end"):
                j -= 1
            if txt[j] in ("assign", "wire", "tri", "wand", "wor", "supply0",
                          "supply1") and txt[j] != "":
                found.append(("net", a, b, val, None))
    return found


def remaining_constants(text: str) -> int:
    """How many one-bit literals of the design are left that `tie_constants`
    does not rewrite (operands, ternary branches, lone parentheses, events):
    a P&R tool may read such a literal as a supply, so they are warned about."""
    toks = _tokens(text)
    n = sum(1 for k, a, b in toks if k == "number"
            and _ONE_BIT.match(text[a:b]))
    return n - len(_find_ties(text)) + _generate_pin_literals(text)


def _generate_pin_literals(text: str) -> int:
    """One-bit literals on instance pins (`.P(1'b0)`) inside `generate` bodies:
    the bodies are skipped (they are not rewritten), but they hold instances,
    so these constants are counted for the warning."""
    toks = [(k, a, b) for m in _TOKEN.finditer(text)
            for k, a, b in [(m.lastgroup, m.start(), m.end())]
            if k not in ("ws", "lcomment", "bcomment", "attr")]
    txt = [text[a:b] for _, a, b in toks]
    n, depth = 0, 0
    for i, (k, a, b) in enumerate(toks):
        if k == "ident" and txt[i] == "generate":
            depth += 1
        elif k == "ident" and txt[i] == "endgenerate":
            depth = max(0, depth - 1)
        elif depth and k == "number" and _ONE_BIT.match(txt[i]) and i >= 3 \
                and txt[i - 1] == "(" and txt[i - 3] == "." \
                and i + 1 < len(toks) and txt[i + 1] == ")":
            n += 1
    return n


def count_constants(text: str) -> int:
    """How many 1-bit literal constants `tie_constants` would drive (see
    `_find_ties`): assignments, pin connections, elements of concatenations,
    positional arguments, assignment and net-declaration right-hand sides,
    outside comments, strings, attributes, escaped identifiers, parameter
    overrides and declarations, and the skipped bodies."""
    return len(_find_ties(text))


def untied_constants_warning(text: str, tie: dict) -> str:
    """A warning when the netlist has constants the P&R tool may read as
    supplies: with no tie cells declared, every constant `tie_constants` would
    drive; with them, the one-bit literals it does not rewrite (see
    `remaining_constants`). A P&R tool then holds those constants on its power
    nets and its exported netlist carries undriven `one_` / `zero_` wires,
    which a gate-level simulator reads as X (every state register downstream of
    a constant can then go X). Else ''."""
    tied = bool(tie and tie.get("hi") and tie.get("lo"))
    n = remaining_constants(text) if tied else (
        count_constants(text) + remaining_constants(text))
    if not n:
        return ""
    if tied:
        return (f"WARNING: the netlist has {n} one-bit constant(s) (1'h0 / "
                "1'h1) in expressions (operands, ternary branches, "
                "parentheses) that the tie rewrite does not touch; a P&R "
                "tool may read them as supplies (undriven `one_` / `zero_` "
                "nets, X in gate-level simulation).")
    return (f"WARNING: the netlist has {n} constant(s) (1'h0 / 1'h1) and the "
            "tech pack declares no `tie: {hi: CELL:PIN, lo: CELL:PIN}`. The P&R "
            "export will leave them as undriven nets (one_ / zero_), which "
            "gate-level simulation reads as X. Add the pack's tie cells.")


def tie_constants(text: str, hi: tuple[str, str], lo: tuple[str, str]
                  ) -> tuple[str, int]:
    """Drive each 1-bit literal from a tie cell (see `_find_ties`). Returns the
    new text and the number of tie cells added. The tie cells and the nets
    they drive are declared in the module that uses them, right after the `;`
    that ends its header (on lines of their own), so a netlist of several
    modules, or a module on one line, works. The cells are numbered in text
    order across the whole text, skipping any number whose names the text
    already uses, so a second pass over a tied netlist ties nothing."""
    cells = {"1": hi, "0": lo}
    found = _find_ties(text)
    if not found:
        return text, 0
    toks = _tokens(text)
    txt = [text[a:b] for _, a, b in toks]
    used = {t.lstrip("\\") for (k, _a, _b), t in zip(toks, txt)
            if k in ("ident", "esc")}
    heads = []                       # end of each module header (its `;`)
    for i, t in enumerate(txt):
        if toks[i][0] == "ident" and t in ("module", "macromodule"):
            depth, j = 0, i + 1
            while j < len(toks):
                if txt[j] == "(":
                    depth += 1
                elif txt[j] == ")":
                    depth -= 1
                elif txt[j] == ";" and depth == 0:
                    break
                j += 1
            heads.append(toks[min(j, len(toks) - 1)][2])
    edits = []                       # (start, end, replacement)
    decls: dict = {}                 # header end -> declaration lines
    k = 0
    for kind, a, b, val, target in found:
        k += 1
        while f"tie_const_{k}" in used or f"tie_const_{k}_net" in used:
            k += 1
        cell, pin = cells[val]
        name = f"tie_const_{k}"
        mod = max((h for h in heads if h <= a), default=None)
        if mod is None:
            raise ValueError("a constant outside any module cannot be tied")
        if kind == "assign":
            sep = " " if target.startswith("\\") else ""
            edits.append((a, b, f"{cell} {name} (.{pin}({target}{sep}));"))
        else:
            edits.append((a, b, f"{name}_net"))
            decls.setdefault(mod, []).append(
                f"  wire {name}_net;\n  {cell} {name} (.{pin}({name}_net));")
    for h, d in decls.items():
        edits.append((h, h, "\n" + "\n".join(d)))
    out, pos = [], 0                 # one pass over the text
    for a, b, r in sorted(edits, key=lambda e: (e[0], e[1])):
        out.append(text[pos:a])
        out.append(r)
        pos = b
    out.append(text[pos:])
    return "".join(out), len(found)


# The ORFS steps that run repair_design / repair_timing, as their
# PRE_<step>_TCL hook names (scripts/util.tcl source_step_tcl).
REPAIR_STEPS = ("FLOORPLAN", "GLOBAL_PLACE", "RESIZE", "CTS", "GLOBAL_ROUTE")
NO_VT_SWAP = [("SKIP_VT_SWAP", "1"), ("SKIP_CRIT_VT_SWAP", "1"),
              ("OPT_POST_GRT_WNS", "0")]


def vt_settings(run_dir: Path, no_vt_swap: bool,
                dont_use: list[str]) -> list[tuple[str, str]]:
    """config.mk variables for --no-vt-swap and --dont-use; writes the
    dont-use hook into run_dir. User --set values come after these, so they
    win."""
    out = list(NO_VT_SWAP) if no_vt_swap else []
    if dont_use:
        hook = run_dir / "dont_use.tcl"
        pats = " ".join(dont_use)
        hook.write_text(
            "# --dont-use: keep the resizer off these cells (written by "
            "circt_flow.orfs_pnr)\n"
            "set ::env(DONT_USE_CELLS) \"[expr {[info exists "
            "::env(DONT_USE_CELLS)] ? $::env(DONT_USE_CELLS) : {}}] "
            f"{pats}\"\n"
            f"set_dont_use {{{pats}}}\n")
        out += [(f"PRE_{s}_TCL", str(hook)) for s in REPAIR_STEPS]
    return out


def cts_settings(buffers: list[str], cluster_size: int | None,
                 cluster_diameter: float | None,
                 extra_args: list[str]) -> list[tuple[str, str]]:
    """config.mk variables for the clock-tree options: ORFS's CTS_ARGS,
    which replaces the arguments ORFS builds from CTS_BUF_LIST and the
    cluster variables, so ORFS's own default arguments are repeated here."""
    if not (buffers or cluster_size or cluster_diameter or extra_args):
        return []
    args = ["-sink_clustering_enable", "-repair_clock_nets"]
    if buffers:
        args += ["-buf_list", "{" + " ".join(buffers) + "}"]
    if cluster_size:
        args += ["-sink_clustering_size", str(cluster_size)]
    if cluster_diameter:
        args += ["-sink_clustering_max_diameter", f"{cluster_diameter:g}"]
    return [("CTS_ARGS", " ".join(args + list(extra_args)))]


def margin_settings(hold: float | None, setup: float | None
                    ) -> list[tuple[str, str]]:
    out = []
    if hold is not None:
        out.append(("HOLD_SLACK_MARGIN", f"{hold:g}"))
    if setup is not None:
        out.append(("SETUP_SLACK_MARGIN", f"{setup:g}"))
    return out



# ---------------------------------------------------------------------------
# Stage restart: which ORFS stage each config.mk variable first acts in, the
# manifest every run writes, and the copy a restart starts from.
# ---------------------------------------------------------------------------

STAGES = ("floorplan", "place", "cts", "route", "finish")
# ORFS numbers its result files by stage: 1 synth, 2 floorplan, 3 place, 4 cts,
# 5 route, 6 finish. A restart at a stage keeps the files below its number.
STAGE_NUMBER = {"floorplan": 2, "place": 3, "cts": 4, "route": 5, "finish": 6}
RESTART_STAGES = ("cts", "route")
# The stage a variable is first read in, for the variables a restart may
# change. Every other variable (and every unknown one) belongs to "floorplan",
# the first stage this module knows, so a restart refuses a change of it.
# HOLD_SLACK_MARGIN is read by the pre-CTS repair only when
# ENABLE_PLACE_REPAIR_TIMING is on (stage_of_var handles that).
_VAR_STAGE = {
    **{v: "cts" for v in (
        "HOLD_SLACK_MARGIN", "CTS_ARGS", "CTS_SNAPSHOTS",
        "SKIP_CTS_REPAIR_TIMING", "PRE_CTS_TCL", "POST_CTS_TCL")},
    **{v: "route" for v in (
        "GLOBAL_ROUTE_ARGS", "DETAILED_ROUTE_ARGS", "SKIP_INCREMENTAL_REPAIR",
        "SKIP_DETAILED_ROUTE", "SKIP_ANTENNA_REPAIR",
        "SKIP_ANTENNA_REPAIR_POST_DRT", "MAX_REPAIR_ANTENNAS_ITER_GRT",
        "MAX_REPAIR_ANTENNAS_ITER_DRT", "FILL_CELLS", "PRE_GLOBAL_ROUTE_TCL",
        "POST_GLOBAL_ROUTE_TCL", "PRE_DETAIL_ROUTE_TCL",
        "POST_DETAIL_ROUTE_TCL")},
}
# Variables whose value is a path into the run directory: the manifest
# records the file's content instead (the directory differs between runs).
_PATH_VARS = {"SYNTH_NETLIST_FILES", "VERILOG_FILES", "SDC_FILE"}
_HOOK_SUFFIX = "_TCL"


def stage_of_var(name: str, value: str, env: dict[str, str]) -> str:
    """The first ORFS stage the variable can change the result in."""
    stage = _VAR_STAGE.get(name, "floorplan")
    if name == "HOLD_SLACK_MARGIN":
        try:
            negative = float(value) < 0
        except ValueError:
            negative = True
        # A negative margin is read by the floorplan's repair; a pre-CTS
        # repair (ENABLE_PLACE_REPAIR_TIMING) reads any margin in place.
        if negative or env.get("ENABLE_PLACE_REPAIR_TIMING", "0") not in (
                "0", ""):
            return "floorplan"
    return stage


class HashError(Exception):
    """A file that is part of a manifest cannot be hashed."""


def _sha(path: Path) -> str:
    """SHA-256 of a file's content (an unreadable file is a HashError)."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        raise HashError(f"cannot read {path} to hash it "
                        f"({e.strerror or e}); it is part of a manifest")
    return h.hexdigest()


def _tree_sha(root: Path) -> str:
    """Hash of a directory tree: names, kinds and contents. Symbolic links are
    followed (a link back to a directory being walked is cut); an unreadable
    file or a broken link is a HashError."""
    import hashlib
    h = hashlib.sha256()
    active: set = set()

    def walk(d: Path, rel: str) -> None:
        real = os.path.realpath(d)
        if real in active:
            h.update(f"loop {rel}\0".encode())
            return
        active.add(real)
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            raise HashError(f"cannot list {d} to hash it "
                            f"({e.strerror or e})")
        h.update(f"dir {rel}\0".encode())
        for n in names:
            q = d / n
            if q.is_dir():
                walk(q, f"{rel}/{n}")
            elif q.is_file():
                h.update(f"file {rel}/{n}\0{_sha(q)}\0".encode())
            else:
                raise HashError(f"{q} is not a readable file or directory "
                                "(a broken link?); it is part of a manifest")
        active.discard(real)
    walk(Path(root), "")
    return h.hexdigest()


def tool_identity(image: str) -> dict:
    """What names the ORFS install: the image reference (and the image id
    when docker can tell), or the in-place ORFS_HOME path with its version
    file's hash when it has one."""
    local = os.environ.get("ORFS_HOME")
    if local:
        ver = Path(local) / "flow" / "scripts" / "variables.yaml"
        return {"orfs_home": local,
                "variables_sha": _sha(ver) if ver.is_file() else None}
    out = {"image": image}
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format",
                            "{{.Id}}", image], capture_output=True, text=True,
                           timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            out["image_id"] = r.stdout.strip()
    except Exception:  # no docker, or not asked: the reference names it
        pass
    return out


def build_manifest(run_dir: Path, config: Path, make_vars, tool: dict,
                   platform_dir: Path | None, target: str) -> dict:
    """The run's manifest: tool, input hashes, and the config.mk variables
    grouped by first stage. File-valued variables are replaced by the file's
    hash."""
    env: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for line in config.read_text().splitlines():
        m = re.match(r"export\s+(\w+)\s*=\s*(.*)$", line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    pairs += [(k, v) for k, v in make_vars]
    env = dict(pairs)
    inputs: dict[str, str] = {}
    options: dict[str, dict[str, str]] = {s: {} for s in STAGES}
    for k, v in pairs:
        f = Path(v)
        if k in _PATH_VARS or (k.endswith(_HOOK_SUFFIX) and f.is_file()) \
                or (k == "SET_RC_TCL" and f.is_file()):
            if f.is_file():
                inputs[k] = _sha(f)
                continue
        options[stage_of_var(k, v, env)][k] = v
    if platform_dir is not None:
        inputs["platform_dir"] = _tree_sha(platform_dir)
    return {"version": 1, "tool": tool, "inputs": inputs, "options": options,
            "target": target}


def restart_diff(old: dict, new: dict, from_stage: str) -> list[str]:
    """What differs between two manifests in the stages before from_stage
    (and in the inputs and the tool): one line per difference. Empty means
    the restart is allowed."""
    out = []
    if old.get("tool") != new.get("tool"):
        out.append(f"tool: {old.get('tool')} != {new.get('tool')}")
    for k in sorted(set(old.get("inputs", {})) | set(new.get("inputs", {}))):
        if old.get("inputs", {}).get(k) != new.get("inputs", {}).get(k):
            out.append(f"input {k} differs")
    for st in STAGES[:STAGES.index(from_stage)]:
        a, b = old.get("options", {}).get(st, {}), new["options"].get(st, {})
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                out.append(f"{k} ({st} stage): {a.get(k)!r} -> {b.get(k)!r}")
    return out


def restart_key(manifest: dict, from_stage: str) -> dict:
    """The part of a manifest a restart at from_stage depends on: tool,
    inputs and the options of the stages before it."""
    return {"tool": manifest.get("tool"), "inputs": manifest.get("inputs"),
            "options": {st: manifest.get("options", {}).get(st, {})
                        for st in STAGES[:STAGES.index(from_stage)]}}


def seed_restart(src: Path, dst: Path, platform: str, top: str,
                 from_stage: str) -> None:
    """Copy ORFS's work tree of the stages before from_stage from the run dir
    src into dst, modification times kept; results of a later stage and the
    metadata of the source run are left out."""
    import shutil
    keep_below = STAGE_NUMBER[from_stage]
    for sub in ("results", "logs", "reports", "objects"):
        a = src / "work" / sub / platform / top / "base"
        if not a.is_dir():
            continue
        b = dst / "work" / sub / platform / top / "base"
        b.parent.mkdir(parents=True, exist_ok=True)
        if b.exists():
            shutil.rmtree(b)

        def ignore(d, names):
            drop = []
            for n in names:
                m = re.match(r"(\d)_", n)
                if (m and int(m.group(1)) >= keep_below) or \
                        n.startswith("metadata"):
                    drop.append(n)
            return drop
        shutil.copytree(a, b, symlinks=True, copy_function=shutil.copy2,
                        ignore=ignore)


def restore_inputs(src: Path, dst: Path, names: list[str]) -> None:
    """Overwrite dst's input files with src's bytes and modification times:
    make would otherwise see a newer netlist than the results made from it."""
    import shutil
    for n in names:
        if (src / n).is_file() and (dst / n).is_file():
            shutil.copy2(src / n, dst / n)


def write_config(path: Path, *, platform: str, top: str, netlist: Path,
                 sdc: Path, utilization: float,
                 place_density: float | None,
                 extra: list[tuple[str, str]],
                 platform_dir: Path | None = None) -> None:
    lines = [
        f"export PLATFORM = {platform}",
        *([f"export PLATFORM_DIR = {platform_dir}"] if platform_dir else []),
        f"export DESIGN_NAME = {top}",
        f"export DESIGN_NICKNAME = {top}",
        f"export SYNTH_NETLIST_FILES = {netlist}",
        # Unused once SYNTH_NETLIST_FILES is set, but the Makefile insists
        # on a value.
        f"export VERILOG_FILES = {netlist}",
        f"export SDC_FILE = {sdc}",
        f"export CORE_UTILIZATION = {utilization:g}",
        "export CORE_ASPECT_RATIO = 1",
        "export CORE_MARGIN = 2",
        # ORFS turns its CTS-time LEC on whenever kepler-formal is
        # installed; the image's build needs AVX-512 and dies with an
        # illegal instruction on CPUs without it. LEC only checks the
        # netlist, so this changes no result; --set LEC_CHECK=1 restores it.
        "export LEC_CHECK = 0",
    ]
    if place_density is not None:
        lines.append(f"export PLACE_DENSITY = {place_density:g}")
    lines += [f"export {k} = {v}" for k, v in extra]
    path.write_text("\n".join(lines) + "\n")


def run_orfs(image: str, run_dir: Path, config: Path,
             targets: list[str], log: Path,
             platform_dir: Path | None = None,
             make_vars: list[tuple[str, str]] = ()) -> int:
    local = os.environ.get("ORFS_HOME")
    root = local or ORFS_ROOT
    make = (f"make -C {root}/flow DESIGN_CONFIG={config} "
            f"WORK_HOME={run_dir / 'work'}"
            + "".join(f" {k}={v}" for k, v in make_vars))
    name = f"orfs_pnr-{os.getpid()}-{os.urandom(5).hex()}"
    script = (f"source {root}/env.sh >/dev/null && "
              + " && ".join(f"{make} {t}" for t in targets))
    if local:
        cmd = ["bash", "-c", script]
    else:
        cmd = ["docker", "run", "--rm", "--name", name,
               "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
               "-v", f"{run_dir}:{run_dir}",
               *(["-v", f"{platform_dir}:{platform_dir}:ro"]
                 if platform_dir else []),
               image, "bash", "-c", script]
    # A job that is told to stop (SIGTERM, as the closure ladder does for
    # a rung it no longer needs) stops its container too: docker run's
    # client going away does not.
    def stop(sig, frame):
        if not local:
            subprocess.run(["docker", "kill", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(128 + sig)
    import signal
    import threading
    # signal handlers can only be installed from the main thread
    main_thread = threading.current_thread() is threading.main_thread()
    old = signal.signal(signal.SIGTERM, stop) if main_thread else None
    try:
        with open(log, "w") as fh:
            return subprocess.run(cmd, stdout=fh,
                                  stderr=subprocess.STDOUT).returncode
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, old)


def _target_stage(target: str) -> str:
    """The last ORFS stage a make target completes (floorplan.. finish);
    anything else counts as the last stage."""
    return target if target in STAGES else "finish"


def collect(run_dir: Path, platform: str, top: str) -> dict:
    meta = run_dir / "work" / "reports" / platform / top / "base" / "metadata.json"
    if not meta.exists():
        return {}
    data = json.loads(meta.read_text())
    return {k: data[v] for k, v in METRICS.items() if v in data}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("netlist", type=Path)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--top", required=True)
    ap.add_argument("--sdc", type=Path)
    ap.add_argument("--clock-port")
    ap.add_argument("--clock-period", type=float,
                    help="in the platform's time unit (ps for asap7)")
    ap.add_argument("--clock-latency", type=float, default=0.0)
    ap.add_argument("--platform", default="asap7")
    ap.add_argument("--utilization", type=float, default=40)
    ap.add_argument("--place-density", type=float)
    ap.add_argument("--set", action="append", default=[], metavar="VAR=VALUE",
                    help="extra config.mk variable (repeatable)")
    ap.add_argument("--no-vt-swap", action="store_true",
                    help="stop repair_timing from swapping Vt flavours")
    ap.add_argument("--dont-use", action="append", default=[],
                    metavar="PATTERN",
                    help="cell-name glob added to the platform's "
                    "DONT_USE_CELLS (repeatable)")
    ap.add_argument("--cts-buffer", action="append", default=[],
                    metavar="CELL", help="clock buffer for CTS (repeatable)")
    ap.add_argument("--cts-cluster-size", type=int,
                    help="CTS sink clustering: sinks per cluster")
    ap.add_argument("--cts-cluster-diameter", type=float,
                    help="CTS sink clustering: cluster diameter (um)")
    ap.add_argument("--cts-arg", action="append", default=[], metavar="FLAG",
                    help="extra clock_tree_synthesis argument (repeatable)")
    ap.add_argument("--hold-margin", type=float,
                    help="HOLD_SLACK_MARGIN, in the platform's time unit")
    ap.add_argument("--setup-margin", type=float,
                    help="SETUP_SLACK_MARGIN, in the platform's time unit")
    ap.add_argument("--image", default=os.environ.get("ORFS_IMAGE",
                                                       DEFAULT_IMAGE))
    ap.add_argument("--tie-hi", metavar="CELL:PIN",
                    help="tie cell for constant 1 (e.g. the platform's "
                    "TIEHI_CELL_AND_PORT)")
    ap.add_argument("--tie-lo", metavar="CELL:PIN",
                    help="tie cell for constant 0")
    ap.add_argument("--platform-dir", type=Path,
                    help="the platform's directory, instead of ORFS's own "
                    "platforms/<platform> (mounted read-only)")
    ap.add_argument("--wire-rc", type=Path, metavar="RC_TCL",
                    help="OpenROAD wire RC Tcl (set_layer_rc/set_wire_rc) "
                    "used instead of the platform's setRC.tcl")
    ap.add_argument("--target", default="finish",
                    help="ORFS make target to stop at (default finish; "
                    "place or cts leaves a run another can restart from)")
    ap.add_argument("--plan", action="store_true",
                    help="write the run's inputs and plan_manifest.json, "
                    "then stop without running ORFS")
    ap.add_argument("--from-dir", type=Path, metavar="RUN_DIR",
                    help="restart: the run directory of an earlier "
                    "--target place/cts run (needs --from-stage)")
    ap.add_argument("--from-stage", choices=RESTART_STAGES,
                    help="restart at this ORFS stage from --from-dir")
    a = ap.parse_args(argv)
    if bool(a.from_dir) != bool(a.from_stage):
        sys.exit("orfs_pnr: --from-dir and --from-stage go together")

    if not a.netlist.is_file():
        sys.exit(f"orfs_pnr: netlist not found: {a.netlist}")
    if a.sdc is None and (a.clock_port is None or a.clock_period is None):
        sys.exit("orfs_pnr: give --sdc, or --clock-port and --clock-period")
    platform_dir = a.platform_dir.resolve() if a.platform_dir else None
    if platform_dir and not (platform_dir / "config.mk").is_file():
        sys.exit(f"orfs_pnr: {platform_dir} has no config.mk; not an ORFS "
                 "platform directory")
    extra = []
    for s in a.set:
        k, sep, v = s.partition("=")
        if not sep or not k:
            sys.exit(f"orfs_pnr: --set expects VAR=VALUE, got {s!r}")
        extra.append((k, v))

    run_dir = a.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    extra = (vt_settings(run_dir, a.no_vt_swap, a.dont_use)
             + cts_settings(a.cts_buffer, a.cts_cluster_size,
                            a.cts_cluster_diameter, a.cts_arg)
             + margin_settings(a.hold_margin, a.setup_margin) + extra)
    # Inputs are copied in, so the container sees only the run directory.
    netlist = run_dir / f"{a.top}.v"
    text = a.netlist.read_text()
    if a.tie_hi or a.tie_lo:
        if not (a.tie_hi and a.tie_lo):
            sys.exit("orfs_pnr: give both --tie-hi and --tie-lo")
        hi, lo = (tuple(x.split(":", 1)) for x in (a.tie_hi, a.tie_lo))
        text, n = tie_constants(text, hi, lo)
        print(f"[orfs_pnr] {n} constant(s) tied with {hi[0]}/{lo[0]}")
        warn = untied_constants_warning(text, {"hi": a.tie_hi, "lo": a.tie_lo})
        if warn:
            print(f"[orfs_pnr] {warn}")
    netlist.write_text(text)
    sdc = run_dir / "constraint.sdc"
    if a.sdc is not None:
        sdc.write_bytes(a.sdc.read_bytes())
    else:
        write_sdc(sdc, a.top, a.clock_port, a.clock_period, a.clock_latency)
    config = run_dir / "config.mk"
    write_config(config, platform=a.platform, top=a.top, netlist=netlist,
                 sdc=sdc, utilization=a.utilization,
                 place_density=a.place_density, extra=extra,
                 platform_dir=platform_dir)

    make_vars = []
    if a.wire_rc is not None:
        from .wire_rc import RcEntry, write_openroad_tcl
        if not a.wire_rc.is_file():
            sys.exit(f"orfs_pnr: wire RC file not found: {a.wire_rc}")
        rc_tcl = write_openroad_tcl(RcEntry("pnr", path=a.wire_rc.resolve()),
                                    run_dir / "wire_rc.tcl")
        make_vars.append(("SET_RC_TCL", str(rc_tcl)))
        print(f"[orfs_pnr] wire RC: {a.wire_rc} -> SET_RC_TCL")
    try:
        manifest = build_manifest(run_dir, config, make_vars,
                                  tool_identity(a.image), platform_dir,
                                  a.target)
    except HashError as e:
        sys.exit(f"orfs_pnr: {e}")
    inputs = [f"{a.top}.v", "constraint.sdc", "wire_rc.tcl", "dont_use.tcl"]
    if a.plan:
        (run_dir / "plan_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return 0
    if a.from_dir is not None:
        src = a.from_dir.resolve()
        mf = src / "stage_manifest.json"
        if not mf.is_file():
            sys.exit(f"orfs_pnr: {src} has no stage_manifest.json; it is not "
                     "a run to restart from")
        old = json.loads(mf.read_text())
        done = old.get("completed", [])
        need = STAGES[STAGES.index(a.from_stage) - 1]
        if need not in done:
            sys.exit(f"orfs_pnr: {src} stopped before the {need} stage "
                     f"(completed: {', '.join(done) or 'none'})")
        bad = restart_diff(old, manifest, a.from_stage)
        if bad:
            sys.exit(f"orfs_pnr: refusing to restart at {a.from_stage} from "
                     f"{src}: an input or an earlier-stage option differs:\n  "
                     + "\n  ".join(bad))
        seed_restart(src, run_dir, a.platform, a.top, a.from_stage)
        restore_inputs(src, run_dir, inputs)
        manifest["restarted_from"] = {"dir": str(src), "stage": a.from_stage}
    log = run_dir / "orfs.log"
    print(f"[orfs_pnr] {a.image}: make {a.target} -> {log}")
    rc = run_orfs(a.image, run_dir, config, [a.target, "metadata-generate"],
                  log, platform_dir, make_vars)
    # The stages this run finished: `make <target>` ran to the end only when
    # it exited 0.
    manifest["completed"] = ([s for s in STAGES
                              if STAGES.index(s) <= STAGES.index(
                                  _target_stage(a.target))] if rc == 0 else [])
    (run_dir / "stage_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    metrics = collect(run_dir, a.platform, a.top)
    metrics["status"] = "ok" if rc == 0 else f"orfs exited {rc}"
    (run_dir / "pnr_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    for k in sorted(metrics):
        print(f"[orfs_pnr]   {k}: {metrics[k]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
