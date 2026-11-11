// RUN: circt-abc-opt "--map-adder-chains=enable-adder-chains=true liberty=%S/adders.lib min-width=8 max-width=16" %s 2>&1 | FileCheck %s
//
// max-width bounds the ripple lowering to short chains: the 12- and 16-bit
// adds become cell chains, the 40-bit add stays a comb.add.
//
// CHECK: lowered 2 comb.add op(s)
// CHECK: comb.add {{.*}} : i40

hw.module @m(in %a : i12, in %b : i12, in %c : i16, in %d : i16, in %e : i40, in %f : i40,
             out x : i12, out y : i16, out z : i40) {
  %0 = comb.add %a, %b : i12
  %1 = comb.add %c, %d : i16
  %2 = comb.add %e, %f : i40
  hw.output %0, %1, %2 : i12, i16, i40
}
