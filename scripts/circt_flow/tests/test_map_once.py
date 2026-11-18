"""map_cache and map_once: an entry is used only for the identical identity; a repeated module is mapped once and put
back. Hermetic: a stand-in mapper, invented files."""
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from circt_flow import map_cache as mc

EXTERN_RE = re.compile(r"^  hw\.module\.extern @([A-Za-z0-9_.$]+)\(.*$", re.M)
MODULE_RE = re.compile(r"^  hw\.module(?: private| public| nested)? @([A-Za-z0-9_.$]+)\(", re.M)


def blocks(text):
    out = {}
    for m in MODULE_RE.finditer(text):
        end = text.find("\n  }\n", m.start())
        out[m.group(1)] = (m.start(), end + len("\n  }\n"))
    return out


def design(body="%0 = comb.and %a, %b : i1", extern="in %A : i1, out Y : i1"):
    return ("module {\n"
            f"  hw.module.extern @CELL({extern})\n"
            "  hw.module @m_residual(in %a : i1, in %b : i1, out y : i1) {\n"
            f"    {body}\n    hw.output %0 : i1\n  }}\n"
            "  hw.module @other_residual(in %a : i1, out y : i1) {\n    hw.output %a : i1\n  }\n"
            "}\n")


class World:
    """files a mapping reads, and a stand-in for the mapper call that counts its runs"""

    def __init__(self, tmp, sub="w"):
        self.d = tmp / sub; self.d.mkdir()
        self.lib = self.d / "cells.lib"; self.lib.write_text("library(x) {}\n")
        self.stubs = self.d / "stubs.sv"; self.stubs.write_text("module CELL(); endmodule\n")
        self.inc = self.d / "inc.abc"; self.inc.write_text("strash\n")
        self.script = self.d / "s.abc"; self.script.write_text("source inc.abc\n&nf\n")
        self.mapper = self.d / "mapper"; self.mapper.write_text("#!/bin/sh\necho mapper 1\n"); self.mapper.chmod(0o755)
        self.abc = self.d / "abc"; self.abc.write_text("#!/bin/sh\necho abc 1\n"); self.abc.chmod(0o755)
        self.work = self.d / "work"; self.work.mkdir()
        self.runs = 0
        self.salt = ""

    def opt(self, period=1000):
        return (f"--abc-bridge=liberty={self.lib} abc-path={self.abc} stubs={self.stubs} clock-period-ps={period} "
                f"enable-classical-resub=true abc-script-file={self.script} read-lib-args=-l")

    def map_one(self, args):
        name, src, workdir = args[0], Path(args[1]), Path(args[2])
        self.runs += 1
        out = workdir / f"{name}.mlir"
        out.write_text(f"module {{\n  hw.module @{name}() {{\n    // from {mc.sha_text(src.read_text())[:8]}{self.salt}\n  }}\n}}\n")
        return SimpleNamespace(name=name, returncode=0, seconds=1.0, output=out, log=workdir / "log")

    def run(self, cache, text=None, **kw):
        text = design() if text is None else text
        return mc.map_cached(cache, "m_residual", text, blocks(text), self.work, self.mapper, self.opt(**kw), self.map_one, EXTERN_RE)


def test_miss_then_hit_and_another_directory_hits(tmp_path):
    c = mc.Cache(tmp_path / "cache")
    w = World(tmp_path)
    out1, _, st1 = w.run(c)
    out2, sec2, st2 = w.run(c)
    assert st1.startswith("miss") and st2 == "hit" and out1 == out2 and sec2 == 0.0 and w.runs == 1
    w2 = World(tmp_path, "elsewhere")                       # the same contents under other paths
    out3, _, st3 = w2.run(c)
    assert st3 == "hit" and out3 == out1 and w2.runs == 0


def test_the_other_module_of_the_design_does_not_matter(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.run(c)
    t = design().replace("hw.output %a : i1\n  }\n}", "%9 = comb.xor %a, %a : i1\n    hw.output %9 : i1\n  }\n}")
    assert w.run(c, text=t)[2] == "hit"


CHANGES = {
    "module text": lambda w: dict(text=design(body="%0 = comb.or %a, %b : i1")),
    "a cell declaration": lambda w: dict(text=design(extern="in %A : i1, in %B : i1, out Y : i1")),
    "an option value": lambda w: dict(period=1200),
    "liberty content": lambda w: w.lib.write_text("library(x) { /* new */ }\n") and {},
    "stubs content": lambda w: w.stubs.write_text("module CELL(input A); endmodule\n") and {},
    "script content": lambda w: w.script.write_text("source inc.abc\n&nf -R 5\n") and {},
    "a file the script names": lambda w: w.inc.write_text("strash; balance\n") and {},
    "the mapper binary": lambda w: w.mapper.write_text("#!/bin/sh\necho mapper 2\n") and {},
    "the abc binary": lambda w: w.abc.write_text("#!/bin/sh\necho abc 2\n") and {},
}


@pytest.mark.parametrize("what", sorted(CHANGES))
def test_any_change_of_what_the_mapper_reads_is_a_miss(tmp_path, what):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.run(c)
    kw = CHANGES[what](w) or {}
    _, _, st = w.run(c, **kw)
    assert st.startswith("miss"), what
    assert w.runs == 2


def test_environment_abc_rc_and_cache_code_are_in_the_identity(tmp_path, monkeypatch):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    monkeypatch.chdir(w.d)
    w.run(c)
    monkeypatch.setenv("ABC_SOMETHING", "1")
    assert w.run(c)[2].startswith("miss")
    monkeypatch.delenv("ABC_SOMETHING")
    assert w.run(c)[2] == "hit"
    monkeypatch.setenv("LC_NUMERIC", "de_DE.UTF-8")
    assert w.run(c)[2].startswith("miss")
    monkeypatch.delenv("LC_NUMERIC")
    for f in ("abc.rc", ".abc.rc", "somelib.skip"):                 # files ABC opens by name in the working directory
        (w.d / f).write_text("x\n")
        assert w.run(c)[2].startswith("miss"), f
        (w.d / f).unlink()
        assert w.run(c)[2] == "hit", f
    home = w.d / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    assert w.run(c)[2] == "hit"
    (home / ".abc.rc").write_text("alias x strash\n")
    assert w.run(c)[2].startswith("miss")
    monkeypatch.undo(); monkeypatch.chdir(w.d)
    code = w.d / "code.py"; code.write_text("v = 1\n")
    t = design()
    a = mc.identity("m_residual", mc.isolated_input(t, blocks(t), "m_residual", EXTERN_RE), w.mapper, w.opt(), [code])[0]
    code.write_text("v = 2\n")
    b = mc.identity("m_residual", mc.isolated_input(t, blocks(t), "m_residual", EXTERN_RE), w.mapper, w.opt(), [code])[0]
    assert mc.key_of(a) != mc.key_of(b)


def test_a_damaged_or_altered_entry_is_rejected_and_replaced(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    out, _, _ = w.run(c)
    key = c.report[-1]["key"]; mlir, rec = c.paths(key)
    mlir.write_text(out.replace("from", "FROM"))                                  # content no longer the recorded one
    _, _, st = w.run(c)
    assert "checksum" in st and w.runs == 2
    r = json.loads(rec.read_text()); r["identity"]["abc_bridge_options"][3]["value"] = "999"; rec.write_text(json.dumps(r))
    _, _, st = w.run(c)
    assert "identity differs" in st and w.runs == 3
    rec.unlink()                                                                    # a module without its record is never read
    assert w.run(c)[2].startswith("miss (no entry)")
    assert w.run(c)[2] == "hit"


def test_verify_maps_again_and_stops_on_a_difference(tmp_path):
    w = World(tmp_path)
    w.run(mc.Cache(tmp_path / "cache"))
    v = mc.Cache(tmp_path / "cache", verify=True)
    assert w.run(v)[2] == "hit, verified" and w.runs == 2
    w.salt = " (a mapper that does not repeat itself)"
    with pytest.raises(SystemExit) as e:
        w.run(v)
    assert "differs from a fresh mapping" in str(e.value)


def test_no_cache_maps_from_the_isolated_input(tmp_path):
    w = World(tmp_path)
    out, _, st = w.run(None)
    assert st == "mapped (no cache)" and w.runs == 1
    iso = (w.work / "m_residual.in.mlir").read_text()
    assert "@m_residual" in iso and "@CELL" in iso and "other_residual" not in iso


# ---- map_once: the text operations and the stage
from circt_flow import map_once as mo, abc_partition as ap  # noqa: E402

DESIGN = ("module {\n"
          "  hw.module.extern @CELL(in %A : i1, out Y : i1)\n"
          "  hw.module private @slice_residual(in %a : i1, out y : i1) {\n    hw.output %a : i1\n  }\n"
          "  hw.module private @ctl_residual(in %a : i1, out y : i1) {\n    hw.output %a : i1\n  }\n"
          "  hw.module @top(in %a : i1, out y : i1) {\n"
          '    %s0.y = hw.instance "s0" @slice_residual(a: %a: i1) -> (y: i1)\n'
          '    %s1.y = hw.instance "s1" @slice_residual(a: %s0.y: i1) -> (y: i1)\n'
          '    %c.y = hw.instance "c" @ctl_residual(a: %s1.y: i1) -> (y: i1)\n'
          "    hw.output %c.y : i1\n  }\n"
          "}\n")
MAPPED = ("module {\n"
          "  hw.module.extern @CELL(in %A : i1, out Y : i1)\n"
          "  hw.module.extern @CELL2(in %A : i1, out Y : i1)\n"
          "  hw.module @slice_residual(in %a : i1, out y : i1) {\n"
          '    %u.Y = hw.instance "u" @CELL2(A: %a: i1) -> (Y: i1)\n    hw.output %u.Y : i1\n  }\n'
          "}\n")


def test_shared_modules_are_the_residual_modules_with_enough_instances():
    b = ap.module_blocks(DESIGN)
    assert mo.shared_modules(DESIGN, b, 2) == ["slice_residual"]
    assert mo.shared_modules(DESIGN, b, 3) == []
    assert mo.remaining_targets(DESIGN, b, ["slice_residual"]) == ["ctl_residual"]
    assert mo.merge_targets(DESIGN, b) == ["slice_residual", "ctl_residual"]         # top instantiates: not a target
    # the grouping pass's own rule: a module with an instance inside is not merged, so it is not shared either,
    # whether it instantiates another module or a cell
    for inner in ('%c.y = hw.instance "c" @ctl_residual(a: %a: i1) -> (y: i1)\n    hw.output %c.y : i1',
                  '%u.Y = hw.instance "u" @CELL(A: %a: i1) -> (Y: i1)\n    hw.output %u.Y : i1'):
        d = DESIGN.replace("  hw.module private @slice_residual(in %a : i1, out y : i1) {\n    hw.output %a : i1\n  }\n",
                           "  hw.module private @slice_residual(in %a : i1, out y : i1) {\n    " + inner + "\n  }\n")
        assert "slice_residual" not in mo.shared_modules(d, ap.module_blocks(d), 2)
        assert "slice_residual" not in mo.merge_targets(d, ap.module_blocks(d))
    # a module nobody instantiates is not a target of the grouping
    dead = DESIGN.replace("  hw.module @top(", "  hw.module @dead(in %a : i1, out y : i1) {\n    hw.output %a : i1\n  }\n  hw.module @top(")
    assert "dead" not in mo.merge_targets(dead, ap.module_blocks(dead))


def test_hide_takes_every_visibility():
    for vis in ("", " private", " public", " nested"):
        d = DESIGN.replace("hw.module private @slice_residual(", f"hw.module{vis} @slice_residual(")
        assert "hw.module.extern @slice_residual(in %a : i1, out y : i1)" in mo.hide(d, ap.module_blocks(d), ["slice_residual"])


def test_hide_and_put_back():
    b = ap.module_blocks(DESIGN)
    hidden = mo.hide(DESIGN, b, ["slice_residual"])
    assert "hw.module.extern @slice_residual(in %a : i1, out y : i1)" in hidden
    assert "hw.module private @slice_residual" not in hidden and hidden.count("@ctl_residual(") == 2
    assert hidden.count('hw.instance "s') == 2                                   # the instances are untouched
    back = mo.put_back(hidden, {"slice_residual": MAPPED}, ap.module_blocks, ap._EXTERN_RE)
    assert "hw.module.extern @slice_residual" not in back
    assert "hw.module private @slice_residual(in %a : i1, out y : i1) {" in back and "@CELL2(A: %a" in back
    assert back.count("hw.module.extern @CELL2(") == 1 and back.count("hw.module.extern @CELL(") == 1
    assert back.rstrip().endswith("}") and set(ap.module_blocks(back)) == {"slice_residual", "ctl_residual", "top"}
    with pytest.raises(RuntimeError, match="extern declarations"):
        mo.put_back(DESIGN, {"slice_residual": MAPPED}, ap.module_blocks, ap._EXTERN_RE)


def _stage(tmp_path, monkeypatch, two_left=True, cache=True, **over):
    """_merge_and_map_partitioned with syn.abc.partition.map_once: the shared module goes to the mapper once, from an
    isolated input; the grouping reads the design without it; the mapped module is put back at the end."""
    from circt_flow import stages
    design = over.get("design", DESIGN)
    if two_left:
        design = (design.replace("  hw.module @top(", "  hw.module private @aux_residual(in %a : i1, out y : i1) {\n"
                                 "    hw.output %a : i1\n  }\n  hw.module @top(")
                  .replace("    hw.output %c.y : i1\n  }\n}\n",
                           '    %x.y = hw.instance "x" @aux_residual(a: %c.y: i1) -> (y: i1)\n    hw.output %x.y : i1\n  }\n}\n'))
    flat = tmp_path / "top_flat.mlir"; flat.write_text(design)
    mapped_out = tmp_path / "top_mapped.mlir"
    S = {"syn.abc.partition.map_once": True, "syn.abc.partition.map_once_min_instances": 2,
         "syn.abc.partition.map_cache": str(tmp_path / "cache") if cache else None, "syn.abc.partition.map_cache_verify": False,
         "syn.cleanup.specialize_mux_arms": False, "syn.library.dont_use_structural": False,
         "syn.abc.partition.group_map": None, "syn.abc.partition.group_by_depth": False,
         "syn.abc.partition.isolate_substr": None, "syn.abc.partition.isolate_each": False,
         "syn.abc.partition.group_scripts": None, "syn.cleanup.narrow_mux": False}
    mapper = tmp_path / "opt"; mapper.write_text("#!/bin/sh\necho opt 1\n"); mapper.chmod(0o755)
    S.update({k: v for k, v in over.items() if k.startswith("syn.")})
    cfg = SimpleNamespace(abc_use_mapped_dir=over.get("abc_use_mapped_dir"), abc_save_mapped_dir=over.get("abc_save_mapped_dir"),
                          abc_save_aig_dir=over.get("abc_save_aig_dir"), partitions=2, partition_workdir=tmp_path / "parts", settings=S, flat_mlir=flat, mapped_mlir=mapped_out,
                          circt_abc_opt=mapper, abc_pool_size=2, skip_unmappable=over.get("skip_unmappable", False), design=SimpleNamespace(lib="/LIB/a.lib", dont_use=[]))
    abc = tmp_path / "abc"; abc.write_text("#!/bin/sh\necho abc 1\n"); abc.chmod(0o755)
    monkeypatch.setattr(stages, "_abc_bridge_opt", lambda c: f"--abc-bridge=clock-period-ps=100 abc-path={abc}")
    monkeypatch.setattr(stages, "_specialize_mux_arms_opts", lambda c: [])
    monkeypatch.setattr(stages, "_remove_identity_buffers_opt", lambda c: "--remove-identity-buffers")
    calls, single = [], []

    def run(cmd, stdout=None, env=None):
        cmd = [str(c) for c in cmd]; calls.append(cmd)
        src, dst = cmd[-3], cmd[-1]
        Path(dst).write_text(Path(src).read_text())               # every whole-design pass: identity

    def map_one(args):
        name, src, workdir = args[0], Path(args[1]), Path(args[2])
        single.append((name, src.read_text()))
        # as the real mapper: every extern declaration of its input comes back in its output
        echoed = "".join(m.group(0) + "\n" for m in ap._EXTERN_RE.finditer(src.read_text()) if m.group(1) not in ("CELL", "CELL2"))
        out = workdir / f"{name}.mlir"
        out.write_text(MAPPED.replace("@slice_residual(", f"@{name}(").replace("module {\n", "module {\n" + echoed, 1))
        return SimpleNamespace(name=name, returncode=0, seconds=1.0, output=out, log=workdir / "log")

    groups_mapped = []

    def run(cmd, stdout=None, env=None):                                      # noqa: F811  (the merge makes a group module)
        cmd = [str(c) for c in cmd]; calls.append(cmd)
        text = Path(cmd[-3]).read_text()
        b = ap.module_blocks(text)
        tg = mo.merge_targets(text, b)
        if any(a.startswith("--merge-residual-modules") for a in cmd) and len(tg) >= 2:
            # as the pass: the targets' definitions go, their instances become instances of the one group module
            for n in sorted(tg, key=lambda n: b[n][0], reverse=True):
                text = text[:b[n][0]] + text[b[n][1]:]
                text = text.replace(f"@{n}(", "@abc_bridge_merged_residual_g0(")
            text = text.rstrip()[:-1] + "  hw.module private @abc_bridge_merged_residual_g0() {\n  }\n}\n"
        Path(cmd[-1]).write_text(text)

    def map_groups(grouped, workdir, opt, bridge, pool, per_group_extra=None):
        groups_mapped.append(str(grouped))
        return [SimpleNamespace(name="g0", returncode=0, seconds=2.0)]
    monkeypatch.setattr(stages, "_run", run)
    monkeypatch.setattr(ap, "_map_one", map_one)
    monkeypatch.setattr(ap, "map_groups", map_groups)
    monkeypatch.setattr(ap, "combine", lambda grouped, results, out: Path(out).write_text(Path(grouped).read_text()))
    return SimpleNamespace(stages=stages, cfg=cfg, calls=calls, single=single, mapped_out=mapped_out, groups_mapped=groups_mapped)


def test_the_stage_maps_shared_modules_once_and_groups_the_rest(tmp_path, monkeypatch):
    """_merge_and_map_partitioned with syn.abc.partition.map_once: the shared module goes to the mapper once, from an
    isolated input; the grouping reads the design without it; the mapped module is put back at the end."""
    t = _stage(tmp_path, monkeypatch)
    stages, cfg, calls, single, mapped_out = t.stages, t.cfg, t.calls, t.single, t.mapped_out
    assert stages._merge_and_map_partitioned(cfg) == mapped_out
    assert [n for n, _ in single] == ["slice_residual"]                          # once, though instantiated twice
    assert "ctl_residual" not in single[0][1] and "@top" not in single[0][1]     # from an isolated input
    merge = [c for c in calls if any(a.startswith("--merge-residual-modules") for a in c)][0]
    assert merge[-3].endswith("once/flat_without_shared.mlir")
    assert "hw.module.extern @slice_residual" in Path(merge[-3]).read_text()
    out = mapped_out.read_text()
    assert "hw.module private @slice_residual" in out and "@CELL2(A: %a" in out and "hw.module.extern @slice_residual" not in out
    assert calls[-1][1:3] == ["--canonicalize", "--remove-identity-buffers"]
    # a second run of the stage takes the module from the cache
    single.clear()
    assert stages._merge_and_map_partitioned(cfg) == mapped_out and single == []
    assert json.loads((tmp_path / "parts" / "once" / "cache_report.json").read_text())[0]["status"] == "hit"


def test_a_module_the_grouping_leaves_unmerged_and_that_cannot_be_mapped_stops_the_stage(tmp_path, monkeypatch):
    """no residual module may reach the netlist unmapped: also with skip-unmappable"""
    t = _stage(tmp_path, monkeypatch, two_left=False, cache=False, skip_unmappable=True)
    real = ap._map_one
    monkeypatch.setattr(ap, "_map_one", lambda args: real(args) if args[0] == "slice_residual" else
                        SimpleNamespace(name=args[0], returncode=1, seconds=1.0, output=None, log=Path(args[2]) / "log"))
    with pytest.raises(RuntimeError, match="ctl_residual: left unmerged by the grouping"):
        t.stages._merge_and_map_partitioned(t.cfg)


def test_one_module_left_is_mapped_by_itself_and_the_grouping_is_skipped(tmp_path, monkeypatch):
    """N copies of a block and ONE other leaf module: the grouping pass does nothing with a single module, so it is
    mapped the same way; the whole-design passes of the grouping command still run."""
    t = _stage(tmp_path, monkeypatch, two_left=False)
    assert t.stages._merge_and_map_partitioned(t.cfg) == t.mapped_out
    assert sorted(n for n, _ in t.single) == ["ctl_residual", "slice_residual"]
    # the grouping command still runs (its whole-design passes), finds nothing to group, and no group is mapped
    merge = [c for c in t.calls if any(a.startswith("--merge-residual-modules") for a in c)]
    assert len(merge) == 1 and "--canonicalize" in merge[0] and "--hw-aggregate-to-comb" in merge[0]
    assert t.groups_mapped == []
    rep = json.loads((tmp_path / "parts" / "once" / "cache_report.json").read_text())
    assert sorted(r["module"] for r in rep) == ["ctl_residual", "slice_residual"]          # one report for the stage run
    out = t.mapped_out.read_text()
    assert "hw.module private @slice_residual" in out and "hw.module private @ctl_residual" in out
    # every name is defined or declared exactly once (the leftover's mapper input did not carry the hidden module)
    for n in ("slice_residual", "ctl_residual"):
        assert len(re.findall(r"^  hw\.module(?:\.extern| private| public)? @%s\(" % n, out, re.M)) == 1, n
    assert "slice_residual" not in dict(t.single)["ctl_residual"]
    assert "hw.module.extern @slice_residual" not in out and "hw.module.extern @ctl_residual" not in out


@pytest.mark.parametrize("over, word", [
    ({"syn.cleanup.narrow_mux": True}, "syn.cleanup.narrow_mux"),
    ({"syn.cleanup.specialize_mux_arms": True}, "syn.cleanup.specialize_mux_arms"),
    ({"syn.abc.partition.isolate_substr": "_dp"}, "syn.abc.partition.isolate_substr"),
    ({"syn.abc.partition.group_scripts": "/a/file.json"}, "syn.abc.partition.group_scripts"),
    ({"abc_use_mapped_dir": "/a/dir"}, "use-mapped-dir"),
    ({"abc_save_mapped_dir": "/a/dir"}, "save-mapped-dir"),
    ({"abc_save_aig_dir": "/a/dir"}, "save-aig-dir"),
])
def test_what_acts_on_groups_only_is_refused_with_shared_modules(tmp_path, monkeypatch, over, word):
    t = _stage(tmp_path, monkeypatch, **over)
    with pytest.raises(ValueError, match=word):
        t.stages._merge_and_map_partitioned(t.cfg)
    assert t.single == []


def test_pipeline_edits_of_the_stage_are_refused_with_shared_modules(tmp_path, monkeypatch):
    t = _stage(tmp_path, monkeypatch)
    monkeypatch.setattr(t.stages, "RUNNER", lambda cmd, stdout=None, env=None: None)
    with pytest.raises(ValueError, match="pipeline edits or pass hooks"):
        t.stages._merge_and_map_partitioned(t.cfg)


# ---- what the cache cannot cover is mapped every time
@pytest.mark.parametrize("extra", ["use-mapped-dir=DIR", "save-mapped-dir=DIR", "save-aig-dir=DIR", "some-dir=DIR",
                                   "some-file=DIR/f.txt"])
def test_a_call_that_reads_or_writes_a_directory_is_never_cached(tmp_path, extra):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    d = tmp_path / "netlists"; d.mkdir(); (d / "f.txt").write_text("x\n")
    opt = w.opt() + " " + extra.replace("DIR", str(d))
    text = design()
    for _ in range(2):
        out, _, st = mc.map_cached(c, "m_residual", text, blocks(text), w.work, w.mapper, opt, w.map_one, EXTERN_RE)
        assert st.startswith("not cacheable")
    assert w.runs == 2 and not list((tmp_path / "cache").rglob("*.mlir"))
    assert c.report[-1]["status"].startswith("mapped, not cacheable")


def test_a_liberty_with_an_include_and_a_missing_abc_are_not_cacheable(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.lib.write_text('library(x) { include_file ("more.lib"); }\n')
    assert w.run(c)[2].startswith("not cacheable") and w.run(c)[2].startswith("not cacheable") and w.runs == 2
    w2 = World(tmp_path, "w2")
    w2.abc.unlink()
    assert w2.run(mc.Cache(tmp_path / "cache2"))[2].startswith("not cacheable")


def test_an_input_rewritten_during_the_mapping_is_not_stored(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    orig = w.map_one

    def slow(args):
        r = orig(args)
        w.stubs.write_text("module CELL(input A, input B); endmodule\n")        # changes while "the mapper runs"
        return r
    text = design()
    _, _, st = mc.map_cached(c, "m_residual", text, blocks(text), w.work, w.mapper, w.opt(), slow, EXTERN_RE)
    assert "not stored" in st and not list((tmp_path / "cache").rglob("*.mlir"))


def test_a_record_that_is_not_an_object_is_rejected(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.run(c)
    _, rec = c.paths(c.report[-1]["key"])
    rec.write_text("[1, 2]")
    assert "not a JSON object" in w.run(c)[2] and w.runs == 2


def test_the_code_that_builds_the_mapper_command_is_in_the_identity(tmp_path):
    w = World(tmp_path); t = design()
    ident = mc.identity("m_residual", mc.isolated_input(t, blocks(t), "m_residual", EXTERN_RE), w.mapper, w.opt())[0]
    assert {"map_cache.py", "abc_partition.py"} <= set(ident["cache_code"])


def test_skip_unmappable_leaves_a_module_the_mapper_fails_on_to_the_grouping(tmp_path, monkeypatch):
    t = _stage(tmp_path, monkeypatch, cache=False, skip_unmappable=True)
    monkeypatch.setattr(ap, "_map_one", lambda args: SimpleNamespace(name=args[0], returncode=1, seconds=1.0, output=None,
                                                                    log=Path(args[2]) / "log"))
    assert t.stages._merge_and_map_partitioned(t.cfg) == t.mapped_out
    merge = [c for c in t.calls if any(a.startswith("--merge-residual-modules") for a in c)][0]
    assert "hw.module private @slice_residual" in Path(merge[-3]).read_text()     # not hidden: the grouping gets it
    assert "hw.module.extern @slice_residual" not in t.mapped_out.read_text()
    assert len(t.groups_mapped) == 1                                               # and maps what it grouped
    # without the switch the failure stops the stage
    t2 = _stage(tmp_path / "b", monkeypatch, cache=False) if (tmp_path / "b").mkdir() is None else None
    monkeypatch.setattr(ap, "_map_one", lambda args: SimpleNamespace(name=args[0], returncode=1, seconds=1.0, output=None,
                                                                    log=Path(args[2]) / "log"))
    with pytest.raises(RuntimeError, match="failed to map"):
        t2.stages._merge_and_map_partitioned(t2.cfg)


# ---- more identity and option checks
def test_every_file_the_abc_script_reaches_is_in_the_identity(tmp_path):
    """a file named without an extension, and a file sourced by a sourced file, count; a source that is not found, or
    nesting without end, makes the call uncacheable"""
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    (w.d / "plain").write_text("strash\n")
    (w.d / "l2.abc").write_text("balance\n")
    w.inc.write_text("source l2.abc\n")
    w.script.write_text("source inc.abc\nsource plain\n&nf\n")
    assert w.run(c)[2].startswith("miss") and w.run(c)[2] == "hit"
    for f, text in ((w.d / "plain", "strash; rewrite\n"), (w.d / "l2.abc", "balance; rewrite\n")):
        f.write_text(text)
        assert w.run(c)[2].startswith("miss"), f.name
        assert w.run(c)[2] == "hit", f.name
    w.script.write_text("source nowhere.abc\n&nf\n")
    assert "sources nowhere.abc" in w.run(c)[2]
    w.script.write_text("source inc.abc\n&nf\n"); w.inc.write_text("source l2.abc\n"); (w.d / "l2.abc").write_text("source inc.abc\n")
    assert w.run(c)[2].startswith(("miss", "hit"))                              # a cycle ends: each file is taken once
    # a comment is not a command (the mapper strips it)
    w.script.write_text("# source the usual recipe\nsource inc.abc\n&nf\n")
    assert w.run(c)[2].startswith(("miss", "hit"))


def test_one_name_that_means_two_files_gives_two_entries(tmp_path, monkeypatch):
    """`source inc.abc` in the script and in a script one directory down are two files: both are in the identity"""
    w = World(tmp_path); t = design()
    monkeypatch.chdir(tmp_path)                                             # the working directory is elsewhere
    sub = w.d / "sub"; sub.mkdir()
    (sub / "x.abc").write_text("source inc.abc\n"); (sub / "inc.abc").write_text("balance\n")
    w.inc.write_text("strash\n")
    w.script.write_text("source inc.abc\nsource sub/x.abc\n&nf\n")
    iso = mc.isolated_input(t, blocks(t), "m_residual", EXTERN_RE)
    key = lambda: mc.key_of(mc.identity("m_residual", iso, w.mapper, w.opt())[0])
    k0 = key()
    w.inc.write_text("strash; rewrite\n"); k1 = key()
    (sub / "inc.abc").write_text("balance; rewrite\n"); k2 = key()
    assert len({k0, k1, k2}) == 3


def test_an_rc_file_is_followed_like_a_script(tmp_path, monkeypatch):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    monkeypatch.chdir(w.d)
    (w.d / "more.abc").write_text("alias x strash\n")
    (w.d / "abc.rc").write_text("source more.abc\n")
    assert w.run(c)[2].startswith("miss") and w.run(c)[2] == "hit"
    (w.d / "more.abc").write_text("alias x balance\n")
    assert w.run(c)[2].startswith("miss")


def test_a_stored_module_that_cannot_be_read_is_rejected(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.run(c)
    mlir, _ = c.paths(c.report[-1]["key"])
    mlir.write_bytes(b"\xff\xfe\x00bad")
    assert "cannot be read" in w.run(c)[2] and w.runs == 2


def test_an_option_value_stays_a_value_even_when_a_file_has_that_name(tmp_path, monkeypatch):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    monkeypatch.chdir(w.d)
    (w.d / "1000").write_text(""); (w.d / "1200").write_text("")
    w.run(c)
    assert w.run(c, period=1200)[2].startswith("miss")


def test_abc_is_found_as_the_mapper_finds_it(tmp_path, monkeypatch):
    w = World(tmp_path); t = design()
    iso = mc.isolated_input(t, blocks(t), "m_residual", EXTERN_RE)
    monkeypatch.chdir(w.d)                                                    # a file named abc here is NOT what a bare name means
    bare = w.opt().replace(f"abc-path={w.abc}", "abc-path=abc")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    with pytest.raises(mc.NotCacheable, match="abc-path abc is not found"):
        mc.identity("m_residual", iso, w.mapper, bare)
    monkeypatch.setenv("PATH", str(w.d) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    assert mc.identity("m_residual", iso, w.mapper, bare)[1]["abc-path"] == str(w.abc)
    with pytest.raises(mc.NotCacheable, match="no abc-path"):
        mc.identity("m_residual", iso, w.mapper, w.opt().replace(f" abc-path={w.abc}", ""))


def test_a_record_with_a_broken_identity_is_rejected_and_a_cache_that_cannot_be_written_costs_nothing(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.run(c)
    _, rec = c.paths(c.report[-1]["key"])
    r = json.loads(rec.read_text()); r["identity"] = [1, 2]; rec.write_text(json.dumps(r))
    assert "identity is not an object" in w.run(c)[2] and w.runs == 2
    c2 = mc.Cache(tmp_path / "cache2"); w2 = World(tmp_path, "w2")
    c2.store = lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only"))
    out, _, st = w2.run(c2)
    assert "not stored: PermissionError" in st and "@m_residual" in out


def test_options_of_the_family_that_cannot_act_are_refused(tmp_path):
    from circt_flow import stages
    def cfg(parts, once, cache, verify):
        return SimpleNamespace(partitions=parts, settings={"syn.abc.partition.map_once": once, "syn.abc.partition.map_cache": cache,
                                                           "syn.abc.partition.map_cache_verify": verify})
    stages._check_map_once_options(cfg(1, False, None, False))
    stages._check_map_once_options(cfg(4, True, "/c", True))
    for c, word in ((cfg(1, True, None, False), "partitioned mapping only"), (cfg(4, False, "/c", False), "needs syn.abc.partition.map_once"),
                    (cfg(4, True, None, True), "needs syn.abc.partition.map_cache")):
        with pytest.raises(ValueError, match=word):
            stages._check_map_once_options(c)


def test_put_back_does_not_declare_again_what_the_design_defines():
    b = ap.module_blocks(DESIGN)
    hidden = mo.hide(DESIGN, b, ["slice_residual", "ctl_residual"])
    echo = MAPPED.replace("@slice_residual(", "@ctl_residual(").replace(
        "module {\n", "module {\n  hw.module.extern @slice_residual(in %a : i1, out y : i1)\n", 1)
    back = mo.put_back(hidden, {"slice_residual": MAPPED, "ctl_residual": echo}, ap.module_blocks, ap._EXTERN_RE)
    assert len(re.findall(r"@slice_residual\(in %a", back)) == 1 and "hw.module.extern @slice_residual" not in back


def test_a_source_with_flags_and_a_missing_file_is_not_cacheable(tmp_path):
    c = mc.Cache(tmp_path / "cache"); w = World(tmp_path)
    w.script.write_text("source -x nowhere.abc\n&nf\n")
    assert "sources nowhere.abc" in w.run(c)[2]


def test_leftovers_take_the_same_refusals_as_shared_modules(tmp_path, monkeypatch):
    """no shared module (min_instances above every count) and one module the grouping leaves unmerged"""
    lone = DESIGN.replace('    %s0.y = hw.instance "s0" @slice_residual(a: %a: i1) -> (y: i1)\n', "").replace(
        '    %s1.y = hw.instance "s1" @slice_residual(a: %s0.y: i1) -> (y: i1)\n', "").replace("a: %s1.y: i1", "a: %a: i1")
    t = _stage(tmp_path, monkeypatch, two_left=False, design=lone, **{"syn.cleanup.narrow_mux": True})
    with pytest.raises(ValueError, match="syn.cleanup.narrow_mux"):
        t.stages._merge_and_map_partitioned(t.cfg)
    assert t.single == []


def test_a_helper_name_the_design_already_has_is_refused_by_name():
    b = ap.module_blocks(DESIGN)
    hidden = mo.hide(DESIGN, b, ["slice_residual"])
    brings = MAPPED.replace("  hw.module @slice_residual(", "  hw.module private @ctl_residual() {\n  }\n  hw.module @slice_residual(")
    with pytest.raises(RuntimeError, match="brings module\\(s\\) ctl_residual"):
        mo.put_back(hidden, {"slice_residual": brings}, ap.module_blocks, ap._EXTERN_RE)


def test_a_cache_directory_that_cannot_be_made_is_named(tmp_path, monkeypatch):
    blocker = tmp_path / "file"; blocker.write_text("x")
    t = _stage(tmp_path, monkeypatch, **{"syn.abc.partition.map_cache": str(blocker / "cache")})
    with pytest.raises(ValueError, match="syn.abc.partition.map_cache: .* cannot be used"):
        t.stages._merge_and_map_partitioned(t.cfg)


def test_the_cache_report_is_of_this_stage_run_only(tmp_path, monkeypatch):
    t = _stage(tmp_path, monkeypatch)
    t.stages._merge_and_map_partitioned(t.cfg)
    rep = tmp_path / "parts" / "once" / "cache_report.json"
    assert rep.is_file()
    t.cfg.settings["syn.abc.partition.map_cache"] = None                     # the next run in this directory: no cache
    t.stages._merge_and_map_partitioned(t.cfg)
    assert not rep.exists()
