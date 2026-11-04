"""Power intent: the netlist model, crossing detection, level-shifter choice
and insertion, the equivalence check, UPF and per-mode SDC writing."""
import pytest

from circt_flow import intent, profile, vnetlist as vn
from circt_flow.intent import IntentError
from circt_flow.modes import Clock, Mode
from circt_flow.power import PowerError, parse_options, parse_power

NETLIST = """\
module lo_core (
\tclk,
\ta,
\ty,
\tz
);
\tinput clk;
\tinput [1:0] a;
\toutput y;
\toutput z;
\twire n;
\tTAND g0(
\t\t.A(a[1]),
\t\t.B(a[0]),
\t\t.Y(n)
\t);
\tTDFF f0(
\t\t.CK(clk),
\t\t.D(n),
\t\t.Q(y)
\t);
\tTINV g1(
\t\t.A(n),
\t\t.Y(z)
\t);
endmodule
module hi2_core (
\tclk,
\td,
\tq
);
\tinput clk;
\tinput d;
\toutput q;
\tTDFF f0(
\t\t.CK(clk),
\t\t.D(d),
\t\t.Q(q)
\t);
endmodule
module top (
\tclk,
\ta,
\tb,
\tout1,
\tout2,
\tout3
);
\tinput clk;
\tinput [1:0] a;
\tinput b;
\toutput out1;
\toutput out2;
\toutput out3;
\twire _y;
\twire _z;
\twire _q;
\tlo_core u_lo(
\t\t.clk(clk),
\t\t.a({a[1], b}),
\t\t.y(_y),
\t\t.z(_z)
\t);
\thi2_core u_h2(
\t\t.clk(clk),
\t\t.d(_y),
\t\t.q(_q)
\t);
\tTAND g(
\t\t.A(_z),
\t\t.B(a[0]),
\t\t.Y(out2)
\t);
\tassign out1 = _q;
\tassign out3 = _y;
endmodule
"""


def domains(prof):
    return intent.domain_infos(prof, prof.default_rail, [
        {"name": "lo", "rail": "LO", "module": "lo_core",
         "netlist_instances": ["u_lo"]},
        {"name": "h2", "rail": "HI2", "module": "hi2_core",
         "netlist_instances": ["u_h2"]}])


def design(prof, text=NETLIST, shifters=None):
    return intent.Design(text, "top", domains(prof),
                         prof.rails["HI"].liberties, prof.level_shifters,
                         shifters)


# --- the netlist model -----------------------------------------------------


def test_bits_and_expressions():
    m = vn.parse(NETLIST)["top"]
    assert vn.bits_of("a", m) == [("a", 1), ("a", 0)]
    assert vn.bits_of("{a[1], b, 2'b10}", m) == [
        ("a", 1), ("b", None), ("'1", None), ("'0", None)]
    assert vn.bits_of("{2{b}}", m) == [("b", None)] * 2
    assert vn.bits_of("3'h5", m) == [("'1", None), ("'0", None), ("'1", None)]
    assert vn.expr_of([("a", 1), ("a", 0)], m) == "a"
    assert vn.expr_of([("a", 0)], m) == "a[0]"
    assert vn.expr_of([("a", 1), ("b", None), ("'0", None)], m) == \
        "{a[1], b, 1'b0}"
    with pytest.raises(vn.NetlistError, match="not declared"):
        vn.bits_of("nope", m)
    with pytest.raises(vn.NetlistError, match="out of range"):
        vn.bits_of("a[2]", m)


def test_parse_refuses_unknown_statements():
    with pytest.raises(vn.NetlistError, match="unsupported statement"):
        vn.parse("module m (a);\n input a;\n always @(a) x = a;\nendmodule\n")
    with pytest.raises(vn.NetlistError, match="named connections"):
        vn.parse("module m (a);\n input a;\n TBUF u (a, b);\nendmodule\n")


def test_write_module_round_trip():
    mods = vn.parse(NETLIST)
    again = vn.parse(vn.write_module(mods["top"]))["top"]
    assert again.ports == mods["top"].ports
    assert [(i.cell, i.name, i.conns) for i in again.instances] == \
        [(i.cell, i.name, i.conns) for i in mods["top"].instances]
    assert again.assigns == mods["top"].assigns


def test_two_drivers_refused(mv_pack):
    prof = profile.load(mv_pack)
    bad = NETLIST.replace("\tassign out3 = _y;\n",
                          "\tassign out3 = _y;\n\tassign _y = b;\n")
    with pytest.raises(vn.NetlistError, match="two drivers"):
        intent.find_crossings(design(prof, bad))


# --- crossings -------------------------------------------------------------


def test_crossings(mv_pack):
    prof = profile.load(mv_pack)
    cs = intent.find_crossings(design(prof))
    got = {(c.net, c.to_domain): (c.from_domain, c.rule, tuple(c.loads))
           for c in cs}
    assert got == {
        ("clk", "lo"): ("top", "high_to_low", ("u_lo/clk",)),
        ("clk", "h2"): ("top", "same_voltage", ("u_h2/clk",)),
        ("a[1]", "lo"): ("top", "high_to_low", ("u_lo/a[1]",)),
        ("b", "lo"): ("top", "high_to_low", ("u_lo/a[0]",)),
        ("out3", "h2"): ("lo", "low_to_high", ("u_h2/d",)),
        ("out3", "top"): ("lo", "low_to_high", ("out3",)),
        ("_z", "top"): ("lo", "low_to_high", ("g/A",)),
        ("out1", "top"): ("h2", "same_voltage", ("out1",)),
    }
    c = next(c for c in cs if c.net == "out3" and c.to_domain == "h2")
    assert (c.driver, c.from_rail, c.to_rail, c.from_voltage,
            c.to_voltage) == ("u_lo/y", "LO", "HI2", 0.5, 0.8)
    assert intent.rule_of(0.5, 0.8) == "low_to_high"
    assert intent.rule_of(0.8, 0.5) == "high_to_low"
    lines = intent.summarize(cs)
    assert any(l.startswith("top (HI) -> lo (LO): high_to_low, 3 net(s)")
               for l in lines)


def test_unknown_domain_instance(mv_pack):
    prof = profile.load(mv_pack)
    doms = intent.domain_infos(prof, "HI", [
        {"name": "lo", "rail": "LO", "module": "lo_core",
         "netlist_instances": ["u_nope"]}])
    with pytest.raises(IntentError, match="no instance u_nope"):
        intent.Design(NETLIST, "top", doms, prof.rails["HI"].liberties)
    doms = intent.domain_infos(prof, "HI", [
        {"name": "lo", "rail": "LO", "module": "lo_core",
         "netlist_instances": []}])
    with pytest.raises(IntentError, match="nested inside"):
        intent.Design(NETLIST, "top", doms, prof.rails["HI"].liberties)


# --- level shifters ---------------------------------------------------------


def test_choose_shifter(mv_pack):
    prof = profile.load(mv_pack)
    cells = intent.liberty_cells(prof.level_shifters)
    assert intent.choose_shifter(cells, "low_to_high", 0.5, 0.8).name == \
        "TLSLH"  # the smaller LH cell
    assert intent.choose_shifter(cells, "high_to_low", 0.8, 0.5).name == \
        "TLSHL"
    with pytest.raises(IntentError, match="no LH level shifter for 0.3 V"):
        intent.choose_shifter(cells, "low_to_high", 0.3, 0.8)
    # A bidirectional cell (Liberty's HL_LH) serves
    # both directions within its voltage ranges.
    both = intent.LibCell("TLS_BOTH", "l", area=0.5,
                          pins={"A": "input", "Y": "output"},
                          functions={"Y": "A"}, is_level_shifter=True,
                          ls_type="HL_LH", in_range=(0.4, 0.9),
                          out_range=(0.4, 0.9))
    for rule, a, b in (("low_to_high", 0.5, 0.8), ("high_to_low", 0.8, 0.5)):
        assert intent.choose_shifter(dict(cells, TLS_BOTH=both), rule, a,
                                     b).name == "TLS_BOTH"
    assert intent.shifter_pins(cells["TLSHL"]) == ("A", "Y")


def test_insert_level_shifters(mv_pack):
    prof = profile.load(mv_pack)
    des = design(prof)
    cs = intent.find_crossings(des)
    text, recs = intent.insert_level_shifters(design(prof), cs)
    assert len(recs) == 6
    by = {(r["net"], r["to_domain"]): r["cell"] for r in recs}
    assert by == {("clk", "lo"): "TLSHL", ("a[1]", "lo"): "TLSHL",
                  ("b", "lo"): "TLSHL", ("out3", "h2"): "TLSLH",
                  ("out3", "top"): "TLSLH", ("_z", "top"): "TLSLH"}
    top = vn.parse(text)["top"]
    u_lo = next(i for i in top.instances if i.name == "u_lo")
    assert dict(u_lo.conns)["a"] == "{a_1_ls_lo, b_ls_lo}"
    assert dict(u_lo.conns)["clk"] == "clk_ls_lo"
    u_h2 = next(i for i in top.instances if i.name == "u_h2")
    assert dict(u_h2.conns)["clk"] == "clk"  # same voltage: no shifter
    assert dict(u_h2.conns)["d"] == "out3_ls_h2"
    assert ("out3", "out3_ls_top") in top.assigns
    # Other modules untouched, byte for byte.
    sp_a, sp_b = vn.module_spans(NETLIST), vn.module_spans(text)
    for m in ("lo_core", "hi2_core"):
        assert NETLIST[slice(*sp_a[m])] == text[slice(*sp_b[m])]
    # Equivalent with the shifters as buffers, and nothing left to shift.
    new = design(prof, text, {r["instance"]: (r["from_domain"],
                                              r["to_domain"]) for r in recs})
    pins = {"TLSHL": ("A", "Y"), "TLSLH": ("A", "Y")}
    assert intent.check_equivalent(NETLIST, text, "top", des.pin_dir,
                                   new.pin_dir, pins) == []
    assert [c for c in intent.find_crossings(new)
            if c.rule != "same_voltage"] == []


def test_equivalence_catches_a_swap(mv_pack):
    prof = profile.load(mv_pack)
    cs = intent.find_crossings(design(prof))
    text, _ = intent.insert_level_shifters(design(prof), cs)
    bad = text.replace("{a_1_ls_lo, b_ls_lo}", "{b_ls_lo, a_1_ls_lo}")
    des, new = design(prof), design(prof, bad)
    probs = intent.check_equivalent(NETLIST, bad, "top", des.pin_dir,
                                    new.pin_dir, {"TLSHL": ("A", "Y"),
                                                  "TLSLH": ("A", "Y")})
    assert any("u_lo/a[1] is driven by b, was a[1]" in p for p in probs)
    # A changed domain module is caught too.
    bad2 = text.replace(".D(n)", ".D(a[0])", 1)
    probs = intent.check_equivalent(NETLIST, bad2, "top", des.pin_dir,
                                    new.pin_dir, {"TLSHL": ("A", "Y")})
    assert "module lo_core changed" in probs


def test_no_crossing_is_a_no_op(mv_pack):
    """Every domain at one voltage: crossings are found but nothing is
    shifted and the netlist text is unchanged apart from the top's layout."""
    prof = profile.load(mv_pack)
    doms = intent.domain_infos(prof, "HI", [
        {"name": "h2", "rail": "HI2", "module": "hi2_core",
         "netlist_instances": ["u_h2"]},
        {"name": "lo", "rail": "HI", "module": "lo_core",
         "netlist_instances": ["u_lo"]}])
    des = intent.Design(NETLIST, "top", doms, prof.rails["HI"].liberties,
                        prof.level_shifters)
    cs = intent.find_crossings(des)
    assert cs and all(c.rule == "same_voltage" for c in cs)
    text, recs = intent.insert_level_shifters(des, cs)
    assert recs == []
    assert vn.parse(text)["top"].instances == vn.parse(NETLIST)["top"].instances


def test_port_driven_directly_is_refused(mv_pack):
    prof = profile.load(mv_pack)
    text = NETLIST.replace(".z(_z)", ".z(out2)").replace(
        "\t\t.Y(out2)\n", "\t\t.Y(_z)\n")
    des = design(prof, text)
    cs = intent.find_crossings(des)
    with pytest.raises(IntentError, match="driven directly by u_lo/z"):
        intent.insert_level_shifters(des, cs)


# --- UPF and SDC -----------------------------------------------------------


def test_write_upf(mv_pack, tmp_path):
    prof = profile.load(mv_pack)
    des = design(prof)
    cs = intent.find_crossings(des)
    p = tmp_path / "top.upf"
    intent.write_upf(p, des, cs, "GND")
    upf = p.read_text()
    lines = upf.splitlines()
    assert "upf_version 2.0" in lines
    assert ("create_power_domain PD_TOP -include_scope "
            "-supply {primary SS_HI}") in lines
    assert any(l.startswith("create_power_domain PD_lo -elements {u_lo} "
                            "-supply {primary SS_LO}") for l in lines)
    assert any(l.startswith("create_power_domain PD_h2 -elements {u_h2} "
                            "-supply {primary SS_HI2}") for l in lines)
    for s in ("HI", "LO", "HI2", "GND"):
        assert f"create_supply_port {s} -direction in" in lines
        assert f"create_supply_net {s}" in lines
        assert f"connect_supply_net {s} -ports {s}" in lines
    assert ("create_supply_set SS_LO -function {power LO} "
            "-function {ground GND}") in lines
    assert not any(l.startswith("set_domain_supply_net") for l in lines)
    assert "add_port_state LO -state {LO_on 0.5}" in lines
    # Every shifter in its receiving domain, between the two rails' sets:
    # into PD_lo from the top (high to low: powered by LO), inside PD_lo;
    assert ("set_level_shifter ls_lo_in_HI_to_LO -domain PD_lo "
            "-applies_to inputs -rule high_to_low -location self "
            "-input_supply_set SS_HI -output_supply_set SS_LO "
            "-elements {u_lo/a u_lo/clk}") in lines
    # from PD_lo into the top's glue, in the parent (the top);
    assert ("set_level_shifter ls_lo_out_LO_to_HI -domain PD_lo "
            "-applies_to outputs -rule low_to_high -location parent "
            "-input_supply_set SS_LO -output_supply_set SS_HI "
            "-elements {u_lo/y u_lo/z}") in lines
    # from PD_lo into PD_h2, inside PD_h2.
    assert ("set_level_shifter ls_h2_in_LO_to_HI2 -domain PD_h2 "
            "-applies_to inputs -rule low_to_high -location self "
            "-input_supply_set SS_LO -output_supply_set SS_HI2 "
            "-elements {u_h2/d}") in lines
    # Inserted shifters: no strategies; each in the domain it drives.
    shifted, ins = intent.insert_level_shifters(design(prof), cs)
    intent.write_upf(p, des, cs, "GND", ins)
    lines = p.read_text().splitlines()
    assert not any(l.startswith("set_level_shifter") for l in lines)
    into_lo = {r["instance"] for r in ins if r["to_domain"] == "lo"}
    pd_lo = [l for l in lines if l.startswith("create_power_domain PD_lo ")]
    els = pd_lo[0].split("{", 1)[1].split("}", 1)[0].split()
    assert into_lo and els[0] == "u_lo" and set(els[1:]) == into_lo
    assert any(r["instance"] in p.read_text() for r in ins
               if r["to_domain"] == "top")
    # Their supply pins: an LH shifter's input side (VDDL) on the driving
    # rail, its output side (VDD) on the receiving one; an HL shifter's
    # one rail (VDDL) on the receiving (low) rail; grounds on GND.
    conn = {l.split()[1]: set(l.split("{", 1)[1].rstrip("}").split())
            for l in lines if l.startswith("connect_supply_net ")
            and "-pins" in l}
    for r in ins:
        i = r["instance"]
        if r["cell"] == "TLSLH":
            assert f"{i}/VDDL" in conn[des.domains[r["from_domain"]].rail]
            assert f"{i}/VDD" in conn[des.domains[r["to_domain"]].rail]
        else:
            assert r["cell"] == "TLSHL"
            assert f"{i}/VDDL" in conn["LO"]
        assert f"{i}/VSS" in conn["GND"]
    assert not any("NOT CONNECTED" in l for l in lines)
    with pytest.raises(IntentError, match="also a rail"):
        intent.write_upf(p, des, cs, "LO")


def test_shifter_supplies(mv_pack):
    prof = profile.load(mv_pack)
    cells = intent.liberty_cells(prof.level_shifters)
    assert intent.shifter_supplies(cells["TLSLH"]) == {
        "VDDL": "input", "VDD": "output", "VSS": "ground"}
    # One rail both pins relate to: the output side.
    assert intent.shifter_supplies(cells["TLSHL"]) == {
        "VDDL": "output", "VSS": "ground"}
    assert intent.shifter_supplies(cells["TLSLH_BIG"]) == {}
    # A library whose pg_pin groups were stripped still tells the sides.
    c = intent.LibCell("X", "l", pins={"A": "input", "Y": "output"},
                       related_power={"A": "VDDI", "Y": "VDD"},
                       related_ground={"A": "VSS", "Y": "VSS"})
    assert intent.shifter_supplies(c) == {"VDDI": "input", "VDD": "output",
                                          "VSS": "ground"}
    # A cell naming no supplies is reported, not guessed.
    des = design(prof)
    rec = {"instance": "u", "cell": "TLSLH_BIG", "from_domain": "lo",
           "to_domain": "top"}
    conn, missing = intent.shifter_connections(des, [rec], "GND")
    assert conn == {} and "names no supply pins" in missing[0]


def test_write_mode_sdcs(tmp_path):
    sdc = tmp_path / "f.sdc"
    sdc.write_text("create_clock -name c -period 2 [get_ports clk]\n")
    modes = (Mode("func", sdc, {"c": Clock("c", 2000.0, ("clk",))}),
             Mode("default", None, {"clk": Clock("clk", 1500.0, ())}))
    out = intent.write_mode_sdcs(tmp_path / "sdc", modes, "top", "ns",
                                 "clk", ["clk", "a"], ["y"])
    f = (tmp_path / "sdc" / "func.sdc").read_text()
    assert "create_clock -name c -period 2 [get_ports clk]" in f
    assert "# clock c: 2000 ps on clk" in f
    d = (tmp_path / "sdc" / "default.sdc").read_text()
    assert "create_clock -name clk -period 1.5 [get_ports clk]" in d
    assert "set_input_delay 0 -clock clk [get_ports {a}]" in d
    assert "set_output_delay 0 -clock clk [get_ports {y}]" in d
    assert set(out) == {"func", "default"}


# --- the power: options ----------------------------------------------------


def test_power_options(mv_pack):
    prof = profile.load(mv_pack)
    sec = {"domains": [{"name": "lo", "rail": "LO", "instances": ["u_lo"]}],
           "insert_level_shifters": True, "ground": "GND"}
    assert parse_power(sec, prof, "f.yaml")[0].name == "lo"
    assert parse_options(sec, "f.yaml") == {"insert_level_shifters": True,
                                            "ground": "GND"}
    assert parse_options({"domains": []}, "f") == {
        "insert_level_shifters": False, "ground": "VSS"}
    with pytest.raises(PowerError, match="true or false"):
        parse_options({"insert_level_shifters": "yes"}, "f")
    with pytest.raises(PowerError, match="supply name"):
        parse_options({"ground": "1x"}, "f")
    # The ground may not be named like a rail (checked at load).
    with pytest.raises(PowerError, match="also a rail"):
        parse_options({"ground": "LO"}, "f", prof)
    with pytest.raises(PowerError, match="reserved"):
        parse_power({"domains": [{"name": "top", "rail": "LO",
                                  "instances": ["u_lo"]}]}, prof, "f")
    with pytest.raises(PowerError, match="unknown key 'isolation'"):
        parse_power({"domains": [], "isolation": 1}, prof, "f")
