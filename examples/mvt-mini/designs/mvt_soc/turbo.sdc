# mvt_soc, mode turbo: the hv domain runs faster, the lv domain slower
# (its power goes to the hv side). Times in ns.
create_clock -name clk_hv -period 1.3 [get_ports clk_hv_i]
create_clock -name clk_lv -period 2.2 [get_ports clk_lv_i]
# Inputs as if launched by a clk_hv flop: no setup budget (-max 0), and
# for hold that flop's clock-to-Q at ff (~0.04 ns in the mini library).
set_input_delay -max 0 -clock clk_hv [get_ports {b_i en_i}]
set_input_delay -min 0.04 -clock clk_hv [get_ports {b_i en_i}]
set_output_delay 0 -clock clk_hv [get_ports {acc_o over_o}]
