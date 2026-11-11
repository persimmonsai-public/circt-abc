// RUN: circt-abc-opt --register-mapper="liberty=%S/register_mapper_multibit.lib multibit=%S/register_mapper_multibit.json" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt --register-mapper="liberty=%S/register_mapper_multibit.lib multibit=%S/register_mapper_multibit_cross.json" %s 2>&1 | FileCheck %s --check-prefix=CROSS
// RUN: circt-abc-opt --register-mapper=liberty=%S/register_mapper_multibit.lib %s 2>&1 | FileCheck %s --check-prefix=SINGLE
//
// Multibit banking in the register mapper, against an invented pack spec.
//   @r  : 11-bit async-clear register (reset 0), a family. Widths declared
//         [2, 8, 4]: bits 0-7 -> one 8-bit bank, bits 8-10 -> a 2-bit bank,
//         bit 10 stays a single-bit cell.
//   @hot_r: 5-bit clear register matched by the "^hot_" override -> the
//         "b" family (widths 4, 2): a 4-bit bank and a single bit.
//   @n  : 3-bit no-reset register: a 2-bit bank (SI tied to its own last Q)
//         and a single bit.
//   @s  : 2-bit async-preset register: set bits are never banked.
//   @x  : two 5-bit clear registers on the same clock and reset. Alone each
//         gets a 4-bit bank and a single bit; with cross_register their 10
//         bits are pooled into one 8-bit bank (a0-a4 + b0-b2) and a 2-bit
//         bank (b3-b4).

// CHECK: [register-mapper] multibit: 6 bank(s) holding 24 bit(s)
// CHECK-DAG: hw.module.extern @TMB8CLR_A(in %CK : i1, in %R : i1, in %SE : i1, in %SI : i1, in %D0 : i1, in %D1 : i1, in %D2 : i1, in %D3 : i1, in %D4 : i1, in %D5 : i1, in %D6 : i1, in %D7 : i1, out Q0 : i1, out Q1 : i1, out Q2 : i1, out Q3 : i1, out Q4 : i1, out Q5 : i1, out Q6 : i1, out Q7 : i1)
// CHECK-DAG: hw.module.extern @TMB2CLR_A
// CHECK-DAG: hw.module.extern @TMB4CLR_A
// CHECK-DAG: hw.module.extern @TMB4CLR_B
// CHECK-DAG: hw.module.extern @TMB2NRS_A(in %CK : i1, in %SE : i1, in %SI : i1, in %D0 : i1, in %D1 : i1, out Q0 : i1, out Q1 : i1)
// CHECK: hw.module @r
// CHECK: hw.instance "r_mb0_7" @TMB8CLR_A
// CHECK: hw.instance "r_mb8_9" @TMB2CLR_A
// CHECK: hw.instance "r_bit10" @TDFFR
// CHECK: hw.module @hot_r
// CHECK: hw.instance "hot_r_mb0_3" @TMB4CLR_B
// CHECK: hw.instance "hot_r_bit4" @TDFFR
// CHECK: hw.module @n
// CHECK: hw.instance "n_mb0_1" @TMB2NRS_A(CK: %{{.+}}: i1, SE: %{{.+}}: i1, SI: %n_mb0_1.Q1: i1,
// CHECK: hw.instance "n_bit2" @TDFF
// CHECK: hw.module @s
// CHECK-COUNT-2: @TDFFS
// CHECK: hw.module @x
// CHECK: hw.instance "xa_mb0_3" @TMB4CLR_A
// CHECK: hw.instance "xa_bit4" @TDFFR
// CHECK: hw.instance "xb_mb0_3" @TMB4CLR_A
// CHECK: hw.instance "xb_bit4" @TDFFR

// CROSS: [register-mapper] multibit: 6 bank(s) holding 26 bit(s)
// CROSS: hw.module @r
// CROSS: hw.instance "r_mb0_7" @TMB8CLR_A
// CROSS: hw.instance "r_mb8_9" @TMB2CLR_A
// CROSS: hw.instance "r_bit10" @TDFFR
// CROSS: hw.module @x
// CROSS: hw.instance "xa_mb0_2_x0" @TMB8CLR_A
// CROSS: hw.instance "xb_mb3_4" @TMB2CLR_A
// CROSS-NOT: @TDFFR

// SINGLE: [register-mapper] mapped 6 firreg(s) -> 31 bit-instance(s)
// SINGLE-NOT: multibit:
// SINGLE-NOT: TMB{{[0-9]}}

hw.module @r(in %clk : !seq.clock, in %rst : i1, in %d : i11, out q : i11) {
  %rv = hw.constant 0 : i11
  %reg = seq.firreg %d clock %clk reset async %rst, %rv {name = "r"} : i11
  hw.output %reg : i11
}
hw.module @hot_r(in %clk : !seq.clock, in %rst : i1, in %d : i5, out q : i5) {
  %rv = hw.constant 0 : i5
  %reg = seq.firreg %d clock %clk reset async %rst, %rv {name = "hot_r"} : i5
  hw.output %reg : i5
}
hw.module @n(in %clk : !seq.clock, in %d : i3, out q : i3) {
  %reg = seq.firreg %d clock %clk {name = "n"} : i3
  hw.output %reg : i3
}
hw.module @s(in %clk : !seq.clock, in %rst : i1, in %d : i2, out q : i2) {
  %rv = hw.constant 3 : i2
  %reg = seq.firreg %d clock %clk reset async %rst, %rv {name = "s"} : i2
  hw.output %reg : i2
}
hw.module @x(in %clk : !seq.clock, in %rst : i1, in %d : i5, in %e : i5, out q : i5, out p : i5) {
  %rv = hw.constant 0 : i5
  %a = seq.firreg %d clock %clk reset async %rst, %rv {name = "xa"} : i5
  %b = seq.firreg %e clock %clk reset async %rst, %rv {name = "xb"} : i5
  hw.output %a, %b : i5, i5
}
