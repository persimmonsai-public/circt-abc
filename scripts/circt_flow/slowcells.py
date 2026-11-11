"""Slow-cell filter for the ABC mapping library.

ABC maps with a load-aware delay model that is optimistic for cells whose
delay is dominated by a tall transistor stack (4-input NAND/NOR, wide AOI,
...): on a placement-free STA those cells are much slower than a
NAND2, so a netlist with few levels of such cells is slower than one with
many levels of 2-input cells and area recovery picks them anyway.  This
module lists the combinational single-output cells whose delay at one
reference slew and load exceeds `ratio` times the median of all of them,
so the flow can hand them to ABC's read_lib as `-X <cell>`.

Nothing here knows a cell name: delays come from the liberty's NLDM
tables; the optional `family_re` (the pack's vt_flavours regex) makes a
cell and its threshold/second-axis twins one family, judged by the
slowest member, so a fast flavour of a slow cell is not kept.

Pack: flow.slow_cell_filter: {ratio: 1.3, slew_ps: 30, load_ff: 2.5}
(opt-in: absent means off), or CIRCT_FLOW_SLOW_CELL_RATIO=<ratio>.
"""
from __future__ import annotations

import bisect
import re
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional

KEYS = {"ratio", "slew_ps", "load_ff"}


def parse_spec(d, where) -> Optional[dict]:
    if d is None or d is False:
        return None
    if not isinstance(d, dict) or set(d) - KEYS or "ratio" not in d:
        raise KeyError(f"{where}: slow_cell_filter takes ratio (required), "
                       "slew_ps, load_ff")
    for k, v in d.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise KeyError(f"{where}: slow_cell_filter.{k} must be a "
                           "positive number")
    return {"ratio": float(d["ratio"]), "slew_ps": float(d.get("slew_ps", 30)),
            "load_ff": float(d.get("load_ff", 2.5))}


def _unit(text, key, default):
    m = re.search(key + r'\s*:\s*"?([\d.]+)\s*([a-z]*)"?', text)
    return m


def _scales(text):
    """(seconds per time unit, farads per cap unit) of a liberty."""
    t = {"ns": 1e-9, "ps": 1e-12, "us": 1e-6}
    m = re.search(r'time_unit\s*:\s*"?(\d*)\s*([a-z]+)"?', text)
    ts = (float(m.group(1) or 1) * t.get(m.group(2), 1e-9)) if m else 1e-9
    cs = 1e-12
    m = re.search(r'capacitive_load_unit\s*\(\s*([\d.]+)\s*,\s*(\w+)\s*\)', text)
    if m:
        cs = float(m.group(1)) * {"pf": 1e-12, "ff": 1e-15}.get(
            m.group(2).lower(), 1e-12)
    return ts, cs


def _interp(i1, i2, v, x, y):
    def pos(ix, z):
        k = max(1, min(len(ix) - 1, bisect.bisect_left(ix, z)))
        return k - 1, k, (z - ix[k - 1]) / (ix[k] - ix[k - 1])
    a, b, fa = pos(i1, x)
    c, d, fc = pos(i2, y)
    return (v[a][c] * (1 - fa) * (1 - fc) + v[b][c] * fa * (1 - fc)
            + v[a][d] * (1 - fa) * fc + v[b][d] * fa * fc)


_TBL = re.compile(r'cell_(?:rise|fall)\s*\(\s*\w+\s*\)\s*\{\s*'
                  r'index_1\s*\(\s*"([^"]+)"\s*\)\s*;\s*'
                  r'index_2\s*\(\s*"([^"]+)"\s*\)\s*;\s*'
                  r'values\s*\(([^;]+)\)\s*;')


def cell_delays(text: str, slew_ps: float, load_ff: float) -> Dict[str, float]:
    """cell -> worst NLDM delay (ps) over its arcs at the reference point,
    for single-output cells with no state."""
    ts, cs = _scales(text)
    slew = slew_ps * 1e-12 / ts
    load = load_ff * 1e-15 / cs
    out: Dict[str, float] = {}
    starts = list(re.finditer(r'\n\s*cell\s*\(\s*"?([\w$]+)"?\s*\)\s*\{',
                              text))
    for i, m in enumerate(starts):
        name = m.group(1)
        body = text[m.end(): starts[i + 1].start() if i + 1 < len(starts)
                    else len(text)]
        if re.search(r'\b(ff|latch|statetable)\s*\(', body):
            continue
        if len(re.findall(r'direction\s*:\s*"?output', body)) != 1:
            continue
        ds = []
        for t in _TBL.finditer(body):
            try:
                i1 = [float(x) for x in t.group(1).split(",")]
                i2 = [float(x) for x in t.group(2).split(",")]
                v = [[float(x) for x in r.split(",")]
                     for r in re.findall(r'"([^"]+)"', t.group(3))]
                if len(i1) < 2 or len(i2) < 2:
                    continue
                ds.append(_interp(i1, i2, v, slew, load) * ts * 1e12)
            except (ValueError, IndexError):
                continue
        if ds:
            out[name] = max(ds)
    return out


def slow_cells(libs: Iterable[Path], spec: dict,
               family_re: Optional[str] = None) -> List[str]:
    delays: Dict[str, float] = {}
    for p in libs:
        delays.update(cell_delays(Path(p).read_text(), spec["slew_ps"],
                                  spec["load_ff"]))
    return _filter(delays, spec, family_re)


def _filter(delays: Dict[str, float], spec: dict,
            family_re: Optional[str]) -> List[str]:
    if not delays:
        return []

    def fam(n):
        if not family_re:
            return n
        m = re.search(family_re, n)
        if not m:
            return n
        s = n
        for g in range(m.lastindex or 0, 0, -1):
            s = s[:m.start(g)] + "*" + s[m.end(g):]
        return s
    fd: Dict[str, float] = {}
    for n, d in delays.items():
        k = fam(n)
        fd[k] = max(fd.get(k, 0.0), d)
    cut = spec["ratio"] * statistics.median(fd.values())
    return sorted(n for n in delays if fd[fam(n)] > cut)
