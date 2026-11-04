// mvt_top with one clock per power domain, for modes: core_lv (0.55 V) on
// clk_lv_i, core_hv (0.75 V) and the glue on clk_hv_i. Each mode's SDC
// (designs/mvt_soc/*.sdc) sets the two clocks' periods; the flow traces
// which clock reaches which domain through the elaborated hierarchy.
// sample and over_lv cross from the lv clock to the hv clock: an example,
// not a synchronizer.
module mvt_soc (
  input  logic        clk_lv_i,
  input  logic        clk_hv_i,
  input  logic        rst_ni,
  input  logic        en_i,
  input  logic [15:0] thresh_i,
  input  logic [7:0]  b_i,
  output logic [19:0] acc_o,
  output logic        over_o
);
  logic [7:0] sample;
  logic       over_lv;
  core_lv u_lv (.clk_i(clk_lv_i), .rst_ni, .en_i, .thresh_i,
                .sample_o(sample), .over_o(over_lv));
  core_hv u_hv (.clk_i(clk_hv_i), .rst_ni, .en_i, .a_i(sample), .b_i, .acc_o);
  always_ff @(posedge clk_hv_i or negedge rst_ni)
    if (!rst_ni) over_o <= 1'b0;
    else         over_o <= over_lv & ~acc_o[19];
endmodule
