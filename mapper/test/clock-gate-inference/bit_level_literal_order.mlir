// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true min-group-width=1" %s 2>&1 | FileCheck %s
//
// The literals of a bit-level enable cube are sorted, and that order becomes
// the operand order of the comb.and built for the enable. It must be IR
// order (block arguments by number, ops by position), not Value pointer
// order: a pointer order is a heap-address order that changes from process
// to process, which made this pass -- and the flow's stage-3
// `*_struct.mlir` and final netlist -- differ run to run on identical input.
//
// The ports are declared c, b, a, e while the mux nest tests e, then a,
// then b (then c for bit 1), so neither nesting order nor port-name order
// coincides with the expected one. Bit 0 loads under !e & a & b, bit 1
// under !e & c. Literals sort by their underlying Value (the port), so IR
// order is b, a, e -- the reverse of the mux nesting -- and the negated e,
// materialized as a comb.xor, comes last.
//
// CHECK: bit-level split 1 register(s), 2 bit(s) gated
// CHECK: %[[NE:.+]] = comb.xor %e, %true : i1
// CHECK: %[[EN0:.+]] = comb.and %b, %a, %[[NE]] : i1
// CHECK: %[[EN1:.+]] = comb.and %c, %[[NE]] : i1
// CHECK-DAG: hw.instance "clock_gate_0" @TICG({{.*}}E: %[[EN0]]: i1
// CHECK-DAG: hw.instance "clock_gate_1" @TICG({{.*}}E: %[[EN1]]: i1

hw.module @lit_order(in %clk : !seq.clock, in %c : i1, in %b : i1, in %a : i1,
                     in %e : i1, in %d : i2, out o : i2) {
  %q1 = comb.extract %q from 1 : (i2) -> i1
  %d0 = comb.extract %d from 0 : (i2) -> i1
  %s0 = comb.concat %q1, %d0 : i1, i1
  %l0 = comb.mux %b, %s0, %q : i2
  %l1 = comb.mux %a, %l0, %q : i2
  %l1_0 = comb.extract %l1 from 0 : (i2) -> i1
  %d1 = comb.extract %d from 1 : (i2) -> i1
  %s1 = comb.concat %d1, %l1_0 : i1, i1
  %l2 = comb.mux %c, %s1, %l1 : i2
  %next = comb.mux %e, %q, %l2 : i2
  %q = seq.firreg %next clock %clk : i2
  hw.output %q : i2
}
