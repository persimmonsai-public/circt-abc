"""Result objects of the session API: what a stage
produced, its key metrics, the commands it ran and the settings it used --
data, with a short text rendering, instead of a log to parse.

Metrics come from the files the stages already write (no extra tool run):
the netlist (instances per cell; area from the setup liberties' `area`), the
fanout stage's fanout.json, vt_swap.json, sta/summary.json and ORFS's
pnr_metrics.json.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StageResult:
    stage: str
    status: str                       # "done" | "skipped"
    produced: Dict[str, str] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    commands: List[list] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)   # explicit options used
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in ("done", "skipped")

    def text(self) -> str:
        m = self.metrics
        bits = [f"{self.stage}: {self.status} in {self.seconds:.1f} s"]
        for k in ("registers", "modules", "cells", "area", "buffers", "wns_ns", "tns_ns",
                  "hold_wns_ns"):
            if k in m and m[k] is not None:
                v = m[k]
                bits.append(f"{k} {v:.4g}" if isinstance(v, float) else f"{k} {v}")
        if self.produced:
            bits.append("-> " + ", ".join(f"{k} {v}" for k, v in self.produced.items()))
        return "; ".join(bits)

    __str__ = text

    def as_dict(self) -> dict:
        return {"stage": self.stage, "status": self.status, "produced": self.produced,
                "metrics": self.metrics, "commands": len(self.commands),
                "settings": self.settings, "seconds": round(self.seconds, 3)}

    @classmethod
    def from_dict(cls, d: dict) -> "StageResult":
        return cls(d["stage"], d["status"], dict(d.get("produced", {})),
                   dict(d.get("metrics", {})), [], dict(d.get("settings", {})),
                   float(d.get("seconds", 0.0)))


@dataclass
class FlowResult:
    stages: List[StageResult] = field(default_factory=list)
    note: str = ""

    def __getitem__(self, name: str) -> StageResult:
        for r in self.stages:
            if r.stage == name:
                return r
        raise KeyError(name)

    @property
    def netlist(self) -> Optional[str]:
        for r in reversed(self.stages):
            if "openroad_netlist" in r.produced:
                return r.produced["openroad_netlist"]
        return None

    @property
    def metrics(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for r in self.stages:
            out.update(r.metrics)
        return out

    def text(self) -> str:
        return "\n".join([r.text() for r in self.stages] + ([self.note] if self.note else []))

    __str__ = text


@dataclass
class PassResult:
    name: str
    command: List[str]
    input: str
    output: str
    replaced: Optional[str]
    seconds: float

    def __str__(self):
        return (f"pass {self.name}: {self.input} -> {self.output}"
                + (f" (now {self.replaced})" if self.replaced else "")
                + f" in {self.seconds:.1f} s")


# ---------------------------------------------------------------- metrics

_FIRREG = re.compile(r"=\s*seq\.(?:firreg|compreg)\b")
_MODULE = re.compile(r"^\s*hw\.module\s", re.M)
_INSTANCE = re.compile(r"\bhw\.instance\b")


def ir_metrics(path: Path) -> Dict[str, Any]:
    """Registers (seq.firreg / compreg ops), modules and instances of an IR
    file (text scan)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return {}
    return {"registers": len(_FIRREG.findall(text)), "modules": len(_MODULE.findall(text)),
            "instances": len(_INSTANCE.findall(text))}


def netlist_metrics(path: Path, table: Optional[dict] = None) -> Dict[str, Any]:
    """Cells (instances of non-module cells), the ten most used cells, and the
    area from `table` ({cell: (area, leakage)}, postlayout.liberty_table)."""
    from . import vtswap
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return {}
    mods = vtswap.instance_cells(text)
    counts: Counter = Counter()
    for insts in mods.values():
        for c in insts.values():
            if c not in mods:
                counts[c] += 1
    out: Dict[str, Any] = {"cells": sum(counts.values()),
                           "cell_types": len(counts),
                           "top_cells": dict(counts.most_common(10))}
    if table:
        out["area"] = round(sum(table[c][0] * n for c, n in counts.items() if c in table), 4)
        unknown = sum(n for c, n in counts.items() if c not in table)
        if unknown:
            out["cells_without_area"] = unknown
    return out


def _json(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def fanout_metrics(out_dir: Path) -> Dict[str, Any]:
    d = _json(Path(out_dir) / "fanout.json") or {}
    return {k: _num(d[k]) for k in ("buffers", "nets", "worst_fanout") if k in d}


def vt_swap_metrics(out_dir: Path) -> Dict[str, Any]:
    d = _json(Path(out_dir) / "vt_swap.json")
    if not d:
        return {}
    end, start = d.get("end") or {}, d.get("start") or {}
    return {"wns_ns": end.get("wns"), "tns_ns": end.get("tns"), "hold_wns_ns": end.get("hold_wns"),
            "start_wns_ns": start.get("wns"), "start_tns_ns": start.get("tns"),
            "vt_flavours": d.get("flavours_after"), "vt_up": len(d.get("up") or ()),
            "vt_down": len(d.get("down") or ())}


def sta_metrics(out_dir: Path) -> Dict[str, Any]:
    d = _json(Path(out_dir) / "sta" / "summary.json")
    if not d:
        return {}
    rows = [{k: r.get(k) for k in ("domain", "mode", "corner", "wns_ns", "tns_ns", "hold_wns_ns")}
            for r in d.get("domains", ())]
    wns = [r["wns_ns"] for r in rows if r.get("wns_ns") is not None]
    tns = [r["tns_ns"] for r in rows if r.get("tns_ns") is not None]
    return {"wns_ns": min(wns) if wns else None, "tns_ns": min(tns) if tns else None,
            "hold_wns_ns": (d.get("hold") or {}).get("wns_ns"), "sta_views": rows}


def pnr_metrics(pnr_dir: Path) -> Dict[str, Any]:
    d = _json(Path(pnr_dir) / "pnr_metrics.json")
    return dict(d) if isinstance(d, dict) else {}


def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return v
