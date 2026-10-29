// Identity buffer must be removed and its load rewired to the buffer's
// input: expected with enable-identity-buffer-removal=true (liberty=
// test_cells.lib): zero TESTBUF instances remain; the TESTINV instance's
// operand becomes %a directly. A buffer CHAIN (b2 fed by b1) collapses
// entirely.
hw.module.extern @TESTBUF(in %A : i1, out Y : i1)
hw.module.extern @TESTINV(in %A : i1, out Y : i1)
hw.module @t1(in %a : i1, out o : i1) {
  %b1.Y = hw.instance "b1" @TESTBUF(A: %a: i1) -> (Y: i1)
  %b2.Y = hw.instance "b2" @TESTBUF(A: %b1.Y: i1) -> (Y: i1)
  %inv.Y = hw.instance "inv" @TESTINV(A: %b2.Y: i1) -> (Y: i1)
  hw.output %inv.Y : i1
}
