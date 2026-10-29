#!/usr/bin/env python3
"""Place and route a mapped netlist with stock OpenROAD-flow-scripts (ORFS).

    python3 -m circt_flow.orfs_pnr <netlist.v> <run_dir> --top <module>
        (--sdc <file> | --clock-port <port> --clock-period <t>
                        [--clock-latency <t>])
        [--platform asap7] [--utilization 40] [--place-density D]
        [--set VAR=VALUE ...] [--image openroad/orfs:<tag>] [--target finish]
        [--tie-hi CELL:PIN --tie-lo CELL:PIN] [--platform-dir <dir>]

ORFS takes a pre-synthesised netlist through SYNTH_NETLIST_FILES: its synth
step then copies the netlist to 1_2_yosys.v instead of running Yosys
(scripts/synth_preamble.tcl), and floorplan onwards is ORFS's own flow on the
platform it names. Nothing in ORFS is modified or overridden, so the result
is a stock-ORFS result for that platform.

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
             platform_dir: Path | None = None) -> int:
    local = os.environ.get("ORFS_HOME")
    root = local or ORFS_ROOT
    make = (f"make -C {root}/flow DESIGN_CONFIG={config} "
            f"WORK_HOME={run_dir / 'work'}")
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

    log = run_dir / "orfs.log"
    print(f"[orfs_pnr] {a.image}: make {a.target} -> {log}")
    rc = run_orfs(a.image, run_dir, config, [a.target, "metadata-generate"],
                  log, platform_dir)
    metrics = collect(run_dir, a.platform, a.top)
    metrics["status"] = "ok" if rc == 0 else f"orfs exited {rc}"
    (run_dir / "pnr_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    for k in sorted(metrics):
        print(f"[orfs_pnr]   {k}: {metrics[k]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
