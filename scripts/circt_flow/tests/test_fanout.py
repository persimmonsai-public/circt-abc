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
