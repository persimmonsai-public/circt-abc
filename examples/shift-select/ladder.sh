#!/usr/bin/env bash
# The closure ladder (doc/ladder.md) on align_unit, ASAP7 RVT, stock ORFS.
#
#   examples/shift-select/ladder.sh <out_dir>
#
# 1. The flow's synthesis (no P&R), once.
# 2. The ladder of designs/align_unit/flow_ladder.yaml, twice from that netlist:
#    sequential (rung after rung, from one placement) and parallel (all rungs
#    at once from one placement). Wall-clock of each.
# 3. A straight run (no ladder, no restart) at the accepted margin.
# 4. The checks: both ladders accept the same rung, and the accepted results of
#    the sequential ladder, the parallel ladder and the straight run are the
#    same files (4_cts.odb, 5_route.odb, 6_final.odb, .v, .def, .sdc and
#    pnr_metrics.json byte for byte; 6_final.spef but for its date line; the
#    GDS files are not compared).
#
# <out_dir> may not be inside this repository. ORFS runs in place under
# $ORFS_HOME, else in the ORFS image with docker (as for `--pnr`); the rungs
# are local processes. A cluster can run them instead: CIRCT_FLOW_LADDER_SUBMIT
# (doc/ladder.md, "Where jobs run"). The values of flow_ladder.yaml are
# illustrative (hold slack of at least 20 ps): ASAP7 closes hold at margin 0
# on this design, so the ladder has nothing to do without them.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
[ $# -ge 1 ] || { sed -n '2,/^set -euo/{/^set -euo/!p}' "$0"; exit 2; }
out=$(realpath -m "$1")
case "$out/" in "$root"/*) echo "ladder.sh: $out is inside $root" >&2; exit 2 ;; esac
mkdir -p "$out"
cfg="$here/designs/align_unit/flow_ladder.yaml"

python3 -I "$root/examples/asap7-mvt/lib/make_libs.py" "$here/lib" >/dev/null
cd "$root/scripts"
export CIRCT_FLOW_NO_CLOCK_GATING=1 CIRCT_FLOW_OUTLINE_AFTER_FLATTEN=1

if [ ! -f "$out/syn/1_synth/align_unit_openroad.v" ]; then
  echo "[ladder] synthesis -> $out/syn"
  python3 -m circt_flow "$cfg" "$out/syn" > "$out/syn.log" 2>&1 \
    || { echo "[ladder] synthesis FAILED, see $out/syn.log" >&2; tail -20 "$out/syn.log" >&2; exit 1; }
fi

secs() { date +%s.%N; }
ladder() {  # ladder <mode>: the ladder from the synthesized netlist
  rm -rf "$out/$1"; mkdir -p "$out/$1"; cp -r "$out/syn/1_synth" "$out/$1/"
  local t0; t0=$(secs)
  python3 -m circt_flow.ladder "$cfg" "$out/$1" --mode "$1" 2>&1 | tee "$out/$1.log"
  awk -v a="$t0" -v b="$(secs)" -v m="$1" 'BEGIN{printf "[ladder] %s: %.0f s wall-clock\n", m, b-a}' \
    | tee -a "$out/$1.log"
}
ladder sequential
ladder parallel

acc=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['value'])" \
      "$out/sequential/pnr/ladder/RESULT.json")
echo "[ladder] straight run at hold margin $acc"
rm -rf "$out/straight"; mkdir -p "$out/straight"; cp -r "$out/syn/1_synth" "$out/straight/"
python3 -I - "$cfg" "$out/straight" "$acc" "$root/scripts" > "$out/straight.log" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[4])
from circt_flow import orfs_pnr, stages
from circt_flow.config import FlowConfig
cfg = FlowConfig.load(sys.argv[1], sys.argv[2])
p = dict(cfg.design.pnr)
del p["ladder"]
p["hold_margin"] = float(sys.argv[3])
sys.exit(orfs_pnr.main(stages.orfs_argv(cfg, p, cfg.pnr_dir)))
PY

python3 -I - "$out" <<'PY'
import json, sys, pathlib
out = pathlib.Path(sys.argv[1])
res = {m: json.load(open(out / m / "pnr/ladder/RESULT.json")) for m in ("sequential", "parallel")}
bad = []
for m, r in res.items():
    print(f"[ladder] {m:10} accepted rung {r['accepted']} (hold_margin {r['value']}), "
          f"{r['wall_seconds']:.0f} s; rungs: "
          + ", ".join(f"{x['value']}:{x['verdict']}" for x in r["rungs"]))
if res["sequential"]["accepted"] != res["parallel"]["accepted"]:
    bad.append("sequential and parallel accepted different rungs")
base = "work/results/asap7/align_unit/base"
dirs = {"sequential": out / "sequential/pnr/accepted", "parallel": out / "parallel/pnr/accepted",
        "straight": out / "straight/pnr"}
# 6_final.spef carries a date line: compared without it (the GDS files are not compared)
same = ["6_final.odb", "6_final.v", "6_final.def", "6_final.sdc", "5_route.odb", "4_cts.odb"]
for name in same:
    h = {k: (d / base / name).read_bytes() for k, d in dirs.items()}
    if len(set(h.values())) != 1:
        bad.append(f"{name} differs between " + ", ".join(sorted(dirs)))
def nodate(p):
    return [l for l in p.read_text().splitlines() if not l.startswith("*DATE")]
sp = {k: nodate(d / base / "6_final.spef") for k, d in dirs.items()}
if any(v != sp["straight"] for v in sp.values()):
    bad.append("6_final.spef differs")
mt = {k: (d / "pnr_metrics.json").read_text() for k, d in dirs.items()}
if len(set(mt.values())) != 1:
    bad.append("pnr_metrics.json differs")
print("[ladder] accepted results of the sequential ladder, the parallel ladder and the "
      "straight run: " + ("identical (" + ", ".join(same + ["6_final.spef", "pnr_metrics.json"]) + ")"
                          if not bad else "; ".join(bad)))
sys.exit(1 if bad else 0)
PY
