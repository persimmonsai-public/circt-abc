// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=OFF
// RUN: circt-abc-opt "--clock-gate-inference=liberty=%S/icg.lib bit-level-enables=true select-dont-care=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=ON
//
// A counter with a synchronous clear on `start` and an increment that only
// happens away from the idle state:
//
//   q <= start ? 0 : (incr ? q + 1 : q)
//   start = idle & req_valid & ready        (req_valid, ready: late inputs)
//   incr  = !idle & step
//
// The bit-level path gates q on start | (!start & incr) and loads
// mux(start, 0, q + 1). Under that enable start == idle: the start cube
// implies idle, and the incr cube refutes it. So with select-dont-care the
// load selects on idle alone, and the late inputs reach only the clock
// gate's enable, not D.
//
// OFF: bit-level split 1 register(s), 4 bit(s) gated
// OFF: %[[IDLE:.+]] = comb.icmp eq %state
// OFF: %[[START:.+]] = comb.and %[[IDLE]], %req_valid, %ready
// OFF: %[[NSTART:.+]] = comb.xor %[[START]], %true
// OFF: comb.and %[[NSTART]], %{{.+}} : i1
// OFF: seq.firreg
//
// ON: bit-level split 1 register(s), 4 bit(s) gated, 4 load select(s) rewritten
// ON: %[[IDLE:.+]] = comb.icmp eq %state
// ON: %[[START:.+]] = comb.and %[[IDLE]], %req_valid, %ready
// ON: %[[NIDLE:.+]] = comb.xor %[[IDLE]], %true
// ON: %[[E:.+]] = comb.or %[[START]], %{{.+}} : i1
// ON-NOT: comb.mux
// ON-COUNT-4: comb.and %[[NIDLE]], %{{[0-9]+}} : i1
// ON-NEXT: comb.concat
// ON-NEXT: seq.firreg
// ON: @TICG(CK: %{{.+}}: i1, E: %[[E]]: i1

hw.module @counter(in %clk : !seq.clock, in %rst : i1, in %state : i2,
                   in %req_valid : i1, in %ready : i1, in %step : i1,
                   out q : i4) {
  %c0_i2 = hw.constant 0 : i2
  %c0_i4 = hw.constant 0 : i4
  %c1_i4 = hw.constant 1 : i4
  %true = hw.constant true
  %idle = comb.icmp eq %state, %c0_i2 : i2
  %start = comb.and %idle, %req_valid, %ready : i1
  %nidle = comb.xor %idle, %true : i1
  %incr = comb.and %nidle, %step : i1
  %inc = comb.add %q, %c1_i4 : i4
  %held = comb.mux %incr, %inc, %q : i4
  %next = comb.mux %start, %c0_i4, %held : i4
  %q = seq.firreg %next clock %clk reset async %rst, %c0_i4 : i4
  hw.output %q : i4
}
