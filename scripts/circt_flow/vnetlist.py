"""A small structural-Verilog netlist model: parse, bit-level connectivity,
and re-emit.

It reads the gate-level form the flow writes for OpenSTA
(`<top>_openroad.v`, sv2v's output):

    module m (a, b, y);
        input [3:0] a;  input b;  output y;
        wire w;  wire [1:0] v;
        CELL u0 (.A(a[0]), .B({v, 1'b0}), .Y(w));
        assign y = w;
        assign v = a[3:2];
    endmodule

Declarations (input / output / inout / wire, with an optional [msb:lsb]),
instances with named connections (`.pin(expr)`), and `assign lhs = rhs;`.
An expression is an identifier, a bit or part select of one, a sized
constant (`4'b10x0`, `8'hff`, `1'd1`), a replication `{n{e}}` or a
concatenation of these. Anything else (behavioural code, positional
connections, parameters on instances) is refused with NetlistError: the
passes built on this (circt_flow.intent) must never silently drop
structure.

A bit is (name, index) with index None for a scalar, or ("'0", None),
("'1", None), ("'x", None), ("'z", None) for a constant bit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

Bit = Tuple[str, Optional[int]]
CONSTS = {"'0", "'1", "'x", "'z"}


class NetlistError(ValueError):
    """A netlist construct this model does not handle, or a netlist that is
    not well formed (an undeclared net, a bit out of range, two drivers)."""


@dataclass
class Decl:
    name: str
    kind: str  # input, output, inout, wire
    msb: Optional[int] = None
    lsb: Optional[int] = None

    @property
    def width(self) -> int:
        return 1 if self.msb is None else abs(self.msb - self.lsb) + 1

    def bits(self) -> List[Bit]:
        """MSB first."""
        if self.msb is None:
            return [(self.name, None)]
        step = -1 if self.msb >= self.lsb else 1
        return [(self.name, i) for i in range(self.msb, self.lsb + step, step)]


@dataclass
class Instance:
    cell: str
    name: str
    conns: List[Tuple[str, str]]  # (pin, expression text), in order


@dataclass
class Module:
    name: str
    ports: List[str]
    decls: Dict[str, Decl] = field(default_factory=dict)
    instances: List[Instance] = field(default_factory=list)
    assigns: List[Tuple[str, str]] = field(default_factory=list)

    def port_dir(self, port: str) -> Optional[str]:
        d = self.decls.get(port)
        return d.kind if d is not None and d.kind != "wire" else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_MODULE = re.compile(r"\bmodule\s+(\\\S+|[A-Za-z_][\w$]*)\s*(.*?)\bendmodule\b",
                     re.S)
_IDENT = r"(?:\\\S+|[A-Za-z_][\w$]*)"
_DECL = re.compile(r"^(input|output|inout|wire)\s+(?:wire\s+|reg\s+)?"
                   r"(?:\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]\s*)?(.*)$", re.S)
_INST = re.compile(r"^(" + _IDENT + r")\s+(" + _IDENT + r")\s*\((.*)\)$", re.S)


def strip_comments(text: str) -> str:
    return _COMMENT.sub(" ", text)


def _split_top(s: str, sep: str) -> List[str]:
    """`s` split on `sep` outside (), {} and []."""
    out, depth, cur = [], 0, []
    for c in s:
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        if c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur))
    return out


def parse_module(name: str, rest: str) -> Module:
    """`rest`: everything after `module <name>` up to endmodule."""
    m = re.match(r"\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;(.*)$", rest, re.S)
    if not m:
        if re.match(r"\s*;", rest):  # module m; (no ports)
            ports_s, body = "", rest.split(";", 1)[1]
        else:
            raise NetlistError(f"module {name}: cannot read its port list")
    else:
        ports_s, body = m.group(1), m.group(2)
    mod = Module(name, [])
    ansi = re.search(r"\b(input|output|inout)\b", ports_s)
    if ansi:
        # ANSI header: `input [3:0] a, b, output y`
        kind, rng = None, (None, None)
        for seg in _split_top(ports_s, ","):
            seg = seg.strip()
            dm = re.match(r"^(input|output|inout)\s+(?:wire\s+|reg\s+)?"
                          r"(?:\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]\s*)?(.*)$",
                          seg, re.S)
            if dm:
                kind = dm.group(1)
                rng = ((int(dm.group(2)), int(dm.group(3)))
                       if dm.group(2) is not None else (None, None))
                seg = dm.group(4).strip()
            elif (m2 := re.match(r"^\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]\s*(.*)$",
                                 seg, re.S)):
                rng = (int(m2.group(1)), int(m2.group(2)))
                seg = m2.group(3).strip()
            if kind is None or not re.fullmatch(_IDENT, seg):
                raise NetlistError(f"module {name}: cannot read port {seg!r}")
            mod.ports.append(seg)
            mod.decls[seg] = Decl(seg, kind, *rng)
    else:
        mod.ports = [p.strip() for p in ports_s.split(",") if p.strip()]
    for stmt in _split_top(body, ";"):
        st = " ".join(stmt.split())
        if not st:
            continue
        dm = _DECL.match(st)
        if dm:
            kind = dm.group(1)
            rng = ((int(dm.group(2)), int(dm.group(3)))
                   if dm.group(2) is not None else (None, None))
            for n in _split_top(dm.group(4), ","):
                n = n.strip()
                if "=" in n:
                    raise NetlistError(f"module {name}: `{kind} {n}` (a "
                                       "declaration with an assignment) is "
                                       "not supported")
                if not re.fullmatch(_IDENT, n):
                    raise NetlistError(f"module {name}: cannot read "
                                       f"declaration {st!r}")
                old = mod.decls.get(n)
                if old is not None and old.kind != "wire" and kind == "wire":
                    continue  # `output y; wire y;`
                mod.decls[n] = Decl(n, kind, *rng)
            continue
        am = re.match(r"^assign\s+(.*?)\s*=\s*(.*)$", st, re.S)
        if am:
            mod.assigns.append((am.group(1).strip(), am.group(2).strip()))
            continue
        im = _INST.match(st)
        if im:
            conns = []
            body_s = im.group(3).strip()
            if body_s:
                for c in _split_top(body_s, ","):
                    cm = re.fullmatch(r"\s*\.(" + _IDENT + r")\s*\((.*)\)\s*",
                                      c, re.S)
                    if not cm:
                        raise NetlistError(
                            f"module {name}: instance {im.group(2)}: only "
                            f"named connections are supported, got {c.strip()!r}")
                    conns.append((cm.group(1), cm.group(2).strip()))
            mod.instances.append(Instance(im.group(1), im.group(2), conns))
            continue
        raise NetlistError(f"module {name}: unsupported statement {st[:80]!r}")
    for p in mod.ports:
        if p not in mod.decls or mod.decls[p].kind == "wire":
            raise NetlistError(f"module {name}: port {p} has no direction")
    return mod


def parse(text: str) -> Dict[str, Module]:
    """{module name: Module} of every module in `text`, in file order."""
    out: Dict[str, Module] = {}
    for m in _MODULE.finditer(strip_comments(text)):
        if m.group(1) in out:
            raise NetlistError(f"module {m.group(1)} is defined twice")
        out[m.group(1)] = parse_module(m.group(1), m.group(2))
    return out


def module_spans(text: str) -> Dict[str, Tuple[int, int]]:
    """{module: (start, end)} of each module's text (`module` .. `endmodule`)
    in the ORIGINAL text, comments allowed outside modules."""
    out = {}
    for m in re.finditer(r"^\s*module\s+(\\\S+|[A-Za-z_][\w$]*)\b", text, re.M):
        e = re.compile(r"\bendmodule\b").search(text, m.end())
        if e is None:
            raise NetlistError(f"module {m.group(1)} has no endmodule")
        out[m.group(1)] = (m.start(), e.end())
    return out


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

_CONST = re.compile(r"^(\d+)?'([sS]?)([bBhHdDoO])([0-9a-fA-FxXzZ_?]+)$")


def _const_bits(w: Optional[str], base: str, digits: str) -> List[Bit]:
    digits = digits.replace("_", "").lower()
    base = base.lower()
    if base == "d":
        if not digits.isdigit():
            raise NetlistError(f"constant {digits!r}: x/z in a decimal")
        v = int(digits)
        n = int(w) if w else max(1, v.bit_length())
        s = format(v, "b").zfill(n)[-n:]
    else:
        per = {"b": 1, "o": 3, "h": 4}[base]
        s = ""
        for ch in digits:
            if ch in "xz?":
                s += ("x" if ch == "x" else "z") * per
            else:
                s += format(int(ch, 16 if base == "h" else 8 if base == "o"
                                else 2), "b").zfill(per)
        n = int(w) if w else len(s)
        if len(s) < n:
            pad = s[0] if s and s[0] in "xz" else "0"
            s = pad * (n - len(s)) + s
        s = s[-n:]
    return [("'" + ("0" if c == "0" else "1" if c == "1" else c), None)
            for c in s]


def bits_of(expr: str, mod: Module) -> List[Bit]:
    """The bits of `expr` in module `mod`, MSB first."""
    e = expr.strip()
    if not e:
        return []
    if e.startswith("{") and e.endswith("}"):
        inner = e[1:-1].strip()
        rm = re.match(r"^(\d+)\s*(\{.*\})$", inner, re.S)
        if rm:
            return bits_of(rm.group(2), mod) * int(rm.group(1))
        out: List[Bit] = []
        for part in _split_top(inner, ","):
            out += bits_of(part, mod)
        return out
    cm = _CONST.match(e.replace(" ", ""))
    if cm:
        return _const_bits(cm.group(1), cm.group(3), cm.group(4))
    if re.fullmatch(r"\d+", e):
        return _const_bits("32", "d", e)
    sm = re.fullmatch(r"(" + _IDENT + r")\s*(?:\[\s*(-?\d+)\s*(?::\s*(-?\d+)\s*)?\])?",
                      e, re.S)
    if not sm:
        raise NetlistError(f"module {mod.name}: unsupported expression {e!r}")
    name = sm.group(1)
    d = mod.decls.get(name)
    if d is None:
        raise NetlistError(f"module {mod.name}: {name} is not declared")
    if sm.group(2) is None:
        return d.bits()
    hi = int(sm.group(2))
    lo = int(sm.group(3)) if sm.group(3) is not None else hi
    if d.msb is None:
        raise NetlistError(f"module {mod.name}: {name} is a scalar; "
                           f"{e!r} selects a bit of it")
    allbits = {i for _, i in d.bits()}
    step = -1 if hi >= lo else 1
    sel = list(range(hi, lo + step, step))
    if any(i not in allbits for i in sel):
        raise NetlistError(f"module {mod.name}: {e!r} is out of range")
    return [(name, i) for i in sel]


def bit_str(b: Bit) -> str:
    n, i = b
    if n in CONSTS:
        return "1'b" + n[1]
    return n if i is None else f"{n}[{i}]"


def expr_of(bits: List[Bit], mod: Module) -> str:
    """A compact expression for `bits` (MSB first): whole vectors and
    descending runs of one vector as selects, constants as one literal per
    run, a concatenation when there is more than one piece."""
    pieces: List[str] = []
    i = 0
    while i < len(bits):
        n, idx = bits[i]
        if n in CONSTS:
            j = i
            while j < len(bits) and bits[j][0] in CONSTS:
                j += 1
            pieces.append(f"{j - i}'b" + "".join(b[0][1] for b in bits[i:j]))
            i = j
            continue
        if idx is None:
            pieces.append(n)
            i += 1
            continue
        j = i + 1
        while (j < len(bits) and bits[j][0] == n and bits[j][1] is not None
               and bits[j][1] == bits[j - 1][1] - 1):
            j += 1
        hi, lo = idx, bits[j - 1][1]
        d = mod.decls.get(n)
        if d is not None and d.msb is not None and (hi, lo) == (d.msb, d.lsb):
            pieces.append(n)
        elif hi == lo:
            pieces.append(f"{n}[{hi}]")
        else:
            pieces.append(f"{n}[{hi}:{lo}]")
        i = j
    if len(pieces) == 1:
        return pieces[0]
    return "{" + ", ".join(pieces) + "}"


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

# An endpoint: ("port", port name, index) for a module port bit, or
# ("pin", instance, pin, position) for bit `position` (0 = LSB) of an
# instance pin's connection.
Endpoint = Tuple


@dataclass
class Net:
    """One electrical net of a module: a set of bits joined by assigns."""
    bits: List[Bit]
    driver: Optional[Endpoint] = None  # None: undriven or constant
    const: Optional[str] = None  # "'0"/"'1"/... when a constant drives it
    loads: List[Endpoint] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The net's representative name (its first declared bit)."""
        return bit_str(self.bits[0]) if self.bits else "?"


class _UF:
    def __init__(self):
        self.p: Dict[Bit, Bit] = {}

    def find(self, x: Bit) -> Bit:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: Bit, b: Bit) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


PinDir = Callable[[str, str], Optional[str]]


def connectivity(mod: Module, pin_dir: PinDir
                 ) -> Tuple[Dict[Bit, Net], Dict[Endpoint, Bit]]:
    """The nets of `mod`: ({bit: its Net}, {endpoint: the bit it is on}).
    `pin_dir(cell, pin)` gives an instance pin's direction (input, output,
    inout) or None when unknown (an error). Port bits are endpoints too: an
    input port drives its net, an output port loads it."""
    uf = _UF()
    for d in mod.decls.values():
        for b in d.bits():
            uf.find(b)
    const_of: Dict[Bit, str] = {}
    for lhs, rhs in mod.assigns:
        lb, rb = bits_of(lhs, mod), bits_of(rhs, mod)
        if len(lb) != len(rb):
            raise NetlistError(f"module {mod.name}: assign {lhs} = {rhs}: "
                               f"widths {len(lb)} and {len(rb)} differ")
        for a, b in zip(lb, rb):
            if a[0] in CONSTS:
                raise NetlistError(f"module {mod.name}: assign to a constant")
            if b[0] in CONSTS:
                const_of[a] = b[0]
            else:
                uf.union(a, b)
    nets: Dict[Bit, Net] = {}

    def net(b: Bit) -> Net:
        r = uf.find(b)
        if r not in nets:
            nets[r] = Net([])
        return nets[r]

    for d in mod.decls.values():
        for b in d.bits():
            net(b).bits.append(b)
    for b, c in const_of.items():
        n = net(b)
        if n.const not in (None, c) or n.driver is not None:
            raise NetlistError(f"module {mod.name}: net {n.name} has two "
                               "drivers")
        n.const = c
    where: Dict[Endpoint, Bit] = {}

    def drive(n: Net, ep: Endpoint) -> None:
        if n.driver is not None or n.const is not None:
            raise NetlistError(
                f"module {mod.name}: net {n.name} has two drivers "
                f"({_ep_str(n.driver) if n.driver else n.const} and "
                f"{_ep_str(ep)})")
        n.driver = ep

    for p in mod.ports:
        d = mod.decls[p]
        for b in d.bits():
            ep = ("port", p, b[1])
            where[ep] = b
            if d.kind == "input":
                drive(net(b), ep)
            elif d.kind == "output":
                net(b).loads.append(ep)
            else:
                raise NetlistError(f"module {mod.name}: inout port {p} is "
                                   "not supported")
    for inst in mod.instances:
        for pin, e in inst.conns:
            bl = bits_of(e, mod)
            if not bl:
                continue
            dr = pin_dir(inst.cell, pin)
            if dr is None:
                raise NetlistError(f"module {mod.name}: {inst.name} "
                                   f"({inst.cell}): no direction for pin "
                                   f"{pin}")
            for pos, b in enumerate(reversed(bl)):
                ep = ("pin", inst.name, pin, pos)
                if b[0] in CONSTS:
                    if dr == "output":
                        raise NetlistError(f"module {mod.name}: output "
                                           f"{inst.name}/{pin} drives a "
                                           "constant")
                    continue
                where[ep] = b
                if dr == "output":
                    drive(net(b), ep)
                elif dr == "input":
                    net(b).loads.append(ep)
                else:
                    raise NetlistError(f"module {mod.name}: {inst.name}/"
                                       f"{pin} is {dr}; only input and "
                                       "output pins are supported")
    return {b: net(b) for b in uf.p}, where


def _ep_str(ep: Endpoint) -> str:
    if ep[0] == "port":
        return ep[1] if ep[2] is None else f"{ep[1]}[{ep[2]}]"
    return f"{ep[1]}/{ep[2]}" + ("" if ep[3] is None else f"[{ep[3]}]")


def ep_str(ep: Endpoint, widths: Optional[Dict[Tuple[str, str], int]] = None
           ) -> str:
    """An endpoint as text: `port[3]`, `inst/pin`, `inst/pin[3]` (the index
    left out for a one-bit pin when `widths` says so)."""
    if ep[0] == "pin" and widths is not None and \
            widths.get((ep[1], ep[2])) == 1:
        return f"{ep[1]}/{ep[2]}"
    return _ep_str(ep)


def pin_widths(mod: Module) -> Dict[Tuple[str, str], int]:
    return {(i.name, p): len(bits_of(e, mod))
            for i in mod.instances for p, e in i.conns}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _rng(d: Decl) -> str:
    return "" if d.msb is None else f"[{d.msb}:{d.lsb}] "


def write_module(mod: Module) -> str:
    """`mod` in the sv2v layout (non-ANSI ports)."""
    out = [f"module {mod.name} (\n"]
    out.append(",\n".join(f"\t{p}" for p in mod.ports))
    out.append("\n);\n")
    for p in mod.ports:
        d = mod.decls[p]
        out.append(f"\t{d.kind} {_rng(d)}{p};\n")
    for d in mod.decls.values():
        if d.kind == "wire" and d.name not in mod.ports:
            out.append(f"\twire {_rng(d)}{d.name};\n")
    for i in mod.instances:
        conns = ",\n".join(f"\t\t.{p}({e})" for p, e in i.conns)
        out.append(f"\t{i.cell} {i.name}(\n{conns}\n\t);\n")
    for lhs, rhs in mod.assigns:
        out.append(f"\tassign {lhs} = {rhs};\n")
    out.append("endmodule\n")
    return "".join(out)
