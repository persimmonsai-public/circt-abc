#!/usr/bin/env bash
# The shift-select example: align_unit (a 16-bit word shifted left by a 4-bit
# amount into a 32-bit result by a `case`, registered at both ends) on ASAP7
# RVT, with the map-shift-select pass off and on.
#
#   base    flow_base.yaml    the flow as it is
#   shift   flow_shift.yaml   shift_select: three-term AND-OR cells, a
#                             mixed-radix recipe
#
# Each arm runs the flow with its STA stage (tt, ss, ff; setup at tt). Then:
# cells, area and logic depth of each netlist (report.py), and a Yosys
# equivalence proof of the two netlists (a miter of the whole design, the
# flip-flops started at zero, 4 clock cycles: the design has two register
# stages, so this covers every input sequence that reaches an output), with a
# negative control (the proof must fail on a netlist with one cell changed).
#
#   examples/shift-select/run.sh <out_dir> [--determinism]
#
# --determinism runs the shift arm a second time and compares the netlists
# (byte for byte, the location comments of export-verilog aside, which name the
# output directory). <out_dir> may not be inside this repository. The libraries
# come from the ORFS image's asap7 platform (lib/make_libs.py of asap7-mvt);
# OpenSTA runs in place with $OPENSTA, else in the ORFS image; Yosys is
# $YOSYS, else `yosys`.
#
# Clock gating is switched off (CIRCT_FLOW_NO_CLOCK_GATING=1): a gated clock
# is outside what a Yosys miter proves, and the example is about the select.
# The outline-after-flatten stage order (CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1)
# leaves one flat module, which the fanout stage and OpenSTA need for a design
# whose top has no sub-modules.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
[ $# -ge 1 ] || { sed -n 2,24p "$0"; exit 2; }
out=$(realpath -m "$1"); shift
DET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --determinism) DET=1; shift ;;
    *) echo "run.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$out/" in "$root"/*) echo "run.sh: $out is inside $root" >&2; exit 2 ;; esac
mkdir -p "$out"
YOSYS=${YOSYS:-yosys}

python3 -I "$root/examples/asap7-mvt/lib/make_libs.py" "$here/lib" >/dev/null
cd "$root/scripts"
python3 -m circt_flow.techpack check "$here/techpack.yaml" >/dev/null \
  || { python3 -m circt_flow.techpack check "$here/techpack.yaml"; exit 1; }
export CIRCT_FLOW_NO_CLOCK_GATING=1 CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1

flow() {  # flow <arm> <out dir>
  python3 -m circt_flow "$here/designs/align_unit/flow_$1.yaml" "$2" --sta \
    > "$2.log" 2>&1 || { echo "[shift-select] $1 FAILED, see $2.log" >&2; tail -20 "$2.log" >&2; return 1; }
}
pids=()
for a in base shift; do echo "[shift-select] $a -> $out/$a"; flow "$a" "$out/$a" & pids+=($!); done
[ "$DET" = 0 ] || { flow shift "$out/shift2" & pids+=($!); }
for p in "${pids[@]}"; do wait "$p"; done

summary="$out/summary.txt"; : > "$summary"
lib="$here/lib/asap7_rvt_tt.lib"
for a in base shift; do
  python3 -I "$here/report.py" "$out/$a/1_synth/align_unit.v" --lib "$lib" \
    --label "$a" --json "$out/$a.json" | tee -a "$summary"
  python3 -I - "$out/$a" "$a" <<'PY' | tee -a "$summary"
import json, sys
s = json.loads(open(sys.argv[1] + "/sta/summary.json").read())
w = min(r["wns_ns"] for r in s["domains"] if r["corner"] == "tt")
print(f"{sys.argv[2]}: setup WNS at tt {w:+.4f} ns (400 ps clock, before P&R)")
PY
done
grep -h "map-shift-select\] [0-9]" "$out/shift.log" | tee -a "$summary"

# Equivalence of the two netlists, and the negative control.
prove() {  # prove <netlist a> <netlist b>: exit 0 when equal
  sed 's/^module align_unit\b/module align_unit_new/' "$2" > "$out/new.v"
  "$YOSYS" -q -p "read_liberty -ignore_miss_func $lib; read_verilog $1 $out/new.v; proc; \
    miter -equiv -flatten -make_assert align_unit align_unit_new miter; async2sync; \
    sat -verify -prove-asserts -seq 4 -set-init-zero miter" > "$out/prove.log" 2>&1
}
status=0
if prove "$out/base/1_synth/align_unit.v" "$out/shift/1_synth/align_unit.v"; then
  echo "equivalence: base == shift (miter, 4 cycles from zero state): OK" | tee -a "$summary"
else
  echo "equivalence: base != shift: FAILED, see $out/prove.log" | tee -a "$summary"; status=1
fi
# Negative control: the first NOR2 of the shift netlist replaced by a NAND2
# (same pins) must be caught.
python3 -I - "$out/shift/1_synth/align_unit.v" "$out/broken.v" <<'PY'
import re, sys
t = open(sys.argv[1]).read()
n = re.sub(r"\bNOR2xp33_ASAP7_75t_R\b", "NAND2xp33_ASAP7_75t_R", t, count=1)
assert n != t, "no cell to change"
open(sys.argv[2], "w").write(n)
PY
if prove "$out/base/1_synth/align_unit.v" "$out/broken.v"; then
  echo "negative control: the proof did NOT catch a changed cell: FAILED" | tee -a "$summary"; status=1
else
  echo "negative control: a changed cell is caught: OK" | tee -a "$summary"
fi
if [ "$DET" = 1 ]; then
  # (the location comments of export-verilog name the output directory; they are not part of the netlist)
  if cmp -s <(sed 's#[[:space:]]*//.*##' "$out/shift/1_synth/align_unit.v") \
            <(sed 's#[[:space:]]*//.*##' "$out/shift2/1_synth/align_unit.v"); then
    echo "determinism: two runs of the shift arm give byte-identical netlists (comments aside): OK" | tee -a "$summary"
  else
    echo "determinism: the two shift netlists differ: FAILED" | tee -a "$summary"; status=1
  fi
fi
echo "[shift-select] summary -> $summary"
exit $status
