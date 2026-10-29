// Must-NOT-merge: identical nexts but different reset VALUES.
hw.module @diff_rv(in %clk : i1, in %rst : i1, in %en : i1, in %d : i32,
                   out o1 : i32, out o2 : i32) {
  %clock = seq.to_clock %clk
  %c0 = hw.constant 0 : i32
  %c5 = hw.constant 5 : i32
  %n1 = comb.mux bin %en, %d, %r1 : i32
  %r1 = seq.firreg %n1 clock %clock reset async %rst, %c0 : i32
  %n2 = comb.mux bin %en, %d, %r2 : i32
  %r2 = seq.firreg %n2 clock %clock reset async %rst, %c5 : i32
  hw.output %r1, %r2 : i32, i32
}
