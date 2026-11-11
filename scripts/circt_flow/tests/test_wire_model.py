"""The pre-placement wire model (circt_flow/wire_model.py): the pack's
declaration, its resolution against the flow config, how the STA and
vt_swap stages carry it, and (with OpenSTA) what it does to timing. All
data is the invented mvt-mini library or an invented pack."""
import os
import re
import shutil
import subprocess
import sys

import pytest

from circt_flow import profile, sta, vtswap, wire_model as wm
from circt_flow.config import ROOT, FlowConfig
from circt_flow.wire_model import WireModel, WireModelError

MINI = ROOT / "examples" / "mvt-mini"


def test_parse_pack():
    d = wm.parse_pack({"cap_per_fanout_ff": 0.3, "cap_base_ff": 1}, "p")
    assert d == {"cap_per_fanout_ff": 0.3, "cap_base_ff": 1.0}
    assert wm.parse_pack({"cap_base_ff": 0.1, "res_base_ohm": 5}, "p")
    for bad, msg in (({}, "mapping of"), ([1], "mapping of"),
                     ({"cap_base_ff": 1, "nope": 2}, "nope is not a key"),
                     ({"cap_base_ff": -1}, "number >= 0"),
                     ({"cap_base_ff": True}, "number >= 0"),
                     ({"cap_base_ff": "1"}, "number >= 0"),
                     ({"res_base_ohm": 5}, "needs a capacitance")):
        with pytest.raises(WireModelError, match=msg):
            wm.parse_pack(bad, "p")


def test_model_arithmetic():
    m = WireModel(cap_per_fanout_ff=0.5, cap_base_ff=0.25)
    assert m.cap_ff(4) == 2.25 and m.res_ohm(4) == 0 and not m.has_res
    r = WireModel(0.1, 0, 10, 2)
    assert r.has_res and r.res_ohm(3) == 32
    assert r.to_dict() == {"cap_per_fanout_ff": 0.1, "cap_base_ff": 0.0,
                           "res_per_fanout_ohm": 10.0, "res_base_ohm": 2.0}


def test_resolve():
    pack = {"cap_per_fanout_ff": 0.3, "cap_base_ff": 0.2}
    # Off unless the pack (or the flow config) declares one.
    assert wm.resolve(None, None, "f") is None
    assert wm.resolve(None, False, "f") is None
    assert wm.resolve(pack, False, "f") is None
    assert wm.resolve(pack, None, "f") == WireModel(0.3, 0.2)
    assert wm.resolve(pack, True, "f") == WireModel(0.3, 0.2)
    with pytest.raises(WireModelError, match="true needs a pack"):
        wm.resolve(None, True, "f")
    # A mapping replaces the named keys; it can also stand alone.
    assert wm.resolve(pack, {"cap_per_fanout_ff": 0.5}, "f") == \
        WireModel(0.5, 0.2)
    assert wm.resolve(pack, {"res_base_ohm": 7}, "f") == \
        WireModel(0.3, 0.2, 0, 7)
    assert wm.resolve(None, {"cap_base_ff": 1.5}, "f") == \
        WireModel(0, 1.5)
    with pytest.raises(WireModelError, match="capacitance"):
        wm.resolve(None, {"res_base_ohm": 1}, "f")
    with pytest.raises(WireModelError, match="x is not a key"):
        wm.resolve(pack, {"x": 1}, "f")
    with pytest.raises(WireModelError, match="true, false or a mapping"):
        wm.resolve(pack, "yes", "f")


def test_tcl():
    assert wm.tcl(None) == ""
    t = wm.tcl(WireModel(0.35, 0.15), "/x/w.spef")
    assert "proc circtflow_wire_model" in t
    assert t.rstrip().endswith("circtflow_wire_model 0.15 0.35 0 0 {/x/w.spef}")
    t = wm.tcl(WireModel(cap_per_fanout_ff=0.35, cap_base_ff=0.15,
                         res_per_fanout_ohm=100, res_base_ohm=50), "/x/w.spef")
    # cap base, cap per load, res base, res per load
    assert t.rstrip().endswith("0.15 0.35 50 100 {/x/w.spef}")


def _pack(tmp_path, mv_pack, flow=""):
    """The conftest two-rail pack with a `flow:` section added."""
    text = mv_pack.read_text().replace(
        "default_rail: HI", f"flow:\n{flow}default_rail: HI") if flow \
        else mv_pack.read_text()
    p = tmp_path / "wpack.yaml"
    p.write_text(text)
    return p


def _flow(tmp_path, pack, extra=""):
    (tmp_path / "func.sdc").write_text(
        "create_clock -name c -period 1.0 [get_ports clk]\n")
    f = tmp_path / "flow.yaml"
    f.write_text(f"""
design: {{top_module: top, clock_period_ps: 1000}}
pdk: {{profile: {pack}}}
mapping_target: {{flop_overhead: false}}
pnr: {{clock_port: clk}}
{extra}""")
    return f


def test_pack_flow_section(tmp_path, mv_pack):
    p = _pack(tmp_path, mv_pack,
              "  wire_model: {cap_per_fanout_ff: 0.3, cap_base_ff: 0.2}\n")
    assert profile.load(p).flow["wire_model"] == {
        "cap_per_fanout_ff": 0.3, "cap_base_ff": 0.2}
    bad = _pack(tmp_path, mv_pack, "  wire_model: {cap_base_ff: -1}\n")
    with pytest.raises(WireModelError, match="number >= 0"):
        profile.load(bad)


def test_flow_config_resolution(tmp_path, mv_pack):
    out = tmp_path / "o"
    # A pack without a model: off (existing packs are unchanged).
    plain = _flow(tmp_path, mv_pack)
    assert FlowConfig.load(plain, out).design.wire_model is None
    p = _pack(tmp_path, mv_pack,
              "  wire_model: {cap_per_fanout_ff: 0.3, cap_base_ff: 0.2}\n")
    on = _flow(tmp_path, p)
    assert FlowConfig.load(on, out).design.wire_model == WireModel(0.3, 0.2)
    off = _flow(tmp_path, p, "wire_model: false")
    assert FlowConfig.load(off, out).design.wire_model is None
    over = _flow(tmp_path, p, "wire_model: {cap_per_fanout_ff: 0.1}")
    assert FlowConfig.load(over, out).design.wire_model == WireModel(0.1, 0.2)
    alone = _flow(tmp_path, mv_pack, "wire_model: {cap_base_ff: 2}")
    assert FlowConfig.load(alone, out).design.wire_model == WireModel(0, 2)
    with pytest.raises(WireModelError, match="true needs a pack"):
        FlowConfig.load(_flow(tmp_path, mv_pack, "wire_model: true"), out)


def test_sta_view_script_carries_the_model(tmp_path):
    from pathlib import Path
    v = sta.View("top.func.tt", "domain", "top", "tt", "func",
                 [Path("a.lib")], Path("n.v"), "top", Path("c.sdc"))
    assert "circtflow_wire_model" not in sta._tcl(v, tmp_path).read_text()
    v.wire = WireModel(0.3, 0.2)
    t = sta._tcl(v, tmp_path).read_text()
    assert f"{{{tmp_path}/top.func.tt.wire.spef}}" in t
    # After the SDC is read, before the reports.
    assert t.index("read_sdc") < t.index("circtflow_wire_model 0.2") < \
        t.index("CIRCTFLOW-BEGIN")


def _have_sta():
    if os.environ.get("OPENSTA"):
        return True
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", vtswap.ORFS_IMAGE],
                       capture_output=True)
    return r.returncode == 0


needs_sta = pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker "
                               "image or $OPENSTA)")

# Flop -> two inverters in a submodule -> inverter named like an escaped
# bus bit -> flop; bus nets, a net seen from two hierarchy levels, an output
# port load.
HIER = """module sub (input a, input clk, output y);
  wire m;
  MINV_X1_R g0 (.A(a), .Y(m));
  MINV_X1_R g1 (.A(m), .Y(y));
endmodule
module top (input clk, input [1:0] d, output [1:0] q);
  wire [1:0] n0;
  wire [1:0] n1;
  MDFF_X1_R f0 (.CK(clk), .D(d[0]), .Q(n0[0]));
  MDFF_X1_R f2 (.CK(clk), .D(d[1]), .Q(n0[1]));
  sub u1 (.a(n0[0]), .clk(clk), .y(n1[0]));
  sub u2 (.a(n0[1]), .clk(clk), .y(n1[1]));
  MINV_X1_R \\odd/name[3] (.A(n1[1]), .Y(q[1]));
  MDFF_X1_R f1 (.CK(clk), .D(n1[0]), .Q(q[0]));
endmodule
"""


def _run_sta(tmp_path, libs, body):
    tcl = tmp_path / "t.tcl"
    tcl.write_text("".join(f"read_liberty {l}\n" for l in libs) + body
                   + "exit\n")
    local = os.environ.get("OPENSTA")
    if local:
        cmd = [local, "-no_init", "-no_splash", "-exit", str(tcl)]
    else:
        cmd = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
               "-e", "HOME=/tmp", "-v", f"{tmp_path}:{tmp_path}",
               vtswap.ORFS_IMAGE, vtswap.STA_BIN, "-no_init", "-no_splash",
               "-exit", str(tcl)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def _libs(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    return [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]


def _wns(out, n=-1):
    return float(re.findall(r"worst slack max (\S+)", out)[n])


@needs_sta
def test_cap_model_matches_hand_loads(tmp_path):
    """The model's worst slack equals the same design with set_load put on every
    net by hand from the fanout formula (pF libraries), and is lower than
    the unloaded one; the model counts the output port and every alias of
    a hierarchical net once."""
    libs = _libs(tmp_path)
    (tmp_path / "hier.v").write_text(HIER)
    head = (f"read_verilog {tmp_path}/hier.v\nlink_design top\n"
            "create_clock -name clk -period 5 [get_ports clk]\n"
            "set_input_delay 0 -clock clk [get_ports d*]\n"
            "set_output_delay 0 -clock clk [get_ports q*]\n"
            "report_worst_slack -max -digits 5\n")
    m = WireModel(cap_per_fanout_ff=30, cap_base_ff=20)
    out = _run_sta(tmp_path, libs, head + wm.tcl(m, str(tmp_path / "w.spef"))
                   + "report_worst_slack -max -digits 5\n")
    assert "circtflow wire model: 11 net(s)" in out
    base, with_wire = _wns(out, 0), _wns(out)
    assert with_wire < base - 0.1
    # By hand: n loads -> (20 + 30 n) fF = (0.02 + 0.03 n) pF, on the flat
    # nets (each internal net once, by the name of its highest alias).
    loads = {"clk": 3, "d[0]": 1, "d[1]": 1, "n0[0]": 1, "n0[1]": 1,
             "n1[0]": 1, "n1[1]": 1, "q[0]": 1, "q[1]": 1, "u1/m": 1,
             "u2/m": 1}
    hand = "".join(f"set_load {0.02 + 0.03 * k:.5f} [get_nets {{{n}}}]\n"
                   for n, k in loads.items())
    out2 = _run_sta(tmp_path, libs, head + hand
                    + "report_worst_slack -max -digits 5\n")
    assert _wns(out2) == pytest.approx(with_wire, abs=2e-5)


@needs_sta
def test_resistance_adds_wire_delay(tmp_path):
    """With a resistance the model reads a SPEF that carries the
    capacitance too: wire delay appears at load pins, and the result is
    slower than the capacitance alone."""
    libs = _libs(tmp_path)
    (tmp_path / "hier.v").write_text(HIER)
    head = (f"read_verilog {tmp_path}/hier.v\nlink_design top\n"
            "create_clock -name clk -period 5 [get_ports clk]\n"
            "set_input_delay 0 -clock clk [get_ports d*]\n"
            "set_output_delay 0 -clock clk [get_ports q*]\n")
    run = lambda m, tag: _run_sta(
        tmp_path, libs, head + wm.tcl(m, str(tmp_path / f"{tag}.spef"))
        + "report_worst_slack -max -digits 5\n"
        "report_checks -path_delay max -fields {input_pins} -digits 4\n")
    c = run(WireModel(30, 20), "c")
    rc = run(WireModel(30, 20, 20000, 10000), "rc")
    assert "Warning" not in rc.replace("Warning: There", "")
    assert "circtflow wire model: 11 net(s)" in rc
    assert (tmp_path / "rc.spef").exists() and not (tmp_path / "c.spef").exists()
    assert _wns(rc) < _wns(c) - 0.5
    # A wire delay row: a load pin with a nonzero delay before its cell.
    delays = re.findall(r"^\s+(\d\.\d+)\s+\d\.\d+ [\^v] (?:u\d/g\d|f\d|odd\S+)"
                        r"/[AD] \(", rc, re.M)
    assert delays and all(float(x) > 0.1 for x in delays)


@needs_sta
def test_swap_sizes_against_the_wire_model(tmp_path):
    """vt_swap with a model needs more speed than without: a clock the RVT
    chain meets bare is missed with wires and the swap moves cells; the
    record carries the model."""
    libs = _libs(tmp_path)
    from circt_flow.tests.test_vtswap import _chain_netlist, CHAIN
    nl = tmp_path / "chain.v"
    nl.write_text(_chain_netlist())
    tokens, fre = ["R", "L", "S"], r"_([RLS])$"
    bare = vtswap.swap(nl, "chain", tmp_path / "a.v", libs, tokens, fre,
                       vtswap.sdc_for(5000, "clk", ["d"]), recover=False)
    period = int((5.0 - bare["start"]["wns"]) * 1000 * 1.12)  # with some slack to spare
    sdc = vtswap.sdc_for(period, "clk", ["d"])
    no_wire = vtswap.swap(nl, "chain", tmp_path / "b.v", libs, tokens, fre,
                          sdc, recover=False)
    assert no_wire["start"]["wns"] >= 0 and not no_wire["up"]
    assert "wire_model" not in no_wire
    model = WireModel(cap_per_fanout_ff=0.5, cap_base_ff=0.5)
    wired = vtswap.swap(nl, "chain", tmp_path / "c.v", libs, tokens, fre,
                        sdc, recover=False, wire_model=model)
    assert wired["wire_model"] == model.to_dict()
    assert wired["start"]["wns"] < no_wire["start"]["wns"]
    assert wired["start"]["wns"] < 0 and wired["up"]
    assert wired["end"]["wns"] >= 0
    assert sum(wired["flavours_after"].values()) == CHAIN + 2
    assert wired["flavours_after"]["R"] < CHAIN + 2
