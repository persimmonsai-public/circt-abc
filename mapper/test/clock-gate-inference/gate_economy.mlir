// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib" %s 2>&1 | FileCheck %s --check-prefix=DEF
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib min-gate-bits=3" %s 2>&1 | FileCheck %s --check-prefix=MIN
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib gate-cost-bits=5 gate-idle-fraction=0.5" %s 2>&1 | FileCheck %s --check-prefix=COST
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib max-gate-depth=1" %s 2>&1 | FileCheck %s --check-prefix=DEPTH
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib max-gate-depth=2" %s 2>&1 | FileCheck %s --check-prefix=DEPTH2
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib max-gate-depth=3" %s 2>&1 | FileCheck %s --check-prefix=DEPTH3
//
// `narrow` (2 bits) and `wide` (8 bits) on enables a and b, both on the root
// clock; `deep` (8 bits, enable c) on a clock that already comes through one
// gate and `deeper` (8 bits, enable a) through two (RTL-instantiated gates,
// instances shaped like the library's, the second one's clock pin wired to
// the first one's output i1 directly).
//
// DEF: gated 4 firreg(s) into 4 shared clock-gate cell(s)
//
// min-gate-bits=3: the 2-bit group keeps its hold-mux.
// MIN: 1 below min-gate-bits
// MIN: gated 3 firreg(s) into 3 shared clock-gate cell(s)
//
// gate-cost-bits=5 at half idle: a gate pays from 10 bits, so none of the
// four (2, 8, 8, 8 bits) is gated.
// COST: 4 failing the savings test
// COST: gated 0 firreg(s) into 0 shared clock-gate cell(s)
//
// max-gate-depth=1: `deep` and `deeper` already sit behind gates; 2 allows
// `deep` (two gates in series), 3 allows `deeper`.
// DEPTH: 2 over max-gate-depth
// DEPTH: gated 2 firreg(s) into 2 shared clock-gate cell(s)
// DEPTH2: 1 over max-gate-depth
// DEPTH2: gated 3 firreg(s) into 3 shared clock-gate cell(s)
// DEPTH3: 0 over max-gate-depth
// DEPTH3: gated 4 firreg(s) into 4 shared clock-gate cell(s)
hw.module.extern @TICG(in %CK : i1, in %E : i1, in %SE : i1, out ECK : i1)
hw.module @top(in %clk_i : i1, in %a : i1, in %b : i1, in %c : i1, in %rtl_en : i1,
               in %d : i8, out q1 : i2, out q2 : i8, out q3 : i8, out q4 : i8) {
  %clk = seq.to_clock %clk_i
  %zero = hw.constant false
  %clk_i1 = seq.from_clock %clk
  %g = hw.instance "rtl_gate" @TICG(CK: %clk_i1: i1, E: %rtl_en: i1, SE: %zero: i1) -> (ECK: i1)
  %gclk = seq.to_clock %g
  %g2 = hw.instance "rtl_gate2" @TICG(CK: %g: i1, E: %rtl_en: i1, SE: %zero: i1) -> (ECK: i1)
  %g2clk = seq.to_clock %g2
  %dn = comb.extract %d from 0 : (i8) -> i2
  %narrow = seq.firreg %n1 clock %clk {name = "narrow"} : i2
  %n1 = comb.mux %a, %dn, %narrow : i2
  %wide = seq.firreg %n2 clock %clk {name = "wide"} : i8
  %n2 = comb.mux %b, %d, %wide : i8
  %deep = seq.firreg %n3 clock %gclk {name = "deep"} : i8
  %n3 = comb.mux %c, %d, %deep : i8
  %deeper = seq.firreg %n4 clock %g2clk {name = "deeper"} : i8
  %n4 = comb.mux %a, %d, %deeper : i8
  hw.output %narrow, %wide, %deep, %deeper : i2, i8, i8, i8
}
