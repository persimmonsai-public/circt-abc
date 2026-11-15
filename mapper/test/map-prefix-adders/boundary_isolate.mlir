// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=BRENT-KUNG boundary=1" %s 2>&1 | FileCheck %s --check-prefix=BND
// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=BRENT-KUNG isolate-inputs=true" %s 2>&1 | FileCheck %s --check-prefix=ISO
// RUN: circt-abc-opt "--map-prefix-adders=enable-prefix-adders=true liberty=%S/prefix.lib min-width=8 arch=BRENT-KUNG" %s 2>&1 | FileCheck %s --check-prefix=PLAIN
//
// boundary=1: each operand bit pair feeds one NAND2 (g) and one NOR2 (t) cell and nothing else; the prefix
// network behind them is comb logic for ABC. isolate-inputs=true: the first level reads each operand bit
// with one NAND2 and one NOR2 only, the rest is cells. Equivalence (SAT) and "is applied":
// scripts/circt_flow/tests/test_mapper_capabilities.py. Any other boundary value is refused
// (test_prefix_boundary_other_than_0_or_1_is_refused).

// BND: [map-prefix-adders] 1 add(s) built as BRENT-KUNG prefix networks, 32 cell(s)
// BND-LABEL: hw.module @add16
// BND-COUNT-16: hw.instance {{.*}} @TNAND2(A: %{{[0-9]+}}: i1, B: %{{[0-9]+}}: i1)
// BND-NOT: @TXOR2
// BND-NOT: @TAOI21
// BND: comb.xor
// ISO: [map-prefix-adders] 1 add(s) built as BRENT-KUNG prefix networks
// ISO-LABEL: hw.module @add16
// ISO-NOT: comb.add
// ISO-NOT: comb.and
// PLAIN-LABEL: hw.module @add16
// PLAIN-NOT: comb.add
hw.module @add16(in %a : i16, in %b : i16, out y : i16) {
  %0 = comb.add %a, %b : i16
  hw.output %0 : i16
}
