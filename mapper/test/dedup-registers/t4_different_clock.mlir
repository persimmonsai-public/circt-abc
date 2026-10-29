// Must-NOT-merge: identical nexts but different clocks.
hw.module @diff_clk(in %clka : i1, in %clkb : i1, in %en : i1, in %d : i32,
                    out o1 : i32, out o2 : i32) {
  %ca = seq.to_clock %clka
  %cb = seq.to_clock %clkb
  %n1 = comb.mux bin %en, %d, %r1 : i32
  %r1 = seq.firreg %n1 clock %ca : i32
  %n2 = comb.mux bin %en, %d, %r2 : i32
  %r2 = seq.firreg %n2 clock %cb : i32
  hw.output %r1, %r2 : i32, i32
}
