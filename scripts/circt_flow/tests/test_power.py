"""The flow config's power: section, hierarchy resolution, the MLIR split
and the netlist stitch (no tools needed)."""
import textwrap

import pytest
import yaml

from circt_flow import power
from circt_flow.config import DesignConfig, FlowConfig
from circt_flow.power import PowerError


def flow_yaml(tmp_path, pack, power_sec=None, rail=None, top="top"):
    d = {"design": {"top_module": top, "clock_period_ps": 1000,
                    "manifest": "m.yaml"},
         "pdk": {"profile": str(pack)}}
    if rail is not None:
        d["pdk"]["rail"] = rail
    if power_sec is not None:
        d["power"] = power_sec
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump(d))
    return f


def doms(*entries):
    return {"domains": list(entries)}


# ---- flow config ----------------------------------------------------------


def test_power_ok(tmp_path, rails_pack):
    cfg = DesignConfig.from_yaml(flow_yaml(tmp_path, rails_pack, doms(
        {"name": "lo", "rail": "LO", "instances": ["u_a", "u_b.u_c"]},
        {"name": "hi", "rail": "HI", "module": "blk"},
        {"name": "one", "rail": "LO", "instance": "u_d"})))
    assert cfg.power == (
        power.Domain("lo", "LO", ("u_a", "u_b.u_c")),
        power.Domain("hi", "HI", (), "blk"),
        power.Domain("one", "LO", ("u_d",)))
    # The top-level run is on the default rail.
    assert cfg.rail == "HI" and cfg.voltage == 0.8
    assert [p.split("/")[-1] for p in cfg.synth_lib_tt] == ["hi_a.lib",
                                                            "hi_b.lib"]


def test_no_power_section(tmp_path, rails_pack):
    cfg = DesignConfig.from_yaml(flow_yaml(tmp_path, rails_pack))
    assert cfg.power is None and cfg.core_mlir is None


def test_pdk_rail_selects_libraries(tmp_path, rails_pack):
    f = flow_yaml(tmp_path, rails_pack, rail="LO")
    cfg = DesignConfig.from_yaml(f)
    assert cfg.rail == "LO" and cfg.voltage == 0.5
    assert [p.split("/")[-1] for p in cfg.synth_lib_tt] == ["lo_a.lib"]
    assert cfg.flow_defaults["abc_read_lib_args"] == "-l"
    fc = FlowConfig.load(f, tmp_path / "out", env={})
    assert fc.abc_read_lib_args == "-l"
    assert fc.abc_script_file.name == "lo.abc"
    assert fc.design.lib.endswith("lo_a.lib")


def test_pdk_rail_unknown(tmp_path, rails_pack):
    with pytest.raises(ValueError, match="pdk.rail 'MID' is not a rail"):
        DesignConfig.from_yaml(flow_yaml(tmp_path, rails_pack, rail="MID"))


@pytest.mark.parametrize("sec,err", [
    (doms({"name": "a", "rail": "MID", "instances": ["u"]}),
     r"unknown rail 'MID'; tstrails has rails HI, LO"),
    (doms({"name": "a", "rail": "LO"}), "exactly one of"),
    (doms({"name": "a", "rail": "LO", "instances": ["u"], "module": "m"}),
     "exactly one of"),
    (doms({"name": "a", "rail": "LO", "instances": []}), "non-empty list"),
    (doms({"name": "a", "rail": "LO", "instances": ["u/x"]}),
     "instance paths"),
    (doms({"name": "a b", "rail": "LO", "instances": ["u"]}),
     "must be an identifier"),
    (doms({"name": "a", "rail": "LO", "instances": ["u"], "vdd": 1}),
     "unknown key 'vdd'"),
    (doms({"name": "a", "rail": "LO", "instances": ["u"]},
          {"name": "a", "rail": "HI", "instances": ["v"]}), "used twice"),
    (doms({"name": "a", "rail": "LO", "instances": ["u"]},
          {"name": "b", "rail": "HI", "instances": ["u"]}),
     "overlapping domains: instance u is also in domain a"),
    (doms({"name": "a", "rail": "LO", "instances": ["u"]},
          {"name": "b", "rail": "HI", "instances": ["u.v"]}),
     "domains a and b overlap: u.v is inside u"),
    (doms({"name": "a", "rail": "LO", "module": "m"},
          {"name": "b", "rail": "HI", "module": "m"}),
     "module m is also domain a"),
    ({"domains": []}, "non-empty list"),
    ({"domain": []}, "one key"),
])
def test_power_errors(tmp_path, rails_pack, sec, err):
    with pytest.raises(PowerError, match=err):
        DesignConfig.from_yaml(flow_yaml(tmp_path, rails_pack, sec))


def test_power_needs_schema2(tmp_path):
    from conftest import write_lib, CELLS
    write_lib(tmp_path / "a.lib", "a", CELLS)
    pack = tmp_path / "techpack.yaml"
    pack.write_text("schema: 1\nname: one\nliberties: [a.lib]\n"
                    "icg: {cell: TSTICG_X1}\n")
    with pytest.raises(PowerError, match="needs a schema-2 tech pack"):
        DesignConfig.from_yaml(flow_yaml(tmp_path, pack, doms(
            {"name": "a", "rail": "default", "instances": ["u"]})))


def test_power_needs_profile(tmp_path):
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump({
        "design": {"top_module": "t", "clock_period_ps": 1},
        "pdk": {"synth_lib_tt": ["x.lib"]},
        "power": doms({"name": "a", "rail": "LO", "instances": ["u"]})}))
    with pytest.raises(ValueError, match="need a pdk.profile"):
        DesignConfig.from_yaml(f)


# ---- hierarchy -------------------------------------------------------------

MLIR = textwrap.dedent('''\
    module {
      hw.module.extern private @CELL(in %A : i1, out Y : i1)
      hw.module private @leaf(in %a : i8, out y : i8) {
        hw.output %a : i8
      }
      hw.module private @leaf_0(in %a : i4, out y : i4) {
        %c.Y = hw.instance "c" @CELL(A: %x: i1) -> (Y: i1)
        hw.output %a : i4
      }
      hw.module private @mid(in %a : i4, out y : i4) {
        %u_leaf.y = hw.instance "u_leaf" @leaf_0(a: %a: i4) -> (y: i4)
        hw.output %u_leaf.y : i4
      }
      hw.module private @blk(in %a : i1, out y : i1) {
        hw.output %a : i1
      }
      hw.module @top(in %a : i4, out y : i4, out z : i8) {
        %u_mid.y = hw.instance "u_mid" @mid(a: %a: i4) -> (y: i4)
        %u_p.y = hw.instance "u_p" @leaf(a: %0: i8) -> (y: i8)
        %u_b0.y = hw.instance "u_b0" @blk(a: %1: i1) -> (y: i1)
        %u_b1.y = hw.instance "u_b1" sym @s @blk(a: %1: i1) -> (y: i1)
        hw.output %u_mid.y, %u_p.y : i4, i8
      }
    }
    ''')


def test_parse_hierarchy():
    mods = power.parse_hierarchy(MLIR)
    assert mods["CELL"].extern and not mods["CELL"].public
    assert mods["top"].public and not mods["mid"].public
    assert mods["top"].instances == [("u_mid", "mid"), ("u_p", "leaf"),
                                     ("u_b0", "blk"), ("u_b1", "blk")]
    assert power.instance_paths(mods, "top") == {
        "u_mid": "mid", "u_mid.u_leaf": "leaf_0", "u_p": "leaf",
        "u_b0": "blk", "u_b1": "blk"}


def D(name, rail="LO", instances=(), module=None):
    return power.Domain(name, rail, tuple(instances), module)


def test_resolve_ok():
    mods = power.parse_hierarchy(MLIR)
    r = power.resolve([D("a", instances=["u_mid.u_leaf"]),
                       D("b", module="blk"), D("c", instances=["u_p"])],
                      mods, "top")
    assert [(x.domain.name, x.module, x.instances) for x in r] == [
        ("a", "leaf_0", ["u_mid.u_leaf"]), ("b", "blk", ["u_b0", "u_b1"]),
        ("c", "leaf", ["u_p"])]


@pytest.mark.parametrize("ds,err", [
    ([D("a", instances=["u_x"])],
     "unknown instance u_x; instances of top: u_b0, u_b1, u_mid, u_p"),
    ([D("a", instances=["u_mid.u_nope"])],
     "unknown instance u_mid.u_nope; instances of u_mid: u_leaf"),
    ([D("a", instances=["u_b0"])],
     "module blk is also instantiated at u_b1, outside the domain"),
    ([D("a", instances=["u_b0"]), D("b", instances=["u_b1"])],
     "both hold module blk"),
    ([D("a", instances=["u_mid", "u_p"])], "different modules"),
    ([D("a", module="leafy")], "not instantiated under top"),
    ([D("a", module="leaf_9")], "not instantiated"),
    ([D("a", module="top")], "is the top"),
    ([D("a", instances=["u_mid"]), D("b", module="leaf_0")],
     "domains a and b overlap: u_mid.u_leaf is inside u_mid"),
])
def test_resolve_errors(ds, err):
    with pytest.raises(PowerError, match=err):
        power.resolve(ds, power.parse_hierarchy(MLIR), "top")


def test_make_top_and_blackbox():
    mods = power.parse_hierarchy(MLIR)
    t = power.make_top(MLIR, mods, "mid")
    m2 = power.parse_hierarchy(t)
    assert [n for n, m in m2.items() if m.public and not m.extern] == ["mid"]
    assert m2["CELL"].extern and not m2["CELL"].public  # externs untouched
    b = power.blackbox(power.make_top(MLIR, mods, "top"), ["mid", "blk"])
    m3 = power.parse_hierarchy(b)
    assert m3["mid"].extern and m3["blk"].extern and not m3["top"].extern
    assert "hw.module.extern private @mid(in %a : i4, out y : i4)\n" in b
    assert m3["top"].instances == mods["top"].instances
    with pytest.raises(PowerError, match="could not black-box"):
        power.blackbox(b, ["nosuch"])


# ---- stitch ----------------------------------------------------------------

REM = textwrap.dedent('''\
    // Generated by CIRCT x
    module top_residual(	// a.mlir:1:1
      input  [3:0] in0,
      output       out0
    );
      INV_X1 u0 (.A (in0[0]), .Y (out0));
    endmodule

    module top(	// a.mlir:2:2
      input        clk,
      input  [3:0] a,
      output [3:0] y,
                   z
    );
      wire _r;
      lo_blk u_lo (	// a.mlir:3:3
        .clk (clk),
        .a   (a),
        .y   (y)
      );
      hi_blk u_hi (
        .a (y),
        .y (z)
      );
      top_residual u_residual (.in0 (a), .out0 (_r));
    endmodule
    ''')

LO = textwrap.dedent('''\
    // Generated by CIRCT x
    module lo_blk(	// b.mlir:1:1
      input        clk,
      input  [3:0] a,	// b.mlir:1:2
      output [3:0] y
    );
      DFF_X1 q (.CK (clk), .D (a[0]), .Q (y[0]));
    endmodule
    ''')

HI = textwrap.dedent('''\
    module hi_blk(
      input  [3:0] a,
      output [3:0] y
    );
      hi_blk_residual u_residual (.in0 (a), .out0 (y));
    endmodule
    module hi_blk_residual(input [3:0] in0, output [3:0] out0);
      INV_X1 u0 (.A (in0[0]), .Y (out0[0]));
    endmodule
    ''')


def test_verilog_modules():
    m = power.verilog_modules(REM)
    assert list(m) == ["top_residual", "top"]
    assert m["top"][0] == ["clk", "a", "y", "z"]
    assert power.verilog_modules(HI)["hi_blk_residual"][0] == ["in0", "out0"]


def test_stitch_ok():
    text, insts = power.stitch(REM, "top", {"lo_blk": LO, "hi_blk": HI})
    assert insts == {"lo_blk": ["u_lo"], "hi_blk": ["u_hi"]}
    mods = power.verilog_modules(text)
    assert list(mods) == ["top_residual", "top", "lo_blk", "hi_blk",
                          "hi_blk_residual"]
    assert text.index("module top(") < text.index("module lo_blk(")


def test_stitch_port_mismatch():
    bad = LO.replace("input        clk,\n", "")
    with pytest.raises(PowerError, match=r"top.u_lo connects port\(s\) clk"):
        power.stitch(REM, "top", {"lo_blk": bad, "hi_blk": HI})


def test_stitch_duplicate_module():
    dup = HI.replace("hi_blk_residual", "top_residual")
    with pytest.raises(PowerError, match="top_residual is defined by both"):
        power.stitch(REM, "top", {"lo_blk": LO, "hi_blk": dup})


def test_stitch_missing():
    with pytest.raises(PowerError, match="does not instantiate domain "
                                         "module other"):
        power.stitch(REM, "top", {"other": LO.replace("lo_blk", "other")})
    with pytest.raises(PowerError, match="does not define it"):
        power.stitch(REM, "top", {"lo_blk": HI})
    with pytest.raises(PowerError, match="does not define top"):
        power.stitch(LO, "top", {})
