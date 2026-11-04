"""Tech-pack schema 2 corners (profile, check, audit) and the
corner-aware priced mapping library (scripts/mvt/make_abc_lib.py)."""
import importlib.util
import re

import pytest
import yaml

from circt_flow import profile as profile_mod
from circt_flow import techpack

from conftest import REPO, write_lib, CELLS


@pytest.fixture
def corners_pack(tmp_path):
    """Rails HI (default) and LO, corners tt/ss/ff (setup ss, leakage ff);
    HI's mapping list is explicit, LO's defaults to its ss liberties."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for rail in ("hi", "lo"):
        for c in ("tt", "ss", "ff"):
            write_lib(lib / f"{rail}_{c}.lib", f"tst_{rail}_{c}", CELLS)
    write_lib(lib / "hi_map.lib", "tst_hi_map", CELLS)
    pack = tmp_path / "techpack.yaml"
    pack.write_text("""
schema: 2
name: tstcorners
time_unit: ns
icg: {cell: TSTICG_X1, clk: CK, en: E, test_en: SE, out: ECK}
tie: {hi: "TSTTIEHI:H", lo: "TSTTIELO:L"}
corners:
  names: [tt, ss, ff]
  setup: ss
  leakage: ff
default_rail: HI
rails:
  HI:
    voltage: 0.8
    liberties: [lib/hi_map.lib, lib/hi_ss.lib]
    corners:
      tt: [lib/hi_tt.lib]
      ss: [lib/hi_ss.lib]
      ff: [lib/hi_ff.lib]
  LO:
    voltage: 0.5
    corners:
      tt: [lib/lo_tt.lib]
      ss: [lib/lo_ss.lib]
      ff: [lib/lo_ff.lib]
""")
    return pack


def test_corners_load(corners_pack):
    p = profile_mod.load(corners_pack)
    lib = corners_pack.parent / "lib"
    assert p.corner_names == ["tt", "ss", "ff"]
    assert p.setup_corner == "ss" and p.leakage_corner == "ff"
    # The mapping list: explicit for HI, the setup corner's for LO.
    assert p.liberties == [lib / "hi_map.lib", lib / "hi_ss.lib"]
    lo = p.for_rail("LO")
    assert lo.liberties == [lib / "lo_ss.lib"]
    assert lo.corner_names == p.corner_names
    # libs_at: the setup corner by default, any corner by name.
    assert p.libs_at() == [lib / "hi_ss.lib"]
    assert p.libs_at("ff") == [lib / "hi_ff.lib"]
    assert p.libs_at("tt", rail="LO") == [lib / "lo_tt.lib"]
    assert lo.libs_at() == [lib / "lo_ss.lib"]
    with pytest.raises(KeyError, match="no corner 'xx'"):
        p.libs_at("xx")
    every = {x.name for x in p.all_liberties()}
    assert every == {f"{r}_{c}.lib" for r in ("hi", "lo")
                     for c in ("tt", "ss", "ff")} | {"hi_map.lib"}


def test_hold_corner(corners_pack):
    """corners.hold is optional: the setup corner without it."""
    assert profile_mod.load(corners_pack).hold_corner == "ss"
    d = yaml.safe_load(corners_pack.read_text())
    d["corners"]["hold"] = "ff"
    corners_pack.write_text(yaml.safe_dump(d))
    p = profile_mod.load(corners_pack)
    assert p.hold_corner == "ff" and p.setup_corner == "ss"
    assert p.for_rail("LO").hold_corner == "ff"


def test_no_corners_is_one_implicit_corner(rails_pack):
    """A phase-1 schema-2 pack: no corners, libs_at is the mapping list."""
    p = profile_mod.load(rails_pack)
    assert p.corner_names == [] and p.setup_corner is None
    assert p.libs_at() == p.liberties
    assert p.for_rail("LO").libs_at("anything") == p.for_rail("LO").liberties
    assert all(r.corners == {} for r in p.rails.values())


@pytest.mark.parametrize("edit,err", [
    (lambda d: d["corners"].pop("leakage"), "corners.leakage is required"),
    (lambda d: d["corners"].update(setup="xx"), "'xx' is not one of"),
    (lambda d: d["corners"].update(names=["tt", "tt"]), "a corner twice"),
    (lambda d: d["corners"].update(names=[]), "non-empty list"),
    (lambda d: d["corners"].update(hold="xx"), "hold: .xx. is not one of"),
    (lambda d: d["corners"].update(drift="ff"), "corners.drift is not"),
    (lambda d: d["rails"]["LO"]["corners"].pop("ff"),
     "missing corner\\(s\\) ff"),
    (lambda d: d["rails"]["LO"]["corners"].update(xx=["lib/lo_tt.lib"]),
     "unknown corner\\(s\\) xx"),
    (lambda d: d["rails"]["LO"].pop("corners"),
     "give the rail's liberties per corner"),
    (lambda d: d["rails"]["LO"]["corners"].update(ss="lib/lo_ss.lib"),
     "corners.ss must be a non-empty list"),
    (lambda d: d.pop("corners"), "needs the pack-level `corners:`"),
])
def test_corners_errors(corners_pack, edit, err):
    d = yaml.safe_load(corners_pack.read_text())
    edit(d)
    corners_pack.write_text(yaml.safe_dump(d))
    with pytest.raises((KeyError, ValueError), match=err):
        profile_mod.load(corners_pack)


def test_schema1_rejects_corners(tmp_path):
    pack = tmp_path / "techpack.yaml"
    pack.write_text("schema: 1\nname: one\nliberties: [a.lib]\n"
                    "icg: {cell: TSTICG_X1}\ncorners: {}\n")
    with pytest.raises(KeyError, match="corners needs `schema: 2`"):
        profile_mod.load(pack)


def test_check_corners_ok(corners_pack, capsys):
    assert techpack.check(str(corners_pack)) == []
    out = capsys.readouterr().out
    assert "corners: tt, ss (setup), ff (leakage)" in out
    assert "    corner ff: 1 liberties, 4 cells" in out


def test_check_corners_cell_mismatch(corners_pack):
    lib = corners_pack.parent / "lib"
    write_lib(lib / "lo_ff.lib", "tst_lo_ff", CELLS[:-1])
    write_lib(lib / "hi_tt.lib", "tst_hi_tt", CELLS + ["TSTEXTRA_X1"])
    (lib / "lo_tt.lib").unlink()
    probs = techpack.check(str(corners_pack))
    assert ("rails.HI.corners.tt: cell names differ from the setup corner "
            "ss's (1 cells, e.g. TSTEXTRA_X1)") in probs
    assert any(p.startswith("rails.LO.corners.ff: cell names differ")
               for p in probs)
    assert any(p.startswith("rails.LO.corners.ff: 1 cells of the rail's "
                            "mapping liberties are missing") for p in probs)
    assert any(p.startswith("rails.LO.corners.tt: liberty") and
               p.endswith("does not exist") for p in probs)


def test_denylist_covers_corners(corners_pack):
    names, _ = techpack.denylist(corners_pack)
    assert {"tst_hi_tt", "tst_hi_ff", "tst_lo_ff", "tst_hi_map"} <= names


def test_mvt_mini_corners_pack():
    p = profile_mod.load(REPO / "examples/mvt-mini/techpack_corners.yaml")
    assert p.schema == 2 and (p.setup_corner, p.leakage_corner) == ("ss",
                                                                     "ff")
    for n, r in p.rails.items():
        assert r.liberties[0].name.endswith("_ss_ffleak_abc.lib")
        assert set(r.corners) == {"tt", "ss", "ff"}
        for c, libs in r.corners.items():
            assert all(x.name.endswith(f"_{c}.lib") for x in libs)
        # The real libraries after the priced one are the setup corner's.
        assert r.liberties[1:] == r.corners["ss"]


# ---- make_abc_lib.py ------------------------------------------------------


def _make_abc_lib():
    spec = importlib.util.spec_from_file_location(
        "make_abc_lib", REPO / "scripts/mvt/make_abc_lib.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def priced_lib(path, name, cells):
    """{cell: (area, leakage, delay)}: a slim_liberty-shaped library."""
    path.write_text(f"library({name}) {{\n  time_unit : \"1ns\";\n" + "".join(
        f"  cell({c}) {{\n    area : {a};\n    cell_leakage_power : {lk};\n"
        f"    pin(Y) {{\n      timing() {{\n        cell_rise(scalar) "
        f"{{ values(\"{d}\"); }}\n      }}\n    }}\n  }}\n"
        for c, (a, lk, d) in cells.items()) + "}\n")
    return path


def _cells(text):
    return {m.group(1): (float(m.group(2)), float(m.group(3)),
                         float(m.group(4)))
            for m in re.finditer(
                r"cell\((\w+)\) \{\s*area : ([\d.e+-]+) ;\s*"
                r"cell_leakage_power : ([\d.e+-]+) ;?.*?values\(\"([\d.]+)\"",
                text, re.S)}


def test_make_abc_lib_default_unchanged(tmp_path):
    """Without --leakage-lib: area := S * (area + W * own leakage)."""
    m = _make_abc_lib()
    a = priced_lib(tmp_path / "a.lib", "a", {"C_R": (1.0, 10.0, 0.5)})
    b = priced_lib(tmp_path / "b.lib", "b", {"C_S": (1.0, 50.0, 0.3)})
    out = tmp_path / "o.lib"
    m.main([str(out), str(a), str(b), "-W", "0.01"])
    text = out.read_text()
    assert text.startswith("library(a_mvt_priced)")
    assert re.search(r"cell\(C_R\) \{\s*area : 110 ;", text)
    assert re.search(r"cell\(C_S\) \{\s*area : 150 ;", text)
    assert "cell_leakage_power : 10.0;" in text  # leakage lines untouched


def test_make_abc_lib_setup_timing_leakage_prices(tmp_path):
    """--leakage-lib: timing (and everything else) from the inputs, the
    setup corner's; the price and leakage from the leakage corner's."""
    m = _make_abc_lib()
    ss = priced_lib(tmp_path / "ss.lib", "x_ss",
                    {"C_R": (1.0, 4.0, 0.6), "C_S": (1.0, 20.0, 0.36)})
    ff = priced_lib(tmp_path / "ff.lib", "x_ff",
                    {"C_R": (1.0, 30.0, 0.4), "C_S": (1.0, 150.0, 0.24)})
    out = tmp_path / "o.lib"
    m.main([str(out), str(ss), "-W", "0.01", "--leakage-lib", str(ff)])
    text = out.read_text()
    assert text.startswith("/* make_abc_lib.py: timing from ss.lib; "
                           "leakage priced from ff.lib;")
    assert "library(x_ss_mvt_priced)" in text
    cells = _cells(text)
    # area 100 * (1 + 0.01 * ff leakage); delay is ss's.
    assert cells == {"C_R": (130.0, 30.0, 0.6), "C_S": (250.0, 150.0, 0.36)}


def test_make_abc_lib_leakage_missing_cell(tmp_path):
    m = _make_abc_lib()
    ss = priced_lib(tmp_path / "ss.lib", "x", {"C_R": (1.0, 4.0, 0.6)})
    ff = priced_lib(tmp_path / "ff.lib", "y", {"C_X": (1.0, 4.0, 0.6)})
    with pytest.raises(SystemExit, match="C_R .* no cell_leakage_power"):
        m.main([str(tmp_path / "o.lib"), str(ss), "-L", str(ff)])


def test_audit_names_files_relative_to_the_scanned_dir(tmp_path):
    """Where a scanned tree sits is not part of it: a file name is matched
    relative to the directory given, so a tree exported under a path that
    happens to carry a denied term is not flagged for it."""
    tree = tmp_path / "secretterm_dir" / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a.txt").write_text("nothing here\n")
    (tree / "secretterm.txt").write_text("x\n")
    names = {t for f, i, t in techpack._files([str(tree)]) if i == 0}
    assert names == {"sub/a.txt", "secretterm.txt"}
