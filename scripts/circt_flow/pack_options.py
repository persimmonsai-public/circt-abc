"""A tech pack's own P&R options, read from the pack's option registry
(doc/pack-options.md).

A pack with its own P&R command may declare its command's options in a JSON
file named by its `techpack.yaml` (`pnr: {settings_registry: <file>}`,
relative to the pack). This module only READS that file (it never imports
pack code) and maps each entry onto `options.Option`, so a value is
validated here exactly as a flow option is (type, range, choices, pattern).

  registry_path(pack_root, pack_pnr=None) -> the registry file
  load(pack_root, pack_pnr=None)  -> {name: options.Option} (binds PnrSetting(var))
  check(pack_root, pack_pnr=None) -> [problem]   drift rules D1 to D5 (D1: a
                                shared option exists in circt-abc with the same
                                type, choices, range and default where
                                circt-abc types it; D2-D3: an unshared option
                                does not reuse circt-abc's names or legacy
                                variables; D4: the same legacy-removal release;
                                D5: a declared namespace, none of circt-abc's
                                own for an unshared option)
  write_settings(...)          -> a user settings file the pack command's
                                  `--settings FILE` takes
  handover(...)                -> the values handed to the pack command (the
                                  profile's settings_defaults, a session's
                                  values over them, the recipe directory) and
                                  their sources
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

from . import options
from .options import Option, OptionError, PnrSetting

TYPES = ("bool", "int", "float", "str", "path", "choice")
# circt-abc's own namespace: no unshared pack option may live in it (D5)
RESERVED_NAMESPACES = ("syn",)


class PackOptionsError(OptionError):
    """The pack's registry file is missing or malformed."""


def _pack_pnr(pack_root) -> dict:
    """The `pnr:` section of the pack's techpack.yaml (`pack_root`: the pack
    directory, or its techpack .yaml file)."""
    import yaml
    root = Path(pack_root)
    y = root if root.is_file() else root / "techpack.yaml"
    try:
        d = yaml.safe_load(y.read_text()) or {}
    except OSError as e:
        raise PackOptionsError(f"{y}: {e}") from None
    return dict(d.get("pnr") or {})


def registry_path(pack_root, pack_pnr=None) -> Path:
    """The pack's option registry: its techpack.yaml's
    `pnr.settings_registry`, relative to the pack directory."""
    root = Path(pack_root)
    if root.is_file():
        root = root.parent
    pnr = _pack_pnr(pack_root) if pack_pnr is None else (pack_pnr or {})
    rel = pnr.get("settings_registry")
    if not rel:
        raise PackOptionsError(f"{root}: the pack declares no P&R option registry "
                               "(pnr.settings_registry in its techpack.yaml)")
    return root / rel


def has_registry(pack_root, pack_pnr=None) -> bool:
    try:
        return pack_root is not None and registry_path(pack_root, pack_pnr).is_file()
    except PackOptionsError:
        return False


def read_registry(pack_root, pack_pnr=None) -> dict:
    """The registry file as parsed JSON (shape-checked)."""
    p = registry_path(pack_root, pack_pnr)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        raise PackOptionsError(f"{p}: the pack's option registry is missing") from None
    except (OSError, ValueError) as e:
        raise PackOptionsError(f"{p}: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("options"), list):
        raise PackOptionsError(f"{p}: not a settings registry (no options list)")
    if raw.get("format") != 1:
        raise PackOptionsError(f"{p}: unsupported format {raw.get('format')!r}")
    return raw


def _option(d: Mapping[str, Any], where: str) -> Option:
    name = d.get("name")
    if not isinstance(name, str) or not name:
        raise PackOptionsError(f"{where}: option without a name")
    t = d.get("type")
    if t not in TYPES:
        raise PackOptionsError(f"{where}: {name}: unknown type {t!r}")
    rng = d.get("range")
    if rng:
        if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
            raise PackOptionsError(f"{where}: {name}: range must be [lo, hi]")
        rng = (rng[0], rng[1])
    else:
        rng = None
    pat = d.get("pattern")
    if pat is not None:
        try:
            re.compile(pat)
        except re.error as e:
            raise PackOptionsError(f"{where}: {name}: bad pattern: {e}") from None
    meta = []
    if d.get("group"):
        meta.append(f"group {d['group']}")
    if d.get("_needs"):
        meta.append(f"needs {d['_needs'][0]} = {d['_needs'][1]!r}")
    if d.get("stage"):
        meta.append(f"stage {d['stage']}")
    if d.get("consumers"):
        meta.append("consumers " + "/".join(d["consumers"]))
    if d.get("shared"):
        meta.append("shared with the flow's option")
    o = Option(
        name, t, d.get("default"), d.get("help", ""), unit=d.get("unit") or "",
        range=rng, choices=tuple(d.get("choices") or ()),
        binds=(PnrSetting(d.get("var") or name),),
        legacy_env=tuple(d.get("legacy_env") or ()),
        note="; ".join(meta), empty_is_value=bool(d.get("empty_ok")),
        pattern=pat)
    # the default must itself be legal ("" is the registry's spelling of
    # "unset" for a str option that does not allow an empty value)
    if o.default is not None and o.default != "":
        try:
            o.parse(o.default, f"{where}: default")
        except OptionError as e:
            raise PackOptionsError(str(e)) from None
    return o


def groups(pack_root, pack_pnr=None) -> Dict[str, str]:
    """{name: group} of the options that carry the free-text `group` field."""
    return {d["name"]: str(d["group"]) for d in read_registry(pack_root, pack_pnr)["options"]
            if d.get("name") and d.get("group")}


def requires_of(raw: dict) -> Dict[str, tuple]:
    """The registry's top-level `requires` ({option: [other, value]}): the
    pack's command uses `option` only when `other` has that value. circt-abc
    lists it (options --pack) and checks that both names are options
    (check); the command enforces it, since `other` may come from a source
    circt-abc does not see (its environment, its own defaults)."""
    out = {}
    req = raw.get("requires") or {}
    if not isinstance(req, dict):
        return out
    for name, cond in req.items():
        if isinstance(cond, (list, tuple)) and len(cond) == 2:
            out[name] = (str(cond[0]), cond[1])
    return out


def _requires_problems(raw: dict) -> List[str]:
    """Entries of `requires` that are not {option: [other, value]}."""
    req = raw.get("requires")
    if req is None:
        return []
    if not isinstance(req, dict):
        return [f"requires: not a mapping of option -> [other, value] ({type(req).__name__})"]
    return [f"requires: {name}: {cond!r} is not [other, value]"
            for name, cond in req.items()
            if not (isinstance(cond, (list, tuple)) and len(cond) == 2
                    and isinstance(cond[0], str) and cond[0])]


def load(pack_root, pack_pnr=None) -> Dict[str, Option]:
    """{name: Option} of the pack's registry file."""
    raw = read_registry(pack_root, pack_pnr)
    where = str(registry_path(pack_root, pack_pnr))
    req = requires_of(raw)
    out: Dict[str, Option] = {}
    for d in raw["options"]:
        if d.get("name") in req:
            d = {**d, "_needs": req[d["name"]]}
        o = _option(d, where)
        if o.name in out:
            raise PackOptionsError(f"{where}: duplicate option {o.name}")
        out[o.name] = o
    return out


def check(pack_root, pack_pnr=None) -> List[str]:
    """Drift rules D1 to D5 (plus a well-formed file); [] when clean."""
    try:
        raw = read_registry(pack_root, pack_pnr)
        opts = load(pack_root, pack_pnr)
    except OptionError as e:
        return [str(e)]
    probs: List[str] = []
    flow = options.registry()
    flow_legacy = options.legacy_map()
    shared = {d["name"]: bool(d.get("shared")) for d in raw["options"]}
    if raw.get("legacy_env_removed_in") != options.LEGACY_ENV_REMOVED_IN:
        probs.append(f"D4: legacy_env_removed_in={raw.get('legacy_env_removed_in')!r} "
                     f"!= circt_flow.options.LEGACY_ENV_REMOVED_IN="
                     f"{options.LEGACY_ENV_REMOVED_IN}")
    ns = set(raw.get("namespaces") or ())
    probs += _requires_problems(raw)
    for name, (other, _v) in requires_of(raw).items():
        for n in (name, other):
            if n not in opts:
                probs.append(f"requires: {n} is not an option of the registry")
    for name, o in opts.items():
        if shared[name]:
            if name not in flow:
                probs.append(f"D1: shared option {name} is not in circt-abc's registry")
            else:
                probs += [f"D1: shared option {name}: {p}" for p in _shared_mismatch(o, flow[name])]
        elif name in flow:
            probs.append(f"D2: {name} exists in circt-abc's registry but is not "
                         f"marked shared")
        for v in o.legacy_env:
            if v in flow_legacy:
                if not shared[name]:
                    probs.append(f"D3: legacy variable {v} of {name} is a circt-abc "
                                 f"legacy variable but {name} is not shared")
                elif name not in flow_legacy[v]:
                    probs.append(f"D3: legacy variable {v} maps to {name} in the "
                                 f"pack but to {', '.join(flow_legacy[v])} in circt-abc")
        if name.split(".", 1)[0] not in ns:
            probs.append(f"D5: {name}: namespace {name.split('.', 1)[0]!r} is not in "
                         f"namespaces {sorted(ns)}")
        elif name.split(".", 1)[0] in RESERVED_NAMESPACES and not shared[name]:
            probs.append(f"D5: {name}: namespace {name.split('.', 1)[0]!r} is circt-abc's "
                         f"(synthesis); a pack option there must be a shared one")
    return probs


def _shared_mismatch(pack: Option, flow: Option) -> List[str]:
    """D1 beyond the name: a shared option must mean the same on both sides.
    Compared where circt-abc types the option itself (its "object" options
    are validated by their flow.yaml section parser, so only the name is
    checked): type, choices, range and default."""
    if flow.type == "object":
        return []
    out = []
    ptype = "str" if pack.type == "path" and flow.type == "str" else pack.type
    if ptype != flow.type:
        out.append(f"type {pack.type} in the pack, {flow.type} in circt-abc")
    if tuple(pack.choices) != tuple(flow.choices):
        out.append(f"choices {list(pack.choices)} in the pack, {list(flow.choices)} in circt-abc")
    if (pack.range or None) != (tuple(flow.range) if flow.range else None):
        out.append(f"range {pack.range} in the pack, {flow.range} in circt-abc")
    if pack.default != flow.default:
        out.append(f"default {pack.default!r} in the pack, {flow.default!r} in circt-abc")
    return out


def parse_value(o: Option, raw, where: str = "pack option"):
    """`raw` as option `o`'s value; refuses "" unless the option's empty_ok."""
    if isinstance(raw, str) and raw == "" and not o.empty_is_value:
        raise OptionError(f"{where}: {o.name} is empty (the option does not "
                          f"allow an empty value)")
    if raw is None:
        raise OptionError(f"{where}: {o.name} has no value (null)")
    v = o.parse(raw, where)
    if isinstance(v, float) and not math.isfinite(v):
        # strict JSON has no Infinity / NaN: the pack's reader would refuse the file
        raise OptionError(f"{where}: {o.name} = {raw!r} is not a finite number")
    return v


def write_settings(pack_root, values: Mapping[str, Any], path, pack_pnr=None,
                   path_base=None) -> Path:
    """Write the user settings file `{"options": {name: value}}` that the
    pack's `--settings` accepts. Every name must be one of the pack's
    options and every value must validate; otherwise nothing is written
    (OptionError). A relative value of a `path` option is written absolute,
    against `path_base` (default: the current directory)."""
    opts = load(pack_root, pack_pnr)
    out: Dict[str, Any] = {}
    for name, val in values.items():
        o = opts.get(name)
        if o is None:
            import difflib
            near = difflib.get_close_matches(name, list(opts), 3, 0.6)
            raise OptionError(f"unknown pack option {name!r}"
                              + (f" (did you mean {', '.join(near)}?)" if near else ""))
        out[name] = parse_value(o, val, "settings file")
        if o.type == "path" and isinstance(out[name], str) and out[name] \
                and not Path(out[name]).is_absolute():
            out[name] = str((Path(path_base or ".") / out[name]).resolve())
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"options": dict(sorted(out.items()))}, indent=1,
                              allow_nan=False) + "\n")
    tmp.replace(path)
    return path


DEFAULTS_SOURCE = "pnr.settings_defaults (the pack profile)"
RECIPE_DIR_SOURCE = "registry recipe_dir"


def handover(pack_root, pack_pnr, given: Mapping[str, Any] = None,
             given_source: Mapping[str, str] = None, environ=None) -> tuple:
    """The pack options handed to the pack's P&R command, and where each
    comes from: ({name: value}, {name: source}). The one place that builds
    them (stages.pack_command writes them; Session.pack_options shows them).

      * the profile's `pnr.settings_defaults` (a relative `path` value is
        relative to the pack directory);
      * over them, `given` (a session's set_pack_option values and the shared
        options it set; a relative `path` value is relative to the current
        directory); `given_source` names their sources (default
        "set_pack_option");
      * the recipe directory: when an option whose `consumers` include one of
        the registry's `recipe_dir_consumers` (default ["recipe"]) is handed
        over and `recipe_dir_option` is not (neither given nor its legacy
        variable in `environ`), `recipe_dir_option` gets the registry's
        `recipe_dir` (relative to the pack); a registry that names no such
        directory is refused (PackOptionsError) naming the option to set.

    Every value is validated against the registry. ({}, {}) for a pack
    without a registry and nothing given."""
    import os
    environ = os.environ if environ is None else environ
    given = dict(given or {})
    pnr = _pack_pnr(pack_root) if pack_pnr is None else dict(pack_pnr or {})
    if not has_registry(pack_root, pnr):
        if given:                  # refused: a missing or undeclared registry
            raise PackOptionsError(f"{registry_path(pack_root, pnr)}: the pack's option "
                                   "registry is missing")
        return {}, {}
    raw = read_registry(pack_root, pnr)
    opts = load(pack_root, pnr)
    root = Path(pack_root)
    base = root.parent if root.is_file() else root

    def norm(name, v, against, where):
        o = opts.get(name)
        if o is None:
            import difflib
            near = difflib.get_close_matches(name, list(opts), 3, 0.6)
            raise OptionError(f"{where}: unknown pack option {name!r}"
                              + (f" (did you mean {', '.join(near)}?)" if near else ""))
        v = parse_value(o, v, where)
        if o.type == "path" and isinstance(v, str) and v and not Path(v).is_absolute():
            v = str((Path(against) / v).resolve())
        return v
    values, src = {}, {}
    for k, v in (pnr.get("settings_defaults") or {}).items():
        values[k], src[k] = norm(k, v, base, "pnr.settings_defaults"), DEFAULTS_SOURCE
    for k, v in given.items():
        values[k] = norm(k, v, Path.cwd(), "pack option")
        src[k] = (given_source or {}).get(k, "set_pack_option")
    rd = raw.get("recipe_dir_option")
    by = {d.get("name"): d for d in raw["options"]}
    if rd and rd in by and rd not in values \
            and not any(v in environ for v in by[rd].get("legacy_env") or ()):
        tags = set(raw.get("recipe_dir_consumers") or ["recipe"])
        need = sorted(n for n in values if tags & set(by.get(n, {}).get("consumers") or ()))
        if need:
            rel = raw.get("recipe_dir")
            d = (base / rel).resolve() if isinstance(rel, str) and rel else None
            if d is None or not d.is_dir():
                raise PackOptionsError(
                    f"pack option(s) {', '.join(need)} are read from the pack's recipe "
                    f"directory, but the pack's registry names none (\"recipe_dir\""
                    f"{f': {rel!r} is not a directory' if rel else ''}): "
                    f"set s.set_pack_option({rd!r}, '<the directory>') (or {rd} in "
                    f"pnr.settings_defaults)")
            values[rd] = str(d)
            src[rd] = f"{RECIPE_DIR_SOURCE} (for {', '.join(need)})"
    return dict(sorted(values.items())), dict(sorted(src.items()))


SETTINGS_FILE = "pack_settings.json"


def write_handover(pack_root, pack_pnr, out, given: Mapping[str, Any] = None,
                   environ=None, log=None):
    """handover() written to <out>/pack_settings.json; the path, or None
    when there is nothing to hand over (no file, no argument)."""
    values, src = handover(pack_root, pack_pnr, given, environ=environ)
    if not values:
        return None
    for k, s in src.items():
        if s.startswith(RECIPE_DIR_SOURCE) and log is not None:
            log(f"[circt_flow] pack option {k} = {values[k]} ({s})")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    return write_settings(pack_root, values, out / SETTINGS_FILE, pack_pnr)


def _type(o: Option) -> str:
    return o.type + (f" {'/'.join(o.choices)}" if o.choices else "") + \
        (f" [{'' if o.range[0] is None else o.range[0]}, "
         f"{'' if o.range[1] is None else o.range[1]}]" if o.range else "") + \
        (f" ({o.unit})" if o.unit else "")


def render_markdown(pack_root, group=None) -> str:
    opts = load(pack_root)
    if group:
        grp = groups(pack_root)
        opts = {n: o for n, o in opts.items() if grp.get(n) == group}
    lines = ["# Pack P&R options (generated)", "",
             f"From `{registry_path(pack_root)}`"
             + (f", group `{group}`" if group else "") + ".", "",
             "| option | type | default | var | legacy variable | note | help |",
             "|---|---|---|---|---|---|---|"]
    for name in sorted(opts):
        o = opts[name]
        d = "unset" if o.default is None else repr(o.default)
        lines.append(f"| `{name}` | {_type(o)} | {d} | `{o.binds[0].name}` | "
                     f"{', '.join(f'`{v}`' for v in o.legacy_env)} | {o.note} | "
                     f"{o.help.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(a) -> int:
    """`circt_flow options --pack ROOT ...` (a: options.main's namespace)."""
    try:
        opts = load(a.pack)
    except OptionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    grp = groups(a.pack)
    want = getattr(a, "group", None)
    if want and want not in set(grp.values()):
        known = sorted(set(grp.values()))
        print(f"ERROR: --group {want}: no option of the pack is in that group; "
              + (f"its groups: {', '.join(known)}" if known
                 else "the pack's registry gives no option a group"), file=sys.stderr)
        return 2
    if a.markdown:
        if not want:
            sys.stdout.write(options.render_markdown())
            sys.stdout.write("\n")
        sys.stdout.write(render_markdown(a.pack, want))
        return 0
    rows = [o for n, o in sorted(opts.items()) if a.pattern in n
            and (not want or grp.get(n) == want)]
    if a.json:
        print(json.dumps([{"name": o.name, "type": o.type,
                           "default": o.default, "unit": o.unit,
                           "range": o.range, "choices": list(o.choices),
                           "binds": [str(b) for b in o.binds],
                           "legacy_env": list(o.legacy_env), "help": o.help,
                           "group": grp.get(o.name), "pack": True} for o in rows], indent=1))
        return 0
    rc = options.main([a.pattern] if a.pattern else [])
    print(f"\n# pack P&R options ({registry_path(a.pack)})")
    for o in rows:
        print(f"{o.name:45s} {o.type:7s} "
              f"{('unset' if o.default is None else repr(o.default)):14s} "
              + (f"[{grp[o.name]}] " if o.name in grp else "") + f"{o.help}"
              + (f"  [legacy {', '.join(o.legacy_env)}]" if o.legacy_env else ""))
    return rc
