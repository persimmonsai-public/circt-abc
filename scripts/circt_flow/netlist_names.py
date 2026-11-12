"""Rename the SPEF delimiter out of escaped Verilog identifiers.

    python3 -m circt_flow.netlist_names IN.v OUT.v [--map map.json] [--repl _] [--strict]
    python3 -m circt_flow.netlist_names --check IN.v        (exit 1 when a rename is needed)

Escaped identifiers may contain `:` (for example `\\u_a/sum:3 `). SPEF uses
`:` as its default name delimiter, and a SPEF reader that does not unescape
names and splits `inst:pin` at the first `:` cannot bind such a net; it
skips it (the parasitic capacitance of every such net is then silently
zero). Renaming the character in the netlist before place and route (so
every file the P&R tool writes carries the new name) is function-preserving.

Properties, all tested: only the `:` inside escaped identifiers changes (not
comments, part selects `[7:0]` or any other text); every name without a `:`
is untouched; the new names never collide with an existing identifier
(escaped or plain, anywhere in the file) or each other: a collision gets a
numeric suffix, or raises with `--strict`; the old -> new map is returned
and written with `--map`, so the rename is reversible (`restore`). A file
with no such identifier is returned byte for byte.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Tuple

# One alternation so that comments are skipped and identifiers are found in
# source order: comment | escaped identifier (up to whitespace) | plain id.
_TOK = re.compile(r"//[^\n]*|/\*.*?\*/|\\(\S+)|([A-Za-z_][A-Za-z0-9_$]*)", re.S)


class NameError_(ValueError):
    pass


def needs_rename(text: str, delim: str = ":") -> bool:
    return any(m.group(1) and delim in m.group(1) for m in _TOK.finditer(text))


def normalize(text: str, repl: str = "_", delim: str = ":",
              strict: bool = False) -> Tuple[str, Dict[str, str]]:
    """(new text, {old escaped name without the backslash: new name})."""
    if delim in repl:
        raise NameError_(f"replacement {repl!r} contains the delimiter")
    taken = set()
    olds = []
    for m in _TOK.finditer(text):
        if m.group(1):
            taken.add(m.group(1))
            if delim in m.group(1) and m.group(1) not in olds:
                olds.append(m.group(1))
        elif m.group(2):
            taken.add(m.group(2))
    mapping: Dict[str, str] = {}
    used = set()
    for old in olds:
        new = old.replace(delim, repl)
        if new in taken or new in used:
            if strict:
                raise NameError_(f"renaming {old!r} to {new!r} collides with an "
                                 "existing identifier")
            n = 1
            while f"{new}_{n}" in taken or f"{new}_{n}" in used:
                n += 1
            new = f"{new}_{n}"
        mapping[old] = new
        used.add(new)
    if not mapping:
        return text, mapping

    def sub(m):
        if m.group(1) and m.group(1) in mapping:
            return "\\" + mapping[m.group(1)]
        return m.group(0)
    return _TOK.sub(sub, text), mapping


def restore(text: str, mapping: Dict[str, str]) -> str:
    """Undo normalize() given its map (used to name results back)."""
    inv = {v: k for k, v in mapping.items()}

    def sub(m):
        if m.group(1) and m.group(1) in inv:
            return "\\" + inv[m.group(1)]
        return m.group(0)
    return _TOK.sub(sub, text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path, nargs="?")
    ap.add_argument("--map", type=Path, default=None)
    ap.add_argument("--repl", default="_")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    text = a.src.read_text()
    if a.check:
        need = needs_rename(text)
        print("netlist_names: " + ("rename needed" if need else "clean"))
        return 1 if need else 0
    if a.dst is None:
        ap.error("dst is required without --check")
    out, mp = normalize(text, a.repl, strict=a.strict)
    a.dst.write_text(out)
    if a.map:
        a.map.write_text(json.dumps(mp, indent=1, sort_keys=True))
    print(f"netlist_names: {len(mp)} identifier(s) renamed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
