// Behavioral SRAM models for the top-level miter.
//
// These must DRIVE their outputs. An undriven blackbox output is X on both
// sides of the miter, compares equal, and can collapse the proof into a
// vacuous SUCCESS -- the failure mode recorded in `empty-cell-stubs-break-
// miters`. A real array also keeps the check faithful: a difference written
// to an address and read back later still shows up.
//
// Depths are the full address space of each port, which matches the real
// instances (64 x 28, 128 x 64 byte-enabled, 64 x 25, 64 x 256 byte-enabled).
module hpdcache_sram_1rw(input clk, input rst_n, input cs, input we,
                         input [5:0] addr, input [27:0] wdata,
                         output reg [27:0] rdata);
  reg [27:0] mem [0:63];
  always @(posedge clk) if (cs) begin
    if (we) mem[addr] <= wdata;
    else    rdata     <= mem[addr];
  end
endmodule

module hpdcache_sram_wbyteenable_1rw(input clk, input rst_n, input cs,
                                     input we, input [6:0] addr,
                                     input [63:0] wdata, input [7:0] wbyteenable,
                                     output reg [63:0] rdata);
  reg [63:0] mem [0:127];
  integer i;
  always @(posedge clk) if (cs) begin
    if (we) begin
      for (i = 0; i < 8; i = i + 1)
        if (wbyteenable[i]) mem[addr][i*8 +: 8] <= wdata[i*8 +: 8];
    end else rdata <= mem[addr];
  end
endmodule

// tc_sram's byte enables are 4 bits over 25 data bits: the last group is the
// 1-bit remainder, so the loop is written to clamp rather than assume 8.
module tc_sram(input clk_i, input rst_ni, input req_i, input we_i,
               input [5:0] addr_i, input [24:0] wdata_i, input [3:0] be_i,
               output reg [24:0] rdata_o);
  reg [24:0] mem [0:63];
  integer i, b;
  always @(posedge clk_i) if (req_i) begin
    if (we_i) begin
      for (i = 0; i < 4; i = i + 1)
        if (be_i[i])
          for (b = 0; b < 8; b = b + 1)
            if (i*8 + b < 25) mem[addr_i][i*8 + b] <= wdata_i[i*8 + b];
    end else rdata_o <= mem[addr_i];
  end
endmodule

module tc_sram_11(input clk_i, input rst_ni, input req_i, input we_i,
                  input [5:0] addr_i, input [255:0] wdata_i, input [31:0] be_i,
                  output reg [255:0] rdata_o);
  reg [255:0] mem [0:63];
  integer i;
  always @(posedge clk_i) if (req_i) begin
    if (we_i) begin
      for (i = 0; i < 32; i = i + 1)
        if (be_i[i]) mem[addr_i][i*8 +: 8] <= wdata_i[i*8 +: 8];
    end else rdata_o <= mem[addr_i];
  end
endmodule
