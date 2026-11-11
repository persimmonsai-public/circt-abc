"""multibit_activity.py: per-register activity from a netlist and net toggles."""
import json

from circt_flow import multibit_activity as ma

NETLIST = """module top (clk, d);
  input clk; input d;
  wire g, q0, q1, q2, q3, s;
  GATE gate_0 (.CK(clk), .E(d), .ECK(g));
  BANK2 u_x2Freg_a_cg0_mb0_1 (
    .CK(g), .D0(n0), .D1(n1), .Q0(q0), .Q1(q1));
  FF1 u_x2Freg_b_bit0 (
    .CK(clk), .D(n2), .Q(q2));
  FF1 reg_c_bit3 (.CK(clk), .D(n3), .Q(q3));
  OTHER not_a_flop (.A(q0), .Y(s));
endmodule
"""


def test_register_activity_from_counts():
    counts = {"clk": 2000, "g": 400, "q0": 100, "q1": 60, "q2": 500, "q3": 0,
              "n0": 100, "n1": 60, "n2": 500}
    r = ma.register_activity(NETLIST, counts, 1000.0)
    regs = r["registers"]
    assert set(regs) == {"reg_a_cg0", "reg_b", "reg_c"}
    a = regs["reg_a_cg0"]
    assert a["bits"] == 2 and a["q"] == 0.08 and a["clk"] == 0.2
    assert regs["reg_b"]["q"] == 0.5 and regs["reg_b"]["clk"] == 1.0
    assert regs["reg_c"]["q"] == 0.0


def test_cli_tsv_and_tcf(tmp_path):
    (tmp_path / "n.v").write_text(NETLIST)
    (tmp_path / "t.tsv").write_text(
        "net\tblock\ttoggles\tglitch\nq2\t0\t300\t0\nq2\t1\t50\t0\nclk\t0\t2000\t0\n")
    ma.main(["--netlist", str(tmp_path / "n.v"), str(tmp_path / "t.tsv"),
             "--cycles", "1000", "-o", str(tmp_path / "o.json")])
    o = json.loads((tmp_path / "o.json").read_text())
    assert o["registers"]["reg_b"]["q"] == 0.3
    tcf = ('header { duration : "1000" unit : "ns" }\n'
           'instance ("top") {\n  "q2" : "0.5 400";\n  "clk" : "0.5 2000";\n}\n')
    (tmp_path / "t.tcf").write_text(tcf)
    ma.main(["--netlist", str(tmp_path / "n.v"), str(tmp_path / "t.tcf"),
             "-o", str(tmp_path / "o2.json")])
    o = json.loads((tmp_path / "o2.json").read_text())
    assert o["registers"]["reg_b"]["q"] == 0.4
