"""The option registry (circt_flow/options.py) and the resolved settings:
golden pass command lines, legacy names, --strict, validation, bindings in
the built tool, settings.json, the generated doc, the deprecation schedule."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from circt_flow import options, provenance_settings
from circt_flow.config import FlowConfig

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(HERE))
import golden_cmdlines  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(options.LEGACY_PREFIXES):
            monkeypatch.delenv(k)


# ---------------------------------------------------------------- (1) golden

def _record(attributes):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        return golden_cmdlines.record(SCRIPTS, attributes)


def test_golden_command_lines_from_legacy_environment():
    want = json.loads((HERE / "data" / "golden_cmdlines.json").read_text())
    got = _record(False)
    assert sorted(got) == sorted(want)
    for case in want:
        assert got[case] == want[case], case


def test_golden_command_lines_from_dotted_settings():
    want = json.loads((HERE / "data" / "golden_cmdlines.json").read_text())
    got = _record(True)
    for case in want:
        assert got[case] == want[case], case


# ---------------------------------------------------------------- (2) legacy names

_SAMPLE = {"int": "7", "float": "0.5", "str": "abc", "path": "/x/y.json",
           "bool": "1"}


def _sample(o):
    if o.type == "choice":
        return o.choices[-1]
    if o.name == "syn.clock_gating.data_gate":
        return "total_r@1-4@zero,m/r@5-12@xor"
    if o.range and o.type == "int":
        return str(int(o.range[1] if o.range[1] is not None else (o.range[0] or 0) + 3))
    return _SAMPLE[o.type]


def test_every_legacy_name_maps_to_its_attribute_and_back():
    lm = options.legacy_map()
    assert lm, "no legacy names"
    for var, names in lm.items():
        for name in names:
            o = options.registry()[name]
            text = _sample(o)
            v = o.legacy(text, var)
            assert v is not None, var
            assert o.to_legacy(v) == text, (var, name, v)
            s = options.resolve(env={var: text}, names=[name], warn=False)
            assert s[name] == v and s.source(name) == "env"
            assert options.legacy_to_settings({var: text})[name] == v


def test_every_flow_variable_in_the_tree_is_covered():
    """Every CIRCT_FLOW_* / CIRCT_VT_* name in the code, docs and examples is
    a legacy option name or an interface variable (prefix mentions such as
    CIRCT_FLOW_NCRB_ in prose excepted)."""
    names = set()
    for d in ("scripts", "doc", "examples", "designs", "recipes", "profiles"):
        for p in (ROOT / d).rglob("*"):
            if p.suffix in (".py", ".sh", ".md", ".yaml", ".tcl") and p.is_file() \
                    and "golden_cmdlines" not in p.name \
                    and p.name not in ("options.md", "test_options.py"):
                names |= set(re.findall(r"\b(?:CIRCT_FLOW|CIRCT_VT)_[A-Z0-9_]*[A-Z0-9]\b",
                                        p.read_text(errors="replace")))
    lm = options.legacy_map()
    missing = sorted(n for n in names if n not in lm and not options.is_interface_var(n)
                     and not any(k.startswith(n + "_") for k in lm))
    assert not missing, missing


def test_legacy_modes():
    reg = options.registry()
    assert reg["syn.clock_gating.enable"].legacy("1", "X") is False      # NO_CLOCK_GATING=1
    assert reg["syn.clock_gating.bit_level"].legacy("0", "X") is False
    # a shift_select flag at 0 leaves the flow.yaml key alone, as before
    s = options.resolve(recipe={"syn.shifters.direct_decode": True},
                        env={"CIRCT_FLOW_SHIFT_SELECT_DIRECT": "0"}, warn=False)
    assert s["syn.shifters.direct_decode"] is True and s.source(
        "syn.shifters.direct_decode") == "recipe"
    # empty is unset
    s = options.resolve(env={"CIRCT_FLOW_ADDER_CELLS": ""}, warn=False)
    assert s["syn.adders.cells.min_width"] is None


def test_precedence():
    rec = {"syn.adders.cells.min_width": 14}
    pack = {"syn.adders.cells.min_width": 5, "syn.fanout.max": 12}
    s = options.resolve(recipe=rec, techpack=pack, env={}, warn=False)
    assert s["syn.adders.cells.min_width"] == 14 and s.source("syn.adders.cells.min_width") == "recipe"
    assert s["syn.fanout.max"] == 12 and s.source("syn.fanout.max") == "techpack"
    s = options.resolve(recipe=rec, techpack=pack, env={"CIRCT_FLOW_ADDER_CELLS": "15"},
                        warn=False)
    assert s["syn.adders.cells.min_width"] == 15 and s.source("syn.adders.cells.min_width") == "env"
    s = options.resolve(argument={"syn.adders.cells.min_width": 17}, recipe=rec,
                        env={"CIRCT_FLOW_ADDER_CELLS": "15"}, warn=False)
    assert s["syn.adders.cells.min_width"] == 17 and s.source("syn.adders.cells.min_width") == "argument"
    assert s["syn.fanout.max"] is None and s.source("syn.fanout.max") == "default"


def test_legacy_warning_names_attribute_and_removal_release(capsys):
    options._WARNED.discard("CIRCT_FLOW_MAX_FANOUT")
    options.resolve(env={"CIRCT_FLOW_MAX_FANOUT": "6"})
    options.resolve(env={"CIRCT_FLOW_MAX_FANOUT": "6"})
    err = capsys.readouterr().err
    assert err.count("CIRCT_FLOW_MAX_FANOUT") == 1          # warned once
    assert "syn.fanout.max" in err
    assert f"release {options.LEGACY_ENV_REMOVED_IN}" in err


# ---------------------------------------------------------------- (3) strict

def test_strict_refuses_legacy_and_unknown_names():
    with pytest.raises(options.OptionError, match="syn.adders.cells.min_width"):
        options.resolve(env={"CIRCT_FLOW_ADDER_CELLS": "14"}, strict=True)
    with pytest.raises(options.OptionError, match="CIRCT_FLOW_ADDER_CELS.*did you mean"):
        options.resolve(env={"CIRCT_FLOW_ADDER_CELS": "14"}, strict=True)
    with pytest.raises(options.OptionError, match="CIRCT_VT_FLOP_CAPP"):
        options.resolve(env={"CIRCT_VT_FLOP_CAPP": "L"}, strict=True)
    # the variables the flow exports, and non-flow names, are allowed
    s = options.resolve(env={"CIRCT_FLOW_RUNG_DIR": "/x", "CIRCT_FLOW_WIRE_RC_TT": "/y",
                             "HOME": "/h"}, strict=True)
    assert s.strict and not s.legacy_used
    s = options.resolve(argument={"syn.fanout.max": 4}, env={}, strict=True)
    assert s["syn.fanout.max"] == 4


def test_unknown_variable_is_a_warning_without_strict(capsys):
    options.resolve(env={"CIRCT_FLOW_NOT_A_KNOB": "1"})
    assert "CIRCT_FLOW_NOT_A_KNOB" in capsys.readouterr().err


# ---------------------------------------------------------------- (4) errors

@pytest.mark.parametrize("src,value,pat", [
    ({"syn.fanout.max": "x"}, None, "integer"),
    ({"syn.fanout.max": 0}, None, "outside"),
    ({"syn.adders.prefix2.boundary": 3}, None, "outside"),
    ({"syn.adders.prefix.arch": "KOGE-STONE"}, None, "did you mean KOGGE-STONE"),
    ({"syn.multipliers.tree": "walace"}, None, "did you mean wallace"),
    ({"syn.clock_gating.enable": "maybe"}, None, "true or false"),
    ({"syn.clock_gating.data_gate": "total_r@1-4@one"}, None, "does not match"),
    ({"syn.library.slow_cell_ratio": True}, None, "number"),
    ({"syn.adders.cell.min_width": 14}, None, "did you mean syn.adders.cells.min_width"),
])
def test_type_range_and_name_errors(src, value, pat):
    with pytest.raises(options.OptionError, match=pat):
        options.resolve(recipe=src, env={})


@pytest.mark.parametrize("var,text", [
    ("CIRCT_FLOW_NO_CLOCK_GATING", "yes"), ("CIRCT_FLOW_ADDER_CELLS", "nine"),
    ("CIRCT_FLOW_PREFIX2_BOUNDARY", "5"), ("CIRCT_FLOW_MUL_CELLS_TREE", "booth"),
    ("CIRCT_FLOW_SLOW_CELL_RATIO", "x")])
def test_legacy_values_are_validated(var, text):
    with pytest.raises(options.OptionError, match=var):
        options.resolve(env={var: text}, warn=False)


def test_recipe_options_mapping_errors_name_the_nearest(tmp_path):
    with pytest.raises(options.OptionError, match="did you mean syn.shifters.radix"):
        options.from_mapping({"syn": {"shifters": {"radixx": 4}}}, "flow.yaml: options")
    assert options.from_mapping({"syn": {"shifters": {"radix": 4}}}, "x") == \
        {"syn.shifters.radix": 4}
    with pytest.raises(options.OptionError):
        options.parse_assignments(["syn.fanout.max"])


# ---------------------------------------------------------------- (5) bindings

def _opt_help():
    """`circt-abc-opt --help`, parsed. Skips only when no mapper was built at
    all (no CIRCT_ABC_OPT and no mapper/build directory); a named or built
    tree whose binary is missing fails."""
    named = os.environ.get("CIRCT_ABC_OPT")
    tool = named or str(ROOT / "mapper/build/bin/circt-abc-opt")
    if not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
        if named or (ROOT / "mapper" / "build").is_dir():
            pytest.fail(f"circt-abc-opt expected at {tool} (CIRCT_ABC_OPT or "
                        "mapper/build) but not executable")
        pytest.skip("no mapper build (set CIRCT_ABC_OPT to run this test)")
    out = subprocess.run([tool, "--help"], capture_output=True, text=True).stdout
    passes, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^      --([\w-]+)\s", line)
        if m:
            cur = m.group(1)
            passes[cur] = set()
            continue
        m = re.match(r"^        --([\w-]+)", line)
        if m and cur:
            passes[cur].add(m.group(1))
    return passes


def test_pass_bindings_exist_in_the_built_tool():
    passes = _opt_help()
    bad = []
    for o in options.registry().values():
        for b in o.binds:
            if isinstance(b, options.PassSwitch) and b.pass_name not in passes:
                bad.append((o.name, str(b)))
            if isinstance(b, options.PassOption) and \
                    b.option not in passes.get(b.pass_name, ()):
                bad.append((o.name, str(b)))
    assert not bad, bad


# ---------------------------------------------------------------- (6) settings.json

def _flow(tmp_path, extra=None):
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
           "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}, **(extra or {})}
    p = tmp_path / "flow.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def test_settings_json_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCT_FLOW_PREFIX2_ADDERS", "56")
    flow = _flow(tmp_path, {"options": {"syn.adders.cells.min_width": 14},
                            "shift_select": {"early": True, "direct_decode": True,
                                             "select_max_fanout": 8},
                            "vt_swap": {"recover_order": "leak"}})
    cfg = FlowConfig.load(flow, tmp_path / "out", settings={"syn.fanout.max": 12})
    S = cfg.settings
    assert S.source("syn.fanout.max") == "argument"
    assert S.source("syn.adders.cells.min_width") == "recipe"
    assert S.source("syn.adders.prefix2.min_width") == "env"
    assert S["syn.shifters.select_max_fanout"] == 8
    assert S["syn.vt_swap.recover_order"] == "leak"
    path = provenance_settings.write(cfg, "test")
    rec = provenance_settings.read(path)
    assert rec["options"]["syn.fanout.max"] == {"value": 12, "source": "argument"}
    assert rec["legacy_env_used"] == {"CIRCT_FLOW_PREFIX2_ADDERS": "56"}
    assert set(rec["options"]) == set(options.registry())
    # replay: the record's options as arguments, no environment, same values
    monkeypatch.delenv("CIRCT_FLOW_PREFIX2_ADDERS")
    d2 = tmp_path / "replay"
    d2.mkdir()
    cfg2 = FlowConfig.load(_flow(d2), tmp_path / "out2",
                           settings=provenance_settings.explicit(rec))
    rec2 = provenance_settings.record(cfg2)
    reg = options.registry()
    # record-only options (syn.shift_select: the section as written) are not
    # replayed; what they set is (syn.shifters.*)
    assert {k: v["value"] for k, v in rec2["options"].items() if reg[k].settable} == \
        {k: v["value"] for k, v in rec["options"].items() if reg[k].settable}


def test_set_overlays_a_yaml_section_key(tmp_path):
    flow = _flow(tmp_path, {"vt_swap": {"recover_order": "inst"}})
    cfg = FlowConfig.load(flow, tmp_path / "o",
                          settings={"syn.vt_swap.recover_order": "leak"})
    assert cfg.design.vt_swap.recover_order == "leak"
    with pytest.raises(Exception, match="recover_order"):
        FlowConfig.load(flow, tmp_path / "o",
                        settings={"syn.vt_swap.recover_order": "fastest"})


def test_cli_lists_options_and_strict(tmp_path, capsys, monkeypatch):
    from circt_flow import cli
    assert cli.main(["options", "syn.adders.prefix2"]) == 0
    out = capsys.readouterr().out
    assert "syn.adders.prefix2.boundary" in out and "CIRCT_FLOW_PREFIX2_BOUNDARY" in out
    monkeypatch.setenv("CIRCT_FLOW_ADDER_CELLS", "14")
    flow = _flow(tmp_path)
    rc = cli.main([str(flow), str(tmp_path / "o"), "--strict"])
    assert rc == 1 and "--strict" in capsys.readouterr().err


# ---------------------------------------------------------------- (7) doc

def test_doc_options_md_is_generated():
    doc = ROOT / "doc" / "options.md"
    if not doc.is_file():
        pytest.skip("no doc/ in this tree (the image ships scripts/ only)")
    assert doc.read_text() == options.render_markdown(), \
        "doc/options.md is stale: python3 -m circt_flow options --markdown > doc/options.md"


def test_every_option_declares_help():
    for o in options.registry().values():
        assert o.help, o.name
    assert options.registry()["syn.clock_gating.data_gate"].experimental


# ---------------------------------------------------------------- (8) schedule

def test_legacy_shim_removed_by_its_release():
    """Never skips: CURRENT_RELEASE is a tracked constant the release
    procedure bumps (doc/options.md, "Release procedure")."""
    shim = any(o.legacy_env for o in options.registry().values())
    assert not (shim and options.CURRENT_RELEASE >= options.LEGACY_ENV_REMOVED_IN), (
        f"CURRENT_RELEASE {options.CURRENT_RELEASE} reached LEGACY_ENV_REMOVED_IN="
        f"{options.LEGACY_ENV_REMOVED_IN}: remove the legacy environment shim "
        "(options.py legacy_env, legacy_mode) in this release")
    assert options.FIRST_RELEASE_WITH_REGISTRY + 2 == options.LEGACY_ENV_REMOVED_IN
    # the registry ships in the release after the current one at the latest,
    # and the removal release is still ahead while the shim exists
    assert options.FIRST_RELEASE_WITH_REGISTRY <= options.CURRENT_RELEASE + 1
    assert not shim or options.CURRENT_RELEASE < options.LEGACY_ENV_REMOVED_IN


def test_session_minimal(tmp_path):
    from circt_flow import Session
    s = Session(_flow(tmp_path), tmp_path / "o")
    s.set("syn.adders.cells.min_width", "14")
    assert s.get("syn.adders.cells.min_width") == 14
    assert ("syn.adders.cells.min_width", 14, "argument") == s.report("cells.min_width")[0][:3]
    with pytest.raises(options.OptionError, match="did you mean"):
        s.set("syn.adders.cell.min_width", 14)
    p = s.write_settings(tmp_path / "s.json")
    s2 = Session.from_settings(_flow(tmp_path), tmp_path / "o2", p)
    assert s2.get("syn.adders.cells.min_width") == 14


def test_options_mapping_reaches_a_section_key(tmp_path):
    """A flow.yaml `options:` entry for a section key is the section's key
    (not silently ignored); a pack default fills only a section in use."""
    flow = _flow(tmp_path, {"options": {"syn.vt_swap.recover_order": "leak"}})
    cfg = FlowConfig.load(flow, tmp_path / "o")
    assert cfg.design.vt_swap is not None and cfg.design.vt_swap.recover_order == "leak"
    raw = {"design": {}}
    s = options.resolve(techpack={"syn.vt_swap.margin_ps": 5}, env={})
    assert "vt_swap" not in options.overlay_yaml(raw, s)
    raw = {"vt_swap": {"fix": "sens"}}
    assert options.overlay_yaml(raw, s)["vt_swap"] == {"fix": "sens", "margin_ps": 5}


def test_tech_pack_defaults(tmp_path, mv_pack, monkeypatch):
    """A pack's flow.env (legacy names) and flow.options are techpack-level
    defaults: the flow.yaml and the legacy variable win over them; an
    unknown name in flow.env is an error."""
    text = mv_pack.read_text().replace(
        "default_rail: HI", "flow:\n  env: {CIRCT_FLOW_ADDER_CELLS: '14', "
        "CIRCT_FLOW_NO_CLOCK_GATING: '1'}\n  options: {syn.fanout.max: 7}\n"
        "default_rail: HI")
    pack = tmp_path / "p.yaml"
    pack.write_text(text)
    flow = tmp_path / "flow.yaml"
    flow.write_text(f"design: {{top_module: top, clock_period_ps: 1000}}\n"
                    f"pdk: {{profile: {pack}}}\noptions: {{syn.fanout.max: 5}}\n")
    S = FlowConfig.load(flow, tmp_path / "o").settings
    assert (S["syn.adders.cells.min_width"], S.source("syn.adders.cells.min_width")) == (14, "techpack")
    assert S["syn.clock_gating.enable"] is False
    assert (S["syn.fanout.max"], S.source("syn.fanout.max")) == (5, "recipe")
    monkeypatch.setenv("CIRCT_FLOW_ADDER_CELLS", "12")
    S = FlowConfig.load(flow, tmp_path / "o").settings
    assert (S["syn.adders.cells.min_width"], S.source("syn.adders.cells.min_width")) == (12, "env")
    pack.write_text(text.replace("CIRCT_FLOW_ADDER_CELLS:", "CIRCT_FLOW_ADDER_CELL:"))
    with pytest.raises(options.OptionError, match="did you mean"):
        FlowConfig.load(flow, tmp_path / "o")


def test_settings_diff(tmp_path):
    from circt_flow import cli
    f = _flow(tmp_path)
    a = provenance_settings.write(FlowConfig.load(f, tmp_path / "a"), "t", tmp_path / "a.json")
    b = provenance_settings.write(FlowConfig.load(f, tmp_path / "b",
                                                  settings={"syn.fanout.max": 3}),
                                  "t", tmp_path / "b.json")
    rows = provenance_settings.diff(provenance_settings.read(a), provenance_settings.read(b))
    assert rows == [("syn.fanout.max", (None, "default"), (3, "argument"))]
    assert cli.main(["options", "--diff", str(a), str(b)]) == 1


# ---------------------------------------------------------------- applied, not only recorded

def _value_for(o, tmp_path):
    """A non-default value of option `o` (an invalid one for an untyped key:
    its section must refuse it, or change)."""
    if o.type == "object":
        return "!!bogus!!"
    if o.type == "bool":
        return not o.default
    if o.type == "choice":
        return next(c for c in o.choices if c != o.default)
    if o.type == "int":
        lo = o.range[0] if o.range and o.range[0] is not None else 0
        v = max(lo, 0) + 3
        if o.range and o.range[1] is not None:
            v = min(v, o.range[1])
        return v if v != o.default else v - 1
    if o.type == "float":
        return 0.5
    if o.name == "syn.clock_gating.data_gate":
        return "q@0-3"
    if o.name == "syn.abc.partition.group_scripts":
        f = tmp_path / "gs.json"
        f.write_text('{"g1": "x.abc"}')
        return str(f)
    return "x_1"


# Options whose effect is outside the synthesis stages' command lines and the
# config: the test that shows each one is applied.
_APPLIED_ELSEWHERE = {
    "syn.vt_swap.flop_cap": "test_flop_cap_key.py (vtswap.run reads the option)",
    "post.flop_cap": "test_flop_cap_key.py (postlayout.run_flow reads the option)",
    "syn.library.slow_cell_ratio": "test_slow_cell_ratio_reaches_the_filter",
    # post.* section keys need a pack with vt_flavours: here they are only refused for the
    # lack of one; this one is shown to reach the swap script with such a pack
    "post.recover.block": "test_postlayout.py::test_new_post_layout_options_reach_the_swap_script",
    # acts only with a profile that has dont_use patterns (this check's flow.yaml has none)
    "syn.library.dont_use_structural": "test_dont_use_structural_reaches_every_mapper_command",
    # read by the partitioned mapping stage in Python (the design decides which modules are shared; no tool command
    # carries them): shown to act in test_map_once.py::test_the_stage_maps_shared_modules_once_and_groups_the_rest
    **{n: "test_map_once.py::test_the_stage_maps_shared_modules_once_and_groups_the_rest"
       for n in ("syn.abc.partition.map_once", "syn.abc.partition.map_once_min_instances",
                 "syn.abc.partition.map_cache", "syn.abc.partition.map_cache_verify")},
}


def _fingerprint(cfg, d):
    """The config and the stage command lines, with the load directory `d`
    (config_dir, out dir) replaced, so two loads from different directories
    compare equal when no option changed anything."""
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        cmds = golden_cmdlines.stage_commands(cfg)
    fp = (repr(cfg.design), cfg.partitions, cfg.abc_script_file, cfg.abc_read_lib_args,
          cfg.sv2v_docker_image, cfg.enable_classical_resub, json.dumps(cmds))
    return str(fp).replace(str(d), "<D>")


def _keys_of(o):
    """The words an error about option `o` names: its dotted name, its
    flow.yaml key path and that key, its section."""
    words = {o.name}
    for b in o.binds:
        if isinstance(b, options.YamlKey):
            words |= {".".join((*b.section, b.key)), b.key}
            if b.section:
                words.add(".".join(b.section))
    return words


def _applied_or_refused(name, tmp_path):
    """'applied' (config or stage command lines change), 'refused' (the load
    raises an error naming the option, its key or its section), or
    'ignored' (accepted and nothing changed). Raises on any other error."""
    o = options.registry()[name]
    req = dict(o.requires)
    parts = req.pop("_partitions", None)

    def load(extra, d):
        d.mkdir()
        doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
               "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}
        if req or extra:
            doc["options"] = {**req, **extra}
        (d / "f.yaml").write_text(yaml.safe_dump(doc))
        return FlowConfig.load(d / "f.yaml", d / "o", partitions=parts,
                               circt_abc_opt="/BIN/opt", circt_opt="/BIN/copt",
                               abc_bin="/BIN/abc")
    base = _fingerprint(load({}, tmp_path / "base"), tmp_path / "base")
    value = _value_for(o, tmp_path)
    try:
        cfg = load({name: value}, tmp_path / "set")
    except Exception as e:
        msg = str(e)
        if any(w in msg for w in _keys_of(o)):
            return "refused"
        raise AssertionError(f"{name}: an error that does not name it: {e!r}")
    assert o.settable, f"{name} is pack-only but was accepted"
    assert cfg.settings[name] == value or o.type == "object"
    return "applied" if _fingerprint(cfg, tmp_path / "set") != base else "ignored"


@pytest.mark.parametrize("name", sorted(options.registry()))
def test_every_option_set_is_applied_or_refused(tmp_path, name):
    """Set each registered option to a non-default value through a flow.yaml
    `options:` entry (with what it needs, Option.requires): the load must
    refuse it with an error naming it (pack-only, invalid value, a section
    that needs another key) or the config / the stage command lines must
    change. Nothing may be recorded as set and then ignored."""
    if name in _APPLIED_ELSEWHERE:
        pytest.skip(_APPLIED_ELSEWHERE[name])
    assert _applied_or_refused(name, tmp_path) in ("applied", "refused")


def test_applied_check_catches_an_option_bound_to_nothing(tmp_path, monkeypatch):
    """Self-check of the test above: an option registered with a binding that
    nothing reads is 'ignored' (the test is not vacuous)."""
    reg = dict(options.registry())
    reg["syn.audit.dummy"] = options.Option(
        "syn.audit.dummy", "int", None, "bound to nothing",
        binds=(options.StageParam("nowhere", "nothing"),))
    reg["syn.audit.dummy_obj"] = options.Option(
        "syn.audit.dummy_obj", "object", None, "a key of a section nothing parses",
        binds=(options.YamlKey(("audit_section_nobody_reads",), "k"),))
    monkeypatch.setattr(options, "_REGISTRY", reg)
    monkeypatch.setattr("circt_flow.config.TOP_KEYS",
                        __import__("circt_flow.config").config.TOP_KEYS
                        | {"audit_section_nobody_reads"})
    for d in "abc":
        (tmp_path / d).mkdir()
    assert _applied_or_refused("syn.audit.dummy", tmp_path / "a") == "ignored"
    assert _applied_or_refused("syn.audit.dummy_obj", tmp_path / "b") == "ignored"
    # a real option still counts as applied under the same check
    assert _applied_or_refused("syn.fanout.max", tmp_path / "c") == "applied"


def test_integral_values_keep_their_written_form(tmp_path):
    """pnr.utilization: 70 through options: and through a settings replay
    stays 70 (the P&R command line text), not 70.0."""
    f = _flow(tmp_path, {"options": {"pnr.utilization": 70, "pnr.clock_period": 1},
                         "pnr": {"clock_port": "clk"}})
    cfg = FlowConfig.load(f, tmp_path / "o")
    assert cfg.design.pnr["utilization"] == 70 and isinstance(cfg.design.pnr["utilization"], int)
    assert isinstance(cfg.design.pnr["clock_period"], int)
    rec = json.loads(json.dumps(provenance_settings.record(cfg)))
    d2 = tmp_path / "r"
    d2.mkdir()
    cfg2 = FlowConfig.load(_flow(d2, {"pnr": {"clock_port": "clk"}}), tmp_path / "o2",
                           settings=provenance_settings.explicit(rec))
    assert repr(cfg2.design.pnr["utilization"]) == "70"
    assert repr(cfg2.design.pnr["clock_period"]) == "1"
    s = options.resolve(argument={"pnr.place_density": "0.6"}, env={})
    assert s["pnr.place_density"] == 0.6


def test_pack_only_options_are_refused(tmp_path):
    for n, o in options.registry().items():
        if not o.settable:
            with pytest.raises(options.OptionError, match="only recorded"):
                options.resolve(argument={n: 1}, env={})
    with pytest.raises(options.OptionError, match="only recorded"):
        FlowConfig.load(_flow(tmp_path, {"options": {"flow.pack.partitions": 6}}),
                        tmp_path / "o")


def test_slow_cell_ratio_reaches_the_filter(monkeypatch):
    from circt_flow import config, slowcells
    seen = {}
    monkeypatch.setattr(slowcells, "parse_spec", lambda d, w: None)

    class D:
        profile = None
    s = options.resolve(argument={"syn.library.slow_cell_ratio": 3.1}, env={})
    assert config._slow_cell_names(D(), {}, s) == []      # no profile: off
    import circt_flow.profile as prof_mod
    monkeypatch.setattr(slowcells, "slow_cells",
                        lambda libs, spec, fre: seen.setdefault("spec", spec) and ["X"])

    class P:
        def flavours_at(self, r):
            return None
    monkeypatch.setattr(prof_mod, "load", lambda p: P())
    import circt_flow.pipeline as pl
    monkeypatch.setattr(pl, "setup_libs", lambda d: [])

    class D2:
        profile, rail = "p", None
    assert config._slow_cell_names(D2(), {}, s) == ["X"]
    assert seen["spec"]["ratio"] == 3.1


# ---------------------------------------------------------------- edge cases

def test_empty_adder_arch_keeps_its_old_meaning():
    s = options.resolve(env={"CIRCT_FLOW_ADDER_ARCH": ""}, warn=False)
    assert s["syn.adders.arch"] == "" and s.source("syn.adders.arch") == "env"


@pytest.mark.parametrize("var,text", [("CIRCT_FLOW_MAX_FANOUT", " 4"), ("CIRCT_FLOW_MAX_FANOUT", "08"),
                                      ("CIRCT_FLOW_SHIFT_SELECT_WIDE", " 1"),
                                      ("CIRCT_FLOW_MUL_CELLS", "1 ")])
def test_legacy_blank_or_leading_zero_is_refused(var, text):
    with pytest.raises(options.OptionError, match="blank|leading zero"):
        options.resolve(env={var: text}, warn=False)


def test_sklansky_spelling_accepted():
    s = options.resolve(argument={"syn.adders.prefix.arch": "SKLANSKY"}, env={})
    assert s["syn.adders.prefix.arch"] == "SKLANSKY"


def test_options_and_section_key_in_one_file_is_an_error(tmp_path):
    f = _flow(tmp_path, {"vt_swap": {"recover_order": "inst"},
                         "options": {"syn.vt_swap.recover_order": "leak"}})
    with pytest.raises(options.OptionError, match="set twice.*vt_swap.recover_order"):
        FlowConfig.load(f, tmp_path / "o")


def test_unknown_top_level_key_is_an_error(tmp_path):
    with pytest.raises(KeyError, match="optons.*did you mean options"):
        FlowConfig.load(_flow(tmp_path, {"optons": {"syn.fanout.max": 3}}), tmp_path / "o")
    FlowConfig.load(_flow(tmp_path, {"abc": {"binary": "x"}}), tmp_path / "o2")   # tolerated


def test_recipe_errors_name_the_file(tmp_path):
    f = _flow(tmp_path, {"options": {"syn.fanout.max": "abc"}})
    with pytest.raises(options.OptionError, match=re.escape(str(f))):
        FlowConfig.load(f, tmp_path / "o")


def test_strict_refuses_a_pack_flow_env(tmp_path, mv_pack):
    text = mv_pack.read_text().replace(
        "default_rail: HI", "flow:\n  env: {CIRCT_FLOW_ADDER_CELLS: '14'}\ndefault_rail: HI")
    pack = tmp_path / "p.yaml"
    pack.write_text(text)
    flow = tmp_path / "flow.yaml"
    flow.write_text(f"design: {{top_module: top, clock_period_ps: 1000}}\npdk: {{profile: {pack}}}\n")
    with pytest.raises(options.OptionError, match="--strict.*flow.env"):
        FlowConfig.load(flow, tmp_path / "o", strict=True)


def test_settings_record_has_recipe_hashes_and_root_free_diff(tmp_path):
    f = _flow(tmp_path)
    rec = provenance_settings.record(FlowConfig.load(f, tmp_path / "o"))
    assert rec["recipe_files"]["flow_yaml"]["sha256"]
    other = json.loads(json.dumps(rec))
    other["tools"]["circt_abc"]["root"] = "/elsewhere"
    other["tools"]["circt_abc_opt"]["path"] = "/elsewhere/bin"
    assert provenance_settings.diff(rec, other) == []
    other["recipe_files"]["flow_yaml"]["sha256"] = "0"
    assert provenance_settings.diff(rec, other)[0][0] == "recipe_files.flow_yaml"


def test_power_sub_runs_get_the_options_mapping(tmp_path, monkeypatch):
    """A flow.yaml `options:` mapping reaches the power flow's domain
    sub-runs (through their generated flow.yaml)."""
    from circt_flow import pipeline, power
    parent = FlowConfig.load(_flow(tmp_path, {"options": {"syn.fanout.max": 5}}),
                             tmp_path / "o")
    sub_yaml = tmp_path / "sub" / "flow.yaml"
    sub_yaml.parent.mkdir()
    sub_yaml.write_text("# Generated by circt_flow.power: test\n" + yaml.safe_dump(
        {"design": {"top_module": "top", "clock_period_ps": 1000},
         "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}))
    seen = {}
    monkeypatch.setattr(pipeline, "run", lambda cfg, **k: seen.setdefault("cfg", cfg))
    power._sub_run(parent, sub_yaml, tmp_path / "subout")
    S = seen["cfg"].settings
    assert (S["syn.fanout.max"], S.source("syn.fanout.max")) == (5, "recipe")
    assert sub_yaml.read_text().startswith("# Generated by circt_flow.power")


# ---------------------------------------------------------------- window form, block recovery

_WIN_ON = {"syn.shifters.early": True, "syn.shifters.direct_decode": True,
           "syn.shifters.form": "window"}


@pytest.mark.parametrize("opts,pat", [
    ({"syn.shifters.early": True, "syn.shifters.direct_decode": True,
      "syn.shifters.window_count": 5}, "window_count: window-form option"),
    ({"syn.shifters.early": True, "syn.shifters.form": "window"}, "direct_decode"),
    ({"syn.shifters.direct_decode": True, "syn.shifters.form": "window"}, "the pass"),
    ({**_WIN_ON, "syn.shifters.window_direct": 45}, "window_direct: need syn.shifters.window_general"),
    ({**_WIN_ON, "syn.shifters.window_cuts": "6;14"}, "not an ASCII integer")])
def test_window_form_options_are_refused_when_their_form_is_off(tmp_path, opts, pat):
    """A window_* option without form = window (or the form without the pass
    or direct_decode) is an error, never a recorded no-op."""
    with pytest.raises(options.OptionError, match=pat):
        FlowConfig.load(_flow(tmp_path, {"options": opts}), tmp_path / "o")


@pytest.mark.parametrize("opt,val,pat", [
    ("window_cuts", ",".join(str(i) for i in range(1, 14)), "at most 12 cuts"),
    ("window_cuts", "6,6", "duplicate cut"),
    ("window_cuts", "6,\u0663", "not an ASCII integer"),
    ("window_cuts", "-4", "not an ASCII integer"),
    ("window_types", "1:0-1;x", "not phase:first-last"),
    ("window_types", "0:5-4", "first > last"),
    ("window_types", "0:\u0661-4", "not phase:first-last"),
    ("window_types", ";", "expected")])
def test_window_lists_are_validated(tmp_path, opt, val, pat):
    """window_cuts / window_types: a malformed, duplicated, too long or
    non-ASCII list is refused at option load (the pass refuses too)."""
    opts = {**_WIN_ON, "syn.shifters.window_general": True, f"syn.shifters.{opt}": val}
    with pytest.raises(options.OptionError, match=pat):
        FlowConfig.load(_flow(tmp_path, {"options": opts}), tmp_path / "o")


@pytest.mark.parametrize("extra,frag", [
    ({}, "window-form=true"),
    ({"syn.shifters.window_count": 5, "syn.shifters.window_clip": True},
     "window-form=true window-count=5 window-clip=true"),
    ({"syn.shifters.window_cuts": "6,14"}, "window-cuts=6,14"),
    ({"syn.shifters.window_general": True, "syn.shifters.window_types": "1:0-1;0:2-3",
      "syn.shifters.window_direct": 45},
     "window-general=true window-types=1:0-1;0:2-3 window-direct=45")])
@pytest.mark.parametrize("via", ["set", "options", "section"])
def test_window_form_reaches_the_pass_command_line(tmp_path, extra, frag, via):
    """syn.shifters.form = window and its knobs, by --set, an options: entry
    or the shift_select: section, end up on the map-shift-select command
    line of the early position (and nowhere without them)."""
    opts = {**_WIN_ON, **extra}
    if via == "section":
        doc = {"shift_select": {k.split(".")[-1]: v for k, v in opts.items()
                                if k != "syn.shifters.early"}}
        cfg = FlowConfig.load(_flow(tmp_path, doc), tmp_path / "o")
    elif via == "options":
        cfg = FlowConfig.load(_flow(tmp_path, {"options": opts}), tmp_path / "o")
    else:
        cfg = FlowConfig.load(_flow(tmp_path), tmp_path / "o", settings=opts)
    with contextlib.redirect_stdout(io.StringIO()):
        cmds = json.dumps(golden_cmdlines.stage_commands(cfg))
    assert frag in cmds
    (tmp_path / "b").mkdir()
    base = FlowConfig.load(_flow(tmp_path / "b", {"options": {
        k: v for k, v in _WIN_ON.items() if k != "syn.shifters.form"}}), tmp_path / "ob")
    with contextlib.redirect_stdout(io.StringIO()):
        assert "window-" not in json.dumps(golden_cmdlines.stage_commands(base))


def test_window_form_reaches_power_sub_runs(tmp_path, monkeypatch):
    """The window form travels to a power flow's domain sub-runs (each maps
    its own shifters) through the options: mapping, and is on their pass
    command line. (The post-layout options are not per domain: the
    post-layout step runs once on the P&R result.)"""
    from circt_flow import pipeline, power
    opts = {**_WIN_ON, "syn.shifters.window_count": 5}
    parent = FlowConfig.load(_flow(tmp_path, {"options": opts}), tmp_path / "o")
    sub_yaml = tmp_path / "sub" / "flow.yaml"
    sub_yaml.parent.mkdir()
    sub_yaml.write_text("# Generated by circt_flow.power: test\n" + yaml.safe_dump(
        {"design": {"top_module": "top", "clock_period_ps": 1000},
         "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}))
    seen = {}
    monkeypatch.setattr(pipeline, "run", lambda cfg, **k: seen.setdefault("cfg", cfg))
    power._sub_run(parent, sub_yaml, tmp_path / "subout")
    S = seen["cfg"].settings
    for k, v in opts.items():
        assert (S[k], S.source(k)) == (v, "recipe"), k
    with contextlib.redirect_stdout(io.StringIO()):
        assert "window-form=true window-count=5" in json.dumps(
            golden_cmdlines.stage_commands(seen["cfg"]))


def test_dont_use_structural_reaches_every_mapper_command(tmp_path):
    """syn.library.dont_use_structural: the profile's dont_use patterns go to every circt-abc-opt command as the
    tool-wide --liberty-dont-use; without the option, or without patterns, no command changes."""
    def cmds(on, pats):
        d = tmp_path / f"{int(on)}{len(pats)}"
        d.mkdir()
        doc = {"design": {"top_module": "top", "clock_period_ps": 1000}, "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}
        if on:
            doc["options"] = {"syn.library.dont_use_structural": True}
        (d / "f.yaml").write_text(yaml.safe_dump(doc))
        cfg = FlowConfig.load(d / "f.yaml", d / "o", circt_abc_opt="/BIN/opt", circt_opt="/BIN/copt", abc_bin="/BIN/abc")
        object.__setattr__(cfg.design, "dont_use", list(pats))      # as a profile with dont_use patterns gives it
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            return [[a.replace(str(d), "<D>") for a in c] for c in golden_cmdlines.stage_commands(cfg)]
    base = cmds(False, ["TINV*", "TBUF_X1"])
    assert cmds(True, []) == cmds(False, [])
    got = cmds(True, ["TINV*", "TBUF_X1"])
    tool = [c for c in got if c[0] == "/BIN/opt"]
    assert tool and all(c[1] == "--liberty-dont-use=TINV*,TBUF_X1" for c in tool)
    assert [[a for a in c if not a.startswith("--liberty-dont-use")] for c in got] == base
    with pytest.raises(ValueError, match="comma or a blank"):
        cmds(True, ["A,B"])


def test_the_pipeline_editor_keeps_liberty_dont_use_with_the_tool(tmp_path):
    """--liberty-dont-use is a flag of the tool, not a pass: a command split at a pass hook keeps it in every part."""
    from circt_flow import pipeline_edit as pe
    argv = ["/BIN/opt", "--liberty-dont-use=TINV*", "--mlir-disable-threading", "--canonicalize", "--cse", "in.mlir", "-o", "out.mlir"]
    c = pe.make_command("x", argv, None, {"/BIN/opt"})
    assert c.editable and [p.arg for p in c.passes] == ["--canonicalize", "--cse"]
    assert c.argv(c.passes[:1], dst="mid.mlir")[:3] == ["/BIN/opt", "--liberty-dont-use=TINV*", "--mlir-disable-threading"]
    assert c.argv(c.passes[1:], src="mid.mlir")[1] == "--liberty-dont-use=TINV*"
