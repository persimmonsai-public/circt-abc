"""Typed configuration for the CIRCT-native synthesis flow.

Mirrors exactly what scripts/run_circt_native_synth.sh reads:

  - from the flow.yaml:            design.top_module, design.clock_period_ps,
                                    pdk.synth_lib_tt (list)
  - from the environment (all with the SAME names/defaults the bash script
    uses today, honored here purely for backward compatibility with
    existing CI/scripts -- the intended way to override these going
    forward is real constructor/CLI arguments, not env vars):
        CIRCT_ABC_OPT         -> $ROOT/mapper/build/bin/circt-abc-opt
        CIRCT_OPT             -> $ROOT/third_party/circt/build/bin/circt-opt
        ABC_BIN               -> $ROOT/third_party/yosys/build/yosys-abc
        ENABLE_CLASSICAL_RESUB -> true
        SV2V_DOCKER_IMAGE     -> circt-abc

Precedence for each overridable field, highest first:
    1. explicit constructor/CLI argument
    2. environment variable (bash-compatible name)
    3. built-in default (matching the bash script's own default exactly)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional

import yaml

from .design.directives import DirectiveSet

# Repo root: this file lives at <ROOT>/scripts/circt_flow/config.py, so ROOT
# is three parents up -- exactly what the bash script computes via
# `ROOT=$(cd "$(dirname "$0")/.." && pwd)` for a script at <ROOT>/scripts/.
ROOT: Path = Path(__file__).resolve().parent.parent.parent


def _env_bool(raw: str) -> bool:
    """Parse a bash-style boolean env var the same way the shell script's
    consumer (circt-abc-opt's `enable-classical-resub=$ENABLE_CLASSICAL_RESUB`
    pass-option string) does: it is forwarded verbatim as the literal text
    'true'/'false', so the only strings that matter here are exactly those.
    """
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise ValueError(
        f"ENABLE_CLASSICAL_RESUB must be a boolean-like value, got {raw!r}"
    )


@dataclass(frozen=True)
class DesignConfig:
    """Everything read out of the flow.yaml's design/pdk sections."""

    top_module: str
    clock_period_ps: int
    synth_lib_tt: List[str]
    # The optional `pnr:` section, for the ORFS stage (`--pnr`): platform,
    # clock_port, and optionally clock_period (in the platform's time unit;
    # default clock_period_ps), clock_latency, utilization, place_density,
    # and `set:`, a mapping of extra ORFS config.mk variables.
    pnr: Optional[dict] = None
    # design.manifest / pdk.profile (paths relative to the flow.yaml): the
    # front end elaborates the manifest (circt_flow/frontend.py) instead of
    # running lower_ibex.sh, and the profile supplies the liberties.
    manifest: Optional[Path] = None
    profile: Optional[Path] = None
    # The profile's dont_use patterns; ABC reads the liberty without them.
    dont_use: List[str] = field(default_factory=list)
    # The profile's tie cells, {"hi": "CELL:PIN", "lo": "CELL:PIN"}.
    tie: dict = field(default_factory=dict)

    @property
    def lib(self) -> str:
        """The primary (first) liberty file -- what the bash script calls
        $LIB (`cfg['pdk']['synth_lib_tt'][0]`)."""
        return self.synth_lib_tt[0]

    @property
    def all_libs(self) -> str:
        """Comma-joined list of every liberty file -- what the bash script
        calls $ALL_LIBS."""
        return ",".join(self.synth_lib_tt)

    @classmethod
    def from_yaml(cls, config_path: Path) -> "DesignConfig":
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        here = Path(config_path).resolve().parent
        rel = lambda p: (here / p).resolve() if p else None
        manifest = rel(cfg["design"].get("manifest"))
        profile = rel(cfg["pdk"].get("profile"))
        dont_use: List[str] = []
        tie: dict = {}
        if profile is not None:
            from . import profile as profile_mod
            prof = profile_mod.load(profile)
            libs = [str(p) for p in prof.synth_libs]
            dont_use = prof.dont_use
            tie = prof.tie
        else:
            libs = list(cfg["pdk"]["synth_lib_tt"])
        return cls(
            top_module=cfg["design"]["top_module"],
            clock_period_ps=int(cfg["design"]["clock_period_ps"]),
            synth_lib_tt=libs,
            pnr=cfg.get("pnr"),
            manifest=manifest,
            profile=profile,
            dont_use=dont_use,
            tie=tie,
        )


@dataclass(frozen=True)
class FlowConfig:
    """Full, validated configuration for one run of the flow.

    Construct via `FlowConfig.load(config_path, out_dir, env=os.environ,
    **overrides)` to get bash-script-compatible env-var honoring with
    explicit-argument precedence, or call the constructor directly to
    bypass environment lookups entirely.
    """

    config_path: Path
    out_dir: Path
    design: DesignConfig

    circt_abc_opt: Path
    circt_opt: Path
    abc_bin: Path
    enable_classical_resub: bool = True
    # Partitioned abc-bridge. 1 = the shipped monolithic path. >1 splits the
    # residual logic into that many groups, each mapped in its own ABC
    # process. This CHANGES the netlist (ABC optimises a smaller scope per
    # group; on ibex that measured as a small area loss), so it is opt-in
    # rather than chosen automatically.
    partitions: int = 1
    abc_pool_size: int = 8
    # Scratch for partitioned runs. Defaults next to the output and
    # deliberately NOT under /tmp: a 64-way OpenC910 run needs tens of GB, and
    # /tmp was both too small and cleaned mid-run, deleting generated stubs
    # and failing 33 groups.
    partition_workdir: Optional[Path] = None
    abc_script_file: Optional[Path] = None
    # extra ABC read_lib arguments (PHYS-ABC P5: "-G <gain>")
    abc_read_lib_args: Optional[str] = None
    # save each ABC target's pre-mapping AIGER (PHYS-ABC P6 remap input)
    abc_save_aig_dir: Optional[Path] = None
    # lower multi-operand comb.add/sub/mul through the Datapath dialect
    # (carry-save compressors) before the AIG conversion (PHYS-QOR: FPU depth)
    abc_use_datapath: bool = False
    # PHYS-ABC loop hooks (abc-bridge save-mapped-dir / use-mapped-dir):
    # save each target's mapped Verilog to a stable path, or skip ABC and
    # re-import a (possibly re-sized) one. See the PHYS-ABC plan.
    abc_save_mapped_dir: Optional[Path] = None
    abc_use_mapped_dir: Optional[Path] = None
    abc_use_mapped_if_present: bool = False
    # Keep the buffers a PHYS-ABC harness pass inserted: disables
    # remove-identity-buffers after the bridge (the reuse path otherwise
    # strips every ABC `buffer`/`repeater` cell -- 74,829 of them on the
    # first remap arm -- before P&R ever sees them).
    abc_keep_buffers: bool = False
    skip_unmappable: bool = False
    sv2v_docker_image: str = "circt-abc"
    directives: DirectiveSet = field(default_factory=DirectiveSet)

    root: Path = field(default_factory=lambda: ROOT)

    @staticmethod
    def default_circt_abc_opt(root: Path = ROOT) -> Path:
        return root / "mapper" / "build" / "bin" / "circt-abc-opt"

    @staticmethod
    def default_circt_opt(root: Path = ROOT) -> Path:
        return root / "third_party" / "circt" / "build" / "bin" / "circt-opt"

    @staticmethod
    def default_abc_bin(root: Path = ROOT) -> Path:
        return root / "third_party" / "yosys" / "build" / "yosys-abc"

    @classmethod
    def load(
        cls,
        config_path: Path | str,
        out_dir: Path | str,
        *,
        env: Optional[Mapping[str, str]] = None,
        circt_abc_opt: Optional[Path | str] = None,
        circt_opt: Optional[Path | str] = None,
        abc_bin: Optional[Path | str] = None,
        enable_classical_resub: Optional[bool] = None,
        sv2v_docker_image: Optional[str] = None,
        directives: Optional[DirectiveSet] = None,
        partitions: Optional[int] = None,
        abc_pool_size: Optional[int] = None,
        partition_workdir: Optional[Path | str] = None,
        abc_script_file: Optional[Path | str] = None,
        abc_read_lib_args: Optional[str] = None,
        abc_save_aig_dir: Optional[Path | str] = None,
        abc_use_datapath: bool = False,
        abc_save_mapped_dir: Optional[Path | str] = None,
        abc_use_mapped_dir: Optional[Path | str] = None,
        abc_use_mapped_if_present: bool = False,
        abc_keep_buffers: bool = False,
        skip_unmappable: Optional[bool] = None,
        root: Path = ROOT,
    ) -> "FlowConfig":
        """Build a FlowConfig for `config_path`/`out_dir`.

        Each overridable field resolves as: explicit argument > env var
        (bash-compatible name) > built-in default -- matching
        run_circt_native_synth.sh's `${VAR:-default}` behavior exactly,
        with an additional (new) explicit-argument level on top.
        """
        env = os.environ if env is None else env
        config_path = Path(config_path)
        out_dir = Path(out_dir)

        design = DesignConfig.from_yaml(config_path)

        resolved_circt_abc_opt = Path(
            circt_abc_opt
            if circt_abc_opt is not None
            else env.get("CIRCT_ABC_OPT", str(cls.default_circt_abc_opt(root)))
        )
        resolved_circt_opt = Path(
            circt_opt
            if circt_opt is not None
            else env.get("CIRCT_OPT", str(cls.default_circt_opt(root)))
        )
        resolved_abc_bin = Path(
            abc_bin
            if abc_bin is not None
            else env.get("ABC_BIN", str(cls.default_abc_bin(root)))
        )
        if enable_classical_resub is not None:
            resolved_enable_classical_resub = enable_classical_resub
        elif "ENABLE_CLASSICAL_RESUB" in env:
            resolved_enable_classical_resub = _env_bool(env["ENABLE_CLASSICAL_RESUB"])
        else:
            resolved_enable_classical_resub = True
        resolved_sv2v_docker_image = (
            sv2v_docker_image
            if sv2v_docker_image is not None
            else env.get("SV2V_DOCKER_IMAGE", "circt-abc")
        )

        resolved_partitions = (
            partitions if partitions is not None
            else int(env.get("ABC_PARTITIONS", "1"))
        )
        resolved_pool = (
            abc_pool_size if abc_pool_size is not None
            else int(env.get("ABC_POOL_SIZE", "8"))
        )
        resolved_partition_workdir = (
            Path(partition_workdir) if partition_workdir
            else Path(out_dir) / "partitions"
        )

        return cls(
            config_path=config_path,
            out_dir=out_dir,
            design=design,
            circt_abc_opt=resolved_circt_abc_opt,
            circt_opt=resolved_circt_opt,
            abc_bin=resolved_abc_bin,
            enable_classical_resub=resolved_enable_classical_resub,
            sv2v_docker_image=resolved_sv2v_docker_image,
            directives=directives if directives is not None else DirectiveSet(),
            partitions=resolved_partitions,
            abc_pool_size=resolved_pool,
            partition_workdir=resolved_partition_workdir,
            abc_script_file=Path(abc_script_file) if abc_script_file else None,
            abc_read_lib_args=(
                abc_read_lib_args if abc_read_lib_args is not None
                else " ".join(f"-X {p}" for p in design.dont_use) or None),
            abc_save_aig_dir=Path(abc_save_aig_dir) if abc_save_aig_dir else None,
            abc_use_datapath=bool(abc_use_datapath),
            abc_save_mapped_dir=Path(abc_save_mapped_dir) if abc_save_mapped_dir else None,
            abc_use_mapped_dir=Path(abc_use_mapped_dir) if abc_use_mapped_dir else None,
            abc_use_mapped_if_present=abc_use_mapped_if_present,
            abc_keep_buffers=bool(abc_keep_buffers),
            skip_unmappable=bool(skip_unmappable),
            root=root,
        )

    def validate_tools(self) -> None:
        """Raise a clear FileNotFoundError for any missing/non-executable
        tool -- BEFORE attempting to run anything, matching (and improving
        on) the bash script's own `[[ -x "$tool" ]] || { echo ...; exit 1; }`
        checks."""
        for name, tool in (
            ("CIRCT_ABC_OPT", self.circt_abc_opt),
            ("CIRCT_OPT", self.circt_opt),
            ("ABC_BIN", self.abc_bin),
        ):
            if not os.access(tool, os.X_OK) or not tool.is_file():
                raise FileNotFoundError(
                    f"{name} ({tool}) not found/executable. "
                    f"Build it first (see scripts/build-circt-from-source.sh / "
                    f"scripts/build-mapper.sh), or pass an explicit override."
                )

    # ---- convenience paths matching the bash script's own variable names ----

    @property
    def core_mlir_raw(self) -> Path:
        return self.out_dir / f"{self.design.top_module}_core.mlir"

    @property
    def core_mlir_rmw(self) -> Path:
        """Stage-1 input after localize-dynamic-rmw (still aggregate-typed)."""
        return self.out_dir / f"{self.design.top_module}_core_rmw.mlir"

    @property
    def core_mlir(self) -> Path:
        return self.out_dir / f"{self.design.top_module}_core_deagg.mlir"

    @property
    def stubs(self) -> Path:
        return self.out_dir / "cell_stubs.sv"

    @property
    def struct_mlir(self) -> Path:
        return self.out_dir / f"{self.design.top_module}_struct.mlir"

    @property
    def flat_mlir(self) -> Path:
        return self.out_dir / f"{self.design.top_module}_flat.mlir"

    @property
    def mapped_mlir(self) -> Path:
        return self.out_dir / f"{self.design.top_module}_mapped.mlir"

    @property
    def synth_dir(self) -> Path:
        return self.out_dir / "1_synth"

    @property
    def netlist(self) -> Path:
        return self.synth_dir / f"{self.design.top_module}.v"

    @property
    def sv2v_out(self) -> Path:
        return self.synth_dir / f"{self.design.top_module}_sv2v.v"

    @property
    def openroad_netlist(self) -> Path:
        return self.synth_dir / f"{self.design.top_module}_openroad.v"

    @property
    def pnr_dir(self) -> Path:
        return self.out_dir / "pnr"

    @property
    def lower_ibex_sh(self) -> Path:
        return self.root / "scripts" / "lower_ibex.sh"

    @property
    def gen_stubs_py(self) -> Path:
        return self.root / "scripts" / "gen_liberty_blackbox_stubs.py"
