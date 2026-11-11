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


_ASSIGN_CONST = re.compile(r"^(\s*)assign\s+([\w$]+|\\\S+\s)\s*=\s*1'[hb]([01])\s*;",
                           re.M)
_PIN_CONST = re.compile(r"(\.[\w$]+\s*\(\s*)1'[hb]([01])(\s*\))")


def count_constants(text: str) -> int:
    """How many 1-bit literal constants `tie_constants` would drive: the
    `assign x = 1'h0;` lines and the `.PIN(1'h1)` connections."""
    return len(_ASSIGN_CONST.findall(text)) + len(_PIN_CONST.findall(text))


def untied_constants_warning(text: str, tie: dict) -> str:
    """A warning when the netlist has constants and the profile has no tie
    cells, else ''. A P&R tool then holds those constants on its power nets
    and its exported netlist carries undriven `one_` / `zero_` wires, which a
    gate-level simulator reads as X (every state register downstream of a
    constant can then go X)."""
    n = count_constants(text)
    if not n or (tie and tie.get("hi") and tie.get("lo")):
        return ""
    return (f"WARNING: the netlist has {n} constant(s) (1'h0 / 1'h1) and the "
            "tech pack declares no `tie: {hi: CELL:PIN, lo: CELL:PIN}`. The P&R "
            "export will leave them as undriven nets (one_ / zero_), which "
            "gate-level simulation reads as X. Add the pack's tie cells.")


def tie_constants(text: str, hi: tuple[str, str], lo: tuple[str, str]
                  ) -> tuple[str, int]:
    """Drive each 1-bit literal from a tie cell. Returns the new text and
    the number of tie cells added."""
    n = 0
    cells = {"1": hi, "0": lo}

    def assign(m: re.Match) -> str:
        nonlocal n
        cell, pin = cells[m.group(3)]
        n += 1
        return (f"{m.group(1)}{cell} tie_const_{n} "
                f"(.{pin}({m.group(2).strip()}));")

    text = _ASSIGN_CONST.sub(assign, text)
    wires = []

    def pin_lit(m: re.Match) -> str:
        nonlocal n
        n += 1
        wires.append((f"tie_const_{n}_net", cells[m.group(2)]))
        return f"{m.group(1)}tie_const_{n}_net{m.group(3)}"

    text = _PIN_CONST.sub(pin_lit, text)
    if wires:
        # A flat netlist has one module: declare the nets right after its
        # port list, ahead of any use.
        decls = "".join(
            f"  wire {w};\n  {cell} {w[:-4]} (.{pin}({w}));\n"
            for w, (cell, pin) in wires)
        m = re.search(r"\bmodule\b.*?\);[^\n]*\n", text, re.S)
        text = text[:m.end()] + decls + text[m.end():]
    return text, n


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
    script = (f"source {root}/env.sh >/dev/null && "
              + " && ".join(f"{make} {t}" for t in targets))
    if local:
        cmd = ["bash", "-c", script]
    else:
        cmd = ["docker", "run", "--rm",
               "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
               "-v", f"{run_dir}:{run_dir}",
               *(["-v", f"{platform_dir}:{platform_dir}:ro"]
                 if platform_dir else []),
               image, "bash", "-c", script]
    with open(log, "w") as fh:
        return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode


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
                    help="ORFS make target to stop at (default finish)")
    a = ap.parse_args(argv)

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
    log = run_dir / "orfs.log"
    print(f"[orfs_pnr] {a.image}: make {a.target} -> {log}")
    rc = run_orfs(a.image, run_dir, config, [a.target, "metadata-generate"],
                  log, platform_dir, make_vars)
    metrics = collect(run_dir, a.platform, a.top)
    metrics["status"] = "ok" if rc == 0 else f"orfs exited {rc}"
    (run_dir / "pnr_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    for k in sorted(metrics):
        print(f"[orfs_pnr]   {k}: {metrics[k]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
