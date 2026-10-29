// RUN: circt-abc-opt --strip-simulation-constructs %s 2>&1 | FileCheck %s
// circt-verilog emits `scf.if` (function bodies with early returns) and
// `ub.unreachable` (after `$fatal` -> sim.terminate). circt-abc-opt must
// parse both, and a `$fatal` check process that drives nothing is erased
// like an assertion process; one that also drives a signal is kept.

// CHECK: erased 1 assertion process(es)

// CHECK-LABEL: hw.module @early_return
// CHECK: scf.if
hw.module @early_return(in %v : i1, in %x : i2, out y : i2) {
  %c0_i2 = hw.constant 0 : i2
  %0 = scf.if %v -> (i2) {
    scf.yield %x : i2
  } else {
    scf.yield %c0_i2 : i2
  }
  hw.output %0 : i2
}

// CHECK-LABEL: hw.module @fatal_check
// CHECK-NOT: llhd.process
// CHECK-NOT: ub.unreachable
// CHECK: hw.output
hw.module @fatal_check(in %a : i4, in %b : i4) {
  %t = llhd.constant_time <0ns, 0d, 1e>
  %c0_i4 = hw.constant 0 : i4
  %sa = llhd.sig name "a" %c0_i4 : i4
  %sb = llhd.sig name "b" %c0_i4 : i4
  llhd.process {
    %msg = sim.fmt.literal "Fatal: mismatch"
    cf.br ^bb1
  ^bb1:
    %pa = llhd.prb %sa : i4
    %pb = llhd.prb %sb : i4
    %eq = comb.icmp ceq %pa, %pb : i4
    cf.cond_br %eq, ^bb1, ^bb2
  ^bb2:
    sim.proc.print %msg
    sim.terminate failure, quiet
    ub.unreachable
  }
  llhd.drv %sa, %a after %t : i4
  llhd.drv %sb, %b after %t : i4
  hw.output
}

// CHECK-LABEL: hw.module @fatal_with_drive
// CHECK: llhd.process
// CHECK: llhd.drv
hw.module @fatal_with_drive(in %a : i4) {
  %t = llhd.constant_time <0ns, 0d, 1e>
  %c0_i4 = hw.constant 0 : i4
  %s = llhd.sig name "s" %c0_i4 : i4
  llhd.process {
    cf.br ^bb1
  ^bb1:
    llhd.drv %s, %a after %t : i4
    %p = llhd.prb %s : i4
    %z = comb.icmp eq %p, %c0_i4 : i4
    cf.cond_br %z, ^bb1, ^bb2
  ^bb2:
    sim.terminate failure, quiet
    ub.unreachable
  }
  hw.output
}
