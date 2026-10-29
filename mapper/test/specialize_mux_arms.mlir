// RUN: circt-abc-opt --specialize-mux-arms=enable=true --canonicalize %s | FileCheck %s
// RUN: circt-abc-opt --specialize-mux-arms --canonicalize %s | FileCheck %s --check-prefix=OFF

// cva6 load_unit, after llhd-remove-control-flow: data_req in IDLE carries an
// OR that is 1 under its own select, so it structurally depends on gnt.
// CHECK-LABEL: hw.module @load_unit_req
// CHECK-NOT: comb.{{.*}}%gnt
// CHECK: %[[R:.+]] = comb.mux %{{.+}}, %accept, %other
// CHECK: hw.output %[[R]]
// OFF-LABEL: hw.module @load_unit_req
// OFF: comb.xor %gnt
hw.module @load_unit_req(in %state : i4, in %accept : i1, in %gnt : i1,
                         in %other : i1, out req : i1) {
  %true = hw.constant true
  %c0 = hw.constant 0 : i4
  %c1 = hw.constant 1 : i4
  %c3 = hw.constant 3 : i4
  %idle = comb.icmp eq %state, %c0 : i4
  %s3 = comb.icmp eq %state, %c3 : i4
  %waitgnt = comb.icmp eq %state, %c1 : i4
  %ngnt = comb.xor %gnt, %true : i1
  %ns3 = comb.xor %s3, %true : i1
  %t = comb.or %idle, %ns3 : i1
  %u = comb.or %ngnt, %t : i1
  %a = comb.and %accept, %u : i1
  // WAIT_GNT arm: %idle == 0 and %s3 == 0 are implied by state == 1
  %b = comb.and %other, %u : i1
  %inner = comb.mux %waitgnt, %b, %other : i1
  %r = comb.mux %idle, %a, %inner : i1
  hw.output %r : i1
}

// The false arm is specialised under select = 0.
// CHECK-LABEL: hw.module @false_arm
// CHECK: %[[M:.+]] = comb.mux %c, %x, %y
// CHECK: hw.output %[[M]]
hw.module @false_arm(in %c : i1, in %x : i1, in %y : i1, out o : i1) {
  %t = comb.or %c, %y : i1
  %m = comb.mux %c, %x, %t : i1
  hw.output %m : i1
}

// One-hot sum-of-products form: an AND input may assume its siblings are 1.
// CHECK-LABEL: hw.module @sop
// CHECK-NOT: comb.{{.*}}%gnt
// OFF-LABEL: hw.module @sop
hw.module @sop(in %state : i3, in %acc : i1, in %gnt : i1, in %z : i1, out o : i1) {
  %true = hw.constant true
  %c0 = hw.constant 0 : i3
  %c1 = hw.constant 1 : i3
  %c3 = hw.constant 3 : i3
  %idle = comb.icmp eq %state, %c0 : i3
  %wait = comb.icmp eq %state, %c1 : i3
  %s3 = comb.icmp eq %state, %c3 : i3
  %ngnt = comb.xor %gnt, %true : i1
  %ns3 = comb.xor %s3, %true : i1
  %t = comb.or %idle, %ns3 : i1
  %u = comb.or %ngnt, %z, %t : i1
  %p = comb.and %idle, %acc, %u : i1
  %q = comb.and %wait, %u : i1
  %o = comb.or %p, %q : i1
  hw.output %o : i1
}
