// Test C: a plain firreg with no reset at all. Expect a straight map to
// plain-flop (no-reset) instances only (no reset/set pin wiring at all).
hw.module @preg(in %clk : !seq.clock, in %d : i4, out q : i4) {
  %reg = seq.firreg %d clock %clk : i4
  hw.output %reg : i4
}
