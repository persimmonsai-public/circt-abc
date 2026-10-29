#!/usr/bin/env python3
"""Write template.lib, the template pack's liberty: a small invented cell
library (no PDK data) with enough cells for the flow to map a real design,
and NLDM tables simple enough to read: delay = intrinsic + k * load + 0.1 *
input slew, in ns and pF.

    python3 make_lib.py > template.lib
"""

# name, area, {output: function}, inputs, intrinsic delay (ns)
COMB = [
    ("TINV", 0.02, {"Y": "(!A)"}, "A", 0.010),
    ("TBUF", 0.03, {"Y": "(A)"}, "A", 0.018),
    ("TNAND2", 0.03, {"Y": "(!(A&B))"}, "AB", 0.014),
    ("TNOR2", 0.03, {"Y": "(!(A|B))"}, "AB", 0.018),
    ("TAND2", 0.04, {"Y": "(A&B)"}, "AB", 0.024),
    ("TOR2", 0.04, {"Y": "(A|B)"}, "AB", 0.028),
    ("TNAND3", 0.04, {"Y": "(!(A&B&C))"}, "ABC", 0.020),
    ("TNOR3", 0.04, {"Y": "(!(A|B|C))"}, "ABC", 0.026),
    ("TXOR2", 0.06, {"Y": "(A^B)"}, "AB", 0.030),
    ("TXNOR2", 0.06, {"Y": "(!(A^B))"}, "AB", 0.030),
    ("TAOI21", 0.04, {"Y": "(!((A&B)|C))"}, "ABC", 0.020),
    ("TOAI21", 0.04, {"Y": "(!((A|B)&C))"}, "ABC", 0.020),
    ("TMUX2", 0.07, {"Y": "((S&B)|(!S&A))"}, "ABS", 0.032),
    ("TADDH", 0.09, {"CO": "(A&B)", "S": "(A^B)"}, "AB", 0.030),
    ("TADDF", 0.17, {"CO": "(((A^B)&CI)|(A&B))", "S": "(A^B^CI)"},
     ["A", "B", "CI"], 0.045),
]
SLEW = (0.01, 0.10)
LOAD = (0.001, 0.010)
CAP = 0.0015


def table(kind: str, intrinsic: float, k: float) -> str:
    rows = ", ".join(
        '"' + ", ".join(f"{intrinsic + k * c + 0.1 * s:.4f}" for c in LOAD)
        + '"' for s in SLEW)
    return f"        {kind}(nldm) {{ values({rows}); }}\n"


def arc(related: str, sense: str, intrinsic: float, extra: str = "") -> str:
    return (f"      timing() {{\n        related_pin : \"{related}\";\n"
            f"        timing_sense : {sense};\n{extra}"
            + table("cell_rise", intrinsic, 4.0)
            + table("cell_fall", intrinsic, 3.5)
            + table("rise_transition", 0.008, 5.0)
            + table("fall_transition", 0.007, 4.5)
            + "      }\n")


def inpin(name: str, *extra: str) -> str:
    # One attribute per line, as real liberties have it: line-based readers
    # (gen_liberty_blackbox_stubs.py) expect that.
    return (f"    pin({name}) {{\n      direction : input;\n"
            f"      capacitance : {CAP};\n"
            + "".join(f"      {e};\n" for e in extra) + "    }\n")


def comb_cell(name, area, outs, ins, d) -> str:
    s = f"  cell({name}) {{\n    area : {area};\n"
    s += "".join(inpin(i) for i in ins)
    for o, fn in outs.items():
        s += (f"    pin({o}) {{\n      direction : output;\n"
              f"      max_capacitance : 0.08;\n      function : \"{fn}\";\n")
        for i in ins:
            s += arc(i, "non_unate", d)
        s += "    }\n"
    return s + "  }\n"


def setup_hold(related: str = "CK") -> str:
    return "".join(
        f"      timing() {{\n        related_pin : \"{related}\";\n"
        f"        timing_type : {t}_rising;\n"
        f"        rise_constraint(scalar) {{ values(\"{v}\"); }}\n"
        f"        fall_constraint(scalar) {{ values(\"{v}\"); }}\n      }}\n"
        for t, v in (("setup", 0.020), ("hold", 0.005)))


def flop(name: str, area: float, async_pin: str = "", kind: str = "") -> str:
    s = f"  cell({name}) {{\n    area : {area};\n"
    s += f"    ff(IQ, IQN) {{\n      clocked_on : \"CK\";\n      next_state : \"D\";\n"
    if async_pin:
        s += f"      {kind} : \"!{async_pin}\";\n"
    s += "    }\n"
    s += inpin("CK", "clock : true")
    s += (f"    pin(D) {{\n      direction : input;\n      capacitance : {CAP};\n"
          f"      nextstate_type : data;\n" + setup_hold() + "    }\n")
    if async_pin:
        s += inpin(async_pin)
    s += ("    pin(Q) {\n      direction : output;\n      max_capacitance : 0.08;\n"
          "      function : \"IQ\";\n"
          + arc("CK", "non_unate", 0.050, "        timing_type : rising_edge;\n"))
    if async_pin:
        s += arc(async_pin, "positive_unate" if kind == "preset"
                 else "negative_unate", 0.040,
                 f"        timing_type : {kind};\n")
    return s + "    }\n  }\n"


def icg() -> str:
    return """  cell(TICG) {
    area : 0.12;
    clock_gating_integrated_cell : "latch_posedge_precontrol";
    pin_name_map(CK, E, SE, ECK);
    statetable("CK E SE", "IQ") {
      table : "L L L : - : L, L L H : - : H, L H L : - : H, L H H : - : H, H - - : - : N";
    }
    pin(IQ) {
      direction : internal;
      internal_node : "IQ";
    }
""" + inpin("CK", "clock : true", "clock_gate_clock_pin : true") + f"""    pin(E) {{
      direction : input;
      capacitance : {CAP};
      clock_gate_enable_pin : true;
{setup_hold()}    }}
    pin(SE) {{
      direction : input;
      capacitance : {CAP};
      clock_gate_test_pin : true;
{setup_hold()}    }}
    pin(ECK) {{
      direction : output;
      max_capacitance : 0.08;
      clock_gate_out_pin : true;
      state_function : "(CK&IQ)";
{arc("CK", "positive_unate", 0.030, "")}    }}
  }}
"""


def tie(name: str, pin: str, fn: str) -> str:
    return (f"  cell({name}) {{\n    area : 0.02;\n"
            f"    pin({pin}) {{\n      direction : output;\n"
            f"      function : \"{fn}\";\n    }}\n  }}\n")


def main() -> None:
    print("""/* template.lib: an invented cell library for the circt-abc tech-pack
   template (examples/techpack-template). Generated by make_lib.py; no
   foundry or PDK data. Units: ns, pF. */
library(template) {
  delay_model : table_lookup;
  time_unit : "1ns";
  capacitive_load_unit(1, pf);
  voltage_unit : "1V";
  current_unit : "1mA";
  leakage_power_unit : "1nW";
  pulling_resistance_unit : "1kohm";
  nom_voltage : 1.0;
  nom_temperature : 25;
  nom_process : 1;
  default_max_transition : 0.2;
  input_threshold_pct_rise : 50;
  input_threshold_pct_fall : 50;
  output_threshold_pct_rise : 50;
  output_threshold_pct_fall : 50;
  slew_lower_threshold_pct_rise : 20;
  slew_lower_threshold_pct_fall : 20;
  slew_upper_threshold_pct_rise : 80;
  slew_upper_threshold_pct_fall : 80;
  lu_table_template(nldm) {
    variable_1 : input_net_transition;
    variable_2 : total_output_net_capacitance;
    index_1("%s");
    index_2("%s");
  }
""" % (", ".join(map(str, SLEW)), ", ".join(map(str, LOAD))))
    for c in COMB:
        print(comb_cell(*c), end="")
    print(flop("TDFF", 0.20), end="")
    print(flop("TDFFRN", 0.24, "RN", "clear"), end="")
    print(flop("TDFFSN", 0.24, "SN", "preset"), end="")
    print(icg(), end="")
    print(tie("TTIEHI", "H", "1"), end="")
    print(tie("TTIELO", "L", "0"), end="")
    print("}")


if __name__ == "__main__":
    main()
