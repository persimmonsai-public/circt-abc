"""shift_select: the flow config's switch for the map-shift-select pass.

map-shift-select rebuilds a priority mux chain (or a one-hot select) over
shifted copies of one word as a mixed-radix AND-OR network of library cells
(mapper/lib/StructuralMapper/MapShiftSelectPass.cpp). It is off by default.
The flow config can switch it on without environment variables:

    shift_select:
      early: true                # run it first in the structural stage (default)
      min_arms: 12               # fewest arms of the chain to rewrite
      radix: 4                   # radix of the first level (legacy path)
      wide: 2                    # 0 none, 1 three-term cells, 2 four-term cells
      recipes: "2,2@2,2,3"       # mixed-radix recipes, string or list of strings
      direct_decode: true        # digit selects straight from the raw amount bits
      default_dont_care: true    # PER-DESIGN assumption, see below; needs direct_decode
      select_max_fanout: 8       # leave digit-select nets of up to N loads unbuffered

`shift_select: true` is `early: true` with the defaults. `early: false` runs the
pass in its older position (after decode-mux-chains), which sees fewer arms of a
`case` whose enable was already pushed per bit by clock gating; use it only to
reproduce old results. `enable: false` keeps a block in the config but off.

`direct_decode` builds each digit select as a small cell network of the raw bits of the
amount (found by evaluating the amount expression over its leaf bits) instead of an OR of
per-arm comparators that the logic mapper turns into an adder plus a comparator network;
the digit-boundary offset is chosen by area. Every amount that has a constant arm selects
the same word as before.

`default_dont_care` (needs `direct_decode`) is a DESIGN ASSUMPTION and is off unless the
design's flow config sets it; no generic default, pack or profile may enable it. It treats
the amounts matched by no constant arm (the default arm) as don't cares and continues the
affine amount-to-shift relation of the constant arms through them, which makes the select
networks smaller. It is valid if and only if the shifter's result is never used when the
amount matches no constant arm (it reaches no output and no register that is later
observed). If the design can ever use the default arm's result, the netlist is functionally
wrong for those amounts and no tool here will say so: establish the condition on the RTL
(or by simulation of the whole design) before setting it, and set it for that design only.

`select_max_fanout: N` (needs `direct_decode`) annotates the instance driving each digit
select with `circt_abc.max_fanout = N`, so the fanout stage leaves a net of at most N loads
unbuffered (a per-driver limit, raised to N when N exceeds the stage's global `max_fanout`).

The environment variables keep working and win over the config key when set:
CIRCT_FLOW_SHIFT_SELECT_EARLY=1 (early position), CIRCT_FLOW_SHIFT_SELECT=1
(legacy position), CIRCT_FLOW_SHIFT_SELECT_{MIN_ARMS,RADIX,WIDE,RECIPES,MAXFO}
and CIRCT_FLOW_SHIFT_SELECT_{DIRECT,DONTCARE}=1.
"""
from __future__ import annotations

import os
from typing import Optional

KEYS = {"enable", "early", "min_arms", "radix", "wide", "recipes",
        "direct_decode", "default_dont_care", "select_max_fanout"}


def parse(d, where: str) -> Optional[dict]:
    """The flow config's `shift_select:` value, checked; None when absent/false."""
    if d is None or d is False:
        return None
    if d is True:
        d = {"enable": True}
    if not isinstance(d, dict):
        raise KeyError(f"{where}: shift_select must be true or a mapping")
    bad = set(d) - KEYS
    if bad:
        raise KeyError(f"{where}: shift_select: unknown key(s) "
                       f"{', '.join(sorted(bad))}; allowed: "
                       f"{', '.join(sorted(KEYS))}")
    for k in ("enable", "early", "direct_decode", "default_dont_care"):
        if k in d and not isinstance(d[k], bool):
            raise KeyError(f"{where}: shift_select.{k} must be true or false")
    for k in ("min_arms", "radix", "select_max_fanout"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int)
                       or d[k] < 1):
            raise KeyError(f"{where}: shift_select.{k} must be an integer >= 1")
    if "wide" in d and d["wide"] not in (0, 1, 2) or \
            isinstance(d.get("wide"), bool):
        raise KeyError(f"{where}: shift_select.wide must be 0, 1 or 2")
    for k in ("default_dont_care", "select_max_fanout"):
        if d.get(k) and not d.get("direct_decode"):
            raise KeyError(f"{where}: shift_select.{k} needs "
                           f"shift_select.direct_decode: true")
    r = d.get("recipes")
    if isinstance(r, list):
        if not all(isinstance(x, str) for x in r):
            raise KeyError(f"{where}: shift_select.recipes entries are strings")
        r = ";".join(r)
    if r is not None and not isinstance(r, str):
        raise KeyError(f"{where}: shift_select.recipes must be a string or list")
    d = {"enable": True, **d}
    return {**d, "recipes": r} if r is not None else d


def pass_options(spec: Optional[dict], early: bool,
                 env=os.environ) -> Optional[str]:
    """The `--map-shift-select=...` option text for the early (`early=True`)
    or the legacy position, or None when the pass is not requested there.
    The environment wins over `spec` (see the module docstring)."""
    spec = spec or {}
    on_cfg = bool(spec) and spec.get("enable", True) and \
        spec.get("early", True) == early
    on_env = env.get("CIRCT_FLOW_SHIFT_SELECT_EARLY" if early
                     else "CIRCT_FLOW_SHIFT_SELECT") == "1"
    if not (on_cfg or on_env):
        return None

    def pick(name, key, default):
        v = env.get(f"CIRCT_FLOW_SHIFT_SELECT_{name}")
        if v:
            return v
        return str(spec[key]) if spec.get(key) not in (None, "") else default

    def flag(env_name, key):
        return env.get(f"CIRCT_FLOW_SHIFT_SELECT_{env_name}") == "1" or \
            spec.get(key) is True

    wide = pick("WIDE", "wide", "0")
    recipes = pick("RECIPES", "recipes", "")
    return ("enable-shift-select=true " + ("chains=true " if early else "")
            + "liberty={lib} "
            + f"min-arms={pick('MIN_ARMS', 'min_arms', '12')} "
            + f"radix={pick('RADIX', 'radix', '4')}"
            + (" wide-cells=true" if wide == "1" else
               " wide4-cells=true" if wide == "2" else "")
            + (f" recipes={recipes}" if recipes else "")
            + (" direct-decode=true" if flag("DIRECT", "direct_decode") else "")
            + (" default-dont-care=true"
               if flag("DONTCARE", "default_dont_care") else "")
            + (f" select-max-fanout={mf}"
               if (mf := pick("MAXFO", "select_max_fanout", "")) else ""))
