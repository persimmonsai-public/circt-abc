// Regression for NarrowDeadRegisterBits soundness Bug B (2026-08-27): a
// 2-flop shift register where regA's ONLY consumer is regB's next value.
// Both registers are live (b feeds the output; a feeds b one cycle
// earlier). The pre-fix pass replaced %a with constant 0 -- silent
// functional corruption (confirmed to have fired on real ibex_top:
// ibex_multdiv_slow's multdiv_count_q was absent from the shipped
// netlist). Expect: BOTH registers survive, untouched.
hw.module @chain(in %clk : i1, in %d : i8, out o : i8) {
  %c = seq.to_clock %clk
  %a = seq.firreg %d clock %c : i8
  %b = seq.firreg %a clock %c : i8
  hw.output %b : i8
}
