// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true liberty=%S/shift.lib min-arms=8 min-width=8" --canonicalize %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true liberty=%S/shift4.lib min-arms=8 min-width=8 wide4-cells=true" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=WIDE4
// RUN: circt-abc-opt "--map-shift-select=liberty=%S/shift.lib" %s 2>&1 | FileCheck %s --check-prefix=OFF
//
// A decoded one-hot select over nine shifts of one byte becomes a radix-4
// AND-OR network of cells; the original OR of muxes is gone. Equivalence is
// proven by scripts/circt_flow/tests/test_shift_select.py (yosys SAT miter).

// CHECK: [map-shift-select] 1 one-hot select(s) built as radix-4 AND-OR networks, {{[0-9]+}} cell(s); 0 declined
// OFF-NOT: map-shift-select
// WIDE4: [map-shift-select] 1 one-hot select(s) built as radix-4 AND-OR networks, {{[0-9]+}} cell(s); 0 declined
// WIDE4: TAOI2222
// CHECK-LABEL: hw.module @shsel
// CHECK-NOT: comb.mux
// CHECK: hw.instance
hw.module @shsel(in %x : i8, in %amt : i4, out y : i16) {
  %z8 = hw.constant 0 : i8
  %c0 = hw.constant 0 : i16
  %k0 = hw.constant 0 : i4
  %s0 = comb.icmp ceq %amt, %k0 : i4
  %zh0 = hw.constant 0 : i8
  %w0 = comb.concat %zh0, %x : i8, i8
  %m0 = comb.mux bin %s0, %w0, %c0 : i16
  %k1 = hw.constant 1 : i4
  %s1 = comb.icmp ceq %amt, %k1 : i4
  %zh1 = hw.constant 0 : i7
  %zl1 = hw.constant 0 : i1
  %w1 = comb.concat %zh1, %x, %zl1 : i7, i8, i1
  %m1 = comb.mux bin %s1, %w1, %c0 : i16
  %k2 = hw.constant 2 : i4
  %s2 = comb.icmp ceq %amt, %k2 : i4
  %zh2 = hw.constant 0 : i6
  %zl2 = hw.constant 0 : i2
  %w2 = comb.concat %zh2, %x, %zl2 : i6, i8, i2
  %m2 = comb.mux bin %s2, %w2, %c0 : i16
  %k3 = hw.constant 3 : i4
  %s3 = comb.icmp ceq %amt, %k3 : i4
  %zh3 = hw.constant 0 : i5
  %zl3 = hw.constant 0 : i3
  %w3 = comb.concat %zh3, %x, %zl3 : i5, i8, i3
  %m3 = comb.mux bin %s3, %w3, %c0 : i16
  %k4 = hw.constant 4 : i4
  %s4 = comb.icmp ceq %amt, %k4 : i4
  %zh4 = hw.constant 0 : i4
  %zl4 = hw.constant 0 : i4
  %w4 = comb.concat %zh4, %x, %zl4 : i4, i8, i4
  %m4 = comb.mux bin %s4, %w4, %c0 : i16
  %k5 = hw.constant 5 : i4
  %s5 = comb.icmp ceq %amt, %k5 : i4
  %zh5 = hw.constant 0 : i3
  %zl5 = hw.constant 0 : i5
  %w5 = comb.concat %zh5, %x, %zl5 : i3, i8, i5
  %m5 = comb.mux bin %s5, %w5, %c0 : i16
  %k6 = hw.constant 6 : i4
  %s6 = comb.icmp ceq %amt, %k6 : i4
  %zh6 = hw.constant 0 : i2
  %zl6 = hw.constant 0 : i6
  %w6 = comb.concat %zh6, %x, %zl6 : i2, i8, i6
  %m6 = comb.mux bin %s6, %w6, %c0 : i16
  %k7 = hw.constant 7 : i4
  %s7 = comb.icmp ceq %amt, %k7 : i4
  %zh7 = hw.constant 0 : i1
  %zl7 = hw.constant 0 : i7
  %w7 = comb.concat %zh7, %x, %zl7 : i1, i8, i7
  %m7 = comb.mux bin %s7, %w7, %c0 : i16
  %k8 = hw.constant 8 : i4
  %s8 = comb.icmp ceq %amt, %k8 : i4
  %zl8 = hw.constant 0 : i8
  %w8 = comb.concat %x, %zl8 : i8, i8
  %m8 = comb.mux bin %s8, %w8, %c0 : i16
  %y = comb.or bin %m0, %m1, %m2, %m3, %m4, %m5, %m6, %m7, %m8 : i16
  hw.output %y : i16
}
