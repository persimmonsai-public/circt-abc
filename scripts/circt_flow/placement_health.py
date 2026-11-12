"""Legalization guard: did the P&R flow's detailed placement legalize every cell?

    python3 -m circt_flow.placement_health LOG_OR_DIR... [--json OUT.json]

Exit status 0 when the last legalization of the flow succeeded, 1 when it failed
(or when no legalization was found and --require is given), 2 when no log was
found. Standard library only.

Why. A detailed placement that cannot legalize every cell reports it in the
log (`Total Placement Failures: N`, DPL-0034), but a P&R command that
continues after the error leaves nothing in its exit status; the design then
goes on to CTS, routing and every measurement with overlapping or unaligned
cells. This reads the logs and says it.

What is read, per log, in order:

* `Total Placement Failures: N` (the movement summary of OpenROAD's
  diamond search), one per legalization attempt, else
  `Detailed placement failed on the following N instances` (DPL-0034)
* in a log with neither: the `Placement Analysis` table (`legalized HPWL
  ... u`) detailed placement prints after a legalization, an attempt that
  left nothing unlegalized

Stock ORFS stops when detailed placement fails, so on the ORFS stage the
check mainly confirms that legalization ran and reported no failure; on a
pack's own P&R command it reports what the logs say, whatever the command's
exit status.

Only the last attempt in the logs counts as the final result. A directory
argument stands for ORFS's stage logs under it (`work/logs/<platform>/<design>/
<variant>/*.log`, the layout the ORFS stage writes), each variant directory a
run of its own, and for the `*.log` files of its `logs/` subdirectory, one
more run; each in name order (ORFS's numbered log names give flow order). Each
run's last attempt is its result; a failure in any run counts (the final
result is the worst run's).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_TPF = re.compile(r"Total Placement Failures:\s+(\d+)")
_D34 = re.compile(r"Detailed placement failed on the following (\d+) instances")
_HPWL = re.compile(r"^legalized HPWL\s+[-\d.]+\s*u\b")


def parse_log(text: str) -> dict:
    """Legalization attempts of one log, in order, each as its count of
    cells left unlegalized: one per `Total Placement Failures: N` line
    (diamond search's movement summary), else one per DPL-0034 line. A log
    with neither shows a clean legalization by the `Placement Analysis`
    table detailed placement prints after it (`legalized HPWL ... u`): one
    attempt with no failure per table."""
    tpf: List[int] = []
    d34: List[int] = []
    tables = 0
    for line in text.splitlines():
        m = _TPF.search(line)
        if m:
            tpf.append(int(m.group(1)))
            continue
        m = _D34.search(line)
        if m:
            d34.append(int(m.group(1)))
            continue
        if _HPWL.match(line.strip()):
            tables += 1
    attempts = tpf if tpf else d34 if d34 else [0] * tables
    return {"attempts": attempts, "tpf": tpf, "dpl0034": d34}


def runs(paths: List[str], patterns: Optional[List[str]] = None,
         extra: Sequence[Path] = ()) -> List[List[Path]]:
    """The logs to read, as runs (each a list of files in flow order). A
    directory without `patterns` brings each ORFS variant directory below
    `work/logs/` as a run of its own, and its `logs/*.log`, with the files in
    `extra` after them, as one more run. With `patterns` (glob patterns
    relative to the directory, e.g. a pack's `pnr.placement_logs`) the files
    they match, pattern by pattern, form the directory's one run, and `extra`
    is not read. A file argument is a run of its own."""
    out: List[List[Path]] = []
    for a in paths:
        p = Path(a)
        if not p.is_dir():
            out.append([p])
            continue
        if patterns is not None:
            run: List[Path] = []
            for pat in patterns:
                for f in sorted(p.glob(pat), key=lambda f: f.name):
                    if f.is_file() and f not in run:
                        run.append(f)
            if run:
                out.append(run)
            continue
        for v in sorted({f.parent for f in
                         (p / "work" / "logs").glob("*/*/*/*.log")}):
            out.append(sorted(v.glob("*.log"), key=lambda f: f.name))
        run = sorted((p / "logs").glob("*.log"), key=lambda f: f.name)
        run += [Path(f) for f in extra if Path(f).exists()]
        if run:
            out.append(run)
    return out


def collect(paths: List[str]) -> List[Path]:
    """Every file runs() reads, run after run."""
    return [f for r in runs(paths) for f in r]


def check(files: List[Path]) -> dict:
    """One run's logs (in flow order): its last attempt is its result."""
    stages: List[dict] = []
    for f in files:
        r = parse_log(f.read_text(errors="replace"))
        r["log"] = str(f)
        r["stage"] = f.stem
        r["failed_cells_max"] = max(r["attempts"], default=0)
        stages.append(r)
    pool = [s for s in stages if s["attempts"]]
    final: Optional[int] = pool[-1]["attempts"][-1] if pool else None
    return {"stages": stages, "final_failures": final,
            "final_stage": pool[-1]["stage"] if pool else None,
            "max_failures": max((s["failed_cells_max"] for s in stages), default=0),
            "ok": final == 0 if final is not None else None}


def check_runs(rs: List[List[Path]]) -> dict:
    """Several runs: each judged on its own last attempt; the final result is
    the worst run's (a failure in any run counts), `stages` all of them."""
    res = [check(r) for r in rs]
    finals = [r["final_failures"] for r in res if r["final_failures"] is not None]
    final = max(finals) if finals else None
    worst = next((r for r in res if r["final_failures"] == final), None)
    return {"stages": [s for r in res for s in r["stages"]],
            "runs": [{"logs": [str(f) for f in rf], "final_failures": r["final_failures"]}
                     for rf, r in zip(rs, res)],
            "final_failures": final,
            "final_stage": worst["final_stage"] if worst else None,
            "max_failures": max((r["max_failures"] for r in res), default=0),
            "ok": final == 0 if final is not None else None}


def report(res: dict) -> str:
    rows = ["log                    attempts  failures-per-attempt"]
    for s in res["stages"]:
        rows.append(f"{s['stage']:<22} {len(s['attempts']):>8}  "
                    f"{','.join(map(str, s['attempts'])) or '-'}")
    if res["final_failures"] is None:
        rows.append("final: no legalization attempt found in the logs")
    else:
        rows.append(f"final: {res['final_failures']} placement failure(s) in the last legalization "
                    f"({res['final_stage']}); {'OK' if res['ok'] else 'FAILED'}; "
                    f"worst attempt {res['max_failures']}")
    return "\n".join(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="stage logs or a P&R run directory")
    ap.add_argument("--json", help="write the result as JSON")
    ap.add_argument("--require", action="store_true",
                    help="also exit 1 when no legalization attempt is found")
    a = ap.parse_args(argv)
    rs = runs(a.paths)
    if not rs:
        print("placement_health: no logs found", file=sys.stderr)
        return 2
    res = check_runs(rs)
    print(report(res))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
    if res["ok"] is None:
        return 1 if a.require else 0
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
