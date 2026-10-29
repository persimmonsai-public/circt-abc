// RUN: circt-abc-opt --mlir-disable-threading --decode-mux-chains --canonicalize %s 2>&1 | FileCheck %s
// key5 and nested are rewritten (structural exclusivity), prio is a
// priority chain and must stay a mux chain. Equivalence of every module
// before/after was proven with a Yosys miter + sat on 2026-09-02.
//
// This test carried a RUN line and ZERO CHECK lines until 2026-09-19, so it
// had always failed the moment anything actually executed it. The
// assertions below encode what the paragraph above already claimed.

// Both exclusive chains are decoded: each arm becomes a guarded value and
// the results are OR-ed, with the default enabled only when no key matched.
// `--mlir-disable-threading` is required, not cosmetic: this pass runs per
// module and MLIR threads those, so without it the per-module diagnostics
// both reorder AND interleave mid-line (observed: "...32[decode-mux-chains]
// link-bit(s)"). The directives are still DAG because even single-threaded
// the visit order is key5, prio, nested -- not source order.
// CHECK-DAG: decode-mux-chains] key5: 1 chain(s), 5 link(s)
// CHECK-DAG: decode-mux-chains] nested: 1 chain(s), 4 link(s)
// The priority chain is recognised and REFUSED -- this is the soundness
// case, since decoding it would change what the module computes.
// CHECK-DAG: decode-mux-chains] prio: declined 1 non-exclusive chain(s)

// CHECK-LABEL: hw.module @key5
// CHECK:         comb.or bin %{{.*}}, %{{.*}}, %{{.*}}, %{{.*}}, %{{.*}} : i1
// CHECK:         comb.or bin {{.*}} : i8

// CHECK-LABEL: hw.module @nested
// CHECK:         comb.or bin {{.*}} : i4

// prio keeps its serial mux chain, each mux feeding the next.
// CHECK-LABEL: hw.module @prio
// CHECK:         %[[M0:.*]] = comb.mux %{{.*}}, %d, %dflt : i8
// CHECK:         %[[M1:.*]] = comb.mux %{{.*}}, %c, %[[M0]] : i8
// CHECK:         %[[M2:.*]] = comb.mux %{{.*}}, %b, %[[M1]] : i8
// CHECK:         comb.mux %{{.*}}, %a, %[[M2]] : i8

hw.module @key5(in %k : i3, in %a : i8, in %b : i8, in %c : i8, in %d : i8, in %e : i8, in %dflt : i8, out o : i8) {
  %c0 = hw.constant 0 : i3
  %c1 = hw.constant 1 : i3
  %c2 = hw.constant 2 : i3
  %c3 = hw.constant 3 : i3
  %c5 = hw.constant 5 : i3
  %e0 = comb.icmp eq %k, %c0 : i3
  %e1 = comb.icmp eq %k, %c1 : i3
  %e2 = comb.icmp eq %k, %c2 : i3
  %e3 = comb.icmp eq %k, %c3 : i3
  %e5 = comb.icmp eq %k, %c5 : i3
  %m4 = comb.mux %e5, %e, %dflt : i8
  %m3 = comb.mux %e3, %d, %m4 : i8
  %m2 = comb.mux %e2, %c, %m3 : i8
  %m1 = comb.mux %e1, %b, %m2 : i8
  %m0 = comb.mux %e0, %a, %m1 : i8
  hw.output %m0 : i8
}
hw.module @nested(in %op : i2, in %f : i2, in %a : i4, in %b : i4, in %c : i4, in %d : i4, in %dflt : i4, out o : i4) {
  %c0 = hw.constant 0 : i2
  %c1 = hw.constant 1 : i2
  %c2 = hw.constant 2 : i2
  %op0 = comb.icmp eq %op, %c0 : i2
  %op1 = comb.icmp eq %op, %c1 : i2
  %f0 = comb.icmp eq %f, %c0 : i2
  %f1 = comb.icmp eq %f, %c1 : i2
  %f2 = comb.icmp eq %f, %c2 : i2
  %k0 = comb.and %op0, %f0 : i1
  %k1 = comb.and %op0, %f1 : i1
  %k2 = comb.and %op1, %f0 : i1
  %k3 = comb.and %op1, %f2 : i1
  %m3 = comb.mux %k3, %d, %dflt : i4
  %m2 = comb.mux %k2, %c, %m3 : i4
  %m1 = comb.mux %k1, %b, %m2 : i4
  %m0 = comb.mux %k0, %a, %m1 : i4
  hw.output %m0 : i4
}
hw.module @prio(in %p : i4, in %a : i8, in %b : i8, in %c : i8, in %d : i8, in %dflt : i8, out o : i8) {
  %b0 = comb.extract %p from 0 : (i4) -> i1
  %b1 = comb.extract %p from 1 : (i4) -> i1
  %b2 = comb.extract %p from 2 : (i4) -> i1
  %b3 = comb.extract %p from 3 : (i4) -> i1
  %m3 = comb.mux %b3, %d, %dflt : i8
  %m2 = comb.mux %b2, %c, %m3 : i8
  %m1 = comb.mux %b1, %b, %m2 : i8
  %m0 = comb.mux %b0, %a, %m1 : i8
  hw.output %m0 : i8
}
