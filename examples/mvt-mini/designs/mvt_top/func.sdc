# mvt_top, mode func: the nominal clock. Times in ns (the pack's time_unit).
create_clock -name clk -period 1.2 [get_ports clk_i]
set_input_delay 0.1 -clock clk [get_ports {en_i thresh_i b_i}]
set_output_delay 0.1 -clock clk [get_ports {acc_o over_o}]
