"""Milestone 1 of the "design-management layer" (the package docstring in
design/__init__.py lists the milestones).

Gives the Python flow layer hierarchical instance addressing over ONE
pipeline stage's IR -- `${TOP}_struct.mlir` (`cfg.struct_mlir`), the file
circt-opt emits *before* `--hw-flatten-modules` runs -- instead of the
grep/ad-hoc-text-parsing every prior QoR investigation in this project used
to answer "how many X instances are there" / "what's under Y in the
hierarchy".

Implementation mechanism (per the scoping's own finding): real MLIR/CIRCT
Python bindings are NOT built or importable in this repo's checkout today
(`CIRCT_BINDINGS_PYTHON_ENABLED:BOOL=OFF`, no nanobind/pybind11 fetched,
`import mlir.ir` fails). So this is the sketch's own documented fallback: a
line-oriented regex scanner over circt-opt's textual `hw` dialect printer
output, NOT a general MLIR grammar parser. It relies on an EMPIRICAL
property of this pipeline's current CIRCT version/pass output (verified
against a real 10,114-line ibex_top _struct.mlir sample, not assumed): every
`hw.module`/`hw.module.extern` header and every `hw.instance` op is a single
source line -- none wrap. See the scoping's "Real risks" for what happens if
a future CIRCT upgrade breaks that assumption (a silently-wrong parse on a
wrapped line), and this package's own verification script for the
independent cross-checks that would catch it.

Explicit non-goals (restated from the scoping -- do not extend this module
to do these without re-scoping first):
  - no cell/net/pin objects, no liberty-cell/mapped-netlist awareness
  - no cross-stage signal tracing
  - no directive/customization surface (this is strictly read-only)
  - no parsing of `_flat.mlir`/`_mapped.mlir` as an INPUT (only used as an
    independent cross-check target by the verification script)
  - no port lists / port types / per-port connectivity
  - no generic op-printing syntax (`--mlir-print-op-generic`) support
  - no caching/incremental re-parse of the source file across calls
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "DesignError",
    "ModuleInstance",
    "Module",
    "HierNode",
    "Design",
]


class DesignError(RuntimeError):
    """Raised when `_struct.mlir` can't be parsed into a Design (e.g. an
    unresolvable/missing top module, or unbalanced module braces)."""


# One `hw.module`/`hw.module.extern` header line, e.g.:
#   hw.module @ibex_top(in %clk_i : i1, ...) {
#   hw.module private @ibex_core(...) {
#   hw.module.extern @DFFS_X1(...)
#   hw.module.extern private @ICG_X1(...)
_MODULE_HEADER_RE = re.compile(
    r"^\s*hw\.module(\.extern)?\s+(?:private\s+)?@(\w+)")

# One `hw.instance` op, e.g.:
#   %x = hw.instance "controller_i" @ibex_controller(...) -> (...)
_INSTANCE_RE = re.compile(r'hw\.instance\s+"([^"]+)"\s+@(\w+)')


@dataclass(frozen=True)
class ModuleInstance:
    """One direct `hw.instance` inside a module's body, in file order."""
    name: str          # the quoted hw.instance name, e.g. "controller_i"
    module_name: str   # the @-referenced hw.module symbol, e.g. "ibex_controller"


@dataclass(frozen=True)
class Module:
    """One `hw.module` / `hw.module.extern` definition."""
    name: str                              # hw.module symbol, e.g. "ibex_core"
    is_extern: bool                        # True for hw.module.extern (liberty-cell leaves; no body, no instances)
    instances: Tuple[ModuleInstance, ...]  # direct children declared in this module's body, file order preserved


@dataclass(frozen=True)
class HierNode:
    """One node in the recursively-expanded instance tree rooted at
    Design.top_name. The root node (returned by Design.hierarchy()) is
    synthetic -- it represents the top module itself, not an instance of
    it -- and has path="", parent=None; every real instance's path is the
    slash-joined chain of instance names below the root, e.g.
    "u_ibex_core/id_stage_i/controller_i".
    """
    path: str                        # slash-joined path from the root, e.g. "u_ibex_core/id_stage_i/controller_i"
    instance_name: str               # "controller_i"
    module_name: str                 # "ibex_controller"
    parent: Optional["HierNode"]
    children: Tuple["HierNode", ...]


class Design:
    """The root handle for one elaborated build's `_struct.mlir`, i.e. one
    `circt-opt` textual-IR snapshot taken before `--hw-flatten-modules`
    runs. Read-only: nothing here mutates the source file or influences
    any later pipeline stage.
    """

    def __init__(self, modules: Dict[str, Module], top_name: str):
        self.modules = modules
        self.top_name = top_name
        self._root: Optional[HierNode] = None  # lazily built, cached in-memory tree

    @classmethod
    def from_mlir_file(cls, path: Union[str, Path],
                        top_name: Optional[str] = None) -> "Design":
        """Parse a `circt-opt` textual-IR file (this pipeline's own
        `hw`-dialect pretty-printer output, not `--mlir-print-op-generic`)
        into a Design.

        top_name defaults to the LAST non-extern hw.module in the file if
        not given -- this pipeline's own circt-opt output always emits the
        top-level module last (confirmed against a real 10,114-line sample:
        `ibex_top` is the final hw.module, after `ibex_top_residual` and
        every instantiated submodule). Callers that already know the real
        top module (e.g. scripts/circt_flow's own orchestration code, via
        `cfg.design.top_module`) should pass it explicitly rather than
        relying on this inference for anything that matters.
        """
        path = Path(path)
        modules: Dict[str, Module] = {}
        last_nonextern: Optional[str] = None

        in_module = False
        current_name: Optional[str] = None
        current_instances: List[ModuleInstance] = []
        current_depth = 0

        with open(path, "r") as f:
            for line in f:
                header = _MODULE_HEADER_RE.match(line)
                if header is not None:
                    is_extern = header.group(1) is not None
                    name = header.group(2)
                    if is_extern:
                        # hw.module.extern has no body/braces to track --
                        # it opens and closes on the same header line.
                        modules[name] = Module(
                            name=name, is_extern=True, instances=())
                    else:
                        last_nonextern = name
                        depth = line.count("{") - line.count("}")
                        if depth <= 0:
                            # Header line's braces (if any, e.g. an inline
                            # attribute dict) net out to non-positive --
                            # this module has no open body at all.
                            modules[name] = Module(
                                name=name, is_extern=False, instances=())
                        else:
                            in_module = True
                            current_name = name
                            current_instances = []
                            current_depth = depth
                    continue

                if in_module:
                    inst = _INSTANCE_RE.search(line)
                    if inst is not None:
                        current_instances.append(
                            ModuleInstance(name=inst.group(1),
                                           module_name=inst.group(2)))
                    current_depth += line.count("{") - line.count("}")
                    if current_depth <= 0:
                        modules[current_name] = Module(
                            name=current_name, is_extern=False,
                            instances=tuple(current_instances))
                        in_module = False
                        current_name = None
                        current_instances = []
                        current_depth = 0

        if in_module:
            raise DesignError(
                f"unterminated hw.module @{current_name} in {path} "
                "(unbalanced braces -- the single-line-op assumption this "
                "parser relies on may not hold for this file)")

        resolved_top = top_name if top_name is not None else last_nonextern
        if resolved_top is None or resolved_top not in modules:
            raise DesignError(
                f"top module {resolved_top!r} not found among "
                f"{len(modules)} modules parsed from {path}")

        return cls(modules=modules, top_name=resolved_top)

    def _build_node(self, path: str, instance_name: str, module_name: str,
                     parent: Optional[HierNode]) -> HierNode:
        node = HierNode(path=path, instance_name=instance_name,
                         module_name=module_name, parent=parent, children=())
        module = self.modules.get(module_name)
        children: List[HierNode] = []
        if module is not None and not module.is_extern:
            for inst in module.instances:
                child_path = f"{path}/{inst.name}" if path else inst.name
                children.append(self._build_node(
                    child_path, inst.name, inst.module_name, node))
        # Two-phase construction: HierNode is frozen (so a fully-built node
        # is never mutated by a caller), but children must be built AFTER
        # the node itself exists so each child's `parent` can point back to
        # it. object.__setattr__ is the standard escape hatch for this.
        object.__setattr__(node, "children", tuple(children))
        return node

    def hierarchy(self) -> HierNode:
        """The recursively-expanded tree rooted at top_name. Built once and
        cached on this Design instance (cheap: this is an in-memory walk of
        the already-parsed `modules` dict, not a file re-scan -- the
        "no caching" non-goal is specifically about from_mlir_file's own
        whole-file text scan, not about this in-memory structure).

        No cycle protection: a real hierarchical netlist has no
        instantiation cycles, so this always terminates today, but a
        malformed/adversarial input file that DID contain one would recurse
        until RecursionError, not raise a clean DesignError. Accepted risk
        for milestone 1, per the scoping's own "Real risks" section.
        """
        if self._root is None:
            self._root = self._build_node(
                "", self.top_name, self.top_name, None)
        return self._root

    def get_instances(self, pattern: str) -> List[HierNode]:
        """Every HierNode (at any depth, excluding the synthetic root
        itself) whose `path` matches the given fnmatch-style glob, e.g.
        "*/id_stage_i/*" or "u_ibex_core/*"."""
        result: List[HierNode] = []

        def walk(n: HierNode) -> None:
            for c in n.children:
                if fnmatch.fnmatch(c.path, pattern):
                    result.append(c)
                walk(c)

        walk(self.hierarchy())
        return result

    def find_instance(self, path: str) -> Optional[HierNode]:
        """Exact slash-path lookup, e.g.
        "u_ibex_core/id_stage_i/controller_i"."""
        found: Optional[HierNode] = None

        def walk(n: HierNode) -> bool:
            nonlocal found
            for c in n.children:
                if c.path == path:
                    found = c
                    return True
                if walk(c):
                    return True
            return False

        walk(self.hierarchy())
        return found

    def instance_count(self, module_name: str) -> int:
        """Count of HierNode across the whole tree (excluding the synthetic
        root) instantiating module_name -- the direct replacement for the
        kind of one-off `grep -c` this project's QoR investigations ran
        repeatedly."""
        count = 0

        def walk(n: HierNode) -> None:
            nonlocal count
            for c in n.children:
                if c.module_name == module_name:
                    count += 1
                walk(c)

        walk(self.hierarchy())
        return count
