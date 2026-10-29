#!/usr/bin/env python3
"""Reconnect combinational paths the residual interface severs.

`merge-residual-modules` builds ONE module from ~34 outlined blocks, but its
port list is the CONCATENATION of theirs.  Where block A's output feeds block
B's input, the parent wires the merged instance's own output back into its own
input -- so in the AIG that ABC/emap optimise, the path is cut into a PO and an
unrelated PI.  On ibex that is **2,934 of 5,139 input bits (57%)**.

Nothing can share, factor or merge a cut across such a break: the two sides are
different variables.  This rewrites the AIGER so each severed PI is replaced by
the logic actually driving the corresponding PO, leaving the module's PORT LIST
untouched (the stitched inputs simply go unused), so the flow can still splice
the mapped netlist back by port.

CORRECTNESS.  The result is NOT equivalent to the input as a standalone
circuit -- it differs exactly when a severed PI disagrees with its PO, which
the parent's wiring makes impossible.  `&cec` against the original AIG is
therefore expected to FAIL and proves nothing.  Verify at the DESIGN level:
build the AIG of the finished flat netlist before and after and compare those.

Usage: aig_stitch_residual.py <in.aig> <mapped.mlir> <out.aig>
"""
import re, sys, collections

src, mlir, dst = sys.argv[1], sys.argv[2], sys.argv[3]

# ---------- 1. port lists of the bit-level module (= AIG PI/PO order) -------
s = open(mlir).read()
m = re.search(r'hw\.module private @abc_bridge_merged_residual_bits\(', s)
if not m: sys.exit("bit-level residual module not found")
j = s.index('(', m.start()); k = j + 1; d = 1
while d > 0:
    if s[k] == '(': d += 1
    elif s[k] == ')': d -= 1
    k += 1
sig = s[j + 1:k - 1]
# Two spellings per port: ports whose bit name needs mangling carry a quoted
# display name, 1-bit word ports (no "[k]" suffix) do not.  Matching only the
# quoted form silently finds 3,585 of 5,737 inputs.
pis = [ (a or b) for a, b in
        re.findall(r'\bin\s+%\S+\s+"([^"]+)"\s*:|\bin\s+%([A-Za-z_]\w*)\s*:', sig) ]
pos = [ (a or b) for a, b in
        re.findall(r'\bout\s+"([^"]+)"\s*:|\bout\s+([A-Za-z_]\w*)\s*:', sig) ]
pi_ix = {n: i for i, n in enumerate(pis)}
po_ix = {n: i for i, n in enumerate(pos)}
print(f"bit module: {len(pis)} inputs, {len(pos)} outputs")

# ---------- 2. which word-level inputs are fed by our own outputs ----------
mi = re.search(r'hw\.instance "[^"]*" @abc_bridge_merged_residual\(', s)
j = s.index('(', mi.start()); k = j + 1; d = 1
while d > 0:
    if s[k] == '(': d += 1
    elif s[k] == ')': d -= 1
    k += 1
ops = re.findall(r'([A-Za-z_]\w*)\s*:\s*(%[A-Za-z0-9_.#]+)\s*:\s*(i\d+|!\S+?)(?=,|$)', s[j+1:k-1])
pairs = []
for port, val, ty in ops:
    mm = re.match(r'%[A-Za-z0-9_]*\.(\S+)$', val)
    if not mm: continue
    outp = mm.group(1)
    w = int(re.match(r'i(\d+)$', ty.strip()).group(1)) if re.match(r'i\d+$', ty.strip()) else 1
    for b in range(w):
        a, c = f"{port}[{b}]", f"{outp}[{b}]"
        if w == 1 and a not in pi_ix: a = port
        if w == 1 and c not in po_ix: c = outp
        if a in pi_ix and c in po_ix: pairs.append((pi_ix[a], po_ix[c]))
print(f"severed PI<-PO bit pairs to stitch: {len(pairs)}")

# ---------- 3. read AIGER ---------------------------------------------------
f = open(src, 'rb')
hdr = f.readline().split()
M, I, L, O, A = (int(x) for x in hdr[1:6])
assert L == 0, "combinational AIG expected"
outs = [int(f.readline()) for _ in range(O)]
def dec():
    x = 0; sh = 0
    while True:
        c = f.read(1)[0]
        x |= (c & 0x7f) << sh
        if not (c & 0x80): break
        sh += 7
    return x
ands = []
for i in range(A):
    lhs = 2 * (I + 1 + i); d0 = dec(); d1 = dec()
    r0 = lhs - d0; r1 = r0 - d1
    ands.append((r0, r1))
print(f"input AIG: I={I} O={O} A={A}")

# ---------- 4. rebuild with the substitution applied ------------------------
sub = {}                      # PI literal -> replacement literal (resolved later)
for pi, po in pairs: sub[2 * (pi + 1)] = outs[po]
andmap = {2 * (I + 1 + i): a for i, a in enumerate(ands)}

new_ands, cache, order = {}, {}, []
inprog = set(); cycles = [0]
import sys as _s
_s.setrecursionlimit(1 << 20)
def res(lit):
    """resolve a literal in the stitched graph, memoised"""
    v, c = lit & ~1, lit & 1
    if v == 0: return c
    if v in cache: return cache[v] ^ c
    if v <= 2 * I:                       # a primary input
        if v in sub:
            if v in inprog:
                cycles[0] += 1; return c   # CYCLE -- returns a constant (WRONG)
            inprog.add(v)
            r = res(sub[v])
            inprog.discard(v); cache[v] = r
            return r ^ c
        cache[v] = v
        return v ^ c
    a, b = andmap[v]                     # an AND node
    if v in inprog:
        cycles[0] += 1; return c           # CYCLE -- returns a constant (WRONG)
    inprog.add(v)
    ra, rb = res(a), res(b)
    inprog.discard(v)
    if ra == 0 or rb == 0: r = 0
    elif ra == 1: r = rb
    elif rb == 1: r = ra
    else:
        key = (min(ra, rb), max(ra, rb))
        if key in new_ands: r = new_ands[key]
        else:
            r = 2 * (I + 1 + len(order)); new_ands[key] = r; order.append(key)
    cache[v] = r
    return r ^ c

new_outs = [res(o) for o in outs]
print(f"CYCLES HIT (silently constant-folded): {cycles[0]}")
print(f"stitched AIG: A={len(order)}  (was {A}, {100*(len(order)-A)/A:+.1f}%)")

# ---------- 5. write AIGER --------------------------------------------------
with open(dst, 'wb') as g:
    g.write(b"aig %d %d 0 %d %d\n" % (I + len(order), I, O, len(order)))
    for o in new_outs: g.write(b"%d\n" % o)
    def enc(x):
        out = bytearray()
        while True:
            c = x & 0x7f; x >>= 7
            if x: out.append(c | 0x80)
            else: out.append(c); break
        return bytes(out)
    for i, (a, b) in enumerate(order):
        lhs = 2 * (I + 1 + i)
        hi, lo = max(a, b), min(a, b)
        g.write(enc(lhs - hi)); g.write(enc(hi - lo))
print(f"wrote {dst}")
