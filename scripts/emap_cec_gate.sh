#!/usr/bin/env bash
# emap_cec_gate.sh -- correctness gate for an externally-mapped netlist.
#
# The mapper is outside the flow, so the only thing that proves the netlist
# is the pre-mapping AIG it came from.  Checks, in order:
#   0. port-ORDER match between the AIG and the re-read netlist.  NOT optional:
#      `&cec` matches by position, so any identifier-rewrite bug (emap writes
#      `module top` with bare bit-indexed names; emap_fixup.py rewrites them)
#      would silently permute ports and pass.
#   1. cec  (FRAIG+SAT, by-name)                    -- CAP seconds
#   2. &cec (SAT sweeping, by-order) if 1 is mute   -- CAP seconds
# A NO VERDICT is a FAIL, not a pass.
#
# usage: emap_cec_gate.sh <frozen.aig> <mapped.v> <liberty>
set -u
AIG=$1; MAPPED=$2
LIB=${3:?usage: emap_cec_gate.sh <frozen.aig> <mapped.v> <liberty>}
A="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/third_party/yosys/build/yosys-abc"
W=$(mktemp -d); CAP=${CAP:-900}
trap 'rm -rf "$W"' EXIT

$A -q "read_aiger $AIG; print_io" > "$W/io_aig.txt" 2>&1
$A -q "read_lib $LIB; read_verilog -m $MAPPED; strash; print_io; &get -n; &w $W/mapped.aig" \
   > "$W/io_map.txt" 2>&1
if [ ! -s "$W/mapped.aig" ]; then
  echo "FAIL: could not read $MAPPED into ABC"; sed -n '1,15p' "$W/io_map.txt"; exit 1
fi

# ABC prefixes its own banner/warning lines to stdout; keep only the port
# listing itself, in order, so the check compares ports and not chatter.
norm() { sed -n '/^Primary input/,$p' "$1" | grep -oE '[A-Za-z_\\][^ ]*' | tr -d ' '; }
if diff <(norm "$W/io_aig.txt") <(norm "$W/io_map.txt") > "$W/io.diff" 2>&1; then
  echo "  io_order_match: OK"
else
  echo "  io_order_match: MISMATCH  (first lines)"; head -12 "$W/io.diff"
fi
printf "  AIG    : %s\n  mapped : %s\n" \
  "$($A -q "read_aiger $AIG; print_stats" 2>&1 | tail -1)" \
  "$($A -q "read_aiger $W/mapped.aig; print_stats" 2>&1 | tail -1)"

out=$(timeout "$CAP" $A -q "cec $AIG $W/mapped.aig" 2>&1 | tail -3)
echo "  cec: $out"
case "$out" in
  *"are equivalent"*) echo "  VERDICT: EQUIVALENT"; exit 0 ;;
  *"are NOT equivalent"*|*"NOT EQUIVALENT"*) echo "  VERDICT: NOT EQUIVALENT"; exit 1 ;;
esac
out2=$(timeout "$CAP" $A -q "&r $W/mapped.aig; &cec -v $AIG" 2>&1 | tail -3)
echo "  &cec: $out2"
case "$out2" in
  *"are equivalent"*|*"Networks are equivalent"*) echo "  VERDICT: EQUIVALENT"; exit 0 ;;
  *"NOT equivalent"*) echo "  VERDICT: NOT EQUIVALENT"; exit 1 ;;
esac
echo "  VERDICT: NO VERDICT -- treat as FAIL"; exit 2
