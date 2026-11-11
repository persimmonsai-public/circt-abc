"""Pack-declared wire RC: the per-corner OpenROAD RC Tcl a tech pack names,
checked like its libraries and handed to the stages that need wire RC.

A placed design's wire capacitance and resistance come from per-layer RC
(OpenROAD's `set_layer_rc`) and, before routing, one per-unit-length RC for
every signal and clock wire (`set_wire_rc`). Those numbers belong to the
technology and the extraction corner, so a pack declares them, one Tcl file
per corner (doc/techpacks.md, "Wire RC"):

    wire_rc:
      tt: ${MYTECH_RC}/rc_typical.tcl              # a path, or
      ff:                                          # a path and its pin
        path: ${MYTECH_RC}/rc_cmin.tcl
        sha256: 3f5c...

A private pack names files only: the numbers stay in the PDK's tree, and
the pack carries at most their sha256. A `public: true` pack may instead
give the values inline (ohm and fF per micron; illustrative values):

    wire_rc:
      tt:
        signal: {res_ohm_per_um: 20, cap_ff_per_um: 0.2}
        clock:  {res_ohm_per_um: 10, cap_ff_per_um: 0.1}   # optional
        layers: {M2: {res_ohm_per_um: 25, cap_ff_per_um: 0.2}}
        vias:   {V1: {res_ohm: 10}}

In a pack without `corners:`, `wire_rc:` is a single entry (a path, or
`{path, sha256}`, or the inline mapping). With corners, it maps corner
names to entries and must cover the setup and the hold corner.

Who uses it:

* P&R: the stage gives OpenROAD the setup corner's file. ORFS gets it as
  SET_RC_TCL on the make command line (which overrides the platform's
  setRC.tcl); a pack's own P&R command gets CIRCT_FLOW_WIRE_RC (the setup
  corner's file) and CIRCT_FLOW_WIRE_RC_<CORNER> for every corner.
* STA and vt_swap time a netlist without a placement, so RC per micron
  means nothing to them on its own. With a wire model (flow.wire_model,
  wire_model.py) that names `rc_corner:`, the corner its capacitances were
  fitted at, each view scales the model by the corner's signal RC against
  that corner's: C x c_signal(view) / c_signal(rc_corner), R likewise. The
  per-micron values are read from each file in a child Tcl interpreter in
  which set_layer_rc / set_wire_rc only record their arguments, so the
  file's units cancel (all files of a pack must use the same units).

`check()` is what `techpack check` prints: per corner the file, its size
and sha256, the pin's verdict, and the coverage of the setup and hold
corners.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The single entry of a pack without corners.
IMPLICIT = "default"
FILE_KEYS = {"path", "sha256"}
INLINE_KEYS = {"signal", "clock", "layers", "vias"}
PER_UM = {"res_ohm_per_um", "cap_ff_per_um"}


class WireRcError(ValueError):
    pass


@dataclass(frozen=True)
class RcEntry:
    corner: str
    path: Optional[Path] = None
    sha256: Optional[str] = None
    # Public packs only: {"signal": {...}, "clock": {...}, "layers": {...},
    # "vias": {...}} in ohm and fF per micron.
    inline: Optional[dict] = None

    def describe(self) -> str:
        return str(self.path) if self.path is not None else "(inline)"


@dataclass(frozen=True)
class WireRc:
    entries: Dict[str, RcEntry] = field(default_factory=dict)

    def at(self, corner: Optional[str]) -> Optional[RcEntry]:
        """The entry for `corner` (the single entry when the pack has no
        corners), or None when the pack does not cover it."""
        if corner is None or corner not in self.entries:
            return self.entries.get(IMPLICIT)
        return self.entries[corner]

    def to_dict(self) -> dict:
        return {c: ({"path": str(e.path), "sha256": file_sha256(e.path)}
                    if e.path is not None else {"inline": e.inline})
                for c, e in self.entries.items()}


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _num_map(d, keys, where: str) -> dict:
    if not isinstance(d, dict) or set(d) != set(keys):
        raise WireRcError(f"{where} must be a mapping of "
                          f"{', '.join(sorted(keys))}")
    for k, v in d.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise WireRcError(f"{where}.{k} must be a number >= 0, got {v!r}")
    return {k: float(v) for k, v in d.items()}


def _inline(d: dict, where: str) -> dict:
    out: dict = {}
    if "signal" not in d:
        raise WireRcError(f"{where}: inline wire RC needs signal: "
                          "{res_ohm_per_um, cap_ff_per_um}")
    for k in ("signal", "clock"):
        if k in d:
            out[k] = _num_map(d[k], PER_UM, f"{where}.{k}")
    for k, keys in (("layers", PER_UM), ("vias", {"res_ohm"})):
        if k in d:
            if not isinstance(d[k], dict):
                raise WireRcError(f"{where}.{k} must map names to values")
            out[k] = {str(n): _num_map(v, keys, f"{where}.{k}.{n}")
                      for n, v in d[k].items()}
    return out


def _entry(v, corner: str, base: Path, public: bool, where: str) -> RcEntry:
    from .profile import expand
    if isinstance(v, (str, Path)):
        return RcEntry(corner, path=expand(str(v), base))
    if not isinstance(v, dict) or not v:
        raise WireRcError(f"{where}: give a path, {{path, sha256}} or (a "
                          "public pack) the inline values")
    keys = set(v)
    if keys & FILE_KEYS:
        if keys - FILE_KEYS or "path" not in v:
            raise WireRcError(f"{where}: a file entry takes path and an "
                              f"optional sha256, got {', '.join(sorted(v))}")
        sha = v.get("sha256")
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
            raise WireRcError(f"{where}.sha256 must be 64 lowercase hex "
                              "digits")
        return RcEntry(corner, path=expand(str(v["path"]), base),
                       sha256=sha)
    bad = sorted(keys - INLINE_KEYS)
    if bad:
        raise WireRcError(f"{where}.{bad[0]} is not a wire_rc key; a corner "
                          "takes path/sha256 or (inline) "
                          + ", ".join(sorted(INLINE_KEYS)))
    if not public:
        raise WireRcError(f"{where}: inline RC values are for public: true "
                          "packs only; a private pack names the PDK's file "
                          "(path, sha256)")
    return RcEntry(corner, inline=_inline(v, where))


def parse(d, base: Path, corner_names, public: bool,
          where: str) -> Optional[WireRc]:
    """A pack's `wire_rc:` (None when absent)."""
    if d is None:
        return None
    single = (isinstance(d, (str, Path))
              or (isinstance(d, dict) and set(d) & (FILE_KEYS | INLINE_KEYS)))
    if not corner_names:
        if not single:
            raise WireRcError(f"{where}: a pack without corners: takes one "
                              "entry (a path, {path, sha256}, or inline "
                              "values), not a mapping of corners")
        return WireRc({IMPLICIT: _entry(d, IMPLICIT, base, public, where)})
    if single or not isinstance(d, dict) or not d:
        raise WireRcError(f"{where}: the pack has corners "
                          f"({', '.join(corner_names)}); map each corner to "
                          "its entry")
    extra = sorted(set(map(str, d)) - set(corner_names))
    if extra:
        raise WireRcError(f"{where}: unknown corner(s) {', '.join(extra)}; "
                          f"corners.names: {', '.join(corner_names)}")
    ents = {str(c): _entry(v, str(c), base, public, f"{where}.{c}")
            for c, v in d.items()}
    kinds = {e.path is None for e in ents.values()}
    if len(kinds) > 1:
        raise WireRcError(f"{where}: give every corner as a file or every "
                          "corner inline, not a mix (the STA scaling "
                          "compares corners in one unit)")
    return WireRc(ents)


def required_corners(prof) -> List[str]:
    """The corners a pack's wire_rc must cover: setup and hold."""
    if not prof.corner_names:
        return [IMPLICIT]
    return list(dict.fromkeys(
        c for c in (prof.setup_corner, prof.hold_corner) if c))


# The probe: read an RC Tcl file in a child interpreter where the OpenROAD
# commands only record their arguments, and return the signal wire's
# {resistance capacitance} per unit length, in the file's own units (a bare
# `set_wire_rc` without -signal/-clock sets both). `env_str NAME DEFAULT` is
# defined here as a helper an RC file may call: the value of environment
# variable NAME, else DEFAULT.
PROBE = r'''
proc circtflow_rc_signal {file} {
  set i [interp create]
  $i eval {
    set ::circtflow_rc {}
    proc env_str {name {default ""}} {
      if {[info exists ::env($name)]} { return $::env($name) }
      return $default
    }
    proc set_layer_rc {args} {}
    proc set_wire_rc {args} {
      set kind both; set r ""; set c ""
      for {set k 0} {$k < [llength $args]} {incr k} {
        set a [lindex $args $k]
        switch -- $a {
          -signal { set kind signal }
          -clock { set kind clock }
          -resistance { incr k; set r [lindex $args $k] }
          -capacitance { incr k; set c [lindex $args $k] }
          -layer { incr k }
          -layers { incr k }
        }
      }
      if {$kind ne "clock"} { set ::circtflow_rc [list $r $c] }
    }
    proc puts {args} {}
  }
  set err [catch {$i eval [list source $file]} msg]
  set rc [$i eval {set ::circtflow_rc}]
  interp delete $i
  if {$err} { error "wire_rc $file: $msg" }
  if {[llength $rc] != 2 || [lindex $rc 0] eq "" || [lindex $rc 1] eq ""} {
    error "wire_rc $file: no set_wire_rc -signal with -resistance and -capacitance"
  }
  return $rc
}
'''


def probe(path: Path, tclsh: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """The file's signal (resistance, capacitance) per unit length, read with
    a local tclsh; None when there is no tclsh. Raises WireRcError when the
    file does not define one."""
    tclsh = tclsh or shutil.which("tclsh")
    if tclsh is None:
        return None
    import tempfile
    script = PROBE + f"puts [circtflow_rc_signal {{{Path(path).resolve()}}}]\n"
    with tempfile.NamedTemporaryFile("w", suffix=".tcl") as t:
        t.write(script)
        t.flush()
        p = subprocess.run([tclsh, t.name], capture_output=True, text=True)
    words = p.stdout.split()
    if p.returncode != 0 or len(words) < 2:
        msg = (p.stderr or p.stdout).strip()
        raise WireRcError(msg.splitlines()[0] if msg
                          else f"wire_rc {path}: tclsh failed")
    try:
        return float(words[0]), float(words[1])
    except ValueError:
        raise WireRcError(f"wire_rc {path}: set_wire_rc -signal values "
                          f"{words[0]!r} {words[1]!r} are not numbers") from None


@dataclass(frozen=True)
class Scale:
    """How one view scales a wire model: Tcl that sets up the factors, and
    the Tcl expressions for the capacitance and resistance factors."""
    setup_tcl: str
    c_expr: str
    r_expr: str
    corner: str
    ref: str


def scale(rc: Optional[WireRc], corner: Optional[str],
          ref: Optional[str]) -> Optional[Scale]:
    """The scaling of a wire model fitted at corner `ref` for a view at
    `corner`; None when there is nothing to scale (no wire_rc or no ref)."""
    if rc is None or ref is None or IMPLICIT in rc.entries:
        # A pack without corners has one RC: nothing to scale between.
        return None
    at, base = rc.at(corner), rc.at(ref)
    if base is None:
        raise WireRcError(f"wire_model.rc_corner {ref!r} has no wire_rc "
                          "entry; wire_rc corners: " + ", ".join(rc.entries))
    if at is None:
        raise WireRcError(f"wire_rc has no entry for corner {corner!r}, "
                          f"which a view times with the wire model "
                          f"(fitted at {ref}); wire_rc corners: "
                          + ", ".join(rc.entries))
    cname = corner if corner is not None else IMPLICIT
    if at.inline is not None:
        s, b = at.inline["signal"], base.inline["signal"]
        fc = s["cap_ff_per_um"] / b["cap_ff_per_um"] if b["cap_ff_per_um"] else 1.0
        fr = (s["res_ohm_per_um"] / b["res_ohm_per_um"]
              if b["res_ohm_per_um"] else 1.0)
        return Scale("", f"{fc:.9g}", f"{fr:.9g}", cname, ref)
    setup = (PROBE
             + f"set circtflow_rc_at [circtflow_rc_signal {{{at.path}}}]\n"
             + f"set circtflow_rc_ref [circtflow_rc_signal {{{base.path}}}]\n")
    c = ("([lindex $circtflow_rc_at 1] / double([lindex $circtflow_rc_ref 1]))")
    r = ("([lindex $circtflow_rc_ref 0] > 0 ? [lindex $circtflow_rc_at 0] / "
         "double([lindex $circtflow_rc_ref 0]) : 1.0)")
    return Scale(setup, c, r, cname, ref)


def openroad_tcl(entry: RcEntry) -> str:
    """Tcl for an OpenROAD session (P&R): the file sourced after defining
    `env_str` (as in the probe, if the session has none), or the inline values as set_layer_rc / set_wire_rc in the
    session's units."""
    if entry.path is not None:
        return ("# circt_flow wire_rc: " + entry.corner + "\n"
                "if {[info procs env_str] eq \"\" && [info commands env_str] eq \"\"} {\n"
                "  proc env_str {name {default \"\"}} {\n"
                "    if {[info exists ::env($name)]} { return $::env($name) }\n"
                "    return $default\n  }\n}\n"
                f"source {{{entry.path}}}\n")
    d = entry.inline
    lines = ["# circt_flow wire_rc: " + entry.corner + " (inline, public pack)",
             "set circtflow_r [expr {1.0 / [sta::unit_scale resistance]}]",
             "set circtflow_c [expr {1e-15 / [sta::unit_scale capacitance]}]"]

    def rc(v):
        return (f"-resistance [expr {{{v['res_ohm_per_um']:g} * $circtflow_r}}] "
                f"-capacitance [expr {{{v['cap_ff_per_um']:g} * $circtflow_c}}]")
    for n, v in sorted((d.get("layers") or {}).items()):
        lines.append(f"set_layer_rc -layer {n} {rc(v)}")
    for n, v in sorted((d.get("vias") or {}).items()):
        lines.append(f"set_layer_rc -via {n} -resistance "
                     f"[expr {{{v['res_ohm']:g} * $circtflow_r}}]")
    lines.append(f"set_wire_rc -signal {rc(d['signal'])}")
    lines.append(f"set_wire_rc -clock {rc(d.get('clock', d['signal']))}")
    return "\n".join(lines) + "\n"


def write_openroad_tcl(entry: RcEntry, dest: Path) -> Path:
    """Write the P&R Tcl for `entry` to `dest`. A file entry is copied beside
    it (dest's stem + _src.tcl), so a container that mounts only the run
    directory can read it."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if entry.path is not None:
        src = dest.with_name(dest.stem + "_src.tcl")
        shutil.copyfile(entry.path, src)
        entry = RcEntry(entry.corner, path=src.resolve())
    dest.write_text(openroad_tcl(entry))
    return dest


def pnr_env(rc: Optional[WireRc], setup: Optional[str],
            out_dir: Path) -> Dict[str, str]:
    """Environment for a pack's own P&R command: CIRCT_FLOW_WIRE_RC (the
    setup corner's OpenROAD Tcl) and CIRCT_FLOW_WIRE_RC_<CORNER> per corner.
    Inline entries are written out under out_dir."""
    if rc is None:
        return {}
    env: Dict[str, str] = {}
    for c, e in rc.entries.items():
        p = (write_openroad_tcl(e, Path(out_dir) / f"wire_rc_{c}.tcl")
             if e.path is None else e.path)
        env[f"CIRCT_FLOW_WIRE_RC_{re.sub(r'[^A-Za-z0-9]', '_', c).upper()}"] = str(p)
    first = rc.at(setup)
    if first is not None:
        env["CIRCT_FLOW_WIRE_RC"] = env[
            f"CIRCT_FLOW_WIRE_RC_{re.sub(r'[^A-Za-z0-9]', '_', first.corner).upper()}"]
    return env


def check(prof, need_signal: bool = False) -> List[str]:
    """`techpack check`'s wire_rc lines; returns the problems. Each file must
    exist, be non-empty and match its sha256 pin; the setup and hold corners
    must be covered. With a local tclsh, each file must also define the
    signal wire (an error when `need_signal`: a wire model scales by it)."""
    rc = getattr(prof, "wire_rc", None)
    if rc is None:
        return []
    problems: List[str] = []
    for c in required_corners(prof):
        if c not in rc.entries:
            role = ("setup" if c == prof.setup_corner else "hold")
            problems.append(f"wire_rc: no entry for the {role} corner {c}")
    for c, e in rc.entries.items():
        where = f"wire_rc.{c}" if c != IMPLICIT else "wire_rc"
        if e.path is None:
            print(f"  {where}: inline (public pack), signal "
                  f"{e.inline['signal']}")
            continue
        if not e.path.is_file():
            problems.append(f"{where}: {e.path} does not exist")
            continue
        size = e.path.stat().st_size
        if size == 0:
            problems.append(f"{where}: {e.path} is empty")
            continue
        sha = file_sha256(e.path)
        verdict = ("pinned, ok" if e.sha256 == sha
                   else "not pinned" if e.sha256 is None else "PIN MISMATCH")
        print(f"  {where}: {e.path} ({size} bytes, sha256 {sha}): {verdict}")
        if e.sha256 is not None and e.sha256 != sha:
            problems.append(f"{where}: {e.path} has sha256 {sha}, the pack "
                            f"pins {e.sha256} (the file changed since it was "
                            "pinned)")
        try:
            got = probe(e.path)
        except WireRcError as x:
            if need_signal:
                problems.append(str(x))
            else:
                print(f"  WARNING: {x}")
            continue
        if got is None:
            print(f"  {where}: no tclsh here; the signal wire is not checked")
    return problems
