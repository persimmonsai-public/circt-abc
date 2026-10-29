// Cell models for register_mapper_miter.sh on ASAP7, read after the
// liberty and overriding it.

// The ICG as a pass-through: register_mapper_miter.sh runs no clock gating,
// so the only ICG is the design's own, the same on both sides.
module ICGx1_ASAP7_75t_R(input CLK, input ENA, input SE, output GCLK);
  assign GCLK = CLK;
endmodule

// From the liberty ff group: ff(IQN,IQNN), clear "!SETN", preset "!RESETN",
// clear_preset_var1 L (clear wins), next_state "!D"; QN = IQN. Yosys 0.67's
// own model of it fires the preset whenever clear is inactive; 0.68's is
// correct, and this one stays as an independent reading of the liberty.
module DFFASRHQNx1_ASAP7_75t_R(input CLK, input D, input RESETN, input SETN,
                               output QN);
  reg IQN;
  always @(posedge CLK or negedge SETN or negedge RESETN)
    if (!SETN) IQN <= 1'b0;
    else if (!RESETN) IQN <= 1'b1;
    else IQN <= !D;
  assign QN = IQN;
endmodule
