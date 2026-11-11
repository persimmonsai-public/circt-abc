# align_unit, mode func: 400 ps at tt. Times in ps.
create_clock -name clk -period 400 [get_ports clk_i]
set_input_delay -max 0 -clock clk [delete_from_list [all_inputs] [get_ports clk_i]]
set_input_delay -min 10 -clock clk [delete_from_list [all_inputs] [get_ports clk_i]]
set_output_delay 0 -clock clk [all_outputs]
set_false_path -from [get_ports rst_ni]
