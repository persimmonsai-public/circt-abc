// Two power domains: core_lv at 0.55 V drives core_hv at 0.75 V. Each core
// is synthesized on its own supply's libraries (designs/mvt_top/flow.yaml's
// power: section); the glue here (one flop and a gate) is on the default
// rail. The signals crossing between rails (sample, over_lv up from 0.55 V;
// core_lv's inputs down) get level shifters (lib/mini_ls.lib: MLSLH_X1
// low-to-high, MLSHL_X1 high-to-low) when designs/mvt_top/flow_ls.yaml runs.
module mvt_top (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic        en_i,
  input  logic [15:0] thresh_i,
  input  logic [7:0]  b_i,
  output logic [19:0] acc_o,
  output logic        over_o
);
  logic [7:0] sample;
  logic       over_lv;
  core_lv u_lv (.clk_i, .rst_ni, .en_i, .thresh_i, .sample_o(sample),
                .over_o(over_lv));
  core_hv u_hv (.clk_i, .rst_ni, .en_i, .a_i(sample), .b_i, .acc_o);
  // Glue on the default rail: over, unless the accumulator has wrapped.
  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni) over_o <= 1'b0;
    else         over_o <= over_lv & ~acc_o[19];
endmodule
