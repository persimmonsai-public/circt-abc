"""Multibit banking is a re-cell, not a logic change: register-mapper's
output with and without the multibit spec, exported to Verilog with
behavioural models of the (invented) cells, is sequentially equivalent from
reset (Yosys miter + SAT, clk2fflogic), and a deliberately wrong bank model
is caught. Hermetic cells: mapper/test/register_mapper_multibit.{lib,json}.
Skipped without circt-abc-opt, circt-opt and yosys (env CIRCT_ABC_OPT,
CIRCT_OPT, YOSYS, as the other tool tests)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from circt_flow.config import ROOT

MT = ROOT / "mapper" / "test"

TOP = """hw.module @top(in %clk_i : i1, in %rst : i1, in %d : i11, in %e : i5, out q : i31) {
  %clk = seq.to_clock %clk_i
  %z11 = hw.constant 0 : i11
  %z5 = hw.constant 0 : i5
  %s2 = hw.constant 3 : i2
  %one5 = hw.constant 1 : i5
  %r = seq.firreg %rn clock %clk reset async %rst, %z11 {name = "r"} : i11
  %rn = comb.xor %r, %d : i11
  %h = seq.firreg %hn clock %clk reset async %rst, %z5 {name = "hot_r"} : i5
  %hn = comb.add %h, %e : i5
  %n = seq.firreg %nn clock %clk {name = "n"} : i3
  %rx = comb.extract %r from 3 : (i11) -> i3
  %nn = comb.xor %n, %rx : i3
  %s = seq.firreg %sn clock %clk reset async %rst, %s2 {name = "s"} : i2
  %hx = comb.extract %h from 0 : (i5) -> i2
  %sn = comb.xor %s, %hx : i2
  %xa = seq.firreg %xan clock %clk reset async %rst, %z5 {name = "xa"} : i5
  %xb = seq.firreg %xbn clock %clk reset async %rst, %z5 {name = "xb"} : i5
  %xan = comb.xor %xb, %e : i5
  %xbn = comb.add %xa, %one5 : i5
  %o = comb.concat %xb, %xa, %s, %n, %h, %r : i5, i5, i2, i3, i5, i11
  hw.output %o : i31
}
"""

SINGLE_MODELS = """
module TDFF(input CK, input D, output reg Q); always @(posedge CK) Q <= D; endmodule
module TDFFR(input CK, input D, input R, output reg Q);
  always @(posedge CK or posedge R) if (R) Q <= 0; else Q <= D; endmodule
module TDFFS(input CK, input D, input SN, output reg Q);
  always @(posedge CK or negedge SN) if (!SN) Q <= 1; else Q <= D; endmodule
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


def bank_models(spec, mutate=False):
    out = []
    for fam in spec["families"].values():
        for cname, c in fam.items():
            for n in c["widths"]:
                name = c["cell"].replace("{n}", str(n))
                ins = [c["clock"]] + ([c["reset"]] if "reset" in c else []) \
                    + list(c.get("tie", {})) + [c["data"].replace("{i}", str(i)) for i in range(n)]
                outs = [c["out"].replace("{i}", str(i)) for i in range(n)]
                ds = [c["data"].replace("{i}", str(i)) for i in range(n)]
                if mutate and n == 2:
                    ds[0], ds[1] = ds[1], ds[0]
                body = "; ".join(f"{o} <= {d}" for o, d in zip(outs, ds))
                rst = ""
                if "reset" in c:
                    r = c["reset"]
                    rst = f"if ({r}) begin " + "; ".join(f"{o} <= 0" for o in outs) + "; end else "
                ev = f"posedge {c['clock']}" + (f" or posedge {c['reset']}" if "reset" in c else "")
                out.append(f"module {name}({', '.join('input ' + i for i in ins)}, "
                           f"{', '.join('output reg ' + o for o in outs)});\n"
                           f"  always @({ev}) {rst}begin {body}; end\nendmodule\n")
    return "\n".join(out)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def export(tmp, name, spec_json):
    src = tmp / "top.mlir"
    src.write_text(TOP)
    opt = f"liberty={MT / 'register_mapper_multibit.lib'}" + (
        f" multibit={spec_json}" if spec_json else "")
    mapped = tmp / f"{name}.mlir"
    p = run([ABC_OPT, f"--register-mapper={opt}", "--canonicalize", str(src),
             "-o", str(mapped)])
    assert p.returncode == 0, p.stderr
    v = run([CIRCT_OPT, str(mapped), "--export-verilog", "-o", "/dev/null"])
    assert v.returncode == 0, v.stderr
    (tmp / f"{name}.v").write_text(v.stdout)
    return p.stderr


def prove(tmp, models_gold, models_gate, depth=14):
    ys = f"""
read_verilog -sv {models_gold}
read_verilog -sv {tmp / 'single.v'}
hierarchy -top top; proc; flatten; opt_clean; rename top gold
design -stash gold
read_verilog -sv {models_gate}
read_verilog -sv {tmp / 'mb.v'}
hierarchy -top top; proc; flatten; opt_clean; rename top gate
design -stash gate
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
miter -equiv -flatten -make_outputs gold gate miter
hierarchy -top miter
clk2fflogic
opt_clean
sat -verify -seq {depth} -set-at 1 in_rst 0 -set-init-zero -prove trigger 0 miter
"""
    return run([YOSYS, "-q", "-p", ys])


@pytest.mark.parametrize("spec", ["register_mapper_multibit.json",
                                  "register_mapper_multibit_cross.json"])
def test_banking_is_a_recell(tmp_path, spec):
    log = export(tmp_path, "single", None)
    log += export(tmp_path, "mb", MT / spec)
    assert "multibit:" in log and "bank(s)" in log
    sp = json.loads((MT / spec).read_text())
    gold = tmp_path / "gold_models.v"
    gold.write_text(SINGLE_MODELS)
    gate = tmp_path / "gate_models.v"
    gate.write_text(SINGLE_MODELS + bank_models(sp))
    p = prove(tmp_path, gold, gate)
    assert p.returncode == 0, p.stdout + p.stderr


def test_wrong_bank_model_is_caught(tmp_path):
    spec = "register_mapper_multibit_cross.json"
    export(tmp_path, "single", None)
    export(tmp_path, "mb", MT / spec)
    sp = json.loads((MT / spec).read_text())
    gold = tmp_path / "gold_models.v"
    gold.write_text(SINGLE_MODELS)
    gate = tmp_path / "gate_models.v"
    gate.write_text(SINGLE_MODELS + bank_models(sp, mutate=True))
    p = prove(tmp_path, gold, gate)
    assert p.returncode != 0
