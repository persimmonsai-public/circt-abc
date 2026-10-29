#!/usr/bin/env python3
"""Generate a genlib for emap that carries REAL cell delays.

WHY.  A genlib ABC writes from a liberty read without `-l` gives every gate
the same unit delay `1.00 0.00 1.00 0.00`, so emap is told an 8-input AOI is
exactly as fast as a NAND2.  Under unit delays emap has no reason not to
stack wide slow cells, `--relax`/`--required` have nothing to act on, and
emap's reported "delay" is a cell-level count with no relation to timing.

ABC can derive real delays with `read_lib -l` from a liberty that carries
the NLDM tables, but that path reproduces `&nf`'s own two blind spots -- the
6-input cap and the `pTwin` multi-output skip -- so it drops exactly the
wide cells and the multi-output (full/half adder) cells that emap's
advantage over `&nf` rests on.

So: take ABC's timed gates verbatim, and append the missing gate/output
pairs with delays extracted from the NLDM here, mapped onto ABC's own scale
by a linear calibration fitted, at run time, over the pin-arcs of the gates
ABC did emit (`--report-fit` prints it).  The reference slew is the one ABC
reports for `read_lib -l` ("at slew <x> ps").

Usage:
    gen_timed_genlib.py <out.genlib> --lib <liberty> [--abc <path>]
"""
import argparse, bisect, os, re, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_ABC = os.path.join(ROOT, "third_party/yosys/build/yosys-abc")


def blocks(body, kw):
    """Yield (header_args, block_body) for each `kw(...) {...}` at any depth."""
    for m in re.finditer(r'\b%s\s*\(([^)]*)\)\s*\{' % kw, body):
        k = m.end(); d = 1; j = k
        while j < len(body) and d:
            d += (body[j] == '{') - (body[j] == '}'); j += 1
        yield m.group(1).strip(), body[k:j - 1]


def cell_starts(txt):
    """Cell name -> offset just past its `cell(name) {`."""
    return {m.group(1): m.end()
            for m in re.finditer(r'\bcell\s*\(\s*"?(\w+)"?\s*\)\s*\{', txt)}


def cell_body(txt, starts, name):
    if name not in starts:
        return None
    s = starts[name]; d = 1; i = s
    while i < len(txt) and d:
        d += (txt[i] == '{') - (txt[i] == '}'); i += 1
    return txt[s:i - 1]


def nums(s):
    return [float(x) for x in re.findall(r'-?\d+\.?\d*(?:[eE][-+]?\d+)?', s)]


def units(txt):
    """(time, capacitance) scale factors from the liberty's units to ns, pF."""
    t = re.search(r'time_unit\s*:\s*"?\s*([0-9.]*)\s*(ns|ps)', txt)
    c = re.search(r'capacitive_load_unit\s*\(\s*([0-9.]+)\s*,\s*(pf|ff)',
                  txt, re.I)
    ts = (float(t.group(1) or 1) * (1.0 if t.group(2) == 'ns' else 1e-3)
          if t else 1.0)
    cs = (float(c.group(1)) * (1.0 if c.group(2).lower() == 'pf' else 1e-3)
          if c else 1.0)
    return ts, cs


def table(blk, ts=1.0, cs=1.0):
    """NLDM table (slew index, load index, rows) in ns and pF; the liberty's
    own units are ts ns and cs pF."""
    i1 = re.search(r'index_1\s*\(([^)]*)\)', blk)
    i2 = re.search(r'index_2\s*\(([^)]*)\)', blk)
    vm = re.search(r'values\s*\((.*?)\)\s*;', blk, re.S)
    if not (i1 and i2 and vm):
        return None
    x, y = nums(i1.group(1)), nums(i2.group(1))
    rows = [nums(r) for r in re.findall(r'"([^"]*)"', vm.group(1))]
    if len(rows) != len(x) or any(len(r) != len(y) for r in rows):
        return None
    return ([v * ts for v in x], [v * cs for v in y],
            [[v * ts for v in r] for r in rows])


def at_slew(t, slew):
    x, y, rows = t
    k = bisect.bisect_left(x, slew)
    if k <= 0:
        return y, rows[0]
    if k >= len(x):
        return y, rows[-1]
    f = (slew - x[k - 1]) / (x[k] - x[k - 1])
    return y, [a + f * (b - a) for a, b in zip(rows[k - 1], rows[k])]


def fit(y, v):
    """Least squares d = PD + LD*C.  y in pF, v in ns -> PD ps, LD ps/fF."""
    n = len(y); sx = sum(y); sy = sum(v)
    sxx = sum(a * a for a in y); sxy = sum(a * b for a, b in zip(y, v))
    den = n * sxx - sx * sx
    if den == 0:
        return None
    ld = (n * sxy - sx * sy) / den
    return ((sy - ld * sx) / n) * 1000.0, ld


def arcs(txt, starts, cell, outpin, slew_ns, ts=1.0, cs=1.0):
    """Per input pin, the worst (PD, LD) over `when` groups, rise/fall averaged."""
    b = cell_body(txt, starts, cell)
    if b is None:
        return {}
    for pn, pb in blocks(b, 'pin'):
        if pn.strip() != outpin:
            continue
        per = {}
        for _, tb in blocks(pb, 'timing'):
            rp = re.search(r'related_pin\s*:\s*"?(\w+)"?', tb)
            if not rp:
                continue
            rp = rp.group(1); got = {}
            for kind in ('cell_rise', 'cell_fall'):
                for _, tab in blocks(tb, kind):
                    t = table(tab, ts, cs)
                    if t:
                        y, v = at_slew(t, slew_ns)
                        r = fit(y, v)
                        if r:
                            got.setdefault(kind, []).append(r)
            rr, ff = got.get('cell_rise', []), got.get('cell_fall', [])
            for i in range(max(len(rr), len(ff))):
                a = rr[i] if i < len(rr) else None
                c = ff[i] if i < len(ff) else None
                p = ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2) if (a and c) else (a or c)
                if rp not in per or p[0] > per[rp][0]:
                    per[rp] = p
        return per
    return {}


def linfit(pairs):
    """Least squares b = A*a + B over (a, b) pairs -> (A, B, residual sd)."""
    n = len(pairs)
    sx = sum(a for a, _ in pairs); sy = sum(b for _, b in pairs)
    sxx = sum(a * a for a, _ in pairs); sxy = sum(a * b for a, b in pairs)
    A = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    B = (sy - A * sx) / n
    sd = (sum((b - A * a - B) ** 2 for a, b in pairs) / n) ** 0.5
    return A, B, sd


def abc_pins(path):
    """Gate -> {pin: (PD, LD)} from the PIN lines of an ABC-written genlib."""
    out, cur = {}, None
    for l in open(path):
        f = l.split()
        if f and f[0] == 'GATE':
            cur = out.setdefault(f[1], {})
        elif f and f[0] == 'PIN' and cur is not None and len(f) >= 7:
            cur[f[1]] = (float(f[5]), float(f[6]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--lib", required=True)
    ap.add_argument("--report-fit", action="store_true",
                    help="print the fitted calibration and its residuals")
    ap.add_argument("--abc", default=DEF_ABC)
    ap.add_argument("--exclude", default="",
                    help="read_lib -X pattern; keeps the cell set identical to "
                         "the unit-delay genlib (default: none)")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="timedgenlib")
    timed = os.path.join(tmp, "timed.genlib")   # ABC's -l output
    plain = os.path.join(tmp, "plain.genlib")   # the full cell set
    slew_ns = None
    for flags, dest in ((["-v", "-l"], timed), ([], plain)):
        cmd = "read_lib %s %s %s; write_genlib %s" % (
            " ".join(flags), "-X " + a.exclude if a.exclude else "", a.lib,
            dest)
        r = subprocess.run([a.abc, "-q", cmd], capture_output=True, text=True)
        if not os.path.exists(dest):
            sys.exit("read_lib failed:\n%s\n%s" % (r.stdout, r.stderr))
        m = re.search(r'at slew ([0-9.]+) ps', r.stdout + r.stderr)
        if m:
            slew_ns = float(m.group(1)) / 1000.0
    if slew_ns is None:
        sys.exit("read_lib -l did not report its reference slew")

    txt = open(a.lib, errors='ignore').read()
    starts = cell_starts(txt)
    ts, cs = units(txt)
    have = {l.split()[1] for l in open(timed) if l.startswith('GATE')}

    # Calibrate the local extraction onto ABC's scale over every pin-arc of
    # the gates ABC did time.
    pd_pairs, ld_pairs = [], []
    plain_out = {}
    for L in open(plain):
        m = re.match(r'GATE\s+(\S+)\s+\S+\s+(\w+)=', L)
        if m:
            plain_out.setdefault(m.group(1), m.group(2))
    for nm, pins in abc_pins(timed).items():
        if nm not in plain_out or not pins:
            continue
        for pin, (pd, ld) in arcs(txt, starts, nm, plain_out[nm],
                                  slew_ns, ts, cs).items():
            if pin in pins:
                pd_pairs.append((pd, pins[pin][0]))
                ld_pairs.append((ld, pins[pin][1]))
    if len(pd_pairs) < 2:
        sys.exit("too few timed pin-arcs to calibrate against")
    PD_A, PD_B, pd_sd = linfit(pd_pairs)
    LD_A, LD_B, ld_sd = linfit(ld_pairs)
    if a.report_fit:
        print("slew %.2f ps; %d pin-arcs" % (slew_ns * 1000, len(pd_pairs)))
        print("PD_abc = %.4f * PD_local + %.4f  (residual sd %.2f)"
              % (PD_A, PD_B, pd_sd))
        print("LD_abc = %.4f * LD_local + %.4f  (residual sd %.2f)"
              % (LD_A, LD_B, ld_sd))

    added, skipped, out = [], [], []
    for L in (l.rstrip('\n') for l in open(plain) if l.startswith('GATE')):
        m = re.match(r'GATE\s+(\S+)\s+(\S+)\s+(\w+)=(.*?);', L)
        if not m:
            continue
        nm, area, outpin, expr = m.groups()
        if nm in have:
            continue
        per = arcs(txt, starts, nm, outpin, slew_ns, ts, cs)
        if not per:
            skipped.append("%s:%s" % (nm, outpin)); continue
        lines = ["GATE %-40s %s  %s=%s;" % (nm, area, outpin, expr)]
        for pin, (pd, ld) in per.items():
            PD, LD = PD_A * pd + PD_B, LD_A * ld + LD_B
            lines.append("    PIN %4s  UNKNOWN   0 999 %8.2f %8.2f %8.2f %8.2f"
                         % (pin, PD, LD, PD, LD))
        out.append("\n".join(lines))
        added.append("%s:%s" % (nm, outpin))

    with open(a.out, 'w') as fh:
        fh.write("# Timed genlib: ABC `read_lib -l` (%d gates, real NLDM delays)\n"
                 "# + %d gate/output pairs ABC's -l path drops (>6 inputs, and\n"
                 "# multi-output cells), extracted from the NLDM and calibrated\n"
                 "# onto ABC's scale.  Generated by scripts/gen_timed_genlib.py.\n"
                 % (len(have), len(added)))
        for l in open(timed):
            if not l.startswith('#'):
                fh.write(l)
        fh.write("\n".join(out) + "\n")

    print("%s: %d ABC-timed gates + %d appended = %d gate/output pairs"
          % (a.out, len(have), len(added), len(have) + len(added)))
    if skipped:
        print("SKIPPED (no timing arc found):", skipped)


if __name__ == '__main__':
    main()
