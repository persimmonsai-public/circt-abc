// Must-merge: reset-bearing pair, same reset net, equal constant reset values
// (distinct hw.constant ops -- must match structurally, not by SSA identity).
hw.module @merge_rst(in %clk : i1, in %rst : i1, in %en : i1, in %d : i32,
                     out o1 : i32, out o2 : i32) {
  %clock = seq.to_clock %clk
  %c0a = hw.constant 0 : i32
  %c0b = hw.constant 0 : i32
  %n1 = comb.mux bin %en, %d, %r1 : i32
  %r1 = seq.firreg %n1 clock %clock reset async %rst, %c0a : i32
  %n2 = comb.mux bin %en, %d, %r2 : i32
  %r2 = seq.firreg %n2 clock %clock reset async %rst, %c0b : i32
  hw.output %r1, %r2 : i32, i32
}
