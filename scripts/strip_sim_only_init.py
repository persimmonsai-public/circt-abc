#!/usr/bin/env python3
"""strip_sim_only_init.py -- remove CIRCT ExportVerilog's simulation-only
register-randomization `initial` blocks from a synthesis-bound netlist.

CIRCT's ExportVerilog wraps this content in `` `ifdef ENABLE_INITIAL_REG_``
/ `` `endif // ENABLE_INITIAL_REG_`` (defined via `` `ifndef SYNTHESIS``,
per the file's own header comment), intending it to compile out under
synthesis. Confirmed this session that this guard is NOT reliably honored
by feeding `-D SYNTHESIS` (nor an in-file `` `define SYNTHESIS`` at the top
of the file) to Yosys's `read_slang`: the block's `$random`-based content
still got synthesized, producing spurious `$dlatch`s that then got
const-propagated to 0, collapsing an ~12,000-cell design down to single
digits before ABC ever ran. Stripping the block outright, rather than
depending on a define mechanism that isn't behaving as documented here, is
simpler and unconditionally correct for a synthesis-only netlist (this
content is simulation-only regardless of tool-specific macro quirks).
"""
import re
import sys
from pathlib import Path

_BLOCK_RE = re.compile(
    r"^\s*`ifdef ENABLE_INITIAL_REG_.*?^\s*`endif // ENABLE_INITIAL_REG_\n?",
    re.DOTALL | re.MULTILINE,
)


def strip(path: Path) -> None:
    text = path.read_text()
    stripped, n = _BLOCK_RE.subn("", text)
    path.write_text(stripped)
    print(f"INFO: stripped {n} simulation-only ENABLE_INITIAL_REG_ block(s)")


if __name__ == "__main__":
    strip(Path(sys.argv[1]))
