#!/usr/bin/env python3
"""The tech-pack P&R interface (doc/techpacks.md), as a stand-in.

circt_flow runs a pack's `pnr.command` from the pack directory with

    --netlist <out>/<top>.v --sdc <out>/constraint.sdc --top <top>
    --out <out> --pack <pack dir>

appended. The netlist is gate-level Verilog with the pack's tie cells on its
constants; the SDC constrains the design's clock. The command writes
<out>/pnr_metrics.json, any subset of the keys circt_flow.orfs_pnr.METRICS
names (instance_count, stdcell_area, setup_ws, ...), and exits 0.

A real pack runs its place-and-route tool here. The template's library has
no layout, so this only counts the netlist's cells and sums their liberty
areas.
"""
import argparse
import json
import re
from pathlib import Path

ap = argparse.ArgumentParser()
for opt in ("--netlist", "--sdc", "--out", "--pack"):
    ap.add_argument(opt, type=Path, required=True)
ap.add_argument("--top", required=True)
a = ap.parse_args()

lib = (a.pack / "lib" / "template.lib").read_text()
area = {m[0]: float(m[1]) for m in re.findall(
    r"cell\((\w+)\)\s*\{\s*area\s*:\s*([\d.]+)", lib)}
cells = re.findall(r"^\s*(\w+)\s+[\w\\$.\[\]]+\s*\(", a.netlist.read_text(),
                   re.M)
used = [c for c in cells if c in area]
(a.out / "pnr_metrics.json").write_text(json.dumps({
    "instance_count": len(used),
    "stdcell_area": round(sum(area[c] for c in used), 4),
}, indent=2) + "\n")
print(f"{len(used)} cells")
