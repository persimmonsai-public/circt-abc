#!/usr/bin/env python3
"""Emit a Yosys script that gives every flop a name derived from its instance.

`design_level_cec.sh` cuts flops and compares the two netlists' combinational
logic with ABC's `cec`, which pairs inputs and outputs by NAME.  Yosys names an
AIGER latch after whatever wire happens to carry its state, and two equivalent
netlists disagree on that wire: on ibex the state of a bit-level clock-gated
flop is named after the residual gate it feeds (`...merged_residual2Fu_bits2F
g3350.A`), and `async2sync` moves the `.IQ` name onto its reset mux, leaving
the real state wire auto-named.  Pairing by position instead is wrong too: the
latch order follows the netlist's instance order, which differs.

The stable identity is the liberty cell instance: every flop model in the
library stores its state in `IQ` (`ff(IQ,IQN)`, `ff_bank(IQ,IQN,n)`), so
after `flatten` the state is `<instance>.IQ`, possibly behind `async2sync`'s
mux.  For each `$_DFF_P_` this finds that name and emits

    add -wire cecq_<inst>.IQ ; connect -set cecq_<inst>.IQ <state bit>
    add -output cecc_<inst>.IQ ; connect -set cecc_<inst>.IQ <clock bit>

so the latch is named `cecq_*` and its clock (after any clock gate) becomes a
compared output `cecc_*`.  Fails if a flop has no `.IQ` name or two flops get
the same one, because either would make the comparison meaningless.

Usage: cec_latch_names.py <design.json> <top> <out.ys>
"""
import json, re, sys
from collections import defaultdict

src, top, dst = sys.argv[1], sys.argv[2], sys.argv[3]
mod = json.load(open(src))["modules"][top]

names = defaultdict(list)   # bit -> [(public?, name, index, width, offset)]
for n, w in mod["netnames"].items():
    bits, off = w["bits"], w.get("offset", 0)
    for i, b in enumerate(bits):
        if isinstance(b, int):
            names[b].append((not w.get("hide_name", 0), n, i, len(bits), off))

def ref(bit):
    """A sigspec for `bit` that a Yosys script can parse."""
    cands = sorted(names[bit], key=lambda c: (c[3] != 1, c[4] != 0, len(c[1])))
    for _, n, i, width, off in cands:
        if off != 0 or any(ch in n for ch in " []{}\\"):
            continue
        n = n if n.startswith("$") else "\\" + n
        return n if width == 1 else f"{n} [{i}]"
    sys.exit(f"cec_latch_names: no usable name for bit {bit}")

def iq_name(bit):
    for pub, n, i, width, _ in names[bit]:
        if pub and n.endswith(".IQ"):
            return n if width == 1 else f"{n}_b{i}"
    return None

# async2sync: Y = S ? reset_value : Q.  Follow $_MUX_ data inputs downstream.
mux_from = defaultdict(list)
for c in mod["cells"].values():
    if c["type"] == "$_MUX_":
        for p in ("A", "B"):
            mux_from[c["connections"][p][0]].append(c["connections"]["Y"][0])

out, missing, seen = [], [], {}
for cname, c in sorted(mod["cells"].items()):
    if c["type"] != "$_DFF_P_":
        continue
    q, clk = c["connections"]["Q"][0], c["connections"]["C"][0]
    canon, frontier = iq_name(q), [q]
    for _ in range(2):                       # dffsr emulation adds a second mux
        if canon:
            break
        frontier = [y for b in frontier for y in mux_from[b]]
        canon = next((iq_name(y) for y in frontier if iq_name(y)), None)
    if not canon:
        missing.append(cname)
        continue
    canon = re.sub(r"[^A-Za-z0-9_.]", "_", canon)
    if canon in seen:
        sys.exit(f"cec_latch_names: {canon} names two flops ({seen[canon]}, {cname})")
    seen[canon] = cname
    out.append(f"add -wire cecq_{canon} 1")
    out.append(f"connect -set cecq_{canon} {ref(q)}")
    if isinstance(clk, int):
        out.append(f"add -output cecc_{canon} 1")
        out.append(f"connect -set cecc_{canon} {ref(clk)}")

if missing:
    sys.exit(f"cec_latch_names: {len(missing)} flop(s) have no .IQ name, "
             f"e.g. {missing[:3]}")
open(dst, "w").write("\n".join(out) + "\n")
print(f"cec_latch_names: named {len(seen)} flops")
