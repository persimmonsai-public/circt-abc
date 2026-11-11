#!/usr/bin/env python3
"""Cells, real area and logic depth of a mapped, flat netlist.

    report.py <netlist.v> --lib <liberty> [--label NAME] [--json OUT]

Area is the sum of the liberty's `area` over the instances. Depth is the
longest chain of combinational cells between a flip-flop output or primary
input and a flip-flop input or primary output (a flip-flop is any cell with
a `ff` group in the liberty). Uses only the standard library.
"""
import argparse
import json
import re
import sys
from pathlib import Path


def read_lib(path):
    """{cell: {area, seq, outs}} from a liberty file."""
    cells, cur, pin = {}, None, None
    for line in Path(path).read_text().splitlines():
        m = re.match(r'\s*cell\s*\(\s*"?(\w+)"?\s*\)', line)
        if m:
            cur = cells.setdefault(m.group(1), {"area": 0.0, "seq": False, "outs": set()})
            continue
        if cur is None:
            continue
        m = re.match(r"\s*area\s*:\s*([-\d.eE+]+)", line)
        if m:
            cur["area"] = float(m.group(1))
        if re.match(r"\s*(ff|latch)\s*\(", line):
            cur["seq"] = True
        m = re.match(r'\s*pin\s*\(\s*"?(\w+)"?\s*\)', line)
        if m:
            pin = m.group(1)
        if re.match(r"\s*direction\s*:\s*output", line) and pin:
            cur["outs"].add(pin)
    return cells


def parse(text, cells):
    insts = []
    for m in re.finditer(r"^\s*(\w+)\s+(\S+)\s*\((.*?)\)\s*;", text, re.S | re.M):
        if m.group(1) in cells:
            pins = dict(re.findall(r"\.(\w+)\s*\(\s*([^)]*?)\s*\)", m.group(3)))
            insts.append((m.group(1), m.group(2), pins))
    return insts


def depth(insts, cells):
    driver = {}  # net -> index of the combinational cell driving it
    for i, (c, _, pins) in enumerate(insts):
        if cells[c]["seq"]:
            continue
        for p, n in pins.items():
            if p in cells[c]["outs"]:
                driver[n] = i
    memo = {}

    def d(i):
        if i in memo:
            return memo[i]
        memo[i] = 0  # guards a loop
        c, _, pins = insts[i]
        best = 0
        for p, n in pins.items():
            if p not in cells[c]["outs"] and n in driver:
                best = max(best, d(driver[n]))
        memo[i] = best + 1
        return memo[i]
    sys.setrecursionlimit(100000)
    return max([d(i) for i in range(len(insts)) if not cells[insts[i][0]]["seq"]], default=0)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist", type=Path)
    ap.add_argument("--lib", type=Path, required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    cells = read_lib(a.lib)
    insts = parse(a.netlist.read_text(), cells)
    comb = [i for i in insts if not cells[i[0]]["seq"]]
    res = {"label": a.label, "cells": len(insts), "comb_cells": len(comb),
           "flops": len(insts) - len(comb),
           "area": round(sum(cells[c]["area"] for c, _, _ in insts), 4),
           "comb_area": round(sum(cells[c]["area"] for c, _, _ in comb), 4),
           "depth": depth(insts, cells)}
    print(f"{a.label or a.netlist.name}: {res['cells']} cells ({res['comb_cells']} combinational), "
          f"area {res['area']:.3f} um2 (combinational {res['comb_area']:.3f}), depth {res['depth']} cells")
    if a.json:
        a.json.write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
