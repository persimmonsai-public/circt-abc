// RUN: circt-abc-opt --narrow-whole-value-reads --cse %s 2>&1 | FileCheck %s
// The whole-value-read false cycle: sig2reg merges the drives of a
// partially-driven word and resolves every slice read as an extract of the
// merged word, so a tree `a[k] = f(a[2k+1], a[2k+2])` becomes word = f(word).
// Here: a 4-bit word whose bit 0 is or(bit1, bit2) and bit 1 is or(bit2, bit3),
// bits 2 and 3 are inputs. Word-level SCC, bit-level acyclic.

// CHECK-LABEL: hw.module @lzc_like
// After narrowing the merged i4 word and every extract of it are gone; the
// two results are 1-bit ors of the inputs.
// CHECK-NOT: comb.extract
// CHECK-NOT: i4
// CHECK: hw.output
hw.module @lzc_like(in %c : i1, in %d : i1, out y0 : i1, out y1 : i1) {
  %zero2 = hw.constant 0 : i2
  // drive 1: bits [3:2] = {d, c}; drive 2: bit 1; drive 3: bit 0
  %hi = comb.concat %d, %c, %zero2 : i1, i1, i2
  %b1x = comb.concat %zero1a, %b1, %zero1b : i2, i1, i1
  %zero1a = hw.constant 0 : i2
  %zero1b = hw.constant 0 : i1
  %b0x = comb.concat %zero3, %b0 : i3, i1
  %zero3 = hw.constant 0 : i3
  %word = comb.or %hi, %b1x, %b0x : i4
  %r1 = comb.extract %word from 1 : (i4) -> i1
  %r2 = comb.extract %word from 2 : (i4) -> i1
  %r3 = comb.extract %word from 3 : (i4) -> i1
  %b1 = comb.or %r2, %r3 : i1
  %b0 = comb.or %r1, %r2 : i1
  hw.output %b0, %b1 : i1, i1
}

// A module without a word-level cycle is untouched by the pass itself
// (canonicalize would narrow this extract, the pass must not).
// CHECK-LABEL: hw.module @acyclic
// CHECK: comb.extract %{{.*}} from 1
hw.module @acyclic(in %a : i4, in %b : i4, out y : i1) {
  %w = comb.or %a, %b : i4
  %e = comb.extract %w from 1 : (i4) -> i1
  hw.output %e : i1
}
