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
    3. the tech pack's `flow:` default, if the profile is a pack
    4. built-in default
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
    # tie_hi / tie_lo (CELL:PIN, replacing the pack's tie cells),
    # vt_swap (false: OpenROAD's repair_timing may not change Vt flavours),
    # dont_use (cell-name globs the resizer must not choose), and
    # `set:`, a mapping of extra ORFS config.mk variables.
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
    # A tech pack's `flow:` defaults (profile.FLOW_DEFAULTS), applied below
    # the environment and explicit arguments.
    flow_defaults: dict = field(default_factory=dict)
    # A tech pack's directory and its `pnr:` section (orfs_platform_dir or
    # command), for the P&R stage.
    pack_root: Optional[Path] = None
    pack_pnr: dict = field(default_factory=dict)
    # A pack that does not say `public: true`: its outputs are its data.
    pack_private: bool = False
    # pdk.rail: the profile's supply rail this run maps on (a schema-2
    # pack; default its default_rail), and that rail's voltage.
    rail: Optional[str] = None
    voltage: Optional[float] = None
    # design.core_mlir (relative to the flow.yaml): start from this core
    # MLIR instead of running the front end. The power flow uses it for
    # each domain's sub-run.
    core_mlir: Optional[Path] = None
    # The flow.yaml's `power:` section, validated (power.parse_power): a
    # tuple of power.Domain. None runs the ordinary single-domain flow.
    power: Optional[tuple] = None
    # The flow.yaml's `modes:` (modes.parse_modes): a tuple of modes.Mode,
    # each from its SDC file; None when the config gives only
    # clock_period_ps (the single-mode shorthand).
    modes: Optional[tuple] = None
    # `mapping_target:` (modes.TargetSpec). Set, the value ABC gets is
    # calibrated (modes.derive_target); None (no modes: and no
    # mapping_target:) leaves it clock_period_ps, as before.
    mapping_target: Optional[object] = None
    # True when mapping_target came from the pack's flow: default rather
    # than the flow config: each power domain then uses its own rail's.
    mapping_target_from_pack: bool = False
    # design.abc_delay_target_ps: the value ABC gets as clock-period-ps,
    # given outright (the power flow writes each domain's derived target
    # here). None: clock_period_ps, or the calibrated target.
    abc_delay_target_ps: Optional[int] = None
    # The design's clock port (pnr.clock_port, else the manifest's
    # constraints.clock_port): where the shorthand mode's clock is.
    clock_port: Optional[str] = None
    # The profile's time unit: SDC times are in it.
    time_unit: str = "ns"
    # The flow config's `sta:` section (sta.StaSpec); None without one.
    sta: Optional[object] = None
    # The flow config's `vt_swap:` section (vtswap.VtSwapSpec); None without
    # one.
    vt_swap: Optional[object] = None
    # Pre-placement fanout buffering (fanout.FanoutSpec): the pack's
    # flow.fanout_buffer merged with the flow config's fanout_buffer:;
    # None when neither sets one or the config turns it off.
    fanout: Optional[object] = None
    # The flow config's own fanout_buffer: (raw), applied over each rail's
    # pack-level spec by a power flow (fanout.rail_specs).
    fanout_override: object = None
    # The power: section's options besides domains (power.parse_options):
    # insert_level_shifters, ground.
    power_options: dict = field(default_factory=dict)

    @property
    def abc_target_ps(self) -> int:
        """What the abc-bridge gets as clock-period-ps ({period})."""
        return (self.abc_delay_target_ps if self.abc_delay_target_ps
                is not None else self.clock_period_ps)

    @property
    def calibrate(self) -> bool:
        return (self.mapping_target is not None
                and self.abc_delay_target_ps is None)

    def effective_modes(self) -> tuple:
        """The modes, or the clock_period_ps shorthand as one mode."""
        from . import modes as modes_mod
        if self.modes:
            return self.modes
        return (modes_mod.shorthand_mode(self.clock_period_ps,
                                         self.clock_port),)

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
        from . import profile as profile_mod, provenance
        manifest = cfg["design"].get("manifest")
        # A bare name is a design under this repo's designs/.
        if manifest and "/" not in manifest and not manifest.endswith(".yaml"):
            manifest = ROOT / "designs" / manifest / "manifest.yaml"
        manifest = rel(manifest)
        profile = cfg["pdk"].get("profile")
        profile = profile_mod.resolve(profile, here) if profile else None
        dont_use: List[str] = []
        tie: dict = {}
        flow_defaults: dict = {}
        pack_root, pack_pnr, pack_private = None, {}, False
        rail = cfg["pdk"].get("rail")
        voltage = None
        prof = None
        if profile is None and (rail is not None or "power" in cfg):
            raise ValueError(f"{config_path}: pdk.rail and power: need a "
                             "pdk.profile (a schema-2 tech pack)")
        if profile is not None:
            prof = profile_mod.load(profile)
            provenance.enforce(prof)
            if rail is not None:
                if rail not in prof.rails:
                    raise ValueError(
                        f"{config_path}: pdk.rail {rail!r} is not a rail of "
                        f"{prof.name}; rails: {', '.join(prof.rails)}")
                prof = prof.for_rail(str(rail))
            rail, voltage = prof.rail, prof.voltage
            libs = [str(p) for p in prof.synth_libs]
            dont_use = prof.dont_use
            tie = prof.tie
            flow_defaults = prof.flow
            pack_root, pack_pnr = prof.root, prof.pnr
            pack_private = prof.root is not None and not prof.public
        else:
            libs = list(cfg["pdk"]["synth_lib_tt"])
        power = None
        power_options: dict = {}
        if cfg.get("power") is not None:
            from . import power as power_mod
            power = power_mod.parse_power(cfg["power"], prof, config_path)
            power_options = power_mod.parse_options(cfg["power"],
                                                    config_path, prof)
        sta_spec = None
        if "sta" in cfg:
            from . import sta as sta_mod
            sta_spec = sta_mod.parse_sta(cfg["sta"], config_path)
        vt_swap_spec = None
        if "vt_swap" in cfg:
            from . import vtswap as vtswap_mod
            vt_swap_spec = vtswap_mod.parse_vt_swap(cfg["vt_swap"],
                                                    config_path)
        from . import fanout as fanout_mod
        fanout_spec = fanout_mod.parse_spec(
            flow_defaults.get("fanout_buffer"), cfg.get("fanout_buffer"),
            str(config_path))
        if power is not None and cfg.get("fanout_buffer") is not False:
            # Every rail's spec is checked here, not after synthesis.
            fanout_mod.rail_specs(prof, cfg.get("fanout_buffer"),
                                  str(config_path))
        core_mlir = cfg["design"].get("core_mlir")
        from . import modes as modes_mod
        time_unit = prof.time_unit if prof is not None else "ns"
        modes = None
        if cfg.get("modes") is not None:
            modes = modes_mod.parse_modes(cfg["modes"], here, config_path,
                                          time_unit)
            for m in modes:
                for w in m.warnings:
                    print(f"[circt_flow] WARNING: mode {m.name}: {w}")
        target = None
        target_from_pack = False
        if modes is not None or cfg.get("mapping_target") is not None:
            target = modes_mod.parse_target(cfg.get("mapping_target"),
                                            config_path)
        if cfg.get("mapping_target") is None and \
                flow_defaults.get("mapping_target") is not None:
            target = modes_mod.parse_target(flow_defaults["mapping_target"],
                                            f"{profile}: flow")
            target_from_pack = True
        period = cfg["design"].get("clock_period_ps")
        if period is None:
            if modes is None:
                raise KeyError(f"{config_path}: design.clock_period_ps is "
                               "required without modes:")
            # The tightest clock of any mode.
            period = min(c.period_ps for m in modes
                         for c in m.clocks.values() if c.ports)
        abc_target = cfg["design"].get("abc_delay_target_ps")
        clock_port = (cfg.get("pnr") or {}).get("clock_port")
        if clock_port is None and manifest is not None and manifest.is_file():
            clock_port = ((yaml.safe_load(manifest.read_text()) or {})
                          .get("constraints") or {}).get("clock_port")
        obj = cls(
            top_module=cfg["design"]["top_module"],
            clock_period_ps=int(round(float(period))),
            synth_lib_tt=libs,
            pnr=cfg.get("pnr"),
            manifest=manifest,
            profile=profile,
            dont_use=dont_use,
            tie=tie,
            flow_defaults=flow_defaults,
            pack_root=pack_root,
            pack_pnr=pack_pnr,
            pack_private=pack_private,
            rail=rail,
            voltage=voltage,
            core_mlir=rel(core_mlir),
            power=power,
            modes=modes,
            mapping_target=target,
            mapping_target_from_pack=target_from_pack,
            abc_delay_target_ps=(int(abc_target) if abc_target is not None
                                 else None),
            clock_port=clock_port,
            time_unit=time_unit,
            sta=sta_spec,
            vt_swap=vt_swap_spec,
            fanout=fanout_spec,
            fanout_override=cfg.get("fanout_buffer"),
            power_options=power_options,
        )
        if sta_spec is not None:
            # Names the run cannot honour fail here, not after synthesis.
            from . import sta as sta_mod
            sta_mod.check_names(sta_spec, sta_mod.corner_names(prof),
                                [m.name for m in obj.effective_modes()],
                                config_path)
        return obj


def _read_lib_args(base: Optional[str], dont_use) -> Optional[str]:
    """ABC's read_lib arguments: `base` (the command line's, else the
    pack's flow default) plus `-X <pattern>` for each of the profile's
    dont_use patterns it does not already carry. The patterns used to be
    dropped whenever read_lib arguments were set (e.g. "-l" for the
    load-aware genlib), which let ABC map onto dont_use cells."""
    base = (base or "").strip()
    have = base.split()
    extra = [f"-X {p}" for p in dont_use
             if not any(have[i] == "-X" and have[i + 1] == p
                        for i in range(len(have) - 1))]
    out = " ".join([base] + extra).strip()
    return out or None


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
    # The image's own name: the circt-abc image carries sv2v (and sets
    # SV2V, which wins over any image).
    sv2v_docker_image: str = "circt-abc"
    directives: DirectiveSet = field(default_factory=DirectiveSet)
    # --sta: run the STA stage (sta.py) even without an sta: section.
    run_sta: bool = False
    run_vt_swap: bool = False

    root: Path = field(default_factory=lambda: ROOT)
    # The explicit (command-line) arguments load() was given, so the power
    # flow can load each domain's sub-run with the same overrides.
    load_overrides: dict = field(default_factory=dict, compare=False,
                                 repr=False)

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
        run_sta: bool = False,
        run_vt_swap: bool = False,
        root: Path = ROOT,
    ) -> "FlowConfig":
        """Build a FlowConfig for `config_path`/`out_dir`.

        Each overridable field resolves as: explicit argument > env var
        (bash-compatible name) > built-in default -- matching
        run_circt_native_synth.sh's `${VAR:-default}` behavior exactly,
        with an additional (new) explicit-argument level on top.
        """
        overrides = {k: v for k, v in dict(
            circt_abc_opt=circt_abc_opt, circt_opt=circt_opt,
            abc_bin=abc_bin, enable_classical_resub=enable_classical_resub,
            sv2v_docker_image=sv2v_docker_image, directives=directives,
            partitions=partitions, abc_pool_size=abc_pool_size,
            partition_workdir=partition_workdir,
            abc_script_file=abc_script_file,
            abc_read_lib_args=abc_read_lib_args,
            abc_save_aig_dir=abc_save_aig_dir,
            abc_use_datapath=abc_use_datapath,
            abc_save_mapped_dir=abc_save_mapped_dir,
            abc_use_mapped_dir=abc_use_mapped_dir,
            abc_use_mapped_if_present=abc_use_mapped_if_present,
            abc_keep_buffers=abc_keep_buffers,
            skip_unmappable=skip_unmappable, run_sta=run_sta,
            run_vt_swap=run_vt_swap).items()
            if v is not None and v is not False}
        env = os.environ if env is None else env
        config_path = Path(config_path)
        out_dir = Path(out_dir)

        design = DesignConfig.from_yaml(config_path)
        pack = design.flow_defaults

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
            resolved_enable_classical_resub = pack.get(
                "enable_classical_resub", True)
        resolved_sv2v_docker_image = (
            sv2v_docker_image
            if sv2v_docker_image is not None
            else env.get("SV2V_DOCKER_IMAGE",
                         pack.get("sv2v_docker_image", "circt-abc"))
        )

        resolved_partitions = (
            partitions if partitions is not None
            else int(env.get("ABC_PARTITIONS", pack.get("partitions", 1)))
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
            abc_script_file=(Path(abc_script_file) if abc_script_file
                             else pack.get("abc_script_file")),
            abc_read_lib_args=_read_lib_args(
                abc_read_lib_args if abc_read_lib_args is not None
                else pack.get("abc_read_lib_args"), design.dont_use),
            abc_save_aig_dir=Path(abc_save_aig_dir) if abc_save_aig_dir else None,
            abc_use_datapath=bool(abc_use_datapath),
            abc_save_mapped_dir=Path(abc_save_mapped_dir) if abc_save_mapped_dir else None,
            abc_use_mapped_dir=Path(abc_use_mapped_dir) if abc_use_mapped_dir else None,
            abc_use_mapped_if_present=abc_use_mapped_if_present,
            abc_keep_buffers=bool(abc_keep_buffers),
            skip_unmappable=bool(skip_unmappable),
            run_sta=bool(run_sta),
            run_vt_swap=bool(run_vt_swap),
            root=root,
            load_overrides=overrides,
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
