"""A recipe as a Python script: examples/mvt-mini's core_lv (a 16-bit LFSR
feeding a running sum, both behind an enable) through synthesis, with two
options set as attributes and the stages run one at a time.

    cd scripts
    python3 -I ../examples/mvt-mini/lib/make_libs.py ../examples/mvt-mini/lib
    python3 -m circt_flow run ../examples/scripting/core_lv_recipe.py \\
        ../examples/mvt-mini/designs/core_lv/flow.yaml <out_dir> --strict

The values are illustrations, not recommendations.
"""
db = s.db
db.syn.adders.cells.min_width = 14          # adds of 14 bits and more: a ripple of adder cells
db.syn.fanout.max = 12                      # balanced buffer trees above 12 loads
print(s.elaborate())
print(s.map_datapath())
r = s.syn_map()
print(r)
print(s.report_changed())
print(s.report_stages())
q = s.report_qor()
print({k: q.get(k) for k in ("cells", "area")})
