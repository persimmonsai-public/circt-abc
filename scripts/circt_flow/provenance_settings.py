"""settings.json: what a run resolved, and from where (options.py), with the
tool commits and the pass pipeline as executed.

Written into the run's output directory at the start of the run and again
after synthesis (and after P&R): every option with value and source, the
legacy environment variables that were used, the circt-abc commit (and
whether the tree was dirty), the circt-abc-opt binary's sha256, and every
command the synthesis stages ran with its exact pass-option strings
(stages.EXECUTED). `read(path)` returns the record; `explicit(record)` the
{name: value} of every option not at its built-in default, which
`FlowConfig.load(settings=...)` takes to replay a run's options.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

FORMAT = 1

# Extra top-level fields a Python session adds to the record while it runs
# (session.py: "set_at", where a script set each option; "scoped", the
# scoped settings, scoping.py). Empty for a command-line run, whose record
# is unchanged.
EXTRA: dict = {}


def _git(root: Path):
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True).stdout.strip()
        return {"commit": head or None, "dirty": bool(dirty)}
    except OSError:
        return {"commit": None, "dirty": None}


_SHA: dict = {}


def _sha256(path: Path):
    p = Path(path)
    if str(p) in _SHA:
        return _SHA[str(p)]
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        _SHA[str(p)] = h.hexdigest()
    except OSError:
        _SHA[str(p)] = None
    return _SHA[str(p)]


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def recipe_files(cfg) -> dict:
    """{role: {path, sha256}} of the files the run's options come from: the
    flow.yaml, the tech pack, the design manifest, the ABC script, the mode
    SDCs (None for a file that cannot be read)."""
    d = cfg.design
    files = {"flow_yaml": cfg.config_path, "techpack": d.profile,
             "manifest": d.manifest, "abc_script": cfg.abc_script_file}
    for m in (d.modes or ()):
        if getattr(m, "sdc", None):
            files[f"sdc:{m.name}"] = m.sdc
    def sha(p):
        try:
            return hashlib.sha256(Path(p).read_bytes()).hexdigest()
        except OSError:
            return None
    return {k: {"path": str(Path(v).resolve()), "sha256": sha(v)}
            for k, v in sorted(files.items()) if v}


def record(cfg, stage: str = "") -> dict:
    from . import options, scoping, stages
    s = cfg.settings
    return {
        "format": FORMAT,
        "stage": stage,
        "config": str(Path(cfg.config_path).resolve()),
        "tools": {"circt_abc": {**_git(cfg.root), "root": str(cfg.root)},
                  "circt_abc_opt": {"path": str(cfg.circt_abc_opt),
                                    "sha256": _sha256(cfg.circt_abc_opt)},
                  "circt_opt": str(cfg.circt_opt), "abc_bin": str(cfg.abc_bin)},
        "recipe_files": recipe_files(cfg),
        "strict": bool(getattr(s, "strict", False)),
        "legacy_env_used": dict(sorted(getattr(s, "legacy_used", {}).items())),
        "legacy_env_removed_in_release": options.LEGACY_ENV_REMOVED_IN,
        "options": s.as_record(),
        "pipeline": [list(c) for c in stages.EXECUTED],
        **scoping.record_extra(cfg),
        **{k: v for k, v in EXTRA.items() if v},
    }


def write(cfg, stage: str = "", path=None) -> Path:
    path = Path(path) if path else Path(cfg.out_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record(cfg, stage), indent=1, sort_keys=True) + "\n")
    return path


def read(path) -> dict:
    return json.loads(Path(path).read_text())


def explicit(rec: dict) -> dict:
    """{name: value} of every option of a settings record whose source is
    not the built-in default: FlowConfig.load(settings=...) replays them
    (as arguments)."""
    from . import options
    reg = options.registry()
    return {k: v["value"] for k, v in rec["options"].items()
            if v["source"] != "default" and k in reg and reg[k].settable}


def diff(a: dict, b: dict) -> list:
    """The options whose value or source differ between two settings
    records, plus the tool commits: [(name, (value, source) in a, in b)]."""
    out = []
    oa, ob = a.get("options", {}), b.get("options", {})
    for k in sorted(set(oa) | set(ob)):
        va, vb = oa.get(k), ob.get(k)
        if va != vb:
            out.append((k, va and (va["value"], va["source"]),
                        vb and (vb["value"], vb["source"])))
    # the tool identity, not where the tree or binary lies
    ident = {"circt_abc": ("commit", "dirty"), "circt_abc_opt": ("sha256",)}
    for k, fields in ident.items():
        ta = {f: (a.get("tools", {}).get(k) or {}).get(f) for f in fields}
        tb = {f: (b.get("tools", {}).get(k) or {}).get(f) for f in fields}
        if ta != tb:
            out.append((f"tools.{k}", ta, tb))
    fa = {k: v.get("sha256") for k, v in a.get("recipe_files", {}).items()}
    fb = {k: v.get("sha256") for k, v in b.get("recipe_files", {}).items()}
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            out.append((f"recipe_files.{k}", fa.get(k), fb.get(k)))
    return out
