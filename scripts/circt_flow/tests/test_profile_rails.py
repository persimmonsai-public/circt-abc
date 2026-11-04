"""Tech-pack schema 2 (rails) and schema-1 back-compat."""
from pathlib import Path

import pytest
import yaml

from circt_flow import profile as profile_mod
from circt_flow import techpack

from conftest import REPO, write_lib, CELLS


def test_schema2_load(rails_pack):
    p = profile_mod.load(rails_pack)
    root = rails_pack.parent
    assert p.schema == 2
    assert list(p.rails) == ["HI", "LO"]
    assert p.default_rail == "HI" and p.rail == "HI"
    # The single-rail view is the default rail's.
    assert p.liberties == [root / "lib/hi_a.lib", root / "lib/hi_b.lib"]
    assert p.synth_libs == p.liberties
    assert p.voltage == 0.8
    assert p.flow == {"abc_read_lib_args": "-G 2", "partitions": 1}
    assert p.dont_use == ["*_X0"]
    assert p.level_shifters == [root / "lib/ls.lib"]


def test_for_rail(rails_pack):
    p = profile_mod.load(rails_pack)
    lo = p.for_rail("LO")
    root = rails_pack.parent
    assert lo.rail == "LO" and lo.voltage == 0.5
    assert lo.liberties == [root / "lib/lo_a.lib"]
    # Rail flow overrides the pack's, key by key.
    assert lo.flow == {"abc_read_lib_args": "-l", "partitions": 1,
                       "abc_script_file": root / "abc/lo.abc"}
    assert lo.dont_use == []
    assert lo.icg == p.icg and lo.tie == p.tie
    assert p.rail == "HI"  # unchanged
    with pytest.raises(KeyError, match="no rail 'MID'"):
        p.for_rail("MID")


def test_all_liberties(rails_pack):
    p = profile_mod.load(rails_pack)
    names = [x.name for x in p.all_liberties()]
    assert names == ["hi_a.lib", "hi_b.lib", "lo_a.lib", "ls.lib"]


@pytest.mark.parametrize("edit,err", [
    (("default_rail: HI", "default_rail: MID"), "default_rail 'MID' is not a rail"),
    (("default_rail: HI\n", ""), "default_rail is required"),
    (("    voltage: 0.5\n", ""), "voltage is required"),
    (("    voltage: 0.5", "    voltage: low"), "is not a number"),
    (("    liberties: [lib/lo_a.lib]", "    liberties: []"), "non-empty list"),
    (("    dont_use: []", "    vdd_pin: VDD"), "not a rail key"),
    (("    flow: {abc_read_lib_args", "    flow: {bogus: 1, abc_read_lib_args"),
     "rails.LO.flow.bogus is not a pack default"),
    (("schema: 2", "schema: 3"), "unsupported techpack schema 3"),
    (("level_shifters:\n  liberties: [lib/ls.lib]",
      "level_shifters:\n  cells: [X]"), "not a level-shifter key"),
])
def test_schema2_errors(rails_pack, edit, err):
    text = rails_pack.read_text()
    assert edit[0] in text
    rails_pack.write_text(text.replace(edit[0], edit[1], 1))
    with pytest.raises((KeyError, ValueError), match=err):
        profile_mod.load(rails_pack)


def test_schema2_top_level_liberties_rejected(rails_pack):
    rails_pack.write_text(rails_pack.read_text()
                          + "liberties: [lib/hi_a.lib]\n")
    with pytest.raises(ValueError, match="per rail"):
        profile_mod.load(rails_pack)


def test_schema1_rejects_rails(tmp_path):
    write_lib(tmp_path / "a.lib", "a", CELLS)
    pack = tmp_path / "techpack.yaml"
    pack.write_text("schema: 1\nname: one\nliberties: [a.lib]\n"
                    "icg: {cell: TSTICG_X1}\nrails: {}\n")
    with pytest.raises(KeyError, match="rails needs `schema: 2`"):
        profile_mod.load(pack)


SCHEMA1 = [REPO / "examples/techpack-template/techpack.yaml",
           *sorted((REPO / "examples/mvt-mini").glob("techpack*.yaml"))]


@pytest.mark.parametrize("pack", [p for p in SCHEMA1
                                  if "rails" not in p.name
                                  and "corners" not in p.name],
                         ids=lambda p: p.name)
def test_schema1_back_compat(pack):
    """A schema-1 pack is one implicit rail with exactly its liberties."""
    import yaml
    d = yaml.safe_load(pack.read_text())
    p = profile_mod.load(pack)
    assert p.schema == 1
    assert p.liberties == [pack.parent / l for l in d["liberties"]]
    assert list(p.rails) == [profile_mod.IMPLICIT_RAIL]
    assert p.rail == p.default_rail == profile_mod.IMPLICIT_RAIL
    r = p.rails[profile_mod.IMPLICIT_RAIL]
    assert r.liberties == p.liberties and r.flow == p.flow
    assert r.voltage is None and p.level_shifters == []
    assert p.for_rail(profile_mod.IMPLICIT_RAIL) == p
    assert p.all_liberties() == p.liberties




def test_mvt_mini_rails_pack():
    p = profile_mod.load(REPO / "examples/mvt-mini/techpack_rails.yaml")
    assert p.schema == 2 and p.default_rail == "VDD_HV"
    assert {n: r.voltage for n, r in p.rails.items()} == {
        "VDD_HV": 0.75, "VDD_LV": 0.55}
    for n, r in p.rails.items():
        assert r.liberties[0].name.startswith("mini_mvt_")
        assert r.flow["abc_read_lib_args"] == "-l"
        assert r.flow["abc_script_file"].name == "mvt.abc"
    assert [x.name for x in p.level_shifters] == ["mini_ls.lib"]


def test_check_schema2_ok(rails_pack, capsys):
    assert techpack.check(str(rails_pack)) == []
    out = capsys.readouterr().out
    assert "rail HI (default): 0.8 V, 2 liberties, 5 cells" in out
    assert "rail LO: 0.5 V, 1 liberties, 4 cells" in out
    assert "level shifters: 1 liberties, 1 cells" in out


def test_check_schema2_per_rail_cells(rails_pack):
    """The ICG must be in EACH rail's own liberties."""
    write_lib(rails_pack.parent / "lib/lo_a.lib", "tst_lo_a",
              ["TSTINV_X1", "TSTTIEHI", "TSTTIELO"])
    probs = techpack.check(str(rails_pack))
    assert probs == ["rails.LO: icg cell TSTICG_X1 is not in the liberties"]


def test_check_schema2_missing_files(rails_pack):
    (rails_pack.parent / "lib/ls.lib").unlink()
    (rails_pack.parent / "abc/lo.abc").unlink()
    probs = techpack.check(str(rails_pack))
    assert any(p.startswith("rails.LO: flow.abc_script_file") for p in probs)
    assert any(p.startswith("level_shifters: liberty") for p in probs)


def test_denylist_covers_all_rails(rails_pack):
    names, terms = techpack.denylist(rails_pack)
    for n in ("tst_hi_a", "tst_hi_b", "tst_lo_a", "tst_lvlshift",
              "TSTNAND_X1", "TSTLSHIFT_X1", "TSTICG_X1"):
        assert n in names


def test_denylist_public_pack_empty(rails_pack):
    rails_pack.write_text(rails_pack.read_text() + "public: true\n")
    assert techpack.denylist(rails_pack) == (set(), set())


def test_dont_use_reaches_abc_with_read_lib_args(rails_pack, tmp_path):
    # The pack sets abc_read_lib_args "-G 2" AND dont_use ["*_X0"]: ABC gets
    # both (the patterns used to be dropped whenever args were set).
    from circt_flow.config import FlowConfig, _read_lib_args
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump({
        "design": {"top_module": "top", "clock_period_ps": 1000,
                   "manifest": "m.yaml"},
        "pdk": {"profile": str(rails_pack)}}))
    fc = FlowConfig.load(f, tmp_path / "o", env={})
    assert fc.abc_read_lib_args == "-G 2 -X *_X0"
    fc = FlowConfig.load(f, tmp_path / "o", env={}, abc_read_lib_args="-l")
    assert fc.abc_read_lib_args == "-l -X *_X0"
    assert _read_lib_args("-l -X *_X0", ["*_X0"]) == "-l -X *_X0"
    assert _read_lib_args(None, []) is None
    assert _read_lib_args(None, ["A*"]) == "-X A*"
