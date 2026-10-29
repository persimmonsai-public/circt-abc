"""Milestone 3: cross-stage tracing for REGISTERS only, across MLIR
pipeline stages (`_struct.mlir`, `_mapped.mlir`) -- NOT the final
exported Verilog netlist (see "Non-goal" below).

Scope, decided empirically: register-mapper bit-blasts every
`seq.firreg` named `<name>` into one `hw.instance "<name>_bit<N>"` per
bit (`N` from 0, contiguous, even when `--narrow-dead-register-bits`
narrowed the register first -- narrowing shrinks the bit COUNT, it does
not leave index gaps). This naming survives from `_struct.mlir`
(register-mapper's own direct output) through `_mapped.mlir` (after
merge-residual-modules/abc-bridge) -- verified on a 3-bit register
(`cm_state_q`, in `ibex_compressed_decoder`) and a 64-bit one
(`counter_q`), both with zero gaps.

One real wrinkle found and handled: `_struct.mlir` (pre-`--hw-flatten-
modules`) has the BARE instance name (`"cm_state_q_bit0"`), but
`_mapped.mlir` (post-flatten) has it prefixed with the full `/`-joined
shell-instance path (`"u_ibex_core/if_stage_i/compressed_decoder_i/
cm_state_q_bit0"`) -- the regex below matches an optional `.../` prefix
and always extracts just the trailing `<name>_bit<N>` part, so both
forms resolve to the same register name.

Milestone 4 UPDATE: the final netlist's "2F-escaped instance name" gap
noted below is now closed by `netlist_ids.py` (see that module's
docstring for the full, source-grounded decoding story) and
`trace_register_to_netlist` at the bottom of this file, which extends a
`mapped_mlir` sighting all the way into `1_synth/<top>.v`. The original
non-goal text is kept below for history/context.

Original non-goal text: the final exported netlist (`1_synth/<top>.v`)
uses a THIRD, different encoding for this same identifier -- CIRCT's
ExportVerilog hex-escapes the `/` path separator to `2F` and produces a
bare (not MLIR-quoted) Verilog identifier (e.g.
`u_ibex_core2Fif_stage_i2Fcompressed_decoder_i2Fcm_state_q_bit0`) --
this is the same "2F-escaped instance names" convention referenced
elsewhere in this project's history. Since Verilog's instance-
declaration syntax is a different grammar entirely (no `hw.instance`/
`@module`), tracing into the netlist needs a separate Verilog-aware
parser, not a reuse of this regex -- deferred at the time this docstring
was first written, now addressed (see UPDATE above).

This also does NOT extend to arbitrary combinational signals: ABC's own
mapping stage regenerates internal node names with no correspondence to
source RTL (already established by the ABC deep-tier milestone 1's own
`write_verilog`-cross-check work -- e.g. `write_verilog` prepending
`new_` to auto-generated names). Only register (`seq.firreg`-derived)
identities are stable enough to trace this way; tracing arbitrary
combinational signals is an explicit non-goal here, not an oversight.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .netlist_ids import encode_netlist_identifier

# Matches an hw.instance whose (possibly `/`-path-prefixed, per the
# struct.mlir-vs-mapped.mlir wrinkle above) name ends in `<name>_bit<N>`.
# Group 1 (the prefix, INCLUDING its trailing `/` if present, else "") is
# now captured, not discarded -- trace_register_to_netlist needs the full
# qualified path to disambiguate repeated-module instances (two different
# instances of the same leaf module both have a `..._bit0`, but only one
# has the specific hierarchy prefix a given call is asking about).
_BIT_INSTANCE_RE = re.compile(
    r'hw\.instance\s+"((?:[^"]*/)?)([A-Za-z_][A-Za-z0-9_]*)_bit(\d+)"\s+@([A-Za-z_][A-Za-z0-9_]*)'
)


class CrossStageError(RuntimeError):
    """Raised when a register trace finds no matching bit instances."""


@dataclass(frozen=True)
class RegisterStageSighting:
    """One register's bit-instance sighting in a single pipeline-stage file."""

    stage_file: str
    bit_names: tuple[str, ...]  # e.g. ("cm_state_q_bit0", "cm_state_q_bit1", ...)
    cell_types: tuple[str, ...]  # liberty/DFF cell type per bit, same order
    full_paths: tuple[str, ...]  # bit_names, each with its full `/`-qualified
                                 # hierarchy prefix if the file has one (e.g.
                                 # "u_ibex_core/if_stage_i/.../cm_state_q_bit0"
                                 # in _mapped.mlir; == bit_names in _struct.mlir,
                                 # which has no such prefix). Same order as
                                 # bit_names/cell_types.

    @property
    def bit_width(self) -> int:
        return len(self.bit_names)


def trace_register_in_file(path: Path | str, register_name: str) -> RegisterStageSighting:
    """Find `register_name`'s per-bit DFF instances in one MLIR
    pipeline-stage file (`_struct.mlir` or `_mapped.mlir` -- NOT the
    final exported `.v` netlist, which uses a different encoding and
    grammar entirely; see trace_register_to_netlist for that). Raises
    CrossStageError if no `_bit<N>` instance of this register is found
    in the file at all.

    Bits are returned in ascending index order starting at 0; this
    project's register-mapper always produces a contiguous 0..width-1
    range (verified empirically, including on narrowed registers -- see
    this module's docstring), but callers should not assume the returned
    bit_width equals the register's ORIGINAL (pre-narrowing) width from
    `_core.mlir` -- narrow-dead-register-bits may have shrunk it.
    """
    path = Path(path)
    text = path.read_text()
    bits: dict[int, tuple[str, str]] = {}  # index -> (cell_type, full_path)
    for m in _BIT_INSTANCE_RE.finditer(text):
        prefix, name, bit_idx, cell_type = m.group(1), m.group(2), m.group(3), m.group(4)
        if name != register_name:
            continue
        bits[int(bit_idx)] = (cell_type, f"{prefix}{name}_bit{bit_idx}")
    if not bits:
        raise CrossStageError(
            f"no '{register_name}_bit<N>' instance found in {path}")
    ordered = sorted(bits)
    return RegisterStageSighting(
        stage_file=str(path),
        bit_names=tuple(f"{register_name}_bit{i}" for i in ordered),
        cell_types=tuple(bits[i][0] for i in ordered),
        full_paths=tuple(bits[i][1] for i in ordered),
    )


def trace_register_across_stages(
    register_name: str,
    *,
    struct_mlir: Path | str | None = None,
    mapped_mlir: Path | str | None = None,
) -> dict[str, RegisterStageSighting]:
    """Trace `register_name` across whichever of the two post-register-
    mapper MLIR stage files are given (either or both; pass only the
    ones you have). Returns a dict keyed by stage label ("struct",
    "mapped") to the sighting found there -- a stage is omitted from the
    result (not raised) if its path is None. Raises CrossStageError from
    the underlying trace_register_in_file call if a GIVEN path doesn't
    contain the register at all (a real, reportable finding: the trail
    went cold at that stage). Use trace_register_to_netlist separately to
    extend a "mapped" sighting into the final exported netlist (milestone
    4) -- kept as a separate call rather than folded in here, since it
    needs a THIRD file (the netlist) with a different failure mode
    (NetlistTraceError, not CrossStageError).
    """
    result: dict[str, RegisterStageSighting] = {}
    for label, path in (
        ("struct", struct_mlir),
        ("mapped", mapped_mlir),
    ):
        if path is not None:
            result[label] = trace_register_in_file(path, register_name)
    return result


# --- Milestone 4: extending a "mapped" sighting into the final netlist ----


class NetlistTraceError(RuntimeError):
    """Raised when a decoded MLIR-side instance path can't be found as a
    cell instantiation in the final exported netlist."""


@dataclass(frozen=True)
class NetlistCellSighting:
    """One resolved cell instantiation in the final exported netlist,
    corresponding to one bit of a RegisterStageSighting from
    `_mapped.mlir`."""

    encoded_name: str  # as it literally appears in the netlist, e.g.
                        # "u_ibex_core2Fif_stage_i2F...cm_state_q_bit0"
    decoded_path: str  # the `/`-separated form, e.g.
                        # "u_ibex_core/if_stage_i/.../cm_state_q_bit0"
    cell_type: str      # e.g. "DFFR_X1"
    line_no: int        # 1-based line number of the instantiation


_NETLIST_INSTANCE_RE_CACHE: dict[str, re.Pattern] = {}


def _netlist_instance_pattern(encoded_name: str) -> re.Pattern:
    pattern = _NETLIST_INSTANCE_RE_CACHE.get(encoded_name)
    if pattern is None:
        pattern = re.compile(
            r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]+" + re.escape(encoded_name)
            + r"[ \t]*\(",
            re.MULTILINE,
        )
        _NETLIST_INSTANCE_RE_CACHE[encoded_name] = pattern
    return pattern


def find_netlist_instance(
    netlist_path: Path | str, decoded_path: str
) -> NetlistCellSighting:
    """Find the cell instantiation in the final exported netlist
    (`1_synth/<top>.v`) for `decoded_path` -- a `/`-separated instance
    path exactly as it appears in one of `_mapped.mlir`'s `hw.instance`
    names (i.e. one of a RegisterStageSighting's `full_paths`, NOT a
    bare `bit_names` entry, which lacks the hierarchy prefix needed to
    disambiguate repeated module instances). Encodes it via
    `netlist_ids.encode_netlist_identifier` and searches for an EXACT
    instance-name match at the start of a Verilog instantiation line --
    not a general Verilog parse, deliberately: this project's netlists
    are CIRCT's own single-shot `--export-verilog` output with one
    instantiation per source line, so an anchored line match is exact
    and sufficient; the earlier "needs a real Verilog parser" framing in
    this file's docstring referred to parsing the netlist's full grammar
    to independently DISCOVER paths, which this function does not do --
    it only confirms a path already known from the MLIR side. Raises
    NetlistTraceError if no matching instantiation line is found.
    """
    encoded = encode_netlist_identifier(decoded_path)
    text = Path(netlist_path).read_text()
    m = _netlist_instance_pattern(encoded).search(text)
    if not m:
        raise NetlistTraceError(
            f"no cell instantiation named '{encoded}' (decoded: "
            f"'{decoded_path}') found in {netlist_path}")
    line_no = text.count("\n", 0, m.start()) + 1
    return NetlistCellSighting(
        encoded_name=encoded,
        decoded_path=decoded_path,
        cell_type=m.group(1),
        line_no=line_no,
    )


def trace_register_to_netlist(
    mapped_sighting: RegisterStageSighting, netlist_path: Path | str
) -> tuple[NetlistCellSighting, ...]:
    """Extend a `_mapped.mlir` RegisterStageSighting (from
    trace_register_in_file/trace_register_across_stages) into the final
    exported netlist -- one NetlistCellSighting per bit, same order as
    `mapped_sighting.full_paths`. Raises NetlistTraceError (via
    find_netlist_instance) if any bit's instantiation can't be found."""
    return tuple(
        find_netlist_instance(netlist_path, full_path)
        for full_path in mapped_sighting.full_paths
    )
