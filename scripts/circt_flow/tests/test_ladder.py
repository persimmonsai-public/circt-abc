"""The closure ladder (circt_flow/ladder.py, doc/ladder.md), with stubs: a
small P&R command that follows the stage contract, and a fake ORFS (a
Makefile with ORFS's stage targets and result files). No P&R tool is run."""
import json
import re
import signal
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from circt_flow import ladder as L
from circt_flow import orfs_pnr as pnr_mod

# ---------------------------------------------------------------------------
# The stub P&R command (a tech pack's `pnr.command`)
# ---------------------------------------------------------------------------

STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Stub P&R: behaviour per rung value from $STUB_PLAN (json), events to
    $STUB_LOG. Follows the ladder's stage contract."""
    import json, os, sys, time
    from pathlib import Path
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    out = Path(a["--out"])
    plan = json.load(open(os.environ["STUB_PLAN"]))
    val = json.loads(os.environ.get("CIRCT_FLOW_LADDER_VALUE", "null"))
    b = plan.get(str(val), {})
    stop = os.environ.get("CIRCT_FLOW_STOP_AFTER")
    start = os.environ.get("CIRCT_FLOW_START_AT")
    def log(ev):
        with open(os.environ["STUB_LOG"], "a") as f:
            f.write(f"{ev} {val} pid={os.getpid()}\\n")
    if stop:
        log("place")
        sd0 = Path(os.environ["STUB_LOG"]).parent
        for _ in range(600):                   # held until a file appears
            if not b.get("place_wait") or (sd0 / b["place_wait"]).exists():
                break
            time.sleep(0.1)
        (out / "placed.txt").write_text(Path(a["--netlist"]).read_text())
        sys.exit(0)
    if start:
        src = Path(os.environ["CIRCT_FLOW_START_FROM"])
        if not (src / "placed.txt").is_file():
            sys.exit(3)
        log("restart")
    else:
        log("full")
    log("begin")
    time.sleep(b.get("sleep", 0))
    sd = Path(os.environ["STUB_LOG"]).parent
    for _ in range(600):                       # wait for a file, at most 60 s
        if not b.get("wait_for") or (sd / b["wait_for"]).exists():
            break
        time.sleep(0.1)
    if b.get("rc"):
        sys.exit(b["rc"])
    m = {k: b[k] for k in ("setup_ws", "hold_ws", "drc_errors") if k in b}
    (out / "pnr_metrics.json").write_text(json.dumps(m))
    if b.get("signal"):
        (sd / b["signal"]).write_text("")
    log("end")
    if b.get("rc_late"):                       # metrics written, then a failure
        sys.exit(b["rc_late"])
''')

CHECK = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    plan = json.load(open(os.environ["STUB_PLAN"]))
    b = plan.get(os.environ["CIRCT_FLOW_LADDER_VALUE"], {})
    time.sleep(b.get("check_sleep", 0))
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write(f"check {os.environ['CIRCT_FLOW_LADDER_VALUE']}\\n")
    sys.exit(b.get("check_rc", 0))
''')

GOOD = {"setup_ws": 5.0, "hold_ws": 5.0, "drc_errors": 0}


class World:
    """A fake flow config around the stub command."""

    def __init__(self, tmp: Path, plan: dict, stage_restart=("place", "cts", "route"),
                 ladder=None, netlist="module t; endmodule\n"):
        self.tmp = tmp
        (tmp / "pack").mkdir(exist_ok=True)
        stub = tmp / "pack" / "pnr.py"
        stub.write_text(STUB)
        stub.chmod(0o755)
        chk = tmp / "check.py"
        chk.write_text(CHECK)
        chk.chmod(0o755)
        self.plan_file, self.log_file = tmp / "plan.json", tmp / "events.log"
        self.set_plan(plan)
        self.log_file.write_text("")
        _MP.setenv("STUB_PLAN", str(self.plan_file))
        _MP.setenv("STUB_LOG", str(self.log_file))
        self.net = tmp / "n.v"
        self.net.write_text(netlist)
        pack_pnr = {"command": [sys.executable, str(stub)]}
        if stage_restart:
            pack_pnr["stage_restart"] = list(stage_restart)
        self.pnr = {"clock_port": "clk",
                    "ladder": ladder or {"param": "knob",
                                         "values": [0, 1, 2],
                                         "from_stage": "cts"}}
        self.cfg = SimpleNamespace(
            design=SimpleNamespace(
                pnr=self.pnr, tie=None, top_module="t", clock_period_ps=1000,
                pack_pnr=pack_pnr, pack_root=tmp / "pack", config_dir=tmp,
                wire_rc=None, wire_rc_setup=None),
            openroad_netlist=self.net, pnr_dir=tmp / "out" / "pnr")

    def set_plan(self, plan):
        self.plan_file.write_text(json.dumps(plan))

    def events(self):
        return [l.split() for l in self.log_file.read_text().splitlines()]

    def run(self, **kw):
        logs = []
        try:
            L.run_flow(self.cfg, log=logs.append, **kw)
            code = 0
        except L.LadderFailed as e:
            code = e.code
        self.logs = logs
        return code, json.loads(
            (self.cfg.pnr_dir / "ladder" / "RESULT.json").read_text())


def verdicts(rep):
    return [r["verdict"] for r in rep["rungs"]]


_MP = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    global _MP
    _MP = monkeypatch
    for k in ("CIRCT_FLOW_LADDER_SUBMIT",):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_defaults_are_neutral():
    s = L.parse_ladder({"param": "hold_margin", "values": [0, 1]})
    assert s.mode == "parallel" and s.slots == 2
    assert {(c.key, c.min, c.max) for c in s.accept.checks} == {
        ("setup_ws", 0.0, None), ("hold_ws", 0.0, None),
        ("drc_errors", None, 0.0)}
    seq = L.parse_ladder({"param": "p", "values": [1, 2], "mode": "sequential"})
    assert seq.slots == 1


@pytest.mark.parametrize("d,msg", [
    ({"values": [1]}, "param: required"),
    ({"param": "p"}, "values: required"),
    ({"param": "p", "values": []}, "non-empty"),
    ({"param": "p", "values": [1, 1]}, "must differ"),
    ({"param": "p", "values": [1], "mode": "x"}, "parallel or sequential"),
    ({"param": "p", "values": [1], "jobs": 0}, "positive"),
    ({"param": "p", "values": [1], "extra": 1}, "unknown key"),
    ({"param": "p", "values": [1], "submit": "run it"}, r"\{cmd\}"),
    ({"param": "p", "values": [1], "submit": "srun ${X} {cmd}"}, "not a placeholder"),
    ({"param": "p", "values": [1], "submit": "srun {cmd} {nope}"}, "not a placeholder"),
    ({"param": "p", "values": [1], "cancel": "scancel {nope}"}, "not a placeholder"),
    ({"param": "p", "values": [1], "fresh": "yes"}, "true or false"),
    ({"param": "p", "values": [1], "accept": {"metrics": {"x": {}}}}, "min and/or max"),
    ({"param": "p", "values": [1], "accept": {"metrics": {"x": {"min": "a"}}}}, "finite number"),
    ({"param": "p", "values": [1], "accept": {"command": "check"}}, "list of words"),
    ({"param": "p", "values": [1], "accept": {"corners": {"ff": {"setup_ws": {"min": 0}}}}}, "libs"),
    ({"param": "p", "values": [1], "accept": {"bogus": 1}}, "unknown key"),
])
def test_bad_declarations_are_refused(d, msg):
    with pytest.raises(L.LadderError, match=msg):
        L.parse_ladder(d)


def test_apply_param():
    p = {"set": {"A": 1}, "hold_margin": 3, "ladder": {}}
    assert L.apply_param(p, "hold_margin", 9)["hold_margin"] == 9
    q = L.apply_param(p, "set.B", 2)
    assert q["set"] == {"A": 1, "B": 2} and "ladder" not in q
    assert p["set"] == {"A": 1}          # the original is untouched


def test_decide_is_order_only():
    A, R, U, E = L.ACCEPTED, L.REJECTED, L.UNVERIFIED, L.ERROR
    assert L.decide([None, None]) == ("pending", None)
    assert L.decide([R, None, A]) == ("pending", None)   # a higher accepted rung waits
    assert L.decide([R, A, None]) == (A, 1)
    assert L.decide([A, R, R]) == (A, 0)
    assert L.decide([R, R]) == ("none", None)
    assert L.decide([R, U, A]) == (U, 1)                 # not skipped over
    assert L.decide([R, E, A]) == (E, 1)


def test_check_metrics_missing_or_nan_is_unverified():
    cs = L.Accept().checks
    assert L.check_metrics(GOOD, cs) == (L.ACCEPTED, [])
    v, why = L.check_metrics({"setup_ws": 1, "hold_ws": 1}, cs)
    assert v == L.UNVERIFIED and "drc_errors not reported" in why[0]
    v, _ = L.check_metrics({**GOOD, "hold_ws": float("nan")}, cs)
    assert v == L.UNVERIFIED
    v, why = L.check_metrics({**GOOD, "hold_ws": -0.1, "drc_errors": None}, cs)
    assert v == L.REJECTED and "hold_ws -0.1 < 0" in why[0]
    assert L.combine([(L.UNVERIFIED, ["a"]), (L.REJECTED, ["b"])]) == (
        L.REJECTED, ["b"])


# ---------------------------------------------------------------------------
# The ladder with the stub command
# ---------------------------------------------------------------------------


def test_lowest_valid_rung_wins_when_a_higher_one_finishes_first(tmp_path):
    # rung 0 cannot end before rung 1 has: it waits for rung 1's signal file
    w = World(tmp_path, {"0": {**GOOD, "wait_for": "r1done"},
                         "1": {**GOOD, "signal": "r1done"}, "2": GOOD})
    code, rep = w.run()
    assert code == 0 and rep["state"] == "accepted" and rep["accepted"] == 0
    ends = [e[1] for e in w.events() if e[0] == "end"]
    assert ends.index("1") < ends.index("0")      # rung 1 really finished first
    assert (w.cfg.pnr_dir / "pnr_metrics.json").is_file()
    assert (w.cfg.pnr_dir / "accepted").resolve().name == "rung_0"


def test_builtin_check_rejects_and_the_next_rung_is_taken(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "hold_ws": -3.0, "sleep": 0.5},
                         "1": GOOD, "2": GOOD})
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 1
    assert verdicts(rep)[:2] == ["rejected", "accepted"]
    assert "hold_ws -3 < 0" in rep["rungs"][0]["reasons"][0]


def test_user_command_rejects(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "check_rc": 1}, "1": GOOD, "2": GOOD},
              ladder={"param": "knob", "values": [0, 1, 2], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 1
    assert rep["rungs"][0]["reasons"] == ["command exited 1"]
    assert ["check", "1"] in w.events()


def test_builtin_rejection_stops_the_user_command(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "hold_ws": -1, "check_sleep": 30},
                         "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    t0 = time.monotonic()
    code, rep = w.run()
    assert time.monotonic() - t0 < 20            # not 30 s: the check was killed
    assert rep["accepted"] == 1 and ["check", "0"] not in w.events()


def test_all_rungs_failing_is_a_nonzero_exit_and_a_report(tmp_path):
    w = World(tmp_path, {str(v): {**GOOD, "setup_ws": -1.0} for v in range(3)})
    code, rep = w.run()
    assert code == L.EXIT_NONE_ACCEPTED
    assert rep["state"] == "none" and rep["accepted"] is None
    assert verdicts(rep) == ["rejected"] * 3
    assert any("NOT ACCEPTED" in l for l in w.logs)
    assert not (w.cfg.pnr_dir / "accepted").exists()
    assert "closure ladder: none" in (w.cfg.pnr_dir / "pnr_metrics.json").read_text()


def test_incomplete_check_is_unverified_not_accepted(tmp_path):
    # the user command did not complete (exit 2): the rung is unverified, and
    # a higher rung that passes is not taken over it
    w = World(tmp_path, {"0": {**GOOD, "check_rc": 2}, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    code, rep = w.run()
    assert code == L.EXIT_UNVERIFIED and rep["state"] == "unverified"
    assert rep["accepted"] is None
    assert "did not complete" in rep["rungs"][0]["reasons"][0]
    assert not (w.cfg.pnr_dir / "accepted").exists()


def test_metric_that_was_not_reported_is_unverified(tmp_path):
    w = World(tmp_path, {"0": {"setup_ws": 1.0}, "1": {"setup_ws": 1.0}},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    code, rep = w.run()
    assert code == L.EXIT_UNVERIFIED and verdicts(rep)[0] == "unverified"


def test_failed_rung_run_is_an_error(tmp_path):
    w = World(tmp_path, {"0": {"rc": 9}, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    code, rep = w.run()
    assert code == L.EXIT_RUNG_FAILED and rep["state"] == "error"


def test_sequential_gives_the_same_answer_and_runs_no_higher_rung(tmp_path):
    plan = {"0": {**GOOD, "hold_ws": -1.0}, "1": GOOD, "2": GOOD}
    pa = World(tmp_path / "a", plan) if (tmp_path / "a").mkdir() is None else None
    ca, ra = pa.run(mode="parallel")
    (tmp_path / "b").mkdir()
    pb = World(tmp_path / "b", plan)
    cb, rb = pb.run(mode="sequential")
    assert (ca, ra["state"], ra["accepted"]) == (cb, rb["state"], rb["accepted"]) == (0, "accepted", 1)
    assert [r["verdict"] for r in rb["rungs"]][:2] == ["rejected", "accepted"]
    assert rb["rungs"][2]["verdict"] == "not run"
    assert not [e for e in pb.events() if e[0] == "begin" and e[1] == "2"]
    # same metrics file for the accepted rung
    assert (pa.cfg.pnr_dir / "pnr_metrics.json").read_text() == \
        (pb.cfg.pnr_dir / "pnr_metrics.json").read_text()


def _gone(pid, timeout=10.0):
    """True once the process is gone (a zombie counts as gone)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if "Z" in Path(f"/proc/{pid}/stat").read_text().split(")")[-1][:3]:
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _wait_until(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.05)


def test_parallel_runs_rungs_at_the_same_time_and_cancels_higher_ones(tmp_path):
    # rung 0 ends only after all three rungs have started: they ran together
    w = World(tmp_path, {"0": {**GOOD, "wait_for": "go"},
                         "1": {**GOOD, "sleep": 60}, "2": {**GOOD, "sleep": 60}})
    out = {}
    th = threading.Thread(target=lambda: out.update(r=w.run()))
    th.start()
    _wait_until(lambda: sum(e[0] == "begin" for e in w.events()) == 3)
    (tmp_path / "go").write_text("")
    th.join(60)
    code, rep = out["r"]
    assert code == 0 and rep["accepted"] == 0
    assert verdicts(rep)[1:] == ["not needed", "not needed"]
    for e in w.events():                          # the cancelled stubs are gone
        if e[0] == "begin" and e[1] != "0":
            assert _gone(int(e[2].split("=")[1]))
    # every job the ladder started has been forgotten again
    assert json.loads((w.cfg.pnr_dir / "ladder" / "processes.json").read_text()) == []


def test_jobs_bounds_the_concurrency(tmp_path):
    w = World(tmp_path, {str(v): {**GOOD, "hold_ws": -1, "sleep": 0.4}
                         for v in range(3)},
              ladder={"param": "knob", "values": [0, 1, 2], "jobs": 1,
                      "from_stage": "cts"})
    code, rep = w.run()
    assert code == L.EXIT_NONE_ACCEPTED
    ev = [(e[0], e[1]) for e in w.events() if e[0] in ("begin", "end")]
    for a, b in zip(ev[::2], ev[1::2]):               # begin/end pairs never interleave
        assert a[0] == "begin" and b == ("end", a[1])


def test_stage_contract_with_restart(tmp_path):
    w = World(tmp_path, {str(v): GOOD for v in range(3)})
    code, rep = w.run(mode="sequential")
    assert code == 0 and rep["restart"] is True
    kinds = [e[0] for e in w.events()]
    assert kinds.count("place") == 1 and kinds.count("restart") == 1
    assert "full" not in kinds


def test_command_without_stage_support_runs_every_rung_in_full(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "hold_ws": -1}, "1": GOOD, "2": GOOD},
              stage_restart=None)
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 1 and rep["restart"] is False
    kinds = [e[0] for e in w.events()]
    assert "place" not in kinds and "restart" not in kinds
    assert kinds.count("full") >= 2
    assert any("in full" in l for l in w.logs)


def test_from_stage_the_command_does_not_declare_falls_back(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD}, stage_restart=("place", "cts"),
              ladder={"param": "knob", "values": [0, 1], "from_stage": "route"})
    code, rep = w.run()
    assert code == 0 and rep["restart"] is False


def test_resume_after_a_simulated_interruption_reuses_placement_and_finished_rungs(tmp_path):
    plan = {"0": {**GOOD, "hold_ws": -1}, "1": {**GOOD, "sleep": 60}, "2": GOOD}
    w = World(tmp_path, plan)
    spec = L.parse_ladder(w.pnr["ladder"])
    lad = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir,
                   log=lambda m: None)
    th = threading.Thread(target=lambda: setattr(lad, "rep", lad.run()))
    th.start()
    deadline = time.monotonic() + 30             # wait for rung 0's verdict
    while not (lad.rung_dir(0) / "rung_result.json").is_file():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    lad.stop.set()                                # the interruption
    for e in lad.rung_stop:
        e.set()
    th.join(30)
    assert lad.rep["state"] == "interrupted"
    n_place = [e[0] for e in w.events()].count("place")
    # second run: rung 1 now finishes quickly
    plan["1"] = GOOD
    w.set_plan(plan)
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 1
    assert [e[0] for e in w.events()].count("place") == n_place == 1   # placed once
    assert rep["rungs"][0]["reused"] is True          # rung 0 came from its record
    ev = lambda kind, v: [e for e in w.events() if e[0] == kind and e[1] == v]
    assert len(ev("restart", "0")) == 1          # rung 0 was not run again
    assert len(ev("end", "1")) == 1              # rung 1 ran to the end this time
    assert any("placement reused" in l for l in w.logs)


def test_a_run_stopped_through_its_stop_event_leaves_nothing_running(tmp_path):
    w = World(tmp_path, {str(v): {**GOOD, "sleep": 60} for v in range(3)})
    spec = L.parse_ladder(w.pnr["ladder"])
    lad = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir,
                   log=lambda m: None)
    th = threading.Thread(target=lambda: setattr(lad, "rep", lad.run()))
    th.start()
    deadline = time.monotonic() + 30
    while sum(e[0] == "begin" for e in w.events()) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    lad.stop.set()
    for e in lad.rung_stop:
        e.set()
    th.join(30)
    for e in w.events():
        if e[0] == "begin":
            pid = int(e[2].split("=")[1])
            assert not Path(f"/proc/{pid}").exists() or \
                "Z" in Path(f"/proc/{pid}/stat").read_text().split(")")[-1][:3]


def test_stale_placement_is_refused(tmp_path):
    w = World(tmp_path, {str(v): GOOD for v in range(2)},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    w.net.write_text("module t(input a); endmodule\n")       # an earlier-stage input
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_changed_pnr_option_is_refused_even_when_it_is_unknown(tmp_path):
    w = World(tmp_path, {str(v): GOOD for v in range(2)},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    w.pnr["some_new_option"] = 7           # the command cannot say what it affects
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_ladder_param_change_does_not_stale_the_placement(tmp_path):
    w = World(tmp_path, {str(v): GOOD for v in range(3)},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    w.pnr["ladder"] = {"param": "knob", "values": [0, 1, 2], "from_stage": "cts"}
    code, rep = w.run()                    # one more rung: same placement
    assert code == 0 and any("placement reused" in l for l in w.logs)


def _wrapper(tmp_path):
    """A blocking stand-in for a scheduler's run command: logs its arguments,
    drops its first two (`--job <name>`), runs the rest."""
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text("#!/bin/bash\necho \"$@\" >> " + str(tmp_path / "wrapped.txt")
                       + "\nshift 2\nexec \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


def test_submit_template_wraps_every_job(tmp_path):
    wrapper = _wrapper(tmp_path)
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": f"{wrapper} --job {{name}} {{cmd}}"})
    code, rep = w.run()
    assert code == 0
    wrapped = (tmp_path / "wrapped.txt").read_text()
    assert wrapped.count("CIRCT_FLOW_LADDER_VALUE") >= 2     # env travels in {cmd}
    assert "--job ladder-" in wrapped
    assert "circt_flow.ladder --exec" in wrapped             # the completion marker


def test_cancel_template_runs_for_the_ladders_own_job_names_only(tmp_path):
    marker = tmp_path / "cancelled.txt"
    wrapper = _wrapper(tmp_path)
    w = World(tmp_path, {"0": GOOD, "1": {**GOOD, "wait_for": "never"}},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": f"{wrapper} --job {{name}} {{cmd}}",
                      "cancel": f"echo {{name}} >> {marker}"})
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 0
    names = marker.read_text().split()
    assert names and all(n.startswith("ladder-") for n in names)


def test_a_submit_command_that_does_not_block_never_accepts(tmp_path):
    """`true {cmd}` returns 0 at once without running the job: no completion
    marker, so no rung is judged (and stale files of an earlier run are not
    read)."""
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0                       # a good run leaves metrics behind
    w.pnr["ladder"]["submit"] = "true {cmd}"     # now nothing runs, exit status 0
    w.pnr["ladder"]["accept"] = {"metrics": {"hold_ws": {"min": 0}}}   # forces the rungs to run again
    code, rep = w.run()
    assert code == L.EXIT_RUNG_FAILED and rep["state"] == "error"
    assert "completion marker" in rep["rungs"][0]["reasons"][0]
    assert not (w.cfg.pnr_dir / "accepted").exists()
    assert "closure ladder: error" in (w.cfg.pnr_dir / "pnr_metrics.json").read_text()


def test_a_failing_submit_command_is_not_read_as_a_result(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": "false && {cmd}"})
    # the placement job never ran (exit 1 of the submit command itself)
    with pytest.raises(L.LadderFailed, match="completion marker") as e:
        L.run_flow(w.cfg, log=lambda m: None)
    assert e.value.code == L.EXIT_RUNG_FAILED


def test_handle_returns_the_jobs_own_status_behind_a_submit_command(tmp_path):
    r = L.Runner(tmp_path, submit="bash -c 'exit 1' ; {cmd}")
    job = L.Job("j", ["bash", "-c", "exit 3"], dict(os.environ), tmp_path,
                tmp_path / "j.log")
    assert r.spawn(job).wait(threading.Event()) == 3     # the job's, not the wrapper's
    r2 = L.Runner(tmp_path, submit="true {cmd}")
    assert r2.spawn(job).wait(threading.Event()) == L.NO_MARKER


def test_the_submit_override_is_validated_like_the_config_key(tmp_path, monkeypatch):
    w = World(tmp_path, {"0": GOOD})
    monkeypatch.setenv("CIRCT_FLOW_LADDER_SUBMIT", "sbatch --wait --wrap x")
    with pytest.raises(L.LadderError, match=r"must contain \{cmd\}"):
        L.run_flow(w.cfg, log=lambda m: None)
    monkeypatch.setenv("CIRCT_FLOW_LADDER_SUBMIT", "srun ${J} {cmd}")
    with pytest.raises(L.LadderError, match="not a placeholder"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_a_rung_that_raises_is_an_error_and_the_ladder_does_not_hang(tmp_path, monkeypatch):
    w = World(tmp_path, {"0": GOOD, "1": GOOD})
    orig = L.CommandBackend.rung

    def boom(self, d, value, place_dir, i):
        if i == 0:
            raise FileNotFoundError("the pack's script vanished")
        return orig(self, d, value, place_dir, i)
    monkeypatch.setattr(L.CommandBackend, "rung", boom)
    out = {}
    th = threading.Thread(target=lambda: out.update(r=w.run()), daemon=True)
    th.start()
    th.join(60)
    assert not th.is_alive(), "the parallel ladder hung"
    code, rep = out["r"]
    assert code == L.EXIT_RUNG_FAILED and "FileNotFoundError" in rep["rungs"][0]["reasons"][0]


def test_job_names_differ_between_runners_of_one_process(tmp_path):
    a, b = L.Runner(tmp_path), L.Runner(tmp_path)
    assert a.tag != b.tag


def test_reap_stale_stops_only_a_process_with_the_recorded_start_time(tmp_path):
    base = dict(start_new_session=True)
    mine = subprocess.Popen(["sleep", "60"], **base)
    other = subprocess.Popen(["sleep", "60"], **base)
    try:
        r = L.Runner(tmp_path, cancel="echo {name} >> " + str(tmp_path / "c.txt"),
                     grace=0.2)
        (tmp_path / "processes.json").write_text(json.dumps([
            {"name": "ours", "pid": mine.pid, "start": L._proc_start(mine.pid),
             "host": L._host_id(), "jobname": ""},
            # same pid but another start time: a reused pid, not ours
            {"name": "reused", "pid": other.pid, "start": "1", "host": L._host_id(),
             "jobname": ""},
            {"name": "job", "pid": 2 ** 22 + 5, "start": None, "host": L._host_id(),
             "jobname": "ladder-x-1"}]))
        stopped = r.reap_stale()
        assert "ours" in stopped and "reused" not in stopped
        assert mine.wait(10) is not None and other.poll() is None
        assert "ladder-x-1" in (tmp_path / "c.txt").read_text()   # the scheduler job
        assert json.loads((tmp_path / "processes.json").read_text()) == []
    finally:
        for p in (mine, other):
            p.kill()
            p.wait()


def test_second_orchestrator_on_the_same_directory_is_refused(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "wait_for": "release"}, "1": GOOD})
    spec = L.parse_ladder(w.pnr["ladder"])
    a = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir,
                 log=lambda m: None)
    th = threading.Thread(target=a.run)
    th.start()
    # rung 0 is held until released: the first ladder is certainly running
    _wait_until(lambda: any(e[0] == "begin" and e[1] == "0" for e in w.events()))
    b = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir,
                 log=lambda m: None)
    with pytest.raises(L.LadderError, match="in use"):
        b.run()
    (tmp_path / "release").write_text("")
    th.join(30)


def test_stage_restart_needs_from_stage(tmp_path):
    w = World(tmp_path, {"0": GOOD},
              ladder={"param": "knob", "values": [0]})
    with pytest.raises(L.LadderError, match="from_stage: required"):
        L.run_flow(w.cfg, log=lambda m: None)


# ---------------------------------------------------------------------------
# The ORFS stage restart, with a fake ORFS
# ---------------------------------------------------------------------------

FAKE_MAKEFILE = textwrap.dedent('''\
    include $(DESIGN_CONFIG)
    R = $(WORK_HOME)/results/$(PLATFORM)/$(DESIGN_NAME)/base
    M = $(WORK_HOME)/reports/$(PLATFORM)/$(DESIGN_NAME)/base
    RAN = $(WORK_HOME)/ran.txt
    .PHONY: floorplan place cts route finish metadata-generate
    floorplan: $(R)/2_floorplan.odb
    place: $(R)/3_place.odb
    cts: $(R)/4_cts.odb
    route: $(R)/5_route.odb
    finish: $(R)/6_final.odb
    $(R)/1_synth.odb: $(SYNTH_NETLIST_FILES) $(SDC_FILE)
    	@mkdir -p $(R) $(M); echo synth >> $(RAN); cat $(SYNTH_NETLIST_FILES) > $@
    $(R)/2_floorplan.odb: $(R)/1_synth.odb
    	@echo floorplan >> $(RAN); (cat $<; echo "fp util=$(CORE_UTILIZATION) foo=$(FOO)") > $@
    $(R)/3_place.odb: $(R)/2_floorplan.odb
    	@echo place >> $(RAN); (cat $<; echo "place") > $@
    $(R)/4_cts.odb: $(R)/3_place.odb
    	@echo cts >> $(RAN); (cat $<; echo "cts hold=$(HOLD_SLACK_MARGIN) args=$(CTS_ARGS)") > $@
    $(R)/5_route.odb: $(R)/4_cts.odb
    	@echo route >> $(RAN); (cat $<; echo "route") > $@
    $(R)/6_final.odb: $(R)/5_route.odb
    	@echo finish >> $(RAN); (cat $<; echo "finish") > $@; touch $(R)/6_final.v $(R)/6_final.sdc $(R)/6_final.spef
    metadata-generate:
    	@mkdir -p $(M); python3 -c "import json,os;h=float(os.environ.get('HOLD_SLACK_MARGIN','0') or 0);json.dump({'finish__timing__hold__ws':h+1,'finish__timing__setup__ws':10},open('$(M)/metadata.json','w'))"
''')


@pytest.fixture
def fake_orfs(tmp_path, monkeypatch):
    home = tmp_path / "orfs_home"
    (home / "flow" / "scripts").mkdir(parents=True)
    (home / "flow" / "Makefile").write_text(FAKE_MAKEFILE)
    (home / "flow" / "scripts" / "variables.yaml").write_text("v: 1\n")
    (home / "env.sh").write_text("")
    monkeypatch.setenv("ORFS_HOME", str(home))
    net = tmp_path / "n.v"
    net.write_text("module t; endmodule\n")
    return SimpleNamespace(home=home, net=net, tmp=tmp_path)


def _orfs(f, run, *extra):
    return pnr_mod.main([str(f.net), str(f.tmp / run), "--top", "t",
                         "--clock-port", "clk", "--clock-period", "100",
                         *extra])


def _ran(f, run):
    p = f.tmp / run / "work" / "ran.txt"
    return p.read_text().split() if p.exists() else []


def _final(f, run):
    return (f.tmp / run / "work/results/asap7/t/base/6_final.odb").read_text()


def test_restart_equals_a_straight_run_and_runs_only_the_later_stages(fake_orfs):
    f = fake_orfs
    assert _orfs(f, "straight", "--hold-margin", "7") == 0
    assert _orfs(f, "place", "--hold-margin", "0", "--target", "place") == 0
    assert _ran(f, "place") == ["synth", "floorplan", "place"]
    assert _orfs(f, "rung", "--hold-margin", "7", "--from-dir",
                 str(f.tmp / "place"), "--from-stage", "cts") == 0
    # make ran the stages after the placement only: the copied results were
    # up to date (their modification times and the inputs' were kept)
    assert _ran(f, "rung") == ["cts", "route", "finish"]
    # same results as the straight run, except for the work tree's own paths
    assert _final(f, "rung") == _final(f, "straight")
    assert json.loads((f.tmp / "rung/pnr_metrics.json").read_text()) == \
        json.loads((f.tmp / "straight/pnr_metrics.json").read_text())
    man = json.loads((f.tmp / "rung/stage_manifest.json").read_text())
    assert man["restarted_from"]["stage"] == "cts"
    assert man["completed"][-1] == "finish"


def test_restart_does_not_touch_the_source_run(fake_orfs):
    f = fake_orfs
    assert _orfs(f, "place", "--target", "place") == 0
    before = {p: p.read_bytes() for p in (f.tmp / "place").rglob("*") if p.is_file()}
    assert _orfs(f, "r1", "--hold-margin", "5", "--from-dir",
                 str(f.tmp / "place"), "--from-stage", "cts") == 0
    assert _orfs(f, "r2", "--hold-margin", "9", "--from-dir",
                 str(f.tmp / "place"), "--from-stage", "cts") == 0
    assert before == {p: p.read_bytes() for p in (f.tmp / "place").rglob("*")
                      if p.is_file()}
    assert "hold=5" in _final(f, "r1") and "hold=9" in _final(f, "r2")


@pytest.mark.parametrize("flags,what", [
    (["--utilization", "55"], "CORE_UTILIZATION"),
    (["--set", "FOO=1"], "FOO"),                    # unknown: counts as earlier
    (["--set", "SOMETHING_NEW=1"], "SOMETHING_NEW"),
    (["--setup-margin", "5"], "SETUP_SLACK_MARGIN"),
    (["--hold-margin", "-4"], "HOLD_SLACK_MARGIN"),   # negative: read by the floorplan
    (["--set", "ENABLE_PLACE_REPAIR_TIMING=1", "--hold-margin", "3"], "ENABLE_PLACE"),
])
def test_restart_is_refused_when_an_earlier_stage_option_differs(fake_orfs, flags, what):
    f = fake_orfs
    assert _orfs(f, "place", "--target", "place") == 0
    with pytest.raises(SystemExit) as e:
        _orfs(f, "r", *flags, "--from-dir", str(f.tmp / "place"),
              "--from-stage", "cts")
    assert "refusing to restart" in str(e.value) and what in str(e.value)
    assert not (f.tmp / "r" / "work" / "results").exists()      # nothing ran


def test_restart_is_refused_for_a_changed_netlist_or_a_changed_tool(fake_orfs, monkeypatch):
    f = fake_orfs
    assert _orfs(f, "place", "--target", "place") == 0
    f.net.write_text("module t; wire w; endmodule\n")
    with pytest.raises(SystemExit, match="input .* differs"):
        _orfs(f, "r", "--from-dir", str(f.tmp / "place"), "--from-stage", "cts")
    f.net.write_text("module t; endmodule\n")
    (f.home / "flow" / "scripts" / "variables.yaml").write_text("v: 2\n")
    with pytest.raises(SystemExit, match="tool"):
        _orfs(f, "r2", "--from-dir", str(f.tmp / "place"), "--from-stage", "cts")


def test_restart_needs_a_source_that_got_far_enough(fake_orfs):
    f = fake_orfs
    with pytest.raises(SystemExit, match="no stage_manifest"):
        _orfs(f, "r", "--from-dir", str(f.tmp / "nothing"), "--from-stage", "cts")
    assert _orfs(f, "early", "--target", "floorplan") == 0
    with pytest.raises(SystemExit, match="stopped before the place stage"):
        _orfs(f, "r", "--from-dir", str(f.tmp / "early"), "--from-stage", "cts")
    with pytest.raises(SystemExit, match="go together"):
        _orfs(f, "r", "--from-dir", str(f.tmp / "early"))


def test_restart_at_route_from_a_cts_run(fake_orfs):
    f = fake_orfs
    assert _orfs(f, "straight", "--cts-arg=-foo", "--set", "GLOBAL_ROUTE_ARGS=-x") == 0
    assert _orfs(f, "c", "--cts-arg=-foo", "--target", "cts") == 0
    assert _orfs(f, "r", "--cts-arg=-foo", "--set", "GLOBAL_ROUTE_ARGS=-x",
                 "--from-dir", str(f.tmp / "c"), "--from-stage", "route") == 0
    assert _ran(f, "r")[-2:] == ["route", "finish"]
    assert _final(f, "r") == _final(f, "straight")
    with pytest.raises(SystemExit, match="refusing"):        # cts option differs
        _orfs(f, "r2", "--cts-arg=-bar", "--from-dir", str(f.tmp / "c"),
              "--from-stage", "route")


def test_stage_of_var_table():
    s = pnr_mod.stage_of_var
    assert s("HOLD_SLACK_MARGIN", "10", {}) == "cts"
    assert s("HOLD_SLACK_MARGIN", "-1", {}) == "floorplan"
    assert s("HOLD_SLACK_MARGIN", "10", {"ENABLE_PLACE_REPAIR_TIMING": "1"}) == "floorplan"
    assert s("SETUP_SLACK_MARGIN", "10", {}) == "floorplan"
    assert s("GLOBAL_ROUTE_ARGS", "x", {}) == "route"
    assert s("NEVER_HEARD_OF_IT", "1", {}) == "floorplan"


def test_ladder_over_the_orfs_backend_with_a_fake_orfs(fake_orfs, tmp_path):
    """The whole ladder on the ORFS backend: place once, restart per rung."""
    f = fake_orfs
    cfg = SimpleNamespace(
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "platform": "asap7",
                 "ladder": {"param": "hold_margin", "values": [0, 5, 10],
                            "accept": {"metrics": {"hold_ws": {"min": 7}}}}},
            tie=None, top_module="t", clock_period_ps=100, pack_pnr={},
            pack_root=None, config_dir=tmp_path, wire_rc=None,
            wire_rc_setup=None),
        openroad_netlist=f.net, pnr_dir=tmp_path / "out" / "pnr")
    logs = []
    L.run_flow(cfg, log=logs.append)
    rep = json.loads((cfg.pnr_dir / "ladder" / "RESULT.json").read_text())
    # the fake reports hold_ws = margin + 1: 1, 6 rejected; 11 accepted
    assert rep["accepted"] == 2 and verdicts(rep) == ["rejected", "rejected", "accepted"]
    place_ran = _ran_in(cfg.pnr_dir / "ladder" / "place")
    assert place_ran == ["synth", "floorplan", "place"]
    for i in range(3):
        assert _ran_in(cfg.pnr_dir / "ladder" / f"rung_{i}")[-3:] == [
            "cts", "route", "finish"]
    # identical to a straight run at the accepted value
    assert _orfs(f, "straight", "--hold-margin", "10") == 0
    assert (cfg.pnr_dir / "accepted").resolve().joinpath(
        "work/results/asap7/t/base/6_final.odb").read_text() == _final(f, "straight")
    # a parameter that acts before the restart stage is refused up front
    cfg.design.pnr["ladder"] = {"param": "utilization", "values": [40, 50]}
    with pytest.raises(L.LadderError, match="not known to act"):
        L.run_flow(cfg, log=logs.append)
    cfg.design.pnr["ladder"] = {"param": "set.CORE_UTILIZATION", "values": [40, 50],
                                "from_stage": "cts"}
    shutil.rmtree(cfg.pnr_dir / "ladder")
    with pytest.raises(L.LadderError, match="acts before the restart stage"):
        L.run_flow(cfg, log=logs.append)


def _ran_in(d):
    p = d / "work" / "ran.txt"
    return p.read_text().split() if p.exists() else []


def test_profile_accepts_stage_restart(tmp_path):
    from circt_flow import profile
    p = profile._pnr_section({"pnr": {"command": ["./x"],
                                      "stage_restart": ["place", "cts"]}},
                             tmp_path, tmp_path / "techpack.yaml")
    assert p["stage_restart"] == ["place", "cts"]
    for bad in ("cts", ["cts"], ["a", "a"]):
        with pytest.raises(ValueError, match="stage_restart"):
            profile._pnr_section({"pnr": {"command": ["./x"],
                                          "stage_restart": bad}},
                                 tmp_path, tmp_path / "techpack.yaml")


def test_rungs_take_their_slots_in_order(tmp_path):
    w = World(tmp_path, {str(v): {**GOOD, "hold_ws": -1, "sleep": 0.2}
                         for v in range(5)},
              ladder={"param": "knob", "values": list(range(5)), "jobs": 2,
                      "from_stage": "cts"})
    code, rep = w.run()
    assert code == L.EXIT_NONE_ACCEPTED
    started = [l.split()[3] for l in w.logs if l.endswith("started")]
    assert started == list("01234")


def test_a_failing_placement_stops_before_any_rung(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD})
    w.pnr["ladder"]["values"] = [0, 1]
    (w.tmp / "pack" / "pnr.py").write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "sys.exit(4 if os.environ.get('CIRCT_FLOW_STOP_AFTER') else 0)\n")
    with pytest.raises(L.LadderFailed) as e:
        L.run_flow(w.cfg, log=lambda m: None)
    assert e.value.code == L.EXIT_RUNG_FAILED and "placement run exited 4" in str(e.value)
    assert not (w.cfg.pnr_dir / "ladder" / "rung_0" / "pnr.log").exists()


def test_changing_only_the_first_rung_value_keeps_the_placement(fake_orfs, tmp_path):
    """The placement key leaves out the options of the restart stage and later:
    a different list of margins is not a different placement."""
    f = fake_orfs
    mk = lambda vals: SimpleNamespace(
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "platform": "asap7",
                 "ladder": {"param": "hold_margin", "values": vals,
                            "accept": {"metrics": {"hold_ws": {"min": 0}}}}},
            tie=None, top_module="t", clock_period_ps=100, pack_pnr={},
            pack_root=None, config_dir=tmp_path, wire_rc=None, wire_rc_setup=None),
        openroad_netlist=f.net, pnr_dir=tmp_path / "out" / "pnr")
    logs = []
    L.run_flow(mk([0, 5]), log=logs.append)
    L.run_flow(mk([3, 5, 9]), log=logs.append)          # another first value
    assert any("placement reused" in l for l in logs)
    # a rung that runs after the resume restarted from the old placement: the
    # planning did not touch the placement's input files (make would redo it)
    for i in range(3):
        d = tmp_path / "out" / "pnr" / "ladder" / f"rung_{i}"
        assert _ran_in(d)[-3:] == ["cts", "route", "finish"]
        assert "synth" not in _ran_in(d)


def test_flow_cli_returns_the_ladders_exit_code(monkeypatch, tmp_path, capsys):
    from circt_flow import cli

    def boom(cfg, pnr):
        raise L.LadderFailed("closure ladder: no rung accepted (none)", L.EXIT_NONE_ACCEPTED)
    monkeypatch.setattr(cli, "FlowConfig", SimpleNamespace(load=lambda *a, **k: None))
    monkeypatch.setattr(cli, "run", boom)
    assert cli.main(["flow.yaml", str(tmp_path), "--pnr"]) == 5
    assert "no rung accepted" in capsys.readouterr().err


def test_flow_config_with_a_bad_ladder_is_refused_at_load(tmp_path):
    from circt_flow.config import DesignConfig
    y = tmp_path / "flow.yaml"
    y.write_text("design: {top_module: t, clock_period_ps: 1000}\n"
                 "pdk: {synth_lib_tt: [x.lib]}\n"
                 "pnr: {clock_port: clk, ladder: {param: hold_margin, values: []}}\n")
    with pytest.raises(L.LadderError, match="values: a non-empty list"):
        DesignConfig.from_yaml(y)


def test_the_command_gets_the_rung_value_in_its_pnr_section(tmp_path, monkeypatch):
    """A flow-level derivation from the parameter (here the clock period, which
    pack_command writes into the SDC) sees the rung's value: a parameter that
    changes an input of the placement is therefore caught by the plan."""
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "clock_period", "values": [100, 200],
                      "from_stage": "cts"})
    with pytest.raises(L.LadderError, match="P&R command's inputs differ"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_the_pnr_stage_runs_the_ladder_when_the_flow_config_declares_one(fake_orfs, tmp_path):
    from circt_flow import stages
    f = fake_orfs
    cfg = SimpleNamespace(
        design=SimpleNamespace(
            pnr={"clock_port": "clk", "platform": "asap7",
                 "ladder": {"param": "hold_margin", "values": [0, 5],
                            "accept": {"metrics": {"hold_ws": {"min": 0}}}}},
            tie=None, top_module="t", clock_period_ps=100, pack_pnr={},
            pack_root=None, config_dir=tmp_path, wire_rc=None, wire_rc_setup=None),
        openroad_netlist=f.net, pnr_dir=tmp_path / "out" / "pnr")
    path = stages.orfs_pnr(cfg)
    assert path == cfg.pnr_dir / "pnr_metrics.json"
    assert json.loads(path.read_text())["status"] == "ok"
    assert (cfg.pnr_dir / "accepted").is_symlink()


def _orfs_cfg(f, tmp_path, ladder, extra=None):
    pnr = {"clock_port": "clk", "platform": "asap7", "ladder": ladder, **(extra or {})}
    return SimpleNamespace(
        design=SimpleNamespace(
            pnr=pnr, tie=None, top_module="t", clock_period_ps=100, pack_pnr={},
            pack_root=None, config_dir=tmp_path, wire_rc=None, wire_rc_setup=None),
        openroad_netlist=f.net, pnr_dir=tmp_path / "out" / "pnr")


def _rep(cfg):
    return json.loads((cfg.pnr_dir / "ladder" / "RESULT.json").read_text())


def test_a_changed_later_stage_option_does_not_reuse_the_rung_records(fake_orfs, tmp_path):
    """The placement key leaves out the options of the restart stage and
    later; the rung records must not: a rung is run again when cts_args
    changed, though the placement is reused."""
    f = fake_orfs
    lad = {"param": "hold_margin", "values": [0, 5],
           "accept": {"metrics": {"hold_ws": {"min": 0}}}}
    cfg = _orfs_cfg(f, tmp_path, lad, {"cts_args": ["-aa"]})
    L.run_flow(cfg, log=lambda m: None)
    assert [r["reused"] for r in _rep(cfg)["rungs"]][:1] == [False]
    L.run_flow(cfg, log=lambda m: None)                      # nothing changed
    assert _rep(cfg)["rungs"][0]["reused"] is True
    cfg.design.pnr["cts_args"] = ["-bb"]
    logs = []
    L.run_flow(cfg, log=logs.append)
    assert any("placement reused" in l for l in logs)        # same placement...
    rep = _rep(cfg)
    assert rep["rungs"][0]["reused"] is False                 # ...rungs run again
    final = (cfg.pnr_dir / "accepted").resolve() / "work/results/asap7/t/base/6_final.odb"
    assert "-bb" in final.read_text() and "-aa" not in final.read_text()
    cfg.design.pnr["set"] = {"GLOBAL_ROUTE_ARGS": "-x"}      # a route-stage option
    L.run_flow(cfg, log=lambda m: None)
    assert _rep(cfg)["rungs"][0]["reused"] is False


def test_an_edited_check_script_does_not_reuse_the_rung_records(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    assert w.run()[1]["accepted"] == 0
    assert w.run()[1]["rungs"][0]["reused"] is True
    chk = tmp_path / "check.py"
    chk.write_text(chk.read_text().replace("sys.exit(b.get(\"check_rc\", 0))", "sys.exit(1)"))
    code, rep = w.run()                    # same name, same words, new content
    assert code == L.EXIT_NONE_ACCEPTED
    assert verdicts(rep) == ["rejected", "rejected"] and not rep["rungs"][0]["reused"]


def test_the_accepted_link_of_an_earlier_run_does_not_survive(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0 and (w.cfg.pnr_dir / "accepted").is_symlink()
    w.pnr["ladder"]["accept"] = {"metrics": {"hold_ws": {"min": 1000}}}   # now nothing passes
    code, rep = w.run()
    assert code == L.EXIT_NONE_ACCEPTED
    assert not (w.cfg.pnr_dir / "accepted").exists()
    assert not (w.cfg.pnr_dir / "accepted").is_symlink()


def test_fresh_discards_the_placement_and_the_rung_records(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    n = [e[0] for e in w.events()].count("place")
    w.pnr["ladder"]["fresh"] = True
    code, rep = w.run()
    assert code == 0 and [e[0] for e in w.events()].count("place") == n + 1
    assert rep["rungs"][0]["reused"] is False


def test_a_command_backend_hashes_the_script_and_the_packs_stage_inputs(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    data = tmp_path / "pack" / "data"
    data.mkdir()
    (data / "rules.txt").write_text("a")
    w.cfg.design.pack_pnr["stage_inputs"] = [str(data)]
    assert w.run()[0] == 0
    (tmp_path / "pack" / "pnr.py").write_text(
        (tmp_path / "pack" / "pnr.py").read_text() + "\n# edited\n")
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)                 # the script is hashed
    (tmp_path / "pack" / "pnr.py").write_text(
        (tmp_path / "pack" / "pnr.py").read_text().replace("\n# edited\n", ""))
    assert w.run()[0] == 0
    (data / "rules.txt").write_text("b")
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)                 # so is the pack's data


def test_a_parameter_the_orfs_stage_does_not_read_is_refused(fake_orfs, tmp_path):
    cfg = _orfs_cfg(fake_orfs, tmp_path, {"param": "hold_marign", "values": [0, 5],
                                          "from_stage": "cts"})
    with pytest.raises(L.LadderError, match="rungs would all be the same run"):
        L.run_flow(cfg, log=lambda m: None)


FAKE_STA = textwrap.dedent('''\
    #!/bin/bash
    echo "$@" >> "$STA_LOG"
    [ -n "$STA_SLEEP" ] && sleep "$STA_SLEEP"
    echo CIRCTFLOW-BEGIN
    echo "worst slack max $STA_SETUP"
    echo "worst slack min $STA_HOLD"
    echo CIRCTFLOW-END
''')


_LIB = None


def _fake_sta(tmp_path, monkeypatch, setup="10", hold="3", sleep=None):
    global _LIB
    _LIB = tmp_path / "ff.lib"
    _LIB.write_text("library(ff) {}\n")
    sta = tmp_path / "fake_sta"
    sta.write_text(FAKE_STA)
    sta.chmod(0o755)
    monkeypatch.setenv("OPENSTA", str(sta))
    monkeypatch.setenv("STA_LOG", str(tmp_path / "sta.log"))
    monkeypatch.setenv("STA_SETUP", setup)
    monkeypatch.setenv("STA_HOLD", hold)
    if sleep:
        monkeypatch.setenv("STA_SLEEP", sleep)


def test_corner_check_runs_as_a_job_of_the_ladder(fake_orfs, tmp_path, monkeypatch):
    _fake_sta(tmp_path, monkeypatch, hold="3")
    lad = {"param": "hold_margin", "values": [0],
           "accept": {"metrics": {"hold_ws": {"min": 0}},
                      "corners": {"ff": {"libs": [str(_LIB)], "hold_ws": {"min": 2}}}}}
    cfg = _orfs_cfg(fake_orfs, tmp_path, lad)
    L.run_flow(cfg, log=lambda m: None)
    assert _rep(cfg)["accepted"] == 0
    assert f"read_liberty {_LIB}" in (cfg.pnr_dir / "ladder/rung_0/corner_ff.tcl").read_text()
    monkeypatch.setenv("STA_HOLD", "1")                       # below the corner's bound
    lad["accept"]["metrics"] = {"hold_ws": {"min": -5}}       # new definition: runs again
    with pytest.raises(L.LadderFailed):
        L.run_flow(cfg, log=lambda m: None)
    assert "corner ff: hold_ws 1 < 2" in _rep(cfg)["rungs"][0]["reasons"][0]


def test_a_rejection_stops_the_corner_check(fake_orfs, tmp_path, monkeypatch):
    """The corner STA is a process the ladder started: when the built-in check
    has already rejected the rung it is stopped, not waited for."""
    _fake_sta(tmp_path, monkeypatch, sleep="120")
    lad = {"param": "hold_margin", "values": [0],
           "accept": {"metrics": {"hold_ws": {"min": 100}},     # the fake reports 1
                      "corners": {"ff": {"libs": [str(_LIB)], "hold_ws": {"min": 0}}}}}
    cfg = _orfs_cfg(fake_orfs, tmp_path, lad)
    t0 = time.monotonic()
    with pytest.raises(L.LadderFailed) as e:
        L.run_flow(cfg, log=lambda m: None)
    assert e.value.code == L.EXIT_NONE_ACCEPTED and time.monotonic() - t0 < 60
    assert json.loads((cfg.pnr_dir / "ladder" / "processes.json").read_text()) == []


def test_cancel_without_submit_is_refused_at_run_time(tmp_path):
    w = World(tmp_path, {"0": GOOD},
              ladder={"param": "knob", "values": [0], "from_stage": "cts",
                      "cancel": "scancel --name {name}"})
    with pytest.raises(L.LadderError, match="`cancel` needs `submit`"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_cancel_and_submit_can_both_come_from_the_environment(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path)
    marker = tmp_path / "cancelled.txt"
    w = World(tmp_path, {"0": GOOD, "1": {**GOOD, "wait_for": "never"}},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    monkeypatch.setenv("CIRCT_FLOW_LADDER_SUBMIT", f"{wrapper} --job {{name}} {{cmd}}")
    monkeypatch.setenv("CIRCT_FLOW_LADDER_CANCEL", f"echo {{name}} >> {marker}")
    assert w.run()[0] == 0
    assert marker.read_text().split()


# ---------------------------------------------------------------------------
# Submit commands that return early, results that must not outlive their run,
# process records, option and constant handling
# ---------------------------------------------------------------------------


def test_a_submit_command_that_returns_early_gets_the_cancel_template(tmp_path):
    """`(nohup {cmd} &)` returns at once and leaves the job running: the
    ladder must cancel it by name."""
    marker = tmp_path / "cancelled.txt"
    w = World(tmp_path, {"0": {**GOOD, "sleep": 2}, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": "(nohup {cmd} >/dev/null 2>&1 &)",
                      "cancel": f"echo {{name}} >> {marker}"})
    with pytest.raises(L.LadderFailed, match="completion marker"):
        L.run_flow(w.cfg, log=lambda m: None)
    names = marker.read_text().split()
    assert names and all(n.startswith("ladder-") for n in names)
    assert json.loads((w.cfg.pnr_dir / "ladder" / "processes.json").read_text()) == []


def test_an_early_returning_job_without_cancel_stays_on_record_and_is_reported(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": "(nohup {cmd} >/dev/null 2>&1 &)"})
    with pytest.raises(L.LadderFailed):
        L.run_flow(w.cfg, log=lambda m: None)
    rec = json.loads((w.cfg.pnr_dir / "ladder" / "processes.json").read_text())
    assert len(rec) == 1 and rec[0]["orphan"] is True and rec[0]["jobname"]
    logs = []
    with pytest.raises(L.LadderFailed):
        L.run_flow(w.cfg, log=logs.append)
    assert any("WARNING: leftover job not stopped" in l for l in logs)


def test_a_run_that_raises_leaves_no_old_result_that_says_accepted(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    res = w.cfg.pnr_dir / "ladder" / "RESULT.json"
    assert json.loads(res.read_text())["state"] == "accepted"
    w.net.write_text("module t(input a); endmodule\n")          # stale placement
    with pytest.raises(L.LadderError):
        L.run_flow(w.cfg, log=lambda m: None)
    r = json.loads(res.read_text())
    assert r["state"] == "refused" and r["accepted"] is None
    assert "different inputs" in r["message"]
    assert not (w.cfg.pnr_dir / "accepted").exists()
    assert "closure ladder" in (w.cfg.pnr_dir / "pnr_metrics.json").read_text()


def test_a_failed_placement_writes_an_error_result(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    shutil.rmtree(w.cfg.pnr_dir / "ladder" / "place")
    (tmp_path / "pack" / "pnr.py").write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.exit(4)\n")
    # the script changed: a fresh placement is needed and it fails
    with pytest.raises(L.LadderFailed):
        L.run_flow(w.cfg, log=lambda m: None, fresh=True)
    r = json.loads((w.cfg.pnr_dir / "ladder" / "RESULT.json").read_text())
    assert r["state"] == "error" and "placement run exited 4" in r["message"]


def test_a_second_orchestrator_does_not_touch_the_first_ones_results(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "wait_for": "release"}, "1": GOOD})
    spec = L.parse_ladder(w.pnr["ladder"])
    a = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir, log=lambda m: None)
    th = threading.Thread(target=a.run)
    th.start()
    _wait_until(lambda: any(e[0] == "begin" and e[1] == "0" for e in w.events()))
    res = w.cfg.pnr_dir / "ladder" / "RESULT.json"
    final = w.cfg.pnr_dir / "pnr_metrics.json"
    final.write_text("{}")
    with pytest.raises(L.LadderBusy):
        L.run_flow(w.cfg, log=lambda m: None)
    assert final.read_text() == "{}"               # not removed by the refused run
    (tmp_path / "release").write_text("")
    th.join(30)


def test_a_changed_wire_rc_file_is_not_reused(tmp_path, monkeypatch):
    """The environment the flow builds for the pack command (the pack's wire
    RC files) is part of the placement key."""
    from circt_flow import stages
    rc = tmp_path / "rc.tcl"
    rc.write_text("set_layer_rc a\n")
    real = stages.pack_command

    def with_rc(cfg, out):
        cmd, env = real(cfg, out)
        return cmd, {**env, "CIRCT_FLOW_WIRE_RC": str(rc)}
    monkeypatch.setattr(stages, "pack_command", with_rc)
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    assert w.run()[1]["rungs"][0]["reused"] is True
    rc.write_text("set_layer_rc b\n")
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_a_new_format_version_does_not_honour_old_records(tmp_path, monkeypatch):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    monkeypatch.setattr(L, "FORMAT", L.FORMAT + 1)
    with pytest.raises(L.LadderError, match="different inputs or options"):
        L.run_flow(w.cfg, log=lambda m: None)
    L.run_flow(w.cfg, log=lambda m: None, fresh=True)
    assert json.loads((w.cfg.pnr_dir / "ladder/RESULT.json").read_text())["rungs"][0]["reused"] is False


def test_the_corner_binary_and_image_are_part_of_the_acceptance_digest(monkeypatch):
    monkeypatch.delenv("OPENSTA", raising=False)
    monkeypatch.delenv("ORFS_IMAGE", raising=False)
    corner = (L.CornerCheck("ff", ("x",), ()),)
    a = L.Accept(corners=corner).digest()
    plain = L.Accept().digest()
    monkeypatch.setenv("ORFS_IMAGE", "other/image:1")
    assert L.Accept(corners=corner).digest() != a
    assert L.Accept().digest() == plain          # no corners: nothing runs OpenSTA
    monkeypatch.delenv("ORFS_IMAGE")
    monkeypatch.setenv("OPENSTA", "/nonexistent/sta")
    assert L.Accept(corners=corner).digest() != a


def test_reap_stale_never_signals_without_a_start_time_or_from_another_host(tmp_path, monkeypatch):
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        r = L.Runner(tmp_path, grace=0.2)
        rec = [{"name": "x", "pid": victim.pid, "start": None,
                "host": L._host_id(), "jobname": ""},
               {"name": "y", "pid": victim.pid, "start": L._proc_start(victim.pid),
                "host": "elsewhere/boot", "jobname": ""}]
        (tmp_path / "processes.json").write_text(json.dumps(rec))
        monkeypatch.setattr(L, "_proc_start", lambda pid: None)     # no /proc
        assert r.reap_stale() == []
        assert victim.poll() is None
        # another host: not touched either, and reported
        monkeypatch.undo()
        (tmp_path / "processes.json").write_text(json.dumps(rec[1:]))
        r2 = L.Runner(tmp_path, grace=0.2)
        assert r2.reap_stale() == [] and victim.poll() is None
        assert any("another host" in u for u in r2.unresolved)
    finally:
        victim.kill()
        victim.wait()


def test_the_rung_directory_is_emptied_before_a_rung_runs(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    old = w.cfg.pnr_dir / "ladder" / "rung_0" / "report_of_the_last_run.txt"
    old.write_text("stale")
    w.pnr["ladder"]["accept"] = {"metrics": {"hold_ws": {"min": 0}}}   # runs again
    assert w.run()[0] == 0
    assert not old.exists()


def test_a_process_that_cannot_be_recorded_is_killed(tmp_path, monkeypatch):
    r = L.Runner(tmp_path)
    pids = []
    real = subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        pids.append(p.pid)
        return p
    monkeypatch.setattr(subprocess, "Popen", spy)

    calls = []

    def full_disk(self):                     # the intent record is written, the completed one is not
        calls.append(1)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(L.Runner, "_save", full_disk)
    job = L.Job("j", ["sleep", "60"], dict(os.environ), tmp_path, tmp_path / "j.log")
    with pytest.raises(OSError, match="No space"):
        r.spawn(job)
    assert pids and _gone(pids[0])


def test_a_check_that_crashes_with_a_traceback_is_unverified_not_a_rejection(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    (tmp_path / "check.py").write_text("#!/usr/bin/env python3\nraise RuntimeError('bug')\n")
    code, rep = w.run()
    assert code == L.EXIT_UNVERIFIED and rep["state"] == "unverified"
    assert "Python traceback" in rep["rungs"][0]["reasons"][0]
    (tmp_path / "check.py").write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    code, rep = w.run()                          # a deliberate exit 1 is a rejection
    assert code == L.EXIT_NONE_ACCEPTED


def test_values_that_are_equal_numbers_are_the_same_rung():
    with pytest.raises(L.LadderError, match="must differ"):
        L.parse_ladder({"param": "p", "values": [0, 0.0]})
    with pytest.raises(L.LadderError, match="all numbers or none"):
        L.parse_ladder({"param": "p", "values": [0, "0"]})


def test_corner_names_are_file_name_safe():
    with pytest.raises(L.LadderError, match="corner name"):
        L.parse_ladder({"param": "p", "values": [1], "accept": {"corners": {
            "a/b": {"libs": ["x"], "hold_ws": {"min": 0}}}}})


def test_job_names_have_a_fixed_width_counter(tmp_path):
    """No name is a prefix of another of the same ladder (a cancel template
    that matches by substring cannot hit its siblings)."""
    r = L.Runner(tmp_path, submit="{cmd}")
    job = L.Job("j", ["true"], dict(os.environ), tmp_path, tmp_path / "j.log")
    names = []
    for _ in range(12):
        h = r.spawn(job)
        h.wait(threading.Event())
        names.append(h.jobname)
    assert all(re.fullmatch(r"ladder-\d+-[0-9a-f]{10}-\d{6}", n) for n in names)
    assert not any(a != b and a in b for a in names for b in names)


# ---------------------------------------------------------------------------
# Paths, process groups, markers, records, hashing, preflight, signals
# ---------------------------------------------------------------------------


def _world_in(tmp_path, rel, plan=None, **kw):
    """A World whose output directory is the given path (relative to cwd)."""
    w = World(tmp_path, plan or {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"}, **kw)
    w.cfg.pnr_dir = Path(rel)
    return w


def test_a_relative_output_directory_works(tmp_path, monkeypatch):
    w = _world_in(tmp_path, "out/pnr")
    monkeypatch.chdir(tmp_path)
    L.run_flow(w.cfg, log=lambda m: None)
    out = tmp_path / "out" / "pnr"
    res = json.loads((out / "ladder" / "RESULT.json").read_text())
    assert res["state"] == "accepted"
    assert (out / "accepted").resolve() == (out / "ladder" / "rung_0").resolve()
    assert json.loads((out / "pnr_metrics.json").read_text())["status"] == "ok"


def test_a_symlinked_or_moved_output_directory_works(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    w = _world_in(tmp_path, str(tmp_path / "link" / "pnr"))
    L.run_flow(w.cfg, log=lambda m: None)
    assert (real / "pnr" / "accepted").is_symlink()
    # moved: the link is relative, so it still resolves after the move
    (tmp_path / "moved").mkdir()
    shutil.move(str(real / "pnr"), str(tmp_path / "moved" / "pnr"))
    assert (tmp_path / "moved" / "pnr" / "accepted" / "pnr_metrics.json").is_file()


def test_a_path_with_a_space_and_a_quote_works(tmp_path):
    odd = tmp_path / "a b'c $x"
    odd.mkdir()
    w = _world_in(tmp_path, str(odd / "pnr"))
    wrapper = _wrapper(tmp_path)
    w.pnr["ladder"]["submit"] = f"{wrapper} --job {{name}} {{cmd}}"
    L.run_flow(w.cfg, log=lambda m: None)
    assert (odd / "pnr" / "accepted").is_symlink()


def test_a_relative_netlist_reaches_the_orfs_job_as_an_absolute_path(fake_orfs, tmp_path, monkeypatch):
    from circt_flow import stages
    cfg = _orfs_cfg(fake_orfs, tmp_path, {"param": "hold_margin", "values": [0]})
    monkeypatch.chdir(tmp_path)
    cfg.openroad_netlist = Path(os.path.relpath(fake_orfs.net, tmp_path))
    cfg.pnr_dir = Path("rel_out/pnr")
    argv = stages.orfs_argv(cfg, cfg.design.pnr, cfg.pnr_dir)
    assert argv[0] == str(fake_orfs.net) and os.path.isabs(argv[1])


def test_a_group_member_that_ignores_sigterm_is_killed_and_the_group_is_checked(tmp_path):
    """The leader exits on SIGTERM, a child ignores it: the whole group gets
    SIGKILL after the grace period, and the ladder checks nothing is left."""
    r = L.Runner(tmp_path, grace=0.3)
    script = "(trap '' TERM; exec sleep 60) & trap 'exit 0' TERM; wait"
    job = L.Job("j", ["bash", "-c", script], dict(os.environ), tmp_path, tmp_path / "j.log")
    h = r.spawn(job)
    time.sleep(0.5)                       # the child is running
    assert L._group_alive(h.pgid)
    ev = threading.Event()
    ev.set()
    assert h.wait(ev) is None
    assert not L._group_alive(h.pgid)


def test_the_leader_is_not_reaped_before_the_group_is_killed(tmp_path, monkeypatch):
    """The wait before SIGKILL does not reap: the group id stays reserved."""
    p = subprocess.Popen(["bash", "-c", "exit 0"])
    assert L._wait_exit_nowait(p.pid, 5)
    assert Path(f"/proc/{p.pid}").exists()      # still a zombie, not reaped
    p.wait()


def test_a_job_started_twice_runs_once_and_is_judged_on_the_first(tmp_path):
    """A requeued or duplicated scheduler job: the second launcher refuses."""
    wrapper = tmp_path / "twice.sh"
    wrapper.write_text("#!/bin/bash\nshift 2\n\"$@\"; \"$@\"\n")
    wrapper.chmod(0o755)
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": f"{wrapper} --job {{name}} {{cmd}}"})
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 0
    begins = [e[1] for e in w.events() if e[0] == "begin"]
    assert begins.count("0") == 1               # not run a second time


def test_a_marker_that_carries_another_attempts_token_is_not_accepted(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": ": {cmd}; echo '0 deadbeef' > {log}.{name}.status"})
    with pytest.raises(L.LadderFailed, match="completion marker"):
        L.run_flow(w.cfg, log=lambda m: None)


def test_the_launcher_refuses_a_second_start_and_clears_nothing_it_did_not_write(tmp_path):
    status = tmp_path / "m.status"
    assert L._exec(str(status), "tok", ["true"]) == 0
    assert status.read_text().split() == ["0", "tok"]
    assert L._exec(str(status), "tok", ["false"]) == 125     # second start
    assert status.read_text().split() == ["0", "tok"]


def test_cancel_runs_for_scheduler_jobs_of_another_host_and_drops_their_records(tmp_path):
    marker = tmp_path / "c.txt"
    r = L.Runner(tmp_path, cancel=f"echo {{name}} >> {marker}", grace=0.1)
    me_host, _, me_boot = L._host_id().partition("/")
    (tmp_path / "processes.json").write_text(json.dumps([
        {"name": "far job", "pid": 1, "start": "1", "host": "elsewhere/boot9",
         "jobname": "ladder-far-1"},
        {"name": "far process", "pid": 2, "start": "1", "host": "elsewhere/boot9",
         "jobname": ""},
        {"name": "before reboot", "pid": 3, "start": "1",
         "host": f"{me_host}/an-older-boot", "jobname": ""}]))
    r.reap_stale()
    assert "ladder-far-1" in marker.read_text()
    kept = json.loads((tmp_path / "processes.json").read_text())
    assert [e["name"] for e in kept] == ["far process"]      # only the local process of another host
    assert len(r.unresolved) == 1 and "another host" in r.unresolved[0]


def test_unreadable_files_and_broken_links_are_errors_when_hashed(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / "dangling").symlink_to(tmp_path / "nowhere")
    with pytest.raises(L.LadderError, match="dangling"):
        L._path_sha(d)
    (d / "dangling").unlink()
    if os.geteuid() != 0:
        f = d / "secret"
        f.write_text("x")
        f.chmod(0)
        try:
            with pytest.raises(L.LadderError, match="secret"):
                L._path_sha(d)
            with pytest.raises(L.LadderError, match="secret"):
                L._file_sha(f)
        finally:
            f.chmod(0o600)


def test_hashes_follow_symbolic_links_and_survive_a_link_loop(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_text("one")
    d = tmp_path / "d"
    d.mkdir()
    (d / "via").symlink_to(real)
    h1 = L._path_sha(d)
    (real / "f").write_text("two")
    assert L._path_sha(d) != h1                   # a change behind the link
    (real / "loop").symlink_to(d)                 # d -> real -> d ...
    L._path_sha(d)                                # terminates


def test_the_orfs_tree_hash_follows_links_and_names_the_bad_file(tmp_path):
    from circt_flow import orfs_pnr as P
    d = tmp_path / "plat"
    d.mkdir()
    (d / "lib").symlink_to(tmp_path)
    (tmp_path / "x").write_text("1")
    h = P._tree_sha(d)
    (tmp_path / "x").write_text("2")
    assert P._tree_sha(d) != h
    (tmp_path / "broken").symlink_to(tmp_path / "gone")
    with pytest.raises(P.HashError, match="broken"):
        P._tree_sha(d)


def test_corners_with_a_pack_command_are_refused_before_anything_runs(tmp_path):
    w = World(tmp_path, {"0": GOOD},
              ladder={"param": "knob", "values": [0], "from_stage": "cts",
                      "accept": {"corners": {"ff": {"libs": ["x"], "hold_ws": {"min": 0}}}}})
    with pytest.raises(L.LadderError, match="accept.corners needs the built-in ORFS stage"):
        L.run_flow(w.cfg, log=lambda m: None)
    assert w.events() == []


def test_a_missing_check_command_or_library_is_refused_before_anything_runs(tmp_path, fake_orfs):
    w = World(tmp_path, {"0": GOOD},
              ladder={"param": "knob", "values": [0], "from_stage": "cts",
                      "accept": {"command": ["no_such_check_command"]}})
    with pytest.raises(L.LadderError, match="not an executable file"):
        L.run_flow(w.cfg, log=lambda m: None)
    assert w.events() == []
    (tmp_path / "plain.txt").write_text("x")
    w.pnr["ladder"]["accept"] = {"command": [str(tmp_path / "plain.txt")]}   # not executable
    with pytest.raises(L.LadderError, match="not an executable file"):
        L.run_flow(w.cfg, log=lambda m: None)
    cfg = _orfs_cfg(fake_orfs, tmp_path, {"param": "hold_margin", "values": [0], "accept": {
        "corners": {"ff": {"libs": [str(tmp_path / "missing.lib")], "hold_ws": {"min": 0}}}}})
    with pytest.raises(L.LadderError, match="does not exist"):
        L.run_flow(cfg, log=lambda m: None)


def test_ends_in_traceback_only_for_a_crash():
    crash = ("starting\nTraceback (most recent call last):\n  File \"c.py\", line 3, in <module>\n"
             "    run()\nValueError: boom\n")
    assert L.ends_in_traceback(crash)
    assert L.ends_in_traceback("Traceback (most recent call last):\n  File \"x\"\nmodule.CustomError\n")
    # a script that printed a traceback and then gave its own verdict
    assert not L.ends_in_traceback(crash + "REJECT: hold slack too small\n")
    assert not L.ends_in_traceback("no traceback here\n")
    assert not L.ends_in_traceback("Traceback (most recent call last):\n  File \"x\"\nsome note\n")


def test_a_deliberate_exit_1_after_a_printed_traceback_is_a_rejection(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"]}})
    (tmp_path / "check.py").write_text(
        "#!/usr/bin/env python3\nimport sys, traceback\n"
        "try:\n    raise RuntimeError('a failing test')\nexcept RuntimeError:\n"
        "    traceback.print_exc()\nprint('REJECT: hold slack too small')\nsys.exit(1)\n")
    code, rep = w.run()
    assert code == L.EXIT_NONE_ACCEPTED and verdicts(rep) == ["rejected", "rejected"]


@pytest.mark.parametrize("values,msg", [
    ([True, 1], "boolean"), ([0, False], "boolean"),
    ([float("nan"), 1.0], "not a finite"), ([1.0, float("inf")], "not a finite"),
    ([10 ** 400, 1], "out of range"), ([0, 1, 0.5], "must increase"),
    ([2, 1], "must increase"), ([1, 1.0], "must differ"),
])
def test_rung_values_are_validated(values, msg):
    with pytest.raises(L.LadderError, match=msg):
        L.parse_ladder({"param": "p", "values": values})


def test_values_that_are_not_all_numbers_keep_the_order_given():
    L.parse_ladder({"param": "p", "values": ["b", "a"]})
    L.parse_ladder({"param": "p", "values": [["-x"], ["-y"]]})


def test_tcl_words_and_mount_paths_are_safe(tmp_path, fake_orfs, monkeypatch):
    assert L._tcl_word("a b$c[d]{e};f") == r"a\ b\$c\[d\]\{e\}\;f"
    monkeypatch.delenv("OPENSTA", raising=False)
    cfg = _orfs_cfg(fake_orfs, tmp_path, {"param": "hold_margin", "values": [0]})
    be = L.OrfsBackend(cfg, L.parse_ladder(cfg.design.pnr["ladder"]))
    odd = tmp_path / "a:b" / "base"
    res = odd / "work" / "results" / "asap7" / "t" / "base"
    res.mkdir(parents=True)
    for n in ("v", "sdc", "spef"):
        (res / f"6_final.{n}").write_text("")
    lib = tmp_path / "ff lib.lib"
    lib.write_text("")
    with pytest.raises(L.LadderError, match="cannot be mounted"):
        be.corner_job(odd, L.CornerCheck("ff", (str(lib),), ()))
    ok = tmp_path / "plain"
    res2 = ok / "work" / "results" / "asap7" / "t" / "base"
    res2.mkdir(parents=True)
    for n in ("v", "sdc", "spef"):
        (res2 / f"6_final.{n}").write_text("")
    be.corner_job(ok, L.CornerCheck("ff", (str(lib),), ()))
    assert r"read_liberty " + str(lib).replace(" ", r"\ ") in (ok / "corner_ff.tcl").read_text()


def test_orphan_never_signals_a_group_whose_leader_was_reaped(tmp_path):
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        r = L.Runner(tmp_path, submit="{cmd}", cancel="false")
        job = L.Job("j", ["true"], dict(os.environ), tmp_path, tmp_path / "j.log")
        h = r.spawn(job)
        h.popen.wait()
        h.pgid = bystander.pid               # as if the pid had been reused
        assert r.orphan(h) is False          # the failing cancel keeps the record
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_half_made_placement_is_emptied_before_placing_again(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    junk = w.cfg.pnr_dir / "ladder" / "place" / "left_by_an_interrupted_placement.txt"
    junk.parent.mkdir(parents=True)
    junk.write_text("x")
    assert w.run()[0] == 0
    assert not junk.exists()


# -- the built-in checks, one case each (they guard the claim "accepted only if
#    every check ran and passed")


def test_a_check_that_times_out_is_unverified_never_accepted(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "check_sleep": 60}, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "accept": {"command": ["./check.py"], "timeout": 1}})
    t0 = time.monotonic()
    code, rep = w.run()
    assert code == L.EXIT_UNVERIFIED and rep["rungs"][0]["verdict"] == "unverified"
    assert "timed out" in rep["rungs"][0]["reasons"][0]
    assert time.monotonic() - t0 < 120


def test_the_final_metrics_are_those_of_the_accepted_rung(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "hold_ws": -1.0},
                         "1": {**GOOD, "setup_ws": 11.0, "hold_ws": 12.0},
                         "2": {**GOOD, "setup_ws": 21.0, "hold_ws": 22.0}})
    code, rep = w.run()
    assert code == 0 and rep["accepted"] == 1
    final = json.loads((w.cfg.pnr_dir / "pnr_metrics.json").read_text())
    assert final["setup_ws"] == 11.0 and final["hold_ws"] == 12.0


def test_a_boolean_or_nan_metric_is_not_a_number(tmp_path):
    for bad in (True, None):
        w = World(tmp_path / str(bad), {"0": {**GOOD, "hold_ws": bad}, "1": GOOD}) \
            if (tmp_path / str(bad)).mkdir() is None else None
        code, rep = w.run()
        assert code == L.EXIT_UNVERIFIED and rep["rungs"][0]["verdict"] == "unverified"
    assert L.check_metrics({"setup_ws": True, "hold_ws": 1, "drc_errors": 0}, L.Accept().checks)[0] == L.UNVERIFIED


def test_a_p_and_r_run_that_fails_after_writing_metrics_is_an_error(tmp_path):
    w = World(tmp_path, {"0": {**GOOD, "rc_late": 3}, "1": GOOD})
    code, rep = w.run()
    assert code == L.EXIT_RUNG_FAILED and rep["rungs"][0]["verdict"] == "error"


def test_a_record_of_another_format_or_placement_is_not_reused(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts"})
    assert w.run()[0] == 0
    rec = w.cfg.pnr_dir / "ladder" / "rung_0" / "rung_result.json"
    for field in ("format", "placement"):
        data = json.loads(rec.read_text())
        assert data[field] is not None
        data[field] = "something else"
        rec.write_text(json.dumps(data))
        code, rep = w.run()
        assert rep["rungs"][0]["reused"] is False        # run again, not trusted


def test_a_check_thread_that_leaves_no_verdict_cannot_pass(tmp_path, monkeypatch):
    w = World(tmp_path, {"0": GOOD})
    spec = L.parse_ladder(w.pnr["ladder"])
    lad = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir, log=lambda m: None)

    def tasks(i, d, metrics, stop):
        def dies(s):
            raise SystemExit(1)            # not an Exception: no verdict is recorded
        return [("metrics", lambda s: (L.ACCEPTED, [])), ("dies", dies)]
    monkeypatch.setattr(lad, "_check_tasks", tasks)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    verdict, why = lad._judge(0, tmp_path, {}, threading.Event())
    assert verdict == L.UNVERIFIED and "no verdict from dies" in why[0]


# -- signals sent to a running orchestrator

DRIVER = '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {root!r})
from circt_flow import ladder as L
c = json.load(open({cfg!r}))
cfg = SimpleNamespace(
    design=SimpleNamespace(pnr=c["pnr"], tie=None, top_module="t", clock_period_ps=1000,
                           pack_pnr={{"command": c["command"], "stage_restart": ["place", "cts", "route"]}},
                           pack_root=Path(c["pack_root"]), config_dir=Path(c["tmp"]),
                           wire_rc=None, wire_rc_setup=None),
    openroad_netlist=Path(c["net"]), pnr_dir=Path(c["out"]))
try:
    L.run_flow(cfg)
except L.LadderFailed as e:
    sys.exit(e.code)
'''


def _orchestrator(w, tmp_path):
    cfgf = tmp_path / "driver_cfg.json"
    cfgf.write_text(json.dumps({
        "pnr": w.pnr, "command": w.cfg.design.pack_pnr["command"],
        "pack_root": str(w.cfg.design.pack_root), "tmp": str(tmp_path),
        "net": str(w.net), "out": str(w.cfg.pnr_dir)}))
    drv = tmp_path / "driver.py"
    root = str(Path(L.__file__).resolve().parent.parent)
    drv.write_text(DRIVER.format(root=root, cfg=str(cfgf)))
    return subprocess.Popen([sys.executable, str(drv)], env=dict(os.environ))


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_rungs_stops_everything_and_says_so(tmp_path, sig):
    w = World(tmp_path, {str(v): {**GOOD, "wait_for": "never"} for v in range(3)})
    p = _orchestrator(w, tmp_path)
    try:
        _wait_until(lambda: sum(e[0] == "begin" for e in w.events()) == 3, 60)
        p.send_signal(sig)
        assert p.wait(60) == L.EXIT_INTERRUPTED
    finally:
        if p.poll() is None:
            p.kill()
    out = w.cfg.pnr_dir
    assert json.loads((out / "ladder" / "RESULT.json").read_text())["state"] == "interrupted"
    assert not (out / "accepted").exists()
    assert "interrupted" in (out / "pnr_metrics.json").read_text()
    for e in w.events():
        if e[0] == "begin":
            assert _gone(int(e[2].split("=")[1]))


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_placement_is_reported_as_interrupted(tmp_path, sig):
    w = World(tmp_path, {"0": {**GOOD, "place_wait": "never"}, "1": GOOD})
    p = _orchestrator(w, tmp_path)
    try:
        _wait_until(lambda: any(e[0] == "place" for e in w.events()), 60)
        p.send_signal(sig)
        assert p.wait(60) == L.EXIT_INTERRUPTED
    finally:
        if p.poll() is None:
            p.kill()
    out = w.cfg.pnr_dir
    assert json.loads((out / "ladder" / "RESULT.json").read_text())["state"] == "interrupted"
    assert "closure ladder: interrupted" in (out / "pnr_metrics.json").read_text()
    for e in w.events():
        if e[0] == "place":
            assert _gone(int(e[2].split("=")[1]))


def test_a_zombie_is_not_a_running_process_of_the_group(tmp_path):
    """Where nothing reaps orphans (a container without init) a killed job
    stays a zombie: the group counts as gone."""
    code = ("import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n    os.setpgid(0, 0)\n    os._exit(0)\n"
            "print(pid, flush=True)\ntime.sleep(30)\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        child = int(p.stdout.readline())
        _wait_until(lambda: "Z" in Path(f"/proc/{child}/stat").read_text().split(")")[-1][:3])
        assert Path(f"/proc/{child}").exists()          # still there, as a zombie
        assert not L._group_alive(child)
    finally:
        p.kill()
        p.wait()


# ---------------------------------------------------------------------------
# Templates, unkillable processes, container kill retry, records, paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sub,msg", [
    ({"submit": "srun {cmd.foo}"}, "is not a placeholder"),
    ({"submit": "srun {cmd[0]}"}, "is not a placeholder"),
    ({"submit": "srun {cmd!r}"}, "is not a placeholder"),
    ({"submit": "srun {cmd:>10}"}, "is not a placeholder"),
    ({"submit": "srun {{cmd}}"}, "must contain"),
    ({"submit": "srun {name}"}, "must contain"),
    ({"submit": "srun {cmd}", "cancel": "true"}, "must contain {name}"),
    ({"submit": "srun {cmd}", "cancel": "scancel {{name}}"}, "must contain {name}"),
    ({"submit": "srun {cmd"}, "not a valid template"),
])
def test_templates_are_checked_field_by_field(sub, msg):
    with pytest.raises(L.LadderError, match=msg):
        L.parse_ladder({"param": "p", "values": [1], **sub})


def test_a_good_template_is_accepted_with_cmdq_and_name():
    L.parse_ladder({"param": "p", "values": [1], "submit": "sbatch --job-name={name} -o {log} --wrap {cmdq}",
                    "cancel": "scancel --name {name}"})


@pytest.mark.parametrize("bad", [0, -1])
def test_jobs_must_be_a_positive_integer_also_from_the_command_line(tmp_path, bad):
    w = World(tmp_path, {"0": GOOD})
    with pytest.raises(L.LadderError, match="--jobs must be an integer >= 1"):
        L.run_flow(w.cfg, jobs=bad, log=lambda m: None)


def test_a_process_that_survives_sigkill_stops_the_ladder_and_stays_on_record(tmp_path, monkeypatch):
    w = World(tmp_path, {"0": {**GOOD, "wait_for": "never"}, "1": GOOD})
    monkeypatch.setattr(L, "_group_alive", lambda pgid: True)     # as if SIGKILL had not worked
    out = {}

    def go():
        try:
            L.run_flow(w.cfg, log=lambda m: None)
        except BaseException as e:
            out["e"] = e
    # rung 1 passes at once and rung 0 is held: nothing cancels rung 0 until the
    # ladder stops; force the kill by interrupting through the stop event
    spec = L.parse_ladder(w.pnr["ladder"])
    lad = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir, log=lambda m: None)
    th = threading.Thread(target=lambda: out.update(r=_catch(lad.run)))
    th.start()
    _wait_until(lambda: any(e[0] == "begin" and e[1] == "0" for e in w.events()))
    lad.stop.set()
    for e in lad.rung_stop:
        e.set()
    th.join(60)
    assert isinstance(out["r"], L.LadderStuck)
    assert "kill -9 -" in str(out["r"]) and "another output directory" in str(out["r"])
    rec = json.loads((w.cfg.pnr_dir / "ladder" / "processes.json").read_text())
    assert any(e.get("unkillable") for e in rec)
    res = json.loads((w.cfg.pnr_dir / "ladder" / "RESULT.json").read_text())
    assert res["state"] == "refused"


def _catch(fn):
    try:
        return fn()
    except BaseException as e:
        return e


def test_a_later_run_and_fresh_refuse_while_the_recorded_group_is_alive(tmp_path):
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        w = World(tmp_path, {"0": GOOD})
        base = w.cfg.pnr_dir / "ladder"
        base.mkdir(parents=True)
        (base / "processes.json").write_text(json.dumps([
            {"name": "j", "pid": victim.pid, "start": L._proc_start(victim.pid),
             "host": L._host_id(), "jobname": "", "unkillable": True}]))
        for fresh in (False, True):
            with pytest.raises(L.LadderStuck, match="survived SIGKILL"):
                L.run_flow(w.cfg, log=lambda m: None, fresh=fresh)
        victim.kill()
        victim.wait()
        L.run_flow(w.cfg, log=lambda m: None)          # gone: the record is dropped
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait()


def test_the_container_kill_is_retried_until_the_container_exists(tmp_path):
    counter = tmp_path / "calls"
    fake = tmp_path / "fake_docker"
    fake.write_text("#!/bin/bash\necho x >> %s\n[ $(wc -l < %s) -ge 4 ]\n" % (counter, counter))
    fake.chmod(0o755)
    r = L.Runner(tmp_path, grace=0.2)
    job = L.Job("sta", ["sleep", "60"], dict(os.environ), tmp_path, tmp_path / "s.log",
                local=True, kill_cmd=[str(fake), "kill", "c"])
    h = r.spawn(job)
    ev = threading.Event()
    ev.set()
    assert h.wait(ev) is None
    assert len(counter.read_text().split()) == 4         # three failures, then it succeeded


def test_the_record_is_written_before_the_job_starts(tmp_path, monkeypatch):
    """A hard kill between starting a job and completing its record leaves an
    intent record with a job name: a later run cancels it by name."""
    seen = []
    real = subprocess.Popen

    def spy(*a, **k):
        seen.append(json.loads((tmp_path / "processes.json").read_text()))
        return real(*a, **k)
    monkeypatch.setattr(subprocess, "Popen", spy)
    r = L.Runner(tmp_path, submit="{cmd}")
    h = r.spawn(L.Job("j", ["true"], dict(os.environ), tmp_path, tmp_path / "j.log"))
    h.wait(threading.Event())
    assert len(seen[0]) == 1 and seen[0][0]["pid"] is None and seen[0][0]["jobname"].startswith("ladder-")
    marker = tmp_path / "c.txt"
    r2 = L.Runner(tmp_path, cancel=f"echo {{name}} >> {marker}", grace=0.1)
    (tmp_path / "processes.json").write_text(json.dumps(seen[0]))
    r2.reap_stale()
    assert seen[0][0]["jobname"] in marker.read_text()


def test_reap_stale_ignores_a_record_of_an_older_boot_even_if_pid_and_start_match(tmp_path):
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        host = L._host_id().partition("/")[0]
        (tmp_path / "processes.json").write_text(json.dumps([
            {"name": "x", "pid": victim.pid, "start": L._proc_start(victim.pid),
             "host": f"{host}/some-earlier-boot", "jobname": ""}]))
        L.Runner(tmp_path, grace=0.2).reap_stale()
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_unresolved_records_say_how_to_clear_them(tmp_path):
    (tmp_path / "processes.json").write_text(json.dumps([
        {"name": "far", "pid": 5, "start": "1", "host": "elsewhere/b", "jobname": ""},
        {"name": "orph", "pid": 6, "start": "1", "host": L._host_id(),
         "jobname": "ladder-x-000001", "orphan": True}]))
    r = L.Runner(tmp_path)
    r.reap_stale()
    assert len(r.unresolved) == 2 and all("delete" in u and "processes.json" in u for u in r.unresolved)


def test_a_user_check_behind_a_non_blocking_submit_is_unverified(tmp_path):
    """Only the check job's submit returns early; the P&R jobs block."""
    tpl = ("if [[ {log} == *check.log ]]; then ( nohup {cmd} >/dev/null 2>&1 & ); "
           "else {cmd}; fi")
    w = World(tmp_path, {"0": GOOD, "1": GOOD},
              ladder={"param": "knob", "values": [0, 1], "from_stage": "cts",
                      "submit": tpl, "accept": {"command": ["./check.py"]}})
    code, rep = w.run()
    assert code == L.EXIT_UNVERIFIED
    assert "completion marker" in rep["rungs"][0]["reasons"][0]
    assert "exited -1000" not in rep["rungs"][0]["reasons"][0]


def test_a_higher_rung_cannot_take_a_slot_before_the_lower_one(tmp_path):
    w = World(tmp_path, {"0": GOOD, "1": GOOD})
    spec = L.parse_ladder({"param": "knob", "values": [0, 1], "from_stage": "cts", "jobs": 1})
    lad = L.Ladder(spec, L.CommandBackend(w.cfg, spec), w.cfg.pnr_dir, log=lambda m: None)
    lad.rung_stop = [threading.Event(), threading.Event()]
    lad.started = [threading.Event(), threading.Event()]
    lad.plans = [{}, {}]
    lad.accept_digest = "x"
    lad.runner = L.Runner(w.cfg.pnr_dir, grace=0.2)
    (w.cfg.pnr_dir / "ladder" / "place").mkdir(parents=True)
    t1 = threading.Thread(target=lambda: lad._run_rung_inner(1, "h", True))
    t1.start()
    time.sleep(0.5)
    assert not lad.started[1].is_set()          # rung 1 waits for rung 0 to take its slot
    t0 = threading.Thread(target=lambda: lad._run_rung_inner(0, "h", True))
    t0.start()
    t0.join(60)
    t1.join(60)
    assert lad.started[0].is_set() and lad.started[1].is_set()


def test_relative_command_words_and_libraries_are_relative_to_the_config_directory(tmp_path):
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "check.py").write_text("")
    (cfgdir / "ff.lib").write_text("")
    spec = L.parse_ladder({"param": "p", "values": [1], "accept": {
        "command": ["python3", "check.py"],
        "corners": {"ff": {"libs": ["ff.lib", "/OpenROAD-flow-scripts/x.lib"],
                           "hold_ws": {"min": 0}}}}})
    out = L._resolve_command(spec, cfgdir)
    assert out.accept.command == ("python3", str(cfgdir / "check.py"))
    assert out.accept.corners[0].libs == (str(cfgdir / "ff.lib"), "/OpenROAD-flow-scripts/x.lib")


@pytest.mark.parametrize("tail,expected", [
    ("Traceback (most recent call last):\n  File \"c.py\", line 1\nStopIteration\n", True),
    ("Traceback (most recent call last):\n  File \"c.py\", line 1\nKeyboardInterrupt\n", True),
    ("Traceback (most recent call last):\n  File \"c.py\", line 1\nSystemExit: 1\n", True),
    ("Traceback (most recent call last):\n  File \"c.py\", line 1\nREJECT: slack too small\n", False),
    ("Traceback (most recent call last):\n  File \"c.py\", line 1\nValueError: boom\nmore output\n", False),
])
def test_crash_detection_names_any_exception(tail, expected):
    assert L.ends_in_traceback(tail) is expected


# ---------------------------------------------------------------------------
# Long inline words, refusals that leave a RESULT, recycled group ids
# ---------------------------------------------------------------------------


def test_a_long_or_odd_inline_command_word_is_left_alone(tmp_path):
    spec = L.parse_ladder({"param": "p", "values": [1], "accept": {
        "command": ["bash", "-c", "echo " + "x" * 5000, "line1\nline2"],
        "corners": {"ff": {"libs": ["y" * 5000], "hold_ws": {"min": 0}}}}})
    out = L._resolve_command(spec, tmp_path)                  # must not raise
    assert out.accept.command == spec.accept.command
    assert out.accept.corners[0].libs == spec.accept.corners[0].libs


def test_a_stuck_record_refusal_writes_a_refused_result(tmp_path):
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        w = World(tmp_path, {"0": GOOD})
        base = w.cfg.pnr_dir / "ladder"
        base.mkdir(parents=True)
        (base / "processes.json").write_text(json.dumps([
            {"name": "j", "pid": victim.pid, "start": L._proc_start(victim.pid),
             "host": L._host_id(), "jobname": "", "unkillable": True}]))
        with pytest.raises(L.LadderStuck, match="delete .*processes.json"):
            L.run_flow(w.cfg, log=lambda m: None)
        res = json.loads((base / "RESULT.json").read_text())
        assert res["state"] == "refused" and "survived SIGKILL" in res["message"]
    finally:
        victim.kill()
        victim.wait()


def test_a_recycled_group_id_does_not_block_later_runs(tmp_path):
    """The recorded leader start time differs from the live process that has
    the pid now: not the group that was recorded."""
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        w = World(tmp_path, {"0": GOOD})
        base = w.cfg.pnr_dir / "ladder"
        base.mkdir(parents=True)
        (base / "processes.json").write_text(json.dumps([
            {"name": "j", "pid": victim.pid, "start": "1",       # not its start time
             "host": L._host_id(), "jobname": "", "unkillable": True}]))
        L.run_flow(w.cfg, log=lambda m: None)                    # does not refuse
        assert victim.poll() is None                             # and did not signal it
    finally:
        victim.kill()
        victim.wait()


def test_checks_read_final_over_the_top_level_and_dotted_paths():
    cs = L.Accept().checks
    m = {"setup_ws": -5.0, "hold_ws": 1.0, "drc_errors": 0, "final": {"setup_ws": 3.0}}
    assert L.check_metrics(m, cs) == (L.ACCEPTED, [])
    v, why = L.check_metrics({**m, "final": {"setup_ws": -2.0}, "setup_ws": 5.0}, cs)
    assert v == L.REJECTED and "setup_ws -2 < 0" in why[0]
    dotted = (L.Check("group.value", min=0.0),)
    assert L.check_metrics({"group": {"value": 3.0}}, dotted) == (L.ACCEPTED, [])
    v, why = L.check_metrics({"setup_ws": 1}, dotted)
    assert v == L.UNVERIFIED and "group.value not reported" in why[0]
    assert L.check_metrics({"group": {"value": float("nan")}}, dotted)[0] == L.UNVERIFIED


def test_the_record_format_was_bumped_with_the_judging_rule():
    assert L.FORMAT >= 3


def test_a_variable_in_a_jobs_environment_is_on_the_submit_line(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_ONLY_VAR", raising=False)
    out = tmp_path / "seen.txt"
    r = L.Runner(tmp_path, submit="env -i PATH=\"$PATH\" {cmd}")
    job = L.Job("j", ["bash", "-c", f'echo "$JOB_ONLY_VAR" > {out}'],
                {**os.environ, "JOB_ONLY_VAR": "7"}, tmp_path, tmp_path / "j.log")
    assert r.spawn(job).wait(threading.Event()) == 0
    assert out.read_text().split()[0] == "7"


def test_a_variable_with_a_changed_value_is_on_the_submit_line(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_ONLY_VAR", "old")
    out = tmp_path / "seen.txt"
    r = L.Runner(tmp_path, submit="env -i PATH=\"$PATH\" {cmd}")
    job = L.Job("j", ["bash", "-c", f'echo "$JOB_ONLY_VAR" > {out}'],
                {**os.environ, "JOB_ONLY_VAR": "new"}, tmp_path, tmp_path / "j.log")
    assert r.spawn(job).wait(threading.Event()) == 0
    assert out.read_text().split()[0] == "new"
