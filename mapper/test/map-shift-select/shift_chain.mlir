// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=4,2@4,2,2;2,2@2,2,4" --canonicalize %s 2>&1 | FileCheck %s
//
// A priority mux chain over distinct constants of one amount (the form a `case` has before
// canonicalization) becomes the mixed-radix network.
// Equivalence: scripts/circt_flow/tests/test_shift_select.py.

// CHECK: [map-shift-select] recipe off={{[0-9]+}} radices={{[0-9,]+}}, caps={{[0-9,]+}}
// CHECK-LABEL: hw.module @shchain
// CHECK-NOT: comb.mux
hw.module @shchain(in %x : i16, in %amt : i4, out y : i32) {
  %k0 = hw.constant 0 : i4
  %k1 = hw.constant 1 : i4
  %k2 = hw.constant 2 : i4
  %k3 = hw.constant 3 : i4
  %k4 = hw.constant 4 : i4
  %k5 = hw.constant 5 : i4
  %k6 = hw.constant 6 : i4
  %k7 = hw.constant 7 : i4
  %k8 = hw.constant 8 : i4
  %k9 = hw.constant 9 : i4
  %k10 = hw.constant 10 : i4
  %k11 = hw.constant 11 : i4
  %k12 = hw.constant 12 : i4
  %k13 = hw.constant 13 : i4
  %k14 = hw.constant 14 : i4
  %k15 = hw.constant 15 : i4
  %s0 = comb.icmp ceq %amt, %k0 : i4
  %s1 = comb.icmp ceq %amt, %k1 : i4
  %s2 = comb.icmp ceq %amt, %k2 : i4
  %s3 = comb.icmp ceq %amt, %k3 : i4
  %s4 = comb.icmp ceq %amt, %k4 : i4
  %s5 = comb.icmp ceq %amt, %k5 : i4
  %s6 = comb.icmp ceq %amt, %k6 : i4
  %s7 = comb.icmp ceq %amt, %k7 : i4
  %s8 = comb.icmp ceq %amt, %k8 : i4
  %s9 = comb.icmp ceq %amt, %k9 : i4
  %s10 = comb.icmp ceq %amt, %k10 : i4
  %s11 = comb.icmp ceq %amt, %k11 : i4
  %s12 = comb.icmp ceq %amt, %k12 : i4
  %s13 = comb.icmp ceq %amt, %k13 : i4
  %s14 = comb.icmp ceq %amt, %k14 : i4
  %s15 = comb.icmp ceq %amt, %k15 : i4
  %zh0 = hw.constant 0 : i16
  %w0 = comb.concat %zh0, %x : i16, i16
  %zh1 = hw.constant 0 : i15
  %zl1 = hw.constant 0 : i1
  %w1 = comb.concat %zh1, %x, %zl1 : i15, i16, i1
  %zh2 = hw.constant 0 : i14
  %zl2 = hw.constant 0 : i2
  %w2 = comb.concat %zh2, %x, %zl2 : i14, i16, i2
  %zh3 = hw.constant 0 : i13
  %zl3 = hw.constant 0 : i3
  %w3 = comb.concat %zh3, %x, %zl3 : i13, i16, i3
  %zh4 = hw.constant 0 : i12
  %zl4 = hw.constant 0 : i4
  %w4 = comb.concat %zh4, %x, %zl4 : i12, i16, i4
  %zh5 = hw.constant 0 : i11
  %zl5 = hw.constant 0 : i5
  %w5 = comb.concat %zh5, %x, %zl5 : i11, i16, i5
  %zh6 = hw.constant 0 : i10
  %zl6 = hw.constant 0 : i6
  %w6 = comb.concat %zh6, %x, %zl6 : i10, i16, i6
  %zh7 = hw.constant 0 : i9
  %zl7 = hw.constant 0 : i7
  %w7 = comb.concat %zh7, %x, %zl7 : i9, i16, i7
  %zh8 = hw.constant 0 : i8
  %zl8 = hw.constant 0 : i8
  %w8 = comb.concat %zh8, %x, %zl8 : i8, i16, i8
  %zh9 = hw.constant 0 : i7
  %zl9 = hw.constant 0 : i9
  %w9 = comb.concat %zh9, %x, %zl9 : i7, i16, i9
  %zh10 = hw.constant 0 : i6
  %zl10 = hw.constant 0 : i10
  %w10 = comb.concat %zh10, %x, %zl10 : i6, i16, i10
  %zh11 = hw.constant 0 : i5
  %zl11 = hw.constant 0 : i11
  %w11 = comb.concat %zh11, %x, %zl11 : i5, i16, i11
  %zh12 = hw.constant 0 : i4
  %zl12 = hw.constant 0 : i12
  %w12 = comb.concat %zh12, %x, %zl12 : i4, i16, i12
  %zh13 = hw.constant 0 : i3
  %zl13 = hw.constant 0 : i13
  %w13 = comb.concat %zh13, %x, %zl13 : i3, i16, i13
  %zh14 = hw.constant 0 : i2
  %zl14 = hw.constant 0 : i14
  %w14 = comb.concat %zh14, %x, %zl14 : i2, i16, i14
  %zh15 = hw.constant 0 : i1
  %zl15 = hw.constant 0 : i15
  %w15 = comb.concat %zh15, %x, %zl15 : i1, i16, i15
  %c15 = comb.mux %s15, %w15, %w15 : i32
  %c14 = comb.mux %s14, %w14, %c15 : i32
  %c13 = comb.mux %s13, %w13, %c14 : i32
  %c12 = comb.mux %s12, %w12, %c13 : i32
  %c11 = comb.mux %s11, %w11, %c12 : i32
  %c10 = comb.mux %s10, %w10, %c11 : i32
  %c9 = comb.mux %s9, %w9, %c10 : i32
  %c8 = comb.mux %s8, %w8, %c9 : i32
  %c7 = comb.mux %s7, %w7, %c8 : i32
  %c6 = comb.mux %s6, %w6, %c7 : i32
  %c5 = comb.mux %s5, %w5, %c6 : i32
  %c4 = comb.mux %s4, %w4, %c5 : i32
  %c3 = comb.mux %s3, %w3, %c4 : i32
  %c2 = comb.mux %s2, %w2, %c3 : i32
  %c1 = comb.mux %s1, %w1, %c2 : i32
  %c0 = comb.mux %s0, %w0, %c1 : i32
  hw.output %c0 : i32
}
