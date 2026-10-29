// RUN: circt-abc-opt --map-shell-logic=liberty=%S/shell_cells.lib %s | FileCheck %s
// A shell OR/AND must map to the single direct cell (OR2 / AND2) rather
// than the per-cell-cheaper inverting cell plus an inverter, which costs
// more area in total and one more level (on cv64a6, eight such pairs sat on
// the worst register-to-register path).
// An XOR, which has only an inverted match here, still gets XNOR2 + INV.

// CHECK-LABEL: hw.module @shell
// CHECK: hw.instance "u_shell_TOR2_{{[0-9]+}}" @TOR2
// CHECK-NOT: @TNOR2
// CHECK: hw.instance "u_shell_TAND2_{{[0-9]+}}" @TAND2
// CHECK-NOT: @TNAND2
// CHECK: hw.instance "u_shell_TXNOR2_{{[0-9]+}}" @TXNOR2
// CHECK: hw.instance "u_shell_TINV_{{[0-9]+}}" @TINV
hw.module @shell(in %a : i1, in %b : i1, in %c : i1, in %d : i1, out o : i1, out n : i1, out x : i1) {
  %0 = comb.or %a, %b : i1
  %1 = comb.and %c, %d : i1
  %2 = comb.xor %a, %c : i1
  hw.output %0, %1, %2 : i1, i1, i1
}
