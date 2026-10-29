// RUN: circt-abc-opt --narrow-constant-register-bits %s | FileCheck %s
//
// A CSR-shaped leaf: a 8-bit register held through its own read-back and
// written from a port. The single caller drives bits 7:4 of that port with
// a literal 0b1010 (matching the reset value 0xA0's upper nibble) and bits
// 3:0 with a live value, so bits 7:4 are provably constant across the
// instance boundary and the register narrows to 4 bits.

// CHECK-LABEL: hw.module private @leaf
hw.module private @leaf(in %clk : !seq.clock, in %rst : i1, in %wdata : i8, in %we : i1, out rdata : i8) {
  %c160_i8 = hw.constant 160 : i8
  %q = seq.firreg %next clock %clk reset async %rst, %c160_i8 : i8
  %next = comb.mux %we, %wdata, %q : i8
  hw.output %q : i8
}
// CHECK: %[[Q:.+]] = seq.firreg {{.*}} : i4
// The read-back path is still rebuilt at the ORIGINAL width (the four
// constant bits concatenated back onto the four live ones), so `: i8`
// legitimately survives in @leaf until canonicalize folds it -- what must
// not survive is a register at that width.
// CHECK-NOT: seq.firreg {{.*}} : i8

// CHECK-LABEL: hw.module @top
hw.module @top(in %clk : !seq.clock, in %rst : i1, in %live : i4, in %we : i1, out rdata : i8) {
  %c10_i4 = hw.constant 10 : i4
  %wdata = comb.concat %c10_i4, %live : i4, i4
  %u.rdata = hw.instance "u" @leaf(clk: %clk: !seq.clock, rst: %rst: i1, wdata: %wdata: i8, we: %we: i1) -> (rdata: i8)
  hw.output %u.rdata : i8
}

// A second leaf instantiated TWICE with different constants must stay
// untouched (no unique instance -> the port is opaque).
// CHECK-LABEL: hw.module private @leaf2
hw.module private @leaf2(in %clk : !seq.clock, in %rst : i1, in %wdata : i8, in %we : i1, out rdata : i8) {
  %c0_i8 = hw.constant 0 : i8
  %q = seq.firreg %next clock %clk reset async %rst, %c0_i8 : i8
  %next = comb.mux %we, %wdata, %q : i8
  hw.output %q : i8
}
// CHECK: seq.firreg {{.*}} : i8

hw.module @top2(in %clk : !seq.clock, in %rst : i1, in %live : i4, in %we : i1, out a : i8, out b : i8) {
  %c0_i4 = hw.constant 0 : i4
  %c5_i4 = hw.constant 5 : i4
  %wa = comb.concat %c0_i4, %live : i4, i4
  %wb = comb.concat %c5_i4, %live : i4, i4
  %ua.rdata = hw.instance "ua" @leaf2(clk: %clk: !seq.clock, rst: %rst: i1, wdata: %wa: i8, we: %we: i1) -> (rdata: i8)
  %ub.rdata = hw.instance "ub" @leaf2(clk: %clk: !seq.clock, rst: %rst: i1, wdata: %wb: i8, we: %we: i1) -> (rdata: i8)
  hw.output %ua.rdata, %ub.rdata : i8, i8
}
