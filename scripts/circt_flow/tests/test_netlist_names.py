"""netlist_names: the SPEF delimiter inside escaped identifiers (hermetic)."""
import pytest

from circt_flow import netlist_names as nn

NET = r"""// comment with a \x:y escaped-looking token and a [7:0] range
module m (a, y);
  input [7:0] a;
  output y;
  wire \u/alu:12_n_0 , \u/alu:12_n_1 , plain_w;
  /* block \b:c */
  AND2 \u/alu:g1 (.A(a[0]), .B(\u/alu:12_n_0 ), .Y(\u/alu:12_n_1 ));
  BUF g2 (.A(\u/alu:12_n_1 ), .Y(y));
endmodule
"""


def test_only_escaped_identifiers_change():
    out, mp = nn.normalize(NET)
    assert mp == {r"u/alu:12_n_0": "u/alu_12_n_0",
                  r"u/alu:12_n_1": "u/alu_12_n_1",
                  r"u/alu:g1": "u/alu_g1"}
    assert "[7:0]" in out and r"\x:y" in out and r"\b:c" in out   # comments, ranges
    assert r"\u/alu_12_n_0 ," in out and r".B(\u/alu_12_n_0 )" in out
    assert ":" not in out.replace("[7:0]", "").replace(r"\x:y", "").replace(r"\b:c", "")
    assert "plain_w" in out and "g2" in out


def test_clean_file_is_returned_byte_for_byte():
    clean = NET.replace("alu:", "alu_")
    out, mp = nn.normalize(clean)
    assert out is clean and mp == {}
    assert not nn.needs_rename(clean) and nn.needs_rename(NET)


def test_collision_with_existing_names_gets_a_suffix():
    t = (r"module m (); wire \a:b , \a_b , \a_b_1 , c; endmodule" "\n")
    out, mp = nn.normalize(t)
    assert mp[r"a:b"] == "a_b_2"          # a_b and a_b_1 are taken
    assert r"\a_b " in out and r"\a_b_2 " in out
    with pytest.raises(nn.NameError_, match="collides"):
        nn.normalize(t, strict=True)


def test_collision_with_plain_identifier_and_between_renames():
    t = "module m (); wire a_b; wire \\a:b , \\a;b , \\a:b ; endmodule\n"
    # ';' is not the delimiter: only a:b renames, and it must dodge the plain a_b
    out, mp = nn.normalize(t)
    assert mp == {"a:b": "a_b_1"}
    t2 = "module m (); wire \\a::b , \\a:_b ; endmodule\n"
    _, mp2 = nn.normalize(t2)
    assert len(set(mp2.values())) == 2     # two different ids, two different names


def test_restore_inverts():
    out, mp = nn.normalize(NET)
    assert nn.restore(out, mp) == NET


def test_replacement_may_not_contain_the_delimiter():
    with pytest.raises(nn.NameError_):
        nn.normalize(NET, repl=":_")


def test_cli(tmp_path, capsys):
    src, dst, mp = tmp_path / "a.v", tmp_path / "b.v", tmp_path / "m.json"
    src.write_text(NET)
    assert nn.main(["--check", str(src)]) == 1
    assert nn.main([str(src), str(dst), "--map", str(mp)]) == 0
    assert nn.main(["--check", str(dst)]) == 0
    assert mp.exists() and "u/alu_g1" in mp.read_text()
