// Test 4: imd_val shape, async reset whose owned slice is constant 0,
// dead slice of the reset reads the live slice (allowed). MUST narrow,
// and the new register's reset value must fold to a constant.
hw.module @t4(in %clk : i1, in %rst : i1, in %en : i1, in %d : i8, out o : i8) {
  %c = seq.to_clock %clk
  %c0_i8 = hw.constant 0 : i8
  %hold = hw.bitcast %F : (!hw.array<2xi8>) -> i16
  %obs = comb.extract %hold from 0 : (i16) -> i8
  %rv_i = comb.concat %obs, %c0_i8 : i8, i8
  %rv = hw.bitcast %rv_i : (i16) -> !hw.array<2xi8>
  %w = comb.concat %obs, %d : i8, i8
  %nx_i = comb.mux %en, %w, %hold : i16
  %nx = hw.bitcast %nx_i : (i16) -> !hw.array<2xi8>
  %F = seq.firreg %nx clock %c reset async %rst, %rv : !hw.array<2xi8>
  hw.output %obs : i8
}
