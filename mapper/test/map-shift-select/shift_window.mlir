// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-count=2 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=AUTO
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-cuts=2,2 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=DUP
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-cuts=1,2,3,4,5,6,7,8,9,10,11,12,13 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=LONG
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-cuts=999 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=RANGE
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-count=40 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=COUNT
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-cuts=2,x liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=ASCII
// RUN: not circt-abc-opt "--map-shift-select=enable-shift-select=true chains=true direct-decode=true window-form=true window-general=true window-types=0:5-4 liberty=%S/shiftrec.lib min-arms=8 min-width=8 wide4-cells=true recipes=2,2@2,2,3" --canonicalize %s 2>&1 | FileCheck %s --check-prefix=TYPES
// DUP: window-cuts: duplicate cut 2
// LONG: window-cuts: more than 12 cuts
// RANGE: window form requested but not applied: window-cuts: cut 999 is outside the shift range
// COUNT: window form requested but not applied: window-count 40 is not reachable
// ASCII: window-cuts: 'x' is not an ASCII integer
// TYPES: window-types: '0:5-4': first > last
//
// Window form (window-form=true): the nodes of the level before the last are built once per
// window of shift amounts and gated by "digit AND window"; the last level selects inside the
// window. The input is an invented zero-filled left shifter: a 5-bit word shifted by 1 + a raw
// 4-bit amount (14 constant arms, the default arm unshifted) into 22 bits. Equivalence on
// generated shifters (exact, default arm included): scripts/circt_flow/tests/test_shift_select.py
// (test_shift_select_window_form_*).

// CHECK: [map-shift-select] window form: windows [{{[0-9]+}},{{[0-9]+}}) [{{[0-9]+}},{{[0-9]+}}) cost={{.*}} copies=
// CHECK: [map-shift-select] recipe off={{[0-9]+}} radices=2,2,
// CHECK: 1 one-hot select(s) built as {{.*}} AND-OR networks, {{[0-9]+}} cell(s); 0 declined
// CHECK-LABEL: hw.module @lsh
// CHECK-NOT: comb.mux
// CHECK-NOT: comb.add
// CHECK-NOT: comb.icmp
// AUTO: [map-shift-select] window form: windows
// AUTO: 0 declined
hw.module @lsh(in %sel : i7, in %x : i5, out y : i22) {
  %amt = comb.extract %sel from 3 : (i7) -> i4
  %k0 = hw.constant 0 : i4
  %s0 = comb.icmp ceq %amt, %k0 : i4
  %k1 = hw.constant 1 : i4
  %s1 = comb.icmp ceq %amt, %k1 : i4
  %k2 = hw.constant 2 : i4
  %s2 = comb.icmp ceq %amt, %k2 : i4
  %k3 = hw.constant 3 : i4
  %s3 = comb.icmp ceq %amt, %k3 : i4
  %k4 = hw.constant 4 : i4
  %s4 = comb.icmp ceq %amt, %k4 : i4
  %k5 = hw.constant 5 : i4
  %s5 = comb.icmp ceq %amt, %k5 : i4
  %k6 = hw.constant 6 : i4
  %s6 = comb.icmp ceq %amt, %k6 : i4
  %k7 = hw.constant 7 : i4
  %s7 = comb.icmp ceq %amt, %k7 : i4
  %k8 = hw.constant 8 : i4
  %s8 = comb.icmp ceq %amt, %k8 : i4
  %k9 = hw.constant 9 : i4
  %s9 = comb.icmp ceq %amt, %k9 : i4
  %k10 = hw.constant 10 : i4
  %s10 = comb.icmp ceq %amt, %k10 : i4
  %k11 = hw.constant 11 : i4
  %s11 = comb.icmp ceq %amt, %k11 : i4
  %k12 = hw.constant 12 : i4
  %s12 = comb.icmp ceq %amt, %k12 : i4
  %k13 = hw.constant 13 : i4
  %s13 = comb.icmp ceq %amt, %k13 : i4
  %h0 = hw.constant 0 : i16
  %l0 = hw.constant 0 : i1
  %w0 = comb.concat %h0, %x, %l0 : i16, i5, i1
  %h1 = hw.constant 0 : i15
  %l1 = hw.constant 0 : i2
  %w1 = comb.concat %h1, %x, %l1 : i15, i5, i2
  %h2 = hw.constant 0 : i14
  %l2 = hw.constant 0 : i3
  %w2 = comb.concat %h2, %x, %l2 : i14, i5, i3
  %h3 = hw.constant 0 : i13
  %l3 = hw.constant 0 : i4
  %w3 = comb.concat %h3, %x, %l3 : i13, i5, i4
  %h4 = hw.constant 0 : i12
  %l4 = hw.constant 0 : i5
  %w4 = comb.concat %h4, %x, %l4 : i12, i5, i5
  %h5 = hw.constant 0 : i11
  %l5 = hw.constant 0 : i6
  %w5 = comb.concat %h5, %x, %l5 : i11, i5, i6
  %h6 = hw.constant 0 : i10
  %l6 = hw.constant 0 : i7
  %w6 = comb.concat %h6, %x, %l6 : i10, i5, i7
  %h7 = hw.constant 0 : i9
  %l7 = hw.constant 0 : i8
  %w7 = comb.concat %h7, %x, %l7 : i9, i5, i8
  %h8 = hw.constant 0 : i8
  %l8 = hw.constant 0 : i9
  %w8 = comb.concat %h8, %x, %l8 : i8, i5, i9
  %h9 = hw.constant 0 : i7
  %l9 = hw.constant 0 : i10
  %w9 = comb.concat %h9, %x, %l9 : i7, i5, i10
  %h10 = hw.constant 0 : i6
  %l10 = hw.constant 0 : i11
  %w10 = comb.concat %h10, %x, %l10 : i6, i5, i11
  %h11 = hw.constant 0 : i5
  %l11 = hw.constant 0 : i12
  %w11 = comb.concat %h11, %x, %l11 : i5, i5, i12
  %h12 = hw.constant 0 : i4
  %l12 = hw.constant 0 : i13
  %w12 = comb.concat %h12, %x, %l12 : i4, i5, i13
  %h13 = hw.constant 0 : i3
  %l13 = hw.constant 0 : i14
  %w13 = comb.concat %h13, %x, %l13 : i3, i5, i14
  %hd = hw.constant 0 : i17
  %wd = comb.concat %hd, %x : i17, i5
  %c13 = comb.mux %s13, %w13, %wd : i22
  %c12 = comb.mux %s12, %w12, %c13 : i22
  %c11 = comb.mux %s11, %w11, %c12 : i22
  %c10 = comb.mux %s10, %w10, %c11 : i22
  %c9 = comb.mux %s9, %w9, %c10 : i22
  %c8 = comb.mux %s8, %w8, %c9 : i22
  %c7 = comb.mux %s7, %w7, %c8 : i22
  %c6 = comb.mux %s6, %w6, %c7 : i22
  %c5 = comb.mux %s5, %w5, %c6 : i22
  %c4 = comb.mux %s4, %w4, %c5 : i22
  %c3 = comb.mux %s3, %w3, %c4 : i22
  %c2 = comb.mux %s2, %w2, %c3 : i22
  %c1 = comb.mux %s1, %w1, %c2 : i22
  %c0 = comb.mux %s0, %w0, %c1 : i22
  hw.output %c0 : i22
}
