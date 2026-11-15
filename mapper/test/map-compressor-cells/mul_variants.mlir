// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 tree=wallace" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=WAL
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 tree=array" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=ARR
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 gate-compressors=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=GATE
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 cell-stages=1" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=STG
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 nand-pp=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=NAND
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 map-final-adder=true" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=MAP
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 map-final-adder=true map-final-min-width=15" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=MINW
// RUN: circt-abc-opt "--map-compressor-cells=enable-compressor-cells=true liberty=%S/adders.lib min-width=4 map-final-adder=true map-final-min-width=17" --canonicalize --cse %s 2>&1 | FileCheck %s --check-prefix=MINW0
//
// Tree-shape and cell-use options of map-compressor-cells on the 8x8 multiply of mul8_dadda.mlir (all default-off).
// Counts are of the test library adders.lib cells.
//
// WAL: tree=wallace gate FA/HA 0/0
// ARR: tree=array gate FA/HA 0/0
// GATE: -> 0 TFA + 0 THA
// GATE: gate FA/HA 42/8
// STG: gate FA/HA 32/4
// NAND: nand-pp bits 64
// MAP: final adds tagged 2
// MAP: circt_abc.no_adder_cells
// map-final-min-width=N: only the final adds at least N wide are tagged (the 16-bit product add,
// not the 8-bit one); above every width none is.
// MINW: final adds tagged 1
// MINW: comb.add bin {{.*}} {circt_abc.no_adder_cells} : i16
// MINW-NOT: {circt_abc.no_adder_cells} : i8
// MINW0: final adds tagged 0
// MINW0-NOT: circt_abc.no_adder_cells

hw.module @mul8(in %a : i8, in %b : i8, in %c : i8, out p : i16, out q : i8) {
  %ae = comb.concat %z, %a : i8, i8
  %be = comb.concat %z, %b : i8, i8
  %z = hw.constant 0 : i8
  %p = comb.mul %ae, %be : i16
  %q = comb.add %a, %b, %c : i8
  hw.output %p, %q : i16, i8
}
