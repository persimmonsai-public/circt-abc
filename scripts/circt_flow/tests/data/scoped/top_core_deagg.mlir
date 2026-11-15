module {
  hw.module private @leaf(in %clk_i : i1, in %rst_ni : i1, in %en : i1, in %d : i16, in %e : i8, in %a : i8, in %b : i8, in %c : i12, in %f : i12, in %g : i20, out o : i16, out p : i8, out r : i8, out s1 : i8, out s2 : i12, out s3 : i12, out s4 : i20, out m1 : i8, out sh : i16) {
    %true = hw.constant true
    %false = hw.constant false
    %c0_i16 = hw.constant 0 : i16
    %c1_i8 = hw.constant 1 : i8
    %clk = seq.to_clock %clk_i
    %rst = comb.xor %rst_ni, %true : i1
    %0 = comb.mux bin %en, %d, %q : i16
    %q = seq.firreg %0 clock %clk reset async %rst, %c0_i16 : i16
    %1 = comb.mux %en, %h, %e : i8
    %h = seq.firreg %1 clock %clk : i8
    %2 = comb.xor %e, %m : i8
    %m = seq.firreg %2 clock %clk : i8
    %3 = comb.add %a, %b : i8
    %4 = comb.add %c, %f : i12
    %5 = comb.sub %c, %f : i12
    %6 = comb.add %g, %g : i20
    %7 = comb.mul %a, %b : i8
    %8 = comb.shl %d, %d : i16
    %9 = comb.add %a, %c1_i8 : i8
    hw.output %q, %h, %m, %3, %4, %5, %6, %7, %8 : i16, i8, i8, i8, i12, i12, i20, i8, i16
  }
  hw.module private @other(in %clk_i : i1, in %en : i1, in %d : i8, in %c : i12, out o : i8, out s : i12) {
    %clk = seq.to_clock %clk_i
    %0 = comb.mux %en, %d, %q : i8
    %q = seq.firreg %0 clock %clk : i8
    %1 = comb.add %c, %c : i12
    hw.output %q, %1 : i8, i12
  }
  hw.module @top(in %clk_i : i1, in %rst_ni : i1, in %en : i1, in %d : i16, in %e : i8, in %a : i8, in %b : i8, in %c : i12, in %f : i12, in %g : i20, out o1 : i16, out o2 : i16, out o3 : i8) {
    %u0.o, %u0.p, %u0.r, %u0.s1, %u0.s2, %u0.s3, %u0.s4, %u0.m1, %u0.sh = hw.instance "u0" @leaf(clk_i: %clk_i: i1, rst_ni: %rst_ni: i1, en: %en: i1, d: %d: i16, e: %e: i8, a: %a: i8, b: %b: i8, c: %c: i12, f: %f: i12, g: %g: i20) -> (o: i16, p: i8, r: i8, s1: i8, s2: i12, s3: i12, s4: i20, m1: i8, sh: i16)
    %u1.o, %u1.p, %u1.r, %u1.s1, %u1.s2, %u1.s3, %u1.s4, %u1.m1, %u1.sh = hw.instance "u1" @leaf(clk_i: %clk_i: i1, rst_ni: %rst_ni: i1, en: %en: i1, d: %d: i16, e: %e: i8, a: %a: i8, b: %b: i8, c: %c: i12, f: %f: i12, g: %g: i20) -> (o: i16, p: i8, r: i8, s1: i8, s2: i12, s3: i12, s4: i20, m1: i8, sh: i16)
    %u2.o, %u2.s = hw.instance "u2" @other(clk_i: %clk_i: i1, en: %en: i1, d: %e: i8, c: %c: i12) -> (o: i8, s: i12)
    hw.output %u0.o, %u1.o, %u2.o : i16, i16, i8
  }
}
