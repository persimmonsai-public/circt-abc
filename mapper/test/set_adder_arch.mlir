// RUN: circt-abc-opt --set-adder-arch=arch=RIPPLE-CARRY %s | FileCheck %s
// RUN: circt-abc-opt --set-adder-arch %s | FileCheck %s --check-prefix=OFF
// 32-bit add and compare get the stamp; the 4-bit add is under min-width.
// CHECK: comb.add %a, %b {synth.test.arch = "RIPPLE-CARRY"} : i32
// CHECK: comb.icmp ult %a, %b {synth.test.arch = "RIPPLE-CARRY"} : i32
// CHECK: comb.add %c, %d : i4
// OFF-NOT: synth.test.arch
hw.module @adders(in %a : i32, in %b : i32, in %c : i4, in %d : i4, out s : i32, out lt : i1, out t : i4) {
  %s = comb.add %a, %b : i32
  %lt = comb.icmp ult %a, %b : i32
  %t = comb.add %c, %d : i4
  hw.output %s, %lt, %t : i32, i1, i4
}
