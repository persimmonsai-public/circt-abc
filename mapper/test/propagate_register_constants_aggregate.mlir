// RUN: circt-abc-opt --lower-aggregate-registers --propagate-register-constants --canonicalize %s | FileCheck %s
//
// An aggregate-typed register whose `next` and reset value are the same
// constant aggregate is constant. `--lower-aggregate-registers` rewrites it
// as an integer firreg whose `next` is an `hw.bitcast` of that aggregate,
// which `--propagate-register-constants` used to refuse to look through --
// so CVA6's `csr_regfile.pmpaddr_q` (2048 bits, `NrPMPEntries == 0`) and
// `pmpcfg_q` (512 bits) both survived to the netlist as constant-D flops.
//
// The packing convention is not asserted from first principles: every
// expected constant below was taken from CIRCT's own lowering of the same
// aggregate (`circt-opt --hw-aggregate-to-comb --canonicalize` on the
// bitcast alone), which is what these bits actually become downstream.
// First operand at the MSB for array_create / array_concat / struct_create;
// hw::aggregateAttrToAPInt for hw.aggregate_constant.

// array_create %c1, %c2, %c3, %c4 of i8 -> 0x01020304.
// CHECK-LABEL: hw.module @arr
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c16909060_i32
hw.module @arr(in %clk : !seq.clock, in %rst : i1, out o : i32) {
  %c1 = hw.constant 1 : i8
  %c2 = hw.constant 2 : i8
  %c3 = hw.constant 3 : i8
  %c4 = hw.constant 4 : i8
  %arr = hw.array_create %c1, %c2, %c3, %c4 : i8
  %r = seq.firreg %arr clock %clk reset async %rst, %arr : !hw.array<4xi8>
  %b = hw.bitcast %r : (!hw.array<4xi8>) -> i32
  hw.output %b : i32
}

// struct_create (1:i4, 2:i4, 3:i8) -> 0x1203; first field at the MSB.
// CHECK-LABEL: hw.module @strct
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c4611_i16
hw.module @strct(in %clk : !seq.clock, in %rst : i1, out o : i16) {
  %a = hw.constant 1 : i4
  %b = hw.constant 2 : i4
  %c = hw.constant 3 : i8
  %s = hw.struct_create (%a, %b, %c) : !hw.struct<f0: i4, f1: i4, f2: i8>
  %r = seq.firreg %s clock %clk reset async %rst, %s : !hw.struct<f0: i4, f1: i4, f2: i8>
  %bc = hw.bitcast %r : (!hw.struct<f0: i4, f1: i4, f2: i8>) -> i16
  hw.output %bc : i16
}

// The pmpcfg_q shape: an array of structs, mixing struct_create with
// hw.aggregate_constant -> 0x102506. `--canonicalize` alone does NOT fold
// this one, which is why the reset side needed the same structural packing
// in --lower-aggregate-registers rather than an hw.aggregate_constant match.
// CHECK-LABEL: hw.module @nested
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c1058054_i24
hw.module @nested(in %clk : !seq.clock, in %rst : i1, out o : i24) {
  %a = hw.constant 1 : i4
  %b = hw.constant 2 : i8
  %s = hw.struct_create (%a, %b) : !hw.struct<f0: i4, f1: i8>
  %t = hw.aggregate_constant [5 : i4, 6 : i8] : !hw.struct<f0: i4, f1: i8>
  %arr = hw.array_create %s, %t : !hw.struct<f0: i4, f1: i8>
  %r = seq.firreg %arr clock %clk reset async %rst, %arr : !hw.array<2xstruct<f0: i4, f1: i8>>
  %bc = hw.bitcast %r : (!hw.array<2xstruct<f0: i4, f1: i8>>) -> i24
  hw.output %bc : i24
}

// array_concat, same 0x01020304 as @arr.
// CHECK-LABEL: hw.module @cat
// CHECK-NOT:     seq.firreg
// CHECK:         hw.output %c16909060_i32
hw.module @cat(in %clk : !seq.clock, in %rst : i1, out o : i32) {
  %c1 = hw.constant 1 : i8
  %c2 = hw.constant 2 : i8
  %c3 = hw.constant 3 : i8
  %c4 = hw.constant 4 : i8
  %a1 = hw.array_create %c1, %c2 : i8
  %a2 = hw.array_create %c3, %c4 : i8
  %arr = hw.array_concat %a1, %a2 : !hw.array<2xi8>, !hw.array<2xi8>
  %r = seq.firreg %arr clock %clk reset async %rst, %arr : !hw.array<4xi8>
  %bc = hw.bitcast %r : (!hw.array<4xi8>) -> i32
  hw.output %bc : i32
}

// NEGATIVE: one element is a real module input, so nothing is provably
// constant and the register must survive. "When in doubt, do not propagate."
// CHECK-LABEL: hw.module @live
// CHECK:         seq.firreg
hw.module @live(in %clk : !seq.clock, in %rst : i1, in %d : i8, out o : i16) {
  %c1 = hw.constant 1 : i8
  %arr = hw.array_create %c1, %d : i8
  %rv = hw.aggregate_constant [1 : i8, 0 : i8] : !hw.array<2xi8>
  %r = seq.firreg %arr clock %clk reset async %rst, %rv : !hw.array<2xi8>
  %bc = hw.bitcast %r : (!hw.array<2xi8>) -> i16
  hw.output %bc : i16
}
