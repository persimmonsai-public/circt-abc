"""Golden command lines: the pass command lines the synthesis stages build
for a set of recipes, recorded with the code before the option registry
(the legacy environment variables read directly) in
data/golden_cmdlines.json; test_options.py checks that the flow builds
the same lines from the same environment and from the same settings given as
dotted options. No tool runs. Regenerate (only for an intended change):
python3 -c 'import golden_cmdlines as g, json, pathlib; ...' (see record()).
"""
import json
import os
import sys
import tempfile
from pathlib import Path



BASE_ENV = {"CIRCT_FLOW_OUTLINE_AFTER_FLATTEN": "1", "CIRCT_FLOW_PREFIX_ADDERS": "12", "CIRCT_FLOW_PREFIX_ARCH": "BRENT-KUNG",
            "CIRCT_FLOW_MUL_CELLS": "1", "CIRCT_FLOW_MUL_CELLS_MERGE": "1",
            "CIRCT_FLOW_ADDER_CELLS": "4"}
BASE_YAML = {"shift_select": {"early": True, "wide": 1, "recipes": "2,2@2,2,3"},
             "clock_gating": True}
DIRECT_ENV = {**BASE_ENV, "CIRCT_FLOW_SHIFT_SELECT_DIRECT": "1",
             "CIRCT_FLOW_SHIFT_SELECT_MAXFO": "6"}
PREFIX2 = {"CIRCT_FLOW_PREFIX2_ADDERS": "56", "CIRCT_FLOW_PREFIX2_ISOLATE": "1",
           "CIRCT_FLOW_PREFIX2_ARCH": "HAN-CARLSON"}
DIRECT_YAML = {"shift_select": {"early": True, "wide": 1, "recipes": "2,2@2,2,3",
                               "direct_decode": True, "select_max_fanout": 6},
              "clock_gating": True}

CASES = {
    "all_off": ({}, {}),
    "base_recipe": (BASE_ENV, BASE_YAML),
    "two_prefix_direct": ({**DIRECT_ENV, **PREFIX2}, DIRECT_YAML),
    "data_gate": ({**BASE_ENV,
                   "CIRCT_FLOW_DATA_GATE": "total_r@0-2@xor,total_r@3-9@zero"}, BASE_YAML),
    "mult_array_nandpp": ({**DIRECT_ENV, "CIRCT_FLOW_MUL_CELLS_TREE": "array",
                           "CIRCT_FLOW_MUL_CELLS_NANDPP": "1"}, DIRECT_YAML),
    "mult_mapfinal_minw": ({**DIRECT_ENV, "CIRCT_FLOW_MUL_CELLS_MAPFINAL": "1",
                            "CIRCT_FLOW_MUL_CELLS_MAPFINAL_MINW": "6"}, DIRECT_YAML),
    "adder_arch_empty": ({"CIRCT_FLOW_ADDER_ARCH": ""}, {}),
    "mult_other": ({"CIRCT_FLOW_MUL_CELLS": "1", "CIRCT_FLOW_MUL_CELLS_GATES": "1",
                    "CIRCT_FLOW_MUL_CELLS_CELL_STAGES": "2", "CIRCT_FLOW_MUL_CELLS_TREE": "array",
                    "CIRCT_FLOW_MUL_CELLS_MERGE": "1", "CIRCT_FLOW_MUL_CELLS_PREFIX": "1",
                    "CIRCT_FLOW_MUL_CELLS_FLATTEN": "1"}, {}),
    "shift_l2_dup": ({**BASE_ENV, "CIRCT_FLOW_SHIFT_SELECT_DIRECT": "1",
                      "CIRCT_FLOW_SHIFT_SELECT_L2GATE": "3",
                      "CIRCT_FLOW_SHIFT_SELECT_DUPFO": "5"}, BASE_YAML),
    "shift_yaml_l2": ({}, {"shift_select": {"early": True, "direct_decode": True,
                                            "l2_range_gate": 4, "l2_range_aligned": True,
                                            "l2_range_compose": True, "select_dup_fanout": 9,
                                            "default_dont_care": True}}),
    "shift_env_l2": ({"CIRCT_FLOW_SHIFT_SELECT_EARLY": "1", "CIRCT_FLOW_SHIFT_SELECT_DIRECT": "1",
                      "CIRCT_FLOW_SHIFT_SELECT_L2GATE": "5", "CIRCT_FLOW_SHIFT_SELECT_L2ALIGNED": "1",
                      "CIRCT_FLOW_SHIFT_SELECT_L2COMPOSE": "1"}, {}),
    "shift_late": ({"CIRCT_FLOW_SHIFT_SELECT": "1", "CIRCT_FLOW_SHIFT_SELECT_MIN_ARMS": "6",
                    "CIRCT_FLOW_SHIFT_SELECT_RADIX": "2", "CIRCT_FLOW_SHIFT_SELECT_WIDE": "1",
                    "CIRCT_FLOW_SHIFT_SELECT_RECIPES": "2,2@2,2",
                    "CIRCT_FLOW_SHIFT_SELECT_DIRECT": "1", "CIRCT_FLOW_SHIFT_SELECT_DONTCARE": "1"}, {}),
    "shift_yaml_late": ({}, {"shift_select": {"early": False, "min_arms": 5, "radix": 8}}),
    "shift_both": ({"CIRCT_FLOW_SHIFT_SELECT": "1"}, {"shift_select": True}),
    "adders_old": ({"CIRCT_FLOW_ADDER_CELLS": "15", "CIRCT_FLOW_ADDER_CELLS_INCREMENTS": "0",
                    "CIRCT_FLOW_PREFIX_ADDERS": "14", "CIRCT_FLOW_ADDER_ARCH": "KOGGE-STONE"}, {}),
    "adders_new": ({"CIRCT_FLOW_PREFIX2_ADDERS": "56", "CIRCT_FLOW_PREFIX2_ISOLATE": "1",
                    "CIRCT_FLOW_ADDCH2_MIN": "20", "CIRCT_FLOW_ADDCH2_MAX": "24"}, {}),
    "clock_gating_off": ({"CIRCT_FLOW_NO_CLOCK_GATING": "1", "CIRCT_FLOW_CG_BITLEVEL": "0",
                          "CIRCT_FLOW_CG_SELECT_DC": "1"},
                         {"clock_gating": {"merge_equivalent_enables": True, "min_gate_bits": 3}}),
    "cleanup": ({"CIRCT_FLOW_PRUNE_ANY_MODULE": "1", "CIRCT_FLOW_PRUNE_CONST_OUTPUTS": "1",
                 "CIRCT_FLOW_PRUNE_OUTPUT_FIELDS": "1", "CIRCT_FLOW_SPECIALIZE_INPUTS": "1",
                 "CIRCT_FLOW_RCP_SEQUENTIAL": "1", "CIRCT_FLOW_NCRB_MULTI_INSTANCE": "1",
                 "CIRCT_FLOW_NCRB_AGGREGATES": "1", "CIRCT_FLOW_NCRB_UNRESET_HOLDS": "1",
                 "CIRCT_FLOW_DEAD_OUTPUT_BITS": "0", "CIRCT_FLOW_DEDUP_REGISTERS": "1",
                 "CIRCT_FLOW_PRIORITY_DECODE": "1", "CIRCT_FLOW_PRIORITY_MIN_WIDTH": "4",
                 "CIRCT_FLOW_NARROW_EXT_MUL": "1", "CIRCT_FLOW_NARROW_SPARSE_SHIFT": "1",
                 "CIRCT_FLOW_ADD_CARRY_IN": "1", "CIRCT_FLOW_BARREL_SHIFT": "1",
                 "CIRCT_FLOW_ADDER_ARCH": "none", "CIRCT_FLOW_LOCALIZE_RMW": "0",
                 "CIRCT_FLOW_NARROW_WHOLE_VALUE_READS": "0",
                 "CIRCT_FLOW_PRUNE_RESIDUAL_OUTPUTS": "0"}, {}),
    "ncrb_one": ({"CIRCT_FLOW_NCRB_AGGREGATES": "1", "CIRCT_FLOW_PRUNE_OUTPUT_FIELDS": "1",
                  "CIRCT_FLOW_PRIORITY_DECODE": "1"}, {}),
    "abc": ({"CIRCT_FLOW_NF_RELAX": "5", "CIRCT_FLOW_DEEPSYN_J": "4", "CIRCT_FLOW_DEEPSYN_SEC": "30",
             "CIRCT_FLOW_SPECIALIZE_MUX_ARMS": "1", "CIRCT_FLOW_SELECT_PREFIX_OR": "1",
             "CIRCT_FLOW_FREE_DEAD_OUTPUTS": "1", "CIRCT_FLOW_NARROW_MUX": "1",
             "CIRCT_FLOW_MAX_FANOUT": "40", "CIRCT_FLOW_DP_OVERRIDES": "<OUT>/ov.json",
             "CIRCT_FLOW_DP_JSON": "<OUT>/dp.json", "CIRCT_FLOW_OUTLINE_DATAPATH": "1",
             "CIRCT_FLOW_DP_IDS": "<OUT>/ids.txt", "CIRCT_FLOW_POST_MAP_SWEEP": "1",
             "CIRCT_FLOW_SWEEP_STUBS": "<OUT>/a.v,<OUT>/b.v"}, {}),
    "abc_internalize": ({"CIRCT_FLOW_INTERNALIZE_SELF_FED": "1", "CIRCT_FLOW_OUTLINE_DATAPATH": "1",
                         "CIRCT_FLOW_POST_MAP_SWEEP": "1"}, {}),
    "partitioned": ({"CIRCT_FLOW_GROUP_BY_DEPTH": "1", "CIRCT_FLOW_ISOLATE_SUBSTR": "_dp",
                     "CIRCT_FLOW_ISOLATE_EACH": "1", "CIRCT_FLOW_GROUP_SCRIPTS": "<OUT>/gs.json",
                     "CIRCT_FLOW_NARROW_MUX": "1", "CIRCT_FLOW_SPECIALIZE_MUX_ARMS": "1",
                     "CIRCT_FLOW_DEEPSYN_J": "2"}, {"_partitions": 2}),
    "partitioned_map": ({"CIRCT_FLOW_GROUP_MAP": "<OUT>/gm.json", "CIRCT_FLOW_GROUP_BY_DEPTH": "1",
                         "CIRCT_FLOW_ISOLATE_SUBSTR": "_dp"}, {"_partitions": 3}),
}




def record(scripts, attributes=False, only=None):
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from circt_flow import stages, config, abc_partition
    out = {}
    for name, (env, yaml_extra) in sorted(CASES.items()):
        if only and name not in only:
            continue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            env = {k: v.replace("<OUT>", str(td)) for k, v in env.items()}
            yaml_extra = dict(yaml_extra)
            parts = yaml_extra.pop("_partitions", None)
            if "CIRCT_FLOW_GROUP_SCRIPTS" in env:
                Path(env["CIRCT_FLOW_GROUP_SCRIPTS"]).write_text('{"g1": "x.abc"}')
            import yaml
            doc = {"design": {"top_module": "top", "clock_period_ps": 1000},
                   "pdk": {"synth_lib_tt": ["/LIB/a.lib", "/LIB/b.lib"]}, **yaml_extra}
            (td / "flow.yaml").write_text(yaml.safe_dump(doc))
            saved = dict(os.environ)
            for k in list(os.environ):
                if k.startswith(("CIRCT_FLOW_", "CIRCT_VT_")):
                    del os.environ[k]
            cmds = []
            orig = (stages._run, stages._binary_has_pass, os.replace,
                    abc_partition.map_groups, abc_partition.combine)

            class R:
                def __init__(s, n):
                    s.name, s.returncode, s.seconds = n, 0, 1.0
            try:
                kw = dict(circt_abc_opt="/BIN/circt-abc-opt", circt_opt="/BIN/circt-opt",
                          abc_bin="/BIN/abc", partitions=parts)
                if attributes:
                    from circt_flow import options
                    kw["settings"] = options.legacy_to_settings(env)
                else:
                    os.environ.update(env)
                cfg = config.FlowConfig.load(td / "flow.yaml", td / "out", **kw)
                stages._run = lambda cmd, stdout=None, env=None: cmds.append([str(c) for c in cmd])
                stages._binary_has_pass = lambda b, p: True
                os.replace = lambda a, b: None
                abc_partition.map_groups = lambda *a, **k: (
                    cmds.append(["map_groups", *[str(x) for x in a[1:]],
                                 json.dumps(k.get("per_group_extra"), sort_keys=True)]),
                    [R("g0")])[1]
                abc_partition.combine = lambda *a, **k: None
                stages.hw_aggregate_to_comb(cfg)
                stages.structural_mapping(cfg)
                stages.flatten_shell(cfg)
                stages.merge_and_map(cfg)
                Path(cfg.mapped_mlir).parent.mkdir(parents=True, exist_ok=True)
                Path(cfg.mapped_mlir).write_text("hw.module @top_residual() {}\n")
                stages.export_netlist(cfg)
            finally:
                (stages._run, stages._binary_has_pass, os.replace,
                 abc_partition.map_groups, abc_partition.combine) = orig
                os.environ.clear()
                os.environ.update(saved)
            root = str(Path(scripts).resolve().parent)
            out[name] = [[a.replace(str(td), "<OUT>").replace(root, "<ROOT>")
                          for a in c] for c in cmds]
    return out



def stage_commands(cfg):
    """The commands the synthesis stages build for `cfg` (no tool runs);
    the same stages and stubs as record()."""
    from circt_flow import stages, abc_partition
    cmds = []
    orig = (stages._run, stages._binary_has_pass, os.replace,
            abc_partition.map_groups, abc_partition.combine)

    class R:
        def __init__(s, n):
            s.name, s.returncode, s.seconds = n, 0, 1.0
    try:
        stages._run = lambda cmd, stdout=None, env=None: cmds.append([str(c) for c in cmd])
        stages._binary_has_pass = lambda b, p: True
        os.replace = lambda a, b: None
        abc_partition.map_groups = lambda *a, **k: (
            cmds.append(["map_groups", *[str(x) for x in a[1:]],
                         json.dumps(k.get("per_group_extra"), sort_keys=True)]),
            [R("g0")])[1]
        abc_partition.combine = lambda *a, **k: None
        stages.hw_aggregate_to_comb(cfg)
        stages.structural_mapping(cfg)
        stages.flatten_shell(cfg)
        stages.merge_and_map(cfg)
        Path(cfg.mapped_mlir).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.mapped_mlir).write_text("hw.module @top_residual() {}\n")
        stages.export_netlist(cfg)
    finally:
        (stages._run, stages._binary_has_pass, os.replace,
         abc_partition.map_groups, abc_partition.combine) = orig
    return cmds
