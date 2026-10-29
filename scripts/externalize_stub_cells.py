#!/usr/bin/env python3
"""externalize_stub_cells.py -- turn specific real-standard-cell placeholder
modules into true `hw.module.extern` declarations in CIRCT's MLIR output.

Root cause this fixes: circt-verilog's ImportVerilog frontend has no source-
level "blackbox module" concept (confirmed by inspecting
lib/Conversion/ImportVerilog/Structure.cpp -- extern handling exists only for
DPI-C `extern` *functions*, never for whole modules). A SystemVerilog module
given an empty body (as scripts/lower_ibex.sh's clock-gate stub does for the
library ICG cell, the one real standard cell instantiated directly in ibex
RTL) still gets fully elaborated: its unconnected `ECK` output net is
SV-semantically X, and CIRCT's HW dialect has no X value, so ImportVerilog
materializes a concrete `hw.constant false` and drives the output with it
(confirmed directly in ibex_top.mlir: `hw.module private @<ICG cell>(...) {
%false = hw.constant false; hw.output %false }`). Because this cell gates the
clock feeding most of ibex_core, that constant-0 propagates through
`--lower-seq-to-sv` into a literal `assign ECK = 1'h0;` in the exported
Verilog -- which is why Yosys's OPT_DFF pass later reports "Handling const
CLK" on nearly every register in the design (their clock net traces back to
this constant), and the $dlatch-collapse symptom chased earlier this session
was downstream fallout of THIS, not an independent Yosys latch-inference bug.

A Yosys-based (sv2v) flow never hits this: its RTL
instantiates the same cell with no local module definition at all, and
synth.tcl's `read_liberty -lib $lib` (run before RTL is read) populates
Yosys's shared design database with a real blackbox stand-in for it, so the
plain `read_verilog` frontend never has to elaborate a body for it either.
CIRCT's frontend requires a real declaration to bind a named-port
instantiation (confirmed: `--ignore-unknown-modules` fails with "unsupported
module member: UninstantiatedDef" for exactly this reason), so we cannot
just omit the module -- we let CIRCT elaborate it (to get correct port
binding) and then rewrite the *result* to drop the fabricated body, matching
what the original flow relied on Yosys to do for free.

Usage: externalize_stub_cells.py <mlir-file> <module-name> [<module-name>...]
Rewrites the file in place.
"""
import re
import sys
from pathlib import Path


def externalize(text: str, module_name: str) -> tuple[str, int]:
    # Matches `hw.module [private] @Name(<ports>) {` ... matching `  }` on its
    # own line (CIRCT's ExportVerilog-adjacent pretty-printer always closes a
    # top-level hw.module body with a `}` at 2-space indent, one level in
    # from the enclosing `module { ... }` wrapper) -- non-greedy so multiple
    # unrelated modules elsewhere in the file are never spanned.
    pattern = re.compile(
        r"hw\.module(?P<private> private)?"
        r" @" + re.escape(module_name) + r"\((?P<ports>.*?)\) \{\n"
        r".*?\n  \}\n",
        re.DOTALL,
    )

    def repl(m: re.Match) -> str:
        private = m.group("private") or ""
        return f"hw.module.extern{private} @{module_name}({m.group('ports')})\n"

    return pattern.subn(repl, text)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    path = Path(sys.argv[1])
    text = path.read_text()
    for module_name in sys.argv[2:]:
        text, n = externalize(text, module_name)
        if n != 1:
            print(
                f"ERROR: expected exactly one definition of {module_name!r} to "
                f"externalize, found {n}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"INFO: externalized {module_name} (1 occurrence)")
    path.write_text(text)


if __name__ == "__main__":
    main()
