// RUN: circt-abc-opt --convert-comb-to-datapath %s | FileCheck %s
// The adder-architecture request (synth.test.arch) must survive the
// lowerings that replace an op by a fresh carry-propagate adder: a sub
// becomes add(lhs, ~rhs, 1), a variadic add becomes add(compress(...)),
// a mul becomes add(partial products). Before this, only 2-operand adds
// and compares honoured the request (cv64a6: 25 of 37 restructured ops
// were byte-identical between Kogge-Stone and Brent-Kung runs).

// CHECK-LABEL: hw.module @sub_keeps_arch
hw.module @sub_keeps_arch(in %a : i16, in %b : i16, out y : i16) {
  // CHECK: comb.add {{.*}} {synth.test.arch = "BRENT-KUNG"} : i16
  %0 = comb.sub %a, %b {synth.test.arch = "BRENT-KUNG"} : i16
  hw.output %0 : i16
}

// CHECK-LABEL: hw.module @add3_keeps_arch
hw.module @add3_keeps_arch(in %a : i16, in %b : i16, in %c : i16, out y : i16) {
  // CHECK: datapath.compress
  // CHECK: comb.add {{.*}} {synth.test.arch = "SKLANSKEY"} : i16
  %0 = comb.add %a, %b, %c {synth.test.arch = "SKLANSKEY"} : i16
  hw.output %0 : i16
}

// CHECK-LABEL: hw.module @mul_keeps_arch
hw.module @mul_keeps_arch(in %a : i8, in %b : i8, out y : i8) {
  // CHECK: datapath.partial_product
  // CHECK: comb.add {{.*}} {synth.test.arch = "KOGGE-STONE"} : i8
  %0 = comb.mul %a, %b {synth.test.arch = "KOGGE-STONE"} : i8
  hw.output %0 : i8
}

// CHECK-LABEL: hw.module @no_attr_stays_clean
hw.module @no_attr_stays_clean(in %a : i16, in %b : i16, out y : i16) {
  // CHECK: comb.add
  // CHECK-NOT: synth.test.arch
  %0 = comb.sub %a, %b : i16
  hw.output %0 : i16
}
