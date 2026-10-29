"""Design-management layer.

Milestone 1 (mlir_text.py): read-only hierarchical instance addressing
over one pipeline stage's IR (`${TOP}_struct.mlir`).

Milestone 2 (mapped_network.py): read-only cell/net/pin queries over one
named module's body in `${TOP}_mapped.mlir` (e.g.
`abc_bridge_merged_residual_bits`, where the design's real mapped
liberty cells live).

Milestone 3 (cross_stage.py): cross-stage tracing for REGISTERS only
(register-mapper's `<name>_bit<N>` naming convention is stable from
`_struct.mlir` through `_mapped.mlir` to the final netlist; arbitrary
combinational signal names are not, and tracing those is out of scope).

Milestone 4 (netlist_ids.py + cross_stage.trace_register_to_netlist):
decodes CIRCT ExportVerilog's hex-inline name-legalization scheme (the
"2F-escaped instance names" this project has referenced since early on),
closing the netlist-tracing gap milestone 3 deferred -- a
RegisterStageSighting from `_mapped.mlir` can now be extended all the way
into the final exported netlist.

Milestone 5 (mapped_network.py, extending milestone 2 in place): closes
milestone 2's own "cross-module net resolution" deferral -- disproved the
premise that `<instance>.<result>`-qualified value names needed new
resolution machinery (they're just CIRCT's ordinary multi-result
`hw.instance` naming) and fixed the two real parser gaps that were
actually blocking it: multi-result LHS lines are now parsed (not
skipped), and a non-extern `hw.instance` (a sub-block instance of
another real `hw.module`) is now a `Cell` too (`is_leaf=False`), not
silently excluded. One level of cross-module resolution only -- see the
module's own docstring for the multi-level-nesting non-goal.

Milestone 6 (directives.py): the directive/customization surface's first
real slice -- `DontTouchDirective`/`DirectiveSet`, naming a register (by
its bare `${TOP}_core.mlir` SSA name) to exclude from
`NarrowDeadRegisterBitsPass`'s narrowing entirely, validated against a
real `core.mlir` and rendered to the exact `circt-abc-opt` option string
that enforces it. Unlike milestones 1-5 (pure read-only inspection),
this milestone's enforcement half touches `mapper/`
(`NarrowDeadRegisterBitsPass.cpp`'s new `dont-touch` option) -- see that
option's own comment and this module's docstring for the full
correctness reasoning and the four independent checks that verified it.

Milestone 7 (directives.py, extending milestone 6's file in place): a
SECOND real enforcement point, proving the directive pattern generalizes
rather than being a one-off tied to narrow-dead-register-bits' own lucky
pre-existing branch -- `DontGateDirective`, naming a register to exclude
from `ClockGateInferencePass`'s clock-gating (leaving it an ordinary
ungated hold-mux firreg), enforced by that pass's own separately-added
`dont-touch` option. Chosen after investigating three other candidate
passes and finding this the safest fit (see directives.py's own
docstring); verified with the same four-check rigor as milestone 6, on
the full real design (all 76 real gated firregs named at once reproduces
output byte-identical to disabling clock-gating outright, surviving
unchanged through register-mapper).

See each module's own docstring for the full rationale/spec.

Usage:

    from circt_flow.design import Design, MappedNetwork

    design = Design.from_mlir_file(cfg.struct_mlir, top_name=cfg.design.top_module)
    design.instance_count("ibex_csr")          # -> int
    design.get_instances("u_ibex_core/*")      # -> list[HierNode]
    design.find_instance("u_ibex_core/id_stage_i/controller_i")  # -> HierNode | None

    net = MappedNetwork.from_mlir_file(cfg.mapped_mlir, "abc_bridge_merged_residual_bits")
    net.cell_type_counts()                     # -> {"AOI21_X1": 1234, ...}
    net.get_cells("DFF*")                      # -> list[Cell]
    net.fanin_cells("g00169")                  # -> list[Cell]

    trace = trace_register_across_stages(
        "cm_state_q", struct_mlir=cfg.struct_mlir, mapped_mlir=cfg.mapped_mlir)
    trace["struct"].bit_names                  # -> ("cm_state_q_bit0", "cm_state_q_bit1", "cm_state_q_bit2")
    trace["mapped"].bit_names                  # -> same 3 names (mapped.mlir's own hierarchy-path prefix stripped)
"""
from __future__ import annotations

from .mlir_text import Design, DesignError, HierNode, Module, ModuleInstance
from .mapped_network import Cell, MappedNetwork, MappedNetworkError, Net, Pin
from .cross_stage import (
    CrossStageError,
    NetlistCellSighting,
    NetlistTraceError,
    RegisterStageSighting,
    find_netlist_instance,
    trace_register_across_stages,
    trace_register_in_file,
    trace_register_to_netlist,
)
from .netlist_ids import (
    decode_netlist_identifier,
    encode_netlist_identifier,
    netlist_path_components,
)
from .directives import (
    DirectiveError,
    DirectiveSet,
    DontGateDirective,
    DontTouchDirective,
)

__all__ = [
    "Design",
    "DesignError",
    "HierNode",
    "Module",
    "ModuleInstance",
    "Cell",
    "MappedNetwork",
    "MappedNetworkError",
    "Net",
    "Pin",
    "CrossStageError",
    "RegisterStageSighting",
    "trace_register_across_stages",
    "trace_register_in_file",
    "NetlistCellSighting",
    "NetlistTraceError",
    "find_netlist_instance",
    "trace_register_to_netlist",
    "decode_netlist_identifier",
    "encode_netlist_identifier",
    "netlist_path_components",
    "DirectiveError",
    "DirectiveSet",
    "DontGateDirective",
    "DontTouchDirective",
]
