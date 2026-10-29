// Companion to the two soundness regressions: a genuinely-dead register
// SCC (two regs feeding only each other, never escaping to any output/
// instance) must STILL be removed by the fixed analysis -- the global
// fixpoint only seeds feedback from registers that are actually reachable
// from a real output/instance, so a dead cycle never gets kick-started.
// Expect: %a and %b removed, %live kept.
hw.module @deadscc(in %clk : i1, in %d : i8, out o : i8) {
  %c = seq.to_clock %clk
  %a = seq.firreg %b clock %c : i8
  %b = seq.firreg %a clock %c : i8
  %live = seq.firreg %d clock %c : i8
  hw.output %live : i8
}
