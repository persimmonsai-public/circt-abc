"""Legalization guard (placement_health.py) on invented log snippets in
OpenROAD's message format."""
from circt_flow import placement_health as ph

BAD = """\
Fixing late overlaps
Total cells:                      7
Total Placement Failures:         3
[INFO DPL-0034] Detailed placement failed on the following 3 instances:
[INFO DPL-0035]  u_a
[INFO DPL-0035]  u_b
[INFO DPL-0035]  u_c
[ERROR DPL-0036] Detailed placement failed inside DPL.
"""
TWO_ATTEMPTS = """\
Total Placement Failures:         3
[INFO DPL-0034] Detailed placement failed on the following 3 instances:
Total Placement Failures:          0
"""
CLEAN = "Total Placement Failures:          0\n"
# The shape of a clean detailed-placement log of a stock ORFS run
# (negotiation legalizer: no movement summary; invented numbers).
ORFS_CLEAN = """\
[INFO DPL-1102] Legalizing using negotiation legalizer.
[INFO DPL-0500] Runtime: 0.01s
Placement Analysis
---------------------------------
total displacement         11.0 u
average displacement        0.1 u
max displacement            1.0 u
original HPWL             200.0 u
legalized HPWL            210.0 u

improve_placement
Placement Analysis
---------------------------------
legalized HPWL            205.0 u
"""


def test_parse_log():
    r = ph.parse_log(BAD)
    assert r["attempts"] == [3] and r["dpl0034"] == [3]


def test_dpl0034_only_log():
    assert ph.parse_log("[INFO DPL-0034] Detailed placement failed on the following 5 instances:\n"
                        )["attempts"] == [5]


def test_exit_status_and_final(tmp_path, capsys):
    (tmp_path / "bad.log").write_text(BAD)
    assert ph.main([str(tmp_path / "bad.log")]) == 1
    assert "FAILED" in capsys.readouterr().out
    (tmp_path / "ok.log").write_text(CLEAN)
    assert ph.main([str(tmp_path / "ok.log")]) == 0


def test_a_later_clean_attempt_is_not_a_failure(tmp_path):
    (tmp_path / "r.log").write_text(TWO_ATTEMPTS)
    res = ph.check([tmp_path / "r.log"])
    assert res["final_failures"] == 0 and res["max_failures"] == 3 and res["ok"]


def test_directory_orfs_layout_in_name_order(tmp_path):
    """The ORFS stage's run directory: work/logs/<platform>/<design>/<variant>/."""
    d = tmp_path / "work" / "logs" / "asap7" / "top" / "base"
    d.mkdir(parents=True)
    (d / "3_5_place_dp.log").write_text(BAD)
    (d / "4_1_cts.log").write_text(CLEAN)
    (d / "2_1_floorplan.log").write_text("no placement here\n")
    (tmp_path / "orfs.log").write_text(BAD)          # the tee of all: not read
    res = ph.check(ph.collect([str(tmp_path)]))
    assert [s["stage"] for s in res["stages"]] == ["2_1_floorplan", "3_5_place_dp", "4_1_cts"]
    assert res["final_failures"] == 0 and res["final_stage"] == "4_1_cts"
    (d / "4_1_cts.log").write_text(BAD)              # a later failure counts
    assert ph.main([str(tmp_path)]) == 1


def test_directory_logs_subdirectory(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a_place.log").write_text(BAD)
    (tmp_path / "logs" / "b_cts.log").write_text(CLEAN)
    res = ph.check(ph.collect([str(tmp_path)]))
    assert res["final_failures"] == 0 and res["max_failures"] == 3


def test_no_attempt(tmp_path):
    (tmp_path / "x.log").write_text("nothing\n")
    assert ph.main([str(tmp_path / "x.log")]) == 0
    assert ph.main([str(tmp_path / "x.log"), "--require"]) == 1
    (tmp_path / "e").mkdir()
    assert ph.main([str(tmp_path / "e")]) == 2


def test_clean_orfs_detailed_placement_log_is_a_successful_attempt(tmp_path):
    assert ph.parse_log(ORFS_CLEAN)["attempts"] == [0, 0]
    d = tmp_path / "work" / "logs" / "asap7" / "top" / "base"
    d.mkdir(parents=True)
    (d / "3_5_place_dp.log").write_text(ORFS_CLEAN)
    (d / "4_1_cts.log").write_text("no placement table\n")
    res = ph.check(ph.collect([str(tmp_path)]))
    assert res["ok"] and res["final_failures"] == 0
    assert ph.main([str(tmp_path), "--require"]) == 0


def test_a_table_after_a_failure_does_not_hide_it():
    assert ph.parse_log(BAD + "legalized HPWL   1.0 u\n")["attempts"] == [3]
    assert ph.parse_log(BAD)["attempts"] == [3]



def test_both_layouts_in_one_directory_are_read(tmp_path):
    """ORFS variant logs and logs/*.log in one directory: two runs, a failure
    in either counts."""
    d = tmp_path / "work" / "logs" / "asap7" / "top" / "base"
    d.mkdir(parents=True)
    (d / "3_5_place_dp.log").write_text(ORFS_CLEAN)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "place.log").write_text("Total Placement Failures:   7\n")
    rs = ph.runs([str(tmp_path)])
    assert len(rs) == 2
    res = ph.check_runs(rs)
    assert res["final_failures"] == 7
    assert [r["final_failures"] for r in res["runs"]] == [0, 7]
    assert ph.main([str(tmp_path)]) == 1


def test_each_orfs_variant_is_a_run_of_its_own(tmp_path):
    for v, text in (("base", CLEAN), ("old", "Total Placement Failures:   5\n")):
        d = tmp_path / "work" / "logs" / "asap7" / "top" / v
        d.mkdir(parents=True)
        (d / "3_5_place_dp.log").write_text(text)
    res = ph.check_runs(ph.runs([str(tmp_path)]))
    assert sorted(r["final_failures"] for r in res["runs"]) == [0, 5]
    assert res["final_failures"] == 5
