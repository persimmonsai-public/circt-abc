// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,2;4@4,2" --canonicalize %s 2>&1 | FileCheck %s
//
// A conditionally negated word (add form: invert, add one) feeding a one-hot select over its
// shifted copies: the negate becomes a ripple inside the network.
// Equivalence is proven by scripts/circt_flow/tests/test_shift_select.py.

// CHECK: [map-shift-select] recipe off={{[0-9]+}} radices={{[0-9,]+}} caps={{[0-9,]+}} negate-ripple=1
// CHECK-LABEL: hw.module @shneg
// CHECK-NOT: comb.mux
// CHECK-NOT: comb.add
hw.module @shneg(in %x : i16, in %sign : i1, in %amt : i3, out y : i32) {
  %true = hw.constant true
  %false = hw.constant false
  %ones = hw.constant -1 : i16
  %one = hw.constant 1 : i17
  %zw = hw.constant 0 : i32
  %xz = comb.concat %false, %x : i1, i16
  %nx = comb.xor %x, %ones : i16
  %cat = comb.concat %true, %nx : i1, i16
  %neg = comb.add %cat, %one : i17
  %root = comb.mux %sign, %neg, %xz : i17
  %rt = comb.extract %root from 16 : (i17) -> i1
  %k0 = hw.constant 0 : i3
  %k1 = hw.constant 1 : i3
  %k2 = hw.constant 2 : i3
  %k3 = hw.constant 3 : i3
  %k4 = hw.constant 4 : i3
  %k5 = hw.constant 5 : i3
  %k6 = hw.constant 6 : i3
  %k7 = hw.constant 7 : i3
  %s0 = comb.icmp ceq %amt, %k0 : i3
  %s1 = comb.icmp ceq %amt, %k1 : i3
  %s2 = comb.icmp ceq %amt, %k2 : i3
  %s3 = comb.icmp ceq %amt, %k3 : i3
  %s4 = comb.icmp ceq %amt, %k4 : i3
  %s5 = comb.icmp ceq %amt, %k5 : i3
  %s6 = comb.icmp ceq %amt, %k6 : i3
  %s7 = comb.icmp ceq %amt, %k7 : i3
  %rep0 = comb.replicate %rt : (i1) -> i15
  %w0 = comb.concat %rep0, %root : i15, i17
  %m0 = comb.mux bin %s0, %w0, %zw : i32
  %rep1 = comb.replicate %rt : (i1) -> i14
  %zl1 = hw.constant 0 : i1
  %w1 = comb.concat %rep1, %root, %zl1 : i14, i17, i1
  %m1 = comb.mux bin %s1, %w1, %zw : i32
  %rep2 = comb.replicate %rt : (i1) -> i13
  %zl2 = hw.constant 0 : i2
  %w2 = comb.concat %rep2, %root, %zl2 : i13, i17, i2
  %m2 = comb.mux bin %s2, %w2, %zw : i32
  %rep3 = comb.replicate %rt : (i1) -> i12
  %zl3 = hw.constant 0 : i3
  %w3 = comb.concat %rep3, %root, %zl3 : i12, i17, i3
  %m3 = comb.mux bin %s3, %w3, %zw : i32
  %rep4 = comb.replicate %rt : (i1) -> i11
  %zl4 = hw.constant 0 : i4
  %w4 = comb.concat %rep4, %root, %zl4 : i11, i17, i4
  %m4 = comb.mux bin %s4, %w4, %zw : i32
  %rep5 = comb.replicate %rt : (i1) -> i10
  %zl5 = hw.constant 0 : i5
  %w5 = comb.concat %rep5, %root, %zl5 : i10, i17, i5
  %m5 = comb.mux bin %s5, %w5, %zw : i32
  %rep6 = comb.replicate %rt : (i1) -> i9
  %zl6 = hw.constant 0 : i6
  %w6 = comb.concat %rep6, %root, %zl6 : i9, i17, i6
  %m6 = comb.mux bin %s6, %w6, %zw : i32
  %rep7 = comb.replicate %rt : (i1) -> i8
  %zl7 = hw.constant 0 : i7
  %w7 = comb.concat %rep7, %root, %zl7 : i8, i17, i7
  %m7 = comb.mux bin %s7, %w7, %zw : i32
  %none = comb.or %s0, %s1, %s2, %s3, %s4, %s5, %s6, %s7 : i1
  %md = comb.mux bin %none, %zw, %w7 : i32
  %y = comb.or bin %m0, %m1, %m2, %m3, %m4, %m5, %m6, %m7, %md : i32
  hw.output %y : i32
}
