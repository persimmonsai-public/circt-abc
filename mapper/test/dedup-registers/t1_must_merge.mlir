// Must-merge: two no-reset registers whose hold-muxes are identical modulo
// each register's own self-reference (the real ibex_if_stage replica shape).
// Expected with enable-register-dedup=true: ONE firreg remains; both output
// ports read the survivor.
hw.module @must_merge(in %clk : i1, in %en : i1, in %d : i32,
                      out o1 : i32, out o2 : i32) {
  %clock = seq.to_clock %clk
  %n1 = comb.mux bin %en, %d, %r1 : i32
  %r1 = seq.firreg %n1 clock %clock : i32
  %n2 = comb.mux bin %en, %d, %r2 : i32
  %r2 = seq.firreg %n2 clock %clock : i32
  hw.output %r1, %r2 : i32, i32
}
