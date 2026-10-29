// RUN: circt-abc-opt --dp-annotate=json=%t.json %s -o /dev/null && cat %t.json | FileCheck %s
// A wide add inside @child whose operands come from and go to mapped flops
// in @parent through ports: the launch/capture anchors resolve UP through
// the instance ("^u_c/") to the parent's flop instances; a flop inside the
// child resolves locally; a top-level port stays a port. (JSON keys sorted.)
// CHECK-DAG: "capture":["inst:^u_c/s_q.D","inst:local_q.D"]
// CHECK-DAG: "id":"child_dp0"
// CHECK-DAG: "launch":["inst:^u_c/a_q.Q","port:^u_c/x"]
// CHECK-DAG: "op":"add"
// CHECK-DAG: "width":16
hw.module.extern @DFF(in %D : i16, in %CK : !seq.clock, out Q : i16)
hw.module @child(in %clk : !seq.clock, in %a : i16, in %b : i16, out s : i16) {
  %s = comb.add %a, %b : i16
  %lq = hw.instance "local_q" @DFF(D: %s: i16, CK: %clk: !seq.clock) -> (Q: i16)
  hw.output %s : i16
}
hw.module @parent(in %clk : !seq.clock, in %x : i16, out o : i16) {
  %aq = hw.instance "a_q" @DFF(D: %x: i16, CK: %clk: !seq.clock) -> (Q: i16)
  %s = hw.instance "u_c" @child(clk: %clk: !seq.clock, a: %aq: i16, b: %x: i16) -> (s: i16)
  %sq = hw.instance "s_q" @DFF(D: %s: i16, CK: %clk: !seq.clock) -> (Q: i16)
  hw.output %sq : i16
}

// Effective width: the widest operand counted without its constant bits
// (both operands zero-extended here; one full-width operand would keep
// the chain full-width) -- the chooser's depth model must not credit
// constant bits.
// CHECK-DAG: "eff_width":8
// CHECK-DAG: "id":"zext_dp0"
// CHECK-DAG: "width":16
hw.module @zext(in %clk : !seq.clock, in %a : i8, in %b : i8, out s : i16) {
  %z = hw.constant 0 : i8
  %ea = comb.concat %z, %a : i8, i8
  %eb = comb.concat %z, %b : i8, i8
  %0 = comb.add %ea, %eb : i16
  %q = hw.instance "s_q" @DFF(D: %0: i16, CK: %clk: !seq.clock) -> (Q: i16)
  hw.output %q : i16
}
