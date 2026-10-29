// RUN: circt-abc-opt --outline-datapath-ops %s | FileCheck %s
// The 32-bit add (with its arch stamp) and the ordered compare are outlined
// into single-op residual modules; the 4-bit add, the eq compare and the
// constant-amount shift stay.
// CHECK: hw.module @m_dpop0_add_residual(in %a0 : i32, in %a1 : i32, out y : i32)
// CHECK: comb.add %a0, %a1 {synth.test.arch = "BRENT-KUNG"} : i32
// CHECK: hw.module @m_dpop1_cmp_residual(in %a0 : i32, in %a1 : i32, out y : i1)
// CHECK: hw.module @m(
// CHECK: hw.instance "dp0" @m_dpop0_add_residual(a0: %a: i32, a1: %b: i32) -> (y: i32)
// CHECK: hw.instance "dp1" @m_dpop1_cmp_residual
// CHECK: comb.icmp eq
// CHECK: comb.add %c, %d : i4
// CHECK: comb.shl
hw.module @m(in %a : i32, in %b : i32, in %c : i4, in %d : i4, out s : i32, out lt : i1, out e : i1, out t : i4, out sh : i32) {
  %s = comb.add %a, %b {synth.test.arch = "BRENT-KUNG"} : i32
  %lt = comb.icmp ult %a, %b : i32
  %e = comb.icmp eq %a, %b : i32
  %t = comb.add %c, %d : i4
  %k = hw.constant 3 : i32
  %sh = comb.shl %a, %k : i32
  hw.output %s, %lt, %e, %t, %sh : i32, i1, i1, i4, i32
}

// Constant-aware boundary: a zero-extended operand crosses as its
// non-constant piece only; the constant and the concat are rebuilt inside.
// CHECK: hw.module @z_dpop0_mul_residual(in %a0_1 : i4, in %a1_1 : i4, out y : i8)
// CHECK: hw.constant 0 : i4
// CHECK: comb.concat
// CHECK: comb.mul
// CHECK: hw.instance "dp0" @z_dpop0_mul_residual(a0_1: %a: i4, a1_1: %b: i4)
hw.module @z(in %a : i4, in %b : i4, out y : i8) {
  %z = hw.constant 0 : i4
  %ea = comb.concat %z, %a : i4, i4
  %eb = comb.concat %z, %b : i4, i4
  %m = comb.mul %ea, %eb : i8
  hw.output %m : i8
}
