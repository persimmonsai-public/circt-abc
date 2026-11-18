"""Map every repeated residual module ONCE (syn.abc.partition.map_once; partitioned mapping only).

merge-residual-modules clones a residual module's body once per instantiation
site into its group, so a design with N identical blocks has ABC map the
block's logic N times in one run, and the group that holds the copies
dominates the stage's wall time. Grouping is per module, so no group map
can spread the copies.

With the option, the partitioned mapping stage
  1. finds the `*_residual` modules instantiated at least
     syn.abc.partition.map_once_min_instances times;
  2. maps each of them once, by itself, from an ISOLATED input: a file with
     the cell declarations and that one module (abc-bridge target=<module>
     emit-target-only). Only modules without an instance inside are
     taken (residual modules are pure logic);
  3. hides them from merge-residual-modules (their definitions become
     hw.module.extern for that step), so the groups hold the other modules;
  4. puts the mapped definitions back (private, so the final flatten inlines
     them at every instance) and runs remove-identity-buffers once more.

The netlist is NOT the one the stage gives without the option: a shared
module is optimised alone, with its own critical delay as ABC's budget, not
together with the other modules of its group. Check timing after turning it
on. A shared module goes to abc-bridge as it stands in the flat IR: what the
stage does to a GROUP before mapping does not reach it, so the options that
act there (syn.cleanup.specialize_mux_arms, syn.cleanup.narrow_mux,
syn.abc.partition.group_scripts, syn.abc.partition.isolate_substr) are
refused together with shared modules, and so are pipeline edits and pass
hooks of the mapping stage (the stage's commands are not the planned ones),
and the mapper's use-mapped-dir / save-mapped-dir / save-aig-dir, whose files
are named per group. These refusals apply when a module is actually taken.
With the skip-unmappable switch a shared module the mapper fails on is left
to the grouping, which blackboxes what its own test finds unmappable. A
module the grouping then leaves unmerged (it does nothing with a group
that has a single instantiation site) is mapped by itself as well, and the
stage stops if that fails: such a module does not reach the netlist
unmapped (what skip-unmappable blackboxes stays a blackbox, as that switch
intends). When modules were mapped by themselves and the grouping makes no
group, the group mapping is skipped.
With
syn.abc.partition.map_cache the mapped modules are kept and reused
(map_cache.py).
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import re
from pathlib import Path

_INST_RE = re.compile(r'hw\.instance "[^"]*"(?: sym @[\w$.]+)? @([A-Za-z0-9_.$]+)\(')


def has_no_instance(text: str, blocks: dict, name: str) -> bool:
    """the module holds no hw.instance at all: merge-residual-modules' own rule for a module it merges (a residual
    module is pure logic), and what lets it be handed to the mapper by itself"""
    a, b = blocks[name]
    return re.search(r"\bhw\.instance\b", text[a:b]) is None


def merge_targets(text: str, blocks: dict) -> list:
    """the modules merge-residual-modules would merge: no instance inside, and instantiated somewhere"""
    inst = collections.Counter(_INST_RE.findall(text))
    return [n for n in blocks if inst.get(n, 0) >= 1 and has_no_instance(text, blocks, n)]


def shared_modules(text: str, blocks: dict, min_instances: int) -> list:
    """the `*_residual` modules without an instance inside that are instantiated at least `min_instances` times"""
    inst = collections.Counter(_INST_RE.findall(text))
    return sorted(n for n in blocks if n.endswith("_residual") and inst.get(n, 0) >= min_instances
                  and has_no_instance(text, blocks, n))


def hide(text: str, blocks: dict, names) -> str:
    """the modules' definitions as extern declarations (same ports), so merge-residual-modules leaves them alone"""
    out = text
    for n in sorted(names, key=lambda n: blocks[n][0], reverse=True):
        a, b = blocks[n]
        header = text[a:text.index("\n", a)].rstrip()
        if not header.endswith("{"):
            raise RuntimeError(f"map_once: unexpected header of hw.module @{n}")
        decl, n_sub = re.subn(r"^  hw\.module(?: private| public| nested)? @", "  hw.module.extern @", header[:-1].rstrip())
        if n_sub != 1:
            raise RuntimeError(f"map_once: unexpected header of hw.module @{n}")
        out = out[:a] + decl + "\n" + out[b:]
    return out


def put_back(text: str, parts: dict, module_blocks, extern_re) -> str:
    """replace each extern declaration by the mapped definition (private) with its helper modules and cell externs"""
    out = text
    for name, mapped in parts.items():
        out, n = re.subn(r"^  hw\.module\.extern @%s\(.*\n" % re.escape(name), "", out, flags=re.M)
        if n != 1:
            raise RuntimeError(f"map_once: {n} extern declarations of @{name} in the mapped design (expected 1)")
        mb = module_blocks(mapped)
        if name not in mb:
            raise RuntimeError(f"map_once: the mapper's output has no hw.module @{name}")
        # the mapper's output brings helper modules (<module>_bits); a name the design already has cannot be added
        clash = sorted(set(mb) & set(module_blocks(out)))
        if clash:
            raise RuntimeError(f"map_once: the mapped {name} brings module(s) {', '.join(clash)}, a name the design "
                               "already defines; rename the design's module or turn syn.abc.partition.map_once off")
        body = "".join(mapped[s:e] for _, (s, e) in sorted(mb.items(), key=lambda kv: kv[1][0]))
        body = re.sub(r"^  hw\.module(?: public| nested)? @%s\(" % re.escape(name), f"  hw.module private @{name}(", body, flags=re.M)
        idx = out.rstrip().rfind("\n}")
        out = out[:idx] + "\n" + body.rstrip("\n") + out[idx:]
    have = set(extern_re.findall(out)) | set(module_blocks(out))      # a name the design defines is not declared again
    new = []
    for mapped in parts.values():
        for m in extern_re.finditer(mapped):
            if m.group(1) not in have:
                have.add(m.group(1)); new.append(m.group(0))
    if new:
        idx = out.rstrip().rfind("\n}")
        out = out[:idx] + "\n" + "\n".join(new) + out[idx:]
    return out


def left_unmerged(grouped_text: str, blocks: dict, group_prefix: str) -> list:
    """the modules the grouping command left as they were although they are merge targets: merge-residual-modules does
    not merge a group that has a single instantiation site (a lone module instantiated once), and such a module would
    otherwise reach the netlist unmapped"""
    return [n for n in merge_targets(grouped_text, blocks) if not n.startswith(group_prefix)]


def remaining_targets(text: str, blocks: dict, shared) -> list:
    """what merge-residual-modules still has to group once `shared` is hidden"""
    return [n for n in merge_targets(text, blocks) if n not in set(shared)]


def map_shared(cfg, text: str, blocks: dict, shared, workdir: Path, abc_bridge_opt: str, log=print, alone=False,
               leave_out=(), earlier_report=False) -> dict:
    """Map each module of `shared` once (over cfg.abc_pool_size workers, through the cache when
    syn.abc.partition.map_cache is set). Returns {module: mapped MLIR text}."""
    from . import abc_partition as ap
    from . import map_cache
    S = cfg.settings
    cache = None
    if S["syn.abc.partition.map_cache"]:
        try:
            cache = map_cache.Cache(S["syn.abc.partition.map_cache"], verify=bool(S["syn.abc.partition.map_cache_verify"]))
        except OSError as e:
            raise ValueError(f"syn.abc.partition.map_cache: {S['syn.abc.partition.map_cache']} cannot be used as the "
                             f"cache directory ({type(e).__name__}: {e})")
    workdir.mkdir(parents=True, exist_ok=True)
    inst = collections.Counter(_INST_RE.findall(text))
    if alone:
        log(f"[circt_flow] map_once: {', '.join(shared)}: left unmerged by the grouping, mapped by itself")
    else:
        log(f"[circt_flow] map_once: {len(shared)} residual module(s) mapped once each instead of per instance: "
            + ", ".join(f"{n} x{inst[n]}" for n in shared))

    def one(n):
        try:
            return (n,) + map_cache.map_cached(cache, n, text, blocks, workdir, cfg.circt_abc_opt, abc_bridge_opt,
                                               ap._map_one, ap._EXTERN_RE, extra_code=[__file__], leave_out=leave_out)
        except RuntimeError as e:
            if not getattr(cfg, "skip_unmappable", False):
                raise
            # skip-unmappable: the module is left to the grouping, which blackboxes what cannot be mapped
            return (n, None, 0.0, f"left to the grouping (skip-unmappable): {e}")
    with cf.ThreadPoolExecutor(max_workers=max(1, cfg.abc_pool_size)) as pool:
        results = list(pool.map(one, shared))
    slowest = max(results, key=lambda r: r[2])
    log(f"[circt_flow]   map_once: {sum(r[2] for r in results) / 60:.1f} min of ABC work, slowest {slowest[0]} ({slowest[2]:.0f}s)")
    if cache is not None:
        hits = sum(1 for r in results if r[3].startswith("hit"))
        log(f"[circt_flow]   map_cache {cache.root}{' (verify)' if cache.verify else ''}: {hits} hit, {len(results) - hits} mapped"
            + "".join(f"; {r[0]}: {r[3]}" for r in results if not r[3].startswith("hit")))
        rep_p = workdir / "cache_report.json"
        earlier = json.loads(rep_p.read_text()) if earlier_report and rep_p.is_file() else []   # one report per stage run
        rep_p.write_text(json.dumps(earlier + cache.report, indent=1) + "\n")
    left = [r for r in results if r[1] is None]
    for r in left:
        log(f"[circt_flow]   map_once: {r[0]}: {r[3]}")
    return {r[0]: r[1] for r in results if r[1] is not None}
