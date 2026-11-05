#!/usr/bin/env python3
"""Write the mvt-mini liberties: a tiny invented cell library (no PDK data)
in three threshold flavours at two supply voltages and three process /
temperature corners, plus level shifters.

    python3 make_libs.py <out_dir>

writes

    mini_{rvt,lvt,slvt}_{0p75v,0p55v}_{tt,ss,ff}.lib
                         one library per flavour x supply x corner
    mini_ls.lib          level shifters between supplies (the tt cells)
    mini_ls_{tt,ss,ff}.lib
                         the same shifters per corner: delay scales by the
                         mean of the two supplies' corner factors, leakage
                         likewise (mini_ls_tt is mini_ls under its own name)

File names are mini_<flavour>_<supply>_<corner>.lib; the library inside has
the same name. Corners (invented, not any foundry's):

    tt  typical process, 25 C       the reference
    ss  slow process, cold (-40 C)  slower, leaks less
    ff  fast process, hot (125 C)   faster, leaks more

The tt libraries' cells are exactly what make_libs.py wrote before corners
existed; ss and ff scale every delay, setup/hold constraint and leakage by
the corner's factors below. Every corner has the same cell names.

The shape copies a real multi-Vt, multi-voltage library on purpose, so what
works here carries over:

* Cell names carry the flavour (`MNAND2_X1_R`, `_L`, `_S`), so flavours can
  share one netlist; names do NOT carry the supply, so the same cell at
  0.55 V and 0.75 V has one name and the supply is a property of where the
  instance sits (a power domain), not of the netlist.
* Every flavour has the same area and footprint. Lower thresholds are
  faster and leak more; the speed gain of a low threshold is larger at the
  low supply.
* Each cell has `area` immediately followed by `cell_leakage_power`, the
  form scripts/mvt/make_abc_lib.py prices.
* Corners keep cell names too, so a netlist mapped at one corner is timed
  at any other by reading that corner's libraries.

NLDM tables are linear and easy to read: delay = f * (intrinsic + k * load)
+ 0.1 * input slew, in ns and pF, with f the flavour x supply factor below.
"""
import sys
from pathlib import Path

# flavour tag -> (library tag, {supply: delay factor}, {supply: leakage factor})
FLAVOURS = {
    "R": ("rvt", {"0p75v": 1.00, "0p55v": 1.90}, {"0p75v": 1.0, "0p55v": 0.4}),
    "L": ("lvt", {"0p75v": 0.82, "0p55v": 1.45}, {"0p75v": 7.0, "0p55v": 2.8}),
    "S": ("slvt", {"0p75v": 0.70, "0p55v": 1.15}, {"0p75v": 30.0, "0p55v": 12.0}),
}
SUPPLIES = {"0p75v": 0.75, "0p55v": 0.55}
# corner -> (nom_process, nom_temperature, {supply: delay factor},
#            {supply: leakage factor}); tt is 1.0 everywhere. The low supply
# is hurt more by a slow corner and helped more by a fast one.
CORNERS = {
    "tt": (1.0, 25, {"0p75v": 1.00, "0p55v": 1.00}, {"0p75v": 1.0, "0p55v": 1.0}),
    "ss": (1.2, -40, {"0p75v": 1.20, "0p55v": 1.30}, {"0p75v": 0.4, "0p55v": 0.4}),
    "ff": (0.8, 125, {"0p75v": 0.85, "0p55v": 0.80}, {"0p75v": 3.0, "0p55v": 3.0}),
}
LEAK_PER_AREA = 20.0  # nW per um^2 for RVT at 0.75 V

# base name, area, {output: function}, inputs, intrinsic delay (ns), drives
COMB = [
    ("MINV", 0.020, {"Y": "(!A)"}, "A", 0.010, (1, 2)),
    ("MBUF", 0.030, {"Y": "(A)"}, "A", 0.018, (1, 2)),
    ("MNAND2", 0.030, {"Y": "(!(A&B))"}, "AB", 0.014, (1, 2)),
    ("MNOR2", 0.030, {"Y": "(!(A|B))"}, "AB", 0.018, (1, 2)),
    ("MAND2", 0.040, {"Y": "(A&B)"}, "AB", 0.024, (1,)),
    ("MOR2", 0.040, {"Y": "(A|B)"}, "AB", 0.028, (1,)),
    ("MXOR2", 0.060, {"Y": "(A^B)"}, "AB", 0.030, (1,)),
    ("MXNOR2", 0.060, {"Y": "(!(A^B))"}, "AB", 0.030, (1,)),
    ("MAOI21", 0.040, {"Y": "(!((A&B)|C))"}, "ABC", 0.020, (1,)),
    ("MOAI21", 0.040, {"Y": "(!((A|B)&C))"}, "ABC", 0.020, (1,)),
    ("MMUX2", 0.070, {"Y": "((S&B)|(!S&A))"}, "ABS", 0.032, (1,)),
    ("MADDH", 0.090, {"CO": "(A&B)", "S": "(A^B)"}, "AB", 0.030, (1,)),
    ("MADDF", 0.170, {"CO": "(((A^B)&CI)|(A&B))", "S": "(A^B^CI)"},
     ["A", "B", "CI"], 0.045, (1,)),
]
# 5x5 tables, as real libraries have: ABC's load-aware genlib (read_lib -l)
# fits each pin's load slope between the first and the second-to-last load
# points, so a 2-point table gives every cell a zero load slope.
SLEW = (0.005, 0.02, 0.05, 0.10, 0.20)
LOAD = (0.0005, 0.002, 0.005, 0.010, 0.020)
CAP = 0.0015


def table(kind, intrinsic, k, f):
    rows = ", ".join(
        '"' + ", ".join(f"{f * (intrinsic + k * c) + 0.1 * s:.4f}"
                        for c in LOAD) + '"' for s in SLEW)
    return f"        {kind}(nldm) {{ values({rows}); }}\n"


def arc(related, sense, intrinsic, f, k=4.0, extra=""):
    return (f"      timing() {{\n        related_pin : \"{related}\";\n"
            f"        timing_sense : {sense};\n{extra}"
            + table("cell_rise", intrinsic, k, f)
            + table("cell_fall", intrinsic, k * 0.875, f)
            + table("rise_transition", 0.008, k * 1.25, f)
            + table("fall_transition", 0.007, k * 1.125, f)
            + "      }\n")


def inpin(name, cap=CAP, *extra):
    # One attribute per line: line-based readers
    # (gen_liberty_blackbox_stubs.py) expect that.
    return (f"    pin({name}) {{\n      direction : input;\n"
            f"      capacitance : {cap};\n"
            + "".join(f"      {e};\n" for e in extra) + "    }\n")


def head(name, area, leak):
    # area, then cell_leakage_power on the next line (make_abc_lib.py).
    return (f"  cell({name}) {{\n    area : {area:.4f};\n"
            f"    cell_leakage_power : {leak:.4f};\n")


def factors(vt, sup, corner):
    """(delay factor, leakage factor) of a flavour at a supply and corner."""
    _, df, lf = FLAVOURS[vt]
    _, _, cd, cl = CORNERS[corner]
    return df[sup] * cd[sup], lf[sup] * cl[sup]


def comb_cell(base, area, outs, ins, d, drive, vt, sup, corner):
    f, leak_f = factors(vt, sup, corner)
    a = area * (1.0 if drive == 1 else 1.6)
    s = head(f"{base}_X{drive}_{vt}", a, a * LEAK_PER_AREA * leak_f)
    s += "".join(inpin(i, CAP * drive) for i in ins)
    for o, fn in outs.items():
        s += (f"    pin({o}) {{\n      direction : output;\n"
              f"      max_capacitance : {0.08 * drive};\n"
              f"      function : \"{fn}\";\n")
        for i in ins:
            s += arc(i, "non_unate", d, f, k=4.0 / drive)
        s += "    }\n"
    return s + "  }\n"


def setup_hold(f, related="CK"):
    return "".join(
        f"      timing() {{\n        related_pin : \"{related}\";\n"
        f"        timing_type : {t}_rising;\n"
        f"        rise_constraint(scalar) {{ values(\"{v * f:.4f}\"); }}\n"
        f"        fall_constraint(scalar) {{ values(\"{v * f:.4f}\"); }}\n"
        f"      }}\n"
        for t, v in (("setup", 0.020), ("hold", 0.005)))


def flop(base, area, vt, sup, corner, async_pin="", kind=""):
    f, leak_f = factors(vt, sup, corner)
    s = head(f"{base}_X1_{vt}", area, area * LEAK_PER_AREA * leak_f)
    s += "    ff(IQ, IQN) {\n      clocked_on : \"CK\";\n      next_state : \"D\";\n"
    if async_pin:
        s += f"      {kind} : \"!{async_pin}\";\n"
    s += "    }\n"
    s += inpin("CK", CAP, "clock : true")
    s += (f"    pin(D) {{\n      direction : input;\n      capacitance : {CAP};\n"
          f"      nextstate_type : data;\n" + setup_hold(f) + "    }\n")
    if async_pin:
        s += inpin(async_pin)
    s += ("    pin(Q) {\n      direction : output;\n      max_capacitance : 0.08;\n"
          "      function : \"IQ\";\n"
          + arc("CK", "non_unate", 0.050, f, extra="        timing_type : rising_edge;\n"))
    if async_pin:
        s += arc(async_pin, "positive_unate" if kind == "preset"
                 else "negative_unate", 0.040, f,
                 extra=f"        timing_type : {kind};\n")
    return s + "    }\n  }\n"


def icg(vt, sup, corner):
    f, leak_f = factors(vt, sup, corner)
    return head(f"MICG_X1_{vt}", 0.12, 0.12 * LEAK_PER_AREA * leak_f) + """    clock_gating_integrated_cell : "latch_posedge_precontrol";
    pin_name_map(CK, E, SE, ECK);
    statetable("CK E SE", "IQ") {
      table : "L L L : - : L, L L H : - : H, L H L : - : H, L H H : - : H, H - - : - : N";
    }
    pin(IQ) {
      direction : internal;
      internal_node : "IQ";
    }
""" + inpin("CK", CAP, "clock : true", "clock_gate_clock_pin : true") + f"""    pin(E) {{
      direction : input;
      capacitance : {CAP};
      clock_gate_enable_pin : true;
{setup_hold(f)}    }}
    pin(SE) {{
      direction : input;
      capacitance : {CAP};
      clock_gate_test_pin : true;
{setup_hold(f)}    }}
    pin(ECK) {{
      direction : output;
      max_capacitance : 0.08;
      clock_gate_out_pin : true;
      state_function : "(CK&IQ)";
{arc("CK", "positive_unate", 0.030, f)}    }}
  }}
"""


def tie(name, pin, fn, vt, sup, corner):
    return (head(f"{name}_{vt}", 0.02,
                 0.02 * LEAK_PER_AREA * factors(vt, sup, corner)[1])
            + f"    pin({pin}) {{\n      direction : output;\n"
            f"      function : \"{fn}\";\n    }}\n  }}\n")


def header(lib, volts, what, process=1, temperature=25):
    return f"""/* {lib}.lib: {what}
   Invented for circt-abc's mvt-mini example (examples/mvt-mini); generated
   by make_libs.py, no foundry or PDK data. Units: ns, pF, nW. */
library({lib}) {{
  delay_model : table_lookup;
  time_unit : "1ns";
  capacitive_load_unit(1, pf);
  voltage_unit : "1V";
  current_unit : "1mA";
  leakage_power_unit : "1nW";
  pulling_resistance_unit : "1kohm";
  nom_voltage : {volts};
  nom_temperature : {temperature};
  nom_process : {process:g};
  default_max_transition : 0.2;
  input_threshold_pct_rise : 50;
  input_threshold_pct_fall : 50;
  output_threshold_pct_rise : 50;
  output_threshold_pct_fall : 50;
  slew_lower_threshold_pct_rise : 20;
  slew_lower_threshold_pct_fall : 20;
  slew_upper_threshold_pct_rise : 80;
  slew_upper_threshold_pct_fall : 80;
  lu_table_template(nldm) {{
    variable_1 : input_net_transition;
    variable_2 : total_output_net_capacitance;
    index_1("{", ".join(map(str, SLEW))}");
    index_2("{", ".join(map(str, LOAD))}");
  }}
"""


def flavour_lib(vt, sup, corner):
    tag = FLAVOURS[vt][0]
    lib = f"mini_{tag}_{sup}_{corner}"
    proc, temp, _, _ = CORNERS[corner]
    s = header(lib, SUPPLIES[sup], f"{tag.upper()} cells at {SUPPLIES[sup]} V, "
               f"corner {corner}", proc, temp)
    for base, area, outs, ins, d, drives in COMB:
        for x in drives:
            s += comb_cell(base, area, outs, ins, d, x, vt, sup, corner)
    s += flop("MDFF", 0.20, vt, sup, corner)
    s += flop("MDFFRN", 0.24, vt, sup, corner, "RN", "clear")
    s += flop("MDFFSN", 0.24, vt, sup, corner, "SN", "preset")
    s += icg(vt, sup, corner)
    s += tie("MTIEHI", "H", "1", vt, sup, corner)
    s += tie("MTIELO", "L", "0", vt, sup, corner)
    return lib, s + "}\n"


def level_shifter(name, kind, vin, vout, f, leak_f=1.0):
    # Supply pins as a real shifter library has them: an LH cell has two
    # power pins, VDDL on the input side (the low rail it reads) and VDD on
    # the output side; an HL cell has one, VDDL, the low rail it drives
    # (its input tolerates the high swing). related_power_pin ties each
    # signal pin to its pin, which is how circt_flow tells the input-side
    # supply from the output-side one.
    pgs = [("VDDL", "primary_power")] + (
        [("VDD", "primary_power")] if kind == "LH" else []) + [
        ("VSS", "primary_ground")]
    in_pg, out_pg = ("VDDL", "VDD") if kind == "LH" else ("VDDL", "VDDL")
    return (head(name, 0.08, 0.08 * LEAK_PER_AREA * leak_f)
            + f"    is_level_shifter : true;\n"
            f"    level_shifter_type : {kind};\n"
            f"    input_voltage_range({vin - 0.05:.2f}, {vin + 0.05:.2f});\n"
            f"    output_voltage_range({vout - 0.05:.2f}, {vout + 0.05:.2f});\n"
            + "".join(f"    pg_pin({n}) {{\n      voltage_name : {n};\n"
                      f"      pg_type : {t};\n    }}\n" for n, t in pgs)
            + inpin("A", CAP, f"related_power_pin : {in_pg}",
                    "related_ground_pin : VSS") +
            "    pin(Y) {\n      direction : output;\n      max_capacitance : 0.08;\n"
            f"      related_power_pin : {out_pg};\n"
            "      related_ground_pin : VSS;\n"
            "      function : \"A\";\n" + arc("A", "positive_unate", 0.040, f)
            + "    }\n  }\n")


def ls_lib(corner=None):
    """The level shifters at `corner` (None: the corner-less mini_ls, which
    is the tt cells). A shifter straddles both supplies, so its delay and
    leakage factors are the mean of the two supplies' corner factors."""
    cd, cl = (1.0, 1.0) if corner is None else (
        sum(CORNERS[corner][2].values()) / len(SUPPLIES),
        sum(CORNERS[corner][3].values()) / len(SUPPLIES))
    name = "mini_ls" if corner is None else f"mini_ls_{corner}"
    what = ("level shifters between the 0.55 V and 0.75 V supplies"
            + ("" if corner is None else f", corner {corner}"))
    proc, temp = (1.0, 25) if corner is None else CORNERS[corner][:2]
    s = header(name, 0.75, what, proc, temp)
    # The supplies' nominal voltages, for the cells' pg_pins.
    s += ("  voltage_map(VDD, 0.75);\n  voltage_map(VDDL, 0.55);\n"
          "  voltage_map(VSS, 0.0);\n")
    s += level_shifter("MLSLH_X1", "LH", 0.55, 0.75, 1.3 * cd, cl)
    s += level_shifter("MLSHL_X1", "HL", 0.75, 0.55, 1.0 * cd, cl)
    return name, s + "}\n"


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    libs = [flavour_lib(vt, sup, c) for c in CORNERS for sup in SUPPLIES
            for vt in FLAVOURS]
    libs.append(ls_lib())
    libs += [ls_lib(c) for c in CORNERS]
    for lib, text in libs:
        (out / f"{lib}.lib").write_text(text)
        print(f"wrote {out / lib}.lib")


if __name__ == "__main__":
    main()
