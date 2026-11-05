"""Top-level pipeline orchestration -- the Python equivalent of
scripts/run_circt_native_synth.sh's linear stage sequence.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import List, Optional

from .config import FlowConfig
from . import stages


def setup_libs(design, rail: Optional[str] = None) -> List[Path]:
    """The liberties a rail is timed with at the setup corner: the
    profile's per-corner ones, or its mapping liberties without corners
    (or without a profile)."""
    if design.profile is None:
        return [Path(p) for p in design.synth_lib_tt]
    from . import profile as profile_mod
    prof = profile_mod.load(design.profile)
    return prof.libs_at(rail=rail or design.rail)


def mapping_target(design, name: str, feeding=None, clock_names=None,
                   rail: Optional[str] = None) -> dict:
    """modes.derive_target for `design` (its modes, its mapping_target:),
    the flop overhead from `rail`'s setup-corner flops when it is auto."""
    from . import modes
    spec = design.mapping_target or modes.TargetSpec()
    r = None
    if design.profile is not None:
        from . import profile as profile_mod
        prof = profile_mod.load(design.profile)
        r = prof.rails.get(rail or design.rail or prof.default_rail)
    if design.mapping_target_from_pack and rail is not None and r is not None:
        # The pack's default: the domain's own rail's (rails may differ).
        if r.flow.get("mapping_target") is not None:
            spec = modes.parse_target(r.flow["mapping_target"],
                                      f"rail {rail}: flow")
    overhead = None
    if spec.flop_overhead == "auto":
        overhead = modes.flop_overhead(setup_libs(design, rail),
                                       spec.flop_cells,
                                       r.dont_use if r is not None else ())
    return modes.derive_target(name, design.effective_modes(), feeding, spec,
                               overhead, clock_names)


def calibrate(cfg: FlowConfig) -> FlowConfig:
    """A single-domain run with modes: or mapping_target:: derive the
    value ABC gets, log it, write <out>/mapping_target.json, and return
    cfg with design.abc_delay_target_ps set. Otherwise cfg unchanged."""
    if not cfg.design.calibrate:
        return cfg
    from . import modes
    rec = mapping_target(cfg.design, cfg.design.top_module)
    print(modes.log_target(rec))
    (cfg.out_dir / "mapping_target.json").write_text(
        json.dumps(rec, indent=2) + "\n")
    return dataclasses.replace(cfg, design=dataclasses.replace(
        cfg.design, abc_delay_target_ps=rec["target_ps"]))


def run(cfg: FlowConfig, pnr: bool = False) -> None:
    """Run the full CIRCT-native synthesis flow for `cfg`, then, with `pnr`,
    place and route the netlist on stock ORFS (the flow.yaml's `pnr:`).

    Raises FileNotFoundError up front if any required tool is missing, and
    subprocess.CalledProcessError if any stage's subprocess exits non-zero.
    """
    cfg.validate_tools()
    # A private pack's netlists name its cells: keep them out of the repo's
    # working tree, where one `git add -f` would commit them.
    if (cfg.design.pack_private
            and cfg.out_dir.resolve().is_relative_to(cfg.root.resolve())
            and os.environ.get("CIRCT_ABC_ALLOW_REPO_OUT") != "1"):
        raise ValueError(
            f"out_dir {cfg.out_dir} is inside {cfg.root} and the profile is "
            "a private tech pack; write the run elsewhere (or set "
            "CIRCT_ABC_ALLOW_REPO_OUT=1)")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    if cfg.design.power is not None:
        # Power domains on their own rails (power.py): each domain and the
        # rest of the design go through this same flow, then are stitched.
        from . import power
        power.run(cfg, pnr=pnr)
        return
    # The pack's pass-knob defaults (flow.env): the stages read the
    # CIRCT_FLOW_* knobs from the environment, so a knob the environment
    # already sets keeps its value.
    for k, v in cfg.design.flow_defaults.get("env", {}).items():
        if k not in os.environ:
            os.environ[k] = v
            print(f"[circt_flow] tech pack default {k}={v}")

    cfg = calibrate(cfg)
    print(
        f"[circt_flow] top={cfg.design.top_module} liberty={cfg.design.lib} "
        f"clock_period_ps={cfg.design.clock_period_ps}"
        + (f" abc_delay_target_ps={cfg.design.abc_delay_target_ps}"
           if cfg.design.abc_delay_target_ps is not None else "")
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
    if cfg.design.fanout is not None:
        from . import fanout
        fanout.run(cfg, cfg.design.fanout)
    from . import vtswap
    vspec = vtswap.wanted(cfg)
    if vspec is not None:
        vtswap.run(cfg, vspec)
    from . import sta
    spec = sta.wanted(cfg)
    if spec is not None:
        sta.run(cfg, spec)
    if pnr:
        stages.orfs_pnr(cfg)
