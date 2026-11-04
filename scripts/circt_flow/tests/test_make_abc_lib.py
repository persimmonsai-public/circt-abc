"""scripts/mvt/make_abc_lib.py: pricing, dont_use filtering, header merging."""
import subprocess
import sys
from pathlib import Path

from circt_flow.config import ROOT

TOOL = ROOT / "scripts" / "mvt" / "make_abc_lib.py"


def _lib(name, cells, extra_header=""):
    out = [f"library({name}) {{\n  time_unit : \"1ns\";\n"
           "  lu_table_template(t1) {\n    variable_1 : total_output_net_capacitance;\n"
           "  }\n" + extra_header]
    for c, (area, leak, dont_use) in cells.items():
        out.append(f"  cell({c}) {{\n    area : {area} ;\n"
                   f"    cell_leakage_power : {leak} ;\n"
                   + ("    dont_use : true ;\n" if dont_use else "")
                   + "    pin(A) { direction : input; }\n  }\n")
    out.append("}\n")
    return "".join(out)


def test_filter_keep_and_headers(tmp_path):
    a = tmp_path / "a.lib"
    a.write_text(_lib("a", {"INV_R": (1.0, 2.0, False),
                            "DLY_R": (1.0, 2.0, True),
                            "PREICG_R": (2.0, 1.0, True)}))
    b = tmp_path / "b.lib"
    b.write_text(_lib("b", {"INV_S": (1.0, 20.0, False),
                            "BAD_S": (1.0, 1.0, False)},
                      "  sensitization(s2) {\n    pin_names(pin_0, pin_1);\n  }\n"))
    out = tmp_path / "m.lib"
    r = subprocess.run([sys.executable, "-I", str(TOOL), str(out), str(a),
                        str(b), "-W", "0.5", "-X", "BAD_*", "--keep", "*ICG*"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "left out 1 dont_use cell(s) and 1 matching -X" in r.stderr
    text = out.read_text()
    cells = [l.split("(")[1].split(")")[0] for l in text.splitlines()
             if l.strip().startswith("cell(")]
    assert cells == ["INV_R", "PREICG_R", "INV_S"]
    # Priced: 100 * (area + 0.5 * leakage).
    assert "area : 200 ;" in text and "area : 1100 ;" in text
    # b's header group comes along (cells may refer to it); a's template once.
    assert text.count("sensitization(s2)") == 1
    assert text.count("lu_table_template(t1)") == 1
    assert text.index("sensitization(s2)") < text.index("cell(INV_R)")
    # --keep-dont-use keeps everything marked dont_use.
    r = subprocess.run([sys.executable, "-I", str(TOOL), str(out), str(a),
                        "--keep-dont-use"], capture_output=True, text=True)
    assert r.returncode == 0 and "cell(DLY_R)" in out.read_text()
