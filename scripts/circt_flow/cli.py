"""CLI entry point for circt_flow.

Invocation (drop-in replacement for run_circt_native_synth.sh):

    python3 -m circt_flow <flow.yaml> <out_dir>

Run from the scripts/ directory (or anywhere with scripts/ on
PYTHONPATH), e.g.:

    cd circt-abc/scripts
    python3 -m circt_flow ../examples/ibex/ibex_flow.yaml /tmp/out

The same environment variables the bash script honors
(CIRCT_ABC_OPT, CIRCT_OPT, ABC_BIN, ENABLE_CLASSICAL_RESUB,
SV2V_DOCKER_IMAGE) still work as overrides; explicit CLI flags below take
precedence over them.
"""
from __future__ import annotations

import argparse
import sys

from .config import FlowConfig
from .design.directives import DirectiveError, DirectiveSet
from .pipeline import run


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="circt_flow",
        description="CIRCT-native synthesis flow orchestration (Python port "
        "of run_circt_native_synth.sh).",
    )
    p.add_argument("config", help="Path to the flow.yaml config file.")
    p.add_argument("out_dir", help="Output directory for all pipeline artifacts.")
    p.add_argument(
        "--circt-abc-opt",
        default=None,
        help="Path to circt-abc-opt (default: env CIRCT_ABC_OPT, else "
        "<root>/mapper/build/bin/circt-abc-opt).",
    )
    p.add_argument(
        "--circt-opt",
        default=None,
        help="Path to circt-opt (default: env CIRCT_OPT, else "
        "<root>/third_party/circt/build/bin/circt-opt).",
    )
    p.add_argument(
        "--abc-bin",
        default=None,
        help="Path to the ABC binary (default: env ABC_BIN, else "
        "<root>/third_party/yosys/build/yosys-abc).",
    )
    resub = p.add_mutually_exclusive_group()
    resub.add_argument(
        "--enable-classical-resub",
        dest="enable_classical_resub",
        action="store_true",
        default=None,
        help="Enable abc-bridge's classical resub (default: env "
        "ENABLE_CLASSICAL_RESUB, else true).",
    )
    resub.add_argument(
        "--disable-classical-resub",
        dest="enable_classical_resub",
        action="store_false",
        default=None,
        help="Disable abc-bridge's classical resub.",
    )
    p.add_argument(
        "--sv2v-docker-image",
        default=None,
        help="Docker image providing sv2v for the OpenSTA-compat "
        "post-processing step (default: env SV2V_DOCKER_IMAGE, else "
        "circt-abc).",
    )
    p.add_argument(
        "--dont-touch",
        dest="dont_touch",
        action="append",
        default=None,
        metavar="REGISTER_NAME",
        help="Exclude a register (its bare seq.firreg name in "
        "${TOP}_core.mlir, e.g. fetch_addr_q) from "
        "--narrow-dead-register-bits. Repeatable. Default: none (the "
        "pipeline's behavior is unaffected unless this is given).",
    )
    p.add_argument(
        "--dont-gate",
        dest="dont_gate",
        action="append",
        default=None,
        metavar="REGISTER_NAME",
        help="Exclude a register (its bare seq.firreg name in "
        "${TOP}_core.mlir) from --clock-gate-inference, leaving it as an "
        "ordinary ungated hold-mux firreg. Repeatable. Default: none (the "
        "pipeline's behavior is unaffected unless this is given).",
    )
    p.add_argument(
        "--partitions",
        type=int,
        default=None,
        metavar="N",
        help="Split the residual logic into N groups and map each in its own "
        "ABC process (default 1 = one whole-design call). This CHANGES the "
        "netlist -- ABC optimises a smaller scope per group -- so use it for "
        "designs where a single ABC call is impractical, not for QoR.",
    )
    p.add_argument(
        "--abc-pool-size",
        type=int,
        default=None,
        metavar="P",
        help="Concurrent ABC processes when --partitions is used (default 8). "
        "Processes, never threads: ABC's frame is a process-wide singleton.",
    )
    p.add_argument(
        "--partition-workdir",
        default=None,
        metavar="DIR",
        help="Scratch directory for partitioned runs (default "
        "<out_dir>/partitions). A 64-way run on a large design needs tens of "
        "GB -- do not point this at /tmp.",
    )
    p.add_argument(
        "--abc-script-file",
        default=None,
        metavar="PATH",
        help="Custom ABC optimisation-body recipe (see recipes/).",
    )
    p.add_argument(
        "--abc-use-datapath",
        action="store_true",
        help="Lower multi-operand add/sub/mul through the Datapath dialect "
        "(carry-save compressors) before the AIG conversion.",
    )
    p.add_argument(
        "--abc-save-aig-dir",
        default=None,
        metavar="DIR",
        help="Save each ABC target's pre-mapping AIGER as <DIR>/<target>.aig.",
    )
    p.add_argument(
        "--abc-read-lib-args",
        default=None,
        metavar="ARGS",
        help="Extra arguments for ABC read_lib, e.g. '-G 4' for logical-effort "
        "delays instead of the unit-delay genlib (PHYS-ABC P5).",
    )
    p.add_argument(
        "--abc-save-mapped-dir",
        default=None,
        metavar="DIR",
        help="Save each ABC target's mapped Verilog as <DIR>/<target>.v "
        "(PHYS-ABC loop: coordinate extraction + standalone re-sizing).",
    )
    p.add_argument(
        "--abc-keep-buffers",
        action="store_true",
        help="Keep the buffers a PHYS-ABC harness pass inserted (disables "
        "remove-identity-buffers after the bridge).",
    )
    p.add_argument(
        "--abc-use-mapped-dir",
        default=None,
        metavar="DIR",
        help="Skip ABC and re-import <DIR>/<target>.v as each target's "
        "mapped netlist (PHYS-ABC loop return path).",
    )
    p.add_argument(
        "--abc-use-mapped-if-present",
        action="store_true",
        help="with --abc-use-mapped-dir: groups without a saved netlist are mapped fresh (partial reuse, PHYS-DP loop)",
    )
    p.add_argument(
        "--skip-unmappable",
        action="store_true",
        help="Skip residual modules AIGER cannot represent (leftover LLHD "
        "signals, aggregates) instead of failing. Produces an INCOMPLETE "
        "netlist; for taking a large design as far as it goes.",
    )
    p.add_argument(
        "--pnr",
        action="store_true",
        help="After synthesis, place and route the OpenSTA-compatible "
        "netlist on stock ORFS, configured by the flow.yaml's `pnr:` "
        "section (see circt_flow/orfs_pnr.py).",
    )
    return p


def _build_directives(args: argparse.Namespace) -> DirectiveSet:
    """Empty (default) unless --dont-touch/--dont-gate were given -- the
    pipeline's behavior with an empty DirectiveSet is byte-identical to
    having no directive support at all (see FlowConfig.directives'
    default_factory)."""
    directives = DirectiveSet()
    for name in args.dont_touch or []:
        directives.add_dont_touch(name)
    for name in args.dont_gate or []:
        directives.add_dont_gate(name)
    return directives


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        directives = _build_directives(args)
        cfg = FlowConfig.load(
            args.config,
            args.out_dir,
            circt_abc_opt=args.circt_abc_opt,
            circt_opt=args.circt_opt,
            abc_bin=args.abc_bin,
            enable_classical_resub=args.enable_classical_resub,
            sv2v_docker_image=args.sv2v_docker_image,
            directives=directives,
            partitions=args.partitions,
            abc_pool_size=args.abc_pool_size,
            partition_workdir=args.partition_workdir,
            abc_script_file=args.abc_script_file,
            abc_read_lib_args=args.abc_read_lib_args,
            abc_save_aig_dir=args.abc_save_aig_dir,
            abc_use_datapath=args.abc_use_datapath,
            abc_save_mapped_dir=args.abc_save_mapped_dir,
            abc_use_mapped_dir=args.abc_use_mapped_dir,
            abc_use_mapped_if_present=args.abc_use_mapped_if_present,
            abc_keep_buffers=args.abc_keep_buffers,
            skip_unmappable=args.skip_unmappable,
        )
        run(cfg, pnr=args.pnr)
    except (FileNotFoundError, DirectiveError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # subprocess.CalledProcessError etc.
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
