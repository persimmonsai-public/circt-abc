"""The editable pipeline and pass hooks through the session API: edits and hooks applied by s.run() / s.<stage>(), the settings
record ("pipeline_edits", "hooks"), replay from settings.json, refusal on a
stage already run. Skipped until session.py is wired to pipeline_edit."""
import inspect
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from conftest import write_lib
from circt_flow import session as session_mod, stages
from circt_flow.pipeline_edit import PipelineError

pytestmark = pytest.mark.skipif(
    "pipeline_edit" not in inspect.getsource(session_mod) or not hasattr(session_mod.Session,
                                                                         "run_stage"),
    reason="session.py not wired to pipeline_edit")

NETLIST = ("module top (a, y);\n  input wire a;\n  output wire y;\n"
           "  TSTINV_X1 u0 (.A(a), .Y(y));\nendmodule\n")


def fake_plain(log):
    """stages._run_plain without tools: each command writes its -o / stdout."""
    def run(cmd, stdout=None, env=None):
        cmd = [str(c) for c in cmd]
        log.append(cmd + ([">", str(stdout)] if stdout is not None else []))
        stages.EXECUTED.append(log[-1])
        if stdout is not None:
            Path(stdout).write_text(NETLIST)
        for i, a in enumerate(cmd):
            if a == "-o" and cmd[i + 1] != "/dev/null":
                Path(cmd[i + 1]).write_text("// " + " ".join(cmd[1:-3]) + "\nhw.module @top() {}\n")
            if a.startswith("--write="):
                Path(a.split("=", 1)[1]).write_text(NETLIST)
    return run


@pytest.fixture
def flow(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    a = write_lib(lib / "a.lib", "tst_a", ["TSTINV_X1"])
    core = tmp_path / "core_in.mlir"
    core.write_text("hw.module @top() {}\n")
    f = tmp_path / "flow.yaml"
    f.write_text(yaml.safe_dump({"design": {"top_module": "top", "clock_period_ps": 1000,
                                            "core_mlir": str(core)},
                                 "pdk": {"synth_lib_tt": [str(a)]}}))
    log = []
    monkeypatch.setattr(stages, "_run_plain", fake_plain(log))
    monkeypatch.setattr(stages, "_binary_has_pass", lambda b, p: True)
    monkeypatch.setattr("circt_flow.config.FlowConfig.validate_tools", lambda self: None)
    monkeypatch.setenv("SV2V", "/BIN/sv2v")
    for k in list(os.environ):
        if k.startswith(("CIRCT_FLOW_", "CIRCT_VT_")):
            monkeypatch.delenv(k)
    stages.EXECUTED.clear()
    return f, log


KW = dict(circt_abc_opt="/BIN/circt-abc-opt", circt_opt="/BIN/circt-opt", abc_bin="/BIN/abc")


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def _opt_cmds(log):
    return [c for c in log if c[0].startswith("/BIN/circt")]


def _hook(s, ir):
    _hook.seen.append(Path(ir).read_text())


_hook.seen = []


def test_session_run_with_and_without_edits(flow, tmp_path):
    f, log = flow
    quiet(session_mod.Session(f, tmp_path / "a", **KW).run)
    plain = [c for c in _opt_cmds(log)]
    log.clear()
    s = session_mod.Session(f, tmp_path / "b", **KW)
    assert "register-mapper" in s.report_passes()
    s.pipeline.remove("map_datapath", "register-mapper")
    s.pipeline.insert("syn_map", "cse", after="map-shell-logic")
    quiet(s.run)
    edited = _opt_cmds(log)
    norm = lambda cs, d: json.dumps(cs).replace(str(tmp_path / d), "<O>")  # noqa: E731
    assert norm(edited, "b") != norm(plain, "a")
    assert not any(a.startswith("--register-mapper") for c in edited for a in c)
    merge = next(c for c in edited if any(a.startswith("--abc-bridge") for a in c))
    assert merge[merge.index(next(a for a in merge if a.startswith("--map-shell-logic"))) + 1] \
        == "--cse"
    rec = json.loads((tmp_path / "b" / "settings.json").read_text())
    assert rec["pipeline_edits"] == s.pipeline.edits and "hooks" not in rec
    assert "pipeline_edits" not in json.loads((tmp_path / "a" / "settings.json").read_text())
    # replay from the record: the same commands
    log.clear()
    s2 = session_mod.Session.from_settings(f, tmp_path / "c", tmp_path / "b" / "settings.json",
                                           **KW)
    assert s2.pipeline.edits == s.pipeline.edits
    quiet(s2.run)
    assert norm(_opt_cmds(log), "c") == norm(edited, "b")


def test_session_pass_hook_and_record(flow, tmp_path):
    f, log = flow
    s = session_mod.Session(f, tmp_path / "o", **KW)
    _hook.seen.clear()
    s.hooks.post_pass("map_datapath", "register-mapper", _hook)
    quiet(s.elaborate)
    quiet(s.map_datapath)
    md = [c for c in _opt_cmds(log) if any(a.startswith("--register-mapper") or
                                           a.startswith("--structural-mapper") for a in c)]
    assert len(md) == 2 and md[0][-4].startswith("--register-mapper")
    assert len(_hook.seen) == 1 and "--register-mapper" in _hook.seen[0]
    rec = json.loads((tmp_path / "o" / "settings.json").read_text())
    hk = rec["hooks"]
    assert hk["reproducible_from_settings"] is False
    assert hk["registered"][0]["kind"] == "post_pass" and hk["registered"][0]["name"] == "_hook"
    assert any(c[0] == "(post_pass)" for c in rec["pipeline"])
    # map_datapath has run: an edit there could no longer take effect
    with pytest.raises(PipelineError, match="already run"):
        s.pipeline.insert("map_datapath", "cse")
    s.pipeline.insert("syn_map", "cse")                  # a later stage still can


def test_session_edit_refused_before_any_tool(flow, tmp_path):
    f, log = flow
    s = session_mod.Session(f, tmp_path / "o", **KW)
    s.pipeline.remove("syn_map", "prune-unused-residual-outputs")
    s.db["syn.cleanup.prune_residual_outputs"] = False   # the edit's pass is gone now
    log.clear()
    with pytest.raises(PipelineError, match="prune-unused-residual-outputs"):
        quiet(s.run)
    assert _opt_cmds(log) == []
