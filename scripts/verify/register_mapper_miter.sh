#!/usr/bin/env bash
# Bounded sequential miter for register-mapper alone: the design with its
# registers as `seq.firreg` (gold) against the same design after
# --register-mapper (gate), whose flops are library cell instances given
# their liberty function by Yosys `read_liberty`.
#
# Usage: register_mapper_miter.sh <core_deagg.mlir> <liberty> <top> <reset_n>
#            [depth] [outdir] [-- <cell models.v>...]
#
#   <liberty>   the liberty register-mapper maps to (and Yosys reads)
#   <reset_n>   the active-low reset input, asserted at step 1
#   models.v    Verilog models read after the liberty, overriding it: one
#               for each cell the liberty's `function` cannot express (an
#               ICG, as a plain pass-through: it is the same on both sides),
#               and for any cell whose Yosys model is wrong. Yosys 0.67's
#               model of a flop with both clear and preset (ASAP7's
#               DFFASRHQNx1) fires the preset whenever clear is inactive, so
#               it holds no value; 0.68's is correct. asap7_miter_models.v
#               keeps a hand model from the liberty `ff` group either way.
#
# Clock gating is not run, so gold and gate differ only in register-mapper.
# A SUCCESS means nothing unless a broken gate fails: run it once with a
# deliberately broken gate (e.g. every tied async pin flipped to its active
# level in gate.v) and expect FAIL.
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IN=${1:?usage: register_mapper_miter.sh <core_deagg.mlir> <liberty> <top> <reset_n> [depth] [outdir] [-- models.v...]}
LIB=${2:?liberty}; TOP=${3:?top}; RST=${4:?reset_n}
shift 4
D=4; OUT=.
[[ $# -gt 0 && $1 != -- ]] && { D=$1; shift; }
[[ $# -gt 0 && $1 != -- ]] && { OUT=$1; shift; }
[[ $# -gt 0 && $1 == -- ]] && shift
MODELS=()
for m in "$@"; do MODELS+=("$(realpath "$m")"); done
OPT=${CIRCT_ABC_OPT:-$R/mapper/build/bin/circt-abc-opt}
CIRCT_OPT=${CIRCT_OPT:-$R/third_party/circt/build/bin/circt-opt}
Y=${YOSYS:-$R/third_party/yosys/build/yosys}
mkdir -p "$OUT"

P=(--lower-firmem --narrow-array-registers --lower-aggregate-registers
   --register-legalize --hw-aggregate-to-comb --canonicalize)
"$OPT" "${P[@]}" "$IN" -o "$OUT/gold.mlir" 2>"$OUT/gold.err"
"$OPT" "${P[@]}" "--register-mapper=liberty=$LIB" --canonicalize "$IN" \
    -o "$OUT/gate.mlir" 2>"$OUT/gate.err"
grep '^\[register-mapper\]' "$OUT/gate.err" || true
for a in gold gate; do
  "$CIRCT_OPT" "$OUT/$a.mlir" --lower-seq-to-sv --export-verilog \
      -o /dev/null > "$OUT/$a.v"
done

read_models=""
for m in "${MODELS[@]}"; do read_models+="read_verilog -overwrite $m
"; done
cat > "$OUT/miter.ys" <<EOF
${read_models//-overwrite /}
read_slang --keep-hierarchy $OUT/gold.v --top $TOP
prep -flatten -top $TOP
design -stash gold
read_liberty -ignore_miss_func $LIB
$read_models
read_slang --keep-hierarchy $OUT/gate.v --top $TOP
prep -flatten -top $TOP
design -stash gate
design -copy-from gold -as gold $TOP
design -copy-from gate -as gate $TOP
miter -equiv -flatten -make_assert gold gate miter
hierarchy -top miter
memory_map; async2sync; opt -full
stat
sat -seq $D -prove-asserts -set-init-zero -set-at 1 in_$RST 0 -verify miter
EOF
"$Y" -s "$OUT/miter.ys" > "$OUT/miter_d$D.log" 2>&1 || true
verdict=$(grep -oE 'SAT proof finished - [a-z ]*: [A-Z!]*' "$OUT/miter_d$D.log" | tail -1)
size=$(grep -oE 'Solving problem with [0-9]+ variables' "$OUT/miter_d$D.log" | tail -1)
echo "depth $D :: ${verdict:-NO VERDICT} :: ${size:-no SAT problem reported}"
[[ $verdict == *SUCCESS* ]]
