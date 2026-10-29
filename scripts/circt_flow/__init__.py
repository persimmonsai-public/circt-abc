"""circt_flow -- Python orchestration for the CIRCT-native synthesis flow.

This package is a byte-for-byte-compatible Python port of
scripts/run_circt_native_synth.sh (see that file's header comment for the
full pipeline description / rationale). It is the new, intended way to run
the flow going forward; the bash script is left untouched as the reference
implementation during the transition.

Invocation:
    python3 -m circt_flow <flow.yaml> <out_dir>

from the scripts/ directory (or with scripts/ on PYTHONPATH), or via the
console entry point installed by this package's own __main__.py.
"""

from .config import FlowConfig

__all__ = ["FlowConfig"]
