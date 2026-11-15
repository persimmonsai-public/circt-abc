# Python recipes (session API, doc/scripting.md)

Three recipes for `python3 -m circt_flow run`, on the `core_lv` design of
`examples/mvt-mini` (a 16-bit LFSR feeding a running sum, both behind an
enable; the invented mvt-mini library, which `lib/make_libs.py` writes):

* `core_lv_recipe.py`: options set as attributes (`s.db`), the stages run one
  at a time, the results printed;
* `core_lv_scoped.py`: scoped commands on query results: data-following clock
  gating of two slices of `sum_q` (`s.gate_register`, EXPERIMENTAL) and a
  ripple of adder cells on the adds of 14 to 70 bits (`s.map_adders`); runs
  under `--strict`; `-- staged` stops after `map_datapath`, `--resume <out_dir>
  -- resume` continues it;
* `core_lv_legacy.py`: the same two settings with the deprecated options
  `syn.clock_gating.data_gate` / `syn.adders.cells2.*`: the same pass command
  and netlist, with deprecation warnings; refused under `--strict`.

```sh
cd scripts
python3 -I ../examples/mvt-mini/lib/make_libs.py ../examples/mvt-mini/lib
python3 -m circt_flow run ../examples/scripting/core_lv_scoped.py \
    ../examples/mvt-mini/designs/core_lv/flow.yaml /tmp/core_lv_scoped --strict
python3 -m circt_flow run ../examples/scripting/core_lv_legacy.py \
    ../examples/mvt-mini/designs/core_lv/flow.yaml /tmp/core_lv_legacy
```

The two runs write the same `1_synth/core_lv_openroad.v`; each run directory
holds `settings.json` (with `scoped_applied`: every target and whether it was
applied), `session.json` and a copy of the recipe. The values in the recipes
are illustrations, not recommendations.
