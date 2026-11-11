#!/usr/bin/env python3
"""Fit a flow.wire_model (circt_flow/wire_model.py) from a routed design's
extracted wire capacitance.

Input: a TSV with a header and the columns `fanout` (load pins on the net)
and `wirecap_pf` (the net's extracted wire capacitance, pin capacitance
excluded); other columns (net name, hpwl_um, ...) are ignored, and any
number of files may be given and are pooled. OpenROAD produces it with, per
signal net after `estimate_parasitics -global_routing` (or a SPEF read):

    foreach net [$block getNets] { ... report_net -digits 5 $name ... }
    # "Wire capacitance:" line, and the load count from `Number of loads`

Output: the least-squares line `cap_base_ff + cap_per_fanout_ff * fanout`
over all nets, the same through the origin, the mean wire capacitance by
fanout bucket (a model is only as good as that table is straight), and a
`wire_model:` snippet for the pack's `flow:` section.

Usage: fit_wire_model.py nets.tsv [more.tsv ...]
"""
import argparse
import csv
import sys

BUCKETS = ((1, 1), (2, 2), (3, 3), (4, 4), (5, 6), (7, 8), (9, 16),
           (17, 10 ** 9))


def read(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                try:
                    fo = int(r["fanout"])
                    c = float(r["wirecap_pf"]) * 1000.0  # fF
                except (KeyError, ValueError):
                    continue
                if fo >= 1:
                    rows.append((fo, c))
    return rows


def fit(rows):
    """(base, per_fanout, r2) of the least-squares line, and the slope
    through the origin."""
    n = len(rows)
    sx = sum(f for f, _ in rows)
    sy = sum(c for _, c in rows)
    sxx = sum(f * f for f, _ in rows)
    sxy = sum(f * c for f, c in rows)
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den else 0.0
    base = (sy - slope * sx) / n
    mean = sy / n
    ss_tot = sum((c - mean) ** 2 for _, c in rows)
    ss_res = sum((c - base - slope * f) ** 2 for f, c in rows)
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    origin = sxy / sxx if sxx else 0.0
    return base, slope, r2, origin


def table(rows):
    out = []
    for lo, hi in BUCKETS:
        b = [c for f, c in rows if lo <= f <= hi]
        if b:
            out.append((lo, hi, len(b), sum(b) / len(b)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tsv", nargs="+")
    a = ap.parse_args(argv)
    rows = read(a.tsv)
    if len(rows) < 2:
        sys.exit("fit_wire_model: fewer than two usable rows")
    base, per, r2, origin = fit(rows)
    print(f"{len(rows)} nets, total wire capacitance "
          f"{sum(c for _, c in rows) / 1000:.3f} pF")
    print(f"fit: wirecap = {base:.3f} + {per:.3f} * fanout fF (R2 {r2:.3f}); "
          f"through the origin: {origin:.3f} * fanout")
    print("fanout   nets   mean wirecap (fF)   model (fF)")
    for lo, hi, n, mean in table(rows):
        mid = (lo + min(hi, lo + 10)) / 2 if lo != hi else lo
        print(f"{lo:>3}-{hi if hi < 10 ** 9 else 'up':<4}{n:6d}   "
              f"{mean:12.3f}   {base + per * mid:12.3f}")
    print("\nflow:\n  wire_model:")
    print(f"    cap_base_ff: {max(base, 0.0):.3f}")
    print(f"    cap_per_fanout_ff: {per:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
