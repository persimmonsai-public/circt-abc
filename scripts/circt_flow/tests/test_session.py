"""The Python session API: the attribute database,
stages one at a time with result objects, run_pass, reports, resumability,
the settings record, and identity with the command-line flow.

The stages run for real (pipeline.py, stages.py) with the external tools
replaced by a stand-in for stages._run that writes each command's output
file; no circt / ABC / OpenSTA binary is needed.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from conftest import write_lib
from circt_flow import Session, options, pipeline, stages, provenance_settings
from circt_flow.db import OptionAttributeError, node_class, stub_text
from circt_flow.session import SessionError, ScopedSetting, stage_of

NETLIST = ("module top (a, y);\n  input wire a;\n  output wire y;\n  wire n;\n"
           "  TSTINV_X1 u0 (.A(a), .Y(n));\n  TSTINV_X1 u1 (.A(n), .Y(y));\n"
           "  TSTNAND_X1 u2 (.A(a), .B(n), .Y());\nendmodule\n")


def fake_run(log):
    def run(cmd, stdout=None, env=None):
        cmd = [str(c) for c in cmd]
        log.append(cmd)
        stages.EXECUTED.append(cmd + ([">", str(stdout)] if stdout is not None else []))
        if stdout is not None:
            Path(stdout).write_text(NETLIST)
        for i, a in enumerate(cmd):
            if a == "-o" and cmd[i + 1] != "/dev/null":
                Path(cmd[i + 1]).write_text("// " + " ".join(Path(x).name for x in cmd[:2]) + "\n"
                                            "hw.module @top() {}\n")
            if a.startswith("--write="):
                Path(a.split("=", 1)[1]).write_text(NETLIST)
    return run


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """A flow.yaml starting from a given core IR, two invented liberties,
    tools replaced by fake_run. Returns (flow.yaml, log of commands)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    a = write_lib(lib / "a.lib", "tst_a", ["TSTINV_X1"])
    b = write_lib(lib / "b.lib", "tst_b", ["TSTNAND_X1"])
    b.write_text(b.read_text().replace("area : 1.0", "area : 2.5"))
    core = tmp_path / "core_in.mlir"
    core.write_text("hw.module @top() {\n  %r = seq.firreg %d clock %c : i8\n}\n")
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000, "core_mlir": str(core)},
           "pdk": {"synth_lib_tt": [str(a), str(b)]}}
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump(doc))
    log = []
    # the tool stand-in sits under stages._run's RUNNER seam (the pipeline's dry
    # runs and pass hooks go through it)
    monkeypatch.setattr(stages, "_run_plain" if hasattr(stages, "_run_plain") else "_run",
                        fake_run(log))
    monkeypatch.setattr(stages, "_binary_has_pass", lambda b, p: True)
    monkeypatch.setattr("circt_flow.config.FlowConfig.validate_tools", lambda self: None)
    monkeypatch.setenv("SV2V", "/BIN/sv2v")
    for k in list(os.environ):
        if k.startswith(("CIRCT_FLOW_", "CIRCT_VT_")):
            monkeypatch.delenv(k)
    stages.EXECUTED.clear()
    return f, log


KW = dict(circt_abc_opt="/BIN/circt-abc-opt", circt_opt="/BIN/circt-opt", abc_bin="/BIN/abc")


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def _rel(cmds, *dirs):
    out = json.dumps(cmds)
    for d in dirs:
        out = out.replace(str(d), "<D>")
    return out


# ---------------------------------------------------------------- attribute database

def test_db_attribute_get_set_and_validation(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    assert s.db.syn.fanout.max is None
    s.db.syn.fanout.max = 7
    assert s.db.syn.fanout.max == 7 and s.db.source("syn.fanout.max") == "argument"
    assert s.db.set_at("syn.fanout.max").startswith("script:") and \
        s.db.set_at("syn.fanout.max").endswith(f"test_session.py:{_line_of('s.db.syn.fanout.max = 7')}")
    s.db["syn.adders.cells.min_width"] = "14"        # parsed by type
    assert s.db.syn.adders.cells.min_width == 14
    with pytest.raises(options.OptionError, match="must be an integer"):
        s.db.syn.fanout.max = "many"
    with pytest.raises(options.OptionError, match="did you mean syn.fanout.max"):
        s.db.syn.fanout.maxx = 3
    with pytest.raises(AttributeError):
        s.db.syn.fanoutt
    with pytest.raises(options.OptionError, match="is not one of"):
        s.db.syn.shifters.form = "spiral"
    with pytest.raises(options.OptionError, match="only recorded"):
        s.db["flow.pack.partitions"] = 2
    s.db.reset("syn.fanout")
    assert s.db.syn.fanout.max is None and s.db.source("syn.fanout.max") == "default"
    assert s.db.syn.adders.cells.min_width == 14
    s.db.reset("syn.adders.")
    assert s.db.source("syn.adders.cells.min_width") == "default"


def _line_of(text):
    for i, line in enumerate(Path(__file__).read_text().splitlines(), 1):
        if text in line and "_line_of" not in line:
            return i
    raise AssertionError(text)


def test_db_namespaces_are_generated_and_documented(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    assert set(dir(s.db)) >= {"syn", "cts", "hold", "post", "pnr", "flow", "run"}
    assert "recover_order" in dir(s.db.syn.vt_swap)
    cls = node_class("syn.vt_swap")
    assert cls.__annotations__["margin_ps"] in (int, float, object)
    assert "recover_order" in cls.__doc__
    assert options.registry()["syn.vt_swap.recover_order"].help in cls.recover_order.__doc__
    # every option reachable by attributes, and only options
    for name in options.registry():
        *ns, leaf = name.split(".")
        node = s.db
        for p in ns:
            node = getattr(node, p)
        if leaf in type(node)._children:     # both an option and a namespace
            assert s.db[name] == getattr(node, leaf).get()
        else:
            assert getattr(node, leaf) == s.db[name]
    # a name that is an option and a namespace: assignable
    s.db.syn.clock_gating = True
    assert s.db["syn.clock_gating"] is True


def test_db_help_info_report(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.db.syn.fanout.max = 7
    h = s.db.help("syn.fanout.max")
    assert "syn.fanout.max = 7" in h and "limit-fanout" in h and "stage elaborate" in h
    assert "syn.vt_swap.recover_order" in s.db.help("syn.vt_swap")
    info = s.db.info("syn.fanout.max")
    assert (info.value, info.source, info.type) == (7, "argument", "int")
    rep = s.report_changed()
    assert "syn.fanout.max" in rep.names() and rep.as_dict()["syn.fanout.max"] == 7
    assert all(r.source != "default" for r in rep)
    assert "syn.fanout.max" in str(rep)
    assert len(s.report_settings("syn.vt_swap")) == len(s.db.names("syn.vt_swap"))
    # short form
    assert ("syn.fanout.max", 7, "argument") == s.report("syn.fanout.max")[0][:3]


def test_db_stub_is_generated(tmp_path):
    """doc/db_types.pyi is current (python3 -m circt_flow options --stub)."""
    stub = Path(__file__).resolve().parents[3] / "doc" / "db_types.pyi"
    if not stub.is_file():
        pytest.skip("no doc/ in this tree (the image ships scripts/ only)")
    want = stub.read_text()
    assert stub_text() == want, \
        "regenerate: python3 -m circt_flow options --stub > doc/db_types.pyi"
    compile(want, "db_types.pyi", "exec")


def test_validate_runs_the_cross_option_checks(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.db.syn.shifters.window_count = 5        # needs form = window: refused at validate
    with pytest.raises(options.OptionError, match="need syn.shifters.form = window"):
        s.db.validate()
    with pytest.raises(options.OptionError, match="need syn.shifters.form = window"):
        quiet(s.elaborate)
    assert not s.done("elaborate")


# ---------------------------------------------------------------- stages and results

def test_stages_one_at_a_time_equal_the_full_flow(flow, tmp_path):
    """Golden: the commands and outputs of the stages run one by one are the
    command-line flow's (pipeline.run, as cli.main calls it)."""
    f, log = flow
    from circt_flow.config import FlowConfig
    cfg = FlowConfig.load(f, tmp_path / "cli", settings={"syn.fanout.max": 5}, **KW)
    quiet(pipeline.run, cfg)
    want = _rel(log, tmp_path / "cli")
    want_rec = provenance_settings.read(tmp_path / "cli" / "settings.json")
    log.clear()
    s = Session(f, tmp_path / "ses", settings={"syn.fanout.max": 5}, **KW)
    names = []
    for st in s.stages[:-1]:
        r = quiet(s.run_stage, st, force=False)
        names.append((r.stage, r.status))
    assert names == [("elaborate", "done"), ("map_datapath", "done"), ("syn_map", "done"),
                     ("fanout", "skipped"), ("vt_swap", "skipped"), ("sta", "skipped")]
    assert _rel(log, tmp_path / "ses") == want
    rec = provenance_settings.read(tmp_path / "ses" / "settings.json")
    assert rec["stage"] == "synthesis"
    assert _rel(rec["pipeline"], tmp_path / "ses") == _rel(want_rec["pipeline"], tmp_path / "cli")
    assert {k: v for k, v in rec["options"].items()} == want_rec["options"]
    assert rec["set_at"] == {"syn.fanout.max": "settings"}
    assert "set_at" not in want_rec          # a command-line record is unchanged
    for n in ("openroad_netlist", "netlist"):
        assert (tmp_path / "ses" / "1_synth" / Path(getattr(cfg, n)).name).read_text() == \
            Path(getattr(cfg, n)).read_text()
    # and s.run() is pipeline.run
    log.clear()
    s2 = Session(f, tmp_path / "ses2", settings={"syn.fanout.max": 5}, **KW)
    fr = quiet(s2.run)
    assert _rel(log, tmp_path / "ses2") == want
    assert [r.stage for r in fr.stages] == [n for n, _ in names]


def test_stage_results_carry_metrics_paths_and_settings(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.db.syn.adders.cells.min_width = 14
    r = quiet(s.elaborate)
    assert r.status == "done" and Path(r.produced["core"]).is_file()
    assert r.metrics["registers"] == 0 or "registers" in r.metrics
    assert r.settings == {"syn.adders.cells.min_width": 14}
    assert any("--hw-aggregate-to-comb" in c for c in r.commands)
    quiet(s.map_datapath)
    r = quiet(s.syn_map)
    m = r.metrics
    assert (m["cells"], m["area"]) == (3, 4.5)          # 2 x 1.0 + 2.5 (invented liberties)
    assert m["top_cells"] == {"TSTINV_X1": 2, "TSTNAND_X1": 1}
    assert r.produced["openroad_netlist"].endswith("top_openroad.v")
    assert "cells 3" in str(r) and "area 4.5" in str(r)
    assert s.report_qor()["cells"] == 3
    out = s.write_netlist(tmp_path / "x" / "final.v")
    assert out.read_text() == s.artifacts["openroad_netlist"].read_text()
    rep = s.report_stages()
    assert "syn_map" in rep and "not run" in rep


def test_stage_order_and_refusals(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    with pytest.raises(SessionError, match="needs elaborate run first"):
        s.map_datapath()
    with pytest.raises(SessionError, match="did you mean"):
        s.run_stage("syn_mapp")
    quiet(s.elaborate)
    # an option read by a stage that ran is refused, not ignored
    with pytest.raises(SessionError, match="read by stage elaborate"):
        s.db.syn.fanout.max = 3
    # a later stage's option is still open
    s.db.syn.vt_swap.margin_ps = 50
    s.invalidate("elaborate")
    s.db.syn.fanout.max = 3
    assert s.db.syn.fanout.max == 3


def test_vt_swap_and_sta_named_calls_force_the_stage(flow, tmp_path, monkeypatch):
    f, _ = flow
    from circt_flow import vtswap, sta
    seen = []
    monkeypatch.setattr(vtswap, "run", lambda cfg, spec: seen.append(("vt", cfg.run_vt_swap)))
    monkeypatch.setattr(sta, "run", lambda cfg, spec: seen.append(("sta", cfg.run_sta)))
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.run, stop="sta")                         # not wanted: skipped
    assert seen == [] and s.results["vt_swap"].status == "skipped"
    assert quiet(s.vt_swap).status == "done" and quiet(s.sta).status == "done"
    assert seen == [("vt", True), ("sta", True)]


def test_hooks_before_and_after_stages(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    seen = []
    s.hooks.pre("map_datapath", lambda ses: seen.append(("pre", ses.done("elaborate"))))
    s.hooks.post("*", lambda ses, r: seen.append(("post", r.stage)))
    with pytest.raises(SessionError, match="unknown stage"):
        s.hooks.pre("mapdatapath", print)
    quiet(s.run)
    assert seen[0] == ("post", "elaborate") and ("pre", True) in seen
    assert [x[1] for x in seen if x[0] == "post"] == list(s.stages[:-1])


def test_pipeline_attributes_delegate(flow, tmp_path):
    """s.pipeline, pass hooks and report_passes come from pipeline_edit.py;
    without it they say so (never a silent no-op)."""
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    try:
        import circt_flow.pipeline_edit  # noqa: F401
    except ImportError:
        with pytest.raises(NotImplementedError, match="pipeline_edit"):
            s.report_passes()
        with pytest.raises(NotImplementedError, match="pipeline_edit"):
            s.hooks.post_pass("map_datapath", "register-mapper", print)


# ---------------------------------------------------------------- resumability

def test_resume_from_the_run_directory(flow, tmp_path):
    f, log = flow
    s = Session(f, tmp_path / "o", settings={"syn.fanout.max": 5}, **KW)
    quiet(s.elaborate)
    quiet(s.map_datapath)
    del s
    r = Session.resume(tmp_path / "o")
    assert r.done("map_datapath") and not r.done("syn_map")
    assert r.db.syn.fanout.max == 5 and r.db.set_at("syn.fanout.max") == "settings"
    with pytest.raises(SessionError, match="read by stage elaborate"):
        r.db.syn.fanout.max = 6
    quiet(r.run)
    assert r.done("sta")
    # the record of a resumed run is the uninterrupted run's
    log.clear()
    s2 = Session(f, tmp_path / "full", settings={"syn.fanout.max": 5}, **KW)
    quiet(s2.run)
    a = provenance_settings.read(tmp_path / "o" / "settings.json")
    b = provenance_settings.read(tmp_path / "full" / "settings.json")
    assert _rel(a["pipeline"], tmp_path / "o") == _rel(b["pipeline"], tmp_path / "full")
    assert a["options"] == b["options"]
    assert r.results["elaborate"].settings == {"syn.fanout.max": 5}


def test_resume_refuses_changed_inputs(flow, tmp_path, monkeypatch, capsys):
    """resume compares session.json's input record (recipe
    files, legacy variables, circt-abc commit, record format) with the
    present inputs; a change is refused unless allow_changed_inputs."""
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.elaborate)
    st = json.loads((tmp_path / "o" / "session.json").read_text())
    assert st["inputs"]["recipe_files"]["flow_yaml"]["sha256"]
    assert st["inputs"]["legacy_env"] == {} and "commit" in st["inputs"]["circt_abc"]
    Session.resume(tmp_path / "o")                               # unchanged: fine
    # a changed flow.yaml
    orig = f.read_text()
    f.write_text(orig + "# edited\n")
    with pytest.raises(SessionError, match="flow_yaml .* changed"):
        Session.resume(tmp_path / "o")
    capsys.readouterr()
    r = Session.resume(tmp_path / "o", allow_changed_inputs=True)
    assert r.done("elaborate") and "allow_changed_inputs: continued" in capsys.readouterr().err
    f.write_text(orig)
    # a legacy variable set since
    monkeypatch.setenv("CIRCT_FLOW_MAX_FANOUT", "7")
    with pytest.raises(SessionError, match="environment CIRCT_FLOW_MAX_FANOUT"):
        quiet(Session.resume, tmp_path / "o")
    monkeypatch.delenv("CIRCT_FLOW_MAX_FANOUT")
    # another circt-abc commit
    monkeypatch.setattr(provenance_settings, "_git", lambda root: {"commit": "f" * 40, "dirty": False})
    with pytest.raises(SessionError, match="circt-abc commit"):
        Session.resume(tmp_path / "o")
    monkeypatch.undo()
    # an unknown record format; a record from before the input check (warned, not compared)
    st["format"] = 2
    (tmp_path / "o" / "session.json").write_text(json.dumps(st))
    with pytest.raises(SessionError, match="format 2"):
        Session.resume(tmp_path / "o")
    st["format"] = 1
    del st["inputs"]
    (tmp_path / "o" / "session.json").write_text(json.dumps(st))
    capsys.readouterr()
    Session.resume(tmp_path / "o")
    assert "predates the input record" in capsys.readouterr().err


def test_resume_refuses_a_missing_input_and_a_foreign_directory(flow, tmp_path):
    f, _ = flow
    with pytest.raises(SessionError, match="not written by a session"):
        Session.resume(tmp_path)
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.elaborate)
    quiet(s.map_datapath)
    s.artifacts["struct"].unlink()
    r = Session.resume(tmp_path / "o")
    with pytest.raises(SessionError, match="input\\(s\\) missing"):
        quiet(r.syn_map)


# ---------------------------------------------------------------- run_pass

HELP = """OVERVIEW: circt-abc-opt
  Compiler passes to run
    Passes:
      --canonicalize                                   -   Canonicalize operations
        --max-iterations=<long>                        - Max. iterations
      --map-prefix-adders                              -   prefix adders
        --arch=<string>                                - arch
        --enable-prefix-adders                         - Enable
"""


def test_run_pass_checks_names_and_replaces_the_current_ir(flow, tmp_path, monkeypatch):
    f, log = flow
    from circt_flow import passes
    monkeypatch.setitem(passes._TOOL_PASSES, "/BIN/circt-abc-opt", passes.parse_help(HELP))
    s = Session(f, tmp_path / "o", **KW)
    with pytest.raises(SessionError, match="no IR between stages"):
        s.run_pass("canonicalize")
    quiet(s.elaborate)
    with pytest.raises(options.OptionError, match="did you mean canonicalize"):
        s.run_pass("canonicalise")
    with pytest.raises(options.OptionError, match="did you mean enable-prefix-adders"):
        s.run_pass("map-prefix-adders", enable_prefix_adder=True)
    r = s.run_pass("map-prefix-adders", enable_prefix_adders=True, arch="KOGGE-STONE",
                   replace=True)
    assert r.command[1] == "--map-prefix-adders=enable-prefix-adders=true arch=KOGGE-STONE"
    assert r.replaced == str(s.artifacts["core"])
    assert Path(r.output).name == "01_map-prefix-adders.mlir"
    assert s.artifacts["core"].read_text() == Path(r.output).read_text()
    quiet(s.map_datapath)
    assert log[-1][-1] == str(s.artifacts["struct"]) and str(s.artifacts["core"]) in log[-1]


def test_run_pass_replace_is_recorded_not_reproducible(flow, tmp_path, monkeypatch, capsys):
    """run_pass(replace=True) marks the record not
    reproducible from settings (kept across resume); from_settings of a
    record with hooks or such rewrites warns that they are not replayed."""
    f, log = flow
    from circt_flow import passes
    monkeypatch.setitem(passes._TOOL_PASSES, "/BIN/circt-abc-opt", passes.parse_help(HELP))
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.elaborate)
    s.run_pass("canonicalize")                                  # not replacing: nothing recorded
    quiet(s.map_datapath)
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert "run_pass_replaced" not in rec
    s.invalidate("map_datapath")
    s.run_pass("canonicalize", replace=True)
    r = Session.resume(tmp_path / "o")
    quiet(r.map_datapath)
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    x = rec["run_pass_replaced"]
    assert x["reproducible_from_settings"] is False
    assert [p["pass"] for p in x["passes"]] == ["canonicalize"]
    assert x["passes"][0]["replaced"] == str(s.artifacts["core"])
    capsys.readouterr()
    Session.from_settings(f, tmp_path / "n", tmp_path / "o" / "settings.json", **KW)
    err = capsys.readouterr().err
    assert "WARNING" in err and "run_pass(replace=True)" in err and "cannot replay" in err
    # a record with hooks warns as well; a plain one does not
    h = json.loads((tmp_path / "o" / "settings.json").read_text())
    h.pop("run_pass_replaced")
    h["hooks"] = {"reproducible_from_settings": False, "registered": ["pre elaborate f"]}
    (tmp_path / "h.json").write_text(json.dumps(h))
    Session.from_settings(f, tmp_path / "n2", tmp_path / "h.json", **KW)
    assert "hooks (Python code)" in capsys.readouterr().err
    h.pop("hooks")
    (tmp_path / "p.json").write_text(json.dumps(h))
    Session.from_settings(f, tmp_path / "n3", tmp_path / "p.json", **KW)
    assert "WARNING" not in capsys.readouterr().err


# ---------------------------------------------------------------- settings.json round trip

def test_settings_round_trip_and_scoped_record(flow, tmp_path):
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.db.syn.adders.cells.min_width = 14
    s.db.syn.vt_swap.recover_order = "leak"
    p = s.write_settings(tmp_path / "s.json")
    rec = json.loads(p.read_text())
    assert rec["options"]["syn.adders.cells.min_width"] == {"value": 14, "source": "argument"}
    assert rec["set_at"]["syn.vt_swap.recover_order"].startswith("script:")
    s2 = Session.from_settings(f, tmp_path / "o2", p, **KW)
    assert s2.db.syn.adders.cells.min_width == 14 and s2.db.syn.vt_swap.recover_order == "leak"
    assert s2.db.report(changed=True).as_dict() == s.db.report(changed=True).as_dict()
    # scoped settings: recorded, refused without the query module
    s.db.add_scoped(ScopedSetting("register", ("total_r@1-4",), "gate_register",
                                  (("detect", "zero"),), "script:x:1"))
    assert s.db.scoped[0].targets == ("total_r@1-4",)
    try:
        import circt_flow.query  # noqa: F401
    except ImportError:
        with pytest.raises(SessionError, match="circt_flow.query"):
            s.db.validate()
    s.db.reset_scoped()
    assert s.db.scoped == ()


# ---------------------------------------------------------------- option -> stage map

LATE = [n for n in options.registry() if stage_of(n) != "elaborate"]


@pytest.mark.parametrize("name", sorted(LATE))
def test_a_late_stage_option_does_not_change_the_synthesis_commands(tmp_path, name):
    """stage_of() lets an option be set after elaborate .. syn_map ran only
    when it cannot change their commands: set to a non-default value, the
    synthesis stages' command lines are unchanged (or the load refuses it)."""
    import test_options as T
    o = options.registry()[name]
    if not o.settable:
        pytest.skip("pack-only")
    req = {k: v for k, v in o.requires if k != "_partitions"}

    def cmds(extra, d):
        d.mkdir()
        doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
               "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}
        if req or extra:
            doc["options"] = {**req, **extra}
        (d / "f.yaml").write_text(yaml.safe_dump(doc))
        from circt_flow.config import FlowConfig
        cfg = FlowConfig.load(d / "f.yaml", d / "o", circt_abc_opt="/BIN/opt",
                              circt_opt="/BIN/copt", abc_bin="/BIN/abc")
        import golden_cmdlines
        with redirect_stdout(io.StringIO()):
            c = golden_cmdlines.stage_commands(cfg)
        if stage_of(name) == "map_datapath":        # only elaborate's commands come before it
            c = [x for x in c if "--hw-aggregate-to-comb" in x and x[-1].endswith("_deagg.mlir")]
        return json.dumps(c).replace(str(d), "<D>")
    try:
        base = cmds({}, tmp_path / "a")
    except Exception:
        pytest.skip("its requirements alone are refused here (no vt_flavours pack)")
    try:
        got = cmds({name: T._value_for(o, tmp_path)}, tmp_path / "b")
    except Exception:
        return                                   # refused
    assert got == base


# ---------------------------------------------------------------- run / shell entry points

def _main(argv, stdin=None):
    from circt_flow import cli
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin, sys.argv
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    from contextlib import redirect_stderr
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        sys.stdin, sys.argv = old
    return rc, out.getvalue(), err.getvalue()


def test_run_entry_point_with_a_preloaded_session(flow, tmp_path):
    f, log = flow
    script = tmp_path / "recipe_x.py"
    script.write_text(
        "s.configure(circt_abc_opt='/BIN/circt-abc-opt', circt_opt='/BIN/circt-opt', abc_bin='/BIN/abc')\n"
        "s.db.syn.fanout.max = int(argv[0])\n"
        "r = s.run()\n"
        "print('CELLS', s.report_qor()['cells'], [x.stage for x in r.stages][:3])\n")
    rc, out, err = _main(["run", str(script), str(f), str(tmp_path / "o"), "--set",
                          "syn.adders.cells.min_width=14", "--", "5"])
    assert rc == 0, err
    assert "CELLS 3 ['elaborate', 'map_datapath', 'syn_map']" in out
    assert (tmp_path / "o" / "recipe.py").read_text() == script.read_text()
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert rec["options"]["syn.fanout.max"] == {"value": 5, "source": "argument"}
    assert rec["set_at"]["syn.fanout.max"] == f"script:{script.resolve()}:2"
    assert rec["set_at"]["syn.adders.cells.min_width"] == "settings"
    assert any("max-fanout=5" in " ".join(c) for c in log)


def test_run_entry_point_recipe_function_and_own_session(flow, tmp_path):
    f, _ = flow
    a = tmp_path / "a.py"
    a.write_text("def recipe(s):\n    s.configure(**KW)\n    s.elaborate()\n    print('DONE', s.done('elaborate'))\n"
                 "KW = dict(circt_abc_opt='/BIN/o', circt_opt='/BIN/c', abc_bin='/BIN/a')\n")
    rc, out, err = _main(["run", str(a), str(f), str(tmp_path / "o1")])
    assert rc == 0, err
    assert "DONE True" in out and (tmp_path / "o1" / "recipe.py").is_file()
    b = tmp_path / "b.py"
    b.write_text(f"x = Session({str(f)!r}, {str(tmp_path / 'o2')!r}, circt_abc_opt='/BIN/o',"
                 " circt_opt='/BIN/c', abc_bin='/BIN/a')\nx.elaborate()\n")
    rc, out, err = _main(["run", str(b)])
    assert rc == 0, err
    assert (tmp_path / "o2" / "recipe.py").read_text() == b.read_text()
    rc, out, err = _main(["run", str(a)])
    assert rc == 1 and "defines recipe(s)" in err


def test_run_keeps_every_script_of_a_run_directory(flow, tmp_path):
    """a second, different script writing into the same run
    directory is kept as recipe_2.py; recipe.py is never overwritten."""
    f, _ = flow
    a = tmp_path / "a.py"
    a.write_text("s.configure(circt_abc_opt='/BIN/o', circt_opt='/BIN/c', abc_bin='/BIN/a')\n"
                 "s.elaborate()\n")
    b = tmp_path / "b.py"
    b.write_text("print('B', s.done('elaborate'))\n")
    o = tmp_path / "o"
    assert _main(["run", str(a), str(f), str(o)])[0] == 0
    rc, out, err = _main(["run", str(b), "--resume", str(o)])
    assert rc == 0, err
    assert (o / "recipe.py").read_text() == a.read_text()
    assert (o / "recipe_2.py").read_text() == b.read_text() and "recipe_2.py" in err
    assert _main(["run", str(b), "--resume", str(o)])[0] == 0      # same content: no third copy
    assert not (o / "recipe_3.py").exists()


def test_run_entry_point_refuses_bad_options(flow, tmp_path):
    f, _ = flow
    c = tmp_path / "c.py"
    c.write_text("s.db.syn.fanout.maxx = 3\n")
    rc, out, err = _main(["run", str(c), str(f), str(tmp_path / "o")])
    assert rc == 1 and "did you mean syn.fanout.max" in err
    rc, out, err = _main(["run", str(c), str(f), str(tmp_path / "o"), "--set", "syn.fanout.mx=3"])
    assert rc == 1 and "unknown option" in err
    # an option of a stage that already ran is refused, not ignored
    d = tmp_path / "d.py"
    d.write_text("s.configure(circt_abc_opt='/BIN/o', circt_opt='/BIN/c', abc_bin='/BIN/a')\n"
                 "s.elaborate()\ns.db.syn.adders.cells.min_width = 14\n")
    rc, out, err = _main(["run", str(d), str(f), str(tmp_path / "o3")])
    assert rc == 1 and "read by stage elaborate" in err


def test_resume_through_the_run_entry_point(flow, tmp_path):
    f, log = flow
    a = tmp_path / "a.py"
    a.write_text("s.configure(circt_abc_opt='/BIN/o', circt_opt='/BIN/c', abc_bin='/BIN/a')\n"
                 "s.elaborate(); s.map_datapath()\n")
    assert _main(["run", str(a), str(f), str(tmp_path / "o")])[0] == 0
    b = tmp_path / "b.py"
    b.write_text("print('NEXT', [n for n in s.stages if not s.done(n)][0]); s.run()\n")
    rc, out, err = _main(["run", str(b), "--resume", str(tmp_path / "o")])
    assert rc == 0, err
    assert "NEXT syn_map" in out
    assert json.loads((tmp_path / "o" / "session.json").read_text())["done"][-1] == "sta"


def test_shell_reads_commands_from_a_pipe(flow, tmp_path):
    f, _ = flow
    cmds = ("s.db.syn.fanout.max = 4\n"
            "print('V', s.db.syn.fanout.max, s.db.source('syn.fanout.max'))\n"
            "help('syn.fanout.max')\n"
            "s.db.syn.fanout.maxx = 1\n"
            "print('STILL', db['syn.fanout.max'])\n")
    rc, out, err = _main(["shell", str(f), str(tmp_path / "o")], stdin=cmds)
    assert rc == 1                                       # an error in piped input: exit 1
    assert "V 4 argument" in out
    assert "syn.fanout.max = 4" in out + err          # help() of an option
    assert "did you mean syn.fanout.max" in out + err  # the error is shown, the shell goes on
    assert "STILL 4" in out


def test_shell_completion_of_attributes_and_option_names(flow, tmp_path):
    f, _ = flow
    from circt_flow import script
    s = Session(f, tmp_path / "o", **KW)
    c = script._Completer(script.namespace(s))
    got = []
    i = 0
    while (x := c.complete("s.db.syn.vt_", i)) is not None:
        got.append(x)
        i += 1
    assert "s.db.syn.vt_swap" in got
    i, got = 0, []
    while (x := c.complete("s.db.syn.vt_swap.rec", i)) is not None:
        got.append(x)
        i += 1
    assert "s.db.syn.vt_swap.recover_order" in got and "s.db.syn.vt_swap.recover" in got
    c.complete.__func__  # noqa: B018
    # inside a string: option names (the line buffer ends with a quote)
    import types
    fake = types.SimpleNamespace(get_line_buffer=lambda: 's.set("syn.vt_swap.rec',
                                 get_begidx=lambda: len('s.set("'))
    sys.modules["readline"], saved = fake, sys.modules.get("readline")
    try:
        names = []
        i = 0
        while (x := c.complete("syn.vt_swap.rec", i)) is not None:
            names.append(x)
            i += 1
    finally:
        if saved is not None:
            sys.modules["readline"] = saved
        else:
            del sys.modules["readline"]
    assert names == ["syn.vt_swap.recover", "syn.vt_swap.recover_order"]


def test_shell_as_a_real_process(tmp_path):
    """python3 -m circt_flow shell, commands on stdin (no tool runs)."""
    import subprocess
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump({"design": {"top_module": "top", "clock_period_ps": 1000},
                                 "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CIRCT_FLOW_", "CIRCT_VT_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    p = subprocess.run([sys.executable, "-m", "circt_flow", "shell", str(f), str(tmp_path / "o"),
                        "--set", "syn.fanout.max=6"],
                       input="print('GOT', s.db.syn.fanout.max, s.stages[0])\n",
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "GOT 6 elaborate" in p.stdout


# ---------------------------------------------------------------- golden: identity through the session

def _set_by_attribute(s, name, value):
    *ns, leaf = name.split(".")
    node = s.db
    for p in ns:
        node = getattr(node, p)
    setattr(node, leaf, value)


@pytest.mark.parametrize("case", sorted(__import__("golden_cmdlines").CASES))
def test_golden_command_lines_through_session_attributes(tmp_path, case, monkeypatch):
    """The golden command lines (recorded before the registry, from the
    legacy environment) come out of a session whose options were assigned as
    s.db attributes: the session path builds the same pass commands."""
    import golden_cmdlines
    for k in list(os.environ):
        if k.startswith(("CIRCT_FLOW_", "CIRCT_VT_")):
            monkeypatch.delenv(k)
    want = json.loads((Path(__file__).parent / "data" / "golden_cmdlines.json").read_text())[case]
    env, yaml_extra = golden_cmdlines.CASES[case]
    td = tmp_path
    env = {k: v.replace("<OUT>", str(td)) for k, v in env.items()}
    yaml_extra = dict(yaml_extra)
    parts = yaml_extra.pop("_partitions", None)
    if "CIRCT_FLOW_GROUP_SCRIPTS" in env:
        Path(env["CIRCT_FLOW_GROUP_SCRIPTS"]).write_text('{"g1": "x.abc"}')
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
           "pdk": {"synth_lib_tt": ["/LIB/a.lib", "/LIB/b.lib"]}, **yaml_extra}
    (td / "flow.yaml").write_text(yaml.safe_dump(doc))
    s = Session(td / "flow.yaml", td / "out", circt_abc_opt="/BIN/circt-abc-opt",
                circt_opt="/BIN/circt-opt", abc_bin="/BIN/abc", partitions=parts)
    for name, v in options.legacy_to_settings(env).items():
        _set_by_attribute(s, name, v)
    with redirect_stdout(io.StringIO()):
        cmds = golden_cmdlines.stage_commands(s.cfg)
    root = str(Path(golden_cmdlines.__file__).resolve().parents[3])
    got = [[a.replace(str(td), "<OUT>").replace(root, "<ROOT>") for a in c] for c in cmds]
    assert got == want


@pytest.mark.parametrize("name", sorted(options.registry()))
def test_every_option_set_through_the_session_is_applied_or_refused(tmp_path, name):
    """test_options' applied-or-refused check, with the option set through
    s.db (assignment, then validate) instead of a flow.yaml options: entry."""
    import test_options as T
    if name in T._APPLIED_ELSEWHERE:
        pytest.skip(T._APPLIED_ELSEWHERE[name])
    o = options.registry()[name]
    req = dict(o.requires)
    parts = req.pop("_partitions", None)

    def session(d, extra):
        d.mkdir()
        doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
               "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}
        (d / "f.yaml").write_text(yaml.safe_dump(doc))
        s = Session(d / "f.yaml", d / "o", partitions=parts, circt_abc_opt="/BIN/opt",
                    circt_opt="/BIN/copt", abc_bin="/BIN/abc")
        for k, v in {**req, **extra}.items():
            s.db[k] = v
        s.db.validate()
        return s
    try:
        base = T._fingerprint(session(tmp_path / "base", {}).cfg, tmp_path / "base")
    except Exception:
        pytest.skip("its requirements alone are refused here")
    try:
        s = session(tmp_path / "set", {name: T._value_for(o, tmp_path)})
    except Exception as e:
        assert any(w in str(e) for w in T._keys_of(o)), f"{name}: error does not name it: {e!r}"
        return
    assert o.settable
    assert T._fingerprint(s.cfg, tmp_path / "set") != base, f"{name}: accepted and ignored"


# ---------------------------------------------------------------- P&R: pack options to the pack command

_PACK_PNR = {"command": ["./pnr/run.py"], "settings_registry": "pnr/opts.json"}


def _pack_reg(tmp_path):
    reg = {"format": 1, "legacy_env_removed_in": options.LEGACY_ENV_REMOVED_IN,
           "namespaces": ["xyz"],
           "options": [{"name": "xyz.tol", "type": "float", "default": 0.5, "range": [0, 1],
                        "var": "XYZ_TOL"}]}
    (tmp_path / "pack" / "pnr").mkdir(parents=True)
    (tmp_path / "pack" / "pnr" / "opts.json").write_text(json.dumps(reg))
    return tmp_path / "pack"


def test_pack_command_takes_a_settings_file(tmp_path):
    from types import SimpleNamespace
    pack = _pack_reg(tmp_path)
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(pnr={"clock_port": "c"},
                               pack_pnr={"command": ["bash", "-c", "true", "x"]}, tie=None,
                               top_module="top", clock_period_ps=1000, pack_root=pack,
                               manifest=tmp_path / "m.yaml", config_dir=tmp_path,
                               pack_settings={}))
    cmd, _ = stages.pack_command(c, tmp_path / "pnr")
    assert "--settings" not in cmd                               # no registry: unchanged
    c.design.pack_pnr = {**c.design.pack_pnr, "settings_registry": "pnr/opts.json"}
    cmd, _ = stages.pack_command(c, tmp_path / "pnr")
    assert "--settings" not in cmd                               # nothing to hand over
    c.design.pack_settings = {"xyz.tol": 0.25}
    cmd, _ = stages.pack_command(c, tmp_path / "pnr")
    f = (tmp_path / "pnr" / "pack_settings.json").resolve()
    assert cmd[4:6] == ["--settings", str(f)] and cmd[6] == "--netlist"
    assert json.loads(f.read_text()) == {"options": {"xyz.tol": 0.25}}
    c.design.pack_pnr = {}
    with pytest.raises(ValueError, match="need the tech pack's own P&R command"):
        stages.orfs_pnr(c)


def _handed(pack, seen):
    """A stand-in for stages.orfs_pnr: what pack_command would write (the
    handover of the session's values, cfg.design.pack_settings)."""
    from circt_flow import pack_options

    def fake(cfg):
        given = getattr(cfg.design, "pack_settings", {}) or {}
        seen["given"] = dict(given)
        seen["options"] = pack_options.handover(pack[0], dict(_PACK_PNR), given)[0]
    return fake


def test_place_and_route_hands_pack_options_over(flow, tmp_path, monkeypatch):
    f, _ = flow
    pack = _pack_reg(tmp_path)
    seen = {}
    monkeypatch.setattr(Session, "_pack_root", lambda self, cfg=None: pack)
    monkeypatch.setattr(Session, "_pack_pnr", lambda self, cfg=None: dict(_PACK_PNR))
    monkeypatch.setattr(stages, "orfs_pnr", _handed([pack], seen))
    s = Session(f, tmp_path / "o", **KW)
    with pytest.raises(options.OptionError, match="unknown pack option"):
        s.set_pack_option("xyz.toll", 0.2)
    with pytest.raises(options.OptionError):
        s.set_pack_option("xyz.tol", 3)                    # out of range
    s.set_pack_option("xyz.tol", "0.25")
    assert s.pack_options == {"xyz.tol": 0.25}
    assert s.pack_option_sources == {"xyz.tol": "set_pack_option"}
    quiet(s.run, stop="sta")
    quiet(s.place_and_route)
    assert seen["options"] == s.pack_options == {"xyz.tol": 0.25}
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert rec["pack_options"] == {"xyz.tol": 0.25}
    r = Session.resume(tmp_path / "o")
    assert r.pack_options == {"xyz.tol": 0.25}
    with pytest.raises(SessionError, match="already run"):
        r.set_pack_option("xyz.tol", 0.3)
    # without pack options the stage is called as before (nothing in the config)
    seen.clear()
    s2 = Session(f, tmp_path / "o2", **KW)
    quiet(s2.run, pnr=True)
    assert seen == {"given": {}, "options": {}}


def test_pack_options_show_the_profile_defaults(flow, tmp_path, monkeypatch):
    """s.pack_options and write_pack_settings() hold what the stage hands
    over: the profile's settings_defaults with the session's values over
    them, each with its source."""
    f, _ = flow
    pack = _pack_reg(tmp_path)
    pnr = {**_PACK_PNR, "settings_defaults": {"xyz.tol": 0.75}}
    monkeypatch.setattr(Session, "_pack_root", lambda self, cfg=None: pack)
    monkeypatch.setattr(Session, "_pack_pnr", lambda self, cfg=None: dict(pnr))
    s = Session(f, tmp_path / "o", **KW)
    assert s.pack_options == {"xyz.tol": 0.75}
    assert s.pack_option_sources == {"xyz.tol": "pnr.settings_defaults (the pack profile)"}
    p = s.write_pack_settings()
    assert p == tmp_path / "o" / "pnr" / "pack_settings.json"
    assert json.loads(p.read_text()) == {"options": {"xyz.tol": 0.75}}
    s.set_pack_option("xyz.tol", 0.25)
    assert s.pack_options == {"xyz.tol": 0.25}
    assert s.pack_option_sources == {"xyz.tol": "set_pack_option"}


def _pack_reg_dir(tmp_path, recipe_dir="pnr/notes", shared=None):
    reg = {"format": 1, "legacy_env_removed_in": options.LEGACY_ENV_REMOVED_IN,
           "namespaces": ["xyz", "cts"], "recipe_dir_option": "xyz.dir",
           "options": [{"name": "xyz.tol", "type": "float", "default": 0.5, "range": [0, 1],
                        "var": "XYZ_TOL", "consumers": ["recipe"]},
                       {"name": "xyz.mode", "type": "str", "default": None,
                        "var": "XYZ_MODE", "consumers": ["command"]},
                       {"name": "xyz.dir", "type": "path", "default": None,
                        "var": "XYZ_DIR", "legacy_env": ["XYZ_DIR"], "consumers": ["command"]}]
           + ([shared] if shared else [])}
    if recipe_dir is not None:
        reg["recipe_dir"] = recipe_dir
    pack = tmp_path / "pack"
    (pack / "pnr" / "notes").mkdir(parents=True, exist_ok=True)
    (pack / "pnr" / "opts.json").write_text(json.dumps(reg))
    return pack


def test_session_selects_the_pack_recipe_dir(flow, tmp_path, monkeypatch):
    """A pack option read from the pack's recipe directory (a consumer tag
    the registry lists) needs the option that carries the directory; it is
    set to the directory the registry names (recipe_dir), shown in
    s.pack_options with its source and recorded; without one the stage is
    refused naming that option; a value the user set, or its legacy
    variable, is left alone; other options add nothing."""
    f, _ = flow
    seen = {}
    holder = [None]
    monkeypatch.setattr(Session, "_pack_pnr", lambda self, cfg=None: dict(_PACK_PNR))
    monkeypatch.setattr(stages, "orfs_pnr", _handed(holder, seen))
    monkeypatch.delenv("XYZ_DIR", raising=False)

    def run(out, pack, **opts):
        holder[0] = pack
        monkeypatch.setattr(Session, "_pack_root", lambda self, cfg=None: pack)
        s = Session(f, tmp_path / out, **KW)
        for k, v in opts.items():
            s.set_pack_option(k.replace("__", "."), v)
        quiet(s.run, stop="sta")
        seen.clear()
        quiet(s.place_and_route)
        assert seen["options"] == s.pack_options
        return s, seen["options"]
    pack = _pack_reg_dir(tmp_path / "p1")
    rd = str((pack / "pnr" / "notes").resolve())
    s, got = run("o1", pack, xyz__tol=0.2)
    assert got == {"xyz.tol": 0.2, "xyz.dir": rd}
    assert s.pack_option_sources["xyz.dir"] == "registry recipe_dir (for xyz.tol)"
    rec = provenance_settings.read(tmp_path / "o1" / "settings.json")
    assert "registry recipe_dir" in rec["set_at"]["pack:xyz.dir"]
    assert Session.resume(tmp_path / "o1").pack_options["xyz.dir"] == rd
    _, got = run("o2", pack, xyz__mode="a")                       # not read from the directory
    assert got == {"xyz.mode": "a"}
    other = tmp_path / "mine"
    other.mkdir()
    _, got = run("o3", pack, xyz__tol=0.2, xyz__dir=str(other))
    assert got["xyz.dir"] == str(other)                            # the user's choice
    monkeypatch.setenv("XYZ_DIR", str(other))
    _, got = run("o4", pack, xyz__tol=0.2)
    assert "xyz.dir" not in got                                    # the legacy form: the pack's business
    monkeypatch.delenv("XYZ_DIR")
    bare = _pack_reg_dir(tmp_path / "p2", recipe_dir=None)
    with pytest.raises(SessionError, match="set s.set_pack_option\\('xyz.dir'"):
        run("o5", bare, xyz__tol=0.2)


def test_closure_ladder_and_orfs_with_session_pack_options(flow, tmp_path, monkeypatch):
    """With a pack command the session's pack options reach the stage also
    through the closure ladder (the config carries them; every ladder job
    writes the same file); without one (ORFS) options set with
    set_pack_option are refused, while shared options set through s.db act
    through the flow's pnr: section and nothing is handed over."""
    f, _ = flow
    shared = {"name": "cts.skip_fanout_le", "type": "int", "default": None, "shared": True,
              "consumers": ["command"]}
    pack = _pack_reg_dir(tmp_path, shared=shared)
    seen = {}
    monkeypatch.setattr(Session, "_pack_root", lambda self, cfg=None: pack)
    monkeypatch.setattr(stages, "orfs_pnr", _handed([pack], seen))
    for pnr, ladder in ((dict(_PACK_PNR), True),
                       ({"orfs_platform_dir": "x", "settings_registry": "pnr/opts.json"}, False)):
        monkeypatch.setattr(Session, "_pack_pnr", lambda self, cfg=None, p=pnr: dict(p))
        if ladder:
            monkeypatch.setattr(Session, "_pnr_route", staticmethod(lambda cfg: "ladder"))
        s = Session(f, tmp_path / f"{ladder}1", **KW)
        s.db["cts.skip_fanout_le"] = 7
        quiet(s.run, stop="sta")
        seen.clear()
        quiet(s.place_and_route)
        assert seen["given"] == ({"cts.skip_fanout_le": 7} if ladder else {})
        s = Session(f, tmp_path / f"{ladder}2", **KW)
        s.set_pack_option("xyz.mode", "q")
        quiet(s.run, stop="sta")
        seen.clear()
        if ladder:
            quiet(s.place_and_route)
            assert seen["given"] == {"xyz.mode": "q"}
        else:
            with pytest.raises(SessionError, match="own P&R command.*ORFS"):
                quiet(s.place_and_route)


# ---------------------------------------------------------------- object queries through the session

def _scoped_flow(tmp_path, monkeypatch, log):
    data = Path(__file__).parent / "data" / "scoped" / "top_core_deagg.mlir"
    core = tmp_path / "core_in.mlir"
    core.write_text(data.read_text())
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump({"design": {"top_module": "top", "clock_period_ps": 1000,
                                            "core_mlir": str(core)},
                                 "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}))
    base = fake_run(log)

    def run(cmd, stdout=None, env=None):
        base(cmd, stdout, env)
        cmd = [str(c) for c in cmd]
        if "--hw-aggregate-to-comb" in cmd and cmd[-1].endswith("_core_deagg.mlir"):
            Path(cmd[-1]).write_text(data.read_text())       # the elaborated design
    monkeypatch.setattr(stages, "_run", run)
    for name in ("_run_plain",):
        if hasattr(stages, name):
            monkeypatch.setattr(stages, name, run)
    return f


def test_scoped_commands_through_the_session_equal_the_legacy_options(flow, tmp_path, monkeypatch):
    """The recipe pattern of examples/scripting/core_lv_scoped.py on a small
    design: elaborate, then s.gate_register / s.map_adders on query results.
    The structural command the session's config builds is the legacy dotted
    form's (which warns); the scoped settings are recorded, and refused once
    map_datapath ran. (The pass reports that check each target need the real
    tool: tests/test_scoping.py, needs_tool.)"""
    import golden_cmdlines
    from circt_flow import scoping
    from contextlib import redirect_stderr
    outs = {}
    for form in ("legacy", "scoped"):
        (tmp_path / form).mkdir()
        f = _scoped_flow(tmp_path / form, monkeypatch, [])
        s = Session(f, tmp_path / form / "o", **KW)
        scoping._WARNED.clear()
        options._WARNED.clear()
        with redirect_stderr(io.StringIO()) as err:
            if form == "legacy":
                s.db.syn.clock_gating.data_gate = "leaf/q@1-4@zero,leaf/q@5-12@zero"
                s.db.syn.adders.prefix2.min_width = 12
                s.db.syn.adders.prefix2.max_width = 40
                s.db.syn.adders.prefix2.boundary = 1
                s.db.syn.adders.prefix2.arch = "HAN-CARLSON"
                quiet(s.elaborate)
            else:
                quiet(s.elaborate)
                for lo, hi in ((1, 4), (5, 12)):
                    s.gate_register(s.flops("leaf/q").bits(lo, hi), detect="zero")
                s.map_adders(s.adders(width=(12, 40)), form="prefix", arch="HAN-CARLSON",
                             boundary=1)
            with redirect_stdout(io.StringIO()):
                cmds = golden_cmdlines.stage_commands(s.cfg)
        assert ("legacy form" in err.getvalue()) == (form == "legacy")
        st = next(c for c in cmds if c[-1].endswith("_struct.mlir"))
        outs[form] = [x.replace(str(tmp_path / form), "<D>") for x in st]
        if form == "scoped":
            assert [x.command for x in s.db.scoped] == ["gate_register"] * 2 + ["map_adders"]
            rec = json.loads(s.write_settings(tmp_path / "rec.json").read_text())
            assert [x["command"] for x in rec["scoped"]] == ["gate_register"] * 2 + ["map_adders"]
            assert rec["options"]["syn.scoped"]["source"] == "argument"
            s._done.append("map_datapath")
            with pytest.raises(SessionError, match="map_datapath, which has already run"):
                s.gate_register(s.flops("leaf/q"))
            with pytest.raises(SessionError, match="read by stage map_datapath"):
                s.db["syn.scoped"] = []
    assert outs["legacy"] == outs["scoped"]
    assert "--data-gate-enables=slices=leaf/q@1-4@zero,leaf/q@5-12@zero" in outs["scoped"]


# Records that are not settable (pack-only) stay out of the stub.
PACK_ONLY = ("flow.pack.partitions", "syn.shift_select")


def test_stub_leaves_out_pack_only_records(capsys):
    """`options --stub` names the settable options only; the committed
    doc/db_types.pyi is its output."""
    from circt_flow import db, options

    def named(stub, full):
        ns, leaf = full.rsplit(".", 1)
        cls = "class db_" + ns.replace(".", "_") + ":"
        if cls not in stub:
            return False
        body = stub.split(cls, 1)[1].split("\nclass ", 1)[0]
        return f"\n    {leaf}:" in body
    pub = db.stub_text()
    for n in PACK_ONLY:
        assert n in options.registry(), n
        assert not named(pub, n), n
    for n, o in options.registry().items():
        if not o.settable:
            assert not named(pub, n), n
    committed = Path(__file__).resolve().parents[3] / "doc" / "db_types.pyi"
    if committed.is_file():                  # no doc/ in the image
        assert committed.read_text() == pub
    compile(pub, "db_types.pyi", "exec")
    assert options.main(["--stub"]) == 0
    assert capsys.readouterr().out == pub


# ---------------------------------------------------------------- options set from hooks, stage order

def _syn_map_cmd(s):
    return " ".join(next(c for c in s.results["syn_map"].commands if "--merge-residual-modules" in " ".join(c)))


@pytest.mark.parametrize("mode", ["full", "staged"])
def test_option_set_in_a_pre_hook_is_applied(flow, tmp_path, mode):
    """an option a pre hook sets applies to that stage and the later ones,
    in a full run and stage by stage; StageResult, settings.json and
    session.json agree."""
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.hooks.pre("elaborate", lambda ses: ses.db.syn.cleanup.__setattr__("localize_rmw", False))
    s.hooks.pre("elaborate", lambda ses: ses.db.syn.fanout.__setattr__("max", 5))
    quiet(s.run) if mode == "full" else quiet(s.run, start="elaborate")
    assert not any("--localize-dynamic-rmw" in c for c in s.results["elaborate"].commands)
    assert "max-fanout=5" in _syn_map_cmd(s)
    for st in ("elaborate", "syn_map", "sta"):
        assert s.results[st].settings["syn.fanout.max"] == 5
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert rec["options"]["syn.fanout.max"] == {"value": 5, "source": "argument"}
    assert any("max-fanout=5" in " ".join(c) for c in rec["pipeline"])
    assert json.loads((tmp_path / "o" / "session.json").read_text())["given"]["syn.fanout.max"] == 5


def test_option_of_a_stage_already_run_set_in_a_hook_is_refused(flow, tmp_path):
    """a pre hook of syn_map setting an option elaborate reads: refused,
    and the stage does not run."""
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.hooks.pre("syn_map", lambda ses: ses.db.syn.fanout.__setattr__("max", 5))
    with pytest.raises(SessionError, match="read by stage elaborate"):
        quiet(s.run)
    assert not s.done("syn_map") and "syn.fanout.max" not in json.loads(
        (tmp_path / "o" / "session.json").read_text())["given"]


def _in_place(monkeypatch, name, seen):
    """Replace pipeline stage `name` by one that records its input netlist
    and edits the netlist in place (as fanout.run / vtswap.run do)."""
    def fn(cfg, force=False):
        net = Path(cfg.openroad_netlist)
        seen.append(net.read_bytes())
        net.write_text(net.read_text() + f"// edited by {name} run {len(seen)}\n")
    stages_ = [(n, fn if n == name else f) for n, f in pipeline.SYNTH_STAGES]
    monkeypatch.setattr(pipeline, "SYNTH_STAGES", tuple(stages_))


def test_rerun_of_an_in_place_stage_starts_from_its_true_input(flow, tmp_path, monkeypatch):
    """the loop of doc/scripting.md section 6 (invalidate vt_swap, set an
    option, run vt_swap again): every iteration's input is the netlist the
    stage got the first time, byte for byte; re-running fanout starts from
    syn_map's netlist and gives vt_swap a fresh input."""
    f, _ = flow
    fan, vt = [], []
    _in_place(monkeypatch, "fanout", fan)
    _in_place(monkeypatch, "vt_swap", vt)
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.run)
    first = vt[0]
    for order in ("inst", "leak"):
        s.invalidate("vt_swap")
        s.db.syn.vt_swap.recover_order = order
        quiet(s.vt_swap)
    assert vt[1] == first and vt[2] == first
    assert b"run 3" in s.artifacts["openroad_netlist"].read_bytes()
    s.invalidate("fanout")
    quiet(s.fanout)
    assert fan[1] == fan[0] and b"edited" not in fan[0]          # syn_map's own netlist
    quiet(s.vt_swap)
    assert vt[3] != first and vt[3].endswith(b"// edited by fanout run 2\n")
    # resumed in another process: the kept input survives
    r = Session.resume(tmp_path / "o")
    r.invalidate("vt_swap")
    quiet(r.vt_swap)
    assert vt[4] == vt[3]
    # a new syn_map drops the kept inputs
    r.invalidate("syn_map")
    quiet(r.syn_map)
    assert r._inputs == {}


def test_field_stage_table_covers_the_configuration():
    """every configuration field has a reviewed earliest-reader stage."""
    import dataclasses
    from circt_flow import config
    from circt_flow.session import FIELD_STAGE
    want = {f.name for f in dataclasses.fields(config.DesignConfig)} | {
        f.name for f in dataclasses.fields(config.FlowConfig)} - {"design", "settings", "load_overrides"}
    assert want == set(FIELD_STAGE), ("add the new field(s) to session.FIELD_STAGE with the earliest "
                                      f"stage that reads them: {sorted(want ^ set(FIELD_STAGE))}")
    assert set(FIELD_STAGE.values()) <= set(pipeline.STAGE_NAMES)


def _cfg_fields(tmp_path, extra):
    import dataclasses
    from circt_flow import config
    d = tmp_path
    d.mkdir(exist_ok=True)
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
           "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}}
    if extra:
        doc["options"] = extra
    (d / "f.yaml").write_text(yaml.safe_dump(doc))
    from contextlib import redirect_stderr
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        c = config.FlowConfig.load(d / "f.yaml", d / "o", circt_abc_opt="/BIN/o",
                                   circt_opt="/BIN/c", abc_bin="/BIN/a")
    out = {f.name: repr(getattr(c.design, f.name)) for f in dataclasses.fields(c.design)}
    out.update({f.name: repr(getattr(c, f.name)) for f in dataclasses.fields(c)
                if f.name not in ("design", "settings", "load_overrides")})
    return out


@pytest.mark.parametrize("name", sorted(n for n, o in options.registry().items()
                                        if o.settable and stage_of(n) != "elaborate"))
def test_stage_of_is_the_earliest_reader_of_what_the_option_changes(tmp_path, name):
    """an option assigned to a later stage changes only configuration
    fields that stage (or a later one) reads (FIELD_STAGE)."""
    import test_options as T
    from circt_flow.session import FIELD_STAGE
    o = options.registry()[name]
    req = {k: v for k, v in o.requires if k != "_partitions"}
    try:
        a = _cfg_fields(tmp_path, req)
    except Exception as e:
        pytest.skip(f"its requirements alone are refused here: {e}")
    # a value the section's parser accepts (an untyped key: try a few)
    cands = [T._value_for(o, tmp_path)] if o.type != "object" else \
        [2, 0.5, True, False, "x_1", "L/S", ["x_1"], {"x": 1}, 0]
    b, errs = None, []
    for v in cands:
        try:
            b = _cfg_fields(tmp_path, {**req, name: v})
            break
        except Exception as e:
            errs.append(str(e)[:80])
    if b is None:
        pytest.skip(f"no value accepted here: {errs[:2]}")
    order = list(pipeline.STAGE_NAMES)
    changed = [f for f in a if a[f] != b[f]]
    early = [f for f in changed if order.index(FIELD_STAGE[f]) < order.index(stage_of(name))]
    assert not early, f"{name} (stage {stage_of(name)}) changes {early}, read earlier"


def test_pnr_clock_port_belongs_to_elaborate(flow, tmp_path):
    """design.clock_port feeds calibration and the SDCs."""
    assert stage_of("pnr.clock_port") == "elaborate"
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.elaborate)
    with pytest.raises(SessionError, match="pnr.clock_port is read by stage elaborate"):
        s.db.pnr.clock_port = "other_clk"


# modules and the earliest stage whose code is in them (stages.py, pipeline.py,
# config.py, ...: elaborate)
_MODULE_STAGE = {"fanout.py": "fanout", "vtswap.py": "vt_swap", "sta.py": "sta",
                 "query.py": "map_datapath", "scoping.py": "map_datapath",
                 "orfs_pnr.py": "place_and_route", "pnr_recipe.py": "place_and_route",
                 "postlayout.py": "place_and_route", "ladder.py": "place_and_route",
                 "pack_options.py": "place_and_route",
                 }


def test_no_module_reads_an_option_before_its_stage():
    """an option name written in a module's source is read no earlier than
    stage_of() says (module -> earliest stage of its code, _MODULE_STAGE)."""
    import re
    order = list(pipeline.STAGE_NAMES)
    pkg = Path(stages.__file__).parent
    reg = options.registry()
    bad = []
    for p in sorted(pkg.glob("*.py")) + sorted((pkg / "design").glob("*.py")):
        if p.name in ("options.py", "session.py", "db.py", "script.py"):
            continue
        st = _MODULE_STAGE.get(p.name, "elaborate")
        for m in set(re.findall(r"[\"']((?:syn|pnr|cts|hold|post|run|flow)\.[a-z0-9_.]+)[\"']",
                                p.read_text())):
            if m in reg and order.index(stage_of(m)) > order.index(st):
                bad.append((p.name, m, stage_of(m)))
    assert not bad, bad


def test_dry_run_plan_leaves_no_trace(flow, tmp_path, monkeypatch):
    """planning the pipeline (any edit) under --partitions
    adds nothing to the executed record, the scoped record or the warnings."""
    f, log = flow
    from circt_flow import abc_partition, pipeline_edit, scoping

    class R:
        def __init__(s, n):
            s.name, s.returncode, s.seconds = n, 0, 1.0
    monkeypatch.setattr(abc_partition, "map_groups", lambda *a, **k: [R("g0")])
    monkeypatch.setattr(abc_partition, "combine", lambda g, r, spliced:
                        Path(spliced).write_text("hw.module @top() {}\n"))
    s = Session(f, tmp_path / "o", partitions=4, **KW)
    s.pipeline._listing = lambda tool: None
    stages.EXECUTED[:] = [["outer"]]
    applied = dict(scoping.APPLIED)
    cfg = s.cfg
    pipeline_edit.plan(cfg)
    assert stages.EXECUTED == [["outer"]] and scoping.APPLIED == applied
    stages.EXECUTED.clear()
    s.pipeline.insert("map_datapath", "canonicalize")
    quiet(s.elaborate)
    quiet(s.map_datapath)
    quiet(s.syn_map)
    assert not any(c[0] == "(per group)" for c in s.results["map_datapath"].commands)
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert sum(c[0] == "(per group)" for c in rec["pipeline"]) == 1


def test_hooks_on_a_power_domain_design_are_refused(tmp_path, monkeypatch):
    """power.run runs its own sub-flows; stage hooks would never be
    called: refused before anything runs."""
    from circt_flow import config
    s = Session(tmp_path / "f.yaml", tmp_path / "o")
    fake = type("C", (), {"design": type("D", (), {"power": object()})()})()
    monkeypatch.setattr(Session, "_load", lambda self, force=False: fake)
    s.hooks.post("*", lambda ses, r: None)
    with pytest.raises(SessionError, match="power domains"):
        s.run()


def test_nested_session_does_not_corrupt_the_outer_record(flow, tmp_path):
    """a session run inside another session's hook."""
    f, _ = flow
    outer = Session(f, tmp_path / "a", **KW)
    inner = Session(f, tmp_path / "b", **KW)
    outer.hooks.post("elaborate", lambda ses, r: quiet(inner.run))
    quiet(outer.run)
    rec = provenance_settings.read(tmp_path / "a" / "settings.json")
    assert all(str(tmp_path / "b") not in " ".join(c) for c in rec["pipeline"])
    assert all(str(tmp_path / "b") not in " ".join(c) for r in outer.results.values()
               for c in r.commands)


def test_resume_keeps_scoped_applied(flow, tmp_path):
    """the per-target record of a structural stage survives a resume."""
    from circt_flow import scoping
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    quiet(s.elaborate)
    quiet(s.map_datapath)
    key = str((tmp_path / "o").resolve())
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    rec["scoped_applied"] = [{"target": "q@1-4", "status": "applied"}]
    (tmp_path / "o" / "settings.json").write_text(json.dumps(rec))
    scoping.APPLIED.pop(key, None)
    r = Session.resume(tmp_path / "o")
    quiet(r.syn_map)
    assert provenance_settings.read(tmp_path / "o" / "settings.json")["scoped_applied"] == \
        [{"target": "q@1-4", "status": "applied"}]


# ---------------------------------------------------------------- writes while a stage runs

def _first_pass(s, stage):
    s.pipeline._listing = lambda tool: None
    return s.pipeline.passes(stage)[0]["ref"]


def test_option_set_in_a_pass_hook_of_its_own_stage_is_refused(flow, tmp_path):
    """an option elaborate reads, set from a pass hook
    inside elaborate: refused (it was accepted and ignored); nothing records it."""
    f, _ = flow
    s = Session(f, tmp_path / "o", **KW)
    s.hooks.post_pass("elaborate", _first_pass(s, "elaborate"),
                      lambda ses, ir: ses.db.syn.adders.cells.__setattr__("min_width", 8))
    with pytest.raises(SessionError, match="stage elaborate is running now"):
        quiet(s.run)
    assert s.db.source("syn.adders.cells.min_width") == "default"
    assert not s.done("elaborate")


def test_scoped_command_and_edit_from_a_pass_hook_are_refused(flow, tmp_path):
    """syn.scoped (read by map_datapath) set from a
    pre_pass hook of map_datapath, a pipeline edit of the running stage and
    a load argument it reads: refused."""
    f, _ = flow
    for what in ("scoped", "edit", "configure"):
        s = Session(f, tmp_path / what, **KW)
        ref = _first_pass(s, "map_datapath")

        def hook(ses, ir, what=what):
            if what == "scoped":
                ses.db["syn.scoped"] = [{"command": "gate_register", "targets": ["r@0-3"]}]
            elif what == "edit":
                ses.pipeline.insert("map_datapath", "cse")
            else:
                ses.configure(partitions=2)
        s.hooks.pre_pass("map_datapath", ref, hook)
        with pytest.raises((SessionError, options.OptionError),
                           match="is running now|while stage map_datapath is running|already run"):
            quiet(s.run)
        assert not s.done("map_datapath") and not s.db.scoped
        assert s.db.source("syn.scoped") == "default"


def test_later_stage_writes_from_a_pass_hook_take_effect(flow, tmp_path, monkeypatch):
    """Writes for later stages from a pass hook are accepted and applied: an
    option vt_swap reads, an edit of syn_map, run_sta for the sta stage."""
    f, _ = flow
    from circt_flow import sta as sta_mod, vtswap as vt_mod
    ran = []
    monkeypatch.setattr(sta_mod, "run", lambda cfg, spec: ran.append(cfg.run_sta))
    monkeypatch.setattr(vt_mod, "run", lambda cfg, spec: None)
    s = Session(f, tmp_path / "o", **KW)
    ref = _first_pass(s, "map_datapath")

    def hook(ses, ir):
        ses.db.syn.vt_swap.recover_order = "leak"
        ses.pipeline.insert("syn_map", "cse")
        ses.configure(run_sta=True)
    s.hooks.post_pass("map_datapath", ref, hook)
    quiet(s.run)
    assert s.results["vt_swap"].settings["syn.vt_swap.recover_order"] == "leak"
    assert s.pipeline.edits and s.pipeline.edits[0]["stage"] == "syn_map"
    assert ran == [True] and s.results["sta"].status == "done"
    rec = provenance_settings.read(tmp_path / "o" / "settings.json")
    assert rec["options"]["syn.vt_swap.recover_order"]["value"] == "leak"


def test_configure_after_the_run_started(flow, tmp_path, monkeypatch):
    """configure() from a pre hook of elaborate is
    applied (the stage's configuration is resolved after its pre hooks);
    once elaborate ran, a load argument elaborate reads is refused and one of
    a later stage applied."""
    f, _ = flow
    from circt_flow import sta as sta_mod
    ran = []
    monkeypatch.setattr(sta_mod, "run", lambda cfg, spec: ran.append(1))
    s = Session(f, tmp_path / "o", **KW)
    s.hooks.pre("elaborate", lambda ses: ses.configure(run_sta=True))
    quiet(s.run)
    assert ran == [1] and s.cfg.run_sta
    t = Session(f, tmp_path / "p", **KW)
    quiet(t.elaborate)
    with pytest.raises(SessionError, match="load argument partitions is read by stage elaborate"):
        t.configure(partitions=2)
    t.configure(run_vt_swap=True)
    assert t.cfg.run_vt_swap
