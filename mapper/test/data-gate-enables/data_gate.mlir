// RUN: circt-abc-opt "--data-gate-enables=slices=q@0-3,q@12-15@xor,dg/h@4-7" %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--data-gate-enables=slices=q@0-3,q@12-15@xor,dg/h@4-7" "--clock-gate-inference=liberty=%S/../clock-gate-inference/icg.lib bit-level-enables=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=CG
// RUN: circt-abc-opt --data-gate-enables %s 2>&1 | FileCheck %s --check-prefix=OFF
// RUN: not circt-abc-opt "--data-gate-enables=slices=q@3-1" %s 2>&1 | FileCheck %s --check-prefix=BAD
// RUN: not circt-abc-opt "--data-gate-enables=slices=q@0-3@ZERO" %s 2>&1 | FileCheck %s --check-prefix=BAD
// RUN: circt-abc-opt "--data-gate-enables=slices=m@0-1,q@0-30" %s 2>&1 | FileCheck %s --check-prefix=SKIP
// RUN: circt-abc-opt "--data-gate-enables=slices=zz@0-1" %s 2>&1 | FileCheck %s --check-prefix=NONE
//
// data-gate-enables: each named slice of a hold-mux register loads
// (load==0 && q==0 ? q : load) (zero, the default) or (load==q ? q : load)
// (xor); the other bits and the hold mux are unchanged. The register-level
// identity (combinational part) is SAT-proven in
// scripts/circt_flow/tests/test_mapper_capabilities.py.

// CHECK-DAG: [data-gate-enables] dg/q: 2 slice(s) rewritten
// CHECK-DAG: [data-gate-enables] dg/h: 1 slice(s) rewritten
// CHECK-DAG: [data-gate-enables] other/q: 2 slice(s) rewritten
// CHECK: [data-gate-enables] 3 register(s) rewritten
// CHECK-LABEL: hw.module @dg
// xor mode on q[15:12]: load == q
// CHECK: %[[L8:.+]] = comb.extract %d from 12 : (i16) -> i4
// CHECK: %[[Q8:.+]] = comb.extract %q from 12 : (i16) -> i4
// CHECK: %[[SX:.+]] = comb.icmp eq %[[L8]], %[[Q8]] : i4
// CHECK: comb.mux %[[SX]], %[[Q8]], %[[L8]] : i4
// zero mode on q[3:0]: load == 0 and q == 0
// CHECK: %[[L0:.+]] = comb.extract %d from 0 : (i16) -> i4
// CHECK: %[[Q0:.+]] = comb.extract %q from 0 : (i16) -> i4
// CHECK: %[[Z:.+]] = hw.constant 0 : i4
// CHECK: %[[A:.+]] = comb.icmp eq %[[L0]], %[[Z]] : i4
// CHECK: %[[B:.+]] = comb.icmp eq %[[Q0]], %[[Z]] : i4
// CHECK: %[[S:.+]] = comb.and %[[A]], %[[B]] : i1
// CHECK: comb.mux %[[S]], %[[Q0]], %[[L0]] : i4
// CHECK: %[[N:.+]] = comb.mux %en, %{{.+}}, %q : i16
// CHECK: %q = seq.firreg %[[N]] clock
// hold-on-true register h: the slice [7:4] only, the hold mux keeps its polarity
// CHECK: comb.mux %en, %h, %{{.+}} : i8
// CHECK: %m = seq.firreg
// CHECK-LABEL: hw.module @other
// CHECK: %h = seq.firreg
hw.module @dg(in %clk_i : i1, in %en : i1, in %d : i16, in %e : i8, out o : i16, out p : i8, out r : i8) {
  %clk = seq.to_clock %clk_i
  %n = comb.mux %en, %d, %q : i16
  %q = seq.firreg %n clock %clk {name = "q"} : i16
  %nh = comb.mux %en, %h, %e : i8
  %h = seq.firreg %nh clock %clk {name = "h"} : i8
  %nm = comb.xor %e, %m : i8
  %m = seq.firreg %nm clock %clk {name = "m"} : i8
  hw.output %q, %h, %m : i16, i8, i8
}

// "dg/h" names one module: other/h is left alone.
hw.module @other(in %clk_i : i1, in %en : i1, in %d : i16, in %e : i8, out o : i16, out p : i8) {
  %clk = seq.to_clock %clk_i
  %n = comb.mux %en, %d, %q : i16
  %q = seq.firreg %n clock %clk {name = "q"} : i16
  %nh = comb.mux %en, %e, %h : i8
  %h = seq.firreg %nh clock %clk {name = "h"} : i8
  hw.output %q, %h : i16, i8
}

// The slices get gates of their own (their enable depends on the data): 5
// gates in @dg (2 without the pass), 3 in @other (1).
// CG-LABEL: hw.module @dg
// CG-COUNT-5: hw.instance "clock_gate_{{[0-9]}}" @TICG
// CG-LABEL: hw.module @other
// CG-COUNT-3: hw.instance "clock_gate_{{[0-9]}}" @TICG
// OFF-NOT: data-gate-enables
// BAD: bad slice
// SKIP: [data-gate-enables] dg/m: next is not a plain hold mux; skipped
// SKIP: [data-gate-enables] q: slices overlap or exceed width 16
// SKIP: [data-gate-enables] slice m@0-1 found but skipped (reason above)
// SKIP: [data-gate-enables] slice q@0-30 found but skipped (reason above)
// NONE: [data-gate-enables] slice zz@0-1 matched no register
