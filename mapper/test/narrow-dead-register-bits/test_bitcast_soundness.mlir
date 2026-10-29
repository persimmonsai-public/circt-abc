// Regression for NarrowDeadRegisterBits soundness Bug A (2026-08-27): an
// integer register whose only path to the output crosses an hw.bitcast
// (aggregate-typed output port). The opaque channel hands the demand back
// into the precise channel -- but pre-fix, the precise worklist was only
// drained once, BEFORE the opaque loop ran, so the handed-back demand
// never propagated and the register was replaced with constant 0.
// Expect: the register survives, untouched.
hw.module @viabitcast(in %clk : i1, in %d : i16, out o : !hw.array<2xi8>) {
  %c = seq.to_clock %clk
  %a = seq.firreg %d clock %c : i16
  %o1 = hw.bitcast %a : (i16) -> !hw.array<2xi8>
  hw.output %o1 : !hw.array<2xi8>
}
