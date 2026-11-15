"""Object queries and scoped commands.

    from circt_flow import query
    d = query.design_of(s)                 # s: a Session, a FlowConfig, or a DesignDB
    regs = d.flops("total_r")              # Collection of Register
    for sl in regs.bits(0, 11).split(4):   # total_r[3:0], total_r[7:4], total_r[11:8]
        query.gate_register(s, sl, detect="zero")
    query.map_adders(s, d.adders(width=(17, 60)), form="prefix", arch="HAN-CARLSON")
    query.map_adders(s, d.adders(width=(5, 12)), form="cells")
    d.icgs(registers="total_r")            # clock gates (after the structural stage)

The session exposes the same as methods (s.flops(...), s.adders(...),
s.gate_register(...), ...), which call the module functions here with the
session as first argument.

Collections are immutable, ordered by a stable key (module, name / index),
compose with Python (iteration, comprehensions) and with | & - (same kind),
and filter with `.filter(pattern, **attributes)` / `.where(predicate)`. An
empty collection is falsy and says so in its repr; a scoped command on an
empty collection is an error unless `allow_empty=True` (then it records
nothing). A query whose snapshot does not exist yet raises StageNotRun.

What is available when:
  after elaborate (core IR):        flops/registers, adders, multipliers,
                                    shifters, operators, modules, RTL clock gates
  after map_datapath (struct IR):   icgs (inferred and RTL gates, fan-out,
                                    gated registers), register cells
                                    (Register.cells / .multibit / .gates)
  after syn_map (netlist):          cells (mapped cell instances)
"""
from __future__ import annotations

import fnmatch
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import scoping
from .design import objects
from .options import OptionError

__all__ = ["QueryError", "StageNotRun", "Collection", "RegisterSlice", "DesignDB",
           "design_of", "flops", "registers", "adders", "multipliers", "shifters",
           "operators", "icgs", "cells", "modules", "gate_register", "map_adders",
           "materialise", "scoped_of"]


class QueryError(OptionError):
    """A query or a scoped command that cannot be answered or applied."""


class StageNotRun(QueryError):
    """The snapshot a query reads does not exist yet."""


# ---------------------------------------------------------------- width windows

Window = Tuple[int, Optional[int]]        # closed [lo, hi]; hi None = no bound
INF = float("inf")


def _norm(ws: Iterable[Window]) -> Tuple[Window, ...]:
    xs = sorted(((lo, INF if hi is None else hi) for lo, hi in ws if hi is None or hi >= lo))
    out: List[List[float]] = []
    for lo, hi in xs:
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((int(lo), None if hi == INF else int(hi)) for lo, hi in out)


def _and(a, b):
    out = []
    for alo, ahi in a:
        for blo, bhi in b:
            lo = max(alo, blo)
            hi = min(INF if ahi is None else ahi, INF if bhi is None else bhi)
            if hi >= lo:
                out.append((lo, None if hi == INF else int(hi)))
    return _norm(out)


def _sub(a, b):
    out = list(a)
    for blo, bhi in b:
        nxt = []
        for lo, hi in out:
            h = INF if hi is None else hi
            bh = INF if bhi is None else bhi
            if bh < lo or blo > h:
                nxt.append((lo, hi))
                continue
            if blo > lo:
                nxt.append((lo, blo - 1))
            if bh < h:
                nxt.append((int(bh) + 1, hi))
        out = nxt
    return _norm(out)


def _width_window(w) -> Tuple[Window, ...]:
    if isinstance(w, bool):
        raise QueryError(f"width={w!r}: an integer or a (lo, hi) pair")
    if isinstance(w, int):
        return ((w, w),)
    if isinstance(w, (tuple, list)) and len(w) == 2:
        lo, hi = w
        return _norm([(1 if lo is None else int(lo), None if hi is None else int(hi))])
    raise QueryError(f"width={w!r}: an integer or a (lo, hi) pair (None: no bound)")


def _is_window(w) -> bool:
    if isinstance(w, bool):
        return False
    if isinstance(w, int):
        return True
    return (isinstance(w, tuple) and len(w) == 2
            and all(x is None or (isinstance(x, int) and not isinstance(x, bool)) for x in w))


def _is_any_of(want) -> bool:
    """A list, set or frozenset of wanted values (a tuple of two is a (lo, hi)
    window; an empty list is refused: it would select nothing silently)."""
    if isinstance(want, (list, set, frozenset)):
        if not want:
            raise QueryError(f"{want!r}: an empty list of values selects nothing")
        return True
    return False


def _in(ws, v: int) -> bool:
    return any(v >= lo and (hi is None or v <= hi) for lo, hi in ws)


# ---------------------------------------------------------------- objects

@dataclass(frozen=True)
class RegisterSlice:
    """Bits [lo, hi] of one register (a data-gating target)."""
    register: objects.Register
    lo: int
    hi: int

    @property
    def name(self) -> str:
        return f"{self.register.name}[{self.hi}:{self.lo}]"

    @property
    def module(self) -> str:
        return self.register.module

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def id(self) -> str:
        return f"{self.register.id}[{self.hi}:{self.lo}]"

    @property
    def key(self):
        return (self.register.module, self.register.name, self.lo, self.hi)

    def split(self, n: int) -> "Collection":
        return Collection("register_bits", _split(self, n), f"{self.name}.split({n})")

    def __getattr__(self, a):
        # the register's attributes (clock, enable, reset, cells, ...)
        if a.startswith("_"):
            raise AttributeError(a)
        return getattr(self.register, a)


def _split(sl: RegisterSlice, n: int) -> List[RegisterSlice]:
    if not isinstance(n, int) or n < 1:
        raise QueryError(f"split({n!r}): a positive number of bits")
    return [RegisterSlice(sl.register, lo, min(lo + n - 1, sl.hi))
            for lo in range(sl.lo, sl.hi + 1, n)]


def _attr_ok(obj, name: str, want) -> bool:
    if not hasattr(obj, name):
        raise QueryError(f"{type(obj).__name__} has no attribute {name!r}")
    v = getattr(obj, name)
    if callable(want) and not isinstance(want, type):
        return bool(want(v))
    if _is_any_of(want):
        # a list / set of values: any of them (each one as above)
        return any(_attr_ok(obj, name, w) for w in want)
    if isinstance(want, tuple) and len(want) == 2 and isinstance(v, int) and not isinstance(v, bool):
        return _in(_width_window(want), v)
    if isinstance(v, (tuple, list)) and not isinstance(want, (tuple, list)):
        return any(fnmatch.fnmatchcase(str(x), want) if isinstance(want, str) else x == want
                   for x in v)
    if isinstance(want, str) and isinstance(v, str):
        return fnmatch.fnmatchcase(v, want)
    return v == want


class Collection(Sequence):
    """An ordered, immutable set of design objects of one kind."""

    def __init__(self, kind: str, items: Iterable[Any], query: str = "",
                 windows: Optional[Tuple[Window, ...]] = None, universe=None):
        uniq = {}
        for x in items:
            uniq.setdefault(x.key, x)
        self.kind = kind
        self._items = tuple(uniq[k] for k in sorted(uniq, key=lambda k: tuple(
            (0, x) if isinstance(x, int) else (1, str(x)) for x in k)))
        self.query = query
        # for operators: the collection is exactly "every operator of this
        # kind whose width is in these windows" (what a width-selecting pass
        # can be given); None when another filter cut it
        self.windows = windows
        self._universe = universe

    # sequence protocol
    def __len__(self):
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __bool__(self):
        return bool(self._items)

    @property
    def empty(self) -> bool:
        return not self._items

    def __repr__(self):
        if not self._items:
            return f"<empty {self.kind} collection: {self.query}>"
        names = ", ".join(x.id for x in self._items[:4])
        more = f", ... ({len(self._items)})" if len(self._items) > 4 else ""
        return f"<{self.kind} collection {self.query}: {names}{more}>"

    def __eq__(self, other):
        return isinstance(other, Collection) and self.kind == other.kind and \
            [x.key for x in self] == [x.key for x in other]

    def __hash__(self):
        return hash((self.kind, tuple(x.key for x in self)))

    def names(self) -> List[str]:
        return [x.name for x in self]

    def ids(self) -> List[str]:
        return [x.id for x in self]

    def attr(self, name: str) -> List[Any]:
        return [getattr(x, name) for x in self]

    def one(self):
        if len(self) != 1:
            raise QueryError(f"{self!r}: expected exactly one object, found {len(self)}")
        return self._items[0]

    def require(self, allow_empty: bool = False) -> "Collection":
        if self.empty and not allow_empty:
            raise QueryError(f"{self.query}: the selection is empty (it matched nothing)")
        return self

    # filtering
    def filter(self, pattern: Optional[str] = None, **attrs) -> "Collection":
        """Objects whose name (or id, or a hierarchical instance path) matches
        `pattern` (fnmatch: * ? and [...] character classes; [[] matches a
        literal "[") and whose attributes match: a value (equality; a string
        is an fnmatch pattern; for a tuple attribute, any element), a (lo, hi)
        TUPLE for a number (inclusive, None open), a list or set of such
        values (any of them, e.g. width=[8, 16, (24, 32)]) or a predicate."""
        items = [x for x in self if objects.match(pattern, x.name, x.id,
                                                   *getattr(x, "instances", ()))
                 and all(_attr_ok(x, k, v) for k, v in attrs.items())]
        # the windows survive only a window-shaped width filter (an integer or a
        # (lo, hi) pair): a predicate or any other filter cuts the collection
        # to a set the windows no longer describe
        windows = None
        if self.windows is not None and pattern is None and set(attrs) <= {"width"}:
            w = attrs.get("width")
            if w is None and "width" not in attrs:
                windows = self.windows
            elif _is_window(w):
                windows = _and(self.windows, _width_window(w))
            elif isinstance(w, (list, set, frozenset)) and w and all(_is_window(x) for x in w):
                # any of several widths / windows: their union
                windows = _and(self.windows, _norm([x for v in w for x in _width_window(v)]))
        desc = ", ".join([repr(pattern)] * (pattern is not None) +
                         [f"{k}={v!r}" for k, v in attrs.items()])
        return Collection(self.kind, items, f"{self.query}.filter({desc})", windows,
                          self._universe)

    def where(self, predicate: Callable[[Any], bool]) -> "Collection":
        return Collection(self.kind, [x for x in self if predicate(x)],
                          f"{self.query}.where(...)", None, self._universe)

    # set operations (same kind)
    def _same(self, other, op):
        if not isinstance(other, Collection) or other.kind != self.kind:
            raise QueryError(f"{op}: both operands must be {self.kind} collections")

    def __or__(self, other):
        self._same(other, "|")
        w = (_norm(self.windows + other.windows)
             if self.windows is not None and other.windows is not None else None)
        return Collection(self.kind, list(self) + list(other),
                          f"({self.query} | {other.query})", w, self._universe)

    def __and__(self, other):
        self._same(other, "&")
        keys = {x.key for x in other}
        w = (_and(self.windows, other.windows)
             if self.windows is not None and other.windows is not None else None)
        return Collection(self.kind, [x for x in self if x.key in keys],
                          f"({self.query} & {other.query})", w, self._universe)

    def __sub__(self, other):
        self._same(other, "-")
        keys = {x.key for x in other}
        w = (_sub(self.windows, other.windows)
             if self.windows is not None and other.windows is not None else None)
        return Collection(self.kind, [x for x in self if x.key not in keys],
                          f"({self.query} - {other.query})", w, self._universe)

    # registers
    def bits(self, lo: int, hi: Optional[int] = None) -> "Collection":
        """Bits [lo, hi] (hi defaults to lo) of every register: a register_bits
        collection. A range outside a register is an error."""
        hi = lo if hi is None else hi
        if self.kind not in ("register", "register_bits"):
            raise QueryError(f"bits(): a register collection, not {self.kind}")
        if hi < lo or lo < 0:
            raise QueryError(f"bits({lo}, {hi}): need 0 <= lo <= hi")
        out = []
        for x in self:
            reg, base, top = (x, 0, x.width - 1) if self.kind == "register" else \
                (x.register, x.lo, x.hi)
            if base + hi > top:
                raise QueryError(f"bits({lo}, {hi}) of {x.id}: outside its {top - base + 1} bits")
            out.append(RegisterSlice(reg, base + lo, base + hi))
        return Collection("register_bits", out, f"{self.query}.bits({lo}, {hi})")

    def split(self, n: int) -> "Collection":
        """Each register (or slice) cut into consecutive slices of n bits."""
        if self.kind == "register":
            return self.bits(0, min(x.width for x in self) - 1).split(n) if \
                len({x.width for x in self}) == 1 else Collection(
                    "register_bits", [s for x in self for s in _split(RegisterSlice(x, 0, x.width - 1), n)],
                    f"{self.query}.split({n})")
        if self.kind != "register_bits":
            raise QueryError(f"split(): a register collection, not {self.kind}")
        return Collection("register_bits", [s for x in self for s in _split(x, n)],
                          f"{self.query}.split({n})")


# ---------------------------------------------------------------- the database

_STAGE_OF = {"core": "elaborate", "struct": "map_datapath", "netlist": "syn_map"}


class DesignDB:
    """Lazy snapshots of one run's design files (reloaded when a file
    changes). Build with DesignDB.from_cfg(cfg) or design_of(session)."""

    def __init__(self, core=None, struct=None, netlist=None, top: Optional[str] = None,
                 require: Optional[Callable[[str], None]] = None):
        self.paths = {"core": Path(core) if core else None,
                      "struct": Path(struct) if struct else None,
                      "netlist": Path(netlist) if netlist else None}
        self.top = top
        self._require = require
        self._cache: Dict[str, Tuple[int, Any]] = {}

    @classmethod
    def from_cfg(cls, cfg, require=None) -> "DesignDB":
        return cls(cfg.core_mlir, cfg.struct_mlir, cfg.openroad_netlist,
                   cfg.design.top_module, require)

    def available(self, what: str) -> bool:
        p = self.paths.get(what)
        return p is not None and p.exists()

    def _need(self, what: str, query: str) -> Path:
        if self._require is not None:
            self._require(_STAGE_OF[what])
        p = self.paths.get(what)
        if p is None or not p.exists():
            raise StageNotRun(f"{query} needs stage {_STAGE_OF[what]} run first "
                              f"(no {what} snapshot{': ' + str(p) if p else ''})")
        return p

    def _snap(self, what: str, query: str):
        p = self._need(what, query)
        m = p.stat().st_mtime_ns
        hit = self._cache.get(what)
        if hit and hit[0] == m:
            return hit[1]
        if what == "core":
            v = objects.parse_core(p, self.top)
        elif what == "struct":
            v = objects.parse_struct(p, self.top, {r.key for r in self.core.registers}
                                     if self.available("core") else None)
        else:
            v = objects.parse_netlist(p)
        self._cache[what] = (m, v)
        return v

    @property
    def core(self) -> objects.CoreSnapshot:
        return self._snap("core", "a register or operator query")

    @property
    def struct(self) -> objects.StructSnapshot:
        return self._snap("struct", "a clock-gate query")

    # queries -----------------------------------------------------------
    def flops(self, pattern: Optional[str] = None, **attrs) -> Collection:
        """Registers (per module definition) whose name, module/name or
        hierarchical path matches `pattern`."""
        regs = list(self._snap("core", "flops()").registers)
        if self.available("struct"):
            by = {}
            for c in self.struct.cells:
                by.setdefault((c.module, c.register), []).append(c)
            regs = [_with_cells(r, by.get(r.key, ())) for r in regs]
        c = Collection("register", regs, "flops()")
        if pattern is None and not attrs:
            return c
        r = c.filter(pattern, **attrs)
        r.query = _call("flops", pattern, attrs)
        return r

    registers = flops

    def operators(self, kind: Optional[str] = None, pattern=None, **attrs) -> Collection:
        ops = [o for o in self._snap("core", "operators()").operators
               if kind is None or o.kind == kind]
        k = kind or "operator"
        base = Collection(k, ops, f"{k}s()", ((1, None),) if kind else None)
        if pattern is None and not attrs:
            return base
        r = base.filter(pattern, **attrs)
        r.query = _call(f"{k}s", pattern, attrs)
        return r

    def adders(self, pattern=None, **attrs) -> Collection:
        return self.operators("adder", pattern, **attrs)

    def multipliers(self, pattern=None, **attrs) -> Collection:
        return self.operators("multiplier", pattern, **attrs)

    def shifters(self, pattern=None, **attrs) -> Collection:
        return self.operators("shifter", pattern, **attrs)

    def icgs(self, pattern=None, **attrs) -> Collection:
        """Clock gates of the struct IR (inferred clock_gate_<n> and RTL gates)."""
        gates = self._snap("struct", "icgs()").gates
        c = Collection("clock_gate", gates, "icgs()")
        if pattern is None and not attrs:
            return c
        r = c.filter(pattern, **attrs)
        r.query = _call("icgs", pattern, attrs)
        return r

    def cells(self, pattern=None, **attrs) -> Collection:
        """Mapped cell instances of the gate-level netlist (`cell=` filters by type)."""
        c = Collection("cell", self._snap("netlist", "cells()"), "cells()")
        if pattern is None and not attrs:
            return c
        r = c.filter(pattern, **attrs)
        r.query = _call("cells", pattern, attrs)
        return r

    def modules(self, pattern=None) -> Collection:
        mods = [_Module(m, p) for m, p in sorted(self._snap("core", "modules()").modules.items())]
        return Collection("module", mods, "modules()").filter(pattern) if pattern else \
            Collection("module", mods, "modules()")


@dataclass(frozen=True)
class _Module:
    name: str
    instances: Tuple[str, ...]

    @property
    def id(self):
        return self.name

    @property
    def key(self):
        return (self.name,)


def _with_cells(r: objects.Register, cells) -> objects.Register:
    import dataclasses
    return dataclasses.replace(r, cells=tuple(sorted(cells, key=lambda c: (c.part or 0, c.lo, c.instance))))


def _call(fn, pattern, attrs) -> str:
    a = [repr(pattern)] if pattern is not None else []
    a += [f"{k}={v!r}" for k, v in attrs.items()]
    return f"{fn}({', '.join(a)})"


# ---------------------------------------------------------------- session glue

def design_of(obj) -> DesignDB:
    """The DesignDB of a session (s.artifacts / s.require, or s.cfg), of a
    FlowConfig, or `obj` itself."""
    if isinstance(obj, DesignDB):
        return obj
    cached = getattr(obj, "_p4_design", None)
    if isinstance(cached, DesignDB):
        return cached
    req = getattr(obj, "require_snapshot", None) or (
        getattr(obj, "require", None) if callable(getattr(obj, "require", None)) else None)
    if hasattr(obj, "core_mlir") and hasattr(obj, "design"):
        db = DesignDB.from_cfg(obj)
    elif hasattr(obj, "artifacts") and isinstance(getattr(obj, "artifacts"), dict):
        a = obj.artifacts
        cfg = obj.cfg
        db = DesignDB(a.get("core", cfg.core_mlir), a.get("struct", cfg.struct_mlir),
                      a.get("openroad_netlist", cfg.openroad_netlist),
                      cfg.design.top_module, req)
    elif hasattr(obj, "cfg"):
        db = DesignDB.from_cfg(obj.cfg, req)
    else:
        raise QueryError(f"no design for {obj!r}: a Session, a FlowConfig or a DesignDB")
    try:
        object.__setattr__(obj, "_p4_design", db)
    except Exception:
        pass
    return db


def flops(s, pattern=None, **attrs) -> Collection:
    return design_of(s).flops(pattern, **attrs)


registers = flops


def adders(s, pattern=None, **attrs) -> Collection:
    return design_of(s).adders(pattern, **attrs)


def multipliers(s, pattern=None, **attrs) -> Collection:
    return design_of(s).multipliers(pattern, **attrs)


def shifters(s, pattern=None, **attrs) -> Collection:
    return design_of(s).shifters(pattern, **attrs)


def operators(s, kind=None, pattern=None, **attrs) -> Collection:
    return design_of(s).operators(kind, pattern, **attrs)


def icgs(s, pattern=None, **attrs) -> Collection:
    return design_of(s).icgs(pattern, **attrs)


def cells(s, pattern=None, **attrs) -> Collection:
    return design_of(s).cells(pattern, **attrs)


def modules(s, pattern=None) -> Collection:
    return design_of(s).modules(pattern)


# ---------------------------------------------------------------- scoped commands

def _set_at() -> str:
    for fr in inspect.stack()[2:8]:
        f = fr.filename
        if not f.endswith(("query.py", "session.py")):
            return f"script:{os.path.basename(f)}:{fr.lineno}"
    return "script"


def _as_collection(sel, kinds: Tuple[str, ...], cmd: str) -> Collection:
    if isinstance(sel, RegisterSlice):
        sel = Collection("register_bits", [sel], sel.name)
    elif isinstance(sel, objects.Register):
        sel = Collection("register", [sel], sel.name)
    elif isinstance(sel, objects.Operator):
        sel = Collection(sel.kind, [sel], sel.id)
    if not isinstance(sel, Collection):
        raise QueryError(f"{cmd}: a collection from a query, got {sel!r}")
    if sel.kind not in kinds:
        raise QueryError(f"{cmd}: acts on {' / '.join(kinds)}, not on a {sel.kind} collection")
    return sel


def _register_targets(sel: Collection, db: Optional[DesignDB]) -> List[str]:
    if sel.kind == "register":
        sel = Collection("register_bits", [RegisterSlice(r, 0, r.width - 1) for r in sel],
                         sel.query)
    names: Dict[str, set] = {}
    if db is not None and db.available("core"):
        for r in db.core.registers:
            names.setdefault(r.name, set()).add(r.module)
    out = []
    for sl in sel:
        # the pass matches a bare name in every module: qualify it when the
        # name is not unique
        qual = len(names.get(sl.register.name, {sl.module})) > 1
        out.append(f"{sl.module + '/' if qual else ''}{sl.register.name}@{sl.lo}-{sl.hi}")
    return out


def _adder_targets(sel: Collection, db: DesignDB) -> List[str]:
    if sel.windows is not None:
        return [scoping.width_target(lo, hi) for lo, hi in sel.windows]
    # a filtered selection: expressible when it holds every adder of each
    # width it touches (the passes select by width only)
    allw: Dict[int, List[str]] = {}
    for o in db.core.operators:
        if o.kind == "adder":
            allw.setdefault(o.width, []).append(o.id)
    mine = {o.id for o in sel}
    widths = sorted({o.width for o in sel})
    for w in widths:
        other = [i for i in allw.get(w, []) if i not in mine]
        if other:
            raise QueryError(
                f"map_adders: {sel.query} cannot be given to the pass: it selects by width only and "
                f"width {w} also has {', '.join(other)} (select whole widths, e.g. "
                f"adders(width=({w}, {w})))")
    # one window per run of consecutive selected widths: the windows hold
    # exactly the selected widths (a gap, even one without an adder in the
    # core IR, is not bridged: the pass may see adds of that width later)
    out: List[Window] = []
    for w in widths:
        if out and out[-1][1] == w - 1:
            out[-1] = (out[-1][0], w)
        else:
            out.append((w, w))
    return [scoping.width_target(lo, hi) for lo, hi in out]


def _add(s, x: scoping.ScopedSetting) -> scoping.ScopedSetting:
    db = getattr(s, "db", None)
    if db is not None and hasattr(db, "add_scoped"):        # a Session
        db.add_scoped(x)
        return x
    if hasattr(s, "set") and hasattr(s, "_given"):           # a minimal session
        cur = list(s._given.get(scoping.OPTION) or [])
        s.set(scoping.OPTION, cur + [x.to_record()])
        return x
    raise QueryError(f"no session to record the scoped setting in: {s!r}")


def gate_register(s, selection, detect: str = "zero", allow_empty: bool = False):
    """EXPERIMENTAL. Data-following clock gating of register bit ranges
    (pass data-gate-enables): each slice holds when loading would not change
    it (detect=zero: load and slice both zero; xor: load equals slice), so
    clock-gate-inference gives it its own gate. A whole register is one slice.
    Records one scoped setting; returns it (None for an allowed empty one)."""
    sel = _as_collection(selection, ("register", "register_bits"), "gate_register")
    if sel.empty:
        if allow_empty:
            return None
        sel.require()
    bad = [x.id for x in sel if not getattr(x, "hold_mux", True)]
    if bad:
        raise QueryError(f"gate_register: {', '.join(bad)}: next is not a plain hold mux "
                         "(data-gate-enables cannot act on it)")
    db = design_of(s) if not isinstance(s, DesignDB) else s
    x = scoping.make("gate_register", _register_targets(sel, db), {"detect": detect},
                     _set_at(), "gate_register")
    return _add(s, x)


def map_adders(s, selection, form: str = "prefix", allow_empty: bool = False, **args):
    """Build the selected adders as a prefix network (form=prefix: arch,
    isolate_inputs, boundary) or a ripple of two-output adder cells
    (form=cells: increments). The passes select by width: a selection made
    of width windows is passed as such; another selection is accepted only
    when it holds every adder of the widths it touches."""
    sel = _as_collection(selection, ("adder",), "map_adders")
    if sel.empty:
        if allow_empty:
            return None
        sel.require()
    db = design_of(s)
    x = scoping.make("map_adders", _adder_targets(sel, db), {"form": form, **args},
                     _set_at(), "map_adders")
    return _add(s, x)


def scoped_of(s) -> Tuple[scoping.ScopedSetting, ...]:
    """The scoped settings recorded in a session."""
    db = getattr(s, "db", None)
    if db is not None and hasattr(db, "scoped"):
        return tuple(db.scoped)
    if hasattr(s, "_given"):
        return scoping.from_value(s._given.get(scoping.OPTION))
    return ()


def materialise(s, scoped) -> Dict[str, Any]:
    """The session's scoped settings as option values
    ({"syn.scoped": [record, ...]}); the stages read syn.scoped."""
    recs = [scoping.from_record(x).to_record() for x in scoped]
    return {scoping.OPTION: recs} if recs else {}
