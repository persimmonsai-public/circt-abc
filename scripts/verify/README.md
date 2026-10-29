# Verification gates

Internal — see `/CLAUDE.md` before sharing.

## Which gate for which pass

| pass changes | gate | why |
| :--- | :--- | :--- |
| logic inside one module | per-module miter (`scripts/run_equivalence_check.sh`) | cheapest that can fail |
| what a module computes for inputs it never gets, or bits no caller reads | **`miter.sh` — top level only** | a per-module miter is invalid by construction |
| the mapped netlist, from an unchanged AIG | `scripts/physdp/cec_gate.sh` | combinational, purpose-built |

`specialize-constant-module-inputs`, `narrow-dead-output-bits` and the
sequential options on `propagate-register-constants` are all in the middle
row. They are design-level sound only.

## Running it

```bash
# Build gold and gate as PRE-REGISTER-MAPPER Verilog from the same input.
circt-abc-opt <baseline passes> cva6_core_deagg.mlir -o gold.mlir
circt-abc-opt <arm passes>      cva6_core_deagg.mlir -o gate.mlir
for a in gold gate; do
  circt-opt $a.mlir --lower-seq-to-sv --export-verilog -o /dev/null > $a.v
done

scripts/verify/miter.sh gold.v gate.v 4        # expect: no model found: SUCCESS!
scripts/verify/miter_control.sh gold.v output  # expect: model found: FAIL!
```

**Both lines are the gate.** A SUCCESS whose control did not fail means
nothing.

## Three ways this has produced a false pass

1. **Empty cell stubs.** Gating on a *mapped* netlist whose cell stubs have
   undriven outputs: both sides are X, compare equal, proof collapses.
   Export before `--register-mapper`.
2. **Undriven SRAM blackboxes.** Same mechanism. `sram_models.sv` exists so
   the memories *drive* their outputs.
3. **A control that cannot discriminate.** Inverting an SRAM read path
   returns SUCCESS — not because the miter is broken, but because within the
   bound nothing reads a cache line back to an output. `miter_control.sh`
   keeps that mode precisely so the limitation stays documented. Use
   `output` mode, which is observable by construction.

## What the verdict means

A depth-*N* run proves equivalence for *N* cycles out of reset, **not**
unbounded equivalence. The reference cv32a60x gate: SUCCESS at 9,026,815
variables / 24,395,668 clauses / 33,242 cells / 5,130 flops, depth 4, with
`rst_ni` deasserted at cycle 1. A verdict arriving with far fewer variables
means the design optimised away — treat it as no verdict at all.
