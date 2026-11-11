// A variable left shift written as a `case`: a 16-bit word is shifted by a 4-bit
// amount into a 32-bit result, one arm per amount (the form map-shift-select
// recognises). Registered at both ends so the timed paths run register to
// register.
module align_unit (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic [3:0]  amt_i,
  input  logic [15:0] x_i,
  output logic [31:0] y_o
);
  logic [3:0]  amt_q;
  logic [15:0] x_q;
  logic [31:0] y_d;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      amt_q <= '0;
      x_q   <= '0;
      y_o   <= '0;
    end else begin
      amt_q <= amt_i;
      x_q   <= x_i;
      y_o   <= y_d;
    end
  end

  logic [31:0] w;
  assign w = {16'b0, x_q};

  always_comb begin
    unique case (amt_q)
      4'd0:    y_d = w;
      4'd1:    y_d = w << 1;
      4'd2:    y_d = w << 2;
      4'd3:    y_d = w << 3;
      4'd4:    y_d = w << 4;
      4'd5:    y_d = w << 5;
      4'd6:    y_d = w << 6;
      4'd7:    y_d = w << 7;
      4'd8:    y_d = w << 8;
      4'd9:    y_d = w << 9;
      4'd10:   y_d = w << 10;
      4'd11:   y_d = w << 11;
      4'd12:   y_d = w << 12;
      4'd13:   y_d = w << 13;
      4'd14:   y_d = w << 14;
      default: y_d = w << 15;
    endcase
  end
endmodule
