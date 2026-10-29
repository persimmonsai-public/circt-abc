// RUN: circt-abc-opt --localize-dynamic-rmw %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt --localize-dynamic-rmw=max-cone-ops=2 %s 2>&1 | FileCheck --check-prefix=CONE %s
// RUN: circt-abc-opt --localize-dynamic-rmw=max-clone-ops=8 %s 2>&1 | FileCheck --check-prefix=CLONE %s
// The dynamic-index read-modify-write idiom `mem[i].a = mem[i].a + d`:
// array_inject %arr[%i], g(array_get %arr[%i]). hw-aggregate-to-comb would
// lower it per entry k to mux(i == k, g(muxtree(arr, i)), arr[k]) with the
// read tree only redundant under the select; the pass rewrites it at the
// aggregate level into array_create of mux(i == k, g(arr[k]), arr[k]).

// The pass reports per module (stderr comes before the IR).
// CHECK: [localize-dynamic-rmw] rmw: 1 of 1 dynamic RMW inject(s) localized
// CHECK: [localize-dynamic-rmw] 1 module(s) rewritten, 1 inject(s) localized, 0 skipped

// CHECK-LABEL: hw.module @rmw
// The shared (read-independent) op stays single; per entry k: a
// constant-index read, the dependent cone (struct_extract, add,
// struct_inject) cloned over it, icmp eq i, k, and a struct-typed mux.
// CHECK:      %[[SH:.+]] = comb.add %d, %d : i8
// CHECK-NOT:  comb.add %d, %d
// CHECK-DAG:  %[[C0:.+]] = hw.constant 0 : i2
// CHECK-DAG:  %[[C1:.+]] = hw.constant 1 : i2
// CHECK-DAG:  %[[C2:.+]] = hw.constant -2 : i2
// CHECK-DAG:  %[[C3:.+]] = hw.constant -1 : i2
// CHECK-DAG:  %[[R0:.+]] = hw.array_get %arr[%[[C0]]]
// CHECK-DAG:  %[[X0:.+]] = hw.struct_extract %[[R0]]["a"]
// CHECK-DAG:  %[[S0:.+]] = comb.add %[[X0]], %[[SH]] : i8
// CHECK-DAG:  %[[N0:.+]] = hw.struct_inject %[[R0]]["a"], %[[S0]]
// CHECK-DAG:  %[[E0:.+]] = comb.icmp eq %i, %[[C0]] : i2
// CHECK-DAG:  %[[M0:.+]] = comb.mux %[[E0]], %[[N0]], %[[R0]] : !hw.struct<a: i8, v: i1>
// CHECK-DAG:  %[[R1:.+]] = hw.array_get %arr[%[[C1]]]
// CHECK-DAG:  %[[E1:.+]] = comb.icmp eq %i, %[[C1]] : i2
// CHECK-DAG:  %[[M1:.+]] = comb.mux %[[E1]], %{{.+}}, %[[R1]]
// CHECK-DAG:  %[[R2:.+]] = hw.array_get %arr[%[[C2]]]
// CHECK-DAG:  %[[E2:.+]] = comb.icmp eq %i, %[[C2]] : i2
// CHECK-DAG:  %[[M2:.+]] = comb.mux %[[E2]], %{{.+}}, %[[R2]]
// CHECK-DAG:  %[[R3:.+]] = hw.array_get %arr[%[[C3]]]
// CHECK-DAG:  %[[E3:.+]] = comb.icmp eq %i, %[[C3]] : i2
// CHECK-DAG:  %[[M3:.+]] = comb.mux %[[E3]], %{{.+}}, %[[R3]]
// Highest index first.
// CHECK:      %[[RES:.+]] = hw.array_create %[[M3]], %[[M2]], %[[M1]], %[[M0]] : !hw.struct<a: i8, v: i1>
// CHECK-NOT:  hw.array_inject
// CHECK-NOT:  hw.array_get %arr[%i]
// CHECK:      hw.output %[[RES]]
hw.module @rmw(in %arr : !hw.array<4xstruct<a: i8, v: i1>>, in %i : i2, in %d : i8,
               out r : !hw.array<4xstruct<a: i8, v: i1>>) {
  %dd = comb.add %d, %d : i8
  %e = hw.array_get %arr[%i] : !hw.array<4xstruct<a: i8, v: i1>>, i2
  %f = hw.struct_extract %e["a"] : !hw.struct<a: i8, v: i1>
  %s = comb.add %f, %dd : i8
  %n = hw.struct_inject %e["a"], %s : !hw.struct<a: i8, v: i1>
  %r = hw.array_inject %arr[%i], %n : !hw.array<4xstruct<a: i8, v: i1>>, i2
  hw.output %r : !hw.array<4xstruct<a: i8, v: i1>>
}

// A dynamic inject whose value does not read arr[i] is not an RMW: untouched.
// CHECK-LABEL: hw.module @plain_write
// CHECK:      hw.struct_create
// CHECK-NEXT: hw.array_inject %arr[%i]
// CHECK-NEXT: hw.output
hw.module @plain_write(in %arr : !hw.array<4xstruct<a: i8, v: i1>>, in %i : i2, in %d : i8,
                       out r : !hw.array<4xstruct<a: i8, v: i1>>) {
  %true = hw.constant true
  %n = hw.struct_create (%d, %true) : !hw.struct<a: i8, v: i1>
  %r = hw.array_inject %arr[%i], %n : !hw.array<4xstruct<a: i8, v: i1>>, i2
  hw.output %r : !hw.array<4xstruct<a: i8, v: i1>>
}

// A constant-index RMW already reads a single entry: untouched.
// CHECK-LABEL: hw.module @const_index
// CHECK:      hw.array_get %arr[%c1_i2]
// CHECK:      hw.array_inject %arr[%c1_i2]
// CHECK-NOT:  hw.array_create
hw.module @const_index(in %arr : !hw.array<4xstruct<a: i8, v: i1>>, in %d : i8,
                       out r : !hw.array<4xstruct<a: i8, v: i1>>) {
  %c1_i2 = hw.constant 1 : i2
  %e = hw.array_get %arr[%c1_i2] : !hw.array<4xstruct<a: i8, v: i1>>, i2
  %f = hw.struct_extract %e["a"] : !hw.struct<a: i8, v: i1>
  %s = comb.add %f, %d : i8
  %n = hw.struct_inject %e["a"], %s : !hw.struct<a: i8, v: i1>
  %r = hw.array_inject %arr[%c1_i2], %n : !hw.array<4xstruct<a: i8, v: i1>>, i2
  hw.output %r : !hw.array<4xstruct<a: i8, v: i1>>
}

// Over budget: @rmw's dependent cone is 3 ops (struct_extract, add,
// struct_inject); max-cone-ops=2 or max-clone-ops=8 (< 4 x 3) leaves it
// alone and reports the skip.
// CONE: [localize-dynamic-rmw] rmw: 0 of 1 dynamic RMW inject(s) localized (skipped: 1 over max-cone-ops )
// CONE: [localize-dynamic-rmw] 0 module(s) rewritten, 0 inject(s) localized, 1 skipped
// CONE-LABEL: hw.module @rmw
// CONE:       hw.array_get %arr[%i]
// CONE:       hw.array_inject %arr[%i]
// CONE-NOT:   hw.array_create
// CONE-LABEL: hw.module @plain_write
// CLONE: [localize-dynamic-rmw] rmw: 0 of 1 dynamic RMW inject(s) localized (skipped: 1 over max-clone-ops )
// CLONE-LABEL: hw.module @rmw
// CLONE:       hw.array_inject %arr[%i]
// CLONE-NOT:   hw.array_create
// CLONE-LABEL: hw.module @plain_write
