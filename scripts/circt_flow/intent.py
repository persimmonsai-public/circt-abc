"""Power intent for a power-domain run: domain crossings, level shifters,
UPF and the per-mode SDC files.

Everything here works on the stitched hierarchical netlist the power flow
writes (power.py), in its OpenSTA form (`<out>/1_synth/<top>_openroad.v`):
the top module instantiates each power domain's module, and everything
else in the top (cells, the remainder's residual modules) is the top's own
domain, on the pack's default rail. The top's ports are on the default
rail too.

  * Crossings (find_crossings): every net of the top module whose driver
    and some load are in different domains. A domain's side is an instance
    of its module; the top's side is every other instance and the top's
    ports. The direction comes from the rails' voltages: low_to_high,
    high_to_low, or same_voltage (two domains on rails of one voltage: no
    shifter needed). Written to <out>/power_intent.json.
  * UPF (write_upf): <out>/<top>.upf, UPF 2.0 (IEEE 1801-2009) commands:
    a supply port and net per rail and the ground, a supply set per rail
    (SS_<rail>: its power net and the ground), one create_power_domain
    per domain with its rail's supply set as primary (the top's with
    -include_scope, the others with -elements {<instances>}), a port
    state per supply and one all-on power-state table, and level-shifter
    strategies that put every shifter in its RECEIVING domain, so a
    single-supply high-to-low cell is powered by the low rail it drives:
    a crossing into a power domain is a strategy on that domain's inputs
    (-location self), a crossing into the top's own domain one on the
    driving domain's outputs (-location parent), each with
    -input_supply_set (the driving rail's) and -output_supply_set (the
    receiving rail's), and -elements naming the crossing ports. When the
    flow inserted the shifters itself, the UPF has no strategies: each
    inserted shifter is an element of the domain it drives into, the UPF
    lists them in comments, and connect_supply_net -pins wires each
    one's supply pins (shifter_supplies: from the liberty's
    related_power_pin / related_ground_pin, so a library stripped of its
    pg_pin groups still works): the input side to the driving rail, the
    output side to the receiving rail, the ground to the ground. A
    shifter whose liberty names no supply pins is reported (a warning and
    a NOT CONNECTED comment), not guessed.
  * Per-mode SDCs (write_mode_sdcs): <out>/sdc/<mode>.sdc for place and
    route, each the mode's SDC file as written (the shorthand
    clock_period_ps mode as one create_clock and zero I/O delays).
  * Level-shifter insertion (insert_level_shifters, power:
    insert_level_shifters: true; off by default, since P&R usually does
    it): one shifter per crossing net and receiving domain, the cell the
    pack's level_shifters liberties offer for that direction and those
    voltages (choose_shifter), its output feeding every load of that
    domain on the net. The shifted netlist replaces <top>_openroad.v; the
    unshifted one is kept as <top>_unshifted_openroad.v. check_equivalent
    then proves the two are the same circuit with every shifter read as a
    buffer: every non-shifter pin and output port is driven by the same
    source in both.

Limits: domain instances must be instances of the top module (a domain
nested in a remainder submodule is refused); inout ports and pins are
refused; a domain output that drives a top-level output port with no net
in between cannot be shifted (refused with the net's name); isolation,
retention and power switches are not described.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import vnetlist as vn

TOP_DOMAIN = "top"  # the name of the top's own (default-rail) domain


class IntentError(ValueError):
    """A crossing, shifter or UPF request that cannot be honoured."""


# ---------------------------------------------------------------------------
# Liberty: pin directions and level-shifter cells
# ---------------------------------------------------------------------------


@dataclass
class LibCell:
    name: str
    library: str
    area: float = 0.0
    pins: Dict[str, str] = field(default_factory=dict)  # pin -> direction
    functions: Dict[str, str] = field(default_factory=dict)  # out -> fn
    is_level_shifter: bool = False
    ls_type: Optional[str] = None  # LH, HL, HL_LH
    in_range: Optional[Tuple[float, float]] = None
    out_range: Optional[Tuple[float, float]] = None
    pg_pins: Dict[str, str] = field(default_factory=dict)  # pin -> pg_type
    # signal pin -> its related_power_pin / related_ground_pin
    related_power: Dict[str, str] = field(default_factory=dict)
    related_ground: Dict[str, str] = field(default_factory=dict)
    clock_pins: set = field(default_factory=set)  # pins with clock : true
    # member pins of a bundle/bus group (a multibit register's D0..Dn, Q0..Qn): pin -> direction. Kept apart from `pins`, which names the
    # group (D, Q) and which the power-intent checks read.
    bundle_pins: Dict[str, str] = field(default_factory=dict)


def liberty_cells(paths: Iterable[Path], only: Optional[set] = None
                  ) -> Dict[str, LibCell]:
    """{cell: LibCell} of `paths` (the first library defining a cell wins),
    every cell or just `only`."""
    from .modes import _CELL_START, parse_group
    from .profile import _block_end
    out: Dict[str, LibCell] = {}
    for p in paths:
        text = Path(p).read_text(errors="ignore")
        lm = re.search(r'^\s*library\s*\(\s*"?([^")\s]+)"?\s*\)', text, re.M)
        lib = lm.group(1) if lm else Path(p).stem
        for cm in _CELL_START.finditer(text):
            name = cm.group(1)
            if name in out or (only is not None and name not in only):
                continue
            g = parse_group(text[cm.start():_block_end(text, cm.end() - 1)])
            c = LibCell(name, lib)
            try:
                c.area = float(g.attrs.get("area", 0) or 0)
            except ValueError:
                pass
            for pg in g.groups:
                if pg.kind == "pg_pin" and pg.args:
                    c.pg_pins[pg.args[0]] = (pg.attrs.get("pg_type")
                                             or "").strip('"')
                if pg.kind in ("pin", "bus", "bundle"):
                    d = pg.attrs.get("direction")
                    if d:
                        c.pins[pg.args[0]] = d
                    if "function" in pg.attrs:
                        c.functions[pg.args[0]] = pg.attrs["function"]
                    if pg.attrs.get("clock", "").strip('"').lower() == "true":
                        c.clock_pins.add(pg.args[0])
                    for attr, dst in (("related_power_pin", c.related_power),
                                      ("related_ground_pin",
                                       c.related_ground)):
                        if pg.attrs.get(attr):
                            dst[pg.args[0]] = pg.attrs[attr].strip('"')
                    for sub in pg.groups:  # pins of a bus
                        if sub.kind == "pin" and sub.attrs.get("direction"):
                            c.pins.setdefault(pg.args[0],
                                              sub.attrs["direction"])
                            if sub.args:
                                c.bundle_pins[sub.args[0]] = sub.attrs["direction"]
            c.is_level_shifter = g.attrs.get("is_level_shifter",
                                             "").lower() == "true"
            c.ls_type = g.attrs.get("level_shifter_type") or None
            for key, attr in (("in_range", "input_voltage_range"),
                              ("out_range", "output_voltage_range")):
                v = g.complex.get(attr)
                if v and len(v) == 2:
                    setattr(c, key, (float(v[0]), float(v[1])))
            out[name] = c
    return out


def shifter_supplies(c: LibCell) -> Dict[str, str]:
    """{supply pin: "input" | "output" | "ground"} of a level shifter: a
    power pin a signal input relates to (related_power_pin) is on the
    input side, one an output relates to on the output side (output wins
    for a pin both relate to: a single-supply cell's one rail is the one
    it drives); a ground pin (related_ground_pin, or a pg_pin whose
    pg_type says ground) is the ground. Read from the signal pins'
    attributes, so a library whose pg_pin groups were stripped still
    works; a power pg_pin no signal pin names is on the output side. {}
    when the liberty says nothing about supplies."""
    out: Dict[str, str] = {}
    for pin, pg in c.related_power.items():
        side = "output" if c.pins.get(pin) == "output" else "input"
        if out.get(pg) != "output":
            out[pg] = side
    for pg in c.related_ground.values():
        out[pg] = "ground"
    for pg, t in c.pg_pins.items():
        if "ground" in t.lower():
            out[pg] = "ground"
        else:
            out.setdefault(pg, "output")
    return out


def _unq_fn(fn: str) -> str:
    return fn.replace('"', "").replace(" ", "")


def shifter_pins(c: LibCell) -> Tuple[str, str]:
    """(input pin, output pin) of a one-input buffer-like shifter cell."""
    ins = [p for p, d in c.pins.items() if d == "input"]
    outs = [p for p, d in c.pins.items() if d == "output"]
    if len(ins) != 1 or len(outs) != 1:
        raise IntentError(f"level shifter {c.name}: needs one input and one "
                          f"output pin (has {', '.join(c.pins) or 'none'}; "
                          "enable pins are not supported)")
    fn = c.functions.get(outs[0])
    if fn is not None and _unq_fn(fn) != ins[0]:
        raise IntentError(f"level shifter {c.name}: {outs[0]} = {fn} is not "
                          f"a buffer of {ins[0]}")
    return ins[0], outs[0]


def choose_shifter(cells: Dict[str, LibCell], rule: str, v_from: float,
                   v_to: float) -> LibCell:
    """The smallest-area level shifter of `cells` for `rule` (low_to_high
    or high_to_low) whose type is LH / HL accordingly, or HL_LH (either
    direction; LH_HL is accepted too), and whose
    input/output voltage ranges, when the cell gives them, hold v_from and
    v_to."""
    want = {"low_to_high": "LH", "high_to_low": "HL"}[rule]
    ok, why = [], []
    for c in cells.values():
        if not c.is_level_shifter:
            continue
        t = (c.ls_type or "").upper()
        if t not in (want, "HL_LH", "LH_HL"):  # Liberty spells it HL_LH
            why.append(f"{c.name}: type {t or '?'}")
            continue
        if c.in_range and not c.in_range[0] <= v_from <= c.in_range[1]:
            why.append(f"{c.name}: input range {c.in_range} excludes "
                       f"{v_from} V")
            continue
        if c.out_range and not c.out_range[0] <= v_to <= c.out_range[1]:
            why.append(f"{c.name}: output range {c.out_range} excludes "
                       f"{v_to} V")
            continue
        try:
            shifter_pins(c)
        except IntentError as e:
            why.append(str(e))
            continue
        ok.append(c)
    if not ok:
        raise IntentError(
            f"no {want} level shifter for {v_from} V -> {v_to} V in the "
            "pack's level_shifters liberties"
            + (f" ({'; '.join(why)})" if why else " (no level-shifter cells)"))
    return min(ok, key=lambda c: (c.area, c.name))


# ---------------------------------------------------------------------------
# Domains of the stitched netlist
# ---------------------------------------------------------------------------


@dataclass
class DomainInfo:
    name: str
    rail: str
    voltage: float
    module: Optional[str] = None  # None for the top's own domain
    instances: List[str] = field(default_factory=list)  # in the top module


@dataclass
class Crossing:
    net: str
    driver: str
    from_domain: str
    to_domain: str
    from_rail: str
    to_rail: str
    from_voltage: float
    to_voltage: float
    rule: str  # low_to_high | high_to_low | same_voltage
    loads: List[str]
    # The endpoints themselves (vnetlist endpoints), for insertion.
    driver_ep: tuple = ()
    load_eps: List[tuple] = field(default_factory=list)

    def record(self) -> dict:
        return {k: getattr(self, k) for k in (
            "net", "driver", "from_domain", "to_domain", "from_rail",
            "to_rail", "from_voltage", "to_voltage", "rule", "loads")}


def rule_of(v_from: float, v_to: float) -> str:
    if abs(v_from - v_to) < 1e-9:
        return "same_voltage"
    return "low_to_high" if v_from < v_to else "high_to_low"


class Design:
    """The stitched netlist's top module with its domains: pin directions
    for cells (from `libs`) and for the netlist's own modules."""

    def __init__(self, text: str, top: str, domains: Sequence[DomainInfo],
                 libs: Sequence[Path], ls_libs: Sequence[Path] = (),
                 shifters: Optional[Dict[str, Tuple[str, str]]] = None):
        """`shifters`: {instance: (from domain, to domain)} of level
        shifters already in the top: their input side is in the first,
        their output side in the second."""
        self.text = text
        self.shifters = dict(shifters or {})
        self.mods = vn.parse(text)
        if top not in self.mods:
            raise IntentError(f"the netlist does not define {top}")
        self.top = top
        self.tmod = self.mods[top]
        self.domains = {d.name: d for d in domains}
        self.inst_domain: Dict[str, str] = {}
        insts = self.insts = {i.name: i for i in self.tmod.instances}
        for d in domains:
            if d.module is None:
                continue
            if not d.instances:
                raise IntentError(
                    f"domain {d.name}: no instance of {d.module} in {top} "
                    "(a domain nested inside a remainder module is not "
                    "supported by crossings, UPF and level shifters)")
            for i in d.instances:
                if i not in insts or insts[i].cell != d.module:
                    raise IntentError(f"domain {d.name}: {top} has no "
                                      f"instance {i} of {d.module}")
                self.inst_domain[i] = d.name
        top_dom = [d for d in domains if d.module is None]
        if len(top_dom) != 1:
            raise IntentError("exactly one domain must be the top's own")
        self.top_domain = top_dom[0].name
        used = {i.cell for i in self.tmod.instances} - set(self.mods)
        self.cells = liberty_cells(list(libs) + list(ls_libs), used)
        self.ls_cells = liberty_cells(ls_libs) if ls_libs else {}
        missing = sorted(used - set(self.cells))
        if missing:
            raise IntentError(f"{top}: cells not in the liberties: "
                              + ", ".join(missing[:10]))

    def pin_dir(self, cell: str, pin: str) -> Optional[str]:
        if cell in self.mods:
            return self.mods[cell].port_dir(pin)
        c = self.cells.get(cell) or self.ls_cells.get(cell)
        return c.pins.get(pin) if c else None

    def domain_of(self, ep) -> str:
        if ep[0] == "port":
            return self.top_domain
        if ep[1] in self.shifters:
            side = self.pin_dir(self.insts[ep[1]].cell, ep[2])
            return self.shifters[ep[1]][1 if side == "output" else 0]
        return self.inst_domain.get(ep[1], self.top_domain)


def find_crossings(des: Design) -> List[Crossing]:
    """Every (net, receiving domain) of the top module whose driver is in
    another domain, in netlist order of the nets."""
    nets, _ = vn.connectivity(des.tmod, des.pin_dir)
    widths = vn.pin_widths(des.tmod)
    out: List[Crossing] = []
    seen = set()
    for b in nets:
        n = nets[b]
        if id(n) in seen:
            continue
        seen.add(id(n))
        if n.driver is None:
            continue
        src = des.domain_of(n.driver)
        by: Dict[str, List[tuple]] = {}
        for ld in n.loads:
            dd = des.domain_of(ld)
            if dd != src:
                by.setdefault(dd, []).append(ld)
        for dst, lds in by.items():
            a, z = des.domains[src], des.domains[dst]
            out.append(Crossing(
                net=n.name, driver=vn.ep_str(n.driver, widths),
                from_domain=src, to_domain=dst, from_rail=a.rail,
                to_rail=z.rail, from_voltage=a.voltage,
                to_voltage=z.voltage, rule=rule_of(a.voltage, z.voltage),
                loads=[vn.ep_str(x, widths) for x in lds],
                driver_ep=n.driver, load_eps=lds))
    return out


def summarize(crossings: Sequence[Crossing]) -> List[str]:
    """One line per (from, to, rule): the count and the nets."""
    groups: Dict[Tuple[str, str, str], List[Crossing]] = {}
    for c in crossings:
        groups.setdefault((c.from_domain, c.to_domain, c.rule), []).append(c)
    out = []
    for (a, z, rule), cs in groups.items():
        nets = [c.net for c in cs]
        out.append(f"{a} ({cs[0].from_rail}) -> {z} ({cs[0].to_rail}): "
                   f"{rule}, {len(cs)} net(s): " + ", ".join(nets[:8])
                   + (" ..." if len(nets) > 8 else ""))
    return out


# ---------------------------------------------------------------------------
# Level-shifter insertion
# ---------------------------------------------------------------------------


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_") or "n"


def insert_level_shifters(des: Design, crossings: List[Crossing]
                          ) -> Tuple[str, List[dict]]:
    """The netlist text with a shifter on every crossing that needs one
    (rule != same_voltage): one per net and receiving domain, inserted in
    the top module. Returns (text, [inserted shifter records])."""
    mod = des.tmod
    nets, where = vn.connectivity(mod, des.pin_dir)
    names = set(mod.decls) | {i.name for i in mod.instances}
    # Each instance pin's bits (LSB first), to rewrite.
    pin_bits: Dict[Tuple[str, str], List[vn.Bit]] = {}
    for i in mod.instances:
        for p, e in i.conns:
            pin_bits[(i.name, p)] = list(reversed(vn.bits_of(e, mod)))
    assign_bits = [(list(vn.bits_of(l, mod)), list(vn.bits_of(r, mod)))
                   for l, r in mod.assigns]
    new_insts: List[vn.Instance] = []
    records: List[dict] = []

    def fresh(base: str) -> str:
        n, k = base, 1
        while n in names:
            k += 1
            n = f"{base}_{k}"
        names.add(n)
        return n

    for c in crossings:
        if c.rule == "same_voltage":
            continue
        cell = choose_shifter(des.ls_cells, c.rule, c.from_voltage,
                              c.to_voltage)
        a_pin, y_pin = shifter_pins(cell)
        src_bit = where[c.driver_ep]
        out_net = fresh(f"{_safe(c.net)}_ls_{c.to_domain}")
        inst = fresh(f"ls_{c.to_domain}_{_safe(c.net)}")
        mod.decls[out_net] = vn.Decl(out_net, "wire")
        for ld in c.load_eps:
            if ld[0] == "pin":
                bits = pin_bits[(ld[1], ld[2])]
                bits[ld[3]] = (out_net, None)
                continue
            # A top output port bit: rewrite the assign that drives it.
            pb = where[ld]
            done = False
            for lb, rb in assign_bits:
                for k, x in enumerate(lb):
                    if x == pb:
                        rb[k] = (out_net, None)
                        done = True
            if not done:
                raise IntentError(
                    f"net {c.net}: output port {vn.ep_str(ld)} is driven "
                    f"directly by {c.driver} in domain {c.from_domain}; "
                    "a shifter needs a net between them")
        new_insts.append(vn.Instance(cell.name, inst, [
            (a_pin, vn.bit_str(src_bit)), (y_pin, out_net)]))
        records.append({"instance": inst, "cell": cell.name,
                        "library": cell.library, "rule": c.rule,
                        "net": c.net, "input": vn.bit_str(src_bit),
                        "output": out_net, "from_domain": c.from_domain,
                        "to_domain": c.to_domain, "loads": c.loads})
    for i in mod.instances:
        i.conns = [(p, e if (i.name, p) not in pin_bits else
                    vn.expr_of(list(reversed(pin_bits[(i.name, p)])), mod))
                   for p, e in i.conns]
    mod.assigns = [(l, vn.expr_of(rb, mod))
                   for (l, _), (_, rb) in zip(mod.assigns, assign_bits)]
    mod.instances += new_insts
    spans = vn.module_spans(des.text)
    s, e = spans[des.top]
    text = (des.text[:s] + vn.write_module(mod).rstrip("\n")
            + des.text[e:])
    return text, records


def check_equivalent(orig: str, shifted: str, top: str, pin_dir_orig,
                     pin_dir_new, ls_cells: Dict[str, Tuple[str, str]]
                     ) -> List[str]:
    """Problems (empty: equivalent) between the unshifted and the shifted
    netlist, reading every instance of `ls_cells` ({cell: (in, out)}) in
    the shifted one as a buffer: every module but `top` must be unchanged,
    and in `top` the ports and the non-shifter instances (cells and pins)
    must be the same and each of their input pins and each output port
    driven by the same source."""
    a, b = vn.parse(orig), vn.parse(shifted)
    probs: List[str] = []
    if set(a) != set(b):
        probs.append("module sets differ: "
                     + ", ".join(sorted(set(a) ^ set(b))))
    sa, sb = vn.module_spans(orig), vn.module_spans(shifted)
    for m in a:
        if m != top and m in b and \
                orig[slice(*sa[m])] != shifted[slice(*sb[m])]:
            probs.append(f"module {m} changed")
    ta, tb = a[top], b[top]
    if ta.ports != tb.ports or any(
            (ta.decls[p].kind, ta.decls[p].width)
            != (tb.decls[p].kind, tb.decls[p].width) for p in ta.ports):
        probs.append(f"{top}: ports differ")
    ia = {i.name: i for i in ta.instances}
    ib = {i.name: i for i in tb.instances if i.cell not in ls_cells}
    ls_b = {i.name: i for i in tb.instances if i.cell in ls_cells}
    if set(ia) != set(ib):
        probs.append(f"{top}: instances differ: "
                     + ", ".join(sorted(set(ia) ^ set(ib))[:10]))
    for n in set(ia) & set(ib):
        if ia[n].cell != ib[n].cell or \
                [p for p, _ in ia[n].conns] != [p for p, _ in ib[n].conns]:
            probs.append(f"{top}: instance {n} changed cell or pins")
    na, wa = vn.connectivity(ta, pin_dir_orig)
    nb, wb = vn.connectivity(tb, pin_dir_new)

    def source(nets, where, ep, ls, depth=0):
        """The driver of the net at endpoint `ep`, through shifters."""
        n = nets[where[ep]]
        if n.driver is None:
            return ("const", n.const)
        d = n.driver
        if d[0] == "pin" and d[1] in ls:
            if depth > 8:
                return ("loop", d[1])
            i_pin = ls_cells[ls[d[1]].cell][0]
            return source(nets, where, ("pin", d[1], i_pin, 0), ls,
                          depth + 1)
        return d

    loads_a = [ep for ep in wa if (ep[0] == "port" and
                                   ta.decls[ep[1]].kind == "output")
               or (ep[0] == "pin" and pin_dir_orig(ia[ep[1]].cell, ep[2])
                   == "input")]
    for ep in loads_a:
        if ep not in wb:
            probs.append(f"{top}: {vn.ep_str(ep)} is gone")
            continue
        sa_, sb_ = source(na, wa, ep, {}), source(nb, wb, ep, ls_b)
        if sa_ != sb_:
            probs.append(f"{top}: {vn.ep_str(ep)} is driven by "
                         f"{vn.ep_str(sb_) if sb_[0] in ('pin', 'port') else sb_}"
                         f", was {vn.ep_str(sa_) if sa_[0] in ('pin', 'port') else sa_}")
    extra = [ep for ep in wb if ep[0] == "pin" and ep[1] in ib
             and ep not in wa]
    if extra:
        probs.append(f"{top}: new pin connections "
                     + ", ".join(vn.ep_str(e) for e in extra[:5]))
    return probs


# ---------------------------------------------------------------------------
# UPF
# ---------------------------------------------------------------------------


def pd_name(domain: str) -> str:
    return "PD_TOP" if domain == TOP_DOMAIN else f"PD_{domain}"


def ss_name(rail: str) -> str:
    """The supply set of a rail: its power net and the ground."""
    return f"SS_{rail}"


def _strategies(des: Design, crossings: List[Crossing]):
    """{(domain, applies_to, location, rule, from_rail, to_rail): sorted set
    of domain ports}, so that every shifter sits in its RECEIVING domain
    (the domain whose supply its output drives at): a crossing into a
    non-top domain is a strategy on that domain's inputs, -location self
    (inside it); a crossing into the top's own domain is a strategy on the
    driving domain's outputs, -location parent (the top)."""
    out: Dict[tuple, set] = {}
    for c in crossings:
        if c.rule == "same_voltage":
            continue
        if c.to_domain != des.top_domain:
            key = (c.to_domain, "inputs", "self", c.rule, c.from_rail,
                   c.to_rail)
            for ep in c.load_eps:
                out.setdefault(key, set()).add(f"{ep[1]}/{ep[2]}")
        else:
            ep = c.driver_ep
            key = (c.from_domain, "outputs", "parent", c.rule, c.from_rail,
                   c.to_rail)
            out.setdefault(key, set()).add(f"{ep[1]}/{ep[2]}")
    return out


def shifter_connections(des: Design, inserted: Sequence[dict],
                        ground: str) -> Tuple[Dict[str, List[str]],
                                              List[str]]:
    """({supply net: [<instance>/<pin>, ...]}, [problems]) for the inserted
    shifters: each supply pin on the driving rail (input side), the
    receiving rail (output side) or the ground (shifter_supplies). A cell
    whose liberty names no supply pins is a problem, not a guess."""
    conn: Dict[str, List[str]] = {}
    missing: List[str] = []
    for r in inserted:
        cell = des.ls_cells.get(r["cell"])
        sup = shifter_supplies(cell) if cell is not None else {}
        if not sup:
            missing.append(f"{r['instance']} ({r['cell']}): its liberty "
                           "names no supply pins (no related_power_pin)")
            continue
        rail = {"input": des.domains[r["from_domain"]].rail,
                "output": des.domains[r["to_domain"]].rail,
                "ground": ground}
        for pg, side in sorted(sup.items()):
            conn.setdefault(rail[side], []).append(f"{r['instance']}/{pg}")
    return conn, missing


def write_upf(path: Path, des: Design, crossings: List[Crossing],
              ground: str = "VSS", inserted: Sequence[dict] = (),
              source: str = "") -> None:
    """<top>.upf (UPF 2.0) for `des`: see the module docstring."""
    doms = list(des.domains.values())
    rails: Dict[str, float] = {}
    for d in doms:
        rails.setdefault(d.rail, d.voltage)
    if ground in rails:
        raise IntentError(f"power.ground {ground} is also a rail name")
    supplies = list(rails) + [ground]
    # Inserted shifters belong to the domain they drive into.
    extra: Dict[str, List[str]] = {}
    for r in inserted:
        if r["to_domain"] != des.top_domain:
            extra.setdefault(r["to_domain"], []).append(r["instance"])
    L = [f"# {path.name}: power intent for {des.top}, written by circt_flow "
         "(circt_flow/intent.py)" + (f" from {source}" if source else ""),
         "# UPF 2.0 (IEEE 1801-2009). Supplies are named after the tech "
         "pack's rails.", "",
         "upf_version 2.0", "",
         "# Supply ports and nets: one per rail and the ground."]
    for s in supplies:
        L.append(f"create_supply_port {s} -direction in")
        L.append(f"create_supply_net {s}")
        L.append(f"connect_supply_net {s} -ports {s}")
    L += ["", "# A supply set per rail: its power net and the ground."]
    for r in rails:
        L.append(f"create_supply_set {ss_name(r)} -function {{power {r}}} "
                 f"-function {{ground {ground}}}")
    L += ["", "# Power domains: the top's own (everything not in another "
          "domain,", "# on the default rail) and one per power: domain, "
          "each with its rail's", "# supply set as its primary supply."]
    for d in doms:
        sup = f"-supply {{primary {ss_name(d.rail)}}}"
        if d.module is None:
            L.append(f"create_power_domain {pd_name(d.name)} -include_scope "
                     + sup)
        else:
            els = list(d.instances) + extra.get(d.name, [])
            L.append(f"create_power_domain {pd_name(d.name)} -elements "
                     "{" + " ".join(els) + "} " + sup + f"  ;# {d.module}"
                     + (" and its inserted level shifters"
                        if extra.get(d.name) else ""))
    L += ["", "# Supply states and the power-state table (every rail on)."]
    for s, v in rails.items():
        L.append(f"add_port_state {s} -state {{{s}_on {v:g}}}")
    L.append(f"add_port_state {ground} -state {{{ground}_on 0}}")
    L.append(f"create_pst pst_{des.top} -supplies {{{' '.join(supplies)}}}")
    L.append(f"add_pst_state all_on -pst pst_{des.top} -state "
             "{" + " ".join(f"{s}_on" for s in supplies) + "}")
    L += ["", "# Level shifters: each in its receiving domain, between the "
          "supply sets", "# of the driving and the receiving rail."]
    if inserted:
        L.append(f"# circt_flow inserted {len(inserted)} shifter(s) in "
                 f"{des.top} (power.insert_level_shifters), each an element "
                 "of the")
        L.append("# domain it drives into; no strategy asks for more:")
        for r in inserted:
            L.append(f"#   {r['instance']} ({r['cell']}, {r['rule']}, "
                     f"{r['from_domain']} -> {r['to_domain']}): "
                     f"{r['input']} -> {r['output']}")
        conn, missing = shifter_connections(des, inserted, ground)
        L += ["", "# Each inserted shifter's supply pins: the input side on "
              "the driving rail,", "# the output side on the receiving "
              "rail (the cells' related_power_pin)."]
        for net, pins in conn.items():
            L.append(f"connect_supply_net {net} -pins "
                     "{" + " ".join(pins) + "}")
        for m in missing:
            L.append(f"# NOT CONNECTED: {m}")
    else:
        strat = _strategies(des, crossings)
        if not strat:
            L.append("# none: no net crosses between rails of different "
                     "voltages")
        for (dom, side, loc, rule, fr, to), ports in sorted(strat.items()):
            nm = (f"ls_{dom}_{'in' if side == 'inputs' else 'out'}_"
                  f"{fr}_to_{to}")
            L.append(f"set_level_shifter {nm} -domain {pd_name(dom)} "
                     f"-applies_to {side} -rule {rule} -location {loc} "
                     f"-input_supply_set {ss_name(fr)} "
                     f"-output_supply_set {ss_name(to)} "
                     "-elements {" + " ".join(sorted(ports)) + "}")
    same = [c for c in crossings if c.rule == "same_voltage"]
    if same:
        L += ["", f"# {len(same)} crossing net(s) between domains of the "
              "same voltage need no shifter."]
    path.write_text("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# Per-mode SDC files for place and route
# ---------------------------------------------------------------------------


def write_mode_sdcs(out_dir: Path, modes, top: str, time_unit: str,
                    clock_port: Optional[str] = None,
                    inputs: Sequence[str] = (), outputs: Sequence[str] = ()
                    ) -> Dict[str, str]:
    """<out_dir>/<mode>.sdc for each mode: the mode's SDC as written, under
    a header; the shorthand mode (no SDC file) as its clock with zero I/O
    delays on `inputs`/`outputs`. Returns {mode: path}."""
    from .modes import unit_ps
    out_dir.mkdir(parents=True, exist_ok=True)
    k = unit_ps(time_unit)
    res = {}
    for m in modes:
        p = out_dir / f"{m.name}.sdc"
        head = (f"# {top}, mode {m.name}, for place and route; written by "
                f"circt_flow (circt_flow/intent.py). Times in {time_unit}.\n")
        if m.sdc is not None:
            body = (f"# From {m.sdc}\n" + "".join(
                f"# clock {c.name}: {c.period_ps:g} ps on "
                f"{' '.join(c.ports) or '(virtual)'}\n"
                for c in m.clocks.values())
                + "\n" + Path(m.sdc).read_text())
        else:
            body = ""
            for c in m.clocks.values():
                port = " ".join(c.ports) or clock_port
                if not port:
                    raise IntentError("the shorthand mode has no clock port "
                                      "(set pnr.clock_port)")
                body += (f"create_clock -name {c.name} -period "
                         f"{c.period_ps / k:g} [get_ports {port}]\n")
                ins = [i for i in inputs if i not in (c.ports or (port,))]
                if ins:
                    body += (f"set_input_delay 0 -clock {c.name} "
                             f"[get_ports {{{' '.join(ins)}}}]\n")
                if outputs:
                    body += (f"set_output_delay 0 -clock {c.name} "
                             f"[get_ports {{{' '.join(outputs)}}}]\n")
        p.write_text(head + body)
        res[m.name] = str(p)
    return res


# ---------------------------------------------------------------------------
# The power flow's intent step
# ---------------------------------------------------------------------------


def domain_infos(prof, top_rail: str, records: Sequence[dict]
                 ) -> List[DomainInfo]:
    """DomainInfo of the top and of each power_domains.json domain record."""
    out = [DomainInfo(TOP_DOMAIN, top_rail, prof.rails[top_rail].voltage)]
    for r in records:
        out.append(DomainInfo(r["name"], r["rail"],
                              prof.rails[r["rail"]].voltage, r["module"],
                              list(r.get("netlist_instances") or [])))
    return out


def run(cfg, prof, records: Sequence[dict], insert: bool = False,
        ground: str = "VSS") -> dict:
    """Crossings, UPF, per-mode SDCs and (with `insert`) level shifters
    for a stitched power-flow run. Returns the record power_domains.json
    keeps under `intent`."""
    d = cfg.design
    top, out = d.top_module, cfg.out_dir
    nl = cfg.openroad_netlist
    if not nl.exists():
        raise IntentError(f"{nl} is missing (the OpenSTA-compatible netlist "
                          "needs sv2v: set SV2V or the sv2v docker image)")
    doms = domain_infos(prof, prof.default_rail, records)
    libs = prof.rails[prof.default_rail].liberties
    text = nl.read_text()
    des = Design(text, top, doms, libs, prof.level_shifters)
    crossings = find_crossings(des)
    need = [c for c in crossings if c.rule != "same_voltage"]
    print(f"[power] crossings: {len(crossings)} net(s) cross domains, "
          f"{len(need)} between different voltages")
    for line in summarize(crossings):
        print(f"[power]   {line}")
    rec = {"crossings": [c.record() for c in crossings]}
    inserted: List[dict] = []
    if insert and not need:
        print("[power] level shifters: no crossing needs one; the netlist "
              "is unchanged")
        rec["level_shifters"] = {"inserted": [], "note": "no crossing "
                                 "between rails of different voltages"}
    elif insert:
        if not prof.level_shifters:
            raise IntentError("power.insert_level_shifters: the tech pack "
                              "lists no level_shifters liberties")
        shifted, inserted = insert_level_shifters(des, crossings)
        keep = cfg.synth_dir / f"{top}_unshifted_openroad.v"
        keep.write_text(text)
        new_des = Design(shifted, top, doms, libs, prof.level_shifters,
                         {r["instance"]: (r["from_domain"], r["to_domain"])
                          for r in inserted})
        ls_pins = {r["cell"]: shifter_pins(des.ls_cells[r["cell"]])
                   for r in inserted}
        probs = check_equivalent(text, shifted, top, des.pin_dir,
                                 new_des.pin_dir, ls_pins)
        left = [c for c in find_crossings(new_des)
                if c.rule != "same_voltage"]
        if probs:
            raise IntentError("level-shifter insertion changed the circuit:\n  "
                              + "\n  ".join(probs[:20]))
        if left:
            raise IntentError(f"level-shifter insertion left {len(left)} "
                              "crossing(s) unshifted: "
                              + ", ".join(c.net for c in left[:10]))
        nl.write_text(shifted)
        print(f"[power] level shifters: inserted {len(inserted)} -> {nl} "
              f"(unshifted: {keep}); equivalent with shifters as buffers")
        for r in inserted:
            print(f"[power]   {r['instance']} {r['cell']} {r['rule']}: "
                  f"{r['input']} -> {r['output']}")
        conn, missing = shifter_connections(des, inserted, ground)
        for m in missing:
            print(f"[power] WARNING: level shifter {m}; its supplies are "
                  "not connected in the UPF")
        rec["level_shifters"] = {
            "inserted": inserted, "unshifted_netlist": str(keep),
            "supply_connections": conn, "unconnected": missing,
            "equivalence": "structural: every non-shifter input pin and "
                           "output port has the same source, shifters read "
                           "as buffers; other modules unchanged",
            "equivalent": True}
    else:
        rec["level_shifters"] = {"inserted": [], "note":
                                 "power.insert_level_shifters is off: "
                                 "the UPF strategies leave them to P&R"}
    upf = out / f"{top}.upf"
    write_upf(upf, des, crossings, ground, inserted, str(cfg.config_path))
    print(f"[power] power intent -> {upf}")
    rec["upf"] = str(upf)
    ports = des.tmod.ports
    ins = [p for p in ports if des.tmod.decls[p].kind == "input"]
    outs = [p for p in ports if des.tmod.decls[p].kind == "output"]
    rec["sdc"] = write_mode_sdcs(out / "sdc", d.effective_modes(), top,
                                 d.time_unit, d.clock_port, ins, outs)
    print(f"[power] per-mode SDCs -> {out / 'sdc'} "
          f"({', '.join(rec['sdc'])})")
    rec["domains"] = {x.name: {"rail": x.rail, "voltage": x.voltage,
                               "module": x.module, "instances": x.instances,
                               "power_domain": pd_name(x.name)}
                      for x in doms}
    rec["ground"] = ground
    (out / "power_intent.json").write_text(json.dumps(rec, indent=2) + "\n")
    return rec
