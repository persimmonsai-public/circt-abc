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
      l2_range_gate: 3           # gate level-2 nodes with the coarse range of the last digit
      l2_range_aligned: true     # ranges are aligned blocks of N digits (fewer distinct selects)
      select_dup_fanout: 5       # at most N loads per select gate; copies instead of buffers
      form: window               # window form (default: recipe)
      window_count: 6            # window form: number of windows (default: chosen by cost)
      window_copy_cost: 120      # window form: cost of a node copy, percent of one active load
      window_clip: true          # window form: a copy's select covers only the amounts using it
      window_per_node: true      # window form: a node may take one copy over the whole range
      window_cuts: "6,14"        # window form: explicit cuts instead of the search
      window_general: true       # window form: overlapping windows, own digit phase, greedy cover
      window_types: "0:0-2;1:3-5"  # general: explicit types phase:first-last (blocks)
      window_types_max: 3        # general: most types the search combines (1..3)
      window_direct: 45          # general: cost of a lower node taken directly (unset: off)

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

`l2_range_gate: N` (needs `direct_decode`, a recipe of two or more digits) builds the form of a
reference shifter: a node of the level below the last one is selected by "digit AND coarse range
of the last digit", so it is quiet unless its range is the active one. The last digits that consume
a node are split into ranges of N consecutive values and each range gets its own copy of the node
(N = 1: one copy per consumer and no last-level select, the joint decode; a large N keeps every node
shared and only gates the nodes not used for every digit). Exact (no design assumption).

`l2_range_aligned: true` makes the range of a node the whole aligned block of N last-digit values that
holds a consumer (shared range selects; nodes at the word ends are active for a few unused digits).

`l2_range_compose: true` builds each gated select as an AND of the digit select and the range select
(both shared with the other selects of that digit set or range) instead of one network per select.

`select_dup_fanout: N` (needs `direct_decode`) caps the loads of a digit-select gate at N by
instantiating a further copy of the gate (same inputs) instead of letting the fanout stage buffer it.

`form: window` (needs `direct_decode`, a recipe of two or more digits) builds the window form:
the levels below the one before the last stay shared and ungated; each node of the level before
the last is built once per window (a contiguous range of shift amounts holding an amount that uses
it), selected by "digit AND window" so it is quiet outside its window; the last level only has to
tell the last digits inside a copy's window apart (a pass-through when the window lies inside one
last digit, else an exact digit select or an active-low range select, whichever is cheaper). The
windows are a partition of the amount range chosen by a small search (cost: copies against the
activity of their outputs; `window_count` fixes their number, `window_copy_cost` the price of a
copy). Each level's selects are built in the polarity of the data they gate, so levels alternate
AOI / OAI without inverters. Exact (no design assumption).

The environment variables keep working and win over the config key when set:
CIRCT_FLOW_SHIFT_SELECT_EARLY=1 (early position), CIRCT_FLOW_SHIFT_SELECT=1
(legacy position), CIRCT_FLOW_SHIFT_SELECT_{MIN_ARMS,RADIX,WIDE,RECIPES,MAXFO}
CIRCT_FLOW_SHIFT_SELECT_{L2GATE,DUPFO}=N and CIRCT_FLOW_SHIFT_SELECT_{DIRECT,DONTCARE}=1.
"""
from __future__ import annotations

from typing import Optional

KEYS = {"enable", "early", "min_arms", "radix", "wide", "recipes",
        "direct_decode", "default_dont_care", "select_max_fanout",
        "l2_range_gate", "select_dup_fanout", "l2_range_aligned",
        "l2_range_compose", "form", "window_count", "window_copy_cost",
        "window_clip", "window_per_node", "window_cuts", "window_general",
        "window_types", "window_types_max", "window_direct"}


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
    for k in ("enable", "early", "direct_decode", "default_dont_care",
              "l2_range_aligned", "l2_range_compose", "window_clip",
              "window_per_node", "window_general"):
        if k in d and not isinstance(d[k], bool):
            raise KeyError(f"{where}: shift_select.{k} must be true or false")
    for k in ("min_arms", "radix", "select_max_fanout", "l2_range_gate",
              "select_dup_fanout"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int)
                       or d[k] < 1):
            raise KeyError(f"{where}: shift_select.{k} must be an integer >= 1")
    if "window_cuts" in d and not (isinstance(d["window_cuts"], str) and all(
            x.strip().isdigit() for x in d["window_cuts"].split(","))):
        raise KeyError(f"{where}: shift_select.window_cuts must be a string of "
                       f"comma-separated integers")
    if "form" in d and d["form"] not in ("recipe", "window"):
        raise KeyError(f"{where}: shift_select.form must be recipe or window")
    if "window_types" in d and not isinstance(d["window_types"], str):
        raise KeyError(f"{where}: shift_select.window_types must be a string")
    for k in ("window_types_max", "window_direct"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int) or d[k] < 1):
            raise KeyError(f"{where}: shift_select.{k} must be an integer >= 1")
    if any(k in d for k in ("window_types", "window_types_max", "window_direct")) \
            and not d.get("window_general"):
        raise KeyError(f"{where}: shift_select.window_types/_max/direct need "
                       f"shift_select.window_general: true")
    for k in ("window_count", "window_copy_cost"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int)
                       or d[k] < (1 if k == "window_count" else 0)):
            raise KeyError(f"{where}: shift_select.{k} must be an integer >= "
                           f"{1 if k == 'window_count' else 0}")
    if any(k in d for k in ("window_count", "window_copy_cost", "window_clip",
                            "window_per_node", "window_cuts", "window_general")) \
            and d.get("form") != "window":
        raise KeyError(f"{where}: shift_select.window_* needs shift_select.form: window")
    if "wide" in d and d["wide"] not in (0, 1, 2) or \
            isinstance(d.get("wide"), bool):
        raise KeyError(f"{where}: shift_select.wide must be 0, 1 or 2")
    if d.get("form") == "window" and not d.get("direct_decode"):
        raise KeyError(f"{where}: shift_select.form: window needs "
                       f"shift_select.direct_decode: true")
    for k in ("default_dont_care", "select_max_fanout", "l2_range_gate",
              "select_dup_fanout", "l2_range_aligned", "l2_range_compose"):
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


def pass_options_from(settings, early: bool) -> Optional[str]:
    """The `--map-shift-select=...` option text for the early (`early=True`)
    or the legacy position from the resolved options (syn.shifters.*), or
    None when the pass is not requested there."""
    S = settings
    if not S["syn.shifters.early" if early else "syn.shifters.late"]:
        return None
    wide = S["syn.shifters.wide"]
    recipes = S["syn.shifters.recipes"] or ""
    mf = S["syn.shifters.select_max_fanout"]
    g = S["syn.shifters.l2_range_gate"]
    df = S["syn.shifters.select_dup_fanout"]
    return ("enable-shift-select=true " + ("chains=true " if early else "")
            + "liberty={lib} "
            + f"min-arms={S['syn.shifters.min_arms']} "
            + f"radix={S['syn.shifters.radix']}"
            + (" wide-cells=true" if wide == "1" else
               " wide4-cells=true" if wide == "2" else "")
            + (f" recipes={recipes}" if recipes else "")
            + (" direct-decode=true" if S["syn.shifters.direct_decode"] else "")
            + (" default-dont-care=true"
               if S["syn.shifters.default_dont_care"] else "")
            + (f" select-max-fanout={mf}" if mf is not None else "")
            + (f" l2-range-gate={g}" if g is not None else "")
            + (" l2-range-compose=true"
               if S["syn.shifters.l2_range_compose"] else "")
            + (" l2-range-aligned=true"
               if S["syn.shifters.l2_range_aligned"] else "")
            + (f" select-dup-fanout={df}" if df is not None else "")
            + _window_options(S))


def _window_options(S) -> str:
    """The window-form pass options; empty (the command line unchanged) for
    the default form."""
    if S["syn.shifters.form"] != "window":
        return ""
    wc, cc = S["syn.shifters.window_count"], S["syn.shifters.window_copy_cost"]
    return (" window-form=true"
            + (f" window-count={wc}" if wc is not None else "")
            + (f" window-copy-cost={cc}" if cc is not None else "")
            + (" window-clip=true" if S["syn.shifters.window_clip"] else "")
            + (" window-per-node=true" if S["syn.shifters.window_per_node"] else "")
            + (f" window-cuts={S['syn.shifters.window_cuts']}"
               if S["syn.shifters.window_cuts"] else "")
            + (" window-general=true" if S["syn.shifters.window_general"] else "")
            + (f" window-types={S['syn.shifters.window_types']}"
               if S["syn.shifters.window_types"] else "")
            + (f" window-types-max={S['syn.shifters.window_types_max']}"
               if S["syn.shifters.window_types_max"] is not None else "")
            + (f" window-direct={S['syn.shifters.window_direct']}"
               if S["syn.shifters.window_direct"] is not None else ""))


def pass_options(spec: Optional[dict], early: bool,
                 env=None) -> Optional[str]:
    """pass_options_from for a parsed `shift_select:` spec and a legacy
    environment mapping `env` (the variables win over `spec`, as before the
    option registry); kept for the tests that compare the two forms."""
    from . import options
    names = [n for n in options.registry() if n.startswith("syn.shifters.")]
    rec = options._shifters_from_yaml(
        {k: v for k, v in (spec or {}).items()} or None)
    s = options.resolve(recipe=rec, env=env or {}, names=names, warn=False)
    return pass_options_from(s, early)
