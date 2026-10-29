// A buffer driving a top-level output port directly: removal leaves
// hw.output reading the module INPUT -- the input-aliases-output case.
// Expected: instance gone, `hw.output %a`, and (checked separately in the
// test script) circt-opt --export-verilog accepts the result.
hw.module.extern @TESTBUF(in %A : i1, out Y : i1)
hw.module @t3(in %a : i1, out o : i1) {
  %b.Y = hw.instance "b" @TESTBUF(A: %a: i1) -> (Y: i1)
  hw.output %b.Y : i1
}
