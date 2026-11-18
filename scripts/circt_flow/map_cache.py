"""Cache of ABC-mapped residual modules for map_once.py (syn.abc.partition.map_cache, a directory).

A cache entry may only be used when a fresh mapping would give the same bytes. So:

* THE INPUT IS WHAT IS HASHED. A module is mapped from an ISOLATED file that holds nothing but the cell declarations and
  that one module (the mapped module is the one the mapper gives from the whole design), and the hash
  is taken over that file. The rest of the design cannot influence the result, by construction.
* EVERYTHING ELSE THE MAPPER READS IS IN THE IDENTITY (identity() below): every abc-bridge option; the CONTENT of every
  file an option names (liberty, cell stubs, ABC script) and of every file the ABC script names; the mapper and ABC
  binaries (content, version text, and the content of every shared library they load); the files ABC opens by name
  when present (.abc.rc / abc.rc in the working directory, ~/.abc.rc, <library>.skip in the working directory); the environment variables of the tools' families and the locale; the
  machine architecture and C library; the format of this cache and the content of this file and of map_once.py.
  File NAMES are not part of the identity (the same inputs in another directory are the same inputs); they are kept in
  the record for the reader.
* A HIT IS CHECKED, NOT TRUSTED. The entry's record must repeat the whole identity field by field (not only the key),
  the stored module must have the checksum the record gives and must contain the target module; otherwise the entry is
  rejected with the reason, the module is mapped, and the entry is replaced.
* VERIFY MODE (syn.abc.partition.map_cache_verify) maps on every hit as well and stops the run if the bytes differ: that would mean
  the mapper is not deterministic for this input, or something it reads is missing from the identity.

* WHAT CANNOT BE COVERED IS NOT CACHED (NotCacheable): a call with use-mapped-dir, save-mapped-dir or save-aig-dir (the
  mapper imports or writes netlists outside its output), an option naming a directory, a liberty with an include_file,
  an ABC binary that cannot be found, a file the ABC script sources that is not found. Such a module is mapped every
  time and the report says why. Every word of the ABC script (and of an rc file) that names an existing file counts as
  a file it reaches, and those files are followed in turn (script_files; a file over 4 MB is taken by content only, a
  small file that is not text or nesting deeper than 8 makes the call uncacheable). abc-shim-path is hashed as one
  file.

A record holds the host name and the paths of the files used (for the reader; not part of the identity): a cache
directory that is shared shares them.

Entries are written under a temporary name and renamed, the record last, so a concurrent run sees a complete entry or
none. Nothing is ever read from an entry without its record.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

CACHE_FORMAT = 1
ENV_FAMILIES = re.compile(r"^(ABC|CIRCT|YOSYS|MLIR|LLVM|OMP|MALLOC|GLIBC|LD)_")
ENV_EXACT = ("LANG", "LC_ALL", "LC_NUMERIC", "LC_CTYPE", "LC_COLLATE", "TZ")

def sha_text(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


# abc-bridge options under which the mapper does not map (it imports a netlist somebody else wrote) or writes files
# beside its output that a cache hit would not write: such a call is never cached
UNCACHEABLE_KEYS = ("use-mapped-dir", "save-mapped-dir", "save-aig-dir")


class NotCacheable(Exception):
    """this mapper call cannot be given an identity that covers everything it reads or writes"""


def sha_liberty(p) -> str:
    """sha_file for a liberty; refuses one with an include_file (the included file is read too, and not followed here)"""
    h = hashlib.sha256()
    tail = b""
    with open(os.path.realpath(str(p)), "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
            if b"include_file" in tail + blk:
                raise NotCacheable(f"the liberty {p} has an include_file")
            tail = blk[-16:]
    return h.hexdigest()


def sha_file(p) -> str:
    """content hash, read every time: nothing is remembered by name, size or time stamp (a file rewritten within the
    file system's time-stamp resolution would otherwise be taken for the old one; a test caught exactly that)"""
    h = hashlib.sha256()
    with open(os.path.realpath(str(p)), "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _run_text(argv, timeout=60) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # the version text is one identification among several; its absence is recorded as such
        return f"<unavailable: {type(e).__name__}>"


def binary_identity(path, version_argv) -> dict:
    """content of the executable, its version text, and the content of every shared library the loader gives it"""
    rp = os.path.realpath(str(path))
    libs = {}
    ldd = _run_text(["ldd", rp])
    if "=>" not in ldd and "statically linked" not in ldd and "not a dynamic executable" not in ldd:
        raise NotCacheable(f"ldd gives no library list for {rp}")
    for ln in ldd.splitlines():
        m = re.match(r"\s*(\S+)\s+=>\s+(/\S+)", ln) or re.match(r"\s*(/\S+)\s+\(0x", ln)
        if m:
            lib = m.group(2) if m.lastindex == 2 else m.group(1)
            libs[os.path.basename(lib)] = sha_file(lib) if os.path.isfile(lib) else "<missing>"
    return {"sha256": sha_file(rp), "version": _run_text([rp] + list(version_argv)), "shared_libraries": libs}


# abc-bridge options whose value is a file the mapper reads: hashed by content, the name left out
FILE_KEYS = ("liberty", "stubs", "abc-script-file", "abc-shim-path")
SCRIPT_DEPTH = 8
SCRIPT_BIG = 4 << 20            # a larger file a script names is a library it reads: hashed, not read for names


def script_files(path, depth=0, seen=None) -> list:
    """[[name as written, sha256], ...] of every file an ABC script reaches: every word of the script (comments left
    out, as the mapper leaves them out) that names an existing file as written, beside the script, or beside the file
    the script is a link to, with or without an extension; and, in turn, the words of those files (a sourced script
    that sources another). Files are told apart by their real path, so one name that means different files at two
    levels gives two entries. A file larger than SCRIPT_BIG is taken by its content and not read for names. Raises
    NotCacheable when the script reaches something this cannot follow: the file of a `source` that is not found, a
    small file that is not text, more than SCRIPT_DEPTH levels."""
    seen = {} if seen is None else seen                  # real path -> name as first written
    if depth > SCRIPT_DEPTH:
        raise NotCacheable(f"the ABC script nests files more than {SCRIPT_DEPTH} deep ({path})")
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise NotCacheable(f"{path}, reached from the ABC script, cannot be read as text ({type(e).__name__})")
    text = re.sub(r"#[^\n]*", "", text)
    bases = {os.path.dirname(os.path.abspath(str(path))), os.path.dirname(os.path.realpath(str(path)))}
    words = re.findall(r"[^\s;\"']+", text)
    for n, word in enumerate(words):
        hits = [c for c in [word, *(os.path.join(b, word) for b in sorted(bases))] if os.path.isfile(c)]
        if not hits:
            # the file name of a `source` (after its flags, which start with "-") must exist
            k = n - 1
            while k >= 0 and words[k].startswith("-"):
                k -= 1
            if k >= 0 and words[k] == "source" and not word.startswith("-"):
                raise NotCacheable(f"the ABC script sources {word}, which is not found")
            continue
        for real in sorted({os.path.realpath(h) for h in hits}):      # every file the word can mean
            if real in seen:
                continue
            seen[real] = word
            if os.path.getsize(real) <= SCRIPT_BIG:
                script_files(real, depth + 1, seen)
    if depth:
        return seen
    return sorted([w, sha_file(r)] for r, w in seen.items())


def option_identity(abc_bridge_opt: str):
    """the abc-bridge option string, token by token. A file the mapper reads (FILE_KEYS) becomes its content hash, and for
    the ABC script the hashes of every file it reaches (script_files); the ABC binary its binary identity; every other
    option its literal value. Returns (identity, names, abc path)."""
    s = abc_bridge_opt.strip()
    if s.startswith("--abc-bridge="):
        s = s[len("--abc-bridge="):]
    ident, names, abc_path = [], {}, None
    for tok in s.split():
        k, eq, v = tok.partition("=")
        if not eq:
            ident.append({"flag": tok}); continue
        if k in UNCACHEABLE_KEYS:
            raise NotCacheable(f"abc-bridge option {k} (the mapper reads or writes netlists outside its output)")
        if k == "abc-path":
            # as the mapper resolves it: a name without a directory through PATH, anything else as the path it is
            abc_path = shutil.which(v) if "/" not in v else (v if os.path.isfile(v) else None)
            if not abc_path:
                raise NotCacheable(f"abc-path {v} is not found")
            ident.append({"key": k, "binary": binary_identity(abc_path, ["-q", "version"])}); names[k] = abc_path
        elif k in FILE_KEYS:
            if not os.path.isfile(v):
                raise NotCacheable(f"abc-bridge option {k}={v}: not a file")
            entry = {"key": k, "file_sha256": sha_liberty(v) if k == "liberty" else sha_file(v)}
            if k == "abc-script-file":
                entry["files_reached_from_script"] = script_files(v)
            ident.append(entry); names[k] = v
        elif "/" in v and os.path.exists(v):
            # an option this does not know that names something on disk: its content is not followed
            raise NotCacheable(f"abc-bridge option {k} names {'a directory' if os.path.isdir(v) else 'a file'} ({v}) "
                               "that the cache does not know how to follow")
        else:
            ident.append({"key": k, "value": v})
    if abc_path is None:
        raise NotCacheable("no abc-path in the abc-bridge options (the mapper would take ABC from PATH)")
    return ident, names, abc_path


def identity(name: str, iso_text: str, circt_abc_opt, abc_bridge_opt: str, extra_code=()) -> tuple:
    """(identity dict, names dict). The key is the hash of the identity; names are for the record only."""
    try:
        return _identity(name, iso_text, circt_abc_opt, abc_bridge_opt, extra_code)
    except OSError as e:
        # something the identity has to read cannot be read (an rc file, the working directory, a library)
        raise NotCacheable(f"{type(e).__name__}: {e}")


def _identity(name: str, iso_text: str, circt_abc_opt, abc_bridge_opt: str, extra_code=()) -> tuple:
    opts, names, _abc = option_identity(abc_bridge_opt)
    from . import abc_partition                     # builds the mapper's command line (_map_one)
    code = {os.path.basename(p): sha_file(p) for p in [__file__, abc_partition.__file__, *extra_code]}
    # Files ABC opens by a fixed or derived name, not through an option (as a system-call trace of a mapping shows):
    # .abc.rc and abc.rc in the working directory, .abc.rc in the home directory, and "<liberty library name>.skip" in the
    # working directory (read_lib's list of cells to leave out). Every *.skip of the working directory is taken.
    cwd = os.getcwd()
    rc = {}
    for where, p in (("cwd/.abc.rc", os.path.join(cwd, ".abc.rc")), ("cwd/abc.rc", os.path.join(cwd, "abc.rc")),
                     ("home/.abc.rc", os.path.join(os.path.expanduser("~"), ".abc.rc"))):
        # an rc file is a script: its own content and every file it reaches
        rc[where] = [sha_file(p), script_files(p)] if os.path.isfile(p) else None
    skip = {f: sha_file(os.path.join(cwd, f)) for f in sorted(os.listdir(cwd)) if f.endswith(".skip") and os.path.isfile(os.path.join(cwd, f))}
    ident = {
        "cache_format": CACHE_FORMAT,
        "cache_code": code,
        "target": name,
        "input_sha256": sha_text(iso_text),
        "mapper": binary_identity(circt_abc_opt, ["--version"]),
        "mapper_fixed_arguments": ["--mlir-disable-threading", "emit-target-only=true"],
        "abc_bridge_options": opts,
        "abc_rc": rc,
        "abc_skip_files": skip,
        "environment": {k: v for k, v in sorted(os.environ.items()) if ENV_FAMILIES.match(k) or k in ENV_EXACT},
        "platform": {"machine": platform.machine(), "system": platform.system(), "libc": list(platform.libc_ver())},
    }
    names["circt_abc_opt"] = str(circt_abc_opt)
    return ident, names


def key_of(ident: dict) -> str:
    return sha_text(json.dumps(ident, sort_keys=True))


class Cache:
    def __init__(self, root, verify: bool = False, log=print):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.verify = verify
        self.log = log
        self.report = []

    def paths(self, key):
        d = self.root / key[:2]
        return d / f"{key}.mlir", d / f"{key}.json"

    def lookup(self, name, ident):
        """the cached mapped text, or None with the reason it cannot be used"""
        key = key_of(ident)
        mlir, rec_p = self.paths(key)
        if not (mlir.is_file() and rec_p.is_file()):
            return key, None, "no entry"
        try:
            rec = json.loads(rec_p.read_text())
        except Exception as e:
            return key, None, f"unreadable record ({type(e).__name__})"
        if not isinstance(rec, dict):
            return key, None, "the record is not a JSON object"
        if rec.get("key") != key:
            return key, None, "the record names another key"
        stored = rec.get("identity")
        want = json.loads(json.dumps(ident))
        if not isinstance(stored, dict):
            return key, None, "the record's identity is not an object"
        if stored != want:                                   # the whole identity, field by field
            diff = sorted(k for k in set(stored) | set(want) if stored.get(k) != want.get(k))
            return key, None, "identity differs from the record in " + ", ".join(diff)
        try:
            text = mlir.read_text()
        except (OSError, UnicodeDecodeError) as e:
            return key, None, f"the stored module cannot be read ({type(e).__name__})"
        if sha_text(text) != rec.get("output_sha256"):
            return key, None, "the stored module does not have the recorded checksum"
        if not re.search(r"^  hw\.module(?: private| public)? @%s\(" % re.escape(name), text, re.M):
            return key, None, "the stored file has no definition of the target module"
        return key, text, "hit"

    def store(self, name, key, ident, names, text, seconds):
        mlir, rec_p = self.paths(key)
        mlir.parent.mkdir(parents=True, exist_ok=True)
        tag = f".{os.getpid()}.{time.time_ns()}.tmp"
        rec = {"key": key, "target": name, "identity": ident, "names": names, "output_sha256": sha_text(text),
               "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": platform.node(), "map_seconds": round(seconds, 1)}
        Path(str(mlir) + tag).write_text(text); os.replace(str(mlir) + tag, mlir)
        Path(str(rec_p) + tag).write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n"); os.replace(str(rec_p) + tag, rec_p)


def isolated_input(text: str, blocks: dict, name: str, extern_re, leave_out=()) -> str:
    """the cell declarations of the design, in their order, and the one module: all the mapper is given for this target.
    `leave_out`: extern declarations that are not cells (modules mapped by themselves earlier, hidden as externs): the
    mapper echoes every declaration it is given, and such a one would come back beside the module's real definition"""
    skip = set(leave_out)
    ext = "".join(m.group(0) + "\n" for m in extern_re.finditer(text) if m.group(1) not in skip)
    s, e = blocks[name]
    return "module {\n" + ext + text[s:e] + "}\n"


def map_cached(cache, name, text, blocks, workdir, circt_abc_opt, abc_bridge_opt, map_one, extern_re, extra_code=(),
               leave_out=()):
    """Map `name` from its isolated input, through the cache when one is given. Returns (mapped text, seconds, status)."""
    workdir = Path(workdir)
    iso = isolated_input(text, blocks, name, extern_re, leave_out)
    iso_p = workdir / f"{name}.in.mlir"
    iso_p.write_text(iso)

    def fresh():
        r = map_one((name, iso_p, workdir, str(circt_abc_opt), abc_bridge_opt))
        if r.returncode != 0:
            raise RuntimeError(f"map_once: {name} failed to map (see {r.log})")
        return r.output.read_text(), r.seconds

    if cache is None:
        out, sec = fresh()
        return out, sec, "mapped (no cache)"
    try:
        ident, names = identity(name, iso, circt_abc_opt, abc_bridge_opt, extra_code)
    except NotCacheable as e:
        out, sec = fresh()
        cache.report.append({"module": name, "key": None, "status": f"mapped, not cacheable: {e}", "seconds": round(sec, 1)})
        return out, sec, f"not cacheable ({e})"
    key, cached, why = cache.lookup(name, ident)
    entry = {"module": name, "key": key}
    if cached is not None and not cache.verify:
        (workdir / f"{name}.mlir").write_text(cached)
        entry.update(status="hit", seconds=0.0); cache.report.append(entry)
        return cached, 0.0, "hit"
    out, sec = fresh()
    if cached is not None:                                    # verify mode
        if out != cached:
            bad = workdir / f"{name}.cache_mismatch.mlir"
            bad.write_text(cached)
            raise SystemExit(f"syn.abc.partition.map_cache_verify: {name}: the cache entry {key} differs from a fresh mapping of the same identity "
                             f"(cached copy kept as {bad}). The mapper is not deterministic for this input, or it reads something the "
                             "identity does not hold; the cache must not be used until this is understood.")
        entry.update(status="hit, verified by mapping", seconds=round(sec, 1)); cache.report.append(entry)
        return out, sec, "hit, verified"
    # stored only when the identity is the same after the mapping as before it: an input rewritten while the mapper ran
    # must not be recorded under the identity taken before
    try:
        same = key_of(identity(name, iso, circt_abc_opt, abc_bridge_opt, extra_code)[0]) == key
    except NotCacheable:
        same = False
    if not same:
        entry.update(status="miss: " + why + "; NOT stored: an input changed while the mapper ran", seconds=round(sec, 1))
        cache.report.append(entry)
        return out, sec, "miss (" + why + "; not stored: an input changed during the mapping)"
    try:
        cache.store(name, key, ident, names, out, sec)
    except OSError as e:
        # the mapping is good; a cache that cannot be written must not cost the run
        entry.update(status=f"miss: {why}; NOT stored: {type(e).__name__}: {e}", seconds=round(sec, 1))
        cache.report.append(entry)
        return out, sec, f"miss ({why}; not stored: {type(e).__name__})"
    entry.update(status="miss: " + why, seconds=round(sec, 1)); cache.report.append(entry)
    return out, sec, "miss (" + why + ")"
