// Test 6: the real ibex_id_stage imd_val_q shape -- TWO array-register
// copies from the same generate-loop-per-entry lowering, entangled through
// an or-resolved signal: each copy's write value and reset value read the
// OTHER copy's owned slice (via %res), and each copy's reset's OWN owned
// slice is constant 0 reachable only by folding extract-through-concat.
// BOTH must narrow (to i8 each), with constant-0 narrowed resets.
//
// This is a regression lock for a stale claim ("imd_val's copies carry
// entangled non-constant reset values ... the conservative matcher
// correctly declines them") that turned out to be an artifact of running
// the pass on PRE-deagg IR: on the pipeline's real post-deagg input this
// exact shape narrows, and an id_stage-scoped sequential BMC (pass-off
// vs. pass-on, depths 2/6/10, -set-init-zero) proves the rewrite sound.
// Run: circt-abc-opt --narrow-array-registers t6_entangled_pair.mlir
// Expect: "narrowed 2 array firreg(s), dropping 16 dead bit(s)".
hw.module @t6(in %clk : i1, in %rst : i1, in %we : i2, in %d : i16, out o : i16) {
  %c = seq.to_clock %clk
  %c0_i8 = hw.constant 0 : i8
  %din = comb.extract %d from 0 : (i16) -> i8      // new data, low entry
  %din_h = comb.extract %d from 8 : (i16) -> i8    // new data, high entry

  // Copy A owns the HIGH slice [8,16); copy B owns the LOW slice [0,8).
  %fa_i = hw.bitcast %FA : (!hw.array<2xi8>) -> i16
  %obs_a = comb.extract %fa_i from 8 : (i16) -> i8
  %fb_i = hw.bitcast %FB : (!hw.array<2xi8>) -> i16
  %obs_b = comb.extract %fb_i from 0 : (i16) -> i8

  // Resolved signal: high half from A's owned read, low half from B's,
  // combined through or (mirrors the real %3 = or(concat, concat) shape).
  %ra = comb.concat %obs_a, %c0_i8 : i8, i8
  %rb = comb.concat %c0_i8, %obs_b : i8, i8
  %res = comb.or %ra, %rb : i16
  %res_lo = comb.extract %res from 0 : (i16) -> i8
  %res_hi = comb.extract %res from 8 : (i16) -> i8

  // Copy A: writes its high entry with new data, passes B's low entry
  // through the resolved signal; reset = {0, res_lo} -- owned slice
  // [8,16) of the reset is the constant 0.
  %wea = comb.extract %we from 0 : (i2) -> i1
  %wa = comb.concat %din_h, %res_lo : i8, i8
  %rva_i = comb.concat %c0_i8, %res_lo : i8, i8
  %rva = hw.bitcast %rva_i : (i16) -> !hw.array<2xi8>
  %nxa_i = comb.mux %wea, %wa, %fa_i : i16
  %nxa = hw.bitcast %nxa_i : (i16) -> !hw.array<2xi8>
  %FA = seq.firreg %nxa clock %c reset async %rst, %rva : !hw.array<2xi8>

  // Copy B: writes its low entry, passes A's high entry through; reset =
  // {res_hi, 0} -- owned slice [0,8) of the reset is the constant 0.
  %web = comb.extract %we from 1 : (i2) -> i1
  %wb = comb.concat %res_hi, %din : i8, i8
  %rvb_i = comb.concat %res_hi, %c0_i8 : i8, i8
  %rvb = hw.bitcast %rvb_i : (i16) -> !hw.array<2xi8>
  %nxb_i = comb.mux %web, %wb, %fb_i : i16
  %nxb = hw.bitcast %nxb_i : (i16) -> !hw.array<2xi8>
  %FB = seq.firreg %nxb clock %c reset async %rst, %rvb : !hw.array<2xi8>

  hw.output %res : i16
}
