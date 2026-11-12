"""The ORFS stage's Vt controls: `pnr.vt_swap: false` and `pnr.dont_use`
reach orfs_pnr's command line, config.mk and the dont-use hook (ORFS itself
is not run here; the asap7-mvt example runs it for real)."""
from pathlib import Path
from types import SimpleNamespace

from circt_flow import orfs_pnr as op, stages

_REAL_MAIN = op.main      # the tests below replace op.main


def cfg(tmp_path, pnr):
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    return SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(pnr=pnr, pack_pnr={}, tie=None,
                               top_module="top", clock_period_ps=1500))


def opt_values(argv, name):
    """The values of an option, whichever way it is spelled (`--opt v` or
    `--opt=v`)."""
    out = []
    for i, a in enumerate(argv):
        if a == name:
            out.append(argv[i + 1])
        elif a.startswith(name + "="):
            out.append(a[len(name) + 1:])
    return out


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
    assert opt_values(argv, "--dont-use") == ["*_SL", "*_L"]


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
    assert opt_values(argv, "--cts-buffer") == ["B1"]        # from the pack
    assert opt_values(argv, "--hold-margin") == ["20"]       # flow.yaml wins
    assert "--cts-arg=-no_insertion_delay" in argv
    # argparse accepts that spelling (a separate "-no_..." value is read as an option)
    ns = pnr_mod_parse(argv)
    assert ns.cts_arg == ["-no_insertion_delay"]


def pnr_mod_parse(argv):
    """The ORFS stage's own parser on an argv the flow built."""
    import argparse
    seen = {}
    real = argparse.ArgumentParser.parse_args

    def spy(self, args=None, namespace=None):
        ns = real(self, args, namespace)
        seen["ns"] = ns
        return ns
    argparse.ArgumentParser.parse_args = spy
    try:
        _REAL_MAIN([*argv, "--plan"])
    finally:
        argparse.ArgumentParser.parse_args = real
    return seen["ns"]


def test_every_option_value_reaches_the_real_parser_even_when_it_starts_with_a_dash(tmp_path, monkeypatch):
    """`--hold-margin -1e-05` made argparse fail like `--cts-arg -flag` did:
    every value travels as --option=value."""
    c = cfg(tmp_path, {"clock_port": "clk", "hold_margin": -1e-05,
                       "setup_margin": -2e-05, "clock_latency": -1.5,
                       "cts_cluster_diameter": -3, "cts_cluster_size": -4,
                       "cts_args": ["-no_insertion_delay", "-x 5"],
                       "cts_buffers": ["-B"], "dont_use": ["-D*"],
                       "utilization": 40, "place_density": 0.5,
                       "set": {"A": "-1"}, "tie_hi": "H:Z", "tie_lo": "L:Z"})
    argv = argv_of(monkeypatch, c)
    ns = pnr_mod_parse(argv)
    assert ns.hold_margin == -1e-05 and ns.setup_margin == -2e-05
    assert ns.clock_latency == -1.5 and ns.cts_cluster_diameter == -3
    assert ns.cts_cluster_size == -4 and ns.cts_arg == ["-no_insertion_delay", "-x 5"]
    assert ns.cts_buffer == ["-B"] and ns.dont_use == ["-D*"] and ns.set == ["A=-1"]
    assert ns.tie_hi == "H:Z" and ns.tie_lo == "L:Z" and ns.top == "top"
    # no separate "--option value" pair is left in the line
    for a in argv[2:]:
        assert not (a.startswith("--") and "=" not in a) or a in (
            "--no-vt-swap",), a
