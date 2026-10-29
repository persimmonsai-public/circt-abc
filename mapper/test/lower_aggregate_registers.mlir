// RUN: circt-abc-opt --lower-aggregate-registers --canonicalize %s | FileCheck %s
// Both aggregate registers become i8 firregs whose async reset value is
// the packed hw.constant (struct {5, -7} -> 0xB9 = -71; array [1, 2] ->
// 0x21... per hw::aggregateAttrToAPInt). Proven 2026-09-03: with the pass
// the register-legalize output is equivalent to the aggregate original
// under a free reset (Yosys miter, depth 6); without it both async resets
// were folded into D-logic and the miter fails.
// CHECK: seq.firreg {{.*}} reset async {{.*}}, %c-71_i8 : i8
// CHECK: seq.firreg {{.*}} reset async {{.*}}, %c18_i8 : i8

hw.module @aggreg(in %clk : !seq.clock, in %rst : i1, in %a : i3, in %b : i5, in %e : i1, out o : i8, out p : i8) {
  %rv = hw.aggregate_constant [5 : i3, -7 : i5] : !hw.struct<x: i3, y: i5>
  %in = hw.struct_create (%a, %b) : !hw.struct<x: i3, y: i5>
  %q = seq.firreg %next clock %clk reset async %rst, %rv : !hw.struct<x: i3, y: i5>
  %next = comb.mux %e, %in, %q : !hw.struct<x: i3, y: i5>
  %o = hw.bitcast %q : (!hw.struct<x: i3, y: i5>) -> i8
  %arv = hw.aggregate_constant [1 : i4, 2 : i4] : !hw.array<2xi4>
  %ai = hw.array_create %b_lo, %a_ext : i4
  %b_lo = comb.extract %b from 0 : (i5) -> i4
  %a_ext = comb.concat %e, %a : i1, i3
  %aq = seq.firreg %anext clock %clk reset async %rst, %arv : !hw.array<2xi4>
  %anext = comb.mux %e, %ai, %aq : !hw.array<2xi4>
  %p = hw.bitcast %aq : (!hw.array<2xi4>) -> i8
  hw.output %o, %p : i8, i8
}
