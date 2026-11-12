"""clock_gating: the pack's numbers, the flow's switch, the option text
clock-gate-inference gets; vt_swap's keep_clock_cells."""
import pytest

from circt_flow import clockgating as cg
from circt_flow import vtswap


def test_off_unless_the_flow_opts_in():
    assert cg.resolve({"gate_cost_bits": 4}, None, "x") is None
    assert cg.resolve({"gate_cost_bits": 4}, False, "x") is None


def test_true_takes_the_packs_numbers_and_a_mapping_overrides():
    pack = {"merge_equivalent_enables": True, "gate_cost_bits": 4,
            "min_gate_bits": 3}
    t = cg.resolve(pack, True, "x")
    assert t == ("gate-cost-bits=4.0 merge-equivalent-enables=true "
                 "min-gate-bits=3")
    t = cg.resolve(pack, {"min_gate_bits": 5, "max_gate_depth": 2}, "x")
    assert "min-gate-bits=5" in t and "max-gate-depth=2" in t
    assert "gate-cost-bits=4.0" in t


def test_true_without_a_pack_merges_only():
    assert cg.resolve(None, True, "x") == "merge-equivalent-enables=true"


@pytest.mark.parametrize("bad", [{"bogus": 1}, {"min_gate_bits": -1},
                                 {"min_gate_bits": 1.5},
                                 {"merge_equivalent_enables": 1},
                                 {"gate_cost_bits": True}, [1]])
def test_bad_values_are_refused(bad):
    with pytest.raises(KeyError):
        cg.resolve(None, bad, "x")
    with pytest.raises(KeyError):
        cg.parse_pack(bad, "x")


def test_vt_swap_keep_clock_cells_key():
    assert vtswap.parse_vt_swap({"keep_clock_cells": True}, "x").keep_clock_cells
    assert not vtswap.parse_vt_swap(True, "x").keep_clock_cells
    with pytest.raises(vtswap.VtSwapError):
        vtswap.parse_vt_swap({"keep_clock_cells": 1}, "x")


def test_vt_swap_tcl_skips_clock_cells_and_names_no_cell():
    t = vtswap.TCL
    assert t.count("is_clock_cell $inst") == 3      # fix, recover and pair recovery
    assert "%(keep_clk)d" in t
