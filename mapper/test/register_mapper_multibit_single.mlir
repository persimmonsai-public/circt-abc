// RUN: circt-abc-opt --register-mapper="liberty=%S/register_mapper_multibit.lib multibit=%S/register_mapper_multibit_single.json" %s 2>&1 | FileCheck %s
//
// A class's optional `single: {cell, data, out}` entry (register_mapper_multibit_single.json): the bits the
// banker leaves over become one instance of the single-bit cell, with the class's clock, reset and tie pins,
// instead of the liberty's plain flop. Widths here are [8, 4], so
//   @r : 11-bit clear register -> an 8-bit bank, 3 single cells (TSB1CLR_A, SE and SI tied 0)
//   @n : 3-bit no-reset register -> 3 single cells (TSB1NRS_A), none left to TDFF
//   @s : async-preset bits are not banked and stay the liberty's TDFFS.

// CHECK: [register-mapper] multibit: 1 bank(s) holding 8 bit(s) (1 TMB8CLR_A); 8 single-bit cell(s), of which 6 were bank candidates left over; 6 of the single-bit cells are the pack's (3 TSB1CLR_A, 3 TSB1NRS_A)
// CHECK-DAG: hw.module.extern @TSB1CLR_A(in %CK : i1, in %R : i1, in %SE : i1, in %SI : i1, in %D : i1, out Q : i1)
// CHECK-DAG: hw.module.extern @TSB1NRS_A(in %CK : i1, in %SE : i1, in %SI : i1, in %D : i1, out Q : i1)
// CHECK: hw.module @r
// CHECK: hw.instance "r_mb0_7" @TMB8CLR_A
// CHECK: hw.instance "r_bit8" @TSB1CLR_A(CK: %{{.+}}: i1, R: %{{.+}}: i1, SE: %{{.+}}: i1, SI: %{{.+}}: i1,
// CHECK: hw.instance "r_bit9" @TSB1CLR_A
// CHECK: hw.instance "r_bit10" @TSB1CLR_A
// CHECK: hw.module @n
// CHECK: hw.instance "n_bit0" @TSB1NRS_A(CK: %{{.+}}: i1, SE: %{{.+}}: i1, SI: %n_bit0.Q: i1,
// CHECK: hw.instance "n_bit2" @TSB1NRS_A
// CHECK-NOT: @TDFF(
// CHECK: hw.module @s
// CHECK-COUNT-2: @TDFFS

hw.module @r(in %clk : !seq.clock, in %rst : i1, in %d : i11, out q : i11) {
  %rv = hw.constant 0 : i11
  %reg = seq.firreg %d clock %clk reset async %rst, %rv {name = "r"} : i11
  hw.output %reg : i11
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
