"""Scoped commands on query results (doc/scripting.md, section 7), on
examples/mvt-mini's core_lv: data-following clock gating of two slices
(bits 1-4 and 5-12) of the running sum `sum_q` (EXPERIMENTAL: no sequential equivalence proof),
and a ripple of adder cells on the adds of 14 to 70 bits.

    cd scripts
    python3 -I ../examples/mvt-mini/lib/make_libs.py ../examples/mvt-mini/lib
    python3 -m circt_flow run ../examples/scripting/core_lv_scoped.py \\
        ../examples/mvt-mini/designs/core_lv/flow.yaml <out_dir> --strict [-- staged]

`-- staged` stops after map_datapath; continue the run directory with
`python3 -m circt_flow run ../examples/scripting/core_lv_scoped.py --resume <out_dir> -- resume`.
The slices and the width window are illustrations, not recommendations.
"""
mode = argv[0] if argv else "full"

if mode != "resume":
    print(s.elaborate())                          # the queries read the core IR
    print(s.flops("sum_q"))
    for lo, hi in ((1, 4), (5, 12)):
        s.gate_register(s.flops("sum_q").bits(lo, hi), detect="zero")
    s.map_adders(s.adders(width=(14, 70)), form="cells")
    for x in s.db.scoped:
        print(x.command, x.targets, dict(x.values))

if mode == "staged":
    print(s.map_datapath())
    print("stopped after map_datapath; continue with --resume")
else:
    print(s.run(start="map_datapath", stop="syn_map") if mode == "full" else s.run(stop="syn_map"))
    print(s.report_stages())
    print(s.icgs(registers="sum_q"))
