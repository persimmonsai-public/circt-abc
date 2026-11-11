// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib" %s 2>&1 | FileCheck %s --check-prefix=DEF
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib merge-equivalent-enables=true" %s 2>&1 | FileCheck %s --check-prefix=MERGE
//
// Four registers whose enables are all the function a & b, spelled four ways
// (and, and with the operands swapped is the one spelling the default
// grouping already shares; De Morgan, and a mux), a fifth whose enable
// a | !a never holds, and a sixth on c.
//
// Default: grouping is by operand multiset, so the De Morgan and mux
// spellings each get their own gate, and the tautology gets a gate that is
// always on.
// DEF: gated 6 firreg(s) into 5 shared clock-gate cell(s)
//
// merge-equivalent-enables: one gate for a & b (all four registers, two more than
// the default's two), none for the tautology (its register keeps its plain
// next), one for c.
// MERGE: 2 group(s) merged by enable equivalence, 1 always-on enable(s) left ungated
// MERGE: gated 5 firreg(s) into 2 shared clock-gate cell(s)
hw.module @top(in %clk_i : i1, in %a : i1, in %b : i1, in %c : i1,
               in %d : i8, out q1 : i8, out q2 : i8, out q3 : i8, out q4 : i8,
               out q5 : i8, out q6 : i8) {
  %clk = seq.to_clock %clk_i
  %true = hw.constant true
  %ab = comb.and %a, %b : i1
  %ba = comb.and %b, %a : i1
  %na = comb.xor %a, %true : i1
  %nb = comb.xor %b, %true : i1
  %nor_n = comb.or %na, %nb : i1
  %demorgan = comb.xor %nor_n, %true : i1
  %zero = hw.constant false
  %viamux = comb.mux %a, %b, %zero : i1
  %taut = comb.or %a, %na : i1
  %r1 = seq.firreg %n1 clock %clk {name = "r1"} : i8
  %n1 = comb.mux %ab, %d, %r1 : i8
  %r2 = seq.firreg %n2 clock %clk {name = "r2"} : i8
  %n2 = comb.mux %ba, %d, %r2 : i8
  %r3 = seq.firreg %n3 clock %clk {name = "r3"} : i8
  %n3 = comb.mux %demorgan, %d, %r3 : i8
  %r4 = seq.firreg %n4 clock %clk {name = "r4"} : i8
  %n4 = comb.mux %viamux, %d, %r4 : i8
  %r5 = seq.firreg %n5 clock %clk {name = "r5"} : i8
  %n5 = comb.mux %taut, %d, %r5 : i8
  %r6 = seq.firreg %n6 clock %clk {name = "r6"} : i8
  %n6 = comb.mux %c, %d, %r6 : i8
  hw.output %r1, %r2, %r3, %r4, %r5, %r6 : i8, i8, i8, i8, i8, i8
}
