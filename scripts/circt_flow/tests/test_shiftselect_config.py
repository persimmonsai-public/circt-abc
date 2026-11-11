"""shift_select: the flow config key and the environment variables give the same
map-shift-select option text; the environment wins."""
import pytest

from circt_flow import shiftselect as ss

EARLY = ("enable-shift-select=true chains=true liberty={lib} min-arms=12 "
         "radix=4 wide4-cells=true recipes=3,2@3,2,2")


def test_off_without_key_or_variable():
    assert ss.pass_options(None, True, {}) is None
    assert ss.pass_options(None, False, {}) is None
    assert ss.parse(None, "x") is None and ss.parse(False, "x") is None


def test_true_is_early_with_defaults():
    spec = ss.parse(True, "x")
    assert ss.pass_options(spec, True, {}) == (
        "enable-shift-select=true chains=true liberty={lib} min-arms=12 radix=4")
    assert ss.pass_options(spec, False, {}) is None


def test_config_equals_environment():
    env = {"CIRCT_FLOW_SHIFT_SELECT_EARLY": "1", "CIRCT_FLOW_SHIFT_SELECT_WIDE": "2",
           "CIRCT_FLOW_SHIFT_SELECT_RECIPES": "3,2@3,2,2"}
    spec = ss.parse({"wide": 2, "recipes": ["3,2@3,2,2"]}, "x")
    assert ss.pass_options(spec, True, {}) == EARLY
    assert ss.pass_options(None, True, env) == EARLY


def test_list_of_recipes_is_semicolon_joined():
    spec = ss.parse({"recipes": ["3,2@3,2,2", "2,2@2,2,3"]}, "x")
    assert spec["recipes"] == "3,2@3,2,2;2,2@2,2,3"


def test_environment_wins_and_legacy_position():
    spec = ss.parse({"early": False, "min_arms": 8}, "x")
    assert ss.pass_options(spec, True, {}) is None
    assert ss.pass_options(spec, False, {"CIRCT_FLOW_SHIFT_SELECT_MIN_ARMS": "20"}) \
        == "enable-shift-select=true liberty={lib} min-arms=20 radix=4"
    assert ss.pass_options(ss.parse({"enable": False}, "x"), True, {}) is None


@pytest.mark.parametrize("bad", [{"nope": 1}, {"radix": 0}, {"wide": 3},
                                 {"early": "yes"}, {"recipes": 5}, "x"])
def test_bad_values_are_rejected(bad):
    with pytest.raises(KeyError):
        ss.parse(bad, "x")


DIRECT = ("enable-shift-select=true chains=true liberty={lib} min-arms=12 radix=4 "
          "wide4-cells=true recipes=3,2@3,2,2 direct-decode=true")


def test_direct_decode_options_config_equals_environment():
    spec = ss.parse({"wide": 2, "recipes": "3,2@3,2,2", "direct_decode": True,
                     "default_dont_care": True, "select_max_fanout": 8}, "x")
    want = DIRECT + " default-dont-care=true select-max-fanout=8"
    assert ss.pass_options(spec, True, {}) == want
    env = {"CIRCT_FLOW_SHIFT_SELECT_EARLY": "1", "CIRCT_FLOW_SHIFT_SELECT_WIDE": "2",
           "CIRCT_FLOW_SHIFT_SELECT_RECIPES": "3,2@3,2,2",
           "CIRCT_FLOW_SHIFT_SELECT_DIRECT": "1", "CIRCT_FLOW_SHIFT_SELECT_DONTCARE": "1",
           "CIRCT_FLOW_SHIFT_SELECT_MAXFO": "8"}
    assert ss.pass_options(None, True, env) == want


def test_dont_care_is_off_unless_the_design_sets_it():
    spec = ss.parse({"wide": 2, "recipes": "3,2@3,2,2", "direct_decode": True}, "x")
    assert ss.pass_options(spec, True, {}) == DIRECT
    assert "dont-care" not in (ss.pass_options(ss.parse(True, "x"), True, {}) or "")
    assert "direct-decode" not in (ss.pass_options(ss.parse(True, "x"), True, {}) or "")


@pytest.mark.parametrize("bad", [{"default_dont_care": True},
                                 {"select_max_fanout": 8},
                                 {"direct_decode": True, "select_max_fanout": 0},
                                 {"direct_decode": "yes"}])
def test_direct_decode_dependents_are_checked(bad):
    with pytest.raises(KeyError):
        ss.parse(bad, "x")
