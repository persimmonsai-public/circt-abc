"""Restore the pin-node ground capacitance missing from a global-route SPEF.

    python3 -m circt_flow.spef_pincap check IN.spef
    python3 -m circt_flow.spef_pincap restore IN.spef \\
        [--est-rc-log LOG --layer-rc RC.tcl]      # in place, original -> .orig
    python3 -m circt_flow.spef_pincap fix IN.spef OUT.spef \\
        [--est-rc-log LOG --layer-rc RC.tcl] [--report nets.tsv]

In the SPEF that OpenROAD's `estimate_parasitics -global_routing -spef_file`
writes, the *CAP section of a net has entries for its internal nodes only:
the capacitance the estimate puts on pin nodes (half of each pin's stub to
its gcell centre) is not in it, while the net's *D_NET total includes it. A
reader that builds the RC network (OpenSTA `read_spef`)
sees less wire cap than OpenROAD's database and times optimistically; how
much depends on the design and the gcell size. `-placement` mode puts no cap
on pins and loses nothing. A reader that takes the *D_NET total as the net's
capacitance is not affected.

`fix` writes, per net, missing = total - sum(ground *CAP) as extra *CAP
entries on the net's own pin nodes (legal SPEF), so the CAP lines sum to the
header total. Without weights the missing cap is split equally between the
pins; with the estimator's debug log (`set_debug_level EST est_rc 1` around
`estimate_parasitics -global_routing`) and the per-layer cap file, each pin
gets length x layer cap of its stub, scaled so the net sum is exact. Nothing
else in the file changes, and a fixed file passes through unchanged.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, TextIO, Tuple

_PIN_DBG = re.compile(
    r"\[DEBUG EST-est_rc\]\s+(\S+)\s+->\s+(\S+)\s+\(\s*[-\d.]+,\s*[-\d.]+\)\s+"
    r"([\d.]+)u\s+layer=(\d+)\s+r=\S+\s+via_res=\S+\s+c=([-\d.eE+]+)")
_SOURCE = re.compile(r"\s*source\s+\{?([^\s}]+)\}?\s*$")
_LAYER_RC = re.compile(
    r"set_layer_rc\s+-layer\s+\S+\s+-resistance\s+\S+\s+-capacitance\s+(\S+)")


def _spef_pin(name: str) -> str:
    """OpenSTA pin path inst/pin -> SPEF inst:pin, with $ escaped as the
    writer does."""
    i = name.rfind("/")
    if i >= 0:
        name = name[:i] + ":" + name[i + 1:]
    return name.replace("$", "\\$")


def fingerprint(path: Path) -> Tuple[int, str]:
    """(size, CRC-32 as 8 hex digits) of a file: what spef_export.tcl
    records for the SPEF it has just written (Tcl's `zlib crc32` is the same
    CRC-32 as Python's)."""
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return Path(path).stat().st_size, "%08x" % crc


def export_segments(log: Path) -> List[Tuple[str, List[str], Optional[Tuple[int, str]]]]:
    """The export segments of a stdout log, in order: (path as written by
    spef_export.tcl, the lines between `SPEF_EXPORT_BEGIN <path>` and
    `SPEF_EXPORT_END <path>`, (size, crc) from the `SPEF_EXPORT_FILE <size>
    <crc> <path>` line that follows, or None without one). Only whole marker
    lines count."""
    segs: list = []
    cur: Optional[List[str]] = None
    path = None
    with open(log, errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if cur is None:
                if line.startswith("SPEF_EXPORT_BEGIN "):
                    path, cur = line[len("SPEF_EXPORT_BEGIN "):], []
                elif line.startswith("SPEF_EXPORT_FILE ") and segs:
                    w = line.split(" ", 3)
                    if (len(w) == 4 and w[3] == segs[-1][0]
                            and segs[-1][2] is None and w[1].isdigit()
                            and re.fullmatch(r"[0-9a-f]{8}", w[2])):
                        segs[-1] = (segs[-1][0], segs[-1][1], (int(w[1]), w[2]))
            elif line == "SPEF_EXPORT_END " + path:
                segs.append((path, cur, None))
                cur = None
            else:
                cur.append(line + "\n")
    return segs


def names_file(marker: str, spef: Path, root: Optional[Path] = None) -> bool:
    """Whether the path in a marker is `spef`. An absolute path that exists
    here must resolve to the same file. Otherwise (the export ran where the
    directory is mounted under another path, e.g. in a container) the
    marker's path must end with `spef`'s path below `root` (its file name
    when no root is given)."""
    spef = Path(spef)
    m = Path(marker)
    if m.is_absolute() and m.exists():
        return m.resolve() == spef.resolve()
    try:
        tail = (spef.resolve().relative_to(Path(root).resolve()).parts
                if root is not None else (spef.name,))
    except ValueError:
        return False
    return len(m.parts) >= len(tail) and m.parts[-len(tail):] == tail


def est_rc_lines(log: Path, spef: Path, root: Optional[Path] = None
                 ) -> Optional[List[str]]:
    """The estimator debug lines of `spef` in a stdout log: the lines of its
    last export segment (spef_export.tcl's markers, see names_file), or None
    when the log has no segment for it."""
    seg = None
    for path, lines, _fp in export_segments(log):
        if names_file(path, spef, root):
            seg = lines
    return seg


def weights_from_log(log: Path, spef: Path,
                     caps: Optional[Dict[int, float]] = None
                     ) -> Dict[str, float]:
    """stub_weights of `log`: the marked segment of `spef` when the log
    has one, else every est_rc line in it."""
    seg = est_rc_lines(log, spef)
    if seg is not None:
        return stub_weights(seg, caps)
    with open(log, errors="replace") as f:
        return stub_weights(f, caps)


def layer_caps(path: Path) -> Dict[int, float]:
    """Routing level (1..N, in file order) -> cap per micron, from a Tcl file
    of `set_layer_rc -layer L -resistance R -capacitance C` lines."""
    caps: Dict[int, float] = {}
    for line in _rc_lines(Path(path)):
        m = _LAYER_RC.search(line)
        if m:
            try:
                caps[len(caps) + 1] = float(m.group(1))
            except ValueError:      # an expression: leave the layer out
                pass
    return caps


def _rc_lines(path: Path, depth: int = 0) -> List[str]:
    """Lines of an RC Tcl file, following `source {file}` one level (the
    flow's wire_rc wrapper sources the pack's file)."""
    out: List[str] = []
    for line in path.read_text().splitlines():
        m = _SOURCE.match(line)
        if m and depth < 2 and Path(m.group(1)).is_file():
            out += _rc_lines(Path(m.group(1)), depth + 1)
        else:
            out.append(line)
    return out


def stub_weights(lines: Iterable[str],
                 caps: Optional[Dict[int, float]] = None) -> Dict[str, float]:
    """Pin (SPEF spelling) -> half its stub cap, from est_rc debug lines.
    The printed c= is rounded to the report digits (often 0.000), so length x
    layer cap is used when the layer is in `caps`."""
    w: Dict[str, float] = {}
    for line in lines:
        if "via_res=" not in line or "] pin " in line:
            continue
        m = _PIN_DBG.search(line)
        if not m:
            continue
        length, level, c = float(m.group(3)), int(m.group(4)), float(m.group(5))
        if caps and level in caps:
            v = length * caps[level] / 2.0
        else:
            v = c / 2.0 if c > 0 else length / 2.0
        k = _spef_pin(m.group(2))
        w[k] = w.get(k, 0.0) + v
    return w


def _blocks(f: TextIO):
    """Yield ('text', str) for lines outside nets and ('net', [lines])."""
    blk: Optional[List[str]] = None
    for line in f:
        if blk is None:
            if line.startswith("*D_NET"):
                blk = [line]
            else:
                yield "text", line
            continue
        blk.append(line)
        if line.startswith("*END"):
            yield "net", blk
            blk = None
    if blk is not None:
        yield "net", blk


def _scan(blk: List[str]):
    """total, cap sum (ground plus coupling entries, as the *D_NET total
    counts both), cap entry count, pin names, index after the last *CAP entry
    (or None), index of *RES (or None), index of *END. The number of
    coupling entries is `_scan.coupled` of the last call."""
    total = float(blk[0].split()[2])
    sec = None
    capsum, ncap, pins, ncoup = 0.0, 0, [], 0
    cap_end = res_i = end_i = None
    for i, line in enumerate(blk):
        if line.startswith("*CONN"):
            sec = "conn"
        elif line.startswith("*CAP"):
            sec = "cap"
            cap_end = i + 1
        elif line.startswith("*RES"):
            sec, res_i = "res", i
        elif line.startswith("*END"):
            sec, end_i = None, i
        elif sec == "conn" and line.startswith(("*I ", "*P ")):
            pins.append(line.split()[1])
        elif sec == "cap" and line.strip():
            t = line.split()
            ncap += 1
            cap_end = i + 1
            if len(t) == 3:
                capsum += float(t[2])
            elif len(t) == 4:          # coupling cap: idx node node value
                capsum += float(t[3])
                ncoup += 1
    _scan.coupled = ncoup
    _scan.ncap = ncap
    return total, capsum, ncap, pins, cap_end, res_i, end_i


def check(path: Path) -> Dict[str, float]:
    """Sum of *D_NET totals and of *CAP entries (ground and coupling, SPEF C
    units); `coupled`: nets with a coupling entry."""
    nets, total, capsum, short, coupled, entries = 0, 0.0, 0.0, 0, 0, 0
    with open(path) as f:
        for kind, b in _blocks(f):
            if kind != "net":
                continue
            t, c, *_ = _scan(b)
            nets += 1
            total += t
            capsum += c
            coupled += _scan.coupled > 0
            short += (t - c) > max(1e-9, 1e-6 * t)
            entries += _scan.ncap
    return {"nets": nets, "total": total, "capsum": capsum,
            "missing": total - capsum, "nets_short": short,
            "nets_coupled": coupled, "cap_entries": entries}


def fix(src: Path, dst: Path, weights: Optional[Dict[str, float]] = None,
        report: Optional[Path] = None) -> Dict[str, float]:
    """Write `dst` = `src` with each net's missing cap on its pin nodes."""
    weights = weights or {}
    st = {"nets": 0, "fixed": 0, "equal_split": 0, "restored": 0.0}
    rep = open(report, "w") if report else None
    if rep:
        rep.write("net\ttotal\tcapsum\tmissing\tnpins\tweight_sum\tmode\n")
    with open(src) as f, open(dst, "w") as out:
        for kind, b in _blocks(f):
            if kind == "text":
                out.write(b)
                continue
            st["nets"] += 1
            total, capsum, ncap, pins, cap_end, res_i, end_i = _scan(b)
            miss = total - capsum
            if miss <= max(1e-9, 1e-6 * total) or not pins:
                out.write("".join(b))
                continue
            ws = [weights.get(p, 0.0) for p in pins]
            mode = "weighted"
            if sum(ws) <= 0:
                ws, mode = [1.0] * len(pins), "equal"
                st["equal_split"] += 1
            sw = sum(ws)
            new, k, given = [], ncap, 0.0
            last = max(i for i, wv in enumerate(ws) if wv > 0)
            for i, (p, wv) in enumerate(zip(pins, ws)):
                if wv <= 0:
                    continue
                # 6 digits like OpenROAD; the last pin takes the remainder so
                # the CAP lines sum to the header total
                v = (float("%.6g" % (miss * wv / sw)) if i < last
                     else miss - given)
                given += v
                k += 1
                new.append("%d %s %.6g\n" % (k, p, v))
            if cap_end is not None:
                b = b[:cap_end] + new + b[cap_end:]
            else:
                at = res_i if res_i is not None else end_i
                b = b[:at] + ["*CAP\n"] + new + b[at:]
            out.write("".join(b))
            st["fixed"] += 1
            st["restored"] += miss
            if rep:
                rep.write("%s\t%.6g\t%.6g\t%.6g\t%d\t%.6g\t%s\n" % (
                    b[0].split()[1], total, capsum, miss, len(pins), sw, mode))
    if rep:
        rep.close()
    return st


def restore(spef: Path, est_rc_log: Optional[Path] = None,
            layer_rc: Optional[Path] = None, keep: bool = True,
            report: Optional[Path] = None,
            est_rc: Optional[List[str]] = None) -> Dict[str, float]:
    """Restore `spef` in place (the P&R export step). The original goes to
    `<spef>.orig` when `keep`. `est_rc_log` is the export's stdout log (the
    segment between spef_export.tcl's markers is used) or a plain debug log;
    `est_rc`, when given, is that segment's lines instead. Raises if the result is not whole (every net's
    *CAP lines must sum to its *D_NET total). A SPEF with nothing missing is
    left alone (placement exports, or an already restored file)."""
    spef = Path(spef)
    before = check(spef)
    # A SPEF with coupling capacitors is an extractor's (detailed-route)
    # file, not the global-route estimate this restores: left alone.
    ratio = before["capsum"] / before["total"] if before["total"] else 1.0
    if before["nets_coupled"] or (
            before["missing"] <= max(1e-9, 1e-6 * before["total"])
            and not before["nets_short"]):
        return {"nets": before["nets"], "fixed": 0, "equal_split": 0,
                "restored": 0.0, "ratio_before": ratio, "ratio_after": ratio,
                "nets_coupled": before["nets_coupled"]}
    w = None
    caps = layer_caps(layer_rc) if layer_rc and Path(layer_rc).exists() \
        else None
    if est_rc is not None:
        w = stub_weights(est_rc, caps)
    elif est_rc_log and Path(est_rc_log).exists():
        w = weights_from_log(Path(est_rc_log), spef, caps)
    tmp = spef.with_name(spef.name + ".tmp")
    st = fix(spef, tmp, w, report)
    after = check(tmp)
    if after["nets_short"] or abs(after["capsum"] - after["total"]) > \
            1e-6 * after["total"]:
        tmp.unlink()
        raise RuntimeError(f"{spef}: restore left {after['nets_short']} "
                           "net(s) short")
    orig = spef.with_name(spef.name + ".orig")
    if keep:
        # The original is kept under .orig first (a hard link, or a copy
        # where links are not possible) while the SPEF keeps its name; the
        # restored file then takes the name in one rename, so the SPEF's
        # name never points at nothing.
        side = spef.with_name(spef.name + ".orig.tmp")
        if side.exists() or side.is_symlink():
            side.unlink()
        try:
            try:
                os.link(spef, side)
            except OSError:
                shutil.copy2(spef, side)
            os.replace(side, orig)
        finally:
            if side.exists():
                side.unlink()
    try:
        os.replace(tmp, spef)
    except OSError:
        if keep:
            orig.unlink()
        raise
    st["ratio_before"] = before["capsum"] / before["total"]
    st["ratio_after"] = after["capsum"] / after["total"]
    return st


def restore_exports(out_dir: Path, layer_rc: Optional[Path] = None
                    ) -> Dict[str, Dict[str, float]]:
    """Restore, in place, the global-route SPEFs the export helper wrote
    below `out_dir` (spef_export.tcl's `spef_export_global_route`). A SPEF
    is rewritten only when a `*.log` below `out_dir` holds the helper's
    export segment for it: whole `SPEF_EXPORT_BEGIN/END <path>` lines whose
    path is this file (names_file), followed by the `SPEF_EXPORT_FILE
    <size> <crc> <path>` line the helper writes after the estimate, whose
    size and CRC-32 equal the file's as it is now. A file another tool
    wrote, or rewrote after the export, has other bytes and is not touched.
    It must also have `*CAP` lines that fall short of its `*D_NET` totals,
    `*CAP` entries and no coupling capacitors, and parse completely. The
    per-pin weights come from that segment; without estimator lines in it
    the missing cap is split equally between a net's pins (the same total
    per net, not the estimator's own split). Every other SPEF, and one that
    cannot be read or parsed, is left as written with a one-line report;
    this never raises for a file. Returns {path: stats} of the files
    rewritten."""
    out_dir = Path(out_dir)
    segs = []
    for lg in sorted(p for p in out_dir.rglob("*.log") if p.is_file()):
        try:
            segs += [(lg, s) for s in export_segments(lg)]
        except OSError:
            continue
    res = {}
    for spef in sorted(out_dir.rglob("*.spef")):
        if not spef.is_file():
            continue
        try:
            c = check(spef)
        except (OSError, ValueError, IndexError) as e:
            print(f"[spef_pincap] {spef.name}: left as written (could not be "
                  f"read: {type(e).__name__}: {e})")
            continue
        if c["missing"] <= max(1e-9, 1e-6 * c["total"]) and not c["nets_short"]:
            continue
        mine = [(lg, s) for lg, s in segs if names_file(s[0], spef, out_dir)]
        src = lines = None
        why = None
        if not mine:
            why = ("not written by spef_export_global_route (no export "
                   "marker for its path in a log)")
        else:
            fp = fingerprint(spef)
            for lg, (_p, seg, rec) in mine:
                if rec == fp:
                    src, lines = lg, seg
            if src is None:
                why = ("its size or CRC-32 differs from the one the export "
                       "recorded: written or changed after the export")
        if why is None and c.get("nets_coupled"):
            why = (f"coupling capacitors on {c['nets_coupled']} net(s); "
                   f"*CAP/*D_NET {c['capsum'] / c['total']:.4f}")
        if why is None and not c.get("cap_entries"):
            why = "no *CAP entries"
        if why:
            print(f"[spef_pincap] {spef.name}: left as written ({why})")
            continue
        try:
            st = restore(spef, None, layer_rc, est_rc=lines)
        except (OSError, ValueError, IndexError, RuntimeError) as e:
            tmp = spef.with_name(spef.name + ".tmp")
            if tmp.exists():
                tmp.unlink()
            print(f"[spef_pincap] {spef.name}: left as written (could not be "
                  f"restored: {type(e).__name__}: {e})")
            continue
        res[str(spef)] = st
        print(f"[spef_pincap] {spef.name}: *CAP/*D_NET {st['ratio_before']:.4f}"
              f" -> {st['ratio_after']:.4f} on {st['fixed']} nets, "
              + (f"weights from {src.name}" if st["equal_split"] < st["fixed"]
                 else "no estimator lines: equal split between pins"))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("spef", type=Path)
    x = sub.add_parser("fix")
    x.add_argument("spef", type=Path)
    x.add_argument("out", type=Path)
    x.add_argument("--est-rc-log", type=Path)
    x.add_argument("--layer-rc", type=Path)
    x.add_argument("--report", type=Path)
    r_ = sub.add_parser("restore", help="fix in place, original kept as .orig")
    r_.add_argument("spef", type=Path)
    r_.add_argument("--est-rc-log", type=Path)
    r_.add_argument("--layer-rc", type=Path)
    r_.add_argument("--report", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "restore":
        st = restore(a.spef, a.est_rc_log, a.layer_rc, report=a.report)
        print("nets %(nets)d  fixed %(fixed)d  equal-split %(equal_split)d"
              "  *CAP/*D_NET %(ratio_before).4f -> %(ratio_after).4f" % st)
        return 0
    if a.cmd == "check":
        r = check(a.spef)
        print("nets %(nets)d  D_NET total %(total).6g  ground *CAP %(capsum).6g"
              "  missing %(missing).6g  nets short %(nets_short)d" % r)
        return 0
    w = None
    if a.est_rc_log:
        caps = layer_caps(a.layer_rc) if a.layer_rc else None
        w = weights_from_log(a.est_rc_log, a.spef, caps)
    r = fix(a.spef, a.out, w, a.report)
    print("nets %(nets)d  fixed %(fixed)d  equal-split %(equal_split)d"
          "  restored %(restored).6g" % r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
