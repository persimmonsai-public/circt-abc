#!/usr/bin/env bash
# Negative control for miter.sh. A miter that cannot fail proves nothing.
#
# Usage:  miter_control.sh <design.v> [mode] [depth] [outdir]
#   mode = output  (default) invert one bit of a top-level output
#          sram             invert an SRAM model's read data
#
# Run this against the SAME netlist on both sides, with the bug injected on
# the gate side only. The control MUST report `model found: FAIL!`. If it
# reports SUCCESS, miter.sh's verdict on your real comparison is worthless.
#
# ---------------------------------------------------------------------------
# CHOOSING THE BUG IS THE HARD PART, and getting it wrong costs a full run.
#
# `sram` mode is kept deliberately, and it DOES NOT WORK as a control -- it
# returns SUCCESS. That is not vacuity: within the 4-cycle bound from reset
# nothing ever reads a cache line back to an output, so the cache arrays are
# optimised out of the miter entirely and the injected bug is unobservable
# *by construction*. Measured 2026-09-17; the miter's own flop count is the
# evidence (5,130, nowhere near the ~56k a mapped pair of SRAM arrays needs).
#
# It is kept because it is the clearest statement of what the bound does not
# cover: this is a BOUNDED proof of equivalence for N cycles out of reset,
# not unbounded equivalence, and anything behind a cache fill or multi-cycle
# handshake lives outside it.
#
# `output` mode is the control that discriminates: gold and gate must differ
# on a top-level output bit even if the signal is constant, so it is
# observable at depth 1. Use it.
# ---------------------------------------------------------------------------
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
Y="$R/third_party/yosys/build/yosys"
SRC=${1:?usage: miter_control.sh <design.v> [output|sram] [depth] [outdir]}
MODE=${2:-output}
D=${3:-1}
OUT=${4:-.}
SRAM="$R/scripts/verify/sram_models.sv"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
GATE_SRAM="$SRAM"

case "$MODE" in
  output)
    # Invert the first field of cvxif_req_o. Observable by construction.
    sed 's|/\*cast(bit)\*/{_id_stage_i_compressed_valid_o,|/*cast(bit)*/{~_id_stage_i_compressed_valid_o,|' \
      "$SRC" > "$W/bad.v"
    if cmp -s "$SRC" "$W/bad.v"; then
      echo "control: could not inject the output bug -- the anchor did not match." >&2
      echo "Pick another top-level output assignment in $SRC and update this sed." >&2
      exit 2
    fi
    GATE_SRC="$W/bad.v" ;;
  sram)
    sed 's|else rdata_o <= mem\[addr_i\];|else rdata_o <= ~mem[addr_i];|' "$SRAM" > "$W/sram_bad.sv"
    GATE_SRC="$SRC"; GATE_SRAM="$W/sram_bad.sv" ;;
  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

timeout "${MITER_TIMEOUT:-21600}" "$Y" -p "
read_slang --keep-hierarchy $SRAM $SRC --top cva6
prep -flatten -top cva6
design -stash gold
read_slang --keep-hierarchy $GATE_SRAM $GATE_SRC --top cva6
prep -flatten -top cva6
design -stash gate
design -copy-from gold -as gold cva6
design -copy-from gate -as gate cva6
miter -equiv -flatten -make_assert gold gate miter
hierarchy -top miter
memory_map; async2sync; opt -full
sat -seq $D -prove-asserts -show-ports -set-init-zero -set-at 1 in_rst_ni 0 -verify miter
" > "$OUT/miter_control_${MODE}_d$D.log" 2>&1

verdict=$(grep -oE 'SAT proof finished - [a-z ]*: [A-Z!]*' "$OUT/miter_control_${MODE}_d$D.log" | tail -1)
echo "control[$MODE] depth $D :: ${verdict:-NO VERDICT}"
case "$MODE:$verdict" in
  output:*FAIL*) echo "  PASS -- the instrument discriminates."; exit 0 ;;
  output:*)      echo "  BROKEN -- the control did not fail, so miter.sh proves nothing." >&2; exit 1 ;;
  sram:*)        echo "  (expected SUCCESS; see this file's header for why it is not a control)"; exit 0 ;;
esac
