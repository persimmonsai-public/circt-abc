#!/usr/bin/env python3
"""Slim a liberty down to what ABC's `read_lib -l` needs.

WHY.  A function-and-area library without timing groups makes ABC map under
unit delays, so every threshold flavour of a cell looks identical.  To let
synthesis choose between flavours it needs the NLDM of every flavour, but
foundry liberties with CCS, CCSN, power and variation tables run to hundreds
of megabytes each, and the bridge re-reads the library on every ABC run.  This keeps the library header, the lu_table templates and, per cell:
area, cell_leakage_power, pin directions/functions/capacitances and the NLDM
`timing()` tables (cell_rise/fall, rise/fall_transition); every other group
is dropped.

Usage:
    slim_liberty.py <in.lib> <out.lib> [--cells <names.txt> [--strip-re RE]]
                    [--rename <lib>]
                    [--comb-only] [--area-scale S] [--leak-weight W]

For a library that carries leakage only as per-state `leakage_power`
groups, this writes `cell_leakage_power` = the mean over states of the sum
over pg pins (library leakage unit), which is what ABC's parser reads.

--area-scale/--leak-weight write a PRICED library for the mapper only:
area := S * (area + W * cell_leakage_power).  S exists because ABC prints
genlib areas as %7.2f (an area of 0.0123 prints as 0.01); W puts leakage into the area
objective so that area recovery prefers the low-leakage flavour off the
critical path.  Never use a priced library for area reports.

--cells keeps only the listed cell names; with --strip-re, the names are
compared after deleting that regex's match (a flavour suffix, e.g.
'_(RVT|LVT)$', so one list covers every flavour); --comb-only drops cells
with ff/latch/statetable groups.
"""
import argparse, re, sys

# groups dropped wherever they appear (CCS/CCSN/power/noise/OCV detail)
DROP = {
    "internal_power", "leakage_power", "output_current_rise",
    "output_current_fall", "receiver_capacitance", "receiver_capacitance1_rise",
    "receiver_capacitance1_fall", "receiver_capacitance2_rise",
    "receiver_capacitance2_fall", "ccsn_first_stage", "ccsn_last_stage",
    "input_ccb", "output_ccb", "normalized_driver_waveform", "dynamic_current",
    "intrinsic_parasitic", "leakage_current", "pg_current", "compact_ccs_rise",
    "compact_ccs_fall", "ocv_sigma_cell_rise", "ocv_sigma_cell_fall",
    "ocv_sigma_rise_transition", "ocv_sigma_fall_transition",
    "ocv_sigma_rise_constraint", "ocv_sigma_fall_constraint",
    "ocv_std_dev_cell_rise", "ocv_std_dev_cell_fall",
    "ocv_mean_shift_cell_rise", "ocv_mean_shift_cell_fall",
    "ocv_skewness_cell_rise", "ocv_skewness_cell_fall",
    "ocv_std_dev_rise_transition", "ocv_std_dev_fall_transition",
    "ocv_mean_shift_rise_transition", "ocv_mean_shift_fall_transition",
    "ocv_skewness_rise_transition", "ocv_skewness_fall_transition",
    "char_config", "pg_pin", "test_cell", "power_lut_template",
    "output_current_template", "normalized_driver_waveform",
    "receiver_capacitance_fall", "receiver_capacitance_rise",
}
SEQ = {"ff", "latch", "statetable", "ff_bank", "latch_bank"}
SUFFIX = None  # --strip-re
GROUP = re.compile(r"^\s*([A-Za-z_][\w]*)\s*\(([^)]*)\)\s*\{")


def base(name):
    name = name.strip().strip('"')
    return SUFFIX.sub("", name) if SUFFIX is not None else name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--cells")
    ap.add_argument("--strip-re", help="regex deleted from cell names "
                    "before matching --cells (a flavour suffix)")
    ap.add_argument("--rename")
    ap.add_argument("--comb-only", action="store_true")
    ap.add_argument("--area-scale", type=float, default=1.0)
    ap.add_argument("--leak-weight", type=float, default=0.0)
    ap.add_argument("--no-timing", action="store_true",
                    help="also drop timing() groups (a yosys-style function/area library)")
    a = ap.parse_args()
    global SUFFIX
    if a.strip_re:
        SUFFIX = re.compile(a.strip_re)
    if a.no_timing:
        DROP.add("timing")
    keep = None
    if a.cells:
        keep = {l.strip() for l in open(a.cells) if l.strip()}

    out = open(a.out, "w")
    depth = 0          # brace depth of the line being read
    skip_to = None     # when dropping a group: the depth to return to
    cell_buf = None    # lines of the current cell (depth-1 group)
    cell_keep = True
    cell_seq = False
    ncell = nkept = 0
    leak = {}          # when -> summed value, for the current cell
    lk_when = lk_val = None
    collecting = False
    for line in open(a.inp, errors="replace"):
        if skip_to is not None:
            if collecting:
                mw = re.match(r'\s*when\s*:\s*"([^"]*)"', line)
                mv = re.match(r'\s*value\s*:\s*"?([-\d.eE+]+)', line)
                if mw: lk_when = mw.group(1)
                if mv: lk_val = float(mv.group(1))
            depth += line.count("{") - line.count("}")
            if depth <= skip_to:
                skip_to = None
                if collecting and lk_val is not None:
                    leak[lk_when] = leak.get(lk_when, 0.0) + lk_val
                collecting = False
            continue
        m = GROUP.match(line)
        if m and depth == 0 and m.group(1) == "library" and a.rename:
            line = f"library({a.rename}) {{\n"
        if m and depth >= 1:
            g = m.group(1)
            if depth == 1 and g == "cell":
                ncell += 1
                cell_buf = []
                cell_seq = False
                cell_keep = keep is None or base(m.group(2)) in keep
                leak = {}
            elif g in DROP:
                opened = depth
                depth += line.count("{") - line.count("}")
                if depth > opened:
                    skip_to = opened
                    collecting = g == "leakage_power" and depth == 3
                    lk_when = lk_val = None
                continue
            elif g in SEQ and cell_buf is not None:
                cell_seq = True
        d0 = depth
        depth += line.count("{") - line.count("}")
        if cell_buf is not None:
            cell_buf.append(line)
            if depth <= 1:   # the cell group closed
                if cell_keep and not (a.comb_only and cell_seq):
                    lk = sum(leak.values()) / len(leak) if leak else 0.0
                    for i, l in enumerate(cell_buf):
                        ma = re.match(r"(\s*)area\s*:\s*([-\d.eE+]+)", l)
                        if ma:
                            ar = a.area_scale * (float(ma.group(2)) + a.leak_weight * lk)
                            cell_buf[i] = (f"{ma.group(1)}area : {ar:.6g} ;\n"
                                           f"{ma.group(1)}cell_leakage_power : {lk:.6g} ;\n")
                            break
                    out.writelines(cell_buf)
                    nkept += 1
                cell_buf = None
            continue
        out.write(line)
    out.close()
    print(f"{a.inp}: {ncell} cells, kept {nkept}", file=sys.stderr)


if __name__ == "__main__":
    main()
