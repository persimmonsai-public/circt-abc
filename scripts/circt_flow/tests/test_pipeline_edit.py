"""The editable pass pipeline and pass hooks:
the data model, every edit and refusal, the record, the golden identity of
the planned command lines with no edits, and (with the tools) a small design
where an inserted / removed pass changes the output and a split-at-pass hook
sees the intermediate IR."""
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(0, str(HERE))

from circt_flow import config, hooks, options, pipeline_edit, stages  # noqa: E402
from circt_flow.pipeline_edit import Pipeline, PipelineError  # noqa: E402
import golden_cmdlines  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(options.LEGACY_PREFIXES):
            monkeypatch.delenv(k)


def _cfg(td: Path, env=None, yaml_extra=None, partitions=None, **kw):
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
           "pdk": {"synth_lib_tt": ["/LIB/a.lib", "/LIB/b.lib"]}, **(yaml_extra or {})}
    (td / "flow.yaml").write_text(yaml.safe_dump(doc))
    args = dict(circt_abc_opt="/BIN/circt-abc-opt", circt_opt="/BIN/circt-opt",
                abc_bin="/BIN/abc", partitions=partitions)
    args.update(kw)
    if env:
        args["settings"] = options.legacy_to_settings(env)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return config.FlowConfig.load(td / "flow.yaml", td / "out", **args)


# a stand-in for the tools' --help listings (the real ones are read when the
# binaries exist)
LISTING = {"/BIN/circt-abc-opt": {
    "canonicalize": set(), "cse": set(), "localize-dynamic-rmw": set(),
    "map-prefix-adders": {"enable-prefix-adders", "liberty", "min-width", "max-width", "arch",
                          "boundary"},
    "set-adder-arch": {"arch"}, "map-adder-chains": set(), "register-mapper": {"liberty"},
    "structural-mapper": {"liberty"}, "narrow-whole-value-reads": set(),
    "abc-bridge": set(), "remove-identity-buffers": {"liberty"},
    "merge-residual-modules": set(), "map-shell-logic": {"liberty"},
    "outline-residual-logic": set(), "prune-unused-residual-outputs": set(),
    "hw-aggregate-to-comb": set()},
    "/BIN/circt-opt": {"canonicalize": set(), "cse": set(), "hw-aggregate-to-comb": set(),
                       "hw-flatten-modules": set(), "export-verilog": set()}}


def _pipe(tmp_path, **kw):
    return Pipeline(cfg=_cfg(tmp_path, **kw), listing=LISTING.get, has_pass=lambda b, p: True)


TWO_PREFIX = {**golden_cmdlines.DIRECT_ENV, **golden_cmdlines.PREFIX2}


# ---------------------------------------------------------------- golden identity

def test_plan_without_edits_is_the_golden_command_lines():
    """No edits: the planned command lines of elaborate + map_datapath +
    syn_map are exactly the golden lines for every case."""
    want = json.loads((HERE / "data" / "golden_cmdlines.json").read_text())
    import tempfile
    for name, (env, yaml_extra) in sorted(golden_cmdlines.CASES.items()):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            env = {k: v.replace("<OUT>", str(td)) for k, v in env.items()}
            yx = dict(yaml_extra)
            parts = yx.pop("_partitions", None)
            if "CIRCT_FLOW_GROUP_SCRIPTS" in env:
                Path(env["CIRCT_FLOW_GROUP_SCRIPTS"]).write_text('{"g1": "x.abc"}')
            cfg = _cfg(td, env, yx, parts)
            p = Pipeline(cfg=cfg, has_pass=lambda b, n: True)
            plans = p.planned()
            got = [c.argv() for st in ("elaborate", "map_datapath", "syn_map")
                   for c in plans[st].commands]
            root = str(SCRIPTS.resolve().parent)
            got = [[a.replace(str(td), "<OUT>").replace(root, "<ROOT>") for a in c] for c in got]
            assert got == want[name], name


def test_no_edits_no_runner(tmp_path):
    """Without edits and hooks, applied() installs nothing (stages._run is
    the plain path)."""
    p = _pipe(tmp_path)
    assert not p.active()
    with p.applied("map_datapath"):
        assert stages.RUNNER is None
    assert stages.RUNNER is None


def test_plan_leaves_no_trace(tmp_path):
    cfg = _cfg(tmp_path, TWO_PREFIX)
    Pipeline(cfg=cfg, has_pass=lambda b, n: True).planned()
    assert not Path(cfg.out_dir).exists()
    assert stages.RUNNER is None and stages.os is os


def test_mapped_back_paths_relative_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "flow.yaml").write_text(yaml.safe_dump(
        {"design": {"top_module": "top", "clock_period_ps": 1000},
         "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}))
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = config.FlowConfig.load("flow.yaml", "rel", circt_abc_opt="/BIN/circt-abc-opt",
                                     circt_opt="/BIN/circt-opt", abc_bin="/BIN/abc")
    c = Pipeline(cfg=cfg, has_pass=lambda b, n: True).planned()["map_datapath"].commands[0]
    assert c.argv()[-3:] == ["rel/top_core_deagg.mlir", "-o", "rel/top_struct.mlir"]


# ---------------------------------------------------------------- data model

def test_refs_and_passes(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    refs = [x["ref"] for x in p.passes("map_datapath")]
    assert "map-prefix-adders#1" in refs and "map-prefix-adders#2" in refs
    assert "register-mapper" in refs and "canonicalize#2" in refs and "canonicalize" not in refs
    assert refs.count("cse#1") == 1 and "cse#2" in refs
    syn = p.passes("syn_map")
    assert [x["command"] for x in syn if x["name"] == "export-verilog"] == [len(
        p.commands("syn_map"))]
    assert p.passes("fanout") == []
    txt = p.text()
    assert "stage map_datapath" in txt and "map-prefix-adders#2" in txt
    assert "edits: 0" in txt


def test_pass_spec_forms():
    e = pipeline_edit.pass_spec("--map-prefix-adders=min-width=56 arch=X", {"boundary": True})
    assert e.arg == "--map-prefix-adders=min-width=56 arch=X boundary=true"
    assert pipeline_edit.pass_spec("cse").arg == "--cse"
    assert pipeline_edit.pass_spec("x", {"max_width": 64}).arg == "--x=max-width=64"
    for bad in ("", None, "1abc", "a b"):
        with pytest.raises(PipelineError):
            pipeline_edit.pass_spec(bad)


def test_parse_help():
    text = ("Available Passes:\n      --canonicalize   -   Canonicalize\n"
            "        --max-iterations=<long> - x\n      --cse\n"
            "      --map-prefix-adders  - y\n        --min-width=<uint> - z\n")
    assert pipeline_edit.parse_help(text) == {
        "canonicalize": {"max-iterations"}, "cse": set(), "map-prefix-adders": {"min-width"}}


# ---------------------------------------------------------------- edits

def _md(p):
    return p.planned()["map_datapath"].commands[0].argv()


def test_insert_after_before_and_end(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    base = _md(p)
    p.insert("map_datapath", "canonicalize", after="map-prefix-adders#2")
    a = _md(p)
    i = a.index(next(x for x in a if x.endswith("isolate-inputs=true")))
    assert a[i + 1] == "--canonicalize" and len(a) == len(base) + 1
    p.insert("map_datapath", "cse", before="register-mapper")
    a = _md(p)
    assert a[a.index("--register-mapper=liberty=/LIB/a.lib") - 1] == "--cse"
    p.insert("map_datapath", "set-adder-arch", arch="SKLANSKY")
    a = _md(p)
    assert a[a.index("-o") - 2] == "--set-adder-arch=arch=SKLANSKY"
    assert [e["op"] for e in p.edits] == ["insert"] * 3
    assert p.edits[0] == {"op": "insert", "stage": "map_datapath", "pass": "canonicalize",
                          "after": "map-prefix-adders#2"}
    assert "+ canonicalize" in p.text()


def test_remove_replace_move(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    p.remove("map_datapath", "map-prefix-adders#2")
    assert not any("isolate-inputs=true" in a for a in _md(p))
    p.replace("map_datapath", "set-adder-arch", "set-adder-arch", arch="KOGGE-STONE")
    assert "--set-adder-arch=arch=KOGGE-STONE" in _md(p)
    p.move("map_datapath", "narrow-whole-value-reads", before="structural-mapper")
    a = _md(p)
    assert a.index("--narrow-whole-value-reads") + 1 == a.index(
        "--structural-mapper=liberty=/LIB/a.lib")
    # refs after the edits: the inserted/moved passes have references too
    assert "narrow-whole-value-reads" in [x["ref"] for x in p.passes("map_datapath")]
    p.clear()
    assert p.edits == [] and not p.active()


def test_move_across_commands_same_tool(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    p.move("syn_map", "outline-residual-logic", after="prune-unused-residual-outputs")
    cmds = p.commands("syn_map")
    assert not any("--outline-residual-logic" in c for c in cmds[:2])
    assert "--outline-residual-logic" in cmds[2]


def test_edits_are_sequential(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    p.insert("map_datapath", "set-adder-arch", before="register-mapper", arch="X")
    # set-adder-arch now occurs twice: the bare name is ambiguous
    with pytest.raises(PipelineError, match="ambiguous"):
        p.remove("map_datapath", "set-adder-arch")
    p.remove("map_datapath", "set-adder-arch#1")    # the inserted one (comes first)
    assert len(p.edits) == 2
    assert "--set-adder-arch=arch=X" not in _md(p) and "--set-adder-arch=arch=BRENT-KUNG" in _md(p)


@pytest.mark.parametrize("call, match", [
    (lambda p: p.insert("nosuch", "cse"), "unknown stage"),
    (lambda p: p.insert("map_datapath", "cse", after="nosuch-pass"), "has no pass"),
    (lambda p: p.insert("map_datapath", "cse", after="map-prefix-adders"), "ambiguous"),
    (lambda p: p.insert("map_datapath", "cse", after="map-prefix-adders#3"), "has 2"),
    (lambda p: p.insert("map_datapath", "cse", after="map-prefix-adders#0"), "has 2"),
    (lambda p: p.insert("map_datapath", "cse", after="cse#1", before="cse#2"), "not both"),
    (lambda p: p.insert("map_datapath", "no-such-pass"), "unknown pass"),
    (lambda p: p.insert("map_datapath", "map-prefix-adders", min_widht=3), "no option"),
    (lambda p: p.insert("syn_map", "export-verilog", after="canonicalize#1"),
     "not a pass of /BIN/circt-abc-opt"),
    (lambda p: p.insert("fanout", "cse"), "no editable pass list"),
    (lambda p: p.remove("syn_map", "abc-bridge"), "cannot be removed"),
    (lambda p: p.remove("syn_map", "export-verilog"), "cannot be removed"),
    (lambda p: p.replace("syn_map", "abc-bridge", "cse"), "cannot be replaced"),
    (lambda p: p.move("syn_map", "export-verilog", before="hw-flatten-modules#1"),
     "cannot leave its command"),
    (lambda p: p.move("map_datapath", "register-mapper"), "needs after= or before="),
    (lambda p: p.move("map_datapath", "register-mapper", after="register-mapper"),
     "its own anchor"),
    (lambda p: p.remove("map_datapath", None), "reference"),
])
def test_refusals(tmp_path, call, match):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    with pytest.raises(PipelineError, match=match):
        call(p)
    assert p.edits == []           # a refused edit is not recorded


def test_load_edits_all_or_nothing(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    good = {"op": "remove", "stage": "map_datapath", "ref": "map-prefix-adders#2"}
    p.load_edits([good])
    assert p.edits == [good]
    with pytest.raises(PipelineError, match="edit 2"):
        p.load_edits([good, {"op": "remove", "stage": "map_datapath", "ref": "zzz"}])
    assert p.edits == [good]
    with pytest.raises(PipelineError, match="unknown pipeline edit"):
        p.load_edits([{"op": "swap", "stage": "map_datapath"}])


def test_edit_refused_when_config_changes(tmp_path):
    """An edit valid for one configuration is refused before any tool runs
    when the run's configuration no longer has its anchor."""
    p = _pipe(tmp_path, env=TWO_PREFIX)
    p.remove("map_datapath", "map-prefix-adders#2")
    other = _cfg(tmp_path, golden_cmdlines.DIRECT_ENV)        # no second prefix-adder call
    with pytest.raises(PipelineError, match="map-prefix-adders#2"):
        with p.applied("elaborate", other):
            pytest.fail("the stage body must not run")


def test_done_stage_refuses_edits(tmp_path):
    class S:
        _run_cfg = _cfg(tmp_path, TWO_PREFIX)

        def done(self, st):
            return st == "map_datapath"
    p = Pipeline(S(), listing=LISTING.get, has_pass=lambda b, n: True)
    with pytest.raises(PipelineError, match="already run"):
        p.remove("map_datapath", "map-prefix-adders#2")
    p.remove("syn_map", "remove-identity-buffers")


# ---------------------------------------------------------------- application

def _fake_run(monkeypatch):
    ran = []

    def plain(cmd, stdout=None, env=None):
        ran.append(([str(c) for c in cmd], None if stdout is None else str(stdout)))
        stages.EXECUTED.append([str(c) for c in cmd])
    monkeypatch.setattr(stages, "_run_plain", plain)
    return ran


def test_applied_runs_edited_commands(tmp_path, monkeypatch):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    cfg = p._cfg()
    p.remove("map_datapath", "map-prefix-adders#2")
    ran = _fake_run(monkeypatch)
    monkeypatch.setattr(stages, "_binary_has_pass", lambda b, n: True)
    with p.applied("map_datapath", cfg):
        stages._run(["/usr/bin/true", "x"])           # not an opt tool: unchanged
        stages.structural_mapping(cfg)
    assert ran[0][0] == ["/usr/bin/true", "x"]
    assert not any("isolate-inputs=true" in a for a in ran[1][0])
    assert ran[1][0] == p.planned()["map_datapath"].commands[0].argv()
    assert stages.RUNNER is None


def test_applied_refuses_unplanned_command(tmp_path, monkeypatch):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    cfg = p._cfg()
    p.remove("map_datapath", "map-prefix-adders#2")
    ran = _fake_run(monkeypatch)
    with pytest.raises(PipelineError, match="does not have"):
        with p.applied("map_datapath", cfg):
            stages._run(["/BIN/circt-abc-opt", "--cse", "a.mlir", "-o", "b.mlir"])
    assert ran == [] and stages.RUNNER is None
    with pytest.raises(PipelineError, match="did not run"):
        with p.applied("map_datapath", cfg):
            pass


def test_pass_hook_splits_the_command(tmp_path, monkeypatch):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    cfg = p._cfg()
    seen = []

    def look(s, ir):
        seen.append(Path(ir).name)
    p.hook("map_datapath", "register-mapper", look, after=True)
    ran = _fake_run(monkeypatch)
    monkeypatch.setattr(stages, "_binary_has_pass", lambda b, n: True)
    full = p.planned()["map_datapath"].commands[0].argv()
    with p.applied("map_datapath", cfg):
        stages.structural_mapping(cfg)
    assert len(ran) == 2
    a, b = ran[0][0], ran[1][0]
    k = full.index("--register-mapper=liberty=/LIB/a.lib")
    assert a[:k + 1] == full[:k + 1] and a[-2] == "-o"
    mid = a[-1]
    assert b[0] == full[0] and b[1:len(full) - k - 3] == full[k + 1:-3]
    assert b[-3:] == [mid, "-o", str(cfg.struct_mlir)]
    assert seen == [Path(mid).name] and "after_register-mapper" in mid
    assert any(e[0] == "(post_pass)" for e in stages.EXECUTED)


def test_hook_at_boundaries_and_stdout(tmp_path):
    """plan_split: a pre-hook on the first pass copies the input; a
    post-hook on the last pass copies to the output; stdout goes to the
    last piece that runs passes."""
    h = hooks.PassHook("s", "x", lambda s, i: None, False)
    segs = hooks.plan_split(2, {0: [h], 2: [h]}, "in", "out", "net.v", lambda b: f"t{b}")
    assert [(s.lo, s.hi, s.src, s.dst, s.stdout) for s in segs] == [
        (0, 0, "in", "t0", None), (0, 2, "t0", "t2", "net.v"), (2, 2, "t2", "out", None)]


def test_hook_refusals(tmp_path):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    with pytest.raises(PipelineError, match="callable"):
        p.hook("map_datapath", "cse#1", "nope", after=True)
    with pytest.raises(PipelineError, match="unknown stage"):
        p.hook("map_datapth", "cse#1", print, after=True)
    with pytest.raises(PipelineError, match="has no pass"):
        p.hook("map_datapath", "nosuch", print, after=True)
    assert p.pass_hooks == []


def test_power_design_refuses(tmp_path, monkeypatch):
    p = _pipe(tmp_path, env=TWO_PREFIX)
    cfg = p._cfg()
    import dataclasses
    pw = dataclasses.replace(cfg, design=dataclasses.replace(cfg.design, power=object()))
    p2 = Pipeline(cfg=pw, listing=LISTING.get, has_pass=lambda b, n: True)
    monkeypatch.setattr(pipeline_edit, "calibrated", lambda c: c)
    with pytest.raises(PipelineError, match="power domains"):
        p2.remove("syn_map", "remove-identity-buffers")


# ---------------------------------------------------------------- record

def _hookfn(s, ir):
    return None


def test_run_record(tmp_path):
    class H:
        _pre = [("syn_map", _hookfn)]
        _post = []

    class S:
        hooks = H()
        _pipe = _pipe(tmp_path, env=TWO_PREFIX)
    assert pipeline_edit.run_record(type("E", (), {"hooks": None, "_pipe": None})()) == {}
    S._pipe.remove("map_datapath", "map-prefix-adders#2")
    S._pipe.hook("map_datapath", "register-mapper", _hookfn, after=False)
    rec = pipeline_edit.run_record(S())
    assert rec["pipeline_edits"] == [{"op": "remove", "stage": "map_datapath",
                                      "ref": "map-prefix-adders#2"}]
    hk = rec["hooks"]
    assert hk["reproducible_from_settings"] is False
    assert [(e["kind"], e["stage"], e["name"]) for e in hk["registered"]] == [
        ("pre", "syn_map", "_hookfn"), ("pre_pass", "map_datapath", "_hookfn")]
    assert hk["registered"][0]["file"] == __file__
    assert hk["registered"][0]["line"] == _hookfn.__code__.co_firstlineno
    json.dumps(rec)
    # replayable: the edits load into a fresh pipeline
    p2 = _pipe(tmp_path, env=TWO_PREFIX)
    p2.load_edits(rec["pipeline_edits"])
    assert p2.commands("map_datapath") == S._pipe.commands("map_datapath")


# ---------------------------------------------------------------- functional (tools)

def _tool(env, *fallback):
    for c in (os.environ.get(env), *fallback):
        if c and Path(c).is_file():
            return c
    return None


ABC_OPT = _tool("CIRCT_ABC_OPT", str(SCRIPTS.parent / "mapper/build/bin/circt-abc-opt"))
CIRCT_OPT = _tool("CIRCT_OPT", str(SCRIPTS.parent / "third_party/circt/build/bin/circt-opt"))

SMALL = """hw.module @top(in %a : i8, in %b : i8, out y : i8) {
  %c0 = hw.constant 0 : i8
  %0 = comb.add %a, %c0 : i8
  %1 = comb.xor %0, %b : i8
  %2 = comb.xor %1, %b : i8
  hw.output %2 : i8
}
"""


@pytest.mark.skipif(not (ABC_OPT and CIRCT_OPT), reason="needs circt-abc-opt and circt-opt")
def test_functional_edit_and_hook(tmp_path):
    """elaborate's de-aggregation command on a small design, run for real
    through the seam: removing canonicalize leaves the redundant ops in the
    output, inserting it back (elsewhere) removes them again, and a post-pass
    hook on hw-aggregate-to-comb sees the intermediate IR (not yet
    canonicalized)."""
    cfg = _cfg(tmp_path, circt_abc_opt=ABC_OPT, circt_opt=CIRCT_OPT)
    out = Path(cfg.out_dir)
    out.mkdir(parents=True)

    def run(p):
        Path(cfg.core_mlir_raw).write_text(SMALL)
        stages.EXECUTED.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            with p.applied("elaborate", cfg):
                stages.hw_aggregate_to_comb(cfg)
        return Path(cfg.core_mlir).read_text()

    def ops(text):
        return sum(text.count(o) for o in ("comb.add", "comb.xor"))

    base = run(Pipeline(cfg=cfg))
    assert ops(base) == 0                      # canonicalize folds a+0 and x^b^b
    p = Pipeline(cfg=cfg)
    p.remove("elaborate", "canonicalize")
    removed = run(p)
    assert ops(removed) == 3 and removed != base
    p.insert("elaborate", "canonicalize", after="cse")
    assert run(p) == base
    p.clear()
    seen = {}

    def look(s, ir):
        seen["text"] = Path(ir).read_text()
        seen["path"] = ir
    p.hook("elaborate", "hw-aggregate-to-comb", look, after=True)
    assert run(p) == base
    assert ops(seen["text"]) == 3 and Path(seen["path"]).parent == out / "pipeline"
    split = [c for c in stages.EXECUTED if c and c[0] == CIRCT_OPT]
    assert len(split) == 2
    assert split[0][1:2] == ["--hw-aggregate-to-comb"]
    assert split[1][1:3] == ["--canonicalize", "--cse"]
    # an unknown pass for the real tool is refused from its own listing
    with pytest.raises(PipelineError, match="unknown pass"):
        p.insert("elaborate", "no-such-pass-xyz")
    with pytest.raises(PipelineError, match="no option"):
        p.insert("elaborate", "canonicalize", no_such_option=1)
