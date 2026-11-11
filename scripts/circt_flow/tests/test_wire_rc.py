"""Pack-declared wire RC (circt_flow/wire_rc.py): the pack's `wire_rc:`
section, `techpack check`'s file / pin / coverage rules, how the STA and
vt_swap stages scale a wire model by it, and how P&R receives it. All RC
values and libraries are invented."""
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from circt_flow import orfs_pnr, profile, sta, techpack, vtswap
from circt_flow import wire_model as wm
from circt_flow import wire_rc
from circt_flow.config import FlowConfig
from circt_flow.wire_model import WireModel, WireModelError
from circt_flow.wire_rc import RcEntry, WireRc, WireRcError

from conftest import CELLS, write_lib
from test_wire_model import HIER, _libs, _run_sta, _wns, needs_sta

# An RC file: layer RC behind `catch`, the pre-route wire RC through the
# `env_str` helper and expr.
RC = """# invented RC, corner {name}
catch {{ set_layer_rc -layer M2 -resistance 2.0e-01 -capacitance {c}e-04 }}
catch {{ set_layer_rc -via V1 -resistance 3.0e-02 }}
set_wire_rc -signal -resistance [env_str TST_SIG_R {r}] -capacitance [expr {{[env_str TST_SIG_C {c}] * 0.001}}]
set_wire_rc -clock -resistance 0.05 -capacitance 2.0e-04
"""


def _rc(d: Path, name: str, r: float, c: float) -> Path:
    p = d / f"rc_{name}.tcl"
    p.write_text(RC.format(name=name, r=r, c=c))
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def rc_pack(tmp_path):
    """A schema-2 pack, corners tt/ss/ff (setup ss, hold ff), one RC file
    per corner (a different signal capacitance in each)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for c in ("tt", "ss", "ff"):
        write_lib(lib / f"hi_{c}.lib", f"tst_hi_{c}", CELLS)
    rc = tmp_path / "rc"
    rc.mkdir()
    files = {c: _rc(rc, c, r, cap) for c, r, cap in
             (("tt", 0.25, 0.2), ("ss", 0.3, 0.3), ("ff", 0.2, 0.15))}
    pack = tmp_path / "techpack.yaml"
    pack.write_text(f"""
schema: 2
name: tstrc
time_unit: ns
icg: {{cell: TSTICG_X1, clk: CK, en: E, test_en: SE, out: ECK}}
tie: {{hi: "TSTTIEHI:H", lo: "TSTTIELO:L"}}
corners: {{names: [tt, ss, ff], setup: ss, leakage: ss, hold: ff}}
wire_rc:
  tt: rc/rc_tt.tcl
  ss: {{path: rc/rc_ss.tcl, sha256: {_sha(files['ss'])}}}
  ff: {{path: rc/rc_ff.tcl}}
default_rail: HI
rails:
  HI:
    voltage: 0.8
    corners: {{tt: [lib/hi_tt.lib], ss: [lib/hi_ss.lib], ff: [lib/hi_ff.lib]}}
""")
    return pack


def test_load_pack_section(rc_pack):
    p = profile.load(rc_pack)
    assert set(p.wire_rc.entries) == {"tt", "ss", "ff"}
    ss = p.wire_rc.entries["ss"]
    assert ss.path == rc_pack.parent / "rc" / "rc_ss.tcl"
    assert ss.sha256 == _sha(ss.path)
    assert p.wire_rc.entries["tt"].sha256 is None
    assert p.wire_rc.at("ff").path.name == "rc_ff.tcl"
    # Per rail, the profile keeps the pack's wire_rc.
    assert p.for_rail("HI").wire_rc is p.wire_rc
    # A pack without one has none.
    text = rc_pack.read_text()
    rc_pack.write_text(re.sub(r"wire_rc:\n(  .*\n)+", "", text))
    assert profile.load(rc_pack).wire_rc is None


@pytest.mark.parametrize("val,err", [
    ({"zz": "rc/a.tcl"}, "unknown corner"),
    ("rc/a.tcl", "map each corner"),
    ({"tt": {"path": "a", "sha256": "XYZ"}}, "64 lowercase hex"),
    ({"tt": {"path": "a", "bogus": 1}}, "path and an optional sha256"),
    ({"tt": {"sha256": "0" * 64}}, "path and an optional sha256"),
    ({"tt": {"nope": 1}}, "nope is not a wire_rc key"),
    ({"tt": []}, "give a path"),
    ({"tt": {"signal": {"res_ohm_per_um": 1, "cap_ff_per_um": 0.2}}},
     "public: true packs only"),
])
def test_parse_errors(tmp_path, val, err):
    with pytest.raises(WireRcError, match=err):
        wire_rc.parse(val, tmp_path, ["tt", "ff"], False, "p: wire_rc")


def test_parse_single_and_inline(tmp_path):
    # Without corners: one entry, a path or {path, sha256}.
    one = wire_rc.parse("rc/a.tcl", tmp_path, [], False, "w")
    assert one.at(None).path == tmp_path / "rc/a.tcl"
    assert one.at("anything") is one.at(None)
    with pytest.raises(WireRcError, match="not a mapping of corners"):
        wire_rc.parse({"tt": "rc/a.tcl"}, tmp_path, [], False, "w")
    # Inline values: public packs only, checked, signal required.
    sig = {"res_ohm_per_um": 30, "cap_ff_per_um": 0.18}
    pub = wire_rc.parse({"tt": {"signal": sig,
                                "layers": {"M2": sig},
                                "vias": {"V1": {"res_ohm": 5}}},
                         "ff": {"signal": {**sig, "cap_ff_per_um": 0.12}}},
                        tmp_path, ["tt", "ff"], True, "w")
    assert pub.at("tt").inline["layers"]["M2"]["cap_ff_per_um"] == 0.18
    for bad, err in (({"tt": {"layers": {}}}, "needs signal"),
                     ({"tt": {"signal": {"res_ohm_per_um": 1}}}, "mapping of"),
                     ({"tt": {"signal": {**sig, "cap_ff_per_um": -1}}},
                      "number >= 0")):
        with pytest.raises(WireRcError, match=err):
            wire_rc.parse(bad, tmp_path, ["tt"], True, "w")
    with pytest.raises(WireRcError, match="not a mix"):
        wire_rc.parse({"tt": {"signal": sig}, "ff": "rc/a.tcl"}, tmp_path,
                      ["tt", "ff"], True, "w")


def test_repo_profile_rejects_wire_rc(tmp_path):
    p = tmp_path / "prof.yaml"
    p.write_text("name: x\nliberties: [a.lib]\nicg: {cell: A}\n"
                 "wire_rc: rc.tcl\n")
    with pytest.raises(KeyError, match="tech-pack key"):
        profile.load(p)


def test_check_ok_prints_hashes(rc_pack, capsys):
    assert techpack.check(str(rc_pack)) == []
    out = capsys.readouterr().out
    ss = rc_pack.parent / "rc" / "rc_ss.tcl"
    assert f"wire_rc.ss: {ss} ({ss.stat().st_size} bytes, sha256 " \
        f"{_sha(ss)}): pinned, ok" in out
    assert "wire_rc.tt:" in out and "not pinned" in out


@pytest.mark.parametrize("edit,err", [
    (lambda d: (d / "rc" / "rc_ff.tcl").unlink(), "rc_ff.tcl does not exist"),
    (lambda d: (d / "rc" / "rc_ff.tcl").write_text(""), "rc_ff.tcl is empty"),
    (lambda d: (d / "rc" / "rc_ss.tcl").write_text("changed\n"),
     "the pack pins"),
])
def test_check_file_problems(rc_pack, edit, err):
    edit(rc_pack.parent)
    problems = techpack.check(str(rc_pack))
    assert any(err in p for p in problems), problems


def test_check_coverage(rc_pack):
    text = rc_pack.read_text()
    rc_pack.write_text(text.replace("  ff: {path: rc/rc_ff.tcl}\n", ""))
    assert techpack.check(str(rc_pack)) == [
        "wire_rc: no entry for the hold corner ff"]
    rc_pack.write_text(text.replace("  ss: {path: rc/rc_ss.tcl, sha256: "
                                    + _sha(rc_pack.parent / "rc/rc_ss.tcl")
                                    + "}\n", ""))
    assert techpack.check(str(rc_pack)) == [
        "wire_rc: no entry for the setup corner ss"]


needs_tclsh = pytest.mark.skipif(shutil.which("tclsh") is None,
                                 reason="no tclsh")


@needs_tclsh
def test_probe_reads_signal_wire(tmp_path, monkeypatch):
    f = _rc(tmp_path, "x", 0.4, 0.25)
    assert wire_rc.probe(f) == (0.4, pytest.approx(0.25e-3))
    # `env_str` honours the environment.
    monkeypatch.setenv("TST_SIG_C", "0.5")
    assert wire_rc.probe(f)[1] == pytest.approx(0.5e-3)
    bare = tmp_path / "bare.tcl"
    bare.write_text("set_wire_rc -resistance 1 -capacitance 2\n")
    assert wire_rc.probe(bare) == (1.0, 2.0)
    clock_only = tmp_path / "clk.tcl"
    clock_only.write_text("set_wire_rc -clock -resistance 1 -capacitance 2\n")
    with pytest.raises(WireRcError, match="no set_wire_rc -signal"):
        wire_rc.probe(clock_only)


@needs_tclsh
def test_check_needs_signal_with_scaling_model(rc_pack):
    (rc_pack.parent / "rc" / "rc_tt.tcl").write_text(
        "set_layer_rc -layer M2 -resistance 1 -capacitance 2\n")
    # Without a model that scales by it, a file without a signal wire is
    # only a warning ...
    assert techpack.check(str(rc_pack)) == []
    # ... with one it is an error.
    rc_pack.write_text(rc_pack.read_text().replace(
        "default_rail: HI",
        "flow:\n  wire_model: {cap_base_ff: 0.2, rc_corner: tt}\n"
        "default_rail: HI"))
    problems = techpack.check(str(rc_pack))
    assert any("no set_wire_rc -signal" in p for p in problems), problems
    rc_pack.write_text(rc_pack.read_text().replace("rc_corner: tt",
                                                   "rc_corner: zz"))
    assert "flow.wire_model.rc_corner 'zz' is not a wire_rc corner" in \
        techpack.check(str(rc_pack))


def test_wire_model_rc_corner_key():
    d = wm.parse_pack({"cap_base_ff": 0.2, "rc_corner": "tt"}, "p")
    assert d == {"cap_base_ff": 0.2, "rc_corner": "tt"}
    m = wm.resolve(d, {"cap_per_fanout_ff": 0.3}, "f")
    assert m == WireModel(0.3, 0.2, rc_corner="tt")
    assert m.to_dict()["rc_corner"] == "tt"
    assert "rc_corner" not in WireModel(0.3, 0.2).to_dict()
    for bad in ({"cap_base_ff": 1, "rc_corner": 3},
                {"cap_base_ff": 1, "rc_corner": ""}):
        with pytest.raises(WireModelError, match="corner name"):
            wm.parse_pack(bad, "p")


def _inline_rc():
    sig = lambda r, c: {"signal": {"res_ohm_per_um": r, "cap_ff_per_um": c}}
    return WireRc({"tt": RcEntry("tt", inline=sig(20, 0.2)),
                   "ss": RcEntry("ss", inline=sig(30, 0.3))})


def test_scale():
    rc = _inline_rc()
    s = wire_rc.scale(rc, "ss", "tt")
    assert (s.c_expr, s.r_expr, s.setup_tcl) == ("1.5", "1.5", "")
    assert wire_rc.scale(rc, "tt", "tt").c_expr == "1"
    assert wire_rc.scale(rc, "ss", None) is None
    assert wire_rc.scale(None, "ss", "tt") is None
    with pytest.raises(WireRcError, match="no entry for corner 'ff'"):
        wire_rc.scale(rc, "ff", "tt")
    with pytest.raises(WireRcError, match="rc_corner 'ff' has no wire_rc"):
        wire_rc.scale(rc, "tt", "ff")
    # One implicit RC: nothing to scale between.
    one = WireRc({wire_rc.IMPLICIT: RcEntry(wire_rc.IMPLICIT, path=Path("a"))})
    assert wire_rc.scale(one, "tt", "tt") is None
    # Files: the factors are read in the OpenSTA session.
    files = WireRc({"tt": RcEntry("tt", path=Path("/r/tt.tcl")),
                    "ss": RcEntry("ss", path=Path("/r/ss.tcl"))})
    f = wire_rc.scale(files, "ss", "tt")
    assert "proc circtflow_rc_signal" in f.setup_tcl
    assert "circtflow_rc_signal {/r/ss.tcl}" in f.setup_tcl
    assert "circtflow_rc_signal {/r/tt.tcl}" in f.setup_tcl


def test_wire_model_tcl_with_scales():
    m = WireModel(0.3, 0.2, 10, 5, rc_corner="tt")
    rc = _inline_rc()
    # Unscaled: unchanged from the plain model.
    assert wm.tcl(m, "/x/w.spef", None) == wm.tcl(m, "/x/w.spef")
    t = wm.tcl(m, "/x/w.spef", {"": wire_rc.scale(rc, "ss", "tt")})
    assert "set circtflow_sc [expr {1.5}]" in t
    assert ("circtflow_wire_model [expr {0.2 * $circtflow_sc}] "
            "[expr {0.3 * $circtflow_sc}] [expr {5 * $circtflow_sr}] "
            "[expr {10 * $circtflow_sr}] {/x/w.spef} {}") in t
    two = wm.tcl(m, "/x/w.spef", {"-max": wire_rc.scale(rc, "ss", "tt"),
                                  "-min": wire_rc.scale(rc, "tt", "tt")})
    assert "{/x/w.max.spef} {-max}" in two and "{/x/w.min.spef} {-min}" in two


def _flow(tmp_path, pack, extra=""):
    f = tmp_path / "flow.yaml"
    f.write_text(f"""
design: {{top_module: top, clock_period_ps: 1000}}
pdk: {{profile: {pack}}}
mapping_target: {{flop_overhead: false}}
pnr: {{clock_port: clk}}
{extra}""")
    return f


def test_flow_config(tmp_path, rc_pack):
    out = tmp_path / "o"
    d = FlowConfig.load(_flow(tmp_path, rc_pack), out).design
    assert d.wire_rc is not None and d.wire_rc_setup == "ss"
    d = FlowConfig.load(_flow(tmp_path, rc_pack, "wire_model: "
                              "{cap_base_ff: 0.2, rc_corner: tt}"), out).design
    assert d.wire_model.rc_corner == "tt"
    with pytest.raises(WireModelError, match="not a wire_rc corner"):
        FlowConfig.load(_flow(tmp_path, rc_pack, "wire_model: "
                              "{cap_base_ff: 0.2, rc_corner: zz}"), out)
    plain = rc_pack.read_text().replace("wire_rc:", "unused_rc:")
    rc_pack.write_text(plain)
    with pytest.raises(WireModelError, match="needs a pack wire_rc"):
        FlowConfig.load(_flow(tmp_path, rc_pack, "wire_model: "
                              "{cap_base_ff: 0.2, rc_corner: tt}"), out)


def test_openroad_tcl(tmp_path):
    f = _rc(tmp_path, "tt", 0.25, 0.2)
    dest = wire_rc.write_openroad_tcl(RcEntry("tt", path=f),
                                      tmp_path / "run" / "wire_rc.tcl")
    src = tmp_path / "run" / "wire_rc_src.tcl"
    assert src.read_text() == f.read_text()
    t = dest.read_text()
    assert f"source {{{src}}}" in t and "proc env_str" in t
    inline = wire_rc.openroad_tcl(RcEntry("tt", inline={
        "signal": {"res_ohm_per_um": 30, "cap_ff_per_um": 0.18},
        "layers": {"M2": {"res_ohm_per_um": 40, "cap_ff_per_um": 0.19}},
        "vias": {"V1": {"res_ohm": 5}}}))
    assert "set_layer_rc -layer M2 -resistance [expr {40 * $circtflow_r}] " \
        "-capacitance [expr {0.19 * $circtflow_c}]" in inline
    assert "set_layer_rc -via V1 -resistance [expr {5 * $circtflow_r}]" in inline
    # No clock entry: the clock wire takes the signal's.
    assert "set_wire_rc -clock -resistance [expr {30 * $circtflow_r}]" in inline


def test_pnr_env(tmp_path, rc_pack):
    p = profile.load(rc_pack)
    env = wire_rc.pnr_env(p.wire_rc, p.setup_corner, tmp_path)
    rc = rc_pack.parent / "rc"
    assert env == {"CIRCT_FLOW_WIRE_RC_TT": str(rc / "rc_tt.tcl"),
                   "CIRCT_FLOW_WIRE_RC_SS": str(rc / "rc_ss.tcl"),
                   "CIRCT_FLOW_WIRE_RC_FF": str(rc / "rc_ff.tcl"),
                   "CIRCT_FLOW_WIRE_RC": str(rc / "rc_ss.tcl")}
    assert wire_rc.pnr_env(None, "ss", tmp_path) == {}
    # Inline entries are written out for the command.
    env = wire_rc.pnr_env(_inline_rc(), "ss", tmp_path)
    assert Path(env["CIRCT_FLOW_WIRE_RC"]) == tmp_path / "wire_rc_ss.tcl"
    assert "set_wire_rc -signal" in Path(env["CIRCT_FLOW_WIRE_RC"]).read_text()


def test_orfs_pnr_sets_rc_on_make_line(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, stdout=None, stderr=None, **kw):
        seen["cmd"] = cmd

        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(orfs_pnr.subprocess, "run", fake_run)
    monkeypatch.delenv("ORFS_HOME", raising=False)
    nl = tmp_path / "n.v"
    nl.write_text("module top(input clk); endmodule\n")
    f = _rc(tmp_path, "tt", 0.25, 0.2)
    run = tmp_path / "run"
    assert orfs_pnr.main([str(nl), str(run), "--top", "top", "--clock-port",
                          "clk", "--clock-period", "100",
                          "--wire-rc", str(f)]) == 0
    script = seen["cmd"][-1]
    assert f"SET_RC_TCL={run / 'wire_rc.tcl'}" in script
    assert (run / "wire_rc_src.tcl").read_text() == f.read_text()
    # Without --wire-rc the make line is the stock one.
    orfs_pnr.main([str(nl), str(tmp_path / "r2"), "--top", "top",
                   "--clock-port", "clk", "--clock-period", "100"])
    assert "SET_RC_TCL" not in seen["cmd"][-1]
    with pytest.raises(SystemExit, match="wire RC file not found"):
        orfs_pnr.main([str(nl), str(run), "--top", "top", "--clock-port",
                       "clk", "--clock-period", "100", "--wire-rc",
                       str(tmp_path / "missing.tcl")])


def test_sta_views_scale_by_corner(tmp_path):
    v = sta.View("top.func.ss", "domain", "top", "ss", "func",
                 [Path("a.lib")], Path("n.v"), "top", Path("c.sdc"))
    v.wire = WireModel(0.3, 0.2, rc_corner="tt")
    v.wire_scale = wire_rc.scale(_inline_rc(), "ss", "tt")
    t = sta._tcl(v, tmp_path).read_text()
    assert "circtflow wire rc: ss against tt" in t
    assert t.index("read_sdc") < t.index("circtflow_wire_model [expr") < \
        t.index("CIRCTFLOW-BEGIN")


def test_vt_swap_scales(tmp_path, rc_pack):
    d = FlowConfig.load(_flow(tmp_path, rc_pack, "wire_model: "
                              "{cap_base_ff: 0.2, rc_corner: tt}"),
                        tmp_path / "o").design
    s = vtswap.wire_scales(d, "ss", "ff")
    assert set(s) == {"-max", "-min"}
    assert (s["-max"].corner, s["-min"].corner) == ("ss", "ff")
    assert {p.name for p in s.files} == {"rc_ss.tcl", "rc_ff.tcl",
                                         "rc_tt.tcl"}
    assert set(vtswap.wire_scales(d, "ss", None)) == {""}
    d2 = FlowConfig.load(_flow(tmp_path, rc_pack, "wire_model: "
                               "{cap_base_ff: 0.2}"), tmp_path / "o").design
    assert vtswap.wire_scales(d2, "ss", "ff") is None


@needs_sta
def test_scaled_model_matches_hand_loads(tmp_path):
    """In OpenSTA, a model fitted at tt and timed at ss (signal C 1.5x in
    the files) equals the unscaled model with 1.5x the capacitances; the
    factors come from the RC files themselves."""
    libs = _libs(tmp_path)
    (tmp_path / "hier.v").write_text(HIER)
    tt = _rc(tmp_path, "tt", 0.25, 0.2)
    ss = _rc(tmp_path, "ss", 0.25, 0.3)
    rc = WireRc({"tt": RcEntry("tt", path=tt), "ss": RcEntry("ss", path=ss)})
    head = (f"read_verilog {tmp_path}/hier.v\nlink_design top\n"
            "create_clock -name clk -period 5 [get_ports clk]\n"
            "set_input_delay 0 -clock clk [get_ports d*]\n"
            "set_output_delay 0 -clock clk [get_ports q*]\n")
    m = WireModel(cap_per_fanout_ff=30, cap_base_ff=20, rc_corner="tt")
    out = _run_sta(tmp_path, libs, head + wm.tcl(
        m, str(tmp_path / "w.spef"), {"": wire_rc.scale(rc, "ss", "tt")})
        + "report_worst_slack -max -digits 5\n")
    m_ = re.search(r"circtflow wire rc: ss against tt: C x (\S+) R x (\S+)",
                   out)
    assert m_ and float(m_.group(1)) == pytest.approx(1.5) and \
        float(m_.group(2)) == pytest.approx(1.0), out
    hand = _run_sta(tmp_path, libs, head + wm.tcl(
        WireModel(cap_per_fanout_ff=45, cap_base_ff=30),
        str(tmp_path / "h.spef")) + "report_worst_slack -max -digits 5\n")
    assert _wns(out) == pytest.approx(_wns(hand), abs=2e-5)
    same = _run_sta(tmp_path, libs, head + wm.tcl(
        m, str(tmp_path / "w2.spef"), {"": wire_rc.scale(rc, "tt", "tt")})
        + "report_worst_slack -max -digits 5\n")
    plain = _run_sta(tmp_path, libs, head + wm.tcl(
        WireModel(30, 20), str(tmp_path / "p.spef"))
        + "report_worst_slack -max -digits 5\n")
    assert _wns(same) == pytest.approx(_wns(plain), abs=2e-5)


@needs_sta
def test_min_max_scaling(tmp_path):
    """-max and -min loads: setup paths see the setup corner's scaled
    capacitance, hold paths the hold corner's."""
    libs = _libs(tmp_path)
    (tmp_path / "hier.v").write_text(HIER)
    rc = _inline_rc()
    head = (f"read_verilog {tmp_path}/hier.v\nlink_design top\n"
            "create_clock -name clk -period 5 [get_ports clk]\n"
            "set_input_delay 0 -clock clk [get_ports d*]\n"
            "set_output_delay 0 -clock clk [get_ports q*]\n")
    m = WireModel(cap_per_fanout_ff=30, cap_base_ff=20, rc_corner="tt")
    rep = "report_worst_slack -max -digits 5\nreport_worst_slack -min -digits 5\n"
    both = _run_sta(tmp_path, libs, head + wm.tcl(
        m, str(tmp_path / "w.spef"),
        {"-max": wire_rc.scale(rc, "ss", "tt"),
         "-min": wire_rc.scale(rc, "tt", "tt")}) + rep)
    big = _run_sta(tmp_path, libs, head + wm.tcl(
        WireModel(45, 30), str(tmp_path / "b.spef")) + rep)
    small = _run_sta(tmp_path, libs, head + wm.tcl(
        WireModel(30, 20), str(tmp_path / "s.spef")) + rep)
    mn = lambda o: float(re.findall(r"worst slack min (\S+)", o)[-1])
    assert _wns(both) == pytest.approx(_wns(big), abs=2e-5)
    assert mn(both) == pytest.approx(mn(small), abs=2e-5)
