// RUN: circt-abc-opt --register-mapper="liberty=%S/register_mapper_multibit.lib multibit=%S/register_mapper_multibit_lowreset.json" %s 2>&1 | FileCheck %s
//
// A bank class whose reset pin is active low: the bank gets the complement of
// the register's (active-high) reset, built once per bank; widths [3, 2] bank a
// 5-bit register as 3 + 2, in index order, with no left-over bit. The no-reset
// class is not declared by the family, so the 2-bit plain register stays on
// single-bit cells.

// CHECK: [register-mapper] multibit: 2 bank(s) holding 5 bit(s) (1 TMB2CLRN, 1 TMB3CLRN)
// CHECK: hw.module.extern @TMB3CLRN(in %CK : i1, in %RN : i1, in %D0 : i1, in %D1 : i1, in %D2 : i1, out Q0 : i1, out Q1 : i1, out Q2 : i1)
// CHECK: hw.module @r
// CHECK: [[INV:%.+]] = comb.xor %rst, %true
// CHECK: hw.instance "r_mb0_2" @TMB3CLRN(CK: %{{.+}}: i1, RN: [[INV]]: i1,
// CHECK: [[INV2:%.+]] = comb.xor %rst, %true
// CHECK: hw.instance "r_mb3_4" @TMB2CLRN(CK: %{{.+}}: i1, RN: [[INV2]]: i1,
// CHECK: hw.module @p
// CHECK-COUNT-2: @TDFF(
// CHECK-NOT: MB

hw.module @r(in %clk : !seq.clock, in %rst : i1, in %d : i5, out q : i5) {
  %rv = hw.constant 0 : i5
  %reg = seq.firreg %d clock %clk reset async %rst, %rv {name = "r"} : i5
  hw.output %reg : i5
}
hw.module @p(in %clk : !seq.clock, in %d : i2, out q : i2) {
  %reg = seq.firreg %d clock %clk {name = "p"} : i2
  hw.output %reg : i2
}
