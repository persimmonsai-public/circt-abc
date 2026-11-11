"""The threshold-voltage swap (circt_flow/vtswap.py): config parsing, and a
real OpenSTA run on a chain of the mini library's cells (skipped without
docker or $OPENSTA)."""
import os
import re
import shutil
import subprocess
import sys

import pytest

from circt_flow import vtswap
from circt_flow.config import ROOT
from circt_flow.vtswap import VtSwapError, VtSwapSpec

MINI = ROOT / "examples" / "mvt-mini"


def test_parse_vt_swap():
    assert vtswap.parse_vt_swap(None, "f") == VtSwapSpec()
    assert vtswap.parse_vt_swap(False, "f").enabled is False
    s = vtswap.parse_vt_swap({"false_from": ["rst_ni"], "paths": 50,
                              "recover": False}, "f")
    assert s.false_from == ("rst_ni",) and s.paths == 50 and not s.recover
    assert vtswap.parse_vt_swap({"margin_ps": 20}, "f").margin_ps == 20.0
    for bad, msg in (({"nope": 1}, "vt_swap.nope is not a key"),
                     ({"false_from": "rst_ni"}, "list of port names"),
                     ({"paths": 0}, "positive integer"),
                     ({"recover": "yes"}, "true or false"),
                     ({"margin_ps": -1}, "margin_ps")):
        with pytest.raises(VtSwapError, match=msg):
            vtswap.parse_vt_swap(bad, "f")


def test_parse_flavours():
    assert vtswap.parse_flavours(None, "p") is None
    assert vtswap.parse_flavours({"tokens": ["R", "L", "S"],
                                  "re": "_([RLS])$"}, "p") == {
        "tokens": ["R", "L", "S"], "re": "_([RLS])$"}
    with pytest.raises(KeyError, match="two or more"):
        vtswap.parse_flavours({"tokens": ["R"]}, "p")
    with pytest.raises(KeyError, match="at least one group"):
        vtswap.parse_flavours({"tokens": ["R", "L"], "re": "_[RL]$"}, "p")


def test_parse_flavours_two_axes():
    """A regex with several groups: tokens name one value per group."""
    d = {"tokens": ["R/A", "R/B", "L/A"], "re": "_([RL])_([AB])$"}
    assert vtswap.parse_flavours(d, "p") == d
    with pytest.raises(KeyError, match="2 groups"):
        vtswap.parse_flavours({"tokens": ["R", "L"],
                               "re": "_([RL])_([AB])$"}, "p")
    with pytest.raises(KeyError, match="2 groups"):
        vtswap.parse_flavours({"tokens": ["R/A", "L/A/x"],
                               "re": "_([RL])_([AB])$"}, "p")
    text = ("module m (a);\n  input a;\n  wire n;\n"
            "  INV_R_A g0 (.A(a), .Y(n));\n  INV_R_B g1 (.A(a), .Y(n));\n"
            "  INV_L_A g2 (.A(a), .Y(n));\n  INV_L_B g3 (.A(a), .Y(n));\n"
            "endmodule\n")
    assert vtswap.flavour_counts(text, d["tokens"], d["re"]) == {
        "R/A": 1, "R/B": 1, "L/A": 1}
    assert vtswap._fmt_counts({"R/A": 3, "R/B": 1}) == "R/A=3 R/B=1"
    assert vtswap._fmt_counts({"R": 3, "L": 1}) == "R3/L1"


def _have_sta():
    if os.environ.get("OPENSTA"):
        return True
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", vtswap.ORFS_IMAGE],
                       capture_output=True)
    return r.returncode == 0


CHAIN = 12


def _chain_netlist():
    """Flop -> CHAIN RVT inverters -> flop."""
    body = ["module chain (clk, d, q);", "  input clk;", "  input d;",
            "  output q;", "  wire n0;"]
    body += [f"  wire n{i};" for i in range(1, CHAIN + 1)]
    body.append("  MDFF_X1_R f0 (.CK(clk), .D(d), .Q(n0));")
    body += [f"  MINV_X1_R g{i} (.A(n{i - 1}), .Y(n{i}));"
             for i in range(1, CHAIN + 1)]
    body.append(f"  MDFF_X1_R f1 (.CK(clk), .D(n{CHAIN}), .Q(q));")
    body.append("endmodule")
    return "\n".join(body) + "\n"


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_meets_a_tight_clock_and_recovers(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_chain_netlist())
    tokens, fre = ["R", "L", "S"], r"_([RLS])$"

    def run(period, recover=True, margin=0.0):
        out = tmp_path / f"out_{period}_{recover}_{margin}.v"
        res = vtswap.swap(nl, "chain", out, libs, tokens, fre,
                          vtswap.sdc_for(period, "clk", ["d"]),
                          recover=recover, margin=margin)
        cells = re.findall(r"^\s*(M\w+)\s", out.read_text(), re.M)
        return res, cells

    # Generous clock: nothing to fix, nothing to recover (all RVT already).
    res, cells = run(5000)
    assert res["start"]["wns"] >= 0 and not res["up"] and not res["down"]
    assert all(c.endswith("_R") for c in cells)
    # Tight clock: RVT fails; the swap moves cells faster until it passes,
    # then recovery moves back whatever is not needed.
    res0, _ = run(5000)
    slack_rvt = res0["start"]["wns"]
    period = int((5.0 - slack_rvt) * 1000 * 0.85)  # 15% under RVT's path
    res, cells = run(period, recover=False)
    assert res["start"]["wns"] < 0 and res["up"]
    assert res["start"]["hold_wns"] is not None
    assert res["flavours_before"] == {"R": CHAIN + 2, "L": 0, "S": 0}
    assert sum(res["flavours_after"].values()) == CHAIN + 2
    assert res["flavours_after"]["R"] < CHAIN + 2
    assert res["end"]["wns"] >= 0
    up_only = sum(not c.endswith("_R") for c in cells)
    res, cells = run(period, recover=True)
    assert res["end"]["wns"] >= -1e-6
    assert sum(not c.endswith("_R") for c in cells) <= up_only
    # A margin: the swap keeps 20 ps of slack in hand (ns libraries).
    res, mcells = run(period, recover=True, margin=0.02)
    assert res["end"]["wns"] >= 0.02 - 1e-6
    assert sum(not c.endswith("_R") for c in mcells) >= \
        sum(not c.endswith("_R") for c in cells)
    # Only flavours changed: the same instances, the same cell bases.
    base = lambda c: re.sub(r"_[RLS]$", "", c)
    assert sorted(map(base, cells)) == sorted(
        ["MDFF_X1"] * 2 + ["MINV_X1"] * CHAIN)


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_keeps_hold(tmp_path):
    """Moves that would break hold go back and stay: the chain is the
    setup-critical path, and a side flop f2 taps it after g2 with a
    minimum delay just under that RVT delay, so f0, g1 and g2 sit on the
    hold-critical path and the rest of the chain is free to speed up."""
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = [lib / f"mini_{v}_0p75v_tt.lib" for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    text = _chain_netlist().replace(
        "output q;", "output q;\n  output q2;").replace(
        "module chain (clk, d, q);", "module chain (clk, d, q, q2);").replace(
        "endmodule", "  MDFF_X1_R f2 (.CK(clk), .D(n2), .Q(q2));\nendmodule")
    nl.write_text(text)
    tokens, fre = ["R", "L", "S"], r"_([RLS])$"
    base = ("set_false_path -hold -to [get_ports {q q2}]\n"
            "set_false_path -hold -to [get_pins f1/D]\n")

    def run(period, extra, tag):
        out = tmp_path / f"out_{tag}.v"
        sdc = vtswap.sdc_for(period, "clk", ["d"]) + base + extra
        return vtswap.swap(nl, "chain", out, libs, tokens, fre, sdc,
                           recover=False)

    slack0 = run(5000, "", "loose")
    side_hold = slack0["start"]["hold_wns"]                # f0 -> g1 g2 -> f2
    period = int((5.0 - slack0["start"]["wns"]) * 1000 * 0.85)
    md = (f"set_min_delay {side_hold - 0.005:.4f} "
          "-from [get_pins f0/CK] -to [get_pins f2/D]\n")
    res = run(period, md, "hold")
    assert 0 < res["start"]["hold_wns"] < 0.01
    assert res["end"]["hold_wns"] >= 0
    held = {h["inst"] for h in res["held"]}
    assert held and held <= {"f0", "g1", "g2"}
    assert not held & {u["inst"] for u in res["up"]}
    assert res["end"]["wns"] > res["start"]["wns"]
    assert "kept for hold" in vtswap.summary(res)


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_times_hold_at_the_hold_corner(tmp_path):
    """hold_libs: setup from `libs` (-max), hold from the hold corner's
    libraries (-min), in one OpenSTA session."""
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    at = lambda c: [lib / f"mini_{v}_0p75v_{c}.lib"
                    for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    nl.write_text(_chain_netlist())
    sdc = vtswap.sdc_for(5000, "clk", ["d"])
    tokens, fre = ["R", "L", "S"], r"_([RLS])$"
    run = lambda tag, libs, hold=(): vtswap.swap(
        nl, "chain", tmp_path / f"{tag}.v", libs, tokens, fre, sdc,
        recover=False, hold_libs=hold)["start"]
    tt, ff, split = run("tt", at("tt")), run("ff", at("ff")), \
        run("split", at("tt"), at("ff"))
    assert ff["hold_wns"] != pytest.approx(tt["hold_wns"], abs=1e-4)
    assert split["wns"] == pytest.approx(tt["wns"], abs=1e-6)
    assert split["hold_wns"] == pytest.approx(ff["hold_wns"], abs=1e-6)


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_level_shifters_follow_their_corner(tmp_path):
    """hold_both_libs: the shifters at the hold corner go with the hold
    libraries. A shifter on the chain times setup at tt and hold at ff, as
    the same flow with hold_libs alone would not (it times both at tt)."""
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    at = lambda c: [lib / f"mini_{v}_0p75v_{c}.lib"
                    for v in ("rvt", "lvt", "slvt")]
    nl = tmp_path / "chain.v"
    # f0 -> shifter -> f1: the shifter is the hold-critical path's only cell.
    nl.write_text("module chain (clk, d, q);\n  input clk;\n  input d;\n"
                  "  output q;\n  wire n0;\n  wire n1;\n"
                  "  MDFF_X1_R f0 (.CK(clk), .D(d), .Q(n0));\n"
                  "  MLSHL_X1 ls0 (.A(n0), .Y(n1));\n"
                  "  MDFF_X1_R f1 (.CK(clk), .D(n1), .Q(q));\nendmodule\n")
    sdc = (vtswap.sdc_for(5000, "clk", ["d"])
           + "set_false_path -hold -from [get_ports d]\n"
           "set_false_path -hold -to [get_ports q]\n")
    tokens, fre = ["R", "L", "S"], r"_([RLS])$"
    ls = lambda c: [lib / f"mini_ls_{c}.lib"]
    run = lambda tag, libs, hold=(), both=(), hboth=(): vtswap.swap(
        nl, "chain", tmp_path / f"{tag}.v", libs, tokens, fre, sdc,
        recover=False, hold_libs=hold, both_libs=both,
        hold_both_libs=hboth)["start"]
    tt = run("tt", at("tt"), both=ls("tt"))
    ff = run("ff", at("ff"), both=ls("ff"))
    old = run("old", at("tt"), at("ff"), both=ls("tt"))
    new = run("new", at("tt"), at("ff"), both=ls("tt"), hboth=ls("ff"))
    assert new["wns"] == pytest.approx(tt["wns"], abs=1e-6)
    assert new["hold_wns"] == pytest.approx(ff["hold_wns"], abs=1e-6)
    assert old["hold_wns"] != pytest.approx(new["hold_wns"], abs=1e-4)


def test_retype_keeps_every_other_byte():
    text = ("module m (a, y);\n  input a;\n  output y;\n  wire n;\n"
            "  INV_R g0 (.A(a), .Y(n));\n"
            "  INV_R \\u_x/g1  (.A(n), .Y(y));\n"
            "endmodule\nmodule k (b);\n  input b;\n"
            "  INV_R g0 (.A(b), .Y());\nendmodule\n")
    out = vtswap.retype(text, {"m": {"g0": "INV_L", "u_x/g1": "INV_S"}})
    assert out == text.replace("INV_R g0 (.A(a)", "INV_L g0 (.A(a)").replace(
        "INV_R \\u_x/g1", "INV_S \\u_x/g1")
    assert "INV_R g0 (.A(b)" in out          # module k untouched
    with pytest.raises(VtSwapError, match="could not retype"):
        vtswap.retype(text, {"m": {"nope": "INV_L"}})
    # cell_changes reads a rewritten netlist back to instance -> cell.
    # (vnetlist keeps an escaped name's backslash; retype takes either.)
    ch = vtswap.cell_changes(text, out)
    assert ch == {"m": {"g0": "INV_L", "\\u_x/g1": "INV_S"}}
    assert vtswap.retype(text, ch) == out


# ---- a two-axis ladder: Vt x a second axis --------------

def _scale_lib(text, tag, name, dscale, lscale):
    """A copy of a mini library whose cells carry `_<tag>` after the Vt
    letter, delays (cell_rise/fall) times dscale and leakage times lscale:
    the 'B' variant of the two-axis tests (invented numbers)."""
    text = re.sub(r"library\((\w+)\)", f"library({name})", text, 1)
    text = re.sub(r"cell\((\w+_[RLS])\)", rf"cell(\1_{tag})", text)

    def scale_values(m):
        vals = re.sub(r"[-\d.]+", lambda n: f"{float(n.group()) * dscale:.5f}",
                      m.group(2))
        return f"{m.group(1)}{vals}"
    # (the mini libraries' tables take their axes from a template; give the
    # ones the characterisation tool reads their own)
    text = re.sub(r"(cell_(?:rise|fall)\(nldm\) \{ )values",
                  r'\1index_1("0.005, 0.02, 0.05, 0.1, 0.2"); '
                  r'index_2("0.0005, 0.002, 0.005, 0.01, 0.02"); values', text)
    text = re.sub(r"(cell_(?:rise|fall)\(nldm\) \{ index_1\([^)]*\); index_2\([^)]*\); values\()(.*?)(?=\); \})",
                  scale_values, text, flags=re.S)
    return re.sub(r"(cell_leakage_power : )([-\d.eE+]+)",
                  lambda m: f"{m.group(1)}{float(m.group(2)) * lscale:.6f}",
                  text)


def _two_axis_libs(tmp_path, drop=()):
    """Libraries for ladder R/A < R/B < L/A < L/B < S/A < S/B: the mini
    libraries as variant A, and B faster for more leakage. `drop`
    lists cell names left out of the B libraries."""
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = []
    for v, t in (("rvt", "R"), ("lvt", "L"), ("slvt", "S")):
        src = (lib / f"mini_{v}_0p75v_tt.lib").read_text()
        for tag, d, k in (("A", 1.0, 1.0), ("B", 0.9, 1.7)):
            text = _scale_lib(src, tag, f"mini_{v}_{tag}", d, k)
            for c in drop:
                if tag == "B" and c.endswith("_" + t + "_B"):
                    text = re.sub(r"  cell\(" + c + r"\) \{.*?\n  \}\n", "",
                                  text, flags=re.S)
            p = tmp_path / f"two_{v}_{tag}.lib"
            p.write_text(text)
            libs.append(p)
    return libs


TWO_TOKENS = ["R/A", "R/B", "L/A", "L/B", "S/A", "S/B"]
TWO_RE = r"_([RLS])_([AB])$"


def _two_axis_netlist():
    return _chain_netlist().replace("_R ", "_R_A ")


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_two_axis_ladder(tmp_path):
    """The ladder runs R/A < R/B < L/A < ...: a small shortfall is closed
    by the cheap R/B step alone, a big one climbs to L; recovery walks
    back down the same ladder; counts are keyed by the joined token."""
    libs = _two_axis_libs(tmp_path)
    nl = tmp_path / "chain.v"
    nl.write_text(_two_axis_netlist())

    def run(period, recover=True):
        out = tmp_path / f"out_{period}_{recover}.v"
        res = vtswap.swap(nl, "chain", out, libs, TWO_TOKENS, TWO_RE,
                          vtswap.sdc_for(period, "clk", ["d"]),
                          recover=recover)
        return res, re.findall(r"^\s*M\w+_([RLS]_[AB])\s", out.read_text(),
                               re.M)

    res0, cells = run(5000)
    assert not res0["up"] and set(cells) == {"R_A"}
    assert res0["flavours_before"]["R/A"] == CHAIN + 2
    slack = res0["start"]["wns"]
    full = 5.0 - slack                       # ns: the all-R/A path
    # Slightly under: R/B is enough, nothing leakier than it is used.
    res, cells = run(int(full * 1000 * 0.96), recover=False)
    assert res["start"]["wns"] < 0 and res["end"]["wns"] >= 0
    assert set(cells) <= {"R_A", "R_B"} and "R_B" in cells
    assert res["flavours_after"]["L/A"] == 0
    # Far under: needs LVT or faster, and the fix reaches it.
    res, cells = run(int(full * 1000 * 0.70), recover=False)
    assert res["end"]["wns"] >= 0
    assert {c[0] for c in cells} - {"R"}
    # Recovery then gives back what the clock does not need, down the
    # ladder (step by step), never past a failing path.
    res_r, cells_r = run(int(full * 1000 * 0.70), recover=True)
    assert res_r["end"]["wns"] >= -1e-6
    order = {t.replace("/", "_"): i for i, t in enumerate(TWO_TOKENS)}
    assert sum(order[c] for c in cells_r) <= sum(order[c] for c in cells)
    assert sum(res_r["flavours_after"].values()) == CHAIN + 2


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_steps_over_a_missing_flavour(tmp_path):
    """A cell family without one ladder token steps over it: no MINV in
    the R/B flavour, so an inverter goes R/A -> L/A or beyond."""
    libs = _two_axis_libs(tmp_path, drop=["MINV_X1_R_B"])
    nl = tmp_path / "chain.v"
    nl.write_text(_two_axis_netlist())
    out = tmp_path / "out.v"
    res0 = vtswap.swap(nl, "chain", tmp_path / "o0.v", libs, TWO_TOKENS,
                       TWO_RE, vtswap.sdc_for(5000, "clk", ["d"]),
                       recover=False)
    full = 5.0 - res0["start"]["wns"]
    res = vtswap.swap(nl, "chain", out, libs, TWO_TOKENS, TWO_RE,
                      vtswap.sdc_for(int(full * 1000 * 0.85), "clk", ["d"]),
                      recover=False)
    text = out.read_text()
    assert res["end"]["wns"] >= 0
    cells = re.findall(r"^\s*(MINV_X1_\w+)\s", text, re.M)
    assert "MINV_X1_R_B" not in cells and "MINV_X1_R_A" not in cells
    assert all(c.startswith("MINV_X1_L_") for c in cells)
    # The flops have an R/B: they take that step (or go on past it).
    assert res["flavours_after"]["R/A"] == 0


def test_ladder_char_reads_a_two_axis_ladder(tmp_path, capsys):
    """scripts/mvt/ladder_char.py on the invented two-axis libraries: B is
    faster than A for more leakage, at the same area."""
    sys.path.insert(0, str(ROOT / "scripts" / "mvt"))
    import ladder_char
    libs = _two_axis_libs(tmp_path)
    ladder_char.main(["--tokens", ",".join(TWO_TOKENS), "--re", TWO_RE]
                     + [str(l) for l in libs])
    out = capsys.readouterr().out
    rows = {l.split()[0]: l.split() for l in out.splitlines()
            if l.split() and l.split()[0] in TWO_TOKENS}
    assert "families carry all 6 flavours" in out
    # first section (comb): R/B vs R/A
    assert float(rows["R/B"][1]) == pytest.approx(0.9, abs=0.02)
    assert float(rows["R/B"][2]) == pytest.approx(1.7, abs=0.01)
    assert float(rows["R/B"][3]) == pytest.approx(1.0)


def test_vt_swap_false_to_parses_and_makes_sdc():
    from circt_flow import vtswap as v
    s = v.parse_vt_swap({"margin_ps": 10, "false_to": ["outputs", "q[0]"]}, "f")
    assert s.false_to == ("outputs", "q[0]")
    assert v.parse_vt_swap({}, "f").false_to == ()
    t = v.false_to_sdc(s.false_to)
    assert "set_false_path -to [all_outputs]" in t
    assert "set_false_path -to [get_ports q[0]]" in t
    with pytest.raises(v.VtSwapError, match="false_to"):
        v.parse_vt_swap({"false_to": "outputs"}, "f")
