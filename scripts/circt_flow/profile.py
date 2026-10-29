"""Library profiles: what the flow needs to know about a cell library.

A profile (profiles/<name>.yaml) names the liberties, the library's time
unit, the integrated clock gate a design's clock-gating primitive maps to,
and the platform's dont_use patterns. Liberties come either from explicit
paths (`liberties:`) or from an ORFS image's platform directory
(`orfs: {platform, liberties}`), in which case they are extracted and
decompressed into a cache on first use and merged into one file, since the
mapper's passes take a single liberty.

    python3 -m circt_flow.profile <profile.yaml>    # prepare and print it
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


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.environ.get("CIRCT_ABC_CACHE", Path(base) / "circt-abc"))


@dataclass(frozen=True)
class Profile:
    name: str
    liberties: List[Path]
    time_unit: str
    icg: dict
    dont_use: List[str] = field(default_factory=list)
    orfs_platform: Optional[str] = None
    # {"hi": "CELL:PIN", "lo": "CELL:PIN"}, for P&R's constant ties.
    tie: dict = field(default_factory=dict)

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


def load(path: Path | str, image: Optional[str] = None) -> Profile:
    path = Path(path)
    d = yaml.safe_load(path.read_text())
    orfs = d.get("orfs")
    if orfs:
        image = image or os.environ.get("ORFS_IMAGE", DEFAULT_IMAGE)
        tag = image.rsplit(":", 1)[-1]
        dest = cache_root() / "orfs" / tag / orfs["platform"]
        libs = _extract_orfs(image, orfs["platform"], orfs["liberties"], dest)
        merged = dest / f"{d['name']}_merged.lib"
        if not merged.exists():
            merge_liberties(libs, merged)
        liberties = [merged]
    else:
        liberties = [Path(os.path.expandvars(p)) for p in d["liberties"]]
    return Profile(name=d["name"], liberties=liberties,
                   time_unit=d.get("time_unit", "ns"), icg=d["icg"],
                   dont_use=list(d.get("dont_use", [])),
                   orfs_platform=(orfs or {}).get("platform"),
                   tie=dict(d.get("tie") or {}))


if __name__ == "__main__":
    p = load(sys.argv[1])
    for k, v in p.__dict__.items():
        print(f"{k}: {v}")
