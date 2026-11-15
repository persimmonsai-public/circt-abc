"""Equivalence of map-shift-select (every recipe and option set, a negative control for the
don't-care option, and run-to-run determinism): the select over shifted copies of one word is
exported to Verilog before and after the pass (cells from the hermetic liberty) and proven equal
by a yosys SAT miter. Skipped when circt-abc-opt, circt-opt or yosys is not found."""
import re
import subprocess

import pytest
from test_arith_lowering import ABC_OPT, CIRCT_OPT, YOSYS, TESTS, _equiv, _verilog

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


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "2,2,2@2,2,2,3"])
@pytest.mark.parametrize("gate,dup,al", [(1, 0, ""), (2, 0, ""), (99, 0, ""), (2, 3, ""), (0, 3, ""),
                                         (2, 0, "l2-range-aligned=true "), (2, 3, "l2-range-aligned=true "),
                                         (2, 3, "l2-range-compose=true "), (1, 0, "l2-range-compose=true "),
                                         (2, 3, "l2-range-aligned=true l2-range-compose=true ")])
def test_shift_select_l2_range_gate_equivalent(tmp_path, recipes, gate, dup, al):
    """l2-range-gate (level-below-last nodes selected by digit AND coarse range of the last digit,
    one copy per range) and select-dup-fanout (copies of select gates instead of buffers) are exact."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_affine.mlir", "shaff",
               [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                f"l2-range-gate={gate} {al}select-dup-fanout={dup} "
                f"liberty={lib} min-arms=8 min-width=8 wide4-cells=true recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("opt", ["l2-range-gate=1", "l2-range-gate=2", "l2-range-gate=99",
                                 "l2-range-gate=2 select-dup-fanout=3", "select-dup-fanout=3",
                                 "l2-range-gate=2 l2-range-aligned=true",
                                 "l2-range-gate=2 l2-range-compose=true select-dup-fanout=3"])
def test_shift_select_l2_range_gate_is_applied(opt):
    """The equivalence test above would also pass if the pass declined (the netlist is then left
    unchanged): the options must build the network (no decline) and change it."""
    import subprocess
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    def run(extra):
        return subprocess.run([ABC_OPT, "--map-shift-select=enable-shift-select=true chains=true "
                               f"direct-decode=true {extra} liberty={lib} min-arms=8 min-width=8 "
                               "wide4-cells=true recipes=4,2@4,2,3",
                               str(TESTS / "map-shift-select" / "shift_affine.mlir")],
                              capture_output=True, text=True)
    r, base = run(opt), run("")
    assert "0 declined" in r.stderr and "1 one-hot select(s)" in r.stderr, r.stderr
    assert r.stdout != base.stdout


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


# ---------------------------------------------------------------- window form
# Generated shifters of several widths and amount sets: a word
# `root` (optionally a conditionally negated magnitude, the ripple-negate shape) shifted by
# a per-arm amount and sign-extended or zero-filled; the amount is a slice of an input plus
# a constant (wrapping) or a raw slice; the arms are a priority chain with a default arm
# (`chain`) or a one-hot OR whose unmatched amounts give 0 (`onehot`, incomplete).

def _gen_shifter(name, xw, W, aw, arms, form="chain", default=None, neg=False,
                 sign_fill=True, add=0):
    """arms: list of (amount constant, shift); default: shift of the default arm (chain)."""
    L = []
    e = L.append
    N = xw + 1 if neg else xw
    e(f"hw.module @{name}(in %a : i{aw + 2}, in %x : i{xw}, in %sg : i1, out y : i{W}) {{")
    e(f"  %lo = comb.extract %a from 1 : (i{aw + 2}) -> i{aw}")
    if add:
        e(f"  %cm = hw.constant {add} : i{aw}")
        e(f"  %amt = comb.add %lo, %cm : i{aw}")
    else:
        e(f"  %amt = comb.or %lo, %lo : i{aw}")
    if neg:
        e("  %f0 = hw.constant false")
        e(f"  %xz = comb.concat %f0, %x : i1, i{xw}")
        e(f"  %z0 = hw.constant 0 : i{N}")
        e(f"  %ng = comb.sub %z0, %xz : i{N}")
        e(f"  %root = comb.mux %sg, %ng, %xz : i{N}")
    else:
        e(f"  %root = comb.or %x, %x : i{N}")
    e(f"  %sb = comb.extract %root from {N - 1} : (i{N}) -> i1")
    cnt = [0]

    def word(s):
        pieces = []  # (kind, lo, n) from MSB down
        for j in range(W - 1, -1, -1):
            i = j - s
            k = ("z", 0) if i < 0 else (("s", 0) if sign_fill else ("z", 0)) if i >= N else ("r", i)
            if pieces and pieces[-1][0] == k[0] and (k[0] != "r" or pieces[-1][1] == i + 1):
                pieces[-1] = (k[0], k[1] if k[0] != "r" else i, pieces[-1][2] + 1)
            else:
                pieces.append((k[0], k[1], 1))
        vals, tys = [], []
        for kind, lo, n in pieces:
            cnt[0] += 1
            v = f"%p{cnt[0]}"
            if kind == "z":
                e(f"  {v} = hw.constant 0 : i{n}")
            elif kind == "s":
                e(f"  {v} = comb.replicate %sb : (i1) -> i{n}" if n > 1 else
                  f"  {v} = comb.extract %root from {N - 1} : (i{N}) -> i1")
            else:
                e(f"  {v} = comb.extract %root from {lo} : (i{N}) -> i{n}")
            vals.append(v)
            tys.append(f"i{n}")
        if len(vals) == 1:
            return vals[0]
        cnt[0] += 1
        e(f"  %w{cnt[0]} = comb.concat {', '.join(vals)} : {', '.join(tys)}")
        return f"%w{cnt[0]}"

    sels = []
    for k, (c, s) in enumerate(arms):
        e(f"  %k{k} = hw.constant {c} : i{aw}")
        e(f"  %s{k} = comb.icmp ceq %amt, %k{k} : i{aw}")
        sels.append((f"%s{k}", word(s)))
    if form == "chain":
        cur = word(default)
        for k in range(len(sels) - 1, -1, -1):
            e(f"  %c{k} = comb.mux {sels[k][0]}, {sels[k][1]}, {cur} : i{W}")
            cur = f"%c{k}"
        e(f"  hw.output {cur} : i{W}")
    else:
        e(f"  %zw = hw.constant 0 : i{W}")
        ms = []
        for k, (s, w) in enumerate(sels):
            e(f"  %m{k} = comb.mux {s}, {w}, %zw : i{W}")
            ms.append(f"%m{k}")
        e(f"  %y = comb.or {', '.join(ms)} : i{W}")
        e(f"  hw.output %y : i{W}")
    e("}")
    return "\n".join(L) + "\n"


# (name, generator kwargs, recipes)
_WIN_DESIGNS = {
    # 4-bit raw amount, 11 arms (shift = amount + 1) + an unshifted default, zero fill, 17 bits
    "raw11": (dict(xw=5, W=17, aw=4, arms=[(c, c + 1) for c in range(11)], default=0,
                   sign_fill=False), "2,2@2,2,3"),
    # 6-bit raw amount, 40 arms, one-hot (amounts 40..63 give 0), zero fill
    "oh40": (dict(xw=8, W=36, aw=6, arms=[(c, c - 6) for c in range(40)], form="onehot",
                  sign_fill=False), "4,2@4,2,3;2,2@2,2,3"),
    # 4-bit amount, 14 arms with a gap and a non-affine pair, chain, sign fill
    "gap14": (dict(xw=7, W=22, aw=4, arms=[(c, c + (2 if c > 6 else 0)) for c in range(13)]
                   + [(15, 1)], default=0), "4@4,3;2,2@2,2,3"),
    # 4-bit raw amount, 14 arms, one-hot with negate (no constant added to the amount)
    "oh14n": (dict(xw=6, W=24, aw=4, arms=[(c, c) for c in range(14)], form="onehot",
                   neg=True), "2,2@2,2,3;4@4,3"),
}


def _win_src(tmp_path, key):
    kw, recipes = _WIN_DESIGNS[key]
    src = tmp_path / f"{key}.mlir"
    src.write_text(_gen_shifter(f"w{key}", **kw))
    return src, recipes


_WIN_OPTS = ["", "window-count=1", "window-count=2", "window-count=5", "window-clip=true",
             "window-copy-cost=0", "window-copy-cost=400 window-clip=true",
             "window-count=2 select-dup-fanout=3", "window-per-node=true window-copy-cost=200",
             "window-count=5 window-per-node=true window-clip=true", "window-cuts=6",
             "window-general=true", "window-general=true window-types-max=1",
             "window-general=true window-types=1:0-1;0:2-3",
             "window-general=true window-types=1:0-1;0:2-3 window-direct=45",
             "window-general=true window-direct=60", "window-general=true window-direct=150 select-dup-fanout=3"]


@pytest.mark.parametrize("key", sorted(_WIN_DESIGNS))
@pytest.mark.parametrize("opt", _WIN_OPTS)
def test_shift_select_window_form_equivalent(tmp_path, key, opt):
    """window-form (level before the last copied per window of amounts, gated by digit AND
    window; last level selects inside the window) is exact on generated shifters of several
    widths, amount sets, fills and arm forms (incomplete one-hot included)."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    src, recipes = _win_src(tmp_path, key)
    try:
        r = _equiv(tmp_path, src, f"w{key}",
                   [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                    f"window-form=true {opt} liberty={lib} min-arms=8 min-width=8 wide4-cells=true "
                    f"recipes={recipes}"], lib=lib)
    except subprocess.CalledProcessError as e:
        # explicit types / cuts that do not fit this shifter (blocks, amount range): the requested form is
        # refused, never silently left to the logic mapper
        assert ("window-types=" in opt or "window-cuts=" in opt) and \
            "window form requested but not applied: window-" in e.stderr, e.stderr
        return
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("recipes", ["4,2@4,2,3", "2,2@2,2,3", "4@4,3", "2,2,2@2,2,2,3"])
@pytest.mark.parametrize("opt", ["", "window-count=2", "window-clip=true select-dup-fanout=3"])
def test_shift_select_window_form_affine_equivalent(tmp_path, recipes, opt):
    """window-form on the lit test's wrapping slice + constant amount, every recipe shape."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    r = _equiv(tmp_path, TESTS / "map-shift-select" / "shift_affine.mlir", "shaff",
               [f"--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
                f"window-form=true {opt} liberty={lib} min-arms=8 min-width=8 wide4-cells=true "
                f"recipes={recipes}"], lib=lib)
    assert r.returncode == 0, r.stdout + r.stderr


def test_shift_select_window_form_negative_control(tmp_path):
    """The generated miter is not vacuous: a source whose one arm is shifted by one more
    than the rebuilt one is NOT equivalent."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    kw, recipes = _WIN_DESIGNS["oh40"]
    src = tmp_path / "a.mlir"
    src.write_text(_gen_shifter("woh40", **kw))
    after = tmp_path / "after.mlir"
    after.write_text(subprocess.run(
        [ABC_OPT, f"--map-shift-select=enable-shift-select=true direct-decode=true window-form=true "
                  f"liberty={lib} min-arms=8 min-width=8 wide4-cells=true recipes={recipes}",
         str(src)], check=True, capture_output=True, text=True).stdout)
    kw2 = dict(kw, arms=[(c, s + (1 if c == 17 else 0)) for c, s in kw["arms"]])
    other = tmp_path / "o.mlir"
    other.write_text(_gen_shifter("woh40", **kw2))
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text(_verilog(other))
    b.write_text(re.sub(r"(?m)^module (\w+)\(", r"module \1_new(", _verilog(after)))
    r = subprocess.run([YOSYS, "-q", "-p", f"read_liberty -ignore_miss_func {lib}; read_verilog -sv "
                        f"{a} {b}; proc; miter -equiv -flatten -make_assert woh40 woh40_new miter; "
                        "sat -verify -prove-asserts miter"], capture_output=True, text=True)
    assert r.returncode != 0


@pytest.mark.parametrize("key", sorted(_WIN_DESIGNS))
@pytest.mark.parametrize("opt", ["", "window-count=2", "window-clip=true", "window-general=true",
                                 "window-general=true window-direct=45"])
def test_shift_select_window_form_is_applied(tmp_path, key, opt):
    """The miters above would pass vacuously if the pass declined: the window form must be
    built (window report, no decline) and differ from the recipe network."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    src, recipes = _win_src(tmp_path, key)

    def run(extra):
        return subprocess.run([ABC_OPT, "--map-shift-select=enable-shift-select=true chains=true "
                               f"direct-decode=true {extra} liberty={lib} min-arms=8 min-width=8 "
                               f"wide4-cells=true recipes={recipes}", str(src)],
                              capture_output=True, text=True)
    r, base = run("window-form=true " + opt), run("")
    assert r.returncode == 0, r.stderr
    assert "0 declined" in r.stderr and "1 one-hot select(s)" in r.stderr, r.stderr
    assert "window form: windows" in r.stderr or "window form (general): types" in r.stderr, r.stderr
    assert r.stdout != base.stdout


def test_shift_select_window_form_default_unchanged():
    """window-form=false (the default) gives the recipe network byte for byte."""
    lib = TESTS / "map-shift-select" / "shiftrec.lib"
    base = ("--map-shift-select=enable-shift-select=true chains=true direct-decode=true "
            f"liberty={lib} min-arms=8 min-width=8 wide4-cells=true recipes=4,2@4,2,3")
    src = str(TESTS / "map-shift-select" / "shift_affine.mlir")
    a = subprocess.run([ABC_OPT, base, src], capture_output=True, text=True).stdout
    b = subprocess.run([ABC_OPT, base + " window-form=false window-count=5 window-clip=true", src],
                       capture_output=True, text=True).stdout
    assert a == b and a
