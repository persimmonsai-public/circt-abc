// RUN: circt-abc-opt --clock-gate-inference=liberty=%S/icg.lib %s 2>&1 | FileCheck %s --check-prefix=TOP
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=BIT
//
// A 2-entry x 4-bit entry array with a whole-array flush hold over
// per-entry update muxes, the way CVA6's TLB reaches register-legalize
// (see ClockGateInferencePass.cpp, "Bit-level enables"). The outer mux is
// a whole-register hold, so the top-level shape gates the register once on
// !flush and keeps the per-entry update chain as hold-muxes; per bit,
// entry i loads %d exactly when !flush & upd_i, and the chain goes away.
//
// TOP: gated 1 firreg(s) into 1 shared clock-gate cell(s)
// TOP-DAG: comb.mux %upd1
// TOP-DAG: %q = seq.firreg %{{.*}} clock %{{.*}} reset async %rst, %c0_i8 : i8
//
// Bit-level: two gated 4-bit parts (entries 0 and 1), no ungated part, each
// part's next the bare update value, the original data re-assembled from
// the parts, and no mux left anywhere.
// BIT: bit-level split 1 register(s), 8 bit(s) gated
// BIT: gated 1 firreg(s) into 2 shared clock-gate cell(s)
// BIT-NOT: comb.mux
// BIT-DAG: %q_cg0 = seq.firreg %d clock %{{.*}} reset async %rst, %c0_i4 : i4
// BIT-DAG: %q_cg1 = seq.firreg %d clock %{{.*}} reset async %rst, %c0_i4 : i4
// BIT-DAG: comb.and %{{.*}}, %upd0 : i1
// BIT-DAG: comb.and %{{.*}}, %upd1 : i1
// BIT-DAG: hw.instance "clock_gate_0" @TICG
// BIT-DAG: hw.instance "clock_gate_1" @TICG
// BIT-DAG: comb.concat %q_cg1, %q_cg0 : i4, i4
// BIT-NOT: %q_ug
// BIT-NOT: comb.mux

hw.module @entry_array(in %clk : !seq.clock, in %rst : i1, in %flush : i1,
                       in %upd0 : i1, in %upd1 : i1, in %d : i4,
                       out o : i8) {
  %c0_i8 = hw.constant 0 : i8
  // entry 0 update: replace bits [3:0] of the current array
  %hi = comb.extract %q from 4 : (i8) -> i4
  %lo = comb.extract %q from 0 : (i8) -> i4
  %set0 = comb.concat %hi, %d : i4, i4
  %link0 = comb.mux %upd0, %set0, %q : i8
  // entry 1 update: replace bits [7:4] of link0
  %link0_lo = comb.extract %link0 from 0 : (i8) -> i4
  %set1 = comb.concat %d, %link0_lo : i4, i4
  %link1 = comb.mux %upd1, %set1, %link0 : i8
  // whole-array flush: hold (a flush here does not touch the data bits)
  %next = comb.mux %flush, %q, %link1 : i8
  %q = seq.firreg %next clock %clk reset async %rst, %c0_i8 : i8
  hw.output %q : i8
}
