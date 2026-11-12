"""`pnr.tie_hi` / `pnr.tie_lo` override the pack's tie cells (stages.orfs_pnr)."""
from types import SimpleNamespace

import pytest

from circt_flow import orfs_pnr as pnr_mod
from circt_flow import stages


def _run(tmp_path, monkeypatch, pnr, tie):
    net = tmp_path / "n.v"
    net.write_text("")
    cfg = SimpleNamespace(
        design=SimpleNamespace(pnr=pnr, tie=tie, top_module="t",
                               clock_period_ps=1000, pack_pnr={}),
        openroad_netlist=net, pnr_dir=tmp_path / "pnr")
    seen = {}
    monkeypatch.setattr(pnr_mod, "main",
                        lambda argv: seen.setdefault("argv", argv) and 0)
    stages.orfs_pnr(cfg)
    return seen["argv"]


def _tie(argv):
    def val(name):
        for i, a in enumerate(argv):
            if a == name:
                return argv[i + 1]
            if a.startswith(name + "="):
                return a[len(name) + 1:]
        return None
    return val("--tie-hi"), val("--tie-lo")


def test_pack_tie_cells_by_default(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch, {"clock_port": "clk"},
                {"hi": "TH:H", "lo": "TL:L"})
    assert _tie(argv) == ("TH:H", "TL:L")


def test_pnr_tie_overrides_pack(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch,
                {"clock_port": "clk", "tie_hi": "XH:H", "tie_lo": "XL:L"},
                {"hi": "TH:H", "lo": "TL:L"})
    assert _tie(argv) == ("XH:H", "XL:L")


def test_pnr_tie_without_pack_tie(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch,
                {"clock_port": "clk", "tie_hi": "XH:H", "tie_lo": "XL:L"}, None)
    assert _tie(argv) == ("XH:H", "XL:L")


def test_no_tie_cells_no_flags(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch, {"clock_port": "clk"}, None)
    assert _tie(argv) == (None, None)


def test_untied_constants_warning():
    net = "module t(a, y);\n  assign z = 1'h0;\n  X u (.A(1'h1), .Y(y));\nendmodule\n"
    assert pnr_mod.count_constants(net) == 2
    w = pnr_mod.untied_constants_warning(net, {})
    assert "2 constant" in w and "tie" in w
    assert pnr_mod.untied_constants_warning(net, {"hi": "TH:H", "lo": "TL:L"}) == ""
    assert pnr_mod.untied_constants_warning("module t; endmodule\n", {}) == ""


def test_tied_netlist_has_no_literals():
    net = "module t(a, y);\n  assign z = 1'h0;\n  X u (.A(1'h1), .Y(y));\nendmodule\n"
    out, n = pnr_mod.tie_constants(net, ("TH", "H"), ("TL", "L"))
    assert n == 2 and pnr_mod.count_constants(out) == 0


def test_constant_in_concatenation_is_tied():
    net = ("module t(a, y);\n  wire [2:0] y;\n  assign y = {1'h0, a, 1'h1};\n"
           "endmodule\n")
    assert pnr_mod.count_constants(net) == 2
    out, n = pnr_mod.tie_constants(net, ("TH", "H"), ("TL", "L"))
    assert n == 2 and pnr_mod.count_constants(out) == 0
    assert "{tie_const_" in out and "TH tie_const_" in out
    assert "TL tie_const_" in out


def test_adjacent_literals_are_all_counted():
    net = "module t(a, y);\n  assign y = {1'h0, 1'h1, a, 1'b1};\nendmodule\n"
    assert pnr_mod.count_constants(net) == 3
    out, n = pnr_mod.tie_constants(net, ("TH", "H"), ("TL", "L"))
    assert n == 3 and pnr_mod.count_constants(out) == 0


def test_literals_in_comments_strings_and_parameter_overrides_are_left_alone():
    net = ("module t(a, y);\n"
           "  // assign q = 1'h0;   .P(1'h1)  {1'h0, a}\n"
           "  /* assign r = 1'h1;\n     .Q(1'h0) */\n"
           '  initial $display("{1\'h0, a} .P(1\'h1)");\n'
           "  X #(.W(1'h1), .V({1'h0, 1'h1})) u (.A(a), .Y(y));\n"
           "  Z #(1'h1) v (.A(a));\n"
           "endmodule\n")
    assert pnr_mod.count_constants(net) == 0
    out, n = pnr_mod.tie_constants(net, ("TH", "H"), ("TL", "L"))
    assert n == 0 and out == net
    # a real literal next to them is still tied
    net2 = net.replace("(.A(a), .Y(y))", "(.A(1'h1), .Y(y))")
    assert pnr_mod.count_constants(net2) == 1
    out2, n2 = pnr_mod.tie_constants(net2, ("TH", "H"), ("TL", "L"))
    assert n2 == 1 and "#(.W(1'h1), .V({1'h0, 1'h1}))" in out2 and "// assign q = 1'h0;" in out2


# Awkward netlist text: every input must give the same count from
# count_constants and tie_constants, leave nothing countable behind, and keep
# what is not a net of the design (names, comments, strings, parameters).
TIE_CASES = {
 "esc_ident_with_lit": "module t(a,y);\n wire \\n{1'b0,x} ;\n assign y = {a, 1'b1};\nendmodule\n",
 "esc_ident_slashes": "module t(a,y);\n wire \\a//b ;\n assign y = {a, 1'b1};\nendmodule\n",
 "esc_ident_quote": "module t(a,y);\n wire \\q\" ;\n assign y = {a, 1'b1};\nendmodule\n",
 "esc_ident_hash": "module t(a,y);\n wire \\z#( ;\n assign y = {a, 1'b1};\nendmodule\n",
 "esc_ident_assign": "module t(a,y);\n assign \\q,1'b0 = 1'b1;\nendmodule\n",
 "signed": "module t(a,y);\n assign y = {1'sb0, a};\nendmodule\n",
 "upper": "module t(a,y);\n assign y = {1'B0, a, 1'H1};\nendmodule\n",
 "decimal": "module t(a,y);\n assign y = {1'd0, a, 1'o1};\nendmodule\n",
 "xz": "module t(a,y);\n assign y = {1'bx, a, 1'bz, 1'b0};\nendmodule\n",
 "spaced": "module t(a,y);\n assign y = {1 'b0, a, 1' b1};\nendmodule\n",
 "underscore": "module t(a,y);\n assign y = {1'b0_, a};\nendmodule\n",
 "nested": "module t(a,y);\n assign y = {{1'b0, a}, {2{1'b1}}, {a, {1'b0}}};\nendmodule\n",
 "repl": "module t(a,y);\n assign y = {4{1'b0}};\nendmodule\n",
 "wide_not_touched": "module t(a,y);\n assign y = {11'b0, a, 21'h1, 8'b1};\nendmodule\n",
 "pin_vs_assign": "module t(a,y);\n X u (.A(1'b1), .B({1'b0, a}), .C(1'b0 ), .D (1'b1));\n assign q = 1'b0;\nendmodule\n",
 "attr": "module t(a,y);\n (* keep = \"{1'b0, a}\" *) X u (.A(a));\n (* init = 1'b0 *) wire w;\n assign y = {1'b0,a};\nendmodule\n",
 "multiline": "module t(a,y);\n assign y = {\n 1'b0\n ,\n a ,\n 1'b1\n };\nendmodule\n",
 "comment_in_concat": "module t(a,y);\n assign y = {1'b0 /*c*/, a};\nendmodule\n",
 "line_comment_in_concat": "module t(a,y);\n assign y = {1'b0 // c\n , a};\nendmodule\n",
 "param_decl": "module t(a,y);\n parameter P = {1'b0, 1'b1};\n localparam Q = {a, 1'b1};\nendmodule\n",
 "positional": "module t(a,y);\n X u (a, 1'b0, 1'b1, y);\nendmodule\n",
 "case_labels": "module t(a,y);\n always @* case (a) 1'b0, 1'b1: y = 1; endcase\nendmodule\n",
 "funcall": "module t(a,y);\n assign y = f(a, 1'b0, 1'b1);\nendmodule\n",
 "ternary": "module t(a,y);\n assign y = a ? {1'b0,a} : {a,1'b1};\nendmodule\n",
 "delay": "module t(a,y);\n and #(1'b0) g (y,a,a);\n and #1 g2 (y,a,a);\nendmodule\n",
 "string_with_quote_esc": "module t(a,y);\n initial $display(\"a\\\"{1'b0, a}\");\n assign y = {1'b0, a};\nendmodule\n",
 "unterminated_comment": "module t(a,y);\n assign y = {1'b0, a}; /* open\n assign z = {1'b1, a};\nendmodule\n",
 "slash_in_division": "module t(a,y);\n assign y = a / 2; assign z = {1'b0, a}; // x\nendmodule\n",
 "hash_not_paren": "module t(a,y);\n assign y = a #5;\n assign z = {1'b0, a};\nendmodule\n",
 "multi_module": "module t(a,y);\n assign y = {1'b0, a};\nendmodule\nmodule u(b);\n assign q = {1'b1,b};\nendmodule\n",
 "assign_midline": "module t(a,y);\n wire w; assign w = 1'b0;\nendmodule\n",
 "x_in_assign_rhs_concat_lhs": "module t(a,y);\n assign {y, z} = {1'b0, a};\nendmodule\n",
}

TIE_COUNTS = {'esc_ident_with_lit': 1, 'esc_ident_slashes': 1, 'esc_ident_quote': 1, 'esc_ident_hash': 1, 'esc_ident_assign': 1, 'signed': 1, 'upper': 2, 'decimal': 2, 'xz': 1, 'spaced': 2, 'underscore': 1, 'nested': 3, 'repl': 1, 'wide_not_touched': 0, 'pin_vs_assign': 5, 'attr': 1, 'multiline': 2, 'comment_in_concat': 1, 'line_comment_in_concat': 1, 'param_decl': 0, 'positional': 2, 'case_labels': 0, 'funcall': 2, 'ternary': 2, 'delay': 0, 'string_with_quote_esc': 1, 'unterminated_comment': 1, 'slash_in_division': 1, 'hash_not_paren': 1, 'multi_module': 2, 'assign_midline': 1, 'x_in_assign_rhs_concat_lhs': 1}


@pytest.mark.parametrize("name", sorted(TIE_CASES))
def test_tie_constants_on_awkward_text(name):
    src = TIE_CASES[name]
    n = pnr_mod.count_constants(src)
    out, tied = pnr_mod.tie_constants(src, ("TH", "H"), ("TL", "L"))
    assert n == tied == TIE_COUNTS[name]
    assert pnr_mod.count_constants(out) == 0
    if n == 0:
        assert out == src
    else:
        assert out.count("TH tie_const_") + out.count("TL tie_const_") == n


def test_tie_rewrite_keeps_escaped_names_comments_strings_and_parameters():
    s = TIE_CASES
    out, _ = pnr_mod.tie_constants(s["esc_ident_with_lit"], ("TH", "H"), ("TL", "L"))
    assert "wire \\n{1'b0,x} ;" in out                       # the identifier is untouched
    out, _ = pnr_mod.tie_constants(s["esc_ident_quote"], ("TH", "H"), ("TL", "L"))
    assert 'wire \\q" ;' in out and "{a, tie_const_1_net}" in out   # and does not hide what follows
    out, _ = pnr_mod.tie_constants(s["param_decl"], ("TH", "H"), ("TL", "L"))
    assert out == s["param_decl"]
    out, _ = pnr_mod.tie_constants(s["attr"], ("TH", "H"), ("TL", "L"))
    assert "(* keep = \"{1'b0, a}\" *)" in out and "(* init = 1'b0 *)" in out
    out, _ = pnr_mod.tie_constants(s["esc_ident_assign"], ("TH", "H"), ("TL", "L"))
    assert "(.H(\\q,1'b0 ))" in out                          # the escaped target keeps its space


def test_each_module_declares_the_ties_it_uses():
    out, n = pnr_mod.tie_constants(TIE_CASES["multi_module"], ("TH", "H"), ("TL", "L"))
    m1, m2 = out.split("endmodule")[0], out.split("endmodule")[1]
    assert "wire tie_const_1_net;" in m1 and "wire tie_const_2_net;" not in m1
    assert "wire tie_const_2_net;" in m2 and "tie_const_2 (.H(tie_const_2_net))" in m2
    with pytest.raises(ValueError, match="outside any module"):
        pnr_mod.tie_constants("assign q = {1'b0, a};\n", ("TH", "H"), ("TL", "L"))


TIE = (("TH", "H"), ("TL", "L"))


def test_a_module_on_one_line_gets_its_declarations_before_endmodule():
    src = "module t(input a, output y); X u(.A(a), .B(1'b1), .Y(y)); endmodule\n"
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 1
    head, tail = out.split("endmodule")[0], out.split("endmodule")[1]
    assert "wire tie_const_1_net;" in head and "TH tie_const_1" in head
    assert head.index("wire tie_const_1_net;") < head.index("X u(")     # declared before its use
    assert tail.strip() == ""


@pytest.mark.parametrize("src", [
    "module t(a,y);\n defparam s.P = {1'b0, 1'b1};\n X s(a);\nendmodule\n",
    "module t(a,y);\n (* foo = {1'b0, 1'b1} *) X u(a);\nendmodule\n",
    "module t(a,y);\n specify $setup(a, posedge a, 1'b0); endspecify\nendmodule\n",
    "module t(a,y);\n function f; input x; f = {1'b0, x}; endfunction\nendmodule\n",
    "module t(a,y);\n generate if (1) begin : g X u(.A({1'b0, a})); end endgenerate\nendmodule\n",
    "`define ZERO {1'b0, a}\nmodule t(a,y);\nendmodule\n",
    "module t(a,y);\n  `define Q(x) {1'b0, x} \\\n   more {1'b1, x}\nendmodule\n",
])
def test_literals_the_language_requires_or_that_are_not_structure_are_left_alone(src):
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 0 and out == src and pnr_mod.count_constants(src) == 0


def test_continuous_assignments_and_net_declarations_are_tied():
    src = ("module t(a,y);\n assign y[3] = 1'b0;\n wire w = 1'b1;\n"
           " assign p = 1'b0, q = 1'b1;\n assign #1 r = 1'b1;\nendmodule\n")
    assert pnr_mod.count_constants(src) == 5
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 5 and pnr_mod.count_constants(out) == 0
    assert "assign y[3] = tie_const_1_net;" in out and "wire w = tie_const_2_net;" in out
    assert "assign p = tie_const_3_net, q = tie_const_4_net;" in out


def test_constants_in_expressions_are_not_rewritten_but_are_counted_and_warned_about():
    src = ("module t(a,y);\n assign y = a ? 1'b1 : 1'b0;\n assign z = a & 1'b1;\n"
           " assign w = (1'b1);\n always @(negedge 1'b1) q <= a;\nendmodule\n")
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 0 and out == src
    assert pnr_mod.remaining_constants(src) == 5
    tied = {"hi": "TH:H", "lo": "TL:L"}
    assert "5 one-bit constant(s)" in pnr_mod.untied_constants_warning(src, tied)
    assert "5 constant(s)" in pnr_mod.untied_constants_warning(src, {})
    assert pnr_mod.untied_constants_warning("module t; endmodule\n", tied) == ""


def test_the_tie_names_avoid_names_the_netlist_already_uses_and_a_second_pass_changes_nothing():
    src = ("module t(a,y);\n wire tie_const_1_net;\n TH tie_const_1 (.H(tie_const_1_net));\n"
           " assign y = {1'b0, a};\nendmodule\n")
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 1 and "wire tie_const_2_net;" in out and out.count("wire tie_const_1_net;") == 1
    again, n2 = pnr_mod.tie_constants(out, *TIE)
    assert n2 == 0 and again == out
    # the same for a netlist that has been through the rewrite once
    once, _ = pnr_mod.tie_constants(TIE_CASES["pin_vs_assign"], *TIE)
    twice, n3 = pnr_mod.tie_constants(once, *TIE)
    assert n3 == 0 and twice == once


def test_macromodule_and_a_first_positional_literal_are_tied():
    out, n = pnr_mod.tie_constants("macromodule t(a,y);\n assign y = {1'b0, a};\nendmodule\n", *TIE)
    assert n == 1 and "wire tie_const_1_net;" in out.split("assign")[0]
    out, n = pnr_mod.tie_constants("module t(a,y);\n X u (1'b0, a);\nendmodule\n", *TIE)
    assert n == 1 and "X u (tie_const_1_net, a);" in out


def test_tying_cost_grows_with_the_number_of_ties_not_with_their_square():
    import time
    body = "".join(f" assign y{i} = {{1'b0, a}};\n" for i in range(60000))
    src = "module t(a);\n" + body + "endmodule\n"
    t0 = time.monotonic()
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 60000 and pnr_mod.count_constants(out) == 0
    assert time.monotonic() - t0 < 8             # quadratic: well over this


def test_an_operand_of_an_operator_is_not_rewritten_but_is_counted():
    src = ("module t(a,y);\n assign y = a == 1'b0;\n assign z = a != 1'b1;\n"
           " assign w = a <= 1'b1;\n assign v = a >= 1'b0;\nendmodule\n")
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 0 and out == src
    assert pnr_mod.remaining_constants(src) == 4


def test_pin_literals_in_a_generate_body_are_reported_not_tied():
    src = ("module t(a,y);\n generate X u(.A(1'b1), .B({1'b0, a}), .C(a)); endgenerate\n"
           " assign y = 1'b0;\nendmodule\n")
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 1 and ".A(1'b1)" in out and "{1'b0, a}" in out     # only the assign is tied
    assert pnr_mod.remaining_constants(out) == 1                   # the pin literal is counted
    tied = {"hi": "TH:H", "lo": "TL:L"}
    assert "1 one-bit constant(s)" in pnr_mod.untied_constants_warning(src, tied)


# ---------------------------------------------------------------------------
# The fast analysis equals the exact tokenizer (which stays as the reference
# `_analyze_exact`), and costs a small multiple of a plain regex pass.
# ---------------------------------------------------------------------------

_FRAGMENTS = [
    " assign y = {1'b0, a};", " assign y[1] = 1'h0;", " assign z = 1'd1;",
    " X u (.A(1'b1), .B(a), .C(1'h0 ));", " X u (a, 1'b0, 1'b1, y);",
    " assign y = a ? 1'h1 : a;", " assign w = a == 1'b0;",
    " wire w = 1'b1;", " wire \\n{1'b0,x} ;", " wire \\a//b ;",
    " wire \\q\" ;", " wire \\z#( ;", " assign \\q,1'b0 = 1'b1;",
    " // c 1'b0 ;", " /* 1'b1 ; assign x = 1'b0; */", " (* init = 1'b0 *)",
    " (* keep = \"{1'b0, a}\" *) X u (.A(a));",
    " initial $display(\"a;1'b0\");", " parameter P = {1'b0, 1'b1};",
    " localparam Q = {a, 1'b1};", " defparam u.P = 1'b1;",
    " function f; input a; begin f = 1'b1; end endfunction",
    " task t; x = {1'b0, a}; endtask", " generate X u (.A(1'b1)); endgenerate",
    " generate if (1) begin assign y = {1'b0, a}; end endgenerate",
    " X #(.P(1'b1)) u (.A(1'b0));", " and #(1'b0) g (y,a,a);",
    " and #1 g2 (y,a,a);", " assign y = a / 2;", " assign y = {4{1'b0}};",
    " assign y = {11'b0, a, 21'h1, 8'b1, 1'bx, 1'sb0, 1 'b1, 1' d0, 1'b0_};",
    " assign y = {1'b0 /*c*/, a};", " assign y = {1'b0 // c\n , a};",
    " X u (.A(tie_const_1_net), .B(1'b1));", " wire tie_const_2_net;",
    " X \\tie_const_3 (.A(1'b0));", " wire \\u_core/tie_const_1 ;",
    " wire \\u_core/tie_const_2_net ;", " X \\a.tie_const_4 (.A(1'b1));", " assign y = 1/**/'b0;",
    " specify (a => y) = 1'b0; endspecify", " case (a) 1'b0, 1'b1: y = 1; endcase",
    " X u (.A(21'b1), .B(a1'b0), .C(4 1'b0));", " assign y = f(a, 1'b0, 1'b1);",
]
_HEADS = ["module t(a,y);", "module u #(parameter P = 1'b1) (input b, output q);",
          "macromodule m(a);", "module v(\n a,\n y\n);", "module w(a); // c ;"]


def _tie_names(used):
    return {u for u in used if "tie_const_" in u}


def test_a_name_inside_an_escaped_identifier_is_not_a_used_tie_name():
    src = ("module t(a,y);\n wire \\u_core/tie_const_1 ;\n wire \\u_core/tie_const_2_net ;\n"
           " X u (.A(1'b1), .B(1'b0));\nendmodule\n")
    out, n = pnr_mod.tie_constants(src, *TIE)
    assert n == 2 and "tie_const_1_net" in out and "tie_const_2_net" in out \
        and "tie_const_3" not in out
    used = pnr_mod._heads_used(src, pnr_mod._analyze(src))[1]
    assert not (used & {"tie_const_1", "tie_const_2_net"})
    assert _tie_names(used) == _tie_names(pnr_mod._analyze_exact(src).used)


def _random_text(rng):
    out = []
    for _ in range(rng.randint(1, 4)):
        out.append(rng.choice(_HEADS))
        out += [rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 12))]
        out.append("endmodule" if rng.random() < 0.9 else "")
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("seed", range(40))
def test_fast_analysis_equals_the_exact_tokenizer(seed):
    import random
    rng = random.Random(seed)
    for _ in range(25):
        text = _random_text(rng)
        fast, exact = pnr_mod._analyze(text), pnr_mod._analyze_exact(text)
        assert (fast.found, fast.ones, fast.gpins) == \
            (exact.found, exact.ones, exact.gpins), text
        if fast.found:
            heads, used = pnr_mod._heads_used(text, fast)
            assert (heads, _tie_names(used)) == \
                (exact.heads, _tie_names(exact.used)), text


def test_fast_analysis_equals_the_exact_tokenizer_on_every_case():
    for name, text in TIE_CASES.items():
        fast, exact = pnr_mod._analyze(text), pnr_mod._analyze_exact(text)
        assert (fast.found, fast.ones, fast.gpins) == \
            (exact.found, exact.ones, exact.gpins), name
        if fast.found:
            heads, used = pnr_mod._heads_used(text, fast)
            assert (heads, _tie_names(used)) == \
                (exact.heads, _tie_names(exact.used)), name


def test_remaining_count_of_a_tied_text_is_known_without_a_second_pass():
    for name, text in TIE_CASES.items():
        out, _ = pnr_mod.tie_constants(text, *TIE)
        seeded = pnr_mod.remaining_constants(out)
        pnr_mod._REMAINING.clear()
        pnr_mod._CACHE.clear()
        assert seeded == pnr_mod.remaining_constants(out), name


def test_text_with_directives_or_multiline_strings_takes_the_exact_path():
    for text in ("`define X 1'b0\nmodule t(a,y);\n assign y = {1'b0, a};\nendmodule\n",
                 "module t(a,y);\n initial $display(\"a\\\n1'b0\");\n"
                 " assign y = {1'b0, a};\nendmodule\n"):
        assert pnr_mod.count_constants(text) == 1
        out, n = pnr_mod.tie_constants(text, *TIE)
        assert n == 1 and "tie_const_1_net" in out


def test_tying_a_large_netlist_costs_a_small_multiple_of_a_regex_pass():
    import re
    import time
    lines = ["module t(input a, output y);"]
    for i in range(120000):
        lines.append(f"  AND2x2_ASAP7_75t_R u{i} (.A(a), .B({'1' if i % 50 == 0 else 'a'}"
                     f"{'' if i % 50 else chr(39) + 'h1'}), .Y(w{i}));")
    lines.append("endmodule")
    text = "\n".join(lines) + "\n"
    t0 = time.monotonic()
    re.sub(r"\.B\(1'h1\)", ".B(x)", text)
    base = max(time.monotonic() - t0, 0.01)
    pnr_mod._CACHE.clear()
    pnr_mod._REMAINING.clear()
    t0 = time.monotonic()
    out, n = pnr_mod.tie_constants(text, *TIE)
    assert pnr_mod.untied_constants_warning(out, {"hi": "a:b", "lo": "c:d"}) == ""
    took = time.monotonic() - t0
    assert n == 2400
    assert took < 30 + 20 * base, (took, base)