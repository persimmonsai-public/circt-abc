"""Mapping-library provenance: make_abc_lib.py's header line and
circt_flow.provenance / `techpack check` / the flow's profile load."""
import json
import subprocess
import sys

import pytest
import yaml

from circt_flow import profile as profile_mod
from circt_flow import provenance, techpack

from conftest import REPO

TOOL = REPO / "scripts" / "mvt" / "make_abc_lib.py"


def lib(path, name, cells):
    path.write_text(f"library({name}) {{\n  time_unit : \"1ns\";\n" + "".join(
        f"  cell({c}) {{\n    area : 1.0 ;\n    cell_leakage_power : {lk} ;\n"
        "    pin(A) { direction : input; }\n  }\n"
        for c, lk in cells.items()) + "}\n")
    return path


def build(out, ins, leak=(), extra=()):
    cmd = [sys.executable, "-I", str(TOOL), str(out), *map(str, ins),
           "-W", "0.5", *extra]
    for p in leak:
        cmd += ["-L", str(p)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def pack(tmp_path):
    """One rail, corners ss (setup) / ff (leakage), priced library built
    from the ss libraries with ff leakage."""
    d = tmp_path / "lib"
    d.mkdir()
    cells = {"TINV_R": 1.0, "TINV_L": 2.0, "TICG_R": 1.0, "TTIEHI": 1.0,
             "TTIELO": 1.0}
    for c in ("ss", "ff"):
        lib(d / f"r_{c}.lib", f"t_r_{c}", {k: v for k, v in cells.items()
                                           if k.endswith("_R")
                                           or k.startswith("TTIE")})
        lib(d / f"l_{c}.lib", f"t_l_{c}", {"TINV_L": 2.0})
    build(d / "priced.lib", [d / "r_ss.lib", d / "l_ss.lib"],
          leak=[d / "r_ff.lib", d / "l_ff.lib"], extra=["-X", "TXX*"])
    p = tmp_path / "techpack.yaml"
    p.write_text("""
schema: 2
name: tstprov
time_unit: ns
icg: {cell: TICG_R, clk: CK, en: E, test_en: SE, out: ECK}
tie: {hi: "TTIEHI:H", lo: "TTIELO:L"}
corners: {names: [ss, ff], setup: ss, leakage: ff}
rails:
  VDD:
    voltage: 0.8
    liberties: [lib/priced.lib, lib/r_ss.lib, lib/l_ss.lib]
    corners:
      ss: [lib/r_ss.lib, lib/l_ss.lib]
      ff: [lib/r_ff.lib, lib/l_ff.lib]
""")
    return p


def verdict(p):
    prof = profile_mod.load(p)
    return provenance.check_rail(prof, prof.rails["VDD"])


def test_header_is_machine_readable(pack):
    h = provenance.read_header(pack.parent / "lib" / "priced.lib")
    assert [e["name"] for e in h["inputs"]] == ["r_ss.lib", "l_ss.lib"]
    assert [e["name"] for e in h["leakage_libs"]] == ["r_ff.lib", "l_ff.lib"]
    e = h["inputs"][0]
    assert len(e["sha256"]) == 64 and e["size"] > 0
    assert (h["W"], h["S"], h["exclude"], h["keep"]) == (0.5, 100.0,
                                                       ["TXX*"], [])
    assert h["cells"] == 5 and "git" in h["tool"]
    first = (pack.parent / "lib" / "priced.lib").read_text().split("\n")[0]
    assert first.startswith("/* ") and first.endswith(" */")
    assert "*/" not in first[:-2]


def test_ok_then_check_ok(pack, capsys):
    assert verdict(pack) == ("ok", [])
    assert techpack.check(str(pack)) == []
    assert "provenance ok" in capsys.readouterr().out


def test_stale_input_changed(pack, capsys):
    d = pack.parent / "lib"
    lib(d / "l_ss.lib", "t_l_ss", {"TINV_L": 3.0})   # a corner re-characterised
    v, why = verdict(pack)
    assert v == "stale" and "input l_ss.lib changed" in why[0]
    probs = techpack.check(str(pack))
    assert any("priced library priced.lib is stale" in p and "l_ss.lib" in p
               for p in probs)
    assert "stale" in capsys.readouterr().out


def test_stale_leakage_library_changed(pack):
    d = pack.parent / "lib"
    lib(d / "r_ff.lib", "t_r_ff", {"TINV_R": 9.0, "TICG_R": 1.0,
                                   "TTIEHI": 1.0, "TTIELO": 1.0})
    v, why = verdict(pack)
    assert v == "stale" and "leakage liberty r_ff.lib changed" in why[0]


def test_stale_list_changed(pack):
    """The pack lists another library than the one the priced file used."""
    d = pack.parent / "lib"
    lib(d / "x_ss.lib", "t_x_ss", {"TINV_X": 1.0})
    y = yaml.safe_load(pack.read_text())
    y["rails"]["VDD"]["corners"]["ss"].append("lib/x_ss.lib")
    pack.write_text(yaml.safe_dump(y))
    v, why = verdict(pack)
    assert v == "stale" and "x_ss.lib is in the rail's list" in " ".join(why)


def test_stale_edited_after_build(pack):
    p = pack.parent / "lib" / "priced.lib"
    p.write_text(p.read_text().replace("area : 150", "area : 151"))
    v, why = verdict(pack)
    assert v == "stale" and "edited after it was built" in why[0]


def test_stale_cell_not_in_sources(pack):
    """A header is not enough: a cell no source has is stale even when the
    body hash was recomputed to match."""
    import hashlib
    p = pack.parent / "lib" / "priced.lib"
    first, body = p.read_text().split("\n", 1)
    body = body.replace("}\n", "  cell(TGHOST) { area : 1 ; }\n}\n")
    head = first.replace(json.loads(first.split(": ", 1)[1][:-3])
                         ["body_sha256"],
                         hashlib.sha256(body.encode()).hexdigest())
    p.write_text(head + "\n" + body)
    v, why = verdict(pack)
    assert v == "stale" and "TGHOST" in why[-1]


def test_no_header_is_unverified(pack, capsys):
    """A priced library without a provenance line (a library built before
    the header existed) is unverified: a warning, not an error."""
    p = pack.parent / "lib" / "priced.lib"
    p.write_text(p.read_text().split("\n", 1)[1])
    v, why = verdict(pack)
    assert v == "unverified"
    assert techpack.check(str(pack)) == []
    assert "WARNING: priced library priced.lib: unverified" \
        in capsys.readouterr().out


def test_plain_rail_is_not_checked(rails_pack):
    """No corners and no header: nothing says the primary is priced."""
    prof = profile_mod.load(rails_pack)
    assert all(provenance.check_rail(prof, r) is None
               for r in prof.rails.values())


def test_enforce_modes(pack, capsys, monkeypatch):
    prof = profile_mod.load(pack)
    provenance._seen.clear()
    provenance.enforce(prof, "error")           # fresh: silent
    lib(pack.parent / "lib" / "l_ss.lib", "t_l_ss", {"TINV_L": 3.0})
    prof = profile_mod.load(pack)
    provenance.enforce(prof, "off")
    assert capsys.readouterr().err == ""
    provenance.enforce(prof, "warn")
    assert "WARNING" in capsys.readouterr().err
    with pytest.raises(ValueError, match="stale priced mapping library"):
        provenance.enforce(prof, "error")
    monkeypatch.setenv("CIRCT_ABC_PROVENANCE", "bogus")
    with pytest.raises(ValueError, match="expected off, warn or error"):
        provenance.enforce(prof)
