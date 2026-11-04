"""pytest setup: scripts/ on sys.path so `import circt_flow` works, and a
tiny invented two-rail tech pack."""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

REPO = SCRIPTS.parent


def write_lib(path: Path, name: str, cells) -> Path:
    path.write_text(f"library ({name}) {{\n" + "".join(
        f"  cell ({c}) {{\n    area : 1.0;\n  }}\n" for c in cells) + "}\n")
    return path


CELLS = ["TSTINV_X1", "TSTICG_X1", "TSTTIEHI", "TSTTIELO"]


@pytest.fixture
def rails_pack(tmp_path):
    """A schema-2 pack: rails HI (default) and LO, a level-shifter lib."""
    lib = tmp_path / "lib"
    lib.mkdir()
    write_lib(lib / "hi_a.lib", "tst_hi_a", CELLS)
    write_lib(lib / "hi_b.lib", "tst_hi_b", ["TSTNAND_X1"])
    write_lib(lib / "lo_a.lib", "tst_lo_a", CELLS)
    write_lib(lib / "ls.lib", "tst_lvlshift", ["TSTLSHIFT_X1"])
    (tmp_path / "abc").mkdir()
    (tmp_path / "abc" / "lo.abc").write_text("strash\n")
    pack = tmp_path / "techpack.yaml"
    pack.write_text("""
schema: 2
name: tstrails
time_unit: ns
icg: {cell: TSTICG_X1, clk: CK, en: E, test_en: SE, out: ECK}
tie: {hi: "TSTTIEHI:H", lo: "TSTTIELO:L"}
dont_use: ["*_X0"]
flow:
  abc_read_lib_args: "-G 2"
  partitions: 1
default_rail: HI
rails:
  HI:
    voltage: 0.8
    liberties: [lib/hi_a.lib, lib/hi_b.lib]
  LO:
    voltage: 0.5
    liberties: [lib/lo_a.lib]
    flow: {abc_read_lib_args: "-l", abc_script_file: abc/lo.abc}
    dont_use: []
level_shifters:
  liberties: [lib/ls.lib]
""")
    return pack


def write_pin_lib(path: Path, name: str, cells: dict, ls: dict = None,
                  time_unit: str = "1ns", supplies: dict = None) -> Path:
    """A liberty with pins: cells {cell: {pin: (direction, function)}}
    (function None for inputs); ls {cell: (type, (in lo, in hi),
    (out lo, out hi), area)} marks level shifters; supplies {cell: ({pg pin:
    pg_type}, {signal pin: related power pin})} adds supply pins (ground
    VSS)."""
    ls = ls or {}
    supplies = supplies or {}
    out = [f"library ({name}) {{\n  time_unit : \"{time_unit}\";\n"]
    for c, pins in cells.items():
        out.append(f"  cell ({c}) {{\n")
        area = ls[c][3] if c in ls else 1.0
        out.append(f"    area : {area};\n")
        if c in ls:
            t, ir, orr, _ = ls[c]
            out.append(f"    is_level_shifter : true;\n"
                       f"    level_shifter_type : {t};\n"
                       f"    input_voltage_range({ir[0]}, {ir[1]});\n"
                       f"    output_voltage_range({orr[0]}, {orr[1]});\n")
        pgs, rel = supplies.get(c, ({}, {}))
        for pg, t in pgs.items():
            out.append(f"    pg_pin ({pg}) {{\n      pg_type : {t};\n    }}\n")
        for p, (d, fn) in pins.items():
            out.append(f"    pin ({p}) {{\n      direction : {d};\n")
            if p in rel:
                out.append(f"      related_power_pin : {rel[p]};\n"
                           "      related_ground_pin : VSS;\n")
            if fn:
                out.append(f"      function : \"{fn}\";\n")
            out.append("    }\n")
        out.append("  }\n")
    out.append("}\n")
    path.write_text("".join(out))
    return path


GATES = {"TBUF": {"A": ("input", None), "Y": ("output", "A")},
         "TINV": {"A": ("input", None), "Y": ("output", "!A")},
         "TAND": {"A": ("input", None), "B": ("input", None),
                  "Y": ("output", "A&B")},
         "TDFF": {"CK": ("input", None), "D": ("input", None),
                  "Q": ("output", None)}}
SHIFTERS = {"TLSLH": {"A": ("input", None), "Y": ("output", "A")},
            "TLSHL": {"A": ("input", None), "Y": ("output", "A")},
            "TLSLH_BIG": {"A": ("input", None), "Y": ("output", "A")}}
# TLSLH_BIG's liberty names no supplies (like a stripped library).
SHIFTER_SUPPLIES = {
    "TLSLH": ({"VDDL": "primary_power", "VDD": "primary_power",
               "VSS": "primary_ground"}, {"A": "VDDL", "Y": "VDD"}),
    "TLSHL": ({"VDDL": "primary_power", "VSS": "primary_ground"},
              {"A": "VDDL", "Y": "VDDL"})}
SHIFTER_ATTRS = {"TLSLH": ("LH", (0.4, 0.6), (0.7, 0.9), 2.0),
                 "TLSLH_BIG": ("LH", (0.4, 0.6), (0.7, 0.9), 4.0),
                 "TLSHL": ("HL", (0.7, 0.9), (0.4, 0.6), 1.5)}


@pytest.fixture
def mv_pack(tmp_path):
    """A schema-2 pack whose liberties have pins: rails HI 0.8 V (default),
    LO 0.5 V and HI2 0.8 V, and level shifters."""
    lib = tmp_path / "lib"
    lib.mkdir()
    write_pin_lib(lib / "hi.lib", "t_hi", GATES)
    write_pin_lib(lib / "lo.lib", "t_lo", GATES)
    write_pin_lib(lib / "ls.lib", "t_ls", SHIFTERS, SHIFTER_ATTRS,
                  supplies=SHIFTER_SUPPLIES)
    pack = tmp_path / "techpack.yaml"
    pack.write_text("""
schema: 2
name: tmv
time_unit: ns
icg: {cell: TAND, clk: A, en: B, test_en: B, out: Y}
tie: {}
default_rail: HI
rails:
  HI: {voltage: 0.8, liberties: [lib/hi.lib]}
  LO: {voltage: 0.5, liberties: [lib/lo.lib]}
  HI2: {voltage: 0.8, liberties: [lib/hi.lib]}
level_shifters:
  liberties: [lib/ls.lib]
""")
    return pack
