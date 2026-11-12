"""A net-delay health check for SDF files.

    python3 -m circt_flow.sdf_tools health FILE.sdf [--spef FILE.spef] [--json OUT.json]

The SDF standard (IEEE 1497) gives a net's delay either as an
`INTERCONNECT driver load (rise) (fall)` entry in the top-level CELL (what
OpenSTA's `write_sdf` writes) or as a `PORT pin (rise) (fall)` entry in the
load cell's own DELAY block. A file written without the design's parasitics
has few or none of them, and its nets then have no wire delay.

`health` counts the net-delay entries (INTERCONNECT plus PORT) and fails when
there are far fewer than the design has cells: every driven input pin of
every cell is a load of some net, so a file with a handful of entries (or
none) was written without parasitics or had them stripped. With `--spef` the
count is also compared with the SPEF's net count. A file whose entries are
nearly all zero is a warning.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Health thresholds, deliberately loose: they separate a file written with
# parasitics from one without, not good from slightly short ones. Every
# driven cell input is the load of some net, so a written file has about one
# net-delay entry per cell input (more than one per CELL block); half of
# that per CELL block, or four fifths of the SPEF's nets, is far below a complete
# file and far above an empty one.
MIN_ENTRIES_PER_CELL = 0.5     # net-delay entries per CELL block, below this fails
MIN_ENTRIES_PER_SPEF_NET = 0.8  # entries per SPEF *D_NET, below this fails
# A file whose entries are (nearly) all zero was written without wire delay.
ZERO_FRACTION_WARN = 0.95

_PORT = re.compile(r"\(PORT\s")
_CELL = re.compile(r"\(CELL\b")


class SdfError(ValueError):
    pass


def _read(path) -> str:
    p = str(path)
    if p.endswith(".gz"):
        with gzip.open(p, "rt", errors="replace") as f:
            return f.read()
    with open(p, errors="replace") as f:
        return f.read()


def health(text: str, spef_nets: Optional[int] = None) -> dict:
    """Net-delay entry counts and verdict (see module docstring)."""
    ic = text.count("(INTERCONNECT ")
    port = len(_PORT.findall(text))
    ncell = len(_CELL.findall(text))
    cells = max(ncell - 1, 0)  # the first CELL is the top
    if ncell == 0:
        raise SdfError("no CELL in the file: not an SDF")
    entries = ic + port
    res = {"cells": cells, "interconnect": ic, "port_delays": port,
           "net_delay_entries": entries, "spef_nets": spef_nets,
           "failures": [], "warnings": []}
    if entries < MIN_ENTRIES_PER_CELL * cells:
        res["failures"].append(
            f"only {entries} net-delay entries (INTERCONNECT {ic}, PORT {port}) "
            f"for {cells} cells: wire delay is missing (need >= "
            f"{MIN_ENTRIES_PER_CELL:g} per cell)")
    if spef_nets is not None and entries < MIN_ENTRIES_PER_SPEF_NET * spef_nets:
        res["failures"].append(
            f"{entries} net-delay entries for {spef_nets} SPEF nets (need >= "
            f"{MIN_ENTRIES_PER_SPEF_NET:g} per net)")
    if entries and cells:
        zero = 0
        for m in re.finditer(r"\((?:INTERCONNECT\s+\S+\s+\S+|PORT\s+\S+)\s+\(([^()]*)\)", text):
            v = m.group(1).split(":")
            vals = [x for x in v if x]
            if all(float(x) == 0.0 for x in vals) if vals else True:
                zero += 1
        res["zero_entries"] = zero
        if zero > ZERO_FRACTION_WARN * entries:
            res["warnings"].append(f"{zero} of {entries} net-delay entries are zero")
    res["ok"] = not res["failures"]
    return res


def spef_net_count(path) -> int:
    n = 0
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("*D_NET"):
                n += 1
    return n


def check_exports(root) -> List[Tuple[Path, dict]]:
    """health() of every `*.sdf` / `*.sdf.gz` file below `root` (the P&R
    output directory). Returns [(path, result)] for the files that fail or
    warn (net-delay entries missing, or nearly all zero); a file that cannot
    be read or parsed is reported ("could not be read: ..."), never raised."""
    bad = []
    for p in sorted(Path(root).rglob("*.sdf*")):
        if not p.name.endswith((".sdf", ".sdf.gz")) or not p.is_file():
            continue
        try:
            r = health(_read(p))
        except Exception as e:      # noqa: BLE001 (a report, never a failure)
            r = {"ok": False, "warnings": [],
                 "failures": [f"could not be read: {type(e).__name__}: {e}"]}
        if not r["ok"] or r.get("warnings"):
            bad.append((p, r))
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health")
    h.add_argument("sdf", type=Path)
    h.add_argument("--spef", type=Path, default=None)
    h.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    r = health(_read(a.sdf), spef_net_count(a.spef) if a.spef else None)
    for w in r["warnings"]:
        print("WARN", w)
    for f in r["failures"]:
        print("FAIL", f)
    print("sdf_health: %s cells=%d interconnect=%d port=%d" % (
        "ok" if r["ok"] else "FAILED", r["cells"], r["interconnect"], r["port_delays"]))
    if a.json:
        a.json.write_text(json.dumps(r, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
