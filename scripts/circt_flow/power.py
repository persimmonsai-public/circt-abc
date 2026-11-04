"""Power domains on their own supply rails, with modes and corners.

A flow config's `power:` section puts parts of the design on rails of a
schema-2 tech pack (profile.py):

    power:
      domains:
        - {name: lv, rail: VDD_LV, instances: [u_lv]}   # instance paths
        - {name: hv, rail: VDD_HV, module: core_hv}     # or every instance
                                                        # of a module

Everything else is on the pack's default_rail. With it, the flow

  1. elaborates the whole design once (<out>/hier/), and resolves each
     domain's instance paths to the elaborated module they instantiate;
  2. synthesizes each domain's module as its own top, through the ordinary
     single-domain flow, on its rail's liberties and flow defaults only
     (<out>/domains/<name>/, from a generated flow.yaml that names
     `pdk.rail` and `design.core_mlir`, so it can be re-run on its own);
  3. synthesizes the rest (<out>/remainder/) on the default rail, with each
     domain module an external (black-box) module, so the domain instances
     survive as instances;
  4. stitches <out>/1_synth/<top>.v: the remainder's netlist plus one
     definition of each domain module, a hierarchical netlist whose domain
     boundaries are module boundaries, and the OpenSTA-compatible one;
  5. writes <out>/power_domains.json: for each domain its rail, voltage,
     instance paths, module, the liberties and ABC settings it was mapped
     with, and its netlists.

With `modes:` (or `mapping_target:`) in the flow config, each domain's
mapping target is derived on its own (modes.py): its clocks are the SDC
clocks of the top-level ports that reach its instances (or its `clocks:`),
the tightest period over the modes, minus its rail's flop overhead at the
setup corner, times the scale. The domain's generated flow.yaml carries
that tightest period as clock_period_ps and the target as
design.abc_delay_target_ps; power_domains.json records the derivation, the
domain module's clock ports, and the modes and corners. Without either,
every domain gets the top's clock_period_ps, as before.

After stitching (intent.py): the nets that cross domains
(<out>/power_intent.json), the power intent <out>/<top>.upf, per-mode SDC
files for place and route (<out>/sdc/<mode>.sdc) and, with
`power: {insert_level_shifters: true}`, level shifters on the crossings of
the OpenSTA netlist. With --sta or `sta:`, multi-corner multi-mode timing
per domain (sta.py). `power: {ground: VSS}` names the UPF's ground supply.

Not yet: a domain module that is also instantiated outside its domain,
nested domains, `--pnr`, and --dont-touch/--dont-gate directives.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

DOMAIN_KEYS = {"name", "rail", "instances", "instance", "module", "clocks"}
POWER_KEYS = {"domains", "insert_level_shifters", "ground"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")


class PowerError(ValueError):
    """A power: section that cannot be honoured, with the reason."""


@dataclass(frozen=True)
class Domain:
    name: str
    rail: str
    instances: Tuple[str, ...] = ()
    module: Optional[str] = None
    # SDC clock names that clock this domain (default: traced from the
    # top-level ports that reach its instances).
    clocks: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The power: section
# ---------------------------------------------------------------------------


def parse_power(d, prof, where) -> Tuple[Domain, ...]:
    """Validate a flow config's `power:` against the profile's rails.
    Instance paths are checked against the design later (resolve)."""
    if prof is None or getattr(prof, "schema", 1) < 2:
        raise PowerError(f"{where}: power: needs a schema-2 tech pack with "
                         "`rails:` as pdk.profile")
    if not isinstance(d, dict) or set(d) - POWER_KEYS:
        bad = sorted(set(d) - POWER_KEYS) if isinstance(d, dict) else []
        raise PowerError(f"{where}: power: takes one key, `domains:` (a "
                         "list), besides the options insert_level_shifters "
                         "and ground"
                         + (f"; unknown key {bad[0]!r}" if bad else ""))
    doms = d.get("domains")
    if not isinstance(doms, list) or not doms:
        raise PowerError(f"{where}: power.domains must be a non-empty list")
    out: List[Domain] = []
    seen_names, seen_paths, seen_modules = {}, {}, {}
    for i, e in enumerate(doms):
        at = f"{where}: power.domains[{i}]"
        if not isinstance(e, dict):
            raise PowerError(f"{at} must be a mapping")
        unknown = set(e) - DOMAIN_KEYS
        if unknown:
            raise PowerError(f"{at}: unknown key {sorted(unknown)[0]!r}; "
                             f"allowed: {', '.join(sorted(DOMAIN_KEYS))}")
        name = e.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise PowerError(f"{at}: name must be an identifier, got {name!r}")
        if name in seen_names:
            raise PowerError(f"{at}: domain name {name!r} is used twice")
        if name == "top":
            raise PowerError(f"{at}: domain name 'top' is reserved for the "
                             "top's own (default-rail) domain")
        seen_names[name] = i
        rail = e.get("rail")
        if rail not in prof.rails:
            raise PowerError(f"{at} ({name}): unknown rail {rail!r}; "
                             f"{prof.name} has rails "
                             + ", ".join(prof.rails))
        given = [k for k in ("instances", "instance", "module") if k in e]
        if len(given) != 1:
            raise PowerError(f"{at} ({name}): give exactly one of instances, "
                             "instance or module")
        inst: List[str] = []
        module = None
        if given[0] == "module":
            module = e["module"]
            if not isinstance(module, str) or not _NAME.match(module):
                raise PowerError(f"{at} ({name}): module must be a module "
                                 f"name, got {module!r}")
            if module in seen_modules:
                raise PowerError(
                    f"{at} ({name}): overlapping domains: module {module} is "
                    f"also domain {seen_modules[module]}")
            seen_modules[module] = name
        else:
            raw = e[given[0]]
            inst = [raw] if given[0] == "instance" else raw
            if (not isinstance(inst, list) or not inst
                    or not all(isinstance(p, str) and _PATH.match(p)
                               for p in inst)):
                raise PowerError(
                    f"{at} ({name}): {given[0]} must be "
                    + ("an instance path" if given[0] == "instance"
                       else "a non-empty list of instance paths")
                    + " like u_core or u_sub.u_core")
            for p in inst:
                if p in seen_paths:
                    raise PowerError(
                        f"{at} ({name}): overlapping domains: instance {p} is "
                        f"also in domain {seen_paths[p]}")
                seen_paths[p] = name
        clocks = e.get("clocks", [])
        if (isinstance(clocks, str) or not isinstance(clocks, list)
                or not all(isinstance(c, str) and c for c in clocks)):
            raise PowerError(f"{at} ({name}): clocks must be a list of SDC "
                             "clock names")
        out.append(Domain(name=name, rail=rail, instances=tuple(inst),
                          module=module, clocks=tuple(clocks)))
    _check_nesting([(p, dm.name) for dm in out for p in dm.instances])
    return tuple(out)


def parse_options(d, where, prof=None) -> dict:
    """The power: section's options: insert_level_shifters (default false)
    and ground (the ground supply's name in the UPF, default VSS; not one
    of `prof`'s rails)."""
    ins = d.get("insert_level_shifters", False)
    if not isinstance(ins, bool):
        raise PowerError(f"{where}: power.insert_level_shifters must be true "
                         "or false")
    ground = d.get("ground", "VSS")
    if not isinstance(ground, str) or not _NAME.match(ground):
        raise PowerError(f"{where}: power.ground must be a supply name, got "
                         f"{ground!r}")
    if prof is not None and ground in getattr(prof, "rails", {}):
        raise PowerError(f"{where}: power.ground {ground} is also a rail of "
                         f"{prof.name}; name the ground supply differently")
    return {"insert_level_shifters": ins, "ground": ground}


def _check_nesting(paths: List[Tuple[str, str]]) -> None:
    """Overlap: one domain's instance inside another domain's instance."""
    for p, a in paths:
        for q, b in paths:
            if p != q and q.startswith(p + "."):
                where = ("domain " + a if a == b
                         else f"domains {a} and {b} overlap")
                raise PowerError(f"power: {where}: {q} is inside {p} "
                                 "(nested domains are not supported)")


# ---------------------------------------------------------------------------
# The elaborated hierarchy (core MLIR text)
# ---------------------------------------------------------------------------

_MOD_HDR = re.compile(
    r"^  hw\.module(?P<ext>\.extern)?(?P<priv> private)? @(?P<name>[^\s(]+)\(",
    re.M)
_INST = re.compile(r'hw\.instance "(?P<inst>[^"]+)"(?: sym @\S+)? '
                   r'@(?P<mod>[^\s(]+)\(')


@dataclass
class Module:
    name: str
    extern: bool
    public: bool
    instances: List[Tuple[str, str]]  # (instance name, module)


def parse_hierarchy(text: str) -> Dict[str, Module]:
    """Every hw.module / hw.module.extern of a core MLIR file and the
    hw.instance ops in each module's body."""
    mods: Dict[str, Module] = {}
    heads = list(_MOD_HDR.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end] if not m.group("ext") else ""
        mods[m.group("name")] = Module(
            name=m.group("name"), extern=bool(m.group("ext")),
            public=not m.group("priv"),
            instances=[(x.group("inst"), x.group("mod"))
                       for x in _INST.finditer(body)])
    return mods


def instance_paths(mods: Dict[str, Module], top: str) -> Dict[str, str]:
    """{hierarchical instance path: module} of every non-extern module
    instance under `top`."""
    out: Dict[str, str] = {}

    def walk(mod: str, prefix: str) -> None:
        for inst, target in mods[mod].instances:
            if target in mods and not mods[target].extern:
                path = f"{prefix}{inst}"
                out[path] = target
                walk(target, path + ".")
    if top not in mods:
        raise PowerError(f"power: top module {top} is not in the elaborated "
                         "design")
    walk(top, "")
    return out


@dataclass
class Resolved:
    domain: Domain
    module: str
    instances: List[str]


def resolve(domains, mods: Dict[str, Module], top: str) -> List[Resolved]:
    """Each domain's module and instance paths in the elaborated design.
    A domain is one elaborated module (a parameterization is its own
    module); every instance of that module must be in the domain."""
    paths = instance_paths(mods, top)
    by_module: Dict[str, List[str]] = {}
    for p, m in paths.items():
        by_module.setdefault(m, []).append(p)
    out: List[Resolved] = []
    for d in domains:
        if d.module is not None:
            if d.module == top:
                raise PowerError(f"power: domain {d.name}: module {top} is "
                                 "the top; the top is on default_rail")
            if d.module not in by_module:
                cand = sorted(m for m in by_module
                              if re.fullmatch(re.escape(d.module) + r"_\d+",
                                              m))
                raise PowerError(
                    f"power: domain {d.name}: module {d.module} is not "
                    f"instantiated under {top}"
                    + (f" (elaborated parameterizations: {', '.join(cand)};"
                       " name one, or give instance paths)" if cand else ""))
            out.append(Resolved(d, d.module, sorted(by_module[d.module])))
            continue
        modules = set()
        for p in d.instances:
            if p not in paths:
                parent = p.rsplit(".", 1)[0] if "." in p else ""
                here = sorted(q.rsplit(".", 1)[-1] for q in paths
                              if (q.rsplit(".", 1)[0] if "." in q else "")
                              == parent)
                raise PowerError(
                    f"power: domain {d.name}: unknown instance {p}; "
                    f"instances of {parent or top}: "
                    + (", ".join(here) or "none"))
            modules.add(paths[p])
        if len(modules) != 1:
            raise PowerError(
                f"power: domain {d.name}: its instances are of different "
                f"modules ({', '.join(sorted(modules))}); give one domain "
                "per module")
        mod = modules.pop()
        out.append(Resolved(d, mod, sorted(d.instances)))
    # Each domain module: all of its instances in that domain, and in no
    # other.
    owner: Dict[str, str] = {}
    for r in out:
        if r.module in owner:
            raise PowerError(
                f"power: domains {owner[r.module]} and {r.domain.name} both "
                f"hold module {r.module}; one module maps on one rail (not "
                "supported yet: a copy per domain)")
        owner[r.module] = r.domain.name
    for r in out:
        rest = sorted(set(by_module[r.module]) - set(r.instances))
        if rest:
            raise PowerError(
                f"power: domain {r.domain.name}: module {r.module} is also "
                f"instantiated at {', '.join(rest)}, outside the domain; "
                "put every instance of it in the domain (not supported yet: "
                "a copy per rail)")
    _check_nesting([(p, r.domain.name) for r in out for p in r.instances])
    return out


# ---------------------------------------------------------------------------
# Splitting the elaborated design
# ---------------------------------------------------------------------------


def make_top(text: str, mods: Dict[str, Module], new_top: str) -> str:
    """`text` with `new_top` the only public module (externs untouched);
    symbol-dce then drops what it does not reach."""
    def repl(m: re.Match) -> str:
        if m.group("ext"):
            return m.group(0)
        name = m.group("name")
        vis = "" if name == new_top else " private"
        return f"  hw.module{vis} @{name}("
    return _MOD_HDR.sub(repl, text)


def _externalize_mod():
    spec = importlib.util.spec_from_file_location(
        "externalize_stub_cells",
        Path(__file__).resolve().parent.parent / "externalize_stub_cells.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def blackbox(text: str, modules: List[str]) -> str:
    """Each of `modules` turned into an hw.module.extern (its ports kept)."""
    ext = _externalize_mod()
    for m in modules:
        text, n = ext.externalize(text, m)
        if n != 1:
            raise PowerError(f"power: could not black-box module {m} "
                             f"({n} definitions matched)")
    return text


def _symbol_dce(circt_opt: Path, src: Path, dst: Path) -> None:
    subprocess.run([str(circt_opt), "--symbol-dce", str(src), "-o", str(dst)],
                   check=True)


# ---------------------------------------------------------------------------
# Stitching the netlists
# ---------------------------------------------------------------------------

_V_MOD = re.compile(r"^module\s+(\w+)\s*(?:#\s*\(.*?\)\s*)?\((.*?)\);",
                    re.M | re.S)
_V_END = re.compile(r"^endmodule\b", re.M)


def verilog_modules(text: str) -> Dict[str, Tuple[List[str], str]]:
    """{module: (port names, body)} of a netlist (ANSI port lists, as
    ExportVerilog and sv2v write them)."""
    out: Dict[str, Tuple[List[str], str]] = {}
    for m in _V_MOD.finditer(text):
        hdr = re.sub(r"//[^\n]*|/\*.*?\*/", "", m.group(2), flags=re.S)
        ports = []
        for seg in hdr.split(","):
            words = re.findall(r"\\\S+|[A-Za-z_][A-Za-z0-9_$]*", seg)
            if words:
                ports.append(words[-1])
        end = _V_END.search(text, m.end())
        if end is None:
            raise PowerError(f"stitch: module {m.group(1)} has no endmodule")
        if m.group(1) in out:
            raise PowerError(f"stitch: module {m.group(1)} is defined twice")
        out[m.group(1)] = (ports, text[m.end():end.start()])
    return out


def _instances_of(body: str, module: str) -> List[Tuple[str, List[str]]]:
    """(instance name, connected port names) of each `module` instance."""
    res = []
    rx = re.compile(r"^\s*" + re.escape(module)
                    + r"\s+(?:#\s*\(.*?\)\s*)?(\\\S+|\w+)\s*\((.*?)\);",
                    re.M | re.S)
    for m in rx.finditer(body):
        conn = re.sub(r"//[^\n]*", "", m.group(2))
        res.append((m.group(1), re.findall(r"\.(\\\S+|\w+)\s*\(", conn)))
    return res


def stitch(remainder: str, top: str, domains: Dict[str, str]) -> Tuple[str, dict]:
    """One hierarchical netlist: the remainder (top `top`, instantiating
    each domain module as a black box) and each domain module's own
    netlist, keyed by module name. Checks that every domain module is
    defined by its netlist and instantiated by the remainder with ports it
    has, and that no module is defined twice. Returns the netlist and
    {module: [instance names in the top module]}."""
    rem = verilog_modules(remainder)
    if top not in rem:
        raise PowerError(f"stitch: the remainder netlist does not define "
                         f"{top}")
    defined = {m: "remainder" for m in rem}
    insts: Dict[str, List[str]] = {}
    for mod, text in domains.items():
        dm = verilog_modules(text)
        if mod not in dm:
            raise PowerError(f"stitch: the netlist for domain module {mod} "
                             "does not define it")
        for m in dm:
            if m in defined:
                raise PowerError(f"stitch: module {m} is defined by both "
                                 f"{defined[m]} and domain module {mod}")
            defined[m] = mod
        ports = set(dm[mod][0])
        found = []
        for name, (_, body) in rem.items():
            for inst, conn in _instances_of(body, mod):
                bad = sorted(set(conn) - ports)
                if bad:
                    raise PowerError(
                        f"stitch: {name}.{inst} connects port(s) "
                        f"{', '.join(bad)} that {mod}'s netlist does not "
                        "have")
                if name == top:
                    found.append(inst)
        if not found and not any(_instances_of(b, mod)
                                 for _, b in rem.values()):
            raise PowerError(f"stitch: the remainder does not instantiate "
                             f"domain module {mod}")
        insts[mod] = found
    parts = [f"// Hierarchical netlist of {top}, stitched by "
             "circt_flow.power: the remainder, then one module per power "
             "domain.\n", remainder.rstrip() + "\n"]
    for mod, text in domains.items():
        parts.append(f"\n// ---- power domain module {mod} ----\n")
        parts.append(text.rstrip() + "\n")
    return "".join(parts), insts


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_flow(path: Path, top: str, period: int, core: Path,
                profile: Path, rail: str, note: str,
                target: Optional[int] = None) -> None:
    design = {"top_module": top, "clock_period_ps": period,
              "core_mlir": core.name}
    if target is not None:
        design["abc_delay_target_ps"] = target
    path.write_text(f"# Generated by circt_flow.power: {note}\n" + yaml.safe_dump(
        {"design": design,
         "pdk": {"profile": str(profile), "rail": rail},
         # The stitched netlist is buffered per module after the intent
         # step (fanout.run_power), not each domain's own.
         "fanout_buffer": False},
        sort_keys=False))


def domain_timing(cfg, text: str, top: str, name: str, rail: str,
                  paths: List[str], clock_names=()) -> dict:
    """A domain's mapping-target derivation (modes.derive_target, with
    the flop overhead of `rail`'s setup-corner flops) and the ports of its
    module that a clock reaches. `paths` empty: the top itself (every top
    port reaches it)."""
    from . import modes as modes_mod
    from .pipeline import mapping_target
    mlist = cfg.design.effective_modes()
    clock_ports = {p for m in mlist for c in m.clocks.values()
                   for p in c.ports}
    if paths:
        feeding, mod_clk = set(), set()
        for p in paths:
            env, _ = modes_mod.feeding_ports(text, top, p)
            for port, srcs in env.items():
                feeding |= srcs
                if srcs & clock_ports:
                    mod_clk.add(port)
    else:
        feeding = None
        mod_clk = clock_ports & set(
            modes_mod._ARG.findall(modes_mod._module_text(text, top)[0]))
    rec = mapping_target(cfg.design, name, feeding, list(clock_names) or None,
                         rail)
    rec["clock_ports"] = sorted(mod_clk)
    print(modes_mod.log_target(rec, "[power] mapping target"))
    return rec


def _sub_run(parent, flow_yaml: Path, out: Path):
    """The ordinary single-domain flow on a generated flow.yaml, with the
    parent's command-line overrides; the pack's flow.env defaults it
    applies do not leak into the next sub-run."""
    from .config import FlowConfig
    from . import pipeline
    ov = {k: v for k, v in parent.load_overrides.items()
          if k not in ("partition_workdir", "directives", "run_sta",
                       "run_vt_swap")}
    sub = FlowConfig.load(flow_yaml, out, root=parent.root, **ov)
    saved = dict(os.environ)
    try:
        pipeline.run(sub)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return sub


def _cells_outside(netlist: Path, libs, skip=()) -> Tuple[int, List[str]]:
    """(cell instances, cell types not in `libs`) of a mapped netlist's
    leaf instances (types defined as modules in it, and `skip`, the
    black-boxed domain modules, are not cells)."""
    from .techpack import liberty_cells
    cells = liberty_cells(libs)
    text = netlist.read_text()
    mods = set(verilog_modules(text)) | set(skip)
    n, unknown = 0, set()
    for t in re.findall(r"^\s*(\w+)\s+(?:\\\S+|\w+)\s*\(", text, re.M):
        if t in ("module", "assign", "input", "output", "wire", "reg") \
                or t in mods:
            continue
        if t in cells:
            n += 1
        else:
            unknown.add(t)
    return n, sorted(unknown)


def _record(sub, rail_obj, extra: dict, skip=()) -> dict:
    libs = [Path(p) for p in sub.design.synth_lib_tt]
    n, outside = _cells_outside(sub.netlist, rail_obj.liberties, skip)
    return {
        **extra,
        "rail": sub.design.rail,
        "voltage": sub.design.voltage,
        "module": sub.design.top_module,
        "liberties": [str(p) for p in libs],
        "primary_liberty": str(libs[0]),
        "primary_liberty_sha256": _sha256(libs[0]),
        "abc_read_lib_args": sub.abc_read_lib_args,
        "abc_script_file": (str(sub.abc_script_file)
                            if sub.abc_script_file else None),
        "clock_period_ps": sub.design.clock_period_ps,
        "abc_delay_target_ps": sub.design.abc_target_ps,
        "flow_config": str(sub.config_path),
        "out_dir": str(sub.out_dir),
        "netlist": str(sub.netlist),
        "openroad_netlist": (str(sub.openroad_netlist)
                             if sub.openroad_netlist.exists() else None),
        "cell_instances": n,
        "cells_not_in_rail_liberties": outside,
    }


def run(cfg, pnr: bool = False) -> Path:
    """The power-domain flow for `cfg` (whose design.power is set). Returns
    <out>/power_domains.json."""
    from . import profile as profile_mod
    from . import stages

    if pnr:
        raise PowerError("power: --pnr is not supported with power domains "
                         "yet (P&R needs per-instance libraries)")
    if (cfg.directives.dont_touch_names or cfg.directives.dont_gate_names):
        raise PowerError("power: --dont-touch/--dont-gate are not supported "
                         "with power domains yet")
    d = cfg.design
    top, out = d.top_module, cfg.out_dir
    prof = profile_mod.load(d.profile)
    domains = d.power

    # 1. The whole design, elaborated once.
    hier = out / "hier"
    hier.mkdir(parents=True, exist_ok=True)
    print(f"[power] elaborating {top} -> {hier}")
    if d.core_mlir is None and d.manifest is None:
        raise PowerError("power: needs design.manifest or design.core_mlir "
                         "(the lower_ibex.sh front end is not supported)")
    core = stages.run_lower_ibex(dataclasses.replace(cfg, out_dir=hier))
    text = core.read_text()
    mods = parse_hierarchy(text)
    resolved = resolve(domains, mods, top)
    for r in resolved:
        print(f"[power] domain {r.domain.name}: {', '.join(r.instances)} "
              f"({r.module}) on {r.domain.rail}")

    profile_path = Path(d.profile).resolve()
    period = d.clock_period_ps
    # Per-domain mapping targets (modes: / mapping_target:), else every
    # domain maps at the top's clock_period_ps as before.
    timing: Dict[str, dict] = {}
    if d.calibrate:
        for r in resolved:
            timing[r.domain.name] = domain_timing(
                cfg, text, top, r.domain.name, r.domain.rail, r.instances,
                r.domain.clocks)
        timing["remainder"] = domain_timing(cfg, text, top, "remainder",
                                            prof.default_rail, [])

    # 2. Each domain on its own rail.
    records = []
    dom_netlists: Dict[str, str] = {}
    for r in resolved:
        ddir = (out / "domains" / r.domain.name).resolve()
        ddir.mkdir(parents=True, exist_ok=True)
        cut = ddir / f"{r.module}_cut.mlir"
        cut.write_text(make_top(text, mods, r.module))
        dcore = ddir / f"{r.module}_extracted.mlir"
        _symbol_dce(cfg.circt_opt, cut, dcore)
        fy = ddir / "flow.yaml"
        t = timing.get(r.domain.name)
        _write_flow(fy, r.module,
                    int(round(t["period_ps"])) if t else period, dcore,
                    profile_path, r.domain.rail,
                    f"power domain {r.domain.name} of {top}",
                    t["target_ps"] if t else None)
        print(f"[power] domain {r.domain.name}: synthesizing {r.module} on "
              f"{r.domain.rail} -> {ddir}")
        sub = _sub_run(cfg, fy, ddir)
        if sub.design.rail != r.domain.rail:  # never: the flow.yaml names it
            raise PowerError(f"power: domain {r.domain.name} ran on "
                             f"{sub.design.rail}")
        rec = _record(sub, prof.rails[r.domain.rail], {
            "name": r.domain.name, "instances": r.instances})
        if t:
            rec["mapping_target"] = t
        records.append(rec)
        dom_netlists[r.module] = sub.netlist.read_text()

    # 3. The rest of the design on the default rail, domains black-boxed.
    rdir = (out / "remainder").resolve()
    rdir.mkdir(parents=True, exist_ok=True)
    cut = rdir / f"{top}_cut.mlir"
    cut.write_text(blackbox(make_top(text, mods, top),
                            [r.module for r in resolved]))
    rcore = rdir / f"{top}_extracted.mlir"
    _symbol_dce(cfg.circt_opt, cut, rcore)
    fy = rdir / "flow.yaml"
    t = timing.get("remainder")
    _write_flow(fy, top, int(round(t["period_ps"])) if t else period, rcore,
                profile_path, prof.default_rail,
                f"the rest of {top} (power domains black-boxed)",
                t["target_ps"] if t else None)
    print(f"[power] remainder: synthesizing {top} on {prof.default_rail} "
          f"-> {rdir}")
    rsub = _sub_run(cfg, fy, rdir)

    # 4. Stitch.
    cfg.synth_dir.mkdir(parents=True, exist_ok=True)
    netlist, insts = stitch(rsub.netlist.read_text(), top, dom_netlists)
    cfg.netlist.write_text(netlist)
    print(f"[power] stitched hierarchical netlist -> {cfg.netlist}")
    stages.opensta_compat_netlist(cfg)

    rec_rem = _record(rsub, prof.rails[prof.default_rail],
                      {"name": "remainder", "instances": []},
                      skip=[r.module for r in resolved])
    if t:
        rec_rem["mapping_target"] = t
    for rec in records:
        rec["netlist_instances"] = insts.get(rec["module"], [])
    summary = {
        "top": top,
        "profile": str(profile_path),
        "default_rail": prof.default_rail,
        "rails": {n: {"voltage": rr.voltage,
                      "liberties": [str(p) for p in rr.liberties],
                      **({"corners": {c: [str(p) for p in ls]
                                      for c, ls in rr.corners.items()}}
                         if rr.corners else {})}
                  for n, rr in prof.rails.items()},
        "level_shifters": [str(p) for p in prof.level_shifters],
        **({"corners": {"names": prof.corner_names,
                        **prof.corner_roles}} if prof.corner_names else {}),
        **({"modes": {m.name: {
            "sdc": str(m.sdc) if m.sdc else None,
            "clocks": {c.name: {"period_ps": c.period_ps,
                                "ports": list(c.ports)}
                       for c in m.clocks.values()}}
            for m in d.effective_modes()}} if timing else {}),
        "netlist": str(cfg.netlist),
        "openroad_netlist": (str(cfg.openroad_netlist)
                             if cfg.openroad_netlist.exists() else None),
        "domains": records,
        "remainder": rec_rem,
    }
    js = out / "power_domains.json"
    js.write_text(json.dumps(summary, indent=2) + "\n")
    bad = [r["name"] for r in records + [rec_rem]
           if r["cells_not_in_rail_liberties"]]
    if bad:
        raise PowerError(f"power: {', '.join(bad)} use cells that are not in "
                         f"their rail's liberties; see {js}")
    print(f"[power] domains -> {js}")

    # 5. Power intent: crossings, UPF, per-mode SDCs, and (opt-in) level
    #    shifters in the OpenSTA netlist. Needs that netlist (sv2v).
    from . import intent
    opts = d.power_options or {}
    if cfg.openroad_netlist.exists():
        summary["intent"] = intent.run(
            cfg, prof, records,
            insert=opts.get("insert_level_shifters", False),
            ground=opts.get("ground", "VSS"))
        js.write_text(json.dumps(summary, indent=2) + "\n")
    elif opts.get("insert_level_shifters"):
        raise PowerError("power.insert_level_shifters needs the "
                         "OpenSTA-compatible netlist (sv2v)")
    else:
        print("[power] WARNING: no OpenSTA-compatible netlist (sv2v); no "
              "crossings, UPF or per-mode SDCs written")

    # 6. Fanout buffering, per module on its rail (the pack's
    #    flow.fanout_buffer; the domain sub-runs do not buffer).
    if d.fanout is not None and cfg.openroad_netlist.exists():
        from . import fanout
        summary["fanout"] = str(cfg.out_dir / "fanout.json")
        fanout.run_power(cfg, d.fanout, summary)
        js.write_text(json.dumps(summary, indent=2) + "\n")

    # 7. Threshold-voltage swap per domain (opt-in).
    from . import vtswap
    vspec = vtswap.wanted(cfg)
    if vspec is not None:
        if not cfg.openroad_netlist.exists():
            raise PowerError("vt_swap needs the OpenSTA-compatible netlist "
                             "(sv2v)")
        summary["vt_swap"] = str(cfg.out_dir / "vt_swap.json")
        vtswap.run_power(cfg, vspec, summary)
        js.write_text(json.dumps(summary, indent=2) + "\n")

    # 8. Multi-corner multi-mode STA (opt-in).
    from . import sta
    spec = sta.wanted(cfg)
    if spec is not None:
        sta.run(cfg, spec, summary)
    return js
