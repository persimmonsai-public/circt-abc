// RUN: circt-abc-opt --register-mapper=liberty=%S/register_mapper_inverted.lib --canonicalize %s | FileCheck %s
// RUN: circt-abc-opt --clock-gate-inference=liberty=%S/register_mapper_inverted.lib %s | FileCheck %s --check-prefix=ICG
//
// A library whose flops all store and output the complement of D, and whose
// only async flop has both clear and preset (register_mapper_inverted.lib).
// D is driven with the complement of the next value, so QN is the register's
// value. The async flop's clear forces QN to 0, so a reset-to-0 register
// drives SETN with the active-low reset and ties RESETN high; a reset-to-1
// register is the other way round.

// CHECK-DAG: hw.module.extern @TDFFASRQN(in %CLK : i1, in %D : i1, in %SETN : i1, in %RESETN : i1, out QN : i1)
// CHECK-DAG: hw.module.extern @TDFFQN(in %CLK : i1, in %D : i1, out QN : i1)

// CHECK-LABEL: hw.module @noreset
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[ND:.+]] = comb.xor %{{.+}}, %[[T]] : i1
// CHECK: %[[Q:.+]] = hw.instance "r_bit0" @TDFFQN(CLK: %{{.+}}: i1, D: %[[ND]]: i1) -> (QN: i1)
// CHECK: hw.output %[[Q]]
hw.module @noreset(in %clk : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %r = seq.firreg %d clock %c : i1
  hw.output %r : i1
}

// CHECK-LABEL: hw.module @reset0
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[NR:.+]] = comb.xor %rst, %[[T]] : i1
// CHECK: %[[ND:.+]] = comb.xor %{{.+}}, %[[T]] : i1
// CHECK: hw.instance "r_bit0" @TDFFASRQN(CLK: %{{.+}}: i1, D: %[[ND]]: i1, SETN: %[[NR]]: i1, RESETN: %[[T]]: i1)
hw.module @reset0(in %clk : i1, in %rst : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %false = hw.constant false
  %r = seq.firreg %d clock %c reset async %rst, %false : i1
  hw.output %r : i1
}

// CHECK-LABEL: hw.module @reset1
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[NR:.+]] = comb.xor %rst, %[[T]] : i1
// CHECK: %[[ND:.+]] = comb.xor %{{.+}}, %[[T]] : i1
// CHECK: hw.instance "r_bit0" @TDFFASRQN(CLK: %{{.+}}: i1, D: %[[ND]]: i1, SETN: %[[T]]: i1, RESETN: %[[NR]]: i1)
hw.module @reset1(in %clk : i1, in %rst : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %true = hw.constant true
  %r = seq.firreg %d clock %c reset async %rst, %true : i1
  hw.output %r : i1
}

// The clock gate is found by its pins' clock_gate_*_pin roles.
// ICG: hw.module.extern @TICG(in %CLK : i1, in %ENA : i1, in %SE : i1, out GCLK : i1)
// ICG-LABEL: hw.module @gated
// ICG: hw.instance "clock_gate_0" @TICG(CLK: %{{.+}}: i1, ENA: %en: i1, SE: %false: i1) -> (GCLK: i1)
hw.module @gated(in %clk : i1, in %en : i1, in %d : i8, out q : i8) {
  %c = seq.to_clock %clk
  %n = comb.mux %en, %d, %r : i8
  %r = seq.firreg %n clock %c : i8
  hw.output %r : i8
}
