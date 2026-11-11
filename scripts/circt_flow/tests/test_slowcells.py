"""circt_flow.slowcells: delay-quality filter for ABC's cell library."""
import pytest

from circt_flow import slowcells

CELL = """
  cell (%(n)s) {
    pin (Y) { direction : output;
      timing () {
        cell_rise (tbl) { index_1 ("0.01, 0.1"); index_2 ("0.001, 0.01");
          values ("%(d)s, %(d)s", "%(d)s, %(d)s"); }
        cell_fall (tbl) { index_1 ("0.01, 0.1"); index_2 ("0.001, 0.01");
          values ("%(d)s, %(d)s", "%(d)s, %(d)s"); }
      } }
  }"""


def _lib(cells):
    return ('library (t) {\n  time_unit : "1ns";\n'
            "  capacitive_load_unit (1, pf);"
            + "".join(CELL % {"n": n, "d": d} for n, d in cells) + "\n}\n")


def test_delays_units_and_state(tmp_path):
    txt = _lib([("A_R", 0.02), ("B_R", 0.05)])
    txt += "\n  cell (FF_R) { ff (IQ, IQN) { clocked_on : CK; } pin (Q) { direction : output; } }\n"
    d = slowcells.cell_delays(txt, 30, 2.5)
    assert d["A_R"] == pytest.approx(20.0)
    assert d["B_R"] == pytest.approx(50.0)
    assert "FF_R" not in d


def test_filter_ratio_and_family(tmp_path):
    p = tmp_path / "x.lib"
    p.write_text(_lib([("N2_R", 0.02), ("N3_R", 0.025), ("N4_R", 0.06),
                       ("N4_S", 0.02), ("N5_R", 0.022)]))
    spec = {"ratio": 1.5, "slew_ps": 30, "load_ff": 2.5}
    assert slowcells.slow_cells([p], spec) == ["N4_R"]
    # family: S is the fast twin of R, judged by the slowest member
    assert slowcells.slow_cells([p], spec, r"_([RS])$") == ["N4_R", "N4_S"]
    assert slowcells.slow_cells([p], {**spec, "ratio": 10}) == []


def test_parse_spec():
    assert slowcells.parse_spec(None, "w") is None
    assert slowcells.parse_spec({"ratio": 1.3}, "w") == {
        "ratio": 1.3, "slew_ps": 30.0, "load_ff": 2.5}
    for bad in ({}, {"ratio": 0}, {"ratio": 1, "x": 2}, {"ratio": True}):
        with pytest.raises(KeyError):
            slowcells.parse_spec(bad, "w")
