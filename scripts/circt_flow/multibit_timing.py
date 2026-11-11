"""Timing-driven multibit family overrides from a finished flow run.

    python -m circt_flow.multibit_timing <out_dir> [--margin 0.05]
        [--family single] [--mode func] [--corner tt] [-o overrides.json]

A multibit cell's clock-to-Q and setup can differ from the single-bit flop's,
so a bank on a near-critical path can cost slack that the Vt swap cannot
always win back (a family may lack the fastest Vt). This reads the run's own OpenSTA view (sta/runs/top.<mode>.<corner>.tcl:
libraries, netlist and SDC), reports every path with slack below `--margin`
(ns) and writes a multibit `overrides:` list (see multibit.py) that sends
each register at a start or end of such a path to `--family`: a family with
no classes in the pack's spec keeps its bits single-bit (declare e.g.
`families: {single: {}}`), or name a faster multibit family.

Register names are module-local, as the register-mapper sees them: the
instance name's last hierarchy segment (`/` is `2F` in the netlist) without
its `_bitN` / `_mbA_B[_xK]` suffix. A name used in several instances of a
module applies to all of them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .sta import ORFS_IMAGE, STA_BIN

INST = re.compile(r"^(?P<h>.*?)(?:_mb\d+_\d+(?:_x\d+)?|_bit\d+)$")


def local_register(inst: str):
    """Module-local register name of a flop instance, or None."""
    m = INST.match(inst)
    if not m:
        return None
    return re.split(r"2F", m.group("h"))[-1] or None


def parse_paths(log: str) -> set:
    names = set()
    for m in re.finditer(r"^(?:Startpoint|Endpoint): (\S+)", log, re.M):
        pin = m.group(1)
        reg = local_register(pin.split("/")[0])
        if reg:
            names.add(reg)
    return names


def critical_registers(out_dir: Path, margin: float, mode="func",
                       corner="tt", image=ORFS_IMAGE) -> set:
    tcl = Path(out_dir) / "sta" / "runs" / f"top.{mode}.{corner}.tcl"
    head = []
    for line in tcl.read_text().splitlines():
        head.append(line)
        if line.startswith("read_sdc"):
            break
    work = Path(out_dir) / "sta" / "mbt"
    work.mkdir(exist_ok=True)
    script = work / "crit.tcl"
    script.write_text("\n".join(head) + f"""
report_checks -path_delay max -slack_max {margin} -group_path_count 100000 -endpoint_path_count 1 -format short
exit
""")
    # The short format has no Startpoint/Endpoint lines for every OpenSTA
    # version; the full format does.
    script.write_text(script.read_text().replace("-format short", "-format full"))
    log = work / "crit.log"
    local = os.environ.get("OPENSTA")
    if local:
        cmd = [local, "-no_init", "-no_splash", "-exit", str(script)]
    else:
        mounts = {str(Path(p).resolve().parent) for p in re.findall(
            r"^(?:read_liberty|read_verilog|read_sdc)\s+(\S+)", "\n".join(head),
            re.M)} | {str(work.resolve())}
        ms = sorted(mounts)
        ms = [m for m in ms if not any(m != o and m.startswith(o + "/")
                                       for o in ms)]
        cmd = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
               "-e", "HOME=/tmp",
               *[a for m in ms for a in ("-v", f"{m}:{m}")],
               image, STA_BIN, "-no_init", "-no_splash", "-exit",
               str(script.resolve())]
    with open(log, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=True)
    return parse_paths(log.read_text())


def overrides(names, family: str) -> list:
    return [{"reg": "^" + re.escape(n) + "$", "family": family}
            for n in sorted(names)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir")
    ap.add_argument("--margin", type=float, default=0.05)
    ap.add_argument("--family", default="single")
    ap.add_argument("--mode", default="func")
    ap.add_argument("--corner", default="tt")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    names = critical_registers(Path(a.out_dir), a.margin, a.mode, a.corner)
    ov = overrides(names, a.family)
    text = json.dumps(ov, indent=1) + "\n"
    if a.output:
        Path(a.output).write_text(text)
    else:
        sys.stdout.write(text)
    print(f"multibit_timing: {len(ov)} register(s) within {a.margin} ns of "
          f"critical -> family {a.family}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
