#!/usr/bin/env bash
# emap_flow.sh — run the flow with Mockturtle's `emap` as the technology
# mapper instead of ABC's `&nf`, via the existing save-aig / use-mapped
# round trip.  No mapper rebuild, no changes to the default flow.
#
# WHY.  `&nf` stores cut functions in a 64-bit word, so `-K` is hard-capped
# at 6 and `Mio_CollectRootsNewDefault2(6, ...)` drops every library cell
# with more than 6 inputs.  On a library with 7- and 8-input AOI/OAI cells
# plain `&nf` emits none of them and splits that logic across several
# narrow cells instead, costing a cell and a net each time.  `emap` has no
# such limit (its cut size is a template parameter and NInputs is deduced
# from the tech_library).
#
# The default arguments came from a sweep on ibex, comparing the WRITTEN
# netlist (emap's compute_area() under-reports) without the buffers a later
# pass strips; `--area --mt-opt=3` used fewer logic cells and less area than
# `&nf`.  What did NOT help:
#   --cut     saturates at the widest cell's input count
#   --mt-opt  knee at 3; higher values change little
#   --refactor  a few cells for a large runtime; nothing on top of --mt-opt
#   --ela / --relax  inert (--relax needs a delay target area mode ignores)
#   --eswp    fewer cells but more area -- packs into much larger cells
#
# Usage: emap_flow.sh <out_dir> [emap args...]
set -euo pipefail

OUT=$(readlink -f -m "$1"); shift
EMAP_ARGS=("${@:---area --mt-opt=3}")
[ $# -eq 0 ] && EMAP_ARGS=(--area --mt-opt=3)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ABC="$ROOT/third_party/yosys/build/yosys-abc"
EMAP="$ROOT/mapper/build/bin/emap-bridge"
LIB="${EMAP_LIB:?set EMAP_LIB to the liberty}"
GENLIB="${EMAP_GENLIB:?set EMAP_GENLIB to its genlib}"
CFG="${FLOW_CONFIG:-$ROOT/examples/ibex/ibex_flow.yaml}"
PERIOD="${CLOCK_PS:-2500}"

for f in "$EMAP" "$GENLIB"; do
    [ -e "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
mkdir -p "$OUT"/{aig,mapped}
# Cell names from the genlib, and the subset that are plain buffers, for the
# per-target counts below.
awk '/^GATE/ {print $2}' "$GENLIB" | sort -u > "$OUT/cells.txt"
awk '/^GATE/ && $4 ~ /^[A-Za-z_]\w*=[A-Za-z_]\w*;$/ && $4 !~ /=CONST[01];$/ \
     {print $2}' "$GENLIB" | sort -u > "$OUT/buffers.txt"

echo "== 1/3 flow, saving the pre-mapping AIG"
( cd "$ROOT/scripts" && python3 -m circt_flow "$CFG" "$OUT/pass1" \
    --abc-save-aig-dir "$OUT/aig" ) > "$OUT/pass1.log" 2>&1

# ABC's optimisation body, verbatim from ABCBridge's built-in recipe minus
# the two `&nf` mapping steps -- emap replaces those.
BODY="strash; scorr; dc2; &get -n; &deepsyn -J 100 -I 4 -S 200 -T 10; &put"
BODY="$BODY; strash; &get -n; &dch -f; &if -C 32 -K 6; &put"
BODY="$BODY; strash; &get -n; &resub -N 2; &put; strash; &get -n"

echo "== 2/3 optimise + emap  (args: ${EMAP_ARGS[*]})"
for aig in "$OUT"/aig/*.aig; do
    t=$(basename "$aig" .aig)
    "$ABC" -q "read_lib $LIB; read_aiger $aig; $BODY; &w $OUT/aig/${t}_opt.aig" \
        >> "$OUT/abc.log" 2>&1
    "$EMAP" "$OUT/aig/${t}_opt.aig" "$GENLIB" "$OUT/mapped/${t}_raw.v" \
        "${EMAP_ARGS[@]}" >> "$OUT/emap.log" 2>&1
    # emap names every module `top` and writes bit-indexed identifiers bare;
    # the flow's re-import needs the target's own name and ABC-style escaped
    # identifiers. Textual only -- the mapping is untouched.
    python3 "$ROOT/scripts/circt_flow/emap_fixup.py" \
        "$OUT/mapped/${t}_raw.v" "$t" "$OUT/mapped/${t}.v" >> "$OUT/emap.log" 2>&1
    rm -f "$OUT/mapped/${t}_raw.v" 
    tot=$(awk '{print $1}' "$OUT/mapped/${t}.v" | grep -cxFf "$OUT/cells.txt" || true)
    buf=$(awk '{print $1}' "$OUT/mapped/${t}.v" | grep -cxFf "$OUT/buffers.txt" || true)
    echo "   $t: $tot cells ($((tot-buf)) logic)"
done

echo "== 3/3 flow, re-importing the emap netlist"
( cd "$ROOT/scripts" && python3 -m circt_flow "$CFG" "$OUT/final" \
    --abc-use-mapped-dir "$OUT/mapped" ) > "$OUT/final.log" 2>&1

echo "== netlist: $OUT/final/1_synth/ibex_top_openroad.v"
