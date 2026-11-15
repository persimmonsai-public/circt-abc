"""Equivalence and "is applied" tests of mapper capabilities: map-prefix-adders boundary / isolate-inputs, the
combinational part of data-gate-enables, map-compressor-cells
map-final-min-width. Same method as test_shift_select.py (yosys SAT miter
of the exported Verilog before and after the pass). Skipped when
circt-abc-opt, circt-opt or yosys is not found."""
import re
import subprocess

import pytest
from test_arith_lowering import ABC_OPT, CIRCT_OPT, YOSYS, TESTS, _equiv, _verilog

pytestmark = pytest.mark.skipif(not (ABC_OPT and CIRCT_OPT and YOSYS),
                                reason="circt-abc-opt, circt-opt or yosys missing")

PREFIX = TESTS / "map-prefix-adders"
LIB = PREFIX / "prefix.lib"


def _pfx(extra, arch="KOGGE-STONE"):
    return [f"--map-prefix-adders=enable-prefix-adders=true liberty={LIB} min-width=4 "
            f"arch={arch} {extra}"]


@pytest.mark.parametrize("opt", ["boundary=1", "isolate-inputs=true"])
@pytest.mark.parametrize("arch", ["SKLANSKEY", "KOGGE-STONE", "HAN-CARLSON", "BRENT-KUNG"])
@pytest.mark.parametrize("module", ["add16", "add32_cin", "add40_one", "add8_const", "untouched"])
def test_prefix_boundary_isolate_equivalent(tmp_path, module, arch, opt):
    r = _equiv(tmp_path, PREFIX / "adds.mlir", module, _pfx(opt, arch), lib=LIB)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("w", [6, 14, 21, 40, 60])
@pytest.mark.parametrize("opt", ["boundary=1", "isolate-inputs=true"])
def test_prefix_boundary_isolate_widths(tmp_path, w, opt):
    src = tmp_path / "w.mlir"
    src.write_text(f"hw.module @m(in %a : i{w}, in %b : i{w}, in %c : i1, out y : i{w}, out z : i{w}) {{\n"
                   f"  %zz = hw.constant 0 : i{w - 1}\n  %cz = comb.concat %zz, %c : i{w - 1}, i1\n"
                   f"  %0 = comb.add %a, %b : i{w}\n  %1 = comb.add %a, %b, %cz : i{w}\n"
                   f"  hw.output %0, %1 : i{w}, i{w}\n}}\n")
    r = _equiv(tmp_path, src, "m", _pfx(opt), lib=LIB)
    assert r.returncode == 0, r.stdout + r.stderr


def _run(passes, src):
    return subprocess.run([ABC_OPT, *passes, str(src)], capture_output=True, text=True)


@pytest.mark.parametrize("opt,cells", [("boundary=1", {"TNAND2", "TNOR2"}),
                                        ("isolate-inputs=true", None)])
def test_prefix_boundary_isolate_is_applied(opt, cells):
    """The equivalence above would also pass if the option were ignored: the
    option changes the netlist, and boundary=1 leaves only NAND2/NOR2 cells."""
    base, r = _run(_pfx(""), PREFIX / "adds.mlir"), _run(_pfx(opt), PREFIX / "adds.mlir")
    assert r.returncode == 0 and r.stdout != base.stdout
    if cells:
        used = set(re.findall(r"hw\.instance \"[^\"]*\" @(\w+)", r.stdout))
        assert used and used <= cells, used


def test_prefix_boundary_other_than_0_or_1_is_refused():
    r = subprocess.run([ABC_OPT, *_pfx("boundary=2"), str(PREFIX / "adds.mlir")],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "boundary=2 is not supported" in r.stderr, r.stderr


DG = TESTS / "data-gate-enables" / "data_gate.mlir"
SLICES = "--data-gate-enables=slices=q@0-3,q@12-15@xor,dg/h@4-7"


def _seq_equiv(tmp_path, before_text, after_text, module):
    """Sequential miter of two versions of a module with registers: equal
    from a zero state, and inductive (the outputs expose every register, so
    one step of induction proves the next-state functions equal)."""
    a, b = tmp_path / "a.mlir", tmp_path / "b.mlir"
    a.write_text(before_text)
    b.write_text(after_text)
    def verilog(m):
        return subprocess.run([CIRCT_OPT, str(m), "--lower-seq-to-sv", "--export-verilog",
                               "-o", "/dev/null"], check=True, capture_output=True,
                              text=True).stdout
    va, vb = tmp_path / "a.v", tmp_path / "b.v"
    va.write_text(verilog(a))
    vb.write_text(re.sub(r"(?m)^module (\w+)\(", r"module \1_new(", verilog(b)))
    script = (f"read_verilog -sv {va} {vb}; proc; miter -equiv -flatten -make_assert "
              f"{module} {module}_new miter; hierarchy -top miter; flatten; "
              f"sat -verify -prove-asserts -tempinduct -set-init-zero -seq 1 miter")
    return subprocess.run([YOSYS, "-q", "-p", script], capture_output=True, text=True)


@pytest.mark.parametrize("module", ["dg", "other"])
def test_data_gate_rewrite_is_the_identity(tmp_path, module):
    """The register-level identity of the rewrite (combinational part: the
    new next-state function equals the old one in every state); the clock
    gate it leads to is a separate clock-gating equivalence, not proven here."""
    src = DG.read_text()
    after = _run([SLICES], DG)
    assert after.returncode == 0 and "3 register(s) rewritten" in after.stderr
    r = _seq_equiv(tmp_path, src, after.stdout, module)
    assert r.returncode == 0, r.stdout + r.stderr


def test_data_gate_miter_is_not_vacuous(tmp_path):
    """Negative control: a wrong hold polarity is caught."""
    src = DG.read_text()
    bad = src.replace("%n = comb.mux %en, %d, %q : i16", "%n = comb.mux %en, %q, %d : i16")
    assert bad != src
    r = _seq_equiv(tmp_path, src, bad, "dg")
    assert r.returncode != 0


def test_data_gate_is_applied():
    base, r = _run([], DG), _run([SLICES], DG)
    assert r.stdout != base.stdout and r.stdout.count("comb.icmp eq") >= 5


COMP = TESTS / "map-compressor-cells"


@pytest.mark.parametrize("minw", [0, 15, 17])
def test_map_final_min_width_equivalent(tmp_path, minw):
    """map-final-adder with map-final-min-width only tags adds (an attribute):
    the function is unchanged; mapped through map-adder-chains as in the flow."""
    lib = COMP / "adders.lib"
    r = _equiv(tmp_path, COMP / "mul_variants.mlir", "mul8",
               [f"--map-compressor-cells=enable-compressor-cells=true liberty={lib} min-width=4 "
                f"map-final-adder=true map-final-min-width={minw}",
                f"--map-adder-chains=liberty={lib} enable-adder-chains=true min-width=4",
                "--canonicalize", "--cse"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


def test_map_final_min_width_is_applied():
    lib = COMP / "adders.lib"
    outs = {w: _run([f"--map-compressor-cells=enable-compressor-cells=true liberty={lib} "
                     f"min-width=8 map-final-adder=true map-final-min-width={w}"],
                    COMP / "mul_variants.mlir").stderr for w in (0, 15, 17)}
    assert "final adds tagged 2" in outs[0]
    assert "final adds tagged 1" in outs[15]
    assert "final adds tagged 0" in outs[17]
