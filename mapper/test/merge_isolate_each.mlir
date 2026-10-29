// RUN: circt-abc-opt --merge-residual-modules="group-count=3 group-index=0 isolate-substr=_dpop isolate-each=true" --merge-residual-modules="group-count=3 group-index=1 isolate-substr=_dpop isolate-each=true" --merge-residual-modules="group-count=3 group-index=2 isolate-substr=_dpop isolate-each=true" %s | FileCheck %s
// isolate-each: the two outlined ops each get their own trailing group (g1,
// g2 in name order); g0 holds everything else.
// CHECK-DAG: hw.module private @abc_bridge_merged_residual_g0(in %m_residual_0_in0 : i8, in %m_residual_0_in1 : i8, out m_residual_0_out0 : i8)
// CHECK-DAG: hw.module private @abc_bridge_merged_residual_g1(in %m_dpop0_add_residual_0_in0 : i8, in %m_dpop0_add_residual_0_in1 : i8, out m_dpop0_add_residual_0_out0 : i8)
// CHECK-DAG: hw.module private @abc_bridge_merged_residual_g2(in %m_dpop1_cmp_residual_0_in0 : i8, in %m_dpop1_cmp_residual_0_in1 : i8, out m_dpop1_cmp_residual_0_out0 : i1)
hw.module private @m_residual(in %in0 : i8, in %in1 : i8, out out0 : i8) {
  %0 = comb.xor %in0, %in1 : i8
  hw.output %0 : i8
}
hw.module private @m_dpop0_add_residual(in %in0 : i8, in %in1 : i8, out out0 : i8) {
  %0 = comb.add %in0, %in1 : i8
  hw.output %0 : i8
}
hw.module private @m_dpop1_cmp_residual(in %in0 : i8, in %in1 : i8, out out0 : i1) {
  %0 = comb.icmp ult %in0, %in1 : i8
  hw.output %0 : i1
}
hw.module @top(in %a : i8, in %b : i8, out x : i8, out y : i8, out z : i1) {
  %x = hw.instance "u_residual" @m_residual(in0: %a: i8, in1: %b: i8) -> (out0: i8)
  %y = hw.instance "dp0" @m_dpop0_add_residual(in0: %a: i8, in1: %b: i8) -> (out0: i8)
  %z = hw.instance "dp1" @m_dpop1_cmp_residual(in0: %a: i8, in1: %b: i8) -> (out0: i1)
  hw.output %x, %y, %z : i8, i8, i1
}
// RUN: echo '{"m_residual": 1}' > %t.json && circt-abc-opt --merge-residual-modules="group-count=4 group-index=0 isolate-substr=_dpop isolate-each=true group-map=%t.json" --merge-residual-modules="group-count=4 group-index=1 isolate-substr=_dpop isolate-each=true group-map=%t.json" --merge-residual-modules="group-count=4 group-index=2 isolate-substr=_dpop isolate-each=true group-map=%t.json" --merge-residual-modules="group-count=4 group-index=3 isolate-substr=_dpop isolate-each=true group-map=%t.json" %s | FileCheck %s --check-prefix=MAP
// group-map pins m_residual to g1 (g0 stays empty); the ops take the
// trailing groups g2/g3 regardless.
// MAP-DAG: hw.module private @abc_bridge_merged_residual_g1(in %m_residual_0_in0
// MAP-DAG: hw.module private @abc_bridge_merged_residual_g2(in %m_dpop0_add_residual_0_in0
// MAP-DAG: hw.module private @abc_bridge_merged_residual_g3(in %m_dpop1_cmp_residual_0_in0
// MAP-NOT: @abc_bridge_merged_residual_g0(
