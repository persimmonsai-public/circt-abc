#!/usr/bin/env python3
"""The tech-pack P&R interface (doc/techpacks.md), as a stand-in.

circt_flow runs a pack's `pnr.command` from the pack directory with

    --netlist <out>/<top>.v --sdc <out>/constraint.sdc --top <top>
    --out <out> --pack <pack dir>

appended. The netlist is gate-level Verilog with the pack's tie cells on its
constants; the SDC constrains the design's clock. The command writes
<out>/pnr_metrics.json, any subset of the keys circt_flow.orfs_pnr.METRICS
names (instance_count, stdcell_area, setup_ws, ...), and exits 0. With
`--settings FILE` (the pack's own options, pnr/options.json; written by the
flow from a session's s.set_pack_option) it applies the `report.*` options
(the label and the note go to <out>/report.json, the histogram to
<out>/cell_histogram.json); the other options of the registry (common P&R
options of an OpenROAD-style command) are accepted and not used by this
stand-in, which has no layout.

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
ap.add_argument("--settings", type=Path, default=None)
a = ap.parse_args()

# The pack's option registry gives the defaults; --settings FILE overrides.
reg = json.loads((a.pack / "pnr" / "options.json").read_text())
opt = {d["name"]: d.get("default") for d in reg["options"]}
if a.settings is not None:
    given = json.loads(a.settings.read_text())["options"]
    unknown = sorted(set(given) - set(opt))
    if unknown:
        raise SystemExit(f"run.py: unknown option(s) in {a.settings}: {', '.join(unknown)}")
    opt.update(given)

lib = (a.pack / "lib" / "template.lib").read_text()
area = {m[0]: float(m[1]) for m in re.findall(
    r"cell\((\w+)\)\s*\{\s*area\s*:\s*([\d.]+)", lib)}
cells = re.findall(r"^\s*(\w+)\s+[\w\\$.\[\]]+\s*\(", a.netlist.read_text(),
                   re.M)
used = [c for c in cells if c in area]
metrics = {
    "instance_count": len(used),
    "stdcell_area": round(sum(area[c] for c in used), opt["report.area_digits"]),
}
report = {}
if opt["report.label"]:
    report["label"] = opt["report.label"]
if opt["report.note"]:
    notes = Path(opt["pnr.recipe_dir"] or a.pack / "pnr" / "notes")
    report["note"] = (notes / opt["report.note"]).read_text().splitlines()[0]
if report:
    (a.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
if opt["report.cell_histogram"]:
    hist = {}
    for c in used:
        hist[c] = hist.get(c, 0) + 1
    (a.out / "cell_histogram.json").write_text(json.dumps(dict(sorted(hist.items())), indent=1) + "\n")
(a.out / "pnr_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(f"{len(used)} cells")
