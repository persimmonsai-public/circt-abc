#!/usr/bin/env bash
# The mvt-mini matrix: each power domain's core through the flow on its
# supply's libraries three ways -- RVT with the stock ABC script, RVT
# load-aware and timing-driven (the control), and the merged leakage-priced
# multi-Vt liberty -- then mvt_top with both cores as power domains on their
# own rails (techpack_rails.yaml, tt corner), then mvt_soc -- a clock per
# domain, two modes (func, turbo) and three corners (techpack_corners.yaml:
# mapped at ss timing with ff leakage prices, each domain at its own
# calibrated target) -- and a per-flavour report of each netlist (for
# mvt_top and mvt_soc, of each domain against its own rail's real
# libraries; for mvt_soc, per domain x mode x corner).
#
#   examples/mvt-mini/run.sh <out_dir> [-W <leak weight>] [--sta] [--vt-swap] [--only <core_hv|core_lv|mvt_top|mvt_soc|mvt_top_buf>]
#
# <out_dir> may not be inside this repository (the flow allows it for this
# public pack, but results do not belong in the tree). -W is
# scripts/mvt/make_abc_lib.py's leakage price (um^2 per nW; default 0.002).
# --sta adds OpenSTA's worst slack at each flow config's clock period (ORFS
# docker image); for mvt_soc, at each mode's period of the domain's clock;
# for mvt_top, mvt_top_ls and mvt_soc it also runs the flow's own STA stage
# (circt_flow --sta: per domain x corner x mode, and the whole chip). Tool paths are the flow's own defaults or CIRCT_ABC_OPT,
# CIRCT_OPT, ABC_BIN; SV2V names a local sv2v instead of the docker image.
# --vt-swap runs circt_flow's vt_swap stage (OpenSTA flavour swap after
# synthesis) on the flow_mvt arm and the power runs.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
[ $# -ge 1 ] || { sed -n 2,20p "$0"; exit 2; }
out=$(realpath -m "$1"); shift
W=0.002; STA=""; ONLY=""; VTSWAP=""
while [ $# -gt 0 ]; do
  case "$1" in
    -W) W=$2; shift 2 ;;
    --sta) STA=--sta; shift ;;
    --vt-swap) VTSWAP=--vt-swap; shift ;;
    --only) ONLY=$2; shift 2 ;;
    *) echo "run.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$out/" in "$root"/*) echo "run.sh: $out is inside $root" >&2; exit 2 ;; esac
mkdir -p "$out"

# 1. Libraries: the per-flavour ones (mini_<flavour>_<supply>_<corner>.lib),
#    then merged priced libraries per supply (their areas are
#    S*(area + W*leakage): prices, not areas): mini_mvt_<supply>_tt_abc.lib,
#    tt timing and tt leakage, and mini_mvt_<supply>_ss_ffleak_abc.lib, the
#    setup corner's (ss) timing priced with the leakage corner's (ff)
#    leakage. Libraries named before corners existed are removed.
python3 -I "$here/lib/make_libs.py" "$here/lib" >/dev/null
for s in 0p75v 0p55v; do
  rm -f "$here"/lib/mini_{rvt,lvt,slvt}_$s.lib "$here/lib/mini_mvt_${s}_abc.lib"
  python3 -I "$root/scripts/mvt/make_abc_lib.py" "$here/lib/mini_mvt_${s}_tt_abc.lib" \
    "$here"/lib/mini_{rvt,lvt,slvt}_${s}_tt.lib -W "$W"
  python3 -I "$root/scripts/mvt/make_abc_lib.py" "$here/lib/mini_mvt_${s}_ss_ffleak_abc.lib" \
    "$here"/lib/mini_{rvt,lvt,slvt}_${s}_ss.lib -W "$W" \
    $(for v in rvt lvt slvt; do echo -L "$here/lib/mini_${v}_${s}_ff.lib"; done)
done

cd "$root/scripts"
for p in techpack techpack_0p55 techpack_mvt techpack_mvt_0p55 techpack_rails techpack_rails_buf techpack_corners; do
  python3 -m circt_flow.techpack check "$here/$p.yaml" >/dev/null \
    || { python3 -m circt_flow.techpack check "$here/$p.yaml"; exit 1; }
done

# 2. The matrix. domain:supply
summary="$out/summary.txt"; : > "$summary"
for ds in core_hv:0p75v core_lv:0p55v; do
  d=${ds%%:*}; s=${ds##*:}
  [ -z "$ONLY" ] || [ "$ONLY" = "$d" ] || continue
  # flow: RVT, the stock ABC script. flow_l: RVT, load-aware timing-driven
  # (abc/mvt.abc), the control that separates timing-driven mapping from
  # flavour choice. flow_mvt: the merged priced liberty, load-aware.
  for arm in flow flow_l flow_mvt; do
    cfg="$here/designs/$d/${arm%_l}.yaml"
    extra=()
    [ "$arm" = flow_l ] && extra=(--abc-read-lib-args=-l --abc-script-file "$here/abc/mvt.abc")
    [ "$arm" = flow_mvt ] && [ -n "$VTSWAP" ] && extra=("$VTSWAP")
    run="$out/$d.$arm"
    period=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['design']['clock_period_ps'])" "$cfg")
    echo "[mvt-mini] $d $arm (${period} ps) -> $run"
    if ! python3 -m circt_flow "$cfg" "$run" ${extra[@]+"${extra[@]}"} > "$run.log" 2>&1; then
      echo "[mvt-mini] FAILED, see $run.log" >&2; tail -20 "$run.log" >&2; exit 1
    fi
    grep -h "Relaxing user-specified delay target" "$run.log" | tail -1 || true
    python3 -I "$here/vt_report.py" "$run/1_synth/${d}_openroad.v" "$d" \
      --lib "$here/lib/mini_rvt_${s}_tt.lib" --lib "$here/lib/mini_lvt_${s}_tt.lib" \
      --lib "$here/lib/mini_slvt_${s}_tt.lib" --period-ps "$period" $STA \
      --label "$d.$arm" --json "$run.vt.json" | tee -a "$summary"
  done
done

# 3. mvt_top: core_lv on VDD_LV, core_hv on VDD_HV, the glue on the default
#    rail, stitched into one hierarchical netlist, with its UPF, per-mode
#    SDCs (func, eco) and crossings. Each domain's flavours are reported
#    from the stitched OpenSTA netlist against that rail's REAL per-flavour
#    libraries: the rail's liberties after its priced primary one. With
#    --sta the flow's own STA stage times each domain x corner x mode and
#    the whole chip (<run>/sta/summary.json). mvt_top_ls is the same with
#    level shifters inserted (flow_ls.yaml).
if [ -z "$ONLY" ] || [ "$ONLY" = mvt_top ]; then
  cfg="$here/designs/mvt_top/flow.yaml"; run="$out/mvt_top"
  period=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['design']['clock_period_ps'])" "$cfg")
  echo "[mvt-mini] mvt_top power domains (${period} ps) -> $run"
  if ! python3 -m circt_flow "$cfg" "$run" $STA $VTSWAP > "$run.log" 2>&1; then
    echo "[mvt-mini] FAILED, see $run.log" >&2; tail -20 "$run.log" >&2; exit 1
  fi
  grep -h "^\[power\] crossings\|^\[power\]   \|^\[sta\]   \|^\[sta\] [pc]" "$run.log" \
    | sed 's/^/mvt_top /' | tee -a "$summary" || true
  # name module rail real-lib... per domain, then the remainder (no STA:
  # its top instantiates both domains, which one liberty set cannot time).
  python3 - "$run/power_domains.json" > "$run.domains" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for r in d["domains"] + [d["remainder"]]:
    libs = d["rails"][r["rail"]]["liberties"][1:]
    print(r["name"], r["module"], r["rail"], *libs)
PY
  while read -r name mod rail libs; do
    libargs=(); for l in $libs; do libargs+=(--lib "$l"); done
    nl="$run/1_synth/mvt_top_openroad.v"
    [ "$name" = remainder ] && nl="$run/remainder/1_synth/mvt_top_openroad.v"
    python3 -I "$here/vt_report.py" "$nl" "$mod" "${libargs[@]}" \
      --label "mvt_top.$name@$rail" \
      --json "$run.$name.vt.json" | tee -a "$summary"
  done < "$run.domains"
  cfg="$here/designs/mvt_top/flow_ls.yaml"; run="$out/mvt_top_ls"
  echo "[mvt-mini] mvt_top with level shifters -> $run"
  if ! python3 -m circt_flow "$cfg" "$run" $STA $VTSWAP > "$run.log" 2>&1; then
    echo "[mvt-mini] FAILED, see $run.log" >&2; tail -20 "$run.log" >&2; exit 1
  fi
  grep -h "^\[power\] level shifters\|^\[sta\]   \|^\[sta\] [pc]" "$run.log" \
    | sed 's/^/mvt_top_ls /' | tee -a "$summary" || true
fi

# 3b. mvt_top_buf (only with --only mvt_top_buf, so the default summary is
#    unchanged): mvt_top on techpack_rails_buf.yaml, each rail with its own
#    fanout buffer cell and limit, and u_lv's flavours without slvt.
#    <run>/fanout.json lists each rail's cell and what each module used.
if [ "$ONLY" = mvt_top_buf ]; then
  cfg="$here/designs/mvt_top/flow_buf.yaml"; run="$out/mvt_top_buf"
  echo "[mvt-mini] mvt_top with a buffer and flavours per rail -> $run"
  if ! python3 -m circt_flow "$cfg" "$run" $STA $VTSWAP > "$run.log" 2>&1; then
    echo "[mvt-mini] FAILED, see $run.log" >&2; tail -20 "$run.log" >&2; exit 1
  fi
  grep -h "^\[fanout\]\|^\[vt_swap\]" "$run.log" | sed 's/^/mvt_top_buf /' \
    | tee -a "$summary" || true
fi

# 4. mvt_soc: core_lv on VDD_LV clocked by clk_lv_i, core_hv on VDD_HV by
#    clk_hv_i, two modes (designs/mvt_soc/{func,turbo}.sdc), the corners
#    pack. The flow logs each domain's mapping target ([power] mapping
#    target). Each domain is reported per mode x corner: the stitched
#    netlist's domain module against that corner's real libraries, STA at
#    that mode's period of the clock reaching the domain (zero I/O delays).
if [ -z "$ONLY" ] || [ "$ONLY" = mvt_soc ]; then
  cfg="$here/designs/mvt_soc/flow.yaml"; run="$out/mvt_soc"
  echo "[mvt-mini] mvt_soc power domains, modes func+turbo, corners -> $run"
  if ! python3 -m circt_flow "$cfg" "$run" $STA $VTSWAP > "$run.log" 2>&1; then
    echo "[mvt-mini] FAILED, see $run.log" >&2; tail -20 "$run.log" >&2; exit 1
  fi
  grep -h "mapping target" "$run.log" | tee -a "$summary"
  grep -h "^\[sta\]   \|^\[sta\] [pc]" "$run.log" | sed 's/^/mvt_soc /' \
    | tee -a "$summary" || true
  # name module rail mode period clock corner real-lib... per line
  python3 - "$run/power_domains.json" > "$run.views" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for r in d["domains"]:
    t = r["mapping_target"]
    clk = (t["clock_ports"] or ["clk_i"])[0]
    for mode, mv in t["modes"].items():
        for corner, libs in d["rails"][r["rail"]]["corners"].items():
            print(r["name"], r["module"], r["rail"], mode, mv["period_ps"],
                  clk, corner, *libs)
PY
  while read -r name mod rail mode period clk corner libs; do
    libargs=(); for l in $libs; do libargs+=(--lib "$l"); done
    python3 -I "$here/vt_report.py" "$run/1_synth/mvt_soc_openroad.v" "$mod" \
      "${libargs[@]}" --period-ps "$period" --clock "$clk" $STA \
      --label "mvt_soc.$name.$mode@$corner" \
      --json "$run.$name.$mode.$corner.vt.json" | tee -a "$summary"
  done < "$run.views"
fi
echo "[mvt-mini] summary -> $summary"
