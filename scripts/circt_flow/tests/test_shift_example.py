"""examples/shift-select: the map-shift-select flow on ASAP7, run twice.

Skipped unless the example's libraries are built (examples/asap7-mvt/lib/
make_libs.py into examples/shift-select/lib), circt-opt, circt-abc-opt and
yosys-abc are found, and (for the proof) yosys. The flow needs no Docker for
the part tested here (the synthesized netlist 1_synth/<top>.v).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_arith_lowering import ABC_OPT, CIRCT_OPT, YOSYS

ROOT = Path(__file__).resolve().parents[3]
EX = ROOT / "examples" / "shift-select"
LIB = EX / "lib" / "asap7_rvt_tt.lib"
ABC_BIN = os.environ.get("ABC_BIN") or str(ROOT / "third_party/yosys/build/yosys-abc")

pytestmark = pytest.mark.skipif(
    not (ABC_OPT and CIRCT_OPT and YOSYS and LIB.exists()
         and Path(ABC_BIN).exists()),
    reason="the example's libraries or a tool (circt-abc-opt, circt-opt, yosys, yosys-abc) is missing")


def _flow(arm, out):
    env = {**os.environ, "CIRCT_FLOW_NO_CLOCK_GATING": "1",
           "CIRCT_FLOW_OUTLINE_AFTER_FLATTEN": "1", "ABC_BIN": ABC_BIN,
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "circt_flow",
                        str(EX / "designs" / "align_unit" / f"flow_{arm}.yaml"), str(out)],
                       cwd=ROOT / "scripts", env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return (out / "1_synth" / "align_unit.v").read_text()


def _bare(netlist):
    """The netlist without export-verilog's location comments (they name the output dir)."""
    return re.sub(r"[ \t]*//[^\n]*", "", netlist)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("shiftex")
    return {"base": _flow("base", d / "base"), "shift": _flow("shift", d / "shift"),
            "shift2": _flow("shift", d / "shift2")}


def test_shift_flow_is_deterministic(runs):
    """The same input twice gives the same netlist."""
    assert _bare(runs["shift"]) == _bare(runs["shift2"])


def test_shift_pass_rewrote_the_select(runs):
    assert "shs_" in runs["shift"] and "shs_" not in runs["base"]


def _prove(tmp_path, a, b):
    sub = re.sub(r"(?m)^module align_unit\b", "module align_unit_new", b)
    (tmp_path / "a.v").write_text(a)
    (tmp_path / "b.v").write_text(sub)
    script = (f"read_liberty -ignore_miss_func {LIB}; read_verilog {tmp_path}/a.v {tmp_path}/b.v; "
              "proc; miter -equiv -flatten -make_assert align_unit align_unit_new miter; "
              "async2sync; sat -verify -prove-asserts -seq 4 -set-init-zero miter")
    return subprocess.run([YOSYS, "-q", "-p", script], capture_output=True, text=True)


def test_shift_netlist_equals_base(runs, tmp_path):
    assert _prove(tmp_path, runs["base"], runs["shift"]).returncode == 0


def test_miter_is_not_vacuous(runs, tmp_path):
    broken = runs["shift"].replace("NOR2xp33_ASAP7_75t_R", "NAND2xp33_ASAP7_75t_R", 1)
    assert broken != runs["shift"]
    assert _prove(tmp_path, runs["base"], broken).returncode != 0
