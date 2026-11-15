"""Passes of circt-abc-opt as the session sees them: the tool's own pass list
(names and options, from `circt-abc-opt --help`) and the argument of one
pass call (Session.run_pass).
"""
from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Set

from .options import OptionError

_TOOL_PASSES: Dict[str, Dict[str, Set[str]]] = {}


def parse_help(text: str) -> Dict[str, Set[str]]:
    """{pass: {option}} from an opt tool's --help text (the "Passes:"
    section: a pass at six spaces, its options at eight)."""
    passes: Dict[str, Set[str]] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^      --([\w-]+)\s", line) or re.match(r"^      --([\w-]+)$", line)
        if m:
            cur = m.group(1)
            passes[cur] = set()
            continue
        m = re.match(r"^        --([\w-]+)", line)
        if m and cur:
            passes[cur].add(m.group(1))
    return passes


def tool_passes(binary) -> Dict[str, Set[str]]:
    """The passes (and their options) `binary` registers (cached)."""
    key = str(binary)
    if key not in _TOOL_PASSES:
        try:
            out = subprocess.run([key, "--help"], capture_output=True, text=True).stdout
        except OSError as e:
            raise OptionError(f"cannot list the passes of {key}: {e}") from None
        _TOOL_PASSES[key] = parse_help(out)
        if not _TOOL_PASSES[key]:
            raise OptionError(f"{key} --help lists no passes")
    return _TOOL_PASSES[key]


def check_pass(binary, name: str) -> Set[str]:
    passes = tool_passes(binary)
    if name not in passes:
        near = difflib.get_close_matches(name, list(passes), 3, 0.6)
        raise OptionError(f"unknown pass {name!r}"
                          + (f" (did you mean {', '.join(near)}?)" if near else "")
                          + f": not a pass of {binary}")
    return passes[name]


def _opt_text(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(map(str, v))
    return str(v)


def pass_arg(binary, name: str, pass_options: Mapping[str, object]) -> str:
    """`--name` or `--name=opt=value opt2=value` for one pass call; the pass
    and its option names are checked against the tool (`_` in a Python
    keyword stands for `-`)."""
    known = check_pass(binary, name)
    parts = []
    for k, v in pass_options.items():
        opt = k.replace("_", "-")
        if opt not in known:
            near = difflib.get_close_matches(opt, sorted(known), 3, 0.6)
            raise OptionError(f"pass {name} has no option {opt!r}"
                              + (f" (did you mean {', '.join(near)}?)" if near else
                                 f"; its options: {', '.join(sorted(known)) or 'none'}"))
        parts.append(f"{opt}={_opt_text(v)}")
    return f"--{name}" + ("=" + " ".join(parts) if parts else "")
