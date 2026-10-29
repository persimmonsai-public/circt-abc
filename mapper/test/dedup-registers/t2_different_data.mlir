// Must-NOT-merge: identical shape but DIFFERENT data operands (%d1 vs %d2).
hw.module @diff_data(in %clk : i1, in %en : i1, in %d1 : i32, in %d2 : i32,
                     out o1 : i32, out o2 : i32) {
  %clock = seq.to_clock %clk
  %n1 = comb.mux bin %en, %d1, %r1 : i32
  %r1 = seq.firreg %n1 clock %clock : i32
  %n2 = comb.mux bin %en, %d2, %r2 : i32
  %r2 = seq.firreg %n2 clock %clock : i32
  hw.output %r1, %r2 : i32, i32
}
