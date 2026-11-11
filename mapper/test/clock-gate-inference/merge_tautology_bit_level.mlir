// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true" %s 2>&1 | FileCheck %s --check-prefix=DEF
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true merge-equivalent-enables=true" %s 2>&1 | FileCheck %s --check-prefix=MERGE
//
// The bit-level path builds a register's enable from the DNF of its mux
// tree. `q` loads one of two values on every cycle (no leg holds), but its
// enable comes out as `c | !c`, two cubes the DNF's absorption cannot
// combine, so by default it gets a gate that is always on. `h` holds when
// !c and loads when c: a real enable. With merge-equivalent-enables the
// tautology is recognized and `q` stays on the plain clock.
// DEF: bit-level split 2 register(s), 16 bit(s) gated
// DEF: gated 2 firreg(s) into 2 shared clock-gate cell(s)
// MERGE: bit-level split 1 register(s), 8 bit(s) gated
// MERGE: gated 1 firreg(s) into 1 shared clock-gate cell(s)
hw.module @top(in %clk_i : i1, in %c : i1, in %d : i8, in %e : i8,
               out q : i8, out h : i8) {
  %clk = seq.to_clock %clk_i
  %q = seq.firreg %nq clock %clk {name = "q"} : i8
  %nq = comb.mux %c, %d, %e : i8
  %h = seq.firreg %nh clock %clk {name = "h"} : i8
  %nh = comb.mux %c, %d, %h : i8
  hw.output %q, %h : i8, i8
}
