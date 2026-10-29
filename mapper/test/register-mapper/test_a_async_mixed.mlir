// Test A: >=4-bit async-reset firreg with a MIXED-bit constant resetValue
// (i4, reset value 0b1010 = 10). Expect: 2 async-clear instances (bits
// 0 and 2, where the constant reset bit is 0) and 2 async-preset
// instances (bits 1 and 3, where the constant reset bit is 1), each wired
// to the correct D bit via comb.extract, correct reset/set polarity, and a
// final comb.concat that reconstructs bit order MSB-first.
hw.module @areg(in %clk : !seq.clock, in %rst : i1, in %d : i4, out q : i4) {
  %rv = hw.constant 10 : i4
  %reg = seq.firreg %d clock %clk reset async %rst, %rv : i4
  hw.output %reg : i4
}
