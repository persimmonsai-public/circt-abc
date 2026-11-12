"""The opt-in clock-tree / SDC / dual-corner options of the P&R stage
(pnr_recipe.py, pnr_tcl/cts_recipe.tcl). Hermetic: no P&R tool runs."""
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from circt_flow import orfs_pnr as op, pnr_recipe as pr, stages
from test_wire_rc import rc_pack  # noqa: F401  (fixture: an invented pack)


def test_defaults_set_nothing():
    assert pr.parse({"clock_port": "clk"}) == {}
    e = pr.env({})
    assert set(e) == {"CTS_RECIPE_TCL", "SPEF_EXPORT_TCL", "SPEF_RESTORE_PINCAP"}
    assert e["SPEF_RESTORE_PINCAP"] == "1"      # restore is the default
    assert pr.env(pr.parse({"spef_restore_pincap": False}))[
        "SPEF_RESTORE_PINCAP"] == "0"


def test_parse_and_env():
    o = pr.parse({"input_delay_min": 4, "cts_skip_fanout_le": 2,
                  "hold_margin": 3, "hold_corner": True,
                  "cts_size_gates": {"drive_re": r"^(.*_X)(\d+)(.*)$",
                                     "drives": [1, 2, 4], "tolerance": 2,
                                     "variants": ["X", "Y"], "variant_re": "Z$"}})
    e = pr.env(o, [Path("/h/a.lib"), Path("/h/b.lib")], [Path("/t/a.lib")],
               ["-no_insertion_delay"])
    assert e["CTS_SKIP_FANOUT_LE"] == "2"
    assert e["CTS_EXTRA_ARGS"] == "-no_insertion_delay"
    assert e["CTS_SIZE_GATES"] == "1" and e["CTS_GATE_DRIVES"] == "1 2 4"
    assert e["CTS_GATE_TOL"] == "2" and e["CTS_GATE_VARIANTS"] == "X Y"
    assert e["LIB_FILES_HOLD"] == "/h/a.lib /h/b.lib"
    assert e["LIB_FILES_TT"] == "/t/a.lib" and e["HOLD_MARGIN"] == "3"


@pytest.mark.parametrize("bad", [
    {"cts_size_gates": {"drives": [1]}},                       # no drive_re
    {"cts_size_gates": {"drive_re": "(a)(b)(c)", "tolerance": 1}},   # no drives
    {"cts_size_gates": {"drive_re": "(a)(b)(c)", "drives": [1]}},    # no tolerance
    {"cts_size_gates": True},
    {"cts_size_gates": {"drive_re": "(a)(b)"}},                # two groups
    {"cts_size_gates": {"drive_re": "(a)(b)(c)", "variants": ["x"]}},
    {"cts_size_gates": {"drive_re": "(a)(b)(c)", "bogus": 1}},
    {"cts_skip_fanout_le": -1}, {"input_delay_min": -4},
    {"input_delay_min": "x"}, {"repair_dont_use": ["("]},
    {"repair_dont_use": [3]}, {"spef_restore_pincap": "no"}])
def test_rejects(bad):
    with pytest.raises(pr.RecipeError):
        pr.parse(bad)


def test_hold_corner_needs_libs():
    with pytest.raises(pr.RecipeError):
        pr.env(pr.parse({"hold_corner": True}))


def test_input_delay_min_generated_sdc(tmp_path):
    sdc = tmp_path / "c.sdc"
    op.write_sdc(sdc, "top", "clk", 1000.0, 0)
    out = pr.append_input_delay_min(sdc.read_text(), 4)
    assert out.startswith(sdc.read_text())
    assert out.rstrip().endswith(
        "set_input_delay -min 4 -clock $clk_io_name [all_inputs -no_clocks]")


def test_input_delay_min_user_sdc():
    s = "create_clock -name clk -period 1 [get_ports c]\nset_input_delay 2 -clock clk [get_ports a]\n"
    out = pr.append_input_delay_min(s, 4)
    assert out.splitlines()[-1] == \
        "set_input_delay -min 4 -clock clk [all_inputs -no_clocks]"
    with pytest.raises(pr.RecipeError):
        pr.append_input_delay_min("set_false_path -from a\n", 4)


def test_orfs_stage_passes_sdc_options(tmp_path, monkeypatch):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    (tmp_path / "my.sdc").write_text("x")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "input_delay_min": 4,
                 "sdc_file": "my.sdc"}, pack_pnr={}, tie=None,
            top_module="top", clock_period_ps=1500,
            manifest=tmp_path / "designs" / "x" / "manifest.yaml",
            config_dir=tmp_path))
    got = []
    monkeypatch.setattr(op, "main", lambda argv: got.append(argv) or 0)
    stages.orfs_pnr(c)
    a = got[0]
    assert f"--sdc={tmp_path / 'my.sdc'}" in a
    assert "--input-delay-min=4.0" in a


@pytest.mark.parametrize("key,value", [
    ("cts_skip_fanout_le", 2), ("cts_flatten_root", True),
    ("repair_dont_use", ["^X"]), ("hold_corner", True),
    ("spef_restore_pincap", False), ("post_layout", {"margin_ps": 5}),
    ("cts_size_gates", {"drive_re": "^(.*_X)([0-9]+)(.*)$", "drives": [1, 2],
                        "tolerance": 0.1})])
def test_orfs_stage_refuses_pack_command_keys(tmp_path, monkeypatch, key, value):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(
            pnr={"clock_port": "clk", key: value}, pack_pnr={}, tie=None,
            top_module="top", clock_period_ps=1500,
            manifest=tmp_path / "manifest.yaml"))
    monkeypatch.setattr(op, "main", lambda argv: 0)
    with pytest.raises(ValueError, match="pnr." + key + ": needs a tech pack"):
        stages.orfs_pnr(c)


def test_orfs_main_appends_input_delay_min(tmp_path, monkeypatch):
    net = tmp_path / "t.v"
    net.write_text("module top; endmodule\n")
    monkeypatch.setattr(op, "run_orfs", lambda *a, **k: 0)
    monkeypatch.setattr(op, "collect", lambda *a, **k: {})
    sdc = tmp_path / "in.sdc"
    sdc.write_text("create_clock -name c -period 1 [get_ports clk]\n"
                   "set_input_delay 0.2 -clock c [get_ports a]\n")
    op.main([str(net), str(tmp_path / "run"), "--top", "top", "--sdc", str(sdc),
             "--input-delay-min", "4", "--target", "synth"])
    text = (tmp_path / "run" / "constraint.sdc").read_text()
    assert "set_input_delay -min 4 -clock c [all_inputs -no_clocks]" in text


def test_tcl_has_no_cell_names_and_parses():
    text = pr.TCL.read_text()
    assert not re.search(r"ASAP7|NangateOpen|sky130|_X[0-9]+\b|INVX|BUFX", text, re.I)
    assert "ICG" not in text.upper().replace("CLOCK GATE", "")
    tclsh = subprocess.run(["which", "tclsh"], capture_output=True, text=True)
    if tclsh.returncode:
        pytest.skip("no tclsh")
    r = subprocess.run(["tclsh"], input="set f [open %s]; set s [read $f]; "
                       "exit [expr {![info complete $s]}]" % pr.TCL,
                       capture_output=True, text=True)
    assert r.returncode == 0


def test_tcl_size_gates_needs_drive_re_and_picks_by_arrival(tmp_path):
    """cts_recipe_size_gates with stubbed OpenROAD commands: the smallest
    listed drive within the tolerance of the fastest wins."""
    if subprocess.run(["which", "tclsh"], capture_output=True).returncode:
        pytest.skip("no tclsh")
    stub = r'''
set ::arr {G_X1_k 0.42 G_X2_k 0.27 G_X3_k 0.265 G_X4_k 0.31}
set ::cur G_X1_k; set ::log {}
proc get_cells {args} { return inst1 }
proc get_property {o p} {
  switch $p { liberty_cell {return lc} name {return $::cur} is_buffer {return 0} is_inverter {return 0}
              direction {return output} arrival_max_rise {return [dict get $::arr $::cur]} arrival_max_fall {return 0.0} }
}
proc get_pins {args} { return pin1 }
namespace eval sta { proc is_clock {p} { return 1 } }
proc get_lib_cells {a} { return x }
proc replace_cell {i c} { set ::cur $c; lappend ::log $c }
'''
    run = ("set ::env(CTS_SIZE_GATES) 1\nset ::env(CTS_GATE_DRIVE_RE) {^(.*_X)([0-9]+)(_.*)$}\n"
           "set ::env(CTS_GATE_DRIVES) {1 2 3 4}\nset ::env(CTS_GATE_TOL) 0.01\n"
           f"source {pr.TCL}\ncts_recipe_size_gates\nputs FINAL=$::cur\n")
    def tcl(body):
        f = tmp_path / "t.tcl"
        f.write_text(body)
        return subprocess.run(["tclsh", str(f)], capture_output=True, text=True)
    r = tcl(stub + run)
    assert r.returncode == 0, r.stderr
    assert "FINAL=G_X2_k" in r.stdout, r.stdout   # X2 is within the tolerance of X3
    r = tcl(stub + run.replace("set ::env(CTS_GATE_DRIVE_RE)",
                               "set ::env(UNUSED)"))
    assert r.returncode != 0 and "CTS_GATE_DRIVE_RE" in r.stderr


def test_pack_command_gets_env_and_sdc(tmp_path):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    sdc_in = tmp_path / "in.sdc"
    sdc_in.write_text("create_clock -name clk -period 1 [get_ports c]\n"
                      "set_input_delay 2 -clock clk [get_ports a]\n")
    script = ('printf "%s|%s|%s" "$CTS_SKIP_FANOUT_LE" "$CTS_EXTRA_ARGS" '
              '"$CTS_RECIPE_TCL" > "$8/env.txt"; echo {} > "$8/pnr_metrics.json"')
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(
            pnr={"clock_port": "c", "sdc_file": "in.sdc",
                 "input_delay_min": 4, "cts_skip_fanout_le": 3,
                 "cts_args": ["-no_insertion_delay"]},
            pack_pnr={"command": ["bash", "-c", script, "x"]}, tie=None,
            top_module="top", clock_period_ps=1000, pack_root=tmp_path,
            manifest=tmp_path / "manifest.yaml", config_dir=tmp_path))
    stages.pack_pnr(c)
    got = (tmp_path / "pnr" / "env.txt").read_text().split("|")
    assert got[:2] == ["3", "-no_insertion_delay"] and got[2].endswith("cts_recipe.tcl")
    text = (tmp_path / "pnr" / "constraint.sdc").read_text()
    assert text.startswith(sdc_in.read_text())
    assert text.rstrip().endswith("[all_inputs -no_clocks]")


def test_flatten_root_opt_in():
    assert "CTS_FLATTEN_ROOT" not in pr.env(pr.parse({}))
    e = pr.env(pr.parse({"cts_flatten_root": True}))
    assert e["CTS_FLATTEN_ROOT"] == "1"
    assert "proc cts_recipe_flatten_root" in pr.TCL.read_text()


def test_repair_dont_use_env_and_tcl(tmp_path):
    o = pr.parse({"repair_dont_use": ["^FAM_A", "FAM_B_"]})
    e = pr.env(o)
    assert e["REPAIR_DONT_USE_RE"] == "^FAM_A FAM_B_"
    assert "REPAIR_DONT_USE_RE" not in pr.env(pr.parse({}))
    assert pr.parse({"repair_dont_use": "^X"})["repair_dont_use"] == ["^X"]
    assert "proc cts_recipe_dont_use" in pr.TCL.read_text()
    tclsh = subprocess.run(["which", "tclsh"], capture_output=True, text=True)
    if tclsh.returncode:
        pytest.skip("no tclsh")
    # stub the OpenSTA commands: three liberty cells, two match
    script = tmp_path / "t.tcl"
    script.write_text("""
set ::env(REPAIR_DONT_USE_RE) "^FAM_A FAM_B_"
proc get_lib_cells {p} { return {FAM_A_1 FAM_B_2 PLAIN_3} }
proc get_property {o p} { return $o }
set ::marked {}
proc set_dont_use {c} { lappend ::marked $c }
source %s
cts_recipe_dont_use
puts $::marked
""" % pr.TCL)
    r = subprocess.run(["tclsh", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "FAM_A_1 FAM_B_2" in r.stdout and "PLAIN_3" not in r.stdout.split("\n")[-2]


# ---- pnr.placement_check (placement_health wired into the P&R stage) --------

_PC_BAD = ("Total Placement Failures:         3\n"
           "[INFO DPL-0034] Detailed placement failed on the following 3 instances:\n")
_PC_RETRY_OK = ("Total Placement Failures:         3\n"
                "Total Placement Failures:         0\n")


def _flow_pc(tmp_path, rc_pack, log_text, pack_pc=None, flow_pc=None):
    """Real stages.pack_pnr with an invented command that prints log_text;
    returns (metrics dict, or the exception raised)."""
    import json
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    logf = tmp_path / "fake.log"
    logf.write_text(log_text)
    pc = f"\n  placement_check: {pack_pc}" if pack_pc else ""
    rc_pack.write_text(rc_pack.read_text() + f"""
pnr:
  command: [bash, -c, 'cat {logf}; echo {{}} > "$8/pnr_metrics.json"', x]{pc}
""")
    extra = f"\n  placement_check: {flow_pc}" if flow_pc else ""
    f = _flow(tmp_path, rc_pack)
    f.write_text(f.read_text().replace("pnr: {clock_port: clk}",
                                       f"pnr:\n  clock_port: clk{extra}"))
    cfg = FlowConfig.load(f, tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    try:
        mp = stages.pack_pnr(cfg)
    except RuntimeError as e:
        return e
    return json.loads(mp.read_text())


def test_placement_check_absent_changes_nothing(tmp_path, rc_pack):
    m = _flow_pc(tmp_path, rc_pack, _PC_BAD)
    assert m == {"status": "ok"}


def test_placement_check_warn_records_and_continues(tmp_path, rc_pack, capsys):
    m = _flow_pc(tmp_path, rc_pack, _PC_BAD, flow_pc="warn")
    assert m["placement_failures"] == 3 and m["placement_attempts"] == 1
    assert "placement_check: 3 placement failure(s)" in capsys.readouterr().out


def test_placement_check_fail_raises_only_when_final_failed(tmp_path, rc_pack):
    e = _flow_pc(tmp_path, rc_pack, _PC_BAD, flow_pc="fail")
    assert isinstance(e, RuntimeError) and "3 instance" in str(e)


def test_placement_check_fail_passes_after_good_retry(tmp_path, rc_pack):
    m = _flow_pc(tmp_path, rc_pack, _PC_RETRY_OK, flow_pc="fail")
    assert m["placement_failures"] == 0 and m["placement_attempts"] == 2


def test_placement_check_no_placement_lines(tmp_path, rc_pack, capsys):
    m = _flow_pc(tmp_path, rc_pack, "nothing relevant\n", flow_pc="warn")
    assert m["placement_attempts"] == 0 and "placement_failures" not in m
    assert "no legalization attempt" in capsys.readouterr().out


def test_placement_check_fail_without_placement_lines_fails(tmp_path, rc_pack):
    r = _flow_pc(tmp_path, rc_pack, "nothing relevant\n", flow_pc="fail")
    assert isinstance(r, stages.PlacementFailed)


@pytest.mark.parametrize("pack_pc,flow_pc,raises", [
    ("fail", None, True), ("fail", "warn", False), ("warn", "fail", True)])
def test_placement_check_flow_overrides_pack(tmp_path, rc_pack, pack_pc, flow_pc, raises):
    r = _flow_pc(tmp_path, rc_pack, _PC_BAD, pack_pc, flow_pc)
    assert isinstance(r, RuntimeError) == raises


def test_placement_check_rejects_bad_value(tmp_path, rc_pack):
    from circt_flow import profile
    with pytest.raises(pr.RecipeError, match="placement_check"):
        pr.parse({"placement_check": "maybe"})
    assert pr.parse({}) == {}
    rc_pack.write_text(rc_pack.read_text() + "\npnr:\n  placement_check: no\n")
    with pytest.raises(ValueError, match="placement_check"):
        profile.load(rc_pack)


def _env_of_pack_command(tmp_path, rc_pack, keys):
    """Invented pack with a pnr command that dumps the recipe variables it saw."""
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    rc_pack.write_text(rc_pack.read_text() + f"""
pnr:
  command: [bash, -c, 'env | grep -e ^CTS_GATE -e ^HOLD_MARGIN | sort > "$8/env.txt"; echo {{}} > "$8/pnr_metrics.json"', x]
{keys}""")
    cfg = FlowConfig.load(_flow(tmp_path, rc_pack), tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    stages.pack_pnr(cfg)
    return (cfg.pnr_dir / "env.txt").read_text().split()


GATES = {"drive_re": r"^(.*_X)(\d+)(.*)$", "drives": [1, 2], "tolerance": 2}


def test_gate_port_tol_env():
    assert "CTS_GATE_PORT_TOL" not in pr.env(pr.parse({"cts_size_gates": GATES}))
    e = pr.env(pr.parse({"cts_size_gates": GATES, "cts_gate_port_tol": 1}))
    assert e["CTS_GATE_PORT_TOL"] == "1"


def test_gate_port_tol_flow_cancels_pack_default():
    pack = {"cts_size_gates": GATES, "cts_gate_port_tol": 1}
    assert pr.parse({}, pack)["cts_gate_port_tol"] == 1
    o = pr.parse({"cts_gate_port_tol": 0}, pack)
    assert "cts_gate_port_tol" not in o


@pytest.mark.parametrize("bad", [
    {"cts_gate_port_tol": 1},                                  # needs cts_size_gates
    {"cts_size_gates": GATES, "cts_gate_port_tol": -1},
    {"cts_size_gates": GATES, "cts_gate_port_tol": "x"}])
def test_gate_port_tol_reject(bad):
    with pytest.raises(pr.RecipeError):
        pr.parse(bad)


def test_tcl_has_port_tolerance():
    t = pr.TCL.read_text()
    assert "CTS_GATE_PORT_TOL" in t and "gtol" in t


def test_pack_recipe_defaults_reach_command(tmp_path, rc_pack):
    got = _env_of_pack_command(tmp_path, rc_pack, """  cts_size_gates: {drive_re: '^(.*_X)([0-9]+)(.*)$', drives: [1, 2], tolerance: 2}
  cts_gate_port_tol: 1
  hold_margin: 0
""")
    for want in ("CTS_GATE_PORT_TOL=1", "HOLD_MARGIN=0"):
        assert want in got


def test_pack_recipe_defaults_validated(tmp_path, rc_pack):
    from circt_flow import profile
    base = rc_pack.read_text()
    for body in ("cts_gate_port_tol: 1", "cts_skip_fanout_le: -1",
                 "cts_size_gates: {bogus: 1}"):
        rc_pack.write_text(base + f"\npnr:\n  {body}\n")
        with pytest.raises(ValueError, match="pnr"):
            profile.load(rc_pack)


def _orfs_with_logs(tmp_path, monkeypatch, mode, logs):
    """stages.orfs_pnr with orfs_pnr.main replaced by a stand-in that writes
    ORFS's log layout (work/logs/<platform>/<design>/<variant>/) and its
    console log; placement_health.collect is the real one."""
    import json
    tmp_path.mkdir(parents=True, exist_ok=True)
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    pnr = tmp_path / "pnr"
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=pnr,
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "placement_check": mode}, pack_pnr={},
            tie=None, top_module="top", clock_period_ps=1500,
            manifest=tmp_path / "manifest.yaml"))

    def fake_main(argv):
        d = pnr / "work" / "logs" / "asap7" / "top" / "base"
        d.mkdir(parents=True, exist_ok=True)
        for name, text in logs.items():
            (d / name).write_text(text)
        (pnr / "orfs.log").write_text("".join(logs.values()))
        (pnr / "pnr_metrics.json").write_text(json.dumps({"status": "ok"}))
        return 0
    monkeypatch.setattr(op, "main", fake_main)
    stages.orfs_pnr(c)
    return json.loads((pnr / "pnr_metrics.json").read_text())


def test_orfs_stage_placement_check_reads_the_orfs_logs(tmp_path, monkeypatch):
    m = _orfs_with_logs(tmp_path / "a", monkeypatch, "fail",
                        {"3_5_place_dp.log": "Placement Analysis\n"
                         "legalized HPWL            210.0 u\n"})
    assert m["placement_failures"] == 0 and m["placement_attempts"] == 1
    with pytest.raises(stages.PlacementFailed, match="unlegalized"):
        _orfs_with_logs(tmp_path / "b", monkeypatch, "fail",
                        {"3_5_place_dp.log": "Total Placement Failures:   4\n"})
    m = _orfs_with_logs(tmp_path / "c", monkeypatch, "warn",
                        {"3_5_place_dp.log": "Total Placement Failures:   4\n"})
    assert m["placement_failures"] == 4


def test_placement_check_fail_without_any_attempt_fails(tmp_path, monkeypatch):
    with pytest.raises(stages.PlacementFailed, match="no legalization attempt"):
        _orfs_with_logs(tmp_path / "a", monkeypatch, "fail",
                        {"2_1_floorplan.log": "nothing here\n"})
    m = _orfs_with_logs(tmp_path / "b", monkeypatch, "warn",
                        {"2_1_floorplan.log": "nothing here\n"})
    assert "placement_failures" not in m


def test_result_files_defaults_and_pack_names(tmp_path, rc_pack):
    from circt_flow import profile
    assert pr.result_files(None) == pr.RESULT_FILES
    assert pr.result_files({"results": {"spef": "a.spef"}}) == {
        **pr.RESULT_FILES, "spef": "a.spef"}
    for bad in ({"results": {"nope": "x"}}, {"results": {"spef": "d/a.spef"}},
                {"results": {"netlist": ""}}, {"results": "x"}):
        with pytest.raises(pr.RecipeError, match="pnr.results"):
            pr.result_files(bad)
    base = rc_pack.read_text()
    rc_pack.write_text(base + "\npnr:\n  command: [x]\n  results: {netlist: a.v}\n")
    assert profile.load(rc_pack).pnr["results"] == {"netlist": "a.v"}
    rc_pack.write_text(base + "\npnr:\n  command: [x]\n  results: {netlist: d/a.v}\n")
    with pytest.raises(ValueError, match="pnr.results.netlist"):
        profile.load(rc_pack)


def test_post_layout_reads_the_pack_named_results(tmp_path, monkeypatch):
    """postlayout.run_flow looks for the files the pack names, else the
    defaults (the optimisation itself is stubbed)."""
    from circt_flow import postlayout
    seen = {}

    def fake_optimise(nl, top, out, libs, hold, tokens, fre, sdc, spef, lefs,
                      spec, unit):
        seen.update(nl=nl.name, spef=Path(spef).name, lefs=[l.name for l in lefs])
        return {"before": {}, "after": {}}
    monkeypatch.setattr(postlayout, "optimise", fake_optimise)
    monkeypatch.setattr(postlayout, "one_line", lambda rec: "")
    monkeypatch.setattr(postlayout, "spef_for_timing", lambda s, o, r: s)
    prof = SimpleNamespace(flavours_at=lambda rail: {"tokens": ["R"], "re": "x"},
                           libs_at=lambda *a, **k: [], hold_corner=None,
                           setup_corner="tt")
    from circt_flow import profile as profile_mod
    monkeypatch.setattr(profile_mod, "load", lambda p: prof)
    pnr = tmp_path / "pnr"
    pnr.mkdir()
    for n in ("a.v", "a.spef", "placed.v", "placed.spef"):
        (pnr / n).write_text("x")
    (pnr / "cells.list").write_text(str(tmp_path / "c.lef") + "\n")
    def cfg(pack_pnr):
        return SimpleNamespace(design=SimpleNamespace(
            profile="p", rail=None, top_module="top", time_unit="ns", pnr={},
            pack_pnr=pack_pnr))
    spec = postlayout.PostLayoutSpec()
    postlayout.run_flow(cfg({"command": ["x"]}), spec, pnr)
    assert seen == {"nl": "placed.v", "spef": "placed.spef", "lefs": []}
    postlayout.run_flow(cfg({"command": ["x"], "results": {
        "netlist": "a.v", "spef": "a.spef", "lef_list": "cells.list"}}), spec, pnr)
    assert seen == {"nl": "a.v", "spef": "a.spef", "lefs": ["c.lef"]}


def test_margins_and_cts_args_reach_a_pack_command_only_when_set():
    e = pr.env(pr.parse({"hold_margin": 3, "setup_margin": -4}),
               cts_args=["-no_insertion_delay", "-x"])
    assert e["HOLD_MARGIN"] == "3" and e["SETUP_MARGIN"] == "-4"
    assert e["CTS_EXTRA_ARGS"] == "-no_insertion_delay -x"
    e = pr.env(pr.parse({}))
    assert not {"HOLD_MARGIN", "SETUP_MARGIN", "CTS_EXTRA_ARGS"} & set(e)


def test_orfs_ladder_with_placement_check_is_refused(tmp_path):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "placement_check": "warn",
                 "ladder": {"param": "hold_margin", "values": [1, 2]}},
            pack_pnr={}, tie=None, top_module="top", clock_period_ps=1500,
            manifest=None, config_dir=tmp_path))
    with pytest.raises(ValueError, match="placement_check with pnr.ladder"):
        stages.orfs_pnr(c)


def test_usage_outline_matches_the_procedures(tmp_path):
    """Every procedure the outline in cts_recipe.tcl's header calls exists
    in the two Tcl files with the number of arguments the outline gives."""
    if subprocess.run(["which", "tclsh"], capture_output=True).returncode:
        pytest.skip("no tclsh")
    calls = []
    for line in pr.TCL.read_text().splitlines():
        m = re.match(r"#   ((?:cts_recipe|spef_export)_\w+)((?: \S+)*)\s*(?:;#.*)?$", line)
        if m:
            calls.append((m.group(1), len(m.group(2).split())))
    assert {c for c, _ in calls} >= {"cts_recipe_skip_nets", "cts_recipe_size_gates",
                                     "cts_recipe_read_corners", "spef_export_global_route"}
    script = tmp_path / "t.tcl"
    script.write_text("source %s\nsource %s\n" % (pr.TCL, pr.SPEF_TCL) + "".join(
        "puts \"%s [llength [info args %s]]\"\n" % (c, c) for c, _ in calls))
    r = subprocess.run(["tclsh", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = dict(l.split() for l in r.stdout.splitlines())
    for c, n in calls:
        assert int(got[c]) == n, (c, n, got[c])


def test_placement_failed_rejects_a_rung():
    from circt_flow import ladder
    assert issubclass(stages.PlacementFailed, ladder.RungRejected)


def test_post_layout_final_merges_with_the_commands_own(tmp_path, monkeypatch):
    """pack_finish: post-layout's four numbers go over a `final` the pack's
    command wrote; the command's other keys stay."""
    import json
    from circt_flow import postlayout
    out = tmp_path / "pnr"
    out.mkdir()
    (out / "pnr_metrics.json").write_text(json.dumps(
        {"setup_ws": 1.0, "final": {"hold_ws": 5.0, "route_ws": 2.0}}))
    monkeypatch.setattr(postlayout, "run_flow", lambda cfg, spec, d: {
        "after": {"wns": 0.5, "tns": 0.0, "hold_wns": 1.0, "hold_tns": 0.0,
                  "r2r_viol": 0}, "instances_changed": 1, "footprint_changed": []})
    cfg = SimpleNamespace(design=SimpleNamespace(
        pnr={"clock_port": "clk", "post_layout": {"margin_ps": 1}},
        pack_pnr={"command": ["x"]}, wire_rc=None, wire_rc_setup=None))
    m = json.loads(stages.pack_finish(cfg, out, 0, ["x"]).read_text())
    assert m["final"] == {"hold_ws": 1.0, "route_ws": 2.0, "setup_ws": 0.5,
                          "setup_tns": 0.0, "hold_tns": 0.0}
    assert m["setup_ws"] == 1.0


def test_placement_logs_validated(tmp_path, rc_pack):
    from circt_flow import profile
    assert pr.placement_logs({}) is None
    assert pr.placement_logs({"placement_logs": ["logs/*.log"]}) == ["logs/*.log"]
    for bad in ([], "x", ["/abs/*.log"], ["../up/*.log"], [""], [" a"], [1]):
        with pytest.raises(pr.RecipeError, match="pnr.placement_logs"):
            pr.placement_logs({"placement_logs": bad})
    base = rc_pack.read_text()
    rc_pack.write_text(base + "\npnr:\n  command: [x]\n  placement_logs: [logs/*.log]\n")
    assert profile.load(rc_pack).pnr["placement_logs"] == ["logs/*.log"]
    rc_pack.write_text(base + "\npnr:\n  command: [x]\n  placement_logs: [/x.log]\n")
    with pytest.raises(ValueError, match="pnr.placement_logs"):
        profile.load(rc_pack)


def _pack_logs(tmp_path, rc_pack, patterns, mode):
    """A pack command whose stage logs are in a directory the pack names
    (clean), next to another log with failures that is not one of them."""
    import json
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    script = ('mkdir -p "$8/other" "$8/logs"; '
              'echo "Total Placement Failures: 4" > "$8/other/place.log"; '
              'echo "Total Placement Failures: 0" > "$8/logs/place.log"; '
              'echo "Total Placement Failures: 4"; '
              'echo {} > "$8/pnr_metrics.json"')
    rc_pack.write_text(rc_pack.read_text() + f"""
pnr:
  command: [bash, -c, '{script}', x]
  placement_logs: {json.dumps(patterns)}
""")
    f = _flow(tmp_path, rc_pack)
    f.write_text(f.read_text().replace("pnr: {clock_port: clk}",
                                       f"pnr:\n  clock_port: clk\n  placement_check: {mode}"))
    cfg = FlowConfig.load(f, tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    return json.loads(stages.pack_pnr(cfg).read_text())


def test_placement_check_reads_the_logs_the_pack_names(tmp_path, rc_pack):
    m = _pack_logs(tmp_path, rc_pack, ["logs/*.log"], "fail")
    assert m["placement_attempts"] == 1 and m["placement_failures"] == 0


def test_placement_logs_that_match_nothing_fail_under_fail(tmp_path, rc_pack, capsys):
    with pytest.raises(stages.PlacementFailed, match=r"placement_logs \['nowhere/\*\.log'\]"):
        _pack_logs(tmp_path, rc_pack, ["nowhere/*.log"], "fail")


def test_post_layout_fails_when_the_named_results_are_missing(tmp_path):
    """A command that leaves its netlist / SPEF under other names than the
    pack's pnr.results (or the defaults) is an error naming the key and the
    files, not a skipped post-layout stage."""
    from circt_flow import postlayout
    pnr = tmp_path / "pnr"
    pnr.mkdir()
    for n in ("old.v", "old.spef"):
        (pnr / n).write_text("x")

    def cfg(pack_pnr):
        return SimpleNamespace(design=SimpleNamespace(
            profile=None, rail=None, top_module="top", time_unit="ns",
            pack_pnr=pack_pnr))
    spec = postlayout.PostLayoutSpec()
    with pytest.raises(RuntimeError, match=r"placed\.v / placed\.spef.*"
                       r"pnr\.results is not set"):
        postlayout.run_flow(cfg({"command": ["x"]}), spec, pnr)
    with pytest.raises(RuntimeError, match=r"no new\.spef.*pnr\.results names them"):
        postlayout.run_flow(cfg({"command": ["x"], "results": {
            "netlist": "old.v", "spef": "new.spef"}}), spec, pnr)


@pytest.mark.parametrize("hooks,ok", [(True, True), (False, False)])
def test_pack_command_ladder_over_hold_margin_shares_the_placement(
        tmp_path, rc_pack, hooks, ok):
    """HOLD_MARGIN reaches a pack command, so a ladder over hold_margin with a
    restart must leave that variable out of the placement's plan: the flow's
    hooks name it (without them the rungs' plans differ)."""
    from test_wire_rc import _flow
    from circt_flow import ladder
    from circt_flow.config import FlowConfig
    rc_pack.write_text(rc_pack.read_text() + """
pnr:
  command: [bash, -c, 'true', x]
  stage_restart: [place, cts]
""")
    cfg = FlowConfig.load(_flow(tmp_path, rc_pack), tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    spec = ladder.LadderSpec(param="hold_margin", values=(1, 2), from_stage="cts")
    be = ladder.CommandBackend(cfg, spec, stages.ladder_hooks() if hooks
                               else ladder.LadderHooks())
    plans = []
    for i, v in enumerate(spec.values):
        d = tmp_path / f"plan{i}"
        d.mkdir()
        plans.append(be.plan(d, v))
    assert (be.diff(plans[0], plans[1]) == []) is ok


def test_own_hooks_started_from_the_flows_keep_the_plan_helpers(tmp_path, rc_pack):
    """The documented way to add a hook: dataclasses.replace on the flow's
    own hooks keeps param_env_names, so a ladder over hold_margin with a
    restart still shares the placement."""
    import dataclasses
    from test_wire_rc import _flow
    from circt_flow import ladder
    from circt_flow.config import FlowConfig
    rc_pack.write_text(rc_pack.read_text() + """
pnr:
  command: [bash, -c, 'true', x]
  stage_restart: [place, cts]
""")
    cfg = FlowConfig.load(_flow(tmp_path, rc_pack), tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    seen = []
    hooks = dataclasses.replace(stages.ladder_hooks(),
                                finish_rung=lambda c, d: seen.append(d))
    assert hooks.param_env_names is stages.ladder_hooks().param_env_names
    spec = ladder.LadderSpec(param="hold_margin", values=(1, 2), from_stage="cts")
    be = ladder.CommandBackend(cfg, spec, hooks)
    plans = []
    for i, v in enumerate(spec.values):
        d = tmp_path / f"plan{i}"
        d.mkdir()
        plans.append(be.plan(d, v))
    assert be.diff(plans[0], plans[1]) == []


@pytest.mark.parametrize("key", ["results: {netlist: a.v}",
                                 "placement_logs: [logs/*.log]"])
def test_command_outputs_need_a_command(tmp_path, rc_pack, key):
    from circt_flow import profile
    rc_pack.write_text(rc_pack.read_text() + f"\npnr:\n  {key}\n")
    with pytest.raises(ValueError, match="needs pnr.command"):
        profile.load(rc_pack)



@pytest.mark.parametrize("given,num", [("0.05", 0.05), ("1e-2", 0.01), (3, 3.0),
                                       (" -0.5", -0.5)])
def test_margins_take_numeric_strings(tmp_path, monkeypatch, rc_pack, given, num):
    """hold_margin / setup_margin as a quoted number load as before and reach
    ORFS as given (its argument parser reads the number)."""
    from circt_flow import profile
    o = pr.parse({"hold_margin": given, "setup_margin": given})
    assert o["hold_margin"] == num and o["setup_margin"] == num
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(pnr={"clock_port": "clk", "hold_margin": given},
                               pack_pnr={}, tie=None, top_module="top",
                               clock_period_ps=1500, manifest=None,
                               config_dir=tmp_path))
    got = []
    monkeypatch.setattr(op, "main", lambda argv: got.append(argv) or 0)
    stages.orfs_pnr(c)
    assert f"--hold-margin={given}" in got[0]
    rc_pack.write_text(rc_pack.read_text() + f"\npnr:\n  hold_margin: '{given}'\n")
    assert profile.load(rc_pack).pnr["hold_margin"] == str(given)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan", "x", True])
def test_margins_refuse_non_numbers_and_non_finite(bad):
    with pytest.raises(pr.RecipeError, match="hold_margin"):
        pr.parse({"hold_margin": bad})


def _tcl_balanced(line):
    depth = {"[": 0, "{": 0}
    for ch in line:
        if ch in "[{":
            depth[ch] += 1
        elif ch == "]":
            depth["["] -= 1
        elif ch == "}":
            depth["{"] -= 1
        assert min(depth.values()) >= 0, line
    return all(v == 0 for v in depth.values())


@pytest.mark.parametrize("sdc,clock", [
    # the clock the inputs are already timed against, in its own spelling
    ("create_clock -name clk -period 1 [get_ports c]\n"
     "set_input_delay 2 -clock [get_clocks clk] [get_ports a]\n", "[get_clocks clk]"),
    ("create_clock -name clk -period 1 [get_ports c]\n"
     "set_input_delay 2 -clock [get_clocks {clk}] [get_ports a]\n", "[get_clocks {clk}]"),
    # a comment does not count
    ("# set_input_delay 2 -clock old [get_ports a]\n"
     "create_clock -name clk -period 1 [get_ports c]\n"
     "set_input_delay 2 -clock clk [get_ports a]\n", "clk"),
    # -name after the source, -clock before the value
    ("create_clock [get_ports clk] -name core -period 1\n"
     "set_input_delay -clock core 2 [get_ports a]\n", "core"),
    # no input delay: the one clock, named after its port when it has no -name
    ("create_clock -period 1 [get_ports clk]\n", "[get_clocks {clk}]"),
    ("create_clock -period 1 -name core [get_ports clk]  ;# the core clock\n",
     "[get_clocks {core}]")])
def test_input_delay_min_finds_the_clock(sdc, clock):
    line = pr.append_input_delay_min(sdc, 4).splitlines()[-1]
    assert line == f"set_input_delay -min 4 -clock {clock} [all_inputs -no_clocks]"
    assert _tcl_balanced(line)


@pytest.mark.parametrize("sdc,msg", [
    ("create_clock -name a -period 1 [get_ports x]\n"
     "create_clock -name b -period 1 [get_ports y]\n", "several clocks"),
    ("create_clock -name a -period 1 [get_ports x]\n"
     "set_input_delay 1 -clock a [get_ports p]\n"
     "set_input_delay 1 -clock b [get_ports q]\n", "several clocks"),
    ("# create_clock -name a -period 1 [get_ports x]\n", "no clock")])
def test_input_delay_min_refuses_an_unclear_clock(sdc, msg):
    with pytest.raises(pr.RecipeError, match=msg):
        pr.append_input_delay_min(sdc, 4)


@pytest.mark.parametrize("sdc,clock", [
    # a plainly quoted or braced literal name is that name
    ('create_clock -name "core" -period 1 [get_ports c]\n', "[get_clocks {core}]"),
    ("create_clock -name {core} -period 1 [get_ports c]\n", "[get_clocks {core}]"),
    # one clock however it is spelt
    ("create_clock -name clk -period 1 [get_ports c]\n"
     "set_input_delay 2 -clock clk [get_ports a]\n"
     "set_input_delay 2 -clock [get_clocks clk] [get_ports b]\n"
     'set_input_delay 2 -clock "clk" [get_ports d]\n', "clk"),
    # no -name: STA names the clock after its (first) source object
    ("create_clock -period 1 clk\n", "[get_clocks {clk}]"),
    ("create_clock -period 1 {clk}\n", "[get_clocks {clk}]"),
    ("create_clock -period 1 [get_ports {clk}]\n", "[get_clocks {clk}]"),
    ("create_clock -period 1 [get_pins u_div/q_reg/Q]\n", "[get_clocks {u_div/q_reg/Q}]"),
    ("create_clock -waveform {0 0.5} -period 1 [get_ports clk]\n", "[get_clocks {clk}]"),
    # a variable set once at the top of the SDC keeps its value at the end
    ("set v vclk\ncreate_clock -name $v -period 1\n"
     "set_input_delay 2 -clock $v [get_ports a]\n", "$v")])
def test_input_delay_min_names_the_clock(sdc, clock):
    line = pr.append_input_delay_min(sdc, 4).splitlines()[-1]
    assert line == f"set_input_delay -min 4 -clock {clock} [all_inputs -no_clocks]"
    assert _tcl_balanced(line)


@pytest.mark.parametrize("sdc,msg", [
    # a name only substitution gives: refused, not wrapped in braces
    ("set clk_name core\ncreate_clock -name $clk_name -period 1 [get_ports c]\n",
     "substitution"),
    ('create_clock -name "$n" -period 1 [get_ports c]\n', "substitution"),
    ("create_clock -name [lindex $l 0] -period 1 [get_ports c]\n", "substitution"),
    ("foreach c {a b} {\n create_clock -name $c -period 1 [get_ports $c]\n}\n",
     "substitution"),
    # a variable that is not set once (a loop variable) is not used
    ("create_clock -name a -period 1 [get_ports x]\n"
     "foreach c {a} {\n set_input_delay 1 -clock $c [get_ports p]\n}\n",
     "substitution"),
    # no -name and a source that is not one literal object
    ("create_clock -period 1 [get_ports clk*]\n", "no -name"),
    ("create_clock -period 1 [get_ports {c1 c2}]\n", "no -name"),
    ("create_clock -period 1 $p\n", "no -name"),
    ("create_clock -name clk -period 1 [get_ports c]\n"
     "set_input_delay 1 -clock clk [get_ports p]\n"
     "set_input_delay 1 -clock [get_clocks other] [get_ports q]\n", "several clocks")])
def test_input_delay_min_refuses_a_clock_it_cannot_name(sdc, msg):
    with pytest.raises(pr.RecipeError, match=msg):
        pr.append_input_delay_min(sdc, 4)


def test_a_successful_pack_command_is_not_failed_by_the_scans(tmp_path, rc_pack, capsys):
    """Files the default scans cannot use (a SPEF with min:typ:max values,
    a directory named .spef, a corrupt .sdf.gz) are reported and left
    alone; the stage succeeds."""
    import json
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    script = ('printf "*SPEF \\"ieee 1481-1999\\"\\n*C_UNIT 1 PF\\n*D_NET n 0.01\\n*CONN\\n'
              '*I u:A I\\n*CAP\\n1 n:1 0.001:0.002:0.003\\n*END\\n" > "$8/t.spef"; '
              'echo "SPEF_EXPORT_BEGIN t.spef" > "$8/x.log"; mkdir -p "$8/d.spef"; '
              'printf "junk" > "$8/b.sdf.gz"; echo {} > "$8/pnr_metrics.json"')
    rc_pack.write_text(rc_pack.read_text() + f"""
pnr:
  command: [bash, -c, '{script}', x]
""")
    cfg = FlowConfig.load(_flow(tmp_path, rc_pack), tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    m = json.loads(stages.pack_pnr(cfg).read_text())
    assert m["status"] == "ok"
    out = capsys.readouterr().out
    assert "t.spef: left as written" in out and "b.sdf.gz: could not be read" in out


def test_an_all_zero_sdf_is_reported_after_a_pack_command(tmp_path, rc_pack, capsys):
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    cells = "".join('(CELL (CELLTYPE \\"INV\\") (INSTANCE g%d) (DELAY (ABSOLUTE '
                    '(IOPATH A Y (0.01::0.01) (0.01::0.01)))))\\n' % i for i in range(4))
    ics = "".join('(INTERCONNECT a g%d/A (0::0) (0::0))\\n' % i for i in range(4))
    script = ('printf "(DELAYFILE\\n(CELL (CELLTYPE \\"top\\") (INSTANCE) (DELAY (ABSOLUTE\\n'
              + ics + ')))\\n' + cells + ')\\n" > "$8/z.sdf"; echo {} > "$8/pnr_metrics.json"')
    rc_pack.write_text(rc_pack.read_text() + f"""
pnr:
  command: [bash, -c, '{script}', x]
""")
    cfg = FlowConfig.load(_flow(tmp_path, rc_pack), tmp_path / "o")
    cfg.openroad_netlist.parent.mkdir(parents=True, exist_ok=True)
    cfg.openroad_netlist.write_text("module top; endmodule\n")
    stages.pack_pnr(cfg)
    out = capsys.readouterr().out
    assert "z.sdf:" in out and "are zero" in out
    assert "written with the design's parasitics loaded" in out

