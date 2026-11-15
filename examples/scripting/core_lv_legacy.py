"""LEGACY FORM of core_lv_scoped.py: the same two settings through the
deprecated options syn.clock_gating.data_gate and syn.adders.cells2.*. It
builds the same pass command and netlist as the scoped form, with a
deprecation warning per option; --strict refuses it (run it without).

    cd scripts
    python3 -m circt_flow run ../examples/scripting/core_lv_legacy.py \\
        ../examples/mvt-mini/designs/core_lv/flow.yaml <out_dir>
"""
db = s.db
db.syn.clock_gating.data_gate = "sum_q@1-4@zero,sum_q@5-12@zero"
db.syn.adders.cells2.min_width = 14
db.syn.adders.cells2.max_width = 70
print(s.run(stop="syn_map"))
print(s.report_stages())
