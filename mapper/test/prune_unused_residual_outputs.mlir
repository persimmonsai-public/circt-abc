// RUN: circt-abc-opt --prune-unused-residual-outputs --canonicalize %s | FileCheck %s
// The residual's second output (%dead, a whole cone) is never read by the
// top: its port, its hw.output operand and its cone must go; the used
// output survives with the same value.
// CHECK-LABEL: hw.module @leaf_residual(in %a : i8, in %b : i8, out used : i8)
// CHECK-NOT: comb.xor
// CHECK: hw.module @top
// CHECK: %[[R:.+]] = hw.instance "u" @leaf_residual(a: %x: i8, b: %y: i8) -> (used: i8)
// CHECK: hw.output %[[R]] : i8
hw.module @leaf_residual(in %a : i8, in %b : i8, out used : i8, out dead : i8) {
  %s = comb.add %a, %b : i8
  %d = comb.xor %a, %b : i8
  %e = comb.mul %d, %d : i8
  hw.output %s, %e : i8, i8
}
hw.module @top(in %x : i8, in %y : i8, out o : i8) {
  %u, %dead = hw.instance "u" @leaf_residual(a: %x: i8, b: %y: i8) -> (used: i8, dead: i8)
  hw.output %u : i8
}
