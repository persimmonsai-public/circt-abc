"""Post-layout optimisation (circt_flow/postlayout.py): config parsing, the
LEF / liberty readers, the ECO script, and a real OpenSTA run on a chain
whose clock passes two buffers (skipped without docker or $OPENSTA)."""
import os
import re
import shutil
import subprocess
import sys

import pytest
from pathlib import Path

from circt_flow import postlayout, vtswap
from circt_flow.config import ROOT
from circt_flow.postlayout import PostLayoutSpec, parse_post_layout
from circt_flow.vtswap import VtSwapError
from test_wire_rc import rc_pack  # noqa: F401  (fixture: an invented pack)

def _have_sta():
    if os.environ.get("OPENSTA"):
        return True
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", vtswap.ORFS_IMAGE],
                          capture_output=True).returncode == 0


MINI = ROOT / "examples" / "mvt-mini"


def test_parse_post_layout():
    assert postlayout.parse_post_layout(None, "f") == PostLayoutSpec()
    assert postlayout.parse_post_layout(False, "f").enabled is False
    s = postlayout.parse_post_layout(
        {"margin_ps": 4, "paths": 300, "false_to": ["outputs"],
         "hold_tol_ps": 5, "fix": "bulk"}, "f")
    assert (s.margin_ps, s.paths, s.false_to, s.hold_tol_ps, s.fix) == (
        4.0, 300, ("outputs",), 5.0, "bulk")
    for bad, msg in (({"nope": 1}, "post_layout.nope is not a key"),
                     ({"paths": 0}, "positive integer"),
                     ({"margin_ps": -1}, "margin_ps"),
                     ({"fix": "x"}, "sens or bulk"),
                     ({"hold_scope": "x"}, "r2r or all"),
                     ({"allow_footprint_change": 1}, "true or false")):
        with pytest.raises(VtSwapError, match=msg):
            postlayout.parse_post_layout(bad, "f")


def test_lef_sizes_ignores_sites_and_properties(tmp_path):
    lef = tmp_path / "a.lef"
    lef.write_text("""SITE s
  SIZE 0.054 BY 0.2 ;
END s
PROPERTYDEFINITIONS
  MACRO LEF58_EEQ STRING ;
END PROPERTYDEFINITIONS
MACRO INV_R
  CLASS CORE ;
  SIZE 0.108 BY 0.2 ;
END INV_R
MACRO INV_L
  SIZE 0.108 BY 0.2 ;
END INV_L
""")
    assert postlayout.lef_sizes([lef]) == {"INV_R": (0.108, 0.2),
                                           "INV_L": (0.108, 0.2)}


def test_liberty_table_state_averages(tmp_path):
    lib = tmp_path / "a.lib"
    lib.write_text("""library (l) {
  cell (A) {
    area : 2.0 ;
    cell_leakage_power : 9.0 ;
    leakage_power () { related_pg_pin : "VDD" ; when : "!a" ; value : "1.0" ; }
    leakage_power () { related_pg_pin : "VDD" ; when : "a" ; value : "3.0" ; }
  }
  cell (B) {
    area : 1.5 ;
    cell_leakage_power : 4.0 ;
  }
}
""")
    t = postlayout.liberty_table([lib])
    assert t["A"] == (2.0, 2.0) and t["B"] == (1.5, 4.0)
    c = postlayout.census("module m (a);\n  A g0 (.A(a));\n  A g1 (.A(a));\n"
                          "  B g2 (.A(a));\n  Z g3 (.A(a));\nendmodule\n", t)
    assert c["instances"] == 4 and c["area"] == 5.5 and c["leakage"] == 8.0
    assert c["unknown"] == {"Z": 1}


def test_eco_script_and_hooks(tmp_path):
    p = tmp_path / "eco.tcl"
    postlayout.write_eco_tcl(p, [("\\u/g[3]", "INV_S"), ("g1", "INV_L")])
    t = p.read_text()
    assert "eco_replace {u/g[3]} INV_S" in t and "eco_replace {g1} INV_L" in t
    assert "swapMaster" in t and "ECO applied" in t
    h = vtswap.LayoutSpec(sizes={"A": (1.0, 2.0)}, hold_tol=0.005
                          ).hooks_tcl()
    assert "fp_size" in h and "{A} {1 2}" in h and "set hold_tol 0.005" in h
    assert "read_spef" not in h
    pre = vtswap.LayoutSpec(spef="x.spef").pre_tcl(hold_libs=True)
    assert "-corner setup" in pre and "-corner hold" in pre
    assert "set_propagated_clock" in pre


CHAIN = 12


def _netlist():
    """Clock port -> two buffers -> flop -> CHAIN inverters
    -> flop."""
    body = ["module chain (clk, d, q);", "  input clk;", "  input d;",
            "  output q;", "  wire c0;", "  wire ck;", "  wire n0;"]
    body += [f"  wire n{i};" for i in range(1, CHAIN + 1)]
    body += ["  MBUF_X1_R cb0 (.A(clk), .Y(c0));",
             "  MBUF_X1_R cb1 (.A(c0), .Y(ck));",
             "  MDFF_X1_R f0 (.CK(ck), .D(d), .Q(n0));"]
    body += [f"  MINV_X1_R g{i} (.A(n{i - 1}), .Y(n{i}));"
             for i in range(1, CHAIN + 1)]
    body.append(f"  MDFF_X1_R f1 (.CK(ck), .D(n{CHAIN}), .Q(q));")
    body.append("endmodule")
    return "\n".join(body) + "\n"


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_post_layout_swaps_data_cells_not_the_clock_network(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_netlist())
    sdc = tmp_path / "c.sdc"

    def run(period, out, sizes=None, **kw):
        sdc.write_text(vtswap.sdc_for(period, "clk", ["d"]))
        lay = vtswap.LayoutSpec(sizes=sizes, **kw)
        return vtswap.swap(nl, "chain", tmp_path / out, libs,
                           ["R", "L", "S"], r"_([RLS])$", sdc.read_text(),
                           recover=False, layout=lay)

    # The mini library's buffers are not unate to OpenSTA, so the setup
    # check is a half-period one: find a period that fails at RVT.
    for period in (400, 300, 250, 200, 150, 120):
        res = run(period, "tight.v")
        if res["start"]["wns"] < 0:
            break
    text = (tmp_path / "tight.v").read_text()
    assert res["start"]["wns"] < 0 and res["up"]
    assert res["end"]["wns"] > res["start"]["wns"]
    assert res["eco_stat"]["clock_cells"] == 2
    assert res["eco_stat"]["blocked_clock"] > 0
    # The clock buffers keep their cell; data cells moved.
    assert re.search(r"MBUF_X1_R cb0", text) and re.search(r"MBUF_X1_R cb1", text)
    assert any(not c.endswith("_R") for c in
               re.findall(r"^\s*(M\w+)\s+g\d", text, re.M))
    # Liberty-area footprint rule: the mini libraries' flavours share area.
    assert "blocked_footprint" not in res["eco_stat"]
    # A LEF where the fast flavour is wider: its swaps are vetoed.
    sizes = {f"{b}_{f}": ((2.0 if f == "S" else 1.0), 1.0)
             for b in ("MINV_X1", "MDFF_X1", "MBUF_X1") for f in "RLS"}
    res2 = run(period, "lef.v", sizes=sizes)
    cells = re.findall(r"^\s*(M\w+)\s+g\d", (tmp_path / "lef.v").read_text(),
                       re.M)
    assert not any(c.endswith("_S") for c in cells)
    assert res2["eco_stat"]["blocked_footprint"] > 0
    # ... and lifted by allow_footprint_change.
    res3 = run(period, "free.v", sizes=sizes, allow_footprint_change=True)
    assert "blocked_footprint" not in res3["eco_stat"]


def test_one_line_reports_in2r_hold_when_measured():
    side = {"wns": 0.0, "tns": 0.0, "r2r_viol": 0, "leakage": 1.0}
    rec = {"before": {**side, "in2r_hold_wns": -0.004},
           "after": {**side, "in2r_hold_wns": -0.005},
           "instances_changed": 3, "footprint_changed": []}
    assert "in2r hold -0.0040 -> -0.0050" in postlayout.one_line(rec)
    del rec["before"]["in2r_hold_wns"]
    assert "in2r" not in postlayout.one_line(rec)


def test_measure_script_has_in2r_hold():
    assert 'puts "M in2r_hold_wns' in postlayout.MEASURE


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_measure_reports_input_to_register_hold(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_netlist())
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -name clk -period 0.7 [get_ports clk]\n"
                   "set_input_delay 0 -clock clk [get_ports d]\n"
                   "set_output_delay 0 -clock clk [get_ports q]\n")
    res = postlayout.measure(nl, "chain", libs, sdc, None)
    assert "in2r_hold_wns" in res and "r2r_hold_wns" in res


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_ladder_cap_and_leakage_ordered_recovery(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_netlist())
    sdc = tmp_path / "c.sdc"
    table = postlayout.liberty_table(libs)
    leak = {c: v[1] for c, v in table.items()}

    def run(period, out, **kw):
        sdc.write_text(vtswap.sdc_for(period, "clk", ["d"]))
        lay = vtswap.LayoutSpec(leakage=leak, fix="bulk", **kw)
        return vtswap.swap(nl, "chain", tmp_path / out, libs,
                           ["R", "L", "S"], r"_([RLS])$", sdc.read_text(),
                           layout=lay)

    for period in (400, 300, 250, 200, 150, 120):
        base = run(period, "base.v")
        if base["start"]["wns"] < 0:
            break
    capped = run(period, "cap.v", ladder_cap="L")
    cells = re.findall(r"^\s*(M\w+)\s+g\d", (tmp_path / "cap.v").read_text(),
                       re.M)
    assert not any(c.endswith("_S") for c in cells)
    ordered = run(period, "ord.v", recover_order="leak")
    assert ordered["end"]["wns"] >= min(0.0, ordered["start"]["wns"]) - 1e-9


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_closing_pass_lifts_the_cap_only_for_what_is_short(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_netlist())
    sdc = tmp_path / "c.sdc"
    table = postlayout.liberty_table(libs)
    leak = {c: v[1] for c, v in table.items()}

    def run(period, out, **kw):
        sdc.write_text(vtswap.sdc_for(period, "clk", ["d"]))
        lay = vtswap.LayoutSpec(leakage=leak, fix="sens",
                                recover_order="leak", **kw)
        return vtswap.swap(nl, "chain", tmp_path / out, libs,
                           ["R", "L", "S"], r"_([RLS])$", sdc.read_text(),
                           layout=lay)

    # a period the capped fix cannot close but the full ladder can
    found = None
    for period in range(600, 380, -10):
        free = run(period, "free.v")
        if free["end"]["wns"] < -1e-9:
            break
        capped = run(period, "cap.v", ladder_cap="L")
        if capped["end"]["wns"] < -1e-9:
            found = period
            break
    assert found, "no period separates the capped and the free ladder"
    closed = run(found, "esc.v", ladder_cap="L", ladder_escalate="S")
    assert closed["end"]["wns"] >= -1e-9
    assert closed["end"]["wns"] > capped["end"]["wns"]
    # without a shortfall the cap stays: nothing is lifted
    easy = run(600, "easy.v", ladder_cap="L", ladder_escalate="S")
    cells = re.findall(r"^\s*(M\w+)\s+g\d", (tmp_path / "easy.v").read_text(),
                       re.M)
    assert not any(c.endswith("_S") for c in cells)


def test_optimiser_sdc_false_paths():
    base = "create_clock -period 1 [get_ports clk]\n"
    plain = postlayout.optimiser_sdc(
        base, PostLayoutSpec(false_to=("outputs", "p"), false_from=("r",)))
    assert plain.startswith(base)
    assert "set_false_path -to [all_outputs]" in plain
    assert "set_false_path -to [get_ports p]" in plain
    assert "set_false_path -from [get_ports r]" in plain


def test_closing_pass_keys():
    spec = parse_post_layout({"ladder_cap": "L", "ladder_escalate": "S",
                              "close_margin_ps": 7}, "t")
    assert spec.ladder_escalate == "S" and spec.close_margin_ps == 7.0
    assert parse_post_layout({}, "t").ladder_escalate == ""
    sv = vtswap.parse_vt_swap({"ladder_escalate": "S", "close_margin_ps": 1},
                              "t")
    assert sv.ladder_escalate == "S" and sv.close_margin_ps == 1.0


def test_second_wire_view_is_a_third_corner(tmp_path):
    spec = postlayout.parse_post_layout({"also_spef": "/x/pl.spef"}, "t")
    assert spec.also_spef == "/x/pl.spef"
    assert postlayout.PostLayoutSpec().also_spef == ""
    lay = vtswap.LayoutSpec(spef=tmp_path / "gr.spef", spef2=tmp_path / "pl.spef")
    t = lay.pre_tcl(True)
    assert "read_spef -corner setup2" in t and "pl.spef" in t
    assert "setup2" not in vtswap.LayoutSpec(spef=tmp_path / "gr.spef").pre_tcl(True)


@pytest.mark.parametrize("bad,msg", [
    ({"sens_frac": 0}, "sens_frac"), ({"sens_frac": 1.5}, "sens_frac"),
    ({"recover_order": "x"}, "recover_order"), ({"ladder_cap": 3}, "ladder_cap"),
    ({"close_margin_ps": 5}, "needs ladder_escalate"),
    ({"close_margin_ps": -1, "ladder_escalate": "S"}, "close_margin_ps"),
    ({"recover_swap": -1}, "recover_swap"), ({"recover_swap": True}, "recover_swap"),
    ({"also_spef": 1}, "also_spef"), ({"false_to": "outputs"}, "false_to"),
    ({"false_from": "rst"}, "false_from"), ({"false_to": [1]}, "false_to"),
    ({"hold_tol_ps": -2}, "hold_tol_ps")])
def test_post_layout_keys_are_validated_at_load(bad, msg):
    with pytest.raises(VtSwapError, match=msg):
        parse_post_layout(bad, "f")


def test_vt_swap_close_margin_needs_escalate_and_tokens_are_checked():
    with pytest.raises(VtSwapError, match="vt_swap.close_margin_ps needs ladder_escalate"):
        vtswap.parse_vt_swap({"close_margin_ps": 3}, "f")
    vtswap.check_ladder_tokens("L", "S", ["R", "L", "S"], "x")
    with pytest.raises(VtSwapError, match="not a token"):
        vtswap.check_ladder_tokens("Q", "", ["R", "L", "S"], "pnr.post_layout")


def test_pair_recovery_records_its_compensating_move_as_a_pair():
    assert 'puts $rec "up pair' in vtswap.TCL


# leakage_power groups as the open libraries write them (invented values):
# value before when, related_pg_pin last (ASAP7 style) and no related_pg_pin
# (Nangate45 style); one with two power pins per state.
ASAP7_STYLE = """
  cell (A_R) {
    area : 0.1;
    cell_leakage_power : 99;
    leakage_power () {
      value : 4.0;
      when : "(A * !Y)";
      related_pg_pin : VDD;
    }
    leakage_power () {
      value : 2.0;
      when : "(!A * Y)";
      related_pg_pin : VDD;
    }
  }
"""
NANGATE_STYLE = """
  cell (B_X1) {
	area		: 1.0;
	cell_leakage_power 	: 99;
	leakage_power () {
		when           : "!A1 & !A2";
		value          : 1.0;
	}
	leakage_power () {
		when           : "A1 & A2";
		value          : 3.0;
	}
  }
"""
TWO_PINS = """
  cell (C) {
    leakage_power () { related_pg_pin : VDD ; when : "A" ; value : 1.0 ; }
    leakage_power () { when : "A" ; value : 0.5 ; related_pg_pin : VDDL ; }
    leakage_power () { value : 3.0 ; when : "!A" ; }
  }
  cell (D) {
    leakage_power () { value : 2.5 ; }
  }
  cell (E) {
    cell_leakage_power : 7 ;
  }
"""


def test_leakage_groups_in_any_attribute_order(tmp_path):
    lib = tmp_path / "l.lib"
    lib.write_text("library (l) {" + ASAP7_STYLE + NANGATE_STYLE + TWO_PINS + "}\n")
    t = postlayout.liberty_table([lib])
    assert t["A_R"] == (0.1, 3.0) and t["B_X1"] == (1.0, 2.0)
    assert t["C"][1] == 2.25          # ((1.0 + 0.5) + 3.0) / 2
    assert t["D"][1] == 2.5 and t["E"][1] == 7.0


def test_also_spef_needs_a_hold_corner(tmp_path, monkeypatch):
    pnr = tmp_path / "pnr"
    pnr.mkdir()
    for n in ("placed.v", "placed.spef", "constraint.sdc"):
        (pnr / n).write_text("x")
    prof = __import__("types").SimpleNamespace(
        flavours_at=lambda rail: {"tokens": ["R", "L"], "re": "x"},
        libs_at=lambda *a, **k: [], hold_corner=None, setup_corner="tt")
    from circt_flow import profile as profile_mod
    monkeypatch.setattr(profile_mod, "load", lambda p: prof)
    monkeypatch.setattr(postlayout, "spef_for_timing", lambda s, o, r, *a: s)
    cfg = __import__("types").SimpleNamespace(design=__import__("types").SimpleNamespace(
        profile="p", rail=None, top_module="t", time_unit="ns", pnr={},
        pack_pnr={"command": ["x"]}, config_dir=tmp_path))
    with pytest.raises(VtSwapError, match="also_spef needs a pack hold corner"):
        postlayout.run_flow(cfg, PostLayoutSpec(also_spef="pl.spef"), pnr)
    with pytest.raises(VtSwapError, match="not a token"):
        postlayout.run_flow(cfg, PostLayoutSpec(ladder_cap="Q"), pnr)


@pytest.mark.parametrize("bad", [
    {"margin_ps": float("inf")}, {"margin_ps": float("nan")},
    {"hold_tol_ps": float("inf")}, {"fix": "sense"},
    {"close_margin_ps": float("nan"), "ladder_escalate": "S"},
    {"sens_frac": "x"}])
def test_numbers_are_finite_and_enums_known(bad):
    with pytest.raises(VtSwapError):
        parse_post_layout(bad, "f")
    if "hold_tol_ps" not in bad:
        with pytest.raises(VtSwapError):
            vtswap.parse_vt_swap(bad, "f")


def test_recipe_numbers_are_finite():
    from circt_flow import pnr_recipe as pr
    for bad in ({"hold_margin": float("inf")}, {"input_delay_min": float("nan")}):
        with pytest.raises(pr.RecipeError, match="finite"):
            pr.parse(bad)



@pytest.mark.parametrize("pnr_extra,top_extra,msg", [
    ("\n  post_layout: {fix: sense}", "", "fix must be sens or bulk"),
    ("\n  post_layout: {ladder_cap: Q}", "", "not a token"),
    ("", "vt_swap: {ladder_cap: Q, fix: sens}", "not a token"),
    ("", "vt_swap: {close_margin_ps: 3}", "needs ladder_escalate")])
def test_post_layout_and_vt_swap_keys_fail_when_the_config_is_read(
        tmp_path, rc_pack, pnr_extra, top_extra, msg):
    """A wrong post_layout or vt_swap key stops the flow when its config is
    loaded, before any stage runs."""
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    rc_pack.write_text(rc_pack.read_text() +
                       "\nvt_flavours: {tokens: [R, L, S], re: '_([RLS])$'}\n")
    f = _flow(tmp_path, rc_pack, top_extra)
    f.write_text(f.read_text().replace("pnr: {clock_port: clk}",
                                       "pnr:\n  clock_port: clk" + pnr_extra))
    with pytest.raises(VtSwapError, match=msg):
        FlowConfig.load(f, tmp_path / "o")


@pytest.mark.parametrize("key,value,ok", [
    ("hold_margin", ".nan", False), ("setup_margin", "-.inf", False),
    ("hold_margin", "abc", False), ("hold_margin", "true", False),
    ("hold_margin", "'0.05'", True), ("setup_margin", "3", True)])
def test_flow_config_margins_are_checked_when_the_config_is_read(
        tmp_path, rc_pack, key, value, ok):
    """The flow config's pnr.hold_margin / setup_margin are refused when it
    is loaded (the same check as a pack's), not when the P&R stage starts."""
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    from circt_flow import pnr_recipe as pr
    f = _flow(tmp_path, rc_pack)
    f.write_text(f.read_text().replace("pnr: {clock_port: clk}",
                                       f"pnr:\n  clock_port: clk\n  {key}: {value}"))
    if ok:
        FlowConfig.load(f, tmp_path / "o")
    else:
        with pytest.raises(pr.RecipeError, match=key):
            FlowConfig.load(f, tmp_path / "o")


def test_post_layout_needs_a_flavour_ladder_when_the_config_is_read(tmp_path, rc_pack):
    from test_wire_rc import _flow
    from circt_flow.config import FlowConfig
    f = _flow(tmp_path, rc_pack)
    f.write_text(f.read_text().replace("pnr: {clock_port: clk}",
                                       "pnr:\n  clock_port: clk\n  post_layout: true"))
    with pytest.raises(VtSwapError, match="vt_flavours"):
        FlowConfig.load(f, tmp_path / "o")


def test_cli_on_a_pack_without_a_flavour_ladder(tmp_path, rc_pack, monkeypatch, capsys):
    """--measure-only needs no flavour ladder; a swap on such a pack is a
    clear error, not a traceback."""
    monkeypatch.setattr(postlayout, "spef_for_timing", lambda s, o, r, *a: s)
    monkeypatch.setattr(postlayout, "measure", lambda *a, **k: {"wns": 0.0})
    args = ["--pack", str(rc_pack), "--netlist", str(tmp_path / "n.v"),
            "--spef", str(tmp_path / "n.spef"), "--sdc", str(tmp_path / "c.sdc"),
            "--top", "top", "--out", str(tmp_path / "o")]
    (tmp_path / "o").mkdir()
    assert postlayout.main(args + ["--measure-only"]) == 0
    assert (tmp_path / "o" / "measure.json").exists()
    assert postlayout.main(args) == 2
    assert "has no vt_flavours" in capsys.readouterr().err
