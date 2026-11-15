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


def prepare(cfg: FlowConfig, executed: Optional[list] = None) -> Optional[FlowConfig]:
    """What the flow does before its first stage: check the tools and the
    output directory, write settings.json (stage "start"), and, for a design
    without power domains, reset the executed-command record (to `executed`,
    a resumed run's earlier commands), print the tech-pack defaults and
    calibrate. Returns the calibrated config, or None for a power-domain
    design (power.run takes the whole flow)."""
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
    from . import provenance_settings
    provenance_settings.write(cfg, stage="start")
    if cfg.design.power is not None:
        return None
    # settings.json's pipeline: the commands of this run only (a power
    # flow's sub-runs each record their own)
    stages.EXECUTED.clear()
    stages.EXECUTED.extend(executed or ())
    # The options were resolved when the config was loaded (options.py);
    # the stages read cfg.settings, never the environment. The pack's
    # defaults (flow.env, flow.options) are one of its sources.
    for name, v in sorted(cfg.settings.overridden().items()):
        if cfg.settings.source(name) == "techpack" and \
                not name.startswith("flow.pack."):
            print(f"[circt_flow] tech pack default {name}={v}")

    cfg = calibrate(cfg)
    print(
        f"[circt_flow] top={cfg.design.top_module} liberty={cfg.design.lib} "
        f"clock_period_ps={cfg.design.clock_period_ps}"
        + (f" abc_delay_target_ps={cfg.design.abc_delay_target_ps}"
           if cfg.design.abc_delay_target_ps is not None else "")
    )
    return cfg


# ---------------------------------------------------------------- stages
# The flow as named stages (the session API runs them one at a time,
# pipeline.run all in order). Each takes the calibrated config and returns
# None, or "skipped" when the stage has nothing to do for this config
# (fanout without a fanout_buffer:, vt_swap / sta not wanted). `force`: the
# stage was asked for by name (Session.vt_swap()), which is what --vt-swap /
# --sta mean.

def elaborate(cfg: FlowConfig, force: bool = False):
    """Front end and LLHD lowering, de-aggregation, directive checks, cell
    stubs: the design as core IR (cfg.core_mlir)."""
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


def map_datapath(cfg: FlowConfig, force: bool = False):
    """Registers, clock gating, datapath operators (adders, multipliers,
    shifters) and the structural mapper; residual logic outlined
    (cfg.struct_mlir)."""
    stages.structural_mapping(cfg)


def syn_map(cfg: FlowConfig, force: bool = False):
    """Flatten, merge the residual logic, map it with ABC, export the
    netlist and its OpenSTA-compatible form."""
    stages.flatten_shell(cfg)
    stages.merge_and_map(cfg)
    stages.export_netlist(cfg)
    stages.opensta_compat_netlist(cfg)


def fanout_stage(cfg: FlowConfig, force: bool = False):
    """Buffer high-fanout nets (the flow.yaml's fanout_buffer:)."""
    if cfg.design.fanout is None:
        return "skipped"
    from . import fanout
    fanout.run(cfg, cfg.design.fanout)


def vt_swap_stage(cfg: FlowConfig, force: bool = False):
    """Threshold-flavour swap with OpenSTA (--vt-swap, or vt_swap:)."""
    from . import vtswap
    if force and not cfg.run_vt_swap:
        cfg = dataclasses.replace(cfg, run_vt_swap=True)
    vspec = vtswap.wanted(cfg)
    if vspec is None:
        return "skipped"
    vtswap.run(cfg, vspec)


def sta_stage(cfg: FlowConfig, force: bool = False):
    """Multi-corner multi-mode OpenSTA (--sta, or sta:)."""
    from . import sta
    if force and not cfg.run_sta:
        cfg = dataclasses.replace(cfg, run_sta=True)
    spec = sta.wanted(cfg)
    if spec is None:
        return "skipped"
    sta.run(cfg, spec)


def place_and_route(cfg: FlowConfig, force: bool = False):
    """Place and route the OpenSTA netlist (the flow.yaml's pnr:)."""
    stages.orfs_pnr(cfg)


# (name, function, settings.json stage label written after it in a full run)
SYNTH_STAGES = (("elaborate", elaborate), ("map_datapath", map_datapath),
                ("syn_map", syn_map), ("fanout", fanout_stage),
                ("vt_swap", vt_swap_stage), ("sta", sta_stage))
PNR_STAGES = (("place_and_route", place_and_route),)
STAGE_NAMES = tuple(n for n, _ in SYNTH_STAGES + PNR_STAGES)


def _call(name, fn, cfg, on_stage):
    if on_stage is None:
        return fn(cfg)
    return on_stage(name, fn, cfg)


def run(cfg: FlowConfig, pnr: bool = False, on_stage=None) -> None:
    """Run the full CIRCT-native synthesis flow for `cfg`, then, with `pnr`,
    place and route the netlist on stock ORFS (the flow.yaml's `pnr:`).

    `on_stage(name, fn, cfg)`, when given, is called instead of each stage
    function `fn(cfg)` (the session's hooks and results); it must call fn.

    Raises FileNotFoundError up front if any required tool is missing, and
    subprocess.CalledProcessError if any stage's subprocess exits non-zero.
    """
    from . import provenance_settings
    run_cfg = prepare(cfg)
    if run_cfg is None:
        # Power domains on their own rails (power.py): each domain and the
        # rest of the design go through this same flow, then are stitched.
        from . import power
        power.run(cfg, pnr=pnr)
        return
    cfg = run_cfg
    for name, fn in SYNTH_STAGES:
        _call(name, fn, cfg, on_stage)
    provenance_settings.write(cfg, stage="synthesis")
    if pnr:
        for name, fn in PNR_STAGES:
            _call(name, fn, cfg, on_stage)
        provenance_settings.write(cfg, stage="pnr")
