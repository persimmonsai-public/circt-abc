// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=8 flatten-adds=true" --canonicalize %s 2>&1 | FileCheck %s
//
// flatten-adds: whole add expressions (chained adds, zero-extensions, shifts,
// gating muxes, compress row pairs) become one compress plus one add, only
// where that is exact. @chain: add(add(a,b),c) at one width (modular, always
// exact). @shared: the inner add has a second user, so it stays. @zext_ok /
// @zext_wrap: a zero-extended inner add merges only when it cannot wrap
// (operands zero-extended inside the inner add cannot wrap; plain 8-bit operands can).
// @gate_inv: mux with the zero on the true arm. @wrap_inner: a product plus a free 8-bit
// addend can wrap, so it must not merge through the zero-extension. All are proven equal to the original by a Yosys miter
// (scripts/circt_flow/tests/test_arith_lowering.py).
//
// CHECK: flatten-adds:

hw.module @chain(in %a : i8, in %b : i8, in %c : i8, in %d : i8, out o : i8) {
  %0 = comb.add %a, %b : i8
  %1 = comb.add %0, %c : i8
  %2 = comb.add %1, %d : i8
  hw.output %2 : i8
}

hw.module @shared(in %a : i8, in %b : i8, in %c : i8, in %d : i8, out o : i8, out p : i8) {
  %0 = comb.add %a, %b : i8
  %1 = comb.add %0, %c : i8
  %2 = comb.add %1, %d : i8
  hw.output %2, %0 : i8, i8
}

hw.module @zext_ok(in %a : i6, in %b : i6, in %c : i9, in %d : i9, out o : i9) {
  %z = hw.constant 0 : i1
  %z2 = hw.constant 0 : i2
  %ae = comb.concat %z, %a : i1, i6
  %be = comb.concat %z, %b : i1, i6
  %0 = comb.add %ae, %be : i7
  %1 = comb.concat %z2, %0 : i2, i7
  %3 = comb.add %1, %c, %d : i9
  hw.output %3 : i9
}

hw.module @zext_wrap(in %a : i8, in %b : i8, in %c : i9, in %d : i9, out o : i9) {
  %z = hw.constant 0 : i1
  %0 = comb.add %a, %b : i8
  %1 = comb.concat %z, %0 : i1, i8
  %2 = comb.add %1, %c : i9
  %3 = comb.add %2, %d : i9
  hw.output %3 : i9
}

hw.module @gate_inv(in %a : i6, in %b : i6, in %c : i7, in %s : i1, out o : i8) {
  %z = hw.constant 0 : i1
  %z7 = hw.constant 0 : i7
  %ae = comb.concat %z, %a : i1, i6
  %be = comb.concat %z, %b : i1, i6
  %0 = comb.add %ae, %be : i7
  %2 = comb.mux %s, %z7, %0 : i7
  %3 = comb.concat %z, %2 : i1, i7
  %4 = comb.concat %z, %c : i1, i7
  %5 = comb.add %3, %4 : i8
  hw.output %5 : i8
}

hw.module @wrap_inner(in %a : i4, in %b : i4, in %e : i8, in %y : i16, out o : i16) {
  %z4 = hw.constant 0 : i4
  %z8 = hw.constant 0 : i8
  %xa = comb.concat %z4, %a : i4, i4
  %xb = comb.concat %z4, %b : i4, i4
  %m = comb.mul %xa, %xb : i8
  %x = comb.add %e, %m : i8
  %xe = comb.concat %z8, %x : i8, i8
  %s = comb.add %xe, %y : i16
  hw.output %s : i16
}
