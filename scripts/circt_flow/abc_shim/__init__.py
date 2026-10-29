"""ctypes wrapper around ABC's in-process entry points (Abc_Start /
Cmd_CommandExecute / Abc_Stop), exposed via libabc_shim.so (see
abc_shim.cpp and build.sh in this directory).

This is the "shallow" ABC-Python integration: run an ABC command script
in-process instead of via a subprocess. It does not touch
mapper/lib/ABCBridge/ABCBridge.cpp's existing subprocess-based
invocation, which remains the flow's actual ABC entry point until this
is validated and adopted deliberately.

Usage:

    from circt_flow.abc_shim import AbcSession

    with AbcSession() as abc:
        abc.execute("read_lib foo.lib; read_aiger in.aig; strash; ...")

Process-global caveat: ABC's C globals mean at most one AbcSession may
be active per process, and Abc_Start()/Abc_Stop() are not safe to call
concurrently from multiple threads.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from .network import MappedNetwork

__all__ = ["AbcSession", "AbcCommandError", "MappedNetwork"]

_LIB_PATH = Path(__file__).parent / "libabc_shim.so"

_NAME_BUF_LEN = 256


class AbcCommandError(RuntimeError):
    """Raised when an ABC command script returns a non-zero status."""


class AbcSession:
    """One ABC frame lifetime: Abc_Start() on entry, Abc_Stop() on exit."""

    def __init__(self, lib_path: Path | str = _LIB_PATH):
        self._lib = ctypes.CDLL(str(lib_path))
        self._lib.abc_shim_start.restype = None
        self._lib.abc_shim_stop.restype = None
        self._lib.abc_shim_execute.argtypes = [ctypes.c_char_p]
        self._lib.abc_shim_execute.restype = ctypes.c_int

        # Deep-tier milestone 1: read-only network inspection.
        self._lib.abc_shim_num_objects.argtypes = []
        self._lib.abc_shim_num_objects.restype = ctypes.c_int
        self._lib.abc_shim_obj_is_pi.argtypes = [ctypes.c_int]
        self._lib.abc_shim_obj_is_pi.restype = ctypes.c_int
        self._lib.abc_shim_obj_is_po.argtypes = [ctypes.c_int]
        self._lib.abc_shim_obj_is_po.restype = ctypes.c_int
        self._lib.abc_shim_obj_is_node.argtypes = [ctypes.c_int]
        self._lib.abc_shim_obj_is_node.restype = ctypes.c_int
        self._lib.abc_shim_obj_num_fanins.argtypes = [ctypes.c_int]
        self._lib.abc_shim_obj_num_fanins.restype = ctypes.c_int
        self._lib.abc_shim_obj_fanin.argtypes = [ctypes.c_int, ctypes.c_int]
        self._lib.abc_shim_obj_fanin.restype = ctypes.c_int
        self._lib.abc_shim_obj_num_fanouts.argtypes = [ctypes.c_int]
        self._lib.abc_shim_obj_num_fanouts.restype = ctypes.c_int
        self._lib.abc_shim_obj_fanout.argtypes = [ctypes.c_int, ctypes.c_int]
        self._lib.abc_shim_obj_fanout.restype = ctypes.c_int
        self._lib.abc_shim_obj_name.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self._lib.abc_shim_obj_name.restype = None
        self._lib.abc_shim_obj_liberty_cell_name.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self._lib.abc_shim_obj_liberty_cell_name.restype = None

        # Deep-tier milestone 2: cheap structural read-outs for recipe
        # experimentation.
        self._lib.abc_shim_num_nodes.argtypes = []
        self._lib.abc_shim_num_nodes.restype = ctypes.c_int
        self._lib.abc_shim_num_levels.argtypes = []
        self._lib.abc_shim_num_levels.restype = ctypes.c_int
        self._lib.abc_shim_num_pis.argtypes = []
        self._lib.abc_shim_num_pis.restype = ctypes.c_int
        self._lib.abc_shim_num_pos.argtypes = []
        self._lib.abc_shim_num_pos.restype = ctypes.c_int

        self._started = False

    def __enter__(self) -> "AbcSession":
        self._lib.abc_shim_start()
        self._started = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._started:
            self._lib.abc_shim_stop()
            self._started = False

    def execute(self, command: str) -> None:
        """Run a `;`-separated ABC command script, matching `abc -q
        "<script>"`'s own semantics. Raises AbcCommandError on failure."""
        if not self._started:
            raise RuntimeError("AbcSession.execute() called outside a "
                                "'with' block")
        rc = self._lib.abc_shim_execute(command.encode("utf-8"))
        if rc != 0:
            raise AbcCommandError(
                f"ABC command script failed (rc={rc}): {command}")

    def num_nodes(self) -> int:
        """Node count of the current network (Abc_NtkNodeNum) -- deep
        tier milestone 2. 0 if no network is current."""
        return self._lib.abc_shim_num_nodes()

    def num_levels(self) -> int:
        """Logic-level count of the current network (Abc_NtkLevel, a
        real DFS-based recomputation, the same one ABC's own "ps"/
        "print_stats" command reports) -- deep tier milestone 2. 0 if no
        network is current."""
        return self._lib.abc_shim_num_levels()

    def num_pis(self) -> int:
        """Primary-input count of the current network (Abc_NtkPiNum) --
        deep tier milestone 2. 0 if no network is current."""
        return self._lib.abc_shim_num_pis()

    def num_pos(self) -> int:
        """Primary-output count of the current network (Abc_NtkPoNum) --
        deep tier milestone 2. 0 if no network is current."""
        return self._lib.abc_shim_num_pos()

    def network(self) -> MappedNetwork:
        """A read-only, point-in-time snapshot of whatever network is
        currently held by the global ABC frame (i.e. whatever the most
        recent execute() call left current). Raises RuntimeError if no
        network has been read/built yet.

        This is milestone 1 of the "deep tier" ABC-Python integration
        (see network.py): read-only inspection only -- no mutation, no
        rewriting, and it does not change what recipe runs or what
        write_verilog would emit.
        """
        if not self._started:
            raise RuntimeError("AbcSession.network() called outside a "
                                "'with' block")
        return MappedNetwork(self._lib)
