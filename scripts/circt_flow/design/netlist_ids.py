"""Milestone 4: decoding CIRCT ExportVerilog's hex-inline name-legalization
scheme, so identifiers in the final exported netlist (`1_synth/<top>.v`)
can be correlated back to the MLIR-side identities milestones 1-3 already
resolve.

Ground truth (read directly from source, not inferred from netlist text):
`circt::sv::legalizeName` (third_party/circt/lib/Dialect/SV/SVDialect.cpp,
~lines 122-155). Any identifier character outside `[A-Za-z0-9_$]` (the
first character also excludes digits and `$`) is legalized: ' ' and '.'
collapse to '_'; every OTHER invalid byte is replaced INLINE, with NO
delimiter, by its uppercase-hex ASCII value via `llvm::utohexstr(ch)`
(confirmed from llvm/include/llvm/ADT/StringExtras.h: `LowerCase` defaults
to false, no zero-padding). This is a CIRCT-specific mechanism -- it is
NOT the standard IEEE Verilog `\\name ` escaped-identifier syntax (a
different, unrelated convention used by ABC's own Verilog writer; see the
ABC deep-tier milestone's own handling of THAT one). Conflating the two
would be a real mistake; this module only ever decodes CIRCT's
ExportVerilog scheme.

Empirically confirmed (not assumed) against a real `ibex_top_mapped.mlir`:
of 6,283 distinct `sv.namehint` values in this project's real pipeline
output, EVERY one consists only of `[A-Za-z0-9_/]` characters. `/` (this
project's own instance-path-joining separator, introduced by
outline-residual-logic/merge-residual-modules/hw-flatten-modules) is the
ONLY character this project's real names ever need to escape, and it
hex-encodes to exactly "2F" (uppercase, two digits, matching
`utohexstr`'s confirmed default behavior for a single byte in this
range). Zero namehints WITHOUT a `/` contain the literal substring "2F"
as ordinary characters anywhere in the same real file.

IMPORTANT LIMIT, stated plainly: `legalizeName`'s no-delimiter inline hex
scheme is not, in general, uniquely invertible from the string alone -- a
name that happened to contain the literal two characters "2" then "F"
adjacent to each other (two perfectly ordinary identifier characters)
would be indistinguishable from an escaped `/` after encoding. This
module's `decode_netlist_identifier` treats every "2F" occurrence as an
escaped `/`, which is an EMPIRICAL property of this project's actual
naming corpus (verified above), not a general guarantee -- if a future
name legitimately contains literal "2F", this decoder will misdecode it.
No such case has ever been observed in this project's real output.
"""
from __future__ import annotations

_SLASH_ESCAPE = "2F"


def decode_netlist_identifier(escaped: str) -> str:
    """Decode a CIRCT-legalized Verilog identifier back to its original
    `/`-separated form, e.g.
    "u_ibex_core2Fif_stage_i2Fcompressed_decoder_i2Fcm_state_q_bit0" ->
    "u_ibex_core/if_stage_i/compressed_decoder_i/cm_state_q_bit0". See
    the module docstring for the empirical basis and its limits.
    """
    return escaped.replace(_SLASH_ESCAPE, "/")


def encode_netlist_identifier(decoded: str) -> str:
    """Inverse of decode_netlist_identifier: legalize a `/`-separated
    path (as it appears in `_mapped.mlir`'s `hw.instance` names) into the
    form it takes in the final exported Verilog netlist, e.g.
    "u_ibex_core/if_stage_i/compressed_decoder_i/cm_state_q_bit0" ->
    "u_ibex_core2Fif_stage_i2Fcompressed_decoder_i2Fcm_state_q_bit0".
    Exact inverse of decode_netlist_identifier for names containing only
    `[A-Za-z0-9_/]`, which is this project's entire real namehint corpus
    (see module docstring) -- not a general Verilog-name legalizer.
    """
    return decoded.replace("/", _SLASH_ESCAPE)


def netlist_path_components(escaped: str) -> tuple[str, ...]:
    """Split a legalized netlist identifier on its decoded `/` separators
    into hierarchy-path components, e.g.
    "u_ibex_core2Fif_stage_i2Fcm_state_q_bit0" ->
    ("u_ibex_core", "if_stage_i", "cm_state_q_bit0")."""
    return tuple(decode_netlist_identifier(escaped).split("/"))
