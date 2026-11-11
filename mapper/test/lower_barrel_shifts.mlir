// RUN: circt-abc-opt --lower-barrel-shifts=enable=true %s | FileCheck %s
// RUN: circt-abc-opt --lower-barrel-shifts %s | FileCheck %s --check-prefix=OFF

// A 32-bit shift by a 5-bit amount: five mux stages, no out-of-range term
// (every 5-bit amount is below 32 and a stage-wise shift by >= 32 clears the word).
// CHECK-LABEL: hw.module @shl32
// CHECK-NOT: comb.shl
// CHECK-COUNT-5: comb.mux
// CHECK-NOT: comb.mux
// OFF-LABEL: hw.module @shl32
// OFF: comb.shl
hw.module @shl32(in %x : i32, in %n : i5, out y : i32) {
  %c = hw.constant 0 : i27
  %amt = comb.concat %c, %n : i27, i5
  %0 = comb.shl %x, %amt : i32
  hw.output %0 : i32
}

// A full-width amount adds the out-of-range mux (ceil(log2 16) = 4 stages).
// CHECK-LABEL: hw.module @shru16
// CHECK-NOT: comb.shru
// CHECK-COUNT-4: comb.mux
// CHECK: comb.icmp ne
// CHECK: comb.mux
hw.module @shru16(in %x : i16, in %n : i16, out y : i16) {
  %0 = comb.shru %x, %n : i16
  hw.output %0 : i16
}

// Arithmetic right shift fills with the sign bit.
// CHECK-LABEL: hw.module @shrs
// CHECK: comb.replicate
// CHECK-NOT: comb.shrs
hw.module @shrs(in %x : i16, in %n : i16, out y : i16) {
  %0 = comb.shrs %x, %n : i16
  hw.output %0 : i16
}

// Constant amounts and narrow words are left to the canonicalizer.
// CHECK-LABEL: hw.module @untouched
// CHECK: comb.shl
// CHECK: comb.shru
hw.module @untouched(in %x : i16, in %n : i1, in %s : i4, out y : i16, out z : i4) {
  %c = hw.constant 3 : i16
  %0 = comb.shl %x, %c : i16
  %1 = comb.shru %s, %s : i4
  hw.output %0, %1 : i16, i4
}
