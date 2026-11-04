// 0.55 V domain: a 16-bit LFSR feeding a running sum and a threshold
// compare. Slower clock, mostly off the critical path.
module core_lv (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic        en_i,
  input  logic [15:0] thresh_i,
  output logic [7:0]  sample_o,
  output logic        over_o
);
  logic [15:0] lfsr_q, sum_q;
  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni) begin
      lfsr_q <= 16'hACE1; sum_q <= '0; over_o <= 1'b0;
    end else if (en_i) begin
      lfsr_q <= {lfsr_q[14:0], lfsr_q[15] ^ lfsr_q[13] ^ lfsr_q[12] ^ lfsr_q[10]};
      sum_q  <= sum_q + lfsr_q;
      over_o <= sum_q > thresh_i;
    end
  assign sample_o = lfsr_q[7:0];
endmodule
