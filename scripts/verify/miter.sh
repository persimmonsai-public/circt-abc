#!/usr/bin/env bash
# Top-level bounded sequential miter for DESIGN-LEVEL passes.
#
# Some passes here are sound only across the whole design, not per module --
# `specialize-constant-module-inputs` changes what a module computes for
# inputs it is never given, and `narrow-dead-output-bits` zeroes bits no
# caller reads. A per-module miter on either is invalid by construction, so
# they are gated here instead.
#
# Usage:  miter.sh <gold.v> <gate.v> [depth] [outdir]
#
# Both inputs must be PRE-REGISTER-MAPPER Verilog, i.e. exported before
# `--register-mapper` turns flops into cell instances. Gating on a mapped
# netlist invites the failure in the `empty-cell-stubs-break-miters` note:
# undriven flop outputs compare equal on both sides and the proof collapses
# into a vacuous SUCCESS. Produce the inputs with:
#
#   circt-abc-opt <passes...> design_deagg.mlir -o gate.mlir
#   circt-opt gate.mlir --lower-seq-to-sv --export-verilog -o /dev/null > gate.v
#
# ALWAYS pair this with scripts/verify/miter_control.sh. A miter that cannot
# fail proves nothing, and this project has already had one print SUCCESS
# while observing nothing.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
Y="$R/third_party/yosys/build/yosys"
GOLD=${1:?usage: miter.sh <gold.v> <gate.v> [depth] [outdir]}
GATE=${2:?usage: miter.sh <gold.v> <gate.v> [depth] [outdir]}
D=${3:-4}
OUT=${4:-.}
SRAM="$R/scripts/verify/sram_models.sv"

timeout "${MITER_TIMEOUT:-21600}" "$Y" -p "
read_slang --keep-hierarchy $SRAM $GOLD --top cva6
prep -flatten -top cva6
design -stash gold
read_slang --keep-hierarchy $SRAM $GATE --top cva6
prep -flatten -top cva6
design -stash gate
design -copy-from gold -as gold cva6
design -copy-from gate -as gate cva6
miter -equiv -flatten -make_assert gold gate miter
hierarchy -top miter
memory_map; async2sync; opt -full
stat
sat -seq $D -prove-asserts -show-ports -set-init-zero -set-at 1 in_rst_ni 0 -verify miter
" > "$OUT/miter_d$D.log" 2>&1
rc=$?

verdict=$(grep -oE 'SAT proof finished - [a-z ]*: [A-Z!]*' "$OUT/miter_d$D.log" | tail -1)
size=$(grep -oE 'Solving problem with [0-9]+ variables' "$OUT/miter_d$D.log" | tail -1)
echo "depth $D rc=$rc :: ${verdict:-NO VERDICT} :: ${size:-no SAT problem reported}"

# A SUCCESS on a collapsed design is the failure mode to watch for. The real
# cv32a60x gate ran at ~9.0M variables / 24.4M clauses / 33,242 cells; a
# verdict arriving with orders of magnitude fewer means the design optimised
# away and the result is meaningless.
[ -z "$verdict" ] && { echo "  NO VERDICT is a FAIL, not a pass" >&2; exit 1; }
exit $rc
