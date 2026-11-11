// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=8 merge-adds=true" --canonicalize %s 2>&1 | FileCheck %s
//
// merge-adds folds add(h, zext(add(rows of one compress))) into a single
// compress when the inner tree's inputs provably cannot reach 2^width (the
// inner add then never wraps). One code path per module. Equivalence of all
// three is proven by a Yosys miter in
// scripts/circt_flow/tests/test_arith_lowering.py.
//
// CHECK: merge-adds: 2 carry-propagate add(s) folded
// CHECK: [map-compressor-cells] 3 compressor tree(s)

// Path: a product plus an addend merges into one compressor tree (8x8 operands
// zero-extended from 4 bits cannot wrap in 8 bits).
hw.module @merge_prod(in %a : i4, in %b : i4, in %e : i16, out o : i16) {
  %z4 = hw.constant 0 : i4
  %z8 = hw.constant 0 : i8
  %xa = comb.concat %z4, %a : i4, i4
  %xb = comb.concat %z4, %b : i4, i4
  %m = comb.mul %xa, %xb : i8
  %m1 = comb.concat %z8, %m : i8, i8
  %s = comb.add %e, %m1 : i16
  hw.output %s : i16
}

// Path: a gated addend (a mux with zero) merges the same way.
hw.module @merge_gated(in %a : i4, in %b : i4, in %e : i16, in %en : i1, out o : i16) {
  %z4 = hw.constant 0 : i4
  %z16 = hw.constant 0 : i16
  %z8 = hw.constant 0 : i8
  %xa = comb.concat %z4, %a : i4, i4
  %xb = comb.concat %z4, %b : i4, i4
  %m = comb.mul %xa, %xb : i8
  %m1 = comb.concat %z8, %m : i8, i8
  %g = comb.mux %en, %e, %z16 : i16
  %s = comb.add %g, %m1 : i16
  hw.output %s : i16
}

// Path: an 8x8 product truncated to 8 bits can wrap, so nothing merges.
hw.module @wrap(in %a : i8, in %b : i8, in %c : i16, out o : i16) {
  %z8 = hw.constant 0 : i8
  %m = comb.mul %a, %b : i8
  %me = comb.concat %z8, %m : i8, i8
  %s = comb.add %c, %me : i16
  hw.output %s : i16
}
