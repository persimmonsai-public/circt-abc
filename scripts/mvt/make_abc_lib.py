#!/usr/bin/env python3
"""Merge slimmed per-flavour liberties into ONE priced library for ABC.

Input: libraries written by slim_liberty.py (same header and lu_table
templates, each cell carrying `area` and `cell_leakage_power`).  Output: the
first library's header, then every cell of every input, with

    area := S * (area + W * cell_leakage_power)

S (default 100) only defeats ABC's %7.2f genlib area rounding; W
(um^2 per library leakage unit, e.g. uW) is the leakage price that makes
area recovery prefer the low-leakage flavour wherever a path has slack.
W = 0 leaves equal-area flavours tied, and the mapper's delay tie-break then
picks the fastest (leakiest) flavour everywhere.

Corners. By default the inputs give both the timing and the leakage that is
priced (one corner), exactly as before corners existed. With --leakage-lib
(repeatable), the inputs are the SETUP corner's libraries and give the
timing, while each cell's cell_leakage_power is taken, by cell name, from
the --leakage-lib libraries (the LEAKAGE corner's): the price is
S * (area + W * leakage-corner leakage), and the output's
cell_leakage_power lines carry that leakage. A cell missing from the
leakage libraries is an error. The output then starts with a comment
naming both sets of libraries.

Do-not-use cells are left out: a cell marked `dont_use : true` (unless
--keep-dont-use) and a cell matching a -X pattern (fnmatch, repeatable:
the tech pack's dont_use: list). This library is the PRIMARY liberty of a
multi-Vt rail, the only one circt-abc's register mapper, structural mapper
and ABC read, and those passes do not read the attribute themselves, so
leaving the cells out is what keeps them out of the netlist. --keep
(fnmatch, repeatable) keeps a dont_use cell the flow places on purpose:
foundries mark integrated clock gates dont_use so that synthesis does not
use them as logic, but clock-gate inference instantiates them as gates
(e.g. --keep '*ICG*').

This library is for the mapper ONLY: its areas are not areas.

The first line of the output is a machine-readable provenance comment
(its inputs' sha256 and sizes, leakage libraries, W, S, the patterns, the
tool revision, and a hash of the rest of the file), which
`python3 -m circt_flow.techpack check` verifies against the tech pack's
real per-flavour libraries (circt_flow/provenance.py).

Usage:
    make_abc_lib.py <out.lib> <in1.lib> [<in2.lib> ...] [-S 100] [-W 10]
                    [--leakage-lib <lk1.lib> ...] [-X <pattern> ...]
                    [--keep-dont-use] [--keep <pattern> ...]
"""
import argparse, re
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

# The first line of every library this tool writes (doc/techpacks.md,
# "Mapping-library provenance"): `/* <MARK> <json> */`.
MARK = "make_abc_lib provenance v1:"

_CELL = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)')
_DONT_USE = re.compile(r'\s*dont_use\s*:\s*"?true"?\s*;', re.I)
_LEAK = re.compile(r"(\s*)cell_leakage_power\s*:\s*([-\d.eE+]+)\s*;")


def sha256_file(path, _chunk=1 << 22):
    """(sha256 hex, size) of a file, streamed."""
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(_chunk), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def _tool_revision():
    me = Path(__file__).resolve()
    try:
        git = subprocess.run(["git", "-C", str(me.parent), "rev-parse",
                              "--short=12", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        git = None
    return {"git": git, "sha256": sha256_file(me)[0][:16]}


def _entry(path):
    sha, size = sha256_file(path)
    return {"name": Path(path).name, "sha256": sha, "size": size}


def cell_leakage(paths):
    """{cell: cell_leakage_power} over `paths` (first definition wins)."""
    out = {}
    for p in paths:
        name = None
        for line in Path(p).read_text().splitlines():
            m = _CELL.match(line)
            if m:
                name = m.group(1)
                continue
            m = _LEAK.match(line)
            if m and name is not None and name not in out:
                out[name] = float(m.group(2))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-S", "--area-scale", type=float, default=100.0)
    ap.add_argument("-W", "--leak-weight", type=float, default=10.0)
    ap.add_argument("-L", "--leakage-lib", action="append", default=[],
                    help="price leakage from these (the leakage corner's) "
                         "libraries instead of the inputs (repeatable)")
    ap.add_argument("-X", "--exclude", action="append", default=[],
                    help="leave out cells matching this fnmatch pattern "
                         "(repeatable)")
    ap.add_argument("-K", "--keep", action="append", default=[],
                    help="keep dont_use cells matching this fnmatch pattern "
                         "(repeatable)")
    ap.add_argument("--keep-dont-use", action="store_true",
                    help="keep cells marked dont_use : true")
    a = ap.parse_args(argv)
    dropped = {"dont_use": 0, "pattern": 0}
    leak_from = cell_leakage(a.leakage_lib) if a.leakage_lib else None
    final = a.out
    out = open(final + ".body", "w")
    if leak_from is not None:
        out.write("/* make_abc_lib.py: timing from "
                  + " ".join(Path(p).name for p in a.inputs)
                  + "; leakage priced from "
                  + " ".join(Path(p).name for p in a.leakage_lib)
                  + f"; area := {a.area_scale:g} * (area + {a.leak_weight:g}"
                  " * leakage). For the mapper only: areas are prices. */\n")
    # Named header groups (lu_table templates, sensitization, ...) of every
    # input: cells refer to them by name, and inputs of different cell
    # families may each define their own.
    seen, extra = set(), []
    for k, path in enumerate(a.inputs):
        lines = open(path).read().splitlines(keepends=True)
        first = next(i for i, l in enumerate(lines)
                     if re.match(r"\s*cell\s*\(", l))
        for key, grp in _header_groups(lines[:first]):
            if key not in seen:
                seen.add(key)
                if k > 0:
                    extra += grp
    for k, path in enumerate(a.inputs):
        lines = open(path).read().splitlines(keepends=True)
        first = next(i for i, l in enumerate(lines) if re.match(r"\s*cell\s*\(", l))
        last = max(i for i, l in enumerate(lines) if l.strip() == "}")  # library close
        if k == 0:
            hdr = lines[:first]
            hdr = [re.sub(r"library\s*\(([^)]*)\)", r"library(\1_mvt_priced)", l, count=1)
                   for l in hdr]
            out.writelines(hdr)
            out.writelines(extra)
        body = lines[first:last]
        cell = None
        # price each cell: area line precedes cell_leakage_power (slim_liberty)
        for i, l in enumerate(body):
            mc = _CELL.match(l)
            if mc:
                cell = mc.group(1)
            ma = re.match(r"(\s*)area\s*:\s*([-\d.eE+]+)\s*;", l)
            if ma:
                ml = re.match(r"\s*cell_leakage_power\s*:\s*([-\d.eE+]+)", body[i + 1])
                lk = float(ml.group(1)) if ml else 0.0
                if leak_from is not None:
                    if cell not in leak_from:
                        raise SystemExit(f"make_abc_lib: cell {cell} of {path} "
                                         "has no cell_leakage_power in the "
                                         "--leakage-lib libraries")
                    lk = leak_from[cell]
                    if ml:
                        ind = re.match(r"(\s*)", body[i + 1]).group(1)
                        body[i + 1] = f"{ind}cell_leakage_power : {lk:.6g} ;\n"
                ar = a.area_scale * (float(ma.group(2)) + a.leak_weight * lk)
                body[i] = f"{ma.group(1)}area : {ar:.6g} ;\n"
        out.writelines(_keep_cells(body, a.exclude, not a.keep_dont_use,
                                   dropped, a.keep))
    out.write("}\n")
    out.close()
    _finish(final, a, dropped)
    if any(dropped.values()):
        print(f"make_abc_lib: left out {dropped['dont_use']} dont_use "
              f"cell(s) and {dropped['pattern']} matching -X", file=sys.stderr)


def _finish(final, a, dropped):
    """Write `final`: the provenance line, then the body written so far.
    The line records the inputs (name, sha256, size), the leakage
    libraries, W, S, the -X / --keep patterns, the counts, the tool
    revision, and the sha256 of everything after it, so any later edit of
    the file is detectable. `*/` is escaped so the comment stays closed."""
    body = final + ".body"
    bsha, bsize = sha256_file(body)
    ncell = 0
    with open(body) as f:
        for l in f:
            ncell += bool(_CELL.match(l))
    prov = {"inputs": [_entry(p) for p in a.inputs],
            "leakage_libs": [_entry(p) for p in a.leakage_lib],
            "W": a.leak_weight, "S": a.area_scale,
            "exclude": a.exclude, "keep": a.keep,
            "keep_dont_use": a.keep_dont_use,
            "cells": ncell, "dropped": dropped,
            "tool": _tool_revision(),
            "body_sha256": bsha, "body_size": bsize}
    js = json.dumps(prov, sort_keys=True, separators=(",", ":"))
    js = js.replace("*/", "*\\/")
    with open(final, "w") as out, open(body) as f:
        out.write(f"/* {MARK} {js} */\n")
        shutil.copyfileobj(f, out)
    Path(body).unlink()


def _header_groups(hdr):
    """[((kind, name), lines)] of the library-level named groups of a
    header (`kind(name) { ... }` at brace depth 1)."""
    out, depth, cur, key = [], 0, None, None
    for l in hdr:
        if cur is None and depth == 1:
            m = re.match(r"\s*(\w+)\s*\(\s*\"?([^)\"]*)\"?\s*\)\s*\{", l)
            if m and m.group(1) != "library":
                cur, key = [], (m.group(1), m.group(2).strip())
        if cur is not None:
            cur.append(l)
        depth += l.count("{") - l.count("}")
        if cur is not None and depth == 1:
            out.append((key, cur))
            cur = None
    return out


def _keep_cells(body, patterns, drop_dont_use, dropped, keep=()):
    """`body` (the cells' lines) without the cells to leave out."""
    starts = [i for i, l in enumerate(body) if _CELL.match(l)]
    if not starts:
        return body
    out = body[:starts[0]]
    for k, i in enumerate(starts):
        chunk = body[i:starts[k + 1] if k + 1 < len(starts) else len(body)]
        name = _CELL.match(body[i]).group(1)
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            dropped["pattern"] += 1
            continue
        if drop_dont_use and not any(fnmatch.fnmatchcase(name, p)
                                     for p in keep) \
                and any(_DONT_USE.match(l) for l in chunk):
            dropped["dont_use"] += 1
            continue
        out += chunk
    return out


if __name__ == "__main__":
    main()
