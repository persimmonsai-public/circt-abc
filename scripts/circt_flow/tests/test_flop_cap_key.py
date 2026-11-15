"""vt_swap.flop_cap / pnr.post_layout.flop_cap (the recipe key that replaced
CIRCT_VT_FLOP_CAP) and pnr.post_layout.recover_opts validation."""
import pytest

from circt_flow import postlayout, vtswap


def test_vt_swap_flop_cap_key():
    assert vtswap.parse_vt_swap({"flop_cap": "L"}, "x").flop_cap == "L"
    assert vtswap.parse_vt_swap({}, "x").flop_cap == ""
    with pytest.raises(vtswap.VtSwapError):
        vtswap.parse_vt_swap({"flop_cap": 3}, "x")


def test_flop_cap_token_checked():
    vtswap.check_ladder_tokens("", "", ["S", "L"], "x", "L")
    with pytest.raises(vtswap.VtSwapError, match="flop_cap"):
        vtswap.check_ladder_tokens("", "", ["S", "L"], "x", "Q")


def test_post_layout_flop_cap_and_recover_opts():
    s = postlayout.parse_post_layout(
        {"flop_cap": "L", "recover_opts": "comp=2,seq_pair,pass=6"}, "x")
    assert s.flop_cap == "L" and s.recover_opts == "comp=2,seq_pair,pass=6"
    for bad in ("comp=0", "comp=x", "first", "pass", 3):
        with pytest.raises(vtswap.VtSwapError):
            postlayout.parse_post_layout({"recover_opts": bad}, "x")


def test_swap_tcl_takes_the_resolved_cap(monkeypatch, tmp_path):
    """swap() puts its flop_cap argument into the Tcl; it reads no
    environment variable (the caller resolves the option)."""
    seen = {}

    class Stop(Exception):
        pass

    real = vtswap.TCL

    class T(str):
        def __mod__(self, d):
            seen.update(d)
            raise Stop

    monkeypatch.setattr(vtswap, "TCL", T(real))
    monkeypatch.setenv("CIRCT_VT_FLOP_CAP", "S")
    net = tmp_path / "n.v"
    net.write_text("module t(); endmodule\n")
    for cap, want in (("", ""), ("L", "L")):
        with pytest.raises(Stop):
            vtswap.swap(net, "t", tmp_path / "o.v", [], ["S", "L"], "_(S|L)$",
                        "", flop_cap=cap)
        assert seen["fcap"] == want


def test_flop_cap_option_sources():
    """syn.vt_swap.flop_cap / post.flop_cap: the recipe key; the legacy
    variable over it (transition); an argument over both."""
    from circt_flow import options
    rec = options.from_yaml_sections({"vt_swap": {"flop_cap": "L"},
                                      "pnr": {"post_layout": {"flop_cap": "S"}}})
    s = options.resolve(recipe=rec, env={}, warn=False)
    assert (s["syn.vt_swap.flop_cap"], s["post.flop_cap"]) == ("L", "S")
    s = options.resolve(recipe=rec, env={"CIRCT_VT_FLOP_CAP": "Q"}, warn=False)
    assert (s["syn.vt_swap.flop_cap"], s["post.flop_cap"]) == ("Q", "Q")
    assert s.source("post.flop_cap") == "env"
    s = options.resolve(argument={"post.flop_cap": "Z"}, recipe=rec,
                        env={"CIRCT_VT_FLOP_CAP": "Q"}, warn=False)
    assert s["post.flop_cap"] == "Z" and s["syn.vt_swap.flop_cap"] == "Q"


def test_recover_opts_blk():
    ok = "comp=2,blk=blk_a_[0-9]+@12"
    sp = postlayout.parse_post_layout({"recover_opts": ok, "margin_ps": 30}, "x")
    assert sp.recover_opts == ok and sp.recover_block == "blk_a_[0-9]+@12"
    for bad in ("blk=@6", "blk=abc", "blk=(@6", "blk=a@x", "blk=a@-1", "blk=a b@6",
                "blk=a@1.2.3"):
        with pytest.raises(vtswap.VtSwapError):
            postlayout.parse_post_layout({"recover_opts": bad}, "x")


def test_recover_block_key_and_token_are_one_setting():
    assert postlayout.parse_post_layout({"recover_block": "blk_a|blk_b@12", "margin_ps": 30},
                                        "x").recover_block == "blk_a|blk_b@12"
    assert postlayout.parse_post_layout({}, "x").recover_block == ""
    for bad in ({"recover_block": "a@6", "recover_opts": "blk=b@6"},
                {"recover_opts": "blk=a@6,blk=b@7"}, {"recover_block": 3},
                {"recover_block": "a,b@6"}, {"recover_block": "a@6", "recover": False}):
        with pytest.raises(vtswap.VtSwapError, match="recover_block|blk|recover"):
            postlayout.parse_post_layout(bad, "x")


def test_blk_pass_in_the_tcl_only_when_set():
    """The block pass is a template value: absent (the default script
    unchanged) unless set; the regex is one literal Tcl word, the floor is in
    the libraries' unit (twelve picoseconds are 0.012 of a nanosecond, a float:
    not Tcl integer division)."""
    t = vtswap.TCL
    assert "%(blk_pass)s" in t and "Block tail recovery" not in t
    assert t.count("is_clock_cell $inst") == 3
    assert vtswap.LayoutSpec().blk_tcl(True) == ""
    spec = vtswap.LayoutSpec(recover_block_re=r"blk_a\d+|blk_b[$]", recover_block_floor=0.012)
    assert spec.blk_tcl(False) == ""
    b = spec.blk_tcl(True)
    assert 'set opt_blk_re "blk_a\\\\d+|blk_b\\[\\$\\]"' in b and "set opt_blk_floor 0.012" in b
    assert "is_clock_cell $inst" in b and "try_down" in b
    # a recover_opts blk= token is taken out of the pair-recovery options
    seen = []

    def fake_swap(netlist, top, out, *a, layout=None, **kw):
        seen.append(layout)
        out.write_text(netlist.read_text())
        return {"iterations": 0, "flavours_before": {}, "flavours_after": {}}
    import pathlib
    import tempfile
    real_swap, real_measure = vtswap.swap, postlayout.measure
    vtswap.swap, postlayout.measure = fake_swap, (lambda *a, **k: {"wns": 0.0})
    try:
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            nl = td / "n.v"
            nl.write_text("module m (a);\n  input a;\nendmodule\n")
            (td / "c.sdc").write_text("create_clock -period 1 [get_ports clk]\n")
            postlayout.optimise(nl, "m", td / "o", [], [], ["R", "L"], r"_([RL])$",
                                td / "c.sdc", td / "x.spef",
                                spec=postlayout.parse_post_layout(
                                    {"recover_opts": "comp=2,blk=blk_a@12", "margin_ps": 30}, "x"))
    finally:
        vtswap.swap, postlayout.measure = real_swap, real_measure
    lay = seen[-1]
    assert lay.recover_opts == "comp=2" and lay.recover_block_re == "blk_a"
    assert lay.recover_block_floor == pytest.approx(0.012)
