"""The two Python entry points of the session API (doc/scripting.md):

    python3 -m circt_flow run RECIPE.py [FLOW.yaml OUT_DIR] [options] [-- ARGS...]
    python3 -m circt_flow shell [FLOW.yaml OUT_DIR] [options]

`run` executes a recipe written as a Python script. With FLOW.yaml and
OUT_DIR given, the script finds a session ready in the globals `s` (also
`session`); a script that defines `recipe(s)` has it called with that
session after the script body. Without them the script makes its own
Session(...). Either way the script gets `Session`, `argv` (the arguments
after `--`) and `options`; every run directory a session of the script
wrote gets a copy of the script (<out>/recipe.py: what ran is kept with what
it produced; a second, different script writing into the same directory is
kept as recipe_2.py, and so on, never over the first).

`shell` is a normal Python REPL (code.InteractiveConsole) with the same
globals; tab completion completes attribute paths (s.db.syn.vt<TAB>) and
option names inside a string (s.set("syn.vt<TAB>), and help("syn.vt_swap")
prints the options of a name or namespace. Reading commands from a pipe
works the same (non-interactive use, tests).

Options of both: --set NAME=VALUE (repeatable), --settings SETTINGS_JSON,
--strict, --resume OUT_DIR (re-open a session's run directory), and the
command line's flow flags --vt-swap, --sta, --abc-script-file PATH.
"""
from __future__ import annotations

import argparse
import code
import runpy
import sys
import weakref
from pathlib import Path
from typing import List, Optional

from . import options
from .session import Session, SessionError

_CREATED: "weakref.WeakSet" = weakref.WeakSet()


def _parser(prog: str, script: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"circt_flow {prog}")
    if script:
        ap.add_argument("script", help="the recipe (a Python file)")
    ap.add_argument("config", nargs="?", help="flow.yaml (with out_dir: a session is preloaded)")
    ap.add_argument("out_dir", nargs="?", help="run directory")
    ap.add_argument("--set", dest="set_options", action="append", default=[],
                    metavar="NAME=VALUE", help="set an option (repeatable)")
    ap.add_argument("--settings", metavar="SETTINGS_JSON",
                    help="the options of an earlier run's settings.json")
    ap.add_argument("--strict", action="store_true",
                    help="refuse legacy CIRCT_FLOW_* / CIRCT_VT_* variables")
    ap.add_argument("--resume", metavar="OUT_DIR",
                    help="re-open a run directory a session wrote (instead of FLOW.yaml OUT_DIR)")
    ap.add_argument("--allow-changed-inputs", action="store_true",
                    help="with --resume: continue although flow.yaml / tech pack / legacy variables / "
                         "circt-abc commit changed since the stages done ran (warned)")
    ap.add_argument("--vt-swap", action="store_true", help="as the command line's --vt-swap")
    ap.add_argument("--sta", action="store_true", help="as the command line's --sta")
    ap.add_argument("--abc-script-file", metavar="PATH", help="as the command line's flag")
    return ap


def _session(a) -> Optional[Session]:
    kw = {}
    if a.vt_swap:
        kw["run_vt_swap"] = True
    if a.sta:
        kw["run_sta"] = True
    if a.abc_script_file:
        kw["abc_script_file"] = a.abc_script_file
    given, pack = options.split_assignments(a.set_options)
    if a.resume:
        if a.config or a.settings:
            raise SessionError("--resume takes no FLOW.yaml / OUT_DIR / --settings")
        return _pack_set(Session.resume(a.resume, settings=given,
                                        allow_changed_inputs=a.allow_changed_inputs, **kw), pack)
    if a.allow_changed_inputs:
        raise SessionError("--allow-changed-inputs goes with --resume")
    if a.config is None:
        if given or pack or a.settings or kw:
            raise SessionError("--set / --settings / flow flags need FLOW.yaml OUT_DIR")
        return None
    if a.out_dir is None:
        raise SessionError("FLOW.yaml needs an OUT_DIR")
    if a.settings:
        s = Session.from_settings(a.config, a.out_dir, a.settings, strict=a.strict, **kw)
        for k, v in given.items():
            s._set(k, v, where="--set")
        return _pack_set(s, pack)
    return _pack_set(Session(a.config, a.out_dir, settings=given, strict=a.strict, **kw), pack)


def _pack_set(s: Session, pack) -> Session:
    """`--set NAME=VALUE` for the names that are not circt-abc's own options:
    the tech pack's P&R options (Session.set_pack_option; the same checks, and
    like every option, one of a stage that has already run is refused)."""
    if not pack:
        return s
    import difflib
    reg = s._pack_registry() or {}
    for k, v in pack:
        if k not in reg:
            near = difflib.get_close_matches(k, [*options.registry(), *reg], 3, 0.5)
            hint = f" (did you mean {', '.join(near)}?)" if near else ""
            where = ("and not one of the tech pack's P&R options (`python3 -m circt_flow options --pack <pack>`)"
                     if reg else "and the tech pack declares no P&R option registry")
            raise options.OptionError(f"--set: unknown option {k!r}{hint}: not an option of circt-abc "
                                      f"(`python3 -m circt_flow options`) {where}")
        s.set_pack_option(k, v)
        s._set_at[f"pack:{k}"] = "--set"
    return s


class _Help:
    """help(x): an option or namespace name prints its options; anything else
    is the builtin help."""

    def __init__(self, s: Optional[Session]):
        self._s = s

    def __repr__(self):
        return ("Type help('syn.vt_swap') for the options of a namespace, "
                "help('syn.vt_swap.recover_order') for one option, help(obj) as usual.")

    def __call__(self, *a, **kw):
        from .db import Node, AttrDB
        if len(a) == 1 and not kw:
            x = a[0]
            if isinstance(x, str) and (x in options.registry()
                                       or any(n.startswith(x.rstrip(".") + ".")
                                              for n in options.registry())):
                if self._s is not None:
                    print(self._s.db.help(x))
                else:
                    print("\n".join(f"{n}: {o.help}" for n, o in sorted(options.registry().items())
                                    if n == x or n.startswith(x.rstrip(".") + ".")))
                return None
            if isinstance(x, (Node, AttrDB)):
                print(type(x).__doc__ or "")
                if isinstance(x, Node):
                    print(x.report().text())
                return None
        import builtins
        import pydoc
        return pydoc.help(*a, **kw) if a or kw else builtins.help()


def namespace(s: Optional[Session], argv: Optional[List[str]] = None) -> dict:
    ns = {"Session": Session, "SessionError": SessionError, "options": options,
          "argv": list(argv or []), "help": _Help(s), "__name__": "__main__"}
    if s is not None:
        ns.update(s=s, session=s, db=s.db)
    return ns


def _track_sessions():
    """Record every Session the script makes (for the script copy)."""
    orig = Session.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        _CREATED.add(self)
    Session.__init__ = init
    return orig


def run_main(argv: List[str]) -> int:
    extra: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = _parser("run", True).parse_args(argv)
    script = Path(a.script)
    if not script.is_file():
        print(f"ERROR: {script}: no such file", file=sys.stderr)
        return 2
    orig = _track_sessions()
    try:
        s = _session(a)
        if s is not None:
            _CREATED.add(s)
        ns = namespace(s, extra)
        ns["__file__"] = str(script.resolve())
        sys.argv = [str(script)] + extra
        glb = runpy.run_path(str(script), init_globals=ns, run_name="__main__")
        if callable(glb.get("recipe")):
            if s is None:
                raise SessionError(f"{script} defines recipe(s): give FLOW.yaml OUT_DIR "
                                   "(or --resume) so a session is made for it")
            glb["recipe"](s)
    except (options.OptionError, SessionError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        Session.__init__ = orig
        for ses in list(_CREATED):
            try:
                if Path(ses.out_dir).is_dir():
                    keep_script(script, Path(ses.out_dir))
            except OSError:
                pass
        _CREATED.clear()
    return 0


def keep_script(script: Path, out_dir: Path) -> Path:
    """Copy the script into the run directory as recipe.py; when another
    script (or another version of it) already wrote recipe.py there, as
    recipe_<n>.py (the first free n from 2; a copy with the same content is
    not made twice): every script that wrote into the directory is kept."""
    data = Path(script).read_bytes()
    n = 1
    while True:
        dst = out_dir / ("recipe.py" if n == 1 else f"recipe_{n}.py")
        if not dst.exists():
            dst.write_bytes(data)
            if n > 1:
                print(f"[circt_flow] {out_dir / 'recipe.py'} holds another script: this one is "
                      f"kept as {dst.name}", file=sys.stderr)
            return dst
        if dst.read_bytes() == data:
            return dst
        n += 1


class _Completer:
    """rlcompleter, plus option names inside a string literal."""

    def __init__(self, ns):
        import rlcompleter
        self._rl = rlcompleter.Completer(ns)
        self._names = sorted(options.registry())

    def complete(self, text, state):
        try:
            import readline
            line = readline.get_line_buffer()[:readline.get_begidx()]
        except Exception:
            line = ""
        if line.endswith(("'", '"')):
            m = [n for n in self._names if n.startswith(text)]
            return m[state] if state < len(m) else None
        return self._rl.complete(text, state)


def shell_main(argv: List[str]) -> int:
    a = _parser("shell", False).parse_args(argv)
    try:
        s = _session(a)
    except (options.OptionError, SessionError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    ns = namespace(s)
    if sys.stdin.isatty():
        try:
            import readline
            readline.set_completer(_Completer(ns).complete)
            readline.set_completer_delims(" \t\n`~!@#$%^&*()=+[{]}\\|;:'\",<>?")
            readline.parse_and_bind("tab: complete")
        except ImportError:
            pass
    banner = ("circt_flow shell: s = " + repr(s) if s is not None else
              "circt_flow shell: no session (Session(flow_yaml, out_dir) makes one)")
    banner += "\n  s.db.<TAB>  help('syn.vt_swap')  s.stages  s.elaborate()  s.report_changed()"
    con = _Console(ns)
    con.interact(banner=banner, exitmsg="")
    # commands piped in: an error anywhere is the exit status (as a script's)
    return 1 if con.errors and not sys.stdin.isatty() else 0


class _Console(code.InteractiveConsole):
    """InteractiveConsole that counts the errors it showed."""
    errors = 0

    def showtraceback(self):
        self.errors += 1
        super().showtraceback()

    def showsyntaxerror(self, filename=None, **kw):
        self.errors += 1
        super().showsyntaxerror(filename, **kw)
