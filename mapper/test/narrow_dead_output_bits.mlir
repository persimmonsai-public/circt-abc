// RUN: circt-abc-opt --narrow-dead-output-bits --canonicalize %s 2>&1 | FileCheck %s
//
// The callee produces 8 bits; its only caller registers them whole and then
// reads bits [4:0] of the register and bit 7 through a mux -- bits 5 and 6
// are dead in every instance. The caller is a root: every one of its own
// output bits stays live, including the ones it forwards unchanged.
//
// CHECK: [narrow-dead-output-bits] 2 module(s), 2 port(s), 6 output bit(s) zeroed
// CHECK-LABEL: hw.module private @callee
// CHECK-DAG: %[[LO:.+]] = comb.extract %{{.*}} from 0 : (i8) -> i5
// CHECK-DAG: %[[HI:.+]] = comb.extract %{{.*}} from 7 : (i8) -> i1
// CHECK: %[[OUT:.+]] = comb.concat %[[HI]], %c0_i2, %[[LO]] : i1, i2, i5
// CHECK: hw.output %[[OUT]] : i8
// CHECK-LABEL: hw.module @caller
// CHECK-NOT: hw.constant 0 : i2
// CHECK: hw.output %{{.*}}, %{{.*}} : i6, i1

hw.module private @callee(in %a : i8, in %b : i8, out o : i8) {
  %d = comb.and %a, %b : i8
  hw.output %d : i8
}

hw.module @caller(in %clk : !seq.clock, in %a : i8, in %b : i8, in %s : i1,
                  out o : i6, out p : i1) {
  %c.o = hw.instance "c" @callee(a: %a: i8, b: %b: i8) -> (o: i8)
  %q = seq.firreg %c.o clock %clk : i8
  %lo = comb.extract %q from 0 : (i8) -> i5
  %hi = comb.extract %q from 7 : (i8) -> i1
  %bit5 = comb.extract %q from 5 : (i8) -> i1
  %sel = comb.mux %s, %hi, %s : i1
  %o = comb.concat %hi, %lo : i1, i5
  hw.output %o, %sel : i6, i1
}

// An aggregate-typed port: the callee builds its struct with a bitcast from
// an integer, the caller reads field `a` (bits [7:4]) through a bitcast back.
// The rewrite peels the callee's bitcast instead of stacking a second one, so
// the zeroed slice reads the integer directly (narrow-dead-register-bits sees
// through it), and bits [3:0] become zero.
// CHECK-LABEL: hw.module private @callee_s
// CHECK: %[[INT:.+]] = comb.concat %{{.*}}, %c0_i4 : i4, i4
// CHECK: %[[S:.+]] = hw.bitcast %[[INT]] : (i8) -> !hw.struct<a: i4, b: i4>
// CHECK: hw.output %[[S]]

hw.module private @callee_s(in %a : i8, in %b : i8, out o : !hw.struct<a: i4, b: i4>) {
  %d = comb.xor %a, %b : i8
  %s = hw.bitcast %d : (i8) -> !hw.struct<a: i4, b: i4>
  hw.output %s : !hw.struct<a: i4, b: i4>
}

hw.module @caller_s(in %a : i8, in %b : i8, out o : i4) {
  %c.o = hw.instance "c" @callee_s(a: %a: i8, b: %b: i8) -> (o: !hw.struct<a: i4, b: i4>)
  %i = hw.bitcast %c.o : (!hw.struct<a: i4, b: i4>) -> i8
  %fa = comb.extract %i from 4 : (i8) -> i4
  hw.output %fa : i4
}
