// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=4,2@4,2,2;2,2@2,2,4" --canonicalize %s 2>&1 | FileCheck %s
//
// Direct decode: the amount is a constant added to a slice of the input (wrapping), so the digit
// selects are built as cell networks of the raw input bits; no adder or comparator remains.
// Equivalence (exact, default arm included): scripts/circt_flow/tests/test_shift_select.py.

// CHECK: [map-shift-select] recipe off={{[0-9]+}} radices={{[0-9,]+}}
// CHECK-LABEL: hw.module @shaff
// CHECK-NOT: comb.mux
// CHECK-NOT: comb.add
// CHECK-NOT: comb.icmp
hw.module @shaff(in %top : i8, in %x : i8, out y : i16) {
  %cm = hw.constant 1 : i4
  %lo = comb.extract %top from 0 : (i8) -> i4
  %amt = comb.add %lo, %cm : i4
  %k0 = hw.constant 0 : i4
  %k1 = hw.constant 1 : i4
  %k2 = hw.constant 2 : i4
  %k3 = hw.constant 3 : i4
  %k4 = hw.constant 4 : i4
  %k5 = hw.constant 5 : i4
  %k6 = hw.constant 6 : i4
  %k7 = hw.constant 7 : i4
  %s0 = comb.icmp ceq %amt, %k0 : i4
  %s1 = comb.icmp ceq %amt, %k1 : i4
  %s2 = comb.icmp ceq %amt, %k2 : i4
  %s3 = comb.icmp ceq %amt, %k3 : i4
  %s4 = comb.icmp ceq %amt, %k4 : i4
  %s5 = comb.icmp ceq %amt, %k5 : i4
  %s6 = comb.icmp ceq %amt, %k6 : i4
  %s7 = comb.icmp ceq %amt, %k7 : i4
  %zh0 = hw.constant 0 : i8
  %w0 = comb.concat %zh0, %x : i8, i8
  %zh1 = hw.constant 0 : i7
  %zl1 = hw.constant 0 : i1
  %w1 = comb.concat %zh1, %x, %zl1 : i7, i8, i1
  %zh2 = hw.constant 0 : i6
  %zl2 = hw.constant 0 : i2
  %w2 = comb.concat %zh2, %x, %zl2 : i6, i8, i2
  %zh3 = hw.constant 0 : i5
  %zl3 = hw.constant 0 : i3
  %w3 = comb.concat %zh3, %x, %zl3 : i5, i8, i3
  %zh4 = hw.constant 0 : i4
  %zl4 = hw.constant 0 : i4
  %w4 = comb.concat %zh4, %x, %zl4 : i4, i8, i4
  %zh5 = hw.constant 0 : i3
  %zl5 = hw.constant 0 : i5
  %w5 = comb.concat %zh5, %x, %zl5 : i3, i8, i5
  %zh6 = hw.constant 0 : i2
  %zl6 = hw.constant 0 : i6
  %w6 = comb.concat %zh6, %x, %zl6 : i2, i8, i6
  %zh7 = hw.constant 0 : i1
  %zl7 = hw.constant 0 : i7
  %w7 = comb.concat %zh7, %x, %zl7 : i1, i8, i7
  %zl8 = hw.constant 0 : i8
  %w8 = comb.concat %x, %zl8 : i8, i8
  %c7 = comb.mux %s7, %w7, %w8 : i16
  %c6 = comb.mux %s6, %w6, %c7 : i16
  %c5 = comb.mux %s5, %w5, %c6 : i16
  %c4 = comb.mux %s4, %w4, %c5 : i16
  %c3 = comb.mux %s3, %w3, %c4 : i16
  %c2 = comb.mux %s2, %w2, %c3 : i16
  %c1 = comb.mux %s1, %w1, %c2 : i16
  %c0 = comb.mux %s0, %w0, %c1 : i16
  hw.output %c0 : i16
}
