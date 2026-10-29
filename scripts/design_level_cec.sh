#!/usr/bin/env bash
# Design-level equivalence for transforms that are only valid IN CONTEXT.
#
# `aig_stitch_residual.py` rewrites the residual AIG using the parent's own
# wiring, so the stitched AIG is deliberately NOT equivalent to the original as
# a standalone circuit -- `&cec` on the two AIGs is expected to fail and proves
# nothing.  What must hold is that the FINISHED, FLATTENED netlists are
# equivalent.  This lowers both to gate-level AIGs and compares those.
#
# Flops are cut and paired BY NAME, never by position: ABC's `cec` matches
# inputs and outputs by name, and `cec_latch_names.py` names every latch after
# its liberty instance (`cecq_<inst>.IQ`) and exposes its clock pin as an
# output (`cecc_<inst>.IQ`) so clock-gate enables are compared too.  The
# positional `&cec` this replaced reported NOT EQUIVALENT on two equivalent
# ibex netlists once bit-level clock gating reordered 22 flops.
#
# Usage: design_level_cec.sh <gold_netlist.v> <test_netlist.v>   (TOP=ibex_top)
#   CEC_LIBERTY      space-separated liberties the netlists instantiate
#                    (required)
#   CEC_CELL_MODELS  optional Verilog models of the cells a liberty `function`
#                    cannot express (integrated clock gates), read after the
#                    liberties
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
Y="$R/third_party/yosys/build/yosys"
ABC="$R/third_party/yosys/build/yosys-abc"
TOP="${TOP:-ibex_top}"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
LIBS=${CEC_LIBERTY:?set CEC_LIBERTY to the liberties the netlists use}
MODELS=${CEC_CELL_MODELS:+read_verilog -sv $CEC_CELL_MODELS}
READ_LIBS=""
for l in $LIBS; do READ_LIBS+="read_liberty -ignore_miss_func $l; "; done
lower() {   # <netlist.v> <out.aig>
  local b="${2%.aig}"
  "$Y" -p "
    $READ_LIBS
    $MODELS
    read_slang --top $TOP $1
    flatten; opt_clean; async2sync; simplemap
    dfflegalize -cell \$_DFF_P_ 0; simplemap; opt_clean
    write_rtlil $b.il; write_json $b.json" > "$b.log" 2>&1 || return 1
  python3 "$R/scripts/cec_latch_names.py" "$b.json" "$TOP" "$b.names.ys" || return 1
  "$Y" -p "
    read_rtlil $b.il
    script $b.names.ys
    rename -hide w:* i:* %d o:* %d w:cecq_* %d
    aigmap; opt_clean; write_aiger -symbols $2" >> "$b.log" 2>&1
}
echo "== lowering gold"; lower "$1" "$W/gold.aig" || true
echo "== lowering test"; lower "$2" "$W/test.aig" || true
for f in gold test; do
  [ -s "$W/$f.aig" ] || { echo "FATAL: $f did not lower"; tail -5 "$W/$f.log"; exit 1; }
done
for f in gold test; do
  "$ABC" -c "read_aiger $W/$f.aig; print_stats" 2>&1 | grep -oE "i/o =.*lat = *[0-9]+" | sed "s/^/  $f: /"
done
echo "== cec by name (combinational, after cutting flops)"
# `comb` is an OLD-space command: it needs `read_aiger`, not `&read`.
# `write_aiger -s` keeps the names `cec` pairs on.
for f in gold test; do
  "$ABC" -c "read_aiger $W/$f.aig; comb; strash; write_aiger -s $W/${f}_c.aig" >/dev/null 2>&1
  [ -s "$W/${f}_c.aig" ] || { echo "FATAL: comb-cut of $f failed"; exit 1; }
done
V=$("$ABC" -c "cec $W/gold_c.aig $W/test_c.aig" 2>&1 | tail -4)
echo "$V"
echo "$V" | grep -qiE "^Networks are equivalent" && echo "VERDICT: EQUIVALENT" || echo "VERDICT: **NOT PROVEN** -- do not ship"
