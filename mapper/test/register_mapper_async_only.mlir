// RUN: circt-abc-opt --register-mapper=liberty=%S/register_mapper_async_only.lib --canonicalize %s | FileCheck %s
//
// A library whose only flop has both an active-low clear and an active-low
// preset (register_mapper_async_only.lib). A register without a reset maps
// to it too, with both async pins tied to their inactive level; a reset-to-0
// register drives RN with the active-low reset and ties SN, and a
// reset-to-1 register the other way round.

// CHECK: hw.module.extern @TDFFASR(in %CLK : i1, in %D : i1, in %RN : i1, in %SN : i1, out Q : i1)

// CHECK-LABEL: hw.module @noreset
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[Q:.+]] = hw.instance "r_bit0" @TDFFASR(CLK: %clk: i1, D: %d: i1, RN: %[[T]]: i1, SN: %[[T]]: i1) -> (Q: i1)
// CHECK: hw.output %[[Q]]
hw.module @noreset(in %clk : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %r = seq.firreg %d clock %c : i1
  hw.output %r : i1
}

// CHECK-LABEL: hw.module @reset0
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[NR:.+]] = comb.xor %rst, %[[T]] : i1
// CHECK: hw.instance "r_bit0" @TDFFASR(CLK: %clk: i1, D: %d: i1, RN: %[[NR]]: i1, SN: %[[T]]: i1)
hw.module @reset0(in %clk : i1, in %rst : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %false = hw.constant false
  %r = seq.firreg %d clock %c reset async %rst, %false : i1
  hw.output %r : i1
}

// CHECK-LABEL: hw.module @reset1
// CHECK: %[[T:.+]] = hw.constant true
// CHECK: %[[NR:.+]] = comb.xor %rst, %[[T]] : i1
// CHECK: hw.instance "r_bit0" @TDFFASR(CLK: %clk: i1, D: %d: i1, RN: %[[T]]: i1, SN: %[[NR]]: i1)
hw.module @reset1(in %clk : i1, in %rst : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %true = hw.constant true
  %r = seq.firreg %d clock %c reset async %rst, %true : i1
  hw.output %r : i1
}
