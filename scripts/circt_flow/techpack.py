"""Tech packs: a library profile kept outside the repo (doc/techpacks.md).

    python3 -m circt_flow.techpack check <name|path>
    python3 -m circt_flow.techpack root <name|path>   # print the pack dir
    python3 -m circt_flow.techpack list               # the installed packs
    python3 -m circt_flow.techpack audit [paths...]   # the leak guard

`check` resolves the pack the way a flow config would (profile.resolve),
loads it, and verifies that every liberty and `flow:` and `pnr:` path
exists and that the ICG and tie cells it names (for a composite ICG, the
cells its netlist instantiates) are cells of its liberties; tie cells may
instead be cells of the pack's ORFS platform (`lib/*.lib`), since only P&R
uses them.
Exit status 1 on any problem.

`audit` refuses the names of every installed private pack: the packs on
CIRCT_ABC_TECH_PATH and the tech-pack checkouts beside this repo, except
those that say `public: true`. A pack's names are its liberties' cell and
library names of MIN_NAME characters or more, matched case-sensitively,
its ICG and tie cells, and its `deny_terms`, matched without regard to
case; `deny_exceptions` are let through. The list is built at run time and
never written down. With paths it scans those files (directories
recursively; inside a work tree, the files git does not ignore); with
none, the lines and file names the staged diff adds, which is what the
pre-commit hook runs. Exit status 1 on any hit.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from . import profile as profile_mod

# Shorter cell names (INV, ND2, ...) are ordinary words; a library's
# distinctive names are longer.
MIN_NAME = 6


def liberty_cells(paths) -> set[str]:
    cell = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)', re.M)
    names: set[str] = set()
    for lib in paths:
        with open(lib, errors="ignore") as f:
            for chunk in iter(lambda: f.read(1 << 24), ""):
                names.update(cell.findall(chunk))
    return names


def check(ref: str) -> list[str]:
    path = profile_mod.resolve(ref)
    print(f"{ref}: {path}")
    prof = profile_mod.load(path)
    problems = [f"liberty {p} does not exist" for p in prof.liberties
                if not p.exists()]
    problems += [f"flow.{k}: {v} does not exist" for k, v in prof.flow.items()
                 if isinstance(v, Path) and not v.exists()]
    pdir = prof.pnr.get("orfs_platform_dir")
    if pdir is not None and not (pdir / "config.mk").is_file():
        problems.append(f"pnr.orfs_platform_dir: {pdir} has no config.mk")
    cmd = prof.pnr.get("command")
    if cmd and "/" in cmd[0] and not os.access(cmd[0], os.X_OK):
        problems.append(f"pnr.command: {cmd[0]} is not executable")
    if not problems:
        cells = liberty_cells(prof.liberties)
        net = prof.icg.get("netlist")
        if net is None:
            want = [("icg", prof.icg["cell"])]
        elif not Path(net).is_file():
            want = []
            problems.append(f"icg.netlist: {net} does not exist")
        else:
            text = Path(net).read_text()
            if not re.search(r"\bmodule\s+" + re.escape(prof.icg["cell"])
                             + r"\b", text):
                problems.append(f"icg.netlist: {net} does not define "
                                f"module {prof.icg['cell']}")
            want = [("icg.netlist", c) for c in sorted(
                set(re.findall(r"^\s*([A-Za-z_]\w*)\s+[A-Za-z_]\w*\s*\(",
                               text, re.M)) - {"module"})]
        problems += [f"{what} cell {c} is not in the liberties"
                     for what, c in want if c not in cells]
        print(f"  {len(prof.liberties)} liberties, {len(cells)} cells")
        # Tie cells are P&R's: a pack's own ORFS platform may supply them.
        if pdir is not None:
            cells |= liberty_cells(sorted(pdir.glob("lib/*.lib")))
        problems += [f"tie.{k} cell {v.split(':')[0]} is not in the "
                     "liberties" + (" or the ORFS platform's" if pdir else "")
                     for k, v in prof.tie.items()
                     if v.split(":")[0] not in cells]
    print(f"  pack root: {prof.root or '(repo profile)'}")
    for k, v in prof.flow.items():
        print(f"  flow.{k}: {v}")
    for k, v in prof.pnr.items():
        print(f"  pnr.{k}: {v}")
    for p in problems:
        print(f"  ERROR: {p}")
    return problems


def installed_packs() -> list[Path]:
    """Every pack's techpack.yaml on CIRCT_ABC_TECH_PATH (<dir>/<name>/)
    and beside this repo (*-techpack, circt-abc-techpack-*), once each."""
    found: dict[Path, None] = {}
    dirs = [Path(d) for d in
            os.environ.get("CIRCT_ABC_TECH_PATH", "").split(":") if d]
    for d in dirs:
        try:
            found.update((p.resolve(), None) for p in
                         sorted(d.glob("*/techpack.yaml")))
        except OSError:  # an unreadable entry, as in profile.resolve
            pass
    beside = profile_mod.REPO_PROFILES.parent.parent
    for pat in ("*-techpack/techpack.yaml", "circt-abc-techpack-*/techpack.yaml"):
        found.update((p.resolve(), None) for p in sorted(beside.glob(pat)))
    return list(found)


def denylist(pack: Path) -> tuple[set[str], set[str]]:
    """(case-sensitive names, case-insensitive terms) a private pack's
    audit refuses; both empty for a public pack."""
    d = yaml.safe_load(pack.read_text()) or {}
    if d.get("public"):
        return set(), set()
    prof = profile_mod.load(pack)
    names = {prof.icg["cell"], *(v.split(":")[0] for v in prof.tie.values())}
    libs = [p for p in prof.liberties if p.exists()]
    if len(libs) < len(prof.liberties):
        print(f"techpack audit: WARNING: {prof.name}: "
              f"{len(prof.liberties) - len(libs)} liberties not installed; "
              "auditing their names from the pack alone", file=sys.stderr)
    names |= liberty_cells(libs) | liberty_names(libs)
    names = {n for n in names if len(n) >= MIN_NAME}
    allow = set(prof.deny_exceptions)
    terms = {t.lower() for t in prof.deny_terms} - {a.lower() for a in allow}
    return names - allow, terms


def liberty_names(paths) -> set[str]:
    lib = re.compile(r'^\s*library\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)', re.M)
    names: set[str] = set()
    for p in paths:
        with open(p, errors="ignore") as f:
            names.update(lib.findall(f.read(1 << 16)))
    return names


def _trie_regex(words: Iterable[str]) -> str:
    """One alternation of `words` as a prefix trie, so a few thousand cell
    names cost one pass over the text rather than one per name."""
    trie: dict = {}
    for w in words:
        node = trie
        for c in w:
            node = node.setdefault(c, {})
        node[""] = {}

    def emit(node: dict) -> str:
        end = "" in node
        alts = [re.escape(c) + emit(sub) for c, sub in sorted(node.items())
                if c]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{body})?" if end else body

    return emit(trie)


def _matcher(names: set[str], terms: set[str]):
    parts = []
    if names:
        parts.append(_trie_regex(names))
    if terms:
        parts.append("(?i:" + _trie_regex(terms) + ")")
    return re.compile("|".join(parts)) if parts else None


def _staged() -> Iterator[tuple[str, int, str]]:
    """(file, line, text) of every file name and line the staged diff
    adds."""
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    for n in names:
        yield n, 0, n
    diff = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"],
        capture_output=True, text=True, errors="replace", check=True).stdout
    path, line = "", 0
    for t in diff.splitlines():
        if t.startswith("+++ "):
            path = t[6:] if t.startswith("+++ b/") else t[4:]
        elif t.startswith("@@"):
            line = int(re.search(r"\+(\d+)", t).group(1))
        elif t.startswith("+"):
            yield path, line, t[1:]
            line += 1


def _files(paths: list[str]) -> Iterator[tuple[str, int, str]]:
    for root in paths:
        r = Path(root)
        if not r.exists():
            raise FileNotFoundError(f"{r} does not exist")
        if r.is_dir():
            # Inside a work tree: tracked and untracked files, not ignored
            # ones, so an untracked tree is scanned too.
            ls = subprocess.run(["git", "-C", str(r), "ls-files", "-z",
                                 "--cached", "--others", "--exclude-standard"],
                                capture_output=True, text=True)
            files = ([r / f for f in ls.stdout.split("\0") if f]
                     if ls.returncode == 0
                     else sorted(f for f in r.rglob("*") if f.is_file()))
        else:
            files = [r]
        for f in files:
            yield str(f), 0, str(f)
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for i, t in enumerate(data.decode(errors="replace").splitlines(), 1):
                yield str(f), i, t


def audit(paths: list[str]) -> int:
    packs = installed_packs()
    names: set[str] = set()
    terms: set[str] = set()
    private = []
    for pack in packs:
        n, t = denylist(pack)
        if n or t:
            private.append(pack.parent.name)
        names |= n
        terms |= t
    rx = _matcher(names, terms)
    if rx is None:
        print("techpack audit: no private pack installed; nothing to refuse")
        return 0
    hits = 0
    for f, i, text in (_files(paths) if paths else _staged()):
        m = rx.search(text)
        if m:
            hits += 1
            where = f"{f}:{i}" if i else f"{f} (file name)"
            print(f"{where}: {m.group(0)}")
    print(f"techpack audit: {len(private)} private pack(s) "
          f"({', '.join(private)}), {len(names)} names, {len(terms)} terms: "
          f"{hits} hit(s)", file=sys.stderr)
    return 1 if hits else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="circt_flow.techpack")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("ref")
    sub.add_parser("root").add_argument("ref")
    sub.add_parser("list")
    sub.add_parser("audit").add_argument("paths", nargs="*")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            for p in installed_packs():
                d = yaml.safe_load(p.read_text()) or {}
                print(f"{d.get('name')}\t{'public' if d.get('public') else 'private'}"
                      f"\t{p.parent}")
            return 0
        if a.cmd == "audit":
            return audit(a.paths)
        if a.cmd == "root":
            print(profile_mod.resolve(a.ref).parent)
            return 0
        return 1 if check(a.ref) else 0
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
