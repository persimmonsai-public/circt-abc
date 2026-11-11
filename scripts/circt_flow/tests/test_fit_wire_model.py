"""scripts/fit_wire_model.py on invented data."""
import subprocess
import sys

import pytest

from circt_flow.config import ROOT

SCRIPT = ROOT / "scripts" / "fit_wire_model.py"


def _load():
    sys.path.insert(0, str(ROOT / "scripts"))
    import fit_wire_model
    return fit_wire_model


def test_recovers_a_line(tmp_path):
    fw = _load()
    rows = [(f, 0.2 + 0.3 * f) for f in (1, 1, 2, 3, 4, 5, 8)]
    base, per, r2, origin = fw.fit(rows)
    assert base == pytest.approx(0.2) and per == pytest.approx(0.3)
    assert r2 == pytest.approx(1.0) and origin > 0.3
    t = fw.table(rows)
    assert [x[:3] for x in t] == [(1, 1, 2), (2, 2, 1), (3, 3, 1), (4, 4, 1),
                                  (5, 6, 1), (7, 8, 1)]


def test_cli_reads_tsv_and_prints_the_snippet(tmp_path):
    tsv = tmp_path / "nets.tsv"
    tsv.write_text("net\tfanout\twirecap_pf\n" + "".join(
        f"n{i}\t{f}\t{(0.5 + 0.25 * f) / 1000}\n"
        for i, f in enumerate([1, 2, 2, 3, 4, 6, 9])) + "bad\tx\t1\n")
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tsv)],
                       capture_output=True, text=True, check=True)
    assert "7 nets" in p.stdout
    assert "cap_base_ff: 0.500" in p.stdout
    assert "cap_per_fanout_ff: 0.250" in p.stdout
    bad = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tsv) + "x"],
                         capture_output=True, text=True)
    assert bad.returncode != 0
