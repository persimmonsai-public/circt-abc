// RUN: circt-abc-opt --narrow-dead-register-bits %s | FileCheck %s --check-prefix=OPAQUE
// RUN: circt-abc-opt --narrow-dead-register-bits=precise-bitcast=true %s | FileCheck %s --check-prefix=PRECISE
//
// A 16-bit register reaches the output through an integer -> aggregate ->
// integer bitcast pair and only bits [7:0] of the integer view are read.
// Default: the first bitcast is opaque, the whole register stays live.
// precise-bitcast: the mask passes through the pair, bits [15:8] are dead
// and the register narrows to i8.
//
// OPAQUE: seq.firreg %d clock %{{.*}} : i16
// PRECISE: seq.firreg %{{.*}} clock %{{.*}} : i8
hw.module @pair(in %clk : i1, in %d : i16, out o : i8) {
  %c = seq.to_clock %clk
  %a = seq.firreg %d clock %c : i16
  %s = hw.bitcast %a : (i16) -> !hw.array<2xi8>
  %i = hw.bitcast %s : (!hw.array<2xi8>) -> i16
  %lo = comb.extract %i from 0 : (i16) -> i8
  hw.output %lo : i8
}
