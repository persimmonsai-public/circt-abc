#!/usr/bin/env python3
"""Depth-class grouping of residual modules for partitioned ABC mapping.

    depth_groups.py <flat.mlir> <k> <group_map.json> --opt <circt-abc-opt>
                    [--adder-arch BRENT-KUNG] [--report <tsv>]

WHY. `&nf` gives every output of a partition the partition's critical delay
as its required time (Nf_ManSetOutputRequireds), so area recovery deepens
every cone in a partition up to the deepest cone in it. merge-residual-modules
splits modules into groups ALPHABETICALLY, so a module's timing budget is set
by whatever happens to sort next to it. In one design a group held
rv_plic_target -- a long linear priority search -- next to cva6's serdiv:
serdiv's 64-bit add/sub came out about three times as deep as when mapped
alone and was the design's worst path.

WHAT. Lower every `*_residual` module of <flat.mlir> to an AIG (the same
adder architecture the flow stamps), take each module's longest
input-to-output AIG path as its intrinsic depth, sort modules by depth and
cut the sorted list into <k> contiguous groups of roughly equal AND count.
Each group then holds modules of similar depth, so no shallow datapath
inherits a deep neighbour's budget. Writes the {module: group} JSON that
merge-residual-modules' `group-map` option consumes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict, deque

HDR = re.compile(r"^\s*hw\.module\s+(?:private\s+)?@([\w$.]+)\((.*)$")
NH = re.compile(r'\s*\{[^{}<>]*=[^{}<>]*\}')
VAL = re.compile(r"%([\w$.\-]+)(?:#(\d+))?")


def split_top(s, sep=","):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [x.strip() for x in out]


_w = {}


def width(t):
    t = t.strip()
    if t in _w:
        return _w[t]
    if re.fullmatch(r"i\d+", t):
        w = int(t[1:])
    elif t.startswith(("!hw.struct<", "struct<")):
        body = t[t.index("<") + 1:-1]
        w = sum(width(f.split(":", 1)[1]) for f in split_top(body))
    elif t.startswith(("!hw.array<", "array<")):
        m = re.match(r"(\d+)x(.*)$", t[t.index("<") + 1:-1])
        w = int(m.group(1)) * width(m.group(2))
    else:
        raise ValueError(f"type {t!r}")
    _w[t] = w
    return w


def module_depth(lines, ins):
    """Longest AIG path (and_inv = 1 level) of one leaf module."""
    env = {}
    for n, t in ins:
        env[n] = [0] * width(t)
    ands = 0

    def get(tok, w):
        m = VAL.fullmatch(tok.strip())
        name = m.group(1) + (f"#{m.group(2)}" if m.group(2) else "")
        # forward references only arise through graph cycles, which leaf
        # residual modules do not have; treat as depth 0 conservatively
        return env.get(name, [0] * w)

    best = 0
    for s in lines:
        if s.startswith("hw.output") or " = " not in s:
            continue
        lhs, rhs = s.split(" = ", 1)
        rhs = NH.sub("", rhs)
        lhs = lhs.strip()
        op = rhs.split()[0]
        if op in ("hw.constant", "hw.aggregate_constant"):
            env[lhs[1:]] = [0] * (width(rhs.rsplit(" : ", 1)[1]) if " : " in rhs else 1)
        elif op == "synth.aig.and_inv":
            body, ty = rhs[len(op):].rsplit(" : ", 1)
            w = width(ty)
            ops = [get(o.replace("not ", ""), w) for o in split_top(body)]
            # a variadic and_inv of n operands is a balanced tree of ANDs
            extra = max(1, (len(ops) - 1).bit_length())
            v = [max(o[i] for o in ops) + extra for i in range(w)]
            ands += w * (len(ops) - 1)
            env[lhs[1:]] = v
            best = max(best, max(v) if v else 0)
        elif op == "comb.extract":
            m = re.match(r"comb\.extract (\S+) from (\d+) : \((.*)\) -> (.*)$", rhs)
            src = get(m.group(1), width(m.group(3)))
            lo = int(m.group(2))
            env[lhs[1:]] = src[lo:lo + width(m.group(4))]
        elif op == "comb.concat":
            body, tys = rhs[len(op):].rsplit(" : ", 1)
            parts = [get(o, width(t)) for o, t in zip(split_top(body), split_top(tys))]
            bits = []
            for p in reversed(parts):
                bits += p
            env[lhs[1:]] = bits
        elif op == "comb.replicate":
            m = re.match(r"comb\.replicate (\S+) : \((.*)\) -> (.*)$", rhs)
            src = get(m.group(1), width(m.group(2)))
            env[lhs[1:]] = src * (width(m.group(3)) // max(1, len(src)))
        elif op == "hw.bitcast":
            m = re.match(r"hw\.bitcast (\S+) : \((.*)\) -> (.*)$", rhs)
            env[lhs[1:]] = get(m.group(1), width(m.group(2)))
        elif op == "hw.struct_explode":
            m = re.match(r"hw\.struct_explode (\S+) : (.*)$", rhs)
            t = m.group(2).strip()
            fields = [width(f.split(":", 1)[1]) for f in split_top(t[t.index("<") + 1:-1])]
            src = get(m.group(1), sum(fields))
            names = [x.strip()[1:] for x in lhs.split(",")]
            if len(names) == 1 and ":" in names[0]:
                base = names[0].split(":")[0]
                names = [f"{base}#{k}" for k in range(len(fields))]
            hi = len(src)
            for k, w in enumerate(fields):
                env[names[k]] = src[hi - w:hi]; hi -= w
        else:
            # any other op: conservatively depth of its widest operand
            w = width(rhs.rsplit(":", 1)[1].split("->")[-1].strip(" ()")) if ":" in rhs else 1
            srcs = [get("%" + m.group(1), 1) for m in VAL.finditer(rhs)]
            d = max([max(x) for x in srcs if x] or [0])
            env[lhs.split(",")[0].strip()[1:]] = [d] * w
    return best, ands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("flat")
    ap.add_argument("k", type=int)
    ap.add_argument("out")
    ap.add_argument("--opt", required=True)
    ap.add_argument("--adder-arch", default=os.environ.get("CIRCT_FLOW_ADDER_ARCH", "BRENT-KUNG"))
    ap.add_argument("--report")
    ap.add_argument("--floor", type=int, default=16)
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="depth_groups.", dir=os.path.dirname(os.path.abspath(a.out)))
    res = os.path.join(work, "residuals.mlir")
    with open(a.flat) as fi, open(res, "w") as fo:
        fo.write("module {\n")
        keep = False
        for line in fi:
            m = HDR.match(line)
            if m:
                keep = m.group(1).endswith("_residual")
            if keep:
                fo.write(line)
                if line.startswith("  }"):
                    keep = False
        fo.write("}\n")
    aig = os.path.join(work, "residuals_aig.mlir")
    # icmp's 4-state predicates are irrelevant to depth; normalise them so
    # convert-comb-to-synth accepts every module.
    subprocess.run(["sed", "-i", "-E",
                    r"s/comb\.icmp (bin )?(weq|ceq) /comb.icmp \1eq /; "
                    r"s/comb\.icmp (bin )?(wne|cne) /comb.icmp \1ne /", res], check=True)
    cmd = [a.opt, res, "--hw-convert-bitcasts", "--hw-aggregate-to-comb"]
    if a.adder_arch != "none":
        cmd.append(f"--set-adder-arch=arch={a.adder_arch}")
    cmd += ["--convert-comb-to-synth=force-aig=true", "--canonicalize", "-o", aig]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        sys.exit(f"depth_groups: lowering failed:\n{r.stderr[-2000:]}")

    mods = {}
    cur, ins, body = None, None, []
    for line in open(aig):
        m = HDR.match(line)
        if m:
            rest = m.group(2)
            d, k = 1, 0
            while d:
                ch = rest[k]
                if ch in "(<[{": d += 1
                elif ch in ")>]}": d -= 1
                k += 1
            ins = []
            for p in split_top(rest[:k - 1]):
                pm = re.match(r"(in|out)\s+%?([\w$.\-]+)\s*:\s*(.*)$", re.sub(r"\s*\{.*\}\s*$", "", p))
                if pm and pm.group(1) == "in":
                    ins.append((pm.group(2), pm.group(3).strip()))
            cur, body = m.group(1), []
            continue
        if cur is not None:
            s = line.strip()
            if s == "}":
                mods[cur] = module_depth(body, ins)
                cur = None
            elif s:
                body.append(s)

    names = sorted(mods, key=lambda n: (-mods[n][0], n))

    def bands(ratio):
        """Walk modules deepest first; open a new group whenever depth drops
        below ratio x the current group's deepest member. Everything at or
        below --floor shares the last band (budgets that small differ by a
        few levels only)."""
        gm, g, top = {}, -1, None
        for n in names:
            d = mods[n][0]
            if top is None or (d < top * ratio and top > a.floor):
                g, top = g + 1, d
            gm[n] = g
        return gm, g + 1

    # the tightest ratio (closest to 1) that still fits in k groups
    lo, hi, best = 0.05, 0.99, None
    for _ in range(30):
        mid = (lo + hi) / 2
        gm, n = bands(mid)
        if n <= a.k:
            best, lo = (gm, mid), mid
        else:
            hi = mid
    gm, ratio = best
    with open(a.out, "w") as f:
        json.dump(gm, f, indent=0, sort_keys=True)
    per = defaultdict(lambda: [0, 0, 10**9, 0])
    for n, gi in gm.items():
        d, s = mods[n]
        p = per[gi]; p[0] += 1; p[1] += s; p[2] = min(p[2], d); p[3] = max(p[3], d)
    print(f"depth_groups: {len(mods)} residual modules -> {len(per)} groups by depth "
          f"class (band ratio {ratio:.2f}, floor {a.floor})")
    for gi in sorted(per):
        p = per[gi]
        print(f"  g{gi:<3} {p[0]:4} modules {p[1]:9} ANDs  depth {p[2]}..{p[3]}")
    if a.report:
        with open(a.report, "w") as f:
            for n in names:
                f.write(f"{n}\t{mods[n][0]}\t{mods[n][1]}\t{gm[n]}\n")


if __name__ == "__main__":
    main()
