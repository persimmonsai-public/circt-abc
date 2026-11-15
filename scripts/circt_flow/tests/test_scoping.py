"""Object queries (query.py,
design/objects.py) and scoped settings (scoping.py, option syn.scoped).

Small designs: tests/data/scoped/top_core_deagg.mlir (a core IR: registers with
and without a hold mux, a register name in two modules, adders / a
multiplier / a shifter, a module instantiated twice) and top_struct.mlir
(register cells and clock gates). The pass-level tests run circt-abc-opt on
the hermetic fixtures of mapper/test/ when it is built (skipped otherwise)."""
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from circt_flow import options, provenance_settings, query, scoping
from circt_flow.config import FlowConfig
from circt_flow.session import Session

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "scoped"
ROOT = HERE.parents[2]
TESTS = ROOT / "mapper" / "test"
sys.path.insert(0, str(HERE))
import golden_cmdlines  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(options.LEGACY_PREFIXES):
            monkeypatch.delenv(k)
    scoping._WARNED.clear()
    options._WARNED.clear()


def _db():
    return query.DesignDB(DATA / "top_core_deagg.mlir", DATA / "top_struct.mlir", None, "top")


def _flow(tmp_path, extra=None, core=True):
    doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
           "pdk": {"synth_lib_tt": ["/LIB/a.lib"]}, **(extra or {})}
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "f.yaml").write_text(yaml.safe_dump(doc))
    out = tmp_path / "o"
    out.mkdir(exist_ok=True)
    if core:
        shutil.copy(DATA / "top_core_deagg.mlir", out / "top_core_deagg.mlir")
    return tmp_path / "f.yaml", out


_KW = dict(circt_abc_opt="/BIN/opt", circt_opt="/BIN/copt", abc_bin="/BIN/abc")


def _session(tmp_path, **kw):
    f, out = _flow(tmp_path)
    return Session(f, out, **_KW, **kw)


def _cmds(cfg):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return golden_cmdlines.stage_commands(cfg)


def _struct_cmd(cfg):
    return next(c for c in _cmds(cfg) if c[-2:] == ["-o", str(cfg.struct_mlir)])


# ---------------------------------------------------------------- queries

def test_registers_and_their_attributes():
    d = _db()
    regs = d.flops()
    assert regs.ids() == ["leaf/h", "leaf/m", "leaf/q", "other/q"]        # stable order
    q = d.flops("leaf/q").one()
    assert (q.width, q.enable, q.hold_mux, q.reset, q.reset_signal, q.reset_value) == \
        (16, "en", True, "async", "!rst_ni", "0")
    assert q.instances == ("u0/q", "u1/q")                                 # per definition
    assert d.flops("h").one().enable == "!en"                              # holds on true
    assert not d.flops("m").one().hold_mux
    assert d.flops("q").ids() == ["leaf/q", "other/q"]                     # a name, any module
    assert d.flops("u1/*").ids() == ["leaf/h", "leaf/m", "leaf/q"]         # hierarchical path
    assert d.flops(width=(9, None)).ids() == ["leaf/q"]
    assert d.flops(hold_mux=True, module="other").ids() == ["other/q"]
    # struct IR: register cells, multibit grouping, gates
    assert q.gates == ("clock_gate_0", "clock_gate_1")
    assert q.multibit == (("q_cg0_mb0_3", 4),)
    assert [(c.part, c.lo, c.hi) for c in q.cells] == [(0, 0, 3), (1, 0, 0)]


def test_empty_query_is_distinguishable_and_scoped_on_it_is_refused(tmp_path):
    d = _db()
    e = d.flops("nothing_*")
    assert e.empty and not e and len(e) == 0 and "empty" in repr(e)
    with pytest.raises(query.QueryError, match="empty"):
        e.require()
    s = _session(tmp_path)
    with pytest.raises(query.QueryError, match="empty"):
        query.gate_register(s, e)
    assert query.gate_register(s, e, allow_empty=True) is None
    assert query.scoped_of(s) == ()


def test_operators_widths_windows_and_set_operations():
    d = _db()
    assert d.adders().ids() == ["leaf/adder0", "leaf/adder1", "leaf/adder2", "leaf/adder3",
                                "leaf/adder4", "other/adder0"]
    a12 = d.adders(width=12)
    assert a12.ids() == ["leaf/adder1", "leaf/adder2", "other/adder0"]
    assert a12.windows == ((12, 12),)
    assert d.adders(op="sub").ids() == ["leaf/adder2"] and d.adders(op="sub").windows is None
    assert d.multipliers().one().width == 8 and d.shifters().one().op == "shl"
    assert d.adders(const_operands=1).one().id == "leaf/adder4"
    u = d.adders(width=(1, 8)) | d.adders(width=(20, None))
    assert u.windows == ((1, 8), (20, None))
    assert (d.adders() - d.adders(width=12)).windows == ((1, 11), (13, None))
    assert (d.adders(width=(3, 14)) & d.adders(width=(12, 30))).windows == ((12, 14),)
    assert d.adders(width=(3, 14)).filter(width=(10, None)).windows == ((10, 14),)
    with pytest.raises(query.QueryError, match="both operands"):
        d.adders() | d.flops()
    assert d.adders().filter("leaf/*").ids()[:1] == ["leaf/adder0"]


def test_bits_and_split():
    d = _db()
    sl = d.flops("leaf/q").bits(1, 9).split(3)
    assert [x.name for x in sl] == ["q[3:1]", "q[6:4]", "q[9:7]"]
    assert [x.width for x in d.flops("leaf/q").split(5)] == [5, 5, 5, 1]
    with pytest.raises(query.QueryError, match="outside"):
        d.flops("q").bits(2, 30)              # other/q has 8 bits


def test_clock_gates():
    g = _db().icgs()
    assert g.ids() == ["leaf/clock_gate_0", "leaf/clock_gate_1"]
    g0 = g[0]
    assert (g0.origin, g0.enable, g0.fanout, g0.registers, g0.bits) == \
        ("inferred", "en", 2, ("h", "q"), 5)
    assert _db().icgs(registers="h").ids() == ["leaf/clock_gate_0"]
    assert _db().icgs(fanout=(2, None)).ids() == ["leaf/clock_gate_0"]


def test_mapped_cells():
    d = query.DesignDB(None, None, DATA / "top_openroad.v", "top")
    assert d.cells().ids() == ["top/q_bit0", "top/u_inv"]
    assert d.cells(cell="DFF*").one().name == "q_bit0"


def test_query_before_its_stage_is_refused(tmp_path):
    d = query.DesignDB(tmp_path / "none_core.mlir", tmp_path / "none_struct.mlir", None, "top")
    with pytest.raises(query.StageNotRun, match="elaborate"):
        d.flops()
    with pytest.raises(query.StageNotRun, match="map_datapath"):
        d.icgs()
    calls = []
    d2 = query.DesignDB(DATA / "top_core_deagg.mlir", None, None, "top",
                        require=lambda st: calls.append(st))
    d2.adders()
    assert calls == ["elaborate"]


# ---------------------------------------------------------------- scoped commands

def test_gate_register_records_targets_and_qualifies_a_shared_name(tmp_path):
    s = _session(tmp_path)
    d = query.design_of(s)
    for sl in d.flops("leaf/q").bits(1, 9).split(3):
        query.gate_register(s, sl)
    query.gate_register(s, d.flops("h").bits(3, 6), detect="xor")
    recs = s.get("syn.scoped")
    assert [r["targets"] for r in recs] == [["leaf/q@1-3"], ["leaf/q@4-6"], ["leaf/q@7-9"], ["h@3-6"]]
    assert recs[3]["values"] == {"detect": "xor"}
    assert recs[0]["set_at"].startswith("script:test_scoping.py:")
    with pytest.raises(query.QueryError, match="hold mux"):
        query.gate_register(s, d.flops("m"))
    with pytest.raises(options.OptionError, match="not one of"):
        query.gate_register(s, d.flops("h"), detect="ones")


def test_map_adders_targets_are_width_windows(tmp_path):
    s = _session(tmp_path)
    d = query.design_of(s)
    x = query.map_adders(s, d.adders(width=(11, 40)), arch="HAN-CARLSON", boundary=1)
    assert x.targets == ("width:11-40",)
    assert x.values == {"form": "prefix", "arch": "HAN-CARLSON", "isolate_inputs": False,
                        "boundary": 1}
    y = query.map_adders(s, d.adders(width=(9, None)), form="cells", increments=False)
    assert y.targets == ("width:9-",) and y.values == {"form": "cells", "increments": False}
    # a filtered selection that is whole widths: one window per run of widths
    z = query.map_adders(s, d.adders(width=(11, 40)).where(lambda o: True))
    assert z.targets == ("width:12-12", "width:20-20")      # exactly the selected widths
    # not expressible: width 12 also has adders outside the selection
    with pytest.raises(query.QueryError, match="selects by width only"):
        query.map_adders(s, d.adders(op="sub"))
    with pytest.raises(options.OptionError, match="not an argument of form=cells"):
        query.map_adders(s, d.adders(width=12), form="cells", arch="HAN-CARLSON")
    with pytest.raises(query.QueryError, match="acts on adder"):
        query.map_adders(s, d.multipliers())
    assert len(s.get("syn.scoped")) == 3


def test_materialise_and_record_round_trip():
    x = scoping.make("map_adders", ["width:70-80"], {"arch": "HAN-CARLSON"}, "script:r.py:3")
    m = query.materialise(None, [x])
    assert m == {"syn.scoped": [x.to_record()]}
    assert scoping.from_record(m["syn.scoped"][0]) == x
    assert query.materialise(None, []) == {}


@pytest.mark.parametrize("value, pat", [
    ([{"command": "gate_regsiter", "targets": ["q@0-1"]}], "did you mean gate_register"),
    ([{"command": "gate_register", "targets": ["q@3-1"]}], "lo <= hi"),
    ([{"command": "gate_register", "targets": []}], "no targets"),
    ([{"command": "map_adders", "targets": ["width:0-4"]}], "1 <= lo"),
    ([{"command": "map_adders", "targets": ["width:4-8"], "values": {"boundary": 2}}], "outside"),
    ([{"command": "map_adders", "targets": ["width:4-8"], "kind": "register_bits"}], "acts on adders"),
    ([{"command": "map_adders", "targets": ["width:4-8"], "junk": 1}], "unknown key"),
    ("!!bogus!!", "syn.scoped"),
])
def test_bad_scoped_values_are_refused_at_load(tmp_path, value, pat):
    f, out = _flow(tmp_path, {"options": {"syn.scoped": value}})
    with pytest.raises(options.OptionError, match=pat):
        FlowConfig.load(f, out, **_KW)


# ---------------------------------------------------------------- applied or refused

def _cfg(tmp_path, settings=None, core=True, env=None, strict=False):
    f, out = _flow(tmp_path, core=core)
    return FlowConfig.load(f, out, settings=settings, env=env or {}, strict=strict, **_KW)


def test_precheck_refuses_targets_that_match_nothing(tmp_path):
    bad = [("gate_register", "zz@0-1", "matched no register"),
           ("gate_register", "other/q@4-9", "8 bits wide"),
           ("gate_register", "m@0-1", "not a plain hold mux"),
           ("map_adders", "width:50-58", "matched no adder")]
    for i, (cmd, t, why) in enumerate(bad):
        cfg = _cfg(tmp_path / str(i), {"syn.scoped": [{"command": cmd, "targets": [t]}]})
        with pytest.raises(options.OptionError, match=why):
            _cmds(cfg)
        a = scoping.record_extra(cfg)["scoped_applied"]
        assert a[0]["status"] == "refused" and why in a[0]["detail"]
    cfg = _cfg(tmp_path / "ov", {"syn.scoped": [
        {"command": "gate_register", "targets": ["leaf/q@1-4", "leaf/q@4-9"]}]})
    with pytest.raises(options.OptionError, match="overlap"):
        _cmds(cfg)


def test_without_core_ir_the_targets_are_unchecked_not_refused(tmp_path):
    cfg = _cfg(tmp_path, {"syn.scoped": [{"command": "gate_register", "targets": ["zz@0-1"]}]},
               core=False)
    assert "--data-gate-enables=slices=zz@0-1@zero" in _struct_cmd(cfg)
    assert scoping.record_extra(cfg)["scoped_applied"][0]["status"] == "unchecked"


def _plan(targets_forms, S=None):
    ts = []
    for cmd, t, v in targets_forms:
        x = scoping.make(cmd, [t], v)
        ts.append(scoping.Target("syn.scoped[0]", x, t,
                                 "data_gate" if cmd == "gate_register" else x.values["form"]))
    return scoping.Plan(ts, lib="L", key="k"), S or NS(values={})


def test_postcheck_reads_the_pass_report():
    p, S = _plan([("gate_register", "q@0-3", {}), ("gate_register", "q@12-15", {})])
    rep = ("[data-gate-enables] dg/q: 1 slice(s) rewritten\n"
           "[data-gate-enables] slice q@12-15 found but skipped (reason above)\n"
           "[data-gate-enables] 1 register(s) rewritten\n")
    with pytest.raises(options.OptionError, match="q@12-15: the pass reports: found but skipped"):
        scoping.postcheck(p, rep, S)
    assert [t.status for t in p.targets] == ["applied", "refused"]
    # every enabled instance prints one line: the k-th line is the k-th instance
    p, S = _plan([("map_adders", "width:3-14", {}), ("map_adders", "width:70-90", {})])
    with pytest.raises(options.OptionError, match="width:70-90: its pass instance built nothing"):
        scoping.postcheck(p, "[map-prefix-adders] 2 add(s) built as X prefix networks\n"
                             "[map-prefix-adders] 0 add(s) built as X prefix networks\n", S)
    assert [t.status for t in p.targets] == ["applied", "refused"]
    p, S = _plan([("map_adders", "width:3-14", {}), ("map_adders", "width:70-90", {})])
    scoping.postcheck(p, "[map-prefix-adders] 2 add(s) built as X\n"
                         "[map-prefix-adders] 1 add(s) built as X\n", S)
    assert [t.detail for t in p.targets] == ["pass report: 2 add(s)", "pass report: 1 add(s)"]
    # a missing line (a binary that stays silent at 0): not attributable, refused
    p, S = _plan([("map_adders", "width:3-14", {}), ("map_adders", "width:70-90", {})])
    with pytest.raises(options.OptionError, match="1 line.s. for 2 map-prefix-adders instance"):
        scoping.postcheck(p, "[map-prefix-adders] 2 add(s) built as X\n", S)
    p, S = _plan([("map_adders", "width:3-14", {"form": "cells"})])
    with pytest.raises(options.OptionError, match="0 line.s. for 1 map-adder-chains"):
        scoping.postcheck(p, "", S)


def test_postcheck_attributes_per_instance_with_a_global_instance():
    """a global instance before the scoped one takes its adders;
    the scoped instance reports 0 and is refused (was: recorded applied)."""
    S = NS(values={"syn.adders.prefix.min_width": 1})
    p, _ = _plan([("map_adders", "width:11-40", {"arch": "HAN-CARLSON"})], S)
    with pytest.raises(options.OptionError, match="width:11-40: its pass instance built nothing"):
        scoping.postcheck(p, "[map-prefix-adders] 4 add(s) built as SKLANSKEY\n"
                             "[map-prefix-adders] 0 add(s) built as HAN-CARLSON\n", S)
    S = NS(values={"syn.adders.cells.min_width": 4})
    p, _ = _plan([("map_adders", "width:3-14", {"form": "cells"})], S)
    scoping.postcheck(p, "[map-adder-chains] lowered 1 comb.add\n[map-adder-chains] lowered 2 comb.add\n",
                      S)
    assert p.targets[0].detail == "pass report: 2 add(s)"


def test_postcheck_refuses_a_target_whose_pass_an_edit_removed_or_moved():
    """the executed commands decide, not the plan."""
    p, S = _plan([("gate_register", "q@0-3", {}), ("map_adders", "width:11-40", {})])
    dg, pf = p.data_gate_args()[0], p.adder_args("prefix")[0]
    rep = "[data-gate-enables] 1 register(s) rewritten\n[map-prefix-adders] 3 add(s) built as X\n"
    with pytest.raises(options.OptionError) as e:
        scoping.postcheck(p, rep, S, ["opt", "--clock-gate-inference=x", pf, "in", "-o", "out"])
    assert "data-gate-enables did not run" in str(e.value)
    p, S = _plan([("gate_register", "q@0-3", {})])
    with pytest.raises(options.OptionError, match="after clock-gate-inference"):
        scoping.postcheck(p, "[data-gate-enables] 1 register(s) rewritten\n", S,
                          ["opt", "--clock-gate-inference=x", p.data_gate_args()[0]])
    p, S = _plan([("map_adders", "width:11-40", {})])
    other = p.adder_args("prefix")[0].replace("min-width=11", "min-width=12")
    with pytest.raises(options.OptionError, match="its pass instance did not run"):
        scoping.postcheck(p, "[map-prefix-adders] 3 add(s) built as X\n", S, ["opt", other])


def test_pipeline_edit_removing_the_scoped_passes_refuses_the_targets(tmp_path):
    """removing data-gate-enables and the
    scoped map-prefix-adders instance with a pipeline edit; the edited
    command that runs is what postcheck reads, so both targets are refused."""
    import test_pipeline_edit as TP
    from circt_flow.pipeline_edit import Pipeline
    listing = {k: dict(v) for k, v in TP.LISTING.items()}
    listing["/BIN/circt-abc-opt"]["data-gate-enables"] = {"slices"}
    cfg = TP._cfg(tmp_path, settings={"syn.scoped": [
        {"command": "gate_register", "targets": ["q@0-3"]},
        {"command": "map_adders", "targets": ["width:11-40"], "values": {"arch": "HAN-CARLSON"}}],
        "syn.adders.prefix.min_width": 41, "syn.adders.prefix.max_width": 0})
    pipe = Pipeline(cfg=cfg, listing=listing.get, has_pass=lambda b, q: True)
    pipe.remove("map_datapath", "data-gate-enables")
    pipe.remove("map_datapath", "map-prefix-adders#2")
    argv = [a for c in pipe.planned(cfg)["map_datapath"].commands for a in c.argv()]
    with redirect_stdout(io.StringIO()):
        p = scoping.plan(cfg)
    rep = "[map-prefix-adders] 5 add(s) built as SKLANSKEY prefix networks\n"
    with pytest.raises(options.OptionError) as e:
        scoping.postcheck(p, rep, cfg.settings, argv)
    assert "q@0-3: data-gate-enables did not run" in str(e.value)
    assert "width:11-40: its pass instance did not run" in str(e.value)
    assert [t.status for t in p.targets] == ["refused", "refused"]


def test_applying_runs_the_postcheck_on_a_real_stage_run(tmp_path):
    """applying() must check the report of the
    command that ran (here a stand-in tool that writes the stage output and
    reports 0 lowered adds for the scoped instance)."""
    from circt_flow import stages
    p, S = _plan([("map_adders", "width:50-60", {"form": "cells"})])
    out = tmp_path / "s.mlir"
    cfg = NS(settings=S, struct_mlir=out, out_dir=tmp_path)
    tool = ("import sys; sys.stderr.write('[map-adder-chains] lowered 0 comb.add op(s) (x)\\n'); "
            "open(sys.argv[-1], 'w').write('ok')")
    with pytest.raises(options.OptionError, match="built nothing"):
        with scoping.applying(p, cfg):
            stages._run_plain([sys.executable, "-c", tool, *p.adder_args("cells"), "-o", str(out)])
    assert p.targets[0].status == "refused"
    # and a run that built something is recorded applied
    p, S = _plan([("map_adders", "width:50-60", {"form": "cells"})])
    cfg = NS(settings=S, struct_mlir=out, out_dir=tmp_path)
    tool = tool.replace("lowered 0", "lowered 2")
    with scoping.applying(p, cfg):
        stages._run_plain([sys.executable, "-c", tool, *p.adder_args("cells"), "-o", str(out)])
    assert p.targets[0].status == "applied"


def test_a_callable_width_filter_drops_the_windows(tmp_path):
    """adders(width=predicate) selects some adders; the command
    must name exactly those widths (or refuse), never all adders."""
    d = _db()
    ev = d.adders(width=lambda w: w > 12)
    assert ev.ids() == ["leaf/adder3"] and ev.windows is None
    assert d.adders().filter(width=lambda w: w == 8).windows is None
    assert query._adder_targets(ev, d) == ["width:20-20"]
    with pytest.raises(query.QueryError, match="selects by width only"):
        query._adder_targets(d.adders(width=lambda w: w == 12).where(lambda o: o.op == "add"), d)
    s = _session(tmp_path)
    assert query.map_adders(s, query.design_of(s).adders(width=lambda w: w > 12)).targets == \
        ("width:20-20",)


def test_a_list_of_widths_is_any_of_them_and_patterns_are_fnmatch(tmp_path):
    """width=[...] (a list or set) selects any of the listed
    widths / (lo, hi) windows and keeps their union as the windows; an empty
    list is refused. Patterns are fnmatch:
    [..] is a character class, [[] a literal bracket."""
    d = _db()
    w = d.adders(width=[8, 20])
    assert sorted({a.width for a in w}) == [8, 20] and w.windows == ((8, 8), (20, 20))
    assert query._adder_targets(w, d) == ["width:8-8", "width:20-20"]
    w2 = d.adders().filter(width={12, (19, 25)})
    assert sorted({a.width for a in w2}) == [12, 20] and w2.windows == ((12, 12), (19, 25))
    assert d.adders(op=["sub", "add"]).ids() == d.adders().ids()
    with pytest.raises(query.QueryError, match="empty list"):
        d.adders(width=[])
    assert d.flops("leaf/[qh]").ids() == ["leaf/h", "leaf/q"]
    assert d.flops("leaf/[[]q").ids() == []


def test_set_at_legacy_is_reserved():
    """a user entry cannot carry the legacy marker (it would be
    left out of the pass text and still recorded)."""
    with pytest.raises(options.OptionError, match="reserved"):
        scoping.from_value([{"command": "gate_register", "targets": ["q@0-3"], "set_at": "legacy"}])


@pytest.mark.parametrize("t", ["q@\u0661-3", "q@0-\u0663"])
def test_targets_take_ascii_digits_only(t):
    """Non-ASCII digits are refused."""
    with pytest.raises(options.OptionError, match="not .module/.reg@lo-hi"):
        scoping.from_value([{"command": "gate_register", "targets": [t]}])
    with pytest.raises(options.OptionError, match="not width:lo-hi"):
        scoping.from_value([{"command": "map_adders", "targets": ["width:1\u0662-20"]}])


# ---------------------------------------------------------------- legacy forms

_LEG = {"CIRCT_FLOW_DATA_GATE": "leaf/q@0-2@zero,leaf/q@3-11@zero",
        "CIRCT_FLOW_PREFIX2_ADDERS": "11", "CIRCT_FLOW_PREFIX2_MAX_WIDTH": "80",
        "CIRCT_FLOW_PREFIX2_ARCH": "HAN-CARLSON", "CIRCT_FLOW_PREFIX2_BOUNDARY": "1",
        "CIRCT_FLOW_ADDCH2_MIN": "8", "CIRCT_FLOW_ADDCH2_MAX": "8"}
_SCOPED = [{"command": "gate_register", "targets": ["leaf/q@0-2", "leaf/q@3-11"]},
           {"command": "map_adders", "targets": ["width:8-8"], "values": {"form": "cells"}},
           {"command": "map_adders", "targets": ["width:11-80"],
            "values": {"arch": "HAN-CARLSON", "boundary": 1}}]


def test_legacy_forms_and_scoped_form_build_the_same_commands(tmp_path):
    with redirect_stderr(io.StringIO()) as err:
        leg = _cfg(tmp_path / "a", env=_LEG)
    assert "syn.clock_gating.data_gate is a legacy form" in err.getvalue()
    assert "release 13" in err.getvalue()
    sco = _cfg(tmp_path / "b", {"syn.scoped": _SCOPED})
    cl = [x.replace(str(tmp_path / "a"), "<D>") for x in _struct_cmd(leg)]
    cs = [x.replace(str(tmp_path / "b"), "<D>") for x in _struct_cmd(sco)]
    assert cl == cs
    assert "--data-gate-enables=slices=leaf/q@0-2@zero,leaf/q@3-11@zero" in cs
    assert any(x.endswith("min-width=8 max-width=8") and "map-adder-chains" in x for x in cs)
    assert any("min-width=11 max-width=80 arch=HAN-CARLSON boundary=1" in x for x in cs)
    # the per-target record names the legacy origin
    a = scoping.record_extra(leg)["scoped_applied"]
    assert a[0]["origin"] == "syn.clock_gating.data_gate (env)"
    assert {x["status"] for x in a} == {"checked"} or {x["status"] for x in a} == {"unchecked"}


def test_legacy_option_set_by_argument_is_translated_and_warned(tmp_path):
    with redirect_stderr(io.StringIO()) as err:
        cfg = _cfg(tmp_path, {"syn.adders.cells2.min_width": 8, "syn.adders.cells2.max_width": 8})
    assert ("syn.adders.cells2.* is a legacy form (set by syn.adders.cells2.min_width (argument); "
            "syn.adders.cells2.max_width (argument))") in err.getvalue()
    assert any("map-adder-chains" in x and "min-width=8 max-width=8" in x
               for x in _struct_cmd(cfg))


def test_legacy_forms_refused_under_strict_and_from_their_release(tmp_path, monkeypatch):
    with pytest.raises(options.OptionError, match="--strict: syn.clock_gating.data_gate"):
        _cfg(tmp_path / "a", {"syn.clock_gating.data_gate": "leaf/q@1-4"}, strict=True)
    monkeypatch.setattr(options, "CURRENT_RELEASE", options.LEGACY_ENV_REMOVED_IN)
    with pytest.raises(options.OptionError, match=r"syn.adders.prefix2.\* is a legacy form"):
        _cfg(tmp_path / "b", {"syn.adders.prefix2.min_width": 56})
    # the scoped form is accepted under strict and after the removal
    cfg = _cfg(tmp_path / "c", {"syn.scoped": _SCOPED}, strict=True)
    assert len(scoping.entries(cfg.settings)) == 3


def test_legacy_guidance_one_warning_naming_the_scoped_form(tmp_path, monkeypatch):
    """a legacy variable of a legacy form gets ONE warning per form,
    naming the variables and recommending the scoped form (not the dotted
    name, itself deprecated); the --strict refusal recommends it too."""
    monkeypatch.setattr(scoping, "_WARNED", set())
    monkeypatch.setattr(options, "_WARNED", set())
    env = {"CIRCT_FLOW_PREFIX2_ADDERS": "11", "CIRCT_FLOW_PREFIX2_MAX_WIDTH": "80",
           "CIRCT_FLOW_PREFIX2_ARCH": "HAN-CARLSON", "CIRCT_FLOW_DATA_GATE": "leaf/q@1-4"}
    with redirect_stderr(io.StringIO()) as err:
        _cfg(tmp_path / "a", env=env)
        _cfg(tmp_path / "b", env=env)                      # repeated in a process: nothing new
    w = [x for x in err.getvalue().splitlines() if "WARNING" in x]
    assert len(w) == 2, w
    pre = next(x for x in w if "prefix2" in x)
    assert all(v in pre for v in ("CIRCT_FLOW_PREFIX2_ADDERS='11'", "CIRCT_FLOW_PREFIX2_MAX_WIDTH='80'",
                                  "CIRCT_FLOW_PREFIX2_ARCH='HAN-CARLSON'"))
    assert "use syn.scoped map_adders" in pre
    assert "use syn.scoped gate_register" in next(x for x in w if "data_gate" in x)
    assert not any("set syn.adders.prefix2" in x or "set syn.clock_gating.data_gate" in x for x in w)
    for var, form in (("CIRCT_FLOW_PREFIX2_ADDERS", "map_adders"), ("CIRCT_FLOW_DATA_GATE", "gate_register")):
        with pytest.raises(options.OptionError, match=f"--strict: legacy environment variable {var} "
                                                      f"is set; use syn.scoped {form}"):
            _cfg(tmp_path / "s", env={var: env[var]}, strict=True)
    # an ordinary legacy variable keeps its own warning (the option is no legacy form)
    with redirect_stderr(io.StringIO()) as err:
        options.resolve(env={"CIRCT_FLOW_MAX_FANOUT": "5"})
    assert "set syn.fanout.max instead" in err.getvalue()


def test_second_window_option_without_min_width_is_ignored_as_before(tmp_path, monkeypatch):
    """prefix2.arch / cells2.max_width without their min_width
    build nothing (as the legacy variables did): the warning says so (not "translated"),
    the structural command is the default one; --strict refuses."""
    monkeypatch.setattr(scoping, "_WARNED", set())
    base = _struct_cmd(_cfg(tmp_path / "d"))
    for i, (src, kw) in enumerate((("argument", {"settings": {"syn.adders.prefix2.arch": "HAN-CARLSON"}}),
                                   ("env", {"env": {"CIRCT_FLOW_ADDCH2_MAX": "20"}}))):
        with redirect_stderr(io.StringIO()) as err:
            cfg = _cfg(tmp_path / str(i), **kw)
        w = err.getvalue()
        assert "has no effect without syn.adders." in w and "ignored (as before)" in w, w
        assert "translated" not in w
        cmd = [x.replace(str(tmp_path / str(i)), "<D>") for x in _struct_cmd(cfg)]
        assert cmd == [x.replace(str(tmp_path / "d"), "<D>") for x in base]
        assert scoping.record_extra(cfg).get("scoped_applied", []) == []
        with pytest.raises(options.OptionError, match="--strict"):
            _cfg(tmp_path / f"s{i}", strict=True, **kw)


def test_dotted_legacy_forms_refused_from_their_release(tmp_path, monkeypatch):
    """from LEGACY_ENV_REMOVED_IN on, every legacy form is refused
    through every source (argument, flow.yaml options:, environment), the
    dotted grammars included, not only the legacy variables."""
    monkeypatch.setattr(options, "CURRENT_RELEASE", options.LEGACY_ENV_REMOVED_IN)
    cases = [{"syn.clock_gating.data_gate": "leaf/q@1-4"},
             {"syn.clock_gating.data_gate": "leaf/q@1-4@xor"},
             {"syn.adders.prefix2.min_width": 10, "syn.adders.prefix2.arch": "HAN-CARLSON"},
             {"syn.adders.prefix2.arch": "HAN-CARLSON"},
             {"syn.adders.cells2.min_width": 8, "syn.adders.cells2.max_width": 8},
             {"syn.adders.cells2.max_width": 8}]
    for i, c in enumerate(cases):
        with pytest.raises(options.OptionError, match="legacy form|no effect without"):
            _cfg(tmp_path / f"a{i}", c)
        with pytest.raises(options.OptionError, match="legacy form|no effect without"):
            options.resolve(recipe=c, where={"recipe": "flow.yaml options:"})
    with pytest.raises(options.OptionError):
        _cfg(tmp_path / "e", env={"CIRCT_FLOW_DATA_GATE": "leaf/q@1-4"})
    # the scoped form is not a legacy form: accepted
    assert len(scoping.entries(_cfg(tmp_path / "s", {"syn.scoped": _SCOPED}).settings)) == 3


def test_legacy_empty_forms_pass_on_without_strict(tmp_path):
    """Without --strict a legacy form the variable accepted (an empty prefix2
    window, a data-gate slice written hi-lo) is passed on in the legacy
    text, warned, recorded "unchecked"; under --strict
    and as a scoped form it is refused."""
    env = {"CIRCT_FLOW_PREFIX2_ADDERS": "56", "CIRCT_FLOW_PREFIX2_MAX_WIDTH": "20",
           "CIRCT_FLOW_DATA_GATE": "leaf/q@4-1"}
    with redirect_stderr(io.StringIO()) as err:
        cfg = _cfg(tmp_path / "a", env=env)
        cmd = _struct_cmd(cfg)
    lib = cfg.design.lib
    # the legacy text: verbatim slices; prefix2 min/max as given
    assert "--data-gate-enables=slices=leaf/q@4-1" in cmd
    assert (f"--map-prefix-adders=enable-prefix-adders=true liberty={lib} min-width=56 "
            f"max-width=20 arch=SKLANSKEY") in cmd
    assert "legacy form" in err.getvalue()
    a = {x["target"]: x for x in scoping.record_extra(cfg)["scoped_applied"]}
    assert a["width:56-20"]["status"] == a["leaf/q@4-1"]["status"] == "unchecked"
    assert "empty width window" in a["width:56-20"]["detail"]
    assert "empty slice" in a["leaf/q@4-1"]["detail"]
    with redirect_stderr(io.StringIO()) as err:
        scoping._WARNED.clear()
        scoping.plan(cfg)
    assert "passed on unchanged" in err.getvalue()
    # under --strict: refused (the legacy forms at all, and the empty window)
    with pytest.raises(options.OptionError, match="--strict"):
        _cfg(tmp_path / "b", env=env, strict=True)
    S = options.resolve(argument={"syn.adders.prefix2.min_width": 8,
                                  "syn.adders.prefix2.max_width": 3}, warn=False)
    S.strict = True
    with pytest.raises(options.OptionError, match="prefix2.max_width=3 is below"):
        scoping.legacy_entries(S)
    # the scoped forms: refused
    for i, (cmd_, bad) in enumerate((("map_adders", "width:56-20"), ("gate_register", "leaf/q@4-1"))):
        with pytest.raises(options.OptionError, match="lo <= hi"):
            _cfg(tmp_path / f"s{i}", {"syn.scoped": [{"command": cmd_, "targets": [bad]}]})


@pytest.mark.parametrize("windows, glob, refused", [
    # two scoped prefix windows sharing adders: the second is refused
    ([("prefix", "width:1-20"), ("prefix", "width:10-40")], None, "width:10-40"),
    ([("prefix", "width:1-20"), ("prefix", "width:1-20")], None, "width:1-20"),
    # a scoped window partly inside the global prefix instance's window
    ([("prefix", "width:10-40")], ("prefix", 10, 12), "width:10-40"),
    # cells before prefix: a prefix window over a cells window's adders
    ([("cells", "width:3-14"), ("prefix", "width:11-40")], None, "width:11-40"),
    # disjoint windows: nothing refused
    ([("prefix", "width:17-60")], ("prefix", 8, 12), None),
])
def test_overlapping_adder_windows_are_refused(tmp_path, windows, glob, refused):
    """an adder two pass instances select is built by the
    first; the later target is refused at the precheck, never recorded
    "applied" for adders it did not build."""
    sc = [{"command": "map_adders", "targets": [w], "values": {"form": f}} for f, w in windows]
    st = {"syn.scoped": sc}
    if glob:
        pre = "syn.adders.prefix" if glob[0] == "prefix" else "syn.adders.cells"
        st.update({pre + ".min_width": glob[1], pre + ".max_width": glob[2]})
    with redirect_stderr(io.StringIO()):
        cfg = _cfg(tmp_path, st)
        if refused is None:
            _cmds(cfg)
            assert all(x["status"] != "refused" for x in scoping.record_extra(cfg)["scoped_applied"])
            return
        with pytest.raises(options.OptionError, match="already\s+selects"):
            _cmds(cfg)
    a = scoping.record_extra(cfg)["scoped_applied"]
    assert [x["status"] for x in a if x["target"] == refused][-1] == "refused"
    assert sum(x["status"] == "refused" for x in a) == 1


# ---------------------------------------------------------------- record, round trip, default

def test_settings_json_round_trip(tmp_path):
    s = _session(tmp_path / "a")
    d = query.design_of(s)
    query.gate_register(s, d.flops("leaf/q").bits(3, 9))
    query.map_adders(s, d.adders(width=(11, 40)), arch="HAN-CARLSON")
    c1 = _struct_cmd(s.cfg)
    p = s.write_settings(tmp_path / "settings.json")
    rec = json.loads(p.read_text())
    assert rec["options"]["syn.scoped"]["source"] == "argument"
    # checked against the core IR (members), the pass report not seen (stubbed tool run)
    assert [(x["status"], bool(x["members"])) for x in rec["scoped_applied"]] == \
        [("unchecked", True), ("unchecked", True)]
    f2, out2 = _flow(tmp_path / "b")
    s2 = Session.from_settings(f2, out2, p, **_KW)
    assert s2.get("syn.scoped") == s.get("syn.scoped")
    c2 = _struct_cmd(s2.cfg)
    norm = lambda c, d: [x.replace(str(d), "<D>") for x in c]   # noqa: E731
    assert norm(c1, tmp_path / "a") == norm(c2, tmp_path / "b")


def test_default_has_no_scoped_record_and_no_report_capture(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    called = []
    monkeypatch.setattr(scoping, "capture_stderr", lambda *a, **k: called.append(1))
    cmd = _struct_cmd(cfg)
    assert not any("data-gate-enables" in x or "map-adder-chains" in x or "map-prefix-adders" in x
                   for x in cmd)
    assert called == [] and scoping.record_extra(cfg) == {}
    assert "scoped_applied" not in provenance_settings.record(cfg)


# ---------------------------------------------------------------- the passes themselves

ABC_OPT = os.environ.get("CIRCT_ABC_OPT") or str(ROOT / "mapper/build/bin/circt-abc-opt")
needs_tool = pytest.mark.skipif(not Path(ABC_OPT).exists(), reason="circt-abc-opt not built")


def _run_pass(args, src, tmp_path):
    out = tmp_path / f"o{len(list(tmp_path.iterdir()))}.mlir"
    r = subprocess.run([ABC_OPT, *args, str(src), "-o", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_text(), r.stderr


def _plan_of(settings, lib, core=None):
    S = options.resolve(argument=settings)
    cfg = NS(settings=S, core_mlir=Path(core or "/tmp/scoped-unit-none/core.mlir"), struct_mlir=Path("/tmp/scoped-unit-none/s.mlir"),
             out_dir=Path("/tmp/scoped-unit"), design=NS(lib=lib, top_module="dg"))
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return scoping.plan(cfg), S


@needs_tool
def test_data_gating_pass_same_output_and_report_checked(tmp_path):
    src = TESTS / "data-gate-enables" / "data_gate.mlir"
    with redirect_stderr(io.StringIO()):
        pl, Sl = _plan_of({"syn.clock_gating.data_gate": "q@0-3,q@12-15@xor,dg/h@4-7"}, "L")
    ps, Ss = _plan_of({"syn.scoped": [
        {"command": "gate_register", "targets": ["q@0-3"]},
        {"command": "gate_register", "targets": ["q@12-15"], "values": {"detect": "xor"}},
        {"command": "gate_register", "targets": ["dg/h@4-7"]}]}, "L")
    a, ea = _run_pass(pl.data_gate_args(), src, tmp_path)
    b, eb = _run_pass(ps.data_gate_args(), src, tmp_path)
    assert a == b and ea == eb
    scoping.postcheck(ps, eb, Ss)
    assert [t.status for t in ps.targets] == ["applied"] * 3
    # a slice the pass skips is refused from its report
    pz, Sz = _plan_of({"syn.scoped": [{"command": "gate_register", "targets": ["q@0-30"]}]}, "L")
    _, ez = _run_pass(pz.data_gate_args(), src, tmp_path)
    with pytest.raises(options.OptionError, match="found but skipped"):
        scoping.postcheck(pz, ez, Sz)


@needs_tool
def test_prefix_window_pass_same_output_as_legacy(tmp_path):
    lib = TESTS / "map-prefix-adders" / "prefix.lib"
    src = TESTS / "map-prefix-adders" / "adds.mlir"
    with redirect_stderr(io.StringIO()):
        pl, _ = _plan_of({"syn.adders.prefix2.min_width": 11, "syn.adders.prefix2.max_width": 90,
                          "syn.adders.prefix2.arch": "HAN-CARLSON",
                          "syn.adders.prefix2.boundary": 1}, lib)
    ps, Ss = _plan_of({"syn.scoped": [{"command": "map_adders", "targets": ["width:11-90"],
                                       "values": {"arch": "HAN-CARLSON", "boundary": 1}}]}, lib)
    assert pl.adder_args("prefix") == ps.adder_args("prefix")
    a, _ = _run_pass(pl.adder_args("prefix"), src, tmp_path)
    b, eb = _run_pass(ps.adder_args("prefix"), src, tmp_path)
    assert a == b
    scoping.postcheck(ps, eb, Ss)
    assert ps.targets[0].status == "applied"


@needs_tool
def test_adder_cells_pass_same_output_as_legacy(tmp_path):
    lib = TESTS / "map-adder-chains" / "adders.lib"
    src = TESTS / "map-adder-chains" / "max_width.mlir"
    with redirect_stderr(io.StringIO()):
        pl, _ = _plan_of({"syn.adders.cells2.min_width": 5, "syn.adders.cells2.max_width": 30}, lib)
    ps, Ss = _plan_of({"syn.scoped": [{"command": "map_adders", "targets": ["width:5-30"],
                                       "values": {"form": "cells"}}]}, lib)
    assert pl.adder_args("cells") == ps.adder_args("cells")
    a, _ = _run_pass(pl.adder_args("cells"), src, tmp_path)
    b, eb = _run_pass(ps.adder_args("cells"), src, tmp_path)
    assert a == b and "lowered 2 comb.add" in eb
    scoping.postcheck(ps, eb, Ss)
    # a window with no adder: the pass builds nothing, refused
    pn, Sn = _plan_of({"syn.scoped": [{"command": "map_adders", "targets": ["width:50-60"],
                                       "values": {"form": "cells"}}]}, lib)
    _, en = _run_pass(pn.adder_args("cells"), src, tmp_path)
    assert "lowered 0 comb.add op(s) (no add in the width window)" in en   # the binary reports 0
    with pytest.raises(options.OptionError, match="its pass instance built nothing"):
        scoping.postcheck(pn, en, Sn)


def test_legacy_form_without_strict_passes_on(tmp_path):
    """CIRCT_FLOW_PREFIX2_ADDERS=56 on a
    design without such adders runs with the legacy pass text, with the
    deprecation warning and a 'matched nothing' warning; status unchecked.
    The scoped form, and the legacy form under --strict, are refused."""
    env = {"CIRCT_FLOW_PREFIX2_ADDERS": "56"}
    with redirect_stderr(io.StringIO()) as err:
        cfg = _cfg(tmp_path / "a", env=env)
        with redirect_stdout(io.StringIO()):
            cmd = next(c for c in golden_cmdlines.stage_commands(cfg)
                       if c[-2:] == ["-o", str(cfg.struct_mlir)])
    w = err.getvalue()
    assert "syn.adders.prefix2.* is a legacy form (set by CIRCT_FLOW_PREFIX2_ADDERS='56' (env))" in w
    assert "legacy syn.adders.prefix2.* (env) width:56-: matched no adder" in w
    assert any(x.startswith("--map-prefix-adders=enable-prefix-adders=true liberty=/LIB/a.lib "
                            "min-width=56 max-width=0 arch=SKLANSKEY") for x in cmd)
    a = scoping.record_extra(cfg)["scoped_applied"]
    assert a[0]["status"] == "unchecked" and "legacy form, not strict" in a[0]["detail"]
    # the same through the scoped form: refused
    cfg = _cfg(tmp_path / "b", {"syn.scoped": [{"command": "map_adders", "targets": ["width:56-"],
                                                "values": {"arch": "HAN-CARLSON"}}]})
    with pytest.raises(options.OptionError, match="matched no adder"):
        _cmds(cfg)
    # the legacy form under --strict: refused
    with pytest.raises(options.OptionError, match="--strict"):
        _cfg(tmp_path / "c", env=env, strict=True)
    # and the pass-report stage: a legacy target that built nothing, not strict: warned, not refused
    x = scoping.make("map_adders", ["width:11-40"], {}, "legacy")
    t = scoping.Target("syn.adders.prefix2.* (env)", x, "width:11-40", "prefix", legacy=True)
    p = scoping.Plan([t], lib="L", key="k2")
    with redirect_stderr(io.StringIO()):
        scoping.postcheck(p, "[map-prefix-adders] 0 add(s) built as X\n", NS(values={}, strict=False))
    assert t.status == "unchecked"
    t.status = "pending"
    with pytest.raises(options.OptionError, match="built nothing"):
        scoping.postcheck(p, "[map-prefix-adders] 0 add(s) built as X\n", NS(values={}, strict=True))
