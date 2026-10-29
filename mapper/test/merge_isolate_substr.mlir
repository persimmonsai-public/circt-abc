// RUN: circt-abc-opt --merge-residual-modules="group-count=2 group-index=0 isolate-substr=_dpop" --merge-residual-modules="group-count=2 group-index=1 isolate-substr=_dpop" %s | FileCheck %s
// The K instances run chained (each sees what earlier ones left). With
// isolate-substr, the LAST group merges exactly the matching leaf
// residuals (the outlined datapath op) and the other groups split the rest.
// CHECK-DAG: hw.module private @abc_bridge_merged_residual_g0(in %m_residual_0_in0 : i8, in %m_residual_0_in1 : i8, out m_residual_0_out0 : i8)
// CHECK-DAG: hw.module private @abc_bridge_merged_residual_g1(in %m_dpop0_add_residual_0_in0 : i8, in %m_dpop0_add_residual_0_in1 : i8, out m_dpop0_add_residual_0_out0 : i8)
hw.module private @m_residual(in %in0 : i8, in %in1 : i8, out out0 : i8) {
  %0 = comb.xor %in0, %in1 : i8
  hw.output %0 : i8
}
hw.module private @m_dpop0_add_residual(in %in0 : i8, in %in1 : i8, out out0 : i8) {
  %0 = comb.add %in0, %in1 : i8
  hw.output %0 : i8
}
hw.module @top(in %a : i8, in %b : i8, out x : i8, out y : i8) {
  %x = hw.instance "u_residual" @m_residual(in0: %a: i8, in1: %b: i8) -> (out0: i8)
  %y = hw.instance "dp0" @m_dpop0_add_residual(in0: %a: i8, in1: %b: i8) -> (out0: i8)
  hw.output %x, %y : i8, i8
}
