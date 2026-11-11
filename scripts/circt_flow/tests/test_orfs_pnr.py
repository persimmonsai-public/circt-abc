"""The ORFS stage's Vt controls: `pnr.vt_swap: false` and `pnr.dont_use`
reach orfs_pnr's command line, config.mk and the dont-use hook (ORFS itself
is not run here; the asap7-mvt example runs it for real)."""
from pathlib import Path
from types import SimpleNamespace

from circt_flow import orfs_pnr as op, stages


def cfg(tmp_path, pnr):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    return SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(pnr=pnr, pack_pnr={}, tie=None,
                               top_module="top", clock_period_ps=1500))


def argv_of(monkeypatch, c):
    got = []
    monkeypatch.setattr(op, "main", lambda argv: got.append(argv) or 0)
    stages.orfs_pnr(c)
    return got[0]


def test_stage_default_passes_nothing(tmp_path, monkeypatch):
    argv = argv_of(monkeypatch, cfg(tmp_path, {"clock_port": "clk"}))
    assert "--no-vt-swap" not in argv and "--dont-use" not in argv


def test_stage_passes_vt_controls(tmp_path, monkeypatch):
    argv = argv_of(monkeypatch, cfg(tmp_path, {
        "clock_port": "clk", "vt_swap": False,
        "dont_use": ["*_SL", "*_L"]}))
    assert "--no-vt-swap" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--dont-use"] \
        == ["*_SL", "*_L"]


def test_no_vt_swap_settings(tmp_path):
    kv = dict(op.vt_settings(tmp_path, True, []))
    # the floorplan/CTS/global-route calls AND the post-GRT vt_swap pass
    assert kv == {"SKIP_VT_SWAP": "1", "SKIP_CRIT_VT_SWAP": "1",
                  "OPT_POST_GRT_WNS": "0"}
    assert op.vt_settings(tmp_path, False, []) == []


def test_dont_use_hook(tmp_path):
    kv = dict(op.vt_settings(tmp_path, False, ["*_ASAP7_75t_SL", "FOO*"]))
    hook = Path(kv["PRE_CTS_TCL"])
    assert set(kv) == {f"PRE_{s}_TCL" for s in op.REPAIR_STEPS}
    assert len(set(kv.values())) == 1 and hook.parent == tmp_path
    text = hook.read_text()
    assert "set_dont_use {*_ASAP7_75t_SL FOO*}" in text
    # Guarded: a platform without DONT_USE_CELLS must not make the hook fail.
    assert "info exists ::env(DONT_USE_CELLS)" in text
    assert "*_ASAP7_75t_SL FOO*" in text


def test_main_writes_config(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "run_orfs", lambda *a, **k: 0)
    net = tmp_path / "n.v"
    net.write_text("module top; endmodule\n")
    run = tmp_path / "run"
    op.main([str(net), str(run), "--top", "top", "--clock-port", "clk",
             "--clock-period", "1500", "--no-vt-swap",
             "--dont-use", "*_SL", "--set", "OPT_POST_GRT_WNS=1"])
    lines = (run / "config.mk").read_text().splitlines()
    assert "export SKIP_VT_SWAP = 1" in lines
    # an explicit --set comes after the flag's value, so it wins
    assert lines.index("export OPT_POST_GRT_WNS = 1") > \
        lines.index("export OPT_POST_GRT_WNS = 0")
    assert f"export PRE_GLOBAL_ROUTE_TCL = {run / 'dont_use.tcl'}" in lines


def test_cts_settings_default_is_empty():
    assert op.cts_settings([], None, None, []) == []
    assert op.margin_settings(None, None) == []


def test_cts_settings_replace_ctsargs_with_defaults_kept():
    kv = dict(op.cts_settings(["BUFA", "BUFB"], 20, 50.0,
                              ["-no_insertion_delay"]))
    a = kv["CTS_ARGS"]
    assert a.startswith("-sink_clustering_enable -repair_clock_nets")
    assert "-buf_list {BUFA BUFB}" in a
    assert "-sink_clustering_size 20" in a
    assert "-sink_clustering_max_diameter 50" in a
    assert a.endswith("-no_insertion_delay")


def test_margins():
    assert dict(op.margin_settings(10, -5)) == {
        "HOLD_SLACK_MARGIN": "10", "SETUP_SLACK_MARGIN": "-5"}


def test_stage_passes_cts_and_margin_controls(tmp_path, monkeypatch):
    c = cfg(tmp_path, {"clock_port": "clk", "cts_args": ["-no_insertion_delay"],
                       "hold_margin": 20})
    c.design.pack_pnr = {"cts_buffers": ["B1"], "hold_margin": 5}
    argv = argv_of(monkeypatch, c)
    assert argv[argv.index("--cts-buffer") + 1] == "B1"      # from the pack
    assert argv[argv.index("--hold-margin") + 1] == "20"     # flow.yaml wins
    assert argv[argv.index("--cts-arg") + 1] == "-no_insertion_delay"
