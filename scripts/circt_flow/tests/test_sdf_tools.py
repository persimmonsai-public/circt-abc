"""sdf_tools: the net-delay health check.
Hermetic: an invented three-cell SDF in OpenSTA's layout (escaped names with
a divider inside an instance name, a top-level port load, a multi-line COND),
the same file without its INTERCONNECT entries, and one that gives the wire
delay as PORT entries in the load cells."""
import re

import pytest

from circt_flow import sdf_tools as st

SDF = r"""(DELAYFILE
 (SDFVERSION "3.0")
 (DIVIDER /)
 (TIMESCALE 1ns)
 (CELL
  (CELLTYPE "top")
  (INSTANCE)
  (DELAY
   (ABSOLUTE
    (INTERCONNECT a u_core\/g1/A (0.0090::0.0090) (0.0095::0.0095))
    (INTERCONNECT u_core\/g1/Y q_reg/D (0.0123::0.0123) (0.0120::0.0120))
    (INTERCONNECT clk_i q_reg/CK (0.0880::0.0880) (0.0880::0.0880))
    (INTERCONNECT q_reg/Q y_o (0.0010::0.0010) (0.0010::0.0010))
   )
  )
 )
 (CELL
  (CELLTYPE "NAND2_X1")
  (INSTANCE u_core\/g1)
  (DELAY
   (ABSOLUTE
    (COND B==1'b1
       (IOPATH A Y (0.03::0.03) (0.04::0.04)))
   )
  )
 )
 (CELL
  (CELLTYPE "DFF_X1")
  (INSTANCE q_reg)
  (DELAY
   (ABSOLUTE
    (IOPATH CK Q (0.05::0.05) (0.06::0.06))
   )
  )
 )
)
"""


# The SDF without its net-delay entries (written without parasitics).
STRIPPED = "".join(l for l in SDF.splitlines(True) if "(INTERCONNECT" not in l)

# The same delays as PORT entries in the load cells' own DELAY blocks.
PORTED = (STRIPPED
          .replace("  (INSTANCE u_core\\/g1)\n",
                   "  (INSTANCE u_core\\/g1)\n  (DELAY (ABSOLUTE\n"
                   "    (PORT A (0.0090::0.0090) (0.0095::0.0095))))\n")
          .replace("  (INSTANCE q_reg)\n",
                   "  (INSTANCE q_reg)\n  (DELAY (ABSOLUTE\n"
                   "    (PORT D (0.0123::0.0123) (0.0120::0.0120))\n"
                   "    (PORT CK (0.0880::0.0880) (0.0880::0.0880))))\n"))


def test_health_flags_stripped_file_and_passes_port_entries():
    r = st.health(STRIPPED)
    assert not r["ok"] and "wire delay is missing" in r["failures"][0]
    assert st.health(SDF)["ok"]
    assert PORTED.count("(PORT ") == 3
    r = st.health(PORTED)
    assert r["ok"] and r["port_delays"] == 3 and r["cells"] == 2


def test_health_spef_net_count():
    assert st.health(PORTED, spef_nets=3)["ok"]
    r = st.health(PORTED, spef_nets=40)
    assert not r["ok"] and "SPEF nets" in r["failures"][0]


def test_health_warns_on_all_zero_delays():
    z = SDF.replace("0.0090", "0.0000").replace("0.0095", "0.0000") \
           .replace("0.0123", "0.0000").replace("0.0120", "0.0000") \
           .replace("0.0880", "0.0000").replace("0.0010", "0.0000")
    r = st.health(z)
    assert r["ok"] and r["warnings"]


def test_cli(tmp_path):
    src = tmp_path / "a.sdf"
    src.write_text(SDF)
    assert st.main(["health", str(src)]) == 0
    stripped = tmp_path / "c.sdf"
    stripped.write_text(STRIPPED)
    assert st.main(["health", str(stripped), "--json", str(tmp_path / "h.json")]) == 1
    assert (tmp_path / "h.json").exists()


def test_check_exports_reports_only_failing_files(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "good.sdf").write_text(SDF)
    (tmp_path / "bad.sdf").write_text(STRIPPED)
    (tmp_path / "junk.sdf").write_text("not an sdf\n")
    (tmp_path / "x.sdf.bak").write_text("ignored")
    bad = st.check_exports(tmp_path)
    assert [p.name for p, _ in bad] == ["bad.sdf", "junk.sdf"]


def test_check_exports_reports_files_it_cannot_read(tmp_path):
    """A corrupt gzip, a directory and non-UTF-8 bytes are reported, not
    raised."""
    (tmp_path / "bad.sdf.gz").write_bytes(b"not gzip")
    (tmp_path / "d.sdf").mkdir()
    (tmp_path / "x.sdf").write_bytes(b"(DELAYFILE \xff")
    got = {p.name: r for p, r in st.check_exports(tmp_path)}
    assert set(got) == {"bad.sdf.gz", "x.sdf"}
    assert all(r["failures"][0].startswith("could not be read") for r in got.values())


def test_check_exports_reports_an_all_zero_sdf(tmp_path):
    """Net-delay entries that are all zero (a writer without parasitics) are
    reported too, as a warning."""
    txt = re.sub(r"\((INTERCONNECT\s+\S+\s+\S+)\s+\([^()]*\)\s*\([^()]*\)\)",
                 r"(\1 (0::0) (0::0))", SDF)
    assert "(0::0)" in txt
    (tmp_path / "z.sdf").write_text(txt)
    got = dict(st.check_exports(tmp_path))
    (r,) = got.values()
    assert r["ok"] and r["warnings"]



def test_only_the_health_check_is_offered(capsys):
    """The module checks an SDF; it does not rewrite one: `health` is its
    only subcommand."""
    with pytest.raises(SystemExit):
        st.main(["--help"])
    assert "{health}" in capsys.readouterr().out
