"""Top-level pipeline orchestration -- the Python equivalent of
scripts/run_circt_native_synth.sh's linear stage sequence.
"""
from __future__ import annotations

from .config import FlowConfig
from . import stages


def run(cfg: FlowConfig, pnr: bool = False) -> None:
    """Run the full CIRCT-native synthesis flow for `cfg`, then, with `pnr`,
    place and route the netlist on stock ORFS (the flow.yaml's `pnr:`).

    Raises FileNotFoundError up front if any required tool is missing, and
    subprocess.CalledProcessError if any stage's subprocess exits non-zero.
    """
    cfg.validate_tools()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[circt_flow] top={cfg.design.top_module} liberty={cfg.design.lib} "
        f"clock_period_ps={cfg.design.clock_period_ps}"
    )

    stages.run_lower_ibex(cfg)
    stages.hw_aggregate_to_comb(cfg)

    # Fail fast on a typo'd directive target -- before wasting a real
    # structural_mapping() run -- rather than silently doing nothing.
    # Only reads core_mlir at all if a directive was actually given (an
    # empty DirectiveSet, the default, costs nothing here).
    if cfg.directives.dont_touch_names or cfg.directives.dont_gate_names:
        # top_name is passed explicitly (milestone 10): Design's own
        # last-module-in-file inference is a heuristic milestone 1's own
        # build found to be wrong on a real file, and cfg already knows
        # the real top module -- no reason to rely on inference here.
        cfg.directives.validate_against_core_mlir(
            cfg.core_mlir, top_name=cfg.design.top_module)
        # Milestone 9: a real (existing) target that's structurally
        # ineligible for its enforcement point is not an error (both
        # passes' "when in doubt, no effect" safety philosophy already
        # guarantees it's never wrong) -- just a soft, non-fatal warning
        # so the user knows why it silently did nothing, instead of
        # finding out by comparing before/after cell counts by hand.
        for warning in cfg.directives.eligibility_warnings(cfg.core_mlir):
            print(f"[circt_flow] WARNING: {warning}")

    stages.gen_stubs(cfg)
    stages.structural_mapping(cfg)
    stages.flatten_shell(cfg)
    stages.merge_and_map(cfg)
    stages.export_netlist(cfg)
    stages.opensta_compat_netlist(cfg)
    if pnr:
        stages.orfs_pnr(cfg)
