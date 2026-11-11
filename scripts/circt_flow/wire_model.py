"""A pre-placement wire model for the flow's timing: the pack's declaration
and the OpenSTA Tcl that applies it.

Flow STA with ideal clocks and no parasitics times every net as its pin
loads alone. After placement and routing each net also carries a wire, and
on a deep-submicron node the wire capacitance is as large as the pin
capacitance, so a design the flow reports as just meeting its clock
can miss it after P&R. A wire model puts an expected wire on every net before there is a
placement, so the STA stage and the vt_swap stage time and size against
something close to what P&R will extract.

A pack declares the model in its `flow:` section (a property of the node
and the P&R recipe, fitted once from extracted parasitics):

    flow:
      wire_model:
        cap_per_fanout_ff: 0.3      # wire capacitance per load pin (fF), illustrative
        cap_base_ff: 0.2            # plus this per net (fF), illustrative
        res_per_fanout_ohm: 0       # optional: wire resistance per load pin
        res_base_ohm: 0             # optional: plus this per net (ohm)
        rc_corner: tt               # optional: the wire_rc corner it was
                                    # fitted at (scales it per corner)

A net with `n` load pins (instance inputs and output ports, counted over
the flattened netlist) gets

    C = cap_base_ff + cap_per_fanout_ff * n          (fF, on the net)
    R = res_base_ohm + res_per_fanout_ohm * n        (ohm, driver to each load)

Without a resistance the capacitance is set with `set_load` on the net.
With one, the Tcl writes a SPEF instead (one pi model per net: half of C at
the driver, R, half of C at a node that every load hangs off) and reads it;
it carries the capacitance too, since OpenSTA ignores a net's parasitics
once `set_load` has given it a capacitance. Nets with no load are left
alone.

The model is off unless the pack declares it. A flow config's
`wire_model:` overrides: `false` turns a pack's model off, `true` uses the
pack's, and a mapping replaces the named keys (so a flow config can also
declare a model on a pack that has none). The model times the STA stage and
the vt_swap stage. With `rc_corner:` and a pack `wire_rc:` (wire_rc.py)
each view scales the model to its corner's wire RC: C by the corner's
signal capacitance per micron over rc_corner's, R by the resistance. The
register-mapper's criticality run
(multibit_timing) and ABC's own delay model do not see it (ABC's target
calibration is `mapping_target.scale`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

KEYS = ("cap_per_fanout_ff", "cap_base_ff", "res_per_fanout_ohm",
        "res_base_ohm")
# The one non-numeric key: the pack wire_rc corner the numbers describe.
REF_KEY = "rc_corner"


class WireModelError(ValueError):
    pass


@dataclass(frozen=True)
class WireModel:
    cap_per_fanout_ff: float = 0.0
    cap_base_ff: float = 0.0
    res_per_fanout_ohm: float = 0.0
    res_base_ohm: float = 0.0
    rc_corner: Optional[str] = None

    @property
    def has_res(self) -> bool:
        return self.res_per_fanout_ohm > 0 or self.res_base_ohm > 0

    def cap_ff(self, fanout: int) -> float:
        return self.cap_base_ff + self.cap_per_fanout_ff * fanout

    def res_ohm(self, fanout: int) -> float:
        return self.res_base_ohm + self.res_per_fanout_ohm * fanout

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in KEYS}
        if self.rc_corner is not None:
            d[REF_KEY] = self.rc_corner
        return d


def _check(d, where: str) -> dict:
    if not isinstance(d, dict) or not d:
        raise WireModelError(f"{where}: wire_model must be a mapping of "
                             f"{', '.join(KEYS)}")
    bad = sorted(set(d) - set(KEYS) - {REF_KEY})
    if bad:
        raise WireModelError(f"{where}: wire_model.{bad[0]} is not a key; "
                             f"allowed: {', '.join(KEYS + (REF_KEY,))}")
    out = {}
    for k, v in d.items():
        if k == REF_KEY:
            if not isinstance(v, str) or not v:
                raise WireModelError(f"{where}: wire_model.{k} must be a "
                                     f"corner name, got {v!r}")
            out[k] = v
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise WireModelError(f"{where}: wire_model.{k} must be a number "
                                 f">= 0, got {v!r}")
        out[k] = float(v)
    return out


def parse_pack(d, where: str) -> dict:
    """A pack's `flow.wire_model` mapping, checked (returned as a dict)."""
    d = _check(d, where)
    if not (d.get("cap_per_fanout_ff") or d.get("cap_base_ff")):
        raise WireModelError(f"{where}: wire_model needs a capacitance "
                             "(cap_per_fanout_ff or cap_base_ff)")
    return d


def resolve(pack: Optional[dict], cfg, where: str) -> Optional[WireModel]:
    """The run's model from the pack's `flow.wire_model` (None if absent)
    and the flow config's `wire_model:` (absent, true, false or a
    mapping of keys that replace the pack's)."""
    if cfg is False:
        return None
    if cfg is True and pack is None:
        raise WireModelError(f"{where}: wire_model: true needs a pack with "
                             "flow.wire_model (or give the keys)")
    if cfg is not None and cfg is not True and not isinstance(cfg, dict):
        raise WireModelError(f"{where}: wire_model must be true, false or "
                             "a mapping")
    merged = dict(pack or {})
    if isinstance(cfg, dict):
        merged.update(_check(cfg, where) if cfg else {})
    return WireModel(**parse_pack(merged, where)) if merged else None


# The Tcl procedure. `circtflow_wire_model cb cf rb rf spef`: cap base and
# per load (fF), res base and per load (ohm), and the SPEF path used when a
# resistance is given. Nets are visited once each: with a hierarchical
# netlist, `get_nets -hierarchical` lists every alias of a net, and only the
# highest-connected one is taken.
PROC = r'''
proc circtflow_wire_model {cb cf rb rf spef {flags {}}} {
  set cs [expr {1e-15 / [sta::unit_scale capacitance]}]
  set nn 0
  set f ""
  if {$rb > 0 || $rf > 0} { set f [open $spef w] }
  set body ""
  foreach n [get_nets -hierarchical *] {
    set nm [get_full_name $n]
    if {[get_full_name [sta::Net_highest_connected_net $n]] ne $nm} continue
    set loads [sta::net_load_pins $n]
    set k [llength $loads]
    if {$k == 0} continue
    set c [expr {$cb + $cf * $k}]
    if {$f eq ""} { set_load {*}$flags [expr {$c * $cs}] $n }
    incr nn
    if {$f ne ""} {
      set drv [sta::net_driver_pins $n]
      if {[llength $drv] != 1} continue
      set r [expr {$rb + $rf * $k}]
      set d [circtflow_spef_pin [lindex $drv 0]]
      append body "*D_NET [circtflow_spef_name $nm] $c\n*CONN\n[circtflow_spef_conn [lindex $drv 0] I O]\n"
      foreach p $loads { append body "[circtflow_spef_conn $p O I]\n" }
      set x "[circtflow_spef_name $nm]:1"
      append body "*CAP\n1 $d [expr {$c / 2.0}]\n2 $x [expr {$c / 2.0}]\n*RES\n1 $d $x $r\n"
      set i 2
      foreach p $loads {
        append body "$i $x [circtflow_spef_pin $p] 0.001\n"
        incr i
      }
      append body "*END\n\n"
    }
  }
  if {$f ne ""} {
    puts $f "*SPEF \"IEEE 1481-1998\"\n*DESIGN \"wire_model\"\n*DATE \"none\"\n*VENDOR \"circt_flow\"\n*PROGRAM \"circt_flow\"\n*VERSION \"1\"\n*DESIGN_FLOW \"PIN_CAP NONE\"\n*DIVIDER /\n*DELIMITER :\n*BUS_DELIMITER \[ \]\n*T_UNIT 1 NS\n*C_UNIT 1 FF\n*R_UNIT 1 OHM\n*L_UNIT 1 HENRY\n"
    puts -nonewline $f $body
    close $f
    read_spef {*}$flags $spef
  }
  puts "circtflow wire model: $nn net(s) $flags"
}
proc circtflow_spef_name {s} {
  return [string map [list "\\" ""] $s]
}
proc circtflow_spef_conn {p port_dir pin_dir} {
  if {[sta::Pin_is_top_level_port $p]} { return "*P [circtflow_spef_pin $p] $port_dir" }
  return "*I [circtflow_spef_pin $p] $pin_dir"
}
proc circtflow_spef_pin {p} {
  set nm [get_full_name $p]
  if {[sta::Pin_is_top_level_port $p]} { return [circtflow_spef_name $nm] }
  set i [string last / $nm]
  return "[circtflow_spef_name [string range $nm 0 [expr {$i - 1}]]]:[circtflow_spef_name [string range $nm [expr {$i + 1}] end]]"
}
'''


def tcl(model: Optional[WireModel], spef_path: str = "wire_model.spef",
        scales=None) -> str:
    """The Tcl that applies `model` to the linked design ("" when None);
    put it after `link_design` and `read_sdc`. `scales` (wire_rc.Scale per
    min/max flag) scales it to the view's corner: {"": s} for the whole
    session, or {"-max": s_setup, "-min": s_hold} for a session that times
    setup and hold with different libraries."""
    if model is None:
        return ""
    if not scales or all(s is None for s in scales.values()):
        return (PROC + f"circtflow_wire_model {model.cap_base_ff:g} "
                f"{model.cap_per_fanout_ff:g} {model.res_base_ohm:g} "
                f"{model.res_per_fanout_ohm:g} {{{spef_path}}}\n")
    out = PROC
    for flag, s in scales.items():
        sp = spef_path if not flag else spef_path.replace(
            ".spef", f"{flag.replace('-', '.')}.spef")
        if s is None:
            out += (f"circtflow_wire_model {model.cap_base_ff:g} "
                    f"{model.cap_per_fanout_ff:g} {model.res_base_ohm:g} "
                    f"{model.res_per_fanout_ohm:g} {{{sp}}} {{{flag}}}\n")
            continue
        out += s.setup_tcl
        out += (f"set circtflow_sc [expr {{{s.c_expr}}}]\n"
                f"set circtflow_sr [expr {{{s.r_expr}}}]\n"
                f"puts \"circtflow wire rc: {s.corner} against {s.ref}: "
                f"C x $circtflow_sc R x $circtflow_sr {flag}\"\n"
                f"circtflow_wire_model "
                f"[expr {{{model.cap_base_ff:g} * $circtflow_sc}}] "
                f"[expr {{{model.cap_per_fanout_ff:g} * $circtflow_sc}}] "
                f"[expr {{{model.res_base_ohm:g} * $circtflow_sr}}] "
                f"[expr {{{model.res_per_fanout_ohm:g} * $circtflow_sr}}] "
                f"{{{sp}}} {{{flag}}}\n")
    return out
