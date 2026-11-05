"""Per-rail fanout buffer and per-rail vt_flavours (profile.py, fanout.py,
vtswap.py)."""
import re
import subprocess
import sys

import pytest

from circt_flow import fanout, sta, vtswap
from circt_flow import profile as profile_mod
from circt_flow.config import ROOT

from conftest import CELLS
from test_fanout import CELLS as FCELLS
from test_vtswap import MINI, _have_sta

PACK_EDITS = [
    ("flow:\n  abc_read_lib_args: \"-G 2\"\n  partitions: 1\n",
     "flow:\n  abc_read_lib_args: \"-G 2\"\n  partitions: 1\n"
     "  fanout_buffer: {cell: BUF_HI, input: A, output: Y, max_fanout: 16}\n"
     "vt_flavours: {tokens: [R, L, S], re: '_([RLS])$'}\n"),
    ("    flow: {abc_read_lib_args: \"-l\", abc_script_file: abc/lo.abc}",
     "    flow: {abc_read_lib_args: \"-l\", abc_script_file: abc/lo.abc,\n"
     "           fanout_buffer: {cell: BUF_LO, max_fanout: 6}}\n"
     "    vt_flavours: {tokens: [R, L], re: '_([RLS])$'}"),
]


@pytest.fixture
def buf_pack(rails_pack):
    t = rails_pack.read_text()
    for a, b in PACK_EDITS:
        assert a in t
        t = t.replace(a, b)
    rails_pack.write_text(t)
    return rails_pack


def test_rail_overrides_the_pack(buf_pack):
    p = profile_mod.load(buf_pack)
    assert p.rails["HI"].flow["fanout_buffer"]["cell"] == "BUF_HI"
    # LO's keys override the pack's one by one; the rest is inherited.
    assert p.rails["LO"].flow["fanout_buffer"] == {
        "cell": "BUF_LO", "input": "A", "output": "Y", "max_fanout": 6}
    specs = fanout.rail_specs(p, None, "t")
    assert specs["HI"] == fanout.FanoutSpec("BUF_HI", "A", "Y", 16)
    assert specs["LO"] == fanout.FanoutSpec("BUF_LO", "A", "Y", 6)
    # A flow config's override applies on top of every rail's.
    specs = fanout.rail_specs(p, {"max_fanout": 4}, "t")
    assert [s.max_fanout for s in specs.values()] == [4, 4]
    assert [s.cell for s in specs.values()] == ["BUF_HI", "BUF_LO"]
    assert set(fanout.rail_specs(p, False, "t").values()) == {None}
    # The default rail's view (what a flat run reads) is its own.
    assert p.flow["fanout_buffer"]["cell"] == "BUF_HI"
    assert p.for_rail("LO").flow["fanout_buffer"]["cell"] == "BUF_LO"


def test_rail_without_buffer_and_disabled(rails_pack):
    t = rails_pack.read_text().replace(
        "    flow: {abc_read_lib_args: \"-l\"",
        "    flow: {fanout_buffer: {cell: BUF_LO}, abc_read_lib_args: \"-l\"")
    rails_pack.write_text(t)
    specs = fanout.rail_specs(profile_mod.load(rails_pack), None, "t")
    assert specs["HI"] is None and specs["LO"].cell == "BUF_LO"
    # A rail turns the pack's buffer off with enabled: false.
    rails_pack.write_text(t.replace(
        "flow:\n  abc_read_lib_args", "flow:\n  fanout_buffer: {cell: B}\n"
        "  abc_read_lib_args", 1).replace(
        "fanout_buffer: {cell: BUF_LO}", "fanout_buffer: {enabled: false}"))
    specs = fanout.rail_specs(profile_mod.load(rails_pack), None, "t")
    assert specs["HI"].cell == "B" and specs["LO"] is None


def test_rail_buffer_validation(buf_pack):
    t = buf_pack.read_text()
    buf_pack.write_text(t.replace("max_fanout: 6", "max_fanout: 1"))
    with pytest.raises(KeyError, match=">= 2"):
        profile_mod.load(buf_pack)
    buf_pack.write_text(t.replace("max_fanout: 6", "fanout: 6"))
    with pytest.raises(KeyError, match="unknown key"):
        profile_mod.load(buf_pack)
    # No cell anywhere in the merged rail spec.
    buf_pack.write_text(t.replace("cell: BUF_LO, ", ""))
    assert profile_mod.load(buf_pack).rails["LO"].flow[
        "fanout_buffer"]["cell"] == "BUF_HI"   # inherited from the pack
    buf_pack.write_text(t.replace("  fanout_buffer: {cell: BUF_HI, input: A, "
                                  "output: Y, max_fanout: 16}\n", "")
                        .replace("cell: BUF_LO, ", ""))
    with pytest.raises(KeyError, match="needs a cell"):
        profile_mod.load(buf_pack)


def test_rail_flavours(buf_pack):
    p = profile_mod.load(buf_pack)
    assert p.flavours_at("HI")["tokens"] == ["R", "L", "S"]   # the pack's
    assert p.flavours_at("LO")["tokens"] == ["R", "L"]        # its own
    assert p.flavours_at()["tokens"] == ["R", "L", "S"]       # default rail
    assert p.for_rail("LO").flavours_at()["tokens"] == ["R", "L"]
    t = buf_pack.read_text()
    buf_pack.write_text(t.replace("tokens: [R, L]", "tokens: [R]"))
    with pytest.raises(KeyError, match="two or more"):
        profile_mod.load(buf_pack)
    buf_pack.write_text(t.replace("tokens: [R, L]", "tkns: [R, L]"))
    with pytest.raises(KeyError, match="takes tokens and re"):
        profile_mod.load(buf_pack)


def test_rail_flavours_without_pack_level(rails_pack):
    t = rails_pack.read_text().replace(
        "    voltage: 0.5\n",
        "    voltage: 0.5\n    vt_flavours: {tokens: [A, B]}\n")
    rails_pack.write_text(t)
    p = profile_mod.load(rails_pack)
    assert p.flavours_at("HI") is None
    assert p.flavours_at("LO")["tokens"] == ["A", "B"]


def test_pack_level_enabled_false():
    assert fanout.parse_spec({"cell": "B", "enabled": False}, None, "w") is None
    s = fanout.parse_spec({"cell": "B", "enabled": False},
                          {"enabled": True, "max_fanout": 4}, "w")
    assert s.cell == "B" and s.max_fanout == 4


def _two_rail_text():
    dom = ["module dom (clk, x, o);", "  input clk;", "  input x;",
           "  output o;", "  wire h;",
           "  DFF s (.CK(clk), .D(x), .Q(h));"]
    dom += [f"  INV i{i} (.A(h), .Y());" for i in range(20)]
    dom += ["  assign o = h;", "endmodule"]
    top = ["module top (clk, a);", "  input clk;", "  input a;",
           "  wire t;", "  DFF s (.CK(clk), .D(a), .Q(t));"]
    top += [f"  INV p{i} (.A(t), .Y());" for i in range(20)]
    top += ["  dom u (.clk(clk), .x(t), .o());", "endmodule"]
    return "\n".join(dom + top) + "\n"


def test_each_rail_buffers_with_its_own_cell():
    text = _two_rail_text()
    cells = dict(FCELLS)
    cells["BUF_LO"] = cells["BUF"]
    specs = {"HV": fanout.FanoutSpec("BUF", max_fanout=8),
             "LV": fanout.FanoutSpec("BUF_LO", max_fanout=5)}
    out, per = fanout.buffer_modules(text, "top", {"dom": "LV", "top": "HV"},
                                     {"HV": cells, "LV": cells}, specs)
    assert set(per) == {"dom", "top"}
    assert per["dom"]["cell"] == "BUF_LO" and per["top"]["cell"] == "BUF"
    assert per["dom"]["buffers"] == 4 and per["top"]["buffers"] == 3
    dom, top = out[:out.index("module top")], out[out.index("module top"):]
    assert "BUF_LO fob_b" in dom and not re.search(r"\bBUF fob_b", dom)
    assert re.search(r"\bBUF fob_b", top) and "BUF_LO" not in top
    # A rail with no spec is left alone.
    out, per = fanout.buffer_modules(text, "top", {"dom": "LV", "top": "HV"},
                                     {"HV": cells, "LV": cells},
                                     {"HV": specs["HV"], "LV": None})
    assert set(per) == {"top"}


def test_techpack_check_reports_rail_buffer_and_flavours(tmp_path, capsys):
    from circt_flow import techpack
    lib = tmp_path / "lib"
    lib.mkdir()
    from conftest import write_lib
    write_lib(lib / "a.lib", "a", ["X_X1_R", "X_X1_L", "X_X1_S", "Y_X1_R",
                                   "Y_X1_L", "BUF_X1_R", "TSTICG_X1",
                                   "TSTTIEHI", "TSTTIELO"])
    pack = tmp_path / "p.yaml"
    pack.write_text("""
schema: 2
name: t
default_rail: A
icg: {cell: TSTICG_X1, clk: CK, en: E, test_en: SE, out: ECK}
tie: {hi: "TSTTIEHI:H", lo: "TSTTIELO:L"}
vt_flavours: {tokens: [R, L, S], re: '_([RLS])$'}
rails:
  A:
    voltage: 0.8
    liberties: [lib/a.lib]
    flow: {fanout_buffer: {cell: BUF_X1_R}}
  B:
    voltage: 0.6
    liberties: [lib/a.lib]
    flow: {fanout_buffer: {cell: NOSUCH_X1_R}}
    vt_flavours: {tokens: [R, L], re: '_([RLS])$'}
""")
    problems = techpack.check(str(pack))
    out = capsys.readouterr().out
    assert "fanout_buffer: BUF_X1_R" in out
    assert "vt_flavours: R < L < S" in out and "vt_flavours: R < L (rail's own)" in out
    a, b = out.split("rail B")
    assert re.search(r"flavour S missing in 2 of 3 cell families", a)
    assert "flavour L missing in 1 of 3" in a and "flavour S" not in b
    assert problems == ["rails.B: fanout_buffer cell NOSUCH_X1_R is not in "
                        "the rail's liberties"]


@pytest.mark.skipif(not _have_sta(), reason="no OpenSTA (docker image or "
                    "$OPENSTA)")
def test_swap_uses_each_rails_flavour_list(tmp_path):
    """Two chains in one module, on rails A and B (cells named
    <rail>__<cell>, as the chip view does): A may use R, L, S; B only R, L.
    A clock too tight for RVT makes both swap; B never reaches S."""
    lib = tmp_path / "lib"
    subprocess.run([sys.executable, "-I", str(MINI / "lib/make_libs.py"),
                    str(lib)], check=True, capture_output=True)
    libs = []
    for rail in ("A", "B"):
        for v in ("rvt", "lvt", "slvt"):
            t, _ = sta.rename_liberty(
                (lib / f"mini_{v}_0p75v_tt.lib").read_text(), rail)
            f = tmp_path / f"{rail}_{v}.lib"
            f.write_text(t)
            libs.append(f)
    n = 12
    body = ["module two (clk, d, qa, qb);", "  input clk;", "  input d;",
            "  output qa;", "  output qb;"]
    for r, q in (("A", "qa"), ("B", "qb")):
        body += [f"  wire {r}n{i};" for i in range(n + 1)]
        body.append(f"  {r}__MDFF_X1_R {r}f0 (.CK(clk), .D(d), .Q({r}n0));")
        body += [f"  {r}__MINV_X1_R {r}g{i} (.A({r}n{i - 1}), .Y({r}n{i}));"
                 for i in range(1, n + 1)]
        body.append(f"  {r}__MDFF_X1_R {r}f1 (.CK(clk), .D({r}n{n}), "
                    f".Q({q}));")
    body.append("endmodule")
    nl = tmp_path / "two.v"
    nl.write_text("\n".join(body) + "\n")
    fl = {"A": {"tokens": ["R", "L", "S"], "re": r"_([RLS])$"},
          "B": {"tokens": ["R", "L"], "re": r"_([RLS])$"}}
    def run(period):
        return vtswap.swap(nl, "two", tmp_path / "out.v", libs,
                           ["R", "L", "S"], r"_([RLS])$",
                           vtswap.sdc_for(period, "clk", ["d"]),
                           rail_flavours=fl)
    slack = run(5000)["start"]["wns"]
    res = run(int((5.0 - slack) * 1000 * 0.80))   # 20% under RVT's path
    text = (tmp_path / "out.v").read_text()
    cells = re.findall(r"^\s*([AB])__M\w+_([RLS])\s", text, re.M)
    a = {f for r, f in cells if r == "A"}
    b = {f for r, f in cells if r == "B"}
    assert a - {"R"}, f"rail A did not swap: {res['flavours_after']}"
    assert "S" not in b
    assert "B.S" not in res["flavours_after"]
    assert res["flavours_after"]["B.L"] > 0
    assert sum(v for k, v in res["flavours_after"].items()
               if k.startswith("A.")) == n + 2
