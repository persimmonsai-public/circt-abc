"""Milestone 2 of the "design-management layer" (the package docstring in
design/__init__.py lists the milestones).

Gives the Python flow layer cell/net/pin queries over ONE already-flat
module body in `${TOP}_mapped.mlir` (`cfg.mapped_mlir`) -- the file
circt-opt emits right after `--merge-residual-modules` +
`--abc-bridge`, containing real liberty-cell `hw.instance` ops (e.g.
`@AOI21_X1`) -- instead of the grep/`stat`-based
one-off cell-usage comparisons every prior QoR investigation in this
project (the buffer-gap/BUFH-source tracing, the AOI/OAI cell-usage
comparisons, the register-count-gap histograms) used to answer "how many
of cell X are there" / "what drives this cell's input".

Implementation mechanism: same textual-IR-parsing fallback as milestone 1
(`mlir_text.py`) -- real MLIR/CIRCT Python bindings are still not built in
this repo's checkout. Deliberately SINGLE-MODULE scope, matching milestone
1's own "smallest slice" discipline: this parses one named module's body
only (e.g. `abc_bridge_merged_residual_bits`, where `merge-residual-logic`
+ `abc-bridge` puts essentially all of a design's real mapped combinational
cells), and resolves nets ONLY within that module's own local SSA-value
namespace. A value crossing this module's OWN port boundary (a primary
input to the module, or a value only ever seen on the module's own
`hw.output` terminator) is a valid "boundary" net with no local driver, by
design -- NOT an error, and NOT something this milestone tries to resolve
across an instance boundary into another module (that is real,
non-trivial complexity: this pipeline's own `_mapped.mlir` uses a
`<instance>.<result>`-qualified naming convention for cross-module value
references, confirmed by inspection -- resolving those would need a
whole-design net model, deliberately out of scope here, see Non-goals).

Milestone 5 extends this beyond milestone 2's original scope: milestone 2
deferred "cross-module net resolution" on the theory that `_mapped.mlir`'s
`<instance>.<result>`-qualified value names (e.g.
`%u_abc_bridge_merged_residual.ibex_alu_residual_0_out5`) were some
special cross-module naming convention needing new resolution machinery.
Direct inspection of a real file disproved that: this is just CIRCT's
ordinary multi-result `hw.instance` asm-printer convention -- plain SSA
value names (MLIR value names may contain `.`), bound via the standard
`%r0, %r1, ... = hw.instance "name" @Module(...) -> (out0: T0, out1: T1,
...)` multi-result syntax, referenced elsewhere exactly like any other
SSA value. The REAL blocker was two already-known, already-flagged gaps
in this module's OWN parser: (1) a multi-result LHS was detected and
SKIPPED outright (never parsed), and (2) only `hw.module.extern`
references became `Cell`s -- a non-extern `hw.instance` (a reference to
another real, non-leaf `hw.module`, e.g. `abc_bridge_merged_residual`)
was silently excluded from the cell model entirely, even though it is a
perfectly legitimate NET DRIVER from its enclosing module's own
perspective (the same "black-box sub-block instance" concept a synthesis
shell's `get_cells -hierarchical` view treats as a real, opaque cell). Fixing
both closes milestone 2's deferral AND its OWN separately-flagged
"multi-output cell support" gap (real liberty cells like full and half adders
genuinely have 2 outputs) with the SAME general fix -- they were the same root
cause, not two unrelated problems.

`Cell.is_leaf` distinguishes the two cases: `True` for a real liberty-cell
`hw.instance` (references a `hw.module.extern`); `False` for an instance
of another real `hw.module` (a sub-block/hierarchical instance, whose
`cell_type` is that module's own symbol name, e.g.
`"abc_bridge_merged_residual"`, not a liberty cell name). Net/fanin/fanout
resolution treats both uniformly -- a `Net`'s driver/loads can now be
either kind of `Cell`.

Still deliberately ONE LEVEL only: resolving a non-leaf `Cell`'s own
internal driver (recursing into `abc_bridge_merged_residual`'s own body
to find what ultimately drives one of ITS outputs) is explicit,
correctly out-of-scope, follow-on work (this project's real
`_mapped.mlir` was found to nest TWO non-extern wrapper levels around the
real liberty-cell body -- `ibex_top` -> `abc_bridge_merged_residual` ->
`abc_bridge_merged_residual_bits` -- so full whole-design flattening
would need real recursive resolution, not a bigger regex).

Explicit non-goals (do not extend this module to do these without
re-scoping first):
  - no RECURSIVE cross-module resolution -- a non-leaf Cell's own pins
    are exposed (so it can be identified as `net.driver`/one of
    `net.loads`), but this module never looks INSIDE another module's
    body to resolve what ultimately drives a non-leaf Cell's own inputs;
    see the multi-level-nesting note above.
  - no port list / port type / module-boundary connectivity modeling
    (mirrors milestone 1's own port-list non-goal)
  - no cross-stage tracing (that's the ORIGINAL sketch's item 4,
    deliberately still deferred -- likely milestone 3)
  - no directive/customization surface -- strictly read-only
  - no generic op-printing syntax support (same as milestone 1)
  - `_split_port_list`'s naive comma-split assumes no pin's TYPE
    contains an internal comma (e.g. a `!hw.struct<a: i1, b: i2>` pin
    type would break it) -- empirically checked against a real file:
    every `hw.struct` occurrence there is inside an `hw.bitcast` or a
    `hw.module` HEADER's own port list (not parsed by this module at
    all), never inside an actual `hw.instance(...)`/`-> (...)` port
    list -- an empirical property of this corpus, not a guarantee.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "MappedNetworkError",
    "Pin",
    "Cell",
    "Net",
    "MappedNetwork",
]


class MappedNetworkError(RuntimeError):
    """Raised when the target module can't be found or its body can't be
    parsed (unbalanced braces -- the same single-line-op empirical
    assumption milestone 1 relies on)."""


_MODULE_HEADER_RE = re.compile(
    r"^\s*hw\.module(\.extern)?\s+(?:private\s+)?@(\w+)")

# One hw.instance op with its full pin list, e.g.:
#   %g00169.Y = hw.instance "g00169" @AOI21_X1(
#       A0: %g00166.Y: i1, A1: %g00168.Y: i1, B0: %g00165.Y: i1) -> (Y: i1)
#       {sv.namehint = "new_n11789"}
# `lhs` is the (possibly comma-separated, for a multi-output cell) LHS
# result-binding, or absent entirely for a zero-result instance.
_CELL_LINE_RE = re.compile(
    r'(?P<lhs>%[\w.]+(?:\s*,\s*%[\w.]+)*\s*=\s*)?'
    r'hw\.instance\s+"(?P<inst>[^"]+)"\s+@(?P<type>\w+)'
    r'\((?P<ins>[^)]*)\)\s*->\s*\((?P<outs>[^)]*)\)')

_NAMEHINT_RE = re.compile(r'sv\.namehint\s*=\s*"([^"]+)"')


@dataclass(frozen=True)
class Pin:
    """One port connection on one Cell instance."""
    name: str       # liberty pin name, e.g. "A0", "Y", "CK"
    net: str        # the LOCAL net name this pin connects to (no leading %)
    is_output: bool


@dataclass(frozen=True)
class Cell:
    """One `hw.instance` inside the target module's body -- either a real
    liberty-cell leaf (`is_leaf=True`, references a `hw.module.extern`)
    or a sub-block instance of another real `hw.module` (`is_leaf=False`,
    `cell_type` is that module's own symbol name, e.g.
    "abc_bridge_merged_residual") -- see the module docstring's milestone
    5 note. Both kinds are valid net drivers/loads; only a leaf Cell's
    `cell_type` is a real liberty cell name."""
    instance_name: str          # e.g. "g00169" or "u_abc_bridge_merged_residual"
    cell_type: str               # e.g. "AOI21_X1", or a
                                  # non-leaf hw.module symbol name
    is_leaf: bool                # True: real liberty cell. False: another
                                  # hw.module instance (opaque sub-block).
    pins: Tuple[Pin, ...]        # input pins then output pin(s), file order
    name_hint: Optional[str]     # sv.namehint if present, e.g. "new_n11789"
                                  # -- cross-references ABC's own internal
                                  # node naming (see the ABC deep-tier
                                  # milestone's write_verilog cross-check
                                  # for the naming conventions this ties to)


@dataclass(frozen=True)
class Net:
    """One local SSA value acting as a net within the target module's own
    namespace. `driver`/`loads` are None/empty for a module-boundary net
    (a primary input, or a value only consumed by the module's own
    hw.output terminator) -- that is the expected, non-error shape for a
    boundary net, not a parse failure."""
    name: str
    driver: Optional[str]        # instance_name of the driving Cell, or None
    loads: Tuple[str, ...]       # instance_names of Cells with an input pin
                                  # on this net, file order


def _split_top_level(s: str, sep: str) -> List[str]:
    """Split `s` on `sep`, but only where bracket-nesting depth is 0 --
    `<...>`/`(...)` don't count as separator boundaries inside a nested
    aggregate TYPE (e.g. a pin typed `!hw.struct<a: i1, b: i2>` has its
    own internal `,` and `:` that must NOT be mistaken for a port-list or
    name/value/type separator; milestone 5 found real struct-typed pins
    on a real 68KB multi-result output-port list where a naive
    `s.split(sep)` silently over-split, producing more "ports" than the
    real LHS count -- this is the fix)."""
    parts = []
    depth = 0
    current: List[str] = []
    for ch in s:
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _split_port_list(s: str) -> List[List[str]]:
    """'A0: %g00166.Y: i1, A1: %g00168.Y: i1' -> [['A0','g00166.Y','i1'],
    ['A1','g00168.Y','i1']]; 'Y: i1' -> [['Y','i1']] (no value field on
    the output side -- see _CELL_LINE_RE's `outs` group). A pin's own
    TYPE text is never further decomposed (this module never uses it) --
    only top-level-depth commas/colons matter, via _split_top_level."""
    result = []
    for part in _split_top_level(s, ','):
        part = part.strip()
        if not part:
            continue
        fields = [f.strip().lstrip('%') for f in _split_top_level(part, ':')]
        result.append(fields)
    return result


class MappedNetwork:
    """Cell/net/pin queries over ONE named module's body in a
    `_mapped.mlir` file. Read-only: nothing here mutates the source file
    or influences any later pipeline stage."""

    def __init__(self, module_name: str, cells: Dict[str, Cell],
                 skipped_multi_output_count: int):
        self.module_name = module_name
        self.cells = cells
        self.skipped_multi_output_count = skipped_multi_output_count
        self._nets: Optional[Dict[str, Net]] = None

    @classmethod
    def from_mlir_file(cls, path: Union[str, Path],
                        module_name: str) -> "MappedNetwork":
        """Parse the named `hw.module`'s body out of a `_mapped.mlir` file
        into a MappedNetwork. EVERY `hw.instance` op becomes a Cell --
        `is_leaf=True` for a `hw.module.extern` reference (a real liberty
        cell), `is_leaf=False` for a reference to another real `hw.module`
        (an opaque sub-block instance, e.g. `abc_bridge_merged_residual`)
        -- see the module docstring's milestone 5 note. A multi-result
        instance (comma-separated LHS) is parsed correctly by positionally
        matching each LHS name against the `-> (name: type, ...)` output
        port list; a genuine LHS/output-list length mismatch (not expected
        for well-formed circt-opt output, but checked rather than assumed)
        is counted in `skipped_multi_output_count`, not silently ignored.
        """
        path = Path(path)

        # Pass 1: which @symbols are hw.module.extern (real liberty
        # leaves) -- needed to set Cell.is_leaf correctly, and to find the
        # target module's own header.
        extern_types: set = set()
        target_is_extern = False
        target_found = False
        with open(path, "r") as f:
            for line in f:
                header = _MODULE_HEADER_RE.match(line)
                if header is None:
                    continue
                is_extern = header.group(1) is not None
                name = header.group(2)
                if is_extern:
                    extern_types.add(name)
                if name == module_name:
                    target_found = True
                    target_is_extern = is_extern

        if not target_found:
            raise MappedNetworkError(
                f"module @{module_name} not found in {path}")
        if target_is_extern:
            raise MappedNetworkError(
                f"@{module_name} is a hw.module.extern (a liberty-cell "
                f"leaf declaration, no body) -- MappedNetwork targets a "
                f"real hw.module with cell instances inside it")

        # Pass 2: scan only the target module's body for hw.instance
        # lines, using the same brace-depth technique milestone 1's
        # mlir_text.py uses to know when the body closes.
        cells: Dict[str, Cell] = {}
        skipped_multi_output = 0
        in_target = False
        depth = 0

        with open(path, "r") as f:
            for line in f:
                if not in_target:
                    header = _MODULE_HEADER_RE.match(line)
                    if header is not None and header.group(2) == module_name:
                        in_target = True
                        depth = line.count("{") - line.count("}")
                        if depth <= 0:
                            raise MappedNetworkError(
                                f"@{module_name}'s header line has no open "
                                f"body -- unexpected for a non-extern "
                                f"module in {path}")
                    continue

                m = _CELL_LINE_RE.search(line)
                if m is not None:
                    inst_name = m.group("inst")
                    cell_type = m.group("type")
                    is_leaf = cell_type in extern_types
                    lhs = m.group("lhs")
                    lhs_names = (
                        [tok.strip().lstrip("%")
                         for tok in lhs.split("=")[0].split(",")]
                        if lhs else [])
                    out_fields = _split_port_list(m.group("outs"))

                    if len(lhs_names) != len(out_fields):
                        # Real mismatch (e.g. LHS count doesn't match the
                        # declared output port count) -- not expected for
                        # well-formed circt-opt output, but checked rather
                        # than assumed; skip the whole cell rather than
                        # guess a pairing.
                        skipped_multi_output += 1
                        continue

                    pins: List[Pin] = []
                    for fields in _split_port_list(m.group("ins")):
                        if len(fields) == 3:
                            pin_name, net, _ty = fields
                            pins.append(Pin(name=pin_name, net=net,
                                             is_output=False))
                    for lhs_value, out in zip(lhs_names, out_fields):
                        pins.append(Pin(name=out[0], net=lhs_value,
                                         is_output=True))

                    hint_m = _NAMEHINT_RE.search(line)
                    cells[inst_name] = Cell(
                        instance_name=inst_name, cell_type=cell_type,
                        is_leaf=is_leaf, pins=tuple(pins),
                        name_hint=hint_m.group(1) if hint_m else None)

                depth += line.count("{") - line.count("}")
                if depth <= 0:
                    break

        if in_target and depth > 0:
            raise MappedNetworkError(
                f"unterminated hw.module @{module_name} in {path} "
                "(unbalanced braces)")

        return cls(module_name=module_name, cells=cells,
                    skipped_multi_output_count=skipped_multi_output)

    def get_cells(self, pattern: str) -> List[Cell]:
        """Every Cell whose cell_type matches the given fnmatch-style
        glob, e.g. "AOI2*" or "DFF*". Sorted by instance_name for a
        deterministic result order."""
        return sorted(
            (c for c in self.cells.values()
             if fnmatch.fnmatch(c.cell_type, pattern)),
            key=lambda c: c.instance_name)

    def cell_type_counts(self) -> Dict[str, int]:
        """Histogram of cell_type -> instance count -- the direct
        replacement for the one-off cell-usage `stat`/grep comparisons
        this project's QoR investigations ran repeatedly by hand."""
        counts: Dict[str, int] = {}
        for c in self.cells.values():
            counts[c.cell_type] = counts.get(c.cell_type, 0) + 1
        return counts

    def find_cell(self, instance_name: str) -> Optional[Cell]:
        return self.cells.get(instance_name)

    def nets(self) -> Dict[str, Net]:
        """Every local net referenced by at least one Cell's Pin, built
        once and cached on this MappedNetwork instance (an in-memory
        index over the already-parsed `cells` dict, not a file re-scan)."""
        if self._nets is None:
            driver: Dict[str, str] = {}
            loads: Dict[str, List[str]] = {}
            for inst_name, cell in self.cells.items():
                for pin in cell.pins:
                    if pin.is_output:
                        driver[pin.net] = inst_name
                    else:
                        loads.setdefault(pin.net, []).append(inst_name)
            names = set(driver) | set(loads)
            self._nets = {
                name: Net(name=name, driver=driver.get(name),
                           loads=tuple(loads.get(name, ())))
                for name in names
            }
        return self._nets

    def get_net(self, name: str) -> Optional[Net]:
        return self.nets().get(name)

    def fanin_cells(self, instance_name: str) -> List[Cell]:
        """Cells directly driving one of `instance_name`'s input pins
        (via a local net this module resolves a driver for). A pin whose
        net has no local driver (a module-boundary input, or a constant)
        is silently excluded -- not an error."""
        cell = self.cells.get(instance_name)
        if cell is None:
            raise MappedNetworkError(
                f"no cell {instance_name!r} in module @{self.module_name}")
        result: List[Cell] = []
        for pin in cell.pins:
            if pin.is_output:
                continue
            net = self.get_net(pin.net)
            if net is not None and net.driver is not None:
                driver_cell = self.cells.get(net.driver)
                if driver_cell is not None:
                    result.append(driver_cell)
        return result

    def fanout_cells(self, instance_name: str) -> List[Cell]:
        """Cells with an input pin directly consuming one of
        `instance_name`'s output nets."""
        cell = self.cells.get(instance_name)
        if cell is None:
            raise MappedNetworkError(
                f"no cell {instance_name!r} in module @{self.module_name}")
        result: List[Cell] = []
        for pin in cell.pins:
            if not pin.is_output:
                continue
            net = self.get_net(pin.net)
            if net is not None:
                for load_inst in net.loads:
                    load_cell = self.cells.get(load_inst)
                    if load_cell is not None:
                        result.append(load_cell)
        return result
