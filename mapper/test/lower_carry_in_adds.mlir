// RUN: circt-abc-opt --lower-carry-in-adds=enable=true %s | FileCheck %s
// RUN: circt-abc-opt --lower-carry-in-adds %s | FileCheck %s --check-prefix=OFF

// a + b + zext(c) becomes one 17-bit add of {a,1} + {b,c}; the result is the
// upper 16 bits, and the architecture request moves to the new add.
// CHECK-LABEL: hw.module @cin_var
// CHECK-NOT: comb.add {{.*}} : i16
// CHECK: comb.concat %a, %true
// CHECK: comb.concat %b, %c
// CHECK: comb.add {{.*}} {synth.test.arch = "KOGGE-STONE"} : i17
// CHECK: comb.extract {{.*}} from 1 : (i17) -> i16
// OFF-LABEL: hw.module @cin_var
// OFF: comb.add {{.*}} : i16
hw.module @cin_var(in %a : i16, in %b : i16, in %c : i1, out y : i16) {
  %z = hw.constant 0 : i15
  %cz = comb.concat %z, %c : i15, i1
  %0 = comb.add %a, %b, %cz {synth.test.arch = "KOGGE-STONE"} : i16
  hw.output %0 : i16
}

// The carry-in may be any operand position.
// CHECK-LABEL: hw.module @cin_first
// CHECK: comb.add {{.*}} : i17
hw.module @cin_first(in %a : i16, in %b : i16, in %c : i1, out y : i16) {
  %z = hw.constant 0 : i15
  %cz = comb.concat %z, %c : i15, i1
  %0 = comb.add %cz, %a, %b : i16
  hw.output %0 : i16
}

// Below min-width, with a constant carry-in, or with a third operand that is
// not a one-bit extension, the add is left alone.
// CHECK-LABEL: hw.module @untouched
// CHECK: comb.add {{.*}} : i4
// CHECK: comb.add {{.*}} : i16
// CHECK: comb.add {{.*}} : i16
hw.module @untouched(in %a : i16, in %b : i16, in %c : i1, in %d : i16, in %s : i4, in %t : i4,
                     out y : i4, out w : i16, out v : i16) {
  %z3 = hw.constant 0 : i3
  %c4 = comb.concat %z3, %c : i3, i1
  %0 = comb.add %s, %t, %c4 : i4
  %z = hw.constant 0 : i15
  %one = hw.constant 1 : i1
  %o16 = comb.concat %z, %one : i15, i1
  %1 = comb.add %a, %b, %o16 : i16
  %2 = comb.add %a, %b, %d : i16
  hw.output %0, %1, %2 : i4, i16, i16
}
