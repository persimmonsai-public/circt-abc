"""Mapping-library provenance: is a rail's priced liberty still the one its
real liberties make? (doc/techpacks.md, "Mapping-library provenance".)

scripts/mvt/make_abc_lib.py writes, as the first line of the priced library,
`/* make_abc_lib provenance v1: <json> */`. verify() reads that line and
compares it with the files the tech pack lists for the rail.

Verdicts: "ok", "stale" (with the reasons), "unverified" (the library has no
provenance line: built before it existed or by another tool), and None (the
rail has no priced library: its mapping list is one liberty or the real
liberties of its setup corner).

Flow integration: circt_flow.config calls enforce() when it loads a profile;
CIRCT_ABC_PROVENANCE=warn (default) prints stale/unverified to stderr,
error refuses a stale library, off skips the check.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

MARK = "make_abc_lib provenance v1:"
_LINE = re.compile(r"^\s*/\*\s*" + re.escape(MARK) + r"\s*(\{.*\})\s*\*/\s*$")
_CELL = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)', re.M)

_hash_cache: dict = {}
_cell_cache: dict = {}


def _key(path: Path):
    st = Path(path).stat()
    return (str(path), st.st_size, st.st_mtime_ns)


def sha256_file(path: Path, chunk: int = 1 << 22) -> Tuple[str, int]:
    """(sha256 hex, size) of `path`, streamed, memoised per process."""
    k = _key(path)
    if k not in _hash_cache:
        h, n = hashlib.sha256(), 0
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
                n += len(b)
        _hash_cache[k] = (h.hexdigest(), n)
    return _hash_cache[k]


def read_header(path: Path) -> Optional[dict]:
    """The provenance dict on the first line of `path`, or None."""
    with open(path, errors="ignore") as f:
        line = f.readline(1 << 22)
    m = _LINE.match(line)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _body_sha256(path: Path, chunk: int = 1 << 22) -> str:
    """sha256 of everything after the first line."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.readline()
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _cells(path: Path) -> set:
    k = _key(path)
    if k not in _cell_cache:
        names: set = set()
        with open(path, errors="ignore") as f:
            for c in iter(lambda: f.read(1 << 24), ""):
                names.update(_CELL.findall(c))
        _cell_cache[k] = names
    return _cell_cache[k]


def _compare(what: str, rec: list, files: List[Path]) -> List[str]:
    """Reasons the recorded {name, sha256} list differs from `files`,
    matched by content hash (order does not matter)."""
    have = {sha256_file(p)[0]: Path(p).name for p in files}
    want = {e.get("sha256"): e.get("name") for e in rec}
    out = []
    for sha, name in want.items():
        if sha not in have:
            if name in have.values():
                out.append(f"{what} {name} changed since the library was "
                           "built (sha256 differs)")
            else:
                out.append(f"{what} {name} was an input of the library but "
                           "is not in the rail's list")
    for sha, name in have.items():
        if sha not in want and name not in want.values():
            out.append(f"{what} {name} is in the rail's list but was not "
                       "an input of the library")
    return out


def verify(primary: Path, sources: List[Path],
           leakage: Optional[List[Path]] = None,
           leakage_is_setup: bool = True) -> Tuple[str, List[str]]:
    """(verdict, reasons) for the priced library `primary`.

    The rule (doc/techpacks.md): with a provenance line,
      1. its recorded inputs equal `sources` (the rail's setup-corner
         liberties; without corners, its mapping list after the primary) by
         sha256, as sets of files;
      2. its recorded leakage libraries equal `leakage` (the leakage
         corner's) or, when it records none, the leakage corner is the
         setup corner (`leakage_is_setup`);
      3. the file after the first line still hashes to body_sha256;
      4. every cell of the library is a cell of `sources`.
    Any failure is stale; a missing line is unverified."""
    h = read_header(primary)
    if h is None:
        return "unverified", ["no provenance line (not built by "
                              "make_abc_lib.py, or before it wrote one)"]
    why: List[str] = []
    why += _compare("input", h.get("inputs", []), sources)
    rec_leak = h.get("leakage_libs", [])
    if rec_leak:
        if not leakage:
            why.append("the library prices leakage from other liberties "
                       "but the pack gives no leakage corner list")
        else:
            why += _compare("leakage liberty", rec_leak, leakage)
    elif not leakage_is_setup:
        why.append("the library priced leakage from its inputs (the setup "
                   "corner) but the pack's leakage corner is another one")
    if _body_sha256(primary) != h.get("body_sha256"):
        why.append("the library was edited after it was built (body "
                   "sha256 differs)")
    extra = (_cells(primary) - set().union(*(_cells(p) for p in sources))
             if sources else set())
    if extra:
        why.append(f"{len(extra)} cell(s) of the library are not cells of "
                   f"its sources, e.g. {sorted(extra)[0]}")
    return ("stale", why) if why else ("ok", [])


def rail_sources(prof, r):
    """(primary, sources, leakage libs, leakage corner is the setup corner)
    of rail `r`, or None when the rail has no priced library (nothing after
    the primary in its mapping list, or the primary is itself a
    setup-corner liberty, or, without corners, no provenance line)."""
    libs = r.liberties
    if r.corners and prof.setup_corner in r.corners:
        src = list(r.corners[prof.setup_corner])
        if libs[0] in src:
            return None
        lk = prof.leakage_corner
        leak = list(r.corners[lk]) if lk in r.corners else None
        return libs[0], src, leak, lk == prof.setup_corner
    # Without corners nothing says the primary is a priced library (a pack
    # may split one cell family over several liberties): only a provenance
    # line makes it one.
    if len(libs) < 2 or not libs[0].exists() or read_header(libs[0]) is None:
        return None
    return libs[0], list(libs[1:]), None, True


def check_rail(prof, r) -> Optional[Tuple[str, List[str]]]:
    """verify() for rail `r` of `prof`; None without a priced library or
    when files are missing (techpack check reports those itself)."""
    rs = rail_sources(prof, r)
    if rs is None:
        return None
    primary, src, leak, same = rs
    if not all(p.exists() for p in [primary, *src, *(leak or [])]):
        return None
    return verify(primary, src, leak, same)


_seen: set = set()


def enforce(prof, mode: Optional[str] = None) -> None:
    """The flow's profile load: warn about, or refuse, a stale or
    unverified priced library. mode: CIRCT_ABC_PROVENANCE, one of off,
    warn (default), error. Only a stale library is an error; unverified
    only warns. Each message is printed once per process."""
    mode = (mode or os.environ.get("CIRCT_ABC_PROVENANCE", "warn")).lower()
    if mode not in ("off", "warn", "error"):
        raise ValueError(f"CIRCT_ABC_PROVENANCE={mode!r}: expected off, "
                         "warn or error")
    if mode == "off":
        return
    stale = []
    for name, r in prof.rails.items():
        res = check_rail(prof, r)
        if res is None or res[0] == "ok":
            continue
        verdict, why = res
        msg = (f"{prof.name} rail {name}: priced library "
               f"{r.liberties[0].name} is {verdict}: " + "; ".join(why))
        fatal = verdict == "stale" and mode == "error"
        if verdict == "stale":
            stale.append(msg)
        if msg not in _seen:
            _seen.add(msg)
            print("circt_flow: provenance: "
                  + ("ERROR: " if fatal else "WARNING: ") + msg,
                  file=sys.stderr)
    if stale and mode == "error":
        raise ValueError("stale priced mapping library (rebuild it with "
                         "scripts/mvt/make_abc_lib.py, or set "
                         "CIRCT_ABC_PROVENANCE=warn): " + stale[0])
