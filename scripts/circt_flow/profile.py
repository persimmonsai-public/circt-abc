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
`deny_exceptions:` and `public:`. A flow config names a
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from .orfs_pnr import DEFAULT_IMAGE, ORFS_ROOT

REPO_PROFILES = Path(__file__).resolve().parent.parent.parent / "profiles"
# The FlowConfig fields a pack's `flow:` section may default. Paths are
# relative to the pack. `env` holds defaults for the flow's CIRCT_FLOW_*
# pass knobs, applied only where the environment leaves them unset.
FLOW_DEFAULTS = {"sv2v_docker_image": str, "abc_script_file": Path,
                 "abc_read_lib_args": str, "enable_classical_resub": bool,
                 "partitions": int, "env": dict}
ENV_PREFIX = "CIRCT_FLOW_"


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.environ.get("CIRCT_ABC_CACHE", Path(base) / "circt-abc"))


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

    @property
    def synth_libs(self) -> List[Path]:
        """What the flow's pdk.synth_lib_tt gets: the merged liberty for an
        ORFS profile, the listed ones otherwise (the first is primary)."""
        return self.liberties


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
    every word has ${VAR} expanded), not both."""
    p = dict(d.get("pnr") or {})
    unknown = set(p) - {"orfs_platform_dir", "command"}
    if unknown:
        raise KeyError(f"{path}: pnr.{sorted(unknown)[0]} is not a pack "
                       "P&R key; allowed: orfs_platform_dir, command")
    if len(p) > 1:
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


def load(path: Path | str, image: Optional[str] = None) -> Profile:
    path = Path(path)
    if not path.exists() and "/" not in str(path):
        path = resolve(str(path))
    d = yaml.safe_load(path.read_text())
    pack = d.get("schema") is not None
    if pack and d["schema"] != 1:
        raise ValueError(f"{path}: unsupported techpack schema {d['schema']}")
    base = path.resolve().parent
    orfs = d.get("orfs")
    if orfs:
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
    flow = {}
    for k, v in (d.get("flow") or {}).items():
        if k not in FLOW_DEFAULTS:
            raise KeyError(f"{path}: flow.{k} is not a pack default; "
                           f"allowed: {', '.join(FLOW_DEFAULTS)}")
        if FLOW_DEFAULTS[k] is Path:
            v = expand(v, base)
        elif k == "env":
            bad = [e for e in v if not e.startswith(ENV_PREFIX)]
            if bad:
                raise KeyError(f"{path}: flow.env.{bad[0]} is not a "
                               f"{ENV_PREFIX}* pass knob")
            # The knobs test for "1"; a YAML boolean means the same.
            v = {e: str(int(x)) if isinstance(x, bool) else str(x)
                 for e, x in v.items()}
        flow[k] = v
    icg = dict(d["icg"])
    if "netlist" in icg:
        if not pack:
            raise KeyError(f"{path}: icg.netlist needs a tech pack")
        icg["netlist"] = expand(icg["netlist"], base)
    return Profile(name=d["name"], liberties=liberties,
                   time_unit=d.get("time_unit", "ns"), icg=icg,
                   dont_use=list(d.get("dont_use", [])),
                   orfs_platform=(orfs or {}).get("platform"),
                   tie=dict(d.get("tie") or {}),
                   root=base if pack else None, flow=flow,
                   pnr=_pnr_section(d, base, path) if pack else {},
                   deny_terms=list(d.get("deny_terms", [])),
                   deny_exceptions=list(d.get("deny_exceptions", [])),
                   public=bool(d.get("public", False)))


if __name__ == "__main__":
    p = load(sys.argv[1])
    for k, v in p.__dict__.items():
        print(f"{k}: {v}")
