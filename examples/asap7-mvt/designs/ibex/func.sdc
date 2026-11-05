# ibex_top on asap7-mvt, mode func: 1500 ps at tt, a clock that only SLVT
# (in some runs) and the multi-Vt netlist meet after synthesis (README).
# Times in ps.
create_clock -name clk -period 1500 [get_ports clk_i]
# Inputs as if launched by a flop on the same clock: no setup budget taken
# (-max 0), and for hold a small clock-to-Q at the hold corner. With 0 here
# every input that reaches a flop D pin directly fails hold by the flop's
# hold time, an artifact of the constraints, not the netlist.
set_input_delay -max 0 -clock clk [delete_from_list [all_inputs] [get_ports clk_i]]
set_input_delay -min 10 -clock clk [delete_from_list [all_inputs] [get_ports clk_i]]
set_output_delay 0 -clock clk [all_outputs]
# The reset fans out to ~1000 flops; before P&R builds its buffer tree that
# net times as a multi-ns slew, so its paths are not timed here.
set_false_path -from [get_ports rst_ni]
