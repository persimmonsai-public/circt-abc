// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/prefix.lib min-width=8 prefix-final-adder=true" --canonicalize --cse %s 2>&1 | FileCheck %s
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/prefix.lib min-width=8" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=DEFAULT
//
// `prefix-final-adder`: build each compressor tree's final two-row add as a
// Brent-Kung prefix network of cells, instead of leaving a comb.add for ABC.
//
// Why it exists: with the tree already emitted as adder cells, the final add
// reaches ABC as an ISOLATED cone from primary inputs, and area-only
// restructuring collapses its prefix structure into a near-ripple -- it is
// smaller. On the design that motivated this, that turned a 128-bit final
// adder into 54 serial stages, which dominated the worst path while the
// adder-cell tree in front of it took 5.
//
// Polarity is tracked rather than inverted eagerly, so the emitter uses the
// cheaper INVERTING compound cells (NAND2/NOR2/AOI21/OAI21) that a CMOS
// prefix tree is normally built from.
//
// Equivalence: every module below, plus three-operand adds at widths 2, 3,
// 5, 7, 9, 12, 16, 17, 31, 33, 64, 65 and 128, SAT-proven against the
// original with Yosys (combinational, so the proofs are complete); a
// control with one cell swapped fails as it must.

// CHECK: [map-compressor-cells] 3 compressor tree(s), 188 input bit(s) -> 100 TFA + 19 THA
// CHECK: [map-compressor-cells] 3 final adder(s) built as Brent-Kung prefix networks, {{[0-9]+}} cell(s)
// CHECK-NOT: comb.add
// CHECK-NOT: comb.mul
// The prefix tree really does use inverting compound cells.
// CHECK-DAG: @TAOI21
// CHECK-DAG: @TOAI21

// DEFAULT-NOT: final adder(s) built
// DEFAULT: comb.add

hw.module @mul8(in %a : i8, in %b : i8, out o : i16) {
  %c0 = hw.constant 0 : i8
  %ae = comb.concat %c0, %a : i8, i8
  %be = comb.concat %c0, %b : i8, i8
  %p = comb.mul %ae, %be : i16
  hw.output %p : i16
}
// A non-power-of-two width: the prefix schedule's awkward case.
hw.module @mul13(in %a : i13, in %b : i13, out o : i13) {
  %p = comb.mul %a, %b : i13
  hw.output %p : i13
}
hw.module @add3(in %a : i11, in %b : i11, in %c : i11, out o : i11) {
  %s = comb.add %a, %b, %c : i11
  hw.output %s : i11
}
