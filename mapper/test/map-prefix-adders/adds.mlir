// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=SKLANSKEY" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=KOGGE-STONE" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=HAN-CARLSON" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=BRENT-KUNG" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-prefix-adders=liberty=%S/prefix.lib" %s 2>&1 | FileCheck %s --check-prefix=OFF
//
// Wide adds become prefix networks of cells (inverting compound cells, via
// the emitter's polarity tracking); a narrow add and a three-operand add
// whose third operand is not a carry-in are left alone. Equivalence of every
// module is SAT-proven in scripts/circt_flow/tests/test_arith_lowering.py.

// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=60 arch=SKLANSKEY" %s 2>&1 | FileCheck %s --check-prefix=NONE
// NONE: [map-prefix-adders] 0 add(s) built as SKLANSKEY prefix networks, 0 cell(s); 0 left to CombToSynth
// CHECK: [map-prefix-adders] 4 add(s) built as {{.*}} prefix networks, {{[0-9]+}} cell(s); 1 left to CombToSynth
// OFF-NOT: map-prefix-adders
// CHECK-LABEL: hw.module @add16
// CHECK-NOT: comb.add
// CHECK: hw.instance
hw.module @add16(in %a : i16, in %b : i16, out y : i16) {
  %0 = comb.add %a, %b : i16
  hw.output %0 : i16
}

// CHECK-LABEL: hw.module @add32_cin
// CHECK-NOT: comb.add
hw.module @add32_cin(in %a : i32, in %b : i32, in %c : i1, out y : i32) {
  %z = hw.constant 0 : i31
  %cz = comb.concat %z, %c : i31, i1
  %0 = comb.add %a, %b, %cz : i32
  hw.output %0 : i32
}

// CHECK-LABEL: hw.module @add40_one
// CHECK-NOT: comb.add
hw.module @add40_one(in %a : i40, in %b : i40, out y : i40) {
  %one = hw.constant 1 : i40
  %0 = comb.add %a, %b, %one : i40
  hw.output %0 : i40
}

// CHECK-LABEL: hw.module @add8_const
// CHECK-NOT: comb.add
hw.module @add8_const(in %a : i8, out y : i8) {
  %k = hw.constant 5 : i8
  %0 = comb.add %a, %k : i8
  hw.output %0 : i8
}

// Left alone: below min-width, and a third operand that is no carry-in.
// CHECK-LABEL: hw.module @untouched
// CHECK: comb.add {{.*}} : i4
// CHECK: comb.add {{.*}} : i16
hw.module @untouched(in %a : i16, in %b : i16, in %d : i16, in %s : i4, in %t : i4,
                     out y : i4, out w : i16) {
  %0 = comb.add %s, %t : i4
  %1 = comb.add %a, %b, %d : i16
  hw.output %0, %1 : i4, i16
}
