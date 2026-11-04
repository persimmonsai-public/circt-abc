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
The libraries are the REAL per-flavour ones, never a priced mapping
library. The netlist is rewritten with OpenSTA's write_verilog; the
record lists every swap and the slack before and after. OpenSTA runs in
the ORFS image (`sta`), or the binary $OPENSTA names.

In the flow (a flow config's `vt_swap:` section, or --vt-swap) the stage
runs on the OpenSTA netlist after synthesis, before the STA stage, with
the profile's setup-corner libraries for the run's rail (its mapping
liberties when it has no corners, so such a pack must not list a priced
library first), the tightest mode's SDC plus `false_from:` ports, and the
pack's `vt_flavours:`. The unswapped netlist is kept as
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
                "margin_ps"}


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
    for k in ("enabled", "recover"):
        if k in d and not isinstance(d[k], bool):
            raise VtSwapError(f"{where}: vt_swap.{k} must be true or false")
    mg = d.get("margin_ps", 0.0)
    if isinstance(mg, bool) or not isinstance(mg, (int, float)) or mg < 0:
        raise VtSwapError(f"{where}: vt_swap.margin_ps must be a number of "
                          "ps >= 0")
    return VtSwapSpec(d.get("enabled", True), tuple(ff), d.get("paths", 200),
                      d.get("max_iter", 30), d.get("recover", True),
                      float(mg))


def parse_flavours(d, where) -> Optional[dict]:
    """A pack's `vt_flavours: {tokens: [slowest, ..., fastest], re: ...}`."""
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
        if re.compile(fre).groups != 1:
            raise re.error("needs exactly one group")
    except re.error as e:
        raise KeyError(f"{where}: vt_flavours.re: {e}")
    return {"tokens": list(toks), "re": fre}


TCL = r'''
set flavours {%(flavours)s}
set fre {%(fre)s}
proc flav_index {name} {
  global flavours fre
  if {![regexp -indices -- $fre $name all tok]} { return -1 }
  set t [string range $name [lindex $tok 0] [lindex $tok 1]]
  return [lsearch -exact $flavours $t]
}
proc flav_swap {name k} {
  global flavours fre
  regexp -indices -- $fre $name all tok
  return "[string range $name 0 [expr {[lindex $tok 0] - 1}]][lindex $flavours $k][string range $name [expr {[lindex $tok 1] + 1}] end]"
}
proc lib_cell_of {name} {
  set c [get_lib_cells -quiet */$name]
  if {[llength $c] == 0} { return "" }
  return [lindex $c 0]
}
# Worst setup slack in the libraries' time unit (worst_slack_cmd is SI).
proc wns {} { return [sta::time_sta_ui [sta::worst_slack_cmd max]] }
proc whs {} { return [sta::time_sta_ui [sta::worst_slack_cmd min]] }
proc move {inst dir} {
  # One flavour step (dir +1 faster, -1 slower); returns the new cell name
  # or "" when there is none.
  set ref [get_property $inst ref_name]
  set k [flav_index $ref]
  if {$k < 0} { return "" }
  set k2 [expr {$k + $dir}]
  if {$k2 < 0 || $k2 >= [llength $::flavours]} { return "" }
  set n [flav_swap $ref $k2]
  set lc [lib_cell_of $n]
  if {$lc eq ""} { return "" }
  replace_cell $inst $lc
  return $n
}
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
set frozen [dict create]
for {set it 0} {$it < %(max_iter)d} {incr it} {
  set w [wns]
  if {$w >= $margin} { break }
  set seen [dict create]
  foreach pe [find_timing_paths -path_delay max -group_path_count %(paths)d \
                -endpoint_path_count 1 -slack_max $margin -sort_by_slack] {
    foreach pin [[$pe path] pins] {
      set inst [$pin instance]
      if {$inst eq "" || $inst eq "NULL"} { continue }
      if {[$inst liberty_cell] eq "NULL"} { continue }
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
  if {[llength $moved] > 0 && [whs] < $hfloor - 1e-6} {
    set onhold [dict create]
    # Filter on each path's own slack: -slack_max misses min paths under
    # a set_min_delay exception.
    foreach pe [find_timing_paths -path_delay min -group_path_count %(paths)d \
                  -endpoint_path_count 1 -sort_by_slack] {
      if {[sta::time_sta_ui [$pe slack]] >= $hfloor - 1e-6} { continue }
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
  if {[llength $moved] > 0 && [whs] < $hfloor - 1e-6} { set why hold }
  if {[llength $moved] > 0 && $why eq "" && $w2 <= $w + 1e-6} {
    set why setup
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
set wns1 [wns]
lap fixed
puts $rec "fixed [format %%.6f $wns1] [total_negative_slack -max] [format %%.6f [whs]]"
# 2. Recover, in batches: a batch of cells moves one step slower at once
# and stays if the worst slack holds; else it is undone and halved. Timing
# updates then scale with the cells that must stay fast, not with all.
proc tns {} { return [total_negative_slack -max] }
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
      if {[flav_index [get_property $inst ref_name]] > 0} { lappend cand $inst }
    }
    set changed 0
    for {set i 0} {$i < [llength $cand]} {incr i 256} {
      incr changed [try_down [lrange $cand $i [expr {$i + 255}]] $floor $tfloor]
    }
  }
}
lap recovered
puts $rec "end [format %%.6f [wns]] [total_negative_slack -max] [format %%.6f [whs]]"
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
         hold_libs: Sequence[Path] = (), both_libs: Sequence[Path] = ()
         ) -> dict:
    """Swap flavours in `netlist` (see the module docstring); writes `out`
    and returns the record. `margin` is the slack to keep, in the
    libraries' time unit. With `hold_libs`, `libs` time setup only
    (`read_liberty -max`) and `hold_libs`, the same cells at the hold
    corner, time hold (`-min`); `both_libs` (level shifters) serve both."""
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    hold_libs = [Path(l).resolve() for l in hold_libs]
    both_libs = [Path(l).resolve() for l in both_libs]
    files = [Path(netlist).resolve(), *(Path(l).resolve() for l in libs),
             *hold_libs, *both_libs]
    flag = "-max " if hold_libs else ""
    # Setup libraries first: lib_cell_of takes the first library's cell,
    # and OpenSTA times min paths with the -min library's cell of that name.
    reads = ("".join(f"read_liberty {flag}{Path(l).resolve()}\n" for l in libs)
             + "".join(f"read_liberty -min {l}\n" for l in hold_libs)
             + "".join(f"read_liberty {l}\n" for l in both_libs))
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
            + TCL % {"flavours": " ".join(tokens), "fre": flavour_re,
                     "record": record, "out": out, "paths": paths,
                     "max_iter": max_iter, "recover": int(recover),
                     "margin": f"{margin:g}"}
            + "exit\n")
        local = os.environ.get("OPENSTA")
        if local:
            cmd = [local, "-no_init", "-no_splash", "-exit", str(tcl)]
        else:
            mounts = sorted({str(f.parent) for f in files}
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
        elif w[0] == "time":
            res.setdefault("seconds", {})[w[1]] = float(w[2])
    # OpenSTA's write_verilog renames and merges nets; keep the original
    # netlist byte for byte except the swapped instances' cell types.
    orig = Path(netlist).read_text()
    new_cells = cell_changes(orig, out.read_text())
    out.write_text(retype(orig, new_cells))
    res["instances_changed"] = sum(len(v) for v in new_cells.values())
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


def flavour_counts(text: str, tokens: Sequence[str], flavour_re: str) -> dict:
    """{token: instances} of a netlist's cells whose name carries one."""
    fre = re.compile(flavour_re)
    counts = {t: 0 for t in tokens}
    for m in re.finditer(r"^\s*(\w+)\s+\\?\S+\s*\(", text, re.M):
        f = fre.search(m.group(1))
        if f and f.group(1) in counts:
            counts[f.group(1)] += 1
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
    if not prof.vt_flavours:
        raise VtSwapError(f"vt_swap: {prof.name} has no vt_flavours:")
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
        f"set_false_path -from [get_ports {p}]\n" for p in spec.false_from)
    keep = cfg.synth_dir / f"{d.top_module}_preswap_openroad.v"
    keep.write_text(nl.read_text())
    out = cfg.synth_dir / f"{d.top_module}_vtswap_openroad.v"
    res = swap(keep, d.top_module, out, libs, prof.vt_flavours["tokens"],
               prof.vt_flavours["re"], sdc, spec.paths, spec.max_iter,
               spec.recover, margin=spec.margin_ps / unit_ps(d.time_unit),
               hold_libs=hold_libs)
    out.replace(nl)
    res.update({"mode": tight.name, "libs": [str(l) for l in libs],
                "setup_corner": prof.setup_corner, "hold_corner": hold,
                "hold_libs": [str(l) for l in hold_libs],
                "false_from": list(spec.false_from),
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
    if not prof.vt_flavours:
        raise VtSwapError(f"vt_swap: {prof.name} has no vt_flavours:")
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
    ls_libs = list(prof.level_shifters)
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
    tokens, fre = prof.vt_flavours["tokens"], prof.vt_flavours["re"]
    rec = {"view": "chip", "corner": corner, "hold_corner": hold,
           "unswapped_netlist": str(keep),
           "netlist": str(nl), "shared_modules": shared, "modes": {}}
    cur = chip
    for k, m in enumerate(order):
        sdc_text = Path(sdcs[m.name]).read_text() + "".join(
            f"set_false_path -from [get_ports {p}]\n"
            for p in spec.false_from)
        out = work / f"{top}_chip_{k}_{m.name}.v"
        # The tightest mode fixes and recovers; each later mode only fixes
        # what the earlier passes left failing in it.
        res = swap(cur, top, out, libs, tokens, fre, sdc_text,
                   spec.paths, spec.max_iter, spec.recover and k == 0,
                   margin=margin, hold_libs=hold_libs, both_libs=ls_libs)
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
