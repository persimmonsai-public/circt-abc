// RUN: circt-abc-opt --localize-dynamic-rmw %s 2>&1 | FileCheck %s
// hw.module bodies are graph regions: a use may precede its definition.
// The pass must (a) not crash or clone an inject into its own replacement
// when the element feeds back to the inject through an instance, and
// (b) still localize an acyclic read-modify-write written out of order.

// CHECK: [localize-dynamic-rmw] 1 module(s) rewritten, 1 inject(s) localized, 1 skipped

hw.module.extern private @fb(in %d : i2, out q : i1)

// The element's enable comes from an instance fed by the injected array:
// a combinational cycle. Declined, IR unchanged.
// CHECK-LABEL: hw.module @cyclic
// CHECK: hw.array_inject
hw.module @cyclic(in %sel : i1, in %a : !hw.array<2xi1>, out o : !hw.array<2xi1>) {
  %fb.q = hw.instance "fb" @fb(d: %cat: i2) -> (q: i1)
  %r = hw.array_get %a[%sel] : !hw.array<2xi1>, i1
  %e = comb.and %r, %fb.q : i1
  %inj = hw.array_inject %a[%sel], %e : !hw.array<2xi1>, i1
  %false = hw.constant false
  %true = hw.constant true
  %g0 = hw.array_get %inj[%false] : !hw.array<2xi1>, i1
  %g1 = hw.array_get %inj[%true] : !hw.array<2xi1>, i1
  %cat = comb.concat %g1, %g0 : i1, i1
  hw.output %inj : !hw.array<2xi1>
}

// Acyclic, but the cone is written uses-before-definitions.
// CHECK-LABEL: hw.module @out_of_order
// CHECK-NOT: hw.array_inject
// CHECK: hw.array_create
hw.module @out_of_order(in %sel : i1, in %a : !hw.array<2xi4>, in %d : i4, out o : !hw.array<2xi4>) {
  %inj = hw.array_inject %a[%sel], %sum : !hw.array<2xi4>, i1
  %sum = comb.add %r, %d2 : i4
  %d2 = comb.xor %d, %d : i4
  %r = hw.array_get %a[%sel] : !hw.array<2xi4>, i1
  hw.output %inj : !hw.array<2xi4>
}
