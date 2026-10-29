"""Partitioned abc-bridge: map K residual groups in parallel, splice back.

One whole-design ABC call stops being practical well before the design does.
On OpenC910 the merged residual logic is a 6,225,732-AND-gate AIGER (about
187x ibex's) and a single call had not finished after 20 minutes; split into
64 groups it maps in ~10 minutes on a 12-way pool, with the slowest group
only 9% of total work.

Shape of the flow:

  1. `merge-residual-modules group-count=K group-index=i`, K pass instances in
     one circt-opt invocation, produces K `abc_bridge_merged_residual_gN`
     modules instead of one merged module.
  2. One `abc-bridge target=...gN emit-target-only=true` process per group,
     run over a pool. `emit-target-only` matters at scale: without it each
     worker writes the whole design (461 MB on C910) when the splicer reads
     back only its one module (56 MB).
  3. `splice()` puts the mapped bodies back into the grouped file, carries
     each worker's `_bits` companion along with its module, and unions the
     extern cell declarations.

Processes, never threads: ABC's frame is a process-wide static singleton
(`s_GlobalFrame`, base/main/mainFrame.c), so one ABC session per process is
the hard limit. A worker dying also costs one group rather than the run.

The partitioned netlist is NOT byte-identical to the unpartitioned one -- ABC
sees a different optimisation scope per group, and on ibex that measured
as a small area loss. What IS checkable, and what `verify_against_serial`
checks, is that running the same groups serially in one process gives the
identical result: that isolates the parallelism from the partitioning.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# circt-opt prints top-level modules at two-space indent. The optional
# visibility keyword is load-bearing: abc-bridge emits a
# `hw.module private @<target>_bits` companion holding the bit-blasted port
# form (AIGER round-trips bit-level ports only), and a regex without it
# silently drops every one of those helpers, producing a spliced file that
# fails verification with "Cannot find module definition".
_MODULE_RE = re.compile(
    r"^  hw\.module(?: private| public| nested)? @([A-Za-z0-9_.$]+)\(", re.M)
_EXTERN_RE = re.compile(
    r"^  hw\.module\.extern @([A-Za-z0-9_.$]+)\(.*$", re.M)

GROUP_PREFIX = "abc_bridge_merged_residual_g"


class PartitionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Textual MLIR splicing. Deliberately textual: this only ever moves whole
# top-level module blocks, and re-parsing a 300 MB module to do that would
# cost more than the mapping it is stitching together.
# ---------------------------------------------------------------------------


def module_blocks(text: str) -> Dict[str, tuple]:
    """name -> (start, end) offsets of each top-level `hw.module` block."""
    out = {}
    for m in _MODULE_RE.finditer(text):
        name = m.group(1)
        end = text.find("\n  }\n", m.start())
        if end < 0:
            raise PartitionError(f"unterminated hw.module @{name}")
        out[name] = (m.start(), end + len("\n  }\n"))
    return out


def group_names(text: str) -> List[str]:
    """Every `abc_bridge_merged_residual_gN` module, in numeric order."""
    names = [n for n in module_blocks(text) if n.startswith(GROUP_PREFIX)]
    return sorted(names, key=lambda n: int(n[len(GROUP_PREFIX):]))


def splice(base_text: str, parts: Dict[str, str]) -> str:
    """Replace each named module block in `base_text` with the worker's mapped
    block, pull in each worker's added helper modules, and union in the
    workers' new `hw.module.extern` cell declarations."""
    blocks = module_blocks(base_text)
    base_names = set(blocks)
    out = base_text
    extra: List[str] = []
    # Highest offset first, so earlier offsets stay valid as we substitute.
    for name in sorted(parts, key=lambda n: blocks[n][0], reverse=True):
        mapped = parts[name]
        mb = module_blocks(mapped)
        if name not in mb:
            raise PartitionError(f"worker output has no hw.module @{name}")
        # Anything the worker ADDED at module scope (its `_bits` companion)
        # has to travel with the mapped block that instantiates it.
        for other, (s2, e2) in mb.items():
            if other not in base_names and other not in parts:
                extra.append(mapped[s2:e2])
        a, b = blocks[name]
        s, e = mb[name]
        out = out[:a] + mapped[s:e] + out[b:]

    if extra:
        idx = out.rstrip().rfind("\n}")
        out = out[:idx] + "\n" + "".join(extra) + out[idx:]

    have = set(_EXTERN_RE.findall(out))
    new_lines = []
    for mapped in parts.values():
        for m in _EXTERN_RE.finditer(mapped):
            if m.group(1) not in have:
                have.add(m.group(1))
                new_lines.append(m.group(0))
    if new_lines:
        # Externs are order-independent declarations; append them just before
        # the closing brace of the outer builtin.module.
        idx = out.rstrip().rfind("\n}")
        out = out[:idx] + "\n" + "\n".join(new_lines) + out[idx:]
    return out


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class PartResult:
    name: str
    returncode: int
    seconds: float
    output: Optional[Path]
    log: Path


def _map_one(args) -> PartResult:
    (name, grouped, workdir, circt_abc_opt, abc_bridge_opt) = args[:5]
    extra = args[5] if len(args) > 5 else ""
    out = workdir / f"{name}.mlir"
    log = workdir / f"{name}.log"
    cmd = [str(circt_abc_opt), "--mlir-disable-threading",
           f"{abc_bridge_opt} target={name} emit-target-only=true{(' ' + extra) if extra else ''}",
           str(grouped), "-o", str(out)]
    t0 = time.time()
    with open(log, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    return PartResult(name, rc, time.time() - t0,
                      out if rc == 0 else None, log)


def map_groups(grouped: Path, workdir: Path, circt_abc_opt: str,
               abc_bridge_opt: str, pool_size: int,
               per_group_extra: dict | None = None) -> List[PartResult]:
    """Map every group in `grouped` over a process pool. `per_group_extra`
    maps a group name to extra abc-bridge options for that group only
    (PHYS-DP: e.g. `abc-script-file=<depth-preserving body>` for groups
    holding a timing-critical datapath op)."""
    workdir.mkdir(parents=True, exist_ok=True)
    names = group_names(Path(grouped).read_text())
    if not names:
        raise PartitionError(
            f"no {GROUP_PREFIX}* modules in {grouped} -- was "
            f"merge-residual-modules run with group-count>1?")
    per_group_extra = per_group_extra or {}
    jobs = [(n, grouped, workdir, circt_abc_opt, abc_bridge_opt, per_group_extra.get(n, ""))
            for n in names]
    # THREADS, not processes. Each worker does nothing but block in
    # subprocess.run, and the real isolation is already in place: every group
    # gets its own circt-abc-opt process, hence its own ABC session, which is
    # what ABC's process-wide `s_GlobalFrame` singleton actually requires.
    # A multiprocessing pool would add nothing and carries a real hazard --
    # under the "spawn" context it re-imports the caller's __main__, so a
    # driver script without an `if __name__ == "__main__":` guard re-runs its
    # entire flow inside every worker. Observed exactly that: 64 workers each
    # restarting the OpenC910 pipeline from the frontend.
    with cf.ThreadPoolExecutor(max_workers=max(1, pool_size)) as pool:
        return list(pool.map(_map_one, jobs))


def combine(grouped: Path, results: Sequence[PartResult], out_path: Path) -> Path:
    """Splice every successfully mapped group back into one MLIR file."""
    failed = [r for r in results if r.returncode != 0]
    if failed:
        raise PartitionError(
            f"{len(failed)} of {len(results)} groups failed to map; first: "
            f"{failed[0].name} (see {failed[0].log})")
    parts = {r.name: r.output.read_text() for r in results}
    out_path.write_text(splice(Path(grouped).read_text(), parts))
    return out_path


def verify_against_serial(grouped: Path, results: Sequence[PartResult],
                          workdir: Path, circt_abc_opt: str,
                          abc_bridge_opt: str) -> bool:
    """Re-map the same groups serially in one process and compare, which
    isolates the parallelism from the partitioning: the two must agree
    exactly. (Partitioned-vs-unpartitioned is a different question, and they
    legitimately differ -- ABC optimises a different scope.)

    Only meaningful with a DETERMINISTIC recipe. The shipped default carries
    `&deepsyn -T 10`, a CPU-time budget, so two runs of the identical input
    differ purely with machine load and this check reports a difference that
    says nothing about the parallelism. Pass `abc_script_file=` pointing at a
    convergent recipe (`-T 0`) before reading anything into a mismatch."""
    serial = workdir / "serial"
    serial.mkdir(parents=True, exist_ok=True)
    for r in results:
        got = _map_one((r.name, grouped, serial, circt_abc_opt, abc_bridge_opt))
        if got.returncode != 0:
            return False
        if got.output.read_text() != r.output.read_text():
            return False
    return True
