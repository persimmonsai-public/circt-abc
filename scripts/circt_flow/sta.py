"""In-flow multi-corner, multi-mode static timing (OpenSTA).

Opt-in: `python3 -m circt_flow <flow.yaml> <out> --sta`, or a flow config
section

    sta:
      enabled: true          # the default once the section is there
      corners: [tt, ss]      # default: every corner of the profile
      modes: [func]          # default: every mode
      chip: true             # power flows: also time the whole chip
      boundary_budget: 0.5   # outside share of the period at domain ports
      image: openroad/orfs:26Q3-277-gf9ec54a6d

OpenSTA runs in the ORFS docker image (`sta` from its OpenROAD install), or
the binary that $OPENSTA names. Everything goes under <out>/sta/: the
generated SDC, Tcl and logs per view, summary.json, and a printed table.

Why domains are timed separately. OpenSTA binds a netlist cell to a
liberty cell by NAME, and the same cell names exist on every rail (and at
every corner). One link of a mixed-rail netlist would time every cell of
one name with one rail's library. So each power domain is linked on its
own (`link_design <domain module>`) with its own rail's liberties at the
corner, and timed with that mode's clocks and boundary I/O delays:

  * clocks: each mode clock whose port reaches a domain port (through nets,
    assigns and level shifters of the top module) is created on that port
    with the mode's period; the mode's other clocks are virtual, so I/O
    delays relative to them still apply;
  * boundary I/O delays: a domain port that reaches a top-level port with
    a set_input_delay / set_output_delay in the mode gets that delay (the
    largest when several bits differ), relative to the same clock (0 when
    the mode has none for it, as the top's own SDC has it); every other
    boundary port (one that connects to another domain or the top's glue)
    gets boundary_budget (default 0.5) x the period of the domain's own
    clock: the share left to the logic on the other side. With 0.5, two
    domains that each pass their view leave each other half the period,
    so a direct domain-to-domain path fits (the glue and shifters between
    them are not counted). The chip view below times those paths exactly;
  * the top's own domain is linked with domain modules as boundary cells
    (a generated liberty with their pins and no arcs: <out>/sta/
    netlists/boundary.lib), its SDC the mode's own plus input delay 0 at
    each domain output pin and output delay 0 at each domain input pin
    (optimistic: the domains' side of those paths is in the chip view).

A view fails the stage on an OpenSTA Error and on a warning that means the
view is not the one asked for (FATAL_WARNING: a cell linked as a black box,
an SDC object not found, a constraint left with no objects). Other
warnings are kept in the summary (sta_warnings).

The chip view (power flows, `chip: true`): a copy of the stitched netlist
whose cells are renamed per rail (`<rail>__<cell>`), linked with copies of
each rail's liberties at that corner with the same renaming
(<out>/sta/libs/<corner>/<rail>/), times the whole design with the mode's
own SDC, paths that cross domains included. Its endpoints are attributed to
the domain they are in. The level-shifter liberties are not renamed and
have no corners: the same file is read at every corner.

Hold: every view records its worst hold slack, overall and by path class
(register to register, from inputs, to outputs); the summary's `hold`
entry is the worst over the views at the pack's hold corner
(`corners.hold`, else the setup corner): the chip views when there are
any (exact paths), else the domain views (assumed boundary delays).

Limits: setup (max) timing is the summary's WNS/TNS; hold is timed with
ideal clocks, so it is a check of the netlist and the constraints, not
signoff; clocks are ideal; a clock that reaches a domain through
logic other than level shifters is not traced (the domain's flops are then
unclocked in its own view, and a warning says so); OpenSTA reports in the
first liberty's time unit, converted to ns.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import vnetlist as vn

ORFS_IMAGE = "openroad/orfs:26Q3-277-gf9ec54a6d"
STA_BIN = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/sta"
STA_KEYS = {"enabled", "corners", "modes", "chip", "image",
            "boundary_budget"}
DEFAULT_BUDGET = 0.5
# OpenSTA warnings that mean the view is not what was asked for: a cell
# left as a black box, an SDC object that does not exist (its constraint
# is dropped). They fail the view like an Error does.
FATAL_WARNING = re.compile(r"not found|black box|no valid objects|"
                           r"unresolved|not linked", re.I)


class StaError(ValueError):
    """An sta: section or a timing run that cannot be honoured."""


@dataclass(frozen=True)
class StaSpec:
    enabled: bool = True
    corners: Tuple[str, ...] = ()  # () = every corner
    modes: Tuple[str, ...] = ()  # () = every mode
    chip: bool = True
    image: str = ORFS_IMAGE
    # The share of the domain clock's period a domain view leaves to the
    # logic outside it at an inter-domain boundary port (see the module
    # docstring).
    boundary_budget: float = DEFAULT_BUDGET


def parse_sta(d, where) -> StaSpec:
    if d is None or d is True:
        return StaSpec()
    if d is False:
        return StaSpec(enabled=False)
    if not isinstance(d, dict) or set(d) - STA_KEYS:
        bad = sorted(set(d) - STA_KEYS) if isinstance(d, dict) else ["?"]
        raise StaError(f"{where}: sta.{bad[0]} is not a key; allowed: "
                       + ", ".join(sorted(STA_KEYS)))
    lists = {}
    for k in ("corners", "modes"):
        v = d.get(k) or []
        if isinstance(v, str) or not all(isinstance(x, str) for x in v):
            raise StaError(f"{where}: sta.{k} must be a list of names")
        lists[k] = tuple(v)
    for k in ("enabled", "chip"):
        if k in d and not isinstance(d[k], bool):
            raise StaError(f"{where}: sta.{k} must be true or false")
    image = d.get("image", ORFS_IMAGE)
    if not isinstance(image, str) or not image:
        raise StaError(f"{where}: sta.image must be a docker image name")
    b = d.get("boundary_budget", DEFAULT_BUDGET)
    if isinstance(b, bool) or not isinstance(b, (int, float)) \
            or not 0 <= b < 1:
        raise StaError(f"{where}: sta.boundary_budget must be a fraction of "
                       f"the clock period in [0, 1), got {b!r}")
    return StaSpec(enabled=d.get("enabled", True), corners=lists["corners"],
                   modes=lists["modes"], chip=d.get("chip", True),
                   image=image, boundary_budget=float(b))


def wanted(cfg) -> Optional[StaSpec]:
    """The run's StaSpec when STA is on (--sta, or an enabled sta:)."""
    spec = cfg.design.sta
    if cfg.run_sta:
        return spec if spec is not None else StaSpec()
    if spec is not None and spec.enabled:
        return spec
    return None


# ---------------------------------------------------------------------------
# SDC helpers
# ---------------------------------------------------------------------------


def target_ports(targets: str, ins: Sequence[str], outs: Sequence[str]
                 ) -> Optional[set]:
    """The top-level port names an SDC object list names, or None when it is
    not one of [get_ports ...], [all_inputs], [all_outputs] or
    [delete_from_list [all_inputs|all_outputs] [get_ports ...]]."""
    from .modes import _get_ports
    t = " ".join(targets.split())
    allp = list(ins) + list(outs)

    def names(word):
        g = _get_ports(word)
        if g is None:
            return None
        out = set()
        for pat in g:
            base = re.sub(r"\[\d+(:\d+)?\]$", "", pat)
            out |= {p for p in allp if fnmatch.fnmatchcase(p, base)}
        return out
    if t == "[all_inputs]":
        return set(ins)
    if t == "[all_outputs]":
        return set(outs)
    m = re.fullmatch(r"\[delete_from_list \[(all_inputs|all_outputs)\] "
                     r"(\[get_ports .*\])\]", t)
    if m:
        base = set(ins if m.group(1) == "all_inputs" else outs)
        sub = names(m.group(2))
        return None if sub is None else base - sub
    return names(t)


def _fmt(ps: float, k: float) -> str:
    return f"{ps / k:.6g}"


# ---------------------------------------------------------------------------
# Tracing a domain's ports to the top's ports
# ---------------------------------------------------------------------------


def trace_ports(tmod: vn.Module, pin_dir, inst: str, ls_cells: Dict[str, Tuple[str, str]]
                ) -> Dict[str, set]:
    """{port of instance `inst`: top-level port names it reaches}: an input
    pin's net driver, followed back through level-shifter cells
    (`ls_cells` {cell: (in, out)}), when it is a top input port; an output
    pin's net loads, followed forward through shifters, that are top
    output ports."""
    nets, where = vn.connectivity(tmod, pin_dir)
    insts = {i.name: i for i in tmod.instances}

    def back(ep, depth=0):
        n = nets[where[ep]]
        d = n.driver
        if d is None:
            return set()
        if d[0] == "port":
            return {d[1]}
        if d[1] in insts and insts[d[1]].cell in ls_cells and depth < 8:
            a = ls_cells[insts[d[1]].cell][0]
            return back(("pin", d[1], a, 0), depth + 1)
        return set()

    def fwd(ep, depth=0):
        out = set()
        for ld in nets[where[ep]].loads:
            if ld[0] == "port":
                out.add(ld[1])
            elif ld[1] in insts and insts[ld[1]].cell in ls_cells \
                    and depth < 8:
                y = ls_cells[insts[ld[1]].cell][1]
                out |= fwd(("pin", ld[1], y, 0), depth + 1)
        return out

    res: Dict[str, set] = {}
    for ep in where:
        if ep[0] != "pin" or ep[1] != inst:
            continue
        dr = pin_dir(insts[inst].cell, ep[2])
        s = back(ep) if dr == "input" else fwd(ep)
        res.setdefault(ep[2], set()).update(s)
    return res


def domain_sdc(mode, module: vn.Module, reach: Dict[str, set],
               top_ins: Sequence[str], top_outs: Sequence[str],
               time_unit: str, budget: float = 0.0
               ) -> Tuple[str, List[str], List[str]]:
    """The SDC that times one domain module alone in `mode`: (text, the
    domain's clock names, warnings). A port that reaches no top-level port
    (an inter-domain boundary) gets `budget` x the domain clock's period
    as its I/O delay: the outside logic's share."""
    from .modes import unit_ps
    k = unit_ps(time_unit)
    L = [f"# domain module {module.name}, mode {mode.name}: written by "
         "circt_flow.sta. Times in " + time_unit + "."]
    warns: List[str] = []
    ins = [p for p in module.ports if module.decls[p].kind == "input"]
    outs = [p for p in module.ports if module.decls[p].kind == "output"]
    clk_ports: Dict[str, List[str]] = {}
    for c in mode.clocks.values():
        ps = [p for p in ins if reach.get(p, set()) & set(c.ports)]
        if ps:
            clk_ports[c.name] = ps
    own = sorted(clk_ports, key=lambda n: mode.clocks[n].period_ps)
    for c in mode.clocks.values():
        if c.name in clk_ports:
            L.append(f"create_clock -name {c.name} -period "
                     f"{_fmt(c.period_ps, k)} [get_ports "
                     f"{{{' '.join(clk_ports[c.name])}}}]")
        else:
            L.append(f"create_clock -name {c.name} -period "
                     f"{_fmt(c.period_ps, k)}  ;# virtual here")
    if not own:
        warns.append(f"mode {mode.name}: no clock of the mode reaches "
                     f"{module.name}'s ports; its flops are unclocked in "
                     "this view")
    clocked = {p for ps in clk_ports.values() for p in ps}
    # Boundary delays: the mode's delays of the top ports a domain port
    # reaches, else 0 against the domain's own (tightest) clock.
    by_port: Dict[str, Dict[Tuple[str, str, str], float]] = {}
    for dly in mode.io_delays:
        tp = target_ports(dly.targets, top_ins, top_outs)
        if tp is None:
            warns.append(f"mode {mode.name}: {dly.kind} delay on "
                         f"{dly.targets}: object list not understood; not "
                         "mapped onto domains")
            continue
        for p in (ins if dly.kind == "input" else outs):
            if p in clocked:
                continue
            if reach.get(p, set()) & tp:
                key = (dly.kind, dly.clock, dly.minmax)
                cur = by_port.setdefault(p, {})
                # The most pessimistic of several bits' delays: the largest
                # for setup, the smallest for hold (-min).
                pick = min if dly.minmax == "min" else max
                cur[key] = pick(cur.get(key, dly.delay_ps), dly.delay_ps)
    for kind, plist in (("input", ins), ("output", outs)):
        for p in plist:
            if p in clocked:
                continue
            if p in by_port:
                first = True
                for (kd, clk, mm), v in sorted(by_port[p].items()):
                    L.append(f"set_{kd}_delay {_fmt(v, k)} -clock {clk}"
                             + (f" -{mm}" if mm else "")
                             + ("" if first else " -add_delay")
                             + f" [get_ports {{{p}}}]  ;# from "
                             + ",".join(sorted(reach.get(p, set()))))
                    first = False
            elif own and reach.get(p):
                L.append(f"set_{kind}_delay 0 -clock {own[0]} "
                         f"[get_ports {{{p}}}]  ;# boundary (top "
                         f"{','.join(sorted(reach[p]))}, no delay in the "
                         "mode)")
            elif own:
                dl = budget * mode.clocks[own[0]].period_ps
                L.append(f"set_{kind}_delay {_fmt(dl, k)} -clock {own[0]} "
                         f"[get_ports {{{p}}}]  ;# boundary (budget "
                         f"{budget:g} of the period)")
    # The mode's false paths from top-level inputs, onto the domain inputs
    # they reach.
    ff = set(getattr(mode, "false_from", ()) or ())
    for p in ins:
        if p not in clocked and reach.get(p, set()) & ff:
            L.append(f"set_false_path -from [get_ports {{{p}}}]  ;# from "
                     + ",".join(sorted(reach[p] & ff)))
    return "\n".join(L) + "\n", own, warns


def boundary_lib(mods: Sequence[vn.Module], time_unit: str = "1ns") -> str:
    """A liberty with one cell per module: its pins and directions, no
    arcs, so the top can be linked with these modules as leaf cells."""
    types, cells = {}, []
    for m in mods:
        pins = []
        for p in m.ports:
            d = m.decls[p]
            if d.msb is None:
                pins.append(f"    pin({p}) {{ direction : {d.kind}; "
                            "capacitance : 0; }")
            else:
                t = f"bus_{d.msb}_{d.lsb}"
                types[t] = (d.msb, d.lsb)
                pins.append(f"    bus({p}) {{ bus_type : {t}; direction : "
                            f"{d.kind}; capacitance : 0; }}")
        cells.append(f"  cell({m.name}) {{\n    area : 0;\n"
                     + "\n".join(pins) + "\n  }")
    tdefs = [f"  type({t}) {{ base_type : array; data_type : bit; "
             f"bit_width : {abs(a - b) + 1}; bit_from : {a}; bit_to : {b}; "
             f"downto : {'true' if a >= b else 'false'}; }}"
             for t, (a, b) in types.items()]
    return ("/* Domain modules as boundary cells (pins only, no timing), "
            "written by circt_flow.sta. */\nlibrary(circt_flow_boundary) {\n"
            "  delay_model : table_lookup;\n"
            f"  time_unit : \"{time_unit}\";\n"
            "  capacitive_load_unit(1, pf);\n  voltage_unit : \"1V\";\n"
            "  current_unit : \"1mA\";\n  leakage_power_unit : \"1nW\";\n"
            "  pulling_resistance_unit : \"1kohm\";\n"
            + "\n".join(tdefs) + ("\n" if tdefs else "")
            + "\n".join(cells) + "\n}\n")


# ---------------------------------------------------------------------------
# Per-rail renaming for the chip view
# ---------------------------------------------------------------------------

_CELL_HDR = re.compile(r'(^[ \t]*cell\s*\(\s*)"?([A-Za-z0-9_]+)"?(\s*\))', re.M)
_LIB_HDR = re.compile(r'(^\s*library\s*\(\s*)"?([^")\s]+)"?(\s*\))', re.M)


def rename_liberty(text: str, prefix: str) -> Tuple[str, set]:
    """`text` with every cell (and the library) renamed `<prefix>__<name>`;
    returns (text, the original cell names)."""
    names = set()

    def cell(m):
        names.add(m.group(2))
        return f"{m.group(1)}{prefix}__{m.group(2)}{m.group(3)}"
    text = _CELL_HDR.sub(cell, text)
    text = _LIB_HDR.sub(lambda m: f"{m.group(1)}{prefix}__{m.group(2)}"
                        f"{m.group(3)}", text, count=1)
    return text, names


def rename_netlist(text: str, module_prefix: Dict[str, str],
                   cells: Dict[str, set]) -> str:
    """`text` with each cell instance in module M renamed to
    `<module_prefix[M]>__<cell>` when the cell is in cells[prefix]."""
    spans = sorted(vn.module_spans(text).items(), key=lambda kv: kv[1][0])
    out, pos = [], 0
    inst = re.compile(r"^(\s*)([A-Za-z_][\w$]*)(\s+(?:\\\S+|[A-Za-z_][\w$]*)\s*\()",
                      re.M)
    for name, (s, e) in spans:
        out.append(text[pos:s])
        pre = module_prefix.get(name)
        body = text[s:e]
        if pre is not None:
            known = cells.get(pre, set())
            body = inst.sub(lambda m: (f"{m.group(1)}{pre}__{m.group(2)}"
                                       f"{m.group(3)}" if m.group(2) in known
                                       else m.group(0)), body)
        out.append(body)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def top_view(text: str, mods, top: str, doms, des, ls_cells, modes,
             mode_names, mode_sdc_text: Dict[str, str], unit: str) -> dict:
    """The top's own domain of a power flow, as STA and vt_swap link it:
    {"netlist": the top and the modules under it but not under a domain,
     "boundary_lib": a liberty with each domain module as a pin-only cell,
     "sdc": {mode: the mode's SDC plus 0 delays at the domain pins},
     "modules": the modules in "netlist"}."""
    tmod = mods[top]
    dmods = [mods[x.module] for x in doms if x.module is not None]
    keep = submodules(mods, top)
    drop = set()
    for x in doms:
        if x.module is not None:
            drop |= set(submodules(mods, x.module))
    spans = vn.module_spans(text)
    kept = [m for m in keep if m not in drop]
    netlist = "\n".join(text[slice(*spans[m])] for m in kept) + "\n"
    blib = boundary_lib(dmods, "1" + unit if unit[0].isalpha() else unit)
    dom_clock: Dict[str, Dict[str, List[str]]] = {}
    for x in doms:
        if x.module is None:
            continue
        for inst in x.instances:
            r = trace_ports(tmod, des.pin_dir, inst, ls_cells)
            for mn in mode_names:
                cl = [c.name for c in modes[mn].clocks.values()
                      if any(s & set(c.ports) for s in r.values())]
                cl.sort(key=lambda n: modes[mn].clocks[n].period_ps)
                dom_clock.setdefault(inst, {})[mn] = cl
    reach_of = {inst: trace_ports(tmod, des.pin_dir, inst, ls_cells)
                for x in doms if x.module is not None for inst in x.instances}
    sdc = {}
    for mn in mode_names:
        extra = [f"# The top's own domain in mode {mn}: the mode's SDC, then "
                 "boundary delays (0) at the domain instances' pins."]
        clk_ports = {p for c in modes[mn].clocks.values() for p in c.ports}
        for x in doms:
            if x.module is None:
                continue
            m = mods[x.module]
            for inst in x.instances:
                cl = dom_clock[inst][mn] or sorted(
                    modes[mn].clocks, key=lambda n: modes[mn].clocks[n].period_ps)
                clk = cl[0]
                for p in m.ports:
                    k = m.decls[p].kind
                    obj = f"[get_pins {{{inst}/{p}" + ("[*]" if m.decls[p].msb
                                                       is not None else "") + "}]"
                    if k == "output":
                        extra.append(f"set_input_delay 0 -clock {clk} {obj}")
                    elif not reach_of[inst].get(p, set()) & clk_ports:
                        extra.append(f"set_output_delay 0 -clock {clk} {obj}")
        sdc[mn] = mode_sdc_text[mn] + "\n" + "\n".join(extra) + "\n"
    return {"netlist": netlist, "boundary_lib": blib, "sdc": sdc,
            "modules": kept}


def submodules(mods: Dict[str, vn.Module], root: str) -> List[str]:
    """`root` and every module of `mods` it instantiates, transitively."""
    seen, todo = [], [root]
    while todo:
        m = todo.pop()
        if m in seen or m not in mods:
            continue
        seen.append(m)
        todo += [i.cell for i in mods[m].instances if i.cell in mods]
    return seen


# ---------------------------------------------------------------------------
# Running OpenSTA
# ---------------------------------------------------------------------------


@dataclass
class View:
    label: str
    kind: str  # domain | chip
    domain: str
    corner: str
    mode: str
    libs: List[Path]
    netlist: Path
    top: str
    sdc: Path
    rail: str = ""
    module: str = ""
    warnings: List[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    # wire_model.WireModel put on every net of the view, or None.
    wire: Optional[object] = None
    # wire_rc.Scale: the model scaled to the view's corner's wire RC, and
    # the RC files that reads (mounted into the container); None / empty
    # without a pack wire_rc and wire_model.rc_corner.
    wire_scale: Optional[object] = None
    rc_files: List[Path] = field(default_factory=list)


# Worst hold slack by path class: register to register, from inputs, to
# outputs. With ideal clocks, the I/O classes are only as real as the
# mode's -min I/O delays (an input delay of 0 straight into a flop fails
# hold by the flop's hold time).
HOLD_SPLIT_TCL = r'''proc circtflow_ws {args} {
  set m ""
  foreach pe [eval find_timing_paths -path_delay min -group_path_count 1 -endpoint_path_count 1 $args] {
    set s [sta::time_sta_ui [$pe slack]]
    if {$m eq "" || $s < $m} { set m $s }
  }
  if {$m eq ""} { return none }
  return $m
}
puts "hold r2r [circtflow_ws -from [all_registers -clock_pins] -to [all_registers -data_pins]]"
puts "hold in [circtflow_ws -from [all_inputs]]"
puts "hold out [circtflow_ws -to [all_outputs]]"
'''


def _tcl(v: View, run_dir: Path) -> Path:
    from . import wire_model
    t = run_dir / f"{v.label}.tcl"
    t.write_text(
        "".join(f"read_liberty {l}\n" for l in v.libs)
        + f"read_verilog {v.netlist}\nlink_design {v.top}\n"
        f"read_sdc {v.sdc}\n"
        + wire_model.tcl(v.wire, str(run_dir / f"{v.label}.wire.spef"),
                         {"": v.wire_scale} if v.wire_scale else None)
        +
        "puts \"CIRCTFLOW-BEGIN\"\n"
        "report_worst_slack -max -digits 4\n"
        "report_tns -max -digits 4\n"
        "report_worst_slack -min -digits 4\n"
        + HOLD_SPLIT_TCL
        + ("puts \"CIRCTFLOW-ENDPOINTS\"\nreport_checks -path_delay max "
           "-format end -group_path_count 1000000 -endpoint_path_count 1 "
           "-digits 4\n" if v.kind == "chip" else "")
        + "puts \"CIRCTFLOW-END\"\nexit\n")
    return t


def _lib_unit_ns(lib: Path) -> float:
    from .modes import unit_ps
    with open(lib, errors="ignore") as f:
        head = f.read(1 << 16)
    m = re.search(r'time_unit\s*:\s*"?([^";]+)"?\s*;', head)
    return unit_ps(m.group(1).strip()) / 1000.0 if m else 1.0


def _num(s: str) -> Optional[float]:
    try:
        v = float(s)
    except ValueError:
        return None
    return None if abs(v) > 1e29 else v


def parse_log(text: str, unit_ns: float, kind: str) -> dict:
    body = text.split("CIRCTFLOW-BEGIN", 1)
    if len(body) != 2 or "CIRCTFLOW-END" not in body[1]:
        raise StaError("OpenSTA did not finish:\n" + text[-3000:])
    b = body[1]
    m = re.search(r"^worst slack max (\S+)", b, re.M)
    t = re.search(r"^tns max (\S+)", b, re.M)
    h = re.search(r"^worst slack min (\S+)", b, re.M)
    sc = lambda x: None if x is None else round(x * unit_ns, 4)
    res = {"wns_ns": sc(_num(m.group(1))) if m else None,
           "tns_ns": sc(_num(t.group(1))) if t else None,
           "hold_wns_ns": sc(_num(h.group(1))) if h else None}
    for cls in ("r2r", "in", "out"):
        hm = re.search(rf"^hold {cls} (\S+)", b, re.M)
        res[f"hold_{cls}_ns"] = (sc(_num(hm.group(1))) if hm and
                                 hm.group(1) != "none" else None)
    if kind == "chip":
        eps = []
        sect = b.split("CIRCTFLOW-ENDPOINTS", 1)[-1]
        for line in sect.splitlines():
            em = re.match(r"^(\S+)\s+\(\S+\)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+"
                          r"(-?[\d.]+)\s+\((MET|VIOLATED)\)", line)
            if em:
                eps.append((em.group(1), float(em.group(4)) * unit_ns))
        res["endpoints"] = eps
    errs = re.findall(r"^Error.*$", text, re.M)
    warns = re.findall(r"^Warning\b.*$", text, re.M)
    errs += [w for w in warns if FATAL_WARNING.search(w)]
    if errs:
        res["errors"] = errs[:10]
    if warns:
        res["sta_warnings"] = warns[:20]
    return res


def run_views(views: List[View], sta_dir: Path, image: str) -> None:
    """Run every view's Tcl through OpenSTA (one container for all) and fill
    each view's result."""
    run_dir = sta_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    tcls = [_tcl(v, run_dir) for v in views]
    script = run_dir / "run_all.sh"
    local = os.environ.get("OPENSTA")
    binary = local or STA_BIN
    script.write_text("#!/bin/sh\n" + "".join(
        f"{shlex.quote(binary)} -no_init -no_splash -exit {shlex.quote(str(t))} "
        f"> {shlex.quote(str(t.with_suffix('.log')))} 2>&1\n" for t in tcls))
    if local:
        cmd = ["sh", str(script)]
    else:
        mounts = {str(sta_dir.resolve())}
        for v in views:
            for p in [*v.libs, v.netlist, v.sdc, *v.rc_files]:
                mounts.add(str(Path(p).resolve().parent))
        # Parents cover children: mount the minimal set.
        ms = sorted(mounts)
        ms = [m for m in ms if not any(m != o and m.startswith(o + "/")
                                       for o in ms)]
        sd = str(sta_dir.resolve())
        rw = lambda m: sd == m or sd.startswith(m + "/")
        cmd = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
               "-e", "HOME=/tmp",
               *[a for m in ms for a in ("-v", f"{m}:{m}" if rw(m)
                                         else f"{m}:{m}:ro")],
               image, "sh", str(script.resolve())]
    print(f"[sta] OpenSTA: {len(views)} view(s) "
          + (f"with {local}" if local else f"in {image}"))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise StaError(f"OpenSTA run failed ({p.returncode}):\n{p.stderr[-3000:]}")
    for v, t in zip(views, tcls):
        log = t.with_suffix(".log")
        v.result = parse_log(log.read_text(), _lib_unit_ns(v.libs[0]),
                             v.kind)
        v.result["log"] = str(log)
        if v.result.get("errors"):
            raise StaError(f"OpenSTA errors in view {v.label} (see {log}):\n"
                           + "\n".join(v.result["errors"]))


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def check_names(spec: StaSpec, corners: Sequence[str],
                modes: Sequence[str], where) -> None:
    """Refuse an sta: section naming a corner or mode the run lacks (at
    config load, before synthesis)."""
    for what, want, names in (("corners", spec.corners, corners),
                              ("modes", spec.modes, modes)):
        bad = [w for w in want if w not in names]
        if bad:
            raise StaError(f"{where}: sta.{what}: {', '.join(bad)} not among "
                           + (", ".join(names) or "none"))


def corner_names(prof) -> List[str]:
    """The corners STA can time: the pack's, else the one `default`."""
    return _corners(prof)


def _select(names: Sequence[str], want: Sequence[str], what: str) -> List[str]:
    if not want:
        return list(names)
    bad = [w for w in want if w not in names]
    if bad:
        raise StaError(f"sta.{what}: {', '.join(bad)} not among "
                       + (", ".join(names) or "none"))
    return [n for n in names if n in want]


def _corners(prof) -> List[str]:
    return list(prof.corner_names) if prof is not None and \
        prof.corner_names else ["default"]


def _libs(prof, design, rail: str, corner: str) -> List[Path]:
    if prof is None:
        return [Path(p) for p in design.synth_lib_tt]
    return prof.rails[rail].libs_at(None if corner == "default" else corner)


def _mode_sdcs(cfg, top_mod: vn.Module, sta_dir: Path) -> Dict[str, Path]:
    """Each mode's SDC as the top sees it (the mode's file, or the
    shorthand written out)."""
    from . import intent
    ins = [p for p in top_mod.ports if top_mod.decls[p].kind == "input"]
    outs = [p for p in top_mod.ports if top_mod.decls[p].kind == "output"]
    paths = intent.write_mode_sdcs(sta_dir / "sdc", cfg.design.effective_modes(),
                                   cfg.design.top_module, cfg.design.time_unit,
                                   cfg.design.clock_port, ins, outs)
    return {m: Path(p) for m, p in paths.items()}


def run(cfg, spec: StaSpec, power_summary: Optional[dict] = None) -> Path:
    """Time the run's netlist per domain x corner x mode (and the chip view
    for a power flow). Writes <out>/sta/summary.json and prints a table."""
    from . import profile as profile_mod
    d = cfg.design
    top = d.top_module
    sta_dir = (cfg.out_dir / "sta").resolve()
    sta_dir.mkdir(parents=True, exist_ok=True)
    nl = cfg.openroad_netlist.resolve()
    if not nl.exists():
        raise StaError(f"sta: {nl} is missing (needs sv2v)")
    prof = profile_mod.load(d.profile) if d.profile is not None else None
    text = nl.read_text()
    mods = vn.parse(text)
    tmod = mods[top]
    modes = {m.name: m for m in d.effective_modes()}
    mode_names = _select(list(modes), spec.modes, "modes")
    corners = _select(_corners(prof), spec.corners, "corners")
    mode_sdc = _mode_sdcs(cfg, tmod, sta_dir)
    views: List[View] = []
    top_ins = [p for p in tmod.ports if tmod.decls[p].kind == "input"]
    top_outs = [p for p in tmod.ports if tmod.decls[p].kind == "output"]

    if power_summary is None:
        rail = d.rail or (prof.default_rail if prof else "default")
        for c in corners:
            for mn in mode_names:
                views.append(View(f"top.{mn}.{c}", "domain", "top", c, mn,
                                  _libs(prof, d, rail, c), nl, top,
                                  mode_sdc[mn], rail=rail, module=top))
    else:
        views += _power_views(cfg, prof, power_summary, text, mods, modes,
                              mode_names, corners, mode_sdc, top_ins,
                              top_outs, sta_dir, spec, nl)

    from . import wire_rc as wire_rc_mod
    wm = cfg.design.wire_model
    rc = getattr(d, "wire_rc", None)
    for v in views:
        v.wire = wm
        if wm is not None:
            v.wire_scale = wire_rc_mod.scale(rc, v.corner, wm.rc_corner)
            if v.wire_scale is not None:
                v.rc_files = [e.path for e in (rc.at(v.corner),
                                               rc.at(wm.rc_corner))
                              if e is not None and e.path is not None]
    run_views(views, sta_dir, spec.image)
    hold = (prof.hold_corner if prof is not None and prof.corner_names
            else corners[0])
    summary = _summarize(cfg, views, corners, mode_names, sta_dir, hold)
    summary["boundary_budget"] = spec.boundary_budget
    summary["wire_model"] = wm.to_dict() if wm is not None else None
    summary["wire_rc"] = rc.to_dict() if rc is not None else None
    js = sta_dir / "summary.json"
    js.write_text(json.dumps(summary, indent=2) + "\n")
    for line in table(summary):
        print(line)
    print(f"[sta] summary -> {js}")
    return js


def _power_views(cfg, prof, ps, text, mods, modes, mode_names, corners,
                 mode_sdc, top_ins, top_outs, sta_dir, spec, nl) -> List[View]:
    from . import intent
    d = cfg.design
    top = d.top_module
    tmod = mods[top]
    # Cell names, pins and domains come from the setup corner's shifter
    # libraries; each view reads the shifters at its own corner.
    ls_libs = prof.level_shifters_at()
    ls_info = intent.liberty_cells(ls_libs) if ls_libs else {}
    ls_cells = {}
    for c in ls_info.values():
        if c.is_level_shifter:
            try:
                ls_cells[c.name] = intent.shifter_pins(c)
            except intent.IntentError:
                pass
    doms = intent.domain_infos(prof, prof.default_rail, ps["domains"])
    des = intent.Design(text, top, doms,
                        prof.rails[prof.default_rail].liberties, ls_libs)
    views: List[View] = []
    ndir = sta_dir / "netlists"
    ndir.mkdir(parents=True, exist_ok=True)
    unit = d.time_unit
    # The domains, each alone.
    for dom in doms:
        if dom.module is None:
            continue
        mod = mods[dom.module]
        reach: Dict[str, set] = {}
        for inst in dom.instances:
            for p, s in trace_ports(tmod, des.pin_dir, inst, ls_cells).items():
                reach.setdefault(p, set()).update(s)
        for mn in mode_names:
            sdc_text, _, warns = domain_sdc(modes[mn], mod, reach, top_ins,
                                            top_outs, unit,
                                            spec.boundary_budget)
            sp = sta_dir / "sdc" / f"{dom.name}.{mn}.sdc"
            sp.write_text(sdc_text)
            for w in warns:
                print(f"[sta] WARNING: domain {dom.name}: {w}")
            for c in corners:
                views.append(View(f"{dom.name}.{mn}.{c}", "domain", dom.name,
                                  c, mn, _libs(prof, d, dom.rail, c), nl,
                                  dom.module, sp, rail=dom.rail,
                                  module=dom.module, warnings=warns))
    # The top's own domain: domain modules as boundary cells.
    tv = top_view(text, mods, top, doms, des, ls_cells, modes, mode_names,
                  {mn: Path(mode_sdc[mn]).read_text() for mn in mode_names},
                  unit)
    keep = submodules(mods, top)
    tnl = ndir / f"{top}_top_domain.v"
    tnl.write_text(tv["netlist"])
    blib = ndir / "boundary.lib"
    blib.write_text(tv["boundary_lib"])
    top_rail = prof.default_rail
    for mn in mode_names:
        sp = sta_dir / "sdc" / f"top.{mn}.sdc"
        sp.write_text(tv["sdc"][mn])
        for c in corners:
            views.append(View(f"top.{mn}.{c}", "domain", "top", c, mn,
                              _libs(prof, d, top_rail, c)
                              + prof.level_shifters_at(c) + [blib],
                              tnl, top, sp, rail=top_rail, module=top))
    if not spec.chip:
        return views
    # The chip: every cell renamed by rail.
    prefix = {m: top_rail for m in keep}
    for x in doms:
        if x.module is not None:
            for m in submodules(mods, x.module):
                prefix[m] = x.rail
    for c in corners:
        libs: List[Path] = []
        cells: Dict[str, set] = {}
        for rail in prof.rails:
            if rail not in prefix.values():
                continue
            ldir = sta_dir / "libs" / c / rail
            ldir.mkdir(parents=True, exist_ok=True)
            for lp in _libs(prof, d, rail, c):
                t, names = rename_liberty(Path(lp).read_text(errors="ignore"),
                                          rail)
                dst = ldir / Path(lp).name
                dst.write_text(t)
                libs.append(dst)
                cells.setdefault(rail, set()).update(names)
        cnl = ndir / f"{top}_chip_{c}.v"
        cnl.write_text(rename_netlist(text, prefix, cells))
        for mn in mode_names:
            views.append(View(f"chip.{mn}.{c}", "chip", "chip", c, mn,
                              libs + prof.level_shifters_at(c), cnl, top,
                              mode_sdc[mn]))
    return views


HOLD_KEYS = ("hold_r2r_ns", "hold_in_ns", "hold_out_ns")


def _summarize(cfg, views: List[View], corners, mode_names, sta_dir,
               hold_corner: Optional[str] = None) -> dict:
    rows, chip = [], []
    inst_dom: Dict[str, str] = {}
    pj = cfg.out_dir / "power_domains.json"
    if pj.exists():
        for r in json.loads(pj.read_text())["domains"]:
            for i in r.get("netlist_instances") or []:
                inst_dom[i] = r["name"]
    for v in views:
        if v.kind == "domain":
            rows.append({"domain": v.domain, "rail": v.rail,
                         "module": v.module, "corner": v.corner,
                         "mode": v.mode,
                         "wns_ns": v.result["wns_ns"],
                         "tns_ns": v.result["tns_ns"],
                         "hold_wns_ns": v.result["hold_wns_ns"],
                         **{k: v.result.get(k) for k in HOLD_KEYS},
                         "libs": [str(p) for p in v.libs],
                         "sdc": str(v.sdc), "log": v.result["log"],
                         **({"warnings": v.warnings} if v.warnings else {}),
                         **({"sta_warnings": v.result["sta_warnings"]}
                            if v.result.get("sta_warnings") else {})})
            continue
        by: Dict[str, dict] = {}
        for ep, slack in v.result.get("endpoints", []):
            dom = inst_dom.get(ep.split("/", 1)[0], "top") if "/" in ep \
                else "top"
            b = by.setdefault(dom, {"wns_ns": None, "tns_ns": 0.0,
                                    "endpoints": 0, "worst_endpoint": None})
            b["endpoints"] += 1
            if b["wns_ns"] is None or slack < b["wns_ns"]:
                b["wns_ns"], b["worst_endpoint"] = round(slack, 4), ep
            b["tns_ns"] = round(b["tns_ns"] + min(0.0, slack), 4)
        chip.append({"corner": v.corner, "mode": v.mode,
                     "wns_ns": v.result["wns_ns"],
                     "tns_ns": v.result["tns_ns"],
                     "hold_wns_ns": v.result["hold_wns_ns"],
                     **{k: v.result.get(k) for k in HOLD_KEYS},
                     "by_domain": by, "netlist": str(v.netlist),
                     **({"sta_warnings": v.result["sta_warnings"]}
                        if v.result.get("sta_warnings") else {}),
                     "sdc": str(v.sdc), "log": v.result["log"]})
    # Hold at the pack's hold corner (corners.hold, else the setup
    # corner): the worst over its views, by path class. With a chip view,
    # its rows only: a domain view's boundary delays are assumptions (a
    # port the mode leaves unconstrained gets 0, which fails hold by the
    # flop's hold time), the chip view times the real paths.
    src = chip if any(r["corner"] == hold_corner for r in chip) else rows
    hv = [r for r in src if r["corner"] == hold_corner]
    worst = lambda k: min((r[k] for r in hv if r.get(k) is not None),
                          default=None)
    hold = ({"corner": hold_corner, "views": "chip" if src is chip
             else "domain", "wns_ns": worst("hold_wns_ns"),
             **{k: worst(k) for k in HOLD_KEYS}} if hv else None)
    return {"top": cfg.design.top_module, "unit": "ns",
            "corners": corners, "modes": mode_names, "hold": hold,
            "method": ("domain rows: each domain module linked alone with "
                       "its rail's liberties at the corner, the mode's "
                       "clocks traced to its ports, boundary I/O delays "
                       "from the mode where a port reaches a top-level "
                       "port, else boundary_budget x the domain clock's "
                       "period; the top's row links the top "
                       "with domain modules as boundary cells. chip rows: "
                       "the whole netlist with cells renamed per rail, "
                       "the mode's SDC, endpoints attributed to domains."),
            "domains": rows, "chip": chip}


def _f(v) -> str:
    return "unconstr" if v is None else f"{v:+.4f}"


def table(summary: dict) -> List[str]:
    out = ["[sta] per domain (each linked alone on its rail's liberties):",
           f"[sta]   {'domain':<10} {'rail':<10} {'mode':<8} {'corner':<8} "
           f"{'WNS ns':>9} {'TNS ns':>9}"]
    for r in sorted(summary["domains"], key=lambda r: (
            r["domain"], r["mode"], summary["corners"].index(r["corner"]))):
        out.append(f"[sta]   {r['domain']:<10} {r['rail']:<10} {r['mode']:<8} "
                   f"{r['corner']:<8} {_f(r['wns_ns']):>9} "
                   f"{_f(r['tns_ns']):>9}")
    if summary["chip"]:
        out.append("[sta] chip (whole design, cells renamed per rail; "
                   "worst endpoint per domain):")
        doms = sorted({k for c in summary["chip"] for k in c["by_domain"]})
        out.append(f"[sta]   {'mode':<8} {'corner':<8} {'WNS ns':>9} "
                   f"{'TNS ns':>9}  " + "  ".join(f"{d + ' WNS':>12}"
                                                   for d in doms))
        for c in summary["chip"]:
            out.append(f"[sta]   {c['mode']:<8} {c['corner']:<8} "
                       f"{_f(c['wns_ns']):>9} {_f(c['tns_ns']):>9}  "
                       + "  ".join(f"{_f(c['by_domain'].get(d, {}).get('wns_ns')):>12}"
                                   for d in doms))
    h = summary.get("hold")
    if h:
        out.append(f"[sta] hold at {h['corner']} ({h['views']} view, ideal "
                   "clocks): worst "
                   f"{_f(h['wns_ns'])}; reg-to-reg {_f(h['hold_r2r_ns'])}, "
                   f"from inputs {_f(h['hold_in_ns'])}, to outputs "
                   f"{_f(h['hold_out_ns'])}")
    return out
