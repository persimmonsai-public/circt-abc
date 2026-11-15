"""The flow's option registry and resolved settings.

Every user-visible setting of the synthesis flow is declared here ONCE: its
dotted name, type, default, unit, legal range or choices, a help line, what it
binds to (a pass option of circt-abc-opt, a flow-stage parameter, a flow.yaml
key read by that section's own parser, a tech-pack `flow:` default, a P&R
setting) and the legacy environment variable it replaces.

Resolution (`resolve`), highest first:

    1. argument          (`--set name=value`, FlowConfig.load(settings=...))
    2. env               legacy CIRCT_FLOW_* / CIRCT_VT_* variable (transition
                         only: validated, warned once, recorded as "env";
                         an error under --strict)
    3. recipe            the flow.yaml: its `options:` mapping (dotted names)
                         and the keys of its sections (vt_swap:, shift_select:,
                         pnr.post_layout:, ...)
    4. techpack          the pack's `flow:` defaults (`flow.options:` dotted
                         names, `flow.env:` legacy names, `flow.<section>`)
    5. default           built in

The legacy variable sits above the recipe because that is what it did before
(the shift_select variables won over the flow.yaml keys, and a pack's flow.env
default applied only where the environment left a variable unset): keeping
that order is what keeps existing job scripts byte-identical.

Deprecation: the legacy variables are accepted, with a warning, in the
release that ships this module and in the next one, and refused from
LEGACY_ENV_REMOVED_IN on (see tests/test_options.py, which fails once the
repository's release number reaches it while the shim still exists).

Not options (left as they are, listed by `python3 -m circt_flow options
--non-options`): tool and build locations (CIRCT_ABC_OPT, CIRCT_OPT, ABC_BIN,
...), and the variables the flow itself exports to a pack's P&R command or a
ladder rung (INTERFACE_VARS).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

# ---------------------------------------------------------------- schedule

# The first public release with the registry is release 11. Legacy variables
# are accepted with a warning in releases 11 and 12 and refused from release
# 13 on.
FIRST_RELEASE_WITH_REGISTRY = 11
LEGACY_ENV_REMOVED_IN = 13
# The repository's current (last published) public release. The release
# procedure bumps it with every public release (doc/options.md, "Release
# procedure"); tests/test_options.py fails once it reaches
# LEGACY_ENV_REMOVED_IN while any option still has a legacy variable.
CURRENT_RELEASE = 11

LEGACY_PREFIXES = ("CIRCT_FLOW_", "CIRCT_VT_")


class OptionError(ValueError):
    """An unknown, mistyped or out-of-range setting."""


# ---------------------------------------------------------------- bindings

@dataclass(frozen=True)
class PassOption:
    """A pass option of circt-abc-opt (`--<pass>=<option>=...`)."""
    pass_name: str
    option: str

    def __str__(self):
        return f"pass {self.pass_name} {self.option}"


@dataclass(frozen=True)
class PassSwitch:
    """Whether a pass (or a pass instance) runs at all."""
    pass_name: str

    def __str__(self):
        return f"runs pass {self.pass_name}"


@dataclass(frozen=True)
class StageParam:
    """A parameter of a flow stage (stages.py, config.py, ...)."""
    stage: str
    what: str

    def __str__(self):
        return f"stage {self.stage}: {self.what}"


@dataclass(frozen=True)
class YamlKey:
    """A key of a flow.yaml section, read and validated by that section's own
    parser (the dotted name is set by overlaying the key)."""
    section: Tuple[str, ...]
    key: str

    def __str__(self):
        return "flow.yaml " + ".".join((*self.section, self.key))


@dataclass(frozen=True)
class PackKey:
    """A tech pack's `flow:` default (profile.FLOW_DEFAULTS)."""
    key: str

    def __str__(self):
        return f"pack flow.{self.key}"


@dataclass(frozen=True)
class PnrSetting:
    """A setting of the P&R recipe."""
    name: str

    def __str__(self):
        return f"P&R {self.name}"


# ---------------------------------------------------------------- options

@dataclass(frozen=True)
class Option:
    name: str
    type: str                       # bool int float str path choice object
    default: Any = None             # None: unset (the binding is not emitted)
    help: str = ""
    unit: str = ""
    range: Optional[Tuple[Optional[float], Optional[float]]] = None
    choices: Tuple[str, ...] = ()
    binds: Tuple[Any, ...] = ()
    legacy_env: Tuple[str, ...] = ()
    # how a legacy value maps: "value" (parsed by type), "on1" (bool: "1"
    # on, "0" off), "invert" (bool: "1" means False), "on1_or" (bool: "1"
    # on, "0" leaves the lower sources alone, as the shift_select variables
    # did against the flow.yaml keys)
    legacy_mode: str = "value"
    experimental: bool = False
    note: str = ""
    # False: recorded from the tech pack only (pack provenance); --set and a
    # flow.yaml options: entry are refused
    settable: bool = True
    # a yaml-bound option written into its flow.yaml section (default: the
    # untyped section keys); typed flow options are read from the settings
    overlay: Optional[bool] = None
    # options that must be set for this one to have an effect (documented,
    # and used by the test that every setting is applied)
    requires: Tuple[Tuple[str, Any], ...] = ()
    # a legacy variable set to "" is a value (passed on as given), not unset
    empty_is_value: bool = False
    # a regular expression the whole text of a str/path value must match
    # (pack options; flow options use _PATTERNS below)
    pattern: Optional[str] = None

    @property
    def overlaid(self) -> bool:
        return self.type == "object" if self.overlay is None else self.overlay

    def parse(self, raw, where: str):
        """`raw` (a recipe value, an argument string, or a legacy text) as this
        option's type; OptionError when it does not fit."""
        t = self.type
        if raw is None:
            return None
        if t == "object":
            return raw
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in (
                    "1", "true", "yes", "on"):
                return True
            if isinstance(raw, str) and raw.strip().lower() in (
                    "0", "false", "no", "off"):
                return False
            raise OptionError(f"{where}: {self.name} must be true or false, "
                              f"got {raw!r}")
        if t in ("int", "float"):
            if isinstance(raw, bool):
                raise OptionError(f"{where}: {self.name} must be "
                                  f"{'an integer' if t == 'int' else 'a number'}"
                                  f", got {raw!r}")
            try:
                if t == "int":
                    v = int(str(raw).strip())
                elif isinstance(raw, int) or (isinstance(raw, str) and
                                              re.fullmatch(r"\s*-?\d+\s*", raw)):
                    # an integral number keeps its written form (40, not
                    # 40.0): a P&R key is passed on as text
                    v = int(str(raw).strip())
                else:
                    v = float(raw)
            except (TypeError, ValueError):
                raise OptionError(f"{where}: {self.name} must be "
                                  f"{'an integer' if t == 'int' else 'a number'}"
                                  f", got {raw!r}") from None
            if t == "float" and v != v:
                raise OptionError(f"{where}: {self.name} is not a number")
            if self.range:
                lo, hi = self.range
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    raise OptionError(
                        f"{where}: {self.name}={v} is outside "
                        f"[{'' if lo is None else lo}, {'' if hi is None else hi}]"
                        + (f" {self.unit}" if self.unit else ""))
            return v
        if t == "choice":
            v = str(raw).strip() if not isinstance(raw, bool) else None
            if v not in self.choices:
                near = difflib.get_close_matches(str(raw), self.choices, 3, 0.5)
                raise OptionError(
                    f"{where}: {self.name}={raw!r} is not one of "
                    f"{', '.join(self.choices)}"
                    + (f" (did you mean {', '.join(near)}?)" if near else ""))
            return v
        if t in ("str", "path"):
            if isinstance(raw, (dict, list, bool)):
                raise OptionError(f"{where}: {self.name} must be a string, "
                                  f"got {raw!r}")
            v = str(raw)
            if self.choices and v not in self.choices:
                raise OptionError(f"{where}: {self.name}={v!r} is not one of "
                                  f"{', '.join(self.choices)}")
            if self.name in _PATTERNS and v and not _PATTERNS[self.name].match(v):
                raise OptionError(f"{where}: {self.name}={v!r} does not match "
                                  f"{_PATTERNS[self.name].pattern}")
            if self.pattern and not (v == "" and self.empty_is_value) \
                    and not re.fullmatch(self.pattern, v):
                raise OptionError(f"{where}: {self.name}={v!r} does not match "
                                  f"{self.pattern}")
            return v
        raise AssertionError(t)

    def legacy(self, text: str, var: str):
        """The value a legacy variable's text stands for; None when it leaves
        the lower sources alone (empty, or "0" of an on1_or flag)."""
        where = f"legacy variable {var}"
        if text == "":
            return "" if self.empty_is_value else None
        if self.type in ("int", "float", "bool", "choice") and text != text.strip():
            # the old code passed such a value on verbatim (an error or a
            # different meaning in the pass): refused rather than normalised
            raise OptionError(f"{where}={text!r}: leading or trailing blank "
                              f"(attribute {self.name})")
        if self.type == "int" and re.match(r"^-?0\d", text):
            raise OptionError(f"{where}={text!r}: leading zero (attribute "
                              f"{self.name})")
        if self.legacy_mode in ("on1", "invert", "on1_or"):
            if text not in ("0", "1"):
                raise OptionError(f"{where}={text!r}: must be 1 or 0 "
                                  f"(attribute {self.name})")
            on = text == "1"
            if self.legacy_mode == "invert":
                return not on
            if self.legacy_mode == "on1_or" and not on:
                return None
            return on
        return self.parse(text, where)

    def to_legacy(self, value) -> Optional[str]:
        """The legacy variable text for `value` (the inverse of legacy())."""
        if value is None:
            return None
        if self.legacy_mode == "invert":
            return "0" if value else "1"
        if self.type == "bool":
            return "1" if value else "0"
        if self.type == "float":
            return repr(float(value))
        return str(value)


_PATTERNS = {
    # [module/]reg@lo-hi[@zero|xor], comma separated (data-gate-enables)
    "syn.clock_gating.data_gate": re.compile(
        r"^\s*(?:[^,@/\s]+/)?[^,@/\s]+@\d+-\d+(?:@(?:zero|xor))?"
        r"(?:\s*,\s*(?:[^,@/\s]+/)?[^,@/\s]+@\d+-\d+(?:@(?:zero|xor))?)*\s*$"),
}

ARCHS = ("SKLANSKEY", "SKLANSKY", "KOGGE-STONE", "HAN-CARLSON", "BRENT-KUNG", "attr")

# The variables the flow exports to a pack's P&R command or a closure-ladder
# rung (and the pack's command reads); they are not options. A user may preset
# CIRCT_FLOW_WIRE_RC for a pack without wire_rc: (pack_command forwards the
# environment). Prefixes end with "_".
INTERFACE_VARS = {
    "CIRCT_FLOW_WIRE_RC": "setup corner's wire RC Tcl, exported to a pack's P&R command (wire_rc.pnr_env)",
    "CIRCT_FLOW_WIRE_RC_": "per-corner wire RC Tcl (CIRCT_FLOW_WIRE_RC_<CORNER>), exported",
    "CIRCT_FLOW_RUNG_DIR": "closure ladder: the rung's directory, exported to the rung's command",
    "CIRCT_FLOW_RUNG_INDEX": "closure ladder: the rung's index, exported",
    "CIRCT_FLOW_LADDER_PARAM": "closure ladder: the parameter, exported",
    "CIRCT_FLOW_LADDER_VALUE": "closure ladder: the rung's value (JSON), exported",
    "CIRCT_FLOW_LADDER_RUNG": "closure ladder: the rung's index (pack command), exported",
    "CIRCT_FLOW_OUT": "closure ladder: the flow's output directory, exported",
    "CIRCT_FLOW_PNR_METRICS": "closure ladder: the rung's pnr_metrics.json, exported",
    "CIRCT_FLOW_PACK_SETTINGS": "closure ladder: the rung's pack_settings.json (when pack options are handed over), exported to accept.command",
    "CIRCT_FLOW_START_AT": "closure ladder: stage a rung starts at (pack command), exported",
    "CIRCT_FLOW_START_FROM": "closure ladder: placement directory a rung restarts from, exported",
    "CIRCT_FLOW_STOP_AFTER": "closure ladder: stage the shared placement stops after, exported",
}

# Tool, build and run-location variables: not options, left as environment
# variables. Listed for the record.
NON_OPTION_ENV = {
    "CIRCT_ABC_OPT": "circt-abc-opt binary", "CIRCT_OPT": "circt-opt binary",
    "CIRCT_VERILOG": "circt-verilog binary", "ABC_BIN": "ABC binary",
    "SV2V": "sv2v binary", "SV2V_DOCKER_IMAGE": "sv2v image",
    "OPENSTA": "local OpenSTA binary", "ORFS_HOME": "local ORFS checkout",
    "ORFS_IMAGE": "ORFS image", "YOSYS": "yosys (tests)",
    "CIRCT_ABC_CACHE": "cache directory", "XDG_CACHE_HOME": "cache base",
    "CIRCT_ABC_TECH_PATH": "tech pack search path",
    "CIRCT_ABC_PROVENANCE": "liberty provenance check mode",
    "CIRCT_ABC_ALLOW_REPO_OUT": "allow a private pack's run inside the repo",
    "ENABLE_CLASSICAL_RESUB": "abc-bridge resub (config.py; has a CLI flag and a pack default)",
    "ABC_PARTITIONS": "--partitions default (config.py; has a CLI flag and a pack default)",
    "ABC_POOL_SIZE": "--abc-pool-size default (config.py)",
    "LIBPINS_LIBS": "design/libpins.py instrument",
    "PYTHONPATH": "", "HOME": "",
}


def _o(name, type, default=None, help="", **kw) -> Option:
    if "legacy_env" in kw and isinstance(kw["legacy_env"], str):
        kw["legacy_env"] = (kw["legacy_env"],)
    if "binds" in kw and not isinstance(kw["binds"], tuple):
        kw["binds"] = (kw["binds"],)
    return Option(name, type, default, help, **kw)


def _flag(name, var, pass_name, option, help, default=False, **kw):
    """A boolean pass option behind a legacy on/off variable."""
    return _o(name, "bool", default, help, legacy_env=var, legacy_mode="on1",
              binds=PassOption(pass_name, option) if option else PassSwitch(pass_name),
              **kw)


_E = "CIRCT_FLOW_"

OPTIONS: Tuple[Option, ...] = (
    # ---------------- syn.cleanup: design-level liveness and constants
    _flag("syn.cleanup.prune_any_module", _E + "PRUNE_ANY_MODULE",
          "prune-unused-residual-outputs", "any-module",
          "erase output ports no instance reads, iterated 3x (stage 3)"),
    _flag("syn.cleanup.prune_const_outputs", _E + "PRUNE_CONST_OUTPUTS",
          "prune-unused-residual-outputs", "propagate-constant-outputs",
          "also substitute instance results whose callee output is a literal (turns the prune loop on)"),
    _flag("syn.cleanup.prune_output_fields", _E + "PRUNE_OUTPUT_FIELDS",
          "prune-unused-residual-outputs", "prune-unused-output-fields",
          "split struct outputs into the fields some instance extracts (turns the prune loop on)"),
    _flag("syn.cleanup.prune_residual_outputs", _E + "PRUNE_RESIDUAL_OUTPUTS",
          "prune-unused-residual-outputs", None,
          "erase residual outputs the flattened design never reads (stage 4)", default=True),
    _flag("syn.cleanup.specialize_inputs", _E + "SPECIALIZE_INPUTS",
          "specialize-constant-module-inputs", "enable-specialize-constant-inputs",
          "module inputs every instance ties to the same literal become that literal (design-level)"),
    _flag("syn.cleanup.rcp_sequential", _E + "RCP_SEQUENTIAL",
          "propagate-register-constants", "mutual-fixpoint",
          "early propagate-register-constants plus fold-unknown-ops / mutual-fixpoint / absorbing-operands"),
    _flag("syn.cleanup.ncrb_multi_instance", _E + "NCRB_MULTI_INSTANCE",
          "narrow-constant-register-bits", "cross-multi-instance",
          "narrow-constant-register-bits: module inputs resolve to the meet over all instances"),
    _flag("syn.cleanup.ncrb_aggregates", _E + "NCRB_AGGREGATES",
          "narrow-constant-register-bits", "cross-aggregates",
          "narrow-constant-register-bits: evaluate struct/array values as packed known bits"),
    _flag("syn.cleanup.ncrb_unreset_holds", _E + "NCRB_UNRESET_HOLDS",
          "narrow-constant-register-bits", "fold-unreset-holds",
          "X-OPTIMISTIC: hypothesise reset-less registers as zero"),
    _flag("syn.cleanup.dead_output_bits", _E + "DEAD_OUTPUT_BITS",
          "narrow-dead-output-bits", None,
          "zero output-port bits no instance reads (design-level); with it precise-bitcast", default=True),
    _flag("syn.cleanup.dedup_registers", _E + "DEDUP_REGISTERS",
          "dedup-registers", "enable-register-dedup",
          "merge register replicas (costs timing)"),
    _flag("syn.cleanup.narrow_ext_mul", _E + "NARROW_EXT_MUL",
          "narrow-extended-multiplies", "enable-narrow-extended-multiplies",
          "sign-extended multiply -> zero-extended multiply plus correction"),
    _flag("syn.cleanup.narrow_sparse_shift", _E + "NARROW_SPARSE_SHIFT",
          "narrow-sparse-shifts", "enable-narrow-sparse-shifts",
          "shifts whose amount has constant bits"),
    _flag("syn.cleanup.narrow_whole_value_reads", _E + "NARROW_WHOLE_VALUE_READS",
          "narrow-whole-value-reads", None,
          "break word-level false cycles before outlining", default=True),
    _flag("syn.cleanup.localize_rmw", _E + "LOCALIZE_RMW",
          "localize-dynamic-rmw", None,
          "per-entry lowering of dynamic-index read-modify-writes (stage 1)", default=True),
    _flag("syn.cleanup.specialize_mux_arms", _E + "SPECIALIZE_MUX_ARMS",
          "specialize-mux-arms", "enable",
          "rewrite mux arms under the value their select must have (before merging)"),
    _flag("syn.cleanup.narrow_mux", _E + "NARROW_MUX",
          "narrow-mux-to-differing-bits", "enable-narrow-mux",
          "cut a wide mux to the bits where its arms differ (before the bridge)"),
    # ---------------- syn.mux
    _flag("syn.mux.priority_decode", _E + "PRIORITY_DECODE",
          "decode-mux-chains", "priority-decode",
          "decode non-exclusive mux chains with priority-encoded selects"),
    _o("syn.mux.priority_min_width", "int", 2,
       "with priority_decode: only data chains at least this wide", unit="bits",
       range=(1, None), legacy_env=_E + "PRIORITY_MIN_WIDTH",
       binds=PassOption("decode-mux-chains", "priority-min-width")),
    _flag("syn.mux.select_prefix_or", _E + "SELECT_PREFIX_OR",
          "select-chain-prefix-or", "enable",
          "collapse pass-through select chains into prefix ORs (turns internalize-self-fed-ports on)"),
    # ---------------- syn.clock_gating
    _o("syn.clock_gating.enable", "bool", True,
       "clock-gate-inference gates registers (false: every register on the plain clock)",
       legacy_env=_E + "NO_CLOCK_GATING", legacy_mode="invert",
       binds=PassOption("clock-gate-inference", "enable-clock-gating")),
    _flag("syn.clock_gating.bit_level", _E + "CG_BITLEVEL",
          "clock-gate-inference", "bit-level-enables",
          "resolve enables per bit and split registers into same-enable parts", default=True),
    _flag("syn.clock_gating.select_dont_care", _E + "CG_SELECT_DC",
          "clock-gate-inference", "select-dont-care",
          "rewrite each gated part's load-mux selects under its own enable"),
    _o("syn.clock_gating.data_gate", "str", None,
       "data-following clock gating of named register slices: [module/]reg@lo-hi[@zero|xor],...",
       legacy_env=_E + "DATA_GATE", binds=PassOption("data-gate-enables", "slices"),
       experimental=True,
       note="no sequential equivalence proof (combinationally exact; the ICG makes it a clock-gating equivalence)"),
    # ---------------- syn.multipliers (map-compressor-cells)
    _flag("syn.multipliers.cells", _E + "MUL_CELLS",
          "map-compressor-cells", "enable-compressor-cells",
          "lower multiply compressor trees to two-output adder cells"),
    _o("syn.multipliers.min_width", "int", None, "multiplier width gate (pass default 16)",
       unit="bits", range=(1, None), legacy_env=_E + "MUL_CELLS_MIN_WIDTH",
       binds=PassOption("map-compressor-cells", "min-width")),
    _flag("syn.multipliers.merge_adds", _E + "MUL_CELLS_MERGE",
          "map-compressor-cells", "merge-adds", "fold add(x, product) into one compress tree"),
    _flag("syn.multipliers.flatten_adds", _E + "MUL_CELLS_FLATTEN",
          "map-compressor-cells", "flatten-adds", "flatten nested adds into one compress plus one add"),
    _flag("syn.multipliers.flatten_gated", _E + "MUL_CELLS_FLATTEN_GATED",
          "map-compressor-cells", "flatten-gated",
          "with flatten_adds: also flatten mux(c, sum, 0) gating", default=True),
    _flag("syn.multipliers.prefix_final_adder", _E + "MUL_CELLS_PREFIX",
          "map-compressor-cells", "prefix-final-adder", "final two-row add as a Brent-Kung prefix network of cells"),
    _flag("syn.multipliers.gate_compressors", _E + "MUL_CELLS_GATES",
          "map-compressor-cells", "gate-compressors", "every full/half adder as plain logic for ABC"),
    _o("syn.multipliers.cell_stages", "int", None, "hard adder cells only in the first N reduction stages",
       range=(0, None), legacy_env=_E + "MUL_CELLS_CELL_STAGES",
       binds=PassOption("map-compressor-cells", "cell-stages")),
    _o("syn.multipliers.tree", "choice", None, "reduction tree shape (pass default dadda)",
       choices=("dadda", "wallace", "array"), legacy_env=_E + "MUL_CELLS_TREE",
       binds=PassOption("map-compressor-cells", "tree")),
    _flag("syn.multipliers.nand_pp", _E + "MUL_CELLS_NANDPP",
          "map-compressor-cells", "nand-pp",
          "NAND partial products, complement carried through the self-dual cells"),
    _flag("syn.multipliers.map_final_adder", _E + "MUL_CELLS_MAPFINAL",
          "map-compressor-cells", "map-final-adder",
          "leave each tree's final add to ABC (map-adder-chains skips it)"),
    _o("syn.multipliers.map_final_min_width", "int", None,
       "with map_final_adder: final adds narrower than N stay adder-cell chains",
       unit="bits", range=(0, None), legacy_env=_E + "MUL_CELLS_MAPFINAL_MINW",
       binds=PassOption("map-compressor-cells", "map-final-min-width")),
    # ---------------- syn.adders
    _o("syn.adders.arch", "choice", "BRENT-KUNG",
       "architecture CombToSynth uses for comb.add/icmp (none: its width heuristic)",
       choices=("RIPPLE-CARRY", "SKLANSKEY", "BRENT-KUNG", "KOGGE-STONE", "none", ""),
       legacy_env=_E + "ADDER_ARCH", binds=PassOption("set-adder-arch", "arch"),
       empty_is_value=True,
       note='"" runs set-adder-arch with an empty arch (it stamps nothing), as a set but empty '
            'CIRCT_FLOW_ADDER_ARCH did'),
    _flag("syn.adders.carry_in", _E + "ADD_CARRY_IN", "lower-carry-in-adds", "enable",
          "a + b + zext(c) as one {a,1} + {b,c} add"),
    _o("syn.adders.cells.min_width", "int", None,
       "map-adder-chains: adds at or above this width become a ripple of adder cells (unset: off)",
       unit="bits", range=(1, None), legacy_env=_E + "ADDER_CELLS",
       binds=PassOption("map-adder-chains", "min-width")),
    _o("syn.adders.cells.max_width", "int", None, "map-adder-chains: only adds at or below this width",
       unit="bits", range=(0, None), legacy_env=_E + "ADDER_CELLS_MAX",
       binds=PassOption("map-adder-chains", "max-width")),
    _flag("syn.adders.cells.increments", _E + "ADDER_CELLS_INCREMENTS",
          "map-adder-chains", "increments", "x + 1 as an inverter + half-adder ripple", default=True),
    _o("syn.adders.cells2.min_width", "int", None,
       "second map-adder-chains instance: minimum width (unset: off)", unit="bits",
       range=(1, None), legacy_env=_E + "ADDCH2_MIN",
       binds=PassOption("map-adder-chains", "min-width")),
    _o("syn.adders.cells2.max_width", "int", 0,
       "second map-adder-chains instance: maximum width (0: none)", unit="bits",
       range=(0, None), legacy_env=_E + "ADDCH2_MAX",
       binds=PassOption("map-adder-chains", "max-width")),
    _o("syn.adders.prefix.min_width", "int", None,
       "map-prefix-adders: adds at or above this width become a prefix network of cells (unset: off)",
       unit="bits", range=(1, None), legacy_env=_E + "PREFIX_ADDERS",
       binds=PassOption("map-prefix-adders", "min-width")),
    _o("syn.adders.prefix.max_width", "int", 0, "map-prefix-adders: maximum width (0: none)",
       unit="bits", range=(0, None), legacy_env=_E + "PREFIX_MAX_WIDTH",
       binds=PassOption("map-prefix-adders", "max-width")),
    _o("syn.adders.prefix.arch", "choice", "SKLANSKEY", "map-prefix-adders architecture",
       choices=ARCHS, legacy_env=_E + "PREFIX_ARCH",
       binds=PassOption("map-prefix-adders", "arch")),
    _o("syn.adders.prefix2.min_width", "int", None,
       "second map-prefix-adders instance: minimum width (unset: off)", unit="bits",
       range=(1, None), legacy_env=_E + "PREFIX2_ADDERS",
       binds=PassOption("map-prefix-adders", "min-width")),
    _o("syn.adders.prefix2.max_width", "int", 0,
       "second map-prefix-adders instance: maximum width (0: none)", unit="bits",
       range=(0, None), legacy_env=_E + "PREFIX2_MAX_WIDTH",
       binds=PassOption("map-prefix-adders", "max-width")),
    _o("syn.adders.prefix2.arch", "choice", "SKLANSKEY",
       "second map-prefix-adders instance: architecture", choices=ARCHS,
       legacy_env=_E + "PREFIX2_ARCH", binds=PassOption("map-prefix-adders", "arch")),
    _flag("syn.adders.prefix2.isolate_inputs", _E + "PREFIX2_ISOLATE",
          "map-prefix-adders", "isolate-inputs",
          "second instance: first level one NAND2 + one NOR2 per operand bit"),
    _o("syn.adders.prefix2.boundary", "int", None,
       "second instance: 1 = NAND2/NOR2 g/t boundary on both operands, ABC maps the rest; 0 off",
       range=(0, 1), legacy_env=_E + "PREFIX2_BOUNDARY",
       binds=PassOption("map-prefix-adders", "boundary")),
    # ---------------- syn.shifters
    _flag("syn.shifters.barrel", _E + "BARREL_SHIFT", "lower-barrel-shifts", "enable",
          "variable shifts as log2(width) stages of 2:1 muxes"),
    _o("syn.shifters.early", "bool", False,
       "map-shift-select in the early position (before clock gating); flow.yaml shift_select: (early: true)",
       legacy_env=_E + "SHIFT_SELECT_EARLY", legacy_mode="on1_or",
       binds=(PassSwitch("map-shift-select"), YamlKey(("shift_select",), "early"))),
    _o("syn.shifters.late", "bool", False,
       "map-shift-select in the legacy position (after decode-mux-chains); flow.yaml shift_select: {early: false}",
       legacy_env=_E + "SHIFT_SELECT", legacy_mode="on1_or",
       binds=(PassSwitch("map-shift-select"), YamlKey(("shift_select",), "early"))),
    _o("syn.shifters.min_arms", "int", 12, "fewest arms of a shifter the pass takes", range=(1, None),
       legacy_env=_E + "SHIFT_SELECT_MIN_ARMS",
       binds=(PassOption("map-shift-select", "min-arms"), YamlKey(("shift_select",), "min_arms"))),
    _o("syn.shifters.radix", "int", 4, "select radix", range=(1, None),
       legacy_env=_E + "SHIFT_SELECT_RADIX",
       binds=(PassOption("map-shift-select", "radix"), YamlKey(("shift_select",), "radix"))),
    _o("syn.shifters.wide", "choice", "0", "wide cells: 0 none, 1 wide-cells, 2 wide4-cells",
       choices=("0", "1", "2"), legacy_env=_E + "SHIFT_SELECT_WIDE",
       binds=(PassOption("map-shift-select", "wide4-cells"), YamlKey(("shift_select",), "wide"))),
    _o("syn.shifters.recipes", "str", "", "digit recipes (';'-separated list)",
       legacy_env=_E + "SHIFT_SELECT_RECIPES",
       binds=(PassOption("map-shift-select", "recipes"), YamlKey(("shift_select",), "recipes"))),
    _o("syn.shifters.direct_decode", "bool", False, "digit selects from the raw amount bits",
       legacy_env=_E + "SHIFT_SELECT_DIRECT", legacy_mode="on1_or",
       binds=(PassOption("map-shift-select", "direct-decode"),
              YamlKey(("shift_select",), "direct_decode"))),
    _o("syn.shifters.default_dont_care", "bool", False,
       "NOT EXACT, per-design assumption: unmatched amounts are don't cares",
       legacy_env=_E + "SHIFT_SELECT_DONTCARE", legacy_mode="on1_or",
       binds=(PassOption("map-shift-select", "default-dont-care"),
              YamlKey(("shift_select",), "default_dont_care"))),
    _o("syn.shifters.select_max_fanout", "int", None, "digit-select nets of up to N loads stay unbuffered",
       range=(1, None), legacy_env=_E + "SHIFT_SELECT_MAXFO",
       binds=(PassOption("map-shift-select", "select-max-fanout"),
              YamlKey(("shift_select",), "select_max_fanout"))),
    _o("syn.shifters.l2_range_gate", "int", None,
       "level-2 nodes selected by digit AND coarse range of the last digit (N values per range)",
       range=(1, None), legacy_env=_E + "SHIFT_SELECT_L2GATE",
       binds=(PassOption("map-shift-select", "l2-range-gate"),
              YamlKey(("shift_select",), "l2_range_gate"))),
    _o("syn.shifters.l2_range_aligned", "bool", False, "ranges are aligned blocks (shared range selects)",
       legacy_env=_E + "SHIFT_SELECT_L2ALIGNED", legacy_mode="on1_or",
       binds=(PassOption("map-shift-select", "l2-range-aligned"),
              YamlKey(("shift_select",), "l2_range_aligned"))),
    _o("syn.shifters.l2_range_compose", "bool", False, "gated select = digit select AND range select, shared",
       legacy_env=_E + "SHIFT_SELECT_L2COMPOSE", legacy_mode="on1_or",
       binds=(PassOption("map-shift-select", "l2-range-compose"),
              YamlKey(("shift_select",), "l2_range_compose"))),
    _o("syn.shifters.select_dup_fanout", "int", None, "at most N loads per select gate; copies instead of buffers",
       range=(1, None), legacy_env=_E + "SHIFT_SELECT_DUPFO",
       binds=(PassOption("map-shift-select", "select-dup-fanout"),
              YamlKey(("shift_select",), "select_dup_fanout"))),
    _o("syn.shifters.form", "choice", "recipe",
       "network form: recipe (the mixed-radix network) or window (level before the last copied "
       "per window of shift amounts, gated by digit AND window; levels alternate polarity)",
       choices=("recipe", "window"),
       binds=(PassOption("map-shift-select", "window-form"), YamlKey(("shift_select",), "form"))),
    _o("syn.shifters.window_count", "int", None, "window form: number of windows (unset: chosen by cost)",
       range=(1, 12),
       binds=(PassOption("map-shift-select", "window-count"),
              YamlKey(("shift_select",), "window_count"))),
    _o("syn.shifters.window_copy_cost", "int", None,
       "window form: cost of a node copy in percent of one always-active load (pass default 150)",
       range=(0, 10000),
       binds=(PassOption("map-shift-select", "window-copy-cost"),
              YamlKey(("shift_select",), "window_copy_cost"))),
    _o("syn.shifters.window_clip", "bool", False,
       "window form: a copy's select covers only the amounts of its window that use the node",
       binds=(PassOption("map-shift-select", "window-clip"),
              YamlKey(("shift_select",), "window_clip"))),
    _o("syn.shifters.window_per_node", "bool", False,
       "window form: a node takes one copy over the whole amount range when its own cost prefers it",
       binds=(PassOption("map-shift-select", "window-per-node"),
              YamlKey(("shift_select",), "window_per_node"))),
    _o("syn.shifters.window_cuts", "str", None,
       "window form: explicit cuts of the amount range (comma-separated, as the window report prints them)",
       binds=(PassOption("map-shift-select", "window-cuts"),
              YamlKey(("shift_select",), "window_cuts"))),
    _o("syn.shifters.window_general", "bool", False,
       "window form: overlapping windows with their own digit phase, greedy cover of (output, block) needs",
       binds=(PassOption("map-shift-select", "window-general"),
              YamlKey(("shift_select",), "window_general"))),
    _o("syn.shifters.window_types", "str", None,
       "window form, general: explicit window types 'phase:first-last;...' in blocks",
       binds=(PassOption("map-shift-select", "window-types"),
              YamlKey(("shift_select",), "window_types"))),
    _o("syn.shifters.window_types_max", "int", None, "window form, general: most window types the search combines",
       range=(1, 3), binds=(PassOption("map-shift-select", "window-types-max"),
                            YamlKey(("shift_select",), "window_types_max"))),
    _o("syn.shifters.window_direct", "int", None,
       "window form, general: cost (percent) of taking a lower node directly under a block select; unset = off",
       range=(1, 10000), binds=(PassOption("map-shift-select", "window-direct"),
                                YamlKey(("shift_select",), "window_direct"))),
    # ---------------- syn.datapath (PHYS-DP)
    _o("syn.datapath.overrides", "path", None, "per-op architecture overrides (dp-annotate ids)",
       legacy_env=_E + "DP_OVERRIDES", binds=PassOption("dp-apply-arch", "json")),
    _o("syn.datapath.annotate_json", "path", None, "sidecar JSON of wide arithmetic ops and their anchors",
       legacy_env=_E + "DP_JSON", binds=PassOption("dp-annotate", "json")),
    _flag("syn.datapath.outline", _E + "OUTLINE_DATAPATH", "outline-datapath-ops", None,
          "every wide arithmetic op becomes its own residual"),
    _o("syn.datapath.outline_ids", "path", None, "with outline: only these dp-annotate ids",
       legacy_env=_E + "DP_IDS", binds=PassOption("outline-datapath-ops", "ids")),
    # ---------------- syn.outline / abc / partition / fanout / sweep / library
    _o("syn.outline_after_flatten", "bool", False,
       "outline residual logic after flattening (one residual, ABC sees no module boundaries)",
       legacy_env=_E + "OUTLINE_AFTER_FLATTEN", legacy_mode="on1",
       binds=StageParam("structural_mapping/flatten_shell/export_netlist", "outline-residual-logic position")),
    _o("syn.abc.nf_relax", "int", None, "&nf -t -R <n> per-output required time (+n%)",
       unit="%", range=(-1, None), legacy_env=_E + "NF_RELAX",
       binds=PassOption("abc-bridge", "nf-relax-per-output")),
    _o("syn.abc.deepsyn_no_improve", "int", None, "&deepsyn -J (pass default 3)", range=(1, None),
       legacy_env=_E + "DEEPSYN_J", binds=PassOption("abc-bridge", "deepsyn-no-improve")),
    _o("syn.abc.deepsyn_time_sec", "int", None, "&deepsyn -T wall-clock limit per chain (machine-load dependent)",
       unit="s", range=(0, None), legacy_env=_E + "DEEPSYN_SEC",
       binds=PassOption("abc-bridge", "deepsyn-time-sec")),
    _flag("syn.abc.internalize_self_fed", _E + "INTERNALIZE_SELF_FED", "merge-residual-modules",
          "internalize-self-fed-ports", "internalise merged-instance inputs driven by the same instance"),
    _flag("syn.abc.free_dead_outputs", _E + "FREE_DEAD_OUTPUTS", "merge-residual-modules",
          "free-dead-output-ports", "constant-drive merged-instance outputs with no consumer"),
    _o("syn.abc.partition.group_map", "path", None, "JSON {module: group}: pin the grouping",
       legacy_env=_E + "GROUP_MAP", binds=PassOption("merge-residual-modules", "group-map")),
    _o("syn.abc.partition.group_by_depth", "bool", False, "group residual modules by AIG depth (depth_groups.py)",
       legacy_env=_E + "GROUP_BY_DEPTH", legacy_mode="on1",
       binds=StageParam("merge_and_map (partitioned)", "depth_groups.py group map")),
    _o("syn.abc.partition.isolate_substr", "str", None, "modules containing this go to the last group alone",
       legacy_env=_E + "ISOLATE_SUBSTR", binds=PassOption("merge-residual-modules", "isolate-substr")),
    _flag("syn.abc.partition.isolate_each", _E + "ISOLATE_EACH", "merge-residual-modules", "isolate-each",
          "with isolate_substr: one group per isolated module"),
    _o("syn.abc.partition.group_scripts", "path", None, "JSON {group: abc script}: per-group ABC body",
       legacy_env=_E + "GROUP_SCRIPTS", binds=PassOption("abc-bridge", "abc-script-file")),
    _o("syn.fanout.max", "int", None, "limit-fanout: balanced buffer trees above N loads (unset: off)",
       range=(1, None), legacy_env=_E + "MAX_FANOUT", binds=PassOption("limit-fanout", "max-fanout")),
    _o("syn.post_map_sweep.enable", "bool", False, "bit-level dead-logic sweep of the mapped netlist",
       legacy_env=_E + "POST_MAP_SWEEP", legacy_mode="on1",
       binds=StageParam("export_netlist", "post_map_sweep.py")),
    _o("syn.post_map_sweep.stubs", "str", "", "comma list of blackbox stub files for the sweep's read",
       legacy_env=_E + "SWEEP_STUBS", binds=StageParam("post_map_sweep", "--stubs")),
    _o("syn.library.slow_cell_ratio", "float", None,
       "slow-cell filter: cells slower than ratio x median leave ABC's library (overrides the pack's ratio)",
       range=(0, None), legacy_env=_E + "SLOW_CELL_RATIO",
       binds=(StageParam("config", "abc read_lib -X"), PackKey("slow_cell_filter"))),
    # ---------------- syn.vt_swap / post: flop cap (recipe key + legacy variable)
    _o("syn.vt_swap.flop_cap", "str", "", "ladder token above which the synthesis swap never moves a flop",
       legacy_env="CIRCT_VT_FLOP_CAP", binds=YamlKey(("vt_swap",), "flop_cap"),
       note="a flop is a cell whose name contains DFF (doc/passes.md)"),
    _o("post.flop_cap", "str", "", "ladder token above which the post-layout swap never moves a flop",
       legacy_env="CIRCT_VT_FLOP_CAP", binds=YamlKey(("pnr", "post_layout"), "flop_cap")),
    # ---------------- syn.scoped: commands on selected objects (scoping.py)
    _o("syn.scoped", "object", None,
       "scoped settings: a list of {command, targets, values} (gate_register on register bit ranges "
       "[module/]reg@lo-hi, EXPERIMENTAL; map_adders on adder width windows width:lo-hi, form prefix "
       "or cells); built by s.gate_register / s.map_adders (query.py), checked against the design "
       "(a target that matches nothing is refused)",
       binds=StageParam("structural_mapping", "data-gate-enables / map-adder-chains / "
                                              "map-prefix-adders instances (scoping.py)"),
       overlay=False),
    # ---------------- run.* (mechanics)
    _o("run.ladder.submit", "str", None, "closure ladder: job submit template ({cmd} or {cmdq})",
       legacy_env=_E + "LADDER_SUBMIT", binds=YamlKey(("pnr", "ladder"), "submit"),
       overlay=True),
    _o("run.ladder.cancel", "str", None, "closure ladder: job cancel template ({name})",
       legacy_env=_E + "LADDER_CANCEL", binds=YamlKey(("pnr", "ladder"), "cancel"),
       overlay=True),
)


# ---------------------------------------------------------------- yaml keys
# The flow.yaml sections and pack flow: keys, declared from the sections' own
# key lists. Their values are validated by the section parsers (type
# "object" here); the registry names them, records them in settings.json and
# lets `--set` / `options:` reach them by overlaying the key.

_VT_HELP = {
    "enabled": "run the synthesis Vt swap", "false_from": "ports whose paths the swap ignores",
    "paths": "paths per iteration", "max_iter": "fix iterations", "recover": "recover leakage",
    "margin_ps": "setup slack to keep (ps)", "keep_clock_cells": "leave clock gates at their flavour",
    "false_to": "endpoints the swap ignores", "fix": "sens | bulk",
    "sens_frac": "sensitivity fix fraction", "recover_order": "inst | leak",
    "ladder_cap": "ladder token the fix stops at",
    "allow_footprint_change": "allow ladder moves that change area",
    "ladder_escalate": "closing pass lifts the cap to this token",
    "close_margin_ps": "closing pass target (ps)"}

_POST_HELP = {
    "recover_block": "off unless set; '<regex>@<ps>': after the pair recovery, cells whose instance name matches go down to "
                     "their own slack floor (ps); other cells untouched (CLI --recover-block; the "
                     "post.recover.opts token blk= is the same setting)"}
# The `pnr:` keys of the P&R recipe (pnr_recipe.py, stages.py) besides the
# ORFS stage's own (ladder._ORFS_PNR_KEYS).
_PNR_RECIPE_KEYS = (
    "clock_port", "platform", "utilization", "tie_hi", "tie_lo", "sdc_file",
    "input_delay_min", "place_density", "cts_args", "cts_skip_fanout_le",
    "cts_size_gates", "cts_gate_port_tol", "cts_flatten_root", "cts_cluster_size",
    "cts_cluster_diameter", "repair_dont_use", "hold_corner", "hold_margin",
    "setup_margin", "spef_restore_pincap", "placement_check", "post_layout")


# Section keys whose section parser is lax: typed here, so a bad value is an
# error before the section sees it (the other section keys are checked by
# their section's parser; the remaining pnr: keys by the P&R recipe).
_YAML_TYPES = {
    "syn.fanout_buffer.enabled": ("bool",), "syn.fanout_buffer.bundle_pins": ("bool",),
    "syn.fanout_buffer.cell": ("str",), "syn.fanout_buffer.input": ("str",),
    "syn.fanout_buffer.output": ("str",), "syn.fanout_buffer.max_fanout": ("int", (1, None)),
    "syn.sta.image": ("str",), "syn.vt_swap.ladder_cap": ("str",),
    "syn.vt_swap.ladder_escalate": ("str",),
    "pnr.utilization": ("float", (0, 100)), "pnr.place_density": ("float", (0, 1)),
    "pnr.clock_period": ("float", (0, None)), "pnr.clock_latency": ("float", (None, None)),
    "pnr.clock_port": ("str",), "pnr.platform": ("str",), "pnr.tie_hi": ("str",),
    "pnr.tie_lo": ("str",), "pnr.sdc_file": ("path",),
    "post.recover.block": ("str",),
}


def _yaml_options() -> Tuple[Option, ...]:
    from . import (clockgating, fanout, modes, multibit, postlayout, sta,
                   vtswap, wire_model, slowcells, ladder, pnr_recipe)
    out = []
    taken = {o.name for o in OPTIONS}

    def add(name, sec, key, help="", **kw):
        if name in taken:
            return
        taken.add(name)
        t = _YAML_TYPES.get(name)
        if t:
            kw = {"overlay": True, **kw}
        out.append(_o(name, t[0] if t else "object", None,
                      help or f"flow.yaml {'.'.join(sec)}.{key}",
                      binds=YamlKey(tuple(sec), key),
                      **({"range": t[1]} if t and len(t) > 1 else {}), **kw))
    for k in sorted(vtswap.VT_SWAP_KEYS):
        add(f"syn.vt_swap.{k}", ("vt_swap",), k, _VT_HELP.get(k, ""))
    for k in sorted(postlayout.POST_KEYS):
        name = {"recover_opts": "post.recover.opts", "recover_swap": "post.recover.swap",
                "recover_order": "post.recover.order",
                "recover_block": "post.recover.block"}.get(k, f"post.{k}")
        add(name, ("pnr", "post_layout"), k, _POST_HELP.get(k, ""))
    for k in sorted(multibit.CFG_KEYS):
        add(f"syn.multibit.{k}", ("multibit",), k)
    for k in sorted(clockgating.KEYS):
        add(f"syn.clock_gating.{k}", ("clock_gating",), k,
            "clock-gate economy (clockgating.py)")
    for k in sorted(fanout.SPEC_KEYS):
        add(f"syn.fanout_buffer.{k}", ("fanout_buffer",), k, "pre-placement fanout buffering")
    for k in wire_model.KEYS:
        add(f"syn.wire_model.{k}", ("wire_model",), k, "pre-placement wire model")
    for k in sorted(modes.TARGET_KEYS):
        add(f"syn.mapping_target.{k}", ("mapping_target",), k, "ABC delay target calibration")
    for k in sorted(sta.STA_KEYS):
        add(f"syn.sta.{k}", ("sta",), k, "STA stage")
    for k in sorted(ladder._KEYS):
        add(f"run.ladder.{k}", ("pnr", "ladder"), k, "closure ladder")
    pnr_keys = (set(_PNR_RECIPE_KEYS) | ladder._ORFS_PNR_KEYS
                | {"clock_period", "clock_latency", "results", "command",
                   "placement_logs", "spef_restore_pincap"}) - {"post_layout"}
    for k in sorted(pnr_keys):
        ns = ("cts." + k[4:] if k.startswith("cts_") else
              "hold." + k[5:] if k.startswith("hold_") else "pnr." + k)
        add(ns, ("pnr",), k, f"pnr.{k} (P&R recipe key)",
            note="a pack's P&R command receives it")
    # whole-section switches and the pack's flow: defaults
    add("syn.shift_select", (), "shift_select",
        "shift_select: true / mapping (recorded; set syn.shifters.* instead)", settable=False)
    add("syn.multibit", (), "multibit", "multibit: (register-mapper multibit=)")
    add("syn.clock_gating", (), "clock_gating", "clock_gating: true / mapping")
    for k in ("sv2v_docker_image", "abc_script_file", "abc_read_lib_args",
              "enable_classical_resub", "partitions", "mapping_target",
              "fanout_buffer", "slow_cell_filter", "multibit", "wire_model",
              "clock_gating"):
        nm = f"flow.pack.{k}"
        if nm not in taken:
            taken.add(nm)
            out.append(_o(nm, "object", None, f"tech pack flow.{k} default (recorded; "
                          "set it in the pack, or with the flow's own key or argument)",
                          binds=PackKey(k), settable=False))
    for k in sorted(slowcells.KEYS):
        nm = f"syn.library.slow_cell_filter.{k}"
        taken.add(nm)
        out.append(_o(nm, "object", None, "pack flow.slow_cell_filter key (recorded; the "
                      "flow's own control is syn.library.slow_cell_ratio)",
                      binds=PackKey(f"slow_cell_filter.{k}"), settable=False))
    return tuple(out)


# What an option needs to have an effect (documentation, and the test that
# every option set is applied). "_partitions": the --partitions argument.
_REQUIRES: Dict[str, Tuple[Tuple[str, Any], ...]] = {}


def _req(names, *pairs):
    for n in names.split():
        _REQUIRES[n] = tuple(pairs)


_req("syn.mux.priority_min_width", ("syn.mux.priority_decode", True))
_req("syn.multipliers.min_width syn.multipliers.merge_adds syn.multipliers.flatten_adds "
     "syn.multipliers.prefix_final_adder syn.multipliers.gate_compressors "
     "syn.multipliers.cell_stages syn.multipliers.tree syn.multipliers.nand_pp "
     "syn.multipliers.map_final_adder", ("syn.multipliers.cells", True))
_req("syn.multipliers.flatten_gated", ("syn.multipliers.cells", True),
     ("syn.multipliers.flatten_adds", True))
_req("syn.multipliers.map_final_min_width", ("syn.multipliers.cells", True),
     ("syn.multipliers.map_final_adder", True))
_req("syn.adders.cells.max_width syn.adders.cells.increments",
     ("syn.adders.cells.min_width", 5))
_req("syn.adders.cells2.max_width", ("syn.adders.cells2.min_width", 5))
_req("syn.adders.prefix.max_width syn.adders.prefix.arch", ("syn.adders.prefix.min_width", 5))
_req("syn.adders.prefix2.max_width syn.adders.prefix2.arch syn.adders.prefix2.isolate_inputs "
     "syn.adders.prefix2.boundary", ("syn.adders.prefix2.min_width", 5))
_req("syn.shifters.min_arms syn.shifters.radix syn.shifters.wide syn.shifters.recipes "
     "syn.shifters.direct_decode", ("syn.shifters.early", True))
_req("syn.shifters.default_dont_care syn.shifters.select_max_fanout syn.shifters.l2_range_gate "
     "syn.shifters.l2_range_aligned syn.shifters.l2_range_compose syn.shifters.select_dup_fanout "
     "syn.shifters.form",
     ("syn.shifters.early", True), ("syn.shifters.direct_decode", True))
_req("syn.shifters.window_types syn.shifters.window_types_max syn.shifters.window_direct",
     ("syn.shifters.early", True), ("syn.shifters.direct_decode", True),
     ("syn.shifters.form", "window"), ("syn.shifters.window_general", True))
_req("syn.shifters.window_count syn.shifters.window_copy_cost syn.shifters.window_clip "
     "syn.shifters.window_per_node syn.shifters.window_cuts syn.shifters.window_general",
     ("syn.shifters.early", True), ("syn.shifters.direct_decode", True),
     ("syn.shifters.form", "window"))
_req("syn.datapath.outline_ids", ("syn.datapath.outline", True))
_req("syn.fanout_buffer.enabled syn.fanout_buffer.bundle_pins syn.fanout_buffer.input "
     "syn.fanout_buffer.output syn.fanout_buffer.max_fanout", ("syn.fanout_buffer.cell", "BUFX"))
_req("syn.post_map_sweep.stubs", ("syn.post_map_sweep.enable", True))
_req("syn.abc.partition.group_map syn.abc.partition.group_by_depth "
     "syn.abc.partition.isolate_substr syn.abc.partition.group_scripts", ("_partitions", 2))
_req("syn.abc.partition.isolate_each", ("_partitions", 2),
     ("syn.abc.partition.isolate_substr", "_dp"))

_REGISTRY: Optional[Dict[str, Option]] = None


def registry() -> Dict[str, Option]:
    """{dotted name: Option}, every option (flow-level and yaml-key)."""
    global _REGISTRY
    if _REGISTRY is None:
        reg = {}
        import dataclasses
        for o in OPTIONS + _yaml_options():
            if o.name in _REQUIRES:
                o = dataclasses.replace(o, requires=_REQUIRES[o.name])
            assert o.name not in reg, o.name
            reg[o.name] = o
        _REGISTRY = reg
    return _REGISTRY


def legacy_map() -> Dict[str, Tuple[str, ...]]:
    """{legacy variable: (attribute, ...)}."""
    m: Dict[str, Tuple[str, ...]] = {}
    for o in registry().values():
        for v in o.legacy_env:
            m[v] = m.get(v, ()) + (o.name,)
    return m


def suggest(name: str, n: int = 3) -> str:
    near = difflib.get_close_matches(name, list(registry()), n, 0.6)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def suggest_legacy(var: str, n: int = 3) -> str:
    near = difflib.get_close_matches(var, list(legacy_map()), n, 0.7)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def lookup(name: str, where: str = "") -> Option:
    try:
        return registry()[name]
    except KeyError:
        raise OptionError(f"{where + ': ' if where else ''}unknown option "
                          f"{name!r}{suggest(name)}") from None


def is_interface_var(var: str) -> bool:
    return var in INTERFACE_VARS or any(
        k.endswith("_") and var.startswith(k) for k in INTERFACE_VARS)


# ---------------------------------------------------------------- settings

SOURCES = ("argument", "env", "recipe", "techpack", "default")


@dataclass
class Settings:
    """The resolved value and source of every option."""
    values: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)
    legacy_used: Dict[str, str] = field(default_factory=dict)
    strict: bool = False
    # the names a flow.yaml `options:` mapping set ({name: value as given});
    # a power flow passes them on to its domain sub-runs
    from_options: Dict[str, Any] = field(default_factory=dict)

    def get(self, name: str):
        if name not in self.values:
            lookup(name)
            return registry()[name].default
        return self.values[name]

    __getitem__ = get

    def source(self, name: str) -> str:
        return self.sources.get(name, "default")

    def is_set(self, name: str) -> bool:
        return self.source(name) != "default"

    def overridden(self) -> Dict[str, Any]:
        """{name: value} of every option not at its built-in default source."""
        return {k: v for k, v in self.values.items() if self.is_set(k)}

    def as_record(self) -> Dict[str, dict]:
        out = {}
        for name, o in sorted(registry().items()):
            v = self.values.get(name, o.default)
            out[name] = {"value": _jsonable(v), "source": self.source(name)}
        return out


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


_WARNED: set = set()


def _scoped_advice(names) -> Optional[str]:
    from . import scoping
    return scoping.advice(tuple(names))


def _use_instead(names) -> str:
    """What a legacy variable's message recommends: the scoped form when the
    option behind it is itself a legacy form (syn.clock_gating.data_gate,
    syn.adders.prefix2.* / cells2.*), else the option name(s)."""
    adv = _scoped_advice(names)
    return adv if adv else f"{' / '.join(names)} (--set NAME=VALUE)"


def _warn_legacy(var: str, names: Tuple[str, ...], text: str,
                 stream=None) -> None:
    if var in _WARNED:
        return
    _WARNED.add(var)
    print(f"[circt_flow] WARNING: legacy environment variable {var}={text!r} "
          f"is deprecated: set {' / '.join(names)} instead (--set NAME=VALUE, "
          f"flow.yaml options:); it is no longer accepted from release "
          f"{LEGACY_ENV_REMOVED_IN} on", file=stream or sys.stderr)


def check_environment(env: Mapping[str, str], strict: bool) -> None:
    """Unknown CIRCT_FLOW_* / CIRCT_VT_* names are an error under --strict
    (a warning otherwise); so is any legacy variable under --strict."""
    lm = legacy_map()
    for var in sorted(env):
        if not var.startswith(LEGACY_PREFIXES) or is_interface_var(var):
            continue
        if var in lm:
            if strict:
                raise OptionError(
                    f"--strict: legacy environment variable {var} is set; use "
                    f"{_use_instead(lm[var])}")
            continue
        near = difflib.get_close_matches(var, list(lm) + list(INTERFACE_VARS), 3, 0.7)
        msg = (f"unknown environment variable {var}: not an option of this flow"
               + (f" (did you mean {', '.join(near)}?)" if near else ""))
        if strict:
            raise OptionError("--strict: " + msg)
        if var not in _WARNED:
            _WARNED.add(var)
            print(f"[circt_flow] WARNING: {msg}; it is ignored", file=sys.stderr)


def resolve(argument: Optional[Mapping[str, Any]] = None,
            recipe: Optional[Mapping[str, Any]] = None,
            techpack: Optional[Mapping[str, Any]] = None,
            env: Optional[Mapping[str, str]] = None,
            strict: bool = False, names: Optional[Iterable[str]] = None,
            warn: bool = True, where: Optional[Mapping[str, str]] = None
            ) -> Settings:
    """Resolve every option (or only `names`). `argument`, `recipe`,
    `techpack`: {dotted name: value}; `env`: the process environment (legacy
    variables); `where`: {source: label} for messages (the recipe's file
    name). Unknown names, bad values and a pack-only option set by an
    argument or a recipe raise OptionError."""
    reg = registry()
    env = {} if env is None else env
    where = dict(where or {})
    check_environment(env, strict)
    layers = []
    for src, d in (("argument", argument), ("recipe", recipe),
                   ("techpack", techpack)):
        label = where.get(src, src)
        parsed = {}
        for k, v in (d or {}).items():
            o = lookup(k, label)
            if not o.settable and src == "argument":
                raise not_settable(o, label)
            parsed[k] = o.parse(v, label)
        layers.append((src, parsed))
    s = Settings(strict=strict)
    want = list(reg) if names is None else list(names)
    arg, rec, pack = (d for _, d in layers)
    for name in want:
        o = reg[name]
        val, src = o.default, "default"
        for lsrc, d in (("techpack", pack), ("recipe", rec)):
            if d.get(name) is not None:
                val, src = d[name], lsrc
        for var in o.legacy_env:
            text = env.get(var)
            if text is None:
                continue
            v = o.legacy(text, var)
            if v is None:
                continue
            if strict:
                raise OptionError(f"--strict: legacy environment variable "
                                  f"{var} is set; use {_use_instead((name,))}")
            if warn and _scoped_advice(legacy_map()[var]) is None:
                # a legacy form of a scoped command: scoping.check warns once
                # for the whole form, naming this variable and the scoped form
                _warn_legacy(var, legacy_map()[var], text)
            s.legacy_used[var] = text
            val, src = v, "env"
        if arg.get(name) is not None:
            val, src = arg[name], "argument"
        s.values[name] = val
        s.sources[name] = src
    _check_shifter_window(s)
    from . import scoping
    scoping.check(s)
    return s


_WINDOW_GENERAL = ("syn.shifters.window_types", "syn.shifters.window_types_max",
                   "syn.shifters.window_direct")
_WINDOW = ("syn.shifters.window_count", "syn.shifters.window_copy_cost",
           "syn.shifters.window_clip", "syn.shifters.window_per_node",
           "syn.shifters.window_cuts", "syn.shifters.window_general") + _WINDOW_GENERAL


def _check_shifter_window(s: "Settings") -> None:
    """The window form's options are refused, not ignored, when what they act
    on is off: syn.shifters.form = window needs the pass (early or late) and
    direct_decode; a window_* option needs form = window; window_types /
    _max / direct need window_general. window_cuts is a comma list of
    integers."""
    names = ("syn.shifters.form", "syn.shifters.early", "syn.shifters.late",
             "syn.shifters.direct_decode") + _WINDOW
    if not all(n in s.values for n in names):
        return
    reg = registry()
    on = [n for n in _WINDOW if s.values[n] not in (None, reg[n].default)]
    if s.values["syn.shifters.form"] == "window":
        if not ((s.values["syn.shifters.early"] or s.values["syn.shifters.late"])
                and s.values["syn.shifters.direct_decode"]):
            raise OptionError("syn.shifters.form = window needs the pass (syn.shifters.early "
                              "or syn.shifters.late) and syn.shifters.direct_decode = true")
    elif on:
        raise OptionError(f"{', '.join(on)}: window-form option(s) need syn.shifters.form = window")
    if not s.values["syn.shifters.window_general"]:
        g = [n for n in on if n in _WINDOW_GENERAL]
        if g:
            raise OptionError(f"{', '.join(g)}: need syn.shifters.window_general = true")
    wc = s.values["syn.shifters.window_cuts"]
    if wc is not None:
        _check_window_cuts(wc)
    wt = s.values["syn.shifters.window_types"]
    if wt is not None:
        _check_window_types(wt)


_MAX_WINDOW_CUTS = 12     # the same limit as MapShiftSelectPass kMaxWindowCuts
_ASCII_INT = re.compile(r"[0-9]{1,9}", re.ASCII)


def _check_window_cuts(wc) -> None:
    """window_cuts: 1..12 distinct ASCII non-negative integers (str.isdigit
    also accepts non-ASCII digits, so it is not used). Whether each cut lies
    inside the shifter's amount range is known to the pass, which refuses."""
    parts = [x.strip() for x in str(wc).split(",") if x.strip()]
    name = "syn.shifters.window_cuts"
    if not parts:
        raise OptionError(f"{name} = {wc!r}: a comma list of integers")
    if len(parts) > _MAX_WINDOW_CUTS:
        raise OptionError(f"{name}: at most {_MAX_WINDOW_CUTS} cuts, got {len(parts)}")
    for x in parts:
        if not _ASCII_INT.fullmatch(x):
            raise OptionError(f"{name} = {wc!r}: {x!r} is not an ASCII integer (at most 9 digits)")
    if len({int(x) for x in parts}) != len(parts):
        raise OptionError(f"{name} = {wc!r}: duplicate cut")


def _check_window_types(wt) -> None:
    """window_types: 'phase:first-last;...', ASCII integers, first <= last."""
    name = "syn.shifters.window_types"
    items = [x.strip() for x in str(wt).split(";") if x.strip()]
    if not items:
        raise OptionError(f"{name} = {wt!r}: 'phase:first-last;...' expected")
    for it in items:
        m = re.fullmatch(r"([0-9]{1,9}):([0-9]{1,9})-([0-9]{1,9})", it, re.ASCII)
        if not m:
            raise OptionError(f"{name}: {it!r} is not phase:first-last (ASCII integers)")
        if int(m.group(2)) > int(m.group(3)):
            raise OptionError(f"{name}: {it!r}: first > last")


def _need_text(k: str, v) -> str:
    """One `Needs:` entry of the generated option table: the pass runs at the early or the late position, so
    syn.shifters.early=True is met by syn.shifters.late=True as well."""
    if (k, v) == ("syn.shifters.early", True):
        return "syn.shifters.early=True (or syn.shifters.late=True)"
    if k == "_partitions":
        return "--partitions 2 or more"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{k} set"
    if k == "syn.abc.partition.isolate_substr" or k == "syn.fanout_buffer.cell":
        return f"{k} set"
    return f"{k}={v}"


def not_settable(o: Option, label: str) -> OptionError:
    return OptionError(
        f"{label}: {o.name} is only recorded (the tech pack only, or the whole "
        f"section as the flow.yaml has it): it cannot be set here. {o.help}")


def legacy_to_settings(env: Mapping[str, str]) -> Dict[str, Any]:
    """{dotted name: value} for the legacy variables in `env` (the attribute
    form of an environment; tests and migration)."""
    out = {}
    lm = legacy_map()
    for var, text in env.items():
        for name in lm.get(var, ()):
            v = registry()[name].legacy(text, var)
            if v is not None:
                out[name] = v
    return out


def parse_assignments(items: Iterable[str]) -> Dict[str, Any]:
    """`NAME=VALUE` command-line assignments; values are parsed by the
    option's type (strings as given)."""
    out = {}
    for it in items or ():
        if "=" not in it:
            raise OptionError(f"--set {it!r}: expected NAME=VALUE")
        k, v = it.split("=", 1)
        k = k.strip()
        o = lookup(k, "--set")
        out[k] = o.parse(v, "--set") if o.type != "object" else _yaml_value(v)
    return out


def _yaml_value(text: str):
    import yaml
    return yaml.safe_load(text)


def from_mapping(d: Optional[Mapping[str, Any]], where: str) -> Dict[str, Any]:
    """A recipe's `options:` mapping (dotted names; nested mappings are
    flattened), checked against the registry."""
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise OptionError(f"{where}: options: must be a mapping of option names")
    out = {}

    def walk(prefix, m):
        for k, v in m.items():
            name = f"{prefix}.{k}" if prefix else str(k)
            if name in registry():
                out[name] = v
            elif isinstance(v, dict) and any(n.startswith(name + ".") for n in registry()):
                walk(name, v)
            else:
                lookup(name, where)
    walk("", d)
    return out


def from_yaml_sections(raw: Mapping[str, Any], where: str = "flow.yaml") -> Dict[str, Any]:
    """{dotted name: value} of the yaml-bound options a flow.yaml (or a pack's
    flow:) sets in its sections. shift_select: also yields syn.shifters.*."""
    out: Dict[str, Any] = {}
    for name, o in registry().items():
        for b in o.binds:
            if not isinstance(b, YamlKey):
                continue
            sec = raw
            for p in b.section:
                sec = sec.get(p) if isinstance(sec, dict) else None
            if b.section == () and b.key in raw:
                out[name] = raw[b.key]
            elif isinstance(sec, dict) and b.key in sec and o.overlaid:
                out[name] = sec[b.key]
    out.update(_shifters_from_yaml(raw.get("shift_select"), where))
    vt = raw.get("vt_swap")
    if isinstance(vt, dict) and "flop_cap" in vt:
        out["syn.vt_swap.flop_cap"] = vt["flop_cap"]
    pl = (raw.get("pnr") or {}).get("post_layout") if isinstance(raw.get("pnr"), dict) else None
    if isinstance(pl, dict) and "flop_cap" in pl:
        out["post.flop_cap"] = pl["flop_cap"]
    lad = (raw.get("pnr") or {}).get("ladder") if isinstance(raw.get("pnr"), dict) else None
    if isinstance(lad, dict):
        for k in ("submit", "cancel"):
            if k in lad:
                out[f"run.ladder.{k}"] = lad[k]
    return out


_SHIFT_KEYS = ("min_arms", "radix", "wide", "recipes", "direct_decode",
               "default_dont_care", "select_max_fanout", "l2_range_gate",
               "l2_range_aligned", "l2_range_compose", "select_dup_fanout",
               "form", "window_count", "window_copy_cost", "window_clip",
               "window_per_node", "window_cuts", "window_general", "window_types",
               "window_types_max", "window_direct")


def _shifters_from_yaml(d, where: str = "flow.yaml") -> Dict[str, Any]:
    """syn.shifters.* from a flow.yaml shift_select: value (checked by
    shiftselect.parse first)."""
    from . import shiftselect
    spec = shiftselect.parse(d, where)
    if not spec:
        return {}
    out = {}
    if spec.get("enable", True):
        out["syn.shifters.early" if spec.get("early", True)
            else "syn.shifters.late"] = True
    for k in _SHIFT_KEYS:
        v = spec.get(k)
        if v is None or v == "" or v is False:
            continue
        out[f"syn.shifters.{k}"] = str(v) if k == "wide" else v
    return out


def overlay_yaml(raw: dict, s: Settings) -> dict:
    """A copy of the flow.yaml dict with every overlaid yaml-bound option
    (Option.overlaid) written into its section, so the section's own parser
    sees (and validates) it. An argument or a recipe `options:` entry creates
    the section; a tech-pack default or a legacy variable only fills a key of
    a section the flow.yaml has (it never switches a section on); a
    tech-pack default never replaces a key the flow.yaml sets."""
    import copy
    raw = copy.deepcopy(raw)
    for name, o in registry().items():
        src = s.source(name)
        if src == "default" or not o.overlaid:
            continue
        if src == "recipe" and name not in s.from_options:
            continue        # the section's own key: left exactly as written
        fill_only = src in ("techpack", "env")
        for b in o.binds:
            if not isinstance(b, YamlKey):
                continue
            if b.section == ():
                if not fill_only:
                    raw[b.key] = s.get(name)
                continue
            sec, present = raw, True
            for p in b.section:
                nxt = sec.get(p) if isinstance(sec, dict) else None
                if not isinstance(nxt, dict):
                    present = False
                    if fill_only:
                        break
                    if nxt is False:
                        raise OptionError(
                            f"{name}: the flow.yaml turns {'.'.join(b.section)} "
                            "off (false); set it on there")
                    nxt = {}
                    sec[p] = nxt
                sec = nxt
            if fill_only and (not present or (src == "techpack" and b.key in sec)):
                continue
            sec[b.key] = s.get(name)
    return raw


# ---------------------------------------------------------------- reporting

def render_markdown() -> str:
    """doc/options.md (generated; tests/test_options.py checks it is current)."""
    reg = registry()
    lines = ["# Option reference (generated)", "",
             "GENERATED by `python3 -m circt_flow options --markdown > doc/options.md` from "
             "`scripts/circt_flow/options.py`; do not edit by hand (a test fails when it is stale).",
             "",
             "Precedence, highest first: argument (`--set NAME=VALUE`), legacy environment variable "
             "(transition only), recipe (the flow.yaml: `options:` and its sections), tech-pack `flow:` "
             "default, built-in default. Every run writes `settings.json` (value and source of every "
             "option, the tool commit, the pass pipeline as executed).",
             "",
             "## Deprecation", "",
             f"The legacy `CIRCT_FLOW_*` / `CIRCT_VT_*` variables are accepted, with one warning each "
             f"naming the replacing attribute, in release {FIRST_RELEASE_WITH_REGISTRY} (the release "
             f"that ships this registry) and release {FIRST_RELEASE_WITH_REGISTRY + 1}; from release "
             f"{LEGACY_ENV_REMOVED_IN} on they are refused (`options.LEGACY_ENV_REMOVED_IN`). "
             "`--strict` refuses them now, and refuses any unknown `CIRCT_FLOW_*` / `CIRCT_VT_*` name "
             "in the environment.", "",
             "Legacy values are read exactly as the old code read them: a flag is `1` or `0` "
             "(anything else is an error; earlier releases read it as off, or as on for a default-on flag); "
             "a number has no blank and no leading zero; an empty value is unset, except "
             "`CIRCT_FLOW_ADDER_ARCH=` (empty), which keeps its old meaning (set-adder-arch runs "
             "with an empty arch). A pack's `flow.env` (legacy names) is a tech-pack default: it "
             "ranks below the flow.yaml and the environment (before the registry it was copied "
             "into the process environment, above the flow.yaml's shift_select keys, and was "
             "inherited by child processes); `--strict` refuses it.", "",
             "## Release procedure", "",
             "Every public release bumps `CURRENT_RELEASE` in `scripts/circt_flow/options.py` "
             f"(now {CURRENT_RELEASE}) in the same commit as the release. "
             "`tests/test_options.py::test_legacy_shim_removed_by_its_release` fails while "
             "`CURRENT_RELEASE >= LEGACY_ENV_REMOVED_IN` and any option still has a legacy "
             "variable: the release that reaches it must first remove the shim (`legacy_env`, "
             "`legacy_mode`, `INTERFACE_VARS` stay).", "",
             "Options marked pack-only are recorded from the tech pack's `flow:` section for the "
             "record; `--set` and `options:` refuse them.", "",
             "An option value that contains commas (a list) must not travel through "
             "`sbatch --export=...` or another wrapper that passes variables as a comma-separated "
             "list: the value is cut at its first comma (the run silently gets only part of the list). "
             "Pass them with `--set` or in the settings file; the flow's own submit and container paths "
             "(closure ladder, ORFS P&R, OpenSTA) carry no option that way.", "",
             "## Options", "",
             "| option | type | default | binds | legacy variable | help |",
             "|---|---|---|---|---|---|"]
    for name in sorted(reg):
        o = reg[name]
        t = o.type + (f" {'/'.join(o.choices)}" if o.choices else "") + \
            (f" [{'' if o.range[0] is None else o.range[0]}, "
             f"{'' if o.range[1] is None else o.range[1]}]" if o.range else "") + \
            (f" ({o.unit})" if o.unit else "")
        d = "unset" if o.default is None else repr(o.default)
        h = o.help + (" EXPERIMENTAL." if o.experimental else "") + \
            (" Pack-only." if not o.settable else "") + \
            (f" Needs: {', '.join(_need_text(k, v) for k, v in o.requires)}." if o.requires else "") + \
            (f" Note: {o.note}." if o.note else "")
        lines.append(f"| `{name}` | {t} | {d} | {'; '.join(map(str, o.binds))} | "
                     f"{', '.join(f'`{v}`' for v in o.legacy_env)}"
                     f"{' (' + o.legacy_mode + ')' if o.legacy_env and o.legacy_mode != 'value' else ''}"
                     f" | {h.replace('|', '/')} |")
    lines += ["", "## Environment variables that are not options", "",
              "Exported by the flow to a pack's P&R command or a ladder rung (allowed under `--strict`):", ""]
    for k, v in sorted(INTERFACE_VARS.items()):
        lines.append(f"* `{k}{'<...>' if k.endswith('_') else ''}`: {v}")
    lines += ["", "Tool, build and location variables (not options; unchanged):", ""]
    for k, v in sorted(NON_OPTION_ENV.items()):
        lines.append(f"* `{k}`" + (f": {v}" if v else ""))
    return "\n".join(lines) + "\n"


def mapping_table() -> str:
    """legacy variable -> attribute (markdown)."""
    lines = ["| legacy variable | attribute(s) | mode |", "|---|---|---|"]
    for var, names in sorted(legacy_map().items()):
        o = registry()[names[0]]
        lines.append(f"| `{var}` | {', '.join(f'`{n}`' for n in names)} | "
                     f"{o.legacy_mode} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="circt_flow options",
                                 description="List the flow's options.")
    ap.add_argument("pattern", nargs="?", default="",
                    help="substring of the option names to list")
    ap.add_argument("--markdown", action="store_true",
                    help="print doc/options.md")
    ap.add_argument("--legacy", action="store_true",
                    help="print the legacy variable -> attribute table")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pack", metavar="PACK",
                    help="also list the P&R options of this tech pack (its directory or "
                    "techpack .yaml; the registry its pnr.settings_registry names)")
    ap.add_argument("--group", metavar="GROUP",
                    help="with --pack: only the pack options of this group (the registry's `group` field; "
                    "with --markdown, only that group's table)")
    ap.add_argument("--stub", action="store_true",
                    help="print doc/db_types.pyi (typed stub of the session's s.db namespaces)")
    ap.add_argument("--diff", nargs=2, metavar=("A_JSON", "B_JSON"),
                    help="what differs between two runs' settings.json")
    a = ap.parse_args(argv)
    if a.group and not a.pack:
        ap.error("--group selects pack options: it needs --pack PACK")
    if a.diff:
        from . import provenance_settings as ps
        rows = ps.diff(ps.read(a.diff[0]), ps.read(a.diff[1]))
        for name, x, y in rows:
            print(f"{name}: {x} -> {y}")
        return 1 if rows else 0
    if a.pack:
        from . import pack_options
        return pack_options.main(a)
    if a.stub:
        from . import db
        sys.stdout.write(db.stub_text())
        return 0
    if a.markdown:
        sys.stdout.write(render_markdown())
        return 0
    if a.legacy:
        sys.stdout.write(mapping_table())
        return 0
    rows = [o for n, o in sorted(registry().items())
            if a.pattern in n]
    if a.json:
        print(json.dumps([{"name": o.name, "type": o.type, "default": _jsonable(o.default),
                           "unit": o.unit, "range": o.range, "choices": list(o.choices),
                           "binds": [str(b) for b in o.binds],
                           "legacy_env": list(o.legacy_env), "help": o.help,
                           "experimental": o.experimental} for o in rows], indent=1))
        return 0
    for o in rows:
        print(f"{o.name:45s} {o.type:7s} {('unset' if o.default is None else repr(o.default)):14s} "
              f"{o.help}"
              + (f"  [legacy {', '.join(o.legacy_env)}]" if o.legacy_env else ""))
    return 0


def of(cfg) -> Settings:
    """The resolved settings of a FlowConfig; for an object without them (a
    test's stand-in), the built-in defaults under the legacy environment."""
    s = getattr(cfg, "settings", None)
    if isinstance(s, Settings):
        return s
    return resolve(env=os.environ)


def cli_default(name: str, env: Optional[Mapping[str, str]] = None):
    """The value of one option for a stand-alone command line tool
    (postlayout, depth_groups) when its own argument is not given: the
    legacy variable (warned, transition only), else the built-in default."""
    s = resolve(env=os.environ if env is None else env, names=[name])
    return s.get(name)
