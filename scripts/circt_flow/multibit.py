"""Multibit flop mapping: the pack's declaration and the flow's switch.

register-mapper (mapper/lib/RegisterMapper/RegisterMapperPass.cpp) packs the
bits of a register that share clock, reset and flop class into the pack's
multibit cells (banks of 8/4/2 bits, in index order) while the clock and
reset structure is still known. It names no cell: this module hands it the
pack's JSON spec.

A pack declares the cells in its `flow:` section (techpack_*.yaml):

    flow:
      multibit:
        default_family: a            # the static choice
        cross_register: false          # pool leftover bits across registers
        families:
          a:
            clear:                     # async clear-to-0 bits
              cell: TMB{n}_A      # {n}: bank width
              widths: [8, 4, 2]        # the widths the library has
              clock: CK
              reset: R
              reset_active_high: true
              data: D{i}               # {i}: bit within the bank
              out: Q{i}
              tie: {SE: 0, SI: last_q} # 0, 1 or last_q (own last output)
              single:                  # optional: the class's single-bit cell
                cell: SOMEFF_X1        # for the bits no bank takes (a literal
                data: D                # name, pins D/Q by default); with it no
                out: Q                 # bit of the class maps to the liberty's
                                       # plain flop. Same clock/reset/tie pins.
            none: {...}                # bits with no reset
          b: {...}

Nothing happens unless the flow config opts in with `multibit:` (true, or a
mapping that overrides `default_family`/`cross_register`, prepends
`overrides:`, and turns the classes' `single:` cells on with `single: true`;
see below). Async-set bits and any class a family leaves out
stay single-bit cells.

Family choice. `default_family` is the static rule. `overrides:` is a list of
{reg: <regex on the register name>, family: <name>}, first match wins; that
is the hook an activity- or timing-driven choice writes (`overrides_file:` reads such a list
from JSON; multibit_timing.py writes one from an OpenSTA run: the registers on
near-critical paths go to a family with no multibit class, i.e. stay single).
`activity:` in the flow config
does it from a file: {file: act.json, hot_family: b, hot_toggle_rate: 0.1}
where act.json maps register names to data toggles per clock cycle; every
register at or above the threshold goes to `hot_family`.

Tie override. `tie: {<pin>: 0 | 1 | last_q}` in the flow config sets the tie value of
that pin in every class that ties it (pin names as in the classes' `tie:`), instead of
the pack's value; `last_q` is the bank's own last output.

Energy rule. With `activity: {file: act.json, rule: energy}` the family of
each register is the one that costs least, from the pack's `energy:` model
(per bit, for the families it lists):

    energy:
      period_ns: 1.0                 # default; the activity file may set its own
      area_weight_nw_per_um2: 5      # what one unit of area is worth, in nW (0: power only)
      families:
        a: {clock_aj: 10, q_aj: 10, d_aj: 10, leak_nw: 1, area_um2: 2}
        b: {clock_aj: 10, q_aj: 100, d_aj: 10, leak_nw: 2, area_um2: 1}

cost(f) = (clk * clock_aj + q * q_aj + d * d_aj) / period_ns + leak_nw
          + area_weight * area_um2           [aJ per ns is nW]

where clk is the clock pulses per cycle at the register's (gated) clock pin,
q and d the data toggles per bit and cycle (`multibit_activity` writes them:
{"period_ns": T, "registers": {name: {"q", "d", "clk"}}}; a bare
{name: rate} file means q = d = rate and an ungated clock). A register
whose cheapest family is not `default_family` gets an override; ties go to
the default. `candidates:` limits the families compared. The coefficients are
the pack's own numbers (the ones above are example values); nothing here names a cell.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

CLASS_KEYS = {"cell", "widths", "clock", "reset", "reset_active_high",
              "data", "out", "tie", "single"}
SINGLE_KEYS = {"cell", "data", "out"}
SPEC_KEYS = {"default_family", "cross_register", "families", "overrides",
             "energy"}
ENERGY_KEYS = {"period_ns", "area_weight_nw_per_um2", "families"}
ENERGY_FAMILY_KEYS = {"clock_aj", "q_aj", "d_aj", "leak_nw", "area_um2"}
CFG_KEYS = {"default_family", "cross_register", "overrides", "overrides_file",
            "activity", "single", "tie"}


def parse_pack(d, where: str) -> dict:
    """A pack's flow.multibit mapping, checked."""
    if not isinstance(d, dict):
        raise KeyError(f"{where}: multibit must be a mapping")
    bad = set(d) - SPEC_KEYS
    if bad:
        raise KeyError(f"{where}: unknown multibit key(s) "
                       f"{', '.join(sorted(bad))}")
    fams = d.get("families")
    if not isinstance(fams, dict) or not fams:
        raise KeyError(f"{where}: multibit.families is required")
    for fname, fam in fams.items():
        for cname, c in (fam or {}).items():
            if cname not in ("clear", "none"):
                raise KeyError(f"{where}: family {fname}: class {cname!r}; "
                               "the classes are clear and none")
            bad = set(c) - CLASS_KEYS
            if bad:
                raise KeyError(f"{where}: {fname}.{cname}: unknown key(s) "
                               f"{', '.join(sorted(bad))}")
            for k in ("cell", "widths", "clock", "data", "out"):
                if k not in c:
                    raise KeyError(f"{where}: {fname}.{cname}.{k} is required")
            if "{n}" not in c["cell"]:
                raise KeyError(f"{where}: {fname}.{cname}.cell needs {{n}}")
            if cname == "clear" and "reset" not in c:
                raise KeyError(f"{where}: {fname}.clear.reset is required")
            if "single" in c:
                sg = c["single"]
                if (not isinstance(sg, dict) or set(sg) - SINGLE_KEYS
                        or not isinstance(sg.get("cell"), str)
                        or "{" in sg["cell"]):
                    raise KeyError(f"{where}: {fname}.{cname}.single takes "
                                   "cell (a literal cell name), data, out")
    if d.get("default_family") not in fams:
        raise KeyError(f"{where}: multibit.default_family must name a family")
    if "energy" in d:
        parse_energy(d["energy"], fams, where)
    return d


def parse_energy(e, fams, where) -> dict:
    if not isinstance(e, dict) or set(e) - ENERGY_KEYS:
        raise KeyError(f"{where}: multibit.energy takes "
                       f"{', '.join(sorted(ENERGY_KEYS))}")
    ef = e.get("families")
    if not isinstance(ef, dict) or not ef:
        raise KeyError(f"{where}: multibit.energy.families is required")
    for name, c in ef.items():
        if name not in fams:
            raise KeyError(f"{where}: multibit.energy.{name} is not a family")
        if not isinstance(c, dict) or set(c) - ENERGY_FAMILY_KEYS:
            raise KeyError(f"{where}: multibit.energy.{name} takes "
                           f"{', '.join(sorted(ENERGY_FAMILY_KEYS))}")
        for k, v in c.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise KeyError(f"{where}: multibit.energy.{name}.{k} must be "
                               "a number >= 0")
    for k in ("period_ns", "area_weight_nw_per_um2"):
        if k in e and (isinstance(e[k], bool) or not isinstance(e[k], (int, float))
                       or e[k] < (1e-9 if k == "period_ns" else 0)):
            raise KeyError(f"{where}: multibit.energy.{k} is out of range")
    return e


def resolve(pack: Optional[dict], cfg, base: Path, where: str
            ) -> Optional[dict]:
    """The spec for the register-mapper, or None (multibit off). `cfg` is the
    flow config's `multibit:` value; the pack's declaration is `pack`."""
    if cfg is None or cfg is False:
        return None
    if pack is None:
        raise KeyError(f"{where}: multibit: needs the pack's flow.multibit "
                       "(the multibit cell families); this pack has none")
    spec = json.loads(json.dumps(pack))  # deep copy
    if cfg is True:
        cfg = {}
    if not isinstance(cfg, dict):
        raise KeyError(f"{where}: multibit must be true or a mapping")
    bad = set(cfg) - CFG_KEYS
    if bad:
        raise KeyError(f"{where}: multibit: unknown key(s) "
                       f"{', '.join(sorted(bad))}; allowed: "
                       f"{', '.join(sorted(CFG_KEYS))}")
    for k in ("default_family", "cross_register"):
        if k in cfg:
            spec[k] = cfg[k]
    if not cfg.get("single"):
        # the pack's single-bit cells are opt-in (`single: true`): without
        # it the leftover bits map to the liberty's plain flop
        for fam in spec["families"].values():
            for c in (fam or {}).values():      # a family or class may be null in a pack
                if isinstance(c, dict):
                    c.pop("single", None)
    if cfg.get("tie") is not None:
        apply_tie(spec, cfg["tie"], where)
    ov = list(cfg.get("overrides") or [])
    if cfg.get("overrides_file"):
        f = Path(cfg["overrides_file"])
        f = f if f.is_absolute() else base / f
        ov += json.loads(f.read_text())
    if cfg.get("activity"):
        ov += activity_overrides(cfg["activity"], base, where, spec)
    spec["overrides"] = ov + list(spec.get("overrides") or [])
    for o in spec["overrides"]:
        if o.get("family") not in spec["families"]:
            raise KeyError(f"{where}: multibit override family "
                           f"{o.get('family')!r} is not a family")
        re.compile(o["reg"])
    if spec["default_family"] not in spec["families"]:
        raise KeyError(f"{where}: multibit.default_family "
                       f"{spec['default_family']!r} is not a family")
    return spec


def apply_tie(spec: dict, tie, where: str) -> None:
    """`tie: {PIN: 0 | 1 | last_q}` in the flow config replaces the tie of PIN in every
    class of every family that already ties PIN (a pin a class does not tie stays
    untouched); `last_q` is the bank's own last output."""
    if not isinstance(tie, dict) or not tie:
        raise KeyError(f"{where}: multibit.tie must be a mapping PIN: 0|1|last_q")
    for pin, val in tie.items():
        if isinstance(val, bool) or val not in (0, 1, "last_q"):
            raise KeyError(f"{where}: multibit.tie.{pin} must be 0, 1 or last_q")
    for fam in spec["families"].values():
        for c in (fam or {}).values():
            t = c.get("tie")
            if not isinstance(t, dict):
                continue
            for pin, val in tie.items():
                if pin in t:
                    t[pin] = val


def activity_overrides(act: dict, base: Path, where: str,
                       spec: Optional[dict] = None) -> list:
    """Overrides from an activity file. Default rule: registers whose data
    toggle rate is at least `hot_toggle_rate` go to `hot_family`. With
    `rule: energy`: each register goes to its cheapest family under the
    pack's energy model (see the module docstring)."""
    f = Path(act["file"])
    f = f if f.is_absolute() else base / f
    data = json.loads(f.read_text())
    if act.get("rule", "threshold") == "energy":
        return energy_overrides(data, act, spec or {}, where)
    if act.get("rule", "threshold") != "threshold":
        raise KeyError(f"{where}: multibit.activity.rule is threshold or energy")
    rates = data.get("registers", data) if isinstance(data, dict) else data
    rates = {r: (v["q"] if isinstance(v, dict) else v) for r, v in rates.items()
             if r != "period_ns"}
    thr = float(act.get("hot_toggle_rate", 0.1))
    fam = act["hot_family"]
    return [reg_override(r, fam) for r, v in sorted(rates.items())
            if float(v) >= thr]


def reg_override(reg: str, family: str) -> dict:
    # Clock-gate inference renames a register `<name>_cg<N>[...]`; a name
    # that already carries the suffix matches itself only.
    pat = "^" + re.escape(reg) + ("$" if re.search(r"_cg\d+\w*$", reg)
                                  else r"(_cg\d+\w*)?$")
    return {"reg": pat, "family": family}


def family_cost(c: dict, rec: dict, period_ns: float, area_weight: float) -> float:
    """nW per bit of a register with activity `rec` in a family with
    coefficients `c`."""
    aj = (rec["clk"] * c.get("clock_aj", 0.0) + rec["q"] * c.get("q_aj", 0.0)
          + rec["d"] * c.get("d_aj", 0.0))
    return aj / period_ns + c.get("leak_nw", 0.0) + area_weight * c.get("area_um2", 0.0)


def energy_choice(rec: dict, energy: dict, period_ns: float, default: str,
                  candidates=None):
    """(family, {family: nW/bit}) for one register record."""
    fams = energy["families"]
    names = [n for n in (candidates or sorted(fams)) if n in fams]
    aw = float(energy.get("area_weight_nw_per_um2", 0.0))
    cost = {n: family_cost(fams[n], rec, period_ns, aw) for n in names}
    best = min(cost.values())
    if default in cost and cost[default] <= best + 1e-12:
        return default, cost
    return min(sorted(cost), key=lambda n: cost[n]), cost


def energy_overrides(data, act: dict, spec: dict, where: str) -> list:
    energy = spec.get("energy")
    if not energy:
        raise KeyError(f"{where}: multibit.activity.rule energy needs the "
                       "pack's multibit.energy model")
    if not isinstance(data, dict):
        raise KeyError(f"{where}: the activity file must be a mapping")
    regs = data.get("registers", data)
    period = float(data.get("period_ns", energy.get("period_ns", 1.0)))
    if "area_weight_nw_per_um2" in act:
        energy = {**energy, "area_weight_nw_per_um2": act["area_weight_nw_per_um2"]}
    cands = act.get("candidates")
    for n in cands or []:
        if n not in spec["families"]:
            raise KeyError(f"{where}: multibit.activity.candidates: {n!r} "
                           "is not a family")
    out = []
    for reg, v in sorted(regs.items()):
        if reg == "period_ns":
            continue
        if isinstance(v, dict):
            rec = {"q": float(v.get("q", 0.0)), "d": float(v.get("d", v.get("q", 0.0))),
                   "clk": float(v.get("clk", 1.0))}
        else:
            rec = {"q": float(v), "d": float(v), "clk": 1.0}
        fam, _ = energy_choice(rec, energy, period, spec["default_family"], cands)
        if fam != spec["default_family"]:
            out.append(reg_override(reg, fam))
    return out


def cell_names(spec: dict) -> list:
    """Every cell name the spec can instantiate."""
    out = []
    for fam in spec["families"].values():
        for c in fam.values():
            out += [c["cell"].replace("{n}", str(n)) for n in c["widths"]]
            if "single" in c:
                out.append(c["single"]["cell"])
    return sorted(set(out))


def check_cells(spec: dict, liberty_cells: set) -> list:
    """Problems: spec cells missing from the liberties."""
    return [f"multibit cell {n} is not in the liberties"
            for n in cell_names(spec) if n not in liberty_cells]


def write(spec: dict, path: Path) -> Path:
    spec = {k: v for k, v in spec.items() if k != "energy"}  # the pass has no use for it
    path.write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
    return path
