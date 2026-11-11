"""Equivalence of the word-level arithmetic lowering passes (lower-carry-in-adds,
lower-barrel-shifts): each module of the lit tests is exported to Verilog before
and after the pass and proven equal by a yosys SAT miter. Skipped when
circt-abc-opt, circt-opt or yosys is not found (CIRCT_ABC_OPT, CIRCT_OPT, YOSYS)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TESTS = ROOT / "mapper" / "test"


def _tool(env, default):
    p = os.environ.get(env) or default
    return p if p and (shutil.which(p) or Path(p).exists()) else None


ABC_OPT = _tool("CIRCT_ABC_OPT", str(ROOT / "mapper/build/bin/circt-abc-opt"))
CIRCT_OPT = _tool("CIRCT_OPT", str(ROOT / "third_party/circt/build/bin/circt-opt"))
YOSYS = _tool("YOSYS", str(ROOT / "third_party/yosys/build/yosys"))
pytestmark = pytest.mark.skipif(not (ABC_OPT and CIRCT_OPT and YOSYS),
                                reason="circt-abc-opt, circt-opt or yosys missing")


def _verilog(mlir: Path) -> str:
    return subprocess.run([CIRCT_OPT, str(mlir), "--export-verilog", "-o", os.devnull],
                          check=True, capture_output=True, text=True).stdout


def _equiv(tmp_path, src: Path, module: str, passes, lib: Path = None):
    after = tmp_path / "after.mlir"
    after.write_text(subprocess.run([ABC_OPT, *passes, str(src)], check=True,
                                    capture_output=True, text=True).stdout)
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_text(_verilog(src))
    b.write_text(re.sub(r"(?m)^module (\w+)\(", r"module \1_new(", _verilog(after)))
    rl = f"read_liberty -ignore_miss_func {lib}; " if lib else ""
    script = (f"{rl}read_verilog -sv {a} {b}; proc; miter -equiv -flatten -make_assert "
              f"{module} {module}_new miter; sat -verify -prove-asserts miter")
    return subprocess.run([YOSYS, "-q", "-p", script], capture_output=True, text=True)


@pytest.mark.parametrize("module", ["cin_var", "cin_first"])
def test_carry_in_adds_equivalent(tmp_path, module):
    r = _equiv(tmp_path, TESTS / "lower_carry_in_adds.mlir", module,
               ["--lower-carry-in-adds=enable=true"])
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("module", ["shl32", "shru16", "shrs"])
def test_barrel_shifts_equivalent(tmp_path, module):
    r = _equiv(tmp_path, TESTS / "lower_barrel_shifts.mlir", module,
               ["--lower-barrel-shifts=enable=true"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_negative_control_detects_difference(tmp_path):
    """The miter must fail for a real difference (add vs xor)."""
    src = tmp_path / "x.mlir"
    src.write_text("hw.module @m(in %a : i8, in %b : i8, out y : i8) {\n"
                   "  %0 = comb.add %a, %b : i8\n  hw.output %0 : i8\n}\n")
    other = tmp_path / "y.mlir"
    other.write_text(src.read_text().replace("comb.add", "comb.xor"))
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text(_verilog(src))
    b.write_text(re.sub(r"(?m)^module (\w+)\(", r"module \1_new(", _verilog(other)))
    r = subprocess.run([YOSYS, "-q", "-p", f"read_verilog -sv {a} {b}; proc; miter -equiv -flatten "
                        f"-make_assert m m_new miter; sat -verify -prove-asserts miter"],
                       capture_output=True, text=True)
    assert r.returncode != 0


PREFIX = TESTS / "map-prefix-adders"


@pytest.mark.parametrize("arch", ["SKLANSKEY", "KOGGE-STONE", "HAN-CARLSON", "BRENT-KUNG"])
@pytest.mark.parametrize("module", ["add16", "add32_cin", "add48_one", "add8_const", "untouched"])
def test_prefix_adders_equivalent(tmp_path, module, arch):
    """Every architecture, SAT-proven (combinational, so the proof is complete)."""
    r = _equiv(tmp_path, PREFIX / "adds.mlir", module,
               [f"--map-prefix-adders=enable-prefix-adders=true liberty={PREFIX}/prefix.lib "
                f"min-width=8 arch={arch}"], lib=PREFIX / "prefix.lib")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("w", [8, 13, 16, 20, 32, 48, 64])
def test_prefix_adders_widths(tmp_path, w):
    src = tmp_path / "w.mlir"
    src.write_text(f"hw.module @m(in %a : i{w}, in %b : i{w}, in %c : i1, out y : i{w}, out z : i{w}) {{\n"
                   f"  %zz = hw.constant 0 : i{w - 1}\n  %cz = comb.concat %zz, %c : i{w - 1}, i1\n"
                   f"  %0 = comb.add %a, %b : i{w}\n  %1 = comb.add %a, %b, %cz : i{w}\n"
                   f"  hw.output %0, %1 : i{w}, i{w}\n}}\n")
    for arch in ("SKLANSKEY", "KOGGE-STONE", "HAN-CARLSON", "BRENT-KUNG"):
        r = _equiv(tmp_path, src, "m",
                   [f"--map-prefix-adders=enable-prefix-adders=true liberty={PREFIX}/prefix.lib "
                    f"min-width=8 arch={arch}"], lib=PREFIX / "prefix.lib")
        assert r.returncode == 0, (arch, r.stdout + r.stderr)


COMPRESSOR = TESTS / "map-compressor-cells"


@pytest.mark.parametrize("module", ["merge_prod", "merge_gated", "wrap"])
@pytest.mark.parametrize("merge", ["false", "true"])
def test_compressor_merge_adds_equivalent(tmp_path, module, merge):
    """merge-adds folds a product's carry-propagate add into the following add's compress tree
    where the inner sum cannot wrap; SAT-proven (combinational) with and without it."""
    r = _equiv(tmp_path, COMPRESSOR / "merge_adds.mlir", module,
               [f"--map-compressor-cells=enable-compressor-cells=true liberty={COMPRESSOR}/adders.lib "
                f"min-width=8 merge-adds={merge}"], lib=COMPRESSOR / "adders.lib")
    assert r.returncode == 0, r.stdout + r.stderr


def test_compressor_merge_adds_counts():
    """The wrapping case is left alone, the other two modules merge once each."""
    out = subprocess.run([ABC_OPT, "--map-compressor-cells=enable-compressor-cells=true "
                          f"liberty={COMPRESSOR}/adders.lib min-width=8 merge-adds=true",
                          str(COMPRESSOR / "merge_adds.mlir")], check=True,
                         capture_output=True, text=True).stderr
    assert "merge-adds: 2 carry-propagate" in out


def test_adder_chains_max_width_equivalent(tmp_path):
    """max-width keeps long adds as comb.add; the whole module is SAT-proven equal either way."""
    d = TESTS / "map-adder-chains"
    r = _equiv(tmp_path, d / "max_width.mlir", "m",
               [f"--map-adder-chains=enable-adder-chains=true liberty={d}/adders.lib min-width=8 max-width=16"],
               lib=d / "adders.lib")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("module", ["chain", "shared", "zext_ok", "zext_wrap", "gate_inv", "wrap_inner"])
@pytest.mark.parametrize("gated", ["false", "true"])
def test_compressor_flatten_adds_equivalent(tmp_path, module, gated):
    """flatten-adds (with and without gating distribution): SAT-proven (combinational)."""
    r = _equiv(tmp_path, COMPRESSOR / "flatten_adds.mlir", module,
               [f"--map-compressor-cells=enable-compressor-cells=true liberty={COMPRESSOR}/adders.lib "
                f"min-width=8 flatten-adds=true flatten-gated={gated}"], lib=COMPRESSOR / "adders.lib")
    assert r.returncode == 0, r.stdout + r.stderr


def test_compressor_flatten_adds_counts():
    """The non-wrapping zero-extended and gated inner adds merge (no narrow add left); the wrapping ones stay."""
    out = subprocess.run([ABC_OPT, "--map-compressor-cells=enable-compressor-cells=true "
                          f"liberty={COMPRESSOR}/adders.lib min-width=8 flatten-adds=true",
                          str(COMPRESSOR / "flatten_adds.mlir")], check=True, capture_output=True, text=True)
    adds = {}
    mod = None
    for line in out.stdout.splitlines():
        m = re.match(r"\s*hw.module @(\w+)", line)
        if m:
            mod = m.group(1)
        m = re.search(r"comb.add .* : i(\d+)$", line)
        if m:
            adds.setdefault(mod, []).append(int(m.group(1)))
    assert adds["zext_ok"] == [9] and adds["gate_inv"] == [8]
    assert sorted(adds["zext_wrap"]) == [8, 9] and sorted(adds["shared"]) == [8, 8]
    assert 8 in adds["wrap_inner"]
