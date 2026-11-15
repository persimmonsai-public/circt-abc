module top (clk_i, a, y);
  input clk_i;
  input a;
  output y;
  wire w;
  INVX u_inv (.A(a), .Y(w));
  DFFX q_bit0 (.CK(clk_i), .D(w), .Q(y));
endmodule
