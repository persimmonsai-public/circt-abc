"""The STA stage: its config, SDC generation per domain, port tracing,
per-rail renaming, log parsing, and the views it builds (OpenSTA itself is
stubbed out here; the mvt-mini example runs it for real)."""
import json

import pytest

from circt_flow import intent, profile, sta, vnetlist as vn
from circt_flow.config import FlowConfig
from circt_flow.cli import _build_parser
from circt_flow.modes import Clock, IODelay, Mode
from circt_flow.tests.test_intent import NETLIST, design


def test_parse_sta():
    assert sta.parse_sta(None, "f") == sta.StaSpec()
    assert sta.parse_sta({"enabled": False}, "f").enabled is False
    s = sta.parse_sta({"corners": ["ss"], "modes": ["func"], "chip": False},
                      "f")
    assert (s.corners, s.modes, s.chip, s.enabled) == (("ss",), ("func",),
                                                       False, True)
    with pytest.raises(sta.StaError, match="sta.hold is not a key"):
        sta.parse_sta({"hold": True}, "f")
    with pytest.raises(sta.StaError, match="list of names"):
        sta.parse_sta({"corners": "ss"}, "f")
    with pytest.raises(sta.StaError, match="true or false"):
        sta.parse_sta({"chip": "yes"}, "f")


def _flow(tmp_path, pack, extra=""):
    (tmp_path / "func.sdc").write_text(
        "create_clock -name c -period 1.0 [get_ports clk]\n"
        "set_input_delay 0.2 -clock c [get_ports {a b}]\n"
        "set_output_delay 0.3 -clock c [get_ports out1]\n")
    f = tmp_path / "flow.yaml"
    f.write_text(f"""
design: {{top_module: top, clock_period_ps: 1000}}
pdk: {{profile: {pack}}}
modes: {{func: func.sdc}}
mapping_target: {{flop_overhead: false}}
power:
  domains:
    - {{name: lo, rail: LO, instances: [u_lo]}}
    - {{name: h2, rail: HI2, instances: [u_h2]}}
pnr: {{clock_port: clk}}
{extra}""")
    return f


def test_wanted(tmp_path, mv_pack):
    f = _flow(tmp_path, mv_pack)
    assert sta.wanted(FlowConfig.load(f, tmp_path / "o")) is None
    assert sta.wanted(FlowConfig.load(f, tmp_path / "o", run_sta=True)) == \
        sta.StaSpec()
    f2 = _flow(tmp_path, mv_pack, "sta: {enabled: false, corners: [default]}")
    cfg = FlowConfig.load(f2, tmp_path / "o")
    assert cfg.design.sta.corners == ("default",)
    assert sta.wanted(cfg) is None
    assert sta.wanted(FlowConfig.load(f2, tmp_path / "o",
                                      run_sta=True)).corners == ("default",)
    f3 = _flow(tmp_path, mv_pack, "sta: {}")
    assert sta.wanted(FlowConfig.load(f3, tmp_path / "o")) == sta.StaSpec()
    assert _build_parser().parse_args(["f", "o", "--sta"]).sta is True
    assert FlowConfig.load(f, tmp_path / "o", run_sta=True) \
        .load_overrides["run_sta"] is True


def test_names_checked_at_load(tmp_path, mv_pack):
    # A corner or mode the run lacks fails when the config loads, not after
    # synthesis; so does a bad boundary_budget.
    for sec, msg in (("sta: {corners: [x]}", "sta.corners: x not among"),
                     ("sta: {modes: [turbo]}", "sta.modes: turbo not among"),
                     ("sta: {boundary_budget: 1.5}", "boundary_budget"),
                     ("sta: {boundary_budget: true}", "boundary_budget")):
        with pytest.raises(sta.StaError, match=msg):
            FlowConfig.load(_flow(tmp_path, mv_pack, sec), tmp_path / "o")
    assert FlowConfig.load(_flow(tmp_path, mv_pack,
                                 "sta: {boundary_budget: 0.25}"),
                           tmp_path / "o").design.sta.boundary_budget == 0.25


def test_parse_log_fatal_warnings():
    ok = ("CIRCTFLOW-BEGIN\nworst slack max 0.1\ntns max 0\n"
          "worst slack min 0.2\nCIRCTFLOW-END\n")
    assert "errors" not in sta.parse_log(ok, 1.0, "domain")
    for w in ("Warning 198: a.v line 5, module MX not found. Creating black "
              "box for u1.",
              "Warning 366: f.sdc line 8, port 'nosuch' not found.",
              "Warning 473: f.sdc line 9, no valid objects specified for -to."):
        r = sta.parse_log(w + "\n" + ok, 1.0, "domain")
        assert r["errors"] == [w] and r["sta_warnings"] == [w]
    r = sta.parse_log("Warning 1: something harmless\n" + ok, 1.0, "domain")
    assert "errors" not in r and r["sta_warnings"]


def test_target_ports():
    ins, outs = ["clk", "a", "b"], ["y", "z"]
    assert sta.target_ports("[get_ports {a b}]", ins, outs) == {"a", "b"}
    assert sta.target_ports("[get_ports a[3]]", ins, outs) == {"a"}
    assert sta.target_ports("[get_ports {*}]", ins, outs) == set(ins + outs)
    assert sta.target_ports("[all_outputs]", ins, outs) == {"y", "z"}
    assert sta.target_ports(
        "[delete_from_list [all_inputs] [get_ports clk]]", ins, outs) == \
        {"a", "b"}
    assert sta.target_ports("[get_pins u/a]", ins, outs) is None


def test_trace_ports_through_shifters(mv_pack):
    prof = profile.load(mv_pack)
    des = design(prof)
    cs = intent.find_crossings(des)
    text, recs = intent.insert_level_shifters(design(prof), cs)
    new = design(prof, text, {r["instance"]: (r["from_domain"],
                                              r["to_domain"]) for r in recs})
    ls = {"TLSHL": ("A", "Y"), "TLSLH": ("A", "Y")}
    for d in (des, new):
        r = sta.trace_ports(d.tmod, d.pin_dir, "u_lo", ls)
        assert r == {"clk": {"clk"}, "a": {"a", "b"}, "y": {"out3"},
                     "z": set()}
        assert sta.trace_ports(d.tmod, d.pin_dir, "u_h2", ls) == {
            "clk": {"clk"}, "d": set(), "q": {"out1"}}


def test_domain_sdc():
    mod = vn.parse(NETLIST)["lo_core"]
    mode = Mode("func", None, {
        "c": Clock("c", 1000.0, ("clk",)),
        "v": Clock("v", 2000.0, ())},
        (IODelay("input", 200.0, "c", "[get_ports {a b}]"),
         IODelay("input", 250.0, "v", "[get_ports b]", "max"),
         IODelay("output", 300.0, "c", "[get_ports out3]"),
         IODelay("input", 1.0, "c", "[get_pins x/y]")))
    reach = {"clk": {"clk"}, "a": {"a", "b"}, "y": {"out3"}, "z": set()}
    text, own, warns = sta.domain_sdc(mode, mod, reach, ["clk", "a", "b"],
                                      ["out1", "out2", "out3"], "ns")
    lines = text.splitlines()
    assert own == ["c"]
    assert "create_clock -name c -period 1 [get_ports {clk}]" in lines
    assert any(l.startswith("create_clock -name v -period 2  ;# virtual")
               for l in lines)
    assert any(l.startswith("set_input_delay 0.2 -clock c [get_ports {a}]")
               for l in lines)
    assert any(l.startswith("set_input_delay 0.25 -clock v -max -add_delay "
                            "[get_ports {a}]") for l in lines)
    assert any(l.startswith("set_output_delay 0.3 -clock c [get_ports {y}]")
               for l in lines)
    assert any(l.startswith("set_output_delay 0 -clock c [get_ports {z}]  "
                            ";# boundary") for l in lines)
    assert not any("{clk}" in l and "delay" in l for l in lines)
    assert any("not understood" in w for w in warns)
    # A mode false path from a top-level input reaches the domain port it
    # drives (a: reached from top ports a, b).
    fmode = Mode("func", None, mode.clocks, mode.io_delays, (), ("b",))
    text_ff, _, _ = sta.domain_sdc(fmode, mod, reach, ["clk", "a", "b"],
                                   ["out1", "out2", "out3"], "ns")
    assert "set_false_path -from [get_ports {a}]  ;# from b" in text_ff
    assert "set_false_path" not in text
    # The budget: a pure boundary port gets that share of the period.
    text, _, _ = sta.domain_sdc(mode, mod, reach,
                                ["clk", "a", "b"], ["out1", "out2", "out3"],
                                "ns", 0.5)
    lines = text.splitlines()
    assert any(l.startswith("set_output_delay 0.5 -clock c [get_ports {z}]  "
                            ";# boundary (budget 0.5") for l in lines)
    # Several -min delays reaching one port: the smallest (hold's worst
    # case); -max ones keep the largest.
    mmode = Mode("func", None, mode.clocks, (
        IODelay("input", 40.0, "c", "[get_ports a]", "min"),
        IODelay("input", 10.0, "c", "[get_ports b]", "min"),
        IODelay("input", 300.0, "c", "[get_ports a]", "max"),
        IODelay("input", 500.0, "c", "[get_ports b]", "max")))
    text, _, _ = sta.domain_sdc(mmode, mod, reach, ["clk", "a", "b"],
                                ["out1", "out2", "out3"], "ns")
    assert "set_input_delay 0.5 -clock c -max [get_ports {a}]" in text
    assert "set_input_delay 0.01 -clock c -min -add_delay [get_ports {a}]" \
        in text
    # No clock reaches the domain: a warning, no boundary delays.
    text, own, warns = sta.domain_sdc(mode, mod, {}, ["clk"], [], "ns")
    assert own == [] and any("unclocked" in w for w in warns)


def test_boundary_lib_parses():
    from circt_flow.modes import parse_group
    mods = vn.parse(NETLIST)
    g = parse_group(sta.boundary_lib([mods["lo_core"], mods["hi2_core"]]))
    cells = {c.args[0]: c for c in g.sub("cell")}
    assert set(cells) == {"lo_core", "hi2_core"}
    assert [b.args[0] for b in cells["lo_core"].sub("bus")] == ["a"]
    assert {p.args[0]: p.attrs["direction"]
            for p in cells["lo_core"].sub("pin")} == {
        "clk": "input", "y": "output", "z": "output"}
    assert [t.args[0] for t in g.sub("type")] == ["bus_1_0"]


def test_rename(mv_pack):
    lib = (mv_pack.parent / "lib" / "lo.lib").read_text()
    t, names = sta.rename_liberty(lib, "LO")
    assert names == {"TBUF", "TINV", "TAND", "TDFF"}
    assert "library (LO__t_lo)" in t and "cell (LO__TAND)" in t
    assert "cell (TAND)" not in t
    out = sta.rename_netlist(NETLIST, {"lo_core": "LO", "top": "HI"},
                             {"LO": names, "HI": {"TAND"}})
    mods = vn.parse(out)
    assert {i.cell for i in mods["lo_core"].instances} == {
        "LO__TAND", "LO__TDFF", "LO__TINV"}
    assert {i.cell for i in mods["hi2_core"].instances} == {"TDFF"}
    assert {i.cell for i in mods["top"].instances} == {
        "lo_core", "hi2_core", "HI__TAND"}
    assert sta.submodules(mods, "top") == ["top", "hi2_core", "lo_core"]


LOG = """\
Warning: something
CIRCTFLOW-BEGIN
worst slack max -0.1234
tns max -0.5000
worst slack min 0.0100
hold r2r 0.0300
hold in 0.0100
hold out none
CIRCTFLOW-ENDPOINTS
max_delay/setup group c

                                     Required  Actual
Endpoint                               Delay   Delay   Slack
------------------------------------------------------------
u_lo/f0/D (LO__TDFF)                     0.98    1.10   -0.12 (VIOLATED)
out1 (output)                            0.70    0.10    0.60 (MET)
CIRCTFLOW-END
"""


def test_parse_log():
    r = sta.parse_log(LOG, 1.0, "chip")
    assert (r["wns_ns"], r["tns_ns"], r["hold_wns_ns"]) == (-0.1234, -0.5,
                                                            0.01)
    assert r["endpoints"] == [("u_lo/f0/D", -0.12), ("out1", 0.6)]
    assert (r["hold_r2r_ns"], r["hold_in_ns"], r["hold_out_ns"]) == (
        0.03, 0.01, None)
    r = sta.parse_log(LOG.replace("-0.1234", "INF"), 1000.0, "domain")
    assert r["wns_ns"] is None and r["tns_ns"] == -500.0
    with pytest.raises(sta.StaError, match="did not finish"):
        sta.parse_log("Error: boom\n", 1.0, "domain")


def test_power_views_and_summary(tmp_path, mv_pack, monkeypatch, capsys):
    """sta.run on a power flow's outputs: the views it builds (each domain
    alone on its rail's liberties, the top with boundary cells, the chip
    renamed per rail) and the summary, with OpenSTA stubbed."""
    f = _flow(tmp_path, mv_pack)
    out = tmp_path / "out"
    cfg = FlowConfig.load(f, out, run_sta=True)
    cfg.synth_dir.mkdir(parents=True)
    cfg.openroad_netlist.write_text(NETLIST)
    recs = [{"name": "lo", "rail": "LO", "module": "lo_core",
             "netlist_instances": ["u_lo"]},
            {"name": "h2", "rail": "HI2", "module": "hi2_core",
             "netlist_instances": ["u_h2"]}]
    (out / "power_domains.json").write_text(json.dumps({"domains": recs}))
    seen = []

    def fake(views, sta_dir, image):
        for v in views:
            seen.append(v)
            v.result = {"wns_ns": 0.1, "tns_ns": 0.0, "hold_wns_ns": 0.0,
                        "hold_in_ns": -0.03, "log": "x"}
            if v.kind == "chip":
                v.result.update(hold_wns_ns=0.02, hold_in_ns=0.04)
                v.result["endpoints"] = [("u_lo/f0/D", -0.2),
                                         ("u_h2/f0/D", 0.3), ("out1", 0.5)]
    monkeypatch.setattr(sta, "run_views", fake)
    js = sta.run(cfg, sta.wanted(cfg), {"domains": recs})
    by = {v.label: v for v in seen}
    assert set(by) == {"lo.func.default", "h2.func.default",
                       "top.func.default", "chip.func.default"}
    lib = mv_pack.parent / "lib"
    assert by["lo.func.default"].libs == [lib / "lo.lib"]
    assert by["lo.func.default"].top == "lo_core"
    assert by["top.func.default"].libs[:2] == [lib / "hi.lib", lib / "ls.lib"]
    assert by["top.func.default"].libs[-1].name == "boundary.lib"
    tnl = vn.parse(by["top.func.default"].netlist.read_text())
    assert set(tnl) == {"top"}
    top_sdc = by["top.func.default"].sdc.read_text()
    assert "set_input_delay 0 -clock c [get_pins {u_lo/y}]" in top_sdc
    assert "set_output_delay 0 -clock c [get_pins {u_lo/a[*]}]" in top_sdc
    assert "u_lo/clk}" not in top_sdc
    chip = by["chip.func.default"]
    cm = vn.parse(chip.netlist.read_text())
    assert {i.cell for i in cm["lo_core"].instances} == {
        "LO__TAND", "LO__TDFF", "LO__TINV"}
    assert {i.cell for i in cm["hi2_core"].instances} == {"HI2__TDFF"}
    assert any(p.name == "lo.lib" and p.parent.name == "LO"
               for p in chip.libs)
    s = json.loads(js.read_text())
    assert s["chip"][0]["by_domain"]["lo"]["wns_ns"] == -0.2
    assert s["chip"][0]["by_domain"]["lo"]["tns_ns"] == -0.2
    assert s["chip"][0]["by_domain"]["top"]["endpoints"] == 1
    assert {r["domain"] for r in s["domains"]} == {"lo", "h2", "top"}
    # Hold comes from the chip view (exact paths), not the domain views'
    # assumed boundary delays.
    assert s["hold"]["views"] == "chip" and s["hold"]["corner"] == "default"
    assert (s["hold"]["wns_ns"], s["hold"]["hold_in_ns"]) == (0.02, 0.04)
    printed = capsys.readouterr().out
    assert "[sta]   lo         LO         func     default    +0.1000" in printed
    with pytest.raises(sta.StaError, match="sta.corners: ss not among"):
        sta.run(cfg, sta.StaSpec(corners=("ss",)), {"domains": recs})
