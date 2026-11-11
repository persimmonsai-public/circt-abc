"""Equivalence of map-shift-select (every recipe and option set, a negative control for the
don't-care option, and run-to-run determinism): the select over shifted copies of one word is
exported to Verilog before and after the pass (cells from the hermetic liberty) and proven equal
by a yosys SAT miter. Skipped when circt-abc-opt, circt-opt or yosys is not found."""
import pytest
from test_arith_lowering import ABC_OPT, CIRCT_OPT, YOSYS, TESTS, _equiv

pytestmark = pytest.mark.skipif(not (ABC_OPT and CIRCT_OPT and YOSYS),
                                reason="circt-abc-opt, circt-opt or yosys missing")


@pytest.mark.parametrize("radix", [2, 4, 8])
def test_shift_select_equivalent(tmp_path, radix):
    lib = TESTS / "map-shift-select" / "shift.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_select.mlir", "shsel",
               [f"--map-shift-select=enable-shift-select=true liberty={lib} min-arms=8 "
                f"min-width=8 radix={radix}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("radix", [2, 4])
def test_shift_select_wide4_equivalent(tmp_path, radix):
    """Four-term AND-OR / OR-AND cells (polarity chosen per operand set) compute the same function."""
    lib = TESTS / "map-shift-select" / "shift4.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_select.mlir", "shsel",
               [f"--map-shift-select=enable-shift-select=true liberty={lib} min-arms=8 "
                f"min-width=8 radix={radix} wide4-cells=true"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "4,2@4,2,2", "2@2,2"])
def test_shift_select_recipe_equivalent(tmp_path, recipes):
    """Mixed-radix recipe (ripple negate, polarity-matched levels, tautology pass-through) is
    equivalent to the one-hot select over shifted copies of a conditionally negated word."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_recipe.mlir", "shneg",
               [f"--map-shift-select=enable-shift-select=true liberty={lib} min-arms=8 "
                f"min-width=8 wide4-cells=true recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "4,2@4,2,2", "2,2@2,2,4"])
def test_shift_select_chain_equivalent(tmp_path, recipes):
    """A priority mux chain over distinct constants of one amount (pre-canonicalization `case`
    form) with a `sub`-form conditional negate is rebuilt as the recipe network, equivalently."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_chain.mlir", "shchain",
               [f"--map-shift-select=enable-shift-select=true chains=true liberty={lib} min-arms=8 "
                f"min-width=8 wide4-cells=true recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "2,2,2@2,2,2,3", "4,2@4,2,2", "2,2@2,2,4"])
def test_shift_select_direct_decode_equivalent(tmp_path, recipes):
    """direct-decode: the digit selects are cell networks of the raw bits of a wrapping
    `slice + constant` amount (exact, default arm included), for any digit-boundary offset."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_affine.mlir", "shaff",
               [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                f"liberty={lib} min-arms=8 min-width=8 wide4-cells=true recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "4,2@4,2,2", "2,2@2,2,4"])
def test_shift_select_direct_decode_dont_care_equivalent(tmp_path, recipes):
    """default-dont-care: equal to the source on the care set (the output is masked outside it
    in the module itself, so the miter proves equality there only)."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_affine_care.mlir", "shaffc",
               [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                f"default-dont-care=true select-max-fanout=20 liberty={lib} min-arms=8 "
                f"min-width=8 wide4-cells=true recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


def test_shift_select_dont_care_negative_control(tmp_path):
    """The dont-care variant must differ from the source when the mask is absent (the miter is
    not vacuous): the unmasked source with default-dont-care=true is NOT equivalent."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_affine.mlir", "shaff",
               [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                f"default-dont-care=true liberty={lib} min-arms=8 min-width=8 wide4-cells=true "
                f"recipes=4,2@4,2,2"], lib=lib)
    assert r.returncode != 0


@pytest.mark.parametrize("name,extra", [("shift_chain", ""), ("shift_affine", "direct-decode=true ")])
def test_shift_select_output_is_deterministic(name, extra):
    """The pin order of the cells is a property of the input: cell identities in the memo keys are
    first-seen sequence numbers, not addresses (which also get reused by the cells of discarded
    candidate builds). 40 runs of one input give one output, and no helper attribute is left."""
    import subprocess
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    cmd = [ABC_OPT, f"--map-shift-select=enable-shift-select=true chains=true {extra}liberty={lib} "
           "min-arms=8 min-width=8 wide4-cells=true recipes=4,2@4,2,2",
           str(TESTS / "map-shift-select" / f"{name}.mlir")]
    outs = {subprocess.run(cmd, capture_output=True, text=True).stdout for _ in range(40)}
    assert len(outs) == 1 and "shs.id" not in next(iter(outs))
