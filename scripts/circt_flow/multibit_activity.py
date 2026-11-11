"""Switching activity per register from a TCF, for multibit family choice.

    python -m circt_flow.multibit_activity <act.tcf> [--period-ns 1.0] [-o act.json]
    python -m circt_flow.multibit_activity --netlist net.v <act.tcf|nets.tsv> \\
        [--cycles N] [--period-ns 1.0] [-o act.json]

Reads a TCF 1.0 file (nets as `"name[i]" : "<duty> <toggle count>";` inside
nested `instance ("x") { ... }` blocks, `duration`/`unit` in the header) and
writes {net base name: mean data toggles per clock cycle}: the bits of a
vector are averaged, and a name used in several instances takes the most
active one. multibit.py's `activity:` then sends every register at or above
`hot_toggle_rate` to `hot_family` (an SDC-less, cell-free rule: the pack names
the families, this file only says which registers change often).
The second form writes the per-register file of the energy rule (multibit.py,
`activity: {rule: energy}`): from a netlist of an earlier run (its flop
instances are the register-mapper's, `<reg>_mb<a>_<b>[_x<n>]` / `<reg>_bit<k>`) and
the net toggle counts of a simulation of it (a TCF, or a tab-separated
`net block toggles ...` dump with `--cycles`: block 0 is the whole span), it
writes {"period_ns": T, "registers": {reg: {"q", "d", "clk", "bits"}}}: data
toggles per bit and cycle on the outputs and inputs, and clock pulses per
cycle on the register's clock net (a clock gate that is off lowers it).
Register names are module-local, like the mapper's (a name that two instances
of a module share takes the busiest).
Names are the RTL's; the register-mapper's registers may carry a `_cg<N>...`
suffix from clock-gate inference, which multibit.activity_overrides allows.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

NET = re.compile(r'^\s*"([^"]+)"\s*:\s*"([-\d.eE+]+)\s+(\d+)"\s*;')
BIT = re.compile(r"^(.*?)(?:\[\d+\])+$")


def parse(text: str, period_ns: float) -> dict:
    dur = float(re.search(r'duration\s*:\s*"([\d.eE+-]+)"', text).group(1))
    unit = re.search(r'unit\s*:\s*"(\w+)"', text).group(1)
    scale = {"ns": 1.0, "ps": 1e-3, "us": 1e3}[unit]
    cycles = dur * scale / period_ns
    toggles = defaultdict(lambda: defaultdict(list))  # name -> instance path -> [tc per bit]
    stack = []
    for line in text.splitlines():
        m = re.match(r'^\s*instance\s*\("([^"]*)"\)\s*{', line)
        if m:
            stack.append(m.group(1))
            continue
        if re.match(r"^\s*instance\s*\(\)\s*{", line):
            stack.append("")
            continue
        m = NET.match(line)
        if m:
            name = m.group(1)
            b = BIT.match(name)
            toggles[b.group(1) if b else name]["/".join(stack)].append(int(m.group(3)))
            continue
        if re.match(r"^\s*}\s*$", line) and stack:
            # only instance blocks push; net () { } blocks close here too
            pass
    out = {}
    for name, per_inst in toggles.items():
        out[name] = max(sum(v) / len(v) for v in per_inst.values()) / cycles
    return out


FLOP_NAME = re.compile(r"^(?P<reg>.+?)_(?:mb\d+_\d+|bit\d+)(?:_\d+)?(?:_x\d+)?$")
INST = re.compile(r"^\s*(\w+)\s+(\\?\S+)\s*\(\s*$")
PIN = re.compile(r"\.(\w+)\(\s*([^()]*?)\s*\)")


def tcf_counts(text: str, period_ns: float):
    """({net path below the top instance: toggle count}, cycles) from a TCF."""
    dur = float(re.search(r'duration\s*:\s*"([\d.eE+-]+)"', text).group(1))
    unit = re.search(r'unit\s*:\s*"(\w+)"', text).group(1)
    cycles = dur * {"ns": 1.0, "ps": 1e-3, "us": 1e3}[unit] / period_ns
    counts, stack = {}, []
    for line in text.splitlines():
        m = re.match(r'^\s*instance\s*\("([^"]*)"\)\s*{', line)
        if m:
            stack.append(m.group(1))
            continue
        if re.match(r"^\s*instance\s*\(\)\s*{", line):
            stack.append("")
            continue
        m = NET.match(line)
        if m:
            counts["/".join(stack[1:] + [m.group(1)])] = int(m.group(3))  # the root is the top module
    return counts, cycles


def tsv_counts(text: str, block: str = "0"):
    """{net: toggles} from a `net block toggles ...` dump, one block."""
    out = {}
    for line in text.splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 3 and f[1] == block:
            out[f[0]] = out.get(f[0], 0) + int(float(f[2]))
    return out


def register_activity(netlist: str, counts: dict, cycles: float,
                      clock_pin: str = "CK", out_re: str = r"^Q\d*$",
                      in_re: str = r"^D\d*$", escape: str = "2F",
                      period_ns: float = 1.0) -> dict:
    """The energy rule's per-register file from a netlist and net toggles."""
    orx, irx = re.compile(out_re), re.compile(in_re)
    acc = {}  # reg -> instance path -> [bits, qtog, dtog, clk pulses * bits]
    for blk in re.split(r";\s*", netlist):
        m = INST.match(blk.split("(", 1)[0] + "(") if "(" in blk else None
        if not m:
            continue
        name = m.group(2).lstrip("\\")
        fm = FLOP_NAME.match(name)
        if not fm:
            continue
        pins = dict(PIN.findall(blk))
        qn = [n for p, n in pins.items() if orx.match(p)]
        dn = [n for p, n in pins.items() if irx.match(p)]
        if not qn or clock_pin not in pins:
            continue
        path = fm.group("reg").replace(escape, "/") if escape else fm.group("reg")
        inst, _, reg = path.rpartition("/")
        a = acc.setdefault(reg, {}).setdefault(inst, [0, 0.0, 0.0, 0.0])
        bits = len(qn)
        a[0] += bits
        a[1] += sum(counts.get(n, 0) for n in qn)
        a[2] += sum(counts.get(n, 0) for n in dn)
        clk = counts.get(pins[clock_pin])
        a[3] += bits * (clk / 2.0 / cycles if clk is not None else 1.0)
    regs = {}
    for reg, per in acc.items():
        best = None
        for b, q, d, c in per.values():
            r = {"q": round(q / b / cycles, 6), "d": round(d / b / cycles, 6),
                 "clk": round(c / b, 6), "bits": b}
            if best is None or r["q"] + r["clk"] > best["q"] + best["clk"]:
                best = r
        regs[reg] = best
    return {"period_ns": period_ns, "registers": dict(sorted(regs.items()))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tcf", help="a TCF, or with --netlist also a net,block,"
                    "toggles TSV")
    ap.add_argument("--period-ns", type=float, default=1.0)
    ap.add_argument("--netlist", help="write the energy rule's per-register file")
    ap.add_argument("--cycles", type=float, help="span in clock cycles (TSV)")
    ap.add_argument("--block", default="0", help="TSV block of the whole span")
    ap.add_argument("--clock-pin", default="CK")
    ap.add_argument("--out-pin-re", default=r"^Q\d*$")
    ap.add_argument("--in-pin-re", default=r"^D\d*$")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    text = Path(a.tcf).read_text()
    if a.netlist:
        if text.lstrip().startswith("net\t") or "\t" in text.split("\n", 1)[0]:
            if not a.cycles:
                ap.error("a TSV needs --cycles")
            counts, cycles = tsv_counts(text, a.block), a.cycles
        else:
            counts, cycles = tcf_counts(text, a.period_ns)
        out = register_activity(Path(a.netlist).read_text(), counts, cycles,
                                a.clock_pin, a.out_pin_re, a.in_pin_re,
                                period_ns=a.period_ns)
        txt = json.dumps(out, indent=1) + "\n"
        (Path(a.output).write_text(txt) if a.output else sys.stdout.write(txt))
        return 0
    rates = parse(text, a.period_ns)
    text = json.dumps(dict(sorted(rates.items())), indent=0) + "\n"
    (Path(a.output).write_text(text) if a.output else sys.stdout.write(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
