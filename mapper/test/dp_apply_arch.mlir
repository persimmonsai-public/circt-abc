// RUN: echo '{"m_dp1": "RIPPLE-CARRY"}' > %t.json && circt-abc-opt --set-adder-arch=arch=BRENT-KUNG --dp-apply-arch=json=%t.json %s | FileCheck %s
// The override keyed by dp-annotate's id (module + walk order) replaces the
// global stamp on exactly that op; the other keeps the default.
// CHECK: comb.add %a, %b {synth.test.arch = "BRENT-KUNG"} : i32
// CHECK: comb.icmp ult %a, %b {synth.test.arch = "RIPPLE-CARRY"} : i32
hw.module @m(in %a : i32, in %b : i32, out s : i32, out lt : i1) {
  %s = comb.add %a, %b : i32
  %lt = comb.icmp ult %a, %b : i32
  hw.output %s, %lt : i32, i1
}
