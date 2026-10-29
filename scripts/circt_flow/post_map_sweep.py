#!/usr/bin/env python3
"""Bit-level dead-logic sweep of a MAPPED netlist (post-ABC), design-wide.

    post_map_sweep.py <netlist.v> <top> <out.v> --liberty <lib> [--liberty ...]
                      [--stubs <blackbox.v> ...] [--yosys <bin>] [--hier-names]

WHY. Every register-removal pass in the flow runs on the IR before ABC maps
it, and the hierarchical WS1 flow additionally synthesises units one at a
time. Logic whose outputs only become unused after mapping, or whose only
reader sits in another unit, therefore survives into the netlist: e.g. a
crossbar port's spill register whose bits feed only each other and gates
with no load. On a large multi-unit design that was about a quarter of all
flops.

WHAT. Yosys: read the netlist with the liberty cells and macro stubs as
blackboxes, flatten, mark every instance whose type is NOT a liberty cell
(macros, pads, black boxes) `keep`, then `opt_clean -purge`: a cell survives
only if some output bit reaches a top-level port or a kept instance.
Removing logic that reaches nothing observable cannot change the design's
port behaviour.

--hier-names rewrites CIRCT's flattened-name escape (`2F` for `/`) back to
'/' in instance names, so a flat netlist carries the same hierarchical
instance paths a hierarchical one would after OpenROAD's read_verilog --
which is what SDC exceptions and macro placement scripts name.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEF_YOSYS = os.path.join(ROOT, "third_party", "yosys", "build", "yosys")
CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)')
INST_RE = re.compile(r"^\s+([A-Za-z_][\w$]*)\s+(\\\S+|[A-Za-z_][\w$]*)\s*\(")


def liberty_cells(libs):
    names = set()
    for lib in libs:
        with open(lib, errors="replace") as f:
            for line in f:
                m = CELL_RE.match(line)
                if m:
                    names.add(m.group(1))
    return names


def instantiated_types(netlist):
    types = {}
    with open(netlist, errors="replace") as f:
        for line in f:
            m = INST_RE.match(line)
            if m and m.group(1) not in ("module", "input", "output", "inout", "wire",
                                        "reg", "assign", "logic"):
                types[m.group(1)] = types.get(m.group(1), 0) + 1
    return types


def count_cells(netlist, lib_cells):
    t = instantiated_types(netlist)
    std = sum(c for k, c in t.items() if k in lib_cells)
    flops = sum(c for k, c in t.items() if k in lib_cells and re.match(r"S?DFF|SDFF", k))
    return std, flops, {k: c for k, c in t.items() if k not in lib_cells}


HIER_TOKEN = re.compile(r"(?<![\w$\\])([A-Za-z_][\w$]*2F[\w$]*)")


def hier_names(src, dst):
    """Instance names only: CIRCT's `2F` escape and Yosys's flatten separator
    `.` both become '/': `<type> <name> (` -> `<type> \\a/b/c (`."""
    n = 0
    with open(src) as fi, open(dst, "w") as fo:
        for line in fi:
            m = INST_RE.match(line)
            if m:
                name = m.group(2)
                bare = name[1:] if name.startswith("\\") else name
                new = bare.replace("2F", "/").replace(".", "/")
                if new != bare:
                    line = line[:m.start(2)] + "\\" + new + " " + line[m.end(2):].lstrip(" ")
                    n += 1
            fo.write(line)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("top")
    ap.add_argument("out")
    ap.add_argument("--liberty", action="append", required=True)
    ap.add_argument("--stubs", action="append", default=[])
    ap.add_argument("--yosys", default=DEF_YOSYS)
    ap.add_argument("--hier-names", action="store_true")
    a = ap.parse_args()

    lib_cells = liberty_cells(a.liberty)
    before_std, before_ff, keep_types = count_cells(a.netlist, lib_cells)
    # A hierarchical (stitched) netlist instantiates its own modules (the
    # units); flatten dissolves them. Only black boxes are kept and checked.
    with open(a.netlist) as f:
        defined = set(re.findall(r"^\s*module\s+\\?([A-Za-z_][\w$]*)", f.read(), re.M))
    keep_types = {t: c for t, c in keep_types.items() if t not in defined}
    work = tempfile.mkdtemp(prefix="post_map_sweep.", dir=os.path.dirname(os.path.abspath(a.out)))
    swept = os.path.join(work, "swept.v")
    ys = os.path.join(work, "sweep.ys")
    lines = [f"read_liberty -lib -ignore_miss_dir {l}" for l in a.liberty]
    lines.append(f"read_slang --top {a.top} " + " ".join(a.stubs + [a.netlist]))
    lines += [f"hierarchy -top {a.top}", "flatten"]
    if keep_types:
        lines.append("setattr -set keep 1 " + " ".join(f"t:{t}" for t in sorted(keep_types)))
    lines += ["opt_clean -purge", f"write_verilog -noattr -noexpr {swept}"]
    with open(ys, "w") as f:
        f.write("\n".join(lines) + "\n")
    log = os.path.join(work, "sweep.log")
    r = subprocess.run([a.yosys, "-q", "-l", log, "-s", ys])
    if r.returncode:
        sys.exit(f"post_map_sweep: yosys failed (rc {r.returncode}), see {log}")
    renamed = hier_names(swept, a.out) if a.hier_names else (os.replace(swept, a.out) or 0)
    after_std, after_ff, after_keep = count_cells(a.out, lib_cells)
    lost = {t: c for t, c in keep_types.items() if after_keep.get(t, 0) != c}
    print(f"post_map_sweep: std cells {before_std} -> {after_std}, flops {before_ff} -> {after_ff}; "
          f"{sum(keep_types.values())} non-liberty instances kept"
          + (f"; {renamed} instance names re-hierarchised" if a.hier_names else ""))
    if lost:
        sys.exit(f"post_map_sweep: non-liberty instances changed: {lost}")


if __name__ == "__main__":
    main()
