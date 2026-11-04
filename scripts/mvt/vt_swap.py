#!/usr/bin/env python3
"""Threshold-voltage swap on a mapped netlist, driven by OpenSTA: the
command line of circt_flow.vtswap (its docstring describes the method).

    vt_swap.py <netlist.v> <top> <out.v> --lib <lib> [--lib ...]
               --flavours R,L,S --flavour-re '_([RLS])$'
               (--sdc <file> | --period-ps N [--clock clk_i])
               [--false-from <port> ...]
               [--paths 200] [--max-iter 30] [--no-recover]
               [--margin-ps 0] [--json <record>] [--image IMAGE]
(--margin-ps assumes libraries in ns.)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from circt_flow import vtswap  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("netlist")
    ap.add_argument("top")
    ap.add_argument("out")
    ap.add_argument("--lib", action="append", required=True)
    ap.add_argument("--flavours", required=True,
                    help="flavour tokens, slowest first, comma-separated")
    ap.add_argument("--flavour-re", default=r"_([A-Z]+)$")
    ap.add_argument("--sdc")
    ap.add_argument("--period-ps", type=float)
    ap.add_argument("--clock", default="clk_i")
    ap.add_argument("--false-from", action="append", default=[])
    ap.add_argument("--paths", type=int, default=200)
    ap.add_argument("--max-iter", type=int, default=30)
    ap.add_argument("--no-recover", action="store_true")
    ap.add_argument("--margin-ps", type=float, default=0.0,
                    help="setup slack to keep in hand (ps)")
    ap.add_argument("--json")
    ap.add_argument("--image", default=vtswap.ORFS_IMAGE)
    a = ap.parse_args(argv)
    if not a.sdc and a.period_ps is None:
        ap.error("--sdc or --period-ps is required")
    sdc = (Path(a.sdc).read_text() + "".join(
        f"set_false_path -from [get_ports {p}]\n" for p in a.false_from)
        if a.sdc else vtswap.sdc_for(a.period_ps, a.clock, a.false_from))
    try:
        res = vtswap.swap(Path(a.netlist), a.top, Path(a.out),
                          [Path(l) for l in a.lib],
                          [f.strip() for f in a.flavours.split(",")
                           if f.strip()],
                          a.flavour_re, sdc, a.paths, a.max_iter,
                          not a.no_recover, a.image,
                          margin=a.margin_ps / 1000.0)
    except vtswap.VtSwapError as e:
        sys.exit(str(e))
    print(f"vt_swap: {vtswap.summary(res)} -> {Path(a.out).resolve()}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2) + "\n")


if __name__ == "__main__":
    main()
