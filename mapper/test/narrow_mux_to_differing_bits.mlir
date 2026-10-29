// RUN: circt-abc-opt --narrow-mux-to-differing-bits=enable-narrow-mux=true --canonicalize --cse %s | FileCheck %s
//
// The scoreboard write shape: a chain of wide muxes each of which sets ONE
// bit of the previous value. Every mux must come out as a 1-bit mux, not a
// 64-bit one -- including the SECOND link, whose false arm is the first
// link's rebuilt concat rather than a plain value (that is why the arms are
// compared by bit provenance and not structurally).

// CHECK-LABEL: hw.module @chain
hw.module @chain(in %x : i64, in %c0 : i1, in %c1 : i1, out o : i64) {
  %true = hw.constant true
  %hi0 = comb.extract %x from 21 : (i64) -> i43
  %lo0 = comb.extract %x from 0 : (i64) -> i20
  %n0  = comb.concat %hi0, %true, %lo0 : i43, i1, i20
  %v0  = comb.mux %c0, %n0, %x : i64

  %hi1 = comb.extract %v0 from 41 : (i64) -> i23
  %lo1 = comb.extract %v0 from 0 : (i64) -> i40
  %n1  = comb.concat %hi1, %true, %lo1 : i23, i1, i40
  %v1  = comb.mux %c1, %n1, %v0 : i64

  hw.output %v1 : i64
}
// Each update comes out as a single BIT, which canonicalize then folds to
// `or` because the true arm is the constant 1 ...
// CHECK: comb.or %c0, %{{.+}} : i1
// CHECK: comb.or %c1, %{{.+}} : i1
// CHECK: comb.concat
// ... and no wide mux is left at all.
// CHECK-NOT: comb.mux

// A mux whose arms share nothing must be left exactly as it was -- the
// rewrite would be the same total mux width in more ops.
// CHECK-LABEL: hw.module @unrelated
hw.module @unrelated(in %a : i32, in %b : i32, in %c : i1, out o : i32) {
  %m = comb.mux %c, %a, %b : i32
  hw.output %m : i32
}
// CHECK: comb.mux %c, %a, %b : i32
