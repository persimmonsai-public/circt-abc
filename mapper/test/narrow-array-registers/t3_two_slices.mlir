// Test 3: observed at TWO distinct slices -> must NOT narrow.
hw.module @t3(in %clk : i1, in %en : i1, in %d : i8, out o0 : i8, out o1 : i8) {
  %c = seq.to_clock %clk
  %hold = hw.bitcast %F : (!hw.array<2xi8>) -> i16
  %obs0 = comb.extract %hold from 0 : (i16) -> i8
  %obs1 = comb.extract %hold from 8 : (i16) -> i8
  %w = comb.concat %obs1, %d : i8, i8
  %nx_i = comb.mux %en, %w, %hold : i16
  %nx = hw.bitcast %nx_i : (i16) -> !hw.array<2xi8>
  %F = seq.firreg %nx clock %c : !hw.array<2xi8>
  hw.output %obs0, %obs1 : i8, i8
}
