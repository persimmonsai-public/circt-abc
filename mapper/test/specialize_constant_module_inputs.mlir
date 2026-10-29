// RUN: circt-abc-opt --specialize-constant-module-inputs=enable-specialize-constant-inputs=true --canonicalize %s | FileCheck %s
// RUN: circt-abc-opt --canonicalize %s | FileCheck %s --check-prefix=DEFAULT
//
// The inward dual of `prune-unused-residual-outputs
// propagate-constant-outputs=true`. That pass substitutes a callee's
// constant OUTPUTS at its callers; nothing specialised a module body on a
// constant it is always GIVEN, so a block its parent ties off stayed live
// all the way to the netlist. CVA6's hardware stride prefetcher is the
// measured case: `cva6.sv` ties it off (the upstream RTL flags this with
// its own `/*FIXME*/`), but the block sits three port boundaries below the
// tie-off, where the constants are block arguments.
//
// Off by default, and design-level sound only -- it changes what a module
// computes for inputs it is never given -- so it must be gated by a
// TOP-LEVEL miter, never a per-module one.

// Every instance agrees on the literal, so the body sees it.
// CHECK-LABEL: hw.module private @agree
// The port itself deliberately STAYS in the signature, so every instance
// signature remains valid and no caller has to be rewritten; canonicalize
// then collapses what the constant enables.
// CHECK-NOT:     comb.mux
// CHECK:         hw.output %c0_i8
// DEFAULT-LABEL: hw.module private @agree
// DEFAULT:       comb.mux
hw.module private @agree(in %en : i1, in %d : i8, out o : i8) {
  %c0_i8 = hw.constant 0 : i8
  %r = comb.mux %en, %d, %c0_i8 : i8
  hw.output %r : i8
}

// Instances disagree: nothing is substituted. The pass is not allowed to
// pick one caller's view.
// CHECK-LABEL: hw.module private @disagree
// CHECK:         comb.mux
hw.module private @disagree(in %en : i1, in %d : i8, out o : i8) {
  %c0_i8 = hw.constant 0 : i8
  %r = comb.mux %en, %d, %c0_i8 : i8
  hw.output %r : i8
}

// An aggregate literal, the shape the real tie-offs actually take
// (`hw.aggregate_constant`, not `hw.constant`). Canonicalize folds the
// array_get through it, so the whole body becomes the element constant.
// CHECK-LABEL: hw.module private @agg
// CHECK-NOT:     hw.array_get
// CHECK:         hw.output %c0_i8
// DEFAULT-LABEL: hw.module private @agg
// DEFAULT:       hw.array_get
hw.module private @agg(in %cfg : !hw.array<2xi8>, out o : i8) {
  %c0 = hw.constant 0 : i1
  %e = hw.array_get %cfg[%c0] : !hw.array<2xi8>, i1
  hw.output %e : i8
}

hw.module @top(in %d : i8, in %t : i1, out a : i8, out b : i8, out c : i8, out g : i8) {
  %false = hw.constant false
  %zeros = hw.aggregate_constant [0 : i8, 0 : i8] : !hw.array<2xi8>
  %a = hw.instance "a0" @agree(en: %false: i1, d: %d: i8) -> (o: i8)
  %a2 = hw.instance "a1" @agree(en: %false: i1, d: %d: i8) -> (o: i8)
  // One instance drives a constant, the other does not.
  %b = hw.instance "b0" @disagree(en: %false: i1, d: %d: i8) -> (o: i8)
  %b2 = hw.instance "b1" @disagree(en: %t: i1, d: %d: i8) -> (o: i8)
  %g = hw.instance "g0" @agg(cfg: %zeros: !hw.array<2xi8>) -> (o: i8)
  %sum = comb.add %a, %a2 : i8
  hw.output %sum, %b, %b2, %g : i8, i8, i8, i8
}

// A root module -- nothing instantiates @top -- must be left alone: its
// inputs are design inputs, not a caller's constants.
// CHECK-LABEL: hw.module @top
// CHECK:         hw.instance
