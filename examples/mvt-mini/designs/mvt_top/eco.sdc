# mvt_top, mode eco: a slower clock (a lower-power operating point of the
# same netlist). Times in ns.
create_clock -name clk -period 1.6 [get_ports clk_i]
set_input_delay 0.1 -clock clk [get_ports {en_i thresh_i b_i}]
set_output_delay 0.1 -clock clk [get_ports {acc_o over_o}]
