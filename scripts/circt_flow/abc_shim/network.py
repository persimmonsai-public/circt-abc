"""Read-only Python view over the CURRENT network held by the global ABC
frame -- "deep tier" milestone 1 of the ABC-Python integration (the
"shallow" tier is the in-process command runner in __init__.py).

No mutation, no rewriting, no cut enumeration: this module only exposes
what abc_shim.cpp's read-only struct-walk functions already answer about
whatever network AbcSession.execute()'s most recent script call left
current. It never changes what recipe runs or what write_verilog would
emit.
"""
from __future__ import annotations

import ctypes
from collections import Counter
from dataclasses import dataclass, field

_NAME_BUF_LEN = 256


def _read_name(lib, fn_name: str, obj_id: int) -> str:
    buf = ctypes.create_string_buffer(_NAME_BUF_LEN)
    getattr(lib, fn_name)(obj_id, buf, _NAME_BUF_LEN)
    return buf.value.decode("utf-8")


@dataclass(frozen=True)
class AbcObject:
    """One object (PI/PO/node) in the current ABC network."""

    id: int
    is_pi: bool
    is_po: bool
    is_node: bool
    name: str
    fanins: tuple[int, ...] = field(default_factory=tuple)
    fanouts: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AbcCell(AbcObject):
    """An AbcObject that is a mapped node with a real liberty cell
    assigned (a leaf of the merged/mapped combinational network
    abc-bridge's recipe produces)."""

    liberty_cell_name: str = ""


class MappedNetwork:
    """A point-in-time, read-only snapshot of the ABC frame's current
    network. Construct via AbcSession.network(), not directly."""

    def __init__(self, lib):
        self._lib = lib
        self._num_objects = lib.abc_shim_num_objects()

    @property
    def num_objects(self) -> int:
        """Total object slots, including any deleted holes -- iterate
        `.objects` rather than looping 0..num_objects yourself unless you
        specifically need to see the holes (an id with is_pi=is_po=
        is_node=False and an empty name)."""
        return self._num_objects

    def _object_at(self, i: int) -> AbcObject | None:
        is_pi = bool(self._lib.abc_shim_obj_is_pi(i))
        is_po = bool(self._lib.abc_shim_obj_is_po(i))
        is_node = bool(self._lib.abc_shim_obj_is_node(i))
        name = _read_name(self._lib, "abc_shim_obj_name", i)
        if not (is_pi or is_po or is_node) and not name:
            return None  # a deleted-object hole
        num_fanins = self._lib.abc_shim_obj_num_fanins(i)
        fanins = tuple(
            self._lib.abc_shim_obj_fanin(i, k) for k in range(num_fanins))
        num_fanouts = self._lib.abc_shim_obj_num_fanouts(i)
        fanouts = tuple(
            self._lib.abc_shim_obj_fanout(i, k) for k in range(num_fanouts))

        if is_node:
            cell_name = _read_name(
                self._lib, "abc_shim_obj_liberty_cell_name", i)
            if cell_name:
                return AbcCell(
                    id=i, is_pi=is_pi, is_po=is_po, is_node=is_node,
                    name=name, fanins=fanins, fanouts=fanouts,
                    liberty_cell_name=cell_name)

        return AbcObject(
            id=i, is_pi=is_pi, is_po=is_po, is_node=is_node, name=name,
            fanins=fanins, fanouts=fanouts)

    @property
    def objects(self) -> list[AbcObject]:
        """Every live object (PIs, POs, and nodes), skipping deleted
        holes in ABC's internal object-id space."""
        result = []
        for i in range(self._num_objects):
            obj = self._object_at(i)
            if obj is not None:
                result.append(obj)
        return result

    @property
    def cells(self) -> list[AbcCell]:
        """Every mapped node with a real liberty cell assigned --
        excludes PIs/POs and any unmapped node (empty liberty cell
        name)."""
        return [obj for obj in self.objects if isinstance(obj, AbcCell)]

    def cell_type_counts(self) -> dict[str, int]:
        """How many instances of each liberty cell type this network's
        `.cells` currently contains -- e.g. for a RecipeExperiment
        snapshot (deep tier milestone 2), the exact per-cell-type deltas
        one recipe step made vs. the last.

        NOTE: this is this package's OWN cell_type_counts, computed from
        the ABC shim's `.cells` (liberty cell names read directly off the
        live Abc_Ntk_t). It is unrelated to the identically-named
        `MappedNetwork.cell_type_counts()` in
        scripts/circt_flow/design/mapped_network.py, which parses a
        DIFFERENT thing (post-hoc MLIR text) for the unrelated
        design-management-layer tooling. The two share a name only; this
        method does not call that one."""
        return dict(Counter(cell.liberty_cell_name for cell in self.cells))
