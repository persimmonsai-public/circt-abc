#!/usr/bin/env bash
# run_equivalence_check.sh -- RTL-vs-netlist logical equivalence check.
#
# An RTL-vs-netlist LEC harness that circt-abc owns. Uses Yosys's
# purpose-built equiv_* commands (equiv_make/equiv_induct/equiv_status), the
# standard OSS-EDA mechanism for "I have RTL and an externally-produced gate
# netlist, prove they match" -- does not require register names to match 1:1
# (equiv_induct does internal correspondence matching), though it works faster
# when register structure is preserved (mapper should not retime by default).
#
# Usage:
#   run_equivalence_check.sh <top_module> <mapped_netlist.v> "<liberty.lib> [more.lib ...]" <rtl_file_list...>
#
# Optional env var RTL_PARAMS: extra `read_slang` flags for the gold (RTL)
# elaboration only, e.g. RTL_PARAMS="-G CounterWidth=64 -G ProvideValUpd=0"
# for a module whose default parameters don't match how it's actually
# instantiated in the real design (get the real values from its instantiation
# site, e.g. ibex_cs_registers.sv's `ibex_counter #(...)` -- guessing wrong
# just fails the check with a real-looking but spurious mismatch, it does not
# silently pass).
#
# Validated 2026-08-26 on real M1b (run_circt_native_synth.sh) output:
#   - ibex_alu (combinational-only): PASSED, 166/166 $equiv cells.
#   - ibex_counter (mcycle_counter_i instance, CounterWidth=64,
#     ProvideValUpd=0; sequential, exercises real DFF-cell register mapping):
#     PASSED, 128/128 $equiv cells.
# Getting there needed four real fixes vs. this script's original recipe, all
# still relevant to any future run:
#   - RTL frontend: plain `read_verilog -sv` cannot parse real ibex sources (SV
#     packages/enums/unpacked-array ports) -- needs `read_slang` instead (same
#     fix already required for synth.tcl and this project's own metrics script).
#   - `read_liberty -lib` only builds empty blackbox modules (port list, no
#     function) -- equiv_induct's SAT engine then has no model for any std
#     cell and fails outright. Needs a real functional import (drop -lib),
#     with `-ignore_miss_func` to skip the handful of cells (e.g. the ICG
#     clock-gate cell) whose liberty entry has no output `function` attribute
#     because they're inherently non-combinational.
#   - `equiv_make` on a netlist with real module hierarchy (a wrapper
#     instantiating `_residual`/`_residual_bits` children) needs an explicit
#     `flatten` before `equiv_induct` -- otherwise the SAT engine has no model
#     for the child-instance cells either ("No SAT model available for cell
#     ...").
#   - Any module with real registers needs `async2sync` between `flatten` and
#     `equiv_induct` -- without it, liberty-derived async-reset FF cells
#     ($_DFF_PP0_ etc.) have no model equiv_induct's induction proof can use
#     ("No SAT model available for async FF cell ... Consider running
#     async2sync or clk2fflogic first").
#
# M0 circt-lec spike RESULT: not viable here without extra work --
# (1) circt-lec's --run mode JITs an SMT query against Z3 at runtime; no
#     libz3 is installed on this machine (checked common lib paths, pip,
#     dnf: none found), so --run fails with unresolved Z3_* symbols even on
#     a trivial two-module combinational comparison.
# (2) more fundamentally, circt-lec's own dialect registry (per its
#     "Available dialects" error message) does not include `seq` at all --
#     it rejected a module using seq.firreg/seq.to_clock outright
#     ("Dialect `seq' not found"). Real per-commit LEC on sequential
#     designs would need an explicit register-externalization/lowering
#     step before circt-lec ever sees the IR, not just a working Z3.
# This script (Yosys equiv_*, already proven, no extra dependency) is
# therefore PRIMARY, not fallback, for both per-module and full-ibex_top
# checks -- revisit circt-lec only if Z3 gets installed AND a
# seq-externalization pre-pass is added; not worth pursuing further until
# the Yosys path shows an actual limitation.
set -euo pipefail

TOP="$1"; NETLIST="$2"; LIBS="$3"; shift 3
RTL_FILES=("$@")

YOSYS_BIN="${YOSYS:-yosys}"

READ_LIBERTY_CMDS=""
for lib in $LIBS; do
  READ_LIBERTY_CMDS+="read_liberty -ignore_miss_func $lib
"
done

"$YOSYS_BIN" -p "
read_slang --top $TOP ${RTL_PARAMS:-} ${RTL_FILES[*]}
rename $TOP gold

read_verilog $NETLIST
rename $TOP gate

$READ_LIBERTY_CMDS
equiv_make gold gate equiv
hierarchy -top equiv
flatten equiv
async2sync
equiv_induct equiv
equiv_status -assert equiv
"

echo "PASS: $TOP RTL vs. $NETLIST are logically equivalent."
