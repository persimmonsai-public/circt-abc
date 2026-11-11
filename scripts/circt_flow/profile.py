"""Library profiles: what the flow needs to know about a cell library.

A profile (profiles/<name>.yaml) names the liberties, the library's time
unit, the integrated clock gate a design's clock-gating primitive maps to,
and the platform's dont_use patterns. Liberties come either from explicit
paths (`liberties:`) or from an ORFS image's platform directory
(`orfs: {platform, liberties}`), in which case they are extracted and
decompressed into a cache on first use and merged into one file, since the
mapper's passes take a single liberty.

A tech pack (doc/techpacks.md) is a profile kept outside the repo, as
<pack>/techpack.yaml with `schema: 1`: its paths are relative to the pack,
`${VAR}` and `${VAR:-default}` expand from the environment, and it may add
`flow:` defaults (FLOW_DEFAULTS), a `pnr:` section (a pack-local ORFS
platform, or a P&R command of its own) and the leak audit's `deny_terms:`,
`deny_exceptions:` and `public:`.

Schema 2 adds supply rails, for designs whose power domains sit on
different supplies (doc/techpacks.md, "Rails"):

    schema: 2
    default_rail: VDD
    rails:
      VDD:    {voltage: 0.75, liberties: [...], flow: {...}}
      VDD_LV: {voltage: 0.55, liberties: [...]}
    level_shifters: {liberties: [...], corners: {tt: [...], ss: [...]}}

Each rail has its own liberties (the first is its mapping-primary one) and
may override the pack's `flow:` defaults and its `icg`, `tie` and
`dont_use`. A schema-2 profile's `liberties`, `flow`, `icg`, `tie` and
`dont_use` are its default rail's, so every single-rail caller keeps
working; `for_rail()` gives the same view of another rail.

Corners (schema 2, optional): a profile-level block names the corners and
their roles, and each rail lists its liberties per corner:

    corners:
      names: [tt, ss, ff]
      setup: ss        # the mapping-delay corner: its timing drives mapping
      leakage: ff      # the corner whose leakage the mapping library prices
    rails:
      VDD: {voltage: 0.75, liberties: [priced.lib, ...],   # mapping list
            corners: {tt: [...], ss: [...], ff: [...]}}

A rail's `liberties:` stays what the mapper reads; without it, it is the
setup corner's list. Every rail must list every corner and nothing else.
A pack without `corners:` has one implicit corner, the rail's liberties.
A schema-1 pack
or a repo profile is one implicit rail, IMPLICIT_RAIL. A flow config names a
profile either by path or by name; resolve() finds a name in the packs
enclosing the flow config, then on CIRCT_ABC_TECH_PATH, then as a
<name>-techpack checkout beside the repo, then in profiles/.

    python3 -m circt_flow.profile <profile.yaml|name>    # prepare and print it
"""
from __future__ import annotations

import gzip
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .orfs_pnr import DEFAULT_IMAGE, ORFS_ROOT

REPO_PROFILES = Path(__file__).resolve().parent.parent.parent / "profiles"
# The FlowConfig fields a pack's `flow:` section may default. Paths are
# relative to the pack. `env` holds defaults for the flow's CIRCT_FLOW_*
# pass knobs, applied only where the environment leaves them unset.
FLOW_DEFAULTS = {"sv2v_docker_image": str, "abc_script_file": Path,
                 "abc_read_lib_args": str, "enable_classical_resub": bool,
                 "partitions": int, "env": dict,
                 # The mapping target's calibration (modes.parse_target
                 # keys): how optimistic ABC's delay model is on this
                 # library is a property of the library, so a pack (or one
                 # of its rails) may set the scale; a flow config's own
                 # mapping_target: replaces it.
                 "mapping_target": dict,
                 # Pre-placement fanout buffering (fanout.py): the buffer
                 # cell, its pins and the fanout limit.
                 "fanout_buffer": dict,
                 # Slow-cell filter for ABC's library (slowcells.py): cells
                 # whose liberty delay at a reference slew/load is more than
                 # `ratio` x the median are passed to read_lib as -X.
                 "slow_cell_filter": dict,
                 # The multibit flop families (multibit.py); a flow config's
                 # `multibit:` turns the mapping on.
                 "multibit": dict,
                 # A pre-placement wire model for the STA and vt_swap
                 # stages (wire_model.py).
                 "wire_model": dict,
                 # Clock-gate economy numbers (clockgating.py); a flow
                 # config's `clock_gating:` turns them on.
                 "clock_gating": dict}
ENV_PREFIX = "CIRCT_FLOW_"
# The one rail of a schema-1 pack or a repo profile.
IMPLICIT_RAIL = "default"
# What a schema-2 rail may set besides its voltage and liberties.
RAIL_KEYS = {"voltage", "liberties", "flow", "icg", "tie", "dont_use",
             "corners", "vt_flavours"}
# The profile-level corners: block.
CORNER_KEYS = {"names", "setup", "leakage", "hold"}
CORNER_ROLES = ("setup", "leakage")  # required; `hold` is optional


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.environ.get("CIRCT_ABC_CACHE", Path(base) / "circt-abc"))


@dataclass(frozen=True)
class Rail:
    """One supply rail: its voltage (None for an implicit rail), its
    liberties (the first is mapping-primary), and the pack's `flow:`,
    `icg`, `tie` and `dont_use` with the rail's own overrides applied."""
    name: str
    voltage: Optional[float]
    liberties: List[Path]
    flow: dict = field(default_factory=dict)
    icg: dict = field(default_factory=dict)
    tie: dict = field(default_factory=dict)
    dont_use: List[str] = field(default_factory=list)
    # {corner: liberties}, every corner of the profile; empty without
    # corners (the rail's one implicit corner is `liberties`).
    corners: Dict[str, List[Path]] = field(default_factory=dict)
    # The rail's own `vt_flavours:` (None: the pack's).
    vt_flavours: Optional[dict] = None

    def libs_at(self, corner: Optional[str]) -> List[Path]:
        """The rail's liberties at `corner` (its mapping liberties when the
        profile has no corners or `corner` is None)."""
        if not self.corners or corner is None:
            return list(self.liberties)
        if corner not in self.corners:
            raise KeyError(f"rail {self.name}: no corner {corner!r}; "
                           "corners: " + ", ".join(self.corners))
        return list(self.corners[corner])


@dataclass(frozen=True)
class Profile:
    name: str
    liberties: List[Path]
    time_unit: str
    # {"cell", "clk", "en", "test_en", "out"}: the integrated clock gate a
    # design's clock-gating primitive maps to. With "netlist" (a pack path),
    # "cell" is a module of that structural Verilog file built from the
    # library's own cells, for a library without an ICG cell.
    icg: dict
    dont_use: List[str] = field(default_factory=list)
    orfs_platform: Optional[str] = None
    # {"hi": "CELL:PIN", "lo": "CELL:PIN"}, for P&R's constant ties.
    tie: dict = field(default_factory=dict)
    # Tech packs only: the pack directory, its `flow:` defaults (paths
    # resolved), its `pnr:` section (orfs_platform_dir resolved, command
    # expanded) and the leak audit's inputs: the extra strings it refuses,
    # the names it lets through, and whether the pack is public (a public
    # pack's names are not refused at all).
    root: Optional[Path] = None
    flow: dict = field(default_factory=dict)
    pnr: dict = field(default_factory=dict)
    deny_terms: List[str] = field(default_factory=list)
    deny_exceptions: List[str] = field(default_factory=list)
    public: bool = False
    # The pack's schema (1 for a repo profile), its rails by name, the rail
    # the single-rail fields above describe (`rail`), the rail undeclared
    # logic sits on (`default_rail`), and the level-shifter liberties.
    schema: int = 1
    rails: Dict[str, Rail] = field(default_factory=dict)
    rail: str = IMPLICIT_RAIL
    default_rail: str = IMPLICIT_RAIL
    level_shifters: List[Path] = field(default_factory=list)
    # `level_shifters.corners`: the shifter liberties per corner; a corner
    # not listed reads `level_shifters` (see level_shifters_at).
    level_shifter_corners: Dict[str, List[Path]] = field(default_factory=dict)
    # `vt_flavours: {tokens: [slowest, ..., fastest], re: <regex>}`: how a
    # cell name carries its threshold flavour (vtswap.py); None without.
    vt_flavours: Optional[dict] = None
    # Schema 2 `corners:`: the corner names in order, and {role: corner}
    # for the roles in CORNER_ROLES. Both empty without corners.
    corner_names: List[str] = field(default_factory=list)
    corner_roles: Dict[str, str] = field(default_factory=dict)
    # `wire_rc:` (wire_rc.WireRc): the per-corner wire RC Tcl the P&R, STA
    # and vt_swap stages use; None without one.
    wire_rc: Optional[object] = None

    @property
    def setup_corner(self) -> Optional[str]:
        return self.corner_roles.get("setup")

    @property
    def leakage_corner(self) -> Optional[str]:
        return self.corner_roles.get("leakage")

    @property
    def hold_corner(self) -> Optional[str]:
        """The corner hold is checked at: `corners.hold`, else the setup
        corner (None without corners)."""
        return self.corner_roles.get("hold", self.setup_corner)

    def libs_at(self, corner: Optional[str] = None,
                rail: Optional[str] = None) -> List[Path]:
        """Rail `rail`'s (default: this view's rail) liberties at `corner`
        (default: the setup corner; the mapping liberties without
        corners)."""
        r = self.rails[rail or self.rail]
        return r.libs_at(corner if corner is not None else self.setup_corner)

    def level_shifters_at(self, corner: Optional[str] = None) -> List[Path]:
        """The level-shifter liberties at `corner` (default: the setup
        corner): `level_shifters.corners[corner]`, else the pack's
        `level_shifters.liberties`."""
        c = corner if corner is not None else self.setup_corner
        return list(self.level_shifter_corners.get(c, self.level_shifters))

    def flavours_at(self, rail: Optional[str] = None) -> Optional[dict]:
        """Rail `rail`'s (default: this view's rail) `vt_flavours`: its
        own, else the pack's; None without either."""
        r = self.rails.get(rail or self.rail)
        return (r.vt_flavours if r and r.vt_flavours else self.vt_flavours)

    @property
    def synth_libs(self) -> List[Path]:
        """What the flow's pdk.synth_lib_tt gets: the merged liberty for an
        ORFS profile, the listed ones otherwise (the first is primary)."""
        return self.liberties

    @property
    def voltage(self) -> Optional[float]:
        r = self.rails.get(self.rail)
        return r.voltage if r else None

    def all_liberties(self) -> List[Path]:
        """Every rail's liberties and the level shifters', once each."""
        seen: Dict[Path, None] = {}
        for libs in [*(r.liberties for r in self.rails.values()),
                     *(c for r in self.rails.values()
                       for c in r.corners.values()),
                     self.liberties, self.level_shifters,
                     *self.level_shifter_corners.values()]:
            for p in libs:
                seen.setdefault(p, None)
        return list(seen)

    def for_rail(self, name: str) -> "Profile":
        """This profile as seen from rail `name`: its liberties, flow
        defaults, ICG, tie cells and dont_use are that rail's."""
        if name not in self.rails:
            raise KeyError(f"profile {self.name}: no rail {name!r}; rails: "
                           + ", ".join(self.rails))
        r = self.rails[name]
        return replace(self, liberties=list(r.liberties), flow=dict(r.flow),
                       icg=dict(r.icg), tie=dict(r.tie),
                       dont_use=list(r.dont_use), rail=name)


def _block_end(text: str, open_brace: int) -> int:
    """Index just past the `}` matching the `{` at open_brace."""
    depth = 0
    for i in range(open_brace, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced braces in liberty")


_GROUP = re.compile(r'^[ \t]*(\w+)\s*\(\s*"?([^")]*?)"?\s*\)\s*\{', re.M)


def _split_library(text: str):
    """(header, [(kind, name, group_text)], cells_text) of one liberty:
    the library-level groups before the first cell, and every cell group."""
    lib = re.search(r'\blibrary\s*\([^)]*\)\s*\{', text)
    if not lib:
        raise ValueError("no library group")
    end = _block_end(text, lib.end() - 1)
    body_start, body_end = lib.end(), end - 1
    groups, cells = [], []
    pos = body_start
    while True:
        m = _GROUP.search(text, pos, body_end)
        if not m:
            break
        g_end = _block_end(text, m.end() - 1)
        if m.group(1) == "cell":
            cells.append(text[m.start():g_end])
        else:
            groups.append((m.group(1), m.group(2), text[m.start():g_end]))
        pos = g_end
    first_cell = re.search(r'^[ \t]*cell\s*\(', text[body_start:body_end], re.M)
    header = text[:body_start + (first_cell.start() if first_cell else 0)]
    return header, groups, cells


def merge_liberties(paths: List[Path], out: Path) -> None:
    """One liberty with the first file's library header and attributes, the
    other files' table templates it lacks, and every file's cells."""
    texts = [p.read_text(errors="ignore") for p in paths]
    header, groups, cells = _split_library(texts[0])
    seen = {(k, n) for k, n, _ in groups}
    extra = []
    for t in texts[1:]:
        _, g, c = _split_library(t)
        for k, n, body in g:
            if (k, n) not in seen:
                seen.add((k, n))
                extra.append(body)
        cells += c
    out.write_text(header.rstrip() + "\n"
                   + "".join("\n" + e for e in extra)
                   + "".join("\n" + c for c in cells) + "\n}\n")


def _extract_orfs(image: str, platform: str, rel: List[str],
                  dest: Path) -> List[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    want = [dest / Path(r).name.removesuffix(".gz") for r in rel]
    if all(p.exists() for p in want):
        return want
    # In place under ORFS_HOME (the circt-abc image), else from the image.
    local = os.environ.get("ORFS_HOME")
    src = f"{local or ORFS_ROOT}/flow/platforms/{platform}"
    copy = " && ".join(f"cp {src}/{r} {dest}/" for r in rel)
    if local:
        subprocess.run(["sh", "-c", copy], check=True)
    else:
        subprocess.run(
            ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
             "-v", f"{dest}:{dest}", "--entrypoint", "sh", image, "-c",
             copy],
            check=True)
    for r, p in zip(rel, want):
        gz = dest / Path(r).name
        if gz.suffix == ".gz":
            p.write_bytes(gzip.decompress(gz.read_bytes()))
            gz.unlink()
    return want


_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand_vars(value: str) -> str:
    """${VAR} / ${VAR:-default} from the environment. An unset ${VAR} with
    no default is an error, not an empty string."""
    def sub(m):
        v = os.environ.get(m.group(1)) or m.group(2)
        if v is None:
            raise KeyError(f"{m.group(1)} is not set (needed by {value})")
        return v
    return _VAR.sub(sub, value)


def expand(value: str, base: Path) -> Path:
    """expand_vars, then relative to base."""
    return base / expand_vars(value)


def _pnr_section(d: dict, base: Path, path: Path) -> dict:
    """A pack's `pnr:`: orfs_platform_dir (a directory, relative to the
    pack) or command (argv; `./` and `../` words are relative to the pack,
    every word has ${VAR} expanded), not both. A pack may also give defaults
    for the ORFS stage's clock-tree and margin controls (`cts_buffers`,
    `cts_cluster_size`, `cts_cluster_diameter`, `cts_args`, `hold_margin`,
    `setup_margin`); a flow config's own `pnr:` value wins."""
    p = dict(d.get("pnr") or {})
    ctl = {"cts_buffers", "cts_cluster_size", "cts_cluster_diameter",
           "cts_args", "hold_margin", "setup_margin"}
    unknown = set(p) - {"orfs_platform_dir", "command"} - ctl
    if unknown:
        raise KeyError(f"{path}: pnr.{sorted(unknown)[0]} is not a pack "
                       "P&R key; allowed: orfs_platform_dir, command, "
                       + ", ".join(sorted(ctl)))
    if len([k for k in p if k not in ctl]) > 1:
        raise ValueError(f"{path}: pnr: give orfs_platform_dir or command, "
                         "not both")
    if "orfs_platform_dir" in p:
        p["orfs_platform_dir"] = expand(p["orfs_platform_dir"], base)
    if "command" in p:
        cmd = p["command"]
        if isinstance(cmd, str) or not cmd:
            raise ValueError(f"{path}: pnr.command must be a non-empty list")
        p["command"] = [str(expand(w, base)) if w.startswith(("./", "../"))
                        else expand_vars(str(w)) for w in cmd]
    return p


def resolve(ref: str, config_dir: Optional[Path] = None) -> Path:
    """A profile reference from a flow config: a path (has a `/` or ends in
    .yaml, relative to config_dir), or a name, looked up as the enclosing
    pack of config_dir, then <dir>/<name>/techpack.yaml on
    CIRCT_ABC_TECH_PATH, then a <name>-techpack or circt-abc-techpack-<name>
    checkout beside this repo, then profiles/<name>.yaml in this repo."""
    config_dir = config_dir or Path.cwd()
    if "/" in ref or ref.endswith((".yaml", ".yml")):
        return (config_dir / ref).resolve()
    tried = []
    for d in [config_dir, *config_dir.parents]:
        pack = d / "techpack.yaml"
        if pack.exists() and (yaml.safe_load(pack.read_text()) or {}).get(
                "name") == ref:
            return pack
    for d in filter(None, os.environ.get("CIRCT_ABC_TECH_PATH", "").split(":")):
        pack = Path(d) / ref / "techpack.yaml"
        tried.append(str(pack))
        try:
            found = pack.is_file()
        except OSError:  # an unreadable path entry is skipped, not fatal
            found = False
        if found:
            return pack.resolve()
    for sib in (f"{ref}-techpack", f"circt-abc-techpack-{ref}"):
        sibling = REPO_PROFILES.parent.parent / sib / "techpack.yaml"
        tried.append(str(sibling))
        if sibling.is_file():
            return sibling
    repo = REPO_PROFILES / f"{ref}.yaml"
    tried.append(str(repo))
    if repo.exists():
        return repo
    raise FileNotFoundError(f"profile {ref!r} not found; tried the packs "
                            f"enclosing {config_dir}, " + ", ".join(tried))


def _flow_section(flow_d: dict, base: Path, where: str,
                  pack_flow: Optional[dict] = None) -> dict:
    """A `flow:` mapping, checked against FLOW_DEFAULTS, paths resolved. A
    rail's passes the pack's `flow:` as `pack_flow`, whose fanout_buffer its
    own overrides key by key."""
    flow = {}
    for k, v in (flow_d or {}).items():
        if k not in FLOW_DEFAULTS:
            raise KeyError(f"{where}.{k} is not a pack default; "
                           f"allowed: {', '.join(FLOW_DEFAULTS)}")
        if FLOW_DEFAULTS[k] is Path:
            v = expand(v, base)
        elif k == "mapping_target":
            from .modes import parse_target
            parse_target(v, where)  # checked here, parsed where used
        elif k == "fanout_buffer":
            from .fanout import parse_spec
            parse_spec((pack_flow or {}).get("fanout_buffer"), v, where)
        elif k == "multibit":
            from .multibit import parse_pack
            parse_pack(v, f"{where}.multibit")
        elif k == "wire_model":
            from .wire_model import parse_pack as parse_wire
            v = parse_wire(v, f"{where}.wire_model")
        elif k == "clock_gating":
            from .clockgating import parse_pack as parse_cg
            parse_cg(v, f"{where}.clock_gating")
        elif k == "env":
            bad = [e for e in v if not e.startswith(ENV_PREFIX)]
            if bad:
                raise KeyError(f"{where}.env.{bad[0]} is not a "
                               f"{ENV_PREFIX}* pass knob")
            # The knobs test for "1"; a YAML boolean means the same.
            v = {e: str(int(x)) if isinstance(x, bool) else str(x)
                 for e, x in v.items()}
        flow[k] = v
    return flow


def _icg_section(icg_d: dict, base: Path, pack: bool, where: str) -> dict:
    icg = dict(icg_d)
    if "netlist" in icg:
        if not pack:
            raise KeyError(f"{where}.netlist needs a tech pack")
        icg["netlist"] = expand(icg["netlist"], base)
    return icg


def _corners_section(d: dict, path: Path) -> Tuple[List[str], Dict[str, str]]:
    """A schema-2 pack's `corners:` block: (names, {role: corner})."""
    c = d.get("corners")
    if c is None:
        return [], {}
    where = f"{path}: corners"
    if not isinstance(c, dict):
        raise ValueError(f"{where} must be a mapping {{names, setup, "
                         "leakage[, hold]}")
    unknown = set(c) - CORNER_KEYS
    if unknown:
        raise KeyError(f"{where}.{sorted(unknown)[0]} is not a corners key; "
                       f"allowed: {', '.join(sorted(CORNER_KEYS))}")
    names = c.get("names")
    if (not isinstance(names, list) or not names
            or not all(isinstance(n, str) and n for n in names)):
        raise ValueError(f"{where}.names must be a non-empty list of corner "
                         "names")
    if len(set(names)) != len(names):
        raise ValueError(f"{where}.names lists a corner twice")
    roles = {}
    for role in CORNER_ROLES:
        if role not in c:
            raise KeyError(f"{where}.{role} is required (the {role} corner)")
        if c[role] not in names:
            raise KeyError(f"{where}.{role}: {c[role]!r} is not one of "
                           f"corners.names ({', '.join(names)})")
        roles[role] = c[role]
    if "hold" in c:
        if c["hold"] not in names:
            raise KeyError(f"{where}.hold: {c['hold']!r} is not one of "
                           f"corners.names ({', '.join(names)})")
        roles["hold"] = c["hold"]
    return list(names), roles


def _rails_section(d: dict, base: Path, path: Path, flow: dict, icg: dict,
                   tie: dict, dont_use: List[str],
                   corner_names: List[str] = (),
                   setup: Optional[str] = None) -> Dict[str, Rail]:
    """A schema-2 pack's `rails:`, each rail's overrides applied over the
    pack-level `flow`, `icg`, `tie` and `dont_use`; with corners, each
    rail's per-corner liberties (its `liberties:` default to the setup
    corner's)."""
    rails_d = d.get("rails")
    if not isinstance(rails_d, dict) or not rails_d:
        raise ValueError(f"{path}: schema 2 needs a non-empty `rails:` "
                         "mapping (rail name -> {voltage, liberties})")
    if "liberties" in d:
        raise ValueError(f"{path}: schema 2 lists liberties per rail "
                         "(rails.<name>.liberties), not at the top level")
    rails: Dict[str, Rail] = {}
    for name, r in rails_d.items():
        where = f"{path}: rails.{name}"
        if not isinstance(r, dict):
            raise ValueError(f"{where} must be a mapping")
        unknown = set(r) - RAIL_KEYS
        if unknown:
            raise KeyError(f"{where}.{sorted(unknown)[0]} is not a rail key; "
                           f"allowed: {', '.join(sorted(RAIL_KEYS))}")
        if "voltage" not in r:
            raise KeyError(f"{where}: voltage is required")
        try:
            voltage = float(r["voltage"])
        except (TypeError, ValueError):
            raise ValueError(f"{where}.voltage: {r['voltage']!r} is not a "
                             "number (volts)") from None
        corners: Dict[str, List[Path]] = {}
        if "corners" in r:
            if not corner_names:
                raise KeyError(f"{where}.corners needs the pack-level "
                               "`corners:` block (names, setup, leakage)")
            rc = r["corners"]
            if not isinstance(rc, dict):
                raise ValueError(f"{where}.corners must map each corner to "
                                 "its liberties")
            missing = [c for c in corner_names if c not in rc]
            extra = sorted(set(rc) - set(corner_names))
            if missing or extra:
                raise KeyError(
                    f"{where}.corners: "
                    + (f"missing corner(s) {', '.join(missing)}" if missing
                       else f"unknown corner(s) {', '.join(extra)}")
                    + f"; corners.names: {', '.join(corner_names)}")
            for c in corner_names:
                cl = rc[c]
                if not cl or isinstance(cl, str) or not isinstance(cl, list):
                    raise ValueError(f"{where}.corners.{c} must be a "
                                     "non-empty list of liberties")
                corners[c] = [expand(p, base) for p in cl]
        elif corner_names:
            raise KeyError(f"{where}: the pack has corners "
                           f"({', '.join(corner_names)}); give the rail's "
                           "liberties per corner (rails.<name>.corners)")
        libs = r.get("liberties")
        if libs is None and corners:
            libs = corners[setup]
            libs_paths = list(libs)
        else:
            libs_paths = None
        if not libs or isinstance(libs, str):
            raise ValueError(f"{where}.liberties must be a non-empty list "
                             "(the first is the rail's mapping-primary one)")
        from .vtswap import parse_flavours
        rflow = _flow_section(r.get("flow"), base, f"{where}.flow", flow)
        if isinstance(flow.get("fanout_buffer"), dict) and \
                isinstance(rflow.get("fanout_buffer"), dict):
            # The rail's buffer overrides the pack's key by key.
            rflow["fanout_buffer"] = {**flow["fanout_buffer"],
                                      **rflow["fanout_buffer"]}
        rails[str(name)] = Rail(
            name=str(name), voltage=voltage,
            liberties=(libs_paths if libs_paths is not None
                       else [expand(p, base) for p in libs]),
            flow={**flow, **rflow},
            vt_flavours=parse_flavours(r.get("vt_flavours"), where),
            icg=(_icg_section(r["icg"], base, True, f"{where}.icg")
                 if "icg" in r else dict(icg)),
            tie=dict(r["tie"]) if "tie" in r else dict(tie),
            dont_use=(list(r["dont_use"]) if "dont_use" in r
                      else list(dont_use)),
            corners=corners)
    return rails


def _level_shifters(d: dict, base: Path, path: Path,
                    corner_names: List[str] = ()):
    """`level_shifters: {liberties: [...], corners: {<corner>: [...]}}` (or
    the bare list): (the default liberties, {corner: liberties}). A corner
    not listed falls back to the default liberties; `corners` needs the
    pack's `corners:` block and only its names. Without `liberties`, every
    corner must be listed."""
    ls = d.get("level_shifters")
    if ls is None:
        return [], {}
    per: Dict[str, List[Path]] = {}
    if isinstance(ls, dict):
        unknown = set(ls) - {"liberties", "corners"}
        if unknown:
            raise KeyError(f"{path}: level_shifters.{sorted(unknown)[0]} is "
                           "not a level-shifter key; allowed: liberties, "
                           "corners")
        lc = ls.get("corners")
        if lc is not None:
            where = f"{path}: level_shifters.corners"
            if not corner_names:
                raise KeyError(f"{where} needs the pack-level `corners:` "
                               "block (names, setup, leakage)")
            if not isinstance(lc, dict) or not lc:
                raise ValueError(f"{where} must map corners to liberties")
            extra = sorted(set(lc) - set(corner_names))
            if extra:
                raise KeyError(f"{where}: unknown corner(s) "
                               f"{', '.join(extra)}; corners.names: "
                               f"{', '.join(corner_names)}")
            for c, cl in lc.items():
                if not cl or isinstance(cl, str) or not isinstance(cl, list):
                    raise ValueError(f"{where}.{c} must be a non-empty list "
                                     "of liberties")
                per[c] = [expand(p, base) for p in cl]
        ls = ls.get("liberties") or []
        miss = [c for c in corner_names if c not in per]
        if not ls and per and miss:
            raise ValueError(f"{path}: level_shifters has no `liberties` "
                             "default and no libraries for corner(s) "
                             f"{', '.join(miss)}")
    if isinstance(ls, str) or not isinstance(ls, list):
        raise ValueError(f"{path}: level_shifters.liberties must be a list")
    return [expand(p, base) for p in ls], per


def _vt_flavours(d: dict, path: Path) -> Optional[dict]:
    from .vtswap import parse_flavours
    return parse_flavours(d.get("vt_flavours"), f"{path}")


def load(path: Path | str, image: Optional[str] = None,
         audit: bool = False) -> Profile:
    path = Path(path)
    if not path.exists() and "/" not in str(path):
        path = resolve(str(path))
    d = yaml.safe_load(path.read_text())
    pack = d.get("schema") is not None
    schema = d["schema"] if pack else 1
    if pack and schema not in (1, 2):
        raise ValueError(f"{path}: unsupported techpack schema {d['schema']}")
    if schema == 1:
        for k in ("rails", "default_rail", "level_shifters", "corners"):
            if k in d:
                raise KeyError(f"{path}: {k} needs `schema: 2`")
    base = path.resolve().parent
    flow = _flow_section(d.get("flow"), base, f"{path}: flow")
    icg = _icg_section(d["icg"], base, pack, f"{path}: icg")
    tie = dict(d.get("tie") or {})
    dont_use = list(d.get("dont_use", []))
    orfs = d.get("orfs")
    corner_names: List[str] = []
    corner_roles: Dict[str, str] = {}
    if schema == 2:
        if orfs:
            raise ValueError(f"{path}: schema 2 takes liberties per rail; "
                             "`orfs:` is schema 1 only")
        corner_names, corner_roles = _corners_section(d, path)
        rails = _rails_section(d, base, path, flow, icg, tie, dont_use,
                               corner_names, corner_roles.get("setup"))
        default = d.get("default_rail")
        if default is None:
            if len(rails) != 1:
                raise KeyError(f"{path}: default_rail is required with more "
                               "than one rail")
            default = next(iter(rails))
        if default not in rails:
            raise KeyError(f"{path}: default_rail {default!r} is not a rail; "
                           "rails: " + ", ".join(rails))
        r = rails[default]
        liberties, flow, icg = r.liberties, r.flow, r.icg
        tie, dont_use = r.tie, r.dont_use
    elif orfs:
        image = image or os.environ.get("ORFS_IMAGE", DEFAULT_IMAGE)
        tag = image.rsplit(":", 1)[-1]
        dest = cache_root() / "orfs" / tag / orfs["platform"]
        libs = _extract_orfs(image, orfs["platform"], orfs["liberties"], dest)
        if pack and not d.get("public"):
            dest.chmod(0o700)  # derived liberties are the pack's data too
        merged = dest / f"{d['name']}_merged.lib"
        if not merged.exists():
            merge_liberties(libs, merged)
        liberties = [merged]
    elif pack:
        liberties = [expand(p, base) for p in d["liberties"]]
    else:
        liberties = [Path(os.path.expandvars(p)) for p in d["liberties"]]
    if schema == 1:
        default = IMPLICIT_RAIL
        rails = {default: Rail(name=default, voltage=None,
                               liberties=list(liberties), flow=dict(flow),
                               icg=dict(icg), tie=dict(tie),
                               dont_use=list(dont_use))}
    ls_libs, ls_corners = _level_shifters(d, base, path, corner_names)
    from . import wire_rc as wire_rc_mod
    if "wire_rc" in d and not pack:
        raise KeyError(f"{path}: wire_rc is a tech-pack key (schema: 1|2)")
    wire_rc = wire_rc_mod.parse(d.get("wire_rc"), base, corner_names,
                                bool(d.get("public", False)),
                                f"{path}: wire_rc")
    return Profile(name=d["name"], liberties=list(liberties),
                   time_unit=d.get("time_unit", "ns"), icg=dict(icg),
                   dont_use=list(dont_use),
                   orfs_platform=(orfs or {}).get("platform"),
                   tie=dict(tie),
                   root=base if pack else None, flow=dict(flow),
                   pnr=_pnr_section(d, base, path) if pack and not audit else {},
                   deny_terms=list(d.get("deny_terms", [])),
                   deny_exceptions=list(d.get("deny_exceptions", [])),
                   public=bool(d.get("public", False)),
                   schema=schema, rails=rails, rail=default,
                   default_rail=default,
                   level_shifters=ls_libs, level_shifter_corners=ls_corners,
                   corner_names=corner_names, corner_roles=corner_roles,
                   vt_flavours=_vt_flavours(d, path), wire_rc=wire_rc)


if __name__ == "__main__":
    p = load(sys.argv[1])
    for k, v in p.__dict__.items():
        print(f"{k}: {v}")
