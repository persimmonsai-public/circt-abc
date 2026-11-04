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
    with pytest.raises(KeyError, match="exactly one group"):
        vtswap.parse_flavours({"tokens": ["R", "L"], "re": "_[RL]$"}, "p")


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
