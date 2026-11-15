"""A pack's own P&R options (pack_options.py): fixture registry unit tests,
write_settings applied or refused, the drift rules D1 to D5, and the example
registry of examples/techpack-template."""
import json
import os
from pathlib import Path

import pytest

from circt_flow import options, pack_options
from circt_flow.options import OptionError


def _reg(**over):
    r = {"format": 1, "legacy_env_removed_in": options.LEGACY_ENV_REMOVED_IN,
         "namespaces": ["abc", "xyz", "syn", "post", "cts", "hold"],
         "options": [
             {"name": "xyz.tol", "type": "float", "default": 0.5, "unit": "ns",
              "range": [0, 1], "help": "a tolerance", "stage": "cts",
              "consumers": ["command"], "var": "XYZ_TOL", "legacy_env": ["XYZ_TOL"]},
             {"name": "xyz.count", "type": "int", "default": None, "range": [1, 9]},
             {"name": "xyz.mode", "type": "choice", "choices": ["fast", "slow"],
              "default": "fast"},
             {"name": "xyz.tag", "type": "str", "pattern": "[a-z]+[0-9]", "default": None,
              "empty_ok": False},
             {"name": "xyz.lever", "type": "str", "default": None, "empty_ok": True},
             {"name": "xyz.on", "type": "bool", "default": False},
         ]}
    r.update(over)
    return r


def _pack(tmp_path, reg):
    (tmp_path / "pnr").mkdir(exist_ok=True)
    (tmp_path / "pnr" / "opts.json").write_text(json.dumps(reg))
    (tmp_path / "techpack.yaml").write_text(
        "pnr:\n  command: [./pnr/run.py]\n  settings_registry: pnr/opts.json\n")
    return tmp_path


def test_load_maps_types(tmp_path):
    o = pack_options.load(_pack(tmp_path, _reg()))
    assert set(o) == {"xyz.tol", "xyz.count", "xyz.mode", "xyz.tag", "xyz.lever", "xyz.on"}
    t = o["xyz.tol"]
    assert t.range == (0, 1) and t.unit == "ns"
    assert t.legacy_env == ("XYZ_TOL",) and str(t.binds[0]) == "P&R XYZ_TOL"
    assert t.parse("0.25", "x") == 0.25
    with pytest.raises(OptionError):
        t.parse("2", "x")
    with pytest.raises(OptionError):
        o["xyz.mode"].parse("medium", "x")
    with pytest.raises(OptionError):
        o["xyz.tag"].parse("abc", "x")       # fullmatch, not search
    assert o["xyz.tag"].parse("abc1", "x") == "abc1"


def test_check_clean_and_rules(tmp_path):
    assert pack_options.check(_pack(tmp_path, _reg())) == []
    flow_name = next(n for n, o in options.registry().items() if o.type == "int")
    fo = options.registry()[flow_name]
    shared_ok = {"name": flow_name, "type": "int", "shared": True,
                 "default": fo.default, "range": list(fo.range) if fo.range else None}
    r = _reg()
    r["options"].append(shared_ok)
    assert pack_options.check(_pack(tmp_path, r)) == []          # D1 satisfied
    r = _reg()
    r["options"].append({"name": "xyz.nothere", "type": "int", "shared": True})
    assert any(p.startswith("D1") for p in pack_options.check(_pack(tmp_path, r)))
    r = _reg()
    r["options"].append({"name": flow_name, "type": "int"})
    probs = pack_options.check(_pack(tmp_path, r))
    assert any(p.startswith("D2") for p in probs)
    r = _reg()
    leg = next(iter(options.legacy_map()))
    r["options"][0]["legacy_env"] = [leg]
    assert any(p.startswith("D3") for p in pack_options.check(_pack(tmp_path, r)))
    assert any(p.startswith("D4") for p in
               pack_options.check(_pack(tmp_path, _reg(legacy_env_removed_in=99))))
    r = _reg(namespaces=["abc"])
    assert any(p.startswith("D5") for p in pack_options.check(_pack(tmp_path, r)))


def test_d1_compares_meaning_d5_reserved_namespace(tmp_path):
    """D1 compares type, choices, range and default of a shared
    option where circt-abc types it; D5 refuses an unshared pack option in
    circt-abc's syn. namespace even when the pack lists it."""
    reg = options.registry()
    name, fo = next((n, o) for n, o in reg.items() if o.type == "choice" and o.choices
                    and n.split(".")[0] in ("syn", "post", "cts"))
    ok = {"name": name, "type": "choice", "choices": list(fo.choices), "default": fo.default,
          "shared": True}
    r = _reg()
    r["options"].append(dict(ok))
    assert pack_options.check(_pack(tmp_path, r)) == []
    for bad, what in (({"type": "str", "choices": []}, "type str"),
                      ({"choices": list(fo.choices)[:1]}, "choices"),
                      ({"default": next(c for c in fo.choices if c != fo.default)}, "default")):
        r = _reg()
        r["options"].append({**ok, **bad})
        probs = pack_options.check(_pack(tmp_path, r))
        assert any(p.startswith(f"D1: shared option {name}:") and what in p for p in probs), probs
    # a circt-abc "object" option (validated by its section parser): the name is enough
    oname = next(n for n, o in reg.items() if o.type == "object" and o.settable)
    r = _reg()
    r["options"].append({"name": oname, "type": "int", "shared": True})
    assert pack_options.check(_pack(tmp_path, r)) == []
    r = _reg()
    r["options"].append({"name": "syn.pack_knob", "type": "int"})
    assert any(p.startswith("D5: syn.pack_knob") and "circt-abc's" in p
               for p in pack_options.check(_pack(tmp_path, r)))


def test_write_settings_refuses_non_finite(tmp_path):
    """The settings file is strict JSON (no Infinity / NaN)."""
    pk = _pack(tmp_path, _reg(options=[{"name": "xyz.f", "type": "float", "default": 1.0}]))
    for v in ("inf", "-inf", "nan", float("inf")):
        with pytest.raises(OptionError, match="finite|not a number"):
            pack_options.write_settings(pk, {"xyz.f": v}, tmp_path / "u.json")
        assert not (tmp_path / "u.json").exists()
    pack_options.write_settings(pk, {"xyz.f": "1e3"}, tmp_path / "u.json")
    assert json.loads((tmp_path / "u.json").read_text()) == {"options": {"xyz.f": 1000.0}}


def test_missing_or_bad_registry(tmp_path):
    (tmp_path / "techpack.yaml").write_text("pnr:\n  command: [x]\n")
    assert "declares no P&R option registry" in pack_options.check(tmp_path)[0]
    (tmp_path / "techpack.yaml").write_text("pnr:\n  command: [x]\n  settings_registry: none.json\n")
    assert "missing" in pack_options.check(tmp_path)[0]
    with pytest.raises(OptionError):
        pack_options.load(_pack(tmp_path, _reg(format=2)))


def test_write_settings_applied(tmp_path):
    pk = _pack(tmp_path, _reg())
    out = tmp_path / "s.json"
    pack_options.write_settings(pk, {"xyz.tol": "0.25", "xyz.on": "true",
                                     "xyz.lever": "", "xyz.count": 3}, out)
    assert json.loads(out.read_text()) == {"options": {
        "xyz.count": 3, "xyz.lever": "", "xyz.on": True, "xyz.tol": 0.25}}


@pytest.mark.parametrize("vals", [
    {"xyz.nope": 1}, {"xyz.tol": 7}, {"xyz.count": 1.5}, {"xyz.mode": "x"},
    {"xyz.tag": ""}, {"xyz.tag": "AB"}, {"xyz.on": "maybe"}, {"xyz.tol": None},
    {"xyz.tol": 0.1, "xyz.bad": 1}])
def test_write_settings_refused(tmp_path, vals):
    pk = _pack(tmp_path, _reg())
    out = tmp_path / "s.json"
    with pytest.raises(OptionError):
        pack_options.write_settings(pk, vals, out)
    assert not out.exists()


def test_cli_lists_pack_options(tmp_path, capsys):
    pk = _pack(tmp_path, _reg())
    assert options.main(["--pack", str(pk), "xyz."]) == 0
    assert "xyz.tol" in capsys.readouterr().out
    assert options.main(["--pack", str(pk), "--markdown"]) == 0
    md = capsys.readouterr().out
    assert "| `xyz.tol`" in md and "| `syn." in md
    # the flow's own reference never names a pack's options
    assert "xyz." not in options.render_markdown()


# ---- the example registry of the template pack

TEMPLATE = Path(__file__).resolve().parents[3] / "examples" / "techpack-template"
needs_template = pytest.mark.skipif(not (TEMPLATE / "techpack.yaml").is_file(),
                                    reason="no examples/ in this tree")


@needs_template
def test_template_registry_is_clean_and_names_its_recipe_dir():
    o = pack_options.load(TEMPLATE)
    assert {"report.area_digits", "report.note", "pnr.recipe_dir", "cts.gate_sizing"} <= set(o)
    assert pack_options.check(TEMPLATE) == []
    raw = pack_options.read_registry(TEMPLATE)
    assert (TEMPLATE / raw["recipe_dir"]).is_dir() and raw["recipe_dir_option"] in o


@needs_template
def test_template_command_reads_the_settings_file(tmp_path):
    """examples/techpack-template/pnr/run.py applies a --settings file that
    write_settings wrote (and refuses an unknown option)."""
    import subprocess
    import sys
    net = tmp_path / "n.v"
    net.write_text("module top (a, y);\n  input a;\n  output y;\n  TINV u0 (.A(a), .Y(y));\n"
                   "  TINV u1 (.A(a), .Y());\nendmodule\n")
    sf = pack_options.write_settings(TEMPLATE, {"report.area_digits": 1, "report.label": "t1",
                                                "report.cell_histogram": True,
                                                "report.note": "standin.txt"}, tmp_path / "s.json")
    run = [sys.executable, str(TEMPLATE / "pnr" / "run.py"), "--netlist", str(net), "--sdc",
           str(tmp_path / "c.sdc"), "--top", "top", "--out", str(tmp_path), "--pack", str(TEMPLATE)]
    r = subprocess.run(run + ["--settings", str(sf)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    m = json.loads((tmp_path / "pnr_metrics.json").read_text())
    rep = json.loads((tmp_path / "report.json").read_text())
    assert m["instance_count"] == 2 and set(m) == {"instance_count", "stdcell_area"}
    assert rep["label"] == "t1" and rep["note"].startswith("template")
    assert json.loads((tmp_path / "cell_histogram.json").read_text()) == {"TINV": 2}
    (tmp_path / "bad.json").write_text(json.dumps({"options": {"report.nope": 1}}))
    r = subprocess.run(run + ["--settings", str(tmp_path / "bad.json")], capture_output=True, text=True)
    assert r.returncode != 0 and "report.nope" in r.stderr


def test_group_field_lists_and_filters(tmp_path, capsys):
    r = _reg()
    r["options"][0]["group"] = "timing"
    pk = _pack(tmp_path, r)
    assert pack_options.groups(pk) == {"xyz.tol": "timing"}
    assert "group timing" in pack_options.load(pk)["xyz.tol"].note
    assert options.main(["--pack", str(pk), "--group", "timing"]) == 0
    out = capsys.readouterr().out.split("# pack P&R options")[1]
    assert "xyz.tol" in out and "[timing]" in out and "xyz.mode" not in out


def test_requires_is_listed_and_checked(tmp_path):
    pk = _pack(tmp_path, _reg(requires={"xyz.count": ["xyz.on", "1"]}))
    assert pack_options.check(pk) == []
    assert "needs xyz.on = '1'" in pack_options.load(pk)["xyz.count"].note
    # the command enforces it: circt-abc writes the value as given
    pack_options.write_settings(pk, {"xyz.count": 2}, tmp_path / "s.json")
    pk = _pack(tmp_path, _reg(requires={"xyz.count": ["xyz.gone", "1"]}))
    assert any("xyz.gone is not an option" in p for p in pack_options.check(pk))


def test_settings_defaults_of_a_profile(tmp_path):
    """pnr.settings_defaults: validated when the pack loads, handed to the
    command as a settings file on every run (no session needed)."""
    from types import SimpleNamespace
    from circt_flow import profile, stages
    pk = _pack(tmp_path, _reg())
    (pk / "lib").mkdir()
    (pk / "lib" / "a.lib").write_text("library(a) { }\n")
    base = ("schema: 1\nname: t\nliberties: [lib/a.lib]\ntime_unit: ns\n"
            "icg: {cell: I, clk: CK, en: E, test_en: SE, out: ECK}\ndont_use: []\n"
            "tie: {hi: 'H:H', lo: 'L:L'}\npublic: true\n"
            "pnr:\n  command: [./pnr/run.py]\n  settings_registry: pnr/opts.json\n")
    (pk / "techpack.yaml").write_text(base + "  settings_defaults: {xyz.mode: slow, xyz.count: 4}\n")
    prof = profile.load(pk / "techpack.yaml")
    assert prof.pnr["settings_defaults"] == {"xyz.mode": "slow", "xyz.count": 4}
    (pk / "techpack.yaml").write_text(base + "  settings_defaults: {xyz.mode: medium}\n")
    with pytest.raises(ValueError, match="settings_defaults"):
        profile.load(pk / "techpack.yaml")
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    c = SimpleNamespace(
        openroad_netlist=net, pnr_dir=tmp_path / "pnr",
        design=SimpleNamespace(pnr={"clock_port": "c"},
                               pack_pnr={"command": ["true"], "settings_registry": "pnr/opts.json",
                                         "settings_defaults": {"xyz.mode": "slow"}},
                               tie=None, top_module="top", clock_period_ps=1000, pack_root=pk,
                               manifest=tmp_path / "m.yaml", config_dir=tmp_path))
    cmd, _ = stages.pack_command(c, tmp_path / "pnr")
    i = cmd.index("--settings")
    assert json.loads(Path(cmd[i + 1]).read_text()) == {"options": {"xyz.mode": "slow"}}


def test_group_needs_pack_markdown_and_unknown_group(tmp_path, capsys):
    r = _reg()
    r["options"][0]["group"] = "timing"
    r["options"][2]["group"] = "effort"
    pk = _pack(tmp_path, r)
    with pytest.raises(SystemExit):
        options.main(["--group", "timing"])                       # no --pack
    assert "needs --pack" in capsys.readouterr().err
    assert options.main(["--pack", str(pk), "--group", "timing", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert "`xyz.tol`" in out and "`xyz.mode`" not in out and "group `timing`" in out
    assert options.main(["--pack", str(pk), "--group", "nope"]) == 2
    err = capsys.readouterr().err
    assert "nope" in err and "effort, timing" in err


def test_malformed_requires_is_reported(tmp_path):
    pk = _pack(tmp_path, _reg(requires={"xyz.count": ["xyz.on"], "xyz.tag": "xyz.on"}))
    probs = pack_options.check(pk)
    assert sum("requires: xyz.count" in p for p in probs) == 1
    assert sum("requires: xyz.tag" in p for p in probs) == 1
    pk = _pack(tmp_path, _reg(requires=["xyz.count"]))
    assert any("requires: not a mapping" in p for p in pack_options.check(pk))


def _recipe_reg(**over):
    r = _reg(recipe_dir="pnr/notes", recipe_dir_option="xyz.dir", **over)
    r["options"] += [{"name": "xyz.dir", "type": "path", "default": None, "legacy_env": ["XYZ_DIR"]},
                     {"name": "xyz.note", "type": "str", "default": None, "consumers": ["recipe"]}]
    return r


def test_handover_defaults_given_and_recipe_dir(tmp_path):
    """One builder of the handed-over values: the profile's defaults (paths
    against the pack), the given values over them, and the recipe directory
    for an option that reads it; each with its source."""
    pk = _pack(tmp_path, _recipe_reg())
    (pk / "pnr" / "notes").mkdir()
    pnr = {"command": ["./pnr/run.py"], "settings_registry": "pnr/opts.json",
           "settings_defaults": {"xyz.mode": "slow", "xyz.note": "a.txt"}}
    v, s = pack_options.handover(pk, pnr, {"xyz.mode": "fast"}, environ={})
    rd = str((pk / "pnr" / "notes").resolve())
    assert v == {"xyz.dir": rd, "xyz.mode": "fast", "xyz.note": "a.txt"}
    assert s["xyz.mode"] == "set_pack_option"
    assert s["xyz.note"] == pack_options.DEFAULTS_SOURCE
    assert s["xyz.dir"].startswith(pack_options.RECIPE_DIR_SOURCE) and "xyz.note" in s["xyz.dir"]
    v, _ = pack_options.handover(pk, pnr, {}, environ={"XYZ_DIR": "/elsewhere"})
    assert "xyz.dir" not in v                         # its legacy variable: the pack's business
    pnr2 = dict(pnr, settings_defaults={"xyz.dir": "pnr"})
    v, s = pack_options.handover(pk, pnr2, {"xyz.note": "b"}, environ={})
    assert v["xyz.dir"] == str((pk / "pnr").resolve()) and s["xyz.dir"] == pack_options.DEFAULTS_SOURCE
    r = _recipe_reg()
    del r["recipe_dir"]
    (tmp_path / "p2").mkdir()
    pk2 = _pack(tmp_path / "p2", r)
    with pytest.raises(pack_options.PackOptionsError, match="set s.set_pack_option\\('xyz.dir'"):
        pack_options.handover(pk2, pnr, {}, environ={})
    with pytest.raises(OptionError, match="unknown pack option"):
        pack_options.handover(pk, pnr, {"xyz.nope": 1}, environ={})
    assert pack_options.handover(pk, {"command": ["x"]}, {}) == ({}, {})


def test_stages_and_a_session_write_the_same_file(tmp_path, monkeypatch):
    """stages.pack_command writes handover() for a run with no session
    (the profile's defaults, the recipe directory) and for a session's
    values (cfg.design.pack_settings) alike, to <out>/pack_settings.json."""
    from types import SimpleNamespace
    from circt_flow import stages
    monkeypatch.delenv("XYZ_DIR", raising=False)
    pk = _pack(tmp_path, _recipe_reg())
    (pk / "pnr" / "notes").mkdir()
    net = tmp_path / "top_openroad.v"
    net.write_text("module top; endmodule\n")
    pnr = {"command": ["true"], "settings_registry": "pnr/opts.json",
           "settings_defaults": {"xyz.note": "a.txt"}}

    def cfg(given):
        return SimpleNamespace(
            openroad_netlist=net, pnr_dir=tmp_path / "pnr",
            design=SimpleNamespace(pnr={"clock_port": "c"}, pack_pnr=pnr, pack_settings=given,
                                   tie=None, top_module="top", clock_period_ps=1000, pack_root=pk,
                                   manifest=tmp_path / "m.yaml", config_dir=tmp_path))
    for given, out in (({}, "a"), ({"xyz.mode": "slow"}, "b")):
        cmd, _ = stages.pack_command(cfg(given), tmp_path / out)
        f = Path(cmd[cmd.index("--settings") + 1])
        assert f == (tmp_path / out / "pack_settings.json").resolve()
        want = pack_options.handover(pk, pnr, given)[0]
        assert json.loads(f.read_text()) == {"options": want}
        assert want["xyz.dir"] == str((pk / "pnr" / "notes").resolve())
    bare = dict(pnr)
    del bare["settings_defaults"]
    c = cfg({})
    c.design.pack_pnr = bare
    assert "--settings" not in stages.pack_command(c, tmp_path / "c")[0]   # nothing to hand over
