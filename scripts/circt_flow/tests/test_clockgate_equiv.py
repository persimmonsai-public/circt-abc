"""Clock-gate economy options are re-wirings, not logic changes: with
clock-gate-inference's `verify-with-mux` every accepted group is rebuilt as a
hold-mux on the plain clock on the group's (merged) enable, so the result,
exported to Verilog, must be sequentially equivalent to the same design with
clock gating off (Yosys miter + SAT from reset). That checks that merging
groups by enable function, and leaving an always-on enable ungated, never
change what a register loads; a deliberately wrong merge is caught. Hermetic
liberty: mapper/test/clock-gate-inference/icg.lib. Skipped without
circt-abc-opt, circt-opt and yosys (env CIRCT_ABC_OPT, CIRCT_OPT, YOSYS)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from circt_flow.config import ROOT

LIB = ROOT / "mapper" / "test" / "clock-gate-inference" / "icg.lib"

# whole-register path: four spellings of a&b, a tautology, c; bit-level path:
# a register that always loads (enable c | !c) beside one that holds on !c.
TOP = """hw.module @top(in %clk_i : i1, in %a : i1, in %b : i1, in %c : i1, in %d : i8,
               in %e : i8, out o : i56) {
  %clk = seq.to_clock %clk_i
  %true = hw.constant true
  %zero = hw.constant false
  %ab = comb.and %a, %b : i1
  %na = comb.xor %a, %true : i1
  %nb = comb.xor %b, %true : i1
  %nor_n = comb.or %na, %nb : i1
  %demorgan = comb.xor %nor_n, %true : i1
  %viamux = comb.mux %a, %b, %zero : i1
  %taut = comb.or %a, %na : i1
  %r1 = seq.firreg %m1 clock %clk {name = "r1"} : i8
  %n1 = comb.xor %d, %r1 : i8
  %m1 = comb.mux %ab, %n1, %r1 : i8
  %r2 = seq.firreg %m2 clock %clk {name = "r2"} : i8
  %n2 = comb.add %e, %r1 : i8
  %m2 = comb.mux %demorgan, %n2, %r2 : i8
  %r3 = seq.firreg %m3 clock %clk {name = "r3"} : i8
  %n3 = comb.xor %e, %r2 : i8
  %m3 = comb.mux %viamux, %n3, %r3 : i8
  %r4 = seq.firreg %m4 clock %clk {name = "r4"} : i8
  %n4 = comb.add %d, %r3 : i8
  %m4 = comb.mux %taut, %n4, %r4 : i8
  %r5 = seq.firreg %m5 clock %clk {name = "r5"} : i8
  %n5 = comb.xor %d, %r4 : i8
  %m5 = comb.mux %c, %n5, %r5 : i8
  %q = seq.firreg %nq clock %clk {name = "q"} : i8
  %nq = comb.mux %c, %r5, %e : i8
  %h = seq.firreg %nh clock %clk {name = "h"} : i8
  %nh = comb.mux %c, %r1, %h : i8
  %cat = comb.concat %r1, %r2, %r3, %r4, %r5, %q, %h : i8, i8, i8, i8, i8, i8, i8
  hw.output %cat : i56
}
"""


def _tool(env, *fallback):
    for c in (os.environ.get(env), *fallback):
        if c and Path(c).is_file():
            return c
    return None


ABC_OPT = _tool("CIRCT_ABC_OPT", str(ROOT / "mapper/build/bin/circt-abc-opt"))
CIRCT_OPT = _tool("CIRCT_OPT", str(ROOT / "third_party/circt/build/bin/circt-opt"))
YOSYS = _tool("YOSYS", str(ROOT / "third_party/yosys/build/yosys"),
              shutil.which("yosys"))
pytestmark = pytest.mark.skipif(not (ABC_OPT and CIRCT_OPT and YOSYS),
                                reason="needs circt-abc-opt, circt-opt, yosys")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def export(tmp, name, top, opts):
    src = tmp / f"{name}_in.mlir"
    src.write_text(top)
    mapped = tmp / f"{name}.mlir"
    p = run([ABC_OPT, f"--clock-gate-inference=liberty={LIB} {opts}",
             "--canonicalize", str(src), "-o", str(mapped)])
    assert p.returncode == 0, p.stderr
    v = run([CIRCT_OPT, str(mapped), "--lower-seq-to-sv",
               "--export-verilog", "-o", "/dev/null"])
    assert v.returncode == 0, v.stderr
    (tmp / f"{name}.v").write_text(v.stdout)
    return p.stderr


MODEL = ("module TICG(input CK, input E, input SE, output ECK);"
                     " assign ECK = CK & E; endmodule\n")


def prove(tmp, gold, gate, depth=10):
    (tmp / "icg_model.v").write_text(MODEL)
    ys = f"""
read_verilog -sv {tmp / 'icg_model.v'}
read_verilog -sv {tmp / (gold + '.v')}
hierarchy -top top; proc; flatten; opt_clean; rename top gold
design -stash gold
read_verilog -sv {tmp / 'icg_model.v'}
read_verilog -sv {tmp / (gate + '.v')}
hierarchy -top top; proc; flatten; opt_clean; rename top gate
design -stash gate
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
miter -equiv -flatten -make_outputs gold gate miter
hierarchy -top miter
clk2fflogic
opt_clean
sat -verify -seq {depth} -set-init-zero -prove trigger 0 miter
"""
    return run([YOSYS, "-q", "-p", ys])


OPTS = ["merge-equivalent-enables=true",
        "merge-equivalent-enables=true bit-level-enables=true",
        "merge-equivalent-enables=true bit-level-enables=true min-gate-bits=9 "
        "max-gate-depth=1 gate-cost-bits=2"]


@pytest.mark.parametrize("opts", OPTS)
def test_merged_and_ungated_groups_are_equivalent(tmp_path, opts):
    export(tmp_path, "gold", TOP, "enable-clock-gating=false")
    log = export(tmp_path, "gate", TOP, opts + " verify-with-mux=true")
    assert "merged by enable equivalence" in log
    p = prove(tmp_path, "gold", "gate")
    assert p.returncode == 0, p.stdout + p.stderr


def test_the_options_change_the_gate_count(tmp_path):
    base = export(tmp_path, "b", TOP, "bit-level-enables=true")
    merged = export(tmp_path, "m", TOP,
                    "bit-level-enables=true merge-equivalent-enables=true")
    import re
    n = lambda s: int(re.search(r"into (\d+) shared", s).group(1))
    assert n(merged) < n(base)


def test_a_wrong_merge_is_caught(tmp_path):
    """The same design with one register's enable altered (b instead of a & b)
    is not equivalent to the gold: the miter must fail."""
    export(tmp_path, "gold", TOP, "enable-clock-gating=false")
    bad = TOP.replace("%m3 = comb.mux %viamux", "%m3 = comb.mux %b")
    export(tmp_path, "gate", bad,
           "merge-equivalent-enables=true verify-with-mux=true")
    p = prove(tmp_path, "gold", "gate")
    assert p.returncode != 0
