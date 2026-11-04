#!/usr/bin/env python3
"""Per-flavour summary of a mapped netlist: cell count, real area and
leakage by threshold flavour, and optionally OpenSTA's worst slack.

    vt_report.py <netlist.v> <top> --lib <lib> [--lib ...]
                 [--period-ps N --clock clk_i] [--sta [IMAGE]] [--json out]

Area and leakage come from the liberties given, which must be the REAL
per-flavour libraries of the netlist's supply, never the priced merged one
(its areas are prices). A cell's flavour is the suffix --flavour-re
captures (default `_([A-Z])$`: MNAND2_X1_S -> S). Only <top> and the
modules it instantiates are counted, so <top> may be one power domain's
module of a hierarchical netlist (STA then links that module alone).

--sta runs OpenSTA (`sta`) in the ORFS image on the netlist, with the clock
on --clock and zero input/output delays, and reports the worst setup slack.
--false-from <port> leaves that port's paths untimed: a synthesized netlist
is unbuffered, so a reset driving a thousand flops times as a multi-ns
slew that P&R's buffer tree removes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ORFS_IMAGE = "openroad/orfs:26Q3-277-gf9ec54a6d"


def lib_cells(paths):
    """{cell: (area, leakage)} from `cell(...)`, `area :` and
    `cell_leakage_power :` (0 when a cell has none)."""
    cells = {}
    for p in paths:
        name = None
        for line in Path(p).read_text().splitlines():
            m = re.match(r'\s*cell\s*\(\s*"?(\w+)"?\s*\)', line)
            if m:
                name = m.group(1)
                cells.setdefault(name, [0.0, 0.0])
                continue
            if name is None:
                continue
            m = re.match(r"\s*area\s*:\s*([-\d.eE+]+)", line)
            if m:
                cells[name][0] = float(m.group(1))
            m = re.match(r"\s*cell_leakage_power\s*:\s*([-\d.eE+]+)", line)
            if m:
                cells[name][1] = float(m.group(1))
    return {k: tuple(v) for k, v in cells.items()}


def module_texts(text):
    """{module: its text} of every module in a netlist."""
    out = {}
    for m in re.finditer(r"^module\s+(\w+)\b.*?^endmodule\b", text,
                         re.M | re.S):
        out[m.group(1)] = m.group(0)
    return out


def instances(netlist, known, top=None):
    """Library-cell instances of `top` and the modules it instantiates (a
    hierarchical netlist holds other modules too, e.g. the power flow's
    stitched one); the whole file when `top` is not defined in it."""
    inst = re.compile(r"^\s*(\w+)\s+\\?\S+\s*\(", re.M)
    text = Path(netlist).read_text()
    mods = module_texts(text)
    if top not in mods:
        return [t for t in inst.findall(text) if t in known]
    cells = []

    def walk(m):  # once per instance: a module instantiated twice counts twice
        for t in inst.findall(mods[m]):
            if t in mods and t != m:
                walk(t)
            elif t in known:
                cells.append(t)
    walk(top)
    return cells


def sta(netlist, top, libs, period_ps, clock, image, false_from=()):
    files = [Path(netlist).resolve(), *(Path(l).resolve() for l in libs)]
    mounts = sorted({str(f.parent) for f in files})
    with tempfile.TemporaryDirectory() as td:
        tcl = Path(td) / "sta.tcl"
        tcl.write_text(
            "".join(f"read_liberty {l}\n" for l in files[1:])
            + f"read_verilog {files[0]}\nlink_design {top}\n"
            f"create_clock -name clk -period {period_ps / 1000:.4f} "
            f"[get_ports {clock}]\n"
            f"set_input_delay 0 -clock clk "
            f"[delete_from_list [all_inputs] [get_ports {clock}]]\n"
            "set_output_delay 0 -clock clk [all_outputs]\n"
            + "".join(f"set_false_path -from [get_ports {p}]\n"
                      for p in false_from)
            +
            "report_worst_slack -max -digits 4\n"
            "report_tns -max -digits 4\n"
            "exit\n")
        # $OPENSTA (as circt_flow's STA and vt_swap stages take it), else
        # OpenSTA in the ORFS image.
        local = os.environ.get("OPENSTA")
        cmd = ([local, "-no_init", "-no_splash", "-exit", str(tcl)] if local
               else ["docker", "run", "--rm", "-u",
                     f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                     "-v", f"{td}:{td}",
                     *[a for m in mounts for a in ("-v", f"{m}:{m}:ro")],
                     image, "bash", "-c",
                     "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/sta "
                     f"-no_init -no_splash -exit {tcl}"])
        out = subprocess.run(cmd, capture_output=True, text=True)
    vals = dict(re.findall(r"^(worst slack|tns) max (\S+)", out.stdout, re.M))
    if "worst slack" not in vals:
        sys.exit(f"vt_report: sta failed:\n{out.stdout}\n{out.stderr}")
    # OpenSTA reports in the library's time unit (ns here).
    return {"wns_ns": float(vals["worst slack"]), "tns_ns": float(vals["tns"])}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("netlist")
    ap.add_argument("top")
    ap.add_argument("--lib", action="append", required=True)
    ap.add_argument("--flavour-re", default=r"_([A-Z])$")
    ap.add_argument("--period-ps", type=float)
    ap.add_argument("--clock", default="clk_i")
    ap.add_argument("--sta", nargs="?", const=ORFS_IMAGE, default=None)
    ap.add_argument("--false-from", action="append", default=[],
                    help="a port whose paths are not timed (e.g. a reset "
                         "whose fan-out tree P&R builds); repeatable")
    ap.add_argument("--json")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    cells = lib_cells(a.lib)
    fre = re.compile(a.flavour_re)
    by = defaultdict(lambda: [0, 0.0, 0.0])
    for t in instances(a.netlist, cells, a.top):
        m = fre.search(t)
        f = m.group(1) if m else "?"
        area, leak = cells[t]
        by[f][0] += 1
        by[f][1] += area
        by[f][2] += leak
    tot = [sum(v[i] for v in by.values()) for i in range(3)]
    res = {"label": a.label, "cells": tot[0], "area": round(tot[1], 4),
           "leakage": round(tot[2], 4),
           "flavours": {f: {"cells": v[0], "area": round(v[1], 4),
                            "leakage": round(v[2], 4)}
                        for f, v in sorted(by.items())}}
    if a.sta:
        if a.period_ps is None:
            sys.exit("vt_report: --sta needs --period-ps")
        res.update(sta(a.netlist, a.top, a.lib, a.period_ps, a.clock, a.sta,
                       a.false_from))
    mix = " ".join(f"{f}={v['cells']}" for f, v in res["flavours"].items())
    line = (f"{a.label:<24} cells={res['cells']:<5} area={res['area']:<9.4f} "
            f"leak={res['leakage']:<10.3f} {mix}")
    if "wns_ns" in res:
        line += f"  wns={res['wns_ns']:.4f}ns tns={res['tns_ns']:.4f}ns"
    print(line)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2) + "\n")


if __name__ == "__main__":
    main()
