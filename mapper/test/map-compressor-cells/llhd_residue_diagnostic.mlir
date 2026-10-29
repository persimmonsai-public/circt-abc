// RUN: not circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=8" %s 2>&1 | FileCheck %s
// LLHD residue left after lowering (here an llhd.combinational holding a
// loop) makes the comb -> datapath -> comb round trip fail; CIRCT's
// convert-datapath-to-comb reports nothing, so the pass must say what it hit
// instead of exiting 1 silently.

// CHECK: error: map-compressor-cells: LLHD op 'llhd.combinational' left after lowering

hw.module @mul(in %a : i8, in %b : i8, out p : i8) {
  %0 = comb.mul %a, %b : i8
  hw.output %0 : i8
}

hw.module @residue(in %n : i4, out y : i4) {
  %0 = llhd.combinational -> i4 {
    %c0 = hw.constant 0 : i4
    cf.br ^loop(%c0 : i4)
  ^loop(%i : i4):
    %c1 = hw.constant 1 : i4
    %next = comb.add %i, %c1 : i4
    %done = comb.icmp uge %next, %n : i4
    cf.cond_br %done, ^exit, ^loop(%next : i4)
  ^exit:
    llhd.yield %next : i4
  }
  hw.output %0 : i4
}
