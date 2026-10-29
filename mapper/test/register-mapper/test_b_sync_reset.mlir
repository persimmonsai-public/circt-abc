// Test B: a `reset sync` firreg (isAsync=false). None of the 3 target
// liberty cells have a synchronous-reset variant, so register-legalize
// must fold this into `next = comb.mux(rst, rv, d)` feeding a plain
// (no-reset) firreg, which register-mapper then maps to plain-flop
// instances only, with the mux logic feeding D directly.
hw.module @sreg(in %clk : !seq.clock, in %rst : i1, in %d : i4, in %rv : i4, out q : i4) {
  %reg = seq.firreg %d clock %clk reset sync %rst, %rv : i4
  hw.output %reg : i4
}
