#!/usr/bin/env python3
"""Generate the 4-input P-class database for `--resub-mapped`.

WHY.  `ResubMappedPass` (ASP-DAC'25 s3.3) replaces a mapped sub-circuit with a
cheaper one computing the same function.  Without a database it can only try
SINGLE-cell replacements, which find little.  The database supplies, per
P-equivalence class of 4-input functions, the cheapest mapped implementation
ABC can find.

The P-class key MUST match the pass exactly: the minimum truth table over the
24 input permutations, using its `permute4` convention (kPerms in
ResubMappedPass.cpp).  ABC's `read_truth` uses the same variable order (input
`a` is bit 0 of the minterm index), so the two agree.

Format consumed by loadDatabase():
    TT <hex4> <area> <ncells>
    C <cellname> <PIN>=<Lk|Nk> ...
`Lk` = database leaf k, `Nk` = k-th cell of this entry, last C line = output.

usage: gen_resub_db.py <out.db> --liberty L [--abc P] [--limit N]
"""
import argparse, itertools, os, re, subprocess, sys, tempfile

PERMS = list(itertools.permutations(range(4)))


def permute4(tt, perm):
    out = 0
    for m in range(16):
        om = 0
        for j in range(4):
            if (m >> j) & 1:
                om |= 1 << perm[j]
        if (tt >> om) & 1:
            out |= 1 << m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--liberty', required=True)
    ap.add_argument('--abc', default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'third_party/yosys/build/yosys-abc'))
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()

    reps = sorted({min(permute4(tt, p) for p in PERMS) for tt in range(1 << 16)})
    if a.limit:
        reps = reps[:a.limit]
    sys.stderr.write('P-classes: %d\n' % len(reps))

    lib = open(a.liberty, errors='ignore').read()
    area = {}
    for m in re.finditer(r'\bcell\s*\(\s*"?(\w+)"?\s*\)\s*\{', lib):
        n = m.group(1); s = m.end(); d = 1; i = s
        while i < len(lib) and d:
            d += (lib[i] == '{') - (lib[i] == '}'); i += 1
        am = re.search(r'area\s*:\s*([0-9.]+)', lib[s:i])
        if am:
            area[n] = float(am.group(1))

    tmp = tempfile.mkdtemp(prefix='resubdb')
    script = os.path.join(tmp, 'go.abc')
    with open(script, 'w') as fh:
        fh.write('read_lib %s\n' % a.liberty)
        for tt in reps:
            fh.write('read_truth %04x\nstrash\ndc2\nmap -a\nwrite_verilog %s/m_%04x.v\n'
                     % (tt, tmp, tt))
    r = subprocess.run([a.abc, '-q', 'source %s' % script], capture_output=True, text=True)
    sys.stderr.write('abc rc=%d\n' % r.returncode)

    LEAF = {'a': 0, 'b': 1, 'c': 2, 'd': 3}
    written = skipped = 0
    with open(a.out, 'w') as out:
        out.write('# 4-input P-class database for --resub-mapped (%d classes)\n' % len(reps))
        for tt in reps:
            p = '%s/m_%04x.v' % (tmp, tt)
            if not os.path.exists(p):
                skipped += 1; continue
            v = open(p).read()
            cells = []
            for m in re.finditer(r'^\s{2,}([A-Za-z][A-Za-z0-9_]*)\s+[A-Za-z_]\w*\s*\(([^;]*)\);', v, re.M):
                name, body = m.group(1), m.group(2)
                if name not in area:
                    continue
                pins = [(k, val.strip()) for k, val in re.findall(r'\.(\w+)\(([^)]*)\)', body)]
                cells.append((name, pins))
            if not cells:
                skipped += 1; continue          # constant / buffer-only class
            # ABC writes the output pin LAST on each instance, and emits
            # cells in topological order, so the driver map is unambiguous.
            drv = {}
            for idx, (name, pins) in enumerate(cells):
                drv[pins[-1][1]] = idx          # last pin is this cell's output
            lines, maxleaf, bad = [], -1, False
            for name, pins in cells:
                toks = []
                for pin, net in pins[:-1]:      # inputs only
                    if net in drv:
                        toks.append('%s=N%d' % (pin, drv[net]))
                    elif net in LEAF:
                        maxleaf = max(maxleaf, LEAF[net])
                        toks.append('%s=L%d' % (pin, LEAF[net]))
                    else:
                        bad = True              # constant or unknown net
                        break
                if bad:
                    break
                lines.append('C %s %s' % (name, ' '.join(toks)))
            if bad or not lines:
                skipped += 1
                continue
            tot = sum(area[n] for n, _ in cells)
            out.write('TT %04x %.6f %d\n' % (tt, tot, len(cells)))
            for l in lines:
                out.write(l + '\n')
            written += 1
    sys.stderr.write('wrote %d entries, skipped %d -> %s\n' % (written, skipped, a.out))


if __name__ == '__main__':
    main()
