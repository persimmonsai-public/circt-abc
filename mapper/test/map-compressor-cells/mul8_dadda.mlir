// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=8" --canonicalize --cse %s 2>&1 | FileCheck %s
//
// An 8x8 zero-extended multiply and a 3-operand add. Both become Dadda
// trees of the liberty's two-output adder cells; no comb.mul or datapath op
// survives, and the final two-row add is an ordinary comb.add. Proven
// 2026-09-14 by a Yosys miter against the original, with the adder cells
// modelled by their liberty functions.
//
// CHECK: [map-compressor-cells] 2 compressor tree(s), 88 input bit(s) -> 42 TFA + 8 THA
// CHECK-NOT: comb.mul
// CHECK-NOT: datapath.
// CHECK: hw.instance "cmp_fa_0" @TFA
// CHECK: comb.add

hw.module @mul8(in %a : i8, in %b : i8, in %c : i8, out p : i16, out q : i8) {
  %ae = comb.concat %z, %a : i8, i8
  %be = comb.concat %z, %b : i8, i8
  %z = hw.constant 0 : i8
  %p = comb.mul %ae, %be : i16
  %q = comb.add %a, %b, %c : i8
  hw.output %p, %q : i16, i8
}
