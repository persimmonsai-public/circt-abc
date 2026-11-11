"""multibit.py: the pack declaration, the flow config's switch, the
activity hook. The register-mapper side is mapper/test/register_mapper_multibit.mlir."""
import json

import pytest

from circt_flow import multibit

PACK = {
    "default_family": "a",
    "families": {
        "a": {"clear": {"cell": "TMB{n}C_A", "widths": [8, 4, 2],
                          "clock": "CK", "reset": "R", "data": "D{i}",
                          "out": "Q{i}", "tie": {"SE": 0, "SI": "last_q"}},
                "none": {"cell": "TMB{n}N_A", "widths": [8, 4, 2],
                         "clock": "CK", "data": "D{i}", "out": "Q{i}"}},
        "b": {"clear": {"cell": "TMB{n}C_B", "widths": [8, 4, 2],
                           "clock": "CK", "reset": "R", "data": "D{i}",
                           "out": "Q{i}"}},
    },
}


def test_pack_checked():
    assert multibit.parse_pack(PACK, "t") is PACK
    bad = json.loads(json.dumps(PACK))
    del bad["families"]["a"]["clear"]["reset"]
    with pytest.raises(KeyError, match="reset"):
        multibit.parse_pack(bad, "t")
    bad = json.loads(json.dumps(PACK))
    bad["families"]["a"]["clear"]["cell"] = "NOWIDTH"
    with pytest.raises(KeyError, match=r"\{n\}"):
        multibit.parse_pack(bad, "t")
    bad = json.loads(json.dumps(PACK))
    bad["default_family"] = "nope"
    with pytest.raises(KeyError, match="default_family"):
        multibit.parse_pack(bad, "t")


def test_off_unless_asked(tmp_path):
    assert multibit.resolve(PACK, None, tmp_path, "t") is None
    assert multibit.resolve(PACK, False, tmp_path, "t") is None
    with pytest.raises(KeyError, match="pack has none"):
        multibit.resolve(None, True, tmp_path, "t")


def test_cfg_overrides_and_order(tmp_path):
    spec = multibit.resolve(PACK, {"default_family": "b",
                                   "cross_register": True,
                                   "overrides": [{"reg": "^a_", "family": "a"}]},
                            tmp_path, "t")
    assert spec["default_family"] == "b" and spec["cross_register"]
    assert spec["overrides"] == [{"reg": "^a_", "family": "a"}]
    assert PACK.get("cross_register") is None  # the pack's dict is untouched
    with pytest.raises(KeyError, match="unknown"):
        multibit.resolve(PACK, {"famly": "x"}, tmp_path, "t")
    with pytest.raises(KeyError, match="not a family"):
        multibit.resolve(PACK, {"overrides": [{"reg": "x", "family": "z"}]},
                         tmp_path, "t")


def test_activity_hook(tmp_path):
    (tmp_path / "act.json").write_text(json.dumps(
        {"sum_q": 0.4, "flag_q": 0.01, "s1.q[0]": 0.2}))
    spec = multibit.resolve(
        PACK, {"activity": {"file": "act.json", "hot_family": "b",
                            "hot_toggle_rate": 0.1}}, tmp_path, "t")
    regs = [o["reg"] for o in spec["overrides"]]
    assert regs == [r"^s1\.q\[0\]" + r"(_cg\d+\w*)?$", r"^sum_q(_cg\d+\w*)?$"]  # sorted by name


def test_cells_and_check(tmp_path):
    names = multibit.cell_names(PACK)
    assert "TMB8C_A" in names and "TMB2N_A" in names and len(names) == 9
    assert multibit.check_cells(PACK, set(names)) == []
    assert multibit.check_cells(PACK, set(names) - {"TMB4C_B"}) == [
        "multibit cell TMB4C_B is not in the liberties"]


def test_write_roundtrip(tmp_path):
    p = multibit.write(PACK, tmp_path / "m.json")
    assert json.loads(p.read_text()) == PACK


def test_stage_option(tmp_path):
    from types import SimpleNamespace

    from circt_flow import stages
    off = SimpleNamespace(out_dir=tmp_path, design=SimpleNamespace(multibit=None))
    assert stages._multibit_option(off) == ""
    on = SimpleNamespace(out_dir=tmp_path, design=SimpleNamespace(multibit=PACK))
    opt = stages._multibit_option(on)
    assert opt == f" multibit={tmp_path / 'multibit.json'}"
    assert json.loads((tmp_path / "multibit.json").read_text()) == PACK


def test_timing_names():
    from circt_flow import multibit_timing as mt
    assert mt.local_register("u_top2Fu_stage2Fs2_q_cg1_mb24_31") == "s2_q_cg1"
    assert mt.local_register("top_q_bit3") == "top_q"
    assert mt.local_register("a_mb0_2_x1") == "a"
    assert mt.local_register("u_r2Fu_s2Fg0001") is None
    log = ("Startpoint: u_a2Fq_mb0_7 (rising edge-triggered flip-flop clocked by clk)\n"
           "Endpoint: u_a2Fr_bit1 (rising edge-triggered flip-flop clocked by clk)\n"
           "Endpoint: out_o[3] (output port clocked by clk)\n")
    assert mt.parse_paths(log) == {"q", "r"}
    assert mt.overrides({"r", "q"}, "single") == [
        {"reg": "^q$", "family": "single"}, {"reg": "^r$", "family": "single"}]


def test_overrides_file(tmp_path):
    (tmp_path / "o.json").write_text(json.dumps([{"reg": "^q$", "family": "b"}]))
    spec = multibit.resolve(PACK, {"overrides_file": "o.json"}, tmp_path, "t")
    assert spec["overrides"] == [{"reg": "^q$", "family": "b"}]


def test_activity_tcf(tmp_path):
    from circt_flow import multibit_activity as ma
    tcf = '''tcffile () {
  duration : "1000";
  unit : "ns";
  instance () {
    instance ("TOP") {
      instance ("dut") {
        net () {
          "q[0]" : "0.5 100";
          "q[1]" : "0.5 300";
          "r" : "1 0";
        }
        instance ("sub") {
          net () {
            "q[0]" : "0.5 600";
          }
        }
      }
    }
  }
}
'''
    rates = ma.parse(tcf, 1.0)
    assert rates["q"] == pytest.approx(0.6)   # max over instances of the bit mean
    assert rates["r"] == 0.0
    assert ma.parse(tcf, 2.0)["q"] == pytest.approx(1.2)


ENERGY_PACK = json.loads(json.dumps(PACK))
ENERGY_PACK["energy"] = {
    "area_weight_nw_per_um2": 5,
    "families": {
        "a": {"clock_aj": 10, "q_aj": 10, "d_aj": 10, "leak_nw": 1,
                "area_um2": 2},
        "b": {"clock_aj": 10, "q_aj": 100, "d_aj": 10, "leak_nw": 2,
                "area_um2": 1}}}


def test_energy_model_checked():
    multibit.parse_pack(ENERGY_PACK, "t")
    bad = json.loads(json.dumps(ENERGY_PACK))
    bad["energy"]["families"]["nope"] = {"clock_aj": 1}
    with pytest.raises(KeyError, match="not a family"):
        multibit.parse_pack(bad, "t")
    bad = json.loads(json.dumps(ENERGY_PACK))
    bad["energy"]["families"]["a"]["bogus"] = 1
    with pytest.raises(KeyError, match="takes"):
        multibit.parse_pack(bad, "t")
    bad = json.loads(json.dumps(ENERGY_PACK))
    bad["energy"]["families"]["a"]["q_aj"] = -1
    with pytest.raises(KeyError, match="number"):
        multibit.parse_pack(bad, "t")


def test_energy_rule_hot_stays_default_cold_goes_cheap(tmp_path):
    act = {"period_ns": 1.0, "registers": {
        "hot_q": {"q": 0.15, "d": 0.15, "clk": 0.45},
        "cold_q": {"q": 0.0001, "d": 0.0001, "clk": 0.001},
        "cold_cg1": {"q": 0.0, "d": 0.0, "clk": 0.0},
        "gated_off": {"q": 0.0, "d": 2.0, "clk": 0.0}}}
    (tmp_path / "a.json").write_text(json.dumps(act))
    spec = multibit.resolve(ENERGY_PACK, {"activity": {"file": "a.json", "rule": "energy"}},
                            tmp_path, "t")
    ov = {o["reg"]: o["family"] for o in spec["overrides"]}
    assert "^hot_q(_cg\\d+\\w*)?$" not in ov           # stays on the default
    assert ov["^cold_q(_cg\\d+\\w*)?$"] == "b"
    assert ov["^cold_cg1$"] == "b"                   # a suffixed name matches itself
    # no area credit: the cheaper-in-power family wins everywhere
    spec = multibit.resolve(ENERGY_PACK, {"activity": {
        "file": "a.json", "rule": "energy", "area_weight_nw_per_um2": 0}}, tmp_path, "t")
    assert spec["overrides"] == []
    # the pass gets no energy block
    out = multibit.write(spec, tmp_path / "spec.json")
    assert "energy" not in json.loads(out.read_text())


def test_energy_rule_needs_model_and_known_names(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"x": 0.1}))
    with pytest.raises(KeyError, match="energy model"):
        multibit.resolve(PACK, {"activity": {"file": "a.json", "rule": "energy"}},
                         tmp_path, "t")
    with pytest.raises(KeyError, match="candidates"):
        multibit.resolve(ENERGY_PACK, {"activity": {
            "file": "a.json", "rule": "energy", "candidates": ["z"]}}, tmp_path, "t")
    with pytest.raises(KeyError, match="threshold or energy"):
        multibit.resolve(ENERGY_PACK, {"activity": {"file": "a.json", "rule": "x"}},
                         tmp_path, "t")
    # a bare rate file: a busy ungated register keeps the default
    (tmp_path / "b.json").write_text(json.dumps({"busy": 0.3}))
    spec = multibit.resolve(ENERGY_PACK, {"activity": {"file": "b.json", "rule": "energy"}},
                            tmp_path, "t")
    assert spec["overrides"] == []


def test_threshold_rule_accepts_both_file_shapes(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        {"registers": {"hot": {"q": 0.5, "d": 0.5, "clk": 1.0}, "cold": {"q": 0.0}}}))
    spec = multibit.resolve(PACK, {"activity": {"file": "a.json", "hot_family": "b",
                                                "hot_toggle_rate": 0.1}}, tmp_path, "t")
    assert [o["family"] for o in spec["overrides"]] == ["b"]
    assert "hot" in spec["overrides"][0]["reg"]
