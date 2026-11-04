"""Pre-placement fanout buffering of a flat mapped netlist.

ABC maps each partition on its own and never sees a net that leaves it,
so a flop or gate whose output fans out to hundreds of pins across
partitions reaches STA unbuffered, its whole load on one output, and on
a large design those nets, not the logic, set the worst path. P&R's
repair_design buffers them on a placement; before P&R this stage builds a
balanced tree instead, so timing (and the vt_swap stage, which can only
change flavours) sees a realistic load on every driver.

Every net driven by one cell output pin with more than `max_fanout` cell
input pins, none of them a clock pin (`clock : true`; clock trees are
CTS's), gets a tree of `buffer` cells: the loads, ordered by instance
name so that pins of one hierarchy branch share a buffer, go in groups of
at most `max_fanout`, each group driven by a buffer, until the driver has
at most `max_fanout` loads. The driver keeps its net, so ports and
`assign`s that read it are untouched; only cell input pins move.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# The pack's flow.fanout_buffer (and a flow config's fanout_buffer:, which
# overrides it key by key): the buffer cell, its pins and the fanout limit.
SPEC_KEYS = {"enabled", "cell", "input", "output", "max_fanout"}


@dataclass(frozen=True)
class FanoutSpec:
    cell: str
    input: str = "A"
    output: str = "Y"
    max_fanout: int = 16
    enabled: bool = True


def parse_spec(pack: Optional[dict], design, where: str
               ) -> Optional[FanoutSpec]:
    """The merged spec, or None when neither sets one or it is disabled.
    `design` may be a mapping, False (off) or None."""
    if design is False or (isinstance(design, dict)
                           and design.get("enabled") is False):
        return None
    merged = {}
    for src, w in ((pack, "pack flow.fanout_buffer"),
                   (design, "fanout_buffer")):
        if src is None or src is True:
            continue
        if not isinstance(src, dict):
            raise KeyError(f"{where}: {w} must be a mapping")
        bad = set(src) - SPEC_KEYS
        if bad:
            raise KeyError(f"{where}: {w}: unknown key(s) "
                           f"{', '.join(sorted(bad))}; allowed: "
                           f"{', '.join(sorted(SPEC_KEYS))}")
        merged.update(src)
    merged.pop("enabled", None)
    if not merged:
        return None
    if not merged.get("cell"):
        raise KeyError(f"{where}: fanout_buffer needs a cell (the pack's "
                       "flow.fanout_buffer.cell, or the flow config's)")
    mf = merged.get("max_fanout", 16)
    if not isinstance(mf, int) or isinstance(mf, bool) or mf < 2:
        raise KeyError(f"{where}: fanout_buffer.max_fanout must be an "
                       "integer >= 2")
    return FanoutSpec(cell=str(merged["cell"]),
                      input=str(merged.get("input", "A")),
                      output=str(merged.get("output", "Y")),
                      max_fanout=mf)

_STMT = re.compile(r"^[ \t]*([A-Za-z_]\w*)[ \t]+(\\\S+|[A-Za-z_]\w*)\s*\("
                   r"(.*?)\)\s*;", re.M | re.S)
_CONN = re.compile(r"\.(\w+)\s*\(\s*((?:\\\S+\s)?[^()]*?)\s*\)")
_CONST = re.compile(r"^\d*'[bhdoBHDO][0-9a-fA-FxXzZ_]+$|^\d+$")


@dataclass
class Buffering:
    nets: int = 0          # nets buffered
    buffers: int = 0       # buffers inserted
    worst_fanout: int = 0  # largest fanout before
    top: List[Tuple[str, int]] = field(default_factory=list)


def buffer_fanout(text: str, cells: Dict[str, "object"], buffer: str,
                  buf_in: str, buf_out: str, max_fanout: int = 16,
                  prefix: str = "fob_", driven_ports: Sequence[str] = ()
                  ) -> Tuple[str, Buffering]:
    """Return (`text` with fanout trees, stats). `cells` maps cell name to an
    object with `.pins` {pin: direction} (intent.LibCell). Nets of
    `driven_ports` (input ports, bits included) count as driven once: a
    submodule's inputs, driven by a cell in its parent."""
    if max_fanout < 2:
        raise ValueError("max_fanout must be at least 2")
    # Pass 1: drivers and loads of every net.
    drivers: Dict[str, int] = {}
    clock_nets = set()   # nets reaching a clock pin: CTS builds those trees
    loads: Dict[str, List[Tuple[str, str, int]]] = {}  # net -> (inst, pin, stmt)
    stmts = []
    for m in _STMT.finditer(text):
        cell = cells.get(m.group(1))
        if cell is None:
            continue
        idx = len(stmts)
        stmts.append(m)
        for c in _CONN.finditer(m.group(3)):
            pin, net = c.group(1), c.group(2).strip()
            if not net or _CONST.match(net) or net.startswith("{"):
                continue
            d = cell.pins.get(pin)
            if d == "output":
                drivers[net] = drivers.get(net, 0) + 1
            elif d == "input":
                loads.setdefault(net, []).append((m.group(2), pin, idx))
                if pin in getattr(cell, "clock_pins", ()):
                    clock_nets.add(net)
    ports = set(driven_ports)
    for net in loads:
        if net.split("[")[0].strip() in ports and net not in drivers:
            drivers[net] = 1
    taken = set(re.findall(r"\b" + re.escape(prefix) + r"\w+", text))

    def fresh(kind: str, n: int) -> str:
        name = f"{prefix}{kind}{n}"
        if name in taken:
            raise ValueError(f"fanout: {name} already in the netlist")
        return name

    st = Buffering()
    moves: Dict[int, Dict[str, str]] = {}   # stmt -> {pin: new net}
    new_wires: List[str] = []
    new_insts: List[str] = []
    nb = nn = 0
    big = sorted(((n, len(l)) for n, l in loads.items()
                  if len(l) > max_fanout and drivers.get(n) == 1
                  and n not in clock_nets),
                 key=lambda x: -x[1])
    for net, fo in big:
        st.nets += 1
        st.worst_fanout = max(st.worst_fanout, fo)
        if len(st.top) < 10:
            st.top.append((net, fo))
        level = sorted(loads[net], key=lambda l: l[0])
        # Leaves: (inst, pin, stmt) pins to drive; build upwards.
        while len(level) > max_fanout:
            groups = [level[i:i + max_fanout]
                      for i in range(0, len(level), max_fanout)]
            # Balance group sizes so no buffer drives a sliver.
            k = len(groups)
            size = -(-len(level) // k)
            groups = [level[i:i + size] for i in range(0, len(level), size)]
            up = []
            for g in groups:
                w = fresh("n", nn); nn += 1
                b = fresh("b", nb); nb += 1
                new_wires.append(w)
                for inst, pin, si in g:
                    if si is None:      # a buffer made one level down
                        new_insts[pin] = new_insts[pin].replace("@IN@", w)
                    else:
                        moves.setdefault(si, {})[pin] = w
                new_insts.append(f"  {buffer} {b} (.{buf_in}(@IN@), "
                                 f".{buf_out}({w}));")
                up.append((b, len(new_insts) - 1, None))
            level = up
        for inst, pin, si in level:   # the driver's own loads
            if si is None:
                new_insts[pin] = new_insts[pin].replace("@IN@", net)
    st.buffers = nb
    if not big:
        return text, st
    # Pass 2: rewrite the moved pins, add the wires and the buffers.
    out, pos = [], 0
    for si, pins in sorted(moves.items()):
        m = stmts[si]
        body = _CONN.sub(lambda c: (f".{c.group(1)}({pins[c.group(1)]})"
                                    if c.group(1) in pins else c.group(0)),
                         m.group(3))
        out.append(text[pos:m.start(3)])
        out.append(body)
        pos = m.end(3)
    out.append(text[pos:])
    text = "".join(out)
    end = text.rfind("endmodule")
    if end < 0:
        raise ValueError("fanout: no endmodule")
    first = stmts[0].start()
    decl = "".join(f"  wire {w};\n" for w in new_wires)
    text = (text[:first] + decl + text[first:end]
            + "\n".join(new_insts) + "\n" + text[end:])
    return text, st


def used_cells(text: str) -> set:
    return {m.group(1) for m in _STMT.finditer(text)}


def run(cfg, spec: FanoutSpec) -> dict:
    """The flow stage: buffer the run's OpenSTA netlist in place, keeping
    the unbuffered one as <top>_prebuf_openroad.v; writes <out>/fanout.json.
    """
    from . import intent, profile as profile_mod
    d = cfg.design
    nl = cfg.openroad_netlist
    if not nl.exists():
        raise ValueError(f"fanout: {nl} is missing (needs sv2v)")
    prof = profile_mod.load(d.profile)
    libs = prof.libs_at(rail=d.rail)
    text = nl.read_text()
    if text.count("endmodule") != 1:
        raise ValueError(f"fanout: {nl} is not flat (one module)")
    cells = intent.liberty_cells(libs, only=used_cells(text) | {spec.cell})
    if spec.cell not in cells:
        raise ValueError(f"fanout: buffer cell {spec.cell} is in none of "
                         f"{', '.join(str(l) for l in libs)}")
    pins = cells[spec.cell].pins
    if pins.get(spec.input) != "input" or pins.get(spec.output) != "output":
        raise ValueError(f"fanout: {spec.cell} has no input {spec.input} "
                         f"and output {spec.output}")
    out, st = buffer_fanout(text, cells, spec.cell, spec.input, spec.output,
                            spec.max_fanout)
    keep = cfg.synth_dir / f"{d.top_module}_prebuf_openroad.v"
    keep.write_text(text)
    nl.write_text(out)
    res = {"cell": spec.cell, "max_fanout": spec.max_fanout,
           "nets": st.nets, "buffers": st.buffers,
           "worst_fanout": st.worst_fanout,
           "largest": [{"net": n, "fanout": f} for n, f in st.top],
           "unbuffered_netlist": str(keep), "netlist": str(nl)}
    (cfg.out_dir / "fanout.json").write_text(json.dumps(res, indent=2)
                                             + "\n")
    print(f"[fanout] {st.nets} net(s) over {spec.max_fanout} loads (worst "
          f"{st.worst_fanout}): {st.buffers} {spec.cell} -> {nl} "
          f"(unbuffered: {keep})")
    return res


def buffer_modules(text: str, top: str, rail_of: Dict[str, str],
                   cells_of: Dict[str, dict], spec: FanoutSpec
                   ) -> Tuple[str, dict]:
    """Buffer each module of `text` named in `rail_of` on its own, with the
    cells of its rail (`cells_of[rail]`); returns (text, {module: stats})
    for the modules that changed."""
    from . import vnetlist as vn
    spans = vn.module_spans(text)
    mods = vn.parse(text)
    per = {}
    out = text
    for m, (a, b) in sorted(spans.items(), key=lambda kv: -kv[1][0]):
        if m not in rail_of:
            continue
        # A module's inputs are driven by its parent, except the top's
        # (primary inputs, left alone as in a flat run).
        ins = [] if m == top else [
            q for q in mods[m].ports if mods[m].decls[q].kind == "input"]
        new, st = buffer_fanout(out[a:b], cells_of[rail_of[m]], spec.cell,
                                spec.input, spec.output, spec.max_fanout,
                                driven_ports=ins)
        if st.nets:
            out = out[:a] + new + out[b:]
            per[m] = {"rail": rail_of[m], "nets": st.nets,
                      "buffers": st.buffers, "worst_fanout": st.worst_fanout,
                      "largest": [{"net": n, "fanout": f}
                                  for n, f in st.top]}
    return out, per


def run_power(cfg, spec: FanoutSpec, power_summary: dict) -> dict:
    """The power flow's stage: buffer each module of the stitched OpenSTA
    netlist on its own, with its rail's libraries (the top and the glue on
    the default rail, each domain's modules on the domain's). Nets that
    cross a module boundary end on module ports, which are not cell pins,
    so a net is buffered on the side of its loads: a submodule's input
    port counts as a driver (a flop's net enters the ABC residual module
    through one), and the tree is built inside the submodule, on its rail.
    Domain crossings keep their level shifters; a tree only follows them.
    A module under domains of two rails is not buffered."""
    from . import intent, profile as profile_mod, sta, vnetlist as vn
    d = cfg.design
    prof = profile_mod.load(d.profile)
    nl = cfg.openroad_netlist
    if not nl.exists():
        raise ValueError(f"fanout: {nl} is missing (needs sv2v)")
    top = d.top_module
    text = nl.read_text()
    mods = vn.parse(text)
    doms = intent.domain_infos(prof, prof.default_rail,
                               power_summary["domains"])
    rail_of = {m: prof.default_rail for m in sta.submodules(mods, top)}
    owners: dict = {}
    for dom in doms:
        if dom.module is not None:
            for m in sta.submodules(mods, dom.module):
                rail_of[m] = dom.rail
                owners.setdefault(m, set()).add(dom.rail)
    shared = {m for m, o in owners.items() if len(o) > 1}
    for m in sorted(shared):
        print(f"[fanout] WARNING: module {m} is under domains on rails "
              f"{', '.join(sorted(owners[m]))}; not buffered")
    used = used_cells(text) | {spec.cell}
    corner = prof.setup_corner
    cells_of = {}
    for rail in sorted(set(rail_of.values())):
        libs = list(prof.libs_at(corner, rail=rail)) + \
            list(prof.level_shifters)
        cells = intent.liberty_cells(libs, only=used)
        c = cells.get(spec.cell)
        if c is None or c.pins.get(spec.input) != "input" or \
                c.pins.get(spec.output) != "output":
            raise ValueError(f"fanout: rail {rail} has no buffer {spec.cell} "
                             f"with input {spec.input} and output "
                             f"{spec.output}")
        cells_of[rail] = cells
    out, per = buffer_modules(text, top, {m: r for m, r in rail_of.items()
                                     if m not in shared}, cells_of, spec)
    keep = cfg.synth_dir / f"{top}_prebuf_openroad.v"
    keep.write_text(text)
    nl.write_text(out)
    res = {"cell": spec.cell, "max_fanout": spec.max_fanout,
           "nets": sum(v["nets"] for v in per.values()),
           "buffers": sum(v["buffers"] for v in per.values()),
           "modules": per, "shared_modules": sorted(shared),
           "unbuffered_netlist": str(keep), "netlist": str(nl)}
    (cfg.out_dir / "fanout.json").write_text(json.dumps(res, indent=2)
                                             + "\n")
    print(f"[fanout] {res['nets']} net(s) over {spec.max_fanout} loads in "
          f"{len(per)} module(s): {res['buffers']} {spec.cell} -> {nl} "
          f"(unbuffered: {keep})")
    return res
