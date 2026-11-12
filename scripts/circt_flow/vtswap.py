"""Threshold-voltage swap on a mapped netlist, driven by OpenSTA: the
flow's vt_swap stage (scripts/mvt/vt_swap.py is its command line).

Cells of one threshold flavour family share area, footprint and pins, so
changing a cell's flavour is renaming its cell type: the function is the
same. ABC chooses flavours on its own delay model, which can be far from
the libraries' (by a factor of two or more on some libraries); the
register mapper and clock gate inference always place the cheapest
(slowest) flavour. This step
corrects both with real timing, in one OpenSTA session:

  1. Fix: while the worst setup slack is below the margin (margin_ps,
     default 0), every instance on the
     worst `paths` failing paths moves one flavour faster (along the
     tokens, slowest first) when that cell exists in the libraries. Stops
     at slack >= 0, at `max_iter`, or when nothing can move.
     Faster cells shorten hold paths too: when an iteration takes the
     worst hold slack below min(0, its value before the swap), its moved
     cells on the failing hold paths go back and are frozen. An iteration
     that then still breaks hold, or does not improve the worst setup
     slack, is undone and ends the loop. Recovery only slows cells, so it
     cannot hurt hold.
  2. Recover (unless recover is off): every instance not at the slowest
     flavour, the ones step 1 moved and the ones ABC chose, steps one
     flavour slower, in batches of 256 that stay if the worst slack holds
     at >= min(margin, the slack after step 1) (and, when step 1 did not
     reach the margin, the total negative slack at its value after step
     1) and are otherwise undone and halved; passes repeat until nothing
     moves. This returns leakage
     wherever a fast cell is not needed.

The flavour regex captures the token in a cell name (default `_([A-Z]+)$`).
It may have several groups for a ladder over more than one axis (Vt and
a second flavour code, say): a token is then the groups' values joined by "/"
("R/A"), the ladder lists them slowest first, and a step moves to the
next listed token whose cell exists in the libraries (a gap is skipped).
The libraries are the REAL per-flavour ones, never a priced mapping
library. The netlist is rewritten with OpenSTA's write_verilog; the
record lists every swap and the slack before and after. OpenSTA runs in
the ORFS image (`sta`), or the binary $OPENSTA names.

In the flow (a flow config's `vt_swap:` section, or --vt-swap) the stage
runs on the OpenSTA netlist after synthesis, before the STA stage, with
the profile's setup-corner libraries for the run's rail (its mapping
liberties when it has no corners, so such a pack must not list a priced
library first), the tightest mode's SDC plus `false_from:` ports, and the
pack's `vt_flavours:`. With a wire model (flow.wire_model, wire_model.py) every
net carries its expected wire in the session, so the fix and recovery steps
size against loaded nets. The unswapped netlist is kept as
<top>_preswap_openroad.v; the record goes to <out>/vt_swap.json.

In a power flow (run_power, after the intent step and before STA) the swap
runs on the CHIP view, as the STA stage builds it: the whole stitched
netlist with every cell renamed after its rail (<rail>__<cell>) and each
rail's setup-corner libraries renamed the same way, so every path,
cross-domain ones and level shifters included, is timed exactly with the
mode's own SDC. (Swapping each domain alone was tried first: its boundary
view misses the shifters and glue on the domain's inputs, so recovery
can leave a cross-domain path short.) The tightest mode
fixes and recovers; each other mode then fixes only. The flavour changes
are written back into the stitched netlist with the rail prefixes
stripped. A module under domains of two rails is not swapped (warned).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

ORFS_IMAGE = "openroad/orfs:26Q3-277-gf9ec54a6d"
STA_BIN = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/sta"
VT_SWAP_KEYS = {"enabled", "false_from", "paths", "max_iter", "recover",
                "margin_ps", "keep_clock_cells", "false_to", "fix",
                "sens_frac", "recover_order", "ladder_cap",
                "allow_footprint_change", "ladder_escalate", "close_margin_ps"}


@dataclass(frozen=True)
class LayoutSpec:
    """How the swap sees a placed netlist: parasitics from `spef`, the
    clocks propagated, the clock network left alone (`protect_clock`), and
    a swap allowed only between cells of equal footprint: the LEF size when
    `sizes` ({cell: (width, height)}) has both cells, else the liberty
    area. `allow_footprint_change` lifts the footprint rule (the swaps are
    then listed, not ECO-clean)."""
    spef: Optional[Path] = None
    propagated: bool = True
    protect_clock: bool = True
    sizes: Optional[dict] = None
    allow_footprint_change: bool = False
    # Time setup and hold in two OpenSTA corners (with `hold_libs`), the
    # parasitics read into both: a propagated clock must not be timed with
    # setup launching on the setup libraries' tree and capturing on the
    # hold libraries' faster one, as one corner with -max/-min libraries does.
    corners: bool = True
    # Which hold paths the swap protects: "r2r" (register to register: the
    # ones a faster cell can break that nothing else repairs; input-port
    # hold is the clock tree's and the constraints' business, and a design
    # that already fails it would otherwise veto every swap) or "all".
    hold_scope: str = "r2r"
    # How far below its start slack (or 0) a hold endpoint may go, in the
    # libraries' time unit.
    hold_tol: float = 0.0
    # Fix by sensitivity ("sens": each candidate cell is tried alone, the
    # best slack gain per leakage added go up, `sens_frac` of the useful
    # ones per iteration) rather than "bulk" (every cell of the failing
    # paths one flavour up, recovery then gives leakage back). `leakage`
    # is {cell: state-averaged leakage}, needed by "sens".
    fix: str = "sens"
    sens_frac: float = 0.25
    leakage: Optional[dict] = None
    # Opt-in leakage controls (defaults leave the swap unchanged):
    # `recover_order` "leak" tries the highest-leakage cells first in each
    # recovery pass (needs `leakage`), so the slack headroom a path has goes
    # to the cells that save most; "inst" keeps netlist order.
    recover_order: str = "inst"
    # `ladder_cap`: a ladder token (e.g. "L") above which the fix step
    # may not move a cell; recovery still moves cells down from above it.
    ladder_cap: str = ""
    # Closing pass: when the fix, held under `ladder_cap`, leaves the worst
    # slack below `close_margin`, the cap is lifted to this token and a
    # sensitivity fix runs once more towards `close_margin` (best slack per
    # leakage added, on the failing paths only). Recovery then takes the
    # swaps that were not needed back, highest leakage first. "" is off.
    ladder_escalate: str = ""
    close_margin: float = 0.0
    # Pair recovery: after recovery, try to take the `recover_swap`
    # highest-leakage cells that could not move down on their own one step
    # down while moving one cell of the worst path up (cheaper in leakage than
    # the saving), keeping the pair only if the worst slack stays at the floor.
    # 0 is off.
    recover_swap: int = 0
    # Second wire view (opt-in): with two corners, a third corner `setup2`
    # (the setup libraries, this SPEF) is timed too, so the worst slack the
    # fix and recovery see is the worst over both parasitic views.
    spef2: Optional[Path] = None

    def pre_tcl(self, hold_libs: bool = False) -> str:
        spef = Path(self.spef).resolve() if self.spef else None
        if spef and self.corners and hold_libs:
            rs = (f"read_spef -corner setup {spef}\n"
                  f"read_spef -corner hold {spef}\n")
            if self.spef2:
                rs += f"read_spef -corner setup2 {Path(self.spef2).resolve()}\n"
        else:
            rs = f"read_spef {spef}\n" if spef else ""
        return ("set_propagated_clock [all_clocks]\n" if self.propagated
                else "") + rs

    def hooks_tcl(self) -> str:
        sizes = " ".join("{%s} {%g %g}" % (c, w, h)
                         for c, (w, h) in sorted((self.sizes or {}).items()))
        return r'''
if {"%(hold_scope)s" eq "r2r"} {
  set hold_ft [list -from [all_registers -clock_pins] -to [all_registers -data_pins]]
  proc whs {} {
    global hold_ft
    set pe [lindex [find_timing_paths -path_delay min {*}$hold_ft -group_path_count 1 -sort_by_slack] 0]
    if {$pe eq ""} { return 1e30 }
    return [sta::time_sta_ui [$pe slack]]
  }
}
# Per-endpoint hold floor: an endpoint may not end below min(0, its slack
# at the start) - tol; a violator the start did not have counts from 0. One
# endpoint already at its floor then stops only the swaps on its own paths,
# not every swap in the design.
set hold_tol %(hold_tol)s
# rounds of undoing the moves on newly failing hold paths before an
# iteration gives up (each round can only shrink the moved set)
set hold_rounds 6
set hold_start [dict create]
proc hold_begin {} {
  global hold_start hold_ft
  foreach pe [find_timing_paths -path_delay min {*}$hold_ft -slack_max 0 \
                -group_path_count 1000000 -endpoint_path_count 1 \
                -unique_paths_to_endpoint] {
    dict set hold_start [get_full_name [$pe pin]] [sta::time_sta_ui [$pe slack]]
  }
}
proc hold_bad_paths {} {
  global hold_start hold_ft hold_tol
  set l {}
  foreach pe [find_timing_paths -path_delay min {*}$hold_ft -slack_max 0 \
                -group_path_count 1000000 -endpoint_path_count 1 \
                -unique_paths_to_endpoint] {
    set s [sta::time_sta_ui [$pe slack]]
    set n [get_full_name [$pe pin]]
    set fl [expr {[dict exists $hold_start $n] ? [dict get $hold_start $n] : 0.0}]
    if {$s < $fl - $hold_tol - 1e-6} { lappend l $pe }
  }
  return $l
}
proc hold_bad {} { return [expr {[llength [hold_bad_paths]] > 0}] }
# Sensitivity fix: try each movable cell of the failing paths one flavour
# up on its own, rank by slack gained (total negative slack plus worst
# slack) per leakage added, and keep the best `frac` of those that gain.
set leak_tab [dict create %(leaks)s]
set recover_leak %(rleak)d
set ladder_cap_idx [lsearch -exact $flavours {%(cap)s}]
if {"%(cap)s" eq ""} { set ladder_cap_idx -1 }
set escalate_idx [lsearch -exact $flavours {%(esc)s}]
if {"%(esc)s" eq ""} { set escalate_idx -1 }
set close_margin %(close)s
set recover_swap %(rswap)d
proc leak_of {ref} {
  global leak_tab
  if {[dict exists $leak_tab $ref]} { return [dict get $leak_tab $ref] }
  return 0.0
}
if {%(sens)d || "%(esc)s" ne ""} {
  if {%(sens)d} { set bulk_iters 0 }
  proc sens_fix {max_iter paths margin} {
    global rec frozen hold_rounds
    set frac %(frac)s
    for {set it 0} {$it < $max_iter} {incr it} {
      set w [wns]
      if {$w >= $margin} { break }
      set t0 [tns]
      set seen [dict create]
      foreach pe [find_timing_paths -path_delay max -group_path_count $paths \
                    -endpoint_path_count 1 -slack_max $margin -sort_by_slack] {
        foreach pin [[$pe path] pins] {
          set inst [$pin instance]
          if {$inst eq "" || $inst eq "NULL"} { continue }
          if {[$inst liberty_cell] eq "NULL"} { continue }
          if {[is_clock_cell $inst]} { continue }
          dict set seen [get_full_name $inst] $inst
        }
      }
      set scored {}
      dict for {name inst} $seen {
        if {[dict exists $frozen $name]} { continue }
        set from [get_property $inst ref_name]
        set to [move $inst 1]
        if {$to eq ""} { continue }
        set t1 [tns]
        set w1 [wns]
        replace_cell $inst [lib_cell_of $from]
        set gain [expr {($t1 - $t0) + ($w1 - $w)}]
        if {$gain <= 1e-6} { continue }
        set dl [expr {[leak_of $to] - [leak_of $from]}]
        if {$dl < 1e-9} { set dl 1e-9 }
        lappend scored [list [expr {$gain / $dl}] $inst $name $from $to]
      }
      if {[llength $scored] == 0} {
        puts $rec "iter $it [format %%.6f $w] moved 0"
        break
      }
      set scored [lsort -real -decreasing -index 0 $scored]
      set k [expr {int(ceil($frac * [llength $scored]))}]
      if {$k < 5} { set k 5 }   ;# a few moves per iteration even on a short list
      set moved {}
      foreach s [lrange $scored 0 [expr {$k - 1}]] {
        lassign $s r inst name from to
        move $inst 1
        lappend moved [list $inst $name $from $to]
      }
      for {set round 0} {$round < $hold_rounds && [llength $moved] > 0 &&
                         [hold_bad]} {incr round} {
        set onhold [dict create]
        foreach pe [hold_bad_paths] {
          foreach pin [[$pe path] pins] {
            set inst [$pin instance]
            if {$inst eq "" || $inst eq "NULL"} { continue }
            dict set onhold [get_full_name $inst] 1
          }
        }
        set kept {}
        foreach m $moved {
          if {[dict exists $onhold [lindex $m 1]]} {
            replace_cell [lindex $m 0] [lib_cell_of [lindex $m 2]]
            dict set frozen [lindex $m 1] 1
            puts $rec "hold $it [lindex $m 1] [lindex $m 2] [lindex $m 3]"
          } else { lappend kept $m }
        }
        set moved $kept
      }
      set why ""
      if {[llength $moved] > 0 && [hold_bad]} { set why hold }
      if {[llength $moved] > 0 && $why eq "" && [wns] <= $w + 1e-6 &&
          [tns] <= $t0 + 1e-6} { set why setup }
      if {$why ne ""} {
        foreach m $moved { replace_cell [lindex $m 0] [lib_cell_of [lindex $m 2]] }
        puts $rec "iter $it [format %%.6f $w] moved 0 undone [llength $moved] $why"
        break
      }
      foreach m $moved { puts $rec "up $it [lindex $m 1] [lindex $m 2] [lindex $m 3]" }
      puts $rec "iter $it [format %%.6f $w] moved [llength $moved]"
      if {[llength $moved] == 0} { break }
    }
  }
}
array set clk_cells {}
if {%(clk)d} {
  foreach i [get_cells -hierarchical *] {
    foreach p [get_pins -of_objects $i] {
      if {[get_property $p direction] eq "output" && [sta::is_clock $p]} {
        set clk_cells([get_full_name $i]) 1
        break
      }
    }
  }
}
set eco_stat(clock_cells) [array size clk_cells]
array set fp_size [list %(sizes)s]
set fp_free %(free)d
proc eco_count {k} {
  global eco_stat
  if {![info exists eco_stat($k)]} { set eco_stat($k) 0 }
  incr eco_stat($k)
}
proc eco_ok {inst from to} {
  global clk_cells fp_size fp_free
  if {[info exists clk_cells([get_full_name $inst])]} {
    eco_count blocked_clock
    return 0
  }
  if {$fp_free} { return 1 }
  if {[info exists fp_size($from)] && [info exists fp_size($to)]} {
    if {$fp_size($from) eq $fp_size($to)} { return 1 }
    eco_count blocked_footprint
    return 0
  }
  if {[array size fp_size] > 0} { eco_count blocked_no_lef; return 0 }
  set a [get_property [lib_cell_of $from] area]
  set b [get_property [lib_cell_of $to] area]
  if {abs($a - $b) < 1e-9} { return 1 }
  eco_count blocked_footprint
  return 0
}
''' % {"clk": int(self.protect_clock), "sizes": sizes,
        "free": int(self.allow_footprint_change),
        "hold_scope": self.hold_scope,
        "hold_tol": "%g" % self.hold_tol,
        "sens": int(self.fix == "sens"), "frac": "%g" % self.sens_frac,
        "rleak": int(self.recover_order == "leak"), "cap": self.ladder_cap,
        "esc": self.ladder_escalate, "close": "%g" % self.close_margin,
        "rswap": int(self.recover_swap),
        "leaks": " ".join("{%s} %.9g" % (c, v) for c, v in
                          sorted((self.leakage or {}).items()))
        if (self.fix == "sens" or self.recover_order == "leak"
            or self.ladder_escalate or self.recover_swap) else ""}


class VtSwapError(RuntimeError):
    """A swap that cannot run, or an OpenSTA failure."""


@dataclass(frozen=True)
class VtSwapSpec:
    enabled: bool = True
    false_from: tuple = ()
    paths: int = 200
    max_iter: int = 30
    recover: bool = True
    # Setup slack to keep in hand (ps): fix until the worst slack is at
    # least this, recover only while it stays there; room for P&R.
    margin_ps: float = 0.0
    # Leave clock cells (any cell other than a buffer or inverter that drives
    # a clock net, i.e. the clock gates) at the flavour synthesis gave them:
    # a faster flavour only adds clock-network leakage and internal power,
    # and a gate's flavour does not set the data path's timing.
    keep_clock_cells: bool = False
    # Endpoints the swap ignores: port names, or "outputs" for every output
    # port (set_false_path -to). A design whose output paths cannot close in
    # this view (output delay beyond what the flop and its driver can do)
    # otherwise has the fix spend the fastest flavours on them without ever
    # reaching the margin, and recovery then starts from that.
    false_to: tuple = ()
    # Opt-in: fix by sensitivity (best slack per leakage added), recover the
    # highest-leakage cells first, and stop the fix at a ladder token. "bulk"
    # with "inst" order and no cap is the default.
    fix: str = "bulk"
    sens_frac: float = 0.25
    recover_order: str = "inst"
    ladder_cap: str = ""
    # A ladder whose steps change a cell's area: without
    # this the swap vetoes every move to a different area.
    allow_footprint_change: bool = False
    # Closing pass (LayoutSpec.ladder_escalate): lift the cap to this token
    # for what the capped fix left short of `close_margin_ps`.
    ladder_escalate: str = ""
    close_margin_ps: float = 0.0


def false_to_sdc(false_to) -> str:
    return "".join("set_false_path -to [all_outputs]\n" if p == "outputs"
                   else f"set_false_path -to [get_ports {p}]\n"
                   for p in false_to)


def check_leakage_keys(d: dict, where: str) -> None:
    """The leakage controls shared by `vt_swap:` and `pnr.post_layout:`
    (`where` names the section), checked when the config is read."""
    if d.get("fix", "bulk") not in ("sens", "bulk"):
        raise VtSwapError(f"{where}.fix must be sens or bulk")
    sf = d.get("sens_frac", 0.25)
    if isinstance(sf, bool) or not isinstance(sf, (int, float)) \
            or not 0 < sf <= 1:
        raise VtSwapError(f"{where}.sens_frac must be a number in (0, 1]")
    if d.get("recover_order", "inst") not in ("inst", "leak"):
        raise VtSwapError(f"{where}.recover_order must be inst or leak")
    for k in ("ladder_cap", "ladder_escalate"):
        if not isinstance(d.get(k, ""), str):
            raise VtSwapError(f"{where}.{k} must be a ladder token")
    cm = d.get("close_margin_ps", 0.0)
    if isinstance(cm, bool) or not isinstance(cm, (int, float)) or cm < 0 \
            or cm != cm or cm == float("inf"):
        raise VtSwapError(f"{where}.close_margin_ps must be a number of ps "
                          ">= 0")
    if "close_margin_ps" in d and not d.get("ladder_escalate"):
        raise VtSwapError(f"{where}.close_margin_ps needs ladder_escalate "
                          "in the same section (it is the closing pass's "
                          "target)")
    if "allow_footprint_change" in d and \
            not isinstance(d["allow_footprint_change"], bool):
        raise VtSwapError(f"{where}.allow_footprint_change must be true or "
                          "false")


def check_ladder_tokens(cap: str, esc: str, tokens, where: str) -> None:
    """`ladder_cap` / `ladder_escalate` name tokens of the pack's ladder."""
    for k, v in (("ladder_cap", cap), ("ladder_escalate", esc)):
        if v and v not in tokens:
            raise VtSwapError(f"{where}.{k} {v!r} is not a token of the "
                              f"pack's vt_flavours ({', '.join(tokens)})")


def parse_vt_swap(d, where) -> VtSwapSpec:
    """A flow config's `vt_swap:` section (true/false, or the keys)."""
    if d is None or d is True:
        return VtSwapSpec()
    if d is False:
        return VtSwapSpec(enabled=False)
    if not isinstance(d, dict) or set(d) - VT_SWAP_KEYS:
        bad = sorted(set(d) - VT_SWAP_KEYS) if isinstance(d, dict) else ["?"]
        raise VtSwapError(f"{where}: vt_swap.{bad[0]} is not a key; allowed: "
                          + ", ".join(sorted(VT_SWAP_KEYS)))
    ff = d.get("false_from") or []
    if isinstance(ff, str) or not all(isinstance(x, str) for x in ff):
        raise VtSwapError(f"{where}: vt_swap.false_from must be a list of "
                          "port names")
    for k in ("paths", "max_iter"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int)
                       or d[k] < 1):
            raise VtSwapError(f"{where}: vt_swap.{k} must be a positive "
                              "integer")
    ft = d.get("false_to") or []
    if isinstance(ft, str) or not all(isinstance(x, str) for x in ft):
        raise VtSwapError(f"{where}: vt_swap.false_to must be a list of "
                          "port names (or \"outputs\")")
    for k in ("enabled", "recover", "keep_clock_cells"):
        if k in d and not isinstance(d[k], bool):
            raise VtSwapError(f"{where}: vt_swap.{k} must be true or false")
    mg = d.get("margin_ps", 0.0)
    if isinstance(mg, bool) or not isinstance(mg, (int, float)) or mg < 0 \
            or mg != mg or mg == float("inf"):
        raise VtSwapError(f"{where}: vt_swap.margin_ps must be a number of "
                          "ps >= 0")
    check_leakage_keys(d, f"{where}: vt_swap")
    return VtSwapSpec(d.get("enabled", True), tuple(ff), d.get("paths", 200),
                      d.get("max_iter", 30), d.get("recover", True),
                      float(mg), d.get("keep_clock_cells", False), tuple(ft),
                      d.get("fix", "bulk"), float(d.get("sens_frac", 0.25)),
                      d.get("recover_order", "inst"), d.get("ladder_cap", ""),
                      bool(d.get("allow_footprint_change", False)),
                      d.get("ladder_escalate", ""),
                      float(d.get("close_margin_ps", 0.0)))


def parse_flavours(d, where) -> Optional[dict]:
    """A pack's `vt_flavours: {tokens: [slowest, ..., fastest], re: ...}`.
    The regex has one group (the token) or several (two axes, say Vt and
    a second flavour code): a token then lists one value per group, joined by
    "/" ("R/A"), and the tokens are the ladder in speed order."""
    if d is None:
        return None
    if not isinstance(d, dict) or set(d) - {"tokens", "re"}:
        raise KeyError(f"{where}: vt_flavours takes tokens and re")
    toks = d.get("tokens")
    if not isinstance(toks, list) or len(toks) < 2 or \
            not all(isinstance(t, str) and t for t in toks):
        raise KeyError(f"{where}: vt_flavours.tokens must list two or more "
                       "flavour tokens, slowest first")
    fre = d.get("re", r"_([A-Z]+)$")
    try:
        ngroups = re.compile(fre).groups
        if ngroups < 1:
            raise re.error("needs at least one group")
    except re.error as e:
        raise KeyError(f"{where}: vt_flavours.re: {e}")
    if ngroups > 1:
        for t in toks:
            if t.count("/") != ngroups - 1:
                raise KeyError(f"{where}: vt_flavours.re has {ngroups} "
                               f"groups, so every token needs {ngroups} "
                               f"'/'-separated parts (got {t!r})")
    return {"tokens": list(toks), "re": fre}


TCL = r'''
set flavours {%(flavours)s}
set fre {%(fre)s}
# Per-rail flavour lists (chip view): {<rail>__ {tokens re} ...}; cells of
# no listed rail use the defaults above.
set rail_fl_default [list $flavours $fre]
set rail_fl [dict create %(rails)s]
proc flav_select {name} {
  global flavours fre rail_fl rail_fl_default
  lassign $rail_fl_default flavours fre
  dict for {pre fl} $rail_fl {
    if {[string first $pre $name] == 0} { lassign $fl flavours fre; break }
  }
}
# A cell name's flavour: the regex's groups joined by "/" (one group, as
# for a threshold-only ladder: just the group). The regex may have several
# groups, e.g. a Vt letter and a second code; a ladder token then names
# one value per group, "/"-separated, in group order.
proc flav_token {name} {
  global fre
  set m [regexp -indices -inline -- $fre $name]
  if {[llength $m] == 0} { return "" }
  set parts {}
  foreach g [lrange $m 1 end] {
    lappend parts [string range $name [lindex $g 0] [lindex $g 1]]
  }
  return [join $parts /]
}
proc flav_index {name} {
  global flavours
  flav_select $name
  if {[llength $flavours] == 0} { return -1 }
  set t [flav_token $name]
  if {$t eq ""} { return -1 }
  return [lsearch -exact $flavours $t]
}
proc flav_swap {name k} {
  global flavours fre
  flav_select $name
  set m [regexp -indices -inline -- $fre $name]
  set gs [lrange $m 1 end]
  set tok [lindex $flavours $k]
  if {[llength $gs] == 1} { set parts [list $tok] } else { set parts [split $tok /] }
  for {set i [expr {[llength $gs] - 1}]} {$i >= 0} {incr i -1} {
    lassign [lindex $gs $i] a b
    set name "[string range $name 0 [expr {$a - 1}]][lindex $parts $i][string range $name [expr {$b + 1}] end]"
  }
  return $name
}
# vt_swap.keep_clock_cells: a clock cell is a non-buffer, non-inverter whose
# output is a clock (cached per instance).
set keep_clk %(keep_clk)d
set clk_cell_cache [dict create]
proc is_clock_cell {inst} {
  global keep_clk clk_cell_cache
  if {!$keep_clk} { return 0 }
  set n [get_full_name $inst]
  if {[dict exists $clk_cell_cache $n]} { return [dict get $clk_cell_cache $n] }
  set r 0
  set lc [get_property $inst liberty_cell]
  if {$lc ne "" && ![get_property $lc is_buffer] && ![get_property $lc is_inverter]} {
    foreach op [get_pins -of_objects $inst] {
      if {[get_property $op direction] eq "output" && [sta::is_clock $op]} { set r 1; break }
    }
  }
  dict set clk_cell_cache $n $r
  return $r
}
proc lib_cell_of {name} {
  set c [get_lib_cells -quiet */$name]
  if {[llength $c] == 0} { return "" }
  return [lindex $c 0]
}
# A swap's veto: 1 lets `inst` go from cell `from` to cell `to`. The
# default allows every swap; the post-layout stage (hooks below) vetoes
# clock-network cells and footprint changes.
array set eco_stat {}
set hold_ft {}
set hold_rounds 1
set bulk_iters %(max_iter)d
proc sens_fix {max_iter paths margin} {}
# Hold: is a floor broken (the worst hold slack below hfloor), and which
# hold paths broke it. Filter on each path's own slack: -slack_max misses
# min paths under a set_min_delay exception.
proc hold_bad {} {
  global hfloor
  return [expr {[whs] < $hfloor - 1e-6}]
}
proc hold_begin {} {}
proc hold_bad_paths {} {
  global hfloor hold_ft
  set l {}
  foreach pe [find_timing_paths -path_delay min {*}$hold_ft -group_path_count %(paths)d \
                -endpoint_path_count 1 -sort_by_slack] {
    if {[sta::time_sta_ui [$pe slack]] >= $hfloor - 1e-6} { continue }
    lappend l $pe
  }
  return $l
}
proc eco_ok {inst from to} { return 1 }
set recover_leak 0
set recover_swap 0
set ladder_cap_idx -1
set escalate_idx -1
set close_margin 0
# Worst setup slack in the libraries' time unit (worst_slack_cmd is SI).
proc wns {} { return [sta::time_sta_ui [sta::worst_slack_cmd max]] }
proc whs {} { return [sta::time_sta_ui [sta::worst_slack_cmd min]] }
proc move {inst dir} {
  # One flavour step (dir +1 faster, -1 slower) to the next flavour whose
  # cell exists in the libraries (a ladder may have gaps: a cell family
  # that lacks one flavour steps over it); returns the new cell name or ""
  # when there is none.
  set ref [get_property $inst ref_name]
  set k [flav_index $ref]
  if {$k < 0} { return "" }
  for {set k2 [expr {$k + $dir}]} {$k2 >= 0 && $k2 < [llength $::flavours]} \
      {incr k2 $dir} {
    if {$dir > 0 && $::ladder_cap_idx >= 0 && $k2 > $::ladder_cap_idx} { return "" }
    set n [flav_swap $ref $k2]
    set lc [lib_cell_of $n]
    if {$lc ne "" && [eco_ok $inst $ref $n]} {
      replace_cell $inst $lc
      return $n
    }
  }
  return ""
}
%(hooks)s
proc tns {} { return [total_negative_slack -max] }
set tns_accept %(tns_accept)d
set margin %(margin)s
set rec [open %(record)s w]
set t0 [clock milliseconds]
proc lap {what} { global rec t0; puts $rec "time $what [expr {([clock milliseconds] - $t0) / 1000.0}]"; flush $rec }
set wns0 [wns]
lap timed
set whs0 [whs]
puts $rec "start [format %%.6f $wns0] [total_negative_slack -max] [format %%.6f $whs0]"
# 1. Fix. An iteration that does not improve the worst slack (faster
# cells load their drivers a little more, which can hurt on long, heavily
# loaded paths) is undone, and the loop stops. So is one that takes the
# worst hold slack below its floor: faster cells shorten short paths too.
set hfloor [expr {$whs0 < 0 ? $whs0 : 0}]
hold_begin
set frozen [dict create]
for {set it 0} {$it < $bulk_iters} {incr it} {
  set w [wns]
  set tn [tns]
  if {$w >= $margin} { break }
  set seen [dict create]
  foreach pe [find_timing_paths -path_delay max -group_path_count %(paths)d \
                -endpoint_path_count 1 -slack_max $margin -sort_by_slack] {
    foreach pin [[$pe path] pins] {
      set inst [$pin instance]
      if {$inst eq "" || $inst eq "NULL"} { continue }
      if {[$inst liberty_cell] eq "NULL"} { continue }
      if {[is_clock_cell $inst]} { continue }
      dict set seen [get_full_name $inst] $inst
    }
  }
  set moved {}
  dict for {name inst} $seen {
    if {[dict exists $frozen $name]} { continue }
    set from [get_property $inst ref_name]
    set to [move $inst 1]
    if {$to ne ""} { lappend moved [list $inst $name $from $to] }
  }
  # Hold: the moved cells on hold paths below the floor go back and stay
  # put from then on; the rest of the iteration stands.
  for {set round 0} {$round < $hold_rounds && [llength $moved] > 0 &&
                     [hold_bad]} {incr round} {
    set onhold [dict create]
    foreach pe [hold_bad_paths] {
      foreach pin [[$pe path] pins] {
        set inst [$pin instance]
        if {$inst eq "" || $inst eq "NULL"} { continue }
        dict set onhold [get_full_name $inst] 1
      }
    }
    set kept {}
    foreach m $moved {
      if {[dict exists $onhold [lindex $m 1]]} {
        replace_cell [lindex $m 0] [lib_cell_of [lindex $m 2]]
        dict set frozen [lindex $m 1] 1
        puts $rec "hold $it [lindex $m 1] [lindex $m 2] [lindex $m 3]"
      } else { lappend kept $m }
    }
    set moved $kept
  }
  set w2 [wns]
  set why ""
  if {[llength $moved] > 0 && [hold_bad]} { set why hold }
  # A post-layout fix also keeps an iteration that lowers the total
  # negative slack while the worst path stays where it is.
  if {[llength $moved] > 0 && $why eq "" && $w2 <= $w + 1e-6 &&
      !($tns_accept && [tns] > $tn + 1e-6)} {
    set why setup
  }
  if {$why eq "hold"} {
    set k 0
    foreach pe [hold_bad_paths] {
      puts $rec "holdbad [get_full_name [$pe pin]] [format %%.6f [sta::time_sta_ui [$pe slack]]]"
      if {[incr k] >= 5} break
    }
  }
  if {$why ne ""} {
    foreach m $moved { replace_cell [lindex $m 0] [lib_cell_of [lindex $m 2]] }
    puts $rec "iter $it [format %%.6f $w] moved 0 undone [llength $moved] $why"
    break
  }
  foreach m $moved { puts $rec "up $it [lindex $m 1] [lindex $m 2] [lindex $m 3]" }
  puts $rec "iter $it [format %%.6f $w] moved [llength $moved]"
  if {[llength $moved] == 0} { break }
}
sens_fix %(max_iter)d %(paths)d $margin
# 1b. Closing pass (opt-in): lift the cap and fix what is still short.
if {$escalate_idx >= 0 && [wns] < $close_margin - 1e-9} {
  puts $rec "close [format %%.6f [wns]] [total_negative_slack -max]"
  set ladder_cap_idx $escalate_idx
  set frozen [dict create]
  sens_fix %(max_iter)d %(paths)d $close_margin
}
set wns1 [wns]
lap fixed
puts $rec "fixed [format %%.6f $wns1] [total_negative_slack -max] [format %%.6f [whs]]"
# 2. Recover, in batches: a batch of cells moves one step slower at once
# and stays if the worst slack holds; else it is undone and halved. Timing
# updates then scale with the cells that must stay fast, not with all.
proc holds {floor tfloor} {
  if {[wns] < $floor - 1e-6} { return 0 }
  if {$tfloor ne "" && [tns] < $tfloor - 1e-6} { return 0 }
  return 1
}
proc try_down {lst floor tfloor} {
  global rec
  set done {}
  foreach inst $lst {
    set ref [get_property $inst ref_name]
    set to [move $inst -1]
    if {$to ne ""} { lappend done [list $inst $ref $to] }
  }
  if {[llength $done] == 0} { return 0 }
  if {[holds $floor $tfloor]} {
    foreach d $done {
      puts $rec "down [get_full_name [lindex $d 0]] [lindex $d 1] [lindex $d 2]"
    }
    return [llength $done]
  }
  foreach d $done { replace_cell [lindex $d 0] [lib_cell_of [lindex $d 1]] }
  if {[llength $done] == 1} { return 0 }
  set insts {}
  foreach d $done { lappend insts [lindex $d 0] }
  set h [expr {[llength $insts] / 2}]
  return [expr {[try_down [lrange $insts 0 [expr {$h - 1}]] $floor $tfloor]
                + [try_down [lrange $insts $h end] $floor $tfloor]}]
}
if {%(recover)d} {
  set floor [expr {$wns1 < $margin ? $wns1 : $margin}]
  # A design the fix did not close keeps its total negative slack too.
  set tfloor [expr {$wns1 < $margin ? [tns] : ""}]
  set changed 1
  while {$changed} {
    set cand {}
    foreach inst [get_cells -hierarchical *] {
      if {[flav_index [get_property $inst ref_name]] > 0 && ![is_clock_cell $inst]} { lappend cand $inst }
    }
    if {$recover_leak} {
      set keyed {}
      foreach inst $cand { lappend keyed [list [leak_of [get_property $inst ref_name]] $inst] }
      set cand {}
      foreach kv [lsort -real -decreasing -index 0 $keyed] { lappend cand [lindex $kv 1] }
    }
    set changed 0
    for {set i 0} {$i < [llength $cand]} {incr i 256} {
      incr changed [try_down [lrange $cand $i [expr {$i + 255}]] $floor $tfloor]
    }
  }
}
# 2b. Pair recovery (opt-in): a cell the plain recovery could not move down
# goes down together with a cheaper compensating cell on the worst path.
if {%(recover)d && $recover_swap > 0} {
  set floor2 [expr {[wns] < $margin ? [wns] : $margin}]
  set floor2 [expr {$floor2 < $floor ? $floor2 : $floor}]
  set cand {}
  foreach inst [get_cells -hierarchical *] {
    if {[flav_index [get_property $inst ref_name]] > 0 && ![is_clock_cell $inst]} {
      lappend cand [list [leak_of [get_property $inst ref_name]] $inst]
    }
  }
  set cand [lsort -real -decreasing -index 0 $cand]
  set ndone 0; set npair 0
  foreach kv [lrange $cand 0 [expr {$recover_swap - 1}]] {
    set inst [lindex $kv 1]
    set ref [get_property $inst ref_name]
    set to [move $inst -1]
    if {$to eq ""} { continue }
    set saved [expr {[leak_of $ref] - [leak_of $to]}]
    if {[holds $floor2 $tfloor]} {
      puts $rec "down [get_full_name $inst] $ref $to"; incr ndone; continue
    }
    # compensate: best cell on the worst path (one step up, under the cap)
    set best ""; set bw -1e9
    foreach pe [find_timing_paths -path_delay max -group_path_count 1 -sort_by_slack] {
      foreach pin [[$pe path] pins] {
        set i2 [$pin instance]
        if {$i2 eq "" || $i2 eq "NULL" || $i2 eq $inst} { continue }
        if {[$i2 liberty_cell] eq "NULL" || [is_clock_cell $i2]} { continue }
        set r2 [get_property $i2 ref_name]
        set t2 [move $i2 1]
        if {$t2 eq ""} { continue }
        set cost [expr {[leak_of $t2] - [leak_of $r2]}]
        # the pair must save leakage: the compensating move may cost at most
        # nine tenths of what the first move saves
        if {$cost < $saved * 0.9} {
          set w2 [wns]
          if {$w2 > $bw} { set bw $w2; set best [list $i2 $r2 $t2] }
        }
        replace_cell $i2 [lib_cell_of $r2]
      }
    }
    if {$best ne ""} {
      lassign $best i2 r2 t2
      replace_cell $i2 [lib_cell_of $t2]
      if {[holds $floor2 $tfloor] && ![hold_bad]} {
        puts $rec "down [get_full_name $inst] $ref $to"
        puts $rec "up pair [get_full_name $i2] $r2 $t2"
        incr npair; continue
      }
      replace_cell $i2 [lib_cell_of $r2]
    }
    replace_cell $inst [lib_cell_of $ref]
  }
  puts $rec "pairrec singles $ndone pairs $npair"
}
lap recovered
puts $rec "end [format %%.6f [wns]] [total_negative_slack -max] [format %%.6f [whs]]"
foreach {k v} [array get eco_stat] { puts $rec "stat $k $v" }
close $rec
write_verilog %(out)s
set rec [open %(record)s a]
lap written
close $rec
'''


def sdc_for(period_ps: float, clock: str, false_from: Sequence[str] = (),
            unit_ps: float = 1000.0) -> str:
    """A one-clock SDC: the clock on `clock`, zero I/O delays, and a false
    path from each of `false_from`."""
    return (f"create_clock -name clk -period {period_ps / unit_ps:.4f} "
            f"[get_ports {clock}]\n"
            f"set_input_delay 0 -clock clk "
            f"[delete_from_list [all_inputs] [get_ports {clock}]]\n"
            "set_output_delay 0 -clock clk [all_outputs]\n"
            + "".join(f"set_false_path -from [get_ports {p}]\n"
                      for p in false_from))


def swap(netlist: Path, top: str, out: Path, libs: Sequence[Path],
         tokens: Sequence[str], flavour_re: str, sdc_text: str,
         paths: int = 200, max_iter: int = 30, recover: bool = True,
         image: str = ORFS_IMAGE, margin: float = 0.0,
         hold_libs: Sequence[Path] = (), both_libs: Sequence[Path] = (),
         hold_both_libs: Sequence[Path] = (),
         rail_flavours: Optional[dict] = None,
         wire_model=None, wire_scales=None,
         layout: Optional["LayoutSpec"] = None,
         keep_clock_cells: bool = False) -> dict:
    """Swap flavours in `netlist` (see the module docstring); writes `out`
    and returns the record. With `layout` (a LayoutSpec: SPEF, propagated
    clocks, clock-network and footprint vetoes) the swap runs on a placed
    netlist; the record then has `eco_stat`, the swaps the vetoes stopped.
    `margin` is the slack to keep, in the libraries' time unit. With `hold_libs`, `libs` time setup only
    (`read_liberty -max`) and `hold_libs`, the same cells at the hold
    corner, time hold (`-min`). `both_libs` (level shifters) serve both;
    with `hold_both_libs` (the shifters at the hold corner) they time setup
    only and `hold_both_libs` time hold, like `libs` and `hold_libs`.
    `wire_model` (wire_model.WireModel) puts an expected wire on every net,
    for the whole session: the fix and recovery steps size against it.
    `wire_scales` ({"": Scale} or {"-max": setup, "-min": hold}, from
    wire_scales()) scales it to the corners' wire RC."""
    from . import wire_model as wire_mod
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    hold_libs = [Path(l).resolve() for l in hold_libs]
    both_libs = [Path(l).resolve() for l in both_libs]
    hold_both_libs = [Path(l).resolve() for l in hold_both_libs]
    files = [Path(netlist).resolve(), *(Path(l).resolve() for l in libs),
             *hold_libs, *both_libs, *hold_both_libs]
    if layout and layout.spef:
        files.append(Path(layout.spef).resolve())
    if layout and layout.spef2:
        files.append(Path(layout.spef2).resolve())
    rc_files = sorted({Path(f).resolve() for f in
                       (getattr(wire_scales, "files", None) or [])})
    flag = "-max " if hold_libs else ""
    both_flag = "-max " if hold_both_libs else ""
    # Setup libraries first: lib_cell_of takes the first library's cell,
    # and OpenSTA times min paths with the -min library's cell of that name.
    mc = bool(layout and layout.corners and hold_libs)
    if mc:
        # Two OpenSTA corners: with propagated clocks, setup must not
        # capture on the hold corner's faster clock tree.
        two = bool(layout.spef2)
        reads = (("define_corners setup hold setup2\n" if two
                  else "define_corners setup hold\n")
                 + "".join(f"read_liberty -corner setup {Path(l).resolve()}\n"
                           for l in libs)
                 + "".join(f"read_liberty -corner hold {l}\n"
                           for l in hold_libs)
                 + ("".join(f"read_liberty -corner setup2 {Path(l).resolve()}\n"
                            for l in libs) if two else ""))
    else:
        reads = ("".join(f"read_liberty {flag}{Path(l).resolve()}\n"
                         for l in libs)
                 + "".join(f"read_liberty -min {l}\n" for l in hold_libs))
    reads += ("".join(f"read_liberty {both_flag}{l}\n" for l in both_libs)
              + "".join(f"read_liberty -min {l}\n" for l in hold_both_libs))
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # The record goes beside the output, so a failed or killed run
        # still shows how far it got.
        record = out.with_suffix(".vt_swap.txt")
        sdc = td / "swap.sdc"
        sdc.write_text(sdc_text)
        tcl = td / "vt_swap.tcl"
        tcl.write_text(
            reads
            + f"read_verilog {files[0]}\nlink_design {top}\n"
            + f"read_sdc {sdc}\n"
            + (layout.pre_tcl(bool(hold_libs)) if layout else "")
            + (wire_mod.tcl(wire_model, str(td / "wire_model.spef"),
                            dict(wire_scales) if wire_scales else None)
               if (not layout or not layout.spef) else "")
            + TCL % {"hooks": layout.hooks_tcl() if layout else "",
                     "tns_accept": int(bool(layout)),
                     "flavours": " ".join(tokens), "fre": flavour_re,
                     "rails": " ".join(
                         "{%s__} {{%s} {%s}}" % (
                             r, " ".join(f["tokens"]) if f else "",
                             f["re"] if f else "")
                         for r, f in sorted((rail_flavours or {}).items())),
                     "record": record, "out": out, "paths": paths,
                     "max_iter": max_iter, "recover": int(recover),
                     "margin": f"{margin:g}",
                     "keep_clk": int(keep_clock_cells)}
            + "exit\n")
        local = os.environ.get("OPENSTA")
        if local:
            cmd = [local, "-no_init", "-no_splash", "-exit", str(tcl)]
        else:
            mounts = sorted({str(f.parent) for f in [*files, *rc_files]}
                            | {str(td), str(out.parent)})
            cmd = ["docker", "run", "--rm", "-u",
                   f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                   *[x for m in mounts for x in ("-v", f"{m}:{m}")],
                   image, STA_BIN, "-no_init", "-no_splash", "-exit",
                   str(tcl)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not record.exists() or not out.exists():
            raise VtSwapError(f"vt_swap: OpenSTA failed ({p.returncode}):\n"
                              f"{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
        lines = record.read_text().splitlines()
    bad = [l for l in p.stdout.splitlines()
           if l.startswith("Error") or re.search(r"not found|black box", l)]
    if bad:
        raise VtSwapError("vt_swap: OpenSTA reported:\n"
                          + "\n".join(bad[:10]))
    res = {"start": None, "fixed": None, "end": None, "iterations": [],
           "up": [], "down": []}
    if wire_model is not None:
        res["wire_model"] = wire_model.to_dict()
        if wire_scales:
            res["wire_rc"] = {f or "all": f"{sc.corner} against {sc.ref}"
                              for f, sc in wire_scales.items() if sc}
    for l in lines:
        w = l.split()
        if w[0] in ("start", "fixed", "end"):
            res[w[0]] = {"wns": float(w[1]), "tns": float(w[2]),
                         "hold_wns": float(w[3]) if len(w) > 3 else None}
        elif w[0] == "iter":
            it = {"wns": float(w[2]), "moved": int(w[4])}
            if len(w) > 6 and w[5] == "undone":
                it["undone"] = int(w[6])
                it["why"] = w[7] if len(w) > 7 else "setup"
            res["iterations"].append(it)
        elif w[0] == "hold":
            res.setdefault("held", []).append(
                {"inst": w[2], "from": w[3], "to": w[4]})
        elif w[0] == "up":
            res["up"].append({"inst": w[2], "from": w[3], "to": w[4]})
        elif w[0] == "down":
            res["down"].append({"inst": w[1], "from": w[2], "to": w[3]})
        elif w[0] == "stat":
            res.setdefault("eco_stat", {})[w[1]] = int(w[2])
        elif w[0] == "time":
            res.setdefault("seconds", {})[w[1]] = float(w[2])
    # OpenSTA's write_verilog renames and merges nets; keep the original
    # netlist byte for byte except the swapped instances' cell types.
    orig = Path(netlist).read_text()
    new_cells = cell_changes(orig, out.read_text())
    out.write_text(retype(orig, new_cells))
    res["instances_changed"] = sum(len(v) for v in new_cells.values())
    if rail_flavours:
        res["flavours_before"] = rail_flavour_counts(orig, rail_flavours)
        res["flavours_after"] = rail_flavour_counts(out.read_text(),
                                                    rail_flavours)
    else:
        res["flavours_before"] = flavour_counts(orig, tokens, flavour_re)
        res["flavours_after"] = flavour_counts(out.read_text(), tokens,
                                               flavour_re)
    return res


_INST = re.compile(r"^([ \t]*)([A-Za-z_]\w*)([ \t]+)(\\\S+|[A-Za-z_]\w*)"
                   r"(\s*\()", re.M)


_KEYWORDS = {"module", "input", "output", "inout", "wire", "reg", "assign",
             "endmodule", "supply0", "supply1", "tri"}


def instance_cells(text: str) -> dict:
    """{module: {instance: cell}} from instance lines alone (no full parse:
    a netlist the parser refuses, e.g. `wire a = b;`, still works). An
    escaped name keeps its backslash, as vnetlist keeps it."""
    from . import vnetlist as vn
    out = {}
    for m, (s0, e0) in vn.module_spans(text).items():
        cells = {}
        for mt in _INST.finditer(text, s0, e0):
            if mt.group(2) not in _KEYWORDS:
                cells[mt.group(4)] = mt.group(2)
        out[m] = cells
    return out


def cell_changes(orig: str, swapped: str) -> dict:
    """{module: {instance: new cell}} between two netlists of the same
    instances (`swapped` as OpenSTA writes it)."""
    a, b = instance_cells(orig), instance_cells(swapped)
    out = {}
    for m, cells in a.items():
        got = b.get(m)
        if not got:
            continue
        diff = {i: got[i] for i, c in cells.items()
                if i in got and got[i] != c}
        if diff:
            out[m] = diff
    return out


def retype(text: str, new_cells: dict) -> str:
    """`text` with each listed instance's cell type replaced
    ({module: {instance: cell}}), every other byte kept."""
    from . import vnetlist as vn
    spans = vn.module_spans(text)
    pieces, last = [], 0
    for m, (s0, e0) in sorted(spans.items(), key=lambda kv: kv[1][0]):
        if m not in new_cells:
            continue
        want = new_cells[m]
        seen = set()

        def sub(mt, want=want, seen=seen):
            name = mt.group(4)
            # vnetlist keeps an escaped name's backslash; accept both.
            key = name if name in want else name.lstrip("\\")
            if key in want and mt.group(2) not in _KEYWORDS:
                seen.add(key)
                return (mt.group(1) + want[key] + mt.group(3) + name
                        + mt.group(5))
            return mt.group(0)
        pieces.append(text[last:s0])
        pieces.append(_INST.sub(sub, text[s0:e0]))
        last = e0
        miss = set(want) - seen
        if miss:
            raise VtSwapError(f"vt_swap: could not retype {len(miss)} "
                              f"instance(s) of {m}, e.g. {sorted(miss)[0]}")
    pieces.append(text[last:])
    return "".join(pieces)


def rail_flavour_counts(text: str, rail_flavours: dict) -> dict:
    """{<rail>.<token>: instances} over the chip view's <rail>__<cell>
    names, each rail by its own flavour list."""
    out = {}
    for r, f in sorted(rail_flavours.items()):
        if not f:
            continue
        sub = "\n".join(l for l in text.splitlines()
                        if re.match(r"\s*" + re.escape(r) + r"__", l))
        out.update({f"{r}.{t}": n for t, n in
                    flavour_counts(sub, f["tokens"], f["re"]).items()})
    return out


def flavour_key(m) -> str:
    """The ladder token of a flavour-regex match: its groups, "/"-joined."""
    return "/".join(g or "" for g in m.groups())


def flavour_counts(text: str, tokens: Sequence[str], flavour_re: str) -> dict:
    """{token: instances} of a netlist's cells whose name carries one."""
    fre = re.compile(flavour_re)
    counts = {t: 0 for t in tokens}
    for m in re.finditer(r"^\s*(\w+)\s+\\?\S+\s*\(", text, re.M):
        f = fre.search(m.group(1))
        if f and flavour_key(f) in counts:
            counts[flavour_key(f)] += 1
    return counts


def summary(res: dict) -> str:
    return (f"WNS {res['start']['wns']:+.4f} -> {res['fixed']['wns']:+.4f} "
            f"after {len(res['iterations'])} fix iteration(s) "
            f"({len(res['up'])} up), {res['end']['wns']:+.4f} after recovery "
            f"({len(res['down'])} down); TNS {res['start']['tns']:.4f} -> "
            f"{res['end']['tns']:.4f}"
            + (f" ({', '.join(f'{k} {v:.0f}s' for k, v in res['seconds'].items())})"
               if res.get("seconds") else "")
            + (f"; hold WNS {res['start']['hold_wns']:+.4f} -> "
               f"{res['end']['hold_wns']:+.4f}"
               if res['start'].get('hold_wns') is not None else "")
            + (f"; {len(res['held'])} kept for hold" if res.get("held")
               else "")
            + "".join(f"; last fix iteration undone ({it['why']} got worse)"
                      for it in res["iterations"][-1:] if it.get("undone"))
            + (f"; flavours {_fmt_counts(res['flavours_before'])} -> "
               f"{_fmt_counts(res['flavours_after'])}"
               if res.get('flavours_after') else ""))


def _fmt_counts(c: dict) -> str:
    if any("/" in k for k in c):         # a two-axis token: keep it readable
        return " ".join(f"{k}={v}" for k, v in c.items())
    return "/".join(f"{k}{v}" for k, v in c.items())


def wanted(cfg) -> Optional[VtSwapSpec]:
    """The run's VtSwapSpec when the stage is on (--vt-swap, or an enabled
    vt_swap: section)."""
    spec = cfg.design.vt_swap
    if cfg.run_vt_swap:
        return spec if spec is not None else VtSwapSpec()
    if spec is not None and spec.enabled:
        return spec
    return None


class WireScales(dict):
    """{flag: wire_rc.Scale} with the RC files the scales read."""
    files: list = []


def wire_scales(d, setup: Optional[str], hold: Optional[str]):
    """The wire model's per-corner scaling for a session timing setup at
    `setup` and (when not None) hold at `hold`: {"": s} or {"-max": s_setup,
    "-min": s_hold}; None without a pack wire_rc / wire_model.rc_corner."""
    from . import wire_rc as wire_rc_mod
    wm, rc = d.wire_model, getattr(d, "wire_rc", None)
    if wm is None or rc is None or wm.rc_corner is None:
        return None
    s = wire_rc_mod.scale(rc, setup, wm.rc_corner)
    if s is None:
        return None
    out = WireScales({"": s} if hold is None else
                     {"-max": s, "-min": wire_rc_mod.scale(rc, hold,
                                                           wm.rc_corner)})
    out.files = [e.path for c in (setup, hold, wm.rc_corner)
                 if c is not None for e in [rc.at(c)]
                 if e is not None and e.path is not None]
    return out


def run(cfg, spec: VtSwapSpec) -> dict:
    """The flow stage: swap the run's OpenSTA netlist in place."""
    from . import intent, profile as profile_mod
    from .modes import unit_ps
    d = cfg.design
    if d.power is not None:
        raise VtSwapError("vt_swap: a power flow swaps per domain "
                          "(run_power, from the power flow)")
    if d.profile is None:
        raise VtSwapError("vt_swap needs a profile with vt_flavours:")
    prof = profile_mod.load(d.profile)
    fl = prof.flavours_at(d.rail)
    if not fl:
        raise VtSwapError(f"vt_swap: {prof.name} has no vt_flavours: "
                          "(pack level or on the run's rail)")
    nl = cfg.openroad_netlist
    if not nl.exists():
        raise VtSwapError(f"vt_swap: {nl} is missing (needs sv2v)")
    libs = prof.libs_at(rail=d.rail)
    hold = prof.hold_corner
    hold_libs = (prof.libs_at(hold, rail=d.rail)
                 if hold is not None and hold != prof.setup_corner else [])
    modes = list(d.effective_modes())
    tight = min(modes, key=lambda m: min(
        (c.period_ps for c in m.clocks.values() if c.ports),
        default=float("inf")))
    paths = intent.write_mode_sdcs(cfg.out_dir / "vt_swap_sdc", [tight],
                                   d.top_module, d.time_unit, d.clock_port)
    sdc = Path(paths[tight.name]).read_text() + "".join(
        f"set_false_path -from [get_ports {p}]\n" for p in spec.false_from
    ) + false_to_sdc(spec.false_to)
    keep = cfg.synth_dir / f"{d.top_module}_preswap_openroad.v"
    keep.write_text(nl.read_text())
    out = cfg.synth_dir / f"{d.top_module}_vtswap_openroad.v"
    check_ladder_tokens(spec.ladder_cap, spec.ladder_escalate,
                        fl["tokens"], "vt_swap")
    layout = None
    if (spec.fix != "bulk" or spec.recover_order != "inst" or spec.ladder_cap
            or spec.allow_footprint_change or spec.ladder_escalate):
        # sensitivity fix / leakage-ordered recovery / ladder cap: the
        # post-layout stage's machinery, on the unplaced netlist (the wire
        # model stands in for the SPEF).
        from .postlayout import liberty_table
        leak = {c: v[1] for c, v in liberty_table(libs).items()}
        layout = LayoutSpec(spef=None, propagated=False, protect_clock=False,
                            corners=False, fix=spec.fix,
                            allow_footprint_change=spec.allow_footprint_change,
                            sens_frac=spec.sens_frac, leakage=leak,
                            recover_order=spec.recover_order,
                            ladder_cap=spec.ladder_cap,
                            ladder_escalate=spec.ladder_escalate,
                            close_margin=spec.close_margin_ps
                            / unit_ps(d.time_unit))
    res = swap(keep, d.top_module, out, libs, fl["tokens"],
               fl["re"], sdc, spec.paths, spec.max_iter,
               spec.recover, margin=spec.margin_ps / unit_ps(d.time_unit),
               hold_libs=hold_libs, wire_model=d.wire_model,
               wire_scales=wire_scales(d, prof.setup_corner,
                                       hold if hold_libs else None),
               layout=layout, keep_clock_cells=spec.keep_clock_cells)
    out.replace(nl)
    res.update({"mode": tight.name, "libs": [str(l) for l in libs],
                "setup_corner": prof.setup_corner, "hold_corner": hold,
                "hold_libs": [str(l) for l in hold_libs],
                "false_from": list(spec.false_from),
                "false_to": list(spec.false_to),
                "unswapped_netlist": str(keep), "netlist": str(nl)})
    (cfg.out_dir / "vt_swap.json").write_text(json.dumps(res, indent=2)
                                              + "\n")
    print(f"[vt_swap] mode {tight.name}: {summary(res)} -> {nl} "
          f"(unswapped: {keep})")
    return res


def run_power(cfg, spec: VtSwapSpec, power_summary: dict) -> dict:
    """The power flow's stage: swap on the chip view (see the module
    docstring); rewrites the stitched OpenSTA netlist in place and returns
    the record (also <out>/vt_swap.json)."""
    from . import intent, profile as profile_mod, sta, vnetlist as vn
    from .modes import unit_ps
    d = cfg.design
    prof = profile_mod.load(d.profile)
    nl = cfg.openroad_netlist
    if not nl.exists():
        raise VtSwapError(f"vt_swap: {nl} is missing (needs sv2v)")
    top = d.top_module
    text = nl.read_text()
    keep = cfg.synth_dir / f"{top}_preswap_openroad.v"
    keep.write_text(text)
    mods = vn.parse(text)
    tmod = mods[top]
    doms = intent.domain_infos(prof, prof.default_rail,
                               power_summary["domains"])
    # Every module's rail: the top's and the glue's the default rail, each
    # domain's (and everything under it) its own.
    prefix = {m: prof.default_rail for m in sta.submodules(mods, top)}
    owners: dict = {}
    for dom in doms:
        if dom.module is not None:
            for m in sta.submodules(mods, dom.module):
                prefix[m] = dom.rail
                owners.setdefault(m, set()).add(dom.rail)
    shared = sorted(m for m, o in owners.items() if len(o) > 1)
    for m in shared:
        print(f"[vt_swap] WARNING: module {m} is under domains on rails "
              f"{', '.join(sorted(owners[m]))}; not swapped")
    work = cfg.out_dir / "vt_swap"
    work.mkdir(parents=True, exist_ok=True)
    corner = prof.setup_corner
    hold = prof.hold_corner
    split = hold is not None and hold != corner
    libs, hold_libs, cells = [], [], {}
    for rail in sorted(set(prefix.values())):
        for c, dest in ((corner, libs),) + (((hold, hold_libs),) if split
                                            else ()):
            ldir = work / "libs" / (rail if c == corner else f"{rail}.{c}")
            ldir.mkdir(parents=True, exist_ok=True)
            for lp in prof.libs_at(c, rail=rail):
                t, names = sta.rename_liberty(
                    Path(lp).read_text(errors="ignore"), rail)
                dst = ldir / Path(lp).name
                dst.write_text(t)
                dest.append(dst)
                if c == corner:
                    cells.setdefault(rail, set()).update(names)
    # The shifters at the setup corner, and (with a split hold corner, when
    # they differ) at the hold corner: they go with the hold libraries.
    ls_libs = prof.level_shifters_at(corner)
    ls_hold = prof.level_shifters_at(hold) if split else []
    if ls_hold == ls_libs:
        ls_hold = []
    chip = work / f"{top}_chip.v"
    chip.write_text(sta.rename_netlist(text, prefix, cells))
    top_ins = [p for p in tmod.ports if tmod.decls[p].kind == "input"]
    top_outs = [p for p in tmod.ports if tmod.decls[p].kind == "output"]
    modes = list(d.effective_modes())
    sdcs = intent.write_mode_sdcs(work / "sdc", modes, top, d.time_unit,
                                  d.clock_port, top_ins, top_outs)
    period = lambda m: min((c.period_ps for c in m.clocks.values()
                            if c.ports), default=float("inf"))
    order = sorted(modes, key=period)
    margin = spec.margin_ps / unit_ps(d.time_unit)
    # Each rail's flavour list; one list when every rail has the same.
    rail_fl = {r: prof.flavours_at(r) for r in sorted(set(prefix.values()))}
    if not any(rail_fl.values()):
        raise VtSwapError(f"vt_swap: {prof.name} has no vt_flavours: "
                          "(pack level or on any rail in use)")
    rail_fl_arg = None
    base = rail_fl.get(prof.default_rail) or next(
        f for f in rail_fl.values() if f)
    if any(f != base for f in rail_fl.values()):
        rail_fl_arg = rail_fl
    tokens, fre = base["tokens"], base["re"]
    rec = {"view": "chip", "corner": corner,
           **({"flavours": {r: (f["tokens"] if f else None)
                            for r, f in rail_fl.items()}}
              if rail_fl_arg else {}), "hold_corner": hold,
           "unswapped_netlist": str(keep),
           "netlist": str(nl), "shared_modules": shared, "modes": {}}
    cur = chip
    for k, m in enumerate(order):
        sdc_text = Path(sdcs[m.name]).read_text() + "".join(
            f"set_false_path -from [get_ports {p}]\n"
            for p in spec.false_from) + false_to_sdc(spec.false_to)
        out = work / f"{top}_chip_{k}_{m.name}.v"
        # The tightest mode fixes and recovers; each later mode only fixes
        # what the earlier passes left failing in it.
        res = swap(cur, top, out, libs, tokens, fre, sdc_text,
                   spec.paths, spec.max_iter, spec.recover and k == 0,
                   margin=margin, hold_libs=hold_libs, both_libs=ls_libs,
                   hold_both_libs=ls_hold, rail_flavours=rail_fl_arg,
                   wire_model=d.wire_model,
                   wire_scales=wire_scales(d, corner,
                                           hold if hold_libs else None))
        rec["modes"][m.name] = res
        print(f"[vt_swap] chip, mode {m.name}"
              + (" (fix only)" if k else "") + f": {summary(res)}")
        cur = out
    # Back to the stitched netlist: strip the rail prefixes, retype.
    new_cells: dict = {}
    for m, diff in cell_changes(chip.read_text(), cur.read_text()).items():
        if m in shared or m not in prefix:
            continue
        pre = f"{prefix[m]}__"
        new_cells[m] = {i: (c[len(pre):] if c.startswith(pre) else c)
                        for i, c in diff.items()}
    rec["instances_changed"] = sum(len(v) for v in new_cells.values())
    nl.write_text(retype(text, new_cells))
    (cfg.out_dir / "vt_swap.json").write_text(json.dumps(rec, indent=2)
                                              + "\n")
    print(f"[vt_swap] {rec['instances_changed']} instance(s) changed -> {nl} "
          f"(unswapped: {keep})")
    return rec
