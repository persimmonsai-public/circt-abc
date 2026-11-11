#!/usr/bin/env python3
"""Characterise a vt_swap flavour ladder from the liberties.

For the flavour tokens of a pack's `vt_flavours:` (slowest first), find the
cell families the libraries carry in every flavour (names equal but for the
regex's groups) and print, per flavour and per class (combinational,
sequential), the median over those families, relative to the first token:

    delay    mean cell_rise/cell_fall over all arcs, at --slew and a load of
             --load-x times the cell's mean input pin capacitance (taken
             from the first token's cell: one absolute load per family)
    leakage  cell_leakage_power (the slim libraries' state mean)
    area, input capacitance

and, between neighbouring tokens, the delay gained per leakage added, so a
ladder that is not in speed order or whose steps are not worth their
leakage shows. Libraries are the slim ones (scripts/mvt/slim_liberty.py).

    ladder_char.py --tokens R/A,R/B,L/B --re '_([RLS])_([AB])$' lib...
    (example arguments; --slew and --load-x take their own, e.g. 0.020, 4.0)
"""
import argparse
import bisect
import re
import statistics as st
import sys
from pathlib import Path


def _nums(s):
    return [float(x) for x in re.findall(r"[-\d.eE+]+", s)]


def parse(path):
    """{cell: {area, leak, seq, cin: [..], tabs: [(idx1, idx2, rows)]}}"""
    txt = Path(path).read_text()
    cells = {}
    parts = re.split(r'\n\s*cell\s*\(\s*"?(\w+)"?\s*\)\s*\{', txt)
    for name, body in zip(parts[1::2], parts[2::2]):
        c = {"area": float(re.search(r"\barea\s*:\s*([-\d.eE+]+)", body).group(1)),
             "leak": float(re.search(r"cell_leakage_power\s*:\s*([-\d.eE+]+)",
                                     body).group(1)),
             "seq": bool(re.search(r"\b(ff|latch|statetable)\s*\(", body)),
             "cin": [], "tabs": []}
        for m in re.finditer(r'pin\s*\(\s*"?(\w+)"?\s*\)\s*\{(.*?)(?=\n    pin\s*\(|\Z)',
                             body, re.S):
            pb = m.group(2)
            if re.search(r'direction\s*:\s*"?input', pb):
                cm = re.search(r"\n\s*capacitance\s*:\s*([-\d.eE+]+)", pb)
                if cm:
                    c["cin"].append(float(cm.group(1)))
            for tm in re.finditer(
                    r'(cell_rise|cell_fall)\s*\(\s*\w+\s*\)\s*\{\s*index_1\("([^"]*)"\);'
                    r'\s*index_2\("([^"]*)"\);\s*values\((.*?)\);', pb, re.S):
                rows = [_nums(r) for r in re.findall(r'"([^"]*)"', tm.group(4))]
                c["tabs"].append((_nums(tm.group(2)), _nums(tm.group(3)), rows))
        cells[name] = c
    return cells


def _seg(ix, v):
    if v <= ix[0]:
        return 0, 0.0
    if v >= ix[-1]:
        return len(ix) - 2, 1.0
    i = bisect.bisect(ix, v) - 1
    return i, (v - ix[i]) / (ix[i + 1] - ix[i])


def delay(c, slew, load):
    """Mean over arcs of the bilinear table value at (slew, load)."""
    ds = []
    for i1, i2, t in c["tabs"]:
        a, fa = _seg(i1, slew)
        b, fb = _seg(i2, load)
        ds.append((1 - fa) * ((1 - fb) * t[a][b] + fb * t[a][b + 1])
                  + fa * ((1 - fb) * t[a + 1][b] + fb * t[a + 1][b + 1]))
    return sum(ds) / len(ds) if ds else None


def families(libs, fre, tokens):
    """{family key: {token: cell dict}} over the libraries' cells."""
    fam = {}
    for lib in libs:
        for name, c in parse(lib).items():
            m = fre.search(name)
            if not m:
                continue
            tok = "/".join(g or "" for g in m.groups())
            if tok not in tokens:
                continue
            key, last = "", 0
            for i in range(1, fre.groups + 1):
                key += name[last:m.start(i)] + "\0"
                last = m.end(i)
            fam.setdefault(key + name[last:], {})[tok] = c
    return fam


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("libs", nargs="+")
    ap.add_argument("--tokens", required=True,
                    help="ladder tokens, slowest first, comma-separated")
    ap.add_argument("--re", required=True, help="the pack's flavour regex")
    ap.add_argument("--slew", type=float, default=0.020,
                    help="input slew, in the libraries' time unit")
    ap.add_argument("--load-x", type=float, default=4.0)
    a = ap.parse_args(argv)
    toks = [t.strip() for t in a.tokens.split(",") if t.strip()]
    fre = re.compile(a.re)
    fam = {k: v for k, v in families(a.libs, fre, toks).items()
           if all(t in v for t in toks)}
    if not fam:
        sys.exit("ladder_char: no cell family has every token")
    print(f"{len(fam)} cell families carry all {len(toks)} flavours")
    for cls, want in (("comb", False), ("seq", True)):
        rel = {t: {"delay": [], "leak": [], "area": [], "cin": []} for t in toks}
        for v in fam.values():
            c0 = v[toks[0]]
            if c0["seq"] != want or not c0["cin"] or not c0["tabs"]:
                continue
            load = a.load_x * sum(c0["cin"]) / len(c0["cin"])
            d0 = delay(c0, a.slew, load)
            for t in toks:
                c = v[t]
                rel[t]["delay"].append(delay(c, a.slew, load) / d0)
                rel[t]["leak"].append(c["leak"] / c0["leak"] if c0["leak"] else 1.0)
                rel[t]["area"].append(c["area"] / c0["area"])
                rel[t]["cin"].append(sum(c["cin"]) / sum(c0["cin"]))
        n = len(rel[toks[0]]["delay"])
        if not n:
            print(f"\n{cls}: no families with timing arcs")
            continue
        print(f"\n{cls}: n={n}, slew {a.slew}, load {a.load_x} x cin "
              f"(median ratio to {toks[0]})")
        print(f"  {'token':10s} {'delay':>7s} {'leakage':>8s} {'area':>6s} "
              f"{'cin':>6s}  step: delay gain per added leakage")
        prev = None
        for t in toks:
            m = {k: st.median(v) for k, v in rel[t].items() if v}
            step = ""
            if prev:
                dl, dk = prev["delay"] - m["delay"], m["leak"] - prev["leak"]
                step = (f"{100 * dl:5.1f}% delay for {dk:+.2f}x leakage"
                        + ("  <-- not faster" if dl <= 0 else "")
                        + ("  <-- not leakier" if dk <= 0 else ""))
            print(f"  {t:10s} {m['delay']:7.3f} {m['leak']:8.2f} {m['area']:6.3f} "
                  f"{m['cin']:6.3f}  {step}")
            prev = m


if __name__ == "__main__":
    main()
