// RUN: not circt-abc-opt --split-input-file --register-mapper=liberty=%S/register_mapper_dffr.lib %s 2>&1 | FileCheck %s
// RUN: not circt-abc-opt --split-input-file --register-mapper=liberty=%S/register_mapper_dffr.lib %s 2>&1 | FileCheck --check-prefix=NOREDEF %s
// RUN: not circt-abc-opt --split-input-file --register-mapper=liberty=%S/register_mapper_dffr.lib %s 2>&1 | FileCheck --check-prefix=BAD %s
// NOREDEF-NOT: redefinition of symbol
// The design already instantiates the library flop directly, so a
// declaration of it exists -- with its pins in a different order than the
// mapper's (clock, data, reset). The mapper must reuse that declaration (no
// "redefinition of symbol") and wire its instances by pin name.

// CHECK-LABEL: hw.module.extern @TDFFR
// CHECK: hw.instance "u" @TDFFR(CK: %clk: i1, D: %d: i1, R: %rst: i1)
// CHECK: hw.instance "r_bit0" @TDFFR(CK: %{{.+}}: i1, D: %{{.+}}: i1, R: %rst: i1)

hw.module.extern @TDFFR(in %CK : i1, in %D : i1, out Q : i1, in %R : i1)
hw.module @top(in %clk : i1, in %rst : i1, in %d : i1, out q0 : i1, out q1 : i1) {
  %u.Q = hw.instance "u" @TDFFR(CK: %clk : i1, D: %d : i1, R: %rst : i1) -> (Q: i1)
  %c = seq.to_clock %clk
  %false = hw.constant false
  %r = seq.firreg %d clock %c reset async %rst, %false : i1
  hw.output %u.Q, %r : i1, i1
}

// -----

// A declaration under the cell's name whose pins are not the cell's: refused.
// BAD: existing declaration of 'TDFFR' does not have the cell's pins
hw.module.extern @TDFFR(in %CLK : i1, in %DIN : i1, out Q : i1, in %R : i1)
hw.module @top2(in %clk : i1, in %rst : i1, in %d : i1, out q : i1) {
  %c = seq.to_clock %clk
  %false = hw.constant false
  %r = seq.firreg %d clock %c reset async %rst, %false : i1
  hw.output %r : i1
}
