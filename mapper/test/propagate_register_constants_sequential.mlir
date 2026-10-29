// RUN: circt-abc-opt --propagate-register-constants='fold-unknown-ops=true mutual-fixpoint=true absorbing-operands=true' --canonicalize %s | FileCheck %s
// RUN: circt-abc-opt --propagate-register-constants --canonicalize %s | FileCheck %s --check-prefix=DEFAULT
//
// The three evaluator levers that collapse a tied-off block. Each is off by
// default, so the DEFAULT run is the control: every register below survives
// it. Derived from CVA6's hardware stride prefetcher, which `cva6.sv` ties
// off with a `/*FIXME*/` and boundary optimisation deletes outright, while we
// shipped 12 registers per instance and 3,366 netlist references to it.

// `mutual-fixpoint` plus `fold-unknown-ops`. Neither register is provably
// constant ALONE -- %a is zero only while %b is zero and vice versa -- so
// the least fixed point, which only ever confirms a register whose proof
// rests on registers already confirmed, cannot start. The greatest fixed
// point assumes both hold their reset value and finds nothing that breaks
// the assumption. `comb.icmp` needs the generic folder to evaluate at all.
// CHECK-LABEL: hw.module @mutual
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c0_i32, %c0_i32
// DEFAULT-LABEL: hw.module @mutual
// DEFAULT-COUNT-2: seq.firreg
hw.module @mutual(in %clk : !seq.clock, in %rst : i1, out a : i32, out b : i32) {
  %c0_i32 = hw.constant 0 : i32
  %c1_i32 = hw.constant 1 : i32
  %arm = comb.icmp ceq %b_q, %c1_i32 : i32
  %a_next = comb.mux %arm, %c1_i32, %c0_i32 : i32
  %a_q = seq.firreg %a_next clock %clk reset async %rst, %c0_i32 : i32
  %fire = comb.icmp ceq %a_q, %c1_i32 : i32
  %b_next = comb.mux %fire, %c1_i32, %c0_i32 : i32
  %b_q = seq.firreg %b_next clock %clk reset async %rst, %c0_i32 : i32
  hw.output %a_q, %b_q : i32, i32
}

// `absorbing-operands`. The write enable is `and(<enable bit of this very
// register>, <something unknowable>)`: under the hypothesis the first
// operand is zero, which forces the result to zero whatever the second
// operand does. Requiring every operand to resolve -- the behaviour before
// this option -- loses the whole register to an input it never needed.
// CHECK-LABEL: hw.module @absorbed
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c0_i32
// DEFAULT-LABEL: hw.module @absorbed
// DEFAULT:       seq.firreg
hw.module @absorbed(in %clk : !seq.clock, in %rst : i1, in %x : i32, out o : i32) {
  %c0_i32 = hw.constant 0 : i32
  %c5_i32 = hw.constant 5 : i32
  %c7_i32 = hw.constant 7 : i32
  %gate = comb.extract %q from 0 : (i32) -> i1
  %unknown = comb.icmp ceq %x, %c5_i32 : i32
  %en = comb.and %gate, %unknown : i1
  %next = comb.mux %en, %c7_i32, %q : i32
  %q = seq.firreg %next clock %clk reset async %rst, %c0_i32 : i32
  hw.output %q : i32
}

// The dual, on comb.or: an all-ones operand absorbs. The all-ones operand
// has to be the register's own hypothesis rather than a literal, or
// canonicalize folds the expression away before the pass ever sees it.
// CHECK-LABEL: hw.module @absorbed_or
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c-1_i8
// DEFAULT-LABEL: hw.module @absorbed_or
// DEFAULT:       seq.firreg
hw.module @absorbed_or(in %clk : !seq.clock, in %rst : i1, in %x : i8, out o : i8) {
  %cff_i8 = hw.constant -1 : i8
  %next = comb.or %q, %x : i8
  %q = seq.firreg %next clock %clk reset async %rst, %cff_i8 : i8
  hw.output %q : i8
}

// A register that genuinely is NOT constant must survive all three. The
// greatest fixed point is the dangerous direction -- it starts by assuming
// what it wants to prove -- so this is the case that matters most.
// CHECK-LABEL: hw.module @not_constant
// CHECK:         seq.firreg
// DEFAULT-LABEL: hw.module @not_constant
// DEFAULT:       seq.firreg
hw.module @not_constant(in %clk : !seq.clock, in %rst : i1, in %x : i32, out o : i32) {
  %c0_i32 = hw.constant 0 : i32
  %q = seq.firreg %x clock %clk reset async %rst, %c0_i32 : i32
  hw.output %q : i32
}

// A mutually-dependent pair where one member really does move: %d counts up
// from an input-driven condition, so the assumption breaks on %d, and %c --
// which is only zero while %d is -- must be dropped with it. This is the
// shrink step doing its job; a greatest fixed point that stopped after one
// sweep would keep %c.
// CHECK-LABEL: hw.module @mutual_broken
// CHECK-COUNT-2: seq.firreg
// DEFAULT-LABEL: hw.module @mutual_broken
// DEFAULT-COUNT-2: seq.firreg
hw.module @mutual_broken(in %clk : !seq.clock, in %rst : i1, in %x : i1, out c : i32, out d : i32) {
  %c0_i32 = hw.constant 0 : i32
  %c1_i32 = hw.constant 1 : i32
  %seen = comb.icmp ceq %d_q, %c1_i32 : i32
  %c_next = comb.mux %seen, %c1_i32, %c0_i32 : i32
  %c_q = seq.firreg %c_next clock %clk reset async %rst, %c0_i32 : i32
  %d_next = comb.mux %x, %c1_i32, %c0_i32 : i32
  %d_q = seq.firreg %d_next clock %clk reset async %rst, %c0_i32 : i32
  hw.output %c_q, %d_q : i32, i32
}
