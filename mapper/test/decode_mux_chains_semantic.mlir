// RUN: circt-abc-opt --mlir-disable-threading --decode-mux-chains --canonicalize %s 2>&1 | FileCheck %s
// semantic: exclusive only by truth table (the small-support fallback);
// overlap: not exclusive, must stay a chain. Both proven equivalent
// before/after with a Yosys miter + sat on 2026-09-02.
//
// This test carried a RUN line and ZERO CHECK lines until 2026-09-19, so it
// had always failed the moment anything actually executed it.
// `--mlir-disable-threading` is required: the pass runs per module and MLIR
// threads those, so the diagnostics otherwise reorder and interleave.

// The whole point of this file is that `semantic` is NOT structurally
// exclusive -- the conditions are ORs of ANDs -- so only the truth-table
// fallback can decode it. If the fallback regresses, this line fails while
// decode_mux_chains.mlir still passes.
// CHECK-DAG: decode-mux-chains] semantic: 1 chain(s), 4 link(s)
// CHECK-DAG: decode-mux-chains] overlap: declined 1 non-exclusive chain(s)

// CHECK-LABEL: hw.module @semantic
// CHECK:         comb.or bin {{.*}} : i8

// overlap keeps its serial chain: decoding it would change the function.
// CHECK-LABEL: hw.module @overlap
// CHECK:         %[[M0:.*]] = comb.mux %{{.*}}, %d, %dflt : i8
// CHECK:         %[[M1:.*]] = comb.mux %{{.*}}, %c, %[[M0]] : i8
// CHECK:         %[[M2:.*]] = comb.mux %{{.*}}, %b, %[[M1]] : i8
// CHECK:         comb.mux %{{.*}}, %a, %[[M2]] : i8

// Exclusive only semantically: arms keyed by a 3-bit address with an
// enable folded into ORs of ANDs the structural test cannot decode.
hw.module @semantic(in %addr : i3, in %en : i1, in %a : i8, in %b : i8, in %c : i8, in %d : i8, in %dflt : i8, out o : i8) {
  %c0 = hw.constant 0 : i3
  %c1 = hw.constant 1 : i3
  %c2 = hw.constant 2 : i3
  %c3 = hw.constant 3 : i3
  %c4 = hw.constant 4 : i3
  %true = hw.constant true
  %e0 = comb.icmp eq %addr, %c0 : i3
  %e1 = comb.icmp eq %addr, %c1 : i3
  %e2 = comb.icmp eq %addr, %c2 : i3
  %e3 = comb.icmp eq %addr, %c3 : i3
  %e4 = comb.icmp eq %addr, %c4 : i3
  %nen = comb.xor %en, %true : i1
  // cond0 = en & (addr==0 | addr==1) ; cond1 = en & addr==2 ; cond2 = ~en & (addr==0|addr==3) ; cond3 = addr==4
  %or01 = comb.or %e0, %e1 : i1
  %k0 = comb.and %en, %or01 : i1
  %k1 = comb.and %en, %e2 : i1
  %or03 = comb.or %e0, %e3 : i1
  %k2 = comb.and %nen, %or03 : i1
  %m3 = comb.mux %e4, %d, %dflt : i8
  %m2 = comb.mux %k2, %c, %m3 : i8
  %m1 = comb.mux %k1, %b, %m2 : i8
  %m0 = comb.mux %k0, %a, %m1 : i8
  hw.output %m0 : i8
}
// NOT exclusive (addr==0 appears under both polarities of en AND alone): must stay a chain.
hw.module @overlap(in %addr : i3, in %en : i1, in %a : i8, in %b : i8, in %c : i8, in %d : i8, in %dflt : i8, out o : i8) {
  %c0 = hw.constant 0 : i3
  %c1 = hw.constant 1 : i3
  %c2 = hw.constant 2 : i3
  %e0 = comb.icmp eq %addr, %c0 : i3
  %e1 = comb.icmp eq %addr, %c1 : i3
  %e2 = comb.icmp eq %addr, %c2 : i3
  %k0 = comb.and %en, %e0 : i1
  %m3 = comb.mux %e0, %d, %dflt : i8
  %m2 = comb.mux %e2, %c, %m3 : i8
  %m1 = comb.mux %e1, %b, %m2 : i8
  %m0 = comb.mux %k0, %a, %m1 : i8
  hw.output %m0 : i8
}
