#!/usr/bin/env bash
# The asap7-mvt example: ibex_top on ASAP7's three threshold flavours.
# Four arms through the flow, in parallel, each with the flow's STA stage (tt, ss, ff;
# setup at tt, hold at ff):
#
#   rvt    RVT only, load-aware timing-driven mapping (techpack_rvt.yaml)
#   lvt    LVT only (techpack_lvt.yaml)
#   slvt   SLVT only (techpack_slvt.yaml)
#   mvt    the leakage-priced multi-Vt library, then the vt_swap stage
#          (techpack.yaml)
#
# then a per-flavour report of each netlist (cells, area, leakage at tt and
# at ss, the leakage corner) and a check: the mvt arm must meet setup at tt
# and hold at ff, and leak less at ss than every single-flavour arm that
# meets setup.
#
#   examples/asap7-mvt/run.sh <out_dir> [-W <leak weight>] [--only <arm>]
#
# <out_dir> may not be inside this repository. -W is the leakage price of
# the multi-Vt library (lib/make_libs.py; um^2 per pW). The libraries come
# from the ORFS image's asap7 platform (in place under $ORFS_HOME, else
# with docker); OpenSTA runs in place with $OPENSTA, else in the ORFS image.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
[ $# -ge 1 ] || { sed -n 2,22p "$0"; exit 2; }
out=$(realpath -m "$1"); shift
W=0.00001; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    -W) W=$2; shift 2 ;;
    --only) ONLY=$2; shift 2 ;;
    *) echo "run.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$out/" in "$root"/*) echo "run.sh: $out is inside $root" >&2; exit 2 ;; esac
mkdir -p "$out"

# 1. Libraries: per flavour and corner, then the priced multi-Vt one
#    (rebuilt when -W changes; its W is in its header comment).
python3 -I "$here/lib/make_libs.py" "$here/lib" -W "$W"

cd "$root/scripts"
for p in techpack techpack_rvt techpack_lvt techpack_slvt; do
  python3 -m circt_flow.techpack check "$here/$p.yaml" >/dev/null \
    || { python3 -m circt_flow.techpack check "$here/$p.yaml"; exit 1; }
done

# 2. The arms, in parallel (each its own flow run; the libraries are
#    built), then their summaries in order.
summary="$out/summary.txt"; : > "$summary"
fre='_ASAP7_75t_(R|L|SL)$'
arm() {  # arm <name>: the flow, then the per-flavour report at tt and ss
  local a=$1 cfg="$here/designs/ibex/flow_$1.yaml" run="$out/$1"
  local extra=(); [ "$a" = mvt ] && extra=(--vt-swap)
  python3 -m circt_flow "$cfg" "$run" --sta ${extra[@]+"${extra[@]}"} \
    > "$run.log" 2>&1 || { echo "FAILED" > "$run.sum"; return 1; }
  { grep -h "^\[vt_swap\] mode" "$run.log" | sed 's# -> /.*##' || true
    grep -h "^\[sta\]   \|^\[sta\] hold" "$run.log" || true
  } | sed "s/^/$a /" > "$run.sum"
  for c in tt ss; do
    local libs=(); for v in rvt lvt slvt; do libs+=(--lib "$here/lib/asap7_${v}_$c.lib"); done
    python3 -I "$root/examples/mvt-mini/vt_report.py" \
      "$run/1_synth/ibex_top_openroad.v" ibex_top "${libs[@]}" \
      --flavour-re "$fre" --label "ibex.$a@$c" \
      --json "$run.$c.vt.json" >> "$run.sum"
  done
}
arms=(rvt lvt slvt mvt); [ -z "$ONLY" ] || arms=("$ONLY")
pids=()
for a in "${arms[@]}"; do
  echo "[asap7-mvt] ibex $a -> $out/$a"
  arm "$a" & pids+=($!)
done
fail=0
for i in "${!arms[@]}"; do
  wait "${pids[$i]}" || { fail=1; a=${arms[$i]}
    echo "[asap7-mvt] $a FAILED, see $out/$a.log" >&2; tail -20 "$out/$a.log" >&2; }
done
[ "$fail" = 0 ] || exit 1
for a in "${arms[@]}"; do tee -a "$summary" < "$out/$a.sum"; done

# 3. The check (all four arms).
[ -n "$ONLY" ] || python3 -I - "$out" <<'PY' | tee -a "$summary"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
def sta(arm):
    return json.loads((out / arm / "sta" / "summary.json").read_text())
def wns(arm, corner):
    return min(r["wns_ns"] for r in sta(arm)["domains"] if r["corner"] == corner)
def leak(arm, c="ss"):
    return json.loads((out / f"{arm}.{c}.vt.json").read_text())["leakage"]
bad = []
for arm in ("rvt", "lvt", "slvt", "mvt"):
    h = sta(arm)["hold"]
    print(f"[asap7-mvt] {arm:4}: setup WNS at tt {wns(arm, 'tt'):+.4f} ns, "
          f"hold at {h['corner']} {h['wns_ns']:+.4f} ns, "
          f"leakage at ss {leak(arm) / 1e6:.3f} uW, at tt "
          f"{leak(arm, 'tt') / 1e6:.3f} uW")
if wns("mvt", "tt") < 0:
    bad.append("mvt misses setup at tt")
if sta("mvt")["hold"]["wns_ns"] < 0:
    bad.append("mvt misses hold")
for arm in ("rvt", "lvt", "slvt"):
    if wns(arm, "tt") >= 0 and leak(arm) <= leak("mvt"):
        bad.append(f"{arm} meets setup with no more leakage than mvt")
print("[asap7-mvt] check: " + ("; ".join(bad) if bad else "OK"))
sys.exit(1 if bad else 0)
PY
echo "[asap7-mvt] summary -> $summary"
