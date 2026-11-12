"""circt_flow.fanout: pre-placement fanout buffering."""
import re
from types import SimpleNamespace

import pytest

from circt_flow import fanout

CELLS = {
    "INV": SimpleNamespace(pins={"A": "input", "Y": "output"}),
    "BUF": SimpleNamespace(pins={"A": "input", "Y": "output"}),
    "DFF": SimpleNamespace(pins={"CK": "input", "D": "input", "Q": "output"},
                           clock_pins={"CK"}),
}


def _netlist(n):
    lines = ["module top (clk, a, y);", "  input clk;", "  input a;",
             "  output y;", "  wire hot;"]
    lines += [f"  wire o{i};" for i in range(n)]
    lines.append("  DFF src (.CK(clk), .D(a), .Q(hot));")
    lines += [f"  INV \\u_x/g{i}  (.A(hot), .Y(o{i}));" for i in range(n)]
    lines.append("  assign y = hot;")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _graph(text):
    """net -> driver inst, net -> [load insts], inst -> cell."""
    drv, ld, cell = {}, {}, {}
    for m in fanout._STMT.finditer(text):
        if m.group(1) not in CELLS:
            continue
        cell[m.group(2)] = m.group(1)
        for c in fanout._CONN.finditer(m.group(3)):
            pin, net = c.group(1), c.group(2).strip()
            if CELLS[m.group(1)].pins[pin] == "output":
                drv[net] = m.group(2)
            else:
                ld.setdefault(net, []).append(m.group(2))
    return drv, ld, cell


@pytest.mark.parametrize("n,f", [(5, 4), (17, 4), (100, 16), (300, 16)])
def test_tree_reaches_every_load_within_the_limit(n, f):
    text = _netlist(n)
    out, st = fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", f)
    drv, ld, cell = _graph(out)
    if n <= f:
        assert out == text and st.nets == 0
        return
    assert st.nets == 1 and st.worst_fanout == n and st.buffers > 0
    assert all(len(v) <= f for v in ld.values())
    # Every INV is reached from src through buffers only.
    def root(inst_net):
        net, hops = inst_net, 0
        while drv[net] != "src":
            b = drv[net]
            assert cell[b] == "BUF"
            net = next(k for k, v in ld.items() if b in v)
            hops += 1
        return hops
    sinks = [i for i, c in cell.items() if c == "INV"]
    assert len(sinks) == n
    for s in sinks:
        net = next(k for k, v in ld.items() if s in v)
        root(net)
    # The driver keeps its net (the port assign reads it); everything
    # outside the moved pins is byte for byte the same.
    assert "assign y = hot;" in out and ".Q(hot)" in out
    assert len(re.findall(r"^\s*wire fob_n\d+;", out, re.M)) == st.buffers
    stripped = re.sub(r"\.A\([^)]*\)", ".A()", out)
    for line in text.splitlines():
        if line.startswith("  INV"):
            assert re.sub(r"\.A\([^)]*\)", ".A()", line) in stripped


def test_multi_driver_and_constant_nets_are_left_alone():
    text = _netlist(40).replace("  assign y = hot;\n",
                                "  DFF src2 (.CK(clk), .D(a), .Q(hot));\n")
    out, st = fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", 8)
    assert st.nets == 0 and out == text
    const = _netlist(0).replace(
        "endmodule", "".join(f"  INV k{i} (.A(1'b0), .Y());\n"
                             for i in range(40)) + "endmodule")
    out, st = fanout.buffer_fanout(const, CELLS, "BUF", "A", "Y", 8)
    assert st.nets == 0


def test_clock_nets_are_left_to_cts():
    text = _netlist(4).replace("endmodule", "".join(
        f"  DFF r{i} (.CK(hot), .D(a), .Q());\n" for i in range(40))
        + "endmodule")
    out, st = fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", 8)
    assert st.nets == 0 and out == text


def test_name_clash_is_an_error():
    text = _netlist(40).replace("wire hot;", "wire hot;\n  wire fob_n0;")
    with pytest.raises(ValueError, match="fob_n0"):
        fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", 8)


def test_parse_spec():
    pack = {"cell": "BUF_D4", "max_fanout": 24}
    s = fanout.parse_spec(pack, None, "w")
    assert s.cell == "BUF_D4" and s.max_fanout == 24 and s.input == "A"
    assert fanout.parse_spec(pack, {"max_fanout": 8}, "w").max_fanout == 8
    assert fanout.parse_spec(pack, False, "w") is None
    assert fanout.parse_spec(pack, {"enabled": False}, "w") is None
    assert fanout.parse_spec(None, None, "w") is None
    with pytest.raises(KeyError, match="needs a cell"):
        fanout.parse_spec(None, {"max_fanout": 8}, "w")
    with pytest.raises(KeyError, match="unknown key"):
        fanout.parse_spec({"cell": "B", "fanout": 3}, None, "w")
    with pytest.raises(KeyError, match=">= 2"):
        fanout.parse_spec({"cell": "B", "max_fanout": 1}, None, "w")


def test_modules_are_buffered_on_their_own_rail():
    # dom's net fans out inside dom (buffered with dom's rail's cell);
    # top's net reaches dom only through a module port (left alone).
    dom = ["module dom (clk, x, o);", "  input clk;", "  input x;",
           "  output o;", "  wire h;"]
    dom.append("  DFF s (.CK(clk), .D(x), .Q(h));")
    dom += [f"  INV i{i} (.A(h), .Y());" for i in range(20)]
    dom += ["  assign o = h;", "endmodule"]
    top = ["module top (clk, a);", "  input clk;", "  input a;", "  wire t;",
           "  DFF s (.CK(clk), .D(a), .Q(t));"]
    top += [f"  dom u{i} (.clk(clk), .x(t), .o());" for i in range(20)]
    top += ["endmodule"]
    text = "\n".join(dom + top) + "\n"
    spec = fanout.FanoutSpec(cell="BUF", max_fanout=8)
    out, per = fanout.buffer_modules(text, "top", {"dom": "LV", "top": "HV"},
                                     {"LV": CELLS, "HV": CELLS}, spec)
    assert set(per) == {"dom"} and per["dom"]["rail"] == "LV"
    assert per["dom"]["nets"] == 1 and per["dom"]["buffers"] == 3
    # A submodule input fanning out inside it is buffered there; the top's
    # own inputs are not.
    res = ["module res (x, a);", "  input [1:0] x;", "  input a;"]
    res += [f"  INV r{i} (.A(x[1]), .Y());" for i in range(20)]
    res += [f"  INV q{i} (.A(a), .Y());" for i in range(20)]
    res += ["endmodule"]
    t2 = "\n".join(res) + "\n" + "\n".join(
        ["module top (a);", "  input a;"]
        + [f"  INV p{i} (.A(a), .Y());" for i in range(20)]
        + ["  res u (.x(2'b0), .a(a));", "endmodule"]) + "\n"
    _, per2 = fanout.buffer_modules(t2, "top", {"res": "HV", "top": "HV"},
                                    {"HV": CELLS}, spec)
    assert set(per2) == {"res"} and per2["res"]["nets"] == 2
    i = out.index("module top")
    assert out[i:] == text[text.index("module top"):]
    assert out.count("BUF fob_b") == 3 and "assign o = h;" in out


def test_annotated_driver_keeps_its_loads_up_to_its_own_limit(tmp_path):
    """A driver annotated circt_abc.max_fanout=N (mapper attribute, read from the mapped MLIR) is
    left unbuffered up to N loads and buffered to N beyond; other nets keep the global limit."""
    mlir = tmp_path / "x_mapped.mlir"
    mlir.write_text('    %a = hw.instance "u_x/src" @DFF(CK: %c: i1, D: %d: i1) -> (Q: i1) '
                    '{circt_abc.max_fanout = 24 : i32}\n')
    lim = fanout.net_limits_from_mlir([mlir])
    assert lim == {"u_x2Fsrc": 24}
    text = _netlist(20).replace("DFF src", "DFF u_x2Fsrc")
    out, st = fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", 8, net_limits=lim)
    assert out == text and st.nets == 0           # 20 loads <= 24: untouched
    out, st = fanout.buffer_fanout(text, CELLS, "BUF", "A", "Y", 8)
    assert st.nets == 1                           # without the annotation: buffered
    out, st = fanout.buffer_fanout(_netlist(60).replace("DFF src", "DFF u_x2Fsrc"), CELLS,
                                   "BUF", "A", "Y", 8, net_limits=lim)
    drv, ld, cell = _graph(out)
    assert max(len(v) for v in ld.values()) <= 24


def test_multibit_register_output_is_buffered_unless_bundle_pins_off():
    """A multibit register's Q0..Qn are bundle members: known to the cell under `bundle_pins`, not `pins` (which names the group).
    Seen by default; with bundle_pins=False the member pins are unknown (the net is not seen)."""
    mb = SimpleNamespace(pins={"CK": "input", "D": "input", "Q": "output"}, clock_pins={"CK"},
                         bundle_pins={"D0": "input", "Q0": "output", "Q1": "output"})
    cells = dict(CELLS, MB=mb)
    lines = ["module top (clk, a, y);", "  input clk;", "  input a;", "  output y;", "  wire hot;"]
    lines += [f"  wire o{i};" for i in range(20)]
    lines.append("  MB src (.CK(clk), .D0(a), .Q0(hot));")
    lines += [f"  INV \\u_x/g{i}  (.A(hot), .Y(o{i}));" for i in range(20)]
    lines.append("endmodule")
    text = "\n".join(lines) + "\n"
    out0, st0 = fanout.buffer_fanout(text, cells, "BUF", "A", "Y", 8, bundle_pins=False)
    assert st0.nets == 0 and out0 == text            # switched off: the net is not seen
    out1, st1 = fanout.buffer_fanout(text, cells, "BUF", "A", "Y", 8)
    assert st1.nets == 1 and st1.buffers >= 2 and st1.worst_fanout == 20
    out2, st2 = fanout.buffer_fanout(text, cells, "BUF", "A", "Y", 8, bundle_pins=True)
    assert out2 == out1 and st2.nets == 1            # the explicit key gives the default's netlist
    # a cell without bundle members (single-bit flops, plain gates) is not affected by the switch
    plain = text.replace("MB src (.CK(clk), .D0(a), .Q0(hot));", "DFF src (.CK(clk), .D(a), .Q(hot));")
    assert fanout.buffer_fanout(plain, cells, "BUF", "A", "Y", 8, bundle_pins=False)[0] == \
        fanout.buffer_fanout(plain, cells, "BUF", "A", "Y", 8)[0]


def test_parse_spec_bundle_pins_key():
    sp = fanout.parse_spec(None, {"cell": "BUF", "bundle_pins": True}, "t")
    assert sp.bundle_pins is True
    assert fanout.parse_spec(None, {"cell": "BUF"}, "t").bundle_pins is True      # the default
    assert fanout.parse_spec(None, {"cell": "BUF", "bundle_pins": False}, "t").bundle_pins is False
    # the pack can switch it off and a flow config back on, key by key
    assert fanout.parse_spec({"cell": "BUF", "bundle_pins": False}, None, "t").bundle_pins is False
    assert fanout.parse_spec({"cell": "BUF", "bundle_pins": False}, {"bundle_pins": True}, "t").bundle_pins is True
    for bad in ("yes", 1, None):
        with pytest.raises(KeyError, match="bundle_pins"):
            fanout.parse_spec(None, {"cell": "BUF", "bundle_pins": bad}, "t")


def test_power_flow_modules_see_bundle_pins():
    """buffer_modules (the power flow's per-module stage) follows the spec's bundle_pins like the flat stage."""
    mb = SimpleNamespace(pins={"CK": "input", "D": "input", "Q": "output"}, clock_pins={"CK"},
                         bundle_pins={"D0": "input", "Q0": "output"})
    cells = dict(CELLS, MB=mb)
    lines = ["module top (clk, a);", "  input clk;", "  input a;", "  wire hot;"]
    lines += [f"  wire o{i};" for i in range(20)]
    lines.append("  MB src (.CK(clk), .D0(a), .Q0(hot));")
    lines += [f"  INV g{i} (.A(hot), .Y(o{i}));" for i in range(20)]
    lines.append("endmodule")
    text = "\n".join(lines) + "\n"
    on = fanout.FanoutSpec(cell="BUF")
    off = fanout.FanoutSpec(cell="BUF", bundle_pins=False)
    out_on, per_on = fanout.buffer_modules(text, "top", {"top": "R"}, {"R": cells}, on)
    out_off, per_off = fanout.buffer_modules(text, "top", {"top": "R"}, {"R": cells}, off)
    assert per_on["top"]["nets"] == 1 and out_on != text
    assert per_off == {} and out_off == text
