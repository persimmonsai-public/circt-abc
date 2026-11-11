"""Clock-gate economy options for clock-gate-inference: the pack's numbers and
the flow's switch.

clock-gate-inference (mapper/lib/RegisterMapper/ClockGateInferencePass.cpp)
builds one gate per (clock, enable) group. On a design whose enables are
many spellings of a few functions that is far more gates than needed, and
every gate costs clock-network power (its own clock pin, internal power, the
clock buffers CTS builds on its output). These options trim them; each is off
unless set, and none names a cell.

A pack may declare numbers under `flow.clock_gating` (how expensive a gate is
on its library, in units of one flop bit's clock-pin power); nothing happens
unless the flow config opts in with `clock_gating:` (true, or a mapping that
overrides the pack's values):

    flow:                                  # pack (example values)
      clock_gating:
        merge_equivalent_enables: true     # one gate per enable FUNCTION
        gate_cost_bits: 4.0                # a gate costs 4 flop clock pins
        gate_idle_fraction: 0.4            # share of cycles a gated group idles
        enable_op_cost_bits: 0.5           # per synthesized enable cell
        hold_mux_bits: 0.0                 # saving per bit of the hold mux
        min_gate_bits: 4                   # never gate fewer bits
        max_gate_depth: 3                  # gates in series on a clock path

    clock_gating: true                     # flow config
"""
from __future__ import annotations

from typing import Optional

BOOLS = {"merge_equivalent_enables"}
INTS = {"min_gate_bits", "max_gate_depth"}
FLOATS = {"gate_cost_bits", "gate_idle_fraction", "enable_op_cost_bits",
          "hold_mux_bits"}
KEYS = BOOLS | INTS | FLOATS


def _check(d, where: str) -> dict:
    if not isinstance(d, dict):
        raise KeyError(f"{where}: must be a mapping")
    bad = set(d) - KEYS
    if bad:
        raise KeyError(f"{where}: unknown key(s) {', '.join(sorted(bad))}; "
                       f"allowed: {', '.join(sorted(KEYS))}")
    for k, v in d.items():
        if k in BOOLS:
            if not isinstance(v, bool):
                raise KeyError(f"{where}.{k} must be true or false")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise KeyError(f"{where}.{k} must be a number >= 0")
        elif k in INTS and int(v) != v:
            raise KeyError(f"{where}.{k} must be an integer")
    return d


def parse_pack(d, where: str) -> dict:
    """A pack's flow.clock_gating mapping, checked."""
    return _check(d, where)


def resolve(pack: Optional[dict], cfg, where: str) -> Optional[str]:
    """The options text appended to clock-gate-inference's, or None (off).
    `cfg` is the flow config's `clock_gating:`; `pack` the pack's values."""
    if cfg is None or cfg is False:
        return None
    if cfg is True:
        cfg = {}
    vals = {**(pack or {}), **_check(cfg, f"{where}: clock_gating")}
    if not vals:
        vals = {"merge_equivalent_enables": True}
    out = []
    for k in sorted(vals):
        v = vals[k]
        v = str(bool(v)).lower() if k in BOOLS else (
            str(int(v)) if k in INTS else repr(float(v)))
        out.append(f"{k.replace('_', '-')}={v}")
    return " ".join(out)
