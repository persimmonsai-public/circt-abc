"""Post-layout timing optimisation: close setup on a placed, clock-tree-built
netlist with its parasitics, by flavour swaps that leave the placement alone.

    python3 -m circt_flow.postlayout --pack <pack.yaml> [--rail R] \\
        --netlist placed.v --spef placed.spef --sdc constraints.sdc \\
        --top <module> --out <dir> [--lef a.lef ...] [--margin-ps 0] \\
        [--measure-only]

Synthesis-time `vt_swap` (vtswap.py) times an unplaced netlist with ideal
clocks and no wires, so its slack is optimistic once P&R adds wire load and
clock skew. This stage runs the same OpenSTA fix/recover loops on the
netlist P&R wrote, in one OpenSTA session with

  * the parasitics of the P&R run (`read_spef`) and propagated clocks;
  * the clock network (every cell with an output pin on a clock: the CTS
    buffers and clock gates) frozen, so skew is not disturbed;
  * a footprint rule: a cell goes to a flavour only when the two have the
    same LEF SIZE (`--lef`; cells the LEF does not list are not swapped) or,
    with no LEF, the same liberty area. Footprint-equal cells have the same
    pins and outline, so the written netlist is ECO-clean: same instances,
    same nets, same placement, only cell types differ. The record lists every
    cell whose type changed and, from the LEFs when given, any whose size
    changed (none unless `allow_footprint_change`).

The result is `<out>/<top>_postopt.v` (the input byte for byte except the
swapped instances' cell types), `<out>/eco.json` (swaps, vetoes, before and
after: WNS, TNS, reg-to-reg violators, hold, leakage, area) and
`<out>/eco.tcl`, which applies the same swaps to an OpenROAD database
(`replace_cell`) so the placement and routing stay.

The flow declares the stage in the flow config's `pnr:` section:

    pnr:
      post_layout: {margin_ps: 0, paths: 1000, max_iter: 40}

and a tech pack's P&R command leaves `placed.v`, `placed.spef` (and
optionally `placed.lef.list`, one LEF per line; the pack's `pnr: {results:
{netlist, spef, lef_list}}` may name them otherwise) in the P&R directory; the
flow then runs this stage after the command (stages.pack_pnr).

Leakage is the state-averaged liberty leakage (cell_leakage): for each cell
the mean over its `when` states of the `leakage_power` values (summed over
power pins), else its `cell_leakage_power`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from . import vtswap

POST_KEYS = {"enabled", "margin_ps", "paths", "max_iter", "recover",
             "allow_footprint_change", "false_from", "hold_scope", "hold_tol_ps", "fix", "false_to",
             "sens_frac", "recover_order", "ladder_cap",
             "ladder_escalate", "close_margin_ps", "recover_swap", "also_spef"}


@dataclass(frozen=True)
class PostLayoutSpec:
    enabled: bool = True
    margin_ps: float = 0.0
    # Search limits, not tuning: a placed netlist has more failing paths per
    # iteration than the synthesis-time swap (paths 200, max_iter 30), and the
    # loop stops as soon as nothing moves.
    paths: int = 1000
    max_iter: int = 40
    recover: bool = True
    allow_footprint_change: bool = False
    false_from: tuple = ()
    hold_scope: str = "r2r"
    hold_tol_ps: float = 0.0
    false_to: tuple = ()
    fix: str = "sens"
    # moves per sensitivity iteration: a quarter of the cells that help, so
    # an iteration changes enough to make progress but few enough that each
    # move's measured gain still holds when the others are made with it
    sens_frac: float = 0.25
    # from pnr.spef_restore_pincap (one key for the export and this stage)
    spef_restore_pincap: bool = True
    recover_order: str = "inst"   # "leak": highest-leakage cells recover first
    ladder_cap: str = ""          # fix may not go above this ladder token
    # closing pass: when the capped fix leaves the worst slack below
    # `close_margin_ps`, lift the cap to this token and fix what is short
    ladder_escalate: str = ""
    close_margin_ps: float = 0.0
    recover_swap: int = 0         # pair recovery: try this many top-leak cells
    # second parasitic view timed together with the SPEF (opt-in): the fix and
    # the recovery then keep the worst slack over both
    also_spef: str = ""


def spef_for_timing(spef: Path, out_dir: Path, restore: bool,
                    est_rc_log: Optional[Path] = None) -> Path:
    """The SPEF this stage times with. OpenROAD's `estimate_parasitics
    -spef_file` drops the cap on pin nodes (spef_pincap.py); on a global-route
    SPEF that is a share of the wire cap and OpenSTA times optimistically. Warns
    when the *CAP lines fall short of the *D_NET totals by more than 1%; with
    `restore` times a copy with the cap put back (<out>/<name>.pincap.spef),
    weighted per pin by the export's stdout log `est_rc_log` when given
    (spef_export.tcl), else split equally."""
    from . import spef_pincap
    try:
        r = spef_pincap.check(spef)
    except (OSError, ValueError, IndexError) as e:
        print(f"[post_layout] WARNING: {spef.name}: could not be checked "
              f"({type(e).__name__}: {e}); timed as written")
        return spef
    if r["missing"] <= 0.01 * r["total"]:
        return spef
    pct = 100.0 * r["missing"] / r["total"]
    if r.get("nets_coupled"):
        # a SPEF with coupling capacitors (an extractor's): restore leaves it
        # as written, and so does this stage
        print(f"[post_layout] WARNING: {spef.name}: coupling capacitors on "
              f"{r['nets_coupled']} net(s): timed as written, although its "
              f"*CAP lines are {pct:.1f}% below the *D_NET totals on "
              f"{r['nets_short']} net(s)")
        return spef
    if not restore:
        print(f"[post_layout] WARNING: {spef.name}: *CAP lines are {pct:.1f}% "
              f"below the *D_NET totals on {r['nets_short']} nets (OpenROAD "
              "estimator SPEF without pin-node caps); timing is optimistic. "
              "spef_restore_pincap is off (--no-spef-restore-pincap).")
        return spef
    out_dir.mkdir(parents=True, exist_ok=True)
    fixed = out_dir / (spef.stem + ".pincap.spef")
    w = (spef_pincap.weights_from_log(Path(est_rc_log), spef)
         if est_rc_log else None)
    st = spef_pincap.fix(spef, fixed, w)
    print(f"[post_layout] {spef.name}: restored {pct:.1f}% pin-node cap on "
          f"{st['fixed']} nets -> {fixed}")
    return fixed


def parse_post_layout(d, where) -> PostLayoutSpec:
    """The `pnr.post_layout:` section (true/false, or the keys)."""
    if d is None or d is True:
        return PostLayoutSpec()
    if d is False:
        return PostLayoutSpec(enabled=False)
    if not isinstance(d, dict) or set(d) - POST_KEYS:
        bad = sorted(set(d) - POST_KEYS) if isinstance(d, dict) else ["?"]
        raise vtswap.VtSwapError(
            f"{where}: pnr.post_layout.{bad[0]} is not a key; allowed: "
            + ", ".join(sorted(POST_KEYS)))
    for k in ("paths", "max_iter"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], int)
                       or d[k] < 1):
            raise vtswap.VtSwapError(f"{where}: pnr.post_layout.{k} must be "
                                     "a positive integer")
    for k in ("enabled", "recover", "allow_footprint_change"):
        if k in d and not isinstance(d[k], bool):
            raise vtswap.VtSwapError(f"{where}: pnr.post_layout.{k} must be "
                                     "true or false")
    mg = d.get("margin_ps", 0.0)
    if isinstance(mg, bool) or not isinstance(mg, (int, float)) or mg < 0 \
            or mg != mg or mg == float("inf"):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.margin_ps must "
                                 "be a number of ps >= 0")
    vtswap.check_leakage_keys(d, f"{where}: pnr.post_layout")
    rs = d.get("recover_swap", 0)
    if isinstance(rs, bool) or not isinstance(rs, int) or rs < 0:
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.recover_swap must "
                                 "be an integer >= 0")
    ht = d.get("hold_tol_ps", 0.0)
    if isinstance(ht, bool) or not isinstance(ht, (int, float)) or ht < 0 \
            or ht != ht or ht == float("inf"):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.hold_tol_ps must "
                                 "be a number of ps >= 0")
    if not isinstance(d.get("also_spef", ""), str):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.also_spef must be "
                                 "a SPEF path")
    ft = d.get("false_to") or []
    if isinstance(ft, str) or not all(isinstance(x, str) for x in ft):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.false_to must be "
                                 "a list of port names (or \"outputs\")")
    if d.get("hold_scope", "r2r") not in ("r2r", "all"):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.hold_scope must "
                                 "be r2r or all")
    ff = d.get("false_from") or []
    if isinstance(ff, str) or not all(isinstance(x, str) for x in ff):
        raise vtswap.VtSwapError(f"{where}: pnr.post_layout.false_from must "
                                 "be a list of port names")
    return PostLayoutSpec(
        enabled=d.get("enabled", True), margin_ps=float(mg),
        paths=d.get("paths", 1000), max_iter=d.get("max_iter", 40),
        recover=d.get("recover", True),
        allow_footprint_change=d.get("allow_footprint_change", False),
        false_from=tuple(ff), hold_scope=d.get("hold_scope", "r2r"),
        hold_tol_ps=float(d.get("hold_tol_ps", 0.0)),
        false_to=tuple(d.get("false_to") or ()),
        fix=d.get("fix", "sens"), sens_frac=float(d.get("sens_frac", 0.25)),
        recover_order=d.get("recover_order", "inst"),
        ladder_cap=d.get("ladder_cap", ""),
        ladder_escalate=d.get("ladder_escalate", ""),
        close_margin_ps=float(d.get("close_margin_ps", 0.0)),
        recover_swap=int(d.get("recover_swap", 0)),
        also_spef=str(d.get("also_spef", "")))


# ---------------------------------------------------------------- data

def lef_sizes(lefs: Sequence[Path]) -> Dict[str, Tuple[float, float]]:
    """{macro: (width, height)} from LEF files' MACRO ... SIZE w BY h."""
    out = {}
    for f in lefs:
        cur = None
        for line in Path(f).read_text(errors="replace").splitlines():
            w = line.split()
            if len(w) == 2 and w[0] == "MACRO":
                cur = w[1]
            elif cur and len(w) >= 4 and w[0] == "SIZE" and w[2] == "BY":
                out[cur] = (float(w[1]), float(w[3].rstrip(";")))
                cur = None
    return out


_LEAK_GROUP = re.compile(r"\bleakage_power\s*\(\s*\)\s*\{([^{}]*)\}")
_ATTR = re.compile(r'(\w+)\s*:\s*("[^"]*"|[^;\s]+)\s*;')


def cell_leakage(part: str) -> float:
    """State-averaged leakage of one liberty `cell (...) {...}` body.

    Each `leakage_power () { ... }` group is read with its attributes in any
    order (`value`, optional `when`, optional `related_pg_pin`). Groups with
    the same `when` (one per power pin) are summed; the result is the mean
    over the distinct `when` states, every state equally likely. Groups
    without `when`, when there are no others, give the cell's leakage the
    same way; with neither, `cell_leakage_power`; else 0."""
    states: Dict[str, float] = {}
    plain = 0.0
    nplain = 0
    for g in _LEAK_GROUP.finditer(part):
        attrs = {k: v.strip('"') for k, v in _ATTR.findall(g.group(1))}
        if "value" not in attrs:
            continue
        v = float(attrs["value"])
        if "when" in attrs:
            w = " ".join(attrs["when"].split())
            states[w] = states.get(w, 0.0) + v
        else:
            plain += v
            nplain += 1
    if states:
        return sum(states.values()) / len(states)
    if nplain:
        return plain
    dl = re.search(r"\bcell_leakage_power\s*:\s*([\d.eE+-]+)", part)
    return float(dl.group(1)) if dl else 0.0


def liberty_table(libs: Sequence[Path]) -> Dict[str, Tuple[float, float]]:
    """{cell: (area, state-averaged leakage, see cell_leakage)} over the
    libraries (later libraries do not replace a cell an earlier one
    defined)."""
    out: Dict[str, Tuple[float, float]] = {}
    for f in libs:
        txt = Path(f).read_text(errors="replace")
        for part in re.split(r"\n\s*cell\s*\(", txt)[1:]:
            m = re.match(r'\s*"?([\w$]+)"?\s*\)', part)
            if not m or m.group(1) in out:
                continue
            a = re.search(r"\n\s*area\s*:\s*([\d.eE+-]+)", part)
            out[m.group(1)] = (float(a.group(1)) if a else 0.0,
                               cell_leakage(part))
    return out


def census(text: str, table: dict) -> dict:
    """Area and state-averaged leakage of a netlist's instances (liberty
    units), and the cells the table does not know."""
    cells = vtswap.instance_cells(text)
    area = leak = 0.0
    n = 0
    miss = {}
    for mod in cells.values():
        for c in mod.values():
            n += 1
            if c in table:
                area += table[c][0]
                leak += table[c][1]
            else:
                miss[c] = miss.get(c, 0) + 1
    return {"instances": n, "area": area, "leakage": leak, "unknown": miss}


# ------------------------------------------------------------- OpenSTA

def run_sta(tcl: str, files: Sequence[Path], image: str = vtswap.ORFS_IMAGE
            ) -> subprocess.CompletedProcess:
    """Run a Tcl script in OpenSTA ($OPENSTA, else the ORFS image)."""
    files = [Path(f).resolve() for f in files]
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "run.tcl"
        script.write_text(tcl + "exit\n")
        local = os.environ.get("OPENSTA")
        if local:
            cmd = [local, "-no_init", "-no_splash", "-exit", str(script)]
        else:
            mounts = sorted({str(f.parent) for f in files} | {td})
            cmd = ["docker", "run", "--rm", "-u",
                   f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                   *[x for m in mounts for x in ("-v", f"{m}:{m}")],
                   image, vtswap.STA_BIN, "-no_init", "-no_splash", "-exit",
                   str(script)]
        return subprocess.run(cmd, capture_output=True, text=True)


def _session(netlist, top, libs, hold_libs, sdc, spef, propagated=True) -> str:
    if hold_libs:
        reads = ("define_corners setup hold\n"
                 + "".join(f"read_liberty -corner setup {Path(l).resolve()}\n"
                           for l in libs)
                 + "".join(f"read_liberty -corner hold {Path(l).resolve()}\n"
                           for l in hold_libs))
        rs = (f"read_spef -corner setup {Path(spef).resolve()}\n"
              f"read_spef -corner hold {Path(spef).resolve()}\n" if spef
              else "")
    else:
        reads = "".join(f"read_liberty {Path(l).resolve()}\n" for l in libs)
        rs = f"read_spef {Path(spef).resolve()}\n" if spef else ""
    return (reads
            + f"read_verilog {Path(netlist).resolve()}\nlink_design {top}\n"
            + f"read_sdc {Path(sdc).resolve()}\n"
            + ("set_propagated_clock [all_clocks]\n" if propagated else "")
            + rs)


MEASURE = r'''
proc ns {t} { return [sta::time_sta_ui $t] }
set cp [all_registers -clock_pins]
set dp [all_registers -data_pins]
puts "M wns [ns [sta::worst_slack_cmd max]]"
puts "M tns [total_negative_slack -max]"
puts "M hold_wns [ns [sta::worst_slack_cmd min]]"
puts "M hold_tns [total_negative_slack -min]"
set r2r [find_timing_paths -from $cp -to $dp -path_delay max -group_path_count 100000 -endpoint_path_count 1 -unique_paths_to_endpoint -sort_by_slack]
set nv 0; set rw ""; set rt 0.0
foreach pe $r2r {
  set s [ns [$pe slack]]
  if {$rw eq ""} { set rw $s }
  if {$s < 0} { incr nv; set rt [expr {$rt + $s}] }
}
puts "M r2r_wns $rw"
puts "M r2r_tns $rt"
puts "M r2r_viol $nv"
set outs [dict create]
foreach p [all_outputs] { dict set outs [get_full_name $p] 1 }
set nall 0; set ni 0; set iw ""; set it 0.0; set nout 0; set ow ""
foreach pe [find_timing_paths -path_delay max -group_path_count 100000 -endpoint_path_count 1 -unique_paths_to_endpoint -sort_by_slack] {
  set s [ns [$pe slack]]
  if {[dict exists $outs [get_full_name [$pe pin]]]} {
    if {$ow eq ""} { set ow $s }
    if {$s < 0} { incr nout; incr nall }
    continue
  }
  if {$iw eq ""} { set iw $s }
  if {$s < 0} { incr ni; incr nall; set it [expr {$it + $s}] }
}
puts "M viol_endpoints $nall"
puts "M internal_wns $iw"
puts "M internal_tns $it"
puts "M internal_viol $ni"
puts "M output_wns $ow"
puts "M output_viol $nout"
set rh [find_timing_paths -from $cp -to $dp -path_delay min -group_path_count 1 -sort_by_slack]
foreach pe $rh { puts "M r2r_hold_wns [ns [$pe slack]]" }
# Input-to-register hold is not covered by the register-to-register numbers.
# Worst over the corners the session holds.
set ih [find_timing_paths -from [all_inputs] -to $dp -path_delay min -group_path_count 1 -sort_by_slack]
foreach pe $ih { puts "M in2r_hold_wns [ns [$pe slack]]" }
%(paths)s
'''


def measure(netlist, top, libs, sdc, spef, hold_libs=(), report=None,
            npaths: int = 10) -> dict:
    """Setup/hold WNS and TNS, reg-to-reg setup violators (endpoints with
    negative slack from a register clock pin to a register data pin) and
    all failing endpoints, on a placed netlist with SPEF and propagated
    clocks. With `report`, the worst `npaths` setup paths (unique
    endpoints, full clock expanded, fanout and capacitance) go to that file."""
    paths = ""
    if report:
        paths = ("report_checks -path_delay max -group_path_count %d "
                 "-unique_paths_to_endpoint -format full_clock_expanded "
                 "-fields {fanout cap slew} -digits 3 > %s\n"
                 % (npaths, Path(report).resolve()))
        Path(report).parent.mkdir(parents=True, exist_ok=True)
    tcl = (_session(netlist, top, libs, hold_libs, sdc, spef)
           + MEASURE % {"paths": paths})
    files = [netlist, sdc, *libs, *hold_libs] + ([spef] if spef else [])
    if report:
        files.append(Path(report).resolve().parent / "x")
    p = run_sta(tcl, files)
    res = {}
    for l in p.stdout.splitlines():
        w = l.split()
        if len(w) == 3 and w[0] == "M":
            res[w[1]] = float(w[2])
    if "wns" not in res:
        raise vtswap.VtSwapError(f"post-layout measure failed:\n"
                                 f"{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    for k in ("r2r_viol", "viol_endpoints", "internal_viol", "output_viol"):
        if k in res:
            res[k] = int(res[k])
    return res


# -------------------------------------------------------------- the stage

def write_eco_tcl(path: Path, swaps) -> None:
    """eco.tcl: [(instance, new cell)] applied to an OpenROAD database."""
    with open(path, "w") as fh:
        fh.write("# applies the post-layout swaps to the open OpenROAD database\n"
                 "set eco_done 0\nset eco_fail 0\n"
                 "# replace_cell where the resizer takes the swap; it silently\n"
                 "# keeps a cell whose library it does not class as equivalent,\n"
                 "# so the master is then swapped in the database itself:\n"
                 "# same pins, outline and placement.\n"
                 "proc eco_replace {inst cell} {\n"
                 "  global eco_done eco_fail\n"
                 "  set i [[ord::get_db_block] findInst $inst]\n"
                 "  if {$i eq \"NULL\"} { incr eco_fail; puts \"ECO FAIL $inst: no instance\"; return }\n"
                 "  catch {replace_cell $inst $cell}\n"
                 "  if {[[$i getMaster] getName] ne $cell} {\n"
                 "    set m [[ord::get_db] findMaster $cell]\n"
                 "    if {$m eq \"NULL\"} { incr eco_fail; puts \"ECO FAIL $inst: no master $cell\"; return }\n"
                 "    $i swapMaster $m\n"
                 "  }\n"
                 "  if {[[$i getMaster] getName] eq $cell} { incr eco_done } else { incr eco_fail; puts \"ECO FAIL $inst $cell\" }\n}\n")
        for i, b in swaps:
            fh.write("eco_replace {%s} %s\n" % (i.lstrip("\\"), b))
        fh.write('puts "ECO applied $eco_done, failed $eco_fail"\n')


def optimiser_sdc(text: str, spec: PostLayoutSpec) -> str:
    """The optimiser's own SDC: `text` plus the false paths of `spec`."""
    out = text + "".join(f"set_false_path -from [get_ports {p}]\n"
                         for p in spec.false_from)
    for p in spec.false_to:
        if p == "outputs":
            out += "set_false_path -to [all_outputs]\n"
        else:
            out += f"set_false_path -to [get_ports {p}]\n"
    return out


def optimise(netlist, top, out_dir, libs, hold_libs, tokens, flavour_re, sdc,
             spef, lefs: Sequence[Path] = (), spec: Optional[PostLayoutSpec] = None,
             time_unit_ps: float = 1000.0,
             image: str = vtswap.ORFS_IMAGE) -> dict:
    """Run the stage; see the module docstring. Times in `libs`' unit;
    `time_unit_ps` (ps per unit: 1000 for ns) converts the *_ps values."""
    spec = spec or PostLayoutSpec()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    netlist = Path(netlist).resolve()
    sizes = lef_sizes(lefs) if lefs else None
    sdc_text = optimiser_sdc(Path(sdc).read_text(), spec)
    table = liberty_table(libs)
    leak_cells = {c: v[1] for c, v in table.items()}
    layout = vtswap.LayoutSpec(spef=Path(spef), sizes=sizes,
                               allow_footprint_change=
                               spec.allow_footprint_change,
                               hold_scope=spec.hold_scope,
                               hold_tol=spec.hold_tol_ps / time_unit_ps,
                               fix=spec.fix, sens_frac=spec.sens_frac,
                               leakage=leak_cells,
                               recover_order=spec.recover_order,
                               ladder_cap=spec.ladder_cap,
                               ladder_escalate=spec.ladder_escalate,
                               close_margin=spec.close_margin_ps
                               / time_unit_ps,
                               recover_swap=spec.recover_swap,
                               spef2=(Path(spec.also_spef)
                                      if spec.also_spef else None))
    out = out_dir / f"{top}_postopt.v"
    before = measure(netlist, top, libs, sdc, spef, hold_libs,
                     report=out_dir / "paths_before.rpt")
    res = vtswap.swap(netlist, top, out, libs, tokens, flavour_re, sdc_text,
                      spec.paths, spec.max_iter, spec.recover, image=image,
                      margin=spec.margin_ps / time_unit_ps,
                      hold_libs=hold_libs, layout=layout)
    after = measure(out, top, libs, sdc, spef, hold_libs,
                    report=out_dir / "paths_after.rpt")
    orig, new = netlist.read_text(), out.read_text()
    changes = vtswap.cell_changes(orig, new)
    flat = {i: (c, new_c) for m, d in changes.items() for i, new_c in d.items()
            for c in [vtswap.instance_cells(orig)[m][i]]}
    footprint = []
    if sizes:
        for i, (a, b) in sorted(flat.items()):
            if sizes.get(a) != sizes.get(b):
                footprint.append({"inst": i, "from": a, "to": b,
                                  "size_from": sizes.get(a),
                                  "size_to": sizes.get(b)})
    # Replace-cell script for the placed database: same placement, only
    # the masters change. (Instance names as in the netlist; hierarchy
    # separators are those of the database.)
    write_eco_tcl(out_dir / "eco.tcl", [(n, b) for n, (a, b) in sorted(flat.items())])
    rec = {
        "netlist_in": str(netlist), "netlist_out": str(out),
        "spef": str(spef), "sdc": str(sdc), "top": top,
        "spec": {"margin_ps": spec.margin_ps, "paths": spec.paths,
                 "max_iter": spec.max_iter, "recover": spec.recover,
                 "allow_footprint_change": spec.allow_footprint_change},
        "before": {**before, **{k: v for k, v in census(orig, table).items()
                                if k != "unknown"}},
        "after": {**after, **{k: v for k, v in census(new, table).items()
                              if k != "unknown"}},
        "unknown_cells": census(new, table)["unknown"],
        "instances_changed": len(flat),
        "footprint_changed": footprint,
        "lef_checked": bool(sizes),
        "eco_stat": res.get("eco_stat", {}),
        "iterations": res["iterations"], "seconds": res.get("seconds"),
        "flavours_before": res["flavours_before"],
        "flavours_after": res["flavours_after"],
        "swaps": [{"inst": i, "from": a, "to": b}
                  for i, (a, b) in sorted(flat.items())],
        "held_for_hold": res.get("held", []),
    }
    (out_dir / "eco.json").write_text(json.dumps(rec, indent=1) + "\n")
    return rec


def one_line(rec: dict) -> str:
    b, a = rec["before"], rec["after"]
    return (f"WNS {b['wns']:+.4f} -> {a['wns']:+.4f}, TNS {b['tns']:.3f} -> "
            f"{a['tns']:.3f}, r2r violators {b['r2r_viol']} -> "
            f"{a['r2r_viol']}, leakage {b['leakage']:.3f} -> "
            f"{a['leakage']:.3f}, " + ("in2r hold %+.4f -> %+.4f, " % (
                b["in2r_hold_wns"], a["in2r_hold_wns"])
                if "in2r_hold_wns" in b and "in2r_hold_wns" in a else "")
            + f"{rec['instances_changed']} cell type(s) "
            f"changed, {len(rec['footprint_changed'])} footprint change(s)")


def run_flow(cfg, spec: PostLayoutSpec, pnr_dir: Path) -> Optional[dict]:
    """The flow stage, after a tech pack's P&R command: reads
    the netlist and SPEF (and LEF list) the pack names in `pnr.results`
    (pnr_recipe.RESULT_FILES by default) from <pnr>, writes
    <pnr>/post_layout/. Returns the record. A command that left no netlist
    or SPEF under those names is an error that names pnr.results and the
    files (not a skipped stage: the ladder would judge the command's own
    numbers as the delivered ones)."""
    from . import profile as profile_mod
    from .modes import unit_ps
    d = cfg.design
    from . import pnr_recipe
    pack_pnr = getattr(d, "pack_pnr", None)
    names = pnr_recipe.result_files(pack_pnr)
    nl, spef = pnr_dir / names["netlist"], pnr_dir / names["spef"]
    missing = [f.name for f in (nl, spef) if not f.exists()]
    if missing:
        raise RuntimeError(
            f"pnr.post_layout: the pack's P&R command left no "
            f"{' / '.join(missing)} in {pnr_dir}; the pack's pnr.results "
            + ("names them" if "results" in (pack_pnr or {}) else
               "is not set, so these are the defaults "
               f"({', '.join(pnr_recipe.RESULT_FILES.values())})")
            + ": set pnr.results to the names the command writes")
    if d.profile is None:
        raise vtswap.VtSwapError("post_layout needs a profile with "
                                 "vt_flavours:")
    prof = profile_mod.load(d.profile)
    fl = prof.flavours_at(d.rail)
    if not fl:
        raise vtswap.VtSwapError(f"post_layout: {prof.name} has no "
                                 "vt_flavours:")
    vtswap.check_ladder_tokens(spec.ladder_cap, spec.ladder_escalate,
                               fl["tokens"], "pnr.post_layout")
    libs = prof.libs_at(rail=d.rail)
    hold = prof.hold_corner
    hold_libs = (prof.libs_at(hold, rail=d.rail)
                 if hold is not None and hold != prof.setup_corner else [])
    sdc = pnr_dir / "constraint.sdc"
    lefl = pnr_dir / names["lef_list"]
    lefs = ([Path(x) for x in lefl.read_text().split()]
            if lefl.exists() else [])
    from dataclasses import replace
    restore = pnr_recipe.parse(d.pnr or {}, getattr(d, "pack_pnr", None)).get(
        "spef_restore_pincap", True)
    spec = replace(spec, spef_restore_pincap=restore)
    spef = spef_for_timing(spef, pnr_dir / "post_layout",
                           spec.spef_restore_pincap)
    if spec.also_spef:
        if not hold_libs:
            raise vtswap.VtSwapError(
                "pnr.post_layout.also_spef needs a pack hold corner (the "
                "second view is timed as a third OpenSTA corner)")
        extra = Path(spec.also_spef)
        if not extra.is_absolute() and getattr(d, "config_dir", None):
            extra = Path(d.config_dir) / extra
        spec = replace(spec, also_spef=str(spef_for_timing(
            extra, pnr_dir / "post_layout", spec.spef_restore_pincap)))
    rec = optimise(nl, d.top_module, pnr_dir / "post_layout", libs,
                   hold_libs, fl["tokens"], fl["re"], sdc, spef, lefs, spec,
                   unit_ps(d.time_unit))
    print(f"[post_layout] {one_line(rec)}")
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pack", required=True, type=Path)
    ap.add_argument("--rail", default=None)
    ap.add_argument("--netlist", required=True, type=Path)
    ap.add_argument("--spef", required=True, type=Path)
    ap.add_argument("--sdc", required=True, type=Path)
    ap.add_argument("--top", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--lef", action="append", default=[], type=Path)
    ap.add_argument("--margin-ps", type=float, default=0.0)
    ap.add_argument("--paths", type=int, default=1000)
    ap.add_argument("--max-iter", type=int, default=40)
    ap.add_argument("--no-recover", action="store_true")
    ap.add_argument("--allow-footprint-change", action="store_true")
    ap.add_argument("--false-to", action="append", default=[],
                    help="port name, or `outputs`: not optimised (a false "
                    "path in the optimiser's own session; the before/after "
                    "measurements still count them)")
    ap.add_argument("--fix", choices=("sens", "bulk"), default="sens")
    ap.add_argument("--sens-frac", type=float, default=0.25)
    ap.add_argument("--recover-order", choices=("inst", "leak"), default="inst")
    ap.add_argument("--ladder-cap", default="")
    ap.add_argument("--ladder-escalate", default="",
                    help="closing pass: lift the ladder cap to this token "
                    "for what the capped fix leaves below --close-margin-ps")
    ap.add_argument("--close-margin-ps", type=float, default=0.0)
    ap.add_argument("--recover-swap", type=int, default=0,
                    help="pair recovery over this many top-leakage cells")
    ap.add_argument("--also-spef", default="",
                    help="second parasitic view (SPEF) timed together with "
                    "--spef: worst slack over both (needs a hold corner)")
    ap.add_argument("--hold-tol-ps", type=float, default=0.0)
    ap.add_argument("--hold-scope", choices=("r2r", "all"), default="r2r")
    ap.add_argument("--false-from", action="append", default=[])
    ap.add_argument("--measure-only", action="store_true")
    ap.add_argument("--spef-restore-pincap", action="store_true",
                    default=True, help="(default) time with the pin-node cap "
                    "OpenROAD's estimator SPEF drops put back "
                    "(spef_pincap.py)")
    ap.add_argument("--est-rc-log", type=Path, default=None,
                    help="stdout log of the SPEF export (spef_export.tcl)")
    ap.add_argument("--no-spef-restore-pincap", dest="spef_restore_pincap",
                    action="store_false",
                    help="time with the SPEF as written (warns when short)")
    a = ap.parse_args(argv)
    a.spef = spef_for_timing(a.spef, a.out, a.spef_restore_pincap,
                             a.est_rc_log)
    if a.also_spef:
        a.also_spef = str(spef_for_timing(Path(a.also_spef), a.out,
                                          a.spef_restore_pincap, None))
    from . import profile as profile_mod
    prof = profile_mod.load(a.pack)
    rail = a.rail or prof.default_rail
    fl = prof.flavours_at(rail)
    libs = prof.libs_at(rail=rail)
    hold = prof.hold_corner
    hold_libs = (prof.libs_at(hold, rail=rail)
                 if hold is not None and hold != prof.setup_corner else [])
    unit_ps = {"ns": 1000.0, "ps": 1.0}.get(prof.time_unit, 1000.0)
    if a.also_spef and not hold_libs:
        raise vtswap.VtSwapError("--also-spef needs a pack hold corner (the "
                                 "second view is timed as a third corner)")
    if a.measure_only:
        r = measure(a.netlist, a.top, libs, a.sdc, a.spef, hold_libs,
                    report=a.out / "paths.rpt")
        (a.out / "measure.json").write_text(json.dumps(r, indent=1) + "\n")
        print(json.dumps(r))
        return 0
    if not fl:
        print(f"postlayout: {a.pack} has no vt_flavours: (the flavour ladder "
              "the swap moves cells along); --measure-only times without one",
              file=sys.stderr)
        return 2
    vtswap.check_ladder_tokens(a.ladder_cap, a.ladder_escalate, fl["tokens"],
                               "postlayout")
    spec = PostLayoutSpec(margin_ps=a.margin_ps, paths=a.paths,
                          max_iter=a.max_iter, recover=not a.no_recover,
                          allow_footprint_change=a.allow_footprint_change,
                          false_from=tuple(a.false_from),
                          hold_scope=a.hold_scope,
                          hold_tol_ps=a.hold_tol_ps, fix=a.fix,
                          false_to=tuple(a.false_to),
                          sens_frac=a.sens_frac,
                          recover_order=a.recover_order,
                          ladder_cap=a.ladder_cap,
                          ladder_escalate=a.ladder_escalate,
                          close_margin_ps=a.close_margin_ps,
                          recover_swap=a.recover_swap,
                          also_spef=a.also_spef)
    rec = optimise(a.netlist, a.top, a.out, libs, hold_libs, fl["tokens"],
                   fl["re"], a.sdc, a.spef, a.lef, spec, unit_ps)
    print(one_line(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
