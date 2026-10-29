// A NON-identity arity-1 cell (inverter, function "(!A)") must NOT be
// touched: expected output identical to input (modulo nothing -- the pass
// must be a no-op here).
hw.module.extern @TESTINV(in %A : i1, out Y : i1)
hw.module @t2(in %a : i1, out o : i1) {
  %inv.Y = hw.instance "inv" @TESTINV(A: %a: i1) -> (Y: i1)
  hw.output %inv.Y : i1
}
