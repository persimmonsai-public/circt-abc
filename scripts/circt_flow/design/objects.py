"""Design objects for the query layer: registers, datapath operators, clock gates and mapped cells,
read from the flow's own stage files.

Three snapshots, each available once the stage that writes it has run:

  core IR    `<top>_core_deagg.mlir` (cfg.core_mlir; after elaboration):
             registers (`seq.firreg` / `seq.compreg`, RTL names), datapath
             operators (`comb.add/sub/mul/shl/shru/shrs`), RTL clock gates
             (an instance whose output is a register clock).
  struct IR  `<top>_struct.mlir` (cfg.struct_mlir; after structural mapping):
             the register cells register-mapper wrote
             (`<reg>[_cg<k>](_bit<i>|_mb<lo>_<hi>)`), the clock gates
             (inferred `clock_gate_<n>` and RTL ones) and what they drive.
  netlist    `<top>_openroad.v` (after syn_map): mapped cells.

Same mechanism as mlir_text.py (a line scanner over circt-opt's textual
printer output, one op per line; no MLIR bindings in this tree). Objects
are per module DEFINITION: an object in a module instantiated twice is one
object with two `instances` paths, which is also what every pass acts on
(the passes rewrite definitions).

Read-only; nothing here changes a stage file.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .mlir_text import Design

__all__ = ["Register", "Operator", "ClockGate", "RegisterCell", "MappedCell",
           "CoreSnapshot", "StructSnapshot", "parse_core", "parse_struct",
           "parse_netlist", "OPERATOR_KINDS"]

# comb op -> (kind, op)
OPERATOR_KINDS = {
    "comb.add": ("adder", "add"), "comb.sub": ("adder", "sub"),
    "comb.mul": ("multiplier", "mul"),
    "comb.shl": ("shifter", "shl"), "comb.shru": ("shifter", "shru"),
    "comb.shrs": ("shifter", "shrs"),
}

_MOD = re.compile(r'^\s*hw\.module(?:\.extern)?\s+(?:private\s+|public\s+)?@([\w$.\-]+)\s*\(')
_DEF = re.compile(r'^\s*(?P<res>%[^=]+?)\s*=\s*(?P<op>[\w.]+)\s*(?P<rest>.*)$')
_TYPE_I = re.compile(r':\s*i(\d+)\s*(?:\{[^}]*\})?\s*$')
_CONST = re.compile(r'^hw\.constant$')
_SSA = re.compile(r'%[\w$.\-]+')
_INST = re.compile(r'hw\.instance\s+"([^"]+)"\s+(?:sym\s+@[\w$.\-]+\s+)?@([\w$.\-]+)\s*\((.*?)\)\s*->')
_PIN = re.compile(r'([\w$.\-]+):\s*(%[\w$.\-]+|%[\w$.\-]+\.[\w$.\-]+)\s*:\s*i\d+')
# register-mapper's cell names: <reg>[_cg<k>](_bit<i> | _mb<lo>_<hi>)
_REGCELL = re.compile(r'^(?P<reg>.+?)(?:_cg(?P<cg>\d+))?(?:_bit(?P<bit>\d+)|_mb(?P<lo>\d+)_(?P<hi>\d+))$')
CLOCK_PINS = ("CK", "CLK", "CP", "C")


@dataclass(frozen=True)
class Register:
    """A register (one `seq.firreg` / `seq.compreg` of a module definition)."""
    name: str
    module: str
    width: int
    kind: str                       # firreg | compreg
    clock: str                      # port name, or "<instance>.<pin>" of a gate
    clock_gate: Optional[str]       # the RTL gate instance the clock comes from
    enable: Optional[str]           # hold-mux condition ("!x" when it holds on true)
    hold_mux: bool                  # next = mux(c, load, q): data gating can act on it
    reset: Optional[str]            # "async" | "sync" | None
    reset_signal: Optional[str]
    reset_value: Optional[str]
    instances: Tuple[str, ...]      # hierarchical paths of the register ("" root)
    line: int
    # filled from the struct IR when it exists: register-mapper's cells
    cells: Tuple["RegisterCell", ...] = ()

    @property
    def id(self) -> str:
        return f"{self.module}/{self.name}"

    @property
    def key(self):
        return (self.module, self.name)

    @property
    def multibit(self) -> Tuple[Tuple[str, int], ...]:
        """(cell instance, bits) of every register cell holding more than one bit."""
        return tuple((c.instance, c.bits) for c in self.cells if c.bits > 1)

    @property
    def gates(self) -> Tuple[str, ...]:
        """The clock gates driving this register's cells (struct IR)."""
        return tuple(sorted({c.clock for c in self.cells if c.clock}))


@dataclass(frozen=True)
class Operator:
    """A datapath operator (adder, multiplier, shifter) of a module definition."""
    kind: str                       # adder | multiplier | shifter
    op: str                         # add sub mul shl shru shrs
    width: int
    module: str
    index: int                      # k-th operator of this kind in the module (text order)
    result: str                     # its SSA name in the core IR (not stable across edits)
    operands: int
    const_operands: int
    instances: Tuple[str, ...]
    line: int

    @property
    def name(self) -> str:
        return f"{self.op}{self.index}"

    @property
    def id(self) -> str:
        return f"{self.module}/{self.kind}{self.index}"

    @property
    def key(self):
        return (self.module, self.kind, self.index)


@dataclass(frozen=True)
class RegisterCell:
    instance: str
    cell: str
    module: str
    register: str
    part: Optional[int]             # clock-gate-inference part (_cg<k>)
    lo: int                         # bit range inside the part
    hi: int
    clock: Optional[str]            # the gate instance driving CK (None: a plain clock)

    @property
    def bits(self) -> int:
        return self.hi - self.lo + 1


@dataclass(frozen=True)
class ClockGate:
    name: str                       # instance name
    module: str
    cell: str
    origin: str                     # inferred (clock_gate_<n>) | rtl
    enable: Optional[str]
    clock: Optional[str]            # what drives its clock input
    fanout: int                     # loads of its output (register cells and gates)
    registers: Tuple[str, ...]      # register names of the cells it clocks
    register_cells: Tuple[str, ...]
    bits: int                       # register bits it clocks
    instances: Tuple[str, ...]
    line: int

    @property
    def id(self) -> str:
        return f"{self.module}/{self.name}"

    @property
    def key(self):
        return (self.module, self.name)


@dataclass(frozen=True)
class MappedCell:
    name: str
    cell: str
    module: str

    @property
    def id(self) -> str:
        return f"{self.module}/{self.name}"

    @property
    def key(self):
        return (self.module, self.name)


# ---------------------------------------------------------------- scanning

@dataclass
class _ModuleBody:
    name: str
    header: str
    lines: List[Tuple[int, str]] = field(default_factory=list)
    defs: Dict[str, Tuple[str, str, int]] = field(default_factory=dict)   # ssa -> (op, rest, line)
    consts: Dict[str, str] = field(default_factory=dict)


def _modules(text: str) -> Dict[str, _ModuleBody]:
    mods: Dict[str, _ModuleBody] = {}
    cur: Optional[_ModuleBody] = None
    for i, line in enumerate(text.splitlines(), 1):
        m = _MOD.match(line)
        if m:
            cur = None
            if "hw.module.extern" in line or not line.rstrip().endswith("{"):
                continue
            cur = mods[m.group(1)] = _ModuleBody(m.group(1), line)
            continue
        if cur is None:
            continue
        if line.strip() == "}" and not line.startswith("    "):
            cur = None
            continue
        cur.lines.append((i, line))
        d = _DEF.match(line)
        if not d:
            continue
        rest = d.group("rest")
        for r in [x.strip() for x in d.group("res").split(",")]:
            cur.defs[r] = (d.group("op"), rest, i)
        if d.group("op") == "hw.constant":
            cur.consts[d.group("res").strip()] = rest.split(":")[0].strip()
    return mods


def _paths(design: Optional[Design]) -> Dict[str, Tuple[str, ...]]:
    """{module: (hierarchical path, ...)} from the instance tree."""
    out: Dict[str, List[str]] = {}
    if design is None:
        return {}

    def walk(n):
        out.setdefault(n.module_name, []).append(n.path)
        for c in n.children:
            walk(c)
    walk(design.hierarchy())
    return {k: tuple(sorted(v)) for k, v in out.items()}


def _design(path: Path, top: Optional[str]) -> Optional[Design]:
    try:
        return Design.from_mlir_file(path, top_name=top)
    except Exception:
        return None


def _port(mod: _ModuleBody, v: str) -> str:
    """An SSA value as a readable signal: a port (block argument) by its name,
    a `seq.to_clock` / `comb.xor x, true` looked through, an instance result as
    `<inst>.<pin>`."""
    seen = 0
    while v in mod.defs and seen < 4:
        op, rest, _ = mod.defs[v]
        seen += 1
        if op == "seq.to_clock":
            v = rest.split()[0]
            continue
        if op == "hw.instance":
            return v.lstrip("%")
        if op == "comb.xor":
            args = [a.strip() for a in rest.split(":")[0].replace("bin ", "").split(",")]
            if len(args) == 2 and mod.consts.get(args[1]) == "true":
                return "!" + _port(mod, args[0])
        break
    if v in mod.consts:
        return mod.consts[v]
    return v.lstrip("%")


def _const_value(mod: _ModuleBody, v: str) -> str:
    if v in mod.consts:
        c = mod.consts[v]
        return {"false": "0", "true": "1"}.get(c, c)
    return v.lstrip("%")


@dataclass
class CoreSnapshot:
    path: Path
    registers: List[Register]
    operators: List[Operator]
    rtl_gates: List[ClockGate]
    modules: Dict[str, Tuple[str, ...]]        # module -> instance paths


_FIRREG = re.compile(r'^(?P<next>%[\w$.\-]+)\s+clock\s+(?P<clk>%[\w$.\-]+)'
                     r'(?:\s+sym\s+@[\w$.\-]+)?'
                     r'(?:\s+reset\s+(?P<rkind>sync|async)\s+(?P<rst>%[\w$.\-]+)\s*,\s*(?P<rval>%[\w$.\-]+))?')
_COMPREG = re.compile(r'^(?:sym\s+@[\w$.\-]+\s+)?(?P<next>%[\w$.\-]+)\s*,\s*(?P<clk>%[\w$.\-]+)'
                      r'(?:\s+reset\s+(?P<rst>%[\w$.\-]+)\s*,\s*(?P<rval>%[\w$.\-]+))?')


def parse_core(path, top: Optional[str] = None) -> CoreSnapshot:
    path = Path(path)
    text = path.read_text()
    mods = _modules(text)
    paths = _paths(_design(path, top))
    regs: List[Register] = []
    ops: List[Operator] = []
    gates: Dict[Tuple[str, str], dict] = {}
    for mname in sorted(mods):
        mod = mods[mname]
        inst = paths.get(mname, ())
        counts: Dict[str, int] = {}
        for i, line in mod.lines:
            d = _DEF.match(line)
            if not d:
                continue
            op, rest = d.group("op"), d.group("rest")
            res = d.group("res").strip()
            tm = _TYPE_I.search(rest)
            width = int(tm.group(1)) if tm else 0
            if op in ("seq.firreg", "seq.compreg"):
                rx = (_FIRREG if op == "seq.firreg" else _COMPREG).match(rest)
                if not rx:
                    continue
                nm = re.search(r'\bname\s*=?\s*"([^"]+)"', rest)
                name = nm.group(1) if nm else res.lstrip("%")
                q = res
                nxt = rx.group("next")
                enable, hold = None, False
                if nxt in mod.defs and mod.defs[nxt][0] == "comb.mux":
                    args = [a.strip() for a in mod.defs[nxt][1].split(":")[0]
                            .replace("bin ", "").split(",")]
                    if len(args) == 3:
                        c, t, f = args
                        if f == q and t != q:
                            enable, hold = _port(mod, c), True
                        elif t == q and f != q:
                            enable, hold = "!" + _port(mod, c), True
                clk = _port(mod, rx.group("clk"))
                gate = clk.split(".")[0] if "." in clk else None
                if gate:
                    gates.setdefault((mname, gate), {"regs": set(), "line": i})["regs"].add(name)
                rkind = rx.groupdict().get("rkind") or ("sync" if op == "seq.compreg"
                                                        and rx.group("rst") else None)
                regs.append(Register(
                    name=name, module=mname, width=width,
                    kind=op.split(".")[1], clock=clk, clock_gate=gate,
                    enable=enable, hold_mux=hold and op == "seq.firreg",
                    reset=rkind if rx.group("rst") else None,
                    reset_signal=_port(mod, rx.group("rst")) if rx.group("rst") else None,
                    reset_value=_const_value(mod, rx.group("rval")) if rx.group("rval") else None,
                    instances=tuple(f"{p}/{name}" if p else name for p in inst),
                    line=i))
            elif op in OPERATOR_KINDS:
                kind, o = OPERATOR_KINDS[op]
                counts[kind] = counts.get(kind, 0) + 1
                args = [a.strip() for a in rest.split(":")[0].replace("bin ", "").split(",")
                        if a.strip()]
                ops.append(Operator(
                    kind=kind, op=o, width=width, module=mname, index=counts[kind] - 1,
                    result=res.lstrip("%"), operands=len(args),
                    const_operands=sum(1 for a in args if a in mod.consts),
                    instances=tuple(f"{p}/{o}{counts[kind] - 1}" if p else f"{o}{counts[kind] - 1}"
                                    for p in inst),
                    line=i))
    # RTL clock gates: instances whose result clocks a register
    rtl = []
    for (mname, gname), info in sorted(gates.items()):
        mod = mods[mname]
        line = next((ln for ln_no, ln in mod.lines if f'hw.instance "{gname}"' in ln), "")
        im = _INST.search(line)
        pins = dict(_PIN.findall(im.group(3))) if im else {}
        rtl.append(ClockGate(
            name=gname, module=mname, cell=im.group(2) if im else "",
            origin="rtl", enable=_port(mod, pins["E"]) if "E" in pins else None,
            clock=next((_port(mod, pins[p]) for p in CLOCK_PINS if p in pins), None),
            fanout=len(info["regs"]), registers=tuple(sorted(info["regs"])),
            register_cells=(), bits=sum(r.width for r in regs
                                        if r.module == mname and r.clock_gate == gname),
            instances=tuple(f"{p}/{gname}" if p else gname for p in paths.get(mname, ())),
            line=info["line"]))
    return CoreSnapshot(path, regs, ops, rtl, paths)


@dataclass
class StructSnapshot:
    path: Path
    gates: List[ClockGate]
    cells: List[RegisterCell]


def parse_struct(path, top: Optional[str] = None, known_registers=None) -> StructSnapshot:
    """Clock gates and register cells of the struct IR. `known_registers`:
    {(module, name)} from the core IR, so a cell name is only read as a
    register cell when its stem names a real register of its module."""
    path = Path(path)
    text = path.read_text()
    mods = _modules(text)
    paths = _paths(_design(path, top))
    cells: List[RegisterCell] = []
    gates: List[ClockGate] = []
    for mname in sorted(mods):
        mod = mods[mname]
        insts = []
        for i, line in mod.lines:
            im = _INST.search(line)
            if im:
                insts.append((i, im.group(1), im.group(2), dict(_PIN.findall(im.group(3)))))
        reg_cells = []
        for i, iname, cell, pins in insts:
            m = _REGCELL.match(iname)
            ck = next((p for p in CLOCK_PINS if p in pins), None)
            if not m or ck is None:
                continue
            reg = m.group("reg")
            if known_registers is not None and (mname, reg) not in known_registers:
                continue
            lo = int(m.group("bit") if m.group("bit") is not None else m.group("lo"))
            hi = int(m.group("bit") if m.group("bit") is not None else m.group("hi"))
            clk = pins[ck]
            rc = RegisterCell(iname, cell, mname, reg,
                              int(m.group("cg")) if m.group("cg") else None, lo, hi,
                              clk.lstrip("%").split(".")[0] if "." in clk else None)
            reg_cells.append((rc, clk))
            cells.append(rc)
        # a clock gate: an instance with a clock input whose output is the
        # clock of a register cell or of another gate
        drivers = {}
        for i, iname, cell, pins in insts:
            if any(p in pins for p in CLOCK_PINS):
                drivers[iname] = (i, cell, pins)
        loads: Dict[str, List[str]] = {}
        for rc, clk in reg_cells:
            if "." in clk:
                loads.setdefault(clk.lstrip("%").split(".")[0], []).append(rc.instance)
        changed = True
        gate_names = {g for g in loads if g in drivers and g not in {rc.instance for rc, _ in reg_cells}}
        gate_names |= {n for n in drivers if re.fullmatch(r"clock_gate_\d+", n)}
        while changed:
            changed = False
            for iname, (i, cell, pins) in drivers.items():
                ck = next((pins[p] for p in CLOCK_PINS if p in pins), "")
                src = ck.lstrip("%").split(".")[0] if "." in ck else None
                if src in drivers and iname in gate_names and src not in gate_names:
                    gate_names.add(src)
                    changed = True
        for gname in sorted(gate_names):
            i, cell, pins = drivers[gname]
            ck = next((pins[p] for p in CLOCK_PINS if p in pins), None)
            sub = [n for n in drivers if n in gate_names and
                   any(v.startswith(f"%{gname}.") for p, v in drivers[n][2].items() if p in CLOCK_PINS)]
            rcs = [rc for rc, clk in reg_cells if clk.startswith(f"%{gname}.")]
            gates.append(ClockGate(
                name=gname, module=mname, cell=cell,
                origin="inferred" if re.fullmatch(r"clock_gate_\d+", gname) else "rtl",
                enable=_port(mod, pins["E"]) if "E" in pins else None,
                clock=_port(mod, ck) if ck else None,
                fanout=len(rcs) + len(sub),
                registers=tuple(sorted({rc.register for rc in rcs})),
                register_cells=tuple(sorted(rc.instance for rc in rcs)),
                bits=sum(rc.bits for rc in rcs),
                instances=tuple(f"{p}/{gname}" if p else gname for p in paths.get(mname, ())),
                line=i))
    return StructSnapshot(path, gates, cells)


def parse_netlist(path) -> List[MappedCell]:
    from .. import vnetlist
    mods = vnetlist.parse(Path(path).read_text())
    out = []
    for mname in sorted(mods):
        for inst in mods[mname].instances:
            out.append(MappedCell(inst.name, inst.cell, mname))
    return out


def match(pattern: Optional[str], *names: str) -> bool:
    """fnmatch `pattern` against any of `names` (None matches everything)."""
    if pattern is None:
        return True
    return any(fnmatch.fnmatchcase(n, pattern) for n in names if n is not None)
