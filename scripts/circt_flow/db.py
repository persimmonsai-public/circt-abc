"""The attribute database of a session (`s.db`): every
option of the registry (options.py) as a typed, documented attribute in its
namespace, validated when it is assigned.

    s.db.syn.vt_swap.recover_order = "leak"     # checked at once
    s.db.syn.vt_swap.recover_order               # the resolved value
    s.db["syn.vt_swap.recover_order"]            # the same, by dotted name
    s.db.report("syn.vt_swap")                   # value, default, source, help
    help(s.db.syn.vt_swap)                       # every option of the namespace

The namespace classes are generated from the registry (one class per
namespace, one property per option, its help as the property's doc and its
Python type in __annotations__), so the attributes, their documentation and
tab completion can never drift from the registry.

A name that is both an option and a namespace (syn.clock_gating: the section
switch and its keys) reads as the namespace; its own value is
`s.db["syn.clock_gating"]` (or `.get()` on the namespace), and assigning it
(`s.db.syn.clock_gating = True`) sets the option.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import options

PY_TYPES = {"bool": bool, "int": int, "float": float, "str": str,
            "path": str, "choice": str, "object": object}


class OptionAttributeError(options.OptionError, AttributeError):
    """An unknown attribute of the database (an OptionError that hasattr()
    and completion also understand)."""


@dataclass(frozen=True)
class OptionInfo:
    name: str
    type: str
    value: Any
    default: Any
    source: str
    set_at: Optional[str]
    unit: str
    range: Optional[tuple]
    choices: Tuple[str, ...]
    help: str
    binds: Tuple[str, ...]
    legacy_env: Tuple[str, ...]
    stage: str

    def text(self) -> str:
        t = self.type + (f" {'/'.join(self.choices)}" if self.choices else "") + \
            (f" [{_b(self.range[0])}, {_b(self.range[1])}]" if self.range else "") + \
            (f" ({self.unit})" if self.unit else "")
        lines = [f"{self.name} = {self.value!r}   ({self.source}"
                 + (f", {self.set_at}" if self.set_at else "") + ")",
                 f"  type {t}; default {_d(self.default)}; read by stage {self.stage}",
                 f"  {self.help}"]
        if self.binds:
            lines.append(f"  binds: {'; '.join(self.binds)}")
        if self.legacy_env:
            lines.append(f"  legacy variable: {', '.join(self.legacy_env)} (deprecated)")
        return "\n".join(lines)

    __str__ = text


def _b(x):
    return "" if x is None else x


def _d(v):
    return "unset" if v is None else repr(v)


@dataclass
class SettingsReport:
    """Rows of OptionInfo; str() is a text table."""
    rows: List[OptionInfo] = field(default_factory=list)
    title: str = ""

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        return self.rows[i]

    def names(self) -> List[str]:
        return [r.name for r in self.rows]

    def as_dict(self) -> Dict[str, Any]:
        return {r.name: r.value for r in self.rows}

    def text(self) -> str:
        if not self.rows:
            return (self.title + "\n" if self.title else "") + "(no options)"
        w = max(len(r.name) for r in self.rows)
        out = [self.title] if self.title else []
        out.append(f"{'option':{w}s}  {'value':18s} {'default':14s} {'source':9s} help")
        for r in self.rows:
            out.append(f"{r.name:{w}s}  {_short(r.value):18s} {_short(r.default):14s} "
                       f"{r.source:9s} {r.help}")
        return "\n".join(out)

    __str__ = text

    def __repr__(self):
        return self.text()


def _short(v, n=18):
    s = _d(v)
    return s if len(s) <= n else s[:n - 3] + "..."


# ---------------------------------------------------------------- namespaces

def stub_names():
    """The options the stub names: the settable ones (the pack-only records
    are left out)."""
    return {n for n, o in options.registry().items()
            if o.settable}


def _tree(names=None) -> Dict[str, dict]:
    """{namespace path: {"options": [leaf names], "children": [names]}}."""
    t: Dict[str, dict] = {"": {"options": [], "children": []}}
    for name in sorted(options.registry() if names is None else names):
        parts = name.split(".")
        for i in range(1, len(parts)):
            ns, child = ".".join(parts[:i - 1]), parts[i - 1]
            path = ".".join(parts[:i])
            t.setdefault(path, {"options": [], "children": []})
            if child not in t[ns]["children"]:
                t[ns]["children"].append(child)
        t[".".join(parts[:-1])]["options"].append(parts[-1])
    return t


class Node:
    """A namespace of the attribute database (generated subclasses)."""
    _path = ""
    _children: Tuple[str, ...] = ()
    _leaves: Tuple[str, ...] = ()

    def __init__(self, db: "AttrDB"):
        object.__setattr__(self, "_db", db)

    def _full(self, attr: str) -> str:
        return f"{self._path}.{attr}" if self._path else attr

    def __getattr__(self, attr):
        # only reached for names that are not properties: unknown
        if attr.startswith("__"):
            raise AttributeError(attr)
        raise OptionAttributeError(f"unknown option or namespace "
                                   f"{self._full(attr)!r}{_near(self._full(attr))}")

    def __setattr__(self, attr, value):
        if attr in self._leaves or attr in self._children:
            object.__setattr__(self, attr, value)       # the property's setter
            return
        raise OptionAttributeError(f"unknown option {self._full(attr)!r}"
                                   f"{_near(self._full(attr))}")

    def __dir__(self):
        return sorted(set(self._leaves) | set(self._children)) + ["get", "report"]

    def get(self):
        """This namespace's own option value (a name that is both)."""
        return self._db.get(self._path)

    def report(self, changed: bool = False) -> SettingsReport:
        return self._db.report(self._path + ".", changed=changed)

    def __repr__(self):
        return self.report().text()


def _near(name: str) -> str:
    near = difflib.get_close_matches(name, list(options.registry()), 3, 0.6)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def _leaf_property(full: str, o: options.Option):
    def fget(self):
        return self._db.get(full)

    def fset(self, value):
        self._db.set(full, value, _depth=3)
    doc = (f"{o.type}" + (f" {'/'.join(o.choices)}" if o.choices else "")
           + (f" ({o.unit})" if o.unit else "")
           + f", default {_d(o.default)}. {o.help}")
    return property(fget, fset, doc=doc)


def _child_property(path: str, own: Optional[options.Option]):
    def fget(self):
        return self._db._node(path)

    def fset(self, value):
        if own is None:
            raise OptionAttributeError(f"{path} is a namespace, not an option")
        self._db.set(path, value, _depth=3)
    doc = f"namespace {path}" + (f"; also an option: {own.help}" if own else "")
    return property(fget, fset, doc=doc)


_CLASSES: Dict[str, type] = {}


def node_class(path: str) -> type:
    """The generated class of namespace `path` ("" = the root)."""
    if path in _CLASSES:
        return _CLASSES[path]
    tree = _tree()
    if path not in tree:
        raise OptionAttributeError(f"unknown namespace {path!r}{_near(path)}")
    reg = options.registry()
    t = tree[path]
    attrs: Dict[str, Any] = {"_path": path, "_children": tuple(t["children"]),
                             "_leaves": tuple(o for o in t["options"]
                                              if o not in t["children"])}
    ann = {}
    for leaf in attrs["_leaves"]:
        full = f"{path}.{leaf}" if path else leaf
        attrs[leaf] = _leaf_property(full, reg[full])
        ann[leaf] = PY_TYPES[reg[full].type]
    for child in t["children"]:
        full = f"{path}.{child}" if path else child
        attrs[child] = _child_property(full, reg.get(full))
    attrs["__annotations__"] = ann
    lines = [f"Options of namespace {path or '(root)'} (generated from options.py)."]
    for leaf in attrs["_leaves"]:
        full = f"{path}.{leaf}" if path else leaf
        lines.append(f"  {leaf}: {reg[full].help}")
    if t["children"]:
        lines.append("  namespaces: " + ", ".join(t["children"]))
    attrs["__doc__"] = "\n".join(lines)
    cls = type("db_" + (path.replace(".", "_") or "root"), (Node,), attrs)
    _CLASSES[path] = cls
    return cls


def stub_text() -> str:
    """A typed stub of the namespaces (editor completion and type checking;
    doc/db_types.pyi): the settable options."""
    tree = _tree(stub_names())
    reg = options.registry()
    head = ('"""Generated by `python3 -m circt_flow options --stub` from options.py; '
            'do not edit. The settable options (pack-only records left out)."""')
    out = [head, "from typing import Any", ""]

    def cname(p):
        return "db_" + (p.replace(".", "_") or "root")
    for path in sorted(tree, key=lambda p: (-p.count("."), p)):
        t = tree[path]
        out.append(f"class {cname(path)}:")
        out.append(f'    """namespace {path or "(root)"}"""')
        for leaf in t["options"]:
            full = f"{path}.{leaf}" if path else leaf
            if leaf in t["children"]:
                continue
            ty = PY_TYPES[reg[full].type]
            out.append(f"    {leaf}: {'Any' if ty is object else ty.__name__}")
            out.append(f'    """{reg[full].help.replace(chr(34), chr(39))}"""')
        for child in t["children"]:
            out.append(f"    {child}: {cname(f'{path}.{child}' if path else child)}")
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- database

class AttrDB:
    """The session's attribute database. Attribute access goes through the
    generated namespace classes; the methods take dotted names."""

    def __init__(self, session):
        object.__setattr__(self, "_s", session)
        object.__setattr__(self, "_nodes", {})

    def _node(self, path: str) -> Node:
        if path not in self._nodes:
            self._nodes[path] = node_class(path)(self)
        return self._nodes[path]

    # attribute access: the top namespaces (syn, cts, hold, post, pnr, flow, run)
    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        if attr in node_class("")._children:
            return self._node(attr)
        raise OptionAttributeError(f"unknown namespace {attr!r}{_near(attr)}; "
                                   f"namespaces: {', '.join(node_class('')._children)}")

    def __setattr__(self, attr, value):
        raise OptionAttributeError(f"{attr!r} is a namespace (or unknown); set an option "
                                   f"inside it, e.g. s.db.syn.fanout.max = 12")

    def __dir__(self):
        return list(node_class("")._children) + [
            "get", "set", "reset", "source", "set_at", "info", "help", "report",
            "changed", "validate", "names", "add_scoped", "scoped", "reset_scoped"]

    def __getitem__(self, name):
        return self.get(name)

    def __setitem__(self, name, value):
        self.set(name, value, _depth=2)

    def __contains__(self, name):
        return name in options.registry()

    # dotted-name interface
    def get(self, name: str):
        options.lookup(name, "s.db")
        return self._s._settings().get(name)

    def set(self, name: str, value, _depth: int = 1) -> None:
        self._s._set(name, value, depth=_depth + 1)

    def reset(self, name: str) -> None:
        """Back to the lower sources (recipe, tech pack, default); a name
        ending in '.' or a namespace resets every option under it."""
        self._s._reset(name)

    def source(self, name: str) -> str:
        options.lookup(name, "s.db")
        return self._s._settings().source(name)

    def set_at(self, name: str) -> Optional[str]:
        return self._s._set_at.get(name)

    def names(self, pattern: str = "") -> List[str]:
        return [n for n in sorted(options.registry()) if pattern in n]

    def info(self, name: str) -> OptionInfo:
        o = options.lookup(name, "s.db")
        s = self._s._settings()
        from .session import stage_of
        return OptionInfo(name, o.type, s.get(name), o.default, s.source(name),
                          self._s._set_at.get(name), o.unit, o.range, o.choices,
                          o.help, tuple(map(str, o.binds)),
                          o.legacy_env, stage_of(name))

    def help(self, name: str = "") -> str:
        """The option's help (type, default, value, source, what it binds,
        the stage that reads it), or the list of a namespace's options."""
        reg = options.registry()
        if name in reg:
            return self.info(name).text()
        rows = [n for n in sorted(reg) if n.startswith(name.rstrip(".") + ".")] \
            if name else sorted(reg)
        if not rows:
            raise OptionAttributeError(f"unknown option or namespace {name!r}{_near(name)}")
        w = max(map(len, rows))
        return "\n".join(f"{n:{w}s}  {reg[n].type:6s} {reg[n].help}" for n in rows)

    def report(self, pattern: str = "", changed: bool = False) -> SettingsReport:
        """Every option whose name contains `pattern` (with `changed`: only
        those not at their built-in default source)."""
        s = self._s._settings()
        rows = [self.info(n) for n in sorted(options.registry())
                if pattern in n and (not changed or s.is_set(n))]
        title = ("options not at their built-in default" if changed and not pattern
                 else f"options matching {pattern!r}" if pattern else "options")
        return SettingsReport(rows, title)

    def changed(self) -> SettingsReport:
        return self.report(changed=True)

    def validate(self) -> None:
        """Resolve everything now (cross-option checks, the flow.yaml section
        parsers); otherwise this happens before the next stage runs."""
        self._s._load(force=True)

    # scoped settings (scoping.py; see doc/scripting.md)
    @property
    def scoped(self) -> tuple:
        return tuple(self._s._scoped)

    def add_scoped(self, item) -> None:
        self._s._add_scoped(item)

    def reset_scoped(self, command: Optional[str] = None) -> None:
        self._s._reset_scoped(command)
