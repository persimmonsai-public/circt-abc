"""spef_pincap: restore the pin-node cap OpenROAD's estimator SPEF drops.
Invented two-net SPEF in the shape OpenROAD writes (no tech data)."""
import pytest
from circt_flow import spef_pincap as sp

HEAD = """*SPEF "ieee 1481-1999"
*DESIGN "t"
*DESIGN_FLOW "NAME_SCOPE LOCAL" "PIN_CAP NONE"
*DIVIDER /
*DELIMITER :
*BUS_DELIMITER []
*T_UNIT 1 NS
*C_UNIT 1 PF
*R_UNIT 1 KOHM
*L_UNIT 1 HENRY

*PORTS
a I

"""
# net n1: total 0.010, internal caps 0.004 + 0.002 -> 0.004 missing (pin nodes)
N1 = """*D_NET n1 0.01
*CONN
*I u1:Y O *D INVX
*I u2:A I *D INVX
*I u3:A I *D INVX
*CAP
1 n1:1 0.004
2 n1:2 0.002
*RES
1 u1:Y n1:1 0.1
2 n1:1 n1:2 0.2
3 u2:A n1:2 0.1
4 u3:A n1:2 0.1
*END

"""
# net a: port-driven, no *CAP section at all, total 0.003
N2 = """*D_NET a 0.003
*CONN
*P a I
*I u1:A I *D INVX
*RES
1 a u1:A 0.1
*END

"""
# net n3: already consistent (placement-mode shape)
N3 = """*D_NET n3 0.002
*CONN
*I u2:Y O *D INVX
*I u3:B I *D INVX
*CAP
1 n3:0 0.002
*RES
1 u2:Y n3:0 0.1
2 n3:0 u3:B 0.1
*END

"""


def _write(tmp_path, body=N1 + N2 + N3):
    p = tmp_path / "in.spef"
    p.write_text(HEAD + body)
    return p


def _export_lines(spef, debug="", path=None):
    """The lines spef_export.tcl prints for `spef` once it has written it:
    the export segment for its path and the size / CRC-32 record."""
    import zlib
    data = spef.read_bytes()
    path = path or str(spef.resolve())
    return (f"SPEF_EXPORT_BEGIN {path}\n{debug}SPEF_EXPORT_END {path}\n"
            f"SPEF_EXPORT_FILE {len(data)} {zlib.crc32(data):08x} {path}\n")


def test_check_reports_missing(tmp_path):
    r = sp.check(_write(tmp_path))
    assert r["nets"] == 3
    assert abs(r["total"] - 0.015) < 1e-12
    assert abs(r["capsum"] - 0.008) < 1e-12
    assert r["nets_short"] == 2


def test_fix_equal_split_restores_totals(tmp_path):
    src = _write(tmp_path)
    dst = tmp_path / "out.spef"
    st = sp.fix(src, dst)
    assert st["fixed"] == 2 and st["equal_split"] == 2
    r = sp.check(dst)
    assert abs(r["capsum"] - r["total"]) < 1e-8
    txt = dst.read_text()
    # three pins share 0.004 equally (the last takes the rounding remainder);
    # numbering continues after the internal caps
    assert "3 u1:Y 0.00133333\n" in txt and "5 u3:A 0.00133334\n" in txt
    # a net without *CAP gets one before *RES, with the port and the pin
    assert "*CAP\n1 a 0.0015\n2 u1:A 0.0015\n*RES" in txt
    # untouched net and header byte-identical
    assert N3 in txt and txt.startswith(HEAD)


def test_fix_is_idempotent(tmp_path):
    a, b = tmp_path / "a.spef", tmp_path / "b.spef"
    sp.fix(_write(tmp_path), a)
    st = sp.fix(a, b)
    assert st["fixed"] == 0
    assert a.read_text() == b.read_text()


def test_weights_from_est_rc_log(tmp_path):
    rc = tmp_path / "rc.tcl"
    rc.write_text("catch { set_layer_rc -layer L1 -resistance 1 -capacitance 1e-4 } ; # x\n"
                  "catch { set_layer_rc -layer L2 -resistance 1 -capacitance 2e-4 }\n")
    log = [
        "[DEBUG EST-est_rc] net n1",
        "[DEBUG EST-est_rc] n1:1 -> u1/Y (1.00, 2.00) 0.30u layer=2 r=0.1 via_res=0.05 c=0.000",
        "[DEBUG EST-est_rc] pin u1/Y -> to grid 0u layer=2 r=0.1 via_res=0.05 c=0.000",
        "[DEBUG EST-est_rc] n1:2 -> u2/A (1.00, 2.00) 0.10u layer=2 r=0.1 via_res=0.05 c=0.000",
        "[DEBUG EST-est_rc] n1:2 -> u3/A (1.00, 2.00) 0.10u layer=1 r=0.1 via_res=0.05 c=0.000",
    ]
    w = sp.stub_weights(log, sp.layer_caps(rc))
    assert abs(w["u1:Y"] - 0.3 * 2e-4 / 2) < 1e-15
    assert abs(w["u3:A"] - 0.1 * 1e-4 / 2) < 1e-15
    dst = tmp_path / "w.spef"
    sp.fix(_write(tmp_path, N1), dst, w)
    txt = dst.read_text()
    # weights 6e-5 : 2e-5 : 1e-5 of 0.004
    assert "3 u1:Y 0.00266667\n" in txt
    assert "4 u2:A 0.000888889\n" in txt
    assert "5 u3:A 0.000444441\n" in txt
    assert abs(sp.check(dst)["missing"]) < 1e-8  # %.6g rounding


def test_cli(tmp_path, capsys):
    src = _write(tmp_path)
    assert sp.main(["check", str(src)]) == 0
    assert "nets short 2" in capsys.readouterr().out
    assert sp.main(["fix", str(src), str(tmp_path / "o.spef")]) == 0
    assert "fixed 2" in capsys.readouterr().out


def test_postlayout_spef_for_timing(tmp_path, capsys):
    from circt_flow import postlayout
    src = _write(tmp_path)
    # short SPEF, restore off: same file, warning
    assert postlayout.spef_for_timing(src, tmp_path / "o", False) == src
    assert "timing is optimistic" in capsys.readouterr().out
    # restore on: a fixed copy in the out dir
    fixed = postlayout.spef_for_timing(src, tmp_path / "o", True)
    assert fixed == tmp_path / "o" / "in.pincap.spef"
    assert sp.check(fixed)["nets_short"] == 0
    # a consistent SPEF is used as is, silently
    assert postlayout.spef_for_timing(fixed, tmp_path / "o2", True) == fixed
    assert capsys.readouterr().out.count("WARNING") == 0


def test_restore_in_place_keeps_original(tmp_path):
    src = _write(tmp_path)
    orig = src.read_text()
    st = sp.restore(src)
    assert st["fixed"] == 2 and st["ratio_after"] == 1.0 and st["ratio_before"] < 0.6
    assert (tmp_path / "in.spef.orig").read_text() == orig
    r = sp.check(src)
    assert r["nets_short"] == 0 and abs(r["capsum"] - r["total"]) < 1e-8
    # a restored file is left alone, and no new .orig overwrites the first
    st = sp.restore(src)
    assert st["fixed"] == 0
    assert (tmp_path / "in.spef.orig").read_text() == orig


def test_layer_caps_follow_source_and_skip_expressions(tmp_path):
    inner = tmp_path / "inner.tcl"
    inner.write_text("set_layer_rc -layer A -resistance 1 -capacitance 1e-4\n"
                     "set_layer_rc -layer B -resistance 1 -capacitance [expr {2 * $x}]\n")
    wrap = tmp_path / "wrap.tcl"
    wrap.write_text("# wrapper\nsource {%s}\n" % inner)
    assert sp.layer_caps(wrap) == {1: 1e-4}


def test_restore_exports_finds_marked_log(tmp_path, capsys):
    d = tmp_path / "pnr"
    d.mkdir()
    gr = d / "out.spef"
    gr.write_text(HEAD + N1)
    other = d / "other.spef"
    other.write_text(HEAD + N3)
    (d / "export.log").write_text(
        "noise\n[DEBUG EST-est_rc] n1:2 -> u1/Y (1.00, 2.00) 0.99u layer=1 r=0.1 via_res=0.0 c=0.000\n"
        + _export_lines(other, "[DEBUG EST-est_rc] n1:2 -> u3/A (1.00, 2.00) 0.99u "
                        "layer=1 r=0.1 via_res=0.0 c=0.000\n")
        + _export_lines(gr, "[DEBUG EST-est_rc] n1:2 -> u2/A (1.00, 2.00) 0.10u "
                        "layer=1 r=0.1 via_res=0.0 c=0.000\n"))
    pl = d / "place.spef"                      # exact: left alone
    pl.write_text(HEAD + N3)
    res = sp.restore_exports(tmp_path)
    assert list(res) == [str(gr)]
    assert sp.check(gr)["nets_short"] == 0 and sp.check(pl)["nets_short"] == 0
    assert "u2:A 0.004" in gr.read_text()      # only the marked segment counts
    assert "weights from export.log" in capsys.readouterr().out


def test_restore_exports_leaves_a_spef_not_marked_by_the_export(tmp_path, capsys):
    """Only a SPEF the export helper wrote (its SPEF_EXPORT_BEGIN marker in a
    log below the directory) is rewritten; any other is left as written."""
    (tmp_path / "o.spef").write_text(HEAD + N1)
    before = (tmp_path / "o.spef").read_text()
    assert sp.restore_exports(tmp_path) == {}
    assert (tmp_path / "o.spef").read_text() == before
    assert "o.spef: left as written" in capsys.readouterr().out


def _marked(d, name):
    (d / f"{name}.log").write_text(_export_lines(d / name))


def _short(d, rel):
    f = d / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(HEAD + N1)
    return f


def test_restore_exports_needs_the_marker_for_this_path(tmp_path, capsys):
    """The marker names a file by its path: a SPEF of the same name in
    another directory, a marker for `x.spef.gz`, and marker text that is not
    a whole line do not qualify."""
    a, b = _short(tmp_path, "a/o.spef"), _short(tmp_path, "b/o.spef")
    (tmp_path / "a" / "run.log").write_text(_export_lines(a))
    x = _short(tmp_path, "x.spef")
    gz = str(x.resolve()) + ".gz"
    (tmp_path / "x.log").write_text(
        _export_lines(x, path=gz)
        + "# SPEF_EXPORT_BEGIN %s\n" % x.resolve()
        + "".join("  " + l for l in _export_lines(x).splitlines(True)))
    before = x.read_text()
    assert list(sp.restore_exports(tmp_path)) == [str(a)]
    assert sp.check(b)["nets_short"] == 1 and x.read_text() == before
    out = capsys.readouterr().out
    assert "x.spef: left as written (not written by spef_export_global_route" in out


def test_restore_exports_keeps_the_file_when_the_last_rename_fails(tmp_path, capsys,
                                                                 monkeypatch):
    """The restored file takes the SPEF's name in one rename: when that
    fails the SPEF is still there, as written, and the report says so."""
    import os, pathlib
    f = _short(tmp_path, "o.spef")
    (tmp_path / "o.log").write_text(_export_lines(f))
    before = f.read_text()

    def fail_from_tmp(real):
        def rename(src, dst, *a, **k):
            if str(dst).endswith(".spef"):       # the restored file's rename
                raise OSError("injected")
            return real(src, dst, *a, **k)
        return rename
    monkeypatch.setattr(os, "replace", fail_from_tmp(os.replace))
    monkeypatch.setattr(os, "rename", fail_from_tmp(os.rename))
    real_path_replace = pathlib.Path.replace
    monkeypatch.setattr(pathlib.Path, "replace",
                        lambda self, t: fail_from_tmp(real_path_replace)(self, t))
    assert sp.restore_exports(tmp_path) == {}
    assert f.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.log", "o.spef"]
    assert "o.spef: left as written (could not be restored" in capsys.readouterr().out


def test_restore_exports_refuses_a_stale_marker(tmp_path, capsys):
    """A file changed after the export (another size or CRC-32 than the one
    the export recorded) is left as written, though its path is marked."""
    f = _short(tmp_path, "o.spef")
    (tmp_path / "o.log").write_text(_export_lines(f))
    f.write_text(HEAD + N1.replace("0.004", "0.005", 1))     # same size
    before = f.read_text()
    assert sp.restore_exports(tmp_path) == {}
    assert f.read_text() == before and not (tmp_path / "o.spef.orig").exists()
    assert "differs from the one the export recorded" in capsys.readouterr().out


def test_restore_exports_matches_a_path_from_a_container(tmp_path):
    """A marker path that does not exist here (the export ran with the
    directory mounted elsewhere) must end with the file's path below the
    directory; the size and CRC-32 must still match."""
    f = _short(tmp_path, "run/o.spef")
    g = _short(tmp_path, "old/o.spef")
    (tmp_path / "c.log").write_text(
        _export_lines(f, path="/no/such/mount/run/o.spef")
        + _export_lines(g, path="/no/such/mount/o.spef"))
    assert list(sp.restore_exports(tmp_path)) == [str(f)]
    assert sp.check(g)["nets_short"] == 1


def test_export_segments_parse_whole_lines_only(tmp_path):
    log = tmp_path / "l.log"
    log.write_text("SPEF_EXPORT_BEGIN /a b/o.spef\nx\nSPEF_EXPORT_END /a b/o.spef\n"
                   "SPEF_EXPORT_FILE 12 0000abcd /a b/o.spef\n"
                   "SPEF_EXPORT_BEGIN /p.spef\nSPEF_EXPORT_END /p.spef.gz\n")
    assert sp.export_segments(log) == [("/a b/o.spef", ["x\n"], (12, "0000abcd"))]


def test_restore_exports_with_the_marker_and_no_weights_splits_equally(tmp_path, capsys):
    (tmp_path / "o.spef").write_text(HEAD + N1)
    _marked(tmp_path, "o.spef")
    res = sp.restore_exports(tmp_path)
    assert res and "equal split" in capsys.readouterr().out
    assert sp.check(tmp_path / "o.spef")["nets_short"] == 0


TRIPLET = N1.replace("1 n1:1 0.004", "1 n1:1 0.003:0.004:0.005")


@pytest.mark.parametrize("name,content", [
    ("t.spef", (HEAD + TRIPLET).encode()),                 # min:typ:max values
    ("b.spef", (HEAD + N1).encode().replace(b"INVX", b"INV\xff")),   # not UTF-8
    ("lumped.spef", (HEAD + N2).encode())])                # no *CAP section anywhere
def test_restore_exports_reports_and_leaves_what_it_cannot_use(tmp_path, capsys,
                                                               name, content):
    f = tmp_path / name
    f.write_bytes(content)
    _marked(tmp_path, name)
    assert sp.restore_exports(tmp_path) == {}
    assert f.read_bytes() == content
    assert not list(tmp_path.glob("*.orig")) and not list(tmp_path.glob("*.tmp"))
    assert f"{name}: left as written" in capsys.readouterr().out


def test_restore_exports_skips_a_directory_named_spef(tmp_path):
    (tmp_path / "d.spef").mkdir()
    assert sp.restore_exports(tmp_path) == {}


def test_cli_restore(tmp_path, capsys):
    src = _write(tmp_path)
    assert sp.main(["restore", str(src)]) == 0
    assert "fixed 2" in capsys.readouterr().out


def test_export_tcl_marks_debug_and_opts_out(tmp_path):
    """spef_export.tcl with stubbed OpenROAD commands: global route turns on
    the est_rc debug level around the estimate and brackets it with markers;
    SPEF_RESTORE_PINCAP=0 does neither; placement never does."""
    import re, subprocess
    from circt_flow import pnr_recipe
    text = pnr_recipe.SPEF_TCL.read_text()
    # names no cell or platform (the test SPEF's invented INVX among them)
    assert not re.search(r"INVX|ASAP7|sky130|nangate", text, re.I)
    if subprocess.run(["which", "tclsh"], capture_output=True).returncode:
        pytest.skip("no tclsh")
    stub = r'''
set ::calls {}
proc set_debug_level {a b l} { puts "debug $a $b $l" }
proc estimate_parasitics {args} {
  puts "estimate $args"
  if {[string match /x/* [lindex $args end]]} { return }
  set f [open [lindex $args end] w]; puts -nonewline $f abc; close $f
}
'''
    def run(body, env=""):
        f = tmp_path / "t.tcl"
        f.write_text(stub + env + f"source {pnr_recipe.SPEF_TCL}\n" + body)
        r = subprocess.run(["tclsh", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip().split("\n")
    assert run("spef_export_global_route /x/o.spef") == [
        "SPEF_EXPORT_BEGIN /x/o.spef", "debug EST est_rc 1",
        "estimate -global_routing -spef_file /x/o.spef",
        "debug EST est_rc 0", "SPEF_EXPORT_END /x/o.spef"]
    # a file it wrote: its normalized path, then size and CRC-32 ("abc")
    o = tmp_path / "d" / "w.spef"
    o.parent.mkdir()
    rel = "%s/../d/./w.spef" % o.parent
    assert run(f"spef_export_global_route {rel}")[-2:] == [
        f"SPEF_EXPORT_END {o}", f"SPEF_EXPORT_FILE 3 352441c2 {o}"]
    assert run("spef_export_global_route /x/o.spef",
               "set ::env(SPEF_RESTORE_PINCAP) 0\n") == \
        ["estimate -global_routing -spef_file /x/o.spef"]
    assert run("spef_export_placement /x/p.spef") == \
        ["estimate -placement -spef_file /x/p.spef"]


def test_postlayout_restore_default_on():
    from circt_flow import postlayout
    assert postlayout.PostLayoutSpec().spef_restore_pincap is True
    assert postlayout.parse_post_layout(True, "f").spef_restore_pincap is True


def _stage_cfg(tmp_path, spef_restore, script):
    from types import SimpleNamespace
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    pnr = {"clock_port": "c"}
    if spef_restore is not None:
        pnr["spef_restore_pincap"] = spef_restore
    return SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(
            pnr=pnr, pack_pnr={"command": ["bash", "-c", script, "x"]},
            tie=None, top_module="top", clock_period_ps=1000,
            pack_root=tmp_path, manifest=tmp_path / "manifest.yaml"))


@pytest.mark.parametrize("flag,short", [(None, 0), (True, 0), (False, 2)])
def test_pack_pnr_restores_global_route_export(tmp_path, flag, short):
    """The stage restores an export the helper wrote (its marker in the
    command's log; default on) and leaves it as written with
    spef_restore_pincap: false."""
    from circt_flow import stages
    spef = tmp_path / "gr.spef"
    spef.write_text(HEAD + N1 + N2 + N3)
    marker = _export_lines(spef, path="$8/placed.spef").replace("\n", "\\n")
    script = ('cp %s "$8/placed.spef"; printf "%s" > "$8/pnr.export.log"; '
              'echo {} > "$8/pnr_metrics.json"' % (spef, marker))
    stages.pack_pnr(_stage_cfg(tmp_path, flag, script))
    got = sp.check(tmp_path / "pnr" / "placed.spef")
    assert got["nets_short"] == short
    assert (tmp_path / "pnr" / "placed.spef.orig").exists() == (not short)


def test_postlayout_spef_for_timing_uses_log(tmp_path):
    from circt_flow import postlayout
    src = _write(tmp_path, N1)
    log = tmp_path / "x.log"
    log.write_text("SPEF_EXPORT_BEGIN in.spef\n"
                   "[DEBUG EST-est_rc] n1:2 -> u2/A (1.00, 2.00) 0.10u layer=1 r=0.1 via_res=0.0 c=0.000\n"
                   "SPEF_EXPORT_END in.spef\n")
    fixed = postlayout.spef_for_timing(src, tmp_path / "o", True, log)
    assert "u2:A 0.004" in fixed.read_text()


# An extractor's SPEF with coupling capacitors (4-field *CAP lines): the
# *D_NET total counts ground and coupling cap, so nothing is missing.
COUPLED = """*D_NET c1 0.010
*CONN
*I u1:Y O *D INVX
*I u2:A I *D INVX
*CAP
1 c1:1 0.004
2 c1:1 c2:1 0.006
*RES
1 u1:Y c1:1 0.1
2 c1:1 u2:A 0.1
*END

*D_NET c2 0.009
*CONN
*I u3:Y O *D INVX
*I u4:A I *D INVX
*CAP
1 c2:1 0.003
2 c2:1 c1:1 0.006
*RES
1 u3:Y c2:1 0.1
2 c2:1 u4:A 0.1
*END
"""


def test_coupling_caps_count_and_restore_leaves_the_file_alone(tmp_path):
    f = tmp_path / "rcx.spef"
    f.write_text(HEAD + COUPLED)
    r = sp.check(f)
    assert r["nets_short"] == 0 and abs(r["missing"]) < 1e-12
    assert r["nets_coupled"] == 2
    before = f.read_text()
    st = sp.restore(f)
    assert st["fixed"] == 0 and f.read_text() == before
    assert not (tmp_path / "rcx.spef.orig").exists()



def test_a_coupled_spef_short_elsewhere_is_left_as_written_and_said_so(tmp_path, capsys):
    """One net with a coupling cap, another short of its total: the file is
    left as written by restore_exports and by the timing copy, and the
    printed line gives the measured ratio and the reason."""
    from circt_flow import postlayout
    d = tmp_path / "x"
    d.mkdir()
    f = d / "mix.spef"
    f.write_text(HEAD + COUPLED.split("\n\n")[0] + "\n\n" + N1)
    r = sp.check(f)
    assert r["nets_coupled"] == 1 and r["nets_short"] == 1
    before = f.read_text()
    _marked(d, "mix.spef")
    assert sp.restore_exports(d) == {}
    out = capsys.readouterr().out
    assert f.read_text() == before
    assert "mix.spef: left as written (coupling capacitors on 1 net(s)" in out
    assert f"{r['capsum'] / r['total']:.4f}" in out and "1.0000 ->" not in out
    assert postlayout.spef_for_timing(f, tmp_path / "o", True) == f
    assert "coupling capacitors" in capsys.readouterr().out
    assert not (tmp_path / "o").exists()
