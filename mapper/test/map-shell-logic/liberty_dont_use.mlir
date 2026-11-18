// RUN: circt-abc-opt --liberty-dont-use='TOR2*,NOSUCH*' --map-shell-logic=liberty=%S/shell_cells.lib %s | FileCheck %s
// RUN: circt-abc-opt --map-shell-logic=liberty=%S/shell_cells.lib %s | FileCheck %s --check-prefix=PLAIN
// RUN: not circt-abc-opt --liberty-dont-use='TINV*' --map-shell-logic=liberty=%S/shell_cells.lib %s 2>&1 | FileCheck %s --check-prefix=NOINV
// --liberty-dont-use: a cell whose name matches a pattern is never CHOSEN by a
// mapping pass. With the direct OR cell excluded the shell OR takes the next
// cheapest implementation (the inverting cell and an inverter); the AND is
// unaffected. An instance of an excluded cell that is already in the design
// is left alone.

// CHECK-LABEL: hw.module @shell
// CHECK-NOT: hw.instance "u_shell_TOR2
// CHECK: hw.instance "u_shell_TNOR2_{{[0-9]+}}" @TNOR2
// CHECK: hw.instance "u_shell_TAND2_{{[0-9]+}}" @TAND2
// CHECK: hw.instance "keep" @TOR2

// With every inverter excluded, a match that needs one to complete it is not offered:
// the OR and the AND still get their direct cells, and the XOR, which has only an
// inverted match here, is reported as having no cell (the pass's own diagnostic).
// NOINV: no liberty cell matches table 6 over 2 inputs

// PLAIN-LABEL: hw.module @shell
// PLAIN: hw.instance "u_shell_TOR2_{{[0-9]+}}" @TOR2
// PLAIN-NOT: @TNOR2
hw.module.extern @TOR2(in %A : i1, in %B : i1, out Y : i1)
hw.module @shell(in %a : i1, in %b : i1, in %c : i1, in %d : i1, out o : i1, out n : i1, out k : i1, out x : i1) {
  %0 = comb.or %a, %b : i1
  %1 = comb.and %c, %d : i1
  %2 = comb.xor %a, %c : i1
  %keep.Y = hw.instance "keep" @TOR2(A: %a: i1, B: %d: i1) -> (Y: i1)
  hw.output %0, %1, %keep.Y, %2 : i1, i1, i1, i1
}
