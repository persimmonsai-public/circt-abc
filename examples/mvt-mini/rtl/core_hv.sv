// 0.75 V domain: an 8x8 multiply-accumulate. The multiplier-adder path is
// the critical one, so a tight clock makes threshold choice matter.
module core_hv (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic        en_i,
  input  logic [7:0]  a_i,
  input  logic [7:0]  b_i,
  output logic [19:0] acc_o
);
  logic [7:0] a_q, b_q;
  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni) begin
      a_q <= '0; b_q <= '0; acc_o <= '0;
    end else begin
      a_q <= a_i;
      b_q <= b_i;
      if (en_i) acc_o <= acc_o + 20'(a_q * b_q);
    end
endmodule
