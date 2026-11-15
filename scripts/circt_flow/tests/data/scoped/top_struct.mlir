module {
  hw.module.extern @ICG(in %CK : i1, in %E : i1, in %SE : i1, out ECK : i1)
  hw.module.extern @DFF(in %CK : i1, in %D : i1, out Q : i1)
  hw.module.extern @DFF4(in %CK : i1, in %D0 : i1, in %D1 : i1, in %D2 : i1, in %D3 : i1, out Q0 : i1, out Q1 : i1, out Q2 : i1, out Q3 : i1)
  hw.module private @leaf(in %clk_i : i1, in %en : i1, in %x : i1, out o : i1) {
    %false = hw.constant false
    %clock_gate_0.ECK = hw.instance "clock_gate_0" @ICG(CK: %clk_i: i1, E: %en: i1, SE: %false: i1) -> (ECK: i1)
    %clock_gate_1.ECK = hw.instance "clock_gate_1" @ICG(CK: %clk_i: i1, E: %x: i1, SE: %false: i1) -> (ECK: i1)
    %q_cg0_mb0_3.Q0, %q_cg0_mb0_3.Q1, %q_cg0_mb0_3.Q2, %q_cg0_mb0_3.Q3 = hw.instance "q_cg0_mb0_3" @DFF4(CK: %clock_gate_0.ECK: i1, D0: %x: i1, D1: %x: i1, D2: %x: i1, D3: %x: i1) -> (Q0: i1, Q1: i1, Q2: i1, Q3: i1)
    %q_cg1_bit0.Q = hw.instance "q_cg1_bit0" @DFF(CK: %clock_gate_1.ECK: i1, D: %x: i1) -> (Q: i1)
    %h_bit0.Q = hw.instance "h_bit0" @DFF(CK: %clock_gate_0.ECK: i1, D: %x: i1) -> (Q: i1)
    %m_bit0.Q = hw.instance "m_bit0" @DFF(CK: %clk_i: i1, D: %x: i1) -> (Q: i1)
    hw.output %q_cg1_bit0.Q : i1
  }
  hw.module @top(in %clk_i : i1, in %en : i1, in %x : i1, out o : i1) {
    %u0.o = hw.instance "u0" @leaf(clk_i: %clk_i: i1, en: %en: i1, x: %x: i1) -> (o: i1)
    hw.output %u0.o : i1
  }
}
