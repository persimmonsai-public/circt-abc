// Test 1: fetch_fifo shape, no reset. MUST narrow to an i8 register.
// W's upper half reads the OWNED slice (allowed passthrough), lower = new data.
hw.module @t1(in %clk : i1, in %en : i1, in %d : i8, out o : i8) {
  %c = seq.to_clock %clk
  %hold = hw.bitcast %F : (!hw.array<2xi8>) -> i16
  %obs = comb.extract %hold from 0 : (i16) -> i8
  %w = comb.concat %obs, %d : i8, i8
  %nx_i = comb.mux %en, %w, %hold : i16
  %nx = hw.bitcast %nx_i : (i16) -> !hw.array<2xi8>
  %F = seq.firreg %nx clock %c : !hw.array<2xi8>
  hw.output %obs : i8
}
