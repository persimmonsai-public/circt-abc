"""Modes: the SDC subset, flop overhead from a liberty, the calibrated
mapping target, clock tracing through the hierarchy, and how a flow config
carries them (no tools needed)."""
import json
import math
import subprocess
import sys
import textwrap

import pytest
import yaml

from circt_flow import modes, pipeline, power, stages
from circt_flow.config import DesignConfig, FlowConfig
from circt_flow.modes import ModeError

from conftest import REPO

# ---- SDC ------------------------------------------------------------------


def test_sdc_subset():
    clocks, delays, warns = modes.parse_sdc(textwrap.dedent("""\
        # a comment
        create_clock -name core -period 1.25 [get_ports clk_i]
        create_clock -period 2 [get_ports {clk2_i}] ; # port-named clock
        create_clock -name v -period 4
        create_clock -name w -period 3 -waveform {0 1.5} [get_ports "clk3"]
        set_input_delay 0.1 -clock core [get_ports {a b}]
        set_output_delay -max 0.2 -clock core \\
            [all_outputs]
        set_input_delay -0.05 -clock v [get_ports c]
        set_false_path -from [get_ports rst_ni]
        """), "t.sdc")
    assert clocks == {
        "core": modes.Clock("core", 1250.0, ("clk_i",)),
        "clk2_i": modes.Clock("clk2_i", 2000.0, ("clk2_i",)),
        "v": modes.Clock("v", 4000.0, ()),
        "w": modes.Clock("w", 3000.0, ("clk3",))}
    assert delays == [
        modes.IODelay("input", 100.0, "core", "[get_ports {a b}]"),
        modes.IODelay("output", 200.0, "core", "[all_outputs]", "max"),
        modes.IODelay("input", -50.0, "v", "[get_ports c]")]
    # set_false_path -from [get_ports ...] is read (Mode.false_from); other
    # exceptions are noted as applying only where OpenSTA reads the SDC.
    assert len(warns) == 1
    assert "t.sdc:5: create_clock: ignored -waveform {0 1.5}" in warns[0]
    _, _, w2, ff = modes.parse_sdc_full(
        "create_clock -period 1 [get_ports c]\n"
        "set_false_path -from [get_ports {rst_ni test_i}]\n"
        "set_false_path -from [get_ports a] -to [get_ports b]\n"
        "set_multicycle_path 2 -setup -from [get_ports a]\n", "u.sdc")
    assert ff == ["rst_ni", "test_i"]
    assert len(w2) == 2 and all("not in power-domain views" in w for w in w2)


def test_sdc_units():
    clocks, _, _ = modes.parse_sdc(
        "set_units -time ps\ncreate_clock -period 900 [get_ports c]\n")
    assert clocks["c"].period_ps == 900.0
    clocks, _, _ = modes.parse_sdc("create_clock -period 900 [get_ports c]",
                                   time_unit="1ps")
    assert clocks["c"].period_ps == 900.0


@pytest.mark.parametrize("text,err", [
    ("create_clock -name c [get_ports c]", "needs -period > 0"),
    ("create_clock -period 0 [get_ports c]", "needs -period > 0"),
    ("create_clock -period x [get_ports c]", "'x' is not a number"),
    ("create_clock -period 1", "a virtual clock needs -name"),
    ("create_clock -period 1 [get_pins u/CK]", "must be \\[get_ports"),
    ("create_clock -period 1 [get_ports c]\ncreate_clock -period 2 "
     "[get_ports c]", "clock c is defined twice"),
    ("set_input_delay 0 -clock nope [get_ports a]", "clock nope is not "
     "defined"),
    ("create_clock -period 1 [get_ports c]\nset_input_delay -clock c "
     "[get_ports a]", "needs a delay"),
])
def test_sdc_errors(text, err):
    with pytest.raises(ModeError, match=err):
        modes.parse_sdc(text)


def test_parse_modes(tmp_path):
    (tmp_path / "a.sdc").write_text("create_clock -period 1 [get_ports c]\n")
    (tmp_path / "b.sdc").write_text("create_clock -period 0.8 "
                                    "[get_ports c]\nfoo bar\n")
    ms = modes.parse_modes({"func": "a.sdc", "fast": {"sdc": "b.sdc"}},
                           tmp_path, "f.yaml")
    assert [m.name for m in ms] == ["func", "fast"]
    assert ms[1].clocks["c"].period_ps == 800.0
    assert ms[1].warnings and "foo" in ms[1].warnings[0]
    (tmp_path / "v.sdc").write_text("create_clock -name v -period 1\n")
    for bad, err in [({"x": "nope.sdc"}, "does not exist"),
                     ({"1x": "a.sdc"}, "not an identifier"),
                     ({}, "must map each mode"),
                     ({"v": "v.sdc"}, "defines no clock on a port"),
                     ({"x": {"sdc": "a.sdc", "y": 1}}, "takes one key")]:
        with pytest.raises(ModeError, match=err):
            modes.parse_modes(bad, tmp_path, "f.yaml")


def test_parse_target():
    assert modes.parse_target(None, "f") == modes.TargetSpec()
    t = modes.parse_target({"flop_overhead": 70, "scale": 0.9,
                            "flop_cells": ["DFF"]}, "f")
    assert t == modes.TargetSpec(70.0, ("DFF",), 0.9)
    assert modes.parse_target({"flop_overhead": False}, "f").flop_overhead \
        is None
    for bad in [{"flop_overhead": "yes"}, {"flop_overhead": -1},
                {"scale": 0}, {"scale": 3}, {"nope": 1},
                {"flop_cells": "DFF"}]:
        with pytest.raises(ModeError):
            modes.parse_target(bad, "f")


# ---- flop overhead --------------------------------------------------------

FLOP_LIB = """\
library (tstff) {
  time_unit : "1ns";
  lu_table_template (t2) {
    variable_1 : input_net_transition;
    variable_2 : total_output_net_capacitance;
    index_1 ("0.01, 0.1");
    index_2 ("0.001, 0.011");
  }
  cell (TSTINV) {
    area : 1.0;
    pin (A) { direction : input; capacitance : 0.001; }
    pin (Y) { direction : output; function : "!A"; }
  }
%s
}
"""


def flop(name, c2q_rise, c2q_fall, setup, dcap=0.0025):
    """A flop whose clock-to-Q table is linear in load: at the smallest
    slew, `x` at 0.001 pF and `x` + 0.1 at 0.011 pF."""
    def tab(kind, x):
        return (f'        {kind} (t2) {{ values ("{x:.3f}, {x + 0.1:.3f}", '
                f'"{x + 0.01:.3f}, {x + 0.11:.3f}"); }}\n')
    return f"""\
  cell ({name}) {{
    area : 2.0;
    cell_leakage_power : 1.0;
    ff (IQ, IQN) {{ clocked_on : "CK"; next_state : "D"; }}
    pin (CK) {{ direction : input; capacitance : 0.001; clock : true; }}
    pin (D) {{
      direction : input;
      capacitance : {dcap};
      timing () {{
        related_pin : "CK";
        timing_type : setup_rising;
        rise_constraint (scalar) {{ values ("{setup:.3f}"); }}
        fall_constraint (scalar) {{ values ("{setup / 2:.3f}"); }}
      }}
    }}
    pin (Q) {{
      direction : output;
      function : "IQ";
      timing () {{
        related_pin : "CK";
        timing_type : rising_edge;
{tab("cell_rise", c2q_rise)}{tab("cell_fall", c2q_fall)}      }}
    }}
  }}
"""


@pytest.fixture
def flop_lib(tmp_path):
    p = tmp_path / "ff.lib"
    p.write_text(FLOP_LIB % (flop("TSTDFF_SLOW", 0.05, 0.04, 0.040)
                             + flop("TSTDFF_FAST", 0.02, 0.03, 0.010)))
    return p


def test_flop_overhead(flop_lib):
    r = modes.flop_overhead([flop_lib])
    # load 4 x 0.0025 = 0.01 pF: 0.05 + 0.1 * (0.009 / 0.01) = 0.14 ns
    assert r["cell"] == "TSTDFF_SLOW" and r["flops_considered"] == 2
    assert r["clk_to_q_ps"] == pytest.approx(140.0)
    assert r["setup_ps"] == pytest.approx(40.0)
    assert r["overhead_ps"] == pytest.approx(180.0)
    fast = modes.flop_overhead([flop_lib], ["TSTDFF_FAST"])
    assert fast["cell"] == "TSTDFF_FAST"
    # max(rise 0.02, fall 0.03) + 0.09 = 120 ps, setup max(10, 5) ps
    assert fast["overhead_ps"] == pytest.approx(120.0 + 10.0)
    with pytest.raises(ModeError, match="no flop"):
        modes.flop_overhead([flop_lib], ["TSTINV"])


def test_flop_overhead_ps_unit(tmp_path):
    p = tmp_path / "ps.lib"
    p.write_text((FLOP_LIB % flop("TSTDFF", 50.0, 40.0, 20.0))
                 .replace('"1ns"', '"1ps"'))
    # table in ps: 50 + 0.1 * 0.9 = 50.09 ps clock-to-Q, 20 ps setup
    assert modes.flop_overhead([p])["overhead_ps"] == pytest.approx(70.09)


# ---- the target -----------------------------------------------------------


def two_modes():
    func = modes.Mode("func", None, {
        "hv": modes.Clock("hv", 1500.0, ("clk_hv",)),
        "lv": modes.Clock("lv", 1800.0, ("clk_lv",))})
    turbo = modes.Mode("turbo", None, {
        "hv": modes.Clock("hv", 1300.0, ("clk_hv",)),
        "lv": modes.Clock("lv", 2200.0, ("clk_lv",)),
        "v": modes.Clock("v", 100.0, ())})
    # mode.sdc None marks a shorthand mode; give these an SDC path so a
    # virtual clock is not taken as a shorthand one.
    from pathlib import Path
    return (modes.Mode("func", Path("f.sdc"), func.clocks),
            modes.Mode("turbo", Path("t.sdc"), turbo.clocks))


OV = {"overhead_ps": 113.3, "cell": "DFF", "library": "x.lib",
      "clk_to_q_ps": 89.3, "setup_ps": 24.0}


def test_derive_target_follows_tightest_mode():
    ms = two_modes()
    spec = modes.TargetSpec()
    hv = modes.derive_target("hv", ms, {"clk_hv", "rst"}, spec, OV)
    assert hv["tightest_mode"] == "turbo" and hv["period_ps"] == 1300.0
    assert hv["modes"] == {"func": {"clocks": {"hv": 1500.0},
                                    "period_ps": 1500.0},
                           "turbo": {"clocks": {"hv": 1300.0},
                                     "period_ps": 1300.0}}
    assert hv["target_ps"] == math.floor(1300 - 113.3) == 1186
    lv = modes.derive_target("lv", ms, {"clk_lv"}, spec, OV)
    assert lv["tightest_mode"] == "func" and lv["target_ps"] == 1686
    # Every clock (the top itself): the tightest of all, virtual ignored.
    top = modes.derive_target("top", ms, None, spec, OV)
    assert top["period_ps"] == 1300.0
    assert "v" not in top["modes"]["turbo"]["clocks"]
    # Named clocks override tracing.
    assert modes.derive_target("x", ms, set(), spec, OV,
                               ["lv"])["period_ps"] == 1800.0
    line = modes.log_target(hv)
    assert "tightest turbo 1300 ps" in line
    assert "floor(1 * (1300 - 113.3)) = 1186" in line
    assert "DFF in x.lib: clk->Q 89.3 + setup 24" in line


def test_derive_target_knobs():
    ms = two_modes()
    t = modes.derive_target("hv", ms, {"clk_hv"},
                            modes.TargetSpec(flop_overhead=100.0, scale=1.1),
                            None)
    assert t["target_ps"] == math.floor(1.1 * 1200) == 1320
    t = modes.derive_target("hv", ms, {"clk_hv"},
                            modes.TargetSpec(flop_overhead=None), None)
    assert t["target_ps"] == 1300 and t["flop_overhead_ps"] == 0


def test_derive_target_errors():
    ms = two_modes()
    spec = modes.TargetSpec()
    with pytest.raises(ModeError, match="no clock of any mode"):
        modes.derive_target("d", ms, {"rst"}, spec, OV)
    with pytest.raises(ModeError, match="not defined in any mode"):
        modes.derive_target("d", ms, None, spec, OV, ["nope"])
    with pytest.raises(ModeError, match="<= 0"):
        modes.derive_target("d", ms, None,
                            modes.TargetSpec(flop_overhead=5000.0), None)
    with pytest.raises(ModeError, match="needs the rail's flops"):
        modes.derive_target("d", ms, None, spec, None)


def test_shorthand_mode():
    m = modes.shorthand_mode(1000, "clk_i")
    assert modes.domain_clocks(m, {"clk_i"}) == [m.clocks["clk"]]
    assert modes.domain_clocks(m, {"x"}) == []
    # Unknown clock port: the one clock clocks everything.
    m = modes.shorthand_mode(1000, None)
    assert modes.domain_clocks(m, {"x"}) == [m.clocks["clk"]]


# ---- clock tracing --------------------------------------------------------

MLIR = textwrap.dedent('''\
    module {
      hw.module private @leaf(in %ck : i1, in %d : i8, out q : i8) {
        hw.output %d : i8
      }
      hw.module private @mid(in %clk : !seq.clock, in %d : i8, out q : i8) {
        %0 = seq.from_clock %clk
        %u_leaf.q = hw.instance "u_leaf" @leaf(ck: %0: i1, d: %d: i8) -> (q: i8)
        hw.output %u_leaf.q : i8
      }
      hw.module @top(in %clk_a : i1, in %clk_b : i1, in %x : i8, out q : i8) {
        %0 = seq.to_clock %clk_b
        %u_m.q = hw.instance "u_m" @mid(clk: %0: !seq.clock, d: %x: i8) -> (q: i8)
        %u_l.q = hw.instance "u_l" @leaf(ck: %clk_a: i1, d: %u_m.q: i8) -> (q: i8)
        hw.output %u_l.q : i8
      }
    }
    ''')


def test_feeding_ports():
    env, mod = modes.feeding_ports(MLIR, "top", "u_l")
    assert mod == "leaf" and env == {"ck": {"clk_a"}, "d": set()}
    env, mod = modes.feeding_ports(MLIR, "top", "u_m")
    assert mod == "mid" and env == {"clk": {"clk_b"}, "d": {"x"}}
    env, mod = modes.feeding_ports(MLIR, "top", "u_m.u_leaf")
    assert mod == "leaf" and env == {"ck": {"clk_b"}, "d": {"x"}}
    with pytest.raises(ModeError, match="instance nope"):
        modes.feeding_ports(MLIR, "top", "u_m.nope")


# ---- flow config ----------------------------------------------------------


def schema1_pack(tmp_path, lib):
    pack = tmp_path / "techpack.yaml"
    pack.write_text(yaml.safe_dump({
        "schema": 1, "name": "tstff", "liberties": [str(lib)],
        "time_unit": "ns", "public": True,
        "icg": {"cell": "X", "clk": "CK", "en": "E", "test_en": "SE",
                "out": "Q"}}))
    return pack


def flow(tmp_path, pack, **extra):
    d = {"design": {"top_module": "top", "clock_period_ps": 1000,
                    "manifest": "m.yaml"},
         "pdk": {"profile": str(pack)}, "pnr": {"clock_port": "clk_i"}}
    for k, v in extra.items():
        if k in ("clock_period_ps", "abc_delay_target_ps"):
            if v is None:
                d["design"].pop(k, None)
            else:
                d["design"][k] = v
        else:
            d[k] = v
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump(d))
    return f


def test_config_without_modes_unchanged(tmp_path, flop_lib):
    f = flow(tmp_path, schema1_pack(tmp_path, flop_lib))
    d = DesignConfig.from_yaml(f)
    assert d.modes is None and d.mapping_target is None
    assert not d.calibrate and d.abc_target_ps == 1000
    assert d.clock_port == "clk_i"
    fc = FlowConfig.load(f, tmp_path / "out", env={})
    assert pipeline.calibrate(fc) is fc
    assert "clock-period-ps=1000 " in stages._abc_bridge_opt(fc)


def test_config_modes_and_calibrate(tmp_path, flop_lib, capsys):
    (tmp_path / "a.sdc").write_text(
        "create_clock -name c -period 1.2 [get_ports clk_i]\n")
    (tmp_path / "b.sdc").write_text(
        "create_clock -name c -period 0.9 [get_ports clk_i]\n")
    f = flow(tmp_path, schema1_pack(tmp_path, flop_lib),
             clock_period_ps=None, modes={"a": "a.sdc", "b": "b.sdc"})
    d = DesignConfig.from_yaml(f)
    assert [m.name for m in d.modes] == ["a", "b"]
    assert d.clock_period_ps == 900 and d.calibrate
    fc = FlowConfig.load(f, tmp_path / "out", env={})
    fc.out_dir.mkdir()
    cal = pipeline.calibrate(fc)
    # 900 - (140 + 40) from TSTDFF_SLOW
    assert cal.design.abc_delay_target_ps == 720
    assert "clock-period-ps=720 " in stages._abc_bridge_opt(cal)
    rec = json.loads((fc.out_dir / "mapping_target.json").read_text())
    assert rec["tightest_mode"] == "b" and rec["target_ps"] == 720
    assert rec["flop_overhead"]["cell"] == "TSTDFF_SLOW"
    assert "[mapping-target] top:" in capsys.readouterr().out


def test_config_shorthand_with_target(tmp_path, flop_lib):
    f = flow(tmp_path, schema1_pack(tmp_path, flop_lib),
             mapping_target={"scale": 1.0})
    fc = FlowConfig.load(f, tmp_path / "out", env={})
    fc.out_dir.mkdir()
    cal = pipeline.calibrate(fc)
    assert cal.design.abc_delay_target_ps == 1000 - 180
    rec = json.loads((fc.out_dir / "mapping_target.json").read_text())
    assert rec["modes"] == {"default": {"clocks": {"clk": 1000.0},
                                        "period_ps": 1000.0}}


def test_pack_default_target(tmp_path, flop_lib):
    # A pack's flow: mapping_target calibrates every run on it (the scale is
    # the library's ABC optimism); a flow config's own replaces it.
    pack = schema1_pack(tmp_path, flop_lib)
    d = yaml.safe_load(pack.read_text())
    d["flow"] = {"mapping_target": {"scale": 0.5}}
    pack.write_text(yaml.safe_dump(d))
    fc = FlowConfig.load(flow(tmp_path, pack), tmp_path / "out", env={})
    assert fc.design.calibrate and fc.design.mapping_target_from_pack
    fc.out_dir.mkdir()
    assert pipeline.calibrate(fc).design.abc_delay_target_ps == \
        int(0.5 * (1000 - 180))
    fc = FlowConfig.load(flow(tmp_path, pack, mapping_target={"scale": 1.0}),
                         tmp_path / "out", env={})
    assert not fc.design.mapping_target_from_pack
    assert pipeline.calibrate(fc).design.abc_delay_target_ps == 1000 - 180
    d["flow"] = {"mapping_target": {"scale": 3}}
    pack.write_text(yaml.safe_dump(d))
    with pytest.raises(ModeError, match="scale must be in"):
        FlowConfig.load(flow(tmp_path, pack), tmp_path / "out", env={})


def test_rail_default_target(mv_pack):
    # A rail's own flow: mapping_target wins for its domains.
    d = yaml.safe_load(mv_pack.read_text())
    d["flow"] = {"mapping_target": {"scale": 0.5, "flop_overhead": False}}
    d["rails"]["LO"]["flow"] = {"mapping_target": {"scale": 0.25,
                                                   "flop_overhead": False}}
    mv_pack.write_text(yaml.safe_dump(d))
    f = mv_pack.parent / "flow.yaml"
    f.write_text(yaml.safe_dump({
        "design": {"top_module": "top", "clock_period_ps": 1000,
                   "manifest": "m.yaml"},
        "pdk": {"profile": str(mv_pack)}, "pnr": {"clock_port": "clk"}}))
    design = DesignConfig.from_yaml(f)
    assert pipeline.mapping_target(design, "x", rail="HI")["target_ps"] == 500
    assert pipeline.mapping_target(design, "x", rail="LO")["target_ps"] == 250


def test_config_explicit_target(tmp_path, flop_lib):
    f = flow(tmp_path, schema1_pack(tmp_path, flop_lib),
             abc_delay_target_ps=777)
    d = DesignConfig.from_yaml(f)
    assert d.abc_target_ps == 777 and not d.calibrate
    assert d.clock_period_ps == 1000


def test_config_needs_period(tmp_path, flop_lib):
    f = flow(tmp_path, schema1_pack(tmp_path, flop_lib),
             clock_period_ps=None)
    with pytest.raises(KeyError, match="clock_period_ps is required"):
        DesignConfig.from_yaml(f)


# ---- the power flow's per-domain target ----------------------------------


def test_domain_timing(tmp_path, flop_lib):
    pack = tmp_path / "techpack.yaml"
    pack.write_text(yaml.safe_dump({
        "schema": 2, "name": "tst2", "time_unit": "ns", "public": True,
        "icg": {"cell": "X", "clk": "CK", "en": "E", "test_en": "SE",
                "out": "Q"},
        "default_rail": "A",
        "rails": {"A": {"voltage": 0.8, "liberties": [str(flop_lib)]}}}))
    (tmp_path / "f.sdc").write_text(
        "create_clock -name ca -period 1.0 [get_ports clk_a]\n"
        "create_clock -name cb -period 2.0 [get_ports clk_b]\n")
    (tmp_path / "t.sdc").write_text(
        "create_clock -name ca -period 1.2 [get_ports clk_a]\n"
        "create_clock -name cb -period 1.5 [get_ports clk_b]\n")
    f = flow(tmp_path, pack, clock_period_ps=None,
             modes={"f": "f.sdc", "t": "t.sdc"},
             power={"domains": [{"name": "m", "rail": "A",
                                 "instances": ["u_m"]}]})
    fc = FlowConfig.load(f, tmp_path / "out", env={})
    assert fc.design.power[0].clocks == ()
    rec = power.domain_timing(fc, MLIR, "top", "m", "A", ["u_m"])
    assert rec["modes"] == {"f": {"clocks": {"cb": 2000.0},
                                  "period_ps": 2000.0},
                            "t": {"clocks": {"cb": 1500.0},
                                  "period_ps": 1500.0}}
    assert rec["target_ps"] == 1500 - 180 and rec["clock_ports"] == ["clk"]
    top = power.domain_timing(fc, MLIR, "top", "remainder", "A", [])
    assert top["period_ps"] == 1000.0 and top["target_ps"] == 820
    assert top["clock_ports"] == ["clk_a", "clk_b"]
    named = power.domain_timing(fc, MLIR, "top", "m", "A", ["u_m"], ["ca"])
    assert named["period_ps"] == 1000.0


def test_power_domain_clocks_key(tmp_path, rails_pack):
    from circt_flow.power import PowerError
    d = {"design": {"top_module": "top", "clock_period_ps": 1000,
                    "manifest": "m.yaml"},
         "pdk": {"profile": str(rails_pack)},
         "power": {"domains": [{"name": "a", "rail": "LO",
                                "instances": ["u_a"], "clocks": ["c"]}]}}
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump(d))
    assert DesignConfig.from_yaml(f).power[0].clocks == ("c",)
    d["power"]["domains"][0]["clocks"] = "c"
    f.write_text(yaml.safe_dump(d))
    with pytest.raises(PowerError, match="clocks must be a list"):
        DesignConfig.from_yaml(f)


# ---- mvt-mini: the target derivation of mvt_soc ---------------------------

MINI = REPO / "examples/mvt-mini"


@pytest.fixture(scope="module")
def mini_libs(tmp_path_factory):
    out = tmp_path_factory.mktemp("minilib")
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(out)], check=True, capture_output=True)
    return out


def test_mvt_soc_modes_and_target(mini_libs):
    """The checked-in SDCs: hv's target follows turbo, lv's follows func,
    each minus its rail's worst ss flop (make_libs.py's numbers)."""
    soc = MINI / "designs/mvt_soc"
    ms = modes.parse_modes({"func": "func.sdc", "turbo": "turbo.sdc"}, soc,
                           "flow.yaml")
    assert all(not m.warnings for m in ms)
    ov_hv = modes.flop_overhead(sorted(mini_libs.glob("mini_*_0p75v_ss.lib")))
    ov_lv = modes.flop_overhead(sorted(mini_libs.glob("mini_*_0p55v_ss.lib")))
    # MDFF_X1_R at 0.75 V ss: (0.050 + 4 * 4 * 0.0015) * 1.2 + 0.1 * 0.005
    # clock-to-Q, 0.020 * 1.2 setup.
    assert ov_hv["cell"] == "MDFF_X1_R"
    assert ov_hv["clk_to_q_ps"] == pytest.approx(89.3)
    assert ov_hv["setup_ps"] == pytest.approx(24.0)
    spec = modes.TargetSpec()
    hv = modes.derive_target("hv", ms, {"clk_hv_i"}, spec, ov_hv)
    lv = modes.derive_target("lv", ms, {"clk_lv_i"}, spec, ov_lv)
    assert (hv["tightest_mode"], hv["target_ps"]) == ("turbo", 1186)
    assert (lv["tightest_mode"], lv["target_ps"]) == ("func", 1567)
    # tt would subtract less: the corner matters.
    ov_tt = modes.flop_overhead(sorted(mini_libs.glob("mini_*_0p75v_tt.lib")))
    assert ov_tt["overhead_ps"] < ov_hv["overhead_ps"]


def test_mvt_soc_flow_config():
    d = DesignConfig.from_yaml(MINI / "designs/mvt_soc/flow.yaml")
    assert [m.name for m in d.modes] == ["func", "turbo"]
    assert d.clock_period_ps == 1300 and d.calibrate
    assert [x.name for x in d.power] == ["lv", "hv"]


def test_pack_fanout_buffer(tmp_path, flop_lib):
    # The pack's flow.fanout_buffer turns the stage on; a flow config
    # overrides a key or turns it off.
    pack = schema1_pack(tmp_path, flop_lib)
    d = yaml.safe_load(pack.read_text())
    assert FlowConfig.load(flow(tmp_path, pack), tmp_path / "out",
                           env={}).design.fanout is None
    d["flow"] = {"fanout_buffer": {"cell": "BUF", "max_fanout": 12}}
    pack.write_text(yaml.safe_dump(d))
    fc = FlowConfig.load(flow(tmp_path, pack), tmp_path / "out", env={})
    assert fc.design.fanout.cell == "BUF" and fc.design.fanout.max_fanout == 12
    fc = FlowConfig.load(flow(tmp_path, pack, fanout_buffer={"max_fanout": 4}),
                         tmp_path / "out", env={})
    assert fc.design.fanout.max_fanout == 4
    assert FlowConfig.load(flow(tmp_path, pack, fanout_buffer=False),
                           tmp_path / "out", env={}).design.fanout is None
    d["flow"] = {"fanout_buffer": {"max_fanout": 12}}
    pack.write_text(yaml.safe_dump(d))
    with pytest.raises(KeyError, match="needs a cell"):
        FlowConfig.load(flow(tmp_path, pack), tmp_path / "out", env={})



def test_flop_overhead_skips_excluded_flops(flop_lib):
    """Flops matching an exclude pattern (the pack's dont_use: scan flops,
    say) never set the overhead, even when they are the slowest."""
    r = modes.flop_overhead([flop_lib], exclude=["*_SLOW"])
    assert r["cell"] == "TSTDFF_FAST" and r["flops_considered"] == 1
    with pytest.raises(ModeError, match="no flop"):
        modes.flop_overhead([flop_lib], exclude=["TSTDFF*"])
