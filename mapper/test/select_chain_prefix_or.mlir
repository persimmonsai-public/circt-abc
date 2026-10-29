// RUN: circt-abc-opt --select-chain-prefix-or=enable=true --canonicalize --cse %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt --select-chain-prefix-or --canonicalize --cse %s | FileCheck %s --check-prefix=OFF
//
// A pass-through chain in the shape of a systolic-array PE row with no spill
// register, as merge-residual-modules + hw-aggregate-to-comb leave it
// (the struct is a word, valid = bit 3, mac_read = bit 2, data = bits 1:0):
//
//   p_k   = in_k.valid & !in_k.mac_read
//   out_k = p_k ? in_k : (pv_k ? pipe_k : 0);      in_{k+1} = out_k
//
// Each select reads the previous stage's mux. Under p_k = 1 that mux
// forwards in_k, so p_{k+1} = p_k | q_k with q_k = pv_k & pipe_k.valid &
// !pipe_k.mac_read, and p_3 = p_0 | q_0 | q_1 | q_2: no select depends on
// another stage's mux any more.
//
// CHECK: [select-chain-prefix-or] 3 select(s) cofactored, 2 prefix OR(s) flattened
// CHECK-LABEL: hw.module @chain
// CHECK-DAG: %[[P0:.+]] = comb.and %v, %{{.+}} : i1
// CHECK-DAG: %[[P1:.+]] = comb.or %[[P0]], %{{.+}} : i1
// CHECK-DAG: %[[P2:.+]] = comb.or %[[P0]], %{{.+}}, %{{.+}} : i1
// CHECK-DAG: %[[P3:.+]] = comb.or %[[P0]], %{{.+}}, %{{.+}}, %{{.+}} : i1
// CHECK-DAG: comb.mux %[[P0]], %{{.+}} : i4
// CHECK-DAG: comb.mux %[[P1]], %{{.+}} : i4
// CHECK-DAG: comb.mux %[[P2]], %{{.+}} : i4
// CHECK-DAG: comb.mux %[[P3]], %{{.+}} : i4
//
// OFF-LABEL: hw.module @chain
// OFF-NOT: comb.or
//
// A select whose true cofactor is NOT decided (the forwarded word is not
// the one the select tested) is left alone.
// CHECK-LABEL: hw.module @undecided
// CHECK-NOT: comb.or
hw.module @chain(in %v : i1, in %mr : i1, in %d : i2,
                 in %pv0 : i1, in %pipe0 : i4, in %pv1 : i1, in %pipe1 : i4,
                 in %pv2 : i1, in %pipe2 : i4, in %pv3 : i1, in %pipe3 : i4,
                 out o : i4) {
  %true = hw.constant true
  %c0 = hw.constant 0 : i4
  %in0 = comb.concat %v, %mr, %d : i1, i1, i2
  %nmr0 = comb.xor %mr, %true : i1
  %p0 = comb.and %v, %nmr0 : i1
  %b0 = comb.mux %pv0, %pipe0, %c0 : i4
  %out0 = comb.mux %p0, %in0, %b0 : i4

  %v1 = comb.extract %out0 from 3 : (i4) -> i1
  %mr1 = comb.extract %out0 from 2 : (i4) -> i1
  %nmr1 = comb.xor %mr1, %true : i1
  %p1 = comb.and %v1, %nmr1 : i1
  %b1 = comb.mux %pv1, %pipe1, %c0 : i4
  %out1 = comb.mux %p1, %out0, %b1 : i4

  %v2 = comb.extract %out1 from 3 : (i4) -> i1
  %mr2 = comb.extract %out1 from 2 : (i4) -> i1
  %nmr2 = comb.xor %mr2, %true : i1
  %p2 = comb.and %v2, %nmr2 : i1
  %b2 = comb.mux %pv2, %pipe2, %c0 : i4
  %out2 = comb.mux %p2, %out1, %b2 : i4

  %v3 = comb.extract %out2 from 3 : (i4) -> i1
  %mr3 = comb.extract %out2 from 2 : (i4) -> i1
  %nmr3 = comb.xor %mr3, %true : i1
  %p3 = comb.and %v3, %nmr3 : i1
  %b3 = comb.mux %pv3, %pipe3, %c0 : i4
  %out3 = comb.mux %p3, %out2, %b3 : i4
  hw.output %out3 : i4
}

hw.module @undecided(in %v : i1, in %mr : i1, in %other : i4,
                     in %pv : i1, in %pipe : i4, out o : i4) {
  %true = hw.constant true
  %nmr = comb.xor %mr, %true : i1
  %p0 = comb.and %v, %nmr : i1
  %m0 = comb.mux %p0, %other, %pipe : i4
  %v1 = comb.extract %m0 from 3 : (i4) -> i1
  %mr1 = comb.extract %m0 from 2 : (i4) -> i1
  %nmr1 = comb.xor %mr1, %true : i1
  %p1 = comb.and %v1, %nmr1 : i1
  %m1 = comb.mux %p1, %m0, %pipe : i4
  hw.output %m1 : i4
}
